"""Independently verify exact candidates and enforce the allowed axiom set."""

from __future__ import annotations

import hashlib
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.prover.models import Dag, Node, digest, safe_relative_file
from leanflow_cli.workflows.prover.source import (
    SourceDocument,
    project_path,
    read_source,
    sorry_spans,
)

SIGNATURE_SCHEMES = ("legacy", "module")


class LeanVerifier:
    """Keep parent checks separate from model-reported tool results."""

    def __init__(
        self,
        root: Path,
        allowed_axioms: tuple[str, ...],
        timeout_s: int = 180,
        *,
        signature_scheme: str = "legacy",
    ) -> None:
        self.root = root
        self.allowed_axioms = set(allowed_axioms)
        self.configured_timeout_s = timeout_s
        self.remaining_time: Callable[[], float] | None = None
        self.on_operation: Callable[..., Any] | None = None
        self.lock = threading.Lock()
        self.workspaces: set[Path] = set()
        if signature_scheme not in SIGNATURE_SCHEMES:
            raise ValueError(f"unknown signature scheme: {signature_scheme}")
        # "legacy" compiles every kernel profile under one fixed module name;
        # its fingerprints are what earlier runs recorded. "module" compiles
        # under the real module name so the checked artifact is publishable.
        self.signature_scheme = signature_scheme
        # Controller-private directory for checked artifacts. It must never be
        # a writable root of any sandboxed Lean process.
        self.artifact_root: Path | None = None

    def profile_module(self, relative: str) -> str | None:
        """Name the module a kernel profile compiles under, if the scheme fixes it."""
        from leanflow_cli.workflows.prover.type_profile import module_name

        return module_name(relative) if self.signature_scheme == "module" else None

    @property
    def timeout_s(self) -> float:
        """Bound every new verification stage by the remaining campaign deadline."""
        return (
            min(self.configured_timeout_s, self.remaining_time())
            if self.remaining_time
            else self.configured_timeout_s
        )

    def _operation(
        self,
        kind: str,
        label: str,
        action: Callable[[], dict[str, Any]],
        *,
        timeout_s: float | None = None,
        **details: Any,
    ) -> dict[str, Any]:
        """Report long deterministic checks without granting source mutation authority."""
        if self.on_operation is not None:
            return dict(
                self.on_operation(
                    kind,
                    label,
                    action,
                    timeout_s=self.timeout_s if timeout_s is None else timeout_s,
                    **details,
                )
            )
        return action()

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
        files = list(dict.fromkeys(node.file for node in dag.nodes))
        for completed, relative in enumerate(files):
            nodes = [node for node in dag.nodes if node.file == relative]
            mutable_names = list(
                dict.fromkeys(
                    name
                    for node in nodes
                    for name in (node.signature_mutable_names or [item.name for item in nodes])
                )
            )
            with self.lock:
                result = self._operation(
                    "signature_check",
                    "Checking protected declaration types",
                    lambda: compiled_type_profiles(
                        root=self.root,
                        source=documents[relative].render(),
                        names=[node.name for node in nodes],
                        workspace=workspace,
                        timeout_s=self.timeout_s,
                        mutable_names=mutable_names,
                        module=self.profile_module(relative),
                    ),
                    file=relative,
                    completed=completed,
                    total=len(files),
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
        """Queue under campaign time, then share one Lean deadline across verification stages."""
        wait_started = time.monotonic()
        queue_limit = self.remaining_time() if self.remaining_time else self.configured_timeout_s
        queue_deadline = wait_started + queue_limit
        acquired = False

        def acquire() -> dict[str, Any]:
            """Wait for the shared verifier without charging the active Lean allowance."""
            nonlocal acquired
            while not acquired:
                remaining = queue_deadline - time.monotonic()
                if self.remaining_time is not None:
                    remaining = min(remaining, self.remaining_time())
                if remaining <= 0:
                    break
                # Observe cancellation while waiting for another proof's check.
                acquired = self.lock.acquire(timeout=min(0.25, remaining))
            return {
                "accepted": acquired,
                "success": acquired,
                "error_code": "" if acquired else "check_busy",
                "error": "" if acquired else "Verifier queue wait exhausted its available time.",
            }

        try:
            if self.on_operation is not None:
                admission = self.on_operation(
                    "verification_queue",
                    "Waiting for the independent verifier",
                    acquire,
                    node_id=node.id,
                    file=str(file),
                    timeout_s=queue_limit,
                )
            else:
                admission = acquire()
            if not acquired:
                if self.remaining_time is not None:
                    # The campaign callback reports its typed global deadline,
                    # rather than misclassifying normal contention as broken Lean.
                    self.remaining_time()
                return dict(admission)
            waited = time.monotonic() - wait_started
            result = self._check_locked(node, file, skeleton=skeleton)
            return {**result, "verifier_queue_wait_s": round(waited, 3)}
        finally:
            if acquired:
                self.lock.release()

    def _check_locked(self, node: Node, file: Path, *, skeleton: bool) -> dict[str, Any]:
        """Check the exact declaration and kernel type while owning the verifier slot."""
        from leanflow_cli.workflows.prover.check_process import check_scratch

        deadline = time.monotonic() + self.timeout_s

        def elaborate(remaining: float) -> dict[str, Any]:
            """Register the warm worker while holding the verifier's invalidation lock."""
            self.workspaces.add(file.parent)
            return check_scratch(
                project_root=self.root,
                workspace=file.parent,
                file=file,
                declaration=node.name,
                include_axiom_profile=not skeleton,
                allow_placeholders_for_elaboration=skeleton,
                timeout_s=remaining,
            )

        # Provers already use the warm REPL for feedback. Closed submissions are
        # authoritatively re-elaborated by the exact-source compiler below; a
        # second controller REPL pass duplicates that work and retains a large
        # imported environment during the subsequent cold checks. Skeletons
        # still need LeanProbe's placeholder-tolerant elaboration diagnostics.
        result = self._candidate_stage(elaborate, deadline) if skeleton else {}
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
            if not accepted:
                return {**result, "accepted": False}
        if node.original and not node.signature_sha256:
            return {
                **result,
                "accepted": False,
                "error": "original kernel type fingerprint missing",
            }
        from leanflow_cli.workflows.prover.type_profile import compiled_type_profiles

        checked_source = read_source(file)
        module = self.profile_module(node.file)
        artifact = (
            self.artifact_root / f"{node.id}_{node.revision}_{uuid.uuid4().hex[:8]}.olean"
            if self.artifact_root is not None and module is not None and not skeleton
            else None
        )
        profile = self._candidate_stage(
            lambda remaining: compiled_type_profiles(
                root=self.root,
                source=checked_source,
                names=[node.name],
                workspace=file.parent,
                timeout_s=remaining,
                mutable_names=node.signature_mutable_names or [node.name],
                module=module,
                artifact=artifact,
                on_stage=lambda stage, action: self._operation(
                    "verification_stage",
                    (
                        "Compiling exact candidate source"
                        if stage == "compile"
                        else "Inspecting compiled kernel types and axioms"
                    ),
                    action,
                    stage=stage,
                    timeout_s=min(self.timeout_s, max(0, deadline - time.monotonic())),
                    node_id=node.id,
                    file=str(file),
                ),
            ),
            deadline,
        )
        if not profile.get("accepted"):
            return {
                **result,
                "accepted": False,
                "kernel_profile": profile,
                "verification_timing": profile.get("timing", {}),
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
        retained = (
            {
                "compiled_artifact": profile["artifact"],
                "compiled_artifact_sha256": profile["artifact_sha256"],
            }
            if accepted and profile.get("artifact") and profile.get("artifact_sha256")
            else {}
        )
        return {
            **result,
            "success": True,
            "ok": accepted and not skeleton,
            "accepted": accepted,
            "axiom_profile_checked": True,
            "axiom_profile_axioms": kernel_axioms,
            "verification_backend": "compiled_kernel",
            "verification_timing": profile.get("timing", {}),
            "checked_source_sha256": digest(checked_source),
            "kernel_profile": target,
            "type_matches": type_matches,
            **retained,
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

    def _candidate_stage(
        self, action: Callable[[float], dict[str, Any]], deadline: float
    ) -> dict[str, Any]:
        """Share one active candidate deadline across both Lean stages."""
        timeout = {
            "success": False,
            "accepted": False,
            "timed_out": True,
            "error_code": "check_timeout",
            "error": "Independent candidate check exhausted its active verification deadline.",
        }
        remaining = min(self.timeout_s, deadline - time.monotonic())
        return action(remaining) if remaining > 0 else timeout

    def compile_module(self, relative: str) -> dict[str, Any]:
        """Build one generated helper artifact so dependent scratch modules can import it."""
        from leanflow_cli.workflows.prover.check_process import isolated_command

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
                self._publish(output, artifact.read_bytes())
            return {**result, "accepted": accepted}

    def install_module(self, relative: str, artifact: Path, sha256: str) -> dict[str, Any]:
        """Publish an artifact this verifier compiled and inspected from the exact accepted source.

        The bytes must still match the digest recorded when the candidate
        compiler was reaped, and must live under the controller-private artifact
        root; anything else is refused so the caller recompiles instead.
        """
        relative = safe_relative_file(relative)
        output = project_path(
            self.root,
            str(Path(".lake/build/lib/lean") / Path(relative).with_suffix(".olean")),
        )
        with self.lock:
            if self.artifact_root is None:
                return {
                    "accepted": False,
                    "error_code": "artifact_unavailable",
                    "error": "no controller-private artifact root is configured",
                }
            try:
                resolved = artifact.resolve(strict=True)
                if artifact.is_symlink() or not resolved.is_relative_to(
                    self.artifact_root.resolve()
                ):
                    raise ValueError("artifact is outside the controller-private root")
                content = resolved.read_bytes()
            except (OSError, ValueError) as exc:
                return {
                    "accepted": False,
                    "error_code": "artifact_unavailable",
                    "error": str(exc),
                }
            actual = hashlib.sha256(content).hexdigest()
            if not sha256 or actual != sha256:
                return {
                    "accepted": False,
                    "error_code": "artifact_mismatch",
                    "error": "checked artifact bytes no longer match their recorded digest",
                }
            output.parent.mkdir(parents=True, exist_ok=True)
            self._publish(output, content)
            resolved.unlink(missing_ok=True)
            return {"accepted": True, "artifact": str(artifact), "sha256": actual}

    def _publish(self, output: Path, content: bytes) -> None:
        """Replace one importable artifact and drop warm environments that imported it."""
        from leanflow_cli.workflows.prover.check_process import close_check_workers

        output.write_bytes(content)
        # File-content caches cannot observe a changed imported .olean.
        # Only this verifier's workers are invalidated; prover scratch
        # sessions remain independent and their results are rechecked here.
        for checked_workspace in self.workspaces:
            close_check_workers(checked_workspace)
        self.workspaces.clear()

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
