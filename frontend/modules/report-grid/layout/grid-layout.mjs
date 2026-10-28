/**
 * G 独立网格模块 · 布局计算公开入口（契约壳 / contract shell）
 *
 * ⚠️ 本文件当前是**协调者冻结的契约壳**，用于让页面与交互 agent 与布局 agent 并行开工。
 * 它只实现「结构校验 + 无碰撞近似」，**不是最终实现**：
 *   - 不做碰撞让位（冲突组件保持原位）
 *   - 初始化按简单顺序堆叠，未做占位搜索
 * 布局 agent（GA）按 `frontend/modules/report-grid/CONTRACT.md` 以 TDD 重写本文件，
 * 补齐碰撞让位、最小尺寸、越界与确定性语义。
 *
 * 冻结的公开导出（签名与名字不得改动）：
 *   GRID_COLUMNS
 *   DEFAULT_GRID_CONFIG
 *   resolveGridLayout({items, intent, config})
 *
 * 契约详见 frontend/modules/report-grid/CONTRACT.md 第 1、2 节。
 */

export const GRID_COLUMNS = 12;

export const DEFAULT_GRID_CONFIG = Object.freeze({
  columns: 12,
  rowUnitPx: 48,
  gapPx: 12,
  defaultMinColumnSpan: 1,
  defaultMinRowSpan: 1,
});

const CODE = Object.freeze({
  INVALID_ITEMS: 'INVALID_ITEMS',
  INVALID_PLACEMENT: 'INVALID_PLACEMENT',
  DUPLICATE_REGION_ID: 'DUPLICATE_REGION_ID',
  UNKNOWN_REGION: 'UNKNOWN_REGION',
  INVALID_INTENT: 'INVALID_INTENT',
  OUT_OF_BOUNDS: 'OUT_OF_BOUNDS',
  MIN_SIZE_VIOLATION: 'MIN_SIZE_VIOLATION',
});

function invalid(code, message) {
  return { kind: 'invalid', code, message };
}

function isPositiveInt(n) {
  return Number.isInteger(n) && n >= 1;
}

/** 只读拷贝，不修改输入。 */
function clonePlacement(p) {
  return {
    type: 'GRID',
    column: p.column,
    row: p.row,
    columnSpan: p.columnSpan,
    rowSpan: p.rowSpan,
  };
}

function mergeConfig(config) {
  return { ...DEFAULT_GRID_CONFIG, ...(config || {}) };
}

function placementIsValid(p, columns) {
  if (!p || typeof p !== 'object') return false;
  if (p.type !== 'GRID') return false;
  if (!isPositiveInt(p.column) || !isPositiveInt(p.row)) return false;
  if (!isPositiveInt(p.columnSpan) || !isPositiveInt(p.rowSpan)) return false;
  if (p.column + p.columnSpan - 1 > columns) return false;
  return true;
}

function minSpans(item, config) {
  const minColumnSpan = isPositiveInt(item.minColumnSpan)
    ? item.minColumnSpan
    : config.defaultMinColumnSpan;
  const minRowSpan = isPositiveInt(item.minRowSpan)
    ? item.minRowSpan
    : config.defaultMinRowSpan;
  return { minColumnSpan, minRowSpan };
}

/**
 * @param {{items?: any[], intent?: any, config?: any}} input
 * @returns {{kind:'success', placements:Array<{regionId:string, placement:object}>, affectedRegionIds:string[]}
 *          |{kind:'invalid', code:string, message:string}}
 */
export function resolveGridLayout(input) {
  const { items, intent, config } = input || {};

  if (!Array.isArray(items)) {
    return invalid(CODE.INVALID_ITEMS, 'items 必须是数组');
  }

  const cfg = mergeConfig(config);

  // 1) 结构校验
  const seen = new Set();
  for (const item of items) {
    if (!item || typeof item.regionId !== 'string' || item.regionId === '') {
      return invalid(CODE.INVALID_PLACEMENT, 'item 缺少有效 regionId');
    }
    if (seen.has(item.regionId)) {
      return invalid(CODE.DUPLICATE_REGION_ID, `regionId 重复: ${item.regionId}`);
    }
    seen.add(item.regionId);
  }

  const hasIntent = intent !== null && intent !== undefined;
  if (hasIntent) {
    if (!intent || typeof intent !== 'object') {
      return invalid(CODE.INVALID_INTENT, 'intent 必须是对象');
    }
    if (intent.kind !== 'move' && intent.kind !== 'resize') {
      return invalid(CODE.INVALID_INTENT, `未知 intent.kind: ${String(intent.kind)}`);
    }
    if (typeof intent.regionId !== 'string' || intent.regionId === '') {
      return invalid(CODE.INVALID_INTENT, 'intent 缺少 regionId');
    }
    if (!seen.has(intent.regionId)) {
      return invalid(CODE.UNKNOWN_REGION, `未知 regionId: ${intent.regionId}`);
    }
  }

  // 2) 归一化现有 placement（缺失或非法者按 items 顺序补齐）
  const affected = [];
  const state = new Map();
  let cursorRow = 1;

  for (const item of items) {
    if (placementIsValid(item.placement, cfg.columns)) {
      state.set(item.regionId, {
        item,
        placement: clonePlacement(item.placement),
        changed: false,
      });
      const endRow = item.placement.row + item.placement.rowSpan - 1;
      if (endRow >= cursorRow) cursorRow = endRow + 1;
      continue;
    }
    const { minColumnSpan, minRowSpan } = minSpans(item, cfg);
    const columnSpan = Math.max(minColumnSpan, 1);
    const rowSpan = Math.max(minRowSpan, 1);
    state.set(item.regionId, {
      item,
      placement: { type: 'GRID', column: 1, row: cursorRow, columnSpan, rowSpan },
      changed: true,
    });
    affected.push(item.regionId);
    cursorRow += rowSpan;
  }

  // 3) 应用 intent（契约壳：无碰撞让位，仅覆盖目标）
  if (hasIntent) {
    const target = state.get(intent.regionId);
    const { minColumnSpan, minRowSpan } = minSpans(target.item, cfg);
    const current = target.placement;

    let next = { ...current };
    if (intent.kind === 'move') {
      if (!isPositiveInt(intent.column) || !isPositiveInt(intent.row)) {
        return invalid(CODE.OUT_OF_BOUNDS, 'move 需要正整数 column/row');
      }
      next.column = intent.column;
      next.row = intent.row;
    } else {
      if (intent.column !== undefined) {
        if (!isPositiveInt(intent.column)) {
          return invalid(CODE.OUT_OF_BOUNDS, 'resize 的 column 必须为正整数');
        }
        next.column = intent.column;
      }
      if (intent.row !== undefined) {
        if (!isPositiveInt(intent.row)) {
          return invalid(CODE.OUT_OF_BOUNDS, 'resize 的 row 必须为正整数');
        }
        next.row = intent.row;
      }
      if (intent.columnSpan !== undefined) {
        if (!isPositiveInt(intent.columnSpan)) {
          return invalid(CODE.MIN_SIZE_VIOLATION, 'resize 的 columnSpan 必须为正整数');
        }
        next.columnSpan = intent.columnSpan;
      }
      if (intent.rowSpan !== undefined) {
        if (!isPositiveInt(intent.rowSpan)) {
          return invalid(CODE.MIN_SIZE_VIOLATION, 'resize 的 rowSpan 必须为正整数');
        }
        next.rowSpan = intent.rowSpan;
      }
    }

    if (next.columnSpan < minColumnSpan || next.rowSpan < minRowSpan) {
      return invalid(
        CODE.MIN_SIZE_VIOLATION,
        `小于最小尺寸 ${minColumnSpan}x${minRowSpan}`,
      );
    }
    if (next.column + next.columnSpan - 1 > cfg.columns) {
      return invalid(CODE.OUT_OF_BOUNDS, `横向越界: ${next.column}+${next.columnSpan}-1 > ${cfg.columns}`);
    }

    target.placement = clonePlacement(next);
    target.changed = true;
    affected.length = 0;
    affected.push(intent.regionId);
  }

  const placements = items.map((item) => ({
    regionId: item.regionId,
    placement: clonePlacement(state.get(item.regionId).placement),
  }));

  return { kind: 'success', placements, affectedRegionIds: affected };
}
