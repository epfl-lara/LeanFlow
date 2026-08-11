/** Typed access to the webview↔host channel and the persisted view state. */
import type { WebviewMessage } from "../src/core/types";

interface VsCodeApi {
  postMessage(message: unknown): void;
  getState<T>(): T | undefined;
  setState<T>(state: T): void;
}

declare function acquireVsCodeApi(): VsCodeApi;

// acquireVsCodeApi may only be called once per webview document.
const api: VsCodeApi = acquireVsCodeApi();

export function post(message: WebviewMessage): void {
  api.postMessage(message);
}

/**
 * Read UI state that should survive the webview being hidden and restored.
 *
 * Only view preferences live here — selected tab, filters, a half-filled form.
 * Anything authoritative comes from the host on every state push.
 *
 * State persisted by an older version of the extension can be missing fields
 * the current one expects, so nested objects are merged onto the fallback
 * rather than replacing it. A missing field would otherwise reach argv
 * construction as `undefined`.
 */
export function loadViewState<T extends object>(fallback: T): T {
  const stored = api.getState<Partial<T>>();
  if (stored === undefined) {
    return fallback;
  }
  const merged: T = { ...fallback, ...stored };
  for (const [key, value] of Object.entries(fallback)) {
    const storedValue = (stored as Record<string, unknown>)[key];
    if (
      value !== null &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      storedValue !== null &&
      typeof storedValue === "object" &&
      !Array.isArray(storedValue)
    ) {
      (merged as Record<string, unknown>)[key] = { ...value, ...storedValue };
    }
  }
  return merged;
}

export function saveViewState<T>(state: T): void {
  api.setState(state);
}

export const mode: "panel" | "sidebar" =
  (window as unknown as { __LEANFLOW_MODE__?: "panel" | "sidebar" }).__LEANFLOW_MODE__ ??
  "panel";
