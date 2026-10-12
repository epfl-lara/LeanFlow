# Evaluation harness

`corpus_manifest.json` pins task inventories and upstream source revisions.
`harness.py` validates the manifest and scores saved artifacts from workflows
that use `blueprint.json`, `summary.json`, and `journal.jsonl`. It does not launch
model calls or verify Lean proofs itself.

The dedicated prover uses a separate DAG/state format. Evaluate its terminal
verification results through the [Lean-IMO campaign harness](../docs/lean-imo-campaign.md)
and retain its native artifacts; do not pass those runs to the legacy
blueprint scorer.

## Inventories

- **T1:** the ProveDemo and DocFormalizationDemo regression projects.
- **T2:** 40 declarations from pinned miniF2F and PutnamBench sources.
- **T3:** ten research tasks, including the isolated IMOMath3 target and
  declarations from pinned Formal Conjectures.
- **T4:** 60 [Lean-IMO-Bench statements](../testdata/workflow_projects/LeanIMOBench/README.md).
- **Adversarial fixtures:** false claims, false decompositions, vacuous targets,
  and nonstandard axioms.

The manifest is an inventory, not a record that every suite has been executed.
The local regression and adversarial fixtures are shipped; other tasks identify
pinned external sources.

## Saved-artifact scoring

`score_terminal_artifacts(state_root)` checks terminal reports, graph counters,
decision packets, and the journal for the supported blueprint format.
`score_campaign_metrics(state_root)` reads that format's persisted campaign
accounting, and `aggregate_campaign_metrics(reports)` combines the reports.
`append_result` writes evaluation records to ignored `results.jsonl`.

From the repository root:

```bash
source .venv/bin/activate
python -m pytest tests/leanflow/test_eval_harness.py -q -n 0
```

For capability comparisons, freeze the same corpus, toolchain, budgets, runtime,
and solution-access settings for every condition. Keep reference proofs outside
scored projects. Report verified completion separately from elaboration,
interrupted attempts, partial proofs, and missing accounting. Validate final
Lean artifacts independently before claiming a solve rate.
