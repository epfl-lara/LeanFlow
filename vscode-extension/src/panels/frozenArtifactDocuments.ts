/** Retain verified artifact text in named read-only virtual documents with bounded ownership. */
import { createHash, randomBytes } from "node:crypto";
import * as vscode from "vscode";
import type { FormalizationWorkspaceScope } from "../core/formalizationScope";

export const MAX_FROZEN_TEXT_BYTES = 64 * 1024 * 1024;
export const MAX_FROZEN_DOCUMENTS = 32;

interface FrozenIdentity {
  scope: FormalizationWorkspaceScope;
  documentId: string;
  revisionId: string;
  artifactId: string;
  name: string;
}

export class FrozenArtifactDocuments implements vscode.TextDocumentContentProvider, vscode.Disposable {
  private readonly scheme = "leanflow-frozen-" + randomBytes(12).toString("hex");
  private readonly entries = new Map<string, { text: string; bytes: number }>();
  private registration: vscode.Disposable | undefined;
  private retainedBytes = 0;
  private disposed = false;

  /** Register immutable verified text; URI identity grants no filesystem lookup capability. */
  uriFor(identity: FrozenIdentity, bytes: Buffer): vscode.Uri {
    if (this.disposed) throw new Error("The frozen artifact viewer was closed. Reopen it from Formalization.");
    if (!identity.name || /[/\\\0]/.test(identity.name) || [".", ".."].includes(identity.name)) throw new Error("The frozen artifact has no portable display name.");
    if (bytes.length > MAX_FROZEN_TEXT_BYTES) throw new Error("This artifact exceeds the frozen text viewer's 64 MiB limit.");
    const text = bytes.toString("utf8");
    if (bytes.includes(0) || !Buffer.from(text, "utf8").equals(bytes)) throw new Error("This frozen artifact is not UTF-8 source text.");
    const sha256 = createHash("sha256").update(bytes).digest("hex");
    const key = createHash("sha256").update(JSON.stringify({ ...identity, sha256 })).digest("hex");
    const uri = vscode.Uri.from({ scheme: this.scheme, authority: "frozen", path: `/${key}/${identity.name}` });
    const address = uri.toString();
    this.registration ??= vscode.workspace.registerTextDocumentContentProvider(this.scheme, this);
    const previous = this.entries.get(address);
    if (previous) { this.entries.delete(address); this.entries.set(address, previous); return uri; }
    while (this.entries.size >= MAX_FROZEN_DOCUMENTS || this.retainedBytes + bytes.length > MAX_FROZEN_TEXT_BYTES) {
      const oldest = this.entries.keys().next().value!;
      this.retainedBytes -= this.entries.get(oldest)!.bytes;
      this.entries.delete(oldest);
    }
    this.entries.set(address, { text, bytes: bytes.length });
    this.retainedBytes += bytes.length;
    return uri;
  }

  /** Serve only exact retained virtual identities, without executing commands or reading paths. */
  provideTextDocumentContent(uri: vscode.Uri): string {
    const entry = !this.disposed && uri.scheme === this.scheme ? this.entries.get(uri.toString()) : undefined;
    if (!entry) throw new Error("This frozen artifact is no longer retained. Open it again from its current inventory.");
    return entry.text;
  }

  dispose(): void {
    this.disposed = true;
    this.registration?.dispose();
    this.registration = undefined;
    this.entries.clear();
    this.retainedBytes = 0;
  }
}
