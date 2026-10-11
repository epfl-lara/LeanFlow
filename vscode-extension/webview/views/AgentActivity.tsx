/** Show each job's emitted messages and tool activity without loading all transcripts. */
import { useMemo, useState } from "react";
import { agentActivity } from "../../src/core/agentActivity";
import { humanEventLabel } from "../../src/core/logRows";
import { useStore } from "../store";
import { EventDetailPanel } from "./EventDetail";
import { clock } from "./proverFormat";

export function AgentActivity({ runId, jobId }: { runId: string; jobId: string }) {
  const { events, setView } = useStore();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [showReasoning, setShowReasoning] = useState(true);
  const activity = useMemo(() => agentActivity(events[runId] || [], runId, jobId), [events, runId, jobId]);
  return <div className="agent-activity">
    {activity.latest && <p className="agent-latest">{activity.latest.slice(0, 420)}</p>}
    <details>
      <summary>Output and tool activity ({activity.total})</summary>
      <p className="muted">Backend observations appear as they are emitted. Independent Lean checks determine acceptance. Reasoning is shown only when the backend exposes it.</p>
      {!activity.total && <p className="muted">No output is in the event buffer yet. A quiet agent may still be working; older runs may contain only final reports.</p>}
      {!activity.hasReasoning && activity.total > 0 && <p className="muted">No reasoning has been reported in the loaded events.</p>}
      {activity.hasReasoning && <label className="row tight"><input type="checkbox" checked={showReasoning} onChange={event => setShowReasoning(event.target.checked)} />Show reported reasoning</label>}
      <div className="agent-activity-feed">
        {activity.rows.filter(event => showReasoning || event.type !== "agent-reasoning").map(event => <div key={event.event_id} className={`agent-activity-entry ${event.type === "agent-reasoning" ? "reasoning" : ""}`}>
          <button className="agent-activity-toggle" aria-expanded={expanded === event.event_id} onClick={() => setExpanded(expanded === event.event_id ? null : event.event_id)}>
            <span className="muted">{clock(event.timestamp)}</span><strong>{humanEventLabel(event.type)}</strong><span className="agent-activity-preview">{event.message}</span>
          </button>
          {expanded === event.event_id && <EventDetailPanel runId={runId} event={event} />}
        </div>)}
      </div>
      <div className="row tight"><span className="muted">Latest {activity.rows.length} events from the bounded run buffer.</span><button className="link" onClick={() => setView({ tab: "logs", logAgent: jobId, logRaw: false, logFilter: [], logSearch: "" })}>Open agent log</button></div>
    </details>
  </div>;
}
