#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PULL=1
INSTALL_ARGS=()

usage() {
  cat <<'TXT'
LeanFlow sandbox updater

Usage:
  ./scripts/update-sandbox.sh [install-internal options] [--no-pull] [--with-local-lean-explore]

The updater fast-forwards the repository when possible, reinstalls LeanFlow,
and rebuilds the sandbox image from the current checkout.
TXT
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-pull)
      PULL=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      INSTALL_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ "$PULL" == "1" && -d "$REPO_ROOT/.git" ]]; then
  git -C "$REPO_ROOT" pull --ff-only
fi

if ((${#INSTALL_ARGS[@]})); then
  "$REPO_ROOT/scripts/install-sandbox.sh" "${INSTALL_ARGS[@]}"
else
  "$REPO_ROOT/scripts/install-sandbox.sh"
fi
