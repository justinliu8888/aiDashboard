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
  const PHASE_TEMPLATE_READY = "TEMPLATE_READY";

  /** 区域数据状态。 */
  const REGION_BOUND = "bound";
  const REGION_PLACEHOLDER = "placeholder";
  const REGION_TEXT = "text";

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
    TEMPLATE_READY: "模板已就绪",
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
    templateWrap: "margin-top:18px;border-top:1px solid #e2e8f0;padding-top:14px;",
    templateTitle: "margin:0 0 4px;font-size:14px;font-weight:600;color:#0f172a;",
    templateMeta: "font-size:12px;color:#64748b;margin:0 0 10px;",
    region: "border:1px solid #e2e8f0;border-radius:8px;padding:10px 12px;margin-bottom:8px;background:#ffffff;",
    regionPlaceholder: "border:1px dashed #cbd5e1;border-radius:8px;padding:10px 12px;margin-bottom:8px;background:#f8fafc;",
    regionHead: "display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px;",
    regionTitle: "font-size:13px;font-weight:600;color:#0f172a;",
    badgeBound: "display:inline-flex;align-items:center;padding:1px 8px;border-radius:999px;background:#dcfce7;color:#15803d;font-size:11px;",
    badgePlaceholder: "display:inline-flex;align-items:center;padding:1px 8px;border-radius:999px;background:#fef3c7;color:#b45309;font-size:11px;",
    badgeText: "display:inline-flex;align-items:center;padding:1px 8px;border-radius:999px;background:#e2e8f0;color:#475569;font-size:11px;",
    metricValue: "font-size:22px;font-weight:600;color:#0f172a;margin:2px 0;",
    metricLabel: "font-size:12px;color:#64748b;",
    metricNote: "font-size:11px;color:#b45309;margin-top:2px;",
    sourceNote: "font-size:11px;color:#64748b;margin-top:4px;",
    chartRow: "display:flex;align-items:center;gap:8px;font-size:12px;color:#475569;margin:3px 0;",
    chartLabel: "width:96px;flex:0 0 96px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;",
    chartTrack: "flex:1 1 auto;height:10px;border-radius:5px;background:#f1f5f9;overflow:hidden;",
    chartFill: "display:block;height:100%;border-radius:5px;background:#60a5fa;",
    chartValue: "width:72px;flex:0 0 72px;text-align:right;color:#334155;",
    emptyFrame: "border:1px dashed #cbd5e1;border-radius:6px;padding:14px;text-align:center;font-size:12px;color:#94a3b8;background:#f8fafc;",
    table: "width:100%;border-collapse:collapse;font-size:12px;color:#475569;",
    tableHead: "text-align:left;border-bottom:1px solid #e2e8f0;padding:4px 6px;font-weight:600;color:#334155;",
    tableCell: "padding:4px 6px;border-bottom:1px solid #f1f5f9;",
    tableEmpty: "padding:8px 6px;color:#94a3b8;",
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
    const template = isPlainObject(source.templateDesign) ? source.templateDesign : null;

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
      templateDesign: template,
      templateTitle: template ? safeText(template.title) : "",
      templateRegions: template && Array.isArray(template.regions) ? template.regions : [],
      templatePendingNotes: stringList(template && template.pendingNotes),
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

  // ---------------------------------------------------------------- 模板渲染
  //
  // 用户口径（需求访谈 Q13）：「数据库里有啥数据就展示啥，没有的
  // 就按 Q11 和 Q12 的方案来」。
  //
  // 所以这里分两种画法：
  //   - 接到数据的区域：把**真实取到的值**画出来；
  //   - 没接到数据的区域：画占位（指标卡显示 `--`、图表是空框、
  //     表格是表头加空行），并把原因写出来。
  //
  // 占位区域**不画任何数值** —— 那会把假数画成真数。

  function regionBadge(doc, status) {
    if (status === REGION_BOUND) {
      return makeText(doc, "span", STYLE.badgeBound, "已接数据");
    }
    if (status === REGION_TEXT) {
      return makeText(doc, "span", STYLE.badgeText, "文字说明");
    }
    return makeText(doc, "span", STYLE.badgePlaceholder, "占位");
  }

  function renderRegionHead(doc, region, status) {
    const head = makeBlock(doc, "div", STYLE.regionHead, "template-region-head");
    const title = makeText(
      doc,
      "span",
      STYLE.regionTitle,
      safeText(region.title) || "（未命名区域）"
    );
    title.setAttribute("class", "template-region-title");
    head.appendChild(title);

    const typeLabel = componentTypeLabel(region.componentType);
    if (typeLabel) {
      const tag = makeText(doc, "span", STYLE.tag, typeLabel);
      tag.setAttribute("class", "template-component-type");
      head.appendChild(tag);
    }
    head.appendChild(regionBadge(doc, status));
    return head;
  }

  function renderMetricCard(doc, region) {
    const data = isPlainObject(region.data) ? region.data : {};
    const label = safeText(data.label) || "指标";
    const box = makeBlock(doc, "div", "", "template-metric-card");

    box.appendChild(makeText(doc, "div", STYLE.metricLabel, label));

    const value = safeText(data.value);
    const unit = safeText(data.unit);
    box.appendChild(
      makeText(
        doc,
        "div",
        STYLE.metricValue,
        value === "" ? "--" : value + (unit ? " " + unit : "")
      )
    );

    if (data.comparisonAvailable === true && safeText(data.yoyPercent) !== "") {
      box.appendChild(
        makeText(
          doc,
          "div",
          STYLE.sourceNote,
          "较上一期 " + safeText(data.yoyPercent) + "%"
        )
      );
    }
    if (safeText(data.windowLabel)) {
      box.appendChild(
        makeText(doc, "div", STYLE.sourceNote, "统计窗口：" + safeText(data.windowLabel))
      );
    }
    return box;
  }

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

  function renderTable(doc, region) {
    const data = isPlainObject(region.data) ? region.data : {};
    const columns = Array.isArray(data.columns) ? data.columns : [];
    const rows = Array.isArray(data.rows) ? data.rows : [];
    const box = makeBlock(doc, "div", "", "template-table");

    const table = makeBlock(doc, "table", STYLE.table, "template-table-el");
    const head = makeBlock(doc, "thead", "", "");
    const headRow = makeBlock(doc, "tr", "", "");
    for (const column of columns) {
      const item = isPlainObject(column) ? column : {};
      headRow.appendChild(
        makeText(doc, "th", STYLE.tableHead, safeText(item.label) || safeText(item.id))
      );
    }
    head.appendChild(headRow);
    table.appendChild(head);

    const body = makeBlock(doc, "tbody", "", "");
    if (!rows.length) {
      const tr = makeBlock(doc, "tr", "", "template-table-empty-row");
      const td = makeBlock(doc, "td", STYLE.tableEmpty, "暂无数据");
      td.setAttribute("colspan", String(Math.max(1, columns.length)));
      tr.appendChild(td);
      body.appendChild(tr);
    } else {
      for (const row of rows) {
        const item = isPlainObject(row) ? row : {};
        const tr = makeBlock(doc, "tr", "", "template-table-row");
        for (const column of columns) {
          const col = isPlainObject(column) ? column : {};
          const fieldRef = safeText(col.fieldRef);
          tr.appendChild(makeText(doc, "td", STYLE.tableCell, safeText(item[fieldRef])));
        }
        body.appendChild(tr);
      }
    }
    table.appendChild(body);
    box.appendChild(table);
    return box;
  }

  function renderRegion(doc, region, index) {
    const item = isPlainObject(region) ? region : {};
    const status = ownString(item.dataStatus) || REGION_PLACEHOLDER;
    const card = makeBlock(
      doc,
      "div",
      status === REGION_BOUND ? STYLE.region : STYLE.regionPlaceholder,
      "template-region"
    );
    card.setAttribute("data-template-region-status", status);
    card.setAttribute("data-template-region-index", String(index));

    card.appendChild(renderRegionHead(doc, item, status));

    if (status === REGION_BOUND) {
      const type = ownString(item.componentType);
      if (type === "metric_card") {
        card.appendChild(renderMetricCard(doc, item));
      } else if (type === "table") {
        card.appendChild(renderTable(doc, item));
      } else if (type === "text") {
        const data = isPlainObject(item.data) ? item.data : {};
        card.appendChild(makeText(doc, "div", STYLE.row, safeText(data.rendered)));
      } else {
        card.appendChild(renderChart(doc, item));
      }
      const sourceLabel = safeText(item.dataSourceLabel);
      if (sourceLabel) {
        const note = makeText(doc, "div", STYLE.sourceNote, sourceLabel);
        note.setAttribute("class", "template-source-note");
        card.appendChild(note);
      }
    } else if (status === REGION_TEXT) {
      const data = isPlainObject(item.data) ? item.data : {};
      card.appendChild(makeText(doc, "div", STYLE.row, safeText(data.rendered)));
    } else {
      // 占位：不画数值，只说明为什么还没有数。
      const type = ownString(item.componentType);
      if (type === "metric_card") {
        card.appendChild(makeText(doc, "div", STYLE.metricLabel, "指标"));
        card.appendChild(makeText(doc, "div", STYLE.metricValue, "--"));
      } else if (type === "table") {
        card.appendChild(renderTable(doc, { data: { columns: [], rows: [] } }));
      } else {
        card.appendChild(
          makeText(doc, "div", STYLE.emptyFrame, "数据待连接，暂无内容")
        );
      }
      const reason = safeText(item.placeholderReason);
      if (reason) {
        const note = makeText(doc, "div", STYLE.metricNote, reason);
        note.setAttribute("class", "template-placeholder-reason");
        card.appendChild(note);
      }
    }
    return card;
  }

  function templateGridItems(template) {
    const layout = template.layout || {};
    const placements = new Map((layout.items || []).map((item) => [item.regionId, item]));
    return (template.regions || []).map((region, index) => {
      const item = placements.get(region.id);
      const legacyColumn = item && Number.isInteger(item.column) ? item.column : index % 2;
      const legacyRow = item && Number.isInteger(item.row) ? item.row : Math.floor(index / 2);
      return {
        regionId: region.id,
        label: region.title,
        placement: item && item.placement ? { ...item.placement } : {
          type: "GRID", column: legacyColumn * 6 + 1, row: legacyRow * 5 + 1,
          columnSpan: 6, rowSpan: 5,
        },
      };
    });
  }

  function mountTemplateGrid(state) {
    const gridApi = typeof globalThis !== "undefined" && globalThis.WorkbenchGrid;
    if (!state.gridRoot || !gridApi) return;
    state.gridItems = templateGridItems(state.view.templateDesign);
    const readonly = state.view.busy || !state.onLayoutChange;
    state.grid = gridApi.mountReportGrid(state.gridRoot, {
      items: state.gridItems,
      readonly,
      renderContent: ({ regionId }) => {
        const index = state.view.templateRegions.findIndex((region) => region.id === regionId);
        return renderRegion(docOf(state.gridRoot), state.view.templateRegions[index], index);
      },
      onIntent: async (intent) => {
        if (state.disposed || state.gridSaving || !state.onLayoutChange) return;
        const previous = state.gridItems;
        const changed = new Map(intent.placements.map((item) => [item.regionId, item.placement]));
        const next = previous.map((item) => ({ ...item, placement: changed.get(item.regionId) || item.placement }));
        state.gridSaving = true;
        state.gridNote.textContent = "正在保存布局…";
        state.grid.update({ items: next, readonly: true });
        try {
          const template = await state.onLayoutChange(state.view.templateDesign,
            next.map(({ regionId, placement }) => ({ regionId, placement })));
          if (state.disposed) return;
          state.view.templateDesign = template;
          state.gridItems = templateGridItems(template);
          state.gridNote.textContent = "布局已保存；可继续拖动区域或蓝色手柄调整。";
        } catch (error) {
          if (state.disposed) return;
          state.gridItems = previous;
          state.gridNote.textContent = "未保存，已恢复原布局：" + errorText(error);
        } finally {
          if (!state.disposed) {
            state.gridSaving = false;
            state.grid.update({ items: state.gridItems, readonly: false });
          }
        }
      },
    });
  }

  function renderTemplate(doc, view, state) {
    if (!view.templateDesign) return null;
    const box = makeBlock(doc, "div", STYLE.templateWrap, "template-design");
    box.setAttribute(MODULE_ATTR, "template");

    const title = makeText(
      doc,
      "h4",
      STYLE.templateTitle,
      view.templateTitle || "报表模板"
    );
    title.setAttribute("class", "template-title");
    box.appendChild(title);

    const bound = view.templateRegions.filter(
      (region) => (isPlainObject(region) ? region.dataStatus : "") === REGION_BOUND
    ).length;
    const metaParts = ["共 " + view.templateRegions.length + " 个区域"];
    metaParts.push(bound + " 个已接数据");
    metaParts.push(view.templateRegions.length - bound + " 个占位");
    box.appendChild(makeText(doc, "p", STYLE.templateMeta, metaParts.join(" · ")));

    const gridApi = typeof globalThis !== "undefined" && globalThis.WorkbenchGrid;
    if (gridApi) {
      state.gridNote = makeText(doc, "p", STYLE.templateMeta,
        state.onLayoutChange ? "拖动区域或蓝色手柄调整布局，松开后自动保存。" : "模板网格预览");
      state.gridNote.setAttribute("class", "template-grid-status");
      box.appendChild(state.gridNote);
      state.gridRoot = makeBlock(doc, "div", "margin-bottom:12px;", "template-grid");
      box.appendChild(state.gridRoot);
    } else {
      for (let index = 0; index < view.templateRegions.length; index += 1) {
        box.appendChild(renderRegion(doc, view.templateRegions[index], index));
      }
    }

    if (view.templatePendingNotes.length) {
      const notes = makeBlock(doc, "div", "", "template-pending-notes");
      for (const note of view.templatePendingNotes) {
        notes.appendChild(makeText(doc, "div", STYLE.metricNote, note));
      }
      box.appendChild(notes);
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

    // 模板已经生成：不再摆「确认方案」入口 —— 方案已经确认过了。
    if (view.templateDesign) {
      box.appendChild(makeText(doc, "span", STYLE.hint, "报表已生成，确认继续，还是修改？"));
      for (const [className, label, action] of [
        ["template-confirm", "确认报表，继续下一步", "CONFIRM_TEMPLATE"],
        ["template-modify", "修改报表", "MODIFY_PLAN"],
      ]) {
        const button = makeBlock(doc, "button", view.busy ? STYLE.buttonDisabled : STYLE.button, className);
        button.textContent = label;
        button.setAttribute("type", "button");
        button.disabled = view.busy;
        const handler = () => {
          if (state.view.busy || state.gridSaving || state.disposed) return;
          state.emit({ designAction: action, templateId: state.view.templateDesign.id,
            baseTemplateRevision: state.view.templateDesign.revision });
        };
        button.addEventListener("click", handler);
        state.listeners.push({ node: button, type: "click", handler });
        box.appendChild(button);
      }
      return box;
    }

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

    const template = renderTemplate(doc, view, state);
    if (template) {
      body.appendChild(renderActions(doc, view, state));
      body.appendChild(template);
      const details = makeBlock(doc, "details", "margin-top:12px;", "confirmed-plan-details");
      details.appendChild(makeText(doc, "summary", STYLE.blockTitle, "查看已确认的文字方案"));
      details.appendChild(renderTopic(doc, view));
      details.appendChild(renderRequirements(doc, view));
      details.appendChild(renderSections(doc, view));
      details.appendChild(renderStringList(doc, "假设", view.assumptions, "plan-assumptions", null));
      details.appendChild(renderSuggestions(doc, view));
      details.appendChild(renderReferences(doc, view));
      body.appendChild(details);
    } else {
      body.appendChild(renderTopic(doc, view));
      body.appendChild(renderRequirements(doc, view));
      body.appendChild(renderSections(doc, view));
      body.appendChild(renderStringList(doc, "假设", view.assumptions, "plan-assumptions", null));
      body.appendChild(renderSuggestions(doc, view));
      body.appendChild(renderReferences(doc, view));
    }
    if (!template) body.appendChild(renderActions(doc, view, state));
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
      onLayoutChange: typeof opts.onLayoutChange === "function" ? opts.onLayoutChange : null,
      grid: null,
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
    mountTemplateGrid(state);
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
    if (state.grid) state.grid.destroy();

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
    templateGridItems,
    errorText,
    canConfirmNow,
    confirmPayload,
    componentTypeLabel,
    designPhaseLabel,
    render,
    clear,
  };
});
