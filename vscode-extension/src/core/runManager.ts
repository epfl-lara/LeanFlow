/**
 * Own the lifecycle and observation of launched runs.
 *
 * A run is a detached child process plus a stream of state the runtime records
 * on disk. This class is the single place that starts one, tracks it across a
 * window reload, tails its activity, and reports when it ends.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

import {
  fetchEventTypes,
  fetchEvents,
  fetchLiveStatus,
  fetchRunLog,
  fetchRuns,
  spawnWorkflow,
  stopWorkflow,
} from "./cli";
import { mergeActivityEvents } from "./eventBuffer";
import { idleDiscoveryDecision, type IdleDiscoveryState } from "./idleDiscovery";
import {
  buildWorkflowArgs,
  describeLaunchLabel,
  resolveLaunchEnv,
} from "./launch";
import { validateLaunchProjectInputs } from "./launchPaths";
import type { FileSignature } from "./proverCache";
import {
  durableOwnerDescription,
  researchConflictDescription,
  resolveRestoredRun,
  sameProjectRoot,
  terminalTrackedRunStatus,
  trackedOwnerDescription,
} from "./runOwnership";
import {
  liveStatusForDisplay,
  redactSensitiveText,
  runSummaryForDisplay,
  trackedRunForPersistence,
} from "./runPrivacy";
import { isActiveLiveStatus } from "./runSelection";
import type {
  ActivityEvent,
  FlagCatalog,
  LaunchRequest,
  LiveStatus,
  ProfileCatalog,
  ProjectInfo,
  RunSummary,
  TrackedRun,
  TrackedRunStatus,
} from "./types";

const RUNS_STORAGE_KEY = "leanflow.trackedRuns";
const MAX_CACHED_EVENT_RUNS = 20;
/** The runtime's live-owner snapshot; a change to it is the signal that a run started or ended. */
const LIVE_STATUS_FILE = "live_status.json";
const WATCHER_DEBOUNCE_MS = 1_000;

interface LiveHandle {
  kill: () => boolean;
}

export interface RunManagerSnapshot {
  runs: TrackedRun[];
  history: RunSummary[];
  liveStatus: LiveStatus | null;
  eventTypes: { type: string; count: number }[];
}

export class RunManager implements vscode.Disposable {
  private runs = new Map<string, TrackedRun>();
  private handles = new Map<string, LiveHandle>();
  private events = new Map<string, ActivityEvent[]>();
  private cursors = new Map<string, string>();
  private history: RunSummary[] = [];
  private liveStatus: LiveStatus | null = null;
  private statusesByRoot = new Map<string, LiveStatus | null>();
  private historyRoots = new Map<string, string>();
  private newestHistoryByRoot = new Map<string, RunSummary>();
  private launchingRoots = new Set<string>();
  private stopRequested = new Set<string>();
  private eventTypes: { type: string; count: number }[] = [];
  private timer: NodeJS.Timeout | null = null;
  private polling = false;
  private persistQueue: Promise<void> = Promise.resolve();
  private project: ProjectInfo;
  // Idle discovery: while nothing is polled, watch the live-status file and
  // check its signature on a bounded interval so a run started from a terminal
  // is noticed without a CLI read per tick.
  private idleTimer: NodeJS.Timeout | null = null;
  private idleWatcher: vscode.FileSystemWatcher | null = null;
  private idleDebounce: NodeJS.Timeout | null = null;
  private idleState: IdleDiscoveryState = { lastSignature: undefined, lastCheckedAt: 0 };
  private disposed = false;

  private readonly changed = new vscode.EventEmitter<void>();
  /** Fires whenever tracked runs, history, or live status change. */
  readonly onDidChange = this.changed.event;

  private readonly eventsAppended = new vscode.EventEmitter<{
    runId: string;
    events: ActivityEvent[];
    reset: boolean;
  }>();
  readonly onDidAppendEvents = this.eventsAppended.event;

  constructor(
    private readonly context: vscode.ExtensionContext,
    project: ProjectInfo,
  ) {
    this.project = project;
    this.restore();
  }

  dispose(): void {
    this.disposed = true;
    this.clearPollTimer();
    this.clearIdleWatch();
    this.changed.dispose();
    this.eventsAppended.dispose();
  }

  // ------------------------------------------------------------------ state

  setProject(project: ProjectInfo): void {
    if (project.root === this.project.root) {
      return;
    }
    this.project = project;
    this.stopIdleWatch();
    this.idleState = { lastSignature: undefined, lastCheckedAt: 0 };
    this.events.clear();
    this.cursors.clear();
    this.liveStatus = null;
    this.statusesByRoot.clear();
    this.historyRoots.clear();
    this.newestHistoryByRoot.clear();
    this.history = [];
    void this.refresh();
  }

  snapshot(): RunManagerSnapshot {
    return {
      runs: [...this.runs.values()]
        .map(trackedRunForPersistence)
        .sort((a, b) => b.startedAt.localeCompare(a.startedAt)),
      history: this.history.map(runSummaryForDisplay),
      liveStatus: this.liveStatus === null ? null : liveStatusForDisplay(this.liveStatus),
      eventTypes: this.eventTypes,
    };
  }

  /** The project these runs belong to. */
  projectRoot(): string {
    return this.project.root;
  }

  getRun(id: string): TrackedRun | undefined {
    return this.runs.get(id);
  }

  /** Resolve only known exact run ids, including isolated experiment checkouts. */
  projectRootForRun(runId: string): string | null {
    return [...this.runs.values()].find((run) => run.runId === runId)?.projectRoot
      ?? this.historyRoots.get(runId)
      ?? (this.liveStatus?.run_id === runId ? this.project.root : null);
  }

  /**
   * The state file the runtime advertises for an exact run id, if live status
   * names one. Used only as a change signal; the CLI remains the reader.
   */
  proverStatePathHint(runId: string): string | null {
    if (!runId) {
      return null;
    }
    for (const status of this.statusesByRoot.values()) {
      if (status !== null && String(status.run_id ?? "") === runId) {
        const hint = status.prover_state_path;
        return typeof hint === "string" && hint !== "" ? hint : null;
      }
    }
    return null;
  }

  /** Return live state only when it belongs to this exact tracked run. */
  liveStatusForRun(id: string): LiveStatus | null {
    const run = this.runs.get(id);
    if (!run?.runId) {
      return null;
    }
    const status = this.statusesByRoot.get(this.rootKey(run.projectRoot)) ?? null;
    return status !== null && String(status.run_id ?? "") === run.runId ? status : null;
  }

  activeRuns(projectRoot?: string): TrackedRun[] {
    return [...this.runs.values()].filter(
      (run) =>
        (projectRoot === undefined || sameProjectRoot(run.projectRoot, projectRoot)) &&
        (run.status === "starting" || run.status === "running"),
    );
  }

  /** Explain why this project cannot accept another workflow owner. */
  launchBlocker(projectRoot = this.project.root): string | null {
    const tracked = trackedOwnerDescription(this.runs.values(), projectRoot);
    if (tracked !== null) {
      return tracked;
    }
    if (this.launchingRoots.has(this.rootKey(projectRoot))) {
      return "Another workflow launch is already being prepared.";
    }
    const status = this.statusesByRoot.get(this.rootKey(projectRoot)) ?? null;
    return durableOwnerDescription(status);
  }

  eventsFor(runId: string): ActivityEvent[] {
    return this.events.get(runId) ?? [];
  }

  // ----------------------------------------------------------------- launch

  /**
   * Start a run and begin tracking it.
   *
   * The tracked record is written before the process starts so a launch that
   * fails immediately is still visible with its error, rather than vanishing.
   */
  async launch(
    request: LaunchRequest,
    profiles: ProfileCatalog | null,
    catalog: FlagCatalog | null,
    options: {
      experimentId?: string;
      experimentCell?: string;
      projectRoot?: string;
    } = {},
  ): Promise<TrackedRun> {
    if (!this.project.found && !options.projectRoot) {
      throw new Error("No LeanFlow project is open.");
    }
    const projectRoot = options.projectRoot ?? this.project.root;
    const rootKey = this.rootKey(projectRoot);
    await validateLaunchProjectInputs(
      projectRoot,
      request.target,
      request.additionalSkills,
    );
    const researchConflict = researchConflictDescription(
      this.runs.values(),
      options.experimentId,
    );
    if (researchConflict !== null) {
      throw new Error(researchConflict);
    }
    const trackedBlocker = trackedOwnerDescription(this.runs.values(), projectRoot);
    if (trackedBlocker !== null || this.launchingRoots.has(rootKey)) {
      throw new Error(
        `${trackedBlocker ?? "Another workflow launch is already being prepared."} ` +
          "Stop it before starting another run in this project.",
      );
    }
    // Reserve before the asynchronous status probe so two clicks cannot both
    // observe an empty project and race each other into `spawn`.
    this.launchingRoots.add(rootKey);

    let durableStatus: LiveStatus | null;
    try {
      durableStatus = await fetchLiveStatus(projectRoot);
      this.statusesByRoot.set(rootKey, durableStatus);
      const durableBlocker = durableOwnerDescription(durableStatus);
      if (durableBlocker !== null) {
        throw new Error(
          `${durableBlocker} Stop it before starting another run in this project.`,
        );
      }
    } catch (error) {
      this.launchingRoots.delete(rootKey);
      throw error;
    }
    const id = `run-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;

    let resolved: ReturnType<typeof resolveLaunchEnv>;
    let argv: string[];
    try {
      resolved = resolveLaunchEnv(request, profiles, catalog);
      if (resolved.rejected.length > 0) {
        throw new Error(
          "Refusing to launch: these knobs are invalid, unknown, non-editable, or terminal-only — " +
            `${resolved.rejected.join(", ")}.`,
        );
      }
      argv = buildWorkflowArgs(request, resolved.env.set);
    } catch (error) {
      this.launchingRoots.delete(rootKey);
      throw error;
    }
    const { env } = resolved;

    // Mint the run id here rather than inferring it afterwards. The runtime
    // honours a preset LEANFLOW_WORKFLOW_RUN_ID (workflow_state._workflow_run_id),
    // so every later lookup — logs, stop, experiment scoring — can key on an
    // identity we chose, instead of guessing which recorded stream belongs to
    // which launch.
    const runId = `${request.kind}-vscode-${id}`;
    env.set.LEANFLOW_WORKFLOW_RUN_ID = runId;

    // Keep the raw request/argv/env local to this method. The tracked object is
    // both durable workspace state and a webview payload, so it receives only
    // a bounded prompt fingerprint and credential-safe override values.
    const run = trackedRunForPersistence({
      id,
      runId,
      label: describeLaunchLabel(request),
      status: "starting",
      pid: null,
      exitCode: null,
      startedAt: new Date().toISOString(),
      finishedAt: null,
      request,
      command: "",
      projectRoot,
      appliedOverrides: env,
      experimentId: options.experimentId ?? null,
      experimentCell: options.experimentCell ?? null,
      error: null,
    });
    this.runs.set(id, run);
    try {
      // Commit the identity before spawning the detached child. If VS Code
      // reloads in the launch gap, the next window can still account for it.
      await this.persist();
    } catch (error) {
      this.runs.delete(id);
      this.launchingRoots.delete(rootKey);
      this.changed.fire();
      throw error;
    }
    this.changed.fire();

    try {
      const child = spawnWorkflow(projectRoot, argv, env);
      run.pid = child.pid;
      run.status = "running";
      this.handles.set(id, { kill: child.kill });
      void child.onExit.then((code) => this.finish(id, code));
    } catch (error) {
      run.status = "failed";
      run.error = redactSensitiveText(
        error instanceof Error ? error.message : String(error),
        [request.prompt],
      );
      run.finishedAt = new Date().toISOString();
    }

    this.launchingRoots.delete(rootKey);

    try {
      await this.persist();
    } catch (error) {
      // The process is already live, so do not pretend the launch failed and
      // invite a duplicate. Surface the degraded reattachment guarantee on the
      // tracked run itself.
      run.error =
        "Run started, but VS Code could not persist its tracking record: " +
        redactSensitiveText(error instanceof Error ? error.message : String(error));
    }
    this.changed.fire();
    this.startPolling();
    return run;
  }

  /**
   * Stop a run, including one adopted after a window reload.
   *
   * A restored run has no in-memory handle, so its durable id is sent to the
   * CLI. The runtime revalidates the recorded launch token and process-group
   * identity before signalling; a reused pid is therefore left alone. A
   * refusal stays visible instead of being presented as a successful stop.
   */
  async stop(id: string): Promise<{ stopped: boolean; reason: string }> {
    const run = this.runs.get(id);
    if (!run) {
      return { stopped: false, reason: "Unknown run." };
    }

    const handle = this.handles.get(id);
    if (handle) {
      if (this.stopRequested.has(id)) {
        return { stopped: true, reason: "An interrupt has already been sent." };
      }
      if (!handle.kill()) {
        return { stopped: false, reason: "That process is no longer running." };
      }
      this.stopRequested.add(id);
      return { stopped: true, reason: "Interrupt sent; waiting for the process to exit." };
    }

    if (!run.runId) {
      return {
        stopped: false,
        reason:
          "This restored run has no durable run id, so LeanFlow cannot safely identify its process.",
      };
    }
    try {
      const outcome = await stopWorkflow(run.projectRoot, run.runId);
      if (outcome.stopped) {
        this.stopRequested.add(id);
        this.startPolling();
      }
      return outcome;
    } catch (error) {
      return {
        stopped: false,
        reason: error instanceof Error ? error.message : String(error),
      };
    }
  }

  stopAll(): void {
    for (const run of this.activeRuns()) {
      void this.stop(run.id);
    }
  }

  forget(id: string): void {
    const run = this.runs.get(id);
    this.runs.delete(id);
    this.handles.delete(id);
    this.stopRequested.delete(id);
    if (run) {
      this.statusesByRoot.delete(this.rootKey(run.projectRoot));
    }
    void this.persist();
    this.changed.fire();
  }

  private finish(id: string, exitCode: number | null): void {
    const run = this.runs.get(id);
    if (!run) {
      return;
    }
    this.handles.delete(id);
    this.statusesByRoot.delete(this.rootKey(run.projectRoot));
    if (run.status === "stopped" || this.stopRequested.delete(id)) {
      run.status = "stopped";
      run.exitCode = exitCode;
    } else {
      run.status = terminalTrackedRunStatus("", "", exitCode);
      run.exitCode = exitCode;
      if (run.status === "failed" && !run.error) {
        run.error = `leanflow exited with code ${exitCode ?? "unknown"}`;
      }
    }
    run.finishedAt = new Date().toISOString();
    void this.persist();
    this.changed.fire();
    if (this.activeRuns().length === 0) {
      // One last read so the final events and status land before polling stops.
      void this.refresh().then(() => this.stopPolling());
    }
  }

  // ---------------------------------------------------------------- polling

  startPolling(): void {
    // Continuous polling supersedes idle discovery until the run ends.
    this.stopIdleWatch();
    if (this.timer) {
      return;
    }
    const interval = vscode.workspace
      .getConfiguration("leanflow")
      .get<number>("pollIntervalMs", 5000);
    this.timer = setInterval(() => void this.poll(), Math.max(250, interval));
    void this.poll();
  }

  stopPolling(): void {
    this.clearPollTimer();
    this.startIdleWatch();
  }

  private clearPollTimer(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  // --------------------------------------------------------- idle discovery

  /**
   * Notice a run started or resumed outside this window while nothing is polled.
   *
   * Two bounded signals: a file watcher on the project's live-status snapshot,
   * and a slow timer that stats the same file and polls only when its
   * signature changed. Neither reads the file; `leanflow runs status` does.
   */
  private startIdleWatch(): void {
    if (this.disposed || !this.project.found || !this.project.stateRoot) {
      return;
    }
    // Restart so a changed interval setting takes effect on the next stop.
    this.clearIdleWatch();
    const interval = Math.max(
      5_000,
      vscode.workspace.getConfiguration("leanflow").get<number>("idlePollIntervalMs", 20_000),
    );
    this.idleTimer = setInterval(() => void this.idleProbe(false), interval);
    try {
      this.idleWatcher = vscode.workspace.createFileSystemWatcher(
        new vscode.RelativePattern(vscode.Uri.file(this.project.stateRoot), LIVE_STATUS_FILE),
      );
      const notify = () => {
        if (this.idleDebounce) {
          clearTimeout(this.idleDebounce);
        }
        this.idleDebounce = setTimeout(() => {
          this.idleDebounce = null;
          void this.idleProbe(true);
        }, WATCHER_DEBOUNCE_MS);
      };
      this.idleWatcher.onDidCreate(notify);
      this.idleWatcher.onDidChange(notify);
      this.idleWatcher.onDidDelete(notify);
    } catch {
      // A state directory outside every workspace folder may not be watchable
      // on this VS Code version; the timer alone still discovers the run.
      this.idleWatcher = null;
    }
    // Record the baseline now so only a later change triggers a read.
    void this.idleProbe(false);
  }

  private clearIdleWatch(): void {
    if (this.idleTimer) {
      clearInterval(this.idleTimer);
      this.idleTimer = null;
    }
    if (this.idleDebounce) {
      clearTimeout(this.idleDebounce);
      this.idleDebounce = null;
    }
    this.idleWatcher?.dispose();
    this.idleWatcher = null;
  }

  private stopIdleWatch(): void {
    this.clearIdleWatch();
  }

  private async liveStatusSignature(): Promise<FileSignature | null> {
    try {
      const stat = await fs.promises.stat(path.join(this.project.stateRoot, LIVE_STATUS_FILE));
      return stat.isFile() ? { mtimeMs: stat.mtimeMs, size: stat.size } : null;
    } catch {
      return null;
    }
  }

  private async idleProbe(fromWatcher: boolean): Promise<void> {
    if (this.disposed || this.timer || !this.project.found) {
      return;
    }
    const signature = await this.liveStatusSignature();
    const decision = idleDiscoveryDecision(this.idleState, signature, Date.now());
    this.idleState = decision.next;
    // The watcher names the exact file, so its notification is itself the
    // change; the timer only reads when the signature moved.
    if ((decision.poll || fromWatcher) && !this.disposed) {
      await this.poll();
    }
  }

  /** Read everything once, regardless of whether a run is active. */
  async refresh(): Promise<void> {
    await this.poll();
  }

  private async poll(): Promise<void> {
    if (this.polling || (!this.project.found && this.activeRuns().length === 0)) {
      return;
    }
    this.polling = true;
    try {
      const roots = new Map<string, string>();
      if (this.project.found) {
        roots.set(this.rootKey(this.project.root), this.project.root);
      }
      for (const run of this.activeRuns()) {
        roots.set(this.rootKey(run.projectRoot), run.projectRoot);
      }

      const snapshots = await Promise.all(
        [...roots.values()].map(async (projectRoot) => {
          const [statusResult, historyResult] = await Promise.allSettled([
            fetchLiveStatus(projectRoot),
            fetchRuns(projectRoot),
          ]);
          return {
            projectRoot,
            status: statusResult.status === "fulfilled" ? statusResult.value : null,
            history: historyResult.status === "fulfilled" ? historyResult.value : [],
            stateReadable:
              statusResult.status === "fulfilled" && historyResult.status === "fulfilled",
          };
        }),
      );

      this.historyRoots.clear();
      this.newestHistoryByRoot.clear();
      const mergedHistory = new Map<string, RunSummary>();
      for (const snapshot of snapshots) {
        const key = this.rootKey(snapshot.projectRoot);
        this.statusesByRoot.set(key, snapshot.status);
        if (sameProjectRoot(snapshot.projectRoot, this.project.root)) {
          this.liveStatus = snapshot.status;
        }
        if (snapshot.history[0]) {
          this.newestHistoryByRoot.set(key, snapshot.history[0]);
        }
        for (const rawSummary of snapshot.history) {
          const summary = runSummaryForDisplay(rawSummary);
          this.historyRoots.set(summary.run_id, snapshot.projectRoot);
          // Run ids are minted globally by the extension. Prefer the newest
          // copy if an imported project happens to contain the same stream.
          const existing = mergedHistory.get(summary.run_id);
          if (!existing || summary.updated_at > existing.updated_at) {
            mergedHistory.set(summary.run_id, summary);
          }
        }
        this.reconcileFromStatus(
          snapshot.status,
          snapshot.projectRoot,
          snapshot.history,
          snapshot.stateReadable,
        );
      }
      this.history = [...mergedHistory.values()].sort((left, right) =>
        right.updated_at.localeCompare(left.updated_at),
      );
      await this.pullEvents();
      this.changed.fire();
      const shouldKeepPolling =
        this.activeRuns().length > 0 ||
        snapshots.some((snapshot) => isActiveLiveStatus(snapshot.status));
      if (shouldKeepPolling) {
        this.startPolling();
      } else {
        this.stopPolling();
      }
    } finally {
      this.polling = false;
    }
  }

  private async pullEvents(): Promise<void> {
    const runTargets = new Map<string, string>();
    // Only active streams are followed continuously. Iterating every finished
    // experiment clone here turns a 1,000-cell sweep into 1,000 CLI processes
    // per poll. Finished runs are loaded on demand when the user selects them.
    for (const run of this.activeRuns()) {
      if (run.runId) {
        runTargets.set(run.runId, run.projectRoot || this.project.root);
      }
    }
    // Follow the newest external stream in every observed root too, so an
    // isolated experiment worktree remains readable after its child exits.
    for (const [rootKey, summary] of this.newestHistoryByRoot) {
      const projectRoot = this.historyRoots.get(summary.run_id);
      if (projectRoot) {
        runTargets.set(summary.run_id, projectRoot);
      } else {
        runTargets.set(summary.run_id, rootKey);
      }
    }

    for (const [runId, projectRoot] of runTargets) {
      await this.pullRunEvents(runId, projectRoot, true);
    }
  }

  private async pullRunEvents(
    runId: string,
    projectRoot: string,
    emit: boolean,
  ): Promise<void> {
    const bufferSize = vscode.workspace
      .getConfiguration("leanflow")
      .get<number>("eventBufferSize", 4000);
    const since = this.cursors.get(runId) ?? "";
    let payload: Awaited<ReturnType<typeof fetchEvents>>;
    try {
      payload = await fetchEvents(projectRoot, runId, since, bufferSize);
    } catch {
      return;
    }
    if (payload.events.length === 0) {
      return;
    }
    const existing = this.events.get(runId) ?? [];
    const merged = mergeActivityEvents(existing, payload.events, bufferSize);
    // Map insertion order is the LRU order used below.
    this.events.delete(runId);
    this.events.set(runId, merged.buffer);
    this.cursors.set(runId, payload.cursor);
    this.trimEventCache();
    if (emit && merged.appended.length > 0) {
      // Incremental pushes keep the webview cheap until the bounded host tail
      // evicts an old row. At that boundary send one replacement tail; without
      // it the React store would retain every event from a multi-hour campaign
      // even though the host correctly capped its own cache.
      const reset = existing.length === 0 || merged.trimmed;
      this.eventsAppended.fire({
        runId,
        events: reset ? merged.buffer : merged.appended,
        reset,
      });
    }
  }

  /** Load a selected terminal run without polling every historical clone. */
  async loadEvents(runId: string): Promise<ActivityEvent[]> {
    const tracked = [...this.runs.values()].find((run) => run.runId === runId);
    const root = tracked?.projectRoot ?? this.historyRoots.get(runId) ?? this.project.root;
    if (root) {
      await this.pullRunEvents(runId, root, false);
    }
    return this.eventsFor(runId);
  }

  private trimEventCache(): void {
    if (this.events.size <= MAX_CACHED_EVENT_RUNS) {
      return;
    }
    const active = new Set(this.activeRuns().map((run) => run.runId));
    for (const candidate of this.events.keys()) {
      if (this.events.size <= MAX_CACHED_EVENT_RUNS) {
        break;
      }
      if (active.has(candidate)) {
        continue;
      }
      this.events.delete(candidate);
      this.cursors.delete(candidate);
    }
  }

  /**
   * Mark a run finished when the runtime says its process is gone.
   *
   * The exit callback covers processes this window started. A run adopted after
   * a reload has no such callback, so the recorded status is the only signal
   * that it ended.
   */
  private reconcileFromStatus(
    status: LiveStatus | null,
    projectRoot: string,
    history: readonly RunSummary[],
    stateReadable: boolean,
  ): void {
    let changed = false;
    for (const run of this.runs.values()) {
      if (run.status !== "running" && run.status !== "starting") {
        continue;
      }
      if (!sameProjectRoot(run.projectRoot, projectRoot)) {
        continue;
      }
      if (this.handles.has(run.id)) {
        // This window owns the process; its exit callback is authoritative.
        continue;
      }
      const resolution = resolveRestoredRun(run, status, history, stateReadable);
      if (resolution.kind === "adopt") {
        if (run.pid !== resolution.processId || run.status === "starting") {
          run.pid = resolution.processId;
          run.status = "running";
          changed = true;
        }
      } else if (resolution.kind === "terminal") {
        run.status = this.stopRequested.delete(run.id)
          ? "stopped"
          : resolution.status;
        run.finishedAt = new Date().toISOString();
        changed = true;
      } else if (resolution.kind === "abandoned") {
        run.status = "failed";
        run.error = resolution.error;
        run.finishedAt = new Date().toISOString();
        changed = true;
      }
    }
    if (changed) {
      void this.persist();
    }
  }

  async loadTypes(): Promise<void> {
    if (!this.project.found) {
      return;
    }
    this.eventTypes = await fetchEventTypes(this.project.root).catch(() => []);
    this.changed.fire();
  }

  runLog(runId = "", tail = 400, projectRoot?: string): Promise<string> {
    const tracked = [...this.runs.values()].find((run) => run.runId === runId);
    const root =
      projectRoot ??
      tracked?.projectRoot ??
      this.historyRoots.get(runId) ??
      this.project.root;
    if (!root) {
      return Promise.resolve("");
    }
    return fetchRunLog(root, runId, tail);
  }

  // ------------------------------------------------------------ persistence

  /**
   * Re-adopt tracked runs after a window reload.
   *
   * The child processes are detached, so they survive the editor. Their exit
   * callbacks do not, which is why anything still marked running is downgraded
   * to be reconciled against recorded status on the next poll.
   */
  private restore(): void {
    const stored = this.context.workspaceState.get<TrackedRun[]>(RUNS_STORAGE_KEY, []);
    for (const run of stored) {
      const status: TrackedRunStatus =
        run.status === "starting" && run.pid !== null ? "running" : run.status;
      this.runs.set(
        run.id,
        trackedRunForPersistence({
          ...run,
          projectRoot: run.projectRoot || this.project.root,
          experimentId: run.experimentId ?? null,
          experimentCell: run.experimentCell ?? null,
          status,
        }),
      );
    }
    // An older extension may have stored the raw `--prompt` tail. Rewrite the
    // memento immediately instead of waiting for the next lifecycle update.
    if (stored.length > 0) {
      void this.persist();
    }
    if (this.activeRuns().length > 0) {
      this.startPolling();
    }
  }

  private persist(): Promise<void> {
    const snapshot = [...this.runs.values()].map(trackedRunForPersistence);
    const update = this.persistQueue.then(() =>
      this.context.workspaceState.update(RUNS_STORAGE_KEY, snapshot),
    );
    // Keep later writes moving after a transient Memento error, while returning
    // this update's rejection to the launch path that must fail closed.
    this.persistQueue = update.catch(() => undefined);
    return update;
  }

  private rootKey(projectRoot: string): string {
    const resolved = path.resolve(projectRoot);
    return process.platform === "win32" ? resolved.toLowerCase() : resolved;
  }
}
