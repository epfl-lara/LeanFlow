#!/usr/bin/env bash
set -euo pipefail

SOURCE_ARCHIVE_DIR="${SOURCE_ARCHIVE_DIR:-/source-archives}"
INSTALL_ROOT="$(mktemp -d /tmp/leanflow-install-smoke.XXXXXX)"
REPO_ROOT="$INSTALL_ROOT/LeanFlow"
LEANFLOW_HOME="$INSTALL_ROOT/state"
LEANFLOW_BIN_DIR="$INSTALL_ROOT/bin"
unset LEANFLOW_VENV_DIR
export LEANFLOW_HOME

die() {
    printf 'ERROR: %s\n' "$1" >&2
    exit 1
}

[[ -f "$SOURCE_ARCHIVE_DIR/LeanFlow.tar.gz" ]] || die "LeanFlow source archive is missing"
tar -xzf "$SOURCE_ARCHIVE_DIR/LeanFlow.tar.gz" -C "$INSTALL_ROOT"
if [[ -f "$SOURCE_ARCHIVE_DIR/LeanProbe.tar.gz" ]]; then
    tar -xzf "$SOURCE_ARCHIVE_DIR/LeanProbe.tar.gz" -C "$INSTALL_ROOT"
fi
[[ -x "$REPO_ROOT/scripts/install-internal.sh" ]] || die "installer executable mode was not preserved"
cd "$REPO_ROOT"

INSTALL_ARGS=(
    --leanflow-home "$LEANFLOW_HOME"
    --bin-dir "$LEANFLOW_BIN_DIR"
    --skip-leanexplore-data
    --no-editable
    --non-interactive
)
if [[ -d "$INSTALL_ROOT/LeanProbe" ]]; then
    INSTALL_ARGS+=(--lean-probe-source "$INSTALL_ROOT/LeanProbe")
fi
./scripts/install-internal.sh "${INSTALL_ARGS[@]}"

export PATH="$LEANFLOW_BIN_DIR:$PATH"
for command in leanflow rg pdftotext pdfinfo pdfimages; do
    command -v "$command" >/dev/null || die "expected command on PATH: $command"
done
leanflow --help
leanflow version
leanflow mcp status --json

LEANFLOW_VENV_DIR="$(sed -n 's/^venv_dir=//p' "$LEANFLOW_HOME/install-root")"
"$LEANFLOW_VENV_DIR/bin/python" -I - "$REPO_ROOT" "$LEANFLOW_HOME" "$LEANFLOW_VENV_DIR" "$LEANFLOW_BIN_DIR" <<'CHECK'
import json
from pathlib import Path
import sys

from leanflow_cli.lean.lean_workflow_specs import load_lean_specs, validate_lean_specs
from leanflow_cli.runtime.skill_core import discover_skills
from lean_probe import API_REVISION
import lean_probe
import leanflow_cli

repo_root, state, venv, binaries = map(Path, sys.argv[1:])
assert (state / 'config.yaml').is_file()
assert (state / '.env').is_file()
metadata = dict(line.split('=', 1) for line in (state / 'install-root').read_text().splitlines())
assert metadata['repo_root'] == str(repo_root)
assert metadata['venv_dir'] == str(venv)
assert metadata['bin_dir'] == str(binaries)
assert venv.parent.parent == state / 'runtimes'
runtime_marker = json.loads((venv.parent / '.leanflow-storage.json').read_text())
assert runtime_marker['ready'] is True
assert not (binaries / 'leanflow-agent').exists()
assert API_REVISION >= 1
assert Path(leanflow_cli.__file__).resolve().is_relative_to(venv)
assert Path(lean_probe.__file__).resolve().is_relative_to(venv)
assert runtime_marker['install_mode'] == 'wheel'
assert not (repo_root / '.git').exists()
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

"$LEANFLOW_VENV_DIR/bin/python" -I - "$LEANFLOW_HOME" <<'CHECK_PROVIDER'
from pathlib import Path
import sys
import shutil
import yaml

state = Path(sys.argv[1])
config = yaml.safe_load((state / 'config.yaml').read_text())
assert config.get('model', {}).get('provider') == 'codex', config
lemex_store = state / 'runtimes' / 'lemex'
assert not (lemex_store / 'current.json').exists()
assert not (lemex_store / 'objects').exists()
assert shutil.which('cargo') is None
print('Default Codex route and preserved wheel runtime verified.')
CHECK_PROVIDER

echo 'Ubuntu repository-local installer smoke passed.'
