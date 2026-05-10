#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

EPFLEMMA_BIN_DIR="${EPFLEMMA_BIN_DIR:-${OPENGAUSS_BIN_DIR:-$HOME/.local/bin}}"
EPFLEMMA_HOME="${EPFLEMMA_HOME:-$HOME/.epflemma}"
BUILD_IMAGE=1
BUILD_LOCAL_LEANEXPLORE=0
INSTALL_ARGS=()

usage() {
  cat <<'TXT'
EPFLemma sandbox installer

Usage:
  ./scripts/install-sandbox.sh [install-internal options] [--no-build] [--with-local-lean-explore]

This runs the normal EPFLemma installer, builds the local container image, and
writes an epflemma-sandbox wrapper that runs commands through:

  epflemma sandbox run -- <command>

Rerun this script after pulling EPFLemma changes to upgrade the sandbox image.

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
    --epflemma-home|--gauss-home)
      EPFLEMMA_HOME="$2"
      INSTALL_ARGS+=("$1" "$2")
      shift 2
      ;;
    --bin-dir)
      EPFLEMMA_BIN_DIR="$2"
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
    EPFLEMMA_HOME="$EPFLEMMA_HOME" "$EPFLEMMA_BIN_DIR/epflemma" sandbox build --with-local-lean-explore
  else
    EPFLEMMA_HOME="$EPFLEMMA_HOME" "$EPFLEMMA_BIN_DIR/epflemma" sandbox build
  fi
fi

mkdir -p "$EPFLEMMA_BIN_DIR"
cat > "$EPFLEMMA_BIN_DIR/epflemma-sandbox" <<EOF
#!/usr/bin/env bash
: "\${EPFLEMMA_HOME:=${EPFLEMMA_HOME}}"
export EPFLEMMA_HOME
exec "${EPFLEMMA_BIN_DIR}/epflemma" sandbox run -- "\$@"
EOF
chmod +x "$EPFLEMMA_BIN_DIR/epflemma-sandbox"

printf '\nEPFLemma sandbox installed.\n'
printf '  wrapper: %s/epflemma-sandbox\n' "$EPFLEMMA_BIN_DIR"
printf '  status : epflemma sandbox status\n'
printf '  run    : epflemma-sandbox workflow prove Main.lean\n'
