# Prover workflow

`prove` and its `autoprove` alias use a deterministic Lean controller around a
native Codex, Claude Code or Lemex session. Other workflows use the same native
backend boundary and their own source-acceptance gates.
The [research note](prover-redesign-research.md) explains the paper evidence and
design choices; this page describes the implemented runtime.

## Start a run

```bash
# One prover, direct proof attempts.
LEANFLOW_PROVER_MODE=standard leanflow workflow prove Main.lean

# Default: research planning and up to two concurrent prover jobs, bottom-up.
leanflow workflow prove Main.lean

# An explicit research concurrency limit.
leanflow workflow prove Main.lean --research --research-workers 3

# Apply the same workflow to discovered project source files.
leanflow workflow prove

# Keep research local to the supplied project and installed libraries.
leanflow workflow prove Main.lean --clean-room
```

`--agents N` with `N > 1` is a compatibility spelling for research proving
with that concurrency. `--no-parallel` limits proving to one job while retaining
research planning. Provider, model, prompt and axiom options remain available.
Clean-room and no-internet execution support Codex, Claude Code and Lemex through
local controller tools while preserving native model connections. Global and
role call limits are available below; each native CLI invocation counts as one
call. The old per-request API settings are rejected with migration guidance.

## What each mode does

**Standard** inspects the requested source, records its literal proof holes and
initial plan, then runs one prover job at a time. The controller selects the
next declaration and verifies submitted replacements. The prover can write local
`have` statements inside its assigned proof, search Lean libraries, and use warm
LeanProbe feedback. Those local obligations share the same session and budget.
There is no standing advisor, decomposer agent, or model-based manager loop.
The prover can request a separate resource agent for a specific external-source
or computation question. Its native invocation consumes the same global call
allowance. It returns
evidence and artifact paths; it does not become a proof-strategy advisor.

**Research** adds separate model sessions for an informal outline, DAG design,
and semantic review. The planner may search general mathematical sources,
download resources, and run restricted arithmetic experiments. It does not have
the prover's Lean search/check tools. The controller validates proposed IDs,
statements, dependencies, acyclicity, and module locations, then independently
compiles helper skeletons before scheduling proof work. It rejects helpers that
merely rename an ancestor statement after removing irrelevant formatting. This
textual check does not establish semantic equivalence or certify that every DAG
edge implies its parent; the independent review and later proofs remain necessary.
Rejected proposals receive
reconstruction attempts with the previous critique within any configured global limits.

Normal jobs have native tools and network access. Clean-room and no-internet jobs
disable native research tools and use only the controller's local tool surface.
Bridge commands run inside an OS sandbox that denies network access. Local Lean
search, project reads, installed libraries and proof checks remain available;
external web research and remote Lean search do not. The model connection remains
available so each runtime can authenticate and generate responses normally.

The model proposes a graph; deterministic code schedules it. Each node stores its
statement, informal justification, dependencies, module/file, source locations,
revision, attempts, and status. New helper declarations use separate modules under
`LeanFlowProofs/`. The controller alone edits the shared plan, graph, and canonical
proof source. Each job receives a separate workspace and a snapshot of the plan
and DAG. Planning, construction, and review start with fresh model histories.

Private project snapshots and resumed run copies use copy-on-write cloning on
APFS and supporting Linux filesystems. They share unchanged disk blocks while
retaining separate inodes and independent writes; they never hardlink canonical
proof files. Copies publish only after completion, and a failed copy removes its
staging files. On filesystems without cloning, ordinary copies require enough
free space to retain 256 MiB for checkpoints.
After each prover, planning, or review session, its copied library dependencies
are released. Proof sources, scratch files, results, and checkpoints remain.
Resuming restores the pinned dependencies with independent copy-on-write files;
changed dependency revisions require explicit repair rather than silent replacement.

The default scheduling order is **bottom-up**, proving prerequisites before
dependent goals. **Top-down DFS** remains selectable. Research mode can work on
separate root trajectories concurrently and deduplicates shared dependencies.
A submitted proof relying on unfinished planned dependencies is
stored as an **untrusted candidate**. It is not an independently verified
conditional theorem. The controller checks it again after all planned dependencies
close; it never treats an allowed `sorryAx` as completion.
**Bottom-up DFS** remains available explicitly: prerequisites are independently
proved before dependent jobs start.

Failed jobs retain their notes and scratch work. Concrete progress can receive
additional native invocations within the configured global limits. A changed report or a claimed promising direction
alone does not earn a restart; retain concrete edits inside the assigned scratch
proof holes. Research recovery investigates contrary evidence or
smaller subproblems, then replans the affected graph region. Existing proved nodes
and original target statements remain immutable; changed helpers receive new IDs.
Results from an obsolete node revision are rejected. Optional global limits also cover recovery, so an unresolved run can stop with
useful saved evidence.

Submitting a proof does not end a pass when independent feedback rejects it.
Malformed or invalid submissions return concrete feedback to the same prover
session so it can repair them within the same native conversation. The controller still
rechecks successful submissions before committing canonical source. It captures
the original elaborated target types before planning, compares them after import
changes, and independently inspects the freshly compiled candidate artifact.
The comparison also covers referenced local definition types and fixed bodies;
explicitly authorized missing definitions may still be filled. A term submitted
for a proof hole cannot append declarations or unscoped commands. Accepted source
modules are rebuilt before dependent jobs can import them. Top-down
submission feedback may check elaboration with planned holes; the resulting
candidate remains untrusted until the later strict gate.

For supported theorem contexts, recovery can dispatch a separate exact-negation
prover pass. The controller verifies the negation independently. A certified
refutation of an original target ends with a disproof outcome without changing
that target; a refuted invented helper triggers branch repair. The adapter declines
ambiguous ambient section-variable contexts rather than constructing a potentially
vacuous negation. An experiment or diagnostic report alone cannot certify disproof.

Negation candidates replace a literal `sorry` inside an existing `:= by` block:
submit tactics without another leading `by`, or use `exact (<proof term>)`.
The controller checks the exact rendered source and returns rejected candidates
to the same native job for repair. If checking is
interrupted, the pending candidate is retained and checked again on resume
before another native invocation, provided the global time limit permits checking. Checker
diagnostics are retained for subsequent recovery decisions even when no model notes were
submitted. A failed refutation leaves the claim unresolved; it does not establish
truth. Replanning can replace an unproved generated helper with a fresh helper
and update its dependents without first certifying a negation.

Existing-node plan updates should include only the existing ID and changed
dependencies or informal justification. Omit the statement to retain its exact
bytes. A copied-statement mismatch identifies the node and the first difference;
original target statements remain immutable.

## Source and verification boundaries

The controller snapshots original project source. It accepts replacements only
inside the discovered literal `sorry` spans and checks that protected source still
matches before committing. Provers edit private scratch files, not canonical
files. Controller-generated imports, helper modules, and additive Lake declarations
are recorded separately as reviewable changes. Concurrent user edits cause a
source conflict rather than being silently overwritten.

Definition holes require `LEANFLOW_PROVER_FILL_DEFINITIONS=1`; discovery reports
unauthorized definition holes instead of silently filling them. This is an
explicit authorization to fill their values; the surrounding definition is still
protected. Missing values may affect reduction and should be completed before
dependent proofs.

Warm LeanProbe workers provide inner-loop feedback. They run under a real OS write
boundary: macOS requires `sandbox-exec`; Linux requires Bubblewrap with usable
user namespaces. Unsupported isolation fails closed. Workers can write their
scratch/cache locations; they cannot edit original project source. This local
boundary is separate from the optional whole-workflow container sandbox.

A controller-owned check independently validates a submitted declaration and its
complete axiom profile before inserting the proof. The default allowed axioms are
`propext`, `Classical.choice`, and `Quot.sound`. User-specified axiom policies are
supported; `sorryAx` is never a permitted completion axiom. A final gate requires
the requested source files to be free of `sorry` and runs `lake build` for the
project. With a file target, unrelated files are not automatically added to the
proof assignment; a project-wide run discovers all eligible project source files.

Lean `maxHeartbeats` exhaustion is reported as `lean_heartbeat_limit` and remains
a proof-repair error. Actual process deadlines, cancellation, and unavailable
verification infrastructure retain their separate failure and resume handling.

Source protection also means `prove` cannot repair an arbitrary broken statement,
import, or definition outside an authorized hole. Make that correction separately
or use the appropriate editing workflow.

## Research resources and libraries

Research tools issue bounded-result searches against arXiv and DuckDuckGo
without extra model summarization. Repeated searches are not limited by a hidden
two-call allowance. Public resource downloads have
timeouts, redirect and byte limits, public-address validation, and provenance:
original URL, final URL, retrieval time, content hashes, and local artifact paths.
HTML is reduced deterministically; source bytes and PDFs can be retained.
Fresh stages receive a bounded inventory of prior downloaded resources with exact
readable paths and content hashes. Sharing requires a fetch-owned receipt in the
private runtime directory as well as matching metadata and file content. Inventory
scans hash at most 32 MiB and mark omitted resources explicitly. These files can be
reused without another download; private proof scratch, job notes, and controller
files remain isolated.
Model file tools cannot write into the downloaded resources directory. Older
downloads without a private receipt remain local evidence but need a new fetch
before another job can receive access; a receipt is never inferred from metadata
written by a model.
Relative tool paths use the current job workspace. Project source reads use the
supplied project root, and download filenames are basenames without directories.

Computation is a restricted integer/rational Python experiment with no filesystem
or network access. Its output is mathematical evidence, not a Lean certificate.
There is no unrestricted terminal tool in a prover session. Normal research can
retrieve public source pages; it is not a general repository checkout service.

A reviewed plan can request additional Lake libraries using explicit names,
public HTTPS Git URLs, and full immutable commit hashes. The controller appends
requirements, runs a bounded named `lake --keep-toolchain update`, and checks
requested pins and unchanged existing locks. Configuration changes are recorded.
Requests already satisfied by an exact locked local checkout are read-only no-ops,
including in online and clean-room campaigns. A repository name such as `mathlib4`
can identify the installed `mathlib` package only when its URL and revision match
one unambiguous locked checkout. New or changed dependencies still require normal
admission. Prefer `libraries: []` when existing dependencies suffice.
Installation uses only the remaining optional global campaign deadline, without
a separate 600-second ceiling. Invalid runtime configuration is an environment
failure; it preserves the reviewed draft rather than requesting another proof plan.
On failure it restores the exact lakefile, manifest, and toolchain bytes; downloaded
package caches may remain and require resynchronization. Custom package layouts
and conflicting dependency requirements are rejected for manual resolution.

`--clean-room` restricts research to the supplied project and installed libraries,
including general mathematical research. It blocks external web access, repository
installation and remote Lean search. Optional labels add task/sibling-solution
path filtering. Each native invocation is ephemeral and excludes previous native
sessions, memories and automatic context. LeanFlow can resume its own clean-room
checkpoint without importing an old native conversation. Online campaigns and
legacy checkpoints with unknown research provenance cannot be resumed as
clean-room tests. Firecrawl is not installed or enabled by this runtime.

## Budgets and context

The runtime owns model requests, tool history, sampling, compaction and retries.
LeanFlow supplies immutable assignments and job-bound MCP operations, using native
session IDs for normal online continuation. Restricted invocations start fresh;
native compaction still operates within each invocation. LeanFlow never overrides
temperature/top-p or invents a context window.
Default model and reasoning effort are inherited; explicit overrides are optional.

Global resource limits default to unset. Zero means exhausted, and negative,
non-finite or boolean limits are invalid. The first three settings apply to
every role together; the remaining settings further limit one role group:

| Environment setting | Default | Meaning |
| --- | --- | --- |
| `LEANFLOW_PROVER_AGENT_CALLS` | unset | Maximum native process invocations |
| `LEANFLOW_PROVER_TOKENS` | unset | Reported-token threshold for admitting future invocations |
| `LEANFLOW_PROVER_WALL_TIME_S` | unset | Cumulative active campaign time, including tools and verification |
| `LEANFLOW_PROVER_PROVER_SESSIONS` | 150 | Prover invocations across all goals, retries and resumes |
| `LEANFLOW_PROVER_ORCHESTRATOR_SESSIONS` | 50 | Shared planning, review, research and recovery invocations |
| `LEANFLOW_PROVER_NEGATION_SESSIONS` | unset | Refutation invocations across the campaign |

The same defaults can be configured as `limits.agent_calls`, `limits.tokens` and
`limits.wall_time_s` in LeanFlow configuration. There is no three-minute or
twenty-minute response, job, command, or Lean-check wall-time cutoff.
The three role-call allowances are additional ceilings within the global
allowance. Prover and Orchestrator cards appear only in Research mode; the Live
view shows spending. An absent setting uses the default, while an explicitly
empty role limit selects Unlimited. Saved campaigns retain their original caps.

One successfully spawned native agent process is one call. Its internal requests,
retries and compaction are owned by that runtime. Admission is reserved before
spawn so parallel workers cannot take the same last call. Failed process creation
is refunded; interrupted started processes retain their charge. A queued Lean
operation does not use an extra agent call, and polling never restarts the work.

Active wall time survives resume. Parallel workers share the controller clock;
time between stopped controllers is excluded. A global deadline cancels native
process trees and active controller operations and prevents promotion afterward.

Usage comes from native structured events. Cumulative counters are normalized to
invocation deltas. Missing usage remains unknown, never zero. A token ceiling
stops new invocations once reported usage reaches it, and may be exceeded by an
already running invocation. Unknown completed usage stops further token-capped
admission. These are agent-invocation and observed-token limits, not a count of
hidden native model requests or a hard service-side token quota.

Additional configuration remains focused on Lean orchestration:

| `LEANFLOW_PROVER_` suffix | Default | Meaning |
| --- | --- | --- |
| `MODE` | `research` | Research planning/review or Standard direct proving |
| `SEARCH_ORDER` | `bottom-up` | Bottom-up prerequisites or top-down candidates |
| `PARALLELISM` | 2 in research, 1 in standard | Concurrent prover jobs |
| `MAX_NODES` | 128 | Structural DAG bound |
| `MAX_RESTARTS` | unset | Additional passes per goal in Standard mode |
| `PLAN_REFINEMENTS` | unset | Accepted changes of mathematical direction in Research mode |
| `MAX_DECOMPOSITIONS` | unset | Research recovery decisions, including retry, continue, refutation and decomposition |
| `PROVIDER` | selected backend | `codex`, `claude` or `lemex` |
| `ORCHESTRATOR_PROVIDER` | prover backend | Optional native planning/review backend |
| `MODEL`, `ORCHESTRATOR_MODEL` | native defaults | Explicit model overrides |
| `REASONING_EFFORT`, `ORCHESTRATOR_REASONING_EFFORT` | native defaults | Explicit effort overrides |
| `FILL_DEFINITIONS` | false | Authorize filling existing definition holes |
| `ALLOWED_AXIOMS` | propext, Classical.choice, Quot.sound | Strict completion policy |

The optional controller limits accept an empty value for unlimited and zero to
disable additional work of that kind. The initial research plan is not a
refinement, and routine notes do not consume a direction change. Saved limits
and counters survive resume. The builtin `research` profile selects Research
and two concurrent provers; an explicit mode selection takes precedence over
legacy research-profile flags.

RCP is configured through Lemex itself:

```bash
export LEMEX_BASE_URL="https://inference.rcp.epfl.ch/v1"
export LEMEX_API_KEY="..."
leanflow workflow prove Main.lean --provider lemex --research
```

Native homes and credentials remain native. Old `BASE_URL`, `API_KEY_ENV`,
`TIMEOUT_S`, per-role API budgets, `CONTEXT_TOKENS`, `MODEL_CONTEXTS` and
`COMPRESSION*` settings are rejected. Existing artifacts remain readable, but
start a new native run when migrating the old provider engine.

The native process works in a filtered private copy. MCP `get_assignment` and
`list_operations` recover controller facts after native compaction. Accepted
submissions produce durable independent receipts; malformed reports and Lean
errors return feedback within the same native conversation. Pending operations
finish before the controller interprets a completed native response.

## Inspect progress and resume

Each run prints its ID and saves:

```text
.leanflow/workflow-state/prover/<run-id>/
├── PLAN.md                 # controller-owned informal plan and findings
├── DAG.json                # theorem dependencies and statuses
├── state.json              # coherent graph, jobs, metrics, and changes snapshot
├── source.json             # protected source and accepted replacements
├── source-transaction.json # temporary accepted-proof installation journal
├── events.jsonl            # controller events
├── inbox.jsonl             # queued user guidance
├── baselines/              # original views for file diffs
└── jobs/
    ├── .runtime/<job-id>/   # protected admission ledger and scratch baseline
    └── <job-id>/
        ├── Scratch.lean    # assigned proof context, for theorem jobs
        ├── PLAN_job.md     # durable local proof notes
        ├── candidate.txt   # optional submitted hole replacement
        ├── report.json     # structured result and usage
        ├── events.jsonl    # separate transcript/tool log
        └── resources/      # downloaded evidence, when used
```

```bash
leanflow runs prover <run-id> --json
leanflow runs prover-message <run-id> --message "Try the compactness argument."

# Join one compact activity row to the full model output in the job transcript.
leanflow runs event <run-id> <event-id> --json

# Resume the same target with its saved configuration and spending.
LEANFLOW_PROVER_RESUME_RUN_ID=<run-id> leanflow workflow prove Main.lean
```

Resume creates a **new run ID** with `resumed_from` / `parent_run_id` lineage.
The prior run's records remain available. Source snapshots are reconciled; saved
global usage, active time and interrupted-job admission ledgers are retained. A resume uses the saved target scope; an explicitly different target is rejected. Missing interrupted-job budget evidence is treated
conservatively as spent capacity.

Planning and research retain bounded report context in controller-owned state,
including completed tool exchanges, so an interrupted report handoff keeps its
findings. Successful reports are reused without spending unused recovery calls.
Rejected planning attempts retain their cursor and are not charged or checked
again on resume. A `planning_stalled` checkpoint requires inspecting the saved
proposals and gate reports, correcting the cause, and deliberately clearing its
stall marker before resuming; plain resume spends no new model calls.

Accepted proof installation records exact before/after source hashes and a
committed-state marker. Resume rolls back an interrupted uncommitted installation
and retains the candidate for independent rechecking. Source content that matches
neither journal image is treated as a conflict; the controller does not silently
take a new baseline.

Helper materialization also records the proposal's exact transaction ID. If its
source and graph committed before acceptance bookkeeping was interrupted, resume
finishes that acceptance and charges its refinement once. Older checkpoints
without this marker cannot establish that an ambiguous acceptance completed and
may require manual inspection.

Generated helper dependencies are stored as removable controller-owned imports,
separate from the original import baseline. Source-consistency failures retain
the planning checkpoint and stop for repair; they do not request a different
mathematical decomposition. After repair, resume reuses the completed proposal
and review while repeating independent source and kernel checks.

Failure reports retain bounded, redacted exception causes and safe provider
identifiers through run and campaign history. Controller cancellations are
reported as interruptions; internal errors retain their own status. A transport
wrapper's message alone does not establish why the remote backend failed.

The VS Code Live view shows the selected run's dependency tree, shared-node links,
statement details, prerequisites and dependents, proof plan, jobs, and metrics.
Click a theorem to open its source location, a job to inspect its scratch proof
or log, or a changed file to open the source or its baseline diff. Plan and DAG
files are directly accessible. The view refreshes periodically while active and
keeps terminal runs inspectable. Launch controls expose the common prover settings;
the Knobs view exposes additional catalogued settings.
Nested research jobs, result reports, resume lineage, startup errors and suggested
next steps are visible. Certified disproofs link to their exact evidence. Cost
totals are labelled partial when some job costs are unavailable.

Guidance is saved in the selected run's inbox. Orchestrator guidance is folded
into the shared plan and delivered to planning sessions; addressed job guidance
is loaded at that job's next model-request boundary. Delivery offsets persist
across resume. Guidance does not interrupt a request already in progress.

Exit codes: `0` means verified completion; `2` means unresolved/resumable work;
`3` means an independently certified disproof outcome; `1` indicates
runtime or final-verification failure; `130` indicates interruption. A candidate
or model success message cannot produce exit `0`.

## Current limits

- The graph is a prerequisite DAG, not a complete AND-OR strategy search tree.
  Top-down candidates are saved without a proof-expression audit and gain no
  trusted status before strict independent checking.
- Research questions can run concurrently in bounded batches. Planning and
  branch repair still occupy the controller: other prover results may finish
  meanwhile, but acceptance waits for that controller phase to return.
- New helper modules cannot import an original goal module, which would create a
  circular import. Use local proof obligations when the necessary definitions are
  local to that goal file. Nonstandard source layouts may require manual setup.
- Anonymous declarations need names before proving. Exact-negation certification
  deliberately supports only contexts it can reconstruct without ambiguity.
- The direction/decomposition distinction includes semantic reviewer judgment.
  Configured global calls/tokens/time and the structural graph bound remain
  independent of that classification.
- Multi-file helper construction and accepted-proof installation use durable
  journals. Recovery checks every recorded source image before rollback and
  refuses to overwrite outside edits. Dependency package caches from an interrupted
  external install can still require project-specific cleanup.
- There is no standing advisor or separate model-backed decomposition engine.
  Native coding tools are available inside the private project boundary;
  controller MCP operations provide durable long-running checks and commands.
- Legacy native queue routing and human-review flags are not additional control
  policies for this runtime. The new runtime reports unresolved work with saved
  evidence when it cannot progress within its configured limits.

The original papers' benchmark gains have not been reproduced for LeanFlow.
Scheduling order and optional budgets are configurable choices for measurement,
not claims of universally optimal proof-search behavior.

Validation separates scripted provider/runtime tests, actual local Lean checks,
and static external review. It has not established pass rates, token savings,
or sustained performance on a representative set of hard research problems.
Dependency-install transaction tests use mocked external execution; arbitrary
third-party dependency combinations still need project-specific validation.

The historical [validation record](prover-redesign-validation.md) reports checks
for the earlier prover engine; it is not evidence for this native migration. The live run exercises provider,
tool, and verification integration on a small fixture; hard-problem effectiveness
and comparative efficiency require broader measurement. The later
[independent live experiments](prover-independent-live-validation.md) record two
completed Putnam problems, a real parallel helper DAG, and specific planning
inefficiencies found and repaired during observation.

### Repairing stage reports

Planning outlines, graph proposals, and reviewer reports are checked before their session finishes. Format and graph-structure failures return precise feedback to the same conversation, preserving its draft, tool history, and remaining call budget. Rejected reports are not cached as completed; interrupted repairs restore the saved conversation. These repairs stay in the same native invocation; another process launch consumes another global agent call. A valid negative mathematical review remains a review rejection, and proof/Lean verification gates still apply.
