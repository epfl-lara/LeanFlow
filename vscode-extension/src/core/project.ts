/**
 * Discover the LeanFlow project the editor is working in.
 *
 * A project is a directory containing `.leanflow/project.yaml`, matching the
 * runtime's own discovery in leanflow_cli/workflows/workflow_state_paths.py.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

import {
  isNestedProjectManifestCandidate,
  projectRootForUniqueManifest,
} from "./projectDiscovery";
import { isHostFileUri } from "./workspaceUris";
import type { ProjectInfo } from "./types";

const MANIFEST = path.join(".leanflow", "project.yaml");

export const NO_PROJECT: ProjectInfo = { found: false, root: "", label: "", stateRoot: "" };

function ascendToProject(start: string): string | null {
  let current = path.resolve(start);
  // Walk to the filesystem root; `path.dirname` is its own fixed point there.
  for (;;) {
    if (fs.existsSync(path.join(current, MANIFEST))) {
      return current;
    }
    const parent = path.dirname(current);
    if (parent === current) {
      return null;
    }
    current = parent;
  }
}

function describe(root: string): ProjectInfo {
  // The CLI reports canonical roots; matching that spelling also keeps setup
  // admission and run ownership consistent when a workspace uses a symlink.
  try { root = fs.realpathSync(root); } catch { /* Discovery may race a folder move. */ }
  return {
    found: true,
    root,
    label: path.basename(root),
    stateRoot: path.join(root, ".leanflow", "workflow-state"),
  };
}

function configuredProjectRoot(): string {
  return vscode.workspace
    .getConfiguration("leanflow")
    .get<string>("projectRoot", "")
    .trim();
}

/**
 * Resolve the active project.
 *
 * Precedence is explicit setting, then the active editor's file (so a
 * multi-root workspace follows what the user is looking at), then the workspace
 * unique project among the workspace folders.
 */
export function discoverProject(): ProjectInfo {
  const configured = configuredProjectRoot();
  if (configured) {
    const resolved = path.resolve(configured);
    return fs.existsSync(path.join(resolved, MANIFEST)) ? describe(resolved) : NO_PROJECT;
  }

  const activeUri = vscode.window.activeTextEditor?.document.uri;
  if (activeUri && isHostFileUri(activeUri)) {
    const found = ascendToProject(path.dirname(activeUri.fsPath));
    if (found) {
      return describe(found);
    }
    if (vscode.workspace.getWorkspaceFolder(activeUri)) return NO_PROJECT;
  }

  const projects = new Set<string>();
  for (const folder of vscode.workspace.workspaceFolders ?? []) {
    if (!isHostFileUri(folder.uri)) {
      continue;
    }
    const found = ascendToProject(folder.uri.fsPath);
    if (found) {
      projects.add(found);
    }
  }
  return projects.size === 1 ? describe([...projects][0]) : NO_PROJECT;
}

/** Resolve one nested project when the workspace folder is its parent. */
export async function discoverProjectIncludingNested(preferredRoot = ""): Promise<ProjectInfo> {
  const immediate = discoverProject();
  if (immediate.found || configuredProjectRoot()) {
    return immediate;
  }

  // A setup choice is explicit, unlike workspace-folder ordering. Retain that
  // selection when several projects exist and no editor identifies one.
  if (preferredRoot && !vscode.window.activeTextEditor && fs.existsSync(path.join(preferredRoot, MANIFEST))) {
    return describe(preferredRoot);
  }

  // VS Code already indexes workspace files. Keep the query bounded so
  // a parent folder containing several projects fails closed instead of
  // crawling a large tree or silently choosing the first one.
  const active = vscode.window.activeTextEditor?.document.uri;
  const activeFolder = active && isHostFileUri(active) ? vscode.workspace.getWorkspaceFolder(active) : undefined;
  const manifests = await vscode.workspace.findFiles(
    activeFolder ? new vscode.RelativePattern(activeFolder, "**/.leanflow/project.yaml") : "**/.leanflow/project.yaml",
    "**/{.git,.lake,archive,archives,node_modules,build}/**",
    20,
  );
  const workspaceRoots = (vscode.workspace.workspaceFolders ?? [])
    .filter((folder) => isHostFileUri(folder.uri))
    .map((folder) => folder.uri.fsPath);
  const candidate = projectRootForUniqueManifest(
    manifests
      .filter(
        (uri) =>
          isHostFileUri(uri) &&
          workspaceRoots.some((root) =>
            isNestedProjectManifestCandidate(root, uri.fsPath),
          ),
      )
      .map((uri) => uri.fsPath),
  );
  if (candidate === null) {
    return NO_PROJECT;
  }

  try {
    const manifest = path.join(candidate, MANIFEST);
    const manifestStat = await fs.promises.lstat(manifest);
    if (!manifestStat.isFile() || manifestStat.isSymbolicLink()) {
      return NO_PROJECT;
    }
    const [realRoot, realManifest] = await Promise.all([
      fs.promises.realpath(candidate),
      fs.promises.realpath(manifest),
    ]);
    if (realManifest !== path.join(realRoot, MANIFEST)) {
      return NO_PROJECT;
    }
    return describe(realRoot);
  } catch {
    return NO_PROJECT;
  }
}

/**
 * List the project's Lean files as project-relative paths, for the target picker.
 *
 * Build output and LeanFlow's own state are excluded — the same directories the
 * runtime skips when it scans a project for targets.
 */
export async function listLeanFiles(project: ProjectInfo, limit = 2000): Promise<string[]> {
  if (!project.found) {
    return [];
  }
  const skip = new Set([".lake", ".git", ".leanflow", "build", "node_modules"]);
  const results: string[] = [];

  const walk = async (dir: string): Promise<void> => {
    if (results.length >= limit) {
      return;
    }
    let entries: fs.Dirent[];
    try {
      entries = await fs.promises.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (results.length >= limit) {
        return;
      }
      if (entry.isDirectory()) {
        if (!skip.has(entry.name)) {
          await walk(path.join(dir, entry.name));
        }
      } else if (entry.isFile() && entry.name.endsWith(".lean")) {
        results.push(path.relative(project.root, path.join(dir, entry.name)));
      }
    }
  };

  await walk(project.root);
  return results.sort((a, b) => a.localeCompare(b));
}

/** Project-relative path for a file, or "" when it lies outside the project. */
export function relativeTarget(project: ProjectInfo, fsPath: string): string {
  if (!project.found) {
    return "";
  }
  const relative = path.relative(project.root, fsPath);
  return relative.startsWith("..") || path.isAbsolute(relative) ? "" : relative;
}
