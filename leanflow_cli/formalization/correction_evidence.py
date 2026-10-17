"""Bind replayed correction warrants to exact closed goals without granting source approval."""

from __future__ import annotations

import copy
from typing import Any

from leanflow_cli.formalization.evidence import identity

_FORBIDDEN_AXIOMS = {"sorryAx", "Lean.ofReduceBool", "Lean.trustCompiler"}
_SAFE_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}


def semantic_context_identity(
    checks: dict[str, Any], *, include_pending_bodies: bool = True
) -> str:
    """Bind compiled mathematics and policy without temporary paths, timings or process metadata."""
    fields = (
        "name",
        "module",
        "role",
        "declaration_origin",
        "kind",
        "levels",
        "is_prop",
        "unsafe",
        "pending",
        "conditional",
        "axioms",
        "type_axioms",
        "depends_on",
        "type_depends_on",
        "direct_dependencies",
        "direct_sorry",
        "direct_type_sorry",
        "type_untracked_sorry",
        "data_sorry",
        "untracked_sorry",
        "correction_shape",
        "generated_from",
        "generation_kind",
        "semantic_surface",
        "authority",
    )

    def profile(row: dict[str, Any]) -> dict[str, Any]:
        value = {key: row[key] for key in fields if key in row}
        value["type"] = row.get("type_hash") or row.get("type")
        if include_pending_bodies or not (
            row.get("kind") == "theorem" and row.get("pending") is True
        ):
            value["body"] = row.get("body_hash") or row.get("body")
        return value

    replay = checks.get("kernel_replay", {})
    return identity(
        {
            "schema": "correction-semantic-context-v1",
            "policy_id": checks.get("policy_id"),
            "checker_sha256": checks.get("checker_sha256"),
            "allowed_axioms": sorted(checks.get("allowed_axioms", [])),
            "replay_scope": replay.get("scope"),
            "axiom_hygiene_scope": replay.get("axiom_hygiene_scope"),
            "declarations": sorted(
                (profile(row) for row in checks.get("declarations", [])),
                key=lambda row: row["name"],
            ),
            "library_declarations": sorted(
                (profile(row) for row in checks.get("library_declarations", [])),
                key=lambda row: (row["name"], row.get("role", "dependency")),
            ),
        }
    )


def bind_evidence(
    record: dict[str, Any],
    checks: dict[str, Any],
    corrections: list[dict[str, Any]],
    context_id: str,
) -> list[dict[str, Any]]:
    """Bind independently replayed closed facts without granting correction or translation approval."""
    if not corrections:
        checks["correction_evidence"] = []
        return []
    replay = checks.get("kernel_replay", {})
    if (
        checks.get("accepted") is not True
        or replay.get("scope") != "referenced-dependency-closure"
        or replay.get("axiom_hygiene_scope") != "noncompiler-import-closure"
    ):
        raise ValueError("Correction evidence requires independent kernel replay")
    if not isinstance(record.get("environment_id"), str) or not record["environment_id"]:
        raise ValueError("Correction evidence requires the exact checked environment identity")
    profiles = {row["name"]: row for row in checks.get("declarations", [])}
    library = {row["name"]: row for row in checks.get("library_declarations", [])}
    all_profiles = {**library, **profiles}
    permitted = set(checks.get("allowed_axioms", [])) & _SAFE_AXIOMS
    checked_context = semantic_context_identity(checks)
    adopted = {name for row in corrections for name in row["adopted"]["declaration_names"]}
    owners: dict[str, set[str]] = {}
    for row in corrections:
        for name in row["adopted"]["declaration_names"]:
            owners.setdefault(name, set()).add(row["id"])
    correction_edges: dict[str, set[str]] = {}
    verified: set[str] = set()

    def reference(name: str) -> dict[str, Any]:
        row = profiles.get(name)
        if row is None or not all(
            isinstance(row.get(key), str) and row[key] for key in ("type_hash", "body_hash")
        ):
            raise ValueError("Correction references a missing exact compiled declaration: " + name)
        return {
            "name": name,
            "type_hash": row["type_hash"],
            "body_hash": row["body_hash"],
            "definition_context_hash": checked_context,
        }

    def closed(name: str) -> None:
        """Traverse each replayed dependency once without recursive Python stack growth."""
        active: set[str] = set()
        pending = [(name, False)]
        while pending:
            current, finished = pending.pop()
            if finished:
                active.remove(current)
                verified.add(current)
                continue
            if current in active:
                raise ValueError("Correction evidence has a circular dependency")
            row = all_profiles.get(current)
            if current in verified or row is None:
                # Compiler-base constants have no semantic rows; the full import
                # closure was replayed under the existing pinned-base policy.
                continue
            if (
                set(row.get("axioms", ["sorryAx"])) - permitted
                or set(row.get("axioms", [])) & _FORBIDDEN_AXIOMS
            ):
                raise ValueError("Correction evidence has a pending or unapproved axiom dependency")
            if current in profiles:
                if (
                    any(
                        row.get(field) is not False
                        for field in (
                            "pending",
                            "unsafe",
                            "direct_sorry",
                            "direct_type_sorry",
                            "type_untracked_sorry",
                            "data_sorry",
                            "untracked_sorry",
                        )
                    )
                    or row.get("depends_on")
                    or row.get("type_depends_on")
                    or set(row.get("type_axioms", ["sorryAx"])) - permitted
                ):
                    raise ValueError(
                        "Correction evidence depends on incomplete or unsafe declarations"
                    )
                if current in adopted and row.get("kind") == "theorem":
                    raise ValueError("Correction evidence cannot assume an adopted theorem")
            dependencies = row.get("direct_dependencies")
            if not isinstance(dependencies, list) or any(
                not isinstance(key, str) for key in dependencies
            ):
                raise ValueError("Correction evidence lacks complete dependency evidence")
            active.add(current)
            pending.extend([(current, True), *((dependency, False) for dependency in dependencies)])

    result = []
    for correction in corrections:
        for unit in record.get("units", []):
            if correction["source_item_id"] not in unit.get("required_source_ids", []):
                continue
            clauses = {row["obligation_id"]: row for row in unit.get("clause_dispositions", [])}
            for key in correction["clause_ids"]:
                disposition = clauses.get(key, {})
                if (
                    disposition.get("disposition") != "represented_via_correction"
                    or disposition.get("correction_id") != correction["id"]
                    or set(disposition.get("declaration_names", []))
                    != set(correction["adopted"]["declaration_names"])
                ):
                    raise ValueError(
                        "Every corrected clause requires its exact correction disposition and adopted declarations"
                    )
        mapped = {
            ref["name"]
            for unit in record.get("units", [])
            if correction["source_item_id"] in unit.get("required_source_ids", [])
            for ref in unit.get("declarations", [])
        }
        if set(correction["adopted"]["declaration_names"]) - mapped:
            raise ValueError("Correction adopted declarations lack an exact source-unit mapping")
        declarations = [reference(name) for name in correction["adopted"]["declaration_names"]]
        facts = []
        for fact in correction["evidence"]:
            goal, proof = (profiles.get(fact[key], {}) for key in ("goal_name", "proof_name"))
            shape, proof_shape = goal.get("correction_shape", {}), proof.get("correction_shape", {})
            if (
                goal.get("kind") != "definition"
                or goal.get("levels") != []
                or shape.get("type_head_forall") is not False
                or shape.get("proposition_body_head_forall") is not False
                or shape.get("proposition_body_is_prop") is not True
                or proof.get("kind") != "theorem"
                or proof.get("is_prop") is not True
                or proof.get("levels") != []
                or proof_shape.get("type_head_forall") is not False
                or proof_shape.get("type_constant_name") != fact["goal_name"]
            ):
                raise ValueError(
                    "Correction warrants require a closed Prop goal and a theorem of exactly that named goal, without hypotheses"
                )
            closed(fact["goal_name"])
            closed(fact["proof_name"])
            facts.append(
                {
                    **fact,
                    "goal": reference(fact["goal_name"]),
                    "proof": reference(fact["proof_name"]),
                }
            )
        payload = {
            "correction_id": correction["id"],
            "content_id": correction["content_id"],
            "context_id": context_id,
            "environment_id": record.get("environment_id"),
            "policy_id": record.get("policy_id"),
            "authority_refs": correction["authority_refs"],
            "declarations": declarations,
            "evidence": facts,
            "checked_context_id": checked_context,
        }
        reachable: set[str] = set()
        pending_names = [
            fact[key] for fact in correction["evidence"] for key in ("goal_name", "proof_name")
        ]
        while pending_names:
            name = pending_names.pop()
            if name in reachable:
                continue
            reachable.add(name)
            pending_names.extend(all_profiles.get(name, {}).get("direct_dependencies", []))
        correction_edges[correction["id"]] = {
            owner
            for name in reachable
            for owner in owners.get(name, set())
            if owner != correction["id"]
        }
        result.append({**payload, "evidence_id": "correction-evidence-" + identity(payload)})
    remaining = dict(correction_edges)
    while remaining:
        independent = {
            key
            for key, dependencies in remaining.items()
            if not dependencies.intersection(remaining)
        }
        if not independent:
            raise ValueError("A group of proposed corrections has circular justification")
        remaining = {
            key: dependencies for key, dependencies in remaining.items() if key not in independent
        }
    checks["correction_evidence"] = result
    return copy.deepcopy(result)
