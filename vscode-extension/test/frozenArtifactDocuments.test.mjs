/** Keep virtual frozen text named, immutable, bounded and independent of filesystem paths. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";

const bundle = await build({
  stdin: { contents: 'export * from "./src/panels/frozenArtifactDocuments";', resolveDir: process.cwd(), loader: "ts" },
  bundle: true, write: false, format: "esm", platform: "node",
  plugins: [{ name: "frozen-document-editor", setup(builder) {
    builder.onResolve({ filter: /^vscode$/ }, () => ({ path: "vscode", namespace: "fixture" }));
    builder.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({ contents: `
      export const Uri = { from(parts) { return { ...parts, toString() { return this.scheme + '://' + this.authority + this.path; } }; } };
      export const workspace = { registerTextDocumentContentProvider(scheme, provider) {
        const f = globalThis.frozenDocumentFixture;
        if (f.failRegistration) { f.failRegistration = false; throw new Error('Registration failed'); }
        f.providers.set(scheme, provider);
        return { dispose() { f.providers.delete(scheme); } };
      } };
    ` }));
  } }],
});
const { FrozenArtifactDocuments, MAX_FROZEN_DOCUMENTS, MAX_FROZEN_TEXT_BYTES } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);
const identity = { scope: { id: "host", generation: 1, root: "/private/paper" }, documentId: "doc", revisionId: "rev", artifactId: "source", name: "paper.tex" };
function fixture() { globalThis.frozenDocumentFixture = { providers: new Map() }; return new FrozenArtifactDocuments(); }

test("frozen source uses a named content-provider URI and preserves exact UTF-8 text", () => {
  const provider = fixture();
  try {
    const text = "\ufeff\\def\\lncs{0}\nTheorem αβ.\n";
    const uri = provider.uriFor(identity, Buffer.from(text));
    assert.match(uri.scheme, /^leanflow-frozen-/);
    assert.equal(uri.authority, "frozen");
    assert.ok(uri.path.endsWith("/paper.tex"));
    assert.ok(!uri.toString().includes(identity.scope.root));
    assert.equal(globalThis.frozenDocumentFixture.providers.get(uri.scheme), provider);
    assert.equal(provider.provideTextDocumentContent(uri), text);
    assert.equal(provider.uriFor(identity, Buffer.from(text)).toString(), uri.toString());
  } finally { provider.dispose(); }
});

test("revision, workspace, artifact and content changes cannot alias another frozen document", () => {
  const provider = fixture();
  try {
    const original = provider.uriFor(identity, Buffer.from("original"));
    const variants = [
      [{ ...identity, revisionId: "next" }, "original"],
      [{ ...identity, scope: { ...identity.scope, generation: 2 } }, "original"],
      [{ ...identity, scope: { ...identity.scope, root: "/another/paper" } }, "original"],
      [{ ...identity, artifactId: "other" }, "original"],
      [identity, "different"],
    ];
    for (const [metadata, text] of variants) assert.notEqual(provider.uriFor(metadata, Buffer.from(text)).toString(), original.toString());
    assert.equal(provider.provideTextDocumentContent(original), "original");
    assert.throws(() => provider.provideTextDocumentContent({ ...original, toString: () => original.toString() + "/../../source" }), /no longer retained/);
    assert.throws(() => provider.provideTextDocumentContent({ ...original, scheme: "file" }), /no longer retained/);
    for (const name of ["../paper.tex", "a/b.tex", "a\\b.tex", ".", ""]) assert.throws(() => provider.uriFor({ ...identity, name }, Buffer.from("x")), /display name/);
  } finally { provider.dispose(); }
});

test("provider rejects invalid text and registration failure does not strand a later retry", () => {
  const provider = fixture();
  try {
    for (const bytes of [Buffer.from([0xff]), Buffer.from("a\0b")]) assert.throws(() => provider.uriFor(identity, bytes), /not UTF-8 source/);
    globalThis.frozenDocumentFixture.failRegistration = true;
    assert.throws(() => provider.uriFor(identity, Buffer.from("valid")), /Registration failed/);
    const uri = provider.uriFor(identity, Buffer.from("valid"));
    assert.equal(provider.provideTextDocumentContent(uri), "valid");
  } finally { provider.dispose(); }
});

test("entry count eviction is bounded and refreshing an identity retains that entry", () => {
  const provider = fixture();
  try {
    const uris = Array.from({ length: MAX_FROZEN_DOCUMENTS }, (_, index) => provider.uriFor({ ...identity, artifactId: String(index) }, Buffer.from(String(index))));
    provider.uriFor({ ...identity, artifactId: "0" }, Buffer.from("0"));
    provider.uriFor({ ...identity, artifactId: "overflow" }, Buffer.from("x"));
    assert.equal(provider.provideTextDocumentContent(uris[0]), "0");
    assert.throws(() => provider.provideTextDocumentContent(uris[1]), /no longer retained/);
  } finally { provider.dispose(); }
});

test("aggregate text bytes evict old entries and a single oversized artifact is rejected", () => {
  const provider = fixture();
  try {
    const bytes = Buffer.alloc(Math.floor(MAX_FROZEN_TEXT_BYTES / 2) + 1, "a");
    const first = provider.uriFor(identity, bytes);
    const second = provider.uriFor({ ...identity, artifactId: "second" }, bytes);
    assert.throws(() => provider.provideTextDocumentContent(first), /no longer retained/);
    assert.equal(provider.provideTextDocumentContent(second).length, bytes.length);
    assert.throws(() => provider.uriFor(identity, Buffer.alloc(MAX_FROZEN_TEXT_BYTES + 1)), /64 MiB/);
  } finally { provider.dispose(); }
});

test("separate viewers have distinct registrations and disposal removes all owned text", () => {
  const first = fixture();
  const second = new FrozenArtifactDocuments();
  try {
    const a = first.uriFor(identity, Buffer.from("A"));
    const b = second.uriFor(identity, Buffer.from("B"));
    assert.notEqual(a.scheme, b.scheme);
    assert.equal(globalThis.frozenDocumentFixture.providers.size, 2);
    first.dispose();
    assert.equal(globalThis.frozenDocumentFixture.providers.size, 1);
    assert.throws(() => first.provideTextDocumentContent(a), /no longer retained/);
    assert.throws(() => first.uriFor(identity, Buffer.from("x")), /viewer was closed/);
    assert.equal(second.provideTextDocumentContent(b), "B");
  } finally { first.dispose(); second.dispose(); }
  assert.equal(globalThis.frozenDocumentFixture.providers.size, 0);
});
