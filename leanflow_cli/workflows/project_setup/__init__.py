"""Expose shared project preparation and read-only readiness inspection."""

from leanflow_cli.workflows.project_setup.service import prepare_project
from leanflow_cli.workflows.project_setup.state import inspect_project

__all__ = ["inspect_project", "prepare_project"]
