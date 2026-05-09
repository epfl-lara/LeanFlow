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

- Design a sandboxed EPFLemma execution mode that works across environments, not only on macOS.
- Do not assume Docker is automatically the final answer; investigate the best isolation model first. Docker may be the most practical default, but the plan should compare options before committing.
- Evaluate:
  - Docker image with Lean, Lake, EPFLemma, tool servers, and Python runtime
  - devcontainer-compatible setup
  - Linux namespaces or other host-native sandboxing where available
  - macOS sandbox as an optional local backend, not the only design
  - remote/container runner backends for systems where local sandboxing is unavailable
  - per-run temporary worktrees with file-lock and checkpoint integration
- Requirements:
  - isolate arbitrary model/tool edits from the user environment
  - mount only the selected project/worktree
  - preserve logs, checkpoints, and final patch export
  - support reproducible Lean dependency cache
  - make failure modes visible in `epflemma status`

### Expert Help Providers

- Extend expert-help configuration beyond RPC/model providers.
- Add provider modes for command-based expert helpers:
  - Codex CLI
  - Claude Code
  - existing RPC/model providers
- Provide flags/config such as `--expert-provider codex`, `--expert-provider claude-code`, and provider-specific command templates.
- Capture expert prompt, command, exit status, response, and truncation metadata in workflow logs.
- Keep command execution opt-in and clearly sandboxed.

### Verification Model Configuration

- Make blueprint verification and autoformalizer verification separately configurable.
- Support the same provider classes as expert help:
  - model/RPC provider
  - Codex CLI command
  - Claude Code command
  - deterministic local verifier where possible
- Keep Lean kernel verification authoritative; model-based verification can only propose or review.
- Log which verifier was used for each blueprint/formalization decision.

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
