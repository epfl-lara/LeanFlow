# Prover readiness and proposed hard target — 2026-09-06

This is a preparation and review record. The user increased the requested limits
and asked for one checkout. No new proof campaign was launched.

## Checkout and installation

The redesign is now checked out on `milikic/prover-redesign` directly in
`~/Desktop/LeanFlow`. The extra redesign worktree was removed after consolidation.
The `main` branch remains available in Git. The original `Refactor.md` is untouched;
the differing worktree copy is preserved under
`.leanflow/archives/redesign-worktree-20260906/Refactor-redesign.md`.

The installed `~/.local/bin/leanflow` now uses `~/Desktop/LeanFlow/.leanflow-venv`.
Its imports resolve to the consolidated checkout. The prepared input and visible
configuration are in `testdata/workflow_projects/BeckFialaResearch/`; experiment
records are preserved under `.leanflow/experiments/`.

After relocation, the installed CLI help and isolated LeanProbe preflight pass,
and the pinned input project builds with its one expected `sorry`. Black, Ruff,
mypy, all 7,070 Python tests, and all 159 extension tests pass again from this
checkout. The installed extension bundles match the rebuilt source hashes.

The redesigned VS Code extension was rebuilt and installed as **0.1.10**.
Type checking, all **159 extension tests**, and VSIX packaging passed. Installed
JavaScript/CSS hashes match the packaged source build. An actual VS Code window
loaded the previous completed research run and displayed its three verified DAG
nodes, plan, eight jobs, API/token totals, source links, and baseline diffs.
Opening a file diff worked; guidance was disabled for the completed run.

## Target recommendation

Codex and **Claude Fable 5.1 at max effort** agree on
`BeckFiala.beck_fiala_theorem` from the
[pinned Formal Conjectures source](https://github.com/google-deepmind/formal-conjectures/blob/8323e878b83fcd7f4a448256069352a265460d75/FormalConjectures/Wikipedia/BeckFialaConjecture.lean).

For a finite set family in which each element belongs to at most `t` sets, with
`t ≥ 1`, the theorem asks for a ±1 coloring whose sum on every set has absolute
value at most `2t − 1`. It is known mathematics with a missing Lean proof in that
repository revision. We do not claim that no formal proof exists anywhere.
The stronger Beck–Fiala conjecture in the same source file remains a separate,
mathematically open problem and is outside this proposal.

The target has one ordinary proof hole and no answer placeholder or unfinished
definition. The isolated input preserves the declaration verbatim and uses the
source revision's Lean **4.33.1** and Mathlib
`0df444a360eaa60ab8c11dca51a86af692955474`.

We also inspected public Prove2Me statements, including the
[integer Gram matrix inequality](https://prove2.me/theorems/a900d293-0b59-4345-952e-528f4a81b221)
and [polynomial target-face access](https://prove2.me/theorems/ed21033c-0ed4-4ae2-ba54-c89013a4c8fc).
Both were marked open when checked. The latter would imply polynomial Hirsch;
neither offers a better justified first hard trial. Public theorem pages expose
statements and dependency pins even though API listing requires authentication.

## Proposed launch settings

| Setting | Proposed value |
| --- | --- |
| Provider | `openai-codex` via the existing Codex login |
| Orchestrator, reviewer, researcher, prover | `gpt-6-astra`, `xhigh` |
| External design reviewer | Claude Code `claude-fable-5-1`, `max` |
| Mode and order | Research, bottom-up |
| Parallel provers | 4 |
| Calls per prover or negation pass | 200 |
| Calls per planning/review/research stage | 50 |
| Total campaign calls | 2,000 |
| Restarts | 2 additional passes per node |
| Direction refinements / decompositions / nodes | 4 / 8 / 24 |
| Campaign wall time | 8 hours |
| Provider and independent verification timeout | 1,200 seconds |
| Prover / orchestrator context | 64,000 / 96,000 tokens |
| Compression | Enabled for both roles |
| Local Loogle in research workers | Disabled; native source and remote search remain available |
| Allowed completion axioms | `propext`, `Classical.choice`, `Quot.sound` |
| Definition filling | Disabled |
| Proposed run ID | `beckfiala-research-01` |

Known informal proofs and literature research are allowed. Clean-room mode is
off, with an explicit instruction to avoid copying/importing an existing formal
solution. That instruction is a prompt and source-audit policy, not a complete
technical ban on fetching formal proofs. The exact installed CLI dry-run resolves
every proposed setting, including `xhigh`, without starting a model session.
The user's global default model is unchanged; this launch must select Codex
explicitly.

The reviewed settings are saved in the input project's `RUN_CONFIG.json` and as
the project-local `beck-fiala-research` flag profile. Loading that profile selects
the limits; it does not change global defaults or replace the explicit Codex
provider selection. The 8-hour campaign and 20-minute request/verification limits
are the chosen interpretation of the user's request to increase time as well.
See [budget scope and stop conditions](prover-budget-contract.md).

## Preparation findings and correction

There is no REPL `v4.33.1` tag. Automatic project initialization initially failed
to fetch it. The isolated project instead pins REPL source
`bbeedf38e0898869fc3b7c009e1ea877b46204e4` (`v4.33.0`) and successfully builds it
with Lean 4.33.1. The installed prover's OS-isolated LeanProbe preflight passes.
This is a tested project-level pin; automatic patch-version selection remains a
product limitation.

The real input then exposed a top-down skeleton-gate bug: LeanProbe correctly returned
`success=true, ok=false, has_errors=false, has_sorry=true`, but the skeleton gate
required `ok=true`. A regression test reproduced the rejection before the fix.
The gate now recognizes sorry-only elaboration, then still requires independent
compilation, a matching kernel type, and only the permitted axioms plus the
temporary skeleton `sorryAx`. Strict proof acceptance still rejects `sorryAx`.
The live unchanged target now passes skeleton acceptance and fails strict proof
acceptance as expected. Its complete project builds with one expected sorry
warning, and its input hash is unchanged.

Fable's focused max-effort review found no soundness regression in the correction.
It confirmed that the corrected branch is used for conditional candidates with
unproved dependencies. The proposed bottom-up campaign instead uses strict
acceptance. Its original-signature capture and strict/final rejection of the
unfinished input pass. Eight real verifier regressions also pass on the pinned
toolchain, covering valid proofs, changed statements through imports, custom
axioms, and changed dependency proofs. Their fresh-project fixture now initializes
Lake's configuration cache before entering the read-only sandbox.

After the correction, Black, Ruff, mypy, and the full Python suite pass:
**7,070 passed, 109 skipped, 14 warnings**. The new regression covers elaboration
failure, missing evidence, timeout, diagnostics, independent compilation failure,
changed kernel types, and disallowed axioms.

## Remaining product limits

- The Anthropic path has no configured credentials here; Claude Code login does
  not authenticate LeanFlow's direct Anthropic client. Fable 5.1 effort handling
  in that client is not ready or live-validated. Fable is the external reviewer.
- Reasoning effort is shared across roles, and role models share one provider.
- Prover scratch checks retain their separate 60-second timeout, including cold
  import time; the proposed 1,200 seconds does not override that limit.
- Negation attempts consume the full prover-sized allowance. The 2,000-call total
  is the effective safeguard against repeated expensive failures.
- A transient provider failure ends the campaign as resumable; resumption is
  manual, using its saved identity and progress.
- Local Loogle's managed client still sends an obsolete explicit index-write
  option. A generic `Nat.add_comm` query succeeded through interactive startup
  on a project-specific Loogle build, but cold startup took 107 seconds. Research
  mode already disables local Loogle by default; the proposal pins that policy
  explicitly. Updating this shared client's index flags remains separate work.
- The machine has 24 GiB RAM; observed free memory was 37% during preparation.
  The requested four workers may contend for memory, so local Loogle remains off.

This evidence supports the specific Codex configuration above. It does not
establish that every provider or every research path is production-ready.

## Evidence

Detailed local artifacts are in
`.leanflow/experiments/20260906-readiness-selection/` relative to this checkout:
`installed-readiness.json`, `fable-review.json`,
`fable-concurrence.json`, `fable-skeleton-review.json`, `proposed-config.json`,
`config-preview.json`, `target-manifest.json`, `target-verification.json`,
`target-build.log`, `doctor-target.json`, and the test/install logs.
The isolated input is
`testdata/workflow_projects/BeckFialaResearch/BeckFiala/Problem.lean`.
Historical logs retain their original absolute paths as provenance;
`consolidation.json` records the move without rewriting those logs.
