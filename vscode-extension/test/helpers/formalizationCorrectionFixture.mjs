/** Saved correction display and exact controller decision templates for UI boundary tests. */
import { formalizationFixture } from "./formalizationFixture.mjs";

export function formalizationCorrectionFixture() {
  const value = formalizationFixture();
  value.source_bundle = "f".repeat(64);
  value.currentness = "historical";
  value.can_prove = false;
  delete value.handoff;
  value.acceptance_freshness = { current: false, status: "historical", reason: "Saved evidence only" };
  const action = { type: "record_correction_authority", authority_id: "paper-correction-policy", version: 1, kind: "bounded_policy", source_bundle: value.source_bundle,
    source_item_ids: ["item-1", "item-2"], clause_ids: [], statement: "Allow source-supported local corrections only after closed checked evidence and fresh independent review.", enabled: true };
  value.items[0].correction_authority_options = [
    { enabled: false, current: null, action },
    { enabled: false, current: null, action: { ...action, authority_id: "interpretation-item-1", kind: "interpretation", source_item_ids: ["item-1"], statement: "" } },
    { enabled: false, current: null, action: { ...action, authority_id: "convention-item-1", kind: "convention", source_item_ids: ["item-1"], statement: "" } },
  ];
  const lean = { artifact_id: "lean-1", file: "Main/Paper.lean", name: "Paper.double", line_start: 5, type: "2 * x = x + x", type_hash: "a".repeat(64), body_hash: "b".repeat(64), definition_context_hash: "c".repeat(64) };
  value.items[0].corrections = [{ id: "local-repair", content_id: "correction-content-1", kind: "source_supported_mathematical", source_item_id: "item-1", clause_ids: ["clause-1"],
    original: { literal_reading: "The printed exponent is positive q.", source_refs: value.items[0].source_refs },
    adopted: { reading: "Use the negative exponent required by the stated probability.", lean_refs: [lean] }, rationale: "The stated reciprocal relation fixes the exponent without changing the source bytes.",
    source_context_refs: value.items[0].source_refs, authorities: [{ ...action, id: "authority-1", actor: "user", current: true }], authority_current: true,
    evidence_status: "saved_checked", evidence_id: "correction-evidence-1", evidence: [{ goal_name: "Paper.goal", proof_name: "Paper.fact", purpose: "Prove the closed arithmetic identity only.", goal: { ...lean, name: "Paper.goal" }, proof: { ...lean, name: "Paper.fact", line_start: 12 } }],
    reviews: [{ correction_id: "local-repair", content_id: "correction-content-1", evidence_id: "correction-evidence-1", authority_refs: ["authority-1"], decision: "PASS", rationale: "The source relation uniquely supports this local repair.", source_support: "The preceding displayed equality defines the reciprocal.", competing_alternatives: "The other sign contradicts that displayed equality.", issues: [], binding_matches: true }],
    dependents: [{ unit_id: "unit-result", source_item_ids: ["item-2"], declaration_names: ["Paper.result"] }], acceptance_status: "saved" }];
  return value;
}
