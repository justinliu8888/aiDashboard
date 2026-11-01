import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../pages/workbench.js", import.meta.url), "utf8");

function extract(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `Missing function: ${name}`);
  return source.slice(start, source.indexOf("\n}", start) + 2);
}

function node() {
  const selected = new Map();
  return {
    dataset: {}, children: [], innerHTML: "", textContent: "",
    appendChild(child) { this.children.push(child); },
    querySelector(selector) {
      if (!selected.has(selector)) selected.set(selector, node());
      return selected.get(selector);
    },
    addEventListener() {},
  };
}

function load() {
  const sandbox = {
    document: { createElement: () => node() },
    WorkbenchQuery: { SOURCE_MODE_USER_JSON: "user_json", SOURCE_MODE_SIMULATED: "simulated", USER_JSON_SOURCE_LABEL: "用户提供的数据，未经业务核验" },
    esc: (value) => String(value ?? "").replaceAll("<", "&lt;"),
    findDefinitionRegion: (definition, id) => definition?.regions?.find((region) => region.id === id),
    readPayloadUnit: () => "", formatMetricValue: (value) => String(value),
    readPayloadWindowLabel: () => "", definitionStatusLabel: () => "草稿",
    renderGenerationInputs: () => "", renderJsonInputIssuesHtml: () => "",
  };
  vm.createContext(sandbox);
  for (const name of ["readSourceMode", "readReportSourceMode", "readCapabilityNotes", "readRegionDisplayNotes", "renderSourceNoteHtml", "renderRegionPayloadBody", "renderRegionCard", "renderPanelHeader"]) {
    vm.runInContext(extract(name), sandbox);
  }
  return sandbox;
}

function textOf(element) {
  return element.innerHTML + element.textContent + element.children.map(textOf).join("");
}

function sample(label, sourceMode) {
  const region = {
    regionId: "region", name: label, componentType: "METRIC_CARD", dataSource: "simulated", capabilityPending: true,
    payload: {
      metrics: [{ label, value: 25, capabilityPending: true, pendingReason: "当前字段尚未接通，真实数据编译尚未完成" }],
      notes: ["真实数据编译尚未完成", "能力待接通：当前字段尚未接通", "模拟业务数据，接入真实数据后自动替换"],
    },
  };
  const definition = { metadata: { sourceMode }, regions: [{ id: "region" }] };
  return { definition, region };
}

test("真实模拟来源不再画旧能力黄条或真实编译说明，不按指标名称特判", () => {
  const api = load();
  for (const label of ["温度", "耗水量", "PUE"]) {
    const { definition, region } = sample(label, "simulated");
    const card = api.renderRegionCard(region, definition, region.payload);
    assert.match(card.innerHTML, /data-region-badge="simulated"/);
    assert.doesNotMatch(textOf(card), /能力待接通|真实数据编译|接入真实数据/);
    assert.equal(card.children.some((child) => child.dataset.regionCapability), false);
    assert.match(card.querySelector(".region-body").innerHTML, />25</);
    assert.match(card.querySelector(".region-body").innerHTML, new RegExp(label));
  }
});

test("模拟顶部只给简洁说明，明确名称占位保留且无自动替换承诺", () => {
  const api = load();
  const header = api.renderPanelHeader({ metadata: { sourceMode: "simulated" } }, {}, 1);
  assert.match(header.innerHTML, /模拟业务数据/);
  assert.match(header.innerHTML, /明确名称的占位项保留，不显示数值/);
  assert.doesNotMatch(header.innerHTML, /自动替换|真实.*编译/);
  assert.match(header.innerHTML, /text-slate-500" data-simulated-note="simulated"/);
});

test("模拟名称占位卡及占位事实保持，忽略显式占位项携带的数字", () => {
  const api = load();
  const { definition, region } = sample("湿度", "simulated");
  region.payload.metrics[0].placeholderOnly = true;
  region.payload.notes.push("湿度先占位，只显示名称，没有编值。");
  const card = api.renderRegionCard(region, definition, region.payload);
  const body = card.querySelector(".region-body");
  assert.match(body.innerHTML, /湿度/);
  assert.match(body.innerHTML, /rounded-lg border border-slate-200 bg-white/);
  assert.doesNotMatch(body.innerHTML, />25</);
  assert.match(textOf(body), /湿度先占位，只显示名称，没有编值/);
});

test("JSON 权威来源优先于旧 simulated 标记，真实记录和问题原文不伪装成模拟", () => {
  const api = load();
  const { definition, region } = sample("温度", "user_json");
  definition.metadata.effectiveCombination = { sourceMode: "user_json" };
  definition.metadata.sourceMode = "simulated";
  region.payload.notes = ["温度来自用户记录", "当前字段没有编值", "真实数据编译尚未完成"];
  const card = api.renderRegionCard(region, definition, region.payload);
  assert.match(card.innerHTML, /data-region-badge="user_json"/);
  assert.doesNotMatch(card.innerHTML, /data-region-badge="simulated"/);
  assert.match(textOf(card.querySelector(".region-body")), /温度来自用户记录/);
  assert.match(textOf(card.querySelector(".region-body")), /真实数据编译尚未完成/);
  assert.match(card.querySelector(".region-body").innerHTML, />25</);
  assert.ok(card.children.some((child) => child.dataset.regionCapability === "pending"));
  const header = api.renderPanelHeader(definition, { regions: [region] }, 1);
  assert.match(header.innerHTML, /用户粘贴的 JSON 数据/);
  assert.match(header.innerHTML, /用户提供的数据，未经业务核验/);
  assert.doesNotMatch(header.innerHTML, /模拟业务数据|名称的占位项保留/);
});

test("来源不明或非契约来源不套用模拟提示清理", () => {
  const api = load();
  for (const sourceMode of ["", "mock_energy", "user_json"]) {
    const notes = api.readRegionDisplayNotes({ notes: ["真实数据编译尚未完成", "原始数据问题"] }, sourceMode);
    assert.deepEqual(Array.from(notes), ["真实数据编译尚未完成", "原始数据问题"]);
    assert.match(api.readCapabilityNotes({ capabilityPending: true }, {}, null, sourceMode)[0], /能力待接通/);
  }
  assert.equal(api.renderSourceNoteHtml(""), "");
});

test("模拟能力说明简化不掩盖授权警告、截断说明及其他业务事实", () => {
  const api = load();
  const metrics = [
    { label: "温度", capabilityPending: true, pendingReason: "字段尚未接通" },
    { label: "湿度", capabilityPending: true, pendingReason: "统计对象不在授权范围" },
  ];
  const notes = api.readCapabilityNotes({}, { metrics }, null, "simulated");
  assert.equal(notes.length, 1);
  assert.match(notes[0], /不在授权范围.*湿度/);
  const facts = ["共 300 行，已显示前 200 行", "统计对象不在授权范围", "累计值按日统计"];
  assert.deepEqual(Array.from(api.readRegionDisplayNotes({ notes: facts }, "simulated")), facts);
});
