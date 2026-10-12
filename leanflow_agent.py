"""Explain migration from the retired direct-API agent entrypoint."""

from __future__ import annotations


def main() -> None:
    """Refuse the removed engine and direct users to native workflow backends."""
    raise SystemExit(
        "leanflow-agent has been removed. Use `leanflow workflow prove FILE --provider "
        "codex|claude|lemex` or `leanflow workflow formalize FILE --provider codex|claude|lemex`. "
        "Each backend uses its native CLI setup."
    )


if __name__ == "__main__":
    main()
