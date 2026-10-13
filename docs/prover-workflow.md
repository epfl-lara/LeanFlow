# Prover workflow

`prove` and its `autoprove` alias complete existing Lean proof holes. Native
agents work in private scratch files; the controller owns scheduling, canonical
source changes, and independent verification.

## Start a run

```bash
leanflow workflow prove Main.lean
leanflow workflow prove Main.lean --research-workers 3
leanflow workflow prove                         # discover project proof holes
leanflow workflow prove Main.lean --clean-room
```

New runs default to Research mode, bottom-up order, and two concurrent provers.
Choose Standard in VS Code or set `LEANFLOW_PROVER_MODE=standard` for direct
sequential proving. `--agents N` is a compatibility spelling for research
concurrency; `--no-parallel` retains research planning with one prover.
Provider, model, prompt, and axiom options are available through the launcher.

## Modes and scheduling

**Standard** records the requested source and proof holes, then runs one prover
job at a time. The prover searches Lean libraries, uses LeanProbe feedback, and
writes proof attempts in its assigned scratch file. It can request a separate
resource session for a concrete research or computation question.

**Research** adds fresh native sessions for an informal outline, a theorem
dependency graph, and semantic review. The controller validates graph structure
and independently compiles helper skeletons before dispatch. Helpers normally
live in modules under `LeanFlowProofs/`. Each prover receives its own workspace
and a snapshot of the plan and graph.

Bottom-up scheduling proves prerequisites before dependent goals. Top-down
scheduling is also available:

```bash
LEANFLOW_PROVER_SEARCH_ORDER=top-down leanflow workflow prove Main.lean
```

Top-down proofs relying on unfinished helpers are retained as untrusted
candidates. They gain verified status only after their dependencies close and
strict independent checks pass. Shared dependencies are represented once.

Rejected submissions return concrete feedback within the same native session.
Failed jobs retain notes, diagnostics, and scratch work. Research recovery can
continue, retry, replan, decompose, or attempt an exact negation while limits
permit. Original targets and accepted proofs remain fixed; changed helpers
receive new revisions, and obsolete job results cannot be integrated.

For supported contexts, an independently checked proof of the exact negation
can certify disproof. Refuting an original target produces a disproof outcome;
refuting an invented helper triggers branch repair. Failed search, computation,
and model reports do not certify disproof.

## Source and verification boundaries

The controller snapshots original source and accepts replacements only inside
authorized literal `sorry` spans. It checks protected bytes, original elaborated
target types, and referenced fixed definitions before integration. Reviewed
helper modules, imports, and additive Lake requirements are recorded separately.
Concurrent outside edits cause a source conflict rather than being overwritten.
Provers can request additional installed modules with an `imports` list in their
submission JSON or scratch header. The controller checks that these imports
preserve protected kernel types and installs them only with an accepted proof.
Saved import-dependent candidates are rechecked on resume before new model work.

Definition holes require `LEANFLOW_PROVER_FILL_DEFINITIONS=1`. This authorizes
only their missing values; surrounding definitions remain protected. Arbitrary
broken statements, imports, and bodies outside authorized holes need a separate
editing workflow.

Native tools and Lean workers run inside enforced private filesystem boundaries:
macOS requires `sandbox-exec`; Linux/WSL requires Bubblewrap with usable user
namespaces. Unsupported isolation fails before model work. Controller state and
canonical source remain outside agent write authority. This boundary is separate
from the optional [whole-workflow container sandbox](sandbox-runtime.md).

Each prover and negation session checks LeanProbe again after creating or restoring
its private project. Local modules imported by the assignment are first rebuilt
from the private sources, with output confined to the private build cache.
With `LEANFLOW_PROJECT_BUILD_CACHE=1`, eligible fresh workspaces can first restore
previously controller-built local imports; Lake still checks their build traces.
See [the cache requirements](storage.md#local-project-imports) before enabling reuse.
Canonical build artifacts are excluded from these workspaces. This check uses the
same sandbox and worker as the job's `lean_check` tool and loads the assigned
scratch imports without requiring finished proofs. Failed setup stops before
model admission and consumes no new agent call.
Cancellation and global deadlines retain their interrupted or timeout outcomes.
Job activity records setup start and result; full diagnostics are retained in
`jobs/.runtime/<job-id>/lean-setup.json`. A successful check keeps the worker warm.

Foreground exact-file checks use independent Lean processes while retaining useful
search workers. `LEANFLOW_LEAN_WARM_RETENTION=auto` is the default; `reclaim`
selects the previous teardown behavior. `retain` requests foreground retention,
but low-memory and background-worker modes still reclaim resident services.
Module and project builds invalidate retained environments when their dependency
generation changes. Ordinary source edits preserve unchanged imported environments.
LeanFlow calls LeanProbe's public API directly; the backend owns declaration
parsing and transport. Generation checks and independent acceptance remain
separate from fast proof-search feedback.

Independent acceptance checks the exact compiled candidate and its complete axiom
profile. Default allowed axioms are `propext`, `Classical.choice`, and `Quot.sound`.
A custom policy is supported; `sorryAx` is always rejected for completion.
Accepted modules are rebuilt before dependent jobs import them.

Final success requires no `sorry` in the requested source files and a project
`lake build`. A file target limits the assignment to that file; unrelated build
failures can still block the final project gate. Lean heartbeat exhaustion,
missing verification infrastructure, interruption, and resource-limit outcomes
remain distinct.

## Research and clean-room execution

Normal research can retrieve mathematical sources and run restricted arithmetic
experiments. Downloads retain URLs, hashes, and artifact paths; validated
receipts allow later jobs to reuse them. Computation is evidence for planning,
not a Lean proof certificate.

A reviewed plan can request additional Lake libraries using explicit public
HTTPS Git URLs and full immutable commit hashes. The controller checks existing
locks and records additive changes. Failed updates restore configuration bytes;
package caches may still need repair. Exact already-installed requirements are
read-only no-ops. Conflicting pins or unsupported layouts require manual setup.

`--clean-room` and `LEANFLOW_PROVER_ALLOW_INTERNET=0` restrict research to project
files and installed libraries. External web retrieval, dependency installation,
and remote Lean search are disabled. Controller bridge commands run without
network access; native model connections retain authentication and defaults.

Clean-room invocations exclude prior native sessions, memories, and automatic
context. Optional clean-room labels add task/sibling-solution path filtering.
LeanFlow can resume its own clean-room checkpoint, but cannot reuse online or
legacy history with unknown research provenance. This restriction does not
remove knowledge learned during model training.

## Settings and limits

The selected native CLI owns model requests, tool history, retries, sampling, and
compression. Empty model and effort settings inherit its defaults. LeanFlow
records runtime-reported settings for each call, separately from requested
aliases and overrides; missing values stay unknown.

| Environment setting | Default | Meaning |
| --- | --- | --- |
| `LEANFLOW_PROVER_AGENT_CALLS` | 250 | Native invocations across all roles |
| `LEANFLOW_PROVER_TOKENS` | unset | Reported-token threshold for future calls |
| `LEANFLOW_PROVER_WALL_TIME_S` | 86400 (24 hours) | Cumulative active time, including checks |
| `LEANFLOW_PROVER_PROVER_SESSIONS` | 200 | Prover calls across goals, retries, and resumes |
| `LEANFLOW_PROVER_ORCHESTRATOR_SESSIONS` | 50 | Planning, review, research, and recovery calls |
| `LEANFLOW_PROVER_NEGATION_SESSIONS` | unset | Refutation calls across the run |

Global limits also use `limits.agent_calls`, `limits.tokens`, and
`limits.wall_time_s` in LeanFlow configuration. New runs default to 250 total
calls and 24 active hours; token usage is unlimited by default. Missing settings
use these defaults; explicit `null` configuration or empty environment values
select unlimited. Zero means exhausted. Role caps are additional ceilings within
the global cap, shared across every job in that role. Resume retains saved limits
unless new limit controls are explicitly supplied. Changing a limit preserves
spent calls and accumulated active time.

One native process (`codex exec`, `lemex exec`, or `claude -p`) is one agent call.
Internal model requests, retries, and compaction remain inside it. A new process
for recovery or continuation consumes another call. Admission is reserved before
spawn; an interrupted started process retains its charge. Lean checks and
operation polling do not consume extra agent calls.
Rejected submissions return feedback to the existing native conversation. An
orchestrator-selected retry or continuation preserves partial proof work, notes,
and the previous report. Role-cap exhaustion prevents new sessions in that role;
independent active provers finish before the controller reports exhaustion.

Active time survives resume and is shared across parallel jobs. Time between
stopped controllers is excluded. A global deadline cancels owned process trees
and checks and prevents source promotion after cancellation. There is no default
per-response, per-job, or Lean-check wall-time limit.

Token usage comes from native events. Missing completed usage remains unknown
and blocks another token-capped invocation. An active invocation can exceed the
threshold; this is not a hard service-side token quota. Costs with missing price
or usage coverage remain unavailable or explicitly partial.

Other settings use the `LEANFLOW_PROVER_` prefix:

| Suffix | Default | Meaning |
| --- | --- | --- |
| `MODE` | `research` | Research planning/review or Standard direct proving |
| `SEARCH_ORDER` | `bottom-up` | Prerequisites first or top-down candidates |
| `PARALLELISM` | 2 | Research prover concurrency; Standard uses one |
| `MAX_NODES` | 128 | Maximum admitted graph nodes |
| `MAX_RESTARTS` | unset | Additional Standard passes per goal |
| `PLAN_REFINEMENTS` | unset | Accepted changes of research direction |
| `MAX_DECOMPOSITIONS` | unset | Research recovery decisions |
| `PROVIDER`, `ORCHESTRATOR_PROVIDER` | selected backend | Optional role backend |
| `MODEL`, `ORCHESTRATOR_MODEL` | native defaults | Explicit role model |
| `REASONING_EFFORT`, `ORCHESTRATOR_REASONING_EFFORT` | native defaults | Explicit role effort |
| `FILL_DEFINITIONS` | false | Authorize existing definition holes |
| `ALLOW_INTERNET` | true | Allow external research outside clean-room mode |
| `ALLOWED_AXIOMS` | standard Lean axioms above | Completion axiom policy |

Use `leanflow flags` for installed defaults and editor-profile compatibility.
Native endpoints and credentials belong to the selected runtime. Former API-call
budgets, context/compression controls, and direct-provider settings are rejected
with migration guidance. See [native backends](product-reference.md#native-backends).

## Inspect progress and resume

Runs print an ID and retain a plan, graph, source baselines, transactions, events,
and per-job evidence under `.leanflow/workflow-state/prover/<run-id>/`.

```bash
leanflow runs prover RUN_ID --json
leanflow runs prover-message RUN_ID --message "Use the project guidance in proof-guidance.md."
leanflow runs event RUN_ID EVENT_ID --json
LEANFLOW_PROVER_RESUME_RUN_ID=RUN_ID leanflow workflow prove Main.lean
```

Resume creates a new ID with prior-run lineage and retains target scope,
configuration, spent calls, active time, notes, and scratch work. A different
explicit target is rejected. Interrupted source transactions are reconciled
against recorded before/after images; outside conflicts are never replaced with
an assumed new baseline.

Accepted reports are reused, and retained candidates can be checked before more
model work. A `planning_stalled` checkpoint requires inspecting its saved
proposals and gate reports and correcting the cause before clearing the stall
marker. Ordinary resume does not spend calls to repeat the same stalled plan.

The VS Code Live view shows dependencies, statements, plan, jobs, source links,
diffs, usage, and per-call model history. Submitted candidates remain distinct
from solved theorems. Queued guidance reaches the selected controller or job at
its next decision boundary and cannot bypass verification.

| Exit code | Outcome |
| --- | --- |
| `0` | Verified completion |
| `2` | Unresolved work with saved state |
| `3` | Independently certified disproof |
| `1` | Startup, runtime, or final-verification failure |
| `130` | Interruption |

## Current limits

The graph describes prerequisites, rather than a complete strategy search tree.
Planning and branch repair occupy the controller, so finished prover results may
wait for acceptance. Helper modules cannot import their original goal module if
that creates a cycle; local definitions may require local proof obligations.
Anonymous declarations need names, and exact-negation certification supports
only unambiguous contexts. Nonstandard dependency layouts can need manual repair.

Modes, scheduling, and budgets are configurable choices. They do not establish
comparative solve rates or efficiency on a particular theorem collection.


## Following agent activity

In VS Code, expand **Output and tool activity** under a prover or orchestrator.
Running jobs open this feed automatically. **Follow live** follows new messages
and tool progress; each native tool shows its description when known and elapsed
time when its start was recorded. Expanded rows present readable output, with
full recorded fields available for inspection.

**Reported reasoning** is optional native output. Public summaries and traces
are labelled separately; visibility follows the native runtime’s settings.
Stream fragments update one visible row, while the underlying observations keep
separate evidence IDs. Interrupted output remains partial. A message or successful
tool result does not establish proof acceptance; independent controller checks do.
