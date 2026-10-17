/** Large formalization inventories stay bounded, collapsible and explicit about saves and currentness. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";
import { mount, act, textOf } from "./helpers/webviewFixture.mjs";
import { formalizationFixture, sourceDraftFixture } from "./helpers/formalizationFixture.mjs";
import { formalizationConflictFixture } from "./helpers/formalizationConflictFixture.mjs";

const bundle = await build({
  stdin: {
    contents: 'export { reduceFormalizationFeedback, formalizationSavePending, NO_FORMALIZATION_FEEDBACK } from "./webview/views/FormalizationFeedback"; export { formalizationNextStep, formalizationProgress, regionState } from "./webview/views/formalizationProgress"; export { formalizationPage } from "./webview/views/FormalizationPager";',
    resolveDir: process.cwd(),
    loader: "tsx",
  },
  bundle: true, write: false, format: "esm", platform: "node", jsx: "automatic",
});
const { reduceFormalizationFeedback, formalizationSavePending, NO_FORMALIZATION_FEEDBACK, formalizationNextStep, formalizationProgress, regionState, formalizationPage } =
  await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);

function message(ui, type) { return ui.messages.filter(row => row.type === type).at(-1); }
function hostMessage(value) { act(() => { const event = new Event("message"); event.data = value; window.dispatchEvent(event); }); }
function showState(snapshot, error = "") { hostMessage({ type: "formalizationState", documentId: snapshot?.document_id ?? "paper-1", snapshot, error }); }
function section(ui, id) { return ui.root().find(node => node.type === "details" && node.props.id === id); }
function summary(ui, id) { return textOf(section(ui, id).findAllByType("summary")[0]); }
function regionForms(ui) { return ui.root().findAll(node => node.type === "select" && String(node.props["aria-label"] ?? "").startsWith("Disposition for ")); }

test("opening a current PDF reveals and focuses its viewer without stealing focus on refresh", async () => {
  const ui = mount("formalization");
  const target = document.getElementById("formalization-inventory");
  const revealed = [];
  target.tagName = "DETAILS";
  Object.defineProperty(target, "open", { configurable: true, set: value => revealed.push(value) });
  try {
    const snapshot = formalizationFixture();
    showState(snapshot);
    act(() => section(ui, "formalization-inventory").props.onToggle({ currentTarget: { open: false } }));
    assert.equal(section(ui, "formalization-inventory").props.open, false);
    assert.deepEqual(ui.effects, []);
    ui.click("Open PDF: paper.pdf");
    assert.equal(message(ui, "openFormalizationArtifact").artifactId, "source-pdf");
    const pdf = { documentId: snapshot.document_id, revisionId: snapshot.revision_id, artifactId: "source-pdf", pageIndex: 0, base64: "JVBERg==" };
    await act(async () => hostMessage({ type: "formalizationPdf", pdf }));
    const viewer = ui.root().find(node => node.type === "div" && node.props["aria-label"] === "PDF source viewer");
    assert.equal(viewer.props.role, "region");
    assert.equal(viewer.props.tabIndex, -1);
    assert.equal(section(ui, "formalization-inventory").props.open, true);
    assert.deepEqual(revealed, [true]);
    assert.deepEqual(ui.effects, [["domFocus", { preventScroll: true }], ["focus"]]);

    // Saved-state refreshes and item navigation must not pull the user back to the PDF.
    ui.effects.length = 0;
    showState(structuredClone(snapshot));
    ui.click("Chosen halves");
    assert.deepEqual(ui.effects, []);
    await act(async () => hostMessage({ type: "formalizationPdf", pdf: { ...pdf, pageIndex: 1 } }));
    assert.deepEqual(ui.effects, [["domFocus", { preventScroll: true }], ["focus"]]);

    ui.effects.length = 0;
    await act(async () => hostMessage({ type: "formalizationPdf", pdf: { ...pdf, revisionId: "older-revision" } }));
    assert.deepEqual(ui.effects, []);
    assert.equal(ui.root().findAll(node => node.props["aria-label"] === "PDF source viewer").length, 0);
  } finally { ui.dispose(); }
});

test("source selection and repeated refreshes preserve one copy of every formalization panel", () => {
  const warnings = [];
  const originalError = console.error;
  console.error = (...args) => warnings.push(args.map(String).join(" "));
  let ui;
  try {
    ui = mount("formalization");
    const snapshot = largePaper();
    for (let index = 0; index < 5; index++) {
      showState(structuredClone(snapshot));
      hostMessage({ type: "formalizationSourceSelection", documentId: snapshot.document_id,
        revisionId: snapshot.revision_id, artifactId: "source-tex", lineStart: index * 10 + 1, lineEnd: index * 10 + 5 });
      for (const id of ["formalization-diagnostics", "formalization-inventory", "formalization-regions"]) {
        assert.equal(ui.root().findAll(node => node.type === "details" && node.props.id === id).length, 1, id);
      }
    }
    assert.deepEqual(warnings.filter(value => /same key|unique.*key/i.test(value)), []);
  } finally {
    ui?.dispose();
    console.error = originalError;
  }
});

/** A paper-sized projection: 9050 regions, a few needing repair, and enough items and issues to page. */
function largePaper() {
  const snapshot = formalizationFixture();
  snapshot.items = Array.from({ length: 60 }, (_, index) => ({
    id: `item-${index}`, title: `Statement ${index}`, kind: "theorem", claim_status: "asserted", scope: "included",
    review_status: index % 2 ? "pass" : "unreviewed", elaboration_status: "checked", proof_status: "pending", feedback: [], lean_refs: [],
    source_refs: [{ artifact_id: "source-tex", file: "paper/main.tex", line_start: index * 10 + 1, line_end: index * 10 + 5, precision: "exact" }],
  }));
  snapshot.regions = Array.from({ length: 9050 }, (_, index) => ({
    id: `region-${index}`, title: `Paragraph ${index}`, repair_required: index < 3,
    classification: index >= 3 && index % 3 === 0 ? "supporting_context" : "unresolved",
    source_refs: [{ artifact_id: "source-tex", file: "paper/main.tex", line_start: index + 1, line_end: index + 1 }],
  }));
  snapshot.diagnostics = Array.from({ length: 30 }, (_, index) => ({
    id: `diagnostic-${index}`, code: "unresolved_command", message: `Unknown command ${index}`, blocking: index === 0, can_resolve_command: index === 0, source_refs: [],
  }));
  return snapshot;
}

function nextStep(snapshot, overrides = {}) {
  return formalizationNextStep(snapshot, formalizationProgress(snapshot), {
    error: false, savedRefreshFailed: false, sourceStale: false, baselineReady: true, setupRunning: false,
    preparationBusy: false, workflowBusy: false, configuring: false, resumable: false, ...overrides,
  });
}

test("published saved readings stay visible without asking for another proposal or user approval", () => {
  const ui = mount("formalization");
  try {
    const snapshot = formalizationFixture();
    snapshot.currentness = "historical";
    snapshot.status = "published";
    snapshot.can_prove = false;
    snapshot.handoff = null;
    snapshot.diagnostics = [{ id: "d-reviewed", code: "unresolved_command", message: "Original lexical warning",
      blocking: false, source_refs: [], can_resolve_command: true,
      native_interpretation_required: false, native_interpretation_status: "saved_reviewed" }];
    showState(snapshot);
    const text = textOf(ui.root());
    assert.match(text, /source reading independently reviewed \(saved\)/);
    assert.doesNotMatch(text, /await the native proposer|awaiting native proposal/);
    assert.equal(formalizationProgress(snapshot).pendingInterpretations, 0);
    assert.equal(ui.root().findAllByType("button").some(button => textOf(button) === "Record command interpretation"), false);
    assert.equal(ui.button("Start proving accepted statements").props.disabled, true);
  } finally { ui.dispose(); }
});

test("only controller-marked source readings defer to native review; hard issues still require repair", () => {
  const snapshot = formalizationFixture();
  const pending = { id: "loader-1", code: "unresolved_loader_effect", message: "Package meaning needs interpretation", blocking: true, native_interpretation_required: true, can_resolve_command: false, source_refs: [] };
  snapshot.diagnostics = [pending];
  const progress = formalizationProgress(snapshot);
  assert.equal(progress.blocking, 0);
  assert.equal(progress.pendingInterpretations, 1);
  assert.equal(nextStep(snapshot).action, "configure");
  assert.match(nextStep(snapshot).text, /native proposer.*1 source interpretation.*independent coverage and coherence/);
  assert.equal(snapshot.diagnostics[0].blocking, true);
  snapshot.diagnostics.push({ ...pending, id: "manual-1", code: "unresolved_command", native_interpretation_required: false });
  assert.equal(formalizationProgress(snapshot).blocking, 1);
  assert.equal(nextStep(snapshot).target.id, "formalization-diagnostics");
  delete snapshot.diagnostics[1].native_interpretation_required;
  assert.equal(formalizationProgress(snapshot).blocking, 1);
});

test("pending native interpretations stay visible and source links do not grant manual approval", () => {
  const ui = mount("formalization");
  try {
    const snapshot = formalizationFixture();
    snapshot.diagnostics = [{ id: "loader-1", code: "unresolved_command", message: "Meaning of source notation", blocking: true, native_interpretation_required: true, can_resolve_command: true, source_refs: [{ artifact_id: "source-tex", file: "paper/main.tex", line_start: 2, precision: "exact", text: "Exact original occurrence" }] }];
    showState(snapshot);
    assert.match(summary(ui, "formalization-diagnostics"), /none requiring manual repair.*1 pending native interpretation/);
    assert.match(textOf(ui.root()), /independent coverage and coherence reviews must pass before acceptance/);
    assert.equal(ui.buttons().some(button => textOf(button) === "Record command interpretation"), false);
    ui.click("Inspect pending interpretation loader-1, occurrence 1");
    assert.equal(message(ui, "openFormalizationArtifact").artifactId, "source-tex");
    assert.equal(message(ui, "formalizationAction"), undefined);
    assert.equal(ui.button("Configure formalization").props.className, "btn primary");
  } finally { ui.dispose(); }
});

test("known clause checklist separates authored omissions, independent verdicts and saved evidence", () => {
  const ui = mount("formalization");
  try {
    const snapshot = formalizationFixture();
    snapshot.currentness = "historical";
    snapshot.items[0].known_obligations = [
      { id: "runtime", description: "Expected polynomial running time", reason: "Identified in the exact source.", source_refs: [{ artifact_id: "source-tex", file: "paper/main.tex", line_start: 3, precision: "exact", text: "Runs in expected polynomial time." }], dispositions: [{ unit_id: "unit-core", disposition: "omitted", declaration_names: ["Paper.double"], rationale: "Only the statistical core was translated." }], verdicts: [{ decision: "BLOCK", rationale: "The efficiency clause is missing.", declaration_names: ["Paper.double"] }] },
      { id: "domain", description: "Arbitrary source domain", reason: "Full quantification matters.", source_refs: [], dispositions: [{ unit_id: "unit-core", disposition: "represented", declaration_names: ["Paper.double"], rationale: "Full domain stated." }], verdicts: [{ decision: "PASS", rationale: "Exact domain compared.", declaration_names: ["Paper.double"] }] },
      { id: "qualifier", description: "Variable-length parenthetical", reason: "Meaning not settled.", source_refs: [], dispositions: [], verdicts: [] },
    ];
    showState(snapshot);
    const checklist = ui.root().find(node => node.props["aria-label"] === "Known source clauses");
    assert.match(textOf(checklist), /Known source clauses · 3/);
    assert.match(textOf(checklist), /saved independent review: BLOCK/);
    assert.match(textOf(checklist), /saved independent review: PASS/);
    assert.match(textOf(checklist), /independent review pending/);
    assert.match(textOf(checklist), /Author proposal\s*unit-core: omitted/);
    assert.match(textOf(checklist), /Only the statistical core was translated/);
    assert.match(textOf(checklist), /Clause verdicts below are saved review records/);
    assert.match(textOf(checklist), /does not establish that every mathematical clause was discovered/);
    ui.click("Inspect source clause runtime, occurrence 1");
    assert.deepEqual(message(ui, "openFormalizationArtifact"), { type: "openFormalizationArtifact", documentId: "paper-1", revisionId: "revision-1", artifactId: "source-tex", itemId: "item-1", pageIndex: undefined, line: 3 });
    assert.match(formalizationProgress(snapshot).translation, /not yet accepted/);
  } finally { ui.dispose(); }
});

test("bounded revision history reports the visible range and total without inventing older choices", () => {
  const ui = mount("formalization");
  try {
    const snapshot = formalizationFixture();
    snapshot.revisions = Array.from({ length: 50 }, (_, index) => ({ revision_id: `revision-${index + 1}`, status: "draft_ready" }));
    snapshot.revision_history = { offset: 0, next_offset: 50, total: 87 };
    showState(snapshot);
    assert.match(textOf(section(ui, "formalization-revisions")), /Showing 1–50 of 87 revisions/);
    assert.match(textOf(section(ui, "formalization-revisions")), /only this history page; use the CLI/);
    assert.equal(ui.input("Select formalization revision").findAllByType("option").length, 50);
  } finally { ui.dispose(); }
});

test("progress keeps source inspection, accepted translation and proof completion distinct", () => {
  const unresolved = formalizationFixture();
  assert.equal(regionState(unresolved.regions[0]), "open");
  // A run's coverage proposal accounts for ordinary unresolved regions; only repair-required ones gate the advice.
  assert.equal(nextStep(unresolved).action, "configure");
  unresolved.regions[0].repair_required = true;
  assert.deepEqual(nextStep(unresolved).target, { id: "formalization-regions", label: "Go to source regions" });
  unresolved.diagnostics = [{ id: "d-1", code: "unresolved_command", message: "Unknown", blocking: true, can_resolve_command: true, source_refs: [] }];
  assert.match(nextStep(unresolved).text, /Resolve 1 blocking source issue and 1 region that needs repair/);

  const draft = sourceDraftFixture();
  assert.equal(formalizationProgress(draft).accepted, false);
  assert.match(formalizationProgress(draft).translation, /^unaccepted source draft only/);

  const slice = formalizationFixture();
  slice.slice = { phase: "representative_slice", required: true };
  assert.match(formalizationProgress(slice).translation, /^not accepted; a representative slice/);
  slice.slice = { phase: "complete", required: true, slice_id: "slice-1" };
  assert.match(formalizationProgress(slice).translation, /whole-paper translation not yet accepted$/);
  assert.doesNotMatch(formalizationProgress(slice).translation, /^accepted/);

  const historical = { ...formalizationFixture(), status: "published", currentness: "historical", can_prove: false, handoff: null };
  const saved = formalizationProgress(historical);
  assert.equal(saved.translation, "accepted for this revision (saved record; check currentness)");
  assert.equal(saved.proofs, "check currentness to load proof obligations");
  assert.equal(nextStep(historical).action, "check");
  const checked = { ...historical, currentness: "checked", can_prove: true, handoff: formalizationFixture().handoff };
  assert.match(formalizationProgress(checked).proofs, /^1 proof obligation open; proving is a separate, optional step$/);
  assert.equal(nextStep(checked).action, undefined);
  assert.equal(nextStep(checked, { error: true, savedRefreshFailed: true }).text, "Your change is saved. Refresh the inventory to load the saved revision.");
  assert.deepEqual(formalizationPage(Array.from({ length: 30 }, (_, index) => index), 9), { rows: [25, 26, 27, 28, 29], page: 1 });
});

test("repair feedback waits for the controller and never infers a save without acknowledgment", () => {
  const action = { type: "classify_region", region_id: "region-1", classification: "supporting_context", reason: "Notation" };
  let state = reduceFormalizationFeedback(NO_FORMALIZATION_FEEDBACK, { type: "submit", documentId: "paper-1", revisionId: "revision-1", action });
  assert.equal(formalizationSavePending(state), true);
  assert.equal(reduceFormalizationFeedback(state, { type: "dismiss" }), state);
  // Preparation and currentness replies cannot resolve a repair.
  assert.equal(reduceFormalizationFeedback(state, { type: "state", snapshot: formalizationFixture(), error: "", channel: "preparation" }), state);
  state = reduceFormalizationFeedback(state, { type: "committed", documentId: "paper-1", previousRevisionId: "revision-1", revisionId: "revision-2" });
  assert.deepEqual(state.notice, { kind: "saved", documentId: "paper-1", previousRevisionId: "revision-1", revisionId: "revision-2", label: "region disposition", refreshed: false, refreshError: "" });
  assert.equal(formalizationSavePending(state), true);
  state = reduceFormalizationFeedback(state, { type: "state", snapshot: null, error: "View refresh failed", channel: "document" });
  assert.equal(state.notice.refreshError, "View refresh failed");
  assert.equal(formalizationSavePending(state), false);
  state = reduceFormalizationFeedback(state, { type: "state", snapshot: formalizationFixture(), error: "", channel: "document" });
  assert.equal(state.notice.refreshed, true);
  assert.equal(state.notice.refreshError, "");

  const unconfirmed = reduceFormalizationFeedback(NO_FORMALIZATION_FEEDBACK, { type: "submit", documentId: "paper-1", revisionId: "revision-1", action });
  assert.equal(reduceFormalizationFeedback(unconfirmed, { type: "state", snapshot: formalizationFixture(), error: "", channel: "document" }).notice, null);
  assert.equal(reduceFormalizationFeedback(unconfirmed, { type: "state", snapshot: null, error: "Revision changed", channel: "document" }).notice.kind, "unsaved");
  const superseded = reduceFormalizationFeedback(unconfirmed, { type: "supersede" });
  assert.equal(formalizationSavePending(superseded), true);
  assert.equal(reduceFormalizationFeedback(superseded, { type: "state", snapshot: null, error: "Refresh failed", channel: "document" }).notice, null);
});

test("currentness checks end only with their own reply and are discarded by newer document requests", () => {
  const historical = { ...formalizationFixture(), currentness: "historical" };
  let state = reduceFormalizationFeedback(NO_FORMALIZATION_FEEDBACK, { type: "check", documentId: "paper-1", revisionId: "revision-1" });
  assert.equal(reduceFormalizationFeedback(state, { type: "state", snapshot: historical, error: "", channel: "document" }).checking?.revisionId, "revision-1");
  assert.equal(reduceFormalizationFeedback(state, { type: "state", snapshot: historical, error: "" }).checking?.revisionId, "revision-1");
  assert.equal(reduceFormalizationFeedback(state, { type: "state", snapshot: null, error: "Timed out", channel: "currentness" }).notice.kind, "checkFailed");
  state = reduceFormalizationFeedback(state, { type: "state", snapshot: { ...historical, currentness: "checked" }, error: "", channel: "currentness" });
  assert.deepEqual(state, { checking: null, notice: { kind: "checked", documentId: "paper-1", revisionId: "revision-1" } });
  const pending = reduceFormalizationFeedback(state, { type: "check", documentId: "paper-1", revisionId: "revision-1" });
  assert.deepEqual(reduceFormalizationFeedback(pending, { type: "supersede" }), NO_FORMALIZATION_FEEDBACK);
  const repair = { type: "set_scope", item_id: "item-1", scope: "excluded", reason: "Out of scope" };
  assert.equal(reduceFormalizationFeedback(pending, { type: "submit", documentId: "paper-1", revisionId: "revision-1", action: repair }).checking, null);
});

test("large papers mount one bounded page of rows and repair forms with searchable totals", () => {
  const ui = mount("formalization");
  try {
    showState(largePaper());
    // Regions needing user repair are listed first; every other region stays reachable by filter, search and paging.
    assert.equal(regionForms(ui).length, 3);
    assert.match(summary(ui, "formalization-regions"), /3 need repair.*6031 not yet accounted.*9050 regions/);
    assert.match(textOf(section(ui, "formalization-regions")), /Showing 1–3 of 3 source regions/);
    ui.change(ui.input("Source region filter"), "all");
    assert.equal(regionForms(ui).length, 25);
    assert.match(textOf(section(ui, "formalization-regions")), /Showing 1–25 of 9050 source regions · page 1 of 362/);
    assert.equal(ui.button("Previous source regions page").props.disabled, true);
    ui.click("Next source regions page");
    assert.match(textOf(section(ui, "formalization-regions")), /Showing 26–50 of 9050 source regions · page 2 of 362/);
    assert.ok(ui.input("Disposition for region-25"));
    assert.equal(regionForms(ui).length, 25);
    ui.click("Previous source regions page");
    assert.ok(ui.input("Disposition for region-0"));
    ui.change(ui.input("Search source regions"), "paragraph 9049");
    assert.deepEqual(regionForms(ui).map(node => node.props["aria-label"]), ["Disposition for region-9049"]);
    ui.change(ui.input("Source region filter"), "accounted");
    assert.match(textOf(section(ui, "formalization-regions")), /No matching source regions/);

    const rows = () => ui.root().find(node => node.type === "table").findAllByType("tr");
    assert.equal(rows().length, 26);
    ui.click("Next source items page");
    ui.click("Next source items page");
    assert.match(textOf(section(ui, "formalization-inventory")), /Showing 51–60 of 60 source items · page 3 of 3/);
    assert.equal(ui.button("Next source items page").props.disabled, true);
    // An editor selection on another page brings its candidate into view.
    hostMessage({ type: "formalizationSourceSelection", documentId: "paper-1", revisionId: "revision-1", artifactId: "source-tex", lineStart: 21, lineEnd: 21 });
    ui.click(ui.buttons().find(button => textOf(button) === "Statement 2" && button.props.className === "btn"));
    assert.equal(textOf(rows().find(row => row.props.className === "selected").findAllByType("button")[0]), "Statement 2");
    ui.change(ui.input("Filter source items"), "statement 5");
    assert.match(textOf(section(ui, "formalization-inventory")), /Showing 1–11 of 11 source items/);

    // Long issue lists open on their blocking issues; every other issue is one filter change away.
    assert.match(summary(ui, "formalization-diagnostics"), /1 blocking.*30 issues/);
    const issues = () => section(ui, "formalization-diagnostics").findAll(node => node.type === "details" && node.props.className === "formalization-region");
    assert.equal(issues().length, 1);
    assert.match(textOf(section(ui, "formalization-diagnostics")), /Showing 1 of 1 source issues/);
    ui.change(ui.input("Source issue filter"), "all");
    assert.equal(issues().length, 25);
    ui.click("Next source issues page");
    assert.equal(issues().length, 5);
    assert.match(textOf(ui.root()), /60 items inventoried · 3 regions need repair · 1 blocking source issue/);
  } finally { ui.dispose(); }
});

test("historical views label saved evidence and an explicit currentness check leaves navigation usable", () => {
  const ui = mount("formalization");
  try {
    const snapshot = formalizationFixture();
    Object.assign(snapshot, { status: "published", currentness: "historical", source_currentness: "unchecked", can_prove: false, handoff: null,
      acceptance_freshness: { current: false, status: "historical", reason: "Saved evidence only" } });
    showState(snapshot);
    assert.match(textOf(ui.root()), /saved view, not rechecked/);
    assert.match(textOf(ui.root()), /Saved source review/);
    assert.match(textOf(ui.root()), /Translation: accepted for this revision \(saved record; check currentness\)/);
    assert.match(textOf(ui.root()), /This saved view cannot authorize proving/);
    assert.equal(ui.button("Check currentness").props.className, "btn primary");
    assert.equal(ui.button("Configure formalization").props.className, "btn secondary");
    assert.equal(ui.button("Start proving accepted statements").props.disabled, true);
    ui.click("Check currentness");
    assert.deepEqual(message(ui, "checkFormalization"), { type: "checkFormalization", documentId: "paper-1", revisionId: "revision-1" });
    assert.match(textOf(ui.root()), /Checking currentness for revision revision-1/);
    assert.equal(ui.button("Check currentness").props.disabled, true);
    ui.click("Doubling");
    ui.change(ui.input("New source item title"), "Missed statement");
    assert.equal(ui.button("Submit repair for revision-1").props.disabled, false);
    showState(structuredClone(snapshot));
    assert.match(textOf(ui.root()), /Checking currentness for revision revision-1/);

    const checked = { ...structuredClone(snapshot), currentness: "checked", source_currentness: "checked", can_prove: true, handoff: formalizationFixture().handoff,
      acceptance_freshness: { current: true, status: "current", reason: "" } };
    showState(checked);
    assert.doesNotMatch(textOf(ui.root()), /Checking currentness for revision/);
    assert.match(textOf(ui.root()), /Currentness checked for revision revision-1\. Translation: accepted for this revision\./);
    assert.match(textOf(ui.root()), /checked current/);
    assert.match(textOf(ui.root()), /Proof obligations stay open until you choose to start the prover/);
    assert.equal(ui.button("Start proving accepted statements").props.disabled, false);
    ui.click("Dismiss formalization status");
    assert.doesNotMatch(textOf(ui.root()), /Currentness checked for revision/);

    ui.click("Check currentness");
    ui.click("Refresh inventory");
    assert.doesNotMatch(textOf(ui.root()), /Checking currentness for revision/);
    assert.equal(ui.button("Check currentness").props.disabled, false);
    ui.click("Check currentness");
    showState(null, "Currentness response belongs to another revision. Refresh before continuing.");
    const banner = ui.root().find(node => node.props.id === "formalization-error");
    assert.equal(banner.props.role, "alert");
    assert.match(textOf(banner), /^The currentness check did not finish\. Currentness response belongs to another revision/);
    assert.match(textOf(ui.root()), /The currentness check did not finish\. Show error/);
  } finally { ui.dispose(); }
});

test("a committed repair locks edits until refreshed, and a failed refresh never looks like a failed edit", () => {
  const ui = mount("formalization");
  try {
    const snapshot = formalizationFixture();
    showState(snapshot);
    ui.change(ui.input("Reason for region-1"), "Paragraph supplies notation");
    ui.click("Record region disposition");
    assert.equal(message(ui, "formalizationAction").action.type, "classify_region");
    assert.match(textOf(ui.root()), /Saving region disposition for revision revision-1/);
    for (const label of ["Record region disposition", "Configure formalization", "Choose TeX or PDF", "Check currentness"]) assert.equal(ui.button(label).props.disabled, true, label);
    assert.equal(ui.buttons().some(button => button.props["aria-label"] === "Dismiss formalization status"), false);

    hostMessage({ type: "formalizationActionCommitted", documentId: "paper-1", previousRevisionId: "revision-1", revisionId: "revision-2" });
    assert.match(textOf(ui.root()), /Saved region disposition as revision revision-2\. Loading the updated view/);
    assert.equal(ui.button("Record region disposition").props.disabled, true);
    const failure = "Saved revision revision-2. View refresh failed: timed out. Refresh the inventory; the edit is already committed.";
    showState(null, failure);
    const banner = ui.root().find(node => node.props.id === "formalization-error");
    assert.equal(banner.props.role, "status");
    assert.match(banner.props.className, /warn/);
    assert.match(textOf(banner), /Change saved; the view did not refresh/);
    assert.match(textOf(banner), /still shows revision revision-1/);
    assert.equal(ui.root().findAll(node => node.props.role === "alert" && textOf(node).includes(failure)).length, 0);
    assert.match(textOf(ui.root()), /Saved region disposition as revision revision-2\. The view did not refresh/);
    assert.match(textOf(ui.root()), /Next step: Your change is saved\. Refresh the inventory to load the saved revision\./);
    assert.equal(ui.button("Refresh inventory").props.className, "btn primary");

    ui.click("Refresh inventory");
    assert.deepEqual(message(ui, "showFormalization"), { type: "showFormalization", documentId: "paper-1" });
    showState({ ...structuredClone(snapshot), revision_id: "revision-2" });
    assert.equal(ui.root().findAll(node => node.props.id === "formalization-error").length, 0);
    assert.match(textOf(ui.root()), /Saved region disposition as revision revision-2\./);
    assert.doesNotMatch(textOf(ui.root()), /did not refresh/);
    assert.equal(ui.button("Record region disposition").props.disabled, false);
    ui.click("Dismiss formalization status");
    assert.doesNotMatch(textOf(ui.root()), /Saved region disposition/);
  } finally { ui.dispose(); }
});

test("a refused repair reports that no save was confirmed", () => {
  const ui = mount("formalization");
  try {
    showState(formalizationFixture());
    ui.change(ui.input("Repair kind"), "scope");
    ui.change(ui.input("Repair reason"), "Out of scope for this paper");
    ui.click("Submit repair for revision-1");
    showState(null, "The source revision changed.");
    const banner = ui.root().find(node => node.props.id === "formalization-error");
    assert.equal(banner.props.role, "alert");
    assert.match(textOf(banner), /^No save was confirmed for the scope change\. The source revision changed\. Any previous snapshot below is stale/);
    assert.match(textOf(ui.root()), /No save was confirmed for scope change\. Show error/);
    assert.equal(ui.button("Submit repair for revision-1").props.disabled, true);
  } finally { ui.dispose(); }
});

test("collapsed sections keep blockers and recovery counts visible beside one primary next action", () => {
  const ui = mount("formalization");
  try {
    const snapshot = formalizationFixture();
    snapshot.regions[0].repair_required = true;
    showState(snapshot);
    assert.match(textOf(ui.root()), /Next step: Resolve 1 region that needs repair; the translation cannot be accepted until then\. Go to source regions/);
    for (const label of ["Configure formalization", "Check currentness", "Choose TeX or PDF", "Refresh inventory"]) assert.equal(ui.button(label).props.className, "btn secondary", label);
    assert.equal(section(ui, "formalization-regions").props.open, true);
    assert.match(summary(ui, "formalization-regions"), /1 needs repair.*1 region/);
    assert.equal(section(ui, "formalization-handoff").props.open, false);
    assert.match(summary(ui, "formalization-handoff"), /1 obligation open/);
    assert.equal(section(ui, "formalization-preparation").props.open, false);
    assert.match(summary(ui, "formalization-preparation"), /prepared and current/);
    assert.equal(section(ui, "formalization-recovery").props.open, false);
    hostMessage({ type: "formalizationConflicts", snapshot: formalizationConflictFixture(), error: "" });
    assert.match(summary(ui, "formalization-recovery"), /1 publication conflict/);
    assert.equal(section(ui, "formalization-recovery").props.open, true);

    const repaired = structuredClone(snapshot);
    repaired.regions[0].repair_required = false;
    showState(repaired);
    assert.equal(ui.button("Configure formalization").props.className, "btn primary");
    assert.match(textOf(ui.root()), /Next step: Configure formalization to translate the whole paper/);
  } finally { ui.dispose(); }
});
