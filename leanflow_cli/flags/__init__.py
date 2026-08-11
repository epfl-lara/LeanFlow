"""Declared catalog of LeanFlow's ``LEANFLOW_*`` runtime knobs.

The runtime reads its feature switches and budgets straight from the process
environment, which keeps the leaf modules dependency-light but leaves no single
place that answers "which knobs exist, what do they default to, and which ones
are worth ablating?". This package is that place.

The catalog is descriptive, not authoritative: flipping an entry here changes no
behavior. Each :class:`FlagSpec` records the name, value type, declared default,
and the modules that read it, so the CLI, the VS Code extension, and the
evaluation harness can present one consistent knob surface.
``tests/leanflow/test_flag_catalog.py`` proves the catalog stays in step with
the source that actually reads these names.
"""

from leanflow_cli.flags.catalog import (
    FLAG_CATALOG,
    catalog_payload,
    flag_groups,
    flags_by_kind,
    lookup_flag,
)
from leanflow_cli.flags.resolve import (
    BUILTIN_PROFILES,
    effective_flag_values,
    profile_payload,
    resolve_profile,
)
from leanflow_cli.flags.spec import FlagKind, FlagSpec, FlagValueType

__all__ = [
    "BUILTIN_PROFILES",
    "FLAG_CATALOG",
    "FlagKind",
    "FlagSpec",
    "FlagValueType",
    "catalog_payload",
    "effective_flag_values",
    "flag_groups",
    "flags_by_kind",
    "lookup_flag",
    "profile_payload",
    "resolve_profile",
]
