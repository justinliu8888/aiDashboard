// 前端「报表展示排版」独立模块（契约 0002 §5，P3 拥有）。
//
// 这个文件刻意做成不依赖主工作台的独立模块：
//   - 浏览器：window.WorkbenchPresentation（由协调者接入 workbench.html）
//   - Node 测试：require("../pages/workbench_presentation.js")，用 node --test 跑
//
// 它只负责四件事：
//   1. 按**定义树**（容器 / 叶子）把报表排出来；
//   2. 把白名单样式合并成受控的 inline style；
//   3. 消费服务端下发的主题（resolvedTheme.tokens + defaultStyle），
//      只改**外观**，绝不改排版与格式；
//   4. 观察容器尺寸变化，通知上层去 resize 图表。
//
// 另外：区域上有 errors 时打错误态（红框 + 提示文案），**区域照常渲染**。
//
// 它**不**负责取数、发请求、改会话状态、造数，也不碰主工作台已有的菜单与状态。
//
// ── 定位 / 补位 / 窄屏（契约 0002 §3.3、§5.2）────────────────────
//   - 容器与叶子**统一**应用合法定位：组也用自己的 layout.placement
//     （GRID / FLOW / ABSOLUTE）＋ component.arrangement；
//   - 隐藏只跳过节点，但**后续子项会前移**（布局重排，不留空位）；
//     「显示隐藏内容」打开时按原顺序插回半透明卡（临时让位，仍是隐藏态）；
//   - 容器宽度太窄时**减列**：定义里有显式窄屏配置就按配置走，
//     没有就用默认断点 480px（纯视图状态，**不写报表定义**）。
//
// ── 视图对象 view ──────────────────────────────────────────────
//   definition      权威定义（**树由它决定**，不再由扁平载荷顺序决定）
//   previewPayload  数据来源，按 regionId 关联叶子
//   resolvedTheme   服务端解析好的主题（{themeId, themeRevision, tokens, defaultStyle}）
//   showHidden      是否显示隐藏内容（开关**不写定义**）
//
// ── 接线选项 options ───────────────────────────────────────────
//   renderLeaf(leafRegion, payloadRegion, ctx)  P0 注入的既有叶子渲染
//   onResize({root, element})                   容器尺寸变化后的回调（去抖 120ms）
//   totalsByUnit                                 P0 已算好的总量表（不给就本模块自己算）
//   renderGroupTitle                             是否给容器渲染组标题（默认 false）
//
// 本文件不使用 innerHTML 写入内容、不使用 insertAdjacentHTML、不执行 eval。
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.WorkbenchPresentation = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // ---------------------------------------------------------------- 常量

  /** 模块名（与契约 §5.1 一致）。 */
  const MODULE_NAME = "workbench-presentation";
  /** 容器上挂的标记属性，便于上层和测试定位自己的内容。 */
  const MODULE_ATTR = "data-presentation";
  /** 渲染状态挂在宿主容器上的键名（clear 时读出来断开）。 */
  const STATE_KEY = "__workbenchPresentationState";
  /** 尺寸变化回调的去抖间隔（毫秒）。 */
  const RESIZE_DEBOUNCE_MS = 120;
  /** 根栅格默认列数（页面坐标栅格）。 */
  const DEFAULT_ROOT_COLUMNS = 12;
  /** 根栅格默认间距（像素）。 */
  const DEFAULT_ROOT_GAP = 16;
  /** 隐藏内容的不透明度。 */
  const HIDDEN_OPACITY = "0.45";

  /**
   * 默认的**容器宽度**断点（像素）。
   *
   * 定义里没有显式窄屏配置时用它（契约 §3.1 的 SET_PAGE 目前只开放
   * webMaxWidth 与 arrangement，没有窄屏字段，所以现状走这个默认值）。
   */
  const DEFAULT_NARROW_BREAKPOINT_PX = 480;
  /** 默认窄屏列数：窄屏时多列组收成几列。 */
  const DEFAULT_NARROW_COLUMNS = 1;

  /**
   * 兜底排版：容器没声明 arrangement、或声明不合法时用这个。
   *
   * 单列竖排 = 改造前的观感（不丢卡、不猜内容）。
   */
  const NEUTRAL_ARRANGEMENT = { mode: "GRID", columns: 1, direction: "HORIZONTAL", gap: 0 };

  /** 契约 §3.4 的样式键白名单：不在这里的一律不进最终 style。 */
  const STYLE_KEYS = [
    "fontFamily",
    "fontSize",
    "fontWeight",
    "textColor",
    "backgroundColor",
    "textAlign",
    "verticalAlign",
    "lineHeight",
    "padding",
    "margin",
    "border",
    "borderRadius",
    "opacity",
  ];

  /** 明确禁止出现在样式值里的东西（契约 §3.4「拒绝」）。 */
  const UNSAFE_VALUE_RE = /[<>]|url\s*\(|expression\s*\(|javascript:/i;

  /**
   * 主题 token 白名单（契约 §8.1「tokens（白名单）」）。
   *
   * 与后端 `report_theme_service.THEME_TOKEN_KEYS` 一一对应：前端**只消费**
   * 服务端解析好的主题，不自己注册主题，也不扩充键名。
   */
  const THEME_TOKEN_KEYS = [
    "colorBackground",
    "colorSurface",
    "colorText",
    "colorTextMuted",
    "colorPrimary",
    "colorBorder",
    "colorAccent",
    "fontFamily",
    "fontSizeBase",
    "lineHeightBase",
    "radiusBase",
    "gapBase",
    "spacingUnit",
  ];

  /** 错误态边框颜色（红）。 */
  const ERROR_BORDER_COLOR = "#dc2626";
  /** 主题只给了边框颜色时，边框宽度取这个值（像素）。 */
  const THEME_TOKEN_BORDER_WIDTH = 1;

  /**
   * 颜色值的紧白名单：只放行十六进制、具名色、rgb()/rgba()/hsl()/hsla()。
   *
   * 目的不是「支持更多颜色」，是**堵住** "red;position:fixed" 这类把
   * 分号后面的东西一起塞进 CSS 的写法。
   */
  const COLOR_VALUE_RE =
    /^(#[0-9a-fA-F]{3,8}|[a-zA-Z]{1,32}|rgba?\(\s*[0-9.%\s,/]+\)|hsla?\(\s*[0-9.%\s,/deg]+\)|transparent|currentColor)$/;

  /** 文本对齐枚举 → CSS 值。 */
  const TEXT_ALIGN_CSS = { LEFT: "left", CENTER: "center", RIGHT: "right" };
  /** 垂直对齐枚举 → CSS 值。 */
  const VERTICAL_ALIGN_CSS = { TOP: "top", MIDDLE: "middle", BOTTOM: "bottom" };
  /** 流式排版对齐枚举 → CSS 值。 */
  const FLOW_ALIGN_CSS = {
    START: "flex-start",
    CENTER: "center",
    END: "flex-end",
    STRETCH: "stretch",
  };
  /** 边框线型枚举 → CSS 值。 */
  const BORDER_STYLE_CSS = {
    SOLID: "solid",
    DASHED: "dashed",
    DOTTED: "dotted",
    NONE: "none",
  };

  // ---------------------------------------------------------------- 小工具

  function isPlainObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }

  function ownString(value) {
    if (typeof value === "string") return value;
    if (typeof value === "number" && isFinite(value)) return String(value);
    return "";
  }

  /** 有限整数，否则 null。 */
  function intOrNull(value) {
    const num = Number(value);
    return Number.isInteger(num) ? num : null;
  }

  /** 有限数字，否则 null。 */
  function numberOrNull(value) {
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
  }

  /** 夹在 [min, max] 之间的整数，非法返回 null。 */
  function clampInt(value, min, max) {
    const num = Number(value);
    if (!Number.isInteger(num)) return null;
    if (num < min || num > max) return null;
    return num;
  }

  /** 取元素所属的 document（宿主优先）。 */
  function docOf(node) {
    if (node && node.ownerDocument) return node.ownerDocument;
    if (typeof document !== "undefined") return document;
    return null;
  }

  /** 取元素所属的 window（宿主优先）。 */
  function winOf(node) {
    const doc = docOf(node);
    if (doc && doc.defaultView) return doc.defaultView;
    if (typeof window !== "undefined") return window;
    return null;
  }

  /**
   * 删属性（宿主没有 removeAttribute 时静默跳过）。
   *
   * 本模块以前只写属性、不删属性；现在窄屏状态要来回切换，需要删。
   */
  function removeAttr(element, name) {
    if (!element || typeof element.removeAttribute !== "function") return;
    try {
      element.removeAttribute(name);
    } catch (error) {
      /* 删不掉就算了：属性多留一个不影响排版 */
    }
  }

  /** 容器区域判定：component.type === "CONTAINER"。 */
  function isContainerRegion(region) {
    if (!isPlainObject(region)) return false;
    const component = isPlainObject(region.component) ? region.component : null;
    if (!component) return false;
    return String(component.type || "").toUpperCase() === "CONTAINER";
  }

  // ---------------------------------------------------------------- 排版（arrangement）

  /**
   * 归一 arrangement：{mode, columns, direction, gap}。
   *
   * 只认白名单取值：mode ∈ {GRID, FLOW}、columns ∈ 1..12、direction ∈
   * {HORIZONTAL, VERTICAL}、gap 0..96 像素。越界的一律**回落**到兜底值，
   * 不猜、不报错 —— 排版坏了最多丑一点，**不能丢卡**。
   */
  function normalizeArrangement(raw, fallback) {
    const base = isPlainObject(fallback) ? fallback : NEUTRAL_ARRANGEMENT;
    if (!isPlainObject(raw)) return { ...base };
    const mode = String(raw.mode || "").toUpperCase();
    if (mode !== "GRID" && mode !== "FLOW") return { ...base };
    const columns = clampInt(raw.columns, 1, 12);
    const gap = resolveGap(raw.gap);
    const direction = String(raw.direction || "").toUpperCase() === "VERTICAL" ? "VERTICAL" : "HORIZONTAL";
    return {
      mode,
      columns: columns === null ? base.columns : columns,
      direction,
      gap: gap === null ? base.gap : gap,
    };
  }

  /** gap：{value, unit:"PX"} → 像素整数；非法返回 null。 */
  function resolveGap(raw) {
    if (!isPlainObject(raw)) return null;
    const unit = String(raw.unit || "PX").toUpperCase();
    if (unit !== "PX") return null;
    const value = clampInt(raw.value, 0, 96);
    return value;
  }

  /**
   * resolveArrangement(region) -> {mode, columns, direction, gap} | null
   *
   * **容器区域**返回归一后的排版；**非容器**返回 null。
   * 容器没声明 arrangement（或声明不合法）→ 回落单列竖排，不丢子卡。
   */
  function resolveArrangement(region) {
    if (!isContainerRegion(region)) return null;
    const component = isPlainObject(region.component) ? region.component : {};
    return normalizeArrangement(component.arrangement, NEUTRAL_ARRANGEMENT);
  }

  // ---------------------------------------------------------------- 位置（placement）

  /**
   * resolvePlacement(region) -> 归一后的 placement | null
   *
   * 只认 GRID / FLOW / ABSOLUTE 三种；其它一律 null（当作没声明位置）。
   */
  function resolvePlacement(region) {
    if (!isPlainObject(region)) return null;
    const layout = isPlainObject(region.layout) ? region.layout : {};
    const raw = isPlainObject(layout.placement) ? layout.placement : null;
    if (!raw) return null;
    const type = String(raw.type || "").toUpperCase();
    if (type === "GRID") {
      return {
        type: "GRID",
        column: clampInt(raw.column, 1, 12),
        columnSpan: clampInt(raw.columnSpan, 1, 12),
        row: clampInt(raw.row, 1, 96),
        rowSpan: clampInt(raw.rowSpan, 1, 12),
        minHeight: numberOrNull(raw.minHeight),
      };
    }
    if (type === "FLOW") {
      const align = String(raw.align || "").toUpperCase();
      return {
        type: "FLOW",
        align: FLOW_ALIGN_CSS[align] ? align : null,
        width: resolveLength(raw.width),
        minHeight: numberOrNull(raw.minHeight),
      };
    }
    if (type === "ABSOLUTE") {
      return {
        type: "ABSOLUTE",
        x: numberOrNull(raw.x),
        y: numberOrNull(raw.y),
        width: numberOrNull(raw.width),
        height: numberOrNull(raw.height),
        zIndex: numberOrNull(raw.zIndex),
      };
    }
    return null;
  }

  /** 长度：{value, unit:"PX"|"PERCENT"} → {value, unit}；非法返回 null。 */
  function resolveLength(raw) {
    if (!isPlainObject(raw)) return null;
    const unit = String(raw.unit || "PX").toUpperCase();
    const value = numberOrNull(raw.value);
    if (value === null) return null;
    if (unit === "PX") return { value, unit: "PX" };
    if (unit === "PERCENT") return { value, unit: "PERCENT" };
    return null;
  }

  // ---------------------------------------------------------------- 网格占位与补位

  /**
   * gridSlotOf(placement) -> {column, columnSpan, row, rowSpan} | null
   *
   * GRID 定位的实际占位；没有定位（或不是 GRID）返回 null。
   */
  function gridSlotOf(placement) {
    if (!placement || placement.type !== "GRID") return null;
    if (placement.column === null || placement.column === undefined) return null;
    return {
      column: placement.column,
      columnSpan:
        placement.columnSpan === null || placement.columnSpan === undefined ? 1 : placement.columnSpan,
      row: placement.row === null || placement.row === undefined ? null : placement.row,
      rowSpan: placement.rowSpan === null || placement.rowSpan === undefined ? 1 : placement.rowSpan,
    };
  }

  /**
   * 这个占位是不是「按容器排列自动推导出来的」。
   *
   * 判定口径与后端 `report_presentation_service._auto_placements` 一致：
   * 第 i 个子项 → 列 `i % 列数 + 1`、行 `i // 列数 + 1`、跨度 1。
   *
   * 不满足的算**用户自定义定位**：补位 / 减列时**不动它**
   * （补位是为了消除隐藏留下的空位，不是推翻用户自己的布局意图）。
   */
  function isAutoDerivedSlot(slot, originalIndex, declaredColumns) {
    if (!slot) return false;
    const cols = declaredColumns && declaredColumns > 0 ? declaredColumns : 1;
    if (slot.columnSpan !== 1) return false;
    if (slot.column !== originalIndex % cols + 1) return false;
    if (slot.row !== null && slot.row !== Math.floor(originalIndex / cols) + 1) return false;
    return true;
  }

  /**
   * packGridSlots(items, columns) -> 新的占位数组
   *
   * 把**已渲染**的子项按列数重新排一遍（补位与减列共用）。
   *
   * items：[{slot, auto}]；slot 为 null 的子项（没有明确列）不参与排布，
   * 交给浏览器自动摆。自定义定位（auto=false）保持原位，但会占掉光标，
   * 后面的自动项不会压到它身上。
   */
  function packGridSlots(items, columns) {
    const cols = columns && columns > 0 ? columns : 1;
    const list = Array.isArray(items) ? items : [];
    const out = new Array(list.length);
    let col = 1;
    let row = 1;
    for (let i = 0; i < list.length; i += 1) {
      const item = list[i];
      const slot = item && item.slot ? item.slot : null;
      if (!slot) {
        out[i] = null;
        continue;
      }
      if (!item.auto) {
        // 自定义定位：原样保留，光标跳过它占掉的位置。
        out[i] = slot;
        if (slot.row !== null && slot.row > row) {
          row = slot.row;
          col = slot.column;
        }
        col = slot.column + slot.columnSpan;
        while (col > cols) {
          col -= cols;
          row += 1;
        }
        continue;
      }
      out[i] = { column: col, row, columnSpan: slot.columnSpan, rowSpan: slot.rowSpan };
      col += slot.columnSpan;
      while (col > cols) {
        col -= cols;
        row += 1;
      }
    }
    return out;
  }

  /** 用新占位覆盖 GRID 定位（只改占位相关的字段）。 */
  function placementWithSlot(placement, slot) {
    if (!placement || placement.type !== "GRID" || !slot) return placement;
    return {
      ...placement,
      column: slot.column,
      columnSpan: slot.columnSpan,
      row: slot.row,
      rowSpan: slot.rowSpan,
    };
  }

  /** 把新占位就地写到元素上（不重建 DOM）。 */
  function updateGridSlot(element, slot) {
    if (!element || !slot) return;
    element.setAttribute("data-grid-column", String(slot.column));
    element.setAttribute("data-grid-column-span", String(slot.columnSpan));
    element.style.gridColumn = slot.column + " / span " + slot.columnSpan;
    if (slot.row !== null) {
      element.setAttribute("data-grid-row", String(slot.row));
      element.style.gridRow = slot.row + " / span " + slot.rowSpan;
    } else {
      removeAttr(element, "data-grid-row");
      element.style.gridRow = "";
    }
  }

  // ---------------------------------------------------------------- 窄屏（容器宽度断点）

  /**
   * 断点宽度：数字，或协议里的长度 {value, unit:"PX"} → 像素；非法返回 null。
   */
  function resolveBreakpointWidth(raw) {
    if (typeof raw === "number") {
      return isFinite(raw) && raw > 0 ? raw : null;
    }
    if (isPlainObject(raw)) {
      const unit = String(raw.unit || "PX").toUpperCase();
      const value = numberOrNull(raw.value);
      if (unit === "PX" && value !== null && value > 0) return value;
    }
    return null;
  }

  /**
   * 解析显式窄屏配置。
   *
   * 形状：`[{ maxWidth: 480 | {value, unit:"PX"}, columns: 1 }, ...]`
   * （也接受单个对象）。非法条目直接丢弃 —— 排版坏了最多丑一点。
   */
  function parseBreakpointEntries(raw) {
    const list = Array.isArray(raw) ? raw : isPlainObject(raw) ? [raw] : [];
    const out = [];
    for (const item of list) {
      if (!isPlainObject(item)) continue;
      const maxWidth = resolveBreakpointWidth(item.maxWidth);
      if (maxWidth === null) continue;
      const columns = clampInt(item.columns, 1, 12);
      if (columns === null) continue;
      out.push({ maxWidth, columns });
    }
    out.sort((a, b) => a.maxWidth - b.maxWidth);
    return out;
  }

  /**
   * resolveNarrowConfig(definition, options) -> {entries, source}
   *
   * **显式配置优先**：
   *   1. options.breakpoints —— P0 / 测试注入；
   *   2. definition.page.breakpoints —— 定义里的显式窄屏配置；
   *   3. 默认：480px → 1 列。
   *
   * source 会写进 DOM（data-narrow-source），便于核实用的是哪一路。
   */
  function resolveNarrowConfig(definition, options) {
    const opts = isPlainObject(options) ? options : {};
    if (opts.breakpoints !== undefined && opts.breakpoints !== null) {
      const entries = parseBreakpointEntries(opts.breakpoints);
      if (entries.length) return { entries, source: "options" };
    }
    const def = isPlainObject(definition) ? definition : {};
    const page = isPlainObject(def.page) ? def.page : {};
    if (page.breakpoints !== undefined && page.breakpoints !== null) {
      const entries = parseBreakpointEntries(page.breakpoints);
      if (entries.length) return { entries, source: "definition" };
    }
    return {
      entries: [{ maxWidth: DEFAULT_NARROW_BREAKPOINT_PX, columns: DEFAULT_NARROW_COLUMNS }],
      source: "default",
    };
  }

  /**
   * 按容器宽度挑断点：取「仍覆盖这个宽度」的最紧一条。
   *
   * width 为 null（量不到容器宽度）→ null（不进入窄屏）。
   */
  function narrowEntryFor(width, entries) {
    if (width === null || width === undefined) return null;
    let hit = null;
    for (const entry of entries) {
      if (!entry || width > entry.maxWidth) continue;
      if (hit === null || entry.maxWidth < hit.maxWidth) hit = entry;
    }
    return hit;
  }

  /** 窄屏状态：{active, columns, breakpointPx, source, measured}。 */
  function narrowStateFor(config, width) {
    const entries = config && Array.isArray(config.entries) ? config.entries : [];
    const entry = narrowEntryFor(width, entries);
    return {
      active: !!entry,
      columns: entry ? entry.columns : null,
      breakpointPx: entry ? entry.maxWidth : DEFAULT_NARROW_BREAKPOINT_PX,
      source: config && config.source ? config.source : "default",
      measured: width !== null && width !== undefined,
    };
  }

  /** 量容器宽度（像素）：量不到返回 null。 */
  function measureWidth(element) {
    if (!element) return null;
    if (typeof element.clientWidth === "number" && element.clientWidth > 0) {
      return element.clientWidth;
    }
    if (typeof element.getBoundingClientRect === "function") {
      const rect = element.getBoundingClientRect();
      if (rect && typeof rect.width === "number" && rect.width > 0) return rect.width;
    }
    return null;
  }

  /** 窄屏下的生效列数：不超过声明列数；不窄屏就是声明列数。 */
  function narrowColumnsOf(arrangement, narrow) {
    if (!arrangement || arrangement.mode !== "GRID") {
      return arrangement && arrangement.columns ? arrangement.columns : 1;
    }
    if (!narrow || !narrow.active || narrow.columns === null) return arrangement.columns;
    return Math.min(arrangement.columns, narrow.columns);
  }

  // ---------------------------------------------------------------- 样式

  /**
   * 值里出现 <、>、url(、expression(、javascript: → 一律丢弃。
   */
  function hasUnsafeText(value) {
    return UNSAFE_VALUE_RE.test(String(value === undefined || value === null ? "" : value));
  }

  /** 颜色值校验（紧白名单）。 */
  function sanitizeColor(value) {
    if (typeof value !== "string") return undefined;
    const text = value.trim();
    if (!text) return undefined;
    if (hasUnsafeText(text)) return undefined;
    if (!COLOR_VALUE_RE.test(text)) return undefined;
    return text;
  }

  /** 长度值校验：{value, unit}。 */
  function sanitizeLength(value, { allowNegative = false, max = 2000 } = {}) {
    const length = resolveLength(value);
    if (!length) return undefined;
    if (length.unit !== "PX") return undefined;
    const num = length.value;
    if (!Number.isFinite(num)) return undefined;
    if (num < (allowNegative ? -max : 0) || num > max) return undefined;
    return { value: num, unit: "PX" };
  }

  /**
   * 归一一个样式键的值。
   *
   * 返回 undefined = 这个值不合法 → **丢弃这次覆盖**（下层样式继续生效）。
   */
  function sanitizeStyleValue(key, value) {
    switch (key) {
      case "fontFamily": {
        if (typeof value !== "string") return undefined;
        const text = value.trim();
        if (!text || hasUnsafeText(text)) return undefined;
        if (/[;{}]/.test(text)) return undefined;
        return text;
      }
      case "fontSize": {
        const length = sanitizeLength(value, { max: 400 });
        return length;
      }
      case "fontWeight": {
        const weight = clampInt(value, 100, 900);
        return weight === null ? undefined : weight;
      }
      case "textColor":
      case "backgroundColor": {
        return sanitizeColor(value);
      }
      case "textAlign": {
        const key2 = String(value || "").toUpperCase();
        return TEXT_ALIGN_CSS[key2] ? key2 : undefined;
      }
      case "verticalAlign": {
        const key2 = String(value || "").toUpperCase();
        return VERTICAL_ALIGN_CSS[key2] ? key2 : undefined;
      }
      case "lineHeight": {
        const length = resolveLength(value);
        if (!length) return undefined;
        if (length.unit === "PX") {
          if (length.value < 0 || length.value > 400) return undefined;
          return length;
        }
        // PERCENT：150 → 1.5；已经是比值的（1.5）就原样用。
        const ratio = length.value > 10 ? length.value / 100 : length.value;
        if (ratio <= 0 || ratio > 20) return undefined;
        return { value: ratio, unit: "PERCENT" };
      }
      case "padding": {
        return sanitizeLength(value, { max: 400 });
      }
      case "margin": {
        return sanitizeLength(value, { allowNegative: true, max: 400 });
      }
      case "border": {
        if (!isPlainObject(value)) return undefined;
        const width = clampInt(value.width, 0, 40);
        const styleKey = String(value.style || "SOLID").toUpperCase();
        const style = BORDER_STYLE_CSS[styleKey];
        const color = sanitizeColor(value.color);
        if (width === null || !style || color === undefined) return undefined;
        return { width, style: styleKey, color };
      }
      case "borderRadius": {
        const radius = clampInt(value, 0, 400);
        return radius === null ? undefined : radius;
      }
      case "opacity": {
        const opacity = numberOrNull(value);
        if (opacity === null || opacity < 0 || opacity > 1) return undefined;
        return opacity;
      }
      default:
        return undefined;
    }
  }

  /**
   * 把一层样式合并进目标对象。
   *
   * 只认白名单键；不合法的值**丢弃这次覆盖**（下层样式继续生效）。
   */
  function applyStyleInto(target, source) {
    if (!isPlainObject(source)) return;
    // styleRefs 里的项可能写成 {style:{...}}，也可能是直接的样式字典。
    const payload = isPlainObject(source.style) ? source.style : source;
    for (const key of STYLE_KEYS) {
      if (!Object.prototype.hasOwnProperty.call(payload, key)) continue;
      const value = payload[key];
      if (value === undefined || value === null) continue;
      const safe = sanitizeStyleValue(key, value);
      if (safe === undefined) continue; // 丢弃非法覆盖：保留下层样式
      target[key] = safe;
    }
  }

  /**
   * token 里的数值：接受数字或纯数字字符串；其它一律 null（= 非法，跳过）。
   */
  function tokenNumber(value) {
    if (typeof value === "number") return isFinite(value) ? value : null;
    if (typeof value === "string" && /^-?\d+(\.\d+)?$/.test(value.trim())) {
      return Number(value.trim());
    }
    return null;
  }

  /**
   * themeStyleFromTokens(tokens) -> 主题 **token 层**样式
   *
   * token → 样式键的映射（只映射到**外观**键，绝不碰排版）：
   *   colorSurface   → backgroundColor
   *   colorText      → textColor
   *   colorBorder    → border{width:1, style:"SOLID", color}
   *   fontFamily     → fontFamily
   *   fontSizeBase   → fontSize{value, unit:"PX"}
   *   lineHeightBase → lineHeight{value, unit:"PERCENT"}
   *   radiusBase     → borderRadius
   *
   * 故意**不**映射的：
   *   - colorBackground：页面底色，落在根容器（见 themePageStyle）；
   *   - colorTextMuted / colorPrimary / colorAccent：样式白名单里没有对应键；
   *   - gapBase / spacingUnit：属于**排版**，主题不许改间距。
   *
   * 非法取值**跳过**（不进最终 style），绝不把坏值塞进 CSS。
   */
  function themeStyleFromTokens(tokens) {
    const out = {};
    if (!isPlainObject(tokens)) return out;
    const put = (key, value) => {
      const safe = sanitizeStyleValue(key, value);
      if (safe !== undefined) out[key] = safe;
    };
    if (tokens.fontFamily !== undefined && tokens.fontFamily !== null) {
      put("fontFamily", tokens.fontFamily);
    }
    const fontSize = tokenNumber(tokens.fontSizeBase);
    if (fontSize !== null) put("fontSize", { value: fontSize, unit: "PX" });
    const lineHeight = tokenNumber(tokens.lineHeightBase);
    if (lineHeight !== null) put("lineHeight", { value: lineHeight, unit: "PERCENT" });
    const radius = tokenNumber(tokens.radiusBase);
    if (radius !== null) put("borderRadius", radius);
    const textColor = sanitizeColor(tokens.colorText);
    if (textColor !== undefined) out.textColor = textColor;
    const surface = sanitizeColor(tokens.colorSurface);
    if (surface !== undefined) out.backgroundColor = surface;
    const borderColor = sanitizeColor(tokens.colorBorder);
    if (borderColor !== undefined) {
      put("border", { width: THEME_TOKEN_BORDER_WIDTH, style: "SOLID", color: borderColor });
    }
    return out;
  }

  /**
   * themePageStyle(resolvedTheme) -> 根容器样式
   *
   * 只有 colorBackground 落到根容器（页面底色）；区域底色是 colorSurface。
   * 没有主题 / 主题里没有这个 token → 空对象（**完全不影响**现有渲染）。
   */
  function themePageStyle(resolvedTheme) {
    const out = {};
    if (!isPlainObject(resolvedTheme)) return out;
    const tokens = isPlainObject(resolvedTheme.tokens) ? resolvedTheme.tokens : null;
    if (!tokens) return out;
    const background = sanitizeColor(tokens.colorBackground);
    if (background !== undefined) out.backgroundColor = background;
    return out;
  }

  /**
   * 区域上的错误条目：{code, message, refRegionId?}。
   *
   * 缺 message 时用 code 兜底；code 与 message 全空的条目丢掉。
   */
  function regionErrors(region) {
    const raw = isPlainObject(region) ? region.errors : null;
    if (!Array.isArray(raw)) return [];
    const out = [];
    for (const item of raw) {
      if (!isPlainObject(item)) continue;
      const code = ownString(item.code).trim();
      const message = ownString(item.message).trim();
      if (!code && !message) continue;
      out.push({
        code,
        message: message || code,
        refRegionId: ownString(item.refRegionId).trim(),
      });
    }
    return out;
  }

  /**
   * 给区域打错误态：红框 + 可见提示文案。
   *
   * 区域**照常渲染、照常可交互**（页面可用、可保存错误草稿、可撤销），
   * 这里只是多一层标记与提示，不拦任何操作。
   *
   * 返回错误提示元素（没有错误返回 null，调用方决定插在哪）。
   */
  function applyErrorState(doc, element, errors) {
    if (!element || !Array.isArray(errors) || !errors.length) return null;
    const codes = [];
    for (const err of errors) {
      if (err && err.code && !codes.includes(err.code)) codes.push(err.code);
    }
    element.setAttribute("data-region-error", "1");
    if (codes.length) {
      element.setAttribute("data-region-error-code", codes[0]);
      element.setAttribute("data-region-error-codes", codes.join(","));
    }
    const cls = ownString(element.getAttribute("class")).trim();
    element.setAttribute("class", cls ? cls + " wp-has-error" : "wp-has-error");
    // 红框盖过主题 / 局部边框；border-box 保证不把内容挤动。
    element.style.border = "1px solid " + ERROR_BORDER_COLOR;
    element.style.boxSizing = "border-box";

    const box = doc.createElement("div");
    box.setAttribute("data-presentation-error", "1");
    box.setAttribute("class", "wp-error");
    box.setAttribute("data-region-error-count", String(errors.length));
    for (const err of errors) {
      const item = doc.createElement("div");
      item.setAttribute("class", "wp-error-item");
      if (err.code) item.setAttribute("data-region-error-code", err.code);
      if (err.refRegionId) item.setAttribute("data-region-error-ref-region-id", err.refRegionId);
      item.textContent = err.message;
      box.appendChild(item);
    }
    return box;
  }

  /**
   * mergeStyle(styleChain, resolvedTheme) -> 合并后的 style 对象
   *
   * 合并顺序（后者覆盖前者）：
   *   主题 token 层（resolvedTheme.tokens 映射出来的样式）
   *     → 主题默认（resolvedTheme.defaultStyle）
   *     → 祖先区域样式
   *     → 当前区域 styleRefs / style
   *     → 组件字段样式
   *
   * styleChain 是一个**按优先级从低到高**排好的数组（也可以直接给一个样式对象）。
   * 返回值只含白名单键，值已归一（{value, unit} 这类原始形状）。
   */
  function mergeStyle(styleChain, resolvedTheme) {
    const merged = {};
    const theme = isPlainObject(resolvedTheme) ? resolvedTheme : null;
    // 主题层：token 先铺底，defaultStyle 盖掉同名键。
    if (theme) {
      applyStyleInto(merged, themeStyleFromTokens(theme.tokens));
      applyStyleInto(merged, theme.defaultStyle);
    }
    const chain = Array.isArray(styleChain)
      ? styleChain
      : isPlainObject(styleChain)
        ? [styleChain]
        : [];
    for (const item of chain) applyStyleInto(merged, item);
    return merged;
  }

  /**
   * styleToCss(style) -> {camelCaseCSS属性: 值}
   *
   * 只做**映射**，不拼 cssText 文本；调用方逐属性写 element.style.xxx。
   */
  function styleToCss(style) {
    const css = {};
    if (!isPlainObject(style)) return css;
    for (const key of STYLE_KEYS) {
      if (!Object.prototype.hasOwnProperty.call(style, key)) continue;
      const value = style[key];
      switch (key) {
        case "fontFamily":
          if (typeof value === "string") css.fontFamily = value;
          break;
        case "fontSize":
          if (isPlainObject(value)) css.fontSize = value.value + "px";
          break;
        case "fontWeight":
          if (typeof value === "number") css.fontWeight = String(value);
          break;
        case "textColor":
          if (typeof value === "string") css.color = value;
          break;
        case "backgroundColor":
          if (typeof value === "string") css.backgroundColor = value;
          break;
        case "textAlign":
          if (TEXT_ALIGN_CSS[value]) css.textAlign = TEXT_ALIGN_CSS[value];
          break;
        case "verticalAlign":
          if (VERTICAL_ALIGN_CSS[value]) css.verticalAlign = VERTICAL_ALIGN_CSS[value];
          break;
        case "lineHeight":
          if (isPlainObject(value)) {
            css.lineHeight = value.unit === "PERCENT" ? String(value.value) : value.value + "px";
          }
          break;
        case "padding":
          if (isPlainObject(value)) css.padding = value.value + "px";
          break;
        case "margin":
          if (isPlainObject(value)) css.margin = value.value + "px";
          break;
        case "border":
          if (isPlainObject(value)) {
            const style2 = BORDER_STYLE_CSS[value.style] || "solid";
            css.border = value.width + "px " + style2 + " " + value.color;
          }
          break;
        case "borderRadius":
          if (typeof value === "number") css.borderRadius = value + "px";
          break;
        case "opacity":
          if (typeof value === "number") css.opacity = String(value);
          break;
        default:
          break;
      }
    }
    return css;
  }

  /** 把合并后的样式写到元素上（逐属性，不拼 cssText）。 */
  function applyStyle(element, style) {
    const css = styleToCss(style);
    for (const prop of Object.keys(css)) {
      try {
        element.style[prop] = css[prop];
      } catch (e) {
        /* 某些环境对未知属性会抛：跳过这一项，不影响其它样式 */
      }
    }
  }

  // ---------------------------------------------------------------- 区域树

  /**
   * buildTree(definition) -> 节点数组
   *
   * 节点 = region 字典 + 计算出来的 arrangement / placement / hidden /
   * isContainer / children（子节点数组，结构与顶层一致）。
   *
   * **树由 definition 决定**：没有 children 的旧平面定义会原样平铺，
   * 顺序与 definition.regions 一致。
   */
  function buildTree(definition) {
    const def = isPlainObject(definition) ? definition : {};
    const regions = Array.isArray(def.regions) ? def.regions : [];
    const nodes = [];
    for (const region of regions) {
      if (!isPlainObject(region)) continue;
      nodes.push(makeNode(region));
    }
    return nodes;
  }

  /** 建一个树节点（递归）。 */
  function makeNode(region) {
    const children = Array.isArray(region.children) ? region.children : [];
    const node = {
      ...region,
      id: ownString(region.id),
      name: ownString(region.name),
      region,
      arrangement: resolveArrangement(region),
      placement: resolvePlacement(region),
      hidden: region.hidden === true,
      isContainer: isContainerRegion(region),
      errors: regionErrors(region),
      children: [],
    };
    node.children = children.filter(isPlainObject).map(makeNode);
    return node;
  }

  /**
   * regionPath(definition, regionId) -> 树内路径（id 数组，根 → 目标）
   *
   * 找不到返回空数组。调试用。
   */
  function regionPath(definition, regionId) {
    const target = ownString(regionId);
    if (!target) return [];
    const def = isPlainObject(definition) ? definition : {};
    const regions = Array.isArray(def.regions) ? def.regions : [];

    const walk = (list, trail) => {
      for (const region of list) {
        if (!isPlainObject(region)) continue;
        const id = ownString(region.id);
        const next = trail.concat([id]);
        if (id === target) return next;
        const children = Array.isArray(region.children) ? region.children : [];
        const found = walk(children, next);
        if (found) return found;
      }
      return null;
    };

    return walk(regions, []) || [];
  }

  // ---------------------------------------------------------------- 载荷与总量

  /** 按 regionId 找载荷条目。 */
  function findPayloadRegion(payloadRegions, regionId) {
    const list = Array.isArray(payloadRegions) ? payloadRegions : [];
    for (const item of list) {
      if (isPlainObject(item) && item.regionId === regionId) return item;
    }
    return null;
  }

  /** 定义里标记为复制件的区域 id（契约 §2.2：复制件不参与总计翻倍）。 */
  function duplicateRegionIds(definition) {
    const out = new Set();
    const visit = (region) => {
      if (!isPlainObject(region)) return;
      const binding = isPlainObject(region.templateBinding) ? region.templateBinding : {};
      const spec = isPlainObject(binding.previewSpec) ? binding.previewSpec : {};
      const own = isPlainObject(region.previewSpec) ? region.previewSpec : {};
      if (spec.duplicateOfRegionId || own.duplicateOfRegionId) {
        const id = ownString(region.id);
        if (id) out.add(id);
      }
      const children = Array.isArray(region.children) ? region.children : [];
      for (const child of children) visit(child);
    };
    const def = isPlainObject(definition) ? definition : {};
    const regions = Array.isArray(def.regions) ? def.regions : [];
    for (const region of regions) visit(region);
    return out;
  }

  /**
   * buildTotalsByUnit(payloadRegions, excludedRegionIds) -> Map<单位, 数值>
   *
   * 与主工作台 buildTotalsByUnit 同一口径：
   *   只算 METRIC_CARD；没值（null / 空串）**不算 0**，直接不参与总量。
   */
  function buildTotalsByUnit(payloadRegions, excludedRegionIds) {
    const totals = new Map();
    const list = Array.isArray(payloadRegions) ? payloadRegions : [];
    const excluded = excludedRegionIds instanceof Set ? excludedRegionIds : new Set();
    for (const region of list) {
      if (!isPlainObject(region)) continue;
      if (String(region.componentType || "") !== "METRIC_CARD") continue;
      if (excluded.has(region.regionId)) continue;
      const payload = isPlainObject(region.payload) ? region.payload : {};
      const unit = ownString(payload.unit).trim();
      if (payload.value === null || payload.value === undefined || payload.value === "") continue;
      const value = Number(payload.value);
      if (!unit || !Number.isFinite(value)) continue;
      if (!totals.has(unit)) totals.set(unit, value);
    }
    return totals;
  }

  // ---------------------------------------------------------------- 渲染

  /** 节点的「区域样式」层：styleRefs + style。 */
  function nodeStyleSources(node) {
    const out = [];
    const region = isPlainObject(node) && isPlainObject(node.region) ? node.region : {};
    const refs = region.styleRefs;
    if (Array.isArray(refs)) {
      for (const ref of refs) if (isPlainObject(ref)) out.push(ref);
    } else if (isPlainObject(refs)) {
      out.push(refs);
    }
    if (isPlainObject(region.style)) out.push(region.style);
    return out;
  }

  /** 节点的「组件字段样式」层：component.style + component.styleHints[].style。 */
  function nodeComponentStyleSources(node) {
    const out = [];
    const region = isPlainObject(node) && isPlainObject(node.region) ? node.region : {};
    const component = isPlainObject(region.component) ? region.component : null;
    if (!component) return out;
    if (isPlainObject(component.style)) out.push(component.style);
    const hints = Array.isArray(component.styleHints) ? component.styleHints : [];
    for (const hint of hints) {
      if (isPlainObject(hint) && isPlainObject(hint.style)) out.push(hint.style);
    }
    return out;
  }

  /** 把排版写到容器元素上。 */
  function applyArrangementStyle(element, arrangement) {
    const arr = arrangement || NEUTRAL_ARRANGEMENT;
    if (arr.mode === "GRID") {
      element.style.display = "grid";
      element.style.gridTemplateColumns = "repeat(" + arr.columns + ", minmax(0, 1fr))";
      element.style.gap = arr.gap + "px";
      element.style.alignItems = "start";
      return;
    }
    element.style.display = "flex";
    element.style.flexDirection = arr.direction === "VERTICAL" ? "column" : "row";
    element.style.flexWrap = arr.direction === "VERTICAL" ? "nowrap" : "wrap";
    element.style.gap = arr.gap + "px";
    element.style.alignItems = "stretch";
  }

  /**
   * 把位置写到叶子外壳上。
   *
   * opts.fullWidthByDefault：根层（页面坐标栅格）没声明列时占满整行，
   * 这样**旧平面定义的观感不变**（改造前就是一张卡一行）。
   */
  function applyPlacementAttributes(element, placement, opts = {}) {
    if (!placement) return;
    element.setAttribute("data-placement-type", placement.type);
    if (placement.type === "GRID") {
      if (placement.column !== null && placement.column !== undefined) {
        element.setAttribute("data-grid-column", String(placement.column));
        const span = placement.columnSpan === null || placement.columnSpan === undefined ? 1 : placement.columnSpan;
        element.setAttribute("data-grid-column-span", String(span));
        element.style.gridColumn = placement.column + " / span " + span;
      } else if (opts.fullWidthByDefault) {
        element.setAttribute("data-grid-column", "1");
        element.setAttribute("data-grid-column-span", "-1");
        element.style.gridColumn = "1 / -1";
      }
      if (placement.row !== null && placement.row !== undefined) {
        element.setAttribute("data-grid-row", String(placement.row));
        const span = placement.rowSpan === null || placement.rowSpan === undefined ? 1 : placement.rowSpan;
        element.style.gridRow = placement.row + " / span " + span;
      }
      if (placement.minHeight !== null && placement.minHeight !== undefined) {
        element.style.minHeight = placement.minHeight + "px";
      }
      return;
    }
    if (placement.type === "FLOW") {
      if (placement.align) {
        element.setAttribute("data-placement-align", placement.align);
        element.style.alignSelf = FLOW_ALIGN_CSS[placement.align];
      }
      if (placement.width) {
        element.setAttribute("data-placement-width", String(placement.width.value) + placement.width.unit);
        element.style.width =
          placement.width.unit === "PERCENT"
            ? placement.width.value + "%"
            : placement.width.value + "px";
      }
      if (placement.minHeight !== null && placement.minHeight !== undefined) {
        element.style.minHeight = placement.minHeight + "px";
      }
      return;
    }
    if (placement.type === "ABSOLUTE") {
      element.style.position = "absolute";
      if (placement.x !== null && placement.x !== undefined) element.style.left = placement.x + "px";
      if (placement.y !== null && placement.y !== undefined) element.style.top = placement.y + "px";
      if (placement.width !== null && placement.width !== undefined) element.style.width = placement.width + "px";
      if (placement.height !== null && placement.height !== undefined) element.style.height = placement.height + "px";
      if (placement.zIndex !== null && placement.zIndex !== undefined) {
        element.style.zIndex = String(placement.zIndex);
      }
    }
  }

  /**
   * 渲染一个节点。
   *
   * ctx: {definition, payload, payloadRegions, resolvedTheme, showHidden,
   *        renderLeaf, totalsByUnit, chain（祖先样式链）, fullWidthByDefault,
   *        groups（窄屏重排用的登记表）}
   * placement：本节点**生效**的定位（父容器补位后可能与声明的不同）。
   */
  function renderNode(doc, node, ctx, placement) {
    if (!isPlainObject(node)) return null;
    if (node.hidden && !ctx.showHidden) return null; // 隐藏内容压根不进 DOM
    const own = placement === undefined ? node.placement : placement;
    if (node.isContainer) return renderGroup(doc, node, ctx, own);
    return renderLeafWrapper(doc, node, ctx, own);
  }

  /** 渲染容器分组（<section>）。 */
  function renderGroup(doc, node, ctx, placement) {
    const section = doc.createElement("section");
    section.setAttribute(MODULE_ATTR, "group");
    section.setAttribute("data-region-id", node.id);
    const arrangement = node.arrangement || NEUTRAL_ARRANGEMENT;
    section.setAttribute("data-arrangement-mode", arrangement.mode);
    if (arrangement.mode === "GRID") {
      section.setAttribute("data-arrangement-columns", String(arrangement.columns));
    } else {
      section.setAttribute("data-arrangement-direction", arrangement.direction);
    }
    section.setAttribute("data-gap", String(arrangement.gap));
    section.setAttribute("class", node.hidden ? "wp-group wp-group-hidden" : "wp-group");
    if (node.hidden) section.setAttribute("data-hidden", "true");
    applyArrangementStyle(section, arrangement);

    // 组自身的定位（契约 §3.3）：与叶子走同一套合法定位语义。
    // 之前组只按 arrangement 铺开、忽略自己的 layout.placement，
    // 导致「组占定义要求的宽度」这条不成立（默认只占根栅格的一格）。
    applyPlacementAttributes(section, placement, { fullWidthByDefault: ctx.fullWidthByDefault });

    // 样式：主题默认 → 祖先 → 本区域 → 组件字段样式
    const ownChain = ctx.chain.concat(nodeStyleSources(node));
    const style = mergeStyle(ownChain.concat(nodeComponentStyleSources(node)), ctx.resolvedTheme);
    applyStyle(section, style);
    if (node.hidden) section.style.opacity = HIDDEN_OPACITY;

    // 错误态：区域照常渲染，只多一层红框与提示。
    const errorBox = applyErrorState(doc, section, node.errors);

    // 组标题（默认不渲染：契约 §5.2 的 DOM 契约里没有标题元素）
    if (ctx.renderGroupTitle && node.name) {
      const title = doc.createElement("div");
      title.setAttribute(MODULE_ATTR, "group-title");
      title.setAttribute("class", "wp-group-title");
      title.textContent = node.name;
      section.appendChild(title);
    }

    // 子节点
    const childCtx = {
      ...ctx,
      chain: ownChain,
      fullWidthByDefault: false,
    };
    const declaredColumns = arrangement.mode === "GRID" ? arrangement.columns : 1;
    const groupState = {
      element: section,
      arrangement,
      declaredColumns,
      effectiveColumns: declaredColumns,
      // 只有「自己排版」的组才参与窄屏减列。
      // 根是页面坐标栅格（12 列坐标），由 render() 单独登记，不减列。
      narrowable: arrangement.mode === "GRID",
      rendered: [],
    };
    if (Array.isArray(ctx.groups)) ctx.groups.push(groupState);

    // 先算「哪些子项会进 DOM」：隐藏且没开「显示隐藏内容」的不进。
    const visible = [];
    node.children.forEach((child, originalIndex) => {
      if (child.hidden && !ctx.showHidden) return;
      visible.push({ child, originalIndex });
    });
    // 隐藏导致补位：有子项被隐藏时，后面的子项要前移、不留空位。
    // 补位只是**布局重排**，不写报表定义（定义里的 hidden 字段原样不动）。
    const needsReflow = visible.length !== node.children.length;
    const packed = needsReflow
      ? packGridSlots(
          visible.map((entry) => {
            const slot = gridSlotOf(entry.child.placement);
            return { slot, auto: isAutoDerivedSlot(slot, entry.originalIndex, declaredColumns) };
          }),
          declaredColumns,
        )
      : null;

    let absoluteExtent = 0;
    visible.forEach((entry, index) => {
      const child = entry.child;
      const slot = packed ? packed[index] : null;
      const childPlacement = packed && slot ? placementWithSlot(child.placement, slot) : child.placement;
      const element = renderNode(doc, child, childCtx, childPlacement);
      if (!element) return;
      groupState.rendered.push({
        element,
        child,
        originalIndex: entry.originalIndex,
        effectivePlacement: childPlacement,
      });
      if (child.placement && child.placement.type === "ABSOLUTE") {
        const bottom = (child.placement.y || 0) + (child.placement.height || 0);
        if (bottom > absoluteExtent) absoluteExtent = bottom;
      }
      section.appendChild(element);
    });
    // 绝对定位的子项不占文档流：给容器留出高度，内容不裁、不重叠。
    if (absoluteExtent > 0) {
      section.style.position = "relative";
      section.style.minHeight = absoluteExtent + "px";
    }
    // 错误提示排在内容之后：卡片本体先看见，提示条紧随其后。
    if (errorBox) section.appendChild(errorBox);
    return section;
  }

  /**
   * 主题生效时，让卡片**让位**给主题外观。
   *
   * 卡片是既有代码画出来的，自带 `bg-white` / `border-slate-200` /
   * `text-slate-*` 这些类。它们会**盖住**落在外壳上的主题底色、边框与字色：
   * 深色主题会变成「白卡片 + 深色外壳」。
   *
   * 这里只摘掉**由主题管理的那些外观类**，别的类（布局、字号、徽标语义色）
   * 一概不动 —— 主题管外观，不管排版。
   */
  const THEME_NEUTRAL_BG_CLASSES = ["bg-white", "bg-slate-50", "bg-slate-100"];

  function neutralizeCardChrome(element) {
    if (!element || typeof element.querySelectorAll !== "function") return;
    const all = [element].concat(
      Array.prototype.slice.call(element.querySelectorAll("*")),
    );
    for (let i = 0; i < all.length; i += 1) {
      const node = all[i];
      if (!node.getAttribute) continue;
      const cls = node.getAttribute("class");
      if (!cls) continue;
      const parts = cls.split(/\s+/).filter(Boolean);
      const kept = parts.filter(function (name) {
        if (THEME_NEUTRAL_BG_CLASSES.indexOf(name) >= 0) return false;
        if (/^border-slate-\d+$/.test(name)) return false;
        if (/^text-slate-\d+$/.test(name)) return false;
        return true;
      });
      if (kept.length !== parts.length) {
        node.setAttribute("class", kept.join(" "));
      }
    }
  }

  // Scope overrides to the rendered card, not its badges, tools or chart internals.
  function applyCardPresentation(element, style) {
    if (!element.style || typeof element.getAttribute !== "function") return;
    const cls = ownString(element.getAttribute("class")).trim();
    element.setAttribute("class", cls ? cls + " wp-card" : "wp-card");
    const css = styleToCss(style);
    for (const prop of ["fontSize", "textAlign", "borderRadius"]) {
      if (!Object.prototype.hasOwnProperty.call(css, prop)) continue;
      const name = prop.replace(/[A-Z]/g, (letter) => "-" + letter.toLowerCase());
      element.setAttribute("data-wp-" + name, "1");
      if (typeof element.style.setProperty === "function") {
        element.style.setProperty("--wp-" + name, css[prop]);
      } else {
        element.style["--wp-" + name] = css[prop];
      }
    }
  }

  /** 渲染叶子外壳（包住 P0 注入的叶子渲染产物）。 */
  function renderLeafWrapper(doc, node, ctx, placement) {
    const wrapper = doc.createElement("div");
    wrapper.setAttribute(MODULE_ATTR, node.hidden ? "hidden-leaf" : "leaf");
    wrapper.setAttribute("data-region-id", node.id);
    wrapper.setAttribute("class", node.hidden ? "wp-leaf wp-leaf-hidden" : "wp-leaf");
    if (node.hidden) wrapper.setAttribute("data-hidden", "true");

    applyPlacementAttributes(wrapper, placement, { fullWidthByDefault: ctx.fullWidthByDefault });

    const ownChain = ctx.chain.concat(nodeStyleSources(node));
    const style = mergeStyle(ownChain.concat(nodeComponentStyleSources(node)), ctx.resolvedTheme);
    applyStyle(wrapper, style);
    if (node.hidden) wrapper.style.opacity = HIDDEN_OPACITY;

    // 错误态：区域照常渲染（可交互、可保存），只多一层红框与提示。
    const errorBox = applyErrorState(doc, wrapper, node.errors);

    const payloadRegion = findPayloadRegion(ctx.payloadRegions, node.id);
    if (typeof ctx.renderLeaf === "function") {
      let produced = null;
      try {
        produced = ctx.renderLeaf(node.region, payloadRegion, {
          totalsByUnit: ctx.totalsByUnit,
          definition: ctx.definition,
          payload: ctx.payload,
          hidden: node.hidden === true,
          errors: node.errors || [],
        });
      } catch (error) {
        // 一张卡渲染失败不该拖垮整张报表：留一个空壳继续往下走。
        produced = null;
        if (typeof console !== "undefined" && console.error) {
          console.error("[workbench-presentation] 叶子渲染失败", node.id, error);
        }
      }
      if (produced && typeof produced.appendChild === "function") {
        wrapper.appendChild(produced);
        // 主题在生效时：卡片让位（摘掉自带底色 / 边框 / 字色类）。
        if (ctx.resolvedTheme && ctx.resolvedTheme.tokens) {
          neutralizeCardChrome(produced);
        }
        applyCardPresentation(produced, style);
      } else if (typeof produced === "string" && produced) {
        wrapper.textContent = produced;
      }
    }
    // 错误提示排在卡片之后：卡片本体先看见，提示条紧随其后。
    if (errorBox) wrapper.appendChild(errorBox);
    return wrapper;
  }

  // ---------------------------------------------------------------- 生命周期

  /** 尺寸变化回调（去抖）。 */
  function scheduleResize(state) {
    if (!state || state.disposed) return;
    if (state.resizeTimer !== null && state.resizeTimer !== undefined) {
      clearTimeout(state.resizeTimer);
    }
    state.resizeTimer = setTimeout(() => {
      state.resizeTimer = null;
      if (!state || state.disposed) return;
      if (typeof state.onResize === "function") {
        try {
          state.onResize({ root: state.root, element: state.lastResizedElement || null });
        } catch (error) {
          if (typeof console !== "undefined" && console.error) {
            console.error("[workbench-presentation] resize 回调失败", error);
          }
        }
      }
      if (state.root && typeof state.root.dispatchEvent === "function" && typeof CustomEvent === "function") {
        try {
          state.root.dispatchEvent(new CustomEvent("workbench-presentation:resize"));
        } catch (error) {
          /* 派发失败不影响渲染结果 */
        }
      }
    }, state.resizeDebounceMs);
  }

  /** 观察容器尺寸变化（ResizeObserver 可能不存在：没有就跳过）。 */
  function setupObservers(state, elements) {
    const win = winOf(state.root);
    const ResizeObserverCtor =
      win && typeof win.ResizeObserver === "function"
        ? win.ResizeObserver
        : typeof ResizeObserver === "function"
          ? ResizeObserver
          : null;
    if (!ResizeObserverCtor) return;
    for (const element of elements) {
      let observer = null;
      try {
        observer = new ResizeObserverCtor(() => {
          state.lastResizedElement = element;
          // 容器尺寸变了：窄屏状态可能翻转（减列 ↔ 恢复列数）。
          updateNarrowLayout(state);
          scheduleResize(state);
        });
        observer.observe(element);
      } catch (error) {
        observer = null;
      }
      if (observer) state.observers.push(observer);
    }
  }

  /**
   * clear(root)
   *
   * 断开所有 observer、移除所有监听器、清掉待触发的去抖定时器、清空 root。
   */
  function clear(root) {
    if (!root || typeof root !== "object") return;
    const state = root[STATE_KEY];
    if (state) {
      state.disposed = true;
      for (const observer of state.observers || []) {
        try {
          observer.disconnect();
        } catch (error) {
          /* 断开失败不影响清理 */
        }
      }
      state.observers = [];
      if (state.resizeTimer !== null && state.resizeTimer !== undefined) {
        clearTimeout(state.resizeTimer);
        state.resizeTimer = null;
      }
      for (const entry of state.listeners || []) {
        const target = entry && entry.target;
        if (target && typeof target.removeEventListener === "function") {
          try {
            target.removeEventListener(entry.type, entry.handler);
          } catch (error) {
            /* 移除失败不影响清理 */
          }
        }
      }
      state.listeners = [];
      state.onResize = null;
    }
    try {
      root.innerHTML = "";
    } catch (error) {
      /* 有些宿主只读 innerHTML：忽略 */
    }
  }

  /**
   * render(root, view, options)
   *
   * root：挂载容器（#preview-root）
   * view：{definition, previewPayload, resolvedTheme, showHidden}
   * options：{renderLeaf, onResize, totalsByUnit, renderGroupTitle}
   *
   * 返回本模块自己创建的根元素（拿不到就返回 null）。
   */
  function render(root, view, options) {
    if (!root || typeof root !== "object") return null;
    clear(root);

    const opts = isPlainObject(options) ? options : {};
    const source = isPlainObject(view) ? view : {};
    const definition = isPlainObject(source.definition) ? source.definition : null;
    if (!definition) return null;

    const doc = docOf(root);
    if (!doc || typeof doc.createElement !== "function") return null;

    const payload = isPlainObject(source.previewPayload) ? source.previewPayload : {};
    const payloadRegions = Array.isArray(payload.regions) ? payload.regions : [];
    const resolvedTheme = isPlainObject(source.resolvedTheme) ? source.resolvedTheme : null;
    const showHidden = source.showHidden === true;

    const groups = [];
    const state = {
      root,
      rootElement: null,
      groups,
      observers: [],
      listeners: [],
      resizeTimer: null,
      resizeDebounceMs:
        typeof opts.resizeDebounceMs === "number" && isFinite(opts.resizeDebounceMs)
          ? opts.resizeDebounceMs
          : RESIZE_DEBOUNCE_MS,
      onResize: typeof opts.onResize === "function" ? opts.onResize : null,
      lastResizedElement: null,
      disposed: false,
      // 窄屏：显式配置优先，配置来源与生效列数记在这里。
      narrowConfig: resolveNarrowConfig(definition, opts),
      narrow: null,
      widthOverride:
        typeof opts.containerWidth === "number" && isFinite(opts.containerWidth)
          ? opts.containerWidth
          : null,
    };
    root[STATE_KEY] = state;

    const totalsByUnit =
      opts.totalsByUnit instanceof Map
        ? opts.totalsByUnit
        : buildTotalsByUnit(payloadRegions, duplicateRegionIds(definition));

    const tree = buildTree(definition);
    const page = isPlainObject(definition.page) ? definition.page : {};
    const rootArrangement = normalizeArrangement(page.arrangement, {
      mode: "GRID",
      columns: DEFAULT_ROOT_COLUMNS,
      direction: "HORIZONTAL",
      gap: DEFAULT_ROOT_GAP,
    });

    const rootElement = doc.createElement("div");
    rootElement.setAttribute(MODULE_ATTR, "root");
    rootElement.setAttribute("class", "wp-root");
    rootElement.setAttribute("data-arrangement-mode", rootArrangement.mode);
    if (rootArrangement.mode === "GRID") {
      rootElement.setAttribute("data-arrangement-columns", String(rootArrangement.columns));
    } else {
      rootElement.setAttribute("data-arrangement-direction", rootArrangement.direction);
    }
    rootElement.setAttribute("data-gap", String(rootArrangement.gap));
    applyArrangementStyle(rootElement, rootArrangement);

    // 主题的页面底色（colorBackground）落在根容器；没有主题就不写任何样式。
    applyStyle(rootElement, themePageStyle(resolvedTheme));

    const webMaxWidth = resolveWebMaxWidth(page.webMaxWidth);
    if (webMaxWidth) {
      rootElement.setAttribute("data-web-max-width", String(webMaxWidth));
      rootElement.style.maxWidth = webMaxWidth + "px";
      rootElement.style.margin = "0 auto";
    }

    const ctx = {
      definition,
      payload,
      payloadRegions,
      resolvedTheme,
      showHidden,
      renderLeaf: typeof opts.renderLeaf === "function" ? opts.renderLeaf : null,
      totalsByUnit,
      chain: [],
      fullWidthByDefault: true,
      renderGroupTitle: opts.renderGroupTitle === true,
      groups,
    };

    state.rootElement = rootElement;

    // 根容器也是一个「容器」：隐藏补位对它一视同仁。
    const rootState = {
      element: rootElement,
      arrangement: rootArrangement,
      declaredColumns: rootArrangement.mode === "GRID" ? rootArrangement.columns : 1,
      effectiveColumns: rootArrangement.mode === "GRID" ? rootArrangement.columns : 1,
      // 根是页面坐标栅格（12 列坐标），不是「自己排版」的组：不参与窄屏减列。
      narrowable: false,
      rendered: [],
    };
    groups.push(rootState);

    const visible = [];
    tree.forEach((node, originalIndex) => {
      if (node.hidden && !showHidden) return;
      visible.push({ node, originalIndex });
    });
    const needsReflow = visible.length !== tree.length;
    const packed = needsReflow
      ? packGridSlots(
          visible.map((entry) => {
            const slot = gridSlotOf(entry.node.placement);
            return { slot, auto: isAutoDerivedSlot(slot, entry.originalIndex, rootState.declaredColumns) };
          }),
          rootState.declaredColumns,
        )
      : null;

    const observed = [rootElement];
    let absoluteExtent = 0;
    visible.forEach((entry, index) => {
      const node = entry.node;
      const slot = packed ? packed[index] : null;
      const placement = packed && slot ? placementWithSlot(node.placement, slot) : node.placement;
      const element = renderNode(doc, node, ctx, placement);
      if (!element) return;
      rootState.rendered.push({
        element,
        child: node,
        originalIndex: entry.originalIndex,
        effectivePlacement: placement,
      });
      if (node.placement && node.placement.type === "ABSOLUTE") {
        const bottom = (node.placement.y || 0) + (node.placement.height || 0);
        if (bottom > absoluteExtent) absoluteExtent = bottom;
      }
      rootElement.appendChild(element);
      if (element.getAttribute && element.getAttribute(MODULE_ATTR) === "group") observed.push(element);
    });
    // 根级绝对定位的子项同样要有定位参照物（之前根没有 position:relative）。
    if (absoluteExtent > 0) {
      rootElement.style.position = "relative";
      rootElement.style.minHeight = absoluteExtent + "px";
    }
    root.appendChild(rootElement);

    // 窄屏减列：容器宽度够窄就把多列组收成窄屏列数。
    // 这是**视图状态**：只改排版，不写报表定义。
    applyNarrowState(
      state,
      narrowStateFor(
        state.narrowConfig,
        state.widthOverride !== null ? state.widthOverride : measureWidth(rootElement),
      ),
    );

    setupObservers(state, observed);

    // 窗口尺寸变化也走同一条回调（容器没变但视口变了时图表同样要 resize）。
    const win = winOf(root);
    if (win && typeof win.addEventListener === "function") {
      const handler = () => {
        updateNarrowLayout(state);
        scheduleResize(state);
      };
      try {
        win.addEventListener("resize", handler);
        state.listeners.push({ target: win, type: "resize", handler });
      } catch (error) {
        /* 加不上就算了：ResizeObserver 仍然在 */
      }
    }

    return rootElement;
  }

  /**
   * 把窄屏状态落到 DOM 上：组收列 + 组内子项重新排位。
   *
   * 只改排版，**不写报表定义**；恢复宽屏时按同一套规则排回去。
   */
  function applyNarrowState(state, narrow) {
    if (!state) return;
    state.narrow = narrow;
    const rootEl = state.rootElement;
    if (rootEl) {
      if (narrow.active) {
        rootEl.setAttribute("data-narrow-active", "1");
        rootEl.setAttribute("data-narrow-breakpoint", String(narrow.breakpointPx));
        rootEl.setAttribute("data-narrow-columns", String(narrow.columns));
      } else {
        removeAttr(rootEl, "data-narrow-active");
        removeAttr(rootEl, "data-narrow-breakpoint");
        removeAttr(rootEl, "data-narrow-columns");
      }
      rootEl.setAttribute("data-narrow-source", narrow.source);
      if (narrow.measured) rootEl.setAttribute("data-narrow-measured", "1");
      else removeAttr(rootEl, "data-narrow-measured");
    }
    for (const group of state.groups || []) {
      if (!group.narrowable) continue;
      const columns = narrowColumnsOf(group.arrangement, narrow);
      if (columns === group.effectiveColumns) continue;
      group.effectiveColumns = columns;
      if (group.arrangement.mode === "GRID") {
        group.element.style.gridTemplateColumns = "repeat(" + columns + ", minmax(0, 1fr))";
        // data-arrangement-columns 保持**声明列数**（契约 §5.2 的 DOM 约定不变），
        // 窄屏生效列数另记一个属性，避免「属性与实际排版」对不上时说不清。
        if (columns !== group.declaredColumns) {
          group.element.setAttribute("data-arrangement-columns-effective", String(columns));
        } else {
          removeAttr(group.element, "data-arrangement-columns-effective");
        }
      }
      repackGroupChildren(group, columns);
    }
  }

  /** 组内子项重新排位（补位 / 减列后调用）。 */
  function repackGroupChildren(group, columns) {
    const entries = group.rendered || [];
    const items = entries.map((entry) => {
      const slot = gridSlotOf(entry.child.placement);
      return { slot, auto: isAutoDerivedSlot(slot, entry.originalIndex, group.declaredColumns) };
    });
    const packed = packGridSlots(items, columns);
    entries.forEach((entry, index) => {
      const slot = packed[index];
      if (!slot) return;
      const current = gridSlotOf(entry.effectivePlacement);
      if (
        current &&
        current.column === slot.column &&
        current.row === slot.row &&
        current.columnSpan === slot.columnSpan &&
        current.rowSpan === slot.rowSpan
      ) {
        return;
      }
      updateGridSlot(entry.element, slot);
      entry.effectivePlacement = placementWithSlot(entry.child.placement, slot);
    });
  }

  /**
   * 容器宽度变了 → 重算窄屏状态并就地重排。
   *
   * 不重渲染、不重建卡片：只改排版（图表实例不受影响）。
   */
  function updateNarrowLayout(state) {
    if (!state || state.disposed) return;
    const width = state.widthOverride !== null ? state.widthOverride : measureWidth(state.rootElement);
    const narrow = narrowStateFor(state.narrowConfig, width);
    const current = state.narrow;
    if (
      current &&
      current.active === narrow.active &&
      current.columns === narrow.columns &&
      current.breakpointPx === narrow.breakpointPx &&
      current.source === narrow.source
    ) {
      return;
    }
    applyNarrowState(state, narrow);
  }

  /** 页面最大宽度：{value, unit:"PX"} → 像素整数；非法返回 null。 */
  function resolveWebMaxWidth(raw) {
    const length = resolveLength(raw);
    if (!length || length.unit !== "PX") return null;
    if (length.value < 640 || length.value > 2560) return null;
    return length.value;
  }

  return {
    MODULE_NAME,
    MODULE_ATTR,
    STATE_KEY,
    RESIZE_DEBOUNCE_MS,
    STYLE_KEYS,
    THEME_TOKEN_KEYS,
    DEFAULT_NARROW_BREAKPOINT_PX,
    DEFAULT_NARROW_COLUMNS,
    // ---- 渲染 ----
    render,
    clear,
    // ---- 纯函数（接线与测试用）----
    buildTree,
    resolveArrangement,
    resolvePlacement,
    mergeStyle,
    styleToCss,
    regionPath,
    buildTotalsByUnit,
    duplicateRegionIds,
    findPayloadRegion,
    isContainerRegion,
    // ---- 定位 / 占位 / 补位（接线与测试用）----
    gridSlotOf,
    isAutoDerivedSlot,
    packGridSlots,
    placementWithSlot,
    // ---- 窄屏（接线与测试用）----
    resolveNarrowConfig,
    parseBreakpointEntries,
    narrowEntryFor,
    narrowStateFor,
    narrowColumnsOf,
    measureWidth,
    // ---- 主题 / 错误态（接线与测试用）----
    themeStyleFromTokens,
    themePageStyle,
    regionErrors,
  };
});
