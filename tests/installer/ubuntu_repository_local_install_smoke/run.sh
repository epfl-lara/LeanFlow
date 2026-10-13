#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
IMAGE_TAG="${IMAGE_TAG:-leanflow-installer-ubuntu-repository-local-smoke}"
ARCHIVE_PYTHON="${LEANFLOW_SMOKE_ARCHIVE_PYTHON:-python3}"
COMPANION_ROOT="${LEANFLOW_LEAN_PROBE_SOURCE:-$REPO_ROOT/../LeanProbe}"

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed or not on PATH" >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: docker daemon is not running" >&2
    exit 1
fi

command -v "$ARCHIVE_PYTHON" >/dev/null || {
    echo "ERROR: Python 3 is required to prepare the source archives" >&2
    exit 1
}
ARCHIVE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/leanflow-smoke-sources.XXXXXX")"
trap 'rm -rf "$ARCHIVE_DIR"' EXIT

write_source_archive() {
    "$ARCHIVE_PYTHON" - "$1" "$2" "$ARCHIVE_DIR" <<'ARCHIVE'
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

source, name, destination = Path(sys.argv[1]).resolve(), sys.argv[2], Path(sys.argv[3])
excluded_directories = {
    '.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache',
    '.mypy_cache', '.ruff_cache', '.leanflow', '.codex', '.claude', 'build', 'dist',
}
credential_names = {'.env', 'id_rsa', 'id_ed25519', 'credentials.json', 'auth.json'}
files = {}

def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args])

def add_repository(archive, root, prefix):
    names = set(git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0'))
    for raw in sorted(names):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if any(part in excluded_directories for part in relative.parts):
            continue
        if relative.name in credential_names or (
            relative.name.startswith('.env.') and relative.suffix not in {'.example', '.sample', '.template', '.dist'}
        ):
            continue
        path = root / relative
        if not path.exists() and not path.is_symlink():
            continue  # Include intentional working-tree deletions as absent files.
        target = f'{prefix}/{relative.as_posix()}'
        if path.is_dir() and not path.is_symlink():
            nested_root = Path(os.fsdecode(git(path, 'rev-parse', '--show-toplevel')).strip()).resolve()
            if nested_root != path.resolve():
                raise RuntimeError(f'Initialize the source submodule before smoke testing: {relative}')
            add_repository(archive, path, target)
            continue
        info = archive.gettarinfo(str(path), arcname=target)
        if info.isreg():
            data = path.read_bytes()
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
            digest = hashlib.sha256(data).hexdigest()
        elif info.issym():
            archive.addfile(info)
            digest = hashlib.sha256(info.linkname.encode()).hexdigest()
        else:
            raise RuntimeError(f'Unsupported source entry: {relative}')
        files[target] = {'sha256': digest, 'mode': oct(info.mode), 'type': info.type.decode()}

archive_path = destination / f'{name}.tar.gz'
with tarfile.open(archive_path, 'w:gz', dereference=False) as archive:
    add_repository(archive, source, name)
manifest = {
    'source_root': str(source),
    'head': git(source, 'rev-parse', 'HEAD').decode().strip(),
    'archive_sha256': hashlib.sha256(archive_path.read_bytes()).hexdigest(),
    'files': files,
}
(destination / f'{name}.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(f'Prepared {name}: {len(files)} source files, archive SHA256 {manifest["archive_sha256"]}')
ARCHIVE
}

write_source_archive "$REPO_ROOT" LeanFlow
if [[ -d "$COMPANION_ROOT" ]]; then
    write_source_archive "$COMPANION_ROOT" LeanProbe
elif [[ -n "${LEANFLOW_LEAN_PROBE_SOURCE:-}" ]]; then
    echo "ERROR: explicit LeanProbe source does not exist: $COMPANION_ROOT" >&2
    exit 1
else
    echo "==> No adjacent LeanProbe source; installer will download its pinned companion source"
fi
if [[ -n "${LEANFLOW_SMOKE_EVIDENCE_DIR:-}" ]]; then
    mkdir -p "$LEANFLOW_SMOKE_EVIDENCE_DIR"
    cp "$ARCHIVE_DIR"/*.json "$LEANFLOW_SMOKE_EVIDENCE_DIR/"
fi

echo "==> Building $IMAGE_TAG"
docker build -t "$IMAGE_TAG" -f "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR"

echo "==> Running ubuntu_repository_local_install_smoke"
docker run --rm \
    -e SOURCE_ARCHIVE_DIR=/source-archives \
    -v "$ARCHIVE_DIR:/source-archives:ro" \
    "$IMAGE_TAG" \
    bash -lc 'bootstrap="$(mktemp -d /tmp/leanflow-smoke-bootstrap.XXXXXX)"; tar -xzf /source-archives/LeanFlow.tar.gz -C "$bootstrap" LeanFlow/tests/installer/ubuntu_repository_local_install_smoke/run-in-container.sh; exec bash "$bootstrap/LeanFlow/tests/installer/ubuntu_repository_local_install_smoke/run-in-container.sh"'
