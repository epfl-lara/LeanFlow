from __future__ import annotations

from pathlib import Path

import pytest

from leanflow_cli import workflow as workflow_mod
from leanflow_cli.cli import loogle_local
from leanflow_cli.config import save_config
from leanflow_cli.formalization.formalization_documents import FormalizationDocumentError
from leanflow_cli.workflow import (
    WORKFLOW_ALIAS_MAP,
    describe_launch_plan,
    parse_workflow_command,
    resolve_workflow_request,
    rewrite_forgiving_workflow_command,
)


def _write_formalization_source(project: Path, relative: str = "docs/paper.tex") -> Path:
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\\section{Main}\\begin{theorem}\\label{thm:toy}True.\\end{theorem}\n",
        encoding="utf-8",
    )
    return path


def test_parse_workflow_command_extracts_swarm_options():
    spec = parse_workflow_command("/autoprove Main.lean --agents 3 --goal finish theorem Foo")

    assert spec.workflow_kind == "prove"
    assert spec.backend_command == "/prove Main.lean"
    assert spec.parallel_agents == 3
    assert spec.explicit_goal == "finish theorem Foo"


def test_parse_workflow_command_accepts_prompt_alias():
    spec = parse_workflow_command("/prove Main.lean --prompt use lemma abs_abs_sub first")

    assert spec.workflow_kind == "prove"
    assert spec.backend_command == "/prove Main.lean"
    assert spec.explicit_goal == "use lemma abs_abs_sub first"


def test_parse_workflow_command_extracts_provider_override():
    spec = parse_workflow_command("/prove Main.lean --provider codex")

    assert spec.workflow_args == "Main.lean"
    assert spec.backend_command == "/prove Main.lean"
    assert spec.provider_override == "codex"


def test_parse_workflow_command_extracts_model_and_clean_room_options():
    spec = parse_workflow_command(
        "/prove IMO2026/P2.lean --provider rcp --model zai-org/GLM-5.2 "
        "--clean-room --clean-room-label 'IMO 2026 Problem 2' "
        "--clean-room-label IMO2026P2"
    )

    assert spec.workflow_args == "IMO2026/P2.lean"
    assert spec.model_override == "zai-org/GLM-5.2"
    assert spec.clean_room is True
    assert spec.clean_room_labels == ("IMO 2026 Problem 2", "IMO2026P2")

    with pytest.raises(ValueError, match="requires a value"):
        parse_workflow_command("/prove Main.lean --model")
    with pytest.raises(ValueError, match="requires a value"):
        parse_workflow_command("/prove Main.lean --clean-room-label")
    with pytest.raises(ValueError, match="only for prove"):
        parse_workflow_command("/formalize notes.tex --clean-room")


def test_parse_workflow_command_extracts_research_profile():
    spec = parse_workflow_command(
        "/prove Main.lean --provider codex --research --research-workers 3"
    )

    assert spec.workflow_args == "Main.lean"
    assert spec.research_mode is True
    assert spec.research_workers == 3

    defaulted = parse_workflow_command("/autoprove Main.lean --research")
    assert defaulted.research_mode is True
    assert defaulted.research_workers == 2

    sequential = parse_workflow_command("/prove Main.lean --research --no-parallel")
    assert sequential.no_parallel is True
    assert sequential.research_workers == 0


def test_parse_workflow_command_human_review_is_explicit():
    assert parse_workflow_command("/prove Main.lean").human_review is False
    assert parse_workflow_command("/prove Main.lean --human-review").human_review is True

    with pytest.raises(ValueError, match="only for prove"):
        parse_workflow_command("/formalize notes.tex --human-review")


def test_parse_research_workers_implies_research_and_validates():
    implied = parse_workflow_command("/prove Main.lean --research-workers 1")
    assert implied.research_mode is True
    assert implied.research_workers == 1

    with pytest.raises(ValueError, match="non-negative"):
        parse_workflow_command("/prove Main.lean --research-workers -1")
    with pytest.raises(ValueError, match="only for prove"):
        parse_workflow_command("/formalize notes.tex --research")


def test_parse_workflow_command_extracts_allowed_axioms():
    spec = parse_workflow_command("/prove Main.lean --axioms my_ax,other_ax")

    assert spec.workflow_args == "Main.lean"
    assert spec.allowed_axioms == "my_ax,other_ax"
    # The axiom list is not leaked into the workflow args / backend command.
    assert "--axioms" not in spec.backend_command

    default_spec = parse_workflow_command("/prove Main.lean")
    assert default_spec.allowed_axioms == ""


def test_parse_workflow_command_extracts_expert_provider_options():
    spec = parse_workflow_command(
        "/prove Main.lean --expert-provider codex --expert-command-template 'codex exec --sandbox read-only -'"
    )

    assert spec.workflow_args == "Main.lean"
    assert spec.expert_provider == "codex"
    assert spec.expert_command_template == "codex exec --sandbox read-only -"


def test_parse_workflow_command_extracts_verifier_provider_options():
    spec = parse_workflow_command(
        "/autoformalize docs/paper.tex "
        "--blueprint-verifier-provider claude-code "
        "--blueprint-verifier-command-template 'claude --print' "
        "--autoformalizer-verifier-provider codex "
        "--autoformalizer-verifier-command-template 'codex exec --sandbox read-only -'"
    )

    assert spec.workflow_args == "docs/paper.tex"
    assert spec.blueprint_verifier_provider == "claude-code"
    assert spec.blueprint_verifier_command_template == "claude --print"
    assert spec.autoformalizer_verifier_provider == "codex"
    assert spec.autoformalizer_verifier_command_template == "codex exec --sandbox read-only -"


def test_parse_workflow_command_extracts_additional_skills():
    spec = parse_workflow_command(
        "/prove Demo/Main.lean --additional-skill .leanflow/skills/paper/SKILL.md --additional_skill extra-skill"
    )

    assert spec.workflow_args == "Demo/Main.lean"
    assert spec.backend_command == "/prove Demo/Main.lean"
    assert spec.additional_skills == (".leanflow/skills/paper/SKILL.md", "extra-skill")


def test_parse_workflow_command_defaults_to_single_agent():
    spec = parse_workflow_command('autoformalize "formalize theorem"')

    assert spec.parallel_agents == 1
    assert spec.no_parallel is False
    assert spec.explicit_goal == ""


def test_resolve_workflow_request_uses_swarm_toolset_only_when_user_requests_agents(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_HUMAN_REVIEW_ENABLED", "1")
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "local",
            "api_mode": "responses",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "sk-test",
            "model": "google/gemma-3-27b-it",
        },
    )

    single = resolve_workflow_request("/autoprove Main.lean", active_cwd=tmp_path)
    swarm = resolve_workflow_request("/autoprove Main.lean --agents 3", active_cwd=tmp_path)

    assert single.toolset_name == "leanflow-prove-worker"
    assert single.child_env["LEANFLOW_NATIVE_USER_APPROVED_SWARM"] == "0"
    assert single.child_env["LEANFLOW_HUMAN_REVIEW_ENABLED"] == "0"
    assert swarm.toolset_name == "leanflow-native-swarm"
    assert swarm.active_skill == "lean-autonomous-swarm"
    assert swarm.child_env["LEANFLOW_NATIVE_USER_APPROVED_SWARM"] == "1"

    reviewed = resolve_workflow_request("/autoprove Main.lean --human-review", active_cwd=tmp_path)
    assert reviewed.child_env["LEANFLOW_HUMAN_REVIEW_ENABLED"] == "1"


def test_resolve_workflow_request_uses_inline_provider_override(monkeypatch, tmp_path):
    captured: dict[str, str | None] = {}
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )

    def fake_runtime(requested=None):
        captured["requested"] = requested
        return {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "sk-test",
            "model": "gpt-5.5",
            "reasoning_effort": "xhigh",
        }

    monkeypatch.setattr(workflow_mod, "resolve_runtime_provider", fake_runtime)

    plan = resolve_workflow_request("/prove Main.lean --provider codex", active_cwd=tmp_path)

    assert captured["requested"] == "codex"
    assert plan.workflow.workflow_args == "Main.lean"
    assert plan.runtime["provider"] == "openai-codex"
    assert plan.child_env["LEANFLOW_NATIVE_REASONING_EFFORT"] == "xhigh"
    assert plan.child_env["LEANFLOW_NATIVE_AUXILIARY_PROVIDER"] == "codex"
    assert plan.child_env["LEANFLOW_NATIVE_AUXILIARY_MODEL"] == "gpt-5.5"
    assert plan.child_env["LEANFLOW_NATIVE_AUXILIARY_REASONING_EFFORT"] == "xhigh"


def test_resolve_clean_room_model_is_scoped_to_one_launch(monkeypatch, tmp_path):
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )

    def fake_runtime(requested=None):
        captured["requested"] = str(requested or "")
        return {
            "provider": "custom",
            "api_mode": "chat_completions",
            "base_url": "https://rcp.example/v1",
            "api_key": "kimi-key",
            "model": "moonshotai/Kimi-K2.7-Code",
        }

    def fake_model_override(runtime, *, requested_provider, model):
        captured["model"] = model
        captured["model_provider"] = requested_provider
        return {
            **runtime,
            "api_key": "glm-key",
            "model": model,
            "requested_provider": requested_provider,
        }

    monkeypatch.setattr(workflow_mod, "resolve_runtime_provider", fake_runtime)
    monkeypatch.setattr(workflow_mod, "apply_runtime_model_override", fake_model_override)

    plan = resolve_workflow_request(
        "/prove IMO2026/P2.lean --provider rcp --model zai-org/GLM-5.2 "
        "--clean-room --clean-room-label 'IMO 2026 Problem 2'",
        active_cwd=tmp_path,
    )

    assert captured == {
        "requested": "rcp",
        "model": "zai-org/GLM-5.2",
        "model_provider": "rcp",
    }
    assert plan.runtime["api_key"] == "glm-key"
    assert plan.child_env["LEANFLOW_NATIVE_MODEL"] == "zai-org/GLM-5.2"
    assert plan.child_env["CONTEXT_COMPRESSION_MODEL"] == "zai-org/GLM-5.2"
    assert plan.child_env["LEANFLOW_NATIVE_AUXILIARY_PROVIDER"] == "custom"
    assert plan.child_env["LEANFLOW_NATIVE_AUXILIARY_BASE_URL"] == "https://rcp.example/v1"
    assert plan.child_env["LEANFLOW_NATIVE_AUXILIARY_API_KEY"] == "glm-key"
    assert plan.child_env["LEANFLOW_NATIVE_AUXILIARY_MODEL"] == "zai-org/GLM-5.2"
    assert "LEANFLOW_DISABLE_REPOSITORY_RESEARCH" not in plan.child_env
    assert plan.child_env["LEANFLOW_DISABLE_SOLUTION_RESEARCH"] == "1"
    labels = plan.child_env["LEANFLOW_CLEAN_ROOM_TASK_LABELS"].split("|")
    assert "IMO2026/P2.lean" in labels
    assert "P2.lean" in labels
    assert "P2" in labels
    assert "IMO2026" in labels
    assert "IMO 2026 Problem 2" in labels


def test_resolve_research_profile_activates_complete_child_env(monkeypatch, tmp_path):
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "sk-test",
            "model": "gpt-5.5",
        },
    )

    plan = resolve_workflow_request(
        "/prove Main.lean --provider codex --research --research-workers 2",
        active_cwd=tmp_path,
    )

    assert plan.workflow.research_mode is True
    assert plan.child_env["LEANFLOW_RESEARCH_MODE"] == "1"
    assert plan.child_env["LEANFLOW_RESEARCH_WORKERS"] == "2"
    assert plan.child_env["LEANFLOW_DISPATCH_MAX_CONCURRENT"] == "2"
    for key in (
        "LEANFLOW_PLAN_STATE",
        "LEANFLOW_PREMISE_RETRIEVAL",
        "LEANFLOW_BUDGET_BREAKPOINT",
        "LEANFLOW_ORCHESTRATOR_ENABLED",
        "LEANFLOW_ORCHESTRATOR_LLM_ENABLED",
        "LEANFLOW_FIDELITY_AUDIT",
        "LEANFLOW_GRAPH_FRONTIER_SELECTION",
        "LEANFLOW_PLANNER_ENABLED",
        "LEANFLOW_DISPATCH_ENABLED",
        "LEANFLOW_NEGATION_PROBE",
        "LEANFLOW_LEARNINGS",
        "LEANFLOW_CURRICULUM_ORDERING",
    ):
        assert plan.child_env[key] == "1"
    assert plan.child_env["LEANFLOW_MANAGER_LLM_MODE"] == "live"
    assert plan.child_env["LEANFLOW_RESEARCH_LOCAL_LOOGLE"] == "0"
    assert plan.child_env["LEANFLOW_PLAN_MD"].endswith("plan.md")


@pytest.mark.parametrize(
    ("command", "local_loogle_override", "expected_builds"),
    [
        ("/prove Main.lean --research", None, 0),
        ("/prove Main.lean --research", "1", 1),
        ("/prove Main.lean", None, 1),
    ],
)
def test_research_profile_suppresses_only_its_detached_local_loogle_build(
    monkeypatch,
    tmp_path,
    command,
    local_loogle_override,
    expected_builds,
):
    if local_loogle_override is None:
        monkeypatch.delenv("LEANFLOW_RESEARCH_LOCAL_LOOGLE", raising=False)
    else:
        monkeypatch.setenv("LEANFLOW_RESEARCH_LOCAL_LOOGLE", local_loogle_override)
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "sk-test",
            "model": "gpt-5.5",
        },
    )
    builds: list[Path] = []
    monkeypatch.setattr(
        loogle_local,
        "ensure_local_loogle_for_project_async",
        lambda project_root: builds.append(Path(project_root)),
    )

    plan = resolve_workflow_request(command, active_cwd=tmp_path)

    assert len(builds) == expected_builds
    assert plan.child_env.get("LEANFLOW_RESEARCH_LOCAL_LOOGLE", "0") == (
        "1" if local_loogle_override == "1" else "0"
    )


def test_env_research_respects_parsed_no_parallel_worker_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.delenv("LEANFLOW_RESEARCH_WORKERS", raising=False)
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "sk-test",
            "model": "gpt-5.5",
        },
    )

    plan = resolve_workflow_request(
        "/prove Main.lean --no-parallel",
        active_cwd=tmp_path,
    )

    assert plan.workflow.no_parallel is True
    assert plan.workflow.research_mode is True
    assert plan.workflow.research_workers == 0
    assert plan.child_env["LEANFLOW_RESEARCH_WORKERS"] == "0"
    assert plan.child_env["LEANFLOW_DISPATCH_MAX_CONCURRENT"] == "1"
    assert plan.child_env["LEANFLOW_BACKGROUND_PROVIDER_CAPACITY"] == "0"


def test_inherited_research_identity_does_not_leak_into_non_prove_workflow(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "sk-test",
            "model": "gpt-5.5",
        },
    )
    builds: list[Path] = []
    monkeypatch.setattr(
        loogle_local,
        "ensure_local_loogle_for_project_async",
        lambda project_root: builds.append(Path(project_root)),
    )

    plan = resolve_workflow_request("/review Main.lean", active_cwd=tmp_path)

    assert plan.workflow.workflow_kind == "review"
    assert plan.workflow.research_mode is False
    assert plan.child_env["LEANFLOW_RESEARCH_MODE"] == "0"
    assert plan.child_env["LEANFLOW_PLAN_STATE"] == "1"
    assert builds == [Path(tmp_path)]


def test_explicit_research_workers_override_stale_inherited_capacity(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_RESEARCH_WORKERS", "9")
    monkeypatch.setenv("LEANFLOW_DISPATCH_MAX_CONCURRENT", "9")
    monkeypatch.setenv("LEANFLOW_BACKGROUND_PROVIDER_CAPACITY", "9")
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "sk-test",
            "model": "gpt-5.5",
        },
    )

    plan = resolve_workflow_request(
        "/prove Main.lean --provider codex --research --research-workers 2",
        active_cwd=tmp_path,
    )

    assert plan.child_env["LEANFLOW_RESEARCH_WORKERS"] == "2"
    assert plan.child_env["LEANFLOW_DISPATCH_MAX_CONCURRENT"] == "2"
    assert plan.child_env["LEANFLOW_BACKGROUND_PROVIDER_CAPACITY"] == "2"


@pytest.mark.parametrize(
    "command",
    (
        "/prove Main.lean --provider codex --research",
        "/prove Main.lean --provider codex --research-workers 2",
    ),
)
def test_explicit_research_forces_inherited_disabled_features(monkeypatch, tmp_path, command):
    required_features = (
        "LEANFLOW_PLAN_STATE",
        "LEANFLOW_PREMISE_RETRIEVAL",
        "LEANFLOW_BUDGET_BREAKPOINT",
        "LEANFLOW_ORCHESTRATOR_ENABLED",
        "LEANFLOW_ORCHESTRATOR_LLM_ENABLED",
        "LEANFLOW_FIDELITY_AUDIT",
        "LEANFLOW_GRAPH_FRONTIER_SELECTION",
        "LEANFLOW_PLANNER_ENABLED",
        "LEANFLOW_DISPATCH_ENABLED",
        "LEANFLOW_NEGATION_PROBE",
        "LEANFLOW_NATIVE_AXIOM_PROFILE_CHECK",
        "LEANFLOW_FINAL_REPORT",
        "LEANFLOW_LEARNINGS",
        "LEANFLOW_CURRICULUM_ORDERING",
        "LEANFLOW_PROJECT_LEAN_ADMISSION",
    )
    for key in required_features:
        monkeypatch.setenv(key, "0")
    # Manager mode is a documented debug control rather than a required
    # feature toggle; deterministic coaching still covers every rejection.
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "off")
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "sk-test",
            "model": "gpt-5.5",
        },
    )

    plan = resolve_workflow_request(command, active_cwd=tmp_path)

    assert plan.workflow.research_mode is True
    for key in required_features:
        assert plan.child_env[key] == "1"
    assert plan.child_env["LEANFLOW_MANAGER_LLM_MODE"] == "off"


def test_environment_research_keeps_feature_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_ENABLED", "0")
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "sk-test",
            "model": "gpt-5.5",
        },
    )

    plan = resolve_workflow_request("/prove Main.lean", active_cwd=tmp_path)

    assert plan.workflow.research_mode is True
    assert plan.child_env["LEANFLOW_ORCHESTRATOR_ENABLED"] == "0"


def test_resolve_workflow_request_passes_configured_api_step_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AGENT_MAX_TURNS", raising=False)
    save_config({"agent": {"max_turns": 145}})
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "local",
            "api_mode": "responses",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "sk-test",
            "model": "google/gemma-3-27b-it",
        },
    )

    plan = resolve_workflow_request("/autoprove Main.lean", active_cwd=tmp_path)

    assert plan.child_env["AGENT_MAX_TURNS"] == "145"


def test_run_workflow_handles_parent_keyboard_interrupt_after_child_exit(monkeypatch, tmp_path):
    class _FakeProcess:
        returncode = None

        def __init__(self):
            self.wait_calls = 0
            self.terminated = False
            self.killed = False

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise KeyboardInterrupt
            self.returncode = 1
            return self.returncode

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    process = _FakeProcess()
    monkeypatch.setattr(
        workflow_mod,
        "spawn_workflow",
        lambda *args, **kwargs: (object(), process),
    )

    assert workflow_mod.run_workflow("/prove Main.lean", active_cwd=tmp_path) == 1
    assert process.wait_calls == 2
    assert process.terminated is False
    assert process.killed is False


def test_run_workflow_never_terminates_child_while_native_runner_handles_interrupt(
    monkeypatch, tmp_path
):
    class _FakeProcess:
        returncode = None

        def __init__(self):
            self.wait_calls = 0
            self.terminated = False
            self.killed = False

        def wait(self, timeout=None):
            assert timeout is None
            self.wait_calls += 1
            if self.wait_calls < 3:
                raise KeyboardInterrupt
            self.returncode = 2
            return self.returncode

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    process = _FakeProcess()
    monkeypatch.setattr(
        workflow_mod,
        "spawn_workflow",
        lambda *args, **kwargs: (object(), process),
    )

    assert workflow_mod.run_workflow("/prove Main.lean", active_cwd=tmp_path) == 2
    assert process.wait_calls == 3
    assert process.terminated is False
    assert process.killed is False


def test_resolve_workflow_request_exports_expert_provider_env(monkeypatch, tmp_path):
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "local",
            "api_mode": "responses",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "sk-test",
            "model": "google/gemma-3-27b-it",
        },
    )

    plan = resolve_workflow_request(
        "/autoprove Main.lean --expert-provider claude-code --expert-command-template 'claude -p'",
        active_cwd=tmp_path,
    )

    assert plan.child_env["AUXILIARY_LEAN_REASONING_PROVIDER"] == "claude-code"
    assert plan.child_env["AUXILIARY_LEAN_REASONING_COMMAND_TEMPLATE"] == "claude -p"
    assert describe_launch_plan(plan)["expert_provider"] == "claude-code"


def test_resolve_workflow_request_exports_verifier_provider_env(monkeypatch, tmp_path):
    _write_formalization_source(tmp_path)
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "local",
            "api_mode": "responses",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "sk-test",
            "model": "google/gemma-3-27b-it",
        },
    )

    plan = resolve_workflow_request(
        "/autoformalize docs/paper.tex "
        "--blueprint-verifier-provider claude-code "
        "--blueprint-verifier-command-template 'claude --print' "
        "--autoformalizer-verifier-provider codex "
        "--autoformalizer-verifier-command-template 'codex exec -'",
        active_cwd=tmp_path,
    )

    assert plan.child_env["AUXILIARY_BLUEPRINT_VERIFICATION_PROVIDER"] == "claude-code"
    assert plan.child_env["AUXILIARY_BLUEPRINT_VERIFICATION_COMMAND_TEMPLATE"] == "claude --print"
    assert plan.child_env["AUXILIARY_AUTOFORMALIZER_VERIFICATION_PROVIDER"] == "codex"
    assert (
        plan.child_env["AUXILIARY_AUTOFORMALIZER_VERIFICATION_COMMAND_TEMPLATE"] == "codex exec -"
    )
    summary = describe_launch_plan(plan)
    assert summary["blueprint_verifier_provider"] == "claude-code"
    assert summary["autoformalizer_verifier_provider"] == "codex"


def test_resolve_workflow_request_forces_single_agent_for_file_scoped_prove(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    (module_dir / "Main.lean").write_text("theorem t : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": project})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "local",
            "api_mode": "responses",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "sk-test",
            "model": "google/gemma-3-27b-it",
        },
    )

    plan = resolve_workflow_request("/autoprove Demo/Main.lean --agents 3", active_cwd=project)

    assert plan.workflow.parallel_agents == 1
    assert plan.toolset_name == "leanflow-prove-worker"
    assert plan.active_skill == "lean-proof-loop"
    assert plan.child_env["LEANFLOW_NATIVE_USER_APPROVED_SWARM"] == "0"


# --- workflow kind mapping ---


@pytest.mark.parametrize(
    "command,expected_kind",
    [
        ("/prove Main.lean", "prove"),
        ("/autoprove Main.lean", "prove"),
        ("autoprove Main.lean", "prove"),
        ("prove Main.lean", "prove"),
        ('/formalize "theorem"', "formalize"),
        ('/autoformalize "theorem"', "formalize"),
        ('autoformalize "theorem"', "formalize"),
        ('formalize "theorem"', "formalize"),
        ("/draft Main.lean", "draft"),
        ("draft Main.lean", "draft"),
        ("/review Main.lean", "review"),
        ("review Main.lean", "review"),
        ("/refactor Main.lean", "refactor"),
        ("refactor Main.lean", "refactor"),
        ("/golf Main.lean", "golf"),
        ("golf Main.lean", "golf"),
    ],
)
def test_parse_workflow_command_maps_all_aliases_to_correct_kind(command, expected_kind):
    spec = parse_workflow_command(command)
    assert (
        spec.workflow_kind == expected_kind
    ), f"{command!r} → {spec.workflow_kind!r}, expected {expected_kind!r}"


@pytest.mark.parametrize(
    "command,expected_backend",
    [
        ("/prove", "/prove"),
        ("/autoprove", "/prove"),
        ("/formalize", "/formalize"),
        ("/autoformalize", "/formalize"),
        ("/draft", "/draft"),
        ("/review", "/review"),
        ("/refactor", "/refactor"),
        ("/golf", "/golf"),
    ],
)
def test_parse_workflow_command_sets_correct_backend_command(command, expected_backend):
    spec = parse_workflow_command(command)
    assert spec.backend_command.startswith(expected_backend)


# --- forgiving alias rewriting ---


@pytest.mark.parametrize(
    "raw,expected_start",
    [
        ("autoprove Main.lean", "/prove Main.lean"),
        ("prove Main.lean", "/prove Main.lean"),
        ('autoformalize "x"', '/formalize "x"'),
        ('formalize "x"', '/formalize "x"'),
        ("draft Main.lean", "/draft Main.lean"),
        ("review Main.lean", "/review Main.lean"),
        ("refactor Main.lean", "/refactor Main.lean"),
        ("golf Main.lean", "/golf Main.lean"),
    ],
)
def test_rewrite_forgiving_workflow_command_maps_bare_aliases(raw, expected_start):
    result = rewrite_forgiving_workflow_command(raw)
    assert result == expected_start, f"{raw!r} → {result!r}, expected {expected_start!r}"


def test_rewrite_forgiving_workflow_command_passthrough_for_slash_commands():
    assert rewrite_forgiving_workflow_command("/prove Main.lean") == "/prove Main.lean"
    assert rewrite_forgiving_workflow_command("/golf Main.lean") == "/golf Main.lean"


def test_rewrite_forgiving_workflow_command_passthrough_for_unknown():
    assert rewrite_forgiving_workflow_command("unknown-command foo") == "unknown-command foo"
    assert rewrite_forgiving_workflow_command("") == ""


# --- agents flag parsing ---


def test_parse_workflow_command_agents_clamped_to_minimum_1():
    spec = parse_workflow_command("/prove Main.lean --agents 0")
    assert spec.parallel_agents == 1


def test_parse_workflow_command_no_parallel_forces_single_agent():
    spec = parse_workflow_command("/prove Main.lean --agents 4 --no-parallel")
    assert spec.parallel_agents == 1
    assert spec.no_parallel is True


def test_parse_workflow_command_legacy_no_parallel_alias_forces_single_agent():
    spec = parse_workflow_command("/prove Main.lean --agents 4 -no-parallel")
    assert spec.parallel_agents == 1
    assert spec.no_parallel is True


def test_parse_workflow_command_agents_rejects_non_integer():
    with pytest.raises(ValueError, match="integer"):
        parse_workflow_command("/prove Main.lean --agents notanumber")


def test_parse_workflow_command_agents_requires_value():
    with pytest.raises(ValueError, match="value"):
        parse_workflow_command("/prove Main.lean --agents")


# --- goal flag parsing ---


def test_parse_workflow_command_goal_is_empty_by_default():
    spec = parse_workflow_command("/prove Main.lean")
    assert spec.explicit_goal == ""


def test_parse_workflow_command_goal_captures_remaining_text():
    spec = parse_workflow_command(
        "/prove Main.lean --goal prove absLipschitz theorem using abs_abs_sub"
    )
    assert spec.explicit_goal == "prove absLipschitz theorem using abs_abs_sub"


def test_parse_workflow_command_goal_and_agents_together():
    spec = parse_workflow_command("/prove Main.lean --agents 2 --goal prove Foo")
    assert spec.parallel_agents == 2
    assert spec.explicit_goal == "prove Foo"


# --- rejected inputs ---


def test_parse_workflow_command_raises_for_unknown_command():
    with pytest.raises(ValueError, match="unsupported"):
        parse_workflow_command("/unknown-workflow Main.lean")


def test_parse_workflow_command_raises_for_empty_command():
    with pytest.raises(ValueError):
        parse_workflow_command("")


# --- workflow args extraction ---


def test_parse_workflow_command_preserves_file_arg():
    spec = parse_workflow_command("/prove ProveDemo/RealTheorems-homework.lean")
    assert spec.workflow_args == "ProveDemo/RealTheorems-homework.lean"
    assert "ProveDemo/RealTheorems-homework.lean" in spec.backend_command


def test_parse_workflow_command_no_workflow_args_when_only_command():
    spec = parse_workflow_command("/prove")
    assert spec.workflow_args == ""


# --- skill selection ---


def test_resolve_workflow_request_assigns_correct_default_skill_for_formalize(
    monkeypatch, tmp_path
):
    _write_formalization_source(tmp_path)
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "local",
            "api_mode": "responses",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "sk-test",
            "model": "google/gemma-3-27b-it",
        },
    )

    plan = resolve_workflow_request("/formalize docs/paper.tex", active_cwd=tmp_path)

    assert plan.active_skill == "lean-formalization"
    assert plan.toolset_name == "leanflow-native"
    assert plan.formalization_document is not None
    assert plan.child_env["LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE"] == "docs/paper.tex"
    assert plan.child_env["LEANFLOW_NATIVE_ACTIVE_FILE"] == "Demo/Paper/Main.lean"
    assert plan.formalization_document.blueprint_skill_path.is_file()
    assert str(plan.formalization_document.blueprint_skill_path) in plan.additional_skills
    assert plan.child_env["LEANFLOW_NATIVE_ADDITIONAL_SKILLS"] == str(
        plan.formalization_document.blueprint_skill_path
    )


def test_resolve_workflow_request_auto_adds_blueprint_skill_for_prove(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    target = project / "Demo" / "Paper" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem t : True := by\n  sorry\n", encoding="utf-8")
    (target.parent / "Blueprint.md").write_text(
        "# Formalization Blueprint\n\n- Source: `docs/paper.tex`\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": project})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "local",
            "api_mode": "responses",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "sk-test",
            "model": "google/gemma-3-27b-it",
        },
    )

    plan = resolve_workflow_request("/prove Demo/Paper/Main.lean", active_cwd=project)

    assert len(plan.additional_skills) == 1
    skill_path = Path(plan.additional_skills[0])
    assert skill_path.is_file()
    assert "Blueprint: `Demo/Paper/Blueprint.md`" in skill_path.read_text(encoding="utf-8")
    assert plan.child_env["LEANFLOW_NATIVE_ADDITIONAL_SKILLS"] == str(skill_path)


def test_resolve_workflow_request_requires_document_for_formalize(monkeypatch, tmp_path):
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "local",
            "api_mode": "responses",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "sk-test",
            "model": "google/gemma-3-27b-it",
        },
    )

    with pytest.raises(FormalizationDocumentError, match="requires a project-local"):
        resolve_workflow_request('/formalize "state Lipschitz theorem"', active_cwd=tmp_path)


def test_resolve_workflow_request_preserves_explicit_swarm_for_document_formalize(
    monkeypatch, tmp_path
):
    _write_formalization_source(tmp_path)
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": Path(tmp_path)})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "local",
            "api_mode": "responses",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "sk-test",
            "model": "google/gemma-3-27b-it",
        },
    )

    plan = resolve_workflow_request("/autoformalize docs/paper.tex --agents 3", active_cwd=tmp_path)

    assert plan.workflow.workflow_kind == "formalize"
    assert plan.workflow.parallel_agents == 3
    assert plan.active_skill == "lean-autonomous-swarm"
    assert plan.toolset_name == "leanflow-native-swarm"
    assert plan.child_env["LEANFLOW_NATIVE_USER_APPROVED_SWARM"] == "1"


def test_resolve_workflow_request_accepts_directory_for_autoformalize(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    _write_formalization_source(project, "docs/paper/main.tex")
    monkeypatch.setattr(
        workflow_mod,
        "discover_leanflow_project",
        lambda cwd: type("Project", (), {"label": "Demo", "root": project})(),
    )
    monkeypatch.setattr(
        workflow_mod,
        "resolve_runtime_provider",
        lambda requested=None: {
            "provider": "local",
            "api_mode": "responses",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "sk-test",
            "model": "google/gemma-3-27b-it",
        },
    )

    plan = resolve_workflow_request("/autoformalize docs/paper", active_cwd=project)

    assert plan.workflow.workflow_kind == "formalize"
    assert plan.workflow.workflow_args == "docs/paper/main.tex"
    assert plan.formalization_document is not None
    assert plan.formalization_document.metadata["document_request_kind"] == "directory"
    assert plan.formalization_document.metadata["document_request_relative"] == "docs/paper"
    assert plan.child_env["LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE"] == "docs/paper/main.tex"
    assert plan.child_env["LEANFLOW_FORMALIZATION_REQUEST_KIND"] == "directory"
    assert plan.child_env["LEANFLOW_FORMALIZATION_REQUEST_RELATIVE"] == "docs/paper"
    assert plan.child_env["LEANFLOW_FORMALIZATION_SELECTED_SOURCE"] == "docs/paper/main.tex"
    assert plan.child_env["LEANFLOW_NATIVE_ACTIVE_FILE"] == "Demo/Main/Main.lean"
    summary = describe_launch_plan(plan)
    assert summary["input"] == "docs/paper (directory)"
    assert summary["document"] == "docs/paper/main.tex"


def test_all_workflow_aliases_are_covered_by_alias_map():
    forgiving_kinds = {"prove", "formalize", "draft", "review", "refactor", "golf"}
    mapped_kinds = {v[0] for v in WORKFLOW_ALIAS_MAP.values()}
    assert forgiving_kinds == mapped_kinds
