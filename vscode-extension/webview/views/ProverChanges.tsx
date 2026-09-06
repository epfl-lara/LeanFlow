/** Files the run created or edited, pending ones first, with their transaction state. */
import type { ProverSnapshot } from "../../src/core/prover";
import { changeRows } from "../../src/core/proverProgress";
import { Card } from "../components";

export function ProverChanges({ state, onOpen, onJob }: {
  state: ProverSnapshot; onOpen: (path: string, line?: number | null, baselinePath?: string) => void; onJob: (jobId: string) => void;
}) {
  const rows = changeRows(state);
  const pending = rows.filter((row) => row.pending).length;
  return <Card title="Files changed" subtitle={pending > 0
    ? `${pending} change${pending === 1 ? "" : "s"} awaiting validation: staged by the controller and not yet published. The source transaction commits or rolls them back as a unit.`
    : "Open source changes or compare with the recorded run baseline."}>
    {rows.length === 0 && <div className="muted">No recorded edits yet. Staged helper files appear here as soon as the runtime reports them.</div>}
    {rows.map((row) => <div className={`prover-job prover-change ${row.pending ? "pending" : ""}`} key={row.change.path}>
      <button className="link mono" onClick={() => onOpen(row.change.path)}>{row.change.path}</button>
      <div className="row tight">
        <span className={`pill ${row.tone === "err" ? "bad" : row.tone}`}>{row.label}</span>
        <span className="tag">{row.change.status || "modified"}{row.pending && row.change.staged_status ? ` → ${row.change.staged_status}` : ""}</span>
        {row.job
          ? <button className="link" onClick={() => onJob(row.job!.id)}>{row.job.agent_id}</button>
          : <span className="muted">{row.change.agent_id}</span>}
        {row.change.baseline_path && <button className="btn ghost" onClick={() => onOpen(row.change.path, null, row.change.baseline_path)}>View diff</button>}
      </div>
    </div>)}
  </Card>;
}
