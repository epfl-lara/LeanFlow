/** Reconcile command meaning against frozen occurrences without approving extraction or review. */
import { useState } from "react";
import type { FormalizationAction, FormalizationDiagnostic, FormalizationSourceRef } from "../../src/core/formalization";

export function FormalizationDiagnostics({ diagnostics, disabled, submit, openSource }: {
  diagnostics: (string | FormalizationDiagnostic)[];
  disabled: boolean;
  submit: (action: FormalizationAction) => void;
  openSource: (source: FormalizationSourceRef) => void;
}) {
  if (!diagnostics.length) return null;
  return <section className="card"><h3>Extraction diagnostics</h3>
    <p>Resolve source uncertainty before independent review. Recording a command's meaning keeps its source items and coverage obligations in the inventory.</p>
    {diagnostics.map((diagnostic, index) => typeof diagnostic === "string"
      ? <p key={index} className="notice warn">{diagnostic}</p>
      : <CommandDiagnostic key={diagnostic.id} diagnostic={diagnostic} disabled={disabled} submit={submit} openSource={openSource} />)}
  </section>;
}

function CommandDiagnostic({ diagnostic, disabled, submit, openSource }: {
  diagnostic: FormalizationDiagnostic;
  disabled: boolean;
  submit: (action: FormalizationAction) => void;
  openSource: (source: FormalizationSourceRef) => void;
}) {
  const resolution = typeof diagnostic.resolution === "object" ? diagnostic.resolution : null;
  const [interpretation, setInterpretation] = useState(resolution?.interpretation ?? "");
  const [reason, setReason] = useState("");
  const [verified, setVerified] = useState(false);
  const resolvable = diagnostic.code === "unresolved_command" && diagnostic.can_resolve_command;
  const valid = !disabled && resolvable && interpretation.trim() && reason.trim() && verified;
  return <details className="formalization-region">
    <summary>{diagnostic.message} · {resolution ? "interpretation recorded" : diagnostic.resolution ? "source repair recorded" : diagnostic.blocking ? "blocking" : "information"}</summary>
    {diagnostic.source_refs.length > 0 && <ol>{diagnostic.source_refs.map((source, index) => <li key={index}>
      <button className="btn" disabled={disabled} onClick={() => openSource(source)}>
        Occurrence {index + 1}: {source.file ?? diagnostic.file ?? "Frozen source"}{source.page_index !== undefined ? ` · physical page ${source.page_index + 1}` : source.line_start ? ` · line ${source.line_start}` : ""}
      </button>{source.text && <pre>{source.text}</pre>}
    </li>)}</ol>}
    {resolution && <>
      <p><strong>User interpretation:</strong> {resolution.interpretation}</p>
      <p>Reason: {resolution.reason}</p>
      <p className="muted">Recorded source interpretation; mathematical translation still requires independent review.</p>
    </>}
    {typeof diagnostic.resolution === "string" && <p>Source repair: {diagnostic.resolution === "verified_transcription" ? "verified page transcription" : diagnostic.resolution === "explicit_source_boundary" ? "statement boundary re-established" : "explicit scope exclusion"}{diagnostic.resolution_reason ? ` · ${diagnostic.resolution_reason}` : ""}</p>}
    {resolvable && <>
      <label>Command meaning at every occurrence<textarea aria-label={`Command interpretation for ${diagnostic.id}`} maxLength={16000} value={interpretation} onChange={event => { setInterpretation(event.target.value); setVerified(false); }} /></label>
      <label>Source evidence and reason<textarea aria-label={`Command evidence for ${diagnostic.id}`} maxLength={16000} value={reason} onChange={event => { setReason(event.target.value); setVerified(false); }} /></label>
      <label><input type="checkbox" aria-label={`Verified all occurrences for ${diagnostic.id}`} checked={verified} onChange={event => setVerified(event.target.checked)} />I checked the rendered meaning against every frozen source occurrence shown above.</label>
      <button className="btn" disabled={!valid} onClick={() => {
        if (valid) submit({ type: "resolve_command", diagnostic_id: diagnostic.id, interpretation, reason, verified: true });
      }}>{resolution ? "Update command interpretation" : "Record command interpretation"}</button>
    </>}
    {!resolvable && diagnostic.blocking && !diagnostic.resolution && <p>Repair the reported source problem and inspect it again. This diagnostic cannot be cleared by recording a command interpretation.</p>}
  </details>;
}
