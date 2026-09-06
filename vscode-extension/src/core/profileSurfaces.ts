/**
 * Say where a saved knob profile can launch, and why not, before a launch is
 * attempted; and show the dedicated prover configuration a launch resolves to.
 *
 * The allowlist itself lives in `launch.ts` and is unchanged here: this module
 * only explains its verdicts and derives a compatible copy that drops the
 * offending knobs, so the user can act instead of guessing.
 */
import { extensionKnobValueProblem, isExtensionEditableKnob } from "./launch";
import type { FlagCatalog, FlagProfile, FlagSpec } from "./types";

export type IncompatibilityReason = "unknown" | "terminal-only" | "launcher-owned" | "invalid-value";

export interface ProfileIncompatibility {
  name: string;
  reason: IncompatibilityReason;
  detail: string;
}

export interface ProfileSurfaces {
  /** A trusted terminal accepts every catalogued knob. */
  terminal: true;
  vscode: boolean;
  incompatibilities: ProfileIncompatibility[];
}

/** Readable names for the dedicated prover's knobs, shared by the launch card and the preview. */
export const PROVER_KNOB_LABELS: Record<string, string> = {
  LEANFLOW_PROVER_MODE: "Prover mode",
  LEANFLOW_PROVER_SEARCH_ORDER: "Dependency order",
  LEANFLOW_PROVER_JOB_API_CALLS: "API calls per prover pass",
  LEANFLOW_PROVER_MAX_RESTARTS: "Maximum restarts per node",
  LEANFLOW_PROVER_PLAN_REFINEMENTS: "Plan refinement limit",
  LEANFLOW_PROVER_PARALLELISM: "Concurrent prover jobs",
  LEANFLOW_PROVER_TOTAL_API_CALLS: "Campaign API calls",
  LEANFLOW_PROVER_ORCHESTRATOR_API_CALLS: "API calls per planning stage",
  LEANFLOW_PROVER_MAX_NODES: "Maximum theorems",
  LEANFLOW_PROVER_MAX_DECOMPOSITIONS: "Maximum decompositions",
  LEANFLOW_PROVER_WALL_TIME_S: "Wall-clock limit (s)",
  LEANFLOW_PROVER_TIMEOUT_S: "Check timeout (s)",
  LEANFLOW_PROVER_MODEL: "Prover model",
  LEANFLOW_PROVER_ORCHESTRATOR_MODEL: "Orchestrator model",
  LEANFLOW_PROVER_CONTEXT_TOKENS: "Prover context tokens",
  LEANFLOW_PROVER_ORCHESTRATOR_CONTEXT_TOKENS: "Orchestrator context tokens",
  LEANFLOW_PROVER_COMPRESSION: "Context compression",
  LEANFLOW_PROVER_ORCHESTRATOR_COMPRESSION: "Orchestrator compression",
  LEANFLOW_PROVER_FILL_DEFINITIONS: "Fill definitions",
  LEANFLOW_PROVER_ALLOWED_AXIOMS: "Allowed axioms",
};

function catalogSpecs(catalog: FlagCatalog | null): Map<string, FlagSpec> {
  const specs = new Map<string, FlagSpec>();
  for (const group of catalog?.groups ?? []) {
    for (const flag of group.flags) {
      specs.set(flag.name, flag);
    }
  }
  return specs;
}

function incompatibility(
  name: string, value: string, spec: FlagSpec | undefined, allowUnset: boolean,
): ProfileIncompatibility | null {
  if (spec === undefined) {
    return { name, reason: "unknown", detail: "not in the installed CLI's flag catalog" };
  }
  if (!spec.editable) {
    return { name, reason: "launcher-owned", detail: "set by the launcher itself; a profile cannot override it" };
  }
  if (!isExtensionEditableKnob(spec)) {
    return {
      name, reason: "terminal-only",
      detail: "sensitive or terminal-only; VS Code cannot set it. Run this profile from a trusted terminal, or save a copy without it",
    };
  }
  const problem = extensionKnobValueProblem(spec, value, allowUnset);
  return problem === null ? null : { name, reason: "invalid-value", detail: problem };
}

/** Which launch surfaces accept a profile, with one actionable line per refused knob. */
export function profileLaunchSurfaces(
  profile: FlagProfile | undefined, catalog: FlagCatalog | null, allowUnset = false,
): ProfileSurfaces {
  if (!profile) {
    return { terminal: true, vscode: true, incompatibilities: [] };
  }
  const specs = catalogSpecs(catalog);
  const incompatibilities = Object.entries(profile.overrides)
    .map(([name, value]) => incompatibility(name, value, specs.get(name), allowUnset))
    .filter((item): item is ProfileIncompatibility => item !== null)
    .sort((left, right) => left.name.localeCompare(right.name));
  return { terminal: true, vscode: incompatibilities.length === 0, incompatibilities };
}

/** The profile's overrides without the knobs VS Code refuses, and the names it dropped. */
export function compatibleProfileOverrides(
  profile: FlagProfile, catalog: FlagCatalog | null,
): { overrides: Record<string, string>; removed: string[] } {
  const removed = profileLaunchSurfaces(profile, catalog).incompatibilities.map((item) => item.name);
  const dropped = new Set(removed);
  const overrides: Record<string, string> = {};
  for (const [name, value] of Object.entries(profile.overrides)) {
    if (!dropped.has(name)) {
      overrides[name] = value;
    }
  }
  return { overrides, removed };
}

/** A filesystem-safe name for the compatible copy of a profile. */
export function compatibleProfileName(name: string): string {
  const suffix = "-vscode";
  const base = name
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^[^A-Za-z0-9]+/, "")
    .replace(/-+$/, "")
    .slice(0, 64 - suffix.length)
    .replace(/-+$/, "");
  return `${base || "profile"}${suffix}`;
}

export interface ProverConfigRow {
  name: string;
  label: string;
  value: string;
  /** Where the effective value came from: a one-off override, the profile, the resolved plan, or the catalog default. */
  source: "override" | "profile" | "resolved" | "default";
}

/**
 * The dedicated prover's effective configuration for a launch.
 *
 * Reads the complete effective environment when the CLI supplies it; an older
 * CLI still gets profile and default values, so inherited settings are visible
 * before the run starts rather than discovered from its state file afterwards.
 */
export function proverConfigurationRows(input: {
  envEffective: Record<string, string> | undefined;
  envDelta: Record<string, string> | undefined;
  profile: FlagProfile | undefined;
  overrides: Record<string, string>;
  catalog: FlagCatalog | null;
}): ProverConfigRow[] {
  const rows: ProverConfigRow[] = [];
  for (const group of input.catalog?.groups ?? []) {
    for (const flag of group.flags) {
      if (!flag.name.startsWith("LEANFLOW_PROVER_")) {
        continue;
      }
      const label = PROVER_KNOB_LABELS[flag.name]
        ?? flag.name.replace(/^LEANFLOW_PROVER_/, "").toLowerCase().replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
      const effective = input.envEffective?.[flag.name] ?? input.envDelta?.[flag.name];
      const override = input.overrides[flag.name];
      const fromProfile = input.profile?.overrides[flag.name];
      if (override !== undefined && override !== "") {
        rows.push({ name: flag.name, label, value: effective ?? override, source: "override" });
      } else if (fromProfile !== undefined && override === undefined) {
        rows.push({ name: flag.name, label, value: effective ?? fromProfile, source: "profile" });
      } else if (effective !== undefined) {
        rows.push({ name: flag.name, label, value: effective, source: "resolved" });
      } else {
        rows.push({ name: flag.name, label, value: flag.default, source: "default" });
      }
    }
  }
  return rows;
}
