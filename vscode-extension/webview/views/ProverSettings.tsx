/** Present the bounded prover's primary controls using the installed CLI's catalog. */
import { isExtensionEditableKnob } from "../../src/core/launch";
import { Card, Field } from "../components";
import { useStore } from "../store";
import { KnobControl } from "./KnobsView";

const CONTROLS: [string, string][] = [
  ["MODE", "Prover mode"], ["SEARCH_ORDER", "Dependency order"],
  ["JOB_API_CALLS", "API calls per prover pass"], ["MAX_RESTARTS", "Maximum restarts per node"],
  ["PLAN_REFINEMENTS", "Plan refinement limit"], ["PARALLELISM", "Concurrent prover jobs"],
  ["MODEL", "Prover model"], ["ORCHESTRATOR_MODEL", "Orchestrator model"],
  ["CONTEXT_TOKENS", "Prover context tokens"], ["ORCHESTRATOR_CONTEXT_TOKENS", "Orchestrator context tokens"],
  ["COMPRESSION", "Context compression"],
];

export function ProverSettings() {
  const { app, view, setForm } = useStore();
  const form = view.form;
  const specs = new Map(app?.catalog?.groups.flatMap((group) => group.flags).filter(isExtensionEditableKnob).map((flag) => [flag.name, flag]));
  const profile = app?.profiles?.profiles.find((item) => item.name === form.profile);
  if (form.kind !== "prove" || !specs.has("LEANFLOW_PROVER_MODE")) return null;

  return <Card title="Prover design" subtitle="Standard uses one prover. Research plans and schedules a theorem DAG. Budgets persist across local decomposition.">
    <div className="grid two">{CONTROLS.map(([suffix, label]) => {
      const name = `LEANFLOW_PROVER_${suffix}`;
      const spec = specs.get(name);
      if (!spec) return null;
      const value = form.overrides[name] ?? profile?.overrides[name];
      return <Field key={name} label={label} hint={spec.summary}>
        <KnobControl spec={spec} value={value} onChange={(next) => {
          const overrides = { ...form.overrides };
          if (next === undefined) delete overrides[name]; else overrides[name] = next;
          // The visible mode and legacy CLI switch describe the same launch.
          const mode = next ?? profile?.overrides[name] ?? spec.default;
          setForm(suffix === "MODE" ? { overrides, research: mode === "research" } : { overrides });
        }} />
      </Field>;
    })}</div>
  </Card>;
}
