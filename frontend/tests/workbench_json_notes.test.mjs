// 工单 R3b（R3 前端审查 🟠-1）：JSON 模式的载荷说明（`notes`）端到屏上 +
// 明细表被截断时 JSON 模式也要出提示条 + `metadata.jsonInputIssues` 端到屏上。
//
// 运行：node --test frontend/tests/workbench_json_notes.test.mjs
// 不依赖任何第三方包（Node 内置 test runner + node:vm + 手写 DOM 桩）。
//
// ── 被测的缝 ────────────────────────────────────────────────────────────
//
// 被测函数**从 frontend/pages/workbench.js 原文抽出来**跑（node:vm + 大括号配平），
// 抽不出来就直接测试失败 —— 抽不出来就是接缝断了。
//
// 本文件**不改**任何既有测试的断言：既有 174 条基线由 CI 单独跑。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const WORKBENCH_JS = readFileSync(new URL("../pages/workbench.js", import.meta.url), "utf8");

// ---------------------------------------------------------------- 抽取工具

/** 抽出一个函数定义（按大括号配平，含模板字面量里的 ${}）。 */
function extractFunction(name) {
  const marker = `function ${name}(`;
  const start = WORKBENCH_JS.indexOf(marker);
  assert.ok(start >= 0, `源码里找不到函数 ${name}`);
  let depth = 0;
  let opened = false;
  for (let i = start; i < WORKBENCH_JS.length; i += 1) {
    const ch = WORKBENCH_JS[i];
    if (ch === "{") {
      depth += 1;
      opened = true;
    } else if (ch === "}") {
      depth -= 1;
      if (opened && depth === 0) return WORKBENCH_JS.slice(start, i + 1);
    }
  }
  throw new Error(`函数 ${name} 的大括号没有配平`);
}

/** 抽出一个函数定义：先跳过参数表（可能有默认值 {}），再对函数体配平大括号。 */
function extractFunctionDeep(name) {
  const marker = `function ${name}(`;
  const start = WORKBENCH_JS.indexOf(marker);
  assert.ok(start >= 0, `源码里找不到函数 ${name}`);
  let parens = 0;
  let i = start + marker.length - 1;
  for (; i < WORKBENCH_JS.length; i += 1) {
    const ch = WORKBENCH_JS[i];
    if (ch === "(") parens += 1;
    else if (ch === ")") {
      parens -= 1;
      if (parens === 0) break;
    }
  }
  assert.ok(i < WORKBENCH_JS.length, `函数 ${name} 的参数表没有收口`);
  let braces = 0;
  let opened = false;
  for (let j = i + 1; j < WORKBENCH_JS.length; j += 1) {
    const ch = WORKBENCH_JS[j];
    if (ch === "{") {
      braces += 1;
      opened = true;
    } else if (ch === "}") {
      braces -= 1;
      if (opened && braces === 0) return WORKBENCH_JS.slice(start, j + 1);
    }
  }
  throw new Error(`函数 ${name} 的大括号没有配平`);
}

/** 抽出一条顶层 const 语句（到行尾的 ;）。 */
function extractConst(name) {
  const pattern = new RegExp(`^const ${name} = .*;$`, "m");
  const match = WORKBENCH_JS.match(pattern);
  assert.ok(match, `源码里找不到 const ${name}`);
  return match[0];
}

/** 把选中的片段放进沙箱跑一遍，返回沙箱本身（用例可以直接改沙箱全局，如 document）。 */
function loadApi(names, extraStatements = [], extractors = {}, globals = {}) {
  const snippets = [
    ...extraStatements,
    ...names.map((name) => (extractors[name] || extractFunction)(name)),
  ].join("\n\n");
  const sandbox = {
    console: { warn() {}, error() {}, log() {} },
    session: null,
    window: {},
    document: {},
    ...globals,
  };
  vm.createContext(sandbox);
  vm.runInContext(snippets, sandbox);
  return sandbox;
}

// ---------------------------------------------------------------- 假 DOM

/** 假元素：够 renderRegionCard 用（建元素、写 innerHTML、挂子节点、记住 querySelector 的结果）。 */
class FakeEl {
  constructor(tagName = "div") {
    this.tagName = String(tagName).toUpperCase();
    this.className = "";
    this.style = {};
    this.dataset = {};
    this.innerHTML = "";
    this.textContent = "";
    this.children = [];
    this.parentNode = null;
    this._attrs = new Map();
    this._selected = new Map();
  }

  setAttribute(name, value) {
    const key = String(name);
    this._attrs.set(key, String(value));
    if (key.startsWith("data-")) {
      this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
    }
  }

  getAttribute(name) {
    return this._attrs.has(String(name)) ? this._attrs.get(String(name)) : null;
  }

  hasAttribute(name) {
    return this._attrs.has(String(name));
  }

  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    child.parentNode = null;
    return child;
  }

  addEventListener() {}
  removeEventListener() {}

  /** 同一个选择器永远返回同一个元素（模拟浏览器里 DOM 节点是稳定的）。 */
  querySelector(selector) {
    const key = String(selector);
    if (!this._selected.has(key)) this._selected.set(key, new FakeEl("div"));
    return this._selected.get(key);
  }

  querySelectorAll() {
    return [];
  }
}

/** 元素树里的全部可见文字。 */
function textOf(node) {
  let out = node.textContent || "";
  for (const child of node.children || []) out += textOf(child);
  return out;
}

// ---------------------------------------------------------------- 夹具

/** 一份 JSON 模式的明细表载荷（契约表 §4.3）：总共 300 行，只显示前 200 行。 */
function jsonTablePayload() {
  return {
    columns: [{ id: "c1", label: "设备名称", fieldRef: "device", format: { kind: "TEXT" } }],
    rowNumber: true,
    rows: Array.from({ length: 200 }, (_, i) => ({ device: `A-${i + 1}` })),
    dataSource: "user_json",
    recordCount: 300,
    displayedRows: 200,
    notes: [
      "共 300 行，已显示前 200 行（上限 200 行）；汇总计算仍按全部 300 行。",
    ],
  };
}

/** 一份 JSON 模式的指标卡载荷（契约表 §4.3）：没绑上 → 不编值。 */
function jsonMetricPayload() {
  return {
    label: "总耗电概览",
    value: null,
    unit: "kWh",
    precision: 1,
    yoyPercent: null,
    comparisonAvailable: false,
    windowLabel: "用户提供的 JSON 数据（300 行）",
    dataSource: "user_json",
    metrics: [
      {
        metricKey: "m_total",
        label: "总耗电",
        kind: "generic",
        unit: "kWh",
        value: null,
        precision: 1,
        capabilityPending: false,
        pendingReason: null,
      },
    ],
    notes: [
      "这个指标没能绑定到输入字段，没有编值。",
      "字段「total_energy」在数据里缺失。",
    ],
  };
}

/** 渲染一块板（走真实的 renderRegionCard），返回正文 HTML 与正文里的说明文字。 */
function renderCard(api, region, defRegion) {
  api.document = { createElement: (tagName) => new FakeEl(tagName) };
  const definition = { regions: defRegion ? [defRegion] : [] };
  const card = api.renderRegionCard(region, definition, region.payload, null);
  const body = card.querySelector(".region-body");
  const noteBox = body.children.find(
    (child) => child.dataset && child.dataset.regionPayloadNotes === "true"
  );
  return {
    card,
    body,
    html: body.innerHTML,
    notes: noteBox ? noteBox.children.map((line) => line.textContent) : null,
  };
}

/** 渲染用的源码片段（全部来自工作台主脚本的原文）。 */
function loadCardApi() {
  return loadApi(
    [
      "renderRegionCard",
      "renderRegionPayloadBody",
      "readCapabilityNotes",
      "readPrimaryMetric",
      "readBoundMetricKey",
      "readPayloadUnit",
      "formatMetricValue",
      "findDefinitionRegion",
      "readTableTruncation",
      "buildTotalsByUnit",
      "readPositiveInt",
      "sumColumnValues",
    ],
    [extractConst("esc")],
    { renderRegionPayloadBody: extractFunctionDeep }
  );
}

// ================================================================ 截断判定

test("JSON 模式：recordCount > displayedRows 就是被截断了（契约表 §4.3）", () => {
  const api = loadApi(["readTableTruncation", "readPositiveInt", "sumColumnValues"]);
  const payload = jsonTablePayload();

  const result = api.readTableTruncation(payload, null, null);
  assert.equal(result.truncated, true, "JSON 明细表被截断了就要说出来");
  assert.equal(result.limit, 200, "「仅显示前 N 行」的 N = 实际显示的行数");
  assert.equal(result.shown, 200);
  assert.equal(result.total, 300, "总共多少行也要说得出来");
  assert.equal(result.source, "json");
});

test("JSON 模式：行数没超上限时不误报截断", () => {
  const api = loadApi(["readTableTruncation", "readPositiveInt", "sumColumnValues"]);

  // 总共 200 行、显示 200 行：没截断。
  const exact = api.readTableTruncation(
    { rows: [{ a: 1 }], recordCount: 200, displayedRows: 200 },
    null,
    null
  );
  assert.equal(exact, null, "行数没超上限不能乱说截断");

  // 只有 recordCount、没有 displayedRows：判据不完整 → 不说。
  const half = api.readTableTruncation({ rows: [{ a: 1 }], recordCount: 300 }, null, null);
  assert.equal(half, null);

  // 两个字段都没有：不是 JSON 载荷 → 走老路（这里也判不出截断）。
  const neither = api.readTableTruncation({ rows: [{ a: 1 }] }, null, null);
  assert.equal(neither, null);
});

test("定义侧（模拟模式）的老判定逐字不变，JSON 字段也带不坏它", () => {
  const api = loadApi([
    "readTableTruncation",
    "buildTotalsByUnit",
    "readPositiveInt",
    "sumColumnValues",
  ]);

  const table = {
    columns: [{ id: "c1", label: "耗电量", fieldRef: "energy", format: { unitLabel: "kWh" } }],
    rows: [
      { energy: 100.0 },
      { energy: 50.0 },
    ],
  };
  const defRegion = { binding: { topN: 10 } };
  const totals = api.buildTotalsByUnit([
    { componentType: "METRIC_CARD", payload: { unit: "kWh", value: 5123.4 } },
  ]);

  // ① 载荷自己说截断：直接采信，limit 取 topN。
  const explicit = api.readTableTruncation({ ...table, truncated: true, totalRows: 12 }, defRegion, null);
  assert.equal(explicit.truncated, true);
  assert.equal(explicit.limit, 10);
  assert.equal(explicit.source, undefined, "老判据不带来源标记");

  // ② 载荷说了总行数：limit 取这张表声明的 topN（老行为），total = 总行数。
  const declared = api.readTableTruncation({ ...table, totalRows: 12 }, defRegion, null);
  assert.equal(declared.truncated, true);
  assert.equal(declared.limit, 10);
  assert.equal(declared.total, 12);

  // ③ 勾稽反推：行数达到 topN 且行和明显小于同口径总量 → 截断。
  const byReconciliation = api.readTableTruncation(table, { binding: { topN: 2 } }, totals);
  assert.equal(byReconciliation.truncated, true);
  assert.equal(byReconciliation.limit, 2);

  // ④ 行和等于总量 → 不标；行数没到 topN → 不标；没声明 topN → 不乱说。
  const full = { ...table, rows: [{ energy: 5123.4 }] };
  assert.equal(api.readTableTruncation(full, { binding: { topN: 1 } }, totals), null);
  assert.equal(api.readTableTruncation(table, defRegion, totals), null);
  assert.equal(api.readTableTruncation(table, { binding: {} }, totals), null);

  // ⑤ 载荷同时带 JSON 的两个字段（相等的）→ 老判定结果不变。
  const withJsonFields = api.readTableTruncation(
    { ...table, recordCount: 2, displayedRows: 2 },
    { binding: { topN: 2 } },
    totals
  );
  assert.equal(withJsonFields.truncated, true, "老判定仍然生效");
  assert.equal(withJsonFields.limit, 2);
  assert.equal(withJsonFields.source, undefined, "JSON 字段没触发时不带来源标记");
});

// ================================================================ 总量卡片

test("指标卡没有值就不计入总量（null 不是 0：不知道 ≠ 零）", () => {
  const api = loadApi(["buildTotalsByUnit"]);

  // 没有值：这一格不参与总量（Number(null) === 0 会把「不知道」说成「零」）。
  const nullValue = api.buildTotalsByUnit([
    { componentType: "METRIC_CARD", payload: { unit: "kWh", value: null } },
  ]);
  assert.equal(nullValue.has("kWh"), false, "null 不进总量");

  // 空串同理（界面上也是「—」）。
  const emptyValue = api.buildTotalsByUnit([
    { componentType: "METRIC_CARD", payload: { unit: "kWh", value: "" } },
  ]);
  assert.equal(emptyValue.has("kWh"), false, "空串不进总量");

  // 0 就是 0：它是真值，必须计入。
  const zeroValue = api.buildTotalsByUnit([
    { componentType: "METRIC_CARD", payload: { unit: "kWh", value: 0 } },
  ]);
  assert.equal(zeroValue.get("kWh"), 0, "0 是 0，不是「没有值」");

  // 有值：照旧计入。
  const real = api.buildTotalsByUnit([
    { componentType: "METRIC_CARD", payload: { unit: "kWh", value: 5123.4 } },
  ]);
  assert.equal(real.get("kWh"), 5123.4);
});

// ================================================================ 说明渲染

test("区域卡正文里渲染 payload.notes（服务端写的原文，一行一句）", () => {
  const api = loadCardApi();

  const table = renderCard(
    api,
    {
      regionId: "region_1",
      name: "设备耗电明细",
      role: "DETAIL",
      componentType: "TABLE",
      payload: jsonTablePayload(),
    },
    { id: "region_1", name: "设备耗电明细" }
  );
  assert.ok(table.notes, "区域卡正文里要有说明文字");
  assert.deepEqual(table.notes, [
    "共 300 行，已显示前 200 行（上限 200 行）；汇总计算仍按全部 300 行。",
  ]);

  const metric = renderCard(
    api,
    {
      regionId: "region_2",
      name: "总耗电概览",
      role: "SUMMARY",
      componentType: "METRIC_CARD",
      payload: jsonMetricPayload(),
    },
    { id: "region_2", name: "总耗电概览" }
  );
  assert.ok(metric.notes, "指标卡的说明文字也要看得见");
  assert.deepEqual(metric.notes, [
    "这个指标没能绑定到输入字段，没有编值。",
    "字段「total_energy」在数据里缺失。",
  ]);
});

test("载荷没有 notes 时不多画说明（没话说就不说）", () => {
  const api = loadCardApi();
  const card = renderCard(
    api,
    {
      regionId: "region_3",
      name: "设备耗电明细",
      role: "DETAIL",
      componentType: "TABLE",
      payload: {
        columns: [{ id: "c1", label: "设备名称", fieldRef: "device" }],
        rows: [{ device: "A-1" }],
      },
    },
    { id: "region_3", name: "设备耗电明细" }
  );
  assert.equal(card.notes, null, "没有 notes 就不画说明块");
});

// ================================================================ 截断提示条

test("JSON 模式也出截断提示条（复用既有的 data-table-truncated）", () => {
  const api = loadCardApi();
  api.document = { createElement: (tagName) => new FakeEl(tagName) };

  const body = new FakeEl("div");
  api.renderRegionPayloadBody(
    body,
    {
      regionId: "region_1",
      role: "DETAIL",
      componentType: "TABLE",
      payload: jsonTablePayload(),
    },
    { regions: [] },
    { defRegion: null, totalsByUnit: null }
  );

  assert.ok(body.innerHTML.includes('data-table-truncated="true"'), "JSON 模式要出截断提示条");
  assert.ok(body.innerHTML.includes("仅显示前 200 行"), "要写明显示了多少行");
  assert.ok(body.innerHTML.includes("共 300 行"), "要写明一共多少行");
});

test("定义侧的截断提示条文案逐字不变（老断言不许被放宽）", () => {
  const api = loadCardApi();
  api.document = { createElement: (tagName) => new FakeEl(tagName) };

  const body = new FakeEl("div");
  api.renderRegionPayloadBody(
    body,
    {
      regionId: "region_1",
      role: "DETAIL",
      componentType: "TABLE",
      payload: {
        columns: [{ id: "c1", label: "耗电量", fieldRef: "energy", format: { unitLabel: "kWh" } }],
        rows: [{ energy: 5123.4 }],
        truncated: true,
        totalRows: 12,
      },
    },
    { regions: [] },
    { defRegion: { binding: { topN: 2 } }, totalsByUnit: null }
  );

  assert.ok(body.innerHTML.includes('data-table-truncated="true"'));
  assert.ok(
    body.innerHTML.includes("仅显示前 2 行（表格行数被截断，各行之和小于报表总量）"),
    `定义侧的既有文案要逐字不变：${body.innerHTML}`
  );
});

// ================================================================ 输入问题

test("metadata.jsonInputIssues 端到屏上（哪些列没绑上、数据里有哪些问题）", () => {
  const api = loadApi(["renderJsonInputIssuesHtml"], [extractConst("esc")]);

  const html = api.renderJsonInputIssuesHtml({
    regions: [{ id: "region_1", name: "设备耗电明细" }],
    metadata: {
      jsonInputIssues: {
        unmappedColumns: [
          { regionId: "region_1", columnId: "c2", label: "采集时间", reason: "输入里没有同名字段" },
        ],
        dataIssues: ["字段「total_energy」有 3 行缺失，缺失按「（缺失）」标注，不当成 0。"],
      },
    },
  });

  assert.ok(html.includes('data-json-input-issues="true"'), "要有输入问题这一块");
  assert.ok(html.includes("以下栏目没能绑定到输入字段"), "要说清是哪些栏目没绑上");
  assert.ok(html.includes("「采集时间」"), "栏目名要摆出来");
  assert.ok(html.includes("设备耗电明细"), "要说清是哪一块板的栏目");
  assert.ok(html.includes("输入里没有同名字段"), "原因照搬服务端写的");
  assert.ok(html.includes("数据里的问题"), "数据问题也要摆出来");
  assert.ok(html.includes("有 3 行缺失"), "数据问题的原文照搬");
});

test("没有 jsonInputIssues 时不多画（没话说就不说）", () => {
  const api = loadApi(["renderJsonInputIssuesHtml"], [extractConst("esc")]);
  assert.equal(api.renderJsonInputIssuesHtml({ metadata: {} }), "");
  assert.equal(api.renderJsonInputIssuesHtml({ metadata: { jsonInputIssues: {} } }), "");
  assert.equal(
    api.renderJsonInputIssuesHtml({
      metadata: { jsonInputIssues: { unmappedColumns: [], dataIssues: [] } },
    }),
    ""
  );
  assert.equal(api.renderJsonInputIssuesHtml(null), "");
});
