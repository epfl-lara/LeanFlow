"""OpenGauss Lean workflow launch helpers."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from opengauss_cli.project import (
    OpenGaussProject,
    ProjectNotFoundError,
    discover_opengauss_project,
)
from opengauss_cli.skill_core import default_workflow_skill
from opengauss_cli.runtime_provider import resolve_runtime_provider


WORKFLOW_ALIAS_MAP = {
    "/prove": ("prove", "/prove", "/lean4:prove"),
    "/draft": ("draft", "/draft", "/lean4:draft"),
    "/review": ("review", "/review", "/lean4:review"),
    "/checkpoint": ("checkpoint", "/checkpoint", "/lean4:checkpoint"),
    "/refactor": ("refactor", "/refactor", "/lean4:refactor"),
    "/golf": ("golf", "/golf", "/lean4:golf"),
    "/autoprove": ("autoprove", "/autoprove", "/lean4:autoprove"),
    "/formalize": ("formalize", "/formalize", "/lean4:formalize"),
    "/autoformalize": ("autoformalize", "/autoformalize", "/lean4:autoformalize"),
}


FORGIVING_WORKFLOW_ALIAS_MAP = {
    "prove": "/prove",
    "draft": "/draft",
    "review": "/review",
    "checkpoint": "/checkpoint",
    "refactor": "/refactor",
    "golf": "/golf",
    "autoprove": "/autoprove",
    "formalize": "/formalize",
    "autoformalize": "/autoformalize",
}


@dataclass(frozen=True)
class NativeWorkflowSpec:
    workflow_kind: str
    frontend_command: str
    canonical_command: str
    backend_command: str
    workflow_args: str
    parallel_agents: int = 1
    explicit_goal: str = ""


@dataclass(frozen=True)
class NativeLaunchPlan:
    project: OpenGaussProject
    workflow: NativeWorkflowSpec
    runtime: dict[str, Any]
    child_env: dict[str, str]
    argv: list[str]
    active_skill: str
    toolset_name: str


def describe_launch_plan(plan: NativeLaunchPlan) -> dict[str, str]:
    runtime_model = str(plan.runtime.get("model") or plan.child_env.get("OPENGAUSS_NATIVE_MODEL", "") or "")
    runtime_name = str(plan.runtime.get("runtime", "") or "")
    provider = str(plan.runtime.get("provider", "") or "")
    provider_label = provider
    if provider == "local" and runtime_name:
        provider_label = f"local:{runtime_name}"
    return {
        "workflow": plan.workflow.workflow_kind,
        "command": plan.workflow.backend_command,
        "project": plan.project.label,
        "project_root": str(plan.project.root),
        "provider": provider_label,
        "base_url": str(plan.runtime.get("base_url", "") or ""),
        "model": runtime_model,
        "skill": plan.active_skill,
        "agents": str(plan.workflow.parallel_agents),
    }


def rewrite_forgiving_workflow_command(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return text
    first, _, rest = text.partition(" ")
    if first in FORGIVING_WORKFLOW_ALIAS_MAP:
        canonical = FORGIVING_WORKFLOW_ALIAS_MAP[first]
        return canonical if not rest else f"{canonical} {rest}"
    return text


def parse_workflow_command(command: str) -> NativeWorkflowSpec:
    normalized = rewrite_forgiving_workflow_command(command)
    if not normalized:
        raise ValueError("workflow command must not be empty")
    parts = shlex.split(normalized)
    command_name = parts[0]
    if command_name not in WORKFLOW_ALIAS_MAP:
        raise ValueError(f"unsupported workflow command: {command_name}")
    remaining = parts[1:]
    parallel_agents = 1
    explicit_goal = ""
    workflow_tokens: list[str] = []
    idx = 0
    while idx < len(remaining):
        token = remaining[idx]
        if token == "--agents":
            if idx + 1 >= len(remaining):
                raise ValueError("--agents requires a value")
            try:
                parallel_agents = max(1, int(remaining[idx + 1]))
            except ValueError as exc:
                raise ValueError("--agents must be an integer") from exc
            idx += 2
            continue
        if token == "--goal":
            if idx + 1 >= len(remaining):
                raise ValueError("--goal requires a value")
            explicit_goal = " ".join(remaining[idx + 1:]).strip()
            idx = len(remaining)
            continue
        workflow_tokens.append(token)
        idx += 1
    workflow_args = " ".join(workflow_tokens).strip()
    workflow_kind, canonical_command, backend_command = WORKFLOW_ALIAS_MAP[command_name]
    return NativeWorkflowSpec(
        workflow_kind=workflow_kind,
        frontend_command=command_name,
        canonical_command=canonical_command,
        backend_command=backend_command if not workflow_args else f"{backend_command} {workflow_args}",
        workflow_args=workflow_args.strip(),
        parallel_agents=parallel_agents,
        explicit_goal=explicit_goal,
    )


def _native_runner_module() -> str:
    return "opengauss_cli.native_runner"


def resolve_workflow_request(
    command: str,
    *,
    active_cwd: str | os.PathLike[str] | None = None,
    requested_provider: str | None = None,
    active_skill: str | None = None,
) -> NativeLaunchPlan:
    workflow = parse_workflow_command(command)
    cwd = Path(active_cwd or os.getcwd()).expanduser().resolve()
    project = discover_opengauss_project(cwd)
    runtime = resolve_runtime_provider(requested=requested_provider)
    selected_skill = (active_skill or "").strip() or default_workflow_skill(workflow.workflow_kind)
    if workflow.parallel_agents > 1 and not active_skill:
        selected_skill = "lean-autonomous-swarm"
    toolset_name = "opengauss-native-swarm" if workflow.parallel_agents > 1 else "opengauss-native"

    child_env = dict(os.environ)
    child_env.update(
        {
            "OPENGAUSS_PROJECT_ROOT": str(project.root),
            "OPENGAUSS_NATIVE_PROVIDER": str(runtime["provider"]),
            "OPENGAUSS_NATIVE_API_MODE": str(runtime["api_mode"]),
            "OPENGAUSS_NATIVE_BASE_URL": str(runtime["base_url"]),
            "OPENGAUSS_NATIVE_API_KEY": str(runtime.get("api_key", "")),
            "OPENGAUSS_NATIVE_MODEL": str(runtime.get("model") or load_default_model()),
            "OPENGAUSS_NATIVE_WORKFLOW_KIND": workflow.workflow_kind,
            "OPENGAUSS_NATIVE_WORKFLOW_COMMAND": workflow.backend_command,
            "OPENGAUSS_NATIVE_ACTIVE_SKILL": selected_skill,
            "OPENGAUSS_NATIVE_PARALLEL_AGENTS": str(workflow.parallel_agents),
            "OPENGAUSS_NATIVE_USER_APPROVED_SWARM": "1" if workflow.parallel_agents > 1 else "0",
            "OPENGAUSS_NATIVE_EXPLICIT_GOAL": workflow.explicit_goal,
            "OPENGAUSS_NATIVE_TOOLSET": toolset_name,
        }
    )
    argv = [sys.executable, "-m", _native_runner_module()]
    return NativeLaunchPlan(
        project=project,
        workflow=workflow,
        runtime=runtime,
        child_env=child_env,
        argv=argv,
        active_skill=selected_skill,
        toolset_name=toolset_name,
    )


def load_default_model() -> str:
    from opengauss_cli.config import load_config

    config = load_config()
    model_cfg = config.get("model")
    if isinstance(model_cfg, Mapping):
        return str(model_cfg.get("default", "google/gemma-4-31B-it") or "google/gemma-4-31B-it")
    if isinstance(model_cfg, str) and model_cfg.strip():
        return model_cfg.strip()
    return "google/gemma-4-31B-it"


def run_workflow(
    command: str,
    *,
    active_cwd: str | os.PathLike[str] | None = None,
    requested_provider: str | None = None,
    active_skill: str | None = None,
) -> int:
    plan = resolve_workflow_request(
        command,
        active_cwd=active_cwd,
        requested_provider=requested_provider,
        active_skill=active_skill,
    )
    process = subprocess.run(plan.argv, cwd=str(plan.project.root), env=plan.child_env, check=False)
    return process.returncode
