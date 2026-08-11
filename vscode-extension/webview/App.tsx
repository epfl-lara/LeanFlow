/** Shell: header, left nav, and the active view. */
import { useEffect } from "react";

import { Pill } from "./components";
import {
  KnobsIcon,
  LogsIcon,
  Mark,
  PlayIcon,
  PulseIcon,
  RefreshIcon,
  SweepIcon,
} from "./icons";
import { useStore, type TabId } from "./store";
import { KnobsView } from "./views/KnobsView";
import { LaunchView } from "./views/LaunchView";
import { LiveView } from "./views/LiveView";
import { LogsView } from "./views/LogsView";
import { SidebarView } from "./views/SidebarView";
import { SweepsView } from "./views/SweepsView";
import { mode, post } from "./vscodeApi";

const TABS: { id: TabId; label: string; Icon: typeof PlayIcon }[] = [
  { id: "launch", label: "Launch", Icon: PlayIcon },
  { id: "live", label: "Live", Icon: PulseIcon },
  { id: "logs", label: "Logs", Icon: LogsIcon },
  { id: "knobs", label: "Knobs", Icon: KnobsIcon },
  { id: "sweeps", label: "Sweeps", Icon: SweepIcon },
];

function Toasts() {
  const { toasts, dismissToast } = useStore();
  useEffect(() => {
    if (toasts.length === 0) {
      return;
    }
    const newest = toasts[toasts.length - 1];
    // Errors stay until dismissed; routine confirmations fade.
    if (newest.level === "error") {
      return;
    }
    const handle = setTimeout(() => dismissToast(newest.id), 4000);
    return () => clearTimeout(handle);
  }, [toasts, dismissToast]);

  if (toasts.length === 0) {
    return null;
  }
  return (
    <div
      aria-atomic="false"
      aria-live="polite"
      className="toast-stack"
      style={{ padding: "8px 14px 0" }}
    >
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`notice ${toast.level}`}
          role={toast.level === "error" ? "alert" : "status"}
        >
          <span>{toast.message}</span>
          <button
            aria-label="Dismiss notification"
            className="link"
            onClick={() => dismissToast(toast.id)}
            type="button"
          >
            Dismiss
          </button>
        </div>
      ))}
    </div>
  );
}

export function App() {
  const { app, view, setView } = useStore();

  if (mode === "sidebar") {
    return (
      <>
        <Toasts />
        <SidebarView />
      </>
    );
  }

  const activeCount =
    app?.runs.filter((run) => run.status === "running" || run.status === "starting").length ?? 0;
  const sweepCount = app?.experiments.length ?? 0;
  const knobCount = app?.catalog?.count ?? 0;

  const counts: Partial<Record<TabId, number>> = {
    live: activeCount,
    knobs: knobCount,
    sweeps: sweepCount,
  };

  const body = () => {
    switch (view.tab) {
      case "launch":
        return <LaunchView />;
      case "live":
        return <LiveView />;
      case "logs":
        return <LogsView />;
      case "knobs":
        return <KnobsView />;
      case "sweeps":
        return <SweepsView />;
      default:
        return null;
    }
  };

  return (
    <div className="app">
      <header className="app-header">
        <span className="app-title">
          <Mark size={17} className="mark" />
          LeanFlow
          {app?.project.found && <span className="project">· {app.project.label}</span>}
        </span>
        <span className="spacer" />
        {app?.busy && <span className="muted">refreshing…</span>}
        {activeCount > 0 && (
          <Pill tone="running">
            <span className="dot" />
            {activeCount} running
          </Pill>
        )}
        {app?.cli.ok ? (
          <span className="tag">{app.cli.version}</span>
        ) : (
          <Pill tone="bad">CLI unavailable</Pill>
        )}
        <button className="btn ghost" onClick={() => post({ type: "refresh" })} title="Refresh">
          <RefreshIcon size={13} />
          Refresh
        </button>
      </header>

      <Toasts />

      <div className="app-body">
        <nav aria-label="LeanFlow dashboard" className="nav">
          {TABS.map(({ id, label, Icon }) => (
            <button
              aria-current={view.tab === id ? "page" : undefined}
              key={id}
              className={view.tab === id ? "active" : ""}
              onClick={() => setView({ tab: id })}
              type="button"
            >
              <Icon size={15} className="icon" />
              <span className="label">{label}</span>
              {counts[id] ? <span className="count">{counts[id]}</span> : null}
            </button>
          ))}
        </nav>
        <main className="content" tabIndex={-1}>{body()}</main>
      </div>
    </div>
  );
}
