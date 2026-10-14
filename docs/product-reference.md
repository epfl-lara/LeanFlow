# LeanFlow product reference

LeanFlow coordinates Lean proof repair, source formalization, and independent
verification through Codex, Claude Code, and Lemex. The CLI, interactive shell,
and VS Code extension share the same workflow launcher.

## Install

The installer supports Linux, macOS, and WSL. It requires Git and Python 3.12 or
newer with `venv` and `pip`. Lean projects require their configured Lean toolchain
and Lake, normally managed by `elan`. Fresh runtime construction uses a default
10 GiB free-space reserve; configure `storage.min_free_gib` to change it. An
unchanged validated runtime can be reused without new-runtime admission.
On Ubuntu/Debian, Python also needs `python3-venv` (or matching `python3.X-venv`);
a supported private Python can be selected with `--python` when administrator
access is unavailable.

```bash
git clone https://github.com/epfl-lara/LeanFlow.git
cd LeanFlow
./scripts/install-internal.sh
export PATH="$HOME/.local/bin:$PATH"
leanflow --help
leanflow doctor
```

On Windows, run `.\scripts\install.cmd` from PowerShell. Use `-Distro NAME` to
select a WSL2 distribution or `-LinuxRepoDir PATH` for its Linux checkout.
The launcher selects an initialized WSL2 distribution, preserves an explicitly
selected distribution, and stops with initialization guidance when needed.
Automatic selection requires a normal Linux user; use `-Distro NAME` explicitly
when an installation as root is intentional.
`-NonInteractive` prevents elevation and password prompts; `-WithLemex` and
`-LeanProbeSource PATH` forward the corresponding Linux options. It preserves
the existing Git origin and refuses to switch a checkout with private or
divergent commits. A requested unpublished branch fails explicitly; an automatic
branch fallback announces that main was selected.
Finish any requested WSL initialization, then configure the native runtime and
Lean project inside that environment. Remote SSH workspaces need LeanFlow and
its prerequisites on the remote host.

| Default location | Contents |
| --- | --- |
| `~/.local/bin/` | CLI wrapper |
| `~/.leanflow/runtimes/<identity>/venv/` | Versioned Python environment |
| `~/.leanflow/mcp/` | Managed Lean backends |
| `~/.leanflow/` | User configuration and managed state |

The default package installation is editable: keep the source checkout in place
and stable while its workflows run. Versioned environments protect interpreters
and dependencies from in-place replacement; they do not freeze editable source.
Use `--no-editable` for a wheel installation when the runtime must be independent
of checkout edits. Wheels build from fresh per-runtime source inputs, so old
checkout build folders cannot reintroduce modules removed by an update.

The installer prepares managed Lean MCP backends. It uses a matching adjacent
`../LeanProbe` checkout, an explicit `--lean-probe-source PATH`, or downloads the
exact reviewed LeanProbe commit into managed state. This supports the development
backend before its Python package release. Git and package downloads need network
access; local companion source avoids the Git download. Its packaged contents are
part of the runtime identity and are rechecked before commands switch.

Codex is the default provider. Codex and Claude Code use their own installations.
A compatible self-contained Lemex installation can be adopted automatically.
A fresh pinned Lemex source build runs only when saved configuration selects
Lemex or `--with-lemex` is supplied. That build additionally requires Node.js,
npm, ripgrep, Bash, Python, a C compiler, and Rust (or curl to install Rust),
network access and build time. Native authentication and configuration are
preserved. The installer does not make model calls. Local search uses `rg`; PDF inspection uses Poppler's `pdftotext`,
`pdfinfo`, and `pdfimages`. Missing optional capabilities are reported.
Linux and WSL also require Bubblewrap with usable user namespaces for native
proving. The installer attempts to prepare it and checks whether its sandbox can
start, including when all prerequisite executables are already installed.
The installer prefers the system `/usr/bin/bwrap` over a user-local copy so
host AppArmor rules apply to the intended executable.

Rerunning the installer adopts a compatible local Lemex upgrade. To refresh just
the managed Lemex package, run
`python -m leanflow_cli.runtime.lemex_install --refresh --no-build` from an
activated LeanFlow environment. Refresh validates and snapshots the package,
then switches the managed pointer atomically. It preserves native configuration,
authentication and older immutable copies. Without a compatible local package,
`--no-build` keeps the existing healthy runtime. Omit it to allow a pinned source
build when necessary.

On restricted Ubuntu hosts, installing `bubblewrap` alone may not provide the
AppArmor policy required to create a sandbox. Ask an administrator to review
and load the [Bubblewrap profile recommended by Ubuntu](https://discourse.ubuntu.com/t/understanding-apparmor-user-namespace-restriction/58007),
using a version compatible with the host's AppArmor package. Run `leanflow doctor`
afterwards to check that the sandbox actually starts. The application profile
permits Bubblewrap's namespaces while retaining the host's global restrictions.

Local LeanExplore semantic search is optional. Use `--local-leanexplore` to
install its machine-learning dependencies and fetch its data. The default
installation uses local text search and available hosted/MCP search providers.

```bash
./scripts/install-internal.sh --help
./scripts/install-internal.sh --no-editable
```

Custom home, wrapper directory, interpreter, and fresh environment paths are
available through `--leanflow-home`, `--bin-dir`, `--python`, and `--venv-dir`.
Installation paths may contain spaces. Python build tooling does not support
dollar signs in the state/runtime paths, so the installer rejects those paths
before changing the installation. Source and wrapper paths can contain dollar
signs. Line breaks in installation paths are rejected.
An existing explicit environment path is rejected. `--recreate-venv` prepares a
fresh sibling, preserving the previous environment. `--non-interactive` disables package and Git prompts; it does not install a
provider CLI or log in to one. Use `--skip-os-tools` or
`--skip-leanexplore-data` to manage those optional components yourself.

Managed MCP repair builds fresh permanent environments, checks actual protocol
initialization and tools, then atomically selects their commands. Package specs,
patch implementations, installed metadata, validated arguments and patch/command
contents are fingerprinted; a mismatch prepares a new generation on upgrade.
Custom MCP arguments and symlinked configuration targets are preserved. Existing
processes retain their previous environments. Configuration updates share a
process lock and preserve unrelated settings. Failed candidates are cleaned
only when their subprocess shutdown is confirmed; uncertain shutdown retains
the candidate for inspection. Managed MCP generations and pinned companion
sources currently remain outside automatic storage collection.

To update, pull the desired revision and rerun the installer. It validates the
new environment before selecting it for future commands. Keep any source
checkout referenced by an active editable installation stable until its jobs
finish. [Storage and cleanup](storage.md) explains runtime retention.

## Project setup

```bash
cd /path/to/project-folder
leanflow project setup
leanflow project show
leanflow project inspect --json
```

Setup creates a Mathlib project in an empty folder, adopts existing Lean sources,
or prepares an existing Lake project. Existing source, toolchain, and dependency
pins are preserved. New projects use pinned compatible dependencies.
`--preset lean` selects a minimal Lean project. `project init` is an alias for
setup; `project create PATH` prepares a new folder.

Readiness requires the real Lean verification protocol to work. An unchanged,
verified project reuses its readiness receipt. `project setup --repair` rechecks
downloads and rebuilds verification tools. Literal `sorry` holes are valid
starting points; setup does not claim their proofs are complete.

Ordinary Lake configurations receive a missing REPL requirement automatically;
custom executable configurations receive manual instructions. Setup refuses to
change a project with an active workflow. Cancellation stops only setup-owned
processes. VS Code provides the same flow with progress, logs, retry, and cancel.
Provider authentication is a separate step.

| Project path | Purpose |
| --- | --- |
| `.leanflow/project.yaml` | Registration and project settings |
| `.leanflow/workflow-state/` | Status, logs, checkpoints, and outcomes |
| `.leanflow/native-workflows/` | Non-prover native sessions and proposals |
| `.leanflow/skills/` | Project skill overlays and formalization guidance |
| `.leanflow/flag-profiles/` | Saved launch profiles |

Durable guidance can be attached to matching targets and files:

```yaml
workflow_guidance:
  - path: proof-guidance.md
    targets: [result]
    active_files: [Algebra/Main.lean]
```

Guidance paths must remain inside the project. Matching guidance is reattached
when the workflow reconstructs its assignment.

## Native backends

Select exactly `codex`, `claude`, or `lemex`. Each native CLI owns authentication,
credentials, endpoints, model requests, retries, sampling, and compression.
Configure it first, then inspect or select the backend in LeanFlow:

```bash
codex login
leanflow config set model.provider codex
leanflow provider
leanflow workflow prove Main.lean --provider codex
```

| Backend | Native authentication and configuration |
| --- | --- |
| `codex` | Codex login or standard `OPENAI_API_KEY` |
| `claude` | Claude Code login or `ANTHROPIC_API_KEY` |
| `lemex` | Lemex configuration, including `LEMEX_BASE_URL` and `LEMEX_API_KEY` |

Export native variables in the launch environment or configure the native CLI.
LeanFlow's dotenv loader does not import provider credentials or translate them.
The VS Code Providers page can store explicit keys in SecretStorage for new
extension-launched runs. Removing a saved override restores inherited settings.
A presence check does not validate paid API access.

Leave model and effort empty to inherit native defaults. `--model`,
`LEANFLOW_PROVER_MODEL`, and role-specific backend/model/effort settings are
optional overrides. A different role backend inherits its own native defaults
unless explicitly configured. The editor reads model and reasoning capabilities
from the runtime rather than maintaining a separate fixed catalog.

Run activity records model and effort evidence for each native invocation.
Requested overrides, reported session settings, response model names, and native
model defaults remain distinguishable. An unreported historical setting remains
unknown; it is not filled from today's configuration.

### Migration from the direct-provider engine

The native configuration uses `schema_version: 2`. Old settings are preserved
and rejected with migration guidance. Back up the config, remove direct
provider endpoints/keys, sampling/compression controls, and per-request limits,
then choose a native backend. Clear unintended model and effort overrides.
Configure external inference endpoints through Lemex.

Direct SDK routes, local-server management, advisor command templates, and
`leanflow-agent` execution are retired. Historical artifacts remain readable;
start a new native run instead of resuming a direct-provider conversation.

## Workflows

| Workflow | Result |
| --- | --- |
| `prove` | Independently accepted replacements for existing proof holes |
| `formalize` | A buildable statement draft reviewed against its TeX/PDF source |
| `draft` | Lean declarations and proof skeletons |
| `review` | Findings about diagnostics, goals, and remaining holes |
| `refactor` / `golf` | Verified Lean proof simplification |

`autoprove` and `autoformalize` are aliases of `prove` and `formalize`.

```bash
leanflow workflow prove Main.lean
leanflow workflow prove Main.lean --research-workers 3
leanflow workflow formalize docs/paper.tex
leanflow workflow review Main.lean --provider claude
leanflow workflow --dry-run --json prove Main.lean
```

Common options include `--provider`, `--model`, `--prompt`, `--additional-skill`,
and `--axioms`. A dry run resolves the launch without starting model work.

Research proving defaults to bottom-up order and two concurrent prover jobs.
It adds fresh outline, graph-construction, and review sessions. Standard mode
runs one prover directly. `--research-workers N` sets research concurrency;
`--agents N` is a compatibility spelling. Other workflows use one native agent,
with a separate source reviewer when required.

A file target limits the proof assignment to that file. An empty target discovers
eligible project sources. Definition holes require
`LEANFLOW_PROVER_FILL_DEFINITIONS=1`. Provers edit private scratch files; only the
controller integrates independently accepted changes. Completion requires no
`sorry` in the requested source files and a final project `lake build`.
The [prover reference](prover-workflow.md) details these boundaries.

`formalize` accepts a project-local `.tex`, `.pdf`, or TeX-project directory.
Intake freezes complete source regions and allocates an evaluated Lake module tree
without editing canonical Lean files. The author submits exact translation units;
independent per-item and whole-document reviews follow compiled declaration checks.
Publication preserves sources, mappings, verdicts and an exact proof-slot handoff:

```bash
leanflow workflow formalize docs/paper.tex
leanflow workflow prove --from-formalization <document-id> --formalization-revision <revision-id>
```

Formalization completion does not certify pending proofs. Missing PDF text or source
assets block review until repaired. See [formalization](formalization.md) for inventory
repairs, parser policy, reviewer settings and publication recovery. `review` publishes findings;
`refactor` and `golf` require independent build, axiom, and statement checks before
source promotion.

## Limits and accounting

User settings live at `$LEANFLOW_HOME/config.yaml`, normally
`~/.leanflow/config.yaml`. Empty model and effort values preserve native defaults:

```yaml
schema_version: 2
model:
  provider: codex
  default: ""
agent:
  reasoning_effort: ""
limits:
  agent_calls: 250
  tokens: null
  wall_time_s: 86400
```

The global limits cover every role and all resumes. Missing settings default to
250 calls and 24 active hours. An explicit `null` or empty environment value means
unlimited; zero means exhausted. Environment overrides are
`LEANFLOW_PROVER_AGENT_CALLS`, `LEANFLOW_PROVER_TOKENS`, and
`LEANFLOW_PROVER_WALL_TIME_S`. They also apply to non-prover workflows.

One native process invocation is one agent call. Internal model requests,
retries, tool steps, and compaction remain inside that call. A new process for
continuation or recovery consumes another call. Admission is reserved before
spawn; interrupted started processes keep their charge.

Active time includes tools, queues, and verification across parallel jobs.
Offline time between stopped controllers is excluded. Reported tokens gate
future invocations; a running invocation can overshoot. Missing usage remains
unknown and prevents another token-capped invocation. LeanFlow has no default
per-response deadline or custom context-compression policy.

Research also has run-wide role caps: 200 prover calls and 50 orchestrator calls by
default. Planning, review, research, and recovery share the orchestrator cap.
Explicitly clear a role limit for unlimited. The [prover settings](prover-workflow.md#settings-and-limits)
cover role caps, scheduling, recovery, and graph bounds.

Inspect settings and save comparable profiles:

```bash
leanflow config show
leanflow flags list
leanflow flags show LEANFLOW_PROVER_AGENT_CALLS
leanflow flags effective --changed
leanflow flags profiles
leanflow flags diff default research
```

Profiles live under project or user `.leanflow/flag-profiles/`. The catalog
records defaults and which controls are safe for editor profiles. Sensitive
storage, raw-request, and authority settings remain terminal-only.

## State and inspection

```bash
leanflow status
leanflow runs list --json
leanflow runs prover RUN_ID --json
leanflow runs events RUN_ID
leanflow runs log RUN_ID
leanflow runs metrics RUN_ID --json
leanflow runs stop RUN_ID
```

`status` gives a bounded live summary; `status --verbose` adds larger history
and a live sandbox-engine check. Exact-run observers retain the selected run's
identity. `runs stop` revalidates process ownership before interruption.
Missing or damaged terminal evidence is reported as unknown or unscored.

Prover plans, graphs, protected baselines, job logs, and transactions live under
`.leanflow/workflow-state/prover/<run-id>/`. Resume with
`LEANFLOW_PROVER_RESUME_RUN_ID` and the same target. Other native workflows use
`.leanflow/native-workflows/<run-id>/` and `LEANFLOW_NATIVE_RESUME_RUN_ID`.
Resume retains configuration, source checks, spending, and lineage.

Metrics bind activity and final outcomes to the exact run. Launch/final records
include source, toolchain, dependencies, runtime, selected guidance, and settings.
Missing token or cost coverage stays unavailable. Digests detect accidental
corruption and mismatched evidence; they are not signatures against someone who
can rewrite the state directory.

## Skills and storage

Built-in guidance lives in `leanflow_skills/`; canonical workflow specifications
live in `leanflow_specs/`. Project overrides in `.leanflow/skills/` take precedence
over user overrides in `~/.leanflow/skills/`, then built-in skills. Each skill is a
directory containing `SKILL.md`. `/skills` lists available guidance; an
additional skill can be selected for a launch.

Compatible projects reuse sealed dependency entries through private writable
filesystem clones. `leanflow storage status` inspects managed usage;
`leanflow storage clean` removes only owned, unused entries. The default soft
cache target is 20 GiB with a 10 GiB free-space reserve. Active processes, proofs,
results, checkpoints, and logs are protected. Low space can stop new work with a
resumable `storage_pressure` outcome. See [storage.md](storage.md).

For container execution, see [sandbox-runtime.md](sandbox-runtime.md). Development
setup and required checks are in [CONTRIBUTING.md](../CONTRIBUTING.md).
