/** Exercise actual React button handlers and validate every message at the host boundary. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { readdirSync, readFileSync } from "node:fs";
import ts from "typescript";
import { mount, textOf, act, SidebarView, clickedButtons, normalizeProverSnapshot } from "./helpers/webviewFixture.mjs";

function message(ui, type) { return ui.messages.filter(row => row.type === type).at(-1); }

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
    for (const label of ["Providers", "Live", "Logs", "Knobs", "Sweeps", "Launch"]) {
      ui.click(new RegExp(`^${label}\\d*$`));
      assert.equal(ui.view.tab, label.toLowerCase());
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
    ui.click("Start run");
    assert.equal(message(ui, "launch").request.model, "fixture-model");
    ui.update(f => { f.app.project.found = false; });
    assert.equal(ui.button("Start run").props.disabled, true);
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
    assert.equal(ui.button("Start run").props.disabled, true);
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
    const limits = [["PROVER_SESSIONS", "prover calls"], ["MAX_RESTARTS", "standard retries per goal"]];
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

test("logs filter, collapse, expand, retry, copy and switch to raw output", () => {
  const ui = mount("logs");
  try {
    ui.update(f => { f.events["run-1"].unshift({ ...f.events["run-1"][0], type: "job-session-start", event_id: "session-start", details: { job_id: "job-1", role: "prover" } }); });
    for (const label of ["Attention", "Everything", "Tool calls", "Lean", "Provider", "Research", "Narrative"]) ui.click(label);
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
    for (const [label, type] of [["Refresh", "refresh"], ["Prove", "launch"], ["Dashboard", "openDashboard"], ["stop", "stopRun"]]) { ui.click(label); assert.ok(message(ui, type)); }
    ui.update(f => { f.app.cli.ok = false; });
    ui.click("Install guide"); assert.ok(message(ui, "openExternalDoc"));
    ui.click("CLI path"); assert.ok(message(ui, "openCliSettings"));
    ui.update(f => { f.app.cli.ok = true; f.app.project.found = false; });
    ui.click("Set up project"); assert.ok(message(ui, "setupProject"));
    ui.update(f => { f.app.projectSetup = { status: "ready", root: "/fixture", message: "Ready" }; });
    ui.click("Set up provider"); assert.ok(message(ui, "configureProvider"));
  } finally { ui.dispose(); }
});

test("Open agent log selects the real agent and preserves visible output", () => {
  const ui = mount("live");
  try {
    ui.click("Open agent log");
    assert.equal(ui.view.logAgent, "prover-1");
    assert.equal(ui.view.tab, "logs");
    assert.match(textOf(ui.root()), /agent-output message/);
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
    ui.click("Check / repair setup"); assert.ok(message(ui, "setupProject"));
    ui.update(f => { f.app.projectSetup = { status: "running", root: "/fixture", phase: "dependencies", message: "Downloading Mathlib", canRetry: false }; });
    assert.equal(ui.button("Start run").props.disabled, true);
    ui.click("Cancel setup"); assert.ok(message(ui, "cancelProjectSetup"));
    ui.click("Show setup log"); assert.ok(message(ui, "showProjectSetupLog"));
    ui.update(f => { f.app.projectSetup = { ...f.app.projectSetup, status: "failed", message: "Download failed", remediation: "Check your network and retry.", canRetry: true }; });
    assert.match(textOf(ui.root()), /Step: dependencies/);
    assert.match(textOf(ui.root()), /Check your network and retry/);
    ui.click("Retry setup"); assert.ok(message(ui, "retryProjectSetup"));
    ui.update(f => { f.app.projectSetup = { ...f.app.projectSetup, status: "ready", canRetry: false, message: "Verification ready." }; });
    assert.equal(ui.button("Start run").props.disabled, false);
    assert.match(textOf(ui.root()), /Ready to prove/);
    ui.update(f => { f.app.projectSetup.sourceDiagnostics = ["Problem.lean: unknown module PrivateLibrary"]; });
    assert.match(textOf(ui.root()), /Source files need attention \(1\)/);
    assert.match(textOf(ui.root()), /unknown module PrivateLibrary/);
    assert.equal(ui.button("Start run").props.disabled, false);
    ui.update(f => { f.app.projectSetup.activeRun = true; });
    assert.equal(ui.button("Check / repair setup").props.disabled, true);
    ui.click("Set up provider"); assert.equal(ui.view.tab, "providers");
  } finally { ui.dispose(); }
});

after(() => {
  const files = ["webview/App.tsx", ...readdirSync("webview/views").filter(name => name.endsWith(".tsx")).map(name => `webview/views/${name}`)];
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
