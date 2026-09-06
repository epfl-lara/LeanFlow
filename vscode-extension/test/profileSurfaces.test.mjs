/**
 * Saved profiles must say where they can launch and why not (backlog D07),
 * and the launch preview must show the effective dedicated prover
 * configuration including values inherited from the profile.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  compatibleProfileName,
  compatibleProfileOverrides,
  profileLaunchSurfaces,
  proverConfigurationRows,
} from "../dist/test/profileSurfaces.mjs";

const CATALOG = {
  version: 1,
  count: 5,
  groups: [
    {
      name: "prover",
      flags: [
        { name: "LEANFLOW_PROVER_MODE", editable: true, extension_editable: true, sensitive: false, value_type: "enum", choices: ["standard", "research"], default: "standard", summary: "Prover mode" },
        { name: "LEANFLOW_PROVER_PARALLELISM", editable: true, extension_editable: true, sensitive: false, value_type: "int", minimum: 1, maximum: 16, default: "2", summary: "Concurrent prover jobs" },
        { name: "LEANFLOW_PROVER_ALLOWED_AXIOMS", editable: true, extension_editable: false, sensitive: true, value_type: "csv", default: "propext,Classical.choice,Quot.sound", summary: "Allowed axioms" },
        { name: "LEANFLOW_PROVER_MODEL", editable: true, extension_editable: true, sensitive: false, value_type: "string", default: "", summary: "Prover model" },
        { name: "LEANFLOW_PROJECT_ROOT", editable: false, extension_editable: false, sensitive: false, value_type: "path", default: "", summary: "Project root" },
      ],
    },
  ],
};

const PROFILE = {
  name: "beckfiala-research",
  summary: "Research campaign",
  builtin: false,
  overrides: {
    LEANFLOW_PROVER_MODE: "research",
    LEANFLOW_PROVER_PARALLELISM: "4",
    LEANFLOW_PROVER_ALLOWED_AXIOMS: "propext,Classical.choice,Quot.sound",
    LEANFLOW_PROVER_UNKNOWN: "1",
    LEANFLOW_PROJECT_ROOT: "/elsewhere",
  },
};

test("a profile reports each incompatible knob with an actionable reason", () => {
  const surfaces = profileLaunchSurfaces(PROFILE, CATALOG);
  assert.equal(surfaces.terminal, true);
  assert.equal(surfaces.vscode, false);
  assert.deepEqual(
    surfaces.incompatibilities.map((item) => [item.name, item.reason]),
    [
      ["LEANFLOW_PROJECT_ROOT", "launcher-owned"],
      ["LEANFLOW_PROVER_ALLOWED_AXIOMS", "terminal-only"],
      ["LEANFLOW_PROVER_UNKNOWN", "unknown"],
    ],
  );
  assert.match(surfaces.incompatibilities[1].detail, /terminal/i);
  assert.match(surfaces.incompatibilities[2].detail, /catalog/i);
});

test("an invalid value is reported with the catalog's problem text", () => {
  const surfaces = profileLaunchSurfaces(
    { name: "bad", summary: "", builtin: false, overrides: { LEANFLOW_PROVER_PARALLELISM: "99" } },
    CATALOG,
  );
  assert.equal(surfaces.vscode, false);
  assert.equal(surfaces.incompatibilities[0].reason, "invalid-value");
  assert.match(surfaces.incompatibilities[0].detail, /maximum 16/);
  const clean = profileLaunchSurfaces({ name: "ok", summary: "", builtin: true, overrides: { LEANFLOW_PROVER_MODE: "research" } }, CATALOG);
  assert.equal(clean.vscode, true);
  assert.deepEqual(clean.incompatibilities, []);
  assert.equal(profileLaunchSurfaces(undefined, CATALOG).vscode, true);
});

test("a compatible copy keeps only knobs VS Code may set and names what it dropped", () => {
  const copy = compatibleProfileOverrides(PROFILE, CATALOG);
  assert.deepEqual(copy.overrides, { LEANFLOW_PROVER_MODE: "research", LEANFLOW_PROVER_PARALLELISM: "4" });
  assert.deepEqual(copy.removed, ["LEANFLOW_PROJECT_ROOT", "LEANFLOW_PROVER_ALLOWED_AXIOMS", "LEANFLOW_PROVER_UNKNOWN"]);
  assert.equal(compatibleProfileName("beckfiala-research"), "beckfiala-research-vscode");
  assert.equal(compatibleProfileName("x".repeat(70)).length <= 64, true);
  assert.equal(compatibleProfileName("odd name!"), "odd-name-vscode");
});

test("the launch preview lists effective prover settings with where each value came from", () => {
  const rows = proverConfigurationRows({
    envEffective: { LEANFLOW_PROVER_MODE: "research", LEANFLOW_PROVER_PARALLELISM: "3", LEANFLOW_PROVER_MODEL: "gpt-6-astra", LEANFLOW_OTHER: "x" },
    envDelta: {},
    profile: { name: "p", summary: "", builtin: false, overrides: { LEANFLOW_PROVER_MODE: "research", LEANFLOW_PROVER_PARALLELISM: "4" } },
    overrides: { LEANFLOW_PROVER_PARALLELISM: "3" },
    catalog: CATALOG,
  });
  const byName = Object.fromEntries(rows.map((row) => [row.name, row]));
  assert.deepEqual(byName.LEANFLOW_PROVER_MODE, { name: "LEANFLOW_PROVER_MODE", label: "Prover mode", value: "research", source: "profile" });
  assert.equal(byName.LEANFLOW_PROVER_PARALLELISM.source, "override");
  assert.equal(byName.LEANFLOW_PROVER_PARALLELISM.value, "3");
  assert.equal(byName.LEANFLOW_PROVER_MODEL.source, "resolved");
  assert.equal(byName.LEANFLOW_PROVER_ALLOWED_AXIOMS.source, "default");
  assert.equal(byName.LEANFLOW_PROVER_ALLOWED_AXIOMS.value, "propext,Classical.choice,Quot.sound");
  assert.equal("LEANFLOW_OTHER" in byName, false);
  assert.equal("LEANFLOW_PROJECT_ROOT" in byName, false);
});

test("an older CLI without an effective environment still previews profile and default values", () => {
  const rows = proverConfigurationRows({
    envEffective: undefined,
    envDelta: { LEANFLOW_PROVER_MODE: "research" },
    profile: { name: "p", summary: "", builtin: false, overrides: { LEANFLOW_PROVER_MODE: "research" } },
    overrides: {},
    catalog: CATALOG,
  });
  const byName = Object.fromEntries(rows.map((row) => [row.name, row]));
  assert.equal(byName.LEANFLOW_PROVER_MODE.value, "research");
  assert.equal(byName.LEANFLOW_PROVER_MODE.source, "profile");
  assert.equal(byName.LEANFLOW_PROVER_PARALLELISM.value, "2");
  assert.equal(byName.LEANFLOW_PROVER_PARALLELISM.source, "default");
});
