/** The launcher: pick a workflow, a target, a model, and a knob profile. */
import { useEffect, useMemo, useState } from "react";

import {
  describeCommand,
  rejectedProfileKnobNames,
  validateLaunch,
} from "../../src/core/launch";
import { LAUNCH_FIELD_LIMITS, WORKFLOW_KINDS } from "../../src/core/types";
import { Card, Check, Field, Notice } from "../components";
import { PlayIcon } from "../icons";
import { useStore } from "../store";
import { post } from "../vscodeApi";
import { ProverSettings } from "./ProverSettings";

/** Providers the CLI understands. Empty means "use the configured default". */
const PROVIDERS = [
  { id: "", label: "Configured default" },
  { id: "codex", label: "codex — Codex OAuth" },
  { id: "custom", label: "custom — OpenAI-compatible endpoint" },
  { id: "openrouter", label: "openrouter" },
  { id: "anthropic", label: "anthropic" },
  { id: "deepseek", label: "deepseek" },
  { id: "rcp", label: "rcp — EPFL inference" },
  { id: "local", label: "local — vLLM / Ollama / llama.cpp" },
];

export function LaunchView() {
  const { app, view, setForm, preview, requestPreview } = useStore();
  const form = view.form;
  const [showPlan, setShowPlan] = useState(false);

  const problems = useMemo(() => validateLaunch(form), [form]);
  const command = useMemo(() => {
    try {
      return describeCommand(form, { ...app?.profiles?.profiles.find((profile) => profile.name === form.profile)?.overrides, ...form.overrides });
    } catch (error) {
      return `[invalid launch: ${error instanceof Error ? error.message : String(error)}]`;
    }
  }, [form, app?.profiles]);
  const kindInfo = WORKFLOW_KINDS.find((kind) => kind.id === form.kind);

  const profiles = app?.profiles?.profiles ?? [];
  const activeProfile = profiles.find((profile) => profile.name === form.profile);
  const restrictedProfileKnobs = rejectedProfileKnobNames(
    activeProfile,
    app?.catalog ?? null,
  );
  const restrictedOverrideKnobs = rejectedProfileKnobNames(
    {
      name: "one-off-overrides",
      summary: "",
      builtin: false,
      overrides: form.overrides,
    },
    app?.catalog ?? null,
    true,
  );
  const restrictedKnobs = [...new Set([...restrictedProfileKnobs, ...restrictedOverrideKnobs])];
  const overrideCount = Object.keys(form.overrides).length;
  const boundedProver = form.kind === "prove" && Boolean(app?.catalog?.groups.some((group) => group.flags.some((flag) => flag.name === "LEANFLOW_PROVER_MODE")));
  useEffect(() => {
    if (form.kind === "prove" && form.humanReview) setForm({ humanReview: false });
  }, [form.kind, form.humanReview, setForm]);
  const canLaunch = Boolean(
    app?.project.found &&
      app?.cli.ok &&
      restrictedKnobs.length === 0 &&
      problems.length === 0,
  );

  // Re-resolve the plan as the form settles. The CLI's --dry-run has no side
  // effects, so debouncing is about request volume, not safety. A request the
  // host would reject is never sent: it would be dropped without a reply and
  // leave this panel resolving forever.
  useEffect(() => {
    if (!showPlan || !canLaunch) {
      return;
    }
    const handle = setTimeout(() => requestPreview(form), 350);
    return () => clearTimeout(handle);
  }, [showPlan, canLaunch, form, requestPreview]);

  const leanFileOptions = app?.leanFiles ?? [];

  return (
    <>
      {!app?.cli.ok && (
        <Notice tone="error">
          <strong>LeanFlow CLI setup is required.</strong>
          <div style={{ marginTop: 4 }}>{app?.cli.error}</div>
          <div className="row tight" style={{ marginTop: 8 }}>
            <button
              className="btn"
              onClick={() => post({ type: "openExternalDoc", topic: "install" })}
            >
              Open install guide
            </button>
            <button
              className="btn ghost"
              onClick={() => post({ type: "openCliSettings" })}
            >
              Configure CLI path
            </button>
            <button className="btn ghost" onClick={() => post({ type: "refresh" })}>
              Check again
            </button>
          </div>
        </Notice>
      )}
      {app?.cli.ok && !app.project.found && (
        <Notice tone="warn">
          No LeanFlow project in this workspace. Run <span className="tag">leanflow project
          init</span> in your Lean repo, or set <span className="tag">leanflow.projectRoot</span>.
        </Notice>
      )}

      <Card
        title="Workflow"
        subtitle={kindInfo?.hint}
        actions={
          <>
            <button
              className="btn secondary"
              onClick={() => setShowPlan((value) => !value)}
              disabled={!canLaunch}
            >
              {showPlan ? "Hide plan" : "Preview plan"}
            </button>
            <button
              className="btn"
              disabled={!canLaunch}
              onClick={() => post({ type: "launch", request: form })}
            >
              <PlayIcon size={12} />
              Start run
            </button>
          </>
        }
      >
        <div className="grid two">
          <Field label="Workflow">
            <select
              value={form.kind}
              onChange={(event) =>
                setForm({ kind: event.target.value as typeof form.kind })
              }
            >
              {WORKFLOW_KINDS.map((kind) => (
                <option key={kind.id} value={kind.id}>
                  {kind.label}
                </option>
              ))}
            </select>
          </Field>

          <Field
            label="Target"
            hint={
              form.kind === "prove"
                ? "Leave empty to scan the whole project for remaining sorries."
                : undefined
            }
          >
            <div className="row tight">
              <input
                type="text"
                list="leanflow-lean-files"
                maxLength={LAUNCH_FIELD_LIMITS.target}
                placeholder={form.kind === "formalize" ? "paper.tex" : "Main.lean (optional)"}
                value={form.target}
                onChange={(event) => setForm({ target: event.target.value })}
              />
              <button
                className="btn ghost"
                onClick={() => post({ type: "pickTarget", kind: form.kind })}
              >
                Browse
              </button>
            </div>
            <datalist id="leanflow-lean-files">
              {leanFileOptions.map((file) => (
                <option key={file} value={file} />
              ))}
            </datalist>
          </Field>

          <Field label="Provider">
            <select
              value={form.provider}
              onChange={(event) => setForm({ provider: event.target.value })}
            >
              {PROVIDERS.map((provider) => (
                <option key={provider.id} value={provider.id}>
                  {provider.label}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Model" hint="Empty uses the provider's configured default.">
            <input
              type="text"
              maxLength={LAUNCH_FIELD_LIMITS.model}
              placeholder="e.g. gpt-5.6-terra, moonshotai/Kimi-K2.6"
              value={form.model}
              onChange={(event) => setForm({ model: event.target.value })}
            />
          </Field>
        </div>
      </Card>

      <ProverSettings />
      <Card title="Workflow options">
        <div className="grid two">
          <div>
            {!boundedProver && <Check
              label="Research mode"
              hint="Plan, retrieval, orchestration, dispatch, feasibility, reporting and learning as one profile. prove only."
              checked={form.research}
              disabled={form.kind !== "prove"}
              onChange={(research) => setForm({ research })}
            />}
            {!boundedProver && form.research && (
              <div style={{ marginLeft: 22, marginTop: 6 }}>
                <Field label="Research workers" hint="0 keeps the run single-lane.">
                  <input
                    type="number"
                    min={0}
                    max={16}
                    value={form.researchWorkers ?? 2}
                    onChange={(event) =>
                      setForm({ researchWorkers: Number(event.target.value) })
                    }
                  />
                </Field>
              </div>
            )}
            <Check
              label="Clean room"
              hint="Forbid looking up an existing solution to the target. Required for uncontaminated benchmark runs."
              checked={form.cleanRoom}
              disabled={form.kind !== "prove"}
              onChange={(cleanRoom) => setForm({ cleanRoom })}
            />
          </div>

          <div>
            <Check
              label="Force single lane"
              hint={boundedProver ? "Run one prover job at a time; research planning remains available." : "Passes --no-parallel: one agent, no background workers."}
              checked={form.noParallel}
              onChange={(noParallel) => setForm({ noParallel })}
            />
            {!boundedProver && <Field
              label="Swarm agents"
              hint="Above 1 opts into file-lock-aware concurrent work. A file-scoped prove run stays single-agent."
            >
              <input
                type="number"
                min={1}
                max={16}
                disabled={form.noParallel}
                value={form.agents}
                onChange={(event) => setForm({ agents: Number(event.target.value) })}
              />
            </Field>}
            <Field
              label="Allowed axioms"
              hint="Comma or space separated, beyond the standard three."
            >
              <input
                type="text"
                maxLength={LAUNCH_FIELD_LIMITS.axioms}
                placeholder="e.g. Classical.choice"
                value={form.axioms}
                onChange={(event) => setForm({ axioms: event.target.value })}
              />
            </Field>
            <Field
              label="Additional skills"
              hint={`One path per line, layered on top of the workflow's default skill. Up to ${LAUNCH_FIELD_LIMITS.additionalSkillCount} paths of ${LAUNCH_FIELD_LIMITS.additionalSkill.toLocaleString()} characters.`}
            >
              <textarea
                placeholder={"skills/my-tactic-notes.md"}
                style={{ minHeight: 46 }}
                value={form.additionalSkills.join("\n")}
                onChange={(event) =>
                  setForm({
                    additionalSkills: event.target.value
                      .split("\n")
                      .map((line) => line.trim())
                      .filter(Boolean),
                  })
                }
              />
            </Field>
          </div>
        </div>
      </Card>

      <Card
        title="Knobs"
        subtitle={
          activeProfile
            ? activeProfile.summary
            : "Apply a saved knob profile, then adjust individual knobs on the Knobs tab."
        }
      >
        <div className="grid two">
          <Field label="Profile">
            <select
              value={form.profile}
              onChange={(event) => setForm({ profile: event.target.value })}
            >
              <option value="">No profile — declared defaults</option>
              {profiles.map((profile) => (
                <option key={profile.name} value={profile.name}>
                  {profile.name}
                  {profile.builtin ? " (built-in)" : ""} · {
                    Object.keys(profile.overrides).length
                  } knobs
                  {rejectedProfileKnobNames(profile, app?.catalog ?? null).length > 0
                    ? " · unavailable in VS Code"
                    : ""}
                </option>
              ))}
            </select>
          </Field>
          <div>
            <span className="label muted" style={{ fontSize: 12 }}>
              One-off overrides
            </span>
            <div className="row tight" style={{ marginTop: 4 }}>
              <span className="pill">{overrideCount} set</span>
              {overrideCount > 0 && (
                <button className="link" onClick={() => setForm({ overrides: {} })}>
                  clear
                </button>
              )}
            </div>
          </div>
        </div>
        {overrideCount > 0 && (
          <div className="scroll-x" style={{ marginTop: 10 }}>
            <table className="data">
              <tbody>
                {Object.entries(form.overrides).map(([name, value]) => (
                  <tr key={name}>
                    <td className="mono">{name}</td>
                    <td className="mono">{value}</td>
                    <td style={{ width: 30 }}>
                      <button
                        className="link"
                        onClick={() => {
                          const next = { ...form.overrides };
                          delete next[name];
                          setForm({ overrides: next });
                        }}
                      >
                        ✕
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {restrictedKnobs.length > 0 && (
          <Notice tone="error">
            This configuration contains terminal-only, invalid, or unknown knobs and cannot run from
            VS Code: <span className="mono">{restrictedKnobs.join(", ")}</span>. Remove
            those overrides or run the profile explicitly from a trusted terminal.
          </Notice>
        )}
      </Card>

      <Card title="Prompt" subtitle="Optional. Steers the run without changing the target.">
        <textarea
          maxLength={LAUNCH_FIELD_LIMITS.prompt}
          placeholder="e.g. Prefer a decomposition through Nat.factorization before attempting the conjunction directly."
          value={form.prompt}
          onChange={(event) => setForm({ prompt: event.target.value })}
        />
        {form.prompt.length > LAUNCH_FIELD_LIMITS.prompt * 0.9 && (
          <span className="hint">
            {form.prompt.length.toLocaleString()} of{" "}
            {LAUNCH_FIELD_LIMITS.prompt.toLocaleString()} characters. The field stops here rather
            than letting the launch be rejected.
          </span>
        )}
      </Card>

      {problems.length > 0 && (
        <Notice tone="warn">
          <ul style={{ margin: "0 0 0 16px", padding: 0 }}>
            {problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </Notice>
      )}

      <Card title="Command">
        <pre className="raw" style={{ maxHeight: 90 }}>
          {command}
        </pre>
      </Card>

      {showPlan && (
        <Card
          title="Resolved launch plan"
          subtitle="Exactly what the child process receives. Credential values are redacted."
        >
          {!canLaunch && (
            <Notice tone="warn">
              Resolve the problems above to preview a plan.
            </Notice>
          )}
          {canLaunch && preview.loading && <div className="muted">Resolving…</div>}
          {preview.error && <Notice tone="error">{preview.error}</Notice>}
          {preview.plan && (
            <>
              {preview.plan.deferred.length > 0 && (
                <Notice tone="info">
                  {preview.plan.deferred.map((note) => (
                    <div key={note}>{note}</div>
                  ))}
                </Notice>
              )}
              <div className="section-heading">Summary</div>
              <div className="table-wrap" style={{ maxHeight: 220 }}>
                <table className="data">
                  <tbody>
                    {Object.entries(preview.plan.summary).map(([key, value]) => (
                      <tr key={key}>
                        <td className="muted" style={{ width: 150 }}>
                          {key}
                        </td>
                        <td className="mono">{value}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="section-heading">
                Environment delta ({Object.keys(preview.plan.env_delta).length})
              </div>
              <div className="table-wrap" style={{ maxHeight: 300 }}>
                <table className="data">
                  <tbody>
                    {Object.entries(preview.plan.env_delta).map(([key, value]) => (
                      <tr key={key}>
                        <td className="mono">{key}</td>
                        <td className="mono">{value}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </Card>
      )}
    </>
  );
}
