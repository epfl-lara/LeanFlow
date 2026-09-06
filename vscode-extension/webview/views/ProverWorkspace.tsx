/** Inspect an exact prover run: dependency tree, durable plan, jobs, edits, and budgets. */
import { useEffect, useMemo, useState } from "react";

import { proverJobAcceptsGuidance, proverTreeRows, type ProverSnapshot } from "../../src/core/prover";
import { Card, Notice, Pill, Stat } from "../components";
import { useStore } from "../store";
import { post } from "../vscodeApi";

function metric(value: number | null): string {
  return value === null ? "—" : value.toLocaleString();
}

function statusTone(status: string): string {
  if (["proved", "verified", "completed", "succeeded"].includes(status)) return "ok";
  if (["failed", "invalidated", "disproved", "false", "rejected", "provider_error", "environment_error", "source_conflict", "verification_failed", "error"].includes(status)) return "err";
  if (["running", "proving", "candidate", "conditional", "provisional", "retry", "blocked", "budget_exhausted", "resume_pending"].includes(status)) return "warn";
  return "";
}

/** Render plan structure as React text only; source HTML and links cannot execute. */
function PlanContent({ text }: { text: string }) {
  let code = false;
  return <div className="prover-plan">{text.split("\n").map((line, index) => {
    if (line.startsWith("```")) { code = !code; return null; }
    if (code) return <div className="prover-plan-code" key={index}>{line || " "}</div>;
    if (/^#{1,3}\s/.test(line)) return <h3 key={index}>{line.replace(/^#+\s/, "")}</h3>;
    if (/^\s*[-*]\s/.test(line)) return <div className="prover-plan-item" key={index}>• {line.replace(/^\s*[-*]\s/, "")}</div>;
    return line ? <p key={index}>{line}</p> : <div className="prover-plan-break" key={index} />;
  })}</div>;
}

function Workspace({ state }: { state: ProverSnapshot }) {
  const { setView } = useStore();
  const [selected, setSelected] = useState("");
  const [search, setSearch] = useState("");
  const [recipient, setRecipient] = useState("orchestrator");
  const [guidance, setGuidance] = useState("");
  const rows = useMemo(() => proverTreeRows(state.dag), [state.dag]);
  const node = state.dag.nodes.find((item) => item.id === selected) ?? state.dag.nodes[0];
  const shown = rows.filter(({ node }) => !search || `${node.name} ${node.file} ${node.status}`.toLowerCase().includes(search.toLowerCase()));
  const proved = state.dag.nodes.filter((item) => ["proved", "verified", "completed"].includes(item.status)).length;
  const api = state.metrics;
  const open = (path: string, line?: number | null, baselinePath?: string) =>
    post({ type: "openProverFile", runId: state.run_id, path, line: line && line > 0 ? line : undefined, baselinePath });
  const agents = [...new Set(state.jobs.filter(proverJobAcceptsGuidance).map((job) => job.agent_id).filter(Boolean))];
  const addressedAgent = recipient === "orchestrator" || agents.includes(recipient) ? recipient : "orchestrator";

  return <>
    <Card title="Proof workspace" subtitle={`${state.mode || "standard"} · ${state.phase || "starting"}`} actions={<Pill>{proved}/{state.dag.nodes.length} verified</Pill>}>
      {state.error && <Notice tone="error">{state.error}{state.next_step && <p>{state.next_step}</p>}</Notice>}
      {!state.error && state.next_step && <Notice tone="info">{state.next_step}</Notice>}
      {state.disproof.certified && <Notice tone="error">The negation of {state.disproof.node_id || "the requested statement"} was independently verified.{state.disproof.evidence_path && <div><button className="btn ghost" onClick={() => open(state.disproof.evidence_path)}>Open disproof evidence</button></div>}</Notice>}
      {state.resumed_from && <div className="muted" style={{ marginBottom: 12 }}>Resumed from <span className="mono">{state.resumed_from}</span>. Budgets and proof progress continue from that run.</div>}
      <div className="stats">
        <Stat label="Plan refinements" value={`${metric(api.plan_refinements)} / ${metric(api.max_plan_refinements)}`} small />
        <Stat label="API calls" value={metric(api.api_calls)} />
        <Stat label="Input tokens" value={metric(api.input_tokens)} />
        <Stat label="Output tokens" value={metric(api.output_tokens)} />
        <Stat label={api.cost_complete === true ? "Cost" : "Reported cost (partial)"} value={api.cost_usd === null ? "—" : `$${api.cost_usd.toFixed(3)}`} />
      </div>
      <div className="muted" style={{ marginTop: 8 }}>Missing usage is shown as —. Candidates await independent verification; conditional proofs still depend on unfinished obligations.</div>
    </Card>

    <Card title="Theorem dependencies" subtitle="Goals lead to their dependencies. Shared dependencies link back to the same node."
      actions={state.dag_path ? <button className="btn ghost" onClick={() => open(state.dag_path)}>Open DAG</button> : undefined}>
      {state.dag.nodes.length === 0 ? <div className="empty">The plan is being prepared. Accepted statements appear here.</div> : <div className="prover-dag-layout">
        <div>
          <input type="text" aria-label="Find a theorem" placeholder="Find theorem, file, or status…" value={search} onChange={(event) => setSearch(event.target.value)} />
          <div className="prover-tree" role="list" aria-label="Theorem dependencies">
            {shown.map(({ node: item, depth, reference, cycle }, index) => <div key={`${item.id}-${index}`} role="listitem"><button
              className={`prover-node ${node?.id === item.id ? "selected" : ""}`} onClick={() => setSelected(item.id)}
              style={{ paddingLeft: `${12 + Math.min(depth, 12) * 18}px` }} aria-pressed={node?.id === item.id}>
              <span className={`prover-dot ${statusTone(item.status)}`} />
              <span className="prover-node-name">{reference ? "↗ " : depth ? "└ " : ""}{item.name}<small>{item.module || item.file || "Placement pending"}</small></span>
              <span className={`pill ${statusTone(item.status)}`}>{cycle ? "cycle" : item.status}</span>
            </button></div>)}
          </div>
        </div>
        {node && <div className="prover-node-detail">
          <div className="row"><strong>{node.name}</strong><span className={`pill ${statusTone(node.status)}`}>{node.status}</span></div>
          {node.file && <button className="link" onClick={() => open(node.file, node.line_start)}>{node.file}{node.line_start ? `:${node.line_start}` : ""}</button>}
          <pre className="raw prover-statement">{node.statement || "Statement not yet recorded."}</pre>
          {node.informal_justification && <p>{node.informal_justification}</p>}
          {node.notes && <p>{node.notes}</p>}
          {node.status === "candidate" && <Notice tone="warn">Candidate proof retained. This node is not counted as proved until independent verification succeeds after its dependencies are ready.</Notice>}
          <span className="label">Depends on</span>
          <div className="row tight">{node.dependencies.length ? node.dependencies.map((id) => <button className="btn ghost" key={id} onClick={() => setSelected(id)}>
            {state.dag.nodes.find((item) => item.id === id)?.name ?? id}
          </button>) : <span className="muted">No planned dependencies</span>}</div>
          {node.conditional_dependencies.length > 0 && <Notice tone="warn">Conditional on {node.conditional_dependencies.join(", ")}. The final kernel gate must discharge these dependencies.</Notice>}
          <span className="label" style={{ marginTop: 12 }}>Used by</span>
          <div className="row tight">{state.dag.nodes.filter((item) => item.dependencies.includes(node.id)).map((item) => <button className="btn ghost" key={item.id} onClick={() => setSelected(item.id)}>{item.name}</button>)}</div>
        </div>}
      </div>}
    </Card>

    <Card title="Proof plan" subtitle="The orchestrator's current durable plan." actions={state.plan_path ? <button className="btn ghost" onClick={() => open(state.plan_path)}>Open PLAN.md</button> : undefined}>
      {state.plan_markdown ? <PlanContent text={state.plan_markdown} /> : <div className="muted">No plan recorded yet.</div>}
    </Card>

    <div className="grid two prover-panels">
      <Card title="Agents and jobs" subtitle="Each prover retains its API budget across local decomposition.">
        {state.jobs.length === 0 && <div className="muted">No jobs launched yet.</div>}
        {state.jobs.map((job) => <div className="prover-job" id={`prover-job-${job.id}`} key={job.id} style={job.parent_job_id ? { marginLeft: 12, borderLeft: "2px solid var(--vscode-panel-border)", paddingLeft: 12 } : undefined}>
          <div className="row"><strong>{job.agent_id}</strong><span className={`pill ${statusTone(job.status)}`}>{job.status}</span></div>
          <div className="muted">{job.role || "prover"}{job.node_id ? ` · ${job.node_id}` : ""} · {metric(job.api_calls)} / {metric(job.api_budget)} calls</div>
          {job.parent_job_id && <div className="muted">Requested by <button className="link" onClick={() => document.getElementById(`prover-job-${job.parent_job_id}`)?.scrollIntoView({ block: "center" })}>{job.parent_job_id}</button></div>}
          {job.purpose && <details><summary>Job purpose</summary><p>{job.purpose}</p></details>}
          <div className="row tight">
            <button className="btn ghost" onClick={() => setView({ tab: "logs", logAgent: job.agent_id, logRaw: false, logFilter: [] })}>Events</button>
            {job.log_path && <button className="btn ghost" onClick={() => open(job.log_path)}>Log file</button>}
            {job.scratch_path && <button className="btn ghost" onClick={() => open(job.scratch_path)}>Scratch proof</button>}
            {job.result_path && <button className="btn ghost" onClick={() => open(job.result_path)}>Result report</button>}
          </div>
        </div>)}
      </Card>
      <Card title="Files changed" subtitle="Open source changes or compare with the recorded run baseline.">
        {state.changes.length === 0 && <div className="muted">No recorded edits yet.</div>}
        {state.changes.map((change) => <div className="prover-job" key={change.path}>
          <button className="link mono" onClick={() => open(change.path)}>{change.path}</button>
          <div className="row tight"><span className="pill">{change.status || "modified"}</span><span className="muted">{change.agent_id}</span>
            {change.baseline_path && <button className="btn ghost" onClick={() => open(change.path, null, change.baseline_path)}>View diff</button>}
          </div>
        </div>)}
      </Card>
    </div>

    <Card title="Send guidance" subtitle={state.terminal ? "This run has finished." : "Guidance is saved to the selected run and delivered between agent decisions."}>
      <div className="row tight"><select aria-label="Guidance recipient" value={addressedAgent} disabled={state.terminal} onChange={(event) => setRecipient(event.target.value)}>
        <option value="orchestrator">{state.mode === "standard" ? "Workflow manager" : "Orchestrator"}</option>
        {agents.map((agent) => <option key={agent} value={agent}>{agent}</option>)}
      </select></div>
      <textarea aria-label="Guidance message" maxLength={8192} value={guidance} disabled={state.terminal} placeholder="Share a mathematical observation, useful resource, or change of direction…" onChange={(event) => setGuidance(event.target.value)} />
      <button className="btn" disabled={state.terminal || !guidance.trim()} onClick={() => {
        post({ type: "proverMessage", runId: state.run_id, agentId: addressedAgent, message: guidance });
        setGuidance("");
      }}>Send guidance</button>
    </Card>
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
    const timer = setInterval(() => post({ type: "loadProver", runId }), 5000);
    return () => clearInterval(timer);
  }, [runId, terminal]);
  if (!runId || !entry) return null;
  if (entry.error) return <Notice tone="info">Prover workspace unavailable: {entry.error}</Notice>;
  if (!entry.snapshot) return null;
  return <Workspace key={runId} state={entry.snapshot} />;
}
