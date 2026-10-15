/** Prepare mathematical infrastructure before staging and reviewing a representative translation slice. */
import { useEffect, useRef, useState } from "react";
import type { FormalizationSnapshot } from "../../src/core/formalization";
import type { FormalizationPreparationPreset, FormalizationPreparationState } from "../../src/core/formalizationPreparation";
import type { HostMessage } from "../../src/core/types";
import { post } from "../vscodeApi";

export function FormalizationPreparation({ snapshot, ready, onActiveChange }: {
  snapshot: FormalizationSnapshot; ready: boolean; onActiveChange?: (active: boolean) => void;
}) {
  const [preset, setPreset] = useState<FormalizationPreparationPreset>("mathlib");
  const [advanced, setAdvanced] = useState("");
  const [state, setState] = useState<FormalizationPreparationState | null>(null);
  const callback = useRef(onActiveChange);
  callback.current = onActiveChange;
  const active = state?.status === "planning" || state?.status === "preparing";
  useEffect(() => {
    const listener = (event: MessageEvent<HostMessage>) => {
      const message = event.data;
      if (message.type === "formalizationPreparation" && message.documentId === snapshot.document_id && message.revisionId === snapshot.revision_id) setState(message);
    };
    window.addEventListener("message", listener);
    return () => window.removeEventListener("message", listener);
  }, [snapshot.document_id, snapshot.revision_id]);
  useEffect(() => { callback.current?.(active); return () => callback.current?.(false); }, [active]);
  useEffect(() => { setState(null); }, [snapshot.document_id, snapshot.revision_id]);
  let overrides: Record<string, unknown> | undefined;
  let invalid = "";
  if (advanced.trim()) {
    try {
      const parsed: unknown = JSON.parse(advanced);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) || new TextEncoder().encode(advanced).length > 64 * 1024) throw new Error("Use a JSON object within the 64 KiB plan limit.");
      overrides = parsed as Record<string, unknown>;
    } catch (error) { invalid = error instanceof Error ? error.message : String(error); }
  }
  const invoke = (type: "planFormalizationPreparation" | "prepareFormalization") => {
    setState({ type: "formalizationPreparation", documentId: snapshot.document_id, revisionId: snapshot.revision_id, status: type === "prepareFormalization" ? "preparing" : "planning", message: "Waiting for the project controller…", error: "" });
    post({ type, documentId: snapshot.document_id, revisionId: snapshot.revision_id, preset, ...(overrides ? { overrides } : {}) });
  };
  const preparation = snapshot.preparation;
  const slice = snapshot.slice;
  const blocked = !ready || active || !!invalid || !!snapshot.source_stale || !!snapshot.selection_conflict;
  return <section className="card formalization-preparation">
    <h3>Prepare project for translation</h3>
    <p>Reuse existing definitions and packages, prepare compatible pinned dependencies and an import-only library root, then check a representative definition and statement before expanding the translation. The prover receives the checked environment and named proof obligations.</p>
    <p>Infrastructure: <strong>{preparation?.current ? "prepared and current" : preparation?.status ?? "preparation required"}</strong>{preparation?.reason ? ` · ${preparation.reason}` : ""}</p>
    {slice && <p>Representative translation slice: {slice.required ? slice.phase : "starts after preparation and launch"}</p>}
    <label>Library preset<select aria-label="Formalization preparation preset" value={preset} disabled={active} onChange={event => setPreset(event.target.value as FormalizationPreparationPreset)}><option value="mathlib">Mathlib mathematics</option><option value="lean">Lean core only</option></select></label>
    <details><summary>Additional pinned dependencies or representations</summary>
      <p>Optional structured overrides: packages with exact HTTPS source and full Git commit, required imports, mathematical prerequisites, representation notes and selected slice source IDs. The controller validates the plan and preserves existing pins.</p>
      <textarea aria-label="Formalization preparation plan overrides" value={advanced} disabled={active} maxLength={65536} onChange={event => setAdvanced(event.target.value)} placeholder={'{"required_imports":["Mathlib.Data.Nat.Basic"]}'} />
    </details>
    {invalid && <p className="notice error">Invalid preparation overrides: {invalid}</p>}
    <div className="toolbar">
      <button className="btn" disabled={blocked} onClick={() => invoke("planFormalizationPreparation")}>Inspect dependency and project plan</button>
      <button className="btn primary" disabled={blocked} onClick={() => invoke("prepareFormalization")}>Prepare dependencies and project</button>
      <button className="btn" disabled={state?.status !== "preparing" || !state.cancellable || state.phase === "cancelling"} onClick={() => {
        setState(old => old && { ...old, phase: "cancelling", cancellable: false, message: "Cancellation requested. Waiting for safe cleanup and a final controller receipt…" });
        post({ type: "cancelFormalizationPreparation", documentId: snapshot.document_id, revisionId: snapshot.revision_id });
      }}>Cancel project preparation</button>
    </div>
    {state?.message && <p role={active ? "status" : undefined}>{state.phase ? `${state.phase}: ` : ""}{state.message}</p>}
    {state?.error && <p className="notice error" role="alert">{state.error}</p>}
    {state?.plan && <details open><summary>Checked infrastructure and unreviewed mathematical estimates</summary>
      <p>Library {state.plan.layout?.library ?? "pending"} · root {state.plan.layout?.root_file ?? "pending"} · Lean {state.plan.toolchain ?? "pending"}</p>
      <p>Existing packages: {state.plan.existing_packages?.map(item => item.name).join(", ") || "none"}</p>
      <ul>{state.plan.packages?.map(item => <li key={item.name}>{item.name} · {item.revision} · {item.reason}</li>)}</ul>
      <p>Required imports: {state.plan.required_imports?.join(", ") || "none"}</p>
      <p>Representative source claims: {state.plan.slice_source_ids?.length ?? 0}</p>
      <ul>{state.plan.slice_source_ids?.map(id => <li key={id}>{snapshot.items.find(item => item.id === id)?.title ?? id}</li>)}</ul>
      {state.plan.mathematical_prerequisites?.length ? <details><summary>Unreviewed mathematical prerequisite estimates</summary><ul>{state.plan.mathematical_prerequisites.map((entry, index) => <li key={index}>{typeof entry === "string" ? entry : entry && typeof entry === "object" && "description" in entry ? String(entry.description) : "Inspect the structured prerequisite in the plan"}</li>)}</ul></details> : null}
      {state.plan.representation_notes?.length ? <ul>{state.plan.representation_notes.map((note, index) => <li key={index}>{note}</li>)}</ul> : null}
      {state.plan.blockers?.length ? <ul className="notice warn">{state.plan.blockers.map((reason, index) => <li key={index}>{reason}</li>)}</ul> : null}
      <p className="muted">This plan prepares infrastructure. Independent source review determines mathematical fidelity.</p>
    </details>}
  </section>;
}
