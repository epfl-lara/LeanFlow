/** Recover owned setup transactions independently of blocked mathematical document reads. */
import { cliPath } from "../core/cli";
import { startFormalizationJsonProcess, type FormalizationPreparationProcess } from "../core/formalizationPreparationProcess";
import { sameFormalizationScope, type FormalizationWorkspaceScope } from "../core/formalizationScope";
import type { FormalizationOperation } from "./formalizationOperation";
import type { HostMessage, WebviewMessage } from "../core/types";
import { redactSensitiveText } from "../core/runPrivacy";

export class FormalizationRecoveryHost {
  private active: { process: FormalizationPreparationProcess<Record<string, unknown>>; cancelled: boolean; scope: FormalizationWorkspaceScope; post: (message: HostMessage) => void } | undefined;
  dispose(): void { this.active?.process.cancel(); }
  cancelOtherWorkspace(scope: FormalizationWorkspaceScope): void {
    if (this.active && !this.active.cancelled && !sameFormalizationScope(this.active.scope, scope)) { this.active.cancelled = true; this.active.process.cancel(); }
  }
  cancel(scope: FormalizationWorkspaceScope): void {
    if (!sameFormalizationScope(this.active?.scope, scope) || !this.active || this.active.cancelled) return;
    this.active.cancelled = true;
    this.active.process.cancel();
    this.active.post({ type: "formalizationPreparationRecovery", snapshot: null, error: "", busy: true, cancellable: false, cancelling: true,
      message: "Cancellation requested. Waiting for the controller to safely preserve infrastructure and recovery evidence…" });
  }

  /** Bind resolution to freshly fetched identities and refresh readiness after every owned attempt. */
  async handle(message: Extract<WebviewMessage, { type: "listFormalizationPreparationRecovery" | "recoverFormalizationPreparation" | "resolveFormalizationPreparation" }>, operation: FormalizationOperation): Promise<void> {
    const services = operation.services;
    const post = operation.post;
    const service = services.formalizations;
    const mutating = message.type !== "listFormalizationPreparationRecovery";
    let error = "";
    let resultMessage = "";
    let refreshed = false;
    post({ type: "formalizationPreparationRecovery", snapshot: null, error: "", busy: true, cancellable: false,
      message: "Inspecting current infrastructure and recovery observations…" });
    try {
      if (mutating) {
        if (this.active) throw new Error("Infrastructure recovery is already active in this view.");
        const args = message.type === "resolveFormalizationPreparation"
          ? await service.preparationResolutionArgs(message.transactionId, message.observationId, message.reason)
          : ["formalization", "recover-preparation", "--json"];
        operation.assertCurrent();
        const process = startFormalizationJsonProcess(cliPath(), args, service.workspaceRoot(),
          (_phase, text) => post({ type: "formalizationPreparationRecovery", snapshot: null, error: "", busy: true,
            cancellable: !this.active?.cancelled, cancelling: this.active?.cancelled,
            message: this.active?.cancelled ? `Waiting for safe cleanup: ${redactSensitiveText(text)}` : redactSensitiveText(text) }),
          value => { if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("The controller returned no infrastructure recovery record."); return value as Record<string, unknown>; });
        const active = { process, cancelled: false, scope: operation.scope, post };
        this.active = active;
        post({ type: "formalizationPreparationRecovery", snapshot: null, error: "", busy: true, cancellable: true,
          message: "Recovering infrastructure. Cancellation waits for a safe stopping point…" });
        try {
          const result = await process.result;
          if (message.type === "resolveFormalizationPreparation" && result.state !== "RESOLVED") throw new Error("The controller did not commit a resolved infrastructure record.");
          resultMessage = message.type === "resolveFormalizationPreparation"
            ? "Resolution recorded, including retained and unavailable versions. Current project readiness was refreshed."
            : "Recovery inspected. Resolve reported conflicts before preparing the project.";
        } finally { if (this.active === active) this.active = undefined; }
      }
    } catch (failure) {
      error = redactSensitiveText(failure instanceof Error ? failure.message : String(failure));
    } finally {
      if (mutating) {
        try { await services.reload(); refreshed = true; }
        catch (failure) { error = [error, `Readiness refresh failed: ${redactSensitiveText(failure instanceof Error ? failure.message : String(failure))}`].filter(Boolean).join(" "); }
      }
      try {
        const snapshot = await service.preparationRecovery();
        post({ type: "formalizationPreparationRecovery", snapshot, error, busy: false, cancellable: false, refreshed, message: resultMessage });
      } catch (failure) {
        post({ type: "formalizationPreparationRecovery", snapshot: null, busy: false, cancellable: false, refreshed,
          error: [error, redactSensitiveText(failure instanceof Error ? failure.message : String(failure))].filter(Boolean).join(" ") });
      }
    }
  }
}
