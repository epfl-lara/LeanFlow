/** Filesystem-boundary tests for manual launch targets and supplemental skills. */
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";

import {
  isPathLikeSkill,
  validateLaunchProjectInputs,
} from "../dist/test/launchPaths.mjs";

async function fixture(t) {
  const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), "leanflow-launch-paths-"));
  const project = path.join(root, "project");
  const outside = path.join(root, "outside");
  await fs.promises.mkdir(project);
  await fs.promises.mkdir(outside);
  await fs.promises.writeFile(path.join(project, "Main.lean"), "theorem ok : True := by trivial\n");
  const skill = path.join(project, ".leanflow", "skills", "local-skill");
  await fs.promises.mkdir(skill, { recursive: true });
  await fs.promises.writeFile(path.join(skill, "SKILL.md"), "# Local skill\n");
  t.after(() => fs.promises.rm(root, { recursive: true, force: true }));
  return { project, outside, skill };
}

test("valid project files and simple discovered skill names are accepted", async (t) => {
  const { project } = await fixture(t);
  assert.equal(isPathLikeSkill("lean-proof-loop"), false);
  assert.equal(isPathLikeSkill("C:relative-skill"), true);
  await validateLaunchProjectInputs(project, "Main.lean", [
    "lean-proof-loop",
    ".leanflow/skills/local-skill",
  ]);
});

test("target traversal and absolute outside targets are refused", async (t) => {
  const { project, outside } = await fixture(t);
  const outsideFile = path.join(outside, "Outside.lean");
  await fs.promises.writeFile(outsideFile, "theorem outside : True := by trivial\n");
  await assert.rejects(
    validateLaunchProjectInputs(project, "../outside/Outside.lean", []),
    /escapes the selected LeanFlow project/,
  );
  await assert.rejects(
    validateLaunchProjectInputs(project, outsideFile, []),
    /escapes the selected LeanFlow project/,
  );
});

test("an additional-skill symlink cannot escape the project", async (t) => {
  const { project, outside } = await fixture(t);
  const outsideSkill = path.join(outside, "skill");
  await fs.promises.mkdir(outsideSkill);
  await fs.promises.writeFile(path.join(outsideSkill, "SKILL.md"), "# Secret\n");
  const linked = path.join(project, "linked-skill");
  try {
    await fs.promises.symlink(
      outsideSkill,
      linked,
      process.platform === "win32" ? "junction" : "dir",
    );
  } catch (error) {
    if (["EPERM", "EACCES"].includes(error.code)) {
      t.skip("creating symlinks is not permitted on this host");
      return;
    }
    throw error;
  }
  await assert.rejects(
    validateLaunchProjectInputs(project, "", ["linked-skill/"]),
    /escapes the selected LeanFlow project/,
  );
});

test("absolute outside and non-SKILL files are refused as additional skills", async (t) => {
  const { project, outside } = await fixture(t);
  const outsideSkill = path.join(outside, "SKILL.md");
  await fs.promises.writeFile(outsideSkill, "# Secret\n");
  await fs.promises.mkdir(path.join(project, "skills"));
  await fs.promises.writeFile(path.join(project, "skills", "notes.md"), "not a skill\n");
  await assert.rejects(
    validateLaunchProjectInputs(project, "", [outsideSkill]),
    /escapes the selected LeanFlow project/,
  );
  await assert.rejects(
    validateLaunchProjectInputs(project, "", ["skills/notes.md"]),
    /must be named SKILL.md/,
  );
});
