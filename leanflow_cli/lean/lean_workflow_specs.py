"""Native LeanFlow workflow and worker specs.

These markdown-backed specs replace the old plugin-shaped split between short
skill descriptions and an external command bundle. Skills remain the routing
surface, but the workflow/worker specs below are the canonical contract used by
the doctor, prompt builder, router, and Lean tool wrappers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # leanflow_cli/lean/X.py -> repo root
SPEC_ROOT = REPO_ROOT / "leanflow_specs"
VALID_SPEC_KINDS = {"workflow", "worker", "helper", "phase"}

#: Actors that may consume a workflow-phase fragment.
KNOWN_PHASE_CONSUMERS = ("orchestrator", "planner", "decomposer", "prover")


@dataclass(frozen=True)
class LeanSpecRecord:
    spec_id: str
    kind: str
    title: str
    summary: str
    aliases: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    workers: tuple[str, ...] = ()
    review_actions: tuple[str, ...] = ()
    stop_conditions: tuple[str, ...] = ()
    route_actions: tuple[str, ...] = ()
    consumed_by: tuple[str, ...] = ()
    deliverable_schema: str = ""
    phases: tuple[str, ...] = ()
    path: Path = field(default_factory=Path)
    content: str = ""

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "id": self.spec_id,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "aliases": list(self.aliases),
            "skills": list(self.skills),
            "tools": list(self.tools),
            "workers": list(self.workers),
            "review_actions": list(self.review_actions),
            "stop_conditions": list(self.stop_conditions),
            "route_actions": list(self.route_actions),
            "consumed_by": list(self.consumed_by),
            "deliverable_schema": self.deliverable_schema,
            "phases": list(self.phases),
            "path": str(self.path),
        }


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---"):
        return {}, raw
    try:
        _, frontmatter, body = raw.split("---", 2)
    except ValueError:
        return {}, raw
    try:
        payload = yaml.safe_load(frontmatter) or {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return payload, body.lstrip("\n")


def _normalize_many(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            normalized = str(item or "").strip()
            if normalized and normalized not in result:
                result.append(normalized)
        return tuple(result)
    return ()


def _normalize_schema(value: Any) -> str:
    """Canonical text for a ``deliverable_schema`` frontmatter value.

    Accepts either a YAML mapping (dumped back canonically) or a literal
    block string; anything else is empty (the validator flags it).
    """
    if not value:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return yaml.safe_dump(value, sort_keys=True).strip()
    except Exception:
        return ""


def _load_spec(path: Path) -> LeanSpecRecord:
    raw = path.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(raw)
    spec_id = str(meta.get("id", "") or path.stem).strip()
    kind = str(meta.get("kind", "") or path.parent.name.rstrip("s")).strip().lower()
    if kind not in VALID_SPEC_KINDS:
        raise ValueError(f"Invalid Lean spec kind for {path}: {kind!r}")
    title = str(meta.get("title", "") or spec_id).strip() or spec_id
    summary = str(meta.get("summary", "") or "").strip()
    if not summary:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                summary = stripped[:180]
                break
    return LeanSpecRecord(
        spec_id=spec_id,
        kind=kind,
        title=title,
        summary=summary,
        aliases=_normalize_many(meta.get("aliases")),
        skills=_normalize_many(meta.get("skills")),
        tools=_normalize_many(meta.get("tools")),
        workers=_normalize_many(meta.get("workers")),
        review_actions=_normalize_many(meta.get("review_actions")),
        stop_conditions=_normalize_many(meta.get("stop_conditions")),
        route_actions=_normalize_many(meta.get("route_actions")),
        consumed_by=_normalize_many(meta.get("consumed_by")),
        deliverable_schema=_normalize_schema(meta.get("deliverable_schema")),
        phases=_normalize_many(meta.get("phases")),
        path=path,
        content=body,
    )


@lru_cache(maxsize=1)
def load_lean_specs() -> dict[str, LeanSpecRecord]:
    specs: dict[str, LeanSpecRecord] = {}
    for path in sorted(SPEC_ROOT.rglob("*.md")):
        if any(part.startswith(".") for part in path.relative_to(SPEC_ROOT).parts):
            continue
        record = _load_spec(path)
        previous = specs.get(record.spec_id)
        if previous is not None:
            # A silently-shadowing later file would make one spec vanish
            # (e.g. phases/search.md eclipsing workflows/search.md) —
            # loudly refuse instead.
            raise ValueError(
                f"Duplicate Lean spec id {record.spec_id!r}: {previous.path} and {path}"
            )
        specs[record.spec_id] = record
    return specs


def phase_fragment_text(spec_id: str, *, include_schema: bool = True) -> str:
    """Return phase-fragment prompt text, or ``""`` when unavailable.

    ``include_schema=False`` embeds the BODY only — for prompts whose own
    reply contract must not compete with the fragment's deliverable schema
    (the fragment is POLICY there, not the reply shape).
    """
    record = get_lean_spec(spec_id)
    if record is None or record.kind != "phase" or not record.content.strip():
        return ""
    parts = [f"[PHASE SPEC: {record.spec_id}]", record.content.strip()]
    if include_schema and record.deliverable_schema:
        parts += ["", "Deliverable schema (YAML):", record.deliverable_schema]
    return "\n".join(parts)


def get_lean_spec(spec_id: str) -> LeanSpecRecord | None:
    normalized = str(spec_id or "").strip()
    if not normalized:
        return None
    specs = load_lean_specs()
    if normalized in specs:
        return specs[normalized]
    for record in specs.values():
        if normalized in record.aliases:
            return record
    return None


def specs_for_skill(skill_name: str) -> list[LeanSpecRecord]:
    wanted = str(skill_name or "").strip()
    if not wanted:
        return []
    return [record for record in load_lean_specs().values() if wanted in record.skills]


def list_specs(kind: str | None = None) -> list[LeanSpecRecord]:
    if not kind:
        return list(load_lean_specs().values())
    normalized = str(kind).strip().lower()
    return [record for record in load_lean_specs().values() if record.kind == normalized]


def validate_lean_specs() -> list[str]:
    errors: list[str] = []
    specs = load_lean_specs()
    aliases: dict[str, str] = {}
    worker_ids = {record.spec_id for record in specs.values() if record.kind == "worker"}
    for record in specs.values():
        if not record.summary:
            errors.append(f"{record.spec_id}: missing summary")
        for alias in record.aliases:
            previous = aliases.get(alias)
            if previous and previous != record.spec_id:
                errors.append(f"alias {alias!r} reused by {previous!r} and {record.spec_id!r}")
            aliases[alias] = record.spec_id
        if record.kind == "workflow":
            for worker in record.workers:
                if worker not in worker_ids:
                    errors.append(f"{record.spec_id}: unknown worker {worker!r}")
        for phase_id in record.phases:
            linked = specs.get(phase_id)
            if linked is None or linked.kind != "phase":
                errors.append(f"{record.spec_id}: unknown phase fragment {phase_id!r}")
        if record.kind == "phase":
            if not record.consumed_by:
                errors.append(f"{record.spec_id}: phase fragment declares no consumed_by")
            for consumer in record.consumed_by:
                if consumer not in KNOWN_PHASE_CONSUMERS:
                    errors.append(f"{record.spec_id}: unknown phase consumer {consumer!r}")
            if not record.deliverable_schema:
                errors.append(f"{record.spec_id}: phase fragment declares no deliverable_schema")
            else:
                try:
                    parsed = yaml.safe_load(record.deliverable_schema)
                    if not isinstance(parsed, dict):
                        errors.append(f"{record.spec_id}: deliverable_schema is not a mapping")
                except Exception:
                    errors.append(f"{record.spec_id}: deliverable_schema is not valid YAML")
    return errors
