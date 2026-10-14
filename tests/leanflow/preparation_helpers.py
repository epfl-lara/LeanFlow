"""Supply explicit compiler-readiness metadata for preparation tests with mocked tool execution."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.lean_inspector import INSPECTOR


def inspector_readiness_record() -> dict[str, Any]:
    """Describe a zero-declaration unit fixture without claiming real Lean execution."""
    library = str(Path(__file__).resolve().parent / "unit-fixture/compiler/lib/lean")
    return {
        "schema_version": 1,
        "readiness_only": True,
        "success": True,
        "mode": "compiler-only-empty-kernel-replay",
        "checker_sha256": hashlib.sha256(INSPECTOR.encode()).hexdigest(),
        "compiler_library": library,
        "requested_modules": ["Init.Prelude"],
        "requested_names": [],
        "declaration_rows": 0,
        "origin_parent_rows": 0,
        "library_rows": 0,
        "proof_acceptance": False,
        "kernel_replay": {
            "modules": [],
            "declarations": 0,
            "roots": [],
            "scanned_modules": [],
            "scanned_declarations": 0,
            "axiom_hygiene_scope": "noncompiler-import-closure",
            "skipped_implementation_declarations": [],
            "trusted_modules": ["Init.Prelude"],
            "imports": [
                {
                    "module": "Init.Prelude",
                    "path": str(Path(library) / "Init/Prelude.olean"),
                    "role": "compiler",
                }
            ],
            "scope": "referenced-dependency-closure",
        },
    }
