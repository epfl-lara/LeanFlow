/** Navigate frozen source items in bounded pages and inspect one item's source and Lean evidence. */
import { useEffect, useState } from "react";
import { proofAuthorizationLabel, type FormalizationAction, type FormalizationItem } from "../../src/core/formalization";
import { FORMALIZATION_PAGE_SIZE, FormalizationPager, formalizationPage } from "./FormalizationPager";
import { FormalizationProofAuthorization } from "./FormalizationProofAuthorization";
import { FormalizationCorrections } from "./FormalizationCorrections";

export function FormalizationInventory({ items, selectedId, selectItem, candidates, historical }: {
  items: FormalizationItem[]; selectedId: string; selectItem: (id: string) => void; candidates: FormalizationItem[] | null; historical: boolean;
}) {
  const [filter, setFilter] = useState("");
  const [page, setPage] = useState(0);
  const needle = filter.toLowerCase();
  const filtered = needle ? items.filter(item => [item.title, item.kind, item.review_status, item.proof_status, item.scope].join(" ").toLowerCase().includes(needle)) : items;
  const visible = formalizationPage(filtered, page);
  // Follow selections made from the editor, PDF or candidate lists to the page that lists them.
  useEffect(() => {
    const index = filtered.findIndex(item => item.id === selectedId);
    if (index >= 0) setPage(Math.floor(index / FORMALIZATION_PAGE_SIZE));
  }, [selectedId]);
  return <>
    {candidates && <div aria-live="polite"><p>Editor selection: {candidates.length} candidate source items.</p>
      {candidates.slice(0, FORMALIZATION_PAGE_SIZE).map(item => <button className="btn" key={item.id} onClick={() => selectItem(item.id)}>{item.title}</button>)}
      {candidates.length > FORMALIZATION_PAGE_SIZE && <p className="muted">Showing the first {FORMALIZATION_PAGE_SIZE}. Narrow the editor selection to list the others.</p>}</div>}
    <input aria-label="Filter source items" placeholder="Filter by title, kind or status" value={filter} onChange={event => { setFilter(event.target.value); setPage(0); }} />
    {historical && <p className="muted">Review results are saved records from earlier runs. Check currentness before relying on them.</p>}
    <FormalizationPager noun="source items" total={filtered.length} page={visible.page} onPage={setPage} filtered={!!needle} />
    <div className="formalization-table"><table><thead><tr><th>Source item</th><th>Scope / claim</th><th>{historical ? "Saved source review" : "Source review"}</th><th>Lean elaboration</th><th>Proof</th><th>Proof permission</th></tr></thead>
      <tbody>{visible.rows.map(item => <tr key={item.id} className={item.id === selectedId ? "selected" : ""}>
        <td><button className="link" onClick={() => selectItem(item.id)}>{item.title}</button><div className="muted">{item.kind}</div></td>
        <td>{item.scope} · {item.claim_status || "unclassified"}</td><td>{item.review_status || "unreviewed"}</td><td>{item.elaboration_status || "unchecked"}</td><td>{item.proof_status || "unknown"}</td><td>{proofAuthorizationLabel(item)}</td>
      </tr>)}</tbody></table></div>
    {items.length === 0 && <p>No mathematical items have been inventoried. Resolve missing regions or add items before review.</p>}
  </>;
}

export function FormalizationItemDetail({ item, revisionId, historical, disabled, correctionsDisabled, linksDisabled, submit, openArtifact }: {
  item: FormalizationItem; revisionId: string; historical: boolean; disabled: boolean; linksDisabled: boolean; submit: (action: FormalizationAction) => void;
  correctionsDisabled?: boolean;
  openArtifact: (artifactId: string, itemId?: string, pageIndex?: number, line?: number) => void;
}) {
  const [clausePage, setClausePage] = useState(0);
  useEffect(() => { setClausePage(0); }, [item.id]);
  const clauses = formalizationPage(item.known_obligations ?? [], clausePage);
  return <section className="card" aria-label="Selected source item">
    <h3>{item.title}</h3>
    <p>Source claim: {item.claim_status || "unclassified"}{item.extracted_claim_status ? ` · extraction: ${item.extracted_claim_status}` : ""}</p>
    {item.claim_status_rationale && <p>{item.claim_status_rationale}</p>}
    <div className="formalization-source-pair"><div><h4>Frozen source</h4>
      {item.source_refs.map((ref, index) => <div key={index} className="formalization-source-ref">
        <button className="btn secondary" disabled={linksDisabled} onClick={() => openArtifact(ref.artifact_id, item.id, ref.page_index, ref.line_start)}>
          {ref.file ?? "Source"}{ref.page_index !== undefined ? ` · physical page ${ref.page_index + 1}` : ref.line_start ? ` · line ${ref.line_start}` : ""}
        </button>
        <span className="tag">{ref.precision ?? "missing alignment"}</span>{ref.text && <pre>{ref.text}</pre>}
      </div>)}
      {!item.source_refs.length && <p className="notice warn">Missing source boundary. Repair it before review.</p>}
    </div><div><h4>Lean declarations</h4>{item.lean_refs.map((ref, index) => <div key={index}>
      <button className="btn secondary" disabled={linksDisabled || !ref.artifact_id} onClick={() => ref.artifact_id && openArtifact(ref.artifact_id, item.id, undefined, ref.line_start)}>{ref.name}</button>
      <p className="muted">{ref.file}{!ref.artifact_id ? " · no verified source link" : ""}</p>
    </div>)}{!item.lean_refs.length && <p>No Lean mapping recorded.</p>}</div></div>
    {item.assumptions?.length ? <p>Assumptions: {item.assumptions.join("; ")}</p> : null}
    {item.representation_notes && <p>Representation: {item.representation_notes}</p>}
    {item.mapping_rationale && <p>Mapping rationale: {item.mapping_rationale}</p>}
    {item.feedback?.length ? <ul>{item.feedback.map((entry, index) => <li key={index}>{entry}</li>)}</ul> : null}
    <FormalizationCorrections item={item} revisionId={revisionId} disabled={disabled || !!correctionsDisabled} linksDisabled={linksDisabled} submit={submit} openArtifact={openArtifact} />
    {!!item.known_obligations?.length && <section aria-label="Known source clauses">
      <h4>Known source clauses · {item.known_obligations.length}</h4>
      <p className="muted">These are explicitly identified source requirements. The checklist does not establish that every mathematical clause was discovered. Author proposals still need independent review.</p>
      {historical && <p className="notice info">Clause verdicts below are saved review records. Check currentness before relying on acceptance.</p>}
      <FormalizationPager noun="source clauses" total={item.known_obligations.length} page={clauses.page} onPage={setClausePage} />
      <ol>{clauses.rows.map(clause => <li key={clause.id}><details>
        <summary>{clause.description} · {clause.verdicts.length ? `${historical ? "saved " : ""}independent review: ${clause.verdicts.map(verdict => verdict.decision).join(", ")}` : "independent review pending"}</summary>
        <p>Why recorded: {clause.reason}</p>
        {clause.source_refs.map((ref, index) => <div className="formalization-source-ref" key={index}>
          <button className="btn secondary" disabled={linksDisabled} onClick={() => openArtifact(ref.artifact_id, item.id, ref.page_index, ref.line_start)}>Inspect source clause {clause.id}, occurrence {index + 1}</button>
          <span className="muted">{ref.file}{ref.page_index !== undefined ? ` · physical page ${ref.page_index + 1}` : ref.line_start ? ` · line ${ref.line_start}` : ""}</span>
          {ref.text && <pre>{ref.text}</pre>}
        </div>)}
        <h5>Author proposal</h5>
        {clause.dispositions.length ? <ul>{clause.dispositions.map((row, index) => <li key={index}>
          {row.unit_id}: <strong>{row.disposition.replaceAll("_", " ")}</strong>{row.declaration_names.length > 0 && <> · {row.declaration_names.join(", ")}</>}<p>{row.rationale}</p>
        </li>)}</ul> : <p>No authored disposition recorded.</p>}
        <h5>{historical ? "Saved independent clause review" : "Independent clause review"}</h5>
        {clause.verdicts.length ? <ul>{clause.verdicts.map((row, index) => <li key={index}>
          <strong>{row.decision}</strong>{row.declaration_names.length > 0 && <> · {row.declaration_names.join(", ")}</>}<p>{row.rationale}</p>
        </li>)}</ul> : <p>Pending independent review; no clause approval recorded.</p>}
      </details></li>)}</ol>
    </section>}
    <FormalizationProofAuthorization key={`${revisionId}:${item.id}`} item={item} disabled={disabled} submit={submit} />
  </section>;
}
