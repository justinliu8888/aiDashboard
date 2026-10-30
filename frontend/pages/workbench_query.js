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

  // ---------------------------------------------------------------- 小工具

  function isPlainObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }

  /** UTF-8 字节数（与服务端按字节算体积的口径一致）。 */
  function byteLength(text) {
    if (typeof TextEncoder === "function") {
      return new TextEncoder().encode(String(text)).length;
    }
    // 没有 TextEncoder 时的兜底：按 UTF-8 手工算。
    let bytes = 0;
    for (const char of String(text)) {
      const code = char.codePointAt(0);
      if (code <= 0x7f) bytes += 1;
      else if (code <= 0x7ff) bytes += 2;
      else if (code <= 0xffff) bytes += 3;
      else bytes += 4;
    }
    return bytes;
  }

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

    // **turnKind 是唯一依据**（ADR 0002 §7.2）。
    // 不看 uiActions：那是服务端强制归一化出来的同义标记，
    // 拿它当第二判据，等于承认「有没有 turnKind 都能算只读」，
    // 前后端契约就出现了两个入口。
    if (response.turnKind !== QUERY_TURN_KIND) return null;

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

  // ---------------------------------------------------------------- JSON 粘贴入口
  //
  // 契约表 §4.3：JSON 输入是本轮**全新**的一类对象。左侧对话区要给用户一个
  // 明确的入口：把**记录数组**贴进来，并附一句自然语言说明想做什么。
  //
  // 这一段刻意与服务端的校验**同口径**（判定顺序、错误码、规模上限都一样）：
  //   - 前端先自查一遍，是为了**当场**给出「哪里不对、怎么改」，不是替服务端做校验；
  //   - 服务端仍然是唯一的边界（契约表 §8 不变量 6），前端放行不代表服务端收下。
  //
  // 错误码取自契约表 §4.3 的固定取值，**不合并、不新增**：
  //   JSON_INPUT_EMPTY / JSON_SYNTAX_INVALID / JSON_NOT_ARRAY / JSON_ARRAY_EMPTY /
  //   JSON_ARRAY_PATH_INVALID / JSON_ARRAY_PATH_AMBIGUOUS / JSON_NESTED_NOT_SUPPORTED /
  //   JSON_ROW_NOT_OBJECT / JSON_TOO_LARGE / JSON_TOO_MANY_ROWS / JSON_TOO_MANY_FIELDS

  /** 规模上限（与服务端一致）：1 MB / 5000 行 / 50 个字段。 */
  const JSON_LIMITS = {
    maxBytes: 1048576,
    maxRows: 5000,
    maxFields: 50,
  };

  /** 数据来源模式（契约表 §1.1）。 */
  const SOURCE_MODE_USER_JSON = "user_json";
  const SOURCE_MODE_SIMULATED = "simulated";

  /**
   * 用户 JSON 模式下的界面标注。
   *
   * 只说「用户提供的数据，未经业务核验」—— **不宣称已连接真实业务库**
   * （这份数据就是用户贴进来的，系统没有核过它）。
   */
  const USER_JSON_SOURCE_LABEL = "用户提供的数据，未经业务核验";

  /** 模拟模式下的界面标注。 */
  const SIMULATED_SOURCE_LABEL = "模拟示例";

  /**
   * 服务端错误码 → 可修正的提示。
   *
   * 服务端通常已经给了人话消息；这里的兜底只在消息缺失时用，
   * 保证界面永远给得出「怎么改」。
   */
  const JSON_ERROR_HINTS = {
    JSON_INPUT_EMPTY: "没有收到 JSON 数据，请把记录数组贴进来再试。",
    JSON_SYNTAX_INVALID: "JSON 语法有错：请检查引号、逗号与括号是否配对。",
    JSON_NOT_ARRAY: "提交的内容不是记录数组，请贴一个 JSON 数组（最外层是 [...]）。",
    JSON_ARRAY_EMPTY: "记录数组是空的，没有可以展示的数据。",
    JSON_ARRAY_PATH_INVALID:
      "报文里找不到数组位置：请确认路径写对了，或改成直接把记录数组放在最外层。",
    JSON_ARRAY_PATH_AMBIGUOUS:
      "报文里有多个数组，系统不会替你挑一个：请写明用哪一个，例如「数组位置: data.records」。",
    JSON_NESTED_NOT_SUPPORTED:
      "数据里有嵌套的对象或数组：请先把它们整理成平铺的字段再提交。",
    JSON_ROW_NOT_OBJECT: "有一行不是对象：每条记录都应是「字段名: 值」这样的一行。",
    JSON_TOO_LARGE: "数据体积超过 1 MB 上限，请裁剪数据后重新提交（系统不会自动截取）。",
    JSON_TOO_MANY_ROWS: "记录数超过 5000 行上限，请裁剪后重新提交（系统不会自动截取）。",
    JSON_TOO_MANY_FIELDS: "字段数超过 50 个上限，请裁剪字段后重新提交（系统不会自动截取）。",
  };

  /** 这个错误是不是 JSON 输入的失败码。 */
  function isJsonErrorCode(code) {
    const key = String(code == null ? "" : code).trim();
    return Object.prototype.hasOwnProperty.call(JSON_ERROR_HINTS, key);
  }

  /**
   * 一次失败该怎么告诉用户「哪里不对、怎么改」。
   *
   * 服务端给了人话消息就用它的；没有就按错误码取兜底。
   */
  function jsonErrorHint(error) {
    if (!error) return "";
    if (typeof error === "string") return error.trim();
    const code = String(error.code == null ? "" : error.code).trim();
    const message = typeof error.message === "string" ? error.message.trim() : "";
    if (message) return message;
    if (code && JSON_ERROR_HINTS[code]) return JSON_ERROR_HINTS[code];
    return "";
  }

  /**
   * 来源模式 → 界面标注。
   *
   * 未知模式返回空串：不渲染这个标注，也不回落成别的模式的说法。
   */
  function sourceModeLabel(sourceMode) {
    const key = String(sourceMode == null ? "" : sourceMode).trim();
    if (key === SOURCE_MODE_USER_JSON) return USER_JSON_SOURCE_LABEL;
    if (key === SOURCE_MODE_SIMULATED) return SIMULATED_SOURCE_LABEL;
    return "";
  }

  /** 是不是用户 JSON 模式。 */
  function isUserJsonMode(sourceMode) {
    return String(sourceMode == null ? "" : sourceMode).trim() === SOURCE_MODE_USER_JSON;
  }

  /**
   * 客户端自查一段粘贴的 JSON。
   *
   * 判定顺序与服务端一致：空 → 体积 → 语法 → 是不是数组 → 空数组 →
   * 行必须是对象 → 不能有嵌套 → 行数上限 → 字段数上限。
   *
   * @returns {{ok: boolean, recordCount: number, fieldCount: number,
   *            error: {code: string, message: string} | null}}
   */
  function parseJsonPaste(text) {
    const raw = typeof text === "string" ? text : "";
    const fail = (code, message) => ({
      ok: false,
      recordCount: 0,
      fieldCount: 0,
      error: { code, message: message || JSON_ERROR_HINTS[code] || "" },
    });

    if (!raw.trim()) return fail("JSON_INPUT_EMPTY");

    const byteSize = byteLength(raw);
    if (byteSize > JSON_LIMITS.maxBytes) {
      return fail(
        "JSON_TOO_LARGE",
        "数据体积 " + byteSize + " 字节，超过上限 " + JSON_LIMITS.maxBytes + " 字节（1 MB）。" +
          "请裁剪数据后重新提交，系统不会自动截取。"
      );
    }

    let payload;
    try {
      payload = JSON.parse(raw);
    } catch (err) {
      return fail("JSON_SYNTAX_INVALID", "JSON 语法有错，请检查引号、逗号与括号是否配对后重试。");
    }

    if (!Array.isArray(payload)) {
      return fail(
        "JSON_NOT_ARRAY",
        "提交的内容不是记录数组，请贴一个 JSON 数组（最外层是 [...]）。"
      );
    }
    if (!payload.length) return fail("JSON_ARRAY_EMPTY");

    if (payload.length > JSON_LIMITS.maxRows) {
      return fail(
        "JSON_TOO_MANY_ROWS",
        "记录数 " + payload.length + " 超过上限 " + JSON_LIMITS.maxRows + " 行。" +
          "请裁剪后重新提交，系统不会自动截取。"
      );
    }

    const fieldKeys = [];
    for (let index = 0; index < payload.length; index += 1) {
      const row = payload[index];
      if (!isPlainObject(row)) {
        return fail(
          "JSON_ROW_NOT_OBJECT",
          "第 " + (index + 1) + " 条记录不是对象。每条记录都应是「字段名: 值」这样的一行。"
        );
      }
      for (const key of Object.keys(row)) {
        const value = row[key];
        if (isPlainObject(value) || Array.isArray(value)) {
          return fail(
            "JSON_NESTED_NOT_SUPPORTED",
            "数据里有嵌套的对象或数组（" + key + "）。请先把它们整理成平铺的字段再提交，" +
              "系统不会自动展开成多张关联表。"
          );
        }
        if (!fieldKeys.includes(key)) fieldKeys.push(key);
      }
    }

    if (fieldKeys.length > JSON_LIMITS.maxFields) {
      return fail(
        "JSON_TOO_MANY_FIELDS",
        "字段数 " + fieldKeys.length + " 超过上限 " + JSON_LIMITS.maxFields + " 个。" +
          "请裁剪字段后重新提交，系统不会自动截取。"
      );
    }

    return {
      ok: true,
      recordCount: payload.length,
      fieldCount: fieldKeys.length,
      error: null,
    };
  }

  /**
   * 把「记录数组 + 一句自然语言」拼成一条消息。
   *
   * 服务端从消息里认得出 JSON 段（`routing_rules.detect_json_payload`），
   * 人话部分它会剥掉、只留摘要入库 —— 所以这里把两者都带上就行。
   *
   * 数组位置写在人话部分里也认（「数组位置: data.records」/「@arrayPath data.records」）。
   */
  function buildJsonPasteMessage(note, jsonText) {
    const text = typeof note === "string" ? note.trim() : "";
    const data = typeof jsonText === "string" ? jsonText.trim() : "";
    if (!text) return data;
    if (!data) return text;
    return text + "\n" + data;
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
    shouldRefreshPreview,
    isQuestionLike,
    shouldHintTrendRegion,
    optionValues,
    menuAnswersQuestion,
    resolveOpenMenuAfterReplay,
    // ---- JSON 粘贴入口（#66）----
    JSON_LIMITS,
    JSON_ERROR_HINTS,
    SOURCE_MODE_USER_JSON,
    SOURCE_MODE_SIMULATED,
    USER_JSON_SOURCE_LABEL,
    SIMULATED_SOURCE_LABEL,
    isJsonErrorCode,
    jsonErrorHint,
    sourceModeLabel,
    isUserJsonMode,
    parseJsonPaste,
    buildJsonPasteMessage,
  };
});
