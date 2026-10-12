/** The complete campaign budget: spend, reservations, availability, time, and allowances. */
import { proverCost, type BudgetView } from "../../src/core/proverProgress";
import { Stat } from "../components";
import { HELP } from "../help";
import { metric, ratio, seconds } from "./proverFormat";

export function ProverBudget({ budget }: { budget: BudgetView }) {
  if (budget.nativeBackend) return <>
    <div className="stats" aria-label="Global campaign limits">
      <Stat label="Agent calls / limit" help={HELP.agentCalls} value={`${metric(budget.usedCalls)} / ${budget.totalCalls === null ? "∞" : metric(budget.totalCalls)}`} />
      <Stat label="Active time / limit" help={HELP.activeTime} value={`${seconds(budget.elapsedS)} / ${budget.wallTimeS === null ? "∞" : seconds(budget.wallTimeS)}`} />
      <Stat label="Reported input tokens" help={HELP.tokens} value={metric(budget.tokens.input)} />
      <Stat label="Reported output tokens" help={HELP.tokens} value={metric(budget.tokens.output)} />
      <Stat label="Global token limit" help={HELP.tokens} value={budget.tokenLimit === null ? "∞" : metric(budget.tokenLimit)} />
      <Stat label="Token coverage" help={HELP.tokenCoverage} value={budget.usageComplete === true ? "Complete" : "Incomplete or unknown"} small />
      {budget.roleSessions.map(usage => <Stat key={usage.role} help={`${HELP.agentCalls} ${HELP.reservations}`}
        label={`${usage.role === "negation" ? "Refutation" : usage.role === "orchestrator" ? "Orchestrator" : "Prover"} calls / limit`}
        value={`${metric(usage.used)} / ${usage.limit === null ? "∞" : metric(usage.limit)}${usage.reserved ? ` · ${metric(usage.reserved)} reserved` : ""}`} small />)}
      <Stat label="Proof-direction changes / limit" help={HELP.directionChanges} value={`${metric(budget.planRefinements.used)} / ${budget.planRefinements.max === null ? "∞" : metric(budget.planRefinements.max)}`} small />
      <Stat label="Recovery decisions / limit" help={HELP.recovery} value={`${metric(budget.decompositions.used)} / ${budget.decompositions.max === null ? "∞" : metric(budget.decompositions.max)}`} small />
      <Stat label="Graph nodes / limit" help={HELP.graph} value={ratio(budget.nodes.used, budget.nodes.max)} small />
      <Stat label="Standard retries per goal" help={HELP.retries} value={budget.maxRestartsPerNode === null ? "∞" : metric(budget.maxRestartsPerNode)} small />
    </div>
    <p className="muted">One process = one agent call. Its model replies and tool steps stay inside that call. ∞ means no cap; — means unknown. Token limits stop future calls; active calls can exceed them.</p>
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
