/** Theorem dependencies: the canonical graph, an explicitly labelled proposal view, and node details. */
import { useMemo, useState } from "react";

import { proverTreeRows, type ProverNode, type ProverSnapshot } from "../../src/core/prover";
import { PROOF_STATES, proposalNodeLifecycle, type NodeLifecycle } from "../../src/core/proverGraph";
import { jobTimeline, type ProposalView } from "../../src/core/proverProgress";
import { Card, Notice } from "../components";
import { ProverGraph } from "./ProverGraph";
import { dotTone, lifecycleTone, metric, seconds } from "./proverFormat";

export function ProverDag({ state, lifecycle, proposal, now, onOpen, onJob }: {
  state: ProverSnapshot;
  lifecycle: (node: ProverNode) => NodeLifecycle;
  proposal: ProposalView | null;
  now: number;
  onOpen: (path: string, line?: number | null) => void;
  onJob: (jobId: string) => void;
}) {
  const [selected, setSelected] = useState("");
  const [search, setSearch] = useState("");
  const [dagView, setDagView] = useState<"graph" | "tree">("graph");
  const [source, setSource] = useState<"canonical" | "proposed">("canonical");
  const showingProposal = source === "proposed" && proposal !== null;
  const dag = showingProposal ? proposal.dag : state.dag;
  const classify = useMemo<(node: ProverNode) => NodeLifecycle>(() => showingProposal
    ? (node) => proposalNodeLifecycle(node, { jobs: state.jobs, operations: state.operations, terminal: state.terminal, canonical: state.dag })
    : lifecycle, [showingProposal, lifecycle, state]);
  const rows = useMemo(() => proverTreeRows(dag), [dag]);
  const node = dag.nodes.find((item) => item.id === selected) ?? dag.nodes[0];
  const life = node ? classify(node) : null;
  const shown = rows.filter(({ node: item }) => !search || `${item.name} ${item.file} ${item.status} ${classify(item).label}`.toLowerCase().includes(search.toLowerCase()));
  const timeline = life?.job ? jobTimeline(life.job, now) : null;

  return <Card title="Theorem dependencies" subtitle={showingProposal ? proposal.explanation : "Follow the proof from its goals to the lemmas they need. Only the independent verifier marks a theorem solved."}
    actions={<div className="row tight">
      {proposal && <div className="prover-view-toggle" role="group" aria-label="Graph source">
        <button className="btn ghost" aria-pressed={source === "canonical"} onClick={() => setSource("canonical")}>Canonical</button>
        <button className="btn ghost" aria-pressed={source === "proposed"} onClick={() => setSource("proposed")}>Proposed · {proposal.status}</button>
      </div>}
      <div className="prover-view-toggle" role="group" aria-label="Dependency view"><button className="btn ghost" aria-pressed={dagView === "graph"} onClick={() => setDagView("graph")}>Graph</button><button className="btn ghost" aria-pressed={dagView === "tree"} onClick={() => setDagView("tree")}>Tree</button></div>
      {state.dag_path && <button className="btn ghost" onClick={() => onOpen(state.dag_path)}>Open DAG</button>}
    </div>}>
    {proposal && !showingProposal && <div className="muted" style={{ marginBottom: 10 }}>A {proposal.label.toLowerCase()} with {proposal.newNodes} new statement{proposal.newNodes === 1 ? "" : "s"} exists. Switch to <strong>Proposed</strong> to inspect it; this canonical graph stays authoritative.</div>}
    {showingProposal && <Notice tone="warn">{proposal.label}. {proposal.newNodes} new and {proposal.existingNodes} existing statement{proposal.existingNodes === 1 ? "" : "s"}. Nothing here is scheduled or counted as proved until the controller publishes it.{proposal.critique && <p><strong>Review verdict:</strong> {proposal.critique}</p>}</Notice>}
    {dag.nodes.length === 0 ? <div className="empty">{showingProposal ? "The proposal has no statements." : "The plan is being prepared. Accepted statements appear here."}</div> : <div className={`prover-dag-layout ${dagView === "graph" ? "graph-view" : ""}`}>
      <div>
        <input type="text" aria-label="Find a theorem" placeholder="Find theorem, file, or status…" value={search} onChange={(event) => setSearch(event.target.value)} />
        {dagView === "graph" ? <ProverGraph dag={dag} lifecycle={classify} selected={node?.id ?? ""} search={search} onSelect={setSelected} /> : <div className="prover-tree" role="list" aria-label="Theorem dependencies">
          {shown.map(({ node: item, depth, reference, cycle }, index) => {
            const itemLife = classify(item);
            return <div key={`${item.id}-${index}`} role="listitem"><button
              className={`prover-node ${node?.id === item.id ? "selected" : ""}`} onClick={() => setSelected(item.id)}
              style={{ paddingLeft: `${12 + Math.min(depth, 12) * 18}px` }} aria-pressed={node?.id === item.id}>
              <span className={`prover-dot ${dotTone(item.status)}`} />
              <span className="prover-node-name">{reference ? "↗ " : depth ? "└ " : ""}{item.name}<small>{item.module || item.file || "Placement pending"}</small></span>
              <span className={`pill ${lifecycleTone(itemLife.state)}`}>{cycle ? "cycle" : itemLife.label}</span>
            </button></div>;
          })}
        </div>}
      </div>
      {node && life && <div className="prover-node-detail">
        <div className="row"><strong>{node.name}</strong><span className={`pill ${lifecycleTone(life.state)}`}><span aria-hidden="true">{PROOF_STATES[life.state].icon}</span> {life.label}</span></div>
        <div className="prover-lifecycle">
          <div>{life.detail}</div>
          <div className="muted">Recorded status <span className="mono">{node.status}</span>{node.kind ? ` · ${node.kind}` : ""}{node.original ? " · original statement" : ""}{node.attempts !== null ? ` · attempts ${node.attempts}` : ""}{node.holes ? ` · ${node.holes} hole${node.holes === 1 ? "" : "s"}` : ""}{node.candidate_count ? ` · ${node.candidate_count} retained candidate${node.candidate_count === 1 ? "" : "s"}` : ""}</div>
          {life.job && <div className="row tight">
            <button className="link mono" onClick={() => onJob(life.job!.id)}>{life.job.agent_id}</button>
            <span className="muted">{life.job.status || "queued"} · {metric(life.job.api_calls)} / {metric(life.job.api_budget)} calls{timeline?.durationS !== null && timeline ? ` · ${seconds(timeline.durationS)}` : ""}</span>
          </div>}
        </div>
        {node.file && <button className="link" onClick={() => onOpen(node.file, node.line_start)}>{node.file}{node.line_start ? `:${node.line_start}` : ""}</button>}
        <pre className="raw prover-statement">{node.statement || "Statement not yet recorded."}</pre>
        {node.informal_justification && <p>{node.informal_justification}</p>}
        {node.notes && <details><summary>Prover notes and check output</summary><pre className="raw prover-notes">{node.notes}</pre></details>}
        {life.state === "candidate" && <Notice tone="warn">Not counted as proved. {life.detail}</Notice>}
        <span className="label">Depends on</span>
        <div className="row tight">{node.dependencies.length ? node.dependencies.map((id) => <button className="btn ghost" key={id} onClick={() => setSelected(id)}>
          {dag.nodes.find((item) => item.id === id)?.name ?? id}
        </button>) : <span className="muted">No planned dependencies</span>}</div>
        {node.conditional_dependencies.length > 0 && <Notice tone="warn">Conditional on {node.conditional_dependencies.map((id) => dag.nodes.find((item) => item.id === id)?.name ?? id).join(", ")}. The final kernel gate must discharge these dependencies.</Notice>}
        <span className="label" style={{ marginTop: 12 }}>Used by</span>
        <div className="row tight">{dag.nodes.filter((item) => item.dependencies.includes(node.id)).map((item) => <button className="btn ghost" key={item.id} onClick={() => setSelected(item.id)}>{item.name}</button>)}</div>
      </div>}
    </div>}
  </Card>;
}
