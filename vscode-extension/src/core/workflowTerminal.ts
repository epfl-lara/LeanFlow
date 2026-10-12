/** Launch durable tmux jobs where available, with a normal VS Code terminal fallback. */
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { promisify } from "node:util";
import * as vscode from "vscode";

import type { RunConnection } from "./types";

const execute = promisify(execFile);
const SESSION = "job";
const TERMINAL_PREFIX = "LeanFlow: ";

export interface WorkflowTerminal {
  pid: number | null;
  connection: RunConnection;
  notice: string;
  onExit?: Promise<number | null>;
  pending?: boolean;
}

/** Derive a private server name so unrelated tmux sessions and environments are untouched. */
export function workflowSocket(runId: string): string {
  return `leanflow-${createHash("sha256").update(runId).digest("hex").slice(0, 24)}`;
}

/** Quote one literal argument for tmux's explicitly configured POSIX shell. */
export function quoteShell(value: string): string {
  return `'${value.replace(/'/g, "'\\''")}'`;
}

function tmuxArgs(runId: string, args: string[]): string[] {
  return ["-L", workflowSocket(runId), ...args];
}

async function tmux(runId: string, args: string[]): Promise<string> {
  const result = await execute("tmux", tmuxArgs(runId, args), {
    timeout: 5000, maxBuffer: 256 * 1024,
  });
  return result.stdout;
}

/** Inspect only this exact run's private pane; unknown is never proof of completion. */
export async function inspectWorkflowTerminal(runId: string): Promise<
  | { state: "running"; pid: number | null }
  | { state: "exited"; code: number | null; output: string }
  | { state: "missing" }
  | { state: "unknown" }
> {
  try {
    const line = await tmux(runId, ["display-message", "-p", "-t", `${SESSION}:0.0`, "#{pane_dead}|#{pane_dead_status}|#{pane_pid}"]);
    const [dead, code, pid] = line.trim().split("|");
    if (dead === "0") return { state: "running", pid: Number(pid) > 0 ? Number(pid) : null };
    if (dead !== "1") return { state: "unknown" };
    const output = await tmux(runId, ["capture-pane", "-p", "-t", `${SESSION}:0.0`, "-S", "-80"]).catch(() => "");
    return { state: "exited", code: /^\d+$/.test(code ?? "") ? Number(code) : null, output };
  } catch (error) {
    const stderr = String((error as { stderr?: string }).stderr ?? "");
    return { state: /no server running|no such file|can't find session/i.test(stderr) ? "missing" : "unknown" };
  }
}

/** Release an exited private pane after its outcome has been durably recorded. */
export async function releaseWorkflowTerminal(runId: string): Promise<boolean> {
  const pane = await inspectWorkflowTerminal(runId);
  if (pane.state === "missing") return true;
  if (pane.state !== "exited") return false;
  return tmux(runId, ["kill-session", "-t", SESSION]).then(() => true, () => false);
}

/** Reattach a tmux job, or reveal the ordinary terminal if this editor still owns it. */
export async function showWorkflowTerminal(runId: string, connection: RunConnection): Promise<void> {
  const name = TERMINAL_PREFIX + runId;
  const existing = vscode.window.terminals.find(terminal => terminal.name === name && !terminal.exitStatus);
  if (existing) { existing.show(true); return; }
  if (connection.kind !== "tmux") throw new Error("This terminal is no longer open. The job's saved activity and logs remain available.");
  const pane = await inspectWorkflowTerminal(runId);
  if (pane.state !== "running") throw new Error("This job's tmux session is no longer live. Open its saved logs instead.");
  const terminal = vscode.window.createTerminal({
    name, shellPath: "tmux", shellArgs: tmuxArgs(runId, ["attach-session", "-t", SESSION]),
    env: { TMUX: null },
    message: "LeanFlow runs in tmux. Closing this terminal detaches; use LeanFlow's Stop button to stop the job.",
  });
  terminal.show(true);
}

/** Start exactly once; fallback is allowed only before tmux could have launched the job. */
export async function launchWorkflowTerminal(
  executable: string, projectRoot: string, argv: string[], env: NodeJS.ProcessEnv,
  prepared: (connection: RunConnection) => Promise<void> = async () => {},
): Promise<WorkflowTerminal> {
  const runId = env.LEANFLOW_WORKFLOW_RUN_ID;
  if (!runId) throw new Error("A durable run id is required before starting a workflow.");
  let fallback = "tmux is not supported on this platform.";
  if (process.platform !== "win32") {
    try {
      await execute("tmux", ["-V"], { timeout: 5000, env });
      fallback = "";
    } catch {
      fallback = "tmux is not installed or could not be started.";
    }
  }
  if (!fallback) {
    // A fresh server inherits this launch's exact environment. Never put API
    // keys in command arguments, configuration files, or a shared tmux server.
    const directory = await mkdtemp(path.join(os.tmpdir(), "leanflow-tmux-"));
    const config = path.join(directory, "tmux.conf");
    try {
      await writeFile(config, 'set-option -g default-shell /bin/sh\nset-option -g update-environment ""\nset-window-option -g remain-on-exit on\n', { mode: 0o600 });
      await prepared({ kind: "tmux" });
      const command = "exec " + [executable, "workflow", ...argv].map(quoteShell).join(" ");
      try {
        await execute("tmux", tmuxArgs(runId, ["-f", config, "new-session", "-d", "-s", SESSION, "-c", projectRoot, command]), {
          env: { ...env, TMUX: undefined }, timeout: 10_000, maxBuffer: 256 * 1024,
        });
      } catch (error) {
        // A timeout/error after dispatch has an ambiguous outcome. Check the
        // exact server and never launch a second copy in another terminal.
        const pane = await inspectWorkflowTerminal(runId);
        const uncertain = pane.state === "unknown" || Boolean((error as { killed?: boolean; signal?: string }).killed || (error as { signal?: string }).signal);
        if (uncertain) return { pid: null, connection: { kind: "tmux" }, pending: true, notice: "Confirming the tmux launch. LeanFlow will keep checking this job; no second process was started." };
        if (pane.state === "missing") fallback = "tmux could not create a session.";
      }
      if (!fallback) return { pid: null, connection: { kind: "tmux" }, notice: "Running in tmux. The job survives closing VS Code or disconnecting from the host." };
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  }
  await prepared({ kind: "terminal" });
  const terminal = vscode.window.createTerminal({
    name: TERMINAL_PREFIX + runId, cwd: projectRoot, shellPath: executable,
    shellArgs: ["workflow", ...argv], env, strictEnv: true,
    message: `${fallback} Using a regular terminal. Keep it open until the job finishes; saved logs remain available afterward.`,
  });
  const onExit = new Promise<number | null>(resolve => {
    const listener = vscode.window.onDidCloseTerminal(closed => {
      if (closed !== terminal) return;
      listener.dispose();
      resolve(closed.exitStatus?.code ?? null);
    });
  });
  terminal.show(true);
  return { pid: null, connection: { kind: "terminal" }, onExit, notice: `${fallback} Running in a regular terminal; keep it open until the job finishes.` };
}
