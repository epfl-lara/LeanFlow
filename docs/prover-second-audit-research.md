# Prover redesign: second literature audit

Checked on 2026-09-06 against the three requested papers, three additional
closely related papers, and official Codex/Kilo sources. Implementation observations
below describe the starting audit revision `a0c46bb`; the accompanying code review
may address them. This is a bounded evidence review, not a reproduction of paper
benchmarks or a claim that every suggested mechanism should be implemented.

## Findings that should affect implementation

### 1. A useful decomposition needs more than independently typed helpers

**Evidence.** LEAP checks a parent proof sketch whose remaining holes correspond
only to its proposed sublemmas. It separately reviews whether those sublemmas
actually simplify the parent. Its reviewer ablation demonstrates a repeated
ancestor-equivalent goal consuming the search budget. The formal implication
witness and semantic progress review serve different purposes.
[LEAP, §§2.5 and 5.3](https://arxiv.org/html/2606.03303v1).

**Observed implementation difference.** `planning.py:apply_proposal` checks unique
names, valid dependencies, cycles, reachable helpers and individual declarations.
`planning_controller.py` checks helper skeletons and a separate semantic review.
There is no formal parent-sketch witness in the planning protocol, and at the
starting revision no deterministic check rejects a helper that merely renames an
ancestor's statement. Thus an accepted DAG is a mathematical proposal, not a
certificate that its edges discharge the targets.

**Engineering recommendation.** Reject exact ancestor restatements after removing
the declaration name and irrelevant formatting. Do not claim that a textual check
decides semantic equivalence. A future bounded decomposition tool should submit
the actual parent sketch and extracted obligations together. Its verifier must
reject a parent whose entire original obligation remains hidden behind an
unregistered hole. Keep this work inside the existing pass allocation and one
decomposition attempt per node revision.

**Regression cases.** Reject `theorem helper : P := by sorry` as a new prerequisite
of `theorem target : P`; accept a proper smaller prerequisite. A future witness
gate should reject irrelevant true helpers for a parent they do not imply.

### 2. Retain checked partial proof structure when recovery changes local goals

**Evidence.** Mechanic isolates an innermost failing proof block, recompiles after
each replacement, and extracts the resulting local goal with its context. This
avoids treating cascading compiler errors as independent failures. The extracted
lemma is checked back at the original location; its Appendix B.1 shows that an
extracted goal may itself be false. The limited ablation covers four Putnam
problems, so it does not establish a universal performance advantage.
[Mechanic, §§3.1, 3.2.3, 4.2 and Appendix B.1](https://arxiv.org/html/2603.24465v1).

**Engineering recommendation.** LeanFlow already preserves a job's scratch
replacements and notes across same-revision passes. Extend that foundation only
when traces show repeated loss of useful partial code: extract exact Lean local
goals, retain the checked surrounding proof, and recheck substitution into that
parent. Do not copy hypotheses from pretty-printed text without accounting for
local instances, dependent binders and metavariables. The paper's additional
informal-review loops are not required to adopt this local repair mechanism.

### 3. Cache proof reuse by formal environment, not just lemma name

**Evidence.** Goedel-Architect gives node provers declared dependency signatures
and reuses solved nodes while signatures and parents remain unchanged. Its
blueprint refinement budget includes decompositions. Nexus's global goal cache
uses the exact Lean target and formal context; its episode validation checks the
original specification and guards against environment exploits.
[Goedel-Architect, §§2.1–2.3 and Appendix A](https://arxiv.org/html/2606.06468v1),
[AlphaProof Nexus, Appendix A.1, “Global Goal Caching and Incorporation”](https://arxiv.org/html/2605.22763v1).

**Engineering recommendation.** Keep final independent checking even when a
saved candidate is reusable. A cross-run proof cache should include the elaborated
target, local context, dependency identities, toolchain and dependency lock, and
must recheck the retrieved proof. A node ID or source line is not such a key.
Preserving target text also needs verification against changes in the elaboration
environment from new imports, notation or instances. These are audit requirements;
this literature pass has not independently demonstrated an environment exploit.

Neither paper establishes a universally optimal top-down or bottom-up traversal.
The documented bottom-up default and untrusted experimental top-down candidates
remain reasonable engineering choices, pending comparisons on LeanFlow tasks.

## Keep the prover small; measure its progress before adding another controller

AxProverBase compares iterative compiler feedback, memory and retrieval. Its
ablation finds the largest gain from iterative repair; retaining short lessons
reduces repeated errors, while adding search produces a smaller further gain.
It compares self-managed notes with retaining five full previous attempts.
This supports preserving `PLAN_job.md`, the actual compiler diagnostics and the
current proof, rather than adding a standing advisor.
[A Minimal Agent for Automated Theorem Proving, §§3 and 4.1](https://arxiv.org/html/2602.24273v3).

An additional cost-routing paper trains a predictor from proof trajectories to
choose whether another attempt is worthwhile. It evaluates an 85-problem subset
and treats Lean compilation cost as negligible. Its quantitative savings therefore
do not justify installing an uncalibrated router in this runtime.
[Optimizing the Cost-Quality Tradeoff of Agentic Theorem Provers in Lean,
§§3.2 and 4.1–4.2](https://arxiv.org/html/2606.04883v1).

For an initial benchmark, record solved roots, all admitted calls, wall time,
compiler time, provider-reported tokens/cost, unique source edits, repeated
diagnostics and repeated searches. Compare standard and research modes at the
same total budget, including failed runs. This is an engineering evaluation
proposal, not a metric set validated by those papers. Calls and refinement counts
remain explicit ceilings; a changed note alone should not be presented as
mathematical progress.

## Concrete lessons from official tool implementations

- **Search completeness must be explicit.** Kilo's grep result separates bounded
  truncation from inaccessible paths. LeanFlow's starting `session_search.py`
  uses eight matches per file but reports truncation only at its global limits.
  A nine-match file can therefore be silently incomplete. Add a regression at
  that boundary and preserve truncation/partial status in the model-visible
  result. [Kilo `grep.ts`, `Result` and `toModelOutput`](https://github.com/Kilo-Org/kilocode/blob/main/packages/core/src/tool/grep.ts).
  **Resolved during this audit:** a ninth match now detects per-file omission
  without increasing the eight returned matches, and clipped line previews also
  set `truncated`. Real-search regressions first reproduced three failures; all
  five focused cases then passed, including exact-boundary and protected-file
  cases. Existing dependency-search/guidance tests also passed.
- **Reserve space for the whole request.** Kilo's documented context check counts
  tool definitions and leaves room for output. LeanFlow's starting
  `agent_session.py` counts history before appending its remaining-budget note,
  and excludes tool schemas. Include both in the admission estimate and test
  just-below/just-above limits without spending a provider request.
  **Resolved during this audit:** admission now includes schemas and the budget
  note before charging a request; both boundary regressions pass.
  [Kilo context condensing, “How Compaction Triggers”](https://github.com/Kilo-Org/kilocode/blob/main/packages/kilo-docs/pages/customize/context/context-condensing.md).
- **Keep waiting and truncation in runtime code.** Codex waits on mailbox activity
  with a deadline and records omitted output explicitly. LeanFlow should keep
  completion-driven queues and bounded, inspectable artifacts; waiting should
  not require repeated model decisions. These patterns corroborate the current
  direction, rather than justify another orchestration layer.
  [Codex `wait.rs`, `wait_for_activity`](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/multi_agents_v2/wait.rs),
  [Codex `context.rs`, `truncated_output_with_policy`](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/context.rs).

The official repository links above were reopened during this audit and track
`main`; they are observations at the stated date. The earlier research note retains
its pinned-revision references. No additional browser, search provider, package,
or agent skill was installed during this pass.
