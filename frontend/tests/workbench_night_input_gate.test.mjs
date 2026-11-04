// 夜间统一修复 · 包 B（前端）：输入门禁（BUG-003 / BUG-009 共用）。
//
// 运行：node --test frontend/tests/workbench_night_input_gate.test.mjs
// 不依赖任何第三方包（Node 内置 test runner + node:vm），不改共用依赖配置。
//
// ── 被测的缝（seam）─────────────────────────────────────────────
// 沿用既有范式：用 node:vm 把「纯逻辑那几段函数」**原样抽出来**在沙箱里跑
// （抽不出来直接测试失败 —— 不是复制粘贴的实现）。
//
//   - lockScopeOf / isInputLocked / isAuthoritativeChoice
//   - menuLocksInput          这份菜单锁不锁输入与发送
//   - inputControlState       输入区各控件的可用状态与提示
//   - syncInputControls       真的把控件锁住（带假 DOM）
//   - appendAiQuestions       澄清卡（文字型）画出**专用输入**
//   - restoreEditFailureMenu  失败待决：没有新菜单就**保持原锁**
//   - sendMessage             专用输入的答案是这一步的答案（不是普通聊天）
//
// ── 契约（共享契约 v1 §3.3）────────────────────────────────────
// 澄清待决 / 候选待选 / 接受处理中 / 失败待决：
//   普通输入框、发送按钮、Enter、快捷建议**全部锁住**；
//   只开放本步合法选项与**专用输入**。
// 「没有候选」/「返回了一段文字」**不是**解锁理由。
// 锁的范围由服务端的 lockScope 决定（"none" | "menu_only" | "input_and_menu"）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const WORKBENCH_JS = readFileSync(new URL("../pages/workbench.js", import.meta.url), "utf8");

// ---------------------------------------------------------------- 抽取工具

function extractFunction(name) {
  const marker = `function ${name}(`;
  const start = WORKBENCH_JS.indexOf(marker);
  assert.ok(start >= 0, `源码里找不到函数 ${name}`);
  // 先把参数表收口（参数表里可能有默认值 {}，直接配平大括号会截断）。
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

function extractConst(name) {
  const pattern = new RegExp(`^const ${name} = .*;$`, "m");
  const match = WORKBENCH_JS.match(pattern);
  assert.ok(match, `源码里找不到 const ${name}`);
  return match[0];
}

// ---------------------------------------------------------------- 假 DOM

function makeElement(dataset = {}) {
  const classes = new Set();
  const listeners = new Map();
  return {
    dataset, value: "", disabled: false, textContent: "", className: "",
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
    },
    focus() { this.focused = true; },
    addEventListener(name, callback) { listeners.set(name, callback); },
    fire(name, event = {}) { return listeners.get(name)?.({ target: this, ...event }); },
  };
}

/**
 * 一个够用的工作台假 DOM。
 * elements 里有：普通输入框、发送按钮、停止按钮、两个快捷入口、
 * 以及「文字型待决」的专用输入卡（data-question-text）。
 */
function makeWorkbenchDom() {
  const input = makeElement();
  const send = makeElement();
  const stop = makeElement();
  const quick = makeElement();
  const quick2 = makeElement();
  const hint = makeElement();

  // 专用输入（澄清卡里那个）：输入框 + 提交按钮。
  const textInput = makeElement({ questionTextInput: "context_missing_info" });
  const textSubmit = makeElement({ questionTextSubmit: "context_missing_info" });
  const textHint = makeElement();
  const textBox = makeElement({ questionText: "context_missing_info" });
  textBox.querySelector = (selector) => ({
    ".question-text-input": textInput,
    ".question-text-submit": textSubmit,
    ".question-text-hint": textHint,
  })[selector];

  // 历史轮的下拉框（当前待答那张卡没有下拉框时，它必须保持禁用 ——
  // 旧菜单被重新打开就等于让用户重放一个已经失效的步骤）。
  const oldSelect = makeElement({ questionId: "template_edit_confirm" });
  const selects = [oldSelect];

  // 同一轮里的**澄清菜单**（带选项）与**专用输入卡**：同一步（step_x）。
  //
  // 实测：模型路径的澄清轮就是这个样子 —— 一轮里同时给两张同一步的卡，
  // 契约 §3.3 要开放的是「本步合法选项与专用输入」，两样都要开着。
  const menuSelect = makeElement({ questionId: "template_edit_clarification" });
  menuSelect.closest = () => ({ getAttribute: () => "step_x" });
  const stepTextInput = makeElement({ questionTextInput: "template_edit_request" });
  const stepTextSubmit = makeElement({ questionTextSubmit: "template_edit_request" });
  const stepTextBox = makeElement({ questionText: "template_edit_request" });
  stepTextBox.closest = () => ({ getAttribute: () => "step_x" });
  stepTextBox.querySelector = (selector) => ({
    ".question-text-input": stepTextInput,
    ".question-text-submit": stepTextSubmit,
    ".question-text-hint": makeElement(),
  })[selector];
  selects.push(menuSelect);

  // 输入框挂在一个 flex 行里，行再挂在一个容器里 —— 提示条挂在容器上
  // （syncInputLockHint 会往 host.parentElement 上挂提示条）。
  const inputRow = makeElement();
  const inputBox = makeElement();
  inputRow.appendChild = (child) => {
    child.parentElement = inputRow;
    return child;
  };
  inputBox.appendChild = (child) => {
    child.parentElement = inputBox;
    return child;
  };
  inputRow.parentElement = inputBox;
  input.parentElement = inputRow;

  const elements = {
    "chat-input": input,
    "btn-send": send,
    "btn-stop": stop,
    "quick-a": quick,
    "quick-b": quick2,
    hint,
  };

  const document = {
    getElementById(id) { return elements[id] || null; },
    createElement() {
      const el = makeElement();
      el.setAttribute = (name) => { el.dataset[name] = true; };
      el.appendChild = (child) => child;
      return el;
    },
    querySelectorAll(selector) {
      if (selector.includes("data-quick")) return [quick, quick2];
      if (selector.includes("question-select")) return selects;
      if (selector.includes("[data-question-text]")) return [textBox, stepTextBox];
      if (selector.includes("[data-question-other]")) return [];
      return [];
    },
  };

  return {
    document, elements, input, send, quick, quick2, hint,
    textBox, textInput, textSubmit,
    oldSelect, menuSelect, stepTextBox, stepTextInput, stepTextSubmit,
  };
}

/** 把选中的片段放进沙箱跑一遍，返回它们组成的 API。 */
function loadApi(names, { extraStatements = [], globals = {} } = {}) {
  const snippets = [...extraStatements, ...names.map((name) => extractFunction(name))].join("\n\n");
  const sandbox = { console: { warn() {}, error() {}, log() {} }, ...globals };
  vm.createContext(sandbox);
  vm.runInContext(snippets + "\n;globalThis.__api = {" + names.join(", ") + "};", sandbox);
  return sandbox;
}

const LOCK_CONSTS = [
  extractConst("LOCK_SCOPE_NONE"),
  extractConst("LOCK_SCOPE_MENU_ONLY"),
  extractConst("LOCK_SCOPE_INPUT_AND_MENU"),
  extractConst("INPUT_LOCK_HINT"),
];

function loadLockApi() {
  return loadApi(
    ["lockScopeOf", "isInputLocked", "isAuthoritativeChoice", "menuLocksInput", "inputControlState"],
    { extraStatements: LOCK_CONSTS }
  ).__api;
}

// ---------------------------------------------------------------- 契约样例

/** 候选待选：待确认修改计划那张卡（三个固定选项）。 */
const CANDIDATE_MENU = {
  id: "pending_edit_plan_confirm",
  prompt: "确认这次修改：",
  expects: "SINGLE_CHOICE",
  selectionOnly: true,
  lockScope: "input_and_menu",
  stepId: "step_plan_1",
  stepVersion: 1,
  stepKind: "menu",
  options: [
    { value: "接受修改", label: "接受修改" },
    { value: "继续调整", label: "继续调整" },
    { value: "取消", label: "取消" },
  ],
};

/** 澄清待决：服务端说「直接填写」（文字型、没有选项）。 */
const CLARIFY_TEXT_QUESTION = {
  id: "context_missing_info",
  prompt: "请补充你想看的对象。",
  expects: "TEXT",
  lockScope: "menu_only",
  stepId: "step_clarify_1",
  stepVersion: 1,
  stepKind: "menu",
  options: [],
};

/** 受限修改输入那一步（既有口径：expects=TEXT + allowsOther）。 */
const EDIT_REQUEST_STEP = {
  id: "template_edit_request",
  prompt: "请描述你想怎样修改当前草稿：",
  expects: "TEXT",
  selectionOnly: false,
  allowsOther: true,
  otherLabel: "修改要求",
  otherPlaceholder: "直接输入修改要求",
  lockScope: "input_and_menu",
  stepId: "step_edit_1",
  stepVersion: 1,
  stepKind: "edit_request",
  allowedSubmissions: ["edit_text"],
  options: [],
};

// ==========================================================================
// 四种状态：普通输入 / 发送 / Enter / 快捷建议 全锁
// ==========================================================================

test("澄清待决（文字型）：普通输入、发送、快捷建议全锁", () => {
  const api = loadLockApi();

  const state = api.inputControlState(CLARIFY_TEXT_QUESTION, false);

  assert.equal(state.inputDisabled, true, "普通输入框应当锁住");
  assert.equal(state.sendDisabled, true, "发送按钮应当锁住");
  assert.equal(state.quickDisabled, true, "快捷建议应当锁住");
  assert.equal(state.hint, "请在卡片内填写后提交", "提示要指向卡片里的专用输入");
});

test("候选待选：普通输入、发送、快捷建议全锁", () => {
  const api = loadLockApi();

  const state = api.inputControlState(CANDIDATE_MENU, false);

  assert.equal(state.inputDisabled, true);
  assert.equal(state.sendDisabled, true);
  assert.equal(state.quickDisabled, true);
});

test("接受处理中（忙）：普通输入、发送、快捷建议全锁", () => {
  const api = loadLockApi();

  const state = api.inputControlState(null, true);

  assert.equal(state.inputDisabled, true);
  assert.equal(state.sendDisabled, true);
  assert.equal(state.quickDisabled, true);
});

test("失败待决：没有新的权威菜单就**保持原锁**，不因失败放开普通聊天", () => {
  const api = loadLockApi();

  // 失败那一步的锁还在（服务端仍留着计划 / 当前步骤）。
  const state = api.inputControlState(CANDIDATE_MENU, false);

  assert.equal(state.inputDisabled, true);
  assert.equal(state.sendDisabled, true);
  assert.equal(state.quickDisabled, true);
});

test("受限修改输入那一步：普通输入锁着，专用输入是唯一合法入口", () => {
  const api = loadLockApi();

  const state = api.inputControlState(EDIT_REQUEST_STEP, false);

  assert.equal(state.inputDisabled, true);
  assert.equal(state.sendDisabled, true);
  assert.equal(state.quickDisabled, true);
});

test("没有待决步骤时：普通输入保持可用（不是一路锁死）", () => {
  const api = loadLockApi();

  const state = api.inputControlState(null, false);

  assert.equal(state.inputDisabled, false);
  assert.equal(state.sendDisabled, false);
  assert.equal(state.quickDisabled, false);
});

test("历史兜底出来的旧菜单（authoritative=false）不锁输入", () => {
  const api = loadLockApi();

  const state = api.inputControlState(
    { id: "template_edit_confirm", lockScope: "menu_only", authoritative: false, options: [{ value: "同意" }] },
    false
  );

  assert.equal(state.inputDisabled, false);
});

// ==========================================================================
// syncInputControls：真的把控件锁住
// ==========================================================================

function loadSyncApi(dom, { hasPendingPlan = false } = {}) {
  const names = [
    "syncInputControls", "syncInputLockHint", "isBusy", "lockScopeOf", "isInputLocked",
    "isAuthoritativeChoice", "menuLocksInput", "inputControlState",
  ];
  const sandbox = loadApi(names, {
    extraStatements: [
      ...LOCK_CONSTS,
      "var activeRequest = null;",
      "var advanceTimers = new Set();",
      "var lockedChoice = null;",
      "var inputLockHintEl = null;",
      "var $ = function (id) { return document.getElementById(id); };",
    ],
    globals: {
      document: dom.document,
      hasPendingPlan: () => hasPendingPlan,
      // syncInputControls 会看 session?.stoppedTaskId（停止现场）。
      session: { stoppedTaskId: null },
    },
  });
  return sandbox;
}

test("syncInputControls：澄清待决时普通输入框与发送按钮都锁住", () => {
  const dom = makeWorkbenchDom();
  const sandbox = loadSyncApi(dom);
  sandbox.lockedChoice = CLARIFY_TEXT_QUESTION;

  sandbox.__api.syncInputControls();

  assert.equal(dom.input.disabled, true, "普通输入框应当锁住");
  assert.equal(dom.send.disabled, true, "发送按钮应当锁住");
  assert.equal(dom.quick.disabled, true, "快捷建议应当锁住");
  assert.equal(dom.quick2.disabled, true);
  assert.equal(dom.input.placeholder, "请在卡片内填写后提交");
});

test("syncInputControls：澄清待决时，专用输入是开着的（唯一合法入口）", () => {
  const dom = makeWorkbenchDom();
  const sandbox = loadSyncApi(dom);
  sandbox.lockedChoice = CLARIFY_TEXT_QUESTION;

  sandbox.__api.syncInputControls();

  assert.equal(dom.textInput.disabled, false, "专用输入要能填");
  assert.equal(dom.textSubmit.disabled, false, "专用输入的提交按钮要能点");
});

test("syncInputControls：专用输入只对**当前待答**的那张卡开放", () => {
  const dom = makeWorkbenchDom();
  const sandbox = loadSyncApi(dom);
  // 当前待答的是别的卡（不是这张澄清卡）。
  sandbox.lockedChoice = { id: "other_step", lockScope: "input_and_menu", options: [{ value: "A" }] };

  sandbox.__api.syncInputControls();

  assert.equal(dom.textInput.disabled, true, "不是当前待答的那张卡 → 专用输入停用");
  assert.equal(dom.textSubmit.disabled, true);
});

test("syncInputControls：候选待选时普通输入框、发送按钮、快捷建议都锁住", () => {
  const dom = makeWorkbenchDom();
  const sandbox = loadSyncApi(dom);
  sandbox.lockedChoice = CANDIDATE_MENU;

  sandbox.__api.syncInputControls();

  assert.equal(dom.input.disabled, true);
  assert.equal(dom.send.disabled, true);
  assert.equal(dom.quick.disabled, true);
});

test("syncInputControls：接受处理中（忙）时普通输入框与发送按钮都锁住", () => {
  const dom = makeWorkbenchDom();
  const sandbox = loadSyncApi(dom);
  sandbox.lockedChoice = null;
  sandbox.activeRequest = { id: "req" };

  sandbox.__api.syncInputControls();

  assert.equal(dom.input.disabled, true);
  assert.equal(dom.send.disabled, true);
  assert.equal(dom.quick.disabled, true);
});

// ==========================================================================
// 澄清卡：文字型待决要真的有专用输入控件
// ==========================================================================

test("澄清卡（expects=TEXT、无选项、无「其他」）：画出专用输入与提交按钮", () => {
  const bubbles = [];
  const api = loadApi([
    "appendAiQuestions", "renderedStepCard", "renderQuestionOptions", "renderOtherAnswerHtml", "otherAnswerLabel",
    "otherAnswerPlaceholder", "bindOtherAnswers", "showOtherAnswerHint", "bindQuestionSelects",
    "inlineAnswerAction", "isBusy", "lockScopeOf", "isInputLocked", "isAuthoritativeChoice",
    "menuLocksInput", "inputControlState",
  ], {
    extraStatements: [...LOCK_CONSTS, "var lockedChoice = null;", "var activeRequest = null;", "var advanceTimers = new Set();"],
    globals: {
      esc: (value) => String(value ?? ""),
      aiBubble: (markup) => {
        const card = { markup, querySelectorAll: () => [], querySelector: () => null };
        bubbles.push(card);
        return card;
      },
      setChoiceLock: () => {},
      clearChoiceLock: () => {},
      syncInputControls: () => {},
      syncInputLockHint: () => {},
      bindOtherAnswers: () => {},
      showOtherAnswerHint: () => {},
      toast: () => {},
      isBusy: () => false,
      sendMessage: async () => ({}),
      requestEpoch: 1,
      $: () => null,
      OTHER_ANSWER_VALUE: "__question_other__",
      EDIT_PLAN_CONTRACT: { requestVisibleRegionIdsField: "visibleRegionIds", choice: { accept: "接受修改", adjust: "继续调整", cancel: "取消" } },
      document: { querySelectorAll: () => [], getElementById: () => null },
    },
  }).__api;

  api.appendAiQuestions([CLARIFY_TEXT_QUESTION]);

  assert.equal(bubbles.length, 1);
  const markup = bubbles[0].markup;
  assert.ok(markup.includes("question-text-input"), "澄清卡里要有专用输入框");
  assert.ok(markup.includes("question-text-submit"), "澄清卡里要有专用输入的提交按钮");
  assert.ok(markup.includes('data-question-text="context_missing_info"'), "专用输入要挂在这一步上");
  assert.ok(markup.includes("直接填写"), "题型说明仍然是「直接填写」");
});

test("澄清卡（expects=TEXT）：抢选择锁 —— 这一步待决，普通输入要锁住", () => {
  const locked = [];
  const api = loadApi([
    "appendAiQuestions", "renderedStepCard", "renderQuestionOptions", "renderOtherAnswerHtml", "otherAnswerLabel",
    "otherAnswerPlaceholder", "bindOtherAnswers", "showOtherAnswerHint", "bindQuestionSelects",
    "inlineAnswerAction", "isBusy", "lockScopeOf", "isInputLocked", "isAuthoritativeChoice",
    "menuLocksInput", "inputControlState",
  ], {
    extraStatements: [...LOCK_CONSTS, "var lockedChoice = null;", "var activeRequest = null;", "var advanceTimers = new Set();"],
    globals: {
      esc: (value) => String(value ?? ""),
      aiBubble: (markup) => ({ markup, querySelectorAll: () => [], querySelector: () => null }),
      setChoiceLock: (question) => locked.push(question),
      clearChoiceLock: () => {},
      syncInputControls: () => {},
      syncInputLockHint: () => {},
      bindOtherAnswers: () => {},
      showOtherAnswerHint: () => {},
      toast: () => {},
      isBusy: () => false,
      sendMessage: async () => ({}),
      requestEpoch: 1,
      $: () => null,
      OTHER_ANSWER_VALUE: "__question_other__",
      EDIT_PLAN_CONTRACT: { requestVisibleRegionIdsField: "visibleRegionIds", choice: { accept: "接受修改", adjust: "继续调整", cancel: "取消" } },
      document: { querySelectorAll: () => [], getElementById: () => null },
    },
  }).__api;

  api.appendAiQuestions([CLARIFY_TEXT_QUESTION]);

  assert.equal(locked.length, 1, "文字型澄清也要抢选择锁");
  assert.equal(locked[0].id, "context_missing_info");
  assert.equal(locked[0].stepId, "step_clarify_1", "步骤身份要带上（下一轮提交靠它关联）");
});

test("服务端澄清卡（expects=TEXT + allowsOther + 无选项）：画**专用输入**，不画下拉框与「其他」块", () => {
  const bubbles = [];
  const api = loadApi([
    "appendAiQuestions", "renderedStepCard", "renderQuestionOptions", "renderOtherAnswerHtml", "otherAnswerLabel",
    "otherAnswerPlaceholder", "bindOtherAnswers", "showOtherAnswerHint", "bindQuestionSelects",
    "inlineAnswerAction", "isBusy", "lockScopeOf", "isInputLocked", "isAuthoritativeChoice",
    "menuLocksInput", "inputControlState",
  ], {
    extraStatements: [...LOCK_CONSTS, "var lockedChoice = null;", "var activeRequest = null;", "var advanceTimers = new Set();"],
    globals: {
      esc: (value) => String(value ?? ""),
      aiBubble: (markup) => {
        const card = { markup, querySelectorAll: () => [], querySelector: () => null };
        bubbles.push(card);
        return card;
      },
      setChoiceLock: () => {},
      clearChoiceLock: () => {},
      syncInputControls: () => {},
      syncInputLockHint: () => {},
      bindOtherAnswers: () => {},
      showOtherAnswerHint: () => {},
      toast: () => {},
      isBusy: () => false,
      sendMessage: async () => ({}),
      requestEpoch: 1,
      $: () => null,
      OTHER_ANSWER_VALUE: "__question_other__",
      EDIT_PLAN_CONTRACT: { requestVisibleRegionIdsField: "visibleRegionIds", choice: { accept: "接受修改", adjust: "继续调整", cancel: "取消" } },
      document: { querySelectorAll: () => [], getElementById: () => null },
    },
  }).__api;

  // 服务端给的模型路径澄清卡：TEXT + allowsOther + lockScope=input_and_menu。
  api.appendAiQuestions([{
    id: "template_edit_clarification",
    prompt: "请补充这一步需要的信息。",
    expects: "TEXT",
    allowsOther: true,
    otherLabel: "补充说明",
    otherPlaceholder: "也可以直接在这里补充这一步需要的信息",
    lockScope: "input_and_menu",
    stepId: "step_clarify_model_1",
    stepVersion: 1,
    stepKind: "menu",
    options: [],
  }]);

  const markup = bubbles[0].markup;
  // BUG-009 的验收点：澄清卡内部要有**真正可用**的专用输入控件。
  assert.ok(markup.includes("question-text-input"), "澄清卡里要有专用输入框");
  assert.ok(markup.includes("question-text-submit"), "澄清卡里要有专用输入的提交按钮");
  assert.ok(markup.includes("补充说明"), "专用输入的标题用服务端给的文案");
  assert.ok(!markup.includes("<select"), "文字型待决不画下拉框（那里面没有可选项）");
  assert.ok(!markup.includes("question-other-input"), "不重复画第二个输入框");
});

test("受限修改输入那一步（template_edit_request）：同样是专用输入，标题用「修改要求」", () => {
  const bubbles = [];
  const api = loadApi([
    "appendAiQuestions", "renderedStepCard", "renderQuestionOptions", "renderOtherAnswerHtml", "otherAnswerLabel",
    "otherAnswerPlaceholder", "bindOtherAnswers", "showOtherAnswerHint", "bindQuestionSelects",
    "inlineAnswerAction", "isBusy", "lockScopeOf", "isInputLocked", "isAuthoritativeChoice",
    "menuLocksInput", "inputControlState",
  ], {
    extraStatements: [...LOCK_CONSTS, "var lockedChoice = null;", "var activeRequest = null;", "var advanceTimers = new Set();"],
    globals: {
      esc: (value) => String(value ?? ""),
      aiBubble: (markup) => {
        const card = { markup, querySelectorAll: () => [], querySelector: () => null };
        bubbles.push(card);
        return card;
      },
      setChoiceLock: () => {},
      clearChoiceLock: () => {},
      syncInputControls: () => {},
      syncInputLockHint: () => {},
      bindOtherAnswers: () => {},
      showOtherAnswerHint: () => {},
      toast: () => {},
      isBusy: () => false,
      sendMessage: async () => ({}),
      requestEpoch: 1,
      $: () => null,
      OTHER_ANSWER_VALUE: "__question_other__",
      EDIT_PLAN_CONTRACT: { requestVisibleRegionIdsField: "visibleRegionIds", choice: { accept: "接受修改", adjust: "继续调整", cancel: "取消" } },
      document: { querySelectorAll: () => [], getElementById: () => null },
    },
  }).__api;

  api.appendAiQuestions([EDIT_REQUEST_STEP]);

  const markup = bubbles[0].markup;
  assert.ok(markup.includes("question-text-input"), "受限输入那一步要画专用输入框");
  assert.ok(markup.includes('data-question-text="template_edit_request"'), "专用输入要挂在这一步上");
  assert.ok(markup.includes("修改要求"), "专用输入的标题用服务端给的 otherLabel");
  assert.ok(!markup.includes("<select"), "这一步不画下拉框");
  assert.ok(!markup.includes("question-other-input"), "不重复画第二个输入框");
});

test("一轮里同时有澄清卡和受限输入那一步：锁落在**权威那一张**（与服务端 pending_choice 同口径）", () => {
  const locked = [];
  const api = loadApi([
    "appendAiQuestions", "renderedStepCard", "renderQuestionOptions", "renderOtherAnswerHtml", "otherAnswerLabel",
    "otherAnswerPlaceholder", "bindOtherAnswers", "showOtherAnswerHint", "bindQuestionSelects",
    "inlineAnswerAction", "isBusy", "lockScopeOf", "isInputLocked", "isAuthoritativeChoice",
    "menuLocksInput", "inputControlState",
  ], {
    extraStatements: [...LOCK_CONSTS, "var lockedChoice = null;", "var activeRequest = null;", "var advanceTimers = new Set();"],
    globals: {
      esc: (value) => String(value ?? ""),
      aiBubble: (markup) => ({ markup, querySelectorAll: () => [], querySelector: () => null }),
      setChoiceLock: (question) => locked.push(question),
      clearChoiceLock: () => {},
      syncInputControls: () => {},
      syncInputLockHint: () => {},
      bindOtherAnswers: () => {},
      showOtherAnswerHint: () => {},
      toast: () => {},
      isBusy: () => false,
      sendMessage: async () => ({}),
      requestEpoch: 1,
      $: () => null,
      OTHER_ANSWER_VALUE: "__question_other__",
      EDIT_PLAN_CONTRACT: { requestVisibleRegionIdsField: "visibleRegionIds", choice: { accept: "接受修改", adjust: "继续调整", cancel: "取消" } },
      document: { querySelectorAll: () => [], getElementById: () => null },
    },
  }).__api;

  api.appendAiQuestions([
    { id: "template_edit_clarification", prompt: "请补充。", expects: "TEXT", allowsOther: true, options: [], lockScope: "input_and_menu", stepId: "step_x", stepVersion: 1 },
    EDIT_REQUEST_STEP,
  ]);

  assert.equal(locked.length, 1, "只锁一张");
  assert.equal(locked[0].id, "template_edit_request", "锁落在受限输入那一步（服务端 pending_choice 记的就是它）");
});

test("syncInputControls：当前待答的是文字卡（没有下拉框）时，历史轮的下拉框保持禁用", () => {
  const dom = makeWorkbenchDom();
  const sandbox = loadSyncApi(dom);
  sandbox.lockedChoice = EDIT_REQUEST_STEP;

  sandbox.__api.syncInputControls();

  assert.equal(dom.oldSelect.disabled, true, "旧菜单不能被重新打开（旧步骤不能再选）");
  assert.equal(dom.input.disabled, true, "普通输入仍然锁住");
  assert.equal(dom.send.disabled, true);
});

test("syncInputControls：同一步的澄清菜单与专用输入**两个入口都开着**（契约 §3.3）", () => {
  const dom = makeWorkbenchDom();
  const sandbox = loadSyncApi(dom);
  // 当前这一步是受限修改输入（step_x）；同一轮里还有一张同一步的澄清菜单。
  sandbox.lockedChoice = { ...EDIT_REQUEST_STEP, stepId: "step_x" };

  sandbox.__api.syncInputControls();

  assert.equal(dom.menuSelect.disabled, false, "本步的合法选项要能选");
  assert.equal(dom.stepTextInput.disabled, false, "本步的专用输入要能填");
  assert.equal(dom.stepTextSubmit.disabled, false, "本步的专用输入要能提交");
  assert.equal(dom.oldSelect.disabled, true, "别的步骤的旧菜单仍然锁着");
  assert.equal(dom.input.disabled, true, "普通输入仍然锁住");
});

// ==========================================================================
// 接线：Enter / 发送按钮与输入框用同一处判据
// ==========================================================================

test("Enter 与发送按钮的锁，用的是输入框那一处判据（inputControlState）", () => {
  const enterBlock = WORKBENCH_JS.slice(
    WORKBENCH_JS.indexOf('$("chat-input").addEventListener("keydown"'),
    WORKBENCH_JS.indexOf('$("chat-input").addEventListener("keydown"') + 900
  );
  assert.ok(enterBlock.includes("inputControlState("), "Enter 要用输入框那一处判据");
  assert.ok(!enterBlock.includes("isInputLocked("), "别用只看 lockScope 的窄判据（比真实的锁窄）");

  const sendBlock = WORKBENCH_JS.slice(
    WORKBENCH_JS.indexOf('$("btn-send").addEventListener("click"'),
    WORKBENCH_JS.indexOf('$("btn-send").addEventListener("click"') + 900
  );
  assert.ok(sendBlock.includes("inputControlState("), "发送按钮也要用同一处判据");
});

test("「没有候选」/「返回了一段文字」不是解锁理由：受限步骤没拿到新菜单时不主动解锁", () => {
  const block = WORKBENCH_JS.slice(
    WORKBENCH_JS.indexOf("async function sendMessage("),
    WORKBENCH_JS.indexOf("// --------------------------------------------------------------------------\n// 请求")
  );
  assert.ok(block.length > 0, "抽得到 sendMessage");
  // 解锁只发生在两处：回答菜单后的 clearChoiceLock()，以及明确的新权威菜单。
  const clears = block.split("clearChoiceLock()").length - 1;
  assert.ok(clears >= 1, "有清锁的地方");
  assert.ok(
    block.includes("syncChoiceLockFromServer") || block.includes("lockedChoice = null"),
    "清锁要么由新菜单接管，要么向服务端问一句 —— 不是「请求结束就解锁」"
  );
});
