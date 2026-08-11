/** Select a project root from bounded nested-manifest discovery. */
import * as path from "node:path";

const IGNORED_PARENT_DIRECTORIES = new Set([
  ".git",
  ".lake",
  "archive",
  "archives",
  "build",
  "node_modules",
]);

/** Return whether a manifest belongs to an active-looking nested project. */
export function isNestedProjectManifestCandidate(
  workspaceRoot: string,
  manifestPath: string,
): boolean {
  const relative = path.relative(path.resolve(workspaceRoot), path.resolve(manifestPath));
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    return false;
  }
  const parts = relative.split(path.sep);
  return !parts.slice(0, -2).some((part) => IGNORED_PARENT_DIRECTORIES.has(part));
}

/** Return the project root only when discovery found one well-shaped manifest. */
export function projectRootForUniqueManifest(
  manifestPaths: readonly string[],
): string | null {
  if (manifestPaths.length !== 1) {
    return null;
  }
  const manifest = path.resolve(manifestPaths[0]);
  const stateDirectory = path.dirname(manifest);
  if (
    path.basename(manifest) !== "project.yaml" ||
    path.basename(stateDirectory) !== ".leanflow"
  ) {
    return null;
  }
  return path.dirname(stateDirectory);
}
