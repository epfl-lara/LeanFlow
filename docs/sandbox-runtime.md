# LeanFlow Sandbox Runtime

The sandbox runtime runs normal LeanFlow commands inside a local Docker or
Podman container while keeping the host project out of the container mount set.
It is intended for high-trust model/tool work where the agent may create,
delete, or rewrite files freely, but those edits should remain reviewable before
they touch the user's checkout.

## Design Choice

The practical default is a container image plus a per-run copied worktree:

- Docker/Podman provide the cross-platform runtime boundary and installable
  Lean/Python/MCP tool environment.
- The selected LeanFlow project is copied into
  `~/.leanflow/sandbox/runs/<run-id>/worktree`.
- The original project path is never mounted into the container by default.
- The copied worktree is committed as a local baseline before the run starts.
- After the command exits, LeanFlow writes a binary-capable Git patch to
  `~/.leanflow/sandbox/runs/<run-id>/changes.patch`.

This is deliberately different from a normal development bind mount. A direct
read/write bind mount would make model edits immediately affect the host
checkout. The copied-worktree path gives the model full freedom inside the
container and keeps the host review/apply step explicit.

Dev containers are useful for editor-integrated development environments, and
the sandbox image is intentionally compatible with that style of setup, but a
devcontainer alone does not provide LeanFlow's per-run baseline, patch export,
status file, or workflow-focused cache layout. Linux namespaces and macOS
sandboxing remain useful implementation details, but they are not portable
enough to be the only LeanFlow runtime.

## Install And Update

Install the normal CLI plus the sandbox image and wrapper:

```bash
./scripts/install-sandbox.sh
```

To bake the local LeanExplore embedding stack into the image as well:

```bash
./scripts/install-sandbox.sh --with-local-lean-explore
```

The installer writes:

- `leanflow`: normal local CLI wrapper
- `leanflow-sandbox`: convenience wrapper for `leanflow sandbox run --`
- `leanflow/sandbox:local`: local container image

The default base image installs `leanflow[mcp]`, not the local LeanExplore extra,
to keep the image portable and quick to build. Passing `--with-local-lean-explore`
builds the image with `leanflow[mcp,lean-explore]`, which includes
`lean-explore[local]` and its embedding dependencies. Managed MCP bootstrap still
installs the configured Lean backends in the sandbox home on first run in both
modes. The image caches third-party Python dependencies before copying LeanFlow
source, so ordinary source changes do not redownload the full dependency set on
the next build.

The sandbox image includes the same baseline external workflow tools as the host
installer: `ripgrep` for local search and Poppler utilities for PDF inspection.

Upgrade from an existing checkout:

```bash
./scripts/update-sandbox.sh
```

The updater runs `git pull --ff-only`, reinstalls LeanFlow, and rebuilds the
sandbox image. If you manage the checkout yourself, use:

```bash
git pull --ff-only
./scripts/install-sandbox.sh
```

## Commands

Build or rebuild the image:

```bash
leanflow sandbox build
leanflow sandbox build --pull
leanflow sandbox build --with-local-lean-explore
```

Check readiness and recent runs:

```bash
leanflow status
leanflow sandbox status
leanflow sandbox doctor
leanflow sandbox status --json
```

Run a workflow from inside an initialized LeanFlow project:

```bash
cd /path/to/lean-project
leanflow-sandbox workflow prove Main.lean
leanflow-sandbox workflow autoformalize docs/paper-directory
```

The wrapper also accepts forgiving workflow forms:

```bash
leanflow-sandbox prove Main.lean
leanflow-sandbox /autoformalize docs/paper-directory
```

## Secrets And Network

By default the sandbox passes `~/.leanflow/.env` with Docker/Podman's
`--env-file` support. The file is not copied into the sandbox worktree. Override
it per run with:

```bash
leanflow sandbox run --env-file /path/to/env -- workflow prove Main.lean
```

Network access is enabled by default because provider APIs, Lake dependency
fetches, and Lean toolchain downloads usually need it. Disable it for local-only
commands with:

```bash
leanflow sandbox run --no-network -- workflow status
```

If a provider endpoint runs on the host at `127.0.0.1`, configure it for the
container-visible host address used by your engine, such as
`host.docker.internal` on Docker Desktop.

For an explicitly Codex-backed workflow, the sandbox mounts the minimal Codex
OAuth files read-only. Because the image intentionally does not install the
desktop `codex` executable, Codex reasoning and decomposition advisors use the
same Responses API adapter there; regular local mode retains command-backed
Codex expert behavior.

## Filesystem Boundary

The container receives these writable mounts:

- `/workspace`: copied LeanFlow project worktree for this run
- `/sandbox-run`: run metadata, status JSON, exported patch
- `/leanflow-home`: sandbox-specific LeanFlow home and managed MCP backends
- `/leanflow-cache`: Lean, Lake, pip, XDG, and large installer-temporary data

When the source project already has `.lake/packages`, that third-party dependency
directory is mounted read-only at `/workspace/.lake/packages`. Project
`.lake/build` output is never mounted, so compiled target declarations cannot
cross into the run. Runtime packages that must create native executables
(notably `repl`) are overlaid with source-only, writable sandbox copies keyed by
the project, toolchain, and dependency manifest. This prevents macOS/host
executables from being reused inside Linux while retaining warm container
builds across runs; ordinary dependency `.olean` data stays shared read-only.

The container root filesystem is read-only by default, with tmpfs mounts for
`/tmp` and `/run`. LeanInteract also receives a bounded 256 MiB, mode-1777 tmpfs
at its package-local `cache/` directory because the upstream package creates that
path at import time; the unprivileged workflow user can write there while
installed package code remains read-only. Dependency installers
use `/leanflow-cache/tmp` instead of the bounded `/tmp` tmpfs so large local
semantic-search wheels cannot exhaust the temporary filesystem during first-run
MCP bootstrap. Docker runs as the host UID/GID when available. Rootless Podman
uses `--userns=keep-id`.

The project copy excludes common host-only state:

- `.git`
- `.env`
- `.lake`
- local Python virtualenvs
- `.leanflow/workflow-state`
- `.leanflow/runtime`
- `.leanflow/cache`
- `.leanflow/workspace` (including prior repository-research clones)
- Python/tool caches

The project manifest and project-local skills under `.leanflow/` are preserved
so workflow resolution inside the sandbox sees the same LeanFlow project shape.

Repository, paper, code, and article research are enabled by default. For a
clean-room benchmark that excludes prior solutions, combine:

```bash
export LEANFLOW_DISABLE_REPOSITORY_RESEARCH=1
export LEANFLOW_DISABLE_SOLUTION_RESEARCH=1
export LEANFLOW_CLEAN_ROOM_TASK_LABELS="Benchmark Problem 6|BP6"
```

The repository flag denies Git, repository clones, repository-host fetches,
Sourcegraph, and repository-network terminal commands. The solution flag and
labels deny task-identifying queries, results, URLs, and network commands.
Unrelated mathematical, paper, and Mathlib research remains available.
`leanflow sandbox run` propagates all three values into the container.

## Outputs

Every run creates:

```text
~/.leanflow/sandbox/runs/<run-id>/
  worktree/
  changes.patch
  git-status.txt
  status.json
```

`status.json` records the original project root, container engine, image,
normalized LeanFlow command, exit code, patch path, and timestamps. `leanflow
sandbox status --json` includes recent run summaries for automation.

Apply a patch manually after review:

```bash
cd /path/to/original/project
git apply ~/.leanflow/sandbox/runs/<run-id>/changes.patch
```

## Configuration

The default configuration keys are:

```yaml
leanflow:
  sandbox:
    engine: auto
    image: leanflow/sandbox:local
    env_file: ""
    cache_dir: ""
    runs_dir: ""
    network: true
    read_only_root: true
    bootstrap_mcp: true
```

Empty paths use `~/.leanflow/sandbox/...`. `engine: auto` prefers a usable
rootless Podman on Linux, falls back to usable Docker, then Podman on other
platforms. `leanflow status` reports permission or daemon failures explicitly.
