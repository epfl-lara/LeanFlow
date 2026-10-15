---
id: formalize
kind: workflow
title: Formalize
summary: Autonomous Lean formalization that drafts source-backed declarations and proof plans, verifies source fidelity, and hands the result to proving.
aliases: [autoformalize]
skills: [lean-formalization, lean-proof-loop]
tools: [formalization_document_inspect, lean_capabilities, lean_inspect, lean_search, lean_verify, lean_sorries, lean_axioms]
workers: []
review_actions: [item-review, coherence-review, correct-translation]
stop_conditions: [published, accepted_staged, blocked, interrupted]
route_actions: [prepare, representative-slice, complete-translation, publish, prover-handoff]
---

# Source-backed formalization

Inspect a TeX file, TeX project directory or PDF without model calls. Before `/formalize docs/paper.tex`, check the project baseline with `leanflow project setup` or **Set up project** in the VS Code Formalization tab. A current formalization preparation is also sufficient infrastructure. Proving is a separate explicit action after source review.

## Source and project setup

Intake freezes original files, include occurrences, macro context, source items, and a complete region inventory. A checked project baseline precedes native planning. A fresh read-only session estimates the required imports, existing declarations, missing dependencies, representation and representative slice. The controller validates this bounded preparation plan, expands approved dependencies privately and independently checks the project before recording its preparation receipt. Existing projects retain their source files and package pins. The controller allocates a document subtree under the checked library root, including `Main` when that is the actual library. Unsupported dynamic configuration is a visible blocker.

Read the complete manifest and source bundle. The inventory includes prose outside recognized environments; extraction is a starting point for audit, not evidence that nothing was missed. Unknown macros, missing includes and unreadable PDF pages stay visible. Request inventory repair for missing items or uncertain boundaries. Only the user may narrow mathematical scope. Preserved inactive source branches remain explicitly classified.

Ordinary intake can request an exact lexical interpretation for eligible frozen TeX diagnostics. Submit every entry in `source_reconciliation_requirements`; the proposal leaves original diagnostics unchanged and grants no source approval. Unknown execution effects, missing inputs and unsupported expansion remain blockers. Bounded contiguous review groups retain their complete ordered canonical members in `source_region_partition`. Cover all groups or all canonical regions exactly once, never a mixture. Grouping changes the review presentation, not the source denominator.

An explicit `--source-draft-only` assignment with a frozen paired PDF permits one preparatory author turn despite structured TeX execution uncertainty. Follow its narrower instructions and save only assigned draft files and allowlisted notes. Report source and mathematical blockers explicitly. It cannot submit accepted mappings, pass review, expand to the complete document or authorize publication and proving. Even a compiling draft remains unaccepted; the controller retains frozen preview artifacts after native process cleanup and does not reroll a completed campaign on resume.

## Drafting and submission

Plan shared definitions and imports before translating statements. Work one declaration at a time, then review the representation as a whole. Search the pinned libraries before duplicating existing declarations. Use LeanProbe for focused feedback and the controller for authoritative acceptance.

The first assignment admits only the representative source slice selected by the preparation plan: a shared definition, a dependent statement, a pinned library reuse or bridge, and a named proof obligation when applicable. Submit its structured proposal and representation choices. The controller compiles the slice and obtains fresh independent source review before authorizing a full-document assignment. The slice stays private. Follow the assigned phase and source IDs; full expansion cannot precede its passing receipt. Revise the setup when later translations expose a better representation; an early choice is not permanent approval.

Create only Lean files in the allocated document module tree, plus the assigned structured proposal file. Its aggregator must import every generated module and contain only imports, comments and whitespace. Record explanations in the proposal fields. The controller generates the portable blueprint and source artifacts after acceptance and owns the bounded root-import edit. Never edit the toolchain or dependency configuration as part of translation.

Definitions, instances, structures and constructions must be complete. Pending holes are allowed only in named propositional theorem proof slots; types and computational data must contain no holes. A construction may depend explicitly on a named pending existence theorem. Completed proofs, complete opaque definitions and exact library reuse are valid. Source conjectures retain their claim status independently of the LaTeX environment name.

Write `.leanflow-job/formalization-proposal.json` according to the assignment schema. Each translation unit records required source IDs, exact Lean declaration references, a representation rationale and unit dependencies. Splits, merges and justified helpers are supported. Coverage accounts for every selected document region, with a reason for each disposition. The controller checks mappings against the complete compiled declaration registry, including auxiliaries, and explicitly builds all generated modules plus the project.

Source repairs can register a known clause with `record_obligation`, exact frozen `source_refs`, an `item_id`, `description` and `reason`. Registration grants no approval or narrower scope. Every unit using that source item must include `clause_dispositions` for all its known obligations, naming the obligation ID, disposition, exact mapped declaration names and rationale. `represented` and `pending_proof` require explicit Lean representations; the latter must identify a real named Prop theorem proof slot with complete types and computational definitions. `omitted`, `specialized` and `unresolved` remain full-item blockers even when a representation note explains them. A statistical core cannot discharge a runtime requirement, a more general domain, or an ambiguous parenthetical by itself. The checklist records identified requirements; it does not claim to discover every mathematical clause automatically.

## Independent review and publication

Each coherent batch is reviewed in a fresh native session with unabridged required statements/proofs and read-only access to all frozen evidence. Every required source ID needs a verdict. A final coherence review checks every region and the complete setup. Reviewer backend/model/effort can differ from the author. Reviews share the run budget; the default is two correction rounds. Identical rejected evidence is not rerolled. Author-written approval prose has no authority.

For an explicitly paired TeX companion PDF, reviewers must retrieve every assigned physical page through `read_source_image` and compare it with the frozen source. Text metadata and a proposed correspondence are insufficient. The controller records delivered image hashes with verdicts and rejects acceptance when any required image is unavailable or unread. PDF-only sources still require their separate verified transcription gate.

The independent reviewer must assess each known clause against its exact `obligation:<id>` evidence and mapped Lean declarations. An omitted efficiency condition, narrowed domain or unresolved qualifier blocks the item. A named pending proof is acceptable only when its proposition faithfully represents that clause; data and type holes remain forbidden. Discovering another missing clause requires correction, even if every registered checklist entry passed.

Every source, scope, mapping, layout or code edit invalidates the selected revision's acceptance. Historical receipts remain immutable. A passing review is published with exact files, source evidence, blueprint and handoff under `formalization/<document-id>/`. Publication uses a durable transaction and refuses concurrent edits. Unsupported publication primitives leave the result accepted and staged, with a visible reason.

## Prover handoff

Launch `/prove --from-formalization <document-id> --formalization-revision <revision-id>` only from a published, current handoff. It selects exact files, named declarations and proof slots, never every project hole or only an aggregator. Zero obligations require no model call. Source proof notes remain available across prover turns. An independently checked proof-only preservation certificate can inherit source review; a changed statement, definition, import or environment requires review again.

The editor displays source coverage, elaboration, translation review and proof completion separately. Use its source/Lean links and local PDF viewer to repair the inventory and inspect each mapping. An extraction count alone never proves complete coverage.
