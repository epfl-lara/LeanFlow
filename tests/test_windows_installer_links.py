"""Verify Windows bootstrap targets and the generated WSL installer script."""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_windows_installers_do_not_reference_legacy_repositories():
    install_ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    install_cmd = (REPO_ROOT / "scripts" / "install.cmd").read_text(encoding="utf-8")

    for source in (install_ps1, install_cmd):
        assert "morph-labs/gauss-agent" not in source
        assert "NousResearch/gauss-agent" not in source


def test_windows_installers_use_current_bootstrap_targets():
    install_ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    install_cmd = (REPO_ROOT / "scripts" / "install.cmd").read_text(encoding="utf-8")

    assert '$DefaultRepoUrl = "https://github.com/epfl-lara/LeanFlow.git"' in install_ps1
    assert (
        'powershell -ExecutionPolicy ByPass -NoProfile -File "%~dp0install.ps1" %*' in install_cmd
    )


@pytest.mark.parametrize("missing_python", [False, True])
def test_wsl_bootstrap_runs_direct_installer_and_bootstraps_python(
    tmp_path: Path, missing_python: bool
) -> None:
    """Execute the generated WSL shell script with isolated prerequisite commands."""
    source = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    generated = source.split("function Build-InstallScript", 1)[1]
    generated = generated.split("return @'\n", 1)[1].split("\n'@", 1)[0]
    script = tmp_path / "bootstrap.sh"
    script.write_text(generated, encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    command_log = tmp_path / "commands.txt"
    package_marker = tmp_path / "packages-installed"
    fake_commands = {
        "git": "exit 0\n",
        "python3": (
            f'if [[ -f "$PACKAGE_MARKER" ]]; then exit 0; fi\nexit {int(missing_python)}\n'
        ),
        "id": "printf '0\\n'\n",
        "apt-get": (
            'printf \'apt-get %s\\n\' "$*" >> "$COMMAND_LOG"\n'
            'if [[ "$1" == "install" ]]; then touch "$PACKAGE_MARKER"; fi\n'
        ),
    }
    for name, body in fake_commands.items():
        command = fake_bin / name
        command.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8")
        command.chmod(0o755)

    home = tmp_path / "home"
    checkout = home / "LeanFlow"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "scripts").mkdir()
    installer = checkout / "scripts" / "install-internal.sh"
    installer.write_text(
        '#!/usr/bin/env bash\nprintf \'installer %s\\n\' "$PWD" >> "$COMMAND_LOG"\n',
        encoding="utf-8",
    )
    installer.chmod(0o755)

    environment = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "COMMAND_LOG": str(command_log),
        "PACKAGE_MARKER": str(package_marker),
    }
    result = subprocess.run(
        ["bash", str(script), "https://example.test/LeanFlow.git", "main", "~/LeanFlow", "0"],
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    commands = command_log.read_text(encoding="utf-8").splitlines()
    assert commands[-1] == f"installer {checkout}"
    if missing_python:
        assert commands[:2] == [
            "apt-get update",
            "apt-get install -y git python3 python3-venv python3-pip",
        ]
    else:
        assert commands == [f"installer {checkout}"]
