// 工单 #62（统一开发 07）：编辑态与选择阶段 —— 选择锁与统一对话。
//
// 运行：node --test frontend/tests/workbench_edit_lock.test.mjs
// 不依赖任何第三方包（Node 内置 test runner + node:vm），不改共用依赖配置。
//
// ── 被测的缝（seam）─────────────────────────────────────────────
// 工作台主脚本（frontend/pages/workbench.js）是**浏览器脚本**，不是模块：
// 顶部就摸 window / document，直接 require 会炸。
// 所以这里用 node:vm 把「纯逻辑那几段函数」原样抽出来，在沙箱里跑：
//
//   - 锁范围判定（lockScopeOf / isInputLocked）
//   - 输入区状态判定（inputControlState）
//   - 菜单归一化（normalizeConfirmQuestion / pendingEditMenuQuestion）
//   - 输入区控件同步（syncInputControls，带假 DOM）
//
// 这些函数在源码里是**同一份文本**，不是复制品：抽不出来就直接测试失败。
//
// 与后端的契约（契约表 §5「有效菜单与输入锁」）：
//   lockScope = "none" | "menu_only" | "input_and_menu"
//   老数据没有这个键 → 按 "menu_only" 处理（等于今天的行为）。
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

/** 一个够用的假元素：记录 disabled / placeholder / title / classList。 */
function makeElement(extra = {}) {
  const classes = new Set();
  return {
    disabled: false,
    placeholder: "",
    title: "",
    textContent: "",
    className: "",
    value: "",
    dataset: {},
    classList: {
      toggle(name, on) {
        if (on) classes.add(name);
        else classes.delete(name);
      },
      contains(name) {
        return classes.has(name);
      },
    },
    ...extra,
  };
}

/**
 * 造一个能跑 syncInputControls 的假 DOM。
 * 返回 { sandbox, elements }：elements 里有输入框、发送按钮、快捷入口、提示条。
 */
function makeWorkbenchDom() {
  const input = makeElement();
  const send = makeElement();
  const stop = makeElement();
  const quick = makeElement();
  const quick2 = makeElement();
  const inputRow = makeElement();
  const inputBox = makeElement();
  const hint = makeElement();

  // 输入框挂在一个 flex 行里，行再挂在一个容器里 —— 提示条挂在容器上。
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
    getElementById(id) {
      return elements[id] || null;
    },
    createElement() {
      const el = makeElement();
      el.setAttribute = (name) => {
        el.dataset[name] = true;
      };
      el.appendChild = (child) => child;
      return el;
    },
    querySelectorAll(selector) {
      if (selector.includes("data-quick")) return [quick, quick2];
      return [];
    },
  };

  return { document, elements, input, send, quick, quick2, hint };
}

/** 把选中的片段放进沙箱跑一遍，返回它们组成的 API。 */
function loadApi(names, { extraStatements = [], globals = {} } = {}) {
  const snippets = [
    ...extraStatements,
    ...names.map((name) => extractFunction(name)),
  ].join("\n\n");
  const sandbox = {
    console: { warn() {}, error() {}, log() {} },
    session: null,
    window: {},
    ...globals,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    snippets + "\n;globalThis.__api = {" + names.join(", ") + "};",
    sandbox,
  );
  return sandbox;
}

// 锁相关的纯函数沙箱（不碰 DOM）。
function loadLockApi() {
  const names = ["lockScopeOf", "isInputLocked", "inputControlState"];
  const sandbox = loadApi(names, {
    extraStatements: [
      extractConst("LOCK_SCOPE_NONE"),
      extractConst("LOCK_SCOPE_MENU_ONLY"),
      extractConst("LOCK_SCOPE_INPUT_AND_MENU"),
      extractConst("INPUT_LOCK_HINT"),
    ],
  });
  return sandbox.__api;
}

// ---------------------------------------------------------------- 契约样例

const DESIGN_MENU = {
  id: "template_edit_confirm",
  prompt: "请选择下一步操作：",
  expects: "SINGLE_CHOICE",
  selectionOnly: true,
  lockScope: "input_and_menu",
  options: [
    { value: "同意", label: "同意" },
    { value: "修改", label: "修改" },
    { value: "重新开始", label: "重新开始" },
  ],
};

const EDIT_PLAN_MENU = {
  id: "pending_edit_plan_confirm",
  prompt: "确认这次修改：",
  expects: "SINGLE_CHOICE",
  selectionOnly: true,
  lockScope: "input_and_menu",
  options: [
    { value: "接受修改", label: "接受修改" },
    { value: "继续调整", label: "继续调整" },
    { value: "取消", label: "取消" },
  ],
};

const OLD_MENU_WITHOUT_LOCK_SCOPE = {
  id: "template_edit_confirm",
  prompt: "请选择下一步操作：",
  expects: "SINGLE_CHOICE",
  selectionOnly: true,
  options: [{ value: "同意", label: "同意" }],
};

// ==========================================================================
// 锁范围的判定（契约表 §5.1、§5.2、§5.3）
// ==========================================================================

test("锁范围只认服务端菜单上的 lockScope", () => {
  const api = loadLockApi();

  assert.equal(api.lockScopeOf(DESIGN_MENU), "input_and_menu");
  assert.equal(api.lockScopeOf(EDIT_PLAN_MENU), "input_and_menu");
  assert.equal(api.lockScopeOf({ lockScope: "menu_only" }), "menu_only");
  assert.equal(api.lockScopeOf({ lockScope: "none" }), "none");
});

test("老数据没有 lockScope 时按 menu_only 处理（不锁输入）", () => {
  const api = loadLockApi();

  assert.equal(api.lockScopeOf(OLD_MENU_WITHOUT_LOCK_SCOPE), "menu_only");
  assert.equal(api.lockScopeOf(null), "menu_only");
  assert.equal(api.isInputLocked(OLD_MENU_WITHOUT_LOCK_SCOPE), false);
});

test("认不出的 lockScope 不当成解锁，按 menu_only 兜底", () => {
  const api = loadLockApi();

  assert.equal(api.lockScopeOf({ lockScope: "whatever" }), "menu_only");
});

// ==========================================================================
// 输入区状态（AC1：锁输入框 / 发送按钮 / 提示文案）
// ==========================================================================

test("等待下一步操作选择时：输入框、发送按钮都锁住，并提示「请先选择下一步操作」", () => {
  const api = loadLockApi();

  const state = api.inputControlState(DESIGN_MENU, false);

  assert.equal(state.inputLocked, true);
  assert.equal(state.inputDisabled, true);
  assert.equal(state.sendDisabled, true);
  assert.equal(state.placeholder, "请先选择下一步操作");
  assert.equal(state.hint, "请先选择下一步操作");
});

test("待确认修改计划出现时也恢复选择锁（输入框一起锁）", () => {
  const api = loadLockApi();

  const state = api.inputControlState(EDIT_PLAN_MENU, false);

  assert.equal(state.inputDisabled, true);
  assert.equal(state.sendDisabled, true);
});

test("只有菜单锁（menu_only）时输入框仍可用 —— 咨询还要能发出去", () => {
  const api = loadLockApi();

  const state = api.inputControlState({ lockScope: "menu_only" }, false);

  assert.equal(state.inputLocked, false);
  assert.equal(state.inputDisabled, false);
  assert.equal(state.sendDisabled, false);
  assert.equal(state.placeholder, "可以先问只读问题，或从上方下拉框选择");
  assert.equal(state.hint, "");
});

test("进入统一对话（none）时完全不锁", () => {
  const api = loadLockApi();

  const state = api.inputControlState({ lockScope: "none" }, false);

  assert.equal(state.inputDisabled, false);
  assert.equal(state.sendDisabled, false);
  assert.equal(state.placeholder, "可以先问只读问题，或从上方下拉框选择");
});

test("没有菜单时输入框可用（文字方案阶段不被一并锁死）", () => {
  const api = loadLockApi();

  const state = api.inputControlState(null, false);

  assert.equal(state.inputDisabled, false);
  assert.equal(state.sendDisabled, false);
  assert.equal(state.placeholder, "描述报表需求，或输入修改要求…");
});

test("正在生成时输入框仍然是锁的（忙优先于菜单）", () => {
  const api = loadLockApi();

  assert.equal(api.inputControlState(null, true).inputDisabled, true);
  assert.equal(api.inputControlState(null, true).sendDisabled, true);
});

test("快捷入口在有菜单时一律不给点", () => {
  const api = loadLockApi();

  assert.equal(api.inputControlState(DESIGN_MENU, false).quickDisabled, true);
  assert.equal(api.inputControlState({ lockScope: "menu_only" }, false).quickDisabled, true);
  assert.equal(api.inputControlState(null, false).quickDisabled, false);
});

// ==========================================================================
// 菜单归一化不能把锁弄丢（AC1、AC5）
// ==========================================================================

test("归一化菜单时 lockScope 原样带过去", () => {
  const api = loadApi(["normalizeConfirmQuestion"]).__api;

  const normalized = api.normalizeConfirmQuestion(DESIGN_MENU);

  assert.equal(normalized.lockScope, "input_and_menu");
  assert.equal(normalized.selectionOnly, true);
  assert.equal(normalized.id, "template_edit_confirm");
});

test("老菜单没有 lockScope 时归一化也不凭空解锁", () => {
  const api = loadApi(["normalizeConfirmQuestion"]).__api;

  const normalized = api.normalizeConfirmQuestion(OLD_MENU_WITHOUT_LOCK_SCOPE);

  assert.equal(normalized.lockScope, undefined);
});

test("待确认修改计划的菜单带上服务端的 lockScope（恢复选择锁）", () => {
  const api = loadApi(["pendingEditMenuQuestion"]).__api;

  const menu = api.pendingEditMenuQuestion(EDIT_PLAN_MENU, EDIT_PLAN_MENU.options);

  assert.equal(menu.id, "pending_edit_plan_confirm");
  assert.equal(menu.lockScope, "input_and_menu");
  assert.equal(menu.options.length, 3);
});

test("服务端没给菜单时，待确认计划的菜单退回契约里的 id", () => {
  const api = loadApi(["pendingEditMenuQuestion"]).__api;

  const menu = api.pendingEditMenuQuestion(null, []);

  assert.equal(menu.id, "pending_edit_plan_confirm");
  assert.equal(menu.lockScope, undefined);
});

// ==========================================================================
// 输入区控件真的被锁住（带假 DOM 跑 syncInputControls）
// ==========================================================================

function loadSyncApi(dom) {
  const names = ["syncInputControls", "syncInputLockHint", "isBusy", "lockScopeOf", "isInputLocked", "inputControlState"];
  const sandbox = loadApi(names, {
    extraStatements: [
      extractConst("LOCK_SCOPE_NONE"),
      extractConst("LOCK_SCOPE_MENU_ONLY"),
      extractConst("LOCK_SCOPE_INPUT_AND_MENU"),
      extractConst("INPUT_LOCK_HINT"),
      // 沙箱里的模块级状态：用 var 才能从外部改（vm 里 let 是脚本作用域）。
      "var activeRequest = null;",
      "var advanceTimers = new Set();",
      "var lockedChoice = null;",
      "var inputLockHintEl = null;",
      // syncInputControls 里的 $ 与 document 由用例注入。
      "var $ = function (id) { return document.getElementById(id); };",
    ],
    globals: { document: dom.document },
  });
  return sandbox;
}

test("syncInputControls：等待下一步操作选择时把输入框与发送按钮都锁住", () => {
  const dom = makeWorkbenchDom();
  const sandbox = loadSyncApi(dom);
  sandbox.lockedChoice = DESIGN_MENU;

  sandbox.__api.syncInputControls();

  assert.equal(dom.input.disabled, true, "输入框应当锁住");
  assert.equal(dom.send.disabled, true, "发送按钮应当锁住");
  assert.equal(dom.input.placeholder, "请先选择下一步操作");
  assert.equal(dom.send.title, "请先选择下一步操作");
  assert.equal(dom.quick.disabled, true, "快捷入口应当锁住");
  assert.equal(dom.quick2.disabled, true);
});

test("syncInputControls：选了「修改」之后（菜单失效）输入框恢复可用", () => {
  const dom = makeWorkbenchDom();
  const sandbox = loadSyncApi(dom);
  sandbox.lockedChoice = null;

  sandbox.__api.syncInputControls();

  assert.equal(dom.input.disabled, false);
  assert.equal(dom.send.disabled, false);
  assert.equal(dom.input.placeholder, "描述报表需求，或输入修改要求…");
  assert.equal(dom.quick.disabled, false);
});

test("syncInputControls：只有菜单锁（menu_only）时输入框仍然可用", () => {
  const dom = makeWorkbenchDom();
  const sandbox = loadSyncApi(dom);
  sandbox.lockedChoice = { id: "template_edit_confirm", lockScope: "menu_only", options: [{ value: "同意" }] };

  sandbox.__api.syncInputControls();

  assert.equal(dom.input.disabled, false);
  assert.equal(dom.send.disabled, false);
  assert.equal(dom.input.placeholder, "可以先问只读问题，或从上方下拉框选择");
  assert.equal(dom.quick.disabled, true, "有菜单时快捷入口仍然不给点");
});

test("syncInputControls：老会话的菜单（没有 lockScope）不锁输入", () => {
  const dom = makeWorkbenchDom();
  const sandbox = loadSyncApi(dom);
  sandbox.lockedChoice = OLD_MENU_WITHOUT_LOCK_SCOPE;

  sandbox.__api.syncInputControls();

  assert.equal(dom.input.disabled, false);
  assert.equal(dom.send.disabled, false);
});

// ==========================================================================
// AC10 不新增独立「询问」按钮来绕过选择锁
// ==========================================================================

test("页面里没有独立的「询问」按钮（要继续咨询先选「继续调整」）", () => {
  const html = readFileSync(new URL("../pages/workbench.html", import.meta.url), "utf8");
  const js = readFileSync(new URL("../pages/workbench.js", import.meta.url), "utf8");

  // 静态页面里没有「询问」这个按钮。
  assert.equal(html.includes("询问"), false);
  // 脚本里也不会凭空造一个。
  assert.equal(js.includes("询问"), false);
  // 「继续调整」这个入口是既有的（待确认修改计划的三个选项之一）。
  assert.equal(js.includes("继续调整"), true);
});

