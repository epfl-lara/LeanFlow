---
name: lean-project-search
description: Search the local Lean project for relevant files, lemmas, imports, and style before editing proofs.
---

# Lean Project Search

Use this skill when the next step depends on local project context rather than generic Mathlib knowledge.

## Procedure

1. Identify the active file and the target declaration before searching broadly.
2. Search the local project for related theorem names, namespaces, imports, notation, and helper lemmas.
3. Reuse nearby project conventions for names, proof style, and module boundaries.
4. If a missing fact appears reusable, prefer adding a local helper near the consuming theorem instead of scattering ad hoc lemmas.
5. Keep the search scoped to the requested file or dependency cone unless the workflow is project-wide.

## Search Targets

- Earlier theorems in the same file that solve a similar goal shape
- Existing helper lemmas in sibling modules
- Imports that already expose the needed API
- Project-specific notation, abbreviations, and wrapper definitions
- Previous proofs that demonstrate the expected tactic or term style

## Guardrails

- Do not drift into unrelated files once the relevant local pattern is found.
- Do not duplicate an existing project helper just because its name was not obvious at first glance.
- When the workflow is file-scoped, search outward only as far as needed to unblock that file.
