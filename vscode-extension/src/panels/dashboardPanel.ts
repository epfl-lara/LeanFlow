/** The full-width dashboard, opened in an editor column. */
import * as vscode from "vscode";

import type { LeanFlowServices } from "../core/services";
import { buildWebviewHtml, WebviewHost } from "./webviewHost";

export class DashboardPanel {
  private static current: DashboardPanel | undefined;

  private readonly disposables: vscode.Disposable[] = [];
  private readonly host: WebviewHost;

  private constructor(
    private readonly panel: vscode.WebviewPanel,
    extensionUri: vscode.Uri,
    services: LeanFlowServices,
  ) {
    this.panel.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(extensionUri, "dist")],
    };
    this.panel.webview.html = buildWebviewHtml(this.panel.webview, extensionUri, "panel");
    this.host = new WebviewHost(this.panel.webview, services);
    this.disposables.push(this.host, this.panel.onDidDispose(() => this.dispose()));
  }

  static show(extensionUri: vscode.Uri, services: LeanFlowServices): void {
    const column = vscode.window.activeTextEditor?.viewColumn ?? vscode.ViewColumn.One;
    if (DashboardPanel.current) {
      DashboardPanel.current.panel.reveal(column);
      return;
    }
    const panel = vscode.window.createWebviewPanel(
      "leanflow.dashboard",
      "LeanFlow",
      column,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, "dist")],
      },
    );
    // Tab icons are rendered as plain images, not theme-colored masks, so the
    // currentColor-based leanflow.svg would come out black on dark themes.
    panel.iconPath = {
      light: vscode.Uri.joinPath(extensionUri, "media", "leanflow-light.svg"),
      dark: vscode.Uri.joinPath(extensionUri, "media", "leanflow-dark.svg"),
    };
    DashboardPanel.current = new DashboardPanel(panel, extensionUri, services);
  }

  dispose(): void {
    DashboardPanel.current = undefined;
    for (const disposable of this.disposables) {
      disposable.dispose();
    }
    this.panel.dispose();
  }
}
