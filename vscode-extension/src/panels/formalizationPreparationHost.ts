/** Plan and prepare source workspaces through an owned cancellable controller process. */
import { cliPath } from "../core/cli";
import { formalizationWorkspaceRoot } from "../core/formalizationWorkspace";
import { startFormalizationPreparation, type FormalizationPreparationProcess } from "../core/formalizationPreparationProcess";
import { redactSensitiveText } from "../core/runPrivacy";
import type { LeanFlowServices } from "../core/services";
import type { HostMessage, WebviewMessage } from "../core/types";

export class FormalizationPreparationHost {
  private active: { documentId: string; revisionId: string; process: FormalizationPreparationProcess; cancelled: boolean } | undefined;
  constructor(private readonly services: LeanFlowServices, private readonly post: (message: HostMessage) => void) {}
  dispose(): void { this.active?.process.cancel(); }

  /** Cancel only this view's current document preparation, bypassing its serialized request queue. */
  cancel(message: Extract<WebviewMessage, { type: "cancelFormalizationPreparation" }>): void {
    if (!this.active || this.active.cancelled || this.active.documentId !== message.documentId || this.active.revisionId !== message.revisionId) return;
    this.active.cancelled = true;
    this.active.process.cancel();
    this.post({ type: "formalizationPreparation", documentId: message.documentId, revisionId: message.revisionId, status: "preparing", phase: "cancelling", cancellable: false, message: "Cancellation requested. Waiting for the controller to finish safe cleanup and preserve recovery evidence…", error: "" });
  }

  /** Freeze the visible revision before planning or acquiring controlled pinned infrastructure. */
  async handle(message: Extract<WebviewMessage, { type: "planFormalizationPreparation" | "prepareFormalization" }>): Promise<void> {
    const state = { type: "formalizationPreparation" as const, documentId: message.documentId, revisionId: message.revisionId };
    const service = this.services.formalizations;
    try {
      this.post({ ...state, status: message.type === "planFormalizationPreparation" ? "planning" : "preparing", phase: "inspection", cancellable: false, message: "Checking the selected source and current project readiness…", error: "" });
      const root = await formalizationWorkspaceRoot(this.services.project.root);
      service.bindSourceRoot(root);
      const selected = await service.show(message.documentId);
      if (selected.revision_id !== message.revisionId) throw new Error("Source revision changed; refresh before preparing.");
      if (message.type === "planFormalizationPreparation") {
        this.post({ ...state, status: "planning", message: "Inspecting existing libraries and dependencies…", error: "" });
        const plan = await service.preparationPlan(message.documentId, message.revisionId, message.preset, message.overrides);
        this.post({ ...state, status: "planned", plan, message: "Preparation plan inspected. Mathematical translation still needs its own review.", error: "" });
        return;
      }
      if (this.active) throw new Error("A formalization preparation is already active in this view.");
      this.post({ ...state, status: "preparing", message: "Preparing pinned project dependencies serially…", error: "" });
      const process = startFormalizationPreparation(cliPath(), ["formalization", "prepare", message.documentId, "--expected-revision", message.revisionId, "--preset", message.preset,
        ...(message.overrides ? [`--plan-json=${JSON.stringify(message.overrides)}`] : []), "--json"], root,
      (phase, text) => {
        const cancelled = this.active?.cancelled === true;
        this.post({ ...state, status: "preparing", phase: cancelled ? "cancelling" : phase, cancellable: !cancelled,
          message: cancelled ? `Waiting for safe cleanup: ${redactSensitiveText(text)}` : redactSensitiveText(text), error: "" });
      });
      const active = { documentId: message.documentId, revisionId: message.revisionId, process, cancelled: false };
      this.active = active;
      this.post({ ...state, status: "preparing", cancellable: true, message: "Preparing pinned dependencies. Cancellation waits for a safe stopping point.", error: "" });
      try {
        const snapshot = await process.result;
        if (snapshot.document_id !== message.documentId) throw new Error("Preparation returned a different source document.");
        const result = (snapshot as unknown as { preparation_result?: { accepted?: boolean; error?: string; status?: string } }).preparation_result;
        const accepted = result?.accepted === true;
        this.post({ ...state, snapshot, status: accepted ? "prepared" : result?.status === "cancelled" ? "cancelled" : "failed", message: accepted ? "Project dependencies prepared. Translate and review the representative slice before expanding." : result?.error || "Preparation did not receive a readiness receipt.", error: accepted ? "" : result?.error || "Project preparation needs attention." });
        this.post({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
      } catch (error) {
        const reason = redactSensitiveText(error instanceof Error ? error.message : String(error));
        this.post({ ...state, status: active.cancelled ? "cancelled" : "failed", message: reason, error: active.cancelled ? "" : reason });
      } finally {
        if (this.active === active) this.active = undefined;
        await this.refreshSelected(message.documentId);
      }
    } catch (error) {
      const reason = redactSensitiveText(error instanceof Error ? error.message : String(error));
      this.post({ ...state, status: "failed", message: reason, error: reason });
    }
  }

  /** Refresh actual readiness after every owned process, including interrupted preparation. */
  private async refreshSelected(documentId: string): Promise<void> {
    try {
      await this.services.reload();
      const snapshot = await this.services.formalizations.show(documentId);
      this.post({ type: "formalizationState", documentId, snapshot, error: "" });
    } catch (error) {
      this.post({ type: "formalizationState", documentId, snapshot: null, error: `Readiness could not be refreshed: ${redactSensitiveText(error instanceof Error ? error.message : String(error))}. Inspect project preparation recovery before continuing.` });
    }
  }
}
