#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OPENGAUSS_HOME="${OPENGAUSS_HOME:-$HOME/.opengauss}"
OPENGAUSS_BIN_DIR="${OPENGAUSS_BIN_DIR:-$HOME/.local/bin}"
OPENGAUSS_VENV_DIR="${OPENGAUSS_VENV_DIR:-$REPO_ROOT/.opengauss-venv}"
OPENGAUSS_INSTALL_PYTHON="${OPENGAUSS_INSTALL_PYTHON:-python3}"
INSTALL_MODE="editable"
RECREATE_VENV=0

usage() {
  cat <<'TXT'
OpenGauss local installer

Usage:
  ./scripts/install-internal.sh [options]

Options:
  --opengauss-home PATH  OpenGauss state directory (default: ~/.opengauss)
  --bin-dir PATH         Directory for opengauss wrappers (default: ~/.local/bin)
  --venv-dir PATH        Virtualenv path (default: ./.opengauss-venv)
  --python BIN           Python interpreter to use (default: python3)
  --recreate-venv        Remove and recreate the virtualenv
  --no-editable          Install a wheel instead of editable mode
  -h, --help             Show this help

Behavior:
  This installer creates separate OpenGauss binaries and state under ~/.opengauss.
  It does not touch ~/.gauss or existing gauss binaries.
TXT
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --opengauss-home|--gauss-home)
      OPENGAUSS_HOME="$2"
      shift 2
      ;;
    --bin-dir)
      OPENGAUSS_BIN_DIR="$2"
      shift 2
      ;;
    --venv-dir)
      OPENGAUSS_VENV_DIR="$2"
      shift 2
      ;;
    --python)
      OPENGAUSS_INSTALL_PYTHON="$2"
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

mkdir -p "$OPENGAUSS_HOME" "$OPENGAUSS_BIN_DIR"

if [[ "$RECREATE_VENV" == "1" && -e "$OPENGAUSS_VENV_DIR" ]]; then
  rm -rf "$OPENGAUSS_VENV_DIR"
fi

if [[ ! -x "$OPENGAUSS_VENV_DIR/bin/python" ]]; then
  "$OPENGAUSS_INSTALL_PYTHON" -m venv "$OPENGAUSS_VENV_DIR"
fi

# shellcheck disable=SC1090
source "$OPENGAUSS_VENV_DIR/bin/activate"

python -m pip install --upgrade pip setuptools wheel
if [[ "$INSTALL_MODE" == "editable" ]]; then
  python -m pip install -e "$REPO_ROOT"
else
  python -m pip install "$REPO_ROOT"
fi

cat > "$OPENGAUSS_BIN_DIR/opengauss" <<EOF
#!/usr/bin/env bash
: "\${OPENGAUSS_HOME:=${OPENGAUSS_HOME}}"
export OPENGAUSS_HOME
exec "${OPENGAUSS_VENV_DIR}/bin/opengauss" "\$@"
EOF

cat > "$OPENGAUSS_BIN_DIR/opengauss-agent" <<EOF
#!/usr/bin/env bash
: "\${OPENGAUSS_HOME:=${OPENGAUSS_HOME}}"
export OPENGAUSS_HOME
exec "${OPENGAUSS_VENV_DIR}/bin/opengauss-agent" "\$@"
EOF

cat > "$OPENGAUSS_BIN_DIR/opengauss-acp" <<EOF
#!/usr/bin/env bash
: "\${OPENGAUSS_HOME:=${OPENGAUSS_HOME}}"
export OPENGAUSS_HOME
exec "${OPENGAUSS_VENV_DIR}/bin/opengauss-acp" "\$@"
EOF

chmod +x \
  "$OPENGAUSS_BIN_DIR/opengauss" \
  "$OPENGAUSS_BIN_DIR/opengauss-agent" \
  "$OPENGAUSS_BIN_DIR/opengauss-acp"

cat > "${OPENGAUSS_HOME}/install-root" <<EOF
repo_root=${REPO_ROOT}
venv_dir=${OPENGAUSS_VENV_DIR}
bin_dir=${OPENGAUSS_BIN_DIR}
installed_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
EOF

printf 'OpenGauss installed.\n'
printf '  repo: %s\n' "$REPO_ROOT"
printf '  home: %s\n' "$OPENGAUSS_HOME"
printf '  venv: %s\n' "$OPENGAUSS_VENV_DIR"
printf '  bin : %s\n' "$OPENGAUSS_BIN_DIR"
printf '\n'
printf 'Add %s to PATH if needed, then run:\n' "$OPENGAUSS_BIN_DIR"
printf '  opengauss --help\n'
