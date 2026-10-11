/** Resolve native model inheritance and reject catalogued incompatible launch choices. */
import { isBackend, type BackendSetup, type NativeModelCatalog, type ProviderSetupState } from "./providerCredentials";
import type { LaunchRequest } from "./types";

export type NativeRole = "launch" | "prover" | "orchestrator";
export interface NativeSelection {
  provider: string;
  model: string;
  requestedModel: string;
  effort: string;
  explicitEffort: string;
  catalog?: NativeModelCatalog;
}
export const ROLE_PREFIXES = { prover: "LEANFLOW_PROVER_", orchestrator: "LEANFLOW_PROVER_ORCHESTRATOR_" } as const;

/** Find metadata only for the selected native backend. */
export function backendSetup(setup: ProviderSetupState | undefined, provider: string): BackendSetup | undefined {
  return setup?.backends.find(row => row.id === provider);
}

/** Resolve model-specific native defaults without inventing an effort level. */
export function nativeEffort(catalog: NativeModelCatalog | undefined, model: string): string {
  const choice = catalog?.models.find(row => row.id === model || row.resolved_model === model);
  const isDefault = model === catalog?.default_model || Boolean(choice && (choice.id === catalog?.default_model || choice.resolved_model === catalog?.default_model));
  return catalog?.configured_reasoning_effort || (isDefault ? catalog?.default_reasoning_effort : "") || choice?.default_reasoning_effort || "";
}

/** Mirror prover role inheritance; settings never cross native backend boundaries. */
export function nativeSelections(form: LaunchRequest, setup?: ProviderSetupState, profile: Record<string, string> = {}): Record<NativeRole, NativeSelection> {
  const values = { ...profile, ...form.overrides };
  const provider = form.provider || (setup?.error ? "" : setup?.selected) || "";
  const catalog = backendSetup(setup, provider)?.model_catalog;
  const model = form.model || setup?.model_override || catalog?.default_model || "";
  const launch: NativeSelection = { provider, model, requestedModel: form.model || setup?.model_override || "", effort: setup?.reasoning_effort_override || nativeEffort(catalog, model), explicitEffort: setup?.reasoning_effort_override || "", catalog };
  const resolve = (role: "prover" | "orchestrator", parent: NativeSelection): NativeSelection => {
    const prefix = ROLE_PREFIXES[role];
    const provider = values[prefix + "PROVIDER"] || parent.provider;
    const catalog = backendSetup(setup, provider)?.model_catalog;
    const sameBackend = provider === parent.provider;
    const model = values[prefix + "MODEL"] || (sameBackend ? parent.model : catalog?.default_model) || "";
    const sameModel = !values[prefix + "MODEL"] || values[prefix + "MODEL"] === parent.requestedModel;
    const explicitEffort = values[prefix + "REASONING_EFFORT"] || (sameBackend && sameModel ? parent.explicitEffort : "");
    const requestedModel = values[prefix + "MODEL"] || (sameBackend ? parent.requestedModel : "");
    return { provider, model, requestedModel, explicitEffort, effort: explicitEffort || nativeEffort(catalog, model), catalog };
  };
  const prover = resolve("prover", launch);
  return { launch, prover, orchestrator: resolve("orchestrator", prover) };
}

/** Reset dependent selections after a user changes a backend or model, including inherited profile values. */
export function changeNativeSelection(form: LaunchRequest, patch: Partial<LaunchRequest>, setup?: ProviderSetupState, profile: Record<string, string> = {}): Partial<LaunchRequest> {
  const before = nativeSelections(form, setup, profile);
  const next = { ...form, ...patch, overrides: { ...(patch.overrides ?? form.overrides) } };
  if (before.launch.provider !== nativeSelections(next, setup, profile).launch.provider) next.model = "";
  const clear = (name: string) => {
    // An explicit empty value masks a saved profile value; deleting it would restore the incompatible choice.
    if (profile[name] !== undefined) next.overrides[name] = "";
    else delete next.overrides[name];
  };
  for (const role of ["prover", "orchestrator"] as const) {
    const after = nativeSelections(next, setup, profile)[role];
    const prefix = ROLE_PREFIXES[role];
    if (before[role].provider !== after.provider) clear(prefix + "MODEL");
    if (before[role].provider !== after.provider || before[role].model !== after.model) clear(prefix + "REASONING_EFFORT");
  }
  return { ...patch, model: next.model, overrides: next.overrides };
}

/** Explain known invalid choices while allowing native defaults when discovery is unavailable. */
export function launchModelProblems(form: LaunchRequest, setup?: ProviderSetupState, profile: Record<string, string> = {}): string[] {
  const selections = nativeSelections(form, setup, profile);
  const roles: NativeRole[] = form.kind === "prove" ? ["prover", "orchestrator"] : ["launch"];
  const problems: string[] = [];
  for (const role of roles) {
    const { provider, model, catalog, explicitEffort } = selections[role];
    if (!isBackend(provider)) continue;
    const label = role === "launch" ? "Launch" : role === "prover" ? "Prover" : "Orchestrator";
    const option = catalog?.models.find(row => row.id === model || row.resolved_model === model);
    if (catalog?.status === "ready" && model && !option) {
      problems.push(`${label}: ${model} is not in ${provider}'s available models. Choose a listed model or refresh models.`);
    }
    if (explicitEffort && (!option?.reasoning_known || !option.reasoning_efforts.includes(explicitEffort))) {
      problems.push(option?.reasoning_known
        ? `${label}: ${model} does not support reasoning effort ${explicitEffort}. Choose a supported effort or the native default.`
        : `${label}: reasoning effort ${explicitEffort} cannot be verified for ${model || "this model"}. Refresh models or use the native default.`);
    }
  }
  return [...new Set(problems)];
}
