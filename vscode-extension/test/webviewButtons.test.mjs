/** Exercise actual React button handlers and validate every message at the host boundary. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { readdirSync, readFileSync } from "node:fs";
import ts from "typescript";
import { mount, textOf, act, SidebarView, FormalizationConflictsFixture, FormalizationPreparationFixture, clickedButtons, normalizeProverSnapshot } from "./helpers/webviewFixture.mjs";
import { formalizationFixture, sourceDraftFixture, deliveredDraftFixture } from "./helpers/formalizationFixture.mjs";
import { formalizationConflictFixture } from "./helpers/formalizationConflictFixture.mjs";
import { formalizationCorrectionFixture } from "./helpers/formalizationCorrectionFixture.mjs";

function message(ui, type) { return ui.messages.filter(row => row.type === type).at(-1); }

function hostMessage(value) {
  act(() => { const event = new Event("message"); event.data = value; window.dispatchEvent(event); });
}

for (const status of ["published", "accepted_staged"]) {
  test(`accepted ${status} revision cannot configure an unaccepted draft`, () => {
    const ui = mount("formalization");
    try {
      const snapshot = sourceDraftFixture();
      snapshot.draft_preview = null;
      snapshot.status = status;
      snapshot.can_prove = status === "published";
      hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
      assert.equal(ui.button("Configure unaccepted source draft").props.disabled, true);
      assert.match(textOf(ui.root()), /Select or create a source revision first/);
      assert.equal(ui.view.form.sourceDraftOnly, false);
      ui.click("Configure formalization");
      ui.setForm({ kind: "formalize", sourceDraftOnly: true });
      const option = ui.root().findAllByType("label").find(node => textOf(node).startsWith("Unaccepted source draft")).findByType("input");
      assert.equal(option.props.checked, false);
      assert.equal(option.props.disabled, true);
      ui.click("Start formalization");
      assert.equal(message(ui, "launchFormalization").request.sourceDraftOnly, false);
    } finally { ui.dispose(); }
  });
}

test("unaccepted preview opens exact frozen artifacts and keeps proving disabled", () => {
  const ui = mount("formalization");
  try {
    const snapshot = sourceDraftFixture();
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    assert.match(textOf(ui.root()), /no coverage, translation, publication or proof approval/);
    assert.deepEqual(ui.input("Repair source artifact").findAllByType("option").map(option => option.props.value), ["source-tex"]);
    ui.click("Unaccepted definitions");
    assert.deepEqual(message(ui, "openFormalizationArtifact"), { type: "openFormalizationArtifact", documentId: "paper-1", revisionId: "revision-1", artifactId: "draft-lean", itemId: undefined, pageIndex: undefined, line: undefined });
    ui.click("Draft report");
    assert.equal(message(ui, "openFormalizationArtifact").artifactId, "draft-report");
    assert.equal(ui.button("Start proving accepted statements").props.disabled, true);
    ui.click("Configure unaccepted source draft");
    assert.equal(ui.view.tab, "formalization");
    assert.equal(ui.view.form.sourceDraftOnly, true);
    const option = () => ui.root().findAllByType("label").find(node => textOf(node).startsWith("Unaccepted source draft")).findByType("input");
    assert.equal(option().props.checked, true);
    ui.click("Prove");
    assert.equal(ui.view.form.sourceDraftOnly, false);
    act(() => ui.setView({ tab: "formalization" }));
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    ui.click("Configure formalization");
    assert.equal(ui.view.form.sourceDraftOnly, false);
    assert.equal(option().props.checked, false);
  } finally { ui.dispose(); }
});

test("delivered draft opens the controller artifact and leaves human revision optional", () => {
  const ui = mount("formalization");
  try {
    const snapshot = deliveredDraftFixture();
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    assert.match(textOf(ui.root()), /Delivered draft project/);
    assert.match(textOf(ui.root()), /Human revision is optional for delivery/);
    assert.match(textOf(ui.root()), /2\/2 required source items mapped \(2 total; 0 explicitly excluded\)/);
    assert.match(textOf(ui.root()), /2\/2 required clauses accounted \(2 total; 0 explicitly excluded\) · 1 unresolved clause/);
    assert.match(textOf(ui.root()), /Saved project compilation checked; theorem proofs may remain sorry/);
    assert.match(textOf(ui.root()), /Open the delivered draft project/);
    assert.equal(ui.button("Start proving accepted statements").props.disabled, true);
    ui.click("Open draft project");
    assert.deepEqual(message(ui, "openFormalizationArtifact"), { type: "openFormalizationArtifact", documentId: "paper-1", revisionId: "revision-1", artifactId: "draft-project", itemId: undefined, pageIndex: undefined, line: undefined });
    assert.equal("path" in message(ui, "openFormalizationArtifact"), false);
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "Controller unavailable" });
    assert.equal(ui.button("Open draft project").props.disabled, true);
  } finally { ui.dispose(); }
});

test("incomplete uncompiled draft remains inspectable with full totals and explicit exclusions", () => {
  const ui = mount("formalization");
  try {
    const snapshot = deliveredDraftFixture();
    snapshot.draft_preview.delivery_status = "incomplete";
    Object.assign(snapshot.draft_preview.delivery, { complete: false, coverage_complete: false, compiled: false });
    Object.assign(snapshot.draft_preview.delivery.counts, { source_items: 4, required_source_items: 3, excluded_source_items: 1,
      known_clauses: 4, required_known_clauses: 3, excluded_known_clauses: 1 });
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    assert.match(textOf(ui.root()), /draft incomplete/);
    assert.match(textOf(ui.root()), /2\/3 required source items mapped \(4 total; 1 explicitly excluded\)/);
    assert.match(textOf(ui.root()), /2\/3 required clauses accounted \(4 total; 1 explicitly excluded\)/);
    assert.match(textOf(ui.root()), /Project compilation is unverified/);
    assert.equal(ui.button("Open draft project").props.disabled, false);
    ui.click("Open draft project");
    assert.equal(message(ui, "openFormalizationArtifact").artifactId, "draft-project");
    assert.equal(ui.button("Start proving accepted statements").props.disabled, true);
  } finally { ui.dispose(); }
});

test("Formalization launches its frozen source and resumes only the matching stopped tracked run", () => {
  const ui = mount("formalization");
  try {
    const snapshot = sourceDraftFixture();
    const runId = "formalize-vscode-run-1234";
    ui.update(f => {
      f.app.formalizationWorkspace = { id: "workspace-1", generation: 1, root: "/fixture" };
      const stopped = { ...f.app.runs[0], id: "stopped-1", runId, projectRoot: "/fixture", status: "stopped",
        request: { ...f.view.form, kind: "formalize", target: snapshot.source.path, sourceDraftOnly: true },
        formalizationSource: { documentId: snapshot.document_id, path: snapshot.source.path } };
      f.app.runs = [stopped, { ...stopped, id: "wrong-root", projectRoot: "/another" }, { ...stopped, id: "active", status: "running" },
        { ...stopped, id: "wrong-document", formalizationSource: { ...stopped.formalizationSource, documentId: "another-paper" } },
        { ...stopped, id: "wrong-target", request: { ...stopped.request, target: "another.tex" } }];
    });
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    ui.click("Configure unaccepted source draft");
    assert.equal(ui.input("Frozen source target").props.readOnly, true);
    assert.equal(ui.button("Create unaccepted source draft").props.disabled, true);
    assert.equal(ui.button(`Resume ${runId}`).props.disabled, true);
    ui.update(f => { f.app.runs = f.app.runs.filter(run => run.id !== "active"); });
    ui.click("Create unaccepted source draft");
    const launch = message(ui, "launchFormalization");
    assert.equal(launch.documentId, snapshot.document_id);
    assert.equal(launch.revisionId, snapshot.revision_id);
    assert.equal(launch.request.kind, "formalize");
    assert.equal(launch.request.target, snapshot.source.path);
    assert.equal(launch.request.sourceDraftOnly, true);
    assert.equal(ui.buttons().filter(button => textOf(button).startsWith("Resume formalize-vscode-")).length, 1);
    ui.click(`Resume ${runId}`);
    const resume = message(ui, "resumeFormalization");
    assert.equal(resume.trackedRunId, "stopped-1");
    assert.equal(resume.documentId, snapshot.document_id);
    assert.equal(resume.revisionId, snapshot.revision_id);
    assert.equal(resume.request.sourceDraftOnly, true);
    ui.click("Prove");
    assert.equal(ui.view.form.sourceDraftOnly, false);
    assert.equal(ui.view.form.target, "");
    assert.equal(message(ui, "launch"), undefined);
  } finally { ui.dispose(); }
});

test("the Prove tab requires an explicit selection after restoring a retired workflow", () => {
  const ui = mount("launch", { view: { tab: "launch", form: { kind: "golf", target: "Old.lean", provider: "lemex", model: "fixture-model", prompt: "Old intent" } } });
  try {
    assert.match(textOf(ui.root()), /saved golf workflow is no longer available/);
    assert.equal(ui.button("Start proving").props.disabled, true);
    assert.equal(ui.view.form.target, "");
    ui.click("Prove");
    assert.equal(ui.view.workflowSelectionRequired, false);
    assert.equal(ui.button("Start proving").props.disabled, false);
    ui.click("Start proving");
    assert.equal(message(ui, "launch").request.kind, "prove");
    assert.equal(message(ui, "launch").request.target, "");
    assert.equal(message(ui, "launch").request.prompt, "");
  } finally { ui.dispose(); }
});

test("a retired sidebar intent cannot quick-launch before explicit workflow configuration", () => {
  const ui = mount("launch", { view: { tab: "launch", form: { kind: "review", target: "Old.lean", prompt: "Review only" } } }, SidebarView);
  try {
    assert.equal(ui.button("Prove").props.disabled, true);
    ui.click("Configure Prove");
    assert.deepEqual(message(ui, "openDashboard"), { type: "openDashboard", workflow: "prove" });
    assert.equal(message(ui, "launch"), undefined);
    assert.equal(ui.view.form.target, "");
    assert.equal(ui.view.form.prompt, "");
  } finally { ui.dispose(); }
});

test("sidebar quick proving uses its saved Prove configuration while Formalization is active", () => {
  const ui = mount("formalization", {}, SidebarView);
  try {
    ui.setForm({ kind: "prove", target: "Proofs/Goal.lean", provider: "claude", model: "claude-model", cleanRoom: true });
    ui.setForm({ kind: "formalize", target: "paper/main.tex", provider: "codex", model: "codex-model", sourceDraftOnly: true });
    ui.click("Prove");
    const request = message(ui, "launch").request;
    assert.equal(request.kind, "prove");
    assert.equal(request.target, "Proofs/Goal.lean");
    assert.equal(request.provider, "claude");
    assert.equal(request.model, "claude-model");
    assert.equal(request.cleanRoom, true);
    assert.equal(request.sourceDraftOnly, false);
    assert.equal(ui.view.form.kind, "formalize");
    assert.equal(ui.view.form.target, "paper/main.tex");
    assert.equal(ui.view.form.sourceDraftOnly, true);
  } finally { ui.dispose(); }
});

test("conditional interpretation retains a closed handoff and resets verification on revision change", () => {
  const ui = mount("formalization");
  try {
    const snapshot = formalizationFixture();
    snapshot.can_prove = false;
    snapshot.diagnostics = [{ id: "conditional-1", code: "unresolved_conditional", message: "Unknown conditional", blocking: true, can_resolve_command: false, can_resolve_conditional: true, source_refs: [{ artifact_id: "source-tex", file: "paper/main.tex", line_start: 1, line_end: 2 }] }];
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    assert.equal(ui.button("Record conditional interpretation").props.disabled, true);
    ui.change(ui.input("Conditional interpretation for conditional-1"), "Both alternatives remain for independent review");
    ui.change(ui.input("Conditional evidence for conditional-1"), "Compared original TeX and PDF");
    ui.change(ui.input("Verified all occurrences for conditional-1"), true);
    ui.click("Record conditional interpretation");
    const sent = message(ui, "formalizationAction");
    assert.equal(sent.revisionId, "revision-1");
    assert.equal(sent.action.type, "resolve_conditional");
    assert.equal(ui.button("Start proving accepted statements").props.disabled, true);
    const changed = { ...snapshot, revision_id: "revision-new" };
    hostMessage({ type: "formalizationState", documentId: changed.document_id, snapshot: changed, error: "" });
    assert.equal(ui.input("Verified all occurrences for conditional-1").props.checked, false);
    assert.equal(ui.button("Record conditional interpretation").props.disabled, true);
    assert.match(textOf(ui.root()), /does not select or exclude a branch/);
  } finally { ui.dispose(); }
});

test("formalization inventory exposes distinct evidence and revision-bound repair and prove controls", () => {
  const ui = mount("formalization");
  try {
    assert.ok(message(ui, "listFormalizations"));
    ui.click("Choose TeX or PDF"); assert.equal(message(ui, "inspectFormalization").sourceKind, "file");
    ui.click("Choose TeX project"); assert.equal(message(ui, "inspectFormalization").sourceKind, "folder");
    ui.click("Refresh inventory");
    const snapshot = formalizationFixture();
    let revision = 1;
    const acknowledge = () => {
      const previousRevisionId = snapshot.revision_id;
      snapshot.revision_id = `revision-${++revision}`;
      hostMessage({ type: "formalizationActionCommitted", documentId: snapshot.document_id, previousRevisionId, revisionId: snapshot.revision_id });
      hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot: structuredClone(snapshot), error: "" });
      ui.click("Dismiss formalization status");
    };
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    assert.match(textOf(ui.root()), /1\/2 included items reviewed · 1 excluded · 1 unresolved regions/);
    assert.match(textOf(ui.root()), /correction_needed/); assert.match(textOf(ui.root()), /conditional/);
    ui.click(ui.buttons().find(button => textOf(button) === "Doubling"));
    ui.click("paper/main.tex · line 2"); assert.equal(message(ui, "openFormalizationArtifact").artifactId, "source-tex");
    ui.click("Paper.double"); assert.equal(message(ui, "openFormalizationArtifact").artifactId, "lean-1");
    ui.click("Open PDF: paper.pdf"); assert.equal(message(ui, "openFormalizationArtifact").artifactId, "source-pdf");
    hostMessage({ type: "formalizationSourceSelection", documentId: "paper-1", revisionId: "revision-1", artifactId: "source-tex", lineStart: 3, lineEnd: 3 });
    const candidate = ui.buttons().find(button => textOf(button) === "Doubling" && button.props.className === "btn");
    ui.click(candidate);
    ui.change(ui.input("New source item title"), "Missed statement");
    ui.click("Submit repair for revision-1");
    assert.equal(message(ui, "formalizationAction").action.type, "add_item");
    assert.equal(ui.button("Submit repair for revision-1").props.disabled, true);
    assert.equal(ui.button("Start proving accepted statements").props.disabled, true);
    acknowledge();
    ui.change(ui.input("Repair kind"), "scope");
    ui.change(ui.input("Repair reason"), "Open problem excluded by author");
    ui.click(`Submit repair for ${snapshot.revision_id}`);
    assert.deepEqual(message(ui, "formalizationAction").action, { type: "set_scope", item_id: "item-1", scope: "excluded", reason: "Open problem excluded by author" });
    acknowledge();
    ui.change(ui.input("Repair kind"), "boundary");
    ui.click(`Submit repair for ${snapshot.revision_id}`);
    assert.equal(message(ui, "formalizationAction").action.type, "update_boundary");
    acknowledge();
    ui.change(ui.input("Repair kind"), "mapping");
    ui.change(ui.input("Corrected Lean mappings"), "Main/Paper.lean | Paper.double");
    ui.click(`Submit repair for ${snapshot.revision_id}`);
    assert.equal(message(ui, "formalizationAction").action.type, "set_mapping");
    acknowledge();
    ui.change(ui.input("Reason for region-1"), "Paragraph supplies notation");
    ui.click("Record region disposition");
    assert.equal(message(ui, "formalizationAction").action.classification, "supporting_context");
    acknowledge();
    ui.click("Check currentness");
    assert.equal(message(ui, "checkFormalization").revisionId, snapshot.revision_id);
    assert.equal(ui.button("Check currentness").props.disabled, true);
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot: { ...structuredClone(snapshot), currentness: "checked", source_currentness: "checked" }, error: "" });
    ui.click("Start proving accepted statements");
    assert.equal(message(ui, "proveFormalization").revisionId, snapshot.revision_id);
    ui.click("Configure formalization");
    assert.equal(ui.view.tab, "formalization"); assert.equal(ui.view.form.target, snapshot.source.path);
    act(() => ui.setView({ tab: "formalization" }));
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot: null, error: "Revision changed" });
    assert.equal(ui.button("Start proving accepted statements").props.disabled, true);
  } finally { ui.dispose(); }
});

test("native source readings and paged clause checklists navigate evidence without recording approval", () => {
  const ui = mount("formalization");
  try {
    const snapshot = formalizationFixture();
    snapshot.currentness = "historical";
    snapshot.diagnostics = Array.from({ length: 26 }, (_, index) => ({
      id: `native-${index}`, code: "unresolved_command", message: `Source notation ${index}`, blocking: true, native_interpretation_required: true, can_resolve_command: true,
      source_refs: [{ artifact_id: "source-tex", file: "paper/main.tex", line_start: index + 1, text: `Frozen source ${index}` }],
    }));
    snapshot.items[0].known_obligations = Array.from({ length: 26 }, (_, index) => ({
      id: `clause-${index}`, description: `Source requirement ${index}`, reason: "Explicit source requirement.",
      source_refs: [{ artifact_id: "source-tex", file: "paper/main.tex", line_start: index + 1 }],
      dispositions: [{ unit_id: "unit-core", disposition: "pending_proof", declaration_names: ["Paper.requirement"], rationale: "A complete named proposition remains to be proved." }],
      verdicts: [],
    }));
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    assert.match(textOf(ui.root()), /26 pending native interpretation/);
    ui.change(ui.input("Source issue filter"), "interpretations");
    ui.click("Inspect pending interpretation native-0, occurrence 1");
    assert.equal(message(ui, "openFormalizationArtifact").line, 1);
    ui.click("Next source issues page");
    assert.match(textOf(ui.root()), /Showing 26 of 26 source issues/);
    ui.click("Previous source issues page");
    ui.click("Inspect source clause clause-0, occurrence 1");
    assert.equal(message(ui, "openFormalizationArtifact").itemId, "item-1");
    ui.click("Next source clauses page");
    assert.match(textOf(ui.root()), /Showing 26 of 26 source clauses/);
    ui.click("Previous source clauses page");
    assert.equal(message(ui, "formalizationAction"), undefined);
    assert.equal(ui.buttons().some(button => textOf(button) === "Record command interpretation"), false);
  } finally { ui.dispose(); }
});

test("local PDF page and text selections navigate to candidate items and keep missing matches visible", async () => {
  const ui = mount("formalization");
  const previousFetch = globalThis.fetch;
  const previousWorker = globalThis.Worker;
  try {
    window.__LEANFLOW_PDF__ = { worker: "vscode-resource:worker", fonts: "vscode-resource:fonts/", cmaps: "vscode-resource:cmaps/", iccs: "vscode-resource:iccs/" };
    globalThis.fetch = async uri => { assert.equal(uri, "vscode-resource:worker"); return { ok: true, text: async () => "// Local fixture worker" }; };
    globalThis.Worker = class { terminate() {} };
    const snapshot = formalizationFixture();
    snapshot.source.kind = "pdf";
    snapshot.source.path = "paper.pdf";
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    await act(async () => {
      hostMessage({ type: "formalizationPdf", pdf: { documentId: "paper-1", revisionId: "revision-1", artifactId: "source-pdf", pageIndex: 0, base64: "JVBERg==" } });
      await new Promise(resolve => setTimeout(resolve, 20));
    });
    assert.equal(ui.pdfOptions.useWorkerFetch, false);
    assert.equal(ui.pdfOptions.enableXfa, false);
    await act(async () => { ui.click("Next page"); await Promise.resolve(); });
    assert.equal(ui.input("Physical PDF page").props.value, 2);
    await act(async () => { ui.click("Previous page"); await Promise.resolve(); });
    window.getSelection = () => ({ toString: () => "Define doubling" });
    act(() => ui.root().findByProps({ className: "formalization-pdf-page" }).props.onMouseUp());
    assert.match(textOf(ui.root()), /Selected passage candidates/);
    const pdfCandidate = ui.root().findByProps({ "aria-label": "Frozen PDF source" }).findAllByType("button").find(button => textOf(button) === "Doubling");
    ui.click(pdfCandidate);
    ui.click("Show all page items");
    window.getSelection = () => ({ toString: () => "A missing statement" });
    act(() => ui.root().findByProps({ className: "formalization-pdf-page" }).props.onMouseUp());
    assert.match(textOf(ui.root()), /No linked items match this passage/);
    assert.equal(ui.input("Source passage").props.value, "A missing statement");
  } finally { ui.dispose(); globalThis.fetch = previousFetch; globalThis.Worker = previousWorker; }
});

test("companion PDF links stay approximate and cannot replace canonical TeX boundaries", async () => {
  const ui = mount("formalization");
  const previousFetch = globalThis.fetch;
  const previousWorker = globalThis.Worker;
  try {
    window.__LEANFLOW_PDF__ = { worker: "vscode-resource:worker", fonts: "vscode-resource:fonts/", cmaps: "vscode-resource:cmaps/", iccs: "vscode-resource:iccs/" };
    globalThis.fetch = async () => ({ ok: true, text: async () => "// Local fixture worker" });
    globalThis.Worker = class { terminate() {} };
    const snapshot = formalizationFixture();
    snapshot.items[2].source_refs = [{ artifact_id: "source-pdf", file: "paper.pdf", precision: "unavailable", text: "" }];
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    await act(async () => {
      hostMessage({ type: "formalizationPdf", pdf: { documentId: "paper-1", revisionId: "revision-1", artifactId: "source-pdf", pageIndex: 0, base64: "JVBERg==" } });
      await new Promise(resolve => setTimeout(resolve, 20));
    });
    assert.match(textOf(ui.root()), /PDF is a visual companion/);
    assert.match(textOf(ui.root()), /approximate/);
    const artifacts = ui.input("Repair source artifact").findAllByType("option").map(option => option.props.value);
    assert.deepEqual(artifacts, ["source-tex"]);
    window.getSelection = () => ({ toString: () => "Define doubling" });
    act(() => ui.root().findByProps({ className: "formalization-pdf-page" }).props.onMouseUp());
    assert.match(textOf(ui.root()), /Selected passage candidates/);
    assert.equal(ui.root().findAllByProps({ "aria-label": "Source passage" }).length, 0);
    ui.click("Open problem");
    assert.match(textOf(ui.root()), /No page match is available/);
    assert.match(textOf(ui.root()), /displayed page is not a claimed match/);
  } finally { ui.dispose(); globalThis.fetch = previousFetch; globalThis.Worker = previousWorker; }
});

test("PDF pane resize rerenders canvas and text at one fitted scale without changing source selection", async () => {
  const ui = mount("formalization");
  const previousFetch = globalThis.fetch;
  const previousWorker = globalThis.Worker;
  try {
    window.__LEANFLOW_PDF__ = { worker: "vscode-resource:worker", fonts: "vscode-resource:fonts/", cmaps: "vscode-resource:cmaps/", iccs: "vscode-resource:iccs/" };
    globalThis.fetch = async () => ({ ok: true, text: async () => "// Local fixture worker" });
    globalThis.Worker = class { terminate() {} };
    const snapshot = formalizationFixture(); snapshot.source.kind = "pdf";
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    await act(async () => {
      hostMessage({ type: "formalizationPdf", pdf: { documentId: "paper-1", revisionId: "revision-1", artifactId: "source-pdf", pageIndex: 0, base64: "JVBERg==" } });
      await new Promise(resolve => setTimeout(resolve, 20));
    });
    assert.equal(ui.pdfRenderedViewports.at(-1).width, 800);
    await act(async () => {
      ui.pdfNodes.scroll.clientWidth = 280;
      for (const observer of ui.resizeObservers) if (!observer.disconnected && observer.targets.includes(ui.pdfNodes.scroll)) observer.callback([]);
      await new Promise(resolve => setTimeout(resolve, 10));
    });
    const rendered = ui.pdfRenderedViewports.at(-1);
    assert.equal(rendered.width, 280);
    assert.equal(rendered.height, 420);
    assert.equal(ui.pdfTextViewports.at(-1), rendered);
    assert.equal(ui.pdfNodes.canvas.style.width, "280px");
    assert.equal(ui.pdfNodes.canvas.style.height, "420px");
    assert.equal(ui.pdfNodes.page.style.width, "280px");
    window.getSelection = () => ({ toString: () => "Define doubling" });
    act(() => ui.root().findByProps({ className: "formalization-pdf-page" }).props.onMouseUp());
    assert.equal(ui.input("Physical PDF page").props.value, 1);
    assert.equal(ui.input("Source passage").props.value, "Define doubling");
    assert.match(textOf(ui.root()), /Selected passage candidates/);
  } finally { ui.dispose(); globalThis.fetch = previousFetch; globalThis.Worker = previousWorker; }
});

test("PDF transcription replacement requires full-page verification and retains repairable detected claims", () => {
  const ui = mount("formalization");
  try {
    const snapshot = formalizationFixture();
    snapshot.source.kind = "pdf";
    snapshot.source.path = "paper.pdf";
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    ui.click("Doubling");
    ui.change(ui.input("Repair kind"), "transcribe");
    ui.change(ui.input("Complete page transcription"), "Theorem: every integer x <= 0 satisfies the bound. Includes all raster clauses.");
    ui.change(ui.input("Repair reason"), "Checked the complete rendered frozen page");
    assert.equal(ui.button("Submit repair for revision-1").props.disabled, true);
    act(() => ui.input("Full transcription checked against frozen page").props.onChange({ target: { checked: true } }));
    assert.equal(ui.button("Submit repair for revision-1").props.disabled, false);
    ui.click("Submit repair for revision-1");
    assert.deepEqual(message(ui, "formalizationAction").action, {
      type: "replace_page_transcription", page_index: 0,
      text: "Theorem: every integer x <= 0 satisfies the bound. Includes all raster clauses.",
      reason: "Checked the complete rendered frozen page", verified: true,
    });
    assert.match(textOf(ui.root()), /affected statement boundaries, mappings and proof permission/);
    ui.change(ui.input("Complete page transcription"), "A later correction");
    assert.equal(ui.input("Full transcription checked against frozen page").props.checked, false);
    assert.equal(ui.button("Submit repair for revision-1").props.disabled, true);
  } finally { ui.dispose(); }
});

test("mapping repair exposes imported reuse and complete shared source groups", () => {
  const ui = mount("formalization");
  try {
    const snapshot = formalizationFixture();
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    ui.click("Doubling");
    ui.change(ui.input("Repair kind"), "mapping");
    ui.change(ui.input("Corrected Lean mappings"), "imports:Init | Nat.add_comm");
    ui.change(ui.input("Repair reason"), "Reuse the existing library identity for both statements");
    const other = snapshot.items.find(item => item.scope !== "excluded" && item.id !== "item-1");
    act(() => ui.input(`Map source claim ${other.title}`).props.onChange({ target: { checked: true } }));
    ui.change(ui.input("Mapping review unit ID"), "shared-unit");
    ui.click("Submit repair for revision-1");
    const action = message(ui, "formalizationAction").action;
    assert.equal(action.type, "set_mapping");
    assert.equal(action.unit_id, "shared-unit");
    assert.deepEqual(action.source_ids, ["item-1", other.id]);
    assert.deepEqual(action.lean_refs, [{ file: "", imports: ["Init"], name: "Nat.add_comm" }]);
  } finally { ui.dispose(); }
});

test("frozen extraction retry explains reopened authority and requires a recorded reason", () => {
  const ui = mount("formalization");
  try {
    const snapshot = formalizationFixture();
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    ui.change(ui.input("Repair kind"), "retry");
    assert.match(textOf(ui.root()), /rereads the unchanged frozen source/);
    assert.match(textOf(ui.root()), /removes current mappings, reviews and proof permissions/);
    assert.equal(ui.button("Submit repair for revision-1").props.disabled, true);
    ui.change(ui.input("Repair reason"), "PDF metadata tools are now installed");
    ui.click("Submit repair for revision-1");
    assert.deepEqual(message(ui, "formalizationAction").action, { type: "retry_extraction", reason: "PDF metadata tools are now installed" });
  } finally { ui.dispose(); }
});

test("publication recovery inspects exact versions and requires reconciliation before keeping current files", () => {
  const ui = mount("formalization", {}, FormalizationConflictsFixture);
  try {
    assert.ok(message(ui, "listFormalizationConflicts"));
    const snapshot = formalizationConflictFixture();
    hostMessage({ type: "formalizationConflicts", snapshot, error: "" });
    ui.click("Inspect preserved version: Retained concurrent save");
    assert.deepEqual(message(ui, "openFormalizationConflictVersion"), { type: "openFormalizationConflictVersion", transactionId: "transaction-1", observationId: "observation-1", versionId: "version-retained" });
    hostMessage({ type: "formalizationConflictPdf", transactionId: "transaction-1", observationId: "observation-1", versionId: "version-retained", pdf: { documentId: "transaction-1", revisionId: "observation-1", artifactId: "version-retained", pageIndex: 0, base64: "" } });
    assert.match(textOf(ui.root()), /frozen publication version bound to the observed file hash/);
    assert.equal(ui.button("Keep reconciled current files and archive resolution").props.disabled, true);
    ui.change(ui.input("Publication reconciliation reason transaction-1"), "Merged concurrent save into current theorem");
    ui.change(ui.input("Reconciled publication transaction-1"), true);
    ui.click("Keep reconciled current files and archive resolution");
    assert.deepEqual(message(ui, "resolveFormalizationConflict"), { type: "resolveFormalizationConflict", transactionId: "transaction-1", observationId: "observation-1", reason: "Merged concurrent save into current theorem" });
    hostMessage({ type: "formalizationConflicts", snapshot: null, error: "Publication versions changed" });
    assert.match(textOf(ui.root()), /Refresh and inspect the versions again/);
    assert.equal(ui.button("Keep reconciled current files and archive resolution").props.disabled, true);
    ui.click("Refresh preserved publication versions");
    snapshot.transactions[0].observation_id = "observation-2";
    hostMessage({ type: "formalizationConflicts", snapshot: structuredClone(snapshot), error: "" });
    assert.doesNotMatch(textOf(ui.root()), /frozen publication version bound to the observed file hash/);
    assert.equal(ui.input("Reconciled publication transaction-1").props.checked, false);
    snapshot.transactions[0].state = "RESOLVED";
    snapshot.transactions[0].resolutions = [{ reason: "All versions archived", resolution_id: "resolution-1" }];
    hostMessage({ type: "formalizationConflicts", snapshot: structuredClone(snapshot), error: "", resolvedTransactionId: "transaction-1" });
    assert.match(textOf(ui.root()), /mathematical acceptance must be re-established/);
    assert.match(textOf(ui.root()), /All versions archived/);
  } finally { ui.dispose(); }
});

test("unavailable publication history is acknowledged without claiming reconstructed versions", () => {
  const ui = mount("formalization", {}, FormalizationConflictsFixture);
  try {
    const snapshot = formalizationConflictFixture();
    Object.assign(snapshot.transactions[0], {
      unavailable_journal: true, planned_versions: null, versions: [], version_count: 0,
      revoked_revision_ids: ["revision-1"], conflicts: ["Publication journal unavailable"],
    });
    hostMessage({ type: "formalizationConflicts", snapshot, error: "" });
    assert.match(textOf(ui.root()), /original write plan cannot be reconstructed/);
    assert.match(textOf(ui.root()), /acknowledge the unavailable publication history/);
    assert.equal(ui.buttons().some(button => textOf(button).startsWith("Inspect preserved version:")), false);
    assert.equal(ui.button("Keep current files and acknowledge unavailable history").props.disabled, true);
    ui.change(ui.input("Publication reconciliation reason transaction-1"), "Inspected current sources and acknowledged unavailable history");
    ui.change(ui.input("Reconciled publication transaction-1"), true);
    ui.click("Keep current files and acknowledge unavailable history");
    assert.deepEqual(message(ui, "resolveFormalizationConflict"), {
      type: "resolveFormalizationConflict", transactionId: "transaction-1", observationId: "observation-1",
      reason: "Inspected current sources and acknowledged unavailable history",
    });
  } finally { ui.dispose(); }
});

test("source-only preparation plans dependencies then starts and cancels an owned setup", () => {
  const snapshot = formalizationFixture(); snapshot.preparation = { status: "preparation_required", current: false };
  const ui = mount("formalization", { sourceSnapshot: snapshot }, FormalizationPreparationFixture);
  try {
    ui.change(ui.input("Formalization preparation preset"), "lean");
    ui.click("Inspect dependency and project plan");
    assert.deepEqual(message(ui, "planFormalizationPreparation"), { type: "planFormalizationPreparation", documentId: "paper-1", revisionId: "revision-1", preset: "lean" });
    hostMessage({ type: "formalizationPreparation", documentId: "paper-1", revisionId: "revision-1", status: "planned", message: "Plan inspected", error: "", plan: { toolchain: "leanprover/lean4:fixture", layout: { library: "Main", root_file: "Main.lean" }, required_imports: ["Init"], packages: [], slice_source_ids: ["item-1", "item-2"] } });
    assert.match(textOf(ui.root()), /Representative source claims: 2/);
    ui.change(ui.input("Formalization preparation plan overrides"), '{"required_imports":["Init"]}');
    ui.click("Prepare dependencies and project");
    assert.deepEqual(message(ui, "prepareFormalization").overrides, { required_imports: ["Init"] });
    assert.equal(ui.button("Cancel project preparation").props.disabled, true);
    hostMessage({ type: "formalizationPreparation", documentId: "paper-1", revisionId: "revision-1", status: "preparing", cancellable: true, message: "Owned preparation started", error: "" });
    ui.click("Cancel project preparation");
    assert.deepEqual(message(ui, "cancelFormalizationPreparation"), { type: "cancelFormalizationPreparation", documentId: "paper-1", revisionId: "revision-1" });
    assert.equal(ui.button("Cancel project preparation").props.disabled, true);
    assert.equal(ui.button("Prepare dependencies and project").props.disabled, true);
    assert.match(textOf(ui.root()), /Waiting for safe cleanup/);
    hostMessage({ type: "formalizationPreparation", documentId: "paper-1", revisionId: "revision-1", status: "cancelled", message: "Cancelled with history preserved", error: "" });
    ui.change(ui.input("Formalization preparation plan overrides"), "[");
    assert.equal(ui.button("Prepare dependencies and project").props.disabled, true);
  } finally { ui.dispose(); }
});

test("a source-only workspace can inspect before baseline setup and retains its document when preparation finishes", () => {
  const ui = mount("formalization");
  try {
    ui.update(f => { f.app.project = { found: false, root: "", label: "Source folder" }; });
    assert.equal(ui.button("Choose TeX or PDF").props.disabled, false);
    ui.click("Choose TeX or PDF");
    const snapshot = formalizationFixture(); snapshot.preparation.current = false;
    hostMessage({ type: "formalizationState", documentId: "paper-1", snapshot, error: "" });
    assert.equal(ui.button("Configure formalization").props.disabled, false);
    ui.click("Configure formalization");
    assert.match(textOf(ui.root()), /Complete Project setup above before starting native dependency planning/);
    assert.equal(ui.button("Start formalization").props.disabled, true);
    const prepared = structuredClone(snapshot); prepared.preparation.current = true;
    hostMessage({ type: "formalizationState", documentId: "paper-1", snapshot: prepared, error: "" });
    assert.equal(ui.button("Start formalization").props.disabled, false);
    ui.update(f => { f.app.project = { found: true, root: "/prepared/source", label: "Prepared source" }; });
    assert.match(textOf(ui.root()), /1\/2 included items reviewed/);
    assert.equal(ui.input("Formalization document").props.value, "paper-1");
    assert.equal(ui.button("Configure formalization").props.disabled, false);
  } finally { ui.dispose(); }
});

test("infrastructure recovery remains accessible when document snapshots are blocked", () => {
  const ui = mount("formalization", {}, FormalizationConflictsFixture);
  try {
    assert.ok(message(ui, "listFormalizationPreparationRecovery"));
    const transaction = { transaction_id: "prep-1", observation_id: "obs-1", history_id: "history-1", state: "CONFLICTED", active: true,
      destinations: { ".lake": "a".repeat(64) }, retained_slots: { ".leanflow/preparation-transactions/prep-1/displaced/lake": "b".repeat(64) }, conflicts: ["Concurrent infrastructure save"], resolutions: [] };
    hostMessage({ type: "formalizationPreparationRecovery", snapshot: { schema_version: 1, transactions: [transaction] }, error: "", busy: false });
    ui.click("Inspect infrastructure recovery");
    assert.equal(ui.button("Inspect infrastructure recovery").props.disabled, true);
    assert.equal(ui.button("Cancel infrastructure recovery").props.disabled, true);
    hostMessage({ type: "formalizationPreparationRecovery", snapshot: { schema_version: 1, transactions: [transaction] }, error: "", busy: false });
    ui.change(ui.input("Infrastructure reconciliation reason prep-1"), "Inspected directory entries locally and reconciled current configuration");
    ui.change(ui.input("Inspected infrastructure prep-1"), true);
    ui.click("Keep current infrastructure and archive resolution");
    assert.deepEqual(message(ui, "resolveFormalizationPreparation"), { type: "resolveFormalizationPreparation", transactionId: "prep-1", observationId: "obs-1", reason: "Inspected directory entries locally and reconciled current configuration" });
    hostMessage({ type: "formalizationPreparationRecovery", snapshot: null, error: "", busy: true, cancellable: true, message: "Controller recovery started" });
    ui.click("Cancel infrastructure recovery");
    assert.ok(message(ui, "cancelFormalizationRecovery"));
    assert.equal(ui.button("Cancel infrastructure recovery").props.disabled, true);
    hostMessage({ type: "formalizationPreparationRecovery", snapshot: null, error: "Preparation versions changed", busy: false });
    assert.equal(ui.button("Keep current infrastructure and archive resolution").props.disabled, true);
    ui.click("Recover interrupted project preparation");
    assert.ok(message(ui, "recoverFormalizationPreparation"));
  } finally { ui.dispose(); }
});

test("open claim proof permission is explicit, revocable and tied to the viewed revision", () => {
  const ui = mount("formalization");
  try {
    const snapshot = formalizationFixture();
    snapshot.can_prove = false;
    snapshot.items[2].claim_status = "open_question";
    snapshot.items[2].extracted_claim_status = "unknown";
    snapshot.items[2].claim_status_rationale = "The source asks whether this claim holds.";
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    ui.click("Open problem");
    assert.match(textOf(ui.root()), /Source claim: open_question · extraction: unknown/);
    assert.match(textOf(ui.root()), /The source asks whether this claim holds/);
    assert.equal(ui.button("Allow proof attempt for this open claim").props.disabled, true);
    ui.change(ui.input("Proof permission reason"), "Try this open problem as explicit research");
    ui.click("Allow proof attempt for this open claim");
    assert.deepEqual(message(ui, "formalizationAction"), { type: "formalizationAction", documentId: "paper-1", revisionId: "revision-1", action: { type: "authorize_proof", item_id: "item-3", authorized: true, reason: "Try this open problem as explicit research" } });
    assert.equal(ui.button("Start proving accepted statements").props.disabled, true);
    const next = structuredClone(snapshot);
    next.revision_id = "revision-3";
    next.items[2].proof_authorization = { actor: "user", authorized: true, reason: "Try this open problem as explicit research" };
    hostMessage({ type: "formalizationState", documentId: next.document_id, snapshot: next, error: "" });
    assert.equal(ui.input("Proof permission reason").props.value, "");
    assert.match(textOf(ui.root()), /Explicitly allowed/);
    assert.match(textOf(ui.root()), /Source claim: open_question/);
    ui.change(ui.input("Proof permission reason"), "Withdraw this research request");
    ui.click("Revoke proof attempt permission");
    assert.deepEqual(message(ui, "formalizationAction"), { type: "formalizationAction", documentId: "paper-1", revisionId: "revision-3", action: { type: "authorize_proof", item_id: "item-3", authorized: false, reason: "Withdraw this research request" } });
    hostMessage({ type: "formalizationState", documentId: next.document_id, snapshot: null, error: "Revision changed" });
    assert.equal(ui.button("Revoke proof attempt permission").props.disabled, true);
  } finally { ui.dispose(); }
});

test("unknown command reconciliation exposes all frozen occurrences and records only an explicit verified interpretation", () => {
  const ui = mount("formalization");
  try {
    const snapshot = formalizationFixture();
    snapshot.can_prove = false;
    snapshot.diagnostics = [
      { id: "diagnostic-1", code: "unresolved_command", message: "Unknown command \\transfer", blocking: true, can_resolve_command: true,
        source_refs: [{ artifact_id: "source-tex", file: "paper/main.tex", line_start: 2 }, { artifact_id: "source-tex", file: "paper/main.tex", line_start: 10 }] },
      { id: "diagnostic-2", code: "missing_include", message: "Missing included source", blocking: true, can_resolve_command: false, source_refs: [] },
    ];
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    ui.click("Occurrence 1: paper/main.tex · line 2");
    assert.equal(message(ui, "openFormalizationArtifact").line, 2);
    ui.click("Occurrence 2: paper/main.tex · line 10");
    assert.equal(message(ui, "openFormalizationArtifact").line, 10);
    assert.equal(ui.button("Record command interpretation").props.disabled, true);
    assert.match(textOf(ui.root()), /cannot be cleared by recording a command interpretation/);
    ui.change(ui.input("Command interpretation for diagnostic-1"), "Transfer is the named operation on the following argument.");
    ui.change(ui.input("Command evidence for diagnostic-1"), "Checked the definition and both rendered occurrences.");
    assert.equal(ui.button("Record command interpretation").props.disabled, true);
    act(() => ui.input("Verified all occurrences for diagnostic-1").props.onChange({ target: { checked: true } }));
    ui.change(ui.input("Command interpretation for diagnostic-1"), "Transfer is the same named operation at both occurrences.");
    assert.equal(ui.input("Verified all occurrences for diagnostic-1").props.checked, false);
    act(() => ui.input("Verified all occurrences for diagnostic-1").props.onChange({ target: { checked: true } }));
    ui.click("Record command interpretation");
    assert.deepEqual(message(ui, "formalizationAction"), { type: "formalizationAction", documentId: "paper-1", revisionId: "revision-1", action: { type: "resolve_command", diagnostic_id: "diagnostic-1", interpretation: "Transfer is the same named operation at both occurrences.", reason: "Checked the definition and both rendered occurrences.", verified: true } });
    assert.equal(ui.button("Start proving accepted statements").props.disabled, true);
    const refreshed = structuredClone(snapshot);
    refreshed.revision_id = "revision-3";
    hostMessage({ type: "formalizationState", documentId: refreshed.document_id, snapshot: refreshed, error: "" });
    assert.equal(ui.input("Command interpretation for diagnostic-1").props.value, "");
    assert.equal(ui.input("Verified all occurrences for diagnostic-1").props.checked, false);
    refreshed.diagnostics[0].resolution = { actor: "user", interpretation: "Named operation", reason: "Both occurrences verified", verified: true };
    refreshed.diagnostics[0].blocking = false;
    refreshed.revision_id = "revision-4";
    refreshed.diagnostics.push({ id: "diagnostic-3", code: "pdf_text_missing", message: "Page transcription repaired", blocking: false, can_resolve_command: false, source_refs: [], resolution: "verified_transcription", resolution_reason: "Checked full page" });
    hostMessage({ type: "formalizationState", documentId: refreshed.document_id, snapshot: refreshed, error: "" });
    assert.match(textOf(ui.root()), /User interpretation: Named operation/);
    assert.match(textOf(ui.root()), /Source repair: verified page transcription · Checked full page/);
    assert.equal(ui.buttons().some(button => textOf(button) === "Record command interpretation"), false);
    assert.equal(ui.button("Update command interpretation").props.disabled, true);
    ui.change(ui.input("Command interpretation for diagnostic-1"), "Corrected named operation");
    ui.change(ui.input("Command evidence for diagnostic-1"), "Rechecked all occurrences after reviewer feedback.");
    act(() => ui.input("Verified all occurrences for diagnostic-1").props.onChange({ target: { checked: true } }));
    ui.click("Update command interpretation");
    assert.equal(message(ui, "formalizationAction").revisionId, "revision-4");
    assert.equal(message(ui, "formalizationAction").action.interpretation, "Corrected named operation");
  } finally { ui.dispose(); }
});

test("checked proof certificate is displayed separately from source review and disables further handoff", () => {
  const ui = mount("formalization");
  try {
    const snapshot = formalizationFixture();
    snapshot.proof_completion = "checked";
    snapshot.proof_revision_id = "proof-revision-1";
    snapshot.items[1].proof_status = "checked";
    snapshot.items[1].review_status = "pass";
    snapshot.can_prove = false;
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    assert.match(textOf(ui.root()), /Proof completion: checked · proof certificate revision proof-revision-1/);
    assert.match(textOf(ui.root()), /All obligations in this handoff have checked proofs/);
    assert.equal(snapshot.items[1].review_status, "pass");
    assert.equal(ui.button("Start proving accepted statements").props.disabled, true);
  } finally { ui.dispose(); }
});

test("a private versus published revision conflict permits explicit selection before repairs or launch", () => {
  const ui = mount("formalization");
  try {
    const snapshot = formalizationFixture();
    snapshot.status = "selection_conflict";
    snapshot.selection_conflict = { message: "A newer published selection conflicts with local work.", private_revision_id: "revision-1", published_revision_id: "revision-2" };
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    assert.match(textOf(ui.root()), /Local working revision revision-1 · published revision revision-2/);
    ui.change(ui.input("New source item title"), "A missed claim");
    assert.equal(ui.button("Submit repair for revision-1").props.disabled, true);
    assert.equal(ui.button("Configure formalization").props.disabled, true);
    assert.equal(ui.button("Start proving accepted statements").props.disabled, true);
    assert.equal(ui.input("Select formalization revision").props.disabled, false);
    ui.change(ui.input("Select formalization revision"), "revision-2");
    assert.deepEqual(message(ui, "formalizationAction"), { type: "formalizationAction", documentId: "paper-1", revisionId: "revision-1", action: { type: "select_revision", revision_id: "revision-2" } });
  } finally { ui.dispose(); }
});

test("terminal check and reviewer errors expose the repair reason without making fresh snapshots stale", () => {
  const ui = mount("formalization");
  try {
    const snapshot = formalizationFixture();
    snapshot.status = "review_error";
    snapshot.receipt_error = "Reviewer authentication unavailable";
    snapshot.can_prove = false;
    snapshot.items[0].review_status = "error";
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    assert.match(textOf(ui.root()), /Review could not finish/);
    assert.match(textOf(ui.root()), /Reviewer authentication unavailable/);
    assert.equal(ui.button("Configure formalization").props.disabled, false);
    ui.change(ui.input("New source item title"), "Corrected source interpretation");
    assert.equal(ui.button("Submit repair for revision-1").props.disabled, false);
    snapshot.status = "check_rejected";
    snapshot.receipt_error = "Unfinished definition: supply its complete construction";
    snapshot.items[0].elaboration_status = "rejected";
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    assert.match(textOf(ui.root()), /Lean draft needs correction/);
    assert.match(textOf(ui.root()), /Unfinished definition: supply its complete construction/);
    assert.equal(ui.button("Start proving accepted statements").props.disabled, true);
    snapshot.status = "check_error";
    snapshot.receipt_error = "Lean checking process timed out";
    snapshot.items[0].elaboration_status = "error";
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    assert.match(textOf(ui.root()), /Lean checking could not finish/);
    assert.match(textOf(ui.root()), /Lean checking process timed out/);
    assert.match(textOf(ui.root()), /configure or resume formalization to retry/);
    assert.doesNotMatch(textOf(ui.root()), /Lean draft needs correction/);
    assert.equal(ui.button("Configure formalization").props.disabled, false);
    assert.equal(ui.button("Start proving accepted statements").props.disabled, true);
  } finally { ui.dispose(); }
});

test("external live jobs expose Stop in the dashboard and sidebar, but stale history does not", () => {
  for (const component of [undefined, SidebarView]) {
    const ui = mount("live", {}, component);
    try {
      ui.update(f => {
        f.app.runs = [];
        f.app.selectedRunId = "external-live";
        f.app.liveStatus = {run_id:"external-live",process_id:42,phase:"planning",workflow_kind:"prove"};
        f.app.history = [{run_id:"external-live",workflow_kind:"prove",updated_at:new Date().toISOString()}];
      });
      const label = component ? "stop" : "Stop";
      ui.click(label);
      assert.deepEqual(message(ui,"stopRun"),{type:"stopRun",id:"external-live"});
      ui.update(f => { f.app.liveStatus.stale_snapshot = true; });
      assert.ok(!ui.buttons().some(node => textOf(node) === label));
    } finally {ui.dispose();}
  }
});

test("storage buttons inspect and collect only through fixed host actions", () => {
  const ui = mount("providers");
  try {
    assert.equal(ui.button("Clean unused storage").props.disabled, true);
    ui.click("Refresh storage");
    assert.deepEqual(message(ui, "refreshStorage"), { type: "refreshStorage" });
    ui.update(f => { f.app.storage = { checking: false, error: "", managedBytes: 1024 ** 3, pinnedBytes: 0, workspaceBytes: 0, freeBytes: 100 * 1024 ** 3, cacheTargetBytes: 20 * 1024 ** 3, minFreeBytes: 10 * 1024 ** 3, automaticCleanup: true }; });
    assert.match(textOf(ui.root()), /Cache target: 20.0 GiB/);
    assert.match(textOf(ui.root()), /Free-space reserve: 10.0 GiB/);
    ui.click("Clean unused storage");
    assert.deepEqual(message(ui, "cleanStorage"), { type: "cleanStorage" });
    ui.update(f => { f.app.storage.checking = true; });
    assert.equal(ui.button("Clean unused storage").props.disabled, true);
    assert.equal(ui.button("Refresh storage").props.disabled, true);
  } finally { ui.dispose(); }
});

test("launch names the inherited provider and keeps setup outside the control label", () => {
  const ui = mount();
  try {
    const option = ui.root().findAllByType("option").find(node => textOf(node) === "Default (Lemex)");
    assert.ok(option, "The effective default must be visible");
    assert.equal(option.props.value, "");
    const setup = ui.button("Manage login, API key, or endpoint");
    for (let parent = setup.parent; parent; parent = parent.parent) assert.notEqual(parent.type, "label");
    ui.click("Refresh models");
    assert.ok(message(ui, "refreshProviders"));
    ui.click(setup);
    assert.equal(ui.view.tab, "providers");
  } finally { ui.dispose(); }
});

test("all dashboard tabs, refresh and dismiss controls respond", () => {
  const ui = mount();
  try {
    for (const label of ["Providers", "Live", "Logs", "Knobs", "Sweeps", "Formalization", "Prove"]) {
      ui.click(new RegExp(`^${label}\\d*$`));
      assert.equal(ui.view.tab, label === "Prove" ? "launch" : label.toLowerCase());
    }
    ui.click("Refresh");
    assert.ok(message(ui, "refresh"));
    ui.update(f => { f.toasts = [{ id: 1, level: "error", message: "Test notification" }]; });
    ui.click("Dismiss notification");
    assert.equal(ui.toasts.length, 0);
  } finally { ui.dispose(); }
});

test("launch preview, browse and start send the current form; invalid launches stay disabled", async () => {
  const ui = mount();
  try {
    ui.click("Browse");
    assert.equal(message(ui, "pickTarget").kind, "prove");
    ui.click("Preview plan");
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 380)); });
    assert.equal(message(ui, "preview").request.provider, "lemex");
    ui.click("Hide plan");
    ui.click("Start proving");
    assert.equal(message(ui, "launch").request.model, "fixture-model");
    ui.update(f => { f.app.project.found = false; });
    assert.equal(ui.button("Start proving").props.disabled, true);
    ui.update(f => { f.app.cli = { ok: false, error: "Missing CLI" }; });
    for (const [label, type] of [["Open install guide", "openExternalDoc"], ["Configure CLI path", "openCliSettings"], ["Check again", "refresh"]]) {
      ui.click(label); assert.ok(message(ui, type));
    }
  } finally { ui.dispose(); }
});

test("unknown provider setup does not invent a default; explicit choices still work", () => {
  const ui = mount();
  try {
    ui.update(f => { f.app.providers = { ...f.app.providers, selected: "", checking: true }; });
    assert.match(textOf(ui.root()), /Default \(checking…\)/);
    ui.update(f => { f.app.providers = { ...f.app.providers, selected: "lemex", checking: false, error: "Failed" }; });
    assert.match(textOf(ui.root()), /Default \(unavailable\)/);
    const select = ui.root().findAllByType("select").find(node => node.findAllByType("option").some(option => option.props.value === "codex"));
    ui.change(select, "claude");
    assert.equal(ui.view.form.provider, "claude");
  } finally { ui.dispose(); }
});

test("provider cards route every setup action to the right provider", () => {
  const ui = mount("providers");
  try {
    ui.click("Refresh setup"); assert.ok(message(ui, "refreshProviders"));
    for (const id of ["codex", "claude", "lemex"]) {
      const card = () => ui.root().findAllByType("section").find(node => node.findAllByProps({ className: "card-title" }).some(title => textOf(title) === ({ codex: "Codex", claude: "Claude Code", lemex: "Lemex" }[id])));
      const click = (label, action) => {
        ui.click(card().findAllByType("button").find(node => textOf(node) === label));
        assert.deepEqual(message(ui, "providerAction"), { type: "providerAction", backend: id, action });
      };
      if (id !== "lemex") click("Sign in", "login");
      click("Add API key", "apiKey");
      ui.update(f => { f.app.providers.backends.find(row => row.id === id).savedKey = true; });
      click("Replace API key", "apiKey"); click("Remove saved key", "removeKey");
      if (id === "lemex") click("Set endpoint", "endpoint");
      click("Open native config", "config");
      ui.click(card().findAllByType("button").find(node => textOf(node) === "Use for next run"));
      assert.equal(ui.view.form.provider, id); assert.equal(ui.view.tab, "launch");
      ui.update(f => { f.view.tab = "providers"; });
    }
    ui.update(f => { f.app.providers.backends[0].installed = false; f.app.providers.error = "Check failed"; });
    assert.equal(ui.buttons().find(node => textOf(node) === "Sign in").props.disabled, true);
    ui.click("Installation guide"); assert.equal(message(ui, "openExternalDoc").topic, "install");
    ui.click("Configure CLI path"); assert.ok(message(ui, "openCliSettings"));
  } finally { ui.dispose(); }
});

test("launch override removal and compatible profile copy preserve allowed settings", () => {
  const ui = mount();
  try {
    ui.update(f => { f.view.form.overrides = { LEANFLOW_PROVER_PARALLELISM: "2" }; });
    ui.click("✕"); assert.deepEqual(ui.view.form.overrides, {});
    ui.update(f => { f.view.form.overrides = { LEANFLOW_PROVER_PARALLELISM: "2" }; });
    ui.click("clear"); assert.deepEqual(ui.view.form.overrides, {});
    ui.update(f => {
      f.app.profiles.profiles[1].overrides.LEANFLOW_INTERNAL_TEST = "1";
      f.view.form.profile = "custom";
      f.view.form.overrides = { LEANFLOW_INTERNAL_TEST: "1" };
    });
    assert.equal(ui.button("Start proving").props.disabled, true);
    ui.click("Drop refused one-off overrides"); assert.deepEqual(ui.view.form.overrides, {});
    ui.click(/^Save a VS Code copy/);
    assert.deepEqual(message(ui, "saveProfile").profile.overrides, { LEANFLOW_PROVER_PARALLELISM: "2" });
  } finally { ui.dispose(); }
});

test("global limits restore a saved profile after making the launch unlimited", () => {
  const ui = mount();
  try {
    ui.update(f => {
      f.app.catalog.groups[0].flags.push({ name: "LEANFLOW_PROVER_AGENT_CALLS", value_type: "int", default: "", minimum: 0, editable: true, extension_editable: true, sensitive: false });
      f.app.profiles.profiles[1].overrides.LEANFLOW_PROVER_AGENT_CALLS = "8";
      f.view.form.profile = "custom";
    });
    const input = () => ui.input("LEANFLOW_PROVER_AGENT_CALLS");
    assert.equal(input().props.value, "8");
    ui.change(input(), "");
    assert.equal(input().props.value, "");
    assert.equal(ui.view.form.overrides.LEANFLOW_PROVER_AGENT_CALLS, "");
    ui.click("Use profile limit");
    assert.equal(input().props.value, "8");
    assert.equal(ui.view.form.overrides.LEANFLOW_PROVER_AGENT_CALLS, undefined);
  } finally { ui.dispose(); }
});

test("role and recovery limit resets restore their own saved allowance", () => {
  const ui = mount();
  try {
    const limits = [["PROVER_SESSIONS", "total prover calls"], ["MAX_RESTARTS", "standard retries per goal"]];
    ui.update(f => {
      for (const [suffix] of limits) {
        const name = `LEANFLOW_PROVER_${suffix}`;
        f.app.catalog.groups[0].flags.push({ name, value_type: "int", default: "", minimum: 0, editable: true, extension_editable: true, sensitive: false });
        f.app.profiles.profiles[1].overrides[name] = "8";
      }
      f.view.form.profile = "custom";
    });
    for (const [suffix, label] of limits) {
      ui.change(ui.input("LEANFLOW_PROVER_MODE"), suffix === "MAX_RESTARTS" ? "standard" : "research");
      const name = `LEANFLOW_PROVER_${suffix}`;
      ui.change(ui.input(name), "");
      assert.equal(ui.view.form.overrides[name], "");
      ui.click(`Restore profile ${label}`);
      assert.equal(ui.input(name).props.value, "8");
      assert.equal(ui.view.form.overrides[name], undefined);
    }
  } finally { ui.dispose(); }
});

test("knob filtering, group expansion, profile save/delete/diff and resets work", () => {
  const ui = mount("knobs");
  try {
    ui.click("Ablatable only"); assert.equal(ui.view.knobAblatableOnly, true);
    ui.click("Ablatable only");
    ui.click("Show plumbing"); assert.match(textOf(ui.root()), /LEANFLOW_INTERNAL_TEST/);
    const group = ui.buttons().find(node => node.props.className?.includes("knob-group"));
    ui.click(group); assert.deepEqual(ui.view.collapsedGroups, ["Prover"]);
    ui.click(group); assert.deepEqual(ui.view.collapsedGroups, []);
    ui.change(ui.input("LEANFLOW_PROVER_PARALLELISM"), "3");
    assert.equal(ui.view.form.overrides.LEANFLOW_PROVER_PARALLELISM, "3");
    ui.change(ui.input("e.g. ablate-negation-probe"), "audit");
    ui.click("Save profile"); assert.equal(message(ui, "saveProfile").profile.name, "audit");
    ui.click("Clear all"); assert.deepEqual(ui.view.form.overrides, {});
    const picker = ui.root().findAllByType("select").find(node => node.findAllByType("option").some(option => textOf(option).startsWith("Load a profile")));
    ui.change(picker, "custom"); assert.equal(ui.view.form.overrides.LEANFLOW_PROVER_PARALLELISM, "2");
    ui.click('Delete "custom"'); assert.equal(message(ui, "deleteProfile").name, "custom");
    ui.click("Compare"); assert.ok(message(ui, "diffProfiles"));
    ui.update(f => { f.view.form.overrides.LEANFLOW_INTERNAL_TEST = "1"; });
    assert.equal(ui.button("Save profile").props.disabled, true);
    ui.click("Drop refused knobs from the working set");
    assert.equal(ui.view.form.overrides.LEANFLOW_INTERNAL_TEST, undefined);
  } finally { ui.dispose(); }
});

test("sweep creation, results, run/resume/reattach, stop, export and deletion are wired", () => {
  const ui = mount("sweeps");
  try {
    ui.click(/^custom /);
    ui.click("Create sweep");
    const matrix = message(ui, "createExperiment").matrix;
    assert.deepEqual(matrix.profiles, ["custom"]);
    const cell = { id: "cell-1", status: "pending", target: "", profile: "custom", model: "fixture-model", repeat: 0, metrics: null };
    ui.update(f => { f.app.experiments = [{ matrix, status: "idle", cells: [cell] }]; });
    ui.click("Results"); assert.match(textOf(ui.root()), /Reproducibility baseline/);
    ui.click("Hide");
    ui.click("Run"); assert.equal(message(ui, "startExperiment").matrixId, matrix.id);
    ui.click("Export"); assert.equal(message(ui, "exportExperiment").matrixId, matrix.id);
    ui.click("Delete"); assert.equal(message(ui, "deleteExperiment").matrixId, matrix.id);
    ui.update(f => { f.app.experiments[0].cells[0].status = "running"; });
    ui.click("Re-attach"); assert.equal(ui.button("Delete").props.disabled, true);
    ui.click("Stop"); assert.equal(message(ui, "stopExperiment").matrixId, matrix.id);
    ui.update(f => { f.app.experiments[0].status = "running"; });
    ui.click("Stop");
    ui.update(f => { f.app.experiments[0].status = "idle"; f.app.experiments[0].cells = [{ ...cell, status: "done" }, { ...cell, id: "cell-2", status: "pending" }]; });
    ui.click("Resume");
  } finally { ui.dispose(); }
});

test("live graph, job artifacts, file diffs and guidance controls act on the selected run", () => {
  const ui = mount("live");
  try {
    ui.click("Stop"); assert.equal(message(ui, "stopRun").id, "tracked-1");
    for (const [label, file] of [["Open DAG", "dag.json"], ["Open PLAN.md", "PLAN.md"], ["Log file", "job.log"], ["Scratch proof", "Scratch.lean"], ["Result report", "result.json"], ["Session report", "report.json"], ["artifact.txt", "artifact.txt"]]) {
      ui.click(label); assert.equal(message(ui, "openProverFile").path, file);
    }
    ui.click("View diff"); assert.equal(message(ui, "openProverFile").baselinePath, "baseline/Main.lean");
    ui.click("Main.lean:7"); assert.equal(message(ui, "openProverFile").line, 7);
    ui.click("Zoom in graph"); ui.click("Zoom out graph"); ui.click("Fit width"); ui.click("Locate selected");
    ui.change(ui.input("Find a theorem"), "helper");
    ui.click("Next match");
    ui.click(ui.buttons().find(node => node.props.className?.includes("prover-graph-card") && node.props.title?.startsWith("helper")));
    ui.click("Tree");
    ui.click(ui.buttons().find(node => node.props.className?.startsWith("prover-node ")));
    ui.change(ui.input("Find a theorem"), "");
    ui.click("Graph");
    ui.click("goal"); ui.click("helper"); ui.click("goal");
    const jobLink = ui.buttons().find(node => node.props.className === "link mono" && textOf(node) === "prover-1");
    ui.click(jobLink); assert.ok(ui.effects.some(row => row[0] === "focus"));
    ui.change(ui.input("Guidance message"), "Try the helper lemma.");
    ui.click("Send guidance");
    const sent = message(ui, "proverMessage");
    assert.equal(sent.runId, "run-1"); assert.equal(sent.message, "Try the helper lemma.");
    assert.equal(ui.button("Sending…").props.disabled, true);
    act(() => { const event = new Event("message"); event.data = { type: "proverMessageResult", ...sent, success: true, error: "" }; event.data.type = "proverMessageResult"; window.dispatchEvent(event); });
    assert.equal(ui.input("Guidance message").props.value, "");
    ui.update(f => { f.view.logSearch = "old unmatched search"; });
    ui.click("Events");
    assert.equal(ui.view.logAgent, "prover-1"); assert.equal(ui.view.logSearch, "");
  } finally { ui.dispose(); }
});

test("empty active formalization logs point to terminal without inventing a preparation phase", () => {
  const ui = mount("logs");
  try {
    ui.update(f => {
      f.app.runs[0].request.kind = "formalize";
      f.events = { ...f.events, "run-1": [], "another-run": [{ type: "agent-output", event_id: "other", run_id: "another-run" }] };
    });
    const hint = /Project preparation and dependency planning may still be running/;
    assert.match(textOf(ui.root()), hint);
    assert.match(textOf(ui.root()), /Check this run’s terminal for setup output; author events appear when the author starts/);
    assert.doesNotMatch(textOf(ui.root()), /No events match this filter|A run that finished before this window opened/);
    ui.update(f => {
      f.events = { ...f.events, "run-1": [{ type: "agent-output", event_id: "recorded", run_id: "run-1", timestamp: "2026-10-15T08:00:00Z", message: "Recorded output" }] };
    });
    ui.click("Reasoning");
    assert.match(textOf(ui.root()), /No events match this filter/);
    assert.match(textOf(ui.root()), /1 events are buffered/);
    assert.doesNotMatch(textOf(ui.root()), hint);

    ui.update(f => { f.events = { ...f.events, "run-1": [] }; f.app.runs[0].status = "stopped"; });
    assert.match(textOf(ui.root()), /No events match this filter/);
    assert.match(textOf(ui.root()), /A run that finished before this window opened/);
    assert.doesNotMatch(textOf(ui.root()), hint);

    ui.update(f => { f.app.runs[0].status = "running"; f.app.runs[0].request.kind = "prove"; });
    assert.match(textOf(ui.root()), /No events match this filter/);
    assert.doesNotMatch(textOf(ui.root()), hint);

    ui.update(f => { f.app.runs[0].request.kind = "formalize"; });
    assert.match(textOf(ui.root()), hint);
    ui.click("Raw log");
    assert.match(textOf(ui.root()), /fixture log/);
    assert.doesNotMatch(textOf(ui.root()), hint);
  } finally { ui.dispose(); }
});

test("logs filter, collapse, expand, retry, copy and switch to raw output", () => {
  const ui = mount("logs");
  try {
    ui.update(f => { f.events = { ...f.events, "run-1": [{ ...f.events["run-1"][0], type: "job-session-start", event_id: "session-start", details: { job_id: "job-1", role: "prover" } }, ...f.events["run-1"]] }; });
    for (const label of ["Attention", "Everything", "Tool calls", "Reasoning", "Lean", "Provider", "Research", "Narrative"]) ui.click(label);
    const type = ui.buttons().find(node => node.props.title === "agent-output");
    ui.click(type); assert.deepEqual(ui.view.logFilter, ["agent-output"]);
    ui.click("Clear (1)"); assert.deepEqual(ui.view.logFilter, []);
    const segment = ui.buttons().find(node => node.props.className?.startsWith("log-segment-head"));
    ui.click(segment); assert.equal(ui.buttons().find(node => node.props.className?.startsWith("log-segment-head")).props["aria-expanded"], false);
    ui.click(ui.buttons().find(node => node.props.className?.startsWith("log-segment-head")));
    const row = ui.root().findAll(node => node.props.role === "button").find(node => textOf(node).includes("Agent output"));
    act(() => row.props.onClick());
    assert.ok(message(ui, "loadEventDetail"));
    ui.click("Open job log"); assert.equal(message(ui, "openProverFile").path, "job.log");
    for (const copy of ui.buttons().filter(node => textOf(node) === "Copy")) ui.click(copy);
    assert.equal(message(ui, "copyText").text, "agent-output full text");
    ui.update(f => { f.eventDetails["run-1:event-0"] = { detail: null, error: "Transient failure", loading: false }; });
    ui.click("Retry"); assert.equal(message(ui, "loadEventDetail").eventId, "event-0");
    ui.click("Collapse all (1)"); assert.equal(ui.buttons().some(node => textOf(node) === "Copy"), false);
    ui.click("Raw log"); assert.ok(message(ui, "loadRunLog")); assert.match(textOf(ui.root()), /fixture log/);
    ui.click("Structured"); assert.equal(ui.view.logRaw, false);
  } finally { ui.dispose(); }
});

test("agent output expands and the reasoning toggle only hides reasoning", () => {
  const ui = mount("live");
  try {
    ui.click(ui.buttons().find(node => node.props.className === "agent-activity-toggle"));
    assert.ok(message(ui, "loadEventDetail"));
    const toggle = ui.root().findAllByType("input").find(node => node.props.type === "checkbox");
    ui.change(toggle, false);
    assert.equal(ui.buttons().filter(node => node.props.className === "agent-activity-toggle").length, 1);
  } finally { ui.dispose(); }
});

test("sidebar quick launch, dashboard, stop and setup recovery buttons work", () => {
  const ui = mount("launch", {}, SidebarView);
  try {
    for (const [label, type] of [["Refresh", "refresh"], ["Prove", "launch"], ["Formalization", "openDashboard"], ["stop", "stopRun"]]) { ui.click(label); assert.ok(message(ui, type)); }
    assert.deepEqual(message(ui, "openDashboard"), { type: "openDashboard", workflow: "formalize" });
    ui.update(f => { f.app.cli.ok = false; });
    ui.click("Install guide"); assert.ok(message(ui, "openExternalDoc"));
    ui.click("CLI path"); assert.ok(message(ui, "openCliSettings"));
    ui.update(f => { f.app.cli.ok = true; f.app.project.found = false; });
    ui.click("Set up project"); assert.ok(message(ui, "setupProject"));
    ui.update(f => { f.app.projectSetup = { status: "ready", root: "/fixture", message: "Ready" }; });
    ui.click("Set up provider"); assert.ok(message(ui, "configureProvider"));
  } finally { ui.dispose(); }
});

test("source-only Open Dashboard selects Formalization before creating or preparing a Lean project", () => {
  const ui = mount("launch", {}, SidebarView);
  try {
    ui.update(f => { f.app.project.found = false; });
    assert.match(textOf(ui.root()), /inspect TeX or PDF sources before preparing a Lean project/);
    assert.equal(ui.buttons().some(button => textOf(button) === "Prove"), false);
    ui.click("Open Dashboard");
    assert.deepEqual(message(ui, "openDashboard"), { type: "openDashboard", workflow: "formalize" });
    assert.equal(message(ui, "setupProject"), undefined);
    assert.equal(message(ui, "launch"), undefined);
    ui.update(f => { f.app.cli.ok = false; });
    ui.click("Open Dashboard");
    assert.equal(ui.messages.filter(row => row.type === "openDashboard").length, 2);
    assert.equal(message(ui, "setupProject"), undefined);
  } finally { ui.dispose(); }
});

test("Open agent log selects the real agent and preserves visible output", () => {
  const ui = mount("live");
  try {
    ui.click("Open agent log");
    assert.equal(ui.view.logAgent, "prover-1");
    assert.equal(ui.view.tab, "logs");
    assert.match(textOf(ui.root()), /agent-output full text/);
  } finally { ui.dispose(); }
});

test("a live reported trace updates in place, keeps its detail open, and can be hidden", () => {
  const ui = mount("live");
  try {
    const details = { job_id: "job-1", item_id: "reasoning-1", invocation_id: "native-1", reasoning_kind: "trace", partial: true, content_preview: "First exposed step [REDACTED]" };
    const row = { ...ui.events["run-1"][0], type: "agent-reasoning", event_id: "partial-1", details };
    ui.update(f => { f.events = { ...f.events, "run-1": [row] }; });
    assert.match(textOf(ui.root()), /Receiving reported reasoning/);
    assert.match(textOf(ui.root()), /Reported reasoning trace/);
    const activity = () => ui.buttons().filter(node => node.props.className === "agent-activity-toggle");
    assert.equal(activity().length, 1);
    ui.click(activity()[0]);
    ui.update(f => { f.eventDetails["run-1:partial-1"] = { detail: { found: true, evidence: { record: { details: { text: "Earlier loaded trace" } } } }, loading: false, error: "" }; });
    assert.match(textOf(ui.root()), /Earlier loaded trace/);
    ui.update(f => { f.events = { ...f.events, "run-1": [row, { ...row, event_id: "partial-2", details: { ...details, content_preview: "First exposed step [REDACTED]\nNext exposed step" } }] }; });
    assert.equal(activity().length, 1);
    assert.equal(activity()[0].props["aria-expanded"], true);
    assert.equal(message(ui, "loadEventDetail").eventId, "partial-2");
    assert.match(textOf(ui.root()), /Next exposed step/);
    assert.ok(!textOf(ui.root()).includes("Earlier loaded trace"), "an updated item never reuses the prior event's full record");
    for (const feed of ui.root().findAllByProps({ className: "agent-activity" })) {
      for (const button of feed.findAllByType("button")) {
        for (let parent = button.parent; parent && parent !== feed; parent = parent.parent) {
          assert.notEqual(parent.type, "button", "detail actions are never nested inside the activity toggle");
          assert.notEqual(parent.props.role, "button");
        }
      }
    }
    ui.click("Collapse detail");
    assert.equal(activity()[0].props["aria-expanded"], false);
    const reasoning = ui.root().findAllByType("label").find(node => textOf(node) === "Show reported reasoning").findByType("input");
    ui.change(reasoning, false);
    assert.equal(activity().length, 0);
    assert.ok(!textOf(ui.root()).includes("Next exposed step"));
    const follow = ui.root().findAllByType("label").find(node => textOf(node) === "Follow live").findByType("input");
    ui.change(follow, false);
    assert.equal(follow.props.checked, false);
  } finally { ui.dispose(); }
});

test("live native tools show running output and finish without claiming proof acceptance", () => {
  const ui = mount("live");
  try {
    const details = { job_id: "job-1", item_id: "tool-1", invocation_id: "native-1", tool: "lean_check", description: "Check the replacement declaration" };
    const start = { ...ui.events["run-1"][0], type: "agent-tool-start", event_id: "start", details };
    const progress = { ...start, type: "agent-tool-progress", event_id: "output", details: { ...details, partial: true, result_preview: "Lean is checking…" } };
    ui.update(f => { f.events = { ...f.events, "run-1": [start, progress] }; });
    assert.match(textOf(ui.root()), /Lean check is running/);
    assert.match(textOf(ui.root()), /Lean is checking…/);
    const final = { ...progress, type: "agent-tool-end", event_id: "finished", details: { ...details, partial: false, status: "completed", result_preview: "Check finished" } };
    ui.update(f => { f.events = { ...f.events, "run-1": [start, progress, final] }; });
    assert.ok(!textOf(ui.root()).includes("Lean check is running"));
    assert.match(textOf(ui.root()), /Check finished/);
    assert.equal(ui.buttons().filter(node => node.props.className === "agent-activity-toggle").length, 2);
    assert.ok(!textOf(ui.root()).includes("Proof accepted"));
  } finally { ui.dispose(); }
});

test("live model settings explain native defaults without borrowing today's provider catalog", () => {
  const ui = mount("live");
  try {
    ui.update(f => {
      const state = f.proverStates["run-1"].snapshot;
      f.proverStates["run-1"].snapshot = normalizeProverSnapshot({ ...state,
        provider: "lemex", config: { agent_calls: null, provider: "lemex", orchestrator_provider: "claude", orchestrator_reasoning_effort: "high" },
        jobs: [{ ...state.jobs[0], provider: "lemex", model: "" }],
      }, "run-1");
    });
    const table = ui.root().findAllByType("table").find(node => /Role \/ agent/.test(textOf(node)));
    assert.ok(table);
    assert.match(textOf(table), /Proverprover-1LemexNot recorded by this run versionRequested: Native defaultRequested: Native default/);
    assert.match(textOf(table), /Orchestrator, review, and researchClaude CodeNot startedRequested: Native defaultRequested: high/);
    assert.doesNotMatch(textOf(table), /—|fixture-model/);
    assert.match(textOf(ui.root()), /lemex · Native default · model name not recorded/);
    assert.match(textOf(ui.help("Run model settings")), /Older jobs cannot be reconstructed/);
    ui.help("Run model settings", false);
  } finally { ui.dispose(); }
});

test("live agents show actual runtime metadata and retain previous calls in model history", () => {
  const ui = mount("live");
  try {
    ui.update(f => {
      const state = f.proverStates["run-1"].snapshot;
      f.proverStates["run-1"].snapshot = normalizeProverSnapshot({ ...state,
        config: { agent_calls: null, provider: "lemex" },
        jobs: ["codex", "claude", "lemex"].map((backend, index) => ({
          ...state.jobs[0], id: `job-${index}`, agent_id: `agent-${index}`, provider: backend,
          model_settings: ["previous", "current"].map((invocation_id, index) => ({
            invocation_id, backend, model: `${backend}-${invocation_id}`, model_source: "native_session",
            reasoning_effort: index ? "high" : "low", reasoning_source: "native_settings",
            requested_model: "alias", observed_at: "2026-10-12T10:00:00Z",
          })),
        })),
      }, "run-1");
    });
    const table = ui.root().findAllByType("table").find(node => /Role \/ agent/.test(textOf(node)));
    for (const backend of ["codex", "claude", "lemex"]) assert.match(textOf(table), new RegExp(`${backend}-currentRuntime sessionhighRuntime settings`));
    assert.match(textOf(ui.root()), /Model history \(2 agent calls\)/);
    assert.match(textOf(ui.root()), /lemex-previous/);
    assert.match(textOf(ui.root()), /Requested: alias/);
    assert.doesNotMatch(textOf(table), /fixture-model|alias/);
  } finally { ui.dispose(); }
});

test("proposal, controller, parent-job and disproof buttons preserve their exact targets", () => {
  const ui = mount("live");
  try {
    ui.click("Main.lean"); assert.equal(message(ui, "openProverFile").path, "Main.lean");
    ui.click(ui.buttons().find(node => node.props.className === "link" && textOf(node) === "prover-1"));
    assert.ok(ui.effects.some(row => row[0] === "focus"));
    ui.update(f => {
      const state = f.proverStates["run-1"].snapshot;
      f.proverStates["run-1"].snapshot = normalizeProverSnapshot({ ...state,
        phase: "planning", planning_request: { steps: { outline: "controller-job" } },
        jobs: [{ ...state.jobs[0], parent_job_id: "controller-job" }, { id: "controller-job", agent_id: "orchestrator-1", role: "orchestrator", status: "running" }],
        proposed_dag: { roots: ["draft"], nodes: [{ id: "draft", name: "draft", status: "pending" }] }, proposal_status: "validating", proposed_plan: "Draft plan",
        disproof: { certified: true, evidence_path: "negation.lean", node_id: "goal" },
      }, "run-1");
    });
    ui.click("Open disproof evidence"); assert.equal(message(ui, "openProverFile").path, "negation.lean");
    ui.click("controller-job");
    ui.click(/^Proposed ·/); assert.match(textOf(ui.root()), /draft/);
    ui.click("Canonical"); assert.match(textOf(ui.root()), /goal/);
    ui.click("orchestrator-1");
    ui.update(f => { f.view.logSearch = "stale search"; });
    ui.click(ui.buttons().find(node => textOf(node) === "Events"));
    assert.equal(ui.view.logAgent, "orchestrator-1"); assert.equal(ui.view.logSearch, "");
  } finally { ui.dispose(); }
});

test("project setup buttons expose progress, retry, readiness and provider setup", () => {
  const ui = mount();
  try {
    ui.click("Check setup"); assert.ok(message(ui, "setupProject"));
    ui.update(f => { f.app.projectSetup = { status: "running", root: "/fixture", phase: "dependencies", message: "Downloading Mathlib", recentOutput: ["Reusing existing downloads"], canRetry: false }; });
    assert.equal(ui.button("Start proving").props.disabled, true);
    ui.click("Cancel setup"); assert.ok(message(ui, "cancelProjectSetup"));
    ui.click("Show setup log"); assert.ok(message(ui, "showProjectSetupLog"));
    assert.match(textOf(ui.root().findByProps({"aria-label":"Project setup log"})), /Reusing existing downloads/);
    ui.update(f => { f.app.projectSetup.recentOutput.push("Cache ready"); });
    assert.match(textOf(ui.root().findByProps({"aria-label":"Project setup log"})), /Cache ready/);
    ui.click("Hide setup log");
    assert.equal(ui.root().findAllByProps({"aria-label":"Project setup log"}).length, 0);
    ui.update(f => { f.app.projectSetup = { ...f.app.projectSetup, status: "failed", message: "Download failed", remediation: "Check your network and retry.", canRetry: true }; });
    assert.match(textOf(ui.root()), /Step: dependencies/);
    assert.match(textOf(ui.root()), /Check your network and retry/);
    ui.click("Retry setup"); assert.ok(message(ui, "retryProjectSetup"));
    ui.update(f => { f.app.projectSetup = { ...f.app.projectSetup, status: "ready", canRetry: false, message: "Verification ready." }; });
    assert.equal(ui.button("Start proving").props.disabled, false);
    assert.match(textOf(ui.root()), /Ready to prove/);
    ui.update(f => { f.app.projectSetup.sourceDiagnostics = ["Problem.lean: unknown module PrivateLibrary"]; });
    assert.match(textOf(ui.root()), /Source files need attention \(1\)/);
    assert.match(textOf(ui.root()), /unknown module PrivateLibrary/);
    assert.equal(ui.button("Start proving").props.disabled, false);
    ui.click("Repair dependencies"); assert.ok(message(ui, "repairProjectSetup"));
    ui.update(f => { f.app.projectSetup.activeRun = true; });
    assert.equal(ui.button("Repair dependencies").props.disabled, true);
    assert.equal(ui.button("Check setup").props.disabled, true);
    ui.click("Set up provider"); assert.equal(ui.view.tab, "providers");
  } finally { ui.dispose(); }
});

test("job menus and clickable sidebar cards navigate exact live and past jobs", () => {
  const ui = mount("live");
  try {
    ui.update(f => {
      f.app.runs[0].connection = {kind:"tmux"};
      f.app.history = [{run_id:"archived",label:"Saved proof",workflow_kind:"prove",started_at:"2026-10-01",updated_at:"2026-10-02",terminal:true,exit_code:0}];
    });
    ui.click("Open terminal"); assert.equal(message(ui,"openRunTerminal").id,"tracked-1");
    ui.click("Job logs"); assert.equal(ui.view.tab,"logs");
    ui.click("Job details"); assert.equal(ui.view.tab,"live");
    const picker = ui.root().findByProps({"aria-label":"Selected job"});
    ui.change(picker,"archived"); assert.equal(message(ui,"selectRun").id,"archived");
    ui.update(f => { f.app.selectedRunId = "archived"; f.app.liveStatus = {run_id:"run-1",process_id:1,phase:"unrelated phase"}; });
    assert.match(textOf(ui.root()), /Viewing a past job/);
    assert.doesNotMatch(textOf(ui.root()), /unrelated phase/);
    ui.click("Job logs");
    assert.equal(message(ui,"loadEvents").runId,"archived");
    ui.click("Raw log");
    assert.equal(message(ui,"loadRunLog").runId,"archived");
    assert.doesNotMatch(textOf(ui.root()), /fixture log/);
  } finally { ui.dispose(); }
  const sidebar = mount("launch",{},SidebarView);
  try {
    sidebar.click(sidebar.buttons().find(button=>button.props.className === "job-card-open"));
    assert.equal(message(sidebar,"openRun").id,"tracked-1");
    sidebar.update(f=>{
      f.app.runs=[];
      f.app.history=[{run_id:"external-past",label:"Past terminal job",workflow_kind:"prove",started_at:"2026-10-01",updated_at:"2026-10-02",terminal:true,exit_code:1}];
    });
    sidebar.click(sidebar.buttons().find(button=>button.props.className === "job-card-open"));
    assert.equal(message(sidebar,"openRun").id,"external-past");
  } finally { sidebar.dispose(); }
});

test("transient CLI failures retain job navigation and offer retry without installation advice", () => {
  const ui = mount("launch",{},SidebarView);
  try {
    ui.update(f => { f.app.cli = { ok:false, health:"retrying", issue:"timeout", error:"Version check timed out" }; });
    assert.match(textOf(ui.root()), /connection interrupted/);
    assert.doesNotMatch(textOf(ui.root()), /setup is required|Install guide/);
    ui.click("Check again"); assert.ok(message(ui,"refresh"));
    ui.click(ui.buttons().find(button=>button.props.className === "job-card-open"));
    assert.equal(message(ui,"openRun").id,"tracked-1");
    ui.update(f => { f.app.cli = { ok:true, health:"ready", version:"0.3.0" }; });
    assert.doesNotMatch(textOf(ui.root()), /connection interrupted/);
    assert.ok(ui.button("Prove"));
  } finally { ui.dispose(); }
});

test("Formalization baseline is manual, workspace scoped, and required before native Start", () => {
  const ui = mount("formalization");
  try {
    const scope = { id: "setup-host", generation: 2, root: "/paper-B" };
    ui.update(f => {
      f.app.formalizationWorkspace = scope;
      f.app.projectSetup = { status: "ready", root: "/wrong-A", phase: "", message: "A only secret", recentOutput: ["A log secret"] };
    });
    assert.doesNotMatch(textOf(ui.root()), /A only secret|A log secret|wrong-A/);
    ui.click("Set up project");
    assert.deepEqual(message(ui, "formalizationProjectSetup"), { type: "formalizationProjectSetup", action: "start" });
    ui.click("Refresh tools status");
    assert.equal(message(ui, "formalizationProjectSetup").action, "inspect");
    const snapshot = formalizationFixture(); snapshot.preparation.current = false; snapshot.can_prove = false;
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    ui.click("Configure formalization");
    assert.equal(ui.button("Start formalization").props.disabled, true);
    const state = { status: "ready", root: scope.root, phase: "", message: "Project is ready to prove.", code: "", remediation: "", logPath: "", activeRun: false, canRetry: false, sourceDiagnostics: [], recentOutput: ["B log"] };
    hostMessage({ type: "formalizationProjectSetupState", formalizationWorkspace: { ...scope, generation: 1 }, state, error: "" });
    assert.equal(ui.button("Start formalization").props.disabled, true);
    hostMessage({ type: "formalizationProjectSetupState", formalizationWorkspace: scope, state, error: "" });
    assert.equal(ui.button("Start formalization").props.disabled, false);
    assert.doesNotMatch(textOf(ui.root()), /Project is ready to prove/);
    ui.click("Show setup log"); assert.equal(message(ui, "formalizationProjectSetup").action, "log");
    assert.match(textOf(ui.root()), /B log/);
    ui.click("Repair dependencies"); assert.equal(message(ui, "formalizationProjectSetup").action, "repair");
    hostMessage({ type: "formalizationProjectSetupState", formalizationWorkspace: scope, state: { ...state, status: "running" }, error: "" });
    assert.equal(ui.button("Start formalization").props.disabled, true);
    ui.click("Cancel setup"); assert.equal(message(ui, "formalizationProjectSetup").action, "cancel");
    hostMessage({ type: "formalizationProjectSetupState", formalizationWorkspace: scope, state: { ...state, status: "failed", canRetry: true }, error: "" });
    ui.click("Retry setup"); assert.equal(message(ui, "formalizationProjectSetup").action, "retry");
    ui.update(f => { f.app.formalizationWorkspace = { ...scope, root: "/paper-C", generation: 3 }; });
    assert.doesNotMatch(textOf(ui.root()), /B log/);
    assert.equal(ui.messages.some(row => row.type === "setupProject" || row.type === "launchFormalization"), false);
  } finally { ui.dispose(); }
});

after(() => {
  const files = ["webview/App.tsx", "webview/CliNotice.tsx", ...readdirSync("webview/views").filter(name => name.endsWith(".tsx")).map(name => `webview/views/${name}`)];
  const buttons = [];
  for (const file of files) {
    const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const visit = node => {
      if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) && node.tagName.getText(source) === "button") {
        buttons.push(`${file}:${source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1}`);
      }
      ts.forEachChild(node, visit);
    };
    visit(source);
  }
  assert.deepEqual(buttons.filter(key => !clickedButtons.has(key)), [], "Every button must be exercised in a reachable state");
  console.log(`Exercised all ${buttons.length} webview button definitions.`);
});


test("normal configuration is primary and the unaccepted draft is a separated explanatory option", () => {
  const ui = mount("formalization");
  try {
    const snapshot = sourceDraftFixture();
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    const primary = ui.button("Configure formalization");
    const secondary = ui.button("Configure unaccepted source draft");
    assert.equal(primary.props.className, "btn primary");
    assert.equal(primary.parent.props.className, "formalization-run-actions");
    assert.equal(secondary.props.className, "btn secondary");
    assert.equal(secondary.parent.type, "details");
    assert.equal(secondary.parent.props.className, "formalization-draft-option");
    assert.match(textOf(secondary.parent), /Translation acceptance, publication and proving stay blocked/);
    assert.match(textOf(secondary.parent), /does not start a run/);
    ui.click(primary);
    assert.equal(ui.messages.some(row => row.type === "launchFormalization" || row.type === "resumeFormalization"), false);
    ui.click(secondary);
    assert.equal(ui.messages.some(row => row.type === "launchFormalization" || row.type === "resumeFormalization"), false);
  } finally { ui.dispose(); }
});


test("returning to Formalization restores its selected source through a fresh scoped controller read", () => {
  const ui = mount("formalization");
  try {
    ui.update(f => { f.app.formalizationWorkspace = { id: "retained-host", generation: 1, root: "/fixture" }; });
    const snapshot = sourceDraftFixture();
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    ui.click("Configure unaccepted source draft");
    ui.change(ui.input("Agent backend"), "codex");
    const saved = structuredClone(ui.view.form);
    ui.click("Prove");
    ui.click("Formalization");
    assert.equal(message(ui, "showFormalization").documentId, snapshot.document_id);
    assert.equal(ui.input("Formalization document").props.value, snapshot.document_id);
    assert.match(textOf(ui.root()), /Restoring the selected source from current controller evidence/);
    assert.equal(ui.buttons().some(button => textOf(button) === "Create unaccepted source draft"), false);
    hostMessage({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
    assert.deepEqual(ui.view.form, saved);
    assert.equal(ui.input("Frozen source target").props.value, snapshot.source.path);
    assert.equal(ui.view.form.sourceDraftOnly, true);
  } finally { ui.dispose(); }
});

test("correction buttons navigate frozen evidence and save or revoke a scoped decision", () => {
  const ui = mount("formalization");
  try {
    let snapshot = formalizationCorrectionFixture();
    const show = next => {
      if (next.revision_id !== snapshot.revision_id) hostMessage({ type: "formalizationActionCommitted", documentId: snapshot.document_id, previousRevisionId: snapshot.revision_id, revisionId: next.revision_id });
      snapshot = next;
      hostMessage({ type: "formalizationState", snapshot, error: "" });
    };
    show(snapshot);
    ui.click("Original source · physical page 1");
    assert.equal(message(ui, "openFormalizationArtifact").pageIndex, 0);
    ui.click("Lean: Paper.double");
    assert.equal(message(ui, "openFormalizationArtifact").artifactId, "lean-1");
    ui.click("Enable bounded correction policy for this paper");
    assert.equal(message(ui, "formalizationAction").action.kind, "bounded_policy");
    show({ ...formalizationCorrectionFixture(), revision_id: "revision-2" });
    ui.change(ui.input("Correction reading"), "Use positive feasible integer parameters in the formal statement.");
    ui.change(ui.input("Correction decision reason"), "This records the source domains without granting mathematical approval.");
    ui.click("Record interpretation");
    const recorded = message(ui, "formalizationAction").action;
    assert.equal(recorded.kind, "interpretation");
    const next = { ...formalizationCorrectionFixture(), revision_id: "revision-3" };
    const { type, ...saved } = recorded;
    const decision = next.items[0].correction_authority_options[1];
    decision.enabled = true;
    decision.current = { ...saved, id: "authority-interpretation", actor: "user" };
    decision.action = { ...recorded, version: 2 };
    show(next);
    ui.click("Revoke this interpretation");
    assert.equal(message(ui, "formalizationAction").action.enabled, false);
    assert.equal(message(ui, "formalizationAction").action.version, 2);
    assert.doesNotMatch(textOf(ui.root()), /current source acceptance/);
  } finally { ui.dispose(); }
});
