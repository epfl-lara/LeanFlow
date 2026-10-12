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
        "  printf 'lemex\\n' >> \"$INSTALL_OPERATIONS\"\n"
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
        "backends",
        "lemex",
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
