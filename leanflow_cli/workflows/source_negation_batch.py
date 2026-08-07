"""Build and classify bounded exact-source negation harness batches."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from typing import Any

from leanflow_cli.workflows.source_negation_harness import SourceNegationHarness

COMPATIBLE = "compatible"
INCOMPATIBLE = "incompatible"
UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class BatchCandidateInput:
    """Describe one exact alias and its source-relative insertion point."""

    proof_declaration: str
    candidate_name: str
    alias: str
    insert_at: int
    harness: SourceNegationHarness


@dataclass(frozen=True)
class BatchCandidateRegion:
    """Bind one candidate to its exact line range in a batch harness."""

    proof_declaration: str
    candidate_name: str
    alias: str
    proof_tactic: str
    start_line: int
    tactic_start_line: int
    proof_end_line: int
    axiom_line: int
    end_line: int


@dataclass(frozen=True)
class SourceNegationBatchHarness:
    """Contain one source-prefix check and every inserted candidate range."""

    source: str
    candidates: tuple[BatchCandidateRegion, ...]


@dataclass(frozen=True)
class BatchCandidateVerdict:
    """Report non-authoritative compatibility evidence for one candidate."""

    proof_declaration: str
    disposition: str
    reason: str
    failure_kind: str = ""
    retryable: bool = False
    axioms: tuple[str, ...] = ()


def build_batch_harness(
    source: str,
    candidates: Sequence[BatchCandidateInput],
) -> SourceNegationBatchHarness:
    """Insert unique aliases after their declarations in one bounded source prefix.

    Source-order insertion preserves private and namespace scope. Returned line
    ranges refer to the generated source consumed by Lean, not the original.
    Commands after the final candidate are deliberately omitted: they cannot
    contribute to candidate compatibility and may contain an unrelated slow or
    broken declaration that would make every verdict spuriously uncertain.
    """
    lines = str(source).splitlines()
    indexed = list(enumerate(candidates))
    aliases = [candidate.alias for candidate in candidates]
    declarations = [candidate.proof_declaration for candidate in candidates]
    if len(set(aliases)) != len(aliases) or len(set(declarations)) != len(declarations):
        raise ValueError("source-negation batch identities must be unique")
    for candidate in candidates:
        if candidate.insert_at <= 0 or candidate.insert_at > len(lines):
            raise ValueError("source-negation batch insertion range is invalid")

    generated: list[str] = []
    cursor = 0
    regions_by_index: dict[int, BatchCandidateRegion] = {}
    for original_index, candidate in sorted(
        indexed,
        key=lambda item: (item[1].insert_at, item[0]),
    ):
        generated.extend(lines[cursor : candidate.insert_at])
        declaration_lines = candidate.harness.declaration.splitlines()
        start_line = len(generated) + 1
        generated.extend(declaration_lines)
        regions_by_index[original_index] = BatchCandidateRegion(
            proof_declaration=candidate.proof_declaration,
            candidate_name=candidate.candidate_name,
            alias=candidate.alias,
            proof_tactic=candidate.harness.proof_tactic,
            start_line=start_line,
            tactic_start_line=start_line + 1,
            proof_end_line=len(generated) - 1,
            axiom_line=len(generated),
            end_line=len(generated),
        )
        cursor = candidate.insert_at
    return SourceNegationBatchHarness(
        source="\n".join(generated) + "\n",
        candidates=tuple(regions_by_index[index] for index in range(len(candidates))),
    )


def _scratch_messages(payload: Mapping[str, Any]) -> str:
    """Flatten checker output while retaining structured diagnostic text."""
    parts = [str(payload.get("error", "") or ""), str(payload.get("output", "") or "")]
    for message in payload.get("messages") or []:
        if isinstance(message, Mapping):
            parts.append(str(message.get("message", "") or ""))
        else:
            parts.append(str(message or ""))
    return "\n".join(part for part in parts if part)


def _structured_error_line(message: Mapping[str, Any]) -> int:
    """Return a one-based line from common structured diagnostic shapes."""
    candidates: list[object] = [
        message.get("line"),
        message.get("line_number"),
        message.get("startLine"),
        message.get("start_line"),
    ]
    for field in ("location", "position", "start"):
        nested = message.get(field)
        if isinstance(nested, Mapping):
            candidates.extend(
                (
                    nested.get("line"),
                    nested.get("line_number"),
                    nested.get("startLine"),
                    nested.get("start_line"),
                )
            )
    for raw in candidates:
        try:
            line = int(str(raw or 0))
        except (TypeError, ValueError):
            continue
        if line > 0:
            return line
    return 0


def _structured_error_path(message: Mapping[str, Any]) -> str:
    """Return a diagnostic path from common structured message shapes."""
    for raw in (message.get("file"), message.get("path"), message.get("uri")):
        value = str(raw or "").strip()
        if value:
            return os.path.normpath(value.removeprefix("file://"))
    for field in ("location", "position", "start"):
        nested = message.get(field)
        if not isinstance(nested, Mapping):
            continue
        for raw in (nested.get("file"), nested.get("path"), nested.get("uri")):
            value = str(raw or "").strip()
            if value:
                return os.path.normpath(value.removeprefix("file://"))
    return ""


def _error_locations(payload: Mapping[str, Any]) -> tuple[tuple[tuple[str, int], ...], bool]:
    """Return known Lean error paths/lines and whether any lacks a location."""
    located: list[tuple[str, int]] = []
    unlocated = False
    for raw_message in payload.get("messages") or []:
        if not isinstance(raw_message, Mapping):
            continue
        if str(raw_message.get("severity", "") or "").strip().lower() != "error":
            continue
        line = _structured_error_line(raw_message)
        path = _structured_error_path(raw_message)
        if line and path:
            located.append((path, line))
        else:
            unlocated = True
    location_re = re.compile(
        r"(?P<path>[^\n]*?\.lean):(?P<line>\d+):\d+:\s*" r"error(?:\[[^\]]+\]|\([^)]*\))?:",
        flags=re.IGNORECASE,
    )
    error_re = re.compile(
        r"\berror(?:\[[^\]]+\]|\([^)]*\))?:",
        flags=re.IGNORECASE,
    )
    for field in ("output", "error"):
        for raw_line in str(payload.get(field, "") or "").splitlines():
            if not error_re.search(raw_line):
                continue
            match = location_re.search(raw_line)
            if match is None:
                unlocated = True
            else:
                located.append(
                    (os.path.normpath(match.group("path").strip()), int(match.group("line")))
                )
    return tuple(located), unlocated


def _printed_axioms(text: str, declaration_name: str) -> tuple[str, ...] | None:
    """Return one unambiguous axiom profile for a possibly qualified alias."""
    suffix = re.escape(str(declaration_name or "").strip())
    if not suffix:
        return None
    name_pattern = rf"(?:[^']+\.)?{suffix}"
    profiles: set[tuple[str, ...]] = set()
    if re.search(rf"'{name_pattern}' does not depend on any axioms", text):
        profiles.add(())
    for match in re.finditer(
        rf"'{name_pattern}' depends on axioms: \[([^\]]*)\]",
        text,
    ):
        profiles.add(tuple(token.strip() for token in match.group(1).split(",") if token.strip()))
    if len(profiles) != 1:
        return None
    return next(iter(profiles))


def _uncertain_verdicts(
    harness: SourceNegationBatchHarness,
    *,
    reason: str,
    failure_kind: str,
) -> tuple[BatchCandidateVerdict, ...]:
    """Return one retryable scope-level uncertainty for every candidate."""
    return tuple(
        BatchCandidateVerdict(
            proof_declaration=candidate.proof_declaration,
            disposition=UNCERTAIN,
            reason=reason,
            failure_kind=failure_kind,
            retryable=True,
        )
        for candidate in harness.candidates
    )


def classify_batch_check(
    harness: SourceNegationBatchHarness,
    payload: Mapping[str, Any],
    *,
    allowed_axioms: Set[str],
) -> tuple[BatchCandidateVerdict, ...]:
    """Classify aliases conservatively after one exact source-prefix check.

    A compatible verdict is only scheduling evidence: the caller must rerun
    the existing single-candidate authoritative promotion gate before changing
    mathematical state. Definitive rejection requires all Lean errors to be
    located inside known alias ranges.
    """
    failure_kind = str(payload.get("failure_kind", "") or "").strip()
    if payload.get("retryable") or payload.get("output_truncated"):
        return _uncertain_verdicts(
            harness,
            reason="batched exact-source check was interrupted or incomplete",
            failure_kind=failure_kind or "source_batch_check_incomplete",
        )
    error_locations, unlocated = _error_locations(payload)
    raw_command = payload.get("command")
    command = (
        tuple(str(part or "").strip() for part in raw_command)
        if isinstance(raw_command, Sequence) and not isinstance(raw_command, (str, bytes))
        else ()
    )
    harness_path = os.path.normpath(command[-1]) if command and command[-1] else ""
    foreign_error = bool(
        error_locations
        and (not harness_path or any(path != harness_path for path, _line in error_locations))
    )
    error_lines = tuple(
        line for path, line in error_locations if harness_path and path == harness_path
    )
    known_ranges = tuple(
        (candidate.start_line, candidate.end_line) for candidate in harness.candidates
    )
    errors_outside_harness = any(
        not any(start <= line <= end for start, end in known_ranges) for line in error_lines
    )
    if unlocated or foreign_error or errors_outside_harness:
        return _uncertain_verdicts(
            harness,
            reason="batched exact-source check exposed non-candidate source uncertainty",
            failure_kind=failure_kind or "source_batch_scope_uncertain",
        )
    if not payload.get("success") and failure_kind != "lean_elaboration":
        return _uncertain_verdicts(
            harness,
            reason="batched exact-source check did not produce Lean elaboration evidence",
            failure_kind=failure_kind or "source_batch_check_incomplete",
        )
    if not payload.get("success") and not error_lines:
        return _uncertain_verdicts(
            harness,
            reason="batched exact-source failure had no attributable diagnostics",
            failure_kind=failure_kind or "source_batch_scope_uncertain",
        )

    text = _scratch_messages(payload)
    verdicts: list[BatchCandidateVerdict] = []
    for candidate in harness.candidates:
        header_errors = tuple(line for line in error_lines if line == candidate.start_line)
        proof_errors = tuple(
            line
            for line in error_lines
            if candidate.tactic_start_line <= line <= candidate.proof_end_line
        )
        axiom_errors = tuple(line for line in error_lines if line == candidate.axiom_line)
        axioms = _printed_axioms(text, candidate.alias)
        axiom_set = set(axioms or ())
        if header_errors:
            verdicts.append(
                BatchCandidateVerdict(
                    proof_declaration=candidate.proof_declaration,
                    disposition=UNCERTAIN,
                    reason="candidate harness header did not elaborate",
                    failure_kind="source_harness_header_uncertain",
                    retryable=True,
                )
            )
        elif proof_errors and axioms is not None and axiom_set <= set(allowed_axioms):
            verdicts.append(
                BatchCandidateVerdict(
                    proof_declaration=candidate.proof_declaration,
                    disposition=UNCERTAIN,
                    reason="candidate emitted conflicting proof-error and clean-axiom evidence",
                    failure_kind="source_batch_candidate_evidence_conflict",
                    retryable=True,
                )
            )
        elif proof_errors:
            # Lean may recover a failed theorem with ``sorryAx`` and still run
            # its following #print command. Located elaboration errors retain
            # the same definitive rejection authority as the single harness.
            verdicts.append(
                BatchCandidateVerdict(
                    proof_declaration=candidate.proof_declaration,
                    disposition=INCOMPATIBLE,
                    reason="candidate cannot elaborate in the exact target harness",
                    failure_kind="source_candidate_kernel_incompatible",
                )
            )
        elif axiom_errors:
            verdicts.append(
                BatchCandidateVerdict(
                    proof_declaration=candidate.proof_declaration,
                    disposition=UNCERTAIN,
                    reason="candidate axiom audit did not elaborate",
                    failure_kind="source_axiom_audit_unavailable",
                    retryable=True,
                )
            )
        elif axioms is None:
            verdicts.append(
                BatchCandidateVerdict(
                    proof_declaration=candidate.proof_declaration,
                    disposition=UNCERTAIN,
                    reason="candidate batch has no auditable axiom result",
                    failure_kind="source_axiom_audit_unavailable",
                    retryable=True,
                )
            )
        elif not axiom_set <= set(allowed_axioms):
            verdicts.append(
                BatchCandidateVerdict(
                    proof_declaration=candidate.proof_declaration,
                    disposition=INCOMPATIBLE,
                    reason="candidate depends on unknown or non-standard axioms",
                    failure_kind="source_candidate_axioms_unacceptable",
                    axioms=axioms,
                )
            )
        else:
            verdicts.append(
                BatchCandidateVerdict(
                    proof_declaration=candidate.proof_declaration,
                    disposition=COMPATIBLE,
                    reason="candidate elaborated in the batched exact target harness",
                    axioms=axioms,
                )
            )
    return tuple(verdicts)
