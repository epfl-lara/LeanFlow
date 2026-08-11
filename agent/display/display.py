"""CLI presentation -- spinner, kawaii faces, tool preview formatting.

Pure display functions and classes with no AIAgent dependency.
Used by AIAgent._execute_tool_calls for CLI feedback.
"""

import json
import logging
import os
import sys
import threading
import time

# ANSI escape codes for coloring tool failure indicators
_RED = "\033[31m"
_RESET = "\033[0m"

logger = logging.getLogger(__name__)


# =========================================================================
# Skin-aware helpers (lazy import to avoid circular deps)
# =========================================================================


def _get_skin():
    """Legacy skins were removed from LeanFlow."""
    return None


def get_skin_tool_prefix() -> str:
    """Get tool output prefix character from active skin."""
    skin = _get_skin()
    if skin:
        return skin.tool_prefix
    return "┊"


def get_tool_emoji(tool_name: str, default: str = "⚡") -> str:
    """Get the display emoji for a tool.

    Resolution order:
    1. Active skin's ``tool_emojis`` overrides (if a skin is loaded)
    2. Tool registry's per-tool ``emoji`` field
    3. *default* fallback
    """
    # 1. Skin override
    skin = _get_skin()
    if skin and skin.tool_emojis:
        override = skin.tool_emojis.get(tool_name)
        if override:
            return override
    # 2. Registry default
    try:
        from tools.registry import registry

        emoji = registry.get_emoji(tool_name, default="")
        if emoji:
            return emoji
    except Exception:
        pass
    # 3. Hardcoded fallback
    return default


# =========================================================================
# Tool preview (one-line summary of a tool call's primary argument)
# =========================================================================


def _oneline(text: str) -> str:
    """Collapse whitespace (including newlines) to single spaces."""
    return " ".join(text.split())


def build_tool_preview(tool_name: str, args: dict, max_len: int = 40) -> str | None:
    """Build a short preview of a tool call's primary argument for display."""
    if not args:
        return None
    primary_args = {
        "terminal": "command",
        "web_search": "query",
        "web_extract": "urls",
        "read_file": "path",
        "write_file": "path",
        "patch": "path",
        "search_files": "pattern",
        "skill_view": "name",
        "skills_list": "category",
        "delegate_task": "goal",
        "clarify": "question",
    }

    if tool_name == "process":
        action = args.get("action", "")
        sid = args.get("session_id", "")
        data = args.get("data", "")
        timeout_val = args.get("timeout")
        parts = [action]
        if sid:
            parts.append(sid[:16])
        if data:
            parts.append(f'"{_oneline(data[:20])}"')
        if timeout_val and action == "wait":
            parts.append(f"{timeout_val}s")
        return " ".join(parts) if parts else None

    if tool_name == "todo":
        todos_arg = args.get("todos")
        merge = args.get("merge", False)
        if todos_arg is None:
            return "reading task list"
        elif merge:
            return f"updating {len(todos_arg)} task(s)"
        else:
            return f"planning {len(todos_arg)} task(s)"

    if tool_name == "session_search":
        query = _oneline(args.get("query", ""))
        return f'recall: "{query[:25]}{"..." if len(query) > 25 else ""}"'

    if tool_name == "memory":
        action = args.get("action", "")
        target = args.get("target", "")
        if action == "add":
            content = _oneline(args.get("content", ""))
            return f'+{target}: "{content[:25]}{"..." if len(content) > 25 else ""}"'
        elif action == "replace":
            return f'~{target}: "{_oneline(args.get("old_text", "")[:20])}"'
        elif action == "remove":
            return f'-{target}: "{_oneline(args.get("old_text", "")[:20])}"'
        return action

    key = primary_args.get(tool_name)
    if not key:
        for fallback_key in ("query", "text", "command", "path", "name", "prompt", "code", "goal"):
            if fallback_key in args:
                key = fallback_key
                break

    if not key or key not in args:
        return None

    value = args[key]
    if isinstance(value, list):
        value = value[0] if value else ""

    preview = _oneline(str(value))
    if not preview:
        return None
    if len(preview) > max_len:
        preview = preview[: max_len - 3] + "..."
    return preview


# =========================================================================
# KawaiiSpinner
# =========================================================================


class KawaiiSpinner:
    """Animated spinner with kawaii faces for CLI feedback during tool execution."""

    SPINNERS = {
        "dots": ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"],
        "bounce": ["⠁", "⠂", "⠄", "⡀", "⢀", "⠠", "⠐", "⠈"],
        "grow": ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█", "▇", "▆", "▅", "▄", "▃", "▂"],
        "arrows": ["←", "↖", "↑", "↗", "→", "↘", "↓", "↙"],
        "star": ["✶", "✷", "✸", "✹", "✺", "✹", "✸", "✷"],
        "moon": ["🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘"],
        "pulse": ["◜", "◠", "◝", "◞", "◡", "◟"],
        "brain": ["🧠", "💭", "💡", "✨", "💫", "🌟", "💡", "💭"],
        "sparkle": ["⁺", "˚", "*", "✧", "✦", "✧", "*", "˚"],
    }

    KAWAII_WAITING = [
        "(｡◕‿◕｡)",
        "(◕‿◕✿)",
        "٩(◕‿◕｡)۶",
        "(✿◠‿◠)",
        "( ˘▽˘)っ",
        "♪(´ε` )",
        "(◕ᴗ◕✿)",
        "ヾ(＾∇＾)",
        "(≧◡≦)",
        "(★ω★)",
    ]

    KAWAII_THINKING = [
        "(｡•́︿•̀｡)",
        "(◔_◔)",
        "(¬‿¬)",
        "( •_•)>⌐■-■",
        "(⌐■_■)",
        "(´･_･`)",
        "◉_◉",
        "(°ロ°)",
        "( ˘⌣˘)♡",
        "ヽ(>∀<☆)☆",
        "٩(๑❛ᴗ❛๑)۶",
        "(⊙_⊙)",
        "(¬_¬)",
        "( ͡° ͜ʖ ͡°)",
        "ಠ_ಠ",
    ]

    THINKING_VERBS = [
        "pondering",
        "contemplating",
        "musing",
        "cogitating",
        "ruminating",
        "deliberating",
        "mulling",
        "reflecting",
        "processing",
        "reasoning",
        "analyzing",
        "computing",
        "synthesizing",
        "formulating",
        "brainstorming",
    ]

    def __init__(self, message: str = "", spinner_type: str = "dots"):
        self.message = message
        self.spinner_frames = self.SPINNERS.get(spinner_type, self.SPINNERS["dots"])
        self.running = False
        self.thread = None
        self.frame_idx = 0
        self.start_time = None
        self.last_line_len = 0
        self._last_flush_time = 0.0  # Rate-limit flushes for patch_stdout compat
        # Capture stdout NOW, before any redirect_stdout(devnull) from
        # child agents can replace sys.stdout with a black hole.
        self._out = sys.stdout

    def _write(self, text: str, end: str = "\n", flush: bool = False):
        """Write to the stdout captured at spinner creation time."""
        try:
            self._out.write(text + end)
            if flush:
                self._out.flush()
        except (ValueError, OSError):
            pass

    def _animate(self):
        # Cache skin wings at start (avoid per-frame imports)
        skin = _get_skin()
        wings = skin.get_spinner_wings() if skin else []

        while self.running:
            if os.getenv("LEANFLOW_SPINNER_PAUSE"):
                time.sleep(0.1)
                continue
            frame = self.spinner_frames[self.frame_idx % len(self.spinner_frames)]
            elapsed = time.time() - self.start_time
            if wings:
                left, right = wings[self.frame_idx % len(wings)]
                line = f"  {left} {frame} {self.message} {right} ({elapsed:.1f}s)"
            else:
                line = f"  {frame} {self.message} ({elapsed:.1f}s)"
            pad = max(self.last_line_len - len(line), 0)
            # Rate-limit flush() calls to avoid spinner spam under
            # prompt_toolkit's patch_stdout.  Each flush() pushes a queue
            # item that may trigger a separate run_in_terminal() call; if
            # items are processed one-at-a-time the \r overwrite is lost
            # and every frame appears on its own line.  By flushing at
            # most every 0.4s we guarantee multiple \r-frames are batched
            # into a single write, so the terminal collapses them correctly.
            now = time.time()
            should_flush = (now - self._last_flush_time) >= 0.4
            self._write(f"\r{line}{' ' * pad}", end="", flush=should_flush)
            if should_flush:
                self._last_flush_time = now
            self.last_line_len = len(line)
            self.frame_idx += 1
            time.sleep(0.12)

    def start(self):
        if self.running:
            return
        self.running = True
        self.start_time = time.time()
        self.thread = threading.Thread(target=self._animate, daemon=True)
        self.thread.start()

    def update_text(self, new_message: str):
        self.message = new_message

    def print_above(self, text: str):
        """Print a line above the spinner without disrupting animation.

        Clears the current spinner line, prints the text, and lets the
        next animation tick redraw the spinner on the line below.
        Thread-safe: uses the captured stdout reference (self._out).
        Works inside redirect_stdout(devnull) because _write bypasses
        sys.stdout and writes to the stdout captured at spinner creation.
        """
        if not self.running:
            self._write(f"  {text}", flush=True)
            return
        # Clear spinner line with spaces (not \033[K) to avoid garbled escape
        # codes when prompt_toolkit's patch_stdout is active — same approach
        # as stop(). Then print text; spinner redraws on next tick.
        blanks = " " * max(self.last_line_len + 5, 40)
        self._write(f"\r{blanks}\r  {text}", flush=True)

    def stop(self, final_message: str = None):
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)
        # Clear the spinner line with spaces instead of \033[K to avoid
        # garbled escape codes when prompt_toolkit's patch_stdout is active.
        blanks = " " * max(self.last_line_len + 5, 40)
        self._write(f"\r{blanks}\r", end="", flush=True)
        if final_message:
            self._write(f"  {final_message}", flush=True)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False


# =========================================================================
# Kawaii face arrays (used by AIAgent._execute_tool_calls for spinner text)
# =========================================================================


# =========================================================================
# Cute tool message (completion line that replaces the spinner)
# =========================================================================


def _detect_tool_failure(tool_name: str, result: str | None) -> tuple[bool, str]:
    """Inspect a tool result string for signs of failure.

    Returns ``(is_failure, suffix)`` where *suffix* is an informational tag
    like ``" [exit 1]"`` for terminal failures, or ``" [error]"`` for generic
    failures.  On success, returns ``(False, "")``.
    """
    if result is None:
        return False, ""

    if tool_name == "terminal":
        try:
            data = json.loads(result)
            exit_code = data.get("exit_code")
            if exit_code is not None and exit_code != 0:
                return True, f" [exit {exit_code}]"
        except (json.JSONDecodeError, TypeError, AttributeError):
            logger.debug("Could not parse terminal result as JSON for exit code check")
        return False, ""

    data = None
    try:
        candidate = json.loads(result)
        if isinstance(candidate, dict):
            data = candidate
    except (json.JSONDecodeError, TypeError, AttributeError):
        logger.debug("Could not parse tool result as JSON for structured failure check")

    # Memory-specific: distinguish "full" from real errors.
    if tool_name == "memory" and data is not None:
        if data.get("success") is False and "exceed the limit" in str(data.get("error", "") or ""):
            return True, " [full]"

    if data is not None:
        # Structured tool contracts are authoritative. In particular, a
        # successful result may deliberately include ``"error": null`` or an
        # empty ``failed`` collection for a stable schema; neither is a failure.
        if data.get("success") is False or data.get("ok") is False:
            return True, " [error]"
        if data.get("error") or data.get("failed"):
            return True, " [error]"
        status = str(data.get("status", "") or "").strip().lower()
        if status in {"error", "failed", "failure", "timeout", "denied"} or status.endswith(
            ("_error", "_failed", "_failure", "_timeout", "_denied")
        ):
            return True, " [error]"
        if data.get("success") is True or data.get("ok") is True:
            return False, ""

    # Preserve the text fallback for legacy tools without a structured result.
    lower = result[:500].lower()
    if '"error"' in lower or '"failed"' in lower or result.startswith("Error"):
        return True, " [error]"

    return False, ""


def get_cute_tool_message(
    tool_name: str,
    args: dict,
    duration: float,
    result: str | None = None,
) -> str:
    """Generate a formatted tool completion line for CLI quiet mode.

    Format: ``| {emoji} {verb:9} {detail}  {duration}``

    When *result* is provided the line is checked for failure indicators.
    Failed tool calls get a red prefix and an informational suffix.
    """
    dur = f"{duration:.1f}s"
    is_failure, failure_suffix = _detect_tool_failure(tool_name, result)
    skin_prefix = get_skin_tool_prefix()

    def _trunc(s, n=40):
        s = str(s)
        return (s[: n - 3] + "...") if len(s) > n else s

    def _path(p, n=35):
        p = str(p)
        return ("..." + p[-(n - 3) :]) if len(p) > n else p

    def _wrap(line: str) -> str:
        """Apply skin tool prefix and failure suffix."""
        if skin_prefix != "┊":
            line = line.replace("┊", skin_prefix, 1)
        if not is_failure:
            return line
        return f"{line}{failure_suffix}"

    if tool_name == "web_search":
        return _wrap(f"┊ 🔍 search    {_trunc(args.get('query', ''), 42)}  {dur}")
    if tool_name == "web_extract":
        urls = args.get("urls", [])
        if urls:
            url = urls[0] if isinstance(urls, list) else str(urls)
            domain = url.replace("https://", "").replace("http://", "").split("/")[0]
            extra = f" +{len(urls) - 1}" if len(urls) > 1 else ""
            return _wrap(f"┊ 📄 fetch     {_trunc(domain, 35)}{extra}  {dur}")
        return _wrap(f"┊ 📄 fetch     pages  {dur}")
    if tool_name == "terminal":
        return _wrap(f"┊ 💻 $         {_trunc(args.get('command', ''), 42)}  {dur}")
    if tool_name == "process":
        action = args.get("action", "?")
        sid = args.get("session_id", "")[:12]
        labels = {
            "list": "ls processes",
            "poll": f"poll {sid}",
            "log": f"log {sid}",
            "wait": f"wait {sid}",
            "kill": f"kill {sid}",
            "write": f"write {sid}",
            "submit": f"submit {sid}",
        }
        return _wrap(f"┊ ⚙️  proc      {labels.get(action, f'{action} {sid}')}  {dur}")
    if tool_name == "read_file":
        return _wrap(f"┊ 📖 read      {_path(args.get('path', ''))}  {dur}")
    if tool_name == "write_file":
        return _wrap(f"┊ ✍️  write     {_path(args.get('path', ''))}  {dur}")
    if tool_name == "patch":
        return _wrap(f"┊ 🔧 patch     {_path(args.get('path', ''))}  {dur}")
    if tool_name == "search_files":
        pattern = _trunc(args.get("pattern", ""), 35)
        target = args.get("target", "content")
        verb = "find" if target == "files" else "grep"
        return _wrap(f"┊ 🔎 {verb:9} {pattern}  {dur}")
    if tool_name == "todo":
        todos_arg = args.get("todos")
        merge = args.get("merge", False)
        if todos_arg is None:
            return _wrap(f"┊ 📋 plan      reading tasks  {dur}")
        elif merge:
            return _wrap(f"┊ 📋 plan      update {len(todos_arg)} task(s)  {dur}")
        else:
            return _wrap(f"┊ 📋 plan      {len(todos_arg)} task(s)  {dur}")
    if tool_name == "session_search":
        return _wrap(f'┊ 🔍 recall    "{_trunc(args.get("query", ""), 35)}"  {dur}')
    if tool_name == "memory":
        action = args.get("action", "?")
        target = args.get("target", "")
        if action == "add":
            return _wrap(
                f'┊ 🧠 memory    +{target}: "{_trunc(args.get("content", ""), 30)}"  {dur}'
            )
        elif action == "replace":
            return _wrap(
                f'┊ 🧠 memory    ~{target}: "{_trunc(args.get("old_text", ""), 20)}"  {dur}'
            )
        elif action == "remove":
            return _wrap(
                f'┊ 🧠 memory    -{target}: "{_trunc(args.get("old_text", ""), 20)}"  {dur}'
            )
        return _wrap(f"┊ 🧠 memory    {action}  {dur}")
    if tool_name == "skills_list":
        return _wrap(f"┊ 📚 skills    list {args.get('category', 'all')}  {dur}")
    if tool_name == "skill_view":
        return _wrap(f"┊ 📚 skill     {_trunc(args.get('name', ''), 30)}  {dur}")
    if tool_name == "delegate_task":
        tasks = args.get("tasks")
        if tasks and isinstance(tasks, list):
            return _wrap(f"┊ 🔀 delegate  {len(tasks)} parallel tasks  {dur}")
        return _wrap(f"┊ 🔀 delegate  {_trunc(args.get('goal', ''), 35)}  {dur}")

    preview = build_tool_preview(tool_name, args) or ""
    return _wrap(f"┊ ⚡ {tool_name[:9]:9} {_trunc(preview, 35)}  {dur}")
