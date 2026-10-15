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
import { formalizationFixture, sourceDraftFixture } from "./helpers/formalizationFixture.mjs";
import { formalizationConflictFixture } from "./helpers/formalizationConflictFixture.mjs";

test("source drafts reject proof or publication authority and unbound artifact bytes", () => {
  const snapshot = sourceDraftFixture();
  const parsed = parseFormalizationSnapshot(snapshot);
  assert.equal(parsed.can_prove, false);
  assert.ok(parsed.items.every(item => item.lean_refs.length === 0));
  for (const flag of ["accepted", "publication_authority", "proof_authority"]) {
    const changed = structuredClone(snapshot);
    changed.draft_preview[flag] = true;
    assert.throws(() => parseFormalizationSnapshot(changed), /invalid unaccepted draft/);
  }
  assert.throws(() => parseFormalizationSnapshot({ ...snapshot, can_prove: true }), /invalid unaccepted draft/);
  for (const changed of [{ base_revision_id: "other-revision" }, { model_report_artifact_id: "other-artifact" }]) {
    assert.throws(() => parseFormalizationSnapshot({ ...snapshot, draft_preview: { ...snapshot.draft_preview, ...changed } }), /invalid unaccepted draft/);
  }
  assert.equal(parseFormalizationSnapshot({ ...snapshot, draft_preview: { ...snapshot.draft_preview, preparation_id: "" } }).can_prove, false);
  for (const changed of [{ id: "other-artifact" }, { sha256: "0".repeat(64) }]) {
    const bad = structuredClone(snapshot);
    bad.draft_preview.artifacts = bad.draft_preview.artifacts.map(artifact => ({ ...artifact }));
    Object.assign(bad.draft_preview.artifacts[0], changed);
    assert.throws(() => parseFormalizationSnapshot(bad), /invalid unaccepted draft/);
  }
});

test("source draft intent is an explicit boolean formalize option before the trailing prompt", () => {
  const request = { kind: "formalize", target: "paper/main.tex", provider: "", model: "", agents: 1,
    noParallel: false, research: false, researchWorkers: null, cleanRoom: false, humanReview: false,
    sourceDraftOnly: true, axioms: "", additionalSkills: [], profile: "", prompt: "Preserve source", overrides: {} };
  assert.deepEqual(buildWorkflowArgs(request), ["formalize", "paper/main.tex", "--source-draft-only", "--prompt", "Preserve source"]);
  assert.equal(parseWebviewMessage({ type: "launch", request }).message.request.sourceDraftOnly, true);
  for (const value of ["true", 1, null]) assert.equal(parseWebviewMessage({ type: "launch", request: { ...request, sourceDraftOnly: value } }).ok, false);
  assert.equal(parseWebviewMessage({ type: "launch", request: { ...request, kind: "prove" } }).ok, false);
  assert.throws(() => buildWorkflowArgs({ ...request, kind: "prove" }), /requires formalize/);
});

test("coverage never equates an excluded claim, checked shell, or conditional construction with a reviewed proof", () => {
  const snapshot = parseFormalizationSnapshot(formalizationFixture());
  assert.deepEqual(formalizationCoverage(snapshot), { included: 2, reviewed: 1, excluded: 1, unresolved: 1 });
  assert.equal(snapshot.items[0].proof_status, "conditional");
  assert.equal(snapshot.items[1].elaboration_status, "checked");
  assert.throws(() => parseFormalizationSnapshot({ ...snapshot, schema_version: 2 }), /not supported/);
  assert.throws(() => parseFormalizationSnapshot({ ...snapshot, items: [snapshot.items[0], snapshot.items[0]] }), /invalid source inventory/);
  assert.throws(() => parseFormalizationSnapshot({ ...snapshot, preparation: { status: "prepared", current: "true" } }), /invalid preparation/);
  assert.throws(() => parseFormalizationSnapshot({ ...snapshot, acceptance_freshness: { status: "current", current: "true", reason: "" } }), /invalid preparation/);
});

test("saved lexical review display cannot masquerade as fresh acceptance", () => {
  const snapshot = formalizationFixture();
  snapshot.currentness = "historical";
  snapshot.can_prove = false;
  snapshot.handoff = null;
  snapshot.diagnostics = [{ id: "d-reviewed", code: "unresolved_command", message: "Original diagnostic",
    blocking: false, source_refs: [], can_resolve_command: false,
    native_interpretation_required: false, native_interpretation_status: "saved_reviewed" }];
  assert.equal(parseFormalizationSnapshot(snapshot).can_prove, false);
  for (const status of ["approved", true, 1]) {
    const bad = structuredClone(snapshot);
    bad.diagnostics[0].native_interpretation_status = status;
    assert.throws(() => parseFormalizationSnapshot(bad), /invalid extraction diagnostics/);
  }
  const conflicting = structuredClone(snapshot);
  conflicting.diagnostics[0].native_interpretation_required = true;
  assert.throws(() => parseFormalizationSnapshot(conflicting), /invalid extraction diagnostics/);
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

test("PDF replacements and shared/imported mapping repairs are typed and discard forged evidence", () => {
  const repair = action => parseWebviewMessage({ type: "formalizationAction", documentId: "paper-1", revisionId: "revision-1", action });
  const replacement = { type: "replace_page_transcription", page_index: 0, text: "Full page including every raster formula", reason: "Checked rendered page", verified: true };
  assert.deepEqual(repair({ ...replacement, approved: true }).message.action, replacement);
  assert.equal(repair({ ...replacement, verified: false }).ok, false);
  assert.deepEqual(repair({ type: "retry_extraction", reason: "Poppler repaired", approved: true }).message.action, { type: "retry_extraction", reason: "Poppler repaired" });
  assert.equal(repair({ type: "retry_extraction", reason: " " }).ok, false);
  const action = { type: "set_mapping", source_ids: ["item-1", "item-2"], unit_id: "shared-unit", lean_refs: [{ imports: ["Init"], name: "Nat.add_comm", declaration_origin: "dependency" }], rationale: "The two source claims reuse one compiled declaration" };
  assert.deepEqual(repair({ ...action, lean_refs: [{ ...action.lean_refs[0], type_hash: "forged", approved: true }] }).message.action, action);
  assert.equal(repair({ ...action, source_ids: ["item-1", "item-1"] }).ok, false);
  assert.equal(repair({ ...action, lean_refs: [{ imports: ["../escape"], name: "t" }] }).ok, false);
  assert.equal(repair({ ...action, lean_refs: [{ file: "Main.lean", name: "t", declaration_origin: "dependency" }] }).ok, false);
  assert.equal(repair({ ...action, lean_refs: [] }).ok, false);
});

test("conflict messages carry opaque observation identities and never accept paths or client hashes", () => {
  const resolved = parseWebviewMessage({ type: "resolveFormalizationConflict", transactionId: "transaction-1", observationId: "observation-1", reason: "Merged the concurrent save", destinations: { "/outside": "forged" }, choice: "overwrite" });
  assert.deepEqual(resolved.message, { type: "resolveFormalizationConflict", transactionId: "transaction-1", observationId: "observation-1", reason: "Merged the concurrent save" });
  assert.equal(parseWebviewMessage({ ...resolved.message, reason: " " }).ok, false);
  assert.equal(parseWebviewMessage({ type: "openFormalizationConflictVersion", transactionId: "transaction-1", observationId: "observation-1", versionId: "../outside" }).ok, false);
  const opened = parseWebviewMessage({ type: "openFormalizationConflictVersion", transactionId: "transaction-1", observationId: "observation-1", versionId: "version-retained", path: "/outside" });
  assert.equal("path" in opened.message, false);
});

test("conflict service reconstructs version pages and resolves only freshly observed maps", async () => {
  const calls = [];
  const fixture = formalizationConflictFixture();
  const service = new FormalizationService(() => "/project", async args => {
    calls.push(args);
    if (args[1] === "resolve-conflict") return JSON.stringify({ transaction_id: "transaction-1", requires_new_acceptance: true });
    if (args[1] === "conflict-artifact") return JSON.stringify({ id: "version-retained", path: "/controller/frozen/version.lean", sha256: "b".repeat(64), kind: "lean" });
    const value = structuredClone(fixture);
    if (args.includes("--version-offset")) {
      value.transactions[0].versions = value.transactions[0].versions.slice(1);
      value.transactions[0].version_offset = 1;
    } else {
      value.transactions[0].versions = value.transactions[0].versions.slice(0, 1);
      value.transactions[0].next_version_offset = 1;
    }
    return JSON.stringify(value);
  });
  assert.equal((await service.conflicts()).transactions[0].versions.length, 2);
  await service.resolveConflict("transaction-1", "observation-1", "Merged exact current files");
  const resolution = JSON.parse(calls.at(-1)[2].slice("--resolution-json=".length));
  assert.deepEqual(resolution, { transaction_id: "transaction-1", observation_id: "observation-1", destinations: fixture.transactions[0].destinations, retained_slots: fixture.transactions[0].retained_slots, choice: "keep_current", reason: "Merged exact current files" });
  const count = calls.filter(args => args[1] === "resolve-conflict").length;
  await assert.rejects(service.resolveConflict("transaction-1", "old-observation", "Old bytes"), /versions changed/);
  assert.equal(calls.filter(args => args[1] === "resolve-conflict").length, count);
  await service.conflictArtifact("transaction-1", "observation-1", "version-retained");
  assert.deepEqual(calls.at(-1), ["formalization", "conflict-artifact", "transaction-1", "version-retained", "--expected-observation", "observation-1", "--json"]);
});

test("conflict service retains unavailable history and rejects fabricated reconstructed versions", async () => {
  const fixture = formalizationConflictFixture();
  Object.assign(fixture.transactions[0], {
    unavailable_journal: true, planned_versions: null, versions: [], version_count: 0,
    revoked_revision_ids: ["revision-1"], unavailable_versions: { "blobs/missing": "a".repeat(64) },
  });
  const service = new FormalizationService(() => "/project", async () => JSON.stringify(fixture));
  const shown = (await service.conflicts()).transactions[0];
  assert.equal(shown.unavailable_journal, true);
  assert.deepEqual(shown.revoked_revision_ids, ["revision-1"]);
  fixture.transactions[0].versions = formalizationConflictFixture().transactions[0].versions;
  fixture.transactions[0].version_count = 2;
  await assert.rejects(service.conflicts(), /cannot contain reconstructed versions/);
});

test("source workspace planning and infrastructure resolution stay bound to controller identities", async () => {
  const calls = [];
  const transaction = { transaction_id: "prep-1", observation_id: "obs-1", history_id: "history-1", destinations: { ".lake": "a".repeat(64) }, retained_slots: { "retained/lake": "b".repeat(64) }, state: "CONFLICTED", active: true, conflicts: ["Changed infrastructure"], resolutions: [] };
  const service = new FormalizationService(() => "/source folder", async (args, options) => {
    calls.push({ args, options });
    if (args[1] === "plan") return JSON.stringify({ schema_version: 1, plan: { preset: "lean", required_imports: ["Init"] }, semantic_approval: false });
    return JSON.stringify({ schema_version: 1, transactions: [transaction] });
  });
  assert.equal((await service.preparationPlan("paper-1", "revision-1", "lean")).preset, "lean");
  assert.deepEqual(calls[0], { args: ["formalization", "plan", "paper-1", "--expected-revision", "revision-1", "--preset", "lean", "--json"], options: { cwd: "/source folder", timeoutMs: 60_000 } });
  const args = await service.preparationResolutionArgs("prep-1", "obs-1", "Checked retained dependency directories locally");
  const request = JSON.parse(args[2].slice("--resolution-json=".length));
  assert.deepEqual(request.observation, { transaction_id: "prep-1", observation_id: "obs-1", history_id: "history-1", destinations: transaction.destinations, retained_slots: transaction.retained_slots });
  await assert.rejects(service.preparationResolutionArgs("prep-1", "old-observation", "Stale inspection"), /versions changed/);
  assert.ok(calls.every(call => call.options.timeoutMs === 60_000));
});

test("the client passes exact revision and JSON as argv and never reads state itself", async () => {
  const calls = [];
  const service = new FormalizationService(() => "/owning/host/project", async (args, options) => {
    calls.push({ args, options });
    if (args[1] === "list") return JSON.stringify({ schema_version: 1, documents: [formalizationFixture()] });
    if (args[1] === "action") return JSON.stringify({ schema_version: 1, committed: true, document_id: "paper-1", previous_revision_id: "revision-1", revision_id: "revision-2", currentness: "unchecked" });
    return JSON.stringify(formalizationFixture());
  });
  await service.list(); await service.show("paper-1"); await service.inspect("paper/main.tex");
  assert.equal(calls[0].options.timeoutMs, 60_000);
  assert.equal(calls[1].options.timeoutMs, 60_000);
  await service.inspect("paper/main.tex", "paper/Rendered Copy.pdf");
  assert.deepEqual(calls.at(-1), { args: ["formalization", "inspect", "paper/main.tex", "--companion-pdf", "paper/Rendered Copy.pdf", "--json"], options: { cwd: "/owning/host/project", timeoutMs: 60_000 } });
  const action = { type: "set_scope", item_id: "item-1", scope: "excluded", reason: 'Keep literal $(no-command) and "quotes"' };
  await service.action("paper-1", "revision-1", action);
  assert.deepEqual(calls.at(-1), { args: ["formalization", "action", "paper-1", "--expected-revision", "revision-1", `--action-json=${JSON.stringify(action)}`, "--acknowledge-only", "--json"], options: { cwd: "/owning/host/project" } });
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

test("historical projections never authorize proving and currentness is revision bound", async () => {
  const historical = { ...formalizationFixture(), currentness: "historical", can_prove: false, handoff: null, acceptance_freshness: { current: false, status: "historical", reason: "Saved evidence only" } };
  assert.equal(parseFormalizationSnapshot(historical).can_prove, false);
  assert.throws(() => parseFormalizationSnapshot({ ...historical, can_prove: true }), /invalid preparation/);
  assert.throws(() => parseFormalizationSnapshot({ ...historical, handoff: formalizationFixture().handoff }), /invalid preparation/);
  const calls = [];
  const service = new FormalizationService(() => "/project", async (args, options) => { calls.push({ args, options }); return JSON.stringify({ ...formalizationFixture(), currentness: "checked" }); });
  assert.equal((await service.check("paper-1", "revision-1")).revision_id, "revision-1");
  assert.deepEqual(calls[0], { args: ["formalization", "check", "paper-1", "--expected-revision", "revision-1", "--json"], options: { cwd: "/project", timeoutMs: 180_000 } });
  await assert.rejects(service.check("paper-1", "changed-revision"), /another revision/);
  assert.equal(parseWebviewMessage({ type: "checkFormalization", documentId: "paper-1" }).ok, false);
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

test("conditional interpretation accepts checked evidence only and cannot select a branch", () => {
  const action = { type: "resolve_conditional", diagnostic_id: "diagnostic-1", interpretation: "Retain both alternatives for review", reason: "Checked original TeX and PDF", verified: true };
  const repair = candidate => parseWebviewMessage({ type: "formalizationAction", documentId: "paper-1", revisionId: "revision-1", action: candidate });
  assert.deepEqual(repair({ ...action, selected_branch: "then", excluded_regions: ["region"], reviewed: true }).message.action, action);
  for (const change of [{ verified: false }, { verified: "true" }, { reason: "" }, { interpretation: " " }]) assert.equal(repair({ ...action, ...change }).ok, false);
  const snapshot = formalizationFixture();
  snapshot.diagnostics.push({ id: "diagnostic-1", code: "unresolved_conditional", message: "Unknown conditional", blocking: true, can_resolve_command: false, can_resolve_conditional: true, source_refs: [] });
  assert.equal(parseFormalizationSnapshot(snapshot).diagnostics[1].can_resolve_conditional, true);
  snapshot.diagnostics[1].can_resolve_conditional = "true";
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
