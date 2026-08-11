"""Wrap the Lean tool and Lake primitives used by ``lean_services``.

``LeanBackend`` does not own backend state or discover tools; each method
forwards to the existing service primitive.

Monkeypatch safety: the gate tests patch ``lean_services._invoke_json_tool`` and
``lean_services._run_command`` to stub the backend. So the wrapper resolves those
two names LAZILY off the ``lean_services`` module at call time (``import
leanflow_cli.lean.lean_services``) rather than importing them at module load. That keeps
``monkeypatch.setattr(lean_services, "_invoke_json_tool", ...)`` effective for calls
routed through the wrapper, and — crucially — avoids importing lean_services at this
module's load time, so re-exporting / using ``LeanBackend`` from lean_services
introduces no import cycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import (no cycle)
    from leanflow_cli.lean.lean_services import LeanCapabilityReport


class LeanBackend:
    """Stateless façade over the lean_services backend primitives.

    ``invoke_tool`` / ``run_command`` forward verbatim to
    ``lean_services._invoke_json_tool`` / ``lean_services._run_command`` (resolved
    lazily so test monkeypatches on those names still apply). ``is_available``
    reports whether a managed capability resolved to a backend tool in a probed
    ``LeanCapabilityReport``.
    """

    __slots__ = ()

    def invoke_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Invoke a managed LSP/MCP JSON tool, forwarding to the primitive."""
        from leanflow_cli.lean import lean_services

        return lean_services._invoke_json_tool(name, args)

    def run_command(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        timeout_s: float | None = None,
    ) -> tuple[int, str]:
        """Run a Lake/subprocess command, forwarding to the primitive."""
        from leanflow_cli.lean import lean_services

        if timeout_s is None:
            return lean_services._run_command(cmd, cwd=cwd)
        return lean_services._run_command(cmd, cwd=cwd, timeout_s=timeout_s)

    def is_available(self, report: LeanCapabilityReport, capability: str) -> bool:
        """True when ``capability`` resolved to a non-empty managed tool in ``report``."""
        return bool(report.mcp_tools.get(capability))
