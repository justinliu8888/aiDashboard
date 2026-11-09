// 前端「表格渲染」独立模块（批次二 · 表格组件包 TB01–TB07）。
//
// 这个文件刻意做成不依赖主工作台的独立模块：
//   - 浏览器：window.WorkbenchTable（由协调者接入 workbench.html）
//   - Node 测试：require("../pages/workbench_table.js")
//
// 它只做一件事：**把表格载荷按声明画出来**。
//
// ── 声明逐字生效（协议 report-definition-v1.md 的 TableComponent）────────
//   * columns：列的**集合与顺序**取服务端给的定义（预览列不会留在表里）；
//     每列的 headerPath 决定它挂在哪一层表头下；
//   * label 与 fieldRef **互相独立**：改标题不改取值，改取值不改标题；
//   * width：PX / PERCENT 两种单位，直接落到 <colgroup>；
//   * groupRowsBy：**只是展示分组**（渲染一行分组标题），**不产生小计或聚合**；
//   * rowNumber：首列序号（**跨页连续**，按可见行集计）；
//   * pageSize：**只是网页展示分页**（不改数据来源、不改汇总口径）；
//   * frozenColumnCount：前若干列冻结（sticky + 实色背景）；
//   * summaries：引用已登记输出的汇总；**没有对应口径就不显示数字**。
//
// 它**不**取数、不发请求、不改会话状态、不写草稿，也不碰主工作台的菜单与状态。
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.WorkbenchTable = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // ---------------------------------------------------------------- 常量

  /** 序号列的估计宽度（像素）—— 冻结列的左偏移要用它起算。 */
  const ROW_NUMBER_WIDTH_PX = 48;

  /** 没声明列宽时的估计宽度（像素）。 */
  const DEFAULT_COLUMN_WIDTH_PX = 140;

  /** 百分比列宽换算成像素时的估计表宽。 */
  const ESTIMATED_TABLE_WIDTH_PX = 960;

  /** 空值显示（列格式里给了 nullText 就用它的）。 */
  const NULL_TEXT = "—";

  /** 汇总口径的中文说法（与后端同一份）。 */
  const BASIS_LABELS = { FULL_RESULT: "全量口径", VISIBLE_ROWS: "可见行口径" };

  /** 分页状态挂在宿主容器上的键名。 */
  const STATE_KEY = "__workbenchTableState";

  /** 表头路径的分段符（表头文字里不会出现，用它比较前缀不会撞车）。 */
  const PATH_KEY_SEP = "\u0001";

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

  /** 是不是一个普通对象。 */
  function isObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }

  /** 列的表头路径：每一层一段表头文字（空层不算一层）。 */
  function normalizeHeaderPath(raw) {
    if (!Array.isArray(raw)) return [];
    return raw
      .map((item) => String(item == null ? "" : item).trim())
      .filter((item) => item !== "");
  }

  /** 列宽 → 像素（PERCENT 按估计表宽换算）。 */
  function columnWidthPx(column) {
    const width = isObject(column) ? column.width : null;
    if (isObject(width)) {
      const value = Number(width.value);
      if (Number.isFinite(value)) {
        const unit = String(width.unit || "PX").toUpperCase();
        if (unit === "PERCENT") {
          return Math.round((value / 100) * ESTIMATED_TABLE_WIDTH_PX);
        }
        return Math.round(value);
      }
    }
    return DEFAULT_COLUMN_WIDTH_PX;
  }

  /** 列宽 → CSS 宽度值（PX → px，PERCENT → %）。 */
  function columnWidthCss(column) {
    const width = isObject(column) ? column.width : null;
    if (!isObject(width)) return "";
    const value = Number(width.value);
    if (!Number.isFinite(value)) return "";
    const unit = String(width.unit || "PX").toUpperCase();
    return unit === "PERCENT" ? `${value}%` : `${value}px`;
  }

  // ---------------------------------------------------------------- 表头

  /**
   * 按 headerPath 排出**多层表头**。
   *
   * 规则：
   *   - 共享同一段 headerPath 前缀的相邻列合成一个父表头（colspan）；
   *   - 没有 headerPath 的列**占满整个表头高度**（没有父级）；
   *   - 叶子表头显示列标题（label）；
   *   - 各行高度**对齐**（短的那一层用 rowspan 补齐），表头保持矩形。
   */
  function buildHeaderLayout(columns) {
    const paths = columns.map((column) => normalizeHeaderPath(column.headerPath));
    let height = 1;
    paths.forEach((path) => {
      if (path.length + 1 > height) height = path.length + 1;
    });

    const rows = [];
    for (let r = 0; r < height; r++) rows.push([]);
    // covered[c] = 这一列的单元格覆盖到第几行（含）。
    const covered = new Array(columns.length).fill(-1);

    for (let r = 0; r < height; r++) {
      let c = 0;
      while (c < columns.length) {
        if (covered[c] >= r) {
          c += 1;
          continue;
        }
        const path = paths[c];
        if (path.length > r) {
          // 父表头：共享同一段前缀的相邻列合成一个。
          const key = path.slice(0, r + 1).join(PATH_KEY_SEP);
          let j = c + 1;
          while (
            j < columns.length &&
            paths[j].length > r &&
            paths[j].slice(0, r + 1).join(PATH_KEY_SEP) === key
          ) {
            j += 1;
          }
          rows[r].push({
            type: "group",
            text: path[r],
            colspan: j - c,
            rowspan: 1,
            from: c,
            to: j - 1,
          });
          for (let x = c; x < j; x++) covered[x] = r;
          c = j;
          continue;
        }
        // 叶子表头：显示列标题，补齐下面几行。
        rows[r].push({
          type: "leaf",
          text: String((columns[c] && columns[c].label) || ""),
          colspan: 1,
          rowspan: height - r,
          index: c,
        });
        covered[c] = height - 1;
        c += 1;
      }
    }
    return { rows, height };
  }

  // ---------------------------------------------------------------- 单元格

  /**
   * 一格的文字：优先用服务端按权威格式算好的显示串，其次原始值。
   *
   * **空值不留空格子**：给列格式的 nullText，没有就显示「—」。
   */
  function cellText(row, displayRow, column) {
    const fieldRef = String((column && column.fieldRef) || "");
    const raw = isObject(row) ? row[fieldRef] : undefined;
    if (raw == null || raw === "") {
      const format = isObject(column) && isObject(column.format) ? column.format : null;
      const nullText = format && format.nullText != null ? String(format.nullText) : "";
      return nullText || NULL_TEXT;
    }
    const display = isObject(displayRow) ? displayRow[fieldRef] : undefined;
    if (display != null && display !== "") return String(display);
    return String(raw);
  }

  // ---------------------------------------------------------------- 渲染

  /** 渲染表格（写 container.innerHTML；分页控件自己接事件）。 */
  function renderTable(container, region, ctx) {
    if (!container) return;
    const options = ctx || {};
    const reg = region || {};
    const payload = reg.payload || {};

    const columns = Array.isArray(payload.columns)
      ? payload.columns.filter(isObject)
      : [];
    const visibleRows = Array.isArray(payload.visibleRows)
      ? payload.visibleRows.filter(isObject)
      : Array.isArray(payload.rows)
        ? payload.rows.filter(isObject)
        : [];
    const displayRows =
      Array.isArray(payload.displayRows) && payload.displayRows.length === visibleRows.length
        ? payload.displayRows
        : null;

    const showRowNumber = payload.rowNumber !== false;
    const groupRowsBy = Array.isArray(payload.groupRowsBy)
      ? payload.groupRowsBy.filter((item) => !!item)
      : [];
    const pageSize =
      Number.isInteger(payload.pageSize) && payload.pageSize > 0 ? payload.pageSize : 0;
    const frozenCount = Math.max(
      0,
      Math.min(Number(payload.frozenColumnCount) || 0, columns.length),
    );

    const totalColumns = columns.length + (showRowNumber ? 1 : 0);
    const header = buildHeaderLayout(columns);

    // -- 列宽（<colgroup>） -------------------------------------------------
    const colgroup = columns.length
      ? `<colgroup>${columns
          .map((column) => {
            const css = columnWidthCss(column);
            return css ? `<col style="width:${css}" />` : "<col />";
          })
          .join("")}</colgroup>`
      : "";

    // -- 冻结列的左偏移（按已知列宽累加） -----------------------------------
    const widths = columns.map(columnWidthPx);
    const rowNumberWidth = showRowNumber ? ROW_NUMBER_WIDTH_PX : 0;
    const frozenOffsets = [];
    let acc = rowNumberWidth;
    for (let i = 0; i < frozenCount; i++) {
      frozenOffsets.push(acc);
      acc += widths[i] || DEFAULT_COLUMN_WIDTH_PX;
    }
    const stickyStyle = (index) => {
      if (index == null || index < 0 || index >= frozenOffsets.length) return "";
      return `position:sticky;left:${frozenOffsets[index]}px;`;
    };

    // -- 表头 ---------------------------------------------------------------
    const cornerCell = showRowNumber
      ? `<th class="wtb-th wtb-th--index${frozenCount > 0 ? " wtb-cell--frozen wtb-cell--frozen-head" : ""}"${
          frozenCount > 0 ? ' style="position:sticky;left:0px;"' : ""
        } rowspan="${header.height}">#</th>`
      : "";

    // 序号列表头**占满整个表头高度**，所以它放在表头的第一行里（rowspan）——
    // 另起一行会让表头多出一行、整张表歪掉。
    const headRowHtml = header.rows.map((row, rowIndex) => {
      const cells = row
        .map((cell) => {
          const attrs = [];
          if (cell.colspan > 1) attrs.push(`colspan="${cell.colspan}"`);
          if (cell.rowspan > 1) attrs.push(`rowspan="${cell.rowspan}"`);
          let style = "";
          let frozenIndex = -1;
          if (cell.type === "leaf") {
            frozenIndex = cell.index < frozenCount ? cell.index : -1;
            if (frozenIndex >= 0) {
              style = stickyStyle(frozenIndex);
              attrs.push(`data-wtb-col="${cell.index}"`);
            }
          } else if (cell.to < frozenCount) {
            // 整段都在冻结区里的父表头也一起冻住。
            frozenIndex = cell.from;
            style = stickyStyle(frozenIndex);
            attrs.push(`data-wtb-col="${cell.from}"`);
          }
          const cls = cell.type === "group" ? "wtb-th wtb-th--group" : "wtb-th";
          const frozenCls = frozenIndex >= 0 ? " wtb-cell--frozen wtb-cell--frozen-head" : "";
          const styleAttr = style ? ` style="${style}"` : "";
          return `<th class="${cls}${frozenCls}"${attrs.length ? ` ${attrs.join(" ")}` : ""}${styleAttr}>${escapeHtml(cell.text)}</th>`;
        })
        .join("");
      const leading = rowIndex === 0 ? cornerCell : "";
      return `<tr class="wtb-head-row">${leading}${cells}</tr>`;
    });
    const headRows = headRowHtml.join("");

    // -- 表体 ---------------------------------------------------------------
    const state = readState(container);
    const totalPages = pageSize ? Math.max(1, Math.ceil(visibleRows.length / pageSize)) : 1;
    if (state.page > totalPages - 1) state.page = totalPages - 1;
    if (state.page < 0) state.page = 0;
    const start = pageSize ? state.page * pageSize : 0;
    const end = pageSize ? Math.min(visibleRows.length, start + pageSize) : visibleRows.length;
    const pageRows = visibleRows.slice(start, end);

    const groupField = groupRowsBy.length ? String(groupRowsBy[0]) : "";
    const bodyRows = [];
    let lastGroup = null;
    pageRows.forEach((row, index) => {
      const displayRow = displayRows ? displayRows[start + index] : null;
      if (groupField) {
        const groupValue = isObject(row) ? row[groupField] : undefined;
        const groupKey = groupValue == null ? "" : String(groupValue);
        if (groupKey !== lastGroup) {
          lastGroup = groupKey;
          const groupText = cellText(row, displayRow, { fieldRef: groupField, format: null });
          bodyRows.push(
            `<tr class="wtb-group-row"><td class="wtb-group-cell" colspan="${totalColumns}">${escapeHtml(groupText)}</td></tr>`,
          );
        }
      }
      const cells = columns
        .map((column, columnIndex) => {
          const text = cellText(row, displayRow, column);
          const frozenIndex = columnIndex < frozenCount ? columnIndex : -1;
          const style = frozenIndex >= 0 ? stickyStyle(frozenIndex) : "";
          const attrs = frozenIndex >= 0 ? ` data-wtb-col="${columnIndex}"` : "";
          const cls = `wtb-td${frozenIndex >= 0 ? " wtb-cell--frozen" : ""}`;
          return `<td class="${cls}"${attrs}${style ? ` style="${style}"` : ""}>${escapeHtml(text)}</td>`;
        })
        .join("");
      const indexCell = showRowNumber
        ? `<td class="wtb-td wtb-td--index${frozenCount > 0 ? " wtb-cell--frozen" : ""}"${
            frozenCount > 0 ? ' style="position:sticky;left:0px;"' : ""
          }>${start + index + 1}</td>`
        : "";
      bodyRows.push(`<tr class="wtb-row">${indexCell}${cells}</tr>`);
    });

    // -- 汇总（tfoot） ------------------------------------------------------
    const summaries = Array.isArray(payload.summaries) ? payload.summaries : [];
    const summaryRows = summaries
      .map((summary) => {
        const item = isObject(summary) ? summary : {};
        const basisLabel = BASIS_LABELS[item.basis] || String(item.basis || "");
        const available = item.available !== false && item.displayValue != null && item.displayValue !== "";
        const valueText = available ? String(item.displayValue) : String(item.note || "");
        const labelSpan = Math.max(1, totalColumns - 2);
        return (
          `<tr class="wtb-summary-row">` +
          `<th class="wtb-summary__label" scope="row" colspan="${labelSpan}">${escapeHtml(item.label || "")}</th>` +
          `<td class="wtb-summary__value${available ? "" : " wtb-summary__value--missing"}">${escapeHtml(valueText)}</td>` +
          `<td class="wtb-summary__basis">${escapeHtml(basisLabel)}</td>` +
          `</tr>`
        );
      })
      .join("");

    // -- 分页控件（只是展示分页） -------------------------------------------
    const pager = pageSize
      ? `<div class="wtb-pager"><span class="wtb-pager__info">共 ${visibleRows.length} 行可见 · 第 ${state.page + 1} / ${totalPages} 页</span>` +
        `<button type="button" class="wtb-pager__btn" data-wtb-page="prev"${state.page <= 0 ? " disabled" : ""}>上一页</button>` +
        `<button type="button" class="wtb-pager__btn" data-wtb-page="next"${state.page >= totalPages - 1 ? " disabled" : ""}>下一页</button></div>`
      : "";

    const truncation = options.truncation;
    const truncationNote =
      isObject(truncation) && truncation.limit != null
        ? `<div class="wtb-note" data-wtb-truncated="true">仅显示前 ${escapeHtml(truncation.limit)} 行（${
            truncation.source === "json"
              ? `共 ${escapeHtml(truncation.total)} 行，表格行数被截断`
              : "表格行数被截断，各行之和小于报表总量"
          }）</div>`
        : "";

    container.innerHTML =
      `<div class="wtb-wrap">` +
      `<div class="wtb-scroll">` +
      `<table class="wtb-table">${colgroup}` +
      `<thead class="wtb-thead">${headRows}</thead>` +
      `<tbody class="wtb-tbody">${bodyRows.join("")}</tbody>` +
      (summaryRows ? `<tfoot class="wtb-tfoot">${summaryRows}</tfoot>` : "") +
      `</table></div>` +
      `${pager}${truncationNote}</div>`;

    bindPager(container, region, ctx, totalPages);
    refineFrozenOffsets(container, frozenCount, showRowNumber);
  }

  // ---------------------------------------------------------------- 分页

  /** 读这一块表格的分页状态（默认第一页）。 */
  function readState(container) {
    if (!container[STATE_KEY] || typeof container[STATE_KEY] !== "object") {
      container[STATE_KEY] = { page: 0 };
    }
    return container[STATE_KEY];
  }

  /** 分页控件的事件（点一下重画这一块表格）。 */
  function bindPager(container, region, ctx, totalPages) {
    const buttons = container.querySelectorAll("[data-wtb-page]");
    if (!buttons || !buttons.length) return;
    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        const state = readState(container);
        const direction = button.getAttribute("data-wtb-page");
        if (direction === "prev") {
          state.page = Math.max(0, state.page - 1);
        } else {
          state.page = Math.min(totalPages - 1, state.page + 1);
        }
        renderTable(container, region, ctx);
      });
    });
  }

  // ---------------------------------------------------------------- 冻结列

  /**
   * 冻结列的左偏移**按实际渲染宽度**再算一遍。
   *
   * HTML 里已经写了估计偏移；渲染后按第一行正文单元格的真实宽度校正一次，
   * 免得列宽是自动算出来时冻结位置对不上。量不到就保持估计值。
   */
  function refineFrozenOffsets(container, frozenCount, showRowNumber) {
    if (!frozenCount) return;
    const row = container.querySelector("tbody tr.wtb-row");
    if (!row || !row.children || !row.children.length) return;
    const offsets = [];
    let acc = 0;
    Array.prototype.forEach.call(row.children, (cell) => {
      offsets.push(acc);
      const width = cell.getBoundingClientRect
        ? cell.getBoundingClientRect().width
        : cell.offsetWidth;
      acc += width || 0;
    });
    const cells = container.querySelectorAll("[data-wtb-col]");
    Array.prototype.forEach.call(cells, (cell) => {
      const columnIndex = Number(cell.getAttribute("data-wtb-col"));
      if (!Number.isFinite(columnIndex)) return;
      const position = (showRowNumber ? 1 : 0) + columnIndex;
      const left = offsets[position];
      if (left == null) return;
      cell.style.left = `${Math.round(left)}px`;
    });
  }

  // ---------------------------------------------------------------- 导出

  return {
    renderTable,
    escapeHtml,
    // 以下是给测试与复用的小工具（不属于对外契约，但一并导出）
    buildHeaderLayout,
    cellText,
    columnWidthCss,
    columnWidthPx,
  };
});
