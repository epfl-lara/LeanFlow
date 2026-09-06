/**
 * Browse the declared knob catalog, build profiles, and diff them.
 *
 * The catalog comes from `leanflow flags list --json`, so it cannot drift from
 * the runtime the way a hand-maintained list in the editor would.
 */
import { useMemo, useState } from "react";

import {
  isExtensionEditableKnob,
  rejectedProfileKnobNames,
} from "../../src/core/launch";
import type { FlagSpec } from "../../src/core/types";
import { LAUNCH_FIELD_LIMITS } from "../../src/core/types";
import { Card, Empty, Notice } from "../components";
import { useStore } from "../store";
import { post } from "../vscodeApi";

/** Render a value editor from the CLI's authoritative flag metadata. */
export function KnobControl(props: {
  spec: FlagSpec;
  value: string | undefined;
  onChange: (value: string | undefined) => void;
}) {
  const { spec, value, onChange } = props;
  const effective = value ?? spec.default;

  if (spec.value_type === "bool") {
    const on = ["1", "true", "yes", "on"].includes(effective.trim().toLowerCase());
    return (
      <select
        aria-label={spec.name}
        value={value === undefined ? "default" : on ? "1" : "0"}
        onChange={(event) =>
          onChange(event.target.value === "default" ? undefined : event.target.value)
        }
      >
        <option value="default">default ({spec.default || "0"})</option>
        <option value="1">on</option>
        <option value="0">off</option>
      </select>
    );
  }

  if (spec.value_type === "enum" && spec.choices) {
    return (
      <select
        aria-label={spec.name}
        value={value ?? "__default__"}
        onChange={(event) =>
          onChange(event.target.value === "__default__" ? undefined : event.target.value)
        }
      >
        <option value="__default__">default ({spec.default || "unset"})</option>
        {spec.choices.map((choice) => (
          <option key={choice} value={choice}>
            {choice || "(unset)"}
          </option>
        ))}
      </select>
    );
  }

  if (spec.value_type === "int" || spec.value_type === "float") {
    return (
      <input
        aria-label={spec.name}
        type="number"
        min={spec.minimum}
        max={spec.maximum}
        step={spec.value_type === "float" ? "any" : 1}
        placeholder={spec.default || "unset"}
        value={value ?? ""}
        onChange={(event) => onChange(event.target.value === "" ? undefined : event.target.value)}
      />
    );
  }

  return (
    <input
      aria-label={spec.name}
      type="text"
      maxLength={LAUNCH_FIELD_LIMITS.overrideValue}
      placeholder={spec.default || "unset"}
      value={value ?? ""}
      onChange={(event) => onChange(event.target.value === "" ? undefined : event.target.value)}
    />
  );
}

export function KnobsView() {
  const { app, view, setView, setForm, diff, requestDiff } = useStore();
  const [draftName, setDraftName] = useState("");
  const [draftSummary, setDraftSummary] = useState("");
  const [showInternal, setShowInternal] = useState(false);

  const catalog = app?.catalog;
  const profiles = app?.profiles?.profiles ?? [];
  const overrides = view.form.overrides;

  const search = view.knobSearch.trim().toLowerCase();

  const groups = useMemo(() => {
    if (!catalog) {
      return [];
    }
    return catalog.groups
      .map((group) => ({
        name: group.name,
        flags: group.flags.filter((flag) => {
          if (view.form.kind === "prove" && flag.name === "LEANFLOW_HUMAN_REVIEW_ENABLED") return false;
          if (!showInternal && flag.kind === "internal") {
            return false;
          }
          if (view.knobAblatableOnly && !flag.ablatable) {
            return false;
          }
          if (!search) {
            return true;
          }
          return (
            flag.name.toLowerCase().includes(search) ||
            flag.summary.toLowerCase().includes(search) ||
            flag.group.toLowerCase().includes(search)
          );
        }),
      }))
      .filter((group) => group.flags.length > 0);
  }, [catalog, search, view.knobAblatableOnly, view.form.kind, showInternal]);

  const setOverride = (name: string, value: string | undefined) => {
    const next = { ...overrides };
    if (value === undefined) {
      delete next[name];
    } else {
      next[name] = value;
    }
    setForm({ overrides: next });
  };

  const toggleGroup = (name: string) => {
    const collapsed = view.collapsedGroups.includes(name);
    setView({
      collapsedGroups: collapsed
        ? view.collapsedGroups.filter((entry) => entry !== name)
        : [...view.collapsedGroups, name],
    });
  };

  if (!catalog) {
    return (
      <Empty>
        The knob catalog could not be read.
        <br />
        Check that <span className="tag">leanflow flags list --json</span> works.
      </Empty>
    );
  }

  const overrideCount = Object.keys(overrides).length;
  const restrictedWorkingKnobs = rejectedProfileKnobNames(
    {
      name: "working-set",
      summary: "",
      builtin: false,
      overrides,
    },
    catalog,
    true,
  );
  const totalShown = groups.reduce((sum, group) => sum + group.flags.length, 0);

  return (
    <>
      <Card
        title="Knob catalog"
        subtitle={`${catalog.count} declared knobs across ${catalog.groups.length} subsystems. Marked knobs are the ones worth flipping in an ablation.`}
      >
        <div className="row tight">
          <input
            type="text"
            placeholder="Search name, summary, or group…"
            style={{ maxWidth: 300 }}
            value={view.knobSearch}
            onChange={(event) => setView({ knobSearch: event.target.value })}
          />
          <button
            className={`btn ${view.knobAblatableOnly ? "" : "ghost"}`}
            onClick={() => setView({ knobAblatableOnly: !view.knobAblatableOnly })}
          >
            Ablatable only
          </button>
          <button
            className={`btn ${showInternal ? "" : "ghost"}`}
            onClick={() => setShowInternal((value) => !value)}
          >
            Show plumbing
          </button>
          <span className="muted">{totalShown} shown</span>
        </div>
      </Card>

      <Card
        title="Working set"
        subtitle="Overrides here apply to the next launch and can be saved as a named profile."
        actions={
          overrideCount > 0 ? (
            <button className="btn ghost" onClick={() => setForm({ overrides: {} })}>
              Clear all
            </button>
          ) : undefined
        }
      >
        <div className="row tight" style={{ marginBottom: 10 }}>
          <span className="pill">{overrideCount} knob{overrideCount === 1 ? "" : "s"} set</span>
          <select
            value={view.knobProfile}
            onChange={(event) => {
              const name = event.target.value;
              setView({ knobProfile: name });
              const profile = profiles.find((entry) => entry.name === name);
              if (profile) {
                setForm({ overrides: { ...profile.overrides } });
                setDraftName(profile.builtin ? `${profile.name}-variant` : profile.name);
                setDraftSummary(profile.builtin ? "" : profile.summary);
              }
            }}
          >
            <option value="">Load a profile into the working set…</option>
            {profiles.map((profile) => (
              <option key={profile.name} value={profile.name}>
                {profile.name} ({Object.keys(profile.overrides).length})
                {rejectedProfileKnobNames(profile, catalog).length > 0
                  ? " · unavailable in VS Code"
                  : ""}
              </option>
            ))}
          </select>
        </div>

        {restrictedWorkingKnobs.length > 0 && (
          <Notice tone="error">
            This working set contains terminal-only, invalid, or unknown knobs and cannot be saved or
            launched from VS Code: <span className="mono">{restrictedWorkingKnobs.join(", ")}</span>.
            Use a trusted terminal for sensitive debug/runtime settings.
          </Notice>
        )}

        <div className="grid two">
          <label className="field">
            <span className="label">Save as profile</span>
            <input
              type="text"
              maxLength={LAUNCH_FIELD_LIMITS.profile}
              placeholder="e.g. ablate-negation-probe"
              value={draftName}
              onChange={(event) => setDraftName(event.target.value)}
            />
          </label>
          <label className="field">
            <span className="label">Summary</span>
            <input
              type="text"
              maxLength={LAUNCH_FIELD_LIMITS.profileSummary}
              placeholder="What this profile is testing"
              value={draftSummary}
              onChange={(event) => setDraftSummary(event.target.value)}
            />
          </label>
        </div>
        <div className="row tight" style={{ marginTop: 8 }}>
          <button
            className="btn"
            disabled={
              !draftName.trim() ||
              overrideCount === 0 ||
              restrictedWorkingKnobs.length > 0
            }
            onClick={() =>
              post({
                type: "saveProfile",
                profile: {
                  name: draftName.trim(),
                  summary: draftSummary.trim(),
                  builtin: false,
                  overrides,
                },
              })
            }
          >
            Save profile
          </button>
          {view.knobProfile &&
            !profiles.find((entry) => entry.name === view.knobProfile)?.builtin && (
              <button
                className="btn secondary"
                onClick={() => post({ type: "deleteProfile", name: view.knobProfile })}
              >
                Delete "{view.knobProfile}"
              </button>
            )}
        </div>
      </Card>

      <Card
        title="Compare profiles"
        subtitle="Shows knobs whose effective value differs — a knob absent from a profile resolves to its declared default."
        actions={
          <button
            className="btn ghost"
            onClick={() => requestDiff(view.diffLeft, view.diffRight)}
          >
            Compare
          </button>
        }
      >
        <div className="row tight">
          <select
            value={view.diffLeft}
            onChange={(event) => setView({ diffLeft: event.target.value })}
          >
            {profiles.map((profile) => (
              <option key={profile.name} value={profile.name}>
                {profile.name}
              </option>
            ))}
          </select>
          <span className="muted">→</span>
          <select
            value={view.diffRight}
            onChange={(event) => setView({ diffRight: event.target.value })}
          >
            {profiles.map((profile) => (
              <option key={profile.name} value={profile.name}>
                {profile.name}
              </option>
            ))}
          </select>
        </div>
        {diff.error && <Notice tone="error">{diff.error}</Notice>}
        {diff.rows.length > 0 && (
          <div className="table-wrap" style={{ marginTop: 10, maxHeight: 280 }}>
            <table className="data">
              <thead>
                <tr>
                  <th>Knob</th>
                  <th>{view.diffLeft}</th>
                  <th>{view.diffRight}</th>
                  <th>Group</th>
                </tr>
              </thead>
              <tbody>
                {diff.rows.map((row) => (
                  <tr key={row.name}>
                    <td className="mono">{row.name}</td>
                    <td className="mono">{row.left || "—"}</td>
                    <td className="mono">{row.right || "—"}</td>
                    <td className="muted">{row.group}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {!diff.loading && diff.rows.length === 0 && !diff.error && (
          <div className="muted" style={{ marginTop: 8 }}>
            Pick two profiles and compare.
          </div>
        )}
      </Card>

      {groups.map((group) => {
        const collapsed = view.collapsedGroups.includes(group.name);
        const changed = group.flags.filter((flag) => overrides[flag.name] !== undefined).length;
        return (
          <div className="knob-group" key={group.name}>
            <button
              aria-expanded={!collapsed}
              className="knob-group-header"
              onClick={() => toggleGroup(group.name)}
              type="button"
            >
              <span>
                {collapsed ? "▸" : "▾"} {group.name}
              </span>
              <span className="row tight">
                {changed > 0 && <span className="pill ok">{changed} set</span>}
                <span className="muted">{group.flags.length}</span>
              </span>
            </button>
            {!collapsed &&
              group.flags.map((flag) => (
                <div
                  className={`knob${overrides[flag.name] !== undefined ? " changed" : ""}`}
                  key={flag.name}
                >
                  <div>
                    <div className="name">
                      {flag.name}
                      {flag.ablatable && (
                        <span className="tag" style={{ marginLeft: 6 }}>
                          ablatable
                        </span>
                      )}
                      {!flag.editable && (
                        <span className="tag" style={{ marginLeft: 6 }}>
                          set by launcher
                        </span>
                      )}
                      {flag.editable && !isExtensionEditableKnob(flag) && (
                        <span className="tag" style={{ marginLeft: 6 }}>
                          terminal only
                        </span>
                      )}
                    </div>
                    <div className="summary">{flag.summary}</div>
                  </div>
                  <div className="control">
                    {isExtensionEditableKnob(flag) ? (
                      <>
                        <KnobControl
                          spec={flag}
                          value={overrides[flag.name]}
                          onChange={(value) => setOverride(flag.name, value)}
                        />
                        <span className="default-note">
                          default {flag.default || "unset"}
                        </span>
                      </>
                    ) : (
                      <span className="default-note">read-only</span>
                    )}
                  </div>
                </div>
              ))}
          </div>
        );
      })}
    </>
  );
}
