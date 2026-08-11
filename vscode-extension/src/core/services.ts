/**
 * Application-wide state shared by every LeanFlow surface.
 *
 * The tree view, the sidebar, and the dashboard all render the same snapshot,
 * so they read it from here rather than each maintaining their own copy and
 * drifting apart.
 */
import * as vscode from "vscode";

import { checkCli, fetchFlagCatalog, fetchProfiles } from "./cli";
import { ExperimentEngine } from "./experiments";
import {
  discoverProject,
  discoverProjectIncludingNested,
  listLeanFiles,
  NO_PROJECT,
} from "./project";
import { RunManager } from "./runManager";
import { trackedRunForLiveStatus } from "./runSelection";
import {
  flagCatalogForDisplay,
  profileCatalogForDisplay,
  redactSensitiveText,
} from "./runPrivacy";
import type { AppState, CliStatus, FlagCatalog, ProfileCatalog, ProjectInfo } from "./types";

export class LeanFlowServices implements vscode.Disposable {
  project: ProjectInfo = NO_PROJECT;
  cli: CliStatus = { ok: false, path: "leanflow", version: "", error: "not checked yet" };
  catalog: FlagCatalog | null = null;
  profiles: ProfileCatalog | null = null;
  leanFiles: string[] = [];
  selectedRunId: string | null = null;
  busy = false;

  readonly runs: RunManager;
  readonly experiments: ExperimentEngine;

  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changed.event;

  private readonly disposables: vscode.Disposable[] = [];

  constructor(context: vscode.ExtensionContext) {
    this.project = discoverProject();
    this.runs = new RunManager(context, this.project);
    this.experiments = new ExperimentEngine(context, this.runs, () => this.profiles, () => this.catalog);

    this.disposables.push(
      this.runs,
      this.experiments,
      this.runs.onDidChange(() => this.changed.fire()),
      this.experiments.onDidChange(() => this.changed.fire()),
      vscode.workspace.onDidChangeConfiguration((event) => {
        if (event.affectsConfiguration("leanflow")) {
          void this.reload();
        }
      }),
      vscode.window.onDidChangeActiveTextEditor(() => void this.rediscoverProject()),
      vscode.workspace.onDidChangeWorkspaceFolders(() => void this.rediscoverProject()),
    );
    void this.reload();
  }

  dispose(): void {
    for (const disposable of this.disposables) {
      disposable.dispose();
    }
    this.changed.dispose();
  }

  state(): AppState {
    const snapshot = this.runs.snapshot();
    const focusedRun = trackedRunForLiveStatus(snapshot.runs, this.selectedRunId);
    const focusedLiveStatus = focusedRun
      ? this.runs.liveStatusForRun(focusedRun.id)
      : null;
    return {
      project: this.project,
      cli: { ...this.cli, error: redactSensitiveText(this.cli.error) },
      catalog: flagCatalogForDisplay(this.catalog),
      profiles: profileCatalogForDisplay(this.profiles),
      runs: snapshot.runs,
      history: snapshot.history,
      selectedRunId: this.selectedRunId,
      // An isolated experiment cell has a different project-local status
      // file. Never pair a selected run with the source checkout's unrelated
      // snapshot merely because that is the workspace currently open.
      // A stopped historical row must not suppress the project's active CLI
      // run. Exact tracked selection still wins; otherwise expose the verified
      // live pointer, including workflows launched outside this window.
      liveStatus: focusedRun ? focusedLiveStatus : snapshot.liveStatus,
      eventTypes: snapshot.eventTypes,
      experiments: this.experiments.list(),
      leanFiles: this.leanFiles,
      busy: this.busy,
    };
  }

  selectRun(id: string | null): void {
    this.selectedRunId = id !== null && this.runs.getRun(id) ? id : null;
    this.changed.fire();
  }

  private async rediscoverProject(): Promise<void> {
    const next = await discoverProjectIncludingNested();
    if (next.root === this.project.root) {
      return;
    }
    this.project = next;
    this.runs.setProject(next);
    await this.reload();
  }

  /** Re-read everything that can change outside a run: CLI, catalog, files. */
  async reload(): Promise<void> {
    this.busy = true;
    this.changed.fire();
    try {
      this.project = await discoverProjectIncludingNested();
      this.runs.setProject(this.project);
      await vscode.commands.executeCommand(
        "setContext",
        "leanflow.hasProject",
        this.project.found,
      );

      this.cli = await checkCli();
      if (this.cli.ok) {
        const cwd = this.project.found ? this.project.root : undefined;
        const [catalog, profiles] = await Promise.all([
          fetchFlagCatalog(cwd).catch(() => null),
          fetchProfiles(cwd).catch(() => null),
        ]);
        this.catalog = catalog;
        this.profiles = profiles;
      } else {
        // Do not leave stale controls visible after a configured CLI moves or
        // becomes unavailable. The UI can now show setup guidance honestly.
        this.catalog = null;
        this.profiles = null;
      }
      this.leanFiles = await listLeanFiles(this.project);
      if (this.project.found) {
        await this.runs.refresh();
        await this.runs.loadTypes();
      }
    } finally {
      this.busy = false;
      this.changed.fire();
    }
  }

  /** Re-read only the profile list, after a profile is saved or deleted. */
  async reloadProfiles(): Promise<void> {
    const cwd = this.project.found ? this.project.root : undefined;
    this.profiles = await fetchProfiles(cwd).catch(() => this.profiles);
    this.changed.fire();
  }
}
