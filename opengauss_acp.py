"""Minimal ACP entrypoint for the kernel build."""

from __future__ import annotations

import os
from pathlib import Path


def main() -> int:
    home = Path(os.getenv("OPENGAUSS_HOME", Path.home() / ".opengauss")).expanduser()
    os.environ.setdefault("OPENGAUSS_HOME", str(home))
    os.environ.setdefault("GAUSS_HOME", str(home))
    print("OpenGauss ACP integration is not included in the kernel build.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
