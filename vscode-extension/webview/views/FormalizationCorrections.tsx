/** Compare original and adopted readings while keeping user decisions, local facts and acceptance separate. */
import { useEffect, useState } from "react";
import type { FormalizationAction, FormalizationItem, FormalizationLeanRef, FormalizationSourceRef } from "../../src/core/formalization";

type OpenArtifact = (artifactId: string, itemId?: string, pageIndex?: number, line?: number) => void;

function SourceLinks({ refs, item, disabled, openArtifact, label, excerpt = false }: { refs: FormalizationSourceRef[]; item: FormalizationItem; disabled: boolean; openArtifact: OpenArtifact; label: string; excerpt?: boolean }) {
  return <>{refs.map((ref, index) => <div key={index}><button className="btn secondary" disabled={disabled} onClick={() => openArtifact(ref.artifact_id, item.id, ref.page_index, ref.line_start)}>
    {label}{ref.page_index !== undefined ? ` · physical page ${ref.page_index + 1}` : ref.line_start ? ` · line ${ref.line_start}` : ` · ${index + 1}`}
  </button>{excerpt && ref.text && <pre>{ref.text}</pre>}</div>)}</>;
}

function LeanLink({ reference, itemId, disabled, openArtifact, label = "Lean" }: { reference: FormalizationLeanRef; itemId: string; disabled: boolean; openArtifact: OpenArtifact; label?: string }) {
  return <div>
    <button className="btn secondary" disabled={disabled || !reference.artifact_id} onClick={() => reference.artifact_id && openArtifact(reference.artifact_id, itemId, undefined, reference.line_start)}>{label}: {reference.name}</button>
    {!reference.artifact_id && <p className="muted">No frozen Lean source link recorded.</p>}
    {(reference.type || reference.type_hash) && <details><summary>Lean type and saved binding</summary>
      {reference.type && <pre>{reference.type}</pre>}
      <p className="muted">Available saved identities appear below. A displayed type alone does not establish compilation; currentness and source acceptance are checked separately.</p>
      {reference.type_hash && <p>Type: <code>{reference.type_hash}</code></p>}
      {reference.body_hash && <p>Body: <code>{reference.body_hash}</code></p>}
      {reference.definition_context_hash && <p>Definition context: <code>{reference.definition_context_hash}</code></p>}
    </details>}
  </div>;
}

export function FormalizationCorrections({ item, revisionId, disabled, linksDisabled, submit, openArtifact }: {
  item: FormalizationItem; revisionId: string; disabled: boolean; linksDisabled: boolean; submit: (action: FormalizationAction) => void; openArtifact: OpenArtifact;
}) {
  const [reading, setReading] = useState("");
  const [reason, setReason] = useState("");
  const [kind, setKind] = useState<"interpretation" | "convention">("interpretation");
  useEffect(() => { setReading(""); setReason(""); setKind("interpretation"); }, [revisionId, item.id]);
  const options = item.correction_authority_options ?? [];
  const policy = options.find(option => option.action.kind === "bounded_policy");
  const decision = options.find(option => option.action.kind === kind);
  const statement = `Reading: ${reading.trim()}\nReason: ${reason.trim()}`;
  const canRecord = reading.trim().length > 0 && reason.trim().length > 0 && statement.length >= 40 && statement.split(/\s+/).length >= 6 && statement.length <= 4096;
  const corrections = item.corrections ?? [];
  if (!corrections.length && !options.length) return null;
  return <section aria-label="Source corrections and interpretations">
    <h4>Source corrections and interpretations{corrections.length ? ` · ${corrections.length}` : ""}</h4>
    <p className="muted">The original source stays unchanged. A recorded choice permits a proposal; it does not approve a translation or prove a theorem.</p>
    <p className="muted">These choices are optional. Unresolved readings can remain flagged in a delivered draft; acceptance and proving are checked separately.</p>
    <details><summary>Optional correction choices · bounded policy {policy?.enabled ? "enabled" : "not enabled"}</summary>
      {policy ? <>
        <p>{policy.action.statement}</p>
        <p>Scope: {policy.action.source_item_ids.length} included source items in this frozen paper. Decision version {policy.action.version}.</p>
        <button className="btn secondary" disabled={disabled} onClick={() => submit({ ...policy.action, enabled: !policy.enabled })}>{policy.enabled ? "Revoke bounded correction policy for this paper" : "Enable bounded correction policy for this paper"}</button>
      </> : <p>Include source items before enabling a paper policy. A paper scope exceeding the supported decision size is not silently truncated.</p>}
      <details><summary>Record a specific reading or convention</summary>
        <label>Decision kind<select aria-label="Correction decision kind" disabled={disabled} value={kind} onChange={event => setKind(event.target.value as typeof kind)}><option value="interpretation">Interpretation</option><option value="convention">Convention</option></select></label>
        <label>Reading<textarea aria-label="Correction reading" maxLength={3800} disabled={disabled} value={reading} onChange={event => setReading(event.target.value)} /></label>
        <label>Reason<textarea aria-label="Correction decision reason" maxLength={3800} disabled={disabled} value={reason} onChange={event => setReason(event.target.value)} /></label>
        {decision?.current && <p>Recorded decision: {decision.current.statement}{!decision.enabled ? " · not currently enabled" : ""}</p>}
        <button className="btn secondary" disabled={disabled || !decision || !canRecord} onClick={() => decision && submit({ ...decision.action, statement, enabled: true })}>Record {kind}</button>
        {decision?.enabled && <button className="btn secondary" disabled={disabled} onClick={() => submit({ ...decision.action, statement: decision.current!.statement, enabled: false })}>Revoke this {kind}</button>}
      </details>
    </details>
    {corrections.map(correction => <article className="card" key={correction.id} aria-label={`Correction ${correction.id}`}>
      <h5>{correction.kind === "encoding_repair" ? "Encoding repair" : correction.kind === "user_interpretation" ? "Recorded interpretation" : "Proposed source correction"} · {correction.acceptance_status === "current" ? "current source acceptance" : correction.acceptance_status === "saved" ? "saved evidence; acceptance not current" : "awaiting checks"}</h5>
      <div className="formalization-source-pair"><div><h5>Original reading</h5><p>{correction.original.literal_reading}</p>
        <SourceLinks refs={correction.original.source_refs} item={item} disabled={linksDisabled} openArtifact={openArtifact} label="Original source" excerpt />
      </div><div><h5>Adopted reading</h5><p>{correction.adopted.reading}</p>
        <h5>Lean statement</h5>{correction.adopted.lean_refs.map(reference => <LeanLink key={reference.name} reference={reference} itemId={item.id} disabled={linksDisabled} openArtifact={openArtifact} />)}
      </div></div>
      <p>{correction.rationale}</p>
      <p>Paper proof: {item.proof_status}. Local correction facts and source review are separate checks.</p>
      <details><summary>Source context and local evidence · {correction.evidence_status.replaceAll("_", " ")}</summary>
        <SourceLinks refs={correction.source_context_refs} item={item} disabled={linksDisabled} openArtifact={openArtifact} label="Source context" />
        {correction.evidence.length ? correction.evidence.map(fact => <div key={fact.goal_name}><p>{fact.purpose}</p>
          <LeanLink reference={fact.goal} itemId={item.id} disabled={linksDisabled} openArtifact={openArtifact} label="Closed goal" />
          <LeanLink reference={fact.proof} itemId={item.id} disabled={linksDisabled} openArtifact={openArtifact} label="Local fact" />
        </div>) : <p>No local proof was claimed for this encoding repair.</p>}
      </details>
      <details><summary>User authority · {correction.authority_current ? "current scoped decision" : "changed or unavailable"}</summary>
        {correction.authorities.map(authority => <div key={authority.id}><p>{authority.kind.replaceAll("_", " ")} · version {authority.version} · {authority.current ? "current" : "saved"}{authority.enabled ? "" : " · revoked"}</p><p>{authority.statement}</p><p className="muted">Source scope: {authority.source_item_ids.join(", ")}</p></div>)}
      </details>
      <details><summary>Saved independent source review · {correction.reviews.length ? correction.reviews.map(review => review.decision).join(", ") : "pending"}</summary>
        {correction.reviews.map((review, index) => <div key={index}><p>{review.decision}{!review.binding_matches ? " · different saved binding" : ""}</p><p>{review.rationale}</p><p>Source support: {review.source_support}</p><p>Other readings considered: {review.competing_alternatives}</p>{review.issues.length > 0 && <ul>{review.issues.map((issue, key) => <li key={key}>{issue}</li>)}</ul>}</div>)}
      </details>
      <details><summary>Affected units · {correction.dependents.length}</summary><ul>{correction.dependents.map(unit => <li key={unit.unit_id}>{unit.unit_id}: {unit.declaration_names.join(", ")}<p className="muted">Source items: {unit.source_item_ids.join(", ")}</p></li>)}</ul></details>
    </article>)}
  </section>;
}
