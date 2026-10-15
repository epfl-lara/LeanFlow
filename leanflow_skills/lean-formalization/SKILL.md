---
name: lean-formalization
description: Translate frozen mathematical source inventories into reviewed Lean declarations with exact prover handoffs.
---

# Lean formalization

Follow `leanflow_specs/workflows/formalize.md` and the controller's revision-bound assignment. Native CLIs own their model sessions; LeanFlow owns source authority and acceptance.

1. Audit all selected document regions and source items. Keep include/macro context, complete statements, trailing qualifiers and original proofs. Request source repair when extraction is uncertain.
2. Follow the checked preparation plan for dependencies, shared definitions, library reuse and file structure. In the first assignment, translate only the representative definition/statement/reuse/obligation slice. Submit its proposal and wait for controller compilation and fresh independent review before full expansion. Implement definitions and constructions fully; preserve exact mathematical meaning.
3. Use focused LeanProbe checks while drafting. Only named Prop proof slots may remain pending. Completed proofs and exact existing library declarations are valid; do not replace them with artificial holes.
4. Write only the assigned Lean modules and proposal JSON; the controller generates the blueprint and source artifacts after acceptance. Submit structured translation units and complete assigned region dispositions. Map exact fully qualified compiled names, justify split/merge/helper choices and expose conditional construction dependencies.
5. Repair independent per-item and whole-document review failures. Never approve your own translation, omit source material, or weaken statements to pass Lean.
6. Keep canonical files untouched until the controller publishes the reviewed revision. The generated portable blueprint and handoff are authoritative only with matching checks and review receipts.
7. Prove only the exact declaration/hole allowlist in the published handoff. Preserve reviewed types, definitions and imports; retain source proof guidance.

Source fidelity, elaboration and proof completion are separate statuses. Report blockers and staged publication honestly.

Honor every explicitly registered source `known_obligations` entry. Each unit using that source includes `clause_dispositions` with the exact obligation ID, disposition, mapped declaration names and rationale. `represented` and `pending_proof` require faithful formal statements; pending proof means an actual named Prop proof slot with complete data and types. Omitted efficiency, specialized domains and unresolved qualifiers block full-item acceptance even when disclosed in a representation note. The independent reviewer must return clause-level evidence and verdicts against frozen `obligation:<id>` source spans. Request `record_obligation` source repair for an identified missing requirement; registration does not grant approval or authorize a narrower scope. Keep auditing the complete source: this explicit checklist does not discover every clause automatically.

Follow exact `source_reconciliation_requirements` when ordinary intake requests a lexical source reading. Keep all candidates and original uncertainty; proposed readings require fresh complete coverage and coherence review. Review groups preserve an exact ordered canonical partition in `source_region_partition`; cover every group or every canonical region once, without mixing IDs. Independent review of paired TeX/PDF evidence requires actual `read_source_image` delivery for every assigned companion page. Image metadata and extracted text alone cannot pass that gate.
