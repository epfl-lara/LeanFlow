"""Preserve literal custom wrapper paths without installing container dependencies."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_sandbox_wrapper_keeps_literal_paths_and_arguments(tmp_path):
    """Avoid expanding dollar signs and quotes from a configured command path."""
    scripts = tmp_path / "source/scripts"
    scripts.mkdir(parents=True)
    root = Path(__file__).resolve().parents[2]
    shutil.copy2(root / "scripts/install-sandbox.sh", scripts / "install-sandbox.sh")
    internal = scripts / "install-internal.sh"
    internal.write_text("#!/usr/bin/env bash\nexit 0\n")
    internal.chmod(0o755)
    binaries = tmp_path / "bin $literal with 'quotes'"
    binaries.mkdir()
    cli = binaries / "leanflow"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\nfrom pathlib import Path\n"
        "Path(os.environ['SANDBOX_LOG']).write_text(json.dumps({'args':sys.argv[1:],'home':os.environ['LEANFLOW_HOME']}))\n"
    )
    cli.chmod(0o755)
    home = tmp_path / "state with 'quotes'"
    env = dict(os.environ, SANDBOX_LOG=str(tmp_path / "result.json"))
    env.pop("LEANFLOW_HOME", None)
    result = subprocess.run(
        [
            "bash",
            str(scripts / "install-sandbox.sh"),
            "--no-build",
            "--bin-dir",
            str(binaries),
            "--leanflow-home",
            str(home),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    arguments = ["workflow", "prove", "file $literal with spaces.lean"]
    result = subprocess.run(
        [str(binaries / "leanflow-sandbox"), *arguments],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "result.json").read_text()) == {
        "args": ["sandbox", "run", "--", *arguments],
        "home": str(home),
    }
