/**
 * Structured log viewer over the run's activity stream.
 *
 * The runtime emits ~50 distinct event types at very different densities, so
 * the filter is the feature: reading a campaign means choosing which of those
 * layers to look at, not scrolling everything. Rows are grouped into the agent
 * sessions they belong to, each row stays one scannable line, and expanding one
 * fetches the model output behind it.
 */
import { Fragment, useEffect, useMemo, useRef, useState } from "react";

import { eventSearchText } from "../../src/core/eventDetail";
import {
  humanEventLabel,
  indexSessions,
  segmentFacts,
  segmentLogRows,
  shouldFrameSegment,
  statusTone,
  turnNumber,
} from "../../src/core/logRows";
import type { ActivityEvent } from "../../src/core/types";
import { Card, Empty, Notice } from "../components";
import { useSelectedRun, useStore } from "../store";
import { post } from "../vscodeApi";
import { EventDetailPanel } from "./EventDetail";

/** Events that tell the story of a run, across the legacy runner and the bounded prover. */
const NARRATIVE_TYPES = new Set([
  "assistant-response",
  "api-response",
  "api-error",
  "runner-start",
  "runner-exit",
  "conversation-start",
  "conversation-end",
  "job-session-start",
  "job-session-end",
  "job_finished",
  "submission-feedback",
  "submission_checked",
  "candidate_checked",
  "negation_checked",
  "plan_rejected",
  "plan_refinement_budget_exhausted",
  "libraries_installed",
  "context-compacted",
  "user_message",
  "user-guidance-received",
]);

/** Curated groupings so the filter is usable without knowing all 50 types. */
const PRESETS: { id: string; label: string; match: (type: string) => boolean }[] = [
  {
    // The events worth noticing during a multi-hour run: something was rejected,
    // rolled back, blocked, retried, or has stopped making progress.
    id: "attention",
    label: "Attention",
    match: (type) =>
      /reject|fail|error|rollback|revert|restored|conflict|retry|exhaust|timeout|stall|stuck|no-progress|breakpoint|blocked|obstruction|checkpoint|give-up|parked/i.test(
        type,
      ),
  },
  { id: "all", label: "Everything", match: () => true },
  {
    id: "narrative",
    label: "Narrative",
    match: (type) =>
      NARRATIVE_TYPES.has(type) ||
      type.startsWith("queue-") ||
      type.startsWith("orchestrator") ||
      type.startsWith("research-portfolio"),
  },
  {
    id: "tools",
    label: "Tool calls",
    match: (type) => type.startsWith("tool-"),
  },
  {
    id: "lean",
    label: "Lean",
    match: (type) => type.startsWith("lean-") || type.includes("verification"),
  },
  {
    id: "provider",
    label: "Provider",
    match: (type) => type.startsWith("api-") || type.startsWith("provider-"),
  },
  {
    id: "research",
    label: "Research",
    match: (type) =>
      type.startsWith("research-") || type.startsWith("dispatch") || type.startsWith("premise"),
  },
];

function toneFor(type: string): string {
  if (type.includes("fail") || type.includes("error") || type.includes("rejected")) {
    return "err";
  }
  if (type.startsWith("tool-")) {
    return "tool";
  }
  if (type.startsWith("lean-") || type.includes("verification")) {
    return "lean";
  }
  return "";
}

/** 24-hour local time: compact, and unambiguous next to a duration. */
function shortTime(iso: string): string {
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime())
    ? iso.slice(11, 19)
    : parsed.toLocaleTimeString(undefined, { hour12: false });
}

export function LogsView() {
  const { app, view, setView, events, runLog } = useStore();
  const run = useSelectedRun();
  const [preset, setPreset] = useState("narrative");
  const [follow, setFollow] = useState(true);
  const [expanded, setExpanded] = useState<Record<string, true>>({});
  const [collapsedAgents, setCollapsedAgents] = useState<Record<string, true>>({});
  const listRef = useRef<HTMLDivElement>(null);

  // Prefer the selected run's own stream; fall back to the newest recorded one
  // so a run started from a terminal is still readable here.
  const runId = run?.runId || app?.history[0]?.run_id || "";
  const stream: ActivityEvent[] = useMemo(() => events[runId] ?? [], [events, runId]);
  const agents = useMemo(
    () => [...new Set(stream.map((event) => event.agent_id).filter(Boolean))].sort(),
    [stream],
  );
  // Session facts come from the whole stream, so a segment header still knows
  // the job's role and outcome when the active filter hides those rows.
  const sessions = useMemo(() => indexSessions(stream), [stream]);

  useEffect(() => {
    if (runId) {
      post({ type: "loadEvents", runId });
    }
    // Expansion state is per stream; another run's ids would never match anyway.
    setExpanded({});
    setCollapsedAgents({});
  }, [runId]);

  useEffect(() => {
    if (view.logRaw) {
      post({ type: "loadRunLog", runId });
    }
  }, [view.logRaw, runId]);

  const activePreset = PRESETS.find((entry) => entry.id === preset) ?? PRESETS[0];
  const search = view.logSearch.trim().toLowerCase();

  const filtered = useMemo(() => {
    return stream.filter((event) => {
      if (view.logAgent && event.agent_id !== view.logAgent) return false;
      if (view.logFilter.length > 0) {
        if (!view.logFilter.includes(event.type)) {
          return false;
        }
      } else if (!activePreset.match(event.type)) {
        return false;
      }
      if (!search) {
        return true;
      }
      return eventSearchText(event).includes(search);
    });
  }, [stream, view.logFilter, view.logAgent, activePreset, search]);

  const segments = useMemo(() => segmentLogRows(filtered, sessions), [filtered, sessions]);

  useEffect(() => {
    if (follow && listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [filtered.length, follow]);

  const toggleRow = (eventId: string) => {
    setExpanded((previous) => {
      const next = { ...previous };
      if (next[eventId]) {
        delete next[eventId];
      } else {
        next[eventId] = true;
      }
      return next;
    });
  };
  const toggleAgent = (agentId: string) => {
    setCollapsedAgents((previous) => {
      const next = { ...previous };
      if (next[agentId]) {
        delete next[agentId];
      } else {
        next[agentId] = true;
      }
      return next;
    });
  };
  const expandedCount = Object.keys(expanded).length;

  if (!app?.project.found) {
    return <Empty>Open a LeanFlow project to read run logs.</Empty>;
  }

  const knownTypes = app.eventTypes ?? [];

  return (
    <>
      <Card
        title="Logs"
        subtitle={
          runId
            ? `Reading ${runId} — ${stream.length} events buffered, ${filtered.length} shown. Select an event to see the model output behind it.`
            : "No recorded run yet."
        }
        actions={
          <button
            className={`btn ${view.logRaw ? "" : "ghost"}`}
            onClick={() => setView({ logRaw: !view.logRaw })}
          >
            {view.logRaw ? "Structured" : "Raw log"}
          </button>
        }
      >
        {!view.logRaw && (
          <div className="row tight">
            <select
              aria-label="Agent log"
              value={view.logAgent || ""}
              style={{ maxWidth: 240 }}
              onChange={(event) => setView({ logAgent: event.target.value })}
            >
              <option value="">All agents</option>
              {view.logAgent && !agents.includes(view.logAgent) && (
                <option value={view.logAgent}>{view.logAgent}</option>
              )}
              {agents.map((agent) => (
                <option key={agent} value={agent}>
                  {agent}
                </option>
              ))}
            </select>
            {PRESETS.map((entry) => (
              <button
                key={entry.id}
                className={`btn ${preset === entry.id && view.logFilter.length === 0 ? "" : "ghost"}`}
                onClick={() => {
                  setPreset(entry.id);
                  setView({ logFilter: [] });
                }}
              >
                {entry.label}
              </button>
            ))}
            <input
              type="text"
              placeholder="Filter text…"
              style={{ maxWidth: 220 }}
              value={view.logSearch}
              onChange={(event) => setView({ logSearch: event.target.value })}
            />
            <label className="check" style={{ margin: 0 }}>
              <input
                type="checkbox"
                checked={follow}
                onChange={(event) => setFollow(event.target.checked)}
              />
              <span className="body">
                <span>Follow</span>
              </span>
            </label>
            {expandedCount > 0 && (
              <button className="btn ghost" onClick={() => setExpanded({})} type="button">
                Collapse all ({expandedCount})
              </button>
            )}
          </div>
        )}
      </Card>

      {!view.logRaw && knownTypes.length > 0 && (
        <details className="card log-types" open={view.logFilter.length > 0}>
          <summary>
            <span className="card-title">Event types</span>
            <span className="muted">
              {view.logFilter.length > 0
                ? `${view.logFilter.length} selected — overrides the preset`
                : "Select specific types to override the preset"}
            </span>
          </summary>
          <div className="row tight" style={{ marginTop: 10 }}>
            {view.logFilter.length > 0 && (
              <button className="btn ghost" onClick={() => setView({ logFilter: [] })}>
                Clear ({view.logFilter.length})
              </button>
            )}
            {knownTypes.slice(0, 60).map((entry) => {
              const selected = view.logFilter.includes(entry.type);
              return (
                <button
                  key={entry.type}
                  className={`pill ${selected ? "ok" : ""}`}
                  style={{ cursor: "pointer", background: "none" }}
                  title={entry.type}
                  onClick={() =>
                    setView({
                      logFilter: selected
                        ? view.logFilter.filter((type) => type !== entry.type)
                        : [...view.logFilter, entry.type],
                    })
                  }
                >
                  {humanEventLabel(entry.type)} <span className="muted">{entry.count}</span>
                </button>
              );
            })}
          </div>
        </details>
      )}

      {view.logRaw ? (
        <pre className="raw">{runLog || "[no workflow run log recorded yet]"}</pre>
      ) : (
        <div className="logs">
          <div className="log-list" ref={listRef}>
            {filtered.length === 0 ? (
              <div className="empty">
                No events match this filter.
                {stream.length > 0 && <> {stream.length} events are buffered.</>}
              </div>
            ) : (
              segments.map((segment) => {
                const framed = shouldFrameSegment(segment);
                const collapsed = framed && Boolean(collapsedAgents[segment.agentId]);
                const facts = segmentFacts(segment).join(" · ");
                const status = segment.session?.status ?? "";
                return (
                  <div className="log-segment" key={segment.key}>
                    {framed && (
                      <button
                        aria-expanded={!collapsed}
                        className={`log-segment-head${collapsed ? " collapsed" : ""}`}
                        onClick={() => toggleAgent(segment.agentId)}
                        type="button"
                      >
                        <span aria-hidden="true" className="caret">
                          ▾
                        </span>
                        <strong>{segment.agentId}</strong>
                        {facts && (
                          <span className="muted facts" title={facts}>
                            {facts}
                          </span>
                        )}
                        <span className="spacer" />
                        <span className="muted count">
                          {segment.events.length} {segment.events.length === 1 ? "event" : "events"}
                        </span>
                        {status && <span className={`pill ${statusTone(status)}`}>{status}</span>}
                      </button>
                    )}
                    {!collapsed &&
                      segment.events.map((event) => {
                        const open = Boolean(expanded[event.event_id]);
                        const turn = turnNumber(event);
                        return (
                          <Fragment key={event.event_id}>
                            <div
                              aria-expanded={open}
                              className={`log-line ${toneFor(event.type)}${open ? " expanded" : ""}`}
                              onClick={() => toggleRow(event.event_id)}
                              onKeyDown={(keyboard) => {
                                if (keyboard.key === "Enter" || keyboard.key === " ") {
                                  keyboard.preventDefault();
                                  toggleRow(event.event_id);
                                }
                              }}
                              role="button"
                              tabIndex={0}
                              title={`${event.type} · ${event.message}`}
                            >
                              <span aria-hidden="true" className="caret">
                                ▶
                              </span>
                              <span className="ts">{shortTime(event.timestamp)}</span>
                              <span className="type">{humanEventLabel(event.type)}</span>
                              <span className="msg">
                                {turn !== null && <span className="turn">#{turn}</span>}
                                {!framed && event.agent_id && (
                                  <span className="tag">{event.agent_id}</span>
                                )}
                                <span className="text">{event.message}</span>
                              </span>
                            </div>
                            {open && <EventDetailPanel runId={runId} event={event} />}
                          </Fragment>
                        );
                      })}
                  </div>
                );
              })
            )}
          </div>
        </div>
      )}

      {!view.logRaw && stream.length === 0 && runId && (
        <Notice tone="info">
          Buffered events are read incrementally while a run is active. A run that finished
          before this window opened shows its events after the next refresh.
        </Notice>
      )}
    </>
  );
}
