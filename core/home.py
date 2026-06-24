"""Single source of truth for the EPFLemma home directory (+ one-time legacy seeding).

This lives in ``core/`` — the lowest layer — so the session store (:mod:`core.state`), the
clock (:mod:`core.time`) and the ``tools/`` data stores (checkpoints, memories, skills) can all
resolve the home *without* importing the higher ``epflemma_cli`` package.

The home is ``~/.epflemma``, overridable with the ``EPFLEMMA_HOME`` env var. The CLI entrypoints
set ``EPFLEMMA_HOME`` before anything reads state, so a plain ``getenv`` is enough here — there is
no longer a ``GAUSS_HOME``/``OPENGAUSS_HOME`` bridge (those legacy names are dropped). Retired
``~/.gauss`` / ``~/.opengauss`` homes are consulted *only* to seed a brand-new home, then never
again.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

HOME_ENV = "EPFLEMMA_HOME"
DEFAULT_HOME = Path.home() / ".epflemma"

# Data entries the one-time seed copies into a fresh home. config.yaml / .env are handled
# separately by epflemma_cli.config (they need format transformation), so they are not listed here.
_SEEDED_ENTRIES: tuple[str, ...] = ("state.db", "checkpoints", "memories", "skills")


def epflemma_home() -> Path:
    """Return the active EPFLemma home (``$EPFLEMMA_HOME`` or ``~/.epflemma``)."""
    explicit = os.getenv(HOME_ENV, "").strip()
    return Path(explicit).expanduser() if explicit else DEFAULT_HOME


def legacy_homes() -> tuple[Path, ...]:
    """Retired Gauss/OpenGauss homes, newest-first.

    Read-only — only ever consulted as a migration *source* to seed a fresh EPFLemma home, never as
    the active home. Resolved at call time (not import time) so tests can redirect it away from the
    developer's real retired homes.
    """
    return (Path.home() / ".opengauss", Path.home() / ".gauss")


def migrate_legacy_home(home: Path | None = None) -> None:
    """Seed a fresh EPFLemma home from a retired ``~/.gauss`` / ``~/.opengauss`` home, once.

    Conservative and idempotent: each entry is seeded only when it is *entirely absent* from the
    active home, so an existing EPFLemma install is never touched. Copies (not moves) so the legacy
    home survives as a backup. A copy that fails partway is removed so the next start can retry
    rather than leaving truncated state behind. Safe to call on every startup.
    """
    home = (home or epflemma_home()).expanduser()
    for entry in _SEEDED_ENTRIES:
        target = home / entry
        if target.exists():
            continue
        for legacy in legacy_homes():
            source = (legacy / entry).expanduser()
            if not source.exists() or source.resolve() == target.resolve():
                continue
            home.mkdir(parents=True, exist_ok=True)
            try:
                if source.is_dir():
                    shutil.copytree(source, target)
                else:
                    shutil.copy2(source, target)
            except OSError:
                # Best-effort seed; drop any partial copy so the next startup retries cleanly.
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            break
