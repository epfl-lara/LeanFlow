"""Negotiate and report the index flags between lean-lsp-mcp and a built Loogle.

lean-lsp-mcp 0.26.x builds Loogle's search index with ``--write-index`` and starts
Loogle with ``--read-index``. Current Loogle replaced both with
``--index-mode {use,read,write,none}`` and ``--index-file PATH``. The unpatched
client therefore logs ``unknown long option '--write-index'`` (the readiness
check's ``loogle-diagnostic.json``), never caches an index in the managed
directory, and every Loogle start pays the full index build (107 s measured).

This module owns three things: classifying which dialect a client or a binary
speaks, patching the managed client so it probes the binary instead of assuming
a dialect, and the compatibility status that ``leanflow doctor`` and
``managed_mcp_power_status`` report. Builds are recorded in
:data:`LOOGLE_BUILD_RECORD` by :mod:`leanflow_cli.cli.loogle_local` so status
checks never have to start Loogle.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from leanflow_cli.cli.mcp_bootstrap import _read_lean_toolchain, managed_mcp_venv_dir
from leanflow_cli.config import get_leanflow_home

#: Written next to a managed build so status checks can report the built binary's
#: index-flag dialect without spawning it again.
LOOGLE_BUILD_RECORD = "loogle-build.json"
_INDEX_FLAGS_MARKER = "_leanflow_index_dialect"
_LEGACY_INDEX_FLAG = '"--write-index"'
_MODERN_INDEX_FLAG = '"--index-mode"'


def loogle_binary_path(repo_dir: Path) -> Path:
    """Return where ``lake build`` leaves the loogle executable in a cache repo."""
    return repo_dir / ".lake" / "build" / "bin" / ("loogle.exe" if os.name == "nt" else "loogle")


def loogle_binary_index_dialect(binary: Path, *, timeout: int = 60) -> str:
    """Return the index-flag dialect a built loogle binary accepts.

    ``loogle --help`` prints usage without importing Mathlib, so the probe is
    cheap. Returns ``index-mode`` (``--index-mode``/``--index-file``),
    ``write-index`` (the older ``--write-index``/``--read-index`` pair), ``none``
    (no on-disk index support at all), ``missing`` (no binary), or ``error: …``
    when the probe could not run.
    """
    if not binary.is_file():
        return "missing"
    try:
        probe = subprocess.run(
            [str(binary), "--help"],
            cwd=str(binary.parent),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LAKE_ARTIFACT_CACHE": "false"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"error: {exc}"
    text = f"{probe.stdout or ''}\n{probe.stderr or ''}"
    if "--index-mode" in text:
        return "index-mode"
    if "--write-index" in text:
        return "write-index"
    return "none"


def loogle_source_index_dialect(repo_dir: Path) -> str:
    """Return the dialect declared by the Loogle sources of a cache repo (no subprocess)."""
    texts: list[str] = []
    for path in (repo_dir / "LoogleMain.lean", repo_dir / "Loogle.lean"):
        with contextlib.suppress(OSError):
            texts.append(path.read_text(encoding="utf-8"))
    if not texts:
        return "missing"
    joined = "\n".join(texts)
    if _MODERN_INDEX_FLAG in joined:
        return "index-mode"
    if _LEGACY_INDEX_FLAG in joined:
        return "write-index"
    return "none"


def build_record_path(cache_dir: Path) -> Path:
    return cache_dir / LOOGLE_BUILD_RECORD


def read_build_record(cache_dir: Path) -> dict[str, Any]:
    """Return the JSON record a managed build wrote, or ``{}``."""
    try:
        payload = json.loads(build_record_path(cache_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_build_record(cache_dir: Path, record: dict[str, Any]) -> None:
    """Persist a build record; a failed write only costs a slower later status check."""
    with contextlib.suppress(OSError):
        build_record_path(cache_dir).write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def managed_client_loogle_module(home_path: Path) -> Path | None:
    """Locate ``lean_lsp_mcp/loogle.py`` inside the managed lean-lsp virtualenv."""
    venv_dir = managed_mcp_venv_dir("lean-lsp", home_path)
    candidates = [
        *venv_dir.glob("lib/python*/site-packages/lean_lsp_mcp/loogle.py"),
        venv_dir / "Lib" / "site-packages" / "lean_lsp_mcp" / "loogle.py",
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def client_index_flags(client_source: str) -> str:
    """Classify which index flags a lean-lsp-mcp ``loogle.py`` sends to the binary."""
    if _INDEX_FLAGS_MARKER in client_source:
        return "negotiated"
    if _LEGACY_INDEX_FLAG in client_source:
        return "legacy-write-index"
    if _MODERN_INDEX_FLAG in client_source:
        return "index-mode"
    return "unknown"


def index_flags_compatible(client: str, binary: str) -> bool | None:
    """Return whether a client dialect can drive a binary dialect; ``None`` when unknown."""
    if binary in {"missing", "unknown"} or binary.startswith("error"):
        return None
    if client == "negotiated":
        return True
    if client == "legacy-write-index":
        return binary == "write-index"
    if client == "index-mode":
        return binary == "index-mode"
    return None


def describe_index_flags(status: dict[str, Any]) -> str:
    """Render one sentence a doctor report can print about client/binary index flags."""
    client = str(status.get("client_index_flags") or "unknown")
    binary = str(status.get("binary_index_flags") or "missing")
    if not status.get("client_found"):
        return "managed lean-lsp client is not installed"
    if binary == "missing":
        return "no managed Loogle build for this toolchain yet"
    if binary.startswith("error"):
        return f"Loogle binary probe failed ({binary})"
    compatible = status.get("compatible")
    if compatible is True:
        if client == "negotiated":
            if binary == "none":
                return "client negotiates index flags; this Loogle build has no on-disk index"
            return f"client negotiates index flags; Loogle accepts --{binary}"
        return f"client and Loogle both use --{binary}"
    if compatible is False:
        if client == "legacy-write-index":
            return (
                "client sends --write-index/--read-index but the built Loogle only accepts "
                "--index-mode/--index-file, so the managed index is never written and every "
                "start rebuilds it"
            )
        return f"client sends --{client} flags but the built Loogle accepts --{binary}"
    return f"compatibility unknown (client {client}, Loogle {binary})"


def managed_loogle_client_status(
    home: str | os.PathLike[str] | None = None,
    *,
    project_root: str | os.PathLike[str] | None = None,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    """Report whether the managed lean-lsp client can drive the built Loogle's index.

    Reads the installed client source and the per-toolchain build record (or the
    Loogle sources as a fallback) without starting either program, so doctor and
    status calls stay cheap. ``compatible`` is ``None`` when either side is absent.
    """
    home_path = Path(home).expanduser().resolve() if home else get_leanflow_home()
    module = managed_client_loogle_module(home_path)
    status: dict[str, Any] = {
        "client_path": str(module) if module else "",
        "client_found": module is not None,
        "client_index_flags": "unknown",
        "client_patched": False,
        "binary_path": "",
        "binary_found": False,
        "binary_index_flags": "missing",
        "binary_index_flags_source": "",
        "compatible": None,
        "detail": "",
    }
    if module is not None:
        try:
            source = module.read_text(encoding="utf-8")
        except OSError:
            source = ""
        status["client_index_flags"] = client_index_flags(source)
        status["client_patched"] = status["client_index_flags"] == "negotiated"
    if cache_dir is None:
        # Imported here: loogle_local depends on this module for build records.
        from leanflow_cli.cli.loogle_local import loogle_cache_dir_for_project

        cache_dir = loogle_cache_dir_for_project(home_path, project_root)
    repo_dir = cache_dir / "repo"
    binary = loogle_binary_path(repo_dir)
    status["binary_path"] = str(binary)
    status["binary_found"] = binary.is_file()
    record = read_build_record(cache_dir)
    recorded = str(record.get("index_dialect") or "")
    if binary.is_file() and recorded and record.get("toolchain") == _read_lean_toolchain(repo_dir):
        status["binary_index_flags"] = recorded
        status["binary_index_flags_source"] = "build-record"
    elif repo_dir.is_dir():
        status["binary_index_flags"] = loogle_source_index_dialect(repo_dir)
        status["binary_index_flags_source"] = "source-scan"
    status["compatible"] = index_flags_compatible(
        str(status["client_index_flags"]), str(status["binary_index_flags"])
    )
    status["detail"] = describe_index_flags(status)
    return status


def patch_lean_lsp_loogle_index_flags(venv_dir: Path) -> bool:
    """Patch lean-lsp-mcp to negotiate Loogle's index flags instead of assuming them.

    The patch probes ``loogle --help`` once per manager, builds the index with the
    flags that binary accepts, and starts Loogle with ``--index-mode use`` so a
    missing or stale index self-heals into the managed cache and is shared by
    every server of that project. Idempotent via the ``_leanflow_index_dialect``
    marker; returns ``False`` when the client layout is not the known one.
    """
    candidates = [
        *venv_dir.glob("lib/python*/site-packages/lean_lsp_mcp/loogle.py"),
        venv_dir / "Lib" / "site-packages" / "lean_lsp_mcp" / "loogle.py",
    ]
    target = next((candidate for candidate in candidates if candidate.is_file()), None)
    if target is None:
        return False
    text = target.read_text(encoding="utf-8")
    if _INDEX_FLAGS_MARKER in text:
        return True
    build_needle = "    def _build_index(self) -> Path | None:\n"
    build_cmd_needle = (
        '        cmd = [str(self.binary_path), "--write-index", str(index_path), "--json"]\n'
        "        for path in self._extra_paths:\n"
        '            cmd.extend(["--path", str(path)])\n'
        '        cmd.append("")  # Empty query for index building\n'
    )
    start_needle = (
        '        cmd = [str(self.binary_path), "--json", "--interactive"]\n'
        "        if (idx := self._get_index_path()).exists():\n"
        '            cmd.extend(["--read-index", str(idx)])\n'
    )
    if not all(needle in text for needle in (build_needle, build_cmd_needle, start_needle)):
        return False
    helper = (
        "    def _leanflow_index_dialect(self) -> str:\n"
        '        """LeanFlow: return which index flags the built loogle binary accepts."""\n'
        '        cached = self.__dict__.get("_leanflow_index_dialect_cache")\n'
        "        if cached:\n"
        "            return cached\n"
        '        dialect = "unknown"\n'
        "        try:\n"
        '            probe = self._run([str(self.binary_path), "--help"], timeout=60)\n'
        '            text = (probe.stdout or "") + (probe.stderr or "")\n'
        '            if "--index-mode" in text:\n'
        '                dialect = "index-mode"\n'
        '            elif "--write-index" in text:\n'
        '                dialect = "write-index"\n'
        "            else:\n"
        '                dialect = "none"\n'
        "        except Exception as exc:\n"
        '            logger.warning("Loogle index-flag probe failed: %s", exc)\n'
        '        if dialect != "unknown":\n'
        '            self.__dict__["_leanflow_index_dialect_cache"] = dialect\n'
        "        return dialect\n"
        "\n"
        "    def _build_index(self) -> Path | None:\n"
    )
    build_cmd_replacement = (
        "        # LeanFlow: use the index flags the built binary actually accepts\n"
        "        dialect = self._leanflow_index_dialect()\n"
        '        if dialect == "index-mode":\n'
        "            cmd = [\n"
        "                str(self.binary_path),\n"
        '                "--index-mode",\n'
        '                "write",\n'
        '                "--index-file",\n'
        "                str(index_path),\n"
        '                "--json",\n'
        "            ]\n"
        "            for path in self._extra_paths:\n"
        '                cmd.extend(["--path", str(path)])\n'
        '        elif dialect == "write-index":\n'
        '            cmd = [str(self.binary_path), "--write-index", str(index_path), "--json"]\n'
        "            for path in self._extra_paths:\n"
        '                cmd.extend(["--path", str(path)])\n'
        '            cmd.append("")  # Empty query for index building\n'
        "        else:\n"
        "            logger.warning(\n"
        '                "Loogle binary accepts no known index flags (%s); skipping index build",\n'
        "                dialect,\n"
        "            )\n"
        "            return None\n"
    )
    start_replacement = (
        '        cmd = [str(self.binary_path), "--json", "--interactive"]\n'
        "        # LeanFlow: use the index flags the built binary actually accepts\n"
        "        dialect = self._leanflow_index_dialect()\n"
        "        idx = self._get_index_path()\n"
        '        if dialect == "index-mode":\n'
        "            self.index_dir.mkdir(parents=True, exist_ok=True)\n"
        '            cmd.extend(["--index-mode", "use", "--index-file", str(idx)])\n'
        '        elif dialect == "write-index" and idx.exists():\n'
        '            cmd.extend(["--read-index", str(idx)])\n'
    )
    patched = text.replace(build_needle, helper, 1)
    patched = patched.replace(build_cmd_needle, build_cmd_replacement, 1)
    patched = patched.replace(start_needle, start_replacement, 1)
    target.write_text(patched, encoding="utf-8")
    return True
