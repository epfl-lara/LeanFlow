/** Shared project preparation state and the CLI's structured setup protocol. */
export type SetupStatus = "idle" | "running" | "ready" | "failed" | "cancelled";

export interface ProjectSetupTarget {
  root: string;
  sourceFile?: string;
  openFolder?: boolean;
}

export interface ProjectSetupResult {
  type: "result";
  status: "ready" | "failed" | "cancelled";
  phase: string;
  code: string;
  message: string;
  remediation: string;
  log_path: string;
  root: string;
  changed_files: string[];
  capabilities: Record<string, unknown>;
}

export interface ProjectSetupState {
  status: SetupStatus;
  root: string;
  phase: string;
  message: string;
  code: string;
  remediation: string;
  logPath: string;
  activeRun: boolean;
  canRetry: boolean;
  sourceDiagnostics: string[];
}

export const EMPTY_PROJECT_SETUP: ProjectSetupState = {
  status: "idle", root: "", phase: "", message: "", code: "",
  remediation: "", logPath: "", activeRun: false, canRetry: false, sourceDiagnostics: [],
};

export type ProjectSetupEvent = ProjectSetupResult | {
  type: "progress" | "output";
  phase: string;
  message: string;
};

/** Block only known incomplete preparation, never an uninspected legacy project. */
export function projectSetupBlocksLaunch(state: ProjectSetupState | undefined, root: string): boolean {
  return Boolean(state && state.root === root && ["running", "failed", "cancelled"].includes(state.status));
}
