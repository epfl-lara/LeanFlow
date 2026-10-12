"""Reject the retired in-process model runner with native workflow migration guidance."""

from __future__ import annotations


def main() -> None:
    """Direct old module invocations to the supported native workflow CLI."""
    raise SystemExit(
        "The in-process LeanFlow model runner has been removed. "
        "Use leanflow workflow with --provider codex, --provider claude, or --provider lemex. "
        "Configure authentication, model defaults, and context management in that native runtime."
    )


if __name__ == "__main__":
    main()
