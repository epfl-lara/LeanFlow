# Context compaction and response recovery

Investigated September 16, 2026 after PB-026 `prover_00029`. Implementation and
validation used the local checkout. The active campaign's frozen runtime and
processes were not modified, stopped, or restarted during the investigation.

## Observed failure

At call 89, estimated history fell from 243,552 to 186,877 tokens, leaving only
3,587 below the 190,464 input ceiling. The old compressor discarded the oldest
exchanges only until history fit that same trigger. With 256,000 context and
65,536 reserved output, the configured 75% threshold was capped at the hard
input ceiling. There was no lower post-compression target.

The separate response loop was more costly: observed calls 84 through 90 each
used 65,536 output tokens, ended with `finish_reason=length`, and returned no
answer or tools. Reasoning text in calls 88 through 90 had identical fingerprints.
The prover loop appended another generic continuation instead of bounding that
failure. Stronger compression alone cannot resolve this output failure.

## Primary-source comparison

| Implementation | Verified mechanism | Application to LeanFlow |
| --- | --- | --- |
| [Codex local compaction](https://github.com/openai/codex/blob/53ff712a48379ce8df605e292afd6046ca88ae9b/codex-rs/core/src/compact.rs) and [handoff prompt](https://github.com/openai/codex/blob/53ff712a48379ce8df605e292afd6046ca88ae9b/codex-rs/prompts/templates/compact/prompt.md) | Replaces history with a summary and bounded retained messages, restores canonical context, and recomputes usage. | Keep exact Lean declarations authoritative; preserve findings and references in a bounded handoff rather than retaining nearly all verbose history. |
| [Codex remote compaction](https://github.com/openai/codex/blob/53ff712a48379ce8df605e292afd6046ca88ae9b/codex-rs/core/src/compact_remote_v2.rs) and [OpenAI compaction API](https://developers.openai.com/api/docs/guides/compaction) | Capability-specific compaction and provider continuation items. | An OpenAI-compatible Chat Completions endpoint does not imply support for OpenAI's compaction endpoint or opaque compaction items. Do not send these to RCP without confirmed support. |
| [Kilo compaction](https://github.com/Kilo-Org/kilocode/blob/main/packages/opencode/src/session/compaction.ts) and [official explanation](https://kilo.ai/docs/customize/context/context-condensing) | Rolling summary with a bounded recent tail, preserving recent context separately from older history. | Preserve proof notes, exact assignment and complete recent tool exchanges; keep older evidence recoverable. |
| [Kilo chunk fallback](https://github.com/Kilo-Org/kilocode/blob/main/packages/opencode/src/kilocode/session/compaction-chunks.ts) | Bounded summarization chunks, output allowance and recursion depth. | Any future model summarizer needs its own strict request/output allowance, failure handling and accounting. Kilo's 60% constant is a chunk budget, not a universal post-compression target. |
| [Kilo response classification](https://github.com/Kilo-Org/kilocode/blob/main/packages/opencode/src/kilocode/session/processor.ts) | Separates bounded incomplete-response retries from reasoning-only output-limit responses. | Do not interpret a completed but unusable 65K reasoning response as ordinary proof persistence. |

The Codex snapshot is pinned; Kilo links reference the inspected `main` source
and may change. Neither source establishes that a 50% target is universally
optimal. The target below is a LeanFlow design choice, not a claimed upstream
default. A duplicate-reasoning detector was not established in the sampled Codex
paths; it is justified by LeanFlow's observed failure.

## Local implementation

- Keep total-window threshold semantics, but cap the trigger at 90% of the
  input allowance **after** reserving output. This cap is our explicit safety
  margin, not a copied assumption about an RCP model's advertised context size.
- Add independent role/model `compression_target` settings, defaulting to half
  the effective trigger. With the existing 256K/65,536 configuration: hard input
  190,464; trigger 171,417; target 85,708 estimated tokens.
- Preserve the exact current contract and newest tool exchange, allowing them
  to exceed the soft target. Report `target_met=false` instead of pretending the
  target was reached. An impossible hard limit stops before provider dispatch.
- Archive replaced history, retain bounded proof notes and recent exchanges,
  and include a reference to the archive in the active handoff. This is
  deterministic retention, **not model-generated semantic summarization**.
- Give an empty/truncated output one purposeful recovery within the existing
  budget. A repeated unusable response ends the attempt as `response_stalled`.
  Repeated identical non-actionable prose also enters bounded recovery.
- Persist in-session recovery admission across compaction and reopening. Preserve
  candidate verification, then hand the stopped attempt to the research
  orchestrator for a budgeted retry, instructed continuation, decomposition,
  or refutation. Invalid decisions return to the orchestrator for correction
  within existing campaign limits. The original implementation incorrectly blocked
  the node without this decision; that policy has been removed. Saved proofs and
  other runnable obligations remain available.
- Identify compression token counts as estimates. Provider-reported input/output
  accounting remains separate. No reasoning level or output cap is changed.

## Further work requiring separate evaluation

An LLM-generated proof handoff could compress more intelligently than saved notes
alone. It must preserve exact declarations outside the summary, use ordinary
provider-supported requests, count every summarization attempt against the same
job/campaign allowance, reject empty or expanding summaries, and atomically retain
the prior context on failure. Reusing the general agent's summarizer without this
accounting would introduce an untracked spending path.

Calibrating the context estimate against the final serialized provider request
would also improve precision, especially for replayed reasoning and opaque
provider metadata. It should not weaken the conservative pre-send hard limit.

Validation uses fake-provider session loops and controller tests: assert what the
next model request receives, exact helper declarations, recent paired feedback,
recoverable archives, one in-session recovery only, bounded orchestrator decisions,
instructions reaching the next actual request, no duplicate recovery on resume,
and independent healthy-job progress. No paid model experiments are needed for
these invariants.

Initial compaction validation: 7,890 tests passed and 111 skipped. Black, Ruff,
mypy and `git diff --check` passed. Installation is checked separately by verifying the
deployed source and exercising the installed runtime without model requests.

Orchestrator recovery correction: 7,917 tests passed and 111 skipped, including
the actual next-provider request, interrupted recovery replay, legacy blocked-run
migration, explicit stop, all recovery actions, and global budget exhaustion.
Black, Ruff, mypy and `git diff --check` also passed.
