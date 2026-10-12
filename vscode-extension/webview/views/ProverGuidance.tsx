/** Queue guidance for the orchestrator or a live prover at its next decision boundary. */
import { useEffect, useRef, useState } from "react";

import { proverDefaultGuidanceRecipient, proverGuidanceUpdate, proverJobAcceptsGuidance, type ProverSnapshot } from "../../src/core/prover";
import type { HostMessage } from "../../src/core/types";
import { Card, Notice } from "../components";
import { post } from "../vscodeApi";

export function ProverGuidance({ state }: { state: ProverSnapshot }) {
  const [recipient, setRecipient] = useState("");
  const [guidance, setGuidance] = useState("");
  const [sending, setSending] = useState(false);
  const [guidanceError, setGuidanceError] = useState("");
  const pendingGuidance = useRef<{ runId: string; requestId: string; message: string } | null>(null);
  useEffect(() => {
    const receive = (event: MessageEvent<HostMessage>) => {
      if (event.data.type !== "proverMessageResult") return;
      const reply = event.data;
      const pending = pendingGuidance.current;
      const update = proverGuidanceUpdate("", pending, reply);
      if (!update) return;
      setGuidance((draft) => proverGuidanceUpdate(draft, pending, reply)!.draft);
      setGuidanceError(update.error);
      pendingGuidance.current = null;
      setSending(false);
    };
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, []);
  const agents = [...new Set(state.jobs.filter(proverJobAcceptsGuidance).map((job) => job.agent_id).filter(Boolean))];
  const addressedAgent = recipient === "orchestrator" || agents.includes(recipient) ? recipient : proverDefaultGuidanceRecipient(state);

  return <Card title="Send guidance" subtitle={state.terminal ? "This run has finished." : "Guidance is saved to the selected run and delivered between agent decisions."}>
    {guidanceError && <Notice tone="error">{guidanceError} Your message has been kept below.</Notice>}
    <div className="row tight"><select aria-label="Guidance recipient" value={addressedAgent} disabled={state.terminal} onChange={(event) => setRecipient(event.target.value)}>
      <option value="orchestrator">{state.mode === "standard" ? "Workflow manager (future work)" : "Orchestrator"}</option>
      {agents.map((agent) => <option key={agent} value={agent}>{agent}</option>)}
    </select></div>
    {addressedAgent === "orchestrator" && <p className="muted">Manager guidance updates the shared plan for future work. To guide a running proof, choose its prover job.</p>}
    <textarea aria-label="Guidance message" maxLength={8192} value={guidance} disabled={state.terminal} placeholder="Share a mathematical observation, useful resource, or change of direction…" onChange={(event) => setGuidance(event.target.value)} />
    <button className="btn" disabled={state.terminal || sending || !guidance.trim()} onClick={() => {
      const request = { runId: state.run_id, requestId: crypto.randomUUID(), message: guidance };
      pendingGuidance.current = request;
      setSending(true);
      setGuidanceError("");
      post({ type: "proverMessage", ...request, agentId: addressedAgent });
    }}>{sending ? "Sending…" : "Send guidance"}</button>
  </Card>;
}
