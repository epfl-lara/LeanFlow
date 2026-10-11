# LeanFlow product reference

LeanFlow coordinates Lean proof repair, source formalization and independent
verification through three native agent runtimes: Codex, Claude Code and Lemex.
The CLI, interactive shell and VS Code extension use the same workflow launcher.

## Installation and configuration

Run `./scripts/install-internal.sh`, then `leanflow --help` and `leanflow doctor`.
The installer prepares LeanFlow and a managed pinned Lemex distribution. An
existing self-contained Lemex package is adopted without replacing its global
executable; otherwise the installer builds a pinned source revision. Codex and
Claude use their own installations. Each runtime keeps its own login, API-key
setup, home, model configuration and session state.

LeanFlow configuration lives at `$LEANFLOW_HOME/config.yaml` (normally
`~/.leanflow/config.yaml`). The native schema is:

```yaml
schema_version: 2
model:
  provider: codex
  default: ""
agent:
  reasoning_effort: ""
limits:
  agent_calls: null
  tokens: null
  wall_time_s: null
```

Empty model/effort values inherit the selected runtime's defaults. LeanFlow
never sets temperature, top-p, a context window, output-token limits or a
compression policy. Configure those in the native runtime when desired.

| Backend | Native setup |
| --- | --- |
| `codex` | Codex login or standard `OPENAI_API_KEY`, official OpenAI endpoint |
| `claude` | Claude Code login or `ANTHROPIC_API_KEY` |
| `lemex` | Native Lemex settings and `LEMEX_BASE_URL` / `LEMEX_API_KEY` |

```bash
export LEMEX_BASE_URL="https://inference.rcp.epfl.ch/v1"
export LEMEX_API_KEY="..."
leanflow workflow prove Main.lean --provider lemex
```

Export native variables in the environment used to launch LeanFlow, or configure
the native CLI. LeanFlow's dotenv loader does not import provider credentials or
rewrite native settings. General tool credentials may still use LeanFlow dotenv.
`leanflow provider` describes selection without reading secrets; `doctor` checks
availability without a paid authentication probe.

## Migration from the direct provider engine

Old configurations are preserved and rejected with guidance. Back up the config,
remove direct endpoints/keys, custom/local provider definitions, sampling,
compression and per-turn limits, and set `schema_version: 2`. Set `model.provider`
to `codex`, `claude` or `lemex`. Remove accidental `model.default` and
`agent.reasoning_effort` overrides to inherit native defaults.

Names such as `rcp`, `custom`, `openrouter`, `local` and `openai-codex` are no
longer backends. External endpoints belong to Lemex. Advisor command templates,
`leanflow models local` and the `leanflow-agent` command are retired. Existing
artifacts remain readable; old provider conversations cannot resume as native
sessions. New native sessions persist their runtime session ID.

## Workflows

```bash
leanflow project init
leanflow workflow prove Main.lean
leanflow workflow prove Main.lean --research --research-workers 3
leanflow workflow formalize paper.tex
leanflow workflow review Main.lean --provider claude
```

- `prove` / `autoprove` fill authorized holes, preserving original statements.
  Standard mode uses one prover. Research mode adds fresh outline, planning,
  review and resource sessions and concurrent jobs on a theorem dependency graph.
- `formalize` / `autoformalize` extract TeX/PDF inputs and produce elaborating
  statement drafts plus a source-linked `Blueprint.md`. Definitions must be
  complete; theorem proof holes may remain. A fresh native source reviewer checks
  fidelity before promotion. Run `prove` separately to finish the proofs.
- `draft` creates elaborating Lean statement skeletons.
- `review` reports findings without publishing source changes.
- `refactor` / `golf` propose Lean edits subject to independent build, axiom and
  protected-statement checks. Existing non-proof definitions remain fixed.

Use `--provider` and `--model` for launch-specific overrides and `--prompt` for
additional instructions. Removed advisor/provider flags fail with an explanation.
`--agents` controls prover concurrency; other native workflows use one agent.
Inspect a launch without starting it using
`leanflow workflow --dry-run --json prove Main.lean`.

## Execution and acceptance

The selected native CLI owns its conversation, coding tools, retries and
compression. LeanFlow provides the assignment and a job-specific MCP bridge for
Lean checks, research and submissions. Long controller operations return an ID;
`poll_operation` retrieves results without restarting or cancelling work. Native
shell tools retain their own runtime behavior; the bridge's `start_command`
supports long jobs without a LeanFlow per-command deadline.

Native tools run in filtered private project copies with an enforced filesystem
boundary. Controller state and canonical sources are outside job authority.
Independent verification freezes proposed source bytes and uses canonical
dependencies, never an agent-modified dependency cache. Only controller-accepted
bytes can be promoted after checking for concurrent canonical edits. Native
self-reports are not acceptance evidence.

Host isolation requires macOS seatbelt or Linux Bubblewrap/user namespaces and
fails closed when unavailable. Native configuration, auth and session homes stay
native. Job launches disable unrelated MCP servers/hooks/plugins so they cannot
carry authority into the job. Clean-room and no-internet jobs use only the
controller's local tools: project and installed-library reads, local Lean search,
checks, and commands whose OS sandbox denies network access. External web research,
remote Lean search, and new repository installations are unavailable. Native model
connections remain available; authentication, model defaults, sampling and
in-invocation compaction remain the runtime's responsibility.

Restricted invocations start with fresh, ephemeral native histories and exclude
saved sessions, memories and automatic native context. LeanFlow retains its own
checkpoint and proof state, so an existing clean-room campaign can resume with the
same restriction. A campaign created online, or with unknown legacy provenance,
cannot be resumed as a clean-room test. General native runs retain their native
tools and network access.

## Limits and accounting

`limits.agent_calls`, `limits.tokens` and `limits.wall_time_s` are optional global
limits. Their launch overrides are `LEANFLOW_PROVER_AGENT_CALLS`,
`LEANFLOW_PROVER_TOKENS` and `LEANFLOW_PROVER_WALL_TIME_S`. Unset means unlimited;
zero means exhausted. Boolean, negative and non-finite limits are invalid.

One successfully spawned harness invocation costs one agent call. Planning,
review, research and continuation processes consume the same global allowance.
Internal model retries, tool use and compression do not create extra LeanFlow
agent calls. A process that never starts is refunded. Interrupted invocations
retain their charge; unknown usage never becomes zero.

Global wall time counts cumulative active controller time across resumes,
including tool queues, checks and builds. Parallel jobs share the same clock;
offline time between controllers is excluded. Tokens are native-reported usage,
normalized when a runtime reports cumulative totals. A token ceiling prevents
future invocations after observed usage reaches it and may be exceeded by an
already running invocation. Unknown completed usage stops further token-capped
admission. No default per-response, per-job or Lean-check wall timeout is imposed.
Transport connectivity checks and process-cleanup grace periods are separate
from execution budgets.

## State and inspection

Prover state lives at `.leanflow/workflow-state/prover/<run-id>/`, including
`PLAN.md`, `DAG.json`, protected sources, jobs, logs and receipts. Use
`leanflow runs prover <run-id> --json` or the VS Code prover workspace. Resume
with `LEANFLOW_PROVER_RESUME_RUN_ID`; saved global usage and active time persist.
See [the prover reference](prover-workflow.md) for verification and recovery.

Other native workflows use `.leanflow/native-workflows/<run-id>/`. `state.json`
records the native session, `limits.json` holds the ledger, and accepted source
and independent-review receipts remain inspectable. Resume with
`LEANFLOW_NATIVE_RESUME_RUN_ID` and the same workflow, backend and limits.
Canonical source conflicts prevent publication or resume.

`leanflow status`, `workflow status`, `workflow activity`, and `leanflow runs`
provide inspection surfaces. Run-bound metrics distinguish known usage from
missing evidence and never infer success from a partial log. The VS Code
extension launches the same CLI and validates profiles against the flag catalog.

## Skills

LeanFlow ships with a small curated skill core for Lean workflows. Skills are not a side feature here; they are part of how the agent is steered toward proving, diagnostics, formalization, resume, and explicitly selected prover concurrency.

Skills are the routing layer over the native workflow and worker specs in
`leanflow_specs/`. The specs define the canonical Lean contract; skills select
the relevant contract and tool order for the current workflow state.

Built-in skills:

- `lean-bounded-prover`
  - current `prove` / `autoprove` identity and bounded proof contract
  - scratch-only work, persistent call limits, concrete proof attempts, and independent controller acceptance
- `lean-proof-loop` and `lean-theorem-queue-worker`
  - retained native compatibility skills; the new prover does not enter their queue/advisory loop
- `lean-diagnostics`
  - focused diagnostic mode for `review`
  - emphasizes: current blockers, open goals, verification state, and project-wide remaining `sorry`
- `lean-formalization`
  - formalization and declaration-building skill for `formalize` and `draft`
  - emphasizes source inspection, blueprint planning, traceable declarations,
    buildable statement drafts, and an explicit handoff of intentional proof
    holes to `prove`
- `lean-search`
  - unified search helper used before editing proofs, covering both local-project context and Mathlib/semantic discovery
  - emphasizes: nearby declarations, imports, naming/style reuse, theorem-name discovery, statement inspection, and reducing proof guessing
- `lean-refactor-golf`
  - refactor / golfing skill for `refactor` and `golf`
  - emphasizes: simplifying proof structure without breaking verification
- `lean-autonomous-swarm`
  - legacy swarm guidance; native prover concurrency uses its dedicated orchestrator skill
  - emphasizes: file ownership, verifier roles, and strict final verification

### How Skills Are Assigned

There are three ways a skill gets into the agent:

1. Automatic workflow assignment
   - `prove`, `autoprove` -> `lean-bounded-prover`
   - `formalize`, `autoformalize`, `draft` -> `lean-formalization`
   - `review` -> `lean-diagnostics`
   - `refactor`, `golf` -> `lean-refactor-golf`
   - `--agents N` selects research proving concurrency; other native workflows reject this option

2. Manual shell activation
   - `/skill lean-proof-loop`
   - `/skill lean-diagnostics`
   - `/skill reload`
   - direct activation also works via `/<skill-name>`

3. Resume/continuation context
   - the managed runner can carry the active skill through checkpoints, compaction, and autonomous continuation cycles

When a skill is active, its `SKILL.md` content is inserted into the agent prompt as explicit workflow guidance. The prompt builder and `/skills` surface also expose linked workflow-spec metadata, so the agent sees both the routing skill and the underlying spec contract it should follow.

### Skill Install And Override Paths

Skill discovery order is:

- built-in repo skills in `leanflow_skills/`
- user overrides in `~/.leanflow/skills`
- project overrides in `.leanflow/skills`

Precedence is:

- project overrides user
- user overrides built-in

That means you can replace a built-in skill for one machine or one project without editing the shipped repo skill.

Install patterns:

- user-wide skill:
  - create `~/.leanflow/skills/<skill-name>/SKILL.md`
- project-local skill:
  - create `.leanflow/skills/<skill-name>/SKILL.md` inside the Lean project

Example:

```text
~/.leanflow/skills/my-proof-policy/SKILL.md
.leanflow/skills/lean-proof-loop/SKILL.md
```

The second example overrides the built-in `lean-proof-loop` only for that project.

### How The Agent Sees Skills

The agent does not install skills as code plugins. It loads them as prompt-time workflow instructions:

- the skill resolver finds the highest-precedence matching skill
- LeanFlow reads the skill’s `SKILL.md`
- that content is embedded into the agent prompt for the active workflow
- supporting files under `references/`, `templates/`, `scripts/`, and `assets/` stay discoverable through the skill system when needed

Use `/skills` to see what the agent can currently load and where each skill came from.

## Project Model

LeanFlow exposes three project commands:

- `leanflow project init [path] [--name NAME]`
- `leanflow project create <path> [--template-source SOURCE] [--name NAME]`
- `leanflow project show [path]`

Requirements for `project init`:

- the target must be inside a Lean 4 repo
- a Lean root must be detectable from `lakefile.lean` or `lakefile.toml`
- REPL acceleration setup is attempted automatically; `lakefile.toml` projects can be updated safely, while ambiguous `lakefile.lean` projects receive manual setup instructions

LeanFlow writes:

- `.leanflow/project.yaml`
- `.leanflow/runtime/`
- `.leanflow/cache/`
- `.leanflow/workflows/`

Projects can explicitly deliver durable, target-scoped proof handoffs by adding
`workflow_guidance` entries to `.leanflow/project.yaml`:

```yaml
workflow_guidance:
  - path: proof-guidance.md
    targets: [result]
    active_files: [Algebra/Main.lean]
```

Each project-relative Markdown file is bounded, confined to the project root,
and reattached after a restart or compaction only when its content hash is
absent from the active conversation. This keeps supervisor or research
findings available without source-code comments or manual prompt steering.

During `project init`, LeanFlow prints visible REPL setup progress:

- inspect Lean project
- detect `lean-toolchain`
- check for an existing `repl` binary or dependency
- add the `leanprover-community/repl` dependency when safe
- run `lake update repl`
- run `lake build repl`

Long Lake commands print status before and after execution, including elapsed time. A failed REPL setup is a warning, not a project-init failure; proof workflows continue with LSP-backed tactic screening.
- `.leanflow/workflow-state/`

## Skills And Overlays

LeanFlow ships a curated Lean-first skill core.

Builtin skills live in:

```text
leanflow_skills/
```

User and project overlays live in:

```text
~/.leanflow/skills
.leanflow/skills
```

Overlay precedence is:

1. project
2. user
3. builtin

Supported shell commands:

```text
/skills
/skill <name>
/skill reload
/<skill-name>
```

Current curated builtin skills:

- `lean-proof-loop`
- `lean-theorem-queue-worker`
- `lean-diagnostics`
- `lean-formalization`
- `lean-search`
- `lean-refactor-golf`
- `lean-autonomous-swarm`

Lean workflows automatically select a matching default skill unless you activate another one explicitly.

Legacy swarm guidance is retained for historical artifacts. Its intended concerns were:

- file-specific delegation
- verifier roles
- strict zero-sorry finish conditions
- lock-before-edit behavior for shared Lean files

## File Locking and historical tools

LeanFlow includes file reservations for concurrent Lean work.

Purpose:

- stop two agents from editing the same Lean file at the same time
- make user-approved swarm runs safer and easier to reason about

How it works:

- file reservations are stored in `.leanflow/workflow-state/file_locks.json` inside the active project
- the native workflow tool surface includes:
  - `acquire_file_lock`
  - `release_file_lock`
  - `list_file_locks`
- file writes through `write_file` and `patch` respect existing locks
- if another agent owns the lock, the write is rejected instead of silently racing
- locks are keyed to the stable agent session id, not a transient per-turn task id
- native runner and delegated child agents release their locks on exit

Current scope:

- lock enforcement is wired into file-tool writes
- this gives strong protection for the recommended edit path
- terminal-based ad hoc shell edits are still a weaker path and should be avoided in swarm workflows

## Development

See [ARCHITECTURE.md](../ARCHITECTURE.md) for ownership and execution boundaries.
Activate `.venv`, then run Black, Ruff, mypy and the full pytest suite before
committing. The repository agent guide defines the quality gate and LeanProbe
verification policy. Optional container isolation is described in
[sandbox-runtime.md](sandbox-runtime.md).
