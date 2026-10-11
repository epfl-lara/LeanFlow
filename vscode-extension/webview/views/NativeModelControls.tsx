/** Render model and effort choices reported by the selected native runtime. */
import type { NativeModelCatalog } from "../../src/core/providerCredentials";
import { Field } from "../components";

interface SelectionProps {
  label: string;
  ariaLabel: string;
  value: string | undefined;
  profileValue?: string;
  inheritedLabel: string;
  source?: string;
  catalog?: NativeModelCatalog;
  onChange: (value: string | undefined) => void;
  launch?: boolean;
}

/** Describe discovery failures without pretending an unknown model is unsupported. */
function catalogHint(catalog?: NativeModelCatalog): string {
  if (!catalog || catalog.status !== "ready") return catalog?.error || "Model list unavailable. Refresh models or check provider setup.";
  return "Models reported by the selected native runtime.";
}

/** Keep inherited and saved values visible while new choices come only from the runtime. */
export function NativeModelControl(props: SelectionProps) {
  const { value, profileValue, inheritedLabel, catalog, launch } = props;
  const choices = catalog?.models ?? [];
  const extraValue = value && !choices.some(model => model.id === value);
  const resolved = choices.some(model => model.resolved_model === value);
  return <Field label={props.label} source={props.source} hint={catalogHint(catalog)}>
    <select aria-label={props.ariaLabel} value={value ?? "__default__"}
      onChange={event => props.onChange(event.target.value === "__default__" ? undefined : event.target.value)}>
      {!launch && <option value="__default__">{profileValue ? `Use profile (${profileValue})` : inheritedLabel}</option>}
      {(launch || profileValue !== undefined || value === "") && <option value="">{inheritedLabel}</option>}
      {choices.map(model => <option key={model.id} value={model.id}>{model.label && model.label !== model.id ? `${model.label} (${model.id})` : model.id}</option>)}
      {extraValue && <option value={value}>{value}{!resolved && ` (${catalog?.status === "ready" ? "not listed" : "not verified"})`}</option>}
    </select>
  </Field>;
}

/** Offer only verified effort levels for the effective model, preserving stale values for correction. */
export function NativeEffortControl(props: SelectionProps & { model: string; effectiveEffort: string }) {
  const model = props.catalog?.models.find(row => row.id === props.model || row.resolved_model === props.model);
  const choices = model?.reasoning_known ? model.reasoning_efforts : [];
  const unknown = props.value && !choices.includes(props.value);
  const hint = !model?.reasoning_known
    ? "This runtime does not report reasoning choices for this model. Keep its native default."
    : !choices.length ? "This model has no configurable reasoning effort."
    : `Available for ${props.model}. ${props.effectiveEffort ? `Effective effort: ${props.effectiveEffort}.` : "The runtime chooses its default."}`;
  return <Field label={props.label} source={props.source} hint={hint}>
    <select aria-label={props.ariaLabel} value={props.value ?? "__default__"}
      onChange={event => props.onChange(event.target.value === "__default__" ? undefined : event.target.value)}>
      <option value="__default__">{props.profileValue ? `Use profile (${props.profileValue})` : props.inheritedLabel}</option>
      {(props.profileValue !== undefined || props.value === "") && <option value="">{props.inheritedLabel}</option>}
      {choices.map(effort => <option key={effort} value={effort}>{effort}</option>)}
      {unknown && <option value={props.value}>{props.value} ({model?.reasoning_known ? "unsupported" : "not verified"})</option>}
    </select>
  </Field>;
}
