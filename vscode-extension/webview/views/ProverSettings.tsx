/** Present the native prover's primary controls using the installed CLI's catalog. */
import { isExtensionEditableKnob } from "../../src/core/launch";
import { BACKEND_IDS, BACKEND_LABELS, isBackend } from "../../src/core/providerCredentials";
import { changeNativeSelection, nativeSelections, ROLE_PREFIXES } from "../../src/core/providerModels";
import { PROVER_KNOB_LABELS } from "../../src/core/profileSurfaces";
import { Card, Field } from "../components";
import { useStore } from "../store";
import { GlobalLimits } from "./GlobalLimits";
import { KnobControl } from "./KnobsView";
import { NativeEffortControl, NativeModelControl } from "./NativeModelControls";

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
    return <Field key={name} label={shortLabel ?? label} source={inherited ? "Profile" : form.overrides[name] !== undefined ? "Override" : undefined} hint={hint ?? spec.summary}>
      <KnobControl spec={spec} value={value} onChange={onChange} />
    </Field>;
  }

  /** Share runtime choices across roles without losing explicit profile inheritance. */
  function roleControl(role: "prover" | "orchestrator", suffix: "PROVIDER" | "MODEL" | "REASONING_EFFORT") {
    const name = ROLE_PREFIXES[role] + suffix;
    if (!specs.has(name)) return null;
    const value = form.overrides[name] ?? profile?.overrides[name];
    const source = form.overrides[name] !== undefined ? "Override" : profile?.overrides[name] !== undefined ? "Profile" : undefined;
    const selections = nativeSelections(form, app?.providers, profile?.overrides);
    const selected = selections[role];
    const parent = role === "prover" ? selections.launch : selections.prover;
    const parentLabel = role === "prover" ? "launch" : "prover";
    const inherited = nativeSelections({ ...form, overrides: { ...form.overrides, [name]: "" } }, app?.providers, profile?.overrides)[role];
    const onChange = (next: string | undefined) => {
      const overrides = { ...form.overrides };
      if (next === undefined) delete overrides[name]; else overrides[name] = next;
      setForm(suffix === "REASONING_EFFORT" ? { overrides } : changeNativeSelection(form, { overrides }, app?.providers, profile?.overrides));
    };
    const modelLabel = selected.provider === parent.provider
      ? `Use ${parentLabel} model (${inherited.model || "runtime chooses"})`
      : `Native default (${inherited.model || "not reported"})`;
    const effortLabel = role === "orchestrator" && selected.provider === parent.provider && inherited.explicitEffort && parent.explicitEffort
      ? `Use prover effort (${parent.explicitEffort})`
      : `${inherited.explicitEffort ? "Launch default" : "Native default"} (${inherited.effort || "runtime chooses"})`;
    const common = { ariaLabel: name, value, profileValue: profile?.overrides[name], source, catalog: selected.catalog, onChange };
    if (suffix === "MODEL") return <NativeModelControl key={name} {...common} label="Model" inheritedLabel={modelLabel} />;
    if (suffix === "REASONING_EFFORT") return <NativeEffortControl key={name} {...common} label="Reasoning effort" model={selected.model} effectiveEffort={selected.effort} inheritedLabel={effortLabel} />;
    const providerLabel = isBackend(parent.provider) ? BACKEND_LABELS[parent.provider] : "not available";
    return <Field key={name} label="Backend" source={source} hint="Uses this native agent's own login and configuration.">
      <select aria-label={name} value={value ?? "__default__"} onChange={event => onChange(event.target.value === "__default__" ? undefined : event.target.value)}>
        <option value="__default__">{profile?.overrides[name] ? `Use profile (${profile.overrides[name]})` : `Use ${parentLabel} backend (${providerLabel})`}</option>
        {(profile?.overrides[name] !== undefined || value === "") && <option value="">Use {parentLabel} backend ({providerLabel})</option>}
        {BACKEND_IDS.map(id => <option key={id} value={id}>{BACKEND_LABELS[id]}</option>)}
      </select>
    </Field>;
  }

  return <><GlobalLimits /><Card title="Prover setup" subtitle="Choose how LeanFlow plans and proves. Set the model and reasoning effort separately for each role.">
    {profile && <div className="prover-profile-note">Profile <strong>{profile.name}</strong><span>Changes here apply to this launch.</span></div>}
    <div className="launch-fields">
      {control("MODE", "Mode", "Research plans and schedules a theorem graph.")}
      {control("SEARCH_ORDER", "Dependency order", "Choose where work begins in the theorem graph.")}
    </div>
    <section className="prover-role" aria-label="Prover settings">
      <div className="prover-role-heading"><strong>Prover</strong><span>Works on individual proof goals</span></div>
      <div className="launch-fields">
        {roleControl("prover", "PROVIDER")}
        {roleControl("prover", "MODEL")}
        {roleControl("prover", "REASONING_EFFORT")}
      </div>
    </section>
    <section className="prover-role" aria-label="Orchestrator settings">
      <div className="prover-role-heading"><strong>Orchestrator</strong><span>Plans, reviews, and revises the graph</span></div>
      <div className="launch-fields">
        {roleControl("orchestrator", "PROVIDER")}
        {roleControl("orchestrator", "MODEL")}
        {roleControl("orchestrator", "REASONING_EFFORT")}
      </div>
    </section>
    <div className="launch-fields">
      {control("PARALLELISM", "Concurrent prover jobs", "Maximum proof jobs running at the same time.")}
    </div>
  </Card></>;
}
