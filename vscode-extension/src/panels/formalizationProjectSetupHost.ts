/** Bind the shared manual project setup service to one host-issued Formalization workspace. */
import type * as vscode from "vscode";
import { sameFormalizationScope, type FormalizationWorkspaceScope } from "../core/formalizationScope";
import { formalizationWorkspaceRoot } from "../core/formalizationWorkspace";
import { redactSensitiveText } from "../core/runPrivacy";
import type { LeanFlowServices } from "../core/services";
import type { HostMessage, WebviewMessage } from "../core/types";
import { formalizationOperation } from "./formalizationOperation";

type SetupMessage = Extract<WebviewMessage, { type: "formalizationProjectSetup" }>;

export class FormalizationProjectSetupHost {
  private binding: { root: string; scope: FormalizationWorkspaceScope; post: (message: HostMessage) => void } | undefined;
  private readonly listener: vscode.Disposable | undefined;

  constructor(private readonly services: LeanFlowServices, private readonly post: (message: HostMessage) => void) {
    this.listener = services.setup?.onDidChange?.(() => {
      const binding = this.binding;
      if (binding && this.current(binding.scope) && services.setup.state().root === binding.root) {
        binding.post({ type: "formalizationProjectSetupState", state: services.setup.stateForRoot(binding.root), error: "" });
      }
    });
  }

  dispose(): void { this.listener?.dispose(); this.binding = undefined; }

  private current(scope: FormalizationWorkspaceScope): boolean {
    return sameFormalizationScope(scope, this.services.formalizations.workspaceScope());
  }

  /** Keep cancellation and cross-workspace inspection outside the source-operation queue. */
  async handle(message: SetupMessage, scope: FormalizationWorkspaceScope): Promise<void> {
    let post = (response: HostMessage) => {
      if (this.current(scope)) this.post({ ...response, formalizationWorkspace: scope, formalizationRequest: message.formalizationRequest });
    };
    try {
      this.services.formalizations.assertWorkspace(scope);
      const root = await formalizationWorkspaceRoot(scope.root);
      this.services.formalizations.assertWorkspace(scope);
      const operation = formalizationOperation(this.services, scope, root, this.post, message.formalizationRequest);
      post = operation.post;
      const setup = this.services.setup;
      if (!setup) throw new Error("Project setup is unavailable. Reload the LeanFlow extension before continuing.");
      this.binding = { root, scope, post };
      const isCurrent = () => this.current(scope);
      if (message.action === "inspect") await setup.refresh(this.services.cli.ok, root, isCurrent);
      else if (message.action === "cancel") setup.cancelForRoot(root);
      else if (message.action === "log") await setup.showLogForRoot(root);
      else await setup.startForRoot(root, { retry: message.action === "retry", repair: message.action === "repair", isCurrent });
      operation.assertCurrent();
      post({ type: "formalizationProjectSetupState", state: setup.stateForRoot(root), error: "" });
    } catch (error) {
      const reason = redactSensitiveText(error instanceof Error ? error.message : String(error));
      const state = this.services.setup?.stateForRoot(scope.root);
      if (state) post({ type: "formalizationProjectSetupState", state, error: reason });
      else post({ type: "notify", level: "error", message: reason });
    }
  }
}
