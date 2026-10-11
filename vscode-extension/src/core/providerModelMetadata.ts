/** Bound and sanitize native model metadata before sending it to the webview. */
import type { NativeModelCatalog, NativeModelOption } from "./providerCredentials";

type Row = Record<string, unknown>;
const object = (value: unknown): Row => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Row : {};

/** Copy only public catalog fields, applying the host's credential redactor to every string. */
export function normalizeModelCatalog(value: unknown, redact: (value: unknown) => string): NativeModelCatalog {
  const row = object(value);
  const text = (value: unknown, max = 256) => redact(value).slice(0, max);
  const modelId = (value: unknown) => typeof value === "string" && value.length <= 256 && !/[\s\x00-\x1f\x7f]/.test(value) ? text(value) : "";
  const effort = (value: unknown) => typeof value === "string" && /^[a-z][a-z0-9_-]{0,31}$/.test(value) ? text(value) : "";
  const seen = new Set<string>();
  const models: NativeModelOption[] = [];
  for (const value of (Array.isArray(row.models) ? row.models : []).slice(0, 500)) {
    const model = object(value);
    const id = modelId(model.id);
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const efforts = [...new Set((Array.isArray(model.reasoning_efforts) ? model.reasoning_efforts : []).slice(0, 32).map(effort).filter(Boolean))];
    models.push({
      id, label: text(model.label) || id,
      resolved_model: modelId(model.resolved_model),
      reasoning_efforts: model.reasoning_known === true ? efforts : [],
      reasoning_known: model.reasoning_known === true,
      default_reasoning_effort: effort(model.default_reasoning_effort),
    });
  }
  return {
    status: row.status === "ready" ? "ready" : row.status === "not_checked" ? "not_checked" : "unavailable",
    source: text(row.source),
    default_model: modelId(row.default_model),
    default_reasoning_effort: effort(row.default_reasoning_effort),
    configured_reasoning_effort: effort(row.configured_reasoning_effort),
    models,
    error: text(row.error, 1024) || (value === undefined ? "Update LeanFlow and refresh provider setup to load native model choices." : ""),
  };
}
