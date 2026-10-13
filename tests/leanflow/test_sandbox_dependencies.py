"""Verify the sandbox's pre-install source context supplies its coordinated backend."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from leanflow_cli.runtime.install_companion import LEAN_PROBE_COMMIT


def test_dependency_context_bootstraps_without_site_packages(tmp_path: Path) -> None:
    """Import the actual staged resolver before LeanFlow or third-party packages exist."""
    repository = Path(__file__).resolve().parents[2]
    recipe = (repository / "containers/leanflow-sandbox.Containerfile").read_text()
    dependency_recipe = recipe.split("COPY . /opt/leanflow", 1)[0]
    staged = tmp_path / "opt/leanflow"
    for instruction in dependency_recipe.replace("\\\n", " ").splitlines():
        if not instruction.startswith("COPY "):
            continue
        *sources, target = shlex.split(instruction)[1:]
        destination = tmp_path / target.lstrip("/")
        destination.mkdir(parents=True, exist_ok=True)
        for name in sources:
            shutil.copy2(repository / name, destination / Path(name).name)
    companion = staged.parent / "LeanProbe"
    (companion / "src/lean_probe").mkdir(parents=True)
    (companion / "pyproject.toml").write_text(
        '[project]\nname = "lean-probe"\nversion = "0.4.1.dev0"\n'
    )
    (companion / "src/lean_probe/__init__.py").write_text("API_REVISION = 1\n")
    code = (
        "import json,sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
        "from leanflow_cli.runtime.install_companion import LEAN_PROBE_COMMIT;"
        "from leanflow_cli.runtime.install_inputs import resolve_lean_probe_source;"
        "print(json.dumps({'commit':LEAN_PROBE_COMMIT,"
        "'source':str(resolve_lean_probe_source(Path(sys.argv[1])))}))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(staged)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == {
        "commit": LEAN_PROBE_COMMIT,
        "source": str(companion),
    }
    assert "from leanflow_cli.runtime.install_companion import fetch_pinned_lean_probe" in recipe
    assert 'pip install -r /tmp/leanflow-requirements.txt "$lean_probe_source"' in recipe


def test_context_excludes_managed_state_and_native_credentials() -> None:
    """Exclude entire state authorities rather than selected cache subdirectories."""
    repository = Path(__file__).resolve().parents[2]
    patterns = (repository / ".dockerignore").read_text().splitlines()
    assert {
        "**/.leanflow",
        "**/.codex",
        "**/.claude",
        "**/.claude.json",
        "**/.ssh",
        "**/.aws",
        "**/.config",
        "**/.cache",
        "**/*.env",
    }.issubset(patterns)
