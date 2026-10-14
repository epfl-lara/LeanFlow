/** Owned preparation avoids short read timeouts and cleans up on explicit cancellation. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, writeFile, readFile, rm } from "node:fs/promises";
import path from "node:path";
import { tmpdir } from "node:os";
import { build } from "esbuild";
import { formalizationFixture } from "./helpers/formalizationFixture.mjs";

const bundle = await build({ stdin: { contents: 'export { startFormalizationPreparation } from "./src/core/formalizationPreparationProcess";', resolveDir: process.cwd(), loader: "ts" }, bundle: true, write: false, format: "esm", platform: "node" });
const { startFormalizationPreparation } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);

test("preparation streams progress and returns the controller snapshot without a read-check timeout", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "formalization-prep-process-"));
  try {
    const source = path.join(root, "prepare.mjs");
    const snapshot = { ...formalizationFixture(), preparation_result: { accepted: true, status: "prepared" } };
    await writeFile(source, `process.stderr.write(JSON.stringify({phase:'dependencies',message:'Preparing pinned imports'})+'\\n');setTimeout(()=>process.stdout.write(${JSON.stringify(JSON.stringify(snapshot))}),150);`);
    const events = [];
    const process = startFormalizationPreparation(globalThis.process.execPath, [source], root, (...event) => events.push(event));
    assert.equal((await process.result).document_id, "paper-1");
    assert.deepEqual(events, [["dependencies", "Preparing pinned imports"]]);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("cancellation reaches the owned controller and does not report preparation readiness", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "formalization-prep-cancel-"));
  try {
    const source = path.join(root, "prepare.mjs");
    const marker = path.join(root, "cancelled.txt");
    await writeFile(source, `import{writeFileSync}from'node:fs';process.on('SIGINT',()=>{writeFileSync(${JSON.stringify(marker)},'cleaned');process.exit(0)});process.stderr.write('ready\\n');setInterval(()=>{},100);`);
    let ready;
    const started = new Promise(resolve => { ready = resolve; });
    const process = startFormalizationPreparation(globalThis.process.execPath, [source], root, () => ready());
    await started;
    process.cancel();
    await assert.rejects(process.result, /preparation cancelled/);
    assert.equal(await readFile(marker, "utf8"), "cleaned");
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("cooperative cancellation waits beyond five seconds and retains the final rollback receipt", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "formalization-prep-safe-cancel-"));
  try {
    const source = path.join(root, "prepare.mjs");
    const marker = path.join(root, "rollback.txt");
    const snapshot = { ...formalizationFixture(), preparation_result: { accepted: false, status: "cancelled" } };
    await writeFile(source, `import{writeFileSync}from'node:fs';let interrupts=0;process.on('SIGINT',()=>{interrupts++;setTimeout(()=>{writeFileSync(${JSON.stringify(marker)},String(interrupts));process.stdout.write(${JSON.stringify(JSON.stringify(snapshot))});process.exit(0)},5500)});process.stderr.write('ready\\n');setInterval(()=>{},100);`);
    let ready;
    const started = new Promise(resolve => { ready = resolve; });
    const preparation = startFormalizationPreparation(process.execPath, [source], root, () => ready());
    await started;
    preparation.cancel(); preparation.cancel();
    const result = await preparation.result;
    assert.equal(result.preparation_result.status, "cancelled");
    assert.equal(result.preparation_result.accepted, false);
    assert.equal(await readFile(marker, "utf8"), "1");
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("a cancellation arriving after commit does not discard the controller's accepted receipt", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "formalization-prep-commit-cancel-"));
  try {
    const source = path.join(root, "prepare.mjs");
    const snapshot = { ...formalizationFixture(), preparation_result: { accepted: true, status: "prepared" } };
    await writeFile(source, `process.on('SIGINT',()=>{process.stdout.write(${JSON.stringify(JSON.stringify(snapshot))});process.exit(0)});process.stderr.write('committed\\n');setInterval(()=>{},100);`);
    let committed;
    const started = new Promise(resolve => { committed = resolve; });
    const preparation = startFormalizationPreparation(process.execPath, [source], root, () => committed());
    await started; preparation.cancel();
    assert.equal((await preparation.result).preparation_result.accepted, true);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("bounded stdout refuses oversized preparation results instead of truncating an inventory", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "formalization-prep-bound-"));
  try {
    const source = path.join(root, "prepare.mjs");
    await writeFile(source, `process.stdout.write('x'.repeat(9*1024*1024));setInterval(()=>{},100);`);
    const process = startFormalizationPreparation(globalThis.process.execPath, [source], root, () => {});
    await assert.rejects(process.result, /bounded editor output budget/);
  } finally { await rm(root, { recursive: true, force: true }); }
});
