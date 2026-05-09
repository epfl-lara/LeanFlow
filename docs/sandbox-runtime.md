# EPFLemma Sandbox Runtime

The sandbox runtime runs normal EPFLemma commands inside a local Docker or
Podman container while keeping the host project out of the container mount set.
It is intended for high-trust model/tool work where the agent may create,
delete, or rewrite files freely, but those edits should remain reviewable before
they touch the user's checkout.

## Design Choice

The practical default is a container image plus a per-run copied worktree:

- Docker/Podman provide the cross-platform runtime boundary and installable
  Lean/Python/MCP tool environment.
- The selected EPFLemma project is copied into
  `~/.epflemma/sandbox/runs/<run-id>/worktree`.
- The original project path is never mounted into the container by default.
- The copied worktree is committed as a local baseline before the run starts.
- After the command exits, EPFLemma writes a binary-capable Git patch to
  `~/.epflemma/sandbox/runs/<run-id>/changes.patch`.

This is deliberately different from a normal development bind mount. A direct
read/write bind mount would make model edits immediately affect the host
checkout. The copied-worktree path gives the model full freedom inside the
container and keeps the host review/apply step explicit.

Dev containers are useful for editor-integrated development environments, and
the sandbox image is intentionally compatible with that style of setup, but a
devcontainer alone does not provide EPFLemma's per-run baseline, patch export,
status file, or workflow-focused cache layout. Linux namespaces and macOS
sandboxing remain useful implementation details, but they are not portable
enough to be the only EPFLemma runtime.

## Install And Update

Install the normal CLI plus the sandbox image and wrapper:

```bash
./scripts/install-sandbox.sh
```

The installer writes:

- `epflemma`: normal local CLI wrapper
- `epflemma-sandbox`: convenience wrapper for `epflemma sandbox run --`
- `epflemma/sandbox:local`: local container image

The base image intentionally installs `epflemma[mcp]`, not the local
LeanExplore extra, to avoid bundling heavyweight Torch/CUDA packages into every
sandbox. Managed MCP bootstrap can still install the configured Lean backends in
the sandbox home on first run.

Upgrade from an existing checkout:

```bash
./scripts/update-sandbox.sh
```

The updater runs `git pull --ff-only`, reinstalls EPFLemma, and rebuilds the
sandbox image. If you manage the checkout yourself, use:

```bash
git pull --ff-only
./scripts/install-sandbox.sh
```

## Commands

Build or rebuild the image:

```bash
epflemma sandbox build
epflemma sandbox build --pull
```

Check readiness and recent runs:

```bash
epflemma status
epflemma sandbox status
epflemma sandbox doctor
epflemma sandbox status --json
```

Run a workflow from inside an initialized EPFLemma project:

```bash
cd /path/to/lean-project
epflemma-sandbox workflow prove Main.lean
epflemma-sandbox workflow autoformalize docs/paper-directory
```

The wrapper also accepts forgiving workflow forms:

```bash
epflemma-sandbox prove Main.lean
epflemma-sandbox /autoformalize docs/paper-directory
```

## Secrets And Network

By default the sandbox passes `~/.epflemma/.env` with Docker/Podman's
`--env-file` support. The file is not copied into the sandbox worktree. Override
it per run with:

```bash
epflemma sandbox run --env-file /path/to/env -- workflow prove Main.lean
```

Network access is enabled by default because provider APIs, Lake dependency
fetches, and Lean toolchain downloads usually need it. Disable it for local-only
commands with:

```bash
epflemma sandbox run --no-network -- workflow status
```

If a provider endpoint runs on the host at `127.0.0.1`, configure it for the
container-visible host address used by your engine, such as
`host.docker.internal` on Docker Desktop.

## Filesystem Boundary

The container receives these writable mounts:

- `/workspace`: copied EPFLemma project worktree for this run
- `/sandbox-run`: run metadata, status JSON, exported patch
- `/epflemma-home`: sandbox-specific EPFLemma home and managed MCP backends
- `/epflemma-cache`: Lean, Lake, pip, and XDG cache data

The container root filesystem is read-only by default, with tmpfs mounts for
`/tmp` and `/run`. Docker runs as the host UID/GID when available. Rootless
Podman uses `--userns=keep-id`.

The project copy excludes common host-only state:

- `.git`
- `.env`
- `.lake`
- local Python virtualenvs
- `.epflemma/workflow-state`
- `.epflemma/runtime`
- `.epflemma/cache`
- Python/tool caches

The project manifest and project-local skills under `.epflemma/` are preserved
so workflow resolution inside the sandbox sees the same EPFLemma project shape.

## Outputs

Every run creates:

```text
~/.epflemma/sandbox/runs/<run-id>/
  worktree/
  changes.patch
  git-status.txt
  status.json
```

`status.json` records the original project root, container engine, image,
normalized EPFLemma command, exit code, patch path, and timestamps. `epflemma
sandbox status --json` includes recent run summaries for automation.

Apply a patch manually after review:

```bash
cd /path/to/original/project
git apply ~/.epflemma/sandbox/runs/<run-id>/changes.patch
```

## Configuration

The default configuration keys are:

```yaml
epflemma:
  sandbox:
    engine: auto
    image: epflemma/sandbox:local
    env_file: ""
    cache_dir: ""
    runs_dir: ""
    network: true
    read_only_root: true
    bootstrap_mcp: true
```

Empty paths use `~/.epflemma/sandbox/...`. `engine: auto` prefers a usable
rootless Podman on Linux, falls back to usable Docker, then Podman on other
platforms. `epflemma status` reports permission or daemon failures explicitly.
