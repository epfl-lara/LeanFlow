import assert from "node:assert/strict";
import test from "node:test";
import { benchmarkCost, benchmarkProject, benchmarkEscape, benchmarkSummary } from "../dist/test/benchmarkCampaign.mjs";

test("benchmark navigation is confined to the exact private cell", () => {
  assert.equal(benchmarkProject("/campaign", {id:"p-astra",project:"/campaign/cells/p-astra"}), "/campaign/cells/p-astra");
  assert.throws(() => benchmarkProject("/campaign", {id:"../other",project:"/campaign/other"}));
  assert.throws(() => benchmarkProject("/campaign", {id:"p-astra",project:"/source-project"}));
});

test("status and model text are escaped", () => {
  assert.equal(benchmarkEscape('<script>"&'), '&lt;script&gt;&quot;&amp;');
});

test("summary distinguishes active cells from verified results", () => {
  const summary = benchmarkSummary({cells:[
    {status:"running",verified:false,metrics:{api_calls:3,input_tokens:100,output_tokens:20}},
    {status:"completed",verified:true,metrics:{api_calls:4,input_tokens:200,output_tokens:30}},
    {status:"completed",verified:false,metrics:{}},
  ]});
  assert.deepEqual(summary,{verified:1,active:1,calls:7,tokens:350});
});


test("costs require provenance and retain partial coverage labels", () => {
  assert.equal(benchmarkCost({cost_usd:152.81,cost_complete:true}), "unavailable");
  assert.equal(benchmarkCost({cost_usd:0.5,cost_source:"provider_reported",cost_complete:false}), "$0.500 · reported · partial");
  assert.equal(benchmarkCost({cost_usd:0,cost_source:"provider_reported",cost_complete:true}), "$0.000 · reported");
  assert.equal(benchmarkCost({cost_usd:1,cost_source:"estimated",cost_complete:true}), "$1.000 · estimated");
});
