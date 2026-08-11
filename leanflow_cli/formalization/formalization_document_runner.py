"""Parse formalization blueprints and classify document-workflow phases.

The helpers inspect workflow state and blueprint manifests for planning,
independent review, organization, and prover handoff. They are deliberately
independent of runner state and remain re-exported from ``native_runner`` for
compatibility.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from leanflow_cli.native.native_config import (
    _read_text_env,
    _workflow_kind,
)

__all__ = [
    "_BLUEPRINT_UNRESOLVED_FIDELITY_RE",
    "_autoformalizer_advisory_review_due",
    "_blueprint_block_missing",
    "_blueprint_bullet_block",
    "_blueprint_bullet_value",
    "_blueprint_checklist_item_checked",
    "_blueprint_fidelity_field",
    "_blueprint_fidelity_field_unresolved",
    "_blueprint_first_bullet_value",
    "_blueprint_import_plan_section",
    "_blueprint_source_inventory_entries",
    "_blueprint_value_missing",
    "_document_formalization_blueprint_checklist_issues",
    "_document_formalization_blueprint_waiting_for_review",
    "_document_formalization_handoff_blocked_state",
    "_document_formalization_has_draft_sorries",
    "_document_formalization_manifest_blocks",
    "_document_formalization_manifest_labels",
    "_document_formalization_needs_blueprint_plan",
    "_document_formalization_organization_phase_active",
    "_document_formalization_organization_phase_needed",
    "_document_formalization_planner_phase",
    "_document_formalization_ready_for_prover_handoff",
    "_document_formalization_requested",
    "_document_formalization_review_prompt",
    "_document_formalization_waiting_for_independent_review",
]


def _document_formalization_waiting_for_independent_review(
    live_state: Mapping[str, Any] | None,
) -> bool:
    if _workflow_kind() != "formalize" or not _document_formalization_requested():
        return False
    if _document_formalization_blueprint_waiting_for_review():
        return True
    handoff = dict((live_state or {}).get("document_formalization_handoff", {}) or {})
    if bool(handoff.get("ok", True)):
        return False
    issues = [str(issue or "") for issue in handoff.get("issues", []) or []]
    if not issues:
        return False
    review_markers = ("statement/source verification", "statement verification")
    if not any(any(marker in issue.lower() for marker in review_markers) for issue in issues):
        return False
    planner_fix_markers = (
        "planner has not drafted",
        "preflight",
        "hard diagnostics",
        "blueprint file",
        "target lean file",
        "root module",
        "scaffold import",
        "import plan",
        "source locator",
        "planned declarations",
        "statement-fidelity review",
        "source proof/prover notes",
        "lean doc comment",
        "missing from target lean file",
        "not parseable",
    )
    return not any(
        any(marker in issue.lower() for marker in planner_fix_markers) for issue in issues
    )


def _document_formalization_handoff_blocked_state(live_state: Mapping[str, Any] | None) -> bool:
    if _workflow_kind() != "formalize" or not _document_formalization_requested():
        return False
    if _document_formalization_blueprint_waiting_for_review():
        return True
    handoff = dict((live_state or {}).get("document_formalization_handoff", {}) or {})
    return bool(handoff) and not bool(handoff.get("ok", True))


def _document_formalization_ready_for_prover_handoff(live_state: Mapping[str, Any] | None) -> bool:
    if _workflow_kind() != "formalize" or not _document_formalization_requested():
        return False
    current = dict(live_state or {})
    handoff = dict(current.get("document_formalization_handoff", {}) or {})
    if not handoff or not bool(handoff.get("ok", False)):
        return False
    if _document_formalization_blueprint_waiting_for_review():
        return False
    try:
        generated_proof_sorry_count = int(
            current.get("document_formalization_proof_sorry_count", 0) or 0
        )
    except Exception:
        generated_proof_sorry_count = 0
    if generated_proof_sorry_count > 0:
        return True
    try:
        sorry_count = int(current.get("sorry_count", 0) or 0)
    except Exception:
        sorry_count = 0
    return sorry_count > 0


def _document_formalization_has_draft_sorries(live_state: Mapping[str, Any] | None) -> bool:
    if _workflow_kind() != "formalize" or not _document_formalization_requested():
        return False
    try:
        generated_proof_sorry_count = int(
            (live_state or {}).get("document_formalization_proof_sorry_count", 0) or 0
        )
    except Exception:
        generated_proof_sorry_count = 0
    if generated_proof_sorry_count > 0:
        return True
    try:
        sorry_count = int((live_state or {}).get("sorry_count", 0) or 0)
    except Exception:
        sorry_count = 0
    return sorry_count > 0


def _document_formalization_planner_phase(agent: Any) -> bool:
    if _workflow_kind() == "formalize" and _document_formalization_requested():
        return True
    autonomy_state = getattr(agent, "_managed_autonomy_state", {}) or {}
    assignment = dict(dict(autonomy_state or {}).get("current_queue_assignment") or {})
    return not bool(str(assignment.get("target_symbol", "") or "").strip())


def _document_formalization_review_prompt(live_state: Mapping[str, Any]) -> str:
    source = _read_text_env("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "").strip()
    source_kind = _read_text_env("LEANFLOW_FORMALIZATION_DOCUMENT_KIND", "").strip() or "document"
    target = str(
        live_state.get("active_file_label", "") or live_state.get("active_file", "") or ""
    ).strip()
    blueprint = _read_text_env("LEANFLOW_FORMALIZATION_BLUEPRINT", "").strip()
    context = _read_text_env("LEANFLOW_FORMALIZATION_CONTEXT", "").strip()
    handoff = dict(live_state.get("document_formalization_handoff", {}) or {})
    issues = "\n".join(f"- {issue}" for issue in handoff.get("issues", []) or []) or "- [none]"
    return (
        "Run the independent statement/source verification pass for this document formalization.\n\n"
        "You are a fresh reviewer, not the drafting formalizer. Do not rely on the previous conversation. "
        "Inspect the source document, blueprint, and Lean target directly, then correct the plan or Lean statements "
        "when the draft does not match the source. If you are running as a read-only command/model verifier, "
        "start the response with exactly `PASS` or `BLOCK`, then give findings and correction steps.\n\n"
        f"- source {source_kind}: {source or '[missing]'}\n"
        f"- planner blueprint: {blueprint or '[missing]'}\n"
        f"- target Lean file: {target or '[missing]'}\n"
        f"- planner context: {context or '[missing]'}\n\n"
        "Note: the target Lean file may be an aggregator such as `Main.lean`. If so, inspect the generated "
        "sibling/imported modules listed in the blueprint layout before deciding declarations or imports are missing.\n\n"
        "Current handoff issues:\n"
        f"{issues}\n\n"
        "Required review actions:\n"
        "1. Use `read_pdf` for project-local PDF text, and use `formalization_document_inspect`, `lean_capabilities`, and `lean_inspect` before editing.\n"
        "2. Compare every source theorem/lemma/definition/proposition/corollary/conjecture/remark entry against the Lean declaration and nearby Lean doc comment.\n"
        "3. For each entry, fill `Source qualifiers`, `Lean coverage`, and `Scope changes`. Source qualifiers should "
        "cover mathematical object class, quantifier order, parameter domain, output codomain, equality/image condition, "
        "side conditions, and follow-on claims that are part of the source statement.\n"
        "4. Treat every explicit source qualifier as statement content, not proof commentary. The Lean theorem must "
        "assert it, a companion declaration must cover it, or the blueprint must mark an intentional scope change.\n"
        "5. If the source gives a parameter-domain conversion, representation bridge, or follow-on equivalence, "
        "formalize that as a separate declaration or record the omission as unapproved.\n"
        "6. In particular, a simpler Lean encoding does not by itself cover a source claim about a richer "
        "object class or representation. Require a bridge definition/declaration, or mark Lean coverage "
        "as partial and record the representation scope change.\n"
        "7. Fix mismatched Lean statements, missing source locators, wrong import plans, missing complete source-proof attachments, or weak prover notes.\n"
        "8. Leave theorem/lemma/example proofs as `by sorry`; do not solve proofs in this review pass.\n"
        "9. Require the blueprint to carry the complete source proof when the source contains one, or an explicit "
        "reason the proof text is unavailable. Require nearby Lean doc comments to give a compact proof nudge "
        "that the prover sees without reopening the whole paper.\n"
        "10. Only after checking all fidelity axes, source-proof completeness, doc-comment nudges, and Lean statement "
        "translation for a source entry, approve it. If this review is running as a read-only command/model verifier, "
        "return `PASS` when the entries are acceptable; do not return `BLOCK` only because you cannot edit the "
        "blueprint. The runner will record the approval stamp after a read-only `PASS`. Do not approve an entry "
        "because a build succeeds.\n"
        "11. Run `lean_verify(mode=project)` after root imports include the generated formalization. "
        "Module/file checks are acceptable while iterating, but project-level verification is required for handoff readiness.\n"
        "12. Stop with a concise report: approved entries, corrected entries, remaining blockers, and whether `/prove` may start.\n"
    )


def _autoformalizer_advisory_review_due(
    *,
    local_ok: bool,
    issues: Sequence[str],
    completion: bool,
) -> bool:
    # External/source-fidelity reviewers are useful only after deterministic
    # local gates say the draft is structurally ready. Running a slow configured
    # verifier while obvious local blockers remain makes the formalizer look
    # stuck and feeds the model feedback it cannot act on yet.
    return bool(local_ok)


def _document_formalization_requested() -> bool:
    return _workflow_kind() == "formalize" and bool(
        _read_text_env("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "").strip()
    )


def _document_formalization_needs_blueprint_plan() -> bool:
    if not _document_formalization_requested():
        return False
    blueprint = _read_text_env("LEANFLOW_FORMALIZATION_BLUEPRINT", "").strip()
    if not blueprint:
        return True
    try:
        text = Path(blueprint).read_text(encoding="utf-8")
    except Exception:
        return True
    lowered = text.lower()
    if "_pending_" in lowered:
        return True
    entries = _blueprint_source_inventory_entries(text)
    if not entries:
        return "planner preflight created" in lowered
    for entry in entries.values():
        planned = _blueprint_first_bullet_value(
            entry,
            (
                "Planned Lean declarations",
                "Planned declarations",
                "Lean declarations",
                "Declaration mapping",
            ),
        )
        review = _blueprint_bullet_block(
            entry, "Formal statement review"
        ) or _blueprint_bullet_value(
            entry,
            "Formal statement review",
        )
        notes = _blueprint_first_bullet_value(
            entry, ("Source proof / prover notes", "Proof strategy", "Prover notes")
        )
        if (
            _blueprint_value_missing(planned)
            or _blueprint_block_missing(review)
            or _blueprint_value_missing(notes)
        ):
            return True
    return False


def _blueprint_import_plan_section(text: str) -> str:
    match = re.search(
        r"^##\s+(?:Lean\s+)?Import Plan\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
        str(text or ""),
        flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    return str(match.group("body") or "") if match else ""


def _document_formalization_manifest_labels() -> list[str]:
    manifest = _read_text_env("LEANFLOW_FORMALIZATION_MANIFEST", "").strip()
    if not manifest:
        return []
    try:
        payload = json.loads(Path(manifest).read_text(encoding="utf-8"))
    except Exception:
        return []
    labels: list[str] = []
    for block in payload.get("theorem_blocks", []) or []:
        if not isinstance(block, Mapping):
            continue
        label = str(block.get("label", "") or "").strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def _document_formalization_manifest_blocks() -> list[dict[str, Any]]:
    manifest = _read_text_env("LEANFLOW_FORMALIZATION_MANIFEST", "").strip()
    if not manifest:
        return []
    try:
        payload = json.loads(Path(manifest).read_text(encoding="utf-8"))
    except Exception:
        return []
    blocks: list[dict[str, Any]] = []
    for block in payload.get("theorem_blocks", []) or []:
        if not isinstance(block, Mapping):
            continue
        label = str(block.get("label", "") or "").strip()
        if not label:
            continue
        blocks.append(
            {
                "label": label,
                "kind": str(block.get("kind", "") or "").strip().lower(),
                "has_proof": bool(str(block.get("proof", "") or "").strip()),
                "statement": str(block.get("statement", "") or ""),
                "proof": str(block.get("proof", "") or ""),
            }
        )
    return blocks


def _blueprint_source_inventory_entries(text: str) -> dict[str, str]:
    section = re.search(
        r"^##\s+Source Statement Inventory\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
        str(text or ""),
        flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if not section:
        return {}
    body = str(section.group("body") or "")
    entries: dict[str, str] = {}
    matches = list(
        re.finditer(
            r"^###\s+(?P<label>[^\n]+?)\s*$",
            body,
            flags=re.MULTILINE,
        )
    )
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        label = str(match.group("label") or "").strip()
        if label:
            entries[label] = body[start:end].strip()
    return entries


def _blueprint_bullet_value(entry: str, label: str) -> str:
    match = re.search(
        rf"^\s*-\s*{re.escape(label)}\s*:\s*(?P<value>.*?)\s*$",
        str(entry or ""),
        flags=re.MULTILINE | re.IGNORECASE,
    )
    return str(match.group("value") or "").strip() if match else ""


def _blueprint_first_bullet_value(entry: str, labels: Sequence[str]) -> str:
    for label in labels:
        value = _blueprint_bullet_value(entry, label)
        if value:
            return value
    return ""


def _blueprint_bullet_block(entry: str, label: str) -> str:
    text = str(entry or "")
    match = re.search(
        rf"^\s*-\s*{re.escape(label)}\s*:\s*(?P<value>.*?)\s*$",
        text,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    if not match:
        return ""
    start = match.start()
    next_match = re.search(
        r"^-\s*[A-Za-z][A-Za-z0-9 /_-]{0,80}\s*:\s*",
        text[match.end() :],
        flags=re.MULTILINE,
    )
    end = match.end() + next_match.start() if next_match else len(text)
    return text[start:end].strip()


def _blueprint_value_missing(value: str) -> bool:
    lowered = str(value or "").strip().lower()
    return not lowered or lowered in {"_pending_", "pending", "todo", "tbd"}


def _blueprint_block_missing(value: str) -> bool:
    text = str(value or "").strip()
    if _blueprint_value_missing(text):
        return True
    return bool(re.search(r":\s*(_pending_|pending|todo|tbd)\s*$", text, flags=re.IGNORECASE))


def _blueprint_fidelity_field(entry: str, labels: Sequence[str]) -> str:
    for label in labels:
        value = _blueprint_bullet_block(entry, label) or _blueprint_bullet_value(entry, label)
        if value:
            return value
    return ""


_BLUEPRINT_UNRESOLVED_FIDELITY_RE = re.compile(
    r"\b(_pending_|pending|todo|tbd|missing|unresolved|unchecked|unverified|not covered|not formalized|omitted|weakened)\b",
    flags=re.IGNORECASE,
)


def _blueprint_fidelity_field_unresolved(value: str) -> bool:
    text = str(value or "").strip()
    if _blueprint_block_missing(text):
        return True
    return bool(_BLUEPRINT_UNRESOLVED_FIDELITY_RE.search(text))


def _blueprint_checklist_item_checked(text: str, pattern: str) -> bool | None:
    compiled = re.compile(
        rf"^\s*-\s*\[(?P<checked>[ xX])\]\s*{pattern}\s*$",
        flags=re.MULTILINE | re.IGNORECASE,
    )
    match = compiled.search(str(text or ""))
    if not match:
        return None
    return str(match.group("checked") or "").lower() == "x"


def _document_formalization_blueprint_checklist_issues(blueprint_text: str) -> list[str]:
    review_checked = _blueprint_checklist_item_checked(
        blueprint_text,
        r"Run independent statement/source verification review and apply corrections\.",
    )
    proof_ready_checked = _blueprint_checklist_item_checked(
        blueprint_text,
        r"(?:Hand stable (?:theorem/lemma/example )?`sorry` declarations to the managed prover queue|Mark stable theorem/lemma/example `sorry` declarations ready for a user-started prove workflow)\.",
    )
    if proof_ready_checked and review_checked is not True:
        return [
            "blueprint proof-ready checklist item is checked before independent statement/source review is checked"
        ]
    return []


def _document_formalization_blueprint_waiting_for_review() -> bool:
    if _workflow_kind() != "formalize" or not _document_formalization_requested():
        return False
    blueprint_path = _read_text_env("LEANFLOW_FORMALIZATION_BLUEPRINT", "").strip()
    if not blueprint_path:
        return False
    try:
        blueprint_text = Path(blueprint_path).read_text(encoding="utf-8")
    except Exception:
        return False
    lowered = blueprint_text.lower()
    entries = _blueprint_source_inventory_entries(blueprint_text)
    if not entries:
        return "awaiting independent statement/source verification" in lowered or (
            "pending review" in lowered and "statement" in lowered and "verification" in lowered
        )
    saw_inventory_entry = False
    for block in _document_formalization_manifest_blocks():
        entry = entries.get(str(block.get("label", "") or ""), "")
        if not entry:
            continue
        saw_inventory_entry = True
        verification = _blueprint_first_bullet_value(
            entry,
            (
                "Statement verification status",
                "Statement/source verification",
                "Source verification status",
                "Verification status",
            ),
        )
        if _blueprint_value_missing(verification):
            return True
        if not re.search(
            r"\b(approved|verified|reviewed|accepted)\b", verification, flags=re.IGNORECASE
        ):
            return True
    if saw_inventory_entry:
        return False
    if "awaiting independent statement/source verification" in lowered:
        return True
    if "pending review" in lowered and "statement" in lowered and "verification" in lowered:
        return True
    return False


def _document_formalization_organization_phase_active(
    live_state: Mapping[str, Any] | None,
    autonomy_state: Mapping[str, Any] | None,
) -> bool:
    if not _document_formalization_ready_for_prover_handoff(live_state):
        return False
    state = dict(autonomy_state or {})
    return bool(state.get("document_formalization_organization_turn_started")) and not bool(
        state.get("document_formalization_organization_completed")
    )


def _document_formalization_organization_phase_needed(
    live_state: Mapping[str, Any] | None,
    autonomy_state: Mapping[str, Any] | None,
) -> bool:
    if not _document_formalization_ready_for_prover_handoff(live_state):
        return False
    state = dict(autonomy_state or {})
    if state.get("document_formalization_organization_completed"):
        return False
    return not bool(state.get("document_formalization_organization_turn_started"))
