#!/usr/bin/env bash
# Run LeanFlow against exactly one Lean-IMO-Bench problem.
#
# Each problem is a standalone module with a single theorem and a single sorry,
# so a run is fully independent of the other 59 -- nothing is shared but the
# Lake build cache.
#
#   ./scripts/run_problem.sh PB-Basic-001
#   ./scripts/run_problem.sh PBAdvanced012 --profile lean-imo-bench --provider openai-codex
#   ./scripts/run_problem.sh PB-Basic-001 --print      # resolve only, run nothing
#   ./scripts/run_problem.sh PB-Basic-001 --dry-run    # let LeanFlow print its launch plan
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <PROBLEM_ID|THEOREM> [--profile NAME] [--provider NAME] [--run-id ID] [--print] [--dry-run]" >&2
  exit 2
fi

PROBLEM="$1"; shift
PROFILE="lean-imo-bench"
PROVIDER=""
RUN_ID=""
PRINT_ONLY=0
DRY_RUN=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --profile)  PROFILE="$2"; shift 2 ;;
    --provider) PROVIDER="$2"; shift 2 ;;
    --run-id)   RUN_ID="$2"; shift 2 ;;
    --print)    PRINT_ONLY=1; shift ;;
    --dry-run)  DRY_RUN=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

PROFILE_PATH="flag-profiles/${PROFILE}.json"
[ -f "${PROFILE_PATH}" ] || { echo "no such profile: ${PROFILE_PATH}" >&2; exit 1; }

# Resolve the problem out of the frozen manifest.
eval "$(python3 - "${PROBLEM}" <<'PY'
import json, sys, pathlib
key = sys.argv[1]
manifest = json.loads(pathlib.Path("manifest.json").read_text(encoding="utf-8"))
matches = [p for p in manifest["problems"] if key in (p["id"], p["theorem"], p["module"])]
if len(matches) != 1:
    print(f'echo "no unique problem matching {key!r}" >&2; exit 1')
    sys.exit(0)
p = matches[0]
for field in ("id", "theorem", "module", "file", "split", "category", "level"):
    print(f'PROBLEM_{field.upper()}={json.dumps(str(p[field]))}')
print(f'PROBLEM_LEAP_SOLVED={json.dumps("yes" if p["leap_solved"] else "no")}')
PY
)"

[ -z "${RUN_ID}" ] && RUN_ID="lean-imo-bench-$(echo "${PROBLEM_ID}" | tr 'A-Z' 'a-z')-$(date -u +%Y%m%d-%H%M%S)"
RESULT_DIR="results/${RUN_ID}"

# Export the profile's overrides into this run's environment.
while IFS='=' read -r key value; do
  [ -n "${key}" ] && export "${key}=${value}"
done < <(python3 -c "
import json,pathlib
prof=json.loads(pathlib.Path('${PROFILE_PATH}').read_text(encoding='utf-8'))
for k,v in prof['overrides'].items(): print(f'{k}={v}')
")
export LEANFLOW_WORKFLOW_RUN_ID="${RUN_ID}"

cat <<SUMMARY
Lean-IMO-Bench -- single problem
  problem     : ${PROBLEM_ID}  (${PROBLEM_SPLIT} / ${PROBLEM_CATEGORY} / ${PROBLEM_LEVEL})
  theorem     : ${PROBLEM_THEOREM}
  file        : ${PROBLEM_FILE}
  module      : ${PROBLEM_MODULE}
  LEAP solved : ${PROBLEM_LEAP_SOLVED}
  profile     : ${PROFILE_PATH}
  run id      : ${RUN_ID}
  results     : ${RESULT_DIR}
SUMMARY

if [ "${PRINT_ONLY}" -eq 1 ]; then
  echo
  echo "would run: leanflow workflow prove ${PROBLEM_FILE}"
  echo "resolved overrides:"
  env | grep -E '^LEANFLOW_' | sort | sed 's/^/  /'
  exit 0
fi

mkdir -p "${RESULT_DIR}"
python3 - "${PROBLEM_ID}" "${RUN_ID}" "${PROFILE_PATH}" "${PROVIDER}" <<'PY' > "${RESULT_DIR}/RUN_CONFIG.json"
import json, os, sys, datetime, pathlib
problem_id, run_id, profile_path, provider = sys.argv[1:5]
manifest = json.loads(pathlib.Path("manifest.json").read_text(encoding="utf-8"))
problem = next(p for p in manifest["problems"] if p["id"] == problem_id)
profile = json.loads(pathlib.Path(profile_path).read_text(encoding="utf-8"))
print(json.dumps({
    "benchmark": "Lean-IMO-Bench",
    "upstream": manifest["upstream"],
    "problem": problem,
    "run_id": run_id,
    "profile": profile["name"],
    "profile_path": profile_path,
    "provider": provider or os.environ.get("LEANFLOW_PROVIDER", ""),
    "project": ".",
    "target_file": problem["file"],
    "target": problem["theorem"],
    "overrides": profile["overrides"],
    "launched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}, indent=2))
PY

CMD=(leanflow workflow prove "${PROBLEM_FILE}")
[ -n "${PROVIDER}" ] && CMD=(leanflow workflow --provider "${PROVIDER}" prove "${PROBLEM_FILE}")
[ "${DRY_RUN}" -eq 1 ] && CMD+=(--dry-run)

echo
echo "+ ${CMD[*]}"
"${CMD[@]}"
