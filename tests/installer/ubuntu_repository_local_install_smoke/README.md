# Ubuntu installer smoke test

This scenario runs `scripts/install-internal.sh` on Ubuntu 24.04 with Git,
Python 3.12+, venv support, and package-manager access available. The installer checks
external tools and installs its Python dependencies and managed MCP backends.

It verifies the CLI wrapper, versioned runtime metadata, bundled skills and
workflow specs, and preservation of user configuration on reinstall. An unchanged
source tree must reuse its validated runtime without replacing its environment. The
LeanExplore data download is skipped to keep the test focused on installation.

The host checkout is mounted read-only. Tracked files are copied into the
container before installation, so the test does not create files in the checkout.

With Docker running and network access available:

```bash
tests/installer/ubuntu_repository_local_install_smoke/run.sh
```
