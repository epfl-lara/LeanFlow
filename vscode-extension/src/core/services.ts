/**
 * Application-wide state shared by every LeanFlow surface.
 *
 * The tree view, the sidebar, and the dashboard all render the same snapshot,
 * so they read it from here rather than each maintaining their own copy and
 * drifting apart.
 */
import * as vscode from "vscode";

import { checkCli, cliPath, fetchFlagCatalog, fetchProfiles } from "./cli";
import { CliHealth } from "./cliHealth";
import { ExperimentEngine } from "./experiments";
import {
  discoverProject,
  discoverProjectIncludingNested,
  listLeanFiles,
  NO_PROJECT,
} from "./project";
import { ProjectSetup } from "./projectSetup";
import { ProviderSetup } from "./providerSetup";
import { StorageService } from "./storage";
import { ProverService } from "./proverService";
import { RunManager } from "./runManager";
import { runChoices, selectedRunChoice } from "./runCatalog";
import {
  flagCatalogForDisplay,
  profileCatalogForDisplay,
  redactSensitiveText,
} from "./runPrivacy";
import type { AppState, CliStatus, FlagCatalog, ProfileCatalog, ProjectInfo } from "./types";

export class LeanFlowServices implements vscode.Disposable {
  project: ProjectInfo = NO_PROJECT;
  get cli(): CliStatus { return this.cliHealth.state; }
  catalog: FlagCatalog | null = null;
  profiles: ProfileCatalog | null = null;
  leanFiles: string[] = [];
  selectedRunId: string | null = null;
  busy = false;
  private runNavigation: { sequence: number; tab: "live" | "logs" } | undefined;

  readonly setup: ProjectSetup;
  readonly providers: ProviderSetup;
  readonly storage: StorageService;
  readonly runs: RunManager;
  readonly experiments: ExperimentEngine;
  /** Cached, change-gated reads of exact-run prover state. */
  readonly prover: ProverService;

  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changed.event;
  private readonly cliHealth = new CliHealth(checkCli, () => this.changed.fire(), () => void this.reload());
  private reloading: Promise<void> | undefined;
  private reloadAgain = false;
  private disposed = false;

  private readonly disposables: vscode.Disposable[] = [];
  private preparedProjectRoot = "";

  constructor(context: vscode.ExtensionContext) {
    this.project = discoverProject();
    this.providers = new ProviderSetup(context, () => this.project.root || undefined);
    this.storage = new StorageService();
    this.runs = new RunManager(context, this.project, () => this.providers.credentials.environment(), root => this.setup.assertLaunchReady(root));
    this.prover = new ProverService(this.runs);
    this.setup = new ProjectSetup({
      projectRoot: () => this.project.root,
      activeRun: (root) => this.runs.launchBlocker(root) !== null,
      completed: (root, ready) => {
        if (ready) this.preparedProjectRoot = root;
        return this.reload();
      },
    });
    this.experiments = new ExperimentEngine(context, this.runs, () => this.profiles, () => this.catalog);

    this.disposables.push(
      this.cliHealth,
      this.setup,
      this.setup.onDidChange(() => this.changed.fire()),
      this.providers,
      this.providers.onDidChange(() => this.changed.fire()),
      this.storage,
      this.storage.onDidChange(() => this.changed.fire()),
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
    this.disposed = true;
    for (const disposable of this.disposables) {
      disposable.dispose();
    }
    this.changed.dispose();
  }

  state(): AppState {
    const snapshot = this.runs.snapshot();
    const choice = selectedRunChoice(runChoices(snapshot.runs, snapshot.history, snapshot.liveStatus), this.selectedRunId);
    const focusedLiveStatus = choice?.tracked
      ? this.runs.liveStatusForRun(choice.tracked.id)
      : choice?.runId === snapshot.liveStatus?.run_id ? snapshot.liveStatus : null;
    return {
      providers: this.providers.state(),
      storage: this.storage.state(),
      projectSetup: this.setup.state(),
      project: this.project,
      cli: { ...this.cli, error: redactSensitiveText(this.cli.error) },
      catalog: flagCatalogForDisplay(this.catalog),
      profiles: profileCatalogForDisplay(this.profiles),
      runs: snapshot.runs,
      history: snapshot.history,
      selectedRunId: this.selectedRunId,
      projectLiveStatus: snapshot.liveStatus,
      runDiscoveryError: snapshot.discoveryError,
      runNavigation: this.runNavigation,
      // An isolated experiment cell has a different project-local status
      // file. Never pair a selected run with the source checkout's unrelated
      // snapshot merely because that is the workspace currently open.
      // A stopped historical row must not suppress the project's active CLI
      // run. Exact tracked selection still wins; otherwise expose the verified
      // live pointer, including workflows launched outside this window.
      liveStatus: focusedLiveStatus,
      eventTypes: snapshot.eventTypes,
      experiments: this.experiments.list(),
      leanFiles: this.leanFiles,
      busy: this.busy,
    };
  }

  selectRun(id: string | null): void {
    const snapshot = this.runs.snapshot();
    const choices = runChoices(snapshot.runs, snapshot.history, snapshot.liveStatus);
    this.selectedRunId = id !== null ? selectedRunChoice(choices, id)?.id ?? null : null;
    this.changed.fire();
  }

  /** Select a clicked job and navigate every dashboard to its live details or saved logs. */
  openRun(id: string): void {
    this.selectRun(id);
    const snapshot = this.runs.snapshot();
    const choice = selectedRunChoice(runChoices(snapshot.runs, snapshot.history, snapshot.liveStatus), this.selectedRunId);
    this.runNavigation = { sequence: (this.runNavigation?.sequence ?? 0) + 1, tab: choice?.active ? "live" : "logs" };
    this.changed.fire();
  }

  private async rediscoverProject(): Promise<void> {
    const next = await discoverProjectIncludingNested(this.preparedProjectRoot);
    if (next.root === this.project.root) {
      if (this.cli.ok || this.cli.health === "unavailable") await this.setup.refresh(this.cli.ok);
      return;
    }
    this.selectedRunId = null;
    this.project = next;
    this.runs.setProject(next);
    await this.reload();
  }

  /** Re-read everything that can change outside a run: CLI, catalog, files. */
  reload(): Promise<void> {
    if (this.disposed) return Promise.resolve();
    if (this.reloading) {
      this.reloadAgain = true;
      return this.reloading;
    }
    this.reloading = this.reloadState().finally(() => {
      this.reloading = undefined;
      if (this.reloadAgain && !this.disposed) {
        this.reloadAgain = false;
        void this.reload();
      }
    });
    return this.reloading;
  }

  /** Refresh one snapshot at a time so concurrent UI requests cannot race. */
  private async reloadState(): Promise<void> {
    this.busy = true;
    this.changed.fire();
    // An explicit refresh must re-read prover state even if its file signature
    // is unchanged; that is the user's escape hatch from any cache.
    this.prover.invalidate();
    try {
      this.project = await discoverProjectIncludingNested(this.preparedProjectRoot);
      this.runs.setProject(this.project);
      await vscode.commands.executeCommand(
        "setContext",
        "leanflow.hasProject",
        this.project.found,
      );

      const previousPath = this.cli.path;
      await this.cliHealth.check(cliPath());
      if (this.cli.ok || this.cli.health === "unavailable") await this.setup.refresh(this.cli.ok);
      void this.providers.refresh();
      if (this.cli.ok) {
        void this.storage.refresh();
        const cwd = this.project.found ? this.project.root : undefined;
        const [catalog, profiles] = await Promise.all([
          fetchFlagCatalog(cwd).catch(() => previousPath === this.cli.path ? this.catalog : null),
          fetchProfiles(cwd).catch(() => previousPath === this.cli.path ? this.profiles : null),
        ]);
        this.catalog = catalog;
        this.profiles = profiles;
      } else if (this.cli.health === "unavailable" || previousPath !== this.cli.path) {
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
