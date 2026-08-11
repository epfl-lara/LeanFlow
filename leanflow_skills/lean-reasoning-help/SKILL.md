---
name: lean-reasoning-help
description: Auxiliary proof-strategy help for hard Lean theorem repairs. Use when repeated focused attempts fail and another configured model or command expert should advise without editing files or changing existing statements.
---

# Lean Reasoning Help

Use this skill for hard theorem-local blockers after normal proof workflow steps have produced useful context but no working proof.

## Contract

1. Preserve the assigned theorem/lemma/example statement exactly.
2. Call `lean_reasoning_help` with the theorem id, file path, current diagnostics, current attempt, and recent failed attempts.
3. Treat the result as advice only; do not accept it until a concrete edit passes `lean_incremental_check(check_target)` for the assigned declaration, or `lean_verify(mode=file_exact)` when doing a final Lake sweep or explicit canonical check.
4. Do not use auxiliary advice to justify deleting, weakening, renaming, moving, or replacing the declaration with `sorry`.
5. If the advice suggests a statement change, report that as a blocker instead of applying it. A blocker or accurate open-problem assessment is route-change evidence only: request a distinct route, job, portfolio refresh, or fresh epoch and continue; advisor prose cannot make it terminal.
6. If the advice suggests `sorry`, `admit`, axioms, unsafe code, or another placeholder, ignore that part and continue with verified proof repair.
7. Helper lemmas or private supporting declarations are acceptable advice when they preserve existing statements and directly support the assigned theorem.
8. If the advisor is unavailable, returns no answer, gives irrelevant advice, or recommends stopping, continue the main Lean workflow from the strongest verified local evidence; missing or surrendering advice is not evidence that the theorem statement is wrong or mathematically resolved.

## Configuration

`lean_reasoning_help` routes through the shared auxiliary client task `lean_reasoning` by default. When explicitly configured with `codex` or `claude-code`, it instead invokes the corresponding command expert in an advisory mode.

Configure it with either:

- `auxiliary.lean_reasoning.provider`
- `auxiliary.lean_reasoning.model`
- `auxiliary.lean_reasoning.base_url`
- `auxiliary.lean_reasoning.api_key`
- `auxiliary.lean_reasoning.command_template`
- `auxiliary.lean_reasoning.codex_command_template`
- `auxiliary.lean_reasoning.claude_code_command_template`

or environment overrides:

- `AUXILIARY_LEAN_REASONING_PROVIDER`
- `AUXILIARY_LEAN_REASONING_MODEL`
- `AUXILIARY_LEAN_REASONING_BASE_URL`
- `AUXILIARY_LEAN_REASONING_API_KEY`
- `AUXILIARY_LEAN_REASONING_COMMAND_TEMPLATE`
- `LEANFLOW_EXPERT_CODEX_COMMAND_TEMPLATE`
- `LEANFLOW_EXPERT_CLAUDE_CODE_COMMAND_TEMPLATE`

Workflow launches can also pass `--expert-provider codex`, `--expert-provider claude-code`, and `--expert-command-template ...`. Command experts receive the advisor prompt on stdin and must still be treated as unverified advice.
