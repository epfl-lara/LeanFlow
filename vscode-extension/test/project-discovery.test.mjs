/** Nested LeanFlow project discovery policy tests. */
import assert from "node:assert/strict";
import path from "node:path";
import { test } from "node:test";

import {
  isNestedProjectManifestCandidate,
  projectRootForUniqueManifest,
} from "../dist/test/projectDiscovery.mjs";

test("archived project copies do not make the active parent workspace ambiguous", () => {
  const root = "/workspace";
  assert.equal(
    isNestedProjectManifestCandidate(
      root,
      "/workspace/archives/old-run/.leanflow/project.yaml",
    ),
    false,
  );
  assert.equal(
    isNestedProjectManifestCandidate(
      root,
      "/workspace/formalization/.leanflow/project.yaml",
    ),
    true,
  );
});

test("one nested manifest identifies its project root", () => {
  assert.equal(
    projectRootForUniqueManifest(["/workspace/formalization/.leanflow/project.yaml"]),
    path.resolve("/workspace/formalization"),
  );
});

test("zero or multiple nested manifests fail closed", () => {
  assert.equal(projectRootForUniqueManifest([]), null);
  assert.equal(
    projectRootForUniqueManifest([
      "/workspace/one/.leanflow/project.yaml",
      "/workspace/two/.leanflow/project.yaml",
    ]),
    null,
  );
});

test("a file outside the manifest contract is rejected", () => {
  assert.equal(projectRootForUniqueManifest(["/workspace/project.yaml"]), null);
  assert.equal(
    projectRootForUniqueManifest(["/workspace/.leanflow/not-project.yaml"]),
    null,
  );
});
