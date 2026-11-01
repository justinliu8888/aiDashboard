// 工单 #66 B 段（前端工作台）：底部追加最新版方案卡 + 历史去重 +
// 阅读位置提示 + JSON 粘贴入口的行为测试。
//
// 运行：node --test frontend/tests/workbench_66b_frontend.test.mjs
// 不依赖任何第三方包（Node 内置 test runner + node:vm + 手写 DOM 桩）。
//
// ── 被测的缝（seam）─────────────────────────────────────────────
//
// 1) 方案模块（frontend/pages/workbench_plan.js，CommonJS 导出）：
//    - 版本身份读取（readPlanVersion / readPlanVersionSequence /
//      versionAliasKeys）—— A 段定的落点：debug.planVersionId、
//      snapshot.planVersions；
//    - 卡片渲染：历史卡标为历史、确认入口失效；当前卡可确认；
//    - 折叠外壳与卡片前的一句简短说明。
//
// 2) 对话区输入模块（frontend/pages/workbench_query.js）：
//    - JSON 粘贴的客户端自查（与服务端同口径、同错误码）；
//    - 可修正的失败提示；来源模式标注。
//
// 3) 主工作台（frontend/pages/workbench.js）：**原样抽出来**跑
//    （node:vm + 大括号配平），验证「底部追加 / 旧卡留原地 / 去重 /
//    澄清与失败 / 阅读位置提示」这几条**编排**行为。
//    抽不出来就直接测试失败 —— 抽不出来就是接缝断了。
//
// 本文件**不改**任何既有测试的断言；既有 129 条基线由 CI 单独跑。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const require = createRequire(import.meta.url);
const Plan = require("../pages/workbench_plan.js");
const Query = require("../pages/workbench_query.js");

const WORKBENCH_JS = readFileSync(new URL("../pages/workbench.js", import.meta.url), "utf8");
const WORKBENCH_HTML = readFileSync(new URL("../pages/workbench.html", import.meta.url), "utf8");

// ---------------------------------------------------------------- DOM 桩

class FakeClassList {
  constructor(node) {
    this.node = node;
    this.set = new Set();
  }
  add(...names) {
    for (const name of names) if (name) this.set.add(name);
  }
  remove(...names) {
    for (const name of names) this.set.delete(name);
  }
  contains(name) {
    return this.set.has(name);
  }
  toggle(name, force) {
    const want = force === undefined ? !this.set.has(name) : Boolean(force);
    if (want) this.set.add(name);
    else this.set.delete(name);
    return want;
  }
  get value() {
    return [...this.set].join(" ");
  }
}

class FakeNode {
  constructor(tagName, ownerDocument) {
    this.tagName = String(tagName).toUpperCase();
    this.ownerDocument = ownerDocument;
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = new Map();
    this.listeners = new Map();
    this.dataset = {};
    this.style = {};
    this.disabled = false;
    // 滚动相关：真函数按 scrollHeight / scrollTop / clientHeight 判「贴底」。
    this.scrollTop = 0;
    this.scrollHeight = 1000;
    this.clientHeight = 500;
    this._text = "";
    this._html = "";
    this.classList = new FakeClassList(this);
  }

  get id() {
    return this.attributes.get("id") || "";
  }
  set id(value) {
    this.attributes.set("id", String(value));
  }

  get className() {
    return this.classList.value;
  }
  set className(value) {
    this.classList.set = new Set(String(value || "").split(/\s+/).filter(Boolean));
  }

  get textContent() {
    // innerHTML 也算进可见文本：浏览器会把它解析成文本，这里不解析但保留原文。
    let out = this._text + this._html;
    for (const child of this.childNodes) out += child.textContent;
    return out;
  }
  set textContent(value) {
    this._text = value === null || value === undefined ? "" : String(value);
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
  }

  // innerHTML 只被记录，不解析：真实浏览器会解析并可能执行脚本。
  get innerHTML() {
    return this._html;
  }
  set innerHTML(value) {
    this._html = value === null || value === undefined ? "" : String(value);
  }

  setAttribute(name, value) {
    const key = String(name);
    this.attributes.set(key, String(value));
    if (key === "class") this.className = String(value);
    if (key.startsWith("data-")) {
      const camel = key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[camel] = String(value);
    }
  }
  getAttribute(name) {
    return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null;
  }
  hasAttribute(name) {
    return this.attributes.has(String(name));
  }
  removeAttribute(name) {
    this.attributes.delete(String(name));
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
  click() {
    if (this.disabled) return false;
    return this.dispatchEvent({ type: "click", target: this });
  }

  get isConnected() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node === (this.ownerDocument ? this.ownerDocument.root : null);
  }

  querySelectorAll(selector) {
    const parsed = parseSelector(selector);
    const out = [];
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (matchesSelector(child, parsed)) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

function parseSelector(selector) {
  const out = { tag: null, id: null, classes: [], attrs: [] };
  const re = /([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=["']?([^\]"']*)["']?)?\]/g;
  let matched = null;
  while ((matched = re.exec(String(selector)))) {
    if (matched[1]) out.tag = matched[1].toUpperCase();
    else if (matched[2]) out.id = matched[2];
    else if (matched[3]) out.classes.push(matched[3]);
    else if (matched[4]) out.attrs.push([matched[4], matched[5]]);
  }
  return out;
}

function matchesSelector(node, parsed) {
  if (parsed.tag && node.tagName !== parsed.tag) return false;
  if (parsed.id && node.id !== parsed.id) return false;
  for (const cls of parsed.classes) if (!node.classList.contains(cls)) return false;
  for (const [name, value] of parsed.attrs) {
    if (!node.hasAttribute(name)) return false;
    if (value !== undefined && node.getAttribute(name) !== value) return false;
  }
  return true;
}

function makeFakeDocument() {
  const doc = {
    root: null,
    byId: new Map(),
    createElement(tagName) {
      return new FakeNode(tagName, doc);
    },
    createElementNS(_ns, tagName) {
      return new FakeNode(tagName, doc);
    },
    getElementById(id) {
      return doc.byId.get(id) || null;
    },
    register(node) {
      if (node.id) doc.byId.set(node.id, node);
      return node;
    },
  };
  doc.root = doc.createElement("body");
  return doc;
}

// ---------------------------------------------------------------- 抽取工具

/** 抽出一个函数定义（按大括号配平）。 */
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

/**
 * 抽出一个函数定义：先跳过参数表（可能有默认值 {}），再对函数体配平大括号。
 */
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

/** 参数表里有默认值的函数要用深抽取。 */
const DEEP_FUNCTIONS = new Set(["planCanConfirm", "appendPlanCard"]);

function extractAny(name) {
  return DEEP_FUNCTIONS.has(name) ? extractFunctionDeep(name) : extractFunction(name);
}

/** 抽出一个 const / let 声明（值到顶层的分号为止）。 */
function extractDecl(name) {
  const markers = [`const ${name} =`, `let ${name} =`];
  let start = -1;
  for (const marker of markers) {
    const at = WORKBENCH_JS.indexOf(marker);
    if (at >= 0 && (start < 0 || at < start)) start = at;
  }
  assert.ok(start >= 0, `源码里找不到声明 ${name}`);
  let depth = 0;
  for (let i = start; i < WORKBENCH_JS.length; i += 1) {
    const ch = WORKBENCH_JS[i];
    if (ch === "{" || ch === "[" || ch === "(") depth += 1;
    else if (ch === "}" || ch === "]" || ch === ")") depth -= 1;
    else if (ch === ";" && depth === 0) return WORKBENCH_JS.slice(start, i + 1);
  }
  throw new Error(`声明 ${name} 没有收口`);
}

// ---------------------------------------------------------------- 工作台环境

const WORKBENCH_FUNCTIONS = [
  "planCanConfirm",
  "resetPlanCards",
  "pruneDisconnectedPlanCards",
  "findPlanCard",
  "createPlanBubble",
  "makePlanCard",
  "registerPlanCard",
  "mountPlanHost",
  "anchorPlanBubble",
  "demoteCurrentPlanCard",
  "planHistoryCardTitle",
  "appendPlanCard",
  "followLatestPlan",
  "showLatestPlanHint",
  "hideLatestPlanHint",
  "renderPlanModule",
  "handlePlanAction",
  "isNewerPlanVersion",
  "buildPlanSummaryLine",
  "firstSentence",
  "syncPlanFromResponse",
  "appendAgentError",
  "appendPlanFailureNotice",
  "showJsonPasteErrorHint",
  "appendJsonSourceNote",
  "formatAgentError",
  "aiBubble",
  "appendAiText",
  "restorePlanFromDetail",
  "restorePlanVersions",
  "clearPlanCards",
  "readSourceMode",
  "renderSourceNoteHtml",
  "isSamePlanVersion",
  "isChatAtBottom",
  "scrollChatBottom",
  "updateJumpButton",
];

const WORKBENCH_DECLS = [
  // $ 与 esc 要排在前面：后面的声明在求值时就要用它们。
  "$",
  "esc",
  "session",
  "planState",
  "planCards",
  "chatScroll",
  "previewRoot",
  "planBubble",
  "planRoot",
  "planAnchorScheduled",
  "chatStickToBottom",
];

/**
 * 起一个工作台环境：把主脚本的相关片段**原样**抽出来跑。
 *
 * 返回的句柄可以直接驱动场景（发响应、读屏上的卡片）。
 */
function makeWorkbench() {
  const doc = makeFakeDocument();
  const chatScroll = doc.createElement("div");
  chatScroll.id = "chat-scroll";
  doc.root.appendChild(chatScroll);
  doc.register(chatScroll);

  const previewRoot = doc.createElement("div");
  previewRoot.id = "preview-root";
  doc.root.appendChild(previewRoot);
  doc.register(previewRoot);

  // 「有最新方案」提示按钮（页面上有；这里只盯它的显隐）。
  const latestBtn = doc.createElement("button");
  latestBtn.id = "btn-latest-plan";
  latestBtn.classList.add("hidden");
  doc.root.appendChild(latestBtn);
  doc.register(latestBtn);

  const microtasks = [];

  const snippets = [
    ...WORKBENCH_FUNCTIONS.map(extractAny),
    ...WORKBENCH_DECLS.map(extractDecl),
  ].join("\n\n");

  const sandbox = {
    console: { warn() {}, error() {}, log() {} },
    document: doc,
    window: { WorkbenchPlan: Plan, WorkbenchQuery: Query },
    WorkbenchQuery: Query,
    session: {
      state: "ANALYZING",
      definition: null,
      policy: null,
      lastReportRun: null,
      reportPreviewPayload: null,
      reportPreviewError: null,
      editingTargetRegionId: null,
    },
    chatBubbleObserver: { observe() {}, unobserve() {} },
    isBusy: () => false,
    sendMessage: () => {},
    queueMicrotask: (fn) => microtasks.push(fn),
    setTimeout: (fn) => microtasks.push(fn),
  };
  vm.createContext(sandbox);
  vm.runInContext(snippets, sandbox);

  const api = {};
  for (const name of WORKBENCH_FUNCTIONS) api[name] = sandbox[name];
  return {
    api,
    sandbox,
    doc,
    chatScroll,
    previewRoot,
    latestBtn,
    /** 把排队的微任务跑掉（浏览器里它们在响应处理之后才跑）。 */
    flush() {
      while (microtasks.length) microtasks.shift()();
    },
    /** 屏上的方案卡气泡（按出现顺序）。 */
    planBubbles() {
      return chatScroll.querySelectorAll("[data-plan-bubble]");
    },
    /** 某张卡里的方案模块根。 */
    planRootOf(bubble) {
      return bubble.querySelector("[data-plan-module]");
    },
    setStuckAtBottom(stuck) {
      vm.runInContext("chatStickToBottom = " + (stuck ? "true" : "false") + ";", sandbox);
    },
    text() {
      return chatScroll.textContent;
    },
  };
}

// ---------------------------------------------------------------- 夹具

const PLAN_V1 = {
  id: "rplan_aaa111",
  revision: 1,
  topic: "A 机房能耗日报",
  requirements: { subject: "A 机房", timeRange: "昨天", metrics: ["总耗电"], exclusions: [] },
  sections: [
    {
      id: "sec_total",
      title: "总电费概览",
      componentType: "metric_card",
      displayIntent: "total",
      selectionReason: "看总量，指标卡最直观",
      metricIntents: ["总耗电"],
      columns: [{ id: "c1", label: "设备名称", fieldRef: "section_1.col_1" }],
    },
  ],
  assumptions: ["口径待核实"],
  suggestions: [],
  referenceTemplates: [{ name: "模板库-能耗日报", version: 2, reason: "同类报表" }],
};

const PLAN_V2 = {
  ...PLAN_V1,
  revision: 2,
  topic: "A 机房能耗日报（含 PUE）",
  sections: [
    ...PLAN_V1.sections,
    {
      id: "sec_pue",
      title: "PUE 总览",
      componentType: "table",
      displayIntent: "records",
      selectionReason: "看具体记录与属性，用表格",
      metricIntents: ["PUE"],
      columns: [{ id: "c2", label: "采集时间", fieldRef: "section_2.col_1" }],
    },
  ],
};

/** 一份「这一轮带来新一版方案」的响应。 */
function planResponse({ plan, versionId, phase = "PLAN_WAITING_CONFIRM", reply }) {
  return {
    state: "WAITING_USER_CONFIRM",
    reply,
    designPhase: phase,
    domainObjects: { reportPlan: plan },
    debug: { planVersionId: versionId, planId: plan.id, planRevision: plan.revision },
  };
}

// ================================================================ 版本身份

test("版本身份按 A 段定的落点读：debug.planVersionId 与顶层 planVersionId", () => {
  const fromDebug = Plan.readPlanVersion({
    debug: { planVersionId: "planver_abc123", planId: "rplan_aaa111", planRevision: 2 },
    domainObjects: { reportPlan: PLAN_V2 },
  });
  assert.equal(fromDebug.versionId, "planver_abc123");
  assert.equal(fromDebug.planId, "rplan_aaa111");
  assert.equal(fromDebug.planRevision, 2);
  assert.equal(fromDebug.reportPlan, PLAN_V2);

  const fromTop = Plan.readPlanVersion({ planVersionId: "planver_abc123", domainObjects: { reportPlan: PLAN_V2 } });
  assert.equal(fromTop.versionId, "planver_abc123");

  // 没有版本 ID 的那一轮：null（前端据此判断「这一轮没有新的一版」）。
  assert.equal(Plan.readPlanVersion({ debug: { planVersionId: null } }), null);
  assert.equal(Plan.readPlanVersion({}), null);
  assert.equal(Plan.readPlanVersion(null), null);
});

test("响应没带版本 ID 时退回「方案身份 + 版本号」（契约表 §4.1）", () => {
  const version = Plan.readPlanVersion({ domainObjects: { reportPlan: PLAN_V1 } });
  assert.equal(version.versionId, "");
  assert.equal(version.planId, "rplan_aaa111");
  assert.equal(version.planRevision, 1);
});

test("去重键：稳定 versionId 与「方案身份 + 版本号」两个别名都认", () => {
  const keys = Plan.versionAliasKeys({ versionId: "planver_abc123", planId: "rplan_aaa111", planRevision: 2 });
  assert.deepEqual(keys, ["ver:planver_abc123", "plan:rplan_aaa111#2"]);
  // 没有 versionId 时也有一个稳定键（同一版永远是同一个）。
  assert.deepEqual(
    Plan.versionAliasKeys({ versionId: "", planId: "rplan_aaa111", planRevision: 1 }),
    ["plan:rplan_aaa111#1"]
  );
  assert.deepEqual(Plan.versionAliasKeys(null), []);
});

test("会话快照的版本序列按真实保存顺序读（snapshot.planVersions）", () => {
  const versions = Plan.readPlanVersionSequence({
    planVersions: [
      { versionId: "planver_c", planId: "rplan_aaa111", planRevision: 3, sequence: 3, reportPlan: PLAN_V2 },
      { versionId: "planver_a", planId: "rplan_aaa111", planRevision: 1, sequence: 1, reportPlan: PLAN_V1 },
      { versionId: "planver_b", planId: "rplan_aaa111", planRevision: 2, sequence: 2, reportPlan: PLAN_V2 },
    ],
  });
  assert.deepEqual(versions.map((v) => v.sequence), [1, 2, 3]);
  assert.deepEqual(versions.map((v) => v.versionId), ["planver_a", "planver_b", "planver_c"]);

  // 老会话没有版本序列 → 空数组（前端不伪造过去的版本）。
  assert.deepEqual(Plan.readPlanVersionSequence({}), []);
  assert.deepEqual(Plan.readPlanVersionSequence(null), []);
});

// ================================================================ 卡片渲染

test("历史卡：标为历史、确认入口失效、正文原样留着", () => {
  const { root } = makePlanRoot();
  Plan.render(
    root,
    {
      designPhase: "PLAN_WAITING_CONFIRM",
      reportPlan: PLAN_V1,
      canConfirm: false,
      isHistory: true,
      collapsible: true,
      cardExpanded: false,
      cardTitle: "历史版本 · 版本 1",
    },
    {}
  );
  const text = root.textContent;
  assert.ok(text.includes("历史版本"), "历史卡要标为历史");
  assert.ok(text.includes("A 机房能耗日报"), "旧卡原文要留着");
  assert.ok(text.includes("总电费概览"), "旧卡板块要留着");
  const confirm = root.querySelector(".plan-confirm");
  assert.ok(confirm, "确认入口要摆出来（失效，而不是消失）");
  assert.equal(confirm.getAttribute("data-plan-can-confirm"), "false");
  assert.equal(confirm.disabled, true);
});

test("当前卡：可确认、标题是「最新方案」、默认展开（底部可读全）", () => {
  const { root } = makePlanRoot();
  Plan.render(
    root,
    {
      designPhase: "PLAN_WAITING_CONFIRM",
      reportPlan: PLAN_V2,
      canConfirm: true,
      collapsible: true,
      cardExpanded: true,
      cardTitle: Plan.PLAN_CARD_TITLE_CURRENT,
      summaryLine: "已按你的要求加上 PUE 板块。",
    },
    {}
  );
  const shell = root.querySelector("details.plan-card-details");
  assert.ok(shell, "当前卡要有折叠外壳");
  assert.equal(shell.hasAttribute("open"), true, "最新一版默认展开：底部要能读全");
  assert.equal(shell.getAttribute("data-plan-card"), "current");
  assert.ok(root.textContent.includes("最新方案"));
  assert.ok(root.textContent.includes("已按你的要求加上 PUE 板块。"), "卡片前那一句说明要显示");
  const confirm = root.querySelector(".plan-confirm");
  assert.equal(confirm.getAttribute("data-plan-can-confirm"), "true");
  assert.equal(confirm.disabled, false);
});

test("卡片前的一句说明来自上层（前端不自己编改了什么）", () => {
  const { root } = makePlanRoot();
  Plan.render(root, { reportPlan: PLAN_V2, summaryLine: "已按你的要求加上 PUE 板块。" }, {});
  const line = root.querySelector("[data-plan-summary]");
  assert.ok(line, "要有那句说明");
  assert.equal(line.textContent, "已按你的要求加上 PUE 板块。");
});

/** 方案模块的 DOM 桩（与既有 workbench_plan.test.mjs 同一套形状）。 */
function makePlanRoot() {
  const doc = makeFakeDocument();
  const root = doc.createElement("div");
  doc.root.appendChild(root);
  return { root, doc };
}

// ================================================================ 编排：底部追加

test("新方案来了：底部追加一张卡，旧卡留在原地并标为历史", () => {
  const wb = makeWorkbench();

  wb.api.syncPlanFromResponse(
    planResponse({ plan: PLAN_V1, versionId: "planver_a", reply: "我拟了这份方案。" })
  );
  wb.flush();
  assert.equal(wb.planBubbles().length, 1, "第一版应当有一张卡");

  wb.api.syncPlanFromResponse(
    planResponse({ plan: PLAN_V2, versionId: "planver_b", reply: "已按你的要求加上 PUE 板块。" })
  );
  wb.flush();

  const bubbles = wb.planBubbles();
  assert.equal(bubbles.length, 2, "新一版要在底部追加一张卡");
  const [oldBubble, newBubble] = bubbles;

  // 旧卡留在原地：原文还在，标成历史，确认入口失效。
  const oldText = oldBubble.textContent;
  assert.ok(oldText.includes("A 机房能耗日报"), "旧卡原文要留在原地");
  assert.ok(oldText.includes("历史版本"), "旧卡要标为历史");
  const oldConfirm = oldBubble.querySelector(".plan-confirm");
  assert.ok(oldConfirm, "旧卡的确认入口要失效（摆出来但点不动）");
  assert.equal(oldConfirm.getAttribute("data-plan-can-confirm"), "false");
  assert.equal(oldConfirm.disabled, true);

  // 新卡在底部：完整展示最新一版。
  const newText = newBubble.textContent;
  assert.ok(newText.includes("A 机房能耗日报（含 PUE）"), "新卡是最新一版");
  assert.ok(newText.includes("PUE 总览"), "新卡要含新增板块");
  assert.ok(newText.includes("最新方案"), "新卡标题是「最新方案」");
  assert.ok(newText.includes("已按你的要求加上 PUE 板块。"), "新卡前那一句说明");
  const newConfirm = newBubble.querySelector(".plan-confirm");
  assert.equal(newConfirm.getAttribute("data-plan-can-confirm"), "true");
  assert.equal(newConfirm.disabled, false);
});

test("重复响应：同一份方案不追加第二遍", () => {
  const wb = makeWorkbench();
  const response = planResponse({ plan: PLAN_V1, versionId: "planver_a", reply: "第一版。" });
  wb.api.syncPlanFromResponse(response);
  wb.flush();
  wb.api.syncPlanFromResponse(response);
  wb.flush();
  assert.equal(wb.planBubbles().length, 1, "重复响应不重复追加");
});

test("同一版换个别名来（只有方案身份 + 版本号）也不重复追加", () => {
  const wb = makeWorkbench();
  wb.api.syncPlanFromResponse(planResponse({ plan: PLAN_V1, versionId: "planver_a" }));
  wb.flush();
  // 这一次响应没带版本 ID（只有方案身份 + 版本号）—— 同一版，不该多一张卡。
  wb.api.syncPlanFromResponse({ designPhase: "PLAN_WAITING_CONFIRM", domainObjects: { reportPlan: PLAN_V1 } });
  wb.flush();
  assert.equal(wb.planBubbles().length, 1, "同一版不重复追加");
});

test("乱序响应：迟到的旧版不追加、不夺取确认资格", () => {
  const wb = makeWorkbench();
  wb.api.syncPlanFromResponse(planResponse({ plan: PLAN_V1, versionId: "planver_a" }));
  wb.flush();
  wb.api.syncPlanFromResponse(planResponse({ plan: PLAN_V2, versionId: "planver_b" }));
  wb.flush();
  assert.equal(wb.planBubbles().length, 2);

  // 迟到的旧版（第 1 版）又回来了。
  wb.api.syncPlanFromResponse(planResponse({ plan: PLAN_V1, versionId: "planver_a" }));
  wb.flush();
  assert.equal(wb.planBubbles().length, 2, "迟到的旧版不追加第三张卡");

  // 当前待确认的仍是第 2 版。
  const current = wb.planBubbles()[1];
  const confirm = current.querySelector(".plan-confirm");
  assert.equal(confirm.getAttribute("data-plan-can-confirm"), "true", "旧版不夺取确认资格");
  assert.equal(current.textContent.includes("含 PUE"), true);
});

test("澄清轮：只发问题，不生成新卡、不出现成功摘要", () => {
  const wb = makeWorkbench();
  wb.api.syncPlanFromResponse(planResponse({ plan: PLAN_V1, versionId: "planver_a" }));
  wb.flush();
  const summariesBefore = wb.chatScroll.querySelectorAll("[data-plan-summary]").length;

  // 澄清：响应只带阶段，没有方案、没有版本 ID。
  wb.api.syncPlanFromResponse({
    state: "NEEDS_CLARIFICATION",
    reply: "你要按天汇总还是按小时汇总？",
    designPhase: "PLAN_WAITING_CONFIRM",
  });
  wb.flush();

  assert.equal(wb.planBubbles().length, 1, "澄清不生成新卡");
  assert.equal(
    wb.chatScroll.querySelectorAll("[data-plan-summary]").length,
    summariesBefore,
    "澄清不追加新的成功摘要"
  );
});

// ================================================================ 编排：失败

test("聊天失败那条路仍然走 appendAgentError(data.error)（既有接线钉住）", () => {
  const start = WORKBENCH_JS.indexOf("if (data.error) {");
  assert.ok(start >= 0, "源码里找不到聊天失败那条路");
  const branch = WORKBENCH_JS.slice(start, WORKBENCH_JS.indexOf("}", start) + 1);
  assert.ok(branch.includes("appendAgentError(data.error)"), "聊天响应的失败原因要显示出来");
  assert.ok(branch.includes("appendPlanFailureNotice()"), "失败要说明上一版仍有效");
});

test("失败轮：只说「本次未更新，上一版仍有效」，旧卡保留", () => {
  const wb = makeWorkbench();
  wb.api.syncPlanFromResponse(planResponse({ plan: PLAN_V1, versionId: "planver_a" }));
  wb.flush();
  const summariesBefore = wb.chatScroll.querySelectorAll("[data-plan-summary]").length;

  wb.api.appendAgentError({ code: "PLAN_REVISION_CONFLICT", message: "方案版本对不上。" });
  wb.api.appendPlanFailureNotice();
  wb.flush();

  const text = wb.text();
  assert.ok(text.includes("本次未更新，上一版仍有效。"), "失败要说明上一版仍有效");
  assert.ok(text.includes("方案版本对不上。"), "失败要说清原因");
  assert.equal(
    wb.chatScroll.querySelectorAll("[data-plan-summary]").length,
    summariesBefore,
    "失败不追加成功摘要"
  );
  assert.equal(wb.planBubbles().length, 1, "失败不生成新卡");
  assert.ok(wb.planBubbles()[0].textContent.includes("A 机房能耗日报"), "旧卡保留");
  // 上一版仍然有效：确认入口照旧可用。
  const confirm = wb.planBubbles()[0].querySelector(".plan-confirm");
  assert.equal(confirm.getAttribute("data-plan-can-confirm"), "true", "上一版仍可确认");
});

// ================================================================ 编排：阅读位置

test("用户在底部：新方案跟着正常新消息走", () => {
  const wb = makeWorkbench();
  wb.setStuckAtBottom(true);
  wb.api.syncPlanFromResponse(planResponse({ plan: PLAN_V1, versionId: "planver_a" }));
  wb.flush();
  assert.equal(wb.chatScroll.scrollTop, wb.chatScroll.scrollHeight, "底部时要跟随滚动");
  assert.equal(wb.latestBtn.classList.contains("hidden"), true, "底部时不弹「有最新方案」");
});

test("用户在上方读历史：不强行滚动，给一个可点的「有最新方案」提示", () => {
  const wb = makeWorkbench();
  wb.api.syncPlanFromResponse(planResponse({ plan: PLAN_V1, versionId: "planver_a" }));
  wb.flush();

  // 用户往上翻了（不在底部）。
  wb.setStuckAtBottom(false);
  wb.chatScroll.scrollTop = 120;
  wb.api.syncPlanFromResponse(planResponse({ plan: PLAN_V2, versionId: "planver_b" }));
  wb.flush();

  assert.equal(wb.chatScroll.scrollTop, 120, "用户在上方时不强行滚动");
  assert.equal(wb.latestBtn.classList.contains("hidden"), false, "给出「有最新方案」提示");
  assert.equal(wb.planBubbles().length, 2, "新卡照样追加（只是不拽用户）");
});

// ================================================================ 编排：刷新还原

test("刷新后按真实保存顺序还原历史卡片与最新版", () => {
  const wb = makeWorkbench();
  wb.api.restorePlanFromDetail({
    snapshot: {
      designPhase: "PLAN_WAITING_CONFIRM",
      reportPlan: PLAN_V2,
      planVersions: [
        { versionId: "planver_a", planId: "rplan_aaa111", planRevision: 1, sequence: 1, reportPlan: PLAN_V1 },
        { versionId: "planver_b", planId: "rplan_aaa111", planRevision: 2, sequence: 2, reportPlan: PLAN_V2 },
      ],
    },
  });
  wb.flush();

  const bubbles = wb.planBubbles();
  assert.equal(bubbles.length, 2, "每一版一张卡");
  assert.ok(bubbles[0].textContent.includes("历史版本"), "第一版是历史卡");
  assert.ok(bubbles[1].textContent.includes("最新方案"), "最后一版是当前卡");
  assert.equal(
    bubbles[1].querySelector(".plan-confirm").getAttribute("data-plan-can-confirm"),
    "true",
    "只有最新版可确认"
  );
});

test("老会话没有版本序列：只还原当前那一版，不伪造历史", () => {
  const wb = makeWorkbench();
  wb.api.restorePlanFromDetail({
    snapshot: { designPhase: "PLAN_WAITING_CONFIRM", reportPlan: PLAN_V1 },
  });
  wb.flush();
  const bubbles = wb.planBubbles();
  assert.equal(bubbles.length, 1, "老会话不伪造过去的版本");
  assert.ok(bubbles[0].textContent.includes("最新方案"));
});

// ================================================================ JSON 粘贴入口

test("JSON 粘贴入口在左侧对话区有明确位置", () => {
  assert.ok(WORKBENCH_HTML.includes('id="btn-json-paste"'), "要有 JSON 粘贴入口");
  assert.ok(WORKBENCH_HTML.includes('id="json-paste-panel"'), "要有粘贴面板");
  assert.ok(WORKBENCH_HTML.includes('id="json-paste-data"'), "要有记录数组输入框");
  assert.ok(WORKBENCH_HTML.includes('id="json-paste-note"'), "要有自然语言输入框");
  assert.ok(WORKBENCH_HTML.includes('id="json-paste-hint"'), "要有可修正提示的位置");
  // 入口在左侧那一栏（会话导航栏之后、右侧预览区之前）。
  const left = WORKBENCH_HTML.split("右：报表预览区")[0];
  assert.ok(left.includes('id="btn-json-paste"'), "粘贴入口在左侧对话区");
});

test("客户端自查与服务端同口径：11 个 JSON 失败码都能说清怎么改", () => {
  assert.equal(Query.parseJsonPaste("").error.code, "JSON_INPUT_EMPTY");
  assert.equal(Query.parseJsonPaste("[{").error.code, "JSON_SYNTAX_INVALID");
  assert.equal(Query.parseJsonPaste('{"a":1}').error.code, "JSON_NOT_ARRAY");
  assert.equal(Query.parseJsonPaste("[]").error.code, "JSON_ARRAY_EMPTY");
  assert.equal(Query.parseJsonPaste('[[1]]').error.code, "JSON_ROW_NOT_OBJECT");
  assert.equal(Query.parseJsonPaste('[{"a":{"b":1}}]').error.code, "JSON_NESTED_NOT_SUPPORTED");

  const tooManyRows = JSON.stringify(Array.from({ length: 5001 }, (_, i) => ({ i })));
  assert.equal(Query.parseJsonPaste(tooManyRows).error.code, "JSON_TOO_MANY_ROWS");

  const tooManyFields = {};
  for (let i = 0; i < 51; i += 1) tooManyFields["f" + i] = i;
  assert.equal(Query.parseJsonPaste(JSON.stringify([tooManyFields])).error.code, "JSON_TOO_MANY_FIELDS");

  const tooLarge = JSON.stringify([{ pad: "x".repeat(1024 * 1024 + 16) }]);
  assert.equal(Query.parseJsonPaste(tooLarge).error.code, "JSON_TOO_LARGE");

  // 每个失败码都要有「怎么改」的说法。
  for (const code of Object.keys(Query.JSON_ERROR_HINTS)) {
    assert.ok(Query.JSON_ERROR_HINTS[code].length > 4, code + " 要有可修正的提示");
  }
  assert.equal(Query.isJsonErrorCode("JSON_ARRAY_PATH_AMBIGUOUS"), true);
  assert.equal(Query.isJsonErrorCode("SOMETHING_ELSE"), false);
});

test("服务端拒绝 JSON 时，界面能拿到可修正的提示", () => {
  const fromServer = { code: "JSON_ARRAY_PATH_AMBIGUOUS", message: "报文里有多个数组，系统不会替你挑一个。" };
  assert.equal(Query.jsonErrorHint(fromServer), fromServer.message);
  // 服务端只给错误码时，前端自己补一句「怎么改」。
  assert.ok(Query.jsonErrorHint({ code: "JSON_ROW_NOT_OBJECT" }).includes("对象"));
});

test("粘贴的消息 = 记录数组 + 自然语言", () => {
  const message = Query.buildJsonPasteMessage("按设备汇总耗电量", '[{"设备名称":"A-1"}]');
  assert.ok(message.includes("按设备汇总耗电量"));
  assert.ok(message.includes('[{"设备名称":"A-1"}]'));
  // 没有自然语言时也照发数组。
  assert.equal(Query.buildJsonPasteMessage("", '[{"a":1}]'), '[{"a":1}]');
});

test("JSON 模式的界面标注：用户提供的数据，未经业务核验；不宣称已连接真实业务库", () => {
  assert.equal(Query.USER_JSON_SOURCE_LABEL, "用户提供的数据，未经业务核验");
  assert.equal(Query.sourceModeLabel("user_json"), "用户提供的数据，未经业务核验");
  assert.equal(Query.sourceModeLabel("simulated"), "模拟示例");
  assert.equal(Query.sourceModeLabel("unknown"), "", "不认识的模式不乱说");

  // 界面上不许出现「已连接真实业务库」这类说法。
  assert.ok(!WORKBENCH_HTML.includes("已连接真实业务库"), "页面上不许这么说");
  const wb = makeWorkbench();
  assert.ok(!wb.api.renderSourceNoteHtml("user_json").includes("已连接真实业务库"));
  assert.ok(!wb.api.renderSourceNoteHtml("simulated").includes("已连接真实业务库"));
  // 右侧不出现文字方案：方案容器只在左侧对话区。
  const right = WORKBENCH_HTML.split("右：报表预览区")[1] || "";
  assert.ok(!right.includes('id="plan-root"'), "右侧不出现文字方案");
});

test("来源模式只按服务端给的信号说；拿不到就不显示标注", () => {
  const wb = makeWorkbench();
  const api = wb.api;
  assert.equal(api.readSourceMode({ metadata: { effectiveCombination: { sourceMode: "user_json" } } }, null, null, null), "user_json");
  assert.equal(api.readSourceMode({ metadata: { sourceMode: "simulated" } }, null, null, null), "simulated");
  assert.equal(api.readSourceMode(null, { dataSource: "user_json" }, null, null), "user_json");
  // 「mock_energy」是数据源标识，不是来源模式。
  assert.equal(api.readSourceMode(null, { dataSource: "mock_energy" }, null, null), "");
  // 拿不到信号 → 空串 → 界面上不显示来源标注。
  assert.equal(api.readSourceMode(null, null, null, null), "");
  assert.equal(api.renderSourceNoteHtml(""), "");
  assert.ok(!api.renderSourceNoteHtml("user_json").includes("模拟业务数据"));
  assert.ok(api.renderSourceNoteHtml("user_json").includes("用户提供的数据，未经业务核验"));
  assert.ok(api.renderSourceNoteHtml("simulated").includes("模拟业务数据"));
});

test("JSON 模式下，服务端那条仍写「模拟业务数据」的能力原因不当成来源说明显示", () => {
  const wb = makeWorkbench();
  const source = readCapabilityNotesSource(wb);
  const region = {
    capabilityPending: true,
    payload: {
      metrics: [
        { label: "PUE", capabilityPending: true, pendingReason: "本区域数值为模拟业务数据，接入真实数据后自动替换。" },
      ],
    },
  };
  const notes = source(region, region.payload, null, "user_json");
  assert.ok(!notes.join("").includes("模拟业务数据"), "JSON 模式下不许显示「模拟业务数据」");
  assert.ok(notes.join("").includes("PUE"), "还要说明是哪个指标没接通");

  // 真实模拟模式使用顶部简洁说明，不重复旧能力原因。
  const simulatedRegion = {
    capabilityPending: true,
    payload: { metrics: [{ label: "PUE", capabilityPending: true, pendingReason: "本区域数值为模拟业务数据。" }] },
  };
  const simulated = source(simulatedRegion, simulatedRegion.payload, null, "simulated");
  assert.equal(simulated.length, 0);
});

/** 把 readCapabilityNotes 从主脚本里抽出来跑（带来源模式参数）。 */
function readCapabilityNotesSource(wb) {
  const source = extractFunction("readCapabilityNotes");
  const sandbox = { console: { warn() {}, error() {}, log() {} } };
  vm.createContext(sandbox);
  vm.runInContext(source + "\n;globalThis.__f = readCapabilityNotes;", sandbox);
  return sandbox.__f;
}

// ================================================================ 两处文案一致

test("两处「用户提供的数据，未经业务核验」是同一句话（不会各说各话）", () => {
  assert.ok(WORKBENCH_JS.includes(Query.USER_JSON_SOURCE_LABEL), "主脚本要用同一句话");
  assert.ok(WORKBENCH_HTML.includes("未经业务核验"), "粘贴面板也要标出来");
});
