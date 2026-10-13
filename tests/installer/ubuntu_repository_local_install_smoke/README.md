# Ubuntu installer smoke test

This scenario runs `scripts/install-internal.sh` on Ubuntu 24.04 with Git,
Python 3.12+, venv support, and package-manager access available. The installer checks
external tools and installs its Python dependencies and managed MCP backends
noninteractively. The main route installs wheels and keeps the default Codex
provider; it does not require a fresh Lemex build.

It verifies the CLI wrapper, versioned runtime metadata, bundled skills and
workflow specs, and preservation of user configuration on reinstall. An unchanged
source tree must reuse its validated runtime without replacing its environment. The
LeanExplore data download is skipped to keep the test focused on installation.

The host prepares temporary source archives from tracked and nonignored untracked
working files, preserving executable modes and symlinks. State, caches and common
credential files are excluded. Only the archive directory is mounted read-only;
the container extracts adjacent `LeanFlow` and optional `LeanProbe` source trees.
This works for managed Git worktrees without resolving their host `.git` paths
inside the container and leaves host sources untouched.

With Docker running and network access available:

```bash
tests/installer/ubuntu_repository_local_install_smoke/run.sh
```

The host also needs Python 3 for archive preparation. An adjacent `../LeanProbe`
checkout is included automatically; use `LEANFLOW_LEAN_PROBE_SOURCE` to select a
different matching backend checkout. Released builds can resolve their backend
from the package index when no companion source exists. Set
`LEANFLOW_SMOKE_EVIDENCE_DIR` to retain source manifests and archive hashes; the
temporary archives themselves are removed when the smoke exits.
