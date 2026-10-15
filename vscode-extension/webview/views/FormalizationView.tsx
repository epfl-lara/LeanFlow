/** Inspect source coverage, independent review evidence and exact accepted prover scope. */
import { useEffect, useRef, useState } from "react";
import { formalizationCoverage, proofAuthorizationLabel, type FormalizationAction, type FormalizationPdf, type FormalizationSnapshot, type FormalizationSourceRef } from "../../src/core/formalization";
import type { HostMessage } from "../../src/core/types";
import { sameFormalizationScope } from "../../src/core/formalizationScope";
import { EMPTY_PROJECT_SETUP } from "../../src/core/projectSetupTypes";
import { useStore } from "../store";
import { post } from "../vscodeApi";
import { workflowForm } from "../workflowForms";
import { LaunchView } from "./LaunchView";
import { FormalizationDiagnostics } from "./FormalizationDiagnostics";
import { FormalizationConflicts } from "./FormalizationConflicts";
import { FormalizationPdfView } from "./FormalizationPdf";
import { FormalizationPreparation } from "./FormalizationPreparation";
import { FormalizationProofAuthorization } from "./FormalizationProofAuthorization";
import { FormalizationRegionRepair, FormalizationRepair } from "./FormalizationRepair";
import { ProjectSetupCard } from "./ProjectSetupCard";

export function FormalizationView() {
  const { app, view, setForm, setView } = useStore();
  const [documents, setDocuments] = useState<FormalizationSnapshot[]>([]);
  const [snapshot, setSnapshot] = useState<FormalizationSnapshot | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [error, setError] = useState("");
  const [pdf, setPdf] = useState<FormalizationPdf | null>(null);
  const [pdfSelection, setPdfSelection] = useState<FormalizationSourceRef | null>(null);
  const [filter, setFilter] = useState("");
  const workspaceRoot = app?.formalizationWorkspace?.root ?? "";
  const savedSelection = view.formalizationSelection?.workspace === workspaceRoot && view.formalizationSelection.hostId === app?.formalizationWorkspace?.id && view.formalizationSelection.generation === app?.formalizationWorkspace?.generation ? view.formalizationSelection : undefined;
  const [configure, setConfigure] = useState(savedSelection?.configure ?? false);
  const [preparationBusy, setPreparationBusy] = useState(false);
  const [setupReply, setSetupReply] = useState<Extract<HostMessage, { type: "formalizationProjectSetupState" }> | null>(null);
  const [sourceSelection, setSourceSelection] = useState<Extract<HostMessage, { type: "formalizationSourceSelection" }> | null>(null);
  const ready = Boolean(app?.cli.ok);
  const latestView = useRef(view);
  latestView.current = view;
  const previousProjectRoot = useRef(app?.project.root ?? "");
  const previousWorkspace = useRef(app?.formalizationWorkspace);

  useEffect(() => {
    const nextRoot = app?.project.root ?? "";
    if (!ready || (previousProjectRoot.current && previousProjectRoot.current !== nextRoot) || previousWorkspace.current?.generation !== app?.formalizationWorkspace?.generation || previousWorkspace.current?.id !== app?.formalizationWorkspace?.id) {
      setConfigure(false); setSnapshot(null); setDocuments([]); setPdf(null); setSourceSelection(null); setPdfSelection(null); setError("");
      setSetupReply(null);
    }
    previousProjectRoot.current = nextRoot;
    previousWorkspace.current = app?.formalizationWorkspace;
    const receive = (event: MessageEvent<HostMessage>) => {
      const message = event.data;
      if (message.type === "formalizationList") {
        setError(message.error);
        if (!message.error) setDocuments(message.documents);
      } else if (message.type === "formalizationState") {
        setError(message.error);
        if (message.snapshot) {
          const next = message.snapshot;
          setSnapshot(next);
          setView({ formalizationSelection: { workspace: workspaceRoot, hostId: app?.formalizationWorkspace?.id, generation: app?.formalizationWorkspace?.generation, documentId: next.document_id, configure: latestView.current.formalizationSelection?.workspace === workspaceRoot && latestView.current.formalizationSelection.documentId === next.document_id ? latestView.current.formalizationSelection.configure : false } });
          setDocuments(old => [...old.filter(document => document.document_id !== next.document_id), next]);
          setSelectedId(old => next.items.some(item => item.id === old) ? old : next.items[0]?.id ?? "");
          setPdf(old => old?.documentId === next.document_id && old.revisionId === next.revision_id ? old : null);
        }
      } else if (message.type === "formalizationPdf") {
        setPdf(message.pdf);
      } else if (message.type === "formalizationSourceSelection") {
        setSourceSelection(message);
      } else if (message.type === "formalizationProjectSetupState" && sameFormalizationScope(message.formalizationWorkspace, app?.formalizationWorkspace)) {
        setSetupReply(message);
      }
    };
    window.addEventListener("message", receive);
    if (ready) {
      post({ type: "formalizationProjectSetup", action: "inspect" });
      post({ type: "listFormalizations" });
      if (savedSelection?.documentId) post({ type: "showFormalization", documentId: savedSelection.documentId });
    }
    return () => window.removeEventListener("message", receive);
  }, [ready, app?.project.root, app?.formalizationWorkspace?.id, app?.formalizationWorkspace?.generation]);

  useEffect(() => { setPdfSelection(null); setSourceSelection(null); }, [snapshot?.document_id, snapshot?.revision_id]);

  const selected = snapshot?.items.find(item => item.id === selectedId);
  const setup = sameFormalizationScope(setupReply?.formalizationWorkspace, app?.formalizationWorkspace) ? setupReply!.state : { ...EMPTY_PROJECT_SETUP, root: workspaceRoot };
  const baselineReady = setup.status === "ready" || snapshot?.preparation?.current === true;
  const setupRunning = setup.status === "running";
  const repairBlocked = !!error || preparationBusy || !!snapshot?.selection_conflict;
  const draftRequiresFreshRevision = snapshot?.can_prove || ["published", "accepted_staged"].includes(snapshot?.status ?? "");
  const workflowBusy = (app?.runs ?? []).some(run => ["starting", "running"].includes(run.status) && !!workspaceRoot && run.projectRoot === workspaceRoot);
  const resumable = snapshot ? (app?.runs ?? []).filter(run => run.request.kind === "formalize" &&
    !["starting", "running"].includes(run.status) && run.formalizationSource?.documentId === snapshot.document_id &&
    run.formalizationSource.path === snapshot.source.path && run.request.target === snapshot.source.path &&
    !!workspaceRoot && run.projectRoot === workspaceRoot &&
    /^formalize-vscode-run-[A-Za-z0-9-]+$/.test(run.runId)) : [];
  const counts = snapshot ? formalizationCoverage(snapshot) : null;
  const sourceCandidates = snapshot && sourceSelection?.documentId === snapshot.document_id && sourceSelection.revisionId === snapshot.revision_id
    ? snapshot.items.filter(item => [...item.source_refs, ...item.lean_refs].some(ref => ref.artifact_id === sourceSelection.artifactId &&
      ref.line_start !== undefined && ref.line_start <= sourceSelection.lineEnd && (ref.line_end ?? ref.line_start) >= sourceSelection.lineStart)) : [];
  const repair = (action: FormalizationAction) => {
    if (snapshot && !error && (!snapshot.selection_conflict || action.type === "select_revision")) post({ type: "formalizationAction", documentId: snapshot.document_id, revisionId: snapshot.revision_id, action });
  };
  const openArtifact = (artifactId: string, itemId?: string, pageIndex?: number, line?: number) => {
    if (snapshot) post({ type: "openFormalizationArtifact", documentId: snapshot.document_id, revisionId: snapshot.revision_id, artifactId, itemId, pageIndex, line });
  };
  const refresh = () => {
    if (snapshot) post({ type: "showFormalization", documentId: snapshot.document_id });
    else post({ type: "listFormalizations" });
  };

  return <div className="view formalization-view">
    <header className="view-header"><h2>Formalization</h2><p>Set up project tools, inspect the source, then plan dependencies and review the translation.</p></header>
    <ProjectSetupCard key={`setup:${app?.formalizationWorkspace?.id}:${app?.formalizationWorkspace?.generation}`} formalization={{ state: setup, error: sameFormalizationScope(setupReply?.formalizationWorkspace, app?.formalizationWorkspace) ? setupReply?.error ?? "" : "", preparationCurrent: snapshot?.preparation?.current === true }} />
    <div className="toolbar">
      <button className="btn primary" disabled={!ready} onClick={() => post({ type: "inspectFormalization", sourceKind: "file" })}>Choose TeX or PDF</button>
      <button className="btn" disabled={!ready} onClick={() => post({ type: "inspectFormalization", sourceKind: "folder" })}>Choose TeX project</button>
      <button className="btn" disabled={!ready} onClick={refresh}>Refresh inventory</button>
    </div>
    <p className="muted">In the file picker, select a TeX file together with its PDF to inspect the pair. Companion page links remain approximate or unavailable until checked against the page.</p>
    {!ready && <p className="notice warn">Set up the LeanFlow CLI in the Prove tab first.</p>}
    <FormalizationConflicts ready={ready} onResolved={refresh} />
    {error && <p className="notice error" role="alert">{error} Any previous snapshot below is stale; repair and prove actions are disabled until refreshed.</p>}
    <label>Source document<select aria-label="Formalization document" value={snapshot?.document_id ?? savedSelection?.documentId ?? ""} onChange={event => {
      setConfigure(false); setView({ formalizationSelection: undefined }); setSnapshot(null); setPdf(null); setError("");
      if (event.target.value) post({ type: "showFormalization", documentId: event.target.value });
    }}><option value="">Choose an inspected source…</option>{!snapshot && savedSelection?.documentId && !documents.some(document => document.document_id === savedSelection.documentId) && <option value={savedSelection.documentId}>{error ? "Selected source needs refresh" : "Restoring selected source…"}</option>}{documents.map(document => <option key={document.document_id} value={document.document_id}>{document.source.path} · {document.status}</option>)}</select></label>
    {!snapshot && savedSelection?.documentId && !error && <p aria-live="polite">Restoring the selected source from current controller evidence…</p>}
    {!snapshot && !savedSelection?.documentId && <p>Source inspection freezes an inventory without launching a model. Included and excluded material stay visible throughout review.</p>}
    {snapshot && counts && <>
      <FormalizationPreparation key={`preparation:${snapshot.document_id}:${snapshot.revision_id}`} snapshot={snapshot} ready={ready} onActiveChange={setPreparationBusy} />
      <section className="card">
        <h3>{snapshot.source.path}</h3>
        <p><strong>{counts.reviewed}/{counts.included} included items reviewed</strong> · {counts.excluded} excluded · {counts.unresolved} unresolved regions</p>
        <p>Selected revision <code>{snapshot.revision_id}</code> · {snapshot.status}{snapshot.transaction_state ? ` · publication ${snapshot.transaction_state}` : ""}</p>
        {snapshot.receipt_error && <div className="notice error" role="alert">
          <p><strong>{snapshot.status === "review_error" ? "Review could not finish" : snapshot.status === "check_error" ? "Lean checking could not finish" : snapshot.status === "check_rejected" ? "Lean draft needs correction" : "Formalization needs attention"}</strong></p>
          <p>{snapshot.receipt_error}</p>
          <p>{snapshot.status === "check_error" ? "Resolve the reported checking problem, then configure or resume formalization to retry. The draft has not received a Lean verdict." : "Address this issue, then configure or resume formalization to run the checks again."}</p>
        </div>}
        <p className="muted">Coverage, source review, Lean elaboration and proof completion are separate. Discovered items alone cannot establish that all mathematics was found.</p>
        {snapshot.source_stale && <p className="notice warn">Live source changed after this frozen revision. Source links show the recorded evidence.</p>}
        {snapshot.selection_conflict && <div className="notice warn" role="alert">
          <p>{snapshot.selection_conflict.message}</p>
          <p>Local working revision <code>{snapshot.selection_conflict.private_revision_id}</code> · published revision <code>{snapshot.selection_conflict.published_revision_id}</code></p>
          <p>Choose an explicit revision below before making repairs or starting a workflow.</p>
        </div>}
        <p>Reviewer: {snapshot.reviewer ? [snapshot.reviewer.provider, snapshot.reviewer.model, snapshot.reviewer.effort].filter(Boolean).join(" · ") || "not recorded" : "not recorded"} · Cost: {snapshot.costs?.known_usd !== undefined ? `$${snapshot.costs.known_usd.toFixed(2)}` : "unknown"}{snapshot.costs?.unknown_calls ? ` + ${snapshot.costs.unknown_calls} unpriced calls` : ""}{snapshot.costs?.budget_usd !== undefined ? ` · budget $${snapshot.costs.budget_usd}` : ""}</p>
        {snapshot.layout && <details><summary>Project layout</summary><p>{snapshot.layout.summary}</p><ul>{[...snapshot.layout.modules ?? [], ...snapshot.layout.changes ?? []].map((entry, index) => <li key={index}>{entry}</li>)}</ul></details>}
        <div className="formalization-run-actions"><button className="btn primary" disabled={!ready || repairBlocked || snapshot.source_stale === true} onClick={() => {
          setForm({ kind: "formalize", target: snapshot.source.path, formalization: undefined, sourceDraftOnly: false }); setConfigure(true); setView({ formalizationSelection: { workspace: workspaceRoot, hostId: app?.formalizationWorkspace?.id, generation: app?.formalizationWorkspace?.generation, documentId: snapshot.document_id, configure: true } });
        }}>Configure formalization</button></div>
        {snapshot.source.kind === "latex" && (snapshot.source.pages ?? 0) > 0 && <details className="formalization-draft-option"><summary>Optional unaccepted source draft</summary><p>Use the paired PDF to prepare one preview while source uncertainty remains. Translation acceptance, publication and proving stay blocked. Configuring this option does not start a run.</p><button className="btn secondary" disabled={!ready || repairBlocked || snapshot.source_stale === true || draftRequiresFreshRevision} onClick={() => {
          setForm({ kind: "formalize", target: snapshot.source.path, formalization: undefined, sourceDraftOnly: true }); setConfigure(true); setView({ formalizationSelection: { workspace: workspaceRoot, hostId: app?.formalizationWorkspace?.id, generation: app?.formalizationWorkspace?.generation, documentId: snapshot.document_id, configure: true } });
        }}>Configure unaccepted source draft</button></details>}
        {draftRequiresFreshRevision && <p className="muted">An accepted revision cannot become an unaccepted source draft. Select or create a source revision first.</p>}
        {snapshot.revisions && snapshot.revisions.length > 1 && <label>Explicit revision selection<select aria-label="Select formalization revision" value={snapshot.revision_id} disabled={!!error} onChange={event => repair({ type: "select_revision", revision_id: event.target.value })}>
          {snapshot.revisions.map(revision => <option key={revision.revision_id} value={revision.revision_id}>{revision.revision_id} · {revision.status}</option>)}
        </select></label>}
      </section>
      {configure && <section aria-label="Formalization configuration">
        {workflowBusy && <p className="notice info">A workflow already owns this workspace. Follow it in Live or wait for it to stop before starting or resuming.</p>}
        <p className="notice info">{baselineReady ? "The run inspects your prepared baseline and transactionally adds required dependencies before translation." : "Complete Project setup above before starting native dependency planning."} Source coverage, independent review and Lean checks determine whether statements can pass to proving.</p>
        <LaunchView kind="formalize" blocked={!baselineReady || setupRunning || repairBlocked || snapshot.source_stale === true || preparationBusy || workflowBusy}
          source={{ target: snapshot.source.path, allowDraft: snapshot.source.kind === "latex" && (snapshot.source.pages ?? 0) > 0 && !draftRequiresFreshRevision, launch: request => post({ type: "launchFormalization", documentId: snapshot.document_id, revisionId: snapshot.revision_id, request }) }} />
        <h3>Resume a tracked formalization</h3>
        {resumable.map(run => <button className="btn" key={run.id} disabled={!ready || !baselineReady || setupRunning || repairBlocked || preparationBusy || workflowBusy || !!view.workflowSelectionRequired} onClick={() => post({ type: "resumeFormalization", documentId: snapshot.document_id, revisionId: snapshot.revision_id, trackedRunId: run.id, request: { ...workflowForm(view, "formalize"), target: snapshot.source.path, sourceDraftOnly: run.request.sourceDraftOnly === true } })}>Resume {run.runId}</button>)}
        {!resumable.length && <p className="muted">No stopped extension-tracked run matches this source and workspace. Resume runs started outside this extension with the CLI and their exact run ID.</p>}
      </section>}
      <FormalizationDiagnostics key={`${snapshot.document_id}:${snapshot.revision_id}`} diagnostics={snapshot.diagnostics} disabled={repairBlocked} submit={repair} openSource={source => openArtifact(source.artifact_id, undefined, source.page_index, source.line_start)} />
      {snapshot.draft_preview && <section className="card">
        <h3>Unaccepted source draft</h3>
        <p className="notice warn">Source uncertainty remains unresolved. These files have no coverage, translation, publication or proof approval.</p>
        <p>{snapshot.draft_preview.source_diagnostics.length} source diagnostics remain attached to this draft. Run <code>{snapshot.draft_preview.run_id}</code>.</p>
        <ul>{snapshot.draft_preview.artifacts.map(artifact => <li key={artifact.id}>
          <button className="btn" disabled={!ready || repairBlocked} onClick={() => openArtifact(artifact.id)}>{artifact.title ?? artifact.path}</button>
        </li>)}</ul>
        <p className="muted">Inspect the frozen draft beside the original TeX and PDF. Source repairs and independent statement review are required before proving.</p>
      </section>}
      <section className="card">
        <h3>Source inventory</h3>
        {sourceSelection && <div aria-live="polite"><p>Editor selection: {sourceCandidates.length} candidate source items.</p>
          {sourceCandidates.map(item => <button className="btn" key={item.id} onClick={() => setSelectedId(item.id)}>{item.title}</button>)}</div>}
        <input aria-label="Filter source items" placeholder="Filter by title, kind or status" value={filter} onChange={event => setFilter(event.target.value)} />
        <div className="formalization-table"><table><thead><tr><th>Source item</th><th>Scope / claim</th><th>Source review</th><th>Lean elaboration</th><th>Proof</th><th>Proof permission</th></tr></thead>
          <tbody>{snapshot.items.filter(item => [item.title, item.kind, item.review_status, item.proof_status, item.scope].join(" ").toLowerCase().includes(filter.toLowerCase())).map(item => <tr key={item.id} className={item.id === selectedId ? "selected" : ""}>
            <td><button className="link" onClick={() => setSelectedId(item.id)}>{item.title}</button><div className="muted">{item.kind}</div></td>
            <td>{item.scope} · {item.claim_status || "unclassified"}</td><td>{item.review_status || "unreviewed"}</td><td>{item.elaboration_status || "unchecked"}</td><td>{item.proof_status || "unknown"}</td><td>{proofAuthorizationLabel(item)}</td>
          </tr>)}</tbody></table></div>
        {snapshot.items.length === 0 && <p>No mathematical items have been inventoried. Resolve missing regions or add items before review.</p>}
      </section>
      {selected && <section className="card">
        <h3>{selected.title}</h3>
        <p>Source claim: {selected.claim_status || "unclassified"}{selected.extracted_claim_status ? ` · extraction: ${selected.extracted_claim_status}` : ""}</p>
        {selected.claim_status_rationale && <p>{selected.claim_status_rationale}</p>}
        <div className="formalization-source-pair"><div><h4>Frozen source</h4>
          {selected.source_refs.map((ref, index) => <div key={index} className="formalization-source-ref">
            <button className="btn" disabled={!!error} onClick={() => openArtifact(ref.artifact_id, selected.id, ref.page_index, ref.line_start)}>
              {ref.file ?? "Source"}{ref.page_index !== undefined ? ` · physical page ${ref.page_index + 1}` : ref.line_start ? ` · line ${ref.line_start}` : ""}
            </button>
            <span className="tag">{ref.precision ?? "missing alignment"}</span>{ref.text && <pre>{ref.text}</pre>}
          </div>)}
          {!selected.source_refs.length && <p className="notice warn">Missing source boundary. Repair it before review.</p>}
        </div><div><h4>Lean declarations</h4>{selected.lean_refs.map((ref, index) => <div key={index}>
          <button className="btn" disabled={!!error || !ref.artifact_id} onClick={() => ref.artifact_id && openArtifact(ref.artifact_id, selected.id, undefined, ref.line_start)}>{ref.name}</button>
          <p className="muted">{ref.file}{!ref.artifact_id ? " · no verified source link" : ""}</p>
        </div>)}{!selected.lean_refs.length && <p>No Lean mapping recorded.</p>}</div></div>
        {selected.assumptions?.length ? <p>Assumptions: {selected.assumptions.join("; ")}</p> : null}
        {selected.representation_notes && <p>Representation: {selected.representation_notes}</p>}
        {selected.mapping_rationale && <p>Mapping rationale: {selected.mapping_rationale}</p>}
        {selected.feedback?.length ? <ul>{selected.feedback.map((entry, index) => <li key={index}>{entry}</li>)}</ul> : null}
        <FormalizationProofAuthorization key={`${snapshot.revision_id}:${selected.id}`} item={selected} disabled={repairBlocked} submit={repair} />
      </section>}
      <div className="toolbar">{snapshot.artifacts.filter(artifact => artifact.kind === "pdf" || artifact.path.endsWith(".pdf")).map(artifact => <button className="btn" key={artifact.id} disabled={!!error} onClick={() => openArtifact(artifact.id)}>Open PDF: {artifact.title ?? artifact.path}</button>)}</div>
      {pdf && pdf.documentId === snapshot.document_id && pdf.revisionId === snapshot.revision_id && <FormalizationPdfView pdf={pdf} items={snapshot.items} selectedId={selectedId} selectItem={setSelectedId} companion={snapshot.source.kind === "latex"} onSelection={snapshot.source.kind === "pdf" ? setPdfSelection : undefined} />}
      <FormalizationRepair key={`${snapshot.document_id}:${selectedId}`} snapshot={snapshot} item={selected} disabled={repairBlocked} submit={repair} pdfSelection={pdfSelection} />
      <section className="card"><h3>Source region accounting</h3>{snapshot.regions.map(region => <FormalizationRegionRepair key={region.id} region={region} disabled={repairBlocked} submit={repair} />)}</section>
      <section className="card"><h3>Prover handoff</h3>
        <p>Only the selected, accepted revision can start proving. The controller rechecks its files, environment and exact named obligations.</p>
        {snapshot.proof_completion && <p>Proof completion: <strong>{snapshot.proof_completion}</strong>{snapshot.proof_revision_id ? <> · proof certificate revision <code>{snapshot.proof_revision_id}</code></> : null}</p>}
        {snapshot.handoff ? <>{snapshot.proof_completion === "checked" ? <p>All obligations in this handoff have checked proofs.</p> : snapshot.handoff.obligations.length > 0 ? <ul>{snapshot.handoff.obligations.map(obligation => <li key={obligation.id}><code>{obligation.declaration}</code> · {obligation.file}</li>)}</ul> : <p>No pending proof obligations in this handoff.</p>}</> : <p>No accepted handoff is available.</p>}
        <p className="muted">Uses the provider, model and budget selected in the Prove tab.</p>
        <button className="btn primary" disabled={!ready || repairBlocked || !snapshot.can_prove || !snapshot.handoff} onClick={() => post({ type: "proveFormalization", documentId: snapshot.document_id, revisionId: snapshot.revision_id, request: workflowForm(view, "prove") })}>Start proving accepted statements</button>
      </section>
    </>}
  </div>;
}
