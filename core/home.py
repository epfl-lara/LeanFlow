"""Single source of truth for the LeanFlow home directory.

This lives in ``core/`` — the lowest layer — so the session store (:mod:`core.state`), the
clock (:mod:`core.time`) and the ``tools/`` data stores (checkpoints, memories, skills) can all
resolve the home *without* importing the higher ``leanflow_cli`` package.

The home is ``~/.leanflow``, overridable with the ``LEANFLOW_HOME`` env var. The CLI entrypoints
set ``LEANFLOW_HOME`` before anything reads state, so a plain ``getenv`` is enough here.
"""

from __future__ import annotations

import os
from pathlib import Path

HOME_ENV = "LEANFLOW_HOME"
DEFAULT_HOME = Path.home() / ".leanflow"


def leanflow_home() -> Path:
    """Return the active LeanFlow home (``$LEANFLOW_HOME`` or ``~/.leanflow``)."""
    explicit = os.getenv(HOME_ENV, "").strip()
    return Path(explicit).expanduser() if explicit else DEFAULT_HOME
