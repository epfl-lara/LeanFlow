---
id: phase-review
kind: phase
title: Review Phase
summary: "Read-only verdict phase with ONE action vocabulary aligned to the orchestrator route enum and a PASS/BLOCK JSON contract."
consumed_by: [orchestrator, prover]
deliverable_schema:
  decision: "PASS | BLOCK — the key the review parser consumes"
  action: "continue | decompose | plan | negate | re-state | park"
  reason: "one paragraph grounded in the evidence"
  evidence: ["file:line or check output backing the decision"]
---

# Review Phase

A review classifies and recommends; it never edits. Its action vocabulary
is exactly one list, aligned to the orchestrator's routes (`continue` is
the reviewer's word for the `direct-prove` route); the router consumes
the action as a SUGGESTION — advice never outranks the deterministic
floor or the kernel gate.

## Action vocabulary (the only one)

- `continue` — the current path is still viable; keep proving.
- `decompose` — the goal needs stated helper lemmas before more attempts.
- `plan` — the scope needs research/strategy before more prover budget.
- `negate` — the statement smells false; a feasibility probe is due.
- `re-state` — the declaration shape is the blocker (sub-lemmas only;
  main-statement changes need a human ACK).
- `park` — statement fidelity or required human approval prevents safe autonomous work;
  pause with a complete decision packet. Difficulty and exhausted routes are not parking reasons.

Do not invent labels outside this list; `deep`, `repair`, `redraft`,
`golf`, `replan`, `falsify`, and `stop` are retired vocabulary.

## Verdict contract

- Reply with ONE JSON object; `decision` is `PASS` or `BLOCK` (the key
  the existing review parser reads — `status`/`result` are accepted
  synonyms, and a bare `PASS`/`BLOCK` first line still parses for legacy
  text replies).
- `BLOCK` always carries an `action` from the vocabulary plus the
  evidence lines that justify it — a block without a route request is a
  protocol violation.
- Reviews run read-only: inspection and search tools only, no write or
  patch tools. Findings are advisory; the kernel gate remains the sole
  acceptance authority.
