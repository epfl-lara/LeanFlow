#!/usr/bin/env python3
"""Native Lean workflow tools for EPFLemma."""

from __future__ import annotations

import json

from epflemma_cli.lean.lean_incremental import lean_incremental_check
from epflemma_cli.lean.lean_services import (
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
from tools.implementations.lean_patch import apply_verified_patch_tool  # noqa: E402
from tools.registry import registry


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


def lean_inspect_tool(target: str, cwd: str = "", line: int | None = None, symbol: str = "") -> str:
    return json.dumps(
        {
            "success": True,
            **lean_inspect(target, cwd=cwd or None, line=line, symbol=symbol or None).to_dict(),
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
                timeout_s=timeout_s,
            ),
        },
        ensure_ascii=False,
    )


def lean_search_tool(query: str, cwd: str = "", mode: str = "auto", limit: int = 10, file_path: str = "") -> str:
    result = lean_search(query, cwd=cwd or None, mode=mode, limit=limit, file_path=file_path)
    payload = {
        "success": True,
        **result.to_dict(),
    }
    if (
        not result.results
        and "repeated empty search loop detected; stop searching and change tactic" in result.degraded_reasons
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
    findings = [item.to_dict() for item in lean_sorries(scope=scope, target=target, cwd=cwd or None)]
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
    return json.dumps(
        {
            "success": True,
            **lean_axioms(target, cwd=cwd or None, file_path=file_path).to_dict(),
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


LEAN_CAPABILITIES_SCHEMA = {
    "name": "lean_capabilities",
    "description": (
        "Inspect the native EPFLemma Lean workflow capability surface: project detection, "
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
    "description": "Return structured Lean state for a target file: diagnostics, goals, sorry counts, blocker kind, queue candidates, and capability snapshot.",
    "parameters": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Lean file path to inspect"},
            "cwd": {"type": "string", "description": "Optional working directory"},
            "line": {"type": "integer", "description": "Optional target line for goals lookup"},
            "symbol": {"type": "string", "description": "Optional declaration name for goals lookup"},
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
            "target": {"type": "string", "description": "Optional file target for file/module verification"},
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
        "file header/imports, reuses cached Lean environments, and checks only the assigned "
        "declaration or replacement chunk. Use this for inner-loop proof feedback and optional "
        "tactic/proof-state annotations; use lean_verify for explicit final Lake sweeps. "
        "Normal queue use is action=check_target with file_path and theorem_id. Use "
        "action=prepare_file to warm imports before a run. Use action=feedback or "
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
                "description": "`prepare_file` warms header/imports and prior envs; `check_target` validates the assigned declaration; `feedback` is a rich diagnostic check with tactic/proof-state output.",
                "default": "check_target",
            },
            "replacement": {
                "type": "string",
                "description": "Optional full replacement declaration chunk to check instead of current file text",
            },
            "include_tactics": {
                "type": "boolean",
                "description": "Include tactic ranges, tactic text, goals, proof_state, and feedback_lean annotations. Leave false for speed on likely-success checks; set true when asking the model to repair a stuck proof. Failures auto-rerun with tactics when possible.",
                "default": False,
            },
            "timeout_s": {"type": "integer", "description": "LeanInteract request timeout", "default": 60},
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
            "file_path": {"type": "string", "description": "Optional active file path for provider-specific search"},
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
            "line": {"type": "integer", "description": "Target line number"},
            "column": {"type": "integer", "description": "Optional target column"},
            "attempts": {"type": "array", "items": {"type": "string"}, "description": "Concrete tactic candidates to test"},
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

APPLY_VERIFIED_PATCH_SCHEMA = {
    "name": "apply_verified_patch",
    "description": (
        "Apply one V4A patch to a single .lean file, persist a pre-edit checkpoint, "
        "then immediately run Lean verification. In managed queue workflows, ordinary `patch` "
        "and `write_file` edits are manager-verified after successful tool calls; use this tool "
        "when you specifically need one atomic patch/checkpoint/verification result."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The single .lean file to add or update"},
            "patch": {"type": "string", "description": "A V4A patch with exactly one Add File or Update File operation for path"},
            "cwd": {"type": "string", "description": "Optional project working directory"},
            "check_mode": {
                "type": "string",
                "description": "Verification tier: file_exact/lean-file/fast, module/medium, or project/strict",
                "default": "file_exact",
            },
            "theorem_id": {"type": "string", "description": "Optional active theorem/declaration id for workflow state"},
        },
        "required": ["path", "patch"],
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
            "theorem_id": {"type": "string", "description": "Assigned theorem/lemma/example identifier"},
            "file_path": {"type": "string", "description": "Lean file containing the theorem"},
            "theorem_statement": {"type": "string", "description": "Exact current declaration statement, if available"},
            "current_diagnostics": {"type": "string", "description": "Current Lean diagnostics or blocker text"},
            "current_goals": {"type": "string", "description": "Current Lean goals, if available"},
            "current_attempt": {"type": "string", "description": "Most recent proof attempt or edit idea"},
            "recent_failed_attempts": {"type": "string", "description": "Summary of prior failed attempts and errors"},
            "question": {"type": "string", "description": "Specific advice request for the auxiliary model"},
            "cwd": {"type": "string", "description": "Optional project working directory"},
            "timeout_s": {
                "type": "integer",
                "description": "Advisor request timeout in seconds",
                "default": LEAN_REASONING_HELP_DEFAULT_TIMEOUT_S,
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
            "theorem_id": {"type": "string", "description": "Assigned theorem/lemma/example identifier"},
            "file_path": {"type": "string", "description": "Lean file containing the theorem"},
            "theorem_statement": {
                "type": "string",
                "description": "Exact current target declaration statement; used to validate helper skeletons with the target closed by `by sorry`",
            },
            "current_diagnostics": {"type": "string", "description": "Current Lean diagnostics or blocker text"},
            "current_goals": {"type": "string", "description": "Current Lean goals, if available"},
            "current_attempt": {"type": "string", "description": "Most recent proof attempt or edit idea"},
            "recent_failed_attempts": {"type": "string", "description": "Summary of prior failed attempts and errors"},
            "question": {"type": "string", "description": "Specific decomposition request for the auxiliary model"},
            "cwd": {"type": "string", "description": "Optional project working directory"},
            "max_helper_count": {
                "type": "integer",
                "description": "Maximum number of helper lemmas to propose",
                "default": 6,
            },
            "timeout_s": {
                "type": "integer",
                "description": "Advisor request timeout in seconds",
                "default": LEAN_DECOMPOSE_HELPERS_DEFAULT_TIMEOUT_S,
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
    name="apply_verified_patch",
    toolset="lean",
    schema=APPLY_VERIFIED_PATCH_SCHEMA,
    handler=lambda args, **kw: apply_verified_patch_tool(
        path=args.get("path", ""),
        patch=args.get("patch", ""),
        cwd=args.get("cwd", ""),
        check_mode=args.get("check_mode", "file_exact"),
        theorem_id=args.get("theorem_id", ""),
        owner_id=str(kw.get("owner_id", "") or ""),
        task_id=str(kw.get("task_id", "") or "default"),
    ),
    check_fn=check_lean_requirements,
    emoji="✅",
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
        timeout_s=int(
            args.get("timeout_s", LEAN_REASONING_HELP_DEFAULT_TIMEOUT_S)
            or LEAN_REASONING_HELP_DEFAULT_TIMEOUT_S
        ),
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
        timeout_s=int(
            args.get("timeout_s", LEAN_DECOMPOSE_HELPERS_DEFAULT_TIMEOUT_S)
            or LEAN_DECOMPOSE_HELPERS_DEFAULT_TIMEOUT_S
        ),
    ),
    check_fn=check_lean_requirements,
    emoji="🪜",
)
