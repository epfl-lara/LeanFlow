"""Tests for the process-isolated auxiliary text-call boundary."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agent.providers import isolated_auxiliary


def _worker_script(tmp_path, source: str):
    script = tmp_path / "auxiliary_worker.py"
    script.write_text(source, encoding="utf-8")
    return [sys.executable, str(script)]


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def test_overrunning_worker_is_killed_at_wall_clock_deadline(tmp_path):
    """An SDK call that ignores its request timeout cannot pin the caller."""
    child_pid_file = tmp_path / "child.pid"
    command = (
        _worker_script(
            tmp_path,
            """
import pathlib
import subprocess
import sys
import time

sys.stdin.read()
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
time.sleep(60)
""",
        )
        + [str(child_pid_file)]
    )

    started = time.monotonic()
    with pytest.raises(isolated_auxiliary.IsolatedAuxiliaryTimeout):
        isolated_auxiliary.run_isolated_auxiliary_text(
            task="orchestration",
            provider="main",
            messages=[{"role": "user", "content": "route"}],
            timeout=1.0,
            _worker_command=command,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 1.6
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2.0
    while _process_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _process_exists(child_pid)


def test_exited_worker_leader_cannot_strand_pipe_owning_descendant(tmp_path):
    """Cleanup kills the owned group even when its leader exited before timeout."""
    child_pid_file = tmp_path / "orphan.pid"
    command = (
        _worker_script(
            tmp_path,
            """
import pathlib
import subprocess
import sys

sys.stdin.read()
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
# Exit now. The child deliberately retains stdout/stderr, so communicate()
# waits on its pipes after this group leader has become a zombie.
""",
        )
        + [str(child_pid_file)]
    )

    started = time.monotonic()
    child_pid = 0
    try:
        with pytest.raises(isolated_auxiliary.IsolatedAuxiliaryTimeout):
            isolated_auxiliary.run_isolated_auxiliary_text(
                task="orchestration",
                provider="main",
                messages=[{"role": "user", "content": "route"}],
                timeout=0.4,
                _worker_command=command,
            )
        elapsed = time.monotonic() - started

        assert elapsed < 1.2
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2.0
        while _process_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _process_exists(child_pid)
    finally:
        if child_pid and _process_exists(child_pid):
            with contextlib.suppress(OSError):
                os.kill(child_pid, 9)


def test_successful_worker_result_is_normalized(tmp_path):
    command = _worker_script(
        tmp_path,
        f"""
import json
import sys

request = json.loads(sys.stdin.read())
print({isolated_auxiliary.RESULT_PREFIX!r} + json.dumps({{
    "ok": True,
    "content": request["messages"][0]["content"] + "-ok",
    "model": "test-model",
}}))
""",
    )

    result = isolated_auxiliary.run_isolated_auxiliary_text(
        task="orchestration",
        provider="main",
        messages=[{"role": "user", "content": "route"}],
        timeout=2.0,
        _worker_command=command,
    )

    assert result.content == "route-ok"
    assert result.model == "test-model"


def test_isolated_worker_inherits_provider_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AUXILIARY_ORCHESTRATION_PROVIDER", "openai-codex")
    command = _worker_script(
        tmp_path,
        f"""
import json
import os
import sys

sys.stdin.read()
print({isolated_auxiliary.RESULT_PREFIX!r} + json.dumps({{
    "ok": True,
    "content": os.environ["AUXILIARY_ORCHESTRATION_PROVIDER"],
    "model": "test-model",
}}))
""",
    )

    result = isolated_auxiliary.run_isolated_auxiliary_text(
        task="orchestration",
        provider=None,
        messages=[{"role": "user", "content": "route"}],
        timeout=2.0,
        _worker_command=command,
    )

    assert result.content == "openai-codex"


def test_worker_protocol_serializes_provider_response(monkeypatch, capsys):
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="continue"))],
        model="control-model",
    )
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return response

    monkeypatch.setattr(isolated_auxiliary, "call_llm", fake_call_llm)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "task": "orchestration",
                    "provider": "main",
                    "messages": [{"role": "user", "content": "route"}],
                    "temperature": 0.1,
                    "max_tokens": 200,
                    "timeout": 5.0,
                }
            )
        ),
    )

    assert isolated_auxiliary.worker_main() == 0
    line = capsys.readouterr().out.strip()
    payload = json.loads(line.removeprefix(isolated_auxiliary.RESULT_PREFIX))
    assert payload == {
        "ok": True,
        "content": "continue",
        "model": "control-model",
    }
    assert calls == [
        {
            "task": "orchestration",
            "provider": "main",
            "model": None,
            "base_url": None,
            "api_key": None,
            "messages": [{"role": "user", "content": "route"}],
            "temperature": 0.1,
            "max_tokens": 200,
            "timeout": 5.0,
        }
    ]


def test_worker_redacts_exact_custom_key_from_success_result(monkeypatch, capsys):
    """An arbitrary custom key cannot cross back even if a provider echoes it."""
    secret = "ordinary-unpatterned-custom-credential"
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=f"echo {secret}"))],
        model=f"custom-model-{secret}",
    )
    monkeypatch.setattr(isolated_auxiliary, "call_llm", lambda **_kwargs: response)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "task": "",
                    "provider": "custom",
                    "model": "custom-model",
                    "base_url": "https://custom.invalid/v1",
                    "api_key": secret,
                    "messages": [{"role": "user", "content": "summarize"}],
                    "timeout": 5.0,
                }
            )
        ),
    )

    assert isolated_auxiliary.worker_main() == 0
    output = capsys.readouterr().out
    assert secret not in output
    payload = json.loads(output.strip().removeprefix(isolated_auxiliary.RESULT_PREFIX))
    assert payload["content"] == "echo [REDACTED]"
    assert payload["model"] == "custom-model-[REDACTED]"


def test_worker_redacts_exact_custom_key_from_unpatterned_exception(monkeypatch, capsys):
    """Exact stdin credentials are redacted even without a recognizable prefix."""
    secret = "ordinary-unpatterned-custom-credential"

    def failed_call(**_kwargs):
        raise RuntimeError(f"transport rejected credential {secret}")

    monkeypatch.setattr(isolated_auxiliary, "call_llm", failed_call)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "task": "",
                    "provider": "custom",
                    "model": "custom-model",
                    "base_url": "https://custom.invalid/v1",
                    "api_key": secret,
                    "messages": [{"role": "user", "content": "summarize"}],
                    "timeout": 5.0,
                }
            )
        ),
    )

    assert isolated_auxiliary.worker_main() == 0
    output = capsys.readouterr().out
    assert secret not in output
    payload = json.loads(output.strip().removeprefix(isolated_auxiliary.RESULT_PREFIX))
    assert payload["error"] == "transport rejected credential [REDACTED]"


def test_worker_error_kind_is_preserved(tmp_path):
    command = _worker_script(
        tmp_path,
        f"""
import json
import sys

sys.stdin.read()
print({isolated_auxiliary.RESULT_PREFIX!r} + json.dumps({{
    "ok": False,
    "error_kind": "unavailable",
    "error": "provider missing",
    "provider": "custom",
    "model": "control-model",
}}))
""",
    )

    with pytest.raises(
        isolated_auxiliary.IsolatedAuxiliaryUnavailable,
        match="provider missing",
    ) as exc_info:
        isolated_auxiliary.run_isolated_auxiliary_text(
            task="orchestration",
            provider="main",
            messages=[{"role": "user", "content": "route"}],
            timeout=2.0,
            _worker_command=command,
        )

    assert exc_info.value.provider == "custom"
    assert exc_info.value.model == "control-model"


def test_worker_protocol_serializes_resolved_identity_on_failure(monkeypatch, capsys):
    def failed_call(**_kwargs):
        raise RuntimeError("Connection error.")

    monkeypatch.setattr(isolated_auxiliary, "call_llm", failed_call)
    monkeypatch.setattr(
        isolated_auxiliary,
        "resolve_auxiliary_call_identity",
        lambda **_kwargs: isolated_auxiliary.AuxiliaryCallIdentity(
            provider="custom",
            model="zai-org/GLM-5.2",
        ),
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "task": "orchestration",
                    "provider": None,
                    "messages": [{"role": "user", "content": "route"}],
                    "temperature": 0.1,
                    "max_tokens": 200,
                    "timeout": 5.0,
                }
            )
        ),
    )

    assert isolated_auxiliary.worker_main() == 0
    line = capsys.readouterr().out.strip()
    payload = json.loads(line.removeprefix(isolated_auxiliary.RESULT_PREFIX))
    assert payload == {
        "ok": False,
        "error_kind": "unavailable",
        "error": "Connection error.",
        "provider": "custom",
        "model": "zai-org/GLM-5.2",
    }


def test_auxiliary_errors_are_unconditionally_redacted_and_bounded(monkeypatch):
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "0")
    secrets = (
        "sk-testCredential1234567890",
        "header-token-1234567890",
        "query-key-1234567890",
        "query-token-1234567890",
        "x-header-key-1234567890",
    )
    error = (
        f"OPENAI_API_KEY={secrets[0]} "
        f"Authorization: Bearer {secrets[1]} "
        f"https://provider.invalid/v1?api_key={secrets[2]}&token={secrets[3]} "
        f"x-api-key: {secrets[4]} " + ("detail " * 100)
    )

    sanitized = isolated_auxiliary.sanitize_auxiliary_error(error, limit=180)

    assert len(sanitized) == 180
    assert "[REDACTED]" in sanitized
    assert all(secret not in sanitized for secret in secrets)


def test_auxiliary_error_redacts_unlabelled_jwt_unconditionally(monkeypatch):
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "0")
    token = (
        "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwic2NvcGUiOiJjb2RleCJ9."
        "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-"
    )

    sanitized = isolated_auxiliary.sanitize_auxiliary_error(f"provider failure returned {token}.")

    assert sanitized == "provider failure returned [REDACTED]."
    assert token not in sanitized
    assert token[:16] not in sanitized
    assert token[-16:] not in sanitized


@pytest.mark.parametrize(
    "value",
    [
        "release 4.27.0 is installed",
        "connect to api.example.com",
        "resolve namespace.module.identifier",
        "read alpha-beta.gamma_delta.component-name",
        "verylongsubdomainname.verylongdomainlabel.verylongtoplevelname",
        "ordinary_identifier_component.another_identifier_component.final_identifier_component",
        "aaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbbbbbb.cccccccccccccccccccc.dddddddddddddddddddd",
    ],
)
def test_auxiliary_error_preserves_ordinary_dotted_text(monkeypatch, value):
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "0")

    assert isolated_auxiliary.sanitize_auxiliary_error(value) == value


@pytest.mark.parametrize(
    "template",
    [
        "OPENAI_API_KEY={secret}",
        "Authorization: Bearer {secret}",
        "https://provider.invalid/v1?api_key={secret}&page=1",
        "https://provider.invalid/v1?key={secret}&page=1",
        "X-API-Key: {secret}",
        '{{"access_token": "{secret}"}}',
    ],
)
def test_auxiliary_error_redaction_covers_common_credential_shapes(monkeypatch, template):
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "0")
    secret = "ordinaryCredentialValue1234567890"

    sanitized = isolated_auxiliary.sanitize_auxiliary_error(template.format(secret=secret))

    assert secret not in sanitized
    assert "[REDACTED]" in sanitized


def test_parent_redacts_untrusted_worker_error_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "0")
    secret = "sk-parentPayloadCredential1234567890"
    command = _worker_script(
        tmp_path,
        f"""
import json
import sys

sys.stdin.read()
print({isolated_auxiliary.RESULT_PREFIX!r} + json.dumps({{
    "ok": False,
    "error_kind": "error",
    "error": "Authorization: Bearer {secret}",
}}))
""",
    )

    with pytest.raises(isolated_auxiliary.IsolatedAuxiliaryError) as raised:
        isolated_auxiliary.run_isolated_auxiliary_text(
            task="orchestration",
            provider="main",
            messages=[{"role": "user", "content": "route"}],
            timeout=2.0,
            _worker_command=command,
        )

    assert secret not in str(raised.value)
    assert "Bearer [REDACTED]" in str(raised.value)


def test_reaped_group_ownership_requires_matching_launch_token(monkeypatch):
    process_token = "unique-launch-token"
    snapshots = iter(
        (
            SimpleNamespace(stdout="13579 24680\n"),
            SimpleNamespace(
                stdout=(
                    "python worker.py " f"{isolated_auxiliary._PROCESS_TOKEN_ENV}={process_token}\n"
                )
            ),
        )
    )
    monkeypatch.setattr(
        isolated_auxiliary.subprocess,
        "run",
        lambda *_args, **_kwargs: next(snapshots),
    )

    assert isolated_auxiliary._reaped_process_group_is_owned(24680, "") is False
    assert isolated_auxiliary._reaped_process_group_is_owned(24680, process_token) is True


def test_reaped_unowned_group_is_never_signaled(monkeypatch):
    class ReapedWorker:
        pid = 24680
        returncode = 1

        def communicate(self, timeout=None):
            return "", ""

    signaled = []
    monkeypatch.setattr(
        isolated_auxiliary,
        "_reaped_process_group_is_owned",
        lambda _pid, _token: False,
    )
    monkeypatch.setattr(
        isolated_auxiliary.os,
        "killpg",
        lambda pid, sig: signaled.append((pid, sig)),
    )

    isolated_auxiliary._kill_and_reap(ReapedWorker())

    assert signaled == []


def test_reaped_failed_worker_group_is_token_revalidated_and_killed(tmp_path):
    """A failed leader cannot strand a detached-stdio descendant after reap."""
    child_pid_file = tmp_path / "reaped-child.pid"
    command = (
        _worker_script(
            tmp_path,
            """
import pathlib
import subprocess
import sys

sys.stdin.read()
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
raise SystemExit(7)
""",
        )
        + [str(child_pid_file)]
    )

    child_pid = 0
    try:
        with pytest.raises(isolated_auxiliary.IsolatedAuxiliaryError, match="status 7"):
            isolated_auxiliary.run_isolated_auxiliary_text(
                task="orchestration",
                provider="main",
                messages=[{"role": "user", "content": "route"}],
                timeout=2.0,
                _worker_command=command,
            )

        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2.0
        while _process_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _process_exists(child_pid)
    finally:
        if child_pid and _process_exists(child_pid):
            with contextlib.suppress(OSError):
                os.kill(child_pid, 9)


def test_caller_interruption_always_cleans_worker(monkeypatch):
    class InterruptedWorker:
        pid = 12345
        returncode = None

        def communicate(self, _input=None, timeout=None):
            raise KeyboardInterrupt

    worker = InterruptedWorker()
    cleaned: list[object] = []
    monkeypatch.setattr(isolated_auxiliary.subprocess, "Popen", lambda *_args, **_kwargs: worker)
    monkeypatch.setattr(
        isolated_auxiliary,
        "_kill_and_reap",
        lambda process: cleaned.append(process),
    )

    with pytest.raises(KeyboardInterrupt):
        isolated_auxiliary.run_isolated_auxiliary_text(
            task="orchestration",
            provider="main",
            messages=[{"role": "user", "content": "route"}],
            timeout=2.0,
        )

    assert cleaned == [worker]


def test_shared_interrupt_cancels_and_reaps_running_worker(tmp_path):
    """Planner cancellation must not wait for the verifier's long deadline."""
    from tools.utilities.interrupt import CooperativeInterrupt, set_interrupt

    child_pid_file = tmp_path / "interrupted-child.pid"
    command = (
        _worker_script(
            tmp_path,
            """
import pathlib
import subprocess
import sys
import time

sys.stdin.read()
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
time.sleep(60)
""",
        )
        + [str(child_pid_file)]
    )
    errors: list[BaseException] = []

    def run() -> None:
        try:
            isolated_auxiliary.run_isolated_auxiliary_text(
                task="planner_synthesis",
                provider="main",
                messages=[{"role": "user", "content": "synthesize"}],
                timeout=30,
                _worker_command=command,
            )
        except BaseException as exc:
            errors.append(exc)

    set_interrupt(False)
    owner = threading.Thread(target=run, daemon=True)
    owner.start()
    deadline = time.monotonic() + 3
    child_pid = 0
    while time.monotonic() < deadline:
        try:
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            time.sleep(0.01)
            continue
        break
    assert child_pid > 0

    started = time.monotonic()
    try:
        set_interrupt(True)
        owner.join(timeout=2)
    finally:
        set_interrupt(False)

    assert not owner.is_alive()
    assert time.monotonic() - started < 2
    assert len(errors) == 1
    assert isinstance(errors[0], CooperativeInterrupt)
    deadline = time.monotonic() + 2
    while _process_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _process_exists(child_pid)
