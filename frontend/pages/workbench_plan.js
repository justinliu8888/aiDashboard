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

  /**
   * 图表类型 → 中文标签。
   *
   * 未知类型走 COMPONENT_TYPE_UNKNOWN_LABEL 中文兜底，**不**回落英文 key
   * （见 componentTypeLabel）。
   */
  const COMPONENT_TYPE_LABELS = {
    metric_card: "指标卡",
    line_chart: "折线图",
    bar_chart: "柱状图",
    pie_chart: "饼图",
    table: "表格",
    text: "文字块",
  };

  /**
   * 未知组件类型的中文兜底标签。
   *
   * 与展示目的的兜底**分开**（DISPLAY_INTENT_UNKNOWN_LABEL）：
   * 两个标签并排出现在同一个板块头里，各说各的维度 —— 用户不悬停就知道
   * 是哪一个不认识；共用一句「未知项」的话，两个都未知时就分不出谁是谁。
   * 原始 key 由调用方放进 title / data-* 属性，保留可追溯性。
   */
  const COMPONENT_TYPE_UNKNOWN_LABEL = "未知组件类型";

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

  // ---------------------------------------------------------------- 卡片文案

  /** 当前待确认那一版的卡片标题。 */
  const PLAN_CARD_TITLE_CURRENT = "最新方案";

  /** 历史版本的卡片标题前缀（后面接版本号）。 */
  const PLAN_CARD_TITLE_HISTORY = "历史版本";

  /** 历史版本的确认入口为什么点不动。 */
  const HISTORY_CONFIRM_HINT = "历史版本不能确认；只有当前待确认的最新版可以确认。";

  // ---------------------------------------------------------------- 方案版本身份
  //
  // 契约表 §4.1.2：每一版方案有一条**版本记录**，稳定 ID 叫 versionId，
  // 「同一版永远是同一个」。前端按它去重：重复响应、乱序响应与重连
  // 都不会把同一份方案追加两遍（工单 #66）。
  //
  // **键名只出现在这一段**：后端改了字段名（或者 A 段最后落定的键名与这里
  // 略有出入），只改这一处就行，不要散到全文件。

  /** 版本记录里用到的字段名。 */
  const VERSION_FIELDS = {
    versionId: "versionId",
    planId: "planId",
    planRevision: "planRevision",
    sequence: "sequence",
    createdAt: "createdAt",
    isCurrent: "isCurrent",
    confirmed: "confirmed",
    recovered: "recovered",
    reportPlan: "reportPlan",
  };

  /**
   * 「版本记录」可能挂在来源对象的哪些位置（**对象**形态）。
   *
   * A 段给本轮响应定的是**版本 ID**（字符串形态，见 VERSION_ID_PATHS）；
   * 这里留的是「整条记录」的读法，兜住别的形状。
   */
  const VERSION_SOURCE_PATHS = [
    ["planVersion"],
    ["domainObjects", "planVersion"],
    ["planVersionRecord"],
    ["domainObjects", "planVersionRecord"],
    ["reportPlanVersion"],
    ["domainObjects", "reportPlanVersion"],
  ];

  /**
   * 「版本 ID」可能挂在来源对象的哪些位置（**字符串**形态）。
   *
   * A 段定的落点：`AgentResponse.debug.planVersionId`；
   * 顶层 `AgentResponse.planVersionId` 是等价读法。
   * 它与版本序列里的 `versionId` **同源同值** —— 两处直接比就行。
   */
  const VERSION_ID_PATHS = [
    ["debug", "planVersionId"],
    ["planVersionId"],
  ];

  /** 会话快照里「版本序列」的位置：A 段定的是 `snapshot.planVersions`。 */
  const VERSION_SEQUENCE_KEYS = ["planVersions"];

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
    // ↓↓↓ #66：卡片外壳（折叠布局）与历史标记
    cardTitle: "cursor:pointer;font-size:13px;font-weight:600;color:#334155;margin:0 0 8px;list-style:revert;",
    cardBody: "margin-top:8px;",
    badgeHistory: "display:inline-flex;align-items:center;padding:2px 8px;border-radius:999px;background:#f1f5f9;color:#64748b;font-size:11px;",
    summaryLine: "font-size:13px;color:#334155;margin:0 0 8px;line-height:1.6;",
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

  // ---------------------------------------------------------------- 方案版本身份

  /** 按路径取一个值：路径里任何一层不是对象就返回 undefined。 */
  function pick(source, path) {
    let current = source;
    for (const key of path) {
      if (!isPlainObject(current)) return undefined;
      current = current[key];
    }
    return current;
  }

  /**
   * 把一条版本记录归一成前端自己用的形状。
   *
   * 缺什么就是缺：没有 versionId 就是没有，不拿 planId 顶替
   * —— versionId 是「服务端给的稳定 ID」，顶替了就等于前端自己编了一个。
   */
  function normalizeVersionRecord(raw) {
    if (!isPlainObject(raw)) return null;
    const plan = isPlainObject(raw[VERSION_FIELDS.reportPlan]) ? raw[VERSION_FIELDS.reportPlan] : null;
    const versionId = safeText(raw[VERSION_FIELDS.versionId]);
    const planId = safeText(raw[VERSION_FIELDS.planId]) || (plan ? safeText(plan.id) : "");
    const revisionRaw =
      raw[VERSION_FIELDS.planRevision] != null
        ? raw[VERSION_FIELDS.planRevision]
        : plan
          ? plan.revision
          : null;
    const planRevision = typeof revisionRaw === "number" && isFinite(revisionRaw) ? revisionRaw : null;
    if (!versionId && !planId && !plan) return null;
    return {
      versionId,
      planId,
      planRevision,
      sequence: typeof raw[VERSION_FIELDS.sequence] === "number" ? raw[VERSION_FIELDS.sequence] : null,
      createdAt: safeText(raw[VERSION_FIELDS.createdAt]),
      isCurrent: raw[VERSION_FIELDS.isCurrent] === true,
      confirmed: raw[VERSION_FIELDS.confirmed] === true,
      recovered: raw[VERSION_FIELDS.recovered] === true,
      reportPlan: plan,
    };
  }

  /**
   * 从一次响应（或一条快照记录）里读出这一版的身份。
   *
   * 找不到就返回 null —— **不猜**。调用方据此判断「这一轮有没有带来新的一版」。
   */
  function readPlanVersion(source) {
    if (!isPlainObject(source)) return null;
    // 整条记录的读法（对象形态）。
    for (const path of VERSION_SOURCE_PATHS) {
      const found = normalizeVersionRecord(pick(source, path));
      if (found) return found;
    }
    // 版本 ID 的读法（字符串形态）：A 段定的落点是 debug.planVersionId，
    // 顶层 planVersionId 是等价读法。
    const plan = readPlanBody(source);
    for (const path of VERSION_ID_PATHS) {
      const versionId = safeText(pick(source, path));
      if (!versionId) continue;
      return normalizeVersionRecord({
        versionId,
        planId: safeText(pick(source, ["debug", "planId"])) || (plan ? safeText(plan.id) : ""),
        planRevision: pick(source, ["debug", "planRevision"]) ?? (plan ? plan.revision : null),
        reportPlan: plan,
      });
    }
    // 响应没带版本 ID，但带了方案本身：按「方案身份 + 版本号」当这一版的身份。
    // 契约表 §4.1：方案身份是 (plan.id, plan.revision)；§4.1.2 的 versionId
    // 正是从「会话 + 方案 + 版本」派生的，两者一一对应。
    if (plan) return normalizeVersionRecord({ reportPlan: plan });
    return null;
  }

  /** 来源对象里的方案正文（domainObjects.reportPlan）。 */
  function readPlanBody(source) {
    const domainObjects = isPlainObject(source.domainObjects) ? source.domainObjects : null;
    const plan = domainObjects && isPlainObject(domainObjects.reportPlan) ? domainObjects.reportPlan : null;
    return plan;
  }

  /**
   * 从会话快照里读出方案版本序列（按**真实保存顺序**）。
   *
   * 老会话没有这个键 → 返回空数组：前端**不伪造**过去的版本
   * （契约表 §4.1.2「旧数据的默认行为」）。
   */
  function readPlanVersionSequence(snapshot) {
    if (!isPlainObject(snapshot)) return [];
    for (const key of VERSION_SEQUENCE_KEYS) {
      const list = snapshot[key];
      if (!Array.isArray(list)) continue;
      const out = [];
      for (const item of list) {
        const record = normalizeVersionRecord(item);
        if (record) out.push(record);
      }
      out.sort((a, b) => (a.sequence || 0) - (b.sequence || 0));
      return out;
    }
    return [];
  }

  /**
   * 这一版的**去重键**（可能不止一个别名）。
   *
   * 优先用后端给的稳定 versionId；同时给出「方案身份 + 版本号」这一个别名 ——
   * 两者语义等价（versionId 就是从会话 + 方案 + 版本派生的），所以
   * 「这次响应带 versionId、刷新后恢复只带 planId + planRevision」
   * 也不会把同一份方案追加两遍。
   */
  function versionAliasKeys(record) {
    if (!record) return [];
    const keys = [];
    if (record.versionId) keys.push("ver:" + record.versionId);
    if (record.planId) {
      keys.push("plan:" + record.planId + "#" + (record.planRevision == null ? "" : record.planRevision));
    }
    return keys;
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
      // ---- #66：卡片身份与卡片外壳 ----
      // 这一版的稳定 ID（契约表 §4.1.2）。前端按它去重。
      versionId: safeText(source.versionId),
      // 这是不是一张**历史**卡片（旧版本）：标为历史，确认入口失效。
      isHistory: source.isHistory === true,
      // 卡片要不要带折叠外壳（沿用既有的 <details> 折叠布局）。
      collapsible: source.collapsible === true,
      // 折叠外壳默认展开：最新一版在对话区底部要能读全。
      cardExpanded: source.cardExpanded !== false,
      // 卡片标题（"最新方案" / "历史版本 · 版本 N"）。
      cardTitle: ownString(source.cardTitle),
      // 卡片前的那一句简短说明（与实际提交改动一致，不编造）。
      summaryLine: ownString(source.summaryLine),
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

  /**
   * 组件类型 → 中文标签。
   *
   * 三种情况：
   *   - 空值（null / 缺字段）→ 空串：不渲染这个标签；
   *   - 已知类型 → 对应中文（译法不动，票07 / 票08b 的资产依赖它）；
   *   - **未知类型 → 中文兜底**（COMPONENT_TYPE_UNKNOWN_LABEL）。
   *
   * 未知类型**不回落英文 key**（用户看不懂），也**不映射到任何已知类型**
   * （那是猜，会显示错误语义）。原始 key 由调用方放进 title / data-* 属性。
   *
   * ⚠️ 本函数只管**显示标签**。componentType 在别处还当判断用
   * （renderChart 用原始值决定画 SVG 折线还是柱条），那些判断一律读原始值，
   * **不经过本函数** —— 显示兜底不许改到渲染逻辑。
   */
  function componentTypeLabel(type) {
    const key = safeText(type);
    if (!key) return "";
    return isKnownComponentType(key) ? COMPONENT_TYPE_LABELS[key] : COMPONENT_TYPE_UNKNOWN_LABEL;
  }

  /** 这个组件类型是否是已知取值。空值也算「不是已知取值」。 */
  function isKnownComponentType(type) {
    const key = safeText(type);
    if (!key) return false;
    return Object.prototype.hasOwnProperty.call(COMPONENT_TYPE_LABELS, key);
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
   *
   * 注意：这里的策略与展示目的 / 组件类型**故意不同** ——
   * 那两个是后端将来会**新增**的枚举，要让人看见落差（回落中文兜底）；
   * 阶段是历史会话残留的**旧**枚举，要藏起来。两者别互相「统一」。
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
    // 历史卡片：明确标成历史，别让用户以为它还能确认。
    if (view.isHistory) {
      box.appendChild(makeText(doc, "span", STYLE.badgeHistory, PLAN_CARD_TITLE_HISTORY));
    }
    if (view.busy) {
      box.appendChild(makeText(doc, "span", STYLE.badgeBusy, "处理中…"));
    }
    return box;
  }

  /**
   * 卡片前的那一句简短说明。
   *
   * 文案由上层给（服务端的说法）：前端**不自己编**这一版改了什么。
   */
  function renderSummaryLine(doc, view) {
    const text = safeText(view.summaryLine);
    if (!text) return null;
    const box = makeText(doc, "div", STYLE.summaryLine, text);
    box.setAttribute("class", "plan-summary-line");
    box.setAttribute("data-plan-summary", "");
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

  function markdownLine(value) {
    return safeText(value).replace(/[\r\n]+/g, " ");
  }

  function sectionAscii(section) {
    const keys = section.metricIntents.map((_, index) => "K" + (index + 1));
    switch (section.componentType) {
      case "metric_card": {
        const rows = [];
        for (let i = 0; i < keys.length; i += 2) {
          rows.push(keys.slice(i, i + 2).map(key => "[ " + key.padEnd(4) + " ]").join("  "));
        }
        return rows.join("\n") || "[ metric ]";
      }
      case "line_chart":
        return "value\n  |       *---*\n  |   *--*     *\n  |--*\n  +---------------- time";
      case "bar_chart":
        return "K1 | ######\nK2 | ##########\nK3 | ####\n   +-------------- value";
      case "pie_chart":
        return "composition\n+---------------------+\n|   K1   | K2 |  K3   |\n+---------------------+";
      case "table": {
        const cells = section.columns.map((_, index) => " C" + (index + 1) + " ");
        if (!cells.length) return "[ table columns to be confirmed ]";
        const rule = "+" + cells.map(cell => "-".repeat(cell.length)).join("+") + "+";
        return rule + "\n|" + cells.join("|") + "|\n" + rule + "\n|" + cells.map(cell => " ".repeat(cell.length)).join("|") + "|\n" + rule;
      }
      default:
        return "[ text / content ]";
    }
  }

  function planMarkdown(plan) {
    const view = normalizeView({ reportPlan: plan });
    const lines = ["# " + markdownLine(view.topic || "报表方案"), ""];
    if (view.revision !== null) lines.push("版本：" + view.revision, "");
    lines.push("## 需求", "",
      "- 对象：" + markdownLine(view.subject || "未指定"),
      "- 时间范围：" + markdownLine(view.timeRange || "未指定"),
      "- 指标：" + (view.metrics.map(markdownLine).join("、") || "未指定"),
      "- 排除项：" + (view.exclusions.map(markdownLine).join("、") || "无"),
      "", "所有展示数据均为虚拟数据。", "", "## 板块与展示建议", "");
    for (const [index, section] of view.sections.entries()) {
      lines.push("### " + (index + 1) + ". " + markdownLine(section.title || "未命名板块"), "",
        "- 展示方式：" + componentTypeLabel(section.componentType),
        "- 指标：" + (section.metricIntents.map(markdownLine).join("、") || "未指定"));
      if (section.displayIntent) lines.push("- 展示目的：" + displayIntentLabel(section.displayIntent));
      if (section.explicitComponentType) lines.push("- 用户指定：" + componentTypeLabel(section.explicitComponentType));
      if (section.selectionReason) lines.push("- 选择理由：" + markdownLine(section.selectionReason));
      if (section.columns.length) lines.push("- 栏目：" + section.columns.map(c => markdownLine(c.label)).join("、"));
      lines.push("", "布局示意（不代表数值）：", "", "```text", sectionAscii(section), "```", "");
      section.metricIntents.forEach((label, i) => lines.push("- K" + (i + 1) + "：" + markdownLine(label)));
      section.columns.forEach((column, i) => lines.push("- C" + (i + 1) + "：" + markdownLine(column.label)));
      lines.push("");
    }
    if (!view.sections.length) lines.push("暂无板块。", "");
    for (const [title, items] of [["假设", view.assumptions], ["未接受的额外建议", view.suggestions]]) {
      lines.push("## " + title, "");
      if (title === "未接受的额外建议") lines.push("以下建议尚未接受，不会自动进入上方板块。", "");
      lines.push(...(items.length ? items.map(item => "- " + markdownLine(item)) : ["无。"]), "");
    }
    lines.push("## 参考来源", "");
    for (const reference of view.referenceTemplates) {
      if (!isPlainObject(reference)) continue;
      lines.push("- " + markdownLine(reference.name || "未命名来源") +
        (reference.version != null ? " v" + markdownLine(reference.version) : "") +
        (reference.reason ? "：" + markdownLine(reference.reason) : ""));
    }
    if (!view.referenceTemplates.length) lines.push("暂无参考来源。");
    return lines.join("\n") + "\n";
  }

  function renderPlanMarkdown(doc, view) {
    const markdown = planMarkdown(view.reportPlan);
    const box = makeBlock(doc, "article", "font-size:13px;line-height:1.75;", "plan-markdown");
    let code = null;
    let list = null;
    for (const line of markdown.split("\n")) {
      if (line.startsWith("```")) {
        if (code) { code = null; } else {
          code = makeBlock(doc, "pre", "font-family:monospace;white-space:pre;overflow:auto;padding:10px 0;margin:8px 0;line-height:1.5;", "plan-ascii");
          box.appendChild(code);
        }
        list = null;
        continue;
      }
      if (code) { code.textContent += line + "\n"; continue; }
      if (!line) { list = null; continue; }
      const heading = /^(#{1,3}) (.*)$/.exec(line);
      if (heading) {
        list = null;
        const depth = heading[1].length;
        box.appendChild(makeText(doc, "h" + depth, depth === 1 ? STYLE.topic : STYLE.blockTitle, heading[2]));
      } else if (line.startsWith("- ")) {
        if (!list) { list = makeBlock(doc, "ul", STYLE.list, ""); box.appendChild(list); }
        list.appendChild(makeText(doc, "li", STYLE.listItem, line.slice(2)));
      } else {
        list = null;
        box.appendChild(makeText(doc, "p", "margin:6px 0;", line));
      }
    }
    const download = makeText(doc, "a", "display:inline-block;margin-top:10px;color:#2563eb;", "下载 .md 文件");
    download.setAttribute("class", "plan-markdown-download");
    download.setAttribute("download", (view.topic || "报表方案").replace(/[\\/:*?"<>|]/g, "_") + ".md");
    download.setAttribute("href", "data:text/markdown;charset=utf-8," + encodeURIComponent(markdown));
    box.appendChild(download);
    return box;
  }

  function renderActions(doc, view, state) {
    const box = makeBlock(doc, "div", STYLE.actions, "plan-actions");
    box.setAttribute("style", "margin-top:16px;");
    const enabled = canConfirmNow(view);
    const select = makeBlock(doc, "select", "width:100%;padding:9px 12px;border:1px solid #cbd5e1;border-radius:8px;background:white;font-size:13px;", "plan-action-select");
    select.setAttribute("aria-label", "方案操作");
    select.disabled = !enabled;
    for (const [value, label] of [["", "请选择方案操作"], ["confirm", "确认方案"], ["modify", "其他修改"]]) {
      const option = makeText(doc, "option", "", label);
      option.setAttribute("value", value);
      select.appendChild(option);
    }
    const other = makeBlock(doc, "div", "margin-top:10px;", "plan-other-answer");
    other.hidden = true;
    const input = makeBlock(doc, "textarea", "box-sizing:border-box;width:100%;padding:9px 12px;border:1px solid #cbd5e1;border-radius:8px;font:inherit;resize:vertical;", "plan-other-input");
    input.setAttribute("rows", "3");
    input.setAttribute("placeholder", "说说这份方案应该怎么改…");
    input.setAttribute("aria-label", "方案修改要求");
    input.disabled = !enabled;
    const submit = makeText(doc, "button", enabled ? STYLE.button : STYLE.buttonDisabled, "提交修改");
    submit.setAttribute("class", "plan-other-submit");
    submit.setAttribute("type", "button");
    submit.disabled = !enabled;
    const error = makeText(doc, "div", STYLE.hint, "");
    error.setAttribute("role", "status");
    other.appendChild(input);
    other.appendChild(submit);
    other.appendChild(error);
    const submitOther = function () {
      if (!state.confirmable()) return;
      const text = String(input.value || "").trim();
      if (!text) { error.textContent = "请先填写修改要求。"; return; }
      state.emit({ designAction: "MODIFY_PLAN", planId: view.planId, basePlanRevision: view.revision, text });
    };
    const changed = function () {
      if (!state.confirmable()) return;
      other.hidden = select.value !== "modify";
      if (select.value === "modify") {
        input.focus();
        other.scrollIntoView?.({ block: "nearest" });
      }
      if (select.value === "confirm") {
        const payload = confirmPayload(state.view);
        if (payload) state.emit(payload);
      }
    };
    const keydown = function (event) {
      if (event.isComposing) return;
      if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submitOther(); }
    };
    const edited = function () { error.textContent = ""; };
    for (const [node, type, handler] of [[select, "change", changed], [submit, "click", submitOther], [input, "keydown", keydown], [input, "input", edited]]) {
      node.addEventListener(type, handler);
      state.listeners.push({ node, type, handler });
    }
    box.appendChild(select);
    box.appendChild(other);
    if (view.isHistory) {
      box.appendChild(makeText(doc, "span", STYLE.hint, HISTORY_CONFIRM_HINT));
    } else if (view.busy) {
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
    details.appendChild(renderPlanMarkdown(doc, view));
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
    //
    // 这一层折叠视图**一直都在**（#66 的卡片外壳是外面那一层，不替代它）：
    // 已确认的方案收起来，右侧的草稿才是主角。
    if (view.designPhase === PHASE_DRAFT_READY) {
      body.appendChild(renderConfirmedPlanDetails(doc, view));
      return body;
    }

    // 待确认阶段：文字方案完整展开，末尾跟动作区。
    body.appendChild(renderPlanMarkdown(doc, view));
    body.appendChild(renderActions(doc, view, state));
    return body;
  }

  // ---------------------------------------------------------------- 公共 API

  /**
   * 卡片外壳（#66）。
   *
   * 沿用既有的折叠布局（原生 <details>，与「查看已确认的文字方案」同一套），
   * **不新增按钮**：卡片标题就是折叠开关。
   *   - 最新一版：默认展开 —— 它在对话区底部，要能读全；
   *   - 历史版本：默认收起 —— 原文留在原地，展开才看。
   */
  function renderCardShell(doc, view, state) {
    const details = makeBlock(doc, "details", "", "plan-card-details");
    details.setAttribute("data-plan-card", view.isHistory ? "history" : "current");
    details.setAttribute("data-plan-collapsed", view.cardExpanded ? "false" : "true");
    if (view.cardExpanded) details.setAttribute("open", "");
    details.appendChild(
      makeText(doc, "summary", STYLE.cardTitle, view.cardTitle || PLAN_CARD_TITLE_CURRENT)
    );
    details.appendChild(renderBody(doc, view, state));
    return details;
  }

  /**
   * 渲染一份方案视图。
   *
   * @param {Element} root          宿主容器。模块只往里加自己的一层 div
   * @param {object}  view          {designPhase, reportPlan, busy, error, canConfirm,
   *                                 versionId, isHistory, collapsible, cardExpanded,
   *                                 cardTitle, summaryLine}
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

    // 卡片前的那一句简短说明（与实际提交改动一致）。
    const summaryLine = renderSummaryLine(doc, normalized);
    if (summaryLine) container.appendChild(summaryLine);

    if (normalized.collapsible) {
      container.appendChild(renderCardShell(doc, normalized, state));
    } else {
      container.appendChild(renderBody(doc, normalized, state));
    }

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
    COMPONENT_TYPE_UNKNOWN_LABEL,
    DISPLAY_INTENT_LABELS,
    DISPLAY_INTENT_UNKNOWN_LABEL,
    PHASE_LABELS,
    MODULE_ATTR,
    MODULE_NAME,
    PLAN_CARD_TITLE_CURRENT,
    PLAN_CARD_TITLE_HISTORY,
    HISTORY_CONFIRM_HINT,
    // ---- #66：方案版本身份（去重与还原都靠它）----
    VERSION_FIELDS,
    VERSION_SOURCE_PATHS,
    VERSION_SEQUENCE_KEYS,
    normalizeVersionRecord,
    readPlanVersion,
    readPlanVersionSequence,
    versionAliasKeys,
    normalizeView,
    errorText,
    canConfirmNow,
    confirmPayload,
    componentTypeLabel,
    isKnownComponentType,
    displayIntentLabel,
    isKnownDisplayIntent,
    designPhaseLabel,
    // 图表渲染器：模板中间页下线后**改由草稿路径复用**
    // （事实：草稿路径按 ADR 0004 用模拟业务数据渲染，图表区域带 xLabels / yValues；
    //  ECharts 可用时走 ECharts，ECharts 不可用时才落到这里画 SVG）。
    // 需求稿 §3.3 点名保护这段代码，不得因为「没有调用方」而删掉。
    renderChart,
    planMarkdown,
    render,
    clear,
  };
});
