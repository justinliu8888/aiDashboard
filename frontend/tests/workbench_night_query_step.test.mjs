// 夜间第二轮（用户现场截图）：纯问句回答之后**不得**多跳一张修改要求卡。
//
// 运行：node --test frontend/tests/workbench_night_query_step.test.mjs
// 不依赖任何第三方包（Node 内置 test runner + node:vm）。
//
// ── 被测的缝（seam）────────────────────────────────────────────
// 沿用既有范式：用 node:vm 把「纯逻辑那几段函数」**原样抽出来**在沙箱里跑
// （抽不出来直接测试失败 —— 不是复制粘贴的实现）。
//
//   - renderedStepCard            屏幕上有没有同一步骤（stepId）的卡
//   - appendAiQuestions           同一步骤的卡**只画一张**，但锁要接着
//   - restoreStepLockAfterReadOnly 只读回答之后把受限步骤的锁接回去
//
// ── 现象（用户截图）────────────────────────────────────────────
// 用户在「请描述你想怎样修改当前草稿」那张卡里问了一句
// 「告诉我『昨日能耗趋势』的位置、尺寸和当前样式」，
// 助手**答对了**，但答案下面又贴出一张**一模一样**的卡 ——
// 用户看到的就是「回答完了又跳一个莫名其妙的东西」。
//
// 契约（共享契约 §3.3 + ADR 0002 §6.1）：
//   - 查询回答后：原菜单、pendingChoice、待确认修改计划、澄清轮次**不变**；
//   - 同一步骤的卡只画一张（重画 = 用户以为流程回退了）；
//   - 只读回答**不结束**受限步骤 → 普通输入继续锁着。
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

// ---------------------------------------------------------------- 假 DOM

/** 屏幕上一张已画好的卡：只带 data-step-id。 */
function makeCard(stepId, question = EDIT_STEP) {
  const attrs = {
    "data-step-id": stepId,
    "data-step-question": JSON.stringify([
      question.id, question.expects, question.prompt || question.text || "",
      question.options || [], question.stepVersion,
    ]),
  };
  return {
    getAttribute: (name) => attrs[name] ?? null,
    querySelectorAll: () => [],
    querySelector: () => null,
  };
}

/**
 * 造一个假 document：`[data-step-id]` 返回屏幕上已经画着的卡。
 * cards 由测试自己往里塞（模拟「上一轮已经画过这张卡」）。
 */
function makeDocument(cards) {
  return {
    querySelectorAll(selector) {
      if (selector.includes("[data-step-id]")) return cards;
      return [];
    },
    getElementById: () => null,
    createElement: () => makeCard(""),
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

// ---------------------------------------------------------------- 契约样例

/** 受限修改输入那一步（服务端给的那张「直接填写」卡）。 */
const EDIT_STEP = {
  id: "template_edit_request",
  prompt: "请描述你想怎样修改当前草稿：",
  expects: "TEXT",
  options: [],
  selectionOnly: false,
  allowsOther: true,
  otherLabel: "修改要求",
  otherPlaceholder: "直接输入修改要求",
  lockScope: "input_and_menu",
  stepId: "step_edit_request_1",
  stepVersion: 1,
  stepKind: "edit_request",
  allowedSubmissions: ["edit_text"],
};

/** 另一步（不同 stepId）—— 用来证明「按步骤身份区分，不是按 id 一刀切」。 */
const OTHER_STEP = { ...EDIT_STEP, stepId: "step_edit_request_2" };

/** appendAiQuestions 需要的那些外部依赖（沙箱里必须齐）。 */
const QUESTION_GLOBALS = {
  esc: (value) => String(value ?? ""),
  renderQuestionOptions: () => "",
  renderOtherAnswerHtml: () => "",
  otherAnswerLabel: () => "其他",
  otherAnswerPlaceholder: () => "直接输入修改要求",
  bindOtherAnswers: () => {},
  showOtherAnswerHint: () => {},
  bindQuestionSelects: () => {},
  inlineAnswerAction: "inline",
  isBusy: () => false,
  toast: () => {},
  sendMessage: async () => ({}),
  requestEpoch: 1,
  $: () => null,
  OTHER_ANSWER_VALUE: "__question_other__",
  EDIT_PLAN_CONTRACT: {
    requestVisibleRegionIdsField: "visibleRegionIds",
    choice: { accept: "接受修改", adjust: "继续修改", cancel: "取消" },
  },
};

function loadQuestionsApi({ cards = [], locked = [] } = {}) {
  const bubbles = [];
  const api = loadApi(
    ["renderedStepCard", "appendAiQuestions"],
    {
      globals: {
        ...QUESTION_GLOBALS,
        document: makeDocument(cards),
        aiBubble: (markup) => {
          const bubble = { markup, querySelectorAll: () => [], querySelector: () => null };
          bubbles.push(bubble);
          return bubble;
        },
        setChoiceLock: (question) => locked.push(question),
        clearChoiceLock: () => {},
        syncInputControls: () => {},
        syncInputLockHint: () => {},
      },
    },
  ).__api;
  return { api, bubbles, locked };
}

// ==========================================================================
// renderedStepCard：按步骤身份找屏幕上已有的那张卡
// ==========================================================================

test("renderedStepCard：按 stepId 找到屏幕上那张卡", () => {
  const card = makeCard("step_edit_request_1");
  const api = loadApi(["renderedStepCard"], {
    globals: { document: makeDocument([card]) },
  }).__api;

  assert.equal(api.renderedStepCard("step_edit_request_1"), card);
  assert.equal(api.renderedStepCard("step_edit_request_2"), null);
  assert.equal(api.renderedStepCard(""), null);
});

// ==========================================================================
// appendAiQuestions：同一步骤的卡只画一张
// ==========================================================================

test("appendAiQuestions：屏幕上已有的那一步**不再画第二张**（用户截图那处）", () => {
  const { api, bubbles } = loadQuestionsApi({ cards: [makeCard("step_edit_request_1")] });

  api.appendAiQuestions([EDIT_STEP]);

  assert.equal(bubbles.length, 0, "同一步骤的卡不能重画（用户看到的就是这个）");
});

test("appendAiQuestions：不重画，但**锁要接着**（这一步还没结束）", () => {
  const { api, bubbles, locked } = loadQuestionsApi({ cards: [makeCard("step_edit_request_1")] });

  api.appendAiQuestions([EDIT_STEP]);

  assert.equal(bubbles.length, 0);
  assert.equal(locked.length, 1, "步骤还在 → 锁要接回去");
  assert.equal(locked[0].stepId, "step_edit_request_1");
});

test("appendAiQuestions：同一步从文字追问变成选卡菜单时必须显示 dropdown", () => {
  const { api, bubbles, locked } = loadQuestionsApi({ cards: [makeCard(EDIT_STEP.stepId)] });
  const menu = {
    ...EDIT_STEP, expects: "SINGLE_CHOICE", selectionOnly: true,
    prompt: "要把冰箱耗电量加到哪张卡片？",
    options: [{ value: "region_2", label: "设备耗电排名" }],
  };
  api.appendAiQuestions([menu]);
  assert.equal(bubbles.length, 1, "旧文字卡不能挡住本轮的选卡 dropdown");
  assert.match(bubbles[0].markup, /<select /);
  assert.equal(locked[0].options[0].value, "region_2");
});

test("appendAiQuestions：**别步**的卡照常画出来（按步骤身份区分，不是一刀切）", () => {
  const { api, bubbles } = loadQuestionsApi({ cards: [makeCard("step_edit_request_1")] });

  api.appendAiQuestions([OTHER_STEP]);

  assert.equal(bubbles.length, 1, "别一步的卡要画出来");
  assert.ok(bubbles[0].markup.includes('data-step-id="step_edit_request_2"'));
});

test("appendAiQuestions：没有步骤身份的旧菜单照常画", () => {
  const { api, bubbles } = loadQuestionsApi({ cards: [] });
  const legacy = { ...EDIT_STEP, stepId: undefined, stepKind: undefined };

  api.appendAiQuestions([legacy]);

  assert.equal(bubbles.length, 1);
});

test("appendAiQuestions：同一轮里两张同一步的卡要一起画出来（不是重复）", () => {
  const { api, bubbles } = loadQuestionsApi({ cards: [] });
  const menu = {
    id: "template_edit_confirm",
    prompt: "请选择下一步操作：",
    expects: "SINGLE_CHOICE",
    selectionOnly: true,
    allowsOther: true,
    options: [{ value: "同意", label: "同意" }],
    lockScope: "input_and_menu",
    stepId: "step_same_1",
    stepVersion: 1,
    stepKind: "menu",
  };
  const step = { ...EDIT_STEP, stepId: "step_same_1" };

  api.appendAiQuestions([menu, step]);

  // 同一步的两张卡是**这一轮**一起画的：不是重复，要都在屏幕上。
  assert.equal(bubbles.length, 1, "同一步的两张卡画在同一张卡壳里");
  assert.ok(bubbles[0].markup.includes('data-step-id="step_same_1"'));
});

test("appendAiQuestions：同一步已有文字卡时，新选项不能被当成重复问题", () => {
  const { api, bubbles } = loadQuestionsApi({ cards: [makeCard("step_same_1")] });
  const menu = {
    id: "template_edit_confirm",
    prompt: "请选择下一步操作：",
    expects: "SINGLE_CHOICE",
    selectionOnly: true,
    allowsOther: true,
    options: [{ value: "同意", label: "同意" }],
    lockScope: "input_and_menu",
    stepId: "step_same_1",
    stepVersion: 1,
    stepKind: "menu",
  };
  const step = { ...EDIT_STEP, stepId: "step_same_1" };

  api.appendAiQuestions([menu, step]);

  assert.equal(bubbles.length, 1, "同一步有新选项 → 必须显示新菜单");
});

// ==========================================================================
// restoreStepLockAfterReadOnly：只读回答之后把锁接回去
// ==========================================================================

test("restoreStepLockAfterReadOnly：按服务端带回的那一步上锁", () => {
  const locked = [];
  let synced = 0;
  const api = loadApi(["restoreStepLockAfterReadOnly"], {
    globals: {
      setChoiceLock: (question) => locked.push(question),
      syncInputControls: () => { synced += 1; },
      syncChoiceLockFromServer: () => null,
    },
  }).__api;

  api.restoreStepLockAfterReadOnly({ questions: [EDIT_STEP] });

  assert.equal(locked.length, 1, "只读回答之后普通输入要继续锁着");
  assert.equal(locked[0].stepId, "step_edit_request_1");
  assert.equal(synced, 1);
});

test("restoreStepLockAfterReadOnly：响应里没带那一步就问服务端要一句", () => {
  const locked = [];
  let asked = 0;
  const api = loadApi(["restoreStepLockAfterReadOnly"], {
    globals: {
      setChoiceLock: (question) => locked.push(question),
      syncInputControls: () => {},
      syncChoiceLockFromServer: () => { asked += 1; return null; },
    },
  }).__api;

  api.restoreStepLockAfterReadOnly({ questions: [] });

  assert.equal(asked, 1, "服务端是唯一说了算的（契约 §3.3）");
  assert.equal(locked.length, 0);
});
