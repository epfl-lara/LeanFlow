/** Keep the two workflow configurations independent and migrate retired intents without launching them. */
import { emptyLaunchRequest, type LaunchRequest, type WorkflowKind } from "../src/core/types";

export interface WorkflowView {
  tab: string;
  form: LaunchRequest;
  workflowForms?: Partial<Record<WorkflowKind, LaunchRequest>>;
  workflowSelectionRequired?: boolean;
  retiredWorkflow?: string;
}

export function isWorkflowKind(value: unknown): value is WorkflowKind {
  return value === "prove" || value === "formalize";
}

/** Bind a form to its tab and remove options belonging to the other workflow. */
export function fixedWorkflowForm(kind: WorkflowKind, request?: Partial<LaunchRequest>): LaunchRequest {
  const form = { ...emptyLaunchRequest(), ...request, kind };
  if (kind === "prove") return { ...form, sourceDraftOnly: false, humanReview: false };
  return { ...form, formalization: undefined, research: false, researchWorkers: null,
    agents: 1, noParallel: false, cleanRoom: false, humanReview: false };
}

export function workflowForm(view: WorkflowView, kind: WorkflowKind): LaunchRequest {
  return fixedWorkflowForm(kind, view.form.kind === kind ? view.form : view.workflowForms?.[kind]);
}

/** Restore legacy shared forms while refusing to reinterpret a retired workflow. */
export function restoreWorkflowView<T extends WorkflowView>(view: T): T {
  const legacyKind: unknown = view.form.kind;
  const retired = !isWorkflowKind(legacyKind);
  const forms = {
    prove: fixedWorkflowForm("prove", view.workflowForms?.prove),
    formalize: fixedWorkflowForm("formalize", view.workflowForms?.formalize),
  };
  if (!retired) forms[legacyKind] = fixedWorkflowForm(legacyKind, view.form);
  const tab = !view.workflowForms && legacyKind === "formalize" && view.tab === "launch"
    ? "formalization" : view.tab;
  const kind = tab === "formalization" ? "formalize" : tab === "launch" ? "prove"
    : isWorkflowKind(legacyKind) ? legacyKind : "prove";
  return { ...view, tab, form: forms[kind], workflowForms: forms,
    workflowSelectionRequired: retired || view.workflowSelectionRequired === true,
    retiredWorkflow: retired ? String(legacyKind).slice(0, 32) : view.retiredWorkflow };
}

/** Patch one workflow without copying its target, budget or draft intent into its sibling. */
export function patchWorkflowForm<T extends WorkflowView>(view: T, patch: Partial<LaunchRequest>, activate = true): T {
  const kind = patch.kind ?? view.form.kind;
  if (!isWorkflowKind(kind)) throw new Error("Choose Prove or Formalization before configuring a run.");
  const current = isWorkflowKind(view.form.kind) ? view.form.kind : "prove";
  const forms = { prove: workflowForm(view, "prove"), formalize: workflowForm(view, "formalize") };
  forms[current] = fixedWorkflowForm(current, view.form);
  const form = fixedWorkflowForm(kind, { ...forms[kind], ...patch });
  const workflowForms = { ...forms, [kind]: form };
  return { ...view, form: activate ? form : workflowForms[current], workflowForms };
}

/** Select the fixed form for a workflow tab, retaining utility navigation preferences. */
export function selectWorkflowTab<T extends WorkflowView>(view: T, tab: T["tab"]): T {
  const kind = tab === "launch" ? "prove" : tab === "formalization" ? "formalize" : null;
  const next = kind ? patchWorkflowForm(view, { kind }) : view;
  return { ...next, tab, ...(kind ? { workflowSelectionRequired: false, retiredWorkflow: undefined } : {}) };
}
