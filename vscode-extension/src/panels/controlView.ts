/** The compact sidebar control: launch a run and watch the live one. */
import * as vscode from "vscode";

import type { LeanFlowServices } from "../core/services";
import { buildWebviewHtml, WebviewHost } from "./webviewHost";

export class ControlViewProvider implements vscode.WebviewViewProvider {
  static readonly viewType = "leanflow.control";

  private host: WebviewHost | undefined;

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly services: LeanFlowServices,
  ) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, "dist")],
    };
    view.webview.html = buildWebviewHtml(view.webview, this.extensionUri, "sidebar");
    this.host?.dispose();
    this.host = new WebviewHost(view.webview, this.services);
    view.onDidDispose(() => {
      this.host?.dispose();
      this.host = undefined;
    });
  }
}
