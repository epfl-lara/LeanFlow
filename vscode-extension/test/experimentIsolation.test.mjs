import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { promisify } from "node:util";
import { test } from "node:test";

import {
  captureExperimentBaseline,
  createIsolatedCellProject,
  deleteExperimentClones,
  experimentStorageKey,
  resolveExistingExperimentMatrixTargets,
  validateIsolatedLaunchInputs,
  validateIsolatedManifest,
} from "../dist/test/experimentIsolation.mjs";

const execFileAsync = promisify(execFile);
const MANIFEST = `schema_version: 1
name: demo
kind: lean4
lean_root: .
paths:
  runtime: .leanflow/runtime
  cache: .leanflow/cache
  workflows: .leanflow/workflows
`;

async function git(cwd, ...args) {
  await execFileAsync("git", ["-C", cwd, ...args]);
}

async function repository(trackedManifest) {
  const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), "leanflow-isolation-test-"));
  await git(root, "init", "-q");
  await git(root, "config", "user.name", "LeanFlow Test");
  await git(root, "config", "user.email", "leanflow-test@example.invalid");
  await fs.promises.writeFile(path.join(root, "lakefile.lean"), "package Demo\n");
  await fs.promises.writeFile(path.join(root, "lean-toolchain"), "leanprover/lean4:v4.19.0\n");
  await fs.promises.writeFile(
    path.join(root, "lake-manifest.json"),
    JSON.stringify({ version: "1.1.0", packages: [] }),
  );
  await fs.promises.mkdir(path.join(root, ".leanflow"));
  await fs.promises.writeFile(path.join(root, ".leanflow", "project.yaml"), MANIFEST);
  if (!trackedManifest) {
    await fs.promises.writeFile(path.join(root, ".gitignore"), ".leanflow/\n");
  }
  await git(root, "add", ".");
  await git(root, "commit", "-q", "-m", "baseline");
  return root;
}

async function experimentStorage(matrixId = "matrix") {
  const global = await fs.promises.mkdtemp(path.join(os.tmpdir(), "leanflow-global-storage-"));
  const storage = path.join(global, "experiment-clones", experimentStorageKey(matrixId));
  return { global, storage };
}

test("manifest validation refuses paths that can escape a clone", () => {
  assert.doesNotThrow(() => validateIsolatedManifest(MANIFEST));
  assert.throws(
    () => validateIsolatedManifest(MANIFEST.replace("lean_root: .", "lean_root: ../outside")),
    /stay inside/,
  );
  assert.throws(
    () => validateIsolatedManifest(MANIFEST.replace("cache: .leanflow/cache", "cache: /tmp/cache")),
    /stay inside/,
  );
  assert.throws(
    () => validateIsolatedManifest(MANIFEST.replace("workflows: .leanflow/workflows", "workflows: >")),
    /plain scalar/,
  );
  for (const field of ["lean_root", "runtime", "cache", "workflows"]) {
    const pattern = new RegExp(`(^|\\s)${field}: [^\\n]+`, "m");
    assert.throws(
      () => validateIsolatedManifest(MANIFEST.replace(pattern, `$1${field}: ~/outside`)),
      /stay inside/,
      field,
    );
  }
  assert.throws(
    () => validateIsolatedManifest(MANIFEST.replace("lean_root: .", "lean_root: C:relative")),
    /stay inside/,
  );
});

test("cell targets and sweep skills must be sealed files inside the clone", async (t) => {
  const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), "leanflow-input-test-"));
  const outside = await fs.promises.mkdtemp(path.join(os.tmpdir(), "leanflow-input-outside-"));
  t.after(async () => {
    await fs.promises.rm(root, { recursive: true, force: true });
    await fs.promises.rm(outside, { recursive: true, force: true });
  });
  await fs.promises.writeFile(path.join(root, "Main.lean"), "theorem x : True := by trivial\n");
  await fs.promises.mkdir(path.join(root, "skills"));
  await fs.promises.writeFile(path.join(root, "skills", "notes.md"), "proof notes\n");
  await fs.promises.mkdir(path.join(root, "skills", "directory-skill"));
  await fs.promises.writeFile(
    path.join(root, "skills", "directory-skill", "SKILL.md"),
    "proof skill\n",
  );
  await validateIsolatedLaunchInputs(root, "Main.lean", [
    "skills/directory-skill",
    "skills/directory-skill/SKILL.md",
  ]);
  await assert.rejects(
    validateIsolatedLaunchInputs(root, "Main.lean", ["leanflow-prove"]),
    /not resolver names/,
  );
  await assert.rejects(
    validateIsolatedLaunchInputs(root, "../outside.lean", []),
    /project-relative/,
  );
  await assert.rejects(
    validateIsolatedLaunchInputs(root, "C:relative.lean", []),
    /project-relative/,
  );
  await assert.rejects(
    validateIsolatedLaunchInputs(root, "~/outside.lean", []),
    /project-relative/,
  );
  await assert.rejects(
    validateIsolatedLaunchInputs(root, "Main.lean", ["skills/missing.md"]),
    /absent or symlinked/,
  );
  await assert.rejects(
    validateIsolatedLaunchInputs(root, "Main.lean", ["skills/notes.md"]),
    /must be named SKILL.md/,
  );
  await fs.promises.writeFile(path.join(outside, "Outside.lean"), "outside\n");
  await fs.promises.symlink(path.join(outside, "Outside.lean"), path.join(root, "Linked.lean"));
  await assert.rejects(
    validateIsolatedLaunchInputs(root, "Linked.lean", []),
    /absent or symlinked/,
  );
});

test("filesystem target identity follows real files without folding distinct names", async (t) => {
  const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), "leanflow-target-id-test-"));
  t.after(() => fs.promises.rm(root, { recursive: true, force: true }));
  const definition = (targets) => ({
    targets,
    profiles: ["p"],
    models: ["m"],
    repeats: 1,
    base: { target: "" },
  });

  await fs.promises.writeFile(path.join(root, "Main.lean"), "main\n");
  await fs.promises.link(path.join(root, "Main.lean"), path.join(root, "Alias.lean"));
  await assert.rejects(
    resolveExistingExperimentMatrixTargets(
      root,
      definition(["Main.lean", "Alias.lean"]),
    ),
    /distinct existing project files/,
  );

  await fs.promises.writeFile(path.join(root, "MixedCase.lean"), "case\n");
  const lowerCaseAlias = "mixedcase.lean";
  const caseAliases = await fs.promises
    .access(path.join(root, lowerCaseAlias))
    .then(() => true)
    .catch(() => false);
  if (caseAliases) {
    const [stored, alias] = await Promise.all([
      resolveExistingExperimentMatrixTargets(root, definition(["MixedCase.lean"])),
      resolveExistingExperimentMatrixTargets(root, definition([lowerCaseAlias])),
    ]);
    assert.equal(stored.matrix.targets[0], alias.matrix.targets[0]);
    await assert.rejects(
      resolveExistingExperimentMatrixTargets(
        root,
        definition(["MixedCase.lean", lowerCaseAlias]),
      ),
      /distinct existing project files/,
    );
  } else {
    await fs.promises.writeFile(path.join(root, lowerCaseAlias), "lower case\n");
    const distinct = await resolveExistingExperimentMatrixTargets(
      root,
      definition(["MixedCase.lean", lowerCaseAlias]),
    );
    assert.notEqual(distinct.matrix.targets[0], distinct.matrix.targets[1]);
  }

  const composed = "Caf\u00e9.lean";
  const decomposed = "Cafe\u0301.lean";
  await fs.promises.writeFile(path.join(root, composed), "unicode\n");
  const unicodeAliases = await fs.promises
    .access(path.join(root, decomposed))
    .then(() => true)
    .catch(() => false);
  if (unicodeAliases) {
    const [stored, alias] = await Promise.all([
      resolveExistingExperimentMatrixTargets(root, definition([composed])),
      resolveExistingExperimentMatrixTargets(root, definition([decomposed])),
    ]);
    assert.equal(stored.matrix.targets[0], alias.matrix.targets[0]);
    await assert.rejects(
      resolveExistingExperimentMatrixTargets(root, definition([composed, decomposed])),
      /distinct existing project files/,
    );
  } else {
    await fs.promises.writeFile(path.join(root, decomposed), "decomposed\n");
    const distinct = await resolveExistingExperimentMatrixTargets(
      root,
      definition([composed, decomposed]),
    );
    assert.notEqual(distinct.matrix.targets[0], distinct.matrix.targets[1]);
  }
});

test("each cell gets the exact clean baseline and a private manifest", async (t) => {
  const root = await repository(false);
  const { global, storage } = await experimentStorage();
  t.after(async () => {
    await fs.promises.rm(root, { recursive: true, force: true });
    await fs.promises.rm(global, { recursive: true, force: true });
  });
  const baseline = await captureExperimentBaseline(root);
  const first = await createIsolatedCellProject(baseline, root, storage, "first");
  const second = await createIsolatedCellProject(baseline, root, storage, "second");
  assert.notEqual(first, second);
  assert.equal(
    (await execFileAsync("git", ["-C", first, "rev-parse", "HEAD"])).stdout.trim(),
    baseline.commit,
  );
  assert.equal(await fs.promises.readFile(path.join(first, ".leanflow", "project.yaml"), "utf8"), MANIFEST);
  await fs.promises.writeFile(path.join(first, "cell-output.txt"), "first");
  assert.equal(fs.existsSync(path.join(second, "cell-output.txt")), false);
  assert.equal(fs.existsSync(path.join(root, "cell-output.txt")), false);
});

test("baseline source URLs never persist remote credentials", async (t) => {
  const root = await repository(false);
  t.after(() => fs.promises.rm(root, { recursive: true, force: true }));
  await git(
    root,
    "remote",
    "add",
    "origin",
    "https://user:password@example.invalid/repository.git?access_token=secret#credential",
  );
  const baseline = await captureExperimentBaseline(root);
  assert.equal(baseline.source, "https://example.invalid/repository.git");
  assert.doesNotMatch(baseline.source, /user|password|token|secret|credential/);
});

test("a tracked project manifest is verified rather than overwritten", async (t) => {
  const root = await repository(true);
  const { global, storage } = await experimentStorage();
  t.after(async () => {
    await fs.promises.rm(root, { recursive: true, force: true });
    await fs.promises.rm(global, { recursive: true, force: true });
  });
  const baseline = await captureExperimentBaseline(root);
  const clone = await createIsolatedCellProject(baseline, root, storage, "tracked");
  assert.equal(await fs.promises.readFile(path.join(clone, ".leanflow", "project.yaml"), "utf8"), MANIFEST);
});

test("dirty or untracked source fails closed without changing it", async (t) => {
  const root = await repository(false);
  t.after(() => fs.promises.rm(root, { recursive: true, force: true }));
  const dirtyFile = path.join(root, "Main.lean");
  await fs.promises.writeFile(dirtyFile, "theorem x : True := by trivial\n");
  await assert.rejects(captureExperimentBaseline(root), /require a clean Git baseline/);
  assert.equal(await fs.promises.readFile(dirtyFile, "utf8"), "theorem x : True := by trivial\n");
});

test("an untracked dependency lock cannot define a research baseline", async (t) => {
  const root = await repository(false);
  t.after(() => fs.promises.rm(root, { recursive: true, force: true }));
  await git(root, "rm", "--cached", "lake-manifest.json");
  await fs.promises.appendFile(path.join(root, ".gitignore"), "lake-manifest.json\n");
  await git(root, "add", ".gitignore");
  await git(root, "commit", "-q", "-m", "ignore lock");
  await assert.rejects(captureExperimentBaseline(root), /must be tracked by Git/);
});

test("changing the ignored manifest after capture fails closed", async (t) => {
  const root = await repository(false);
  const { global, storage } = await experimentStorage();
  t.after(async () => {
    await fs.promises.rm(root, { recursive: true, force: true });
    await fs.promises.rm(global, { recursive: true, force: true });
  });
  const baseline = await captureExperimentBaseline(root);
  await fs.promises.appendFile(path.join(root, ".leanflow", "project.yaml"), "# changed\n");
  await assert.rejects(
    createIsolatedCellProject(baseline, root, storage, "changed"),
    /manifest changed/,
  );
});

test("an absent recorded project subdirectory is never synthesized empty", async (t) => {
  const root = await repository(false);
  const { global, storage } = await experimentStorage();
  t.after(async () => {
    await fs.promises.rm(root, { recursive: true, force: true });
    await fs.promises.rm(global, { recursive: true, force: true });
  });
  const baseline = await captureExperimentBaseline(root);
  await assert.rejects(
    createIsolatedCellProject(
      { ...baseline, projectSubdirectory: "ignored-only-project" },
      root,
      storage,
      "missing-subdirectory",
    ),
    /project directory is absent or unsafe/,
  );
  assert.equal(fs.existsSync(path.join(root, "ignored-only-project")), false);
});

test("cleanup removes only the hashed app-owned matrix directory", async (t) => {
  const { global, storage } = await experimentStorage("remove-me");
  const sibling = path.join(
    global,
    "experiment-clones",
    experimentStorageKey("keep-me"),
  );
  t.after(() => fs.promises.rm(global, { recursive: true, force: true }));
  await fs.promises.mkdir(path.join(storage, "cell"), { recursive: true });
  await fs.promises.mkdir(sibling, { recursive: true });
  await fs.promises.writeFile(path.join(storage, "cell", "output"), "generated");
  await fs.promises.writeFile(path.join(sibling, "output"), "keep");

  assert.equal(await deleteExperimentClones(global, "remove-me"), true);
  assert.equal(fs.existsSync(storage), false);
  assert.equal(await fs.promises.readFile(path.join(sibling, "output"), "utf8"), "keep");
});

test("cleanup refuses a symlinked matrix target", async (t) => {
  const { global, storage } = await experimentStorage("unsafe");
  const outside = await fs.promises.mkdtemp(path.join(os.tmpdir(), "leanflow-outside-"));
  t.after(async () => {
    await fs.promises.rm(global, { recursive: true, force: true });
    await fs.promises.rm(outside, { recursive: true, force: true });
  });
  await fs.promises.mkdir(path.dirname(storage), { recursive: true });
  await fs.promises.writeFile(path.join(outside, "keep"), "outside");
  await fs.promises.symlink(outside, storage, "dir");

  await assert.rejects(deleteExperimentClones(global, "unsafe"), /unsafe experiment entry/);
  assert.equal(await fs.promises.readFile(path.join(outside, "keep"), "utf8"), "outside");
});
