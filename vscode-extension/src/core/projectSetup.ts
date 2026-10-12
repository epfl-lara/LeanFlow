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
  private inspection = 0;
  private outputUpdate: NodeJS.Timeout | undefined;
  private readonly log = vscode.window.createOutputChannel("LeanFlow Project Setup");
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changed.event;

  constructor(private readonly context: SetupContext) {}

  dispose(): void {
    this.process?.cancel();
    if (this.outputUpdate) clearTimeout(this.outputUpdate);
    this.log.dispose();
    this.changed.dispose();
  }

  state(): ProjectSetupState {
    return { ...this.snapshot, activeRun: Boolean(this.snapshot.root && this.context.activeRun(this.snapshot.root)) };
  }

  /** Reject launches only for this project's known incomplete preparation. */
  assertLaunchReady(root: string): void {
    if (projectSetupBlocksLaunch(this.snapshot, root)) {
      throw new Error(this.snapshot.status === "running" ? "Project setup is still running. Wait for Ready to prove." : "Project setup is incomplete. Retry setup before starting a workflow.");
    }
  }

  /** Read persisted readiness without preparing tools or modifying project files. */
  async refresh(cliAvailable: boolean): Promise<void> {
    if (this.preparing) return;
    const root = currentSetupRoot(this.context.projectRoot());
    const generation = ++this.inspection;
    if (!root || !cliAvailable) {
      this.snapshot = { ...EMPTY_PROJECT_SETUP, root };
      this.changed.fire();
      return;
    }
    try {
      const parsed: unknown = JSON.parse(await runCli(["project", "inspect", root, "--json"]));
      if (generation !== this.inspection || this.preparing || !parsed || typeof parsed !== "object") return;
      const value = parsed as Record<string, unknown>;
      const read = (key: string) => typeof value[key] === "string" ? redactSensitiveText((value[key] as string).slice(0, 32_768)) : "";
      const status = ["detected", "ready", "failed", "cancelled"].includes(String(value.setup_status)) ? value.setup_status as "detected" | "ready" | "failed" | "cancelled" : "idle";
      this.snapshot = {
        ...EMPTY_PROJECT_SETUP, root: read("root") || root, status,
        phase: read("phase"), code: read("code"), message: read("message"),
        remediation: read("remediation"), logPath: read("log_path"), canRetry: status === "failed" || status === "cancelled",
        sourceDiagnostics: sourceDiagnostics(value.capabilities),
        recentOutput: this.snapshot.root === (read("root") || root) ? this.snapshot.recentOutput : [],
      };
      if (this.target?.root !== this.snapshot.root) this.target = { root: this.snapshot.root };
      this.changed.fire();
    } catch {
      // Older CLIs and projects may have no setup metadata. Absence is not a
      // failure and must not disable a previously working project.
      if (generation === this.inspection && !this.preparing && this.snapshot.root !== root) {
        this.snapshot = { ...EMPTY_PROJECT_SETUP, root };
        this.changed.fire();
      }
    }
  }

  /** Prepare the selected folder, retaining its exact request for Retry. */
  async start(retry = false, repair = false): Promise<void> {
    if (this.preparing) return;
    this.preparing = true;
    ++this.inspection;
    try {
      const chosen = retry && this.target ? this.target : await chooseSetupTarget(this.context.projectRoot());
      const target = chosen && (repair ? { ...chosen, repair: true } : chosen);
      if (!target) return;
      this.target = target;
      if (this.context.activeRun(target.root)) {
        throw new Error("A workflow is running in this project. Wait for it to finish before setting up or repairing project dependencies.");
      }
      this.snapshot = { ...EMPTY_PROJECT_SETUP, status: "running", root: target.root, phase: "checking_tools", message: "Checking project tools…" };
      this.log.clear();
      this.appendOutput(target.repair ? "Repairing project dependencies and verification tools…" : "Checking existing project setup; installing only what is missing…");
      this.changed.fire();
      await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: "LeanFlow project setup", cancellable: true }, async (progress, token) => {
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
        if (token.isCancellationRequested) this.cancel();
        try {
          const result = await owned.result;
          this.target = { ...target, root: result.root || target.root };
          this.snapshot = {
            status: result.status, root: result.root || target.root, phase: result.phase,
            code: result.code, message: redactSensitiveText(result.message),
            remediation: redactSensitiveText(result.remediation), logPath: result.log_path,
            activeRun: false, canRetry: result.status !== "ready",
            sourceDiagnostics: sourceDiagnostics(result.capabilities),
            recentOutput: this.snapshot.recentOutput,
          };
          this.appendOutput(`${result.status}: ${this.snapshot.message}`);
          if (this.snapshot.remediation) this.appendOutput(this.snapshot.remediation);
          this.changed.fire();
        } finally { cancellation.dispose(); this.process = null; }
      });
      // Rediscover the new manifest while retaining the exact terminal result
      // and Retry request; unrelated editor discovery must not replace them.
      await this.context.completed(this.snapshot.root, this.snapshot.status === "ready");
      if (this.snapshot.status === "ready" && target.openFolder) {
        await vscode.commands.executeCommand("vscode.openFolder", hostFileUri(this.snapshot.root), false);
      }
    } finally { this.preparing = false; }
  }

  /** Cancel only the currently owned setup, preserving retry information. */
  cancel(): void {
    if (!this.process) return;
    this.snapshot = { ...this.snapshot, message: "Cancelling project setup…" };
    this.changed.fire();
    this.process.cancel();
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
    if (this.process || this.preparing) { this.changed.fire(); return; }
    const { root, logPath } = this.snapshot;
    const generation = this.inspection;
    const current = () => generation === this.inspection && root === this.snapshot.root && logPath === this.snapshot.logPath;
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
        this.snapshot = { ...this.snapshot, recentOutput: redactSensitiveText(text, [], 32_768).split("\n").slice(-80) };
      } catch (error) {
        if (!current()) return;
        this.appendOutput(`Could not read the saved setup log: ${error instanceof Error ? error.message : String(error)}`);
      }
    }
    this.changed.fire();
  }
}
