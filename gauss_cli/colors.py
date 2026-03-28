"""Shared terminal rendering utilities for Gauss CLI modules."""

from __future__ import annotations

import locale
import os
import re
import sys
import unicodedata
from functools import lru_cache
from typing import TextIO


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"


_TRUE_VALUES = {"1", "true", "yes", "on"}
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ASCII_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("⚠️", "!"),
    ("⚠", "!"),
    ("✓", "OK"),
    ("✔", "OK"),
    ("✗", "X"),
    ("✘", "X"),
    ("⏳", "..."),
    ("⏱", "T"),
    ("⏭", ">>"),
    ("✨", "*"),
    ("⚡", "*"),
    ("⚕", "+"),
    ("◆", "*"),
    ("•", "*"),
    ("·", "|"),
    ("—", "-"),
    ("–", "-"),
    ("…", "..."),
    ("→", "->"),
    ("←", "<-"),
    ("↔", "<->"),
    ("╭", "+"),
    ("╮", "+"),
    ("╰", "+"),
    ("╯", "+"),
    ("┌", "+"),
    ("┐", "+"),
    ("└", "+"),
    ("┘", "+"),
    ("├", "+"),
    ("┤", "+"),
    ("┬", "+"),
    ("┴", "+"),
    ("┼", "+"),
    ("│", "|"),
    ("║", "|"),
    ("─", "-"),
    ("═", "="),
    ("█", "#"),
    ("∑", "S"),
)
_UNICODE_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_ASCII_SPINNER_FRAMES = ("|", "/", "-", "\\")


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUE_VALUES


def _stdout_stream(stream: TextIO | None = None) -> TextIO:
    return stream or sys.stdout


@lru_cache(maxsize=1)
def _enable_windows_vt_mode() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        if handle in (0, -1):
            return False
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
            return False
        enable_vt = 0x0004
        if mode.value & enable_vt:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | enable_vt))
    except Exception:
        return False


def supports_ansi(stream: TextIO | None = None) -> bool:
    """Return True when the active terminal should receive ANSI color codes."""
    if _truthy_env("GAUSS_FORCE_PLAIN_OUTPUT") or os.getenv("NO_COLOR") is not None:
        return False

    output = _stdout_stream(stream)
    try:
        if not output.isatty():
            return False
    except Exception:
        return False

    term = os.getenv("TERM", "").strip().lower()
    if term == "dumb":
        return False

    if os.name != "nt":
        return True

    if _enable_windows_vt_mode():
        return True

    return any(
        os.getenv(name)
        for name in ("WT_SESSION", "ANSICON", "ConEmuANSI", "TERM_PROGRAM")
    )


def supports_unicode(stream: TextIO | None = None) -> bool:
    """Return True when the active terminal can render UTF-8 cleanly."""
    if _truthy_env("GAUSS_FORCE_ASCII"):
        return False

    output = _stdout_stream(stream)
    encoding = getattr(output, "encoding", None) or locale.getpreferredencoding(False) or ""
    normalized = encoding.strip().lower()
    if "utf" in normalized or "65001" in normalized:
        return True

    if os.name == "nt":
        return bool(os.getenv("WT_SESSION"))

    return False


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from *text*."""
    return _ANSI_RE.sub("", text or "")


def render_terminal_text(
    text: str,
    *,
    allow_ansi: bool | None = None,
    allow_unicode: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """Normalize text for the current terminal's color and encoding support."""
    rendered = text or ""

    if allow_ansi is None:
        allow_ansi = supports_ansi(stream)
    if not allow_ansi:
        rendered = strip_ansi(rendered)

    if allow_unicode is None:
        allow_unicode = supports_unicode(stream)
    if allow_unicode:
        return rendered

    for source, target in _ASCII_REPLACEMENTS:
        rendered = rendered.replace(source, target)
    return (
        unicodedata.normalize("NFKD", rendered)
        .encode("ascii", "ignore")
        .decode("ascii")
    )


def terminal_symbol(unicode_symbol: str, ascii_symbol: str, *, stream: TextIO | None = None) -> str:
    """Pick a Unicode or ASCII symbol for the current terminal."""
    return unicode_symbol if supports_unicode(stream) else ascii_symbol


def spinner_frames(stream: TextIO | None = None) -> tuple[str, ...]:
    """Return spinner frames that won't render as mojibake on plain terminals."""
    return _UNICODE_SPINNER_FRAMES if supports_unicode(stream) else _ASCII_SPINNER_FRAMES


def color(text: str, *codes) -> str:
    """Apply color codes to text (only when output is a TTY)."""
    if supports_unicode():
        rendered = text
    else:
        rendered = render_terminal_text(text, allow_ansi=True, allow_unicode=False)

    if not supports_ansi():
        return render_terminal_text(rendered, allow_ansi=False, allow_unicode=supports_unicode())

    return "".join(codes) + rendered + Colors.RESET
