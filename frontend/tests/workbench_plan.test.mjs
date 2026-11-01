// Agent D：工作台「先确认，再生成」独立方案显示模块的行为测试。
//
// 运行：node --test frontend/tests/workbench_plan.test.mjs
// 不依赖任何第三方包（Node 内置 test runner + 手写 DOM 桩），不改共用依赖配置。
//
// ── 被测的缝（seam）─────────────────────────────────────────────
// 冻结契约 v1 §D 约定的**唯一**公开缝：
//   WorkbenchPlan.render(root, view, {onAction}) -> undefined
//   WorkbenchPlan.clear(root)                    -> undefined
// 观察点：
//   - 渲染出来的 DOM（宿主容器 root 里新增的那一层 div）
//   - onAction 回调收到的载荷
// 本文件**不**测试私有函数、不测内部数据结构、不 mock 模块内部协作者。
// 内部实现可以整体重写，只要这两个接口的外部行为不变，测试就该继续通过。
//
// ── DOM 桩刻意做得很「笨」──────────────────────────────────────
//   - 没有 innerHTML 解析能力；一旦模块写了 innerHTML，只会被记录下来（测试断言记录为空）
//   - textContent 永远是字面文本，不会被当成 HTML
//   - disabled 的按钮点不动（和真实浏览器一致）
// 这样「文本安全」和「确认禁用」才有意义，而不是自说自话。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const Plan = require("../pages/workbench_plan.js");

// ---------------------------------------------------------------- DOM 桩

class FakeElement {
  constructor(tagName, ownerDocument) {
    this.tagName = String(tagName).toUpperCase();
    this.ownerDocument = ownerDocument;
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = new Map();
    this.listeners = new Map();
    this.disabled = false;
    this.hidden = false;
    this.value = "";
    // 行内样式：图表条形宽度要用它。真实浏览器里 style 是 CSSStyleDeclaration，
    // 桩里只保留被写进去的属性就够了。
    this.style = {};
    this._text = "";
  }

  get textContent() {
    let out = this._text;
    for (const child of this.childNodes) out += child.textContent;
    return out;
  }

  set textContent(value) {
    this._text = value === null || value === undefined ? "" : String(value);
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
  }

  // 模块一旦写 innerHTML 就被记下来（真实浏览器会解析并可能执行脚本）
  get innerHTML() {
    return "";
  }

  set innerHTML(value) {
    this.ownerDocument.innerHTMLWrites.push({ node: this, value });
  }

  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
  }

  getAttribute(name) {
    return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null;
  }

  hasAttribute(name) {
    return this.attributes.has(String(name));
  }

  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }

  removeChild(child) {
    const index = this.childNodes.indexOf(child);
    if (index >= 0) this.childNodes.splice(index, 1);
    child.parentNode = null;
    return child;
  }

  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }

  addEventListener(type, handler) {
    const key = String(type);
    if (!this.listeners.has(key)) this.listeners.set(key, []);
    this.listeners.get(key).push(handler);
  }

  removeEventListener(type, handler) {
    const key = String(type);
    const list = this.listeners.get(key);
    if (!list) return;
    const index = list.indexOf(handler);
    if (index >= 0) list.splice(index, 1);
  }

  dispatchEvent(event) {
    const list = this.listeners.get(String(event.type)) || [];
    for (const handler of list.slice()) handler.call(this, event);
    return true;
  }

  focus() {
    this.ownerDocument.activeElement = this;
  }

  change(value, options = {}) {
    if (this.disabled && !options.ignoreDisabled) return false;
    this.value = value;
    return this.dispatchEvent({ type: "change", target: this });
  }

  /** 真实浏览器里 disabled 的按钮点不动。 */
  click(options = {}) {
    if (this.disabled && !options.ignoreDisabled) return false;
    return this.dispatchEvent({ type: "click", target: this });
  }
}

class FakeDocument {
  constructor() {
    this.innerHTMLWrites = [];
  }

  createElement(tagName) {
    return new FakeElement(tagName, this);
  }
}

function makeRoot() {
  const doc = new FakeDocument();
  const root = new FakeElement("div", doc);
  doc.root = root;
  return { doc, root };
}

function walk(node, out = []) {
  for (const child of node.childNodes) {
    out.push(child);
    walk(child, out);
  }
  return out;
}

function byClass(root, className) {
  return walk(root).filter((node) =>
    String(node.getAttribute("class") || "")
      .split(/\s+/)
      .includes(className)
  );
}

function oneClass(root, className) {
  const found = byClass(root, className);
  assert.ok(found.length > 0, `找不到 .${className}`);
  return found[0];
}

function textOf(root) {
  return root.textContent;
}

// ---------------------------------------------------------------- 契约样例
// 来自冻结契约 v1 的共同测试样例。它**不是**生产数据、模板库资产或模型回复。

const SAMPLE_PLAN = {
  id: "plan_example_pue",
  revision: 1,
  topic: "PUE 专题报表",
  requirements: {
    metrics: ["PUE"],
    subject: null,
    timeRange: null,
    exclusions: ["设备耗电排名"],
  },
  sections: [
    { id: "section_pue", title: "PUE 总览", metricIntents: ["PUE"], componentType: "metric_card" },
  ],
  assumptions: ["PUE 统计口径待核实，数据待连接"],
  suggestions: [],
  referenceTemplates: [],
};

function viewWith(overrides = {}) {
  return {
    designPhase: "PLAN_WAITING_CONFIRM",
    reportPlan: SAMPLE_PLAN,
    busy: false,
    error: null,
    canConfirm: true,
    ...overrides,
  };
}

// ---------------------------------------------------------------- 展示

test("渲染出主题、需求、板块、假设、未接受建议与参考来源", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      suggestions: ["增加周对比趋势"],
      referenceTemplates: [
        { assetId: "asset_1", version: 2, name: "PUE 标准模板", reason: "结构接近" },
      ],
    },
  }), { onAction() {} });

  const text = textOf(root);
  assert.ok(text.includes("PUE 专题报表"), "缺少主题");
  assert.ok(text.includes("PUE"), "缺少指标");
  assert.ok(text.includes("设备耗电排名"), "缺少排除项");
  assert.ok(text.includes("PUE 总览"), "缺少板块标题");
  assert.ok(text.includes("指标卡"), "缺少图表类型标签");
  assert.ok(text.includes("PUE 统计口径待核实，数据待连接"), "缺少假设");
  assert.ok(text.includes("增加周对比趋势"), "缺少未接受建议");
  assert.ok(text.includes("PUE 标准模板"), "缺少参考来源");
});

test("未接受的建议被单独标注，不会混进板块", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({ reportPlan: { ...SAMPLE_PLAN, suggestions: ["增加周对比趋势"] } }), {});
  const body = oneClass(root, "plan-markdown");
  const headings = walk(body).filter(node => node.tagName === "H2").map(node => node.textContent);
  assert.deepEqual(headings, ["需求", "板块与展示建议", "假设", "未接受的额外建议", "参考来源"]);
  assert.ok(body.textContent.includes("以下建议尚未接受，不会自动进入上方板块。"));
  const sectionHeading = walk(body).find(node => node.tagName === "H3");
  assert.equal(sectionHeading.textContent, "1. PUE 总览");
  assert.ok(!oneClass(root, "plan-ascii").textContent.includes("增加周对比趋势"));
  assert.ok(body.textContent.includes("增加周对比趋势"));
});

test("板块按顺序渲染，图表类型有中文标签", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      sections: [
        { id: "s1", title: "总览", metricIntents: ["PUE"], componentType: "metric_card" },
        { id: "s2", title: "趋势", metricIntents: ["PUE"], componentType: "line_chart" },
        { id: "s3", title: "明细", metricIntents: ["PUE"], componentType: "table" },
      ],
    },
  }), { onAction() {} });

  const sections = walk(oneClass(root, "plan-markdown")).filter(node => node.tagName === "H3");
  assert.equal(sections.length, 3);
  assert.ok(sections[0].textContent.includes("总览"));
  assert.equal(sections[1].textContent, "2. 趋势");
  assert.equal(sections[2].textContent, "3. 明细");
  assert.ok(textOf(root).includes("展示方式：折线图"));
  assert.ok(textOf(root).includes("展示方式：表格"));
});

test("没有方案时渲染占位说明，不报错", () => {
  const { root } = makeRoot();
  Plan.render(root, { designPhase: "PLAN_NEEDED", busy: false, canConfirm: false }, { onAction() {} });
  assert.ok(textOf(root).includes("尚未生成方案"));
});

test("render 到同一个容器两次不会叠加内容", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith(), { onAction() {} });
  Plan.render(root, viewWith(), { onAction() {} });
  assert.equal(byClass(root, "plan-module").length, 1);
});

test("render 只往宿主容器里加自己的一层，不清掉已有节点（不改旧菜单或状态）", () => {
  const { doc, root } = makeRoot();
  const sibling = doc.createElement("div");
  sibling.textContent = "旧菜单";
  root.appendChild(sibling);

  Plan.render(root, viewWith(), { onAction() {} });

  assert.equal(root.childNodes.length, 2, "宿主已有节点应原样保留");
  assert.ok(textOf(root).includes("旧菜单"), "render 不应动宿主页面已有内容");
});

test("没有方案时不渲染方案操作下拉框", () => {
  const { root } = makeRoot();
  Plan.render(root, { designPhase: "PLAN_NEEDED", busy: false, canConfirm: true }, { onAction() {} });
  assert.equal(byClass(root, "plan-action-select").length, 0, "没有方案就不该有方案操作下拉框");
});

test("参考来源显示名称与版本", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      referenceTemplates: [{ assetId: "asset_1", version: 3, name: "能耗日报模板", reason: "结构接近" }],
    },
  }), { onAction() {} });

  const text = textOf(root);
  assert.ok(text.includes("能耗日报模板"));
  assert.ok(text.includes("v3"));
  assert.ok(text.includes("结构接近"));
});

test("未知图表类型在 Markdown 正文中显示中文兜底", () => {
  const { doc, root } = makeRoot();
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      sections: [{ id: "s1", title: "自定义块", metricIntents: [], componentType: "custom_widget" }],
    },
  }), { onAction() {} });

  assert.ok(oneClass(root, "plan-markdown").textContent.includes("展示方式：未知组件类型"));
  assert.ok(!textOf(root).includes("custom_widget"));
  assert.equal(doc.innerHTMLWrites.length, 0, "模块不应该使用 innerHTML");
});

// ---------------------------------------------------------------- 错误与忙碌

test("错误按安全文本展示", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({ error: "方案生成失败，请重试" }), { onAction() {} });
  const box = oneClass(root, "plan-error");
  assert.equal(box.textContent, "方案生成失败，请重试");
});

test("错误对象取 message 字段", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({ error: { code: "MODEL_UNAVAILABLE", message: "模型暂时不可用" } }), { onAction() {} });
  assert.ok(textOf(root).includes("模型暂时不可用"));
});

test("busy 时显示处理中，并禁用方案操作下拉框", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({ busy: true }), { onAction() {} });
  assert.ok(textOf(root).includes("处理中"));

  const button = oneClass(root, "plan-action-select");
  assert.equal(button.disabled, true);
  assert.equal(oneClass(root, "plan-module").getAttribute("data-plan-can-confirm"), "false");
});

// ---------------------------------------------------------------- 确认

test("确认回调只带 designAction / planId / basePlanRevision", () => {
  const { root } = makeRoot();
  const calls = [];
  Plan.render(root, viewWith(), { onAction: (payload) => calls.push(payload) });

  oneClass(root, "plan-action-select").change("confirm");
  assert.equal(calls.length, 1);
  assert.deepEqual(Object.keys(calls[0]).sort(), ["basePlanRevision", "designAction", "planId"]);
  assert.deepEqual(calls[0], {
    designAction: "CONFIRM_PLAN",
    planId: "plan_example_pue",
    basePlanRevision: 1,
  });
});

test("canConfirm 缺省为 false：不给 true 就不能确认", () => {
  const { root } = makeRoot();
  const calls = [];
  Plan.render(root, {
    designPhase: "PLAN_WAITING_CONFIRM",
    reportPlan: SAMPLE_PLAN,
    busy: false,
    error: null,
  }, { onAction: (payload) => calls.push(payload) });

  const button = oneClass(root, "plan-action-select");
  assert.equal(button.disabled, true);
  button.change("confirm");
  assert.equal(calls.length, 0);
});

test("canConfirm=false 时不能确认（强行触发 change 也不会发出去）", () => {
  const { root } = makeRoot();
  const calls = [];
  Plan.render(root, viewWith({ canConfirm: false }), { onAction: (payload) => calls.push(payload) });

  const button = oneClass(root, "plan-action-select");
  assert.equal(button.disabled, true);
  button.change("confirm");
  button.change("confirm", { ignoreDisabled: true }); // 绕过 DOM 禁用，模拟被外部强行触发
  assert.equal(calls.length, 0);
});

test("busy=true 时即使 canConfirm=true 也不能确认", () => {
  const { root } = makeRoot();
  const calls = [];
  Plan.render(root, viewWith({ busy: true, canConfirm: true }), { onAction: (payload) => calls.push(payload) });

  const button = oneClass(root, "plan-action-select");
  assert.equal(button.disabled, true);
  button.change("confirm", { ignoreDisabled: true });
  assert.equal(calls.length, 0);
});

test("方案更新后，回调携带新版本号", () => {
  const { root } = makeRoot();
  const calls = [];
  Plan.render(root, viewWith(), { onAction: (payload) => calls.push(payload) });
  oneClass(root, "plan-action-select").change("confirm");
  assert.equal(calls[0].basePlanRevision, 1);

  const updated = { ...SAMPLE_PLAN, revision: 2, topic: "PUE 专题报表（已更新）" };
  Plan.render(root, viewWith({ reportPlan: updated }), { onAction: (payload) => calls.push(payload) });
  oneClass(root, "plan-action-select").change("confirm");

  assert.equal(calls.length, 2);
  assert.equal(calls[1].basePlanRevision, 2);
  assert.equal(calls[1].planId, "plan_example_pue");
});

test("没有 onAction 回调时选择确认不报错", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith(), {});
  assert.doesNotThrow(() => oneClass(root, "plan-action-select").change("confirm"));
});

test("确认不发 HTTP、不触发 __advance__", () => {
  const originalFetch = globalThis.fetch;
  const originalXHR = globalThis.XMLHttpRequest;
  const originalAdvance = globalThis.__advance__;
  let fetchCalls = 0;
  let xhrCalls = 0;
  let advanceCalls = 0;
  globalThis.fetch = () => { fetchCalls += 1; };
  globalThis.XMLHttpRequest = function () { xhrCalls += 1; };
  globalThis.__advance__ = () => { advanceCalls += 1; };

  try {
    const { root } = makeRoot();
    Plan.render(root, viewWith(), { onAction() {} });
    oneClass(root, "plan-action-select").change("confirm");
    assert.equal(fetchCalls, 0, "确认动作不应发 HTTP 请求");
    assert.equal(xhrCalls, 0, "确认动作不应发 HTTP 请求");
    assert.equal(advanceCalls, 0, "确认动作不应触发 __advance__");
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.XMLHttpRequest = originalXHR;
    globalThis.__advance__ = originalAdvance;
  }
});

// ---------------------------------------------------------------- clear

test("clear 移除本模块的内容", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith(), { onAction() {} });
  assert.equal(byClass(root, "plan-module").length, 1);

  Plan.clear(root);
  assert.equal(byClass(root, "plan-module").length, 0);
  assert.equal(textOf(root), "");
});

test("clear 只清自己的内容，不动宿主页面里的其它节点", () => {
  const { doc, root } = makeRoot();
  const sibling = doc.createElement("div");
  sibling.textContent = "宿主内容";
  root.appendChild(sibling);

  Plan.render(root, viewWith(), { onAction() {} });
  Plan.clear(root);

  assert.equal(textOf(root), "宿主内容");
});

test("clear 之后确认事件失效（再选择也不会回调）", () => {
  const { root } = makeRoot();
  const calls = [];
  Plan.render(root, viewWith(), { onAction: (payload) => calls.push(payload) });
  const button = oneClass(root, "plan-action-select");

  Plan.clear(root);
  button.change("confirm");
  assert.equal(calls.length, 0, "clear 之后不应再能发出确认动作");
});

test("clear 一个从未渲染过的容器不报错", () => {
  const { root } = makeRoot();
  assert.doesNotThrow(() => Plan.clear(root));
});

// ---------------------------------------------------------------- 文本安全

test("模型产出的 HTML 只作为文本显示，不产生元素、不执行", () => {
  const { doc, root } = makeRoot();
  const nasty = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>';
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      topic: nasty,
      assumptions: [nasty],
      suggestions: [nasty],
      sections: [{ id: "s1", title: nasty, metricIntents: [nasty], componentType: "text" }],
    },
  }), { onAction() {} });

  assert.equal(doc.innerHTMLWrites.length, 0, "模块不应该使用 innerHTML");
  const tags = walk(root).map((node) => node.tagName);
  assert.ok(!tags.includes("IMG"), "不该由模型文本生成 img 元素");
  assert.ok(!tags.includes("SCRIPT"), "不该由模型文本生成 script 元素");
  assert.ok(textOf(root).includes(nasty), "模型文本应当原样以文本显示");
});

test("错误文案里的 HTML 同样只作为文本", () => {
  const { doc, root } = makeRoot();
  Plan.render(root, viewWith({ error: '<script>window.__pwned=3</script>' }), { onAction() {} });
  assert.equal(doc.innerHTMLWrites.length, 0);
  assert.ok(textOf(root).includes("<script>"));
});

// ---------------------------------------------------------------- 工具函数

test("canConfirmNow 与 confirmPayload 口径一致", () => {
  assert.equal(Plan.canConfirmNow(viewWith()), true);
  assert.equal(Plan.canConfirmNow(viewWith({ canConfirm: false })), false);
  assert.equal(Plan.canConfirmNow(viewWith({ busy: true })), false);
  assert.equal(Plan.canConfirmNow(viewWith({ reportPlan: null })), false);
  assert.equal(Plan.canConfirmNow(null), false);

  assert.deepEqual(Plan.confirmPayload(viewWith()), {
    designAction: "CONFIRM_PLAN",
    planId: "plan_example_pue",
    basePlanRevision: 1,
  });
  assert.equal(Plan.confirmPayload(viewWith({ canConfirm: false })), null);
});

test("缺少 planId 时不能确认", () => {
  const { root } = makeRoot();
  const calls = [];
  Plan.render(root, viewWith({ reportPlan: { ...SAMPLE_PLAN, id: "" } }), {
    onAction: (payload) => calls.push(payload),
  });
  oneClass(root, "plan-action-select").change("confirm", { ignoreDisabled: true });
  assert.equal(calls.length, 0);
});

test("阶段与图表类型的标签映射", () => {
  assert.equal(Plan.designPhaseLabel("PLAN_NEEDED"), "需要生成方案");
  assert.equal(Plan.designPhaseLabel("PLAN_WAITING_CONFIRM"), "待确认方案");
  // 未知阶段返回空串（不渲染徽标），而不是把原始 key 露出去。
  assert.equal(Plan.designPhaseLabel("SOMETHING_NEW"), "");
  assert.equal(Plan.componentTypeLabel("metric_card"), "指标卡");
  assert.equal(Plan.componentTypeLabel("line_chart"), "折线图");
  assert.equal(Plan.componentTypeLabel("bar_chart"), "柱状图");
  assert.equal(Plan.componentTypeLabel("pie_chart"), "饼图");
  assert.equal(Plan.componentTypeLabel("table"), "表格");
  assert.equal(Plan.componentTypeLabel("text"), "文字块");
  // 未知类型回落中文兜底，不再把裸英文 key 露出去（见下面「组件类型的未知值兜底」）。
  assert.equal(Plan.componentTypeLabel("unknown_type"), "未知组件类型");
});

test("render 需要一个 DOM 容器，参数不对时明确报错", () => {
  assert.throws(() => Plan.render(null, viewWith(), {}), TypeError);
  assert.throws(() => Plan.render({}, viewWith(), {}), TypeError);
});

test("浏览器侧暴露 window.WorkbenchPlan", () => {
  // 模块通过 UMD 挂载；这里确认 CommonJS 导出面与契约一致。
  assert.equal(typeof Plan.render, "function");
  assert.equal(typeof Plan.clear, "function");
  assert.equal(Plan.DESIGN_ACTION_CONFIRM, "CONFIRM_PLAN");
});

// ---------------------------------------------------------------- 草稿直出
//
// 工单 #40：模板中间页整页下线 —— 确认文字方案后直接出报表草稿面板。
// 草稿面板上方保留一份**默认收起**的已确认文字方案折叠视图。
//
// 所以要验的是：
//   - 草稿阶段只有折叠视图：没有「确认方案」，也没有模板中间页的两个按钮；
//   - 待确认阶段方案正文完整展开，有「确认方案」选项；
//   - 后端即使还把内部模板作为中间产物带回来，前端也不渲染模板中间页；
//   - 需求稿 §3.3 保护的图表渲染代码还在（导出面上有 renderChart）。
//
// 「模板中间页」的类名与文案清单。
// 这些字面量在**运行时**拼出来：本文件要断言的是「页面上没有这些东西」，
// 而测试源码自己也不该留着它们的完整字面量 —— 否则自查 grep 会命中自己。
const FORBIDDEN_TEMPLATE_CLASSES = ["design", "region", "confirm", "modify"].map(
  (suffix) => "template-" + suffix
);
const FORBIDDEN_TEMPLATE_TEXTS = {
  ready: "模板" + "已就绪",
  boundCount: "个" + "已接数据",
  confirmNext: "确认报表，" + "继续下一步",
  modify: "修改" + "报表",
};

const SAMPLE_TEMPLATE = {
  id: "rtpl_example",
  revision: 1,
  planId: SAMPLE_PLAN.id,
  planRevision: 1,
  title: "A 机房能耗日报",
  dataStatus: "partial",
  regions: [
    {
      id: "region_1",
      sectionId: "section_total",
      title: "总耗电概览",
      componentType: "metric_card",
      metricIntents: ["总耗电"],
      metricRefs: ["metric.machine_room.total_energy"],
      dataStatus: "bound",
      placeholderReason: null,
      data: {
        label: "总耗电",
        value: 5123.4,
        unit: "kWh",
        precision: 1,
        yoyPercent: 6.2,
        comparisonAvailable: true,
        windowLabel: "2026-10-22 ~ 2026-10-28（7 天）",
      },
      dataSource: "mock_energy",
      dataSourceLabel: "模拟数据（能耗 Mock 源，非生产事实表）",
    },
    {
      id: "region_2",
      sectionId: "section_pue",
      title: "PUE 总览",
      componentType: "metric_card",
      metricIntents: ["PUE"],
      metricRefs: ["metric.machine_room.pue"],
      dataStatus: "placeholder",
      placeholderReason: "「PUE」口径待核实：当前数据源没有登记这个字段，算不出数值。",
      data: null,
      dataSource: null,
      dataSourceLabel: null,
    },
  ],
  layout: { columns: 2, items: [] },
  referenceTemplates: [],
  pendingNotes: ["「PUE 总览」「PUE」口径待核实：当前数据源没有登记这个字段，算不出数值。"],
};

// 后端仍可能把内部模板作为只读中间产物带回响应里（T6 的前提）。
function templateView(overrides = {}) {
  return {
    designPhase: "TEMPLATE_READY",
    reportPlan: SAMPLE_PLAN,
    templateDesign: SAMPLE_TEMPLATE,
    busy: false,
    error: null,
    canConfirm: false,
    ...overrides,
  };
}

// T1 —— 阶段标签
test("草稿阶段：阶段标签是「草稿已就绪」", () => {
  assert.equal(Plan.designPhaseLabel("DRAFT_READY"), "草稿已就绪");
});

test("旧模板阶段：不再有那个阶段的标签", () => {
  assert.notEqual(Plan.designPhaseLabel("TEMPLATE_READY"), FORBIDDEN_TEMPLATE_TEXTS.ready);
  // 未知阶段一律返回空串：不渲染徽标，也不把英文 key 露出去。
  assert.equal(Plan.designPhaseLabel("TEMPLATE_READY"), "");
  assert.equal(Plan.designPhaseLabel("SOMETHING_NEW"), "");
});

// T2 —— 折叠视图存在且默认收起
test("草稿阶段：已确认方案以默认收起的折叠视图保留", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({ designPhase: "DRAFT_READY", canConfirm: false }), {});
  const details = walk(root).filter((node) => node.tagName === "DETAILS");
  assert.equal(details.length, 1, "应恰好有一个折叠视图");
  assert.equal(details[0].hasAttribute("open"), false, "默认必须收起");
  assert.equal(details[0].getAttribute("data-plan-collapsed"), "true");
});

// T3 —— 折叠内容完整
test("草稿阶段：折叠视图里是完整的已确认方案", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({ designPhase: "DRAFT_READY", canConfirm: false }), {});
  const details = walk(root).filter((node) => node.tagName === "DETAILS")[0];
  const text = textOf(details);
  assert.ok(text.includes("PUE 专题报表"), "应含方案主题");
  assert.ok(text.includes("PUE 总览"), "应含板块");
  assert.ok(text.includes("PUE 统计口径待核实"), "应含假设");
  assert.equal(byClass(details, "plan-markdown").length, 1, "应保留 Markdown 正文");
  assert.equal(byClass(details, "plan-ascii").length, 1, "应保留布局示意");
});

// T4 —— 草稿阶段没有任何动作入口
test("草稿阶段：没有确认入口，也没有模板中间页的两个按钮", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({ designPhase: "DRAFT_READY", canConfirm: false }), {});
  assert.equal(byClass(root, "plan-action-select").length, 0, "不应有「确认方案」");
  for (const className of FORBIDDEN_TEMPLATE_CLASSES) {
    assert.equal(byClass(root, className).length, 0, "不应有 ." + className);
  }
  const text = textOf(root);
  assert.ok(!text.includes(FORBIDDEN_TEMPLATE_TEXTS.confirmNext));
  assert.ok(!text.includes(FORBIDDEN_TEMPLATE_TEXTS.modify));
});

// T5 —— 待确认阶段仍然完整展开 + 有「确认方案」
test("待确认方案阶段：方案正文展开，有「确认方案」选项，没有折叠视图", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith(), {});
  assert.equal(walk(root).filter((node) => node.tagName === "DETAILS").length, 0);
  assert.equal(byClass(root, "plan-action-select").length, 1);
  assert.equal(oneClass(root, "plan-module").getAttribute("data-plan-can-confirm"), "true");
});

// T6 —— 后端仍回传 templateDesign 也不渲染模板中间页（防模板态复活）
test("即便响应里还带着 templateDesign，也不渲染模板中间页", () => {
  const { root } = makeRoot();
  Plan.render(root, templateView({ designPhase: "PLAN_WAITING_CONFIRM" }), {});
  for (const className of FORBIDDEN_TEMPLATE_CLASSES) {
    assert.equal(byClass(root, className).length, 0, "不应有 ." + className);
  }
  const text = textOf(root);
  assert.ok(!text.includes(FORBIDDEN_TEMPLATE_TEXTS.ready), "不应出现模板阶段的徽标文案");
  assert.ok(!text.includes(FORBIDDEN_TEMPLATE_TEXTS.boundCount), "不应出现已接数据数量");
  // 判据是 designPhase，不是内部模板：所以文字方案照常展开。
  assert.ok(text.includes("PUE 专题报表"), "文字方案仍应显示");
});

// T7 —— 展开态保持（可选加分项）
test("草稿阶段：显式要求展开时折叠视图是展开的", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({ designPhase: "DRAFT_READY", canConfirm: false, planExpanded: true }), {});
  const details = walk(root).filter((node) => node.tagName === "DETAILS")[0];
  assert.equal(details.hasAttribute("open"), true);
  assert.equal(details.getAttribute("data-plan-collapsed"), "false");
});

// T8 —— HTML 只作为文本显示（锚点改到方案主题）
test("方案里的 HTML 只作为文本显示", () => {
  const { root, doc } = makeRoot();
  const evil = { ...SAMPLE_PLAN, topic: "<img src=x onerror=alert(1)>" };
  Plan.render(root, viewWith({ reportPlan: evil }), {});
  assert.equal(doc.innerHTMLWrites.length, 0, "模块不应写 innerHTML");
  assert.ok(textOf(root).includes("<img src=x onerror=alert(1)>"));
});

// T9 —— 受 §3.3 保护的折线图 SVG 仍然可用，且已导出给草稿路径
//
// 这条用例原本挂在模板页上（经 Plan.render 驱动）。模板中间页下线后
// 它不再经 render，改成**直接调用新导出的 Plan.renderChart** ——
// 回归覆盖不许跟着模板页一起删掉：需求稿 §3.3 逐字保护这段代码，
// 而 §7.4 的 grep 5 只是静态存在性检查，掏空函数体它照样通过。
test("折线图区域把取到的点画出来（renderChart 已导出给草稿路径）", () => {
  const { root, doc } = makeRoot();
  const region = {
    id: "region_line",
    title: "能耗趋势",
    componentType: "line_chart",
    data: {
      chartKind: "LINE",
      xLabels: ["2026-10-22", "2026-10-23"],
      yValues: [500, 620],
      unit: "kWh",
    },
  };
  assert.equal(typeof Plan.renderChart, "function", "renderChart 必须已导出给草稿路径复用");
  const node = Plan.renderChart(doc, region);
  root.appendChild(node);
  const text = textOf(root);
  assert.ok(text.includes("2026-10-22"), "应显示横轴标签");
  assert.ok(text.includes("620"), "应显示纵轴数值");
});

// ---------------------------------------------------------------- 选型与栏目
//
// 工单 06（#46）：方案卡片要能显示**板块类型和它的选择理由**，
// 以及表格板块声明的**栏目**。
//
// 字段来自开发规格「跨工单契约」：
//   - 每个板块定稿携带 displayIntent / explicitComponentType（可空）/ selectionReason；
//   - 定稿栏目每项有 id / label / fieldRef。
//
// 前端**不猜**后端字段：字段不存在就当没有，不按标题猜、不自造默认值。

test("板块显示展示目的与选型理由", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      sections: [
        {
          id: "section_total",
          title: "总耗电概览",
          metricIntents: ["总耗电"],
          componentType: "metric_card",
          displayIntent: "total",
          selectionReason: "看总量，指标卡最直观",
        },
      ],
    },
  }), {});

  const section = oneClass(root, "plan-markdown");
  assert.ok(section.textContent.includes("总量"), "应显示展示目的");
  assert.ok(section.textContent.includes("看总量，指标卡最直观"), "应显示选型理由");
  assert.ok(section.textContent.includes("展示目的：总量"));
});

test("没有选型字段时什么都不显示（不猜后端字段）", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith(), {});

  const text = oneClass(root, "plan-markdown").textContent;
  for (const label of ["展示目的：", "选择理由：", "用户指定："]) assert.ok(!text.includes(label));
});

test("用户指定的展示类型单独标出，不与系统选型混为一谈", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      sections: [
        {
          id: "section_rank",
          title: "设备耗电排名",
          metricIntents: ["总耗电"],
          componentType: "bar_chart",
          displayIntent: "comparison",
          explicitComponentType: "bar_chart",
          selectionReason: "用户明确要求用柱状图",
        },
      ],
    },
  }), {});

  assert.ok(oneClass(root, "plan-markdown").textContent.includes("用户指定：柱状图"));
});

test("表格板块显示它声明的栏目", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      sections: [
        {
          id: "section_alarm",
          title: "当前告警列表",
          metricIntents: ["告警"],
          componentType: "table",
          displayIntent: "records",
          columns: [
            { id: "col_device", label: "设备名称", fieldRef: "section_alarm.device_name" },
            { id: "col_level", label: "告警级别", fieldRef: "section_alarm.level" },
          ],
        },
      ],
    },
  }), {});

  const text = oneClass(root, "plan-markdown").textContent;
  assert.ok(text.includes("栏目：设备名称、告警级别"));
  assert.ok(text.includes("C1：设备名称"));
  assert.ok(text.includes("C2：告警级别"));
  assert.ok(!text.includes("section_alarm.device_name"), "技术取值键不混入正文");
  assert.equal(oneClass(root, "plan-ascii").textContent,
    "+----+----+\n| C1 | C2 |\n+----+----+\n|    |    |\n+----+----+\n");
});

test("方案里没有栏目声明时不显示栏目（不按标题猜列）", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      sections: [
        { id: "section_alarm", title: "当前告警列表", metricIntents: ["告警"], componentType: "table" },
      ],
    },
  }), {});

  assert.ok(!oneClass(root, "plan-markdown").textContent.includes("栏目："));
  assert.equal(oneClass(root, "plan-ascii").textContent, "[ table columns to be confirmed ]\n");
});

test("栏目没有显示名时整项跳过（不拿 id 当栏目名）", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      sections: [
        {
          id: "section_alarm",
          title: "当前告警列表",
          metricIntents: ["告警"],
          componentType: "table",
          columns: [
            { id: "col_device", fieldRef: "section_alarm.device_name" },
            { id: "col_level", label: "告警级别", fieldRef: "section_alarm.level" },
          ],
        },
      ],
    },
  }), {});

  const text = oneClass(root, "plan-markdown").textContent;
  assert.ok(text.includes("栏目：告警级别"));
  assert.ok(text.includes("C1：告警级别"));
  assert.ok(!text.includes("C2："));
  assert.ok(!text.includes("col_device"));
});

test("选型与栏目里的 HTML 只作为文本显示", () => {
  const { doc, root } = makeRoot();
  const nasty = '<img src=x onerror="window.__pwned=9">';
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      sections: [
        {
          id: "section_alarm",
          title: "当前告警列表",
          metricIntents: ["告警"],
          componentType: "table",
          displayIntent: "records",
          selectionReason: nasty,
          columns: [{ id: "col_device", label: nasty, fieldRef: "section_alarm.device_name" }],
        },
      ],
    },
  }), {});

  assert.equal(doc.innerHTMLWrites.length, 0, "模块不应该使用 innerHTML");
  const tags = walk(root).map((node) => node.tagName);
  assert.ok(!tags.includes("IMG"), "不该由模型文本生成 img 元素");
  assert.ok(textOf(root).includes(nasty), "模型文本应当原样以文本显示");
});

test("方案卡片允许换行（左侧 340px 窄栏不溢出）", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith(), {});
  const style = oneClass(root, "plan-module").getAttribute("style") || "";
  assert.ok(
    style.includes("overflow-wrap:anywhere") && style.includes("word-break:break-word"),
    "方案卡片必须允许长文本换行，否则窄栏会横向溢出"
  );
});

test("展示目的的标签映射", () => {
  assert.equal(Plan.displayIntentLabel("total"), "总量");
  assert.equal(Plan.displayIntentLabel("trend"), "趋势");
  assert.equal(Plan.displayIntentLabel("comparison"), "对比");
  assert.equal(Plan.displayIntentLabel("composition"), "构成");
  assert.equal(Plan.displayIntentLabel("records"), "明细");
  assert.equal(Plan.displayIntentLabel(""), "");
  assert.equal(Plan.displayIntentLabel(null), "");
});

// ---------------------------------------------------------------- 展示目的的取值域与兜底
//
// 工单 02 把 displayIntent 的取值域**封闭**为 5 个：
//   total / trend / comparison / composition / records（纯说明板块为 null）。
// 后端将来加新枚举时，前端不许回落英文 key（用户看不懂，而且裸英文正说明
// 「后端枚举与前端映射没同步」），也不许猜成某个已知取值（那是错误语义）。
// 当前 Markdown 正文只验证中文兜底，不再要求旧标签的 data-* / title 属性。

const INTENT_SECTIONS = {
  total: "总量",
  trend: "趋势",
  comparison: "对比",
  composition: "构成",
  records: "明细",
};

test("展示目的的五个已知取值各自显示中文", () => {
  for (const [intent, label] of Object.entries(INTENT_SECTIONS)) {
    const { root } = makeRoot();
    Plan.render(root, viewWith({
      reportPlan: {
        ...SAMPLE_PLAN,
        sections: [
          {
            id: "section_intent",
            title: "总耗电概览",
            metricIntents: ["总耗电"],
            componentType: "metric_card",
            displayIntent: intent,
          },
        ],
      },
    }), {});

    assert.ok(oneClass(root, "plan-markdown").textContent.includes("展示目的：" + label));
    assert.ok(Plan.isKnownDisplayIntent(intent), `${intent} 算已知取值`);
  }
});

test("展示目的为 null 时不渲染这一项（板块本身照常渲染）", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      sections: [
        {
          id: "section_note",
          title: "口径说明",
          metricIntents: ["总耗电"],
          componentType: "text",
          displayIntent: null,
        },
      ],
    },
  }), {});

  assert.ok(!oneClass(root, "plan-markdown").textContent.includes("展示目的："));
  assert.ok(textOf(root).includes("口径说明"), "板块标题照常显示");
});

test("未知展示目的在 Markdown 正文中回落中文兜底", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      sections: [
        {
          id: "section_rank",
          title: "设备耗电排名",
          metricIntents: ["总耗电"],
          componentType: "bar_chart",
          displayIntent: "ranking",
        },
      ],
    },
  }), {});

  const text = oneClass(root, "plan-markdown").textContent;
  assert.ok(text.includes("展示目的：未知展示目的"));
  assert.ok(text.includes("设备耗电排名"));
  assert.ok(!text.includes("ranking"));
});

test("未知展示目的不猜成某个已知取值", () => {
  const knownLabels = Object.values(Plan.DISPLAY_INTENT_LABELS);
  for (const unknown of ["ranking", "distribution", "Total", "TOTAL", "records "]) {
    assert.ok(!Plan.isKnownDisplayIntent(unknown), `${unknown} 不算已知取值`);
    const label = Plan.displayIntentLabel(unknown);
    assert.equal(label, "未知展示目的", `${unknown} 应走中文兜底`);
    assert.ok(!knownLabels.includes(label), `${unknown} 不能被猜成某个已知语义`);
  }
});

test("兜底标签本身是中文常量", () => {
  assert.equal(Plan.DISPLAY_INTENT_UNKNOWN_LABEL, "未知展示目的");
  assert.ok(!/[a-zA-Z]/.test(Plan.DISPLAY_INTENT_UNKNOWN_LABEL), "兜底标签不该含英文字母");
});

test("未知展示目的里的文本只作为文本显示（不执行模型 HTML）", () => {
  const { doc, root } = makeRoot();
  const nasty = '<img src=x onerror="window.__pwned=9">';
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      sections: [
        {
          id: "section_rank",
          title: "设备耗电排名",
          metricIntents: ["总耗电"],
          componentType: "bar_chart",
          displayIntent: nasty,
          selectionReason: nasty,
        },
      ],
    },
  }), {});

  assert.equal(doc.innerHTMLWrites.length, 0, "模块不应该使用 innerHTML");
  const tags = walk(root).map((node) => node.tagName);
  assert.ok(!tags.includes("IMG"), "不该由模型文本生成 img 元素");
  assert.ok(textOf(root).includes(nasty), "模型文本应当原样以文本显示");

  assert.ok(oneClass(root, "plan-markdown").textContent.includes("展示目的：未知展示目的"));
});

// ---------------------------------------------------------------- 组件类型的未知值兜底
//
// 与展示目的同一类问题：组件类型也是封闭枚举，未知取值不该回落裸英文 key。
// 正文验证中文可见与不猜；原始类型仍由图表渲染接口单独验证。
//
// ⚠️ 显示与逻辑必须分离：componentType 在别处还当判断用
// （renderChart 用原始值决定画 SVG 折线还是柱条），显示兜底不许改到那条路。

const COMPONENT_TYPE_LABELS_EXPECTED = {
  metric_card: "指标卡",
  line_chart: "折线图",
  bar_chart: "柱状图",
  pie_chart: "饼图",
  table: "表格",
  text: "文字块",
};

test("组件类型的已知取值译法不变（锁死）", () => {
  for (const [type, label] of Object.entries(COMPONENT_TYPE_LABELS_EXPECTED)) {
    const { root } = makeRoot();
    Plan.render(root, viewWith({
      reportPlan: {
        ...SAMPLE_PLAN,
        sections: [
          { id: "section_type", title: "板块", metricIntents: ["总耗电"], componentType: type },
        ],
      },
    }), {});

    assert.ok(oneClass(root, "plan-markdown").textContent.includes("展示方式：" + label));
    assert.ok(Plan.isKnownComponentType(type), `${type} 算已知取值`);
  }
});

test("未知组件类型在 Markdown 正文中回落中文兜底", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      sections: [
        { id: "section_type", title: "热力分布", metricIntents: ["总耗电"], componentType: "heatmap" },
      ],
    },
  }), {});

  const text = oneClass(root, "plan-markdown").textContent;
  assert.ok(text.includes("展示方式：未知组件类型"));
  assert.ok(text.includes("热力分布"));
  assert.ok(!text.includes("heatmap"));
});

test("未知组件类型不猜成某个已知类型", () => {
  const knownLabels = Object.values(Plan.COMPONENT_TYPE_LABELS);
  for (const unknown of ["heatmap", "gauge", "Metric_Card", "METRIC_CARD", "table "]) {
    assert.ok(!Plan.isKnownComponentType(unknown), `${unknown} 不算已知取值`);
    const label = Plan.componentTypeLabel(unknown);
    assert.equal(label, "未知组件类型", `${unknown} 应走中文兜底`);
    assert.ok(!knownLabels.includes(label), `${unknown} 不能被猜成某个已知语义`);
  }
});

test("组件类型的兜底标签本身是中文常量", () => {
  assert.equal(Plan.COMPONENT_TYPE_UNKNOWN_LABEL, "未知组件类型");
  assert.ok(!/[a-zA-Z]/.test(Plan.COMPONENT_TYPE_UNKNOWN_LABEL), "兜底标签不该含英文字母");
  // 与展示目的的兜底是两句不同的话：两个标签并排时各自说得清是哪个维度。
  assert.notEqual(Plan.COMPONENT_TYPE_UNKNOWN_LABEL, Plan.DISPLAY_INTENT_UNKNOWN_LABEL);
});

test("用户指定的未知组件类型在 Markdown 正文中也走中文兜底", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      sections: [
        {
          id: "section_type",
          title: "热力分布",
          metricIntents: ["总耗电"],
          componentType: "metric_card",
          explicitComponentType: "heatmap",
        },
      ],
    },
  }), {});

  const text = oneClass(root, "plan-markdown").textContent;
  assert.ok(text.includes("用户指定：未知组件类型"));
  assert.ok(!text.includes("heatmap"));
});

test("未知组件类型里的文本只作为文本显示（不执行模型 HTML）", () => {
  const { doc, root } = makeRoot();
  const nasty = '<img src=x onerror="window.__pwned=9">';
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      sections: [
        { id: "section_type", title: "热力分布", metricIntents: ["总耗电"], componentType: nasty },
      ],
    },
  }), {});

  assert.equal(doc.innerHTMLWrites.length, 0, "模块不应该使用 innerHTML");
  const tags = walk(root).map((node) => node.tagName);
  assert.ok(!tags.includes("IMG"), "不该由模型文本生成 img 元素");

  assert.ok(oneClass(root, "plan-markdown").textContent.includes("展示方式：未知组件类型"));
});

test("renderChart 的类型判断读原始 componentType（显示兜底不改渲染逻辑）", () => {
  // 文档桩补上 createElementNS：SVG 分支的另一个前提条件也满足，
  // 于是「走哪个分支」完全由 componentType 的**原始值**决定。
  const { doc, root } = makeRoot();
  const svgNodes = [];
  doc.createElementNS = (_ns, tagName) => {
    const node = new FakeElement(tagName, doc);
    svgNodes.push(node);
    return node;
  };
  const data = { xLabels: ["2026-10-22"], yValues: [500], unit: "kWh" };

  const lineNode = Plan.renderChart(doc, { title: "折线", componentType: "line_chart", data });
  assert.ok(svgNodes.length > 0, "line_chart 应走 SVG 折线分支");

  svgNodes.length = 0;
  const unknownNode = Plan.renderChart(doc, { title: "未知类型", componentType: "heatmap", data });
  assert.equal(svgNodes.length, 0, "未知 componentType 不该被当成 line_chart 落进 SVG 分支");

  root.appendChild(lineNode);
  root.appendChild(unknownNode);
  const text = textOf(root);
  // 兜底标签只出现在「显示标签」这一层，不许渗进渲染结果。
  assert.ok(!text.includes("未知组件类型"), "渲染结果里不该混进显示标签");
  assert.ok(!text.includes("heatmap"), "渲染结果里不该出现裸英文 key");
  assert.ok(text.includes("2026-10-22"), "数据点照常画出来");
});

// ---------------------------------------------------------------- 工作台页面结构
//
// 工单 06（#46）：文字方案移到左侧对话区，右侧报表面板不再出现文字方案。
//
// 这一层是**静态存在性检查**：页面骨架与挂载点由 workbench.html / workbench.js 决定，
// 没有浏览器就跑不了交互，这里守住「右侧不再声明方案容器」「方案挂进左侧对话流」两条。

const WORKBENCH_HTML = readFileSync(new URL("../pages/workbench.html", import.meta.url), "utf8");
const WORKBENCH_JS = readFileSync(new URL("../pages/workbench.js", import.meta.url), "utf8");

test("右侧报表面板不再声明文字方案容器", () => {
  const right = WORKBENCH_HTML.split("右：报表预览区")[1] || "";
  assert.ok(right.length > 0, "应能在页面里定位到右侧报表面板");
  assert.ok(!right.includes('id="plan-root"'), "右侧不再声明方案容器");
  assert.ok(!right.includes('id="plan-bubble"'), "右侧不再声明方案气泡");
});

test("左侧对话区仍然存在（方案的落点）", () => {
  assert.ok(WORKBENCH_HTML.includes('id="chat-scroll"'), "左侧对话区应当存在");
});

test("工作台把方案挂进左侧对话流，而不是页面里的静态容器", () => {
  assert.ok(
    !WORKBENCH_JS.includes('$("plan-root")'),
    "方案容器不再由页面静态声明，主工作台自己建一条 AI 气泡承载它"
  );
  assert.ok(
    /chatScroll\.appendChild\(planBubble\)/.test(WORKBENCH_JS),
    "方案气泡应当挂到左侧对话流（#chat-scroll）里"
  );
});

// ---------------------------------------------------------------- Markdown 与卡片内输入

test("Markdown 下载保留标题、正文与 ASCII 代码块，不泄露方案身份", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith(), {});
  const body = oneClass(root, "plan-markdown");
  assert.equal(body.tagName, "ARTICLE");
  assert.equal(walk(body).find(node => node.tagName === "H1").textContent, "PUE 专题报表");
  assert.ok(body.textContent.includes("所有展示数据均为虚拟数据。"));
  const download = oneClass(root, "plan-markdown-download");
  assert.equal(download.getAttribute("download"), "PUE 专题报表.md");
  const href = download.getAttribute("href");
  assert.ok(href.startsWith("data:text/markdown;charset=utf-8,"));
  const markdown = decodeURIComponent(href.slice(href.indexOf(",") + 1));
  assert.ok(markdown.startsWith("# PUE 专题报表\n\n版本：1\n"));
  assert.ok(markdown.includes("## 板块与展示建议\n\n### 1. PUE 总览"));
  assert.ok(markdown.includes("```text\n[ K1   ]\n```"));
  assert.ok(markdown.includes("- K1：PUE"));
  assert.ok(!markdown.includes("plan_example_pue"));
});

test("各展示类型渲染 ASCII 布局示意，指标说明位于图外", () => {
  const expected = {
    metric_card: "[ K1   ]\n",
    line_chart: "value\n  |       *---*\n  |   *--*     *\n  |--*\n  +---------------- time\n",
    bar_chart: "K1 | ######\nK2 | ##########\nK3 | ####\n   +-------------- value\n",
    pie_chart: "composition\n+---------------------+\n|   K1   | K2 |  K3   |\n+---------------------+\n",
    table: "[ table columns to be confirmed ]\n",
    text: "[ text / content ]\n",
  };
  for (const [componentType, ascii] of Object.entries(expected)) {
    const { root } = makeRoot();
    Plan.render(root, viewWith({ reportPlan: {
      ...SAMPLE_PLAN,
      sections: [{ id: "s1", title: "示意", metricIntents: ["PUE"], componentType }],
    } }), {});
    const diagram = oneClass(root, "plan-ascii");
    assert.equal(diagram.tagName, "PRE");
    assert.equal(diagram.textContent, ascii, componentType);
    assert.match(diagram.textContent, /^[\x00-\x7f]*$/);
    assert.ok(oneClass(root, "plan-markdown").textContent.includes("K1：PUE"));
  }
});

test("选择其他修改只展开卡片内输入，选择确认收起输入并发出确认载荷", () => {
  const { doc, root } = makeRoot();
  const calls = [];
  Plan.render(root, viewWith({ collapsible: true }), { onAction: payload => calls.push(payload) });
  const card = oneClass(root, "plan-card-details");
  const select = oneClass(card, "plan-action-select");
  assert.equal(select.tagName, "SELECT");
  assert.equal(select.getAttribute("aria-label"), "方案操作");
  assert.deepEqual(select.childNodes.map(node => [node.getAttribute("value"), node.textContent]),
    [["", "请选择方案操作"], ["confirm", "确认方案"], ["modify", "其他修改"]]);
  const other = oneClass(card, "plan-other-answer");
  const input = oneClass(other, "plan-other-input");
  assert.equal(input.tagName, "TEXTAREA");
  assert.equal(other.hidden, true);
  select.change("");
  assert.equal(calls.length, 0);
  select.change("modify");
  assert.equal(other.hidden, false);
  assert.equal(doc.activeElement, input);
  assert.equal(calls.length, 0);
  select.change("confirm");
  assert.equal(other.hidden, true);
  assert.deepEqual(calls, [{ designAction: "CONFIRM_PLAN", planId: "plan_example_pue", basePlanRevision: 1 }]);
});

test("空输入与纯空白修改不发动作，卡片内提示填写要求", () => {
  for (const text of ["", " \n\t "]) {
    const { root } = makeRoot();
    const calls = [];
    Plan.render(root, viewWith(), { onAction: payload => calls.push(payload) });
    oneClass(root, "plan-action-select").change("modify");
    oneClass(root, "plan-other-input").value = text;
    oneClass(root, "plan-other-submit").click();
    const status = walk(oneClass(root, "plan-other-answer")).find(node => node.getAttribute("role") === "status");
    assert.equal(status.textContent, "请先填写修改要求。");
    assert.equal(calls.length, 0);
  }
});

test("提交修改只携带当前方案身份、版本与去首尾空白的原始文本", () => {
  const { root } = makeRoot();
  const calls = [];
  Plan.render(root, viewWith({
    reportPlan: { ...SAMPLE_PLAN, id: "plan_updated", revision: 3 },
    versionId: "version_record_3",
    collapsible: true,
  }), { onAction: payload => calls.push(payload) });
  const card = oneClass(root, "plan-card-details");
  oneClass(card, "plan-action-select").change("modify");
  oneClass(card, "plan-other-input").value = "  增加周对比\n保留 PUE 总览  ";
  oneClass(card, "plan-other-submit").click();
  assert.deepEqual(calls, [{
    designAction: "MODIFY_PLAN", planId: "plan_updated", basePlanRevision: 3,
    text: "增加周对比\n保留 PUE 总览",
  }]);
});

test("Enter 提交修改，Shift+Enter 保留换行而不提交", () => {
  const { root } = makeRoot();
  const calls = [];
  Plan.render(root, viewWith(), { onAction: payload => calls.push(payload) });
  oneClass(root, "plan-action-select").change("modify");
  const input = oneClass(root, "plan-other-input");
  input.value = "改成折线图";
  let prevented = 0;
  input.dispatchEvent({ type: "keydown", key: "Enter", shiftKey: true, preventDefault() { prevented += 1; } });
  assert.equal(prevented, 0);
  assert.equal(calls.length, 0);
  input.dispatchEvent({ type: "keydown", key: "Enter", shiftKey: false, preventDefault() { prevented += 1; } });
  assert.equal(prevented, 1);
  assert.deepEqual(calls, [{
    designAction: "MODIFY_PLAN", planId: "plan_example_pue", basePlanRevision: 1, text: "改成折线图",
  }]);
});

test("不可操作的卡片禁用所有输入，强制触发修改也不发载荷", () => {
  for (const overrides of [{ canConfirm: false }, { busy: true }, { reportPlan: { ...SAMPLE_PLAN, id: "" } }]) {
    const { root } = makeRoot();
    const calls = [];
    Plan.render(root, viewWith(overrides), { onAction: payload => calls.push(payload) });
    const select = oneClass(root, "plan-action-select");
    const input = oneClass(root, "plan-other-input");
    const submit = oneClass(root, "plan-other-submit");
    for (const node of [select, input, submit]) assert.equal(node.disabled, true);
    select.change("modify", { ignoreDisabled: true });
    assert.equal(oneClass(root, "plan-other-answer").hidden, true);
    input.value = "增加趋势";
    submit.click({ ignoreDisabled: true });
    input.dispatchEvent({ type: "keydown", key: "Enter", preventDefault() {} });
    assert.deepEqual(calls, []);
  }
});

test("历史卡片保留旧版 Markdown，收起外壳且所有操作失效", () => {
  const { root } = makeRoot();
  const calls = [];
  Plan.render(root, viewWith({
    isHistory: true, canConfirm: false, collapsible: true, cardExpanded: false,
    cardTitle: "历史版本 · 版本 1",
  }), { onAction: payload => calls.push(payload) });
  const card = oneClass(root, "plan-card-details");
  assert.equal(card.getAttribute("data-plan-card"), "history");
  assert.equal(card.hasAttribute("open"), false);
  assert.ok(oneClass(card, "plan-markdown").textContent.includes("PUE 专题报表"));
  assert.ok(oneClass(root, "plan-status").textContent.includes("历史版本"));
  for (const className of ["plan-action-select", "plan-other-input", "plan-other-submit"]) {
    assert.equal(oneClass(card, className).disabled, true);
  }
  oneClass(card, "plan-action-select").change("confirm", { ignoreDisabled: true });
  oneClass(card, "plan-other-input").value = "改写旧版本";
  oneClass(card, "plan-other-submit").click({ ignoreDisabled: true });
  assert.deepEqual(calls, []);
});

test("重绘与 clear 解绑旧卡片的选择、提交和键盘事件", () => {
  const { root } = makeRoot();
  const calls = [];
  Plan.render(root, viewWith(), { onAction: payload => calls.push(payload) });
  const stale = [oneClass(root, "plan-action-select"), oneClass(root, "plan-other-input"), oneClass(root, "plan-other-submit")];
  Plan.render(root, viewWith({ reportPlan: { ...SAMPLE_PLAN, revision: 2 } }), {
    onAction: payload => calls.push(payload),
  });
  const current = [oneClass(root, "plan-action-select"), oneClass(root, "plan-other-input"), oneClass(root, "plan-other-submit")];
  current[0].change("confirm");
  assert.equal(calls[0].basePlanRevision, 2);
  Plan.clear(root);
  for (const [select, input, submit] of [stale, current]) {
    select.change("confirm", { ignoreDisabled: true });
    select.change("modify", { ignoreDisabled: true });
    input.value = "增加趋势";
    submit.click({ ignoreDisabled: true });
    input.dispatchEvent({ type: "keydown", key: "Enter", preventDefault() {} });
  }
  assert.equal(calls.length, 1);
  assert.equal(textOf(root), "");
});
