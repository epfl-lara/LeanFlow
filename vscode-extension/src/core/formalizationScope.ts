/** Bind formalization messages to a host-issued workspace generation. */
export interface FormalizationWorkspaceScope {
  id: string;
  generation: number;
  root: string;
}

export interface FormalizationRequestScope { channel: string; id: number; documentRequestId?: number }

export function formalizationRequestChannel(type: string): string {
  if (type === "formalizationProjectSetup") return "setup";
  if (type === "openFormalizationArtifact") return "artifact";
  if (type === "checkFormalization") return "currentness";
  if (type === "launchFormalization" || type === "resumeFormalization") return "launch";
  if (type === "proveFormalization") return "prove";
  if (/Recovery|recoverFormalizationPreparation|resolveFormalizationPreparation/.test(type)) return "recovery";
  if (/Conflict/.test(type)) return "conflicts";
  if (/Preparation|prepareFormalization/.test(type)) return "preparation";
  return "document";
}

export function sameFormalizationScope(left: FormalizationWorkspaceScope | undefined, right: FormalizationWorkspaceScope | undefined): boolean {
  return !!left && !!right && left.id === right.id && left.generation === right.generation && left.root === right.root;
}

/** Discard obsolete asynchronous replies before any view installs their state. */
export class FormalizationMessageGate {
  private current: FormalizationWorkspaceScope | undefined;
  private sequence = 0;
  private readonly requests = new Map<string, number>();

  acceptWorkspace(scope: FormalizationWorkspaceScope | undefined): boolean {
    if (!scope) return !this.current;
    if (this.current?.id === scope.id && scope.generation < this.current.generation) return false;
    if (!sameFormalizationScope(scope, this.current)) this.requests.clear();
    this.current = scope;
    return true;
  }

  scope(): FormalizationWorkspaceScope | undefined { return this.current; }

  request(type: string): FormalizationRequestScope {
    const channel = formalizationRequestChannel(type);
    const id = type.startsWith("cancel") ? this.requests.get(channel) ?? ++this.sequence : ++this.sequence;
    this.requests.set(channel, id);
    return { channel, id, documentRequestId: this.requests.get("document") ?? 0 };
  }

  accepts(message: { type: string; formalizationWorkspace?: FormalizationWorkspaceScope; formalizationRequest?: FormalizationRequestScope }): boolean {
    if (!message.type.startsWith("formalization")) return true;
    if (!sameFormalizationScope(message.formalizationWorkspace, this.current)) return false;
    const request = message.formalizationRequest;
    if (!request) return true;
    if (this.requests.get(request.channel) !== request.id) return false;
    return !["formalizationState", "formalizationActionCommitted", "formalizationPdf"].includes(message.type) || request.documentRequestId === (this.requests.get("document") ?? 0);
  }
}
