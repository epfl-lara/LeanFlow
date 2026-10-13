"""Check that public installation delegates to the supported local installer."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "arguments",
    [[], ["--no-editable"], ["--bin-dir", "bin $literal with spaces", "--non-interactive"]],
)
@pytest.mark.parametrize("exit_code", [0, 7])
def test_install_wrapper_forwards_exact_arguments_environment_and_exit(
    tmp_path, arguments, exit_code
):
    """Preserve caller inputs and failures without installing a second runner."""
    scripts = tmp_path / "source with spaces/scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts/install.sh", scripts / "install.sh")
    (scripts / "install-internal.sh").write_text(
        "#!/usr/bin/env bash\n"
        f"{sys.executable} - \"$@\" <<'CHECK'\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['WRAPPER_LOG']).write_text(json.dumps({'args':sys.argv[1:], 'home':os.environ['LEANFLOW_HOME']}))\n"
        "CHECK\n"
        f"exit {exit_code}\n"
    )
    (scripts / "install-internal.sh").chmod(0o755)
    log = tmp_path / "result.json"
    state = tmp_path / "private state"
    result = subprocess.run(
        ["bash", str(scripts / "install.sh"), *arguments],
        env={**os.environ, "WRAPPER_LOG": str(log), "LEANFLOW_HOME": str(state)},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == exit_code, result.stderr
    assert json.loads(log.read_text()) == {"args": arguments, "home": str(state)}
    assert not (scripts.parent / ".leanflow-installer-venv").exists()
    assert not state.exists()
