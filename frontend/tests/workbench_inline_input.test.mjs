import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../pages/workbench.js", import.meta.url), "utf8");
const html = readFileSync(new URL("../pages/workbench.html", import.meta.url), "utf8");

function snippet(name) {
  const start = source.search(new RegExp(`^(?:async )?function ${name}\\(`, "m"));
  assert.ok(start >= 0, `Missing function: ${name}`);
  return source.slice(start, source.indexOf("\n}", start) + 2);
}

function element(dataset = {}) {
  const classes = new Set();
  const listeners = new Map();
  return {
    dataset, value: "", disabled: false, textContent: "",
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
    },
    focus() { this.focused = true; },
    addEventListener(name, callback) { listeners.set(name, callback); },
    fire(name, event = {}) { return listeners.get(name)?.({ target: this, ...event }); },
  };
}

function load() {
  const cards = [];
  const controls = Object.fromEntries(["chat-input", "btn-send", "btn-stop"].map((id) => [id, element()]));
  const calls = [];
  const warnings = [];
  /** @type {Record<string, any>} */
  const sandbox = {
    lockedChoice: null, activeRequest: null, advanceTimers: new Set(), requestEpoch: 1,
    session: { sessionId: "session", state: "WAITING_USER_CONFIRM" },
    planState: {}, DEFAULT_CONTEXT: {}, businessMenuReplayed: false,
    OTHER_ANSWER_VALUE: "__question_other__",
    INPUT_LOCK_HINT: "请先选择下一步操作",
    LOCK_SCOPE_NONE: "none", LOCK_SCOPE_MENU_ONLY: "menu_only", LOCK_SCOPE_INPUT_AND_MENU: "input_and_menu",
    EDIT_PLAN_CONTRACT: { requestVisibleRegionIdsField: "visibleRegionIds", choice: { accept: "接受修改", adjust: "继续调整", cancel: "取消" } },
    esc: (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;"),
    $: (id) => controls[id],
    hasPendingPlan: () => false,
    syncInputLockHint() {}, syncPreviewEditability() {},
    aiBubble(markup) {
      const selects = [...markup.matchAll(/<select[^>]*data-question-id="([^"]*)"[^>]*>/g)].map((match) => element({ questionId: match[1] }));
      const boxes = [...markup.matchAll(/<div class="question-other hidden[^>]*data-question-other="([^"]*)"/g)].map((match) => {
        const box = element({ questionOther: match[1] });
        box.classList.add("hidden");
        const children = Object.fromEntries(["input", "submit", "hint", "label"].map((name) => [`.question-other-${name}`, element()]));
        children[".question-other-input"].disabled = true;
        children[".question-other-submit"].disabled = true;
        box.querySelector = (selector) => children[selector];
        return box;
      });
      const card = {
        markup, selects, boxes,
        querySelectorAll(selector) {
          if (selector === ".question-select") return selects;
          if (selector === ".question-other") return boxes;
          return [];
        },
        querySelector(selector) {
          if (selector === "[data-plan-body]") return { appendChild() {} };
          return this.querySelectorAll(selector)[0];
        },
      };
      cards.push(card);
      return card;
    },
    document: {
      createElement() {
        let content;
        return {
          set innerHTML(markup) { content = sandbox.aiBubble(markup); },
          querySelectorAll: (selector) => content.querySelectorAll(selector),
          querySelector: (selector) => content.querySelector(selector),
        };
      },
      querySelectorAll(selector) {
      if (selector === ".question-select") return cards.flatMap((card) => card.selects);
      if (selector === "[data-question-other]") return cards.flatMap((card) => card.boxes);
      return [];
    } },
    sendMessage: async (text, options) => { calls.push({ text, options }); return { state: "USER_EDITING_TEMPLATE" }; },
    toast: (text) => warnings.push(text),
    appendAiText() {}, appendUser() {}, newMessageId: () => "message",
    visibleBodyRegionIds: () => [],
    visibleRegionLabel: () => "", isPendingPlanStale: () => false,
    readReportSourceMode: () => "",
    appendAiThinking: () => ({}), appendDebugTrace: () => ({}),
    AbortController, finishDebugTrace() {}, removeBubble() {},
    syncPlanFromResponse() {}, readPendingEditPlan: () => null,
    readReportPreviewPayload: () => null, updateMeta() {}, renderSessionList() {},
    renderReportPanel() {}, appendTemplateEditSummary() {},
    setState(state) { sandbox.session.state = state; },
    WorkbenchQuery: {
      shouldHintTrendRegion: () => false,
      readOnlyAnswerKind: () => null,
      shouldPreserveBusinessState: () => false,
    },
    showJsonPasteErrorHint() {}, appendAgentError() {}, appendPlanFailureNotice() {},
  };
  vm.createContext(sandbox);
  const names = ["lockScopeOf", "isBusy", "isInputLocked", "isAuthoritativeChoice", "menuLocksInput", "inputControlState", "syncInputControls", "setChoiceLock", "clearChoiceLock",
    "otherAnswerLabel", "otherAnswerPlaceholder", "inlineAnswerAction", "renderQuestionOptions", "bindQuestionSelects",
    "renderOtherAnswerHtml", "showOtherAnswerHint", "bindOtherAnswers", "appendConfirmActions", "appendReadyActions", "appendAiQuestions", "pendingEditMenuQuestion", "appendPendingEditPlan", "isTemplateEditFailure", "restoreEditFailureMenu"];
  vm.runInContext(names.map(snippet).join("\n"), sandbox);
  return { sandbox, cards, controls, calls, warnings };
}

const draft = {
  id: "template_edit_confirm", expects: "NEXT_ACTION", selectionOnly: true,
  options: [{ value: "同意", label: "同意" }, { value: "修改", label: "修改" }, { value: "重新开始", label: "重新开始" }],
};

test("NEXT_ACTION 修改只展开原卡输入，空输入不提交，普通聊天保持全锁", async () => {
  const { sandbox, cards, controls, calls } = load();
  sandbox.appendConfirmActions(draft);
  const card = cards[0];
  const box = card.boxes[0];
  assert.ok(box.classList.contains("hidden"));
  card.selects[0].value = "修改";
  card.selects[0].fire("change");
  assert.equal(cards.length, 1);
  assert.equal(calls.length, 0);
  assert.equal(box.classList.contains("hidden"), false);
  assert.equal(box.querySelector(".question-other-input").disabled, false);
  assert.equal(controls["chat-input"].disabled, true);
  assert.equal(controls["btn-send"].disabled, true);
  await box.querySelector(".question-other-submit").fire("click");
  assert.equal(calls.length, 0);
  box.querySelector(".question-other-input").value = "增加温度";
  await box.querySelector(".question-other-submit").fire("click");
  assert.equal(calls[0].text, "修改");
  assert.equal(calls[0].options.inlineEditText, "增加温度");
});

test("所有 allowsOther 题型都提供下拉其他项，选择后才显示输入，Enter 只提交卡片文本", async () => {
  for (const selectionOnly of [true, false]) {
    const { sandbox, cards, calls } = load();
    sandbox.appendAiQuestions([{ id: "clarify", selectionOnly, allowsOther: true, otherLabel: "其他想法", options: [{ value: "A", label: "A" }] }]);
    const card = cards[0];
    assert.match(card.markup, /<option value="__question_other__">其他想法<\/option>/);
    assert.ok(card.boxes[0].classList.contains("hidden"));
    card.selects[0].value = "__question_other__";
    card.selects[0].fire("change");
    assert.equal(calls.length, 0);
    const input = card.boxes[0].querySelector(".question-other-input");
    input.value = "改为温度";
    let prevented = false;
    let stopped = false;
    await input.fire("keydown", { key: "Enter", preventDefault() { prevented = true; }, stopPropagation() { stopped = true; } });
    assert.equal(prevented && stopped, true);
    assert.equal(calls[0].text, "改为温度");
    assert.equal(calls[0].options.fromOther, true);
  }
});

test("同 id 历史卡不可展开或提交，当前卡可切回普通选项并隐藏输入", () => {
  const { sandbox, cards, calls } = load();
  sandbox.appendConfirmActions({ ...draft, allowsOther: true });
  const old = cards[0];
  old.selects[0].value = "修改";
  old.selects[0].fire("change");
  sandbox.appendConfirmActions({ ...draft, allowsOther: true });
  assert.equal(old.boxes[0].querySelector(".question-other-input").disabled, true);
  old.selects[0].fire("change");
  assert.equal(calls.length, 0);
  const current = cards[1];
  current.selects[0].value = "修改";
  current.selects[0].fire("change");
  current.selects[0].value = "";
  current.selects[0].fire("change");
  assert.ok(current.boxes[0].classList.contains("hidden"));
  current.selects[0].value = "同意";
  current.selects[0].fire("change");
  assert.equal(calls[0].text, "同意");
});

test("待确认编辑继续修改内联，READY 继续修改也不提前发动作", () => {
  const { sandbox, cards, calls } = load();
  const menu = sandbox.pendingEditMenuQuestion({ id: "pending_edit_plan_confirm", allowsOther: true }, [
    { value: "接受修改", label: "接受修改" }, { value: "取消", label: "撤回上一步" }, { value: "继续调整", label: "继续修改" },
  ]);
  sandbox.appendConfirmActions(menu);
  cards[0].selects[0].value = "继续调整";
  cards[0].selects[0].fire("change");
  assert.equal(calls.length, 0);
  assert.equal(cards[0].boxes[0].dataset.answerMode, "edit");
  cards[0].selects[0].value = "取消";
  cards[0].selects[0].fire("change");
  assert.equal(calls[0].text, "取消");
  sandbox.appendReadyActions({ id: "ready_action", selectionOnly: true, options: [{ value: "继续修改", label: "继续修改" }] });
  cards[1].selects[0].value = "继续修改";
  cards[1].selects[0].fire("change");
  assert.equal(calls.length, 1);
});

test("实际待确认计划保留占位选项，显示新动作文案并沿用后端动作值", () => {
  const { sandbox, cards, calls } = load();
  const menu = sandbox.appendPendingEditPlan({ planId: "plan", operations: [], draftRevision: 1 }, { question: {
    id: "pending_edit_plan_confirm", allowsOther: true,
    options: [
      { value: "先占位，只显示名称", label: "先占位，只显示名称" },
      { value: "接受修改", label: "接受修改" },
      { value: "继续调整", label: "继续调整" },
      { value: "取消", label: "取消" },
    ],
  } });
  const card = cards.at(-1);
  assert.match(card.markup, /<option value="继续调整">继续修改<\/option>/);
  assert.match(card.markup, /<option value="取消">撤回上一步<\/option>/);
  assert.match(card.markup, /<option value="接受修改">接受修改<\/option>/);
  assert.match(card.markup, /先占位，只显示名称/);
  assert.equal(menu.options.length, 4);
  card.selects[0].value = "继续调整";
  card.selects[0].fire("change");
  assert.equal(calls.length, 0);
  assert.equal(card.boxes[0].classList.contains("hidden"), false);
  card.selects[0].value = "接受修改";
  card.selects[0].fire("change");
  assert.equal(calls[0].text, "接受修改");
});

test("请求忙时禁用原卡输入，点击和 Enter 均不提交", async () => {
  const { sandbox, cards, calls } = load();
  sandbox.appendConfirmActions(draft);
  const card = cards[0];
  card.selects[0].value = "修改";
  card.selects[0].fire("change");
  const input = card.boxes[0].querySelector(".question-other-input");
  input.value = "增加温度";
  sandbox.activeRequest = {};
  sandbox.syncInputControls();
  assert.equal(input.disabled, true);
  await card.boxes[0].querySelector(".question-other-submit").fire("click");
  await input.fire("keydown", { key: "Enter", preventDefault() {}, stopPropagation() {} });
  assert.equal(calls.length, 0);
});

function realSender(sandbox, responses) {
  const messages = [];
  const bodies = [];
  sandbox.postAgentStream = async (body) => {
    messages.push(body.message);
    bodies.push(body);
    const response = responses.shift();
    if (response instanceof Error) throw response;
    return response;
  };
  vm.runInContext(snippet("sendMessage"), sandbox);
  return { messages, bodies };
}

test("编辑校验失败恢复新问题，保留草稿与载荷，旧接受菜单失效", async () => {
  const { sandbox, cards } = load();
  sandbox.appendConfirmActions({ ...draft, id: "pending_edit_plan_confirm", options: [{ value: "接受修改", label: "接受修改" }] });
  const old = cards[0];
  const definition = { metadata: { revision: 1 } };
  const payload = { regions: [{ value: 25 }] };
  sandbox.session.definition = definition;
  sandbox.session.reportPreviewPayload = payload;
  const question = { id: "template_edit_action", allowsOther: true, options: [{ value: "改指标", label: "改指标" }] };
  const response = { state: "USER_EDITING_TEMPLATE", error: { code: "EDIT_VALIDATION_FAILED" }, questions: [question] };
  const { messages } = realSender(sandbox, [response]);
  assert.equal(await sandbox.sendMessage("接受修改", { fromChoice: true }), response);
  assert.deepEqual(messages, ["接受修改"]);
  assert.equal(old.selects[0].disabled, true);
  assert.equal(cards.at(-1).selects[0].disabled, false);
  assert.equal(sandbox.lockedChoice.id, question.id);
  assert.equal(sandbox.session.definition, definition);
  assert.equal(sandbox.session.reportPreviewPayload, payload);
});

test("历史编辑失败返回恢复菜单；无新问题时释放旧计划锁", () => {
  const { sandbox } = load();
  vm.runInContext(snippet("replayAssistantMessage"), sandbox);
  const question = { id: "template_edit_action", allowsOther: true, options: [{ value: "改标题", label: "改标题" }] };
  const response = { state: "USER_EDITING_TEMPLATE", error: { code: "EDIT_VALIDATION_FAILED" }, questions: [question] };
  assert.equal(sandbox.replayAssistantMessage(response), question);
  sandbox.appendConfirmActions(draft);
  sandbox.restoreEditFailureMenu({ ...response, questions: [] });
  assert.equal(sandbox.lockedChoice, null);
  assert.equal(sandbox.lastPendingPlan, null);
});

test("内联提交兼容既有协议：提交时才先动作后内容，中间不画追问卡", async () => {
  const { sandbox, cards } = load();
  sandbox.appendConfirmActions(draft);
  const { messages } = realSender(sandbox, [{ state: "USER_EDITING_TEMPLATE" }, { state: "USER_EDITING_TEMPLATE" }]);
  const result = await sandbox.sendMessage("修改", { fromChoice: true, inlineEditText: "增加温度" });
  assert.deepEqual(messages, ["修改", "增加温度"]);
  assert.equal(result.state, "USER_EDITING_TEMPLATE");
  assert.equal(cards.length, 1);
});

test("第二段请求失败保留卡内文字和聊天锁，重试不再发送修改动作", async () => {
  const { sandbox, cards, controls } = load();
  sandbox.appendConfirmActions(draft);
  const { messages } = realSender(sandbox, [{ state: "USER_EDITING_TEMPLATE" }, new Error("network"), { state: "USER_EDITING_TEMPLATE" }]);
  const card = cards[0];
  card.selects[0].value = "修改";
  card.selects[0].fire("change");
  const input = card.boxes[0].querySelector(".question-other-input");
  input.value = "增加温度";
  await card.boxes[0].querySelector(".question-other-submit").fire("click");
  assert.equal(input.value, "增加温度");
  assert.equal(controls["chat-input"].disabled, true);
  await card.boxes[0].querySelector(".question-other-submit").fire("click");
  assert.deepEqual(messages, ["修改", "增加温度", "增加温度"]);
  assert.equal(input.value, "");
});

test("动作失败不继续发送内容；普通聊天与伪造内联意图不能绕过菜单锁", async () => {
  const { sandbox, controls } = load();
  sandbox.appendConfirmActions(draft);
  const { messages } = realSender(sandbox, [{ state: "USER_EDITING_TEMPLATE", error: { code: "CONFLICT" } }]);
  await sandbox.sendMessage("修改", { fromChoice: true, inlineEditText: "增加温度" });
  await sandbox.sendMessage("普通聊天");
  await sandbox.sendMessage("增加温度", { fromChoice: true, fromInline: true });
  assert.deepEqual(messages, ["修改"]);
  assert.equal(controls["chat-input"].disabled, true);
});

test("名称占位沿用数值卡外形及数值区高度，但显式占位不展示数值或单位", () => {
  const { sandbox } = load();
  sandbox.readPayloadUnit = () => "kWh";
  sandbox.formatMetricValue = (value) => String(value);
  vm.runInContext(snippet("renderRegionPayloadBody"), sandbox);
  const render = (payload) => {
    const container = {};
    sandbox.renderRegionPayloadBody(container, { componentType: "METRIC_CARD", payload });
    return container.innerHTML;
  };
  const real = render({ metrics: [{ label: "温度", value: 24, unit: "C" }] });
  const placeholder = render({ metrics: [{ label: "温度", placeholderOnly: true, value: 12345, unit: "C" }] });
  assert.match(real, /rounded-lg border border-slate-200 bg-white px-3 py-2/);
  assert.match(placeholder, /rounded-lg border border-slate-200 bg-white px-3 py-2/);
  assert.match(placeholder, /min-h-6/);
  assert.match(placeholder, /温度/);
  assert.doesNotMatch(placeholder, /12345|>C<|border-dashed|NaN|undefined/);
  assert.doesNotMatch(render({ placeholderOnly: true, metrics: [{ label: "温度", value: 12345 }] }), /12345/);
  assert.match(render({ label: "温度", value: 0 }), />0</);
});

test("HTML 使用新缓存版本", () => {
  // 断言的是「带缓存号」这件事，不是某个具体版本号：
  // workbench.js 与页面一起改动，版本号由改动它的人一起 bump；
  // 断言具体版本号会在别人 bump 别的资源时误报。
  assert.match(html, /workbench\.js\?v=2026\d{4}-\w+/);
});

// --------------------------------------------------------------------------
// 临时卡片目标（20261102 工单「选中卡片以后增加不了」）
//
// 目标只属于**当前这一次操作**：新的普通一轮不能带上一轮的 selectedRegionId，
// 显式卡片动作仍然要带；响应里的目标是三态（缺字段＝不变、null＝释放、非空＝设置）。
// --------------------------------------------------------------------------

/** 带一张「已高亮」的卡片，用来断言释放时高亮被清掉。 */
function loadWithHighlight() {
  const loaded = load();
  const highlighted = element();
  highlighted.classList.add("region-highlight");
  loaded.sandbox.previewRoot = {
    querySelectorAll: (selector) => (selector === ".region-highlight" ? [highlighted] : []),
  };
  vm.runInContext(snippet("clearRegionHighlight"), loaded.sandbox);
  return { ...loaded, highlighted };
}

test("新的普通一轮不带上一轮留下的 selectedRegionId", async () => {
  const { sandbox } = load();
  sandbox.session.state = "USER_EDITING_TEMPLATE";
  sandbox.session.editingTargetRegionId = "region_total_energy";
  const { bodies } = realSender(sandbox, [{ state: "USER_EDITING_TEMPLATE" }]);
  await sandbox.sendMessage("增加 abc");
  assert.equal(bodies.length, 1);
  assert.equal(bodies[0].context.selectedRegionId, undefined);
});

test("显式卡片动作仍然带上这张卡的 selectedRegionId", async () => {
  const { sandbox } = load();
  const { bodies } = realSender(sandbox, [{ state: "USER_EDITING_TEMPLATE" }]);
  vm.runInContext(snippet("sendTemplateEditAction"), sandbox);
  await sandbox.sendTemplateEditAction("删除这张卡片", "region_total_energy");
  assert.equal(bodies.length, 1);
  assert.equal(bodies[0].context.selectedRegionId, "region_total_energy");
});

test("内联两段请求都不带上一轮留下的 selectedRegionId", async () => {
  const { sandbox } = load();
  sandbox.appendConfirmActions(draft);
  sandbox.session.state = "USER_EDITING_TEMPLATE";
  sandbox.session.editingTargetRegionId = "region_total_energy";
  const { bodies } = realSender(sandbox, [{ state: "USER_EDITING_TEMPLATE" }, { state: "USER_EDITING_TEMPLATE" }]);
  await sandbox.sendMessage("修改", { fromChoice: true, inlineEditText: "增加温度" });
  assert.equal(bodies.length, 2);
  assert.equal(bodies[0].context.selectedRegionId, undefined);
  assert.equal(bodies[1].context.selectedRegionId, undefined);
});

test("回答后端菜单（接受待确认计划）延续当前操作，带上当前目标", async () => {
  const { sandbox } = load();
  sandbox.session.state = "USER_EDITING_TEMPLATE";
  sandbox.session.editingTargetRegionId = "region_total_energy";
  const { bodies } = realSender(sandbox, [{ state: "USER_EDITING_TEMPLATE" }]);
  await sandbox.sendMessage("接受修改", { fromChoice: true });
  assert.equal(bodies[0].context.selectedRegionId, "region_total_energy");
});

test("响应三态：缺字段保持原值、显式 null 释放并清高亮、非空设置目标", async () => {
  const { sandbox, highlighted } = loadWithHighlight();
  sandbox.session.state = "USER_EDITING_TEMPLATE";
  const { bodies } = realSender(sandbox, [
    { state: "USER_EDITING_TEMPLATE", debug: { editingTargetRegionId: "region_total_energy" } },
    { state: "USER_EDITING_TEMPLATE", debug: {} },
    { state: "USER_EDITING_TEMPLATE", debug: { editingTargetRegionId: null } },
  ]);
  await sandbox.sendMessage("第一轮");
  assert.equal(sandbox.session.editingTargetRegionId, "region_total_energy");
  await sandbox.sendMessage("第二轮");
  assert.equal(sandbox.session.editingTargetRegionId, "region_total_energy");
  assert.equal(highlighted.classList.contains("region-highlight"), true);
  await sandbox.sendMessage("第三轮");
  assert.equal(sandbox.session.editingTargetRegionId, null);
  assert.equal(highlighted.classList.contains("region-highlight"), false);
  assert.equal(bodies[1].context.selectedRegionId, undefined);
});
