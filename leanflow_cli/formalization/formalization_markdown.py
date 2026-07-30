"""Render document metadata and source excerpts for planner context."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.document_extraction import _bounded

MAX_CONTEXT_EXCERPT_CHARS = 24_000


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
        lines.append(
            f"- ... plus {remaining} more detected block(s); inspect the source document before planning."
        )
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
    """Build the complete Markdown formalization-planner context document from preflight metadata. Assembles source/target paths, hardcoded workflow contract, detected theorem blocks and sections, optional TeX project discovery, preflight degradation notes, and bounded source excerpt into a single context string for the formalization planner AI."""
    title = str(metadata.get("title", "") or "").strip()
    blocks = list(metadata.get("theorem_blocks", []) or [])
    sections = list(metadata.get("sections", []) or [])
    degraded = list(metadata.get("degraded_reasons", []) or [])
    discovery_summary = str(metadata.get("tex_project_discovery_summary", "") or "").strip()
    request_relative = str(metadata.get("document_request_relative", "") or "").strip()
    request_kind = str(metadata.get("document_request_kind", "") or "").strip()
    excerpt = str(
        metadata.get("text_excerpt", "") or metadata.get("extracted_text", "") or ""
    ).strip()
    lines = [
        "# LeanFlow Document Formalization Context",
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
        "1. Read the source document, nearby PDFs/figures/support files from this manifest, and this preflight manifest before drafting Lean.",
        "2. Use `read_pdf` to read project-local PDF text, and use `formalization_document_inspect` for deterministic re-inspection when the source is a .tex or .pdf file.",
        "3. Search local project facts and Mathlib before inventing names or definitions.",
        "4. Use web search only for references or surrounding literature that the source document actually points to.",
        "5. Create or update the planner blueprint before drafting Lean, recording definitions, lemmas, theorem dependencies, source pointers, formal-statement review, complete source proof text when available, and natural-language proof/prover notes. The initial `_pending_` blueprint is only a placeholder and does not satisfy the workflow.",
        "6. Draft Lean files in small units with stable names, minimal imports, and `sorry` placeholders only for theorem/lemma/example proofs that a later explicit prove workflow should solve. Do not do deep proof repair in the planner draft.",
        "7. Lean import discipline is mandatory: every generated Lean file must begin with all `import` commands before any `/-! ... -/` module doc comment or declaration.",
        "8. Before marking the formalization proof-ready, satisfy the document formalization handoff verifier: keep `## Import Plan` to direct Lean imports only, put non-gating search hints under `## Suggested Search Modules`, ensure the root project module imports the generated target module path so plain `lake build` covers it, and keep direct imports aligned with the target Lean files.",
        "9. Verify draft readiness with `lean_inspect`, then `lean_verify(mode=project)` after the generated files and root imports are in place. Module/file checks are useful while iterating, but they do not satisfy the final formalization gate.",
        "10. Stop after the source map, blueprint, theorem statements, and proof `sorry` skeletons are ready. Ask for an independent statement/source verification pass; verifier agents are read-only reviewers and drafting agents apply corrections.",
        "11. Definition, structure, class, and instance construction gaps block proof handoff. A declaration such as `noncomputable def foo := sorry` is not a theorem queue item; either implement the construction or record the blocker instead of launching `/prove`.",
        "12. During the source-fidelity drafting phase, prefer clarity and correctness over premature file splitting. It is acceptable to stabilize the first draft in one generated Lean file if that helps you read the source carefully and respond to verifier feedback.",
        "13. After independent statement/source review passes, the runner will give one final organization pass before the formalizer exits. In that pass, decide whether the generated formalization should be split into multiple files, preserve every blueprint declaration/source mapping, update imports and `## Generated File Layout`, and run project-level Lean verification.",
        "14. Do not check the proof-ready checklist item yourself. Leave it unchecked until independent statement/source review has approved every source entry.",
        "",
        "Statement fidelity:",
        "- keep source pointers, ambiguity notes, dependencies, complete source proof text, and proof notes in the planner blueprint",
        "- put a compact `Source proof` / `Proof sketch` / `Prover notes` paragraph in the Lean doc comment immediately above each source theorem or lemma so the prover gets the right proof nudge immediately",
        "- the generated supplemental blueprint skill keeps the `Blueprint.md` path available to prover turns after compaction",
        "- explicitly compare each Lean statement against the corresponding source statement before marking the formalization proof-ready",
        "- when the source theorem quantifies over a structured object class or representation, do not count a simpler Lean encoding as full coverage unless a definition or companion declaration records the bridge",
        "- if a representation bridge is intentionally omitted, mark the Lean coverage as partial and record the representation change under `Scope changes`; do not approve the entry as exact source coverage",
        "- record `Statement verification status: approved` only after the verification pass has checked source-proof completeness, doc-comment nudges, and Lean statement correctness",
        "- do not silently weaken or strengthen the source theorem",
        "- avoid adding Lean comments unless they clarify a concrete formalization choice",
        "- the blueprint is intentionally next to the Lean files so planner and prover turns can reread it easily",
        "",
        "Proof phase:",
        "- After the declaration skeleton is stable and statement/source verification is approved, the formalizer exits. Do not start the prover queue or a fresh prove workflow automatically.",
        "- Print/log the suggested `/prove` command so the user can review the generated formalization before starting proof search explicitly.",
        "- The theorem queue includes only `theorem`, `lemma`, and `example` proof obligations; construction stubs block handoff.",
        "- Do not force suggested search modules into `.lean` imports. The prover may add imports when needed, then update the direct import plan.",
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
        pdf_files = list(metadata.get("tex_project_pdf_files", []) or [])
        figure_files = list(metadata.get("tex_project_figure_files", []) or [])
        support_files = list(metadata.get("tex_project_support_files", []) or [])
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
                "",
                "Nearby PDF files:",
                *([f"- `{item}`" for item in pdf_files] or ["- [none]"]),
                "",
                "Figure/image files:",
                *([f"- `{item}`" for item in figure_files] or ["- [none]"]),
                "",
                "TeX support files:",
                *([f"- `{item}`" for item in support_files] or ["- [none]"]),
            ]
        )
        if missing_includes:
            lines.extend(
                [
                    "",
                    "Missing or external TeX inputs:",
                    *[f"- `{item}`" for item in missing_includes],
                ]
            )
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
