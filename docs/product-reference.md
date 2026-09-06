# LeanFlow Product Reference

This reference covers LeanFlow's workflows, skills, providers, runtime
configuration, persistence model, and verification contracts.

LeanFlow is a Lean-first automation shell for proof repair and mathematical
formalization. The installed command is `leanflow`.

The product is optimized for two main jobs:

- `prove`: drive Lean proof repair and completion until the code compiles cleanly
- `formalize`: translate a project-local LaTeX/PDF source document or TeX project directory into statement-verified Lean declarations; `/prove` fills the resulting `sorry`s

Internally, `/prove` and `/autoprove` normalize to the dedicated bounded prover runtime; `/formalize` and `/autoformalize` normalize to the existing native formalization workflow. The auto-prefixed forms are compatibility aliases, not separate product surfaces.

It installs as `leanflow`, uses `~/.leanflow` for user-level config, and keeps project-owned workflow state in `.leanflow/`.

LeanFlow runs `prove` through `leanflow_cli.workflows.prover`; other managed workflows continue through the internal `leanflow-native` runtime. See the [prover workflow](prover-workflow.md) for its current contract, settings, and limits.
Inference can use a Codex OAuth session, direct provider APIs,
OpenAI-compatible endpoints, or local runtimes such as vLLM, Ollama, and
llama.cpp.

## Product Direction

LeanFlow is intentionally Lean-first and automation-first.

- The default shell and workflow UX are built around Lean proving and formalization, not generic assistant chat.
- Proof workflows are judged by strict Lean verification, not by partial progress:
  - explicit successful build
  - clean diagnostics
  - no open goals
  - no `sorry`
- Standard proving uses one prover; research proving explicitly enables planning and bounded concurrent prover jobs.
- Other workflows retain their existing explicit swarm mode and file-lock behavior.

## Skills

LeanFlow ships with a small curated skill core for Lean workflows. Skills are not a side feature here; they are part of how the agent is steered toward proving, diagnostics, formalization, resume, and user-approved swarm behavior.

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
  - swarm skill used only when you explicitly launch a workflow with `--agents N`
  - emphasizes: file ownership, verifier roles, and strict final verification

### How Skills Are Assigned

There are three ways a skill gets into the agent:

1. Automatic workflow assignment
   - `prove`, `autoprove` -> `lean-bounded-prover`
   - `formalize`, `autoformalize`, `draft` -> `lean-formalization`
   - `review` -> `lean-diagnostics`
   - `refactor`, `golf` -> `lean-refactor-golf`
   - `--agents N` on proving selects research concurrency; other autonomous workflows retain `lean-autonomous-swarm`

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

## Native Workflow Contract

This section describes retained native specifications. The current prover uses
its `lean-bounded-prover` / `lean-prover-orchestrator` skills and deterministic
controller contract documented in [prover-workflow.md](prover-workflow.md);
the older native `prove` spec is not its queue or budget authority.

Native Markdown specs are the canonical Lean workflow contract.

Spec roots:

- `leanflow_specs/workflows/`
- `leanflow_specs/workers/`

Workflow specs shipped in the repo:

- `prove`
- `formalize`
- `draft`
- `review`
- `refactor`
- `golf`
- `doctor`
- `search`

Dormant worker specs shipped in the repo:

- `proof-repair`
- `proof-golfer`
- `axiom-eliminator`
- `sorry-filler-deep`

These specs are the source of truth for:

- prompt assembly
- native Lean tool ordering and fallbacks
- doctor/capability reporting
- route decisions
- contract validation in tests

Skills select these specs without duplicating the full operational contract.

For package boundaries and the native execution path, see
[`ARCHITECTURE.md`](../ARCHITECTURE.md).

## What Ships

- `leanflow` CLI with LeanFlow shell branding
- `leanflow-agent` shared agent entrypoint
- Lean workflows:
  - `/draft`
  - `/review`
  - `/refactor`
  - `/golf`
  - `/prove`
  - `/formalize`
  - `/autoprove` -> alias of `/prove`
  - `/autoformalize` -> alias of `/formalize`
- Local runtime commands:
  - `leanflow models local list`
  - `leanflow models local start`
  - `leanflow models local stop`
  - `leanflow models local status`
  - `leanflow models local logs`
  - `leanflow models local use`

## Product Scope

The supported repository surface is limited to the Lean workflow kernel.

Supported product surface:

- LeanFlow shell UX on top of `leanflow`
- Lean proving and formalization workflows
- user-approved multi-agent swarm mode
- file locking for concurrent Lean editing
- curated Lean skill core in `leanflow_skills/`
- provider routing for direct APIs, RCP/custom endpoints, and local runtimes
- managed local runtimes: `vllm`, `ollama`, `llama.cpp`

Removed from the supported product:

- gateway and messaging platforms
- cron/scheduler product surfaces
- browser automation workflow surface
- website/landing page/docs site
- RL/benchmark environment suites
- generic and marketplace-style skill catalogs
- WhatsApp bridge and other non-Lean platform extras

## Name, CLI, and Paths

- Product name: `LeanFlow`
- CLI command: `leanflow`
- State directory: `~/.leanflow`
- Project manifest: `.leanflow/project.yaml`

The interface is styled around EPFL / Lean / AI-for-math work, but the executable name stays `leanflow`.

## Install

Direct local install from the current repo:

```bash
git clone https://github.com/epfl-lara/LeanFlow.git
cd LeanFlow
./scripts/install-internal.sh
```

If you already have the repo checked out locally, just run:

```bash
./scripts/install-internal.sh
```

`./scripts/install.sh` is the Morph/local-template wrapper. Use `./scripts/install-internal.sh` when you want the repo to install its own local CLI wrappers directly.

Default install locations:

- state: `~/.leanflow`
- wrappers: `~/.local/bin/leanflow`, `~/.local/bin/leanflow-agent`
- virtualenv: `./.leanflow-venv`

The installer also checks or wires the external CLI tools used by normal
workflows: `rg` for repository search and Poppler's `pdftotext`, `pdfinfo`, and
`pdfimages` for PDF source inspection.

Custom install locations:

```bash
./scripts/install-internal.sh \
  --leanflow-home "$HOME/.leanflow" \
  --bin-dir "$HOME/.local/bin" \
  --venv-dir "$PWD/.leanflow-venv"
```

## Update

Update by reinstalling from the repo:

```bash
cd LeanFlow
git pull
./scripts/install-internal.sh
```

Sandboxed install:

```bash
./scripts/install-sandbox.sh
```

The sandbox installer runs the normal install, builds the local Docker/Podman
image, and writes an `leanflow-sandbox` wrapper. To upgrade that runtime:

```bash
./scripts/update-sandbox.sh
```

The sandbox runtime copies the active LeanFlow project into a per-run worktree,
mounts only that copy plus sandbox cache/home directories, and exports the final
diff to `~/.leanflow/sandbox/runs/<run-id>/changes.patch`. See
[sandbox-runtime.md](sandbox-runtime.md) for the isolation and patch-export
contract.

## Quick Start

Check the install:

```bash
leanflow --help
leanflow doctor
leanflow doctor env --json
leanflow mcp bootstrap lean
leanflow mcp status --json
leanflow config show
```

Initialize an existing Lean project:

```bash
cd /path/to/lean-project
leanflow project init
leanflow project show
```

Run a workflow:

```bash
leanflow workflow prove Main.lean
leanflow workflow prove Main.lean --provider codex --research
leanflow workflow prove Main.lean --provider codex --research --research-workers 2
leanflow workflow prove Main.lean --provider rcp --model zai-org/GLM-5.2
leanflow workflow prove Main.lean --clean-room --clean-room-label "Benchmark Problem 2"
leanflow workflow prove Main.lean --agents 3
leanflow workflow prove Main.lean --no-parallel
leanflow workflow formalize docs/paper.tex
```

Monitor a run without coupling the default status path to Docker/Podman
availability:

```bash
leanflow status
leanflow status --verbose  # larger history plus a live sandbox-engine probe
```

## Workflow Example Projects

The repo also carries opt-in Lean workflow projects under `testdata/workflow_projects/`.

These are for manual workflow runs and future targeted integration coverage, not for the default pytest or CI path. `testdata/workflow_projects/ProveDemo` is the proof-repair fixture, and `testdata/workflow_projects/DocFormalizationDemo` is the document-formalization fixture.

Interactive mode:

```bash
leanflow
```

Inside the shell:

```text
/banner
/status
/workflow status
/workflow history
/workflow activity
/workflow log 120
/goals
/diagnostics
/proof-state
/provider
/swarm
/skills
/skill lean-proof-loop
/skill reload
/models local list
/cd path/to/project
/project init
/project create DemoProject --template-source https://github.com/example/lean-template.git
/prove
/prove Main.lean
/prove Main.lean --agents 3
/prove Main.lean --no-parallel
/formalize docs/paper.tex
/doctor
/doctor search --json
/mcp bootstrap lean
/mcp status
/mcp status --json
/config get model.default
/exit
/quit
```

Workflow commands also accept forgiving forms without the leading slash:

```text
prove
prove Main.lean
prove Main.lean --agents 3
prove Main.lean --no-parallel
formalize docs/paper.tex
```

The interactive shell starts with an LeanFlow banner that shows the current route and the main Lean commands you are expected to use.

The bottom toolbar is live workflow context, not decoration. It surfaces:

- current managed workflow phase
- active file and target theorem
- latest build state
- active skill
- latest structured workflow event

The shell also reads persisted managed-workflow state so these commands work across resumed sessions:

- `/workflow status`
- `/workflow history`
- `/workflow activity`
- `/workflow log 120`
- `/goals`
- `/diagnostics`
- `/proof-state`

A newly launched runner publishes `starting` and then `reconciling` before it loads the potentially
expensive checkpoint, plan, queue, and Lean preflight state. These phases carry the new process's
verified ownership identity and heartbeat immediately. Proof fields retained from the prior durable
snapshot remain explicitly marked with `startup_reconciliation_pending: true` until fresh Lean state
replaces them, so startup visibility does not claim that historical mathematical state is current.
The shell status panel labels those retained fields as a prior durable snapshot pending
reconciliation.

`/exit` asks the current project's managed runner to shut down cleanly first and waits briefly
for that exit request to land. Any later direct interrupt requires the live process to match the
per-launch ownership-token fingerprint plus its recorded process-group/session identity. Historical
PID-only records are never signaled, so PID reuse cannot redirect cleanup at an unrelated process.

## Autonomous Lean Behavior

The prover and formalizer have separate terminal contracts. `prove` fills only
explicitly authorized source holes, independently audits each accepted
replacement, and requires the requested files to be free of `sorry` plus a final
project build. A file target does not automatically expand to unrelated proof
holes. Without a file, proving discovers eligible project source files.
`formalize` produces a buildable statement draft reviewed against its source and
then hands intentional proof holes to an explicit prover run.

### Bounded Proving And Research Mode

The current prover has standard and research modes. Standard attempts one
assigned declaration at a time in a private workspace. Research adds fresh
informal-planning, DAG-construction, and review sessions, then schedules up to two
provers concurrently by default. Bottom-up dependency completion is the default;
experimental top-down results remain untrusted candidates until prerequisites
close and independent Lean checks accept them.

There is no standing advisor or model-based manager loop. Local proof
subgoals share one 300-request pass by default; compression and local
decomposition cannot reset it. Separate campaign limits bound total requests,
restarts, direction refinements, structural recovery, graph growth, and time.
The controller owns canonical source, the plan, and graph. Models write scratch
artifacts and submit candidates or evidence.
Rejected submissions continue within that same pass. Each prover job can request
one separate resource/computation agent, with a default 40-call allocation charged
to the campaign total. Accepted branch reviews classify direction changes versus
structural decomposition for refinement accounting.

For source protection, research resources and libraries, the complete settings
catalog, outcomes, and current limitations, use the
[prover workflow reference](prover-workflow.md). The
[source research note](prover-redesign-research.md) separates paper evidence
from implementation choices.

### Document Formalization

`/formalize` and `/autoformalize` require a project-local `.tex` source, `.pdf` source, or directory containing a TeX project. They remain the same workflow; `autoformalize` is only a compatibility alias.

The resolver prepares a document formalization workspace before the native runner starts:

- source-document preflight manifest under `.leanflow/workflow-state/formalization/`
- bounded extracted-text cache
- Markdown planner blueprint
- generated supplemental blueprint skill under `.leanflow/skills/`
- active Lean target file for drafted declarations
- original request metadata, selected source document metadata, and deterministic TeX project discovery metadata when the user provided a directory

`/prove SomeFile.lean` auto-attaches that generated blueprint skill when the file has a nearby `Blueprint.md`, so prover turns can recover the source map after context compaction. Any workflow can also receive extra persistent guidance with `--additional-skill path/to/SKILL.md`.
- startup context that tells the drafting agent to plan definitions, lemmas, theorem splits, source comments, source pointers, and statement-fidelity checks before proof repair
- an automatic independent statement/source verifier pass once the draft is otherwise ready and only approval statuses are missing

The deterministic preflight is intentionally modest, but it recognizes common math-paper structure. LaTeX documents get sections, labels, references, citations, theorem-like blocks, and adjacent proof excerpts extracted. The theorem scanner covers standard environments, custom `\newtheorem` environments, `thmtools` `\declaretheorem`, `mdframed` `\newmdtheoremenv` / `\mdtheorem`, Springer `\spnewtheorem`, `tcolorbox` `\newtcbtheorem`, theorem-like `\newenvironment` names, and plain-TeX `\profess...\endprofess` blocks. Directory inputs first select a main TeX entrypoint, collect included `.tex` files, bibliography files, local assets, PDFs, figures, and TeX support files, and reject ambiguous roots with an explicit error. PDFs use installed local tools such as `pdftotext`, `pdfinfo`, and `pdfimages` when available, and record degraded extraction reasons when they are not. The planner agent can then use the normal file, terminal, web, and Lean tools to inspect the document more deeply, pull referenced material, and draft Lean files with `sorry`. The independent verifier then checks the source fidelity and marks approved blueprint entries. When that gate passes, the formalizer gets one final generated-file organization pass, exits, and proof filling waits for an explicit user-run `/prove`.

Expected document-prep completion is a buildable statement/source-approved draft that may still contain intentional `sorry`s. Proof filling is the next phase: it starts only when the user explicitly runs `/prove SomeFile.lean` or `/prove` after reviewing the generated formalization, and it is not part of judging whether the source formalization draft itself is ready.

LeanFlow writes managed workflow status, activity, checkpoints, file locks, and the full latest managed runner log into the active project’s `.leanflow/workflow-state/` directory by default so long runs stay next to the Lean repo you are debugging.

Workflow state also includes structured capability snapshots and route decisions in
`.leanflow/workflow-state/outcomes.jsonl`, so resumed runs can reuse prior
blocker classification instead of starting blind.

### Project-Scoped `/prove`

`leanflow workflow prove` discovers eligible project Lean files and builds one
controller-owned DAG from their proof holes. `leanflow workflow prove Main.lean`
restricts the assignment to that file. Definition holes require
`LEANFLOW_PROVER_FILL_DEFINITIONS=1`. Original source outside authorized holes is
protected; reviewed helper modules and their imports are additive controller
changes. The controller refuses conflicting concurrent source edits.

### Logging And Inspection

The existing `/workflow status`, `/workflow activity`, and `/workflow log`
surfaces receive compact prover events through its observer adapter. Full prover
state belongs to the exact run under
`.leanflow/workflow-state/prover/<run-id>/`, including `PLAN.md`, `DAG.json`,
`state.json`, baselines, and independent job logs.

The prover publishes progress independently of source transactions, with a
two-second heartbeat during long Lean or provider operations. The VS Code Live
view refreshes live prover state every three seconds, rejects older snapshots,
and preserves the last good state after a read failure. Submission checking and
source integration count as awaiting verification, including when a prover job
is still waiting synchronously for the independent check. Only accepted,
integrated proofs count as solved. Proposed DAGs and staged diffs are labelled
separately from the committed source checkpoint.

The budget view distinguishes total spent calls, outstanding reservations and
capacity available to new jobs. Each planning/review/resource stage has its own
ceiling and may finish early. A job exhausting its allocation does not mean the
campaign has exhausted its total budget; terminal reports identify the limiting
scope and retained obligations. One independent candidate-check deadline covers
Lean elaboration and kernel inspection after acquiring the verifier. Queue waits
are bounded by remaining campaign time and reported separately; scratch checks inherit
the configured check cap. New stages are also capped by remaining campaign time.
Graph validation is a multi-check controller operation, so its individual
checks and overall campaign allowance are displayed separately.

```bash
leanflow runs prover <run-id> --json
leanflow runs prover-message <run-id> --message "Use the compactness argument."
leanflow runs event <run-id> <event-id> --json
LEANFLOW_PROVER_RESUME_RUN_ID=<run-id> leanflow workflow prove Main.lean
```

Compact activity rows carry a one-line description, bounded previews, and an
`evidence_id`. `leanflow runs event` joins one row to the complete record in the
job's transcript (`jobs/<job-id>/events.jsonl`) or the controller event log, so
an editor can show the full assistant message, tool arguments, and tool result
on demand without copying transcripts into the shared stream. Rows recorded
before evidence ids existed are matched by kind, call count, tool, and time,
and the payload reports `match: "heuristic"` for them.

Resume creates a new execution with prior-run lineage, saved configuration, and
retained request spending. It does not grant an interrupted job a fresh pass.
The VS Code Live workspace shows the selected run's theorem tree, plan, jobs,
source locations, baseline diffs, and usage. Guidance reaches the controller or
addressed job at its next decision/request boundary; it does not interrupt an
active provider call.
See [progress and resume](prover-workflow.md#inspect-progress-and-resume).

Other native workflows retain their existing checkpoint, activity-retention,
terminal-log, and Lean-service inspection behavior. Native tool and queue
compatibility details below do not add advisor loops to the new prover.

## Native Lean Tool Surface

LeanFlow exposes a typed Lean tool surface through the `lean`,
`leanflow-native`, and `leanflow-native-swarm` toolsets.

- `lean_capabilities`
  - probe project validity, Lean/Lake/Elan binaries, MCP/LSP tools, search providers, helper availability, worker availability, and degraded-mode reasons
- `lean_inspect`
  - return structured Lean state for a file: diagnostics, goals, `sorry` counts, blocker classification, queue candidates, and the current capability snapshot
- `lean_verify`
  - run the canonical verification ladder in `file_exact`, `module`, or `project` mode
- `lean_incremental_check`
  - run the fast LeanProbe/LeanInteract-backed verifier for ordered same-file theorem queues
  - `prepare_file` warms imports/header and optionally advances cached environments to a target declaration
  - `check_target` verifies the assigned declaration or replacement chunk with `allow_sorry=False`
  - `include_axiom_profile=true` embeds marker-bound transitive axiom evidence in the same exact
    target check; managed assigned-target replacements enable it automatically
  - foreground `check_helper` uses LeanProbe for fast elaboration feedback; adding
    `include_axiom_profile=true` switches to the one-shot exact-project Lake harness and requires a
    complete allowed-axiom profile before model-authored insertion
  - dispatch research workers always use that exact helper harness instead of retaining LeanProbe;
    it keeps the exact pre-anchor source, rejects placeholders and disallowed axioms, and always
    requires the parent recheck
  - staging a canonical worker-checked helper creates a durable exact-assignment action record;
    the parent rechecks it before orchestration, fences one immediate insertion opportunity, and
    retires it only after the ordinary current-source helper gate banks it. Merely acknowledging
    the research prompt is not action, and operational recheck failures remain resumable
  - `feedback` returns diagnostics and optional tactic/proof-state annotations for repair prompts
  - default usage for queue progress:
    - `lean_incremental_check(file_path="Demo/Main.lean", theorem_id="my_theorem", action="check_target")`
    - read `ok`, `valid_without_sorry`, `has_errors`, `has_sorry`, `messages`, `elapsed_s`, and `cache`
  - richer repair usage:
    - set `include_tactics=true`, or use `action="feedback"`, when diagnostics are not enough and the model needs intermediate tactic states
    - inspect `tactics[*].tactic`, `tactics[*].goals`, `tactics[*].proof_state`, `tactics[*].file_start`, `messages[*].file_start`, and `feedback_lean`
    - `feedback_lean` is the model-readable version of the current declaration with inserted feedback comments; use it to repair the proof at the exact failing line
    - failures automatically try to rerun with tactic collection when possible, so blocked proofs usually return richer context without slowing successful checks
  - trust it for queue-step validity when LeanProbe is available, the project-local REPL matches the current toolchain, the cached environment was built from current file content up to the target, and the checked chunk exactly matches the current declaration replacement
  - use `lean_verify` instead for final sweeps, unavailable/crashed/stale LeanProbe sessions, header/import/earlier-declaration edits, non-ordered queues, or explicit canonical checks
- `lean_search`
  - search in `auto`, `local`, `semantic`, `type-pattern`, or `natural-language` mode
  - prefers MCP/LSP-backed providers first and falls back to local `rg`/Mathlib search with explicit provider provenance and degraded reasons
  - managed file-scoped proving marks confirmed later same-file declarations as source-order
    inaccessible using the current disk declaration index; imported, prior, and ambiguous results
    remain in the usable result list
- `lean_proof_context`
  - theorem-context retrieval from the managed automation backend: theorem statement, original proof text, hypotheses, in-scope names, namespace, and similar proofs
  - this is not a replacement for `lean_inspect` goals
  - when the active file already contains the target declaration, LeanFlow first stabilizes lookup from the local declaration range before asking the backend for richer context
  - if the proof-auto backend reports `theorem_not_found` or another backend-side context failure, LeanFlow falls back to a local declaration-slice context instead of pretending the backend succeeded
  - a theorem-lookup miss does not disable proof-auto for the rest of the run; LeanFlow only sticky-disables proof-auto after transport or systemic backend failures
- `lean_multi_attempt`
  - screen 2-6 concrete tactic candidates at one proof location through the MCP backend
- `lean_auto_search`
  - ask the managed automation backend for one theorem-local automated proof candidate after proof context or concrete local evidence exists
- `apply_verified_patch`
  - compatibility path for one atomic Lean patch, pre-edit checkpoint, and immediate verification payload
  - in managed queue workflows, successful `patch` and `write_file` edits are already verified by the manager before the queue advances
- `lean_sorries`
  - list remaining `sorry` findings across a project or a single file with declaration names and line numbers
- `lean_axioms`
  - run a best-effort `#print axioms` check for one declaration and report `axioms`, `custom_axioms`, `classical`, and `choice`
- `lean_reasoning_help` / `lean_decompose_helpers`
  - request advisory mathematical strategy or a structured helper split without granting either
    advisor proof or stopping authority
  - the decomposition `timeout_s` is one whole-request deadline shared by the advisor and all
    subsequent Lean skeleton checks; an inner research cold-start floor cannot extend it
## Prover Session Tools And Scheduling

The dedicated prover uses a small role-specific tool set. Provers receive bounded
file reads, scratch writes/replacements, project/Lean library search, and warm
Lean checks. Planning and research receive web search, resource download, and
restricted integer/rational computation. The controller performs source
mutations, helper registration, scheduling, acceptance, and final verification.
No generic terminal, standing advisor, or nested model decomposition service is
exposed to a prover session.

The [prover reference](prover-workflow.md) documents its actual default budgets
and top-down/bottom-up semantics. The native runtime's former theorem queue,
route selector, specialist advisors, and per-route budget refreshes are not the
active `prove` execution path. Their compatibility modules remain available to
existing code and tests; other workflow behavior is unchanged.

## Reasoning / Thinking Policy

The settings in this section describe the existing native agent loop. The new
prover uses the launch/provider reasoning setting without native queue-based
escalation. Its per-role model/context limits and deterministic compression are
controlled by `LEANFLOW_PROVER_*`; see [budgets and context](prover-workflow.md#budgets-and-context).

Default native agent settings:

```yaml
agent:
  max_turns: 200
  reasoning_effort: "auto"
  seed: 42
  temperature: 0.3
  top_p: null
  top_k: null
  min_p: null
```

`auto` is Lean-specific rather than a generic chat setting:

- managed theorem-queue turns start at `medium`
- after `5` failed attempts on the same `(theorem, file)` pair, the runner raises that theorem's reasoning intensity to `high`
- when the queue moves to a different theorem, the new theorem resets back to `medium`
- when the declaration queue is empty but the file still needs a final cleanup pass, the whole-file sweep uses `high`
- failed-attempt memory is scoped per theorem, so previous theorems do not drag old blocker history into unrelated prompts

Operational details:

- the failed-attempt counter increments on each failed `edit -> verification feedback -> still blocked` boundary, not only once per long conversation
- the default reasoning escalation threshold is configurable with `LEANFLOW_NATIVE_FAILED_ATTEMPT_REASONING_THRESHOLD`
- the `PREVIOUS ATTEMPTS` cap is configurable with `LEANFLOW_NATIVE_FAILED_ATTEMPT_HISTORY` and defaults to `10`

You can still override it explicitly:

```bash
/reasoning auto
/reasoning none
/reasoning low
/reasoning minimal
/reasoning medium
/reasoning high
/reasoning xhigh
```

On routes that only support `low|medium|high`, LeanFlow maps automatically:

- `minimal -> low`
- `xhigh -> high`
- `none` disables model thinking entirely

Sampling defaults are Lean-oriented rather than chatty:

- `seed: 42` keeps runs more reproducible on compatible routes
- `temperature: 0.3` leaves a small amount of exploration for proof search
- `top_p`, `top_k`, and `min_p` stay unset by default

This mode is automatic for autonomous workflows with an `ACTIVE_FILE`. For project-wide autonomous runs the queue is per-file instead of per-declaration, and swarm mode (`--agents N`) is the path for parallel per-file work.

## User-Approved Swarm Mode

For proving, `--research` enables a planning orchestrator and concurrent DAG
workers. `--research-workers N` selects their limit; `--agents N` with `N > 1`
is a compatibility spelling. `--no-parallel` retains research planning with one
prover. Standard proving runs one prover without an independent research pool.

Other workflows retain explicit native swarm delegation:

```bash
leanflow workflow formalize docs/paper.tex --agents 3
```

That native mode uses `lean-autonomous-swarm`, enables delegation only for the
requested workflow, and uses file locks to prevent conflicting ownership.

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

The swarm-specific skill is only relevant when the user enabled parallel agents. It encodes:

- file-specific delegation
- verifier roles
- strict zero-sorry finish conditions
- lock-before-edit behavior for shared Lean files

## File Locking

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

## Repository Layout

The main active codepaths are:

- `leanflow_cli/` for shell UX, config, project/workflow orchestration, local runtimes, locks, and workflow state
- `leanflow_skills/` for the curated Lean skill core
- `agent/` for prompt assembly, context compression, display, and shared agent internals
- `tools/` for the Lean-kernel tool surface
- `tests/leanflow/` plus selected agent/runtime tests for the supported product

You should not expect deleted gateway, website, cron, data-generation, voice, or broad skill-catalog directories to exist anymore.

## Provider Configuration

Inspect the active provider selection:

```bash
leanflow provider
leanflow provider --requested zai
leanflow provider --requested local
leanflow provider --requested custom
leanflow provider --requested rcp
```

LeanFlow supports three provider classes:

1. Codex OAuth and direct provider APIs
2. OpenAI-compatible remote endpoints
3. Managed local runtimes

### Direct Providers

Supported direct providers include:

- `codex`
- `zai`
- `kimi-coding`
- `minimax`
- `minimax-cn`
- `deepseek`
- `anthropic`

The `codex` route reuses an existing Codex CLI login and sends requests through
the Codex Responses endpoint:

```bash
codex login
leanflow config set model.provider codex
```

Example:

```bash
export GLM_API_KEY=...
leanflow provider --requested zai
```

### OpenAI-Compatible Remote Endpoints

Generic OpenAI-compatible endpoints use the `custom` path:

```bash
export LEANFLOW_OPENAI_BASE_URL="https://inference.rcp.epfl.ch/v1"
export LEANFLOW_OPENAI_API_KEY="..."
leanflow provider --requested custom
```

Preferred env var names for LeanFlow are `LEANFLOW_OPENAI_BASE_URL` and `LEANFLOW_OPENAI_API_KEY`.
Legacy/generic names such as `OPENAI_BASE_URL` and `OPENAI_API_KEY` are still accepted, but the LeanFlow-prefixed names are the stable user-facing ones.

EPFL RCP also has a first-class `rcp` route. It resolves GLM credentials from
`GLM_API_KEY` / `GLM_BASE_URL` and other RCP models from
`RCP_OPENAI_API_KEY` / `RCP_OPENAI_BASE_URL`, with explicit documented
fallbacks. A workflow-local model choice refreshes the coupled credential and
endpoint before launch:

```bash
leanflow workflow prove Main.lean \
  --provider rcp --model zai-org/GLM-5.2 --research
```

For any explicit workflow provider, LeanFlow propagates the resolved provider,
model, and reasoning effort to isolated manager, planner, advisor, verifier,
worker, and compression calls after dotenv reload. Custom/RCP launches also
propagate the coupled endpoint and credential. This prevents an auxiliary role
from silently reverting to a globally configured model, reasoning policy, or
incompatible model-family key.

Environment precedence is process environment, then `~/.leanflow/.env`, then
the project `.env`. Dotenv files fill missing values and do not overwrite an
explicit launch-scoped environment override.

The `LEANFLOW_NATIVE_*` variables are internal workflow-launcher plumbing. The CLI sets those automatically when it starts `leanflow-native`; you should not need to export them manually.

For RCP / vLLM-style endpoints, LeanFlow enables model thinking through provider-compatible request fields instead of only the OpenRouter-style `reasoning` payload:

- `extra_body.chat_template_kwargs.enable_thinking`
- `extra_body.reasoning_effort`

That matches AIaaS/RCP-style models such as Qwen hybrid reasoning checkpoints and GLM routes that expose reasoning content on the OpenAI-compatible API.

If GLM is down, the tested fallback model on that endpoint is:

```text
google/gemma-3-27b-it
```

Use the exact model name. The endpoint is case-sensitive.

Inside the interactive shell, `/provider` shows both the resolved provider and the supported target names so you can verify the route before launching a workflow.

### Local Runtimes

Select a local runtime:

```bash
leanflow models local use vllm google/gemma-3-27b-it
leanflow provider --requested local
```

Start a local runtime:

```bash
leanflow models local start vllm google/gemma-3-27b-it
leanflow models local status vllm
leanflow models local logs vllm
```

Other supported runtimes:

- `ollama`
- `llama_cpp`

## Workflow Tool Surfaces

`prove` uses `leanflow-prover-session`, an intentionally empty registry toolset:
`session_tools.py` supplies the actual role-specific schemas directly. It does
not inherit the generic native terminal, advisor, or delegation tools.

Other workflows retain these native surfaces:

- `lean`
  - shared typed Lean capability surface
  - includes `lean_capabilities`, `lean_inspect`, `lean_verify`, `lean_incremental_check`, `lean_search`, `lean_proof_context`, `lean_multi_attempt`, `lean_auto_search`, `apply_verified_patch`, `lean_sorries`, `lean_axioms`, `lean_reasoning_help`, and `lean_decompose_helpers`
- `document`
  - project-local source-document inspection for formalization
  - includes `read_pdf` and `formalization_document_inspect`

- `leanflow-native`
  - default single-agent Lean workflow runtime
  - includes the shared `lean` and `document` toolsets plus file, terminal, web, session search, skills, and file-lock coordination
  - does not include delegation
- `leanflow-native-swarm`
  - enabled only for user-approved `--agents N` workflows
  - adds delegation on top of the same native Lean tool surface
  - intended for bounded multi-agent Lean runs with file ownership rules

## Configuration

The dedicated prover settings are catalogued under `LEANFLOW_PROVER_*`; see
[the configuration table](prover-workflow.md#budgets-and-context). Existing
`agent.max_turns`, advisor, and model-summary compression settings below do not
reset or override prover pass/campaign request limits.

Main config file:

```text
~/.leanflow/config.yaml
```

Main env file:

```text
~/.leanflow/.env
```

Top-level config shape:

```yaml
leanflow:
  project:
    template_source: ""
  workflow:
    managed_state_dir: ""
    autonomous_followups: 6

model:
  default: moonshotai/Kimi-K2.6
  provider: auto
  base_url: ""
  api_key: ""

auxiliary:
  lean_reasoning:
    provider: main
    model: moonshotai/Kimi-K2.6-int4
    reasoning_effort: high
    base_url: ""
    api_key: ""
    command_template: ""
    codex_command_template: ""
    claude_code_command_template: ""
  blueprint_verification:
    provider: main
    model: ""
    reasoning_effort: ""
    base_url: ""
    api_key: ""
    command_template: ""
    codex_command_template: ""
    claude_code_command_template: ""
  autoformalizer_verification:
    provider: local
    model: ""
    reasoning_effort: ""
    base_url: ""
    api_key: ""
    command_template: ""
    codex_command_template: ""
    claude_code_command_template: ""

agent:
  max_turns: 200
  reasoning_effort: "auto"
  seed: 42
  temperature: 0.3
  top_p: null
  top_k: null
  min_p: null

compression:
  enabled: true
  threshold: 0.75
  summary_model: moonshotai/Kimi-K2.6
  reserved_output_tokens: 20000
  prune_tool_output: true
  prune_keep_recent_user_turns: 2

local_models:
  default_runtime: vllm
  active_runtime: ""
  active_model: ""
  runtimes:
    vllm:
      host: 127.0.0.1
      port: 8000
      extra_args: []
    ollama:
      host: 127.0.0.1
      port: 11434
      extra_args: []
    llama_cpp:
      host: 127.0.0.1
      port: 8080
      extra_args: []

logging:
  preview_lines: 8
  preview_chars: 1600
  tool_output_head_lines: 28
  tool_output_tail_lines: 12
  activity_preview_chars: 420
```

Useful commands:

```bash
leanflow config get model.default
leanflow config set model.default '"moonshotai/Kimi-K2.6"'
leanflow config set model.provider '"auto"'
leanflow config set model.base_url '"https://inference.rcp.epfl.ch/v1"'
leanflow config set auxiliary.lean_reasoning.model '"moonshotai/Kimi-K2.6-int4"'
leanflow config set auxiliary.lean_reasoning.provider '"main"'
leanflow config set auxiliary.lean_reasoning.reasoning_effort '"high"'
leanflow workflow review Main.lean --expert-provider codex
leanflow workflow review Main.lean --expert-provider claude-code
leanflow config set auxiliary.blueprint_verification.provider '"claude-code"'
leanflow config set auxiliary.autoformalizer_verification.provider '"local"'
leanflow config set agent.reasoning_effort '"auto"'
leanflow config set agent.seed '42'
leanflow config set agent.temperature '0.3'
leanflow config set agent.top_p 'null'
leanflow config set agent.top_k 'null'
leanflow config set agent.min_p 'null'
```

The native tool surface retains `lean_reasoning_help`; it is absent from the
bounded prover session. Where available, it uses the configured `auxiliary.lean_reasoning` model as a
deep theorem advisor. Its default response budget is `64000` tokens so hard
proof advice is not prematurely clipped; override with
`LEANFLOW_LEAN_REASONING_HELP_MAX_TOKENS` when a provider needs a lower cap.
Reasoning and decomposition advisor requests use a `360`-second default whole
request deadline so an auxiliary route cannot silently occupy the foreground
for twenty minutes.
Main model calls wait up to `1200` seconds by default before LeanFlow treats the
provider request as timed out; override with `LEANFLOW_API_TIMEOUT` if needed.
Model and command responses pass through the same persistence guard: accurate
open-problem or blocker evidence is retained, terminal surrender recommendations
are removed, and the response is framed as evidence for a distinct route, job,
portfolio refresh, or fresh campaign epoch. Advisor prose remains unverified and
cannot establish proof, disproof, or campaign termination.

For opt-in command advisors, set `auxiliary.lean_reasoning.provider` or pass
`--expert-provider codex` / `--expert-provider claude-code` on a workflow.
Command templates may be supplied with `--expert-command-template`,
`AUXILIARY_LEAN_REASONING_COMMAND_TEMPLATE`, or the provider-specific
`LEANFLOW_EXPERT_CODEX_COMMAND_TEMPLATE` /
`LEANFLOW_EXPERT_CLAUDE_CODE_COMMAND_TEMPLATE` variables. Commands are split
without a shell and receive the full advisor prompt on stdin; workflow activity
logs record the prompt, command, exit status, response, and truncation metadata.

Formalization verification uses two separate auxiliary tasks. `auxiliary.blueprint_verification`
controls the independent statement/source review for document blueprints. The default
`main` path preserves the managed reviewer-agent behavior. Setting it to `codex`
or `claude-code` runs the corresponding command reviewer; setting it to another
model/RPC provider records an advisory review report. `auxiliary.autoformalizer_verification`
controls advisory review around the deterministic handoff verifier and defaults
to `local`. Non-local verifier output can propose corrections or review source
fidelity, but deterministic local checks and Lean kernel verification remain the
authoritative acceptance gate.

If an endpoint omits or misreports model context-window metadata, pin the value
in `~/.leanflow/config.yaml`:

```yaml
model:
  context_lengths:
    vendor/model-id: 200000
```

The native file-tool surface guards Lean declaration edits by default. These
settings do not relax the bounded prover controller's original-source protection. File write and patch tools block
deleting, renaming, moving, or changing existing `theorem`, `lemma`, and
`example` statements; proof-body edits and new declarations are allowed. In a
managed theorem queue turn, the queue guard additionally restores edits to
pre-existing non-assigned declarations while allowing new helper declarations
for the assigned theorem. For an intentional statement refactor, set
`LEANFLOW_ALLOW_LEAN_STATEMENT_EDITS=1` in the process environment or
`~/.leanflow/.env`, then unset it again after the refactor.

Compression defaults are tuned for long Lean sessions:

- `reserved_output_tokens` keeps headroom for the next response instead of filling the full context window.
- `prune_tool_output` replaces stale old tool result bodies with a fixed marker.
- `prune_keep_recent_user_turns` keeps the newest user turns and their nearby tool output intact.
- the compression gate checks the exact outgoing API payload before every model call, including provider-specific reasoning replay fields such as `reasoning_content`.
- provider usage accounting can undercount replayed reasoning for some backends; the `Request: ~N tokens` log line is the local payload estimate used for pre-send compression.
- for custom endpoints, LeanFlow does not use OpenRouter context metadata; it uses config overrides, provider `/models` metadata, curated defaults, or the conservative `200,000` token fallback.

## Doctor And MCP Status

Run:

```bash
leanflow doctor
leanflow doctor env
leanflow doctor mcp --json
leanflow doctor search --json
leanflow mcp bootstrap lean
leanflow mcp status
leanflow mcp status --json
```

Supported doctor modes:

- `all`
- `env`
- `mcp`
- `search`
- `migrate`
- `cleanup`

`doctor` is non-throwing and uses the same capability layer as the Lean
workflows. It reports:

- `git`
- `rg`
- `lake`
- `elan`
- current LeanFlow home and config
- active project discovery
- current provider resolution
- MCP/LSP tool availability
- search-provider availability
- helper-tool availability
- available native workers
- degraded-mode reasons

MCP is backend infrastructure for native Lean tools, not a separate
user-facing workflow.

Installer/bootstrap-managed default Lean MCP backends:

- `lean-probe>=0.2.2,<0.3`
  - Python package dependency powering the LeanFlow `lean_incremental_check` compatibility tool
  - provides the LeanProbe/LeanInteract-backed queue-step verifier, target feedback, tactic states, and warm same-file declaration cache
  - used as an internal verifier surface, not exposed as a separate user workflow; final file/project acceptance still goes through Lake
- `lean-lsp-mcp==0.26.1`
  - primary state/search backend
  - diagnostics, goals, local search, semantic search helpers, state/premise/hover/outline discovery, and `lean_multi_attempt`
  - configured with local power modes: `LEAN_REPL=true`, `LEAN_LOOGLE_LOCAL=true` on Linux/macOS/WSL, `LEAN_REPL_TIMEOUT=60`, and `LEAN_REPL_MEM_MB=8192`
  - search order prefers local Loogle when ready and toolchain-compatible with the active project, then public remote Loogle/Lean search fallbacks, then project/Mathlib `rg`
- `lean-proof-auto-mcp@v0.4.0`
  - secondary automation/context backend
  - theorem-local context and automation helpers such as `get_proof_context`, `probe`, `search_automated_proof`, and `try_automated_proof`
  - LeanFlow uses it through native wrappers and degrades cleanly when backend lookup misses a declaration that exists in the local file
- `lean-explore`
  - optional semantic declaration-search backend
  - `lean_search` prefers the local backend when `lean-explore[local]` is installed and `lean-explore data fetch` has prepared the index
  - `lean_search` uses the hosted API only when `LEANEXPLORE_API_KEY` is present and local search is unavailable or disabled
  - installed and configured disabled by default as an MCP server because the API backend requires credentials; enable it in `~/.leanflow/config.yaml` for MCP tools or switch its args to the local backend after fetching LeanExplore data

The install script bootstraps these backends by default under `~/.leanflow/mcp/venvs/`. To repair or recreate them later, run:

```bash
leanflow mcp bootstrap lean
```

`leanflow mcp status` shows server role labels, whether a server is
LeanFlow-managed, whether it is configured or installed, local Loogle/REPL
power-mode status, public remote fallback policy, and whether bootstrap is
recommended. The same information is available in the interactive shell
through `/doctor ...`, `/mcp bootstrap lean`, and `/mcp status [--json]`.

Local Loogle requires Unix-like systems (Linux, macOS, or WSL), `git`, `lake`/`elan`, and roughly 2GB of disk. The first local Loogle build can take 5-10 minutes; later starts are fast. If local Loogle is unavailable, LeanFlow allows public remote Lean search fallbacks. Paid or API-key backends are never required by the installer.

Raw `mcp_*` tools are still available through explicit `mcp-{server}` toolsets for debugging, but they are not part of the normal native Lean workflow surface. The model should use the native Lean wrappers instead.

In research mode, a completed `lean_multi_attempt` triggers bounded reclamation of its exact
managed `lean-lsp` server after the result has been preserved. The client lets already-admitted
concurrent requests finish under their own timeouts, closes the server process tree, and leaves the
remaining MCP portfolio running. A pre-probed handler or later capability probe reconnects
lean-lsp lazily; a pending recycle is retryable and never circuits the capability off for the run.
This prevents a tactic screening call's multi-gigabyte Lean worker peak from remaining resident
indefinitely while keeping the full proof workflow available.
`LEANFLOW_RESEARCH_RECYCLE_MULTI_ATTEMPT_MCP=0` is an explicit
short-run benchmarking opt-out; it is not recommended for long campaigns. Reconnect is bounded by
the waiting tool call's original deadline. A retirement error keeps the old server identity owned
and blocks replacement startup, so failed teardown cannot silently overlap two heavy Lean workers.
A timed-out replacement keeps its per-server startup fence until asynchronous cancellation cleanup
has finished, preventing an immediate retry from starting another worker. Native runtime shutdown
also owns unregistered startups and active retire tasks, retains exact identities that fail to
close, and reports those names as cleanup failure instead of stopping the shared loop or claiming a
clean exit.

For memory-constrained runs, `LEANFLOW_LOW_MEMORY=1` skips every configured MCP
subprocess, the in-process LeanExplore index, and LeanProbe's warm incremental
environment cache for that process. Native Lean tools then report degraded capability
provenance and use exact Lean checks plus project/Mathlib text-search fallbacks; final
file/project acceptance still uses Lean/Lake and is unchanged. Use the narrower
`LEANFLOW_DISABLE_MCP=1` switch to disable only MCP subprocesses.

Background research processes default to a worker-specific light profile: the foreground
keeps its configured MCP and LeanExplore portfolio, while each worker starts no MCP
subprocesses and no local LeanExplore index. Native Lean checks, local proof-context
extraction, and project/Mathlib text-search fallbacks remain available. Advanced
deployments can opt workers back into configured MCP servers with
`LEANFLOW_DISPATCH_MCP_SERVERS=*`; after lean-lsp is enabled, its private local Loogle
still requires the additional `LEANFLOW_DISPATCH_LOCAL_LOOGLE=1` opt-in. Choose a worker
LeanExplore backend separately with
`LEANFLOW_DISPATCH_LEANEXPLORE_BACKEND=api|local|auto`.

For theorem-local automation, the important behavior is:

- `lean_proof_context` prefers backend context when available
- if proof-auto lookup fails for a declaration that the local file already contains, LeanFlow falls back to a local declaration slice and nearby declarations
- a proof-auto `theorem_not_found` miss is treated as a local context miss, not a run-wide backend failure; proof-auto remains available for later declarations

To persist MCP sampling audit events to disk, enable it per server in `~/.leanflow/config.yaml`:

```yaml
mcp_servers:
  some_server:
    sampling:
      enabled: true
      audit_jsonl: true
      audit_jsonl_path: "~/.leanflow/logs/mcp-sampling.jsonl"  # optional override
```

If `audit_jsonl_path` is omitted, LeanFlow writes to `~/.leanflow/logs/mcp-sampling.jsonl`.

## Packaging

Python package:

```text
leanflow-agent
```

Console scripts:

- `leanflow`
- `leanflow-agent`

The wheel also includes the curated `leanflow_skills` guidance and
`leanflow_specs` workflow contracts used at runtime.

## Development

Create the repo venv and install editable deps:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e '.[dev]'
```

Run the focused LeanFlow tests:

```bash
source .venv/bin/activate
python -m pytest tests/leanflow -q -n 0
```

Recommended broader verification for the supported kernel:

```bash
source .venv/bin/activate
python -m pytest tests/leanflow tests/agent/test_prompt_builder.py tests/agent/test_context_compressor.py tests/test_run_agent.py tests/test_run_agent_codex_responses.py tests/test_windows_installer_links.py -q -n 0
```
