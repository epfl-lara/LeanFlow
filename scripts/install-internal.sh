#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

LEANFLOW_HOME="${LEANFLOW_HOME:-$HOME/.leanflow}"
LEANFLOW_BIN_DIR="${LEANFLOW_BIN_DIR:-$HOME/.local/bin}"
LEANFLOW_VENV_DIR="${LEANFLOW_VENV_DIR:-}"
INSTALL_PYTHON_EXPLICIT=0
if [[ -n "${LEANFLOW_INSTALL_PYTHON:-}" ]]; then
  INSTALL_PYTHON_EXPLICIT=1
fi
LEANFLOW_INSTALL_PYTHON="${LEANFLOW_INSTALL_PYTHON:-python3}"
LEANFLOW_FETCH_LEANEXPLORE_DATA="${LEANFLOW_FETCH_LEANEXPLORE_DATA:-0}"
LEANFLOW_INSTALL_OS_TOOLS="${LEANFLOW_INSTALL_OS_TOOLS:-1}"
INSTALL_MODE="editable"
RECREATE_VENV=0
STEP=0
TOTAL_STEPS=12
REQUIRED_EXTERNAL_TOOLS=(rg pdftotext pdfinfo pdfimages)
if [[ "$(uname -s)" == "Linux" ]]; then
  REQUIRED_EXTERNAL_TOOLS+=(bwrap)
fi

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

python_supported() {
  # Probe with syntax supported by old Python before importing repository code.
  "$1" -c 'import sys, venv, ensurepip; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
    >/dev/null 2>&1
}

select_install_python() {
  local candidate
  if [[ "$INSTALL_PYTHON_EXPLICIT" == "1" ]]; then
    if python_supported "$LEANFLOW_INSTALL_PYTHON"; then
      return 0
    fi
    printf 'LeanFlow installation requires Python 3.12+ with venv support; the explicitly selected interpreter is unsupported or unavailable: %s\n' \
      "$LEANFLOW_INSTALL_PYTHON" >&2
    printf 'Use --python PATH to select a supported interpreter.\n' >&2
    return 1
  fi
  for candidate in \
    python3 python3.13 python3.12 python3.14 python \
    "$HOME/miniforge3/bin/python" \
    "$HOME/miniconda3/bin/python" \
    "$HOME/mambaforge/bin/python" \
    /opt/homebrew/bin/python3 \
    /opt/homebrew/bin/python3.13 \
    /opt/homebrew/bin/python3.12 \
    /usr/local/bin/python3 \
    "$REPO_ROOT/.venv/bin/python"; do
    if python_supported "$candidate"; then
      LEANFLOW_INSTALL_PYTHON="$candidate"
      return 0
    fi
  done
  printf 'LeanFlow installation requires Python 3.12+ with venv support. Install a supported Python or select it with --python PATH.\n' >&2
  return 1
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
    # Ubuntu AppArmor attaches to the system executable, not an extracted copy.
    if [[ "$binary" == "bwrap" && "$(uname -s)" == "Linux" && -x /usr/bin/bwrap ]]; then
      if /usr/bin/bwrap --version >/dev/null 2>&1; then
        ln -sf /usr/bin/bwrap "$LEANFLOW_BIN_DIR/bwrap"
        continue
      fi
    fi
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
  tool_version_ok bwrap || packages+=(bubblewrap)
  if ! tool_version_ok pdftotext || ! tool_version_ok pdfinfo || ! tool_version_ok pdfimages; then
    packages+=(poppler-utils)
  fi
  ((${#packages[@]})) || return 0
  warn "installing external tools with apt: ${packages[*]}"
  sudo env DEBIAN_FRONTEND=noninteractive apt-get update
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
}

install_user_local_tool_with_apt_download() {
  local binary="$1"
  local package="$2"
  tool_version_ok "$binary" && return 0
  command -v apt-get >/dev/null 2>&1 || return 1
  command -v dpkg-deb >/dev/null 2>&1 || return 1
  local tmp_dir
  local deb
  tmp_dir="$(mktemp -d)"
  if (
    cd "$tmp_dir"
    apt-get download "$package" >/dev/null
  ); then
    deb="$(find "$tmp_dir" -name "${package}_*.deb" -print -quit)"
    if [[ -n "$deb" ]]; then
      mkdir -p "$LEANFLOW_HOME/vendor/os-tools"
      dpkg-deb -x "$deb" "$LEANFLOW_HOME/vendor/os-tools"
      ln -sf "$LEANFLOW_HOME/vendor/os-tools/usr/bin/$binary" "$LEANFLOW_BIN_DIR/$binary"
    fi
  fi
  rm -rf "$tmp_dir"
  tool_version_ok "$binary"
}

ensure_external_cli_tools() {
  if [[ "$LEANFLOW_INSTALL_OS_TOOLS" != "1" ]]; then
    warn "skipped external CLI tool installation"
    return 0
  fi

  link_external_tool_candidates
  if external_tools_ready; then
    ok "external tools wired into PATH: ${REQUIRED_EXTERNAL_TOOLS[*]}"
  else
    case "$(uname -s 2>/dev/null || true)" in
      Darwin)
        install_external_tools_with_brew || true
        ;;
      Linux)
        install_external_tools_with_apt_sudo || true
        install_user_local_tool_with_apt_download rg ripgrep || true
        install_user_local_tool_with_apt_download bwrap bubblewrap || true
        ;;
    esac

    link_external_tool_candidates
    if external_tools_ready; then
      ok "external tools ready: ${REQUIRED_EXTERNAL_TOOLS[*]}"
    else
      warn "missing external CLI tools after install attempt: $(missing_external_tools_text)"
      warn "PDF extraction needs poppler-utils/poppler; local search needs ripgrep"
    fi
  fi
  if [[ "$(uname -s)" == "Linux" ]] && tool_version_ok bwrap; then
    if ! bwrap --ro-bind / / --unshare-user --unshare-pid --unshare-net --proc /proc --dev /dev /bin/true >/dev/null 2>&1; then
      warn "Bubblewrap cannot create the required sandbox. Native proving is unavailable until the host permits its user namespaces."
      warn "On restricted Ubuntu hosts, installing bubblewrap alone may not suffice. Ask an administrator to load an AppArmor profile for /usr/bin/bwrap; see docs/product-reference.md."
    fi
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
  --venv-dir PATH        Fresh virtualenv path (default: versioned runtime in ~/.leanflow)
  --python BIN           Python 3.12+ interpreter (default: detect a supported local Python)
  --recreate-venv        Build a fresh runtime; never remove an existing environment
  --skip-leanexplore-data Skip local LeanExplore installation and index fetch
  --local-leanexplore    Install optional local semantic search and fetch its data
  --skip-os-tools       Do not install or wire external CLI tools
  --no-editable          Install a wheel instead of editable mode
  -h, --help             Show this help

Behavior:
  Installs use separate versioned runtimes; running jobs keep their original runtime.
  Existing environments are never upgraded or deleted. Wrappers switch after validation.
  The full installation requires Python 3.12+ with venv and pip bootstrap support.
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
      INSTALL_PYTHON_EXPLICIT=1
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
    --local-leanexplore)
      LEANFLOW_FETCH_LEANEXPLORE_DATA=1
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

select_install_python
banner
step "Preparing install directories"
mkdir -p "$LEANFLOW_HOME" "$LEANFLOW_BIN_DIR"
export PATH="$LEANFLOW_BIN_DIR:$PATH"
ok "home: $LEANFLOW_HOME"
ok "bin : $LEANFLOW_BIN_DIR"
ok "Python: $LEANFLOW_INSTALL_PYTHON"

INSTALL_ARGS=(prepare --source "$REPO_ROOT" --mode "$INSTALL_MODE" --installer-pid "$$")
if [[ "$LEANFLOW_FETCH_LEANEXPLORE_DATA" == "1" ]]; then
  INSTALL_ARGS+=(--local-search)
fi
if [[ -n "$LEANFLOW_VENV_DIR" ]]; then
  INSTALL_ARGS+=(--venv-dir "$LEANFLOW_VENV_DIR")
fi
if [[ "$RECREATE_VENV" == "1" ]]; then
  INSTALL_ARGS+=(--recreate)
fi
INSTALL_PLAN="$(LEANFLOW_HOME="$LEANFLOW_HOME" PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" "$LEANFLOW_INSTALL_PYTHON" \
  -m leanflow_cli.runtime.runtime_install "${INSTALL_ARGS[@]}")"
INSTALL_REUSE="${INSTALL_PLAN%%$'\n'*}"
LEANFLOW_VENV_DIR="${INSTALL_PLAN#*$'\n'}"

step "Checking external CLI tools"
ensure_external_cli_tools

step "Preparing Python environment"
if [[ "$INSTALL_REUSE" != "reuse" ]]; then
  "$LEANFLOW_INSTALL_PYTHON" -m venv "$LEANFLOW_VENV_DIR"
  ok "created virtualenv: $LEANFLOW_VENV_DIR"
else
  ok "using validated immutable runtime: $LEANFLOW_VENV_DIR"
fi

# shellcheck disable=SC1090
source "$LEANFLOW_VENV_DIR/bin/activate"

step "Installing LeanFlow package"
if [[ "$INSTALL_REUSE" == "reuse" ]]; then
  ok "retained installed package without modifying its environment"
else
  PACKAGE_EXTRAS="mcp"
  if [[ "$LEANFLOW_FETCH_LEANEXPLORE_DATA" == "1" ]]; then
    PACKAGE_EXTRAS="mcp,lean-explore"
  fi
  python -m pip install --quiet --quiet --upgrade pip "setuptools<82" wheel
  if [[ "$INSTALL_MODE" == "editable" ]]; then
    python -m pip install --quiet --quiet -e "$REPO_ROOT[$PACKAGE_EXTRAS]"
    ok "installed editable package"
  else
    python -m pip install --quiet --quiet "$REPO_ROOT[$PACKAGE_EXTRAS]"
    ok "installed package wheel"
  fi
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

step "Ensuring a compatible private Lemex runtime"
LEANFLOW_HOME="$LEANFLOW_HOME" "$LEANFLOW_VENV_DIR/bin/python" -m leanflow_cli.runtime.lemex_install
ok "Lemex executable managed separately from its native configuration"

step "Installing managed Lean MCP backends and power modes"
LEANFLOW_HOME="$LEANFLOW_HOME" "$LEANFLOW_VENV_DIR/bin/python" \
  -m leanflow_cli.runtime.runtime_install ensure-backends
ok "managed MCP bootstrap complete"

step "Validating the new runtime before switching commands"
LEANFLOW_HOME="$LEANFLOW_HOME" "$LEANFLOW_VENV_DIR/bin/leanflow" --help >/dev/null
ok "leanflow --help"

step "Writing command wrappers"
LEANFLOW_HOME="$LEANFLOW_HOME" "$LEANFLOW_VENV_DIR/bin/python" \
  -m leanflow_cli.runtime.runtime_install finalize \
  --source "$REPO_ROOT" --venv-dir "$LEANFLOW_VENV_DIR" --bin-dir "$LEANFLOW_BIN_DIR"
ok "wrapper: $LEANFLOW_BIN_DIR/leanflow"

step "Cleaning legacy wrapper names"
rm -f \
  "$LEANFLOW_BIN_DIR/leanflow-agent" \
  "$LEANFLOW_BIN_DIR/leanflow-acp" \
  "$LEANFLOW_BIN_DIR/epflemma" \
  "$LEANFLOW_BIN_DIR/epflemma-prove" \
  "$LEANFLOW_BIN_DIR/epflemma-formalize"
ok "legacy wrappers removed if present"

step "Recording install metadata"
ok "metadata: ${LEANFLOW_HOME}/install-root"

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
