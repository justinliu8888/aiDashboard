// 前端「先确认，再生成」的**独立文字方案显示模块**。
//
// 这个文件刻意做成不依赖主工作台的独立模块：
//   - 浏览器：window.WorkbenchPlan（由协调者接入 workbench.html）
//   - Node 测试：require("../pages/workbench_plan.js")，用 node --test 跑
//
// 它只负责「把一份文字方案安全地显示出来，并把用户的确认意图交回给上层」。
// 它**不**负责取数、发请求、推进状态机，也不碰主工作台已有的菜单与状态。
//
// ── 契约说明（前端侧）────────────────────────────────────────────
// 视图对象 view：
//   designPhase   "PLAN_NEEDED" | "PLAN_WAITING_CONFIRM"
//   reportPlan    方案对象（结构见冻结契约 v1 的 ReportPlan）
//   busy          布尔。处理中
//   error         安全错误文案（字符串，或带 message 的对象）
//   canConfirm    布尔。**缺省 false**
//
// 确认动作只回调一个设计意图，不发 HTTP、不运行 __advance__、不改旧菜单：
//   {"designAction":"CONFIRM_PLAN","planId":...,"basePlanRevision":...}
//
// basePlanRevision 是用户点确认时**正在显示**的那一版方案的 revision。
// 方案被更新后重新渲染，回调自然带上新版本号（上层据此做版本冲突检查）。
//
// 展示全部走 textContent（安全文本 DOM）：模型产出的任何 HTML 都不会被执行。
// 本文件不使用 innerHTML、不使用 insertAdjacentHTML、不执行 eval。
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.WorkbenchPlan = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // ---------------------------------------------------------------- 常量

  /** 确认动作名（与冻结契约 v1 一致）。 */
  const DESIGN_ACTION_CONFIRM = "CONFIRM_PLAN";

  /** 设计阶段。首波只有这两个值。 */
  const PHASE_PLAN_NEEDED = "PLAN_NEEDED";
  const PHASE_PLAN_WAITING_CONFIRM = "PLAN_WAITING_CONFIRM";

  /** 图表类型 → 中文标签。未知类型原样显示（安全文本）。 */
  const COMPONENT_TYPE_LABELS = {
    metric_card: "指标卡",
    line_chart: "折线图",
    bar_chart: "柱状图",
    pie_chart: "饼图",
    table: "表格",
    text: "文字块",
  };

  /** 设计阶段 → 中文标签。 */
  const PHASE_LABELS = {
    PLAN_NEEDED: "需要生成方案",
    PLAN_WAITING_CONFIRM: "待确认方案",
  };

  /** 容器上挂的标记属性，便于上层和测试定位自己的内容。 */
  const MODULE_ATTR = "data-plan-module";
  const MODULE_NAME = "report-plan";

  // ---------------------------------------------------------------- 样式
  // 用内联样式而不是外部 CSS：本模块不引入样式表、不改宿主页面，
  // 协调者接入时可以直接覆盖这些类名。
  const STYLE = {
    module: "font-family:inherit;color:#1e293b;",
    status: "display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;color:#64748b;margin-bottom:10px;",
    badge: "display:inline-flex;align-items:center;padding:2px 8px;border-radius:999px;background:#e2e8f0;color:#475569;font-size:11px;",
    badgeBusy: "display:inline-flex;align-items:center;padding:2px 8px;border-radius:999px;background:#fef3c7;color:#b45309;font-size:11px;",
    error: "margin-bottom:12px;padding:10px 12px;border-radius:8px;border:1px solid #fecaca;background:#fef2f2;color:#b91c1c;font-size:13px;",
    topic: "margin:0 0 4px;font-size:16px;font-weight:600;color:#0f172a;",
    meta: "font-size:12px;color:#64748b;margin:0 0 12px;",
    blockTitle: "margin:14px 0 6px;font-size:13px;font-weight:600;color:#334155;",
    list: "margin:0;padding-left:18px;font-size:13px;color:#475569;",
    listItem: "margin:2px 0;",
    tag: "display:inline-flex;align-items:center;padding:1px 8px;margin:2px 4px 2px 0;border-radius:999px;border:1px solid #cbd5e1;background:#f8fafc;color:#475569;font-size:11px;",
    section: "border:1px solid #e2e8f0;border-radius:8px;padding:10px 12px;margin-bottom:8px;background:#f8fafc;",
    sectionHead: "display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px;",
    sectionTitle: "font-size:13px;font-weight:600;color:#0f172a;",
    sectionMetrics: "font-size:12px;color:#64748b;",
    note: "font-size:12px;color:#b45309;margin:0 0 6px;",
    actions: "margin-top:16px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;",
    button: "padding:7px 16px;border-radius:8px;border:1px solid #1d4ed8;background:#1d4ed8;color:#ffffff;font-size:13px;font-weight:500;cursor:pointer;",
    buttonDisabled: "padding:7px 16px;border-radius:8px;border:1px solid #cbd5e1;background:#f1f5f9;color:#94a3b8;font-size:13px;font-weight:500;cursor:not-allowed;",
    hint: "font-size:12px;color:#b45309;",
    empty: "font-size:13px;color:#94a3b8;",
    row: "font-size:13px;color:#475569;margin:2px 0;",
    rowKey: "color:#64748b;",
  };

  // ---------------------------------------------------------------- 状态
  // 每个容器一份渲染状态：记录自己的 DOM 节点和事件监听，
  // clear 时只拆自己的东西，不动宿主页面的其它内容。
  const states = new WeakMap();

  // ---------------------------------------------------------------- 小工具

  function isPlainObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }

  function ownString(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    return "";
  }

  /** 取一个「可以安全显示成文本」的值。非字符串一律不显示。 */
  function safeText(value) {
    if (typeof value === "string") return value;
    if (typeof value === "number" && isFinite(value)) return String(value);
    if (typeof value === "boolean") return String(value);
    return "";
  }

  function stringList(value) {
    if (!Array.isArray(value)) return [];
    const out = [];
    for (const item of value) {
      const text = safeText(item);
      if (text) out.push(text);
    }
    return out;
  }

  function docOf(node) {
    if (node && node.ownerDocument) return node.ownerDocument;
    if (typeof document !== "undefined") return document;
    return null;
  }

  /**
   * 建一个元素并写入文本。
   * 唯一允许写入「模型产出的字符串」的入口 —— 永远是 textContent。
   */
  function makeText(doc, tag, cssText, text) {
    const node = doc.createElement(tag);
    if (cssText) node.setAttribute("style", cssText);
    node.textContent = safeText(text);
    return node;
  }

  function makeBlock(doc, tag, cssText, className) {
    const node = doc.createElement(tag);
    if (cssText) node.setAttribute("style", cssText);
    if (className) node.setAttribute("class", className);
    return node;
  }

  // ---------------------------------------------------------------- 视图归一

  /**
   * 把上层传进来的 view 归一成一份安全、好用的视图对象。
   * 字段缺失 / 类型不对都不抛错，只按「空」处理。
   */
  function normalizeView(view) {
    const source = isPlainObject(view) ? view : {};
    const plan = isPlainObject(source.reportPlan) ? source.reportPlan : null;
    const requirements = plan && isPlainObject(plan.requirements) ? plan.requirements : {};

    return {
      designPhase: ownString(source.designPhase),
      reportPlan: plan,
      topic: plan ? safeText(plan.topic) : "",
      planId: plan ? safeText(plan.id) : "",
      revision: plan && typeof plan.revision === "number" && isFinite(plan.revision) ? plan.revision : null,
      metrics: stringList(requirements.metrics),
      subject: safeText(requirements.subject),
      timeRange: safeText(requirements.timeRange),
      exclusions: stringList(requirements.exclusions),
      sections: Array.isArray(plan && plan.sections) ? plan.sections : [],
      assumptions: stringList(plan && plan.assumptions),
      suggestions: stringList(plan && plan.suggestions),
      referenceTemplates: Array.isArray(plan && plan.referenceTemplates) ? plan.referenceTemplates : [],
      busy: source.busy === true,
      error: errorText(source.error),
      canConfirm: source.canConfirm === true,
    };
  }

  /** 把错误对象压成一段安全文案。 */
  function errorText(error) {
    if (error === null || error === undefined || error === false) return "";
    if (typeof error === "string") return error;
    if (isPlainObject(error)) {
      if (typeof error.message === "string" && error.message) return error.message;
      if (typeof error.code === "string" && error.code) return error.code;
    }
    return "";
  }

  /**
   * 当前能不能确认。
   * 规则（冻结契约）：
   *   - busy 为 true → 不能确认
   *   - canConfirm 不为 true → 不能确认（**缺省 false**）
   *   - 没有可确认的方案 / 没有 planId → 不能确认
   */
  function canConfirmNow(view) {
    const normalized = normalizeView(view);
    if (normalized.busy) return false;
    if (!normalized.canConfirm) return false;
    if (!normalized.reportPlan) return false;
    if (!normalized.planId) return false;
    return true;
  }

  /**
   * 组装确认动作的载荷。
   * 载荷只有三个字段：designAction / planId / basePlanRevision。
   * 不能确认时返回 null。
   */
  function confirmPayload(view) {
    const normalized = normalizeView(view);
    if (!canConfirmNow(normalized)) return null;
    return {
      designAction: DESIGN_ACTION_CONFIRM,
      planId: normalized.planId,
      basePlanRevision: normalized.revision,
    };
  }

  function componentTypeLabel(type) {
    const key = safeText(type);
    if (!key) return "";
    return Object.prototype.hasOwnProperty.call(COMPONENT_TYPE_LABELS, key)
      ? COMPONENT_TYPE_LABELS[key]
      : key;
  }

  function designPhaseLabel(phase) {
    const key = safeText(phase);
    if (!key) return "";
    return Object.prototype.hasOwnProperty.call(PHASE_LABELS, key) ? PHASE_LABELS[key] : key;
  }

  // ---------------------------------------------------------------- 渲染

  function renderStatus(doc, view) {
    const box = makeBlock(doc, "div", STYLE.status, "plan-status");
    box.setAttribute(MODULE_ATTR, "status");

    const phaseText = designPhaseLabel(view.designPhase);
    if (phaseText) {
      box.appendChild(makeText(doc, "span", STYLE.badge, phaseText));
    }
    if (view.reportPlan && view.revision !== null) {
      box.appendChild(makeText(doc, "span", STYLE.badge, "版本 " + view.revision));
    }
    if (view.busy) {
      box.appendChild(makeText(doc, "span", STYLE.badgeBusy, "处理中…"));
    }
    return box;
  }

  function renderError(doc, view) {
    if (!view.error) return null;
    const box = makeText(doc, "div", STYLE.error, view.error);
    box.setAttribute("class", "plan-error");
    box.setAttribute(MODULE_ATTR, "error");
    box.setAttribute("role", "alert");
    return box;
  }

  function renderTopic(doc, view) {
    const box = makeBlock(doc, "div", "", "plan-topic-wrap");
    const title = makeText(doc, "h3", STYLE.topic, view.topic || "（未命名方案）");
    title.setAttribute("class", "plan-topic");
    box.appendChild(title);
    const metaParts = [];
    if (view.planId) metaParts.push("方案 ID：" + view.planId);
    if (view.revision !== null) metaParts.push("版本 " + view.revision);
    if (metaParts.length) {
      const meta = makeText(doc, "p", STYLE.meta, metaParts.join(" · "));
      meta.setAttribute("class", "plan-meta");
      box.appendChild(meta);
    }
    return box;
  }

  function renderRequirements(doc, view) {
    const box = makeBlock(doc, "div", "", "plan-requirements");
    box.appendChild(makeText(doc, "div", STYLE.blockTitle, "需求"));

    const rows = [];
    rows.push(["指标", view.metrics.length ? view.metrics.join("、") : "未指定"]);
    rows.push(["对象", view.subject || "未指定"]);
    rows.push(["时间范围", view.timeRange || "未指定"]);
    rows.push(["排除项", view.exclusions.length ? view.exclusions.join("、") : "无"]);

    for (const [key, value] of rows) {
      const row = makeBlock(doc, "div", STYLE.row, "plan-requirement");
      row.appendChild(makeText(doc, "span", STYLE.rowKey, key + "："));
      row.appendChild(makeText(doc, "span", "", value));
      box.appendChild(row);
    }
    return box;
  }

  function renderSections(doc, view) {
    const box = makeBlock(doc, "div", "", "plan-sections");
    box.appendChild(makeText(doc, "div", STYLE.blockTitle, "板块与图表建议"));

    if (!view.sections.length) {
      box.appendChild(makeText(doc, "div", STYLE.empty, "暂无板块。"));
      return box;
    }

    for (const section of view.sections) {
      const item = isPlainObject(section) ? section : {};
      const card = makeBlock(doc, "div", STYLE.section, "plan-section");

      const head = makeBlock(doc, "div", STYLE.sectionHead, "plan-section-head");
      const title = makeText(doc, "span", STYLE.sectionTitle, safeText(item.title) || "（未命名板块）");
      title.setAttribute("class", "plan-section-title");
      head.appendChild(title);

      const typeLabel = componentTypeLabel(item.componentType);
      if (typeLabel) {
        const tag = makeText(doc, "span", STYLE.tag, typeLabel);
        tag.setAttribute("class", "plan-component-type");
        tag.setAttribute("data-plan-component-type", safeText(item.componentType));
        head.appendChild(tag);
      }
      card.appendChild(head);

      const intents = stringList(item.metricIntents);
      const metrics = makeText(
        doc,
        "div",
        STYLE.sectionMetrics,
        "指标意图：" + (intents.length ? intents.join("、") : "未指定")
      );
      metrics.setAttribute("class", "plan-section-metrics");
      card.appendChild(metrics);
      box.appendChild(card);
    }
    return box;
  }

  function renderStringList(doc, title, items, className, note) {
    const box = makeBlock(doc, "div", "", className);
    box.appendChild(makeText(doc, "div", STYLE.blockTitle, title));
    if (note) box.appendChild(makeText(doc, "p", STYLE.note, note));
    if (!items.length) {
      box.appendChild(makeText(doc, "div", STYLE.empty, "无。"));
      return box;
    }
    const list = makeBlock(doc, "ul", STYLE.list, "plan-list");
    for (const item of items) {
      const li = makeBlock(doc, "li", STYLE.listItem, "plan-list-item");
      li.appendChild(makeText(doc, "span", "", item));
      list.appendChild(li);
    }
    box.appendChild(list);
    return box;
  }

  function renderSuggestions(doc, view) {
    return renderStringList(
      doc,
      "未接受的额外建议",
      view.suggestions,
      "plan-suggestions",
      "以下建议尚未接受，不会自动进入上方板块。"
    );
  }

  function renderReferences(doc, view) {
    const box = makeBlock(doc, "div", "", "plan-references");
    box.appendChild(makeText(doc, "div", STYLE.blockTitle, "参考来源"));

    if (!view.referenceTemplates.length) {
      box.appendChild(makeText(doc, "div", STYLE.empty, "暂无参考来源。"));
      return box;
    }

    const list = makeBlock(doc, "ul", STYLE.list, "plan-reference-list");
    for (const template of view.referenceTemplates) {
      const item = isPlainObject(template) ? template : {};
      const li = makeBlock(doc, "li", STYLE.listItem, "plan-reference");
      const name = safeText(item.name) || "(未命名来源)";
      const version = typeof item.version === "number" && isFinite(item.version) ? " v" + item.version : "";
      li.appendChild(makeText(doc, "span", "", name + version));
      const reason = safeText(item.reason);
      if (reason) {
        li.appendChild(makeText(doc, "span", STYLE.sectionMetrics, "（" + reason + "）"));
      }
      list.appendChild(li);
    }
    box.appendChild(list);
    return box;
  }

  function renderActions(doc, view, state) {
    const box = makeBlock(doc, "div", STYLE.actions, "plan-actions");

    const enabled = canConfirmNow(view);
    const button = makeBlock(
      doc,
      "button",
      enabled ? STYLE.button : STYLE.buttonDisabled,
      "plan-confirm"
    );
    button.setAttribute("type", "button");
    button.setAttribute(MODULE_ATTR, "confirm");
    button.setAttribute("data-plan-can-confirm", enabled ? "true" : "false");
    button.textContent = "确认方案";
    button.disabled = !enabled;
    if (!enabled) {
      button.setAttribute("title", view.busy ? "处理中，暂不可确认方案" : "当前不可确认方案");
    }

    // 点击时再查一次当前状态：即便 DOM 被绕过（disabled 被强制触发），
    // 也不会在没有确认资格时把确认动作发出去。
    const handler = function () {
      if (!state.confirmable()) return;
      const payload = confirmPayload(state.view);
      if (!payload) return;
      state.emit(payload);
    };
    button.addEventListener("click", handler);
    state.listeners.push({ node: button, type: "click", handler: handler });

    box.appendChild(button);

    if (view.busy) {
      box.appendChild(makeText(doc, "span", STYLE.hint, "处理中，暂不可确认方案。"));
    }
    return box;
  }

  function renderBody(doc, view, state) {
    const body = makeBlock(doc, "div", "", "plan-body");

    if (!view.reportPlan) {
      const note = view.busy
        ? "正在生成方案，请稍候。"
        : view.designPhase === PHASE_PLAN_NEEDED
        ? "尚未生成方案。"
        : "当前没有可显示的方案。";
      body.appendChild(makeText(doc, "div", STYLE.empty, note));
      return body;
    }

    body.appendChild(renderTopic(doc, view));
    body.appendChild(renderRequirements(doc, view));
    body.appendChild(renderSections(doc, view));
    body.appendChild(renderStringList(doc, "假设", view.assumptions, "plan-assumptions", null));
    body.appendChild(renderSuggestions(doc, view));
    body.appendChild(renderReferences(doc, view));
    body.appendChild(renderActions(doc, view, state));
    return body;
  }

  // ---------------------------------------------------------------- 公共 API

  /**
   * 渲染一份方案视图。
   *
   * @param {Element} root          宿主容器。模块只往里加自己的一层 div
   * @param {object}  view          {designPhase, reportPlan, busy, error, canConfirm}
   * @param {object}  options       {onAction}
   * @returns {undefined}
   */
  function render(root, view, options) {
    if (!root || typeof root.appendChild !== "function") {
      throw new TypeError("WorkbenchPlan.render 需要一个 DOM 容器");
    }
    const doc = docOf(root);
    if (!doc || typeof doc.createElement !== "function") {
      throw new TypeError("WorkbenchPlan.render 需要一个可用的 document");
    }

    // 重画之前先拆掉上一次自己的内容与事件，避免叠加。
    clear(root);

    const normalized = normalizeView(view);
    const opts = isPlainObject(options) ? options : {};
    const onAction = typeof opts.onAction === "function" ? opts.onAction : null;

    const state = {
      view: normalized,
      container: null,
      listeners: [],
      confirmable: function () {
        return canConfirmNow(state.view);
      },
      emit: function (payload) {
        if (!onAction) return;
        onAction(payload);
      },
    };

    const container = makeBlock(doc, "div", STYLE.module, "plan-module");
    container.setAttribute(MODULE_ATTR, MODULE_NAME);
    container.setAttribute("data-plan-phase", normalized.designPhase);
    container.setAttribute("data-plan-busy", normalized.busy ? "true" : "false");
    container.setAttribute("data-plan-can-confirm", canConfirmNow(normalized) ? "true" : "false");

    container.appendChild(renderStatus(doc, normalized));

    const errorBox = renderError(doc, normalized);
    if (errorBox) container.appendChild(errorBox);

    container.appendChild(renderBody(doc, normalized, state));

    root.appendChild(container);

    state.container = container;
    states.set(root, state);
  }

  /**
   * 清掉本模块在 root 下的内容和事件。
   * 只拆自己的东西：宿主页面里其它内容不受影响。
   */
  function clear(root) {
    if (!root || typeof root !== "object") return;
    const state = states.get(root);
    if (!state) return;

    for (const entry of state.listeners) {
      if (entry.node && typeof entry.node.removeEventListener === "function") {
        entry.node.removeEventListener(entry.type, entry.handler);
      }
    }
    state.listeners.length = 0;

    const container = state.container;
    if (container) {
      if (typeof container.remove === "function") {
        container.remove();
      } else if (container.parentNode && typeof container.parentNode.removeChild === "function") {
        container.parentNode.removeChild(container);
      }
    }
    state.container = null;
    states.delete(root);
  }

  return {
    DESIGN_ACTION_CONFIRM,
    PHASE_PLAN_NEEDED,
    PHASE_PLAN_WAITING_CONFIRM,
    COMPONENT_TYPE_LABELS,
    PHASE_LABELS,
    MODULE_ATTR,
    MODULE_NAME,
    normalizeView,
    errorText,
    canConfirmNow,
    confirmPayload,
    componentTypeLabel,
    designPhaseLabel,
    render,
    clear,
  };
});
