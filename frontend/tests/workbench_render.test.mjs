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

/** 从源码里原样抽出一个函数定义（按大括号配平，含模板字面量里的 ${}）。 */
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

/** 抽出一条顶层 const 语句（到行尾的 ;）。 */
function extractConst(name) {
  const pattern = new RegExp(`^const ${name} = .*;$`, "m");
  const match = WORKBENCH_JS.match(pattern);
  assert.ok(match, `源码里找不到 const ${name}`);
  return match[0];
}

/** 把选中的片段放进沙箱跑一遍，返回它们组成的 API。 */
function loadApi(names, extraStatements = []) {
  const snippets = [...extraStatements, ...names.map(extractFunction)].join("\n\n");
  const sandbox = {
    console: { warn() {}, error() {}, log() {} },
    session: null, // 每个用例自己填
    window: {},
    document: {},
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
