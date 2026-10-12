# Running benchmark campaigns on Linux

These helpers run an existing frozen campaign and manage its rebuildable cell
artifacts. They use explicit paths and never select a campaign automatically.

## Container setup

Build the image from the LeanFlow checkout:

```bash
docker build -t leanflow-leapbench:search testdata/workflow_projects/LeanIMOBench/linux
```

The image supplies Python, ripgrep, and setuid bubblewrap for hosts where
unprivileged user namespaces are unavailable. The wrapper enables the capabilities
and container security options needed by this setup. Review these options before
running on a shared host. `apparmor-bwrap.profile` is an alternative host
configuration for administrators who enable bubblewrap directly.

Run a command with an explicit campaign root:

```bash
testdata/workflow_projects/LeanIMOBench/linux/run-campaign-docker \
  --campaign-dir /absolute/path/to/campaign \
  --repo-dir /absolute/path/to/LeanFlow \
  -- sh -c 'command -v rg'
```

`--repo-dir` defaults to this checkout. `--home-dir` defaults to your home and must
contain `.elan`, `.leanflow`, and `.codex`. The campaign, checkout, and these home
subdirectories are mounted at identical absolute paths inside the container so
recorded campaign paths continue to resolve. The checkout is mounted read-only;
the campaign and home state are writable. Docker access is required.

The corresponding environment variables are `LEANFLOW_CAMPAIGN_DIR`,
`LEANFLOW_CAMPAIGN_REPO`, `LEANFLOW_CAMPAIGN_HOME`, and `LEANFLOW_CAMPAIGN_IMAGE`.
Use `--image` to override the image. `LEANFLOW_CAMPAIGN_LANES` controls concurrency
(default: 2) and is forwarded into the container.

## Reclaiming finished cells

```bash
python3 testdata/workflow_projects/LeanIMOBench/linux/reclaim-finished-cells \
  --campaign-dir /absolute/path/to/campaign
```

This is a dry run. Add `--apply` to delete eligible `.lake` directories, or
`--apply --loop 300` to repeat every five minutes. Only settled `completed` cells
with verification evidence and `budget_exhausted` cells are eligible. Cells with
resume state, other statuses, recent completion, live processes, or unavailable
process inspection are protected. Proof files, logs, and campaign metrics remain.
Escaping cell identifiers and symlink paths outside the campaign are rejected.

## Resetting infrastructure failures

```bash
python3 testdata/workflow_projects/LeanIMOBench/linux/reset-blocked-cells \
  --campaign-dir /absolute/path/to/campaign
```

After fixing the infrastructure failure and stopping the campaign runner, review
the dry run and add `--apply`. Cells with `environment_error`, `error`, or
`infrastructure_error` can be reset only when their recorded API-call count is
zero. Their previous attempt is archived under `executions`, partial cell/config
artifacts are removed, and status returns to `pending`. Cells with provider
spending are preserved for inspection.

Both cleanup helpers accept `LEANFLOW_CAMPAIGN_DIR` instead of `--campaign-dir`.
Use Python 3.11+ and run reclamation on Linux where `/proc` is available.
