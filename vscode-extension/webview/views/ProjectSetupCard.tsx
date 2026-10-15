/** One project setup surface shared by the dashboard and narrow sidebar. */
import { useState } from "react";
import { EMPTY_PROJECT_SETUP, type ProjectSetupState } from "../../src/core/projectSetupTypes";
import { Card } from "../components";
import { useStore } from "../store";
import { post } from "../vscodeApi";

/** Display verified preparation separately from provider credentials and proof success. */
export function ProjectSetupCard({ sidebar = false, formalization }: { sidebar?: boolean; formalization?: { state: ProjectSetupState; error: string; preparationCurrent: boolean } }) {
  const { app, setView } = useStore();
  const [logVisible, setLogVisible] = useState(false);
  if (!app?.cli.ok) return null;
  const setup = formalization?.state ?? app.projectSetup ?? EMPTY_PROJECT_SETUP;
  const otherBusy = formalization && app.projectSetup?.status === "running" && app.projectSetup.root !== setup.root;
  const activeRun = setup.code === "setup_busy_other_root" ? otherBusy : setup.activeRun || otherBusy;
  const running = setup.status === "running";
  const ready = setup.status === "ready" || formalization?.preparationCurrent === true;
  const retry = setup.canRetry;
  const title = formalization ? "1. Project setup" : running ? "Setting up project" : ready ? "Ready to prove" : retry ? (setup.status === "cancelled" ? "Project setup cancelled" : "Project setup failed") : setup.status === "detected" || app.project.found ? "Existing Lean project" : "Project setup";
  const action = (value: "start" | "retry" | "repair" | "cancel" | "log" | "inspect") => {
    if (formalization) post({ type: "formalizationProjectSetup", action: value });
    else if (value !== "inspect") post({ type: ({ start: "setupProject", retry: "retryProjectSetup", repair: "repairProjectSetup", cancel: "cancelProjectSetup", log: "showProjectSetupLog" } as const)[value] });
  };
  return <Card title={title}>
    {setup.root && <div className="muted setup-root">{setup.root}</div>}
    {setup.phase && (running || retry) && <div className="setup-phase">Step: {setup.phase.replaceAll("_", " ")}</div>}
    <p className={retry ? "setup-error" : "muted"} role={retry ? "alert" : "status"}>
      {formalization?.preparationCurrent ? "Current formalization preparation already verifies this project's tools and dependencies." : formalization && ready ? "Lean, the installed dependencies and the REPL have passed the project tools check." : setup.message || (formalization ? "Set up Lean, Mathlib and the REPL before native dependency planning. Existing project configuration and source files are preserved." : app.project.found ? "This project is already configured. Check setup only if you need to verify its tools." : "Create a Lean project with Mathlib, or prepare an existing project. Your Lean files are preserved.")}
    </p>
    {formalization?.error && <p className="notice error" role="alert">{formalization.error}</p>}
    {setup.remediation && <p className="muted">{setup.remediation}</p>}
    {ready && <p className="muted">Project tools are ready. {formalization ? "Native planning can inspect this baseline and add missing dependencies in a checked private transaction. This is not translation or proof acceptance." : "Choose a provider and sign in before your first run."}</p>}
    {formalization && !ready && <p className="muted">Source inspection is free and remains available before setup. Setup does not call a model or prove paper statements.</p>}
    {Boolean(setup.sourceDiagnostics?.length) && <details className="setup-source-diagnostics">
      <summary>Source files need attention ({setup.sourceDiagnostics.length})</summary>
      <p className="muted">These source issues remain after preparing the project tools:</p>
      <ul>{setup.sourceDiagnostics.map((diagnostic, index) => <li key={index}>{diagnostic}</li>)}</ul>
    </details>}
    {activeRun && <p className="muted">{otherBusy ? "Project setup is running in another workspace. Wait for its owned process to finish before starting another setup." : "A workflow is running. Project setup will be available when it finishes."}</p>}
    <div className="row project-setup-actions">
      {running ? <button key="cancel" className="btn secondary" onClick={() => action("cancel")}>Cancel setup</button>
        : <button key="setup" className={ready ? "btn ghost" : "btn"} disabled={activeRun || !!formalization && !setup.root} onClick={() => action(retry ? "retry" : "start")}>{retry ? "Retry setup" : ready || !formalization && app.project.found ? "Check setup" : "Set up project"}</button>}
      {(running || setup.status !== "idle") && <button className="btn ghost" aria-expanded={logVisible} onClick={() => { setLogVisible(!logVisible); if (!logVisible) action("log"); }}>{logVisible ? "Hide setup log" : "Show setup log"}</button>}
      {formalization && <button className="btn ghost" onClick={() => action("inspect")}>Refresh tools status</button>}
      {ready && <button className="btn secondary" onClick={() => sidebar ? post({ type: "configureProvider" }) : setView({ tab: "providers" })}>Set up provider</button>}
    </div>
    {logVisible && <pre className="setup-live-log" aria-label="Project setup log">{setup.recentOutput?.join("\n") || "No output recorded yet."}</pre>}
    {!running && (formalization ? ready || retry : app.project.found) && <details className="setup-troubleshooting">
      <summary>Repair options</summary>
      <p className="muted">Recheck dependency downloads and rebuild verification tools if the installed environment is broken. This can take longer than a setup check.</p>
      <button className="btn secondary" disabled={activeRun} onClick={() => action("repair")}>Repair dependencies</button>
    </details>}
  </Card>;
}
