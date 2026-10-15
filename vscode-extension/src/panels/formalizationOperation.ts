/** Keep each asynchronous editor operation on its original host-owned workspace. */
import type { FormalizationWorkspaceScope, FormalizationRequestScope } from "../core/formalizationScope";
import type { LeanFlowServices } from "../core/services";
import type { HostMessage } from "../core/types";

export type FormalizationHostServices = Pick<LeanFlowServices, "project" | "formalizations" | "reload">;

export interface FormalizationOperation {
  scope: FormalizationWorkspaceScope;
  /** Set only after resolving an exact host-tracked stopped campaign. */
  resumeTrackedId?: string;
  sourceDocumentId?: string;
  services: FormalizationHostServices;
  assertCurrent(): void;
  post(message: HostMessage): void;
}

export function formalizationOperation(services: FormalizationHostServices, scope: FormalizationWorkspaceScope, root: string, post: (message: HostMessage) => void, request?: FormalizationRequestScope): FormalizationOperation {
  const assertCurrent = () => services.formalizations.assertWorkspace(scope);
  assertCurrent();
  return {
    scope, assertCurrent,
    services: {
      project: { ...services.project, root },
      formalizations: services.formalizations.forWorkspace(scope, root),
      async reload() { assertCurrent(); await services.reload(); assertCurrent(); },
    },
    post(message) {
      try { assertCurrent(); } catch { return; }
      post({ ...message, formalizationWorkspace: scope, formalizationRequest: request });
    },
  };
}
