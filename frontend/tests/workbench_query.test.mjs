// Agent B：工作台只读问答的前端决策行为测试。
//
// 运行：node --test frontend/tests/*.test.mjs
// （目录参数在 Node 24 上不匹配默认测试文件规则，要显式给 glob；
//   跑单个文件 node --test frontend/tests/workbench_query.test.mjs 也可以）
// 不依赖任何第三方包（用 Node 内置 test runner），不改动共用依赖配置。
//
// 这些测试只覆盖「外部可见的行为决策」：
//   - 哪些响应算只读问答（不推进业务）
//   - 只读问答会不会触发自动推进 __advance__
//   - 只读问答会不会改业务状态 / 重画业务菜单 / 重画右侧预览
//   - 待确认菜单在历史回放后是否仍然「开着」
// 渲染细节（DOM 样式、文案）由 flows/ 下的交互流覆盖。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const Q = require("../pages/workbench_query.js");

// ---------------------------------------------------------------- 只读语义识别

test("普通业务响应不被识别成只读问答", () => {
  assert.equal(Q.readOnlyAnswerKind({ state: "WAITING_USER_CONFIRM" }), null);
  assert.equal(Q.readOnlyAnswerKind({ state: "ANALYZING", uiActions: ["suggest_prompt"] }), null);
  assert.equal(Q.readOnlyAnswerKind({ state: "VALIDATING", uiActions: ["show_diff"] }), null);
  assert.equal(Q.readOnlyAnswerKind(null), null);
  assert.equal(Q.readOnlyAnswerKind(undefined), null);
  assert.equal(Q.readOnlyAnswerKind("not-an-object"), null);
});

test("uiActions 标记的只读回答与只读澄清能被识别", () => {
  assert.equal(Q.readOnlyAnswerKind({ uiActions: ["query_answer"] }), "ANSWER");
  assert.equal(
    Q.readOnlyAnswerKind({ uiActions: ["query_answer"], debug: { queryOutcome: "clarification" } }),
    "CLARIFICATION"
  );
  assert.equal(
    Q.readOnlyAnswerKind({ uiActions: ["query_answer"], debug: { needsClarification: true } }),
    "CLARIFICATION"
  );
});

test("主契约是 turnKind=query_answer（后端共用契约 ADR 0002）", () => {
  const queryTurn = {
    state: "WAITING_USER_CONFIRM",
    reply: "当前对话里有一份草稿「A 机房昨日能耗日报」。",
    turnKind: "query_answer",
    uiActions: ["query_answer"],
    questions: [],
    toolRequests: [],
    debug: { queryMode: true, factStatus: "FOUND" },
  };
  assert.equal(Q.readOnlyAnswerKind(queryTurn), "ANSWER");
  assert.equal(Q.isReadOnlyResponse(queryTurn), true);

  const clarificationTurn = {
    ...queryTurn,
    debug: { queryMode: true, queryOutcome: "clarification", needsClarification: true },
  };
  assert.equal(Q.readOnlyAnswerKind(clarificationTurn), "CLARIFICATION");
  assert.equal(Q.isQueryClarification(clarificationTurn), true);
});

test("非法标记值不生效，按普通业务响应处理", () => {
  assert.equal(Q.readOnlyAnswerKind({ turnKind: "MAYBE" }), null);
  assert.equal(Q.readOnlyAnswerKind({ readOnlyAnswerKind: "ANSWER" }), null);
  assert.equal(Q.readOnlyAnswerKind({ uiActions: ["read_only_unknown"] }), null);
});

test("isReadOnlyResponse 与 readOnlyAnswerKind 口径一致", () => {
  assert.equal(Q.isReadOnlyResponse({ turnKind: "query_answer", uiActions: ["query_answer"] }), true);
  assert.equal(Q.isReadOnlyResponse({ turnKind: "query_answer", uiActions: ["query_answer"], debug: { queryOutcome: "clarification" } }), true);
  assert.equal(Q.isReadOnlyResponse({ state: "ANALYZING" }), false);
});

// ---------------------------------------------------------------- 不推进业务

test("只读回答不触发自动推进（红线：保留推进阶段的查询响应不得发 __advance__）", () => {
  assert.equal(Q.shouldAutoAdvance({ state: "VALIDATING" }), true);
  assert.equal(Q.shouldAutoAdvance({ state: "SAVE_DEFINITION_DRAFT" }), true);
  assert.equal(Q.shouldAutoAdvance({ state: "PREVIEWING" }), true);
  assert.equal(Q.shouldAutoAdvance({ state: "PREVIEW_READY" }), true);
  assert.equal(Q.shouldAutoAdvance({ state: "RENDERING" }), true);

  assert.equal(Q.shouldAutoAdvance({ turnKind: "query_answer", state: "VALIDATING", uiActions: ["query_answer"] }), false);
  assert.equal(Q.shouldAutoAdvance({ turnKind: "query_answer", state: "PREVIEWING", uiActions: ["query_answer"] }), false);
  assert.equal(
    Q.shouldAutoAdvance({ turnKind: "query_answer", state: "SAVE_DEFINITION_DRAFT", uiActions: ["query_answer"], debug: { queryOutcome: "clarification" } }),
    false
  );
});

test("本来就不推进的状态始终不推进", () => {
  assert.equal(Q.shouldAutoAdvance({ state: "ANALYZING" }), false);
  assert.equal(Q.shouldAutoAdvance({ state: "WAITING_USER_CONFIRM" }), false);
  assert.equal(Q.shouldAutoAdvance({ state: "NEEDS_CLARIFICATION" }), false);
  assert.equal(Q.shouldAutoAdvance({ state: "READY" }), false);
});

test("只读问答保持原业务状态，不被查询结果的临时状态覆盖", () => {
  assert.equal(Q.shouldPreserveBusinessState({ turnKind: "query_answer", uiActions: ["query_answer"] }), true);
  assert.equal(Q.shouldPreserveBusinessState({ turnKind: "query_answer", uiActions: ["query_answer"], debug: { queryOutcome: "clarification" } }), true);
  assert.equal(Q.shouldPreserveBusinessState({ state: "ANALYZING" }), false);
  assert.equal(Q.shouldPreserveBusinessState({ state: "NEEDS_CLARIFICATION" }), false);
});

test("只读问答不重画业务确认菜单，也不重画右侧预览", () => {
  assert.equal(Q.shouldRenderBusinessMenu({ turnKind: "query_answer", uiActions: ["query_answer"] }), false);
  assert.equal(Q.shouldRenderBusinessMenu({ state: "WAITING_USER_CONFIRM" }), true);
  assert.equal(Q.shouldRefreshPreview({ turnKind: "query_answer", uiActions: ["query_answer"] }), false);
  assert.equal(Q.shouldRefreshPreview({ state: "WAITING_USER_CONFIRM" }), true);
});

// ---------------------------------------------------------------- 提问 vs 操作

test("疑问句被识别为提问，不会被当成修改指令带区域提示", () => {
  assert.equal(Q.isQuestionLike("你有未完成的草稿吗？"), true);
  assert.equal(Q.isQuestionLike("你支持分钟趋势吗"), true);
  assert.equal(Q.isQuestionLike("能顺便说说这个吗？"), true);
  assert.equal(Q.isQuestionLike("这个草稿改过了吗"), true);
});

test("祈使句和含糊句不算提问", () => {
  assert.equal(Q.isQuestionLike("把趋势改成天粒度"), false);
  assert.equal(Q.isQuestionLike("继续弄一下"), false);
  assert.equal(Q.isQuestionLike("把第三个卡片换成小时粒度，数据源用能耗明细"), false);
  assert.equal(Q.isQuestionLike("生成 A 机房昨日能耗日报"), false);
  assert.equal(Q.isQuestionLike(""), false);
});

test("只有修改意图才带 selectedRegionId 区域提示", () => {
  assert.equal(Q.shouldHintTrendRegion("把趋势改成天粒度"), true);
  assert.equal(Q.shouldHintTrendRegion("把第三个卡片换成小时粒度"), false);
  assert.equal(Q.shouldHintTrendRegion("你支持分钟趋势吗？"), false);
  assert.equal(Q.shouldHintTrendRegion(""), false);
});

// ---------------------------------------------------------------- 菜单存废

const CONFIRM_QUESTION = {
  id: "confirm_change",
  prompt: "请选择：",
  expects: "SINGLE_CHOICE",
  selectionOnly: true,
  allowRestart: false,
  options: [
    { value: "同意", label: "同意" },
    { value: "修改", label: "修改" },
    { value: "重新开始", label: "重新开始" },
  ],
};

test("optionValues 兼容只有 value 或只有 label 的选项", () => {
  assert.deepEqual(Q.optionValues(CONFIRM_QUESTION), ["同意", "修改", "重新开始"]);
  assert.deepEqual(Q.optionValues({ options: [{ value: "A" }, { label: "B" }] }), ["A", "B"]);
  assert.deepEqual(Q.optionValues(null), []);
});

test("menuAnswersQuestion 只认菜单里的选项", () => {
  assert.equal(Q.menuAnswersQuestion(CONFIRM_QUESTION, "同意"), true);
  assert.equal(Q.menuAnswersQuestion(CONFIRM_QUESTION, "你有未完成的草稿吗？"), false);
  assert.equal(Q.menuAnswersQuestion(CONFIRM_QUESTION, ""), false);
  assert.equal(Q.menuAnswersQuestion(null, "同意"), false);

  const restartable = { ...CONFIRM_QUESTION, allowRestart: true };
  assert.equal(Q.menuAnswersQuestion(restartable, "重新开始"), true);
});

test("历史回放：只读提问不关掉原确认菜单", () => {
  const events = [
    { type: "user", content: "生成 A 机房昨日能耗日报" },
    { type: "assistant", response: { state: "DEFINITION_DRAFT_READY" }, menu: CONFIRM_QUESTION },
    { type: "user", content: "你有未完成的草稿吗？" },
    { type: "assistant", response: { turnKind: "query_answer", uiActions: ["query_answer"] }, menu: null },
  ];
  assert.equal(Q.resolveOpenMenuAfterReplay(events), CONFIRM_QUESTION);
});

test("历史回放：只读澄清同样不关掉原确认菜单", () => {
  const events = [
    { type: "assistant", response: { state: "DEFINITION_DRAFT_READY" }, menu: CONFIRM_QUESTION },
    { type: "user", content: "继续弄一下" },
    { type: "assistant", response: { turnKind: "query_answer", uiActions: ["query_answer"], debug: { queryOutcome: "clarification" } }, menu: null },
  ];
  assert.equal(Q.resolveOpenMenuAfterReplay(events), CONFIRM_QUESTION);
});

test("历史回放：菜单被选项答复后关闭，不恢复过时确认项", () => {
  const events = [
    { type: "assistant", response: { state: "DEFINITION_DRAFT_READY" }, menu: CONFIRM_QUESTION },
    { type: "user", content: "同意" },
    { type: "assistant", response: { state: "SAVE_DEFINITION_DRAFT" }, menu: null },
  ];
  assert.equal(Q.resolveOpenMenuAfterReplay(events), null);
});

test("历史回放：业务动作取代原菜单，新菜单生效", () => {
  const next = { ...CONFIRM_QUESTION, id: "confirm_change_2" };
  const events = [
    { type: "assistant", response: { state: "DEFINITION_DRAFT_READY" }, menu: CONFIRM_QUESTION },
    { type: "user", content: "修改" },
    { type: "assistant", response: { state: "DEFINITION_DRAFT_READY" }, menu: next },
  ];
  assert.equal(Q.resolveOpenMenuAfterReplay(events), next);
});

test("历史回放：没有菜单时返回空", () => {
  assert.equal(Q.resolveOpenMenuAfterReplay([]), null);
  assert.equal(Q.resolveOpenMenuAfterReplay([{ type: "user", content: "你好" }]), null);
});

// ---------------------------------------------------------------- 澄清选项
