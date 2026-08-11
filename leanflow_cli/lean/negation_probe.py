"""Build and evaluate bounded Lean negation probes without modifying project files.

The Hilbert-loop feasibility engine's Lean-facing leaf: mechanically build
``¬P`` for a stuck declaration, run the `plausible` counterexample pre-probe
(hint only — passing samples close the goal like ``admit`` and are NEVER
proof), then try a cheap tactic ladder on the negation in LeanProbe scratch.
``negation_proved`` requires ok=true AND ``#print axioms`` inside the
standard set — and even then a scratch verdict is only routing evidence:
flipping a project node to ``false`` requires promotion through the
authoritative verification gate; the probe never writes a project file.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_declarations import declaration_region
from leanflow_cli.lean.lean_incremental import lean_scratch_check
from leanflow_cli.lean.lean_parsing import declaration_statement_text

STANDARD_AXIOMS = {"propext", "Quot.sound", "Classical.choice"}
MAX_ROUTE_REASON_CHARS = 1600

_MODIFIER_WORDS = (
    "private",
    "protected",
    "noncomputable",
    "unsafe",
    "partial",
    "scoped",
)
_DECL_KEYWORDS = ("theorem", "lemma", "example")

_OPENERS = {"(": ")", "{": "}", "[": "]", "⦃": "⦄", "⟨": "⟩"}
_CLOSERS = {v: k for k, v in _OPENERS.items()}


@dataclass(frozen=True)
class NegationGoal:
    name: str  # neg_<short name>
    original: str  # the source declaration signature (statement part)
    binders: str  # verbatim binder text ("" if none)
    result_type: str  # verbatim type text
    prop: str  # "∀ <binders>, <type>" (or just <type>)
    lean_code: str  # "theorem neg_x : ¬ (<prop>) := by\n  sorry"


def _scan(text: str, start: int) -> tuple[int, str] | None:
    """Yield-next-significant-char scanner: skips comments/strings/chars."""
    index = start
    length = len(text)
    while index < length:
        ch = text[index]
        if ch == "-" and text.startswith("--", index):
            newline = text.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue
        if ch == "/" and text.startswith("/-", index):
            depth = 1
            index += 2
            while index < length and depth:
                if text.startswith("/-", index):
                    depth += 1
                    index += 2
                elif text.startswith("-/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            continue
        if ch == '"':
            index += 1
            while index < length:
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == '"':
                    index += 1
                    break
                index += 1
            continue
        return index, ch
    return None


def _split_signature(statement: str) -> tuple[str, str] | None:
    """Split ``<binders> : <type>`` at the top-level ':' (depth/comment aware).

    Skips ``:=`` (binder defaults) and never splits inside brackets, so
    ``(f : A → B := default) {n : ℕ}`` stays intact as binder text.
    """
    depth = 0
    index = 0
    while True:
        found = _scan(statement, index)
        if found is None:
            return None
        position, ch = found
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            depth = max(0, depth - 1)
        elif ch == ":" and depth == 0:
            if statement.startswith(":=", position):
                index = position + 2
                continue
            return statement[:position].strip(), statement[position + 1 :].strip()
        index = position + 1


def build_negation_goal(
    file_path: str, theorem_id: str, *, cwd: str = ""
) -> NegationGoal | dict[str, Any]:
    """Mechanically construct ¬P for a declaration (error dict on failure)."""
    region = declaration_region(Path(file_path), theorem_id)
    if not region:
        return {"error": f"declaration {theorem_id!r} not found", "error_code": "not_found"}
    text = str(region.get("text", "") or "").strip()
    statement = declaration_statement_text(text)

    # Strip attributes (@[...]) and modifier keywords before the decl keyword.
    cursor = 0
    while True:
        remainder = statement[cursor:].lstrip()
        cursor = len(statement) - len(remainder)
        if remainder.startswith("@["):
            depth = 0
            for offset, ch in enumerate(remainder):
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        cursor += offset + 1
                        break
            else:
                return {"error": "unterminated attribute", "error_code": "parse_failure"}
            continue
        word = remainder.split(None, 1)[0] if remainder else ""
        if word in _MODIFIER_WORDS:
            cursor += len(word)
            continue
        break
    remainder = statement[cursor:].lstrip()
    keyword = remainder.split(None, 1)[0] if remainder else ""
    if keyword not in _DECL_KEYWORDS:
        return {
            "error": f"unsupported declaration keyword {keyword!r}",
            "error_code": "unsupported_kind",
        }
    after_keyword = remainder[len(keyword) :].lstrip()
    name_match = re.match(r"[^\s({\[⦃:]+", after_keyword)
    if not name_match:
        return {"error": "could not read the declaration name", "error_code": "parse_failure"}
    name = name_match.group(0)
    if ".{" in name or name.endswith("."):
        return {
            "error": f"universe-binder names are not supported ({name!r})",
            "error_code": "ill_formed",
        }
    signature = after_keyword[len(name) :].strip()
    split = _split_signature(signature)
    if split is None:
        return {"error": "no top-level ':' found in the signature", "error_code": "parse_failure"}
    binders, result_type = split
    if not result_type:
        return {"error": "empty result type", "error_code": "parse_failure"}
    prop = f"∀ {binders}, {result_type}" if binders else result_type
    short = name.rsplit(".", 1)[-1]
    neg_name = f"neg_{short}"
    lean_code = f"theorem {neg_name} : ¬ ({prop}) := by\n  sorry"
    return NegationGoal(
        name=neg_name,
        original=statement,
        binders=binders,
        result_type=result_type,
        prop=prop,
        lean_code=lean_code,
    )


def scratch_header(file_path: str) -> str:
    """The file's ``import`` lines verbatim, plus ``import Plausible`` if absent."""
    try:
        lines = Path(file_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    imports = [line for line in lines if line.startswith("import ")]
    if not any(line.split()[1:2] == ["Plausible"] for line in imports):
        imports.append("import Plausible")
    # Fail closed on unbound identifiers: autoImplicit could silently
    # generalize a section variable and make the scratch prop DIFFER from the
    # theorem's elaborated statement — more ill_formed outcomes are the
    # correct price until section-variable capture exists.
    imports.append("set_option autoImplicit false")
    return "\n".join(imports)


def _messages_text(payload: Mapping[str, Any]) -> str:
    parts = [str(payload.get("error", "") or "")]
    for message in payload.get("messages") or []:
        if isinstance(message, Mapping):
            parts.append(str(message.get("message", "") or ""))
        else:
            parts.append(str(message))
    return "\n".join(part for part in parts if part)


def run_plausible_preprobe(
    file_path: str, theorem_id: str, *, cwd: str = "", timeout_s: int = 90
) -> dict[str, Any]:
    """Counterexample search via `plausible` — a decisive HINT, never proof."""
    goal = build_negation_goal(file_path, theorem_id, cwd=cwd)
    if isinstance(goal, dict):
        return {"verdict": "error", **goal}
    binder_text = f" {goal.binders}" if goal.binders else ""
    code = (
        scratch_header(file_path)
        + f"\n\nexample{binder_text} : {goal.result_type} := by\n  plausible"
    )
    payload = lean_scratch_check(code, cwd=cwd, timeout_s=timeout_s)
    text = _messages_text(payload)
    if "Found problems!" in text or "Found a counter-example" in text:
        return {
            "verdict": "counterexample",
            "counterexample_text": text[:1000],
        }
    if "Gave up" in text or "gave up" in text:
        return {"verdict": "gave_up"}
    if "Failed to create a `testable`" in text or "Failed to create a `Testable`" in text:
        return {"verdict": "not_testable"}
    if bool(payload.get("ok")):
        # 100 passing samples closes the goal like `admit` — plausibility only.
        return {"verdict": "passed_sampling"}
    return {"verdict": "error", "detail": text[:500]}


def _axioms_from_text(text: str, name: str) -> list[str] | None:
    if f"'{name}' does not depend on any axioms" in text:
        return []
    match = re.search(rf"'{re.escape(name)}' depends on axioms: \[([^\]]*)\]", text)
    if not match:
        return None
    return [token.strip() for token in match.group(1).split(",") if token.strip()]


def run_negation_attempt(
    goal: NegationGoal,
    *,
    file_path: str = "",
    cwd: str = "",
    timeout_s: int = 120,
    tactics: tuple[str, ...] = ("decide", "simp", "omega"),
) -> dict[str, Any]:
    """Try cheap closers on ¬P in scratch; proved ONLY with standard axioms."""
    header = scratch_header(file_path) if file_path else ""
    skeleton = f"{header}\n\n{goal.lean_code}".strip()
    shape_check = lean_scratch_check(skeleton, cwd=cwd, timeout_s=timeout_s)
    if not shape_check.get("success"):
        # Tool-level failure (probe unavailable, timeout): a recorded,
        # budget-consuming error — otherwise the trigger retries forever.
        return {"verdict": "probe_error", "detail": _messages_text(shape_check)[:500]}
    shape_errors = [
        message
        for message in shape_check.get("messages") or []
        if isinstance(message, Mapping) and str(message.get("severity", "")).lower() == "error"
    ]
    if shape_errors:
        # The statement itself fails to elaborate: never counts against the
        # probe budget (autoImplicit/scope edge cases).
        return {"verdict": "ill_formed", "detail": _messages_text(shape_check)[:500]}
    for tactic in tactics:
        code = (
            f"{header}\n\ntheorem {goal.name} : ¬ ({goal.prop}) := by\n  {tactic}\n"
            f"#print axioms {goal.name}"
        ).strip()
        payload = lean_scratch_check(code, cwd=cwd, timeout_s=timeout_s)
        if not payload.get("ok"):
            continue
        axioms = _axioms_from_text(_messages_text(payload), goal.name)
        if axioms is None:
            continue
        if set(axioms) <= STANDARD_AXIOMS:
            return {
                "verdict": "negation_proved",
                "tactic": tactic,
                "axioms": axioms,
                "axioms_ok": True,
            }
        return {
            "verdict": "inconclusive",
            "tactic": tactic,
            "axioms": axioms,
            "axioms_ok": False,
            "detail": "negation closed only via non-standard axioms",
        }
    return {"verdict": "inconclusive"}


# ---------------------------------------------------------------------------
# Budgeted probe pipeline with recorded outcomes.
# ---------------------------------------------------------------------------


def negation_probe_enabled() -> bool:
    import os

    raw = str(os.getenv("LEANFLOW_NEGATION_PROBE", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def probe_budget() -> int:
    import os

    try:
        return max(1, int(os.getenv("LEANFLOW_NEGATION_PROBE_BUDGET", "1") or 1))
    except ValueError:
        return 1


def remaining_probe_budget(
    probes: object,
    storage_key: str,
    *,
    budget: int | None = None,
    now: datetime | None = None,
) -> int:
    """Return the exact remaining scratch-probe budget for one theorem key.

    Count completed rows and non-reclaimable reservations so advisory route
    selection uses the same conservative semantics as the locked gate. A
    malformed reservation remains budget-consuming and therefore fail-closed.
    """
    limit = probe_budget() if budget is None else max(0, int(budget))
    entries = probes if isinstance(probes, list) else []
    reference_time = now or datetime.now(UTC)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=UTC)
    else:
        reference_time = reference_time.astimezone(UTC)
    used = sum(
        1
        for probe in entries
        if isinstance(probe, Mapping)
        and str(probe.get("key", "")) == storage_key
        and not _reservation_is_reclaimably_stale(probe, now=reference_time)
    )
    return max(0, limit - used)


def probe_after_failures() -> int:
    import os

    try:
        return max(1, int(os.getenv("LEANFLOW_NEGATION_PROBE_AFTER_FAILURES", "2") or 2))
    except ValueError:
        return 2


def probe_timeout_s() -> int:
    import os

    try:
        return max(10, int(os.getenv("LEANFLOW_NEGATION_PROBE_TIMEOUT_S", "120") or 120))
    except ValueError:
        return 120


def probe_reservation_stale_s() -> int:
    """Return the age after which a crashed scratch reservation is reclaimed."""
    import os

    # One probe can run plausible, shape, and several tactic checks, each with
    # the per-check timeout. Keep the reclaim window above that whole ladder.
    minimum = max(900, probe_timeout_s() * 8)
    try:
        configured = int(os.getenv("LEANFLOW_NEGATION_RESERVATION_STALE_S", minimum) or minimum)
    except ValueError:
        return minimum
    return max(minimum, configured)


def _probe_time(value: Any) -> datetime | None:
    """Parse a persisted probe timestamp, returning None when malformed."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _reservation_is_reclaimably_stale(
    entry: Mapping[str, Any],
    *,
    now: datetime,
) -> bool:
    """Return whether one timestamped reservation is safely reclaimable."""
    if entry.get("status") != "reserved":
        return False
    reserved_at = _probe_time(entry.get("reserved_at"))
    if reserved_at is None:
        # Legacy/broken reservations have no trustworthy age. Keep them
        # budget-consuming until an operator resolves the orphan.
        return False
    reference_time = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    stale_before = reference_time.astimezone(UTC).timestamp() - probe_reservation_stale_s()
    return reserved_at.timestamp() <= stale_before


def _recovered_probe_result(
    entry: Mapping[str, Any],
    *,
    theorem_id: str,
    file_path: str,
    storage_key: str,
) -> dict[str, Any]:
    """Return the normal runtime result shape for one completed ledger row."""
    completed = dict(entry)
    negation = dict(completed.get("negation") or {})
    result: dict[str, Any] = {
        "verdict": str(negation.get("verdict", "inconclusive") or "inconclusive"),
        "plausible": dict(completed.get("plausible") or {}),
        "negation": negation,
        "reservation": str(completed.get("reservation", "") or ""),
        "probe_entry": completed,
        "recovered": True,
    }
    if result["verdict"] == "negation_proved":
        from leanflow_cli.workflows.plan_state import node_id_for

        result["plan_delta"] = [
            {
                "node_id": node_id_for(theorem_id, file_path),
                "status": "false",
                "evidence": f"negation-probe:{storage_key}",
                "requires_promotion": True,
            }
        ]
    return result


def recover_latest_compatible_probe(
    file_path: str,
    theorem_id: str,
    *,
    cwd: str = "",
) -> dict[str, Any] | None:
    """Recover the latest completed row for the declaration's current signature.

    Match exact theorem and canonical-file identity, then require the persisted
    declaration-signature hash to equal a freshly rebuilt source declaration.
    Trigger, route reason, selection time, and row timestamp are intentionally
    irrelevant: signature compatibility is the recovery authority.
    """
    from leanflow_cli.workflows.queue_models import TheoremKey
    from leanflow_cli.workflows.workflow_json_io import read_json_file
    from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

    goal = build_negation_goal(file_path, theorem_id, cwd=cwd)
    if isinstance(goal, dict):
        return None
    canonical_file = Path(file_path).expanduser().resolve()
    storage_key = TheoremKey.make(theorem_id, str(canonical_file)).storage_key()
    current_signature = hashlib.sha256(goal.original.encode("utf-8")).hexdigest()
    summary = read_json_file(workflow_state_root() / "summary.json")
    rows = summary.get("negation_probes") or []
    if not isinstance(rows, list):
        return None
    for raw in reversed(rows):
        if not isinstance(raw, Mapping):
            continue
        entry = dict(raw)
        evidence = entry.get("promotion_evidence")
        entry_file = str(entry.get("file", "") or "").strip()
        if (
            entry.get("status") == "reserved"
            or not isinstance(entry.get("negation"), Mapping)
            or not isinstance(evidence, Mapping)
            or str(entry.get("key", "") or "") != storage_key
            or str(entry.get("theorem", "") or "").strip() != theorem_id
            or not entry_file
            or Path(entry_file).expanduser().resolve() != canonical_file
            or str(evidence.get("declaration_signature_sha256", "") or "") != current_signature
        ):
            continue
        return _recovered_probe_result(
            entry,
            theorem_id=theorem_id,
            file_path=str(canonical_file),
            storage_key=storage_key,
        )
    return None


def recover_persisted_probe(
    file_path: str,
    theorem_id: str,
    *,
    trigger: str,
    route_reason: str = "",
    selected_at: str = "",
) -> dict[str, Any] | None:
    """Recover an exact completed row persisted before outcome-stream failure.

    A replay is admitted only when the row postdates the route selection and
    matches theorem, file, trigger, and (when supplied) bounded route evidence.
    Bare reservations never count as completed work.
    """
    from leanflow_cli.workflows.queue_models import TheoremKey
    from leanflow_cli.workflows.workflow_json_io import read_json_file
    from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

    selected = _probe_time(selected_at)
    if selected is None:
        return None
    storage_key = TheoremKey.make(theorem_id, file_path).storage_key()
    summary = read_json_file(workflow_state_root() / "summary.json")
    expected_reason = str(route_reason or "").strip()[:MAX_ROUTE_REASON_CHARS]
    candidates: list[tuple[Any, dict[str, Any]]] = []
    for raw in summary.get("negation_probes") or []:
        if not isinstance(raw, Mapping):
            continue
        entry = dict(raw)
        recorded_at = _probe_time(entry.get("timestamp"))
        negation = entry.get("negation")
        if (
            entry.get("status") == "reserved"
            or not isinstance(negation, Mapping)
            or str(entry.get("key", "") or "") != storage_key
            or str(entry.get("theorem", "") or "").strip() != theorem_id
            or Path(str(entry.get("file", "") or "")).resolve() != Path(file_path).resolve()
            or str(entry.get("job_id", "") or "") != str(trigger or "")
            or recorded_at is None
            or recorded_at < selected
            or expected_reason
            and str(entry.get("route_reason", "") or "") != expected_reason
        ):
            continue
        candidates.append((recorded_at, entry))
    if not candidates:
        return None
    _, entry = max(candidates, key=lambda pair: pair[0])
    return _recovered_probe_result(
        entry,
        theorem_id=theorem_id,
        file_path=file_path,
        storage_key=storage_key,
    )


def run_negation_probe(
    file_path: str,
    theorem_id: str,
    *,
    cwd: str = "",
    trigger: str = "",
    route_reason: str = "",
) -> dict[str, Any]:
    """Full pipeline: goal -> plausible pre-probe -> cheap ¬P ladder; budgeted.

    Consumes exactly one budget unit per completed probe (ill-formed goals do
    not count); outcomes are recorded in summary.json.negation_probes and the
    outcomes stream. A ``negation_proved`` result carries a plan_delta
    PROPOSAL — flipping the node to false still requires promotion through
    the authoritative gate; the probe itself never writes a project
    file and is never an acceptance authority.
    """
    import uuid
    from datetime import UTC, datetime

    from leanflow_cli.workflows.plan_state import node_id_for
    from leanflow_cli.workflows.queue_models import TheoremKey
    from leanflow_cli.workflows.workflow_json_io import update_json_file
    from leanflow_cli.workflows.workflow_state import append_workflow_outcome
    from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

    if not negation_probe_enabled():
        return {"verdict": "disabled"}
    storage_key = TheoremKey.make(theorem_id, file_path).storage_key()
    summary_path = workflow_state_root() / "summary.json"
    budget = probe_budget()
    reservation = ""
    reserved_at = datetime.now(UTC).replace(microsecond=0)
    bounded_route_reason = str(route_reason or "").strip()[:MAX_ROUTE_REASON_CHARS]

    def reserve(summary: dict[str, Any]) -> str:
        probes = [
            dict(existing)
            for existing in (summary.get("negation_probes") or [])
            if isinstance(existing, Mapping)
            and not (
                str(existing.get("key", "") or "") == storage_key
                and _reservation_is_reclaimably_stale(existing, now=reserved_at)
            )
        ]
        if (
            remaining_probe_budget(
                probes,
                storage_key,
                budget=budget,
                now=reserved_at,
            )
            <= 0
        ):
            summary["negation_probes"] = probes
            return ""
        # Unique marker: a sequence number could collide with an ACTIVE
        # reservation after an ill_formed release (budget > 1).
        marker = f"{storage_key}#{uuid.uuid4().hex[:8]}"
        pending = {
            "key": storage_key,
            "reservation": marker,
            "status": "reserved",
            "theorem": theorem_id,
            "file": file_path,
            "job_id": trigger,
            "reserved_at": reserved_at.isoformat(),
        }
        if bounded_route_reason:
            pending["route_reason"] = bounded_route_reason
        probes.append(pending)
        summary["negation_probes"] = probes
        return marker

    # Reserve the budget unit UNDER the lock so concurrent runners can never
    # exceed the per-theorem budget; the reservation is filled in (or freed
    # on ill_formed) below.
    reservation = update_json_file(summary_path, reserve)
    if not reservation:
        from leanflow_cli.workflows.workflow_json_io import read_json_file

        rows = read_json_file(summary_path).get("negation_probes") or []
        matching_reservations = [
            dict(row)
            for row in rows
            if isinstance(row, Mapping)
            and str(row.get("key", "") or "") == storage_key
            and row.get("status") == "reserved"
        ]
        if matching_reservations:
            verdict = (
                "reservation_orphaned"
                if any(_probe_time(row.get("reserved_at")) is None for row in matching_reservations)
                else "reservation_pending"
            )
            return {
                "verdict": verdict,
                "reservation_count": len(matching_reservations),
            }
        return {"verdict": "budget_exhausted"}

    def release(summary: dict[str, Any]) -> None:
        summary["negation_probes"] = [
            probe
            for probe in (summary.get("negation_probes") or [])
            if not (isinstance(probe, Mapping) and probe.get("reservation") == reservation)
        ]

    try:
        goal = build_negation_goal(file_path, theorem_id, cwd=cwd)
        if isinstance(goal, dict):
            update_json_file(summary_path, release)
            return {"verdict": "error", **goal}
        plausible = run_plausible_preprobe(
            file_path,
            theorem_id,
            cwd=cwd,
            timeout_s=probe_timeout_s(),
        )
        negation = run_negation_attempt(
            goal,
            file_path=file_path,
            cwd=cwd,
            timeout_s=probe_timeout_s(),
        )
        if negation.get("verdict") == "ill_formed":
            # Statement-level elaboration failure: free the reserved budget unit.
            update_json_file(summary_path, release)
            return {"verdict": "ill_formed", "detail": negation.get("detail", "")}

        entry = {
            "key": storage_key,
            "reservation": reservation,
            "job_id": trigger,
            "theorem": theorem_id,
            "file": file_path,
            "plausible": plausible,
            "negation": negation,
            "promotion_evidence": {
                "declaration_signature_sha256": hashlib.sha256(
                    goal.original.encode("utf-8")
                ).hexdigest(),
                "source_revision_sha256": hashlib.sha256(Path(file_path).read_bytes()).hexdigest(),
                "negation_name": goal.name,
                "negation_prop": goal.prop,
                "original_signature": goal.original,
                "proof_tactic": str(negation.get("tactic", "") or ""),
            },
            "timestamp": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
        if bounded_route_reason:
            entry["route_reason"] = bounded_route_reason
    except Exception:
        # Before the reservation is filled, ordinary probe failures are safe to
        # retry immediately. Signals/BaseException remain timestamped so the
        # locked stale-reclaim policy owns their eventual recovery.
        with contextlib.suppress(Exception):
            update_json_file(summary_path, release)
        raise

    def fill(summary: dict[str, Any]) -> None:
        probes = [
            dict(existing)
            for existing in (summary.get("negation_probes") or [])
            if isinstance(existing, Mapping)
        ]
        for index, probe in enumerate(probes):
            if probe.get("reservation") == reservation:
                probes[index] = dict(entry)
                break
        else:
            probes.append(dict(entry))
        summary["negation_probes"] = probes

    update_json_file(summary_path, fill)
    append_workflow_outcome("negation-probe", dict(entry))

    result: dict[str, Any] = {
        "verdict": str(negation.get("verdict", "inconclusive")),
        "plausible": plausible,
        "negation": negation,
        "reservation": reservation,
        "probe_entry": entry,
    }
    if negation.get("verdict") == "negation_proved":
        # Proposal only: promotion through the gate flips the node.
        result["plan_delta"] = [
            {
                "node_id": node_id_for(theorem_id, file_path),
                "status": "false",
                "evidence": f"negation-probe:{storage_key}",
                "requires_promotion": True,
            }
        ]
    return result
