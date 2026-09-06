# Prover workflow

`prove` and its `autoprove` alias use a dedicated bounded controller. This
redesign does not change `formalize`, `draft`, `review`, `refactor`, or `golf`.
The [research note](prover-redesign-research.md) explains the paper evidence and
design choices; this page describes the implemented runtime.

## Start a run

```bash
# One prover, direct proof attempts.
leanflow workflow prove Main.lean

# Research planning and up to two concurrent prover jobs.
leanflow workflow prove Main.lean --research

# An explicit research concurrency limit.
leanflow workflow prove Main.lean --research --research-workers 3

# Apply the same workflow to discovered project source files.
leanflow workflow prove
```

`--agents N` with `N > 1` is a compatibility spelling for research proving
with that concurrency. `--no-parallel` limits proving to one job while retaining
research planning. Provider, model, prompt, axiom, and clean-room launch options
remain available. Use the `LEANFLOW_PROVER_*` settings below for the new runtime;
legacy queue/advisor budgets do not control these jobs.

## What each mode does

**Standard** inspects the requested source, records its literal proof holes and
initial plan, then runs one prover job at a time. The controller selects the
next declaration and verifies submitted replacements. The prover can write local
`have` statements inside its assigned proof, search Lean libraries, and use warm
LeanProbe feedback. Those local obligations share the same session and budget.
There is no standing advisor, decomposer agent, or model-based manager loop.
Once per prover job, the prover can request a separate resource agent for one
specific external-source or computation question. That agent has its own bounded
allocation, defaulting to 40 calls, charged to the campaign total. It returns
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
bounded reconstruction attempts with the previous critique.

The model proposes a graph; deterministic code schedules it. Each node stores its
statement, informal justification, dependencies, module/file, source locations,
revision, attempts, and status. New helper declarations use separate modules under
`LeanFlowProofs/`. The controller alone edits the shared plan, graph, and canonical
proof source. Each job receives a separate workspace and a snapshot of the plan
and DAG. Planning, construction, and review start with fresh model histories.

The default scheduling order is **bottom-up DFS**: prerequisites are independently
proved before dependent jobs start. Research mode can work on separate root
trajectories concurrently and deduplicates shared dependencies. **Top-down** is
experimental: a submitted proof relying on unfinished planned dependencies is
stored as an **untrusted candidate**. It is not an independently verified
conditional theorem. The controller checks it again after all planned dependencies
close; it never treats an allowed `sorryAx` as completion.

Failed jobs retain their notes and scratch work. Concrete progress can receive up
to three additional passes. A changed report or a claimed promising direction
alone does not earn a restart; retain concrete edits inside the assigned scratch
proof holes. Research recovery investigates contrary evidence or
smaller subproblems, then replans the affected graph region. Existing proved nodes
and original target statements remain immutable; changed helpers receive new IDs.
Results from an obsolete node revision are rejected. Finite limits also bound
recovery, so an unresolved run can stop with useful saved evidence.

Submitting a proof does not end a pass when independent feedback rejects it.
Malformed or invalid submissions return concrete feedback to the same prover
session so it can repair them with its remaining calls. The controller still
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

Source protection also means `prove` cannot repair an arbitrary broken statement,
import, or definition outside an authorized hole. Make that correction separately
or use the appropriate editing workflow.

## Research resources and libraries

Research tools issue bounded searches against existing arXiv and DuckDuckGo
providers without extra model summarization. Repeated identical searches are
blocked after two results without a scratch edit. Public resource downloads have
timeouts, redirect and byte limits, public-address validation, and provenance:
original URL, final URL, retrieval time, content hashes, and local artifact paths.
HTML is reduced deterministically; source bytes and PDFs can be retained.

Computation is a restricted integer/rational Python experiment with no filesystem
or network access. Its output is mathematical evidence, not a Lean certificate.
There is no unrestricted terminal tool in a prover session. Normal research can
retrieve public source pages; it is not a general repository checkout service.

A reviewed plan can request additional Lake libraries using explicit names,
public HTTPS Git URLs, and full immutable commit hashes. The controller appends
requirements, runs a bounded named `lake --keep-toolchain update`, and checks
requested pins and unchanged existing locks. Configuration changes are recorded.
On failure it restores the exact lakefile, manifest, and toolchain bytes; downloaded
package caches may remain and require resynchronization. Custom package layouts
and conflicting dependency requirements are rejected for manual resolution.

`--clean-room` retains general mathematical research while blocking repository
installation and prohibited task/sibling-solution retrieval through the research
and source-search policy. Optional labels help identify the benchmark. Firecrawl
was evaluated but is not installed or enabled by this runtime. Its keyless service
limits and the proposed setup command are discussed in the research note.

## Budgets and context

All settings below use the `LEANFLOW_PROVER_` prefix. They appear in
`leanflow flags`, editor launch controls, or the full editor Knobs catalog.

| Setting suffix | Default | Meaning |
| --- | --- | --- |
| `MODE` | `standard` | `standard` or `research` |
| `SEARCH_ORDER` | `bottom-up` | `bottom-up` or experimental `top-down` |
| `JOB_API_CALLS` | `300` | Request ceiling for one prover or exact-negation pass |
| `MAX_RESTARTS` | `3` | Additional passes per node |
| `PLAN_REFINEMENTS` | `16` | Changes of informal proof direction |
| `PARALLELISM` | `2` | Concurrent research-mode provers; standard uses one |
| `TOTAL_API_CALLS` | `10000` | Total admitted requests across roles and passes |
| `ORCHESTRATOR_API_CALLS` | `40` | Per planning, review, or resource-agent session, including a prover's research request |
| `MAX_NODES` | `128` | Maximum admitted DAG nodes |
| `MAX_DECOMPOSITIONS` | `32` | Structural recovery limit, separate from direction changes |
| `WALL_TIME_S` | `14400` | Campaign wall-clock limit, retaining recorded elapsed time on resume |
| `TIMEOUT_S` | `180` | Provider request and independent Lean-check deadline |
| `MODEL` | launch model | Prover model override |
| `ORCHESTRATOR_MODEL` | prover model | Planning, review, and research model override |
| `CONTEXT_TOKENS` | `64000` | Prover context estimate, including output reserve |
| `ORCHESTRATOR_CONTEXT_TOKENS` | `64000` | Planning/research context estimate |
| `COMPRESSION` | `1` | Deterministically compact prover history |
| `ORCHESTRATOR_COMPRESSION` | `1` | Deterministically compact planning/research history |
| `FILL_DEFINITIONS` | `0` | Authorize replacing definition holes |
| `ALLOWED_AXIOMS` | `propext,Classical.choice,Quot.sound` | Allowed completion axioms |

For example:

```bash
LEANFLOW_PROVER_JOB_API_CALLS=120 \
LEANFLOW_PROVER_TOTAL_API_CALLS=1500 \
LEANFLOW_PROVER_PLAN_REFINEMENTS=4 \
leanflow workflow prove Main.lean --research

leanflow flags show LEANFLOW_PROVER_JOB_API_CALLS
leanflow flags effective --changed
```

The runtime reserves campaign capacity before dispatching jobs and writes each
job's admitted request count before sending a provider request. Failed and
interrupted requests consume their admission. Provider retries cannot silently
mint new calls. A local `have`, context compaction, or resumed scratch job does not
reset its pass budget. A new restart has a new pass allocation and still consumes
the campaign total. Routine plan notes do not consume a direction refinement;
node, decomposition, total-request, and time limits prevent endless “only splitting.”
Branch replans are classified as a change of direction or a structural
decomposition. An accepted review classification controls whether a direction
refinement is charged; a certified-refutation repair reserves its refinement
before replanning. Direction changes require remaining refinement capacity before
their source changes are admitted.

Compression makes no model calls. It keeps the system contract, selected skill guidance, original assignment
with PLAN/DAG, durable addressed user guidance, current `PLAN_job.md`, and recent
complete tool exchanges. Large feedback remains valid JSON with explicit
truncation and a readable artifact containing the full result. Context
size uses a conservative estimate rather than the provider's exact tokenizer; if
pinned context does not fit, the session saves its work and returns `context_limit`.
Token counts use reported usage. Missing monetary costs remain unknown, not zero;
a sum of reported costs may be partial (`metrics.cost_complete` records whether
all jobs supplied costs).

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

# Resume the same target with its saved configuration and spending.
LEANFLOW_PROVER_RESUME_RUN_ID=<run-id> leanflow workflow prove Main.lean
```

Resume creates a **new run ID** with `resumed_from` / `parent_run_id` lineage.
The prior run's records remain available. Source snapshots are reconciled; saved
budgets and interrupted-job admission ledgers are retained. A resume uses the saved target scope; an explicitly different target is rejected. Missing interrupted-job budget evidence is treated
conservatively as spent capacity.

Accepted proof installation records exact before/after source hashes and a
committed-state marker. Resume rolls back an interrupted uncommitted installation
and retains the candidate for independent rechecking. Source content that matches
neither journal image is treated as a conflict; the controller does not silently
take a new baseline.

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
  Total requests, graph size, recovery count, and elapsed time provide independent
  deterministic ceilings regardless of that classification.
- Multi-file helper construction and accepted-proof installation use durable
  journals. Recovery checks every recorded source image before rollback and
  refuses to overwrite outside edits. Dependency package caches from an interrupted
  external install can still require project-specific cleanup.
- There is no standing advisor, generic shell, model-backed decomposer, or PDF
  interpretation service in the new session tool surface. Prefer a paper's HTML
  source when the model needs readable content.
- Legacy native queue routing and human-review flags are not additional control
  policies for this runtime. The new runtime reports unresolved work with saved
  evidence when it cannot progress within its finite limits.

The original papers' benchmark gains have not been reproduced for LeanFlow.
Scheduling order and default budgets are configurable choices for measurement,
not claims of universally optimal proof-search behavior.

Validation separates scripted provider/runtime tests, actual local Lean checks,
and static external review. It has not established pass rates, token savings,
or sustained performance on a representative set of hard research problems.
Dependency-install transaction tests use mocked external execution; arbitrary
third-party dependency combinations still need project-specific validation.

The [validation record](prover-redesign-validation.md) reports the full quality
gate and actual Codex/Lean smoke evidence. The live run exercises provider,
tool, and verification integration on a small fixture; hard-problem effectiveness
and comparative efficiency remain unmeasured.
