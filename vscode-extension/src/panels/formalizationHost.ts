/** Route formalization UI actions through the owning host's controller and verified artifacts. */
import * as path from "node:path";
import { lstat, realpath } from "node:fs/promises";
import * as vscode from "vscode";
import type { FormalizationArtifact, FormalizationSnapshot } from "../core/formalization";
import { readFormalizationArtifact } from "../core/formalizationArtifact";
import { formalizationWorkspaceRoot } from "../core/formalizationWorkspace";
import { redactSensitiveText } from "../core/runPrivacy";
import type { LeanFlowServices } from "../core/services";
import { resolveExistingProjectPath } from "../core/storageSecurity";
import type { HostMessage, LaunchRequest, WebviewMessage } from "../core/types";
import { sameFormalizationScope, type FormalizationWorkspaceScope } from "../core/formalizationScope";
import { formalizationOperation, type FormalizationOperation } from "./formalizationOperation";
import { FormalizationPreparationHost } from "./formalizationPreparationHost";
import { FormalizationRecoveryHost } from "./formalizationRecoveryHost";
import { FormalizationProjectSetupHost } from "./formalizationProjectSetupHost";
import { FrozenArtifactDocuments } from "./frozenArtifactDocuments";

const MESSAGE_TYPES = new Set([
  "formalizationProjectSetup",
  "listFormalizations", "inspectFormalization", "showFormalization", "checkFormalization", "formalizationAction",
  "openFormalizationArtifact", "proveFormalization", "launchFormalization", "resumeFormalization",
  "listFormalizationConflicts", "openFormalizationConflictVersion", "resolveFormalizationConflict",
  "planFormalizationPreparation", "prepareFormalization", "cancelFormalizationPreparation",
  "listFormalizationPreparationRecovery", "recoverFormalizationPreparation", "resolveFormalizationPreparation", "cancelFormalizationRecovery",
]);

/** Open only the controller-resolved run project, retaining the owning VS Code host. */
async function draftProjectUri(root: string, snapshot: FormalizationSnapshot, listed: FormalizationArtifact, artifact: FormalizationArtifact): Promise<vscode.Uri> {
  const preview = snapshot.draft_preview;
  if (preview?.status !== "unaccepted_formalization" || preview.reason !== "review_ambiguities" ||
      preview.accepted !== false || preview.publication_authority !== false || preview.proof_authority !== false ||
      snapshot.can_prove || snapshot.handoff || !/^[A-Za-z0-9_-]{1,80}$/.test(preview.run_id) ||
      artifact.kind !== "project" || artifact.id !== listed.id || artifact.sha256 !== listed.sha256 ||
      !preview.artifacts.some(row => row.id === listed.id && row.kind === "project" && row.sha256 === listed.sha256)) {
    throw new Error("This draft project is not bound to the selected unaccepted revision.");
  }
  const relative = `.leanflow/native-workflows/${preview.run_id}/project`;
  const expected = path.join(root, relative);
  if (listed.path !== relative || artifact.path !== expected || (await realpath(expected)) !== expected) {
    throw new Error("The controller returned an unexpected draft project location.");
  }
  const stat = await lstat(expected);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error("The draft project is not a regular run-owned folder.");
  for (const folder of vscode.workspace.workspaceFolders ?? []) {
    if (await realpath(folder.uri.fsPath).catch(() => "") === root) return folder.uri.with({ path: vscode.Uri.file(expected).path });
  }
  if (vscode.env.remoteName) throw new Error("Open this draft from its owning VS Code workspace.");
  return vscode.Uri.file(expected);
}

export class FormalizationHost {
  private queue: Promise<void> = Promise.resolve();
  private readonly openedSources = new Map<string, { documentId: string; revisionId: string; artifactId: string; formalizationWorkspace: FormalizationWorkspaceScope }>();
  private readonly selectionListener: vscode.Disposable | undefined;
  private readonly workspaceListener: vscode.Disposable | undefined;
  private readonly preparation: FormalizationPreparationHost;
  private readonly recovery: FormalizationRecoveryHost;
  private readonly setup: FormalizationProjectSetupHost;
  private readonly frozenDocuments = new FrozenArtifactDocuments();

  constructor(
    private readonly services: LeanFlowServices,
    private readonly post: (message: HostMessage) => void,
    private readonly launch: (request: LaunchRequest, operation: FormalizationOperation) => Promise<void>,
  ) {
    this.preparation = new FormalizationPreparationHost();
    this.recovery = new FormalizationRecoveryHost();
    this.setup = new FormalizationProjectSetupHost(services, post);
    this.workspaceListener = services.onDidChange?.(() => {
      const scope = services.formalizations.workspaceScope();
      this.preparation.cancelOtherWorkspace(scope);
      this.recovery.cancelOtherWorkspace(scope);
    });
    this.selectionListener = vscode.window.onDidChangeTextEditorSelection?.(event => {
      const source = this.openedSources.get(event.textEditor.document.uri.toString());
      const selection = event.selections[0];
      if (source && selection && sameFormalizationScope(source.formalizationWorkspace, this.services.formalizations.workspaceScope())) this.post({ type: "formalizationSourceSelection", ...source,
        lineStart: selection.start.line + 1, lineEnd: selection.end.line + 1 });
    });
  }

  dispose(): void { this.frozenDocuments.dispose(); this.setup.dispose(); this.preparation.dispose(); this.recovery.dispose(); this.selectionListener?.dispose(); this.workspaceListener?.dispose(); this.openedSources.clear(); }

  /** Serialize requests within this webview; the controller also checks cross-client revisions. */
  async handle(message: WebviewMessage): Promise<boolean> {
    if (!MESSAGE_TYPES.has(message.type)) return false;
    const scope = this.services.formalizations.workspaceScope();
    const bootstrap = ["listFormalizations", "listFormalizationConflicts", "listFormalizationPreparationRecovery"].includes(message.type);
    if (!sameFormalizationScope(message.formalizationWorkspace, scope) && !(bootstrap && !message.formalizationWorkspace)) {
      this.post({ type: "notify", level: "warn", message: "The formalization workspace changed. Refresh before continuing." });
      return true;
    }
    if (message.type === "cancelFormalizationPreparation") { this.preparation.cancel(message, scope); return true; }
    if (message.type === "cancelFormalizationRecovery") { this.recovery.cancel(scope); return true; }
    if (message.type === "formalizationProjectSetup") { await this.setup.handle(message, scope); return true; }
    // Currentness may inspect a large Mathlib environment. Keep source navigation and cancellation responsive.
    if (message.type === "checkFormalization") { await this.dispatch(message, scope); return true; }
    const request = this.queue.then(() => this.dispatch(message, scope));
    this.queue = request.catch(() => undefined);
    await request;
    return true;
  }

  private async dispatch(message: WebviewMessage, scope: FormalizationWorkspaceScope): Promise<void> {
    let post = (response: HostMessage) => {
      if (sameFormalizationScope(scope, this.services.formalizations.workspaceScope())) this.post({ ...response, formalizationWorkspace: scope, formalizationRequest: message.formalizationRequest });
    };
    let service = this.services.formalizations;
    try {
      service.assertWorkspace(scope);
      const root = await formalizationWorkspaceRoot(scope.root);
      service.assertWorkspace(scope);
      const operation = formalizationOperation(this.services, scope, root, this.post, message.formalizationRequest);
      service = operation.services.formalizations;
      post = operation.post;
      switch (message.type) {
        case "listFormalizationPreparationRecovery":
        case "recoverFormalizationPreparation":
        case "resolveFormalizationPreparation":
          await this.recovery.handle(message, operation);
          return;
        case "planFormalizationPreparation":
        case "prepareFormalization":
          await this.preparation.handle(message, operation);
          return;
        case "listFormalizationConflicts":
          post({ type: "formalizationConflicts", snapshot: await service.conflicts(), error: "" });
          return;
        case "openFormalizationConflictVersion": {
          const artifact = await service.conflictArtifact(message.transactionId, message.observationId, message.versionId);
          const bytes = await readFormalizationArtifact(artifact);
          operation.assertCurrent();
          if (artifact.kind === "pdf" || artifact.path.toLowerCase().endsWith(".pdf")) {
            post({ type: "formalizationConflictPdf", transactionId: message.transactionId, observationId: message.observationId, versionId: message.versionId,
              pdf: { documentId: message.transactionId, revisionId: message.observationId, artifactId: message.versionId, pageIndex: 0, base64: bytes.toString("base64") } });
            return;
          }
          if (bytes.includes(0)) throw new Error("This preserved version requires local binary inspection on the owning host.");
          const uri = this.frozenDocuments.uriFor({ scope, documentId: message.transactionId, revisionId: message.observationId, artifactId: message.versionId, name: path.basename(artifact.path) }, bytes);
          const document = await vscode.languages.setTextDocumentLanguage(await vscode.workspace.openTextDocument(uri), artifact.kind === "lean" || artifact.path.endsWith(".lean") ? "lean4" : artifact.path.endsWith(".md") ? "markdown" : "plaintext");
          operation.assertCurrent();
          this.services.formalizations.registerPreview(document.uri.toString(), scope);
          this.openedSources.delete(document.uri.toString());
          await vscode.window.showTextDocument(document, { viewColumn: vscode.ViewColumn.Beside, preview: true });
          return;
        }
        case "resolveFormalizationConflict":
          await service.resolveConflict(message.transactionId, message.observationId, message.reason);
          post({ type: "formalizationConflicts", snapshot: await service.conflicts(), error: "", resolvedTransactionId: message.transactionId });
          post({ type: "notify", level: "info", message: "Current workspace files retained. Available versions and any unavailable publication history were recorded with the resolution. Revalidate the mathematical translation before proving." });
          return;
        case "listFormalizations":
          post({ type: "formalizationList", documents: await service.list(), error: "" });
          return;
        case "showFormalization": {
          const snapshot = await service.show(message.documentId);
          post({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
          return;
        }
        case "checkFormalization": {
          const snapshot = await service.check(message.documentId, message.revisionId);
          post({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
          return;
        }
        case "inspectFormalization": {
          const picked = await vscode.window.showOpenDialog({
            canSelectMany: message.sourceKind === "file", canSelectFolders: message.sourceKind === "folder", canSelectFiles: message.sourceKind === "file",
            filters: message.sourceKind === "file" ? { "Source documents": ["tex", "pdf"] } : undefined,
            defaultUri: vscode.Uri.file(root), openLabel: "Inspect source coverage",
            title: message.sourceKind === "file" ? "Select TeX or PDF, or one TeX file with its companion PDF" : "Select a TeX project",
          });
          operation.assertCurrent();
          if (!picked?.[0]) return;
          if (picked.length > 2 || message.sourceKind === "folder" && picked.length !== 1) throw new Error("Choose one TeX file and optionally one companion PDF, or one PDF source.");
          const selected = await Promise.all(picked.map(async entry => {
            const source = await resolveExistingProjectPath(root, entry.fsPath);
            if (!source) throw new Error("Choose source material inside the active LeanFlow project.");
            return source;
          }));
          const tex = selected.filter(source => path.extname(source).toLowerCase() === ".tex");
          const pdf = selected.filter(source => path.extname(source).toLowerCase() === ".pdf");
          if (selected.length === 2 && (tex.length !== 1 || pdf.length !== 1)) throw new Error("A source pair must contain exactly one TeX file and one companion PDF.");
          const source = selected.length === 2 ? tex[0] : selected[0];
          const companion = selected.length === 2 ? path.relative(root, pdf[0]) : undefined;
          operation.assertCurrent();
          const snapshot = await service.inspect(path.relative(root, source) || ".", companion);
          post({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
          return;
        }
        case "formalizationAction": {
          const committed = await service.action(message.documentId, message.revisionId, message.action);
          post({ type: "formalizationActionCommitted", documentId: committed.document_id, previousRevisionId: committed.previous_revision_id, revisionId: committed.revision_id });
          try {
            const snapshot = await service.show(message.documentId);
            post({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
          } catch (error) {
            const reason = redactSensitiveText(error instanceof Error ? error.message : String(error));
            post({ type: "formalizationState", documentId: message.documentId, snapshot: null, error: `Saved revision ${committed.revision_id}. View refresh failed: ${reason}. Refresh the inventory; the edit is already committed.` });
          }
          return;
        }
        case "openFormalizationArtifact": {
          const snapshot = message.artifactId.startsWith("proof-lean-")
            ? await service.check(message.documentId, message.revisionId)
            : await service.show(message.documentId);
          if (snapshot.revision_id !== message.revisionId) throw new Error("The source revision changed. Refresh before opening this mapping.");
          const listedArtifact = snapshot.artifacts.find(artifact => artifact.id === message.artifactId);
          if (!listedArtifact) throw new Error("This artifact is not part of the selected revision.");
          if (message.itemId) {
            const item = snapshot.items.find(item => item.id === message.itemId);
            const correctionRefs = item?.corrections?.flatMap(row => [
              ...row.original.source_refs, ...row.source_context_refs, ...row.adopted.lean_refs,
              ...row.evidence.flatMap(fact => [fact.goal, fact.proof]),
            ]) ?? [];
            if (!item || ![...item.source_refs, ...item.lean_refs, ...correctionRefs].some(ref => ref.artifact_id === message.artifactId)) {
              throw new Error("This artifact does not belong to the selected source item.");
            }
          }
          const artifact = await service.artifact(message.documentId, message.revisionId, message.artifactId);
          if (listedArtifact.kind === "project") {
            const uri = await draftProjectUri(root, snapshot, listedArtifact, artifact);
            operation.assertCurrent();
            await vscode.commands.executeCommand("vscode.openFolder", uri, { forceNewWindow: true });
            return;
          }
          const bytes = await readFormalizationArtifact(artifact);
          operation.assertCurrent();
          if (artifact.kind === "pdf" || artifact.path.toLowerCase().endsWith(".pdf")) {
            post({ type: "formalizationPdf", pdf: {
              documentId: message.documentId, revisionId: message.revisionId, artifactId: message.artifactId,
              pageIndex: message.pageIndex ?? 0, base64: bytes.toString("base64"),
            } });
            return;
          }
          if (bytes.includes(0)) throw new Error("This artifact cannot be displayed as source text.");
          const uri = this.frozenDocuments.uriFor({ scope, documentId: message.documentId, revisionId: message.revisionId, artifactId: message.artifactId, name: path.basename(listedArtifact.path) }, bytes);
          const document = await vscode.languages.setTextDocumentLanguage(await vscode.workspace.openTextDocument(uri), listedArtifact.path.endsWith(".lean") ? "lean4" : listedArtifact.path.endsWith(".tex") ? "latex" : listedArtifact.path.endsWith(".md") ? "markdown" : "plaintext");
          operation.assertCurrent();
          this.services.formalizations.registerPreview(document.uri.toString(), scope);
          this.openedSources.delete(document.uri.toString());
          if (artifact.kind !== "lean" && !message.artifactId.startsWith("draft-")) {
            this.openedSources.set(document.uri.toString(), { documentId: message.documentId, revisionId: message.revisionId, artifactId: message.artifactId, formalizationWorkspace: scope });
          }
          const line = Math.min(Math.max(0, (message.line ?? 1) - 1), Math.max(0, document.lineCount - 1));
          operation.assertCurrent();
          await vscode.window.showTextDocument(document, {
            viewColumn: vscode.ViewColumn.Beside, preview: true, selection: new vscode.Range(line, 0, line, 0),
          });
          return;
        }
        case "launchFormalization":
        case "resumeFormalization": {
          const snapshot = await service.show(message.documentId);
          if (snapshot.revision_id !== message.revisionId || snapshot.selection_conflict || snapshot.source_stale) {
            throw new Error("The selected source revision changed or has a conflict. Refresh the inventory before launching.");
          }
          const setupService = this.services.setup;
          let setup = setupService?.stateForRoot(root);
          if (setup?.status === "running") throw new Error("Project setup is still running. Wait for project tools before starting or resuming formalization.");
          if (setup?.status !== "ready" && setupService) {
            await setupService.refresh(this.services.cli.ok, root,
              () => sameFormalizationScope(scope, this.services.formalizations.workspaceScope()));
            operation.assertCurrent();
            setup = setupService.stateForRoot(root);
          }
          if (setup?.status === "running") throw new Error("Project setup is still running. Wait for project tools before starting or resuming formalization.");
          if (setup?.status !== "ready") {
            // Historical show deliberately carries no preparation authority. A
            // checked document may still provide stronger current setup evidence.
            const checked = await service.check(message.documentId, message.revisionId);
            if (checked.selection_conflict || checked.source_stale) throw new Error("The selected source revision changed or has a conflict. Refresh the inventory before launching.");
            if (checked.preparation?.current !== true) {
              throw new Error("Set up project tools in the Formalization tab before starting or resuming native planning.");
            }
          }
          const source = await resolveExistingProjectPath(root, snapshot.source.path);
          if (!source) throw new Error("The source is no longer available inside this workspace.");
          if (message.request.kind !== "formalize") throw new Error("Use the Formalization tab to configure this source.");
          let request = { ...message.request, target: snapshot.source.path };
          if (message.type === "resumeFormalization") {
            request = await this.services.runs.bindFormalizationResumeRequest(message.trackedRunId, request, root, snapshot.document_id);
            operation.assertCurrent();
          }
          if (request.sourceDraftOnly && (snapshot.can_prove || ["published", "accepted_staged"].includes(snapshot.status))) {
            throw new Error("An accepted revision cannot become an unaccepted source draft. Select a source revision first.");
          }
          operation.assertCurrent();
          await this.launch({ ...request, kind: "formalize", target: snapshot.source.path,
            formalization: undefined, research: false, researchWorkers: null, agents: 1,
            noParallel: false, cleanRoom: false, humanReview: false }, {
            ...operation, sourceDocumentId: snapshot.document_id,
            resumeTrackedId: message.type === "resumeFormalization" ? message.trackedRunId : undefined,
          });
          return;
        }
        case "proveFormalization": {
          const snapshot = await service.check(message.documentId, message.revisionId);
          if (snapshot.revision_id !== message.revisionId || !snapshot.can_prove ||
              snapshot.handoff?.revision_id !== message.revisionId) {
            throw new Error("This revision is not ready for proving. Refresh and resolve the controller's blockers.");
          }
          if (!snapshot.handoff.obligations.length) throw new Error("The controller returned an inconsistent empty prover handoff. Refresh before proving.");
          operation.assertCurrent();
          await this.launch({
            ...message.request, kind: "prove", target: "", prompt: "", additionalSkills: [], sourceDraftOnly: false,
            formalization: { documentId: message.documentId, revisionId: message.revisionId },
          }, operation);
          return;
        }
      }
    } catch (error) {
      const reason = redactSensitiveText(error instanceof Error ? error.message : String(error));
      if (message.type === "listFormalizations") {
        post({ type: "formalizationList", documents: [], error: reason });
      } else if (message.type === "showFormalization" || message.type === "checkFormalization" || message.type === "formalizationAction") {
        post({ type: "formalizationState", documentId: message.documentId, snapshot: null, error: reason });
      } else if (message.type === "listFormalizationConflicts" || message.type === "openFormalizationConflictVersion" || message.type === "resolveFormalizationConflict") {
        post({ type: "formalizationConflicts", snapshot: null, error: reason });
      } else if (message.type === "planFormalizationPreparation" || message.type === "prepareFormalization") {
        post({ type: "formalizationPreparation", documentId: message.documentId, revisionId: message.revisionId, status: "failed", message: reason, error: reason });
      } else if (message.type === "listFormalizationPreparationRecovery" || message.type === "recoverFormalizationPreparation" || message.type === "resolveFormalizationPreparation") {
        post({ type: "formalizationPreparationRecovery", snapshot: null, error: reason, busy: false });
      } else {
        post({ type: "notify", level: "error", message: reason });
      }
    }
  }
}
