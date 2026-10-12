# Lean-IMO-Bench campaigns

`scripts/lean_imo_campaign/` runs frozen comparisons through LeanFlow's native
prover. Each condition/problem pair receives a private project and recorded
configuration. This is evaluation tooling, separate from ordinary workflows.
See the [fixture guide](../testdata/workflow_projects/LeanIMOBench/README.md) for
statement provenance and benchmark preparation.

## Prepare and run

Configure the required native backends first. Only `codex`, `claude`, and `lemex`
are supported; their own configuration supplies authentication, endpoints,
sampling, and compression. Keep native CLI versions and settings stable during
a comparison.

```bash
source .venv/bin/activate
python -m scripts.lean_imo_campaign.runner prepare /absolute/path/to/new-campaign
/absolute/path/to/new-campaign/run-campaign
```

Use `prepare --help` to inspect condition sets and limits. `--conditions` chooses
a predefined comparison; `--problems` selects comma-separated problem IDs from
the fixture's 18 LEAP-unsolved statements. Record each condition separately;
best-of-several is a separate aggregate.

```bash
python -m scripts.lean_imo_campaign.runner prepare /absolute/path/to/bounded-campaign \
  --agent-calls 100 --tokens 2000000 --wall-time-s 28800
```

Global limits are unset by default. Calls count native invocations across roles
and resumes. Tokens are reported input/output usage and gate future calls;
an active call can overshoot. Time is cumulative active cell time. Historical
API-request budgets do not have the same meaning.

Preparation records runtime, skill, dependency, and statement identities. Every
cell receives a fresh project without reference solutions, sibling problem
sources, or earlier results. External research and dependency downloads are
disabled while model access remains available. Existing campaign directories are
not overwritten. Native homes are separately managed and are not frozen by the
Python snapshot.

## Observe and recover

`campaign.json` and `metrics.csv` record cell outcomes, calls, reported usage,
elapsed time, and evidence paths. Cell configurations and process logs stay outside
model-readable projects. Missing token or price coverage remains incomplete or
unavailable. Only terminal completion with independent final Lean acceptance
counts as verified.

The VS Code benchmark view opens `campaign.json` and links to each cell's prover
workspace. Pause writes `PAUSE_AFTER_ACTIVE`, allowing active cells to finish.
Recognized transient native failures can reconnect with backoff while limits
permit. Permanent or ambiguous infrastructure/authentication failures pause
new dispatch for inspection; earlier results and cumulative usage are retained.

After resolving a pause, remove its marker and restart the frozen runner.
Reconcile active admissions before replacing a crashed dispatcher to avoid
duplicate workers. `run DIRECTORY --adopt-active` uses recorded process identity
and the dispatcher lock for an explicit handoff; a live process is not proof of
successful verification.

`LEANFLOW_CAMPAIGN_LANES` defaults to two independent problem lanes. To serialize
already assigned lanes, preserve their count and set
`LEANFLOW_CAMPAIGN_MAX_ACTIVE=1`. Narrowing below an existing claim is rejected.

New manifests use version 2 and reject legacy direct-provider campaigns. Preserve
historical records and prepare a new native comparison. A recorded runtime
upgrade can affect unstarted cells; started cells retain their runtime identity
and resume lineage. Keep retained inputs available or explicitly reconstruct them
before dispatch. See [storage and retention](storage.md).
