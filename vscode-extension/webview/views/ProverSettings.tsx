/** Present the bounded prover's primary controls using the installed CLI's catalog. */
import { isExtensionEditableKnob } from "../../src/core/launch";
import { PROVER_KNOB_LABELS } from "../../src/core/profileSurfaces";
import { Card, Field } from "../components";
import { useStore } from "../store";
import { KnobControl } from "./KnobsView";

/** Catalog suffixes shown on the launch card, in reading order. */
const CONTROLS = [
  "MODE", "SEARCH_ORDER", "JOB_API_CALLS", "MAX_RESTARTS", "PLAN_REFINEMENTS", "PARALLELISM",
  "TOTAL_API_CALLS", "ORCHESTRATOR_API_CALLS", "WALL_TIME_S",
  "MODEL", "ORCHESTRATOR_MODEL", "CONTEXT_TOKENS", "ORCHESTRATOR_CONTEXT_TOKENS", "COMPRESSION",
];

export function ProverSettings() {
  const { app, view, setForm } = useStore();
  const form = view.form;
  const specs = new Map(app?.catalog?.groups.flatMap((group) => group.flags).filter(isExtensionEditableKnob).map((flag) => [flag.name, flag]));
  const profile = app?.profiles?.profiles.find((item) => item.name === form.profile);
  if (form.kind !== "prove" || !specs.has("LEANFLOW_PROVER_MODE")) return null;
  const effectiveMode = form.overrides.LEANFLOW_PROVER_MODE ?? profile?.overrides.LEANFLOW_PROVER_MODE
    ?? (form.research || form.agents > 1 ? "research" : specs.get("LEANFLOW_PROVER_MODE")!.default);

  return <Card title="Prover design" subtitle="Standard uses one prover. Research plans and schedules a theorem DAG. Budgets persist across local decomposition. Provider and reasoning effort come from the launch above and are shared by every role.">
    <div className="grid two">{CONTROLS.map((suffix) => {
      const name = `LEANFLOW_PROVER_${suffix}`;
      const spec = specs.get(name);
      if (!spec) return null;
      const label = PROVER_KNOB_LABELS[name] ?? spec.name;
      const value = form.overrides[name] ?? profile?.overrides[name]
        ?? (suffix === "MODE" && (form.research || form.agents > 1) ? "research" : undefined)
        ?? (suffix === "PARALLELISM" && form.researchWorkers !== null ? String(form.researchWorkers)
          : suffix === "PARALLELISM" && form.agents > 1 ? String(form.agents) : undefined);
      const inherited = form.overrides[name] === undefined && profile?.overrides[name] !== undefined;
      return <Field key={name} label={inherited ? `${label} (from profile ${profile?.name ?? ""})` : label} hint={spec.summary}>
        <KnobControl spec={spec} value={value} onChange={(next) => {
          const overrides = { ...form.overrides };
          if (next === undefined) delete overrides[name]; else overrides[name] = next;
          // The visible mode and legacy CLI switch describe the same launch.
          const mode = next ?? profile?.overrides[name] ?? spec.default;
          if (suffix === "PARALLELISM") overrides.LEANFLOW_PROVER_MODE = effectiveMode;
          setForm(suffix === "MODE" || suffix === "PARALLELISM"
            ? { overrides, research: suffix === "MODE" ? mode === "research" : effectiveMode === "research", agents: 1, researchWorkers: null }
            : { overrides });
        }} />
      </Field>;
    })}</div>
  </Card>;
}
