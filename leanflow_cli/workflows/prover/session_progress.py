"""Bound repeated unchanged research evidence without judging mathematical progress."""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from core.utils import atomic_json_write
from tools.utilities.empirical_compute_runtime import MAX_PROGRAM_BYTES

_ROLES = frozenset({"orchestrator", "planner", "research", "review", "reviewer"})
_TOOLS = frozenset({"read_file", "web_search", "fetch_resource", "compute"})
_MAX_ENTRIES = 256
_MAX_STATE_BYTES = 256_000


class _Observation(TypedDict):
    result: str
    count: int


@dataclass(frozen=True)
class ProgressNotice:
    """Describe a repeated-evidence warning or a request to finish the stage."""

    action: Literal["warn", "report"]
    tool: str
    count: int
    signature: str
    message: str


def _digest(value: Any) -> str:
    """Hash stable structured evidence without retaining its potentially private text."""
    serialized = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _resolved_path(raw: Any, workspace: Path) -> Path:
    """Resolve the same relative-path base as the scratch file tools."""
    path = Path(str(raw))
    return (path if path.is_absolute() else workspace / path).resolve()


def _arguments(name: str, args: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    """Normalize tool defaults and syntax without merging distinct computations or queries."""
    if name == "read_file":
        return {
            "path": str(_resolved_path(args["path"], workspace)),
            "offset": max(0, int(args.get("offset", 0))),
            "limit": min(16000, max(1, int(args.get("limit", 8000)))),
        }
    if name == "web_search":
        return {
            "query": str(args["query"]).strip(),
            "limit": min(5, max(1, int(args.get("limit", 5)))),
        }
    if name == "fetch_resource":
        # A different destination basename does not make the source new evidence.
        return {"url": str(args["url"]).strip()}
    program = str(args["program"])
    if len(program.encode("utf-8")) > MAX_PROGRAM_BYTES:
        return {"program": program}
    try:
        program = ast.dump(ast.parse(program), include_attributes=False)
    except (SyntaxError, ValueError, RecursionError):
        pass
    return {"program": program}


def _evidence(
    name: str, args: Mapping[str, Any], result: Mapping[str, Any], workspace: Path
) -> Any:
    """Retain result changes while discarding fetch bookkeeping and timing noise."""
    evidence = dict(result)
    if name == "fetch_resource" and result.get("success") is True:
        for field in {
            "retrieved_at",
            "path",
            "source_path",
            "manifest_path",
            "model_calls",
            "status",
        }:
            evidence.pop(field, None)
    if name == "compute":
        for field in {"elapsed_s", "duration_s", "elapsed_ms"}:
            evidence.pop(field, None)
    version = None
    if name == "read_file" and result.get("success") is True:
        # A changed file permits a fresh read even when this particular window is
        # unchanged. Never inspect failed reads, which may have been access-denied.
        try:
            stat = _resolved_path(args["path"], workspace).stat()
            version = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        except (KeyError, OSError, RuntimeError, ValueError):
            pass
    return [evidence, version]


class SessionProgress:
    """Warn on three identical observations and request a report on the sixth.

    Track each semantic request across intervening tools and scratch writes.
    Changed results or a changed read target restart only that request's count;
    unrelated calls and no-op writes cannot erase its history. This is an evidence
    repetition limit, not a mathematical failure verdict. Proving is unaffected.
    """

    def __init__(self, runtime_directory: Path, *, role: str, workspace: Path) -> None:
        """Restore bounded hashed observations and a sticky report handoff on resume."""
        self.path = runtime_directory / "tool-progress.json"
        self.workspace = workspace.resolve()
        self.enabled = role in _ROLES
        self.report_only = False
        self.report_reason = ""
        self._role = role
        self._observations: dict[str, _Observation] = {}
        if self.enabled and self.path.exists():
            self._restore()

    def _restore(self) -> None:
        """Restore valid state or finish the stage if its guard ledger is damaged."""
        try:
            if self.path.stat().st_size > _MAX_STATE_BYTES:
                raise ValueError("Progress state exceeds its size bound")
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(saved, dict)
                or saved.get("version") != 1
                or saved.get("role") != self._role
                or type(saved.get("report_only")) is not bool
                or not isinstance(saved.get("observations"), dict)
                or len(saved["observations"]) > _MAX_ENTRIES
            ):
                raise ValueError("Invalid progress state")
            observations: dict[str, _Observation] = {}
            for signature, entry in saved["observations"].items():
                if (
                    not _is_digest(signature)
                    or not isinstance(entry, dict)
                    or not _is_digest(entry.get("result"))
                    or type(entry.get("count")) is not int
                    or not 1 <= entry["count"] <= 6
                ):
                    raise ValueError("Invalid progress observation")
                observations[signature] = {"result": entry["result"], "count": entry["count"]}
            self._observations = observations
            self.report_only = saved["report_only"]
            if self.report_only:
                self.report_reason = _report_message()
        except (OSError, ValueError, TypeError, UnicodeError):
            # A bad ledger cannot refund previously consumed repetition allowance.
            # Report existing findings; do not label this a mathematical failure.
            self.report_only = True
            self.report_reason = (
                "The saved repeated-tool guard state could not be validated. Tools are "
                "disabled; return the requested report from existing findings and state "
                "remaining uncertainties. This does not establish a mathematical failure."
            )

    def observe(
        self, name: str, args: Mapping[str, Any], result: Mapping[str, Any]
    ) -> ProgressNotice | None:
        """Persist one completed tool observation and return a threshold notice once.

        Call after every actual tool invocation, including each member of a batch.
        The caller must honor ``report_only`` before further research dispatch and
        model requests, keeping the normal stage report/recovery budget intact.
        """
        if not self.enabled or name not in _TOOLS or self.report_only:
            return None
        try:
            normalized = _arguments(name, args, self.workspace)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError, OverflowError):
            normalized = dict(args)
        signature = _digest([name, normalized])
        digest = _digest(_evidence(name, args, result, self.workspace))
        previous = self._observations.pop(signature, None)
        count = previous["count"] + 1 if previous and previous["result"] == digest else 1
        self._observations[signature] = {"result": digest, "count": count}
        while len(self._observations) > _MAX_ENTRIES:
            del self._observations[next(iter(self._observations))]
        notice = None
        if count == 3:
            notice = ProgressNotice(
                "warn",
                name,
                count,
                signature,
                f"The same {name} request has returned unchanged evidence 3 times. "
                "Use the evidence already obtained, resolve a different concrete uncertainty, "
                "or return the requested report. Six unchanged observations will end tool "
                "exploration and require a report with remaining uncertainties.",
            )
        elif count >= 6:
            self.report_only = True
            self.report_reason = _report_message()
            notice = ProgressNotice("report", name, count, signature, self.report_reason)
        atomic_json_write(
            self.path,
            {
                "version": 1,
                "role": self._role,
                "report_only": self.report_only,
                "observations": self._observations,
            },
        )
        return notice


def _is_digest(value: Any) -> bool:
    """Recognize a SHA-256 digest in persisted progress state."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _report_message() -> str:
    """Explain the report handoff without asserting that the mathematical plan failed."""
    return (
        "The same tool request returned unchanged evidence six times. Tool exploration "
        "has ended; tools are disabled. Return the complete requested JSON/report from "
        "the accumulated findings, with unresolved uncertainties stated honestly. "
        "This handoff does not establish that the mathematical plan or helpers are invalid."
    )
