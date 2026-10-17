/** Coordinate one-click project preparation, retry, cancellation, and setup logs. */
import * as path from "node:path";
import { open } from "node:fs/promises";
import * as vscode from "vscode";
import { cliPath, runCli } from "./cli";
import { startProjectSetup, type SetupProcess } from "./projectSetupProcess";
import { EMPTY_PROJECT_SETUP, projectSetupBlocksLaunch, type ProjectSetupState, type ProjectSetupTarget } from "./projectSetupTypes";
import { redactSensitiveText } from "./runPrivacy";
import { hostFileUri, isHostFileUri } from "./workspaceUris";

interface SetupContext {
  projectRoot(): string;
  activeRun(root: string): boolean;
  completed(root: string, ready: boolean): Promise<void>;
}

export interface ScopedProjectSetupOptions {
  retry?: boolean;
  repair?: boolean;
  isCurrent?: () => boolean;
}

function sameSetupRoot(left: string, right: string): boolean {
  return Boolean(left && right && path.resolve(left) === path.resolve(right));
}

function copySetupState(state: ProjectSetupState): ProjectSetupState {
  return { ...state, sourceDiagnostics: [...state.sourceDiagnostics], recentOutput: [...state.recentOutput] };
}

/** Prefer the active editor's folder; require a choice for ambiguous workspaces. */
export async function chooseSetupTarget(projectRoot: string): Promise<ProjectSetupTarget | null> {
  const folders = (vscode.workspace.workspaceFolders ?? []).filter(folder => isHostFileUri(folder.uri));
  const active = vscode.window.activeTextEditor?.document.uri;
  const activeFolder = active && isHostFileUri(active) ? vscode.workspace.getWorkspaceFolder(active) : undefined;
  if (activeFolder && isHostFileUri(activeFolder.uri)) {
    const insideProject = projectRoot && active && !path.relative(projectRoot, active.fsPath).startsWith("..") && !path.isAbsolute(path.relative(projectRoot, active.fsPath));
    return { root: insideProject ? projectRoot : activeFolder.uri.fsPath };
  }
  if (projectRoot && folders.length <= 1) return { root: projectRoot };
  if (folders.length === 1) return { root: folders[0].uri.fsPath };
  if (folders.length > 1) {
    const picked = await vscode.window.showQuickPick(folders.map(folder => ({ label: folder.name, description: folder.uri.fsPath, root: folder.uri.fsPath })), { title: "Choose the folder to set up with LeanFlow" });
    return picked ? { root: picked.root } : null;
  }
  if (projectRoot) return { root: projectRoot };
  const sourceFile = active && isHostFileUri(active) && active.fsPath.endsWith(".lean") ? active.fsPath : undefined;
  const picked = await vscode.window.showOpenDialog({
    canSelectFolders: true, canSelectFiles: false, canSelectMany: false,
    title: sourceFile ? "Choose a project folder; your Lean file will be copied there" : "Choose a folder for your Lean project",
    openLabel: "Set up project here",
  });
  if (!picked?.[0]) return null;
  if (!isHostFileUri(picked[0])) throw new Error("Choose a folder on the current VS Code extension host.");
  return { root: picked[0].fsPath, sourceFile, openFolder: true };
}

/** Resolve a non-interactive setup candidate for the dashboard's readiness card. */
function currentSetupRoot(projectRoot: string): string {
  const active = vscode.window.activeTextEditor?.document.uri;
  const folder = active && isHostFileUri(active) ? vscode.workspace.getWorkspaceFolder(active) : undefined;
  if (folder && isHostFileUri(folder.uri)) {
    const relative = projectRoot && active ? path.relative(projectRoot, active.fsPath) : "..";
    return projectRoot && !relative.startsWith("..") && !path.isAbsolute(relative) ? projectRoot : folder.uri.fsPath;
  }
  if (projectRoot) return projectRoot;
  const folders = (vscode.workspace.workspaceFolders ?? []).filter(item => isHostFileUri(item.uri));
  return folders.length === 1 ? folders[0].uri.fsPath : "";
}

/** Bound and redact source warnings without confusing them with tool readiness. */
function sourceDiagnostics(capabilities: unknown): string[] {
  if (!capabilities || typeof capabilities !== "object" || Array.isArray(capabilities)) return [];
  const diagnostics = (capabilities as Record<string, unknown>).source_diagnostics;
  return Array.isArray(diagnostics)
    ? diagnostics.filter((item): item is string => typeof item === "string" && Boolean(item.trim()))
      .slice(0, 20).map(item => redactSensitiveText(item.slice(0, 2_000)))
    : [];
}

export class ProjectSetup implements vscode.Disposable {
  private snapshot: ProjectSetupState = { ...EMPTY_PROJECT_SETUP };
  private target: ProjectSetupTarget | null = null;
  private process: SetupProcess | null = null;
  private preparing = false;
  private cancellationRequested = false;
  private activeRoot = "";
  private readonly snapshots = new Map<string, ProjectSetupState>();
  private readonly baselineReadyRoots = new Set<string>();
  private inspection = 0;
  private outputUpdate: NodeJS.Timeout | undefined;
  private readonly log = vscode.window.createOutputChannel("LeanFlow Project Setup");
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changed.event;

  constructor(private readonly context: SetupContext) {}

  dispose(): void {
    this.cancel();
    if (this.outputUpdate) clearTimeout(this.outputUpdate);
    this.log.dispose();
    this.changed.dispose();
  }

  state(): ProjectSetupState {
    return { ...copySetupState(this.snapshot), activeRun: Boolean(this.snapshot.root && this.context.activeRun(this.snapshot.root)) };
  }

  /** Return only this workspace's readiness, with unrelated setup ownership kept anonymous. */
  stateForRoot(root: string): ProjectSetupState {
    const retained = this.retainedState(root) ?? { ...EMPTY_PROJECT_SETUP, root };
    let state = copySetupState(retained);
    if (state.status === "ready" && !this.baselineReadyRoots.has(path.resolve(root))) {
      state = { ...state, status: "detected", code: "setup_baseline_required", message: "Check setup to restore this workspace's registration and verified project tools.", remediation: "Use Set up project or Check setup before starting formalization." };
    }
    if (this.preparing && !sameSetupRoot(root, this.activeRoot)) {
      return { ...state, activeRun: true, code: "setup_busy_other_root", message: "Another project setup is running. Wait for it to finish before preparing this workspace." };
    }
    return { ...state, activeRun: Boolean(root && this.context.activeRun(root)) };
  }

  private retainedState(root: string): ProjectSetupState | undefined {
    return sameSetupRoot(root, this.snapshot.root) ? this.snapshot : root ? this.snapshots.get(path.resolve(root)) : undefined;
  }

  /** Retain bounded workspace-local state without exposing another workspace's log. */
  private retainState(state: ProjectSetupState): void {
    if (!state.root) return;
    const key = path.resolve(state.root);
    this.snapshots.delete(key);
    this.snapshots.set(key, copySetupState(state));
    if (this.snapshots.size > 16) {
      const oldest = this.snapshots.keys().next().value!;
      this.snapshots.delete(oldest);
      this.baselineReadyRoots.delete(oldest);
    }
  }

  private replaceState(state: ProjectSetupState): void {
    this.retainState(this.snapshot);
    this.snapshot = state;
    this.retainState(state);
  }

  /** Reject launches only for this project's known incomplete preparation. */
  assertLaunchReady(root: string): void {
    if (projectSetupBlocksLaunch(this.snapshot, root)) {
      throw new Error(this.snapshot.status === "running" ? "Project setup is still running. Wait for Ready to prove." : "Project setup is incomplete. Retry setup before starting a workflow.");
    }
  }

  /** Read persisted readiness without preparing tools or modifying project files. */
  async refresh(cliAvailable: boolean, requestedRoot?: string, isCurrent: () => boolean = () => true): Promise<void> {
    if (!isCurrent() || this.preparing && (requestedRoot === undefined || sameSetupRoot(requestedRoot, this.activeRoot))) return;
    const root = requestedRoot ?? currentSetupRoot(this.context.projectRoot());
    const generation = ++this.inspection;
    const install = (state: ProjectSetupState, baselineReady = false): void => {
      if (generation !== this.inspection || !isCurrent()) return;
      if (this.preparing) this.retainState(state);
      else {
        this.replaceState(state);
        if (!sameSetupRoot(this.target?.root ?? "", state.root)) this.target = { root: state.root };
      }
      if (state.root) {
        if (baselineReady) this.baselineReadyRoots.add(path.resolve(state.root));
        else this.baselineReadyRoots.delete(path.resolve(state.root));
      }
      this.changed.fire();
    };
    if (!root || !cliAvailable) {
      install({ ...EMPTY_PROJECT_SETUP, root });
      return;
    }
    try {
      const parsed: unknown = JSON.parse(await runCli(["project", "inspect", root, "--json"]));
      if (generation !== this.inspection || !isCurrent() || !parsed || typeof parsed !== "object") return;
      const value = parsed as Record<string, unknown>;
      const read = (key: string) => typeof value[key] === "string" ? redactSensitiveText((value[key] as string).slice(0, 32_768)) : "";
      if (requestedRoot !== undefined && !sameSetupRoot(read("root"), root)) throw new Error("Inspection did not identify the selected workspace.");
      let status: ProjectSetupState["status"] = ["detected", "ready", "failed", "cancelled"].includes(String(value.setup_status)) ? value.setup_status as "detected" | "ready" | "failed" | "cancelled" : "idle";
      const incomplete = requestedRoot !== undefined && status === "ready" && (value.ready !== true || value.configured !== true);
      if (incomplete) status = "detected";
      install({
        ...EMPTY_PROJECT_SETUP, root: requestedRoot !== undefined ? root : read("root") || root, status,
        phase: read("phase"), code: read("code"), message: read("message"),
        remediation: read("remediation"), logPath: read("log_path"), canRetry: status === "failed" || status === "cancelled",
        sourceDiagnostics: sourceDiagnostics(value.capabilities),
        recentOutput: this.retainedState(root)?.recentOutput ?? [],
        ...(incomplete ? { code: "setup_baseline_required", message: "Check setup to restore this workspace's registration and verified project tools.", remediation: "Use Set up project or Check setup before starting formalization." } : {}),
      }, value.ready === true && value.configured === true);
    } catch {
      // Older CLIs and projects may have no setup metadata. Absence is not a
      // failure and must not disable a previously working project.
      if (requestedRoot !== undefined || !sameSetupRoot(this.snapshot.root, root)) {
        install({ ...EMPTY_PROJECT_SETUP, root });
      }
    }
  }

  /** Prepare the selected folder, retaining its exact request for Retry. */
  async start(retry = false, repair = false): Promise<void> {
    if (this.preparing) return;
    await this.startOwned(retry, repair);
  }

  /** Prepare an explicitly selected host workspace without consulting editor focus. */
  async startForRoot(root: string, options: ScopedProjectSetupOptions = {}): Promise<void> {
    if (!root || !path.isAbsolute(root)) throw new Error("Choose a source workspace on this extension host before setting up its project.");
    if (this.preparing) throw new Error("A project setup is already running. Wait for it to finish or cancel its owning workspace before retrying.");
    await this.startOwned(options.retry === true, options.repair === true, root, options.isCurrent);
  }

  /** Reserve setup ownership and retain terminal evidence even after the selected workspace changes. */
  private async startOwned(retry: boolean, repair: boolean, root?: string, isCurrent: () => boolean = () => true): Promise<void> {
    const assertCurrent = () => { if (!isCurrent()) throw new Error("The formalization workspace changed. Refresh before setting up its project."); };
    assertCurrent();
    this.preparing = true;
    this.cancellationRequested = false;
    this.activeRoot = root ?? "";
    ++this.inspection;
    try {
      const chosen = root !== undefined ? retry && this.target && sameSetupRoot(this.target.root, root) ? this.target : { root }
        : retry && this.target ? this.target : await chooseSetupTarget(this.context.projectRoot());
      const target = chosen && (repair ? { ...chosen, repair: true } : chosen);
      if (!target) return;
      assertCurrent();
      this.activeRoot = target.root;
      this.target = target;
      if (this.context.activeRun(target.root)) {
        throw new Error("A workflow is running in this project. Wait for it to finish before setting up or repairing project dependencies.");
      }
      this.replaceState({ ...EMPTY_PROJECT_SETUP, status: "running", root: target.root, phase: "checking_tools", message: "Checking project tools…" });
      this.baselineReadyRoots.delete(path.resolve(target.root));
      this.log.clear();
      this.appendOutput(target.repair ? "Repairing project dependencies and verification tools…" : "Checking existing project setup; installing only what is missing…");
      this.changed.fire();
      await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: "LeanFlow project setup", cancellable: true }, async (progress, token) => {
        if (this.cancellationRequested || token.isCancellationRequested) {
          this.replaceState({ ...this.snapshot, status: "cancelled", code: "cancelled", canRetry: true,
            message: "Project setup cancelled before starting.", remediation: "Retry to continue project setup." });
          this.appendOutput(this.snapshot.message);
          this.changed.fire();
          return;
        }
        assertCurrent();
        const owned = startProjectSetup(cliPath(), target, event => {
          const message = redactSensitiveText(event.message);
          this.appendOutput(`[${event.phase || "setup"}] ${message}`);
          if (event.log_path && path.isAbsolute(event.log_path)) this.snapshot.logPath = event.log_path;
          if (event.type === "progress") {
            this.snapshot = { ...this.snapshot, phase: event.phase, message };
            progress.report({ message });
            this.changed.fire();
          }
        });
        this.process = owned;
        const cancellation = token.onCancellationRequested(() => this.cancel());
        if (this.cancellationRequested || token.isCancellationRequested) this.cancel();
        try {
          const result = await owned.result;
          if (root !== undefined && !sameSetupRoot(root, result.root)) throw new Error("Project setup did not identify the selected workspace. Refresh before retrying.");
          this.target = { ...target, root: result.root || target.root };
          this.replaceState({
            status: result.status, root: result.root || target.root, phase: result.phase,
            code: result.code, message: redactSensitiveText(result.message),
            remediation: redactSensitiveText(result.remediation), logPath: result.log_path,
            activeRun: false, canRetry: result.status !== "ready",
            sourceDiagnostics: sourceDiagnostics(result.capabilities),
            recentOutput: this.snapshot.recentOutput,
          });
          if (result.status === "ready") this.baselineReadyRoots.add(path.resolve(this.snapshot.root));
          this.appendOutput(`${result.status}: ${this.snapshot.message}`);
          if (this.snapshot.remediation) this.appendOutput(this.snapshot.remediation);
          this.changed.fire();
        } finally { cancellation.dispose(); this.process = null; }
      });
      // Rediscover the new manifest while retaining the exact terminal result
      // and Retry request; unrelated editor discovery must not replace them.
      this.retainState(this.snapshot);
      if (isCurrent()) await this.context.completed(this.snapshot.root, this.snapshot.status === "ready");
      if (isCurrent() && this.snapshot.status === "ready" && target.openFolder) {
        await vscode.commands.executeCommand("vscode.openFolder", hostFileUri(this.snapshot.root), false);
      }
    } catch (error) {
      if (this.snapshot.status === "running" && sameSetupRoot(this.snapshot.root, this.activeRoot)) {
        this.replaceState({ ...this.snapshot, status: "failed", code: "setup_interrupted", canRetry: true,
          message: redactSensitiveText(error instanceof Error ? error.message : String(error)), remediation: "Refresh the selected workspace and retry setup." });
      }
      throw error;
    } finally { this.preparing = false; this.activeRoot = ""; this.cancellationRequested = false; this.retainState(this.snapshot); this.changed.fire(); }
  }

  /** Cancel only setup owned by this exact host workspace. */
  cancelForRoot(root: string): void {
    if (!this.preparing || !sameSetupRoot(root, this.activeRoot)) throw new Error("No active project setup belongs to this workspace. Refresh its setup status.");
    this.cancel();
  }

  /** Cancel only the currently owned setup, preserving retry information. */
  cancel(): void {
    if (!this.preparing && !this.process) return;
    this.cancellationRequested = true;
    if (sameSetupRoot(this.activeRoot, this.snapshot.root)) {
      this.snapshot = { ...this.snapshot, message: "Cancelling project setup…" };
      this.changed.fire();
    }
    this.process?.cancel();
  }

  /** Publish a bounded live tail without broadcasting a full app state for every line. */
  private appendOutput(message: string): void {
    const safe = redactSensitiveText(message).slice(-4_000);
    this.log.appendLine(safe);
    this.snapshot = { ...this.snapshot, recentOutput: [...this.snapshot.recentOutput, safe].slice(-40) };
    if (!this.outputUpdate) this.outputUpdate = setTimeout(() => {
      this.outputUpdate = undefined;
      this.changed.fire();
    }, 100);
  }

  /** Load a bounded saved tail into the card; live output is already streamed there. */
  async showLog(): Promise<void> {
    await this.readLog(this.snapshot.root);
  }

  /** Load only the selected workspace's retained setup log. */
  async showLogForRoot(root: string): Promise<void> {
    if (!root || !this.retainedState(root)) throw new Error("No setup log belongs to this workspace. Refresh its setup status.");
    if (this.preparing && !sameSetupRoot(root, this.activeRoot)) throw new Error("Another project setup is running. Wait before opening this workspace's saved setup log.");
    await this.readLog(root);
  }

  /** Read a bounded saved log without replacing a different workspace's active snapshot. */
  private async readLog(root: string): Promise<void> {
    if (this.process || this.preparing) { this.changed.fire(); return; }
    const retained = this.retainedState(root);
    if (!retained) return;
    const { logPath } = retained;
    const generation = this.inspection;
    const current = () => generation === this.inspection && logPath === this.retainedState(root)?.logPath;
    const installOutput = (recentOutput: string[]) => {
      const next = { ...retained, recentOutput };
      if (sameSetupRoot(root, this.snapshot.root)) this.replaceState(next);
      else this.retainState(next);
    };
    if (logPath && path.isAbsolute(logPath)) {
      try {
        const handle = await open(logPath, "r");
        let text: string;
        try {
          const size = (await handle.stat()).size;
          const buffer = Buffer.alloc(Math.min(size, 32_768));
          const { bytesRead } = await handle.read(buffer, 0, buffer.length, Math.max(0, size - buffer.length));
          text = buffer.subarray(0, bytesRead).toString("utf8");
        } finally { await handle.close(); }
        if (!current()) return;
        installOutput(redactSensitiveText(text, [], 32_768).split("\n").slice(-80));
      } catch (error) {
        if (!current()) return;
        const message = redactSensitiveText(`Could not read the saved setup log: ${error instanceof Error ? error.message : String(error)}`).slice(-4_000);
        this.log.appendLine(message);
        installOutput([...retained.recentOutput, message].slice(-40));
      }
    }
    this.changed.fire();
  }
}
