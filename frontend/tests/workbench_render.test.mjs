// 工单 07（#53）：草稿面板按载荷渲染、弱标注、徽标与取消独立预览面板。
//
// 运行：node --test frontend/tests/workbench_render.test.mjs
// 不依赖任何第三方包（Node 内置 test runner + node:vm），不改共用依赖配置。
//
// ── 被测的缝（seam）─────────────────────────────────────────────
// 工作台主脚本（frontend/pages/workbench.js）是**浏览器脚本**，不是模块：
// 顶部就摸 window / document，直接 require 会炸。
// 所以这里用 node:vm 把「纯逻辑那几段函数」原样抽出来，在沙箱里跑：
//
//   - 载荷读取与版本校对（readReportPreviewPayload / isStaleReportPayload /
//     applyReportPreviewPayload）
//   - 单位读取（readPayloadUnit）
//   - 能力待接通标注（readCapabilityNotes）
//   - 指标卡主量判定（readPrimaryMetric / readBoundMetricKey）
//   - 卡片渲染（renderRegionCard / renderRegionPayloadBody，带假 DOM）
//   - 表格截断判定（readTableTruncation）
//   - 徽标状态机（draftBadgeView）
//
// 这些函数在源码里是**同一份文本**，不是复制品：抽不出来就直接测试失败。
//
// DOM 层的断言（真的画出卡片、真的画了曲线）由交互流程
// （flows/tmpl-07-render.flow.json，浏览器里跑）承担，这里只守纯逻辑与静态契约。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const WORKBENCH_JS = readFileSync(new URL("../pages/workbench.js", import.meta.url), "utf8");
const WORKBENCH_HTML = readFileSync(new URL("../pages/workbench.html", import.meta.url), "utf8");

// ---------------------------------------------------------------- 抽取工具

/**
 * 抽出一个函数定义（按大括号配平，含模板字面量里的 ${}）。
 * 注意：参数表里有**默认值 {}** 时不能直接配平大括号
 * （renderRegionPayloadBody 的 `ctx = {}` 会被截断），那种函数用
 * extractFunctionDeep。
 */
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

/** 抽出一条顶层 const 对象/数组语句（大括号配平，允许跨行）。 */
function extractConstBlock(name) {
  const marker = `const ${name} =`;
  const start = WORKBENCH_JS.indexOf(marker);
  assert.ok(start >= 0, `源码里找不到 const ${name}`);
  let depth = 0;
  let opened = false;
  for (let i = start; i < WORKBENCH_JS.length; i += 1) {
    const ch = WORKBENCH_JS[i];
    if (ch === "{" || ch === "[") {
      depth += 1;
      opened = true;
    } else if (ch === "}" || ch === "]") {
      depth -= 1;
      if (opened && depth === 0) return WORKBENCH_JS.slice(start, i + 1);
    } else if (ch === ";" && opened && depth === 0) {
      return WORKBENCH_JS.slice(start, i + 1);
    }
  }
  throw new Error(`const ${name} 的括号没有配平`);
}

/** 把选中的片段放进沙箱跑一遍，返回它们组成的 API。 */
function loadApi(names, extraStatements = [], extractors = {}, globals = {}) {
  const snippets = [
    ...extraStatements,
    ...names.map((name) => (extractors[name] || extractFunction)(name)),
  ].join("\n\n");
  const sandbox = {
    console: { warn() {}, error() {}, log() {} },
    session: null, // 每个用例自己填
    window: {},
    document: {},
    ...globals, // 用例自己的沙箱全局（假 DOM、桩函数……）
  };
  vm.createContext(sandbox);
  vm.runInContext(snippets + "\n;globalThis.__api = {" + names.join(", ") + "};", sandbox);
  return sandbox;
}

function makeSession(overrides = {}) {
  return {
    state: "ANALYZING",
    definition: null,
    previewPayload: null,
    reportPreviewPayload: null,
    reportPreviewError: null,
    ...overrides,
  };
}

// ---------------------------------------------------------------- 契约样例
//
// 载荷形状来自开发规格 #46「跨工单契约」与票 04 的造数器输出：
// 区域带 regionId / name / componentType / role / payload；
// payload 与既有公开预览载荷逐字段同构。

const SAMPLE_PAYLOAD = {
  previewContext: {
    definitionId: "def_001",
    definitionVersion: 3,
    timezone: "Asia/Shanghai",
    anchorTime: "2026-10-28T00:00:00+08:00",
    windowStart: "2026-10-27T00:00:00+08:00",
    windowEnd: "2026-10-28T00:00:00+08:00",
    grainUnit: "HOUR",
    generatorVersion: "sim-1",
  },
  regions: [
    {
      regionId: "region_total",
      name: "总耗电概览",
      componentType: "METRIC_CARD",
      role: "TOTAL",
      dataSource: "simulated",
      capabilityPending: false,
      payload: {
        label: "总耗电",
        value: 5123.4,
        unit: "kWh",
        precision: 1,
        yoyPercent: 6.2,
        comparisonAvailable: true,
        windowLabel: "2026-10-27 全天",
        dataSource: "simulated",
        metrics: [
          { metricKey: "total_energy", label: "总耗电", kind: "energy", unit: "kWh", value: 5123.4, precision: 1, capabilityPending: false },
          { metricKey: "total_cost", label: "总电费", kind: "cost", unit: "元", value: 512.34, precision: 2, capabilityPending: false },
        ],
      },
    },
    {
      regionId: "region_pue",
      name: "PUE 总览",
      componentType: "METRIC_CARD",
      role: "TOTAL",
      dataSource: "simulated",
      capabilityPending: true,
      payload: {
        label: "PUE",
        value: 1.42,
        unit: "",
        precision: 2,
        yoyPercent: -1.1,
        comparisonAvailable: true,
        windowLabel: "2026-10-27 全天",
        dataSource: "simulated",
        metrics: [
          {
            metricKey: "pue",
            label: "PUE",
            kind: "pue",
            unit: "",
            value: 1.42,
            precision: 2,
            capabilityPending: true,
            pendingReason: "PUE 口径待核实，当前数据源没有登记这个字段",
          },
        ],
      },
    },
    {
      regionId: "region_trend",
      name: "耗电趋势",
      componentType: "CHART",
      role: "TREND",
      dataSource: "simulated",
      capabilityPending: false,
      payload: {
        chartKind: "LINE",
        grain: "HOUR",
        xField: "timestamp",
        yField: "energyKwh",
        xLabels: ["09:00", "10:00", "11:00"],
        yValues: [210.5, 233.1, 198.7],
        points: [],
        unit: "kWh",
        dataSource: "simulated",
      },
    },
    {
      regionId: "region_ranking",
      name: "设备耗电排名",
      componentType: "TABLE",
      role: "DETAIL",
      dataSource: "simulated",
      capabilityPending: false,
      payload: {
        columns: [
          { id: "col_device", label: "设备名称", fieldRef: "ranking.device_name", format: { kind: "TEXT" } },
          { id: "col_energy", label: "耗电", fieldRef: "ranking.energy", format: { kind: "NUMBER", precision: 1, unitLabel: "kWh" } },
        ],
        rowNumber: true,
        rows: [
          { "ranking.device_name": "A-1 机架", "ranking.energy": 120.5 },
          { "ranking.device_name": "A-2 机架", "ranking.energy": 98.2 },
        ],
        dataSource: "simulated",
      },
    },
  ],
};

const SAMPLE_DEFINITION = {
  schemaVersion: "1.0",
  metadata: { id: "def_001", revision: 3, name: "A 机房能耗日报", status: "DRAFT" },
  regions: [
    { id: "region_total", name: "总耗电概览", role: "TOTAL", component: { type: "METRIC_CARD" }, binding: {} },
    { id: "region_ranking", name: "设备耗电排名", role: "DETAIL", component: { type: "TABLE" }, binding: { topN: 10 } },
  ],
};

// ---------------------------------------------------------------- 载荷读取

test("载荷必须是对象且带 regions 数组，否则当作没有载荷", () => {
  const api = loadApi(["readReportPreviewPayload"]);
  assert.equal(api.__api.readReportPreviewPayload({ reportPreviewPayload: null }), null);
  assert.equal(api.__api.readReportPreviewPayload({}), null);
  assert.equal(api.__api.readReportPreviewPayload({ reportPreviewPayload: { regions: null } }), null);
  assert.equal(api.__api.readReportPreviewPayload({ reportPreviewPayload: { regions: [] } }).regions.length, 0);
  assert.equal(api.__api.readReportPreviewPayload({ reportPreviewPayload: SAMPLE_PAYLOAD }), SAMPLE_PAYLOAD);
});

test("旧版本的载荷被丢掉，不覆盖新模板", () => {
  const api = loadApi(["normalizeVersionNumber", "isStaleReportPayload", "applyReportPreviewPayload"]);
  const session = makeSession({ definition: { metadata: { id: "def_001", revision: 3 } } });
  api.session = session;

  // 定义没变：不是过期。
  assert.equal(api.__api.isStaleReportPayload(SAMPLE_PAYLOAD), false);

  // 定义换了一个（id 不同）：过期。
  const otherDefinition = {
    ...SAMPLE_PAYLOAD,
    previewContext: { ...SAMPLE_PAYLOAD.previewContext, definitionId: "def_002" },
  };
  assert.equal(api.__api.isStaleReportPayload(otherDefinition), true);

  // 版本号对不上：过期。
  const otherVersion = {
    ...SAMPLE_PAYLOAD,
    previewContext: { ...SAMPLE_PAYLOAD.previewContext, definitionVersion: 4 },
  };
  assert.equal(api.__api.isStaleReportPayload(otherVersion), true);

  // 收载荷：新的收下，过期的丢掉。
  assert.equal(api.__api.applyReportPreviewPayload(SAMPLE_PAYLOAD), true);
  assert.equal(session.reportPreviewPayload, SAMPLE_PAYLOAD);
  assert.equal(api.__api.applyReportPreviewPayload(otherVersion), false);
  assert.equal(session.reportPreviewPayload, SAMPLE_PAYLOAD);
});

test("版本校对按方案口径（templateBinding），不拿定义编辑计数去比", () => {
  const api = loadApi(["normalizeVersionNumber", "isStaleReportPayload", "applyReportPreviewPayload"]);
  // 草稿阶段的定义：metadata.revision 是**定义的编辑计数**（0 起），
  // templateBinding.definitionVersion 才是**方案 revision** —— 两条轴，不能混。
  // 混了会把同一份载荷误判成过期，草稿阶段的数字直接被丢掉。
  api.session = makeSession({
    definition: {
      metadata: {
        id: "def_001",
        revision: 0,
        templateBinding: { definitionId: "plan_001", definitionVersion: 1 },
      },
    },
  });

  const payload = {
    previewContext: { definitionId: "plan_001", definitionVersion: 1 },
    regions: [],
  };
  // 同一份方案版本：不是过期，载荷必须收下。
  assert.equal(api.__api.isStaleReportPayload(payload), false);
  assert.equal(api.__api.applyReportPreviewPayload(payload), true);
  assert.equal(api.session.reportPreviewPayload, payload);

  // 方案版本变了：过期，丢掉。
  const bumped = {
    previewContext: { definitionId: "plan_001", definitionVersion: 2 },
    regions: [],
  };
  assert.equal(api.__api.isStaleReportPayload(bumped), true);
  assert.equal(api.__api.applyReportPreviewPayload(bumped), false);
  assert.equal(api.session.reportPreviewPayload, payload);

  // 方案 id 变了：过期。
  const otherPlan = {
    previewContext: { definitionId: "plan_002", definitionVersion: 1 },
    regions: [],
  };
  assert.equal(api.__api.isStaleReportPayload(otherPlan), true);
});

test("版本号 0 是合法版本号，不能被当成缺省而回退", () => {
  const api = loadApi(["normalizeVersionNumber", "isStaleReportPayload"]);
  api.session = makeSession({
    definition: {
      metadata: {
        revision: 5,
        templateBinding: { definitionId: "plan_001", definitionVersion: 0 },
      },
    },
  });
  // templateBinding 的版本 0 优先于 metadata.revision 5 —— 不该判过期。
  assert.equal(
    api.__api.isStaleReportPayload({ previewContext: { definitionId: "plan_001", definitionVersion: 0 } }),
    false,
  );
  // 对比版本 1：与 templateBinding 的 0 不一致 → 过期。
  assert.equal(
    api.__api.isStaleReportPayload({ previewContext: { definitionId: "plan_001", definitionVersion: 1 } }),
    true,
  );
});

test("定义还没落位时不拿载荷的版本乱比（不误杀）", () => {
  const api = loadApi(["isStaleReportPayload"]);
  api.session = makeSession();
  assert.equal(api.__api.isStaleReportPayload(SAMPLE_PAYLOAD), false);
  assert.equal(api.__api.isStaleReportPayload({ regions: [] }), false);
});

// ---------------------------------------------------------------- 单位

test("单位从载荷里读，不硬编码 kWh / 耗电", () => {
  const api = loadApi(["readPayloadUnit"]);

  const chart = SAMPLE_PAYLOAD.regions[2];
  assert.equal(api.__api.readPayloadUnit(chart, chart.payload, null), "kWh");

  // 同一张图换成总电费，单位必须跟着变（套 kWh 就是错的）。
  const costChart = { ...chart, payload: { ...chart.payload, unit: "元" } };
  assert.equal(api.__api.readPayloadUnit(costChart, costChart.payload, null), "元");

  // 多指标：逐条序列也带单位。
  const multi = {
    ...chart,
    payload: { chartKind: "LINE", series: [{ metricKey: "cost", label: "总电费", kind: "cost", unit: "元", yValues: [1] }] },
  };
  assert.equal(api.__api.readPayloadUnit(multi, multi.payload, null), "元");

  // 载荷没带单位时退回定义里声明的单位；再没有就空着（不拿别的单位顶替）。
  const defRegion = { templateBinding: { previewSpec: { unit: "kW" } } };
  assert.equal(api.__api.readPayloadUnit({}, {}, defRegion), "kW");
  assert.equal(api.__api.readPayloadUnit({}, {}, null), "");
});

// ---------------------------------------------------------------- 能力待接通

test("未接通的指标额外写明「能力待接通」，并说明具体项", () => {
  const api = loadApi(["readCapabilityNotes"]);

  const region = SAMPLE_PAYLOAD.regions[1];
  const notes = api.__api.readCapabilityNotes(region, region.payload, null);
  assert.equal(notes.length, 1);
  assert.ok(notes[0].includes("PUE"), "应说明是哪个指标");
  assert.ok(notes[0].includes("口径待核实"), "应带上原因");

  // 区域级标记（没有逐项说明）：也要写出来。
  const generic = api.__api.readCapabilityNotes({ capabilityPending: true }, {}, null);
  assert.equal(generic.length, 1);

  // 都接通了：没有这条标注。
  const ok = SAMPLE_PAYLOAD.regions[0];
  assert.equal(api.__api.readCapabilityNotes(ok, ok.payload, null).length, 0);
});

// ---------------------------------------------------------------- 指标卡主量与角标文案
//
// 真实形状来自 frontend/tests/page_accept_harness.html（**后端真实产出**，冻结在那里）：
//   payload.label           = **区域名**（「总电费概览」）
//   payload.metrics[].label = **目录里的指标名**（「能耗成本」）
// 两者不同名 —— 「label 相等」判不出主量，主量会在「其它指标」清单里再出现一次
//（同一个数字在卡片上出现两次）。
//
// 这里**直接读 harness**（不抄一份）：抄的那份会跟着后端形状一起漂移
//（区域级多了 dataSource / capabilityPending、噪声数字也变了），
// 抄一份就得跟着抄一次。形状本身由下面那条「形状守卫」用例钉住。

const HARNESS_HTML = readFileSync(new URL("./page_accept_harness.html", import.meta.url), "utf8");

/** 从 harness 里原样取出某个 var 的 JSON 文本（大括号配平）。 */
function extractHarnessVar(name) {
  const at = HARNESS_HTML.indexOf(`var ${name} =`);
  assert.ok(at >= 0, `harness 里找不到 ${name}`);
  const start = HARNESS_HTML.indexOf("{", at);
  let depth = 0;
  for (let i = start; i < HARNESS_HTML.length; i += 1) {
    const ch = HARNESS_HTML[i];
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) return HARNESS_HTML.slice(start, i + 1);
    }
  }
  throw new Error(`${name} 的大括号没有配平`);
}

/** harness 里真实产出的 region_3 载荷与定义（原件，不是抄的）。 */
function harnessRealMetricCard() {
  const payload = JSON.parse(extractHarnessVar("REAL_PAYLOAD"));
  const definition = JSON.parse(extractHarnessVar("DEFINITION"));
  return {
    region: payload.regions.find((r) => r.regionId === "region_3"),
    defRegion: definition.regions.find((r) => r.id === "region_3"),
  };
}

const HARNESS_REAL = harnessRealMetricCard();

/** 真实载荷（直接取自 harness 的 region_3）。 */
const REAL_METRIC_CARD_PAYLOAD = HARNESS_REAL.region.payload;

/** 真实区域（直接取自 harness 的 region_3）。 */
const REAL_METRIC_CARD_REGION = HARNESS_REAL.region;

/** 真实定义里 region_3 那一块（绑定 + 纯结构描述）。 */
const REAL_METRIC_CARD_DEF_REGION = HARNESS_REAL.defRegion;

/** 真实载荷里那张卡的主量数值（断言用，不写死数字）。 */
const REAL_CARD_VALUE = String(REAL_METRIC_CARD_PAYLOAD.value);

/** 假 DOM 元素：够 renderRegionCard 用（建元素、写 innerHTML、挂子节点、记住 querySelector 的结果）。 */
function makeFakeCardElement(tagName = "div") {
  const el = {
    tagName: String(tagName).toUpperCase(),
    className: "",
    style: {},
    dataset: {},
    innerHTML: "",
    textContent: "",
    children: [],
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    addEventListener() {},
    setAttribute(name, value) {
      this[name] = value;
    },
    getAttribute(name) {
      return this[name] ?? null;
    },
  };
  el.querySelector = (selector) => {
    if (!el._selected) el._selected = {};
    if (!el._selected[selector]) el._selected[selector] = makeFakeCardElement("div");
    return el._selected[selector];
  };
  return el;
}

/** 渲染一块板（走真实的 renderRegionCard），返回正文 HTML 与角标文案。 */
function renderCard(api, region, defRegion) {
  api.document = { createElement: (tagName) => makeFakeCardElement(tagName) };
  const definition = { regions: defRegion ? [defRegion] : [] };
  const card = api.__api.renderRegionCard(region, definition, region.payload, null);
  const body = card.querySelector(".region-body");
  const badge = card.children.find((child) => child.dataset && child.dataset.regionCapability);
  return { card, html: body.innerHTML, badge: badge ? badge.textContent : "" };
}

/** 渲染用到的源码片段（全部来自工作台主脚本的原文）。 */
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
    ],
    [extractConst("esc")],
    { renderRegionPayloadBody: extractFunctionDeep }
  );
}

test("harness 里的真实形状仍是本测试假设的形状（不写死一套假数据）", () => {
  const real = harnessRealMetricCard();
  assert.ok(real.region && real.defRegion, "harness 里没有 region_3");

  // 区域级：后端现在**七个**键（多出 dataSource / capabilityPending）。
  assert.deepEqual(
    Object.keys(real.region).sort(),
    ["capabilityPending", "componentType", "dataSource", "name", "payload", "regionId", "role"]
  );
  assert.equal(real.region.dataSource, "simulated");
  assert.equal(typeof real.region.capabilityPending, "boolean");
  // 区域级能力标记 = 该区域各项指标的逻辑或。
  assert.equal(
    real.region.capabilityPending,
    (real.region.payload.metrics || []).some((m) => m.capabilityPending === true)
  );

  // 载荷级：主量在顶层，逐项指标在 metrics[] 里。
  for (const key of ["label", "value", "unit", "precision", "metrics"]) {
    assert.ok(key in real.region.payload, `真实载荷里没有 ${key}`);
  }
  assert.ok(real.region.payload.metrics.length >= 1, "真实载荷里没有逐项指标");
  for (const key of ["metricKey", "label", "kind", "unit", "value", "capabilityPending"]) {
    assert.ok(key in real.region.payload.metrics[0], `真实指标里没有 ${key}`);
  }
  // 定义侧：绑定与纯结构描述。
  for (const key of ["sectionId", "dataStatus", "metricRefs", "previewSpec"]) {
    assert.ok(key in real.defRegion.templateBinding, `真实定义里没有 templateBinding.${key}`);
  }
  assert.ok(real.defRegion.templateBinding.previewSpec.metrics.length >= 1, "定义侧没有逐项指标");

  // 这个缺陷依赖的两条真实关系：两处 label 不同名、主量的值与单位在顶层。
  assert.equal(real.region.payload.label, "总电费概览");
  assert.notEqual(real.region.payload.label, real.region.payload.metrics[0].label, "真实形状里两处 label 本来就不同名");
  assert.equal(real.region.payload.metrics[0].value, real.region.payload.value);
  assert.equal(real.region.payload.metrics[0].unit, real.region.payload.unit);
});

test("指标卡主量只渲染一次（真实形状：payload.label 是区域名、metrics[].label 是指标名）", () => {
  const api = loadCardApi();
  const { html } = renderCard(api, REAL_METRIC_CARD_REGION, REAL_METRIC_CARD_DEF_REGION);

  // 主量（取自 harness 的真实数值）在卡片正文里只出现一次（不再在「其它指标」清单里再来一遍）。
  assert.equal(html.split(REAL_CARD_VALUE).length - 1, 1, "主量渲染了多次");
  assert.ok(!html.includes('data-region-metric="metric.machine_room.energy_cost"'), "主量还在「其它指标」清单里");

  // 大数字上方写的是**指标名**（卡片标题已经是区域名，不必再写一遍）。
  assert.ok(html.includes("能耗成本"), "大数字上方要写明这是哪个量");
});

test("多指标卡片：主量只画一次，真正的「其它指标」留在清单里", () => {
  const api = loadCardApi();
  const region = {
    ...REAL_METRIC_CARD_REGION,
    payload: {
      ...REAL_METRIC_CARD_PAYLOAD,
      metrics: [
        REAL_METRIC_CARD_PAYLOAD.metrics[0],
        {
          metricKey: "metric.machine_room.total_energy",
          label: "总耗电",
          kind: "energy",
          unit: "kWh",
          value: 37357.8,
          precision: 1,
          capabilityPending: false,
          pendingReason: null,
        },
      ],
    },
  };
  const { html } = renderCard(api, region, REAL_METRIC_CARD_DEF_REGION);

  assert.equal(html.split(REAL_CARD_VALUE).length - 1, 1, "主量渲染了多次");
  assert.equal((html.match(/37357\.8/g) || []).length, 1, "其它指标应出现一次");
  assert.ok(!html.includes('data-region-metric="metric.machine_room.energy_cost"'), "主量不该在「其它指标」清单里");
  assert.ok(html.includes('data-region-metric="metric.machine_room.total_energy"'), "其它指标应留在清单里");
});

test("主量判定不靠位置：主量排在清单后面也认得出来（按值+单位，不按 label）", () => {
  const api = loadCardApi();
  const region = {
    ...REAL_METRIC_CARD_REGION,
    payload: {
      ...REAL_METRIC_CARD_PAYLOAD,
      metrics: [
        {
          metricKey: "metric.machine_room.total_energy",
          label: "总耗电",
          kind: "energy",
          unit: "kWh",
          value: 37357.8,
          precision: 1,
          capabilityPending: false,
          pendingReason: null,
        },
        REAL_METRIC_CARD_PAYLOAD.metrics[0],
      ],
    },
  };
  const { html } = renderCard(api, region, null);

  // 值+单位对得上的是主量（能耗成本，取自 harness 的真实数值），不是清单第一条。
  assert.equal(html.split(REAL_CARD_VALUE).length - 1, 1, "主量渲染了多次");
  assert.ok(!html.includes('data-region-metric="metric.machine_room.energy_cost"'), "主量不该在「其它指标」清单里");
  assert.ok(html.includes('data-region-metric="metric.machine_room.total_energy"'), "其它指标应留在清单里");
});

test("授权类原因不标成「能力待接通」：角标按原因分文案", () => {
  const api = loadCardApi();
  const withReason = (pendingReason) => ({
    ...REAL_METRIC_CARD_REGION,
    payload: {
      ...REAL_METRIC_CARD_PAYLOAD,
      metrics: [{ ...REAL_METRIC_CARD_PAYLOAD.metrics[0], pendingReason }],
    },
  });

  // 统计对象不在授权范围内 → 这是授权问题，不是能力缺失。
  const scope = renderCard(api, withReason("统计对象不在本次授权范围内，暂不纳入统计。"), null);
  assert.ok(scope.badge.includes("不在授权范围"), `授权类角标文案不对：${scope.badge}`);
  assert.ok(!scope.badge.includes("能力待接通"), `授权类不该标成能力待接通：${scope.badge}`);

  // 能力没接通 → 还是「能力待接通」。
  const capability = renderCard(api, withReason("「能耗成本」口径待核实：当前数据源没有登记这个字段，算不出数值。"), null);
  assert.ok(capability.badge.includes("能力待接通"), `能力类角标文案不对：${capability.badge}`);
  assert.ok(!capability.badge.includes("不在授权范围"), `能力类不该标成授权范围：${capability.badge}`);

  // 原因为空 → 按「能力待接通」兜底（没写原因就分不出是不是授权问题）。
  const blank = renderCard(api, withReason(""), null);
  assert.ok(blank.badge.includes("能力待接通"), `空原因应兜底成能力待接通：${blank.badge}`);
  assert.ok(!blank.badge.includes("不在授权范围"), `空原因不该标成授权范围：${blank.badge}`);
});

test("区域级 capabilityPending 缺失时仍能从 payload.metrics[] 聚合出标记", () => {
  const api = loadCardApi();
  // 真实后端**现在**产出区域级 capabilityPending；这一条守的是**回退口径**：
  // 老载荷（只有逐项标记）也要能标出「能力待接通」。
  assert.equal(REAL_METRIC_CARD_REGION.capabilityPending, true, "真实载荷现在带区域级标记");
  const legacy = { ...REAL_METRIC_CARD_REGION };
  delete legacy.capabilityPending;

  const { badge } = renderCard(api, legacy, null);
  assert.ok(badge.includes("能力待接通"), `未接通的区域不能被当成已接通：${badge}`);
  assert.ok(badge.includes("能耗成本"), "要说明是哪个指标：");
});

test("区域级 capabilityPending 存在时被采用（逐项没标也要标出来）", () => {
  const api = loadCardApi();
  const region = {
    ...REAL_METRIC_CARD_REGION,
    capabilityPending: true,
    payload: {
      ...REAL_METRIC_CARD_PAYLOAD,
      metrics: [{ ...REAL_METRIC_CARD_PAYLOAD.metrics[0], capabilityPending: false, pendingReason: null }],
    },
  };
  const { badge } = renderCard(api, region, null);
  assert.ok(badge.includes("能力待接通"), `区域级标记必须被采用：${badge}`);
});

test("区域级与逐项都没有：不标成未接通", () => {
  const api = loadCardApi();
  const region = {
    ...REAL_METRIC_CARD_REGION,
    capabilityPending: false,
    payload: {
      ...REAL_METRIC_CARD_PAYLOAD,
      metrics: [{ ...REAL_METRIC_CARD_PAYLOAD.metrics[0], capabilityPending: false, pendingReason: null }],
    },
  };
  const { badge } = renderCard(api, region, null);
  assert.equal(badge, "", "都接通了就不该有角标");
});

// ---------------------------------------------------------------- 表格截断

test("行被截断时判定出来，并给出「前 N 行」的行数", () => {
  const api = loadApi(["readTableTruncation", "buildTotalsByUnit", "readPositiveInt", "sumColumnValues"]);

  const table = SAMPLE_PAYLOAD.regions[3];
  const defRegion = { binding: { topN: 10 } };

  // 载荷自己说截断：直接采信。
  const explicit = api.__api.readTableTruncation(
    { ...table.payload, truncated: true, totalRows: 12 },
    defRegion,
    null
  );
  assert.equal(explicit.truncated, true);
  assert.equal(explicit.limit, 10);

  // 勾稽反推：行数达到了这张表声明的 topN，且行和明显小于同口径总量卡片 → 截断了。
  const totals = api.__api.buildTotalsByUnit([SAMPLE_PAYLOAD.regions[0]]);
  assert.equal(totals.get("kWh"), 5123.4);
  const byReconciliation = api.__api.readTableTruncation(table.payload, { binding: { topN: 2 } }, totals);
  assert.equal(byReconciliation.truncated, true);
  assert.equal(byReconciliation.limit, 2, "显示 2 行就是「前 2 行」");

  // 行和等于总量：没有截断，不标。
  const fullTable = {
    ...table.payload,
    rows: [
      { "ranking.device_name": "A-1 机架", "ranking.energy": 5123.4 },
    ],
  };
  assert.equal(api.__api.readTableTruncation(fullTable, { binding: { topN: 1 } }, totals), null);

  // 行数没到 topN：不能断言「还有别的行没显示」（对象本来就少，行和不等也可能是另一段范围）。
  assert.equal(api.__api.readTableTruncation(table.payload, defRegion, totals), null);

  // 没声明 topN 的表也不乱说。
  assert.equal(api.__api.readTableTruncation(table.payload, { binding: {} }, totals), null);
});

// ---------------------------------------------------------------- 徽标

test("徽标状态机：草稿「示意」→ 校验成功后「已校验 / 待接真数」", () => {
  const api = loadApi(["draftBadgeView"], [extractConst("VALIDATED_AGENT_STATES")]);
  api.session = makeSession();

  // 没有草稿、没有载荷。
  assert.equal(api.__api.draftBadgeView().key, "none");
  assert.equal(api.__api.draftBadgeView().text, "尚无草稿");

  // 有草稿、示意还没到：说「草稿」，不说「示意」（屏幕上还没有数）。
  api.session = makeSession({ definition: SAMPLE_DEFINITION });
  assert.equal(api.__api.draftBadgeView().text, "草稿");

  // 草稿阶段（右侧已是带数的报表）：「示意」。
  api.session = makeSession({
    definition: SAMPLE_DEFINITION,
    reportPreviewPayload: SAMPLE_PAYLOAD,
    state: "ANALYZING",
  });
  assert.equal(api.__api.draftBadgeView().text, "示意");
  assert.equal(api.__api.draftBadgeView().key, "simulated");

  // 校验进行中：不显示成功徽标，示意保持可见。
  api.session.state = "VALIDATING";
  assert.equal(api.__api.draftBadgeView().text, "示意");

  // 校验失败：同样不显示成功徽标。
  api.session.state = "VALIDATION_FAILED";
  assert.equal(api.__api.draftBadgeView().text, "示意");

  // 校验通过（状态机过了 VALIDATING）：「已校验 / 待接真数」。
  for (const state of ["PREVIEWING", "PREVIEW_READY", "RENDERING", "READY"]) {
    api.session.state = state;
    assert.equal(api.__api.draftBadgeView().text, "已校验 / 待接真数", state + " 阶段应是已校验徽标");
    assert.equal(api.__api.draftBadgeView().key, "validated");
  }
});

// ---------------------------------------------------------------- 静态契约

test("不再有「有数才展示」的三层占位分支", () => {
  assert.ok(!WORKBENCH_JS.includes("暂无数值"), "不应再有「暂无数值」占位分支");
  assert.ok(!WORKBENCH_JS.includes("表格样例占位"), "不应再有表格空行占位");
  assert.ok(!WORKBENCH_JS.includes("图表占位"), "不应再有图表空壳占位");
  assert.ok(!WORKBENCH_JS.includes("placeholder: true"), "不应再有载荷级占位分支");
});

test("指标卡不再写死破折号", () => {
  // 载荷里的 value 缺失时才显示破折号；代码里不再有一处写死的 `—<span` 卡片。
  assert.ok(!/text-2xl font-semibold text-slate-900 mt-1">—/.test(WORKBENCH_JS), "指标卡不应写死破折号");
});

test("图表的纵轴名与系列名都从载荷读，不硬编码", () => {
  const chartBlock = WORKBENCH_JS.slice(
    WORKBENCH_JS.indexOf("function renderChartPayload("),
    WORKBENCH_JS.indexOf("// -------- 会话导航栏")
  );
  assert.ok(chartBlock.includes("name: unit || undefined"), "纵轴名应取自载荷单位");
  assert.ok(!/name: "kWh"/.test(chartBlock), "纵轴名不应硬编码 kWh");
  assert.ok(!/name: "耗电"/.test(chartBlock), "系列名不应硬编码耗电");
});

test("只有一个渲染入口：不再按会话状态切换渲染入口，也不再单独渲染预览面板", () => {
  assert.ok(!WORKBENCH_JS.includes("function renderFinalReport"), "独立的预览面板渲染器应下线");
  assert.ok(!WORKBENCH_JS.includes("function renderFinalRegion"), "独立的预览面板渲染器应下线");
  assert.ok(WORKBENCH_JS.includes("function renderReportPanel("), "统一渲染入口应保留");
  // 面板只从示意载荷渲染：不从试运行结果渲染。
  const panelBlock = WORKBENCH_JS.slice(
    WORKBENCH_JS.indexOf("function renderReportPanel("),
    WORKBENCH_JS.indexOf("function renderPanelHeader(")
  );
  assert.ok(panelBlock.includes("session.reportPreviewPayload"), "面板应从示意载荷渲染");
  assert.ok(!panelBlock.includes("session.previewPayload"), "面板不应从试运行结果渲染");
});

test("载荷的读写点：聊天响应顶层与会话快照各一处，两个键互不替代", () => {
  assert.ok(
    WORKBENCH_JS.includes("data?.reportPreviewPayload"),
    "应从聊天响应顶层读 reportPreviewPayload"
  );
  assert.ok(
    WORKBENCH_JS.includes("detail.snapshot?.reportPreviewPayload"),
    "应从会话快照读 reportPreviewPayload"
  );
  // 试运行结果仍然写进 previewPayload（既有键，语义不变）。
  assert.ok(WORKBENCH_JS.includes("session.previewPayload = payload"), "试运行结果仍写 previewPayload");
  assert.ok(WORKBENCH_JS.includes("detail.snapshot?.previewPayload"), "会话快照里的试运行结果仍读 previewPayload");
});

test("示意载荷不落任何持久化位置", () => {
  const storeCalls = WORKBENCH_JS.match(/localStorage\.(setItem|removeItem)\([^)]*reportPreviewPayload[^)]*\)/g) || [];
  assert.equal(storeCalls.length, 0, "模拟载荷只存在于响应与内存，不写 localStorage");
  const sessionStoreCalls = WORKBENCH_JS.match(/sessionStorage[^)]*reportPreviewPayload[^)]*/g) || [];
  assert.equal(sessionStoreCalls.length, 0, "模拟载荷不写 sessionStorage");
});

test("标题区按 role 跳过，不单独占一块", () => {
  assert.ok(
    WORKBENCH_JS.includes('region?.role === "REPORT_TITLE"'),
    "应按 role 识别标题区"
  );
  assert.ok(
    WORKBENCH_JS.includes("regions.filter((region) => !isTitleRegion(region))"),
    "渲染时应过滤标题区"
  );
});

test("面板顶部有模拟业务数据说明，每块有角标", () => {
  assert.ok(WORKBENCH_JS.includes("data-simulated-note"), "面板顶部应有模拟数据说明");
  assert.ok(WORKBENCH_JS.includes("模拟业务数据"), "说明里要写明是模拟业务数据");
  assert.ok(WORKBENCH_JS.includes('data-region-badge="simulated"'), "每块应有角标");
  assert.ok(WORKBENCH_JS.includes('dataset.regionCapability = "pending"'), "未接通的指标要额外写明");
});

test("页面仍然声明空态提示位（草稿在但示意没到时说清楚）", () => {
  assert.ok(WORKBENCH_HTML.includes("data-preview-empty-hint"), "空态应有可更新的提示位");
});

// ---------------------------------------------------------------- 图表渲染后端
//
// 图表有两条渲染后端：
//   - ECharts：CDN 可达时走真图表（单位画在纵轴名上，DOM 里抓不到）；
//   - SVG 兜底：CDN 挂了时走方案模块的渲染器（单位写在图下方的说明里）。
//
// 两条后端都要把「单位来自载荷」表达出来，而且要用**同一种**可断言的标记
// （data-chart-unit，属性值就是载荷里的单位）—— 否则同一条断言只能覆盖其中一条路，
// 另一条后端等于没被测过。
//
// 这里仍然用 node:vm 把 renderChartPayload 原样抽出来跑（源码原文，不是复制品），
// 配一个够用的假 DOM 与假 echarts，把两条分支都走一遍。

/** 假 DOM 元素：只够 renderChartPayload 用（建元素、挂子节点、dataset、textContent）。 */
function makeFakeElement(tagName = "div") {
  return {
    tagName: String(tagName).toUpperCase(),
    className: "",
    style: {},
    dataset: {},
    textContent: "",
    children: [],
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    setAttribute(name, value) {
      this[name] = value;
    },
    getAttribute(name) {
      return this[name] ?? null;
    },
  };
}

function makeFakeDom() {
  return { createElement: (tagName) => makeFakeElement(tagName) };
}

/** 假 echarts：把 setOption 的参数截下来供断言。 */
function makeFakeEcharts(capture) {
  return {
    init(el) {
      capture.el = el;
      return {
        setOption(option) {
          capture.option = option;
        },
        dispose() {},
      };
    },
  };
}

/** 容器里带 data-chart-unit 标记的元素。 */
function unitMarkers(container) {
  return container.children.filter(
    (child) => child.dataset && child.dataset.chartUnit !== undefined
  );
}

/** 图表渲染要用的那几段源码（原样抽出来）。 */
function loadChartApi() {
  return loadApi(["renderChartPayload", "hasEcharts", "readPayloadUnit"], [
    extractConstBlock("CHART_KIND_TO_COMPONENT_TYPE"),
    extractConst("CHART_SERIES_COLORS"),
    extractConst("_chartInstances"),
    extractConst("esc"),
  ]);
}

test("ECharts 路径：纵轴名取载荷单位，并产出与 SVG 路径同一种单位标记", () => {
  const api = loadChartApi();
  const dom = makeFakeDom();
  const capture = {};
  api.document = dom;
  api.window.echarts = makeFakeEcharts(capture);

  const region = {
    regionId: "region_trend",
    name: "耗电趋势",
    componentType: "CHART",
    payload: {
      chartKind: "LINE",
      xLabels: ["09:00", "10:00", "11:00"],
      yValues: [210.5, 233.1, 198.7],
      unit: "kWh",
    },
  };
  const unit = api.__api.readPayloadUnit(region, region.payload, null);
  assert.equal(unit, "kWh");

  const container = dom.createElement("div");
  api.__api.renderChartPayload(container, region, region.payload, unit);

  // 纵轴名来自载荷（不是写死的 kWh）。
  assert.equal(capture.option.yAxis.name, "kWh");
  assert.equal(capture.option.series.length, 1);
  assert.equal(capture.option.series[0].name, "kWh", "单指标图没有指标名时退回单位");

  // 单位标记：属性值就是载荷里的单位，文本里也写着这个单位。
  const markers = unitMarkers(container);
  assert.equal(markers.length, 1, "ECharts 路径也要产出 [data-chart-unit] 标记");
  assert.equal(markers[0].dataset.chartUnit, "kWh");
  assert.ok(markers[0].textContent.includes("kWh"), "标记的文本要写明单位");
  assert.ok(markers[0].textContent.includes("3"), "标记要写明数据点数");
});

test("ECharts 路径：换一张图，单位跟着载荷变（不写死 kWh）", () => {
  const api = loadChartApi();
  const dom = makeFakeDom();
  const capture = {};
  api.document = dom;
  api.window.echarts = makeFakeEcharts(capture);

  // 总电费那张图：单位是元，纵轴名与标记都必须跟着变。
  const cost = {
    regionId: "region_cost",
    name: "总电费趋势",
    componentType: "CHART",
    payload: {
      chartKind: "LINE",
      xLabels: ["09:00", "10:00"],
      yValues: [12.5, 13.5],
      unit: "元",
    },
  };
  const costUnit = api.__api.readPayloadUnit(cost, cost.payload, null);
  const costContainer = dom.createElement("div");
  api.__api.renderChartPayload(costContainer, cost, cost.payload, costUnit);

  assert.equal(capture.option.yAxis.name, "元");
  const markers = unitMarkers(costContainer);
  assert.equal(markers.length, 1);
  assert.equal(markers[0].dataset.chartUnit, "元");
  assert.ok(markers[0].textContent.includes("元"));
  assert.ok(!markers[0].textContent.includes("kWh"), "不能把别的单位顶上来");

  // 载荷没带单位：既不写单位，也不造一个标记出来。
  const bare = {
    regionId: "region_bare",
    name: "某指标",
    componentType: "CHART",
    payload: { chartKind: "LINE", xLabels: ["09:00"], yValues: [1] },
  };
  const bareUnit = api.__api.readPayloadUnit(bare, bare.payload, null);
  assert.equal(bareUnit, "");
  const bareContainer = dom.createElement("div");
  api.__api.renderChartPayload(bareContainer, bare, bare.payload, bareUnit);
  assert.equal(capture.option.yAxis.name, undefined, "没单位就不写纵轴名");
  assert.equal(unitMarkers(bareContainer).length, 0, "没单位就不产出单位标记");
});

test("ECharts 路径：多指标图，系列名各取载荷里的指标名，单位按载荷链读", () => {
  const api = loadChartApi();
  const dom = makeFakeDom();
  const capture = {};
  api.document = dom;
  api.window.echarts = makeFakeEcharts(capture);

  const region = {
    regionId: "region_multi",
    name: "多指标对比",
    componentType: "CHART",
    payload: {
      chartKind: "LINE",
      xLabels: ["09:00", "10:00"],
      series: [
        { metricKey: "energy", label: "总耗电", unit: "kWh", yValues: [210.5, 233.1] },
        { metricKey: "cost", label: "总电费", unit: "元", yValues: [12.5, 13.5] },
      ],
    },
  };
  // 载荷顶层没带 unit：单位按回退链取第一条序列的（各序列的单位没被丢掉）。
  const unit = api.__api.readPayloadUnit(region, region.payload, null);
  assert.equal(unit, "kWh");

  const container = dom.createElement("div");
  api.__api.renderChartPayload(container, region, region.payload, unit);

  assert.equal(capture.option.series.length, 2);
  assert.equal(capture.option.series[0].name, "总耗电", "系列名取各自的指标名");
  assert.equal(capture.option.series[1].name, "总电费");
  assert.equal(capture.option.yAxis.name, "kWh");

  const markers = unitMarkers(container);
  assert.equal(markers.length, 1);
  assert.equal(markers[0].dataset.chartUnit, "kWh");

  // 顶层写了单位就用顶层的（各序列的单位仍在载荷里，没被顶替）。
  const withTopUnit = { ...region, payload: { ...region.payload, unit: "kW" } };
  assert.equal(api.__api.readPayloadUnit(withTopUnit, withTopUnit.payload, null), "kW");
});

test("SVG 兜底路径：补出同一种单位标记（属性值就是载荷里的单位）", () => {
  const api = loadChartApi();
  const dom = makeFakeDom();
  api.document = dom;
  api.window.echarts = undefined; // CDN 挂了：没有 echarts

  // 方案模块的渲染器：柱条那条不会自带「单位：」说明（折线那条自带）。
  api.window.WorkbenchPlan = {
    renderChart: () => {
      const node = dom.createElement("div");
      node.textContent = "A-1 机架120.5A-2 机架98.2";
      return node;
    },
  };

  const region = {
    regionId: "region_rank_chart",
    name: "设备耗电对比",
    componentType: "CHART",
    payload: {
      chartKind: "BAR",
      xLabels: ["A-1 机架", "A-2 机架"],
      yValues: [120.5, 98.2],
      unit: "kWh",
    },
  };
  const unit = api.__api.readPayloadUnit(region, region.payload, null);
  const container = dom.createElement("div");
  api.__api.renderChartPayload(container, region, region.payload, unit);

  const markers = unitMarkers(container);
  assert.equal(markers.length, 1, "SVG 兜底路径也要产出 [data-chart-unit] 标记");
  assert.equal(markers[0].dataset.chartUnit, "kWh", "标记的属性值与 ECharts 路径一致");
  assert.ok(markers[0].textContent.includes("单位：kWh"));
});

test("SVG 兜底路径：渲染器自己写了单位时，不重复再写一遍", () => {
  const api = loadChartApi();
  const dom = makeFakeDom();
  api.document = dom;
  api.window.echarts = undefined;

  // 折线渲染器自带「单位：…，共 N 个数据点」：这时不重复补一条。
  api.window.WorkbenchPlan = {
    renderChart: () => {
      const node = dom.createElement("div");
      node.textContent = "单位：kWh，共 2 个数据点";
      return node;
    },
  };

  const region = {
    regionId: "region_trend",
    name: "耗电趋势",
    componentType: "CHART",
    payload: {
      chartKind: "LINE",
      xLabels: ["09:00", "10:00"],
      yValues: [210.5, 233.1],
      unit: "kWh",
    },
  };
  const unit = api.__api.readPayloadUnit(region, region.payload, null);
  const container = dom.createElement("div");
  api.__api.renderChartPayload(container, region, region.payload, unit);

  const text = container.children.map((child) => child.textContent).join("|");
  assert.ok(text.includes("单位：kWh"), "单位要写在图上");
  assert.equal((text.match(/单位：/g) || []).length, 1, "单位只写一遍，不重复");
});

test("两条渲染后端产出同一种单位标记，且不写死单位", () => {
  const chartBlock = WORKBENCH_JS.slice(
    WORKBENCH_JS.indexOf("function renderChartPayload("),
    WORKBENCH_JS.indexOf("// -------- 会话导航栏")
  );
  const split = chartBlock.indexOf("// ECharts 不可用");
  assert.ok(split > 0, "应能分出 ECharts 分支与 SVG 兜底分支");

  // ECharts 分支也要产出标记：否则同一条断言只能覆盖 SVG 兜底那条路。
  const echartsBranch = chartBlock.slice(0, split);
  assert.ok(
    echartsBranch.includes("dataset.chartUnit"),
    "ECharts 分支应产出 data-chart-unit 标记（单位只在纵轴名上时 DOM 抓不到）"
  );
  // SVG 兜底分支同样产出标记：两条后端用同一套选择器断言。
  const svgBranch = chartBlock.slice(split);
  assert.ok(svgBranch.includes("dataset.chartUnit"), "SVG 兜底分支应产出 data-chart-unit 标记");

  // 单位一律来自载荷：整个图表块里不出现写死的单位字面量。
  assert.ok(!/["']kWh["']/.test(chartBlock), "图表渲染块不应出现写死的 kWh");
  assert.ok(!/["']耗电["']/.test(chartBlock), "图表渲染块不应出现写死的耗电");
});

// ---------------------------------------------------------------- 恢复会话失败（#53）
//
// 后端在「模拟载荷生成不出来」时（典型：方案要「按周统计」，示意数据只能按
// 分钟 / 小时 / 天生成），把原因挂在会话读取响应的 snapshot.error 上
// （code + 人话 message），而不是静默返回空快照。
//
// 前端要把这个原因**显示出来**：
//   - 右侧面板顶上写明原因，不是一片空白；
//   - 不伪造载荷、不画空壳卡片、不显示成「已校验 / 成功」；
//   - 正常会话（有载荷、无 error）的行为一点不变。
//
// 聊天响应自带 error（AgentResponse.error）那条路径本来就有提示
// （对话区一条 AI 回复），这里补一条回归断言把它钉住。

/** 后端在恢复失败时给回来的形状（实测响应）。 */
const RESTORE_ERROR = {
  code: "UNRESOLVED_GRAIN",
  message:
    "示意数据没有生成出来：无法解析的时间粒度：'WEEK'；受支持的取值为 MINUTE / HOUR / DAY。示意数据只能按这几个粒度生成，请把方案里的时间粒度改成其中之一后重试。",
};

/** 假的面板 / 气泡节点：够用（清空、挂子节点、按属性或 dataset 找节点）。 */
function makeFakePanelNode(tagName = "div") {
  const el = {
    tagName: String(tagName).toUpperCase(),
    className: "",
    classList: { add() {}, remove() {}, toggle() {} },
    dataset: {},
    textContent: "",
    children: [],
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    setAttribute(name, value) {
      this[name] = value;
    },
    getAttribute(name) {
      return this[name] ?? null;
    },
    querySelector() {
      return null;
    },
  };
  let html = "";
  Object.defineProperty(el, "innerHTML", {
    get() {
      return html;
    },
    set(value) {
      html = value;
      // 面板每次渲染前都先 innerHTML=""：子节点要跟着清掉。
      if (!value) el.children.length = 0;
    },
  });
  return el;
}

/** 按属性标记深度优先找节点（找不到返回 null）。 */
function findByAttr(node, attr) {
  for (const child of node.children || []) {
    if (child[attr] !== undefined) return child;
    const found = findByAttr(child, attr);
    if (found) return found;
  }
  return null;
}

/** 按 dataset 里的键深度优先找节点（找不到返回 null）。 */
function findByDataset(node, key) {
  for (const child of node.children || []) {
    if (child.dataset && child.dataset[key] !== undefined) return child;
    const found = findByDataset(child, key);
    if (found) return found;
  }
  return null;
}

/** 节点及其后代的文本。 */
function textOf(node) {
  let text = node.textContent || "";
  for (const child of node.children || []) text += textOf(child);
  return text;
}

/** 从源码里切出一段配平的大括号块（从 marker 开始）。 */
function extractBlock(marker) {
  const at = WORKBENCH_JS.indexOf(marker);
  assert.ok(at >= 0, `源码里找不到 ${marker}`);
  let depth = 0;
  for (let i = at; i < WORKBENCH_JS.length; i += 1) {
    if (WORKBENCH_JS[i] === "{") depth += 1;
    else if (WORKBENCH_JS[i] === "}") {
      depth -= 1;
      if (depth === 0) return WORKBENCH_JS.slice(at, i + 1);
    }
  }
  throw new Error(`${marker} 的大括号没有配平`);
}

/**
 * 面板渲染用到的源码片段（全部来自工作台主脚本的原文）。
 *
 * 面板头与区域卡片用桩函数代替：这一层要验的是「失败时面板画不画区域卡片」，
 * 不是卡片长什么样（卡片由上面的用例守着）。
 */
function loadPanelApi(sessionState) {
  const root = makeFakePanelNode("div");
  const empty = makeFakePanelNode("div");
  const renderedRegions = [];
  const api = loadApi(
    ["renderReportPanel", "renderPreviewErrorNotice", "readSessionRestoreError", "formatAgentError"],
    [
      "const previewRoot = globalThis.__previewRoot;",
      "const previewEmpty = globalThis.__previewEmpty;",
      "const _chartInstances = [];",
      "const requestAnimationFrame = (fn) => fn();",
      "function destroyCharts() {}",
      "function isTitleRegion(region) { return region?.role === 'REPORT_TITLE'; }",
      "function setPreviewEmptyHint(kind) { globalThis.__emptyHint = kind; }",
      "function buildTotalsByUnit() { return {}; }",
      "function renderPanelHeader() { const head = globalThis.__fakeNode(); head.dataset.panelHeader = '1'; return head; }",
      "function renderRegionCard(region) { globalThis.__renderedRegions.push(region.regionId); const card = globalThis.__fakeNode(); card.dataset.regionId = region.regionId; return card; }",
      "function syncPreviewEditability() {}",
    ],
    { renderReportPanel: extractFunctionDeep },
    {
      __previewRoot: root,
      __previewEmpty: empty,
      __renderedRegions: renderedRegions,
      __fakeNode: () => makeFakePanelNode("div"),
      document: { createElement: (tagName) => makeFakePanelNode(tagName) },
    }
  );
  api.session = sessionState;
  return { api, root, empty, renderedRegions };
}

/** 对话区用的源码片段（AI 气泡 + 失败提示，原文）。 */
function loadChatApi() {
  const chatScroll = makeFakePanelNode("div");
  const api = loadApi(
    ["aiBubble", "appendAiText", "appendAgentError", "formatAgentError"],
    [
      extractConst("esc"),
      "const chatScroll = globalThis.__chatScroll;",
      "const chatBubbleObserver = { observe() {} };",
      "function scrollChatBottom() {}",
    ],
    {},
    {
      __chatScroll: chatScroll,
      document: { createElement: (tagName) => makeFakePanelNode(tagName) },
    }
  );
  return { api, chatScroll };
}

test("恢复会话的失败原因从 snapshot.error 里读出来（没有这个键就是正常会话）", () => {
  const api = loadApi(["readSessionRestoreError"]);

  const failed = api.__api.readSessionRestoreError({ snapshot: { error: RESTORE_ERROR } });
  assert.equal(failed.code, "UNRESOLVED_GRAIN");
  assert.ok(failed.message.includes("无法解析的时间粒度"), "原因要原样带回来");

  // 正常会话：响应里没有 error 键。
  assert.equal(api.__api.readSessionRestoreError({ snapshot: { reportPreviewPayload: SAMPLE_PAYLOAD } }), null);
  assert.equal(api.__api.readSessionRestoreError({ snapshot: {} }), null);
  assert.equal(api.__api.readSessionRestoreError({}), null);
  assert.equal(api.__api.readSessionRestoreError(null), null);
  // 空值 / 不是对象的 error 不当作失败原因（不凭空造一条出来）。
  assert.equal(api.__api.readSessionRestoreError({ snapshot: { error: null } }), null);
  assert.equal(api.__api.readSessionRestoreError({ snapshot: { error: {} } }), null);
  assert.equal(api.__api.readSessionRestoreError({ snapshot: { error: "boom" } }), null);
});

test("恢复会话失败：右侧面板写明原因，且一个区域卡片都不画（不伪造载荷）", () => {
  const { api, root, renderedRegions } = loadPanelApi(
    makeSession({
      definition: SAMPLE_DEFINITION,
      reportPreviewPayload: null,
      reportPreviewError: RESTORE_ERROR,
    })
  );
  api.__api.renderReportPanel();

  // 原因出现在面板上（刷新后也还在，不是空白）。
  const notice = findByAttr(root, "data-preview-error");
  assert.ok(notice, "恢复失败时右侧要有原因提示，不能一片空白");
  const text = textOf(notice);
  assert.ok(text.includes("无法解析的时间粒度"), `原因要写在面板上：${text}`);
  assert.ok(text.includes("UNRESOLVED_GRAIN"), `错误码也要写明：${text}`);

  // 不伪造载荷、不画空壳卡片：一个区域卡片都不画。
  assert.equal(renderedRegions.length, 0, "失败时不该画区域卡片");
  assert.equal(findByDataset(root, "regionId"), null, "面板里不该有区域节点");
  // 只剩原因条：不摆「示意数据还没生成」的空态（失败时那句话是错的）。
  assert.equal(root.children.length, 1, "面板里只应有原因条");
});

test("正常会话（有载荷、无 error）：不出现错误提示，既有渲染不变", () => {
  const { api, root, renderedRegions } = loadPanelApi(
    makeSession({
      definition: SAMPLE_DEFINITION,
      reportPreviewPayload: SAMPLE_PAYLOAD,
      reportPreviewError: null,
    })
  );
  api.__api.renderReportPanel();

  assert.equal(findByAttr(root, "data-preview-error"), null, "正常会话不该出现失败提示");
  assert.deepEqual(
    renderedRegions,
    SAMPLE_PAYLOAD.regions.map((region) => region.regionId),
    "各区域照旧渲染"
  );
  assert.ok(findByDataset(root, "panelHeader"), "面板头照旧渲染");
});

test("聊天响应带 error：对话区把原因显示出来（回归钉住）", () => {
  const { api, chatScroll } = loadChatApi();
  api.__api.appendAgentError(RESTORE_ERROR);

  const bubble = chatScroll.children[0];
  assert.ok(bubble, "失败原因应当以一条 AI 回复出现在对话区");
  assert.ok(bubble.innerHTML.includes("UNRESOLVED_GRAIN"), `错误码要写明：${bubble.innerHTML}`);
  assert.ok(bubble.innerHTML.includes("无法解析的时间粒度"), `原因要写明：${bubble.innerHTML}`);
});

test("恢复会话那条路真的读了 snapshot.error，并把原因摆到面板与对话区", () => {
  const restoreBlock = extractFunctionDeep("restoreSession");
  assert.ok(restoreBlock.includes("readSessionRestoreError(detail)"), "恢复会话要读 snapshot.error");
  assert.ok(
    restoreBlock.includes("appendAgentError(session.reportPreviewError)"),
    "恢复失败要在对话区说清楚原因"
  );
  assert.ok(restoreBlock.includes("renderReportPanel()"), "右侧面板照旧由那一个入口渲染");
});

test("聊天响应带 error 那条路仍然把原因显示出来（接线钉住）", () => {
  const branch = extractBlock("if (data.error) {");
  assert.ok(branch.includes("appendAgentError(data.error)"), "聊天响应的失败原因要显示出来");
});
