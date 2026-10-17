"""Check the shipped kernel inspector against the pinned compiler without accepting proofs."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.deadlines import remaining_seconds
from leanflow_cli.formalization.environment import compiler_executable
from leanflow_cli.formalization.lean_inspector import INSPECTOR
from leanflow_cli.workflows.prover.check_process import isolated_command

_MODE = "compiler-only-empty-kernel-replay"
_REPORTS = {
    "declaration_rows": "declarations.jsonl",
    "origin_parent_rows": "origin-parent-declarations.jsonl",
    "library_rows": "library-evidence.jsonl",
}
_MARKER = {
    "readiness_only": True,
    "success": True,
    "proof_acceptance": False,
    "requested_modules": ["Init.Prelude"],
    "requested_names": [],
}


def _checker_sha256() -> str:
    """Bind readiness to the exact current trusted inspector source."""
    return hashlib.sha256(INSPECTOR.encode()).hexdigest()


def valid_inspector_readiness(record: Any) -> bool:
    """Recognize only current compiler-only readiness, never declaration acceptance."""
    keys = {
        "schema_version",
        "readiness_only",
        "success",
        "mode",
        "checker_sha256",
        "compiler_library",
        "requested_modules",
        "requested_names",
        "proof_acceptance",
        "kernel_replay",
        *_REPORTS,
    }
    if (
        not isinstance(record, dict)
        or set(record) != keys
        or type(record.get("schema_version")) is not int
        or record["schema_version"] != 1
        or record.get("readiness_only") is not True
        or record.get("success") is not True
        or record.get("proof_acceptance") is not False
        or record.get("mode") != _MODE
        or record.get("checker_sha256") != _checker_sha256()
        or record.get("requested_modules") != ["Init.Prelude"]
        or record.get("requested_names") != []
        or any(type(record.get(key)) is not int or record[key] != 0 for key in _REPORTS)
    ):
        return False
    library = record.get("compiler_library")
    if (
        not isinstance(library, str)
        or not Path(library).is_absolute()
        or ".." in Path(library).parts
    ):
        return False
    replay = record.get("kernel_replay")
    if not isinstance(replay, dict) or set(replay) != {
        "modules",
        "declarations",
        "roots",
        "scanned_modules",
        "scanned_declarations",
        "axiom_hygiene_scope",
        "skipped_implementation_declarations",
        "trusted_modules",
        "imports",
        "scope",
    }:
        return False
    if (
        any(
            replay.get(key) != []
            for key in (
                "modules",
                "roots",
                "scanned_modules",
                "skipped_implementation_declarations",
            )
        )
        or any(
            type(replay.get(key)) is not int or replay[key] != 0
            for key in ("declarations", "scanned_declarations")
        )
        or replay.get("axiom_hygiene_scope") != "noncompiler-import-closure"
        or replay.get("scope") != "referenced-dependency-closure"
    ):
        return False
    trusted, imports = replay.get("trusted_modules"), replay.get("imports")
    if (
        not isinstance(trusted, list)
        or not trusted
        or any(not isinstance(name, str) or not name for name in trusted)
        or "Init.Prelude" not in trusted
        or len(set(trusted)) != len(trusted)
        or not isinstance(imports, list)
        or len(imports) != len(trusted)
    ):
        return False
    names = []
    for row in imports:
        if (
            not isinstance(row, dict)
            or set(row) != {"module", "path", "role"}
            or not isinstance(row.get("module"), str)
            or not isinstance(row.get("path"), str)
            or row.get("role") != "compiler"
        ):
            return False
        path = Path(row["path"])
        if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(library):
            return False
        names.append(row["module"])
    return len(set(names)) == len(names) and set(names) == set(trusted)


def _failure(details: dict[str, Any]) -> dict[str, Any]:
    """Keep compile, cancellation and report failures separate from proof acceptance."""
    return {
        "readiness_only": True,
        "success": False,
        "proof_acceptance": False,
        "mode": _MODE,
        "checker_sha256": _checker_sha256(),
        "error": "Independent kernel inspector readiness failed",
        "details": details,
    }


def check_inspector_runtime(
    root: Path,
    workspace: Path,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, Any]:
    """Run the exact shipped inspector on an empty pinned-compiler-only replay.

    The caller supplies its preparation supervisor and absolute campaign deadline.
    Retain the private diagnostic workspace until that owner verifies shutdown.
    No project layout, dependency census, policy evaluation or mathematical source
    participates in this compatibility check.
    """

    def stopped() -> dict[str, Any] | None:
        if cancelled is not None and cancelled():
            return _failure(
                {"error_code": "check_cancelled", "error": "Inspector readiness cancelled"}
            )
        remaining = remaining_seconds(deadline)
        if remaining is not None and remaining <= 0:
            return _failure({"error_code": "check_timeout", "timed_out": True})
        return None

    if failure := stopped():
        return failure
    try:
        if os.environ.get("LEAN_PATH") or os.environ.get("ELAN_TOOLCHAIN"):
            raise ValueError("Inspector readiness rejects compiler or loader overrides")
        lean = compiler_executable(root, "lean")
        compiler = (Path(lean).parent.parent / "lib" / "lean").resolve(strict=True)
        if failure := stopped():
            return failure
        workspace.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="inspector-readiness-", dir=workspace)).resolve()
        script = work / "Inspect.lean"
        script.write_text(INSPECTOR, encoding="utf-8")
        inputs = work / "compiler-inputs.json"
        inputs.write_text(json.dumps({"paths": [], "artifacts": {}, "roles": {}}), encoding="utf-8")
        result = isolated_command(
            project_root=root,
            workspace=work,
            argv=[
                lean,
                "-j1",
                "--run",
                str(script),
                str(work),
                str(compiler),
                str(compiler),
                "readiness",
                str(inputs),
                "Init.Prelude",
                "--",
            ],
            timeout_s=remaining_seconds(deadline),
            cancelled=cancelled,
        )
        if not result.get("success"):
            return _failure(result)
        if failure := stopped():
            return failure
        marker = json.loads((work / "inspector-readiness.json").read_text())
        if (
            marker != _MARKER
            or any(
                type(marker.get(key)) is not bool
                for key in ("readiness_only", "success", "proof_acceptance")
            )
            or hashlib.sha256(script.read_bytes()).hexdigest() != _checker_sha256()
        ):
            raise ValueError("Inspector readiness mode or checker source changed")
        record = {
            "schema_version": 1,
            **_MARKER,
            "mode": _MODE,
            "checker_sha256": _checker_sha256(),
            "compiler_library": str(compiler),
            **{key: len((work / name).read_text().splitlines()) for key, name in _REPORTS.items()},
            "kernel_replay": json.loads((work / "kernel-replay.json").read_text()),
        }
        if not valid_inspector_readiness(record):
            raise ValueError("Inspector readiness produced nonempty or untrusted evidence")
        return record
    except (OSError, ValueError) as error:
        return _failure({"error_code": "check_setup_failed", "error": str(error)})
