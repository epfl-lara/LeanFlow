// Two bundles from one script: the extension host (Node CJS, vscode external)
// and the webview UI (browser IIFE, React inlined). Keeping both here means the
// build stays a single dependency and one `npm run build`.
import esbuild from "esbuild";

const production = process.argv.includes("--production");
const watch = process.argv.includes("--watch");
const tests = process.argv.includes("--tests");

/** @type {import("esbuild").BuildOptions} */
const shared = {
  bundle: true,
  minify: production,
  sourcemap: production ? false : "inline",
  logLevel: "info",
  define: {
    "process.env.NODE_ENV": JSON.stringify(production ? "production" : "development"),
  },
};

/** @type {import("esbuild").BuildOptions} */
const hostConfig = {
  ...shared,
  entryPoints: ["src/extension.ts"],
  outfile: "dist/extension.js",
  format: "cjs",
  platform: "node",
  target: "node18",
  // The vscode module is provided by the extension host at runtime and must
  // never be bundled.
  external: ["vscode"],
};

/** @type {import("esbuild").BuildOptions} */
const webviewConfig = {
  ...shared,
  entryPoints: ["webview/index.tsx"],
  outfile: "dist/webview.js",
  format: "iife",
  platform: "browser",
  target: "es2020",
  jsx: "automatic",
  loader: { ".css": "css" },
};

if (tests) {
  await esbuild.build({
    bundle: true,
    logLevel: "info",
    format: "esm",
    platform: "node",
    target: "node18",
    external: ["vscode"],
    entryPoints: {
      launch: "src/core/launch.ts",
      launchPaths: "src/core/launchPaths.ts",
      projectDiscovery: "src/core/projectDiscovery.ts",
      eventBuffer: "src/core/eventBuffer.ts",
      experimentIsolation: "src/core/experimentIsolation.ts",
      experimentLaunchContract: "src/core/experimentLaunchContract.ts",
      experimentMatrix: "src/core/experimentMatrix.ts",
      experimentScoring: "src/core/experimentScoring.ts",
      experimentSecrets: "src/core/experimentSecrets.ts",
      experimentValidity: "src/core/experimentValidity.ts",
      stats: "webview/stats.ts",
      storageSecurity: "src/core/storageSecurity.ts",
      runOwnership: "src/core/runOwnership.ts",
      runSelection: "src/core/runSelection.ts",
      runHistory: "src/core/runHistory.ts",
      runPrivacy: "src/core/runPrivacy.ts",
      messageSchema: "src/panels/messageSchema.ts",
      prover: "src/core/prover.ts",
      proverGraph: "src/core/proverGraph.ts",
    },
    outdir: "dist/test",
    outExtension: { ".js": ".mjs" },
  });
} else if (watch) {
  const contexts = await Promise.all([
    esbuild.context(hostConfig),
    esbuild.context(webviewConfig),
  ]);
  await Promise.all(contexts.map((context) => context.watch()));
  console.log("[leanflow] watching for changes…");
} else {
  await Promise.all([esbuild.build(hostConfig), esbuild.build(webviewConfig)]);
}
