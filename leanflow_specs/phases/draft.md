---
id: phase-draft
kind: phase
title: Draft Phase
summary: "The sorry-stub contract: stub shape, placement, naming, and the graph-node emission schema every statement-drafting actor must follow."
consumed_by: [planner, decomposer, orchestrator]
deliverable_schema:
  stubs:
    - name: "declaration name (must equal the parsed name in the statement)"
      file: "repo-relative .lean path"
      statement: "COMPLETE sorry-bodied declaration"
      depends_on: ["other stub or node names"]
      split_of: "parent goal name when this stub decomposes it"
      notes: "why this statement, one line"
---

# Draft Phase

Every actor that states Lean declarations — the planner's synthesis, the
mechanical decomposer, multi-direction files — writes stubs under ONE
contract. The runner enforces it mechanically (`stub_shape_ok`); this
fragment is the prose the drafting model sees.

## Stub shape (enforced, not advisory)

- Exactly ONE `theorem` or `lemma` per stub, optionally `private` or
  attribute-annotated.
- The body is literally `by sorry` — one `:=`, one `sorry`, nothing else
  rides along. A second declaration smuggled into the text is rejected
  whole.
- The declaration name in the statement is the name of record: a claimed
  name that mismatches the parsed name rejects the stub (multi-direction
  files refuse the whole direction; planner placement skips the stub).
- Never restate the parent goal as a helper. Where the parent statement
  is known (the decomposer path) this is enforced mechanically
  (anti-sorry-offloading: ≥92% similarity rejects the stub); everywhere
  else it is your contract to honor.

## Placement

- Helpers for a target in the same file go immediately ABOVE the target
  (before its attributes and doc block) so they become the next queue
  assignments.
- Rival attack directions go into sibling files (`<Goal>_<dir>.lean`)
  carrying the goal file's import header; existing files are never
  overwritten. Sibling files are the multi-direction path's business —
  the planner defers statements aimed at other files to conjectures.

## Naming

- Lower snake_case, prefixed by the goal it serves
  (`goal_left_bound`, `goal_sum_split`); no apostrophes-only variants of
  existing names.

## Graph emission

Every stated stub must be reported in the deliverable so the dependency
graph learns it: `stated` status is DERIVED from the presence of the
statement — never claim `proved`, `false`, or `blocked` for a draft.
