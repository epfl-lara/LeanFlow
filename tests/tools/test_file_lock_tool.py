"""Model-facing file-reservation tool safety tests."""

from __future__ import annotations

import json

from tools.implementations import file_lock_tool
from tools.registry import registry


def test_model_file_lock_schemas_do_not_expose_force():
    """Keep unconditional reservation takeover out of the model tool surface."""
    acquire_properties = file_lock_tool.FILE_LOCK_ACQUIRE_SCHEMA["parameters"]["properties"]
    release_properties = file_lock_tool.FILE_LOCK_RELEASE_SCHEMA["parameters"]["properties"]

    assert "force" not in acquire_properties
    assert "force" not in release_properties


def test_model_file_lock_dispatch_ignores_injected_force(monkeypatch):
    """Ignore a force argument even when a model submits it outside the schema."""
    calls = []

    def acquire(path, *, owner_id, purpose="", ttl_seconds=1800, force=False):
        calls.append(("acquire", force))
        return {"success": True}

    def release(path, *, owner_id, force=False):
        calls.append(("release", force))
        return {"success": True}

    monkeypatch.setattr(file_lock_tool, "_acquire_file_lock", acquire)
    monkeypatch.setattr(file_lock_tool, "_release_file_lock", release)

    acquired = json.loads(
        registry.dispatch(
            "acquire_file_lock",
            {"path": "/tmp/Main.lean", "force": True},
            owner_id="model-owner",
        )
    )
    released = json.loads(
        registry.dispatch(
            "release_file_lock",
            {"path": "/tmp/Main.lean", "force": True},
            owner_id="model-owner",
        )
    )

    assert acquired["success"] is True
    assert released["success"] is True
    assert calls == [("acquire", False), ("release", False)]
