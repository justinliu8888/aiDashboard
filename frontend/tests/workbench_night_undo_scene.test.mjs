// 夜间统一修复 · 包 B（前端）：撤销后左侧会话现场同步回退。
//
// 运行：node --test frontend/tests/workbench_night_undo_scene.test.mjs
// 不依赖任何第三方包（Node 内置 test runner + node:vm），不改共用依赖配置。
//
// ── 被测的缝（seam）─────────────────────────────────────────────
// 工作台主脚本（frontend/pages/workbench.js）是**浏览器脚本**，不是模块：
// 顶部就摸 window / document，直接 require 会炸。
// 所以这里沿用既有范式：用 node:vm 把「纯逻辑那几段函数」**原样抽出来**
// 在沙箱里跑 —— 抽不出来直接测试失败（不是复制粘贴的实现）。
//
//   - isRevokedMessagePayload   这条消息是不是被服务端标成了「已撤销」
//   - revokedStepIdsOf          权威现场里被撤销的步骤清单
//   - revokedStepIdsOfPayload   一条消息带出来的步骤 id
//   - authoritativeSceneOf      把服务端的现场归一成前端要的那一份
//   - applyRevokedSteps         把被撤销的那一步在界面上标出来（带假 DOM）
//
// ── 契约（共享契约 v1 §2.3 / §3.2 / BUG-008）────────────────────
//   1. 撤销之后左侧要**整份**回退：当前步骤摘要 / 合法菜单按权威阶段恢复；
//   2. 被撤销那一步的旧 dropdown 失效：不能再选、明确标为「已撤销」，
//      **消息文本保留**（不删聊天历史）；
//   3. 更早的有效修改与历史**保留**；
//   4. 刷新后仍然一致（失效标记来自服务端落在消息上的标记，重放时同样生效）；
//   5. 前端**不自己算**哪一步被撤销：服务端没给失效清单时就不写「已撤销」。
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

/** 抽出一条顶层 const 语句（到行尾的 ;）。 */
function extractConst(name) {
  const pattern = new RegExp(`^const ${name} = .*;$`, "m");
  const match = WORKBENCH_JS.match(pattern);
  assert.ok(match, `源码里找不到 const ${name}`);
  return match[0];
}

// ---------------------------------------------------------------- 假 DOM

/** 一个够用的假元素：属性、类、控件、文本。 */
function makeElement(tagName = "div") {
  const classes = new Set();
  const attributes = new Map();
  const children = [];
  const el = {
    tagName: String(tagName).toUpperCase(),
    attributes,
    children,
    textContent: "",
    disabled: false,
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
    },
    getAttribute: (name) => (attributes.has(name) ? attributes.get(name) : null),
    setAttribute: (name, value) => attributes.set(name, String(value)),
    appendChild: (child) => {
      children.push(child);
      child.parentElement = el;
      return child;
    },
    querySelectorAll: (selector) => {
      const want = /select|input|textarea|button/.test(selector) ? "control" : null;
      if (!want) return [];
      return el._controls || [];
    },
  };
  return el;
}

/**
 * 造一张「菜单卡」：带步骤身份、有一个下拉框与一个提交按钮、正文一段文字。
 * 这就是左侧对话里那张待确认的卡片。
 */
function makeMenuCard(stepId, { text = "已在「总耗电概览」这张卡片上追加 1 项指标：飞机耗电量。原有指标保留", controls = 2 } = {}) {
  const card = makeElement();
  if (stepId) card.setAttribute("data-step-id", stepId);
  card.textContent = text;
  card._controls = [];
  for (let i = 0; i < controls; i += 1) {
    const control = makeElement(i % 2 === 0 ? "select" : "button");
    control.disabled = false;
    card._controls.push(control);
  }
  return card;
}

/** 沙箱里的数组是别的 realm 的，断言前先转成当前 realm 的数组。 */
const own = (value) => Array.from(value || []);

/** 把选中的片段放进沙箱跑一遍，返回它们组成的 API。 */
function loadApi(names, { extraStatements = [], globals = {} } = {}) {
  const snippets = [...extraStatements, ...names.map((name) => extractFunction(name))].join("\n\n");
  const sandbox = { console: { warn() {}, error() {}, log() {} }, ...globals };
  vm.createContext(sandbox);
  vm.runInContext(snippets + "\n;globalThis.__api = {" + names.join(", ") + "};", sandbox);
  return sandbox;
}

// ---------------------------------------------------------------- 契约样例

const MENU_STEP = {
  id: "template_edit_confirm",
  prompt: "请选择下一步操作：",
  expects: "SINGLE_CHOICE",
  selectionOnly: true,
  lockScope: "input_and_menu",
  stepId: "step_menu_1",
  stepVersion: 1,
  stepKind: "menu",
  options: [
    { value: "同意", label: "同意" },
    { value: "修改", label: "修改" },
  ],
};

/** 一条「被撤销」的消息：服务端在载荷上标了 revoked。 */
const REVOKED_MESSAGE_PAYLOAD = {
  state: "USER_EDITING_TEMPLATE",
  reply: "已在「总耗电概览」这张卡片上追加 1 项指标：飞机耗电量。原有指标保留",
  revoked: true,
  questions: [MENU_STEP],
};

/** 一条更早的、仍然有效的消息。 */
const EARLIER_MESSAGE_PAYLOAD = {
  state: "USER_EDITING_TEMPLATE",
  reply: "已把「昨日总耗电」的数值格式改为保留两位小数、启用千分位。",
  questions: [
    {
      id: "template_edit_confirm",
      prompt: "请选择下一步操作：",
      expects: "SINGLE_CHOICE",
      selectionOnly: true,
      lockScope: "input_and_menu",
      stepId: "step_menu_0",
      stepVersion: 1,
      stepKind: "menu",
      options: [{ value: "同意", label: "同意" }],
    },
  ],
};

// ==========================================================================
// 服务端给的失效清单：前端只消费，不自己算
// ==========================================================================

test("服务端标了 revoked 的消息：前端认得出这是「已撤销」那一步", () => {
  const api = loadApi(["isRevokedMessagePayload"]).__api;

  assert.equal(api.isRevokedMessagePayload(REVOKED_MESSAGE_PAYLOAD), true);
  assert.equal(api.isRevokedMessagePayload(EARLIER_MESSAGE_PAYLOAD), false);
  assert.equal(api.isRevokedMessagePayload({}), false);
  assert.equal(api.isRevokedMessagePayload(null), false);
});

test("一条消息带出来的步骤 id：从它自己的 questions 里读", () => {
  const api = loadApi(["revokedStepIdsOfPayload"]).__api;

  assert.deepEqual(own(api.revokedStepIdsOfPayload(REVOKED_MESSAGE_PAYLOAD)), ["step_menu_1"]);
  assert.deepEqual(own(api.revokedStepIdsOfPayload({ questions: [] })), []);
  assert.deepEqual(own(api.revokedStepIdsOfPayload({})), []);
});

test("权威现场里的失效清单：只认服务端给的 revokedStepIds（动作响应顶层）", () => {
  const api = loadApi(["revokedStepIdsOf"]).__api;

  assert.deepEqual(own(api.revokedStepIdsOf({ revokedStepIds: ["step_menu_1", "step_menu_2"] })), [
    "step_menu_1",
    "step_menu_2",
  ]);
  assert.deepEqual(own(api.revokedStepIdsOf({})), [], "服务端没给就是没有，不许前端自己补");
  assert.deepEqual(own(api.revokedStepIdsOf(null)), []);
  assert.deepEqual(own(api.revokedStepIdsOf({ revokedStepIds: "不是数组" })), []);
});

// ==========================================================================
// 权威现场归一（BUG-008 的核心）
// ==========================================================================

test("撤销响应带了 revokedStepIds：失效步骤按服务端给的那份来", () => {
  const api = loadApi(["authoritativeSceneOf", "isRevokedMessagePayload", "revokedStepIdsOf", "revokedStepIdsOfPayload"]).__api;

  // 服务端在撤销那一笔事务里把待决选择清掉了 → pendingChoice 为 null。
  const scene = api.authoritativeSceneOf(
    { revokedStepIds: ["step_menu_1"] },
    { state: "USER_EDITING_TEMPLATE", pendingChoice: null, messages: [] }
  );

  assert.equal(scene.state, "USER_EDITING_TEMPLATE");
  assert.equal(scene.pendingQuestion, null, "服务端已清掉待决选择 → 不锁旧的菜单");
  assert.deepEqual(own(scene.revokedStepIds), ["step_menu_1"]);
  assert.equal(scene.hasRevokedList, true);
});

test("服务端没给失效清单：当前有效菜单仍按服务端的 pendingChoice，失效清单为空", () => {
  const api = loadApi(["authoritativeSceneOf", "isRevokedMessagePayload", "revokedStepIdsOf", "revokedStepIdsOfPayload"]).__api;

  const scene = api.authoritativeSceneOf(
    {},
    { state: "USER_EDITING_TEMPLATE", pendingChoice: { question: MENU_STEP }, messages: [] }
  );

  assert.equal(scene.pendingQuestion?.id, "template_edit_confirm");
  assert.deepEqual(own(scene.revokedStepIds), []);
  assert.equal(scene.hasRevokedList, false, "服务端没给失效清单 → 前端不许写「已撤销」");
});

test("会话历史里被标了 revoked 的消息：失效步骤从消息里读出来", () => {
  const api = loadApi(["authoritativeSceneOf", "isRevokedMessagePayload", "revokedStepIdsOf", "revokedStepIdsOfPayload"]).__api;

  const scene = api.authoritativeSceneOf(
    {},
    {
      state: "USER_EDITING_TEMPLATE",
      pendingChoice: null,
      messages: [
        { role: "user", content: "给总耗电加一个指标" },
        { role: "assistant", payload: EARLIER_MESSAGE_PAYLOAD },
        { role: "assistant", payload: REVOKED_MESSAGE_PAYLOAD },
      ],
    }
  );

  assert.deepEqual(own(scene.revokedStepIds), ["step_menu_1"]);
  assert.equal(scene.hasRevokedList, true);
  assert.equal(scene.pendingQuestion, null, "服务端说没有待决步骤 → 不锁");
});

test("失效清单两个来源合并，不重复", () => {
  const api = loadApi(["authoritativeSceneOf", "isRevokedMessagePayload", "revokedStepIdsOf", "revokedStepIdsOfPayload"]).__api;

  const scene = api.authoritativeSceneOf(
    { revokedStepIds: ["step_menu_1", "step_menu_2"] },
    { messages: [{ role: "assistant", payload: REVOKED_MESSAGE_PAYLOAD }] }
  );

  assert.deepEqual(own(scene.revokedStepIds), ["step_menu_1", "step_menu_2"]);
});

// ==========================================================================
// 界面上的「已撤销」（假 DOM）
// ==========================================================================

function loadDomApi(cards) {
  const document = {
    querySelectorAll(selector) {
      if (selector.includes("data-step-id")) return cards;
      return [];
    },
    createElement(tagName) {
      return makeElement(tagName);
    },
  };
  return loadApi(["applyRevokedSteps"], { globals: { document } });
}

test("被撤销那一步：旧 dropdown 失效、明确标为「已撤销」、消息文本保留", () => {
  const card = makeMenuCard("step_menu_1");
  const api = loadDomApi([card]).__api;

  const marked = api.applyRevokedSteps(["step_menu_1"]);

  assert.equal(marked, 1);
  assert.equal(card.getAttribute("data-revoked"), "1", "这一步要标成已撤销");
  assert.equal(card.classList.contains("wp-revoked"), true);
  assert.equal(card.textContent, "已在「总耗电概览」这张卡片上追加 1 项指标：飞机耗电量。原有指标保留",
    "消息文本一个字都不删（契约 §2.3 第 2 条）");
  for (const control of card._controls) {
    assert.equal(control.disabled, true, "旧控件不能再选 / 不能再提交");
  }
  const badge = card.children.find((child) => child.getAttribute("data-revoked-badge") === "1");
  assert.ok(badge, "要有「已撤销」标记");
  assert.equal(badge.textContent, "已撤销");
});

test("更早的有效修改与历史保留：不在失效清单里的卡片一个字都不动", () => {
  const earlier = makeMenuCard("step_menu_0", { text: "已把「昨日总耗电」的数值格式改为保留两位小数。" });
  const revoked = makeMenuCard("step_menu_1");
  const api = loadDomApi([earlier, revoked]).__api;

  api.applyRevokedSteps(["step_menu_1"]);

  assert.equal(earlier.getAttribute("data-revoked"), null, "更早那一步仍然有效");
  assert.equal(earlier.classList.contains("wp-revoked"), false);
  assert.equal(earlier.children.length, 0, "更早那一步不加撤销标记");
  for (const control of earlier._controls) {
    assert.equal(control.disabled, false, "更早那一步的控件仍然可用");
  }
  assert.equal(revoked.getAttribute("data-revoked"), "1");
});

test("服务端没给失效清单：一个都不标（前端不自己算哪一步被撤销）", () => {
  const card = makeMenuCard("step_menu_1");
  const api = loadDomApi([card]).__api;

  const marked = api.applyRevokedSteps([]);

  assert.equal(marked, 0);
  assert.equal(card.getAttribute("data-revoked"), null);
  for (const control of card._controls) {
    assert.equal(control.disabled, false);
  }
});

test("刷新后仍然一致：同一份服务端标记重放一次，结果一样", () => {
  // 刷新走的是回放路径（restoreSession 逐条回放消息）。
  // 回放时的失效来源是消息载荷上的 revoked 标记 —— 与撤销那一刻同一份数据，
  // 所以刷新后标出来的步骤必须完全一样。
  const api = loadApi(["revokedStepIdsOfPayload", "isRevokedMessagePayload"]).__api;

  const first = api.revokedStepIdsOfPayload(REVOKED_MESSAGE_PAYLOAD);
  const second = api.revokedStepIdsOfPayload(REVOKED_MESSAGE_PAYLOAD);

  assert.deepEqual(own(first), own(second));
  assert.deepEqual(own(first), ["step_menu_1"]);
  assert.equal(api.isRevokedMessagePayload(EARLIER_MESSAGE_PAYLOAD), false);
});

// ==========================================================================
// 接线：撤销之后左侧确实走的是「整份恢复」这条路
// ==========================================================================

test("runDraftAction 在撤销 / 重做之后要走会话现场的整份恢复", () => {
  const block = WORKBENCH_JS.slice(
    WORKBENCH_JS.indexOf("async function runDraftAction("),
    WORKBENCH_JS.indexOf("// --------------------------------------------------------------------------\n// 撤销 / 重做之后")
  );
  assert.ok(block.length > 0, "抽得到 runDraftAction");
  assert.ok(
    block.includes("restoreSessionSceneAfterHistoryAction"),
    "撤销 / 重做之后必须恢复左侧会话现场（BUG-008）"
  );
  assert.ok(
    /action === "UNDO" \|\| action === "REDO"/.test(block),
    "只有撤销 / 重做才恢复现场；保存 / 另存为不动会话"
  );
});

test("restoreSession 接受外部取好的权威现场（省一次重复请求，且现场优先）", () => {
  const block = WORKBENCH_JS.slice(
    WORKBENCH_JS.indexOf("async function restoreSession("),
    WORKBENCH_JS.indexOf("// 刷新后自动接回上次那条会话。")
  );
  assert.ok(block.includes("detail: preloadedDetail"), "restoreSession 能接收外部取好的会话详情");
  assert.ok(block.includes("scene = null"), "restoreSession 能接收权威现场");
  assert.ok(
    block.includes("scene.pendingChoice?.question || null"),
    "权威现场里的当前有效菜单优先于会话快照里的 pendingChoice"
  );
  assert.ok(
    block.includes("applyRevokedSteps(revokedStepIdsOfPayload(message.payload))"),
    "回放时按服务端的撤销标记把旧步骤标成已撤销"
  );
});

test("回放时不会把被撤销的消息从历史里删掉", () => {
  const block = WORKBENCH_JS.slice(
    WORKBENCH_JS.indexOf("async function restoreSession("),
    WORKBENCH_JS.indexOf("// 刷新后自动接回上次那条会话。")
  );
  const loop = block.slice(block.indexOf("for (const message of detail.messages)"));
  assert.ok(loop.includes("replayAssistantMessage(message.payload, pending)"), "消息照常回放");
  assert.ok(
    !/continue;[\s\S]{0,80}isRevokedMessagePayload/.test(loop),
    "被撤销的消息**不跳过**：历史要保留，只是那一步失效"
  );
});
