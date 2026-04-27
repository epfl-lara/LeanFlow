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
    target_lean_path: Path
    target_lean_relative: str
    metadata: dict[str, Any]

    def to_env(self) -> dict[str, str]:
        return {
            "EPFLEMMA_FORMALIZATION_DOCUMENT": str(self.source_path),
            "OPENGAUSS_FORMALIZATION_DOCUMENT": str(self.source_path),
            "EPFLEMMA_FORMALIZATION_DOCUMENT_RELATIVE": self.source_relative,
            "OPENGAUSS_FORMALIZATION_DOCUMENT_RELATIVE": self.source_relative,
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
            "EPFLEMMA_FORMALIZATION_EXTRACTED_TEXT": str(self.extracted_text_path),
            "OPENGAUSS_FORMALIZATION_EXTRACTED_TEXT": str(self.extracted_text_path),
            "EPFLEMMA_FORMALIZATION_TARGET_FILE": self.target_lean_relative,
            "OPENGAUSS_FORMALIZATION_TARGET_FILE": self.target_lean_relative,
        }


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


def resolve_formalization_document(
    project_root: str | Path,
    cwd: str | Path,
    workflow_args: str,
) -> tuple[Path, str, str]:
    """Resolve and validate the required document path for `/formalize`."""

    root = Path(project_root).expanduser().resolve()
    base = Path(cwd).expanduser().resolve()
    raw = _strip_wrapping_quotes(workflow_args)
    if not raw:
        raise FormalizationDocumentError(
            "formalize requires a project-local .tex or .pdf document path, "
            "for example `/formalize docs/paper.tex`."
        )
    if raw.startswith("-"):
        raise FormalizationDocumentError(
            "formalize requires the document path before options, for example `/formalize docs/paper.tex --goal section 2`."
        )

    suffix = Path(raw).suffix.lower()
    if suffix == ".lean":
        raise FormalizationDocumentError(
            "formalize now expects a source document (.tex or .pdf). Use `/prove` for an existing Lean file "
            "or `/draft` for statement-only skeleton work."
        )
    if suffix not in SUPPORTED_FORMALIZATION_DOCUMENT_EXTENSIONS:
        raise FormalizationDocumentError(
            "formalize requires a project-local .tex or .pdf document path. "
            f"Unsupported document extension: {suffix or '[none]'}"
        )

    for candidate in _candidate_paths(root, base, raw):
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if not resolved.is_file():
            continue
        relative = _relative_to_project(resolved, root)
        kind = SUPPORTED_FORMALIZATION_DOCUMENT_EXTENSIONS[suffix]
        return resolved, relative, kind

    raise FormalizationDocumentError(f"formalization document not found: {raw}")


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


def _default_target_lean_path(project_root: Path, project_label: str, source_path: Path) -> Path:
    module_name = _safe_name(project_label or project_root.name, "Formalization")
    source_name = _safe_name(source_path.stem, "Document")
    module_dir = project_root / module_name
    if module_dir.is_dir():
        return module_dir / "Formalization" / f"{source_name}.lean"
    return project_root / "Formalization" / f"{source_name}.lean"


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


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
        blocks.append(
            {
                "kind": match.group("env"),
                "line": _line_number(raw, match.start()),
                "label": label_match.group(1).strip() if label_match else "",
                "title": option.strip("[]"),
                "lean": _extract_braced_commands(body, "lean"),
                "uses": _extract_braced_commands(body, "uses"),
                "statement": _bounded(_clean_tex_statement(body), MAX_STATEMENT_CHARS),
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
    bibliography_files = _extract_braced_commands(raw, "bibliography") + _extract_braced_commands(raw, "addbibresource")
    citations = _extract_braced_commands(raw, "cite")[:MAX_REFERENCES]
    labels = _extract_braced_commands(raw, "label")[:MAX_REFERENCES]
    refs = _extract_braced_commands(raw, "ref")[:MAX_REFERENCES]
    return {
        "source_kind": "latex",
        "title": title_match.group(1).strip() if title_match else "",
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
    source, relative, kind = resolve_formalization_document(root, base, str(path))
    summary = _extract_latex_summary(source) if kind == "latex" else _extract_pdf_summary(source)
    summary.update(
        {
            "success": True,
            "source_path": str(source),
            "source_relative": relative,
            "source_kind": kind,
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
        title = str(block.get("title", "") or "").strip()
        heading = f"{index}. `{label}` ({kind}, line {line})"
        if title:
            heading += f" - {title}"
        lines.append(heading)
        statement = str(block.get("statement", "") or "").strip()
        if statement:
            lines.append(f"   Source statement: {statement}")
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
    excerpt = str(metadata.get("text_excerpt", "") or metadata.get("extracted_text", "") or "").strip()
    lines = [
        "# EPFLemma Document Formalization Context",
        "",
        f"Source document: `{source_relative}`",
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
        "2. Use `formalization_document_inspect` for deterministic re-inspection when the source is a .tex or .pdf file.",
        "3. Search local project facts and Mathlib before inventing names or definitions.",
        "4. Use web search only for references or surrounding literature that the source document actually points to.",
        "5. Create or update the planner blueprint first, recording definitions, lemmas, theorem dependencies, source pointers, and proof notes.",
        "6. Draft Lean files in small units with stable names, minimal imports, and `sorry` only where the prover queue should take over.",
        "7. Verify that the drafted declarations typecheck and that each formal statement matches the original source claim before moving into proof repair.",
        "",
        "Required source comments above every generated Lean declaration:",
        "- a short natural-language statement copied or paraphrased from the document",
        "- the source pointer, such as section, theorem label, page, or equation number",
        "- dependency/proof notes from the blueprint when useful",
        "- any intentional scope changes; do not silently weaken or strengthen the source theorem",
        "",
        "Proof phase:",
        "- After the declaration skeleton is stable, use the normal managed Lean queue to eliminate `sorry` one declaration at a time.",
        "- Keep comments and blueprint entries aligned when a theorem is split or renamed.",
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
        "- [ ] Hand stable `sorry` declarations to the managed prover queue.",
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
                f"- Source line/page: {block.get('line', '?')}",
                f"- Planned Lean declarations: _pending_",
                f"- Dependencies: {', '.join(block.get('uses', []) or []) or '_pending_'}",
                "",
                str(block.get("statement", "") or "_statement pending manual extraction_"),
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _initial_target_lean(source_relative: str, blueprint_relative: str) -> str:
    return (
        "/-!\n"
        "# Document formalization draft\n\n"
        f"Source document: `{source_relative}`\n"
        f"Planner blueprint: `{blueprint_relative}`\n\n"
        "EPFLemma created this file as the active formalization target. The planner should\n"
        "replace this scaffold with definitions, lemmas, and theorems. Every generated\n"
        "declaration should have a source comment immediately above it.\n"
        "-/\n"
    )


def prepare_formalization_document_context(
    *,
    project_root: str | Path,
    cwd: str | Path,
    workflow_args: str,
    project_label: str = "",
) -> FormalizationDocumentContext:
    root = Path(project_root).expanduser().resolve()
    base = Path(cwd).expanduser().resolve()
    source_path, source_relative, source_kind = resolve_formalization_document(root, base, workflow_args)
    metadata = _extract_latex_summary(source_path) if source_kind == "latex" else _extract_pdf_summary(source_path)
    metadata["text_excerpt"] = _bounded(str(metadata.get("extracted_text", "") or ""), MAX_CONTEXT_EXCERPT_CHARS)

    slug = _safe_slug(Path(source_relative).with_suffix("").as_posix().replace("/", "-"))
    state_dir = root / ".epflemma" / "workflow-state" / "formalization" / slug
    target_lean_path = _default_target_lean_path(root, project_label, source_path)
    target_lean_relative = _relative_to_project(target_lean_path, root) if target_lean_path.exists() else str(target_lean_path.relative_to(root))
    context_path = state_dir / "context.md"
    manifest_path = state_dir / "manifest.json"
    extracted_text_path = state_dir / "extracted.txt"
    blueprint_path = state_dir / "blueprint.md"

    metadata.update(
        {
            "source_path": str(source_path),
            "source_relative": source_relative,
            "source_kind": source_kind,
            "target_lean_path": str(target_lean_path),
            "target_lean_relative": target_lean_relative,
            "context_path": str(context_path),
            "manifest_path": str(manifest_path),
            "extracted_text_path": str(extracted_text_path),
            "blueprint_path": str(blueprint_path),
            "created_at_unix": int(time.time()),
        }
    )

    state_dir.mkdir(parents=True, exist_ok=True)
    extracted_text_path.write_text(str(metadata.get("extracted_text", "") or ""), encoding="utf-8")
    _write_json(manifest_path, metadata)
    if not blueprint_path.exists():
        blueprint_path.write_text(_initial_blueprint(source_relative, target_lean_relative, metadata), encoding="utf-8")
    if not target_lean_path.exists():
        target_lean_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            blueprint_relative = str(blueprint_path.resolve().relative_to(root.resolve()))
        except Exception:
            blueprint_relative = str(blueprint_path)
        target_lean_path.write_text(_initial_target_lean(source_relative, blueprint_relative), encoding="utf-8")
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
        target_lean_path=target_lean_path,
        target_lean_relative=target_lean_relative,
        metadata=dict(metadata),
    )
