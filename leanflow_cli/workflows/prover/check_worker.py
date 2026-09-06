"""Serve bounded LeanProbe checks inside the controller's OS write sandbox."""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

_MAX_MESSAGE = 4 * 1024 * 1024


def _private_repl(source: Path, private: Path) -> Path:
    """Copy mutable REPL configuration while sharing its prebuilt outputs read-only."""
    target = private / "repl"
    target.mkdir(mode=0o700)
    for filename in ("lean-toolchain", "lakefile.toml", "lakefile.lean", "lake-manifest.json"):
        path = source / filename
        if path.is_file():
            shutil.copyfile(path, target / filename)
    # LeanInteract versions differ in whether they rewrite the toolchain marker.
    # The 150+ MB executable and compiled Lean artifacts need no copy or rebuild.
    (target / ".lake").mkdir()
    (target / ".lake" / "build").symlink_to(source / ".lake" / "build", target_is_directory=True)
    return target


def _configure(project: Path, private: Path) -> Any:
    """Redirect LeanInteract cache and admission sidecars without rebuilding Lean."""

    class SameGroupProcess(subprocess.Popen[Any]):
        """Keep LeanInteract's normally detached REPL in the owned process group."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["start_new_session"] = False
            kwargs.pop("process_group", None)
            super().__init__(*args, **kwargs)

    setattr(subprocess, "Popen", SameGroupProcess)
    # Older LeanInteract releases create a package-local cache during import.
    # Redirect precisely that mkdir before loading it; never grant site-packages writes.
    spec = importlib.util.find_spec("lean_interact")
    if spec is None or not spec.origin:
        raise RuntimeError("lean-interact is unavailable in the LeanFlow Python environment.")
    installed_cache = Path(spec.origin).parent / "cache"
    cache = private / "cache"
    original_makedirs = os.makedirs

    def private_cache_makedirs(name: Any, mode: int = 0o777, exist_ok: bool = False) -> None:
        destination = cache if Path(name).resolve() == installed_cache.resolve() else name
        original_makedirs(destination, mode=mode, exist_ok=exist_ok)

    os.makedirs = private_cache_makedirs
    try:
        import lean_interact
    finally:
        os.makedirs = original_makedirs
    lean_interact.LeanREPLConfig = functools.partial(lean_interact.LeanREPLConfig, cache_dir=cache)

    from core import project_resource_admission as admission
    from leanflow_cli.lean import lean_incremental

    # Admission uses private sidecars in this worker. The controller owns global
    # scheduling; a proof metaprogram must never open its resource gates for writing.
    gates = private / "resource-gates"
    admission._lock_path = lambda root: gates / "lean-heavy.lock"
    admission._priority_state_lock_path = lambda root: gates / "lean-heavy-priority.lock"
    admission._priority_waiter_root = lambda root: gates / "lean-heavy-foreground-waiters"
    probe = lean_incremental._probe()
    lake = shutil.which("lake")
    if lake is None:
        raise RuntimeError("Lake is unavailable on PATH. Install the project's Lean toolchain.")
    probe.lake_path = Path(lake)
    repl = lean_incremental._local_repl_dir(project)
    if repl is None and (project / ".lake" / "build" / "bin" / "repl").is_file():
        repl = project
    if repl is None:
        raise RuntimeError(
            "A built Lean REPL is required. Run `leanflow project init "
            + str(project)
            + "` before proving; for lakefile.lean, follow its printed REPL dependency instructions, then run `lake update repl` and `lake build repl` in the project."
        )
    probe.local_repl_path = _private_repl(repl, private)
    lean_incremental._local_repl_dir = lambda project_root: probe.local_repl_path
    return lean_incremental


def _check(module: Any, project: Path, workspace: Path, raw: Any) -> dict[str, Any]:
    """Validate one read target and return LeanFlow's unchanged incremental verdict."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        raise ValueError("Malformed Lean checker request.")
    file = Path(str(raw.get("file", ""))).resolve(strict=True)
    if not file.is_file() or not (file.is_relative_to(workspace) or file.is_relative_to(project)):
        raise ValueError("Requested Lean file is outside the isolated checker read roots.")
    declaration = str(raw.get("declaration", ""))
    return dict(
        module.lean_incremental_check(
            action="check_target" if declaration else "check_file",
            file_path=str(file),
            theorem_id=declaration,
            replacement=str(raw.get("replacement", "")),
            cwd=str(project),
            timeout_s=max(1, int(raw.get("timeout_s", 60))),
            timeout_ceiling_s=max(1, int(raw.get("timeout_s", 60))),
            include_axiom_profile=raw.get("include_axiom_profile") is True,
            allow_placeholders_for_elaboration=raw.get("allow_placeholders_for_elaboration")
            is True,
        )
    )


def main() -> int:
    """Process JSON-lines requests while preserving one warm LeanProbe instance."""
    project, workspace, private = (Path(value).resolve(strict=True) for value in sys.argv[1:4])
    # Preserve the protocol descriptor; library notices must not corrupt its JSON stream.
    output = sys.stdout
    sys.stdout = sys.stderr
    module: Any = None
    startup_error = ""
    try:
        module = _configure(project, private)
    except Exception as exc:
        startup_error = str(exc)
    try:
        while True:
            line = sys.stdin.buffer.readline(_MAX_MESSAGE + 1)
            if not line:
                return 0
            if len(line) > _MAX_MESSAGE or not line.endswith(b"\n"):
                return 2
            request: Any = None
            try:
                request = json.loads(line)
                if startup_error:
                    raise RuntimeError(startup_error)
                result = _check(module, project, workspace, request)
            except Exception as exc:
                result = {
                    "success": False,
                    "ok": False,
                    "error_code": (
                        "check_setup_failed" if startup_error else "isolated_lean_check_failed"
                    ),
                    "error": str(exc)[:16000],
                }
            response = {
                "id": request.get("id") if isinstance(request, dict) else None,
                "result": result,
            }
            encoded = json.dumps(response, ensure_ascii=False, default=str)
            if len(encoded.encode("utf-8")) > _MAX_MESSAGE - 1:
                encoded = json.dumps(
                    {
                        "id": response["id"],
                        "result": {
                            "success": False,
                            "ok": False,
                            "error_code": "check_response_too_large",
                            "error": "Lean feedback exceeded 4 MiB.",
                        },
                    }
                )
            output.write(encoded + "\n")
            output.flush()
    finally:
        if module is not None:
            module.close_incremental_sessions()


if __name__ == "__main__":
    raise SystemExit(main())
