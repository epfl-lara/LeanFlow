"""EPFLemma Lean workflow launch helpers."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from difflib import SequenceMatcher
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from epflemma_cli.project import (
    EPFLemmaProject,
    ProjectNotFoundError,
    discover_epflemma_project,
)
from epflemma_cli.formalization_documents import (
    FormalizationDocumentContext,
    ensure_formalization_blueprint_skill,
    prepare_formalization_document_context,
)
from epflemma_cli.skill_core import default_workflow_skill
from epflemma_cli.runtime_provider import resolve_runtime_provider


WORKFLOW_ALIAS_MAP = {
    "/draft": ("draft", "/draft", "/draft"),
    "/review": ("review", "/review", "/review"),
    "/checkpoint": ("checkpoint", "/checkpoint", "/checkpoint"),
    "/refactor": ("refactor", "/refactor", "/refactor"),
    "/golf": ("golf", "/golf", "/golf"),
    "/prove": ("prove", "/prove", "/prove"),
    "/formalize": ("formalize", "/formalize", "/formalize"),
    "/autoprove": ("prove", "/prove", "/prove"),
    "/autoformalize": ("formalize", "/formalize", "/formalize"),
}


FORGIVING_WORKFLOW_ALIAS_MAP = {
    "draft": "/draft",
    "review": "/review",
    "checkpoint": "/checkpoint",
    "refactor": "/refactor",
    "golf": "/golf",
    "prove": "/prove",
    "autoprove": "/prove",
    "formalize": "/formalize",
    "autoformalize": "/formalize",
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
    additional_skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class NativeLaunchPlan:
    project: EPFLemmaProject
    workflow: NativeWorkflowSpec
    runtime: dict[str, Any]
    child_env: dict[str, str]
    argv: list[str]
    active_skill: str
    toolset_name: str
    additional_skills: tuple[str, ...] = ()
    formalization_document: FormalizationDocumentContext | None = None


def _project_lean_files(project_root: Path) -> list[Path]:
    if not project_root.is_dir():
        return []
    skipped = {".lake", ".git", ".epflemma", "build"}
    return [
        path
        for path in project_root.rglob("*.lean")
        if not any(part in skipped for part in path.parts)
    ]


def _candidate_path_strings(candidate: Path, project_root: Path) -> list[str]:
    values: list[str] = []
    try:
        relative = str(candidate.resolve().relative_to(project_root.resolve()))
        values.append(relative)
    except Exception:
        pass
    values.append(candidate.name)
    values.append(str(candidate))
    deduped: list[str] = []
    for value in values:
        if value and value not in deduped:
            deduped.append(value)
    return deduped


def _recover_similar_project_file(project_root: Path, raw: str) -> str:
    raw = str(raw or "").strip()
    if not raw:
        return ""
    target_name = Path(raw).name
    target_suffix = raw.lstrip("./")
    best_score = 0.0
    best_match = ""
    for candidate in _project_lean_files(project_root):
        strings = _candidate_path_strings(candidate, project_root)
        score = max(SequenceMatcher(None, target_suffix, value).ratio() for value in strings)
        if target_name and candidate.name == target_name:
            score += 0.35
        if target_suffix and any(value.endswith(target_suffix) for value in strings):
            score += 0.2
        if score <= best_score:
            continue
        try:
            best_match = str(candidate.resolve().relative_to(project_root.resolve()))
        except Exception:
            best_match = str(candidate.resolve())
        best_score = score
    return best_match if best_score >= 0.72 else ""


def _normalize_requested_active_file(project_root: Path, cwd: Path, workflow_args: str) -> str:
    raw = str(workflow_args or "").strip()
    if not raw or not raw.endswith(".lean"):
        return ""
    candidates = [
        Path(raw).expanduser(),
        cwd / raw,
        project_root / raw,
    ]
    project_name = project_root.name
    trimmed = raw
    for prefix in (f"./{project_name}/", f"{project_name}/"):
        if trimmed.startswith(prefix):
            trimmed = trimmed[len(prefix):]
            break
    if trimmed != raw:
        candidates.extend([cwd / trimmed, project_root / trimmed, Path(trimmed).expanduser()])
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if resolved.is_file():
            try:
                return str(resolved.relative_to(project_root.resolve()))
            except Exception:
                return str(resolved)
    return _recover_similar_project_file(project_root, trimmed if trimmed != raw else raw)


def _normalize_workflow_args(project_root: Path, cwd: Path, workflow_args: str) -> str:
    raw = str(workflow_args or "").strip()
    if not raw:
        return raw
    normalized_active_file = _normalize_requested_active_file(project_root, cwd, raw)
    if normalized_active_file:
        return normalized_active_file
    return raw


def describe_launch_plan(plan: NativeLaunchPlan) -> dict[str, str]:
    runtime_model = str(plan.runtime.get("model") or plan.child_env.get("EPFLEMMA_NATIVE_MODEL", "") or "")
    runtime_name = str(plan.runtime.get("runtime", "") or "")
    provider = str(plan.runtime.get("provider", "") or "")
    provider_label = provider
    if provider == "local" and runtime_name:
        provider_label = f"local:{runtime_name}"
    frontend_command = plan.workflow.canonical_command
    if plan.workflow.workflow_args:
        frontend_command = f"{frontend_command} {plan.workflow.workflow_args}"
    summary = {
        "workflow": plan.workflow.canonical_command.lstrip("/"),
        "command": frontend_command,
        "project": plan.project.label,
        "project_root": str(plan.project.root),
        "provider": provider_label,
        "base_url": str(plan.runtime.get("base_url", "") or ""),
        "model": runtime_model,
        "skill": plan.active_skill,
        "agents": str(plan.workflow.parallel_agents),
    }
    if plan.formalization_document is not None:
        request_relative = str(
            plan.formalization_document.metadata.get(
                "document_request_relative",
                plan.formalization_document.source_relative,
            )
            or plan.formalization_document.source_relative
        )
        request_kind = str(plan.formalization_document.metadata.get("document_request_kind", "file") or "file")
        if request_relative != plan.formalization_document.source_relative or request_kind != "file":
            summary["input"] = f"{request_relative} ({request_kind})"
        summary["document"] = plan.formalization_document.source_relative
        summary["target_file"] = plan.formalization_document.target_lean_relative
        summary["planner_context"] = str(plan.formalization_document.context_path)
    if plan.additional_skills:
        summary["additional_skills"] = ", ".join(plan.additional_skills)
    if plan.workflow.explicit_goal:
        summary["prompt"] = plan.workflow.explicit_goal
    return summary


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
    no_parallel = False
    explicit_goal = ""
    additional_skills: list[str] = []
    workflow_tokens: list[str] = []
    idx = 0
    while idx < len(remaining):
        token = remaining[idx]
        if token in {"--no-parallel", "-no-parallel"}:
            no_parallel = True
            idx += 1
            continue
        if token == "--agents":
            if idx + 1 >= len(remaining):
                raise ValueError("--agents requires a value")
            try:
                parallel_agents = max(1, int(remaining[idx + 1]))
            except ValueError as exc:
                raise ValueError("--agents must be an integer") from exc
            idx += 2
            continue
        if token in {"--prompt", "--goal"}:
            if idx + 1 >= len(remaining):
                raise ValueError(f"{token} requires a value")
            explicit_goal = " ".join(remaining[idx + 1:]).strip()
            idx = len(remaining)
            continue
        if token in {"--additional-skill", "--additional_skill"}:
            if idx + 1 >= len(remaining):
                raise ValueError(f"{token} requires a value")
            additional_skills.append(remaining[idx + 1])
            idx += 2
            continue
        workflow_tokens.append(token)
        idx += 1
    if no_parallel:
        parallel_agents = 1
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
        additional_skills=tuple(additional_skills),
    )


def _native_runner_module() -> str:
    return "epflemma_cli.native_runner"


def _dedupe_skills(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def resolve_workflow_request(
    command: str,
    *,
    active_cwd: str | os.PathLike[str] | None = None,
    requested_provider: str | None = None,
    active_skill: str | None = None,
) -> NativeLaunchPlan:
    workflow = parse_workflow_command(command)
    cwd = Path(active_cwd or os.getcwd()).expanduser().resolve()
    project = discover_epflemma_project(cwd)
    runtime = resolve_runtime_provider(requested=requested_provider)
    formalization_document: FormalizationDocumentContext | None = None
    normalized_workflow_args = _normalize_workflow_args(project.root, cwd, workflow.workflow_args)
    if workflow.workflow_kind == "formalize":
        formalization_document = prepare_formalization_document_context(
            project_root=project.root,
            cwd=cwd,
            workflow_args=workflow.workflow_args,
            project_label=project.label,
        )
        normalized_workflow_args = formalization_document.source_relative
    if normalized_workflow_args != workflow.workflow_args:
        workflow = replace(
            workflow,
            workflow_args=normalized_workflow_args,
            backend_command=(
                workflow.backend_command.rsplit(" ", 1)[0]
                if workflow.workflow_args
                else workflow.backend_command
            )
            if not normalized_workflow_args
            else f"{WORKFLOW_ALIAS_MAP[workflow.frontend_command][2]} {normalized_workflow_args}",
        )
    normalized_active_file = _normalize_requested_active_file(project.root, cwd, workflow.workflow_args)
    if formalization_document is not None:
        normalized_active_file = formalization_document.target_lean_relative
    if normalized_active_file and workflow.workflow_kind == "prove" and workflow.parallel_agents > 1:
        workflow = replace(workflow, parallel_agents=1)
    selected_skill = (active_skill or "").strip() or default_workflow_skill(workflow.workflow_kind)
    if workflow.parallel_agents > 1 and not active_skill:
        selected_skill = "lean-autonomous-swarm"
    additional_skills = list(workflow.additional_skills)
    if formalization_document is not None:
        additional_skills.append(str(formalization_document.blueprint_skill_path))
    elif workflow.workflow_kind == "prove" and normalized_active_file:
        blueprint_skill = ensure_formalization_blueprint_skill(
            project_root=project.root,
            target_lean_relative=normalized_active_file,
        )
        if blueprint_skill is not None:
            additional_skills.append(str(blueprint_skill))
    additional_skills_tuple = _dedupe_skills(additional_skills)
    toolset_name = "epflemma-native-swarm" if workflow.parallel_agents > 1 else "epflemma-native"
    agent_max_turns = load_agent_max_turns()

    child_env = dict(os.environ)
    child_env.setdefault("AGENT_MAX_TURNS", agent_max_turns)
    child_env.update(
        {
            "EPFLEMMA_PROJECT_ROOT": str(project.root),
            "OPENGAUSS_PROJECT_ROOT": str(project.root),
            "EPFLEMMA_NATIVE_PROVIDER": str(runtime["provider"]),
            "OPENGAUSS_NATIVE_PROVIDER": str(runtime["provider"]),
            "EPFLEMMA_NATIVE_API_MODE": str(runtime["api_mode"]),
            "OPENGAUSS_NATIVE_API_MODE": str(runtime["api_mode"]),
            "EPFLEMMA_NATIVE_BASE_URL": str(runtime["base_url"]),
            "OPENGAUSS_NATIVE_BASE_URL": str(runtime["base_url"]),
            "EPFLEMMA_NATIVE_API_KEY": str(runtime.get("api_key", "")),
            "OPENGAUSS_NATIVE_API_KEY": str(runtime.get("api_key", "")),
            "EPFLEMMA_NATIVE_MODEL": str(runtime.get("model") or load_default_model()),
            "OPENGAUSS_NATIVE_MODEL": str(runtime.get("model") or load_default_model()),
            "EPFLEMMA_NATIVE_WORKFLOW_KIND": workflow.workflow_kind,
            "OPENGAUSS_NATIVE_WORKFLOW_KIND": workflow.workflow_kind,
            "EPFLEMMA_NATIVE_WORKFLOW_COMMAND": workflow.backend_command,
            "OPENGAUSS_NATIVE_WORKFLOW_COMMAND": workflow.backend_command,
            "EPFLEMMA_NATIVE_ACTIVE_SKILL": selected_skill,
            "OPENGAUSS_NATIVE_ACTIVE_SKILL": selected_skill,
            "EPFLEMMA_NATIVE_ADDITIONAL_SKILLS": os.pathsep.join(additional_skills_tuple),
            "OPENGAUSS_NATIVE_ADDITIONAL_SKILLS": os.pathsep.join(additional_skills_tuple),
            "EPFLEMMA_NATIVE_PARALLEL_AGENTS": str(workflow.parallel_agents),
            "OPENGAUSS_NATIVE_PARALLEL_AGENTS": str(workflow.parallel_agents),
            "EPFLEMMA_NATIVE_USER_APPROVED_SWARM": "1" if workflow.parallel_agents > 1 else "0",
            "OPENGAUSS_NATIVE_USER_APPROVED_SWARM": "1" if workflow.parallel_agents > 1 else "0",
            "EPFLEMMA_NATIVE_EXPLICIT_GOAL": workflow.explicit_goal,
            "OPENGAUSS_NATIVE_EXPLICIT_GOAL": workflow.explicit_goal,
            "EPFLEMMA_NATIVE_USER_PROMPT": workflow.explicit_goal,
            "OPENGAUSS_NATIVE_USER_PROMPT": workflow.explicit_goal,
            "EPFLEMMA_NATIVE_EFFECTIVE_PROMPT": workflow.explicit_goal,
            "OPENGAUSS_NATIVE_EFFECTIVE_PROMPT": workflow.explicit_goal,
            "EPFLEMMA_NATIVE_TOOLSET": toolset_name,
            "OPENGAUSS_NATIVE_TOOLSET": toolset_name,
            "EPFLEMMA_NATIVE_ACTIVE_FILE": normalized_active_file,
            "OPENGAUSS_NATIVE_ACTIVE_FILE": normalized_active_file,
        }
    )
    if formalization_document is not None:
        child_env.update(formalization_document.to_env())
    argv = [sys.executable, "-m", _native_runner_module()]
    return NativeLaunchPlan(
        project=project,
        workflow=workflow,
        runtime=runtime,
        child_env=child_env,
        argv=argv,
        active_skill=selected_skill,
        additional_skills=additional_skills_tuple,
        toolset_name=toolset_name,
        formalization_document=formalization_document,
    )


def load_default_model() -> str:
    from epflemma_cli.config import load_config

    config = load_config()
    model_cfg = config.get("model")
    if isinstance(model_cfg, Mapping):
        return str(model_cfg.get("default", "moonshotai/Kimi-K2.6") or "moonshotai/Kimi-K2.6")
    if isinstance(model_cfg, str) and model_cfg.strip():
        return model_cfg.strip()
    return "moonshotai/Kimi-K2.6"


def load_agent_max_turns() -> str:
    from epflemma_cli.config import load_config

    config = load_config()
    agent_cfg = config.get("agent")
    if isinstance(agent_cfg, Mapping):
        try:
            value = int(agent_cfg.get("max_turns", 180) or 180)
        except Exception:
            value = 180
        return str(max(1, value))
    return "180"


def spawn_workflow(
    command: str,
    *,
    active_cwd: str | os.PathLike[str] | None = None,
    requested_provider: str | None = None,
    active_skill: str | None = None,
    interactive: bool = False,
) -> tuple[NativeLaunchPlan, subprocess.Popen[bytes]]:
    plan = resolve_workflow_request(
        command,
        active_cwd=active_cwd,
        requested_provider=requested_provider,
        active_skill=active_skill,
    )
    child_env = dict(plan.child_env)
    child_env["EPFLEMMA_NATIVE_INTERACTIVE"] = "1" if interactive else "0"
    child_env["OPENGAUSS_NATIVE_INTERACTIVE"] = "1" if interactive else "0"
    process = subprocess.Popen(
        plan.argv,
        cwd=str(plan.project.root),
        env=child_env,
        stdin=subprocess.DEVNULL if not interactive else None,
        stdout=subprocess.DEVNULL if not interactive else None,
        stderr=subprocess.DEVNULL if not interactive else None,
        start_new_session=not interactive,
    )
    return plan, process


def run_workflow(
    command: str,
    *,
    active_cwd: str | os.PathLike[str] | None = None,
    requested_provider: str | None = None,
    active_skill: str | None = None,
) -> int:
    plan, process = spawn_workflow(
        command,
        active_cwd=active_cwd,
        requested_provider=requested_provider,
        active_skill=active_skill,
        interactive=True,
    )
    process.wait()
    return process.returncode
