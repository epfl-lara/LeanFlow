/** Present the native prover's primary controls using the installed CLI's catalog. */
import { isExtensionEditableKnob } from "../../src/core/launch";
import { PROVER_KNOB_LABELS } from "../../src/core/profileSurfaces";
import { Card, Field } from "../components";
import { useStore } from "../store";
import { KnobControl } from "./KnobsView";

/** Suggested native levels; saved runtime-specific values remain visible. */
const REASONING_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"];

const BUDGETS = ["AGENT_CALLS", "TOKENS", "WALL_TIME_S"];

export function ProverSettings() {
  const { app, view, setForm } = useStore();
  const form = view.form;
  const specs = new Map(app?.catalog?.groups.flatMap((group) => group.flags).filter(isExtensionEditableKnob).map((flag) => [flag.name, flag]));
  const profile = app?.profiles?.profiles.find((item) => item.name === form.profile);
  if (form.kind !== "prove" || !specs.has("LEANFLOW_PROVER_MODE")) return null;
  const effectiveMode = form.overrides.LEANFLOW_PROVER_MODE ?? profile?.overrides.LEANFLOW_PROVER_MODE
    ?? (form.research || form.agents > 1 ? "research" : specs.get("LEANFLOW_PROVER_MODE")!.default);

  /** Keep profile inheritance and the legacy research switches consistent with the form. */
  function control(suffix: string, shortLabel?: string, hint?: string) {
    const name = `LEANFLOW_PROVER_${suffix}`;
    const spec = specs.get(name);
    if (!spec) return null;
    const label = PROVER_KNOB_LABELS[name] ?? spec.name;
    const value = form.overrides[name] ?? profile?.overrides[name]
      ?? (suffix === "MODE" && (form.research || form.agents > 1) ? "research" : undefined)
      ?? (suffix === "PARALLELISM" && form.researchWorkers !== null ? String(form.researchWorkers)
        : suffix === "PARALLELISM" && form.agents > 1 ? String(form.agents) : undefined);
    const inherited = form.overrides[name] === undefined && profile?.overrides[name] !== undefined;
    const onChange = (next: string | undefined) => {
      const overrides = { ...form.overrides };
      if (next === undefined) delete overrides[name]; else overrides[name] = next;
      // The visible mode and legacy CLI switch describe the same launch.
      const mode = next ?? profile?.overrides[name] ?? spec.default;
      if (suffix === "PARALLELISM") overrides.LEANFLOW_PROVER_MODE = effectiveMode;
      setForm(suffix === "MODE" || suffix === "PARALLELISM"
        ? { overrides, research: suffix === "MODE" ? mode === "research" : effectiveMode === "research", agents: 1, researchWorkers: null }
        : { overrides });
    };
    const isEffort = suffix.endsWith("REASONING_EFFORT");
    const efforts = spec.choices?.filter(Boolean) ?? REASONING_EFFORTS;
    const inheritLabel = suffix === "ORCHESTRATOR_REASONING_EFFORT" ? "Use prover effort" : "Use launch default";
    return <Field key={name} label={shortLabel ?? label} source={inherited ? "Profile" : form.overrides[name] !== undefined ? "Override" : undefined} hint={hint ?? spec.summary}>
      {isEffort ? <select
        aria-label={name}
        value={value ?? "__default__"}
        onChange={(event) => onChange(event.target.value === "__default__" ? undefined : event.target.value)}
      >
        <option value="__default__">{profile?.overrides[name] ? `Use profile (${profile.overrides[name]})` : inheritLabel}</option>
        {(profile?.overrides[name] || value === "") && <option value="">{inheritLabel}</option>}
        {efforts.map((effort) => <option key={effort} value={effort}>{effort === "xhigh" ? "Extra high" : effort[0].toUpperCase() + effort.slice(1)}</option>)}
        {value && !efforts.includes(value) && <option value={value}>{value}</option>}
      </select> : <KnobControl spec={spec} value={value} onChange={onChange} />}
    </Field>;
  }

  return <Card title="Prover setup" subtitle="Choose how LeanFlow plans and proves. Set the model and reasoning effort separately for each role.">
    {profile && <div className="prover-profile-note">Profile <strong>{profile.name}</strong><span>Changes here apply to this launch.</span></div>}
    <div className="launch-fields">
      {control("MODE", "Mode", "Research plans and schedules a theorem graph.")}
      {control("SEARCH_ORDER", "Dependency order", "Choose where work begins in the theorem graph.")}
    </div>
    <section className="prover-role" aria-label="Prover settings">
      <div className="prover-role-heading"><strong>Prover</strong><span>Works on individual proof goals</span></div>
      <div className="launch-fields">
        {control("PROVIDER", "Backend")}
        {control("MODEL", "Model", "Leave empty to use the launch model above.")}
        {control("REASONING_EFFORT", "Reasoning effort", "Controls the prover's reasoning effort.")}
      </div>
    </section>
    <section className="prover-role" aria-label="Orchestrator settings">
      <div className="prover-role-heading"><strong>Orchestrator</strong><span>Plans, reviews, and revises the graph</span></div>
      <div className="launch-fields">
        {control("ORCHESTRATOR_PROVIDER", "Backend")}
        {control("ORCHESTRATOR_MODEL", "Model", "Leave empty to use the prover model.")}
        {control("ORCHESTRATOR_REASONING_EFFORT", "Reasoning effort", "Controls planning, review, and research effort.")}
      </div>
    </section>
    <div className="launch-fields">
      {control("PARALLELISM", "Concurrent prover jobs", "Maximum proof jobs running at the same time.")}
    </div>
    <details className="launch-details">
      <summary>Global limits (optional)</summary>
      <p className="muted">Empty means unlimited. One CLI invocation counts as one agent call. Token limits stop future calls; an active call may overshoot.</p>
      <div className="launch-fields">{BUDGETS.map((suffix) => control(suffix))}</div>
    </details>

  </Card>;
}
