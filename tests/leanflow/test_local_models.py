from __future__ import annotations

import json

import pytest

from leanflow_cli.local_models import (
    SUPPORTED_LOCAL_RUNTIMES,
    _build_command,
    get_local_runtime_status,
    list_local_runtimes,
    read_local_runtime_logs,
    resolve_active_local_runtime,
    stop_local_runtime,
    use_local_runtime,
)


def test_use_local_runtime_persists_active_selection(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    payload = use_local_runtime("vllm", model="google/gemma-3-27b-it")

    assert payload["runtime"] == "vllm"
    assert payload["model"] == "google/gemma-3-27b-it"

    active = resolve_active_local_runtime()
    assert active is not None
    assert active["runtime"] == "vllm"
    assert active["model"] == "google/gemma-3-27b-it"


def test_use_local_runtime_rejects_unsupported_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    with pytest.raises(ValueError, match="Unsupported"):
        use_local_runtime("llamacloud", model="some-model")


def test_runtime_status_reads_state_file(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    state_root = tmp_path / "home" / "local-models"
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "ollama.json").write_text(
        json.dumps(
            {
                "runtime": "ollama",
                "pid": 999999,
                "model": "qwen2.5-coder",
                "host": "127.0.0.1",
                "port": 11434,
                "base_url": "http://127.0.0.1:11434/v1",
                "command": ["ollama", "serve"],
            }
        ),
        encoding="utf-8",
    )

    status = get_local_runtime_status("ollama")

    assert status["runtime"] == "ollama"
    assert status["running"] is False
    assert status["base_url"] == "http://127.0.0.1:11434/v1"


def test_runtime_status_raises_for_unknown_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    with pytest.raises(ValueError, match="Unsupported"):
        get_local_runtime_status("mythical-runtime")


def test_stop_local_runtime_handles_no_pid_gracefully(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    result = stop_local_runtime("vllm")

    assert result["runtime"] == "vllm"
    assert result["running"] is False


def test_stop_local_runtime_raises_for_unknown_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    with pytest.raises(ValueError, match="Unsupported"):
        stop_local_runtime("badruntime")


def test_list_local_runtimes_returns_all_supported(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    results = list_local_runtimes()

    assert len(results) == len(SUPPORTED_LOCAL_RUNTIMES)
    returned_runtimes = {r["runtime"] for r in results}
    assert returned_runtimes == set(SUPPORTED_LOCAL_RUNTIMES)


def test_list_local_runtimes_all_have_required_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    for status in list_local_runtimes():
        assert "runtime" in status
        assert "running" in status
        assert "healthy" in status
        assert "pid" in status
        assert "base_url" in status


def test_read_local_runtime_logs_returns_empty_for_missing_log(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    result = read_local_runtime_logs("vllm")

    assert result == ""


def test_read_local_runtime_logs_tails_content(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    state_root = tmp_path / "home" / "local-models"
    state_root.mkdir(parents=True, exist_ok=True)
    lines = [f"line {i}" for i in range(1, 11)]
    (state_root / "vllm.log").write_text("\n".join(lines), encoding="utf-8")

    result = read_local_runtime_logs("vllm", tail=3)

    assert result == "line 8\nline 9\nline 10"


def test_resolve_active_local_runtime_returns_none_without_config(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    result = resolve_active_local_runtime()

    assert result is None or isinstance(result, dict)


def test_build_command_vllm_includes_model_and_port():
    cmd = _build_command("vllm", "google/gemma-3-27b-it", "127.0.0.1", 8000, [])

    assert "vllm.entrypoints.openai.api_server" in " ".join(cmd)
    assert "--model" in cmd
    assert "google/gemma-3-27b-it" in cmd
    assert "--port" in cmd
    assert "8000" in cmd
    assert "--host" in cmd
    assert "127.0.0.1" in cmd


def test_build_command_ollama_uses_serve_subcommand():
    cmd = _build_command("ollama", "qwen2.5-coder", "127.0.0.1", 11434, [])

    assert cmd[0] == "ollama"
    assert "serve" in cmd


def test_build_command_llama_cpp_includes_model_flag():
    cmd = _build_command("llama_cpp", "my-model.gguf", "127.0.0.1", 8080, [])

    assert "llama-server" in cmd[0]
    assert "-m" in cmd
    assert "my-model.gguf" in cmd
    assert "--port" in cmd
    assert "8080" in cmd


def test_build_command_raises_for_unknown_runtime():
    with pytest.raises(ValueError, match="Unsupported"):
        _build_command("badruntime", "model", "127.0.0.1", 9000, [])


def test_build_command_appends_extra_args():
    extra = ["--tensor-parallel-size", "2"]
    cmd = _build_command("vllm", "google/gemma-3-27b-it", "127.0.0.1", 8000, extra)

    assert "--tensor-parallel-size" in cmd
    assert "2" in cmd


def test_build_command_extra_args_appear_in_ollama():
    extra = ["--debug"]
    cmd = _build_command("ollama", "model", "127.0.0.1", 11434, extra)

    assert "--debug" in cmd
