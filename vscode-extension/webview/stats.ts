/**
 * Aggregate experiment cells with missing-data and small-sample honesty.
 *
 * Metric and solve-rate denominators use only measured observations. Missing
 * durable aggregates are reported separately and never become zero-valued or
 * failed samples. Mean intervals use Student's t, and pairwise condition rows
 * report a direct Welch contrast instead of inviting readers to compare two
 * unrelated intervals by eye.
 */
import type { ExperimentCell } from "../src/core/types";
import { isComparisonEligibleCell } from "../src/core/experimentMatrix";

export interface Aggregate {
  target: string;
  profile: string;
  model: string;
  attemptedN: number;
  measuredN: number;
  missingN: number;
  solveMeasuredN: number;
  solveMissingN: number;
  solved: number;
  solveRate: number | null;
  /** Mean of the sampled metric, or null when nothing was recorded. */
  mean: number | null;
  /** Sample standard deviation; null when measured n < 2. */
  stdDev: number | null;
  /** Half-width of the two-sided 95% Student-t interval on the mean. */
  ci95: number | null;
}

export interface ConditionContrast {
  target: string;
  leftProfile: string;
  leftModel: string;
  rightProfile: string;
  rightModel: string;
  leftN: number;
  rightN: number;
  /** Right condition mean minus left condition mean. */
  effect: number | null;
  /** Welch-Satterthwaite degrees of freedom. */
  degreesOfFreedom: number | null;
  /** Half-width of the two-sided 95% Welch interval on the effect. */
  ci95: number | null;
}

export type MetricKey = "durationSeconds" | "costUsd" | "outputTokens" | "toolCalls";

interface SampleStats {
  mean: number | null;
  sd: number | null;
}

function sampleStats(values: number[]): SampleStats {
  if (values.length === 0) {
    return { mean: null, sd: null };
  }
  const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
  if (values.length < 2) {
    return { mean, sd: null };
  }
  const variance =
    values.reduce((sum, value) => sum + (value - mean) ** 2, 0) / (values.length - 1);
  return { mean, sd: Math.sqrt(variance) };
}

function logGamma(value: number): number {
  const coefficients = [
    676.5203681218851,
    -1259.1392167224028,
    771.3234287776531,
    -176.6150291621406,
    12.507343278686905,
    -0.13857109526572012,
    9.984369578019572e-6,
    1.5056327351493116e-7,
  ];
  if (value < 0.5) {
    return Math.log(Math.PI) - Math.log(Math.sin(Math.PI * value)) - logGamma(1 - value);
  }
  const shifted = value - 1;
  let series = 0.9999999999998099;
  for (let index = 0; index < coefficients.length; index += 1) {
    series += coefficients[index] / (shifted + index + 1);
  }
  const t = shifted + coefficients.length - 0.5;
  return (
    0.5 * Math.log(2 * Math.PI) +
    (shifted + 0.5) * Math.log(t) -
    t +
    Math.log(series)
  );
}

function betaContinuedFraction(a: number, b: number, x: number): number {
  const maxIterations = 300;
  const epsilon = 3e-14;
  const minimum = 1e-300;
  const sum = a + b;
  const aPlusOne = a + 1;
  const aMinusOne = a - 1;
  let c = 1;
  let d = 1 - (sum * x) / aPlusOne;
  d = Math.abs(d) < minimum ? minimum : d;
  d = 1 / d;
  let result = d;
  for (let iteration = 1; iteration <= maxIterations; iteration += 1) {
    const twice = 2 * iteration;
    let numerator =
      (iteration * (b - iteration) * x) /
      ((aMinusOne + twice) * (a + twice));
    d = 1 + numerator * d;
    d = Math.abs(d) < minimum ? minimum : d;
    c = 1 + numerator / c;
    c = Math.abs(c) < minimum ? minimum : c;
    d = 1 / d;
    result *= d * c;

    numerator =
      (-(a + iteration) * (sum + iteration) * x) /
      ((a + twice) * (aPlusOne + twice));
    d = 1 + numerator * d;
    d = Math.abs(d) < minimum ? minimum : d;
    c = 1 + numerator / c;
    c = Math.abs(c) < minimum ? minimum : c;
    d = 1 / d;
    const delta = d * c;
    result *= delta;
    if (Math.abs(delta - 1) < epsilon) {
      return result;
    }
  }
  return result;
}

function regularizedIncompleteBeta(x: number, a: number, b: number): number {
  if (x <= 0) {
    return 0;
  }
  if (x >= 1) {
    return 1;
  }
  const factor = Math.exp(
    logGamma(a + b) -
      logGamma(a) -
      logGamma(b) +
      a * Math.log(x) +
      b * Math.log1p(-x),
  );
  if (x < (a + 1) / (a + b + 2)) {
    return (factor * betaContinuedFraction(a, b, x)) / a;
  }
  return 1 - (factor * betaContinuedFraction(b, a, 1 - x)) / b;
}

function studentTCdf(value: number, degreesOfFreedom: number): number {
  const x = degreesOfFreedom / (degreesOfFreedom + value * value);
  const tail =
    0.5 * regularizedIncompleteBeta(x, degreesOfFreedom / 2, 0.5);
  return value >= 0 ? 1 - tail : tail;
}

/** Return the two-sided 95% Student-t critical value for positive df. */
export function tCritical95(degreesOfFreedom: number): number | null {
  if (!Number.isFinite(degreesOfFreedom)) {
    return degreesOfFreedom === Number.POSITIVE_INFINITY ? 1.959963984540054 : null;
  }
  if (degreesOfFreedom < 1) {
    return null;
  }
  let lower = 0;
  let upper = 1;
  while (studentTCdf(upper, degreesOfFreedom) < 0.975) {
    upper *= 2;
    if (upper > 1_000_000) {
      return null;
    }
  }
  for (let iteration = 0; iteration < 80; iteration += 1) {
    const midpoint = (lower + upper) / 2;
    if (studentTCdf(midpoint, degreesOfFreedom) < 0.975) {
      lower = midpoint;
    } else {
      upper = midpoint;
    }
  }
  return (lower + upper) / 2;
}

function terminalCell(cell: ExperimentCell): boolean {
  return cell.status !== "pending" && cell.status !== "running";
}

function metricValue(cell: ExperimentCell, metric: MetricKey): number | null {
  if (
    !isComparisonEligibleCell(cell) ||
    cell.metrics === null
  ) {
    return null;
  }
  if (metric === "toolCalls" && !cell.metrics.countsAreComplete) {
    return null;
  }
  const value = cell.metrics[metric];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function conditionKey(cell: ExperimentCell): string {
  return JSON.stringify([cell.target, cell.profile, cell.model]);
}

function groupedCells(cells: ExperimentCell[]): Map<string, ExperimentCell[]> {
  const groups = new Map<string, ExperimentCell[]>();
  for (const cell of cells) {
    if (!terminalCell(cell)) {
      continue;
    }
    const key = conditionKey(cell);
    groups.set(key, [...(groups.get(key) ?? []), cell]);
  }
  return groups;
}

/** Group terminal cells by condition and summarize measured and missing data. */
export function aggregateCells(cells: ExperimentCell[], metric: MetricKey): Aggregate[] {
  const rows: Aggregate[] = [];
  for (const [key, members] of groupedCells(cells)) {
    const [target, profile, model] = JSON.parse(key) as [string, string, string];
    const values = members
      .map((cell) => metricValue(cell, metric))
      .filter((value): value is number => value !== null);
    const { mean, sd } = sampleStats(values);
    const solvedValues = members
      .map((cell) =>
        isComparisonEligibleCell(cell)
          ? cell.metrics?.solved ?? null
          : null,
      )
      .filter((value): value is boolean => typeof value === "boolean");
    const solved = solvedValues.filter(Boolean).length;
    const critical = values.length > 1 ? tCritical95(values.length - 1) : null;
    rows.push({
      target,
      profile,
      model,
      attemptedN: members.length,
      measuredN: values.length,
      missingN: members.length - values.length,
      solveMeasuredN: solvedValues.length,
      solveMissingN: members.length - solvedValues.length,
      solved,
      solveRate: solvedValues.length > 0 ? solved / solvedValues.length : null,
      mean,
      stdDev: sd,
      ci95:
        sd !== null && critical !== null ? critical * (sd / Math.sqrt(values.length)) : null,
    });
  }
  rows.sort(
    (left, right) =>
      left.target.localeCompare(right.target) ||
      left.profile.localeCompare(right.profile) ||
      left.model.localeCompare(right.model),
  );
  return rows;
}

/** Return direct pairwise Welch contrasts within each target. */
export function contrastCells(
  cells: ExperimentCell[],
  metric: MetricKey,
): ConditionContrast[] {
  const groups = [...groupedCells(cells)]
    .map(([key, members]) => {
      const [target, profile, model] = JSON.parse(key) as [string, string, string];
      const values = members
        .map((cell) => metricValue(cell, metric))
        .filter((value): value is number => value !== null);
      return { target, profile, model, values, stats: sampleStats(values) };
    })
    .sort(
      (left, right) =>
        left.target.localeCompare(right.target) ||
        left.profile.localeCompare(right.profile) ||
        left.model.localeCompare(right.model),
    );
  const contrasts: ConditionContrast[] = [];
  for (let leftIndex = 0; leftIndex < groups.length; leftIndex += 1) {
    const left = groups[leftIndex];
    for (let rightIndex = leftIndex + 1; rightIndex < groups.length; rightIndex += 1) {
      const right = groups[rightIndex];
      if (left.target !== right.target) {
        continue;
      }
      const effect =
        left.stats.mean === null || right.stats.mean === null
          ? null
          : right.stats.mean - left.stats.mean;
      let degreesOfFreedom: number | null = null;
      let ci95: number | null = null;
      if (
        effect !== null &&
        left.stats.sd !== null &&
        right.stats.sd !== null &&
        left.values.length > 1 &&
        right.values.length > 1
      ) {
        const leftTerm = left.stats.sd ** 2 / left.values.length;
        const rightTerm = right.stats.sd ** 2 / right.values.length;
        const standardErrorSquared = leftTerm + rightTerm;
        if (standardErrorSquared === 0) {
          degreesOfFreedom = Number.POSITIVE_INFINITY;
          ci95 = 0;
        } else {
          degreesOfFreedom =
            standardErrorSquared ** 2 /
            (leftTerm ** 2 / (left.values.length - 1) +
              rightTerm ** 2 / (right.values.length - 1));
          const critical = tCritical95(degreesOfFreedom);
          if (critical !== null) {
            ci95 = critical * Math.sqrt(standardErrorSquared);
          }
        }
      }
      contrasts.push({
        target: left.target,
        leftProfile: left.profile,
        leftModel: left.model,
        rightProfile: right.profile,
        rightModel: right.model,
        leftN: left.values.length,
        rightN: right.values.length,
        effect,
        degreesOfFreedom,
        ci95,
      });
    }
  }
  return contrasts.sort(
    (left, right) =>
      left.target.localeCompare(right.target) ||
      left.leftProfile.localeCompare(right.leftProfile) ||
      left.rightProfile.localeCompare(right.rightProfile),
  );
}

/** Render a mean with its Student-t interval and measured sample count. */
export function formatMean(row: Aggregate, digits = 1): string {
  if (row.mean === null) {
    return "—";
  }
  const mean = row.mean.toFixed(digits);
  if (row.ci95 === null) {
    return `${mean} (n=${row.measuredN})`;
  }
  return `${mean} ± ${row.ci95.toFixed(digits)}`;
}

/** Render a right-minus-left effect with its direct Welch interval. */
export function formatContrast(row: ConditionContrast, digits = 1): string {
  if (row.effect === null) {
    return "—";
  }
  const effect = row.effect.toFixed(digits);
  if (row.ci95 === null) {
    return `${effect} (interval needs n≥2/condition)`;
  }
  return `${effect} ± ${row.ci95.toFixed(digits)}`;
}
