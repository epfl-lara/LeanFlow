"""Resolve formalization sources and discover TeX entrypoint dependencies."""

from __future__ import annotations

import re
from pathlib import Path

from leanflow_cli.formalization.document_extraction import _extract_braced_commands
from leanflow_cli.formalization.formalization_models import (
    FormalizationDocumentError,
    _FormalizationDocumentSelection,
)

TEX_PROJECT_SKIPPED_DIRS = {
    ".leanflow",
    ".git",
    ".lake",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
TEX_PROJECT_FIGURE_SUFFIXES = {".eps", ".jpg", ".jpeg", ".pdf", ".png", ".svg"}
TEX_PROJECT_SUPPORT_SUFFIXES = {
    ".bib",
    ".bbl",
    ".bst",
    ".bbx",
    ".cbx",
    ".cfg",
    ".cls",
    ".clo",
    ".def",
    ".sty",
}


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
            f"formalization document must be inside the LeanFlow project: {path}"
        ) from exc


def _candidate_paths(project_root: Path, cwd: Path, raw_path: str) -> list[Path]:
    raw = _strip_wrapping_quotes(raw_path)
    candidate = Path(raw).expanduser()
    candidates: list[Path] = (
        [candidate] if candidate.is_absolute() else [cwd / raw, project_root / raw]
    )
    project_name = project_root.name
    for prefix in (f"./{project_name}/", f"{project_name}/"):
        if raw.startswith(prefix):
            trimmed = raw[len(prefix) :]
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


def _files_under_by_suffix(directory: Path, suffixes: set[str]) -> list[Path]:
    files: list[Path] = []
    for path in directory.rglob("*"):
        try:
            relative_parts = path.relative_to(directory).parts
        except Exception:
            continue
        if any(part in TEX_PROJECT_SKIPPED_DIRS or part.startswith(".") for part in relative_parts):
            continue
        if path.is_file() and path.suffix.lower() in suffixes:
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


def _resolve_local_reference(
    base_file: Path, project_directory: Path, reference: str
) -> Path | None:
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


def _included_tex_closure(
    entrypoint: Path, project_directory: Path
) -> tuple[list[Path], list[str]]:
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
    if re.search(
        r"\\(?:begin\{(?:theorem|lemma|proposition|corollary|definition|defn)\}|profess\{)", text
    ):
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


def _collect_tex_project_assets(
    entrypoint: Path, directory: Path, included_tex: list[Path]
) -> tuple[list[Path], list[Path]]:
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
            for match in _resolve_asset_reference(
                source,
                directory,
                reference,
                (".bst", ".png", ".jpg", ".jpeg", ".pdf", ".eps", ".svg"),
            ):
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


def _discover_tex_project_entrypoint(
    project_root: Path, directory: Path
) -> _FormalizationDocumentSelection:
    """Locate and score the primary TeX entrypoint in a directory, build its include closure, and return project metadata. Scans all .tex files, ranks them by heuristics (documentclass, begin{document}, common names like main.tex), and fails if multiple files tie for best score. Returns a FormalizationDocumentSelection with the entrypoint path, transitive TeX includes, bibliography files, figures, and support assets."""
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
        (
            (tex_file, _entrypoint_score(tex_file, directory, included_by_other_tex))
            for tex_file in tex_files
        ),
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
    bibliography_files, local_assets = _collect_tex_project_assets(
        best_path, directory, included_tex
    )
    pdf_files = _files_under_by_suffix(directory, {".pdf"})
    figure_files = _files_under_by_suffix(directory, TEX_PROJECT_FIGURE_SUFFIXES)
    support_files = _files_under_by_suffix(directory, TEX_PROJECT_SUPPORT_SUFFIXES)
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
        "tex_project_included_tex_files_relative_to_directory": _directory_relative_list(
            included_tex, directory
        ),
        "tex_project_missing_includes": missing_includes,
        "tex_project_bibliography_files": _relative_list(bibliography_files, project_root),
        "tex_project_local_asset_files": _relative_list(local_assets, project_root),
        "tex_project_pdf_files": _relative_list(pdf_files, project_root),
        "tex_project_figure_files": _relative_list(figure_files, project_root),
        "tex_project_support_files": _relative_list(support_files, project_root),
        "tex_project_discovery_summary": (
            f"Selected `{source_relative}` from `{directory_relative}`; "
            f"{len(included_tex)} included .tex file(s), {len(bibliography_files)} bibliography file(s), "
            f"{len(local_assets)} referenced local asset file(s), {len(pdf_files)} PDF file(s), "
            f"{len(figure_files)} figure file(s), {len(support_files)} TeX support file(s)."
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
