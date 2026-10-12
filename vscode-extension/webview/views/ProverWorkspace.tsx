/** Inspect an exact prover run: state, budget, controller work, dependencies, plan, jobs, edits, and guidance. */
import { useEffect, useMemo, useState } from "react";

import type { ProverNode, ProverSnapshot } from "../../src/core/prover";
import { nodeLifecycle, proofStateCounts, proofStateSummary } from "../../src/core/proverGraph";
import {
  STALE_AFTER_MS,
  acceptedReview,
  controllerActivity,
  proposalView,
  proverBudget,
  proverCapacity,
  proverPhaseLabel,
  snapshotAgeMs,
  stopReasonView,
} from "../../src/core/proverProgress";
import { Card, Notice, Pill, duration } from "../components";
import { useStore } from "../store";
import { post } from "../vscodeApi";
import { ProverBudget } from "./ProverBudget";
import { ProverChanges } from "./ProverChanges";
import { ProverController } from "./ProverController";
import { ProverDag } from "./ProverDag";
import { ProverGuidance } from "./ProverGuidance";
import { ProverJobs } from "./ProverJobs";
import { ProverPlan } from "./ProverPlan";
import { clock } from "./proverFormat";

/** Refresh cadence while a run is live; the host serves an unchanged state file from cache. */
const REFRESH_MS = 3000;

/** A ticking clock so elapsed times and durations advance between snapshots. */
function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (intervalMs <= 0) return;
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

function Workspace({ state, staleError }: { state: ProverSnapshot; staleError: string }) {
  const now = useNow(state.terminal ? 0 : 1000);
  const lifecycle = useMemo(() => {
    const context = { jobs: state.jobs, operations: state.operations, terminal: state.terminal, nodes: state.dag.nodes };
    return (node: ProverNode) => nodeLifecycle(node, context);
  }, [state]);
  const counts = useMemo(() => proofStateCounts(state.dag.nodes, state.jobs, state.terminal, state.operations), [state]);
  const budget = proverBudget(state, now);
  const capacity = useMemo(() => proverCapacity(state), [state]);
  const controller = controllerActivity(state, now);
  const stop = useMemo(() => stopReasonView(state), [state]);
  const proposal = useMemo(() => proposalView(state), [state]);
  const age = snapshotAgeMs(state, now);
  const open = (path: string, line?: number | null, baselinePath?: string) =>
    post({ type: "openProverFile", runId: state.run_id, path, line: line && line > 0 ? line : undefined, baselinePath });
  const focusJob = (jobId: string) => document.getElementById(`prover-job-${jobId}`)?.scrollIntoView({ block: "center" });

  return <>
    <Card title="Proof workspace" subtitle={`${state.mode || "standard"} · ${proverPhaseLabel(state)}${state.updated_at ? ` · snapshot ${clock(state.updated_at)}` : ""}`} actions={<Pill>{proofStateSummary(counts)}</Pill>}>
      {stop && <Notice tone={stop.tone}>
        <strong>{stop.title}.</strong> {stop.message}
        {stop.scopeLabel && <> Stopped by {stop.scopeLabel}{stop.usage ? ` (${stop.usage})` : ""}.</>}
        {stop.campaign && <> {stop.campaign}</>}
        {stop.unresolved.length > 0 && <> {stop.unresolved.length} theorem{stop.unresolved.length === 1 ? " remains" : "s remain"} unproved.</>}
        {stop.nextStep && <p><strong>Next step:</strong> {stop.nextStep}</p>}
        {stop.legacy && <p className="muted">This snapshot predates recorded stop reasons; the scope is inferred from the run status alone.</p>}
      </Notice>}
      {!stop && state.error && <Notice tone="error">{state.error}{state.next_step && <p>{state.next_step}</p>}</Notice>}
      {!stop && !state.error && state.next_step && <Notice tone="info">{state.next_step}</Notice>}
      {staleError && <Notice tone="warn">The latest read failed: {staleError} Showing the last snapshot{state.updated_at ? ` from ${clock(state.updated_at)}` : ""}.</Notice>}
      {!staleError && !state.terminal && age !== null && age > STALE_AFTER_MS && <Notice tone="info">
        The controller has not published a newer snapshot for {duration(age / 1000)}. A long Lean check or provider wait looks like this too; elapsed time below continues from the snapshot's own clock.
      </Notice>}
      {state.disproof.certified && <Notice tone="error">The negation of {state.disproof.node_id || "the requested statement"} was independently verified.{state.disproof.evidence_path && <div><button className="btn ghost" onClick={() => open(state.disproof.evidence_path)}>Open disproof evidence</button></div>}</Notice>}
      {state.resumed_from && <div className="muted" style={{ marginBottom: 12 }}>Resumed from <span className="mono">{state.resumed_from}</span>. Budgets and proof progress continue from that run.</div>}
      <ProverBudget budget={budget} />
    </Card>

    <ProverController state={state} controller={controller} capacity={capacity} now={now} onJob={focusJob} />
    <ProverDag state={state} lifecycle={lifecycle} proposal={proposal} now={now} onOpen={open} onJob={focusJob} />
    <ProverPlan state={state} proposal={proposal} review={acceptedReview(state)} onOpen={open} />
    <div className="grid two prover-panels">
      <ProverJobs state={state} now={now} onOpen={open} onJob={focusJob} />
      <ProverChanges state={state} onOpen={open} onJob={focusJob} />
    </div>
    <ProverGuidance state={state} />
  </>;
}

export function ProverWorkspace({ runId }: { runId: string }) {
  const { proverStates } = useStore();
  const entry = proverStates[runId];
  const terminal = entry?.snapshot?.terminal;
  useEffect(() => {
    if (!runId) return;
    post({ type: "loadProver", runId });
    if (terminal) return;
    const timer = setInterval(() => post({ type: "loadProver", runId }), REFRESH_MS);
    return () => clearInterval(timer);
  }, [runId, terminal]);
  if (!runId) return null;
  if (!entry) return <Notice tone="info">Loading the recorded proof state…</Notice>;
  if (!entry.snapshot) {
    return entry.error ? <Notice tone="info">Prover workspace unavailable: {entry.error}</Notice> : null;
  }
  return <Workspace key={runId} state={entry.snapshot} staleError={entry.error} />;
}
