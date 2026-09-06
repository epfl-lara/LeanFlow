# Lean-IMO-Bench comparison

`scripts/lean_imo_campaign/` is the repository harness for the requested
LEAP-unsolved comparison. It calls the dedicated prover entry point; it does
not introduce a general batch workflow or use the legacy agent queue.

The frozen manifest selects 18 problems. Each problem receives these four
independent research runs in order: Astra xhigh bottom-up, Astra xhigh top-down,
Terra xhigh bottom-up, Terra xhigh top-down. Two problem lanes advance independently;
a lane finishes all four conditions before claiming another problem. Every cell
gets 4 prover slots, 200 calls per prover/negation pass, 50 per planning/review/
research session, 2,000 total calls, and 8 active hours. The 20-minute request/check
ceiling remains inside the campaign wall-time limit. Context limits are 64,000
for provers and 96,000 for orchestrators, with deterministic compression enabled.
Other explicit limits: 3 restarts per node, 16 directional plan refinements,
32 decompositions, and 128 DAG nodes. These limits are identical in every condition.

`LEANFLOW_PROVER_ALLOW_INTERNET=0` removes web/download tool schemas, rejects
their invocation, routes Lean search through local source search, and blocks
planned dependency installations. Model API access remains enabled. Clean-room
and repository-research restrictions are also set. Computation retains its
filesystem/network restrictions; Lean checks retain OS isolation. No reference
solutions, sibling problem files, or earlier attempt results enter a cell.

The harness snapshots the latest working Python runtime and skills, clones
the installed Python environment and pinned built Lake dependencies with APFS
copy-on-write, and hashes the exact statement inputs. Each cell gets a fresh
private project. Existing cell directories are never overwritten. The home
configuration hash is checked before each launch; a change pauses dispatch
rather than silently changing the comparison. Credentials are resolved at
launch and never written to the launch record.

```bash
source .venv/bin/activate
python -m scripts.lean_imo_campaign.runner prepare /absolute/path/to/new-campaign
# The generated launcher pins imports independently of the current directory.
/absolute/path/to/new-campaign/run-campaign
```

`campaign.json` and `metrics.csv` update every five seconds. Each cell retains
the complete native prover state, DAG, plan, transcripts, checks, changes, and metrics.
Cell configurations and process logs live outside model-readable projects in
`cell-configs/` and `cell-logs/`; credential-free launch records are controller
artifacts under each project's `.leanflow/` directory.
Only terminal `completed` state with independent final verification accepted
counts as verified. Missing prices remain unavailable. Report each condition
over 18 problems; a best-of-four aggregate is a separate result. This fixture
uses Lean/Mathlib 4.33.1, while LEAP used 4.27.0.

In VS Code, **LeanFlow: Open Benchmark Campaign** opens `campaign.json` as a
live 72-cell overview. **Open prover dashboard** / **Inspect** selects the exact
cell project in the existing DAG/plan/jobs/changes dashboard. The queue's pause
button writes `PAUSE_AFTER_ACTIVE`; active work finishes normally. Transient provider
failures can reconnect three times through the native resume path, preserving
cumulative calls, time, proof progress and each failed execution's record.
Persistent provider failures and other infrastructure errors pause new dispatch.
To continue pending cells, inspect the cause,
remove that marker if present, and run the same frozen runner. Terminal cells
are preserved. If the dispatcher crashed with active admissions, it refuses
automatic replacement: reconcile the recorded PIDs and native state first so
that surviving jobs cannot be duplicated or their budgets reset.
