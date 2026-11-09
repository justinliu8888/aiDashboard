// 前端「图表渲染」独立模块（批次二 · 图表组件包 CH01–CH06）。
//
// 这个文件刻意做成不依赖主工作台的独立模块：
//   - 浏览器：window.WorkbenchChart（由协调者接入 workbench.html）
//   - Node 测试：require("../pages/workbench_chart.js")
//
// 它只做一件事：**把图表载荷按声明画出来**。
//
// ── 声明逐字生效（协议 report-definition-v1.md 的 ChartComponent）────────
//   * chartKind **逐字**读：PIE / DONUT / BAR / STACKED_BAR / LINE / AREA
//     **互不折叠**（环形图不是饼图，堆叠柱状图不是柱状图）；
//   * axes：title / minimum / maximum / format / scale 逐项落到 ECharts；
//   * legend：只有开关（协议里没有位置与方向）；seriesColors 按**稳定系列键**上色；
//   * tooltipFields：加到提示里（基础提示永远保留，取不到值显示占位符）；
//   * referenceLines：画在数值轴上（没有数值轴的图型不画）。
//
// ── 两条渲染后端 ──────────────────────────────────────────────────────
//   - ECharts 可用 → 真图表；
//   - ECharts 不可用（CDN 挂了 / 断网）→ 方案模块的 SVG 渲染器
//     （workbench_plan.js 的 renderChart），照样把曲线 / 柱条 / 扇区画出来；
//   - 两条都不通 → 把点列出来，数据仍然可见，不留空壳。
//
// 它**不**取数、不发请求、不改会话状态，也不碰主工作台已有的菜单与状态。
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.WorkbenchChart = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // ---------------------------------------------------------------- 常量

  /** 系列配色（按顺序取）—— 与主工作台既有的一张表一致。 */
  const PALETTE = ["#2563eb", "#16a34a", "#d97706", "#9333ea", "#0891b2"];

  /** 没有坐标轴的图型（分类就是扇区）。 */
  const PIE_KINDS = { PIE: true, DONUT: true };

  /** 有单一数值轴的图型（参考线只挂在这类图上）。 */
  const VALUE_AXIS_KINDS = { LINE: true, AREA: true, BAR: true, STACKED_BAR: true };

  /** 兜底渲染（ECharts 不可用时）用的组件类型。 */
  const FALLBACK_COMPONENT_TYPE = {
    LINE: "line_chart",
    AREA: "line_chart",
    BAR: "bar_chart",
    STACKED_BAR: "bar_chart",
    PIE: "pie_chart",
    DONUT: "pie_chart",
  };

  /** 本模块创建的 ECharts 实例（协调者按它 resize / dispose）。 */
  const instances = [];

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

  /** 图表库可用性（CDN 挂了也不能让整条状态机卡住）。 */
  function hasEcharts() {
    return (
      typeof window !== "undefined" &&
      !!window.echarts &&
      typeof window.echarts.init === "function"
    );
  }

  /** 主题生效时画布上的文字也要跟着主题走（颜色取服务端解析好的 token）。 */
  function themeTextColor() {
    try {
      if (typeof session === "undefined" || !session) return "";
      const color = session.resolvedTheme?.tokens?.colorText;
      return typeof color === "string" ? color : "";
    } catch (err) {
      return "";
    }
  }

  function isObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }

  /** 取一个值（支持点号路径）。 */
  function pick(source, path) {
    if (!source) return undefined;
    return String(path)
      .split(".")
      .reduce((acc, key) => (acc == null ? undefined : acc[key]), source);
  }

  /** 数值 → 显示串（取不到就是占位符，不写 undefined）。 */
  function valueText(value, unit) {
    if (value == null || value === "") return "—";
    const suffix = unit ? " " + unit : "";
    return String(value) + suffix;
  }

  /** 按声明的格式渲染一个值（优先用主工作台那份同一口径的实现）。 */
  function formatByFormat(value, format, unit) {
    const spec = isObject(format) ? format : {};
    if (
      typeof window !== "undefined" &&
      typeof window.formatDisplayValueByFormat === "function"
    ) {
      try {
        return window.formatDisplayValueByFormat(value, spec, unit || "");
      } catch (err) {
        /* 落到本地兜底 */
      }
    }
    const precision = Number(spec.precision);
    const digits = Number.isFinite(precision) ? Math.max(0, Math.min(6, precision)) : 1;
    const num = Number(value);
    let text = Number.isFinite(num) ? num.toFixed(digits) : String(value);
    if (spec.thousandSeparator === true) {
      const parts = text.split(".");
      parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
      text = parts.join(".");
    }
    const label = String(spec.unitLabel || unit || "").trim();
    if (label && Number.isFinite(num)) text = text + " " + label;
    return text;
  }

  /** 稳定系列键 → 颜色（没配就用调色板按顺序取）。 */
  function colorOf(colors, key, index) {
    const map = isObject(colors) ? colors : {};
    const name = String(key == null ? "" : key);
    if (name && Object.prototype.hasOwnProperty.call(map, name)) {
      const color = String(map[name] || "").trim();
      if (color) return color;
    }
    return PALETTE[index % PALETTE.length];
  }

  // ---------------------------------------------------------------- 坐标轴

  /** 一根轴的 ECharts 设置（按声明逐项落）。 */
  function axisOption(spec, fallbackType, options) {
    const opts = options || {};
    const source = isObject(spec) ? spec : {};
    const out = { type: fallbackType };

    const title = source.title == null ? "" : String(source.title);
    if (title) {
      out.name = title;
      out.nameTextStyle = { fontSize: 10, color: opts.nameColor || "#64748b" };
    }
    if (typeof source.minimum === "number" && isFinite(source.minimum)) {
      out.min = source.minimum;
    }
    if (typeof source.maximum === "number" && isFinite(source.maximum)) {
      out.max = source.maximum;
    }
    const scale = String(source.scale || "").toUpperCase();
    if (scale === "LINEAR") out.type = "value";
    else if (scale === "LOG") out.type = "log";
    else if (scale === "TIME") out.type = "time";
    else if (scale === "CATEGORY") out.type = "category";

    const axisLabel = { fontSize: 11 };
    if (opts.labelColor) axisLabel.color = opts.labelColor;
    if (isObject(source.format) && !opts.useServerLabels) {
      const format = source.format;
      axisLabel.formatter = (value) => formatByFormat(value, format, opts.unit || "");
    }
    out.axisLabel = axisLabel;
    return out;
  }

  // ---------------------------------------------------------------- 提示

  /** 提示里某个字段的取值：点里 → 系列点里 → 载荷的单值字段。 */
  function fieldValue(field, params, payload) {
    const name = String(field);
    const data = isObject(params?.data) ? params.data : null;
    if (data && Object.prototype.hasOwnProperty.call(data, name)) {
      return data[name];
    }
    const series = Array.isArray(payload?.series) ? payload.series : [];
    const index = Number.isFinite(params?.seriesIndex) ? params.seriesIndex : 0;
    const item = series[index];
    const points = Array.isArray(item?.points) ? item.points : [];
    const dataIndex = Number.isFinite(params?.dataIndex) ? params.dataIndex : -1;
    const point = points[dataIndex];
    if (point && Object.prototype.hasOwnProperty.call(point, name)) {
      return point[name];
    }
    const values = isObject(payload?.fieldValues) ? payload.fieldValues : null;
    if (values && Object.prototype.hasOwnProperty.call(values, name)) {
      return values[name];
    }
    return undefined;
  }

  /** 提示里的额外字段行（取不到值显示占位符，不写 undefined）。 */
  function extraFieldLines(params, fields, payload) {
    const lines = [];
    for (const field of fields) {
      const value = fieldValue(field, params, payload);
      lines.push(
        escapeHtml(String(field)) + "：" + escapeHtml(value == null ? "—" : String(value))
      );
    }
    return lines;
  }

  /** 提示框：基础提示（分类名 + 数值）永远保留，额外字段接在后面。 */
  function tooltipFormatter(payload, unit, kind) {
    const fields = Array.isArray(payload?.tooltipFields)
      ? payload.tooltipFields.filter((item) => String(item || "").trim())
      : [];
    const suffix = unit ? " " + unit : "";
    const isPie = !!PIE_KINDS[kind];
    return function (params) {
      const list = Array.isArray(params) ? params : [params];
      const first = list[0] || {};
      const lines = [];
      if (isPie) {
        lines.push(
          escapeHtml(String(first.name == null ? "" : first.name)) +
            "<br/>" +
            escapeHtml(String(first.value == null ? "—" : first.value)) +
            escapeHtml(suffix)
        );
      } else {
        const axisValue =
          first.axisValueLabel != null ? first.axisValueLabel : first.name;
        if (axisValue != null && String(axisValue) !== "") {
          lines.push(escapeHtml(String(axisValue)));
        }
        for (const item of list) {
          lines.push(
            (item.marker || "") +
            escapeHtml(String(item.seriesName == null ? "" : item.seriesName)) +
            ": " +
            escapeHtml(String(item.value == null ? "—" : item.value)) +
            escapeHtml(suffix)
          );
        }
      }
      if (fields.length) lines.push(...extraFieldLines(first, fields, payload));
      return lines.join("<br/>");
    };
  }

  // ---------------------------------------------------------------- 参考线

  /** 参考线 → ECharts markLine（只画在有数值轴的图型上）。 */
  function buildMarkLine(payload, themeColor) {
    const lines = isObject(payload?.referenceLines) ? payload.referenceLines : {};
    const data = [];
    for (const name of Object.keys(lines)) {
      const expr = lines[name];
      const value = Number(isObject(expr) ? expr.value : NaN);
      if (!Number.isFinite(value)) continue;
      data.push({
        name,
        yAxis: value,
        lineStyle: { color: "#94a3b8", type: "dashed", width: 1 },
        label: {
          formatter: name,
          fontSize: 10,
          color: themeColor || "#64748b",
        },
      });
    }
    if (!data.length) return null;
    return { silent: true, symbol: "none", data };
  }

  // ---------------------------------------------------------------- 系列

  /** 笛卡尔图（LINE / AREA / BAR / STACKED_BAR）的系列。 */
  function cartesianSeries(payload, kind, options) {
    const opts = options || {};
    const colors = isObject(payload?.seriesColors) ? payload.seriesColors : {};
    const type = kind === "BAR" || kind === "STACKED_BAR" ? "bar" : "line";
    const markLine = buildMarkLine(payload, opts.themeColor);
    const series = Array.isArray(payload?.series) && payload.series.length
      ? payload.series
      : null;
    const list = [];
    if (series) {
      series.forEach((item, index) => {
        const key = String(item?.metricKey || "");
        const color = colorOf(colors, key, index);
        const entry = {
          name: String(item?.label || key || opts.unit || "指标"),
          data: Array.isArray(item?.yValues) ? item.yValues : [],
          type,
          smooth: type === "line",
          itemStyle: { color },
          lineStyle: { color },
        };
        if (kind === "AREA") entry.areaStyle = { opacity: 0.3 };
        if (kind === "STACKED_BAR") entry.stack = "total";
        if (markLine && index === 0) entry.markLine = markLine;
        list.push(entry);
      });
      return list;
    }
    const metrics = Array.isArray(payload?.metrics) ? payload.metrics : [];
    const key = String(metrics[0]?.metricKey || "");
    const color = colorOf(colors, key, 0);
    const entry = {
      name: String(metrics[0]?.label || opts.unit || "数值"),
      data: Array.isArray(payload?.yValues) ? payload.yValues : [],
      type,
      smooth: type === "line",
      itemStyle: { color },
      lineStyle: { color },
    };
    if (kind === "AREA") entry.areaStyle = { opacity: 0.3 };
    if (kind === "STACKED_BAR") entry.stack = "total";
    if (markLine) entry.markLine = markLine;
    return [entry];
  }

  /** 饼图 / 环形图的扇区（配色用同一张调色板，不走 ECharts 默认色）。 */
  function pieSeries(payload, kind, options) {
    const opts = options || {};
    const colors = isObject(payload?.seriesColors) ? payload.seriesColors : {};
    const labels = Array.isArray(payload?.xLabels) ? payload.xLabels : [];
    const values = Array.isArray(payload?.yValues) ? payload.yValues : [];
    const data = labels.map((label, index) => ({
      name: String(label),
      value: Number(values[index] ?? 0),
      itemStyle: { color: colorOf(colors, label, index) },
    }));
    const metrics = Array.isArray(payload?.metrics) ? payload.metrics : [];
    return [
      {
        name: String(
          metrics[0]?.label || payload?.categoryLabel || opts.title || "数值"
        ),
        type: "pie",
        // 环形图有洞，饼图没有 —— 两者画出来**明显不同**。
        radius: kind === "DONUT" ? ["38%", "68%"] : ["0%", "68%"],
        center: ["50%", "52%"],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: "#fff", borderWidth: 2 },
        label: {
          formatter: (params) =>
            `${params.name}\n${params.value}${opts.unit ? " " + opts.unit : ""}`,
          fontSize: 11,
          color: opts.labelColor || "",
        },
        labelLine: { show: true },
        data,
      },
    ];
  }

  // ---------------------------------------------------------------- 说明行

  /** 图下的说明行：单位 + 数据点个数 + 模拟标记（与既有断言同一套标记）。 */
  function appendNote(container, payload, unit, count, simulated) {
    const note = document.createElement("div");
    note.className = "wch-note";
    if (unit) note.dataset.chartUnit = unit;
    if (simulated) note.dataset.chartSimulated = "true";
    note.textContent = simulated
      ? `单位：${unit || "数值"}，共 ${count} 个数据点（模拟数据）`
      : `单位：${unit || "数值"}，共 ${count} 个数据点`;
    container.appendChild(note);
  }

  // ---------------------------------------------------------------- 渲染

  /**
   * 渲染一个图表区域。
   *
   * @param {HTMLElement} container 落图的容器（内容会被清空）
   * @param {object} region  {regionId, name, role, componentType, payload, ...}
   * @param {object} ctx     {unit, defRegion, totalsByUnit, payload}
   */
  function renderChart(container, region, ctx) {
    if (!container) return;
    const r = isObject(region) ? region : {};
    const context = isObject(ctx) ? ctx : {};
    const payload = isObject(r.payload) ? r.payload : {};
    const defRegion = isObject(context.defRegion) ? context.defRegion : null;

    let unit = String(context.unit == null ? "" : context.unit);
    if (!unit && typeof window !== "undefined" && typeof window.readPayloadUnit === "function") {
      try {
        unit = String(window.readPayloadUnit(r, payload, defRegion) || "");
      } catch (err) {
        unit = "";
      }
    }
    if (!unit) unit = String(payload.unit || "");

    // 图型：**逐字**读载荷（载荷没有才退回定义里的声明）。
    const kind = String(
      payload.chartKind || pick(defRegion, "component.chartKind") || "LINE"
    ).toUpperCase();

    const themeColor = themeTextColor();
    const labelColor = themeColor ? { fontSize: 11, color: themeColor } : { fontSize: 11 };
    const simulated = payload.simulated === true;

    const displayLabels =
      Array.isArray(payload.displayXLabels) && payload.displayXLabels.length
        ? payload.displayXLabels
        : Array.isArray(payload.xLabels)
          ? payload.xLabels
          : [];
    const values = Array.isArray(payload.yValues) ? payload.yValues : [];
    const series = Array.isArray(payload.series) && payload.series.length ? payload.series : null;
    const hasData =
      displayLabels.length &&
      (values.length || (series && series[0] && series[0].yValues?.length));

    container.innerHTML = "";
    if (!hasData) {
      container.innerHTML =
        '<div class="wch-empty">这个图表还没有数据点。</div>';
      return;
    }

    // ---- ECharts 主路径 ----
    if (hasEcharts()) {
      const holder = document.createElement("div");
      holder.className = "wch-canvas";
      container.appendChild(holder);
      try {
        const chart = window.echarts.init(holder);
        instances.push(chart);
        const axes = isObject(payload.axes) ? payload.axes : {};
        const legendOption =
          payload.legend === false ? { show: false } : { show: true };
        const option = {
          tooltip: {
            trigger: PIE_KINDS[kind] ? "item" : "axis",
            formatter: tooltipFormatter(payload, unit, kind),
          },
          legend: legendOption,
        };
        if (PIE_KINDS[kind]) {
          // 饼图 / 环形图没有坐标轴 —— 不画 xAxis / yAxis。
          option.series = pieSeries(payload, kind, {
            unit,
            title: r.name || "",
            labelColor: themeColor,
          });
        } else {
          const xSpec = axes.x;
          const ySpec = axes.y;
          option.grid = { left: 60, right: 20, top: 24, bottom: 36 };
          option.xAxis = axisOption(xSpec, "category", {
            unit,
            labelColor: themeColor,
            useServerLabels:
              !!isObject(xSpec) &&
              !!isObject(xSpec.format) &&
              Array.isArray(payload.displayXLabels) &&
              payload.displayXLabels.length > 0,
          });
          option.yAxis = axisOption(ySpec, "value", {
            unit,
            labelColor: themeColor,
            nameColor: themeColor || "#64748b",
          });
          // 单位只是**兜底**：声明过的轴标题（``axes.y.title``）优先 ——
          // 不能拿单位把用户设置的标题盖掉（协议 AxisSpec.title）。
          if (unit && !option.yAxis.name) option.yAxis.name = unit;
          option.series = cartesianSeries(payload, kind, {
            unit,
            themeColor,
          });
        }
        chart.setOption(option);
        appendNote(container, payload, unit, displayLabels.length, simulated);
        return;
      } catch (err) {
        console.error("[AI Report] 图表初始化失败", err);
        container.innerHTML = "";
      }
    }

    // ---- ECharts 不可用：走方案模块的 SVG 渲染器 ----
    const api = typeof window !== "undefined" ? window.WorkbenchPlan : null;
    if (api && typeof api.renderChart === "function") {
      try {
        const node = api.renderChart(document, {
          componentType: FALLBACK_COMPONENT_TYPE[kind] || "line_chart",
          title: r.name || "",
          data: {
            xLabels: displayLabels,
            yValues: values,
            unit,
            simulated,
          },
        });
        container.appendChild(node);
        if (unit && !String(node.textContent || "").includes("单位：")) {
          appendNote(container, payload, unit, displayLabels.length, simulated);
        }
        return;
      } catch (err) {
        console.error("[AI Report] 图表渲染失败", err);
      }
    }

    // ---- 兜底：至少把点列出来，数据不丢 ----
    const rows = displayLabels
      .map(
        (label, index) =>
          `<div class="wch-fallback-row">${escapeHtml(label)}: ${escapeHtml(
            values[index] == null ? "—" : String(values[index])
          )}</div>`
      )
      .join("");
    container.innerHTML =
      `<div class="wch-fallback"><div class="wch-fallback-unit">${escapeHtml(
        unit || "数值"
      )}</div>${rows}</div>`;
  }

  /** 销毁本模块创建的图表实例（协调者从既有的 destroyCharts() 调用）。 */
  function destroy() {
    for (const chart of instances.splice(0)) {
      try {
        chart.dispose();
      } catch (err) {
        /* 忽略：实例可能已经被移除 */
      }
    }
  }

  return {
    renderChart,
    destroy,
    instances,
    escapeHtml,
  };
});
