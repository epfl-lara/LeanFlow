/** Inspect source coverage, independent review evidence and exact accepted prover scope. */
import { useEffect, useReducer, useRef, useState } from "react";
import { formalizationCoverage, type FormalizationAction, type FormalizationPdf, type FormalizationSnapshot, type FormalizationSourceRef } from "../../src/core/formalization";
import type { HostMessage } from "../../src/core/types";
import { sameFormalizationScope } from "../../src/core/formalizationScope";
import { EMPTY_PROJECT_SETUP } from "../../src/core/projectSetupTypes";
import { useStore } from "../store";
import { post } from "../vscodeApi";
import { workflowForm } from "../workflowForms";
import { LaunchView } from "./LaunchView";
import { FormalizationFeedbackBar, NO_FORMALIZATION_FEEDBACK, formalizationSavePending, reduceFormalizationFeedback } from "./FormalizationFeedback";
import { FormalizationInventory, FormalizationItemDetail } from "./FormalizationInventory";
import { FormalizationIssues } from "./FormalizationIssues";
import { FormalizationPager, formalizationPage } from "./FormalizationPager";
import { FormalizationPdfView } from "./FormalizationPdf";
import { FormalizationPreparation } from "./FormalizationPreparation";
import { formalizationNextStep, formalizationProgress, plural } from "./formalizationProgress";
import { FormalizationRecoverySection } from "./FormalizationRecoverySection";
import { FormalizationRegions } from "./FormalizationRegions";
import { FormalizationRepair } from "./FormalizationRepair";
import { FormalizationSection, revealFormalizationSection, type SectionStatus } from "./FormalizationSection";
import { ProjectSetupCard } from "./ProjectSetupCard";

export function FormalizationView() {
  const { app, view, setForm, setView } = useStore();
  const [documents, setDocuments] = useState<FormalizationSnapshot[]>([]);
  const [snapshot, setSnapshot] = useState<FormalizationSnapshot | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [error, setError] = useState("");
  const [pdf, setPdf] = useState<FormalizationPdf | null>(null);
  const [pdfSelection, setPdfSelection] = useState<FormalizationSourceRef | null>(null);
  const [obligationPage, setObligationPage] = useState(0);
  const [feedback, dispatchFeedback] = useReducer(reduceFormalizationFeedback, NO_FORMALIZATION_FEEDBACK);
  const workspaceRoot = app?.formalizationWorkspace?.root ?? "";
  const savedSelection = view.formalizationSelection?.workspace === workspaceRoot && view.formalizationSelection.hostId === app?.formalizationWorkspace?.id && view.formalizationSelection.generation === app?.formalizationWorkspace?.generation ? view.formalizationSelection : undefined;
  const [configure, setConfigure] = useState(savedSelection?.configure ?? false);
  const [configureRequests, setConfigureRequests] = useState(0);
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
      setSetupReply(null); dispatchFeedback({ type: "reset" });
    }
    previousProjectRoot.current = nextRoot;
    previousWorkspace.current = app?.formalizationWorkspace;
    const receive = (event: MessageEvent<HostMessage>) => {
      const message = event.data;
      if (message.type === "formalizationList") {
        setError(message.error);
        if (!message.error) setDocuments(message.documents);
      } else if (message.type === "formalizationState") {
        dispatchFeedback({ type: "state", snapshot: message.snapshot, error: message.error, channel: message.formalizationRequest?.channel });
        setError(message.error);
        if (message.snapshot) {
          const next = message.snapshot;
          setSnapshot(next);
          setView({ formalizationSelection: { workspace: workspaceRoot, hostId: app?.formalizationWorkspace?.id, generation: app?.formalizationWorkspace?.generation, documentId: next.document_id, configure: latestView.current.formalizationSelection?.workspace === workspaceRoot && latestView.current.formalizationSelection.documentId === next.document_id ? latestView.current.formalizationSelection.configure : false } });
          setDocuments(old => [...old.filter(document => document.document_id !== next.document_id), next]);
          setSelectedId(old => next.items.some(item => item.id === old) ? old : next.items[0]?.id ?? "");
          setPdf(old => old?.documentId === next.document_id && old.revisionId === next.revision_id ? old : null);
        }
      } else if (message.type === "formalizationActionCommitted") {
        dispatchFeedback({ type: "committed", documentId: message.documentId, previousRevisionId: message.previousRevisionId, revisionId: message.revisionId });
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
  useEffect(() => { setObligationPage(0); }, [snapshot?.document_id]);

  const selected = snapshot?.items.find(item => item.id === selectedId);
  const setupKnown = sameFormalizationScope(setupReply?.formalizationWorkspace, app?.formalizationWorkspace);
  const setup = setupKnown ? setupReply!.state : { ...EMPTY_PROJECT_SETUP, root: workspaceRoot };
  const baselineReady = setup.status === "ready" || snapshot?.preparation?.current === true;
  const setupRunning = setup.status === "running";
  const savePending = formalizationSavePending(feedback);
  const sourceStale = snapshot?.source_stale === true || snapshot?.source_currentness === "stale";
  const repairBlocked = !!error || preparationBusy || !!snapshot?.selection_conflict;
  // Serialize controller edits: the transport discards replies to an older document request.
  const actionBlocked = repairBlocked || savePending;
  const draftRequiresFreshRevision = snapshot?.can_prove || ["published", "accepted_staged"].includes(snapshot?.status ?? "");
  const workflowBusy = (app?.runs ?? []).some(run => ["starting", "running"].includes(run.status) && !!workspaceRoot && run.projectRoot === workspaceRoot);
  const launchBlocked = !baselineReady || setupRunning || actionBlocked || sourceStale || preparationBusy || workflowBusy;
  const resumable = snapshot ? (app?.runs ?? []).filter(run => run.request.kind === "formalize" &&
    !["starting", "running"].includes(run.status) && run.formalizationSource?.documentId === snapshot.document_id &&
    run.formalizationSource.path === snapshot.source.path && run.request.target === snapshot.source.path &&
    !!workspaceRoot && run.projectRoot === workspaceRoot &&
    /^formalize-vscode-run-[A-Za-z0-9-]+$/.test(run.runId)) : [];
  const counts = snapshot ? formalizationCoverage(snapshot) : null;
  const progress = snapshot ? formalizationProgress(snapshot) : null;
  const sourceCandidates = snapshot && sourceSelection?.documentId === snapshot.document_id && sourceSelection.revisionId === snapshot.revision_id
    ? snapshot.items.filter(item => [...item.source_refs, ...item.lean_refs].some(ref => ref.artifact_id === sourceSelection.artifactId &&
      ref.line_start !== undefined && ref.line_start <= sourceSelection.lineEnd && (ref.line_end ?? ref.line_start) >= sourceSelection.lineStart)) : [];
  const notice = feedback.notice;
  const savedRefreshFailed = !!error && notice?.kind === "saved" && notice.refreshError === error;
  const errorLead = notice?.kind === "unsaved" && notice.error === error ? `No save was confirmed for the ${notice.label}. `
    : notice?.kind === "checkFailed" && notice.error === error ? "The currentness check did not finish. " : "";
  const next = snapshot && progress ? formalizationNextStep(snapshot, progress, { error: !!error, savedRefreshFailed, sourceStale, baselineReady, setupRunning,
    preparationBusy, workflowBusy, configuring: configure, resumable: resumable.length > 0 }) : null;
  const obligationView = snapshot?.handoff ? formalizationPage(snapshot.handoff.obligations, obligationPage) : null;
  const pdfShown = !!snapshot && pdf?.documentId === snapshot.document_id && pdf.revisionId === snapshot.revision_id;
  const setupStatus: SectionStatus = setupRunning ? { label: "running", tone: "running" } : baselineReady ? { label: "tools ready", tone: "ok" }
    : !setupKnown ? { label: "checking…" } : setup.canRetry ? { label: setup.status === "cancelled" ? "cancelled" : "failed", tone: "bad" } : { label: "required before formalizing", tone: "warn" };
  const currentness: SectionStatus | null = !snapshot ? null : feedback.checking ? { label: "checking currentness…", tone: "running" }
    : snapshot.currentness === "historical" ? { label: "saved view, not rechecked" }
    : snapshot.currentness === "checked" ? sourceStale ? { label: "checked: live source changed", tone: "bad" } : { label: "checked current", tone: "ok" } : null;

  const repair = (action: FormalizationAction) => {
    if (!snapshot || error || savePending || snapshot.selection_conflict && action.type !== "select_revision") return;
    dispatchFeedback({ type: "submit", documentId: snapshot.document_id, revisionId: snapshot.revision_id, action });
    post({ type: "formalizationAction", documentId: snapshot.document_id, revisionId: snapshot.revision_id, action });
  };
  const openArtifact = (artifactId: string, itemId?: string, pageIndex?: number, line?: number) => {
    if (snapshot) post({ type: "openFormalizationArtifact", documentId: snapshot.document_id, revisionId: snapshot.revision_id, artifactId, itemId, pageIndex, line });
  };
  // Newer document requests make the transport discard pending check and repair replies.
  const refresh = () => {
    dispatchFeedback({ type: "supersede" });
    if (snapshot) post({ type: "showFormalization", documentId: snapshot.document_id });
    else post({ type: "listFormalizations" });
  };
  const inspect = (sourceKind: "file" | "folder") => {
    dispatchFeedback({ type: "supersede" });
    post({ type: "inspectFormalization", sourceKind });
  };
  const checkCurrentness = () => {
    if (!snapshot || feedback.checking) return;
    dispatchFeedback({ type: "check", documentId: snapshot.document_id, revisionId: snapshot.revision_id });
    post({ type: "checkFormalization", documentId: snapshot.document_id, revisionId: snapshot.revision_id });
  };
  const configureRun = (sourceDraftOnly: boolean) => {
    if (!snapshot) return;
    setForm({ kind: "formalize", target: snapshot.source.path, formalization: undefined, sourceDraftOnly }); setConfigure(true); setConfigureRequests(count => count + 1);
    setView({ formalizationSelection: { workspace: workspaceRoot, hostId: app?.formalizationWorkspace?.id, generation: app?.formalizationWorkspace?.generation, documentId: snapshot.document_id, configure: true } });
  };

  return <div className="view formalization-view">
    <header className="view-header"><h2>Formalization</h2><p>Set up project tools, inspect the source, then plan dependencies and review the translation.</p></header>
    <FormalizationFeedbackBar feedback={feedback} errorVisible={!!error} onDismiss={() => dispatchFeedback({ type: "dismiss" })}
      checkedTranslation={snapshot?.currentness === "checked" && progress && notice?.kind === "checked" && notice.documentId === snapshot.document_id && notice.revisionId === snapshot.revision_id ? progress.translation : undefined} />
    {ready && <FormalizationSection id="formalization-setup" title="Project setup" status={[setupStatus]} attention={setupRunning || setupKnown && (!baselineReady || !!setupReply?.error)}>
      <ProjectSetupCard key={`setup:${app?.formalizationWorkspace?.id}:${app?.formalizationWorkspace?.generation}`} formalization={{ state: setup, error: setupKnown ? setupReply?.error ?? "" : "", preparationCurrent: snapshot?.preparation?.current === true }} />
    </FormalizationSection>}
    <div className="toolbar">
      <button className={next ? next.action === "choose" ? "btn primary" : "btn secondary" : error ? "btn secondary" : "btn primary"} disabled={!ready || savePending} onClick={() => inspect("file")}>Choose TeX or PDF</button>
      <button className="btn secondary" disabled={!ready || savePending} onClick={() => inspect("folder")}>Choose TeX project</button>
      <button className={(next ? next.action === "refresh" : !!error) ? "btn primary" : "btn secondary"} disabled={!ready} onClick={refresh}>Refresh inventory</button>
    </div>
    <p className="muted">In the file picker, select a TeX file together with its PDF to inspect the pair. Companion page links remain approximate or unavailable until checked against the page.</p>
    {!ready && <p className="notice warn">Set up the LeanFlow CLI in the Prove tab first.</p>}
    <label>Source document<select aria-label="Formalization document" value={snapshot?.document_id ?? savedSelection?.documentId ?? ""} onChange={event => {
      setConfigure(false); setView({ formalizationSelection: undefined }); setSnapshot(null); setPdf(null); setError(""); dispatchFeedback({ type: "reset" });
      if (event.target.value) post({ type: "showFormalization", documentId: event.target.value });
    }}><option value="">Choose an inspected source…</option>{!snapshot && savedSelection?.documentId && !documents.some(document => document.document_id === savedSelection.documentId) && <option value={savedSelection.documentId}>{error ? "Selected source needs refresh" : "Restoring selected source…"}</option>}{documents.map(document => <option key={document.document_id} value={document.document_id}>{document.source.path} · {document.status}</option>)}</select></label>
    <FormalizationRecoverySection ready={ready} onResolved={refresh} />
    {error && (savedRefreshFailed
      ? <div id="formalization-error" className="notice warn" role="status"><p><strong>Change saved; the view did not refresh.</strong> {error}</p>
        <p>The inventory below still shows revision <code>{snapshot?.revision_id}</code>. Refresh to continue; repairs and launches stay disabled until then.</p></div>
      : <p id="formalization-error" className="notice error" role="alert">{errorLead}{error} Any previous snapshot below is stale; repair and prove actions are disabled until refreshed.</p>)}
    {!snapshot && savedSelection?.documentId && !error && <p aria-live="polite">Restoring the selected source from current controller evidence…</p>}
    {!snapshot && !savedSelection?.documentId && <p>Source inspection freezes an inventory without launching a model. Included and excluded material stay visible throughout review.</p>}
    {snapshot && counts && progress && next && <>
      <section className="card" aria-label="Formalization overview">
        <h3>{snapshot.source.path}</h3>
        <p><strong>{counts.reviewed}/{counts.included} included items reviewed</strong> · {counts.excluded} excluded · {counts.unresolved} unresolved regions</p>
        <p>Selected revision <code>{snapshot.revision_id}</code> · {snapshot.status}{snapshot.transaction_state ? ` · publication ${snapshot.transaction_state}` : ""}{currentness && <> · <span className={currentness.tone ? `pill ${currentness.tone}` : "pill"}>{currentness.label}</span></>}</p>
        <ul aria-label="Formalization progress">
          <li><strong>Source inspection:</strong> {progress.source}</li>
          <li><strong>Translation:</strong> {progress.translation}</li>
          <li><strong>Proofs:</strong> {progress.proofs}</li>
        </ul>
        <p className="muted">Coverage, source review, Lean elaboration and proof completion are separate. Discovered items alone cannot establish that all mathematics was found.</p>
        {snapshot.receipt_error && <div className="notice error" role="alert">
          <p><strong>{snapshot.status === "review_error" ? "Review could not finish" : snapshot.status === "check_error" ? "Lean checking could not finish" : snapshot.status === "check_rejected" ? "Lean draft needs correction" : "Formalization needs attention"}</strong></p>
          <p>{snapshot.receipt_error}</p>
          <p>{snapshot.status === "check_error" ? "Resolve the reported checking problem, then configure or resume formalization to retry. The draft has not received a Lean verdict." : "Address this issue, then configure or resume formalization to run the checks again."}</p>
        </div>}
        {sourceStale && <p className="notice warn">Live source changed after this frozen revision. Source links show the recorded evidence.</p>}
        {snapshot.selection_conflict && <div className="notice warn" role="alert">
          <p>{snapshot.selection_conflict.message}</p>
          <p>Local working revision <code>{snapshot.selection_conflict.private_revision_id}</code> · published revision <code>{snapshot.selection_conflict.published_revision_id}</code></p>
          <p>Choose an explicit revision below before making repairs or starting a workflow.</p>
        </div>}
        <p className="notice info"><strong>Next step:</strong> {next.text}{next.target && <> <a href={`#${next.target.id}`} onClick={() => next.target && revealFormalizationSection(next.target.id)}>{next.target.label}</a></>}</p>
        <div className="formalization-run-actions">
          <button className={next.action === "configure" ? "btn primary" : "btn secondary"} disabled={!ready || actionBlocked || sourceStale} onClick={() => configureRun(false)}>Configure formalization</button>
          <button className={next.action === "check" ? "btn primary" : "btn secondary"} disabled={!ready || !!feedback.checking || savePending || !!error} onClick={checkCurrentness}>Check currentness</button>
        </div>
        {progress.historical && <p className="muted">This fast view uses saved evidence. Check currentness before relying on acceptance or proving; exact checking can take a few minutes.</p>}
        {snapshot.source.kind === "latex" && (snapshot.source.pages ?? 0) > 0 && <details className="formalization-draft-option"><summary>Optional unaccepted source draft</summary><p>Use the paired PDF to prepare one preview while source uncertainty remains. Translation acceptance, publication and proving stay blocked. Configuring this option does not start a run.</p><button className="btn secondary" disabled={!ready || actionBlocked || sourceStale || draftRequiresFreshRevision} onClick={() => configureRun(true)}>Configure unaccepted source draft</button></details>}
        {draftRequiresFreshRevision && <p className="muted">An accepted revision cannot become an unaccepted source draft. Select or create a source revision first.</p>}
        {snapshot.revisions && (snapshot.revisions.length > 1 || (snapshot.revision_history?.total ?? 0) > snapshot.revisions.length) && <details id="formalization-revisions" open={!!snapshot.selection_conflict}>
          <summary>Revisions · {plural(snapshot.revisions.length, "revision")}{progress.historical ? " · statuses from saved records" : ""}</summary>
          <label>Explicit revision selection<select aria-label="Select formalization revision" value={snapshot.revision_id} disabled={!!error || savePending} onChange={event => repair({ type: "select_revision", revision_id: event.target.value })}>
            {snapshot.revisions.map(revision => <option key={revision.revision_id} value={revision.revision_id}>{revision.revision_id} · {revision.status}</option>)}
          </select></label>
          {snapshot.revision_history && snapshot.revision_history.total > snapshot.revisions.length && <p className="muted">Showing {snapshot.revision_history.offset + 1}–{snapshot.revision_history.offset + snapshot.revisions.length} of {snapshot.revision_history.total} revisions. This view shows only this history page; use the CLI to inspect or select older revisions.</p>}
        </details>}
        <p>Reviewer: {snapshot.reviewer ? [snapshot.reviewer.provider, snapshot.reviewer.model, snapshot.reviewer.effort].filter(Boolean).join(" · ") || "not recorded" : "not recorded"} · Cost: {snapshot.costs?.known_usd !== undefined ? `$${snapshot.costs.known_usd.toFixed(2)}` : "unknown"}{snapshot.costs?.unknown_calls ? ` + ${snapshot.costs.unknown_calls} unpriced calls` : ""}{snapshot.costs?.budget_usd !== undefined ? ` · budget $${snapshot.costs.budget_usd}` : ""}</p>
        {snapshot.layout && <details><summary>Project layout</summary><p>{snapshot.layout.summary}</p><ul>{[...snapshot.layout.modules ?? [], ...snapshot.layout.changes ?? []].map((entry, index) => <li key={index}>{entry}</li>)}</ul></details>}
      </section>
      {configure && <FormalizationSection id="formalization-configuration" title="Run configuration" defaultOpen revealKey={configureRequests}
        status={[workflowBusy ? { label: "workflow running", tone: "running" } : launchBlocked ? { label: "waiting for prerequisites", tone: "warn" } : { label: "ready to start", tone: "ok" }]}>
        {workflowBusy && <p className="notice info">A workflow already owns this workspace. Follow it in Live or wait for it to stop before starting or resuming.</p>}
        <p className="notice info">{baselineReady ? "The run inspects your prepared baseline and transactionally adds required dependencies before translation." : "Complete Project setup above before starting native dependency planning."} Source coverage, independent review and Lean checks determine whether statements can pass to proving.</p>
        <LaunchView kind="formalize" blocked={launchBlocked}
          source={{ target: snapshot.source.path, allowDraft: snapshot.source.kind === "latex" && (snapshot.source.pages ?? 0) > 0 && !draftRequiresFreshRevision, launch: request => post({ type: "launchFormalization", documentId: snapshot.document_id, revisionId: snapshot.revision_id, request }) }} />
        <h4>Resume a tracked formalization</h4>
        {resumable.map(run => <button className="btn secondary" key={run.id} disabled={!ready || !baselineReady || setupRunning || actionBlocked || preparationBusy || workflowBusy || !!view.workflowSelectionRequired} onClick={() => post({ type: "resumeFormalization", documentId: snapshot.document_id, revisionId: snapshot.revision_id, trackedRunId: run.id, request: { ...workflowForm(view, "formalize"), target: snapshot.source.path, sourceDraftOnly: run.request.sourceDraftOnly === true } })}>Resume {run.runId}</button>)}
        {!resumable.length && <p className="muted">No stopped extension-tracked run matches this source and workspace. Resume runs started outside this extension with the CLI and their exact run ID.</p>}
      </FormalizationSection>}
      <FormalizationIssues key={`issues:${snapshot.document_id}`} revisionKey={`${snapshot.document_id}:${snapshot.revision_id}`} diagnostics={snapshot.diagnostics} disabled={actionBlocked} submit={repair}
        openSource={source => openArtifact(source.artifact_id, undefined, source.page_index, source.line_start)} />
      <FormalizationSection id="formalization-inventory" title="Source inventory" defaultOpen attention={pdfShown}
        status={[{ label: plural(snapshot.items.length, "item") }, { label: `${counts.reviewed}/${counts.included} reviewed${progress.historical ? " (saved)" : ""}` }, ...(counts.excluded ? [{ label: `${counts.excluded} excluded` }] : [])]}>
        <FormalizationInventory key={snapshot.document_id} items={snapshot.items} selectedId={selectedId} selectItem={setSelectedId} candidates={sourceSelection ? sourceCandidates : null} historical={progress.historical} />
        {selected && <FormalizationItemDetail item={selected} revisionId={snapshot.revision_id} historical={progress.historical} disabled={actionBlocked} linksDisabled={!!error} submit={repair} openArtifact={openArtifact} />}
        <div className="toolbar">{snapshot.artifacts.filter(artifact => artifact.kind === "pdf" || artifact.path.endsWith(".pdf")).map(artifact => <button className="btn secondary" key={artifact.id} disabled={!!error} onClick={() => openArtifact(artifact.id)}>Open PDF: {artifact.title ?? artifact.path}</button>)}</div>
        {pdf && pdfShown && <FormalizationPdfView pdf={pdf} items={snapshot.items} selectedId={selectedId} selectItem={setSelectedId} companion={snapshot.source.kind === "latex"} onSelection={snapshot.source.kind === "pdf" ? setPdfSelection : undefined} />}
        <FormalizationRepair key={`${snapshot.document_id}:${selectedId}`} snapshot={snapshot} item={selected} disabled={actionBlocked} submit={repair} pdfSelection={pdfSelection} />
      </FormalizationSection>
      <FormalizationRegions key={`regions:${snapshot.document_id}`} regions={snapshot.regions} disabled={actionBlocked} submit={repair} />
      <FormalizationSection id="formalization-preparation" title="Project preparation" attention={preparationBusy}
        status={[preparationBusy ? { label: "running", tone: "running" } : snapshot.preparation?.current ? { label: "prepared and current", tone: "ok" } : { label: snapshot.preparation?.status ? snapshot.preparation.status.replaceAll("_", " ") : "not prepared" }]}>
        <FormalizationPreparation key={`preparation:${snapshot.document_id}:${snapshot.revision_id}`} snapshot={snapshot} ready={ready} onActiveChange={setPreparationBusy} />
      </FormalizationSection>
      {snapshot.draft_preview && <FormalizationSection id="formalization-draft" title="Unaccepted source draft"
        status={[{ label: "not accepted", tone: "warn" }, { label: plural(snapshot.draft_preview.artifacts.length, "file") }]}>
        <p className="notice warn">Source uncertainty remains unresolved. These files have no coverage, translation, publication or proof approval.</p>
        <p>{snapshot.draft_preview.source_diagnostics.length} source diagnostics remain attached to this draft. Run <code>{snapshot.draft_preview.run_id}</code>.</p>
        <ul>{snapshot.draft_preview.artifacts.map(artifact => <li key={artifact.id}>
          <button className="btn secondary" disabled={!ready || repairBlocked} onClick={() => openArtifact(artifact.id)}>{artifact.title ?? artifact.path}</button>
        </li>)}</ul>
        <p className="muted">Inspect the frozen draft beside the original TeX and PDF. Source repairs and independent statement review are required before proving.</p>
      </FormalizationSection>}
      <FormalizationSection id="formalization-handoff" title="Prover handoff"
        status={[snapshot.proof_completion === "checked" ? { label: "proofs checked", tone: "ok" } : snapshot.handoff ? { label: `${plural(snapshot.handoff.obligations.length, "obligation")} open` }
          : progress.accepted && progress.historical ? { label: "check currentness" } : { label: "no accepted handoff" }]}>
        <p>Only the selected, accepted revision can start proving. The controller rechecks its files, environment and exact named obligations.</p>
        {progress.historical && <p className="notice info">This saved view cannot authorize proving. Check currentness to load the accepted handoff.</p>}
        {snapshot.proof_completion && <p>Proof completion: <strong>{snapshot.proof_completion}</strong>{snapshot.proof_revision_id ? <> · proof certificate revision <code>{snapshot.proof_revision_id}</code></> : null}</p>}
        {snapshot.handoff && obligationView ? <>{snapshot.proof_completion === "checked" ? <p>All obligations in this handoff have checked proofs.</p> : snapshot.handoff.obligations.length > 0 ? <>
          <FormalizationPager noun="proof obligations" total={snapshot.handoff.obligations.length} page={obligationView.page} onPage={setObligationPage} />
          <ul>{obligationView.rows.map(obligation => <li key={obligation.id}><code>{obligation.declaration}</code> · {obligation.file}</li>)}</ul>
        </> : <p>No pending proof obligations in this handoff.</p>}</> : <p>No accepted handoff is available.</p>}
        <p className="muted">Uses the provider, model and budget selected in the Prove tab.</p>
        <button className="btn primary" disabled={!ready || actionBlocked || !snapshot.can_prove || !snapshot.handoff} onClick={() => post({ type: "proveFormalization", documentId: snapshot.document_id, revisionId: snapshot.revision_id, request: workflowForm(view, "prove") })}>Start proving accepted statements</button>
      </FormalizationSection>
    </>}
  </div>;
}
