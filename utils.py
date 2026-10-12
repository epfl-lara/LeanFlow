"""Backward-compat shim: atomic-write helpers moved to core/utils.py (public `utils.atomic_*`)."""

import yaml  # noqa: F401 - public utils.yaml.dump patch target

from core.utils import *  # noqa: F401,F403
