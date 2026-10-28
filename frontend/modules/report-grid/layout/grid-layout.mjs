/**
 * G 独立网格模块 · 布局计算（纯函数，无 DOM、无全局状态）
 *
 * 冻结契约：frontend/modules/report-grid/CONTRACT.md 第 1 节（Interface / 数据形状 / 配置）
 * 与第 2 节（布局语义）、第 2.1 节（错误码）。
 *
 * 本模块只做几何计算：
 *   - 不碰 DOM、不读全局、不写文件、不发网络；
 *   - 输入数组与嵌套对象一律不就地修改，返回全新对象。
 *
 * 公开导出（签名与名字冻结，不得改动）：
 *   GRID_COLUMNS
 *   DEFAULT_GRID_CONFIG
 *   resolveGridLayout({items, intent, config})
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

/** 落位是否与输入不同（只看几何字段）。 */
function placementChanged(before, after) {
  if (!before || !after) return true;
  return (
    before.column !== after.column ||
    before.row !== after.row ||
    before.columnSpan !== after.columnSpan ||
    before.rowSpan !== after.rowSpan
  );
}

/** config 与默认值浅合并；非法配置回落到默认值。 */
function mergeConfig(config) {
  const source = config && typeof config === 'object' ? config : {};
  const merged = { ...DEFAULT_GRID_CONFIG, ...source };
  // 契约 §1.1/§1.4：列数固定 12，不接受 config 覆盖，避免与页面渲染口径不一致
  merged.columns = GRID_COLUMNS;
  if (!isPositiveInt(merged.defaultMinColumnSpan)) {
    merged.defaultMinColumnSpan = DEFAULT_GRID_CONFIG.defaultMinColumnSpan;
  }
  if (!isPositiveInt(merged.defaultMinRowSpan)) {
    merged.defaultMinRowSpan = DEFAULT_GRID_CONFIG.defaultMinRowSpan;
  }
  return merged;
}

/** item 覆盖优先，否则取 config 默认。 */
function effectiveMinSpans(item, config) {
  const minColumnSpan = isPositiveInt(item.minColumnSpan)
    ? item.minColumnSpan
    : config.defaultMinColumnSpan;
  const minRowSpan = isPositiveInt(item.minRowSpan)
    ? item.minRowSpan
    : config.defaultMinRowSpan;
  return { minColumnSpan, minRowSpan };
}

/** 两个矩形（行区间 × 列区间）是否占用同一网格单元。 */
function overlaps(a, b) {
  const aColEnd = a.column + a.columnSpan - 1;
  const bColEnd = b.column + b.columnSpan - 1;
  if (aColEnd < b.column || bColEnd < a.column) return false;
  const aRowEnd = a.row + a.rowSpan - 1;
  const bRowEnd = b.row + b.rowSpan - 1;
  if (aRowEnd < b.row || bRowEnd < a.row) return false;
  return true;
}

/** placement 是否合法（含生效最小尺寸与横向不越界）。 */
function placementIsValid(p, config, minColumnSpan, minRowSpan) {
  if (!isPositiveInt(p.column) || !isPositiveInt(p.row)) return false;
  if (!isPositiveInt(p.columnSpan) || !isPositiveInt(p.rowSpan)) return false;
  if (p.columnSpan < minColumnSpan || p.rowSpan < minRowSpan) return false;
  if (p.column + p.columnSpan - 1 > config.columns) return false;
  return true;
}

/**
 * 补齐一个非法 placement：已有合法信息（列、跨列、跨行）一律保留，
 * 只修非法字段；行号非法或与已占位冲突时向下寻找第一个合法行。
 */
function repairPlacement(item, config, minColumnSpan, minRowSpan, occupied) {
  const p = item.placement;

  let columnSpan = isPositiveInt(p.columnSpan) ? p.columnSpan : minColumnSpan;
  if (columnSpan < minColumnSpan) columnSpan = minColumnSpan;
  if (columnSpan > config.columns) columnSpan = config.columns;

  let rowSpan = isPositiveInt(p.rowSpan) ? p.rowSpan : minRowSpan;
  if (rowSpan < minRowSpan) rowSpan = minRowSpan;

  let column = isPositiveInt(p.column) ? p.column : 1;
  if (column + columnSpan - 1 > config.columns) {
    column = config.columns - columnSpan + 1;
    if (column < 1) column = 1;
  }

  let row = isPositiveInt(p.row) ? p.row : 1;
  const candidate = { column, row, columnSpan, rowSpan };
  while (occupied.some((other) => overlaps(other, candidate))) {
    row += 1;
    candidate.row = row;
  }

  return { type: 'GRID', column, row, columnSpan, rowSpan };
}

/**
 * 归一化全部 item 的 placement：
 *   1) 合法项原样保留（先占位，任何补齐项都不会压到它们）；
 *   2) 非法项按 items 顺序补齐，避开已占位的网格单元。
 */
function normalizeItems(items, config) {
  const minSpans = new Map();
  const placements = new Map();

  for (const item of items) {
    const mins = effectiveMinSpans(item, config);
    minSpans.set(item.regionId, mins);
    if (placementIsValid(item.placement, config, mins.minColumnSpan, mins.minRowSpan)) {
      placements.set(item.regionId, clonePlacement(item.placement));
    }
  }

  const occupied = [...placements.values()];
  for (const item of items) {
    if (placements.has(item.regionId)) continue;
    const mins = minSpans.get(item.regionId);
    const fixed = repairPlacement(item, config, mins.minColumnSpan, mins.minRowSpan, occupied);
    placements.set(item.regionId, fixed);
    occupied.push(fixed);
  }

  return { placements, minSpans };
}

/**
 * 碰撞让位（契约 2.5 / 2.6）：
 *   - 目标优先占据落位；
 *   - 与落位冲突的组件保持横向位置（column/columnSpan 不变），只向下让位到第一个合法行；
 *   - 连锁碰撞按「原 row → 原 column → regionId」确定性顺序处理；
 *   - 与落位不冲突的组件完全不动，也不向上填空。
 */
function yieldToTarget(items, state, targetId) {
  const targetRect = state.get(targetId);
  const movers = [];
  const others = [];

  for (const item of items) {
    if (item.regionId === targetId) continue;
    const rect = state.get(item.regionId);
    if (overlaps(rect, targetRect)) movers.push({ regionId: item.regionId, rect });
    else others.push(rect);
  }

  movers.sort((x, y) => {
    if (x.rect.row !== y.rect.row) return x.rect.row - y.rect.row;
    if (x.rect.column !== y.rect.column) return x.rect.column - y.rect.column;
    return x.regionId < y.regionId ? -1 : x.regionId > y.regionId ? 1 : 0;
  });

  const settled = [targetRect, ...others];
  const isFree = (candidate, self) =>
    !settled.some((other) => overlaps(other, candidate)) &&
    !movers.some((m) => m !== self && overlaps(m.rect, candidate));

  for (const mover of movers) {
    const { column, columnSpan, rowSpan } = mover.rect;
    let row = mover.rect.row;
    const candidate = { column, row, columnSpan, rowSpan };
    while (!isFree(candidate, mover)) {
      row += 1;
      candidate.row = row;
    }
    const next = { type: 'GRID', column, row, columnSpan, rowSpan };
    mover.rect = next;
    state.set(mover.regionId, next);
    settled.push(next);
  }
}

/**
 * 组装受影响 ID：
 *   - 目标优先，其后是实际被让位的 ID（按「原 row → 原 column → regionId」排序）；
 *   - 初始化时按 items 顺序只含被补齐的 ID。
 */
function collectAffected(items, finalPlacements, before, targetId) {
  const changed = [];
  for (const item of items) {
    const after = finalPlacements.get(item.regionId);
    if (!placementChanged(item.placement, after)) continue;
    changed.push(item.regionId);
  }

  if (targetId === null || targetId === undefined) {
    return changed;
  }

  const others = changed
    .filter((id) => id !== targetId)
    .sort((a, b) => {
      const pa = before.get(a);
      const pb = before.get(b);
      if (pa.row !== pb.row) return pa.row - pb.row;
      if (pa.column !== pb.column) return pa.column - pb.column;
      return a < b ? -1 : a > b ? 1 : 0;
    });

  const affected = [];
  if (changed.includes(targetId)) affected.push(targetId);
  affected.push(...others);
  return affected;
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

  // 1) 结构校验（契约 2.1：INVALID_PLACEMENT 全部先于 DUPLICATE_REGION_ID）
  const seen = new Set();
  for (const item of items) {
    if (!item || typeof item.regionId !== 'string' || item.regionId === '') {
      return invalid(CODE.INVALID_PLACEMENT, 'item 缺少有效 regionId');
    }
    if (!item.placement || typeof item.placement !== 'object' || item.placement.type !== 'GRID') {
      return invalid(CODE.INVALID_PLACEMENT, `item ${item.regionId} 的 placement.type 必须是 GRID`);
    }
  }

  for (const item of items) {
    if (seen.has(item.regionId)) {
      return invalid(CODE.DUPLICATE_REGION_ID, `regionId 重复: ${item.regionId}`);
    }
    seen.add(item.regionId);
  }

  // 2) 意图校验（INVALID_INTENT 先于 UNKNOWN_REGION）
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

  // 3) 归一化：合法项原样保留，非法项按 items 顺序补齐
  const normalized = normalizeItems(items, cfg);
  const state = normalized.placements;
  const before = new Map(state);

  // 4) 应用意图（尺寸与越界是最后一组校验）
  if (hasIntent) {
    const target = state.get(intent.regionId);
    const { minColumnSpan, minRowSpan } = normalized.minSpans.get(intent.regionId);
    const next = { ...target };

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
      return invalid(
        CODE.OUT_OF_BOUNDS,
        `横向越界: ${next.column}+${next.columnSpan}-1 > ${cfg.columns}`,
      );
    }

    state.set(intent.regionId, clonePlacement(next));

    // 4b) 碰撞让位
    yieldToTarget(items, state, intent.regionId);
  }

  // 5) 组装结果
  const affected = collectAffected(items, state, before, hasIntent ? intent.regionId : null);

  const placements = items.map((item) => ({
    regionId: item.regionId,
    placement: clonePlacement(state.get(item.regionId)),
  }));

  return { kind: 'success', placements, affectedRegionIds: affected };
}
