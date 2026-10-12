#!/usr/bin/env bash
# Prepare the benchmark's Lake environment, reusing a Mathlib that is already
# built somewhere else in testdata/workflow_projects instead of building or
# downloading a second copy.
#
# A full Mathlib build tree is ~7-8 GB. On APFS (macOS default) `cp -c` clones
# it with copy-on-write: the clone shares blocks with the original, so it costs
# effectively no disk, is created in seconds, and still diverges safely if
# either side rebuilds. That gives one Mathlib on disk serving several projects
# without the fragility of a symlinked store that a `lake update` in one project
# could pull out from under a run in another.
#
#   ./scripts/setup_project.sh          # clone if possible, else build
#   ./scripts/setup_project.sh --build  # skip donor lookup, resolve normally
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIBLINGS="$(cd "${PROJECT_ROOT}/.." && pwd)"
cd "${PROJECT_ROOT}"

FORCE_BUILD=0
[ "${1:-}" = "--build" ] && FORCE_BUILD=1

resolve_without_donor() {
  # Without a donor, resolve the pinned dependencies and then pull Mathlib's
  # prebuilt olean cache. Skipping the cache means building Mathlib from
  # source, which is hours instead of minutes -- always try the cache first.
  lake update
  if ! lake exe cache get; then
    echo "WARNING: could not fetch the Mathlib olean cache." >&2
    echo "The next build will compile Mathlib from source and take hours." >&2
  fi
}

WANT_TOOLCHAIN="$(tr -d '[:space:]' < lean-toolchain)"
WANT_REV="$(sed -n '/name = "mathlib"/,/^$/p' lakefile.toml | sed -n 's/^rev = "\(.*\)"/\1/p' | head -1)"
echo "want: ${WANT_TOOLCHAIN}  mathlib ${WANT_REV:0:10}"

if [ -d .lake/packages/mathlib ]; then
  echo "packages already present; nothing to clone"
elif [ "${FORCE_BUILD}" -eq 0 ]; then
  DONOR=""
  for candidate in "${SIBLINGS}"/*/; do
    [ "$(basename "${candidate}")" = "$(basename "${PROJECT_ROOT}")" ] && continue
    mathlib="${candidate}.lake/packages/mathlib"
    [ -d "${mathlib}/.lake/build" ] || continue
    [ "$(git -C "${mathlib}" rev-parse HEAD 2>/dev/null)" = "${WANT_REV}" ] || continue
    [ "$(tr -d '[:space:]' < "${candidate}lean-toolchain" 2>/dev/null)" = "${WANT_TOOLCHAIN}" ] || continue
    DONOR="${candidate}"
    break
  done

  if [ -n "${DONOR}" ]; then
    echo "cloning built packages from $(basename "${DONOR}") (copy-on-write)"
    mkdir -p .lake
    if cp -c -R "${DONOR}.lake/packages" .lake/packages 2>/dev/null; then
      echo "cloned with APFS copy-on-write: no meaningful disk cost"
    else
      echo "clonefile unavailable on this filesystem; falling back to a full copy"
      cp -R "${DONOR}.lake/packages" .lake/packages
    fi
    [ -f lake-manifest.json ] || cp "${DONOR}lake-manifest.json" lake-manifest.json
  else
    echo "no sibling project with a built Mathlib at ${WANT_REV:0:10}; resolving from upstream"
    resolve_without_donor
  fi
else
  resolve_without_donor
fi

echo
echo "building all 60 statements (the statement-integrity gate)"
lake build
echo
echo "ready. Next: leanflow project init, then ./scripts/run_problem.sh <PROBLEM_ID>"
