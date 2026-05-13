#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

EPFLEMMA_HOME="${EPFLEMMA_HOME:-$HOME/.epflemma}"
EPFLEMMA_BIN_DIR="${EPFLEMMA_BIN_DIR:-${OPENGAUSS_BIN_DIR:-$HOME/.local/bin}}"
EPFLEMMA_VENV_DIR="${EPFLEMMA_VENV_DIR:-${OPENGAUSS_VENV_DIR:-$REPO_ROOT/.epflemma-venv}}"
EPFLEMMA_INSTALL_PYTHON="${EPFLEMMA_INSTALL_PYTHON:-${OPENGAUSS_INSTALL_PYTHON:-python3}}"
EPFLEMMA_FETCH_LEANEXPLORE_DATA="${EPFLEMMA_FETCH_LEANEXPLORE_DATA:-1}"
EPFLEMMA_INSTALL_OS_TOOLS="${EPFLEMMA_INSTALL_OS_TOOLS:-1}"
INSTALL_MODE="editable"
RECREATE_VENV=0
STEP=0
TOTAL_STEPS=11
REQUIRED_EXTERNAL_TOOLS=(rg pdftotext pdfinfo pdfimages)

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

tool_version_ok() {
  local binary="$1"
  local resolved
  resolved="$(command -v "$binary" 2>/dev/null || true)"
  [[ -n "$resolved" && -x "$resolved" ]] || return 1
  case "$binary" in
    rg)
      "$resolved" --version >/dev/null 2>&1
      ;;
    pdftotext|pdfinfo|pdfimages)
      "$resolved" -v >/dev/null 2>&1
      ;;
    *)
      "$resolved" --version >/dev/null 2>&1
      ;;
  esac
}

external_tools_ready() {
  local binary
  for binary in "${REQUIRED_EXTERNAL_TOOLS[@]}"; do
    tool_version_ok "$binary" || return 1
  done
}

missing_external_tools_text() {
  local binary
  local missing=()
  for binary in "${REQUIRED_EXTERNAL_TOOLS[@]}"; do
    if ! tool_version_ok "$binary"; then
      missing+=("$binary")
    fi
  done
  printf '%s' "${missing[*]}"
}

link_external_tool_candidates() {
  local binary
  local candidate
  local resolved
  for binary in "${REQUIRED_EXTERNAL_TOOLS[@]}"; do
    if tool_version_ok "$binary"; then
      resolved="$(command -v "$binary" 2>/dev/null || true)"
      if [[ -n "$resolved" && "$resolved" != "$EPFLEMMA_BIN_DIR/$binary" ]]; then
        ln -sf "$resolved" "$EPFLEMMA_BIN_DIR/$binary"
      fi
      continue
    fi
    for candidate in \
      "$EPFLEMMA_HOME/vendor/os-tools/usr/bin/$binary" \
      "$HOME/.local/usr/bin/$binary" \
      "$HOME/miniconda3/bin/$binary" \
      "$HOME/miniforge3/bin/$binary" \
      "$HOME/mambaforge/bin/$binary" \
      "/opt/homebrew/bin/$binary" \
      "/usr/local/bin/$binary" \
      "/usr/bin/$binary" \
      "/bin/$binary"; do
      if [[ -x "$candidate" ]]; then
        ln -sf "$candidate" "$EPFLEMMA_BIN_DIR/$binary"
        if tool_version_ok "$binary"; then
          ok "linked $binary: $candidate"
          break
        fi
        rm -f "$EPFLEMMA_BIN_DIR/$binary"
      fi
    done
  done
}

install_external_tools_with_brew() {
  command -v brew >/dev/null 2>&1 || return 1
  local packages=()
  tool_version_ok rg || packages+=(ripgrep)
  if ! tool_version_ok pdftotext || ! tool_version_ok pdfinfo || ! tool_version_ok pdfimages; then
    packages+=(poppler)
  fi
  ((${#packages[@]})) || return 0
  warn "installing external tools with Homebrew: ${packages[*]}"
  brew install "${packages[@]}"
}

install_external_tools_with_apt_sudo() {
  command -v apt-get >/dev/null 2>&1 || return 1
  command -v sudo >/dev/null 2>&1 || return 1
  sudo -n true >/dev/null 2>&1 || return 1
  local packages=()
  tool_version_ok rg || packages+=(ripgrep)
  if ! tool_version_ok pdftotext || ! tool_version_ok pdfinfo || ! tool_version_ok pdfimages; then
    packages+=(poppler-utils)
  fi
  ((${#packages[@]})) || return 0
  warn "installing external tools with apt: ${packages[*]}"
  sudo env DEBIAN_FRONTEND=noninteractive apt-get update
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
}

install_user_local_ripgrep_with_apt_download() {
  tool_version_ok rg && return 0
  command -v apt-get >/dev/null 2>&1 || return 1
  command -v dpkg-deb >/dev/null 2>&1 || return 1
  local tmp_dir
  local deb
  tmp_dir="$(mktemp -d)"
  if (
    cd "$tmp_dir"
    apt-get download ripgrep >/dev/null
  ); then
    deb="$(find "$tmp_dir" -name 'ripgrep_*.deb' -print -quit)"
    if [[ -n "$deb" ]]; then
      mkdir -p "$EPFLEMMA_HOME/vendor/os-tools"
      dpkg-deb -x "$deb" "$EPFLEMMA_HOME/vendor/os-tools"
      ln -sf "$EPFLEMMA_HOME/vendor/os-tools/usr/bin/rg" "$EPFLEMMA_BIN_DIR/rg"
    fi
  fi
  rm -rf "$tmp_dir"
  tool_version_ok rg
}

ensure_external_cli_tools() {
  if [[ "$EPFLEMMA_INSTALL_OS_TOOLS" != "1" ]]; then
    warn "skipped external CLI tool installation"
    return 0
  fi

  link_external_tool_candidates
  if external_tools_ready; then
    ok "external tools wired into PATH: ${REQUIRED_EXTERNAL_TOOLS[*]}"
    return 0
  fi

  case "$(uname -s 2>/dev/null || true)" in
    Darwin)
      install_external_tools_with_brew || true
      ;;
    Linux)
      install_external_tools_with_apt_sudo || true
      install_user_local_ripgrep_with_apt_download || true
      ;;
  esac

  link_external_tool_candidates
  if external_tools_ready; then
    ok "external tools ready: ${REQUIRED_EXTERNAL_TOOLS[*]}"
  else
    warn "missing external CLI tools after install attempt: $(missing_external_tools_text)"
    warn "PDF extraction needs poppler-utils/poppler; local search needs ripgrep"
  fi
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
  --skip-os-tools       Do not install or wire external CLI tools
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
    --skip-os-tools)
      EPFLEMMA_INSTALL_OS_TOOLS=0
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
export PATH="$EPFLEMMA_BIN_DIR:$PATH"
ok "home: $EPFLEMMA_HOME"
ok "bin : $EPFLEMMA_BIN_DIR"

if [[ "$RECREATE_VENV" == "1" && -e "$EPFLEMMA_VENV_DIR" ]]; then
  warn "recreating virtualenv: $EPFLEMMA_VENV_DIR"
  rm -rf "$EPFLEMMA_VENV_DIR"
fi

step "Checking external CLI tools"
ensure_external_cli_tools

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
  python -m pip install --quiet --quiet -e "$REPO_ROOT[mcp,lean-explore]"
  ok "installed editable package"
else
  python -m pip install --quiet --quiet "$REPO_ROOT[mcp,lean-explore]"
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
export PATH="${EPFLEMMA_BIN_DIR}:\$PATH"
exec "${EPFLEMMA_VENV_DIR}/bin/epflemma" "\$@"
EOF

cat > "$EPFLEMMA_BIN_DIR/epflemma-agent" <<EOF
#!/usr/bin/env bash
: "\${EPFLEMMA_HOME:=${EPFLEMMA_HOME}}"
export OPENGAUSS_HOME="\${OPENGAUSS_HOME:-\${EPFLEMMA_HOME}}"
export EPFLEMMA_HOME
export PATH="${EPFLEMMA_BIN_DIR}:\$PATH"
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
