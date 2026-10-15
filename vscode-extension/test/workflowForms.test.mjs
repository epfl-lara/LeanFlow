/** Keep workflow intent, source drafts and delayed picker results isolated across tabs. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { fixedWorkflowForm, patchWorkflowForm, restoreWorkflowView, selectWorkflowTab, workflowForm } from "../dist/test/workflowForms.mjs";

const form = (kind, patch = {}) => fixedWorkflowForm(kind, patch);

test("a legacy formalization launcher restores into its dedicated tab with its exact configuration", () => {
  const legacy = { tab: "launch", form: form("formalize", { target: "paper.tex", provider: "codex", model: "paper-model", sourceDraftOnly: true, prompt: "Preserve source" }) };
  const restored = restoreWorkflowView(legacy);
  assert.equal(restored.tab, "formalization");
  assert.deepEqual(restored.form, legacy.form);
  assert.equal(restored.workflowSelectionRequired, false);
  assert.equal(restored.workflowForms.prove.target, "");
  assert.equal(restored.workflowForms.prove.prompt, "");
  assert.equal(restored.workflowForms.prove.sourceDraftOnly, false);
});

for (const kind of ["draft", "review", "refactor", "golf"]) {
  test(`a retired ${kind} saved intent requires explicit selection and drops its target and prompt`, () => {
    const restored = restoreWorkflowView({ tab: "launch", form: { ...form("prove"), kind, target: "Old.lean", prompt: "Old workflow instructions", provider: "claude" } });
    assert.equal(restored.workflowSelectionRequired, true);
    assert.equal(restored.retiredWorkflow, kind);
    for (const next of Object.values(restored.workflowForms)) {
      assert.equal(next.target, "");
      assert.equal(next.prompt, "");
      assert.equal(next.provider, "");
    }
    const utility = selectWorkflowTab(restored, "providers");
    assert.equal(utility.workflowSelectionRequired, true);
    const selected = selectWorkflowTab(utility, "formalization");
    assert.equal(selected.workflowSelectionRequired, false);
    assert.equal(selected.retiredWorkflow, undefined);
    assert.equal(selected.form.kind, "formalize");
    assert.equal(selected.form.prompt, "");
  });
}

test("tab changes preserve each workflow's own target, provider, model, budgets and proof options", () => {
  let view = restoreWorkflowView({ tab: "launch", form: form("prove", { target: "Main.lean", provider: "claude", model: "proof-model", cleanRoom: true, research: true, agents: 4, overrides: { LEANFLOW_PROVER_AGENT_CALLS: "7" } }) });
  const prove = structuredClone(view.form);
  view = selectWorkflowTab(view, "formalization");
  view = patchWorkflowForm(view, { target: "paper/main.tex", provider: "codex", model: "translation-model", sourceDraftOnly: true, overrides: { LEANFLOW_PROVER_AGENT_CALLS: "20" } });
  const formalize = structuredClone(view.form);
  assert.equal(formalize.cleanRoom, false);
  assert.equal(formalize.research, false);
  assert.equal(formalize.agents, 1);
  view = selectWorkflowTab(view, "providers");
  assert.deepEqual(view.form, formalize);
  view = selectWorkflowTab(view, "launch");
  assert.deepEqual(view.form, prove);
  assert.equal(view.form.sourceDraftOnly, false);
  view = selectWorkflowTab(view, "formalization");
  assert.deepEqual(view.form, formalize);
});

test("a delayed Prove target picker updates only Prove while Formalization stays active", () => {
  let view = restoreWorkflowView({ tab: "launch", form: form("prove", { target: "Before.lean" }) });
  view = selectWorkflowTab(view, "formalization");
  view = patchWorkflowForm(view, { target: "paper.tex", sourceDraftOnly: true });
  const active = structuredClone(view.form);
  view = patchWorkflowForm(view, { kind: "prove", target: "Picked.lean" }, false);
  assert.equal(view.tab, "formalization");
  assert.deepEqual(view.form, active);
  assert.equal(workflowForm(view, "prove").target, "Picked.lean");
  assert.equal(workflowForm(view, "prove").sourceDraftOnly, false);
});

test("restoring a utility tab keeps both saved workflow configurations and selects neither as a new run", () => {
  const prove = form("prove", { target: "Proofs/Goal.lean", provider: "claude", model: "proof-model", overrides: { LEANFLOW_PROVER_WALL_TIME_S: "600" } });
  const formalize = form("formalize", { target: "paper.tex", provider: "codex", model: "translation-model", sourceDraftOnly: true, overrides: { LEANFLOW_PROVER_WALL_TIME_S: "1800" } });
  const restored = restoreWorkflowView({ tab: "providers", form: formalize, workflowForms: { prove, formalize } });
  assert.equal(restored.tab, "providers");
  assert.deepEqual(restored.form, formalize);
  assert.deepEqual(selectWorkflowTab(restored, "launch").form, prove);
  assert.deepEqual(selectWorkflowTab(restored, "formalization").form, formalize);
  assert.equal(restored.workflowSelectionRequired, false);
});

test("fixed forms remove incompatible intent without mutating their input", () => {
  const source = { sourceDraftOnly: true, humanReview: true, cleanRoom: true, research: true, researchWorkers: 3, agents: 4, noParallel: true, formalization: { documentId: "doc", revisionId: "rev" } };
  const before = structuredClone(source);
  const prove = fixedWorkflowForm("prove", source);
  assert.equal(prove.sourceDraftOnly, false);
  assert.equal(prove.humanReview, false);
  assert.equal(prove.cleanRoom, true);
  const formalize = fixedWorkflowForm("formalize", source);
  assert.equal(formalize.sourceDraftOnly, true);
  for (const key of ["humanReview", "cleanRoom", "research", "noParallel"]) assert.equal(formalize[key], false);
  assert.equal(formalize.researchWorkers, null);
  assert.equal(formalize.agents, 1);
  assert.equal(formalize.formalization, undefined);
  assert.deepEqual(source, before);
});

test("unknown workflow patches fail rather than overwriting a supported configuration", () => {
  const view = restoreWorkflowView({ tab: "launch", form: form("prove", { target: "Main.lean" }) });
  assert.throws(() => patchWorkflowForm(view, { kind: "golf", target: "Other.lean" }), /Choose Prove or Formalization/);
  assert.equal(view.form.target, "Main.lean");
});
