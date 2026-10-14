/** Route formalization UI actions through the owning host's controller and verified artifacts. */
import * as path from "node:path";
import * as vscode from "vscode";
import { readFormalizationArtifact } from "../core/formalizationArtifact";
import { formalizationWorkspaceRoot } from "../core/formalizationWorkspace";
import { redactSensitiveText } from "../core/runPrivacy";
import type { LeanFlowServices } from "../core/services";
import { resolveExistingProjectPath } from "../core/storageSecurity";
import type { HostMessage, LaunchRequest, WebviewMessage } from "../core/types";
import { FormalizationPreparationHost } from "./formalizationPreparationHost";
import { FormalizationRecoveryHost } from "./formalizationRecoveryHost";

const MESSAGE_TYPES = new Set([
  "listFormalizations", "inspectFormalization", "showFormalization", "formalizationAction",
  "openFormalizationArtifact", "proveFormalization",
  "listFormalizationConflicts", "openFormalizationConflictVersion", "resolveFormalizationConflict",
  "planFormalizationPreparation", "prepareFormalization", "cancelFormalizationPreparation",
  "listFormalizationPreparationRecovery", "recoverFormalizationPreparation", "resolveFormalizationPreparation", "cancelFormalizationRecovery",
]);

export class FormalizationHost {
  private queue: Promise<void> = Promise.resolve();
  private readonly openedSources = new Map<string, { documentId: string; revisionId: string; artifactId: string }>();
  private readonly selectionListener: vscode.Disposable | undefined;
  private readonly preparation: FormalizationPreparationHost;
  private readonly recovery: FormalizationRecoveryHost;

  constructor(
    private readonly services: LeanFlowServices,
    private readonly post: (message: HostMessage) => void,
    private readonly launch: (request: LaunchRequest) => Promise<void>,
  ) {
    this.preparation = new FormalizationPreparationHost(services, post);
    this.recovery = new FormalizationRecoveryHost(services, post);
    this.selectionListener = vscode.window.onDidChangeTextEditorSelection?.(event => {
      const source = this.openedSources.get(event.textEditor.document.uri.toString());
      const selection = event.selections[0];
      if (source && selection) this.post({ type: "formalizationSourceSelection", ...source,
        lineStart: selection.start.line + 1, lineEnd: selection.end.line + 1 });
    });
  }

  dispose(): void { this.preparation.dispose(); this.recovery.dispose(); this.selectionListener?.dispose(); this.openedSources.clear(); }

  /** Serialize requests within this webview; the controller also checks cross-client revisions. */
  async handle(message: WebviewMessage): Promise<boolean> {
    if (!MESSAGE_TYPES.has(message.type)) return false;
    if (message.type === "cancelFormalizationPreparation") { this.preparation.cancel(message); return true; }
    if (message.type === "cancelFormalizationRecovery") { this.recovery.cancel(); return true; }
    const request = this.queue.then(() => this.dispatch(message));
    this.queue = request.catch(() => undefined);
    await request;
    return true;
  }

  private async dispatch(message: WebviewMessage): Promise<void> {
    const service = this.services.formalizations;
    try {
      if (!this.services.project.root) service.bindSourceRoot(await formalizationWorkspaceRoot(""));
      switch (message.type) {
        case "listFormalizationPreparationRecovery":
        case "recoverFormalizationPreparation":
        case "resolveFormalizationPreparation":
          await this.recovery.handle(message);
          return;
        case "planFormalizationPreparation":
        case "prepareFormalization":
          await this.preparation.handle(message);
          return;
        case "listFormalizationConflicts":
          this.post({ type: "formalizationConflicts", snapshot: await service.conflicts(), error: "" });
          return;
        case "openFormalizationConflictVersion": {
          const artifact = await service.conflictArtifact(message.transactionId, message.observationId, message.versionId);
          const bytes = await readFormalizationArtifact(artifact);
          if (artifact.kind === "pdf" || artifact.path.toLowerCase().endsWith(".pdf")) {
            this.post({ type: "formalizationConflictPdf", transactionId: message.transactionId, observationId: message.observationId, versionId: message.versionId,
              pdf: { documentId: message.transactionId, revisionId: message.observationId, artifactId: message.versionId, pageIndex: 0, base64: bytes.toString("base64") } });
            return;
          }
          if (bytes.includes(0)) throw new Error("This preserved version requires local binary inspection on the owning host.");
          const document = await vscode.workspace.openTextDocument({ content: bytes.toString("utf8"), language: artifact.kind === "lean" || artifact.path.endsWith(".lean") ? "lean4" : "plaintext" });
          await vscode.window.showTextDocument(document, { viewColumn: vscode.ViewColumn.Beside, preview: true });
          return;
        }
        case "resolveFormalizationConflict":
          await service.resolveConflict(message.transactionId, message.observationId, message.reason);
          this.post({ type: "formalizationConflicts", snapshot: await service.conflicts(), error: "", resolvedTransactionId: message.transactionId });
          this.post({ type: "notify", level: "info", message: "Current workspace files retained. Available versions and any unavailable publication history were recorded with the resolution. Revalidate the mathematical translation before proving." });
          return;
        case "listFormalizations":
          this.post({ type: "formalizationList", documents: await service.list(), error: "" });
          return;
        case "showFormalization": {
          const snapshot = await service.show(message.documentId);
          this.post({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
          return;
        }
        case "inspectFormalization": {
          const root = this.services.project.root || service.workspaceRoot();
          const picked = await vscode.window.showOpenDialog({
            canSelectMany: message.sourceKind === "file", canSelectFolders: message.sourceKind === "folder", canSelectFiles: message.sourceKind === "file",
            filters: message.sourceKind === "file" ? { "Source documents": ["tex", "pdf"] } : undefined,
            defaultUri: vscode.Uri.file(root), openLabel: "Inspect source coverage",
            title: message.sourceKind === "file" ? "Select TeX or PDF, or one TeX file with its companion PDF" : "Select a TeX project",
          });
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
          const snapshot = await service.inspect(path.relative(root, source) || ".", companion);
          this.post({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
          return;
        }
        case "formalizationAction": {
          const snapshot = await service.action(message.documentId, message.revisionId, message.action);
          this.post({ type: "formalizationState", documentId: snapshot.document_id, snapshot, error: "" });
          return;
        }
        case "openFormalizationArtifact": {
          const snapshot = await service.show(message.documentId);
          if (snapshot.revision_id !== message.revisionId) throw new Error("The source revision changed. Refresh before opening this mapping.");
          if (!snapshot.artifacts.some(artifact => artifact.id === message.artifactId)) throw new Error("This artifact is not part of the selected revision.");
          if (message.itemId) {
            const item = snapshot.items.find(item => item.id === message.itemId);
            if (!item || ![...item.source_refs, ...item.lean_refs].some(ref => ref.artifact_id === message.artifactId)) {
              throw new Error("This artifact does not belong to the selected source item.");
            }
          }
          const artifact = await service.artifact(message.documentId, message.revisionId, message.artifactId);
          const bytes = await readFormalizationArtifact(artifact);
          if (artifact.kind === "pdf" || artifact.path.toLowerCase().endsWith(".pdf")) {
            this.post({ type: "formalizationPdf", pdf: {
              documentId: message.documentId, revisionId: message.revisionId, artifactId: message.artifactId,
              pageIndex: message.pageIndex ?? 0, base64: bytes.toString("base64"),
            } });
            return;
          }
          if (bytes.includes(0)) throw new Error("This artifact cannot be displayed as source text.");
          // A content document shows the exact frozen bytes on local and remote hosts.
          // Opening it never grants the webview a path or a writable canonical source file.
          const document = await vscode.workspace.openTextDocument({
            content: bytes.toString("utf8"),
            language: artifact.path.endsWith(".lean") ? "lean4" : artifact.path.endsWith(".tex") ? "latex" : "plaintext",
          });
          this.openedSources.set(document.uri.toString(), { documentId: message.documentId, revisionId: message.revisionId, artifactId: message.artifactId });
          const line = Math.min(Math.max(0, (message.line ?? 1) - 1), Math.max(0, document.lineCount - 1));
          await vscode.window.showTextDocument(document, {
            viewColumn: vscode.ViewColumn.Beside, preview: true, selection: new vscode.Range(line, 0, line, 0),
          });
          return;
        }
        case "proveFormalization": {
          const snapshot = await service.show(message.documentId);
          if (snapshot.revision_id !== message.revisionId || !snapshot.can_prove ||
              snapshot.handoff?.revision_id !== message.revisionId) {
            throw new Error("This revision is not ready for proving. Refresh and resolve the controller's blockers.");
          }
          if (!snapshot.handoff.obligations.length) {
            this.post({ type: "notify", level: "info", message: "No proof obligations remain in this accepted formalization. No model was launched." });
            return;
          }
          await this.launch({
            ...message.request, kind: "prove", target: "", prompt: "", additionalSkills: [],
            formalization: { documentId: message.documentId, revisionId: message.revisionId },
          });
          return;
        }
      }
    } catch (error) {
      const reason = redactSensitiveText(error instanceof Error ? error.message : String(error));
      if (message.type === "listFormalizations") {
        this.post({ type: "formalizationList", documents: [], error: reason });
      } else if (message.type === "showFormalization" || message.type === "formalizationAction") {
        this.post({ type: "formalizationState", documentId: message.documentId, snapshot: null, error: reason });
      } else if (message.type === "listFormalizationConflicts" || message.type === "openFormalizationConflictVersion" || message.type === "resolveFormalizationConflict") {
        this.post({ type: "formalizationConflicts", snapshot: null, error: reason });
      } else if (message.type === "planFormalizationPreparation" || message.type === "prepareFormalization") {
        this.post({ type: "formalizationPreparation", documentId: message.documentId, revisionId: message.revisionId, status: "failed", message: reason, error: reason });
      } else if (message.type === "listFormalizationPreparationRecovery" || message.type === "recoverFormalizationPreparation" || message.type === "resolveFormalizationPreparation") {
        this.post({ type: "formalizationPreparationRecovery", snapshot: null, error: reason, busy: false });
      } else {
        this.post({ type: "notify", level: "error", message: reason });
      }
    }
  }
}
