"""Document preflight helpers for LeanFlow formalization workflows."""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.utils import atomic_json_write

# The text/LaTeX/PDF extraction layer lives in leanflow_cli.formalization.document_extraction. It is
# re-exported here so every caller and test keeps resolving these names as
# formalization_documents.<name>. document_extraction does not import this module, so
# the re-export introduces no import cycle.
from leanflow_cli.formalization.document_extraction import (  # noqa: F401
    _DEFAULT_LATEX_THEOREM_ENV_KINDS,
    MAX_EXTRACTED_TEXT_CHARS,
    MAX_REFERENCES,
    MAX_SECTIONS,
    MAX_STATEMENT_CHARS,
    MAX_THEOREM_BLOCKS,
    _bounded,
    _clean_tex_statement,
    _extract_braced_commands,
    _extract_latex_option_name,
    _extract_latex_summary,
    _extract_pdf_summary,
    _extract_plaintext_sections,
    _following_latex_proof,
    _latex_theorem_environment_kinds,
    _line_number,
    _line_span,
    _normalize_theorem_kind,
    _run_document_tool,
)
from leanflow_cli.formalization.formalization_markdown import (  # noqa: F401
    MAX_CONTEXT_EXCERPT_CHARS,
    _render_blocks_for_markdown,
    _render_context_markdown,
    _render_sections_for_markdown,
)
from leanflow_cli.formalization.formalization_models import (  # noqa: F401
    FormalizationDocumentContext,
    FormalizationDocumentError,
    _FormalizationDocumentSelection,
)
from leanflow_cli.formalization.formalization_tex_discovery import (  # noqa: F401
    _candidate_paths,
    _collect_tex_project_assets,
    _directory_relative_list,
    _discover_tex_project_entrypoint,
    _entrypoint_score,
    _extract_bibliography_asset_refs,
    _extract_tex_inputs,
    _files_under_by_suffix,
    _included_tex_closure,
    _is_inside_directory,
    _normalize_tex_reference,
    _project_local_existing_path,
    _read_text_lossy,
    _relative_list,
    _relative_to_project,
    _resolve_asset_reference,
    _resolve_local_reference,
    _strip_wrapping_quotes,
    _tex_files_under,
)

SUPPORTED_FORMALIZATION_DOCUMENT_EXTENSIONS = {
    ".tex": "latex",
    ".pdf": "pdf",
}


def _select_formalization_document(
    project_root: str | Path,
    cwd: str | Path,
    workflow_args: str,
) -> _FormalizationDocumentSelection:
    """Parse and resolve a formalization document path (file or directory) from a user request, validating extension and project locality. Returns a _FormalizationDocumentSelection with resolved source path, kind (latex/pdf), and discovery metadata; raises FormalizationDocumentError on invalid input."""
    root = Path(project_root).expanduser().resolve()
    base = Path(cwd).expanduser().resolve()
    raw = _strip_wrapping_quotes(workflow_args)
    if not raw:
        raise FormalizationDocumentError(
            "formalize requires a project-local .tex source, .pdf source, or TeX project directory, "
            "for example `/formalize docs/paper.tex` or `/autoformalize docs/paper`."
        )
    if raw.startswith("-"):
        raise FormalizationDocumentError(
            "formalize requires the document path before options, for example `/formalize docs/paper.tex --prompt section 2`."
        )

    raw_suffix = Path(raw).suffix.lower()
    if raw_suffix == ".lean":
        raise FormalizationDocumentError(
            "formalize now expects a source document (.tex or .pdf) or TeX project directory. Use `/prove` for an existing Lean file "
            "or `/draft` for statement-only skeleton work."
        )
    try:
        requested_path, requested_relative = _project_local_existing_path(root, base, raw)
    except FormalizationDocumentError:
        if raw_suffix and raw_suffix not in SUPPORTED_FORMALIZATION_DOCUMENT_EXTENSIONS:
            raise FormalizationDocumentError(
                "formalize requires a project-local .tex source, .pdf source, or directory containing a TeX project. "
                f"Unsupported document extension: {raw_suffix}"
            ) from None
        if not raw_suffix:
            raise FormalizationDocumentError(
                "formalize requires a project-local .tex source, .pdf source, or directory containing a TeX project, "
                "for example `/formalize docs/paper.tex` or `/autoformalize docs/paper`."
            ) from None
        raise
    if requested_path.is_dir():
        return _discover_tex_project_entrypoint(root, requested_path)

    suffix = requested_path.suffix.lower()
    if suffix not in SUPPORTED_FORMALIZATION_DOCUMENT_EXTENSIONS:
        raise FormalizationDocumentError(
            "formalize requires a project-local .tex source, .pdf source, or directory containing a TeX project. "
            f"Unsupported document extension: {suffix or '[none]'}"
        )
    kind = SUPPORTED_FORMALIZATION_DOCUMENT_EXTENSIONS[suffix]
    return _FormalizationDocumentSelection(
        source_path=requested_path,
        source_relative=requested_relative,
        source_kind=kind,
        request_path=requested_path,
        request_relative=requested_relative,
        request_kind="file",
        discovery_metadata={
            "document_request_kind": "file",
            "document_request_path": str(requested_path),
            "document_request_relative": requested_relative,
            "selected_source_document": str(requested_path),
            "selected_source_document_relative": requested_relative,
        },
    )


def resolve_formalization_document(
    project_root: str | Path,
    cwd: str | Path,
    workflow_args: str,
) -> tuple[Path, str, str]:
    """Resolve and validate the required document path for `/formalize`."""

    selection = _select_formalization_document(project_root, cwd, workflow_args)
    return selection.source_path, selection.source_relative, selection.source_kind


def _safe_name(value: str, default: str = "Formalization") -> str:
    words = re.findall(r"[A-Za-z0-9]+", value or "")
    if not words:
        return default
    name = "".join(word[:1].upper() + word[1:] for word in words)
    if not re.match(r"^[A-Za-z_]", name):
        name = f"{default}{name}"
    return name[:80] or default


def _safe_slug(value: str, default: str = "formalization") -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value or "").strip("-._")
    return slug[:96] or default


def _blueprint_skill_name(target_lean_relative: str) -> str:
    slug = _safe_slug(
        Path(target_lean_relative).with_suffix("").as_posix().replace("/", "-"), "formalization"
    )
    return f"formalization-blueprint-{slug[:72]}"


def _extract_blueprint_source(blueprint_path: Path) -> str:
    try:
        text = blueprint_path.read_text(encoding="utf-8")
    except Exception:
        return ""
    match = re.search(r"^\s*-\s*Source:\s*`?([^`\n]+)`?\s*$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def ensure_formalization_blueprint_skill(
    *,
    project_root: str | Path,
    target_lean_relative: str,
    blueprint_path: str | Path | None = None,
    source_relative: str = "",
) -> Path | None:
    """Generate and register a transient Lean skill that embeds the blueprint and source document metadata for a target .lean file. Writes a SKILL.md file to .leanflow/skills/, extracted from the blueprint and optional source label; returns the skill path or None if blueprint does not exist."""
    root = Path(project_root).expanduser().resolve()
    if not target_lean_relative:
        return None
    target_path = (root / target_lean_relative).resolve()
    resolved_blueprint = (
        Path(blueprint_path).expanduser() if blueprint_path else target_path.parent / "Blueprint.md"
    )
    with contextlib.suppress(Exception):
        resolved_blueprint = resolved_blueprint.resolve()
    if not resolved_blueprint.is_file():
        return None
    try:
        blueprint_relative = str(resolved_blueprint.relative_to(root))
    except Exception:
        blueprint_relative = str(resolved_blueprint)
    source_label = source_relative.strip() or _extract_blueprint_source(resolved_blueprint)
    source_line = f"- Source document: `{source_label}`\n" if source_label else ""
    skill_name = _blueprint_skill_name(target_lean_relative)
    skill_dir = root / ".leanflow" / "skills" / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    content = (
        "---\n"
        f"name: {skill_name}\n"
        f"description: Blueprint and source map for `{target_lean_relative}`.\n"
        "---\n\n"
        "# Formalization Blueprint Skill\n\n"
        f"Before proving declarations in `{target_lean_relative}`, read and use the local formalization blueprint.\n\n"
        f"- Blueprint: `{blueprint_relative}`\n"
        f"{source_line}"
        "- Treat the blueprint as the source map for theorem locators, planned Lean names, dependencies, "
        "statement-fidelity caveats, and prover notes.\n"
        "- If the current proof is unclear, reopen the blueprint first, then the original source document when listed.\n"
        "- Do not change source-backed theorem statements during proving unless a separate statement/source review "
        "explicitly corrected the blueprint and Lean draft.\n"
    )
    skill_path.write_text(content, encoding="utf-8")
    return skill_path


def _default_document_workspace_path(
    project_root: Path, project_label: str, source_path: Path
) -> Path:
    module_name = _safe_name(project_label or project_root.name, "Formalization")
    source_name = _safe_name(source_path.stem, "Document")
    return project_root / module_name / source_name


def _default_target_lean_path(project_root: Path, project_label: str, source_path: Path) -> Path:
    return _default_document_workspace_path(project_root, project_label, source_path) / "Main.lean"


def inspect_formalization_document(
    path: str | Path,
    *,
    project_root: str | Path | None = None,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """Return a flat inspection dict of a document's resolved source path, extracted text/metadata, and discovery details. Calls _select_formalization_document and extracts LaTeX or PDF summary, bounded to context excerpt size."""
    base = Path(cwd or Path.cwd()).expanduser().resolve()
    root = Path(project_root).expanduser().resolve() if project_root else base
    selection = _select_formalization_document(root, base, str(path))
    source = selection.source_path
    relative = selection.source_relative
    kind = selection.source_kind
    summary = _extract_latex_summary(source) if kind == "latex" else _extract_pdf_summary(source)
    summary.update(selection.discovery_metadata)
    summary.update(
        {
            "success": True,
            "source_path": str(source),
            "source_relative": relative,
            "source_kind": kind,
            "document_request_kind": selection.request_kind,
            "document_request_path": str(selection.request_path),
            "document_request_relative": selection.request_relative,
            "text_excerpt": _bounded(
                str(summary.get("extracted_text", "") or ""), MAX_CONTEXT_EXCERPT_CHARS
            ),
        }
    )
    return summary


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    # Crash-atomic so a crash mid-write never truncates extraction state.
    atomic_json_write(path, payload, sort_keys=True, default=_json_default)


def _initial_blueprint(
    source_relative: str, target_lean_relative: str, metadata: Mapping[str, Any]
) -> str:
    """Build a preflight blueprint Markdown template from source relative path and extracted metadata. Lists planner checklist, import plan, theorem inventory, and proof guidance placeholders; truncates to MAX_THEOREM_BLOCKS extracted LaTeX/PDF blocks."""
    blocks = list(metadata.get("theorem_blocks", []) or [])
    lines = [
        f"# Formalization Blueprint: {source_relative}",
        "",
        f"- Source: `{source_relative}`",
        f"- Target Lean entry file: `{target_lean_relative}`",
        "- Status: planner preflight created; replace this with the agent's dependency plan.",
        "",
        "## Planner Checklist",
        "",
        "- [ ] Identify definitions and notation that must exist before theorem statements.",
        "- [ ] Split large source theorems into Lean-sized lemmas.",
        "- [ ] Record source labels/pages/equations for every generated declaration.",
        "- [ ] Check local project and Mathlib names before introducing duplicates.",
        "- [ ] Verify drafted Lean statements match the source document.",
        "- [ ] Run independent statement/source verification review and apply corrections.",
        "- [ ] Attach the complete source proof text when available, or explicitly record why it is unavailable.",
        "- [ ] Record a natural-language proof strategy or source proof pointer for each theorem/lemma.",
        "- [ ] Resolve all construction stubs before proof handoff.",
        "- [ ] Mark stable theorem/lemma/example `sorry` declarations ready for a user-started prove workflow. (Only check after independent review approves every source entry.)",
        "",
        "Replace all `_pending_` entries before drafting Lean. The managed workflow treats this initial",
        "blueprint as a placeholder, not as a completed plan.",
        "",
        "For each theorem or lemma, include proof guidance useful to the prover: the complete source proof",
        "when available, relevant source proof paragraphs, induction variables, reductions, important",
        "previously planned lemmas, and any known statement-fidelity caveats. Lean doc comments should include compact proof notes;",
        "the generated supplemental blueprint skill carries the durable `Blueprint.md` reference.",
        "",
        "## Import Plan",
        "",
        "Direct Lean imports expected in generated Lean files only:",
        "- `Mathlib`",
        "",
        "## Suggested Search Modules",
        "",
        "Non-gating modules or namespaces to search while proving. Do not force these into `.lean` imports unless the prover actually needs them.",
        "- [none yet]",
        "",
        "## Generated File Layout",
        "",
        f"- Aggregator entry file: `{target_lean_relative}`",
        "- Split into `Basic.lean`, `Constructions.lean`, and `Theorems.lean` when declaration count, proof count, or source sections justify it.",
        "",
        "## Source Statement Inventory",
        "",
    ]
    if not blocks:
        lines.append(
            "- No theorem-like LaTeX blocks were detected by preflight; inspect the document manually."
        )
    for block in blocks[:MAX_THEOREM_BLOCKS]:
        label = str(block.get("label", "") or f"line-{block.get('line', '?')}")
        lines.extend(
            [
                f"### {label}",
                "",
                f"- Kind: {block.get('kind', 'statement')}",
                f"- Source locator: `{source_relative}:{block.get('line', '?')}-{block.get('end_line') or block.get('line', '?')}`",
                "- Planned Lean declarations: _pending_",
                f"- Dependencies: {', '.join(block.get('uses', []) or []) or '_pending_'}",
                "- Formal statement review: _pending_",
                "- Source qualifiers: _pending_",
                "- Lean coverage: _pending_",
                "- Scope changes: _pending_",
                "- Statement verification status: _pending_",
                "- Complete source proof: _pending_",
                "- Source proof / prover notes: _pending_",
                f"- Source proof excerpt: {str(block.get('proof', '') or '[none detected by preflight]')}",
                "",
                str(block.get("statement", "") or "_statement pending manual extraction_"),
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _lean_module_for_relative_path(relative: str | Path) -> str:
    path = Path(relative)
    parts = list(path.parts)
    if not parts or parts[-1] == "":
        return ""
    if parts[-1].endswith(".lean"):
        parts[-1] = parts[-1][:-5]
    if any(not re.match(r"^[A-Za-z_][A-Za-z0-9_']*$", part) for part in parts):
        return ""
    return ".".join(parts)


def _ensure_lean_import(path: Path, module: str) -> bool:
    if not module:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    import_line = f"import {module}"
    if not path.exists():
        path.write_text(import_line + "\n", encoding="utf-8")
        return True

    text = path.read_text(encoding="utf-8")
    if re.search(rf"^\s*import\s+{re.escape(module)}\s*$", text, flags=re.MULTILINE):
        return False

    lines = text.splitlines()
    insert_at = 0
    while insert_at < len(lines) and not lines[insert_at].strip():
        insert_at += 1
    while insert_at < len(lines) and lines[insert_at].lstrip().startswith("import "):
        insert_at += 1
    lines.insert(insert_at, import_line)
    if insert_at == 0 and len(lines) > 1 and lines[1].strip():
        lines.insert(1, "")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return True


def _ensure_formalization_import_chain(
    root: Path, target_lean_path: Path, target_lean_relative: str
) -> dict[str, str]:
    target_module = _lean_module_for_relative_path(target_lean_relative)
    if not target_module or "." not in target_module:
        return {}

    modules = target_module.split(".")
    root_module = modules[0]
    root_file = root / f"{root_module}.lean"
    artifacts: dict[str, str] = {
        "target_module": target_module,
        "root_module": root_module,
        "root_module_path": str(root_file),
    }

    if len(modules) >= 3 and target_lean_path.name == "Main.lean":
        parent_module = ".".join(modules[:-1])
        parent_relative = Path(*modules[:-1]).with_suffix(".lean")
        parent_path = root / parent_relative
        _ensure_lean_import(parent_path, target_module)
        _ensure_lean_import(root_file, parent_module)
        artifacts.update(
            {
                "parent_module": parent_module,
                "parent_module_path": str(parent_path),
            }
        )
    else:
        _ensure_lean_import(root_file, target_module)
    return artifacts


def _initial_target_lean(
    _source_relative: str, _blueprint_relative: str, _import_module: str
) -> str:
    return "import Mathlib\n"


def prepare_formalization_document_context(
    *,
    project_root: str | Path,
    cwd: str | Path,
    workflow_args: str,
    project_label: str = "",
) -> FormalizationDocumentContext:
    """Orchestrate the formalization document intake: resolve source, extract metadata, scaffold state directory (.leanflow/workflow-state), initialize target Lean file and blueprint, register the blueprint skill, and return a FormalizationDocumentContext with paths to context markdown, manifest, extracted text, and blueprint. Writes manifests and initializes the Lean import chain."""
    root = Path(project_root).expanduser().resolve()
    base = Path(cwd).expanduser().resolve()
    selection = _select_formalization_document(root, base, workflow_args)
    source_path = selection.source_path
    source_relative = selection.source_relative
    source_kind = selection.source_kind
    metadata = (
        _extract_latex_summary(source_path)
        if source_kind == "latex"
        else _extract_pdf_summary(source_path)
    )
    metadata.update(selection.discovery_metadata)
    metadata["text_excerpt"] = _bounded(
        str(metadata.get("extracted_text", "") or ""), MAX_CONTEXT_EXCERPT_CHARS
    )

    slug = _safe_slug(Path(source_relative).with_suffix("").as_posix().replace("/", "-"))
    state_dir = root / ".leanflow" / "workflow-state" / "formalization" / slug
    target_lean_path = _default_target_lean_path(root, project_label, source_path)
    module_name = _safe_name(project_label or root.name, "Formalization")
    import_module = "Mathlib"
    target_lean_relative = (
        _relative_to_project(target_lean_path, root)
        if target_lean_path.exists()
        else str(target_lean_path.relative_to(root))
    )
    context_path = state_dir / "context.md"
    manifest_path = state_dir / "manifest.json"
    extracted_text_path = state_dir / "extracted.txt"
    blueprint_path = target_lean_path.parent / "Blueprint.md"
    blueprint_skill_path = (
        root / ".leanflow" / "skills" / _blueprint_skill_name(target_lean_relative) / "SKILL.md"
    )

    metadata.update(
        {
            "source_path": str(source_path),
            "source_relative": source_relative,
            "source_kind": source_kind,
            "document_request_kind": selection.request_kind,
            "document_request_path": str(selection.request_path),
            "document_request_relative": selection.request_relative,
            "selected_source_document": str(source_path),
            "selected_source_document_relative": source_relative,
            "target_lean_path": str(target_lean_path),
            "target_lean_relative": target_lean_relative,
            "context_path": str(context_path),
            "manifest_path": str(manifest_path),
            "extracted_text_path": str(extracted_text_path),
            "blueprint_path": str(blueprint_path),
            "blueprint_skill_path": str(blueprint_skill_path),
            "created_at_unix": int(time.time()),
        }
    )

    state_dir.mkdir(parents=True, exist_ok=True)
    target_lean_path.parent.mkdir(parents=True, exist_ok=True)
    extracted_text_path.write_text(str(metadata.get("extracted_text", "") or ""), encoding="utf-8")
    _write_json(manifest_path, metadata)
    if not blueprint_path.exists():
        blueprint_path.write_text(
            _initial_blueprint(source_relative, target_lean_relative, metadata), encoding="utf-8"
        )
    generated_skill_path = ensure_formalization_blueprint_skill(
        project_root=root,
        target_lean_relative=target_lean_relative,
        blueprint_path=blueprint_path,
        source_relative=source_relative,
    )
    if generated_skill_path is not None:
        blueprint_skill_path = generated_skill_path
    if not target_lean_path.exists():
        try:
            blueprint_relative = str(blueprint_path.resolve().relative_to(root.resolve()))
        except Exception:
            blueprint_relative = str(blueprint_path)
        target_lean_path.write_text(
            _initial_target_lean(source_relative, blueprint_relative, import_module),
            encoding="utf-8",
        )
    import_chain = _ensure_formalization_import_chain(root, target_lean_path, target_lean_relative)
    metadata.update({f"formalization_{key}": value for key, value in import_chain.items()})
    context_path.write_text(
        _render_context_markdown(
            source_relative=source_relative,
            source_kind=source_kind,
            target_lean_relative=target_lean_relative,
            blueprint_path=blueprint_path,
            extracted_text_path=extracted_text_path,
            manifest_path=manifest_path,
            metadata=metadata,
        ),
        encoding="utf-8",
    )

    return FormalizationDocumentContext(
        source_path=source_path,
        source_relative=source_relative,
        source_kind=source_kind,
        context_path=context_path,
        manifest_path=manifest_path,
        extracted_text_path=extracted_text_path,
        blueprint_path=blueprint_path,
        blueprint_skill_path=blueprint_skill_path,
        target_lean_path=target_lean_path,
        target_lean_relative=target_lean_relative,
        metadata=dict(metadata),
    )
