/** Inspect managed storage and request safe cleanup on the extension host. */
import * as vscode from "vscode";
import { runCli } from "./cli";

import { normalizeStorage, type StorageState } from "./storageState";

export class StorageService implements vscode.Disposable {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChange = this.changed.event;
  private snapshot: StorageState | null = null;
  private pending: Promise<void> | null = null;
  private action: "status" | "clean" | null = null;

  dispose(): void { this.changed.dispose(); }
  state(): StorageState | null { return this.snapshot; }

  /** Read storage without cleaning or modifying a project. */
  refresh(): Promise<void> { return this.request("status"); }

  /** Collect only owned, unused caches; the CLI protects live owners and saved work. */
  clean(): Promise<void> {
    if (!vscode.workspace.isTrusted) throw new Error("Trust this workspace before cleaning managed storage.");
    return this.request("clean");
  }

  private request(action: "status" | "clean"): Promise<void> {
    if (this.pending) {
      if (action === "clean" && this.action !== "clean") return this.pending.then(() => this.request("clean"));
      return this.pending;
    }
    this.action = action;
    if (this.snapshot) this.snapshot = { ...this.snapshot, checking: true, error: "" };
    this.changed.fire();
    this.pending = (async () => {
      try {
        const raw: unknown = JSON.parse(await runCli(["storage", action, "--json"], { timeoutMs: 120_000 }));
        this.snapshot = { ...normalizeStorage(raw), checking: false, error: "" };
      } catch {
        this.snapshot = this.snapshot ? { ...this.snapshot, checking: false, error: "Storage is unavailable. Update LeanFlow or check the CLI path, then refresh." } : null;
        if (action === "clean") throw new Error("Could not clean managed storage. Check the LeanFlow CLI and refresh.");
      } finally {
        this.pending = null;
        this.action = null;
        this.changed.fire();
      }
    })();
    return this.pending;
  }
}
