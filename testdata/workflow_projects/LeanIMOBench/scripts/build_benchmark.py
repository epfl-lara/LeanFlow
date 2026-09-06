#!/usr/bin/env python3
"""Regenerate the Lean-IMO-Bench fixture from the pinned upstream CSV.

The benchmark statements are vendored verbatim from IMO-LeanProofBench
(``imobench/lean_proof_bench_v2.csv`` in google-deepmind/superhuman). This
script is the only supported way to refresh them: it rewrites every problem
module and ``manifest.json`` so the fixture can always be traced back to an
upstream revision.

Statement fidelity is the point of this file. Each ``.lean`` module is the
upstream ``Lean Statement`` cell byte for byte -- no added headers, no
reformatting -- so LeanFlow results stay comparable with published numbers.
Problem metadata (category, level, competition source) lives in
``manifest.json`` instead of in the modules, which also keeps competition
provenance out of the prover's context window.

Usage::

    python3 scripts/build_benchmark.py --csv /path/to/lean_proof_bench_v2.csv
    python3 scripts/build_benchmark.py --download   # fetch the pinned revision
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Upstream revision this fixture is pinned to.
UPSTREAM_REPO = "google-deepmind/superhuman"
UPSTREAM_COMMIT = "80b2527a0b4e4bfc6a8b28825fadbdcfdd6048a1"
UPSTREAM_CSV_PATH = "imobench/lean_proof_bench_v2.csv"
UPSTREAM_CSV_SHA256 = "ffc48aecb1290a90582c1b0634f40ea38b86c15891ce081011c5c55905e1c2f3"
UPSTREAM_CSV_URL = (
    f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/{UPSTREAM_COMMIT}/{UPSTREAM_CSV_PATH}"
)

#: Problems LEAP reports as solved, read off its published solution files.
#: Used only to slice our results against the paper's baseline; the fixture
#: never vendors the solutions themselves.
LEAP_SOLVED = {
    "Basic": {
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        27,
    },
    "Advanced": {1, 6, 7, 8, 11, 12, 13, 14, 17, 19, 20, 24, 25, 26, 28, 29, 30},
}

#: The toolchain this fixture is pinned to, and the result of building every
#: statement against it. Upstream verified the statements at 4.27.0; we run
#: 4.33.1 so this project shares one on-disk Mathlib with BeckFialaResearch and
#: SpencerResearch, and the move was checked rather than assumed.
LOCAL_TOOLCHAIN = "leanprover/lean4:v4.33.1"
LOCAL_MATHLIB_REV = "0df444a360eaa60ab8c11dca51a86af692955474"
VERIFICATION = {
    "verified_on": "2026-09-06",
    "command": "lake build",
    "result": "all 60 statements elaborate; 0 errors",
    "sorry_warnings": 60,
    "deprecation_warnings": [
        "PB-Basic-012",
        "PB-Advanced-018",
        "PB-Advanced-023",
    ],
    "deprecation_note": (
        "Those three use List.Chain', deprecated in favour of List.IsChain at this "
        "Mathlib. They still elaborate with the same meaning, but they are the three "
        "statements most likely to break on a future Mathlib bump -- recheck them first."
    ),
}

EXPECTED_TOTAL = 60
EXPECTED_PER_SPLIT = 30


def load_rows(csv_path: Path) -> list[dict[str, str]]:
    """Read the upstream CSV and check it against the pinned digest."""
    raw = csv_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != UPSTREAM_CSV_SHA256:
        print(
            f"warning: {csv_path} sha256 {digest} does not match the pinned "
            f"{UPSTREAM_CSV_SHA256}; the fixture will drift from the pin.",
            file=sys.stderr,
        )
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def parse_id(problem_id: str) -> tuple[str, int]:
    """Split ``PB-Basic-001`` into its ``("Basic", 1)`` split and index."""
    match = re.fullmatch(r"PB-(Basic|Advanced)-(\d+)", problem_id)
    if match is None:
        raise ValueError(f"unrecognised problem id: {problem_id!r}")
    return match.group(1), int(match.group(2))


def theorem_name(statement: str) -> str:
    """Return the single theorem declared by an upstream statement cell."""
    names = re.findall(r"^\s*(?:theorem|lemma)\s+([A-Za-z0-9_'.]+)", statement, re.M)
    if len(names) != 1:
        raise ValueError(f"expected exactly one theorem, found {names}")
    return names[0]


def build(csv_path: Path) -> dict[str, object]:
    rows = load_rows(csv_path)
    if len(rows) != EXPECTED_TOTAL:
        raise SystemExit(f"expected {EXPECTED_TOTAL} problems, got {len(rows)}")

    problems: list[dict[str, object]] = []
    for row in rows:
        problem_id = row["Problem ID"].strip()
        split, index = parse_id(problem_id)
        statement = row["Lean Statement"]
        if statement.count("sorry") != 1:
            raise SystemExit(f"{problem_id}: expected exactly one sorry")
        name = theorem_name(statement)

        module = f"LeanIMOBench.{split}.{name}"
        rel_path = Path("LeanIMOBench") / split / f"{name}.lean"
        text = statement if statement.endswith("\n") else statement + "\n"
        target = PROJECT_ROOT / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

        problems.append(
            {
                "id": problem_id,
                "split": split,
                "index": index,
                "theorem": name,
                "module": module,
                "file": rel_path.as_posix(),
                "category": row["Category"].strip(),
                "level": row["Level"].strip(),
                "source": row["Source"].strip(),
                "leap_solved": index in LEAP_SOLVED[split],
                "statement_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )

    for split in ("Basic", "Advanced"):
        count = sum(1 for p in problems if p["split"] == split)
        if count != EXPECTED_PER_SPLIT:
            raise SystemExit(f"{split}: expected {EXPECTED_PER_SPLIT} problems, got {count}")

    manifest = {
        "version": 1,
        "name": "Lean-IMO-Bench",
        "description": (
            "60 IMO-style problems formalized in Lean 4, vendored verbatim from "
            "IMO-LeanProofBench. Each module is standalone (imports Mathlib only) "
            "and carries exactly one theorem with one sorry."
        ),
        "toolchain": {
            "lean": LOCAL_TOOLCHAIN,
            "mathlib_rev": LOCAL_MATHLIB_REV,
            "shared_with": ["BeckFialaResearch", "SpencerResearch"],
            "differs_from_upstream": "upstream verified at Lean/Mathlib 4.27.0",
            "verification": VERIFICATION,
        },
        "upstream": {
            "repo": f"https://github.com/{UPSTREAM_REPO}",
            "commit": UPSTREAM_COMMIT,
            "path": UPSTREAM_CSV_PATH,
            "csv_sha256": UPSTREAM_CSV_SHA256,
            "verified_against": "Lean and Mathlib 4.27.0",
            "license": "CC-BY-4.0 (benchmark materials)",
            "papers": [
                "https://arxiv.org/abs/2606.03303",
                "https://imobench.github.io",
            ],
        },
        "baseline": {
            "system": "LEAP (one-shot formal solve rate, arXiv:2606.03303)",
            "overall": {"solved": 42, "total": 60, "rate": 0.70},
            "Basic": {"solved": 25, "total": 30, "rate": 0.833},
            "Advanced": {"solved": 17, "total": 30, "rate": 0.567},
            "note": (
                "Read off LEAP's published solution files; leap_solved marks which "
                "problems those cover. Reference solutions are deliberately not "
                "vendored -- see README 'Contamination'."
            ),
        },
        "problems": problems,
    }
    (PROJECT_ROOT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=None, help="Local upstream CSV to read")
    parser.add_argument(
        "--download", action="store_true", help="Fetch the pinned CSV revision over the network"
    )
    args = parser.parse_args()

    csv_path = args.csv
    if args.download or csv_path is None:
        csv_path = PROJECT_ROOT / ".upstream" / "lean_proof_bench_v2.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        if args.download or not csv_path.is_file():
            print(f"downloading {UPSTREAM_CSV_URL}")
            urllib.request.urlretrieve(UPSTREAM_CSV_URL, csv_path)  # noqa: S310

    manifest = build(csv_path)
    problems = manifest["problems"]
    assert isinstance(problems, list)
    print(f"wrote {len(problems)} problem modules and manifest.json")


if __name__ == "__main__":
    main()
