/** Runtime message validation tests for filesystem-backed identifiers. */
import assert from "node:assert/strict";
import { test } from "node:test";

import { parseWebviewMessage } from "../dist/test/messageSchema.mjs";

test("run log messages accept minted identifiers and the latest-run fallback", () => {
  assert.deepEqual(parseWebviewMessage({ type: "loadRunLog", runId: "prove-vscode-run-1" }), {
    ok: true,
    message: { type: "loadRunLog", runId: "prove-vscode-run-1" },
  });
  assert.deepEqual(parseWebviewMessage({ type: "loadRunLog", runId: "" }), {
    ok: true,
    message: { type: "loadRunLog", runId: "" },
  });
});

test("run log messages reject traversal and separators", () => {
  assert.equal(parseWebviewMessage({ type: "loadRunLog", runId: "../../secret" }).ok, false);
  assert.equal(parseWebviewMessage({ type: "loadRunLog", runId: "run/secret" }).ok, false);
  assert.equal(parseWebviewMessage({ type: "loadRunLog", runId: ".." }).ok, false);
});

test("the CLI settings action is an explicit message", () => {
  assert.deepEqual(parseWebviewMessage({ type: "openCliSettings" }), {
    ok: true,
    message: { type: "openCliSettings" },
  });
});

function launchRequest(overrides = {}) {
  return {
    kind: "prove",
    target: "",
    provider: "",
    model: "",
    agents: 1,
    research: false,
    researchWorkers: null,
    noParallel: false,
    cleanRoom: false,
    humanReview: false,
    axioms: "",
    prompt: "",
    additionalSkills: [],
    profile: "",
    overrides: {},
    ...overrides,
  };
}

function matrix(overrides = {}) {
  return {
    id: "matrix-one",
    name: "Matrix one",
    kind: "prove",
    targets: ["Main.lean"],
    profiles: ["default"],
    models: ["model"],
    repeats: 1,
    base: launchRequest(),
    createdAt: "2026-08-11T00:00:00Z",
    ...overrides,
  };
}

test("an experiment matrix may contain at most 1000 cells", () => {
  const accepted = parseWebviewMessage({
    type: "createExperiment",
    matrix: matrix({
      targets: Array.from({ length: 10 }, (_, index) => `T${index}.lean`),
      profiles: Array.from({ length: 10 }, (_, index) => `p${index}`),
      models: Array.from({ length: 10 }, (_, index) => `m${index}`),
    }),
  });
  assert.equal(accepted.ok, true);

  const rejected = parseWebviewMessage({
    type: "createExperiment",
    matrix: matrix({
      targets: Array.from({ length: 11 }, (_, index) => `T${index}.lean`),
      profiles: Array.from({ length: 10 }, (_, index) => `p${index}`),
      models: Array.from({ length: 10 }, (_, index) => `m${index}`),
    }),
  });
  assert.equal(rejected.ok, false);
  assert.match(rejected.reason, /1100 cells.*maximum is 1000/);
});

test("experiment axes and repeats reject oversize values instead of truncating", () => {
  assert.equal(
    parseWebviewMessage({
      type: "createExperiment",
      matrix: matrix({ targets: Array.from({ length: 101 }, () => "Main.lean") }),
    }).ok,
    false,
  );
  assert.equal(
    parseWebviewMessage({
      type: "createExperiment",
      matrix: matrix({ repeats: 101 }),
    }).ok,
    false,
  );
});

test("selectRun distinguishes null from a missing or malformed id", () => {
  assert.equal(parseWebviewMessage({ type: "selectRun", id: null }).ok, true);
  assert.equal(parseWebviewMessage({ type: "selectRun" }).ok, false);
  assert.equal(parseWebviewMessage({ type: "selectRun", id: 12 }).ok, false);
});

test("profile diffs reject path-like identifiers", () => {
  assert.equal(
    parseWebviewMessage({
      type: "diffProfiles",
      requestId: "request-one",
      left: "../../outside",
      right: "default",
    }).ok,
    false,
  );
});

test("launch and profile payloads reject oversized maps instead of truncating", () => {
  const overrides = Object.fromEntries(
    Array.from({ length: 513 }, (_, index) => [`LEANFLOW_TEST_${index}`, "1"]),
  );
  assert.equal(
    parseWebviewMessage({
      type: "launch",
      request: launchRequest({ overrides }),
    }).ok,
    false,
  );
  assert.equal(
    parseWebviewMessage({
      type: "saveProfile",
      profile: { name: "too-large", summary: "", overrides },
    }).ok,
    false,
  );
});

test("additional skills reject oversized or malformed lists instead of truncating", () => {
  assert.equal(
    parseWebviewMessage({
      type: "launch",
      request: launchRequest({ additionalSkills: Array.from({ length: 257 }, () => "s.md") }),
    }).ok,
    false,
  );
  assert.equal(
    parseWebviewMessage({
      type: "launch",
      request: launchRequest({ additionalSkills: ["s.md", 12] }),
    }).ok,
    false,
  );
});

test("launch fields reject missing, wrong-type, and oversized values", () => {
  assert.equal(
    parseWebviewMessage({
      type: "launch",
      request: launchRequest({ prompt: "x".repeat(8193) }),
    }).ok,
    false,
  );
  assert.equal(
    parseWebviewMessage({
      type: "launch",
      request: launchRequest({ model: 12 }),
    }).ok,
    false,
  );
  const missingTarget = launchRequest();
  delete missingTarget.target;
  assert.equal(parseWebviewMessage({ type: "launch", request: missingTarget }).ok, false);
  assert.equal(
    parseWebviewMessage({
      type: "launch",
      request: launchRequest({ agents: 1.5 }),
    }).ok,
    false,
  );
  assert.equal(
    parseWebviewMessage({
      type: "launch",
      request: launchRequest({ prompt: "proof\0secret" }),
    }).ok,
    false,
  );
});

test("a rejected launch names the offending field so the UI can explain it", () => {
  const longPrompt = parseWebviewMessage({
    type: "launch",
    request: launchRequest({ prompt: "x".repeat(8193) }),
  });
  assert.equal(longPrompt.ok, false);
  assert.match(longPrompt.reason, /launch.*prompt/);

  const badModel = parseWebviewMessage({
    type: "launch",
    request: launchRequest({ model: 12 }),
  });
  assert.equal(badModel.ok, false);
  assert.match(badModel.reason, /model/);

  const badWorkers = parseWebviewMessage({
    type: "launch",
    request: launchRequest({ researchWorkers: 99 }),
  });
  assert.equal(badWorkers.ok, false);
  assert.match(badWorkers.reason, /researchWorkers/);

  const badBase = parseWebviewMessage({
    type: "createExperiment",
    matrix: matrix({ base: launchRequest({ axioms: "x".repeat(2049) }) }),
  });
  assert.equal(badBase.ok, false);
  assert.match(badBase.reason, /base field "axioms"/);
});

test("a rejection reason never echoes the value that was rejected", () => {
  // The host shows this reason to the user, so it must name the field only.
  // A prompt is the one launch field that routinely carries private text.
  const secret = `sk-live-${"z".repeat(9000)}`;
  const rejected = parseWebviewMessage({
    type: "launch",
    request: launchRequest({ prompt: secret }),
  });
  assert.equal(rejected.ok, false);
  assert.equal(rejected.reason.includes("sk-live"), false);
  assert.equal(rejected.reason.includes("zzzz"), false);
  assert.ok(rejected.reason.length < 120);
});

test("experiment axes use the corresponding launch-field length bounds", () => {
  for (const axes of [
    { targets: ["x".repeat(1025)] },
    { profiles: ["x".repeat(129)] },
    { models: ["x".repeat(257)] },
  ]) {
    assert.equal(
      parseWebviewMessage({ type: "createExperiment", matrix: matrix(axes) }).ok,
      false,
    );
  }
});

test("profile fields reject missing or malformed values", () => {
  assert.equal(
    parseWebviewMessage({
      type: "saveProfile",
      profile: { name: "bad-summary", summary: 12, overrides: {} },
    }).ok,
    false,
  );
  assert.equal(
    parseWebviewMessage({
      type: "saveProfile",
      profile: { name: "missing-overrides", summary: "" },
    }).ok,
    false,
  );
});

test("event detail requests carry two filesystem-safe identifiers", () => {
  assert.deepEqual(
    parseWebviewMessage({ type: "loadEventDetail", runId: "prove-vscode-run-1", eventId: "1a315a12eb10" }),
    { ok: true, message: { type: "loadEventDetail", runId: "prove-vscode-run-1", eventId: "1a315a12eb10" } },
  );
  assert.equal(parseWebviewMessage({ type: "loadEventDetail", runId: "prove-run", eventId: "../x" }).ok, false);
  assert.equal(parseWebviewMessage({ type: "loadEventDetail", runId: "", eventId: "abc" }).ok, false);
  assert.equal(parseWebviewMessage({ type: "loadEventDetail", runId: "prove-run" }).ok, false);
});
