"""Regression tests for loading feedback on slow slash commands."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cli as cli_mod
from cli import GaussCLI


class TestCLILoadingIndicator:
    def _make_cli(self):
        cli_obj = GaussCLI.__new__(GaussCLI)
        cli_obj._app = None
        cli_obj._last_invalidate = 0.0
        cli_obj._command_running = False
        cli_obj._command_status = ""
        cli_obj._active_handoff_cwd = lambda: "/tmp"
        cli_obj._invalidate = MagicMock()
        cli_obj.console = MagicMock()
        cli_obj.config = {}
        cli_obj._project_override_root = None
        return cli_obj

    def test_autoformalize_command_sets_busy_state_and_prints_status(self, capsys):
        cli_obj = self._make_cli()
        seen = {}
        plan = SimpleNamespace(
            user_instruction="prove the main lemma",
            handoff_request=SimpleNamespace(
                argv=["/usr/bin/claude", "--model", "claude-opus-4-6"],
                cwd="/tmp/project",
                env={"HOME": "/tmp/home"},
            ),
            managed_context=SimpleNamespace(startup_context_path=None, backend_name="claude-code"),
            workflow_kind="autoformalize",
            backend_command="/lean4:autoformalize prove the main lemma",
            project=SimpleNamespace(label="Demo Project", root="/tmp/project"),
        )

        def fake_spawn_interactive(**_kwargs):
            seen["running"] = cli_obj._command_running
            seen["status"] = cli_obj._command_status
            print("autoformalize done")
            return SimpleNamespace(task_id="af-001")

        swarm = MagicMock()
        swarm.spawn_interactive.side_effect = fake_spawn_interactive

        with patch.object(cli_mod, "resolve_autoformalize_request", return_value=plan), \
             patch.object(cli_obj, "_setup_swarm_completion_callback"), \
             patch.object(cli_mod, "SwarmManager", return_value=swarm), \
             patch.object(cli_mod, "ChatConsole", return_value=MagicMock()):
            assert cli_obj.process_command("/autoformalize prove the main lemma")

        output = capsys.readouterr().out
        assert "⏳ Preparing managed Lean workflow session..." in output
        assert "autoformalize done" in output
        assert seen == {
            "running": True,
            "status": "Preparing managed Lean workflow session...",
        }
        assert cli_obj._command_running is False
        assert cli_obj._command_status == ""
        assert cli_obj._invalidate.call_count == 2

    def test_handoff_alias_uses_the_same_busy_state(self, capsys):
        cli_obj = self._make_cli()
        seen = {}
        plan = SimpleNamespace(
            user_instruction="prove the base case",
            handoff_request=SimpleNamespace(
                argv=["/usr/bin/claude", "--model", "claude-opus-4-6"],
                cwd="/tmp/project",
                env={"HOME": "/tmp/home"},
            ),
            managed_context=SimpleNamespace(startup_context_path=None, backend_name="claude-code"),
            workflow_kind="autoformalize",
            backend_command="/lean4:autoformalize prove the base case",
            project=SimpleNamespace(label="Demo Project", root="/tmp/project"),
        )

        def fake_spawn_interactive(**_kwargs):
            seen["running"] = cli_obj._command_running
            seen["status"] = cli_obj._command_status
            print("handoff alias done")
            return SimpleNamespace(task_id="af-001")

        swarm = MagicMock()
        swarm.spawn_interactive.side_effect = fake_spawn_interactive

        with patch.object(cli_mod, "resolve_autoformalize_request", return_value=plan) as mock_resolve, \
             patch.object(cli_obj, "_setup_swarm_completion_callback"), \
             patch.object(cli_mod, "SwarmManager", return_value=swarm), \
             patch.object(cli_mod, "ChatConsole", return_value=MagicMock()):
            assert cli_obj.process_command("/handoff prove the base case")

        output = capsys.readouterr().out
        assert "⏳ Preparing managed Lean workflow session..." in output
        assert "handoff alias done" in output
        mock_resolve.assert_called_once_with(
            "/autoformalize prove the base case",
            cli_obj.config,
            active_cwd="/tmp",
        )
        assert seen == {
            "running": True,
            "status": "Preparing managed Lean workflow session...",
        }
        assert cli_obj._command_running is False
        assert cli_obj._command_status == ""
        assert cli_obj._invalidate.call_count == 2

    def test_busy_state_falls_back_to_ascii_prefix_when_unicode_is_disabled(self, capsys, monkeypatch):
        cli_obj = self._make_cli()
        monkeypatch.setattr(cli_mod, "supports_unicode", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(cli_mod, "supports_ansi", lambda *_args, **_kwargs: False)

        with cli_obj._busy_command("Preparing managed Lean workflow session..."):
            print("done")

        output = capsys.readouterr().out
        assert "... Preparing managed Lean workflow session..." in output
        assert "⏳" not in output
        assert "done" in output
