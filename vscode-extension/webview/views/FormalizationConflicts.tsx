/** Inspect preserved publication versions and reconcile exact observations without discarding history. */
import { useEffect, useRef, useState } from "react";
import type { FormalizationConflictTransaction, FormalizationConflictsSnapshot } from "../../src/core/formalizationConflicts";
import type { HostMessage } from "../../src/core/types";
import { post } from "../vscodeApi";
import { FormalizationPreparationRecovery } from "./FormalizationPreparationRecovery";
import { FormalizationPdfView } from "./FormalizationPdf";

export function FormalizationConflicts({ ready, onResolved }: { ready: boolean; onResolved?: () => void }) {
  const [snapshot, setSnapshot] = useState<FormalizationConflictsSnapshot | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pdf, setPdf] = useState<Extract<HostMessage, { type: "formalizationConflictPdf" }> | null>(null);
  const callback = useRef(onResolved);
  callback.current = onResolved;
  useEffect(() => {
    const listener = (event: MessageEvent<HostMessage>) => {
      if (event.data.type === "formalizationConflictPdf") { setPdf(event.data); return; }
      if (event.data.type !== "formalizationConflicts") return;
      setPdf(null);
      setError(event.data.error); setBusy(false);
      if (event.data.snapshot) setSnapshot(event.data.snapshot);
      if (event.data.resolvedTransactionId) callback.current?.();
    };
    window.addEventListener("message", listener);
    return () => window.removeEventListener("message", listener);
  }, []);
  useEffect(() => { if (ready) post({ type: "listFormalizationConflicts" }); }, [ready]);
  const version = pdf && snapshot?.transactions.find(row => row.transaction_id === pdf.transactionId && row.observation_id === pdf.observationId)?.versions.find(row => row.version_id === pdf.versionId);
  return <><section className="card formalization-conflicts">
    <h3>Publication recovery</h3>
    <p>Inspect preserved versions when publication meets a concurrent save or loses historical evidence. Reconcile your current workspace files, then record the exact observation you checked. Available versions and acknowledged losses remain recorded.</p>
    <button className="btn" disabled={!ready} onClick={() => { setBusy(true); post({ type: "listFormalizationConflicts" }); }}>Refresh preserved publication versions</button>
    {busy && <p role="status">Checking current and preserved file versions…</p>}
    {error && <p className="notice error" role="alert">{error} Refresh and inspect the versions again before confirming a resolution.</p>}
    {snapshot && !snapshot.transactions.length && <p>No recorded publication conflicts.</p>}
    {snapshot?.transactions.map(transaction => <ConflictTransaction key={`${transaction.transaction_id}:${transaction.observation_id}`} transaction={transaction}
      disabled={!ready || busy || !!error || snapshot.transactions.some(other => other.active && other.transaction_id !== transaction.transaction_id)}
      resolve={reason => { setBusy(true); post({ type: "resolveFormalizationConflict", transactionId: transaction.transaction_id, observationId: transaction.observation_id, reason }); }} />)}
    {pdf && version && <><h4>{version.label} · {version.display_path}</h4><FormalizationPdfView pdf={pdf.pdf} items={[]} selectedId="" selectItem={() => undefined} publicationVersion /></>}
  </section><FormalizationPreparationRecovery ready={ready} onRefreshed={onResolved} /></>;
}

function ConflictTransaction({ transaction, disabled, resolve }: {
  transaction: FormalizationConflictTransaction; disabled: boolean; resolve: (reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  const [checked, setChecked] = useState(false);
  const resolved = transaction.state === "RESOLVED";
  const unavailableRegistry = transaction.unavailable_registry === true;
  const unavailableJournal = transaction.unavailable_journal === true || unavailableRegistry;
  const unavailableVersions = Object.keys(transaction.unavailable_versions ?? {});
  const complete = transaction.versions.length === transaction.version_count && transaction.next_version_offset === null;
  return <details className="formalization-region" open={!resolved}>
    <summary>Publication {transaction.transaction_id} · {transaction.state}</summary>
    <p>Mathematical revision <code>{transaction.revision_id}</code> · {transaction.version_count} recorded versions</p>
    {unavailableRegistry && <p className="notice warn">The publication registry is unavailable while publication evidence survives. Historical acceptance remains blocked. Acknowledgment preserves available evidence and requires a fresh independently accepted publication; restoring old source bytes cannot restore approval.</p>}
    {unavailableJournal && !unavailableRegistry && <p className="notice warn">The publication journal is unavailable. Historical versions and the original write plan cannot be reconstructed. Inspect the current workspace files locally; acknowledgment records this loss and requires new mathematical acceptance.</p>}
    {unavailableVersions.length > 0 && <p className="notice warn">{unavailableVersions.length} historical archive {unavailableVersions.length === 1 ? "version is" : "versions are"} unavailable or changed. Their expected identities remain recorded; acknowledgment does not restore missing bytes.</p>}
    <ul>{transaction.conflicts.map((issue, index) => <li key={index}>{issue}</li>)}</ul>
    <ol>{transaction.versions.map(version => <li key={version.version_id}>
      <strong>{version.label}</strong> · {version.display_path} · {version.sha256 === null ? "file absent" : version.sha256 === "nonregular" ? "non-regular entry: inspect locally" : `SHA-256 ${version.sha256.slice(0, 12)}…`}
      <button className="btn" disabled={disabled || !version.available} onClick={() => post({ type: "openFormalizationConflictVersion", transactionId: transaction.transaction_id, observationId: transaction.observation_id, versionId: version.version_id })}>Inspect preserved version: {version.label}</button>
    </li>)}</ol>
    {!complete && <p className="notice warn">The version inventory is incomplete. Refresh all versions before resolving.</p>}
    {transaction.resolutions.length > 0 && <details><summary>Recorded resolution history</summary><ol>{transaction.resolutions.map(entry => <li key={entry.resolution_id}>{entry.reason} · <code>{entry.resolution_id}</code></li>)}</ol></details>}
    {resolved ? <p>Resolution recorded. Current files remain in the workspace; mathematical acceptance must be re-established before proving.</p> : <>
      <p>{unavailableJournal ? "Inspect the current files through the normal workspace editor and acknowledge the unavailable history. Confirmation keeps those files and records the exact loss observation." : "Inspect available versions above and reconcile the current files through the normal workspace editor. Confirmation keeps those current files and archives available observations while recording unavailable versions."}</p>
      <label>Reconciliation note<textarea aria-label={`Publication reconciliation reason ${transaction.transaction_id}`} value={reason} maxLength={16000} onChange={event => setReason(event.target.value)} /></label>
      <label><input type="checkbox" aria-label={`Reconciled publication ${transaction.transaction_id}`} checked={checked} onChange={event => setChecked(event.target.checked)} />{unavailableJournal ? "I inspected the current workspace files and acknowledge the unavailable publication history." : "I inspected available versions, acknowledged any unavailable versions, and reconciled the current workspace files."}</label>
      <button className="btn" disabled={disabled || !complete || !checked || !reason.trim()} onClick={() => resolve(reason)}>{unavailableJournal ? "Keep current files and acknowledge unavailable history" : "Keep reconciled current files and archive resolution"}</button>
      <p className="muted">Changed hashes reject confirmation and require a fresh inspection. A new translation acceptance is required after resolution.</p>
    </>}
  </details>;
}
