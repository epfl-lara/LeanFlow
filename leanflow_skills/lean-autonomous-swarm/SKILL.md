---
name: lean-autonomous-swarm
description: Run a user-approved Lean swarm with clear file ownership, verifier roles, and strict zero-sorry verification.
---

# Lean Autonomous Swarm

Use this skill only when the user explicitly enabled multi-agent execution for the workflow.

## Objective

Drive the workflow until the target Lean code:

1. compiles successfully,
2. has clean diagnostics,
3. has no open goals, and
4. contains no `sorry`.

## Delegation Rules

1. Delegate only when the subtask is concrete and bounded.
2. Give each child a clear role:
   - file-specific prover
   - lemma extractor
   - verifier / build checker
3. Do not let two child agents edit the same file concurrently.
4. Acquire a file lock before editing a shared Lean file.
5. If a file is locked by another agent, choose a different task or wait.

## Verification Rules

1. Keep one path focused on final verification.
2. Do not declare success from partial progress.
3. If the swarm stalls, summarize the blocker precisely instead of hiding it.
