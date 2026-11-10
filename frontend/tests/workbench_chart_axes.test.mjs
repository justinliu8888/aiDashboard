// 图表坐标轴：轴标题排版（11-02）+ 参考线与数值轴范围（11-03）。
//
// 运行：node --test frontend/tests/workbench_chart_axes.test.mjs
// 不依赖任何第三方包（Node 内置 test runner + node:vm）。
//
// ── 被测的缝 ───────────────────────────────────────────────────
// 被测对象是 **源码原文**（frontend/pages/workbench_chart.js），用 node:vm 把整个
// 模块跑在假 DOM + 假 ECharts 上，拿到它真正交给 ECharts 的 option，
// 再对 option 断言 —— 不是把实现抄一份到测试里。
//
// ── 断言口径（和实现里的口径一一对应）────────────────────────────
// 11-02（轴标题被裁）：
//   * 横轴标题 **居中**放在刻度下面 → option.xAxis.nameLocation === "middle"
//     （ECharts 对 category 轴的默认是 "end"，即落在轴的最右端 → 被画布右缘裁掉）；
//     同时 grid.bottom 被放大到能容下「刻度 + 标题」。
//   * 纵轴标题 **竖排居中**放在刻度左边 → option.yAxis.nameLocation === "middle"
//     且 nameRotate === 90；grid.left 被放大到能容下「刻度 + 标题」。
//   * 口径写在这里，测试里按同一口径断言。
//
// 11-03（参考线看不见）：
//   * 用户**没有**固定上下界 → option.yAxis.max 覆盖数据值与全部参考线值（含边距）；
//   * 用户显式固定了 maximum → option.yAxis.max **仍是用户的值**，不被静默改写，
//     并且图下多出一句「参考线在当前纵轴范围之外」的中文提示；
//   * 堆叠图型按**堆叠总值**算范围。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const CHART_JS = readFileSync(new URL("../pages/workbench_chart.js", import.meta.url), "utf8");

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

// ---------------------------------------------------------------- 加载模块（源码原文）

/**
 * 把 workbench_chart.js **源码原文**放进 vm 里跑。
 * 模块是 UMD 工厂：给了 module.exports 就把 API 挂在沙箱的 module.exports 上。
 */
function loadChartModule() {
  const sandbox = {
    console: { warn() {}, error() {}, log() {} },
    module: { exports: {} },
    window: {},
    document: {},
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(CHART_JS, sandbox);
  const api = sandbox.module.exports;
  assert.ok(api && typeof api.renderChart === "function", "模块要导出 renderChart");
  return { api, sandbox };
}

/**
 * 跑一次真实的 renderChart，返回 { option, container }。
 * 载荷、定义里的 axes 都按用户验收时的那份来。
 */
function renderWith(payload, region, ctx) {
  const { api, sandbox } = loadChartModule();
  const dom = makeFakeDom();
  const capture = {};
  sandbox.document = dom;
  sandbox.window.echarts = makeFakeEcharts(capture);
  const container = dom.createElement("div");
  api.renderChart(container, region, ctx);
  assert.ok(capture.option, "ECharts 路径必须真的建图");
  return { option: capture.option, container, api };
}

// ---------------------------------------------------------------- 载荷

/** 「设备耗电排名」：3 个设备、耗电量 120 / 180 / 60 kWh。 */
function devicePowerPayload(overrides = {}) {
  return {
    chartKind: "BAR",
    xLabels: ["设备 A", "设备 B", "设备 C"],
    yValues: [120, 180, 60],
    unit: "kWh",
    simulated: true,
    axes: {
      x: { title: "设备" },
      y: { title: "昨日耗电量（kWh）", minimum: 0 },
    },
    referenceLines: {},
    ...overrides,
  };
}

function devicePowerRegion(payload) {
  return {
    regionId: "region_device_power",
    name: "设备耗电排名",
    componentType: "CHART",
    payload,
  };
}

function renderDevicePower(payload) {
  return renderWith(payload, devicePowerRegion(payload), { unit: "kWh" });
}

// ================================================================ 11-02 轴标题

test("11-02：横轴标题存在，且不再落在轴的末端（居中放在刻度下面）", () => {
  const { option } = renderDevicePower(devicePowerPayload());

  assert.equal(option.xAxis.name, "设备", "横轴标题要照声明写进去");
  // 口径：横轴标题居中放在刻度下面（默认 "end" 会落在轴的最右端 → 被裁）。
  assert.equal(option.xAxis.nameLocation, "middle");
  assert.notEqual(option.xAxis.nameLocation, "end");
  assert.ok(option.xAxis.nameGap >= 20, "标题要离刻度有一段距离");
  // 口径：下边距按「刻度 + 标题」放大（基线是 36）。
  assert.ok(
    option.grid.bottom >= 56,
    `grid.bottom 要给横轴标题留位置（实际 ${option.grid.bottom}）`
  );
  assert.ok(option.grid.bottom > 36, "有横轴标题时 grid.bottom 必须比基线大");
});

test("11-02：纵轴标题存在且竖排居中，左边距被放大到能容下标题", () => {
  const { option } = renderDevicePower(devicePowerPayload());

  assert.equal(option.yAxis.name, "昨日耗电量（kWh）", "纵轴标题要照声明写进去");
  // 口径：纵轴标题竖排居中放在刻度左边（长标题不会顶出画布左缘）。
  assert.equal(option.yAxis.nameLocation, "middle");
  assert.equal(option.yAxis.nameRotate, 90);
  assert.ok(option.yAxis.nameGap >= 20);
  // 口径：左边距按「刻度 + 标题」放大（基线是 60）。
  assert.ok(
    option.grid.left >= 80,
    `grid.left 要给纵轴标题留位置（实际 ${option.grid.left}）`
  );
  assert.ok(option.grid.left > 60, "有纵轴标题时 grid.left 必须比基线大");
});

test("11-02：长标题同样不越界（居中 + 让出留白），且不截断标题", () => {
  const payload = devicePowerPayload({
    axes: {
      x: { title: "设备名称（按台计）" },
      y: { title: "昨日耗电量（千瓦时 kWh）" },
    },
  });
  const { option } = renderDevicePower(payload);

  // 标题**逐字**保留（不截断、不缩成省略号）。
  assert.equal(option.xAxis.name, "设备名称（按台计）");
  assert.equal(option.yAxis.name, "昨日耗电量（千瓦时 kWh）");
  assert.ok(!String(option.xAxis.name).includes("…"));
  assert.ok(!String(option.xAxis.name).includes("..."));

  // 排版口径与短标题一致：居中 + 留白让位。
  assert.equal(option.xAxis.nameLocation, "middle");
  assert.equal(option.yAxis.nameLocation, "middle");
  assert.ok(option.grid.bottom >= 56);
  assert.ok(option.grid.left >= 80);
  // 字号没有被缩小到认不出。
  assert.ok(option.xAxis.nameTextStyle.fontSize >= 10);
  assert.ok(option.yAxis.nameTextStyle.fontSize >= 10);
});

test("11-02：没有声明标题时不改布局（grid 保持基线）", () => {
  const payload = devicePowerPayload({
    axes: { x: {}, y: {} },
    unit: "kWh",
  });
  const { option } = renderDevicePower(payload);

  assert.equal(option.grid.left, 60);
  assert.equal(option.grid.right, 20);
  assert.equal(option.grid.top, 24);
  assert.equal(option.grid.bottom, 36);
  assert.equal(option.xAxis.name, undefined, "没声明横轴标题就不该凭空有标题");
});

test("11-02：改标题不改变分类、系列和值", () => {
  const before = devicePowerPayload();
  const after = devicePowerPayload({
    axes: { x: { title: "设备" }, y: { title: "昨日耗电量（kWh）", minimum: 0 } },
  });
  const a = renderDevicePower(before).option;
  const b = renderDevicePower(after).option;

  assert.deepEqual(a.series[0].data, b.series[0].data);
  assert.deepEqual(a.xAxis.data, b.xAxis.data);
  assert.equal(a.series[0].type, b.series[0].type);
  assert.equal(a.series.length, b.series.length);
});

// ================================================================ 11-03 参考线与范围

test("11-03：用户没固定上界时，轴范围覆盖参考线（500 > 数据最大 180）", () => {
  const payload = devicePowerPayload({
    referenceLines: { 关注阈值: { type: "LITERAL", value: 500 } },
  });
  const { option } = renderDevicePower(payload);

  assert.ok(
    typeof option.yAxis.max === "number" && option.yAxis.max >= 500,
    `y 轴上界必须盖住 500 的参考线（实际 ${option.yAxis.max}）`
  );
  assert.ok(option.yAxis.max > 500, "上界要留出边距（不能正好压在参考线上）");
  // 用户给的下界 0 仍然尊重。
  assert.equal(option.yAxis.min, 0);
  // 参考线本身照旧画在数值轴上（值没有被改）。
  const markLine = option.series[0].markLine;
  assert.ok(markLine, "参考线要画出来");
  assert.equal(markLine.data[0].yAxis, 500);
  assert.equal(markLine.data[0].name, "关注阈值");
  // 参考线的**名称**也要看得见：标签画在线的内侧末端（默认在外侧 → 被画布右缘裁掉）。
  assert.equal(markLine.data[0].label.position, "insideEndTop");
});

test("11-03：用户显式固定 maximum 时不静默改写，并给出「在范围外」提示", () => {
  const payload = devicePowerPayload({
    axes: { x: { title: "设备" }, y: { title: "昨日耗电量（kWh）", minimum: 0, maximum: 180 } },
    referenceLines: { 关注阈值: { type: "LITERAL", value: 500 } },
  });
  const { option, container } = renderDevicePower(payload);

  assert.equal(option.yAxis.max, 180, "用户固定的上界不能被静默改写");
  assert.equal(option.yAxis.min, 0, "用户固定的下界也不能被静默改写");

  const text = collectText(container);
  assert.ok(text.includes("关注阈值"), "提示里要写参考线的名称");
  assert.ok(text.includes("500"), "提示里要写参考线的数值");
  assert.ok(
    text.includes("范围") && (text.includes("之外") || text.includes("高于") || text.includes("低于")),
    "提示里要说明这条参考线在当前纵轴范围之外"
  );
  // 提示用的是既有的说明行样式（不另起一套 CSS）。
  const notes = container.children.filter((child) => child.className === "wch-note");
  assert.ok(notes.length >= 2, "既有说明行 + 越界提示各一行");
  assert.ok(notes.some((note) => note.textContent.includes("关注阈值")));
});

test("11-03：堆叠图型按堆叠总值算范围（不是单系列最大值）", () => {
  const payload = {
    chartKind: "STACKED_BAR",
    xLabels: ["机房 A", "机房 B"],
    unit: "kWh",
    axes: { x: { title: "机房" }, y: { title: "昨日耗电量（kWh）" } },
    referenceLines: { 关注阈值: { type: "LITERAL", value: 260 } },
    series: [
      { metricKey: "m1", label: "照明", yValues: [100, 80] },
      { metricKey: "m2", label: "空调", yValues: [150, 90] },
    ],
  };
  const { option } = renderWith(payload, devicePowerRegion(payload), { unit: "kWh" });

  // 堆叠总值：机房 A = 250，机房 B = 170；参考线 260 > 单系列最大值 150。
  assert.ok(
    typeof option.yAxis.max === "number" && option.yAxis.max >= 260,
    `上界要盖住堆叠总值与参考线（实际 ${option.yAxis.max}）`
  );
  assert.ok(option.yAxis.max > 150, "不能只按单系列最大值算");
  assert.equal(option.series[0].stack, "total");
});

test("11-03：参考线 100（在数据范围内）时不动轴范围", () => {
  const payload = devicePowerPayload({
    referenceLines: { 关注阈值: { type: "LITERAL", value: 100 } },
  });
  const { option, container } = renderDevicePower(payload);

  // 100 落在数据范围（60 ~ 180）里 → 不需要动轴（交给 ECharts 自动算）。
  assert.equal(option.yAxis.max, undefined, "参考线在数据范围内时不要乱设上界");
  assert.equal(option.yAxis.min, 0);
  assert.ok(!collectText(container).includes("看不到这条参考线"), "不该有越界提示");
});

test("11-03：多条参考线（高低都加）时范围一次算对", () => {
  const payload = devicePowerPayload({
    referenceLines: {
      高阈值: { type: "LITERAL", value: 500 },
      低阈值: { type: "LITERAL", value: 20 },
    },
  });
  const { option } = renderDevicePower(payload);

  assert.ok(
    typeof option.yAxis.max === "number" && option.yAxis.max >= 500,
    `上界盖住最高的参考线（实际 ${option.yAxis.max}）`
  );
  // 用户固定了下界 0 → 低阈值（20）仍在范围内，不动下界。
  assert.equal(option.yAxis.min, 0);
  assert.ok(!collectText(renderDevicePower(payload).container).includes("看不到这条参考线"));
});

test("11-03：用户没固定下界时，低于数据范围的参考线把下界拽下来", () => {
  const payload = devicePowerPayload({
    axes: { x: { title: "设备" }, y: { title: "昨日耗电量（kWh）" } },
    referenceLines: { 低阈值: { type: "LITERAL", value: 20 } },
  });
  const { option } = renderDevicePower(payload);

  assert.ok(
    typeof option.yAxis.min === "number" && option.yAxis.min <= 20,
    `下界要盖住 20 的参考线（实际 ${option.yAxis.min}）`
  );
  assert.ok(option.yAxis.min < 20, "下界要留出边距");
  assert.ok(option.yAxis.max === undefined || option.yAxis.max >= 180);
});

test("11-03：删掉参考线后回到适合数据的范围（不再被参考线撑开）", () => {
  const withLine = devicePowerPayload({
    referenceLines: { 关注阈值: { type: "LITERAL", value: 500 } },
  });
  const withoutLine = devicePowerPayload({ referenceLines: {} });

  const a = renderDevicePower(withLine).option;
  const b = renderDevicePower(withoutLine).option;

  assert.ok(a.yAxis.max >= 500, "有参考线时上界被撑开");
  assert.equal(b.yAxis.max, undefined, "删掉参考线后上界回到自动（不残留 500 的撑开）");
  assert.equal(b.yAxis.min, 0);
  assert.equal(b.series[0].markLine, undefined, "没有参考线就不该有 markLine");
});

test("11-03：参考线数值没被改写（源值与绑定不动）", () => {
  const payload = devicePowerPayload({
    referenceLines: { 关注阈值: { type: "LITERAL", value: 500 } },
  });
  const { option } = renderDevicePower(payload);

  assert.equal(option.series[0].markLine.data[0].yAxis, 500);
  assert.deepEqual(option.series[0].data, [120, 180, 60]);
});

// ================================================================ 取整口径

test("11-03：算出来的上界是「好看的数」（自己实现，不引第三方库）", () => {
  // 500 → 上界落在 {500, 600} 这一档（1 / 2 / 5 × 10ⁿ 的好看数 + 边距）。
  const payload = devicePowerPayload({
    referenceLines: { 关注阈值: { type: "LITERAL", value: 500 } },
  });
  const { option } = renderDevicePower(payload);
  assert.ok(
    [500, 600].includes(option.yAxis.max),
    `上界应是好看数（实际 ${option.yAxis.max}）`
  );
});


// ================================================================ 11-01 类别标签

test("11-01：类别轴的标签用载荷里的类别名（不画成 0、1、2 序号）", () => {
  const { option } = renderDevicePower(devicePowerPayload());

  // ECharts 不给 xAxis.data 就画序号 —— 「横轴用设备名称」在图上会变成一串数字。
  assert.ok(Array.isArray(option.xAxis.data), "类别轴必须带 data");
  assert.deepEqual(
    option.xAxis.data,
    ["设备 A", "设备 B", "设备 C"],
    "横轴标签必须逐字是载荷里的类别名"
  );
  assert.ok(
    !option.xAxis.data.every((item) => /^\d+$/.test(String(item))),
    "横轴不能画成序号"
  );
  assert.equal(option.xAxis.type, "category");
});

test("11-01：displayXLabels 优先于 xLabels（服务端已格式化时用服务端的）", () => {
  const { option } = renderDevicePower(
    devicePowerPayload({
      xLabels: ["设备 A", "设备 B", "设备 C"],
      displayXLabels: ["A 机架", "B 机架", "C 机架"],
    }),
  );

  assert.deepEqual(option.xAxis.data, ["A 机架", "B 机架", "C 机架"]);
});
