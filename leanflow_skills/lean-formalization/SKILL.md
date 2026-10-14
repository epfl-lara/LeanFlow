---
name: lean-formalization
description: Translate frozen mathematical source inventories into reviewed Lean declarations with exact prover handoffs.
---

# Lean formalization

Follow `leanflow_specs/workflows/formalize.md` and the controller's revision-bound assignment. Native CLIs own their model sessions; LeanFlow owns source authority and acceptance.

1. Audit all selected document regions and source items. Keep include/macro context, complete statements, trailing qualifiers and original proofs. Request source repair when extraction is uncertain.
2. Plan shared definitions, pinned library reuse and a useful file structure under the evaluated Lake library. First check a representative definition/statement/reuse/obligation slice and record its feedback in setup notes. Implement definitions and constructions fully; preserve exact mathematical meaning.
3. Use focused LeanProbe checks while drafting. Only named Prop proof slots may remain pending. Completed proofs and exact existing library declarations are valid; do not replace them with artificial holes.
4. Submit the structured translation units and complete region dispositions. Map exact fully qualified compiled names, justify split/merge/helper choices and expose conditional construction dependencies.
5. Repair independent per-item and whole-document review failures. Never approve your own translation, omit source material, or weaken statements to pass Lean.
6. Keep canonical files untouched until the controller publishes the reviewed revision. The generated portable blueprint and handoff are authoritative only with matching checks and review receipts.
7. Prove only the exact declaration/hole allowlist in the published handoff. Preserve reviewed types, definitions and imports; retain source proof guidance.

Source fidelity, elaboration and proof completion are separate statuses. Report blockers and staged publication honestly.
