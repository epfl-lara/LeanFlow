/** Plan and prepare source workspaces through an owned cancellable controller process. */
import { cliPath } from "../core/cli";
import { startFormalizationPreparation, type FormalizationPreparationProcess } from "../core/formalizationPreparationProcess";
import { redactSensitiveText } from "../core/runPrivacy";
import { sameFormalizationScope, type FormalizationWorkspaceScope } from "../core/formalizationScope";
import type { FormalizationHostServices, FormalizationOperation } from "./formalizationOperation";
import type { HostMessage, WebviewMessage } from "../core/types";

export class FormalizationPreparationHost {
  private active: { documentId: string; revisionId: string; process: FormalizationPreparationProcess; cancelled: boolean; scope: FormalizationWorkspaceScope; post: (message: HostMessage) => void } | undefined;
  dispose(): void { this.active?.process.cancel(); }

  /** Stop an owned old-workspace process so it cannot hold the new view's queue. */
  cancelOtherWorkspace(scope: FormalizationWorkspaceScope): void {
    if (this.active && !this.active.cancelled && !sameFormalizationScope(this.active.scope, scope)) { this.active.cancelled = true; this.active.process.cancel(); }
  }

  /** Cancel only this view's current document preparation, bypassing its serialized request queue. */
  cancel(message: Extract<WebviewMessage, { type: "cancelFormalizationPreparation" }>, scope: FormalizationWorkspaceScope): void {
    if (!sameFormalizationScope(this.active?.scope, scope) || !this.active || this.active.cancelled || this.active.documentId !== message.documentId || this.active.revisionId !== message.revisionId) return;
    this.active.cancelled = true;
    this.active.process.cancel();
    this.active.post({ type: "formalizationPreparation", documentId: message.documentId, revisionId: message.revisionId, status: "preparing", phase: "cancelling", cancellable: false, message: "Cancellation requested. Waiting for the controller to finish safe cleanup and preserve recovery evidence…", error: "" });
  }

  /** Freeze the visible revision before planning or acquiring controlled pinned infrastructure. */
  async handle(message: Extract<WebviewMessage, { type: "planFormalizationPreparation" | "prepareFormalization" }>, operation: FormalizationOperation): Promise<void> {
    const services = operation.services;
    const post = operation.post;
    const state = { type: "formalizationPreparation" as const, documentId: message.documentId, revisionId: message.revisionId };
    const service = services.formalizations;
    try {
      post({ ...state, status: message.type === "planFormalizationPreparation" ? "planning" : "preparing", phase: "inspection", cancellable: false, message: "Checking the selected source and current project readiness…", error: "" });
      const root = services.project.root;
      operation.assertCurrent();
      const selected = await service.show(message.documentId);
      if (selected.revision_id !== message.revisionId) throw new Error("Source revision changed; refresh before preparing.");
      if (message.type === "planFormalizationPreparation") {
        post({ ...state, status: "planning", message: "Inspecting existing libraries and dependencies…", error: "" });
        const plan = await service.preparationPlan(message.documentId, message.revisionId, message.preset, message.overrides);
        post({ ...state, status: "planned", plan, message: "Preparation plan inspected. Mathematical translation still needs its own review.", error: "" });
        return;
      }
      if (this.active) throw new Error("A formalization preparation is already active in this view.");
      post({ ...state, status: "preparing", message: "Preparing pinned project dependencies serially…", error: "" });
      operation.assertCurrent();
      const process = startFormalizationPreparation(cliPath(), ["formalization", "prepare", message.documentId, "--expected-revision", message.revisionId, "--preset", message.preset,
        ...(message.overrides ? [`--plan-json=${JSON.stringify(message.overrides)}`] : []), "--json"], root,
      (phase, text) => {
        const cancelled = this.active?.cancelled === true;
        post({ ...state, status: "preparing", phase: cancelled ? "cancelling" : phase, cancellable: !cancelled,
          message: cancelled ? `Waiting for safe cleanup: ${redactSensitiveText(text)}` : redactSensitiveText(text), error: "" });
      });
      const active = { documentId: message.documentId, revisionId: message.revisionId, process, cancelled: false, scope: operation.scope, post };
      this.active = active;
      post({ ...state, status: "preparing", cancellable: true, message: "Preparing pinned dependencies. Cancellation waits for a safe stopping point.", error: "" });
      try {
        const snapshot = await process.result;
        if (snapshot.document_id !== message.documentId) throw new Error("Preparation returned a different source document.");
        const result = (snapshot as unknown as { preparation_result?: { accepted?: boolean; error?: string; status?: string } }).preparation_result;
        const accepted = result?.accepted === true;
        post({ ...state, snapshot, status: accepted ? "prepared" : result?.status === "cancelled" ? "cancelled" : "failed", message: accepted ? "Project dependencies prepared. Translate and review the representative slice before expanding." : result?.error || "Preparation did not receive a readiness receipt.", error: accepted ? "" : result?.error || "Project preparation needs attention." });
        post({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
      } catch (error) {
        const reason = redactSensitiveText(error instanceof Error ? error.message : String(error));
        post({ ...state, status: active.cancelled ? "cancelled" : "failed", message: reason, error: active.cancelled ? "" : reason });
      } finally {
        if (this.active === active) this.active = undefined;
        await this.refreshSelected(message.documentId, services, post);
      }
    } catch (error) {
      const reason = redactSensitiveText(error instanceof Error ? error.message : String(error));
      post({ ...state, status: "failed", message: reason, error: reason });
    }
  }

  /** Refresh actual readiness after every owned process, including interrupted preparation. */
  private async refreshSelected(documentId: string, services: FormalizationHostServices, post: (message: HostMessage) => void): Promise<void> {
    try {
      await services.reload();
      const snapshot = await services.formalizations.show(documentId);
      post({ type: "formalizationState", documentId, snapshot, error: "" });
    } catch (error) {
      post({ type: "formalizationState", documentId, snapshot: null, error: `Readiness could not be refreshed: ${redactSensitiveText(error instanceof Error ? error.message : String(error))}. Inspect project preparation recovery before continuing.` });
    }
  }
}
