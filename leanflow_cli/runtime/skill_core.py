"""Curated LeanFlow skill discovery and loading."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from core.home import leanflow_home
from leanflow_cli.lean.lean_workflow_specs import phase_fragment_text, specs_for_skill

CURATED_BUILTIN_SKILLS = {
    "lean-proof-loop",
    "lean-diagnostics",
    "lean-formalization",
    "lean-refactor-golf",
    "lean-autonomous-swarm",
    "lean-search",
    "lean-reasoning-help",
    "lean-theorem-queue-worker",
}

PROJECT_SKILL_DIRNAME = ".leanflow"


@dataclass(frozen=True)
class SkillRecord:
    name: str
    description: str
    source: str
    root: Path
    skill_dir: Path
    skill_md: Path

    @property
    def command_name(self) -> str:
        return self.name.lower().replace(" ", "-").replace("_", "-")

    @property
    def relative_path(self) -> str:
        return str(self.skill_md.relative_to(self.root))


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent  # leanflow_cli/runtime/X.py -> repo root


def _builtin_root() -> Path:
    return _repo_root() / "leanflow_skills"


def _leanflow_home() -> Path:
    return leanflow_home()


def _user_root() -> Path:
    return _leanflow_home() / "skills"


def _project_root(cwd: str | os.PathLike[str] | None = None) -> Path:
    base = Path(cwd or os.getcwd()).expanduser().resolve()
    for candidate in (base, *base.parents):
        leanflow_dir = candidate / PROJECT_SKILL_DIRNAME
        if leanflow_dir.is_dir():
            return leanflow_dir / "skills"
    return base / PROJECT_SKILL_DIRNAME / "skills"


def _parse_skill_metadata(skill_md: Path) -> tuple[str, str]:
    try:
        raw = skill_md.read_text(encoding="utf-8")
    except Exception:
        return skill_md.parent.name, ""

    if raw.startswith("---"):
        try:
            _, frontmatter, _ = raw.split("---", 2)
            parsed = yaml.safe_load(frontmatter) or {}
            if isinstance(parsed, dict):
                name = str(parsed.get("name", "") or "").strip() or skill_md.parent.name
                description = str(parsed.get("description", "") or "").strip()
                return name, description
        except Exception:
            pass

    name = skill_md.parent.name
    description = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            description = stripped[:120]
            break
    return name, description


def _scan_root(root: Path, source: str, *, curated_only: bool = False) -> dict[str, SkillRecord]:
    skills: dict[str, SkillRecord] = {}
    if not root.exists():
        return skills
    for skill_md in sorted(root.rglob("SKILL.md")):
        if any(part.startswith(".") for part in skill_md.relative_to(root).parts):
            continue
        name, description = _parse_skill_metadata(skill_md)
        if curated_only and name not in CURATED_BUILTIN_SKILLS:
            continue
        skills[name] = SkillRecord(
            name=name,
            description=description,
            source=source,
            root=root,
            skill_dir=skill_md.parent,
            skill_md=skill_md,
        )
    return skills


def discover_skills(cwd: str | os.PathLike[str] | None = None) -> list[SkillRecord]:
    merged: dict[str, SkillRecord] = {}
    for record in _scan_root(_builtin_root(), "builtin", curated_only=True).values():
        merged[record.name] = record
    for record in _scan_root(_user_root(), "user").values():
        merged[record.name] = record
    for record in _scan_root(_project_root(cwd), "project").values():
        merged[record.name] = record
    return sorted(merged.values(), key=lambda item: (item.name, item.source))


def discover_skill_commands(cwd: str | os.PathLike[str] | None = None) -> dict[str, dict[str, str]]:
    commands: dict[str, dict[str, str]] = {}
    for record in discover_skills(cwd):
        commands[f"/{record.command_name}"] = {
            "name": record.name,
            "description": record.description or f"Activate the {record.name} skill",
            "source": record.source,
        }
    return commands


def find_skill(name: str, cwd: str | os.PathLike[str] | None = None) -> SkillRecord | None:
    """Return a SkillRecord matching the given name, path, or command name, or None if not found. Supports discovery across builtin, user, and project skill directories; accepts slash-prefixed names, file paths, and normalized names. Falls back to parsing SKILL.md from a provided file path if no discovered skill matches."""
    raw_requested = (name or "").strip()
    if (
        raw_requested.startswith("/")
        and Path(raw_requested).expanduser().exists()
        or raw_requested.startswith(("~", "."))
    ):
        requested = raw_requested
    else:
        requested = raw_requested.lstrip("/")
    if not requested:
        return None
    requested_path = Path(requested).expanduser()
    candidate_paths = [requested_path]
    if not requested_path.is_absolute() and cwd is not None:
        candidate_paths.append((Path(cwd).expanduser() / requested).resolve())
    normalized = requested.lower().replace(" ", "-").replace("_", "-")
    for record in discover_skills(cwd):
        if record.name == requested or record.command_name == normalized:
            return record
        if requested == record.relative_path or requested == str(
            record.skill_dir.relative_to(record.root)
        ):
            return record
        for candidate_path in candidate_paths:
            try:
                resolved = candidate_path.resolve()
            except Exception:
                resolved = candidate_path
            if resolved == record.skill_md.resolve() or resolved == record.skill_dir.resolve():
                return record
    for candidate_path in candidate_paths:
        try:
            resolved = candidate_path.resolve()
        except Exception:
            resolved = candidate_path
        skill_md = resolved / "SKILL.md" if resolved.is_dir() else resolved
        if skill_md.name != "SKILL.md" or not skill_md.is_file():
            continue
        name_value, description = _parse_skill_metadata(skill_md)
        return SkillRecord(
            name=name_value,
            description=description,
            source="path",
            root=skill_md.parent.parent,
            skill_dir=skill_md.parent,
            skill_md=skill_md,
        )
    return None


def load_skill(name: str, cwd: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    record = find_skill(name, cwd)
    if record is None:
        return None
    try:
        content = record.skill_md.read_text(encoding="utf-8")
    except Exception:
        return None

    linked_files: dict[str, list[str]] = {}
    for subdir in ("references", "templates", "scripts", "assets"):
        target = record.skill_dir / subdir
        if not target.exists():
            continue
        files = [
            str(path.relative_to(record.skill_dir))
            for path in sorted(target.rglob("*"))
            if path.is_file()
        ]
        if files:
            linked_files[subdir] = files

    return {
        "name": record.name,
        "description": record.description,
        "source": record.source,
        "path": record.relative_path,
        "content": content,
        "linked_files": linked_files or None,
    }


def load_skill_file(
    name: str, file_path: str, cwd: str | os.PathLike[str] | None = None
) -> dict[str, Any] | None:
    record = find_skill(name, cwd)
    if record is None:
        return None
    target = (record.skill_dir / file_path).resolve()
    try:
        target.relative_to(record.skill_dir.resolve())
    except Exception:
        return None
    if not target.is_file():
        return None
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = f"[Binary file: {target.name}]"
    return {
        "name": record.name,
        "source": record.source,
        "file": file_path,
        "content": content,
    }


def default_workflow_skill(workflow_kind: str) -> str:
    normalized = (workflow_kind or "").strip().lower()
    if normalized in {"prove", "autoprove"}:
        return "lean-proof-loop"
    if normalized in {"formalize", "autoformalize"}:
        return "lean-formalization"
    if normalized == "review":
        return "lean-diagnostics"
    if normalized in {"refactor", "golf"}:
        return "lean-refactor-golf"
    if normalized == "draft":
        return "lean-formalization"
    return "lean-proof-loop"


def build_skill_prompt(name: str, cwd: str | os.PathLike[str] | None = None) -> str:
    """Build a formatted prompt string from a skill, including its content, linked workflow specs from lean_workflow_specs, and supporting file references. Returns empty string if skill not found."""
    payload = load_skill(name, cwd)
    if not payload:
        return ""
    parts = [
        f"[LEANFLOW SKILL: {payload['name']} ({payload['source']})]",
        "",
        str(payload.get("content", "") or "").strip(),
    ]
    spec_records = specs_for_skill(str(payload.get("name", "") or name))
    if spec_records:
        parts.append("")
        parts.append(
            "[Linked workflow specs follow. Treat them as the policy manuals for this skill.]"
        )
        embedded_phases: set[str] = set()
        for record in spec_records:
            spec_content = str(record.content or "").strip()
            if not spec_content:
                continue
            parts.extend(
                [
                    "",
                    f"[{record.kind.upper()} SPEC: {record.spec_id}]",
                    spec_content,
                ]
            )
            # Phase fragments the spec defers to ride along (deduped) —
            # a spec must never point the model at a contract it cannot see.
            for phase_id in record.phases:
                if phase_id in embedded_phases:
                    continue
                fragment = phase_fragment_text(phase_id)
                if fragment:
                    embedded_phases.add(phase_id)
                    parts.extend(["", fragment])
    linked_files = payload.get("linked_files") or {}
    if linked_files:
        parts.append("")
        parts.append("[Supporting files available through the skill system:]")
        for category, files in linked_files.items():
            for item in files:
                parts.append(f"- {category}: {item}")
    return "\n".join(parts).strip()
