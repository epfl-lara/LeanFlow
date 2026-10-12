/** Normalize runtime outcomes for both the editor host and browser job menus. */
import type { TrackedRunStatus } from "./types";

/** Map the CLI's exact terminal vocabulary into the compact tree-view states. */
export function terminalTrackedRunStatus(
  statusValue: unknown,
  phaseValue: unknown,
  exitCodeValue: unknown,
): TrackedRunStatus {
  const terminal = String(statusValue ?? "").trim().toLowerCase();
  if (terminal === "interrupted") {
    return "stopped";
  }
  if (terminal === "failed") {
    return "failed";
  }
  if (["succeeded", "paused", "disproved", "exited"].includes(terminal)) {
    return "finished";
  }
  const phase = String(phaseValue ?? "").trim().toLowerCase();
  if (phase === "stopped" || phase === "interrupted") {
    return "stopped";
  }
  const exitCode =
    typeof exitCodeValue === "number" && Number.isInteger(exitCodeValue)
      ? exitCodeValue
      : null;
  if (exitCode === 0) {
    return "finished";
  }
  if (exitCode === 130) {
    return "stopped";
  }
  if (exitCode !== null || phase === "failed" || phase === "dead") {
    return "failed";
  }
  return "finished";
}
