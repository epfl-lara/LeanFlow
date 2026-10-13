# Contributing to LeanFlow

Contributions should improve Lean proving, formalization, independent
verification, workflow visibility, or the CLI and editor experience around them.
Prioritize correctness, reliable continuation, inspectable state, and accurate
installation and documentation.

## Development setup

Use Python 3.12 or newer:

This development branch requires the matching LeanProbe 0.4.1 development
checkout beside LeanFlow as `../LeanProbe`. The uv lock uses that companion
source directly. For pip, install the companion before LeanFlow:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install ../LeanProbe
python -m pip install -e '.[dev]'
```

Activate the repository environment before Python commands. Run the complete
quality gate before committing:

```bash
black .
ruff check .
mypy
python -m pytest -q
```

Black formats Python; Ruff lints and sorts imports. New behavior requires tests.
Write characterization tests before refactoring coupled code, add extracted
modules to the mypy gate, and update documentation with behavior changes.
Extension changes also require its typecheck, tests, and package build:

```bash
cd vscode-extension
npm install
npm run typecheck
npm test
npm run package
```

## Code organization

Dependencies flow from `core/` through `agent/` and `tools/` to `leanflow_cli/`.
Keep modules focused and use existing subpackages. The native harness under
`agent/runtime/harness/` owns Codex, Claude Code, and Lemex process execution.
`leanflow_cli/workflows/prover/` and `leanflow_cli/native/harness_*` own
orchestration and independent acceptance. Native runtimes retain authentication,
model calls, retries, sampling, and compression.

Use skills for workflow guidance over existing tools. Add tools when deterministic
behavior is needed for verification, state, process lifecycle, or file ownership.
Preserve exact-source checks and meaningful completion gates.

LeanFlow's scope excludes messaging, editor servers, schedulers, browser/voice
products, broad batch-generation systems, and skill marketplaces. Avoid adding
those surfaces to the Lean kernel.

See [AGENTS.md](AGENTS.md) for coding standards and checks,
[ARCHITECTURE.md](ARCHITECTURE.md) for module responsibilities, and the
[documentation guide](docs/README.md) for operational references.
