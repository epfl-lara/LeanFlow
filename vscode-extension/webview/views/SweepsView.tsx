/**
 * Define and run ablation sweeps, then compare the cells.
 *
 * A sweep is targets × profiles × models × repeats. Cells run sequentially in
 * isolated clones of one recorded baseline, with randomized order and explicit
 * missing-data/contrast reporting.
 */
import { useMemo, useState } from "react";

import {
  isSealedExperimentSkillReference,
  validateExplicitExperimentRuntime,
  validateExperimentMatrixSize,
} from "../../src/core/experimentMatrix";
import { rejectedProfileKnobNames, validateLaunch } from "../../src/core/launch";
import type { ExperimentCell, ExperimentMatrix, LaunchRequest } from "../../src/core/types";
import { CellProgress, Card, Empty, Notice, Pill, duration, formatNumber } from "../components";
import { PlayIcon, StopIcon } from "../icons";
import { useStore } from "../store";
import {
  aggregateCells,
  contrastCells,
  formatContrast,
  formatMean,
  type MetricKey,
} from "../stats";
import { post } from "../vscodeApi";

function parseList(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function cellTone(status: ExperimentCell["status"]): string {
  return status === "done"
    ? "done"
    : status === "failed" || status === "unscored" || status === "blocked"
      ? "failed"
      : status === "running"
        ? "running"
        : "";
}

export function SweepsView() {
  const { app, view } = useStore();
  const [name, setName] = useState("");
  const [targets, setTargets] = useState("");
  const [selectedProfiles, setSelectedProfiles] = useState<string[]>([]);
  const [models, setModels] = useState("");
  const [repeats, setRepeats] = useState(1);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [metric, setMetric] = useState<MetricKey>("durationSeconds");

  const profiles = app?.profiles?.profiles ?? [];
  const experiments = app?.experiments ?? [];
  const frozenBase = useMemo<LaunchRequest>(
    () => ({
      ...view.form,
      additionalSkills: [...view.form.additionalSkills],
      overrides: { ...view.form.overrides },
    }),
    [view.form],
  );
  const parsedTargets = parseList(targets);
  const parsedModels = parseList(models);
  const representative = {
    ...frozenBase,
    target: parsedTargets[0] ?? frozenBase.target,
    profile: selectedProfiles[0] ?? frozenBase.profile,
    model: parsedModels[0] ?? frozenBase.model,
  };
  const launchProblems = validateLaunch(representative);
  const unsealedSkills = frozenBase.additionalSkills.filter(
    (skill) => !isSealedExperimentSkillReference(skill),
  );
  if (unsealedSkills.length > 0) {
    launchProblems.push(
      `Sweep skills must be tracked project paths, not resolver names: ${unsealedSkills.join(", ")}.`,
    );
  }
  const profileNames = new Set(selectedProfiles);
  if (frozenBase.profile) {
    profileNames.add(frozenBase.profile);
  }
  const rejectedSelectedProfiles = [...profileNames].flatMap((name) => {
    const profile = profiles.find((entry) => entry.name === name);
    return rejectedProfileKnobNames(profile, app?.catalog ?? null).map(
      (knob) => `${name}:${knob}`,
    );
  });
  const rejectedBaseOverrides = rejectedProfileKnobNames(
    {
      name: "Launch overrides",
      summary: "Frozen Launch overrides",
      builtin: false,
      overrides: frozenBase.overrides,
    },
    app?.catalog ?? null,
    true,
  );
  if (frozenBase.profile && !profiles.some((entry) => entry.name === frozenBase.profile)) {
    launchProblems.push(`Launch profile is unavailable: ${frozenBase.profile}.`);
  }
  if (rejectedSelectedProfiles.length > 0) {
    launchProblems.push(
      `Selected profiles contain unavailable knobs: ${rejectedSelectedProfiles.join(", ")}.`,
    );
  }
  if (rejectedBaseOverrides.length > 0) {
    launchProblems.push(
      `Launch overrides contain unavailable knobs: ${rejectedBaseOverrides.join(", ")}.`,
    );
  }
  if (
    [representative.target, ...parsedTargets].some(
      (target) =>
        target.startsWith("-") ||
        target.startsWith("~") ||
        /^[A-Za-z]:/.test(target) ||
        /^[\\/]/.test(target) ||
        target.replace(/\\/g, "/").split("/").includes(".."),
    )
  ) {
    launchProblems.push("A sweep target must be a portable project-relative path.");
  }

  const draftMatrix: ExperimentMatrix = {
    id: "sweep-preview",
    name: name.trim() || "sweep",
    kind: frozenBase.kind,
    targets: parsedTargets,
    profiles: selectedProfiles,
    models: parsedModels,
    repeats,
    base: frozenBase,
    createdAt: "",
  };
  let matrixProblem = "";
  let cellCount = 0;
  try {
    cellCount = validateExperimentMatrixSize(draftMatrix);
    validateExplicitExperimentRuntime(draftMatrix);
  } catch (error) {
    matrixProblem = error instanceof Error ? error.message : String(error);
  }

  const baseSummary = [
    frozenBase.provider ? `provider ${frozenBase.provider}` : "configured provider",
    frozenBase.research ? "research" : "standard",
    frozenBase.cleanRoom ? "clean room" : "normal retrieval",
    frozenBase.noParallel ? "single lane" : `${frozenBase.agents} agent(s)`,
    frozenBase.prompt.trim() ? "prompt set" : "no prompt",
    frozenBase.axioms.trim() ? "custom axioms" : "standard axioms",
    `${frozenBase.additionalSkills.length} additional skill(s)`,
    `${Object.keys(frozenBase.overrides).length} one-off override(s)`,
  ].join(" · ");

  const create = () => {
    if (launchProblems.length > 0 || matrixProblem) {
      return;
    }
    const matrix: ExperimentMatrix = {
      id: `sweep-${Date.now().toString(36)}`,
      name: name.trim() || `sweep-${new Date().toISOString().slice(0, 10)}`,
      kind: frozenBase.kind,
      targets: parsedTargets,
      profiles: [...selectedProfiles],
      models: parsedModels,
      repeats,
      base: {
        ...frozenBase,
        additionalSkills: [...frozenBase.additionalSkills],
        overrides: { ...frozenBase.overrides },
      },
      createdAt: new Date().toISOString(),
    };
    post({ type: "createExperiment", matrix });
    setName("");
  };

  if (!app?.project.found) {
    return <Empty>Open a LeanFlow project to define a sweep.</Empty>;
  }

  return (
    <>
      <Card
        title="New sweep"
        subtitle={`Targets × profiles × models × repeats. This one expands to ${cellCount} run${cellCount === 1 ? "" : "s"}.`}
        actions={
          <button
            className="btn"
            onClick={create}
            disabled={launchProblems.length > 0 || Boolean(matrixProblem)}
          >
            Create sweep
          </button>
        }
      >
        <div className="grid two">
          <label className="field">
            <span className="label">Name</span>
            <input
              type="text"
              placeholder="e.g. negation-probe-ablation"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label className="field">
            <span className="label">Workflow</span>
            <input type="text" value={frozenBase.kind} readOnly />
            <span className="hint">Change this on the Launch tab before creating.</span>
          </label>
        </div>

        <Notice tone="info">
          <strong>Base settings frozen from the Launch tab when you create.</strong>
          <div style={{ marginTop: 4 }}>{baseSummary}</div>
        </Notice>

        <div className="grid two" style={{ marginTop: 10 }}>
          <label className="field">
            <span className="label">Targets</span>
            <textarea
              placeholder={"One per line.\nIMO2026/P1.lean\nIMO2026/P2.lean"}
              value={targets}
              onChange={(event) => setTargets(event.target.value)}
            />
            <span className="hint">
              Empty uses the Launch target: {frozenBase.target || "whole project"}.
            </span>
          </label>
          <label className="field">
            <span className="label">Models</span>
            <textarea
              placeholder={"One per line. Empty uses the configured default.\ngpt-5.6-terra"}
              value={models}
              onChange={(event) => setModels(event.target.value)}
            />
            <span className="hint">
              Empty uses the Launch model: {frozenBase.model || "configured default"}.
            </span>
          </label>
        </div>

        <div className="section-heading">Knob profiles — the ablation axis</div>
        <div className="row tight">
          {profiles.map((profile) => {
            const selected = selectedProfiles.includes(profile.name);
            const rejected = rejectedProfileKnobNames(profile, app?.catalog ?? null);
            return (
              <button
                key={profile.name}
                className={`pill ${selected ? "ok" : ""}`}
                style={{ cursor: rejected.length > 0 ? "not-allowed" : "pointer", background: "none" }}
                disabled={rejected.length > 0}
                onClick={() =>
                  setSelectedProfiles(
                    selected
                      ? selectedProfiles.filter((entry) => entry !== profile.name)
                      : [...selectedProfiles, profile.name],
                  )
                }
                title={
                  rejected.length > 0
                    ? `Unavailable in extension runs: ${rejected.join(", ")}`
                    : profile.summary
                }
              >
                {profile.name}{" "}
                <span className="muted">{Object.keys(profile.overrides).length}</span>
              </button>
            );
          })}
        </div>
        {profiles.length === 0 && (
          <Notice tone="warn">
            No profiles available. Build one on the Knobs tab first.
          </Notice>
        )}
        {selectedProfiles.length === 0 && (
          <div className="hint" style={{ marginTop: 6 }}>
            No axis profile selected; this uses the Launch profile: {frozenBase.profile || "declared defaults"}.
          </div>
        )}

        <div style={{ marginTop: 10, maxWidth: 180 }}>
          <label className="field">
            <span className="label">Repeats per cell</span>
            <input
              type="number"
              min={1}
              max={10}
              value={repeats}
              onChange={(event) => setRepeats(Number(event.target.value))}
            />
            <span className="hint">More than one lets you see run-to-run variance.</span>
          </label>
        </div>
        {(launchProblems.length > 0 || matrixProblem) && (
          <Notice tone="warn">
            {[...launchProblems, matrixProblem].filter(Boolean).join(" ")}
          </Notice>
        )}
      </Card>

      <Notice tone="info">
        Cells run one at a time in detached clones of one clean Git baseline. Their order is
        randomized once and recorded. This prevents an earlier proof or workflow state from
        leaking into a later condition; private build trees also avoid cross-cell cache writes.
        A source-checkout workflow observed mid-cell invalidates that cell. Unrelated external
        machine workloads remain outside the extension's control and should be held constant.
        Additional skills must be tracked project-relative SKILL.md paths; bare discovered names
        are rejected because their external content is not part of the Git baseline.
      </Notice>

      {experiments.length === 0 && (
        <Empty>No sweeps yet. Define one above to start comparing knob settings.</Empty>
      )}

      {experiments.map((experiment) => {
        const done = experiment.cells.filter((cell) => cell.status === "done").length;
        const failed = experiment.cells.filter((cell) => cell.status === "failed").length;
        const unscored = experiment.cells.filter((cell) => cell.status === "unscored").length;
        const blocked = experiment.cells.filter((cell) => cell.status === "blocked").length;
        const solved = experiment.cells.filter((cell) => cell.metrics?.solved === true).length;
        const isOpen = expanded === experiment.matrix.id;
        const runnable = experiment.cells.some(
          (cell) => cell.status === "pending" || cell.status === "running",
        );
        const hasRunningCell = experiment.cells.some((cell) => cell.status === "running");
        const aggregates = aggregateCells(experiment.cells, metric);
        const contrasts = contrastCells(experiment.cells, metric);
        return (
          <Card
            key={experiment.matrix.id}
            title={
              <>
                {experiment.matrix.name}{" "}
                <Pill tone={experiment.status === "running" ? "running" : undefined}>
                  {experiment.status}
                </Pill>
              </>
            }
            subtitle={`${experiment.cells.length} cells · ${done} done · ${failed} failed · ${unscored} unscored · ${blocked} blocked · ${solved} solved`}
            actions={
              <>
                <button
                  className="btn ghost"
                  onClick={() => setExpanded(isOpen ? null : experiment.matrix.id)}
                >
                  {isOpen ? "Hide" : "Results"}
                </button>
                {experiment.status === "running" ? (
                  <button
                    className="btn secondary"
                    onClick={() =>
                      post({ type: "stopExperiment", matrixId: experiment.matrix.id })
                    }
                  >
                    <StopIcon size={12} />
                    Stop
                  </button>
                ) : (
                  <button
                    className="btn"
                    disabled={!runnable}
                    onClick={() =>
                      post({ type: "startExperiment", matrixId: experiment.matrix.id })
                    }
                  >
                    <PlayIcon size={12} />
                    {!runnable
                      ? "No pending cells"
                      : hasRunningCell
                        ? "Re-attach"
                        : done > 0
                          ? "Resume"
                          : "Run"}
                  </button>
                )}
                {experiment.status !== "running" && hasRunningCell && (
                  <button
                    className="btn secondary"
                    onClick={() =>
                      post({ type: "stopExperiment", matrixId: experiment.matrix.id })
                    }
                  >
                    <StopIcon size={12} />
                    Stop
                  </button>
                )}
                <button
                  className="btn ghost"
                  onClick={() => post({ type: "exportExperiment", matrixId: experiment.matrix.id })}
                >
                  Export
                </button>
                <button
                  className="btn ghost"
                  disabled={experiment.status === "running" || hasRunningCell}
                  title={
                    experiment.status === "running" || hasRunningCell
                      ? "Stop the sweep and wait for its active cell before deleting."
                      : undefined
                  }
                  onClick={() => post({ type: "deleteExperiment", matrixId: experiment.matrix.id })}
                >
                  Delete
                </button>
              </>
            }
          >
            <CellProgress cells={experiment.cells} />
            {experiment.error && <Notice tone="warn">{experiment.error}</Notice>}
            {isOpen && (
              <>
                <div className="section-heading">Reproducibility baseline</div>
                {experiment.baseline ? (
                  <div className="kv-list" style={{ marginBottom: 12 }}>
                    <div className="kv-row">
                      <span>strategy</span>
                      <code>{experiment.baseline.strategy}</code>
                    </div>
                    <div className="kv-row">
                      <span>commit</span>
                      <code>{experiment.baseline.commit}</code>
                    </div>
                    <div className="kv-row">
                      <span>tree</span>
                      <code>{experiment.baseline.tree}</code>
                    </div>
                    <div className="kv-row">
                      <span>manifest</span>
                      <code>{experiment.baseline.manifestSha256}</code>
                    </div>
                    <div className="kv-row">
                      <span>order seed</span>
                      <code>{experiment.matrix.randomizationSeed}</code>
                    </div>
                    <div className="kv-row">
                      <span>first launch full identity</span>
                      <code>{experiment.launchSourceIdentitySha256 ?? "pending first exact cell"}</code>
                    </div>
                    <div className="kv-row">
                      <span>actual source bytes</span>
                      <code>{experiment.launchInvariant?.sourceTreeSha256 ?? "pending"}</code>
                    </div>
                    <div className="kv-row">
                      <span>runtime source</span>
                      <code>{experiment.launchInvariant?.runtimeSourceSha256 ?? "pending"}</code>
                    </div>
                  </div>
                ) : (
                  <Notice tone="warn">
                    The baseline is captured at first launch. A dirty, non-Git, symlinked, or
                    otherwise non-reproducible source fails closed without editing the checkout.
                  </Notice>
                )}
                <div className="section-heading">Across repeats</div>
                <div className="row tight" style={{ marginBottom: 8 }}>
                  <span className="muted" style={{ fontSize: 11.5 }}>
                    metric
                  </span>
                  <select
                    style={{ maxWidth: 180 }}
                    value={metric}
                    onChange={(event) => setMetric(event.target.value as MetricKey)}
                  >
                    <option value="durationSeconds">duration (s)</option>
                    <option value="costUsd">cost (USD)</option>
                    <option value="outputTokens">output tokens</option>
                    <option value="toolCalls">tool calls</option>
                  </select>
                  <span className="muted" style={{ fontSize: 11.5 }}>
                    mean ± 95% Student-t CI; missing durable metrics are excluded and shown
                  </span>
                </div>
                <div className="table-wrap" style={{ maxHeight: 260, marginBottom: 12 }}>
                  <table className="data">
                    <thead>
                      <tr>
                        <th>Target</th>
                        <th>Profile</th>
                        <th>Model</th>
                        <th className="num">Measured n</th>
                        <th className="num">Missing n</th>
                        <th className="num">Solved</th>
                        <th className="num">{metric}</th>
                        <th className="num">SD</th>
                      </tr>
                    </thead>
                    <tbody>
                      {aggregates.map((row) => (
                        <tr key={`${row.target}|${row.profile}|${row.model}`}>
                          <td className="mono">{row.target || "(project)"}</td>
                          <td>{row.profile || "—"}</td>
                          <td className="mono">{row.model || "default"}</td>
                          <td className="num">{row.measuredN}</td>
                          <td className="num">{row.missingN}</td>
                          <td className="num">
                            {row.solveRate === null
                              ? `— (${row.solveMissingN} missing)`
                              : `${row.solved}/${row.solveMeasuredN} (${Math.round(row.solveRate * 100)}%)${row.solveMissingN > 0 ? ` · ${row.solveMissingN} missing` : ""}`}
                          </td>
                          <td className="num">{formatMean(row, metric === "costUsd" ? 2 : 1)}</td>
                          <td className="num">
                            {row.stdDev === null ? "—" : row.stdDev.toFixed(2)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {contrasts.length > 0 && (
                  <>
                    <div className="section-heading">Direct condition contrasts</div>
                    <div className="muted" style={{ fontSize: 11.5, marginBottom: 8 }}>
                      Effect is right − left with a direct 95% Welch interval. This is the row to
                      use when assessing a knob difference; separate mean intervals do not test it.
                      Pairwise intervals are unadjusted, so pre-specify a contrast or correct for
                      multiplicity when comparing several conditions.
                    </div>
                    <div className="table-wrap" style={{ maxHeight: 220, marginBottom: 12 }}>
                      <table className="data">
                        <thead>
                          <tr>
                            <th>Target</th>
                            <th>Left</th>
                            <th>Right</th>
                            <th className="num">n L/R</th>
                            <th className="num">Right − left</th>
                          </tr>
                        </thead>
                        <tbody>
                          {contrasts.map((row) => (
                            <tr
                              key={`${row.target}|${row.leftProfile}|${row.leftModel}|${row.rightProfile}|${row.rightModel}`}
                            >
                              <td className="mono">{row.target || "(project)"}</td>
                              <td>
                                {row.leftProfile || "—"} · <span className="mono">{row.leftModel || "default"}</span>
                              </td>
                              <td>
                                {row.rightProfile || "—"} · <span className="mono">{row.rightModel || "default"}</span>
                              </td>
                              <td className="num">{row.leftN}/{row.rightN}</td>
                              <td className="num">
                                {formatContrast(row, metric === "costUsd" ? 2 : 1)}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </>
                )}
                <div className="section-heading">Individual cells</div>
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Target</th>
                      <th>Profile</th>
                      <th>Model</th>
                      <th>Order</th>
                      <th>#</th>
                      <th>Status</th>
                      <th>Solved</th>
                      <th className="num">Sorries</th>
                      <th className="num">Duration</th>
                      <th className="num">API</th>
                      <th className="num">Out tok</th>
                      <th className="num">Cost</th>
                      <th className="num">Decls</th>
                    </tr>
                  </thead>
                  <tbody>
                    {experiment.cells.map((cell) => (
                      <tr key={cell.id} className={cellTone(cell.status)}>
                        <td className="mono">{cell.target || "(project)"}</td>
                        <td>{cell.profile || "—"}</td>
                        <td className="mono">{cell.model || "default"}</td>
                        <td className="num">
                          {cell.executionOrder ?? "—"}
                          <span className="muted">/{cell.plannedOrder}</span>
                        </td>
                        <td className="num">{cell.repeat}</td>
                        <td title={cell.error ?? undefined}>{cell.status}</td>
                        <td>
                          {cell.metrics?.solved === true
                            ? "yes"
                            : cell.metrics?.solved === false
                              ? "no"
                              : "—"}
                        </td>
                        <td className="num">{cell.metrics?.sorryCount ?? "—"}</td>
                        <td className="num">{duration(cell.metrics?.durationSeconds)}</td>
                        <td className="num">{formatNumber(cell.metrics?.apiCalls)}</td>
                        <td className="num">{formatNumber(cell.metrics?.outputTokens)}</td>
                        <td className="num">
                          {cell.metrics?.costUsd !== null && cell.metrics?.costUsd !== undefined
                            ? `$${cell.metrics.costUsd.toFixed(2)}`
                            : "—"}
                        </td>
                        <td className="num">
                          {cell.metrics?.declarationsProved !== null &&
                          cell.metrics?.declarationsProved !== undefined &&
                          cell.metrics?.declarationsTotal !== null &&
                          cell.metrics?.declarationsTotal !== undefined
                            ? `${cell.metrics.declarationsProved}/${cell.metrics.declarationsTotal}`
                            : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              </>
            )}
          </Card>
        );
      })}
    </>
  );
}
