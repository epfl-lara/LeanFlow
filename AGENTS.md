# LeanFlow agent guide

Instructions for coding agents working in this repository.

## Scope and architecture

LeanFlow is a Lean-first orchestration and verification kernel. Prioritize proof
repair, mathematical formalization, strict verification, clear status, logs,
checkpoints, and resume. Keep generic assistant product features outside this repo.

Dependencies flow in one direction:
`core/` → `agent/` and `tools/` → `leanflow_cli/`.
Never import a higher layer from a lower one. Keep one responsibility per module;
extract cohesive sibling leaves when a file mixes concerns or grows beyond a few
hundred lines. Use existing subpackages and update `ARCHITECTURE.md` when module
ownership or public interfaces change.

| Path | Responsibility |
| --- | --- |
| `core/` | Home authority, state, clock, constants, registry, toolsets, and utilities |
| `agent/runtime/harness/` | Native Codex, Claude Code, and Lemex execution |
| `agent/accounting/` | Shared usage, pricing, and redaction utilities |
| `tools/{implementations,utilities,mcp,environments}/` | Deterministic Lean tools and transport |
| `leanflow_cli/{cli,formalization,lean,native,runtime,workflows}/` | CLI, source intake, Lean services, and orchestration |
| `leanflow_skills/`, `leanflow_specs/` | Packaged Lean workflow guidance |

Top-level `model_tools`, `toolsets`, and `utils` are compatibility re-exports of
core modules. `run_agent.py`, `native_runner.py`, and removed direct-provider
entry points contain migration errors. Do not reintroduce SDK model loops or
routing. Native CLIs own authentication, model calls, retries, sampling,
compression, and native session state.

## Development environment and quality gate

Always activate the repository environment before Python commands:

```bash
source .venv/bin/activate
```

Run all four before every commit; CI enforces them:

```bash
black .
ruff check .
mypy
python -m pytest -q
```

Configuration lives in `pyproject.toml`. Black is the formatter; Ruff handles
linting and import sorting. Do not hand-format Python or use Ruff formatting.
Remove unused imports. An explicit `# noqa: F401` is allowed only for a genuine
public re-export or dynamic patch target with an actual caller. Include extracted
modules in the mypy `files` list.

The known xdist flake is
`tests/tools/test_mcp_tool.py::...::test_existing_tool_names_reflect_registered_subset`.
It passes in isolation with `-n 0`; ignore only that parallel flake and investigate
all other failures.

## Implementation standards

Every module starts with a responsibility docstring. Non-trivial functions need
typed signatures and concise imperative docstrings describing behavior, side
effects, or the non-obvious reason. Avoid parameter-by-parameter boilerplate and
comments that restate code. Explain why where needed.

New behavior requires tests. Before refactoring coupled code, add characterization
tests that pin existing behavior. See `tests/agent/test_native_harness.py` and
`tests/leanflow/test_retained_native_helpers.py`. Move tests with extracted helpers
and patch names where the function resolves them; patching a former re-export
does not affect a sibling's bound import.

Use `core.home.leanflow_home()` for every user-state path. Do not introduce another
home resolver. LeanFlow orchestration uses `LEANFLOW_*`; the launcher supplies
`LEANFLOW_NATIVE_*` and `LEANFLOW_PROJECT_ROOT`. Preserve native homes and
credential variables; do not translate or copy native authentication.

Controller-owned independent Lean acceptance determines proof completion.
Preserve source protection, exact target types, axiom policy, transaction recovery,
job capabilities, and run-bound evidence. Model reports and activity are not
verification results.

Use skills for guidance on the existing tool surface. Add tools only for
necessary deterministic behavior such as verification, file ownership, state,
or native process lifecycle.

## Documentation

Keep behavior changes aligned across these sources:

- `README.md`: installation and first workflows.
- `docs/`: operational settings, proving, sandbox, storage, and evaluation guides.
- `ARCHITECTURE.md`: module map, native contracts, and acceptance invariants.
- `CONTRIBUTING.md`: development setup and required checks.
- Module/function docstrings: primary behavior reference.
- `vscode-extension/README.md` and `CHANGELOG.md`: editor setup and user-visible changes.

Write general instructions without personal paths, temporary experiment diaries,
or unsupported provider assumptions. Keep examples valid against the shipped CLI.

## Excluded product surfaces

Do not reintroduce messaging gateways, ACP/editor servers, cron or schedulers,
browser/voice products, batch data-generation subsystems, skill marketplaces, or
mini-swe-agent and its Docker/Modal terminal backends. Supported terminal backends
are local, SSH, Singularity, and Daytona; `leanflow sandbox` provides isolation.

## Focused checks

```bash
source .venv/bin/activate
python -m pytest tests/leanflow -q -n 0
python -m pytest tests/leanflow tests/agent/test_prompt_builder.py tests/agent/test_native_harness.py -q -n 0
python -m leanflow_cli.main --help
```

For workflow UX, runner, or installation changes, also smoke-test installation and
the installed wrapper:

```bash
./scripts/install-internal.sh
~/.local/bin/leanflow --help
```

An editable installation still references its checkout. Keep active runtime
source stable while changing or installing another checkout. Extension changes
require `npm run typecheck`, `npm test`, and `npm run package` in its directory.
