"""Negation feasibility probe (Phase 3, specs Part II §5).

The Hilbert-loop feasibility engine's Lean-facing leaf: mechanically build
``¬P`` for a stuck declaration, run the `plausible` counterexample pre-probe
(hint only — passing samples close the goal like ``admit`` and are NEVER
proof), then try a cheap tactic ladder on the negation in LeanProbe scratch.
``negation_proved`` requires ok=true AND ``#print axioms`` inside the
standard set — and even then a scratch verdict is only routing evidence:
flipping a project node to ``false`` requires promotion through the
authoritative gate (roadmap §4.11); the probe never writes a project file.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_declarations import declaration_region
from leanflow_cli.lean.lean_incremental import lean_scratch_check

STANDARD_AXIOMS = {"propext", "Quot.sound", "Classical.choice"}

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


def _statement_end(text: str) -> int:
    """Index of the top-level ':=' that starts the proof body (len if none)."""
    depth = 0
    index = 0
    seen_colon = False
    while True:
        found = _scan(text, index)
        if found is None:
            return len(text)
        position, ch = found
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            depth = max(0, depth - 1)
        elif depth == 0 and ch == ":":
            if text.startswith(":=", position):
                if seen_colon:
                    return position
                # ':=' before the type colon: binder default at top level is
                # impossible in a theorem header, so this is the body start.
                return position
            seen_colon = True
        index = position + 1


def build_negation_goal(
    file_path: str, theorem_id: str, *, cwd: str = ""
) -> NegationGoal | dict[str, Any]:
    """Mechanically construct ¬P for a declaration (error dict on failure)."""
    region = declaration_region(Path(file_path), theorem_id)
    if not region:
        return {"error": f"declaration {theorem_id!r} not found", "error_code": "not_found"}
    text = str(region.get("text", "") or "").strip()
    statement = text[: _statement_end(text)].strip()

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
# Pipeline: budgeted probe with recorded outcomes (specs §5 d/e)
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


def run_negation_probe(
    file_path: str,
    theorem_id: str,
    *,
    cwd: str = "",
    trigger: str = "",
) -> dict[str, Any]:
    """Full pipeline: goal -> plausible pre-probe -> cheap ¬P ladder; budgeted.

    Consumes exactly one budget unit per completed probe (ill-formed goals do
    not count); outcomes are recorded in summary.json.negation_probes and the
    outcomes stream. A ``negation_proved`` result carries a plan_delta
    PROPOSAL — flipping the node to false still requires promotion through
    the authoritative gate (§4.11); the probe itself never writes a project
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

    def reserve(summary: dict[str, Any]) -> str:
        probes = [
            dict(existing)
            for existing in (summary.get("negation_probes") or [])
            if isinstance(existing, Mapping)
        ]
        used = sum(1 for probe in probes if str(probe.get("key", "")) == storage_key)
        if used >= budget:
            return ""
        # Unique marker: a sequence number could collide with an ACTIVE
        # reservation after an ill_formed release (budget > 1).
        marker = f"{storage_key}#{uuid.uuid4().hex[:8]}"
        probes.append({"key": storage_key, "reservation": marker, "status": "reserved"})
        summary["negation_probes"] = probes
        return marker

    # Reserve the budget unit UNDER the lock so concurrent runners can never
    # exceed the per-theorem budget; the reservation is filled in (or freed
    # on ill_formed) below.
    reservation = update_json_file(summary_path, reserve)
    if not reservation:
        return {"verdict": "budget_exhausted"}

    def release(summary: dict[str, Any]) -> None:
        summary["negation_probes"] = [
            probe
            for probe in (summary.get("negation_probes") or [])
            if not (isinstance(probe, Mapping) and probe.get("reservation") == reservation)
        ]

    goal = build_negation_goal(file_path, theorem_id, cwd=cwd)
    if isinstance(goal, dict):
        update_json_file(summary_path, release)
        return {"verdict": "error", **goal}
    plausible = run_plausible_preprobe(file_path, theorem_id, cwd=cwd, timeout_s=probe_timeout_s())
    negation = run_negation_attempt(goal, file_path=file_path, cwd=cwd, timeout_s=probe_timeout_s())
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
        "timestamp": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }

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
    }
    if negation.get("verdict") == "negation_proved":
        # Proposal only: §4.11 promotion through the gate flips the node.
        result["plan_delta"] = [
            {
                "node_id": node_id_for(theorem_id, file_path),
                "status": "false",
                "evidence": f"negation-probe:{storage_key}",
                "requires_promotion": True,
            }
        ]
    return result
