/** Edit campaign-wide limits while preserving profile inheritance and unlimited values. */
import { isExtensionEditableKnob } from "../../src/core/launch";
import { Card, Field } from "../components";
import { useStore } from "../store";

const LIMITS = [
  { suffix: "WALL_TIME_S", label: "Active time (minutes)", scale: 60, hint: "Shared across resumes, including tools and verification." },
  { suffix: "AGENT_CALLS", label: "Agent calls", scale: 1, hint: "One native agent invocation is one call. Retries and resumes count again." },
  { suffix: "TOKENS", label: "Reported tokens", scale: 1, hint: "Stops future calls. Active calls can exceed the limit; some runtimes may not report usage." },
] as const;

export function GlobalLimits() {
  const { app, view, setForm } = useStore();
  const { form } = view;
  const specs = new Map(app?.catalog?.groups.flatMap(group => group.flags).filter(isExtensionEditableKnob).map(flag => [flag.name, flag]));
  const profile = app?.profiles?.profiles.find(item => item.name === form.profile);
  if (form.kind !== "prove" || !LIMITS.some(limit => specs.has(`LEANFLOW_PROVER_${limit.suffix}`))) return null;

  /** An explicit empty override removes a profile limit instead of restoring it. */
  function setLimit(name: string, value: string | undefined) {
    const overrides = { ...form.overrides };
    if (value === undefined) delete overrides[name]; else overrides[name] = value;
    setForm({ overrides });
  }

  return <Card title="Global limits" subtitle="Optional limits for the whole run, shared by all agents. Empty means Unlimited; 0 allows no further work.">
    <div className="launch-fields">
      {LIMITS.map(({ suffix, label, scale, hint }) => {
        const name = `LEANFLOW_PROVER_${suffix}`;
        const spec = specs.get(name);
        if (!spec) return null;
        const override = form.overrides[name];
        const profileValue = profile?.overrides[name];
        const value = override ?? profileValue ?? spec.default;
        const displayed = value.trim() === "" ? "" : scale === 1 ? value : String(Number(value) / scale);
        return <div className="global-limit" key={name}>
          <Field label={label} source={override !== undefined ? "Override" : profileValue !== undefined ? "Profile" : undefined} hint={hint}>
            <input
              aria-label={name}
              type="number"
              min={spec.minimum === undefined ? undefined : spec.minimum / scale}
              max={spec.maximum === undefined ? undefined : spec.maximum / scale}
              step={spec.value_type === "float" ? "any" : 1}
              placeholder="Unlimited"
              value={displayed}
              onChange={event => setLimit(name, event.target.value === "" ? "" : scale === 1 ? event.target.value : String(Number(event.target.value) * scale))}
            />
          </Field>
          {override !== undefined && profileValue !== undefined && <button className="link" onClick={() => setLimit(name, undefined)} aria-label={`Restore profile ${label.toLowerCase()}`}>Use profile limit</button>}
        </div>;
      })}
    </div>
  </Card>;
}
