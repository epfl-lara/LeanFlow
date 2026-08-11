#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

LEANFLOW_HOME="${LEANFLOW_HOME:-$HOME/.leanflow}"
LEANFLOW_BIN_DIR="${LEANFLOW_BIN_DIR:-$HOME/.local/bin}"
LEANFLOW_VENV_DIR="${LEANFLOW_VENV_DIR:-$REPO_ROOT/.leanflow-venv}"
LEANFLOW_INSTALL_PYTHON="${LEANFLOW_INSTALL_PYTHON:-python3}"
LEANFLOW_FETCH_LEANEXPLORE_DATA="${LEANFLOW_FETCH_LEANEXPLORE_DATA:-1}"
LEANFLOW_INSTALL_OS_TOOLS="${LEANFLOW_INSTALL_OS_TOOLS:-1}"
INSTALL_MODE="editable"
RECREATE_VENV=0
STEP=0
TOTAL_STEPS=11
REQUIRED_EXTERNAL_TOOLS=(rg pdftotext pdfinfo pdfimages)

banner() {
  printf '\n'
  printf '============================================================\n'
  printf ' LeanFlow Installer\n'
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
      if [[ -n "$resolved" && "$resolved" != "$LEANFLOW_BIN_DIR/$binary" ]]; then
        ln -sf "$resolved" "$LEANFLOW_BIN_DIR/$binary"
      fi
      continue
    fi
    for candidate in \
      "$LEANFLOW_HOME/vendor/os-tools/usr/bin/$binary" \
      "$HOME/.local/usr/bin/$binary" \
      "$HOME/miniconda3/bin/$binary" \
      "$HOME/miniforge3/bin/$binary" \
      "$HOME/mambaforge/bin/$binary" \
      "/opt/homebrew/bin/$binary" \
      "/usr/local/bin/$binary" \
      "/usr/bin/$binary" \
      "/bin/$binary"; do
      if [[ -x "$candidate" ]]; then
        ln -sf "$candidate" "$LEANFLOW_BIN_DIR/$binary"
        if tool_version_ok "$binary"; then
          ok "linked $binary: $candidate"
          break
        fi
        rm -f "$LEANFLOW_BIN_DIR/$binary"
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
      mkdir -p "$LEANFLOW_HOME/vendor/os-tools"
      dpkg-deb -x "$deb" "$LEANFLOW_HOME/vendor/os-tools"
      ln -sf "$LEANFLOW_HOME/vendor/os-tools/usr/bin/rg" "$LEANFLOW_BIN_DIR/rg"
    fi
  fi
  rm -rf "$tmp_dir"
  tool_version_ok rg
}

ensure_external_cli_tools() {
  if [[ "$LEANFLOW_INSTALL_OS_TOOLS" != "1" ]]; then
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
LeanFlow local installer

Usage:
  ./scripts/install-internal.sh [options]

Options:
  --leanflow-home PATH  LeanFlow state directory (default: ~/.leanflow)
  --bin-dir PATH         Directory for LeanFlow wrappers (default: ~/.local/bin)
  --venv-dir PATH        Virtualenv path (default: ./.leanflow-venv)
  --python BIN           Python interpreter to use (default: python3)
  --recreate-venv        Remove and recreate the virtualenv
  --skip-leanexplore-data
                        Skip local LeanExplore index fetch
  --skip-os-tools       Do not install or wire external CLI tools
  --no-editable          Install a wheel instead of editable mode
  -h, --help             Show this help

Behavior:
  This installer creates separate LeanFlow binaries and state under ~/.leanflow.
TXT
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --leanflow-home)
      LEANFLOW_HOME="$2"
      shift 2
      ;;
    --bin-dir)
      LEANFLOW_BIN_DIR="$2"
      shift 2
      ;;
    --venv-dir)
      LEANFLOW_VENV_DIR="$2"
      shift 2
      ;;
    --python)
      LEANFLOW_INSTALL_PYTHON="$2"
      shift 2
      ;;
    --recreate-venv)
      RECREATE_VENV=1
      shift
      ;;
    --skip-leanexplore-data)
      LEANFLOW_FETCH_LEANEXPLORE_DATA=0
      shift
      ;;
    --skip-os-tools)
      LEANFLOW_INSTALL_OS_TOOLS=0
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
mkdir -p "$LEANFLOW_HOME" "$LEANFLOW_BIN_DIR"
export PATH="$LEANFLOW_BIN_DIR:$PATH"
ok "home: $LEANFLOW_HOME"
ok "bin : $LEANFLOW_BIN_DIR"

if [[ "$RECREATE_VENV" == "1" && -e "$LEANFLOW_VENV_DIR" ]]; then
  warn "recreating virtualenv: $LEANFLOW_VENV_DIR"
  rm -rf "$LEANFLOW_VENV_DIR"
fi

step "Checking external CLI tools"
ensure_external_cli_tools

step "Preparing Python environment"
if [[ ! -x "$LEANFLOW_VENV_DIR/bin/python" ]]; then
  "$LEANFLOW_INSTALL_PYTHON" -m venv "$LEANFLOW_VENV_DIR"
  ok "created virtualenv: $LEANFLOW_VENV_DIR"
else
  ok "using existing virtualenv: $LEANFLOW_VENV_DIR"
fi

# shellcheck disable=SC1090
source "$LEANFLOW_VENV_DIR/bin/activate"

step "Installing LeanFlow package"
python -m pip install --quiet --quiet --upgrade pip "setuptools<82" wheel
if [[ "$INSTALL_MODE" == "editable" ]]; then
  python -m pip install --quiet --quiet -e "$REPO_ROOT[mcp,lean-explore]"
  ok "installed editable package"
else
  python -m pip install --quiet --quiet "$REPO_ROOT[mcp,lean-explore]"
  ok "installed package wheel"
fi

step "Fetching local LeanExplore data"
if [[ "$LEANFLOW_FETCH_LEANEXPLORE_DATA" == "1" ]]; then
  if "$LEANFLOW_VENV_DIR/bin/lean-explore" data fetch; then
    ok "LeanExplore local data ready"
  else
    warn "LeanExplore data fetch failed; semantic search will fall back to hosted API/MCP/rg until you run lean-explore data fetch"
  fi
else
  warn "skipped LeanExplore data fetch"
fi

# Create/backfill user-visible config before any bootstrap step that may need
# provider/env discovery. This makes ~/.leanflow/config.yaml and ~/.leanflow/.env
# explicit installation artifacts instead of hidden first-run side effects.
step "Creating LeanFlow config"
LEANFLOW_HOME="$LEANFLOW_HOME" "$LEANFLOW_VENV_DIR/bin/python" <<'PY'
from leanflow_cli.config import ensure_leanflow_home, load_config

ensure_leanflow_home()
load_config()
PY
ok "config: $LEANFLOW_HOME/config.yaml"

step "Installing managed Lean MCP backends and power modes"
LEANFLOW_HOME="$LEANFLOW_HOME" "$LEANFLOW_VENV_DIR/bin/leanflow" mcp bootstrap lean
ok "managed MCP bootstrap complete"

step "Writing command wrappers"
cat > "$LEANFLOW_BIN_DIR/leanflow" <<EOF
#!/usr/bin/env bash
: "\${LEANFLOW_HOME:=${LEANFLOW_HOME}}"
export OPENLEANFLOW_HOME="\${OPENLEANFLOW_HOME:-\${LEANFLOW_HOME}}"
export LEANFLOW_HOME
export PATH="${LEANFLOW_BIN_DIR}:\$PATH"
exec "${LEANFLOW_VENV_DIR}/bin/leanflow" "\$@"
EOF

cat > "$LEANFLOW_BIN_DIR/leanflow-agent" <<EOF
#!/usr/bin/env bash
: "\${LEANFLOW_HOME:=${LEANFLOW_HOME}}"
export OPENLEANFLOW_HOME="\${OPENLEANFLOW_HOME:-\${LEANFLOW_HOME}}"
export LEANFLOW_HOME
export PATH="${LEANFLOW_BIN_DIR}:\$PATH"
exec "${LEANFLOW_VENV_DIR}/bin/leanflow-agent" "\$@"
EOF

chmod +x \
  "$LEANFLOW_BIN_DIR/leanflow" \
  "$LEANFLOW_BIN_DIR/leanflow-agent"
ok "wrapper: $LEANFLOW_BIN_DIR/leanflow"
ok "wrapper: $LEANFLOW_BIN_DIR/leanflow-agent"

step "Cleaning legacy wrapper names"
rm -f \
  "$LEANFLOW_BIN_DIR/leanflow-acp" \
  "$LEANFLOW_BIN_DIR/epflemma" \
  "$LEANFLOW_BIN_DIR/epflemma-prove" \
  "$LEANFLOW_BIN_DIR/epflemma-formalize"
ok "legacy wrappers removed if present"

step "Recording install metadata"
cat > "${LEANFLOW_HOME}/install-root" <<EOF
repo_root=${REPO_ROOT}
venv_dir=${LEANFLOW_VENV_DIR}
bin_dir=${LEANFLOW_BIN_DIR}
installed_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
EOF
ok "metadata: ${LEANFLOW_HOME}/install-root"

step "Final smoke check"
LEANFLOW_HOME="$LEANFLOW_HOME" "$LEANFLOW_VENV_DIR/bin/leanflow" --help >/dev/null
ok "leanflow --help"

printf '\nLeanFlow installed.\n'
printf '  repo: %s\n' "$REPO_ROOT"
printf '  home: %s\n' "$LEANFLOW_HOME"
printf '  config: %s/config.yaml\n' "$LEANFLOW_HOME"
printf '  env : %s/.env\n' "$LEANFLOW_HOME"
printf '  venv: %s\n' "$LEANFLOW_VENV_DIR"
printf '  bin : %s\n' "$LEANFLOW_BIN_DIR"
printf '  mcp : managed Lean MCP backends installed under %s/mcp\n' "$LEANFLOW_HOME"
printf '  power modes: local Loogle/REPL configured when supported; public remote search fallbacks remain enabled\n'
printf '\n'
printf 'Add %s to PATH if needed, then run:\n' "$LEANFLOW_BIN_DIR"
printf '  leanflow --help\n'
printf '  leanflow mcp status\n'
printf '  leanflow project init   # inside a Lean repo, to build REPL acceleration\n'
