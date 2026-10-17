/** Reconcile preserved infrastructure entries without claiming their directories were rendered or reviewed. */
import { useEffect, useRef, useState } from "react";
import type { FormalizationPreparationObservation, FormalizationPreparationRecoverySnapshot } from "../../src/core/formalizationPreparationRecovery";
import type { HostMessage } from "../../src/core/types";
import { post } from "../vscodeApi";

export function FormalizationPreparationRecovery({ ready, onRefreshed }: { ready: boolean; onRefreshed?: () => void }) {
  const [snapshot, setSnapshot] = useState<FormalizationPreparationRecoverySnapshot | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [cancellable, setCancellable] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const callback = useRef(onRefreshed);
  callback.current = onRefreshed;
  useEffect(() => {
    const receive = (event: MessageEvent<HostMessage>) => {
      if (event.data.type !== "formalizationPreparationRecovery") return;
      if (event.data.snapshot) setSnapshot(event.data.snapshot);
      setError(event.data.error); setMessage(event.data.message ?? ""); setBusy(event.data.busy === true);
      setCancellable(event.data.cancellable === true); setCancelling(event.data.cancelling === true);
      if (event.data.refreshed) callback.current?.();
    };
    window.addEventListener("message", receive);
    if (ready) { setBusy(true); setMessage("Inspecting infrastructure recovery…"); post({ type: "listFormalizationPreparationRecovery" }); }
    return () => window.removeEventListener("message", receive);
  }, [ready]);
  return <section className="card formalization-preparation-recovery"><h3>Project preparation recovery</h3>
    <p>Interrupted dependency setup has its own recovery history. Directory identities below cover actual infrastructure entries; large dependency directories require local inspection in the workspace.</p>
    <div className="toolbar">
      <button className="btn" disabled={!ready || busy} onClick={() => { setBusy(true); setCancellable(false); setMessage("Inspecting current infrastructure and recovery observations…"); post({ type: "listFormalizationPreparationRecovery" }); }}>Inspect infrastructure recovery</button>
      <button className="btn" disabled={!ready || busy} onClick={() => { setBusy(true); setCancellable(false); post({ type: "recoverFormalizationPreparation" }); }}>Recover interrupted project preparation</button>
      <button className="btn" disabled={!busy || !cancellable || cancelling} onClick={() => { setCancelling(true); setCancellable(false); setMessage("Cancellation requested. Waiting for safe infrastructure cleanup…"); post({ type: "cancelFormalizationRecovery" }); }}>Cancel infrastructure recovery</button>
    </div>
    {message && <p role={busy ? "status" : undefined}>{message}</p>}
    {error && <p className="notice error" role="alert">{error} Refresh the observed entries before resolving.</p>}
    {snapshot && !snapshot.transactions.length && <p>No recorded infrastructure conflicts. Recovery can still reconcile an interrupted transaction.</p>}
    {snapshot?.transactions.map(transaction => <PreparationConflict key={`${transaction.transaction_id}:${transaction.observation_id}`} transaction={transaction} disabled={!ready || busy || !!error} resolve={reason => { setBusy(true); setCancellable(false); post({ type: "resolveFormalizationPreparation", transactionId: transaction.transaction_id, observationId: transaction.observation_id, reason }); }} />)}
  </section>;
}

function PreparationConflict({ transaction, disabled, resolve }: { transaction: FormalizationPreparationObservation; disabled: boolean; resolve: (reason: string) => void }) {
  const [reason, setReason] = useState("");
  const [checked, setChecked] = useState(false);
  const resolvable = transaction.resolvable ?? (transaction.state === "CONFLICTED" && transaction.active);
  const revokes = transaction.revokes_current_receipt ?? transaction.active;
  return <details className="formalization-region" open={resolvable}><summary>Infrastructure {transaction.transaction_id} · {transaction.state}</summary>
    <ul>{transaction.conflicts.map((issue, index) => <li key={index}>{issue}</li>)}</ul>
    <p>Current infrastructure</p><ul>{Object.entries(transaction.destinations).map(([path, hash]) => <li key={path}>{path} · {hash ?? "absent"}</li>)}</ul>
    <p>Retained entries for local inspection</p><ul>{Object.entries(transaction.retained_slots).map(([path, hash]) => <li key={path}>{path} · {hash ?? "absent"}</li>)}</ul>
    {!!Object.keys(transaction.unavailable_versions ?? {}).length && <div className="notice warn"><p>These recorded versions are unavailable. Their expected identities remain in history; this resolution cannot restore their bytes.</p><ul>{Object.entries(transaction.unavailable_versions ?? {}).map(([path, hash]) => <li key={path}>{path} · expected {hash}</li>)}</ul></div>}
    {transaction.resolutions.length > 0 && <p>Earlier resolutions: {transaction.resolutions.map(entry => entry.reason).join(" · ")}</p>}
    {resolvable && <>
      <p>Reconcile the current configuration and dependency directories locally. Confirmation preserves available entries and records any unavailable versions.{revokes ? " It revokes the selected preparation receipt." : " This historical resolution leaves the newer preparation receipt in place."}</p>
      <label>Infrastructure reconciliation note<textarea aria-label={`Infrastructure reconciliation reason ${transaction.transaction_id}`} value={reason} maxLength={16000} onChange={event => setReason(event.target.value)} /></label>
      <label><input type="checkbox" aria-label={`Inspected infrastructure ${transaction.transaction_id}`} checked={checked} onChange={event => setChecked(event.target.checked)} />I inspected current infrastructure and available retained entries locally, reconciled the current versions, and acknowledge any unavailable versions listed above.</label>
      <button className="btn" disabled={disabled || !checked || !reason.trim()} onClick={() => resolve(reason)}>Keep current infrastructure and archive resolution</button>
      <p className="muted">Changed observations require another inspection.{revokes ? " Prepare the project again after resolution." : " Refresh current project readiness after resolution."}</p>
    </>}
  </details>;
}
