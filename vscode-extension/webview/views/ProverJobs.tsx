/** Agent jobs with their lifecycle, budget, timing, settings, and the files they left behind. */
import type { ProverJob, ProverSnapshot } from "../../src/core/prover";
import { isRunningOperation, operationLabel, operationProgress } from "../../src/core/proverOperations";
import { jobSettings, jobTimeline, stopUsage } from "../../src/core/proverProgress";
import { Card } from "../components";
import { useStore } from "../store";
import { artifactName, clock, isTextArtifact, metric, pillTone, seconds } from "./proverFormat";

const MAX_ARTIFACTS = 24;

export function ProverJobs({ state, now, onOpen, onJob }: {
  state: ProverSnapshot; now: number; onOpen: (path: string) => void; onJob: (jobId: string) => void;
}) {
  const { setView } = useStore();
  const nodeName = (id: string): string => state.dag.nodes.find((node) => node.id === id)?.name ?? id;
  return <Card title="Agents and jobs" subtitle="Each prover retains its API budget across local decomposition. Independent checks run outside these jobs and are listed under the controller.">
    {state.jobs.length === 0 && <div className="muted">No jobs launched yet.</div>}
    {state.jobs.map((job: ProverJob) => {
      const timeline = jobTimeline(job, now);
      const settings = jobSettings(job, state);
      const awaiting = state.operations.find((operation) => isRunningOperation(operation) && operation.job_id === job.id);
      const artifacts = job.artifacts.slice(0, MAX_ARTIFACTS);
      return <div className="prover-job" id={`prover-job-${job.id}`} key={job.id} style={job.parent_job_id ? { marginLeft: 12, borderLeft: "2px solid var(--vscode-panel-border)", paddingLeft: 12 } : undefined}>
        <div className="row"><strong>{job.agent_id}</strong><span className={`pill ${pillTone(job.status)}`}>{job.status || "queued"}</span>{job.phase && job.phase !== job.status && <span className="tag">{job.phase}</span>}</div>
        <div className="muted">{job.role || "prover"}{job.node_id ? <> · <span className="mono">{nodeName(job.node_id)}</span></> : null} · {metric(job.api_calls)} / {metric(job.api_budget)} calls
          {timeline.durationS !== null && <> · {timeline.live ? "running for" : "took"} {seconds(timeline.durationS)}</>}
          {job.started_at && <> · started {clock(job.started_at)}</>}
          {job.finished_at && <> · finished {clock(job.finished_at)}</>}
        </div>
        {awaiting && <div className="muted">Awaiting {operationLabel(awaiting).toLowerCase()}{operationProgress(awaiting) ? ` ${operationProgress(awaiting)}` : ""} inside this job's allocation.</div>}
        {(settings.model || settings.provider) && <div className="muted">
          {settings.provider && <>{settings.provider} · </>}<span className="mono">{settings.model || "model not reported"}</span>
          {settings.reasoning_effort && <> · effort {settings.reasoning_effort}</>}
          {settings.context_tokens !== null && <> · {metric(settings.context_tokens)} context tokens</>}
          {settings.compression !== null && <> · compression {settings.compression ? "on" : "off"}</>}
          {settings.source === "shared" && <> · run-level settings</>}
        </div>}
        {job.error && <div className="prover-op-error">{job.error}</div>}
        {job.stop_reason && <div className="prover-job-stop">
          <strong>Allocation ended:</strong> {job.stop_reason.message || job.stop_reason.code}
          {stopUsage(job.stop_reason) && <> ({stopUsage(job.stop_reason)})</>}
          {job.stop_reason.next_step && <span className="muted"> {job.stop_reason.next_step}</span>}
        </div>}
        {job.parent_job_id && <div className="muted">Requested by <button className="link" onClick={() => onJob(job.parent_job_id)}>{job.parent_job_id}</button></div>}
        {job.purpose && <details><summary>Job purpose</summary><p>{job.purpose}</p></details>}
        {artifacts.length > 0 && <details>
          <summary>Artifacts ({job.artifacts.length}{job.artifacts.length > MAX_ARTIFACTS ? `, first ${MAX_ARTIFACTS} shown` : ""})</summary>
          <div className="prover-artifacts">{artifacts.map((artifact) => isTextArtifact(artifact)
            ? <button className="btn ghost mono" key={artifact} title={artifact} onClick={() => onOpen(artifact)}>{artifactName(artifact)}</button>
            : <span className="tag" key={artifact} title={artifact}>{artifactName(artifact)}</span>)}</div>
        </details>}
        <div className="row tight">
          <button className="btn ghost" onClick={() => setView({ tab: "logs", logAgent: job.agent_id, logRaw: false, logFilter: [] })}>Events</button>
          {job.log_path && <button className="btn ghost" onClick={() => onOpen(job.log_path)}>Log file</button>}
          {job.scratch_path && <button className="btn ghost" onClick={() => onOpen(job.scratch_path)}>Scratch proof</button>}
          {job.result_path && <button className="btn ghost" onClick={() => onOpen(job.result_path)}>Result report</button>}
          {job.report_path && job.report_path !== job.result_path && <button className="btn ghost" onClick={() => onOpen(job.report_path)}>Session report</button>}
        </div>
      </div>;
    })}
  </Card>;
}
