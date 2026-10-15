/** Summarize source inspection, translation acceptance and proof completion as separate tracks. */
import type { FormalizationDiagnostic, FormalizationRegion, FormalizationSnapshot } from "../../src/core/formalization";

const ACCOUNTED = new Set(["represented", "covered", "excluded", "non_mathematical", "supporting_context"]);
const ACCEPTED = new Set(["published", "accepted_staged"]);
const STALE = new Set(["environment_stale", "checker_stale", "acceptance_stale"]);
const RETRY = new Set(["review_error", "check_error", "check_rejected"]);

export type RegionState = "repair" | "open" | "accounted";

/** Classify like formalizationCoverage: user repair first, then regions a proposal must still account for. */
export function regionState(region: FormalizationRegion): RegionState {
  if (region.repair_required) return "repair";
  return ACCOUNTED.has(region.classification ?? region.disposition ?? "unresolved") ? "accounted" : "open";
}

export function plural(count: number, noun: string): string {
  return `${count} ${count === 1 ? noun : `${noun}s`}`;
}

/** Only the controller may identify source uncertainty handled by the native proposal and review gates. */
export function nativeInterpretation(entry: string | FormalizationDiagnostic): entry is FormalizationDiagnostic {
  return typeof entry !== "string" && entry.native_interpretation_required === true;
}

export function manualSourceRepair(entry: string | FormalizationDiagnostic): boolean {
  return typeof entry !== "string" && entry.blocking && !nativeInterpretation(entry);
}

export interface FormalizationProgress {
  historical: boolean;
  accepted: boolean;
  blocking: number;
  pendingInterpretations: number;
  repairRegions: number;
  source: string;
  translation: string;
  proofs: string;
}

/** Inventory evidence never implies acceptance, and accepted statements never imply checked proofs. */
export function formalizationProgress(snapshot: FormalizationSnapshot): FormalizationProgress {
  const historical = snapshot.currentness === "historical";
  const accepted = ACCEPTED.has(snapshot.status);
  const repairRegions = snapshot.regions.filter(region => regionState(region) === "repair").length;
  const blocking = snapshot.diagnostics.filter(manualSourceRepair).length;
  const pendingInterpretations = snapshot.diagnostics.filter(nativeInterpretation).length;
  const awaitingScope = snapshot.items.filter(item => item.scope !== "included" && item.scope !== "excluded").length;
  const unbounded = snapshot.items.filter(item => item.scope !== "excluded" && !item.source_refs.length).length;
  const source = [
    `${plural(snapshot.items.length, "item")} inventoried`,
    ...(awaitingScope ? [`${awaitingScope} awaiting a scope decision`] : []),
    ...(unbounded ? [`${plural(unbounded, "item")} without a source boundary`] : []),
    repairRegions ? `${plural(repairRegions, "region")} need${repairRegions === 1 ? "s" : ""} repair` : "no region needs repair",
    blocking ? plural(blocking, "blocking source issue") : "no source issue requiring manual repair",
    ...(pendingInterpretations ? [`${plural(pendingInterpretations, "source interpretation")} awaiting native proposal and independent review`] : []),
  ].join(" · ");
  return { historical, accepted, blocking, pendingInterpretations, repairRegions, source, translation: translationLabel(snapshot, historical, accepted), proofs: proofLabel(snapshot, historical, accepted) };
}

/** A draft preview or reviewed representative slice is never reported as an accepted translation. */
function translationLabel(snapshot: FormalizationSnapshot, historical: boolean, accepted: boolean): string {
  const saved = historical ? " (saved record; check currentness)" : "";
  if (snapshot.draft_preview) return "unaccepted source draft only; no translation approval";
  if (accepted) return (snapshot.status === "accepted_staged" ? "accepted; publication still pending" : "accepted for this revision") + saved;
  const freshness = snapshot.acceptance_freshness;
  if (STALE.has(snapshot.status) || freshness && !freshness.current && STALE.has(freshness.status)) return `earlier acceptance is stale${freshness?.reason ? `: ${freshness.reason}` : ""}`;
  if (snapshot.status === "review_error") return "not accepted; review could not finish";
  if (snapshot.status === "check_error") return "not accepted; Lean checking could not finish";
  if (snapshot.status === "check_rejected") return "not accepted; the Lean draft needs correction";
  if (snapshot.slice?.required && snapshot.slice.phase === "representative_slice") return "not accepted; a representative slice is translated and reviewed before the whole paper";
  if (snapshot.slice?.required) return `representative slice reviewed${saved}; whole-paper translation not yet accepted`;
  return "not yet accepted";
}

function proofLabel(snapshot: FormalizationSnapshot, historical: boolean, accepted: boolean): string {
  if (snapshot.proof_completion === "checked") return historical ? "proof certificate recorded (saved record)" : "all handoff obligations have checked proofs";
  if (snapshot.handoff) {
    const open = snapshot.handoff.obligations.length;
    return open ? `${plural(open, "proof obligation")} open; proving is a separate, optional step` : "no pending proof obligations";
  }
  if (accepted) return historical ? "check currentness to load proof obligations" : "no prover handoff for this revision";
  return "not started; proving needs an accepted translation";
}

export interface NextStep { text: string; action?: "choose" | "refresh" | "configure" | "check"; target?: { id: string; label: string } }

export interface NextStepContext {
  error: boolean; savedRefreshFailed: boolean; sourceStale: boolean; baselineReady: boolean; setupRunning: boolean;
  preparationBusy: boolean; workflowBusy: boolean; configuring: boolean; resumable: boolean;
}

/** Name one next action. Only controller-blocking issues and repair-required regions gate the advice. */
export function formalizationNextStep(snapshot: FormalizationSnapshot, progress: FormalizationProgress, context: NextStepContext): NextStep {
  if (context.error) return { text: context.savedRefreshFailed ? "Your change is saved. Refresh the inventory to load the saved revision." : "Refresh the inventory before making repairs or starting a workflow.", action: "refresh" };
  if (snapshot.selection_conflict) return { text: "Choose an explicit revision before repairs or launch.", target: { id: "formalization-revisions", label: "Choose a revision" } };
  if (context.sourceStale) return { text: "The live source changed after this frozen revision. Inspect the source again to create a new intake.", action: "choose" };
  if (context.workflowBusy) return { text: "A workflow is running in this workspace. Follow it in Live; another run can start after it stops." };
  if (context.preparationBusy) return { text: "Project preparation is running.", target: { id: "formalization-preparation", label: "Show preparation" } };
  if (context.setupRunning) return { text: "Project setup is running.", target: { id: "formalization-setup", label: "Show project setup" } };
  if (!context.baselineReady) return { text: "Set up project tools before formalizing. Source inspection and repairs remain available.", target: { id: "formalization-setup", label: "Go to project setup" } };
  if (progress.blocking || progress.repairRegions) {
    const work = [
      ...(progress.blocking ? [plural(progress.blocking, "blocking source issue")] : []),
      ...(progress.repairRegions ? [`${plural(progress.repairRegions, "region")} that need${progress.repairRegions === 1 ? "s" : ""} repair`] : []),
    ];
    return { text: `Resolve ${work.join(" and ")}; the translation cannot be accepted until then.`,
      target: progress.blocking ? { id: "formalization-diagnostics", label: "Go to source issues" } : { id: "formalization-regions", label: "Go to source regions" } };
  }
  if (progress.accepted) {
    if (progress.historical) return { text: "Check currentness to confirm this saved acceptance and load the proof obligations.", action: "check" };
    if (snapshot.proof_completion === "checked") return { text: "The translation is accepted and its proofs are checked." };
    if (snapshot.handoff?.obligations.length) return { text: "The translation is accepted. Proof obligations stay open until you choose to start the prover.", target: { id: "formalization-handoff", label: "Go to prover handoff" } };
    return { text: "The translation is accepted. This revision has no prover handoff." };
  }
  if (context.configuring) return { text: `Choose the provider and model, then start the run.${progress.pendingInterpretations ? " The native proposer must explain every pending source interpretation; independent coverage and coherence reviews must pass before acceptance." : ""}`, target: { id: "formalization-configuration", label: "Go to run configuration" } };
  if (RETRY.has(snapshot.status) || STALE.has(snapshot.status)) return { text: `Address the reported problem, then configure formalization to retry${context.resumable ? " or resume the tracked run" : ""}.`, action: "configure" };
  if (snapshot.draft_preview) return { text: "Inspect the unaccepted draft and resolve its source uncertainty, then configure a normal formalization.", action: "configure" };
  if (progress.pendingInterpretations) return { text: `Configure formalization. The native proposer must address ${plural(progress.pendingInterpretations, "source interpretation")}; independent coverage and coherence reviews gate acceptance.`, action: "configure" };
  if (snapshot.slice?.required && snapshot.slice.phase === "representative_slice") return { text: "Configure formalization. The run reviews a representative slice before translating the whole paper.", action: "configure" };
  return { text: "Configure formalization to translate the whole paper and review every included statement.", action: "configure" };
}
