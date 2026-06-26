#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

LEANFLOW_BIN_DIR="${LEANFLOW_BIN_DIR:-$HOME/.local/bin}"
LEANFLOW_HOME="${LEANFLOW_HOME:-$HOME/.leanflow}"
BUILD_IMAGE=1
BUILD_LOCAL_LEANEXPLORE=0
INSTALL_ARGS=()

usage() {
  cat <<'TXT'
LeanFlow sandbox installer

Usage:
  ./scripts/install-sandbox.sh [install-internal options] [--no-build] [--with-local-lean-explore]

This runs the normal LeanFlow installer, builds the local container image, and
writes an leanflow-sandbox wrapper that runs commands through:

  leanflow sandbox run -- <command>

Rerun this script after pulling LeanFlow changes to upgrade the sandbox image.

Use --with-local-lean-explore to bake lean-explore[local] and its embedding
stack into the sandbox image. This makes the image much larger.
TXT
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-build)
      BUILD_IMAGE=0
      shift
      ;;
    --with-local-lean-explore)
      BUILD_LOCAL_LEANEXPLORE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --leanflow-home)
      LEANFLOW_HOME="$2"
      INSTALL_ARGS+=("$1" "$2")
      shift 2
      ;;
    --bin-dir)
      LEANFLOW_BIN_DIR="$2"
      INSTALL_ARGS+=("$1" "$2")
      shift 2
      ;;
    *)
      INSTALL_ARGS+=("$1")
      shift
      ;;
  esac
done

if ((${#INSTALL_ARGS[@]})); then
  "$REPO_ROOT/scripts/install-internal.sh" "${INSTALL_ARGS[@]}"
else
  "$REPO_ROOT/scripts/install-internal.sh"
fi

if [[ "$BUILD_IMAGE" == "1" ]]; then
  if [[ "$BUILD_LOCAL_LEANEXPLORE" == "1" ]]; then
    LEANFLOW_HOME="$LEANFLOW_HOME" "$LEANFLOW_BIN_DIR/leanflow" sandbox build --with-local-lean-explore
  else
    LEANFLOW_HOME="$LEANFLOW_HOME" "$LEANFLOW_BIN_DIR/leanflow" sandbox build
  fi
fi

mkdir -p "$LEANFLOW_BIN_DIR"
cat > "$LEANFLOW_BIN_DIR/leanflow-sandbox" <<EOF
#!/usr/bin/env bash
: "\${LEANFLOW_HOME:=${LEANFLOW_HOME}}"
export LEANFLOW_HOME
exec "${LEANFLOW_BIN_DIR}/leanflow" sandbox run -- "\$@"
EOF
chmod +x "$LEANFLOW_BIN_DIR/leanflow-sandbox"

printf '\nLeanFlow sandbox installed.\n'
printf '  wrapper: %s/leanflow-sandbox\n' "$LEANFLOW_BIN_DIR"
printf '  status : leanflow sandbox status\n'
printf '  run    : leanflow-sandbox workflow prove Main.lean\n'
