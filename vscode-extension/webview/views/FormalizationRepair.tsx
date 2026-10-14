/** Collect explicit scope, source-boundary and mapping repairs without accepting review evidence. */
import { useEffect, useState } from "react";
import type { FormalizationAction, FormalizationItem, FormalizationRegion, FormalizationSnapshot, FormalizationSourceRef } from "../../src/core/formalization";

export function FormalizationRepair({ snapshot, item, disabled, submit, pdfSelection }: {
  snapshot: FormalizationSnapshot; item?: FormalizationItem; disabled: boolean; submit: (action: FormalizationAction) => void; pdfSelection?: FormalizationSourceRef | null;
}) {
  const [mode, setMode] = useState<"add" | "boundary" | "mapping" | "scope" | "transcribe" | "retry">("add");
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState("theorem");
  const [artifactId, setArtifactId] = useState("");
  const [start, setStart] = useState(1);
  const [end, setEnd] = useState(1);
  const [passage, setPassage] = useState("");
  const [scope, setScope] = useState<"included" | "excluded">("excluded");
  const [reason, setReason] = useState("");
  const [mapping, setMapping] = useState("");
  const [verified, setVerified] = useState(false);
  const [sourceIds, setSourceIds] = useState<string[]>([]);
  const [unitId, setUnitId] = useState("");
  const hasPdf = snapshot.source.kind === "pdf" || snapshot.artifacts.some(entry => entry.kind === "pdf" || entry.path.toLowerCase().endsWith(".pdf"));
  useEffect(() => { setSourceIds(item ? [item.id] : []); setUnitId(""); }, [item?.id]);
  useEffect(() => {
    if ((snapshot.source.kind === "pdf" || mode === "transcribe") && pdfSelection?.page_index !== undefined) {
      setArtifactId(pdfSelection.artifact_id); setStart(pdfSelection.page_index + 1); setPassage(pdfSelection.text ?? ""); setVerified(false);
    }
  }, [pdfSelection, snapshot.source.kind, mode]);
  const artifacts = snapshot.artifacts.filter(artifact => {
    const pdf = artifact.kind === "pdf" || artifact.path.toLowerCase().endsWith(".pdf");
    return snapshot.source.kind === "pdf" ? pdf : !pdf && (["tex", "source"].includes(artifact.kind) || artifact.path.toLowerCase().endsWith(".tex"));
  });
  const artifact = artifacts.find(entry => entry.id === artifactId) ?? artifacts[0];
  const canonicalRefs = item?.source_refs.filter(ref => artifacts.some(entry => entry.id === ref.artifact_id)) ?? [];
  const isPdf = artifact?.kind === "pdf" || artifact?.path.endsWith(".pdf");
  const refs: FormalizationSourceRef[] = artifact ? [{ artifact_id: artifact.id,
    ...(isPdf ? { page_index: Math.max(0, start - 1), text: passage } : { line_start: start, line_end: end }),
  }] : [];
  const mapped = mapping.trim() ? mapping.trim().split("\n").map(line => {
    const split = line.indexOf("|");
    const location = split < 0 ? "" : line.slice(0, split).trim();
    const name = split < 0 ? "" : line.slice(split + 1).trim();
    return location.startsWith("imports:") ? { file: "", imports: location.slice(8).split(",").map(value => value.trim()).filter(Boolean), name }
      : { file: location, name };
  }) : [];
  const valid = !disabled && (mode === "add" ? title.trim() && refs.length && (isPdf || end >= start)
    : mode === "boundary" ? item && refs.length && (isPdf || end >= start)
    : mode === "scope" ? item && reason.trim()
    : mode === "transcribe" ? hasPdf && passage.trim() && reason.trim() && verified
    : mode === "retry" ? reason.trim()
    : item && reason.trim() && sourceIds.length && mapped.length && mapped.every(ref => (ref.file.endsWith(".lean") || ref.imports?.length) && ref.name));
  const send = () => {
    if (!valid) return;
    if (mode === "add") submit({ type: "add_item", title, kind, source_refs: refs });
    else if (mode === "boundary" && item) submit({ type: "update_boundary", item_id: item.id, source_refs: refs });
    else if (mode === "scope" && item) submit({ type: "set_scope", item_id: item.id, scope, reason });
    else if (mode === "transcribe") submit({ type: "replace_page_transcription", page_index: start - 1, text: passage, reason, verified: true });
    else if (mode === "retry") submit({ type: "retry_extraction", reason });
    else if (item) submit({ type: "set_mapping", item_id: item.id, ...(sourceIds.length !== 1 || sourceIds[0] !== item.id ? { source_ids: sourceIds } : {}), ...(unitId.trim() ? { unit_id: unitId.trim() } : {}), lean_refs: mapped, rationale: reason });
  };
  return <details className="card formalization-repair">
    <summary>Repair inventory or mapping</summary>
    <p>Repairs create a new working revision and invalidate affected evidence. Only the controller can accept review and proof results.</p>
    <label>Repair kind<select aria-label="Repair kind" value={mode} onChange={event => setMode(event.target.value as typeof mode)}>
      <option value="add">Add missed source item</option>
      <option value="boundary" disabled={!item}>Change selected item's source boundary</option>
      <option value="mapping" disabled={!item}>Correct selected item's Lean mapping</option>
      <option value="scope" disabled={!item}>Change selected item's scope</option>
      <option value="transcribe" disabled={!hasPdf}>Verify or replace a complete PDF page transcription</option>
      <option value="retry">Retry extraction from the original frozen source</option>
    </select></label>
    {(mode === "add" || mode === "boundary") && <>
      {mode === "add" && <div className="formalization-fields">
        <label>Title<input aria-label="New source item title" value={title} onChange={event => setTitle(event.target.value)} maxLength={512} /></label>
        <label>Kind<select aria-label="New source item kind" value={kind} onChange={event => setKind(event.target.value)}>{["theorem", "definition", "lemma", "proposition", "corollary", "example", "conjecture", "construction"].map(value => <option key={value}>{value}</option>)}</select></label>
      </div>}
      <label>Frozen source<select aria-label="Repair source artifact" value={artifact?.id ?? ""} onChange={event => setArtifactId(event.target.value)}>
        {artifacts.map(entry => <option key={entry.id} value={entry.id}>{entry.title ?? entry.path}</option>)}
      </select></label>
      <div className="formalization-fields">
        <label>{isPdf ? "Physical page" : "First line"}<input aria-label="Source boundary start" type="number" min={1} value={start} onChange={event => setStart(Math.max(1, Number(event.target.value)))} /></label>
        {!isPdf && <label>Last line<input aria-label="Source boundary end" type="number" min={start} value={end} onChange={event => setEnd(Math.max(1, Number(event.target.value)))} /></label>}
      </div>
      {isPdf && <label>Selected source passage<textarea aria-label="Source passage" value={passage} onChange={event => setPassage(event.target.value)} maxLength={16000} placeholder="Paste or describe the passage on this physical page" /></label>}
      {mode === "boundary" && canonicalRefs.length > 1 && <p className="notice warn">This replaces all {canonicalRefs.length} statement source references with the selected boundary. Use the CLI to retain multiple source ranges.</p>}
    </>}
    {mode === "scope" && <label>Scope<select aria-label="Source scope" value={scope} onChange={event => setScope(event.target.value as typeof scope)}><option value="included">Included</option><option value="excluded">Explicitly excluded</option></select></label>}
    {mode === "mapping" && <>
      <fieldset><legend>Source claims represented together</legend>{snapshot.items.filter(source => source.scope !== "excluded").map(source => <label key={source.id}><input type="checkbox" aria-label={`Map source claim ${source.title}`} checked={sourceIds.includes(source.id)} onChange={event => setSourceIds(ids => event.target.checked ? [...ids, source.id] : ids.filter(id => id !== source.id))} />{source.title}</label>)}</fieldset>
      <label>Lean mappings (one per line: file | fully qualified declaration)<textarea aria-label="Corrected Lean mappings" value={mapping} onChange={event => setMapping(event.target.value)} placeholder={"Main/Paper/Results.lean | Paper.result\nimports:Init | Nat.add_comm"} /></label>
      <p>Existing declarations can be reused by their file or import modules. Select every source claim in a shared mapping. The controller checks all referenced declarations.</p>
      <details><summary>Select a particular review unit</summary><label>Review unit ID<input aria-label="Mapping review unit ID" value={unitId} onChange={event => setUnitId(event.target.value)} /></label><p>Use the existing unit ID to change one mapping when a source claim belongs to multiple units.</p></details>
    </>}
    {mode === "transcribe" && <>
      <label>Physical page<input aria-label="Transcription physical page" type="number" min={1} value={start} onChange={event => { setStart(Math.max(1, Number(event.target.value))); setVerified(false); }} /></label>
      <label>Complete page transcription<textarea aria-label="Complete page transcription" value={passage} maxLength={100000} onChange={event => { setPassage(event.target.value); setVerified(false); }} /></label>
      <label><input type="checkbox" aria-label="Full transcription checked against frozen page" checked={verified} onChange={event => setVerified(event.target.checked)} />I checked the full transcription against the frozen page.</label>
      <p>Include every formula and mathematically relevant diagram or rasterized clause. Replacing a page reopens its coverage and the affected statement boundaries, mappings and proof permission. Original PDF bytes and earlier revisions remain available. The repaired items still require independent translation review.</p>
    </>}
    {mode === "retry" && <p className="notice warn">Retry after installing or fixing the extraction tools. This rereads the unchanged frozen source, reopens inventory repairs, and removes current mappings, reviews and proof permissions. Earlier revisions preserve your previous work. A different source version must be selected as a new intake.</p>}
    {(mode === "scope" || mode === "mapping" || mode === "transcribe" || mode === "retry") && <label>Reason<textarea aria-label="Repair reason" value={reason} onChange={event => setReason(event.target.value)} maxLength={16000} /></label>}
    <button className="btn" disabled={!valid} onClick={send}>Submit repair for {snapshot.revision_id}</button>
  </details>;
}

export function FormalizationRegionRepair({ region, disabled, submit }: {
  region: FormalizationRegion; disabled: boolean; submit: (action: FormalizationAction) => void;
}) {
  const [classification, setClassification] = useState<"non_mathematical" | "excluded" | "supporting_context">("supporting_context");
  const [reason, setReason] = useState("");
  return <details className="formalization-region">
    <summary>{region.title ?? region.id} · {region.classification ?? region.disposition ?? "unresolved"}</summary>
    {region.reason && <p>{region.reason}</p>}
    {region.source_refs?.map((ref, index) => <p key={index}>{ref.file ?? ref.artifact_id}{ref.page_index !== undefined ? ` · physical page ${ref.page_index + 1}` : ` · lines ${ref.line_start}–${ref.line_end}`}</p>)}
    <p>Link missing mathematical content using “Add missed source item.” Classification alone cannot mark a claim reviewed.</p>
    <label>Region disposition<select aria-label={`Disposition for ${region.id}`} value={classification} onChange={event => setClassification(event.target.value as typeof classification)}>
      <option value="supporting_context">Supporting mathematical context</option><option value="non_mathematical">Non-mathematical material</option><option value="excluded">Explicitly excluded from scope</option>
    </select></label>
    <label>Reason<input aria-label={`Reason for ${region.id}`} value={reason} onChange={event => setReason(event.target.value)} maxLength={16000} /></label>
    <button className="btn" disabled={disabled || !reason.trim()} onClick={() => submit({ type: "classify_region", region_id: region.id, classification, reason })}>Record region disposition</button>
  </details>;
}
