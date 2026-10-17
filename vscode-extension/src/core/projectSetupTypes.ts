/** Shared project preparation state and the CLI's structured setup protocol. */
export type SetupStatus = "idle" | "detected" | "running" | "ready" | "failed" | "cancelled";

export interface ProjectSetupTarget {
  root: string;
  sourceFile?: string;
  repair?: boolean;
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
  recentOutput: string[];
}

export const EMPTY_PROJECT_SETUP: ProjectSetupState = {
  status: "idle", root: "", phase: "", message: "", code: "",
  remediation: "", logPath: "", activeRun: false, canRetry: false, sourceDiagnostics: [], recentOutput: [],
};

export type ProjectSetupEvent = ProjectSetupResult | {
  type: "progress" | "output";
  log_path?: string;
  phase: string;
  message: string;
};

/** Block only known incomplete preparation, never an uninspected legacy project. */
export function projectSetupBlocksLaunch(state: ProjectSetupState | undefined, root: string): boolean {
  return Boolean(state && state.root === root && ["running", "failed", "cancelled"].includes(state.status));
}

/** Keep Prove's legacy readiness gate while Formalization revalidates its stronger controller receipt. */
export function assertWorkflowProjectReady(setup: { assertLaunchReady(root: string): void; stateForRoot(root: string): ProjectSetupState }, root: string, kind?: "prove" | "formalize"): void {
  if (kind !== "formalize") setup.assertLaunchReady(root);
  else if (setup.stateForRoot(root).status === "running") throw new Error("Project setup is still running. Wait for project tools before starting formalization.");
}
