#!/usr/bin/env bash
# Download LEAP's published Lean solutions for POST-HOC comparison only.
#
#   !!  Do not run this before or during a benchmark run.  !!
#
# These files are complete formal proofs of the exact theorems in this fixture.
# They land in ./reference-solutions, which is gitignored and is NOT on the Lake
# import path, so a LeanFlow run cannot pick them up from the project. The point
# is to let you diff your proof against LEAP's after you have scored a run --
# not to give the prover anything to copy.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${PROJECT_ROOT}/reference-solutions"
COMMIT="80b2527a0b4e4bfc6a8b28825fadbdcfdd6048a1"
BASE="https://raw.githubusercontent.com/google-deepmind/superhuman/${COMMIT}/leap/solutions/LEAN-IMO-Bench"

read -r -p "Download LEAP reference solutions into ${DEST}? [y/N] " reply
case "${reply}" in
  y|Y|yes|YES) ;;
  *) echo "aborted"; exit 0 ;;
esac

mkdir -p "${DEST}/Basic" "${DEST}/Advanced"
python3 - "${DEST}" "${BASE}" <<'PY'
import json, pathlib, sys, urllib.request
dest, base = pathlib.Path(sys.argv[1]), sys.argv[2]
manifest = json.loads((dest.parent / "manifest.json").read_text(encoding="utf-8"))
for problem in manifest["problems"]:
    if not problem["leap_solved"]:
        continue
    name = f'{problem["theorem"]}_solution.lean'
    url = f'{base}/{problem["split"]}/{name}'
    out = dest / problem["split"] / name
    try:
        urllib.request.urlretrieve(url, out)  # noqa: S310
        print(f"  {problem['id']} -> {out.relative_to(dest.parent)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {problem['id']} FAILED: {exc}")
PY
echo "done: ${DEST} (gitignored, off the import path)"
