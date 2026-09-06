# Prover redesign: source checks and design decisions

Primary sources checked on 2026-09-06. This document records evidence and design
recommendations for `prove` / `autoprove`; it is not a claim that every recommendation
is implemented. The papers' benchmark results were not reproduced.

## What the papers establish

| Source | Relevant mechanism and limit |
| --- | --- |
| [Goedel-Architect, §§2, 4, Appendix A](https://arxiv.org/html/2606.06468v1) | Generates a global dependency blueprint, checks its statements and acyclicity, and proves nodes in parallel using declared dependency signatures. Failed nodes return a structured diagnosis; verified negations identify broken intermediate claims. Refinement can split hard nodes, repair statements, or change dependencies while retaining reusable proofs. PutnamBench uses 16 blueprint refinements, including decomposition; the paper does **not** establish that only changes of informal direction should consume budget. It does not compare a general top-down scheduler against bottom-up scheduling. |
| [LEAP, §§2.2–2.5, 5.3, Appendix C](https://arxiv.org/html/2606.03303v1) | Attempts direct proving, then constructs an informal blueprint and a compiler-checked parent proof whose only holes belong to proposed sublemmas. Its AND-OR DAG distinguishes alternative strategies from jointly required subgoals; search uses DFS with backtracking. A reviewer assesses whether decomposition is relevant and makes progress. Without that gate, an ablation repeatedly introduces an ancestor-equivalent goal. Typechecking alone therefore does not establish useful decomposition. This is evidence for a focused gate, not perpetual advice. |
| [AlphaProof Nexus, §2, §3 failure analysis, Appendix A.1](https://arxiv.org/html/2605.22763v1) | The cited paper's title is *Advancing Mathematics Research with AI-Driven Formal Proof Search*. User markers delimit editable blocks and values. Episodes retain a sketch and lessons; validation checks theorem integrity and rejects axiom injection. Goal caching hashes the exact formal context and target. Calls and episodes have explicit limits. Reported failures include moving the entire difficulty into one helper and inventing allegedly known results behind `sorry`. The evolutionary rating population is optional complexity; these results do not require LeanFlow to adopt it. |

These systems support different search organizations. Their published results do
not establish a universally best traversal order for LeanFlow's project workloads.

## Scheduling and proof states

Adopt **bottom-up completion by default**, with an explicit experimental top-down
attempt policy. This is an engineering choice: it gives the UI and proof importer
a simple, auditable meaning for “proved.” A configurable scheduler selects jobs;
the language model proposes mathematical changes, not queue order.

Use one documented edge convention: `node.dependencies` lists prerequisites.
Traverse from each requested target through that list. In bottom-up mode, dispatch
only nodes whose prerequisites are complete. In top-down mode, dispatch the target
first, record any accepted conditional sketch, and then descend into its unresolved
prerequisites. Select distinct target trajectories fairly at each scheduling round,
deduplicate shared prerequisites, and issue at most one active lease for a node and
its revision. Start with parallelism 2 in research mode and 1 in standard mode.

Do not equate scheduling direction with proof validity. A conditional parent proof
is useful progress but must remain `conditional` until its used dependencies are
complete. Suggested states are `open`, `running`, `conditional`, `proved`,
`disproved`, `blocked`, and `stale`; “budget exhausted” is a job outcome, not a
mathematical claim about the node.

Keep alternative approaches as separate plan revisions initially. A full AND-OR
search database can wait until measured workloads demonstrate its value. An
ordinary DAG with explicit revisions is simpler, provided an abandoned alternative
does not remain a required dependency.

Definitions with missing values need special treatment: complete them before
dependent proofs use their reduction behavior. Requested definition holes are
eligible jobs; newly invented definitions should be checked concrete declarations.
Do not silently turn missing definition values into theorem assumptions.

## Verification must survive adversarial candidates

Lean's documentation distinguishes successful elaboration from a complete proof:
`#print axioms` exposes axioms used transitively, and `sorryAx` means that a proof or
dependency is incomplete. The usual mathematical allowlist is `propext`,
`Classical.choice`, and `Quot.sound`. Additional trust from native computation is
version-dependent and requires an explicit policy rather than silently broadening
that list. [Lean: validating proofs](https://lean-lang.org/doc/reference/latest/ValidatingProofs/),
[Lean: axioms](https://lean-lang.org/doc/reference/latest/Axioms/).

For LeanFlow, retain LeanProbe as the fast inner loop, then independently validate
accepted artifacts against the frozen project environment. The validator, not the
agent's success message, grants status. Require the original elaborated target type,
allowed axioms, expected declaration identity, and accepted source changes.

For top-down checking, prefer a conditional theorem with the permitted prerequisites
as explicit hypotheses. Otherwise implement a proof-expression dependency audit
that identifies the exact unresolved declarations used. Blanket acceptance of
`sorryAx` is unsafe: it cannot distinguish a permitted prerequisite from the
original target imported with `sorry`. In particular, `exact target` against that
import must never count as a new proof. Reject undeclared dependencies, self-use,
and cycles through the transitive proof dependency graph.

Keep final verification separate: assemble accepted proofs in the current revision,
build all relevant modules, and audit every requested declaration's transitive
axioms. A root is complete only when its dependency closure is complete. A Python
computation is evidence for a plan; it is not a Lean proof or certified disproof.

## Source protection and incremental revisions

Freeze the supplied source before agents run. Record exact editable hole spans
with lexical/syntax awareness so comments and strings containing `sorry` are not
treated as holes. Preserve everything outside those spans. Allow helper files and
new code through designated insertion boundaries; validate that additions cannot
change the target's elaborated meaning through notation, instances, namespaces,
or imports. The pristine snapshot and acceptance checks must be outside candidate
write access. Recheck against current content before applying a patch so concurrent
user edits are not overwritten.

Give each node a stable ID, fully qualified declaration name, statement fingerprint,
dependency IDs, informal rationale, intended module/file, source range, revision,
and verification record. Line numbers are navigation metadata, not identity.
Record toolchain and dependency-lock fingerprints with proof artifacts.

Only the coordinator commits `PLAN.md` and the DAG. A result arrives with its job
ID, node ID, dispatched revision, dependency fingerprints, purpose, and artifact
paths. Accept unrelated branch results during replanning. Reject stale results
whose own statement or dependency environment changed. Invalidate the affected
reverse dependency closure, retain old proof text, and try rechecking it before
requesting another expensive proof attempt. A disproved invented helper can change;
a disproved user target must be reported without weakening its statement.

Additional libraries should enter through additive Lake requirements with explicit
public HTTPS repositories and immutable commit pins. Use a named dependency update
with `--keep-toolchain`: a bare update may upgrade unrelated dependencies, and Lake
otherwise supports changing the toolchain. Preserve existing locked package
identities and independently recheck the environment after any change. Dependency
configuration can execute code, so use the same OS write boundary as verification,
granting only dependency/configuration outputs. Restore exact configuration and
manifest bytes on failure; package caches may still require resynchronization.
[Lake: dependencies and updating](https://lean-lang.org/doc/reference/latest/Build-Tools-and-Distribution/Lake/).

## Budgets, context, and stopping unproductive loops

Keep the requested default of 300 model calls for one prover pass, shared by its
manager, concrete decomposition work, and any nested jobs. Count calls before
dispatch, including failed provider attempts and compression calls; keep usage in
persistent runtime state. A compression or local subgoal transition cannot reset
it. Up to three explicit restarts may each grant another pass, but cumulative
workflow spending still rises.

Expose 16 direction-changing `PLAN.md` refinements as requested, and separately
count local DAG revisions. Also enforce finite workflow call/time limits and
bounded node/decomposition growth: otherwise arbitrarily many “only splitting”
revisions evade the main limit. Administrative edits and completed proofs do not
consume a direction-change refinement. Log the reason for every counted revision.

The coordinator's fresh context should be constructed by the runtime from the
current plan, validated DAG snapshot, remaining budgets, and one completed job
envelope. The prover receives the target and relevant dependency slice, its
`PLAN_<job-id>.md`, and exact current diagnostics. Preserve these anchors outside
lossy transcript summarization. Do not repeatedly summarize summaries or reload
all proof bodies.

Remove standing advisors. If decomposition is requested, allow one bounded,
concrete proposal per node revision: write the helpers and parent sketch, check
them, then perform one relevance/simplification review. Reject exact duplicate
statements and ancestor-equivalent restatements where detectable. Further retries
need new evidence or a revised approach. When there is no such progress, report
the attempted strategies, remaining Lean goals, useful artifacts, and a typed
failure for coordinator handling.

## Reusable tool patterns

The following repository revisions were inspected, rather than assuming current
behavior from older versions:

- **Codex**, `6af345407d9c2a568da9d01b6c4b81a9e61495c0`:
  its [patch handler](https://github.com/openai/codex/blob/6af345407d9c2a568da9d01b6c4b81a9e61495c0/codex-rs/core/src/tools/handlers/apply_patch.rs)
  parses and validates patches against the selected environment and emits change
  events. Its [agent wait handler](https://github.com/openai/codex/blob/6af345407d9c2a568da9d01b6c4b81a9e61495c0/codex-rs/core/src/tools/handlers/multi_agents_v2/wait.rs)
  subscribes to queue activity and has a deadline. Adopt structured artifact/change
  events and completion-driven waiting; keep Lean acceptance as a stronger layer
  above generic patch validation.
- **KiloCode**, `1e469355802ccce9b82d29ac2e5c1cf6e0b95da9`:
  its [grep tool](https://github.com/Kilo-Org/kilocode/blob/1e469355802ccce9b82d29ac2e5c1cf6e0b95da9/packages/core/src/tool/grep.ts)
  exposes scoped paths, match limits, and explicit partial/truncated results.
  Its [compactor](https://github.com/Kilo-Org/kilocode/blob/1e469355802ccce9b82d29ac2e5c1cf6e0b95da9/packages/core/src/session/compaction.ts)
  keeps structured work-state summaries and recent context with configurable
  thresholds. Its [diff viewer](https://github.com/Kilo-Org/kilocode/blob/1e469355802ccce9b82d29ac2e5c1cf6e0b95da9/packages/kilo-vscode/src/DiffVirtualProvider.ts)
  accepts file patches and addition/deletion counts independently of Git. Adopt
  bounded search results, anchored recovery summaries, and per-job clickable diffs.

## Firecrawl assessment

Firecrawl currently documents keyless Search, Scrape, and Parse in hosted MCP;
official CLI/SDK/REST clients also support keyless Interact. Usage is capped per IP
by daily request and credit limits, returning `429` when exhausted. Keyless access
does not cover crawl, map, extract, or batch scrape. The documentation does not
publish numeric daily caps here. [Firecrawl rate limits](https://docs.firecrawl.dev/rate-limits).

The proposed `init --all --browser` command installs agent skills for all detected
agents and opens browser authentication. It is setup, not a required keyless search
command. No installation or authentication changes were made during this review.
[Firecrawl CLI](https://docs.firecrawl.dev/sdks/cli).

Recommendation: make Firecrawl an optional research resource provider behind the
existing web tool interface. Bound queries, returned results, downloaded bytes,
timeouts, and retries; cache source URL, retrieval time, content hash, and local
artifact path. On rate limiting, return an actionable tool result rather than
repeating calls indefinitely. Local Lean search remains the prover's primary
retrieval route. The quoted accuracy and latency claims were not independently
verified and should not become product guarantees.
