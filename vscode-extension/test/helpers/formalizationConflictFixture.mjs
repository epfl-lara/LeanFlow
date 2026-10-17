/** Publication observations supplied by the controller, never constructed from UI paths. */
export function formalizationConflictFixture() {
  return { schema_version: 1, transactions: [{
    transaction_id: "transaction-1", observation_id: "observation-1", revision_id: "revision-1", state: "CONFLICTED", active: true,
    destinations: { "Main.lean": "a".repeat(64) }, retained_slots: { "displaced/entry-1": "b".repeat(64) },
    conflicts: ["Concurrent save preserved for Main.lean"], resolutions: [],
    version_count: 2, version_offset: 0, next_version_offset: null,
    versions: [
      { version_id: "version-current", group: "destinations", display_path: "Main.lean", label: "Current workspace", sha256: "a".repeat(64), kind: "lean", available: true },
      { version_id: "version-retained", group: "retained_slots", display_path: "Main.lean (retained save)", label: "Retained concurrent save", sha256: "b".repeat(64), kind: "lean", available: true },
    ],
  }] };
}
