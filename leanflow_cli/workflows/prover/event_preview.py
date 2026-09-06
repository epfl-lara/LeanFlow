"""Pure previews that make compact prover activity events readable in editors.

Full transcripts stay in each job's private ``events.jsonl``. The shared activity
stream carries only what a log row and its expanded view need immediately: a
one-line message that says what happened, bounded text previews of the model
output, and the evidence id that resolves the complete record on demand through
``leanflow runs event``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from agent.accounting.redact import redact_sensitive_text

MESSAGE_CHARS = 200
PREVIEW_CHARS = 600
ARGUMENT_CHARS = 300
MAX_TOOL_CALLS = 4

#: Argument names that identify *what* a call acted on, ordered by how well
#: each names the call's subject. A row shows the verb plus one salient
#: argument rather than the whole payload.
_PATH_KEYS = ("path", "file", "file_path", "filename", "scratch_file")
_SUBJECT_KEYS = (
    "query",
    "question",
    "search",
    "pattern",
    "url",
    "command",
    "declaration",
    "theorem",
    "target",
    "name",
    "node_id",
    "topic",
)
#: Payload names that carry a document rather than an identifier. These never
#: belong on a one-line row.
_BULK_KEYS = frozenset(
    {
        "body",
        "code",
        "content",
        "diff",
        "new",
        "notes",
        "old",
        "patch",
        "plan",
        "program",
        "proof",
        "proofs",
        "replacement",
        "report",
        "script",
        "snippet",
        "source",
        "text",
    }
)
SUBJECT_CHARS = 60
#: Recovers a subject from arguments that are not valid JSON, such as a
#: response truncated mid-object.
_TRUNCATED_SUBJECT_RE = re.compile(
    r'"(path|file|file_path|filename|query|question|command|name)"\s*:\s*"([^"\\]{1,120})"'
)

_CHECK_KINDS = {
    "submission-feedback",
    "submission_checked",
    "candidate_checked",
    "negation_checked",
}
_CHECK_LABELS = {
    "submission-feedback": "Candidate",
    "submission_checked": "Submission",
    "candidate_checked": "Candidate",
    "negation_checked": "Negation",
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _clip(value: Any, limit: int) -> str:
    text = _text(value).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _line(value: Any, limit: int) -> str:
    return _clip(" ".join(_text(value).split()), limit)


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value


def _short_path(value: str) -> str:
    """Return the file name that identifies a path, without its directories."""
    parts = [part for part in value.replace("\\", "/").split("/") if part]
    return parts[-1] if parts else value


def salient_argument(arguments: Any) -> str:
    """Return the one argument that says what a call acted on, or an empty string.

    Tool arguments are frequently a whole file, proof, or program. Rendering
    them on a log row buries the call; naming its subject does not.
    """
    args = arguments
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            # A provider can stream arguments that are cut off mid-object. The
            # subject is usually the first key, so recover it rather than
            # showing the row nothing at all.
            named = _TRUNCATED_SUBJECT_RE.search(args)
            if named:
                return _short_path(named.group(2))
            collapsed = " ".join(args.split())
            return _clip(collapsed, SUBJECT_CHARS) if len(collapsed) <= 80 else ""
    if not isinstance(args, Mapping):
        return ""
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return _short_path(value.strip())
    for key in _SUBJECT_KEYS:
        value = args.get(key)
        if isinstance(value, str | int | float) and not isinstance(value, bool):
            collapsed = " ".join(str(value).split())
            if collapsed:
                return _clip(collapsed, SUBJECT_CHARS)
    for key, value in args.items():
        # A one-word tactic or snippet names the call; a whole file does not.
        if key in _BULK_KEYS and isinstance(value, str) and "\n" not in value:
            collapsed = value.strip()
            if collapsed and len(collapsed) <= 40:
                return collapsed
    for key, value in args.items():
        if key in _BULK_KEYS or isinstance(value, Mapping | list):
            continue
        collapsed = " ".join(str(value).split())
        if collapsed and len(collapsed) <= 40:
            return f"{key}={collapsed}"
    return ""


def _tool_call_summaries(assistant: Mapping[str, Any]) -> tuple[list[dict[str, str]], int]:
    """Summarize requested tool calls in both provider and legacy shapes."""
    calls = assistant.get("tool_calls")
    if not isinstance(calls, list):
        return [], 0
    summaries: list[dict[str, str]] = []
    for call in calls[:MAX_TOOL_CALLS]:
        if not isinstance(call, Mapping):
            continue
        function = call.get("function")
        function = function if isinstance(function, Mapping) else call
        summaries.append(
            {
                "name": str(function.get("name") or ""),
                "subject": salient_argument(function.get("arguments")),
                "arguments_preview": _line(function.get("arguments"), ARGUMENT_CHARS),
            }
        )
    return summaries, len(calls)


def preview_details(kind: str, details: Mapping[str, Any]) -> dict[str, Any]:
    """Return bounded, redacted preview fields for one prover event."""
    preview: dict[str, Any] = {}
    if kind == "api-response":
        assistant = details.get("assistant")
        assistant = assistant if isinstance(assistant, Mapping) else {}
        content = _text(assistant.get("content"))
        reasoning = _text(assistant.get("reasoning") or assistant.get("reasoning_content"))
        calls, call_count = _tool_call_summaries(assistant)
        preview.update(
            content_preview=_clip(content, PREVIEW_CHARS),
            content_chars=len(content),
            reasoning_preview=_clip(reasoning, PREVIEW_CHARS),
            reasoning_chars=len(reasoning),
            tool_calls=calls,
            tool_call_count=call_count,
            finish_reason=str(assistant.get("finish_reason") or ""),
        )
    elif kind == "tool-result":
        result = details.get("result")
        result_text = _text(result)
        preview.update(
            subject=salient_argument(details.get("arguments")),
            arguments_preview=_clip(details.get("arguments"), ARGUMENT_CHARS),
            result_preview=_clip(result_text, PREVIEW_CHARS),
            result_chars=len(result_text),
        )
        if isinstance(result, Mapping):
            if isinstance(result.get("success"), bool):
                preview["success"] = result["success"]
            error = _text(result.get("error"))
            if error:
                preview["error"] = _clip(error, PREVIEW_CHARS)
    elif kind == "job-session-end":
        final = _text(details.get("final_response"))
        preview.update(
            final_response_preview=_clip(final, PREVIEW_CHARS), final_response_chars=len(final)
        )
    elif kind in _CHECK_KINDS:
        feedback = details.get("feedback")
        if feedback is None:
            feedback = details.get("result")
        if feedback is not None:
            preview["feedback_preview"] = _clip(feedback, PREVIEW_CHARS)
        if isinstance(details.get("conditional"), bool):
            preview["conditional"] = details["conditional"]
    elif kind in {"plan_rejected", "plan_refinement_budget_exhausted"}:
        preview["reason_preview"] = _clip(details.get("reason"), PREVIEW_CHARS)
    elif kind == "user_message":
        preview["message_preview"] = _clip(details.get("message"), PREVIEW_CHARS)
    elif kind == "context-compacted":
        preview["method"] = str(details.get("method") or "")
    return {
        key: _redact(value)
        for key, value in preview.items()
        if value not in ("", None) and value != []
    }


def _call_text(call: Mapping[str, str]) -> str:
    return f"{call['name']}({call.get('subject', '')})"


def _tool_call_message(calls: list[dict[str, str]], total: int) -> str:
    """Name the requested calls compactly: subjects when few, counts when many."""
    named = [call for call in calls if call.get("name")]
    if not named:
        return ""
    if len(named) == total and len(named) <= 2:
        rendered = ", ".join(_call_text(call) for call in named)
        if len(rendered) <= 90:
            return rendered
    counts: dict[str, int] = {}
    for call in named:
        counts[call["name"]] = counts.get(call["name"], 0) + 1
    parts = [name if count == 1 else f"{name} ×{count}" for name, count in counts.items()]
    return ", ".join(parts) + ("…" if total > len(named) else "")


def event_message(kind: str, details: Mapping[str, Any], preview: Mapping[str, Any]) -> str:
    """Return a one-line message that says what happened, not only which kind."""
    job = str(details.get("job_id") or "")
    calls = details.get("api_calls")
    budget = details.get("api_budget")
    counted = isinstance(calls, int) and not isinstance(calls, bool)
    budgeted = isinstance(budget, int) and not isinstance(budget, bool)
    if kind in {"operation-start", "operation-end"}:
        label = str(details.get("label") or "Controller check")
        file = str(details.get("file") or "")
        outcome = str(details.get("status") or "running")
        error = str(details.get("error") or "")
        return _line(
            f"{label}: {outcome}"
            + (f" · {file}" if file else "")
            + (f" · {error}" if error else ""),
            MESSAGE_CHARS,
        )
    if kind == "api-request":
        model = str(details.get("model") or "")
        text = f"Request {calls}/{budget}" if counted and budgeted else "Request"
        if details.get("final_report_only"):
            text += " (final report, tools disabled)"
        return f"{text} · {model}" if model else text
    if kind == "api-response":
        head = str(preview.get("content_preview") or "")
        tools = _tool_call_message(
            list(preview.get("tool_calls") or []), int(preview.get("tool_call_count") or 0)
        )
        if head and tools:
            # Keep the requested tools visible even when the text runs long.
            room = max(60, MESSAGE_CHARS - len(tools) - 3)
            return _line(f"{_line(head, room)} · {tools}", MESSAGE_CHARS)
        if head:
            return _line(head, MESSAGE_CHARS)
        if tools:
            return _line(tools, MESSAGE_CHARS)
        reasoning = str(preview.get("reasoning_preview") or "")
        if reasoning:
            return _line(f"Reasoning: {reasoning}", MESSAGE_CHARS)
        return "Empty response"
    if kind == "tool-result":
        tool = str(details.get("tool") or "tool")
        subject = str(preview.get("subject") or "")
        success = preview.get("success")
        outcome = ""
        if success is False:
            error = str(preview.get("error") or "")
            outcome = f" → failed: {error}" if error else " → failed"
        elif success is True:
            outcome = " → ok"
        return _line(f"{tool}({subject}){outcome}", MESSAGE_CHARS)
    if kind == "job-session-start":
        role = str(details.get("role") or "job")
        return f"{role} session started" + (f" · budget {budget} calls" if budgeted else "")
    if kind == "job-session-end":
        status = str(details.get("status") or "ended")
        head = str(preview.get("final_response_preview") or "")
        base = f"Session {status}" + (f" after {calls} calls" if counted else "")
        return _line(f"{base} · {head}" if head else base, MESSAGE_CHARS)
    if kind == "job_finished":
        status = str(details.get("status") or "finished")
        return f"{job or 'job'} {status}" + (f" · {calls} calls" if counted else "")
    if kind == "api-error":
        return _line(f"Provider error: {details.get('error', '')}", MESSAGE_CHARS)
    if kind == "final-report-rejected":
        return _line(f"Final report rejected: {details.get('error', '')}", MESSAGE_CHARS)
    if kind == "context-compacted":
        return "Context compacted" + (f" at call {calls}" if counted else "")
    if kind == "submission-feedback":
        if details.get("accepted") is True:
            suffix = (
                " (conditional on unfinished dependencies)" if details.get("conditional") else ""
            )
            return "Candidate accepted by the independent check" + suffix
        reason = details.get("error") or preview.get("feedback_preview") or ""
        return _line(
            f"Candidate rejected: {reason}" if reason else "Candidate rejected", MESSAGE_CHARS
        )
    if kind in _CHECK_KINDS:
        node = str(details.get("node_id") or "")
        accepted = details.get("accepted")
        verdict = (
            "accepted" if accepted is True else ("rejected" if accepted is False else "checked")
        )
        text = f"{_CHECK_LABELS[kind]} {verdict}" + (f" for {node}" if node else "")
        return text + (" (conditional)" if details.get("conditional") else "")
    if kind == "plan_rejected":
        return _line(f"Plan rejected: {preview.get('reason_preview', '')}", MESSAGE_CHARS)
    if kind == "plan_refinement_budget_exhausted":
        return _line(
            f"Plan refinement budget exhausted: {preview.get('reason_preview', '')}", MESSAGE_CHARS
        )
    if kind == "user-guidance-received":
        return "User guidance delivered to the job"
    if kind == "user_message":
        return _line(f"User message: {preview.get('message_preview', '')}", MESSAGE_CHARS)
    if kind == "libraries_installed":
        return (
            "Planned libraries installed"
            if details.get("accepted") is True
            else "Planned library installation failed"
        )
    fallback = details.get("message") or details.get("error") or details.get("tool")
    return _line(fallback or kind, MESSAGE_CHARS)
