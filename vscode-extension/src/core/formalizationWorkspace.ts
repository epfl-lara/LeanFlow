/** Select an unambiguous host source workspace before a LeanFlow manifest exists. */
import * as fs from "node:fs/promises";
import { realpathSync } from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import { discoverProject } from "./project";
import { isHostFileUri } from "./workspaceUris";

export async function formalizationWorkspaceRoot(projectRoot: string): Promise<string> {
  const candidate = projectRoot || sourceWorkspaceRoot();
  if (!candidate) throw new Error("Open a source folder, or select an editor in the intended workspace folder, before preparing formalization.");
  const root = await fs.realpath(candidate);
  if (!(await fs.stat(root)).isDirectory()) throw new Error("The selected source workspace is not a directory on the owning host.");
  return root;
}

/** Resolve aliases synchronously so a changed editor owner is visible before discovery awaits. */
function canonicalRoot(root: string): string {
  try { return realpathSync(root); }
  catch { return path.resolve(root); }
}

/** Test ancestry using path components rather than a spelling prefix. */
function contains(parent: string, child: string): boolean {
  const relative = path.relative(parent, child);
  return relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}

/** Keep external dependency/reference editors from replacing an open workspace owner. */
function externalReference(active: vscode.Uri | undefined): boolean {
  if (!active || !isHostFileUri(active) || vscode.workspace.getWorkspaceFolder(active) ||
      !(vscode.workspace.workspaceFolders ?? []).some(folder => isHostFileUri(folder.uri)) ||
      /\.(tex|ltx|latex|pdf)$/i.test(active.fsPath)) return false;
  const project = discoverProject().root;
  return !project || !contains(canonicalRoot(project), canonicalRoot(active.fsPath));
}

/** Observe the owning folder synchronously so picker/CLI waits cannot hide a switch. */
export function sourceWorkspaceRoot(): string {
  const active = vscode.window.activeTextEditor?.document.uri;
  const activeFolder = active && isHostFileUri(active) ? vscode.workspace.getWorkspaceFolder(active) : undefined;
  const folders = (vscode.workspace.workspaceFolders ?? []).filter(folder => isHostFileUri(folder.uri));
  const selected = activeFolder ?? (folders.length === 1 ? folders[0] : undefined);
  return selected?.uri.fsPath ?? "";
}

/** Notice another owning folder before asynchronous Lean project discovery completes. */
export function formalizationScopeRoot(projectRoot: string): string {
  const folder = sourceWorkspaceRoot();
  const project = projectRoot ? canonicalRoot(projectRoot) : "";
  const source = folder ? canonicalRoot(folder) : "";
  if (!project || !source) return project || source;
  return contains(source, project) || contains(project, source) ? project : source;
}

/** Retain host selection through dashboard focus without masking a real editor or explicit switch. */
export class FormalizationWorkspaceSelection {
  private selectedRoot = "";
  private invalidated = false;
  private retainSelection = false;

  /** Forget host selection after an explicit root or workspace invalidation. */
  invalidate(): void { this.selectedRoot = ""; this.invalidated = true; this.retainSelection = false; }

  /** Remember a successfully prepared host choice when no file editor selects another owner. */
  select(root: string): void { this.selectedRoot = canonicalRoot(root); this.retainSelection = true; this.invalidated = false; }

  /** Observe real editor switches immediately and retain selection only through non-file focus. */
  scopeRoot(projectRoot: string, ownedPreviewRoot = ""): string {
    if (this.invalidated) { projectRoot = discoverProject().root; this.invalidated = false; }
    const configured = vscode.workspace.getConfiguration("leanflow").get<string>("projectRoot", "").trim();
    const active = vscode.window.activeTextEditor?.document.uri;
    if (configured) { this.selectedRoot = canonicalRoot(configured); this.retainSelection = true; }
    else if (ownedPreviewRoot) { this.selectedRoot = canonicalRoot(ownedPreviewRoot); this.retainSelection = true; }
    else if (!active || !isHostFileUri(active) || externalReference(active)) {
      // Panel focus is not a request to replace source-only B with the only
      // configured Lean project A in a multi-root window.
      const project = projectRoot ? canonicalRoot(projectRoot) : "";
      if (project && (!this.selectedRoot || (!this.retainSelection && (contains(project, this.selectedRoot) || contains(this.selectedRoot, project))))) {
        this.selectedRoot = project; this.retainSelection = true;
      } else this.selectedRoot = this.selectedRoot || formalizationScopeRoot(projectRoot);
    } else {
      // Two projects can share one parent workspace folder. The actual file's
      // project must replace cached A before asynchronous discovery of B.
      const editorProject = discoverProject().root;
      const file = canonicalRoot(active.fsPath);
      const folder = vscode.workspace.getWorkspaceFolder(active)?.uri.fsPath;
      const matchingProject = projectRoot && contains(canonicalRoot(projectRoot), file) ? projectRoot : "";
      this.selectedRoot = editorProject && contains(editorProject, file) ? editorProject
        : folder ? formalizationScopeRoot(matchingProject) : path.dirname(file);
      this.retainSelection = true;
    }
    return this.selectedRoot;
  }

  /** Preserve an observed owner through rediscovery without suppressing initial nested discovery. */
  discoveryRoot(ownedPreviewRoot = ""): string {
    const active = vscode.window.activeTextEditor?.document.uri;
    return ownedPreviewRoot || ((!active || !isHostFileUri(active) || externalReference(active)) && this.retainSelection ? this.selectedRoot : "");
  }
}
