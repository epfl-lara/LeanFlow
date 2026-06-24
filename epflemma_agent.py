"""EPFLemma-owned wrapper around the shared agent runner."""

from __future__ import annotations

import os


def main() -> None:
    from core.home import epflemma_home, migrate_legacy_home

    home = epflemma_home()
    os.environ.setdefault("EPFLEMMA_HOME", str(home))
    migrate_legacy_home(home)

    from run_agent import main as run_agent_main

    run_agent_main()


if __name__ == "__main__":
    main()
