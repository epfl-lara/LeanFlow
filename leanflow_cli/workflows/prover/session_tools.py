"""Expose bounded scratch tools without canonical-source mutation authority."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from core.utils import atomic_json_write
from leanflow_cli.workflows.prover.resource_handoff import shared_resource
from tools.utilities.repository_research_policy import clean_room_path_block_reason


def _schema(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    """Build one provider-neutral function schema."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TEXT = {"type": "string"}
INTEGER = {"type": "integer"}


class SessionTools:
    """Own one job's read boundary, scratch writes and bounded tool history."""

    def __init__(
        self,
        *,
        role: str,
        project_root: Path,
        workspace: Path,
        context: Mapping[str, Any],
        research_job: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.role = role
        self.project_root = project_root.resolve()
        self.workspace = workspace.resolve()
        self.context = context
        self.artifacts: set[str] = set()
        self.revision = 0
        self.repeated: dict[str, int] = {}
        self.research_job = research_job
        self.research_resources: dict[str, Any] = {}

    def schemas(self) -> list[dict[str, Any]]:
        """Return only tools permitted to this role; advisors are never exposed."""
        tools = [
            _schema(
                "read_file",
                "Read project source, your workspace, or exact paths in the supplied resource inventory. Relative paths are based on your private workspace; use project_root plus the DAG file path for project source. PLAN and DAG are already in the assignment; other jobs' notes and controller files are private. Omit offset/limit for a normal read. Offset is a UTF-8 byte position (default 0); limit is a CHARACTER count (default 8000, maximum 16000), never a line count.",
                {"path": TEXT, "offset": INTEGER, "limit": INTEGER},
                ["path"],
            ),
            _schema(
                "write_file",
                "Write only inside your job workspace. Use PLAN_job.md for proof notes and candidate.txt for a completed replacement of the assigned sorry.",
                {"path": TEXT, "content": TEXT},
                ["path", "content"],
            ),
            _schema(
                "replace_text",
                "Replace one exact unique string in a scratch file.",
                {"path": TEXT, "old": TEXT, "new": TEXT},
                ["path", "old", "new"],
            ),
        ]
        if self.role in {"prover", "negation"}:
            tools += [
                _schema(
                    "search_project",
                    "Find Lean declarations by literal text in source and installed dependencies. Use focused searches and act on the results.",
                    {"query": TEXT, "path": TEXT},
                    ["query"],
                ),
                _schema(
                    "lean_check",
                    "Check the scratch file with warm LeanProbe. For a temporary declaration replacement give its COMPLETE signature and body. This check does NOT save the replacement to disk. After success, submit candidate.txt or the requested JSON with the exact text replacing the literal sorry. Only the controller can accept a proof.",
                    {"file": TEXT, "declaration": TEXT, "replacement": TEXT},
                    [],
                ),
                _schema(
                    "lean_search",
                    "Search Lean libraries with the existing Lean search service.",
                    {"query": TEXT},
                    ["query"],
                ),
            ]
        if self.role == "prover" and self.research_job is not None:
            tools.append(
                _schema(
                    "research_job",
                    "Launch one separate bounded research agent for a concrete external source or computational question. It cannot solve Lean goals or provide generic proof advice. Its calls count against the campaign budget. Available once per prover job, including resumes.",
                    {"question": TEXT},
                    ["question"],
                )
            )
        if self.role in {"orchestrator", "planner", "research", "negation"}:
            tools += [
                _schema(
                    "web_search",
                    "Run one bounded external resource search and retain the results locally.",
                    {"query": TEXT, "limit": INTEGER},
                    ["query"],
                ),
                _schema(
                    "fetch_resource",
                    "Reuse a resource from the supplied inventory or download a public paper/source page with provenance. Optional filename is a simple basename such as paper.md, never a directory or resources/paper.md. New files go under your workspace's resources directory; read the exact returned path.",
                    {
                        "url": TEXT,
                        "filename": {
                            "type": "string",
                            "description": "Optional basename only, without path components; omit to generate a name.",
                            "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}$",
                        },
                    },
                    ["url"],
                ),
                _schema(
                    "compute",
                    "Run a short exact integer/rational Python experiment in a restricted subprocess. No filesystem or network access. Empirical results are evidence, not Lean proofs.",
                    {"program": TEXT},
                    ["program"],
                ),
            ]
        return tools

    def _path(self, raw: str, *, write: bool = False) -> Path:
        """Resolve paths against the workspace and reject symlink escapes."""
        candidate = Path(raw)
        path = (candidate if candidate.is_absolute() else self.workspace / candidate).resolve()
        roots = (self.workspace,) if write else (self.workspace, self.project_root)
        if not any(path.is_relative_to(root) for root in roots):
            raise ValueError("Path is outside this job's permitted workspace")
        if write and path.is_relative_to(self.workspace / "resources"):
            raise ValueError(
                "Downloaded resources are read-only; use fetch_resource to create them"
            )
        if not write and not path.is_relative_to(self.workspace):
            reason = clean_room_path_block_reason(path, cwd=self.project_root)
            if reason:
                raise ValueError(reason)
        if (
            not write
            and path.is_relative_to(self.project_root / ".leanflow")
            and not path.is_relative_to(self.workspace)
            and shared_resource(self.context, self.project_root, path=path) is None
            and shared_resource(self.research_resources, self.project_root, path=path) is None
        ):
            raise ValueError(
                "Other jobs and controller state are private; use the supplied plan, DAG, and exact resource inventory paths"
            )
        if write and path.name in {
            "PLAN.md",
            "DAG.json",
            "state.json",
            "inbox.jsonl",
            "request-count.json",
            "report.json",
            "events.jsonl",
        }:
            raise ValueError("Controller artifact names are reserved; use PLAN_job.md")
        return path

    def invoke(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """Execute a permitted tool and return bounded structured feedback."""
        if name not in {item["function"]["name"] for item in self.schemas()}:
            return {"success": False, "error": f"Tool {name} is unavailable to role {self.role}"}
        signature = json.dumps([name, args, self.revision], sort_keys=True)
        self.repeated[signature] = self.repeated.get(signature, 0) + 1
        if self.repeated[signature] > 2 and name in {
            "search_project",
            "lean_search",
            "web_search",
            "fetch_resource",
        }:
            return {
                "success": False,
                "error": "This search already returned twice without a scratch edit. Use its evidence to try a proof step, or change the query.",
            }
        try:
            return self._invoke(name, args)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def _invoke(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch tools without a generic terminal or project write escape."""
        if name == "research_job":
            question = str(args["question"]).strip()
            if not question or len(question) > 4000:
                raise ValueError("Supply one concrete research question of at most 4000 characters")
            path = self.workspace.parent / ".runtime" / self.workspace.name / "research-count.json"
            if path.is_file():
                raise ValueError(
                    "This prover job already requested its research agent; use the returned evidence"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json_write(path, {"used": 1, "question": question})
            assert self.research_job is not None
            result = self.research_job(question)
            # The callback is controller-owned and derives grants from completed
            # resource files; the child model's JSON cannot grant private reads.
            self.research_resources = {"resources": result.get("resources", {})}
            return result
        if name == "read_file":
            path = self._path(str(args["path"]))
            if not path.exists():
                raise ValueError(
                    f"File not found: {path}. Relative paths use this job's workspace; "
                    "use the supplied project_root for source or exact resource inventory paths."
                )
            if not path.is_file():
                raise ValueError("Only regular files can be read")
            offset, limit = max(0, int(args.get("offset", 0))), min(
                16000, max(1, int(args.get("limit", 8000)))
            )
            with path.open(encoding="utf-8") as handle:
                handle.seek(offset)
                content = handle.read(limit)
                next_offset = handle.tell()
            return {"success": True, "content": content, "next_offset": next_offset}
        if name in {"write_file", "replace_text"}:
            path = self._path(str(args["path"]), write=True)
            previous = path.read_text(encoding="utf-8") if path.is_file() else None
            if name == "write_file":
                content = str(args["content"])
            else:
                if previous is None:
                    raise ValueError("The file to replace does not exist")
                content = previous
                old = str(args["old"])
                if not old or content.count(old) != 1:
                    raise ValueError("The old text must match exactly once")
                content = content.replace(old, str(args["new"]), 1)
            if len(content.encode()) > 512000:
                raise ValueError("Scratch files are limited to 512 KB")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            if content != previous:
                self.revision += 1
            self.artifacts.add(str(path))
            return {
                "success": True,
                "path": str(path),
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
        if name == "search_project":
            from leanflow_cli.workflows.prover.session_search import search_sources

            path = self._path(str(args.get("path") or self.project_root))
            return search_sources(str(args["query"]), path, self._path)
        if name == "lean_check":
            from leanflow_cli.workflows.prover.check_process import check_scratch

            path = self._path(
                str(args.get("file") or self.context.get("scratch_file", "")), write=True
            )
            declaration = str(args.get("declaration") or "")
            return check_scratch(
                project_root=self.project_root,
                workspace=self.workspace,
                file=path,
                declaration=declaration,
                replacement=str(args.get("replacement") or ""),
                timeout_s=60,
            )
        if name == "lean_search":
            from leanflow_cli.lean.lean_services import lean_search
            from tools.implementations.lean_tool import _filter_clean_room_lean_search_results

            return _filter_clean_room_lean_search_results(
                lean_search(
                    query=str(args["query"]), cwd=str(self.project_root), limit=5
                ).to_dict(),
                cwd=str(self.project_root),
            )
        if name == "web_search":
            from leanflow_cli.workflows.prover.session_research import web_search

            result = web_search(str(args["query"]), limit=min(5, max(1, int(args.get("limit", 5)))))
            filename = (
                "search-" + hashlib.sha256(str(args["query"]).encode()).hexdigest()[:12] + ".json"
            )
            artifact = self.workspace / filename
            artifact.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            self.artifacts.add(str(artifact))
            return result
        if name == "fetch_resource":
            from leanflow_cli.workflows.prover.session_research import _url_policy, fetch_resource

            url = str(args["url"]).strip()
            reason = _url_policy(url)
            if reason:
                raise ValueError(reason)
            saved = shared_resource(self.context, self.project_root, url=url)
            if saved is not None:
                return {"success": True, "status": "reused", "model_calls": 0, **saved}
            result = fetch_resource(url, self.workspace, str(args.get("filename") or ""))
            if result.get("path"):
                self.artifacts.add(str(result["path"]))
            return result
        if name == "compute":
            from tools.utilities import empirical_compute_runtime

            program = str(args["program"])
            if len(program.encode()) > empirical_compute_runtime.MAX_PROGRAM_BYTES:
                raise ValueError("Experiment exceeds the bounded program size")
            process = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    str(Path(empirical_compute_runtime.__file__).resolve()),
                    "--timeout-s",
                    "4",
                ],
                input=program,
                capture_output=True,
                text=True,
                cwd=self.workspace,
                env={"LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"},
                timeout=6,
            )
            result = json.loads(process.stdout)
            if not isinstance(result, dict):
                raise ValueError("Invalid computation result")
            return result
        raise ValueError(f"Unknown tool {name}")
