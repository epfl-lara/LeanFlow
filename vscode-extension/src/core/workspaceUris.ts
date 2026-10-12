/** Map files on the extension host to editor URIs, including remote workspaces. */
import * as vscode from "vscode";

/** Accept local files or files served by this workspace's remote extension host. */
export function isHostFileUri(uri: vscode.Uri): boolean {
  if (uri.scheme === "file") return !vscode.env.remoteName;
  if (uri.scheme !== "vscode-remote" || !vscode.env.remoteName) return false;
  const remote = vscode.workspace.workspaceFolders?.find(folder => folder.uri.scheme === "vscode-remote")?.uri
    ?? vscode.window.activeTextEditor?.document.uri;
  return remote?.scheme === "vscode-remote" && uri.authority === remote.authority;
}

/** Preserve the remote authority when opening a path managed by the host process. */
export function hostFileUri(filePath: string): vscode.Uri {
  const base = vscode.workspace.workspaceFolders?.find(folder => isHostFileUri(folder.uri))?.uri
    ?? vscode.window.activeTextEditor?.document.uri;
  if (base?.scheme === "vscode-remote" && isHostFileUri(base)) {
    return base.with({ path: filePath.replace(/\\/g, "/"), query: "", fragment: "" });
  }
  return vscode.Uri.file(filePath);
}
