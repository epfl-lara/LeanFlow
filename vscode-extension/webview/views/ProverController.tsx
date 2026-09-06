/** What the controller is doing now: checks, builds, planning stages, capacity, and role settings. */
import type { ProverSnapshot } from "../../src/core/prover";
import { jobTimeline, roleSettings, type CapacityView, type ControllerView, type OperationView } from "../../src/core/proverProgress";
import { Card, Notice, Stat } from "../components";
import { useStore } from "../store";
import { clock, metric, pillTone, seconds } from "./proverFormat";

function OperationRow({ view }: { view: OperationView }) {
  const operation = view.operation;
  const fraction = operation.total !== null && operation.total > 0 ? Math.min(1, (operation.completed ?? 0) / operation.total) : null;
  const status = view.running ? "running" : view.failed ? "failed" : "completed";
  return <div className={`prover-op ${status}`} role="listitem">
    <div>
      <div className="prover-op-head">
        <strong>{view.label}</strong>
        {view.progress && <span className="tag">{view.progress}</span>}
        {operation.label && operation.label !== view.label && <span className="muted">{operation.label}</span>}
        {operation.file && <span className="mono muted">{operation.file}</span>}
        {operation.node_id && !operation.file && <span className="mono muted">{operation.node_id}</span>}
      </div>
      {fraction !== null && view.running && <div className="prover-progress" aria-hidden="true"><span style={{ width: `${fraction * 100}%` }} /></div>}
      <div className="prover-op-meta">
        {operation.started_at && <>started {clock(operation.started_at)} · </>}
        {view.durationS !== null && <>{view.running ? "running for" : "took"} {seconds(view.durationS)}</>}
        {view.timeoutS !== null && <> · timeout {seconds(view.timeoutS)}</>}
        {operation.job_id && <> · {operation.job_id}</>}
        {operation.error && <span className="prover-op-error"> · {operation.error}</span>}
      </div>
    </div>
    <span className={`pill ${pillTone(status)}`}>{status}</span>
  </div>;
}

export function ProverController({ state, controller, capacity, now, onJob }: {
  state: ProverSnapshot; controller: ControllerView; capacity: CapacityView; now: number; onJob: (jobId: string) => void;
}) {
  const { setView } = useStore();
  const roles = roleSettings(state);
  const orchestrator = capacity.controllerActive
    ? capacity.controllerJob ? `working · ${capacity.controllerJob.agent_id}` : "working"
    : state.terminal ? "finished" : "idle";
  return <Card title="Controller and capacity" subtitle={controller.explanation ? `${controller.phaseLabel} · ${controller.explanation}` : controller.phaseLabel}>
    <div className="stats" aria-label="Agent capacity">
      <Stat label="Orchestrator" value={orchestrator} small />
      <Stat label="Prover slots in use" value={`${capacity.proverActive} / ${metric(capacity.proverLimit)}`} />
      <Stat label="Checks in progress" value={capacity.checksActive} />
      <Stat label="Ready theorems" value={capacity.readyNodes} />
      <Stat label="Prover-requested research" value={capacity.researchActive} />
    </div>
    {capacity.waitingReason && <Notice tone="info">{capacity.waitingReason}</Notice>}
    {controller.active.length > 0 && <div className="prover-ops" role="list" aria-label="Active operations">
      <span className="label">Active operations</span>
      {controller.active.map((view) => <OperationRow key={view.operation.id} view={view} />)}
    </div>}
    {controller.recent.length > 0 && <details className="prover-ops-recent">
      <summary>Recent operations ({controller.recent.length})</summary>
      <div role="list">{controller.recent.map((view) => <OperationRow key={view.operation.id} view={view} />)}</div>
    </details>}
    {controller.planningSteps.length > 0 && <div className="prover-stages">
      <span className="label">Planning stages for the current request</span>
      {controller.planningSteps.map((stage) => {
        const timeline = stage.job ? jobTimeline(stage.job, now) : null;
        return <div className="prover-stage" key={stage.step}>
          <span>{stage.label}</span>
          {stage.job ? <>
            <button className="link mono" onClick={() => onJob(stage.job!.id)}>{stage.job.agent_id}</button>
            <span className={`pill ${pillTone(stage.job.status)}`}>{stage.job.status || "queued"}</span>
            <span className="muted">{metric(stage.job.api_calls)} / {metric(stage.job.api_budget)} calls{timeline?.durationS !== null && timeline ? ` · ${seconds(timeline.durationS)}` : ""}</span>
            <button className="btn ghost" onClick={() => setView({ tab: "logs", logAgent: stage.job!.agent_id, logRaw: false, logFilter: [] })}>Events</button>
          </> : <span className="muted">not started</span>}
        </div>;
      })}
    </div>}
    {controller.preflight && <div className="muted" style={{ marginTop: 10 }}>
      Preflight: {controller.preflight.accepted === true ? "the isolated Lean environment passed" : controller.preflight.accepted === false ? `failed — ${controller.preflight.error || "no detail recorded"}` : "result not recorded"}.
    </div>}
    {controller.finalBuild && <Notice tone={controller.finalBuild.accepted === true ? "info" : "error"}>
      <strong>Final build:</strong> {controller.finalBuild.summary}
      {(controller.finalBuild.stderr || controller.finalBuild.stdout) && <details>
        <summary>Build output</summary>
        <pre className="raw" style={{ maxHeight: 220 }}>{controller.finalBuild.stderr || controller.finalBuild.stdout}</pre>
      </details>}
    </Notice>}
    <div className="prover-roles">
      <span className="label">Models and context</span>
      <div className="muted">
        Provider <span className="mono">{state.provider || "not reported by this runtime version"}</span> · reasoning effort{" "}
        <span className="mono">{state.reasoning_effort || "not reported"}</span>. One provider and one effort setting are shared by
        every role; only the model, context size, and compression differ per role.
      </div>
      <div className="table-wrap">
        <table className="data">
          <thead><tr><th>Role</th><th>Model</th><th>Context tokens</th><th>Compression</th><th>Call allowance</th></tr></thead>
          <tbody>{roles.map((role) => <tr key={role.role}>
            <td>{role.label}</td>
            <td className="mono">{role.model || "—"}</td>
            <td>{metric(role.contextTokens)}</td>
            <td>{role.compression === null ? "—" : role.compression ? "on" : "off"}</td>
            <td>{metric(role.callsPerStage)} <span className="muted">{role.stageLabel}</span></td>
          </tr>)}</tbody>
        </table>
      </div>
    </div>
  </Card>;
}
