"""Pin the argv contract the VS Code extension builds against the CLI parser.

The extension constructs `leanflow workflow` arguments in TypeScript
(`vscode-extension/src/core/launch.ts`) and cannot import this parser. These
cases are the same ones asserted on the TypeScript side in
`vscode-extension/test/launch.test.mjs`; keeping both means a change to either
end that breaks the other fails a test instead of silently launching a run that
does something the form did not say.
"""

from __future__ import annotations

import shlex
from types import SimpleNamespace

import pytest

from leanflow_cli.workflow import (
    NativeLaunchPlan,
    NativeWorkflowSpec,
    _redacted_env_value,
    launch_plan_payload,
    parse_workflow_command,
)


def _spec(argv: list[str]):
    """Parse argv the way `leanflow workflow <argv>` does."""
    kind, *rest = argv
    return parse_workflow_command(shlex.join([f"/{kind}", *rest]))


def test_bare_prove_run() -> None:
    spec = _spec(["prove"])
    assert spec.workflow_kind == "prove"
    assert spec.workflow_args == ""
    assert spec.parallel_agents == 1
    assert spec.research_mode is False


def test_file_scoped_target_is_positional() -> None:
    spec = _spec(["prove", "IMO2026/P1.lean"])
    assert spec.workflow_args == "IMO2026/P1.lean"
    assert spec.backend_command == "/prove IMO2026/P1.lean"


def test_provider_and_model_overrides() -> None:
    spec = _spec(["prove", "--provider", "codex", "--model", "gpt-5.6-terra"])
    assert spec.provider_override == "codex"
    assert spec.model_override == "gpt-5.6-terra"


def test_research_without_worker_count_uses_the_default() -> None:
    spec = _spec(["prove", "--research"])
    assert spec.research_mode is True
    assert spec.research_workers == 2


@pytest.mark.parametrize("workers,expected", [("0", 0), ("3", 3)])
def test_research_worker_count_is_honoured(workers: str, expected: int) -> None:
    spec = _spec(["prove", "--research", "--research-workers", workers])
    assert spec.research_mode is True
    assert spec.research_workers == expected


def test_no_parallel_forces_a_single_agent() -> None:
    spec = _spec(["prove", "--no-parallel"])
    assert spec.no_parallel is True
    assert spec.parallel_agents == 1


def test_agents_opts_into_swarm() -> None:
    spec = _spec(["prove", "--agents", "4"])
    assert spec.parallel_agents == 4


def test_prompt_is_last_and_keeps_its_whole_value() -> None:
    """--prompt consumes the rest of the line, so the extension must emit it last."""
    spec = _spec(
        [
            "prove",
            "Main.lean",
            "--clean-room",
            "--axioms",
            "Classical.choice",
            "--prompt",
            "try factorization first",
        ]
    )
    assert spec.explicit_goal == "try factorization first"
    assert spec.clean_room is True
    assert spec.allowed_axioms == "Classical.choice"
    assert spec.workflow_args == "Main.lean"


def test_additional_skills_repeat_the_flag() -> None:
    spec = _spec(["prove", "--additional-skill", "a.md", "--additional-skill", "b.md"])
    assert spec.additional_skills == ("a.md", "b.md")


def test_human_review_flag() -> None:
    assert _spec(["prove", "--human-review"]).human_review is True


def test_every_workflow_kind_the_extension_offers_is_routable() -> None:
    """The extension's workflow picker must not offer a command the CLI rejects."""
    for kind in ("prove", "formalize", "review", "refactor", "golf", "draft"):
        assert _spec([kind]).workflow_kind == kind


def test_launch_preview_redacts_credentials_embedded_in_urls() -> None:
    raw = (
        "https://preview-user:preview-password@example.test/v1"
        "?api-version=1&accessToken=preview-query-secret"
        "#access_token=preview-fragment-secret"
    )

    redacted = _redacted_env_value("LEANFLOW_NATIVE_BASE_URL", raw)
    embedded = _redacted_env_value("LEANFLOW_NATIVE_COMMAND", f"curl {raw} --fail")

    assert "example.test/v1" in redacted
    assert "api-version=1" in redacted
    for secret in (
        "preview-user",
        "preview-password",
        "preview-query-secret",
        "preview-fragment-secret",
    ):
        assert secret not in redacted
        assert secret not in embedded


def test_launch_preview_sanitizes_runtime_urls_without_losing_endpoint_identity(
    tmp_path,
) -> None:
    base_url = (
        "https://preview-user:preview-password@example.test/v1"
        "?api-version=2026-08-01&accessToken=preview-query-secret"
    )
    workflow = NativeWorkflowSpec(
        workflow_kind="prove",
        frontend_command="/prove",
        canonical_command="/prove",
        backend_command="/prove Main.lean",
        workflow_args="Main.lean",
        explicit_goal="prove with sk-preview-prompt-secret",
        expert_provider="codex",
        expert_command_template="codex exec sk-expert-template-secret",
        blueprint_verifier_provider="claude-code",
        blueprint_verifier_command_template="claude sk-blueprint-template-secret",
        autoformalizer_verifier_provider="codex",
        autoformalizer_verifier_command_template="codex sk-autoformalizer-template-secret",
    )
    project = SimpleNamespace(label="Demo", root=tmp_path, lean_root=tmp_path)
    plan = NativeLaunchPlan(
        project=project,
        workflow=workflow,
        runtime={
            "provider": "custom",
            "api_mode": "chat_completions",
            "base_url": base_url,
            "api_key": "runtime-api-key-secret",
            "model": "model-a",
        },
        child_env={
            "LEANFLOW_KNOB_ALPHA": "enabled",
            "LEANFLOW_NATIVE_API_KEY": "runtime-api-key-secret",
        },
        argv=["python", "-m", "leanflow_cli.native.native_runner"],
        active_skill="lean-proof-loop",
        toolset_name="leanflow-native",
    )

    payload = launch_plan_payload(plan)
    serialized = str(payload)

    assert payload["runtime"]["api_key"] == "[redacted]"
    assert payload["env_effective"]["LEANFLOW_KNOB_ALPHA"] == "enabled"
    assert payload["env_effective"]["LEANFLOW_NATIVE_API_KEY"] == "[redacted]"
    assert "example.test/v1" in payload["runtime"]["base_url"]
    assert "api-version=2026-08-01" in payload["runtime"]["base_url"]
    assert payload["summary"]["base_url"] == payload["runtime"]["base_url"]
    assert payload["workflow"]["explicit_goal"].startswith("[content-sha256:")
    for key in (
        "prompt",
        "expert_command_template",
        "blueprint_verifier_command_template",
        "autoformalizer_verifier_command_template",
    ):
        assert payload["summary"][key].startswith("[content-sha256:")
    for secret in (
        "preview-user",
        "preview-password",
        "preview-query-secret",
        "runtime-api-key-secret",
        "sk-preview-prompt-secret",
        "sk-expert-template-secret",
        "sk-blueprint-template-secret",
        "sk-autoformalizer-template-secret",
    ):
        assert secret not in serialized


@pytest.mark.parametrize("flag", ["--research", "--clean-room", "--human-review"])
def test_prove_only_flags_are_rejected_elsewhere(flag: str) -> None:
    """The extension disables these outside prove; the CLI is the backstop."""
    with pytest.raises(ValueError):
        _spec(["golf", flag])
