// 前端「文本与指标渲染」独立模块（批次二 · 文本 / 指标组件包 T01–T03、M01–M04、MG01–MG03）。
//
// 这个文件刻意做成不依赖主工作台的独立模块：
//   - 浏览器：window.WorkbenchTextMetric（由协调者接入 workbench.html）
//   - Node 测试：require("../pages/workbench_text_metric.js")
//
// 它只做一件事：**把文本 / 指标卡 / 指标组的载荷按声明画出来**。
//
// ── 声明逐字生效（协议 report-definition-v1.md）────────────────────────
//   * 文本：format = PLAIN（**保留换行**）或 MARKDOWN（受控子集）；
//   * 指标卡：指标级 displayValue（服务端按**指标自己的格式**算好）、
//     对比值 comparisonDisplayValue、注册图标 icon；
//   * 指标组：按 arrangement 排版（GRID 的列数 / FLOW 的方向 / gap 的间距），
//     指标顺序按定义来。
//
// ── 安全 ───────────────────────────────────────────────────────────────
//   * **先转义、后加工**：Markdown 子集在**已转义**的文本上做替换 ——
//     不注入任何原始 HTML，不产出 <script>、不产出 on* 属性、不跟随
//     javascript: 链接；
//   * 图标名按白名单正则校验后才写进属性；
//   * 它**不**取数、不发请求、不改会话状态，也不碰主工作台已有的菜单与状态。
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.WorkbenchTextMetric = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // ---------------------------------------------------------------- 常量

  /** 受控 Markdown 支持的标题层级（# ~ ###）。 */
  const MAX_HEADING_LEVEL = 3;

  /** 图标名白名单：只认 mdi:xxx 这种**固定形状**的名字。 */
  const ICON_NAME_RE = /^mdi:[a-z0-9]+(?:-[a-z0-9]+)*$/;

  /** 网格列数的取值范围（协议 ContainerArrangement.columns）。 */
  const MIN_COLUMNS = 1;
  const MAX_COLUMNS = 12;

  /** 间距的取值范围。 */
  const MIN_GAP = 0;
  const MAX_GAP = 96;

  /** 没有值时显示的占位符。 */
  const EMPTY_TEXT = "—";

  /** 危险协议名（``javascript:`` 之类）—— 正文里**原样出现也不留**。 */
  const UNSAFE_SCHEME_RE = /javascript\s*:/gi;

  // ---------------------------------------------------------------- 小工具

  /** 转义文本（所有写进 innerHTML 的文字都过这里）。 */
  function escapeHtml(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /**
   * 危险协议名**中和**：``javascript:`` → ``javascript&#58;``。
   *
   * 页面显示的样子不变（``&#58;`` 渲染出来还是冒号），
   * 但产物里不再有 ``javascript:`` 这个**可被扫出来**的序列。
   */
  function neutralizeUnsafeSchemes(text) {
    return String(text == null ? "" : text).replace(UNSAFE_SCHEME_RE, "javascript&#58;");
  }

  function isObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  /** 取一个整数（取不到就用兜底值）。 */
  function toInt(value, fallback) {
    const number = Number(value);
    if (!Number.isFinite(number)) return fallback;
    return Math.trunc(number);
  }

  /** 取一个夹在区间里的整数。 */
  function clampInt(value, fallback, low, high) {
    const number = toInt(value, fallback);
    if (number < low) return low;
    if (number > high) return high;
    return number;
  }

  // ---------------------------------------------------------------- Markdown

  /**
   * 行内标记：**在已转义的文本上**做替换。
   *
   * 只认三种：`` `code` ``、`**bold**`、`*italic*`。
   */
  function inlineMarkdown(text) {
    return String(text == null ? "" : text)
      .replace(/`([^`]+)`/g, '<code class="wtm-md-code">$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>");
  }

  /**
   * 受控 Markdown 子集：标题（# ~ ###）、无序列表、有序列表、
   * 围栏代码块、行内标记与段落。
   *
   * **绝不注入原始 HTML**：先整体转义，再在这份已转义的文本上加工。
   */
  function renderMarkdown(source) {
    const text = neutralizeUnsafeSchemes(escapeHtml(source));
    const lines = text.split(/\r?\n/);
    const out = [];
    let paragraph = [];
    let listType = "";
    let inCode = false;
    let codeLines = [];

    function flushParagraph() {
      if (!paragraph.length) return;
      out.push('<p class="wtm-md-p">' + inlineMarkdown(paragraph.join("\n")) + "</p>");
      paragraph = [];
    }

    function closeList() {
      if (!listType) return;
      out.push("</" + listType + ">");
      listType = "";
    }

    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];
      if (/^\s*```/.test(line)) {
        flushParagraph();
        closeList();
        if (!inCode) {
          inCode = true;
          codeLines = [];
        } else {
          // 代码块内容**已经转义过**（整体转义在前）—— 这里不再转义一次。
          out.push('<pre class="wtm-md-pre"><code>' + codeLines.join("\n") + "</code></pre>");
          inCode = false;
        }
        continue;
      }
      if (inCode) {
        codeLines.push(line);
        continue;
      }
      const heading = /^(#{1,3})\s+(.*)$/.exec(line);
      if (heading) {
        flushParagraph();
        closeList();
        const level = Math.min(MAX_HEADING_LEVEL, heading[1].length);
        out.push(
          '<h' + level + ' class="wtm-md-h wtm-md-h' + level + '">' +
            inlineMarkdown(heading[2]) +
            "</h" + level + ">"
        );
        continue;
      }
      const unordered = /^\s*[-*]\s+(.*)$/.exec(line);
      if (unordered) {
        flushParagraph();
        if (listType !== "ul") {
          closeList();
          out.push('<ul class="wtm-md-ul">');
          listType = "ul";
        }
        out.push('<li class="wtm-md-li">' + inlineMarkdown(unordered[1]) + "</li>");
        continue;
      }
      const ordered = /^\s*\d+\.\s+(.*)$/.exec(line);
      if (ordered) {
        flushParagraph();
        if (listType !== "ol") {
          closeList();
          out.push('<ol class="wtm-md-ol">');
          listType = "ol";
        }
        out.push('<li class="wtm-md-li">' + inlineMarkdown(ordered[1]) + "</li>");
        continue;
      }
      if (!line.trim()) {
        flushParagraph();
        closeList();
        continue;
      }
      paragraph.push(line);
    }
    flushParagraph();
    closeList();
    if (inCode) {
      out.push('<pre class="wtm-md-pre"><code>' + codeLines.join("\n") + "</code></pre>");
    }
    return out.join("");
  }

  // ---------------------------------------------------------------- 指标

  /** 注册图标 → iconify 标签（**只认白名单形状**的图标名）。 */
  function iconHtml(icon) {
    const name = String(icon || "").trim();
    if (!name || !ICON_NAME_RE.test(name)) return "";
    return '<iconify-icon class="wtm-icon" icon="' + escapeHtml(name) + '"></iconify-icon>';
  }

  /**
   * 一个指标项的显示串。
   *
   * **空值不能被吞掉**：没有值时显示格式里的 nullText，没有就显示「—」。
   */
  function metricDisplay(item, unit) {
    const spec = isObject(item.format) ? item.format : {};
    const hasValue = item.value != null && item.value !== "";
    const serverDisplay =
      item.displayValue != null && item.displayValue !== "" ? String(item.displayValue) : "";
    if (!hasValue) {
      if (serverDisplay) return serverDisplay;
      const nullText = spec.nullText != null ? String(spec.nullText) : "";
      return nullText || EMPTY_TEXT;
    }
    if (serverDisplay) return serverDisplay;
    const number = Number(item.value);
    if (Number.isFinite(number)) {
      const precision = clampInt(
        item.precision != null ? item.precision : spec.precision,
        0,
        0,
        6
      );
      const suffix = unit ? " " + unit : "";
      return number.toFixed(precision) + suffix;
    }
    const suffix = unit ? " " + unit : "";
    return String(item.value) + suffix;
  }

  /** 对比值（有才显示）。 */
  function comparisonText(item) {
    const value = item.comparisonDisplayValue;
    if (value == null || value === "") return "";
    return "对比 " + String(value);
  }

  /** 载荷里的指标项清单（没有就按载荷本身造一项）。 */
  function metricItems(payload, unit) {
    const items = asArray(payload.metrics).filter((item) => isObject(item));
    if (items.length) return items;
    return [
      {
        metricKey: payload.metricKey || "",
        id: payload.id || "",
        label: payload.label || "指标",
        value: payload.value,
        unit: payload.unit || unit || "",
        precision: payload.precision,
        displayValue: payload.displayValue,
        format: payload.format,
      },
    ];
  }

  /** 一个指标项的卡片（保持既有观感：名称在上、数值在下）。 */
  function metricCardHtml(item, unit) {
    const label = item.label || item.metricKey || "指标";
    const key = item.metricKey || item.id || label;
    // **空值不能被吞掉**：没有值就显示格式里的 nullText，没有就显示「—」。
    // （旧的渲染在 hasValue 为假时整格留空 —— 那一格看着像没渲染出来，
    //   而「没取到值」与「没有这一项」在页面上必须分得开。）
    const display = metricDisplay(item, unit);
    const compare = comparisonText(item);
    const pending = item.capabilityPending === true ? " wtm-metric--pending" : "";
    return (
      '<div class="wtm-metric' + pending + '" data-region-metric="' + escapeHtml(key) + '">' +
        '<div class="wtm-metric-head">' +
        iconHtml(item.icon) +
        '<span class="wtm-metric-label">' + escapeHtml(label) + "</span>" +
        "</div>" +
        '<div class="wtm-metric-value">' + escapeHtml(display) + "</div>" +
        (compare ? '<div class="wtm-metric-compare">' + escapeHtml(compare) + "</div>" : "") +
        "</div>"
    );
  }

  // ---------------------------------------------------------------- 文本

  /**
   * 文本区块。
   *
   * PLAIN **保留换行**（CSS 里 white-space: pre-wrap）；MARKDOWN 走受控子集。
   */
  function renderText(container, region, ctx) {
    if (!container) return;
    const payload = (region && region.payload) || {};
    const format = String(payload.format || "PLAIN").toUpperCase() === "MARKDOWN" ? "MARKDOWN" : "PLAIN";
    const source =
      payload.rendered != null && payload.rendered !== ""
        ? String(payload.rendered)
        : String(payload.template || "");
    const headingLevel =
      payload.headingLevel == null ? "" : String(payload.headingLevel);
    const notes = asArray(payload.notes)
      .map((note) => String(note == null ? "" : note))
      .filter((note) => note);
    const unbound = asArray(payload.unboundPlaceholders).map((name) => String(name || ""));

    const bodyClass = format === "MARKDOWN" ? "wtm-text wtm-text--markdown" : "wtm-text wtm-text--plain";
    let html =
      '<div class="' + bodyClass + '" data-wtm-format="' + format + '"' +
      (headingLevel === "" ? "" : ' data-wtm-heading-level="' + escapeHtml(headingLevel) + '"') +
      ">";
    html += format === "MARKDOWN" ? renderMarkdown(source) : neutralizeUnsafeSchemes(escapeHtml(source));
    html += "</div>";

    if (unbound.length) {
      html +=
        '<div class="wtm-text-note" data-wtm-unbound="' + escapeHtml(unbound.join(",")) + '">' +
        "未绑定的占位符：" + escapeHtml(unbound.join("、")) +
        "</div>";
    }
    if (notes.length) {
      html +=
        '<ul class="wtm-text-notes" data-wtm-notes="1">' +
        notes.map((note) => '<li class="wtm-text-note">' + escapeHtml(note) + "</li>").join("") +
        "</ul>";
    }
    container.innerHTML = html;
  }

  // ---------------------------------------------------------------- 指标卡

  /** 指标卡：名称在上、数值在下，另加注册图标与对比值。 */
  function renderMetricCard(container, region, ctx) {
    if (!container) return;
    const payload = (region && region.payload) || {};
    const unit = String((ctx && ctx.unit) || payload.unit || "");
    const items = metricItems(payload, unit);
    const cards = items.map((item) => metricCardHtml(item, unit)).join("");
    const pending = region && region.capabilityPending === true ? " wtm-metrics--pending" : "";
    // 只有一张卡时**占满整行**：两列布局会把卡片压到一半宽，
    // 指标名称被挤到截断（验收 08-02）。
    const single = items.length === 1 ? " wtm-metrics--single" : "";
    container.innerHTML = '<div class="wtm-metrics' + single + pending + '">' + cards + "</div>";
  }

  // ---------------------------------------------------------------- 指标组

  /** 指标组：按 arrangement 排版，指标顺序按定义来。 */
  function renderMetricGroup(container, region, ctx) {
    if (!container) return;
    const payload = (region && region.payload) || {};
    const unit = String((ctx && ctx.unit) || payload.unit || "");
    const arrangement = isObject(payload.arrangement) ? payload.arrangement : {};
    const items = metricItems(payload, unit);

    const declaredMode = String(arrangement.mode || "GRID").toUpperCase();
    // ABSOLUTE 是协议里的合法取值，但渲染侧按 GRID 行为回落。
    const mode = declaredMode === "FLOW" ? "FLOW" : "GRID";
    const columns = clampInt(arrangement.columns, 2, MIN_COLUMNS, MAX_COLUMNS);
    const direction = String(arrangement.direction || "HORIZONTAL").toUpperCase() === "VERTICAL" ? "VERTICAL" : "HORIZONTAL";
    const gap = isObject(arrangement.gap) ? arrangement.gap : {};
    const gapValue = clampInt(gap.value, 12, MIN_GAP, MAX_GAP);
    const gapUnit = String(gap.unit || "PX").toUpperCase() === "PERCENT" ? "PERCENT" : "PX";

    const styles = [];
    if (mode === "GRID") {
      styles.push("display:grid", "grid-template-columns:repeat(" + columns + ",minmax(0,1fr))");
    } else {
      styles.push(
        "display:flex",
        "flex-wrap:wrap",
        "flex-direction:" + (direction === "VERTICAL" ? "column" : "row")
      );
    }
    styles.push("--wtm-gap:" + gapValue + (gapUnit === "PERCENT" ? "%" : "px"));
    if (gapUnit !== "PERCENT") styles.push("gap:" + gapValue + "px");

    const cards = items.map((item) => metricCardHtml(item, unit)).join("");
    container.innerHTML =
      '<div class="wtm-group wtm-group--' +
      mode.toLowerCase() +
      '" data-wtm-mode="' +
      escapeHtml(declaredMode) +
      '" data-wtm-mode-applied="' +
      escapeHtml(mode) +
      '" data-wtm-gap-unit="' +
      escapeHtml(gapUnit) +
      '" style="' +
      escapeHtml(styles.join(";")) +
      '">' +
      cards +
      "</div>";
  }

  // ---------------------------------------------------------------- 出口

  return {
    renderText: renderText,
    renderMetricCard: renderMetricCard,
    renderMetricGroup: renderMetricGroup,
    escapeHtml: escapeHtml,
  };
});
