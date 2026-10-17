/** Preserve exact setup-recovery observations independently of a readable document snapshot. */
export interface FormalizationPreparationObservation {
  transaction_id: string; observation_id: string; history_id: string;
  destinations: Record<string, string | null>; retained_slots: Record<string, string | null>;
  state: string; active: boolean; conflicts: string[];
  resolvable?: boolean; revokes_current_receipt?: boolean; unavailable_versions?: Record<string, string>;
  resolutions: { resolution_id: string; reason: string; receipt_revoked: boolean }[];
}
export interface FormalizationPreparationRecoverySnapshot { schema_version: 1; transactions: FormalizationPreparationObservation[] }

/** Validate entry identities without pretending dependency directories are rendered artifacts. */
export function parseFormalizationPreparationRecovery(value: unknown): FormalizationPreparationRecoverySnapshot {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid project preparation recovery inventory.");
  const raw = value as Partial<FormalizationPreparationRecoverySnapshot>;
  const id = (value: unknown) => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(value);
  const map = (value: unknown) => !!value && typeof value === "object" && !Array.isArray(value) && Object.entries(value).every(([path, hash]) => !!path && !path.includes("\0") && (hash === null || typeof hash === "string" && /^[a-f0-9]{64}$/i.test(hash)));
  if (raw.schema_version !== 1 || !Array.isArray(raw.transactions) || raw.transactions.some(row => !row || !id(row.transaction_id) || !id(row.observation_id) || !id(row.history_id) ||
      !map(row.destinations) || !map(row.retained_slots) || typeof row.state !== "string" || typeof row.active !== "boolean" ||
      row.resolvable !== undefined && typeof row.resolvable !== "boolean" || row.revokes_current_receipt !== undefined && typeof row.revokes_current_receipt !== "boolean" ||
      row.unavailable_versions !== undefined && !map(row.unavailable_versions) ||
      !Array.isArray(row.conflicts) || row.conflicts.some(issue => typeof issue !== "string") || !Array.isArray(row.resolutions) || row.resolutions.some(item => !item || !id(item.resolution_id) || typeof item.reason !== "string"))) throw new Error("Invalid project preparation recovery observation.");
  return raw as FormalizationPreparationRecoverySnapshot;
}
