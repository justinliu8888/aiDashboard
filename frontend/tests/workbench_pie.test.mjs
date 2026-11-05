// 饼图渲染（2026-11-04 需求：PIE 不能被画成折线 / 柱条）。
//
// 运行：node --test frontend/tests/workbench_pie.test.mjs
// 不依赖任何第三方包（Node 内置 test runner + node:vm）。
//
// ── 被测的缝 ───────────────────────────────────────────────────
// 两条渲染后端都要出**扇区**：
//   - ECharts 路径（frontend/pages/workbench.js 的 renderChartPayload / renderPiePayload）
//   - SVG 兜底路径（frontend/pages/workbench_plan.js 的 renderChart，pie_chart 分支）
//
// 两条路径都用**源码原文**跑（node:vm 抽出来），不是复制品。
// 另外校验「模拟数据」在图上标出来（载荷里的 simulated 标记）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const require = createRequire(import.meta.url);

const WORKBENCH_JS = readFileSync(new URL("../pages/workbench.js", import.meta.url), "utf8");
const WORKBENCH_PLAN_JS = readFileSync(new URL("../pages/workbench_plan.js", import.meta.url), "utf8");

// ---------------------------------------------------------------- 抽取工具

function extractFunctionDeep(source, name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  assert.ok(start >= 0, `源码里找不到函数 ${name}`);
  let depth = 0;
  let opened = false;
  let inString = null;
  for (let i = start; i < source.length; i += 1) {
    const ch = source[i];
    const prev = source[i - 1];
    if (inString) {
      if (ch === inString && prev !== "\\") inString = null;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") {
      inString = ch;
      continue;
    }
    if (ch === "{") {
      depth += 1;
      opened = true;
    } else if (ch === "}") {
      depth -= 1;
      if (opened && depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`函数 ${name} 的大括号没有配平`);
}

function extractConst(source, name) {
  const pattern = new RegExp(`^const ${name} = .*;$`, "m");
  const match = source.match(pattern);
  assert.ok(match, `源码里找不到 const ${name}`);
  return match[0];
}

/** 抽出一条顶层 const 对象 / 数组语句（大括号配平，允许跨行）。 */
function extractConstBlock(source, name) {
  const marker = `const ${name} =`;
  const start = source.indexOf(marker);
  assert.ok(start >= 0, `源码里找不到 const ${name}`);
  let depth = 0;
  let opened = false;
  for (let i = start; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === "{" || ch === "[") {
      depth += 1;
      opened = true;
    } else if (ch === "}" || ch === "]") {
      depth -= 1;
      if (opened && depth === 0) return source.slice(start, i + 1);
    } else if (ch === ";" && opened && depth === 0) {
      return source.slice(start, i + 1);
    }
  }
  throw new Error(`const ${name} 的括号没有配平`);
}

// ---------------------------------------------------------------- 假 DOM

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
  return {
    createElement: (tagName) => makeFakeElement(tagName),
    createElementNS: (ns, tagName) => makeFakeElement(tagName),
  };
}

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

/** 递归收集节点树里的所有文字。 */
function collectText(node) {
  let text = node.textContent || "";
  for (const child of node.children || []) {
    text += " " + collectText(child);
  }
  return text;
}

// ---------------------------------------------------------------- ECharts 路径

function loadChartApi() {
  const snippets = [
    extractConstBlock(WORKBENCH_JS, "CHART_KIND_TO_COMPONENT_TYPE"),
    extractConst(WORKBENCH_JS, "CHART_SERIES_COLORS"),
    extractConst(WORKBENCH_JS, "_chartInstances"),
    extractFunctionDeep(WORKBENCH_JS, "readPayloadUnit"),
    extractFunctionDeep(WORKBENCH_JS, "appendChartNote"),
    extractFunctionDeep(WORKBENCH_JS, "renderPiePayload"),
    extractFunctionDeep(WORKBENCH_JS, "renderChartPayload"),
    extractFunctionDeep(WORKBENCH_JS, "hasEcharts"),
  ].join("\n\n");
  const sandbox = {
    console: { warn() {}, error() {}, log() {} },
    session: null,
    window: {},
    document: {},
    // 兜底分支才用得到；饼图两条主路径都不碰它。
    esc: (value) => String(value ?? ""),
  };
  vm.createContext(sandbox);
  vm.runInContext(
    snippets + "\n;globalThis.__api = { renderChartPayload, hasEcharts, readPayloadUnit };",
    sandbox
  );
  return sandbox;
}

const ALARM_LEVEL_PAYLOAD = {
  chartKind: "PIE",
  xLabels: ["严重", "重要", "一般", "提示"],
  yValues: [18, 27, 41, 12],
  xField: "alertLevel",
  yField: "alertCount",
  unit: "条",
  simulated: true,
  categoryLabel: "告警等级",
  statistic: "按告警等级统计告警数量",
  dataSource: "simulated",
};

test("ECharts 路径：PIE 真的画成饼图（不是折线、不是柱条）", () => {
  const api = loadChartApi();
  const dom = makeFakeDom();
  const capture = {};
  api.document = dom;
  api.window.echarts = makeFakeEcharts(capture);

  const region = {
    regionId: "region_alarm_level",
    name: "告警等级分布",
    componentType: "CHART",
    payload: ALARM_LEVEL_PAYLOAD,
  };
  const unit = api.__api.readPayloadUnit(region, region.payload, null);
  assert.equal(unit, "条");

  const container = dom.createElement("div");
  api.__api.renderChartPayload(container, region, region.payload, unit);

  assert.ok(capture.option, "ECharts 路径必须真的建图");
  const series = capture.option.series;
  assert.equal(series.length, 1);
  assert.equal(series[0].type, "pie", "PIE 必须是 pie，不能落成 line / bar");
  // 饼图没有坐标轴。
  assert.equal(capture.option.xAxis, undefined, "饼图不该带横轴");
  assert.equal(capture.option.yAxis, undefined, "饼图不该带纵轴");
  // 每个分类一个扇区，标签与数值一一对应。
  assert.equal(series[0].data.length, 4);
  assert.deepEqual(
    series[0].data.map((item) => item.name),
    ["严重", "重要", "一般", "提示"]
  );
  assert.deepEqual(
    series[0].data.map((item) => item.value),
    [18, 27, 41, 12]
  );
});

test("ECharts 路径：标签与数值来自载荷（不是写死的设备能耗）", () => {
  const api = loadChartApi();
  const dom = makeFakeDom();
  const capture = {};
  api.document = dom;
  api.window.echarts = makeFakeEcharts(capture);

  const region = {
    regionId: "region_alarm_region",
    name: "告警区域分布",
    componentType: "CHART",
    payload: {
      chartKind: "PIE",
      xLabels: ["华北机房", "华东机房", "华南机房", "西部机房"],
      yValues: [26, 34, 22, 16],
      unit: "条",
      simulated: true,
    },
  };
  const container = dom.createElement("div");
  api.__api.renderChartPayload(container, region, region.payload, "条");

  const data = capture.option.series[0].data;
  assert.equal(data[0].name, "华北机房");
  assert.equal(data[1].value, 34);
  // 不是拿设备能耗数据冒充告警分布。
  assert.ok(!data.some((item) => String(item.name).includes("耗电")));
});

test("ECharts 路径：模拟数据在图上标出来", () => {
  const api = loadChartApi();
  const dom = makeFakeDom();
  const capture = {};
  api.document = dom;
  api.window.echarts = makeFakeEcharts(capture);

  const region = {
    regionId: "region_alarm_level",
    name: "告警等级分布",
    componentType: "CHART",
    payload: ALARM_LEVEL_PAYLOAD,
  };
  const container = dom.createElement("div");
  api.__api.renderChartPayload(container, region, region.payload, "条");

  const markers = container.children.filter(
    (child) => child.dataset && child.dataset.chartSimulated !== undefined
  );
  assert.equal(markers.length, 1, "模拟数据要有可断言的标记");
  assert.equal(markers[0].dataset.chartSimulated, "true");
  assert.ok(markers[0].textContent.includes("模拟数据"), "说明里要写明是模拟数据");
});

// ---------------------------------------------------------------- SVG 兜底路径

function loadPlanApi() {
  // workbench_plan.js 是 CommonJS 模块：直接 require 拿导出的 Plan。
  const plan = require("../pages/workbench_plan.js");
  assert.ok(plan && typeof plan.renderChart === "function", "方案模块要导出 renderChart");
  return plan;
}

test("SVG 兜底路径：pie_chart 画的是扇区，不是横条", () => {
  const api = loadPlanApi();
  assert.ok(api && typeof api.renderChart === "function", "方案模块要暴露 renderChart");
  const dom = makeFakeDom();
  const node = api.renderChart(dom, {
    componentType: "pie_chart",
    title: "告警等级分布",
    data: {
      xLabels: ["严重", "重要", "一般", "提示"],
      yValues: [18, 27, 41, 12],
      unit: "条",
      simulated: true,
    },
  });

  // 扇区是 <path>（带圆弧），不是 <div> 横条。
  const svg = node.children.find((child) => child.tagName === "SVG");
  assert.ok(svg, "饼图兜底要产出 <svg>");
  const paths = svg.children.filter((child) => child.tagName === "PATH");
  assert.equal(paths.length, 4, "四个分类各一个扇区");
  const first = paths[0];
  assert.ok(first.getAttribute("d").includes("A "), "扇区路径必须带圆弧指令");
  assert.ok(first.getAttribute("d").startsWith("M "), "扇区路径必须从圆心出发");
  // 扇区带分类说明（<title>）：标签与数值一一对应。
  assert.equal(first.children.length, 1);
  assert.equal(first.children[0].textContent, "严重: 18 条");

  // 不是横条：横条那套是 div.template-chart-row + span.template-chart-fill。
  const bars = node.children.filter(
    (child) => child.className === "template-chart-row" && child.children.some(
      (sub) => sub.className === "template-chart-track"
    )
  );
  assert.equal(bars.length, 0, "饼图不能画成横条");

  // 分类与数值逐行列出来，且标注模拟。
  const text = collectText(node);
  assert.ok(text.includes("严重"));
  assert.ok(text.includes("18"));
  assert.ok(text.includes("模拟数据"), "SVG 兜底也要标出模拟数据");
});

// ---------------------------------------------------------------- 契约

test("源码里 PIE 不再落到折线分支", () => {
  const marker = 'type: kind === "BAR" ? "bar" : "line"';
  assert.ok(
    WORKBENCH_JS.includes(marker),
    "非饼图的折线 / 柱条分支应保留原样"
  );
  // 饼图必须**先**被单独拦出来。
  const pieIndex = WORKBENCH_JS.indexOf('if (kind === "PIE")');
  const lineIndex = WORKBENCH_JS.indexOf(marker);
  assert.ok(pieIndex > 0, "renderChartPayload 里要有 PIE 的专门分支");
  assert.ok(pieIndex < lineIndex, "PIE 分支必须在折线分支之前");
});

test("SVG 兜底里有 pie_chart 的扇区分支", () => {
  assert.ok(
    WORKBENCH_PLAN_JS.includes('region.componentType === "pie_chart"'),
    "workbench_plan.js 要有 pie_chart 分支"
  );
});
