"""Plan bounded project preparation while preserving existing dependency and source identities."""

from __future__ import annotations

import copy
import re
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from leanflow_cli.formalization.evidence import digest, identity, normalized_relative
from leanflow_cli.formalization.preparation_bounds import (
    MAX_BLOCKER_CHARS,
    MAX_BLOCKERS,
    MAX_CANDIDATE_DECLARATIONS,
    MAX_DECLARATION_HINT_CHARS,
    MAX_IMPORTS,
    MAX_PACKAGE_FIELD_CHARS,
    MAX_PACKAGES,
    MAX_PREREQUISITE_DESCRIPTION_CHARS,
    MAX_PREREQUISITES,
    MAX_REPRESENTATION_NOTE_CHARS,
    MAX_REPRESENTATION_NOTES,
    MAX_REPRESENTATION_NOTES_BYTES,
    MAX_SLICE_SOURCE_IDS,
)
from leanflow_cli.formalization.preparation_inputs import (
    configuration_hashes,
    existing_packages,
    read_configuration,
)
from leanflow_cli.workflows.project_setup.scaffold import (
    DEFAULT_TOOLCHAIN,
    MATHLIB_REVISION,
    REPL_REVISION,
    REPL_URL,
    read_toolchain,
)

MATHLIB_URL = "https://github.com/leanprover-community/mathlib4"
CATALOG = {
    "mathlib": {"url": MATHLIB_URL, "revision": MATHLIB_REVISION, "imports": ["Mathlib"]},
    "repl": {"url": REPL_URL, "revision": REPL_REVISION, "imports": []},
}
_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*")
_PIN = re.compile(r"[0-9a-fA-F]{40}")
_DEFINITION_KINDS = {"definition", "notation", "construction"}


def _declaration_hint(value: object) -> bool:
    """Recognize bounded Unicode declaration hints without granting import or type authority."""
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_DECLARATION_HINT_CHARS:
        return False
    for component in value.split("."):
        if not component or not (
            component[0] == "_" or unicodedata.category(component[0]).startswith("L")
        ):
            return False
        if any(
            char not in {"_", "'", "?", "!"}
            and unicodedata.category(char)[0] not in {"L", "M", "N"}
            for char in component[1:]
        ):
            return False
    return True


def plan_identity(plan: dict[str, Any]) -> str:
    """Identify the exact setup intent independently of its presentation."""
    return "preparation-plan-" + identity(
        {key: value for key, value in plan.items() if key != "plan_id"}
    )


def _slice(record: dict[str, Any]) -> list[str]:
    """Choose a small source slice containing a definition and a result when present."""
    items = [item for item in record["inventory"]["items"] if item.get("scope") != "excluded"]
    definitions = [item for item in items if item["kind"] in _DEFINITION_KINDS]
    results = [item for item in items if item["kind"] not in _DEFINITION_KINDS]
    chosen = [*definitions[:2], *results[:2]] or items[:2]
    return [item["id"] for item in items if item in chosen]


def _layout(config: dict[str, Any], document_id: str) -> dict[str, Any]:
    """Estimate an existing or dedicated library; preparation must verify actual Lake membership."""
    libraries = config.get("lean_lib", [])
    candidates: list[tuple[dict[str, Any], str]] = []
    for row in libraries:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ValueError("Formalization library requires an exact configured name")
        roots = row.get("roots", [row["name"]])
        if (
            not isinstance(roots, list)
            or not roots
            or any(not isinstance(name, str) or not _MODULE.fullmatch(name) for name in roots)
        ):
            raise ValueError("Formalization needs an explicit buildable library root")
        candidates.extend((row, module) for module in roots)
    candidates.sort(
        key=lambda pair: (
            pair[0]["name"] not in config.get("defaultTargets", []),
            pair[0]["name"],
            pair[1],
        )
    )
    library = candidates[0][0] if candidates else None
    if library:
        name = library.get("name")
        root_module = candidates[0][1]
        src = (Path(config.get("srcDir", ".")) / library.get("srcDir", ".")).as_posix()
        mode = "existing_library"
    else:
        name = root_module = "Formalization" if config else "Main"
        src = config.get("srcDir", ".")
        mode = "dedicated_library" if config else "new_project"
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("Preparation requires a portable library name")
    if not isinstance(root_module, str) or not _MODULE.fullmatch(root_module):
        raise ValueError("Preparation requires a portable root module")
    if src != ".":
        normalized_relative(src)
    prefix = root_module + ".Formalization.Document_" + digest(document_id)[:16]
    owned = Path(src).joinpath(*prefix.split(".")).as_posix()
    return {
        "mode": mode,
        "library": name,
        "src_dir": src,
        "root_module": root_module,
        "root_file": Path(src).joinpath(*root_module.split(".")).with_suffix(".lean").as_posix(),
        "module_prefix": prefix,
        "owned_prefix": owned,
        "aggregator": owned + ".lean",
        "membership_verification": "required_after_preparation",
    }


def _package(row: Any, toolchain: str) -> dict[str, Any]:
    """Validate an explicitly authorized pinned package proposal without fetching it."""
    if not isinstance(row, dict) or set(row) - {
        "name",
        "url",
        "revision",
        "imports",
        "reason",
        "toolchain",
        "authorization",
    }:
        raise ValueError("Invalid preparation package proposal fields")
    result = copy.deepcopy(row)
    name, url, revision = (result.get(key) for key in ("name", "url", "revision"))
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("Preparation package needs a portable exact name")
    if not isinstance(url, str):
        raise ValueError("Preparation package requires an HTTPS source")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.strip("/")
    ):
        raise ValueError("Preparation package requires a plain HTTPS source without credentials")
    if not isinstance(revision, str) or not _PIN.fullmatch(revision):
        raise ValueError("New dependencies require a full immutable Git commit")
    if result.get("toolchain", toolchain) != toolchain:
        raise ValueError("Preparation package requests a different Lean toolchain")
    result.update(
        toolchain=toolchain,
        imports=result.get("imports", []),
        reason=result.get("reason", "Explicit user dependency plan"),
    )
    if (
        not isinstance(result["imports"], list)
        or len(result["imports"]) > MAX_IMPORTS
        or any(
            not isinstance(module, str) or not _MODULE.fullmatch(module)
            for module in result["imports"]
        )
    ):
        raise ValueError("Preparation package imports must be bounded module names")
    if (
        not isinstance(result["reason"], str)
        or not 1 <= len(result["reason"]) <= MAX_PACKAGE_FIELD_CHARS
    ):
        raise ValueError("Preparation package requires a bounded nonempty rationale")
    return result


def default_preparation_plan(
    root: Path,
    record: dict[str, Any],
    *,
    preset: str = "mathlib",
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build setup intent; only this trusted user entry point grants custom-source authorization."""
    overrides = copy.deepcopy(overrides or {})
    if set(overrides) - {
        "preset",
        "packages",
        "required_imports",
        "mathematical_prerequisites",
        "representation_notes",
        "slice_source_ids",
    }:
        raise ValueError("Unsupported preparation plan override")
    preset = overrides.get("preset", preset)
    if preset not in {"mathlib", "lean"}:
        raise ValueError("Choose the mathlib or lean preparation preset")
    before = configuration_hashes(root)
    config = read_configuration(root)
    toolchain = read_toolchain(root) or DEFAULT_TOOLCHAIN
    if not re.fullmatch(r"leanprover/lean4:v\d+\.\d+\.\d+(?:-rc\d+)?", toolchain):
        raise ValueError("Preparation requires an explicit versioned Lean toolchain")
    existing = existing_packages(root, config)
    names = {row["name"].casefold() for row in existing}
    packages, catalog_requirements, blockers = [], [], []
    requested = [_package(row, toolchain) for row in overrides.get("packages", [])]
    custom = []
    for row in requested:
        if row["name"].casefold() in names:
            current = next(
                item for item in existing if item["name"].casefold() == row["name"].casefold()
            )
            pinned = current.get("manifest") or current.get("requirement", {})
            if (
                pinned.get("url", pinned.get("git")) != row["url"]
                or pinned.get("rev") != row["revision"]
            ):
                raise ValueError(
                    "User dependency plans must reuse existing pins, never replace them"
                )
            continue
        row["authorization"] = "user"
        custom.append(row)
    wanted = (["mathlib"] if preset == "mathlib" else []) + ["repl"]
    for name in wanted:
        if name in names or any(row["name"].casefold() == name for row in custom):
            continue
        if toolchain == DEFAULT_TOOLCHAIN:
            packages.append(
                {
                    "name": name,
                    **CATALOG[name],
                    "reason": (
                        "Mathematical library" if name == "mathlib" else "LeanProbe proof checking"
                    ),
                    "toolchain": toolchain,
                    "authorization": "catalog",
                }
            )
        elif name == "repl":
            catalog_requirements.append(
                {
                    "name": "repl",
                    "url": REPL_URL,
                    "toolchain": toolchain,
                    "resolution": "matching-release",
                }
            )
        else:
            blockers.append(
                "Mathlib is missing for this Lean version; supply a compatible full Git pin"
            )
    items = [item for item in record["inventory"]["items"] if item.get("scope") != "excluded"]
    plan = {
        "schema_version": 1,
        "source_revision_id": record["revision_id"],
        "source_bundle": record["source_bundle"],
        "preset": preset,
        "toolchain": toolchain,
        "baseline_config_hashes": before,
        "existing_packages": existing,
        "packages": packages + custom,
        "catalog_requirements": catalog_requirements,
        "required_imports": overrides.get(
            "required_imports", ["Mathlib"] if preset == "mathlib" else ["Init"]
        ),
        "mathematical_prerequisites": overrides.get(
            "mathematical_prerequisites",
            [
                {
                    "source_ids": [item["id"]],
                    "description": "Search existing declarations for source "
                    + item["id"]
                    + " before defining its mathematical representation",
                    "strategy": "search_existing_then_define",
                    "candidate_declarations": [],
                }
                for item in items
                if item["kind"] in _DEFINITION_KINDS
            ][:MAX_PREREQUISITES],
        ),
        "representation_notes": overrides.get(
            "representation_notes",
            [
                "Reuse checked existing definitions and theorem statements when faithful. Definitions must be complete; missing existence results require explicit named Prop obligations."
            ],
        ),
        "layout": _layout(config, record["document_id"]),
        "slice_source_ids": overrides.get("slice_source_ids", _slice(record)),
        "network_policy": {
            "catalog": copy.deepcopy(CATALOG),
            "user_authorized_packages": custom,
            "allow_package_acquisition": True,
            "allow_unlisted_sources": False,
        },
        "blockers": blockers,
    }
    plan["plan_id"] = plan_identity(plan)
    return validate_preparation_plan(root, record, plan)


def validate_preparation_plan(
    root: Path, record: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    """Validate frozen setup intent; native recommendations cannot change its acquisition policy."""
    if plan.get("schema_version") != 1 or plan.get("plan_id") != plan_identity(plan):
        raise ValueError("Preparation plan identity is invalid")
    if (
        plan.get("source_revision_id") != record["revision_id"]
        or plan.get("source_bundle") != record["source_bundle"]
    ):
        raise ValueError("Preparation plan refers to a different source revision")
    if plan.get("baseline_config_hashes") != configuration_hashes(root):
        raise ValueError("Project configuration changed after preparation planning")
    if plan.get("existing_packages") != existing_packages(root, read_configuration(root)):
        raise ValueError("Existing package pins changed after preparation planning")
    if plan.get("toolchain") != (read_toolchain(root) or DEFAULT_TOOLCHAIN):
        raise ValueError("Preparation may not replace the project's pinned Lean toolchain")
    if plan.get("preset") not in {"mathlib", "lean"}:
        raise ValueError("Invalid project preparation preset")
    policy = plan["network_policy"]
    if (
        policy.get("catalog") != CATALOG
        or policy.get("allow_unlisted_sources") is not False
        or policy.get("allow_package_acquisition") is not True
    ):
        raise ValueError("Preparation cannot broaden the authorized dependency catalog")
    user = [_package(row, plan["toolchain"]) for row in policy.get("user_authorized_packages", [])]
    if len(plan["packages"]) > MAX_PACKAGES or len(user) > MAX_PACKAGES:
        raise ValueError("Preparation plan exceeds the package limit")
    existing_names = {row["name"].casefold() for row in plan["existing_packages"]}
    proposed_names: set[str] = set()
    for source in plan["packages"]:
        row = _package(source, plan["toolchain"])
        name = row["name"]
        if name.casefold() in existing_names | proposed_names:
            raise ValueError("Preparation may not upgrade or duplicate an existing dependency")
        proposed_names.add(name.casefold())
        catalog = CATALOG.get(name)
        allowed = (
            row.get("authorization") == "catalog"
            and plan["toolchain"] == DEFAULT_TOOLCHAIN
            and catalog is not None
            and all(row[key] == catalog[key] for key in ("url", "revision"))
        ) or any(
            all(row[key] == selected[key] for key in ("name", "url", "revision", "toolchain"))
            for selected in user
        )
        if not allowed:
            raise ValueError("Preparation package acquisition was not authorized")
    for request in plan["catalog_requirements"]:
        if (
            request
            != {
                "name": "repl",
                "url": REPL_URL,
                "toolchain": plan["toolchain"],
                "resolution": "matching-release",
            }
            or "repl" in existing_names | proposed_names
        ):
            raise ValueError("Invalid catalog compatibility resolution request")
    imports = plan["required_imports"]
    if (
        not isinstance(imports, list)
        or len(imports) > MAX_IMPORTS
        or any(not isinstance(name, str) or not _MODULE.fullmatch(name) for name in imports)
        or len(imports) != len(set(imports))
    ):
        raise ValueError("Preparation imports must be bounded distinct Lean module names")
    if any(name == "REPL" or name.startswith("REPL.") for name in imports):
        raise ValueError("REPL tooling cannot become a mathematical import")
    blockers = plan["blockers"]
    if (
        not isinstance(blockers, list)
        or len(blockers) > MAX_BLOCKERS
        or any(
            not isinstance(reason, str) or not 1 <= len(reason) <= MAX_BLOCKER_CHARS
            for reason in blockers
        )
    ):
        raise ValueError("Preparation blockers must be bounded explicit reasons")
    needs_mathlib = plan["preset"] == "mathlib" or any(
        name == "Mathlib" or name.startswith("Mathlib.") for name in imports
    )
    if needs_mathlib and "mathlib" not in existing_names | proposed_names and not blockers:
        raise ValueError("Missing compatible Mathlib must remain an explicit preparation blocker")
    items = {
        item["id"]: item for item in record["inventory"]["items"] if item.get("scope") != "excluded"
    }
    selected = plan["slice_source_ids"]
    if (
        not isinstance(selected, list)
        or not 1 <= len(selected) <= MAX_SLICE_SOURCE_IDS
        or len(selected) != len(set(selected))
        or set(selected) - set(items)
    ):
        raise ValueError("Representative slice requires one to eight included source IDs")
    if any(item["kind"] in _DEFINITION_KINDS for item in items.values()) and not any(
        items[key]["kind"] in _DEFINITION_KINDS for key in selected
    ):
        raise ValueError("Representative slice must include an available source definition")
    if any(item["kind"] not in _DEFINITION_KINDS for item in items.values()) and not any(
        items[key]["kind"] not in _DEFINITION_KINDS for key in selected
    ):
        raise ValueError("Representative slice must include an available source result")
    prerequisites = plan["mathematical_prerequisites"]
    if not isinstance(prerequisites, list) or len(prerequisites) > MAX_PREREQUISITES:
        raise ValueError("Mathematical prerequisite plan exceeds its bound")
    for index, row in enumerate(prerequisites):
        if (
            not isinstance(row, dict)
            or set(row) != {"source_ids", "description", "strategy", "candidate_declarations"}
            or row.get("strategy") != "search_existing_then_define"
            or not isinstance(row.get("source_ids"), list)
            or len(row["source_ids"]) > len(items)
            or any(not isinstance(key, str) for key in row["source_ids"])
            or set(row["source_ids"]) - set(items)
            or not isinstance(row.get("description"), str)
            or not 1 <= len(row["description"]) <= MAX_PREREQUISITE_DESCRIPTION_CHARS
            or not isinstance(row.get("candidate_declarations"), list)
            or len(row["candidate_declarations"]) > MAX_CANDIDATE_DECLARATIONS
        ):
            raise ValueError("Invalid mathematical prerequisite estimate")
        for hint_index, name in enumerate(row["candidate_declarations"]):
            if not _declaration_hint(name):
                raise ValueError(
                    f"Invalid mathematical prerequisite estimate: row {index + 1}, "
                    f"candidate_declarations[{hint_index}] requires a bounded qualified "
                    "declaration hint with Unicode identifier components and optional primes, ? or !"
                )
    notes = plan["representation_notes"]
    if not isinstance(notes, list):
        raise ValueError("Representation notes must be bounded nonempty text: expected a list")
    if len(notes) > MAX_REPRESENTATION_NOTES:
        raise ValueError(
            "Representation notes must be bounded nonempty text: "
            f"count {len(notes)} exceeds limit {MAX_REPRESENTATION_NOTES}"
        )
    total_bytes = 0
    for index, note in enumerate(notes):
        if not isinstance(note, str) or not 1 <= len(note) <= MAX_REPRESENTATION_NOTE_CHARS:
            raise ValueError(
                "Representation notes must be bounded nonempty text: "
                f"note {index + 1} requires 1..{MAX_REPRESENTATION_NOTE_CHARS} characters"
            )
        total_bytes += len(note.encode("utf-8"))
    if total_bytes > MAX_REPRESENTATION_NOTES_BYTES:
        raise ValueError(
            "Representation notes must be bounded nonempty text: "
            f"UTF-8 bytes {total_bytes} exceeds limit {MAX_REPRESENTATION_NOTES_BYTES}"
        )
    if plan.get("layout") != _layout(read_configuration(root), record["document_id"]):
        raise ValueError("Native discovery cannot replace the controller's layout allocation")
    return copy.deepcopy(plan)
