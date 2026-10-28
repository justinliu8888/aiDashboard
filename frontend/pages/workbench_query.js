// 工作台「只读问答」的前端决策逻辑。
//
// 这个文件刻意做成**无 DOM 依赖的纯函数模块**：
//   - 浏览器：window.WorkbenchQuery（workbench.html 里先加载本文件再加载 workbench.js）
//   - Node 测试：require("../pages/workbench_query.js")，用 node --test 跑
// 好处是「只读问答会不会推进业务」这类红线可以在没有浏览器的情况下被测到，
// 不用低价值测试复述渲染代码。
//
// ── 契约说明（前端侧）────────────────────────────────────────────
// 「本轮是只读问答」由后端在响应里标记，识别口径集中在 readOnlyAnswerKind()，
// 是前端与后端之间**唯一**的耦合点。
//
// 主契约（后端共用契约 ADR 0002 / Agent A 实现）：
//   response.turnKind === "query_answer"      → 本轮不推进业务
//   response.uiActions === ["query_answer"]   → 同义标记（服务端强制归一化）
//   response.debug.queryOutcome               → "answer" | "clarification"
//   response.debug.needsClarification          → true 表示这是只读澄清
//
// 后端契约保证：查询轮的 state 保持不变、questions 是原待确认菜单、
// toolRequests 为空、uiActions 只有 query_answer。
//
// 语义：
//   ANSWER        只读事实回答。原样展示，不画业务菜单、不推进、不改业务状态。
//   CLARIFICATION 只读意图澄清。只展示澄清，不占用业务菜单的选择锁，
//                 原待确认操作保持可用。
//
// 只读问答**不改变**业务状态、待确认选项、待确认修改计划和右侧预览，
// 也不会触发 __advance__ 自动推进。
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.WorkbenchQuery = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const READ_ONLY_ANSWER = "ANSWER";
  const READ_ONLY_CLARIFICATION = "CLARIFICATION";

  // 后端标记只读查询轮的 turnKind / uiActions 取值
  const QUERY_TURN_KIND = "query_answer";

  // 这些状态轮到前端自动发 __advance__ 推进下一步（保存→校验→预览→渲染）。
  const AUTO_ADVANCE_STATES = new Set([
    "SAVE_DEFINITION_DRAFT",
    "VALIDATING",
    "PREVIEWING",
    "PREVIEW_READY",
    "RENDERING",
  ]);

  // 疑问句特征。用于把「问一句」和「要求改一下」区分开：
  // 只有明确的修改说法才带 selectedRegionId 区域提示，
  // 否则「你支持分钟趋势吗？」会被误当成要改趋势图。
  const QUESTION_SUFFIX = /[?？]\s*$/;
  const QUESTION_WORDS = /(吗|呢|能否|能不能|可不可以|是否|有没有)/;

  // ---------------------------------------------------------------- 只读语义

  /** 这一轮是不是只读澄清（而不是已经答完的只读回答）。 */
  function isQueryClarification(response) {
    const debug = (response && response.debug) || {};
    if (debug.queryOutcome === "clarification") return true;
    if (debug.needsClarification === true) return true;
    return false;
  }

  /**
   * 识别本轮响应是不是只读问答。
   * @returns {"ANSWER"|"CLARIFICATION"|null}
   */
  function readOnlyAnswerKind(response) {
    if (!response || typeof response !== "object") return null;

    const isQueryTurn =
      response.turnKind === QUERY_TURN_KIND ||
      (Array.isArray(response.uiActions) && response.uiActions.includes(QUERY_TURN_KIND));
    if (!isQueryTurn) return null;

    return isQueryClarification(response) ? READ_ONLY_CLARIFICATION : READ_ONLY_ANSWER;
  }

  function isReadOnlyResponse(response) {
    return readOnlyAnswerKind(response) !== null;
  }

  function isReadOnlyClarification(response) {
    return readOnlyAnswerKind(response) === READ_ONLY_CLARIFICATION;
  }

  // ---------------------------------------------------------------- 不推进业务

  /** 只读问答不得触发自动推进 __advance__。 */
  function shouldAutoAdvance(response) {
    if (isReadOnlyResponse(response)) return false;
    return AUTO_ADVANCE_STATES.has(response && response.state);
  }

  /** 只读问答不覆盖原业务状态（会话业务状态必须保持不变）。 */
  function shouldPreserveBusinessState(response) {
    return isReadOnlyResponse(response);
  }

  /** 只读问答不重画业务确认菜单（原 pendingChoice / 修改计划保持不变）。 */
  function shouldRenderBusinessMenu(response) {
    return !isReadOnlyResponse(response);
  }

  /** 只读问答不重画右侧预览（预览结果保持不变）。 */
  function shouldRefreshPreview(response) {
    return !isReadOnlyResponse(response);
  }

  // ---------------------------------------------------------------- 提问 vs 操作

  /** 这句话是不是在提问（而不是在要求改东西）。 */
  function isQuestionLike(text) {
    const value = String(text == null ? "" : text).trim();
    if (!value) return false;
    return QUESTION_SUFFIX.test(value) || QUESTION_WORDS.test(value);
  }

  /**
   * 是否给后端带上 selectedRegionId 的区域提示。
   * 只在明确提到「趋势」且不是提问时给：提问不该被当成要改趋势图。
   */
  function shouldHintTrendRegion(text) {
    const value = String(text == null ? "" : text).trim();
    if (!value) return false;
    if (!value.includes("趋势")) return false;
    return !isQuestionLike(value);
  }

  // ---------------------------------------------------------------- 菜单存废

  /** 菜单里所有可提交的值。 */
  function optionValues(question) {
    const options = question && Array.isArray(question.options) ? question.options : [];
    return options
      .filter((option) => option && (option.value !== undefined || option.label !== undefined))
      .map((option) => {
        const value = option.value !== undefined && option.value !== null ? option.value : option.label;
        return String(value);
      });
  }

  /** 这条用户消息是不是在选菜单里的某一项。 */
  function menuAnswersQuestion(question, content) {
    if (!question) return false;
    const value = String(content == null ? "" : content).trim();
    if (!value) return false;
    if (optionValues(question).includes(value)) return true;
    if (question.allowRestart && value === "重新开始") return true;
    return false;
  }

  /**
   * 回放历史消息后，判断原来那份业务确认菜单是否仍然「开着」。
   *
   * 规则：
   *   - 渲染出新菜单            → 新菜单取代旧菜单
   *   - 只读问答 / 只读澄清     → 原菜单保持不动（提问不是决定）
   *   - 其它业务响应（没有菜单）→ 原菜单被取代，关闭
   *
   * events: [{type:"user", content}, {type:"assistant", response, menu}]
   */
  function resolveOpenMenuAfterReplay(events) {
    let open = null;
    for (const event of Array.isArray(events) ? events : []) {
      if (!event || event.type !== "assistant") continue;
      if (event.menu) {
        open = event.menu;
        continue;
      }
      if (!open) continue;
      if (isReadOnlyResponse(event.response)) continue; // 只读问答不动原菜单
      open = null; // 业务响应取代了原菜单
    }
    return open;
  }

  return {
    READ_ONLY_ANSWER,
    READ_ONLY_CLARIFICATION,
    QUERY_TURN_KIND,
    AUTO_ADVANCE_STATES,
    isQueryClarification,
    readOnlyAnswerKind,
    isReadOnlyResponse,
    isReadOnlyClarification,
    shouldAutoAdvance,
    shouldPreserveBusinessState,
    shouldRenderBusinessMenu,
    shouldRefreshPreview,
    isQuestionLike,
    shouldHintTrendRegion,
    optionValues,
    menuAnswersQuestion,
    resolveOpenMenuAfterReplay,
  };
});
