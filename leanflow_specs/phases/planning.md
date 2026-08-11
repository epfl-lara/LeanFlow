---
id: phase-planning
kind: phase
title: Planning Phase
summary: "The planner-synthesis contract: turn research deliverables into grounding facts, an ordered strategy, and graph nodes under the draft-phase stub rules."
consumed_by: [planner, orchestrator]
deliverable_schema:
  grounding: ["fact worth remembering, with its source"]
  strategy: ["ordered strategy step"]
  nodes:
    - name: "declaration or conjecture name"
      file: "repo-relative .lean path"
      statement: "COMPLETE sorry-bodied declaration, or omit when not yet formal"
      depends_on: ["other node names"]
      split_of: "parent goal name"
      notes: "one line"
---

# Planning Phase

Synthesis turns research lanes (search findings, mathlib candidates,
empirical probes) into a plan the graph can hold: grounding, strategy,
and nodes. The plan is advisory strategy — the kernel gate is the sole
authority on truth.

## Rules

- Aim for at most 8 nodes per synthesis (advisory); the graph door
  hard-truncates at 24 and journals what it dropped.
- A node with a complete formal statement enters as `stated` (and must
  obey the draft-phase stub contract); a node without one enters as
  `conjectured`. You never assign statuses — they are derived.
- Never restate the target goal as a helper node.
- Grounding lines are one-line facts WITH sources; strategy lines are
  ordered, concrete steps ("state X, then discharge Y via Z"), not
  aspirations.
- Contradictory lane evidence (an empirical `refutes` against a
  literature claim) belongs in grounding verbatim — flag it, do not
  resolve it silently.

## What the graph does with this

Nodes merge through the planner's graph door (derived statuses, immutable
existing state, validated edges); grounding and strategy land in
`summary.json` and render into plan.md's `## Grounding` and
`## Strategy`. Statements that pass the stub guards are stated into the
target file and become the next queue assignments.
