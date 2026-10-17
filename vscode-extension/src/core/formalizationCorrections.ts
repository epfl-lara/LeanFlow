/** Validate controller correction projections and the exact trusted user-decision vocabulary. */
import type { FormalizationAuthorityAction, FormalizationSnapshot } from "./formalization";

type Row = Record<string, unknown>;
const row = (value: unknown): value is Row => !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown, max = 4096): value is string => typeof value === "string" && value.length <= max && !value.includes("\0");
const id = (value: unknown): value is string => text(value, 256) && /^[\p{L}\p{N}_.'-]{1,256}$/u.test(value);
const hash = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const strings = (value: unknown, max = 64): value is string[] => Array.isArray(value) && value.length <= max && value.every(entry => text(entry));
const ids = (value: unknown, empty = true): value is string[] => strings(value) && (empty || value.length > 0) && value.every(id) && new Set(value).size === value.length;
const kinds = ["bounded_policy", "interpretation", "convention"];
const actionFields = ["type", "authority_id", "version", "kind", "source_bundle", "source_item_ids", "clause_ids", "statement", "enabled"];

/** Accept decisions only, never client-provided proofs, verdicts or authority actor stamps. */
export function correctionAuthorityAction(value: unknown, template = false): FormalizationAuthorityAction | null {
  if (!row(value) || Object.keys(value).length !== actionFields.length || actionFields.some(key => !(key in value)) ||
      value.type !== "record_correction_authority" || !id(value.authority_id) || !Number.isSafeInteger(value.version) || Number(value.version) < 1 ||
      !kinds.includes(String(value.kind)) || !hash(value.source_bundle) || !ids(value.source_item_ids, false) || !ids(value.clause_ids) ||
      !text(value.statement) || !template && (value.statement.trim().length < 40 || value.statement.trim().split(/\s+/).length < 6) ||
      typeof value.enabled !== "boolean") return null;
  return value as unknown as FormalizationAuthorityAction;
}

function authority(value: unknown): boolean {
  return row(value) && id(value.id) && value.actor === "user" && kinds.includes(String(value.kind)) && id(value.authority_id) &&
    Number.isSafeInteger(value.version) && Number(value.version) >= 1 && hash(value.source_bundle) && ids(value.source_item_ids, false) &&
    ids(value.clause_ids) && text(value.statement) && typeof value.enabled === "boolean" && (value.current === undefined || typeof value.current === "boolean");
}

function sourceRefs(value: unknown): boolean {
  return Array.isArray(value) && value.length <= 128 && value.every(ref => row(ref) && id(ref.artifact_id) &&
    (ref.page_index === undefined || Number.isSafeInteger(ref.page_index) && Number(ref.page_index) >= 0) &&
    (ref.line_start === undefined || Number.isSafeInteger(ref.line_start) && Number(ref.line_start) >= 1) &&
    (ref.text === undefined || text(ref.text, 1_000_000)));
}

function leanRef(value: unknown): boolean {
  return row(value) && text(value.name, 1024) && !!value.name &&
    (value.artifact_id === undefined || id(value.artifact_id)) &&
    (value.file === undefined || text(value.file)) && (value.type === undefined || text(value.type, 1_000_000)) &&
    ["type_hash", "body_hash", "definition_context_hash"].every(key => value[key] === undefined || hash(value[key]));
}

/** Fail closed on malformed display authority and on saved PASS being labelled current acceptance. */
export function invalidCorrectionProjection(snapshot: FormalizationSnapshot): boolean {
  const known = new Set(snapshot.items.map(item => item.id));
  for (const item of snapshot.items) {
    const options: unknown = item.correction_authority_options;
    if (options !== undefined && (!Array.isArray(options) || options.length > 3 || options.some(option => {
      if (!row(option) || typeof option.enabled !== "boolean" || !correctionAuthorityAction(option.action, true) ||
          option.current !== null && !authority(option.current)) return true;
      const action = option.action as FormalizationAuthorityAction;
      return action.source_bundle !== snapshot.source_bundle || action.source_item_ids.some(key => !known.has(key)) ||
        action.kind === "bounded_policy" && (action.source_item_ids.length !== snapshot.items.filter(value => value.scope === "included").length || action.source_item_ids.some(key => !snapshot.items.some(value => value.id === key && value.scope === "included"))) ||
        action.kind !== "bounded_policy" && (action.source_item_ids.length !== 1 || action.source_item_ids[0] !== item.id) ||
        (row(option.current) ? action.version !== Number(option.current.version) + 1 : action.version !== 1);
    }))) return true;
    const corrections: unknown = item.corrections;
    if (corrections === undefined) continue;
    if (!Array.isArray(corrections) || corrections.length > 64 || new Set(corrections.map(value => row(value) ? value.id : null)).size !== corrections.length) return true;
    for (const correction of corrections) {
      if (!row(correction) || !id(correction.id) || !id(correction.content_id) || correction.source_item_id !== item.id || !ids(correction.clause_ids, false) ||
          !["encoding_repair", "equivalent_reformulation", "source_supported_mathematical", "user_interpretation"].includes(String(correction.kind)) ||
          !row(correction.original) || !text(correction.original.literal_reading) || !sourceRefs(correction.original.source_refs) ||
          !row(correction.adopted) || !text(correction.adopted.reading) || !Array.isArray(correction.adopted.lean_refs) || !correction.adopted.lean_refs.every(leanRef) ||
          !text(correction.rationale) || !sourceRefs(correction.source_context_refs) || typeof correction.authority_current !== "boolean" ||
          !Array.isArray(correction.authorities) || !correction.authorities.every(authority) ||
          !["saved_checked", "stale", "pending"].includes(String(correction.evidence_status)) ||
          correction.evidence_id != null && !id(correction.evidence_id) ||
          !Array.isArray(correction.evidence) || correction.evidence.length > 16 || correction.evidence.some(fact => !row(fact) || !id(fact.goal_name) || !id(fact.proof_name) || !text(fact.purpose) || !leanRef(fact.goal) || !leanRef(fact.proof)) ||
          !Array.isArray(correction.reviews) || correction.reviews.some(review => !row(review) || review.correction_id !== correction.id || !id(review.content_id) || !id(review.evidence_id) || !ids(review.authority_refs, false) ||
            !["PASS", "BLOCK"].includes(String(review.decision)) || !text(review.rationale) || !text(review.source_support) || !text(review.competing_alternatives) || !strings(review.issues, 32) || typeof review.binding_matches !== "boolean") ||
          !Array.isArray(correction.dependents) || correction.dependents.some(value => !row(value) || !id(value.unit_id) || !ids(value.source_item_ids) || !strings(value.declaration_names)) ||
          !["current", "saved", "pending"].includes(String(correction.acceptance_status))) return true;
      if (correction.acceptance_status === "current" && (snapshot.currentness !== "checked" || snapshot.acceptance_freshness?.current !== true ||
          snapshot.status !== "published" || !snapshot.handoff || snapshot.source_stale || snapshot.selection_conflict ||
          correction.authority_current !== true || correction.evidence_status !== "saved_checked" || !correction.reviews.length ||
          correction.reviews.some(review => !row(review) || review.binding_matches !== true || review.decision !== "PASS"))) return true;
      if (correction.kind !== "encoding_repair" && !correction.evidence.length || correction.evidence_status === "saved_checked" &&
          (!id(correction.evidence_id) || [...correction.adopted.lean_refs, ...correction.evidence.flatMap(fact => row(fact) ? [fact.goal, fact.proof] : [])].some(ref =>
            !row(ref) || !["type_hash", "body_hash", "definition_context_hash"].every(key => hash(ref[key])))) ||
          correction.evidence.some(fact => !row(fact) || !row(fact.goal) || !row(fact.proof) || fact.goal.name !== fact.goal_name || fact.proof.name !== fact.proof_name)) return true;
    }
  }
  return false;
}
