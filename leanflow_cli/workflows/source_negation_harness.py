"""Build exact Lean harnesses for source-backed negation promotion."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceNegationHarness:
    """Describe the proof identity and complete alias declaration to check."""

    proof_tactic: str
    declaration: str


def canonical_proof_tactic(candidate_name: str) -> str:
    """Build a direct-or-specialized counterexample bridge for one source lemma.

    Applying the candidate exposes the positive proposition it refutes.  The
    exact target hypothesis closes a direct negation; applying that hypothesis
    lets Lean infer any universally quantified arguments fixed by a specialized
    counterexample such as the target body at ``t = 0``.
    """
    return "\n".join(
        (
            "intro leanflowTarget",
            f"apply {candidate_name}",
            "first",
            "| exact leanflowTarget",
            "| apply leanflowTarget",
        )
    )


def build_source_negation_harness(
    *,
    alias: str,
    negation_prop: str,
    candidate_name: str,
    recorded_proof_tactic: str = "",
) -> SourceNegationHarness | None:
    """Build an exact alias while accepting only current or legacy proof identities.

    Fresh promotions use the canonical bridge.  Revalidation also accepts the
    historical ``exact <candidate>`` form so already-committed direct-negation
    evidence remains reproducible, but never executes an arbitrary stored tactic.
    """
    alias = str(alias or "").strip()
    negation_prop = str(negation_prop or "").strip()
    candidate_name = str(candidate_name or "").strip()
    if not alias or not negation_prop or not candidate_name:
        return None
    if any("\n" in value or "\r" in value for value in (alias, candidate_name)):
        return None

    canonical = canonical_proof_tactic(candidate_name)
    legacy = f"exact {candidate_name}"
    recorded = str(recorded_proof_tactic or "").strip()
    if recorded and recorded not in {canonical, legacy}:
        return None
    proof_tactic = recorded or canonical
    declaration = "\n".join(
        (
            f"theorem {alias} : \u00ac ({negation_prop}) := by",
            *(f"  {line}" for line in proof_tactic.splitlines()),
            f"#print axioms {alias}",
        )
    )
    return SourceNegationHarness(
        proof_tactic=proof_tactic,
        declaration=declaration,
    )
