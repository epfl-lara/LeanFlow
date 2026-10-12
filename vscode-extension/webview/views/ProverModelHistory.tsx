/** Display native model evidence and preserve changes across each job's invocations. */
import type { NativeModelSettings, ProverJob } from "../../src/core/prover";
import { modelSettingsHistory, modelSettingsSource } from "../../src/core/proverModelSettings";
import { InfoHelp } from "../InfoHelp";
import { HELP } from "../help";
import { clock } from "./proverFormat";

export function ReportedSetting({ settings, effort = false }: { settings: NativeModelSettings | null; effort?: boolean }) {
  const value = effort ? settings?.reasoning_effort : settings?.model;
  const source = effort ? settings?.reasoning_source : settings?.model_source;
  return <><span className="mono">{value || "Not reported"}</span>{value && <div className="muted">{modelSettingsSource(source || "")}</div>}</>;
}

export function ProverModelHistory({ job }: { job: ProverJob }) {
  const history = modelSettingsHistory(job);
  if (!history.length) return null;
  const invocations = [...new Set(history.map((row) => row.invocation_id))];
  return <details className="prover-model-history">
    <summary>Model history ({invocations.length} {invocations.length === 1 ? "agent call" : "agent calls"})</summary>
    <p className="muted">Settings reported by this job’s native runtime <InfoHelp label={`Model history for ${job.agent_id}`}>{HELP.reportedModels}</InfoHelp></p>
    <div className="table-wrap"><table className="data">
      <thead><tr><th>Agent call</th><th>Time</th><th>Backend</th><th>Model</th><th>Reasoning effort</th></tr></thead>
      <tbody>{history.map((row, index) => <tr key={index}>
        <td>{invocations.indexOf(row.invocation_id) + 1}</td><td>{clock(row.observed_at)}</td><td>{row.backend}</td>
        <td><ReportedSetting settings={row} /></td><td><ReportedSetting settings={row} effort /></td>
      </tr>)}</tbody>
    </table></div>
  </details>;
}
