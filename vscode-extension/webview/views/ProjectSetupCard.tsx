/** One project setup surface shared by the dashboard and narrow sidebar. */
import { useState } from "react";
import { EMPTY_PROJECT_SETUP } from "../../src/core/projectSetupTypes";
import { Card } from "../components";
import { useStore } from "../store";
import { post } from "../vscodeApi";

/** Display verified preparation separately from provider credentials and proof success. */
export function ProjectSetupCard({ sidebar = false }: { sidebar?: boolean }) {
  const { app, setView } = useStore();
  const [logVisible, setLogVisible] = useState(false);
  if (!app?.cli.ok) return null;
  const setup = app.projectSetup ?? EMPTY_PROJECT_SETUP;
  const running = setup.status === "running";
  const ready = setup.status === "ready";
  const retry = setup.canRetry;
  const title = running ? "Setting up project" : ready ? "Ready to prove" : retry ? (setup.status === "cancelled" ? "Project setup cancelled" : "Project setup failed") : setup.status === "detected" || app.project.found ? "Existing Lean project" : "Project setup";
  return <Card title={title}>
    {setup.root && <div className="muted setup-root">{setup.root}</div>}
    {setup.phase && (running || retry) && <div className="setup-phase">Step: {setup.phase.replaceAll("_", " ")}</div>}
    <p className={retry ? "setup-error" : "muted"} role={retry ? "alert" : "status"}>
      {setup.message || (app.project.found ? "This project is already configured. Check setup only if you need to verify its tools." : "Create a Lean project with Mathlib, or prepare an existing project. Your Lean files are preserved.")}
    </p>
    {setup.remediation && <p className="muted">{setup.remediation}</p>}
    {ready && <p className="muted">Project tools are ready. Choose a provider and sign in before your first run.</p>}
    {Boolean(setup.sourceDiagnostics?.length) && <details className="setup-source-diagnostics">
      <summary>Source files need attention ({setup.sourceDiagnostics.length})</summary>
      <p className="muted">These source issues remain after preparing the project tools:</p>
      <ul>{setup.sourceDiagnostics.map((diagnostic, index) => <li key={index}>{diagnostic}</li>)}</ul>
    </details>}
    {setup.activeRun && <p className="muted">A workflow is running. Project setup will be available when it finishes.</p>}
    <div className="row project-setup-actions">
      {running ? <button key="cancel" className="btn secondary" onClick={() => post({ type: "cancelProjectSetup" })}>Cancel setup</button>
        : <button key="setup" className={ready ? "btn ghost" : "btn"} disabled={setup.activeRun} onClick={() => post({ type: retry ? "retryProjectSetup" : "setupProject" })}>{retry ? "Retry setup" : ready || app.project.found ? "Check setup" : "Set up project"}</button>}
      {(running || setup.status !== "idle") && <button className="btn ghost" aria-expanded={logVisible} onClick={() => { setLogVisible(!logVisible); if (!logVisible) post({ type: "showProjectSetupLog" }); }}>{logVisible ? "Hide setup log" : "Show setup log"}</button>}
      {ready && <button className="btn secondary" onClick={() => sidebar ? post({ type: "configureProvider" }) : setView({ tab: "providers" })}>Set up provider</button>}
    </div>
    {logVisible && <pre className="setup-live-log" aria-label="Project setup log">{setup.recentOutput?.join("\n") || "No output recorded yet."}</pre>}
    {!running && app.project.found && <details className="setup-troubleshooting">
      <summary>Repair options</summary>
      <p className="muted">Recheck dependency downloads and rebuild verification tools if the installed environment is broken. This can take longer than a setup check.</p>
      <button className="btn secondary" disabled={setup.activeRun} onClick={() => post({ type: "repairProjectSetup" })}>Repair dependencies</button>
    </details>}
  </Card>;
}
