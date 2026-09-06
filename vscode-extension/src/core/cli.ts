/**
 * Thin wrapper over the `leanflow` executable.
 *
 * Everything the extension learns about LeanFlow comes through the CLI's JSON
 * commands rather than by reading the state directory directly, so the on-disk
 * layout stays free to change without breaking the editor.
 */
import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";
import * as vscode from "vscode";

import { normalizeEventDetail } from "./eventDetail";
import type { LaunchEnv } from "./launch";
import { normalizeRunHistoryPayload } from "./runHistory";
import { normalizeProverSnapshot, type ProverSnapshot } from "./prover";
import {
  eventDetailForDisplay,
  launchPlanForDisplay,
  liveStatusForDisplay,
  proverStateForDisplay,
  redactSensitiveText,
  runLogForDisplay,
  runSummaryForDisplay,
} from "./runPrivacy";
import type {
  ActivityEventDetail,
  CliStatus,
  FlagCatalog,
  LaunchPlanPreview,
  LiveStatus,
  ProfileCatalog,
  ProfileDiffRow,
  RunMetrics,
  RunHistorySnapshot,
  RunSummary,
  ActivityEvent,
} from "./types";

const execFileAsync = promisify(execFile);

/** Commands that read state should never hang the UI. */
const DEFAULT_TIMEOUT_MS = 20_000;

export class CliError extends Error {
  constructor(
    message: string,
    readonly stderr: string = "",
    readonly exitCode: number | null = null,
    readonly stdout: string = "",
  ) {
    super(message);
    this.name = "CliError";
  }
}

export function cliPath(): string {
  const configured = vscode.workspace.getConfiguration("leanflow").get<string>("cliPath", "");
  return configured.trim() || "leanflow";
}

/**
 * Run a leanflow subcommand and return its stdout.
 *
 * `cwd` matters: several subcommands discover the project from the working
 * directory, so callers pass the project root rather than relying on whatever
 * directory the editor happened to start in.
 */
export async function runCli(
  args: string[],
  options: { cwd?: string; timeoutMs?: number } = {},
): Promise<string> {
  const bin = cliPath();
  try {
    const { stdout } = await execFileAsync(bin, args, {
      cwd: options.cwd,
      timeout: options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
      maxBuffer: 64 * 1024 * 1024,
      env: { ...process.env },
    });
    return stdout;
  } catch (error) {
    const err = error as NodeJS.ErrnoException & {
      stderr?: string;
      stdout?: string;
      code?: number | string;
    };
    if (err.code === "ENOENT") {
      throw new CliError(
        `LeanFlow CLI was not found at "${bin}". Install LeanFlow, then reload this ` +
          `window, or set LeanFlow: CLI Path to the executable in Settings.`,
      );
    }
    const stderr = (err.stderr ?? "").toString().trim();
    const stdout = (err.stdout ?? "").toString().trim();
    let structuredReason = "";
    if (stdout.startsWith("{")) {
      try {
        const payload = JSON.parse(stdout) as { reason?: unknown; error?: unknown };
        structuredReason = String(payload.reason ?? payload.error ?? "").trim();
      } catch {
        // The caller still receives the bounded stdout field for diagnostics.
      }
    }
    throw new CliError(
      stderr || structuredReason || err.message || `leanflow ${args.join(" ")} failed`,
      stderr,
      typeof err.code === "number" ? err.code : null,
      stdout.slice(0, 64 * 1024),
    );
  }
}

async function runCliJson<T>(args: string[], options: { cwd?: string; timeoutMs?: number } = {}) {
  const stdout = await runCli(args, options);
  try {
    return JSON.parse(stdout) as T;
  } catch {
    throw new CliError(
      `leanflow ${args.join(" ")} did not return JSON.`,
      stdout.slice(0, 2000),
    );
  }
}

export async function checkCli(): Promise<CliStatus> {
  const path = cliPath();
  try {
    const version = (await runCli(["version"], { timeoutMs: 10_000 })).trim();
    return { ok: true, path, version, error: "" };
  } catch (error) {
    return {
      ok: false,
      path,
      version: "",
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

export function fetchFlagCatalog(cwd?: string): Promise<FlagCatalog> {
  return runCliJson<FlagCatalog>(["flags", "list", "--json"], { cwd });
}

/** Read the selected run's plan and DAG without falling back to another run. */
export async function fetchProver(projectRoot: string, runId: string): Promise<ProverSnapshot | null> {
  if (!runId) return null;
  const payload = await runCliJson<{ prover: unknown }>(
    ["runs", "--project", projectRoot, "prover", runId, "--json"], { cwd: projectRoot },
  );
  // Apply the same credential redaction as status before crossing into the webview.
  const safe = proverStateForDisplay(payload.prover as LiveStatus);
  return normalizeProverSnapshot(safe, runId);
}

/** Queue explicit user guidance for the runtime's next decision boundary. */
export async function sendProverMessage(projectRoot: string, runId: string, agentId: string, message: string): Promise<void> {
  const result = await runCliJson<{ success: boolean; error?: string }>(
    ["runs", "--project", projectRoot, "prover-message", runId, "--agent", agentId, `--message=${message}`, "--json"],
    { cwd: projectRoot },
  );
  if (!result.success) throw new CliError(result.error || "The prover did not accept this message.");
}

export function fetchProfiles(cwd?: string): Promise<ProfileCatalog> {
  return runCliJson<ProfileCatalog>(["flags", "profiles", "--json"], { cwd });
}

export async function fetchProfileDiff(
  left: string,
  right: string,
  cwd?: string,
): Promise<ProfileDiffRow[]> {
  const payload = await runCliJson<{ diff: ProfileDiffRow[] }>(
    ["flags", "diff", left, right, "--json"],
    { cwd },
  );
  return payload.diff ?? [];
}

export async function fetchRuns(projectRoot: string, limit = 100): Promise<RunSummary[]> {
  return (await fetchRunHistory(projectRoot, limit)).runs;
}

/** Read retained run history with the CLI's archive-completeness verdict intact. */
export async function fetchRunHistory(
  projectRoot: string,
  limit = 100,
): Promise<RunHistorySnapshot> {
  const payload = await runCliJson<{
    version: number;
    complete?: boolean;
    completeness_issues?: unknown;
    archive_audit?: RunHistorySnapshot["archiveAudit"];
    limit?: number;
    total_count?: number;
    truncated?: boolean;
    count?: number;
    runs?: RunSummary[];
  }>(
    ["runs", "--project", projectRoot, "list", "--limit", String(limit)],
    { cwd: projectRoot },
  );
  const normalized = normalizeRunHistoryPayload(payload);
  return {
    ...normalized,
    runs: normalized.runs.map(runSummaryForDisplay),
  };
}

export async function fetchLiveStatus(projectRoot: string): Promise<LiveStatus | null> {
  const payload = await runCliJson<{ status: LiveStatus }>(
    ["runs", "--project", projectRoot, "status"],
    { cwd: projectRoot },
  );
  const status = payload.status ?? {};
  return Object.keys(status).length > 0 ? liveStatusForDisplay(status) : null;
}

export async function fetchEvents(
  projectRoot: string,
  runId: string,
  since: string,
  limit = 500,
): Promise<{ events: ActivityEvent[]; cursor: string; runId: string }> {
  const args = ["runs", "--project", projectRoot, "events", "--limit", String(limit)];
  if (runId) {
    args.splice(4, 0, runId);
  }
  if (since) {
    args.push("--since", since);
  }
  const payload = await runCliJson<{
    events: ActivityEvent[];
    cursor: string;
    run_id: string;
  }>(args, { cwd: projectRoot });
  return {
    events: payload.events ?? [],
    cursor: payload.cursor ?? since,
    runId: payload.run_id ?? runId,
  };
}

/**
 * Join one compact activity row to the full model output recorded for it.
 *
 * The runtime keeps transcripts in each job's private log and only projects a
 * bounded row into the polled stream, so this is read on demand when a row is
 * expanded rather than shipped with every poll.
 */
export async function fetchEventDetail(
  projectRoot: string,
  runId: string,
  eventId: string,
): Promise<ActivityEventDetail> {
  const payload = await runCliJson<unknown>(
    ["runs", "--project", projectRoot, "event", runId, eventId],
    { cwd: projectRoot },
  );
  return eventDetailForDisplay(normalizeEventDetail(payload, runId, eventId));
}

export async function fetchEventTypes(
  projectRoot: string,
): Promise<{ type: string; count: number }[]> {
  const payload = await runCliJson<{ types: { type: string; count: number }[] }>(
    ["runs", "--project", projectRoot, "types"],
    { cwd: projectRoot },
  );
  return payload.types ?? [];
}

/**
 * Read one run's durable aggregate.
 *
 * Computed by the CLI over the complete recorded stream, so these are totals
 * rather than whatever the UI happens to have buffered.
 */
export function fetchRunMetrics(
  projectRoot: string,
  runId: string,
): Promise<RunMetrics> {
  const args = ["runs", "--project", projectRoot, "metrics"];
  if (runId) {
    args.push(runId);
  }
  return runCliJson<RunMetrics>(args, { cwd: projectRoot, timeoutMs: 60_000 });
}

export function fetchRunLog(
  projectRoot: string,
  runId = "",
  tail = 400,
): Promise<string> {
  const args = ["runs", "--project", projectRoot, "log"];
  if (runId) {
    args.push(runId);
  }
  args.push("--tail", String(tail));
  return runCli(args, {
    cwd: projectRoot,
  }).then(runLogForDisplay);
}

/** Ask the CLI to interrupt a restored run after revalidating its identity. */
export async function stopWorkflow(
  projectRoot: string,
  runId: string,
): Promise<{ stopped: boolean; reason: string }> {
  type StopPayload = {
    stopped?: boolean;
    success?: boolean;
    reason?: string;
    error?: string;
  };
  let payload: StopPayload;
  try {
    payload = await runCliJson<StopPayload>(
      ["runs", "--project", projectRoot, "stop", runId],
      {
        cwd: projectRoot,
        timeoutMs: 20_000,
      },
    );
  } catch (error) {
    // `runs stop` intentionally exits nonzero when identity verification
    // refuses the signal, while still returning a structured explanation.
    if (error instanceof CliError && error.stdout) {
      try {
        payload = JSON.parse(error.stdout) as StopPayload;
      } catch {
        throw error;
      }
    } else {
      throw error;
    }
  }
  const stopped = payload.stopped ?? payload.success ?? false;
  return {
    stopped,
    reason:
      payload.reason ??
      payload.error ??
      (stopped ? "" : "LeanFlow could not confirm that the recorded process still owns this run."),
  };
}

/**
 * Resolve a launch without starting it.
 *
 * `--dry-run` skips the resource provisioning a real launch performs, so this
 * is safe to call whenever the launcher form changes.
 */
/**
 * Compose the child environment from the inherited one plus a resolved launch.
 *
 * `unset` is applied after `set` so an explicitly cleared knob does not survive
 * as an ambient value inherited from the editor's own environment.
 */
function childEnv(env: LaunchEnv): NodeJS.ProcessEnv {
  const composed: NodeJS.ProcessEnv = { ...process.env, ...env.set };
  for (const name of env.unset) {
    delete composed[name];
  }
  return composed;
}

export async function previewLaunch(
  projectRoot: string,
  argv: string[],
  env: LaunchEnv,
): Promise<LaunchPlanPreview> {
  const bin = cliPath();
  const args = ["workflow", "--dry-run", "--json", ...argv];
  const promptIndex = argv.indexOf("--prompt");
  const prompt = promptIndex >= 0 ? (argv[promptIndex + 1] ?? "") : "";
  try {
    const { stdout } = await execFileAsync(bin, args, {
      cwd: projectRoot,
      timeout: DEFAULT_TIMEOUT_MS,
      maxBuffer: 16 * 1024 * 1024,
      env: childEnv(env),
    });
    return launchPlanForDisplay(JSON.parse(stdout) as LaunchPlanPreview, prompt);
  } catch (error) {
    const err = error as NodeJS.ErrnoException & { stderr?: string };
    if (err.code === "ENOENT") {
      throw new CliError(
        `LeanFlow CLI was not found at "${bin}". Install LeanFlow or set ` +
          `LeanFlow: CLI Path in Settings.`,
      );
    }
    throw new CliError(
      redactSensitiveText(
        (err.stderr ?? "").toString().trim() || err.message,
        [prompt],
      ),
    );
  }
}

/**
 * Start a workflow as a detached background process.
 *
 * Detached rather than an integrated terminal on purpose: long runs and
 * sequential experiment sweeps must survive a window reload, and their output
 * is already recorded as structured state that the log viewer reads. The
 * child's stdio is discarded because the run log on disk is the authority.
 */
export function spawnWorkflow(
  projectRoot: string,
  argv: string[],
  env: LaunchEnv,
): { pid: number | null; kill: () => boolean; onExit: Promise<number | null> } {
  const child = spawn(cliPath(), ["workflow", ...argv], {
    cwd: projectRoot,
    env: childEnv(env),
    detached: true,
    stdio: "ignore",
  });
  child.unref();

  const onExit = new Promise<number | null>((resolve) => {
    child.once("exit", (code) => resolve(code));
    child.once("error", () => resolve(null));
  });

  return {
    pid: child.pid ?? null,
    kill: () => {
      if (child.pid === undefined) {
        return false;
      }
      try {
        // The child was started with `detached`, so it leads its own process
        // group. Signalling the group reaches the Lean and MCP subprocesses it
        // owns; signalling only the leader would orphan them.
        if (process.platform !== "win32") {
          process.kill(-child.pid, "SIGINT");
          return true;
        }
      } catch {
        // Fall back to the direct child below. This is also the only safe
        // primitive Node exposes for a detached child on Windows.
      }
      try {
        return child.kill("SIGINT");
      } catch {
        return false;
      }
    },
    onExit,
  };
}
