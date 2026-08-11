/**
 * The narrow sidebar: what is running, and the quickest way to start something.
 *
 * Anything that needs width — logs, the knob catalog, sweep tables — lives in
 * the dashboard panel instead of being squeezed in here.
 */
import { Empty, Notice, StatusPill, relativeTime } from "../components";
import { Mark, PlayIcon, RefreshIcon } from "../icons";
import { isActiveLiveStatus } from "../../src/core/runSelection";
import { LAUNCH_FIELD_LIMITS } from "../../src/core/types";
import { useStore } from "../store";
import { post } from "../vscodeApi";

export function SidebarView() {
  const { app, view, setForm } = useStore();
  const form = view.form;

  if (!app) {
    return <div className="sidebar muted">Loading…</div>;
  }

  if (!app.cli.ok) {
    return (
      <div className="sidebar">
        <Notice tone="error">
          <strong>LeanFlow CLI setup is required</strong>
          <div style={{ marginTop: 4 }}>{app.cli.error}</div>
          <div className="row tight" style={{ marginTop: 8 }}>
            <button
              className="btn"
              onClick={() => post({ type: "openExternalDoc", topic: "install" })}
            >
              Install guide
            </button>
            <button
              className="btn ghost"
              onClick={() => post({ type: "openCliSettings" })}
            >
              CLI path
            </button>
          </div>
        </Notice>
      </div>
    );
  }

  if (!app.project.found) {
    return (
      <div className="sidebar">
        <Notice tone="warn">
          No LeanFlow project in this workspace.
        </Notice>
        <button
          className="btn"
          onClick={() => post({ type: "openExternalDoc", topic: "project-init" })}
        >
          How to register a project
        </button>
      </div>
    );
  }

  const active = app.runs.filter(
    (run) => run.status === "running" || run.status === "starting",
  );
  const recent = app.runs.filter(
    (run) => run.status !== "running" && run.status !== "starting",
  );
  const status = app.liveStatus;
  const observedExternalRun = active.length === 0 && isActiveLiveStatus(status);

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

      <section className="card" style={{ margin: 0 }}>
        <div className="card-title">Quick launch</div>
        <label className="field" style={{ marginBottom: 8 }}>
          <span className="label">Target</span>
          <input
            type="text"
            list="leanflow-sidebar-files"
            maxLength={LAUNCH_FIELD_LIMITS.target}
            placeholder="Main.lean (optional)"
            value={form.target}
            onChange={(event) => setForm({ target: event.target.value })}
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
            onChange={(event) => setForm({ research: event.target.checked })}
          />
          <span className="body">
            <span>Research mode</span>
          </span>
        </label>
        <div className="row tight" style={{ marginTop: 8 }}>
          <button
            className="btn"
            onClick={() => post({ type: "launch", request: { ...form, kind: "prove" } })}
          >
            <PlayIcon size={12} />
            Prove
          </button>
          <button className="btn ghost" onClick={() => post({ type: "openDashboard" })}>
            Dashboard
          </button>
        </div>
      </section>

      {status && (active.length > 0 || observedExternalRun) && (
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
            <div className="truncate">{status.model || "—"}</div>
          </div>
          {status.last_activity_message && (
            <div style={{ marginTop: 8, fontSize: 11.5, lineHeight: 1.5 }}>
              <span className="tag">{status.last_activity_type}</span>
              <div style={{ marginTop: 3 }}>{status.last_activity_message}</div>
            </div>
          )}
        </section>
      )}

      <div>
        <div className="section-heading">Active</div>
        {active.length === 0 && !observedExternalRun ? (
          <Empty>Nothing running.</Empty>
        ) : (
          <>
            {observedExternalRun && (
              <section className="card" style={{ marginBottom: 8 }}>
                <div className="spread">
                  <span className="truncate">
                    {status?.workflow_kind || "workflow"} · {status?.active_file_label || "project"}
                  </span>
                  <span className="pill running">
                    <span className="dot" />
                    {status?.phase || "running"}
                  </span>
                </div>
                <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
                  Observed from the project’s verified live owner
                </div>
              </section>
            )}
            {active.map((run) => (
            <section className="card" key={run.id} style={{ marginBottom: 8 }}>
              <div className="spread">
                <span className="truncate">{run.label}</span>
                <StatusPill status={run.status} />
              </div>
              <div className="row tight" style={{ marginTop: 6 }}>
                <span className="muted">{relativeTime(run.startedAt)}</span>
                <button
                  className="link"
                  onClick={() => post({ type: "stopRun", id: run.id })}
                >
                  stop
                </button>
              </div>
            </section>
            ))}
          </>
        )}
      </div>

      {recent.length > 0 && (
        <div>
          <div className="section-heading">Recent</div>
          {recent.slice(0, 6).map((run) => (
            <section className="card" key={run.id} style={{ marginBottom: 8 }}>
              <div className="spread">
                <span className="truncate">{run.label}</span>
                <StatusPill status={run.status} />
              </div>
              <div className="muted" style={{ fontSize: 11 }}>
                {relativeTime(run.finishedAt ?? run.startedAt)}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
