/**
 * Create reproducible, non-destructive source clones for experiment cells.
 *
 * Research sweeps must not run sequentially in one mutable checkout: an early
 * proof would become a hidden input to every later condition. This module
 * captures one required-clean Git commit and gives each cell a detached local
 * clone of that exact commit under extension-owned storage. The user's checkout
 * is only read; it is never reset, cleaned, or removed.
 */
import { execFile } from "node:child_process";
import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { promisify } from "node:util";

import {
  canonicalExperimentTarget,
  normalizeExperimentMatrixTargets,
} from "./experimentMatrix";
import { secureStorageDirectory } from "./storageSecurity";
import type { ExperimentBaseline, ExperimentMatrix } from "./types";

const execFileAsync = promisify(execFile);
const GIT_TIMEOUT_MS = 120_000;
const MANIFEST = path.join(".leanflow", "project.yaml");

/** Return the extension-owned storage directory name for one matrix. */
export function experimentStorageKey(matrixId: string): string {
  return crypto.createHash("sha256").update(matrixId).digest("hex").slice(0, 16);
}

async function git(cwd: string, args: string[]): Promise<string> {
  const { stdout } = await execFileAsync("git", ["-C", cwd, ...args], {
    timeout: GIT_TIMEOUT_MS,
    maxBuffer: 16 * 1024 * 1024,
    env: { ...process.env },
  });
  return stdout.toString();
}

function containedRelative(parent: string, child: string): string {
  const relative = path.relative(parent, child);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error("The LeanFlow project must be contained in its Git repository.");
  }
  return relative || ".";
}

function unsafePortableRelativePath(value: string): boolean {
  const segments = value.replace(/\\/g, "/").split("/");
  return (
    !value ||
    value.startsWith("~") ||
    path.isAbsolute(value) ||
    /^[A-Za-z]:/.test(value) ||
    /^[\\/]{2}/.test(value) ||
    segments.includes("..")
  );
}

export interface ResolvedExperimentMatrixTargets {
  matrix: ExperimentMatrix;
  /** Filesystem-canonical path keyed by the pre-resolution lexical identity. */
  identityByTarget: Readonly<Record<string, string>>;
}

interface ExistingTargetIdentity {
  target: string;
  fileIdentity: string | null;
}

async function existingTargetIdentity(
  realProjectRoot: string,
  rawTarget: string,
): Promise<ExistingTargetIdentity> {
  const lexical = canonicalExperimentTarget(rawTarget);
  if (!lexical) {
    return { target: "", fileIdentity: null };
  }
  const candidate = path.join(realProjectRoot, lexical);
  const candidateStat = await fs.promises.lstat(candidate).catch(() => null);
  if (!candidateStat?.isFile() || candidateStat.isSymbolicLink()) {
    throw new Error(`Experiment target is absent or symlinked in the project: ${rawTarget}`);
  }
  const realTarget = await fs.promises.realpath(candidate);
  const relative = path.relative(realProjectRoot, realTarget);
  if (
    !relative ||
    relative === ".." ||
    relative.startsWith(`..${path.sep}`) ||
    path.isAbsolute(relative)
  ) {
    throw new Error(`Experiment target escapes the selected project: ${rawTarget}`);
  }
  const stat = await fs.promises.stat(realTarget, { bigint: true });
  if (!stat.isFile()) {
    throw new Error(`Experiment target must be a regular project file: ${rawTarget}`);
  }
  const target = canonicalExperimentTarget(relative.split(path.sep).join("/"));
  const fileIdentity = stat.ino !== 0n
    ? `inode:${String(stat.dev)}:${String(stat.ino)}`
    : `realpath:${realTarget}`;
  return { target, fileIdentity };
}

/** Resolve active target paths through the host filesystem and reject real-file aliases. */
export async function resolveExistingExperimentMatrixTargets(
  projectRoot: string,
  matrix: ExperimentMatrix,
): Promise<ResolvedExperimentMatrixTargets> {
  const normalized = normalizeExperimentMatrixTargets(matrix);
  const realProjectRoot = await fs.promises.realpath(projectRoot);
  const rawTargets = normalized.targets.length > 0
    ? normalized.targets
    : [normalized.base.target];
  const identities = await Promise.all(
    rawTargets.map((target) => existingTargetIdentity(realProjectRoot, target)),
  );
  const seenFiles = new Set<string>();
  const identityByTarget = Object.create(null) as Record<string, string>;
  for (let index = 0; index < identities.length; index += 1) {
    const resolved = identities[index];
    if (resolved.fileIdentity !== null) {
      if (seenFiles.has(resolved.fileIdentity)) {
        throw new Error(
          "Experiment targets must identify distinct existing project files after filesystem resolution.",
        );
      }
      seenFiles.add(resolved.fileIdentity);
    }
    identityByTarget[canonicalExperimentTarget(rawTargets[index])] = resolved.target;
  }
  return {
    matrix:
      normalized.targets.length > 0
        ? { ...normalized, targets: identities.map((identity) => identity.target) }
        : {
            ...normalized,
            base: { ...normalized.base, target: identities[0].target },
          },
    identityByTarget,
  };
}

function sanitizeRemote(remote: string): string {
  const trimmed = remote.trim();
  if (!trimmed) {
    return "";
  }
  try {
    const parsed = new URL(trimmed);
    parsed.username = "";
    parsed.password = "";
    // Git hosting tokens are also commonly embedded in query parameters.
    // Source identity needs only the repository location, never URL metadata.
    parsed.search = "";
    parsed.hash = "";
    return parsed.toString();
  } catch {
    // SCP-style Git URLs may contain a username. It is not part of source
    // identity and should not be persisted in experiment results.
    return trimmed.replace(/^[^/@:]+@(?=[^/:]+:)/, "");
  }
}

function parseYamlScalar(raw: string): string | null {
  const withoutComment = raw.replace(/\s+#.*$/, "").trim();
  if (!withoutComment || /^[>|&*!]/.test(withoutComment)) {
    return null;
  }
  if (withoutComment.startsWith('"')) {
    try {
      const parsed = JSON.parse(withoutComment);
      return typeof parsed === "string" ? parsed : null;
    } catch {
      return null;
    }
  }
  if (withoutComment.startsWith("'")) {
    if (!withoutComment.endsWith("'")) {
      return null;
    }
    return withoutComment.slice(1, -1).replace(/''/g, "'");
  }
  return withoutComment;
}

/**
 * Reject manifest paths that would route a clone's writes outside the clone.
 *
 * LeanFlow resolves these four fields as filesystem paths. Supporting complex
 * YAML expressions here would make containment ambiguous, so research mode
 * fails closed and asks the user to use an ordinary scalar path.
 */
export function validateIsolatedManifest(text: string): void {
  const required = new Set(["lean_root", "runtime", "cache", "workflows"]);
  const seen = new Set<string>();
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*(lean_root|runtime|cache|workflows):\s*(.*?)\s*$/);
    if (!match) {
      continue;
    }
    const key = match[1];
    const value = parseYamlScalar(match[2]);
    if (value === null || value.includes("\0")) {
      throw new Error(`Cannot isolate project.yaml: ${key} must be a plain scalar path.`);
    }
    if (unsafePortableRelativePath(value)) {
      throw new Error(`Cannot isolate project.yaml: ${key} must stay inside the project.`);
    }
    seen.add(key);
  }
  for (const key of required) {
    if (!seen.has(key)) {
      throw new Error(`Cannot isolate project.yaml: missing an explicit ${key} path.`);
    }
  }
}

/** Capture the required-clean Git source shared by every experiment cell. */
export async function captureExperimentBaseline(
  projectRoot: string,
): Promise<ExperimentBaseline> {
  if (!projectRoot) {
    throw new Error("Open a LeanFlow project before starting a research sweep.");
  }
  const canonicalProject = await fs.promises.realpath(projectRoot);
  const repositoryRoot = await fs.promises.realpath(
    (await git(canonicalProject, ["rev-parse", "--show-toplevel"])).trim(),
  );
  const projectSubdirectory = containedRelative(repositoryRoot, canonicalProject);

  const dirty = await git(repositoryRoot, [
    "status",
    "--porcelain=v1",
    "-z",
    "--untracked-files=all",
  ]);
  const dirtyCount = dirty.split("\0").filter(Boolean).length;
  if (dirtyCount > 0) {
    throw new Error(
      `Research sweeps require a clean Git baseline; found ${dirtyCount} tracked or ` +
        "untracked change(s). Commit or stash them, then start the sweep again.",
    );
  }

  if (fs.existsSync(path.join(repositoryRoot, ".gitmodules"))) {
    throw new Error(
      "Research sweeps currently fail closed for Git submodules because their revisions " +
        "cannot yet be cloned and verified as one immutable baseline.",
    );
  }
  const symlinkEntries = (await git(repositoryRoot, ["ls-files", "--stage"]))
    .split(/\r?\n/)
    .filter((line) => line.startsWith("120000 "));
  if (symlinkEntries.length > 0) {
    throw new Error(
      "Research sweeps require a source tree without tracked symbolic links; a cell could " +
        "otherwise write outside its isolated clone.",
    );
  }

  const manifestPath = path.join(canonicalProject, MANIFEST);
  const stateRoot = path.join(canonicalProject, ".leanflow");
  const stateStat = await fs.promises.lstat(stateRoot).catch(() => null);
  if (!stateStat?.isDirectory() || stateStat.isSymbolicLink()) {
    throw new Error("Research sweeps require a regular, non-symlinked .leanflow directory.");
  }
  const manifestStat = await fs.promises.lstat(manifestPath).catch(() => null);
  if (!manifestStat?.isFile() || manifestStat.isSymbolicLink()) {
    throw new Error("Research sweeps require a regular, non-symlinked .leanflow/project.yaml.");
  }
  containedRelative(canonicalProject, await fs.promises.realpath(manifestPath));
  const manifest = await fs.promises.readFile(manifestPath, "utf8");
  validateIsolatedManifest(manifest);

  // A research clone must carry its toolchain and exact Lake dependency lock
  // in Git. Copying ignored dependency state would make cells depend on a
  // mutable source checkout; omitting it could resolve different revisions.
  const requiredSourceFiles = ["lean-toolchain", "lake-manifest.json"];
  const lakeDefinition = (await Promise.all(
    ["lakefile.lean", "lakefile.toml"].map(async (candidate) => {
      try {
        await requireContainedRegularFile(canonicalProject, candidate, "Lean project definition");
        return candidate;
      } catch {
        return null;
      }
    }),
  )).find((candidate): candidate is string => candidate !== null);
  if (!lakeDefinition) {
    throw new Error(
      "Research sweeps require a regular lakefile.lean or lakefile.toml in the project baseline.",
    );
  }
  requiredSourceFiles.push(lakeDefinition);
  for (const relative of requiredSourceFiles) {
    await requireContainedRegularFile(canonicalProject, relative, "Research baseline input");
    const repositoryRelative = path
      .join(projectSubdirectory === "." ? "" : projectSubdirectory, relative)
      .replace(/\\/g, "/");
    try {
      await git(repositoryRoot, ["ls-files", "--error-unmatch", "--", repositoryRelative]);
    } catch {
      throw new Error(
        `Research baseline input must be tracked by Git so every cell receives it: ${relative}`,
      );
    }
  }

  const commit = (await git(repositoryRoot, ["rev-parse", "--verify", "HEAD^{commit}"])).trim();
  const tree = (await git(repositoryRoot, ["rev-parse", "--verify", "HEAD^{tree}"])).trim();
  const remote = await git(repositoryRoot, ["config", "--get", "remote.origin.url"]).catch(
    () => "",
  );
  return {
    strategy: "detached-local-clone",
    repositoryRoot,
    projectSubdirectory,
    source: sanitizeRemote(remote) || repositoryRoot,
    commit,
    tree,
    manifestSha256: crypto.createHash("sha256").update(manifest).digest("hex"),
    capturedAt: new Date().toISOString(),
  };
}

async function safeStorageRoot(storageRoot: string): Promise<string> {
  const absolute = path.resolve(storageRoot);
  const clonesRoot = path.dirname(absolute);
  const globalRoot = path.dirname(clonesRoot);
  const matrixDirectory = path.basename(absolute);
  if (
    path.basename(clonesRoot) !== "experiment-clones" ||
    !/^[0-9a-f]{16}$/.test(matrixDirectory)
  ) {
    throw new Error("Experiment storage is not an app-owned matrix directory.");
  }
  const secured = await secureStorageDirectory(
    globalRoot,
    ["experiment-clones", matrixDirectory],
    true,
  );
  if (secured === null) {
    throw new Error("Experiment storage could not be created safely.");
  }
  return secured;
}

/** Create a detached clone for one cell and return its isolated project root. */
export async function createIsolatedCellProject(
  baseline: ExperimentBaseline,
  originalProjectRoot: string,
  storageRoot: string,
  cellId: string,
): Promise<string> {
  const canonicalStorage = await safeStorageRoot(storageRoot);
  const slug = crypto.createHash("sha256").update(cellId).digest("hex").slice(0, 12);
  const cloneRepository = await fs.promises.mkdtemp(path.join(canonicalStorage, `${slug}-`));
  const canonicalClone = await fs.promises.realpath(cloneRepository);
  containedRelative(canonicalStorage, canonicalClone);

  await execFileAsync(
    "git",
    ["clone", "--local", "--no-checkout", baseline.repositoryRoot, canonicalClone],
    {
      timeout: GIT_TIMEOUT_MS,
      maxBuffer: 16 * 1024 * 1024,
      env: { ...process.env },
    },
  );
  await git(canonicalClone, ["checkout", "--detach", baseline.commit]);

  const clonedCommit = (
    await git(canonicalClone, ["rev-parse", "--verify", "HEAD^{commit}"])
  ).trim();
  const clonedTree = (
    await git(canonicalClone, ["rev-parse", "--verify", "HEAD^{tree}"])
  ).trim();
  const clonedStatus = await git(canonicalClone, [
    "status",
    "--porcelain=v1",
    "--untracked-files=all",
  ]);
  if (
    clonedCommit !== baseline.commit ||
    clonedTree !== baseline.tree ||
    clonedStatus.trim() !== ""
  ) {
    throw new Error("The isolated clone did not reproduce the recorded Git baseline exactly.");
  }

  const projectRootCandidate =
    baseline.projectSubdirectory === "."
      ? canonicalClone
      : path.join(canonicalClone, baseline.projectSubdirectory);
  const projectStat = await fs.promises.lstat(projectRootCandidate).catch(() => null);
  if (!projectStat?.isDirectory() || projectStat.isSymbolicLink()) {
    throw new Error("The recorded project directory is absent or unsafe in the isolated clone.");
  }
  const projectRoot = await fs.promises.realpath(projectRootCandidate);
  containedRelative(canonicalClone, projectRoot);
  const canonicalOriginal = await fs.promises.realpath(originalProjectRoot);
  const expectedOriginal = await fs.promises.realpath(
    baseline.projectSubdirectory === "."
      ? baseline.repositoryRoot
      : path.join(baseline.repositoryRoot, baseline.projectSubdirectory),
  );
  if (canonicalOriginal !== expectedOriginal) {
    throw new Error("The source project identity changed after the baseline was captured.");
  }
  const originalManifest = path.join(originalProjectRoot, MANIFEST);
  const originalState = await fs.promises.lstat(path.dirname(originalManifest));
  if (!originalState.isDirectory() || originalState.isSymbolicLink()) {
    throw new Error("The source project storage changed while the sweep was being prepared.");
  }
  const manifestStat = await fs.promises.lstat(originalManifest);
  if (!manifestStat.isFile() || manifestStat.isSymbolicLink()) {
    throw new Error("The source project manifest changed while the sweep was being prepared.");
  }
  const manifest = await fs.promises.readFile(originalManifest, "utf8");
  validateIsolatedManifest(manifest);
  if (crypto.createHash("sha256").update(manifest).digest("hex") !== baseline.manifestSha256) {
    throw new Error("The project manifest changed after the experiment baseline was captured.");
  }

  const stateRoot = path.join(projectRoot, ".leanflow");
  const stateStat = await fs.promises.lstat(stateRoot).catch(() => null);
  if (stateStat?.isSymbolicLink() || (stateStat && !stateStat.isDirectory())) {
    throw new Error("The isolated source contains an unsafe .leanflow path.");
  }
  await fs.promises.mkdir(stateRoot, { recursive: true });
  const clonedManifest = path.join(stateRoot, "project.yaml");
  const clonedManifestStat = await fs.promises.lstat(clonedManifest).catch(() => null);
  if (clonedManifestStat !== null) {
    if (clonedManifestStat.isSymbolicLink() || !clonedManifestStat.isFile()) {
      throw new Error("The isolated source contains an unsafe project manifest.");
    }
    if ((await fs.promises.readFile(clonedManifest, "utf8")) !== manifest) {
      throw new Error("The tracked project manifest differs from the captured baseline manifest.");
    }
  } else {
    await fs.promises.writeFile(clonedManifest, manifest, { encoding: "utf8", flag: "wx" });
  }
  return projectRoot;
}

async function requireContainedRegularFile(
  projectRoot: string,
  rawPath: string,
  label: string,
): Promise<void> {
  const normalized = rawPath.trim();
  if (unsafePortableRelativePath(normalized)) {
    throw new Error(`${label} must be an explicit project-relative file path.`);
  }
  const candidate = path.join(projectRoot, normalized);
  const stat = await fs.promises.lstat(candidate).catch(() => null);
  if (!stat?.isFile() || stat.isSymbolicLink()) {
    throw new Error(`${label} is absent or symlinked in the immutable source clone: ${normalized}`);
  }
  const realProject = await fs.promises.realpath(projectRoot);
  containedRelative(realProject, await fs.promises.realpath(candidate));
}

function isDiscoveredSkillName(value: string): boolean {
  return Boolean(
    value &&
      !value.startsWith(".") &&
      !value.startsWith("~") &&
      !path.isAbsolute(value) &&
      !/^[A-Za-z]:/.test(value) &&
      !/[\\/]/.test(value),
  );
}

async function requireContainedSkillPath(projectRoot: string, rawPath: string): Promise<void> {
  const normalized = rawPath.trim();
  if (unsafePortableRelativePath(normalized)) {
    throw new Error("Additional skill must be a discovered name or project-relative path.");
  }
  const candidate = path.join(projectRoot, normalized);
  const stat = await fs.promises.lstat(candidate).catch(() => null);
  if (stat?.isFile() && !stat.isSymbolicLink()) {
    if (path.basename(candidate) !== "SKILL.md") {
      throw new Error(`Additional skill file must be named SKILL.md: ${normalized}`);
    }
    const realProject = await fs.promises.realpath(projectRoot);
    containedRelative(realProject, await fs.promises.realpath(candidate));
    return;
  }
  if (stat?.isDirectory() && !stat.isSymbolicLink()) {
    await requireContainedRegularFile(
      projectRoot,
      path.join(normalized, "SKILL.md"),
      "Additional skill entrypoint",
    );
    return;
  }
  throw new Error(
    `Additional skill is absent or symlinked in the immutable source clone: ${normalized}`,
  );
}

/** Verify every cell input resolves within, and therefore varies with, its clone. */
export async function validateIsolatedLaunchInputs(
  projectRoot: string,
  target: string,
  additionalSkills: readonly string[],
): Promise<void> {
  if (target.trim()) {
    await requireContainedRegularFile(projectRoot, target, "Experiment target");
  }
  for (const skill of additionalSkills) {
    const normalized = skill.trim();
    if (isDiscoveredSkillName(normalized)) {
      throw new Error(
        `Research sweep additional skills must be tracked project paths, not resolver names: ${normalized}`,
      );
    }
    await requireContainedSkillPath(projectRoot, normalized);
  }
}

async function lstatOrNull(candidate: string): Promise<fs.Stats | null> {
  try {
    return await fs.promises.lstat(candidate);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return null;
    }
    throw error;
  }
}

/**
 * Delete only the app-owned clones for one experiment.
 *
 * Every ancestor below global storage is rechecked as a regular directory and
 * the real target must remain contained. A replaced symlink therefore causes a
 * safe refusal rather than redirecting recursive deletion.
 */
export async function deleteExperimentClones(
  globalStorageRoot: string,
  matrixId: string,
): Promise<boolean> {
  const globalStat = await lstatOrNull(globalStorageRoot);
  if (globalStat === null) {
    return false;
  }
  if (!globalStat.isDirectory() || globalStat.isSymbolicLink()) {
    throw new Error("Refusing to clean an unsafe experiment global-storage root.");
  }
  const realGlobal = await fs.promises.realpath(globalStorageRoot);
  const clonesRoot = path.join(realGlobal, "experiment-clones");
  const clonesStat = await lstatOrNull(clonesRoot);
  if (clonesStat === null) {
    return false;
  }
  if (!clonesStat.isDirectory() || clonesStat.isSymbolicLink()) {
    throw new Error("Refusing to clean a symlinked experiment-clones directory.");
  }
  const realClones = await fs.promises.realpath(clonesRoot);
  containedRelative(realGlobal, realClones);

  const target = path.join(realClones, experimentStorageKey(matrixId));
  const targetStat = await lstatOrNull(target);
  if (targetStat === null) {
    return false;
  }
  if (!targetStat.isDirectory() || targetStat.isSymbolicLink()) {
    throw new Error("Refusing to recursively clean an unsafe experiment entry.");
  }
  const realTarget = await fs.promises.realpath(target);
  containedRelative(realClones, realTarget);

  // Re-resolve immediately before recursive removal to catch an ancestor swap.
  if (
    (await fs.promises.realpath(globalStorageRoot)) !== realGlobal ||
    (await fs.promises.realpath(clonesRoot)) !== realClones ||
    (await fs.promises.realpath(target)) !== realTarget
  ) {
    throw new Error("Experiment storage changed before cleanup; nothing was deleted.");
  }
  await fs.promises.rm(realTarget, { recursive: true, force: false });
  return true;
}
