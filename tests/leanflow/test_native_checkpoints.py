"""Tests for native workflow checkpoint persistence semantics."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from leanflow_cli.native import native_checkpoints
from tools.utilities import checkpoint_manager


def test_import_does_not_load_agent_or_start_mcp_discovery():
    """Keep checkpoint-only startup independent of provider and MCP imports."""
    repo_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["LEANFLOW_DISABLE_MCP"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import leanflow_cli.native.native_checkpoints; "
                "assert 'run_agent' not in sys.modules; "
                "assert 'core.model_tools' not in sys.modules; "
                "assert 'tools.mcp.mcp_tool' not in sys.modules"
            ),
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_forced_native_checkpoint_captures_post_edit_state_in_same_turn(monkeypatch, tmp_path):
    """Require pre-exit checkpointing to bypass ordinary per-turn deduplication."""
    project = tmp_path / "Demo"
    project.mkdir()
    source = project / "Main.lean"
    source.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    checkpoint_base = tmp_path / "checkpoints"
    monkeypatch.setattr(checkpoint_manager, "CHECKPOINT_BASE", checkpoint_base)
    monkeypatch.setattr(native_checkpoints, "_project_root", lambda: str(project))

    manager = checkpoint_manager.CheckpointManager(enabled=True)
    assert manager.ensure_checkpoint(str(project), "before verified edit") is True
    before_hash = manager.list_checkpoints(str(project))[0]["hash"]

    source.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    agent = SimpleNamespace(_checkpoint_mgr=manager)

    linked_hash = native_checkpoints._latest_filesystem_checkpoint_hash(
        agent, reason="pre-exit checkpoint", force=True
    )

    assert linked_hash != before_hash
    shadow = checkpoint_manager._shadow_repo_path(str(project))
    ok, content, _ = checkpoint_manager._run_git(
        ["show", f"{linked_hash}:Main.lean"], shadow, str(project)
    )
    assert ok is True
    assert content == "theorem demo : True := by\n  trivial"
