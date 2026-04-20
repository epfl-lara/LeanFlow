from __future__ import annotations

import json

from opengauss_cli.local_models import get_local_runtime_status, resolve_active_local_runtime, use_local_runtime


def test_use_local_runtime_persists_active_selection(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENGAUSS_HOME", str(tmp_path / "home"))

    payload = use_local_runtime("vllm", model="google/gemma-4-31B-it")

    assert payload["runtime"] == "vllm"
    assert payload["model"] == "google/gemma-4-31B-it"

    active = resolve_active_local_runtime()
    assert active is not None
    assert active["runtime"] == "vllm"
    assert active["model"] == "google/gemma-4-31B-it"


def test_runtime_status_reads_state_file(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENGAUSS_HOME", str(tmp_path / "home"))
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
