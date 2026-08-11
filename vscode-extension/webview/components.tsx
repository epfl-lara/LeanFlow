/** Small presentational pieces shared across the views. */
import type { ReactNode } from "react";

import type { ExperimentCell, TrackedRunStatus } from "../src/core/types";
import { Mark } from "./icons";

export function Card(props: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="card">
      {(props.title || props.actions) && (
        <div className="spread">
          {props.title && <div className="card-title">{props.title}</div>}
          {props.actions && <div className="row tight">{props.actions}</div>}
        </div>
      )}
      {props.subtitle && <div className="card-subtitle">{props.subtitle}</div>}
      {props.children}
    </section>
  );
}

export function Field(props: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="field">
      <span className="label">{props.label}</span>
      {props.children}
      {props.hint && <span className="hint">{props.hint}</span>}
    </label>
  );
}

export function Check(props: {
  checked: boolean;
  label: string;
  hint?: string;
  disabled?: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="check">
      <input
        type="checkbox"
        checked={props.checked}
        disabled={props.disabled}
        onChange={(event) => props.onChange(event.target.checked)}
      />
      <span className="body">
        <span>{props.label}</span>
        {props.hint && <span className="hint">{props.hint}</span>}
      </span>
    </label>
  );
}

const STATUS_TONE: Record<TrackedRunStatus, string> = {
  starting: "running",
  running: "running",
  finished: "ok",
  failed: "bad",
  stopped: "warn",
};

export function StatusPill(props: { status: TrackedRunStatus; label?: string }) {
  return (
    <span className={`pill ${STATUS_TONE[props.status]}`}>
      <span aria-hidden="true" className="dot" />
      {props.label ?? props.status}
    </span>
  );
}

export function Pill(props: { tone?: "ok" | "bad" | "warn" | "running"; children: ReactNode }) {
  return <span className={`pill ${props.tone ?? ""}`}>{props.children}</span>;
}

export function Stat(props: { label: string; value: ReactNode; small?: boolean }) {
  return (
    <div className="stat">
      <div className="k">{props.label}</div>
      <div className={`v${props.small ? " small" : ""}`}>{props.value}</div>
    </div>
  );
}

export function Empty(props: { children: ReactNode }) {
  return (
    <div className="empty">
      <Mark size={30} className="mark" />
      <div>{props.children}</div>
    </div>
  );
}

/**
 * Segmented progress across a sweep's cells.
 *
 * Segments are proportional to counts rather than a single percentage, so a
 * sweep that is half done and half failed reads differently from one that is
 * simply half done — which is the distinction that matters mid-run.
 */
export function CellProgress(props: { cells: ExperimentCell[] }) {
  const total = props.cells.length || 1;
  const count = (status: ExperimentCell["status"]) =>
    props.cells.filter((cell) => cell.status === status).length;

  const done = count("done");
  const failed = count("failed");
  const unscored = count("unscored");
  const blocked = count("blocked");
  const running = count("running");
  const completed = done + failed + unscored + blocked;
  const pending = total - completed - running;
  const pct = (value: number) => `${(value / total) * 100}%`;

  return (
    <div className="progress-wrap">
      <div
        aria-label={`${completed} of ${total} experiment cells completed`}
        aria-valuemax={total}
        aria-valuemin={0}
        aria-valuenow={completed}
        className="progress"
        role="progressbar"
      >
        <div className="seg done" style={{ width: pct(done) }} />
        <div className="seg failed" style={{ width: pct(failed) }} />
        <div className="seg unscored" style={{ width: pct(unscored) }} />
        <div className="seg blocked" style={{ width: pct(blocked) }} />
        <div className="seg running" style={{ width: pct(running) }} />
      </div>
      <div className="progress-legend">
        <span>
          <span
            className="swatch"
            style={{ background: "var(--vscode-charts-green)" }}
          />
          {done} done
        </span>
        {failed > 0 && (
          <span>
            <span
              className="swatch"
              style={{ background: "var(--vscode-charts-red)" }}
            />
            {failed} failed
          </span>
        )}
        {unscored > 0 && <span>{unscored} unscored</span>}
        {blocked > 0 && <span>{blocked} blocked</span>}
        <span>{pending} pending</span>
      </div>
    </div>
  );
}

export function Notice(props: { tone: "info" | "warn" | "error"; children: ReactNode }) {
  return (
    <div className={`notice ${props.tone}`} role={props.tone === "error" ? "alert" : "status"}>
      {props.children}
    </div>
  );
}

/** Compact relative time, matching the tree view's phrasing. */
export function relativeTime(iso: string | null | undefined): string {
  if (!iso) {
    return "—";
  }
  const then = Date.parse(iso);
  if (Number.isNaN(then)) {
    return iso;
  }
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) {
    return `${seconds}s ago`;
  }
  if (seconds < 3600) {
    return `${Math.round(seconds / 60)}m ago`;
  }
  if (seconds < 86400) {
    return `${Math.round(seconds / 3600)}h ago`;
  }
  return `${Math.round(seconds / 86400)}d ago`;
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) {
    return "—";
  }
  if (seconds < 60) {
    return `${Math.round(seconds)}s`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m ${Math.round(seconds % 60)}s`;
  }
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "—";
  }
  return value.toLocaleString();
}
