"""Materialize mechanically validated proof decompositions.

The orchestrator's ``decompose`` route asks the helper backend for managed
skeletons, guards their shape and trust profile, places them immediately before
the target, validates the contiguous batch with LeanProbe, records the split in
the dependency graph, and journals each action. File order makes the queue
assign the new helpers before their parent.

Writes use byte-exact source compare-and-swap between prover turns. Every write
passes the same forbidden-axiom scan as prover edits; a stated stub is exactly
``theorem/lemma … := by sorry``; and any validation error reverts the batch.
After success the caller must refresh queue-edit guard caches via
``refresh_queue_edit_guard``.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_decomposition_shape import exact_sorry_stub_shape_ok
from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _find_assignment_marker_for_statement,
    _strip_lean_comments_and_strings,
)
from leanflow_cli.workflows import campaign_epoch, decomposition_provenance, plan_state
from leanflow_cli.workflows.queue_edit_guard import _introduced_forbidden_axioms
from leanflow_cli.workflows.workflow_json_io import update_json_file
from tools.utilities import decomposer_admission
from tools.utilities.interrupt import CooperativeInterrupt, raise_if_interrupted

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return a compact UTC timestamp for migration audit records."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _nonnegative_counter(value: Any, default: int = 0) -> int:
    """Return one persisted counter as a non-negative integer."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _positive_limit(value: Any, default: int) -> int:
    """Return one persisted campaign limit as a positive integer."""
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))


#: Similarity above which a child statement counts as absorbing the parent's
#: whole difficulty. This structural check prevents offloading the goal to a
#: near-duplicate helper.
_OFFLOADING_SIMILARITY = 0.92

#: Numerals 0 and 1 are structural constants used pervasively in harmless
#: helper statements. Larger constants need to be inherited from the parent
#: before an LLM may use them in a concrete bound.
_STRUCTURAL_NUMERALS = frozenset({"0", "1"})
_NUMERAL_RE = re.compile(r"(?<![A-Za-z_])\d+(?![A-Za-z_])")
_ORDER_RELATION_RE = re.compile(r"(?:<=|>=|≤|≥|<|>)")
_PROVER_EDIT_EVIDENCE_EDGE_MIGRATION = "prover-edit-unused-helper-evidence-v3"
_LEAN_IDENTIFIER_RE = re.compile(r"(?:[^\W\d]|_)[\w']*(?:\.(?:[^\W\d]|_)[\w']*)*")
_FIRST_CONCRETE_NEXT_EDIT_LIMIT = 1600
_EDITABLE_DEPENDENCY_GENERATORS = frozenset({"decomposer", "prover-edit", "prover-edit-backfill"})


def _bounded_first_concrete_next_edit(value: Any) -> str:
    """Return one compact, bounded decomposer action for prover handoff."""
    collapsed = " ".join(str(value or "").split())
    if len(collapsed) <= _FIRST_CONCRETE_NEXT_EDIT_LIMIT:
        return collapsed
    return collapsed[: _FIRST_CONCRETE_NEXT_EDIT_LIMIT - 3] + "..."


@dataclass(frozen=True)
class DecomposeOutcome:
    """Report decomposition placement and the underlying advisor call status."""

    ok: bool
    reason: str = ""
    placed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    file: str = ""
    requires_pause: bool = False
    obstacle_summary: str = ""
    recommended_split: str = ""
    first_concrete_next_edit: str = ""
    advisor_success: bool | None = None
    advisor_status: str = ""
    advisor_provider_called: bool | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "placed": list(self.placed),
            "skipped": list(self.skipped),
            "file": self.file,
            "requires_pause": self.requires_pause,
            "obstacle_summary": self.obstacle_summary,
            "recommended_split": self.recommended_split,
            "first_concrete_next_edit": self.first_concrete_next_edit,
            "advisor_success": self.advisor_success,
            "advisor_status": self.advisor_status,
            "advisor_provider_called": self.advisor_provider_called,
        }


@dataclass(frozen=True)
class GraphRollbackOutcome:
    """Report whether an interrupted decomposition graph was safely retired."""

    ok: bool
    reason: str = ""
    removed: tuple[str, ...] = ()
    already_absent: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProverHelperGraphUpdate:
    """Report helper relationships written by one accepted prover edit."""

    introduced: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    proof_support: tuple[str, ...] = ()
    promoted: tuple[str, ...] = ()


def stub_shape_ok(skeleton: str) -> bool:
    """True iff the skeleton is exactly ONE sorry-bodied theorem/lemma stub.

    Independent checks over comment/string-stripped text (a lone regex is
    bypassable by anchoring on the final ``:= by sorry`` across smuggled
    declarations, including same-line ones): the structural regex; exactly
    one declaration keyword anywhere; exactly one ``:=`` whose body strips
    to literally ``by sorry``; exactly one ``sorry`` token; and the real
    declaration parser agreeing on one theorem/lemma. Anything exotic is
    rejected — the decomposition then falls back to the prompt directive.
    """
    return exact_sorry_stub_shape_ok(skeleton)


_SLICE_HEADER_RE = re.compile(r"^Assigned declaration slice[^\n]*:\s*\n", re.IGNORECASE)


def normalize_statement(text: str) -> str:
    """Strip the queue-slice display header so the raw declaration remains.

    Assignment slices arrive prefixed 'Assigned declaration slice (N-M):' —
    that prefix must reach neither the decomposition backend nor the
    offloading similarity check.
    """
    return _SLICE_HEADER_RE.sub("", str(text or "").strip(), count=1).strip()


def _statement_core(text: str) -> str:
    """Normalize a declaration to its statement tokens for similarity checks."""
    body = normalize_statement(text)
    body = body.split(":=", 1)[0]
    body = re.sub(r"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+)?(?:theorem|lemma)\s+\S+", "", body)
    return " ".join(body.split())


def sorry_offloading_suspect(parent_statement: str, skeleton: str) -> bool:
    """True when a child statement is essentially the parent restated.

    Children must be strictly easier; one child restating the parent means the
    decomposition just moved the sorry (frontier-documented pathology).
    """
    parent = _statement_core(parent_statement)
    child = _statement_core(skeleton)
    if not parent or not child:
        return False
    if parent == child:
        return True
    ratio = difflib.SequenceMatcher(None, parent, child).ratio()
    return ratio >= _OFFLOADING_SIMILARITY


def unsupported_novel_bound_suspect(parent_statement: str, skeleton: str) -> bool:
    """Return whether a child invents a concrete bound absent from its parent.

    A sorry-bodied helper is only a work split, not evidence. In particular,
    an LLM must not turn an eventual/existential target into a stronger claim
    by guessing a threshold such as ``n >= 6 * a``. Such constants belong in
    an empirical or negation probe first. Zero and one are exempt because
    they are structural constants in ordinary Lean statements.

    The guard is deliberately conservative and syntax-level: it examines
    comparison atoms separated by the common logical delimiters. A rejected
    helper can still be introduced later with a real proof rather than a
    ``sorry`` body.
    """
    parent = _statement_core(parent_statement)
    child = _statement_core(skeleton)
    if not parent or not child:
        return False
    inherited = set(_NUMERAL_RE.findall(parent)) | set(_STRUCTURAL_NUMERALS)
    novel = set(_NUMERAL_RE.findall(child)) - inherited
    if not novel:
        return False
    atoms = re.split(r"[→∧∨,\n]", child)
    return any(
        _ORDER_RELATION_RE.search(atom)
        and any(
            re.search(rf"(?<![A-Za-z_]){re.escape(number)}(?![A-Za-z_])", atom) for number in novel
        )
        for atom in atoms
    )


def refresh_queue_edit_guard(agent: Any) -> None:
    """Reset the prover's stale per-agent guard caches after an out-of-turn edit.

    Without this the guard's protected-declaration inventory (cached per
    (symbol, file) key) and the per-file initial-declaration keys would treat
    the new stubs as illegal edits and restore them away.
    """
    if agent is None:
        return
    for attr in ("_managed_queue_edit_guard_state", "_managed_initial_declaration_keys_by_file"):
        try:
            setattr(agent, attr, {})
        except Exception:
            logger.debug("guard-cache refresh failed for %s", attr, exc_info=True)


def _target_insertion_offset(content: str, target_symbol: str) -> int | None:
    """Character offset just ABOVE the target's metadata block.

    Doc comments and attribute lines directly above the declaration belong to
    it — inserting between them and the keyword would re-attach them to the
    helper. Walk upward over contiguous attribute lines and doc-comment
    blocks before computing the offset.
    """
    try:
        entries = _declaration_line_index_from_text(content)
    except Exception:
        return None
    target_line = None
    for entry in entries:
        if str(entry.get("name", "") or "") == target_symbol:
            target_line = max(1, int(entry.get("line", 1) or 1))
            break
    if target_line is None:
        return None
    lines = content.splitlines(keepends=True)
    index = target_line - 1  # first line of the block, 0-based
    while index > 0:
        previous = lines[index - 1].strip()
        if previous.startswith("@["):
            index -= 1
            continue
        if previous.endswith("-/"):
            # Walk to the opening of the doc/comment block.
            cursor = index - 1
            while cursor >= 0 and not lines[cursor].lstrip().startswith(("/--", "/-")):
                cursor -= 1
            if cursor < 0:
                break
            index = cursor
            continue
        break
    return sum(len(text) for text in lines[:index])


def _source_newline(content: str) -> str:
    """Return the source's first line-ending style, defaulting to LF."""
    match = re.search(r"\r\n|\n|\r", content)
    return match.group(0) if match else "\n"


def _normalize_stub_newlines(stub: str, newline: str) -> str:
    """Render one guarded stub with the target source's line endings."""
    return newline.join(stub.splitlines())


def _existing_declarations_by_name(source: str) -> dict[str, str]:
    """Return exact parsed declaration slices keyed by unqualified source name."""
    try:
        entries = _declaration_line_index_from_text(source)
    except Exception:
        return {}
    return {
        str(entry.get("name", "") or "").strip(): str(entry.get("text", "") or "").strip()
        for entry in entries
        if str(entry.get("name", "") or "").strip()
    }


def place_helpers(
    *,
    active_file: str,
    target_symbol: str,
    skeletons: Sequence[str],
    allowed_axioms: Sequence[str],
    helper_dependencies: Mapping[str, Sequence[str]] | None = None,
    cwd: str = "",
) -> DecomposeOutcome:
    """Write guarded helper stubs before the target and verify them in place.

    All-or-nothing: shape check and axiom scan run BEFORE the write; the
    contiguous batch must then elaborate via LeanProbe (sorry warnings fine,
    errors revert the entire write).
    """
    raise_if_interrupted("helper placement interrupted before source lease")
    try:
        with decomposition_provenance.source_operation(Path(active_file)) as operation:
            return _place_helpers_under_lease(
                operation=operation,
                target_symbol=target_symbol,
                skeletons=skeletons,
                allowed_axioms=allowed_axioms,
                helper_dependencies=helper_dependencies,
                cwd=cwd,
            )
    except (OSError, RuntimeError) as exc:
        return DecomposeOutcome(ok=False, reason=f"unreadable target file: {exc}")


def _place_helpers_under_lease(
    *,
    operation: decomposition_provenance.SourceOperation,
    target_symbol: str,
    skeletons: Sequence[str],
    allowed_axioms: Sequence[str],
    helper_dependencies: Mapping[str, Sequence[str]] | None,
    cwd: str,
) -> DecomposeOutcome:
    """Place and validate helpers while holding one pinned source lifecycle lease."""
    path = operation.path
    try:
        before_bytes = decomposition_provenance.read_source_bytes(operation)
    except OSError as exc:
        return DecomposeOutcome(ok=False, reason=f"unreadable target file: {exc}")
    try:
        before_text = before_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return DecomposeOutcome(ok=False, reason=f"target file is not valid UTF-8: {exc}")
    offset = _target_insertion_offset(before_text, target_symbol)
    if offset is None:
        return DecomposeOutcome(
            ok=False, reason=f"target declaration {target_symbol} not found in file"
        )
    stubs = [str(s or "").strip() for s in skeletons if str(s or "").strip()]
    if not stubs:
        return DecomposeOutcome(ok=False, reason="no insertable helper skeletons")
    for stub in stubs:
        if not stub_shape_ok(stub):
            return DecomposeOutcome(
                ok=False,
                reason="stub-shape violation: stated stubs are `theorem/lemma … := by sorry`",
            )
    newline = _source_newline(before_text)
    normalized_stubs = [_normalize_stub_newlines(stub, newline) for stub in stubs]
    requested_names = [_helper_name(stub) for stub in normalized_stubs]
    if any(not name for name in requested_names) or len(set(requested_names)) != len(
        requested_names
    ):
        return DecomposeOutcome(
            ok=False,
            reason="helper skeleton names are missing or duplicated",
        )
    requested_name_set = set(requested_names)
    managed_dependencies = {
        name: tuple(
            dependency
            for raw_dependency in (helper_dependencies or {}).get(name, ())
            if (dependency := str(raw_dependency or "").strip()) in requested_name_set
            and dependency != name
        )
        for name in requested_names
    }
    existing_declarations = _existing_declarations_by_name(before_text)
    existing_names: list[str] = []
    source_stubs: list[str] = []
    graph_skeletons: dict[str, str] = {}
    for name, stub in zip(requested_names, normalized_stubs, strict=True):
        existing = existing_declarations.get(name, "")
        if existing:
            if _statement_core(existing) != _statement_core(stub):
                return DecomposeOutcome(
                    ok=False,
                    reason=(
                        f"existing declaration {name} has a different statement; "
                        "refusing duplicate helper insertion"
                    ),
                )
            existing_names.append(name)
            graph_skeletons[name] = existing
            continue
        source_stubs.append(stub)
        graph_skeletons[name] = stub
    if not source_stubs:
        try:
            graph_helpers = _record_split_in_graph(
                target_symbol=target_symbol,
                active_file=str(path),
                placed=requested_names,
                skeletons=graph_skeletons,
                helper_dependencies=managed_dependencies,
            )
        except Exception as exc:
            return DecomposeOutcome(
                ok=False,
                reason=f"existing helper graph reconciliation failed: {exc}",
            )
        if plan_state.plan_state_enabled() and set(graph_helpers) != set(requested_names):
            return DecomposeOutcome(
                ok=False,
                reason="dependency graph did not retain every existing helper",
            )
        return DecomposeOutcome(
            ok=True,
            reason="exact helper declarations already present; reinsertion skipped",
            placed=tuple(requested_names),
            skipped=tuple(existing_names),
            file=str(path),
        )
    block = (newline * 2).join(source_stubs) + (newline * 2)
    after_text = before_text[:offset] + block + before_text[offset:]
    after_bytes = after_text.encode("utf-8")
    forbidden = _introduced_forbidden_axioms(before_text, after_text, allowed_axioms)
    if forbidden:
        return DecomposeOutcome(
            ok=False, reason=f"forbidden axiom(s) introduced: {', '.join(forbidden)}"
        )
    try:
        provenance = decomposition_provenance.begin_decomposition(
            active_file=str(path),
            target_symbol=target_symbol,
            skeletons=source_stubs,
            before_text=before_text,
            after_text=after_text,
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            helper_dependencies=managed_dependencies,
            cwd=cwd,
            operation=operation,
        )
    except Exception as exc:
        return DecomposeOutcome(
            ok=False,
            reason=f"could not persist exact decomposition provenance: {exc}",
        )
    transaction_id = str(provenance.get("transaction_id", "") or "")

    def finish_transaction(*, state: str, reason: str = "") -> tuple[bool, str]:
        """Persist one terminal ledger state without losing a required pause."""
        try:
            transitioned = decomposition_provenance.finish_decomposition(
                transaction_id,
                state=state,
                reason=reason,
            )
        except Exception as exc:
            logger.exception("decomposition provenance terminal write failed")
            return False, f"could not persist {state} decomposition provenance: {exc}"
        if transaction_id and not transitioned:
            return False, f"decomposition provenance refused the {state} transition"
        return True, ""

    try:
        inserted = decomposition_provenance.compare_and_swap_source(
            path,
            expected_bytes=before_bytes,
            replacement_bytes=after_bytes,
            operation=operation,
        )
    except OSError as exc:
        _transitioned, transition_error = finish_transaction(
            state="quarantined",
            reason=f"source compare-and-swap failed: {exc}",
        )
        return DecomposeOutcome(
            ok=False,
            reason=(
                f"source write failed or became ambiguous: {exc}"
                + (f"; {transition_error}" if transition_error else "")
            ),
            requires_pause=True,
        )
    if not inserted:
        transitioned, transition_error = finish_transaction(
            state="reverted",
            reason="source changed concurrently before helper insertion",
        )
        if not transitioned:
            return DecomposeOutcome(
                ok=False,
                reason=(
                    "source changed concurrently before helper insertion; write refused; "
                    f"{transition_error}"
                ),
                requires_pause=True,
            )
        return DecomposeOutcome(
            ok=False,
            reason="source changed concurrently before helper insertion; write refused",
        )

    from leanflow_cli.lean.lean_incremental import lean_incremental_check

    placed: list[str] = []
    names = [name for stub in source_stubs if (name := _helper_name(stub))]

    def reject_and_rollback(reason: str) -> DecomposeOutcome:
        """Rollback only the exact inserted revision, preserving concurrent edits."""
        try:
            reverted = decomposition_provenance.compare_and_swap_source(
                path,
                expected_bytes=after_bytes,
                replacement_bytes=before_bytes,
                operation=operation,
            )
        except OSError as exc:
            _transitioned, transition_error = finish_transaction(
                state="quarantined",
                reason=f"{reason}; safe rollback failed: {exc}",
            )
            return DecomposeOutcome(
                ok=False,
                reason=(
                    f"{reason}; safe rollback failed and transaction was quarantined: {exc}"
                    + (f"; {transition_error}" if transition_error else "")
                ),
                requires_pause=True,
            )
        if not reverted:
            _transitioned, transition_error = finish_transaction(
                state="quarantined",
                reason=f"{reason}; source changed concurrently before rollback",
            )
            return DecomposeOutcome(
                ok=False,
                reason=(
                    f"{reason}; source changed concurrently, so rollback was safely refused"
                    + (f"; {transition_error}" if transition_error else "")
                ),
                requires_pause=True,
            )
        transitioned, transition_error = finish_transaction(
            state="reverted",
            reason=reason,
        )
        if not transitioned:
            return DecomposeOutcome(
                ok=False,
                reason=f"{reason}; source reverted but {transition_error}",
                requires_pause=True,
            )
        return DecomposeOutcome(ok=False, reason=f"{reason}; write reverted")

    try:
        raise_if_interrupted("helper placement interrupted before Lean validation")
    except CooperativeInterrupt:
        reject_and_rollback("helper placement interrupted before Lean validation")
        raise

    if len(names) != len(source_stubs):
        return reject_and_rollback(
            "could not resolve every placed helper's exact declaration name",
        )

    # LeanProbe's check_target builds the environment before its exact target,
    # elaborating every preceding segment. The inserted block is contiguous,
    # so checking its tail validates every earlier stub and the tail itself in
    # one pass. Per-stub checks redundantly rebuild longer prefixes and made a
    # four-stub planner batch take minutes on large files.
    validation_target = names[-1]
    try:
        check = lean_incremental_check(
            action="check_target",
            file_path=str(path),
            theorem_id=validation_target,
            cwd=cwd,
        )
        check = decomposition_provenance.canonical_source_fallback_for_incremental_failure(
            check,
            source=after_text,
            cwd=cwd,
        )
    except CooperativeInterrupt:
        reject_and_rollback("helper placement interrupted during Lean validation")
        raise
    except Exception as exc:
        return reject_and_rollback(
            f"in-place validation crashed: {exc}",
        )
    try:
        raise_if_interrupted("helper placement interrupted during Lean validation")
    except CooperativeInterrupt:
        reject_and_rollback("helper placement interrupted during Lean validation")
        raise
    # Sorry warnings are normal work-in-progress; hard errors reject.
    if not check.get("success", False) or check.get("has_errors"):
        detail = _validation_failure_detail(check)
        suffix = f": {detail}" if detail else ""
        return reject_and_rollback(
            f"placed helper batch ending at {validation_target} failed "
            f"in-place validation{suffix}",
        )
    placed.extend(names)
    try:
        source_unchanged = decomposition_provenance.compare_and_swap_source(
            path,
            expected_bytes=after_bytes,
            replacement_bytes=after_bytes,
            operation=operation,
        )
    except OSError as exc:
        _transitioned, transition_error = finish_transaction(
            state="quarantined",
            reason=f"could not confirm source after validation: {exc}",
        )
        return DecomposeOutcome(
            ok=False,
            reason=(
                f"could not confirm source after validation; transaction quarantined: {exc}"
                + (f"; {transition_error}" if transition_error else "")
            ),
            requires_pause=True,
        )
    if not source_unchanged:
        _transitioned, transition_error = finish_transaction(
            state="quarantined",
            reason="source changed concurrently during helper validation",
        )
        return DecomposeOutcome(
            ok=False,
            reason=(
                "source changed concurrently during helper validation; placement not committed"
                + (f"; {transition_error}" if transition_error else "")
            ),
            requires_pause=True,
        )
    try:
        graph_helpers = _record_split_in_graph(
            target_symbol=target_symbol,
            active_file=str(path),
            placed=requested_names,
            skeletons=graph_skeletons,
            helper_dependencies=managed_dependencies,
        )
    except Exception as exc:
        logger.debug("decomposer graph transaction failed", exc_info=True)
        return reject_and_rollback(f"dependency graph persistence failed: {exc}")
    if plan_state.plan_state_enabled() and set(graph_helpers) != set(requested_names):
        # A successful graph save returns every materialized helper. Missing
        # ownership would make later negation cleanup impossible to authorize.
        _transitioned, transition_error = finish_transaction(
            state="quarantined",
            reason="dependency graph did not retain every placed helper",
        )
        return DecomposeOutcome(
            ok=False,
            reason=(
                "dependency graph did not retain every placed helper"
                + (f"; {transition_error}" if transition_error else "")
            ),
            requires_pause=True,
        )
    transitioned, transition_error = finish_transaction(state="committed")
    if not transitioned:
        return DecomposeOutcome(
            ok=False,
            reason=(
                "validated helper source could not commit its exact provenance transaction: "
                f"{transition_error}"
            ),
            requires_pause=True,
        )
    return DecomposeOutcome(
        ok=True,
        placed=tuple(requested_names),
        skipped=tuple(existing_names),
        file=str(path),
    )


def _helper_name(skeleton: str) -> str:
    match = re.match(
        r"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+)?(?:theorem|lemma)\s+([A-Za-z_«][\w'.«»]*)",
        str(skeleton or "").strip(),
    )
    return match.group(1) if match else ""


def _validation_failure_detail(check: Mapping[str, Any]) -> str:
    """Return one bounded diagnostic for a rejected in-place helper batch."""
    code = str(check.get("error_code", "") or "").strip()
    message = str(check.get("error", "") or check.get("output", "") or "").strip()
    message = " ".join(message.split())[:600]
    if code and message:
        return f"{code}: {message}"
    return code or message


def _record_helper_entries_in_graph(
    *,
    target_symbol: str,
    active_file: str,
    entries: Sequence[Mapping[str, Any]],
    generated_by: str,
    evidence_helper_names: Sequence[str] = (),
    helper_dependencies: Mapping[str, Sequence[str]] | None = None,
) -> tuple[str, ...]:
    """Record explicit helper declarations and their graph relationship.

    The native runner is the sole graph writer.  This helper therefore loads
    and saves the dependency graph exactly once for a whole accepted edit,
    then journals only the mutations that were successfully persisted.
    Negation-route prover edits use one non-structural ``evidence`` edge;
    ordinary proof helpers use reciprocal ``split_of``/``depends_on`` edges.
    Existing nodes keep kernel-owned statuses such as ``proved``; a newly
    written proof candidate is only ``proving`` until the manager gate checks
    it, while a declaration that still contains ``sorry`` remains ``stated``.
    """
    if not plan_state.plan_state_enabled():
        return ()
    target = str(target_symbol or "").strip()
    file_path = str(active_file or "").strip()
    if not target or not file_path:
        return ()
    evidence_names = {
        str(name or "").strip() for name in evidence_helper_names if str(name or "").strip()
    }

    helpers: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for raw_entry in entries:
        entry = dict(raw_entry)
        kind = str(entry.get("kind", "") or "").strip().lower()
        name = str(entry.get("name", "") or "").strip()
        if (
            kind not in {"theorem", "lemma"}
            or not name
            or name.startswith("[anonymous ")
            or name in seen
            or name in {target, target.split(".")[-1]}
        ):
            continue
        seen.add(name)
        declaration = str(entry.get("text", "") or "")
        status = "stated" if bool(entry.get("has_sorry")) else "proving"
        # Keep the exact declaration for graph identity. Older revisions stored
        # only `_statement_core`; planner admission migrates those conservatively.
        helpers.append((name, kind, declaration.strip(), status))
    if not helpers:
        return ()

    bp = plan_state.load_blueprint()
    target_id = plan_state.node_id_for(target, file_path)
    changed = False
    created_helpers: list[tuple[str, str]] = []
    updated_helpers: list[tuple[str, str]] = []
    linked_helpers: list[tuple[str, str, bool]] = []
    linked_dependencies: list[tuple[str, str, str, str]] = []
    if bp.node_by_id(target_id) is None:
        bp = bp.replace_node(
            plan_state.GraphNode(
                id=target_id,
                name=target,
                file=file_path,
                status="proving",
                generated_by=generated_by,
            )
        )
        changed = True

    edges = list(bp.edges)
    for name, kind, statement, status in helpers:
        helper_id = plan_state.node_id_for(name, file_path)
        existing = bp.node_by_id(helper_id)
        if existing is None:
            bp = bp.replace_node(
                plan_state.GraphNode(
                    id=helper_id,
                    kind=kind,
                    name=name,
                    file=file_path,
                    statement=statement,
                    status=status,
                    generated_by=generated_by,
                )
            )
            created_helpers.append((helper_id, name))
            changed = True
        elif existing.status == "conjectured":
            # A planner may have forecast this exact helper before it reached
            # the file. Materializing it advances only to a non-kernel status,
            # while an exact decomposer insertion becomes the source owner
            # needed by authoritative false-helper cleanup.
            bp = bp.replace_node(
                replace(
                    existing,
                    kind=kind,
                    statement=statement or existing.statement,
                    status=status,
                    generated_by=(
                        "decomposer"
                        if generated_by == "decomposer"
                        else existing.generated_by or generated_by
                    ),
                )
            )
            changed = True
        elif generated_by == "decomposer" and existing.generated_by != "decomposer":
            # Exact pending source provenance owns this materialized helper.
            # Queue sync may have discovered the declaration after a crash but
            # before recovery restored its split edges; preserve kernel status
            # while making source ownership explicit for later false cleanup.
            bp = bp.replace_node(
                replace(
                    existing,
                    kind=kind,
                    statement=statement or existing.statement,
                    generated_by="decomposer",
                )
            )
            changed = True
        elif (
            existing.generated_by in _EDITABLE_DEPENDENCY_GENERATORS
            and statement
            and existing.statement.strip() != statement
        ):
            # A generated dependency stays provisional until its parent
            # verifies. Revising it invalidates the earlier kernel status, so
            # force the exact new declaration through the helper gate again.
            bp = bp.replace_node(
                replace(
                    existing,
                    kind=kind,
                    statement=statement,
                    source_sha256="",
                    status=status,
                )
            )
            updated_helpers.append((helper_id, name))
            changed = True

        helper_linked = False
        helper_is_evidence = name in evidence_names
        relationships = (
            ((helper_id, target_id, "evidence"),)
            if helper_is_evidence
            else (
                (helper_id, target_id, "split_of"),
                (target_id, helper_id, "depends_on"),
            )
        )
        for source, edge_target, edge_kind in relationships:
            if any(
                edge.source == source and edge.target == edge_target and edge.kind == edge_kind
                for edge in edges
            ):
                continue
            edges.append(plan_state.GraphEdge(source=source, target=edge_target, kind=edge_kind))
            helper_linked = True
            changed = True
        if helper_linked:
            linked_helpers.append((helper_id, name, helper_is_evidence))

    helper_names = {name for name, *_rest in helpers}
    for name, *_rest in helpers:
        helper_id = plan_state.node_id_for(name, file_path)
        for raw_dependency in (helper_dependencies or {}).get(name, ()):
            dependency = str(raw_dependency or "").strip()
            if not dependency or dependency == name or dependency not in helper_names:
                continue
            dependency_id = plan_state.node_id_for(dependency, file_path)
            edge = plan_state.GraphEdge(
                source=helper_id,
                target=dependency_id,
                kind="depends_on",
            )
            if edge in edges:
                continue
            edges.append(edge)
            linked_dependencies.append((helper_id, name, dependency_id, dependency))
            changed = True

    if not changed:
        return tuple(name for name, *_rest in helpers)
    bp = replace(bp, edges=tuple(edges))
    plan_state.save_blueprint(bp)
    for helper_id, name in created_helpers:
        try:
            plan_state.append_journal_event(
                {
                    "event": "node-created",
                    "node_id": helper_id,
                    "name": name,
                    "via": generated_by,
                }
            )
        except Exception:
            logger.debug("decomposer node journal write failed", exc_info=True)
    for helper_id, name in updated_helpers:
        try:
            plan_state.append_journal_event(
                {
                    "event": "generated-helper-revised",
                    "node_id": helper_id,
                    "name": name,
                    "target": target,
                    "via": generated_by,
                    "status": next(
                        helper_status
                        for helper_name, _kind, _statement, helper_status in helpers
                        if helper_name == name
                    ),
                }
            )
        except Exception:
            logger.debug("decomposer revised-node journal write failed", exc_info=True)
    for helper_id, name, helper_is_evidence in linked_helpers:
        try:
            plan_state.append_journal_event(
                {
                    "event": (
                        "helper-evidence-recorded"
                        if helper_is_evidence
                        else "helper-split-recorded"
                    ),
                    "node_id": helper_id,
                    "name": name,
                    "target": target,
                    "via": generated_by,
                    "relationship": "evidence" if helper_is_evidence else "decomposition",
                }
            )
        except Exception:
            logger.debug("decomposer edge journal write failed", exc_info=True)
    for helper_id, name, dependency_id, dependency in linked_dependencies:
        try:
            plan_state.append_journal_event(
                {
                    "event": "helper-dependency-recorded",
                    "node_id": helper_id,
                    "name": name,
                    "dependency_node_id": dependency_id,
                    "dependency": dependency,
                    "target": target,
                    "via": generated_by,
                }
            )
        except Exception:
            logger.debug("decomposer dependency journal write failed", exc_info=True)
    return tuple(name for name, *_rest in helpers)


def _target_proof_dependency_names(
    content: str,
    *,
    target_symbol: str,
    helper_names: Sequence[str],
) -> set[str] | None:
    """Return helpers referenced as exact identifiers in one target proof body.

    ``None`` means the target declaration or assignment body was ambiguous;
    callers must preserve structural proof support in that case. Comments and
    strings are removed before tokenization, so prose cannot promote evidence.
    """
    target = str(target_symbol or "").strip()
    requested = {str(name or "").strip() for name in helper_names if str(name or "").strip()}
    if not target or not requested:
        return set()
    target_aliases = {target, target.split(".")[-1]}
    matches = [
        entry
        for entry in _declaration_line_index_from_text(content)
        if str(entry.get("name", "") or "").strip() in target_aliases
    ]
    if len(matches) != 1:
        return None
    declaration = str(matches[0].get("text", "") or "")
    marker = _find_assignment_marker_for_statement(declaration)
    if marker < 0:
        return None
    proof_body = _strip_lean_comments_and_strings(declaration[marker + 2 :])
    identifiers = set(_LEAN_IDENTIFIER_RE.findall(proof_body))
    referenced = requested.intersection(identifiers)
    helpers_by_short_name: dict[str, set[str]] = {}
    for helper_name in requested:
        helpers_by_short_name.setdefault(helper_name.split(".")[-1], set()).add(helper_name)
    # Lean permits an unqualified reference inside the declaration namespace.
    # Accept that spelling only when the candidate set resolves it uniquely;
    # two namespaced helpers with the same final component fail closed rather
    # than both being promoted by one ambiguous token.
    for identifier in identifiers:
        if "." in identifier:
            continue
        candidates = helpers_by_short_name.get(identifier, set())
        if len(candidates) == 1:
            referenced.update(candidates)
    return referenced


def prover_edit_evidence_helper_names(
    *,
    target_symbol: str,
    active_file: str,
    helper_names: Sequence[str],
    assigned_changed: bool,
) -> tuple[str, ...]:
    """Classify spontaneous helpers without exact target use as evidence.

    An unchanged assigned declaration cannot depend on a newly introduced
    helper. When it changed, only helpers absent from its sanitized exact proof
    identifiers remain evidence. Read or parser ambiguity cannot establish an
    exact dependency and therefore fails closed as evidence, never as campaign
    progress.
    """
    helpers = tuple(
        dict.fromkeys(str(name or "").strip() for name in helper_names if str(name or "").strip())
    )
    if not helpers:
        return ()
    if not assigned_changed:
        return helpers
    try:
        content = Path(active_file).read_text(encoding="utf-8")
    except OSError:
        return helpers
    referenced = _target_proof_dependency_names(
        content,
        target_symbol=target_symbol,
        helper_names=helpers,
    )
    if referenced is None:
        return helpers
    return tuple(name for name in helpers if name not in referenced)


def negation_evidence_helper_names(
    *,
    target_symbol: str,
    active_file: str,
    helper_names: Sequence[str],
    assigned_changed: bool,
) -> tuple[str, ...]:
    """Return the generalized prover-edit evidence classification.

    Keep this compatibility name for callers from the earlier negate-only
    policy. Route identity no longer changes the result.
    """
    return prover_edit_evidence_helper_names(
        target_symbol=target_symbol,
        active_file=active_file,
        helper_names=helper_names,
        assigned_changed=assigned_changed,
    )


def _promote_integrated_evidence_helpers(
    *,
    target_symbol: str,
    active_file: str,
    content: str,
) -> tuple[str, ...]:
    """Promote exact target-used evidence nodes to structural proof support."""
    if not plan_state.plan_state_enabled():
        return ()
    blueprint = plan_state.load_blueprint()
    target_id = plan_state.node_id_for(target_symbol, active_file)
    evidence_nodes = [
        blueprint.node_by_id(edge.source)
        for edge in blueprint.edges
        if edge.kind == "evidence" and edge.target == target_id
    ]
    evidence_helpers = [
        node.name
        for node in evidence_nodes
        if node is not None
        and node.file
        and _canonical_graph_file(node.file) == _canonical_graph_file(active_file)
    ]
    referenced = _target_proof_dependency_names(
        content,
        target_symbol=target_symbol,
        helper_names=evidence_helpers,
    )
    if not referenced:
        return ()
    edges = list(blueprint.edges)
    promoted: list[tuple[str, str]] = []
    for helper_name in sorted(referenced):
        helper_id = plan_state.node_id_for(helper_name, active_file)
        evidence = plan_state.GraphEdge(source=helper_id, target=target_id, kind="evidence")
        if edges.count(evidence) != 1:
            continue
        edges.remove(evidence)
        for structural in (
            plan_state.GraphEdge(source=helper_id, target=target_id, kind="split_of"),
            plan_state.GraphEdge(source=target_id, target=helper_id, kind="depends_on"),
        ):
            if structural not in edges:
                edges.append(structural)
        promoted.append((helper_id, helper_name))
    if not promoted:
        return ()
    plan_state.save_blueprint(replace(blueprint, edges=tuple(edges)))
    for helper_id, helper_name in promoted:
        plan_state.append_journal_event(
            {
                "event": "helper-evidence-promoted-to-proof-support",
                "node_id": helper_id,
                "name": helper_name,
                "target_node_id": target_id,
                "target": target_symbol,
                "file": active_file,
                "from": "evidence",
                "to": ["split_of", "depends_on"],
                "via": "exact-target-proof-reference",
            }
        )
    return tuple(name for _node_id, name in promoted)


def record_prover_helpers_from_edit(
    *,
    target_symbol: str,
    active_file: str,
    before_text: str,
    assigned_changed: bool = False,
) -> ProverHelperGraphUpdate:
    """Record generated helper declarations changed by one accepted prover edit.

    Historical declarations remain excluded unless the graph already records
    them as generated dependencies of the current target. Every spontaneous
    helper remains non-structural evidence until the target proof body
    references its exact Lean identifier. Route names and helper names cannot
    grant proof-progress authority.
    """
    if not plan_state.plan_state_enabled():
        return ProverHelperGraphUpdate()
    try:
        after_text = Path(active_file).read_text(encoding="utf-8")
    except OSError:
        return ProverHelperGraphUpdate()
    before_entries = _declaration_line_index_from_text(before_text)
    before_by_name = {
        str(entry.get("name", "") or "").strip(): entry
        for entry in before_entries
        if str(entry.get("name", "") or "").strip()
    }
    before_names = set(before_by_name)
    after_entries = _declaration_line_index_from_text(after_text)
    introduced = [
        entry
        for entry in after_entries
        if str(entry.get("name", "") or "").strip() not in before_names
    ]
    editable_dependencies = editable_dependency_helper_names(
        target_symbol=target_symbol,
        active_file=active_file,
    )
    updated = [
        entry
        for entry in after_entries
        if (name := str(entry.get("name", "") or "").strip()) in editable_dependencies
        and name in before_by_name
        and str(entry.get("text", "") or "").strip()
        != str(before_by_name[name].get("text", "") or "").strip()
    ]
    introduced_names = tuple(
        str(entry.get("name", "") or "").strip()
        for entry in introduced
        if str(entry.get("name", "") or "").strip()
    )
    evidence_names = prover_edit_evidence_helper_names(
        target_symbol=target_symbol,
        active_file=active_file,
        helper_names=introduced_names,
        assigned_changed=assigned_changed,
    )
    recorded = _record_helper_entries_in_graph(
        target_symbol=target_symbol,
        active_file=active_file,
        entries=(*introduced, *updated),
        generated_by="prover-edit",
        evidence_helper_names=evidence_names,
    )
    promoted = (
        _promote_integrated_evidence_helpers(
            target_symbol=target_symbol,
            active_file=active_file,
            content=after_text,
        )
        if assigned_changed
        else ()
    )
    evidence = tuple(name for name in recorded if name in set(evidence_names))
    introduced_set = set(introduced_names)
    updated_names = {
        str(entry.get("name", "") or "").strip()
        for entry in updated
        if str(entry.get("name", "") or "").strip()
    }
    return ProverHelperGraphUpdate(
        introduced=tuple(name for name in recorded if name in introduced_set),
        updated=tuple(name for name in recorded if name in updated_names),
        evidence=evidence,
        proof_support=tuple(name for name in recorded if name not in set(evidence_names)),
        promoted=promoted,
    )


def removable_prover_helper(target_symbol: str, active_file: str) -> bool:
    """Return whether the assigned node is optional prover-generated evidence.

    Only spontaneous prover-edit nodes with evidence-only graph edges qualify.
    Source declarations, proved facts, decomposer obligations, and structural
    dependencies must use their stronger statement-repair/negation protocols.
    """
    if not plan_state.plan_state_enabled():
        return False
    try:
        blueprint = plan_state.load_blueprint()
    except Exception:
        return False
    node = blueprint.node_by_id(plan_state.node_id_for(target_symbol, active_file))
    if (
        node is None
        or node.generated_by not in {"prover-edit", "prover-edit-backfill"}
        or node.status in {"proved", "false"}
    ):
        return False
    touching = [
        edge for edge in blueprint.edges if edge.source == node.id or edge.target == node.id
    ]
    return all(edge.kind == "evidence" for edge in touching)


def retire_removed_prover_helper(target_symbol: str, active_file: str) -> bool:
    """Park one safely removed optional helper while preserving its dead-branch record."""
    if not removable_prover_helper(target_symbol, active_file):
        return False
    blueprint = plan_state.load_blueprint()
    node_id = plan_state.node_id_for(target_symbol, active_file)
    node = blueprint.node_by_id(node_id)
    if node is None:
        return False
    note = "retired: unused prover-generated helper removed from source"
    notes = "; ".join(part for part in (str(node.notes or "").strip(), note) if part)
    plan_state.save_blueprint(
        blueprint.replace_node(replace(node, status="parked", owner="", notes=notes))
    )
    plan_state.append_journal_event(
        {
            "event": "prover-helper-retired",
            "node_id": node_id,
            "name": target_symbol,
            "file": active_file,
            "from": node.status,
            "to": "parked",
            "why": "unused optional prover-generated helper removed from source",
        }
    )
    return True


def _canonical_graph_file(value: Any) -> str:
    """Return a stable exact-scope key for one journal or graph file."""
    text = str(value or "").strip()
    if not text:
        return ""
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(text))))


def editable_dependency_helper_names(
    *,
    target_symbol: str,
    active_file: str,
) -> frozenset[str]:
    """Return generated same-file dependencies that may evolve with a target.

    Fail closed when graph state is absent or unreadable. Original queue
    declarations and model-created nodes outside the target's transitive
    dependency closure remain immutable.
    """
    if not plan_state.plan_state_enabled():
        return frozenset()
    target = str(target_symbol or "").strip()
    file_path = str(active_file or "").strip()
    canonical_file = _canonical_graph_file(file_path)
    if not target or not canonical_file:
        return frozenset()
    try:
        blueprint = plan_state.load_blueprint()
    except Exception:
        logger.debug("editable dependency graph read failed", exc_info=True)
        return frozenset()
    target_id = plan_state.node_id_for(target, file_path)
    if blueprint.node_by_id(target_id) is None:
        target_aliases = {target, target.split(".")[-1]}
        candidates = [
            node
            for node in blueprint.nodes
            if node.name in target_aliases and _canonical_graph_file(node.file) == canonical_file
        ]
        if len(candidates) != 1:
            return frozenset()
        target_id = candidates[0].id

    dependencies: dict[str, list[str]] = {}
    for edge in blueprint.edges:
        if edge.kind == "depends_on":
            dependencies.setdefault(edge.source, []).append(edge.target)
    by_id = {node.id: node for node in blueprint.nodes}
    editable: set[str] = set()
    pending = list(dependencies.get(target_id, ()))
    visited: set[str] = set()
    while pending:
        node_id = pending.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        pending.extend(dependencies.get(node_id, ()))
        node = by_id.get(node_id)
        if (
            node is not None
            and node.id != target_id
            and node.kind in {"theorem", "lemma"}
            and node.generated_by in _EDITABLE_DEPENDENCY_GENERATORS
            and _canonical_graph_file(node.file) == canonical_file
            and node.name
        ):
            editable.add(node.name)
    return frozenset(editable)


def _prover_edit_evidence_migration_complete() -> bool:
    """Return whether this plan-state root completed the one-time migration."""
    summary = plan_state.load_summary()
    migrations = summary.get("migrations")
    if not isinstance(migrations, Mapping):
        return False
    record = migrations.get(_PROVER_EDIT_EVIDENCE_EDGE_MIGRATION)
    return isinstance(record, Mapping) and bool(record.get("complete"))


def _reconcile_migrated_evidence_campaign(
    raw_campaign: Mapping[str, Any],
    *,
    migrated_node_ids: set[str],
    nodes_by_id: Mapping[str, plan_state.GraphNode],
    route_streak_floor: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove false proof-progress history for migrated evidence nodes.

    The graph relationship and campaign mechanism ledger form one accounting
    invariant. Reclassifying a helper edge without removing its old mechanism
    record would let an epoch handoff continue treating an obstruction as
    proof progress. Preserve unrelated entries and reconstruct a false latest
    reset from the campaign epoch's durable route/activity floor.
    """
    campaign = dict(raw_campaign)
    removed_entries: list[str] = []
    repaired_entries: list[str] = []
    raw_ledger = campaign.get("verified_mechanisms")
    if isinstance(raw_ledger, Mapping):
        ledger = dict(raw_ledger)
        raw_entries = ledger.get("entries")
        if isinstance(raw_entries, Mapping):
            entries = dict(raw_entries)
            for raw_key, raw_record in tuple(entries.items()):
                if not isinstance(raw_record, Mapping):
                    continue
                key = str(raw_key)
                record = dict(raw_record)
                seen_node_ids = list(
                    dict.fromkeys(
                        str(value or "").strip()
                        for value in (record.get("seen_node_ids") or [])
                        if str(value or "").strip()
                    )
                )
                for identity_field in ("first_node_id", "last_node_id"):
                    identity = str(record.get(identity_field, "") or "").strip()
                    if identity and identity not in seen_node_ids:
                        seen_node_ids.append(identity)
                if not migrated_node_ids.intersection(seen_node_ids):
                    continue
                retained = [
                    node_id for node_id in seen_node_ids if node_id not in migrated_node_ids
                ]
                if not retained:
                    entries.pop(raw_key, None)
                    removed_entries.append(key)
                    continue
                first_node_id = retained[0]
                last_node_id = retained[-1]
                record["seen_node_ids"] = retained
                record["seen_count"] = len(retained)
                record["first_node_id"] = first_node_id
                record["last_node_id"] = last_node_id
                first_node = nodes_by_id.get(first_node_id)
                last_node = nodes_by_id.get(last_node_id)
                if first_node is not None:
                    record["first_node_name"] = first_node.name
                    record["first_node_file"] = first_node.file
                if last_node is not None:
                    record["last_node_name"] = last_node.name
                    if "last_node_file" in record:
                        record["last_node_file"] = last_node.file
                entries[raw_key] = record
                repaired_entries.append(key)
            if entries:
                ledger["entries"] = entries
                campaign["verified_mechanisms"] = ledger
            else:
                campaign.pop("verified_mechanisms", None)

    last_progress_cleared = False
    last_progress_repaired = False
    previous_streak = _nonnegative_counter(campaign.get("no_progress_route_streak", 0))
    reconstructed_streak = previous_streak
    repaired_streak = previous_streak
    route_limit = _positive_limit(
        campaign.get("no_progress_route_limit", campaign_epoch.ROUTE_EPOCH_LIMIT),
        campaign_epoch.ROUTE_EPOCH_LIMIT,
    )
    raw_last_progress = campaign.get("last_verified_graph_progress")
    if isinstance(raw_last_progress, Mapping):
        last_progress = dict(raw_last_progress)
        node_ids = list(
            dict.fromkeys(
                str(value or "").strip()
                for value in (last_progress.get("node_ids") or [])
                if str(value or "").strip()
            )
        )
        if migrated_node_ids.intersection(node_ids):
            retained = [node_id for node_id in node_ids if node_id not in migrated_node_ids]
            if retained:
                last_progress["node_ids"] = retained
                campaign["last_verified_graph_progress"] = last_progress
                last_progress_repaired = True
            else:
                campaign.pop("last_verified_graph_progress", None)
                last_progress_cleared = True
                reconstructed_streak = max(previous_streak, route_streak_floor)
                repaired_streak = min(reconstructed_streak, route_limit)
                campaign["no_progress_route_streak"] = repaired_streak

    reconciliation = {
        "version": 3,
        "migration": _PROVER_EDIT_EVIDENCE_EDGE_MIGRATION,
        "node_ids": sorted(migrated_node_ids),
        "removed_mechanism_entries": sorted(removed_entries),
        "repaired_mechanism_entries": sorted(repaired_entries),
        "last_verified_graph_progress_cleared": last_progress_cleared,
        "last_verified_graph_progress_repaired": last_progress_repaired,
        "previous_streak": previous_streak,
        "route_streak_floor": route_streak_floor,
        "reconstructed_streak": reconstructed_streak,
        "repaired_streak": repaired_streak,
        "route_limit": route_limit,
        "rollover_required": repaired_streak >= route_limit,
        "reconciled_at": _now_iso(),
    }
    campaign["prover_edit_evidence_accounting_reconciliation"] = reconciliation
    campaign["updated_at"] = reconciliation["reconciled_at"]
    return campaign, reconciliation


def _mark_prover_edit_evidence_migration_complete(
    migrated_count: int,
    *,
    accounting_node_ids: Sequence[str] = (),
    nodes_by_id: Mapping[str, plan_state.GraphNode] | None = None,
) -> dict[str, Any]:
    """Atomically mark migration complete and repair campaign accounting."""
    evidence_node_ids = {
        str(node_id or "").strip() for node_id in accounting_node_ids if str(node_id or "").strip()
    }
    summary_before = plan_state.load_summary()
    campaign_before = summary_before.get("campaign")
    route_streak_floor = (
        campaign_epoch.progress_route_streak_floor(campaign_before, evidence_node_ids)
        if evidence_node_ids and isinstance(campaign_before, Mapping)
        else 0
    )

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        raw_migrations = summary.get("migrations")
        migrations = dict(raw_migrations) if isinstance(raw_migrations, Mapping) else {}
        reconciliation: dict[str, Any] = {}
        raw_campaign = summary.get("campaign")
        if evidence_node_ids and isinstance(raw_campaign, Mapping):
            campaign, reconciliation = _reconcile_migrated_evidence_campaign(
                raw_campaign,
                migrated_node_ids=evidence_node_ids,
                nodes_by_id=nodes_by_id or {},
                route_streak_floor=route_streak_floor,
            )
            summary["campaign"] = campaign
        migrations[_PROVER_EDIT_EVIDENCE_EDGE_MIGRATION] = {
            "complete": True,
            "migrated_count": max(0, int(migrated_count)),
            "accounting_reconciled_count": len(evidence_node_ids),
        }
        summary["migrations"] = migrations
        summary["version"] = 1
        summary["updated_at"] = _now_iso()
        return reconciliation

    return dict(update_json_file(plan_state.plan_state_paths().summary_json, mutate) or {})


def _legacy_prover_edit_helper_candidates(
    blueprint: plan_state.Blueprint,
) -> tuple[dict[str, str], ...]:
    """Return event-proven spontaneous helpers without exact target use.

    Journal order is the authority. A helper qualifies only when its exact
    ``helper-split-recorded`` event says ``via=prover-edit``. Route labels and
    name resemblance are irrelevant; an exact current target proof identifier
    is the only fact that preserves structural proof support. Managed
    decomposer-owned nodes remain structural even if a malformed legacy event
    claims prover provenance.
    """
    journal = plan_state.plan_state_paths().journal_jsonl
    try:
        handle = journal.open("r", encoding="utf-8")
    except OSError:
        return ()
    nodes_by_id = {node.id: node for node in blueprint.nodes}
    candidates: dict[str, dict[str, str]] = {}
    source_cache: dict[str, str | None] = {}
    with handle:
        for line in handle:
            try:
                raw_event = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(raw_event, Mapping):
                continue
            event = dict(raw_event)
            event_kind = str(event.get("event", "") or "")
            if (
                event_kind != "helper-split-recorded"
                or str(event.get("via", "") or "").strip() != "prover-edit"
            ):
                continue
            helper_id = str(event.get("node_id", "") or "").strip()
            helper_name = str(event.get("name", "") or "").strip()
            target = str(event.get("target", "") or "").strip()
            helper = nodes_by_id.get(helper_id)
            if (
                helper is None
                or not helper_name
                or helper.name != helper_name
                or helper.generated_by == "decomposer"
                or not target
                or not helper.file
            ):
                continue
            file = _canonical_graph_file(helper.file)
            target_id = plan_state.node_id_for(target, helper.file)
            target_node = nodes_by_id.get(target_id)
            if (
                target_node is None
                or target_node.name != target
                or _canonical_graph_file(target_node.file) != file
            ):
                continue
            if file not in source_cache:
                try:
                    source_cache[file] = Path(helper.file).read_text(encoding="utf-8")
                except OSError:
                    source_cache[file] = None
            source = source_cache[file]
            if source is None:
                continue
            referenced = _target_proof_dependency_names(
                source,
                target_symbol=target,
                helper_names=(helper_name,),
            )
            if referenced is not None and helper_name in referenced:
                continue
            candidates[helper_id] = {
                "helper_id": helper_id,
                "helper_name": helper_name,
                "target_id": target_id,
                "target": target,
                "file": helper.file,
                "helper_event_ts": str(event.get("ts", "") or ""),
            }
    return tuple(candidates.values())


def migrate_legacy_prover_helper_edges() -> tuple[str, ...]:
    """Reclassify pre-fix unused prover-edit helper splits as evidence.

    Convert only a uniquely present reciprocal ``helper split_of target`` and
    ``target depends_on helper`` pair supported by durable ordered journal
    provenance. Nodes, statuses, declarations, and unrelated edges remain
    untouched. Matching false mechanism-ledger and last-progress records are
    removed atomically with the marker, and a false latest progress reset
    restores the campaign epoch's durable route-streak floor. The summary
    marker makes the migration one-time per plan-state root; graph shape makes
    it independently idempotent if marker persistence is interrupted.

    Return every helper whose graph or accounting was reconciled. This lets a
    live runner immediately rehydrate the repaired campaign counters, including
    when an earlier migration already changed the edge shape.
    """
    if not plan_state.plan_state_enabled():
        return ()
    paths = plan_state.plan_state_paths()
    if not paths.blueprint_json.is_file() or _prover_edit_evidence_migration_complete():
        return ()
    blueprint = plan_state.load_blueprint()
    edges = list(blueprint.edges)
    migrated: list[dict[str, str]] = []
    accounting_candidates: list[dict[str, str]] = []
    for candidate in _legacy_prover_edit_helper_candidates(blueprint):
        helper_id = candidate["helper_id"]
        target_id = candidate["target_id"]
        split_indexes = [
            index
            for index, edge in enumerate(edges)
            if edge.source == helper_id and edge.target == target_id and edge.kind == "split_of"
        ]
        dependency_indexes = [
            index
            for index, edge in enumerate(edges)
            if edge.source == target_id and edge.target == helper_id and edge.kind == "depends_on"
        ]
        evidence_indexes = [
            index
            for index, edge in enumerate(edges)
            if edge.source == helper_id and edge.target == target_id and edge.kind == "evidence"
        ]
        if len(split_indexes) != 1 or len(dependency_indexes) != 1:
            # An earlier route-specific migration may already have converted the graph while
            # leaving stale mechanism/progress history. Repair that exact
            # event-proven evidence shape when the generalized marker is first written.
            if not split_indexes and not dependency_indexes and len(evidence_indexes) == 1:
                accounting_candidates.append(candidate)
            continue
        edges = [
            edge
            for edge in edges
            if not (
                (edge.source == helper_id and edge.target == target_id and edge.kind == "split_of")
                or (
                    edge.source == target_id
                    and edge.target == helper_id
                    and edge.kind == "depends_on"
                )
            )
        ]
        evidence = plan_state.GraphEdge(source=helper_id, target=target_id, kind="evidence")
        if evidence not in edges:
            edges.append(evidence)
        migrated.append(candidate)
        accounting_candidates.append(candidate)

    if migrated:
        plan_state.save_blueprint(replace(blueprint, edges=tuple(edges)))
        for candidate in migrated:
            plan_state.append_journal_event(
                {
                    "event": "prover-helper-evidence-migrated",
                    "migration": _PROVER_EDIT_EVIDENCE_EDGE_MIGRATION,
                    "node_id": candidate["helper_id"],
                    "name": candidate["helper_name"],
                    "target_node_id": candidate["target_id"],
                    "target": candidate["target"],
                    "file": candidate["file"],
                    "from": ["split_of", "depends_on"],
                    "to": "evidence",
                    "helper_event_ts": candidate["helper_event_ts"],
                }
            )
    reconciliation = _mark_prover_edit_evidence_migration_complete(
        len(migrated),
        accounting_node_ids=[candidate["helper_id"] for candidate in accounting_candidates],
        nodes_by_id={node.id: node for node in blueprint.nodes},
    )
    if reconciliation:
        plan_state.append_journal_event(
            {
                "event": "prover-helper-accounting-reconciled",
                **reconciliation,
            }
        )
    return tuple(candidate["helper_name"] for candidate in accounting_candidates)


def migrate_legacy_negation_prover_helper_edges() -> tuple[str, ...]:
    """Run the generalized unused prover-helper migration.

    Preserve the earlier public name for integrations and old checkpoints that
    still resolve it dynamically.
    """
    return migrate_legacy_prover_helper_edges()


def backfill_known_prover_helpers(
    *,
    target_symbol: str,
    active_file: str,
    helper_names: Sequence[str],
) -> tuple[str, ...]:
    """Conservatively link an explicit list of pre-existing prover helpers.

    This is the recovery route for helpers written before automatic edit
    tracking existed.  It deliberately accepts names, not an inferred file
    sweep, so callers must identify the known live children and cannot attach
    unrelated historical declarations accidentally.
    """
    requested = {str(name or "").strip() for name in helper_names if str(name or "").strip()}
    if not requested or not plan_state.plan_state_enabled():
        return ()
    try:
        content = Path(active_file).read_text(encoding="utf-8")
    except OSError:
        return ()
    entries = [
        entry
        for entry in _declaration_line_index_from_text(content)
        if str(entry.get("name", "") or "").strip() in requested
    ]
    return _record_helper_entries_in_graph(
        target_symbol=target_symbol,
        active_file=active_file,
        entries=entries,
        generated_by="prover-edit-backfill",
    )


def _record_split_in_graph(
    *,
    target_symbol: str,
    active_file: str,
    placed: Sequence[str],
    skeletons: Mapping[str, str],
    helper_dependencies: Mapping[str, Sequence[str]] | None = None,
) -> tuple[str, ...]:
    """Stated helper nodes + split_of/depends_on edges (journaled)."""
    entries: list[dict[str, Any]] = []
    for name in placed:
        skeleton = str(skeletons.get(name, "") or "")
        parsed = _declaration_line_index_from_text(skeleton)
        if parsed:
            entries.append(parsed[0])
    return _record_helper_entries_in_graph(
        target_symbol=target_symbol,
        active_file=active_file,
        entries=entries,
        generated_by="decomposer",
        helper_dependencies=helper_dependencies,
    )


def _exact_graph_identity(
    blueprint: plan_state.Blueprint,
    *,
    node_id: str,
    name: str,
    active_file: str,
    role: str,
) -> tuple[plan_state.GraphNode | None, str]:
    """Resolve one graph identity, rejecting duplicates and reassignment."""
    id_indexes = [index for index, node in enumerate(blueprint.nodes) if node.id == node_id]
    identity_indexes = [
        index
        for index, node in enumerate(blueprint.nodes)
        if node.name == name and node.file == active_file
    ]
    if len(id_indexes) > 1 or len(identity_indexes) > 1:
        return None, f"dependency graph {role} identity is duplicated"
    if bool(id_indexes) != bool(identity_indexes):
        return None, f"dependency graph {role} identity is internally inconsistent"
    if id_indexes and id_indexes != identity_indexes:
        return None, f"dependency graph {role} id was reassigned to another declaration"
    if not id_indexes:
        return None, ""
    return blueprint.nodes[id_indexes[0]], ""


def rollback_decomposition_graph(
    *,
    target_symbol: str,
    active_file: str,
    helper_names: Sequence[str],
) -> GraphRollbackOutcome:
    """Retire one exact decomposer split after source rollback is proven.

    The caller remains responsible for proving that every helper is absent
    from source and the parent identity is intact (the exact pre-insertion
    revision is sufficient). This graph half then removes only uniquely
    identified, decomposer-owned helpers with non-kernel statuses and their
    reciprocal structural edges. Any competing identity, status, or incident
    edge fails closed without mutating the graph.
    """
    helpers = tuple(
        dict.fromkeys(str(name or "").strip() for name in helper_names if str(name or "").strip())
    )
    target = str(target_symbol or "").strip()
    file_path = str(active_file or "").strip()
    if not plan_state.plan_state_enabled():
        return GraphRollbackOutcome(ok=True, already_absent=helpers)
    if not target or not file_path or not helpers:
        return GraphRollbackOutcome(
            ok=False,
            reason="graph rollback requires an exact parent, file, and helper identity",
        )

    parent_id = plan_state.node_id_for(target, file_path)
    helper_ids = {name: plan_state.node_id_for(name, file_path) for name in helpers}
    if parent_id in helper_ids.values() or len(set(helper_ids.values())) != len(helper_ids):
        return GraphRollbackOutcome(
            ok=False,
            reason="graph rollback helper identities collide with another declaration",
        )

    blueprint = plan_state.load_blueprint()
    parent, parent_reason = _exact_graph_identity(
        blueprint,
        node_id=parent_id,
        name=target,
        active_file=file_path,
        role="parent",
    )
    if parent_reason:
        return GraphRollbackOutcome(ok=False, reason=parent_reason)

    removable_ids: set[str] = set()
    removed: list[str] = []
    already_absent: list[str] = []
    for name, helper_id in helper_ids.items():
        helper, helper_reason = _exact_graph_identity(
            blueprint,
            node_id=helper_id,
            name=name,
            active_file=file_path,
            role=f"helper {name!r}",
        )
        incident = [edge for edge in blueprint.edges if helper_id in {edge.source, edge.target}]
        if helper_reason:
            return GraphRollbackOutcome(ok=False, reason=helper_reason)
        if helper is None:
            if incident:
                return GraphRollbackOutcome(
                    ok=False,
                    reason=f"absent helper {name!r} retains incident dependency graph edges",
                )
            already_absent.append(name)
            continue
        if parent is None:
            return GraphRollbackOutcome(
                ok=False,
                reason="dependency graph parent is absent while rollback helpers remain",
            )
        if helper.generated_by != "decomposer":
            return GraphRollbackOutcome(
                ok=False,
                reason=f"helper {name!r} is not owned by the decomposer",
            )
        if helper.status in {"proved", "false"}:
            return GraphRollbackOutcome(
                ok=False,
                reason=f"helper {name!r} has protected kernel status {helper.status!r}",
            )
        for edge in incident:
            expected = (
                edge.kind == "split_of" and edge.source == helper_id and edge.target == parent_id
            ) or (
                edge.kind == "depends_on" and edge.source == parent_id and edge.target == helper_id
            )
            if not expected:
                return GraphRollbackOutcome(
                    ok=False,
                    reason=f"helper {name!r} has evidence or unrelated dependency graph edges",
                )
        removable_ids.add(helper_id)
        removed.append(name)

    nodes = tuple(node for node in blueprint.nodes if node.id not in removable_ids)
    edges = tuple(
        edge
        for edge in blueprint.edges
        if not any(
            (edge.kind == "split_of" and edge.source == helper_id and edge.target == parent_id)
            or (edge.kind == "depends_on" and edge.source == parent_id and edge.target == helper_id)
            for helper_id in removable_ids
        )
    )
    updated = replace(blueprint, nodes=nodes, edges=edges)
    if updated != blueprint:
        try:
            plan_state.save_blueprint(updated)
        except plan_state.PlanStateRevisionConflict:
            return GraphRollbackOutcome(
                ok=False,
                reason="dependency graph changed while decomposition rollback was being saved",
            )

    persisted = plan_state.load_blueprint()
    remaining_nodes = [node.id for node in persisted.nodes if node.id in helper_ids.values()]
    remaining_edges = [
        edge
        for edge in persisted.edges
        if any(helper_id in {edge.source, edge.target} for helper_id in helper_ids.values())
    ]
    if remaining_nodes or remaining_edges:
        return GraphRollbackOutcome(
            ok=False,
            reason="dependency graph still contains a rolled-back helper identity or edge",
        )
    for name in removed:
        try:
            plan_state.append_journal_event(
                {
                    "event": "decomposer-split-rolled-back",
                    "name": name,
                    "node_id": helper_ids[name],
                    "target": target,
                }
            )
        except Exception:
            logger.debug("decomposer rollback journal write failed", exc_info=True)
    return GraphRollbackOutcome(
        ok=True,
        removed=tuple(removed),
        already_absent=tuple(already_absent),
    )


def run_decomposer(
    *,
    target_symbol: str,
    active_file: str,
    statement: str = "",
    diagnostics: str = "",
    goals: str = "",
    failed_attempts_text: str = "",
    allowed_axioms: Sequence[str] = ("propext", "Classical.choice", "Quot.sound"),
    cwd: str = "",
    agent: Any = None,
    max_helpers: int = 4,
) -> DecomposeOutcome:
    """Propose → guard → place → validate → graph → refresh (never raises)."""
    statement = normalize_statement(statement)
    try:
        from tools.implementations.lean_experts import lean_decompose_helpers_tool

        raw = lean_decompose_helpers_tool(
            target_symbol,
            active_file,
            theorem_statement=statement,
            current_diagnostics=diagnostics,
            current_goals=goals,
            recent_failed_attempts=failed_attempts_text,
            cwd=cwd,
            max_helper_count=max_helpers,
        )
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except Exception as exc:
        logger.debug("decomposer backend failed", exc_info=True)
        return DecomposeOutcome(ok=False, reason=f"decomposition backend failed: {exc}")
    advisor_success = payload.get("success") is True
    advisor_status = str(payload.get("status", "") or "").strip()
    advisor_provider_called = payload.get("provider_called")
    if not isinstance(advisor_provider_called, bool):
        advisor_provider_called = None
    if not payload.get("success"):
        return DecomposeOutcome(
            ok=False,
            reason=str(payload.get("message", "") or "backend returned no helpers"),
            advisor_success=advisor_success,
            advisor_status=advisor_status,
            advisor_provider_called=advisor_provider_called,
        )
    obstacle_summary = str(payload.get("obstacle_summary", "") or "").strip()
    recommended_split = str(payload.get("recommended_split", "") or "").strip()
    first_concrete_next_edit = _bounded_first_concrete_next_edit(
        payload.get("first_concrete_next_edit", "")
    )
    helpers = [dict(h) for h in payload.get("helpers") or [] if isinstance(h, Mapping)]
    helpers.sort(key=lambda h: int(h.get("validation_order", 0) or 0))
    ready: list[dict[str, Any]] = []
    skipped: list[str] = []
    for helper in helpers:
        name = str(helper.get("name", "") or "")
        skeleton = str(helper.get("lean_skeleton", "") or "")
        if str(helper.get("check_status", "") or "") in {
            "rejected_instantiated_parent",
            "rejected_admission",
        }:
            skipped.append(name or "[instantiated-parent]")
            reported_guard = helper.get("admission_guard")
            if not isinstance(reported_guard, Mapping):
                reported_guard = {
                    "instantiated_parameters": helper.get("instantiated_parameters", []),
                }
            guard_fields = decomposer_admission.bounded_journal_fields(
                reported_guard,
            )
            event = (
                "decomposer-instantiated-parent-rejected"
                if guard_fields.get("reason_code") == "closed_literal_parent_instantiation"
                else "decomposer-admission-rejected"
            )
            plan_state.append_journal_event(
                {
                    "event": event,
                    "helper": name,
                    "target": target_symbol,
                    **guard_fields,
                }
            )
            continue
        if "ready_for_managed_placement" in helper:
            managed_ready = helper.get("ready_for_managed_placement") is True
        else:
            # Persisted pre-contract payloads used ready_to_insert for guarded stubs.
            managed_ready = helper.get("ready_to_insert") is True
        if not managed_ready or not skeleton:
            skipped.append(name or "[unnamed]")
            continue
        if not stub_shape_ok(skeleton):
            skipped.append(name or "[malformed]")
            continue
        admission = decomposer_admission.assess_helper_admission(statement, skeleton)
        if not admission.accepted:
            skipped.append(name or "[instantiated-parent]")
            event = (
                "decomposer-instantiated-parent-rejected"
                if admission.reason_code == "closed_literal_parent_instantiation"
                else "decomposer-admission-rejected"
            )
            plan_state.append_journal_event(
                {
                    "event": event,
                    "helper": name,
                    "target": target_symbol,
                    **admission.journal_fields(),
                }
            )
            continue
        if sorry_offloading_suspect(statement, skeleton):
            skipped.append(name or "[offloading]")
            plan_state.append_journal_event(
                {
                    "event": "decomposer-offloading-rejected",
                    "helper": name,
                    "target": target_symbol,
                }
            )
            continue
        if unsupported_novel_bound_suspect(statement, skeleton):
            skipped.append(name or "[unsupported-bound]")
            plan_state.append_journal_event(
                {
                    "event": "decomposer-unsupported-bound-rejected",
                    "helper": name,
                    "target": target_symbol,
                }
            )
            continue
        ready.append(helper)
    if not ready:
        return DecomposeOutcome(
            ok=False,
            reason="no ready, guarded helpers to insert",
            skipped=tuple(skipped),
            obstacle_summary=obstacle_summary,
            recommended_split=recommended_split,
            first_concrete_next_edit=first_concrete_next_edit,
            advisor_success=advisor_success,
            advisor_status=advisor_status,
            advisor_provider_called=advisor_provider_called,
        )
    outcome = place_helpers(
        active_file=active_file,
        target_symbol=target_symbol,
        skeletons=[str(h["lean_skeleton"]) for h in ready],
        allowed_axioms=allowed_axioms,
        helper_dependencies={
            str(helper.get("name", "") or ""): tuple(
                str(dependency or "") for dependency in (helper.get("dependencies") or [])
            )
            for helper in ready
        },
        cwd=cwd,
    )
    if not outcome.ok:
        return replace(
            outcome,
            skipped=tuple(skipped),
            obstacle_summary=obstacle_summary,
            recommended_split=recommended_split,
            first_concrete_next_edit=first_concrete_next_edit,
            advisor_success=advisor_success,
            advisor_status=advisor_status,
            advisor_provider_called=advisor_provider_called,
        )
    refresh_queue_edit_guard(agent)
    return replace(
        outcome,
        skipped=tuple(skipped),
        obstacle_summary=obstacle_summary,
        recommended_split=recommended_split,
        first_concrete_next_edit=first_concrete_next_edit,
        advisor_success=advisor_success,
        advisor_status=advisor_status,
        advisor_provider_called=advisor_provider_called,
    )
