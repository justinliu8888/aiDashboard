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

  /** 设计阶段。 */
  const PHASE_PLAN_NEEDED = "PLAN_NEEDED";
  const PHASE_PLAN_WAITING_CONFIRM = "PLAN_WAITING_CONFIRM";
  /** 草稿阶段：方案已确认，右侧直接出报表草稿面板。 */
  const PHASE_DRAFT_READY = "DRAFT_READY";

  /** 图表类型 → 中文标签。未知类型原样显示（安全文本）。 */
  const COMPONENT_TYPE_LABELS = {
    metric_card: "指标卡",
    line_chart: "折线图",
    bar_chart: "柱状图",
    pie_chart: "饼图",
    table: "表格",
    text: "文字块",
  };

  /**
   * 展示目的 → 中文标签。
   *
   * 字段来自开发规格「跨工单契约」：每个板块定稿携带 displayIntent，
   * 取值是 total / trend / comparison / composition / records。
   *
   * 取值域已封闭为这 5 个（方案侧工单 02）。后端将来加新枚举时，
   * 走 DISPLAY_INTENT_UNKNOWN_LABEL 兜底，**不**回落英文 key（见 displayIntentLabel）。
   * 空值（null / 缺字段）不显示。
   */
  const DISPLAY_INTENT_LABELS = {
    total: "总量",
    trend: "趋势",
    comparison: "对比",
    composition: "构成",
    records: "明细",
  };

  /**
   * 未知展示目的的中文兜底标签。
   *
   * 后端将来加了新枚举时用它：用户看到的是中文，不是裸英文 key；
   * 原始 key 由调用方放进 title / data-* 属性，保留可追溯性。
   * 它**不**代表任何已知语义 —— 未知就是未知，不猜。
   */
  const DISPLAY_INTENT_UNKNOWN_LABEL = "未知展示目的";

  /** 设计阶段 → 中文标签。 */
  const PHASE_LABELS = {
    PLAN_NEEDED: "需要生成方案",
    PLAN_WAITING_CONFIRM: "待确认方案",
    DRAFT_READY: "草稿已就绪",
  };

  /** 容器上挂的标记属性，便于上层和测试定位自己的内容。 */
  const MODULE_ATTR = "data-plan-module";
  const MODULE_NAME = "report-plan";

  // ---------------------------------------------------------------- 样式
  // 用内联样式而不是外部 CSS：本模块不引入样式表、不改宿主页面，
  // 协调者接入时可以直接覆盖这些类名。
  const STYLE = {
    // overflow-wrap / word-break 是继承属性：写在容器上，方案卡片里所有长文本
    // 都会换行。左侧对话区最窄只有 340px，没有换行就会横向溢出。
    module: "font-family:inherit;color:#1e293b;max-width:100%;overflow-wrap:anywhere;word-break:break-word;",
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
    reason: "font-size:12px;color:#475569;margin:4px 0 0;",
    columnBlock: "margin-top:8px;",
    columnTitle: "font-size:12px;font-weight:600;color:#64748b;margin:0 0 4px;",
    columnList: "display:flex;flex-wrap:wrap;gap:4px;",
    column: "display:inline-flex;align-items:baseline;max-width:100%;padding:1px 8px;border-radius:999px;border:1px solid #cbd5e1;background:#ffffff;color:#475569;font-size:11px;",
    columnLabel: "flex:0 1 auto;min-width:0;",
    note: "font-size:12px;color:#b45309;margin:0 0 6px;",
    actions: "margin-top:16px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;",
    button: "padding:7px 16px;border-radius:8px;border:1px solid #1d4ed8;background:#1d4ed8;color:#ffffff;font-size:13px;font-weight:500;cursor:pointer;",
    buttonDisabled: "padding:7px 16px;border-radius:8px;border:1px solid #cbd5e1;background:#f1f5f9;color:#94a3b8;font-size:13px;font-weight:500;cursor:not-allowed;",
    hint: "font-size:12px;color:#b45309;",
    empty: "font-size:13px;color:#94a3b8;",
    row: "font-size:13px;color:#475569;margin:2px 0;",
    rowKey: "color:#64748b;",
    // ↓↓↓ 以下 7 个样式键是 renderChart 的依赖。
    // 需求稿 §3.3 明令「严禁删除折线图 SVG 渲染与 chartFill 样式」，
    // 它们与网格、与模板中间页都无关 —— 模板中间页下线**不代表**删它们。
    sourceNote: "font-size:11px;color:#64748b;margin-top:4px;",
    chartRow: "display:flex;align-items:center;gap:8px;font-size:12px;color:#475569;margin:3px 0;",
    chartLabel: "width:96px;flex:0 0 96px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;",
    chartTrack: "flex:1 1 auto;height:10px;border-radius:5px;background:#f1f5f9;overflow:hidden;",
    chartFill: "display:block;height:100%;border-radius:5px;background:#60a5fa;",
    chartValue: "width:72px;flex:0 0 72px;text-align:right;color:#334155;",
    emptyFrame: "border:1px dashed #cbd5e1;border-radius:6px;padding:14px;text-align:center;font-size:12px;color:#94a3b8;background:#f8fafc;",
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
   * 归一一个板块（section）。
   *
   * 只取契约里有的字段，缺的就是缺：
   *   - displayIntent / explicitComponentType / selectionReason（选型契约）
   *   - columns[]：定稿栏目，每项有 id / label / fieldRef（栏目契约）
   * 字段不存在就当没有 —— 不按标题猜、不自造默认值。
   */
  function normalizeSection(section) {
    const item = isPlainObject(section) ? section : {};
    return {
      id: safeText(item.id),
      title: safeText(item.title),
      metricIntents: stringList(item.metricIntents),
      componentType: safeText(item.componentType),
      displayIntent: safeText(item.displayIntent),
      explicitComponentType: safeText(item.explicitComponentType),
      selectionReason: safeText(item.selectionReason),
      columns: normalizeColumns(item.columns),
    };
  }

  /**
   * 归一栏目声明。
   * 没有显示名（label）的栏目整项跳过：显示名是给人看的，id / fieldRef 是技术键，
   * 拿它们当栏目名就是「自造默认值」。
   */
  function normalizeColumns(value) {
    if (!Array.isArray(value)) return [];
    const out = [];
    for (const column of value) {
      if (!isPlainObject(column)) continue;
      const label = safeText(column.label);
      if (!label) continue;
      out.push({
        id: safeText(column.id),
        label,
        fieldRef: safeText(column.fieldRef),
      });
    }
    return out;
  }

  /**
   * 把上层传进来的 view 归一成一份安全、好用的视图对象。
   * 字段缺失 / 类型不对都不抛错，只按「空」处理。
   */
  function normalizeView(view) {
    const source = isPlainObject(view) ? view : {};
    const plan = isPlainObject(source.reportPlan) ? source.reportPlan : null;
    const requirements = plan && isPlainObject(plan.requirements) ? plan.requirements : {};
    const sections = plan && Array.isArray(plan.sections) ? plan.sections : [];

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
      sections: sections.map(normalizeSection),
      assumptions: stringList(plan && plan.assumptions),
      suggestions: stringList(plan && plan.suggestions),
      referenceTemplates: Array.isArray(plan && plan.referenceTemplates) ? plan.referenceTemplates : [],
      // 折叠视图的展开态：默认收起，用户主动展开后由上层记住。
      planExpanded: source.planExpanded === true,
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

  /**
   * 展示目的 → 中文标签。
   *
   * 三种情况：
   *   - 空值（null / 缺字段）→ 空串：不渲染这一行；
   *   - 5 个已知取值 → 对应中文；
   *   - **未知取值 → 中文兜底**（DISPLAY_INTENT_UNKNOWN_LABEL）。
   *
   * 未知取值**不回落英文 key**：用户看不懂英文，而且裸英文正说明
   * 「后端枚举与前端映射没同步」—— 那是要暴露的问题，不是要洗成中文的样子。
   * 未知取值也**不映射到任何已知取值**：那是猜，会显示错误语义。
   * 原始 key 由调用方放进 title / data-* 属性，保留可追溯性。
   */
  function displayIntentLabel(intent) {
    const key = safeText(intent);
    if (!key) return "";
    return isKnownDisplayIntent(key) ? DISPLAY_INTENT_LABELS[key] : DISPLAY_INTENT_UNKNOWN_LABEL;
  }

  /** 这个展示目的是否是已知取值。空值也算「不是已知取值」。 */
  function isKnownDisplayIntent(intent) {
    const key = safeText(intent);
    if (!key) return false;
    return Object.prototype.hasOwnProperty.call(DISPLAY_INTENT_LABELS, key);
  }

  /**
   * 阶段 → 中文标签。
   *
   * **未知阶段返回空串**（而不是原始 key）：历史会话里可能还留着
   * 「模板阶段」的旧枚举，直接把 key 渲染出去会露出英文徽标。
   * 空串意味着「不渲染这个徽标」。
   */
  function designPhaseLabel(phase) {
    const key = safeText(phase);
    if (!key) return "";
    return Object.prototype.hasOwnProperty.call(PHASE_LABELS, key) ? PHASE_LABELS[key] : "";
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

      // 展示目的：用户想看什么（总量 / 趋势 / 对比 / 构成 / 明细）。
      const intentLabel = displayIntentLabel(item.displayIntent);
      if (intentLabel) {
        const tag = makeText(doc, "span", STYLE.tag, intentLabel);
        tag.setAttribute("class", "plan-display-intent");
        tag.setAttribute("data-plan-display-intent", safeText(item.displayIntent));
        // 未知取值：标签上是中文兜底，原始 key 放进 title（悬停可见）——
        // 既不把英文外露到界面上，又不把「后端加了新枚举」这件事藏起来。
        // 已知取值不加 title：它的中文已经是人话，原文在 data-* 里就够追溯了。
        if (!isKnownDisplayIntent(item.displayIntent)) {
          tag.setAttribute("title", safeText(item.displayIntent));
        }
        head.appendChild(tag);
      }

      // 用户明确指定的展示类型：与系统按意图选出的类型分开标，
      // 免得把「用户指定」看成「系统自己决定的」。
      const explicitLabel = componentTypeLabel(item.explicitComponentType);
      if (explicitLabel) {
        const tag = makeText(doc, "span", STYLE.tag, "用户指定：" + explicitLabel);
        tag.setAttribute("class", "plan-explicit-type");
        tag.setAttribute("data-plan-explicit-type", safeText(item.explicitComponentType));
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

      // 选型理由：系统为什么这么画。没有这一项就不显示，不自造。
      const reason = safeText(item.selectionReason);
      if (reason) {
        const reasonBox = makeText(doc, "div", STYLE.reason, "选型理由：" + reason);
        reasonBox.setAttribute("class", "plan-selection-reason");
        reasonBox.setAttribute("data-plan-selection-reason", reason);
        card.appendChild(reasonBox);
      }

      // 表格板块声明的栏目：方案里写清楚这张表要展示哪些列。
      const columns = Array.isArray(item.columns) ? item.columns : [];
      if (columns.length) {
        const columnBox = makeBlock(doc, "div", STYLE.columnBlock, "plan-columns");
        columnBox.appendChild(makeText(doc, "div", STYLE.columnTitle, "栏目"));
        const list = makeBlock(doc, "div", STYLE.columnList, "plan-column-list");
        for (const column of columns) {
          const chip = makeBlock(doc, "span", STYLE.column, "plan-column");
          chip.setAttribute("data-plan-column-id", safeText(column.id));
          chip.setAttribute("data-plan-column-field-ref", safeText(column.fieldRef));
          // 给人看的是栏目名；取值键（fieldRef）是技术键，
          // 放 title 里（悬停可见），不跟名字挤在一行 —— 窄栏里挤在一起会被拦腰截断。
          if (safeText(column.fieldRef)) chip.setAttribute("title", safeText(column.fieldRef));
          chip.appendChild(makeText(doc, "span", STYLE.columnLabel, safeText(column.label)));
          list.appendChild(chip);
        }
        columnBox.appendChild(list);
        card.appendChild(columnBox);
      }

      box.appendChild(card);
    }
    return box;
  }

  // ---------------------------------------------------------------- 图表渲染
  //
  // renderChart 把一组数据点画成折线图（SVG）或柱条图。
  //
  // 它**不随模板中间页下线而删除**：需求稿 §3.3 点名保护这段代码
  // （「严禁删除折线图 SVG 渲染与 chartFill 样式」）—— 它是独立的图表改进，
  // 与网格、与业务链路都无关。模板中间页下线只是**换了调用方**：
  //   - 模板中间页下线后，本模块自己不再调用它；
  //   - 草稿路径（workbench.js）在 ECharts 不可用时调用它画 SVG。
  //
  // 草稿阶段**不再画空壳**：按 ADR 0004，模版阶段一律用模拟业务数据渲染，
  // 图表区域带 xLabels / yValues —— 这条路径是**真的会被调用**的。

  function renderChart(doc, region) {
    const data = isPlainObject(region.data) ? region.data : {};
    const labels = Array.isArray(data.xLabels) ? data.xLabels : [];
    const values = Array.isArray(data.yValues) ? data.yValues : [];
    const box = makeBlock(doc, "div", "", "template-chart");

    if (!labels.length || !values.length) {
      box.appendChild(makeText(doc, "div", STYLE.emptyFrame, "暂无数据"));
      return box;
    }

    let max = 0;
    for (const value of values) {
      const num = Number(value);
      if (isFinite(num) && num > max) max = num;
    }

    if (region.componentType === "line_chart" && typeof doc.createElementNS === "function") {
      const svgNode = (tag, attributes) => {
        const node = doc.createElementNS("http://www.w3.org/2000/svg", tag);
        for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
        return node;
      };
      const svg = svgNode("svg", { viewBox: "0 0 360 180", width: "100%", role: "img", "aria-label": region.title });
      svg.appendChild(svgNode("path", { d: "M 32 12 V 150 H 348", fill: "none", stroke: "#cbd5e1" }));
      const points = values.map((value, index) => ({
        x: 32 + index * 316 / Math.max(1, values.length - 1),
        y: 150 - (max > 0 && isFinite(Number(value)) ? Number(value) / max : 0) * 125,
      }));
      svg.appendChild(svgNode("polyline", {
        points: points.map(({ x, y }) => x + "," + y).join(" "),
        fill: "none", stroke: "#2563eb", "stroke-width": 2,
      }));
      points.forEach(({ x, y }, index) => {
        const dot = svgNode("circle", { cx: x, cy: y, r: 3, fill: "#2563eb" });
        const title = svgNode("title", {});
        title.textContent = safeText(labels[index]) + ": " + safeText(values[index]) + " " + safeText(data.unit);
        dot.appendChild(title);
        svg.appendChild(dot);
      });
      for (const [index, x, anchor] of [[0, 32, "start"], [labels.length - 1, 348, "end"]]) {
        const label = svgNode("text", { x, y: 170, "text-anchor": anchor, "font-size": 11, fill: "#64748b" });
        label.textContent = safeText(labels[index]);
        svg.appendChild(label);
      }
      box.appendChild(svg);
      box.appendChild(makeText(doc, "div", STYLE.sourceNote, "单位：" + safeText(data.unit) + "，共 " + values.length + " 个数据点"));
      return box;
    }

    const limit = 12;
    for (let index = 0; index < labels.length && index < limit; index += 1) {
      const row = makeBlock(doc, "div", STYLE.chartRow, "template-chart-row");
      row.appendChild(makeText(doc, "span", STYLE.chartLabel, safeText(labels[index]) || "—"));

      const track = makeBlock(doc, "span", STYLE.chartTrack, "template-chart-track");
      const fill = makeBlock(doc, "span", STYLE.chartFill, "template-chart-fill");
      const num = Number(values[index]);
      const ratio = max > 0 && isFinite(num) ? Math.max(0, Math.min(1, num / max)) : 0;
      fill.style.width = Math.round(ratio * 100) + "%";
      track.appendChild(fill);
      row.appendChild(track);

      row.appendChild(
        makeText(doc, "span", STYLE.chartValue, safeText(values[index]) || "—")
      );
      box.appendChild(row);
    }
    if (labels.length > limit) {
      box.appendChild(
        makeText(doc, "div", STYLE.sourceNote, "仅显示前 " + limit + " 项")
      );
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

    // 模板中间页整页下线（#40）：这里**不再**有模板中间页的那两个按钮
    // （确认 / 修改），也不换成任何提示语 —— 规格明写「不替换成单行模板提示」。
    // 文字方案的「确认方案」入口保留（这是唯一的人工确认入口）。
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

  /**
   * 已确认方案的折叠视图。
   *
   * - 原生 <details>：默认收起、可展开、零状态、零 CSS。
   * - 内容与「待确认」时看到的文字方案完全一致（复用同一批渲染函数，不复制逻辑）。
   * - 纯 DOM 合成，不发任何请求 —— 渲染它不会重新生成方案。
   */
  function renderConfirmedPlanDetails(doc, view) {
    const details = makeBlock(doc, "details", "margin-top:12px;", "confirmed-plan-details");
    details.setAttribute("data-plan-collapsed", view.planExpanded ? "false" : "true");
    if (view.planExpanded) details.setAttribute("open", "");
    details.appendChild(makeText(doc, "summary", STYLE.blockTitle, "查看已确认的文字方案"));
    details.appendChild(renderTopic(doc, view));
    details.appendChild(renderRequirements(doc, view));
    details.appendChild(renderSections(doc, view));
    details.appendChild(renderStringList(doc, "假设", view.assumptions, "plan-assumptions", null));
    details.appendChild(renderSuggestions(doc, view));
    details.appendChild(renderReferences(doc, view));
    return details;
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

    // 草稿阶段：方案已确认 —— 只保留一份默认收起的折叠视图。
    // 这里没有动作区：既没有「确认方案」，也没有模板中间页的两个按钮。
    //
    // 判据一律以 designPhase 为准，**不要**拿「内部模板」那个中间产物做判据：
    // 新流程下后端仍可能把内部模板作为只读中间产物带回响应里，
    // 拿它做判据会让模板态以另一种形式复活。
    if (view.designPhase === PHASE_DRAFT_READY) {
      body.appendChild(renderConfirmedPlanDetails(doc, view));
      return body;
    }

    // 待确认阶段：文字方案完整展开，末尾跟动作区。
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
      disposed: false,
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
    state.disposed = true;

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
    PHASE_DRAFT_READY,
    COMPONENT_TYPE_LABELS,
    DISPLAY_INTENT_LABELS,
    DISPLAY_INTENT_UNKNOWN_LABEL,
    PHASE_LABELS,
    MODULE_ATTR,
    MODULE_NAME,
    normalizeView,
    errorText,
    canConfirmNow,
    confirmPayload,
    componentTypeLabel,
    displayIntentLabel,
    isKnownDisplayIntent,
    designPhaseLabel,
    // 图表渲染器：模板中间页下线后**改由草稿路径复用**
    // （事实：草稿路径按 ADR 0004 用模拟业务数据渲染，图表区域带 xLabels / yValues；
    //  ECharts 可用时走 ECharts，ECharts 不可用时才落到这里画 SVG）。
    // 需求稿 §3.3 点名保护这段代码，不得因为「没有调用方」而删掉。
    renderChart,
    render,
    clear,
  };
});
