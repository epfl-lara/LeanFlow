"""Enforce opt-in clean-room boundaries for repository and solution research."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

DISABLE_REPOSITORY_RESEARCH_ENV = "LEANFLOW_DISABLE_REPOSITORY_RESEARCH"
DISABLE_SOLUTION_RESEARCH_ENV = "LEANFLOW_DISABLE_SOLUTION_RESEARCH"
CLEAN_ROOM_TASK_LABELS_ENV = "LEANFLOW_CLEAN_ROOM_TASK_LABELS"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_REPOSITORY_HOSTS = frozenset(
    {
        "bitbucket.org",
        "codeberg.org",
        "gist.github.com",
        "github.com",
        "gitlab.com",
        "raw.github.com",
        "raw.githubusercontent.com",
        "sourcegraph.com",
    }
)
_REPOSITORY_HOST_SUFFIXES = (".github.io",)
_GIT_COMMAND_RE = re.compile(
    r"(?:^|[\s;&|()])(?:[A-Za-z]:)?(?:[^\s;&|()]*/)?git(?=$|[\s;&|()])",
    re.IGNORECASE,
)
_REPOSITORY_TRANSITIVE_COMMAND_RE = re.compile(
    r"(?:^|[\s;&|()])(?:[^\s;&|()]*/)?(?:"
    r"lake\s+(?:update|init|new)\b|"
    r"leanflow\s+project\s+init\b"
    r")",
    re.IGNORECASE,
)
_NETWORK_CAPABLE_COMMAND_RE = re.compile(
    r"(?:"
    r"https?://|"
    r"(?:^|[\s;&|()])(?:[^\s;&|()]*/)?(?:"
    r"curl|wget|http|https|httpie|aria2c|lynx|links|elinks"
    r")(?=$|[\s;&|()])|"
    r"\b(?:requests|urllib|urlopen|aiohttp|httpx|socket)\b"
    r")",
    re.IGNORECASE,
)


def repository_research_disabled() -> bool:
    """Return whether this process must avoid repository-backed research."""
    raw = str(os.getenv(DISABLE_REPOSITORY_RESEARCH_ENV, "") or "")
    return raw.strip().lower() in _TRUE_VALUES


def solution_research_disabled() -> bool:
    """Return whether this process must avoid prior solutions to the active task."""
    raw = str(os.getenv(DISABLE_SOLUTION_RESEARCH_ENV, "") or "")
    return raw.strip().lower() in _TRUE_VALUES


def clean_room_task_labels() -> tuple[str, ...]:
    """Return non-empty task labels whose appearance identifies solution research."""
    raw = str(os.getenv(CLEAN_ROOM_TASK_LABELS_ENV, "") or "")
    return tuple(label.strip() for label in raw.split("|") if label.strip())


def _compact_research_text(value: str) -> str:
    """Return lowercase alphanumeric text for punctuation-insensitive label matching."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def solution_research_text_block_reason(text: str, *, surface: str) -> str:
    """Return a denial reason when text names the clean-room task itself."""
    if not solution_research_disabled():
        return ""
    compact = _compact_research_text(text)
    if not compact:
        return ""
    for label in clean_room_task_labels():
        normalized_label = _compact_research_text(label)
        if normalized_label and normalized_label in compact:
            return (
                "Prior-solution research is disabled for this clean-room run; "
                f"refusing {surface} that names the active task label {label!r}"
            )
    return ""


def solution_research_query_block_reason(query: str) -> str:
    """Return a denial reason when a web query names the clean-room task."""
    return solution_research_text_block_reason(query, surface="web query")


def solution_research_url_block_reason(url: str) -> str:
    """Return a denial reason when a URL names the clean-room task."""
    return solution_research_text_block_reason(url, surface="URL")


def is_repository_url(url: str) -> bool:
    """Return whether a URL targets a public source-code repository host."""
    candidate = str(url or "").strip()
    if not candidate:
        return False
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    host = (urlparse(candidate).hostname or "").lower().rstrip(".")
    return host in _REPOSITORY_HOSTS or any(
        host.endswith(suffix) for suffix in _REPOSITORY_HOST_SUFFIXES
    )


def repository_url_block_reason(url: str) -> str:
    """Return a denial reason when the clean-room policy blocks a URL."""
    if repository_research_disabled() and is_repository_url(url):
        return (
            "Repository-backed research is disabled for this clean-room run; "
            f"refusing repository URL {url!r}"
        )
    return ""


def repository_command_block_reason(command: str) -> str:
    """Return a denial reason for Git or repository-network terminal commands."""
    if not repository_research_disabled():
        return ""
    text = str(command or "")
    if _GIT_COMMAND_RE.search(text):
        return "Git commands are disabled for this clean-room run"
    if _REPOSITORY_TRANSITIVE_COMMAND_RE.search(text):
        return "Dependency commands that may invoke Git are disabled for this " "clean-room run"
    lowered = text.lower()
    for host in sorted(_REPOSITORY_HOSTS):
        if host in lowered:
            return (
                "Repository-backed network access is disabled for this clean-room run; "
                f"command references {host}"
            )
    for token in re.findall(r"https?://[^\s'\"<>]+", text, flags=re.IGNORECASE):
        if is_repository_url(token):
            host = urlparse(token).hostname or "a repository host"
            return (
                "Repository-backed network access is disabled for this clean-room run; "
                f"command references {host}"
            )
    return ""


def solution_research_command_block_reason(command: str) -> str:
    """Return a denial reason for network-capable commands naming the task.

    Local project inspection and Lean compilation must remain available even when
    their file paths contain the active task label.
    """
    if not _NETWORK_CAPABLE_COMMAND_RE.search(str(command or "")):
        return ""
    return solution_research_text_block_reason(command, surface="terminal command")


def clean_room_project_root(*, cwd: str | Path | None = None) -> Path:
    """Return the canonical project boundary used by clean-room file tools."""
    configured = str(os.getenv("LEANFLOW_PROJECT_ROOT", "") or "").strip()
    base = Path(configured) if configured else Path(cwd or Path.cwd())
    return base.expanduser().resolve()


def _active_clean_room_file(root: Path) -> Path | None:
    """Return the canonical active file when solution isolation can identify it."""
    active_text = str(os.getenv("LEANFLOW_NATIVE_ACTIVE_FILE", "") or "").strip()
    if not active_text:
        return None
    active = Path(active_text).expanduser()
    if not active.is_absolute():
        active = root / active
    return active.resolve(strict=False)


def _is_sibling_benchmark_file(path: Path, active: Path) -> bool:
    """Return whether ``path`` belongs to another numbered task beside ``active``."""
    if path == active or path.parent != active.parent:
        return False
    match = re.match(r"^(?P<prefix>.*?)(?P<number>\d+)$", active.stem)
    if match is None:
        return False
    sibling = re.match(
        rf"^{re.escape(match.group('prefix'))}(?P<number>\d+)(?:[^0-9].*)?$",
        path.stem,
        flags=re.IGNORECASE,
    )
    return sibling is not None and sibling.group("number") != match.group("number")


def clean_room_path_block_reason(
    path: str | Path,
    *,
    cwd: str | Path | None = None,
) -> str:
    """Return a denial reason for escaped or sibling benchmark solution paths."""
    if not (repository_research_disabled() or solution_research_disabled()):
        return ""
    root = clean_room_project_root(cwd=cwd)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        return f"Clean-room path could not be resolved safely: {exc}"
    if resolved != root and root not in resolved.parents:
        return (
            "Clean-room file access is confined to the active project; "
            f"refusing path {str(path)!r} because it resolves outside {str(root)!r}"
        )
    if not solution_research_disabled() or not resolved.is_file():
        return ""
    active = _active_clean_room_file(root)
    if active is None:
        return ""
    if not _is_sibling_benchmark_file(resolved, active):
        return ""
    return (
        "Clean-room file access cannot read a sibling benchmark task; "
        f"refusing {str(path)!r} while the active task is {active.name!r}"
    )


def clean_room_terminal_path_block_reason(
    path: str | Path,
    *,
    cwd: str | Path | None = None,
) -> str:
    """Return a denial reason for terminal reads that can span sibling tasks.

    File tools filter individual search matches, but a terminal directory scan
    emits raw output.  Deny an active benchmark directory or one of its
    ancestors so a broad ``rg``/``ls`` cannot bypass per-file isolation.
    """
    root = clean_room_project_root(cwd=cwd)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(cwd or root).expanduser() / candidate
    resolved = candidate.resolve(strict=False)
    base_reason = clean_room_path_block_reason(resolved, cwd=cwd)
    if base_reason or not solution_research_disabled() or not resolved.is_dir():
        return base_reason
    active = _active_clean_room_file(root)
    if active is None:
        return ""
    active_parent = active.parent
    if resolved != active_parent and resolved not in active_parent.parents:
        return ""
    try:
        exposes_sibling = any(
            entry.is_file() and _is_sibling_benchmark_file(entry.resolve(strict=False), active)
            for entry in active_parent.iterdir()
        )
    except OSError:
        exposes_sibling = True
    if not exposes_sibling:
        return ""
    return (
        "Clean-room terminal access cannot scan a directory containing sibling "
        f"benchmark tasks; use the active file {active.name!r} or filtered file tools"
    )
