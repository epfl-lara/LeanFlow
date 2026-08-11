/** Validate launch file inputs against the selected project's real boundary. */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { isInsidePath } from "./storageSecurity";

/** Return whether a skill reference asks the runtime to resolve a filesystem path. */
export function isPathLikeSkill(reference: string): boolean {
  const value = reference.trim();
  return (
    path.isAbsolute(value) ||
    /^[A-Za-z]:/.test(value) ||
    value.startsWith("~") ||
    value.startsWith(".") ||
    value.includes("/") ||
    value.includes("\\")
  );
}

function expandCandidate(projectRoot: string, raw: string): string {
  const value = raw.trim();
  if (value === "~") {
    return os.homedir();
  }
  if (value.startsWith("~/") || value.startsWith("~\\")) {
    return path.resolve(os.homedir(), value.slice(2));
  }
  return path.isAbsolute(value) ? path.resolve(value) : path.resolve(projectRoot, value);
}

async function containedRealPath(projectRoot: string, raw: string): Promise<string> {
  const [realRoot, realCandidate] = await Promise.all([
    fs.promises.realpath(projectRoot),
    fs.promises.realpath(expandCandidate(projectRoot, raw)),
  ]);
  if (!isInsidePath(realRoot, realCandidate)) {
    throw new Error(`Path escapes the selected LeanFlow project: ${raw}`);
  }
  return realCandidate;
}

async function requireProjectFile(
  projectRoot: string,
  raw: string,
  label: string,
): Promise<string> {
  let resolved: string;
  try {
    resolved = await containedRealPath(projectRoot, raw);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      throw new Error(`${label} does not exist in the selected project: ${raw}`);
    }
    throw error;
  }
  const stat = await fs.promises.stat(resolved);
  if (!stat.isFile()) {
    throw new Error(`${label} must be a regular file in the selected project: ${raw}`);
  }
  return resolved;
}

/**
 * Refuse file-backed launch inputs that escape the selected real project.
 *
 * Simple skill names are resolved later through LeanFlow's declared builtin,
 * user, and project skill catalogs. Anything path-like is an explicit file
 * authority request and must resolve to an in-project `SKILL.md` (or a
 * directory containing one) before the host spawns a child.
 */
export async function validateLaunchProjectInputs(
  projectRoot: string,
  target: string,
  additionalSkills: readonly string[],
): Promise<void> {
  if (target.trim()) {
    await requireProjectFile(projectRoot, target, "Workflow target");
  }

  for (const reference of additionalSkills) {
    if (!isPathLikeSkill(reference)) {
      continue;
    }
    let resolved: string;
    try {
      resolved = await containedRealPath(projectRoot, reference);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") {
        throw new Error(`Additional skill path does not exist in the project: ${reference}`);
      }
      throw error;
    }
    const stat = await fs.promises.stat(resolved);
    const skillFile = stat.isDirectory() ? path.join(resolved, "SKILL.md") : resolved;
    if (!stat.isDirectory() && path.basename(resolved) !== "SKILL.md") {
      throw new Error(`Additional skill file must be named SKILL.md: ${reference}`);
    }
    await requireProjectFile(projectRoot, skillFile, "Additional skill");
  }
}
