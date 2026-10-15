/** Read controller-owned inventories and submit revision-bound repairs without editing state files. */
import {
  parseFormalizationSnapshot,
  type FormalizationAction,
  type FormalizationArtifact,
  type FormalizationSnapshot,
} from "./formalization";
import { parseFormalizationConflicts, type FormalizationConflictsSnapshot } from "./formalizationConflicts";
import type { FormalizationPreparationPlan, FormalizationPreparationPreset } from "./formalizationPreparation";
import { parseFormalizationPreparationRecovery, type FormalizationPreparationRecoverySnapshot } from "./formalizationPreparationRecovery";
import { randomUUID } from "node:crypto";
import { sameFormalizationScope, type FormalizationWorkspaceScope } from "./formalizationScope";

type Transport = (args: string[], options: { cwd: string; timeoutMs?: number }) => Promise<string>;
const INSPECTION_TIMEOUT_MS = 60_000;
const INSPECTION_COMMANDS = new Set(["list", "show", "plan", "preparation-conflicts", "conflicts", "artifact", "conflict-artifact"]);

export class FormalizationService {
  private readonly workspaceId = randomUUID();
  private generation = 0;
  private observedRoot = "";
  private readonly previews = new Map<string, FormalizationWorkspaceScope>();
  constructor(private readonly projectRoot: () => string, private readonly transport: Transport) {}

  invalidateWorkspace(): void { this.previews.clear(); this.generation += 1; }

  /** Preserve only a verified frozen preview's host-recorded originating workspace. */
  registerPreview(uri: string, scope: FormalizationWorkspaceScope): void { this.assertWorkspace(scope); this.previews.set(uri, scope); }
  previewRoot(uri: string | undefined): string {
    const scope = uri && this.previews.get(uri);
    return scope && scope.id === this.workspaceId && scope.generation === this.generation ? scope.root : "";
  }
  workspaceRoot(): string { return this.projectRoot(); }

  /** Issue identity from host state; webview roots never select a filesystem target. */
  workspaceScope(): FormalizationWorkspaceScope {
    const root = this.workspaceRoot();
    if (root !== this.observedRoot) { this.observedRoot = root; this.generation += 1; }
    return { id: this.workspaceId, generation: this.generation, root };
  }

  assertWorkspace(scope: FormalizationWorkspaceScope): void {
    if (!sameFormalizationScope(scope, this.workspaceScope())) throw new Error("The formalization workspace changed. Refresh before continuing.");
  }

  /** Freeze all calls and continuations of one operation to its original host workspace. */
  forWorkspace(scope: FormalizationWorkspaceScope, root = scope.root): FormalizationService {
    this.assertWorkspace(scope);
    return new FormalizationService(() => { this.assertWorkspace(scope); return root; }, async (args, options) => {
      this.assertWorkspace(scope);
      const result = await this.transport(args, { ...options, cwd: root });
      this.assertWorkspace(scope);
      return result;
    });
  }

  private async call(args: string[]): Promise<unknown> {
    const cwd = this.workspaceRoot();
    if (!cwd) throw new Error("Open a source workspace or LeanFlow project to inspect formalization material.");
    return JSON.parse(await this.transport(["formalization", ...args, "--json"], { cwd, ...(INSPECTION_COMMANDS.has(args[0]) ? { timeoutMs: INSPECTION_TIMEOUT_MS } : {}) }));
  }

  /** Return the backend's current document list, without reading its private registry. */
  async list(): Promise<FormalizationSnapshot[]> {
    const value = await this.call(["list"]) as { schema_version?: number; documents?: unknown[] };
    if (value.schema_version !== 1 || !Array.isArray(value.documents)) throw new Error("Invalid formalization document list.");
    return value.documents.map(parseFormalizationSnapshot);
  }

  async show(documentId: string): Promise<FormalizationSnapshot> {
    const snapshot = parseFormalizationSnapshot(await this.call(["show", documentId]));
    if (snapshot.document_id !== documentId) throw new Error("The formalization service returned a different document.");
    return snapshot;
  }

  /** Intake freezes source evidence but never launches a model or writes Lean proposals. */
  async inspect(source: string, companionPdf?: string): Promise<FormalizationSnapshot> {
    return parseFormalizationSnapshot(await this.call(["inspect", source, ...(companionPdf ? ["--companion-pdf", companionPdf] : [])]));
  }

  async action(documentId: string, revisionId: string, action: FormalizationAction): Promise<FormalizationSnapshot> {
    const snapshot = parseFormalizationSnapshot(await this.call([
      "action", documentId, "--expected-revision", revisionId, `--action-json=${JSON.stringify(action)}`,
    ]));
    if (snapshot.document_id !== documentId) throw new Error("The repair returned a different source inventory.");
    return snapshot;
  }

  /** Inspect the controller's bounded setup intent before any infrastructure writes. */
  async preparationPlan(documentId: string, revisionId: string, preset: FormalizationPreparationPreset, overrides?: Record<string, unknown>): Promise<FormalizationPreparationPlan> {
    const value = await this.call(["plan", documentId, "--expected-revision", revisionId, "--preset", preset,
      ...(overrides ? [`--plan-json=${JSON.stringify(overrides)}`] : [])]) as { schema_version?: number; plan?: FormalizationPreparationPlan; semantic_approval?: boolean };
    if (!value || value.schema_version !== 1 || !value.plan || typeof value.plan !== "object" || Array.isArray(value.plan) || value.semantic_approval !== false) throw new Error("No bounded preparation plan was returned by the controller.");
    return value.plan;
  }

  /** Inspect infrastructure conflicts even when ordinary formalization reads are blocked. */
  async preparationRecovery(): Promise<FormalizationPreparationRecoverySnapshot> {
    return parseFormalizationPreparationRecovery(await this.call(["preparation-conflicts"]));
  }

  /** Return controller-derived reconciliation argv only after verifying the visible observation. */
  async preparationResolutionArgs(transactionId: string, observationId: string, reason: string): Promise<string[]> {
    const transaction = (await this.preparationRecovery()).transactions.find(row => row.transaction_id === transactionId);
    if (!transaction || transaction.observation_id !== observationId) throw new Error("Preparation versions changed; refresh and inspect all entries again.");
    const observation = { transaction_id: transactionId, observation_id: observationId, history_id: transaction.history_id,
      destinations: transaction.destinations, retained_slots: transaction.retained_slots };
    return ["formalization", "resolve-preparation", `--resolution-json=${JSON.stringify({ observation, reason })}`, "--json"];
  }

  /** Read all preserved-version pages against a stable controller observation. */
  async conflicts(transactionId?: string): Promise<FormalizationConflictsSnapshot> {
    const args = ["conflicts", ...(transactionId ? ["--transaction-id", transactionId] : [])];
    const snapshot = parseFormalizationConflicts(await this.call(args));
    for (const transaction of snapshot.transactions) {
      let offset = transaction.next_version_offset;
      while (offset !== null) {
        const page = parseFormalizationConflicts(await this.call([
          "conflicts", "--transaction-id", transaction.transaction_id, "--version-offset", String(offset),
        ]));
        const row = page.transactions[0];
        if (page.transactions.length !== 1 || row.transaction_id !== transaction.transaction_id || row.observation_id !== transaction.observation_id ||
            row.version_count !== transaction.version_count || row.version_offset !== offset || row.next_version_offset !== null && row.next_version_offset <= offset) throw new Error("Publication versions changed; refresh all versions before resolving.");
        transaction.versions.push(...row.versions);
        offset = row.next_version_offset;
      }
      if (transaction.versions.length !== transaction.version_count || new Set(transaction.versions.map(version => version.version_id)).size !== transaction.versions.length) throw new Error("Publication version inventory is incomplete; refresh before resolving.");
      transaction.version_offset = 0;
      transaction.next_version_offset = null;
    }
    return snapshot;
  }

  /** Fetch exact observed maps in the host; clients cannot supply destination paths or hashes. */
  async resolveConflict(transactionId: string, observationId: string, reason: string): Promise<void> {
    const snapshot = await this.conflicts(transactionId);
    const transaction = snapshot.transactions.find(row => row.transaction_id === transactionId);
    if (!transaction || transaction.observation_id !== observationId) throw new Error("Publication versions changed; refresh and inspect them again before resolving.");
    const request = { transaction_id: transactionId, observation_id: observationId,
      destinations: transaction.destinations, retained_slots: transaction.retained_slots, choice: "keep_current", reason };
    const value = await this.call(["resolve-conflict", `--resolution-json=${JSON.stringify(request)}`]) as { transaction_id?: string; requires_new_acceptance?: boolean };
    if (value.transaction_id !== transactionId || value.requires_new_acceptance !== true) throw new Error("The controller returned no valid conflict resolution receipt.");
  }

  /** Resolve an opaque preserved-version identity through the controller, never a supplied path. */
  async conflictArtifact(transactionId: string, observationId: string, versionId: string): Promise<FormalizationArtifact> {
    const artifact = await this.call(["conflict-artifact", transactionId, versionId, "--expected-observation", observationId]) as FormalizationArtifact;
    if (artifact.id !== versionId || typeof artifact.path !== "string" || !/^(?:sha256:)?[a-f0-9]{64}$/i.test(artifact.sha256)) throw new Error("The preserved version has no valid controller identity.");
    return artifact;
  }

  /** Ask the controller to resolve an allowlisted artifact; paths never come from the webview. */
  async artifact(documentId: string, revisionId: string, artifactId: string): Promise<FormalizationArtifact> {
    const artifact = await this.call([
      "artifact", documentId, artifactId, "--expected-revision", revisionId,
    ]) as FormalizationArtifact;
    if (artifact.id !== artifactId || typeof artifact.path !== "string" ||
        !/^(?:sha256:)?[a-f0-9]{64}$/i.test(artifact.sha256)) {
      throw new Error("The source artifact has no valid controller identity.");
    }
    return artifact;
  }
}
