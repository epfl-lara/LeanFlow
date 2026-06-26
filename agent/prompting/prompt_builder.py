"""System prompt assembly -- identity, platform hints, skills index, context files.

All functions are stateless. AIAgent._build_system_prompt() calls these to
assemble pieces, then combines them with memory and ephemeral prompts.
"""

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context file scanning — detect prompt injection in AGENTS.md, .cursorrules,
# SOUL.md before they get injected into the system prompt.
# ---------------------------------------------------------------------------

_CONTEXT_THREAT_PATTERNS = [
    (r"ignore\s+(previous|all|above|prior)\s+instructions", "prompt_injection"),
    (r"do\s+not\s+tell\s+the\s+user", "deception_hide"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    (r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)", "disregard_rules"),
    (
        r"act\s+as\s+(if|though)\s+you\s+(have\s+no|don\'t\s+have)\s+(restrictions|limits|rules)",
        "bypass_restrictions",
    ),
    (r"<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->", "html_comment_injection"),
    (r'<\s*div\s+style\s*=\s*["\'].*display\s*:\s*none', "hidden_div"),
    (r"translate\s+.*\s+into\s+.*\s+and\s+(execute|run|eval)", "translate_execute"),
    (r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl"),
    (r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass)", "read_secrets"),
]

_CONTEXT_INVISIBLE_CHARS = {
    "\u200b",
    "\u200c",
    "\u200d",
    "\u2060",
    "\ufeff",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
}


def _scan_context_content(content: str, filename: str) -> str:
    """Scan context file content for injection. Returns sanitized content."""
    findings = []

    # Check invisible unicode
    for char in _CONTEXT_INVISIBLE_CHARS:
        if char in content:
            findings.append(f"invisible unicode U+{ord(char):04X}")

    # Check threat patterns
    for pattern, pid in _CONTEXT_THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            findings.append(pid)

    if findings:
        logger.warning("Context file %s blocked: %s", filename, ", ".join(findings))
        return f"[BLOCKED: {filename} contained potential prompt injection ({', '.join(findings)}). Content not loaded.]"

    return content


# =========================================================================
# Constants
# =========================================================================

DEFAULT_AGENT_IDENTITY = (
    "You are LeanFlow, a direct and capable AI assistant for Lean and AI-for-math workflows. "
    "Focus on the user's task instead of introducing yourself. "
    "Do not volunteer company history, model lineage, product background, or "
    "a general self-summary unless the user explicitly asks. When asked who you "
    "are, answer briefly and return to the work. "
    "When the user is trying to use the shell itself or seems unsure how to "
    "start, give them the lowest-friction Lean path first: point them to /project "
    "to initialize the repo, then /prove, /autoprove, /formalize, or /autoformalize "
    "with a concrete file or goal. "
    "Keep the workflow centered on verified Lean output: successful builds, "
    "clean diagnostics, no open goals, and no `sorry`. "
    "User-approved swarm mode exists only when the workflow was launched with "
    "`--agents N`; otherwise keep the run single-agent. "
    "Be clear, admit uncertainty when appropriate, and prioritize being "
    "genuinely useful over being verbose unless otherwise directed below. "
    "Be targeted and efficient in your exploration and investigations."
)

MEMORY_GUIDANCE = (
    "You have persistent memory across sessions. Save durable facts using the memory "
    "tool: user preferences, environment details, tool quirks, and stable conventions. "
    "Memory is injected into every turn, so keep it compact and focused on facts that "
    "will still matter later.\n"
    "Prioritize what reduces future user steering — the most valuable memory is one "
    "that prevents the user from having to correct or remind you again. "
    "User preferences and recurring corrections matter more than procedural task details.\n"
    "Do NOT save task progress, session outcomes, completed-work logs, or temporary TODO "
    "state to memory; use session_search to recall those from past transcripts. "
    "If you've discovered a new way to do something, solved a problem that could be "
    "necessary later, save it as a skill with the skill tool."
)

SESSION_SEARCH_GUIDANCE = (
    "When the user references something from a past conversation or you suspect "
    "relevant cross-session context exists, use session_search to recall it before "
    "asking them to repeat themselves."
)

SKILLS_GUIDANCE = (
    "Treat the LeanFlow skill system as a Lean workflow aid, not a marketplace. "
    "If the task matches a curated Lean skill or a project-local override, load it and follow it. "
    "Prefer project-local overlays when they exist because they capture repo-specific solver guidance."
)

PLATFORM_HINTS = {
    "cli": (
        "You are a CLI AI Agent. Try not to use markdown but simple text "
        "renderable inside a terminal."
    ),
}

CONTEXT_FILE_MAX_CHARS = 20_000
CONTEXT_TRUNCATE_HEAD_RATIO = 0.7
CONTEXT_TRUNCATE_TAIL_RATIO = 0.2


# =========================================================================
# Skills index
# =========================================================================


def _parse_skill_file(skill_file: Path) -> tuple[bool, dict, str]:
    """Read a SKILL.md once and return platform compatibility, frontmatter, and description.

    Returns (is_compatible, frontmatter, description). On any error, returns
    (True, {}, "") to err on the side of showing the skill.
    """
    try:
        from tools.implementations.skills_tool import _parse_frontmatter, skill_matches_platform

        raw = skill_file.read_text(encoding="utf-8")[:2000]
        frontmatter, _ = _parse_frontmatter(raw)

        if not skill_matches_platform(frontmatter):
            return False, {}, ""

        desc = ""
        raw_desc = frontmatter.get("description", "")
        if raw_desc:
            desc = str(raw_desc).strip().strip("'\"")
            if len(desc) > 60:
                desc = desc[:57] + "..."

        return True, frontmatter, desc
    except Exception as e:
        logger.debug("Failed to parse skill file %s: %s", skill_file, e)
        return True, {}, ""


def _read_skill_conditions(skill_file: Path) -> dict:
    """Extract conditional activation fields from SKILL.md frontmatter."""
    try:
        from tools.implementations.skills_tool import _parse_frontmatter

        raw = skill_file.read_text(encoding="utf-8")[:2000]
        frontmatter, _ = _parse_frontmatter(raw)
        meta = frontmatter.get("metadata", {}).get("leanflow", {})
        return {
            "fallback_for_toolsets": meta.get("fallback_for_toolsets", []),
            "requires_toolsets": meta.get("requires_toolsets", []),
            "fallback_for_tools": meta.get("fallback_for_tools", []),
            "requires_tools": meta.get("requires_tools", []),
        }
    except Exception as e:
        logger.debug("Failed to read skill conditions from %s: %s", skill_file, e)
        return {}


def _skill_should_show(
    conditions: dict,
    available_tools: "set[str] | None",
    available_toolsets: "set[str] | None",
) -> bool:
    """Return False if the skill's conditional activation rules exclude it."""
    if available_tools is None and available_toolsets is None:
        return True  # No filtering info — show everything (backward compat)

    at = available_tools or set()
    ats = available_toolsets or set()

    # fallback_for: hide when the primary tool/toolset IS available
    for ts in conditions.get("fallback_for_toolsets", []):
        if ts in ats:
            return False
    for t in conditions.get("fallback_for_tools", []):
        if t in at:
            return False

    # requires: hide when a required tool/toolset is NOT available
    for ts in conditions.get("requires_toolsets", []):
        if ts not in ats:
            return False
    for t in conditions.get("requires_tools", []):
        if t not in at:
            return False

    return True


def build_skills_system_prompt(
    available_tools: "set[str] | None" = None,
    available_toolsets: "set[str] | None" = None,
) -> str:
    """Build a compact LeanFlow skill index for the system prompt."""
    try:
        from leanflow_cli.lean.lean_workflow_specs import specs_for_skill
        from leanflow_cli.runtime.skill_core import discover_skills

        skills = discover_skills()
    except Exception as exc:
        logger.debug("Failed to discover LeanFlow skills: %s", exc)
        return ""

    if not skills:
        return ""

    skills_by_source: dict[str, list[tuple[str, str]]] = {}
    for skill in skills:
        skills_by_source.setdefault(skill.source, []).append((skill.name, skill.description))

    source_labels = {
        "project": "project overrides",
        "user": "user overlays",
        "builtin": "builtin core",
    }
    index_lines: list[str] = []
    for source in ("project", "user", "builtin"):
        entries = skills_by_source.get(source) or []
        if not entries:
            continue
        index_lines.append(f"  {source_labels[source]}:")
        seen: set[str] = set()
        for name, desc in sorted(entries, key=lambda item: item[0]):
            if name in seen:
                continue
            seen.add(name)
            try:
                spec_records = specs_for_skill(name)
            except Exception:
                spec_records = []
            spec_suffix = ""
            if spec_records:
                spec_suffix = " [" + ", ".join(record.spec_id for record in spec_records) + "]"
            if desc:
                index_lines.append(f"    - {name}{spec_suffix}: {desc}")
            else:
                index_lines.append(f"    - {name}{spec_suffix}")

    return (
        "## LeanFlow Skills\n"
        "Before replying, scan the skills below. Load a skill with `skill_view(name)` when it clearly matches the task. "
        "Prefer Lean workflow skills and any project-local override over the builtin default. "
        "When a skill exposes native workflow specs, treat those specs as the operational contract.\n"
        "\n"
        "<available_skills>\n" + "\n".join(index_lines) + "\n</available_skills>\n"
        "\n"
        "If none match, continue normally without loading a skill."
    )


# =========================================================================
# Context files (SOUL.md, AGENTS.md, .cursorrules)
# =========================================================================


def _truncate_content(content: str, filename: str, max_chars: int = CONTEXT_FILE_MAX_CHARS) -> str:
    """Head/tail truncation with a marker in the middle."""
    if len(content) <= max_chars:
        return content
    head_chars = int(max_chars * CONTEXT_TRUNCATE_HEAD_RATIO)
    tail_chars = int(max_chars * CONTEXT_TRUNCATE_TAIL_RATIO)
    head = content[:head_chars]
    tail = content[-tail_chars:]
    marker = f"\n\n[...truncated {filename}: kept {head_chars}+{tail_chars} of {len(content)} chars. Use file tools to read the full file.]\n\n"
    return head + marker + tail


def build_context_files_prompt(cwd: str | None = None) -> str:
    """Discover and load context files for the system prompt.

    Discovery: AGENTS.md (recursive), .cursorrules / .cursor/rules/*.mdc,
    and SOUL.md from LEANFLOW_HOME only. Each capped at 20,000 chars.
    """
    if cwd is None:
        cwd = os.getcwd()

    cwd_path = Path(cwd).resolve()
    sections = []

    # AGENTS.md (hierarchical, recursive)
    top_level_agents = None
    for name in ["AGENTS.md", "agents.md"]:
        candidate = cwd_path / name
        if candidate.exists():
            top_level_agents = candidate
            break

    if top_level_agents:
        agents_files = []
        for root, dirs, files in os.walk(cwd_path):
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")
                and d not in ("node_modules", "__pycache__", "venv", ".venv")
            ]
            for f in files:
                if f.lower() == "agents.md":
                    agents_files.append(Path(root) / f)
        agents_files.sort(key=lambda p: len(p.parts))

        total_agents_content = ""
        for agents_path in agents_files:
            try:
                content = agents_path.read_text(encoding="utf-8").strip()
                if content:
                    rel_path = agents_path.relative_to(cwd_path)
                    content = _scan_context_content(content, str(rel_path))
                    total_agents_content += f"## {rel_path}\n\n{content}\n\n"
            except Exception as e:
                logger.debug("Could not read %s: %s", agents_path, e)

        if total_agents_content:
            total_agents_content = _truncate_content(total_agents_content, "AGENTS.md")
            sections.append(total_agents_content)

    # .cursorrules
    cursorrules_content = ""
    cursorrules_file = cwd_path / ".cursorrules"
    if cursorrules_file.exists():
        try:
            content = cursorrules_file.read_text(encoding="utf-8").strip()
            if content:
                content = _scan_context_content(content, ".cursorrules")
                cursorrules_content += f"## .cursorrules\n\n{content}\n\n"
        except Exception as e:
            logger.debug("Could not read .cursorrules: %s", e)

    cursor_rules_dir = cwd_path / ".cursor" / "rules"
    if cursor_rules_dir.exists() and cursor_rules_dir.is_dir():
        mdc_files = sorted(cursor_rules_dir.glob("*.mdc"))
        for mdc_file in mdc_files:
            try:
                content = mdc_file.read_text(encoding="utf-8").strip()
                if content:
                    content = _scan_context_content(content, f".cursor/rules/{mdc_file.name}")
                    cursorrules_content += f"## .cursor/rules/{mdc_file.name}\n\n{content}\n\n"
            except Exception as e:
                logger.debug("Could not read %s: %s", mdc_file, e)

    if cursorrules_content:
        cursorrules_content = _truncate_content(cursorrules_content, ".cursorrules")
        sections.append(cursorrules_content)

    # SOUL.md from LEANFLOW_HOME only
    try:
        from leanflow_cli.config import ensure_leanflow_home

        ensure_leanflow_home()
    except Exception as e:
        logger.debug("Could not ensure LEANFLOW_HOME before loading SOUL.md: %s", e)

    soul_path = Path(os.getenv("LEANFLOW_HOME", Path.home() / ".leanflow")) / "SOUL.md"
    if soul_path.exists():
        try:
            content = soul_path.read_text(encoding="utf-8").strip()
            if content:
                content = _scan_context_content(content, "SOUL.md")
                content = _truncate_content(content, "SOUL.md")
                sections.append(content)
        except Exception as e:
            logger.debug("Could not read SOUL.md from %s: %s", soul_path, e)

    if not sections:
        return ""
    return (
        "# Project Context\n\nThe following project context files have been loaded and should be followed:\n\n"
        + "\n".join(sections)
    )
