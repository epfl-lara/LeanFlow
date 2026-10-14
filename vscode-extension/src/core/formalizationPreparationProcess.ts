/** Run one owned preparation process with bounded output and cooperative process-group cancellation. */
import { spawn } from "node:child_process";
import { parseFormalizationSnapshot, type FormalizationSnapshot } from "./formalization";

const MAX_OUTPUT_BYTES = 8 * 1024 * 1024;
const MAX_PROGRESS_LINE_BYTES = 32768;
const MAX_PROGRESS_CHARACTERS = MAX_PROGRESS_LINE_BYTES / 4;

export interface FormalizationPreparationProcess<T = FormalizationSnapshot> {
  result: Promise<T>;
  cancel(): void;
}

/** Preserve the final JSON snapshot while never imposing the read-only CLI timeout on acquisition. */
export function startFormalizationPreparation(
  executable: string,
  args: string[],
  cwd: string,
  progress: (phase: string, message: string) => void,
): FormalizationPreparationProcess {
  return startFormalizationJsonProcess(executable, args, cwd, progress, parseFormalizationSnapshot);
}

/** Run preparation or its recovery protocol with the same ownership and output boundaries. */
export function startFormalizationJsonProcess<T>(
  executable: string, args: string[], cwd: string, progress: (phase: string, message: string) => void,
  decode: (value: unknown) => T,
): FormalizationPreparationProcess<T> {
  const child = spawn(executable, args, { cwd, stdio: ["ignore", "pipe", "pipe"], detached: process.platform !== "win32", env: { ...process.env, PYTHONUNBUFFERED: "1" } });
  const stdout: Buffer[] = [];
  let size = 0;
  let stderr = "";
  let line = "";
  let cancelled = false;
  let settled = false;
  let overflow = false;
  let stopping = false;
  const signal = (kind: NodeJS.Signals) => {
    if (settled || !child.pid) return;
    try { if (process.platform === "win32") child.kill(kind); else process.kill(-child.pid, kind); } catch { /* The owned process may already have exited. */ }
  };
  const stop = () => {
    if (stopping) return;
    stopping = true;
    // Infrastructure preservation may outlast any fixed deadline. Let the
    // controller finish its safe stopping point and report the actual receipt.
    signal("SIGINT");
  };
  const consume = (text: string) => {
    if (!text) return;
    stderr = (stderr + text + "\n").slice(-MAX_PROGRESS_CHARACTERS);
    try {
      const event = JSON.parse(text) as { phase?: unknown; message?: unknown };
      progress(typeof event.phase === "string" ? event.phase.slice(0, 256) : "preparing", typeof event.message === "string" ? event.message.slice(0, MAX_PROGRESS_CHARACTERS) : text.slice(0, MAX_PROGRESS_CHARACTERS));
    } catch { progress("preparing", text.slice(0, MAX_PROGRESS_CHARACTERS)); }
  };
  child.stdout.on("data", (chunk: Buffer) => {
    size += chunk.length;
    if (size > MAX_OUTPUT_BYTES) { overflow = true; stop(); return; }
    stdout.push(chunk);
  });
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk: string) => {
    line += chunk;
    let newline = line.indexOf("\n");
    while (newline >= 0) { consume(line.slice(0, newline)); line = line.slice(newline + 1); newline = line.indexOf("\n"); }
    if (Buffer.byteLength(line) > MAX_PROGRESS_LINE_BYTES) { consume(line.slice(0, 8192)); line = ""; }
  });
  const result = new Promise<T>((resolve, reject) => {
    const finish = (error?: Error, snapshot?: T) => {
      if (settled) return;
      settled = true;
      if (error) reject(error); else resolve(snapshot!);
    };
    child.once("error", (error: NodeJS.ErrnoException) => finish(new Error(error.code === "ENOENT" ? "LeanFlow CLI was not found. Configure the CLI and retry preparation." : error.message)));
    child.once("close", code => {
      consume(line);
      if (overflow) { finish(new Error("Preparation exceeded the bounded editor output budget. Inspect the controller logs on the owning host.")); return; }
      try {
        const text = Buffer.concat(stdout).toString("utf8");
        const raw = JSON.parse(text) as FormalizationSnapshot & { reason?: string; error?: string };
        if (code !== 0) throw new Error(raw.reason || raw.error || stderr.trim() || `Preparation exited with code ${code}`);
        finish(undefined, decode(raw));
      } catch (error) {
        finish(cancelled ? new Error("Formalization preparation cancelled without a final controller receipt. Refresh the source and inspect infrastructure recovery before continuing.") : error instanceof Error ? error : new Error(String(error)));
      }
    });
  });
  return { result, cancel() { if (cancelled || settled) return; cancelled = true; stop(); } };
}
