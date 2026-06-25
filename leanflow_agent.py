"""LeanFlow-owned wrapper around the shared agent runner."""

from __future__ import annotations

import os


def main() -> None:
    from core.home import leanflow_home

    home = leanflow_home()
    os.environ.setdefault("LEANFLOW_HOME", str(home))

    from run_agent import main as run_agent_main

    run_agent_main()


if __name__ == "__main__":
    main()
