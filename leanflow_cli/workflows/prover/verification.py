"""Independently verify exact candidates and enforce the allowed axiom set."""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.prover.models import Node
from leanflow_cli.workflows.prover.source import read_source, sorry_spans


class LeanVerifier:
    """Keep parent checks separate from model-reported tool results."""

    def __init__(self, root: Path, allowed_axioms: tuple[str, ...], timeout_s: int = 180) -> None:
        self.root = root
        self.allowed_axioms = set(allowed_axioms)
        self.timeout_s = timeout_s
        self.lock = threading.Lock()

    def preflight(self, workspace: Path) -> dict[str, Any]:
        """Check isolated Lean availability before any paid model request."""
        from leanflow_cli.workflows.prover.check_process import preflight_check

        workspace.mkdir(parents=True, exist_ok=True)
        result = preflight_check(
            project_root=self.root, workspace=workspace, timeout_s=self.timeout_s
        )
        return {**result, "accepted": result.get("success") is True and result.get("ok") is True}

    def close(self, workspace: Path) -> None:
        """Release the controller's warm verifier before publishing terminal status."""
        from leanflow_cli.workflows.prover.check_process import close_check_workers

        close_check_workers(workspace)

    def check(self, node: Node, file: Path, *, skeleton: bool = False) -> dict[str, Any]:
        """Check the exact declaration and require complete, permitted axiom evidence."""
        from leanflow_cli.workflows.prover.check_process import check_scratch

        with self.lock:
            result = check_scratch(
                project_root=self.root,
                workspace=file.parent,
                file=file,
                declaration=node.name,
                include_axiom_profile=not skeleton,
                allow_placeholders_for_elaboration=skeleton,
                timeout_s=self.timeout_s,
            )
        if skeleton:
            messages = result.get("messages", [])
            no_errors = not any(
                isinstance(message, dict) and str(message.get("severity", "")).lower() == "error"
                for message in messages
            )
            accepted = (
                result.get("success") is True
                and result.get("ok") is True
                and no_errors
                and not result.get("error_code")
            )
        else:
            axioms = result.get("axiom_profile_axioms")
            accepted = (
                result.get("success") is True
                and result.get("ok") is True
                and result.get("axiom_profile_checked") is True
                and isinstance(axioms, list)
                and set(axioms) <= self.allowed_axioms
                and "sorryAx" not in axioms
            )
        return {**result, "accepted": accepted}

    def compile_module(self, relative: str) -> dict[str, Any]:
        """Build one generated helper artifact so dependent scratch modules can import it."""
        from leanflow_cli.workflows.prover.check_process import isolated_command

        output = (
            self.root / ".lake" / "build" / "lib" / "lean" / Path(relative).with_suffix(".olean")
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="leanflow-helper-check-") as temporary:
            workspace = Path(temporary)
            artifact = workspace / "checked.olean"
            result = isolated_command(
                project_root=self.root,
                workspace=workspace,
                argv=["lake", "env", "lean", "-o", str(artifact), relative],
                timeout_s=self.timeout_s,
                extra_writable_roots=self._lake_config_cache(),
            )
            accepted = (
                result.get("success") is True
                and result.get("returncode") == 0
                and artifact.is_file()
            )
            if accepted:
                output.write_bytes(artifact.read_bytes())
            return {**result, "accepted": accepted}

    def _lake_config_cache(self) -> tuple[Path, ...]:
        """Allow Lake's compiled configuration artifacts while protecting source and dependencies."""
        return tuple(
            self.root / ".lake" / name
            for name in ("lakefile.olean", "lakefile.olean.trace", "lakefile.olean.lock")
            if (self.root / ".lake" / name).exists()
        )

    def final(self, files: list[Path]) -> dict[str, Any]:
        """Require requested source to be sorry-free and the complete project to build."""
        from leanflow_cli.workflows.prover.check_process import isolated_command

        unresolved = [
            str(path.relative_to(self.root)) for path in files if sorry_spans(read_source(path))
        ]
        if unresolved:
            return {"accepted": False, "error": "remaining sorry", "files": unresolved}
        with self.lock, tempfile.TemporaryDirectory(prefix="leanflow-final-check-") as temporary:
            result = isolated_command(
                project_root=self.root,
                workspace=Path(temporary),
                argv=["lake", "build"],
                timeout_s=self.timeout_s,
                extra_writable_roots=(*self._lake_config_cache(), self.root / ".lake" / "build"),
            )
        return {
            **result,
            "accepted": result.get("success") is True and result.get("returncode") == 0,
        }
