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
//     轴标题的**排版**在画布内部解决（nameLocation / nameGap / grid 边距）——
//     横轴标题居中放在刻度下面（不再落在轴的末端被裁掉），纵轴标题竖排居中；
//   * legend：只有开关（协议里没有位置与方向）；seriesColors 按**稳定系列键**上色；
//   * tooltipFields：加到提示里（基础提示永远保留，取不到值显示占位符）；
//   * referenceLines：画在数值轴上（没有数值轴的图型不画）；
//     用户没固定上下界时，数值轴的自动范围**同时覆盖数据值和参考线值**（含边距）；
//     用户固定过的边界**不动** —— 参考线落在外面时在图下写一句提示。
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

  // ---------------------------------------------------------------- 排版参数
  //
  // 轴标题与画布留白都**在画布内部**解决：不放大外层卡片、不把字号缩到认不出、
  // 不截断标题 —— 只调 ECharts 的 nameLocation / nameGap 与 grid 边距。

  /** 轴标题的字号（沿用既有的 nameTextStyle 口径）。 */
  const AXIS_NAME_FONT_SIZE = 10;
  /** 轴刻度文字的字号（用于估算刻度宽度、给标题让位）。 */
  const AXIS_LABEL_FONT_SIZE = 11;
  /** 横轴标题离轴线的距离（居中放在刻度下面）。 */
  const X_AXIS_NAME_GAP = 30;
  /** 纵轴标题离轴线的距离区间（按刻度文字宽度算，取它的上下限）。 */
  const Y_AXIS_NAME_MIN_GAP = 24;
  const Y_AXIS_NAME_MAX_GAP = 76;
  /** 画布四边的留白（有轴标题时按标题让位）。 */
  const GRID_LEFT = 60;
  const GRID_RIGHT = 20;
  const GRID_TOP = 24;
  const GRID_BOTTOM = 36;
  /** 有横轴标题时，下边距要给「刻度 + 标题」留出位置。 */
  const GRID_BOTTOM_WITH_X_NAME = 64;
  /** 纵轴标题（竖排）离画布左缘的净空。 */
  const Y_AXIS_NAME_LEFT_CLEARANCE = 30;

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

  // ---------------------------------------------------------------- 数值范围

  /**
   * 上界取整到「好看的」数（1 / 1.5 / 2 / 2.5 / 3 / 4 / 5 / 6 / 8 / 10 × 10ⁿ）。
   *
   * 自己实现，不引第三方库：取比 value 大的最小「好看数」。
   */
  function niceCeil(value) {
    if (!Number.isFinite(value) || value <= 0) return value;
    const steps = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    const exp = Math.floor(Math.log10(value));
    const base = Math.pow(10, exp);
    const norm = value / base;
    for (const step of steps) {
      if (norm <= step + 1e-9) return step * base;
    }
    return 10 * base;
  }

  /** 下界取整到「好看的」数（取比 value 小的最大「好看数」）。 */
  function niceFloor(value) {
    if (!Number.isFinite(value) || value <= 0) return value;
    const steps = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    const exp = Math.floor(Math.log10(value));
    const base = Math.pow(10, exp);
    const norm = value / base;
    let picked = steps[0];
    for (const step of steps) {
      if (norm >= step - 1e-9) picked = step;
      else break;
    }
    return picked * base;
  }

  /**
   * 载荷里的数据值范围。
   *
   * 堆叠图型（STACKED_BAR）按**同一 x 位置上的堆叠总值**算 —— 不是单系列最大值。
   */
  function dataValueRange(payload, kind) {
    const series = Array.isArray(payload?.series) && payload.series.length
      ? payload.series
      : null;
    const stacked = String(kind || "").toUpperCase() === "STACKED_BAR";
    let min = null;
    let max = null;
    const take = (value) => {
      const num = Number(value);
      if (!Number.isFinite(num)) return;
      if (min === null || num < min) min = num;
      if (max === null || num > max) max = num;
    };
    if (series) {
      if (stacked) {
        const lengths = series.map((item) =>
          Array.isArray(item?.yValues) ? item.yValues.length : 0
        );
        const count = lengths.reduce((acc, len) => (len > acc ? len : acc), 0);
        for (let index = 0; index < count; index += 1) {
          let sum = 0;
          let any = false;
          for (const item of series) {
            const values = Array.isArray(item?.yValues) ? item.yValues : [];
            const num = Number(values[index]);
            if (Number.isFinite(num)) {
              sum += num;
              any = true;
            }
          }
          if (any) take(sum);
        }
      } else {
        for (const item of series) {
          const values = Array.isArray(item?.yValues) ? item.yValues : [];
          for (const value of values) take(value);
        }
      }
      return { min, max };
    }
    const values = Array.isArray(payload?.yValues) ? payload.yValues : [];
    for (const value of values) take(value);
    return { min, max };
  }

  /** 载荷里的有效参考线（数值能当数用的才算）。 */
  function referenceLineValues(payload) {
    const lines = isObject(payload?.referenceLines) ? payload.referenceLines : {};
    const result = [];
    for (const name of Object.keys(lines)) {
      const expr = lines[name];
      const value = Number(isObject(expr) ? expr.value : NaN);
      if (!Number.isFinite(value)) continue;
      result.push({ name: String(name), value });
    }
    return result;
  }

  /**
   * 数值轴的范围计划。
   *
   * - 用户**显式**给了 minimum / maximum → 尊重用户的值，**不静默改写**；
   * - 用户没给的那一边 → 由「数据值 + 全部有效参考线值」算出（含边距、取整到好看的数），
   *   参考线因此不会跑到可视范围之外；
   * - 参考线落在用户固定的范围之外 → 记一条提示（图下的说明行里写出来）。
   */
  function planValueRange(payload, kind, ySpec) {
    const spec = isObject(ySpec) ? ySpec : {};
    const userMin =
      typeof spec.minimum === "number" && isFinite(spec.minimum)
        ? spec.minimum
        : null;
    const userMax =
      typeof spec.maximum === "number" && isFinite(spec.maximum)
        ? spec.maximum
        : null;
    const references = referenceLineValues(payload);
    const data = dataValueRange(payload, kind);

    let min = null;
    let max = null;
    if (userMin === null) {
      let low = data.min;
      for (const ref of references) {
        if (low === null || ref.value < low) low = ref.value;
      }
      // 只有参考线把下界拽出数据范围时才动它（否则交给 ECharts 自动算）。
      if (low !== null && (data.min === null || low < data.min)) {
        min = niceFloor(low * 0.95);
      }
    }
    if (userMax === null) {
      let high = data.max;
      for (const ref of references) {
        if (high === null || ref.value > high) high = ref.value;
      }
      if (high !== null && (data.max === null || high > data.max)) {
        max = niceCeil(high * 1.05);
      }
    }

    const effectiveMin = userMin !== null ? userMin : min !== null ? min : data.min;
    const effectiveMax = userMax !== null ? userMax : max !== null ? max : data.max;

    const warnings = [];
    for (const ref of references) {
      if (effectiveMin !== null && ref.value < effectiveMin) {
        warnings.push({ name: ref.name, value: ref.value, side: "below" });
      } else if (effectiveMax !== null && ref.value > effectiveMax) {
        warnings.push({ name: ref.name, value: ref.value, side: "above" });
      }
    }

    return {
      min,
      max,
      userMin,
      userMax,
      effectiveMin,
      effectiveMax,
      references,
      warnings,
    };
  }

  // ---------------------------------------------------------------- 轴标题排版

  /** 估一段文字的像素宽（CJK 约 1 字宽，数字 / 字母约 0.62 字宽）。 */
  function estimateTextWidth(text, fontSize) {
    const size = Number(fontSize) || AXIS_LABEL_FONT_SIZE;
    let width = 0;
    for (const ch of String(text == null ? "" : text)) {
      width += /[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]/.test(ch)
        ? size
        : size * 0.62;
    }
    return width;
  }

  /** 纵轴刻度里最宽的一格大概多宽（用来给竖排标题让出位置）。 */
  function estimateValueLabelWidth(payload, kind, ySpec, unit) {
    const spec = isObject(ySpec) ? ySpec : {};
    const range = dataValueRange(payload, kind);
    let width = 0;
    for (const value of [range.min, range.max]) {
      if (value === null || !Number.isFinite(value)) continue;
      const text = formatByFormat(value, spec.format, unit || "");
      width = Math.max(width, estimateTextWidth(text, AXIS_LABEL_FONT_SIZE));
    }
    return width + 8; // 刻度进位后可能多一两位，留一点余量。
  }

  /**
   * 轴标题 + 画布留白的排版计划。
   *
   * 口径（测试按这个口径断言）：
   * - 横轴标题：**居中**放在刻度下面（nameLocation="middle"），不再落在轴的末端；
   * - 纵轴标题：**竖排居中**放在刻度左边（nameLocation="middle" + rotate 90），
   *   左边距按刻度文字宽度让位，保证不压住刻度；
   * - 画布留白：grid.bottom 给横轴标题留位置，grid.left 给纵轴标题留位置。
   */
  function planAxisLayout(payload, kind, axes, unit) {
    const specs = isObject(axes) ? axes : {};
    const xSpec = isObject(specs.x) ? specs.x : {};
    const ySpec = isObject(specs.y) ? specs.y : {};
    const xTitle = xSpec.title == null ? "" : String(xSpec.title);
    const yTitle = ySpec.title == null ? "" : String(ySpec.title);
    const labelWidth = estimateValueLabelWidth(payload, kind, ySpec, unit);
    const yNameGap = Math.min(
      Y_AXIS_NAME_MAX_GAP,
      Math.max(Y_AXIS_NAME_MIN_GAP, Math.round(labelWidth + 20))
    );
    return {
      xName: xTitle,
      yName: yTitle,
      yNameGap,
      grid: {
        left: yTitle
          ? Math.max(GRID_LEFT, yNameGap + Y_AXIS_NAME_LEFT_CLEARANCE)
          : GRID_LEFT,
        right: GRID_RIGHT,
        top: GRID_TOP,
        bottom: xTitle ? GRID_BOTTOM_WITH_X_NAME : GRID_BOTTOM,
      },
    };
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
      out.nameTextStyle = {
        fontSize: AXIS_NAME_FONT_SIZE,
        color: opts.nameColor || "#64748b",
        align: "center",
      };
      // 轴标题的**画布内**排版：
      //   - 横轴：ECharts 默认把轴名落在轴的**末端（最右）** → 被画布右缘裁掉；
      //     改成**居中**放在刻度下面（nameGap 决定它离刻度多远）；
      //   - 纵轴：默认横排在轴的顶端 → 长标题会顶出画布左缘；
      //     改成**竖排居中**放在刻度左边（nameGap 按刻度宽度让位）。
      if (opts.axisDirection === "x") {
        out.nameLocation = "middle";
        out.nameGap = X_AXIS_NAME_GAP;
      } else if (opts.axisDirection === "y") {
        out.nameLocation = "middle";
        out.nameGap = opts.nameGap || Y_AXIS_NAME_MIN_GAP;
        out.nameRotate = 90;
      }
    }
    if (typeof source.minimum === "number" && isFinite(source.minimum)) {
      out.min = source.minimum;
    } else if (Number.isFinite(opts.autoMin)) {
      // 用户**没有**固定下界 → 用算出来的（盖住参考线）。
      out.min = opts.autoMin;
    }
    if (typeof source.maximum === "number" && isFinite(source.maximum)) {
      out.max = source.maximum;
    } else if (Number.isFinite(opts.autoMax)) {
      // 用户**没有**固定上界 → 用算出来的（盖住参考线）。
      out.max = opts.autoMax;
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
          // 名称画在线的**内侧末端**（默认落在轴的外侧 → 被画布右缘裁掉）。
          position: "insideEndTop",
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

  /**
   * 参考线落在用户固定的纵轴范围之外 → 在图下写一句提示。
   *
   * 用的是既有的 ``wch-note`` 样式（不另起一套 CSS）。
   * 提示里说清：参考线**名称**、**数值**、以及它在当前纵轴范围的**外面**。
   */
  function appendRangeNote(container, warnings, unit, range) {
    const list = Array.isArray(warnings) ? warnings : [];
    if (!list.length) return;
    const suffix = unit ? " " + unit : "";
    const bound = (value) => (value === null || value === undefined ? "不限" : String(value));
    const scope = `当前纵轴范围 ${bound(range?.effectiveMin)} ~ ${bound(range?.effectiveMax)}`;
    const parts = list.map((item) => {
      const side = item.side === "above" ? "高于" : "低于";
      return `参考线「${item.name}」（${item.value}${suffix}）${side}${scope}`;
    });
    const note = document.createElement("div");
    note.className = "wch-note";
    note.textContent = `注意：${parts.join("；")}，图上看不到这条参考线。`;
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
        let rangePlan = null;
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
          // 轴标题的排版 + 画布留白（都在画布内部解决）。
          const layout = planAxisLayout(payload, kind, axes, unit);
          // 数值轴的范围：数据 + 参考线（用户固定过的边界不动）。
          rangePlan = planValueRange(payload, kind, ySpec);
          option.grid = layout.grid;
          option.xAxis = axisOption(xSpec, "category", {
            unit,
            labelColor: themeColor,
            axisDirection: "x",
            useServerLabels:
              !!isObject(xSpec) &&
              !!isObject(xSpec.format) &&
              Array.isArray(payload.displayXLabels) &&
              payload.displayXLabels.length > 0,
          });
          // **类别轴的标签**：ECharts 不给 ``data`` 就画序号（0、1、2…），
          // 于是「横轴用设备名称」在图上还是一串数字（验收 11-01）。
          // 载荷已经算好类别名（``displayXLabels`` / ``xLabels``），
          // 这里原样交给 ECharts —— 图上显示的必须是载荷里的类别。
          if (option.xAxis && option.xAxis.type === "category" && displayLabels.length) {
            option.xAxis.data = displayLabels.slice();
          }
          option.yAxis = axisOption(ySpec, "value", {
            unit,
            labelColor: themeColor,
            nameColor: themeColor || "#64748b",
            axisDirection: "y",
            nameGap: layout.yNameGap,
            autoMin: rangePlan.min,
            autoMax: rangePlan.max,
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
        if (rangePlan) {
          appendRangeNote(container, rangePlan.warnings, unit, rangePlan);
        }
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
