"""Compile all owned modules and enforce named proof obligations from kernel evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
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
from leanflow_cli.formalization.lean_origin import (
    authenticated_imported_equation as authenticated_imported_equation,
)
from leanflow_cli.formalization.lean_origin import imported_origin_owner, provenance_parent_allowed
from leanflow_cli.workflows.prover.check_command_cleanup import command_workspace
from leanflow_cli.workflows.prover.check_process import isolated_command
from leanflow_cli.workflows.prover.source import lean_code_mask, project_path, project_sources

POLICY_VERSION = "formalization-exact-artifact-kernel-proofs-v6"
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
    is based on independently imported kernel constants. Literal autoImplicit
    false only disables implicit binder creation; other source options remain
    restricted to the existing positive resource limits.
    """
    masked = lean_code_mask(source)
    masked = re.sub(r"\bset_option\s+(?:maxHeartbeats|maxRecDepth)\s+[1-9]\d*\b", "", masked)
    masked = re.sub(r"\bset_option\s+autoImplicit\s+false(?=\s|$)", "", masked)
    if re.search(r"\bset_option\b", masked):
        raise ValueError(
            "Only bounded resource options and literal autoImplicit false are supported in reviewed drafts"
        )
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
    root: Path, *, layout: dict[str, Any] | None = None
) -> dict[str, str]:
    """Reject locally defined elaboration extensions even when imported by an innocuous draft.

    Declared mathematical library sources remain declarative. Executable roots
    and undeclared scripts are separate from this admission boundary.
    Dependencies must be recorded declarative inputs or pinned
    compiled libraries whose mathematical import closure is independently replayed.
    """
    dependency_inputs(root)
    result = {}
    for path in project_sources(root):
        if path.name == "lakefile.lean":
            continue
        if layout is not None:
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
    rows: list[dict[str, Any]],
    allowed_axioms: tuple[str, ...],
    *,
    dependency: bool = False,
    origin_parents: list[dict[str, Any]] | None = None,
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
    origin_profiles = (
        evaluate_policy(origin_parents, allowed_axioms)["profiles"] if origin_parents else {}
    )
    if set(origin_profiles) & set(profiles):
        raise ValueError("Origin parent evidence duplicates an authored declaration")
    if any(
        not provenance_parent_allowed(parent, origin_profiles)
        for parent in origin_profiles.values()
    ):
        raise ValueError(
            "Origin parents require complete source-backed definitions or exact matchers"
        )
    inspected_parents = {**origin_profiles, **profiles}
    if not dependency:
        for row in declarations:
            if any(
                parent not in inspected_parents
                or (
                    (
                        parent in origin_profiles
                        or inspected_parents[parent]["module"] != row["module"]
                    )
                    and not authenticated_imported_equation(row, inspected_parents[parent])
                    and imported_origin_owner(row, inspected_parents) is None
                )
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
        "origin_parent_profiles": origin_profiles,
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
    local_artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if os.environ.get("LEAN_PATH") or os.environ.get("ELAN_TOOLCHAIN"):
        raise ValueError("Formalization inspection rejects compiler or loader overrides")
    script = workspace / "Inspect.lean"
    script.write_text(INSPECTOR, encoding="utf-8")
    lean_executable = compiler_executable(root, "lean")
    compiler_library = Path(lean_executable).parent.parent / "lib" / "lean"
    artifact_map: dict[str, list[list[str]]] = {}
    roles: dict[str, str] = {}
    local_artifacts = local_artifacts or {}
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
    paths_file = workspace / "library-paths.json"
    paths_file.write_text(
        json.dumps({"paths": [str(workspace), str(library)], "artifacts": encoded, "roles": roles}),
        encoding="utf-8",
    )
    result = isolated_command(
        project_root=root,
        workspace=workspace,
        argv=[
            lean_executable,
            "--run",
            str(script),
            str(workspace),
            str(library),
            str(compiler_library),
            "closure",
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
    parent_report = workspace / "origin-parent-declarations.jsonl"
    origin_parents = (
        [json.loads(line) for line in parent_report.read_text().split("\n") if line.strip()]
        if parent_report.is_file() and not dependency
        else []
    )
    if any(not isinstance(row, dict) for row in origin_parents):
        raise ValueError("Invalid origin parent declaration report")
    checked = evaluate_policy(
        rows, allowed_axioms, dependency=dependency, origin_parents=origin_parents
    )
    # Existing project reuse is read-only, but its compiler ranges still identify
    # the frozen local source. Dependency ranges have no such source authority.
    if dependency and local_artifacts:
        raw = {row["name"]: row for row in rows}
        for row in checked["declarations"]:
            if row["module"] in local_artifacts:
                for key in ("line_start", "source_range"):
                    if key in raw[row["name"]]:
                        row[key] = raw[row["name"]][key]
    checked["kernel_replay"] = json.loads(replay_path.read_text())
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
) -> dict[str, Any]:
    """Build every requested project-relative file and inspect all exported constants.

    ``root`` must be the controller's frozen proposal, never an agent-writable
    checkout. Only its evaluated build outputs are writable by the compiler.
    Import-only aggregators are valid modules with an empty declaration list.
    Without a campaign deadline, inspection has a ten-minute cap. Every source
    and dependency input is admitted and replayed through the strict policy.
    """
    try:
        if deadline is None:
            deadline = deadline_after(DEFAULT_INSPECTION_TIMEOUT_SECONDS)
        layout = inspect_layout(root, deadline=deadline, cancelled=cancelled)
        if not layout.get("accepted"):
            return layout
        admitted_sources = _admitted_project_sources(root, layout=layout)
        mapping = {module_for_file(layout, path): path for path in modules}
        if len(mapping) != len(modules):
            raise ValueError("Duplicate generated modules")
        if not mapping:
            raise ValueError("No generated modules were supplied")
        sources = {path: project_path(root, path).read_bytes() for path in modules}
        for data in sources.values():
            validate_source(data.decode("utf-8"))
        workspace.mkdir(parents=True, exist_ok=True)
        with command_workspace(prefix="leanflow-draft-build-") as temporary:
            built = build_project(
                root,
                layout,
                temporary,
                files=modules,
                deadline=deadline,
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
            or _admitted_project_sources(root, layout=layout) != admitted_sources
        ):
            raise ValueError("Generated source changed during compilation")
        # A fresh inspection directory prevents source-side elaboration from
        # retaining write access to the inspector or forging its JSON output.
        with command_workspace(prefix="leanflow-draft-inspect-") as temporary:
            directory = temporary
            library = project_path(root, layout["lean_lib_dir"])
            result = _inspect(
                root,
                list(mapping),
                directory,
                [],
                allowed_axioms,
                deadline,
                cancelled,
                library,
                local_artifacts=built["local_artifacts"],
            )
            if result.get("accepted"):
                for row in result.get("origin_parent_profiles", {}).values():
                    if row["module"] in built.get("local_artifacts", {}):
                        candidates = [
                            file
                            for file in admitted_sources
                            if module_for_file(layout, file) == row["module"]
                        ]
                        if len(candidates) != 1:
                            raise ValueError("Origin parent has no exact admitted project source")
                        row.update(
                            file=candidates[0],
                            source_sha256=admitted_sources[candidates[0]],
                            declaration_origin="existing_project",
                        )
                    else:
                        row["declaration_origin"] = "dependency"
                for row in result["declarations"]:
                    row["file"] = mapping[row["module"]]
                    row["declaration_origin"] = "generated"
                    row["source_sha256"] = hashlib.sha256(sources[row["file"]]).hexdigest()
                result.update(
                    modules=mapping,
                    layout=layout,
                    artifact_sha256={},
                    local_artifacts=built.get("local_artifacts", {}),
                    admitted_project_sources=admitted_sources,
                    execution_policy="local-elaboration-extensions-rejected; recorded-referenced-library-closure-kernel-replayed",
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
    local_artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve exact read-only project or dependency names using recorded artifacts.

    Project modules are available only through the controller's compiled artifact
    map; arbitrary local search paths never authorize existing-declaration reuse.
    """
    try:
        if deadline is None:
            deadline = deadline_after(DEFAULT_INSPECTION_TIMEOUT_SECONDS)
        if not names or not imports or len(names) != len(set(names)):
            raise ValueError("Exact distinct names and explicit imports are required")
        layout = inspect_layout(root, deadline=deadline, cancelled=cancelled)
        if not layout.get("accepted"):
            return layout
        sources = _admitted_project_sources(root, layout=layout)
        local_sources = {module_for_file(layout, name): name for name in sources}
        library = project_path(root, layout["lean_lib_dir"])
        library.mkdir(parents=True, exist_ok=True)
        workspace.mkdir(parents=True, exist_ok=True)
        with command_workspace(prefix="leanflow-dependency-inspect-") as temporary:
            result = _inspect(
                root,
                imports,
                temporary,
                names,
                allowed_axioms,
                deadline,
                cancelled,
                library,
                dependency=True,
                local_artifacts=local_artifacts,
            )
        if result.get("accepted"):
            for row in result["declarations"]:
                if row["module"] in (local_artifacts or {}):
                    relative = local_sources.get(row["module"])
                    if relative is None:
                        raise ValueError(
                            f"Existing declaration has no admitted project source: {row['name']}"
                        )
                    row.update(
                        declaration_origin="existing_project",
                        file=relative,
                        source_sha256=sources[relative],
                    )
                else:
                    row["declaration_origin"] = "dependency"
            result["profiles"] = {row["name"]: row for row in result["declarations"]}
        return result
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"accepted": False, "error": f"Dependency inspection failed: {exc}"}
