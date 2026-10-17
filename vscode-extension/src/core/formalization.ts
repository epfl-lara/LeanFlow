/** Project source inventory and review projections owned by the formalization controller. */
import { invalidCorrectionProjection } from "./formalizationCorrections";

export interface FormalizationSourceRef {
  artifact_id: string;
  file?: string;
  line_start?: number;
  line_end?: number;
  page_index?: number;
  page_label?: string;
  rotation?: number;
  precision?: string;
  method?: string;
  bbox?: number[];
  text?: string;
}

export interface FormalizationLeanRef {
  artifact_id?: string;
  file?: string;
  name: string;
  imports?: string[];
  declaration_origin?: "generated" | "existing_project" | "dependency";
  line_start?: number;
  line_end?: number;
  type?: string;
  type_hash?: string;
  body_hash?: string;
  definition_context_hash?: string;
}

export interface FormalizationAuthorityAction {
  type: "record_correction_authority";
  authority_id: string;
  version: number;
  kind: "bounded_policy" | "interpretation" | "convention";
  source_bundle: string;
  source_item_ids: string[];
  clause_ids: string[];
  statement: string;
  enabled: boolean;
}

export interface FormalizationCorrectionAuthority extends Omit<FormalizationAuthorityAction, "type"> {
  id: string;
  actor: "user";
  current?: boolean;
}

export interface FormalizationCorrection {
  id: string;
  content_id: string;
  kind: "encoding_repair" | "equivalent_reformulation" | "source_supported_mathematical" | "user_interpretation";
  source_item_id: string;
  clause_ids: string[];
  original: { literal_reading: string; source_refs: FormalizationSourceRef[] };
  adopted: { reading: string; lean_refs: FormalizationLeanRef[] };
  rationale: string;
  source_context_refs: FormalizationSourceRef[];
  authorities: FormalizationCorrectionAuthority[];
  authority_current: boolean;
  evidence_status: "saved_checked" | "stale" | "pending";
  evidence_id?: string | null;
  evidence: { goal_name: string; proof_name: string; purpose: string; goal: FormalizationLeanRef; proof: FormalizationLeanRef }[];
  reviews: { correction_id: string; content_id: string; evidence_id: string; authority_refs: string[]; decision: "PASS" | "BLOCK"; rationale: string; source_support: string; competing_alternatives: string; issues: string[]; binding_matches: boolean }[];
  dependents: { unit_id: string; source_item_ids: string[]; declaration_names: string[] }[];
  acceptance_status: "current" | "saved" | "pending";
}

export interface FormalizationItem {
  id: string;
  title: string;
  kind: string;
  claim_status: string;
  extracted_claim_status?: string;
  claim_status_rationale?: string;
  proof_authorization?: { actor: "user"; authorized: boolean; reason: string } | null;
  scope: string;
  source_refs: FormalizationSourceRef[];
  lean_refs: FormalizationLeanRef[];
  draft_mapping?: { preview_id: string; lean_refs: FormalizationLeanRef[]; issues: string[]; notes?: string[] };
  review_status: string;
  elaboration_status: string;
  proof_status: string;
  feedback: string[];
  assumptions?: string[];
  representation_notes?: string;
  mapping_rationale?: string;
  corrections?: FormalizationCorrection[];
  correction_authority_options?: { enabled: boolean; current: FormalizationCorrectionAuthority | null; action: FormalizationAuthorityAction }[];
  known_obligations?: {
    id: string;
    description: string;
    reason: string;
    source_refs: FormalizationSourceRef[];
    dispositions: { unit_id: string; disposition: string; declaration_names: string[]; rationale: string }[];
    verdicts: { decision: "PASS" | "BLOCK"; rationale: string; declaration_names: string[] }[];
  }[];
}

export interface FormalizationRegion {
  id: string;
  title?: string;
  classification?: string;
  disposition?: string;
  source_refs?: FormalizationSourceRef[];
  item_ids?: string[];
  reason?: string;
  repair_required?: boolean;
}

export interface FormalizationArtifact {
  id: string;
  path: string;
  kind: string;
  sha256: string;
  title?: string;
}

export interface FormalizationDraftDelivery {
  complete: boolean;
  coverage_complete: boolean;
  compiled: boolean;
  project_saved: boolean;
  counts: {
    source_items: number;
    required_source_items: number;
    excluded_source_items: number;
    mapped_source_items: number;
    regions: number;
    accounted_regions: number;
    known_clauses: number;
    required_known_clauses: number;
    excluded_known_clauses: number;
    accounted_known_clauses: number;
    unresolved_known_clauses: number;
  };
  project_artifact_id: string;
  census_artifact_id: string;
  report_artifact_ids: Record<string, string>;
}

export interface FormalizationDiagnostic {
  id: string;
  code: string;
  message: string;
  file?: string;
  blocking: boolean;
  source_refs: FormalizationSourceRef[];
  can_resolve_command: boolean;
  can_resolve_conditional?: boolean;
  native_interpretation_required?: boolean;
  native_interpretation_status?: "saved_reviewed" | "pending" | null;
  resolution_reason?: string;
  resolution?: "verified_transcription" | "explicit_scope_exclusion" | "explicit_source_boundary" | {
    actor: "user";
    interpretation: string;
    reason: string;
    verified: true;
  } | null;
}

export interface FormalizationSnapshot {
  schema_version: 1;
  document_id: string;
  revision_id: string;
  source_bundle?: string;
  status: string;
  currentness?: "historical" | "checked";
  source_currentness?: "unchecked" | "checked" | "stale";
  preparation?: { status: string; current: boolean; reason?: string; receipt?: Record<string, unknown> | null };
  preparation_result?: { accepted: boolean; status: string; error?: string };
  slice?: { phase: "representative_slice" | "complete"; required: boolean; slice_id?: string | null; slice_source_ids?: string[] };
  acceptance_freshness?: { current: boolean; status: "historical" | "not_published" | "current" | "environment_stale" | "checker_stale" | "acceptance_stale"; reason: string };
  receipt_error?: string | null;
  source: { kind: string; path: string; files?: string[]; pages?: number };
  items: FormalizationItem[];
  regions: FormalizationRegion[];
  diagnostics: (string | FormalizationDiagnostic)[];
  counts: Record<string, number>;
  layout?: { summary?: string; modules?: string[]; changes?: string[] };
  artifacts: FormalizationArtifact[];
  draft_preview?: {
    schema_version: 1;
    preview_id: string;
    run_id: string;
    base_revision_id: string;
    source_bundle: string;
    preparation_id: string;
    source_diagnostics: string[];
    status: "unaccepted_source_uncertain" | "unaccepted_formalization";
    reason?: "review_ambiguities";
    delivery_status?: "complete" | "incomplete";
    delivery?: FormalizationDraftDelivery;
    accepted: false;
    publication_authority: false;
    proof_authority: false;
    artifacts: FormalizationArtifact[];
    model_report_artifact_id: string;
  } | null;
  can_prove: boolean;
  source_stale?: boolean;
  selection_conflict?: { message: string; private_revision_id: string; published_revision_id: string } | null;
  transaction_state?: string;
  proof_revision_id?: string | null;
  proof_completion?: "checked" | "pending";
  reviewer?: { provider?: string; model?: string; effort?: string };
  costs?: { known_usd?: number; unknown_calls?: number; budget_usd?: number };
  handoff?: {
    id?: string;
    revision_id: string;
    obligations: { id: string; declaration: string; file: string; source_ids?: string[] }[];
  };
  revisions?: { revision_id: string; status: string }[];
  revision_history?: { offset: number; next_offset: number | null; total: number };
}

export type FormalizationAction =
  | FormalizationAuthorityAction
  | { type: "retry_extraction"; reason: string }
  | { type: "resolve_command" | "resolve_conditional"; diagnostic_id: string; interpretation: string; reason: string; verified: true }
  | { type: "authorize_proof"; item_id: string; authorized: boolean; reason: string }
  | { type: "transcribe_page" | "replace_page_transcription"; page_index: number; text: string; reason: string; verified: true }
  | { type: "set_scope"; item_id: string; scope: "included" | "excluded"; reason: string }
  | { type: "classify_region"; region_id: string; classification: "non_mathematical" | "excluded" | "supporting_context"; reason: string }
  | { type: "add_item"; title: string; kind: string; source_refs: FormalizationSourceRef[] }
  | { type: "update_boundary"; item_id: string; source_refs: FormalizationSourceRef[] }
  | { type: "set_mapping"; item_id?: string; source_ids?: string[]; unit_id?: string; depends_on_units?: string[]; lean_refs: FormalizationLeanRef[]; rationale: string }
  | { type: "select_revision"; revision_id: string };

export interface FormalizationPdf {
  documentId: string;
  revisionId: string;
  artifactId: string;
  base64: string;
  pageIndex: number;
}

/** Require an inventory protocol version rather than interpreting an unknown state as success. */
export function parseFormalizationSnapshot(value: unknown): FormalizationSnapshot {
  if (!value || typeof value !== "object") throw new Error("The formalization service returned no inventory.");
  const raw = value as Partial<FormalizationSnapshot>;
  if (raw.schema_version !== 1 || !raw.document_id || !raw.revision_id || !raw.source ||
      !Array.isArray(raw.items) || !Array.isArray(raw.regions) || !Array.isArray(raw.artifacts)) {
    throw new Error("This inventory format is not supported. Update the LeanFlow CLI and extension together.");
  }
  const ids = raw.items.map(item => item.id);
  if (ids.some(id => typeof id !== "string" || !id) || new Set(ids).size !== ids.length ||
      raw.items.some(item => !Array.isArray(item.source_refs) || !Array.isArray(item.lean_refs) ||
        item.known_obligations !== undefined && (!Array.isArray(item.known_obligations) || item.known_obligations.some(row =>
          !row || typeof row.id !== "string" || typeof row.description !== "string" || typeof row.reason !== "string" ||
          !Array.isArray(row.source_refs) || !Array.isArray(row.dispositions) || !Array.isArray(row.verdicts) ||
          row.dispositions.some(value => !value || typeof value.unit_id !== "string" || typeof value.disposition !== "string" ||
            typeof value.rationale !== "string" || !Array.isArray(value.declaration_names) || value.declaration_names.some(name => typeof name !== "string")) ||
          row.verdicts.some(value => !value || !["PASS", "BLOCK"].includes(value.decision) || typeof value.rationale !== "string" ||
            !Array.isArray(value.declaration_names) || value.declaration_names.some(name => typeof name !== "string")))) ||
        item.proof_authorization != null && (item.proof_authorization.actor !== "user" ||
          typeof item.proof_authorization.authorized !== "boolean" || typeof item.proof_authorization.reason !== "string"))) {
    throw new Error("The formalization service returned an invalid source inventory.");
  }
  if (Array.isArray(raw.diagnostics) && raw.diagnostics.some(diagnostic =>
    typeof diagnostic !== "string" && (!diagnostic || typeof diagnostic !== "object" ||
      typeof diagnostic.id !== "string" || typeof diagnostic.code !== "string" ||
      typeof diagnostic.message !== "string" || typeof diagnostic.blocking !== "boolean" ||
      typeof diagnostic.can_resolve_command !== "boolean" || !Array.isArray(diagnostic.source_refs) ||
      diagnostic.can_resolve_conditional !== undefined && typeof diagnostic.can_resolve_conditional !== "boolean" ||
      diagnostic.native_interpretation_required !== undefined && typeof diagnostic.native_interpretation_required !== "boolean" ||
      diagnostic.native_interpretation_status != null && !["saved_reviewed", "pending"].includes(diagnostic.native_interpretation_status) ||
      diagnostic.native_interpretation_status === "saved_reviewed" && diagnostic.native_interpretation_required === true ||
      diagnostic.resolution != null && (typeof diagnostic.resolution === "string"
        ? !["verified_transcription", "explicit_scope_exclusion", "explicit_source_boundary"].includes(diagnostic.resolution)
        : diagnostic.resolution.actor !== "user" || diagnostic.resolution.verified !== true ||
          typeof diagnostic.resolution.interpretation !== "string" || typeof diagnostic.resolution.reason !== "string")))) {
    throw new Error("The formalization service returned invalid extraction diagnostics.");
  }
  if (invalidCorrectionProjection(raw as FormalizationSnapshot)) {
    throw new Error("The formalization service returned invalid correction evidence or user decisions.");
  }
  for (const item of raw.items) {
    const mapping = item.draft_mapping;
    if (mapping === undefined) continue;
    const preview = raw.draft_preview;
    if (!mapping || Object.keys(mapping).some(key => !["preview_id", "lean_refs", "issues", "notes"].includes(key)) ||
        preview?.status !== "unaccepted_formalization" || !Array.isArray(preview.artifacts) || mapping.preview_id !== preview.preview_id ||
        !Array.isArray(mapping.issues) || mapping.issues.length > 33 || mapping.issues.some(issue => typeof issue !== "string" || issue.length > 4096) ||
        mapping.notes !== undefined && (!Array.isArray(mapping.notes) || mapping.notes.length > 33 || mapping.notes.some(note => typeof note !== "string" || note.length > 4096)) ||
        !Array.isArray(mapping.lean_refs) || mapping.lean_refs.length > 128 || mapping.lean_refs.length > 0 && preview.delivery?.compiled !== true || mapping.lean_refs.some(ref =>
          !ref || typeof ref.name !== "string" || !ref.name || ref.name.length > 1024 ||
          ![ref.type_hash, ref.body_hash].every(value => typeof value === "string" && /^[a-f0-9]{64}$/.test(value)) ||
          ref.type !== undefined && (typeof ref.type !== "string" || ref.type.length > 1_000_000) ||
          (ref.artifact_id ? !Number.isSafeInteger(ref.line_start) || Number(ref.line_start) < 1 ||
            !preview.artifacts?.some(artifact => artifact.id === ref.artifact_id && artifact.kind === "lean" && artifact.path === ref.file &&
              raw.artifacts!.some(listed => listed.id === artifact.id && listed.sha256 === artifact.sha256 && listed.path === artifact.path && listed.kind === "lean"))
            : ref.declaration_origin !== "dependency" || ref.file !== undefined || ref.line_start !== undefined))) {
      throw new Error("The formalization service returned invalid unaccepted draft navigation.");
    }
  }
  if (raw.preparation != null && (typeof raw.preparation !== "object" || typeof raw.preparation.current !== "boolean" || typeof raw.preparation.status !== "string") ||
      raw.slice != null && (typeof raw.slice !== "object" || typeof raw.slice.required !== "boolean" || !["representative_slice", "complete"].includes(raw.slice.phase)) ||
      raw.acceptance_freshness != null && (typeof raw.acceptance_freshness !== "object" || typeof raw.acceptance_freshness.current !== "boolean" || typeof raw.acceptance_freshness.reason !== "string" ||
        !["historical", "not_published", "current", "environment_stale", "checker_stale", "acceptance_stale"].includes(raw.acceptance_freshness.status)) ||
      raw.currentness === "historical" && (raw.can_prove === true || raw.handoff != null || raw.acceptance_freshness?.current === true)) {
    throw new Error("The formalization service returned invalid preparation or acceptance freshness.");
  }
  if (raw.draft_preview != null) {
    const preview = raw.draft_preview;
    const artifacts = raw.artifacts;
    const delivered = preview.status === "unaccepted_formalization";
    const delivery = preview.delivery;
    const counts = delivery?.counts;
    if (typeof preview !== "object" || preview.schema_version !== 1 || !["unaccepted_source_uncertain", "unaccepted_formalization"].includes(preview.status) ||
        preview.accepted !== false || preview.publication_authority !== false || preview.proof_authority !== false || raw.can_prove === true || raw.handoff != null ||
        ["published", "accepted_staged"].includes(raw.status ?? "") ||
        [preview.preview_id, preview.run_id, preview.base_revision_id, preview.source_bundle, preview.model_report_artifact_id].some(value => typeof value !== "string" || !value) ||
        typeof preview.preparation_id !== "string" || preview.base_revision_id !== raw.revision_id ||
        !Array.isArray(preview.source_diagnostics) || preview.source_diagnostics.some(value => typeof value !== "string") ||
        !Array.isArray(preview.artifacts) || preview.artifacts.some(artifact => !artifact || typeof artifact !== "object" ||
          [artifact.id, artifact.path, artifact.kind, artifact.sha256].some(value => typeof value !== "string" || !value) ||
          !["lean", "source", ...(delivered ? ["project"] : [])].includes(artifact.kind) || !/^[a-f0-9]{64}$/.test(artifact.sha256) ||
          !artifacts.some(existing => existing.id === artifact.id && existing.sha256 === artifact.sha256 && existing.kind === artifact.kind && existing.path === artifact.path)) ||
        !delivered && (preview.reason !== undefined || preview.delivery_status !== undefined || preview.delivery !== undefined) ||
        delivered && (preview.reason !== "review_ambiguities" || !["complete", "incomplete"].includes(preview.delivery_status ?? "") ||
          !delivery || typeof delivery !== "object" || Array.isArray(delivery) || typeof delivery.complete !== "boolean" ||
          typeof delivery.compiled !== "boolean" || typeof delivery.coverage_complete !== "boolean" ||
          delivery.complete !== (preview.delivery_status === "complete") || delivery.complete !== (delivery.coverage_complete && delivery.compiled) ||
          typeof delivery.project_saved !== "boolean" ||
          !counts || [counts.source_items, counts.required_source_items, counts.excluded_source_items, counts.mapped_source_items,
            counts.regions, counts.accounted_regions, counts.known_clauses, counts.required_known_clauses, counts.excluded_known_clauses,
            counts.accounted_known_clauses, counts.unresolved_known_clauses].some(count => !Number.isSafeInteger(count) || count < 0) ||
          counts.required_source_items + counts.excluded_source_items !== counts.source_items ||
          counts.required_known_clauses + counts.excluded_known_clauses !== counts.known_clauses ||
          counts.mapped_source_items > counts.required_source_items || counts.accounted_regions > counts.regions ||
          counts.accounted_known_clauses > counts.required_known_clauses || counts.unresolved_known_clauses > counts.required_known_clauses ||
          delivery.coverage_complete && (!delivery.project_saved || counts.mapped_source_items !== counts.required_source_items ||
            counts.accounted_regions !== counts.regions || counts.accounted_known_clauses !== counts.required_known_clauses) ||
          [delivery.project_artifact_id, delivery.census_artifact_id].some(id => typeof id !== "string" || !id) ||
          !delivery.report_artifact_ids || typeof delivery.report_artifact_ids !== "object" || Array.isArray(delivery.report_artifact_ids) ||
          delivery.report_artifact_ids["Delivery-census"] !== delivery.census_artifact_id ||
          Object.values(delivery.report_artifact_ids).some(id => typeof id !== "string" || !preview.artifacts.some(artifact => artifact.id === id && artifact.kind === "source")) ||
          !preview.artifacts.some(artifact => artifact.kind === "project" && artifact.id === delivery.project_artifact_id) ||
          !/^[A-Za-z0-9_-]{1,80}$/.test(preview.run_id) || preview.artifacts.filter(artifact => artifact.kind === "project").length !== 1 ||
          preview.artifacts.some(artifact => artifact.kind === "project" && artifact.path !== `.leanflow/native-workflows/${preview.run_id}/project`)) ||
        !preview.artifacts.some(artifact => artifact.id === preview.model_report_artifact_id && artifact.kind === "source")) {
      throw new Error("The formalization service returned an invalid unaccepted draft preview.");
    }
  }
  return {
    ...raw as FormalizationSnapshot,
    can_prove: raw.can_prove === true,
    diagnostics: Array.isArray(raw.diagnostics) ? raw.diagnostics : [],
    counts: raw.counts ?? {},
  };
}

/** Keep explicit permission separate from the source claim and acceptance evidence. */
export function proofAuthorizationLabel(item: FormalizationItem): string {
  return item.proof_authorization == null ? "No explicit permission"
    : item.proof_authorization.authorized ? "Explicitly allowed" : "Explicitly revoked";
}

/** Find only candidates on the selected physical page; approximate matches never confer review. */
export function pdfItemCandidates(
  items: FormalizationItem[], artifactId: string, pageIndex: number, selection = "",
): FormalizationItem[] {
  const normalize = (text: string): string => text.toLocaleLowerCase().replace(/\s+/g, " ").trim();
  const needle = normalize(selection);
  return items.filter(item => item.source_refs.some(ref => {
    if (ref.artifact_id !== artifactId || ref.page_index !== pageIndex) return false;
    if (!needle) return true;
    const text = normalize(ref.text ?? "");
    return !!text && (text.includes(needle) || needle.includes(text));
  }));
}

/** Display evidence independently, including absent alignment and unreviewed exclusions. */
export function formalizationCoverage(snapshot: FormalizationSnapshot): {
  included: number; reviewed: number; excluded: number; unresolved: number;
} {
  const included = snapshot.items.filter(item => item.scope === "included");
  return {
    included: included.length,
    reviewed: included.filter(item => ["pass", "passed", "reviewed"].includes(item.review_status)).length,
    excluded: snapshot.items.filter(item => item.scope === "excluded").length,
    unresolved: snapshot.regions.filter(region =>
      region.repair_required || !["represented", "covered", "excluded", "non_mathematical", "supporting_context"].includes(region.classification ?? region.disposition ?? "unresolved")
    ).length,
  };
}
