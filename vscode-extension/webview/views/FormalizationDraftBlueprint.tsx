/** Navigate saved unaccepted mappings without changing canonical review or proof status. */
import { useState } from "react";
import type { FormalizationItem } from "../../src/core/formalization";
import { FormalizationPager, formalizationPage } from "./FormalizationPager";

type OpenArtifact = (artifactId: string, itemId?: string, pageIndex?: number, line?: number) => void;

/** Reuse frozen artifact navigation for independently compiled draft locations. */
export function DraftDeclarations({ item, disabled, openArtifact, showTypes = false }: {
  item: FormalizationItem; disabled: boolean; openArtifact: OpenArtifact; showTypes?: boolean;
}) {
  const mapping = item.draft_mapping;
  return <>{mapping?.lean_refs.map(ref => <div key={`${ref.file ?? "library"}:${ref.name}`}>
    {ref.artifact_id ? <button className="btn secondary" disabled={disabled}
      onClick={() => openArtifact(ref.artifact_id!, item.id, undefined, ref.line_start)}>Draft Lean: {ref.name}</button>
      : <p>{ref.name} · pinned library declaration; no frozen source link</p>}
    {ref.file && <p className="muted">{ref.file} · line {ref.line_start}</p>}
    {showTypes && ref.type && <details><summary>Saved draft type</summary><pre>{ref.type}</pre></details>}
  </div>)}
  {mapping?.issues.map((issue, index) => <p className="muted" key={index}>{issue}</p>)}
  {mapping?.notes?.map((note, index) => <p className="muted" key={`note-${index}`}>{note}</p>)}
  {!mapping && <p className="muted">No saved draft navigation is available; inspect the frozen proposal.</p>}</>;
}

/** Keep the complete source denominator visible while mounting only one page of mapping rows. */
export function FormalizationDraftBlueprint({ items, disabled, openArtifact }: {
  items: FormalizationItem[]; disabled: boolean; openArtifact: OpenArtifact;
}) {
  const [page, setPage] = useState(0);
  const visible = formalizationPage(items, page);
  const excluded = items.filter(item => item.scope === "excluded").length;
  const gaps = items.filter(item => item.scope !== "excluded" && (!item.draft_mapping?.lean_refs.length || item.draft_mapping.issues.length > 0)).length;
  return <details><summary>Unaccepted draft blueprint · {items.length} source items</summary>
    <p>{items.length} total source items · {excluded} explicitly excluded · {gaps} with navigation gaps.
      These are provisional mappings from saved compilation evidence; they grant no source acceptance or proof approval. Unresolved clauses and alternatives remain in the frozen census and review notes.</p>
    <FormalizationPager noun="draft source items" total={items.length} page={visible.page} onPage={setPage} />
    <div className="formalization-table"><table><thead><tr><th>Frozen source</th><th>Unaccepted draft declarations</th></tr></thead>
      <tbody>{visible.rows.map(item => <tr key={item.id}><td><strong>{item.title}</strong><p className="muted">{item.id} · {item.scope}</p>
        {item.source_refs.map((ref, index) => <button key={index} className="btn secondary" disabled={disabled}
          onClick={() => openArtifact(ref.artifact_id, item.id, ref.page_index, ref.line_start)}>
          Draft source: {item.title}{ref.page_index !== undefined ? ` · physical page ${ref.page_index + 1}` : ref.line_start ? ` · line ${ref.line_start}` : ""}
        </button>)}</td><td><DraftDeclarations item={item} disabled={disabled} openArtifact={openArtifact} /></td></tr>)}</tbody></table></div>
  </details>;
}
