---
id: formalize
kind: workflow
title: Formalize
summary: Autonomous Lean formalization that drafts source-backed declarations and proof plans, verifies source fidelity, and hands the result to proving.
aliases: [autoformalize]
skills: [lean-formalization, lean-proof-loop, lean-theorem-queue-worker]
tools: [formalization_document_inspect, lean_capabilities, lean_inspect, lean_search, lean_verify, lean_sorries, lean_axioms]
workers: []
review_actions: [continue, decompose, plan, negate, re-state, park]
stop_conditions: [verified, blocked, interrupted, stalled]
route_actions: [queue-worker, final-sweep]
---

# Native Formalize Spec

Use `/formalize` or `/autoformalize` for the same autonomous workflow.

## When To Use

Use this workflow when the input is a project-local mathematical source document that must be turned into source-backed Lean declarations, theorem skeletons, and prover-ready proof notes.

Typical inputs:

- a LaTeX source document inside the project, for example `docs/paper.tex`
- a TeX project directory inside the project, for example `docs/paper`
- a PDF source document inside the project, for example `docs/paper.pdf`
- a partially drafted document-backed Lean file that still needs formalization and proof completion

## What Not To Use This Workflow For

Do not use this workflow for:

- one-off informal theorem strings with no source document
- proof-only repair where the statement shape is already stable
- read-only review
- save-point work (persisted checkpoints are automatic in managed runs)
- pure tactic shortening after the declarations already compile

Use `prove`, `review`, or `golf` for those cases. Use `draft` when the task is limited to declaration skeletons or signatures with no expectation of completing the proving loop.

## Document Input Contract

`/formalize` and `/autoformalize` require a project-local `.tex` source, `.pdf` source, or directory containing a TeX project.

The workflow resolver prepares:

- a source-document preflight manifest
- a Markdown planner blueprint next to the generated Lean files
- a bounded extracted-text cache
- an active Lean target file for the generated declarations
- startup context that points to all of the above

For directory inputs, the resolver deterministically selects the main `.tex` entrypoint, records included `.tex` files, bibliography files, and local assets, and fails ambiguous roots with a clear error before launch.

The active Lean target file is only the entry point. By default a document gets its own project-local workspace such as `ProjectName/PaperName/Main.lean` plus `ProjectName/PaperName/Blueprint.md`, and the planner may split work into additional Lean files in that same directory when the blueprint justifies it. Keep imports and blueprint references coherent.

## Tool Order

1. `formalization_document_inspect`
   - inspect the required project-local source document
   - for LaTeX, use extracted theorem-like environments, labels, references, citations, and sections as the starting inventory
   - for PDF, use the extracted text and PDF metadata; if extraction is degraded, record that explicitly before planning
2. `lean_capabilities`
   - read the actual available diagnostics, search, and worker surface first
3. `lean_inspect`
   - inspect the target file, existing declarations, queue items, and blocker kind
   - use this before inserting more declarations into a broken file
4. `lean_search`
   - search local project facts, imports, and Mathlib before inventing names or structures
   - use it both for statement design and proof search
5. create or update the planner blueprint
   - list source statements, dependencies, planned Lean names, split lemmas, statement-fidelity checks, and proof notes
   - include natural-language source proof strategy useful to the prover: relevant paper paragraphs, induction variables, reductions, important intermediate facts, and likely Mathlib dependencies
   - rely on the generated supplemental blueprint skill to keep the `Blueprint.md` path available across prover turns and compaction
   - if a `blueprint/` directory or `leanblueprint` setup exists, keep a compatible TeX blueprint in sync where practical
   - replace the preflight `_pending_` entries before drafting Lean; the initial blueprint is only an inventory placeholder
6. draft or revise declarations in small verifiable steps
   - prefer one declaration or one local helper at a time
   - keep imports and dependencies minimal and explicit
   - in the planner draft, theorem/lemma/example proofs must remain `by sorry`; do not start proof repair inside `/formalize`
   - every generated Lean file must start with all `import` commands before any module doc comment, file overview, namespace, or declaration
7. `lean_verify`
   - use the narrowest truthful verification gate for the current step
8. `lean_sorries` / `lean_axioms`
   - use when remaining `sorry` inventory or axiom profile is the real blocker

## Formalization Policy

Formalization is not complete when the declaration header merely parses. The formalizer should move from informal intent to:

- declarations with stable names and signatures
- compilable Lean code
- source locators and proof/prover notes that tell the prover exactly what to read
- theorem/lemma/example skeletons with `sorry`
- a statement/source verification request before proving begins

Prefer:

- explicit intermediate lemmas over brittle monolithic tactics
- local project naming and import patterns over fresh ad hoc style
- small draft-and-verify increments over large speculative file rewrites
- stable source pointers: section, label, page, equation, or bibliography reference
- a blueprint dependency plan before deep proof work
- natural-language proof/prover notes in the blueprint plus compact source-aware comments above declarations
- imports first in every generated Lean file; do not put `/-! ... -/` module docs above imports

Avoid:

- drifting into unrelated helper files unless the workflow explicitly widened scope
- repeated header rewrites when the blocker is actually proof search or compilation
- declaring victory after only drafting statements without requesting statement/source verification
- silent theorem weakening or strengthening relative to the source document
- doing proof repair during the formalization planner draft; leave `sorry` and let the prover queue work one declaration at a time after review approval

## Statement Fidelity

The planner blueprint should record:

- the natural-language mathematical statement or definition from the document
- the source pointer, such as theorem label, section, page, or equation number
- relevant dependency/proof notes from the source document or reconstructed proof plan
- any intentional scope change, generalization, specialization, or assumption added for Lean
- a statement-fidelity review comparing the planned Lean type to the source claim
- `Source qualifiers`: mathematical object class, quantifier order, parameter domain, output codomain, equality/image condition, side conditions, and follow-on claims that are part of the source statement
- `Lean coverage`: the Lean declarations or theorem clauses that cover each source qualifier
- `Scope changes`: `none`, or an explicit list of intentional weakenings, strengthenings, omissions, or representation changes

Draft readiness is checked with `lean_inspect`, `lean_verify`, and the document formalization handoff verifier. Do not use terminal Lake commands as the normal way to decide whether the formalization draft is ready.

Before moving from planning to proving, the runner starts a fresh independent statement/source verification pass when the draft is otherwise ready and only approval statuses are missing. The review must check that each Lean statement matches the source claim, correct the blueprint or Lean draft when it does not, and record `Statement verification status: approved` for each source theorem/lemma entry before the prover queue starts. Each source theorem/lemma doc comment should include compact proof notes; the generated supplemental blueprint skill carries the durable `Blueprint.md` reference for prover turns after compaction. If the document statement is ambiguous, record the ambiguity in the blueprint rather than hiding it in the Lean signature. The prover phase may and should reread both `Blueprint.md` and the source `.tex`/`.pdf` when the proof needs the paper's argument.

The verifier must treat source qualifiers as theorem-statement content, not proof commentary. Every explicit qualifier must be covered in Lean, covered by a companion declaration, or recorded as an intentional scope change. If the source includes a parameter-domain conversion, representation bridge, or follow-on equivalence, either formalize that bridge as its own declaration or leave the entry unapproved with the omission recorded.

Object-class qualifiers need particular care. A theorem about a richer source representation is not fully covered by a simpler Lean encoding with matching output values unless the bridge is explicit. Either add a definition or companion declaration that witnesses the representation bridge, or mark `Lean coverage` as partial and list the representation change under `Scope changes`.

The preflight blueprint is not a completed plan. Update it with planned Lean declaration names, dependencies, split lemmas, statement-fidelity reviews, and proof/prover notes before writing the main Lean draft.

Do not place generated module-level documentation before imports. It is acceptable, and usually preferred, to omit generated Lean documentation entirely and keep planning prose in the blueprint.

## Header Stability And Redraft

Headers should stay stable once they are good enough to support focused proving work.

Redraft is justified when:

- the statement is ill-typed
- the dependencies are wrong
- the generated shape clearly blocks the proof
- the workflow or a `re-state` route decision explicitly calls for a redraft

Redraft is not the default answer to an ordinary proof blocker. If the statement already expresses the intended theorem, prefer proof repair over header churn.

## Verification Ladder

Formalization uses the same verification ladder as proving:

1. `lean_inspect` for per-edit diagnostics and goals
2. `lean_verify(mode=file_exact)` for file-scoped theorem acceptance
3. `lean_verify(mode=module)` for focused milestone checks
4. `lean_verify(mode=project)` before declaring project-scoped formalization complete

Formalizer completion requires:

- the formalized declarations compile
- source locators and proof notes are recorded in the blueprint
- theorem/lemma/example proofs remain as `sorry`
- the statement/source verification gate has been requested or completed

Proving completion is handled by `/prove` after the review-approved handoff.

## Blocker Taxonomy

- statement-design blocker
  - wrong dependencies, malformed signature, or missing imports
  - route: `re-state` — redraft in small steps, then re-inspect
- compiler-style blocker
  - route: direct local fix, then richer local feedback if repeated
- search blocker
  - route: `lean_search` before changing the theorem shape again
- axiom-risk blocker
  - route: `lean_axioms`, then direct proof cleanup if necessary
- stuck formalization queue item
  - route: document the blocker and hand off to the proving loop with source-backed notes

## Stop Conditions

Stop only when:

- the requested formalization scope is verified
- a concrete blocker has been recorded and the next action is a handoff or redraft, not another speculative proof attempt
- the workflow was interrupted
- progress is stalled and the route decision already identifies the correct next step

## Handoff Format

When handing off unfinished formalization work, include:

- declarations added or revised
- declaration still blocked
- current blocker kind
- verification gate most recently passed
- search already attempted
- whether a redraft or worker escalation is recommended
