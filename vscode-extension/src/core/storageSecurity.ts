/**
 * Keep extension-managed files inside a project's real filesystem boundary.
 *
 * Lexical `path.relative` checks do not protect a write when `.leanflow` or one
 * of its children is a symlink. These helpers walk storage directories one
 * component at a time, reject links, and use exclusive no-follow temporary
 * files before an atomic replacement.
 */
import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";

/** Report a storage path that cannot be trusted for an extension write. */
export class UnsafeStoragePathError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "UnsafeStoragePathError";
  }
}

/** Return whether `candidate` is `root` or a descendant of it. */
export function isInsidePath(root: string, candidate: string): boolean {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
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

function validateStorageSegments(segments: readonly string[]): void {
  if (
    segments.length === 0 ||
    segments.some(
      (segment) =>
        !segment || segment === "." || segment === ".." || path.basename(segment) !== segment,
    )
  ) {
    throw new UnsafeStoragePathError("The extension storage path is not a safe relative path.");
  }
}

/**
 * Return a no-symlink storage directory below a project's real root.
 *
 * Missing components are created only when `create` is true. Each component is
 * checked after creation as well, closing the common `mkdir`/symlink fallback
 * mistake. The returned path is the canonical real path.
 */
export async function secureStorageDirectory(
  projectRoot: string,
  segments: readonly string[],
  create: boolean,
): Promise<string | null> {
  validateStorageSegments(segments);
  const realProjectRoot = await fs.promises.realpath(projectRoot);
  let current = realProjectRoot;

  for (const segment of segments) {
    const next = path.join(current, segment);
    let stat = await lstatOrNull(next);
    if (stat === null) {
      if (!create) {
        return null;
      }
      try {
        await fs.promises.mkdir(next, { mode: 0o700 });
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "EEXIST") {
          throw error;
        }
      }
      stat = await fs.promises.lstat(next);
    }
    if (stat.isSymbolicLink()) {
      throw new UnsafeStoragePathError(
        `Refusing to use symlinked LeanFlow storage directory: ${next}`,
      );
    }
    if (!stat.isDirectory()) {
      throw new UnsafeStoragePathError(`LeanFlow storage path is not a directory: ${next}`);
    }
    const realNext = await fs.promises.realpath(next);
    if (!isInsidePath(realProjectRoot, realNext)) {
      throw new UnsafeStoragePathError(
        `Refusing to use LeanFlow storage outside the active project: ${next}`,
      );
    }
    current = realNext;
  }
  return current;
}

function safeLeafName(name: string): void {
  if (!name || name === "." || name === ".." || path.basename(name) !== name) {
    throw new UnsafeStoragePathError("The extension storage filename is not safe.");
  }
}

/** Replace a regular file while preserving the old value on Windows failure. */
async function replaceStorageFile(temporary: string, target: string): Promise<void> {
  try {
    await fs.promises.rename(temporary, target);
    return;
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (!new Set(["EEXIST", "EPERM", "EACCES"]).has(code ?? "")) {
      throw error;
    }
  }

  // Windows does not consistently replace an existing destination with
  // rename(2). Move the already-validated regular file aside, install the new
  // file, then remove the backup. If installation fails, restore the old file.
  const backup = `${target}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.bak`;
  await fs.promises.rename(target, backup);
  try {
    await fs.promises.rename(temporary, target);
  } catch (error) {
    await fs.promises.rename(backup, target).catch(() => undefined);
    throw error;
  }
  await fs.promises.unlink(backup).catch(() => undefined);
}

/** Write one storage file through an exclusive no-follow temporary file. */
export async function writeProjectStorageFile(
  projectRoot: string,
  segments: readonly string[],
  filename: string,
  contents: string,
): Promise<string> {
  safeLeafName(filename);
  const directory = await secureStorageDirectory(projectRoot, segments, true);
  if (directory === null) {
    throw new UnsafeStoragePathError("LeanFlow storage directory could not be created.");
  }

  const target = path.join(directory, filename);
  const existing = await lstatOrNull(target);
  if (existing?.isSymbolicLink() || (existing !== null && !existing.isFile())) {
    throw new UnsafeStoragePathError(`Refusing to replace unsafe storage entry: ${target}`);
  }

  const temporary = path.join(
    directory,
    `.${filename}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`,
  );
  const flags =
    fs.constants.O_WRONLY |
    fs.constants.O_CREAT |
    fs.constants.O_EXCL |
    (fs.constants.O_NOFOLLOW ?? 0);
  let handle: fs.promises.FileHandle | null = null;
  try {
    handle = await fs.promises.open(temporary, flags, 0o600);
    await handle.writeFile(contents, "utf8");
    await handle.sync();
    await handle.close();
    handle = null;

    // Re-walk immediately before the rename so a replaced ancestor is caught.
    const rechecked = await secureStorageDirectory(projectRoot, segments, false);
    if (rechecked === null || rechecked !== directory) {
      throw new UnsafeStoragePathError("LeanFlow storage changed while the file was saved.");
    }
    const recheckedTarget = path.join(rechecked, filename);
    await replaceStorageFile(temporary, recheckedTarget);
    return recheckedTarget;
  } catch (error) {
    if (handle !== null) {
      await handle.close().catch(() => undefined);
    }
    await fs.promises.unlink(temporary).catch(() => undefined);
    throw error;
  }
}

/** Delete one regular storage file without following a link. */
export async function deleteProjectStorageFile(
  projectRoot: string,
  segments: readonly string[],
  filename: string,
): Promise<boolean> {
  safeLeafName(filename);
  const directory = await secureStorageDirectory(projectRoot, segments, false);
  if (directory === null) {
    return false;
  }
  const target = path.join(directory, filename);
  const stat = await lstatOrNull(target);
  if (stat === null) {
    return false;
  }
  if (stat.isSymbolicLink() || !stat.isFile()) {
    throw new UnsafeStoragePathError(`Refusing to delete unsafe storage entry: ${target}`);
  }

  const rechecked = await secureStorageDirectory(projectRoot, segments, false);
  if (rechecked === null || rechecked !== directory) {
    throw new UnsafeStoragePathError("LeanFlow storage changed before the file was deleted.");
  }
  await fs.promises.unlink(path.join(rechecked, filename));
  return true;
}

/** Resolve an existing path only when its real target remains in the project. */
export async function resolveExistingProjectPath(
  projectRoot: string,
  candidate: string,
): Promise<string | null> {
  const lexicalRoot = path.resolve(projectRoot);
  const lexicalTarget = path.resolve(lexicalRoot, candidate);
  if (!isInsidePath(lexicalRoot, lexicalTarget)) {
    return null;
  }
  try {
    const [realRoot, realTarget] = await Promise.all([
      fs.promises.realpath(lexicalRoot),
      fs.promises.realpath(lexicalTarget),
    ]);
    return isInsidePath(realRoot, realTarget) ? realTarget : null;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return null;
    }
    throw error;
  }
}
