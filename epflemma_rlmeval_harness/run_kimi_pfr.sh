#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EPFLEMMA_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$SCRIPT_DIR"
mkdir -p logs

# If --resume or --recheck, append to the most recent log; otherwise create a new timestamped one.
if printf '%s\n' "$@" | grep -qE '^--(resume|recheck)$'; then
  LOGFILE="$(find logs -maxdepth 1 -name 'kimi_pfr_*.log' 2>/dev/null | sort | tail -1)"
  if [[ -z "$LOGFILE" ]]; then
    LOGFILE="logs/kimi_pfr_$(date +%Y%m%d_%H%M%S).log"
  fi
else
  LOGFILE="logs/kimi_pfr_$(date +%Y%m%d_%H%M%S).log"
fi

echo "Logging to $LOGFILE"
echo "PID: $$"

{
  echo ""
  echo "========================================"
  echo "=== RUN STARTED $(date '+%Y-%m-%d %H:%M:%S') ==="
  echo "=== PID: $$ | ARGS: $* ==="
  echo "========================================"
} | tee -a "$LOGFILE"

KEY_VALUE="$(tr -d '\n' < "$EPFLEMMA_ROOT/.rcpkey")"

EPFLEMMA_OPENAI_API_KEY="$KEY_VALUE" OPENAI_API_KEY="$KEY_VALUE" \
  "$EPFLEMMA_ROOT/.venv/bin/python" -u eval_epflemma_full_autoformalize.py \
    --benchmark-config "$EPFLEMMA_ROOT/RLMEval/configs/benchmark/rlm25.yaml" \
    --model-config configs/model_kimi_rcp.yaml \
    --repo PFR \
    "$@" 2>&1 | tee -a "$LOGFILE"
