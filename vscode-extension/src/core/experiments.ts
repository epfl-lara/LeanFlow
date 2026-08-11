/**
 * Run and score knob-ablation sweeps.
 *
 * A sweep is the cross product of targets, knob profiles, and models, executed
 * one cell at a time and scored from each run's immutable recorded state.
 *
 * Every cell receives a detached local clone of one required-clean Git
 * baseline. This prevents an earlier proof from becoming a hidden input to a
 * later condition. Cells remain sequential so machine/provider contention does
 * not become an uncontrolled experimental variable.
 */
import * as crypto from "node:crypto";
import * as path from "node:path";
import * as vscode from "vscode";

import { fetchRunHistory, fetchRunMetrics, previewLaunch } from "./cli";
import {
  captureExperimentBaseline,
  createIsolatedCellProject,
  deleteExperimentClones,
  experimentStorageKey,
  resolveExistingExperimentMatrixTargets,
  validateIsolatedLaunchInputs,
} from "./experimentIsolation";
import {
  assertExperimentDeletable,
  assertExperimentIdAvailable,
  canonicalExperimentTarget,
  experimentTargets,
  isComparisonEligibleCell,
  isSealedExperimentSkillReference,
  isRunnableExperimentCellStatus,
  normalizeExperimentMatrixTargets,
  validateExplicitExperimentRuntime,
  validateExperimentMatrixSize,
  validateRestoredExperimentCells,
} from "./experimentMatrix";
import {
  freezeExperimentLaunchContract,
  isExperimentLaunchContract,
  runMetricsMatchLaunchContract,
  runMetricsMatchRequestedTarget,
} from "./experimentLaunchContract";
import {
  compactExperimentMetrics,
  compareTargetSelectedSkills,
  experimentCommandForExport,
  experimentLaunchInvariant,
  freezeExperimentRuntimeIdentity,
  isExperimentLaunchInvariant,
  isExperimentRuntimeIdentity,
  metricsFromRun,
  runMetricsMatchExplicitPrompt,
  runMetricsMatchExplicitRuntime,
  sameExperimentLaunchInvariant,
  selectedSkillsTargetKey,
  unscoredMetrics,
} from "./experimentScoring";
import {
  deleteExperimentPrompt,
  readExperimentPrompt,
  scrubExperimentMatrixPrompt,
  storeExperimentPrompt,
} from "./experimentSecrets";
import { auditSourceRunOverlap } from "./experimentValidity";
import {
  buildWorkflowArgs,
  rejectedProfileKnobNames,
  resolveLaunchEnv,
  validateLaunch,
} from "./launch";
import type { RunManager } from "./runManager";
import { writeProjectStorageFile } from "./storageSecurity";
import type {
  ExperimentCell,
  FlagCatalog,
  ExperimentMatrix,
  ExperimentMetrics,
  ExperimentRun,
  ExperimentRuntimeIdentity,
  LaunchRequest,
  ProfileCatalog,
  RunHistorySnapshot,
  RunMetrics,
  TrackedRun,
} from "./types";

const EXPERIMENTS_STORAGE_KEY = "leanflow.experiments";

function seededRandom(seed: string): () => number {
  let state = crypto.createHash("sha256").update(seed).digest().readUInt32LE(0) || 1;
  return () => {
    state ^= state << 13;
    state ^= state >>> 17;
    state ^= state << 5;
    return (state >>> 0) / 0x1_0000_0000;
  };
}

export function expandMatrix(matrix: ExperimentMatrix): ExperimentCell[] {
  validateExperimentMatrixSize(matrix);
  const cells: ExperimentCell[] = [];
  const targets = experimentTargets(matrix);
  const profiles = matrix.profiles.length > 0 ? matrix.profiles : [matrix.base.profile];
  const models = matrix.models.length > 0 ? matrix.models : [matrix.base.model];
  const repeats = matrix.repeats;

  for (const target of targets) {
    for (const profile of profiles) {
      for (const model of models) {
        for (let repeat = 1; repeat <= repeats; repeat += 1) {
          cells.push({
            id: `${matrix.id}:cell-${cells.length + 1}`,
            matrixId: matrix.id,
            target,
            targetIdentity: target,
            profile,
            model,
            repeat,
            plannedOrder: 0,
            executionOrder: null,
            status: "pending",
            runId: null,
            trackedRunId: null,
            startedAt: null,
            finishedAt: null,
            projectRoot: null,
            expectedRuntime: null,
            expectedLaunchContract: null,
            metrics: null,
            error: null,
          });
        }
      }
    }
  }
  // Randomize once, deterministically from the persisted seed. This avoids
  // making one profile systematically earlier (and warmer) than another while
  // keeping the exact planned order reproducible after reload.
  const random = seededRandom(matrix.randomizationSeed ?? matrix.id);
  for (let index = cells.length - 1; index > 0; index -= 1) {
    const swap = Math.floor(random() * (index + 1));
    [cells[index], cells[swap]] = [cells[swap], cells[index]];
  }
  return cells.map((cell, index) => ({ ...cell, plannedOrder: index + 1 }));
}

function cellRequest(
  matrix: ExperimentMatrix,
  cell: ExperimentCell,
  prompt: string,
): LaunchRequest {
  return {
    ...matrix.base,
    kind: matrix.kind,
    target: cell.target,
    profile: cell.profile,
    model: cell.model || matrix.base.model,
    prompt,
  };
}

function validateExperimentDefinition(matrix: ExperimentMatrix): number {
  const cellCount = validateExperimentMatrixSize(matrix);
  validateExplicitExperimentRuntime(matrix);
  if (matrix.kind !== matrix.base.kind) {
    throw new Error("Experiment workflow kind must match its frozen Launch base.");
  }
  const unsealedSkills = matrix.base.additionalSkills.filter(
    (skill) => !isSealedExperimentSkillReference(skill),
  );
  if (unsealedSkills.length > 0) {
    throw new Error(
      "Research sweeps require additional skills to be tracked project-relative paths; " +
        `bare resolver names are not content-sealed: ${unsealedSkills.join(", ")}`,
    );
  }
  const targets = experimentTargets(matrix);
  for (const target of targets) {
    const problems = validateLaunch({ ...matrix.base, kind: matrix.kind, target });
    if (problems.length > 0) {
      throw new Error(`Invalid frozen Launch base: ${problems.join(" ")}`);
    }
  }
  return cellCount;
}

export class ExperimentEngine implements vscode.Disposable {
  private experiments = new Map<string, ExperimentRun>();
  private cancelled = new Set<string>();
  private contaminatedRuns = new Set<string>();
  private running: string | null = null;
  private persistQueue: Promise<void> = Promise.resolve();
  private readonly ready: Promise<void>;

  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changed.event;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly runs: RunManager,
    private readonly getProfiles: () => ProfileCatalog | null,
    private readonly getCatalog: () => FlagCatalog | null,
  ) {
    const legacyPrompts: { matrixId: string; prompt: string }[] = [];
    for (const experiment of context.workspaceState.get<ExperimentRun[]>(
      EXPERIMENTS_STORAGE_KEY,
      [],
    )) {
      const protectedPrompt = scrubExperimentMatrixPrompt(experiment.matrix);
      const rawPrompt = protectedPrompt.prompt;
      const legacyEnvelope = protectedPrompt.envelope;
      const promptPresent = legacyEnvelope.promptPresent
        ? true
        : experiment.promptPresent === true;
      const promptSha256 = legacyEnvelope.promptPresent
        ? legacyEnvelope.promptSha256
        : promptPresent
          ? (experiment.promptSha256 ?? null)
          : null;
      const promptMetadataValid = promptPresent
        ? typeof promptSha256 === "string" && /^[0-9a-f]{64}$/.test(promptSha256)
        : promptSha256 === null;
      let scrubbedMatrix = protectedPrompt.matrix;
      let restoredCells: ExperimentCell[] = [];
      if (rawPrompt) {
        legacyPrompts.push({ matrixId: experiment.matrix.id, prompt: rawPrompt });
      }
      try {
        scrubbedMatrix = normalizeExperimentMatrixTargets(scrubbedMatrix);
        restoredCells = experiment.cells.map((cell) => ({
          ...cell,
          target: canonicalExperimentTarget(cell.target),
          targetIdentity:
            typeof cell.targetIdentity === "string"
              ? canonicalExperimentTarget(cell.targetIdentity)
              : null,
        }));
        const expectedCells = validateExperimentDefinition(scrubbedMatrix);
        if (!Array.isArray(experiment.cells) || experiment.cells.length !== expectedCells) {
          throw new Error(
            `Persisted experiment contains ${Array.isArray(experiment.cells) ? experiment.cells.length : "invalid"} ` +
              `cells, but its matrix defines ${expectedCells}.`,
          );
        }
        validateRestoredExperimentCells(scrubbedMatrix, restoredCells);
      } catch (error) {
        const matrix: ExperimentMatrix = {
          ...scrubbedMatrix,
          targets: [],
          profiles: [],
          models: [],
          repeats: 1,
          randomizationSeed: experiment.matrix.id,
          base: { ...scrubbedMatrix.base, target: "" },
        };
        this.experiments.set(matrix.id, {
          matrix,
          cells: [],
          promptPresent,
          promptSha256,
          baseline: null,
          launchSourceIdentitySha256: null,
          launchInvariant: null,
          selectedSkillsSha256ByTarget: {},
          profileSnapshot: {},
          status: "blocked",
          error:
            "Refusing invalid persisted experiment: " +
            (error instanceof Error ? error.message : String(error)),
        });
        continue;
      }
      // A cell that was mid-flight keeps its "running" status only when both
      // its minted run id and isolated project survive. A pre-isolation record
      // is blocked rather than paid for again or silently trusted.
      const launchInvariant = isExperimentLaunchInvariant(experiment.launchInvariant)
        ? { ...experiment.launchInvariant }
        : null;
      const selectedSkillsSha256ByTarget: Record<string, string> = {};
      const persistedSkillIdentities = experiment.selectedSkillsSha256ByTarget ?? {};
      const rawTargets =
        experiment.matrix.targets.length > 0
          ? experiment.matrix.targets
          : [experiment.matrix.base.target];
      for (const rawTarget of rawTargets) {
        const canonicalKey = selectedSkillsTargetKey(rawTarget);
        const oldRawKey = JSON.stringify([rawTarget]);
        const value = persistedSkillIdentities[canonicalKey] ?? persistedSkillIdentities[oldRawKey];
        if (!value || !/^[0-9a-f]{64}$/.test(value)) {
          continue;
        }
        const previous = selectedSkillsSha256ByTarget[canonicalKey];
        if (previous === undefined || previous === value) {
          selectedSkillsSha256ByTarget[canonicalKey] = value;
        } else {
          delete selectedSkillsSha256ByTarget[canonicalKey];
        }
      }
      let unsafeRestoredCell = false;
      const cells = restoredCells.map((cell, index) => {
        let migrated = {
          ...cell,
          plannedOrder: cell.plannedOrder ?? index + 1,
          executionOrder: cell.executionOrder ?? null,
          projectRoot: cell.projectRoot ?? null,
          expectedRuntime: isExperimentRuntimeIdentity(cell.expectedRuntime)
            ? { ...cell.expectedRuntime }
            : null,
          expectedLaunchContract: isExperimentLaunchContract(cell.expectedLaunchContract)
            ? {
                ...cell.expectedLaunchContract,
                workflow: { ...cell.expectedLaunchContract.workflow },
                set: { ...cell.expectedLaunchContract.set },
                unset: [...cell.expectedLaunchContract.unset],
                falseOrAbsent: [...cell.expectedLaunchContract.falseOrAbsent],
                emptyOrAbsent: [...cell.expectedLaunchContract.emptyOrAbsent],
                additionalSkills: cell.expectedLaunchContract.additionalSkills.map(
                  (identity) => ({ ...identity }),
                ),
              }
            : null,
        };
        if (
          migrated.targetIdentity === null &&
          (migrated.status === "done" || migrated.status === "failed")
        ) {
          const missing = unscoredMetrics(
            "This row predates filesystem-canonical target identity and cannot enter a research comparison.",
          );
          migrated = {
            ...migrated,
            status: "unscored" as const,
            error: missing.missingReason,
            metrics: missing,
          };
        }
        if (migrated.metrics !== null) {
          const compact = compactExperimentMetrics(migrated.metrics);
          if (
            compact.scopeExact !== true &&
            (migrated.status === "done" || migrated.status === "failed")
          ) {
            migrated = {
              ...migrated,
              status: "unscored" as const,
              error: compact.missingReason,
              metrics: compact,
            };
          } else {
            migrated = { ...migrated, metrics: compact };
          }
        }
        if (
          migrated.expectedRuntime === null &&
          (migrated.status === "done" || migrated.status === "failed")
        ) {
          const missing = unscoredMetrics(
            "This row predates preview-frozen runtime identity and cannot enter a research comparison.",
          );
          migrated = {
            ...migrated,
            status: "unscored" as const,
            error: missing.missingReason,
            metrics: missing,
          };
        }
        if (
          migrated.expectedLaunchContract === null &&
          (migrated.status === "done" || migrated.status === "failed")
        ) {
          const missing = unscoredMetrics(
            "This row predates the frozen per-cell launch contract and cannot enter a research comparison.",
          );
          migrated = {
            ...migrated,
            status: "unscored" as const,
            error: missing.missingReason,
            metrics: missing,
          };
        }
        if (
          (migrated.status === "done" || migrated.status === "failed") &&
          (launchInvariant === null ||
            !selectedSkillsSha256ByTarget[
              selectedSkillsTargetKey(migrated.targetIdentity ?? migrated.target)
            ])
        ) {
          const missing = unscoredMetrics(
            "This row predates component-wise launch invariants and cannot enter a research comparison.",
          );
          migrated = {
            ...migrated,
            status: "unscored" as const,
            error: missing.missingReason,
            metrics: missing,
          };
        }
        if (migrated.status === "running" && migrated.projectRoot && !migrated.trackedRunId) {
          // RunManager persists its launch record independently. If the window
          // closed in the tiny post-spawn/pre-cell-persist gap, recover only an
          // unambiguous record carrying this exact experiment/cell/root tuple.
          const matches = this.runs.snapshot().runs.filter(
            (run) =>
              run.experimentId === experiment.matrix.id &&
              run.experimentCell === migrated.id &&
              path.resolve(run.projectRoot) === path.resolve(migrated.projectRoot as string),
          );
          if (matches.length === 1) {
            migrated = {
              ...migrated,
              trackedRunId: matches[0].id,
              runId: matches[0].runId || migrated.runId,
            };
          }
        }
        if (
          migrated.status === "running" &&
          (!migrated.trackedRunId || !migrated.projectRoot || !migrated.expectedRuntime)
        ) {
          unsafeRestoredCell = true;
          return {
            ...migrated,
            status: "blocked" as const,
            error:
              "This pre-isolation cell cannot be re-attached reproducibly. Start a new sweep.",
          };
        }
        if (migrated.status === "running" && migrated.expectedLaunchContract === null) {
          unsafeRestoredCell = true;
          return {
            ...migrated,
            status: "blocked" as const,
            error:
              "This running cell predates the frozen launch contract. Start a new sweep.",
          };
        }
        return migrated;
      });
      const hasUnsafeRestoredCell = unsafeRestoredCell;
      this.experiments.set(experiment.matrix.id, {
        ...experiment,
        matrix: {
          ...scrubbedMatrix,
          randomizationSeed: experiment.matrix.randomizationSeed ?? experiment.matrix.id,
        },
        cells,
        promptPresent,
        promptSha256,
        baseline: experiment.baseline ?? null,
        launchSourceIdentitySha256: experiment.launchSourceIdentitySha256 ?? null,
        launchInvariant,
        selectedSkillsSha256ByTarget,
        profileSnapshot: experiment.profileSnapshot ?? {},
        status: !promptMetadataValid
          ? "blocked"
          : hasUnsafeRestoredCell
          ? "blocked"
          : experiment.status === "running"
            ? "paused"
            : experiment.status,
        error: !promptMetadataValid
          ? "Refusing experiment with invalid encrypted-prompt metadata. Create a new sweep."
          : hasUnsafeRestoredCell
          ? "A restored cell predates isolated experiment execution. Start a new sweep."
          : (experiment.error ?? null),
      });
    }
    this.ready = this.migrateLegacyPrompts(legacyPrompts);
    void this.ready.catch((error) =>
      vscode.window.showErrorMessage(
        `LeanFlow could not securely migrate stored experiment prompts: ${
          error instanceof Error ? error.message : String(error)
        }`,
      ),
    );
  }

  dispose(): void {
    this.changed.dispose();
  }

  /** The sweep currently executing, if any. */
  activeMatrixId(): string | null {
    return this.running;
  }

  list(): ExperimentRun[] {
    return [...this.experiments.values()]
      .sort((a, b) => b.matrix.createdAt.localeCompare(a.matrix.createdAt))
      .map((experiment) => ({
        ...experiment,
        matrix: {
          ...experiment.matrix,
          targets: [...experiment.matrix.targets],
          profiles: [...experiment.matrix.profiles],
          models: [...experiment.matrix.models],
          base: {
            ...experiment.matrix.base,
            prompt: "",
            additionalSkills: [...experiment.matrix.base.additionalSkills],
            overrides: { ...experiment.matrix.base.overrides },
          },
        },
        baseline: experiment.baseline ? { ...experiment.baseline } : null,
        launchInvariant: experiment.launchInvariant
          ? { ...experiment.launchInvariant }
          : null,
        selectedSkillsSha256ByTarget: {
          ...experiment.selectedSkillsSha256ByTarget,
        },
        profileSnapshot: Object.fromEntries(
          Object.entries(experiment.profileSnapshot).map(([name, overrides]) => [
            name,
            { ...overrides },
          ]),
        ),
        cells: experiment.cells.map((cell) => ({
          ...cell,
          expectedRuntime: cell.expectedRuntime ? { ...cell.expectedRuntime } : null,
          expectedLaunchContract: cell.expectedLaunchContract
            ? {
                ...cell.expectedLaunchContract,
                workflow: { ...cell.expectedLaunchContract.workflow },
                set: { ...cell.expectedLaunchContract.set },
                unset: [...cell.expectedLaunchContract.unset],
                falseOrAbsent: [...cell.expectedLaunchContract.falseOrAbsent],
                emptyOrAbsent: [...cell.expectedLaunchContract.emptyOrAbsent],
                additionalSkills: cell.expectedLaunchContract.additionalSkills.map(
                  (identity) => ({ ...identity }),
                ),
              }
            : null,
          error: cell.error?.slice(0, 4_000) ?? null,
          metrics: cell.metrics ? compactExperimentMetrics(cell.metrics) : null,
        })),
        error: experiment.error?.slice(0, 4_000) ?? null,
      }));
  }

  /** Return how many new provider-backed runs a start action would launch. */
  pendingCellCount(matrixId: string): number {
    return (
      this.experiments
        .get(matrixId)
        ?.cells.filter((cell) => cell.status === "pending").length ?? 0
    );
  }

  async create(matrix: ExperimentMatrix): Promise<ExperimentRun> {
    await this.ready;
    assertExperimentIdAvailable(this.experiments.has(matrix.id), matrix.id);
    const lexical = normalizeExperimentMatrixTargets({
      ...matrix,
      targets: [...matrix.targets],
      profiles: [...matrix.profiles],
      models: [...matrix.models],
      base: {
        ...matrix.base,
        additionalSkills: [...matrix.base.additionalSkills],
        overrides: { ...matrix.base.overrides },
      },
      randomizationSeed: matrix.randomizationSeed ?? crypto.randomUUID(),
    });
    const { matrix: normalized } = await resolveExistingExperimentMatrixTargets(
      this.runs.projectRoot(),
      lexical,
    );
    validateExperimentDefinition(normalized);
    const profileSnapshot = this.freezeProfiles(normalized);
    const protectedPrompt = scrubExperimentMatrixPrompt(normalized);
    const prompt = await storeExperimentPrompt(
      this.context.secrets,
      normalized.id,
      protectedPrompt.prompt,
    );
    const scrubbed = protectedPrompt.matrix;
    const experiment: ExperimentRun = {
      matrix: scrubbed,
      cells: expandMatrix(scrubbed),
      ...prompt,
      baseline: null,
      launchSourceIdentitySha256: null,
      launchInvariant: null,
      selectedSkillsSha256ByTarget: {},
      profileSnapshot,
      status: "idle",
      error: null,
    };
    this.experiments.set(matrix.id, experiment);
    try {
      await this.persistAuthority("the new prompt-safe sweep metadata");
    } catch (error) {
      this.experiments.delete(matrix.id);
      await deleteExperimentPrompt(this.context.secrets, matrix.id).catch(() => undefined);
      throw error;
    }
    this.changed.fire();
    return experiment;
  }

  async delete(matrixId: string): Promise<void> {
    await this.ready;
    const experiment = this.experiments.get(matrixId);
    if (!experiment) {
      throw new Error("Unknown experiment.");
    }
    const trackedProcessIsLive = this.runs
      .activeRuns()
      .some((run) => run.experimentId === matrixId);
    assertExperimentDeletable(
      this.running === matrixId || trackedProcessIsLive,
      experiment.cells,
    );

    // Generated clones can be many gigabytes. Remove only this matrix's hashed
    // app-owned directory; a live cell is rejected above, so no process can be
    // writing there. The source checkout is never a deletion target.
    await deleteExperimentClones(this.context.globalStorageUri.fsPath, matrixId);
    await deleteExperimentPrompt(this.context.secrets, matrixId);
    this.experiments.delete(matrixId);
    await this.persist();
    this.changed.fire();
  }

  stop(matrixId: string): void {
    const experiment = this.experiments.get(matrixId);
    if (!experiment) {
      throw new Error("Unknown experiment.");
    }
    this.cancelled.add(matrixId);
    experiment.status = "paused";
    for (const cell of experiment.cells) {
      if (cell.status === "running" && cell.trackedRunId) {
        const trackedRunId = cell.trackedRunId;
        void this.runs
          .stop(trackedRunId)
          .then(async (outcome) => {
            if (outcome.stopped && this.running !== matrixId && cell.status === "running") {
              await this.awaitRun(trackedRunId, matrixId);
              if (cell.status === "running") {
                cell.status = "skipped";
                cell.finishedAt = new Date().toISOString();
                cell.error = "Stopped before the sweep was re-attached.";
                await this.persist();
                this.changed.fire();
              }
            } else if (!outcome.stopped && outcome.reason) {
              cell.error = outcome.reason;
              await this.persist();
              this.changed.fire();
            }
          })
          .catch((error) => {
            cell.error = error instanceof Error ? error.message : String(error);
            void this.persist().catch(() => undefined);
            this.changed.fire();
          });
      }
    }
    void this.persist().catch(() => undefined);
    this.changed.fire();
  }

  /**
   * Execute every pending cell in order.
   *
   * Only pending cells and a persisted running cell are eligible. Every
   * terminal row is immutable, so Resume cannot overwrite paid-for evidence.
   */
  async start(matrixId: string): Promise<void> {
    await this.ready;
    const experiment = this.experiments.get(matrixId);
    if (!experiment) {
      throw new Error("Unknown experiment.");
    }
    if (this.running) {
      throw new Error(
        "Another sweep is already running. Sweeps run one at a time to keep " +
          "experiment cells resource-comparable.",
      );
    }
    if (
      !experiment.cells.some((cell) => isRunnableExperimentCellStatus(cell.status))
    ) {
      throw new Error(
        "This experiment has no pending cells. Terminal rows are immutable; create a new " +
          "sweep to retry a failed, blocked, or unscored condition.",
      );
    }
    this.running = matrixId;
    this.cancelled.delete(matrixId);
    experiment.status = "running";
    experiment.error = null;
    this.changed.fire();

    const settle = vscode.workspace
      .getConfiguration("leanflow")
      .get<number>("cellSettleMs", 3000);

    try {
      await this.persistAuthority("the experiment start state");
      await this.refreshFilesystemTargetIdentities(experiment);
      this.validateFrozenKnobs(experiment);
      await this.requireExclusiveExecution(experiment.matrix.id);
      const requiredProfiles = new Set(experiment.matrix.profiles);
      if (experiment.matrix.base.profile) {
        requiredProfiles.add(experiment.matrix.base.profile);
      }
      if ([...requiredProfiles].some((name) => !(name in experiment.profileSnapshot))) {
        // Migration path for an idle sweep created before profile snapshots
        // were persisted. New sweeps freeze them in create().
        experiment.profileSnapshot = this.freezeProfiles(experiment.matrix);
      }
      await this.freezeRuntimeIdentities(experiment);
      if (experiment.baseline === null) {
        experiment.baseline = await captureExperimentBaseline(this.runs.projectRoot());
        await this.persistAuthority("the immutable experiment baseline");
        this.changed.fire();
      }
      for (const cell of experiment.cells) {
        if (this.cancelled.has(matrixId)) {
          break;
        }
        if (!isRunnableExperimentCellStatus(cell.status)) {
          continue;
        }
        if (cell.status === "running" && cell.trackedRunId) {
          // Restored mid-flight: wait for the run this cell already owns.
          await this.resumeCell(cell, settle);
          continue;
        }
        await this.runCell(experiment, cell, settle);
      }
      experiment.status = this.cancelled.has(matrixId) ? "paused" : "done";
    } catch (error) {
      experiment.status = "blocked";
      experiment.error = error instanceof Error ? error.message : String(error);
      await this.persist().catch(() => undefined);
      this.changed.fire();
      throw error;
    } finally {
      this.running = null;
      await this.persistAuthority("the terminal experiment state");
      this.changed.fire();
    }
  }

  /**
   * Finish a cell whose run survived a window reload.
   *
   * The process was detached, so it kept going; re-attaching costs nothing and
   * avoids re-running work that has already been paid for.
   */
  private async resumeCell(cell: ExperimentCell, settleMs: number): Promise<void> {
    if (!cell.trackedRunId || !cell.projectRoot) {
      return;
    }
    await this.awaitRun(cell.trackedRunId, cell.matrixId);
    const finished = this.runs.getRun(cell.trackedRunId);
    cell.runId = finished?.runId ?? cell.runId;
    cell.finishedAt = new Date().toISOString();
    if (this.contaminatedRuns.delete(cell.trackedRunId)) {
      cell.metrics = unscoredMetrics(
        "A source-checkout workflow overlapped this cell's execution window.",
      );
      cell.status = "blocked";
      cell.error =
        "This restored cell overlapped a workflow in the source checkout and was invalidated.";
      await this.persistAuthority("the invalidated restored cell");
      this.changed.fire();
      return;
    }
    if (settleMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, settleMs));
    }
    cell.metrics = await this.scoreCell(cell, cell.projectRoot);
    cell.status =
      cell.metrics.phase === "unscored"
        ? "unscored"
        : finished?.status === "failed"
        ? "failed"
        : finished?.status === "stopped"
          ? "skipped"
          : "done";
    if (cell.status === "unscored") {
      cell.error = cell.metrics.missingReason;
    }
    await this.persistAuthority("the restored cell result");
    this.changed.fire();
  }

  private async runCell(
    experiment: ExperimentRun,
    cell: ExperimentCell,
    settleMs: number,
  ): Promise<void> {
    await this.requireExclusiveExecution(experiment.matrix.id);
    const prompt = await this.requireExperimentPrompt(experiment);
    cell.status = "running";
    cell.executionOrder =
      Math.max(0, ...experiment.cells.map((entry) => entry.executionOrder ?? 0)) + 1;
    cell.startedAt = new Date().toISOString();
    cell.finishedAt = null;
    cell.runId = null;
    cell.trackedRunId = null;
    cell.metrics = null;
    cell.error = null;
    await this.persistAuthority("the cell execution order");
    this.changed.fire();

    if (this.cancelled.has(experiment.matrix.id)) {
      cell.status = "skipped";
      cell.finishedAt = new Date().toISOString();
      await this.persistAuthority("the pre-launch cancellation");
      this.changed.fire();
      return;
    }

    if (experiment.baseline === null) {
      throw new Error("The experiment has no immutable baseline.");
    }
    try {
      // VS Code owns this root; the isolation helper then creates and checks
      // every descendant one component at a time without following links.
      await vscode.workspace.fs.createDirectory(this.context.globalStorageUri);
      const storageRoot = path.join(
        this.context.globalStorageUri.fsPath,
        "experiment-clones",
        experimentStorageKey(experiment.matrix.id),
      );
      cell.projectRoot = await createIsolatedCellProject(
        experiment.baseline,
        this.runs.projectRoot(),
        storageRoot,
        `${cell.id}:${cell.executionOrder}`,
      );
      await this.persistAuthority("the isolated cell root");
      this.changed.fire();
      if (this.cancelled.has(experiment.matrix.id)) {
        cell.status = "skipped";
        cell.finishedAt = new Date().toISOString();
        await this.persistAuthority("the post-clone cancellation");
        this.changed.fire();
        return;
      }
    } catch (error) {
      cell.status = "blocked";
      cell.error =
        "Reproducible isolation failed: " +
        (error instanceof Error ? error.message : String(error));
      cell.finishedAt = new Date().toISOString();
      await this.persist().catch(() => undefined);
      this.changed.fire();
      throw error;
    }

    let tracked: TrackedRun;
    try {
      const request = cellRequest(experiment.matrix, cell, prompt);
      await validateIsolatedLaunchInputs(
        cell.projectRoot,
        request.target,
        request.additionalSkills,
      );
      if (!cell.expectedRuntime) {
        throw new Error("This cell has no preview-frozen runtime identity.");
      }
      const frozenProfiles = this.frozenProfileCatalog(experiment);
      if (this.cancelled.has(experiment.matrix.id)) {
        cell.status = "skipped";
        cell.finishedAt = new Date().toISOString();
        await this.persistAuthority("the immediate pre-launch cancellation");
        this.changed.fire();
        return;
      }
      tracked = await this.runs.launch(
        request,
        frozenProfiles,
        this.getCatalog(),
        {
          experimentId: experiment.matrix.id,
          experimentCell: cell.id,
          projectRoot: cell.projectRoot,
        },
      );
    } catch (error) {
      cell.status = "blocked";
      cell.error = error instanceof Error ? error.message : String(error);
      cell.finishedAt = new Date().toISOString();
      await this.persist().catch(() => undefined);
      this.changed.fire();
      return;
    }

    cell.trackedRunId = tracked.id;
    try {
      await this.persistAuthority("the launched cell's durable run id");
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      await this.runs.stop(tracked.id);
      await this.awaitRun(tracked.id, experiment.matrix.id);
      cell.status = "blocked";
      cell.finishedAt = new Date().toISOString();
      cell.error = `${reason} The launched run was interrupted before another cell could start.`;
      await this.persist().catch(() => undefined);
      this.changed.fire();
      throw error;
    }
    this.changed.fire();

    if (this.cancelled.has(experiment.matrix.id)) {
      void this.runs.stop(tracked.id);
    }

    await this.awaitRun(tracked.id, experiment.matrix.id);

    const finished = this.runs.getRun(tracked.id);
    cell.runId = finished?.runId ?? null;
    cell.finishedAt = new Date().toISOString();

    // Settle BEFORE scoring, not after. The run's exit fires listeners before
    // its final state is flushed, so reading immediately can capture the
    // previous run's numbers. A later pause would have protected the next cell
    // and left this one's metrics wrong — which is the failure that matters.
    if (this.contaminatedRuns.delete(tracked.id)) {
      cell.metrics = unscoredMetrics(
        "A source-checkout workflow overlapped this cell's execution window.",
      );
      cell.status = "blocked";
      cell.error =
        "This cell overlapped a workflow in the source checkout and was invalidated before scoring.";
      await this.persistAuthority("the invalidated cell result");
      this.changed.fire();
      return;
    }
    if (settleMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, settleMs));
    }
    cell.metrics = await this.scoreCell(cell, cell.projectRoot);
    if (cell.metrics.phase === "unscored") {
      cell.status = "unscored";
      cell.error = cell.metrics.missingReason;
    } else if (finished?.status === "failed") {
      cell.status = "failed";
      cell.error = finished.error;
    } else if (finished?.status === "stopped") {
      cell.status = "skipped";
    } else {
      cell.status = "done";
    }
    await this.persistAuthority("the terminal cell evidence");
    this.changed.fire();
  }

  private awaitRun(trackedRunId: string, matrixId: string): Promise<void> {
    return new Promise((resolve) => {
      const check = () => {
        const run = this.runs.getRun(trackedRunId);
        if (!run || (run.status !== "running" && run.status !== "starting")) {
          subscription.dispose();
          clearInterval(poll);
          resolve();
          return;
        }
        if (this.cancelled.has(matrixId)) {
          void this.runs.stop(trackedRunId);
        }
        const originalOwner = this.runs.launchBlocker(this.runs.projectRoot());
        if (originalOwner !== null && run?.projectRoot !== this.runs.projectRoot()) {
          this.contaminatedRuns.add(trackedRunId);
          void this.runs.stop(trackedRunId);
        }
      };
      const subscription = this.runs.onDidChange(check);
      // The change event covers normal transitions; the interval is a backstop
      // for a run that ends without one (an adopted run after a reload).
      const poll = setInterval(check, 2000);
      check();
    });
  }

  /** Refuse a new cell while another observable workflow can confound it. */
  private async requireExclusiveExecution(matrixId: string): Promise<void> {
    await this.runs.refresh();
    const originalOwner = this.runs.launchBlocker(this.runs.projectRoot());
    if (originalOwner !== null) {
      throw new Error(
        `${originalOwner} Research sweeps require exclusive machine/workflow ownership.`,
      );
    }
    const unrelated = this.runs.activeRuns().find((run) => run.experimentId !== matrixId);
    if (unrelated) {
      throw new Error(
        `${unrelated.label} is still running. Research cells require exclusive execution.`,
      );
    }
  }

  /** Move legacy plaintext prompts into SecretStorage before any experiment action. */
  private async migrateLegacyPrompts(
    legacyPrompts: readonly { matrixId: string; prompt: string }[],
  ): Promise<void> {
    if (legacyPrompts.length === 0) {
      return;
    }
    const failures: string[] = [];
    for (const legacy of legacyPrompts) {
      const experiment = this.experiments.get(legacy.matrixId);
      if (!experiment) {
        continue;
      }
      try {
        const envelope = await storeExperimentPrompt(
          this.context.secrets,
          legacy.matrixId,
          legacy.prompt,
        );
        experiment.promptPresent = envelope.promptPresent;
        experiment.promptSha256 = envelope.promptSha256;
      } catch (error) {
        await deleteExperimentPrompt(this.context.secrets, legacy.matrixId).catch(
          () => undefined,
        );
        experiment.status = "blocked";
        experiment.error =
          "Secure migration of the legacy experiment prompt failed; no cell may launch. " +
          (error instanceof Error ? error.message : String(error));
        failures.push(legacy.matrixId);
      }
    }
    // Persist the already-scrubbed in-memory matrices even when SecretStorage
    // failed, so retrying the extension never re-emits plaintext to webviews.
    await this.persistAuthority("the secure legacy prompt migration");
    this.changed.fire();
    if (failures.length > 0) {
      void vscode.window.showErrorMessage(
        `Secure prompt migration failed for ${failures.length} experiment(s); ` +
          "they were scrubbed and blocked.",
      );
    }
  }

  /** Recover and verify the frozen prompt immediately before a new cell boundary. */
  private async requireExperimentPrompt(experiment: ExperimentRun): Promise<string> {
    try {
      return await readExperimentPrompt(this.context.secrets, experiment.matrix.id, {
        promptPresent: experiment.promptPresent,
        promptSha256: experiment.promptSha256,
      });
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      experiment.status = "blocked";
      experiment.error = `Experiment prompt verification failed: ${reason}`;
      await this.persist().catch(() => undefined);
      this.changed.fire();
      throw new Error(experiment.error);
    }
  }

  /** Build the immutable profile catalog used by previews and real cells. */
  private frozenProfileCatalog(experiment: ExperimentRun): ProfileCatalog {
    return {
      version: 1,
      count: Object.keys(experiment.profileSnapshot).length,
      search_paths: [],
      profiles: Object.entries(experiment.profileSnapshot).map(([name, overrides]) => ({
        name,
        summary: "Frozen at experiment start",
        builtin: false,
        overrides: { ...overrides },
      })),
    };
  }

  /** Re-resolve persisted targets to real project files before any run boundary. */
  private async refreshFilesystemTargetIdentities(
    experiment: ExperimentRun,
  ): Promise<void> {
    const { matrix, identityByTarget } = await resolveExistingExperimentMatrixTargets(
      this.runs.projectRoot(),
      experiment.matrix,
    );
    const selectedSkillsSha256ByTarget: Record<string, string> = {};
    const cells = experiment.cells.map((cell) => {
      const previousTarget = canonicalExperimentTarget(cell.target);
      const targetIdentity = identityByTarget[previousTarget];
      if (targetIdentity === undefined) {
        throw new Error("A persisted cell no longer belongs to the experiment target axis.");
      }
      if (
        cell.targetIdentity !== null &&
        canonicalExperimentTarget(cell.targetIdentity) !== targetIdentity
      ) {
        throw new Error(
          "A persisted cell's filesystem-canonical target identity changed. Create a new sweep.",
        );
      }

      const values = new Set<string>();
      for (const key of [
        selectedSkillsTargetKey(previousTarget),
        ...(cell.targetIdentity === null
          ? []
          : [selectedSkillsTargetKey(cell.targetIdentity)]),
        selectedSkillsTargetKey(targetIdentity),
      ]) {
        const value = experiment.selectedSkillsSha256ByTarget[key];
        if (value && /^[0-9a-f]{64}$/.test(value)) {
          values.add(value);
        }
      }
      if (values.size > 1) {
        throw new Error(
          "Conflicting selected-skill evidence exists for aliases of one experiment target.",
        );
      }
      const value = values.values().next().value as string | undefined;
      if (value !== undefined) {
        selectedSkillsSha256ByTarget[selectedSkillsTargetKey(targetIdentity)] = value;
      }
      return { ...cell, target: targetIdentity, targetIdentity };
    });
    validateRestoredExperimentCells(matrix, cells);
    experiment.matrix = matrix;
    experiment.cells = cells;
    experiment.selectedSkillsSha256ByTarget = selectedSkillsSha256ByTarget;
    await this.persistAuthority("the filesystem-canonical experiment target identities");
    this.changed.fire();
  }

  /** Resolve and persist every canonical runtime before the first paid launch. */
  private async freezeRuntimeIdentities(experiment: ExperimentRun): Promise<void> {
    const missing = experiment.cells.filter(
      (cell) => cell.expectedRuntime === null || cell.expectedLaunchContract === null,
    );
    if (missing.length === 0) {
      return;
    }
    if (
      experiment.baseline !== null ||
      experiment.cells.some((cell) => cell.startedAt !== null)
    ) {
      throw new Error(
        "This sweep predates preview-frozen runtime identities. Create a new sweep; " +
          "pending cells cannot inherit today's provider configuration.",
      );
    }
    const prompt = await this.requireExperimentPrompt(experiment);

    const profiles = this.frozenProfileCatalog(experiment);
    const catalog = this.getCatalog();
    const resolvedByCondition = new Map<
      string,
      {
        runtime: ExperimentRuntimeIdentity;
        contract: ExperimentCell["expectedLaunchContract"];
      }
    >();
    const resolvedCells = new Map<string, ExperimentRuntimeIdentity>();
    const resolvedContracts = new Map<
      string,
      NonNullable<ExperimentCell["expectedLaunchContract"]>
    >();
    for (const cell of experiment.cells) {
      const request = cellRequest(experiment.matrix, cell, prompt);
      const resolved = resolveLaunchEnv(request, profiles, catalog);
      if (resolved.rejected.length > 0) {
        throw new Error(
          "Cannot preview the frozen runtime because these knobs are unavailable: " +
            resolved.rejected.join(", "),
        );
      }
      const argv = buildWorkflowArgs(request);
      const conditionKey = JSON.stringify({
        argv,
        set: Object.entries(resolved.env.set).sort(([left], [right]) =>
          left.localeCompare(right),
        ),
        unset: [...resolved.env.unset].sort(),
      });
      let frozen = resolvedByCondition.get(conditionKey);
      if (!frozen) {
        const preview = await previewLaunch(
          this.runs.projectRoot(),
          argv,
          resolved.env,
        );
        if (preview.version !== 1) {
          throw new Error("The CLI returned an unsupported launch-preview version.");
        }
        frozen = {
          runtime: freezeExperimentRuntimeIdentity(preview.runtime, request.provider),
          contract: freezeExperimentLaunchContract(preview, resolved.env),
        };
        resolvedByCondition.set(conditionKey, frozen);
      }
      if (frozen.runtime.model !== cell.model) {
        throw new Error(
          `The CLI preview resolved model ${frozen.runtime.model}, but the cell requests ${cell.model}.`,
        );
      }
      resolvedCells.set(cell.id, frozen.runtime);
      resolvedContracts.set(cell.id, frozen.contract!);
    }

    for (const cell of experiment.cells) {
      cell.expectedRuntime = { ...resolvedCells.get(cell.id)! };
      const contract = resolvedContracts.get(cell.id)!;
      cell.expectedLaunchContract = {
        ...contract,
        workflow: { ...contract.workflow },
        set: { ...contract.set },
        unset: [...contract.unset],
        falseOrAbsent: [...contract.falseOrAbsent],
        emptyOrAbsent: [...contract.emptyOrAbsent],
        additionalSkills: contract.additionalSkills.map((identity) => ({ ...identity })),
      };
    }
    await this.persistAuthority("the preview-frozen runtime and per-cell launch contracts");
    this.changed.fire();
  }

  /** Copy every profile definition the matrix or its frozen base may select. */
  private freezeProfiles(matrix: ExperimentMatrix): Record<string, Record<string, string>> {
    const profiles = this.getProfiles();
    const snapshot: Record<string, Record<string, string>> = {};
    const names = new Set(matrix.profiles);
    if (matrix.base.profile) {
      names.add(matrix.base.profile);
    }
    for (const name of names) {
      const profile = profiles?.profiles.find((entry) => entry.name === name);
      if (!profile) {
        throw new Error(`Cannot freeze missing knob profile ${name}.`);
      }
      const rejected = rejectedProfileKnobNames(profile, this.getCatalog());
      if (rejected.length > 0) {
        throw new Error(
          `Cannot freeze profile ${name}; these knobs are unavailable to the extension: ` +
            rejected.join(", "),
        );
      }
      snapshot[name] = { ...profile.overrides };
    }
    this.rejectUnavailableKnobs(matrix.base.overrides, "frozen Launch overrides", true);
    return snapshot;
  }

  /** Revalidate frozen knobs before any clone or provider-backed run is prepared. */
  private validateFrozenKnobs(experiment: ExperimentRun): void {
    for (const [name, overrides] of Object.entries(experiment.profileSnapshot)) {
      this.rejectUnavailableKnobs(overrides, `frozen profile ${name}`, false);
    }
    this.rejectUnavailableKnobs(
      experiment.matrix.base.overrides,
      "frozen Launch overrides",
      true,
    );
  }

  private rejectUnavailableKnobs(
    overrides: Record<string, string>,
    label: string,
    allowUnset: boolean,
  ): void {
    const rejected = rejectedProfileKnobNames(
      {
        name: label,
        summary: "Experiment validation",
        builtin: false,
        overrides,
      },
      this.getCatalog(),
      allowUnset,
    );
    if (rejected.length > 0) {
      throw new Error(
        `Cannot run ${label}; these knobs are unavailable to the extension: ${rejected.join(", ")}`,
      );
    }
  }

  /**
   * Read one cell's outcome from the CLI's durable aggregate.
   *
   * Scoring is keyed on the cell's own run id, so a concurrent or subsequent
   * run cannot contribute to it, and the counts are totals over the complete
   * recorded stream rather than whatever the UI had buffered.
   */
  private async scoreCell(cell: ExperimentCell, projectRoot: string): Promise<ExperimentMetrics> {
    const metrics = await this.fetchBoundRunMetrics(cell, projectRoot);
    if (metrics === null) {
      // Without a run-bound aggregate the honest answer is "unknown", not a
      // guess assembled from whatever happens to be in memory.
      return unscoredMetrics("No exact durable aggregate matched this cell's run and baseline.");
    }
    const overlapReason = await this.sourceCheckoutOverlapReason(
      metrics.run.started_at,
      metrics.run.updated_at,
    );
    if (overlapReason !== null) {
      return unscoredMetrics(overlapReason);
    }
    const scored = metricsFromRun(metrics);
    if (!scored.scopeExact || scored.provenance === null) {
      return scored;
    }
    const launchProvenance = metrics.provenance;
    if (!launchProvenance) {
      return unscoredMetrics("The exact run has no launch provenance.");
    }
    const experiment = this.experiments.get(cell.matrixId);
    if (!experiment) {
      return unscoredMetrics("The experiment metadata disappeared before scoring.");
    }
    const launchInvariant = experimentLaunchInvariant(launchProvenance);
    if (launchInvariant === null) {
      return unscoredMetrics(
        "The launch provenance lacks complete component-wise experiment identities.",
      );
    }
    if (
      experiment.launchInvariant !== null &&
      !sameExperimentLaunchInvariant(experiment.launchInvariant, launchInvariant)
    ) {
      return unscoredMetrics(
        "A global launch input (source bytes, runtime, project configuration, behavior " +
          "configuration, toolchain, or dependencies) differs from the first measured cell.",
      );
    }
    const selectedSkills = compareTargetSelectedSkills(
      experiment.selectedSkillsSha256ByTarget,
      cell.targetIdentity ?? cell.target,
      launchProvenance.selected_skills_sha256,
    );
    if (!selectedSkills.accepted) {
      return unscoredMetrics(
        "Selected skill content differs from the first measured cell for this target.",
      );
    }
    let identityChanged = false;
    if (experiment.launchInvariant === null) {
      experiment.launchInvariant = launchInvariant;
      identityChanged = true;
    }
    if (!(selectedSkills.key in experiment.selectedSkillsSha256ByTarget)) {
      experiment.selectedSkillsSha256ByTarget[selectedSkills.key] = selectedSkills.expected;
      identityChanged = true;
    }
    if (experiment.launchSourceIdentitySha256 === null) {
      // Retained for display/export compatibility. Component-wise identities
      // above are the comparison authority because selected skills may
      // legitimately be target-specific.
      experiment.launchSourceIdentitySha256 = scored.provenance.source_identity_sha256;
      identityChanged = true;
    }
    if (identityChanged) {
      await this.persistAuthority("the cross-cell launch component identities");
    }
    return scored;
  }

  /** Reject a cell when durable source-root history cannot prove exclusivity. */
  private async sourceCheckoutOverlapReason(
    runStartedAt: string,
    runUpdatedAt: string,
  ): Promise<string | null> {
    let history: RunHistorySnapshot;
    try {
      // The CLI applies this as a slice bound. A safe-integer maximum requests
      // the complete retained index instead of silently auditing only a recent
      // page; an oversized response fails through the CLI wrapper and is
      // treated as unknown rather than clean.
      history = await fetchRunHistory(
        this.runs.projectRoot(),
        Number.MAX_SAFE_INTEGER,
      );
    } catch (error) {
      return (
        "Source-checkout run history was unreadable, so exclusive execution cannot be " +
        `verified: ${error instanceof Error ? error.message : String(error)}`
      );
    }
    if (!history.complete) {
      return (
        "Source-checkout run history is incomplete, so exclusive execution cannot be " +
        `verified: ${history.completenessIssues.slice(0, 10).join(", ") || "unknown archive issue"}`
      );
    }
    const audit = auditSourceRunOverlap(runStartedAt, runUpdatedAt, history.runs);
    if (!audit.cellIntervalReadable) {
      return "The cell execution interval is incomplete, so source-run overlap is unknown.";
    }
    if (audit.unreadableRunIds.length > 0) {
      return (
        "Source-checkout history contains run intervals that cannot be audited: " +
        audit.unreadableRunIds.slice(0, 10).join(", ")
      );
    }
    if (audit.overlappingRunIds.length > 0) {
      return (
        "A source-checkout workflow overlapped this cell's execution window: " +
        audit.overlappingRunIds.slice(0, 10).join(", ")
      );
    }
    return null;
  }

  /** Return exact immutable evidence only when it belongs to this cell and clone. */
  private async fetchBoundRunMetrics(
    cell: ExperimentCell,
    projectRoot: string,
  ): Promise<RunMetrics | null> {
    if (!cell.runId) {
      return null;
    }
    const metrics = await fetchRunMetrics(projectRoot, cell.runId).catch(() => null);
    const experiment = this.experiments.get(cell.matrixId);
    const baseline = experiment?.baseline;
    const provenance = metrics?.provenance;
    const finalProvenance = metrics?.provenance_final;
    const expectedRuntime = cell.expectedRuntime;
    const expectedLaunchContract = cell.expectedLaunchContract;
    if (
      metrics === null ||
      experiment == null ||
      baseline == null ||
      expectedRuntime === null ||
      expectedLaunchContract === null ||
      metrics.scope.requested_run_id !== cell.runId ||
      metrics.scope.resolved_run_id !== cell.runId ||
      metrics.run.run_id !== cell.runId ||
      metrics.run.workflow_kind !== experiment.matrix.kind ||
      !runMetricsMatchRequestedTarget(metrics, cell.targetIdentity ?? cell.target) ||
      !runMetricsMatchLaunchContract(metrics, expectedLaunchContract) ||
      !runMetricsMatchExplicitRuntime(metrics, expectedRuntime) ||
      provenance == null ||
      finalProvenance == null ||
      provenance.git_commit !== baseline.commit ||
      provenance.git_dirty !== false ||
      provenance.provenance_complete !== true ||
      finalProvenance.provenance_complete !== true ||
      path.resolve(provenance.project_root) !== path.resolve(projectRoot) ||
      path.resolve(finalProvenance.project_root) !== path.resolve(projectRoot)
    ) {
      return null;
    }
    let prompt: string;
    try {
      prompt = await readExperimentPrompt(this.context.secrets, experiment.matrix.id, {
        promptPresent: experiment.promptPresent,
        promptSha256: experiment.promptSha256,
      });
    } catch {
      return null;
    }
    if (
      !runMetricsMatchExplicitPrompt(metrics, {
        promptPresent: experiment.promptPresent,
        promptSha256: experiment.promptSha256,
        // Python's marker uses len(str), which counts Unicode code points.
        promptChars: Array.from(prompt).length,
      })
    ) {
      return null;
    }
    return metrics;
  }

  /**
   * Append the sweep to the evaluation harness's results file.
   *
   * The record shape matches `evals/harness.append_result`: one JSON object per
   * line carrying the suite, the experiment label, the flags, and the metrics,
   * so a sweep run from the editor is analysable with the same tooling as one
   * run from a script.
   */
  async export(matrixId: string, projectRoot: string): Promise<string> {
    await this.ready;
    const experiment = this.experiments.get(matrixId);
    if (!experiment) {
      throw new Error("Unknown experiment.");
    }
    if (!projectRoot) {
      throw new Error("Open a LeanFlow project before exporting a sweep.");
    }
    // The sweep name is user text and must not become a path. Slugify it before
    // passing the leaf to the symlink-safe project storage writer.
    const slug =
      experiment.matrix.name
        .trim()
        .replace(/[^A-Za-z0-9._-]+/g, "-")
        .replace(/^[-.]+|[-.]+$/g, "")
        .slice(0, 64) || experiment.matrix.id;
    const safeId = experiment.matrix.id.replace(/[^A-Za-z0-9_-]+/g, "-").slice(0, 64);
    const filename = `${slug}-${safeId || "sweep"}.jsonl`;

    const lines: string[] = [];
    for (const cell of experiment.cells) {
      if (cell.status === "pending" || cell.status === "running") {
        continue;
      }
      // Full declaration and dependency evidence remains authoritative in the
      // immutable per-run snapshot, not duplicated into VS Code Memento or
      // every webview refresh. Hydrate it only for this explicit export.
      const durableMetrics = cell.projectRoot
        ? await this.fetchBoundRunMetrics(cell, cell.projectRoot)
        : null;
      // Prefer what the run actually applied. Reading the profile's *current*
      // overrides would misreport the configuration whenever a profile has
      // been edited since the cell ran.
      const applied = cell.trackedRunId
        ? this.runs.getRun(cell.trackedRunId)?.appliedOverrides
        : undefined;
      const frozenProfile = experiment.profileSnapshot[cell.profile];
      const reconstructedFlags = { ...(frozenProfile ?? {}) };
      const reconstructedCleared: string[] = [];
      for (const [name, value] of Object.entries(experiment.matrix.base.overrides)) {
        if (value === "") {
          delete reconstructedFlags[name];
          reconstructedCleared.push(name);
        } else {
          reconstructedFlags[name] = value;
        }
      }
      const comparisonEligible = isComparisonEligibleCell(cell);
      lines.push(
        JSON.stringify({
          suite: experiment.matrix.name,
          experiment: cell.id,
          workflow_kind: experiment.matrix.kind,
          target: cell.target,
          target_identity: cell.targetIdentity,
          profile: cell.profile,
          model: cell.model,
          repeat: cell.repeat,
          planned_order: cell.plannedOrder,
          execution_order: cell.executionOrder,
          status: cell.status,
          comparison_eligible: comparisonEligible,
          comparison_exclusion_reason: comparisonEligible
            ? null
            : cell.metrics?.missingReason ||
              cell.error ||
              `Cell status ${cell.status} is not an eligible completed measurement.`,
          run_id: cell.runId,
          baseline: experiment.baseline,
          expected_runtime: cell.expectedRuntime,
          expected_launch_contract: cell.expectedLaunchContract,
          global_launch_invariant: experiment.launchInvariant,
          selected_skills_sha256_for_target:
            experiment.selectedSkillsSha256ByTarget[
              selectedSkillsTargetKey(cell.targetIdentity ?? cell.target)
            ] ?? null,
          expected_launch_source_identity_sha256:
            experiment.launchSourceIdentitySha256,
          isolated_project_root: cell.projectRoot,
          // `flags` is what ran; `flags_source` says where that came from, so a
          // reader can tell a recorded configuration from a reconstructed one.
          flags: applied?.set ?? reconstructedFlags,
          flags_cleared: applied?.unset ?? reconstructedCleared,
          flags_source: applied ? "recorded-at-launch" : "frozen-at-experiment-start",
          command: experimentCommandForExport(
            cell.trackedRunId
              ? this.runs.getRun(cell.trackedRunId)?.command
              : null,
            experiment.promptPresent,
          ),
          command_omitted_reason: experiment.promptPresent
            ? "prompt-content-is-recorded-only-as-a-durable-content-identity"
            : null,
          prompt_present: experiment.promptPresent,
          prompt_sha256: experiment.promptSha256,
          metrics: cell.metrics,
          durable_run_metrics: durableMetrics,
          started_at: cell.startedAt,
          finished_at: cell.finishedAt,
          error: cell.error,
        }),
      );
    }

    return writeProjectStorageFile(
      projectRoot,
      [".leanflow", "experiments"],
      filename,
      lines.join("\n") + (lines.length ? "\n" : ""),
    );
  }

  private persist(): Promise<void> {
    const snapshot = this.list();
    const update = this.persistQueue.then(() =>
      this.context.workspaceState.update(EXPERIMENTS_STORAGE_KEY, snapshot),
    );
    this.persistQueue = update.catch(() => undefined);
    return update;
  }

  /** Persist a reattachment authority before crossing a paid execution boundary. */
  private async persistAuthority(label: string): Promise<void> {
    try {
      await this.persist();
      return;
    } catch {
      // Memento updates can fail transiently. One serialized retry is cheap and
      // avoids either launching without authority or losing a terminal row.
    }
    try {
      await this.persist();
    } catch (error) {
      throw new Error(
        `LeanFlow could not persist ${label}; the sweep stopped before proceeding. ${
          error instanceof Error ? error.message : String(error)
        }`,
      );
    }
  }
}
