/** Interactive theorem graph; geometry is stable across polling and proof-state updates. */
import { useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ProverDag, ProverNode } from "../../src/core/prover";
import { GRAPH_NODE_HEIGHT, GRAPH_NODE_WIDTH, layoutProverGraph, PROOF_STATES, type NodeLifecycle, type ProofState } from "../../src/core/proverGraph";

export function ProverGraph({ dag, lifecycle, selected, search, onSelect }: {
  dag: ProverDag; lifecycle: (node: ProverNode) => NodeLifecycle; selected: string; search: string; onSelect: (id: string) => void;
}) {
  const graph = useMemo(() => layoutProverGraph(dag), [dag]);
  const marker = useId().replace(/:/g, "");
  const viewport = useRef<HTMLDivElement>(null);
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const [width, setWidth] = useState(800);
  const [zoom, setZoom] = useState<number | null>(null);
  useLayoutEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const observer = new ResizeObserver(() => setWidth(element.clientWidth));
    observer.observe(element);
    setWidth(element.clientWidth);
    return () => observer.disconnect();
  }, []);
  const fit = Math.min(1, Math.max(.15, (width - 16) / graph.width));
  const scale = zoom ?? fit;
  const query = search.trim().toLowerCase();
  const states = new Map(graph.nodes.map(({ node }) => [node.id, lifecycle(node)]));
  const matches = new Set(graph.nodes.filter(({ node }) => `${node.name} ${node.file} ${node.status} ${states.get(node.id)!.label} ${PROOF_STATES[states.get(node.id)!.state].label}`.toLowerCase().includes(query)).map(({ node }) => node.id));
  const related = new Set([selected, ...graph.edges.filter((edge) => edge.from === selected || edge.to === selected).flatMap((edge) => [edge.from, edge.to])]);
  const counts = Object.fromEntries(Object.keys(PROOF_STATES).map((key) => [key, 0])) as Record<ProofState, number>;
  graph.nodes.forEach(({ node }) => counts[states.get(node.id)!.state]++);
  const focusSelected = () => buttons.current.get(selected)?.scrollIntoView({ block: "center", inline: "center", behavior: "auto" });

  return <div className="prover-graph">
    <div className="prover-graph-toolbar">
      <span className="muted">Goal ↓ prerequisite · {graph.nodes.length} theorems · {graph.edges.length} dependencies</span>
      <div className="row tight">
        <button className="btn ghost" aria-label="Zoom out graph" disabled={scale <= .15} onClick={() => setZoom(Math.max(.15, scale / 1.25))}>−</button>
        <span className="prover-graph-zoom">{Math.round(scale * 100)}%</span>
        <button className="btn ghost" aria-label="Zoom in graph" disabled={scale >= 1.75} onClick={() => setZoom(Math.min(1.75, scale * 1.25))}>+</button>
        <button className="btn ghost" onClick={() => { setZoom(null); viewport.current?.scrollTo(0, 0); }}>Fit width</button>
        <button className="btn ghost" disabled={!selected} onClick={focusSelected}>Locate selected</button>
        {query && <button className="btn ghost" disabled={!matches.size} onClick={() => {
          const ids = [...matches];
          const id = ids[(ids.indexOf(selected) + 1) % ids.length];
          onSelect(id);
          buttons.current.get(id)?.scrollIntoView({ block: "center", inline: "center" });
        }}>Next match</button>}
      </div>
    </div>
    <div className="prover-graph-legend" aria-label="Theorem status legend">
      {(Object.keys(PROOF_STATES) as ProofState[]).map((key) => <span key={key} className={`prover-graph-state ${key}`}><span aria-hidden="true">{PROOF_STATES[key].icon}</span> {PROOF_STATES[key].label} <strong>{counts[key]}</strong></span>)}
    </div>
    {(graph.cyclic || graph.missingDependencies > 0) && <p role="status" className="prover-graph-warning">{graph.cyclic && "The recorded graph contains a cycle. "}{graph.missingDependencies > 0 && `${graph.missingDependencies} dependencies have no recorded statement. `}Inspect the DAG before trusting its dependency order.</p>}
    {query && <p role="status" className="muted">{matches.size} matching theorems. Other nodes remain visible to preserve dependency context.</p>}
    <div className="prover-graph-viewport" ref={viewport} role="region" aria-label="Interactive theorem dependency graph" tabIndex={0}>
      <div style={{ position: "relative", overflow: "hidden", width: graph.width * scale, height: graph.height * scale, minWidth: "100%" }}>
        <div className="prover-graph-canvas" style={{ width: graph.width, height: graph.height, left: Math.max(0, (width - graph.width * scale) / 2), transform: `scale(${scale})` }}>
          <svg className="prover-graph-edges" width={graph.width} height={graph.height} aria-hidden="true">
            <defs><marker id={marker} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="strokeWidth"><path d="M 0 0 L 8 4 L 0 8 z" fill="context-stroke" /></marker></defs>
            {graph.edges.map((edge) => <path key={`${edge.from}\0${edge.to}`} d={edge.path} className={`prover-graph-edge ${edge.from === selected || edge.to === selected ? "highlighted" : ""}`} markerEnd={`url(#${marker})`} />)}
          </svg>
          {graph.nodes.map(({ node, x, y }) => {
            const life = states.get(node.id)!;
            const status = PROOF_STATES[life.state];
            const goal = dag.roots.includes(node.id);
            return <button key={node.id} ref={(element) => { if (element) buttons.current.set(node.id, element); else buttons.current.delete(node.id); }}
              className={`prover-graph-card ${life.state} ${node.id === selected ? "selected" : ""} ${related.has(node.id) ? "related" : ""} ${query && !matches.has(node.id) ? "dimmed" : ""} ${query && matches.has(node.id) ? "matched" : ""}`}
              style={{ left: x, top: y, width: GRAPH_NODE_WIDTH, height: GRAPH_NODE_HEIGHT }}
              aria-pressed={node.id === selected} aria-label={`${node.name}: ${life.label}${goal ? ", goal" : ""}`}
              title={`${node.name}\n${life.label} (${status.label})\n${life.detail}\n${node.module || node.file}`} onClick={() => onSelect(node.id)}>
              <span className="prover-graph-card-top"><span>{goal ? "GOAL" : "LEMMA"}</span><span className={`prover-graph-state ${life.state}`}><span aria-hidden="true">{status.icon}</span> {life.label}</span></span>
              <strong>{node.name}</strong><small>{node.module || node.file || "Placement pending"}</small>
            </button>;
          })}
        </div>
      </div>
    </div>
    <div className="muted prover-graph-hint">Select a theorem to inspect its statement and proof file below. Scroll to explore; use Tab and Enter to select nodes.</div>
  </div>;
}
