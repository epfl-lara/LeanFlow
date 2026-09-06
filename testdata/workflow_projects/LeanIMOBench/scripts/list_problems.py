#!/usr/bin/env python3
"""List and slice the Lean-IMO-Bench inventory.

python3 scripts/list_problems.py                          # all 60
python3 scripts/list_problems.py --split Advanced --level IMO-hard
python3 scripts/list_problems.py --category Geometry --unsolved-by-leap
python3 scripts/list_problems.py --split Basic --ids       # ids only, for xargs
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "manifest.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["Basic", "Advanced"])
    parser.add_argument("--category")
    parser.add_argument("--level")
    parser.add_argument("--solved-by-leap", action="store_true")
    parser.add_argument("--unsolved-by-leap", action="store_true")
    parser.add_argument("--ids", action="store_true", help="Print bare ids, one per line")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    problems = manifest["problems"]
    if args.split:
        problems = [p for p in problems if p["split"] == args.split]
    if args.category:
        problems = [p for p in problems if p["category"].lower() == args.category.lower()]
    if args.level:
        problems = [p for p in problems if p["level"].lower() == args.level.lower()]
    if args.solved_by_leap:
        problems = [p for p in problems if p["leap_solved"]]
    if args.unsolved_by_leap:
        problems = [p for p in problems if not p["leap_solved"]]

    if args.ids:
        for problem in problems:
            print(problem["id"])
        return

    width = max((len(p["id"]) for p in problems), default=0)
    for problem in problems:
        flag = "LEAP:solved  " if problem["leap_solved"] else "LEAP:unsolved"
        print(
            f"{problem['id']:<{width}}  {problem['split']:<8} "
            f"{problem['category']:<14} {problem['level']:<11} {flag}  {problem['file']}"
        )

    print(f"\n{len(problems)} problem(s)")
    if len(problems) > 1:
        for field in ("split", "category", "level"):
            counts = Counter(p[field] for p in problems)
            print(f"  by {field}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        solved = sum(1 for p in problems if p["leap_solved"])
        print(f"  LEAP baseline: {solved}/{len(problems)} solved")


if __name__ == "__main__":
    main()
