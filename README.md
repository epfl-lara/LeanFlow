# LeanFlow

LeanFlow automates Lean 4 proof repair and mathematical formalization. It works
inside a Lean project, checks proof candidates independently, and records the
plan, progress, logs, and usage so you can inspect or resume a run.

- **Prove** fills existing `sorry` holes while preserving supplied statements.
- **Research** develops a mathematical plan and coordinates concurrent provers
  through a theorem dependency graph.
- **Formalize** turns LaTeX or PDF sources into a buildable Lean statement draft
  for review and subsequent proving.
- **Observe** runs from the terminal or the [VS Code extension](vscode-extension/README.md).
- **Isolate** a workflow in an optional container and review its exported patch.

## Install

Use Linux, macOS, or WSL with Git and Python 3.12 or newer. Lean workflows also
require the project's Lean toolchain and Lake, normally managed by `elan`.
Codex, Claude Code, or Lemex provides the native agent runtime.
On Linux and WSL, native proving requires Bubblewrap and usable user namespaces.

```bash
git clone https://github.com/epfl-lara/LeanFlow.git
cd LeanFlow
./scripts/install-internal.sh
```

This coordinated development branch requires the companion LeanProbe 0.4.1
development build. Place its matching checkout beside LeanFlow, build its wheel,
and supply it to the installer:

```bash
python3 -m pip wheel --no-deps ../LeanProbe --wheel-dir ../lean-probe-wheels
PIP_FIND_LINKS="$PWD/../lean-probe-wheels" ./scripts/install-internal.sh
```

On Windows, run `.\scripts\install.ps1` from PowerShell to install inside WSL2.
Continue with project and provider setup in that WSL environment. For Remote SSH,
install LeanFlow and the native runtime on the remote workspace host.

The installer prepares a versioned runtime, managed Lean backends, and wrappers
in `~/.local/bin`. Add that directory to your shell's `PATH` if needed:

```bash
export PATH="$HOME/.local/bin:$PATH"
leanflow --help
leanflow doctor
```

`doctor` reports missing tools and unavailable capabilities, including local
search and PDF extraction. It checks native runtime availability without making
an inference request. See the [installation reference](docs/product-reference.md#install)
for custom paths and optional tools.

To update, pull the desired revision and rerun the installer. A validated runtime
is selected for future commands; active processes retain their environment.
The default installation is editable: keep its source checkout stable while
jobs run, or use `--no-editable` to install a wheel independent of checkout edits.
See [storage and cleanup](docs/storage.md) for runtime and dependency retention.
Local semantic search is optional: `--local-leanexplore` installs its additional
machine-learning dependencies and downloads its search data.

## Quick start

Prepare a project and configure the native runtime before starting a workflow:

```bash
codex login
leanflow config set model.provider codex

cd /path/to/project-folder
leanflow project setup
leanflow workflow prove Main.lean
```

`project setup` creates a Mathlib project in an empty folder, prepares a folder
containing Lean files, or checks an existing Lake project while preserving its
source, toolchain, and dependency pins. It checks the Lean verification protocol
before reporting readiness. Use `--preset lean` for a minimal project or
`--repair` to recheck dependencies and rebuild verification tools.

The VS Code sidebar provides the same flow through **Set up project**, with
progress, logs, retry, and cancellation. Provider login is separate from Lean
project readiness.

Choose the scope and workflow:

```bash
leanflow workflow prove                         # discover project proof holes
leanflow workflow prove Main.lean               # research mode, up to two provers
leanflow workflow prove Main.lean --research-workers 3
leanflow workflow prove Main.lean --clean-room  # project and installed libraries only
leanflow workflow formalize docs/paper.tex      # draft source-linked statements
leanflow status                                # inspect progress from another terminal
```

Run `leanflow` for the interactive shell:

```text
/prove Main.lean
/formalize docs/paper.tex
/workflow status
/workflow activity
/workflow log 120
/goals
/diagnostics
/provider
/doctor
/exit
```

## Verification and source protection

Provers work in private scratch files. The controller installs a candidate only
after independent Lean checks accept it and its axiom profile. Supplied source
outside authorized proof holes stays protected; reviewed helper modules,
imports, and library requirements are recorded as additional changes.
Definition holes require explicit authorization.

Before each prover or negation session starts, LeanFlow checks LeanProbe in that
job's actual private project and OS sandbox, including its assigned imports.
A setup failure stops model launch and saves the checker diagnostics.

LeanProbe directly owns proof feedback, declaration parsing, and REPL transport.
LeanFlow adds project resource limits and controller acceptance. Coordinated
development installations require the companion LeanProbe development wheel.

Foreground verification retains warm proof-search workers by default. Low-memory
and background-worker modes reclaim them; `LEANFLOW_LEAN_WARM_RETENTION=reclaim`
also selects reclamation explicitly. See [storage and compilation reuse](docs/storage.md)
for the opt-in cache of local imports in fresh private workspaces.

A successful `prove` run requires no `sorry` in the requested source files and
a final project `lake build`. A file target limits the proof assignment to that
file; omitting the target discovers eligible project source files. A blocked or
limited run saves its state and reports an unresolved or failed outcome.

Research is the default, with bottom-up scheduling and two concurrent prover
jobs. Standard mode uses one prover directly. Top-down scheduling is also
available; candidates depending on unfinished helpers remain untrusted until
those dependencies close and strict verification passes.

Clean-room proving supports all three native backends. It restricts research to
the project and installed libraries, blocks external research and remote Lean
search, and excludes prior native session context. Native model connections
remain available. Clean-room checkpoints can resume; online or legacy runs
cannot be converted into clean-room tests.

`formalize` has a separate completion gate: a buildable statement draft reviewed
against its source, with intentional theorem proof holes. Review the draft, then
run `prove` to complete it. `autoprove` and `autoformalize` are compatibility
aliases of `prove` and `formalize`.

See the [prover workflow](docs/prover-workflow.md) for settings, verification,
limits, outcomes, and resume behavior.

## Native backends and configuration

LeanFlow supports **Codex**, **Claude Code**, and **Lemex**. Each CLI owns its
login, credentials, endpoint, model calls, retries, sampling, and compression.
Configure the native CLI first, then select it in LeanFlow:

```bash
leanflow provider
leanflow workflow prove Main.lean --provider codex
leanflow workflow prove Main.lean --provider claude
leanflow workflow prove Main.lean --provider lemex
```

Leave model and reasoning effort empty to inherit native defaults. `--model`
and role settings are optional explicit overrides. Lemex inherits its native
`LEMEX_BASE_URL` and `LEMEX_API_KEY`; external inference endpoints are configured
there. The [backend reference](docs/product-reference.md#native-backends) covers
setup and migration from the former direct-provider engine. Rerunning the
LeanFlow installer adopts a compatible locally updated Lemex. To refresh only
LeanFlow’s private Lemex copy, use:

```bash
python -m leanflow_cli.runtime.lemex_install --refresh --no-build
```

Run this from an activated LeanFlow installation or development environment.
Older immutable copies remain available to existing jobs. The VS Code activity
view shows public messages, tool progress and reasoning when the native runtime
reports it; providers may expose summaries, traces, or no reasoning.

New proof runs default to **250 total agent calls**, with caps of **200 prover
calls** and **50 orchestration calls**, plus **24 hours of active time**. Configure
global limits on calls, reported tokens, and active time:

```bash
leanflow config set limits.agent_calls 250
leanflow config set limits.tokens 1000000
leanflow config set limits.wall_time_s 86400
```

One native CLI invocation is one agent call, including a new retry or resume
process. Its internal model requests and compaction share that call. Rejected
submissions return feedback within that session; a new attempt carries forward
the saved partial proof, notes, and report. Research uses the role caps across
the entire run; clear a limit to make it unlimited. Token limits gate later
invocations and may be exceeded by an active call. These are whole-job limits,
rather than per-agent
model-request budgets. Limits and spending survive resume.

User configuration lives in `~/.leanflow/config.yaml`; project registration and
workflow state live under `.leanflow/`. Set `LEANFLOW_HOME` to choose another
user-state directory. Inspect configuration and recorded state with:

```bash
leanflow flags list
leanflow workflow --dry-run --json prove Main.lean
leanflow runs list --json
```

## Container sandbox

With Docker or Podman available, install the sandbox image and wrapper:

```bash
./scripts/install-sandbox.sh
cd /path/to/lean-project
leanflow-sandbox workflow prove Main.lean
```

The sandbox works on a copied project and exports `changes.patch` under
`~/.leanflow/sandbox/runs/<run-id>/`. Review the patch before applying it.
See the [sandbox runtime](docs/sandbox-runtime.md) for network, credentials,
cache mounts, and update options.

## Documentation and development

- [Documentation guide](docs/README.md) — operational references.
- [Product reference](docs/product-reference.md) — workflows, projects, backends, settings, and diagnostics.
- [Prover workflow](docs/prover-workflow.md) — source protection, limits, progress, and resume.
- [VS Code extension](vscode-extension/README.md) — setup, launching, proof workspace, and experiments.
- [Contributing](CONTRIBUTING.md) — development setup and required checks.
- [Architecture](ARCHITECTURE.md) — module ownership and runtime invariants.
- [Agent guide](AGENTS.md) — repository coding standards.

LeanFlow also includes `draft`, `review`, `refactor`, and `golf` workflows.
Its curated guidance lives in `leanflow_skills/`; workflow specifications live in
`leanflow_specs/`. Built-in examples and evaluation fixtures are described in
[testdata/workflow_projects](testdata/workflow_projects/README.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
