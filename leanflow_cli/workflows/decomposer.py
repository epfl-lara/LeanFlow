"""Mechanical decomposer for the /prove redesign (Phase 4 §4.2).

Executes the orchestrator's ``decompose`` route between prover turns: asks
the existing helper-decomposition backend for ready-to-insert skeletons,
guards them (stub shape, forbidden-axiom scan, anti-sorry-offloading), writes
them into the target file immediately BEFORE the target declaration, verifies
each placed stub in place via LeanProbe, records the split in the dependency
graph, and journals every action. The next queue cycle picks the stubs up
naturally — they precede the target in file order, so the file-order selector
assigns them first.

Writes are direct ``Path.write_text`` — this is a runner-level actor acting
strictly between prover turns, not a prover tool call. Non-negotiable
invariants (roadmap §4.5/§4.11, audit hole-1): every write passes the SAME
forbidden-axiom scan as prover edits, a stated stub is
``theorem/lemma … := by sorry`` and nothing else, and any in-place validation
error reverts the whole write. After a successful write the caller must
refresh the prover's queue-edit guard caches (:func:`refresh_queue_edit_guard`)
or the guard will false-positive-restore the new stubs.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import _declaration_line_index_from_text
from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.queue_edit_guard import _introduced_forbidden_axioms

logger = logging.getLogger(__name__)

#: A stated stub is exactly a (possibly private) theorem/lemma whose body is
#: `by sorry` — nothing else may ride along in a decomposer write.
_STUB_SHAPE_RE = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+)?(?:theorem|lemma)\s+"
    r"[A-Za-z_«][^:=]*:.+?:=\s*by\s+sorry\s*$",
    re.DOTALL,
)

#: Similarity above which a child statement counts as absorbing the parent's
#: whole difficulty (anti-sorry-offloading, roadmap §4.11 — structural check,
#: because prompting alone demonstrably does not fix this).
_OFFLOADING_SIMILARITY = 0.92


@dataclass(frozen=True)
class DecomposeOutcome:
    ok: bool
    reason: str = ""
    placed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    file: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "placed": list(self.placed),
            "skipped": list(self.skipped),
            "file": self.file,
        }


#: Any Lean command keyword ANYWHERE (word-boundary, position-independent) —
#: a second declaration of any kind must not ride along inside a "single"
#: stub, whether on its own line or smuggled onto the same one.
_DECL_KEYWORD_RE = re.compile(
    r"\b(?:theorem|lemma|example|def|abbrev|axiom|instance|structure|class|inductive|opaque)\b"
)


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
    text = str(skeleton or "").strip()
    if not text or not _STUB_SHAPE_RE.match(text):
        return False
    try:
        from leanflow_cli.lean.lean_parsing import _strip_lean_comments_and_strings

        stripped = _strip_lean_comments_and_strings(text)
    except Exception:
        stripped = text
    if len(_DECL_KEYWORD_RE.findall(stripped)) != 1:
        return False
    if stripped.count(":=") != 1:
        return False
    if len(re.findall(r"\bsorry\b", stripped)) != 1:
        return False
    if stripped.split(":=", 1)[1].strip() != "by sorry":
        return False
    try:
        entries = _declaration_line_index_from_text(text)
    except Exception:
        return False
    if len(entries) != 1:
        return False
    return str(entries[0].get("kind", "") or "").strip().lower() in {"theorem", "lemma"}


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


def place_helpers(
    *,
    active_file: str,
    target_symbol: str,
    skeletons: Sequence[str],
    allowed_axioms: Sequence[str],
    cwd: str = "",
) -> DecomposeOutcome:
    """Write guarded helper stubs before the target and verify them in place.

    All-or-nothing: shape check and axiom scan run BEFORE the write; each
    placed stub must then elaborate via LeanProbe (sorry warnings fine,
    errors revert the entire write).
    """
    path = Path(active_file)
    try:
        before_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return DecomposeOutcome(ok=False, reason=f"unreadable target file: {exc}")
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
    block = "\n\n".join(stubs) + "\n\n"
    after_text = before_text[:offset] + block + before_text[offset:]
    forbidden = _introduced_forbidden_axioms(before_text, after_text, allowed_axioms)
    if forbidden:
        return DecomposeOutcome(
            ok=False, reason=f"forbidden axiom(s) introduced: {', '.join(forbidden)}"
        )
    path.write_text(after_text, encoding="utf-8")

    from leanflow_cli.lean.lean_incremental import lean_incremental_check

    placed: list[str] = []
    names = [_helper_name(stub) for stub in stubs]
    for name in names:
        if not name:
            continue
        try:
            check = lean_incremental_check(
                action="check_target", file_path=str(path), theorem_id=name, cwd=cwd
            )
        except Exception as exc:
            path.write_text(before_text, encoding="utf-8")
            return DecomposeOutcome(ok=False, reason=f"in-place validation crashed: {exc}")
        # Sorry warnings are normal work-in-progress; hard errors reject.
        if not check.get("success", False) or check.get("has_errors"):
            path.write_text(before_text, encoding="utf-8")
            return DecomposeOutcome(
                ok=False,
                reason=f"placed stub {name} failed in-place validation; write reverted",
            )
        placed.append(name)
    return DecomposeOutcome(ok=True, placed=tuple(placed), file=str(path))


def _helper_name(skeleton: str) -> str:
    match = re.match(
        r"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+)?(?:theorem|lemma)\s+([A-Za-z_«][\w'.«»]*)",
        str(skeleton or "").strip(),
    )
    return match.group(1) if match else ""


def _record_split_in_graph(
    *, target_symbol: str, active_file: str, placed: Sequence[str], skeletons: Mapping[str, str]
) -> None:
    """Stated helper nodes + split_of/depends_on edges (journaled)."""
    if not plan_state.plan_state_enabled():
        return
    bp = plan_state.load_blueprint()
    target_id = plan_state.node_id_for(target_symbol, active_file)
    if bp.node_by_id(target_id) is None:
        bp = bp.replace_node(
            plan_state.GraphNode(
                id=target_id,
                name=target_symbol,
                file=active_file,
                status="proving",
                generated_by="decomposer",
            )
        )
    edges = list(bp.edges)
    for name in placed:
        helper_id = plan_state.node_id_for(name, active_file)
        bp = bp.replace_node(
            plan_state.GraphNode(
                id=helper_id,
                kind="lemma",
                name=name,
                file=active_file,
                statement=_statement_core(skeletons.get(name, "")),
                status="stated",
                generated_by="decomposer",
            )
        )
        for source, target, kind in (
            (helper_id, target_id, "split_of"),
            (target_id, helper_id, "depends_on"),
        ):
            if not any(e.source == source and e.target == target and e.kind == kind for e in edges):
                edges.append(plan_state.GraphEdge(source=source, target=target, kind=kind))
        plan_state.append_journal_event(
            {"event": "node-created", "node_id": helper_id, "name": name, "via": "decomposer"}
        )
    bp = replace(bp, edges=tuple(edges))
    plan_state.save_blueprint(bp)


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
    if not payload.get("success"):
        return DecomposeOutcome(
            ok=False, reason=str(payload.get("message", "") or "backend returned no helpers")
        )
    helpers = [dict(h) for h in payload.get("helpers") or [] if isinstance(h, Mapping)]
    helpers.sort(key=lambda h: int(h.get("validation_order", 0) or 0))
    ready: list[dict[str, Any]] = []
    skipped: list[str] = []
    for helper in helpers:
        name = str(helper.get("name", "") or "")
        skeleton = str(helper.get("lean_skeleton", "") or "")
        if not helper.get("ready_to_insert") or not skeleton:
            skipped.append(name or "[unnamed]")
            continue
        if not stub_shape_ok(skeleton):
            skipped.append(name or "[malformed]")
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
        ready.append(helper)
    if not ready:
        return DecomposeOutcome(
            ok=False,
            reason="no ready, guarded helpers to insert",
            skipped=tuple(skipped),
        )
    outcome = place_helpers(
        active_file=active_file,
        target_symbol=target_symbol,
        skeletons=[str(h["lean_skeleton"]) for h in ready],
        allowed_axioms=allowed_axioms,
        cwd=cwd,
    )
    if not outcome.ok:
        return replace(outcome, skipped=tuple(skipped))
    skeleton_by_name = {
        _helper_name(str(h["lean_skeleton"])): str(h["lean_skeleton"]) for h in ready
    }
    try:
        _record_split_in_graph(
            target_symbol=target_symbol,
            active_file=active_file,
            placed=outcome.placed,
            skeletons=skeleton_by_name,
        )
    except Exception:
        logger.debug("decomposer graph update failed", exc_info=True)
    refresh_queue_edit_guard(agent)
    return replace(outcome, skipped=tuple(skipped))
