---
name: lean-bounded-prover
description: Complete an assigned Lean sorry in a private scratch job under a fixed provider-call budget.
---

# Bounded prover

The controller supplies the exact assignment, PLAN and DAG. Write a short
informal proof sketch in `PLAN_job.md`. Work directly in the assigned scratch
file, using warm LeanProbe for incremental checks and focused library search.
After a search, use the evidence in an actual proof attempt. Do not request an
advisor, start a recursive prover workflow, or reset a budget.

Keep original declarations and statements fixed. Only the controller can
integrate source edits, update PLAN.md, or revise DAG.json. Submit the exact text
replacing the authorized literal `sorry` in `candidate.txt` or the requested JSON
report. Definitions can be filled only when explicitly included in the assignment.

Decompose concretely when needed: write local `have` statements and discharge
their obligations in the same scratch job and budget. Do not return an informal
decomposition as if it were a proof. Report a necessary DAG change to the controller.

Use only the supplied acyclic dependencies. Never import the assigned theorem
and use its existing sorry proof, add an axiom, change a signature, or weaken
the target. A candidate relying on unfinished planned dependencies remains
unverified until the controller independently checks dependency closure and
the complete allowed-axiom profile.

Persist failed approaches and current useful progress in the proof notes.
Context compaction preserves those notes and the original contract; it does
not grant calls. Keep trying concrete approaches while budget remains. At
exhaustion, the runtime writes a report without an additional model call.
