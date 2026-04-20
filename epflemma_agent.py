"""EPFLemma-owned wrapper around the shared agent runner."""

from __future__ import annotations

import os
from pathlib import Path


def main() -> None:
    home = Path(
        os.getenv("EPFLEMMA_HOME") or os.getenv("OPENGAUSS_HOME") or (Path.home() / ".epflemma")
    ).expanduser()
    os.environ.setdefault("EPFLEMMA_HOME", str(home))
    os.environ.setdefault("OPENGAUSS_HOME", str(home))
    os.environ.setdefault("GAUSS_HOME", str(home))

    from run_agent import main as run_agent_main

    run_agent_main()


if __name__ == "__main__":
    main()
