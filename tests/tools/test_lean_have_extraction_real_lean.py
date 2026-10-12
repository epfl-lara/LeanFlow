"""Real-Lean regression for verified local-have extraction.

Runs only with ``LEANFLOW_TEST_LEAN_PROJECT`` pointing at a prebuilt Mathlib
project.  The project is never touched: an isolated copy reuses its prebuilt
dependency artifacts, the way the type-profile integration test does.
"""

import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from tools.implementations.lean_have_extraction import lean_extract_have_tool

pytestmark = pytest.mark.integration

REAL_LEAN_TIMEOUT_S = 1800


def _timeout_handler(signum, frame):
    raise TimeoutError(f"Real-Lean extraction test exceeded {REAL_LEAN_TIMEOUT_S} seconds")


@pytest.fixture(autouse=True)
def _enforce_test_timeout():
    """Replace the suite-wide 30 second alarm: cold Mathlib checks take minutes."""
    old = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(REAL_LEAN_TIMEOUT_S)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)


SOURCE = """import Mathlib

theorem demo {α : Type*} [DecidableEq α] (m : ℕ) (a : ℝ) (_ha : 0 < a) (F : Finset α)
    (hne : F.Nonempty) : F.card = F.card := by
  classical
  let x := Real.exp (-a)
  have hx0 : 0 ≤ x := (Real.exp_pos _).le
  have hm : 0 ≤ m := Nat.zero_le _
  have hcoord : ∀ S : Finset ℤ, 0 ≤ (S.sum fun z => x ^ z.natAbs) := by
    intro S
    have _hm' : 0 ≤ m := hm
    exact Finset.sum_nonneg fun z _ => pow_nonneg hx0 _
  let B := F
  have hFB : F ⊆ B := by
    intro y hy
    exact hy
  rfl
"""


@pytest.fixture
def real_project(tmp_path: Path) -> Path:
    """Make an isolated project while reusing only prebuilt dependency artifacts."""
    configured = os.environ.get("LEANFLOW_TEST_LEAN_PROJECT")
    if not configured:
        pytest.skip("Set LEANFLOW_TEST_LEAN_PROJECT to a prebuilt Lean project for integration")
    base = Path(configured).resolve()
    root = tmp_path / "project"
    root.mkdir()
    for name in ("lakefile.toml", "lakefile.lean", "lake-manifest.json", "lean-toolchain"):
        if (base / name).is_file():
            shutil.copyfile(base / name, root / name)
    (root / ".lake").mkdir()
    (root / ".lake" / "packages").symlink_to(base / ".lake" / "packages", target_is_directory=True)
    subprocess.run(
        ["lake", "env", "lean", "--version"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return root


def _extract(root: Path, target: Path, have_name: str) -> dict:
    return json.loads(
        lean_extract_have_tool(
            "demo",
            str(target),
            cwd=str(root),
            have_names=[have_name],
            minimum_lines=2,
            timeout_s=900,
        )
    )


def test_extracts_forall_goal_and_let_context_haves(real_project: Path) -> None:
    target = real_project / "Demo.lean"
    target.write_text(SOURCE, encoding="utf-8")

    # `hcoord` starts with a named ∀, so extract_goal prints a Pi type; its proof
    # also uses `hm`, which the relevance cleanup drops, so the full context is used.
    first = _extract(real_project, target, "hcoord")
    assert first["success"] is True, json.dumps(first, indent=1)[:4000]
    assert first["extraction"]["extraction_modes"] == ["full_context"]

    # `hFB` is a plain goal whose relevant context is a let; the minimal helper suffices.
    second = _extract(real_project, target, "hFB")
    assert second["success"] is True, json.dumps(second, indent=1)[:4000]
    assert second["extraction"]["extraction_modes"] == ["cleanup"]

    text = target.read_text(encoding="utf-8")
    assert "private lemma leanflow_demo_hcoord" in text
    assert "private lemma leanflow_demo_hFB" in text
    assert "exact @leanflow_demo_hcoord " in text
    assert "exact @leanflow_demo_hFB " in text
    assert "solve_by_elim" not in text

    compiled = subprocess.run(
        ["lake", "env", "lean", str(target)],
        cwd=real_project,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    assert "error" not in compiled.stdout.lower()
