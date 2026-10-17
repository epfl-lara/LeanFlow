/** Record checked source interpretations while retaining every branch and review obligation. */
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
    <p>Original diagnostics remain visible alongside recorded interpretations and independent reviews. All original branches, candidate items and coverage obligations remain visible.</p>
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
  const conditional = diagnostic.code === "unresolved_conditional";
  const savedReview = diagnostic.native_interpretation_status === "saved_reviewed";
  const subject = conditional ? "Conditional" : "Command";
  const resolvable = !savedReview && (diagnostic.code === "unresolved_command" && diagnostic.can_resolve_command || conditional && diagnostic.can_resolve_conditional === true);
  const valid = !disabled && resolvable && interpretation.trim() && reason.trim() && verified;
  return <details className="formalization-region">
    <summary>{diagnostic.message} · {savedReview ? "source reading independently reviewed (saved)" : resolution ? "interpretation recorded" : diagnostic.resolution ? "source repair recorded" : diagnostic.blocking ? "blocking" : "information"}</summary>
    {savedReview && <p className="muted">Saved source-reading review for this revision. Translation and current acceptance are shown separately.</p>}
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
      {conditional && <p className="notice warn">This records your interpretation; it does not select or exclude a branch. Check every alternative against the original TeX and rendered document. Loader effects, inherited meanings and other source issues retain their own gates.</p>}
      <label>{subject} meaning at every occurrence<textarea aria-label={`${subject} interpretation for ${diagnostic.id}`} maxLength={16000} value={interpretation} onChange={event => { setInterpretation(event.target.value); setVerified(false); }} /></label>
      <label>Source evidence and reason<textarea aria-label={`${subject} evidence for ${diagnostic.id}`} maxLength={16000} value={reason} onChange={event => { setReason(event.target.value); setVerified(false); }} /></label>
      <label><input type="checkbox" aria-label={`Verified all occurrences for ${diagnostic.id}`} checked={verified} onChange={event => setVerified(event.target.checked)} />I checked the rendered meaning against every frozen source occurrence shown above.</label>
      <button className="btn" disabled={!valid} onClick={() => {
        if (valid) submit({ type: conditional ? "resolve_conditional" : "resolve_command", diagnostic_id: diagnostic.id, interpretation, reason, verified: true });
      }}>{resolution ? `Update ${subject.toLowerCase()} interpretation` : `Record ${subject.toLowerCase()} interpretation`}</button>
    </>}
    {!resolvable && diagnostic.blocking && !diagnostic.resolution && <p>Repair the reported source problem and inspect it again. This diagnostic cannot be cleared by recording a command interpretation.</p>}
  </details>;
}
