/** Small controller projection with separate review, build, proof and PDF evidence. */
export function formalizationFixture() {
  return {
    schema_version: 1, document_id: "paper-1", revision_id: "revision-1", status: "draft_ready",
    preparation: { status: "prepared", current: true, reason: "" },
    slice: { phase: "complete", required: true, slice_id: "slice-1", slice_source_ids: ["item-1", "item-2"] },
    source: { kind: "latex", path: "paper/main.tex", files: ["paper/main.tex", "paper/results.tex"], pages: 2 },
    items: [
      { id: "item-1", title: "Doubling", kind: "definition", claim_status: "asserted", scope: "included",
        review_status: "passed", elaboration_status: "checked", proof_status: "conditional", feedback: ["Depends on the named existence obligation."],
        source_refs: [{ artifact_id: "source-tex", file: "paper/main.tex", line_start: 2, line_end: 4, precision: "exact", text: "Define doubling." }, { artifact_id: "source-pdf", page_index: 0, precision: "approximate", text: "Define doubling." }],
        lean_refs: [{ artifact_id: "lean-1", file: "Main/Paper.lean", name: "Paper.double", line_start: 5, line_end: 9 }], assumptions: ["Natural numbers"], representation_notes: "Reuse multiplication.", mapping_rationale: "The source operation agrees with the library operation." },
      { id: "item-2", title: "Chosen halves", kind: "theorem", claim_status: "asserted", scope: "included",
        review_status: "correction_needed", elaboration_status: "checked", proof_status: "pending", feedback: [],
        source_refs: [{ artifact_id: "source-pdf", page_index: 1, precision: "approximate", text: "Every even number has a half." }], lean_refs: [] },
      { id: "item-3", title: "Open problem", kind: "conjecture", claim_status: "open", scope: "excluded", review_status: "unreviewed", elaboration_status: "unchecked", proof_status: "unknown", source_refs: [], lean_refs: [], feedback: [] },
    ],
    regions: [{ id: "region-1", title: "Unclassified paragraph", classification: "unresolved", source_refs: [{ artifact_id: "source-tex", line_start: 10, line_end: 15 }] }],
    diagnostics: ["One source region still needs a disposition."], counts: {}, can_prove: true,
    artifacts: [
      { id: "source-tex", path: "formalization/paper-1/source/main.tex", title: "main.tex", kind: "tex", sha256: "a".repeat(64) },
      { id: "source-pdf", path: "formalization/paper-1/source/paper.pdf", title: "paper.pdf", kind: "pdf", sha256: "b".repeat(64) },
      { id: "lean-1", path: "Main/Paper.lean", kind: "lean", sha256: "c".repeat(64) },
    ],
    handoff: { revision_id: "revision-1", obligations: [{ id: "proof-1", declaration: "Paper.half_exists", file: "Main/Paper.lean", source_ids: ["item-2"] }] },
    reviewer: { provider: "claude", model: "fixture-model" }, costs: { known_usd: 1, unknown_calls: 1 },
    layout: { summary: "Main library", modules: ["Main.Paper"], changes: ["Add an import-only aggregator"] },
    revisions: [{ revision_id: "revision-1", status: "accepted" }, { revision_id: "revision-2", status: "rejected" }],
  };
}

/** Frozen previews expose files without adding mappings or accepting source uncertainty. */
export function sourceDraftFixture() {
  const snapshot = formalizationFixture();
  snapshot.status = "source_draft_blocked";
  snapshot.can_prove = false;
  delete snapshot.handoff;
  for (const item of snapshot.items) {
    item.review_status = "unreviewed";
    item.elaboration_status = "unchecked";
    item.lean_refs = [];
  }
  const artifacts = [
    { id: "draft-lean", path: "/frozen/blobs/draft.lean", kind: "lean", title: "Unaccepted definitions", sha256: "d".repeat(64) },
    { id: "draft-report", path: "/frozen/blobs/report.txt", kind: "source", title: "Draft report", sha256: "e".repeat(64) },
  ];
  snapshot.artifacts.push(...artifacts);
  snapshot.draft_preview = {
    schema_version: 1, preview_id: "draft-preview-1", run_id: "run-1", base_revision_id: snapshot.revision_id,
    source_bundle: "f".repeat(64), preparation_id: "preparation-1", source_diagnostics: ["loader-1"],
    status: "unaccepted_source_uncertain", accepted: false, publication_authority: false, proof_authority: false,
    artifacts, model_report_artifact_id: "draft-report",
  };
  return snapshot;
}
