"""Compile all owned modules and enforce named proof obligations from kernel evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.deadlines import deadline_after, remaining_seconds
from leanflow_cli.formalization.dependency_inputs import dependency_inputs
from leanflow_cli.formalization.environment import compiler_executable
from leanflow_cli.formalization.lean_build import (
    _library_artifacts,
    _setup_artifacts,
    build_project,
)
from leanflow_cli.formalization.lean_inspector import INSPECTOR
from leanflow_cli.formalization.lean_layout import build_membership, inspect_layout, module_for_file
from leanflow_cli.workflows.prover.check_process import isolated_command
from leanflow_cli.workflows.prover.source import lean_code_mask, project_path, project_sources
from leanflow_cli.workflows.prover.type_profile import _shadow_package_root

POLICY_VERSION = "formalization-exact-artifact-kernel-proofs-v3"
DEFAULT_INSPECTION_TIMEOUT_SECONDS = 600.0
_EFFECTFUL = re.compile(
    r"(?<![\w'])"
    r"(?:initialize|builtin_initialize|run_tac|run_elab|elab|elab_rules|macro|macro_rules|"
    r"syntax|declare_syntax_cat|unsafe|extern|implemented_by|include_str|include_bin|"
    r"run_cmd|run_meta|simproc|dsimproc|compile_inductive|add_decl|addDecl|IO|System|Environment|ofReduceBool|ofReduceNat|"
    r"example|command_elab|term_elab|tactic|builtin_tactic|builtin_command_elab|"
    r"builtin_term_elab|extern|csimp|implemented_by)"
    r"(?![\w'])"
)


def validate_source(source: str) -> None:
    """Reject unsupported effectful syntax before invoking the isolated compiler.

    This is an admission restriction, not declaration discovery. The pinned
    imported environment remains a trusted input; all declaration policy below
    is based on independently imported kernel constants.
    """
    masked = lean_code_mask(source)
    masked = re.sub(r"\bset_option\s+(?:maxHeartbeats|maxRecDepth)\s+[1-9]\d*\b", "", masked)
    if re.search(r"\bset_option\b", masked):
        raise ValueError("Only bounded resource options are supported in reviewed drafts")
    if re.search(r"#\s*eval\b", masked):
        raise ValueError("Unsupported effectful elaboration or external input: #eval")
    if match := _EFFECTFUL.search(masked):
        raise ValueError(f"Unsupported effectful elaboration or external input: {match.group()}")
    # Quoted identifiers could otherwise disguise disallowed effectful names.
    if "«" in source or "»" in source:
        raise ValueError("Quoted identifiers require a syntax-aware admission adapter")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _admitted_project_sources(
    root: Path, *, require_closed_inputs: bool = True, layout: dict[str, Any] | None = None
) -> dict[str, str]:
    """Reject locally defined elaboration extensions even when imported by an innocuous draft.

    Declared mathematical library sources remain declarative. Executable roots
    and undeclared scripts are separate from this admission boundary.
    Dependencies must be recorded declarative inputs or pinned
    compiled libraries whose mathematical import closure is independently replayed.
    """
    if require_closed_inputs:
        dependency_inputs(root)
    result = {}
    for path in project_sources(root):
        if path.name == "lakefile.lean":
            continue
        if require_closed_inputs and layout is not None:
            membership = build_membership(layout, path.relative_to(root).as_posix())
            if membership is None or membership[1] not in layout.get("libraries", []):
                continue
        data = path.read_bytes()
        try:
            validate_source(data.decode("utf-8"))
        except ValueError as error:
            raise ValueError(
                f"Unsupported project elaboration in {path.relative_to(root)}: {error}"
            ) from error
        result[path.relative_to(root).as_posix()] = hashlib.sha256(data).hexdigest()
    return result


def evaluate_policy(
    rows: list[dict[str, Any]], allowed_axioms: tuple[str, ...], *, dependency: bool = False
) -> dict[str, Any]:
    """Validate complete inspector records and derive conditional proof status."""
    permitted = set(allowed_axioms) - {"sorryAx"}
    declarations: list[dict[str, Any]] = []
    names: set[str] = set()
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("Missing or duplicate compiled declaration identity")
        names.add(name)
        for field in ("type", "kind", "module"):
            if not isinstance(row.get(field), str) or not row[field]:
                raise ValueError(f"Missing compiled {field} for {name}")
        for field in (
            "axioms",
            "type_axioms",
            "type_depends_on",
            "depends_on",
            "direct_dependencies",
            "generated_from",
            "levels",
        ):
            if not isinstance(row.get(field), list) or any(
                not isinstance(value, str) for value in row[field]
            ):
                raise ValueError(f"Incomplete {field} evidence for {name}")
        if not isinstance(row.get("generation_kind"), str) or bool(row["generated_from"]) != bool(
            row["generation_kind"]
        ):
            raise ValueError(f"Incomplete compiler origin evidence for {name}")
        for field in (
            "is_prop",
            "unsafe",
            "internal",
            "direct_sorry",
            "direct_type_sorry",
            "type_untracked_sorry",
            "data_sorry",
            "pending",
            "untracked_sorry",
        ):
            if type(row.get(field)) is not bool:
                raise ValueError(f"Incomplete {field} evidence for {name}")
        if row["unsafe"] or (row["kind"] == "axiom" and not dependency):
            raise ValueError(f"New axioms or unsafe declarations are forbidden: {name}")
        if (
            row["direct_type_sorry"]
            or row["type_untracked_sorry"]
            or set(row["type_axioms"]) - permitted - {"sorryAx"}
            or ("sorryAx" in row["type_axioms"] and not row["type_depends_on"])
        ):
            raise ValueError(f"Declaration type contains a hole or unapproved assumption: {name}")
        if row["data_sorry"]:
            raise ValueError(f"Computational data contains a hole: {name}")
        if set(row["axioms"]) - permitted - {"sorryAx"}:
            raise ValueError(f"Unapproved axiom dependency in {name}")
        if row["direct_sorry"] and not (
            row["pending"] and row["is_prop"] and row["kind"] == "theorem"
        ):
            raise ValueError(f"A hole is outside a named propositional proof slot: {name}")
        if row["untracked_sorry"]:
            raise ValueError(f"Untracked placeholder dependency in {name}")
        if dependency and "sorryAx" in row["axioms"]:
            raise ValueError(f"Library reuse requires an already verified declaration: {name}")
        if "sorryAx" in row["axioms"] and not (row["pending"] or row["depends_on"]):
            raise ValueError(f"A placeholder has no designated obligation: {name}")
        body = row.get("body")
        if body is not None and not isinstance(body, str):
            raise ValueError(f"Invalid compiled body evidence for {name}")
        declarations.append(
            {
                **row,
                "type_hash": _hash([row["levels"], row["type"]]),
                "body_hash": _hash(body),
                "conditional": bool(row["depends_on"]),
                "origin": "compiler_generated" if row["generated_from"] else "declaration",
            }
        )
    pending = {row["name"] for row in declarations if row["pending"]}
    if any(set(row["depends_on"]) - pending for row in declarations):
        raise ValueError("Obligation dependencies escape the inspected declaration set")
    profiles = {row["name"]: row for row in declarations}
    if not dependency:
        for row in declarations:
            if any(
                parent not in profiles or profiles[parent]["module"] != row["module"]
                for parent in row["generated_from"]
            ):
                raise ValueError(
                    f"Compiler-generated declaration has no inspected parent: {row['name']}"
                )
    else:
        for row in declarations:
            row.pop("line_start", None)
            row.pop("source_range", None)

    def closure(name: str, active: frozenset[str] = frozenset()) -> set[str]:
        if name in active:
            raise ValueError("Pending proof obligations have a dependency cycle")
        direct = set(profiles[name]["depends_on"])
        return direct | {
            transitive for target in direct for transitive in closure(target, active | {name})
        }

    closures = {row["name"]: sorted(closure(row["name"])) for row in declarations}
    for row in declarations:
        row["direct_obligations"] = row["depends_on"]
        row["depends_on"] = closures[row["name"]]
    return {
        "accepted": True,
        "policy_id": POLICY_VERSION,
        "checker_sha256": hashlib.sha256(INSPECTOR.encode()).hexdigest(),
        "allowed_axioms": sorted(permitted),
        "declarations": declarations,
        "profiles": profiles,
        "obligations": [row for row in declarations if row["pending"]],
    }


def _inspect(
    root: Path,
    modules: list[str],
    workspace: Path,
    names: list[str],
    allowed_axioms: tuple[str, ...],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    library: Path,
    *,
    dependency: bool = False,
    full_import_replay: bool = True,
    local_artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if full_import_replay and (os.environ.get("LEAN_PATH") or os.environ.get("ELAN_TOOLCHAIN")):
        raise ValueError("Formalization inspection rejects compiler or loader overrides")
    script = workspace / "Inspect.lean"
    script.write_text(INSPECTOR, encoding="utf-8")
    lean_executable = compiler_executable(root, "lean")
    compiler_library = Path(lean_executable).parent.parent / "lib" / "lean"
    artifact_map: dict[str, list[list[str]]] = {}
    roles: dict[str, str] = {}
    local_artifacts = local_artifacts or {}
    if full_import_replay:
        artifact_map = _library_artifacts(root, compiler_library)
        roles = dict.fromkeys(artifact_map, "dependency")
        for module, artifact in local_artifacts.items():
            if module in artifact_map:
                raise ValueError(f"Local artifact shadows a recorded dependency: {module}")
            parts = artifact["parts"]
            flattened = [path for group in parts for path in group]
            if not flattened or set(flattened) != set(artifact["sha256"]):
                raise ValueError(f"Incomplete local artifact hash binding: {module}")
            for filename in flattened:
                path = Path(filename)
                if (
                    path.is_symlink()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"][filename]
                ):
                    raise ValueError(f"Compiled local artifact changed: {module}")
            artifact_map[module] = parts
            roles[module] = "project"
        version = re.search(r"(?:^|[:/])v?(\d+\.\d+\.\d+)", (root / "lean-toolchain").read_text())
        if artifact_map and version is None:
            raise ValueError("Exact artifact loading requires a versioned pinned compiler")
        encoded = _setup_artifacts(artifact_map, version[1]) if version else {}
    else:
        encoded = {}
    paths_file = workspace / "library-paths.json"
    paths_file.write_text(
        json.dumps({"paths": [str(workspace), str(library)], "artifacts": encoded, "roles": roles}),
        encoding="utf-8",
    )
    executable = (
        [lean_executable]
        if full_import_replay
        else [compiler_executable(root, "lake"), "env", lean_executable]
    )
    result = isolated_command(
        project_root=root,
        workspace=workspace,
        argv=[
            *executable,
            "--run",
            str(script),
            str(workspace),
            str(library),
            str(compiler_library),
            "closure" if full_import_replay else "project",
            str(paths_file),
            *modules,
            "--",
            *names,
        ],
        timeout_s=remaining_seconds(deadline),
        cancelled=cancelled,
    )
    for module, artifact in local_artifacts.items():
        for filename, expected in artifact["sha256"].items():
            if hashlib.sha256(Path(filename).read_bytes()).hexdigest() != expected:
                raise ValueError(f"Compiled local artifact changed during inspection: {module}")
    report = workspace / "declarations.jsonl"
    replay_path = workspace / "kernel-replay.json"
    if not result.get("success") or not report.is_file() or not replay_path.is_file():
        diagnostic = str(result.get("stderr", ""))
        if any(
            marker in diagnostic
            for marker in (
                "maximum number of heartbeats",
                "maximum recursion depth",
                "deterministic timeout",
                "maxRecDepth",
            )
        ):
            result = {**result, "error_code": "check_timeout"}
        return {
            "accepted": False,
            "error": "Independent kernel inspection failed",
            "details": result,
        }
    rows = [
        json.loads(line) for line in report.read_text(encoding="utf-8").split("\n") if line.strip()
    ]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("Invalid kernel declaration report")
    if names and {row.get("name") for row in rows} != set(names):
        raise ValueError("Kernel report does not contain every exact requested declaration")
    checked = evaluate_policy(rows, allowed_axioms, dependency=dependency)
    checked["kernel_replay"] = json.loads(replay_path.read_text())
    if full_import_replay:
        library_report = workspace / "library-evidence.jsonl"
        evidence = [json.loads(line) for line in library_report.read_text().splitlines() if line]
        for row in evidence:
            row["type_hash"] = _hash([row["levels"], row["type"]])
            row["body_hash"] = _hash(row["body"]) if row["body"] is not None else None
            row["authority"] = "kernel-checked-referenced-compiled-library"
        checked["library_declarations"] = evidence
        checked["dependency_authority"] = "kernel-checked-referenced-compiled-library"
    return checked


def inspect_draft(
    root: Path,
    modules: list[str],
    workspace: Path,
    allowed_axioms: tuple[str, ...],
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    *,
    require_closed_inputs: bool = True,
) -> dict[str, Any]:
    """Build every requested project-relative file and inspect all exported constants.

    ``root`` must be the controller's frozen proposal, never an agent-writable
    checkout. Only its evaluated build outputs are writable by the compiler.
    Import-only aggregators are valid modules with an empty declaration list.
    Without a campaign deadline, closed-input formalization has a ten-minute cap;
    ordinary statement drafting retains its five-minute cap.
    """
    try:
        if deadline is None:
            deadline = deadline_after(
                DEFAULT_INSPECTION_TIMEOUT_SECONDS if require_closed_inputs else 300.0
            )
        layout = inspect_layout(root, deadline=deadline, cancelled=cancelled)
        if not layout.get("accepted"):
            return layout
        admitted_sources = _admitted_project_sources(
            root, require_closed_inputs=require_closed_inputs, layout=layout
        )
        mapping = {module_for_file(layout, path): path for path in modules}
        if len(mapping) != len(modules):
            raise ValueError("Duplicate generated modules")
        if not mapping:
            raise ValueError("No generated modules were supplied")
        sources = {path: project_path(root, path).read_bytes() for path in modules}
        for data in sources.values():
            validate_source(data.decode("utf-8"))
        workspace.mkdir(parents=True, exist_ok=True)
        build = project_path(root, layout["build_dir"])
        build.mkdir(parents=True, exist_ok=True)
        writable = [build]
        for relative in (
            ".lake/config",
            ".lake/lakefile.olean",
            ".lake/lakefile.olean.trace",
            ".lake/lakefile.olean.lock",
        ):
            path = root / relative
            if path.exists():
                writable.append(path)
        with tempfile.TemporaryDirectory(prefix="leanflow-draft-build-") as temporary:
            if require_closed_inputs:
                built = build_project(
                    root,
                    layout,
                    Path(temporary).resolve(),
                    files=modules,
                    deadline=deadline,
                    cancelled=cancelled,
                )
            else:
                built = isolated_command(
                    project_root=root,
                    workspace=Path(temporary).resolve(),
                    argv=[
                        compiler_executable(root, "lake"),
                        "build",
                        *(f"+{module}:olean" for module in mapping),
                    ],
                    timeout_s=remaining_seconds(deadline),
                    extra_writable_roots=tuple(writable),
                    cancelled=cancelled,
                )
        if not built.get("success"):
            return {
                "accepted": False,
                "error": "Explicit generated-module build failed",
                "build": built,
            }
        if (
            any(project_path(root, path).read_bytes() != data for path, data in sources.items())
            or _admitted_project_sources(
                root, require_closed_inputs=require_closed_inputs, layout=layout
            )
            != admitted_sources
        ):
            raise ValueError("Generated source changed during compilation")
        # A fresh inspection directory prevents source-side elaboration from
        # retaining write access to the inspector or forging its JSON output.
        with tempfile.TemporaryDirectory(prefix="leanflow-draft-inspect-") as temporary:
            directory = Path(temporary).resolve()
            artifacts: dict[str, str] = {}
            library = project_path(root, layout["lean_lib_dir"])
            if not require_closed_inputs:
                for module in mapping:
                    artifact = library.joinpath(*module.split(".")).with_suffix(".olean")
                    if not artifact.is_file() or artifact.is_symlink():
                        raise ValueError(f"Compiled artifact missing or aliased: {module}")
                    target = directory.joinpath(*module.split(".")).with_suffix(".olean")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    for suffix in ("", ".private", ".server", ".ir"):
                        source = (
                            artifact.with_suffix(suffix)
                            if suffix == ".ir"
                            else Path(str(artifact) + suffix)
                        )
                        if source.is_file():
                            destination = (
                                target.with_suffix(suffix)
                                if suffix == ".ir"
                                else Path(str(target) + suffix)
                            )
                            shutil.copyfile(source, destination)
                            artifacts[f"{module}{suffix}"] = hashlib.sha256(
                                destination.read_bytes()
                            ).hexdigest()
                for package in {module.split(".")[0] for module in mapping}:
                    _shadow_package_root(directory / package, library / package)
                    root_artifact = library / f"{package}.olean"
                    if root_artifact.is_file() and not (directory / root_artifact.name).exists():
                        for suffix in ("", ".private", ".server", ".ir"):
                            source = (
                                root_artifact.with_suffix(suffix)
                                if suffix == ".ir"
                                else Path(str(root_artifact) + suffix)
                            )
                            if source.exists():
                                shutil.copyfile(source, directory / source.name)
            result = _inspect(
                root,
                list(mapping),
                directory,
                [],
                allowed_axioms,
                deadline,
                cancelled,
                library,
                full_import_replay=require_closed_inputs,
                local_artifacts=built["local_artifacts"] if require_closed_inputs else None,
            )
            if result.get("accepted"):
                for row in result["declarations"]:
                    row["file"] = mapping[row["module"]]
                result.update(
                    modules=mapping,
                    layout=layout,
                    artifact_sha256=artifacts,
                    local_artifacts=built.get("local_artifacts", {}),
                    admitted_project_sources=admitted_sources,
                    execution_policy=(
                        "local-elaboration-extensions-rejected; recorded-referenced-library-closure-kernel-replayed"
                        if require_closed_inputs
                        else "local-elaboration-extensions-rejected; existing-pinned-packages-trusted"
                    ),
                )
            return result
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {
            "accepted": False,
            "error": f"Draft policy inspection failed: {exc}",
            "declarations": [],
            "obligations": [],
        }


def inspect_dependency(
    root: Path,
    names: list[str],
    workspace: Path,
    *,
    imports: list[str],
    allowed_axioms: tuple[str, ...] = ("propext", "Classical.choice", "Quot.sound"),
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Resolve exact existing names in explicit pinned imports without new declarations."""
    try:
        if deadline is None:
            deadline = deadline_after(DEFAULT_INSPECTION_TIMEOUT_SECONDS)
        if not names or not imports or len(names) != len(set(names)):
            raise ValueError("Exact distinct names and explicit imports are required")
        layout = inspect_layout(root, deadline=deadline, cancelled=cancelled)
        if not layout.get("accepted"):
            return layout
        _admitted_project_sources(root, layout=layout)
        library = project_path(root, layout["lean_lib_dir"])
        library.mkdir(parents=True, exist_ok=True)
        workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="leanflow-dependency-inspect-") as temporary:
            return _inspect(
                root,
                imports,
                Path(temporary).resolve(),
                names,
                allowed_axioms,
                deadline,
                cancelled,
                library,
                dependency=True,
            )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"accepted": False, "error": f"Dependency inspection failed: {exc}"}
