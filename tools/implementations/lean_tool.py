#!/usr/bin/env python3
"""Native Lean workflow tools for LeanFlow."""

from __future__ import annotations

import json
import os
from pathlib import Path

from leanflow_cli.lean.lean_declarations import declaration_outline, declaration_region
from leanflow_cli.lean.lean_incremental import lean_incremental_check
from leanflow_cli.lean.lean_lemma_suggest import lean_lemma_suggest
from leanflow_cli.lean.lean_search_horizon import (
    enrich_local_source_results,
    partition_source_order_results,
)
from leanflow_cli.lean.lean_services import (
    LEAN_WORKER_DISPATCH_ENABLED,
    LeanWorkerRequest,
    dispatch_worker,
    lean_auto_search,
    lean_axioms,
    lean_inspect,
    lean_multi_attempt,
    lean_proof_context,
    lean_search,
    lean_sorries,
    lean_verify,
    probe_capabilities,
)
from tools.implementations.lean_experts import (  # noqa: E402
    LEAN_DECOMPOSE_HELPERS_DEFAULT_TIMEOUT_S,
    LEAN_DECOMPOSE_HELPERS_MIN_TIMEOUT_S,
    LEAN_REASONING_HELP_DEFAULT_TIMEOUT_S,
    LEAN_REASONING_HELP_MIN_TIMEOUT_S,
    lean_decompose_helpers_tool,
    lean_reasoning_help_tool,
)
from tools.implementations.lean_have_extraction import lean_extract_have_tool  # noqa: E402
from tools.implementations.lean_patch import apply_verified_patch_tool  # noqa: E402
from tools.registry import registry
from tools.utilities.bounded_call import run_bounded_call
from tools.utilities.lean_inspection_projection import project_exact_symbol_inspection
from tools.utilities.repository_research_policy import clean_room_path_block_reason

LEAN_INSPECT_WALL_TIMEOUT_S = 60.0


def _filter_clean_room_lean_search_results(
    payload: dict[str, object],
    *,
    cwd: str = "",
) -> dict[str, object]:
    """Remove sibling benchmark source matches before returning Lean search output."""
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return payload
    kept: list[object] = []
    omitted = 0
    for raw in raw_results:
        if isinstance(raw, dict):
            candidate = str(raw.get("file", "") or raw.get("path", "") or "")
            if candidate and clean_room_path_block_reason(candidate, cwd=cwd):
                omitted += 1
                continue
        kept.append(raw)
    if not omitted:
        return payload
    filtered = dict(payload)
    filtered["results"] = kept
    filtered["clean_room_omitted_results"] = omitted
    filtered["clean_room_guidance"] = (
        "Sibling benchmark source matches were omitted. Search the active task, shared project "
        "infrastructure, imported libraries, or external non-solution sources instead."
    )
    return filtered


def _lean_inspect_wall_timeout_s() -> float:
    """Return the bounded end-to-end deadline for one inspection call."""
    raw = str(os.environ.get("LEANFLOW_LEAN_INSPECT_WALL_TIMEOUT_S", "") or "").strip()
    if not raw:
        return LEAN_INSPECT_WALL_TIMEOUT_S
    try:
        parsed = float(raw)
    except ValueError:
        return LEAN_INSPECT_WALL_TIMEOUT_S
    return min(300.0, max(0.01, parsed))


def check_lean_requirements() -> bool:
    return True


def lean_capabilities(cwd: str = "") -> str:
    return json.dumps(
        {
            "success": True,
            **probe_capabilities(cwd or None).to_dict(),
        },
        ensure_ascii=False,
    )


def _existing_lean_file(value: str, *, cwd: str = "") -> Path | None:
    """Return the resolved existing Lean file named by a tool argument."""
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() and cwd:
        candidate = Path(cwd).expanduser() / candidate
    candidate = candidate.resolve()
    if candidate.suffix != ".lean" or not candidate.is_file():
        return None
    return candidate


def _lean_inspect_target(target: str, *, file_path: str = "", cwd: str = "") -> str:
    """Select an unambiguous file argument for ``lean_inspect``.

    The historical ``target`` parameter remains authoritative when it names a
    file. A valid explicit ``file_path`` recovers calls that use ``target`` as
    a declaration alias, while two different real files are rejected.
    """
    explicit = str(file_path or "").strip()
    if not explicit:
        return target

    explicit_file = _existing_lean_file(explicit, cwd=cwd)
    target_file = _existing_lean_file(target, cwd=cwd)
    if explicit_file is None:
        if target_file is not None:
            return target
        raise ValueError(
            "lean_inspect file_path must name an existing .lean file when target is not a file: "
            f"{explicit}"
        )
    if target_file is not None and target_file != explicit_file:
        raise ValueError(
            "lean_inspect received conflicting Lean file paths: "
            f"target={target_file}, file_path={explicit_file}"
        )
    return str(explicit_file)


def lean_inspect_tool(
    target: str,
    cwd: str = "",
    line: int | None = None,
    symbol: str = "",
    file_path: str = "",
) -> str:
    """Return full file state or a bounded model-facing exact-symbol projection."""
    inspection_target = _lean_inspect_target(target, file_path=file_path, cwd=cwd)
    timeout_s = _lean_inspect_wall_timeout_s()
    bounded = run_bounded_call(
        lambda: lean_inspect(
            inspection_target,
            cwd=cwd or None,
            line=line,
            symbol=symbol or None,
        ).to_dict(),
        timeout_s=timeout_s,
    )
    if not bounded.completed:
        return json.dumps(
            {
                "success": False,
                "status": "lean_inspect_timeout",
                "timed_out": True,
                "timeout_s": timeout_s,
                "target": inspection_target,
                "symbol": str(symbol or "").strip(),
                "no_progress": True,
                "action_required": (
                    "Do not repeat the unchanged inspection. Use an exact file read, declaration "
                    "outline, cached LeanProbe check, or helper decomposition next."
                ),
            },
            ensure_ascii=False,
        )
    if bounded.error is not None:
        raise bounded.error
    inspection = bounded.value
    if inspection is None:
        raise RuntimeError("lean_inspect completed without returning a report")
    wanted = str(symbol or "").strip()
    if wanted:
        inspection_path = Path(str(inspection.get("target", "") or inspection_target)).expanduser()
        if not inspection_path.is_absolute() and cwd:
            inspection_path = Path(cwd).expanduser() / inspection_path
        region = declaration_region(inspection_path.resolve(), wanted)
        if region is not None:
            inspection = (
                project_exact_symbol_inspection(
                    inspection,
                    symbol=wanted,
                    declaration=region,
                )
                or inspection
            )
    return json.dumps(
        {
            "success": True,
            **inspection,
        },
        ensure_ascii=False,
    )


def lean_verify_tool(target: str = "", cwd: str = "", mode: str = "project") -> str:
    return json.dumps(
        {
            "success": True,
            **lean_verify(target=target, cwd=cwd or None, mode=mode).to_dict(),
        },
        ensure_ascii=False,
    )


def lean_incremental_check_tool(
    file_path: str,
    *,
    action: str = "check_target",
    theorem_id: str = "",
    cwd: str = "",
    replacement: str = "",
    include_tactics: bool = False,
    include_axiom_profile: bool = False,
    timeout_s: int = 60,
) -> str:
    return json.dumps(
        {
            "success": True,
            **lean_incremental_check(
                action=action,
                file_path=file_path,
                theorem_id=theorem_id,
                cwd=cwd,
                replacement=replacement,
                include_tactics=include_tactics,
                include_axiom_profile=include_axiom_profile,
                timeout_s=timeout_s,
            ),
        },
        ensure_ascii=False,
    )


def lean_search_tool(
    query: str,
    cwd: str = "",
    mode: str = "auto",
    limit: int = 10,
    file_path: str = "",
    *,
    _leanflow_source_horizon_file: str = "",
    _leanflow_source_horizon_target: str = "",
) -> str:
    """Search Lean declarations and hide confirmed future same-file results."""
    result = lean_search(query, cwd=cwd or None, mode=mode, limit=limit, file_path=file_path)
    clean_room_payload = _filter_clean_room_lean_search_results(
        {
            "success": True,
            **result.to_dict(),
        },
        cwd=cwd,
    )
    payload = partition_source_order_results(
        enrich_local_source_results(
            clean_room_payload,
            active_file=_leanflow_source_horizon_file,
            cwd=cwd,
        ),
        active_file=_leanflow_source_horizon_file,
        target_symbol=_leanflow_source_horizon_target,
        cwd=cwd,
    )
    if (
        not result.results
        and "repeated empty search loop detected; stop searching and change tactic"
        in result.degraded_reasons
    ):
        payload["success"] = False
        payload["action_required"] = (
            "Stop searching in this turn and either make the strongest concrete proof/edit attempt, "
            "run verification, dispatch a worker, or report a blocker."
        )
    return json.dumps(
        payload,
        ensure_ascii=False,
    )


def lean_sorries_tool(scope: str = "project", target: str = "", cwd: str = "") -> str:
    findings = [
        item.to_dict() for item in lean_sorries(scope=scope, target=target, cwd=cwd or None)
    ]
    return json.dumps(
        {
            "success": True,
            "scope": scope,
            "target": target,
            "count": len(findings),
            "findings": findings,
        },
        ensure_ascii=False,
    )


def lean_axioms_tool(target: str, cwd: str = "", file_path: str = "") -> str:
    report = lean_axioms(target, cwd=cwd or None, file_path=file_path)
    return json.dumps(
        {
            "success": report.inspection_succeeded,
            **report.to_dict(),
        },
        ensure_ascii=False,
    )


def lean_worker_dispatch_tool(
    worker: str,
    goal: str,
    *,
    context: str = "",
    file_path: str = "",
    line: int | None = None,
    allow_delegation: bool = False,
    use_file_lock: bool = True,
    parent_agent=None,
    owner_id: str = "",
) -> str:
    result = dispatch_worker(
        LeanWorkerRequest(
            worker=worker,
            goal=goal,
            context=context,
            file_path=file_path,
            line=line,
            use_file_lock=use_file_lock,
            allow_delegation=allow_delegation,
        ),
        parent_agent=parent_agent,
        owner_id=owner_id,
    )
    return json.dumps({"success": True, **result.to_dict()}, ensure_ascii=False)


def lean_proof_context_tool(
    file_path: str,
    theorem_id: str,
    *,
    cwd: str = "",
    include_similar_proofs: bool = True,
    similarity_threshold: float = 0.7,
) -> str:
    return json.dumps(
        lean_proof_context(
            file_path,
            theorem_id,
            cwd=cwd or None,
            include_similar_proofs=include_similar_proofs,
            similarity_threshold=similarity_threshold,
        ),
        ensure_ascii=False,
    )


def lean_multi_attempt_tool(
    file_path: str,
    line: int,
    attempts: list[str],
    *,
    cwd: str = "",
    column: int | None = None,
) -> str:
    return json.dumps(
        lean_multi_attempt(
            file_path,
            line,
            attempts,
            cwd=cwd or None,
            column=column,
        ),
        ensure_ascii=False,
    )


def lean_auto_search_tool(
    file_path: str,
    theorem_id: str,
    *,
    cwd: str = "",
    timeout_s: int = 10,
    objective: str = "balanced",
) -> str:
    return json.dumps(
        lean_auto_search(
            file_path,
            theorem_id,
            cwd=cwd or None,
            timeout_s=timeout_s,
            objective=objective,
        ),
        ensure_ascii=False,
    )


def lean_lemma_suggest_tool(
    file_path: str,
    theorem_id: str,
    *,
    cwd: str = "",
    max_candidates: int = 12,
) -> str:
    return json.dumps(
        lean_lemma_suggest(
            file_path,
            theorem_id,
            cwd=cwd or None,
            max_candidates=max_candidates,
        ),
        ensure_ascii=False,
    )


def lean_outline_tool(file_path: str, *, symbol: str = "", cwd: str = "") -> str:
    path = Path(str(file_path or "").strip()).expanduser()
    if not path.is_absolute() and cwd:
        path = (Path(cwd).expanduser() / path).resolve()
    wanted = str(symbol or "").strip()
    if wanted:
        region = declaration_region(path, wanted)
        return json.dumps(
            {
                "success": region is not None,
                "file_path": str(path),
                "symbol": wanted,
                "declaration": region,
                **({} if region is not None else {"error": f"declaration not found: {wanted}"}),
            },
            ensure_ascii=False,
        )
    outline = declaration_outline(path)
    return json.dumps(
        {
            "success": True,
            "file_path": str(path),
            "count": len(outline),
            "outline": [
                f"{row['kind']} {row['name']} L{row['line']}-{row['end_line']}" for row in outline
            ],
        },
        ensure_ascii=False,
    )


LEAN_CAPABILITIES_SCHEMA = {
    "name": "lean_capabilities",
    "description": (
        "Inspect the native LeanFlow Lean workflow capability surface: project detection, "
        "Lean/Lake/Elan binaries, MCP/LSP tool availability, search providers, local Loogle/REPL "
        "power-mode status, helper availability, workers, and degraded-mode reasons. Use this early "
        "to learn which proof-search and tactic-screening backends are actually available."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "cwd": {"type": "string", "description": "Optional working directory to probe"},
        },
    },
}

LEAN_INSPECT_SCHEMA = {
    "name": "lean_inspect",
    "description": (
        "Return structured Lean state for a target file: diagnostics, goals, sorry counts, "
        "blocker kind, queue candidates, and capability snapshot. With an exact symbol, the "
        "model-facing response retains every file error but limits other diagnostics and queue "
        "items to that declaration, with explicit aggregate and omitted counts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Lean file path to inspect. When file_path is also supplied, a non-file "
                    "target is treated as a declaration alias; use symbol for exact scope."
                ),
            },
            "file_path": {
                "type": "string",
                "description": (
                    "Optional explicit existing Lean file path. It wins when target is not a "
                    "file; conflicting real file paths are rejected."
                ),
            },
            "cwd": {"type": "string", "description": "Optional working directory"},
            "line": {"type": "integer", "description": "Optional target line for goals lookup"},
            "symbol": {
                "type": "string",
                "description": (
                    "Optional exact declaration name for goals lookup and a bounded response. "
                    "If the declaration cannot be resolved, full-file output is returned."
                ),
            },
        },
        "required": ["target"],
    },
}

LEAN_VERIFY_SCHEMA = {
    "name": "lean_verify",
    "description": "Run a canonical Lean verification step for a file, module, or the whole project. Use `mode=file_exact` for file-scoped theorem acceptance checks.",
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Optional file target for file/module verification",
            },
            "cwd": {"type": "string", "description": "Optional working directory"},
            "mode": {
                "type": "string",
                "description": "Verification mode: `file_exact`, `module`, or `project`",
                "default": "project",
            },
        },
    },
}

LEAN_INCREMENTAL_CHECK_SCHEMA = {
    "name": "lean_incremental_check",
    "description": (
        "Fast LeanInteract-backed verifier for ordered same-file proof queues. It warms the "
        "file header/imports, reuses cached Lean environments, and checks the current file, assigned "
        "declaration, or replacement chunk. Use this for inner-loop proof feedback and optional "
        "tactic/proof-state annotations; use lean_verify for explicit final Lake sweeps. "
        "Normal queue use is action=check_target with file_path and theorem_id. Use "
        "action=check_helper with theorem_id set to the existing assigned declaration and "
        "replacement set to a complete new helper declaration; this validates the helper "
        "against the exact pre-target environment without counting as target acceptance. Use "
        "action=check_file to incrementally elaborate the current file after a patch, including "
        "files that retain an intentional assigned `sorry`. Use action=prepare_file to warm imports "
        "before a run. Use action=feedback or "
        "include_tactics=true when the proof is blocked and you need intermediate tactic "
        "ranges, goals, proof_state, feedback_lean comments, and file-global diagnostic locations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Lean file path"},
            "theorem_id": {"type": "string", "description": "Assigned declaration name"},
            "cwd": {"type": "string", "description": "Optional project working directory"},
            "action": {
                "type": "string",
                "enum": [
                    "prepare_file",
                    "check_file",
                    "check_target",
                    "check_helper",
                    "feedback",
                ],
                "description": "`prepare_file` warms header/imports and prior envs; `check_file` incrementally elaborates the current file; `check_target` validates the assigned declaration; `check_helper` validates a complete new helper supplied in replacement, anchored immediately before the existing theorem_id; `feedback` is a rich diagnostic check with tactic/proof-state output.",
                "default": "check_target",
            },
            "replacement": {
                "type": "string",
                "description": "Optional full replacement declaration chunk. For check_helper, pass only complete, sorry-free helper declarations and use theorem_id as the existing assigned anchor.",
            },
            "include_tactics": {
                "type": "boolean",
                "description": "Include tactic ranges, tactic text, goals, proof_state, and feedback_lean annotations. Leave false for speed on likely-success checks; set true when asking the model to repair a stuck proof. Failures auto-rerun with tactics when possible.",
                "default": False,
            },
            "include_axiom_profile": {
                "type": "boolean",
                "description": (
                    "For `check_target`, embed marker-bound `#print axioms` evidence in the "
                    "exact target check. For `check_helper`, select the one-shot exact-project "
                    "helper harness, require a complete allowed-axiom profile, and fail closed "
                    "when that profile is unavailable. Managed assigned-target replacements "
                    "enable target profiling automatically. Do not use this option with "
                    "`prepare_file`, `check_file`, or `feedback`."
                ),
                "default": False,
            },
            "timeout_s": {
                "type": "integer",
                "description": (
                    "LeanInteract request timeout in seconds. Process-isolated research "
                    "workers enforce a 300-second cold-start floor so a large-file check "
                    "does not repeatedly kill and restart its Lean server."
                ),
                "minimum": 1,
                "default": 60,
            },
        },
        "required": ["file_path"],
    },
}

LEAN_SEARCH_SCHEMA = {
    "name": "lean_search",
    "description": (
        "Search for Lean declarations, theorem names, type-pattern matches, and proof hints. "
        "Uses powered MCP/LSP providers first: local project search, local/public Loogle, LeanExplore "
        "when configured, and other semantic providers, then falls back to native rg/mathlib search. "
        "Use `mode=type-pattern` for theorem-shape queries, `semantic` or `natural-language` for concept "
        "queries, and `local` for project examples. Returns provider provenance. If degraded reasons report "
        "a repeated empty search loop, stop searching and change tactic."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query or type pattern"},
            "cwd": {"type": "string", "description": "Optional working directory"},
            "mode": {
                "type": "string",
                "description": "Search mode: `auto`, `local`, `semantic`, `type-pattern`, or `natural-language`",
                "default": "auto",
            },
            "limit": {"type": "integer", "description": "Maximum number of results", "default": 10},
            "file_path": {
                "type": "string",
                "description": "Optional active file path for provider-specific search",
            },
        },
        "required": ["query"],
    },
}

LEAN_SORRIES_SCHEMA = {
    "name": "lean_sorries",
    "description": "List `sorry` findings across the project or a single file, including declaration names and line numbers.",
    "parameters": {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "description": "`project` or `file`", "default": "project"},
            "target": {"type": "string", "description": "File path when scope=`file`"},
            "cwd": {"type": "string", "description": "Optional working directory"},
        },
    },
}

LEAN_AXIOMS_SCHEMA = {
    "name": "lean_axioms",
    "description": "Run a best-effort axiom report for one declaration using a temporary Lean file that imports the target module and issues `#print axioms`.",
    "parameters": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Declaration name to inspect"},
            "file_path": {"type": "string", "description": "Lean file containing the declaration"},
            "cwd": {"type": "string", "description": "Optional working directory"},
        },
        "required": ["target"],
    },
}

LEAN_PROOF_CONTEXT_SCHEMA = {
    "name": "lean_proof_context",
    "description": (
        "Fetch theorem-local context before deeper automation or major proof edits: theorem statement, "
        "original proof, hypotheses, in-scope names, namespace, nearby declarations, and optional similar "
        "proofs. Uses the managed Lean automation backend when available and local file fallback when needed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Lean file containing the theorem"},
            "theorem_id": {"type": "string", "description": "Declaration name to inspect"},
            "cwd": {"type": "string", "description": "Optional working directory"},
            "include_similar_proofs": {"type": "boolean", "default": True},
            "similarity_threshold": {"type": "number", "default": 0.7},
        },
        "required": ["file_path", "theorem_id"],
    },
}

LEAN_MULTI_ATTEMPT_SCHEMA = {
    "name": "lean_multi_attempt",
    "description": (
        "Screen 2-6 short concrete tactic attempts at one proof location before editing. When REPL power mode "
        "is available this can quickly test local tactics such as `simp`, `omega`, `linarith`, `aesop`, `ring`, "
        "or small `exact`/`apply` candidates. Do not pass full proof blocks, declaration headers, or candidates "
        "containing `sorry`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Lean file path"},
            "line": {
                "type": "integer",
                "description": (
                    "Target tactic line number; safe line-only requests resolve an immediate "
                    "post-proof blank or an inline `:= by` tactic body"
                ),
            },
            "column": {"type": "integer", "description": "Optional target column"},
            "attempts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete tactic candidates to test",
            },
            "cwd": {"type": "string", "description": "Optional working directory"},
        },
        "required": ["file_path", "line", "attempts"],
    },
}

LEAN_AUTO_SEARCH_SCHEMA = {
    "name": "lean_auto_search",
    "description": (
        "Ask the managed Lean automation backend to search for one theorem-local proof candidate after context "
        "or probe data exists. Use when the goal looks automation-suited or repeated manual attempts are stuck; "
        "treat backend/setup errors as degraded automation, not as evidence that the theorem is false."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Lean file path"},
            "theorem_id": {"type": "string", "description": "Declaration name to search"},
            "timeout_s": {"type": "integer", "default": 10},
            "objective": {"type": "string", "default": "balanced"},
            "cwd": {"type": "string", "description": "Optional working directory"},
        },
        "required": ["file_path", "theorem_id"],
    },
}

LEAN_LEMMA_SUGGEST_SCHEMA = {
    "name": "lean_lemma_suggest",
    "description": (
        "Given the assigned declaration, read its goal/hypotheses, derive a few targeted queries "
        "(conclusion head symbol, key operators, hypothesis types), search semantic + type-pattern "
        "modes, and return a ranked candidate-lemma list (name, signature, provider, why_relevant). "
        "Use before hand-searching to find existing lemmas that likely close or advance the goal; "
        "check `degraded_reasons` when candidates is empty."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Lean file containing the declaration"},
            "theorem_id": {"type": "string", "description": "Assigned declaration name"},
            "cwd": {"type": "string", "description": "Optional working directory"},
            "max_candidates": {
                "type": "integer",
                "description": "Maximum ranked candidates to return",
                "default": 12,
            },
        },
        "required": ["file_path", "theorem_id"],
    },
}

LEAN_OUTLINE_SCHEMA = {
    "name": "lean_outline",
    "description": (
        "Return a token-cheap outline of a Lean file: one line per top-level declaration as "
        "`kind name Lstart-Lend`. Pass `symbol` to instead return just that declaration's "
        "kind/name/line range and full source text. Use to map a file or fetch one declaration "
        "without reading the whole file."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Lean file to outline"},
            "symbol": {
                "type": "string",
                "description": "Optional declaration name; returns just that declaration's source region",
            },
            "cwd": {
                "type": "string",
                "description": "Optional working directory for relative paths",
            },
        },
        "required": ["file_path"],
    },
}

APPLY_VERIFIED_PATCH_SCHEMA = {
    "name": "apply_verified_patch",
    "description": (
        "Apply one V4A patch to a single .lean file, persist a pre-edit checkpoint, "
        "then immediately run LeanFlow's cached incremental verification by default. In managed "
        "queue workflows, ordinary `patch` "
        "and `write_file` edits are manager-verified after successful tool calls; use this tool "
        "when you specifically need one atomic patch/checkpoint/verification result."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The single .lean file to add or update"},
            "patch": {
                "type": "string",
                "description": "A V4A patch with exactly one Add File or Update File operation for path",
            },
            "cwd": {"type": "string", "description": "Optional project working directory"},
            "check_mode": {
                "type": "string",
                "description": "Verification tier: incremental/fast (default warm LeanFlow check), file_exact/lean-file (explicit Lake file check), module/medium, or project/strict",
                "default": "incremental",
            },
            "theorem_id": {
                "type": "string",
                "description": "Optional active theorem/declaration id for workflow state",
            },
            "timeout_s": {
                "type": "integer",
                "minimum": 1,
                "default": 300,
                "description": "LeanProbe verification timeout in seconds; raise it for large declarations or cold project state",
            },
        },
        "required": ["path", "patch"],
    },
}

LEAN_EXTRACT_HAVE_SCHEMA = {
    "name": "lean_extract_have",
    "description": (
        "Inventory or transactionally extract up to four top-level local `have` proofs into private helpers. "
        "LeanFlow uses Mathlib `extract_goal` to recover the exact local context, verifies the "
        "helpers and replacements independently with LeanProbe, then banks one combined source rewrite. "
        "Use after repeated target timeouts instead of growing or manually refactoring a monolithic proof."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "theorem_id": {"type": "string", "description": "Assigned declaration name"},
            "file_path": {"type": "string", "description": "Lean file containing the declaration"},
            "action": {
                "type": "string",
                "enum": ["inventory", "extract"],
                "default": "extract",
                "description": "Inventory candidates without editing, or extract a bounded verified batch",
            },
            "have_name": {
                "type": "string",
                "description": "Optional single local have name (backward-compatible shorthand)",
            },
            "have_names": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 4,
                "description": "Optional ordered set of active local have names to extract transactionally",
            },
            "helper_names": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Optional mapping from local have names to semantic private helper names",
            },
            "minimum_lines": {
                "type": "integer",
                "description": "Minimum local proof size for automatic selection",
                "default": 8,
            },
            "max_helpers": {
                "type": "integer",
                "minimum": 1,
                "maximum": 4,
                "default": 1,
                "description": "Automatic extraction batch size when explicit names are omitted",
            },
            "cwd": {"type": "string", "description": "Optional project working directory"},
            "timeout_s": {
                "type": "integer",
                "description": "Hard wall-clock ceiling for each LeanProbe extraction stage",
                "default": 300,
            },
        },
        "required": ["theorem_id", "file_path"],
    },
}

LEAN_WORKER_DISPATCH_SCHEMA = {
    "name": "lean_worker_dispatch",
    "description": "Dispatch or describe a native Lean specialist worker such as `proof-repair`, `proof-golfer`, `axiom-eliminator`, or `sorry-filler-deep`. When delegation is unavailable, returns a structured worker plan instead of failing.",
    "parameters": {
        "type": "object",
        "properties": {
            "worker": {"type": "string", "description": "Worker preset to use"},
            "goal": {"type": "string", "description": "Concrete worker objective"},
            "context": {"type": "string", "description": "Additional worker context"},
            "file_path": {"type": "string", "description": "Lean file the worker should focus on"},
            "line": {"type": "integer", "description": "Optional target line"},
            "allow_delegation": {
                "type": "boolean",
                "description": "Allow subagent delegation when parent-agent context exists",
                "default": False,
            },
            "use_file_lock": {
                "type": "boolean",
                "description": "Reserve the file before dispatching when owner context exists",
                "default": True,
            },
        },
        "required": ["worker", "goal"],
    },
}

LEAN_REASONING_HELP_SCHEMA = {
    "name": "lean_reasoning_help",
    "description": (
        "Ask the configured auxiliary theorem advisor for proof-strategy advice on a hard Lean theorem. "
        "Use after repeated focused attempts or search/automation exhaustion. The advisor only gives advice; "
        "you must still preserve the theorem statement and verify same-file queue edits with "
        "`lean_incremental_check(check_target)`; keep `lean_verify` for final Lake sweeps or explicit canonical checks."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "theorem_id": {
                "type": "string",
                "description": "Assigned theorem/lemma/example identifier",
            },
            "file_path": {"type": "string", "description": "Lean file containing the theorem"},
            "theorem_statement": {
                "type": "string",
                "description": "Exact current declaration statement, if available",
            },
            "current_diagnostics": {
                "type": "string",
                "description": "Current Lean diagnostics or blocker text",
            },
            "current_goals": {"type": "string", "description": "Current Lean goals, if available"},
            "current_attempt": {
                "type": "string",
                "description": "Most recent proof attempt or edit idea",
            },
            "recent_failed_attempts": {
                "type": "string",
                "description": "Summary of prior failed attempts and errors",
            },
            "question": {
                "type": "string",
                "description": "Specific advice request for the auxiliary model",
            },
            "cwd": {"type": "string", "description": "Optional project working directory"},
            "timeout_s": {
                "type": "integer",
                "description": "Advisor request timeout in seconds",
                "default": LEAN_REASONING_HELP_DEFAULT_TIMEOUT_S,
                "minimum": LEAN_REASONING_HELP_MIN_TIMEOUT_S,
            },
        },
        "required": ["theorem_id", "file_path"],
    },
}

LEAN_DECOMPOSE_HELPERS_SCHEMA = {
    "name": "lean_decompose_helpers",
    "description": (
        "Ask the configured auxiliary theorem advisor for a structured helper-lemma decomposition of a hard "
        "Lean theorem. Use when the next useful step is splitting the proof into sublemmas, especially after "
        "broad search has stopped producing progress. This tool does not edit files; it returns ordered helper "
        "skeletons with `by sorry` as temporary planning artifacts and checks them with `lean_incremental_check` "
        "so ready helpers at least parse/elaborate before the main agent patches anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "theorem_id": {
                "type": "string",
                "description": "Assigned theorem/lemma/example identifier",
            },
            "file_path": {"type": "string", "description": "Lean file containing the theorem"},
            "theorem_statement": {
                "type": "string",
                "description": "Exact current target declaration statement; used to validate helper skeletons with the target closed by `by sorry`",
            },
            "current_diagnostics": {
                "type": "string",
                "description": "Current Lean diagnostics or blocker text",
            },
            "current_goals": {"type": "string", "description": "Current Lean goals, if available"},
            "current_attempt": {
                "type": "string",
                "description": "Most recent proof attempt or edit idea",
            },
            "recent_failed_attempts": {
                "type": "string",
                "description": "Summary of prior failed attempts and errors",
            },
            "question": {
                "type": "string",
                "description": "Specific decomposition request for the auxiliary model",
            },
            "cwd": {"type": "string", "description": "Optional project working directory"},
            "max_helper_count": {
                "type": "integer",
                "description": "Maximum number of helper lemmas to propose",
                "default": 6,
            },
            "timeout_s": {
                "type": "integer",
                "description": (
                    "Whole decomposition request timeout in seconds, shared by the "
                    "advisor and every subsequent Lean skeleton validation"
                ),
                "default": LEAN_DECOMPOSE_HELPERS_DEFAULT_TIMEOUT_S,
                "minimum": LEAN_DECOMPOSE_HELPERS_MIN_TIMEOUT_S,
            },
        },
        "required": ["theorem_id", "file_path"],
    },
}


registry.register(
    name="lean_capabilities",
    toolset="lean",
    schema=LEAN_CAPABILITIES_SCHEMA,
    handler=lambda args, **kw: lean_capabilities(cwd=args.get("cwd", "")),
    check_fn=check_lean_requirements,
    emoji="🧭",
)
registry.register(
    name="lean_inspect",
    toolset="lean",
    schema=LEAN_INSPECT_SCHEMA,
    handler=lambda args, **kw: lean_inspect_tool(
        target=args.get("target", ""),
        cwd=args.get("cwd", ""),
        line=args.get("line"),
        symbol=args.get("symbol", ""),
        file_path=args.get("file_path", ""),
    ),
    check_fn=check_lean_requirements,
    emoji="🔬",
)
registry.register(
    name="lean_verify",
    toolset="lean",
    schema=LEAN_VERIFY_SCHEMA,
    handler=lambda args, **kw: lean_verify_tool(
        target=args.get("target", ""),
        cwd=args.get("cwd", ""),
        mode=args.get("mode", "project"),
    ),
    check_fn=check_lean_requirements,
    emoji="✅",
)
registry.register(
    name="lean_incremental_check",
    toolset="lean",
    schema=LEAN_INCREMENTAL_CHECK_SCHEMA,
    handler=lambda args, **kw: lean_incremental_check_tool(
        file_path=args.get("file_path", ""),
        action=args.get("action", "check_target"),
        theorem_id=args.get("theorem_id", ""),
        cwd=args.get("cwd", ""),
        replacement=args.get("replacement", ""),
        include_tactics=bool(args.get("include_tactics", False)),
        include_axiom_profile=bool(args.get("include_axiom_profile", False)),
        timeout_s=int(args.get("timeout_s", 60) or 60),
    ),
    check_fn=check_lean_requirements,
    emoji="⚡",
)
registry.register(
    name="lean_search",
    toolset="lean",
    schema=LEAN_SEARCH_SCHEMA,
    handler=lambda args, **kw: lean_search_tool(
        query=args.get("query", ""),
        cwd=args.get("cwd", ""),
        mode=args.get("mode", "auto"),
        limit=args.get("limit", 10),
        file_path=args.get("file_path", ""),
        _leanflow_source_horizon_file=args.get("_leanflow_source_horizon_file", ""),
        _leanflow_source_horizon_target=args.get("_leanflow_source_horizon_target", ""),
    ),
    check_fn=check_lean_requirements,
    emoji="🔎",
)
registry.register(
    name="lean_sorries",
    toolset="lean",
    schema=LEAN_SORRIES_SCHEMA,
    handler=lambda args, **kw: lean_sorries_tool(
        scope=args.get("scope", "project"),
        target=args.get("target", ""),
        cwd=args.get("cwd", ""),
    ),
    check_fn=check_lean_requirements,
    emoji="📍",
)
registry.register(
    name="lean_axioms",
    toolset="lean",
    schema=LEAN_AXIOMS_SCHEMA,
    handler=lambda args, **kw: lean_axioms_tool(
        target=args.get("target", ""),
        cwd=args.get("cwd", ""),
        file_path=args.get("file_path", ""),
    ),
    check_fn=check_lean_requirements,
    emoji="📐",
)
registry.register(
    name="lean_proof_context",
    toolset="lean",
    schema=LEAN_PROOF_CONTEXT_SCHEMA,
    handler=lambda args, **kw: lean_proof_context_tool(
        file_path=args.get("file_path", ""),
        theorem_id=args.get("theorem_id", ""),
        cwd=args.get("cwd", ""),
        include_similar_proofs=bool(args.get("include_similar_proofs", True)),
        similarity_threshold=float(args.get("similarity_threshold", 0.7)),
    ),
    check_fn=check_lean_requirements,
    emoji="🧾",
)
registry.register(
    name="lean_multi_attempt",
    toolset="lean",
    schema=LEAN_MULTI_ATTEMPT_SCHEMA,
    handler=lambda args, **kw: lean_multi_attempt_tool(
        file_path=args.get("file_path", ""),
        line=int(args.get("line", 1) or 1),
        attempts=list(args.get("attempts", []) or []),
        cwd=args.get("cwd", ""),
        column=args.get("column"),
    ),
    check_fn=check_lean_requirements,
    emoji="🎯",
)
registry.register(
    name="lean_auto_search",
    toolset="lean",
    schema=LEAN_AUTO_SEARCH_SCHEMA,
    handler=lambda args, **kw: lean_auto_search_tool(
        file_path=args.get("file_path", ""),
        theorem_id=args.get("theorem_id", ""),
        cwd=args.get("cwd", ""),
        timeout_s=int(args.get("timeout_s", 10) or 10),
        objective=args.get("objective", "balanced"),
    ),
    check_fn=check_lean_requirements,
    emoji="🛰️",
)
registry.register(
    name="lean_lemma_suggest",
    toolset="lean",
    schema=LEAN_LEMMA_SUGGEST_SCHEMA,
    handler=lambda args, **kw: lean_lemma_suggest_tool(
        file_path=args.get("file_path", ""),
        theorem_id=args.get("theorem_id", ""),
        cwd=args.get("cwd", ""),
        max_candidates=int(args.get("max_candidates", 12) or 12),
    ),
    check_fn=check_lean_requirements,
    emoji="🧩",
)
registry.register(
    name="lean_outline",
    toolset="lean",
    schema=LEAN_OUTLINE_SCHEMA,
    handler=lambda args, **kw: lean_outline_tool(
        file_path=args.get("file_path", ""),
        symbol=args.get("symbol", ""),
        cwd=args.get("cwd", ""),
    ),
    check_fn=check_lean_requirements,
    emoji="🗂️",
)
registry.register(
    name="apply_verified_patch",
    toolset="lean",
    schema=APPLY_VERIFIED_PATCH_SCHEMA,
    handler=lambda args, **kw: apply_verified_patch_tool(
        path=args.get("path", ""),
        patch=args.get("patch", ""),
        cwd=args.get("cwd", ""),
        check_mode=args.get("check_mode", "incremental"),
        theorem_id=args.get("theorem_id", ""),
        owner_id=str(kw.get("owner_id", "") or ""),
        task_id=str(kw.get("task_id", "") or "default"),
        timeout_s=int(args.get("timeout_s", 300) or 300),
        verified_edit_authority_token=str(args.get("_leanflow_verified_edit_authority", "") or ""),
    ),
    check_fn=check_lean_requirements,
    emoji="✅",
)
registry.register(
    name="lean_extract_have",
    toolset="lean",
    schema=LEAN_EXTRACT_HAVE_SCHEMA,
    handler=lambda args, **kw: lean_extract_have_tool(
        theorem_id=args.get("theorem_id", ""),
        file_path=args.get("file_path", ""),
        action=args.get("action", "extract"),
        have_name=args.get("have_name", ""),
        have_names=args.get("have_names", []),
        helper_names=args.get("helper_names", {}),
        minimum_lines=int(args.get("minimum_lines", 8) or 8),
        max_helpers=int(args.get("max_helpers", 1) or 1),
        cwd=args.get("cwd", ""),
        timeout_s=int(args.get("timeout_s", 300) or 300),
        owner_id=str(kw.get("owner_id", "") or ""),
    ),
    check_fn=check_lean_requirements,
    emoji="✂️",
)
if LEAN_WORKER_DISPATCH_ENABLED:
    registry.register(
        name="lean_worker_dispatch",
        toolset="lean",
        schema=LEAN_WORKER_DISPATCH_SCHEMA,
        handler=lambda args, **kw: lean_worker_dispatch_tool(
            worker=args.get("worker", ""),
            goal=args.get("goal", ""),
            context=args.get("context", ""),
            file_path=args.get("file_path", ""),
            line=args.get("line"),
            allow_delegation=bool(args.get("allow_delegation", False)),
            use_file_lock=bool(args.get("use_file_lock", True)),
            parent_agent=kw.get("parent_agent"),
            owner_id=str(kw.get("owner_id", "") or ""),
        ),
        check_fn=check_lean_requirements,
        emoji="🧠",
    )
registry.register(
    name="lean_reasoning_help",
    toolset="lean",
    schema=LEAN_REASONING_HELP_SCHEMA,
    handler=lambda args, **kw: lean_reasoning_help_tool(
        theorem_id=args.get("theorem_id", ""),
        file_path=args.get("file_path", ""),
        theorem_statement=args.get("theorem_statement", ""),
        current_diagnostics=args.get("current_diagnostics", ""),
        current_goals=args.get("current_goals", ""),
        current_attempt=args.get("current_attempt", ""),
        recent_failed_attempts=args.get("recent_failed_attempts", ""),
        question=args.get("question", ""),
        cwd=args.get("cwd", ""),
        timeout_s=int(args.get("timeout_s", LEAN_REASONING_HELP_DEFAULT_TIMEOUT_S)),
    ),
    check_fn=check_lean_requirements,
    emoji="💡",
)
registry.register(
    name="lean_decompose_helpers",
    toolset="lean",
    schema=LEAN_DECOMPOSE_HELPERS_SCHEMA,
    handler=lambda args, **kw: lean_decompose_helpers_tool(
        theorem_id=args.get("theorem_id", ""),
        file_path=args.get("file_path", ""),
        theorem_statement=args.get("theorem_statement", ""),
        current_diagnostics=args.get("current_diagnostics", ""),
        current_goals=args.get("current_goals", ""),
        current_attempt=args.get("current_attempt", ""),
        recent_failed_attempts=args.get("recent_failed_attempts", ""),
        question=args.get("question", ""),
        cwd=args.get("cwd", ""),
        max_helper_count=int(args.get("max_helper_count", 6) or 6),
        timeout_s=int(args.get("timeout_s", LEAN_DECOMPOSE_HELPERS_DEFAULT_TIMEOUT_S)),
    ),
    check_fn=check_lean_requirements,
    emoji="🪜",
)
