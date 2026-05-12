"""Document preflight helpers for EPFLemma formalization workflows."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


SUPPORTED_FORMALIZATION_DOCUMENT_EXTENSIONS = {
    ".tex": "latex",
    ".pdf": "pdf",
}

MAX_EXTRACTED_TEXT_CHARS = 120_000
MAX_CONTEXT_EXCERPT_CHARS = 24_000
MAX_STATEMENT_CHARS = 1_600
MAX_THEOREM_BLOCKS = 80
MAX_SECTIONS = 80
MAX_REFERENCES = 80
TEX_PROJECT_SKIPPED_DIRS = {
    ".epflemma",
    ".git",
    ".lake",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}


class FormalizationDocumentError(ValueError):
    """Raised when a formalization document request is invalid."""


@dataclass(frozen=True)
class FormalizationDocumentContext:
    source_path: Path
    source_relative: str
    source_kind: str
    context_path: Path
    manifest_path: Path
    extracted_text_path: Path
    blueprint_path: Path
    blueprint_skill_path: Path
    target_lean_path: Path
    target_lean_relative: str
    metadata: dict[str, Any]

    def to_env(self) -> dict[str, str]:
        return {
            "EPFLEMMA_FORMALIZATION_DOCUMENT": str(self.source_path),
            "OPENGAUSS_FORMALIZATION_DOCUMENT": str(self.source_path),
            "EPFLEMMA_FORMALIZATION_DOCUMENT_RELATIVE": self.source_relative,
            "OPENGAUSS_FORMALIZATION_DOCUMENT_RELATIVE": self.source_relative,
            "EPFLEMMA_FORMALIZATION_REQUEST_KIND": str(self.metadata.get("document_request_kind", "file") or "file"),
            "OPENGAUSS_FORMALIZATION_REQUEST_KIND": str(self.metadata.get("document_request_kind", "file") or "file"),
            "EPFLEMMA_FORMALIZATION_REQUEST_RELATIVE": str(
                self.metadata.get("document_request_relative", self.source_relative) or self.source_relative
            ),
            "OPENGAUSS_FORMALIZATION_REQUEST_RELATIVE": str(
                self.metadata.get("document_request_relative", self.source_relative) or self.source_relative
            ),
            "EPFLEMMA_FORMALIZATION_SELECTED_SOURCE": self.source_relative,
            "OPENGAUSS_FORMALIZATION_SELECTED_SOURCE": self.source_relative,
            "EPFLEMMA_FORMALIZATION_DOCUMENT_KIND": self.source_kind,
            "OPENGAUSS_FORMALIZATION_DOCUMENT_KIND": self.source_kind,
            "EPFLEMMA_FORMALIZATION_CONTEXT": str(self.context_path),
            "OPENGAUSS_FORMALIZATION_CONTEXT": str(self.context_path),
            "EPFLEMMA_WORKFLOW_CONTEXT": str(self.context_path),
            "GAUSS_AUTOFORMALIZE_CONTEXT": str(self.context_path),
            "EPFLEMMA_FORMALIZATION_MANIFEST": str(self.manifest_path),
            "OPENGAUSS_FORMALIZATION_MANIFEST": str(self.manifest_path),
            "EPFLEMMA_FORMALIZATION_BLUEPRINT": str(self.blueprint_path),
            "OPENGAUSS_FORMALIZATION_BLUEPRINT": str(self.blueprint_path),
            "EPFLEMMA_FORMALIZATION_BLUEPRINT_SKILL": str(self.blueprint_skill_path),
            "OPENGAUSS_FORMALIZATION_BLUEPRINT_SKILL": str(self.blueprint_skill_path),
            "EPFLEMMA_FORMALIZATION_EXTRACTED_TEXT": str(self.extracted_text_path),
            "OPENGAUSS_FORMALIZATION_EXTRACTED_TEXT": str(self.extracted_text_path),
            "EPFLEMMA_FORMALIZATION_TARGET_FILE": self.target_lean_relative,
            "OPENGAUSS_FORMALIZATION_TARGET_FILE": self.target_lean_relative,
        }


@dataclass(frozen=True)
class _FormalizationDocumentSelection:
    source_path: Path
    source_relative: str
    source_kind: str
    request_path: Path
    request_relative: str
    request_kind: str
    discovery_metadata: dict[str, Any]


def _bounded(text: str, limit: int) -> str:
    raw = str(text or "")
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 44)].rstrip() + "\n\n[truncated by EPFLemma document preflight]"


def _strip_wrapping_quotes(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1].strip()
    return text


def _relative_to_project(path: Path, project_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_root.resolve()))
    except Exception as exc:
        raise FormalizationDocumentError(
            f"formalization document must be inside the EPFLemma project: {path}"
        ) from exc


def _candidate_paths(project_root: Path, cwd: Path, raw_path: str) -> list[Path]:
    raw = _strip_wrapping_quotes(raw_path)
    candidate = Path(raw).expanduser()
    candidates: list[Path] = [candidate] if candidate.is_absolute() else [cwd / raw, project_root / raw]
    project_name = project_root.name
    for prefix in (f"./{project_name}/", f"{project_name}/"):
        if raw.startswith(prefix):
            trimmed = raw[len(prefix):]
            candidates.extend([cwd / trimmed, project_root / trimmed])
            break
    deduped: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        key = str(item)
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def _is_inside_directory(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except Exception:
        return False


def _project_local_existing_path(project_root: Path, cwd: Path, raw_path: str) -> tuple[Path, str]:
    raw = _strip_wrapping_quotes(raw_path)
    for candidate in _candidate_paths(project_root, cwd, raw):
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if not resolved.exists():
            continue
        relative = _relative_to_project(resolved, project_root)
        return resolved, relative
    raise FormalizationDocumentError(f"formalization document not found: {raw}")


def _tex_files_under(directory: Path) -> list[Path]:
    files: list[Path] = []
    for path in directory.rglob("*.tex"):
        if any(
            part in TEX_PROJECT_SKIPPED_DIRS or part.startswith(".")
            for part in path.relative_to(directory).parts
        ):
            continue
        if path.is_file():
            files.append(path.resolve())
    return sorted(files, key=lambda item: item.relative_to(directory).as_posix().lower())


def _read_text_lossy(path: Path, limit: int = 500_000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return text[:limit]


def _normalize_tex_reference(value: str, default_suffix: str = ".tex") -> str:
    raw = str(value or "").strip().strip("{}").strip()
    raw = raw.split("%", 1)[0].strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        return ""
    path = Path(raw)
    if not path.suffix and default_suffix:
        raw = f"{raw}{default_suffix}"
    return raw


def _extract_tex_inputs(text: str) -> list[str]:
    values: list[str] = []
    command_pattern = re.compile(
        r"\\(?P<command>input|include|subfile|includeonly)\s*(?:\{(?P<braced>[^{}]+)\}|(?P<plain>[^\s{}]+))",
        re.IGNORECASE,
    )
    for match in command_pattern.finditer(text or ""):
        raw_value = match.group("braced") or match.group("plain") or ""
        for part in raw_value.split(","):
            normalized = _normalize_tex_reference(part)
            if normalized and normalized not in values:
                values.append(normalized)
    return values


def _resolve_local_reference(base_file: Path, project_directory: Path, reference: str) -> Path | None:
    raw = _normalize_tex_reference(reference, default_suffix="")
    if not raw:
        return None
    candidates = [base_file.parent / raw, project_directory / raw]
    if Path(raw).suffix == "":
        candidates.extend([base_file.parent / f"{raw}.tex", project_directory / f"{raw}.tex"])
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if resolved.is_file() and _is_inside_directory(resolved, project_directory):
            return resolved
    return None


def _included_tex_closure(entrypoint: Path, project_directory: Path) -> tuple[list[Path], list[str]]:
    included: list[Path] = []
    missing: list[str] = []
    seen: set[Path] = {entrypoint.resolve()}
    queue: list[Path] = [entrypoint.resolve()]
    while queue:
        current = queue.pop(0)
        text = _read_text_lossy(current)
        for reference in _extract_tex_inputs(text):
            resolved = _resolve_local_reference(current, project_directory, reference)
            if resolved is None:
                if reference not in missing:
                    missing.append(reference)
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            included.append(resolved)
            queue.append(resolved)
    return included, missing


def _entrypoint_score(path: Path, directory: Path, included_by_other_tex: set[Path]) -> int:
    text = _read_text_lossy(path)
    lower_name = path.name.lower()
    score = 0
    if path.resolve() not in included_by_other_tex:
        score += 80
    else:
        score -= 40
    if re.search(r"\\documentclass(?:\[[^\]]*\])?\{", text):
        score += 120
    if re.search(r"\\begin\{document\}", text):
        score += 80
    if re.search(r"\\bye\b", text):
        score += 70
    if re.search(r"\\(?:title|centerline)\b", text):
        score += 25
    if re.search(r"\\(?:begin\{(?:theorem|lemma|proposition|corollary|definition|defn)\}|profess\{)", text):
        score += 25
    if re.search(r"\\bibliography\{", text):
        score += 10
    if lower_name in {"main.tex", "paper.tex", "article.tex", "root.tex", "index.tex"}:
        score += 35
    if path.parent.resolve() == directory.resolve():
        score += 8
    if re.search(r"(?:preamble|macros|defs|commands|setup)", lower_name):
        score -= 50
    return score


def _relative_list(paths: list[Path], root: Path) -> list[str]:
    values: list[str] = []
    for path in paths:
        try:
            value = str(path.resolve().relative_to(root.resolve()))
        except Exception:
            value = str(path)
        if value not in values:
            values.append(value)
    return values


def _directory_relative_list(paths: list[Path], directory: Path) -> list[str]:
    values: list[str] = []
    for path in paths:
        try:
            value = str(path.resolve().relative_to(directory.resolve()))
        except Exception:
            value = str(path)
        if value not in values:
            values.append(value)
    return values


def _extract_bibliography_asset_refs(text: str) -> tuple[list[str], list[str]]:
    bibliography: list[str] = []
    assets: list[str] = []
    for command in ("bibliography", "addbibresource"):
        for value in _extract_braced_commands(text, command):
            for part in value.split(","):
                normalized = _normalize_tex_reference(part, ".bib")
                if normalized and normalized not in bibliography:
                    bibliography.append(normalized)
    for value in _extract_braced_commands(text, "bibliographystyle"):
        normalized = _normalize_tex_reference(value, ".bst")
        if normalized and normalized not in assets:
            assets.append(normalized)
    graphics_pattern = re.compile(
        r"\\(?:includegraphics|epsfig)\s*(?:\[[^\]]*\])?\s*\{(?P<path>[^{}]+)\}",
        re.IGNORECASE,
    )
    for match in graphics_pattern.finditer(text or ""):
        normalized = _normalize_tex_reference(match.group("path"), "")
        if normalized and normalized not in assets:
            assets.append(normalized)
    return bibliography, assets


def _resolve_asset_reference(
    base_file: Path,
    directory: Path,
    reference: str,
    suffixes: tuple[str, ...] = (),
) -> list[Path]:
    raw = _normalize_tex_reference(reference, "")
    if not raw:
        return []
    candidates = [base_file.parent / raw, directory / raw]
    if not Path(raw).suffix:
        for suffix in suffixes:
            candidates.extend([base_file.parent / f"{raw}{suffix}", directory / f"{raw}{suffix}"])
    resolved: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            path = candidate.resolve()
        except Exception:
            continue
        if path in seen or not path.is_file() or not _is_inside_directory(path, directory):
            continue
        seen.add(path)
        resolved.append(path)
    return resolved


def _collect_tex_project_assets(entrypoint: Path, directory: Path, included_tex: list[Path]) -> tuple[list[Path], list[Path]]:
    bibliography_files: list[Path] = []
    local_assets: list[Path] = []
    tex_sources = [entrypoint, *included_tex]
    for source in tex_sources:
        text = _read_text_lossy(source)
        bibliography_refs, asset_refs = _extract_bibliography_asset_refs(text)
        for reference in bibliography_refs:
            matches = _resolve_asset_reference(source, directory, reference, (".bib", ".bbl"))
            stem = str(Path(reference).with_suffix(""))
            matches.extend(_resolve_asset_reference(source, directory, f"{stem}.bbl", ()))
            for match in matches:
                if match not in bibliography_files:
                    bibliography_files.append(match)
        for reference in asset_refs:
            for match in _resolve_asset_reference(source, directory, reference, (".bst", ".png", ".jpg", ".jpeg", ".pdf", ".eps", ".svg")):
                if match not in local_assets:
                    local_assets.append(match)
    for sidecar in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
        if not sidecar.is_file() or sidecar.suffix.lower() in {".tex", ".pdf"}:
            continue
        if sidecar.suffix.lower() in {".bib", ".bbl"}:
            if sidecar.resolve() not in bibliography_files:
                bibliography_files.append(sidecar.resolve())
        elif sidecar.resolve() not in local_assets:
            local_assets.append(sidecar.resolve())
    return bibliography_files, local_assets


def _discover_tex_project_entrypoint(project_root: Path, directory: Path) -> _FormalizationDocumentSelection:
    tex_files = _tex_files_under(directory)
    if not tex_files:
        raise FormalizationDocumentError(
            "formalize directory input requires at least one project-local .tex file. "
            f"No .tex files were found under {_relative_to_project(directory, project_root)}."
        )

    included_by_other_tex: set[Path] = set()
    for tex_file in tex_files:
        for reference in _extract_tex_inputs(_read_text_lossy(tex_file)):
            resolved = _resolve_local_reference(tex_file, directory, reference)
            if resolved is not None and resolved != tex_file.resolve():
                included_by_other_tex.add(resolved)

    scored = sorted(
        ((tex_file, _entrypoint_score(tex_file, directory, included_by_other_tex)) for tex_file in tex_files),
        key=lambda item: (-item[1], item[0].relative_to(directory).as_posix().lower()),
    )
    best_path, best_score = scored[0]
    tied = [path for path, score in scored if score == best_score]
    if len(tied) > 1:
        choices = ", ".join(_relative_to_project(path, project_root) for path in tied[:8])
        raise FormalizationDocumentError(
            "formalize directory input has multiple possible TeX entrypoints. "
            f"Pass one explicitly instead: {choices}"
        )

    included_tex, missing_includes = _included_tex_closure(best_path, directory)
    bibliography_files, local_assets = _collect_tex_project_assets(best_path, directory, included_tex)
    source_relative = _relative_to_project(best_path, project_root)
    directory_relative = _relative_to_project(directory, project_root)
    metadata = {
        "document_request_kind": "directory",
        "document_request_path": str(directory),
        "document_request_relative": directory_relative,
        "tex_project_directory": str(directory),
        "tex_project_directory_relative": directory_relative,
        "selected_source_document": str(best_path),
        "selected_source_document_relative": source_relative,
        "tex_project_entrypoint": source_relative,
        "tex_project_entrypoint_score": best_score,
        "tex_project_files": _relative_list(tex_files, project_root),
        "tex_project_files_relative_to_directory": _directory_relative_list(tex_files, directory),
        "tex_project_included_tex_files": _relative_list(included_tex, project_root),
        "tex_project_included_tex_files_relative_to_directory": _directory_relative_list(included_tex, directory),
        "tex_project_missing_includes": missing_includes,
        "tex_project_bibliography_files": _relative_list(bibliography_files, project_root),
        "tex_project_local_asset_files": _relative_list(local_assets, project_root),
        "tex_project_discovery_summary": (
            f"Selected `{source_relative}` from `{directory_relative}`; "
            f"{len(included_tex)} included .tex file(s), {len(bibliography_files)} bibliography file(s), "
            f"{len(local_assets)} local asset file(s)."
        ),
    }
    return _FormalizationDocumentSelection(
        source_path=best_path,
        source_relative=source_relative,
        source_kind="latex",
        request_path=directory,
        request_relative=directory_relative,
        request_kind="directory",
        discovery_metadata=metadata,
    )


def _select_formalization_document(
    project_root: str | Path,
    cwd: str | Path,
    workflow_args: str,
) -> _FormalizationDocumentSelection:
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
    slug = _safe_slug(Path(target_lean_relative).with_suffix("").as_posix().replace("/", "-"), "formalization")
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
    root = Path(project_root).expanduser().resolve()
    if not target_lean_relative:
        return None
    target_path = (root / target_lean_relative).resolve()
    resolved_blueprint = Path(blueprint_path).expanduser() if blueprint_path else target_path.parent / "Blueprint.md"
    try:
        resolved_blueprint = resolved_blueprint.resolve()
    except Exception:
        pass
    if not resolved_blueprint.is_file():
        return None
    try:
        blueprint_relative = str(resolved_blueprint.relative_to(root))
    except Exception:
        blueprint_relative = str(resolved_blueprint)
    source_label = source_relative.strip() or _extract_blueprint_source(resolved_blueprint)
    source_line = f"- Source document: `{source_label}`\n" if source_label else ""
    skill_name = _blueprint_skill_name(target_lean_relative)
    skill_dir = root / ".epflemma" / "skills" / skill_name
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


def _default_document_workspace_path(project_root: Path, project_label: str, source_path: Path) -> Path:
    module_name = _safe_name(project_label or project_root.name, "Formalization")
    source_name = _safe_name(source_path.stem, "Document")
    return project_root / module_name / source_name


def _default_target_lean_path(project_root: Path, project_label: str, source_path: Path) -> Path:
    return _default_document_workspace_path(project_root, project_label, source_path) / "Main.lean"


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


def _line_span(text: str, start: int, end: int) -> tuple[int, int]:
    return _line_number(text, start), _line_number(text, max(start, end))


def _clean_tex_statement(value: str) -> str:
    text = re.sub(r"\\(?:label|lean|uses|proves|discussion)\{[^{}]*\}", " ", value or "")
    text = re.sub(r"\\(?:leanok|notready|mathlibok)\b", " ", text)
    text = re.sub(r"%[^\n]*", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_braced_commands(text: str, command: str) -> list[str]:
    pattern = re.compile(rf"\\{re.escape(command)}\{{([^{{}}]+)\}}")
    values: list[str] = []
    for match in pattern.finditer(text or ""):
        for value in match.group(1).split(","):
            item = value.strip()
            if item and item not in values:
                values.append(item)
    return values


def _extract_latex_summary(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    theorem_pattern = re.compile(
        r"\\begin\{(?P<env>theorem|lemma|proposition|corollary|definition|defn|conjecture|example|remark)\}"
        r"(?P<option>\[[^\]]*\])?"
        r"(?P<body>.*?)"
        r"\\end\{(?P=env)\}",
        re.IGNORECASE | re.DOTALL,
    )
    blocks: list[dict[str, Any]] = []
    for match in theorem_pattern.finditer(raw):
        body = match.group("body") or ""
        label_match = re.search(r"\\label\{([^{}]+)\}", body)
        option = (match.group("option") or "").strip()
        start_line, end_line = _line_span(raw, match.start(), match.end())
        blocks.append(
            {
                "kind": match.group("env"),
                "line": start_line,
                "end_line": end_line,
                "offset": match.start(),
                "label": label_match.group(1).strip() if label_match else "",
                "title": option.strip("[]"),
                "lean": _extract_braced_commands(body, "lean"),
                "uses": _extract_braced_commands(body, "uses"),
                "statement": _bounded(_clean_tex_statement(body), MAX_STATEMENT_CHARS),
            }
        )
        if len(blocks) >= MAX_THEOREM_BLOCKS:
            break
    if len(blocks) < MAX_THEOREM_BLOCKS:
        existing_offsets = {int(block.get("offset", -1) or -1) for block in blocks}
        profess_pattern = re.compile(
            r"\\profess\{(?P<kind>[^{}]+)\}\s*(?P<body>.*?)\\endprofess",
            re.IGNORECASE | re.DOTALL,
        )
        for match in profess_pattern.finditer(raw):
            if match.start() in existing_offsets:
                continue
            raw_kind = str(match.group("kind") or "statement").strip()
            kind = re.sub(r"[^A-Za-z]+", " ", raw_kind).strip().lower() or "statement"
            if kind.endswith("."):
                kind = kind[:-1].strip()
            following = raw[match.end(): match.end() + 12_000]
            proof = ""
            proof_match = re.search(
                r"^\s*\\proof\s*(?P<body>.*?)\\endproof",
                following,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if proof_match:
                proof = _bounded(_clean_tex_statement(proof_match.group("body") or ""), MAX_STATEMENT_CHARS)
                proof_start_line, proof_end_line = _line_span(
                    raw,
                    match.end() + proof_match.start(),
                    match.end() + proof_match.end(),
                )
            else:
                proof_start_line = proof_end_line = 0
            line = _line_number(raw, match.start())
            _start_line, end_line = _line_span(raw, match.start(), match.end())
            label_match = re.search(r"\\label\{([^{}]+)\}", match.group("body") or "")
            blocks.append(
                {
                    "kind": kind,
                    "line": line,
                    "end_line": end_line,
                    "proof_line": proof_start_line,
                    "proof_end_line": proof_end_line,
                    "offset": match.start(),
                    "label": label_match.group(1).strip() if label_match else f"line-{line}",
                    "title": raw_kind,
                    "lean": _extract_braced_commands(match.group("body") or "", "lean"),
                    "uses": _extract_braced_commands(match.group("body") or "", "uses"),
                    "statement": _bounded(_clean_tex_statement(match.group("body") or ""), MAX_STATEMENT_CHARS),
                    "proof": proof,
                }
            )
            if len(blocks) >= MAX_THEOREM_BLOCKS:
                break

    section_pattern = re.compile(r"\\(?P<level>chapter|section|subsection|subsubsection)\*?\{(?P<title>[^{}\n]+)\}")
    sections = [
        {
            "level": match.group("level"),
            "line": _line_number(raw, match.start()),
            "title": match.group("title").strip(),
        }
        for match in section_pattern.finditer(raw)
    ][:MAX_SECTIONS]
    title_match = re.search(r"\\title\{([^{}\n]+)\}", raw)
    title = title_match.group(1).strip() if title_match else ""
    if not title:
        title_lines = re.findall(r"\\centerline\{\\titlefont\s+([^{}\n]+)\}", raw)
        if title_lines:
            title = " ".join(item.strip() for item in title_lines if item.strip())
    bibliography_files = _extract_braced_commands(raw, "bibliography") + _extract_braced_commands(raw, "addbibresource")
    citations = _extract_braced_commands(raw, "cite")[:MAX_REFERENCES]
    labels = _extract_braced_commands(raw, "label")[:MAX_REFERENCES]
    refs = _extract_braced_commands(raw, "ref")[:MAX_REFERENCES]
    return {
        "source_kind": "latex",
        "title": title,
        "bytes": path.stat().st_size,
        "sections": sections,
        "theorem_blocks": blocks,
        "labels": labels,
        "refs": refs,
        "citations": citations,
        "bibliography_files": bibliography_files[:MAX_REFERENCES],
        "extracted_text": _bounded(raw, MAX_EXTRACTED_TEXT_CHARS),
        "extraction_status": "ok",
    }


def _run_document_tool(command: list[str], timeout_s: int = 30) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    output = (result.stdout or "").strip()
    error = (result.stderr or "").strip()
    if result.returncode != 0:
        return False, error or output or f"exit code {result.returncode}"
    return True, output


def _extract_pdf_summary(path: Path) -> dict[str, Any]:
    tools = {
        "pdftotext": bool(shutil.which("pdftotext")),
        "pdfinfo": bool(shutil.which("pdfinfo")),
        "pdfimages": bool(shutil.which("pdfimages")),
    }
    metadata: dict[str, Any] = {
        "source_kind": "pdf",
        "bytes": path.stat().st_size,
        "pdf_tools": tools,
        "sections": [],
        "theorem_blocks": [],
        "extracted_text": "",
        "extraction_status": "missing-pdftotext",
        "degraded_reasons": [],
    }
    if tools["pdfinfo"]:
        ok, info = _run_document_tool(["pdfinfo", str(path)], timeout_s=20)
        metadata["pdfinfo"] = _bounded(info, 4_000)
        if ok:
            pages = re.search(r"^Pages:\s+(\d+)", info, flags=re.MULTILINE)
            if pages:
                metadata["pages"] = int(pages.group(1))
        else:
            metadata["degraded_reasons"].append(f"pdfinfo failed: {info}")
    if tools["pdfimages"]:
        ok, image_list = _run_document_tool(["pdfimages", "-list", str(path)], timeout_s=30)
        metadata["pdf_images"] = _bounded(image_list, 6_000) if ok else ""
        if not ok:
            metadata["degraded_reasons"].append(f"pdfimages failed: {image_list}")
    if not tools["pdftotext"]:
        metadata["degraded_reasons"].append("pdftotext is not installed; PDF text extraction was not available")
        return metadata

    ok, extracted = _run_document_tool(["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"], timeout_s=60)
    if not ok:
        metadata["extraction_status"] = "pdftotext-failed"
        metadata["degraded_reasons"].append(f"pdftotext failed: {extracted}")
        return metadata
    metadata["extracted_text"] = _bounded(extracted, MAX_EXTRACTED_TEXT_CHARS)
    metadata["extraction_status"] = "ok"
    metadata["sections"] = _extract_plaintext_sections(extracted)
    return metadata


def _extract_plaintext_sections(text: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    pattern = re.compile(r"^\s*((?:\d+(?:\.\d+)*)\s+[A-Z][^\n]{2,120}|[A-Z][A-Z0-9 ,;:()'/-]{6,120})\s*$")
    for line_number, line in enumerate((text or "").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if pattern.match(stripped):
            sections.append({"level": "section", "line": line_number, "title": stripped})
        if len(sections) >= MAX_SECTIONS:
            break
    return sections


def inspect_formalization_document(
    path: str | Path,
    *,
    project_root: str | Path | None = None,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
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
            "text_excerpt": _bounded(str(summary.get("extracted_text", "") or ""), MAX_CONTEXT_EXCERPT_CHARS),
        }
    )
    return summary


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=_json_default), encoding="utf-8")


def _render_blocks_for_markdown(blocks: list[Mapping[str, Any]]) -> str:
    if not blocks:
        return "- No theorem-like LaTeX environments were detected by preflight."
    lines: list[str] = []
    for index, block in enumerate(blocks[:20], start=1):
        label = str(block.get("label", "") or "[unlabeled]")
        kind = str(block.get("kind", "") or "statement")
        line = block.get("line", "?")
        end_line = block.get("end_line") or line
        title = str(block.get("title", "") or "").strip()
        heading = f"{index}. `{label}` ({kind}, lines {line}-{end_line})"
        if title:
            heading += f" - {title}"
        lines.append(heading)
        statement = str(block.get("statement", "") or "").strip()
        if statement:
            lines.append(f"   Source statement: {statement}")
        proof = str(block.get("proof", "") or "").strip()
        if proof:
            lines.append(f"   Source proof excerpt: {proof}")
            proof_line = block.get("proof_line") or "?"
            proof_end_line = block.get("proof_end_line") or proof_line
            lines.append(f"   Source proof locator: lines {proof_line}-{proof_end_line}")
        lean = ", ".join(block.get("lean", []) or [])
        uses = ", ".join(block.get("uses", []) or [])
        if lean:
            lines.append(f"   Existing blueprint Lean names: `{lean}`")
        if uses:
            lines.append(f"   Existing dependency labels: `{uses}`")
    remaining = len(blocks) - min(len(blocks), 20)
    if remaining > 0:
        lines.append(f"- ... plus {remaining} more detected block(s); inspect the source document before planning.")
    return "\n".join(lines)


def _render_sections_for_markdown(sections: list[Mapping[str, Any]]) -> str:
    if not sections:
        return "- No section headings were detected by preflight."
    return "\n".join(
        f"- line {item.get('line', '?')}: {item.get('title', '[untitled]')}"
        for item in sections[:30]
    )


def _render_context_markdown(
    *,
    source_relative: str,
    source_kind: str,
    target_lean_relative: str,
    blueprint_path: Path,
    extracted_text_path: Path,
    manifest_path: Path,
    metadata: Mapping[str, Any],
) -> str:
    title = str(metadata.get("title", "") or "").strip()
    blocks = list(metadata.get("theorem_blocks", []) or [])
    sections = list(metadata.get("sections", []) or [])
    degraded = list(metadata.get("degraded_reasons", []) or [])
    discovery_summary = str(metadata.get("tex_project_discovery_summary", "") or "").strip()
    request_relative = str(metadata.get("document_request_relative", "") or "").strip()
    request_kind = str(metadata.get("document_request_kind", "") or "").strip()
    excerpt = str(metadata.get("text_excerpt", "") or metadata.get("extracted_text", "") or "").strip()
    lines = [
        "# EPFLemma Document Formalization Context",
        "",
        f"Source document: `{source_relative}`",
        f"Input request: `{request_relative or source_relative}` ({request_kind or 'file'})",
        f"Document kind: `{source_kind}`",
        f"Detected title: {title or '[none]'}",
        f"Target Lean file: `{target_lean_relative}`",
        f"Planner blueprint: `{blueprint_path}`",
        f"Extracted text cache: `{extracted_text_path}`",
        f"Preflight manifest: `{manifest_path}`",
        "",
        "## Hard Workflow Contract",
        "",
        "This is a document formalization run. Do not treat the request as a proof-only repair.",
        "",
        "Required planner phase:",
        "1. Read the source document and this preflight manifest before drafting Lean.",
        "2. Use `read_pdf` to read project-local PDF text, and use `formalization_document_inspect` for deterministic re-inspection when the source is a .tex or .pdf file.",
        "3. Search local project facts and Mathlib before inventing names or definitions.",
        "4. Use web search only for references or surrounding literature that the source document actually points to.",
        "5. Create or update the planner blueprint before drafting Lean, recording definitions, lemmas, theorem dependencies, source pointers, formal-statement review, and natural-language proof/prover notes. The initial `_pending_` blueprint is only a placeholder and does not satisfy the workflow.",
        "6. Draft Lean files in small units with stable names, minimal imports, and `sorry` placeholders for theorem/lemma proofs that the prover queue should solve. Do not do deep proof repair in the planner draft.",
        "7. Lean import discipline is mandatory: every generated Lean file must begin with all `import` commands before any `/-! ... -/` module doc comment or declaration.",
        "8. Before handing declarations to the managed prover queue, satisfy the document formalization handoff verifier: keep target imports as direct dependencies, ensure the root project module imports the generated target module path so plain `lake build` covers it, and keep the blueprint import plan aligned with the target Lean imports.",
        "9. Verify draft readiness with `lean_inspect` and `lean_verify` (module or file_exact); do not fall back to terminal `lake env lean` just to decide whether the draft is ready.",
        "10. Stop after the source map, blueprint, theorem statements, and `sorry` skeletons are ready. Ask for an independent statement/source verification pass before the prover queue starts.",
        "",
        "Statement fidelity:",
        "- keep source pointers, ambiguity notes, dependencies, and proof notes in the planner blueprint",
        "- put a compact `Source proof` / `Proof sketch` / `Prover notes` paragraph in the Lean doc comment immediately above each source theorem or lemma when the source contains proof guidance",
        "- the generated supplemental blueprint skill keeps the `Blueprint.md` path available to prover turns after compaction",
        "- explicitly compare each Lean statement against the corresponding source statement before handing it to the prover queue",
        "- when the source theorem quantifies over a structured object class or representation, do not count a simpler Lean encoding as full coverage unless a definition or companion declaration records the bridge",
        "- if a representation bridge is intentionally omitted, mark the Lean coverage as partial and record the representation change under `Scope changes`; do not approve the entry as exact source coverage",
        "- record `Statement verification status: approved` only after the verification pass has checked and corrected the blueprint and Lean statements",
        "- do not silently weaken or strengthen the source theorem",
        "- avoid adding Lean comments unless they clarify a concrete formalization choice",
        "- the blueprint is intentionally next to the Lean files so planner and prover turns can reread it easily",
        "",
        "Proof phase:",
        "- After the declaration skeleton is stable and statement/source verification is approved, use the normal managed Lean queue to eliminate `sorry` one declaration at a time.",
        "- If the handoff verifier blocks the queue, update the root module, target imports, or blueprint first; do not work around the blocker by editing theorem statements opportunistically.",
        "- When proving, consult the nearby blueprint and the original source document for natural-language proof strategy before inventing a proof.",
        "- Keep blueprint entries aligned when a theorem is split or renamed.",
        "- Completion still requires clean diagnostics, no open goals, no `sorry` in the requested scope, and final Lean verification.",
        "",
        "Blueprint format:",
        "- The default artifact is Markdown so it works without extra dependencies.",
        "- If the project already has a `blueprint/` directory or `leanblueprint` is available, also keep a leanblueprint-compatible TeX blueprint in sync using `\\lean`, `\\uses`, and `\\leanok` when appropriate.",
        "",
        "## Detected Sections",
        "",
        _render_sections_for_markdown(sections),
        "",
        "## Detected Theorem-Like Blocks",
        "",
        _render_blocks_for_markdown(blocks),
    ]
    if discovery_summary:
        included_tex = list(metadata.get("tex_project_included_tex_files", []) or [])
        bibliography_files = list(metadata.get("tex_project_bibliography_files", []) or [])
        local_assets = list(metadata.get("tex_project_local_asset_files", []) or [])
        missing_includes = list(metadata.get("tex_project_missing_includes", []) or [])
        lines.extend(
            [
                "",
                "## TeX Project Discovery",
                "",
                discovery_summary,
                "",
                "Included TeX files:",
                *([f"- `{item}`" for item in included_tex] or ["- [none]"]),
                "",
                "Bibliography files:",
                *([f"- `{item}`" for item in bibliography_files] or ["- [none]"]),
                "",
                "Local assets:",
                *([f"- `{item}`" for item in local_assets] or ["- [none]"]),
            ]
        )
        if missing_includes:
            lines.extend(["", "Missing or external TeX inputs:", *[f"- `{item}`" for item in missing_includes]])
    if degraded:
        lines.extend(["", "## Preflight Degraded Reasons", "", *[f"- {item}" for item in degraded]])
    if excerpt:
        lines.extend(
            [
                "",
                "## Source Excerpt",
                "",
                "```text",
                _bounded(excerpt, MAX_CONTEXT_EXCERPT_CHARS),
                "```",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _initial_blueprint(source_relative: str, target_lean_relative: str, metadata: Mapping[str, Any]) -> str:
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
        "- [ ] Record a natural-language proof strategy or source proof pointer for each theorem/lemma.",
        "- [ ] Hand stable `sorry` declarations to the managed prover queue.",
        "",
        "Replace all `_pending_` entries before drafting Lean. The managed workflow treats this initial",
        "blueprint as a placeholder, not as a completed plan.",
        "",
        "For each theorem or lemma, include proof guidance useful to the prover: relevant source proof",
        "paragraphs, induction variables, reductions, important previously planned lemmas, and any",
        "known statement-fidelity caveats. Lean doc comments should include compact proof notes;",
        "the generated supplemental blueprint skill carries the durable `Blueprint.md` reference.",
        "",
        "## Source Statement Inventory",
        "",
    ]
    if not blocks:
        lines.append("- No theorem-like LaTeX blocks were detected by preflight; inspect the document manually.")
    for block in blocks[:MAX_THEOREM_BLOCKS]:
        label = str(block.get("label", "") or f"line-{block.get('line', '?')}")
        lines.extend(
            [
                f"### {label}",
                "",
                f"- Kind: {block.get('kind', 'statement')}",
                f"- Source locator: `{source_relative}:{block.get('line', '?')}-{block.get('end_line') or block.get('line', '?')}`",
                f"- Planned Lean declarations: _pending_",
                f"- Dependencies: {', '.join(block.get('uses', []) or []) or '_pending_'}",
                "- Formal statement review: _pending_",
                "- Source qualifiers: _pending_",
                "- Lean coverage: _pending_",
                "- Scope changes: _pending_",
                "- Statement verification status: _pending_",
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


def _ensure_formalization_import_chain(root: Path, target_lean_path: Path, target_lean_relative: str) -> dict[str, str]:
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


def _initial_target_lean(_source_relative: str, _blueprint_relative: str, _import_module: str) -> str:
    return "import Mathlib\n"


def prepare_formalization_document_context(
    *,
    project_root: str | Path,
    cwd: str | Path,
    workflow_args: str,
    project_label: str = "",
) -> FormalizationDocumentContext:
    root = Path(project_root).expanduser().resolve()
    base = Path(cwd).expanduser().resolve()
    selection = _select_formalization_document(root, base, workflow_args)
    source_path = selection.source_path
    source_relative = selection.source_relative
    source_kind = selection.source_kind
    metadata = _extract_latex_summary(source_path) if source_kind == "latex" else _extract_pdf_summary(source_path)
    metadata.update(selection.discovery_metadata)
    metadata["text_excerpt"] = _bounded(str(metadata.get("extracted_text", "") or ""), MAX_CONTEXT_EXCERPT_CHARS)

    slug = _safe_slug(Path(source_relative).with_suffix("").as_posix().replace("/", "-"))
    state_dir = root / ".epflemma" / "workflow-state" / "formalization" / slug
    target_lean_path = _default_target_lean_path(root, project_label, source_path)
    module_name = _safe_name(project_label or root.name, "Formalization")
    import_module = "Mathlib"
    target_lean_relative = _relative_to_project(target_lean_path, root) if target_lean_path.exists() else str(target_lean_path.relative_to(root))
    context_path = state_dir / "context.md"
    manifest_path = state_dir / "manifest.json"
    extracted_text_path = state_dir / "extracted.txt"
    blueprint_path = target_lean_path.parent / "Blueprint.md"
    blueprint_skill_path = (
        root
        / ".epflemma"
        / "skills"
        / _blueprint_skill_name(target_lean_relative)
        / "SKILL.md"
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
        blueprint_path.write_text(_initial_blueprint(source_relative, target_lean_relative, metadata), encoding="utf-8")
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
