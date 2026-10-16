"""Bind proposed lexical source readings to ordinary independent coverage and coherence review."""

from __future__ import annotations

import copy
import re
from typing import Any

from .evidence import digest, identity
from .inventory_authority import complete_inventory
from .source_inventory import EXTRACTION_VERSION

LEXICAL_CODES = frozenset(
    {
        "unresolved_loader_effect",
        "inherited_tex_meanings",
        "unresolved_conditional",
        "unresolved_command",
        "unresolved_environment_structure",
    }
)
MAX_INTERPRETATION_CHARACTERS = 4096
RECONCILIATION_POLICY = "source-lexical-reading-v1"


def compact_requirements(value: dict[str, Any]) -> dict[str, Any]:
    """Keep all proposed meanings visible while moving long identity arrays to audit evidence."""
    result = {
        key: copy.deepcopy(entry)
        for key, entry in value.items()
        if key not in {"affected_region_ids", "candidate_source_ids"}
    }
    result.update(
        affected_region_count=len(value["affected_region_ids"]),
        candidate_source_count=len(value["candidate_source_ids"]),
        complete_membership_evidence_id="source-reconciliation-requirements",
    )
    return result


def _spans(inventory: dict[str, Any], diagnostic: dict[str, Any]) -> list[dict[str, Any]]:
    """Require frozen original coordinates, retaining every occurrence of aggregate commands."""
    spans = diagnostic.get("spans", [diagnostic["span"]] if "span" in diagnostic else [])
    if not isinstance(spans, list) or not spans:
        return []
    for span in spans:
        if not isinstance(span, dict):
            return []
        file = inventory.get("files", {}).get(span.get("file"), {})
        start, end = span.get("byte_start"), span.get("byte_end")
        if (
            not file
            or file.get("encoding") == "binary"
            or span.get("file_hash") != file.get("sha256")
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= file.get("size", -1)
        ):
            return []
        if not any(
            region.get("file") == span["file"]
            and region.get("coordinate_space") == "source_bytes"
            and (
                "occurrence_id" not in span or region.get("occurrence_id") == span["occurrence_id"]
            )
            and region["byte_start"] < end
            and start < region["byte_end"]
            for region in inventory.get("regions", [])
        ):
            return []
    return copy.deepcopy(spans)


def assert_current_extraction(inventory: dict[str, Any]) -> None:
    """Require an explicit fresh TeX inventory before granting current source authority."""
    if (
        inventory.get("source_kind") == "latex"
        and inventory.get("extraction_version") != EXTRACTION_VERSION
    ):
        raise ValueError(
            "The saved TeX extraction policy is outdated; use retry_extraction to create "
            "a fresh source revision before translation, review or current acceptance. "
            "Historical source and saved reviews remain inspectable."
        )


def _eligible(
    inventory: dict[str, Any], diagnostic: dict[str, Any], *, historical: bool = False
) -> bool:
    """Admit only bounded lexical candidates, never unknown execution or missing inputs."""
    return (
        inventory.get("source_kind") == "latex"
        and (historical or inventory.get("extraction_version") == EXTRACTION_VERSION)
        and (
            diagnostic.get("code") in LEXICAL_CODES
            or bool(_commented_bibliography(inventory, diagnostic))
        )
        and isinstance(diagnostic.get("id"), str)
        and bool(diagnostic["id"])
        and bool(_spans(inventory, diagnostic))
        and (
            diagnostic["code"] != "unresolved_environment_structure"
            or (
                diagnostic.get("independent_conditional_paths") is True
                and diagnostic.get("violation_witnesses_truncated") is False
                and isinstance(diagnostic.get("violation_witnesses"), list)
                and bool(diagnostic["violation_witnesses"])
                and all(
                    isinstance(witness, dict)
                    and _spans(inventory, {"spans": witness.get("source_spans")})
                    for witness in diagnostic["violation_witnesses"]
                )
            )
        )
    )


def _commented_bibliography(
    inventory: dict[str, Any], diagnostic: dict[str, Any]
) -> dict[str, Any] | None:
    """Expose a counterfactual commented reference only with frozen inline and PDF context."""
    if (
        diagnostic.get("code") != "missing_support_file"
        or diagnostic.get("support_command") != "bibliography"
        or diagnostic.get("support_role") != "rendered_bibliography"
        or diagnostic.get("support_unavailable_reason") is not None
    ):
        return None
    spans = _spans(inventory, diagnostic)
    companion = inventory.get("companion_pdf", {})
    pdf = inventory.get("files", {}).get(companion.get("file"), {})
    if (
        len(spans) != 1
        or companion.get("selection") != "explicit"
        or not isinstance(companion.get("file"), str)
        or not companion["file"].lower().endswith(".pdf")
        or pdf.get("encoding") != "binary"
        or not companion.get("sha256")
        or companion["sha256"] != pdf.get("sha256")
    ):
        return None
    span = spans[0]
    file = inventory["files"][span["file"]]
    text = file.get("text")
    if not isinstance(text, str):
        return None
    raw = text.encode(file.get("encoding", "utf-8"))
    if len(raw) != file["size"] or digest(raw) != file["sha256"].removeprefix("sha256:"):
        return None
    first, last = span["byte_start"], span["byte_end"]
    line_start = max(raw.rfind(b"\n", 0, first), raw.rfind(b"\r", 0, first)) + 1
    if (
        re.fullmatch(rb"[ \t]*%[^\r\n]*", raw[line_start:first]) is None
        or re.fullmatch(rb"\\bibliography\s*\{[A-Za-z0-9_./, \t-]+\}", raw[first:last]) is None
    ):
        return None
    inline = list(
        re.finditer(
            rb"(?m)^[ \t]*\\begin\{thebibliography\}[ \t]*\{[^{}\r\n]+\}"
            rb"[\s\S]*?^[ \t]*\\end\{thebibliography\}",
            raw,
        )
    )
    if len(inline) != 1 or re.search(rb"(?m)^[ \t]*\\bibitem(?:\[|\{)", inline[0][0]) is None:
        return None
    if any(
        definition.get("span", {}).get("file") == span["file"]
        and definition["span"]["byte_start"] < inline[0].end()
        and inline[0].start() < definition["span"]["byte_end"]
        for definition in inventory.get("macro_definitions", [])
    ):
        return None
    return {
        "interpretation_kind": "commented-reference-with-supplied-inline-bibliography",
        "commented_reference_span": {**span, "byte_start": line_start},
        "inline_bibliography_span": {
            "file": span["file"],
            "file_hash": file["sha256"],
            "byte_start": inline[0].start(),
            "byte_end": inline[0].end(),
            "coordinate_space": "source_bytes",
        },
        "companion_pdf": {"file": companion["file"], "sha256": companion["sha256"]},
        "boundary": (
            "The missing reference remains an execution counterfactual. Independently review "
            "the exact percent-comment reading, complete inline bibliography and companion "
            "images; do not invent a supplied database, rendered support file or TeX execution."
        ),
    }


def lexical_admission(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    """Return hard blockers while leaving eligible lexical uncertainty pending, not approved."""
    return [
        row
        for row in inventory.get("diagnostics", [])
        if row.get("blocking") and not _eligible(inventory, row)
    ]


def reconciliation_requirements(
    record: dict[str, Any], *, historical: bool = False
) -> dict[str, Any]:
    """Bind complete source readings, permitting old-policy replay only for historical display."""
    inventory = complete_inventory(record)
    diagnostics = []
    for diagnostic in inventory.get("diagnostics", []):
        if diagnostic.get("blocking") and _eligible(inventory, diagnostic, historical=historical):
            diagnostics.append(
                {
                    "diagnostic_id": diagnostic["id"],
                    "code": diagnostic["code"],
                    "source_spans": _spans(inventory, diagnostic),
                    "diagnostic_sha256": identity(diagnostic),
                    "interpretation_scope": "complete-source-lexical-reading",
                    **(
                        {"commented_support_evidence": evidence}
                        if (evidence := _commented_bibliography(inventory, diagnostic))
                        else {}
                    ),
                }
            )
    result = {
        "policy": RECONCILIATION_POLICY,
        "base_revision_id": record["revision_id"],
        "source_bundle": record["source_bundle"],
        "inventory_sha256": identity(inventory),
        "diagnostics": diagnostics,
        "affected_region_ids": [row["id"] for row in inventory["regions"]],
        "candidate_source_ids": [row["id"] for row in inventory["items"]],
        "authority": "author-proposal-requires-independent-coverage-and-coherence",
        "boundary": (
            "Propose a literal lexical interpretation of every frozen occurrence. Preserve "
            "all conditional alternatives, candidates, original bytes and include occurrences. "
            "Explain package and inherited meanings; do not assert TeX execution, loader purity, "
            "user verification, branch exclusion or mathematical approval. Unknown effects, "
            "missing evidence and genuine mathematical ambiguity still block. Normal fresh "
            "coverage and coherence reviews must independently check this reading, complete "
            "source context and all companion page images; a proposal alone grants no authority."
        ),
    }
    result["requirements_id"] = "source-reading-" + identity(result)
    return result


def apply_reconciliation(
    record: dict[str, Any], proposal: Any, *, historical: bool = False
) -> None:
    """Record exact proposals; reserve historical mode for saved-evidence reconstruction."""
    if not historical:
        assert_current_extraction(complete_inventory(record))
    requirements = reconciliation_requirements(record, historical=historical)
    expected = {row["diagnostic_id"] for row in requirements["diagnostics"]}
    if not expected and proposal is None:
        record.pop("source_reconciliation", None)
        return
    if not isinstance(proposal, dict):
        raise ValueError("Source reading root must be an object")
    if set(proposal) != {"requirements_id", "entries"}:
        raise ValueError("Source reading root keys must be exactly requirements_id and entries")
    if proposal["requirements_id"] != requirements["requirements_id"]:
        raise ValueError(
            "Source reading requirements_id must bind the exact frozen reconciliation requirements"
        )
    if not isinstance(proposal["entries"], list):
        raise ValueError("Source reading entries must be a list")
    entries: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(proposal["entries"], start=1):
        location = f"Source reading entry {index}"
        if not isinstance(row, dict):
            raise ValueError(f"{location} must be an object")
        if set(row) != {"diagnostic_id", "interpretation", "reason"}:
            raise ValueError(
                f"{location} keys must be exactly diagnostic_id, interpretation and reason"
            )
        if not isinstance(row["diagnostic_id"], str):
            raise ValueError(f"{location} diagnostic_id must be a string")
        if row["diagnostic_id"] not in expected:
            raise ValueError(f"{location} diagnostic_id is not in the frozen requirements")
        if row["diagnostic_id"] in entries:
            raise ValueError(f"{location} diagnostic_id duplicates an earlier entry")
        for key in ("interpretation", "reason"):
            if not isinstance(row[key], str) or not row[key].strip():
                raise ValueError(f"{location} {key} must be a nonempty string")
            if len(row[key]) > MAX_INTERPRETATION_CHARACTERS:
                raise ValueError(
                    f"{location} {key} must contain at most "
                    f"{MAX_INTERPRETATION_CHARACTERS} Python characters"
                )
        entries[row["diagnostic_id"]] = copy.deepcopy(row)
    if set(entries) != expected:
        raise ValueError("Source reading omitted frozen lexical uncertainty")
    # Keep an original denominator even for the pre-preparation protocol. Candidate
    # diagnostics can become review-pending without rewriting extraction authority.
    if "full_inventory" not in record:
        record["full_inventory"] = copy.deepcopy(record["inventory"])
    record["source_reconciliation"] = {
        **requirements,
        "entries": [entries[row["diagnostic_id"]] for row in requirements["diagnostics"]],
        "actor": "author_proposal",
        "verified": False,
    }
    for diagnostic in record["inventory"].get("diagnostics", []):
        if diagnostic["id"] in entries:
            diagnostic.update(blocking=False, pending_independent_review=True)


def assert_reconciliation_reviewed(
    record: dict[str, Any], reviews: list[dict[str, Any]], *, historical: bool = False
) -> None:
    """Require current-policy coverage by default; historical replay grants no current authority."""
    if not historical:
        assert_current_extraction(complete_inventory(record))
    stored = record.get("source_reconciliation")
    if stored is not None and not isinstance(stored, dict):
        raise ValueError("Source reading evidence must be a frozen controller-bound object")
    original = {
        **record,
        "revision_id": (stored or {}).get("base_revision_id", record["revision_id"]),
    }
    requirements = reconciliation_requirements(original, historical=historical)
    if not requirements["diagnostics"] and stored is None:
        return
    if (
        not isinstance(stored, dict)
        or any(stored.get(key) != value for key, value in requirements.items())
        or stored.get("actor") != "author_proposal"
        or stored.get("verified") is not False
        or stored["base_revision_id"]
        not in {record["revision_id"], record.get("author_base_revision")}
    ):
        raise ValueError("Source reading does not match the complete frozen source authority")
    replay = copy.deepcopy(original)
    apply_reconciliation(
        replay,
        {"requirements_id": stored["requirements_id"], "entries": stored.get("entries")},
        historical=historical,
    )
    if replay["source_reconciliation"] != stored:
        raise ValueError("Source reading evidence differs from the proposed exact interpretation")
    if not reviews or any(
        row.get("revision_id") != record["revision_id"]
        or row.get("accepted") is not True
        or row.get("cleanup_verified") is not True
        for row in reviews
    ):
        raise ValueError("Source reading requires fresh independently closed reviews")
    expected = {row["id"] for row in record["inventory"]["regions"]}
    if record.get("proposal_phase") != "representative_slice" and expected != set(
        requirements["affected_region_ids"]
    ):
        raise ValueError("Complete lexical review cannot narrow the original source census")
    coverage = [row for review in reviews for row in review.get("coverage", [])]
    passed = [row.get("region_id") for row in coverage if row.get("decision") == "PASS"]
    if (
        set(passed) != expected
        or len(passed) != len(set(passed))
        or any(row.get("decision") != "PASS" for row in coverage)
        or reviews[-1].get("coherence_decision") != "PASS"
    ):
        raise ValueError("Source reading needs complete canonical coverage and coherent review")
