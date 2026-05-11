#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

EPFLEMMA_HOME="${EPFLEMMA_HOME:-$HOME/.epflemma}"
EPFLEMMA_BIN_DIR="${EPFLEMMA_BIN_DIR:-${OPENGAUSS_BIN_DIR:-$HOME/.local/bin}}"
EPFLEMMA_VENV_DIR="${EPFLEMMA_VENV_DIR:-${OPENGAUSS_VENV_DIR:-$REPO_ROOT/.epflemma-venv}}"
EPFLEMMA_INSTALL_PYTHON="${EPFLEMMA_INSTALL_PYTHON:-${OPENGAUSS_INSTALL_PYTHON:-python3}}"
EPFLEMMA_FETCH_LEANEXPLORE_DATA="${EPFLEMMA_FETCH_LEANEXPLORE_DATA:-1}"
INSTALL_MODE="editable"
RECREATE_VENV=0
STEP=0
TOTAL_STEPS=10

banner() {
  printf '\n'
  printf '============================================================\n'
  printf ' EPFLemma Installer\n'
  printf ' Lean-first automation kernel setup\n'
  printf '============================================================\n'
  printf '\n'
}

step() {
  STEP=$((STEP + 1))
  printf '\n[%d/%d] %s\n' "$STEP" "$TOTAL_STEPS" "$1"
}

ok() {
  printf '  [ok] %s\n' "$1"
}

warn() {
  printf '  [warn] %s\n' "$1"
}

usage() {
  cat <<'TXT'
EPFLemma local installer

Usage:
  ./scripts/install-internal.sh [options]

Options:
  --epflemma-home PATH  EPFLemma state directory (default: ~/.epflemma)
  --bin-dir PATH         Directory for EPFLemma wrappers (default: ~/.local/bin)
  --venv-dir PATH        Virtualenv path (default: ./.epflemma-venv)
  --python BIN           Python interpreter to use (default: python3)
  --recreate-venv        Remove and recreate the virtualenv
  --skip-leanexplore-data
                        Skip local LeanExplore index fetch
  --no-editable          Install a wheel instead of editable mode
  -h, --help             Show this help

Behavior:
  This installer creates separate EPFLemma binaries and state under ~/.epflemma.
  It does not touch ~/.gauss or existing gauss binaries.
TXT
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --epflemma-home|--gauss-home)
      EPFLEMMA_HOME="$2"
      shift 2
      ;;
    --bin-dir)
      EPFLEMMA_BIN_DIR="$2"
      shift 2
      ;;
    --venv-dir)
      EPFLEMMA_VENV_DIR="$2"
      shift 2
      ;;
    --python)
      EPFLEMMA_INSTALL_PYTHON="$2"
      shift 2
      ;;
    --recreate-venv)
      RECREATE_VENV=1
      shift
      ;;
    --skip-leanexplore-data)
      EPFLEMMA_FETCH_LEANEXPLORE_DATA=0
      shift
      ;;
    --no-editable)
      INSTALL_MODE="wheel"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

banner
step "Preparing install directories"
mkdir -p "$EPFLEMMA_HOME" "$EPFLEMMA_BIN_DIR"
ok "home: $EPFLEMMA_HOME"
ok "bin : $EPFLEMMA_BIN_DIR"

if [[ "$RECREATE_VENV" == "1" && -e "$EPFLEMMA_VENV_DIR" ]]; then
  warn "recreating virtualenv: $EPFLEMMA_VENV_DIR"
  rm -rf "$EPFLEMMA_VENV_DIR"
fi

step "Preparing Python environment"
if [[ ! -x "$EPFLEMMA_VENV_DIR/bin/python" ]]; then
  "$EPFLEMMA_INSTALL_PYTHON" -m venv "$EPFLEMMA_VENV_DIR"
  ok "created virtualenv: $EPFLEMMA_VENV_DIR"
else
  ok "using existing virtualenv: $EPFLEMMA_VENV_DIR"
fi

# shellcheck disable=SC1090
source "$EPFLEMMA_VENV_DIR/bin/activate"

step "Installing EPFLemma package"
python -m pip install --quiet --quiet --upgrade pip "setuptools<82" wheel
if [[ "$INSTALL_MODE" == "editable" ]]; then
  python -m pip install --quiet --quiet -e "$REPO_ROOT[mcp,lean-explore,web]"
  ok "installed editable package"
else
  python -m pip install --quiet --quiet "$REPO_ROOT[mcp,lean-explore,web]"
  ok "installed package wheel"
fi

step "Fetching local LeanExplore data"
if [[ "$EPFLEMMA_FETCH_LEANEXPLORE_DATA" == "1" ]]; then
  if "$EPFLEMMA_VENV_DIR/bin/lean-explore" data fetch; then
    ok "LeanExplore local data ready"
  else
    warn "LeanExplore data fetch failed; semantic search will fall back to hosted API/MCP/rg until you run lean-explore data fetch"
  fi
else
  warn "skipped LeanExplore data fetch"
fi

# Create/backfill user-visible config before any bootstrap step that may need
# provider/env discovery. This makes ~/.epflemma/config.yaml and ~/.epflemma/.env
# explicit installation artifacts instead of hidden first-run side effects.
step "Creating EPFLemma config"
EPFLEMMA_HOME="$EPFLEMMA_HOME" "$EPFLEMMA_VENV_DIR/bin/python" <<'PY'
from collections.abc import Mapping

import yaml

from epflemma_cli.config import (
    DEFAULT_CONFIG,
    _deep_merge,
    ensure_epflemma_home,
    get_config_path,
    save_config,
)

ensure_epflemma_home()
path = get_config_path()
try:
    current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
except Exception:
    current = {}
if not isinstance(current, Mapping):
    current = {}
merged = _deep_merge(DEFAULT_CONFIG, current)
if merged != current:
    save_config(merged)
PY
ok "config: $EPFLEMMA_HOME/config.yaml"

step "Installing managed Lean MCP backends and power modes"
EPFLEMMA_HOME="$EPFLEMMA_HOME" "$EPFLEMMA_VENV_DIR/bin/epflemma" mcp bootstrap lean
ok "managed MCP bootstrap complete"

step "Writing command wrappers"
cat > "$EPFLEMMA_BIN_DIR/epflemma" <<EOF
#!/usr/bin/env bash
: "\${EPFLEMMA_HOME:=${EPFLEMMA_HOME}}"
export OPENGAUSS_HOME="\${OPENGAUSS_HOME:-\${EPFLEMMA_HOME}}"
export EPFLEMMA_HOME
exec "${EPFLEMMA_VENV_DIR}/bin/epflemma" "\$@"
EOF

cat > "$EPFLEMMA_BIN_DIR/epflemma-agent" <<EOF
#!/usr/bin/env bash
: "\${EPFLEMMA_HOME:=${EPFLEMMA_HOME}}"
export OPENGAUSS_HOME="\${OPENGAUSS_HOME:-\${EPFLEMMA_HOME}}"
export EPFLEMMA_HOME
exec "${EPFLEMMA_VENV_DIR}/bin/epflemma-agent" "\$@"
EOF

chmod +x \
  "$EPFLEMMA_BIN_DIR/epflemma" \
  "$EPFLEMMA_BIN_DIR/epflemma-agent"
ok "wrapper: $EPFLEMMA_BIN_DIR/epflemma"
ok "wrapper: $EPFLEMMA_BIN_DIR/epflemma-agent"

step "Cleaning legacy wrapper names"
rm -f "$EPFLEMMA_BIN_DIR/epflemma-acp" "$EPFLEMMA_BIN_DIR/opengauss" "$EPFLEMMA_BIN_DIR/opengauss-agent"
ok "legacy wrappers removed if present"

step "Recording install metadata"
cat > "${EPFLEMMA_HOME}/install-root" <<EOF
repo_root=${REPO_ROOT}
venv_dir=${EPFLEMMA_VENV_DIR}
bin_dir=${EPFLEMMA_BIN_DIR}
installed_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
EOF
ok "metadata: ${EPFLEMMA_HOME}/install-root"

step "Final smoke check"
EPFLEMMA_HOME="$EPFLEMMA_HOME" "$EPFLEMMA_VENV_DIR/bin/epflemma" --help >/dev/null
ok "epflemma --help"

printf '\nEPFLemma installed.\n'
printf '  repo: %s\n' "$REPO_ROOT"
printf '  home: %s\n' "$EPFLEMMA_HOME"
printf '  config: %s/config.yaml\n' "$EPFLEMMA_HOME"
printf '  env : %s/.env\n' "$EPFLEMMA_HOME"
printf '  venv: %s\n' "$EPFLEMMA_VENV_DIR"
printf '  bin : %s\n' "$EPFLEMMA_BIN_DIR"
printf '  mcp : managed Lean MCP backends installed under %s/mcp\n' "$EPFLEMMA_HOME"
printf '  power modes: local Loogle/REPL configured when supported; public remote search fallbacks remain enabled\n'
printf '\n'
printf 'Add %s to PATH if needed, then run:\n' "$EPFLEMMA_BIN_DIR"
printf '  epflemma --help\n'
printf '  epflemma mcp status\n'
printf '  epflemma project init   # inside a Lean repo, to build REPL acceleration\n'
