#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="${REPO_ROOT:-/src}"
INSTALL_ROOT="$(mktemp -d /tmp/leanflow-install-smoke.XXXXXX)"
REPO_ROOT="$INSTALL_ROOT/repo"
LEANFLOW_HOME="$INSTALL_ROOT/state"
LEANFLOW_BIN_DIR="$INSTALL_ROOT/bin"
unset LEANFLOW_VENV_DIR
export LEANFLOW_HOME

die() {
    printf 'ERROR: %s\n' "$1" >&2
    exit 1
}

[[ -f "$SOURCE_ROOT/scripts/install-internal.sh" ]] || die "installer not found under $SOURCE_ROOT"
mkdir -p "$REPO_ROOT"

# Copy tracked sources from the read-only mount; never install into the host checkout.
git -c safe.directory="$SOURCE_ROOT" -C "$SOURCE_ROOT" ls-files -z | while IFS= read -r -d '' source; do
    [[ -f "$SOURCE_ROOT/$source" ]] || continue
    mkdir -p "$REPO_ROOT/$(dirname "$source")"
    cp "$SOURCE_ROOT/$source" "$REPO_ROOT/$source"
done
cd "$REPO_ROOT"

INSTALL_ARGS=(
    --leanflow-home "$LEANFLOW_HOME"
    --bin-dir "$LEANFLOW_BIN_DIR"
    --skip-leanexplore-data
)
./scripts/install-internal.sh "${INSTALL_ARGS[@]}"

export PATH="$LEANFLOW_BIN_DIR:$PATH"
for command in leanflow rg pdftotext pdfinfo pdfimages; do
    command -v "$command" >/dev/null || die "expected command on PATH: $command"
done
leanflow --help
leanflow version
leanflow mcp status --json

LEANFLOW_VENV_DIR="$(sed -n 's/^venv_dir=//p' "$LEANFLOW_HOME/install-root")"
"$LEANFLOW_VENV_DIR/bin/python" - "$REPO_ROOT" "$LEANFLOW_HOME" "$LEANFLOW_VENV_DIR" "$LEANFLOW_BIN_DIR" <<'CHECK'
import json
from pathlib import Path
import sys

from leanflow_cli.lean.lean_workflow_specs import load_lean_specs, validate_lean_specs
from leanflow_cli.runtime.skill_core import discover_skills

repo_root, state, venv, binaries = map(Path, sys.argv[1:])
assert (state / 'config.yaml').is_file()
assert (state / '.env').is_file()
metadata = dict(line.split('=', 1) for line in (state / 'install-root').read_text().splitlines())
assert metadata['repo_root'] == str(repo_root)
assert metadata['venv_dir'] == str(venv)
assert metadata['bin_dir'] == str(binaries)
assert venv.parent.parent == state / 'runtimes'
assert json.loads((venv.parent / '.leanflow-storage.json').read_text())['ready'] is True
assert not (binaries / 'leanflow-agent').exists()
assert load_lean_specs()
assert discover_skills()
assert validate_lean_specs() == []
CHECK

printf '\n# installer smoke marker\n' >> "$LEANFLOW_HOME/config.yaml"
printf '\nSMOKE_CUSTOM_VALUE=preserved\n' >> "$LEANFLOW_HOME/.env"
./scripts/install-internal.sh "${INSTALL_ARGS[@]}" --skip-os-tools
[[ "$(sed -n 's/^venv_dir=//p' "$LEANFLOW_HOME/install-root")" == "$LEANFLOW_VENV_DIR" ]] \
    || die "unchanged source did not reuse its validated runtime"
rg -F '# installer smoke marker' "$LEANFLOW_HOME/config.yaml"
rg -F 'SMOKE_CUSTOM_VALUE=preserved' "$LEANFLOW_HOME/.env"
leanflow --help >/dev/null

echo 'Ubuntu repository-local installer smoke passed.'
