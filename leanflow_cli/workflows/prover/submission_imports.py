"""Stage existing-module import requests under protected kernel-type verification."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from leanflow_cli.workflows.prover.models import Node
from leanflow_cli.workflows.prover.source import SourceDocument, read_source
from leanflow_cli.workflows.prover.submission_cache import imported_modules

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime

_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*\Z")


def remember_imports(
    runtime: ProverRuntime, node: Node, job: dict[str, Any], report: dict[str, Any]
) -> None:
    """Retain explicit or scratch-header imports without changing canonical source."""
    modules = report.get("imports")
    if "imports" not in report:
        modules = list(job.get("candidate_imports", []))
        scratch = Path(job.get("scratch_path", ""))
        if scratch.is_file() and not scratch.is_symlink():
            baseline = set(imported_modules(runtime.documents[node.file].render()))
            modules += [m for m in imported_modules(read_source(scratch)) if m not in baseline]
    if (
        not isinstance(modules, list)
        or len(modules) > 16
        or any(not isinstance(m, str) or not _MODULE.fullmatch(m) for m in modules)
    ):
        raise ValueError("Imports must be a list of at most 16 Lean module names.")
    if node.module in modules:
        raise ValueError("A proof cannot import its own module.")
    if getattr(runtime, "formalization_handoff", None) is not None:
        existing = set(imported_modules(runtime.documents[node.file].baseline))
        if set(modules) - existing:
            raise ValueError(
                "This reviewed formalization permits only its named proof-slot replacements. "
                "Use the existing imports, or revise and review the formalization before adding imports."
            )
    job["candidate_imports"] = list(dict.fromkeys(modules))
    node.candidate_imports = list(job["candidate_imports"])


def candidate_document(runtime: ProverRuntime, node: Node) -> SourceDocument:
    """Copy the current document and stage the candidate's additive imports."""
    document = copy.deepcopy(runtime.documents[node.file])
    existing = set(imported_modules(document.render()))
    for module in node.candidate_imports:
        if not isinstance(module, str) or not _MODULE.fullmatch(module) or module == node.module:
            raise ValueError("Invalid retained proof import.")
        if module not in existing:
            document.proof_imports.append(module)
            existing.add(module)
    return document


def check_import_types(
    runtime: ProverRuntime, node: Node, document: SourceDocument, workspace: Path
) -> dict[str, Any]:
    """Require new imports to preserve every managed declaration's kernel type."""
    if getattr(runtime, "formalization_handoff", None) is not None and (
        document.imports or document.proof_imports or document.generated
    ):
        return {
            "accepted": False,
            "error": "Reviewed formalization proof scope forbids new imports and generated helper files; revise the formalization before changing its setup.",
        }
    if document.proof_imports == runtime.documents[node.file].proof_imports:
        return {"accepted": True}
    if not hasattr(runtime.verifier, "capture_signatures"):
        return {"accepted": False, "error": "Import requests require protected-type verification."}
    documents = copy.deepcopy(runtime.documents)
    documents[node.file] = document
    return runtime.verifier.capture_signatures(
        copy.deepcopy(runtime.dag), documents, workspace, initialize=False
    )


def recover_saved_import_candidates(runtime: ProverRuntime) -> None:
    """Recheck saved import-dependent proofs before spending another recovery session."""
    recovered: set[str] = set()
    for job in reversed(runtime.state.get("jobs", [])):
        node = runtime.dag.by_id().get(job.get("node_id", ""))
        if (
            node is None
            or node.id in recovered
            or node.status == "proved"
            or job.get("role") != "prover"
            or job.get("node_revision") != node.revision
        ):
            continue
        result_path = Path(job.get("result_path", ""))
        if not result_path.is_file() or result_path.is_symlink():
            continue
        result = json.loads(result_path.read_text())
        candidate, _ = runtime._candidate(job, result, node)
        if not candidate or not job.get("candidate_imports"):
            continue
        runtime._retain_candidate(job, result)
        recovered.add(node.id)
    if recovered:
        runtime._persist()
        runtime._promote_candidates()
        for node_id in recovered:
            if runtime.dag.by_id()[node_id].status == "proved":
                runtime._dismiss_recovery(node_id)
