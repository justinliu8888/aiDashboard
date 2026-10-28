import test from 'node:test';
import assert from 'node:assert/strict';

import { resolveGridLayout } from '../../layout/grid-layout.mjs';

function grid(column, row, columnSpan = 1, rowSpan = 1) {
  return { type: 'GRID', column, row, columnSpan, rowSpan };
}

function item(regionId, placement = grid(1, 1), extra = {}) {
  return { regionId, placement, ...extra };
}

/** 从成功返回里取出某个 regionId 的 placement。 */
function placementOf(result, regionId) {
  const found = result.placements.find((p) => p.regionId === regionId);
  assert.ok(found, `结果里没有 ${regionId}`);
  return found.placement;
}

test('初始化：全部合法时原样返回且没有受影响 ID', () => {
  const items = [
    item('a', grid(1, 1, 6, 2)),
    item('b', grid(7, 1, 6, 2)),
  ];
  const result = resolveGridLayout({ items, intent: null });

  assert.equal(result.kind, 'success');
  assert.deepEqual(result.affectedRegionIds, []);
  assert.deepEqual(placementOf(result, 'a'), grid(1, 1, 6, 2));
  assert.deepEqual(placementOf(result, 'b'), grid(7, 1, 6, 2));
});

test('初始化：位置缺失时按 items 顺序补齐', () => {
  const result = resolveGridLayout({
    items: [
      item('a', { type: 'GRID' }),
      item('b', { type: 'GRID' }),
    ],
    intent: null,
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(1, 1, 1, 1));
  assert.deepEqual(placementOf(result, 'b'), grid(1, 2, 1, 1));
  assert.deepEqual(result.affectedRegionIds, ['a', 'b']);
});

test('初始化：只补齐非法字段，已有合法信息一项不改', () => {
  const result = resolveGridLayout({
    items: [item('a', { type: 'GRID', column: 3, row: 0, columnSpan: 2, rowSpan: 1 })],
    intent: null,
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(3, 1, 2, 1));
  assert.deepEqual(result.affectedRegionIds, ['a']);
});

test('初始化：补齐项不会压到已有合法项上', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1)), item('b', { type: 'GRID' })],
    intent: null,
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(1, 1));
  assert.deepEqual(placementOf(result, 'b'), grid(1, 2));
  assert.deepEqual(result.affectedRegionIds, ['b']);
});

test('初始化：合法项的位置完全不动', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(5, 7, 4, 2)), item('b', { type: 'GRID' })],
    intent: null,
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(5, 7, 4, 2));
});

test('初始化：按 item 覆盖的最小尺寸补齐跨列跨行', () => {
  const result = resolveGridLayout({
    items: [item('a', { type: 'GRID' }, { minColumnSpan: 3, minRowSpan: 2 })],
    intent: null,
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(1, 1, 3, 2));
});

test('初始化：没有 item 覆盖时用 config 的默认最小尺寸', () => {
  const result = resolveGridLayout({
    items: [item('a', { type: 'GRID' })],
    intent: null,
    config: { defaultMinColumnSpan: 2, defaultMinRowSpan: 3 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(1, 1, 2, 3));
});

test('初始化：补齐的跨列跨行不小于生效最小尺寸', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1, 1, 1), { minColumnSpan: 2 })],
    intent: null,
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(1, 1, 2, 1));
  assert.deepEqual(result.affectedRegionIds, ['a']);
});

test('初始化：受影响 ID 只含被补齐的项', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1)), item('b', { type: 'GRID' }), item('c', grid(9, 4, 4, 1))],
    intent: null,
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(result.affectedRegionIds, ['b']);
});

test('初始化：结果 placements 覆盖全部 item 且顺序与 items 一致', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1)), item('b', grid(3, 1)), item('c', grid(5, 1))],
    intent: null,
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(result.placements.map((p) => p.regionId), ['a', 'b', 'c']);
});
