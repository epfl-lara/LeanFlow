/** The accepted PLAN, kept apart from a draft proposal and its review verdict. */
import type { ProverSnapshot } from "../../src/core/prover";
import type { ProposalView } from "../../src/core/proverProgress";
import { Card, Notice } from "../components";

/** Render plan structure as React text only; source HTML and links cannot execute. */
export function PlanContent({ text }: { text: string }) {
  let code = false;
  return <div className="prover-plan">{text.split("\n").map((line, index) => {
    if (line.startsWith("```")) { code = !code; return null; }
    if (code) return <div className="prover-plan-code" key={index}>{line || " "}</div>;
    if (/^#{1,3}\s/.test(line)) return <h3 key={index}>{line.replace(/^#+\s/, "")}</h3>;
    if (/^\s*[-*]\s/.test(line)) return <div className="prover-plan-item" key={index}>• {line.replace(/^\s*[-*]\s/, "")}</div>;
    return line ? <p key={index}>{line}</p> : <div className="prover-plan-break" key={index} />;
  })}</div>;
}

export function ProverPlan({ state, proposal, review, onOpen }: {
  state: ProverSnapshot; proposal: ProposalView | null; review: string; onOpen: (path: string) => void;
}) {
  return <>
    <Card title="Proof plan" subtitle="The orchestrator's current durable plan. Research results and user guidance are appended to it." actions={state.plan_path ? <button className="btn ghost" onClick={() => onOpen(state.plan_path)}>Open PLAN.md</button> : undefined}>
      {review && <Notice tone="info"><strong>Accepted by review:</strong> {review}</Notice>}
      {state.plan_markdown ? <PlanContent text={state.plan_markdown} /> : <div className="muted">No plan recorded yet.</div>}
    </Card>
    {proposal && (proposal.plan || proposal.critique) && <Card title="Proposed plan (draft)" subtitle={`${proposal.label}. ${proposal.planNote}`}>
      {proposal.critique && <Notice tone={proposal.status === "rejected" ? "error" : "info"}><strong>Review verdict:</strong> {proposal.critique}</Notice>}
      {proposal.plan ? <PlanContent text={proposal.plan} /> : <div className="muted">The proposal carries no plan text.</div>}
    </Card>}
  </>;
}
