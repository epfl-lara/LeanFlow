"""Shared constants for LeanFlow.

Import-safe module with no dependencies — can be imported from anywhere
without risk of circular imports.
"""

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODELS_URL = f"{OPENROUTER_BASE_URL}/models"

# Cooperative interrupt used to close one managed model turn while leaving the
# enclosing Lean campaign alive.  Keep this in the dependency-free kernel so
# delegated workers can recognize the boundary without importing native_runner.
WORKFLOW_STEP_BOUNDARY_INTERRUPT = "[leanflow-native workflow step boundary]"
