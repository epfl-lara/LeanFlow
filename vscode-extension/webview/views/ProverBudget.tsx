/** The complete campaign budget: spend, reservations, availability, time, and allowances. */
import { proverCost, type BudgetView } from "../../src/core/proverProgress";
import { Stat } from "../components";
import { metric, ratio, seconds } from "./proverFormat";

export function ProverBudget({ budget }: { budget: BudgetView }) {
  if (budget.nativeBackend) return <>
    <div className="stats" aria-label="Global campaign limits">
      <Stat label="Agent calls / limit" value={`${metric(budget.usedCalls)} / ${budget.totalCalls === null ? "Unlimited" : metric(budget.totalCalls)}`} />
      <Stat label="Active time / limit" value={`${seconds(budget.elapsedS)} / ${budget.wallTimeS === null ? "Unlimited" : seconds(budget.wallTimeS)}`} />
      <Stat label="Reported input tokens" value={metric(budget.tokens.input)} />
      <Stat label="Reported output tokens" value={metric(budget.tokens.output)} />
      <Stat label="Global token limit" value={budget.tokenLimit === null ? "Unlimited" : metric(budget.tokenLimit)} />
      <Stat label="Token coverage" value={budget.usageComplete === true ? "Complete" : "Incomplete or unknown"} small />
    </div>
    <p className="muted">One native CLI invocation counts as one agent call. Token limits stop future calls; active calls can overshoot. Missing usage stays unknown. Native runtimes own sampling and context compression.</p>
  </>;

  const reserved = budget.reservedCalls === null
    ? "—"
    : `${metric(budget.reservedCalls)}${budget.remainingReservedCalls !== null ? ` · ${metric(budget.remainingReservedCalls)} unspent` : ""}`;
  return <>
    <div className="stats" aria-label="Campaign budget">
      <Stat label="API calls used / total" value={ratio(budget.usedCalls, budget.totalCalls)} />
      <Stat label="Available for new work" value={metric(budget.availableCalls)} />
      <Stat label="Reserved by running jobs" value={reserved} small />
      <Stat label={budget.elapsedAdvancing ? "Elapsed / limit (advancing)" : "Elapsed / limit"} value={`${seconds(budget.elapsedS)} / ${seconds(budget.wallTimeS)}`} small />
      <Stat label="Plan refinements" value={ratio(budget.planRefinements.used, budget.planRefinements.max)} small />
      <Stat label="Decompositions" value={ratio(budget.decompositions.used, budget.decompositions.max)} small />
      <Stat label="Theorems / limit" value={ratio(budget.nodes.used, budget.nodes.max)} small />
      <Stat label="Restarts per theorem" value={metric(budget.maxRestartsPerNode)} small />
      <Stat label="Input tokens" value={metric(budget.tokens.input)} />
      <Stat label="Output tokens" value={metric(budget.tokens.output)} />
      <Stat label="Cost" value={proverCost({cost_usd: budget.cost.usd, cost_source: budget.cost.source, cost_complete: budget.cost.complete})} small />
    </div>
    <div className="muted" style={{ marginTop: 8 }}>
      Reserved counts every outstanding allocation, including calls already spent inside running jobs, so it is
      never subtracted from spend again; availability for new work is what the runtime reports, or — when it does not.
      Each planning, review, or research stage may use up to {metric(budget.orchestratorCalls)} calls; each prover
      pass up to {metric(budget.jobCalls)}, retained across local decomposition. Unknown usage and cost stay —.
    </div>
  </>;
}
