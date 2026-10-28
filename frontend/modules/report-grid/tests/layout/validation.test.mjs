import test from 'node:test';
import assert from 'node:assert/strict';

import {
  GRID_COLUMNS,
  DEFAULT_GRID_CONFIG,
  resolveGridLayout,
} from '../../layout/grid-layout.mjs';

/** 构造一个合法的 GRID placement。 */
function grid(column, row, columnSpan = 1, rowSpan = 1) {
  return { type: 'GRID', column, row, columnSpan, rowSpan };
}

/** 构造一个最小合法 item。 */
function item(regionId, placement = grid(1, 1), extra = {}) {
  return { regionId, placement, ...extra };
}

/** 断言结果是某个错误码的失败返回。 */
function assertInvalid(result, code) {
  assert.equal(result.kind, 'invalid', `期望 invalid，实际 ${JSON.stringify(result)}`);
  assert.equal(result.code, code);
  assert.equal(typeof result.message, 'string');
  assert.ok(result.message.length > 0, 'message 不应为空');
}

test('导出固定列数与默认配置', () => {
  assert.equal(GRID_COLUMNS, 12);
  assert.equal(DEFAULT_GRID_CONFIG.columns, 12);
  assert.equal(DEFAULT_GRID_CONFIG.rowUnitPx, 48);
  assert.equal(DEFAULT_GRID_CONFIG.gapPx, 12);
  assert.equal(DEFAULT_GRID_CONFIG.defaultMinColumnSpan, 1);
  assert.equal(DEFAULT_GRID_CONFIG.defaultMinRowSpan, 1);
});

test('items 不是数组时返回 INVALID_ITEMS', () => {
  assertInvalid(resolveGridLayout({ items: 'nope' }), 'INVALID_ITEMS');
  assertInvalid(resolveGridLayout({ items: null }), 'INVALID_ITEMS');
  assertInvalid(resolveGridLayout({}), 'INVALID_ITEMS');
});

test('item 缺少有效 regionId 时返回 INVALID_PLACEMENT', () => {
  assertInvalid(resolveGridLayout({ items: [{ placement: grid(1, 1) }] }), 'INVALID_PLACEMENT');
  assertInvalid(resolveGridLayout({ items: [{ regionId: '', placement: grid(1, 1) }] }), 'INVALID_PLACEMENT');
  assertInvalid(resolveGridLayout({ items: [{ regionId: 7, placement: grid(1, 1) }] }), 'INVALID_PLACEMENT');
});

test('placement.type 不是 GRID 时返回 INVALID_PLACEMENT', () => {
  assertInvalid(
    resolveGridLayout({ items: [{ regionId: 'a', placement: { type: 'FLOW', column: 1, row: 1 } }] }),
    'INVALID_PLACEMENT',
  );
});

test('regionId 重复时返回 DUPLICATE_REGION_ID', () => {
  assertInvalid(
    resolveGridLayout({ items: [item('a'), item('a')] }),
    'DUPLICATE_REGION_ID',
  );
});

test('intent.kind 非法或缺 regionId 时返回 INVALID_INTENT', () => {
  assertInvalid(resolveGridLayout({ items: [item('a')], intent: { kind: 'delete', regionId: 'a' } }), 'INVALID_INTENT');
  assertInvalid(resolveGridLayout({ items: [item('a')], intent: { kind: 'move' } }), 'INVALID_INTENT');
  assertInvalid(resolveGridLayout({ items: [item('a')], intent: { kind: 'move', regionId: '' } }), 'INVALID_INTENT');
  assertInvalid(resolveGridLayout({ items: [item('a')], intent: 'move' }), 'INVALID_INTENT');
});

test('intent 指向不存在的 regionId 时返回 UNKNOWN_REGION', () => {
  assertInvalid(
    resolveGridLayout({ items: [item('a')], intent: { kind: 'move', regionId: 'ghost', column: 1, row: 1 } }),
    'UNKNOWN_REGION',
  );
});

test('校验顺序：INVALID_ITEMS 先于其余所有错误', () => {
  assertInvalid(
    resolveGridLayout({ items: 'nope', intent: { kind: 'bogus' } }),
    'INVALID_ITEMS',
  );
});

test('校验顺序：INVALID_PLACEMENT 先于 DUPLICATE_REGION_ID', () => {
  assertInvalid(
    resolveGridLayout({ items: [item('a'), item('a', { type: 'TABLE' })] }),
    'INVALID_PLACEMENT',
  );
});

test('校验顺序：DUPLICATE_REGION_ID 先于 INVALID_INTENT', () => {
  assertInvalid(
    resolveGridLayout({ items: [item('a'), item('a')], intent: { kind: 'bogus' } }),
    'DUPLICATE_REGION_ID',
  );
});

test('校验顺序：INVALID_INTENT 先于 UNKNOWN_REGION', () => {
  assertInvalid(
    resolveGridLayout({ items: [item('a')], intent: { kind: 'bogus', regionId: 'ghost' } }),
    'INVALID_INTENT',
  );
});

test('校验顺序：UNKNOWN_REGION 先于尺寸与越界', () => {
  assertInvalid(
    resolveGridLayout({
      items: [item('a', grid(1, 1, 4, 4), { minColumnSpan: 6 })],
      intent: { kind: 'resize', regionId: 'ghost', columnSpan: 1 },
    }),
    'UNKNOWN_REGION',
  );
});

test('校验顺序：尺寸与越界错误只在结构与意图都合法后出现', () => {
  assertInvalid(
    resolveGridLayout({
      items: [item('a', grid(1, 1, 4, 4), { minColumnSpan: 6 })],
      intent: { kind: 'resize', regionId: 'a', columnSpan: 1 },
    }),
    'MIN_SIZE_VIOLATION',
  );
});
