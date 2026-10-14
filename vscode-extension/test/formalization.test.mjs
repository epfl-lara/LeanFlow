/** Inventory semantics, typed repair boundaries, controller transport and frozen source safety. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { createHash } from "node:crypto";
import { mkdtemp, writeFile, symlink, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { parseFormalizationSnapshot, pdfItemCandidates, formalizationCoverage, proofAuthorizationLabel } from "../dist/test/formalization.mjs";
import { FormalizationService } from "../dist/test/formalizationService.mjs";
import { readFormalizationArtifact } from "../dist/test/formalizationArtifact.mjs";
import { parseWebviewMessage } from "../dist/test/messageSchema.mjs";
import { buildWorkflowArgs } from "../dist/test/launch.mjs";
import { formalizationFixture } from "./helpers/formalizationFixture.mjs";

test("coverage never equates an excluded claim, checked shell, or conditional construction with a reviewed proof", () => {
  const snapshot = parseFormalizationSnapshot(formalizationFixture());
  assert.deepEqual(formalizationCoverage(snapshot), { included: 2, reviewed: 1, excluded: 1, unresolved: 1 });
  assert.equal(snapshot.items[0].proof_status, "conditional");
  assert.equal(snapshot.items[1].elaboration_status, "checked");
  assert.throws(() => parseFormalizationSnapshot({ ...snapshot, schema_version: 2 }), /not supported/);
  assert.throws(() => parseFormalizationSnapshot({ ...snapshot, items: [snapshot.items[0], snapshot.items[0]] }), /invalid source inventory/);
});

test("PDF navigation uses physical pages, document-bound artifacts and approximate text candidates", () => {
  const items = formalizationFixture().items;
  assert.deepEqual(pdfItemCandidates(items, "source-pdf", 0).map(item => item.id), ["item-1"]);
  assert.deepEqual(pdfItemCandidates(items, "source-pdf", 1, "even number").map(item => item.id), ["item-2"]);
  assert.deepEqual(pdfItemCandidates(items, "other-pdf", 1), []);
  assert.deepEqual(pdfItemCandidates(items, "source-pdf", 1, "absent mathematical qualifier"), []);
});

test("repairs reject arbitrary paths, forged verdicts and missing revision expectations", () => {
  const repair = action => parseWebviewMessage({ type: "formalizationAction", documentId: "paper-1", revisionId: "revision-1", action });
  assert.equal(repair({ type: "approve", item_id: "item-1" }).ok, false);
  assert.equal(repair({ type: "set_scope", item_id: "item-1", scope: "excluded", reason: "" }).ok, false);
  assert.equal(repair({ type: "classify_region", region_id: "region-1", classification: "covered", reason: "Looks good" }).ok, false);
  assert.equal(repair({ type: "set_mapping", item_id: "item-1", lean_refs: [{ file: "../outside.lean", name: "Proof" }], rationale: "Map" }).ok, false);
  const mapped = repair({ type: "update_boundary", item_id: "item-1", source_refs: [{ artifact_id: "source-pdf", page_index: 0, text: "statement", file: "/secret", precision: "exact" }] });
  assert.equal(mapped.ok, true);
  assert.deepEqual(mapped.message.action.source_refs, [{ artifact_id: "source-pdf", page_index: 0, text: "statement" }]);
  assert.equal(parseWebviewMessage({ type: "formalizationAction", documentId: "paper-1", action: { type: "select_revision", revision_id: "old" } }).ok, false);
  assert.equal(parseWebviewMessage({ type: "openFormalizationArtifact", documentId: "../paper", revisionId: "revision-1", artifactId: "source-pdf" }).ok, false);
});

test("the client passes exact revision and JSON as argv and never reads state itself", async () => {
  const calls = [];
  const service = new FormalizationService(() => "/owning/host/project", async (args, options) => {
    calls.push({ args, options });
    if (args[1] === "list") return JSON.stringify({ schema_version: 1, documents: [formalizationFixture()] });
    return JSON.stringify(formalizationFixture());
  });
  await service.list(); await service.show("paper-1"); await service.inspect("paper/main.tex");
  await service.inspect("paper/main.tex", "paper/Rendered Copy.pdf");
  assert.deepEqual(calls.at(-1), { args: ["formalization", "inspect", "paper/main.tex", "--companion-pdf", "paper/Rendered Copy.pdf", "--json"], options: { cwd: "/owning/host/project" } });
  const action = { type: "set_scope", item_id: "item-1", scope: "excluded", reason: 'Keep literal $(no-command) and "quotes"' };
  await service.action("paper-1", "revision-1", action);
  assert.deepEqual(calls.at(-1), { args: ["formalization", "action", "paper-1", "--expected-revision", "revision-1", `--action-json=${JSON.stringify(action)}`, "--json"], options: { cwd: "/owning/host/project" } });
  const bad = new FormalizationService(() => "/project", async () => JSON.stringify({ ...formalizationFixture(), document_id: "other" }));
  await assert.rejects(bad.show("paper-1"), /different document/);
});

test("proof permission requires an explicit boolean and reason without conferring source review", () => {
  const repair = action => parseWebviewMessage({ type: "formalizationAction", documentId: "paper-1", revisionId: "revision-1", action });
  for (const authorized of [true, false]) {
    const action = { type: "authorize_proof", item_id: "item-3", authorized, reason: "User-selected research scope" };
    assert.deepEqual(repair(action).message.action, action);
    assert.equal(repair({ ...action, reason: "  " }).ok, false);
  }
  for (const authorized of [1, "true", null, undefined]) {
    assert.equal(repair({ type: "authorize_proof", item_id: "item-3", authorized, reason: "User scope" }).ok, false);
  }
  const snapshot = formalizationFixture();
  const item = snapshot.items[2];
  assert.equal(proofAuthorizationLabel(item), "No explicit permission");
  item.proof_authorization = { actor: "user", authorized: true, reason: "Explore this question" };
  assert.equal(proofAuthorizationLabel(parseFormalizationSnapshot(snapshot).items[2]), "Explicitly allowed");
  assert.equal(item.claim_status, "open");
  assert.equal(item.review_status, "unreviewed");
  assert.equal(item.proof_status, "unknown");
  item.proof_authorization.authorized = "true";
  assert.throws(() => parseFormalizationSnapshot(snapshot), /invalid source inventory/);
});

test("command reconciliation requires verified meaning and evidence without accepting source or review claims", () => {
  const repair = action => parseWebviewMessage({ type: "formalizationAction", documentId: "paper-1", revisionId: "revision-1", action });
  const action = { type: "resolve_command", diagnostic_id: "diagnostic-1", interpretation: "A declared operation applied to the following argument", reason: "Checked both rendered occurrences", verified: true };
  assert.deepEqual(repair({ ...action, blocking: false, verdict: "PASS", source_refs: [{ file: "/outside" }] }).message.action, action);
  for (const changed of [{ interpretation: " " }, { reason: "" }, { verified: "true" }, { verified: false }, { diagnostic_id: "../outside" }]) {
    assert.equal(repair({ ...action, ...changed }).ok, false);
  }
  const snapshot = formalizationFixture();
  snapshot.diagnostics.push({ id: "diagnostic-1", code: "unresolved_command", message: "Unknown command", blocking: true, can_resolve_command: true, source_refs: [] });
  assert.equal(parseFormalizationSnapshot(snapshot).diagnostics.length, 2);
  snapshot.diagnostics[1].resolution = { actor: "model", interpretation: "approved", reason: "guess", verified: true };
  assert.throws(() => parseFormalizationSnapshot(snapshot), /invalid extraction diagnostics/);
  snapshot.diagnostics[1].code = "pdf_text_missing";
  snapshot.diagnostics[1].can_resolve_command = false;
  for (const resolution of ["verified_transcription", "explicit_scope_exclusion"]) {
    snapshot.diagnostics[1].resolution = resolution;
    assert.equal(parseFormalizationSnapshot(snapshot).diagnostics[1].resolution, resolution);
  }
  snapshot.diagnostics[1].resolution = "silently_ignore";
  assert.throws(() => parseFormalizationSnapshot(snapshot), /invalid extraction diagnostics/);
});

test("artifact reads preserve exact frozen bytes and reject drift and symlink leaves", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "formalization-artifact-"));
  try {
    const target = path.join(root, "paper.tex");
    const content = "Frozen mathematical statement\n";
    const sha256 = createHash("sha256").update(content).digest("hex");
    await writeFile(target, content);
    const artifact = { id: "source", kind: "tex", path: target, sha256 };
    assert.equal((await readFormalizationArtifact(artifact)).toString(), content);
    await writeFile(target, "Changed bytes");
    await assert.rejects(readFormalizationArtifact(artifact), /stale/);
    await symlink(target, path.join(root, "link.tex"));
    await assert.rejects(readFormalizationArtifact({ ...artifact, path: path.join(root, "link.tex") }));
    await assert.rejects(readFormalizationArtifact({ ...artifact, path: "relative.tex" }), /owning host/);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("formalization prover launch carries exact document and revision, never an aggregator target", () => {
  const request = { kind: "prove", target: "", provider: "", model: "", agents: 1, noParallel: false, research: false, cleanRoom: false, humanReview: false, axioms: "", additionalSkills: [], prompt: "", overrides: {}, formalization: { documentId: "paper-1", revisionId: "revision-1" } };
  assert.deepEqual(buildWorkflowArgs(request), ["prove", "--from-formalization", "paper-1", "--formalization-revision", "revision-1"]);
  assert.throws(() => buildWorkflowArgs({ ...request, target: "Main.lean" }), /exact accepted scope/);
});
