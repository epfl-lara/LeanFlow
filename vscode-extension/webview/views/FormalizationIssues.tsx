/** Page extraction diagnostics; long lists open on their blocking issues while every issue stays reachable. */
import { useState } from "react";
import type { FormalizationAction, FormalizationDiagnostic, FormalizationSourceRef } from "../../src/core/formalization";
import { FormalizationDiagnostics } from "./FormalizationDiagnostics";
import { FORMALIZATION_PAGE_SIZE, FormalizationPager, formalizationPage } from "./FormalizationPager";
import { FormalizationSection } from "./FormalizationSection";
import { manualSourceRepair, nativeInterpretation, plural } from "./formalizationProgress";

export function FormalizationIssues({ revisionKey, diagnostics, disabled, submit, openSource }: {
  revisionKey: string; diagnostics: (string | FormalizationDiagnostic)[]; disabled: boolean;
  submit: (action: FormalizationAction) => void; openSource: (source: FormalizationSourceRef) => void;
}) {
  const blocking = diagnostics.filter(manualSourceRepair).length;
  const interpretations = diagnostics.filter(nativeInterpretation).length;
  const [show, setShow] = useState<"blocking" | "interpretations" | "all">(blocking ? "blocking" : "all");
  const [page, setPage] = useState(0);
  if (!diagnostics.length) return null;
  const long = diagnostics.length > FORMALIZATION_PAGE_SIZE;
  const filtered = long && show === "blocking" ? diagnostics.filter(manualSourceRepair)
    : long && show === "interpretations" ? diagnostics.filter(nativeInterpretation) : diagnostics;
  const visible = formalizationPage(filtered, page);
  return <FormalizationSection id="formalization-diagnostics" title="Source issues" attention={blocking > 0}
    status={[blocking ? { label: `${blocking} blocking`, tone: "warn" } : { label: "none requiring manual repair" }, ...(interpretations ? [{ label: `${interpretations} pending native interpretation`, tone: "warn" as const }] : []), { label: plural(diagnostics.length, "issue") }]}>
    {interpretations > 0 && <p className="notice info">{plural(interpretations, "source interpretation")} await the native proposer. Configure formalization to propose exact readings; independent coverage and coherence reviews must pass before acceptance. The original source issues stay recorded.</p>}
    {long && <label>Show<select aria-label="Source issue filter" value={show} onChange={event => { setShow(event.target.value as typeof show); setPage(0); }}>
      <option value="blocking">Blocking ({blocking})</option>
      <option value="interpretations">Pending native interpretations ({interpretations})</option>
      <option value="all">All issues ({diagnostics.length})</option>
    </select></label>}
    <FormalizationPager noun="source issues" total={filtered.length} page={visible.page} onPage={setPage} filtered={filtered !== diagnostics} />
    {visible.rows.filter(nativeInterpretation).map(diagnostic => <details className="formalization-region" key={diagnostic.id}>
      <summary>{diagnostic.message} · pending native interpretation</summary>
      <p>Pending source reading and independent review. No manual approval is recorded here.</p>
      {diagnostic.source_refs.map((source, index) => <div key={index}>
        <button className="btn secondary" disabled={disabled} onClick={() => openSource(source)}>Inspect pending interpretation {diagnostic.id}, occurrence {index + 1}</button>
        {source.text && <pre>{source.text}</pre>}
      </div>)}
    </details>)}
    <FormalizationDiagnostics key={revisionKey} diagnostics={visible.rows.filter(entry => !nativeInterpretation(entry))} disabled={disabled} submit={submit} openSource={openSource} />
  </FormalizationSection>;
}
