/** Filesystem-boundary tests for extension-managed project storage. */
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";

import {
  deleteProjectStorageFile,
  resolveExistingProjectPath,
  secureStorageDirectory,
  writeProjectStorageFile,
} from "../dist/test/storageSecurity.mjs";

async function fixture(t) {
  const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), "leanflow-storage-test-"));
  const project = path.join(root, "project");
  const outside = path.join(root, "outside");
  await fs.promises.mkdir(project);
  await fs.promises.mkdir(outside);
  t.after(() => fs.promises.rm(root, { recursive: true, force: true }));
  return { project, outside };
}

async function symlinkOrSkip(t, target, link) {
  try {
    await fs.promises.symlink(target, link, process.platform === "win32" ? "junction" : "dir");
    return true;
  } catch (error) {
    if (["EPERM", "EACCES"].includes(error.code)) {
      t.skip("creating symlinks is not permitted on this host");
      return false;
    }
    throw error;
  }
}

test("profile storage creates, replaces, and deletes a regular file", async (t) => {
  const { project } = await fixture(t);
  const segments = [".leanflow", "flag-profiles"];
  const first = await writeProjectStorageFile(project, segments, "research.json", "one\n");
  assert.equal(await fs.promises.readFile(first, "utf8"), "one\n");

  const second = await writeProjectStorageFile(project, segments, "research.json", "two\n");
  assert.equal(second, first);
  assert.equal(await fs.promises.readFile(first, "utf8"), "two\n");
  assert.equal(await deleteProjectStorageFile(project, segments, "research.json"), true);
  assert.equal(await deleteProjectStorageFile(project, segments, "research.json"), false);
});

test("a symlinked profile directory cannot redirect a write", async (t) => {
  const { project, outside } = await fixture(t);
  await fs.promises.mkdir(path.join(project, ".leanflow"));
  if (
    !(await symlinkOrSkip(
      t,
      outside,
      path.join(project, ".leanflow", "flag-profiles"),
    ))
  ) {
    return;
  }
  await assert.rejects(
    writeProjectStorageFile(
      project,
      [".leanflow", "flag-profiles"],
      "research.json",
      "unsafe\n",
    ),
    /symlinked LeanFlow storage/,
  );
  assert.equal(fs.existsSync(path.join(outside, "research.json")), false);
});

test("a symlinked profile file is neither replaced nor deleted", async (t) => {
  const { project, outside } = await fixture(t);
  const directory = await secureStorageDirectory(
    project,
    [".leanflow", "flag-profiles"],
    true,
  );
  assert.ok(directory);
  const outsideFile = path.join(outside, "important.json");
  await fs.promises.writeFile(outsideFile, "keep\n");
  try {
    await fs.promises.symlink(outsideFile, path.join(directory, "research.json"), "file");
  } catch (error) {
    if (["EPERM", "EACCES"].includes(error.code)) {
      t.skip("creating symlinks is not permitted on this host");
      return;
    }
    throw error;
  }

  await assert.rejects(
    writeProjectStorageFile(
      project,
      [".leanflow", "flag-profiles"],
      "research.json",
      "replace\n",
    ),
    /unsafe storage entry/,
  );
  await assert.rejects(
    deleteProjectStorageFile(
      project,
      [".leanflow", "flag-profiles"],
      "research.json",
    ),
    /unsafe storage entry/,
  );
  assert.equal(await fs.promises.readFile(outsideFile, "utf8"), "keep\n");
});

test("opening an in-project symlink cannot escape the real project", async (t) => {
  const { project, outside } = await fixture(t);
  const outsideFile = path.join(outside, "secret.txt");
  await fs.promises.writeFile(outsideFile, "secret\n");
  try {
    await fs.promises.symlink(outsideFile, path.join(project, "linked.txt"), "file");
  } catch (error) {
    if (["EPERM", "EACCES"].includes(error.code)) {
      t.skip("creating symlinks is not permitted on this host");
      return;
    }
    throw error;
  }
  assert.equal(await resolveExistingProjectPath(project, "linked.txt"), null);
});
