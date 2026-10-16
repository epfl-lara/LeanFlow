"""Read static setup inputs and freeze bounded read-only preparation evidence."""

from __future__ import annotations

import copy
import json
import re
import tomllib
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained, digest, identity
from leanflow_cli.formalization.handoff_slots import source_entries
from leanflow_cli.formalization.inventory_authority import complete_inventory
from leanflow_cli.formalization.lean_configuration import static_configuration
from leanflow_cli.formalization.records import source_bytes
from leanflow_cli.workflows.prover.source import lean_code_mask, project_sources

CONFIGURATION_FILES = ("lean-toolchain", "lakefile.toml", "lakefile.lean", "lake-manifest.json")


def configuration_hashes(root: Path) -> dict[str, str | None]:
    """Bind present and absent project setup files without following aliases."""
    return {
        name: digest(path.read_bytes()) if path.is_file() else None
        for name in CONFIGURATION_FILES
        for path in [contained(root, name)]
    }


def read_configuration(root: Path) -> dict[str, Any]:
    """Read screened literal Lake fields without evaluating any configuration."""
    toml, lean = contained(root, "lakefile.toml"), contained(root, "lakefile.lean")
    if toml.is_file() and lean.is_file():
        raise ValueError("Choose one Lake configuration before preparation")
    path = toml if toml.is_file() else lean
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    static_configuration(path, text)
    if path.suffix == ".toml":
        return dict(tomllib.loads(text))
    config: dict[str, Any] = {"lean_lib": [], "require": []}
    context = config
    literal = r'"(?:[^"\\\n]|\\.)*"'
    next_default = False
    for line in lean_code_mask(text, preserve_literals=True).splitlines():
        line = line.strip()
        command = re.fullmatch(
            r'(?:@\[\s*default_target\s*\]\s*)?(package|lean_lib|lean_exe)\s+(\w+|"[^"]+")(?:\s+where)?',
            line,
        )
        if command:
            kind, name = command.groups()
            name = json.loads(name) if name.startswith('"') else name
            context = config if kind == "package" else {"name": name}
            if kind == "package":
                config["name"] = name
            else:
                config.setdefault(kind, []).append(context)
                if next_default or "default_target" in line:
                    config.setdefault("defaultTargets", []).append(name)
            next_default = False
            continue
        if line == "@[default_target]":
            next_default = True
            continue
        requirement = re.fullmatch(
            rf"require\s+(\w+)\s+from\s+git\s+({literal})(?:\s*@\s*({literal}))?", line
        )
        scoped = re.fullmatch(
            rf"require\s+({literal})\s*/\s*({literal})\s*@\s*git\s+({literal})", line
        )
        if requirement or scoped:
            if requirement:
                name, url, revision = requirement.groups()
                row = {"name": name, "git": json.loads(url)}
                if revision:
                    row["rev"] = json.loads(revision)
            else:
                assert scoped is not None
                scope, name, revision = (json.loads(value) for value in scoped.groups())
                row = {"name": name, "scope": scope, "rev": revision}
            config["require"].append(row)
            continue
        assignment = re.fullmatch(r"(srcDir|buildDir|leanLibDir)\s*:=\s*(.+)", line)
        if assignment:
            context[assignment[1]] = json.loads(assignment[2])
        roots = re.fullmatch(r"(roots|defaultTargets)\s*:=\s*#\[(.*?)\]", line)
        if roots:
            context[roots[1]] = re.findall(r"\x60([A-Za-z_][\w.]*)", roots[2])
    return config


def existing_packages(root: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Retain declared requirements and their exact resolved manifest pins."""
    path = contained(root, "lake-manifest.json")
    manifest = json.loads(path.read_text()) if path.is_file() else {}
    if not isinstance(manifest, dict) or not isinstance(manifest.get("packages", []), list):
        raise ValueError("Project dependency manifest is malformed")
    packages: dict[str, dict[str, Any]] = {}
    for row in manifest.get("packages", []):
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ValueError("Project dependency manifest has an invalid package")
        name = row["name"]
        if name.casefold() in {key.casefold() for key in packages}:
            raise ValueError("Project dependency manifest has duplicate package identities")
        packages[name] = {"name": name, "manifest": copy.deepcopy(row), "declared": False}
    for row in config.get("require", []):
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ValueError("Lake dependency requirement has no exact name")
        if any(
            name != row["name"] and name.casefold() == row["name"].casefold() for name in packages
        ):
            raise ValueError("Lake dependency names must retain exact portable package identities")
        package = packages.setdefault(row["name"], {"name": row["name"], "manifest": None})
        if package.get("declared"):
            raise ValueError("Lake configuration declares a dependency more than once")
        package.update(requirement=copy.deepcopy(row), declared=True)
    return [packages[name] for name in sorted(packages)]


def preparation_documents(
    root: Path, record: dict[str, Any], plan: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Freeze source evidence and a bounded existing-project declaration census."""
    inventory = complete_inventory(record)
    documents: dict[str, dict[str, Any]] = {"inventory": copy.deepcopy(inventory)}
    for relative, checksum in record["source_files"].items():
        data = source_bytes(record, relative)
        encoding = record["inventory"]["files"][relative].get("encoding", "utf-8")
        documents["source:" + relative] = {
            "sha256": checksum,
            "encoding": encoding,
            **(
                {"text": data.decode(encoding)}
                if encoding != "binary"
                else {
                    "pages": record["inventory"].get("pages", []),
                    "rendered_page_delivery": "unavailable",
                    "fidelity": "requires verified transcription before source acceptance",
                }
            ),
        }
    index = []
    macros = {row["id"]: row for row in inventory.get("macro_definitions", [])}
    for item in inventory["items"]:
        evidence_id = "source-item:" + item["id"]
        # Preserve complete statements, clauses and provenance separately from
        # the short navigation index; the canonical coverage denominator stays intact.
        documents[evidence_id] = {
            "item": copy.deepcopy(item),
            "macro_context": [
                copy.deepcopy(macros[key]) for key in item.get("context_refs", []) if key in macros
            ],
            "authority": "frozen source candidates; no semantic or coverage approval",
        }
        index.append(
            {
                "source_id": item["id"],
                "kind": item["kind"],
                "scope": item["scope"],
                "title": item.get("title", "")[:160],
                "statement_excerpt": item["text"][:400],
                "statement_excerpt_complete": len(item["text"]) <= 400,
                "clause_ids": [row["id"] for row in item.get("known_obligations", [])],
                "statement_spans": copy.deepcopy(item["statement_spans"]),
                "evidence_id": evidence_id,
            }
        )
    documents["mathematical-source-index"] = {
        "source_revision_id": record["revision_id"],
        "canonical_inventory_ref": "inventory",
        "source_item_count": len(index),
        "known_clause_count": sum(len(row["clause_ids"]) for row in index),
        "canonical_region_count": len(inventory["regions"]),
        "items": index,
        "authority": "navigation only; excerpts never narrow source coverage or approve translations",
    }
    census: list[dict[str, Any]] = []
    total_bytes = 0
    omitted: list[str] = []
    for path in project_sources(root):
        if path.name == "lakefile.lean":
            continue
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        if len(census) >= 64 or size > 512_000 or total_bytes + size > 4_000_000:
            omitted.append(relative)
            continue
        data = path.read_bytes()
        if digest(path.read_bytes()) != digest(data):
            raise ValueError("Existing project source changed during preparation discovery")
        text = data.decode("utf-8")
        key = "existing-source:" + relative
        declarations = [
            {"name": row["declaration"], "line_start": row["line"]} for row in source_entries(text)
        ]
        documents[key] = {
            "file": relative,
            "sha256": digest(data),
            "text": text,
            "declarations": declarations,
            "authority": "source-candidates-only; compilation-required",
        }
        census.append(
            {
                "file": relative,
                "sha256": digest(data),
                "declarations": declarations,
                "evidence_id": key,
            }
        )
        total_bytes += size
    documents["preparation-assignment"] = {
        "source_revision_id": record["revision_id"],
        "base_plan_id": plan["plan_id"],
        "mathematical_source_index_ref": "mathematical-source-index",
        "reading_order": (
            "Read mathematical-source-index completely, following next_offset until null. "
            "It lists every canonical item and known clause, including excluded items. "
            "Read relevant source-item evidence for complete statements, clauses, proofs and "
            "macro context. Exact statement_spans locate source bytes or PDF page text. "
            "Use source:<file> for surrounding passages and inventory for the complete "
            "lexical/region audit; do not begin with its preamble or treat excerpts as coverage."
        ),
        "default_plan": plan,
        "existing_declaration_candidates": census,
        "omitted_project_sources": omitted,
        "census_complete": not omitted,
        "evidence_catalog": {
            key: {
                "sha256": identity(value),
                "characters": len(json.dumps(value, ensure_ascii=False)),
            }
            for key, value in documents.items()
        },
        "approval": "none; estimates require compilation, representative slice and independent source review",
    }
    return documents
