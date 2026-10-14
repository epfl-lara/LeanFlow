/** Select an unambiguous host source workspace before a LeanFlow manifest exists. */
import * as fs from "node:fs/promises";
import * as vscode from "vscode";
import { isHostFileUri } from "./workspaceUris";

export async function formalizationWorkspaceRoot(projectRoot: string): Promise<string> {
  if (projectRoot) return projectRoot;
  const active = vscode.window.activeTextEditor?.document.uri;
  const activeFolder = active && isHostFileUri(active) ? vscode.workspace.getWorkspaceFolder(active) : undefined;
  const folders = (vscode.workspace.workspaceFolders ?? []).filter(folder => isHostFileUri(folder.uri));
  const selected = activeFolder ?? (folders.length === 1 ? folders[0] : undefined);
  if (!selected) throw new Error("Open a source folder, or select an editor in the intended workspace folder, before preparing formalization.");
  const root = await fs.realpath(selected.uri.fsPath);
  if (!(await fs.stat(root)).isDirectory()) throw new Error("The selected source workspace is not a directory on the owning host.");
  return root;
}
