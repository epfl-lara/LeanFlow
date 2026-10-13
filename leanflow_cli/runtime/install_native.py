"""Prepare an optional native provider without making a model or login request."""

from __future__ import annotations

import argparse
import subprocess

from leanflow_cli.config import load_config
from leanflow_cli.runtime.lemex_install import LemexInstallError, ensure_lemex


def install_native_provider(*, with_lemex: bool = False) -> bool:
    """Adopt existing Lemex, building only when explicitly requested or selected."""
    config = load_config()
    model = config.get("model", {})
    selected = str(model.get("provider", "codex") if isinstance(model, dict) else "codex")
    required = with_lemex or selected.strip().lower() == "lemex"
    try:
        result = ensure_lemex(build=required, refresh=True)
    except (LemexInstallError, OSError, subprocess.SubprocessError) as exc:
        if required:
            raise
        print(f"Optional Lemex unavailable: {exc}")
        print("Codex and Claude installations can continue. Use --with-lemex to build Lemex.")
        return False
    print(f"Lemex ready: {result['executable']} ({result['source']}; {result['version']})")
    return True


def main() -> None:
    """Apply native-provider setup policy selected by the installer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-lemex", action="store_true")
    arguments = parser.parse_args()
    try:
        install_native_provider(with_lemex=arguments.with_lemex)
    except (LemexInstallError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Lemex installation failed: {exc}\n")


if __name__ == "__main__":
    main()
