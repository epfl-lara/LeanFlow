"""Shared Lean workflow services for native EPFLemma workflows and tools."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from epflemma_cli.file_locks import acquire_file_lock as _acquire_file_lock
from epflemma_cli.file_locks import list_file_locks as _list_file_locks
from epflemma_cli.project import (
    ProjectManifestError,
    ProjectNotFoundError,
    discover_epflemma_project,
    find_lean_project_root,
)
from epflemma_cli.workflow_state import append_workflow_outcome
from epflemma_cli.lean_workflow_specs import get_lean_spec, list_specs


STANDARD_AXIOMS = {"propext", "Quot.sound", "Classical.choice"}
SEARCH_PROVIDER_LABELS = {
    "local_search": "mcp-local-search",
    "leanfinder": "mcp-leanfinder",
    "leansearch": "mcp-leansearch",
    "loogle": "mcp-loogle",
    "project_rg": "project-rg",
    "mathlib_rg": "mathlib-rg",
}


@dataclass(frozen=True)
class LeanCapabilityReport:
    cwd: str
    project_root: str
    project_valid: bool
    project_error: str
    binaries: dict[str, bool]
    mcp_tools: dict[str, str]
    search_providers: list[str]
    helper_tools: dict[str, bool]
    workers: list[str]
    degraded_reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanSorryFinding:
    file: str
    line: int
    declaration: str
    preview: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanInspection:
    target: str
    project_root: str
    diagnostics: str
    goals: str
    sorry_count: int | None
    project_sorry_count: int | None
    blocker_kind: str
    queue_items: list[dict[str, Any]] = field(default_factory=list)
    capability_report: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanVerificationResult:
    ok: bool
    mode: str
    command: str
    target: str
    output: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanSearchResult:
    query: str
    mode: str
    attempted_providers: list[str]
    results: list[dict[str, Any]]
    degraded_reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanAxiomReport:
    target: str
    file_path: str
    ok: bool
    axioms: list[str]
    custom_axioms: list[str]
    classical: bool
    choice: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkflowRouteDecision:
    workflow_kind: str
    skill_name: str
    route_action: str
    blocker_kind: str
    recommended_worker: str
    search_exhausted: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanWorkerRequest:
    worker: str
    goal: str
    context: str
    file_path: str = ""
    line: int | None = None
    use_file_lock: bool = True
    allow_delegation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeanWorkerResult:
    worker: str
    mode: str
    dispatched: bool
    summary: str
    result: Any = None
    lock: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _project_root(cwd: str | os.PathLike[str] | None = None) -> tuple[Path | None, str]:
    base = Path(cwd or os.getcwd()).expanduser().resolve()
    try:
        project = discover_epflemma_project(base)
        return project.root, ""
    except (ProjectNotFoundError, ProjectManifestError) as exc:
        lean_root = find_lean_project_root(base)
        if lean_root is not None:
            return lean_root, str(exc)
        return None, str(exc)


def _run_command(cmd: list[str], *, cwd: Path | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
            check=False,
        )
        return result.returncode, result.stdout.strip()
    except Exception as exc:
        return 1, str(exc)


def _tool_parameter_names(tool_name: str) -> set[str]:
    try:
        from tools.registry import registry

        entry = registry._tools.get(tool_name)  # type: ignore[attr-defined]
    except Exception:
        entry = None
    if entry is None:
        return set()
    schema = getattr(entry, "schema", {}) or {}
    parameters = schema.get("parameters", {}) if isinstance(schema, Mapping) else {}
    properties = parameters.get("properties", {}) if isinstance(parameters, Mapping) else {}
    if isinstance(properties, Mapping):
        return {str(key) for key in properties.keys()}
    return set()


def _invoke_json_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from model_tools import handle_function_call

    accepted = _tool_parameter_names(tool_name)
    if accepted:
        filtered = {
            key: value
            for key, value in arguments.items()
            if key in accepted and value not in (None, "")
        }
    else:
        filtered = {key: value for key, value in arguments.items() if value not in (None, "")}
    raw = handle_function_call(tool_name, filtered)
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {"raw": raw}


def _discover_lean_mcp_tools() -> dict[str, str]:
    try:
        from tools.mcp_tool import discover_mcp_tools

        discover_mcp_tools()
    except Exception:
        pass

    try:
        from tools.registry import registry

        tool_names = registry.get_all_tool_names()
    except Exception:
        tool_names = []

    discovered = {
        "diagnostics": "",
        "goals": "",
        "code_actions": "",
        "multi_attempt": "",
        "run_code": "",
        "local_search": "",
        "leanfinder": "",
        "leansearch": "",
        "loogle": "",
    }
    for tool_name in tool_names:
        lowered = tool_name.lower()
        if "lean" not in lowered:
            continue
        if not discovered["diagnostics"] and any(token in lowered for token in ("diagnostic", "message")):
            discovered["diagnostics"] = tool_name
        if not discovered["goals"] and any(token in lowered for token in ("goal", "proof")):
            discovered["goals"] = tool_name
        if not discovered["code_actions"] and "code_action" in lowered:
            discovered["code_actions"] = tool_name
        if not discovered["multi_attempt"] and "multi_attempt" in lowered:
            discovered["multi_attempt"] = tool_name
        if not discovered["run_code"] and "run_code" in lowered:
            discovered["run_code"] = tool_name
        if not discovered["local_search"] and "local_search" in lowered:
            discovered["local_search"] = tool_name
        if not discovered["leanfinder"] and "leanfinder" in lowered:
            discovered["leanfinder"] = tool_name
        if not discovered["leansearch"] and "leansearch" in lowered:
            discovered["leansearch"] = tool_name
        if not discovered["loogle"] and "loogle" in lowered:
            discovered["loogle"] = tool_name
    return discovered


def _helper_tools() -> dict[str, bool]:
    return {
        "sorry_analyzer": True,
        "axiom_checker": True,
        "error_parser": True,
        "search_fallback": True,
        "solver_cascade": True,
        "usage_search": True,
        "instance_search": True,
        "golf_candidates": True,
    }


def probe_capabilities(cwd: str | os.PathLike[str] | None = None) -> LeanCapabilityReport:
    base = Path(cwd or os.getcwd()).expanduser().resolve()
    project_root, project_error = _project_root(base)
    binaries = {name: bool(shutil.which(name)) for name in ("lean", "lake", "elan", "git", "rg")}
    mcp_tools = _discover_lean_mcp_tools()
    search_providers: list[str] = []
    for key in ("leanfinder", "local_search", "leansearch", "loogle"):
        if mcp_tools.get(key):
            search_providers.append(SEARCH_PROVIDER_LABELS[key])
    if binaries.get("rg"):
        search_providers.append(SEARCH_PROVIDER_LABELS["project_rg"])
        if project_root and (project_root / ".lake" / "packages" / "mathlib").is_dir():
            search_providers.append(SEARCH_PROVIDER_LABELS["mathlib_rg"])
    degraded: list[str] = []
    if not binaries.get("lean"):
        degraded.append("lean binary unavailable")
    if not binaries.get("lake"):
        degraded.append("lake binary unavailable")
    if project_root is None:
        degraded.append("lean project not detected")
    if not mcp_tools.get("diagnostics"):
        degraded.append("lean diagnostics MCP unavailable")
    if not search_providers:
        degraded.append("no search providers available")
    return LeanCapabilityReport(
        cwd=str(base),
        project_root=str(project_root or ""),
        project_valid=project_root is not None and not project_error,
        project_error=project_error,
        binaries=binaries,
        mcp_tools=mcp_tools,
        search_providers=search_providers,
        helper_tools=_helper_tools(),
        workers=[record.spec_id for record in list_specs("worker")],
        degraded_reasons=degraded,
    )


def _strip_comments_and_strings(text: str) -> str:
    text = re.sub(r"/-.*?-/", "", text, flags=re.DOTALL)
    text = re.sub(r"--.*", "", text)
    text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
    return text


def _count_sorries(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return None
    return len(re.findall(r"\bsorry\b", _strip_comments_and_strings(raw)))


def _project_sorry_stats(project_root: Path | None) -> tuple[int | None, list[str]]:
    if project_root is None or not project_root.is_dir():
        return None, []
    total = 0
    files: list[str] = []
    for path in project_root.rglob("*.lean"):
        if any(part in {".git", ".lake", ".epflemma", "build"} for part in path.parts):
            continue
        count = _count_sorries(path)
        if not count:
            continue
        total += count
        try:
            files.append(str(path.relative_to(project_root)))
        except Exception:
            files.append(str(path))
    return total, files


def _declaration_index(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    pattern = re.compile(
        r"^\s*(?:@[A-Za-z0-9_.]+\s+)*(theorem|lemma|example|def|instance|class|structure)\s+([A-Za-z0-9_'.-]+)?"
    )
    entries: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        match = pattern.match(line)
        if not match:
            continue
        name = (match.group(2) or "").strip()
        if not name:
            continue
        entries.append({"kind": match.group(1), "name": name, "line": line_number})
    for idx, entry in enumerate(entries):
        start = entry["line"]
        end = entries[idx + 1]["line"] - 1 if idx + 1 < len(entries) else len(lines)
        entry["end_line"] = end
        entry["text"] = "\n".join(lines[start - 1:end]).strip()
    return entries


def _find_symbol_line(path: Path, symbol: str | None) -> int | None:
    wanted = str(symbol or "").strip()
    if not wanted:
        return None
    for entry in _declaration_index(path):
        if entry["name"] == wanted:
            return int(entry["line"])
    return None


def _diagnostics_text(file_path: Path, project_root: Path | None, mcp_tools: Mapping[str, str]) -> str:
    diagnostics_tool = str(mcp_tools.get("diagnostics", "") or "")
    if diagnostics_tool:
        payload = _invoke_json_tool(
            diagnostics_tool,
            {"file_path": str(file_path), "path": str(file_path)},
        )
        fragments = [str(value).strip() for value in payload.values() if isinstance(value, str) and value.strip()]
        if fragments:
            return "\n".join(fragments[:8])
    if project_root is None:
        return "Lean project unavailable."
    try:
        relative = str(file_path.resolve().relative_to(project_root.resolve()))
    except Exception:
        relative = str(file_path)
    _, output = _run_command(["lake", "env", "lean", relative], cwd=project_root)
    return output or "no diagnostics available"


def _goals_text(
    file_path: Path,
    project_root: Path | None,
    mcp_tools: Mapping[str, str],
    *,
    line: int | None = None,
    symbol: str | None = None,
) -> str:
    goals_tool = str(mcp_tools.get("goals", "") or "")
    if goals_tool:
        payload = _invoke_json_tool(
            goals_tool,
            {
                "file_path": str(file_path),
                "path": str(file_path),
                "line": line or _find_symbol_line(file_path, symbol) or 1,
            },
        )
        fragments = [str(value).strip() for value in payload.values() if isinstance(value, str) and value.strip()]
        if fragments:
            return "\n".join(fragments[:8])
    return "Lean goals unavailable."


def classify_blocker_kind(text: str) -> str:
    lowered = str(text or "").lower()
    if not lowered.strip():
        return "none"
    patterns = {
        "axiom-risk": ("axiom", "#print axioms", "classical.choice"),
        "unknown_ident": ("unknown constant", "unknown identifier", "unknown namespace"),
        "synth_instance": ("failed to synthesize", "type class", "instance"),
        "type_mismatch": ("type mismatch", "application type mismatch"),
        "timeout": ("timeout", "maximum recursion depth", "maximum number of heartbeats"),
        "open_goals": ("⊢", "unsolved goals", "goal"),
        "sorry": ("sorry",),
        "warnings": ("warning:",),
    }
    for name, tokens in patterns.items():
        if any(token in lowered for token in tokens):
            return name
    return "diagnostics"


def lean_sorries(scope: str = "project", target: str = "", cwd: str | os.PathLike[str] | None = None) -> list[LeanSorryFinding]:
    project_root, _ = _project_root(cwd)
    if scope == "file" and target:
        paths = [Path(target).expanduser().resolve()]
    elif project_root:
        paths = [
            path
            for path in project_root.rglob("*.lean")
            if not any(part in {".git", ".lake", ".epflemma", "build"} for part in path.parts)
        ]
    else:
        paths = []
    findings: list[LeanSorryFinding] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        decl_entries = _declaration_index(path)
        for line_number, line in enumerate(lines, start=1):
            if "sorry" not in line or re.search(r"--.*\bsorry\b", line):
                continue
            declaration = ""
            for entry in decl_entries:
                if int(entry["line"]) <= line_number <= int(entry["end_line"]):
                    declaration = str(entry["name"])
                    break
            findings.append(
                LeanSorryFinding(
                    file=str(path),
                    line=line_number,
                    declaration=declaration,
                    preview=line.strip(),
                )
            )
    return findings


def lean_inspect(
    target: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    line: int | None = None,
    symbol: str | None = None,
) -> LeanInspection:
    report = probe_capabilities(cwd)
    file_path = Path(target).expanduser().resolve()
    project_root = Path(report.project_root).resolve() if report.project_root else None
    diagnostics = _diagnostics_text(file_path, project_root, report.mcp_tools)
    goals = _goals_text(file_path, project_root, report.mcp_tools, line=line, symbol=symbol)
    sorry_count = _count_sorries(file_path)
    project_sorry_count, _ = _project_sorry_stats(project_root)
    queue_items: list[dict[str, Any]] = []
    for entry in _declaration_index(file_path):
        reasons: list[str] = []
        text = str(entry.get("text", "") or "")
        if re.search(r"\bsorry\b", _strip_comments_and_strings(text)):
            reasons.append("contains sorry")
        if reasons:
            queue_items.append(
                {
                    "label": entry["name"],
                    "kind": entry["kind"],
                    "line": entry["line"],
                    "end_line": entry["end_line"],
                    "reasons": reasons,
                    "blocker_signature": hashlib.sha1(
                        f"{file_path}:{entry['line']}:{','.join(reasons)}".encode("utf-8")
                    ).hexdigest()[:12],
                    "search_hints": [entry["name"], entry["kind"]],
                    "verification_gate": f"lake env lean {file_path.name}",
                }
            )
    inspection = LeanInspection(
        target=str(file_path),
        project_root=str(project_root or ""),
        diagnostics=diagnostics,
        goals=goals,
        sorry_count=sorry_count,
        project_sorry_count=project_sorry_count,
        blocker_kind=classify_blocker_kind("\n".join((diagnostics, goals))),
        queue_items=queue_items,
        capability_report=report.to_dict(),
    )
    append_workflow_outcome("lean-inspect", inspection.to_dict())
    return inspection


def _module_name_for_file(project_root: Path, file_path: Path) -> str:
    relative = file_path.resolve().relative_to(project_root.resolve())
    return ".".join(relative.with_suffix("").parts)


def lean_verify(
    target: str = "",
    *,
    cwd: str | os.PathLike[str] | None = None,
    mode: str = "project",
) -> LeanVerificationResult:
    project_root, _ = _project_root(cwd)
    root = Path(project_root) if project_root else None
    normalized_mode = str(mode or "project").strip().lower()
    target_path = Path(target).expanduser().resolve() if target else None
    if normalized_mode == "file_exact" and root and target_path:
        try:
            relative = str(target_path.relative_to(root))
        except Exception:
            relative = str(target_path)
        command = ["lake", "env", "lean", relative]
    elif normalized_mode == "module" and root and target_path:
        command = ["lake", "build", _module_name_for_file(root, target_path)]
    else:
        normalized_mode = "project"
        command = ["lake", "build"]
    code, output = _run_command(command, cwd=root)
    result = LeanVerificationResult(
        ok=code == 0,
        mode=normalized_mode,
        command=" ".join(command),
        target=str(target_path or (root or "")),
        output=output,
    )
    append_workflow_outcome("lean-verify", result.to_dict())
    return result


def _rg_search(root: Path, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    if not shutil.which("rg") or not root.is_dir():
        return []
    code, output = _run_command(
        ["rg", "-n", "-m", str(limit), "--color", "never", query, str(root)],
        cwd=root,
    )
    if code not in {0, 1}:
        return []
    results: list[dict[str, Any]] = []
    for line in output.splitlines()[:limit]:
        path_text, _, rest = line.partition(":")
        line_no, _, preview = rest.partition(":")
        results.append(
            {
                "file": path_text,
                "line": int(line_no) if line_no.isdigit() else None,
                "preview": preview.strip(),
            }
        )
    return results


def lean_search(
    query: str,
    *,
    mode: str = "auto",
    cwd: str | os.PathLike[str] | None = None,
    limit: int = 10,
    file_path: str = "",
) -> LeanSearchResult:
    report = probe_capabilities(cwd)
    attempted: list[str] = []
    results: list[dict[str, Any]] = []

    mcp_order = []
    normalized_mode = str(mode or "auto").strip().lower()
    semantic_provider_keys = ("leanfinder", "leansearch", "loogle")
    semantic_provider_labels = [SEARCH_PROVIDER_LABELS[key] for key in semantic_provider_keys if report.mcp_tools.get(key)]
    if normalized_mode in {"auto", "local"} and report.mcp_tools.get("local_search"):
        mcp_order.append(("local_search", report.mcp_tools["local_search"]))
    if normalized_mode in {"auto", "semantic"} and report.mcp_tools.get("leanfinder"):
        mcp_order.append(("leanfinder", report.mcp_tools["leanfinder"]))
    if normalized_mode in {"auto", "natural-language", "natural"} and report.mcp_tools.get("leansearch"):
        mcp_order.append(("leansearch", report.mcp_tools["leansearch"]))
    if normalized_mode in {"auto", "type-pattern", "type"} and report.mcp_tools.get("loogle"):
        mcp_order.append(("loogle", report.mcp_tools["loogle"]))
    for provider_key, tool_name in mcp_order:
        attempted.append(SEARCH_PROVIDER_LABELS[provider_key])
        payload = _invoke_json_tool(
            tool_name,
            {
                "query": query,
                "q": query,
                "path": file_path,
                "file_path": file_path,
                "limit": limit,
            },
        )
        if payload.get("error"):
            continue
        text_fragments = []
        for key, value in payload.items():
            if isinstance(value, str) and value.strip():
                text_fragments.append(value.strip())
            elif isinstance(value, list):
                for item in value[:limit]:
                    text_fragments.append(str(item))
        if text_fragments:
            results.extend(
                {
                    "provider": SEARCH_PROVIDER_LABELS[provider_key],
                    "match": fragment[:400],
                }
                for fragment in text_fragments[:limit]
            )
            break

    root = Path(report.project_root) if report.project_root else None
    if not results and root:
        attempted.append(SEARCH_PROVIDER_LABELS["project_rg"])
        for match in _rg_search(root, query, limit=limit):
            results.append({"provider": SEARCH_PROVIDER_LABELS["project_rg"], **match})
    mathlib_root = root / ".lake" / "packages" / "mathlib" if root else None
    if not results and mathlib_root and mathlib_root.is_dir():
        attempted.append(SEARCH_PROVIDER_LABELS["mathlib_rg"])
        for match in _rg_search(mathlib_root, query, limit=limit):
            results.append({"provider": SEARCH_PROVIDER_LABELS["mathlib_rg"], **match})

    degraded = list(report.degraded_reasons)
    if any(provider in attempted for provider in (SEARCH_PROVIDER_LABELS["project_rg"], SEARCH_PROVIDER_LABELS["mathlib_rg"])):
        if not semantic_provider_labels:
            degraded.append("semantic providers unavailable")
        elif not any(provider in attempted for provider in semantic_provider_labels):
            degraded.append("semantic providers skipped; falling back to rg")
    if not results:
        degraded.append("search returned no results")
    result = LeanSearchResult(
        query=query,
        mode=normalized_mode,
        attempted_providers=attempted,
        results=results[:limit],
        degraded_reasons=list(dict.fromkeys(degraded)),
    )
    append_workflow_outcome("lean-search", result.to_dict())
    return result


def lean_axioms(
    target: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    file_path: str = "",
) -> LeanAxiomReport:
    project_root, _ = _project_root(cwd)
    root = Path(project_root) if project_root else None
    target_file = Path(file_path).expanduser().resolve() if file_path else None
    if root is None or target_file is None:
        report = LeanAxiomReport(
            target=target,
            file_path=str(target_file or ""),
            ok=False,
            axioms=[],
            custom_axioms=[],
            classical=False,
            choice=False,
            note="Provide both a Lean project and file_path to inspect axioms.",
        )
        append_workflow_outcome("lean-axioms", report.to_dict())
        return report
    try:
        module_name = _module_name_for_file(root, target_file)
    except Exception:
        module_name = ""
    if not module_name:
        report = LeanAxiomReport(
            target=target,
            file_path=str(target_file),
            ok=False,
            axioms=[],
            custom_axioms=[],
            classical=False,
            choice=False,
            note="Could not resolve module name for the target file.",
        )
        append_workflow_outcome("lean-axioms", report.to_dict())
        return report
    with tempfile.NamedTemporaryFile("w", suffix=".lean", delete=False, dir=str(root)) as handle:
        handle.write(f"import {module_name}\n#print axioms {target}\n")
        temp_path = Path(handle.name)
    try:
        try:
            relative = str(temp_path.relative_to(root))
        except Exception:
            relative = str(temp_path)
        _, output = _run_command(["lake", "env", "lean", relative], cwd=root)
    finally:
        temp_path.unlink(missing_ok=True)
    axioms = sorted(
        {
            token
            for token in re.findall(r"[A-Za-z0-9_.]+", output)
            if "." in token or token in STANDARD_AXIOMS
        }
        - {
            token
            for token in (
                target,
                module_name,
                *(
                    f"{prefix}.{target.split('.')[-1]}"
                    for prefix in {
                        module_name,
                        module_name.rsplit(".", 1)[0] if "." in module_name else "",
                    }
                    if prefix
                ),
            )
            if token
        }
    )
    nonstandard = [axiom for axiom in axioms if axiom not in STANDARD_AXIOMS]
    report = LeanAxiomReport(
        target=target,
        file_path=str(target_file),
        ok=bool(output) and not nonstandard,
        axioms=axioms,
        custom_axioms=nonstandard,
        classical=any("Classical" in axiom for axiom in axioms),
        choice="Classical.choice" in axioms,
        note="no non-standard axioms found" if output and not nonstandard else output[:600],
    )
    append_workflow_outcome("lean-axioms", report.to_dict())
    return report


def route_workflow_step(
    workflow_kind: str,
    live_state: Mapping[str, Any] | None,
    *,
    configured_skill: str = "",
    autonomy_state: Mapping[str, Any] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> WorkflowRouteDecision:
    current = dict(live_state or {})
    autonomy = dict(autonomy_state or {})
    report = probe_capabilities(cwd)
    queue_item = dict(current.get("current_queue_item") or {})
    blocker_text = "\n".join(
        part
        for part in (
            str(current.get("current_blocker", "") or ""),
            str(current.get("diagnostics", "") or ""),
            str(current.get("goals", "") or ""),
            str(current.get("build_status", "") or ""),
        )
        if part
    )
    blocker_kind = classify_blocker_kind(blocker_text)
    target_symbol = str(queue_item.get("label", "") or current.get("target_symbol", "") or "").strip()
    active_file = str(current.get("active_file", "") or "").strip()
    attempts = [
        dict(item)
        for item in autonomy.get("failed_attempts", [])
        if isinstance(item, Mapping)
        and str(item.get("target_symbol", "") or "").strip() == target_symbol
        and str(item.get("active_file", "") or "").strip() in {active_file, str(current.get("active_file_label", "") or "")}
    ]
    attempt_count = len(attempts)
    search_exhausted = attempt_count >= 2 or not report.search_providers
    normalized_workflow = str(workflow_kind or "").strip().lower()
    if normalized_workflow == "autoprove":
        normalized_workflow = "prove"
    elif normalized_workflow == "autoformalize":
        normalized_workflow = "formalize"
    recommended_worker = ""
    route_action = "final-sweep"
    skill_name = configured_skill.strip() or "lean-proof-loop"
    reason = "default autonomous workflow path"

    if normalized_workflow in {"review", "checkpoint"}:
        return WorkflowRouteDecision(
            workflow_kind=normalized_workflow,
            skill_name="lean-diagnostics",
            route_action="diagnostics",
            blocker_kind=blocker_kind,
            recommended_worker="",
            search_exhausted=search_exhausted,
            reason="review/checkpoint use the diagnostics skill",
        )
    if normalized_workflow in {"refactor", "golf"}:
        return WorkflowRouteDecision(
            workflow_kind=normalized_workflow,
            skill_name="lean-refactor-golf",
            route_action="delegate-proof-golfer" if normalized_workflow == "golf" else "refactor",
            blocker_kind=blocker_kind,
            recommended_worker="proof-golfer" if normalized_workflow == "golf" else "",
            search_exhausted=search_exhausted,
            reason="refactor/golf routes through the dedicated refactor skill",
        )

    if queue_item:
        skill_name = "lean-theorem-queue-worker"
        route_action = "queue-worker"
        reason = "file-scoped queue item active"
    if blocker_kind in {"unknown_ident", "synth_instance", "type_mismatch", "timeout"} and attempt_count >= 2:
        recommended_worker = "proof-repair"
        route_action = "delegate-proof-repair"
        reason = f"compiler-style blocker {blocker_kind} repeated {attempt_count} times"
    elif blocker_kind == "axiom-risk":
        recommended_worker = "axiom-eliminator"
        route_action = "delegate-axiom-eliminator"
        reason = "axiom-sensitive blocker detected"
    elif queue_item and (attempt_count >= 3 or (search_exhausted and blocker_kind in {"sorry", "open_goals", "diagnostics"})):
        recommended_worker = "sorry-filler-deep"
        route_action = "delegate-sorry-filler-deep"
        reason = "queue item remains blocked after repeated attempts/search exhaustion"

    decision = WorkflowRouteDecision(
        workflow_kind=normalized_workflow,
        skill_name=skill_name,
        route_action=route_action,
        blocker_kind=blocker_kind,
        recommended_worker=recommended_worker,
        search_exhausted=search_exhausted,
        reason=reason,
    )
    append_workflow_outcome("workflow-route", decision.to_dict())
    return decision


def _worker_prompt(worker: str, request: LeanWorkerRequest) -> str:
    record = get_lean_spec(worker)
    title = record.title if record else worker
    summary = record.summary if record else worker
    parts = [
        f"Native Lean worker: {title}",
        summary,
        "",
        f"Goal: {request.goal}",
    ]
    if request.file_path:
        parts.append(f"File: {request.file_path}")
    if request.line:
        parts.append(f"Line: {request.line}")
    if request.context:
        parts.extend(["", "Context:", request.context])
    if record:
        parts.extend(
            [
                "",
                "Worker contract:",
                f"- route action(s): {', '.join(record.route_actions) or '[none]'}",
                f"- tools: {', '.join(record.tools) or '[none]'}",
            ]
        )
    return "\n".join(parts).strip()


def dispatch_worker(
    request: LeanWorkerRequest,
    *,
    parent_agent: Any = None,
    owner_id: str = "",
) -> LeanWorkerResult:
    worker = request.worker.strip()
    lock_result: dict[str, Any] | None = None
    if request.use_file_lock and request.file_path and owner_id:
        lock_result = _acquire_file_lock(
            request.file_path,
            owner_id=owner_id,
            purpose=f"lean-worker:{worker}",
            ttl_seconds=1800,
            force=False,
        )
        if not lock_result.get("success"):
            result = LeanWorkerResult(
                worker=worker,
                mode="plan",
                dispatched=False,
                summary=f"File lock unavailable for {request.file_path}: {lock_result.get('error', 'unknown error')}",
                lock=lock_result,
            )
            append_workflow_outcome("lean-worker", result.to_dict())
            return result

    prompt = _worker_prompt(worker, request)
    if not request.allow_delegation or parent_agent is None:
        result = LeanWorkerResult(
            worker=worker,
            mode="plan",
            dispatched=False,
            summary=prompt,
            lock=lock_result,
        )
        append_workflow_outcome("lean-worker", result.to_dict())
        return result

    from tools.delegate_tool import delegate_task

    delegated = delegate_task(
        goal=request.goal,
        context=prompt,
        toolsets=["terminal", "file", "skills", "coordination"],
        parent_agent=parent_agent,
        max_iterations=40,
    )
    result = LeanWorkerResult(
        worker=worker,
        mode="delegate",
        dispatched=True,
        summary=f"Delegated {worker}",
        result=delegated,
        lock=lock_result,
    )
    append_workflow_outcome("lean-worker", result.to_dict())
    return result
