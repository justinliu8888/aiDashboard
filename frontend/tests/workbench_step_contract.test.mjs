import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../pages/workbench.js", import.meta.url), "utf8");

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
          if (selector === "[data-plan-body]") { return { appendChild() {} }; }
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
      },
    },
    sendMessage: async () => ({ state: "USER_EDITING_TEMPLATE" }),
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
    "renderOtherAnswerHtml", "showOtherAnswerHint", "bindOtherAnswers", "appendConfirmActions", "appendReadyActions", "appendAiQuestions", "pendingEditMenuQuestion", "appendPendingEditPlan", "isTemplateEditFailure", "resolveEditStepQuestion", "restoreEditFailureMenu"];
  vm.runInContext(names.map(snippet).join("\n"), sandbox);
  return { sandbox, cards, controls, warnings };
}

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

const nextAction = {
  id: "template_edit_confirm", expects: "NEXT_ACTION", selectionOnly: true,
  lockScope: "input_and_menu", stepId: "step_menu_1", stepVersion: 1, stepKind: "menu",
  options: [{ value: "同意", label: "同意" }, { value: "修改", label: "修改" }, { value: "重新开始", label: "重新开始" }],
};

const editStep = {
  id: "template_edit_request", expects: "TEXT", selectionOnly: false, allowsOther: true,
  lockScope: "input_and_menu", stepId: "step_edit_1", stepVersion: 1, stepKind: "edit_request",
  allowedSubmissions: ["edit_text"], options: [],
};

test("请求带上当前这一步的关联：步骤 id 与版本", async () => {
  const { sandbox } = load();
  sandbox.appendConfirmActions(nextAction);
  const { bodies } = realSender(sandbox, [{ state: "USER_EDITING_TEMPLATE" }]);
  await sandbox.sendMessage("修改", { fromChoice: true });
  assert.equal(bodies.length, 1);
  assert.equal(bodies[0].context.stepId, "step_menu_1");
  assert.equal(bodies[0].context.stepVersion, 1);
});

test("内联两段请求：第二段带的是**新一步**的关联，不是旧菜单的", async () => {
  const { sandbox } = load();
  sandbox.appendConfirmActions(nextAction);
  const { bodies } = realSender(sandbox, [
    { state: "USER_EDITING_TEMPLATE", questions: [editStep] },
    { state: "USER_EDITING_TEMPLATE" },
  ]);
  await sandbox.sendMessage("修改", { fromChoice: true, inlineEditText: "页面紧凑一点" });
  assert.equal(bodies.length, 2);
  assert.equal(bodies[0].context.stepId, "step_menu_1");
  assert.equal(bodies[1].context.stepId, "step_edit_1");
  assert.equal(bodies[1].context.stepVersion, 1);
});

test("受限修改输入那一步：普通输入与发送按钮都锁着", () => {
  const { sandbox, controls } = load();
  sandbox.appendConfirmActions(nextAction);
  sandbox.setChoiceLock({ ...editStep, inlineEdit: true });
  sandbox.syncInputControls();
  assert.equal(controls["chat-input"].disabled, true);
  assert.equal(controls["btn-send"].disabled, true);
});
