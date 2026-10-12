/** Stream one owned setup process without imposing a toolchain download timeout. */
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import type { ProjectSetupEvent, ProjectSetupResult, ProjectSetupTarget } from "./projectSetupTypes";

/** Accept the small public setup protocol and discard arbitrary CLI fields. */
export function parseSetupEvent(line: string): ProjectSetupEvent | null {
  let value: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(line);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    value = parsed as Record<string, unknown>;
  } catch { return null; }
  const text = (key: string) => typeof value[key] === "string" ? (value[key] as string).slice(0, 32_768) : "";
  if (value.type === "progress" || value.type === "output") {
    return { type: value.type, phase: text("phase"), message: text("message") };
  }
  if (value.type !== "result" || !["ready", "failed", "cancelled"].includes(String(value.status))) return null;
  return {
    type: "result", status: value.status as ProjectSetupResult["status"],
    phase: text("phase"), code: text("code"), message: text("message"),
    remediation: text("remediation"), log_path: text("log_path"), root: text("root"),
    changed_files: Array.isArray(value.changed_files) ? value.changed_files.filter((entry): entry is string => typeof entry === "string") : [],
    capabilities: value.capabilities && typeof value.capabilities === "object" && !Array.isArray(value.capabilities) ? value.capabilities as Record<string, unknown> : {},
  };
}

export interface SetupProcess {
  result: Promise<ProjectSetupResult>;
  cancel(): void;
}

/** Launch the native setup CLI and cancel only its owned process group. */
export function startProjectSetup(
  executable: string,
  target: ProjectSetupTarget,
  onEvent: (event: ProjectSetupEvent) => void,
): SetupProcess {
  const args = ["project", "setup", target.root, "--preset", "mathlib", "--json"];
  if (target.sourceFile) args.push("--source-file", target.sourceFile);
  const child = spawn(executable, args, {
    stdio: ["ignore", "pipe", "pipe"], detached: process.platform !== "win32",
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
  });
  let cancelled = false;
  let settled = false;
  let escalation: NodeJS.Timeout | undefined;
  let lastPhase = "checking_tools";
  let final: ProjectSetupResult | null = null;
  const failure = (message: string, code: string): ProjectSetupResult => ({
    type: "result", status: cancelled ? "cancelled" : "failed", phase: lastPhase,
    code: cancelled ? "cancelled" : code, message: cancelled ? "Project setup cancelled." : message,
    remediation: cancelled ? "Retry to continue project setup." : "Review the setup log, fix the reported problem, and retry.",
    log_path: "", root: target.root, changed_files: [], capabilities: {},
  });
  const signal = (kind: NodeJS.Signals) => {
    if (settled || !child.pid) return;
    try {
      if (process.platform === "win32") child.kill(kind);
      else process.kill(-child.pid, kind);
    } catch { /* The owned process may have exited between the check and signal. */ }
  };
  const result = new Promise<ProjectSetupResult>((resolve) => {
    const finish = (value: ProjectSetupResult) => {
      if (settled) return;
      settled = true;
      if (escalation) clearTimeout(escalation);
      resolve(value);
    };
    const read = createInterface({ input: child.stdout });
    read.on("line", (line: string) => {
      const event = parseSetupEvent(line);
      if (!event) { onEvent({ type: "output", phase: lastPhase, message: line }); return; }
      if (event.phase) lastPhase = event.phase;
      if (event.type === "result") final = event;
      else onEvent(event);
    });
    const errors = createInterface({ input: child.stderr });
    errors.on("line", (line: string) => onEvent({ type: "output", phase: lastPhase, message: line }));
    child.once("error", (error: NodeJS.ErrnoException) => {
      const missing = error.code === "ENOENT";
      finish({ ...failure(missing ? "LeanFlow CLI was not found. Install LeanFlow or configure its CLI path." : error.message, missing ? "cli_missing" : "spawn_failed"), remediation: missing ? "Open the install guide or configure LeanFlow: CLI Path, then retry." : "Check executable permissions and retry." });
    });
    child.once("close", (code) => {
      if (cancelled) finish(final?.status === "cancelled" ? final : failure("", "cancelled"));
      else if (final && (code === 0 || final.status !== "ready")) finish(final);
      else finish(failure(final ? `Setup exited with code ${code} after reporting readiness.` : `Setup exited ${code === null ? "without an exit code" : `with code ${code}`} without a readiness result.`, "setup_protocol_error"));
    });
  });
  return {
    result,
    cancel() {
      if (cancelled || settled) return;
      cancelled = true;
      signal("SIGINT");
      // This is cancellation escalation, never a deadline for a healthy setup.
      escalation = setTimeout(() => signal("SIGKILL"), 5_000);
      escalation.unref();
    },
  };
}
