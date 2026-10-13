/** Show each job's emitted messages and tool activity without loading all transcripts. */
import { useEffect, useMemo, useRef, useState } from "react";
import { agentActivity } from "../../src/core/agentActivity";
import { activityEventKey, activityEventLabel, activityEventPreview, activityEventState } from "../../src/core/logRows";
import { useStore } from "../store";
import { EventDetailPanel } from "./EventDetail";
import { clock, seconds } from "./proverFormat";
import { HELP } from "../help";

export function AgentActivity({ runId, jobId, agentId, running = false, now = Date.now() }: {
  runId: string; jobId: string; agentId: string; running?: boolean; now?: number;
}) {
  const { events, setView } = useStore();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [showReasoning, setShowReasoning] = useState(true);
  const [follow, setFollow] = useState(true);
  const [feedOpen, setFeedOpen] = useState(running);
  const feedRef = useRef<HTMLDivElement>(null);
  const activity = useMemo(() => agentActivity(events[runId] || [], runId, jobId), [events, runId, jobId]);
  const latest = activity.latestEvent;
  const lastId = latest?.event_id ?? "";
  useEffect(() => {
    setExpanded(null);
    setFeedOpen(running);
  }, [runId, jobId]);
  useEffect(() => {
    if (follow && feedOpen && feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight;
  }, [lastId, follow, feedOpen]);
  return <div className="agent-activity">
    {running && <div className="agent-current" role="status">
      <span className="agent-live-dot" aria-hidden="true" />
      {activity.runningTools.length > 0 ? activity.runningTools.map(tool => {
        const age = (now - Date.parse(tool.timestamp)) / 1000;
        return <span key={activityEventKey(tool)}><strong>{activityEventLabel(tool)}</strong> is running{Number.isFinite(age) && age >= 0 && <> · {seconds(age)}</>}</span>;
      }) : <span>{latest?.details?.partial === true ? latest.type === "agent-reasoning" ? "Receiving reported reasoning" : "Receiving agent output" : "Agent is working"}</span>}
    </div>}
    {latest && (showReasoning || latest.type !== "agent-reasoning") && <div className="agent-latest">
      <div className="agent-latest-heading"><strong>{activityEventLabel(latest)}</strong><span className="muted">{clock(latest.timestamp)}</span></div>
      <p>{activityEventPreview(latest).slice(0, 420)}</p>
    </div>}
    <details open={feedOpen} onToggle={event => setFeedOpen(event.currentTarget.open)}>
      <summary>Output and tool activity ({activity.total})</summary>
      <p className="muted">{HELP.activity} Independent Lean checks determine acceptance.</p>
      {!activity.total && <p className="muted">No output is in the event buffer yet. A quiet agent may still be working; older runs may contain only final reports.</p>}
      {!activity.hasReasoning && activity.total > 0 && <p className="muted">No reasoning was shared in these events. Providers may share a summary, a trace, or neither.</p>}
      <div className="row tight">
        {activity.hasReasoning && <label className="check"><input type="checkbox" checked={showReasoning} onChange={event => setShowReasoning(event.target.checked)} />Show reported reasoning</label>}
        <label className="check"><input type="checkbox" checked={follow} onChange={event => setFollow(event.target.checked)} />Follow live</label>
        {expanded && <button className="link" onClick={() => setExpanded(null)}>Collapse detail</button>}
      </div>
      <div className="agent-activity-feed" ref={feedRef}>
        {activity.rows.filter(event => showReasoning || event.type !== "agent-reasoning").map(event => {
          const key = activityEventKey(event);
          const state = activityEventState(event);
          return <div key={key} className={`agent-activity-entry ${event.type === "agent-reasoning" ? "reasoning" : ""}`}>
            <button className="agent-activity-toggle" aria-expanded={expanded === key} onClick={() => setExpanded(expanded === key ? null : key)}>
              <span className="muted">{clock(event.timestamp)}</span><strong>{activityEventLabel(event)}</strong>{state && <span className={`agent-event-state ${state.toLowerCase()}`}>{state}</span>}<span className="agent-activity-preview">{activityEventPreview(event)}</span>
            </button>
            {expanded === key && <EventDetailPanel runId={runId} event={event} />}
          </div>;
        })}
      </div>
      <div className="row tight"><span className="muted">Latest {activity.rows.length} activities; live text updates in place.</span><button className="link" onClick={() => setView({ tab: "logs", logAgent: agentId, logRaw: false, logFilter: [], logSearch: "" })}>Open agent log</button></div>
    </details>
  </div>;
}
