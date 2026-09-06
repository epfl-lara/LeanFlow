/** The expanded body of one activity row: what the model said, asked, and got back. */
import { useEffect, useMemo } from "react";

import {
  eventFacts,
  eventOutputSections,
  evidenceDetails,
  evidenceStatus,
  rawDetailsForDisplay,
  type EventOutputSection,
} from "../../src/core/eventDetail";
import type { ActivityEvent } from "../../src/core/types";
import { Notice } from "../components";
import { eventDetailKey, useStore } from "../store";
import { post } from "../vscodeApi";
import { CodeBlock, RichText, ValueView, sectionLanguage } from "./RichText";

function copyText(value: string): void {
  // Clipboard access needs a user gesture, which the Copy button supplies. A
  // refusal (older webview host, restricted context) is not worth a toast.
  void navigator.clipboard?.writeText(value).catch(() => undefined);
}

function SectionBody({ section }: { section: EventOutputSection }) {
  if (section.kind === "fields") {
    return (
      <div className="section-fields">
        <ValueView value={section.value} />
      </div>
    );
  }
  const text = String(section.value);
  if (section.kind === "code") {
    return <CodeBlock text={text} language={sectionLanguage(text, section.language)} />;
  }
  return <RichText text={text} />;
}

export function EventDetailPanel(props: { runId: string; event: ActivityEvent }) {
  const { runId, event } = props;
  const { eventDetails, requestEventDetail, proverStates } = useStore();
  const entry = eventDetails[eventDetailKey(runId, event.event_id)];

  useEffect(() => {
    // Fetch once per expansion; the store keeps the answer for re-expansion.
    if (!entry) {
      requestEventDetail(runId, event.event_id);
    }
  }, [entry, runId, event.event_id, requestEventDetail]);

  const proverKnown = proverStates[runId] !== undefined;
  useEffect(() => {
    // The job list normally arrives through the Live tab. Ask for it here so
    // the transcript link works when Logs is the first tab opened; the host
    // coalesces concurrent requests for one run.
    if (!proverKnown) {
      post({ type: "loadProver", runId });
    }
  }, [proverKnown, runId]);

  const evidence = evidenceDetails(entry?.detail ?? null);
  const sections = useMemo(() => eventOutputSections(event, evidence), [event, evidence]);
  const facts = useMemo(() => eventFacts(event, evidence), [event, evidence]);
  const rawEvent = useMemo(
    () => JSON.stringify(rawDetailsForDisplay(event.details ?? {}), null, 2),
    [event],
  );
  const rawRecord = useMemo(
    () => (evidence ? JSON.stringify(evidence, null, 2) : ""),
    [evidence],
  );
  const status = evidenceStatus(
    event,
    entry?.detail ?? null,
    entry === undefined || entry.loading,
    entry?.error ?? "",
  );
  const job = proverStates[runId]?.snapshot?.jobs.find(
    (candidate) => candidate.agent_id === event.agent_id && candidate.log_path,
  );

  return (
    <div className="log-detail" role="region" aria-label={`Details for ${event.type}`}>
      <div className="log-detail-facts">
        <span className="tag">{event.timestamp}</span>
        {facts.map((fact) => (
          <span key={fact.label} className="pill">
            {fact.label}: {fact.value}
          </span>
        ))}
        <span className="spacer" />
        {job && (
          <button
            className="btn ghost"
            onClick={() => post({ type: "openProverFile", runId, path: job.log_path })}
            title="Open this agent's complete transcript log in the editor"
            type="button"
          >
            Open job log
          </button>
        )}
        {entry?.error && (
          <button
            className="btn ghost"
            onClick={() => requestEventDetail(runId, event.event_id)}
            type="button"
          >
            Retry
          </button>
        )}
      </div>

      {status && <Notice tone={status.tone}>{status.text}</Notice>}

      {sections.length === 0 && !status && (
        <div className="muted">This event carries no model output. Its fields are below.</div>
      )}

      {sections.map((section) => (
        <div className={`log-detail-section ${section.tone ?? ""}`} key={section.id}>
          <div className="label">
            <span>{section.title}</span>
            {section.subtitle && <span className="subtitle">{section.subtitle}</span>}
            <span className="spacer" />
            {section.text && (
              <button className="link" onClick={() => copyText(section.text)} type="button">
                Copy
              </button>
            )}
          </div>
          <SectionBody section={section} />
        </div>
      ))}

      <div className="log-detail-raw-group">
        <details className="log-detail-raw">
          <summary>Event fields (JSON)</summary>
          <CodeBlock text={rawEvent} language="json" />
        </details>
        {rawRecord && (
          <details className="log-detail-raw">
            <summary>Full record (JSON)</summary>
            <CodeBlock text={rawRecord} language="json" />
          </details>
        )}
      </div>
    </div>
  );
}
