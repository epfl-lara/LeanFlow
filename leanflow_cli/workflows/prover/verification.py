"""Independently verify exact candidates and enforce the allowed axiom set."""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.prover.models import Dag, Node, safe_relative_file
from leanflow_cli.workflows.prover.source import (
    SourceDocument,
    project_path,
    read_source,
    sorry_spans,
)


class LeanVerifier:
    """Keep parent checks separate from model-reported tool results."""

    def __init__(self, root: Path, allowed_axioms: tuple[str, ...], timeout_s: int = 180) -> None:
        self.root = root
        self.allowed_axioms = set(allowed_axioms)
        self.timeout_s = timeout_s
        self.lock = threading.Lock()
        self.workspaces: set[Path] = set()

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

    def capture_signatures(
        self,
        dag: Dag,
        documents: dict[str, SourceDocument],
        workspace: Path,
        *,
        initialize: bool = True,
    ) -> dict[str, Any]:
        """Capture original kernel types or verify that planned imports preserve them.

        Call before any research dependency or helper materialization. Later
        calls compare every existing fingerprint; only newly created nodes may
        acquire their first fingerprint during materialization.
        """
        from leanflow_cli.workflows.prover.type_profile import compiled_type_profiles

        pending: dict[str, str] = {}
        mutable_by_id: dict[str, list[str]] = {}
        for relative in dict.fromkeys(node.file for node in dag.nodes):
            nodes = [node for node in dag.nodes if node.file == relative]
            mutable_names = list(
                dict.fromkeys(
                    name
                    for node in nodes
                    for name in (node.signature_mutable_names or [item.name for item in nodes])
                )
            )
            with self.lock:
                result = compiled_type_profiles(
                    root=self.root,
                    source=documents[relative].render(),
                    names=[node.name for node in nodes],
                    workspace=workspace,
                    timeout_s=self.timeout_s,
                    mutable_names=mutable_names,
                )
            if not result.get("accepted"):
                return {**result, "file": relative}
            for node in nodes:
                fingerprint = result["profiles"][node.name]["sha256"]
                if node.signature_sha256 and node.signature_sha256 != fingerprint:
                    return {
                        "accepted": False,
                        "error": "protected declaration kernel type changed",
                        "node_id": node.id,
                    }
                if not node.signature_sha256 and node.original and not initialize:
                    return {
                        "accepted": False,
                        "error": "original kernel type fingerprint missing",
                        "node_id": node.id,
                    }
                pending[node.id] = fingerprint
                mutable_by_id[node.id] = mutable_names
        for node in dag.nodes:
            node.signature_sha256 = pending[node.id]
            node.signature_mutable_names = mutable_by_id[node.id]
        return {"accepted": True, "signatures": pending}

    def check(self, node: Node, file: Path, *, skeleton: bool = False) -> dict[str, Any]:
        """Check the exact declaration and require complete, permitted axiom evidence."""
        from leanflow_cli.workflows.prover.check_process import check_scratch

        with self.lock:
            self.workspaces.add(file.parent)
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
            # LeanProbe keeps ok=False for an elaborated declaration containing
            # sorry. Skeleton acceptance still requires the independent compile
            # and matching kernel type below; it never counts as a finished proof.
            elaborated = result.get("ok") is True or (
                result.get("has_sorry") is True and result.get("has_errors") is False
            )
            accepted = (
                result.get("success") is True
                and elaborated
                and no_errors
                and not result.get("error_code")
                and not result.get("timed_out")
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
        if not accepted:
            return {**result, "accepted": False}
        if node.original and not node.signature_sha256:
            return {
                **result,
                "accepted": False,
                "error": "original kernel type fingerprint missing",
            }
        from leanflow_cli.workflows.prover.type_profile import compiled_type_profiles

        with self.lock:
            profile = compiled_type_profiles(
                root=self.root,
                source=read_source(file),
                names=[node.name],
                workspace=file.parent,
                timeout_s=self.timeout_s,
                mutable_names=node.signature_mutable_names or [node.name],
            )
        if not profile.get("accepted"):
            return {
                **result,
                "accepted": False,
                "kernel_profile": profile,
                "error": profile.get("error", "kernel profile unavailable"),
            }
        target = profile["profiles"][node.name]
        type_matches = not node.signature_sha256 or node.signature_sha256 == target["sha256"]
        kernel_axioms = target["axioms"]
        permitted_axioms = self.allowed_axioms | ({"sorryAx"} if skeleton else set())
        kernel_safe = set(kernel_axioms) <= permitted_axioms and (
            skeleton or "sorryAx" not in kernel_axioms
        )
        accepted = type_matches and kernel_safe
        if accepted and skeleton and not node.signature_sha256:
            node.signature_sha256 = target["sha256"]
        return {
            **result,
            "accepted": accepted,
            "kernel_profile": target,
            "type_matches": type_matches,
            "error": (
                "protected declaration kernel type changed"
                if not type_matches
                else (
                    "compiled declaration uses disallowed axioms"
                    if not kernel_safe
                    else result.get("error", "")
                )
            ),
        }

    def compile_module(self, relative: str) -> dict[str, Any]:
        """Build one generated helper artifact so dependent scratch modules can import it."""
        from leanflow_cli.workflows.prover.check_process import (
            close_check_workers,
            isolated_command,
        )

        relative = safe_relative_file(relative)
        output = project_path(
            self.root,
            str(Path(".lake/build/lib/lean") / Path(relative).with_suffix(".olean")),
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, tempfile.TemporaryDirectory(prefix="leanflow-helper-check-") as temporary:
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
                # File-content caches cannot observe a changed imported .olean.
                # Only this verifier's workers are invalidated; prover scratch
                # sessions remain independent and their results are rechecked here.
                for checked_workspace in self.workspaces:
                    close_check_workers(checked_workspace)
                self.workspaces.clear()
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
        build_root = project_path(self.root, ".lake/build")
        build_root.mkdir(parents=True, exist_ok=True)
        with self.lock, tempfile.TemporaryDirectory(prefix="leanflow-final-check-") as temporary:
            result = isolated_command(
                project_root=self.root,
                workspace=Path(temporary),
                argv=["lake", "build"],
                timeout_s=self.timeout_s,
                extra_writable_roots=(*self._lake_config_cache(), build_root),
            )
        return {
            **result,
            "accepted": result.get("success") is True and result.get("returncode") == 0,
        }
