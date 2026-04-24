#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

EPFLEMMA_HOME="${EPFLEMMA_HOME:-$HOME/.epflemma}"
EPFLEMMA_BIN_DIR="${EPFLEMMA_BIN_DIR:-${OPENGAUSS_BIN_DIR:-$HOME/.local/bin}}"
EPFLEMMA_VENV_DIR="${EPFLEMMA_VENV_DIR:-${OPENGAUSS_VENV_DIR:-$REPO_ROOT/.epflemma-venv}}"
EPFLEMMA_INSTALL_PYTHON="${EPFLEMMA_INSTALL_PYTHON:-${OPENGAUSS_INSTALL_PYTHON:-python3}}"
INSTALL_MODE="editable"
RECREATE_VENV=0

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

mkdir -p "$EPFLEMMA_HOME" "$EPFLEMMA_BIN_DIR"

if [[ "$RECREATE_VENV" == "1" && -e "$EPFLEMMA_VENV_DIR" ]]; then
  rm -rf "$EPFLEMMA_VENV_DIR"
fi

if [[ ! -x "$EPFLEMMA_VENV_DIR/bin/python" ]]; then
  "$EPFLEMMA_INSTALL_PYTHON" -m venv "$EPFLEMMA_VENV_DIR"
fi

# shellcheck disable=SC1090
source "$EPFLEMMA_VENV_DIR/bin/activate"

python -m pip install --upgrade pip setuptools wheel
if [[ "$INSTALL_MODE" == "editable" ]]; then
  python -m pip install -e "$REPO_ROOT[mcp]"
else
  python -m pip install "$REPO_ROOT[mcp]"
fi

# Create/backfill user-visible config before any bootstrap step that may need
# provider/env discovery. This makes ~/.epflemma/config.yaml and ~/.epflemma/.env
# explicit installation artifacts instead of hidden first-run side effects.
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

EPFLEMMA_HOME="$EPFLEMMA_HOME" "$EPFLEMMA_VENV_DIR/bin/epflemma" mcp bootstrap lean >/dev/null

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

rm -f "$EPFLEMMA_BIN_DIR/epflemma-acp" "$EPFLEMMA_BIN_DIR/opengauss" "$EPFLEMMA_BIN_DIR/opengauss-agent"

cat > "${EPFLEMMA_HOME}/install-root" <<EOF
repo_root=${REPO_ROOT}
venv_dir=${EPFLEMMA_VENV_DIR}
bin_dir=${EPFLEMMA_BIN_DIR}
installed_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
EOF

printf 'EPFLemma installed.\n'
printf '  repo: %s\n' "$REPO_ROOT"
printf '  home: %s\n' "$EPFLEMMA_HOME"
printf '  config: %s/config.yaml\n' "$EPFLEMMA_HOME"
printf '  env : %s/.env\n' "$EPFLEMMA_HOME"
printf '  venv: %s\n' "$EPFLEMMA_VENV_DIR"
printf '  bin : %s\n' "$EPFLEMMA_BIN_DIR"
printf '  mcp : managed lean-lsp + lean-proof-auto installed under %s/mcp\n' "$EPFLEMMA_HOME"
printf '\n'
printf 'Add %s to PATH if needed, then run:\n' "$EPFLEMMA_BIN_DIR"
printf '  epflemma --help\n'
