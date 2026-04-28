"""Incremental Lean verification backed by LeanInteract.

This module is intentionally scoped to same-file, ordered queue workflows.  It
warms the file header/imports once, keeps a Lean REPL server alive, and checks
declaration chunks against cached environment checkpoints.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from epflemma_cli.project import find_lean_project_root


DECLARATION_PATTERN = re.compile(
    r"^[ \t]*(?:@[A-Za-z0-9_.'-]+(?:[ \t]+[A-Za-z0-9_.'-]+)*[ \t]+)*"
    r"(theorem|lemma|example|def|instance)\s+([A-Za-z0-9_'.-]+)?",
    re.MULTILINE,
)
LOCAL_REPL_CANDIDATES = (
    ".lake/packages/repl",
    ".lake/build",
)
NOISY_MESSAGE_PREFIXES = (
    "note: this linter can be disabled with",
)


@dataclass(frozen=True)
class LeanIncrementalSegment:
    index: int
    kind: str
    name: str
    start: int
    end: int
    declaration_start: int
    start_line: int
    end_line: int
    text: str
    text_hash: str


@dataclass
class _Checkpoint:
    before_env: int | None
    after_env: int | None
    text_hash: str


@dataclass
class _IncrementalSession:
    project_root: Path
    file_path: Path
    repl_dir: Path
    server: Any
    config: Any
    header_hash: str = ""
    header_env: int | None = None
    checkpoints: dict[int, _Checkpoint] = field(default_factory=dict)
    segment_names: dict[int, str] = field(default_factory=dict)

    def close(self) -> None:
        try:
            self.server.kill()
        except Exception:
            pass


_SESSIONS: dict[tuple[str, str], _IncrementalSession] = {}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lean_code_mask(text: str) -> list[bool]:
    """Return a per-character mask for positions outside Lean comments/strings."""
    mask = [True] * len(text)
    i = 0
    block_depth = 0
    in_string = False
    in_line_comment = False
    while i < len(text):
        if in_line_comment:
            if text[i] == "\n":
                in_line_comment = False
            else:
                mask[i] = False
            i += 1
            continue
        if block_depth:
            mask[i] = False
            if text.startswith("/-", i):
                if i + 1 < len(mask):
                    mask[i + 1] = False
                block_depth += 1
                i += 2
                continue
            if text.startswith("-/", i):
                if i + 1 < len(mask):
                    mask[i + 1] = False
                block_depth -= 1
                i += 2
                continue
            i += 1
            continue
        if in_string:
            mask[i] = False
            if text[i] == "\\":
                if i + 1 < len(mask):
                    mask[i + 1] = False
                i += 2
                continue
            if text[i] == '"':
                in_string = False
            i += 1
            continue
        if text.startswith("--", i):
            mask[i] = False
            if i + 1 < len(mask):
                mask[i + 1] = False
            in_line_comment = True
            i += 2
            continue
        if text.startswith("/-", i):
            mask[i] = False
            if i + 1 < len(mask):
                mask[i + 1] = False
            block_depth = 1
            i += 2
            continue
        if text[i] == '"':
            mask[i] = False
            in_string = True
            i += 1
            continue
        i += 1
    return mask


def _import_lean_interact() -> tuple[Any, Any, Any, Any, str]:
    try:
        from lean_interact import Command, LeanREPLConfig, LeanServer, LocalProject
    except Exception as exc:
        return None, None, None, None, f"lean-interact unavailable: {exc}"
    return Command, LeanREPLConfig, LeanServer, LocalProject, ""


def _resolve_project_root(cwd: str | Path | None, file_path: str | Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if cwd:
        candidates.append(Path(cwd).expanduser().resolve())
    if file_path:
        path = Path(file_path).expanduser()
        candidates.append((path if path.is_dir() else path.parent).resolve())
    candidates.append(Path.cwd().resolve())
    for candidate in candidates:
        root = find_lean_project_root(candidate)
        if root is not None:
            return root.resolve()
    return None


def _resolve_file_path(file_path: str | Path, project_root: Path | None) -> Path:
    raw = Path(str(file_path or "")).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    if project_root is not None:
        return (project_root / raw).resolve()
    return raw.resolve()


def _local_repl_dir(project_root: Path) -> Path | None:
    suffix = ".exe" if __import__("platform").system() == "Windows" else ""
    for candidate in LOCAL_REPL_CANDIDATES:
        root = project_root / candidate
        binary = root / ".lake" / "build" / "bin" / f"repl{suffix}"
        if binary.is_file():
            return root
    return None


def _doc_boundary_start(text: str, declaration_start: int) -> int:
    cursor = declaration_start
    while cursor > 0 and text[cursor - 1] in " \t\r\n":
        cursor -= 1
    if cursor >= 2 and text[:cursor].endswith("-/"):
        start = text.rfind("/-", 0, cursor)
        if start >= 0 and text.startswith("/--", start) and text[cursor:declaration_start].strip() == "":
            return text.rfind("\n", 0, start) + 1
    return declaration_start


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


def _segment_file(text: str) -> tuple[str, list[LeanIncrementalSegment]]:
    code_mask = _lean_code_mask(text)
    matches = [match for match in DECLARATION_PATTERN.finditer(text) if code_mask[match.start()]]
    if not matches:
        return text, []

    boundaries = [_doc_boundary_start(text, match.start()) for match in matches]
    header = text[: boundaries[0]].rstrip() + "\n"
    segments: list[LeanIncrementalSegment] = []
    for index, match in enumerate(matches):
        start = boundaries[index]
        end = boundaries[index + 1] if index + 1 < len(boundaries) else len(text)
        chunk = text[start:end].rstrip() + "\n"
        kind = str(match.group(1) or "")
        name = str(match.group(2) or "").strip()
        segments.append(
            LeanIncrementalSegment(
                index=index,
                kind=kind,
                name=name,
                start=start,
                end=end,
                declaration_start=match.start(),
                start_line=_line_number(text, start),
                end_line=_line_number(text, max(start, end - 1)),
                text=chunk,
                text_hash=_sha(chunk),
            )
        )
    return header, segments


def _find_segment(segments: list[LeanIncrementalSegment], theorem_id: str) -> LeanIncrementalSegment | None:
    wanted = str(theorem_id or "").strip()
    if not wanted:
        return None
    short = wanted.split(".")[-1]
    for segment in segments:
        if segment.name in {wanted, short}:
            return segment
    return None


def _session_key(project_root: Path, file_path: Path) -> tuple[str, str]:
    return str(project_root.resolve()), str(file_path.resolve())


def close_incremental_sessions() -> None:
    for session in list(_SESSIONS.values()):
        session.close()
    _SESSIONS.clear()


def lean_incremental_capabilities(cwd: str | Path | None = None) -> dict[str, Any]:
    _, _, _, _, import_error = _import_lean_interact()
    project_root = _resolve_project_root(cwd)
    repl_dir = _local_repl_dir(project_root) if project_root else None
    available = not import_error and bool(project_root) and bool(repl_dir)
    degraded: list[str] = []
    if import_error:
        degraded.append(import_error)
    if project_root is None:
        degraded.append("Lean project root not detected")
    elif repl_dir is None:
        degraded.append("project-local Lean REPL binary not found; run `epflemma project init`")
    active_sessions = [
        {"project_root": project, "file": file}
        for project, file in _SESSIONS.keys()
        if project_root is None or project == str(project_root)
    ]
    return {
        "available": available,
        "project_root": str(project_root or ""),
        "repl_dir": str(repl_dir or ""),
        "active_sessions": active_sessions,
        "degraded_reasons": degraded,
    }


def _new_session(project_root: Path, file_path: Path, repl_dir: Path) -> tuple[_IncrementalSession | None, str]:
    Command, LeanREPLConfig, LeanServer, LocalProject, import_error = _import_lean_interact()
    del Command
    if LeanREPLConfig is None or LeanServer is None or LocalProject is None:
        return None, import_error
    try:
        project = LocalProject(directory=str(project_root), auto_build=False)
        config = LeanREPLConfig(project=project, local_repl_path=str(repl_dir), build_repl=False, verbose=False)
        server = LeanServer(config)
    except Exception as exc:
        return None, f"failed to start LeanInteract server: {exc}"
    return _IncrementalSession(project_root=project_root, file_path=file_path, repl_dir=repl_dir, server=server, config=config), ""


def _get_session(project_root: Path, file_path: Path) -> tuple[_IncrementalSession | None, str]:
    repl_dir = _local_repl_dir(project_root)
    if repl_dir is None:
        return None, "project-local Lean REPL binary not found; run `epflemma project init`"
    key = _session_key(project_root, file_path)
    existing = _SESSIONS.get(key)
    if existing and existing.repl_dir == repl_dir:
        return existing, ""
    if existing:
        existing.close()
    session, error = _new_session(project_root, file_path, repl_dir)
    if session is not None:
        _SESSIONS[key] = session
    return session, error


def _run_command(
    session: _IncrementalSession,
    cmd: str,
    *,
    env: int | None,
    include_tactics: bool,
    timeout_s: int,
    retry_dead_server: bool = True,
) -> tuple[Any | None, float, str]:
    Command, _, _, _, import_error = _import_lean_interact()
    if Command is None:
        return None, 0.0, import_error
    start = time.perf_counter()
    try:
        request = Command(cmd=cmd, all_tactics=True) if include_tactics else Command(cmd=cmd)
        if env is not None:
            request = request.model_copy(update={"env": env})
        response = session.server.run(request, timeout=timeout_s)
    except Exception as exc:
        elapsed = time.perf_counter() - start
        error = str(exc)
        dead_server = any(
            token in error.lower()
            for token in (
                "lean server is not running",
                "server is not running",
                "broken pipe",
                "connection reset",
                "process has exited",
            )
        )
        if retry_dead_server and dead_server:
            restarted, restart_error = _restart_session(session)
            if restarted is None:
                return None, elapsed, restart_error or error
            session.__dict__.update(restarted.__dict__)
            response, retry_elapsed, retry_error = _run_command(
                session,
                cmd,
                env=env,
                include_tactics=include_tactics,
                timeout_s=timeout_s,
                retry_dead_server=False,
            )
            return response, elapsed + retry_elapsed, retry_error
        return None, time.perf_counter() - start, str(exc)
    return response, time.perf_counter() - start, ""


def _pos_to_dict(pos: Any | None, *, line_offset: int = 0) -> dict[str, int] | None:
    if pos is None:
        return None
    line = int(getattr(pos, "line", 0) or 0)
    column = int(getattr(pos, "column", 0) or 0)
    return {"line": line + line_offset, "column": column}


def _clean_message_text(text: str) -> str:
    lines = []
    for line in str(text or "").splitlines():
        if any(line.strip().startswith(prefix) for prefix in NOISY_MESSAGE_PREFIXES):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _message_payloads(response: Any, *, line_offset: int = 0, limit: int = 12) -> list[dict[str, Any]]:
    payloads = []
    for message in list(getattr(response, "messages", []) or [])[:limit]:
        text = _clean_message_text(str(getattr(message, "data", "") or ""))
        payloads.append(
            {
                "severity": str(getattr(message, "severity", "") or ""),
                "message": text,
                "start": _pos_to_dict(getattr(message, "start_pos", None), line_offset=0),
                "end": _pos_to_dict(getattr(message, "end_pos", None), line_offset=0),
                "file_start": _pos_to_dict(getattr(message, "start_pos", None), line_offset=line_offset),
                "file_end": _pos_to_dict(getattr(message, "end_pos", None), line_offset=line_offset),
            }
        )
    return payloads


def _format_message_summary(messages: list[dict[str, Any]], *, limit: int = 3) -> str:
    parts: list[str] = []
    for item in messages[:limit]:
        pos = item.get("file_start") if isinstance(item.get("file_start"), Mapping) else item.get("start")
        location = ""
        if isinstance(pos, Mapping):
            line = pos.get("line")
            column = pos.get("column")
            if line:
                location = f"line {line}"
                if column is not None:
                    location += f":{column}"
        severity = str(item.get("severity", "") or "").strip()
        message = " ".join(str(item.get("message", "") or "").split())
        prefix = f"{location} " if location else ""
        if severity:
            prefix += f"{severity}: "
        if message:
            parts.append((prefix + message)[:240])
    return "; ".join(parts)


def _tactic_payloads(response: Any, *, line_offset: int = 0, limit: int = 20) -> list[dict[str, Any]]:
    payloads = []
    for tactic in list(getattr(response, "tactics", []) or [])[:limit]:
        payloads.append(
            {
                "tactic": str(getattr(tactic, "tactic", "") or ""),
                "goals": str(getattr(tactic, "goals", "") or ""),
                "proof_state": getattr(tactic, "proof_state", None),
                "start": _pos_to_dict(getattr(tactic, "start_pos", None), line_offset=0),
                "end": _pos_to_dict(getattr(tactic, "end_pos", None), line_offset=0),
                "file_start": _pos_to_dict(getattr(tactic, "start_pos", None), line_offset=line_offset),
                "file_end": _pos_to_dict(getattr(tactic, "end_pos", None), line_offset=line_offset),
                "used_constants": list(getattr(tactic, "used_constants", []) or []),
            }
        )
    return payloads


def _has_sorry(response: Any) -> bool:
    if list(getattr(response, "sorries", []) or []):
        return True
    for message in list(getattr(response, "messages", []) or []):
        if str(getattr(message, "data", "") or "") in {"declaration uses 'sorry'", "declaration uses `sorry`"}:
            return True
    return False


def _feedback_lean(text: str, messages: list[dict[str, Any]], tactics: list[dict[str, Any]], *, limit: int = 18) -> str:
    by_line: dict[int, list[str]] = {}
    for message in messages:
        pos = message.get("start") if isinstance(message.get("start"), Mapping) else None
        line = int(pos.get("line", 1) if isinstance(pos, Mapping) else 1)
        entry = f"-- type: {message.get('severity', '')}, msg: {str(message.get('message', '') or '').replace(chr(10), ' ')}"
        by_line.setdefault(max(1, line), []).append(entry[:240])
    for tactic in tactics[:limit]:
        pos = tactic.get("start") if isinstance(tactic.get("start"), Mapping) else None
        line = int(pos.get("line", 1) if isinstance(pos, Mapping) else 1)
        goals = str(tactic.get("goals", "") or "").strip()
        if goals:
            by_line.setdefault(max(1, line), []).append("-- proof state: " + goals.replace("\n", " ")[:300])

    output: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line_number in by_line:
            indent = line[: len(line) - len(line.lstrip())]
            output.append(f"{indent}/- <feedback>")
            output.extend(f"{indent}{entry}" for entry in by_line[line_number][:4])
            output.append(f"{indent}</feedback> -/")
        output.append(line)
    return "\n".join(output)


def _response_payload(
    response: Any,
    *,
    backend: str,
    action: str,
    file_path: Path,
    target: LeanIncrementalSegment | None,
    elapsed_s: float,
    env_before: int | None,
    env_after: int | None,
    cache_hit: bool,
    include_tactics: bool,
    checked_text: str,
    timed_out: bool = False,
    error: str = "",
) -> dict[str, Any]:
    line_offset = (int(target.start_line) - 1) if target is not None else 0
    messages = _message_payloads(response, line_offset=line_offset) if response is not None else []
    tactics = _tactic_payloads(response, line_offset=line_offset) if response is not None and include_tactics else []
    has_errors = bool(response.has_errors()) if response is not None and hasattr(response, "has_errors") else bool(error)
    has_sorry = _has_sorry(response) if response is not None else False
    valid_without_sorry = (
        bool(response.lean_code_is_valid(allow_sorry=False))
        if response is not None and hasattr(response, "lean_code_is_valid")
        else False
    )
    output = "\n".join(
        f"{item.get('severity', '')}: {item.get('message', '')}".strip()
        for item in messages
        if str(item.get("message", "") or "").strip()
    )
    return {
        "success": not bool(error),
        "ok": valid_without_sorry and not has_errors and not has_sorry,
        "backend": backend,
        "action": action,
        "file": str(file_path),
        "target": target.name if target else "",
        "target_kind": target.kind if target else "",
        "target_range": {"start_line": target.start_line, "end_line": target.end_line} if target else {},
        "valid_without_sorry": valid_without_sorry,
        "has_errors": has_errors,
        "has_sorry": has_sorry,
        "timed_out": timed_out,
        "error": error,
        "elapsed_s": round(float(elapsed_s), 3),
        "command": f"lean_interact {action}",
        "output": output or error,
        "messages": messages,
        "tactics": tactics,
        "feedback_lean": _feedback_lean(checked_text, messages, tactics) if (messages or tactics) else "",
        "cache": {
            "env_before": env_before,
            "env_after": env_after,
            "cache_hit": cache_hit,
            "header_env": env_before if target is None else None,
        },
    }


def _restart_session(session: _IncrementalSession) -> tuple[_IncrementalSession | None, str]:
    key = _session_key(session.project_root, session.file_path)
    session.close()
    _SESSIONS.pop(key, None)
    new_session, error = _new_session(session.project_root, session.file_path, session.repl_dir)
    if new_session is not None:
        _SESSIONS[key] = new_session
    return new_session, error


def _ensure_header(session: _IncrementalSession, header: str, *, timeout_s: int) -> tuple[bool, str, float]:
    header_hash = _sha(header)
    if session.header_hash == header_hash and session.header_env is not None:
        return True, "", 0.0
    if session.header_hash and session.header_hash != header_hash:
        restarted, error = _restart_session(session)
        if restarted is None:
            return False, error, 0.0
        session.__dict__.update(restarted.__dict__)
    response, elapsed, error = _run_command(session, header, env=None, include_tactics=False, timeout_s=timeout_s)
    if error:
        return False, error, elapsed
    if response is None or bool(response.has_errors()):
        return False, "LeanInteract header warmup failed", elapsed
    session.header_hash = header_hash
    session.header_env = getattr(response, "env", None)
    session.checkpoints.clear()
    session.segment_names.clear()
    return True, "", elapsed


def _ensure_env_before(
    session: _IncrementalSession,
    segments: list[LeanIncrementalSegment],
    target_index: int,
    *,
    timeout_s: int,
) -> tuple[int | None, str, float, bool]:
    env = session.header_env
    total_elapsed = 0.0
    cache_hit = True
    for segment in segments[:target_index]:
        checkpoint = session.checkpoints.get(segment.index)
        if (
            checkpoint is not None
            and checkpoint.before_env == env
            and checkpoint.text_hash == segment.text_hash
            and checkpoint.after_env is not None
        ):
            env = checkpoint.after_env
            continue
        cache_hit = False
        response, elapsed, error = _run_command(
            session,
            segment.text,
            env=env,
            include_tactics=False,
            timeout_s=timeout_s,
        )
        total_elapsed += elapsed
        if error:
            return None, error, total_elapsed, cache_hit
        if response is None or bool(response.has_errors()):
            messages = _message_payloads(response, line_offset=segment.start_line - 1, limit=4) if response is not None else []
            summary = _format_message_summary(messages)
            detail = f": {summary}" if summary else ""
            return None, f"failed to build env before target at {segment.name or segment.index}{detail}", total_elapsed, cache_hit
        after_env = getattr(response, "env", None)
        session.checkpoints[segment.index] = _Checkpoint(before_env=env, after_env=after_env, text_hash=segment.text_hash)
        session.segment_names[segment.index] = segment.name
        env = after_env
    return env, "", total_elapsed, cache_hit


def lean_incremental_check(
    *,
    action: str,
    file_path: str,
    theorem_id: str = "",
    cwd: str = "",
    replacement: str = "",
    include_tactics: bool = False,
    timeout_s: int = 60,
) -> dict[str, Any]:
    normalized_action = str(action or "check_target").strip().lower().replace("-", "_")
    project_root = _resolve_project_root(cwd, file_path)
    if project_root is None:
        return {"success": False, "ok": False, "backend": "lean_interact", "action": normalized_action, "error": "Lean project root not detected"}
    resolved = _resolve_file_path(file_path, project_root)
    if not resolved.is_file():
        return {"success": False, "ok": False, "backend": "lean_interact", "action": normalized_action, "file": str(resolved), "error": "Lean file not found"}
    text = resolved.read_text(encoding="utf-8")
    header, segments = _segment_file(text)
    session, error = _get_session(project_root, resolved)
    if session is None:
        return {"success": False, "ok": False, "backend": "lean_interact", "action": normalized_action, "file": str(resolved), "error": error}
    ok_header, header_error, header_elapsed = _ensure_header(session, header, timeout_s=timeout_s)
    if not ok_header:
        return {
            "success": False,
            "ok": False,
            "backend": "lean_interact",
            "action": normalized_action,
            "file": str(resolved),
            "error": header_error,
            "elapsed_s": round(header_elapsed, 3),
        }
    if normalized_action == "prepare_file":
        target = _find_segment(segments, theorem_id) if theorem_id else None
        if target is not None:
            env, env_error, env_elapsed, cache_hit = _ensure_env_before(session, segments, target.index, timeout_s=timeout_s)
            return {
                "success": not bool(env_error),
                "ok": not bool(env_error),
                "backend": "lean_interact",
                "action": normalized_action,
                "file": str(resolved),
                "target": target.name,
                "target_range": {"start_line": target.start_line, "end_line": target.end_line},
                "elapsed_s": round(header_elapsed + env_elapsed, 3),
                "error": env_error,
                "cache": {"header_env": session.header_env, "env_before": env, "cache_hit": cache_hit},
            }
        return {
            "success": True,
            "ok": True,
            "backend": "lean_interact",
            "action": normalized_action,
            "file": str(resolved),
            "elapsed_s": round(header_elapsed, 3),
            "cache": {"header_env": session.header_env, "cache_hit": header_elapsed == 0.0},
        }

    target = _find_segment(segments, theorem_id)
    if target is None:
        return {
            "success": False,
            "ok": False,
            "backend": "lean_interact",
            "action": normalized_action,
            "file": str(resolved),
            "target": theorem_id,
            "error": "target declaration not found",
        }
    env_before, env_error, env_elapsed, cache_hit = _ensure_env_before(
        session,
        segments,
        target.index,
        timeout_s=timeout_s,
    )
    if env_error:
        return {
            "success": False,
            "ok": False,
            "backend": "lean_interact",
            "action": normalized_action,
            "file": str(resolved),
            "target": target.name,
            "error": env_error,
            "elapsed_s": round(env_elapsed, 3),
        }
    checked_text = str(replacement or "") or target.text
    want_tactics = include_tactics or normalized_action == "feedback"
    payload_include_tactics = want_tactics
    response, check_elapsed, check_error = _run_command(
        session,
        checked_text,
        env=env_before,
        include_tactics=want_tactics,
        timeout_s=timeout_s,
    )
    if response is not None and not check_error and not want_tactics:
        has_errors = bool(response.has_errors()) if hasattr(response, "has_errors") else False
        valid_without_sorry = (
            bool(response.lean_code_is_valid(allow_sorry=False))
            if hasattr(response, "lean_code_is_valid")
            else False
        )
        if has_errors or not valid_without_sorry or _has_sorry(response):
            tactic_response, tactic_elapsed, tactic_error = _run_command(
                session,
                checked_text,
                env=env_before,
                include_tactics=True,
                timeout_s=timeout_s,
            )
            check_elapsed += tactic_elapsed
            if tactic_response is not None and not tactic_error:
                response = tactic_response
                payload_include_tactics = True
    env_after = getattr(response, "env", None) if response is not None else None
    if response is not None and not check_error and bool(response.lean_code_is_valid(allow_sorry=False)):
        session.checkpoints[target.index] = _Checkpoint(
            before_env=env_before,
            after_env=env_after,
            text_hash=_sha(checked_text),
        )
        session.segment_names[target.index] = target.name
    return _response_payload(
        response,
        backend="lean_interact",
        action=normalized_action,
        file_path=resolved,
        target=target,
        elapsed_s=env_elapsed + check_elapsed,
        env_before=env_before,
        env_after=env_after,
        cache_hit=cache_hit,
        include_tactics=payload_include_tactics,
        checked_text=checked_text,
        error=check_error,
    )
