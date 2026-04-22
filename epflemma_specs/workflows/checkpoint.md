---
id: checkpoint
kind: workflow
title: Checkpoint
summary: Save a workflow milestone only after explicit verification and auxiliary sanity checks such as axiom inspection.
skills: [lean-diagnostics]
tools: [lean_capabilities, lean_inspect, lean_verify, lean_axioms]
stop_conditions: [checkpoint-written, blocked]
route_actions: [diagnostics]
---

# Native Checkpoint Spec

Checkpoint after the active scope is explicitly verified and the current queue state is persisted.

## When To Use

Use this workflow when a meaningful proving or formalization milestone has been reached and you want a safe save point with explicit verification.

Typical uses:

- after clearing a group of queue items
- before switching tactics or workflows
- before ending a long autonomous session

## What Not To Use This Workflow For

Do not use this workflow for:

- unfinished proof repair where the scope still fails verification
- read-only review
- strategy refactoring or golfing
- pushing to remote or opening a PR

Checkpoint is a save-and-verify workflow, not a repair workflow.

## Tool Order

1. `lean_capabilities`
   - confirm the available verification and helper surface
2. `lean_inspect`
   - capture the current queue state, diagnostics, and active file status
3. `lean_verify`
   - verify touched scope first, then the broader requested scope
4. `lean_axioms`
   - run a best-effort axiom sanity check when the milestone includes freshly proved declarations

## Checkpoint Policy

Before writing a checkpoint:

- the touched Lean scope should verify explicitly
- the current queue state should be coherent enough to resume from
- remaining `sorry` and axiom status should be reported honestly

Do not create a checkpoint that hides a failing verification gate.

## Verification Ladder

1. verify touched file(s) or module(s)
2. verify project scope when the workflow milestone claims project-level progress
3. record current `sorry` count and axiom status alongside the checkpoint summary

## Output Contract

A checkpoint result should state:

- what scope was verified
- what verification gates passed
- current `sorry` count
- axiom status
- what the next workflow should do

## Stop Conditions

Stop when:

- a checkpoint-ready milestone has been verified and recorded
- or the workflow is blocked because verification did not pass

## Handoff Format

If the checkpoint cannot be completed, report:

- failing verification gate
- file/module/project scope involved
- remaining blocker
- whether the right next step is `prove`, `formalize`, or `review`
