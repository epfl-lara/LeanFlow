#!/usr/bin/env python3
"""Native Lean workflow tools for EPFLemma."""

from __future__ import annotations

import json

from epflemma_cli.lean_services import (
    LeanWorkerRequest,
    dispatch_worker,
    lean_auto_probe,
    lean_auto_search,
    lean_auto_try,
    lean_axioms,
    lean_inspect,
    lean_multi_attempt,
    lean_proof_context,
    lean_search,
    lean_sorries,
    lean_verify,
    probe_capabilities,
)
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


def lean_auto_probe_tool(
    file_path: str,
    theorem_id: str,
    *,
    cwd: str = "",
    methods: list[str] | None = None,
    timeout_s: int = 10,
) -> str:
    return json.dumps(
        lean_auto_probe(
            file_path,
            theorem_id,
            cwd=cwd or None,
            methods=methods,
            timeout_s=timeout_s,
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


def lean_auto_try_tool(
    file_path: str,
    theorem_id: str,
    proof_attempt: str,
    *,
    cwd: str = "",
    timeout_s: int = 10,
) -> str:
    return json.dumps(
        lean_auto_try(
            file_path,
            theorem_id,
            proof_attempt,
            cwd=cwd or None,
            timeout_s=timeout_s,
        ),
        ensure_ascii=False,
    )


LEAN_CAPABILITIES_SCHEMA = {
    "name": "lean_capabilities",
    "description": "Inspect the native EPFLemma Lean workflow capability surface: project detection, Lean/Lake/Elan binaries, MCP/LSP tool availability, search providers, helper availability, workers, and degraded-mode reasons.",
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

LEAN_SEARCH_SCHEMA = {
    "name": "lean_search",
    "description": "Search for Lean declarations and proof hints using MCP/LSP providers first, then native rg/mathlib fallbacks. Returns provider provenance with each result. If degraded reasons report a repeated empty search loop, stop searching and change tactic.",
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
    "description": "Fetch theorem-local context from the managed Lean automation backend: theorem statement, original proof, hypotheses, in-scope names, namespace, and optional similar proofs.",
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
    "description": "Screen 2-6 short concrete tactic attempts at one proof location using the Lean MCP backend. Do not pass full proof blocks or candidates containing `sorry`.",
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

LEAN_AUTO_PROBE_SCHEMA = {
    "name": "lean_auto_probe",
    "description": "Probe theorem-local automation methods such as `aesop`, `aesop?`, and `grind` before broader search.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Lean file path"},
            "theorem_id": {"type": "string", "description": "Declaration name to probe"},
            "methods": {"type": "array", "items": {"type": "string"}, "description": "Automation methods to probe"},
            "timeout_s": {"type": "integer", "default": 10},
            "cwd": {"type": "string", "description": "Optional working directory"},
        },
        "required": ["file_path", "theorem_id"],
    },
}

LEAN_AUTO_SEARCH_SCHEMA = {
    "name": "lean_auto_search",
    "description": "Ask the managed Lean automation backend to search for one theorem-local automated proof candidate after context/probe data exists.",
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

LEAN_AUTO_TRY_SCHEMA = {
    "name": "lean_auto_try",
    "description": "Validate one concrete theorem-local automated proof attempt before patching it into the file.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Lean file path"},
            "theorem_id": {"type": "string", "description": "Declaration name to test"},
            "proof_attempt": {"type": "string", "description": "Concrete proof candidate to validate"},
            "timeout_s": {"type": "integer", "default": 10},
            "cwd": {"type": "string", "description": "Optional working directory"},
        },
        "required": ["file_path", "theorem_id", "proof_attempt"],
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
    name="lean_auto_probe",
    toolset="lean",
    schema=LEAN_AUTO_PROBE_SCHEMA,
    handler=lambda args, **kw: lean_auto_probe_tool(
        file_path=args.get("file_path", ""),
        theorem_id=args.get("theorem_id", ""),
        cwd=args.get("cwd", ""),
        methods=list(args.get("methods", []) or []) or None,
        timeout_s=int(args.get("timeout_s", 10) or 10),
    ),
    check_fn=check_lean_requirements,
    emoji="🧪",
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
    name="lean_auto_try",
    toolset="lean",
    schema=LEAN_AUTO_TRY_SCHEMA,
    handler=lambda args, **kw: lean_auto_try_tool(
        file_path=args.get("file_path", ""),
        theorem_id=args.get("theorem_id", ""),
        proof_attempt=args.get("proof_attempt", ""),
        cwd=args.get("cwd", ""),
        timeout_s=int(args.get("timeout_s", 10) or 10),
    ),
    check_fn=check_lean_requirements,
    emoji="🛠️",
)
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
