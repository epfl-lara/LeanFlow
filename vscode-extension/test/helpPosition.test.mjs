/** Keep contextual help inside the viewport at panel edges and narrow widths. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";
const result = await build({ entryPoints: ["webview/helpPosition.ts"], bundle: true, write: false, format: "esm", platform: "node" });
const { helpPosition } = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString("base64")}`);
test("help at left, right and bottom edges remains completely visible", () => {
  for (const width of [280,350,800]) for (const left of [0,15,width-20]) for (const top of [0,150,580]) {
    const p = helpPosition({left,top,bottom:top+20},180,width,620);
    assert.ok(p.left >= 12);
    assert.ok(p.left + p.width <= width - 12);
    assert.ok(p.top >= 12);
    assert.ok(p.top + 180 <= 608);
  }
});
test("long explanations get a scrollable height and bottom anchors place help above", () => {
  assert.deepEqual(helpPosition({left:30,top:550,bottom:570},180,800,600),{width:320,maxHeight:576,left:30,top:362});
  const p=helpPosition({left:30,top:250,bottom:270},1000,300,400);
  assert.equal(p.maxHeight,376); assert.equal(p.top,12); assert.equal(p.width,276);
});
