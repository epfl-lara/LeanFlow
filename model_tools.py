"""Backward-compat shim: the tool-registry API moved to core/model_tools.py.

Kept at the top level because `from model_tools import get_tool_definitions, handle_function_call,
check_toolset_requirements` is the documented public surface used across the repo and tests.
"""

from core.model_tools import *  # noqa: F401,F403
from core.model_tools import _run_async  # noqa: F401  (used by leanflow_cli at runtime)
