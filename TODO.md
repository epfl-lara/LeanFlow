# EPFLemma TODO

## Product TODOs

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
