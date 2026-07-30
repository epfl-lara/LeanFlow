---
id: refactor
kind: workflow
title: Refactor
summary: Lean proof refactoring with structure-preserving simplification, search-backed helper reuse, and explicit verification after each meaningful edit.
skills: [lean-refactor-golf]
tools: [lean_capabilities, lean_inspect, lean_search, lean_multi_attempt, lean_verify]
workers: []
stop_conditions: [verified, blocked]
route_actions: [refactor]
---

# Native Refactor Spec

Refactor keeps theorem meaning fixed and focuses on reusable local improvements.

## When To Use

Use this workflow when proofs already compile and the goal is strategy-level simplification:

- replacing hand-rolled arguments with existing library structure
- extracting reusable helper lemmas
- simplifying case splits or proof architecture
- improving local proof organization without changing theorem meaning

Refactor is broader than `golf`: it is allowed to improve proof structure, helper layout, and reuse, not only shorten tactics.

## What Not To Use This Workflow For

Do not use this workflow for:

- files that still contain active compiler blockers or `sorry`
- declaration redrafting or theorem-statement changes
- read-only review
- pure brevity cleanup once the proof shape is already good

Use `prove` first when the target does not compile, `review` for read-only auditing, and `golf` for already-good proofs that only need directness or brevity cleanup.

## Tool Order

1. `lean_capabilities`
   - check whether semantic search and worker support are available before planning a larger refactor
2. `lean_inspect`
   - confirm the target proof compiles and identify nearby diagnostics or warnings that would make refactoring unsafe
3. `lean_search`
   - search for existing Mathlib/project lemmas, reusable APIs, or better proof shapes before rewriting by hand
4. `lean_multi_attempt`
   - use only when the refactor has collapsed into 2-6 concrete local simplification candidates at one proof position
   - do not use it for theorem-sized proof blocks, statement changes, or speculative rewrites
5. apply one coherent refactor batch
   - prefer one proof or one local helper cluster at a time
   - keep theorem meaning and declaration headers fixed
6. `lean_verify`
   - verify after each meaningful batch
## Refactor Policy

Prefer:

- existing library lemmas over hand-rolled proof chains
- extracted helpers when the same pattern repeats
- simpler proof structure that future proving passes can reuse
- local private helpers before broad public API changes

Avoid:

- semantic changes to theorem statements
- new axioms
- public interface changes unless the user explicitly requested them
- broad multi-file rewrites unless the workflow explicitly widened scope
- “refactors” that only hide complexity behind heavier automation

## Safety Rules

- start only from compiling proofs
- revert the current batch if verification gets worse
- preserve theorem meaning and declaration headers
- keep diffs reproducible and scoped to the requested file or proof cluster

## Verification Ladder

1. `lean_inspect` after each meaningful change
2. `lean_verify(mode=file_exact)` for file-scoped proof batches
3. `lean_verify(mode=module)` or `lean_verify(mode=project)` when the refactor touches broader scope

Do not accept a refactor on aesthetics alone. The resulting proof must still verify cleanly.

## Stop Conditions

Stop when:

- the targeted refactor goal is verified
- the next change would become a semantic rewrite rather than a refactor
- the proof is blocked and should return to `prove`

## Handoff Format

If the refactor remains unfinished, record:

- proof or helper cluster touched
- structural improvement already applied
- remaining risk or next structural step
- verification gate most recently passed
