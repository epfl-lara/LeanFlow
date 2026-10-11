# Lean-IMO-Bench comparison

`scripts/lean_imo_campaign/` runs the repository's LEAP-unsolved comparison through
LeanFlow's native prover. The default matrix contains Astra and Terra, each with
bottom-up and top-down search, across 18 problems. Two lanes advance independently;
each finishes a problem's conditions before claiming another problem. Each cell has
four prover slots. Unspecified reasoning effort uses the native runtime default.

Campaigns select only `codex`, `claude`, or `lemex`. Native runtimes own login, keys,
endpoints, sampling, context management, and compression. The `kimi-glm-flash-top`
condition uses Lemex for Kimi planning/review/research and GLM proving/negation.
The `astra-low-glm-flash-top` and `astra-low-deepseek-flash-top` conditions use Codex
for Astra planning and Lemex for the external prover, with one prover slot per cell.
Configure RCP directly in Lemex before launching:

```bash
export LEMEX_BASE_URL="https://inference.rcp.epfl.ch/v1"
export LEMEX_API_KEY="your-key"
```

Native environment variables and homes pass through unchanged. The campaign does
not copy credentials, read native authentication files, or translate `RCP_API_KEY`.
Keep native CLI versions and configuration stable during a comparison; the Python
snapshot does not freeze those separately managed runtimes or their native homes.

All resource limits are unlimited unless explicitly supplied when preparing the
campaign. `--agent-calls` counts native CLI invocations across all roles and resumes.
`--tokens` is an approximate cap on reported input plus output tokens; it prevents
future calls once reached, and active calls can overshoot. `--wall-time-s` limits
cumulative active time for the whole cell. There are no per-response deadlines,
per-job call limits, compression overrides, or output-token caps.

```bash
source .venv/bin/activate
python -m scripts.lean_imo_campaign.runner prepare /absolute/path/to/new-campaign
# Optional limits, recorded identically for each cell:
python -m scripts.lean_imo_campaign.runner prepare /absolute/path/to/bounded-campaign \
  --agent-calls 100 --tokens 2000000 --wall-time-s 28800
/absolute/path/to/new-campaign/run-campaign
```

`--conditions` selects a named model/effort comparison; `--problems` selects problem
IDs from the 18 unsolved statements. `LEANFLOW_CAMPAIGN_LANES` sets concurrency
(default two). To serialize already assigned lanes, retain their lane count and
set `LEANFLOW_CAMPAIGN_MAX_ACTIVE=1`; lane assignments and cell configurations stay
fixed. The dispatcher refuses to narrow the lane count below an existing claim.

Preparation snapshots the working Python runtime and skills, copies the installed
Python environment and pinned Lake dependencies, and hashes the statement inputs.
Each cell gets a fresh private project; existing directories are never overwritten.
A changed LeanFlow home configuration hash prevents launch. Reference solutions,
sibling problem files, and earlier attempt results do not enter a fresh cell.
Internet research, downloads, and planned dependency installations are disabled;
native model access remains available. Lean checks retain OS isolation.

`campaign.json` and `metrics.csv` update every five seconds. State includes the DAG,
plan, transcripts, checks, changes, agent calls, reported usage, and elapsed time.
Cell configurations and process logs live in `cell-configs/` and `cell-logs/` outside
model-readable projects. Launch records omit credentials. Missing token or price
coverage remains marked incomplete or unavailable. Only terminal `completed` state
with accepted independent verification counts as verified. Report each condition
separately; best-of-four is a separate aggregate. The fixture uses Lean/Mathlib
4.33.1, while LEAP used 4.27.0.

The VS Code benchmark view opens `campaign.json` and links each cell to its prover
dashboard. Pausing writes `PAUSE_AFTER_ACTIVE`, allowing active work to finish.
Recognized transient connection, timeout, rate-limit, HTTP 408/429, and server
failures can reconnect with backoff while explicit global budgets permit. Recovery
preserves cumulative metrics and failed execution history; there is no fixed retry
count. Permanent authentication, prompt, quota, context, unknown/internal, and
infrastructure failures pause dispatch for inspection. The watchdog applies only
when the cell has an explicit global time limit.

After inspecting a paused campaign, remove its pause marker and restart the frozen
runner. Terminal results remain recorded. A dispatcher crash with active admissions
requires reconciliation before replacement to avoid duplicate workers. An explicit
handoff uses `run <directory> --adopt-active`, recorded process identities, and the
existing dispatcher lock. Adoption rejects reused PIDs and unfinished preparation;
only terminal native verification establishes success.

New campaigns use manifest version 2. Earlier direct-API campaigns keep their
historical records and frozen runtime. The new dispatcher refuses to resume them:
old API-request budgets do not have the same meaning as native agent invocations.
Prepare a new campaign for native-backend comparisons. Within version 2, an explicit
runtime upgrade can select a new immutable `default_runtime_directory` for
unstarted cells; started cells retain their runtime hash and resume lineage.
Never overwrite an existing snapshot.
