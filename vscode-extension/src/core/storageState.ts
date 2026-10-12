/** Validate storage metrics crossing the CLI boundary. */
export interface StorageState {
  checking: boolean;
  error: string;
  managedBytes: number;
  pinnedBytes: number;
  workspaceBytes: number;
  freeBytes: number;
  cacheTargetBytes: number;
  minFreeBytes: number;
  automaticCleanup: boolean;
}

/** Reject unavailable or malformed metrics instead of showing invented zero usage. */
export function normalizeStorage(value: unknown): Omit<StorageState, "checking" | "error"> {
  if (!value || typeof value !== "object") throw new Error("Storage metadata is unavailable.");
  const raw = value as Record<string, unknown>;
  const number = (key: string): number => {
    const value = raw[key];
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) throw new Error("Invalid storage metadata.");
    return value;
  };
  if (typeof raw.automatic_cleanup !== "boolean") throw new Error("Invalid storage policy.");
  return {
    managedBytes: number("managed_bytes"), pinnedBytes: number("pinned_bytes"),
    workspaceBytes: number("workspace_bytes"), freeBytes: number("free_bytes"),
    cacheTargetBytes: number("cache_target_bytes"), minFreeBytes: number("min_free_bytes"),
    automaticCleanup: raw.automatic_cleanup,
  };
}
