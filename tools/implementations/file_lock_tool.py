#!/usr/bin/env python3
"""Tool-facing wrappers for LeanFlow file reservations."""

from __future__ import annotations

import json

from leanflow_cli.runtime.file_locks import (
    acquire_file_lock as _acquire_file_lock,
)
from leanflow_cli.runtime.file_locks import (
    list_file_locks as _list_file_locks,
)
from leanflow_cli.runtime.file_locks import (
    release_file_lock as _release_file_lock,
)
from tools.registry import registry


def check_file_lock_requirements() -> bool:
    return True


def acquire_file_lock(
    path: str, owner_id: str, purpose: str = "", ttl_seconds: int = 1800, force: bool = False
) -> str:
    return json.dumps(
        _acquire_file_lock(
            path, owner_id=owner_id, purpose=purpose, ttl_seconds=ttl_seconds, force=force
        ),
        ensure_ascii=False,
    )


def release_file_lock(path: str, owner_id: str, force: bool = False) -> str:
    return json.dumps(
        _release_file_lock(path, owner_id=owner_id, force=force),
        ensure_ascii=False,
    )


def list_file_locks() -> str:
    locks = _list_file_locks()
    return json.dumps({"success": True, "count": len(locks), "locks": locks}, ensure_ascii=False)


FILE_LOCK_ACQUIRE_SCHEMA = {
    "name": "acquire_file_lock",
    "description": "Reserve a file for the current agent before editing it. Use this in user-approved swarm workflows so parallel agents do not edit the same Lean file concurrently.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Lean or project file to reserve"},
            "purpose": {
                "type": "string",
                "description": "Short reason for the reservation",
                "default": "",
            },
            "ttl_seconds": {
                "type": "integer",
                "description": "How long the reservation should last",
                "default": 1800,
            },
        },
        "required": ["path"],
    },
}


FILE_LOCK_RELEASE_SCHEMA = {
    "name": "release_file_lock",
    "description": "Release a file reservation held by the current agent.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Reserved file to release"},
        },
        "required": ["path"],
    },
}


FILE_LOCK_LIST_SCHEMA = {
    "name": "list_file_locks",
    "description": "List active LeanFlow file reservations so swarm agents can avoid conflicting edits.",
    "parameters": {"type": "object", "properties": {}},
}


registry.register(
    name="acquire_file_lock",
    toolset="coordination",
    schema=FILE_LOCK_ACQUIRE_SCHEMA,
    handler=lambda args, **kw: acquire_file_lock(
        path=args.get("path", ""),
        owner_id=str(kw.get("owner_id", "") or ""),
        purpose=args.get("purpose", ""),
        ttl_seconds=args.get("ttl_seconds", 1800),
    ),
    check_fn=check_file_lock_requirements,
    emoji="🔒",
)

registry.register(
    name="release_file_lock",
    toolset="coordination",
    schema=FILE_LOCK_RELEASE_SCHEMA,
    handler=lambda args, **kw: release_file_lock(
        path=args.get("path", ""),
        owner_id=str(kw.get("owner_id", "") or ""),
    ),
    check_fn=check_file_lock_requirements,
    emoji="🔓",
)

registry.register(
    name="list_file_locks",
    toolset="coordination",
    schema=FILE_LOCK_LIST_SCHEMA,
    handler=lambda args, **kw: list_file_locks(),
    check_fn=check_file_lock_requirements,
    emoji="🗂️",
)
