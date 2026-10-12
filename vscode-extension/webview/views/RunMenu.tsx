/** Shared job selection for live details and archived logs. */
import { appRunChoices, selectedRunChoice } from "../../src/core/runCatalog";
import { InfoHelp } from "../InfoHelp";
import { Notice, relativeTime } from "../components";
import { useStore } from "../store";
import { post } from "../vscodeApi";

export function RunMenu() {
  const { app, view, setView } = useStore();
  if (!app) return null;
  const choices = appRunChoices(app);
  const selected = selectedRunChoice(choices, app.selectedRunId);
  if (!choices.length) return app.runDiscoveryError ? <Notice tone="warn">{app.runDiscoveryError}</Notice> : null;
  return <section className="card run-menu" aria-label="Job navigation">
    {app.runDiscoveryError && <Notice tone="warn">{app.runDiscoveryError}</Notice>}
    <div className="row tight">
      <strong>Jobs</strong>
      <InfoHelp label="Job history">Live jobs have a verified process or an active terminal in this editor. Past jobs keep their saved logs and proof state. A status marked unknown has no confirmed completion record. Reopening the project discovers jobs started from other windows or terminals.</InfoHelp>
      <span className="spacer" />
      <span className="muted">{choices.filter(row => row.active).length} live · {choices.filter(row => !row.active).length} past</span>
    </div>
    <select aria-label="Selected job" value={selected?.id || ""} onChange={event => {
      post({ type: "selectRun", id: event.target.value });
      setView({ logAgent: "", logSearch: "", logFilter: [] });
    }}>
      {!selected && <option value="">Choose a job</option>}
      {[true, false].map(active => <optgroup key={String(active)} label={active ? "Live jobs" : "Past jobs"}>
        {choices.filter(row => row.active === active).map(row => <option key={row.id} value={row.id}>
          {row.label} · {row.status} · {relativeTime(row.updatedAt)}
        </option>)}
      </optgroup>)}
    </select>
    <div className="row tight">
      <button className={`btn ${view.tab === "live" ? "secondary" : "ghost"}`} onClick={() => setView({ tab: "live" })}>Job details</button>
      <button className={`btn ${view.tab === "logs" ? "secondary" : "ghost"}`} onClick={() => setView({ tab: "logs" })}>Job logs</button>
      {selected?.active && selected.tracked?.connection && <button className="btn ghost" onClick={() => post({ type: "openRunTerminal", id: selected.id })}>Open terminal</button>}
    </div>
    {selected?.active && selected.tracked?.launchNotice && <div className="muted">{selected.tracked.launchNotice}</div>}
    {selected?.tracked?.error && <Notice tone="error">{selected.tracked.error}</Notice>}
  </section>;
}
