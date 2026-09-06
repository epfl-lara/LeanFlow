/** What the active run is doing right now, read from the live status snapshot. */
import { Card, Empty, Notice, Pill, Stat, StatusPill, relativeTime } from "../components";
import { useSelectedRun, useStore } from "../store";
import { post } from "../vscodeApi";
import { ProverWorkspace } from "./ProverWorkspace";

function heartbeatAge(iso: string | undefined): number | null {
  if (!iso) {
    return null;
  }
  const then = Date.parse(iso);
  return Number.isNaN(then) ? null : (Date.now() - then) / 1000;
}

export function LiveView() {
  const { app } = useStore();
  const run = useSelectedRun();
  const status = app?.liveStatus;
  const runId = run?.runId || String(status?.run_id || "") || app?.history[0]?.run_id || "";
  const workflowKind = run?.request.kind || status?.workflow_kind || app?.history[0]?.workflow_kind || "";

  if (!app?.project.found) {
    return <Empty>Open a LeanFlow project to see live run state.</Empty>;
  }

  if (!status && !run && !runId) {
    return (
      <Empty>
        Nothing running.
        <br />
        Start a workflow from the Launch tab.
      </Empty>
    );
  }

  const age = heartbeatAge(status?.runtime_heartbeat_at);
  const stale = Boolean(status?.stale_snapshot);
  // The runtime heartbeats on its own cadence; a long gap means the process is
  // busy inside one step or gone, and the two look identical from out here.
  const quiet = age !== null && age > 120;

  return (
    <>
      {run && (
        <Card
          title={run.label}
          actions={
            <>
              <StatusPill status={run.status} />
              {(run.status === "running" || run.status === "starting") && (
                <button
                  className="btn secondary"
                  onClick={() => post({ type: "stopRun", id: run.id })}
                >
                  Stop
                </button>
              )}
            </>
          }
        >
          <pre className="raw" style={{ maxHeight: 76 }}>
            {run.command}
          </pre>
          {run.error && <Notice tone="error">{run.error}</Notice>}
          <div className="row tight" style={{ marginTop: 8 }}>
            <span className="muted">started {relativeTime(run.startedAt)}</span>
            {run.pid !== null && <span className="tag">pid {run.pid}</span>}
            {run.runId && <span className="tag">{run.runId}</span>}
            {run.appliedOverrides.set &&
              Object.keys(run.appliedOverrides.set).length +
                (run.appliedOverrides.unset?.length ?? 0) >
                0 && (
              <span className="pill">
                {Object.keys(run.appliedOverrides.set).length +
                  (run.appliedOverrides.unset?.length ?? 0)}{" "}
                knob overrides
              </span>
            )}
          </div>
        </Card>
      )}

      {stale && (
        <Notice tone="warn">
          The recorded status belongs to a process that is no longer alive. It is shown as the
          last known state.
        </Notice>
      )}
      {!stale && quiet && (
        <Notice tone="info">
          No heartbeat for {Math.round(age ?? 0)}s. A long Lean check or provider wait looks like
          this too.
        </Notice>
      )}

      {workflowKind.includes("prove") && <ProverWorkspace runId={runId} />}

      {status && (
        <>
          <div className="stats">
            <Stat label="Phase" value={status.phase ?? "—"} small />
            <Stat label="Target" value={status.target_symbol || "—"} small />
            <Stat label="File" value={status.active_file_label || "—"} small />
            <Stat
              label="Sorries here"
              value={status.sorry_count ?? "—"}
            />
            <Stat label="Project sorries" value={status.project_sorry_count ?? "—"} />
            <Stat
              label="Proved"
              value={status.proof_solved ? "yes" : "not yet"}
              small
            />
          </div>

          <Card title="Run configuration">
            <div className="stats">
              <Stat label="Provider" value={status.provider || "—"} small />
              <Stat label="Model" value={status.model || "—"} small />
              <Stat label="Skill" value={status.active_skill || "—"} small />
              <Stat label="Agents" value={status.parallel_agents ?? 1} />
              <Stat label="Held locks" value={status.held_locks ?? 0} />
              <Stat label="Checkpoints" value={status.checkpoint_count ?? 0} />
            </div>
            {status.agent_capacity && (
              <div className="row tight" style={{ marginTop: 10 }}>
                {Object.entries(status.agent_capacity).map(([key, value]) => (
                  <span key={key} className="pill">
                    {key}: {value}
                  </span>
                ))}
              </div>
            )}
          </Card>

          {status.current_blocker && (
            <Card title="Current blocker">
              <div className="mono">{status.current_blocker}</div>
            </Card>
          )}

          {(status.declaration_queue_summary || status.declaration_queue_total) && (
            <Card
              title="Declaration queue"
              actions={
                <Pill>
                  {status.declaration_queue_total ?? 0} item
                  {status.declaration_queue_total === 1 ? "" : "s"}
                </Pill>
              }
              subtitle={status.declaration_scope ? `scope: ${status.declaration_scope}` : undefined}
            >
              <pre className="raw" style={{ maxHeight: 180 }}>
                {status.declaration_queue_summary || "—"}
              </pre>
            </Card>
          )}

          {status.last_activity_message && (
            <Card title="Last activity">
              <div className="row tight" style={{ marginBottom: 6 }}>
                <span className="tag">{status.last_activity_type}</span>
                <span className="muted">{relativeTime(status.updated_at)}</span>
              </div>
              <div>{status.last_activity_message}</div>
            </Card>
          )}

          {status.diagnostics && (
            <Card title="Lean diagnostics">
              <pre className="raw" style={{ maxHeight: 220 }}>
                {status.diagnostics}
              </pre>
            </Card>
          )}

          {status.goals && (
            <Card title="Goals">
              <pre className="raw" style={{ maxHeight: 260 }}>
                {status.goals}
              </pre>
            </Card>
          )}

          {status.latest_checkpoint_label && (
            <Card title="Latest checkpoint">
              <div className="mono">{status.latest_checkpoint_label}</div>
            </Card>
          )}
        </>
      )}
    </>
  );
}
