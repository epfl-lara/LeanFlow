/** Project source inventory and review projections owned by the formalization controller. */
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
  review_status: string;
  elaboration_status: string;
  proof_status: string;
  feedback: string[];
  assumptions?: string[];
  representation_notes?: string;
  mapping_rationale?: string;
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

export interface FormalizationDiagnostic {
  id: string;
  code: string;
  message: string;
  file?: string;
  blocking: boolean;
  source_refs: FormalizationSourceRef[];
  can_resolve_command: boolean;
  can_resolve_conditional?: boolean;
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
  status: string;
  preparation?: { status: string; current: boolean; reason?: string; receipt?: Record<string, unknown> | null };
  preparation_result?: { accepted: boolean; status: string; error?: string };
  slice?: { phase: "representative_slice" | "complete"; required: boolean; slice_id?: string | null; slice_source_ids?: string[] };
  acceptance_freshness?: { current: boolean; status: "not_published" | "current" | "environment_stale" | "checker_stale" | "acceptance_stale"; reason: string };
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
    status: "unaccepted_source_uncertain";
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
}

export type FormalizationAction =
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
      diagnostic.resolution != null && (typeof diagnostic.resolution === "string"
        ? !["verified_transcription", "explicit_scope_exclusion", "explicit_source_boundary"].includes(diagnostic.resolution)
        : diagnostic.resolution.actor !== "user" || diagnostic.resolution.verified !== true ||
          typeof diagnostic.resolution.interpretation !== "string" || typeof diagnostic.resolution.reason !== "string")))) {
    throw new Error("The formalization service returned invalid extraction diagnostics.");
  }
  if (raw.preparation != null && (typeof raw.preparation !== "object" || typeof raw.preparation.current !== "boolean" || typeof raw.preparation.status !== "string") ||
      raw.slice != null && (typeof raw.slice !== "object" || typeof raw.slice.required !== "boolean" || !["representative_slice", "complete"].includes(raw.slice.phase)) ||
      raw.acceptance_freshness != null && (typeof raw.acceptance_freshness !== "object" || typeof raw.acceptance_freshness.current !== "boolean" || typeof raw.acceptance_freshness.reason !== "string" ||
        !["not_published", "current", "environment_stale", "checker_stale", "acceptance_stale"].includes(raw.acceptance_freshness.status))) {
    throw new Error("The formalization service returned invalid preparation or acceptance freshness.");
  }
  if (raw.draft_preview != null) {
    const preview = raw.draft_preview;
    const artifacts = raw.artifacts;
    if (typeof preview !== "object" || preview.schema_version !== 1 || preview.status !== "unaccepted_source_uncertain" ||
        preview.accepted !== false || preview.publication_authority !== false || preview.proof_authority !== false || raw.can_prove === true ||
        [preview.preview_id, preview.run_id, preview.base_revision_id, preview.source_bundle, preview.model_report_artifact_id].some(value => typeof value !== "string" || !value) ||
        typeof preview.preparation_id !== "string" || preview.base_revision_id !== raw.revision_id ||
        !Array.isArray(preview.source_diagnostics) || preview.source_diagnostics.some(value => typeof value !== "string") ||
        !Array.isArray(preview.artifacts) || preview.artifacts.some(artifact => !artifact || typeof artifact !== "object" ||
          [artifact.id, artifact.path, artifact.kind, artifact.sha256].some(value => typeof value !== "string" || !value) ||
          !["lean", "source"].includes(artifact.kind) || !/^[a-f0-9]{64}$/.test(artifact.sha256) ||
          !artifacts.some(existing => existing.id === artifact.id && existing.sha256 === artifact.sha256)) ||
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
