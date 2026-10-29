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
  Plan.render(root, viewWith({
    reportPlan: { ...SAMPLE_PLAN, suggestions: ["增加周对比趋势"] },
  }), { onAction() {} });

  const suggestions = oneClass(root, "plan-suggestions");
  assert.ok(suggestions.textContent.includes("尚未接受"));
  assert.ok(suggestions.textContent.includes("增加周对比趋势"));

  const sections = oneClass(root, "plan-sections");
  assert.ok(!sections.textContent.includes("增加周对比趋势"), "未接受的建议不应出现在板块里");
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

  const sections = byClass(root, "plan-section");
  assert.equal(sections.length, 3);
  assert.ok(sections[0].textContent.includes("总览"));
  assert.ok(sections[1].textContent.includes("折线图"));
  assert.ok(sections[2].textContent.includes("表格"));
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

test("没有方案时不渲染确认按钮", () => {
  const { root } = makeRoot();
  Plan.render(root, { designPhase: "PLAN_NEEDED", busy: false, canConfirm: true }, { onAction() {} });
  assert.equal(byClass(root, "plan-confirm").length, 0, "没有方案就不该有确认按钮");
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

test("未知图表类型原样显示为文本", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({
    reportPlan: {
      ...SAMPLE_PLAN,
      sections: [{ id: "s1", title: "自定义块", metricIntents: [], componentType: "custom_widget" }],
    },
  }), { onAction() {} });
  assert.ok(textOf(root).includes("custom_widget"));
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

test("busy 时显示处理中，并禁用确认按钮", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith({ busy: true }), { onAction() {} });
  assert.ok(textOf(root).includes("处理中"));

  const button = oneClass(root, "plan-confirm");
  assert.equal(button.disabled, true);
  assert.equal(button.getAttribute("data-plan-can-confirm"), "false");
});

// ---------------------------------------------------------------- 确认

test("确认回调只带 designAction / planId / basePlanRevision", () => {
  const { root } = makeRoot();
  const calls = [];
  Plan.render(root, viewWith(), { onAction: (payload) => calls.push(payload) });

  oneClass(root, "plan-confirm").click();
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

  const button = oneClass(root, "plan-confirm");
  assert.equal(button.disabled, true);
  button.click();
  assert.equal(calls.length, 0);
});

test("canConfirm=false 时不能确认（强行触发点击也不会发出去）", () => {
  const { root } = makeRoot();
  const calls = [];
  Plan.render(root, viewWith({ canConfirm: false }), { onAction: (payload) => calls.push(payload) });

  const button = oneClass(root, "plan-confirm");
  assert.equal(button.disabled, true);
  button.click();
  button.click({ ignoreDisabled: true }); // 绕过 DOM 禁用，模拟被外部强行触发
  assert.equal(calls.length, 0);
});

test("busy=true 时即使 canConfirm=true 也不能确认", () => {
  const { root } = makeRoot();
  const calls = [];
  Plan.render(root, viewWith({ busy: true, canConfirm: true }), { onAction: (payload) => calls.push(payload) });

  const button = oneClass(root, "plan-confirm");
  assert.equal(button.disabled, true);
  button.click({ ignoreDisabled: true });
  assert.equal(calls.length, 0);
});

test("方案更新后，回调携带新版本号", () => {
  const { root } = makeRoot();
  const calls = [];
  Plan.render(root, viewWith(), { onAction: (payload) => calls.push(payload) });
  oneClass(root, "plan-confirm").click();
  assert.equal(calls[0].basePlanRevision, 1);

  const updated = { ...SAMPLE_PLAN, revision: 2, topic: "PUE 专题报表（已更新）" };
  Plan.render(root, viewWith({ reportPlan: updated }), { onAction: (payload) => calls.push(payload) });
  oneClass(root, "plan-confirm").click();

  assert.equal(calls.length, 2);
  assert.equal(calls[1].basePlanRevision, 2);
  assert.equal(calls[1].planId, "plan_example_pue");
});

test("没有 onAction 回调时点击确认不报错", () => {
  const { root } = makeRoot();
  Plan.render(root, viewWith(), {});
  assert.doesNotThrow(() => oneClass(root, "plan-confirm").click());
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
    oneClass(root, "plan-confirm").click();
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

test("clear 之后确认事件失效（再点也不会回调）", () => {
  const { root } = makeRoot();
  const calls = [];
  Plan.render(root, viewWith(), { onAction: (payload) => calls.push(payload) });
  const button = oneClass(root, "plan-confirm");

  Plan.clear(root);
  button.click({ ignoreDisabled: false });
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
  oneClass(root, "plan-confirm").click({ ignoreDisabled: true });
  assert.equal(calls.length, 0);
});

test("阶段与图表类型的标签映射", () => {
  assert.equal(Plan.designPhaseLabel("PLAN_NEEDED"), "需要生成方案");
  assert.equal(Plan.designPhaseLabel("PLAN_WAITING_CONFIRM"), "待确认方案");
  assert.equal(Plan.designPhaseLabel("SOMETHING_NEW"), "SOMETHING_NEW");
  assert.equal(Plan.componentTypeLabel("metric_card"), "指标卡");
  assert.equal(Plan.componentTypeLabel("line_chart"), "折线图");
  assert.equal(Plan.componentTypeLabel("bar_chart"), "柱状图");
  assert.equal(Plan.componentTypeLabel("pie_chart"), "饼图");
  assert.equal(Plan.componentTypeLabel("table"), "表格");
  assert.equal(Plan.componentTypeLabel("text"), "文字块");
  assert.equal(Plan.componentTypeLabel("unknown_type"), "unknown_type");
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

// ---------------------------------------------------------------- 模板渲染
//
// 工单 02：确认方案后生成模板。用户口径（需求访谈 Q13）：
// 「数据库里有啥数据就展示啥，没有的就按 Q11 和 Q12 的方案来」。
//
// 所以要验的是：
//   - 接到数据的区域把**真实值**显示出来；
//   - 没接到数据的区域是占位（指标卡 `--`、图表空框、表格表头加空行）；
//   - 占位区域**不含任何数值**；
//   - 数据来源性质必须显示出来，不能让人把演示数值当生产数据。

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

test("模板已就绪：阶段标签是「模板已就绪」", () => {
  assert.equal(Plan.designPhaseLabel("TEMPLATE_READY"), "模板已就绪");
});

test("接到数据的区域显示真实取到的值", () => {
  const { root } = makeRoot();
  Plan.render(root, templateView(), {});
  const text = textOf(root);
  assert.ok(text.includes("总耗电"), "应显示指标名");
  assert.ok(text.includes("5123.4"), "应显示真实取到的数值");
  assert.ok(text.includes("kWh"), "应显示单位");
  assert.ok(text.includes("2026-10-22 ~ 2026-10-28（7 天）"), "应显示统计窗口");
});

test("数据来源性质必须显示出来", () => {
  const { root } = makeRoot();
  Plan.render(root, templateView(), {});
  assert.ok(textOf(root).includes("模拟数据"), "必须标明这是模拟数据");
});

test("没接到数据的区域是占位：指标值显示 -- 且写明原因", () => {
  const { root } = makeRoot();
  Plan.render(root, templateView(), {});
  const text = textOf(root);
  assert.ok(text.includes("PUE 总览"), "应保留该区域");
  assert.ok(text.includes("--"), "占位指标值应为 --");
  assert.ok(text.includes("口径待核实"), "应写明占位原因");
});

test("占位区域不带任何数值", () => {
  const { root } = makeRoot();
  Plan.render(root, templateView(), {});
  const placeholders = walk(root).filter(
    (node) => node.getAttribute("data-template-region-status") === "placeholder"
  );
  assert.equal(placeholders.length, 1);
  // 占位区域里除了「--」不该出现数字
  const text = placeholders[0].textContent;
  assert.ok(!/\d/.test(text.replace("PUE", "")), "占位区域不应含数值：" + text);
});

test("区域带状态标记，界面与测试都能定位", () => {
  const { root } = makeRoot();
  Plan.render(root, templateView(), {});
  const bound = walk(root).filter(
    (node) => node.getAttribute("data-template-region-status") === "bound"
  );
  assert.equal(bound.length, 1);
});

test("模板已就绪时不再摆「确认方案」入口", () => {
  const { root } = makeRoot();
  const calls = [];
  Plan.render(root, templateView(), { onAction: (payload) => calls.push(payload) });
  assert.equal(byClass(root, "plan-confirm").length, 0);
  assert.equal(calls.length, 0);
});

test("图表区域把取到的点画出来", () => {
  const { root } = makeRoot();
  const chartTemplate = {
    ...SAMPLE_TEMPLATE,
    dataStatus: "bound",
    pendingNotes: [],
    regions: [
      {
        id: "region_1",
        sectionId: "section_trend",
        title: "耗电趋势",
        componentType: "line_chart",
        metricIntents: ["总耗电"],
        metricRefs: ["metric.machine_room.total_energy"],
        dataStatus: "bound",
        placeholderReason: null,
        data: {
          chartKind: "LINE",
          xLabels: ["2026-10-22", "2026-10-23"],
          yValues: [500, 620],
          points: [],
          unit: "kWh",
        },
        dataSource: "mock_energy",
        dataSourceLabel: "模拟数据",
      },
    ],
  };
  Plan.render(root, templateView({ templateDesign: chartTemplate }), {});
  const text = textOf(root);
  assert.ok(text.includes("2026-10-22"), "应显示横轴标签");
  assert.ok(text.includes("620"), "应显示纵轴数值");
});

test("表格区域有表头和数据行；没有数据时是表头加空行", () => {
  const { root } = makeRoot();
  const tableTemplate = {
    ...SAMPLE_TEMPLATE,
    dataStatus: "bound",
    pendingNotes: [],
    regions: [
      {
        id: "region_1",
        sectionId: "section_ranking",
        title: "设备耗电排名",
        componentType: "table",
        metricIntents: ["设备耗电"],
        metricRefs: ["metric.machine_room.device_energy"],
        dataStatus: "bound",
        placeholderReason: null,
        data: {
          columns: [
            { id: "deviceName", label: "设备", fieldRef: "deviceName" },
            { id: "energyKwh", label: "耗电（kWh）", fieldRef: "energyKwh" },
          ],
          rows: [{ deviceName: "CRAC-01 空调", energyKwh: 812.3 }],
          rowNumber: true,
        },
        dataSource: "mock_energy",
        dataSourceLabel: "模拟数据",
      },
    ],
  };
  Plan.render(root, templateView({ templateDesign: tableTemplate }), {});
  const text = textOf(root);
  assert.ok(text.includes("设备"), "应有表头");
  assert.ok(text.includes("CRAC-01 空调"), "应有数据行");
  assert.ok(text.includes("812.3"), "应有数值");
});

test("模板区域里的 HTML 只作为文本显示", () => {
  const { root, doc } = makeRoot();
  const evil = {
    ...SAMPLE_TEMPLATE,
    title: "<img src=x onerror=alert(1)>",
    pendingNotes: [],
  };
  Plan.render(root, templateView({ templateDesign: evil }), {});
  assert.equal(doc.innerHTMLWrites.length, 0, "模块不应写 innerHTML");
  assert.ok(textOf(root).includes("<img src=x onerror=alert(1)>"));
});
