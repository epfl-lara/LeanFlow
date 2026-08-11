"""Bind worker-checked helper candidates to exact decomposer-created queue children.

This module does not promote worker evidence or edit Lean source.  It resolves
only a one-hop handoff from a committed decomposer parent to the unchanged
child stub currently assigned by the queue.  The foreground prover must still
recheck the full candidate as a replacement for that child before editing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_decomposition_shape import exact_sorry_stub_shape_ok
from leanflow_cli.workflows import decomposition_provenance, plan_state

_SHA256_RE = re.compile(r"[0-9a-f]{64}", flags=re.IGNORECASE)


@dataclass(frozen=True)
class QueuedHelperBinding:
    """Describe one unchanged decomposer child and its originating parent revision."""

    target_symbol: str
    active_file: str
    parent_symbol: str
    parent_assignment_revision: str
    source_revision_sha256: str
    queued_declaration_sha256: str
    signature_sha256: str
    transaction_id: str


@dataclass(frozen=True)
class QueuedHelperCandidate:
    """Carry one exact worker candidate without granting it proof authority."""

    job_id: str
    consumed_at: str
    declaration: str
    declaration_sha256: str
    source_helper_name: str
    name_only_adaptation: bool = False


@dataclass(frozen=True)
class ProvedHelperHandback:
    """Describe a proved decomposition child awaiting parent integration."""

    target_symbol: str
    active_file: str
    helper_symbol: str


def _same_file(left: str, right: str) -> bool:
    """Return whether two paths identify the same source under the project root."""
    if not left or not right:
        return left == right
    project_root = str(os.getenv("LEANFLOW_PROJECT_ROOT", "") or os.getcwd())

    def canonical(value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(project_root) / path
        return str(path.resolve(strict=False))

    return canonical(left) == canonical(right)


def _solved_queue_outcome(
    summary: Mapping[str, Any], *, target_symbol: str, active_file: str
) -> bool:
    """Return whether the queue manager durably accepted the exact declaration."""
    manager_state = summary.get("queue_manager_state")
    if not isinstance(manager_state, Mapping):
        return False
    raw_outcomes = manager_state.get("theorem_outcomes")
    if isinstance(raw_outcomes, Mapping):
        outcomes: Sequence[object] = tuple(raw_outcomes.values())
    elif isinstance(raw_outcomes, Sequence) and not isinstance(
        raw_outcomes, (str, bytes, bytearray)
    ):
        outcomes = raw_outcomes
    else:
        return False
    for raw_outcome in reversed(outcomes):
        if not isinstance(raw_outcome, Mapping):
            continue
        if (
            str(raw_outcome.get("target_symbol", "") or "").strip() == target_symbol
            and _same_file(str(raw_outcome.get("active_file", "") or ""), active_file)
            and str(raw_outcome.get("status", "") or "").strip().lower()
            in {"solved", "proved", "verified", "success"}
        ):
            return True
    return False


def proved_helper_handback(
    summary: Mapping[str, Any],
    blueprint: plan_state.Blueprint,
    *,
    target_symbol: str,
    active_file: str,
) -> ProvedHelperHandback | None:
    """Return a newly proved split helper that the unresolved parent has not consumed.

    Graph status alone is advisory. Require the reciprocal decomposition edges,
    a durable successful queue outcome, exact source declarations, and an
    unresolved parent body that does not yet reference the helper. The parent
    still receives an ordinary foreground proving turn and remains subject to
    the normal kernel and placeholder gates.
    """
    target = str(target_symbol or "").strip()
    file_label = str(active_file or "").strip()
    if not target or not file_label:
        return None
    parent_nodes = [
        node
        for node in blueprint.nodes
        if node.name == target and _same_file(node.file, file_label)
    ]
    if len(parent_nodes) != 1:
        return None
    parent = parent_nodes[0]
    if parent.status not in {"stated", "audited", "proving", "blocked"}:
        return None
    source_path = Path(file_label).expanduser()
    if not source_path.is_absolute():
        source_path = Path(str(os.getenv("LEANFLOW_PROJECT_ROOT", "") or os.getcwd())) / source_path
    try:
        source = source_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    parent_slice = decomposition_provenance.declaration_slice(source, target)
    if parent_slice is None or not re.search(
        r"\b(?:sorry|admit|sorryAx)\b", parent_slice.text, flags=re.IGNORECASE
    ):
        return None
    dependency_ids = {
        edge.target
        for edge in blueprint.edges
        if edge.kind == "depends_on" and edge.source == parent.id
    }
    split_ids = {
        edge.source
        for edge in blueprint.edges
        if edge.kind == "split_of" and edge.target == parent.id
    }
    candidates = [
        node
        for node in blueprint.nodes
        if node.id in dependency_ids & split_ids
        and node.status == "proved"
        and node.generated_by == "decomposer"
        and _same_file(node.file, file_label)
        and _solved_queue_outcome(
            summary,
            target_symbol=node.name,
            active_file=file_label,
        )
    ]
    for helper in reversed(candidates):
        helper_slice = decomposition_provenance.declaration_slice(source, helper.name)
        if helper_slice is None or re.search(
            r"\b(?:sorry|admit|sorryAx)\b", helper_slice.text, flags=re.IGNORECASE
        ):
            continue
        if re.search(
            rf"(?<![A-Za-z0-9_']){re.escape(helper.name)}(?![A-Za-z0-9_'])",
            parent_slice.text,
        ):
            continue
        return ProvedHelperHandback(
            target_symbol=target,
            active_file=str(source_path.resolve(strict=False)),
            helper_symbol=helper.name,
        )
    return None


def _matching_helper_record(
    summary: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
    current_child: decomposition_provenance.DeclarationSlice,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return the newest committed record that owns the exact unchanged child stub."""
    records = summary.get("decomposition_provenance")
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes, bytearray)):
        return None
    for raw_record in reversed(records):
        if not isinstance(raw_record, Mapping):
            continue
        record = dict(raw_record)
        parent_symbol = str(record.get("parent", "") or "").strip()
        parent_before = str(record.get("parent_before_declaration", "") or "")
        parent_before_slice = decomposition_provenance.declaration_slice(
            parent_before, parent_symbol
        )
        if (
            str(record.get("state", "") or "") != "committed"
            or not _same_file(str(record.get("file", "") or ""), active_file)
            or not parent_symbol
            or not _SHA256_RE.fullmatch(str(record.get("transaction_id", "") or ""))
            or parent_before_slice is None
            or parent_before_slice.text != parent_before.strip()
            or parent_before_slice.declaration_sha256
            != str(record.get("parent_before_declaration_sha256", "") or "")
            or parent_before_slice.signature_sha256
            != str(record.get("parent_signature_sha256", "") or "")
        ):
            continue
        raw_helpers = record.get("helpers")
        if not isinstance(raw_helpers, Sequence) or isinstance(
            raw_helpers, (str, bytes, bytearray)
        ):
            continue
        for raw_helper in raw_helpers:
            if not isinstance(raw_helper, Mapping):
                continue
            helper = dict(raw_helper)
            inserted = str(helper.get("inserted_declaration", "") or "")
            if (
                str(helper.get("name", "") or "").strip() != target_symbol
                or not inserted
                or inserted != current_child.text
                or str(helper.get("declaration_sha256", "") or "")
                != current_child.declaration_sha256
                or str(helper.get("signature_sha256", "") or "") != current_child.signature_sha256
                or hashlib.sha256(inserted.encode("utf-8")).hexdigest()
                != current_child.declaration_sha256
            ):
                continue
            return record, helper
    return None


def _has_candidate_record(
    summary: Mapping[str, Any], *, target_symbol: str, active_file: str
) -> bool:
    """Return whether durable provenance could own this exact child assignment."""
    records = summary.get("decomposition_provenance")
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes, bytearray)):
        return False
    for record in records:
        if (
            not isinstance(record, Mapping)
            or str(record.get("state", "") or "") != "committed"
            or not _same_file(str(record.get("file", "") or ""), active_file)
        ):
            continue
        helpers = record.get("helpers")
        if not isinstance(helpers, Sequence) or isinstance(helpers, (str, bytes, bytearray)):
            continue
        if any(
            isinstance(helper, Mapping)
            and str(helper.get("name", "") or "").strip() == target_symbol
            for helper in helpers
        ):
            return True
    return False


def resolve_queued_helper_binding(
    summary: Mapping[str, Any],
    blueprint: plan_state.Blueprint,
    *,
    target_symbol: str,
    active_file: str,
) -> QueuedHelperBinding | None:
    """Resolve an exact current child stub to its committed parent revision.

    The source is read under the same path/inode lease used by decomposition
    writes.  A changed child declaration, parent signature, graph statement,
    malformed provenance record, or unstable path fails closed.
    """
    target = str(target_symbol or "").strip()
    file_label = str(active_file or "").strip()
    if not target or not file_label:
        return None
    if not _has_candidate_record(summary, target_symbol=target, active_file=file_label):
        return None
    source_path = Path(file_label).expanduser()
    if not source_path.is_absolute():
        project_root = Path(str(os.getenv("LEANFLOW_PROJECT_ROOT", "") or os.getcwd()))
        source_path = project_root / source_path
    try:
        with decomposition_provenance.source_operation(source_path) as operation:
            source_bytes = decomposition_provenance.read_source_bytes(operation)
            source = source_bytes.decode("utf-8")
            canonical_file = str(operation.path)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    current_child = decomposition_provenance.declaration_slice(source, target)
    if current_child is None or not exact_sorry_stub_shape_ok(current_child.text):
        return None
    matched = _matching_helper_record(
        summary,
        target_symbol=target,
        active_file=canonical_file,
        current_child=current_child,
    )
    if matched is None:
        return None
    record, _helper = matched
    parent_symbol = str(record.get("parent", "") or "").strip()
    current_parent = decomposition_provenance.declaration_slice(source, parent_symbol)
    if current_parent is None or current_parent.signature_sha256 != str(
        record.get("parent_signature_sha256", "") or ""
    ):
        return None
    # Graph files may retain the user-supplied alias while provenance owns the
    # canonical path.  Require exactly one equivalent identity so a corrupted
    # duplicate graph cannot choose an arbitrary parent revision.
    parent_nodes = [
        node
        for node in blueprint.nodes
        if node.name == parent_symbol and _same_file(node.file, canonical_file)
    ]
    if len(parent_nodes) != 1:
        return None
    parent_statement = str(parent_nodes[0].statement or "")
    if not parent_statement.strip():
        return None
    return QueuedHelperBinding(
        target_symbol=target,
        active_file=canonical_file,
        parent_symbol=parent_symbol,
        parent_assignment_revision=hashlib.sha256(parent_statement.encode("utf-8")).hexdigest(),
        source_revision_sha256=hashlib.sha256(source_bytes).hexdigest(),
        queued_declaration_sha256=current_child.declaration_sha256,
        signature_sha256=current_child.signature_sha256,
        transaction_id=str(record.get("transaction_id", "") or "").strip(),
    )


def ready_to_prove_binding(
    summary: Mapping[str, Any],
    blueprint: plan_state.Blueprint,
    *,
    target_symbol: str,
    active_file: str,
) -> QueuedHelperBinding | None:
    """Return one untouched zero-attempt decomposer child for foreground proof.

    Exact source provenance, the reciprocal graph relationship, and zero graph
    effort jointly distinguish a newly placed helper from an older difficult
    child.  This gives the former one proof turn without suppressing later
    research routing after a genuine attempt.
    """
    binding = resolve_queued_helper_binding(
        summary,
        blueprint,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    if binding is None:
        return None
    child_nodes = [
        node
        for node in blueprint.nodes
        if node.name == binding.target_symbol and _same_file(node.file, binding.active_file)
    ]
    parent_nodes = [
        node
        for node in blueprint.nodes
        if node.name == binding.parent_symbol and _same_file(node.file, binding.active_file)
    ]
    if len(child_nodes) != 1 or len(parent_nodes) != 1:
        return None
    child = child_nodes[0]
    parent = parent_nodes[0]
    if (
        child.generated_by != "decomposer"
        or child.status not in {"stated", "audited", "proving"}
        or child.attempts != 0
        or child.api_steps != 0
        or not any(
            edge.kind == "split_of" and edge.source == child.id and edge.target == parent.id
            for edge in blueprint.edges
        )
        or not any(
            edge.kind == "depends_on" and edge.source == parent.id and edge.target == child.id
            for edge in blueprint.edges
        )
    ):
        return None
    return binding


def _checked_declaration_slice(
    declaration: str,
    replacement_declarations: Sequence[object],
) -> decomposition_provenance.DeclarationSlice | None:
    """Return one unambiguous declaration named by the worker check."""
    slices: dict[str, decomposition_provenance.DeclarationSlice] = {}
    for raw_name in replacement_declarations:
        name = str(raw_name or "").strip().removeprefix("_root_.")
        for candidate_name in dict.fromkeys((name, name.rsplit(".", 1)[-1])):
            if not candidate_name:
                continue
            candidate = decomposition_provenance.declaration_slice(
                declaration,
                candidate_name,
            )
            if candidate is not None:
                slices[candidate.declaration_sha256] = candidate
    return next(iter(slices.values())) if len(slices) == 1 else None


def _rename_declaration_head(
    declaration: str,
    source: decomposition_provenance.DeclarationSlice,
    target_symbol: str,
) -> str:
    """Rename only the declaration-head identifier, or return an empty value."""
    pattern = re.compile(rf"(?<![A-Za-z0-9_']){re.escape(source.name)}(?![A-Za-z0-9_'])")
    match = pattern.search(source.signature)
    if match is None:
        return ""
    return (
        declaration[: match.start()] + str(target_symbol or "").strip() + declaration[match.end() :]
    )


def candidate_from_checked_helper(
    binding: QueuedHelperBinding,
    helper: Mapping[str, Any],
    *,
    job_id: str,
    consumed_at: str,
) -> QueuedHelperCandidate | None:
    """Return a same-signature child candidate from canonical worker evidence.

    A worker may prove the exact helper statement under a different declaration
    name before the decomposer chooses its child name.  A name-only adaptation
    is safe as a *hint* because this function requires the adapted signature to
    equal the untouched child signature and the foreground contract still
    reruns Lean against the current source before editing.
    """
    declaration = str(helper.get("declaration", "") or "")
    declaration_sha256 = str(helper.get("declaration_sha256", "") or "").strip()
    worker_check = helper.get("worker_check")
    replacement_declarations = (
        worker_check.get("replacement_declarations") if isinstance(worker_check, Mapping) else None
    )
    replacement_names = (
        tuple(replacement_declarations)
        if isinstance(replacement_declarations, Sequence)
        and not isinstance(replacement_declarations, (str, bytes, bytearray))
        else ()
    )
    if (
        str(helper.get("anchor_target_symbol", "") or "").strip().removeprefix("_root_.")
        != binding.parent_symbol.removeprefix("_root_.")
        or not _same_file(str(helper.get("active_file", "") or ""), binding.active_file)
        or not declaration.strip()
        or hashlib.sha256(declaration.encode("utf-8")).hexdigest() != declaration_sha256
        or not replacement_names
    ):
        return None
    source = _checked_declaration_slice(declaration, replacement_names)
    if source is None or source.text != declaration.strip():
        return None
    normalized_target = binding.target_symbol.removeprefix("_root_.")
    normalized_source = source.name.removeprefix("_root_.")
    name_only_adaptation = normalized_source != normalized_target
    candidate_declaration = (
        _rename_declaration_head(declaration, source, binding.target_symbol)
        if name_only_adaptation
        else declaration
    )
    if not candidate_declaration:
        return None
    candidate = decomposition_provenance.declaration_slice(
        candidate_declaration,
        binding.target_symbol,
    )
    candidate_sha256 = hashlib.sha256(candidate_declaration.encode("utf-8")).hexdigest()
    if (
        candidate is None
        or candidate.text != candidate_declaration.strip()
        or candidate.signature_sha256 != binding.signature_sha256
        or candidate.declaration_sha256 != candidate_sha256
        or exact_sorry_stub_shape_ok(candidate.text)
    ):
        return None
    return QueuedHelperCandidate(
        job_id=str(job_id or "").strip(),
        consumed_at=str(consumed_at or "").strip(),
        declaration=candidate_declaration,
        declaration_sha256=candidate_sha256,
        source_helper_name=source.name,
        name_only_adaptation=name_only_adaptation,
    )


def render_queued_helper_candidates(
    binding: QueuedHelperBinding,
    candidates: Sequence[QueuedHelperCandidate],
) -> list[str]:
    """Render exact proof hints with an explicit current-source foreground gate."""
    if not candidates:
        return []
    lines = [
        "Queued decomposer-child candidate handoff:",
        "- authority: WORKER-CHECKED HINT ONLY; it is not current-source proof evidence and "
        "cannot close or verify this queue item",
        f"- originating parent: `{binding.parent_symbol}` at assignment revision "
        f"`{binding.parent_assignment_revision}`",
        f"- current source binding: source sha256 `{binding.source_revision_sha256}`, queued "
        f"declaration sha256 `{binding.queued_declaration_sha256}`, signature sha256 "
        f"`{binding.signature_sha256}`",
        f"- decomposition transaction: `{binding.transaction_id or '[unavailable]'}`",
        "- REQUIRED BEFORE EDITING: rerun `lean_incremental_check` with "
        f"`action=check_target`, `theorem_id={binding.target_symbol}`, and the exact candidate "
        "as `replacement` against the current active file; insert it only if that foreground "
        "result is sorry-free, error-free, and matches the assigned target",
        "- consume this exact candidate before invoking `lean_decompose_helpers` or repeating "
        "the originating search; a failed foreground recheck is new route evidence",
        "- after insertion, the ordinary manager/kernel gate remains the only proof authority",
    ]
    for candidate in candidates:
        adaptation = (
            f"; name-only adaptation from worker helper `{candidate.source_helper_name}`"
            if candidate.name_only_adaptation
            else ""
        )
        lines.extend(
            [
                f"- candidate from `{candidate.job_id or '[unknown job]'}` consumed "
                f"{candidate.consumed_at or '[unknown time]'}; declaration sha256 "
                f"`{candidate.declaration_sha256}`{adaptation}",
                "  exact_candidate_declaration_json: "
                + json.dumps(candidate.declaration, ensure_ascii=False),
            ]
        )
    return lines
