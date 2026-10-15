/**
 * The narrow sidebar: what is running, and the quickest way to start something.
 *
 * Anything that needs width — logs, the knob catalog, sweep tables — lives in
 * the dashboard panel instead of being squeezed in here.
 */
import { Empty, Notice, Pill, relativeTime } from "../components";
import { Mark, PlayIcon, RefreshIcon } from "../icons";
import { appRunChoices } from "../../src/core/runCatalog";
import { LAUNCH_FIELD_LIMITS } from "../../src/core/types";
import { useStore } from "../store";
import { ProjectSetupCard } from "./ProjectSetupCard";
import { projectSetupBlocksLaunch } from "../../src/core/projectSetupTypes";
import { post } from "../vscodeApi";
import { CliNotice } from "../CliNotice";
import { workflowForm } from "../workflowForms";

export function SidebarView() {
  const { app, view, setForm } = useStore();
  const form = workflowForm(view, "prove");

  if (!app) {
    return <div className="sidebar muted">Loading…</div>;
  }

  if (!app.project.found) {
    return (
      <div className="sidebar">
        <CliNotice cli={app.cli} compact />
        <button className="btn" onClick={() => post({ type: "openDashboard", workflow: "formalize" })}>
          Open Dashboard
        </button>
        <p className="muted">Open Formalization in the Dashboard to inspect TeX or PDF sources before preparing a Lean project.</p>
        {app.cli.ok && <ProjectSetupCard sidebar />}
      </div>
    );
  }

  const choices = appRunChoices(app);
  const active = choices.filter(run => run.active);
  const recent = choices.filter(run => !run.active);
  const status = app.projectLiveStatus ?? app.liveStatus;

  return (
    <div className="sidebar">
      <div className="spread">
        <span className="app-title">
          <Mark size={15} className="mark" />
          {app.project.label}
        </span>
        <button
          className="btn ghost"
          onClick={() => post({ type: "refresh" })}
          title="Refresh"
        >
          <RefreshIcon size={13} />
        </button>
      </div>

      <CliNotice cli={app.cli} compact />
      {app.cli.ok && <ProjectSetupCard sidebar />}
      {view.workflowSelectionRequired && <Notice tone="warn">The saved workflow is retired. Open Prove or Formalization to choose a new configuration.</Notice>}
      {view.workflowSelectionRequired && <button className="btn ghost" onClick={() => post({ type: "openDashboard", workflow: "prove" })}>Configure Prove</button>}
      {app.runDiscoveryError && <Notice tone="warn">{app.runDiscoveryError}</Notice>}
      {app.cli.ok && <section className="card" style={{ margin: 0 }}>
        <div className="card-title">Prove</div>
        <label className="field" style={{ marginBottom: 8 }}>
          <span className="label">Target</span>
          <input
            type="text"
            list="leanflow-sidebar-files"
            maxLength={LAUNCH_FIELD_LIMITS.target}
            placeholder="Main.lean (optional)"
            value={form.target}
            onChange={(event) => setForm({ kind: "prove", target: event.target.value })}
          />
          <datalist id="leanflow-sidebar-files">
            {app.leanFiles.map((file) => (
              <option key={file} value={file} />
            ))}
          </datalist>
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={form.research}
            onChange={(event) => setForm({ kind: "prove", research: event.target.checked })}
          />
          <span className="body">
            <span>Research mode</span>
          </span>
        </label>
        <div className="row tight" style={{ marginTop: 8 }}>
          <button
            className="btn"
            disabled={!!view.workflowSelectionRequired || projectSetupBlocksLaunch(app.projectSetup, app.project.root)}
            onClick={() => post({ type: "launch", request: { ...form, kind: "prove" } })}
          >
            <PlayIcon size={12} />
            Prove
          </button>
          <button className="btn ghost" onClick={() => post({ type: "openDashboard", workflow: "formalize" })}>
            Formalization
          </button>
        </div>
      </section>}

      {status && active.length > 0 && (
        <section className="card" style={{ margin: 0 }}>
          <div className="card-title">
            <span className="pill running">
              <span className="dot" />
              {status.phase ?? "running"}
            </span>
          </div>
          <div className="muted" style={{ fontSize: 12, lineHeight: 1.6 }}>
            <div className="truncate">{status.active_file_label || "—"}</div>
            <div className="truncate">target: {status.target_symbol || "—"}</div>
            <div>
              sorries: {status.sorry_count ?? "—"} here, {status.project_sorry_count ?? "—"} in
              project
            </div>
            <div className="truncate">{String(status.model || (status.prover_mode ? `${status.prover_mode} prover` : "") || "—")}</div>
          </div>
          {status.last_activity_message && (
            <div style={{ marginTop: 8, fontSize: 11.5, lineHeight: 1.5 }}>
              <span className="tag">{status.last_activity_type}</span>
              <div style={{ marginTop: 3 }}>{status.last_activity_message}</div>
            </div>
          )}
        </section>
      )}

      {[{ label: "Live jobs", rows: active }, { label: "Past jobs", rows: recent }].map(group => (
        <div key={group.label}>
          <div className="section-heading">{group.label} · {group.rows.length}</div>
          {group.rows.length === 0 && <Empty>{group.label === "Live jobs" ? "Nothing running." : "No saved jobs yet."}</Empty>}
          <div className="job-list">
            {group.rows.map(run => <section className="job-card" key={run.id}>
              <button className="job-card-open" aria-pressed={app.selectedRunId === run.id} title={`Open ${run.label}`} onClick={() => post({ type: "openRun", id: run.id })}>
                <span className="spread">
                  <span className="truncate">{run.label}</span>
                  <Pill tone={run.active ? "running" : run.status === "failed" ? "bad" : run.status === "stopped" ? "warn" : undefined}>{run.status}</Pill>
                </span>
                <span className="muted">{relativeTime(run.updatedAt)}</span>
              </button>
              {run.active && <button className="link job-stop" onClick={() => post({ type: "stopRun", id: run.id })}>stop</button>}
            </section>)}
          </div>
        </div>
      ))}
    </div>
  );
}
