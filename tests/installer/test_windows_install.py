"""Exercise WSL bootstrap contracts with real PowerShell and isolated native fixtures."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

FAKE_WSL = r"""
import base64, json, os, subprocess, sys
from pathlib import Path
configuration = json.loads(Path(os.environ['WSL_FIXTURE_CONFIG']).read_text())
args = sys.argv[1:]
with Path(os.environ['WSL_FIXTURE_CALLS']).open('a') as stream:
    stream.write(json.dumps(args) + '\n')
if args[:2] == ['--list', '--quiet']:
    if configuration.get('utf16_lists'):
        sys.stdout.reconfigure(encoding='utf-16-le')
        sys.stderr.reconfigure(encoding='utf-16-le')
    print(configuration.get('list_stdout', ''), end='')
    print(configuration.get('list_stderr', ''), end='', file=sys.stderr)
    if configuration.get('list_exit'):
        raise SystemExit(configuration['list_exit'])
    for item in configuration['distros']:
        name = item['name']
        print(''.join(character + '\0' for character in name) if configuration.get('nul_output') else name)
elif args[:2] == ['--list', '--verbose']:
    if configuration.get('utf16_lists'):
        sys.stdout.reconfigure(encoding='utf-16-le')
        sys.stderr.reconfigure(encoding='utf-16-le')
    print(configuration.get('metadata_stderr', ''), end='', file=sys.stderr)
    if configuration.get('metadata_exit'):
        raise SystemExit(configuration['metadata_exit'])
    print('NAME STATE VERSION')
    for item in reversed(configuration['distros']):
        prefix = '* ' if item.get('default') else '  '
        line = prefix + item['name'] + '    En ejecución    ' + str(item['version'])
        print(''.join(character + '\0' for character in line) if configuration.get('nul_output') else line)
elif args[:1] == ['--install']:
    print(configuration.get('install_stderr', ''), end='', file=sys.stderr)
    raise SystemExit(configuration.get('install_exit', 0))
else:
    distro = args[1] if args[:1] == ['-d'] else ''
    command = args[2:] if distro else args
    if command[:2] == ['--exec', 'wslpath']:
        assert os.environ.get('WSL_UTF8') == '1'
        if configuration.get('path_exit'):
            print('fixture path translation failed', file=sys.stderr)
            raise SystemExit(configuration['path_exit'])
        print(command[-1])
    elif command == ['--exec', 'bash', '--noprofile', '--norc', '-c', 'printf ok:; id -u']:
        ready = next((item.get('ready', True) for item in configuration['distros'] if item['name'] == distro), False)
        if not ready:
            print('execvpe(bash) failed: No such file or directory', file=sys.stderr)
            raise SystemExit(1)
        uid = next(item.get('uid', 1000) for item in configuration['distros'] if item['name'] == distro)
        print('ok:' + str(uid))
    elif command[:2] == ['--exec', 'bash']:
        if configuration.get('require_terminal'):
            assert sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty(), 'WSL terminal streams were redirected'
            print('WSL_FIXTURE_PROMPT', flush=True)
            assert sys.stdin.readline().strip() == 'fixture-terminal-input', 'Interactive input was lost'
        payload = [base64.b64decode(value).decode()[1:] for value in command[3:]]
        Path(os.environ['WSL_FIXTURE_PAYLOAD']).write_text(json.dumps(payload))
        Path(os.environ['WSL_FIXTURE_BOOTSTRAP']).write_bytes(Path(command[2]).read_bytes())
        if configuration.get('execute_bootstrap'):
            environment = {**os.environ, 'HOME': os.environ['WSL_FIXTURE_HOME']}
            raise SystemExit(subprocess.run(['bash', *command[2:]], env=environment, check=False).returncode)
        print(configuration.get('bootstrap_stderr', ''), end='', file=sys.stderr)
        raise SystemExit(configuration.get('bootstrap_exit', 0))
    else:
        raise SystemExit('unexpected WSL arguments: ' + repr(args))
"""

FAKE_GIT = r"""
import json, os, sys
from pathlib import Path
configuration = json.loads(Path(os.environ['WSL_FIXTURE_CONFIG']).read_text())
args = sys.argv[1:]
with Path(os.environ['WSL_FIXTURE_GIT_CALLS']).open('a') as stream:
    stream.write(json.dumps(args) + '\n')
command = args[2:] if args[:1] == ['-C'] else args
if command[:1] == ['rev-parse']:
    print('unpublished-current')
elif command[:1] == ['ls-remote']:
    raise SystemExit(0 if configuration.get('published_branch', True) else 2)
elif command[:1] == ['clone']:
    target = Path(command[-1])
    (target / '.git').mkdir(parents=True)
    (target / 'scripts').mkdir()
    internal = target / 'scripts' / 'install-internal.sh'
    internal.write_text('#!/usr/bin/env bash\nexec "$WSL_FIXTURE_PYTHON" "$WSL_FIXTURE_INTERNAL_CAPTURE" "$@"\n')
    internal.chmod(0o755)
elif command[:1] == ['status']:
    if configuration.get('status_exit'):
        raise SystemExit(configuration['status_exit'])
    print(configuration.get('dirty', ''), end='')
elif command[:1] == ['merge-base']:
    if configuration.get('divergent_head') and command[-2] == 'HEAD':
        raise SystemExit(1)
    if configuration.get('divergent_selected_branch') and command[-2].startswith('refs/heads/'):
        raise SystemExit(1)
"""


def _write_executable(path: Path, body: str) -> None:
    """Create an isolated Python command used only by the native-boundary harness."""
    path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _run_with_terminal(
    command: list[str], environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run the PowerShell/native boundary with a real local pseudo-terminal."""
    pty = pytest.importorskip("pty")
    select = pytest.importorskip("select")
    master, slave = pty.openpty()
    process = subprocess.Popen(command, env=environment, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = bytearray()
    input_sent = False
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
                if not input_sent and b"WSL_FIXTURE_PROMPT" in output:
                    os.write(master, b"fixture-terminal-input\n")
                    input_sent = True
            elif process.poll() is not None:
                break
        else:
            raise subprocess.TimeoutExpired(command, 30, bytes(output))
        return subprocess.CompletedProcess(
            command, process.wait(timeout=5), output.decode("utf-8", errors="replace"), ""
        )
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture
def powershell() -> str:
    """Choose an explicitly supplied runtime or skip where PowerShell is unavailable."""
    executable = os.environ.get("LEANFLOW_TEST_POWERSHELL") or shutil.which("pwsh")
    if not executable:
        pytest.skip("Set LEANFLOW_TEST_POWERSHELL to run real PowerShell boundary contracts")
    return executable


@pytest.fixture
def windows_harness(tmp_path: Path, powershell: str) -> Any:
    """Run the real bootstrap with fake WSL/Git and a scratch Linux-side installer."""
    binaries = tmp_path / "native fixtures"
    binaries.mkdir()
    paths = {
        name: tmp_path / f"{name}.json"
        for name in ("config", "calls", "payload", "git_calls", "installed")
    }
    bootstrap = tmp_path / "bootstrap.sh"
    scratch_home = tmp_path / "linux home"
    scratch_home.mkdir()
    temporary = tmp_path / "Windows temp Jos\u00e9 O'Brien \u65e5\u672c with $literal `tick`"
    temporary.mkdir()
    _write_executable(binaries / "wsl.exe", FAKE_WSL)
    _write_executable(binaries / "git", FAKE_GIT)
    _write_executable(
        binaries / "python3.13",
        "import os, sys\nos.execv(sys.executable, [sys.executable, *sys.argv[1:]])",
    )
    internal_capture = tmp_path / "capture_internal.py"
    internal_capture.write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "configuration=json.loads(Path(os.environ['WSL_FIXTURE_CONFIG']).read_text())\n"
        "if sys.argv[1:] == ['--help']:\n"
        "    print(' '.join(configuration.get('installer_flags', ['--non-interactive','--lean-probe-source','--with-lemex'])))\n"
        "    raise SystemExit(configuration.get('installer_help_exit',0))\n"
        "Path(os.environ['WSL_FIXTURE_INSTALLED']).write_text(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "TEMP": str(temporary),
        "WSL_FIXTURE_CONFIG": str(paths["config"]),
        "WSL_FIXTURE_CALLS": str(paths["calls"]),
        "WSL_FIXTURE_PAYLOAD": str(paths["payload"]),
        "WSL_FIXTURE_BOOTSTRAP": str(bootstrap),
        "WSL_FIXTURE_GIT_CALLS": str(paths["git_calls"]),
        "WSL_FIXTURE_INSTALLED": str(paths["installed"]),
        "WSL_FIXTURE_HOME": str(scratch_home),
        "WSL_FIXTURE_PYTHON": sys.executable,
        "WSL_FIXTURE_INTERNAL_CAPTURE": str(internal_capture),
        "WSL_FIXTURE_SCRIPT": str(REPO_ROOT / "scripts/install.ps1"),
        "WSL_UTF8": "parent-sentinel",
    }

    def run(configuration: dict[str, Any], *arguments: str) -> dict[str, Any]:
        """Execute one bootstrap invocation without touching a real distribution."""
        paths["config"].write_text(json.dumps(configuration))
        for name in ("calls", "payload", "git_calls", "installed"):
            paths[name].unlink(missing_ok=True)
        script_path = REPO_ROOT / "scripts/install.ps1"
        if any(
            configuration.get(name)
            for name in ("legacy_arguments", "native_error_stop", "ascii_console")
        ):
            script_path = tmp_path / "invoke.ps1"
            preferences = []
            if configuration.get("legacy_arguments"):
                preferences.append("$PSNativeCommandArgumentPassing = 'Legacy'")
            if configuration.get("native_error_stop"):
                preferences.append("$PSNativeCommandUseErrorActionPreference = $true")
            if configuration.get("ascii_console"):
                preferences.append("[Console]::OutputEncoding = [System.Text.Encoding]::ASCII")
            script_path.write_text(
                "\n".join(preferences) + "\n$encodingBefore = [Console]::OutputEncoding.WebName\n"
                "$nativeBefore = $PSNativeCommandUseErrorActionPreference\n"
                "& $env:WSL_FIXTURE_SCRIPT @args\n$exitCode = $LASTEXITCODE\n"
                "if ([Console]::OutputEncoding.WebName -ne $encodingBefore -or "
                "$PSNativeCommandUseErrorActionPreference -ne $nativeBefore -or "
                "$env:WSL_UTF8 -ne 'parent-sentinel') { throw 'Caller preferences changed' }\n"
                "exit $exitCode\n"
            )
        command = [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script_path),
            *arguments,
        ]
        result = (
            _run_with_terminal(command, environment)
            if configuration.get("require_terminal")
            else subprocess.run(
                command,
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        )
        return {
            "result": result,
            "calls": [json.loads(line) for line in paths["calls"].read_text().splitlines()],
            "payload": (
                json.loads(paths["payload"].read_text()) if paths["payload"].exists() else None
            ),
            "git_calls": (
                [json.loads(line) for line in paths["git_calls"].read_text().splitlines()]
                if paths["git_calls"].exists()
                else []
            ),
            "installed": (
                json.loads(paths["installed"].read_text()) if paths["installed"].exists() else None
            ),
            "home": scratch_home,
            "binaries": binaries,
            "temporary": temporary,
            "bootstrap": bootstrap,
        }

    return run


def _distro(name: str = "Ubuntu", version: int = 2, **extra: Any) -> dict[str, Any]:
    """Describe one installed distribution at the fake Windows boundary."""
    return {"name": name, "version": version, **extra}


def test_powershell_parser_accepts_windows_script(powershell: str) -> None:
    """Parse the actual source using PowerShell's own language parser."""
    source = str(REPO_ROOT / "scripts/install.ps1").replace("'", "''")
    command = (
        "$tokens=$null; $errors=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{source}',[ref]$tokens,[ref]$errors)>$null; "
        "$errors | ForEach-Object { $_.ToString() }; if($errors.Count){exit 1}"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_stderr_error_records_use_exit_code_and_preserve_outer_preferences(
    powershell: str, tmp_path: Path
) -> None:
    """Model pre-7.2 native stderr records with real PowerShell nonterminating errors."""
    script_path = tmp_path / "legacy-error-records.ps1"
    source = str(REPO_ROOT / "scripts/install.ps1").replace("'", "''")
    script_path.write_text(
        f"$source = [System.IO.File]::ReadAllText('{source}')\n"
        '$source = $source.Substring(0, $source.LastIndexOf("`ntry {`n    Main"))\n'
        ". ([scriptblock]::Create($source))\n"
        "$PSNativeCommandUseErrorActionPreference = $true\n"
        "function wsl.exe {\n"
        "    $global:LASTEXITCODE = 1\n"
        "    Write-Error 'Windows Subsystem for Linux has no installed distributions.'\n"
        "}\n"
        "if (@(Get-InstalledWSLDistros).Count -ne 0) { throw 'Expected empty distro state' }\n"
        "if (Test-WSLBash -TargetDistro docker-desktop) { throw 'Expected failed bash probe' }\n"
        "if ($ErrorActionPreference -ne 'Stop' -or "
        "-not $PSNativeCommandUseErrorActionPreference) { throw 'Outer preferences changed' }\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-File", str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_utf16le_quiet_and_verbose_native_listing_outputs(windows_harness: Any) -> None:
    """Exercise WSL's legacy no-BOM listing byte stream using the native fixture."""
    result = windows_harness(
        {"distros": [_distro("Debian")], "utf16_lists": True, "native_error_stop": True}
    )
    assert result["result"].returncode == 0, result["result"].stdout + result["result"].stderr
    assert result["calls"][-1][:2] == ["-d", "Debian"]


def test_single_non_ubuntu_distro_uses_its_full_name(windows_harness: Any) -> None:
    result = windows_harness({"distros": [_distro("Debian")]})
    assert result["result"].returncode == 0, result["result"].stdout + result["result"].stderr
    assert result["calls"][-1][:2] == ["-d", "Debian"]


@pytest.mark.parametrize("old_name,new_name", [("Ubuntu", "Debian"), ("Debian", "Ubuntu")])
def test_name_matched_version_records_choose_ready_wsl2(
    windows_harness: Any, old_name: str, new_name: str
) -> None:
    result = windows_harness(
        {"distros": [_distro(old_name, 1, default=True), _distro(new_name)], "nul_output": True}
    )
    assert result["result"].returncode == 0, result["result"].stdout + result["result"].stderr
    assert result["calls"][-1][:2] == ["-d", new_name]
    assert not any(call[:2] == ["-d", old_name] for call in result["calls"])


def test_explicit_wsl1_distro_is_rejected_without_conversion(windows_harness: Any) -> None:
    result = windows_harness(
        {"distros": [_distro("Ubuntu", 1), _distro("Debian")]}, "-Distro", "Ubuntu"
    )
    assert result["result"].returncode == 1 and "uses WSL 1" in result["result"].stdout
    assert not any(call[:1] in (["--install"], ["--set-version"]) for call in result["calls"])
    assert result["payload"] is None


def test_uninitialized_default_falls_through_to_other_ready_wsl2(windows_harness: Any) -> None:
    result = windows_harness(
        {"distros": [_distro("Ubuntu", default=True, ready=False), _distro("Debian")]}
    )
    assert result["result"].returncode == 0
    assert result["calls"][-1][:2] == ["-d", "Debian"]


def test_auto_root_distro_requires_normal_user_initialization(windows_harness: Any) -> None:
    result = windows_harness({"distros": [_distro(uid=0)], "native_error_stop": True})
    assert result["result"].returncode == 1
    assert "currently runs as root" in result["result"].stdout
    assert "Windows Start menu" in result["result"].stdout
    assert "finish Linux user setup" in result["result"].stdout
    assert result["payload"] is None and result["git_calls"] == []
    assert not any(call[:1] == ["--install"] for call in result["calls"])


def test_auto_root_distro_falls_through_to_ready_normal_user(windows_harness: Any) -> None:
    result = windows_harness(
        {"distros": [_distro(default=True, uid=0), _distro("Debian", uid=1001)]}
    )
    assert result["result"].returncode == 0, result["result"].stdout + result["result"].stderr
    assert "currently runs as root" in result["result"].stdout
    assert result["calls"][-1][:2] == ["-d", "Debian"]


def test_explicit_root_distro_is_an_intentional_install(windows_harness: Any) -> None:
    result = windows_harness({"distros": [_distro(uid=0)]}, "-Distro", "Ubuntu")
    assert result["result"].returncode == 0, result["result"].stdout + result["result"].stderr
    assert result["payload"] is not None
    assert "currently runs as root" not in result["result"].stdout


@pytest.mark.parametrize("uid", ["", "not-a-uid", "1000\nextra"])
def test_malformed_identity_probe_is_not_admitted(windows_harness: Any, uid: str) -> None:
    result = windows_harness({"distros": [_distro(uid=uid)]})
    assert result["result"].returncode == 1
    assert result["payload"] is None


def test_interactive_wsl_launch_inherits_terminal_and_receives_input(windows_harness: Any) -> None:
    """Validate a real terminal boundary without claiming a real WSL/sudo execution."""
    result = windows_harness(
        {"distros": [_distro()], "require_terminal": True, "native_error_stop": True}
    )
    assert result["result"].returncode == 0, result["result"].stdout
    assert "WSL_FIXTURE_PROMPT" in result["result"].stdout
    assert result["payload"] is not None
    assert not list(result["temporary"].glob("leanflow-wsl-bootstrap-*"))


def test_clean_wsl_bootstrap_installs_without_launch_then_requires_rerun(
    windows_harness: Any,
) -> None:
    result = windows_harness({"distros": []})
    assert result["result"].returncode == 1
    assert ["--install", "-d", "Ubuntu", "--no-launch"] in result["calls"]
    assert "initialize 'Ubuntu', then rerun" in result["result"].stdout
    assert result["payload"] is None


@pytest.mark.parametrize("stream", ["list_stdout", "list_stderr"])
@pytest.mark.parametrize(
    "diagnostic",
    [
        "Windows Subsystem for Linux has no installed distributions.\n"
        "Use 'wsl.exe --install <Distro>' to install.\n",
        "Error code: Wsl/WSL_E_DEFAULT_DISTRO_NOT_FOUND\n",
    ],
)
def test_nonzero_no_distributions_diagnostic_reaches_initialization(
    windows_harness: Any, stream: str, diagnostic: str
) -> None:
    """Recognize only the known empty-state diagnostic, including stderr failures."""
    result = windows_harness(
        {"distros": [], "list_exit": 255, stream: diagnostic, "native_error_stop": True}
    )
    assert result["result"].returncode == 1
    assert ["--install", "-d", "Ubuntu", "--no-launch"] in result["calls"]
    assert "initialize 'Ubuntu', then rerun" in result["result"].stdout
    assert ".\\scripts\\install.cmd" in result["result"].stdout
    assert result["payload"] is None


def test_nonzero_empty_state_obeys_noninteractive_setup_boundary(windows_harness: Any) -> None:
    result = windows_harness(
        {
            "distros": [],
            "list_exit": 255,
            "list_stderr": "Windows Subsystem for Linux has no installed distributions.\n",
            "native_error_stop": True,
        },
        "-NonInteractive",
    )
    assert result["result"].returncode == 1 and "needs setup" in result["result"].stdout
    assert result["calls"] == [["--list", "--quiet"]]


@pytest.mark.parametrize(
    "diagnostic",
    ["The WSL service is unavailable.\n", "WSL optional component is disabled: 0x8007019e\n"],
)
def test_other_list_failures_preserve_diagnostic_without_installing(
    windows_harness: Any, diagnostic: str
) -> None:
    result = windows_harness(
        {
            "distros": [],
            "list_exit": 17,
            "list_stderr": diagnostic,
            "native_error_stop": True,
        }
    )
    assert result["result"].returncode == 1
    assert diagnostic.strip() in result["result"].stdout
    assert "Check wsl --status" in result["result"].stdout
    assert result["calls"] == [["--list", "--quiet"]]


def test_stderr_probe_failure_falls_through_under_strict_native_preferences(
    windows_harness: Any,
) -> None:
    result = windows_harness(
        {
            "distros": [_distro("docker-desktop", default=True, ready=False), _distro()],
            "native_error_stop": True,
            "legacy_arguments": True,
        }
    )
    assert result["result"].returncode == 0, result["result"].stdout + result["result"].stderr
    assert result["calls"][-1][:2] == ["-d", "Ubuntu"]
    assert all("--exec" in call for call in result["calls"] if call[:1] == ["-d"])


def test_utf8_path_capture_preserves_username_under_ascii_console(windows_harness: Any) -> None:
    result = windows_harness(
        {"distros": [_distro()], "ascii_console": True, "legacy_arguments": True}
    )
    assert result["result"].returncode == 0, result["result"].stdout + result["result"].stderr
    translated = next(call[-1] for call in result["calls"] if "wslpath" in call)
    assert "Jos\u00e9 O'Brien \u65e5\u672c" in translated
    assert result["calls"][-1][4] == translated
    assert not list(result["temporary"].glob("leanflow-wsl-bootstrap-*"))


@pytest.mark.parametrize(
    "failure,expected",
    [
        ({"metadata_exit": 7, "metadata_stderr": "metadata failed\n"}, "metadata failed"),
        ({"bootstrap_exit": 23, "bootstrap_stderr": "install failed\n"}, "exited with code 23"),
    ],
)
def test_native_stderr_failures_report_exit_status_before_cleanup(
    windows_harness: Any, failure: dict[str, Any], expected: str
) -> None:
    result = windows_harness({"distros": [_distro()], "native_error_stop": True, **failure})
    assert result["result"].returncode == 1
    assert expected in result["result"].stdout
    assert not list(result["temporary"].glob("leanflow-wsl-bootstrap-*"))


def test_windows_powershell_source_needs_no_unicode_bom() -> None:
    """Keep the source readable by Windows PowerShell 5.1's legacy file decoder."""
    source = (REPO_ROOT / "scripts/install.ps1").read_bytes()
    assert source.isascii()


def test_noninteractive_uninitialized_wsl_stops_before_elevation_or_first_run(
    windows_harness: Any,
) -> None:
    result = windows_harness({"distros": []}, "-NonInteractive")
    assert result["result"].returncode == 1 and "needs setup" in result["result"].stdout
    assert result["calls"] == [["--list", "--quiet"]]


@pytest.mark.parametrize("failure", [{"list_exit": 17}, {"path_exit": 19}, {"bootstrap_exit": 23}])
def test_native_failures_are_reported_and_temporary_files_removed(
    windows_harness: Any, failure: dict[str, Any]
) -> None:
    result = windows_harness({"distros": [_distro()], **failure})
    assert result["result"].returncode == 1
    assert not list(result["temporary"].glob("leanflow-wsl-bootstrap-*"))
    assert "LeanFlow is installed in WSL" not in result["result"].stdout


def test_payload_round_trip_preserves_paths_quotes_dollars_and_backticks(
    windows_harness: Any,
) -> None:
    target = "~/project with 'single' \"double\" $literal `tick`/LeanFlow"
    companion = "~/LeanProbe 'single' \"double\" $literal `tick`"
    result = windows_harness(
        {"distros": [_distro()], "execute_bootstrap": True},
        "-LinuxRepoDir",
        target,
        "-LeanProbeSource",
        companion,
        "-NonInteractive",
        "-Branch",
        "published",
    )
    assert result["result"].returncode == 0, result["result"].stdout + result["result"].stderr
    assert result["payload"] == [
        "https://github.com/epfl-lara/LeanFlow.git",
        "published",
        target,
        "0",
        "",
        "0",
        "1",
        companion,
        "0",
    ]
    assert all(result["calls"][-1][index] for index in range(5, len(result["calls"][-1])))
    assert result["installed"] == [
        "--python",
        "python3.13",
        "--non-interactive",
        "--lean-probe-source",
        str(result["home"] / companion[2:]),
    ]
    clone = next(call for call in result["git_calls"] if call[:1] == ["clone"])
    assert clone[-1] == str(result["home"] / target[2:])
    assert not list(result["temporary"].glob("leanflow-wsl-bootstrap-*"))


def test_explicit_missing_branch_is_not_silently_replaced(windows_harness: Any) -> None:
    result = windows_harness(
        {"distros": [_distro()], "execute_bootstrap": True, "published_branch": False},
        "-Branch",
        "typo",
    )
    assert (
        result["result"].returncode == 1
        and "requested branch is unavailable: typo" in result["result"].stdout
    )
    assert not any(call[:1] == ["clone"] for call in result["git_calls"])


def test_automatic_unpublished_branch_falls_back_to_main(windows_harness: Any) -> None:
    result = windows_harness(
        {"distros": [_distro()], "execute_bootstrap": True, "published_branch": False}
    )
    assert result["result"].returncode == 0
    clone = next(call for call in result["git_calls"] if call[:1] == ["clone"])
    assert clone[1:3] == ["--branch", "main"]


def test_clean_rerun_reuses_checkout_and_dirty_checkout_blocks_update(windows_harness: Any) -> None:
    configuration = {"distros": [_distro()], "execute_bootstrap": True}
    assert windows_harness(configuration)["result"].returncode == 0
    repeated = windows_harness(configuration)
    assert repeated["result"].returncode == 0
    assert not any(call[:1] == ["clone"] for call in repeated["git_calls"])
    blocked = windows_harness({**configuration, "dirty": " M changed.lean\n"})
    assert blocked["result"].returncode == 1 and "has local changes" in blocked["result"].stdout
    assert blocked["installed"] is None


def test_explicit_python_path_preserves_native_and_linux_arguments(windows_harness: Any) -> None:
    initial = windows_harness({"distros": [_distro()]})
    interpreter = initial["binaries"] / "python with 'quote' \"double\" $literal `tick`"
    _write_executable(
        interpreter, "import os, sys\nos.execv(sys.executable, [sys.executable, *sys.argv[1:]])"
    )
    result = windows_harness(
        {"distros": [_distro()], "execute_bootstrap": True}, "-Python", str(interpreter)
    )
    assert result["result"].returncode == 0, result["result"].stdout + result["result"].stderr
    assert result["payload"][4] == str(interpreter)
    assert result["installed"] == ["--python", str(interpreter)]


def test_unsupported_explicit_python_fails_before_checkout_changes(windows_harness: Any) -> None:
    initial = windows_harness({"distros": [_distro()]})
    interpreter = initial["binaries"] / "old-python"
    _write_executable(interpreter, "raise SystemExit(1)")
    result = windows_harness(
        {"distros": [_distro()], "execute_bootstrap": True}, "-Python", str(interpreter)
    )
    assert (
        result["result"].returncode == 1
        and "selected WSL Python needs Python 3.12+" in result["result"].stdout
    )
    assert not any(call[:1] == ["clone"] for call in result["git_calls"])


def test_noninteractive_bootstrap_sudo_never_prompts(windows_harness: Any, tmp_path: Path) -> None:
    initial = windows_harness({"distros": [_distro()]})
    for name in ("python3.14", "python3.13", "python3.12", "python3", "python"):
        _write_executable(initial["binaries"] / name, "raise SystemExit(1)")
    _write_executable(initial["binaries"] / "id", "print('1000')")
    _write_executable(initial["binaries"] / "apt-get", "raise SystemExit(97)")
    sudo_calls = tmp_path / "sudo.json"
    _write_executable(
        initial["binaries"] / "sudo",
        f"import json, sys\nfrom pathlib import Path\nPath({str(sudo_calls)!r}).write_text(json.dumps(sys.argv[1:]))\nraise SystemExit(1)",
    )
    result = windows_harness({"distros": [_distro()], "execute_bootstrap": True}, "-NonInteractive")
    assert result["result"].returncode == 1
    assert json.loads(sudo_calls.read_text()) == [
        "-n",
        "env",
        "DEBIAN_FRONTEND=noninteractive",
        "apt-get",
        "update",
    ]
    assert result["installed"] is None


def test_unknown_distro_version_fails_closed_before_shell_probe(windows_harness: Any) -> None:
    result = windows_harness({"distros": [_distro(version=3)]})
    assert (
        result["result"].returncode == 1
        and "Could not determine the WSL version" in result["result"].stdout
    )
    assert result["payload"] is None
    assert not any(call[:1] == ["-d"] for call in result["calls"])


def test_each_invocation_has_a_distinct_temporary_script(windows_harness: Any) -> None:
    first = windows_harness({"distros": [_distro()]})
    second = windows_harness({"distros": [_distro()]})
    assert first["calls"][-1][4] != second["calls"][-1][4]
    assert not list(second["temporary"].glob("leanflow-wsl-bootstrap-*"))


def test_cmd_wrapper_preserves_script_path_and_forwards_arguments() -> None:
    source = (REPO_ROOT / "scripts/install.cmd").read_text()
    assert (
        'powershell.exe -ExecutionPolicy ByPass -NoLogo -NoProfile -File "%~dp0install.ps1" %*'
        in source
    )
    assert "exit /b 1" in source and source.rstrip().endswith("exit /b 0")


@pytest.mark.parametrize(
    ("arguments", "unsupported_flag"),
    [
        (["-NonInteractive"], "--non-interactive"),
        (["-LeanProbeSource", "~/LeanProbe"], "--lean-probe-source"),
        (["-WithLemex"], "--with-lemex"),
    ],
)
def test_requested_new_flags_refuse_older_shared_installer(
    windows_harness: Any, arguments: list[str], unsupported_flag: str
) -> None:
    result = windows_harness(
        {"distros": [_distro()], "execute_bootstrap": True, "installer_flags": []},
        *arguments,
    )
    assert result["result"].returncode == 1
    assert f"does not support {unsupported_flag}" in result["result"].stdout
    assert "published matching revision" in result["result"].stdout
    assert result["installed"] is None


def test_failed_companion_help_refuses_requested_options(windows_harness: Any) -> None:
    result = windows_harness(
        {"distros": [_distro()], "execute_bootstrap": True, "installer_help_exit": 3},
        "-NonInteractive",
    )
    assert result["result"].returncode == 1
    assert "does not expose a compatible shared installer" in result["result"].stdout
    assert result["installed"] is None


def test_with_lemex_reaches_linux_installer_and_rerun_guidance(windows_harness: Any) -> None:
    installed = windows_harness({"distros": [_distro()], "execute_bootstrap": True}, "-WithLemex")
    assert installed["result"].returncode == 0, (
        installed["result"].stdout + installed["result"].stderr
    )
    assert installed["installed"] == ["--python", "python3.13", "--with-lemex"]
    assert installed["payload"][-1] == "1"
    setup = windows_harness({"distros": []}, "-WithLemex", "-NonInteractive")
    assert "-WithLemex -NonInteractive" in setup["result"].stdout


@pytest.mark.parametrize("divergence", ["divergent_head", "divergent_selected_branch"])
def test_unpublished_or_divergent_branches_are_not_checked_out(
    windows_harness: Any, divergence: str
) -> None:
    configuration = {"distros": [_distro()], "execute_bootstrap": True}
    assert windows_harness(configuration)["result"].returncode == 0
    refused = windows_harness({**configuration, divergence: True})
    assert (
        refused["result"].returncode == 1
        and "unpublished or divergent commits" in refused["result"].stdout
    )
    assert refused["installed"] is None
    assert not any("switch" in call or "set-url" in call for call in refused["git_calls"])


def _git(repo: Path, *arguments: str) -> str:
    """Run real Git against an isolated local test repository."""
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("selected_branch_diverges", [False, True])
def test_real_git_histories_preserve_unpublished_checkout(
    tmp_path: Path, selected_branch_diverges: bool
) -> None:
    """Check ancestry protection using actual commits and a local-only bare remote."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    target = tmp_path / "checkout"
    remote.mkdir()
    seed.mkdir()
    _git(remote, "init", "--bare")
    _git(seed, "init", "-b", "main")
    (seed / "file").write_text("public\n")
    _git(seed, "add", "file")
    _git(
        seed,
        "-c",
        "user.name=Installer fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "public",
    )
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "origin", "main")
    _git(tmp_path, "clone", "--branch", "main", str(remote), str(target))
    if selected_branch_diverges:
        _git(target, "branch", "published-head")
    else:
        _git(target, "switch", "-c", "unpublished")
    (target / "file").write_text("private commit\n")
    _git(target, "add", "file")
    _git(
        target,
        "-c",
        "user.name=Installer fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "unpublished",
    )
    if selected_branch_diverges:
        _git(target, "switch", "published-head")
    original_branch = _git(target, "branch", "--show-current")
    original_head = _git(target, "rev-parse", "HEAD")
    source = (REPO_ROOT / "scripts/install.ps1").read_text()
    script = tmp_path / "bootstrap.sh"
    script.write_text(source.split("    return @'\n", 1)[1].split("\n'@", 1)[0])
    payload = [str(remote), "main", str(target), "0", sys.executable, "0", "1", "", "0"]
    encoded = [base64.b64encode(("." + value).encode()).decode() for value in payload]
    result = subprocess.run(
        ["bash", str(script), *encoded],
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1 and "unpublished or divergent commits" in result.stdout
    assert _git(target, "branch", "--show-current") == original_branch
    assert _git(target, "rev-parse", "HEAD") == original_head
    assert (target / "file").read_text() == (
        "public\n" if selected_branch_diverges else "private commit\n"
    )
