#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNNER_VENV="${REPO_ROOT}/.leanflow-installer-venv"
RUNNER_PYTHON="${RUNNER_VENV}/bin/python"
RUNNER_MORPH="${RUNNER_VENV}/bin/morphcloud"

ensure_runner_venv() {
  if [[ -x "$RUNNER_PYTHON" ]] && "$RUNNER_PYTHON" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0)
PY
  then
    return 0
  fi

  local runner_python_version="${LEANFLOW_INSTALLER_RUNNER_PYTHON:-3.13}"
  uv venv --seed --python "$runner_python_version" "$RUNNER_VENV"
}

ensure_runner_pip() {
  local pip_version
  pip_version="$("$RUNNER_PYTHON" -m pip --version 2>/dev/null || true)"
  if [[ "$pip_version" != *"$RUNNER_VENV"* ]]; then
    "$RUNNER_PYTHON" -m ensurepip --upgrade
  fi
}

install_runner_morphcloud() {
  "$RUNNER_PYTHON" -m pip install --upgrade morphcloud
}

main() {
  local morph_args=()

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --leanflow-home)
        export LEANFLOW_HOME="$2"
        shift 2
        ;;
      --workspace-dir)
        export LEANFLOW_WORKSPACE_DIR="$2"
        shift 2
        ;;
      --skip-system-packages)
        export LEANFLOW_SKIP_SYSTEM_PACKAGES="1"
        shift
        ;;
      --with-workspace)
        export LEANFLOW_CREATE_WORKSPACE="1"
        shift
        ;;
      --skip-setup)
        export LEANFLOW_SETUP_MODE="skip"
        shift
        ;;
      --recreate-venv)
        export LEANFLOW_RECREATE_VENV="1"
        shift
        ;;
      --plain|--json)
        morph_args+=("$1")
        shift
        ;;
      --param|--secret)
        morph_args+=("$1" "$2")
        shift 2
        ;;
      *)
        morph_args+=("$1")
        shift
        ;;
    esac
  done

  export LEANFLOW_SKIP_SHELL_AUTOENV="1"

  ensure_runner_venv
  ensure_runner_pip
  install_runner_morphcloud

  if ((${#morph_args[@]})); then
    exec "$RUNNER_MORPH" devbox template run leanflow --experimental-run-locally "${morph_args[@]}"
  fi
  exec "$RUNNER_MORPH" devbox template run leanflow --experimental-run-locally
}

main "$@"
