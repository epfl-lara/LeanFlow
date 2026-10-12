/** Present the native prover's primary controls using the installed CLI's catalog. */
import { isExtensionEditableKnob } from "../../src/core/launch";
import { BACKEND_IDS, BACKEND_LABELS, isBackend } from "../../src/core/providerCredentials";
import { changeNativeSelection, nativeSelections, ROLE_PREFIXES } from "../../src/core/providerModels";
import { PROVER_KNOB_LABELS } from "../../src/core/profileSurfaces";
import { Card, Field } from "../components";
import { InfoHelp } from "../InfoHelp";
import { HELP, LIMIT_HELP } from "../help";
import { useStore } from "../store";
import { KnobControl } from "./KnobsView";
import { NativeEffortControl, NativeModelControl } from "./NativeModelControls";

export function ProverSettings() {
  const { app, view, setForm } = useStore();
  const form = view.form;
  const specs = new Map(app?.catalog?.groups.flatMap((group) => group.flags).filter(isExtensionEditableKnob).map((flag) => [flag.name, flag]));
  const profile = app?.profiles?.profiles.find((item) => item.name === form.profile);
  if (form.kind !== "prove" || !specs.has("LEANFLOW_PROVER_MODE")) return null;
  const modeDefault = specs.get("LEANFLOW_PROVER_MODE")!.default;
  const profileResearch = profile?.overrides.LEANFLOW_RESEARCH_MODE;
  const profileMode = profile?.overrides.LEANFLOW_PROVER_MODE
    ?? (profileResearch?.trim() ? ["1", "true", "yes", "on"].includes(profileResearch.trim().toLowerCase()) ? "research" : "standard" : undefined);
  const inheritedResearch = form.overrides.LEANFLOW_RESEARCH_MODE ?? profile?.overrides.LEANFLOW_RESEARCH_MODE;
  const inheritedMode = profile?.overrides.LEANFLOW_PROVER_MODE
    ?? (form.research || (!form.noParallel && form.agents > 1) ? "research"
      : inheritedResearch?.trim()
        ? ["1", "true", "yes", "on"].includes(inheritedResearch.trim().toLowerCase()) ? "research" : "standard"
        : modeDefault);
  // An explicit empty mode clears the profile too; it resolves to the CLI default.
  const effectiveMode = (form.overrides.LEANFLOW_PROVER_MODE ?? inheritedMode) || modeDefault;

  /** Keep profile inheritance and the legacy research switches consistent with the form. */
  function control(suffix: string, shortLabel?: string, hint?: string, help?: string) {
    const name = `LEANFLOW_PROVER_${suffix}`;
    const spec = specs.get(name);
    if (!spec) return null;
    const label = PROVER_KNOB_LABELS[name] ?? spec.name;
    const value = suffix === "MODE" ? effectiveMode : form.overrides[name] ?? profile?.overrides[name]
      ?? (suffix === "PARALLELISM" && form.researchWorkers !== null ? String(form.researchWorkers)
        : suffix === "PARALLELISM" && form.agents > 1 ? String(form.agents) : undefined);
    const inherited = form.overrides[name] === undefined && (suffix === "MODE" ? profileMode !== undefined : profile?.overrides[name] !== undefined);
    const onChange = (next: string | undefined) => {
      const overrides = { ...form.overrides };
      if (next === undefined) delete overrides[name]; else overrides[name] = next;
      // The visible mode and legacy CLI switch describe the same launch.
      const mode = (next ?? (suffix === "MODE" ? profileMode : profile?.overrides[name]) ?? spec.default) || spec.default;
      if (suffix === "PARALLELISM") overrides.LEANFLOW_PROVER_MODE = effectiveMode;
      setForm(suffix === "MODE" || suffix === "PARALLELISM"
        ? { overrides, research: suffix === "MODE" ? mode === "research" : effectiveMode === "research", agents: 1, researchWorkers: null }
        : { overrides });
    };
    return <Field key={name} label={shortLabel ?? label} source={inherited ? "Profile" : form.overrides[name] !== undefined ? "Override" : undefined} hint={hint ?? spec.summary} help={help} helpLabel={suffix === "MODE" ? "Research and Standard modes" : undefined}>
      <KnobControl spec={spec} value={value} onChange={onChange} />
    </Field>;
  }

  /** Keep an emptied optional allowance unlimited, including over a saved profile. */
  function limitControl(suffix: string, label: string, hint: string) {
    const name = `LEANFLOW_PROVER_${suffix}`;
    const spec = specs.get(name);
    if (!spec) return null;
    const override = form.overrides[name];
    const saved = profile?.overrides[name];
    const value = override ?? saved ?? spec.default;
    const setValue = (next: string | undefined) => {
      const overrides = { ...form.overrides };
      if (next === undefined) delete overrides[name]; else overrides[name] = next;
      setForm({ overrides });
    };
    return <div className="global-limit" key={name}>
      <Field label={label} source={override !== undefined ? "Override" : saved !== undefined ? "Profile" : undefined} hint={hint} help={LIMIT_HELP[suffix]}>
        <input aria-label={name} type="number" min={spec.minimum} max={spec.maximum} step={1}
          placeholder={suffix === "MAX_NODES" ? spec.default : "∞"} value={value}
          onChange={event => setValue(event.target.value)} />
      </Field>
      {override !== undefined && saved !== undefined && <button className="link" onClick={() => setValue(undefined)} aria-label={`Restore profile ${label.toLowerCase()}`}>Use profile limit</button>}
    </div>;
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
    return <Field key={name} label="Backend" source={source} hint="Uses this native agent's own login and configuration." help={HELP.backend}>
      <select aria-label={name} value={value ?? "__default__"} onChange={event => onChange(event.target.value === "__default__" ? undefined : event.target.value)}>
        <option value="__default__">{profile?.overrides[name] ? `Use profile (${profile.overrides[name]})` : `Use ${parentLabel} backend (${providerLabel})`}</option>
        {(profile?.overrides[name] !== undefined || value === "") && <option value="">Use {parentLabel} backend ({providerLabel})</option>}
        {BACKEND_IDS.map(id => <option key={id} value={id}>{BACKEND_LABELS[id]}</option>)}
      </select>
    </Field>;
  }

  return <Card title="Prover setup" subtitle="Research builds, reviews, and revises a proof plan while provers work on its goals. Standard works through proof goals directly.">
    {profile && <div className="prover-profile-note">Profile <strong>{profile.name}</strong><span>Changes here apply to this launch.</span></div>}
    <div className="launch-fields prover-mode-fields">
      {control("MODE", "Mode", "Choose research coordination or direct sequential proving.", HELP.mode)}
      {control("SEARCH_ORDER", "Dependency order", "Bottom-up solves dependencies before the goals that use them. Top-down starts from the main goals.", HELP.dependencyOrder)}
      {effectiveMode === "research" && control("PARALLELISM", "Concurrent prover jobs", "Maximum simultaneous proof jobs in research mode.", HELP.concurrency)}
    </div>
    {effectiveMode === "research" ? <>
    <section className="prover-role" aria-label="Prover settings">
      <div className="prover-role-heading"><strong>Prover</strong><InfoHelp label="Prover">{HELP.prover}</InfoHelp><span>Works on individual proof goals</span></div>
      <div className="launch-fields">
        {roleControl("prover", "PROVIDER")}
        {roleControl("prover", "MODEL")}
        {roleControl("prover", "REASONING_EFFORT")}
        {limitControl("PROVER_SESSIONS", "Prover calls", "Shared across all prover jobs in the run. Clear for ∞; 0 starts none.")}
      </div>
    </section>
    <section className="prover-role" aria-label="Orchestrator settings">
      <div className="prover-role-heading"><strong>Orchestrator</strong><InfoHelp label="Orchestrator">{HELP.orchestrator}</InfoHelp><span>Research uses this agent to plan, review, and revise the graph</span></div>
      <div className="launch-fields">
        {roleControl("orchestrator", "PROVIDER")}
        {roleControl("orchestrator", "MODEL")}
        {roleControl("orchestrator", "REASONING_EFFORT")}
        {limitControl("ORCHESTRATOR_SESSIONS", "Orchestrator calls", "Shared by planning, review and research jobs. Clear for ∞; 0 starts none.")}
      </div>
    </section>
    <p className="muted">One process = one agent call, even with many model replies or tool steps. Global limits apply across all roles.</p>
    <section className="prover-role" aria-label="Research and recovery limits">
      <div className="prover-role-heading"><strong>Research and recovery limits</strong><span>Control retries and changes to the proof plan</span></div>
      <div className="launch-fields">
        {limitControl("NEGATION_SESSIONS", "Refutation calls", "Shared across refutation jobs in the run. Clear for ∞; 0 starts none.")}
        {limitControl("PLAN_REFINEMENTS", "Proof-direction changes", "Limits changes of informal proof direction. Routine plan notes do not count. Clear for ∞; 0 keeps the initial direction.")}
        {limitControl("MAX_DECOMPOSITIONS", "Recovery decisions", "Shared allowance for orchestrator retry, refutation, and decomposition decisions. Clear for ∞; 0 disables these recovery decisions.")}
        {limitControl("MAX_NODES", "Maximum graph nodes", "Maximum original declarations and helper lemmas in the theorem graph. Clear to use the CLI default.")}
      </div>
    </section>
    </> : <>
      <p className="muted">Standard uses one prover at a time. Saved role overrides remain available in Knobs.</p>
      <div className="launch-fields">
        {limitControl("MAX_RESTARTS", "Standard retries per goal", "Additional passes after the first attempt in Standard mode. Clear for ∞; 0 allows no retries.")}
      </div>
    </>}
  </Card>;
}
