"""Render frozen PDF pages through the bounded, closure-checked subprocess runner."""

from __future__ import annotations

import math
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.regular_files import read_regular_bytes
from leanflow_cli.formalization.evidence import digest
from leanflow_cli.runtime.storage import StoragePressure, storage_policy
from leanflow_cli.workflows.project_setup.process import SetupCancelled, run_command

RENDER_PROTOCOL = "poppler-frozen-page-v1"
RENDER_FLAGS = ("-scale-to", "2200", "-singlefile", "-png")
MAX_IMAGE_BYTES = 4 * 1024**2
PAGE_TIMEOUT_S = 60


def renderer_identity() -> dict[str, str]:
    """Bind the exact installed executable rather than trusting a mutable command name."""
    selected = shutil.which("pdftoppm")
    if not selected:
        raise ValueError("Paired PDF visual review requires the Poppler pdftoppm renderer")
    executable = Path(selected).resolve(strict=True)
    raw = read_regular_bytes(executable, 128 * 1024**2)
    return {"protocol": RENDER_PROTOCOL, "executable": str(executable), "sha256": digest(raw)}


def render_page(
    source: Path,
    target: Path,
    page: int,
    renderer: dict[str, Any],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bytes:
    """Require bounded rendering and physical process/output closure before reading the PNG."""
    if deadline is not None and not math.isfinite(deadline):
        raise ValueError("Visual review requires a finite deadline")
    stop = threading.Event()
    cancel = threading.Event()
    expires = min(
        deadline if deadline is not None else float("inf"),
        time.monotonic() + PAGE_TIMEOUT_S,
    )

    def watch() -> None:
        while not stop.is_set():
            if time.monotonic() >= expires or cancelled is not None and cancelled():
                cancel.set()
                return
            stop.wait(0.05)

    if cancelled is not None and cancelled():
        raise InterruptedError("Source page rendering was cancelled")
    if time.monotonic() >= expires:
        raise TimeoutError("Source page rendering exceeded the campaign deadline")
    free = shutil.disk_usage(target.parent).free
    required = storage_policy().min_free_bytes + 64 * 1024**2
    if free < required:
        raise StoragePressure(free, required)
    watcher = threading.Thread(target=watch, name="leanflow-page-render-deadline", daemon=True)
    watcher.start()
    try:
        with (target.parent / "render.log").open("a", encoding="utf-8") as log:
            code, output = run_command(
                [
                    renderer["executable"],
                    "-f",
                    str(page + 1),
                    "-l",
                    str(page + 1),
                    *RENDER_FLAGS,
                    str(source),
                    str(target),
                ],
                cwd=target.parent,
                log=log,
                cancel=cancel,
                progress=None,
                phase="rendering_source_page",
                stdout_only=True,
                stdout_limit_bytes=16000,
            )
        if code:
            raise ValueError(f"Frozen PDF page {page + 1} could not be rendered: {output[-2000:]}")
        return read_regular_bytes(target.with_suffix(".png"), MAX_IMAGE_BYTES)
    except SetupCancelled as error:
        if cancelled is not None and cancelled():
            raise InterruptedError("Source page rendering was cancelled") from error
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Source page rendering exceeded the campaign deadline") from error
        raise ValueError(
            f"Frozen PDF page {page + 1} exceeded its {PAGE_TIMEOUT_S}-second render limit"
        ) from error
    finally:
        stop.set()
        watcher.join(timeout=1)
        if watcher.is_alive():
            raise RuntimeError(
                "Source-render deadline observer did not close; retain rendering inputs"
            )
