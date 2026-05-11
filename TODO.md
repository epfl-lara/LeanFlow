# EPFLemma TODO

## Product TODOs

### Autoformalization Inputs

Implemented:

- `/autoformalize` accepts project-local TeX directories while preserving single `.tex` and `.pdf` inputs.
- Directory preflight deterministically selects a main TeX entrypoint, records included `.tex` files, bibliography files, local assets, and rejects ambiguous roots with an explicit error.
- Workflow state, launch summaries, and runner startup prompts distinguish the original request from the selected source document and keep the generated blueprint path visible.

### User Prompt Overrides

Implemented:

- `/prove` and `/autoformalize` accept `--prompt`; legacy `--goal` remains a compatibility alias for the same scoped guidance field.
- The effective prompt is threaded through workflow resolution, child runner environment, startup prompt assembly, live status, activity logs, and run metadata.
- Prompt guidance is appended as scoped user guidance and does not replace the Lean-first workflow/skill contracts.

### Sandboxed Runtime

Implemented:

- Added `epflemma sandbox build/status/doctor/run` with Docker/Podman auto-detection. Linux prefers usable rootless Podman when present and falls back to Docker when appropriate.
- Added `containers/epflemma-sandbox.Containerfile` with Python, Lean via elan, Lake, Git, ripgrep, EPFLemma's MCP runtime, and managed MCP bootstrap-on-first-run support.
- Added `--with-local-lean-explore` for sandbox image builds when users want `lean-explore[local]` and its embedding stack baked into the image.
- Added per-run copied EPFLemma project worktrees under `~/.epflemma/sandbox/runs/<run-id>/worktree`; the original project is not mounted into the container by default.
- Added baseline Git commits and exported `changes.patch`, `git-status.txt`, and `status.json` artifacts for each sandbox run.
- Added persistent sandbox cache/home mounts for Lean, Lake, pip/XDG, and managed MCP backends while keeping arbitrary model edits confined to sandbox-owned directories.
- Added `scripts/install-sandbox.sh`, `scripts/update-sandbox.sh`, and an `epflemma-sandbox` wrapper for install and upgrade/reinstall flows.
- Added top-level `epflemma status` reporting for sandbox readiness alongside workflow state.
- Documented the comparison with devcontainers, native namespace/macOS sandboxing, and direct bind mounts in `docs/sandbox-runtime.md`.

### Expert Help Providers

Implemented:

- Extended `lean_reasoning_help` beyond RPC/model providers while keeping existing model/RPC routing as the default.
- Added opt-in command expert providers for Codex CLI and Claude Code via `--expert-provider codex` / `--expert-provider claude-code`, `AUXILIARY_LEAN_REASONING_PROVIDER`, or `auxiliary.lean_reasoning.provider`.
- Added provider-specific command templates through `--expert-command-template`, `AUXILIARY_LEAN_REASONING_COMMAND_TEMPLATE`, `EPFLEMMA_EXPERT_CODEX_COMMAND_TEMPLATE`, `EPFLEMMA_EXPERT_CLAUDE_CODE_COMMAND_TEMPLATE`, and config keys under `auxiliary.lean_reasoning`.
- Captured expert prompt, command, exit status, response, and truncation metadata in workflow activity logs.
- Kept command execution explicit, no-shell, stdin-driven, and defaulted to read-only/planning-oriented CLI modes.

### Verification Model Configuration

- Make blueprint verification and autoformalizer verification separately configurable.
- Support the same provider classes as expert help:
  - model/RPC provider
  - Codex CLI command
  - Claude Code command
  - deterministic local verifier where possible
- Keep Lean kernel verification authoritative; model-based verification can only propose or review.
- Log which verifier was used for each blueprint/formalization decision.

Low priority:

- Investigate Codex CLI / Claude Code as primary workflow model adapters. Do not rush this into the main runner: it needs an explicit bridge for tool calls, streaming, budget accounting, logs, and Lean-kernel orchestration instead of treating command output as a drop-in API model.

### Refactoring Plan

- Split workflow parsing, runtime execution, provider dispatch, and workflow-state persistence into smaller modules.
- Keep Lean-specific behavior explicit; avoid growing generic assistant abstractions.
- Prioritize:
  - `epflemma_cli/main.py`: CLI surface and option parsing only
  - `epflemma_cli/workflow.py`: request resolution and plan construction only
  - `epflemma_cli/native_runner.py`: runtime loop and tool orchestration only
  - `epflemma_cli/workflow_state.py`: persistence API and schema migrations only
  - provider modules: expert/help/verification provider dispatch
- Add narrow tests around each extracted boundary before large rewrites.

## Additional Suggestions

- Add `.epflemma/` or at least `.epflemma/workflow-state/` to `.gitignore` if workflow state is intended to be local generated data.
- Add a command such as `epflemma status --json` that distinguishes live, stale, and completed workflow state.
- Add a "commit report" command that summarizes changed Lean declarations, verification commands, warnings, and remaining `sorry`.
- Add a first-class "phase summary" artifact for `/autoformalize` runs: source, theorem target, generated Lean file, verified declarations, warnings, and unresolved tasks.
- Add regression tests for directory autoformalization and prompt override routing once those features are implemented.
- Add a policy for checked-in demo/test Lean projects that intentionally keep `sorry`, separate from strict no-`sorry` workflow targets.
