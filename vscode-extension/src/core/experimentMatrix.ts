/** Validate experiment dimensions before allocating or persisting cells. */
import type { ExperimentCell, ExperimentCellStatus, ExperimentMatrix } from "./types";

export const MAX_EXPERIMENT_AXIS = 100;
export const MAX_EXPERIMENT_CELLS = 1_000;

/** Return one portable project-relative target spelling without resolving the filesystem. */
export function canonicalExperimentTarget(rawTarget: string): string {
  const target = rawTarget.trim();
  if (!target) {
    return "";
  }
  if (
    target.includes("\0") ||
    target.startsWith("~") ||
    /^[A-Za-z]:/.test(target) ||
    /^[\\/]/.test(target)
  ) {
    throw new Error(`Experiment target must be a safe project-relative path: ${rawTarget}`);
  }
  const segments = target.replace(/\\/g, "/").split("/");
  if (segments.includes("..")) {
    throw new Error(`Experiment target must be a safe project-relative path: ${rawTarget}`);
  }
  const canonical = segments.filter((segment) => segment && segment !== ".").join("/");
  if (!canonical || canonical.startsWith("-")) {
    throw new Error(`Experiment target must be a safe project-relative path: ${rawTarget}`);
  }
  return canonical;
}

/** Return the canonical target axis used to size, persist, and expand a matrix. */
export function experimentTargets(matrix: ExperimentMatrix): string[] {
  const targets = matrix.targets.length > 0 ? matrix.targets : [matrix.base.target];
  return targets.map(canonicalExperimentTarget);
}

/** Copy a matrix with canonical target spellings while preserving project-relative meaning. */
export function normalizeExperimentMatrixTargets(matrix: ExperimentMatrix): ExperimentMatrix {
  return {
    ...matrix,
    targets: matrix.targets.map(canonicalExperimentTarget),
    base: {
      ...matrix.base,
      target: canonicalExperimentTarget(matrix.base.target),
    },
  };
}

/** Require every cell to name the provider and model sealed into its result. */
export function validateExplicitExperimentRuntime(matrix: ExperimentMatrix): void {
  const provider = matrix.base.provider.trim();
  if (!provider || provider !== matrix.base.provider) {
    throw new Error(
      "Research sweeps require an explicit provider with no surrounding whitespace.",
    );
  }
  const models = matrix.models.length > 0 ? matrix.models : [matrix.base.model];
  if (models.some((model) => !model.trim() || model.trim() !== model)) {
    throw new Error(
      "Research sweeps require an explicit model for every cell with no surrounding whitespace.",
    );
  }
}

/** Return whether a skill reference names project content instead of an external resolver key. */
export function isSealedExperimentSkillReference(reference: string): boolean {
  const normalized = reference.trim();
  const segments = normalized.replace(/\\/g, "/").split("/");
  return Boolean(
    normalized &&
      !normalized.startsWith("~") &&
      !/^[A-Za-z]:/.test(normalized) &&
      !/^[\\/]/.test(normalized) &&
      !segments.includes("..") &&
      (normalized.startsWith(".") || normalized.includes("/") || normalized.includes("\\")),
  );
}

/** Return whether start may act on a cell without overwriting terminal evidence. */
export function isRunnableExperimentCellStatus(status: ExperimentCellStatus): boolean {
  return status === "pending" || status === "running";
}

/** Return whether a terminal row may enter a research comparison. */
export function isComparisonEligibleCell(cell: ExperimentCell): boolean {
  return (
    (cell.status === "done" || cell.status === "failed") &&
    cell.metrics?.scopeExact === true &&
    cell.metrics.phase !== "unscored"
  );
}

/** Refuse a replay that would replace evidence under an existing matrix id. */
export function assertExperimentIdAvailable(existing: boolean, matrixId: string): void {
  if (existing) {
    throw new Error(`Experiment id already exists and cannot be overwritten: ${matrixId}`);
  }
}

/** Refuse clone cleanup until both the engine and every cell are terminal. */
export function assertExperimentDeletable(
  engineIsRunning: boolean,
  cells: readonly ExperimentCell[],
): void {
  if (engineIsRunning || cells.some((cell) => cell.status === "running")) {
    throw new Error(
      "Stop this sweep and wait for its active cell to finish before deleting it.",
    );
  }
}

const CELL_STATUSES = new Set<ExperimentCellStatus>([
  "pending",
  "running",
  "done",
  "failed",
  "unscored",
  "blocked",
  "skipped",
]);

function designKey(
  target: string,
  profile: string,
  model: string,
  repeat: number,
): string {
  return JSON.stringify([canonicalExperimentTarget(target), profile, model, repeat]);
}

/** Reject restored cells that no longer represent exactly one matrix cross product. */
export function validateRestoredExperimentCells(
  matrix: ExperimentMatrix,
  cells: readonly ExperimentCell[],
): void {
  const expectedCount = validateExperimentMatrixSize(matrix);
  if (cells.length !== expectedCount) {
    throw new Error(
      `Persisted experiment contains ${cells.length} cells, but its matrix defines ${expectedCount}.`,
    );
  }
  const targets = experimentTargets(matrix);
  const profiles = matrix.profiles.length > 0 ? matrix.profiles : [matrix.base.profile];
  const models = matrix.models.length > 0 ? matrix.models : [matrix.base.model];
  const expected = new Set<string>();
  for (const target of targets) {
    for (const profile of profiles) {
      for (const model of models) {
        for (let repeat = 1; repeat <= matrix.repeats; repeat += 1) {
          expected.add(designKey(target, profile, model, repeat));
        }
      }
    }
  }

  const ids = new Set<string>();
  const designs = new Set<string>();
  const runIds = new Set<string>();
  const trackedRunIds = new Set<string>();
  const plannedOrders = new Set<number>();
  const executionOrders = new Set<number>();
  for (const cell of cells) {
    if (
      !cell.id ||
      ids.has(cell.id) ||
      cell.matrixId !== matrix.id ||
      !CELL_STATUSES.has(cell.status)
    ) {
      throw new Error("Persisted experiment contains an invalid or duplicate cell identity.");
    }
    ids.add(cell.id);
    if (
      cell.targetIdentity != null &&
      canonicalExperimentTarget(cell.targetIdentity) !== canonicalExperimentTarget(cell.target)
    ) {
      throw new Error(
        "Persisted experiment cell target identity does not match its recorded matrix target.",
      );
    }
    const key = designKey(cell.target, cell.profile, cell.model, cell.repeat);
    if (!expected.has(key) || designs.has(key)) {
      throw new Error("Persisted experiment cells do not match the recorded matrix design.");
    }
    designs.add(key);
    if (cell.plannedOrder !== undefined) {
      if (
        !Number.isSafeInteger(cell.plannedOrder) ||
        cell.plannedOrder < 1 ||
        cell.plannedOrder > cells.length ||
        plannedOrders.has(cell.plannedOrder)
      ) {
        throw new Error("Persisted experiment contains an invalid planned execution order.");
      }
      plannedOrders.add(cell.plannedOrder);
    }
    if (cell.executionOrder !== null && cell.executionOrder !== undefined) {
      if (
        !Number.isSafeInteger(cell.executionOrder) ||
        cell.executionOrder < 1 ||
        cell.executionOrder > cells.length ||
        executionOrders.has(cell.executionOrder)
      ) {
        throw new Error("Persisted experiment contains an invalid actual execution order.");
      }
      executionOrders.add(cell.executionOrder);
    }
    for (const [label, value, seen] of [
      ["run id", cell.runId, runIds],
      ["tracked run id", cell.trackedRunId, trackedRunIds],
    ] as const) {
      if (value && seen.has(value)) {
        throw new Error(`Persisted experiment reuses one ${label} across multiple cells.`);
      }
      if (value) {
        seen.add(value);
      }
    }
  }
}

/** Return matrix cardinality or reject an oversized/malformed request. */
export function validateExperimentMatrixSize(matrix: ExperimentMatrix): number {
  const axes: [string, unknown][] = [
    ["targets", matrix.targets],
    ["profiles", matrix.profiles],
    ["models", matrix.models],
  ];
  let cells = 1;
  for (const [name, value] of axes) {
    if (!Array.isArray(value)) {
      throw new Error(`Experiment ${name} must be an array.`);
    }
    if (value.length > MAX_EXPERIMENT_AXIS) {
      throw new Error(`Experiment ${name} exceeds the ${MAX_EXPERIMENT_AXIS}-value limit.`);
    }
    if (value.some((entry) => typeof entry !== "string")) {
      throw new Error(`Every experiment ${name} value must be text.`);
    }
    const identityValues =
      name === "targets"
        ? (value as string[]).map(canonicalExperimentTarget)
        : (value as string[]);
    if (new Set(identityValues).size !== value.length) {
      throw new Error(`Experiment ${name} values must be unique.`);
    }
    cells *= Math.max(1, value.length);
  }
  if (
    !Number.isSafeInteger(matrix.repeats) ||
    matrix.repeats < 1 ||
    matrix.repeats > MAX_EXPERIMENT_AXIS
  ) {
    throw new Error(`Experiment repeats must be an integer from 1 to ${MAX_EXPERIMENT_AXIS}.`);
  }
  cells *= matrix.repeats;
  if (cells > MAX_EXPERIMENT_CELLS) {
    throw new Error(
      `Experiment expands to ${cells} cells; the product limit is ${MAX_EXPERIMENT_CELLS}.`,
    );
  }
  return cells;
}
