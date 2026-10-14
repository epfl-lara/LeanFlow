/** Validate the bounded repair vocabulary; review verdicts and artifact paths cannot be submitted. */
import type { FormalizationAction, FormalizationSourceRef } from "../core/formalization";

export function formalizationId(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(value);
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function text(value: unknown, max = 16000): value is string {
  return typeof value === "string" && value.length <= max && !value.includes("\0");
}

function positive(value: unknown, min = 1): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= min && value <= 10_000_000;
}

function sourceRefs(value: unknown): FormalizationSourceRef[] | null {
  if (!Array.isArray(value) || value.length === 0 || value.length > 128) return null;
  const refs: FormalizationSourceRef[] = [];
  for (const raw of value) {
    const row = record(raw);
    if (!row || !formalizationId(row.artifact_id)) return null;
    const ref: FormalizationSourceRef = { artifact_id: row.artifact_id };
    if (row.page_index !== undefined) {
      if (!positive(row.page_index, 0) || !text(row.text ?? "")) return null;
      ref.page_index = row.page_index;
      ref.text = String(row.text ?? "");
    } else {
      if (!positive(row.line_start) || !positive(row.line_end) || row.line_end < row.line_start) return null;
      ref.line_start = row.line_start;
      ref.line_end = row.line_end;
    }
    // File paths, precision, and hashes are controller-derived, never accepted from UI claims.
    refs.push(ref);
  }
  return refs;
}

/** Decode repairs only. There is deliberately no action that marks an item reviewed or proved. */
export function parseFormalizationAction(value: unknown): FormalizationAction | null {
  const raw = record(value);
  if (!raw) return null;
  switch (raw.type) {
    case "resolve_command":
      return formalizationId(raw.diagnostic_id) && text(raw.interpretation) && raw.interpretation.trim() &&
        text(raw.reason) && raw.reason.trim() && raw.verified === true
        ? { type: raw.type, diagnostic_id: raw.diagnostic_id, interpretation: raw.interpretation, reason: raw.reason, verified: true } : null;
    case "authorize_proof":
      return formalizationId(raw.item_id) && typeof raw.authorized === "boolean" && text(raw.reason) && raw.reason.trim()
        ? { type: raw.type, item_id: raw.item_id, authorized: raw.authorized, reason: raw.reason } : null;
    case "transcribe_page":
      return positive(raw.page_index, 0) && text(raw.text, 100000) && raw.text.trim() && text(raw.reason) && raw.reason.trim() && raw.verified === true
        ? { type: raw.type, page_index: raw.page_index, text: raw.text, reason: raw.reason, verified: true } : null;
    case "set_scope":
      return formalizationId(raw.item_id) && ["included", "excluded"].includes(String(raw.scope)) && text(raw.reason) && raw.reason.trim()
        ? { type: raw.type, item_id: raw.item_id, scope: raw.scope as "included" | "excluded", reason: raw.reason } : null;
    case "classify_region":
      return formalizationId(raw.region_id) && ["non_mathematical", "excluded", "supporting_context"].includes(String(raw.classification)) && text(raw.reason) && raw.reason.trim()
        ? { type: raw.type, region_id: raw.region_id, classification: raw.classification as "non_mathematical" | "excluded" | "supporting_context", reason: raw.reason } : null;
    case "add_item": {
      const refs = sourceRefs(raw.source_refs);
      return refs && text(raw.title, 512) && raw.title.trim() && text(raw.kind, 64) && raw.kind.trim()
        ? { type: raw.type, title: raw.title, kind: raw.kind, source_refs: refs } : null;
    }
    case "update_boundary": {
      const refs = sourceRefs(raw.source_refs);
      return refs && formalizationId(raw.item_id) ? { type: raw.type, item_id: raw.item_id, source_refs: refs } : null;
    }
    case "set_mapping": {
      if (!formalizationId(raw.item_id) || !text(raw.rationale) || !raw.rationale.trim() ||
          !Array.isArray(raw.lean_refs) || raw.lean_refs.length > 128) return null;
      const refs: { file: string; name: string }[] = [];
      for (const value of raw.lean_refs) {
        const row = record(value);
        if (!row || !text(row.file, 4096) || !row.file.endsWith(".lean") || /^[\\/]|^[A-Za-z]:/.test(row.file) ||
            row.file.split(/[\\/]/).some(part => part === "..") || !text(row.name, 1024) || !row.name.trim()) return null;
        refs.push({ file: row.file, name: row.name });
      }
      return { type: raw.type, item_id: raw.item_id, lean_refs: refs, rationale: raw.rationale };
    }
    case "select_revision":
      return formalizationId(raw.revision_id) ? { type: raw.type, revision_id: raw.revision_id } : null;
    default:
      return null;
  }
}
