import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { loaderNodes, applyResolutions, resolveGraph, wrapLoadGraphData, ROUTE } from "./oa_model_files_graph.mjs";

// The ComfyUI v0.45.2 templates' loader nodes (adopters/comfyui/testdata),
// and answers in the shape oa_model_files.py's route gives.
const fixtureUrl = name => new URL(`../../testdata/comfyui-0.45.2-${name}-loaders.json`, import.meta.url);
const fixture = name => JSON.parse(readFileSync(fixtureUrl(name), "utf-8"));
const LTX = "8b4f085c-1bb3-4ecd-aeed-603a8d6d3970";
const INT8 = "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors";
const Q8 = "ltx-2.5-22b-distilled-transformer-Q8_0.gguf";
const UPSCALER = "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors";
// Swap pairs as a table listing them would answer. The shipped table lists
// none: Step 1 never targets a custom node's loader.
const SWAPS = [{ from: "UNETLoader", to: "UnetLoaderGGUF" }, { from: "CLIPLoader", to: "CLIPLoaderGGUF" }];
const everyType = () => true;
const node = (graph, scope, id) => (scope === "root" ? graph.nodes
  : graph.definitions.subgraphs.find(s => s.id === scope).nodes).find(n => n.id === id);

const upscaler = { key: `${LTX}:371`, from_oa: true, file: UPSCALER, type: "LatentUpscaleModelLoader",
  widgets_values: [UPSCALER], title: "file from OA: LTX-2.5 latent upscaler x2 bf16", inputs: ["model_name"], file_widget: "model_name" };
const transformer = { key: `${LTX}:384`, from_oa: true, file: Q8, type: "UnetLoaderGGUF", widgets_values: [Q8],
  title: "file from OA: LTX-2.5 transformer Q8_0", inputs: ["unet_name"], file_widget: "unet_name" };

test("the loader nodes of the root and its subgraphs are sent with their files", () => {
  const nodes = loaderNodes(fixture("video_ltx2_5_t2v"));
  assert.deepEqual(nodes.map(n => n.key), [371, 384, 385, 386, 387, 393].map(id => `${LTX}:${id}`));
  assert.equal(nodes[1].value, INT8);
  assert.equal(nodes[1].models[0].directory, "diffusion_models");
  assert.deepEqual(loaderNodes(fixture("video_wan2_2_5B_ti2v")).map(n => [n.key, n.type]),
    [["root:37", "UNETLoader"], ["root:38", "CLIPLoader"], ["root:39", "VAELoader"]]);
});

test("OA's file with the same name keeps the file and adds the title line", () => {
  const graph = fixture("video_ltx2_5_t2v");
  const { graph: out, applied } = applyResolutions(graph, { resolutions: [upscaler], swaps: SWAPS }, everyType);
  assert.equal(applied.length, 1);
  const loader = node(out, LTX, 371);
  assert.equal(loader.title, "LatentUpscaleModelLoader · file from OA: LTX-2.5 latent upscaler x2 bf16");
  assert.deepEqual(loader.widgets_values, [UPSCALER]);
  assert.equal(loader.properties.models.length, 1);
  const again = applyResolutions(out, { resolutions: [upscaler], swaps: SWAPS }, everyType).graph;
  assert.equal(node(again, LTX, 371).title, loader.title);
});

test("a listed swap changes the loader class and the subgraph node's promoted file; the template stays", () => {
  const graph = fixture("video_ltx2_5_t2v");
  const before = JSON.stringify(graph);
  const { graph: out, applied } = applyResolutions(graph, { resolutions: [transformer], swaps: SWAPS }, everyType);
  assert.equal(JSON.stringify(graph), before);
  assert.deepEqual(applied, [{ key: `${LTX}:384`, file: Q8, type: "UnetLoaderGGUF" }]);
  const loader = node(out, LTX, 384);
  assert.equal(loader.type, "UnetLoaderGGUF");
  assert.equal(loader.properties["Node name for S&R"], "UnetLoaderGGUF");
  assert.deepEqual(loader.widgets_values, [Q8]);
  assert.deepEqual(loader.inputs.map(i => [i.name, i.link]), [["unet_name", 786]]);
  assert.deepEqual(loader.properties.models, []);
  const outer = node(out, "root", 405).widgets_values;
  assert.equal(outer[7], Q8);
  assert.equal(outer[8], "ltx-2.5-video-vae-bf16.safetensors");
});

test("a swap the answer does not list, or to a class this ComfyUI lacks, is ignored", () => {
  const graph = fixture("video_ltx2_5_t2v");
  for (const [answer, hasType] of [
    [{ resolutions: [transformer], swaps: [] }, everyType],
    [{ resolutions: [transformer], swaps: [{ from: "VAELoader", to: "UnetLoaderGGUF" }] }, everyType],
    [{ resolutions: [transformer], swaps: SWAPS }, name => name !== "UnetLoaderGGUF"],
    [{ resolutions: [{ ...transformer, type: "SomethingElse" }], swaps: [...SWAPS, { from: "UNETLoader", to: "SomethingElse" }] },
      name => name !== "SomethingElse"],
  ]) {
    const result = applyResolutions(graph, answer, hasType);
    assert.equal(result.graph, graph);
    assert.deepEqual(result.applied, []);
  }
});

test("malformed resolutions are ignored", () => {
  const graph = fixture("video_ltx2_5_t2v");
  for (const bad of [
    { ...upscaler, from_oa: false },
    { ...upscaler, file: "../../secret.safetensors", widgets_values: ["../../secret.safetensors"] },
    { ...upscaler, title: "something else" },
    { ...upscaler, widgets_values: [{ path: "x" }] },
    { ...upscaler, widgets_values: ["other.safetensors"] },
    { ...upscaler, key: "root:999" },
  ]) assert.equal(applyResolutions(graph, { resolutions: [bad], swaps: SWAPS }, everyType).graph, graph);
  assert.equal(applyResolutions(graph, null, everyType).graph, graph);
});

test("a swap that would drop a linked input is skipped", () => {
  const graph = fixture("video_wan2_2_5B_ti2v");
  graph.nodes.find(n => n.id === 38).inputs = [{ name: "device", type: "COMBO", widget: { name: "device" }, link: 7 }];
  const encoder = { key: "root:38", from_oa: true, file: "umt5-xxl-encoder-Q8_0.gguf", type: "CLIPLoaderGGUF",
    widgets_values: ["umt5-xxl-encoder-Q8_0.gguf", "wan"], title: "file from OA: umt5-xxl encoder Q8_0",
    inputs: ["clip_name", "type"], file_widget: "clip_name" };
  assert.equal(applyResolutions(graph, { resolutions: [encoder], swaps: SWAPS }, everyType).graph, graph);
  graph.nodes.find(n => n.id === 38).inputs = [];
  const out = applyResolutions(graph, { resolutions: [encoder], swaps: SWAPS }, everyType).graph;
  assert.deepEqual(node(out, "root", 38).widgets_values, ["umt5-xxl-encoder-Q8_0.gguf", "wan"]);
});

test("the route is asked once and its answer applied", async () => {
  const graph = fixture("video_ltx2_5_t2v");
  const calls = [];
  const fetchApi = async (route, init) => {
    calls.push([route, JSON.parse(init.body)]);
    return { ok: true, json: async () => ({ resolutions: [upscaler, transformer], swaps: SWAPS }) };
  };
  const quiet = { info() {}, warn() {} };
  const out = await resolveGraph(graph, fetchApi, everyType, quiet);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], ROUTE);
  assert.equal(calls[0][1].nodes.length, 6);
  assert.equal(node(out, LTX, 384).type, "UnetLoaderGGUF");
});

test("with OA unreachable or the route failing the workflow loads as authored", async () => {
  const graph = fixture("video_ltx2_5_t2v");
  const warnings = [];
  const log = { info() {}, warn: line => warnings.push(line) };
  const failures = [
    async () => { throw new TypeError("Failed to fetch"); },
    async () => ({ ok: false, status: 404, json: async () => ({}) }),
    async () => ({ ok: true, json: async () => { throw new SyntaxError("not JSON"); } }),
    async () => ({ ok: true, json: async () => ({ resolutions: "nonsense" }) }),
  ];
  for (const fetchApi of failures) assert.equal(await resolveGraph(graph, fetchApi, everyType, log), graph);
  assert.equal(warnings.length, 3);
  const empty = { nodes: [] };
  assert.equal(await resolveGraph(empty, () => assert.fail("no loaders, no request"), everyType, log), empty);
});

test("the wrapped loadGraphData opens a rewritten copy; the source workflow and its file stay as stored", async () => {
  const file = readFileSync(fixtureUrl("video_ltx2_5_t2v"));
  const graph = JSON.parse(file.toString("utf-8"));
  const before = JSON.stringify(graph);
  const loads = [];
  let asked = 0;
  const original = async function (data, ...rest) { loads.push([this, data, rest]); return "loaded"; };
  const fetchApi = async () => {
    asked += 1;
    return { ok: true, json: async () => ({ resolutions: [upscaler], swaps: [] }) };
  };
  const quiet = { info() {}, warn() {} };
  const wrapped = wrapLoadGraphData(original, fetchApi, everyType, quiet);
  const app = { loadGraphData: wrapped };
  assert.equal(await app.loadGraphData(graph, true, true, null, {}), "loaded");
  const [self, data, rest] = loads[0];
  assert.equal(self, app);
  assert.notEqual(data, graph);
  assert.equal(node(data, LTX, 371).title, "LatentUpscaleModelLoader · file from OA: LTX-2.5 latent upscaler x2 bf16");
  assert.deepEqual(rest, [true, true, null, {}]);
  assert.equal(JSON.stringify(graph), before);
  assert.deepEqual(readFileSync(fixtureUrl("video_ltx2_5_t2v")), file);
  await app.loadGraphData(graph, false, false, null, {});  // undo and redo reload as given
  assert.equal(loads[1][1], graph);
  assert.equal(asked, 1);
  assert.equal(wrapLoadGraphData(wrapped, fetchApi, everyType), wrapped);
});

test("with OA unreachable the wrapped loadGraphData opens the workflow as authored", async () => {
  const graph = fixture("video_ltx2_5_t2v");
  const loads = [];
  const wrapped = wrapLoadGraphData(async data => loads.push(data), async () => { throw new TypeError("Failed to fetch"); },
    everyType, { info() {}, warn() {} });
  await wrapped(graph, true);
  assert.equal(loads[0], graph);
});
