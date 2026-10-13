"""Exercise isolated installer publication without downloads or native model calls."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_executable(path: Path, source: str) -> None:
    """Create an executable shell fixture."""
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + source, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.parametrize("system_bubblewrap", [False, True])
def test_installed_tools_still_probe_sandbox_and_prefer_system_bubblewrap(
    tmp_path: Path, system_bubblewrap: bool
) -> None:
    """Detect namespace restrictions even when no tools need installation."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    operations = tmp_path / "sandbox-probes.txt"
    _write_executable(binaries / "uname", "printf 'Linux\\n'\n")
    for name in ("rg", "pdftotext", "pdfinfo", "pdfimages"):
        _write_executable(binaries / name, "exit 0\n")
    fallback = tmp_path / "fallback-bwrap"
    system = tmp_path / "system-bwrap"
    for executable in (fallback, system) if system_bubblewrap else (fallback,):
        _write_executable(
            executable,
            'if [[ "$1" == "--version" ]]; then exit 0; fi\n'
            'printf \'%s\\n\' "$0 $*" >> "$SANDBOX_PROBES"\n'
            "exit 1\n",
        )
    (binaries / "bwrap").symlink_to(fallback)
    # Exercise the installer functions without publishing a Python environment.
    definitions = (
        (REPO_ROOT / "scripts/install-internal.sh")
        .read_text()
        .split("\nwhile [[ $# -gt 0 ]]", 1)[0]
    )
    definitions = definitions.replace("/usr/bin/bwrap", str(system))
    script = tmp_path / "probe-installer.sh"
    script.write_text(definitions + "\nensure_external_cli_tools\n")
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "LEANFLOW_HOME": str(state),
            "LEANFLOW_BIN_DIR": str(binaries),
            "LEANFLOW_INSTALL_OS_TOOLS": "1",
            "SANDBOX_PROBES": str(operations),
        },
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "external tools wired into PATH" in result.stdout
    assert "Bubblewrap cannot create the required sandbox" in result.stdout
    assert "installing bubblewrap alone may not suffice" in result.stdout
    probes = operations.read_text().splitlines()
    assert len(probes) == 1
    assert "--unshare-user" in probes[0] and "--unshare-net" in probes[0]
    assert (binaries / "bwrap").resolve() == (system if system_bubblewrap else fallback)


def test_local_installer_preserves_config_and_publishes_immutable_runtimes(
    tmp_path: Path,
) -> None:
    """Exercise real metadata and wrappers with only dependency installation stubbed."""
    source = tmp_path / "source with spaces"
    (source / "scripts").mkdir(parents=True)
    script = source / "scripts" / "install-internal.sh"
    script.write_bytes((REPO_ROOT / "scripts" / "install-internal.sh").read_bytes())
    state = tmp_path / "state with spaces"
    binaries = tmp_path / "bin $literal with spaces"
    binaries.mkdir()
    (binaries / "leanflow-agent").write_text("legacy wrapper\n", encoding="utf-8")
    fixture = tmp_path / "fixtures"
    fixture.mkdir()
    operations = tmp_path / "operations.txt"
    python = shlex.quote(sys.executable)
    _write_executable(
        fixture / "python",
        'if [[ "${1:-}" == "-c" ]]; then exit 0; fi\n'
        'if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then\n'
        '  mkdir -p "$3/bin"\n'
        '  cp "$INSTALL_FIXTURE/python" "$3/bin/python"\n'
        '  cp "$INSTALL_FIXTURE/leanflow" "$3/bin/leanflow"\n'
        '  printf \'export PATH=%q:"$PATH"\\n\' "$3/bin" > "$3/bin/activate"\n'
        "  printf 'venv\\n' >> \"$INSTALL_OPERATIONS\"\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then\n'
        "  printf 'pip\\n' >> \"$INSTALL_OPERATIONS\"\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "${1:-}" == "-m" && "${2:-}" == "leanflow_cli.runtime.lemex_install" ]]; then\n'
        '  if [[ "${3:-}" == "--refresh" && "${4:-}" == "--no-build" ]]; then\n'
        "    printf 'lemex-refresh\\n' >> \"$INSTALL_OPERATIONS\"\n"
        "  else\n"
        "    printf 'lemex\\n' >> \"$INSTALL_OPERATIONS\"\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "${1:-}" == "-m" && "${2:-}" == "leanflow_cli.runtime.runtime_install" '
        '&& "${3:-}" == "ensure-backends" ]]; then\n'
        "  printf 'backends\\n' >> \"$INSTALL_OPERATIONS\"\n"
        "  exit 0\n"
        "fi\n"
        f'exec {python} "$@"\n',
    )
    _write_executable(fixture / "leanflow", f'exec {python} -m leanflow_cli.main "$@"\n')
    environment = {
        **os.environ,
        "LEANFLOW_HOME": str(state),
        "LEANFLOW_INSTALL_PYTHON": str(fixture / "python"),
        "PYTHONPATH": str(REPO_ROOT),
        "INSTALL_FIXTURE": str(fixture),
        "INSTALL_OPERATIONS": str(operations),
    }
    environment.pop("LEANFLOW_VENV_DIR", None)
    command = [
        "bash",
        str(script),
        "--leanflow-home",
        str(state),
        "--bin-dir",
        str(binaries),
        "--skip-os-tools",
        "--skip-leanexplore-data",
    ]

    def install(*extra: str) -> subprocess.CompletedProcess[str]:
        """Run the isolated installer and retain diagnostics on failure."""
        result = subprocess.run(
            [*command, *extra],
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    install()
    metadata = dict(
        line.split("=", 1) for line in (state / "install-root").read_text().splitlines()
    )
    venv = Path(metadata["venv_dir"])
    assert metadata["repo_root"] == str(source)
    assert metadata["bin_dir"] == str(binaries)
    assert venv.parent.parent == state / "runtimes"
    assert json.loads((venv.parent / ".leanflow-storage.json").read_text())["ready"] is True
    assert not (binaries / "leanflow-agent").exists()
    previous_python = (venv / "bin" / "python").read_bytes()
    with (state / "config.yaml").open("a", encoding="utf-8") as stream:
        stream.write("\n# custom installer marker\n")
    with (state / ".env").open("a", encoding="utf-8") as stream:
        stream.write("\nCUSTOM_INSTALLER_SETTING=preserved\n")

    repeated = install()
    assert "retained installed package without modifying its environment" in repeated.stdout
    assert f"venv_dir={venv}\n" in (state / "install-root").read_text()
    assert "# custom installer marker" in (state / "config.yaml").read_text()
    assert "CUSTOM_INSTALLER_SETTING=preserved" in (state / ".env").read_text()
    assert operations.read_text().splitlines() == [
        "venv",
        "pip",
        "pip",
        "lemex",
        "lemex-refresh",
        "backends",
        "lemex",
        "lemex-refresh",
        "backends",
    ]
    cli = subprocess.run(
        [str(binaries / "leanflow"), "--help"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert cli.returncode == 0, cli.stderr
    assert "Lean-first AI automation" in cli.stdout

    install("--recreate-venv")
    assert f"venv_dir={venv}\n" not in (state / "install-root").read_text()
    assert (venv / "bin" / "python").read_bytes() == previous_python
    assert "# custom installer marker" in (state / "config.yaml").read_text()
    assert "CUSTOM_INSTALLER_SETTING=preserved" in (state / ".env").read_text()


@pytest.mark.parametrize("missing_venv", [False, True])
def test_local_installer_rejects_python_prerequisites_before_creating_paths(
    tmp_path: Path, missing_venv: bool
) -> None:
    """Refuse old Python and missing venv support before installation changes."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import builtins, sys\n"
        "from collections import namedtuple\n"
        "Version = namedtuple('Version', 'major minor micro releaselevel serial')\n"
        f"sys.version_info = Version(3, {12 if missing_venv else 11}, 0, 'final', 0)\n"
        "original_import = builtins.__import__\n"
        "def import_module(name, *args, **kwargs):\n"
        f"    if {missing_venv!r} and name == 'venv':\n"
        "        raise ModuleNotFoundError('No module named venv')\n"
        "    return original_import(name, *args, **kwargs)\n"
        "builtins.__import__ = import_module\n"
        "exec(sys.argv[1])\n",
        encoding="utf-8",
    )
    interpreter = tmp_path / "selected-python"
    _write_executable(
        interpreter,
        'if [[ "${1:-}" == "-c" ]]; then\n'
        f'  exec {shlex.quote(sys.executable)} {shlex.quote(str(probe))} "$2"\n'
        "fi\nexit 99\n",
    )
    state, binaries, venv = (tmp_path / name for name in ("state", "bin", "venv"))
    result = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "scripts" / "install-internal.sh"),
            "--python",
            str(interpreter),
            "--leanflow-home",
            str(state),
            "--bin-dir",
            str(binaries),
            "--venv-dir",
            str(venv),
        ],
        env={**os.environ, "LEANFLOW_HOME": str(state)},
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "requires Python 3.12+ with venv support" in result.stderr
    assert str(interpreter) in result.stderr
    assert "--python PATH" in result.stderr
    assert not state.exists()
    assert not binaries.exists()
    assert not venv.exists()
