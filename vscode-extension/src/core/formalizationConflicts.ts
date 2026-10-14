/** Validate controller-owned publication observations without treating paths as UI capabilities. */
export interface FormalizationConflictVersion {
  version_id: string;
  group: "destinations" | "retained_slots" | "planned_before" | "planned_after";
  display_path: string;
  label: string;
  sha256: string | null;
  kind: string;
  available: boolean;
}

export interface FormalizationConflictTransaction {
  transaction_id: string;
  observation_id: string;
  revision_id: string;
  state: string;
  active: boolean;
  destinations: Record<string, string | null>;
  retained_slots: Record<string, string | null>;
  conflicts: string[];
  resolutions: { reason: string; resolution_id: string }[];
  versions: FormalizationConflictVersion[];
  version_count: number;
  version_offset: number;
  next_version_offset: number | null;
  unavailable_journal?: boolean;
  unavailable_versions?: Record<string, string>;
  revoked_revision_ids?: string[];
}

export interface FormalizationConflictsSnapshot {
  schema_version: 1;
  transactions: FormalizationConflictTransaction[];
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

const identity = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(value);
const hash = (value: unknown): value is string | null => value === null || typeof value === "string" && (/^[a-f0-9]{64}$/i.test(value) || value === "nonregular");

/** Keep live content maps exact; resolution requests are constructed in the host from these maps. */
function observedMap(value: unknown): value is Record<string, string | null> {
  const entries = record(value);
  return !!entries && Object.entries(entries).every(([name, checksum]) => name.length > 0 && !name.includes("\0") && hash(checksum));
}

/** Reject incomplete or malformed conflict evidence before exposing a resolution control. */
export function parseFormalizationConflicts(value: unknown): FormalizationConflictsSnapshot {
  const raw = record(value);
  if (!raw || raw.schema_version !== 1 || !Array.isArray(raw.transactions)) throw new Error("Unsupported formalization conflict inventory.");
  for (const entry of raw.transactions) {
    const row = record(entry);
    if (!row || !identity(row.transaction_id) || !identity(row.observation_id) || !identity(row.revision_id) ||
        typeof row.state !== "string" || typeof row.active !== "boolean" || !observedMap(row.destinations) || !observedMap(row.retained_slots) ||
        !Array.isArray(row.conflicts) || row.conflicts.some(issue => typeof issue !== "string") ||
        !Array.isArray(row.resolutions) || row.resolutions.some(value => !record(value) || typeof value.reason !== "string" || !identity(value.resolution_id)) || !Array.isArray(row.versions) ||
        row.unavailable_journal !== undefined && typeof row.unavailable_journal !== "boolean" ||
        row.unavailable_versions !== undefined && (!observedMap(row.unavailable_versions) || Object.values(row.unavailable_versions).some(value => value === null || value === "nonregular")) ||
        row.revoked_revision_ids !== undefined && (!Array.isArray(row.revoked_revision_ids) || row.revoked_revision_ids.some(value => !identity(value))) ||
        !Number.isSafeInteger(row.version_count) || Number(row.version_count) < 0 ||
        !Number.isSafeInteger(row.version_offset) || Number(row.version_offset) < 0 ||
        row.next_version_offset !== null && (!Number.isSafeInteger(row.next_version_offset) || Number(row.next_version_offset) <= Number(row.version_offset)) ||
        Number(row.version_offset) + row.versions.length > Number(row.version_count)) throw new Error("Invalid formalization conflict observation.");
    if (row.unavailable_journal === true && (Number(row.version_count) !== 0 || row.versions.length !== 0 || row.next_version_offset !== null)) throw new Error("Unavailable publication history cannot contain reconstructed versions.");
    for (const value of row.versions) {
      const version = record(value);
      if (!version || !identity(version.version_id) || !["destinations", "retained_slots", "planned_before", "planned_after"].includes(String(version.group)) ||
          typeof version.display_path !== "string" || typeof version.label !== "string" || !hash(version.sha256) || typeof version.kind !== "string" || typeof version.available !== "boolean") throw new Error("Invalid preserved publication version.");
    }
    if (new Set(row.versions.map(version => version.version_id)).size !== row.versions.length) throw new Error("Duplicate publication version identity.");
  }
  if (new Set(raw.transactions.map(row => row.transaction_id)).size !== raw.transactions.length) throw new Error("Duplicate publication transaction identity.");
  return raw as unknown as FormalizationConflictsSnapshot;
}
