import test from 'node:test';
import assert from 'node:assert/strict';

import { resolveGridLayout } from '../../layout/grid-layout.mjs';

function grid(column, row, columnSpan = 1, rowSpan = 1) {
  return { type: 'GRID', column, row, columnSpan, rowSpan };
}

function item(regionId, placement = grid(1, 1), extra = {}) {
  return { regionId, placement, ...extra };
}

function placementOf(result, regionId) {
  const found = result.placements.find((p) => p.regionId === regionId);
  assert.ok(found, `结果里没有 ${regionId}`);
  return found.placement;
}

/** 断言结果里没有任何两个区域占用同一网格单元。 */
function assertNoOverlap(result) {
  const list = result.placements.map((p) => p.placement);
  for (let i = 0; i < list.length; i += 1) {
    for (let j = i + 1; j < list.length; j += 1) {
      const a = list[i];
      const b = list[j];
      const colOverlap = a.column <= b.column + b.columnSpan - 1 && b.column <= a.column + a.columnSpan - 1;
      const rowOverlap = a.row <= b.row + b.rowSpan - 1 && b.row <= a.row + a.rowSpan - 1;
      assert.ok(
        !(colOverlap && rowOverlap),
        `${result.placements[i].regionId} 与 ${result.placements[j].regionId} 重叠`,
      );
    }
  }
}

test('move：冲突组件向下让位到第一个合法行', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1)), item('b', grid(1, 2))],
    intent: { kind: 'move', regionId: 'a', column: 1, row: 2 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(1, 2));
  assert.deepEqual(placementOf(result, 'b'), grid(1, 3));
  assert.deepEqual(result.affectedRegionIds, ['a', 'b']);
  assertNoOverlap(result);
});

test('move：让位组件保持横向位置与跨列跨行', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1)), item('b', grid(2, 2, 3, 2))],
    intent: { kind: 'move', regionId: 'a', column: 2, row: 2 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'b'), grid(2, 3, 3, 2));
});

test('move：让位时避开无关组件占据的单元', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1, 2, 1)), item('b', grid(2, 1, 2, 1))],
    intent: { kind: 'move', regionId: 'a', column: 1, row: 1 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(1, 1, 2, 1));
  assert.deepEqual(placementOf(result, 'b'), grid(2, 2, 2, 1));
  assertNoOverlap(result);
});

test('move：多个冲突组件按 原 row → 原 column → regionId 处理', () => {
  const result = resolveGridLayout({
    items: [
      item('t', grid(5, 1, 2, 1)),
      item('a', grid(1, 1)),
      item('b', grid(2, 1)),
      item('c', grid(1, 1)),
    ],
    intent: { kind: 'move', regionId: 't', column: 1, row: 1 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 't'), grid(1, 1, 2, 1));
  assert.deepEqual(placementOf(result, 'a'), grid(1, 2));
  assert.deepEqual(placementOf(result, 'b'), grid(2, 2));
  assert.deepEqual(placementOf(result, 'c'), grid(1, 3));
  assert.deepEqual(result.affectedRegionIds, ['t', 'a', 'c', 'b']);
  assertNoOverlap(result);
});

test('move：目标优先占据落位，不与冲突组件换位', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1)), item('b', grid(1, 1))],
    intent: { kind: 'move', regionId: 'a', column: 4, row: 4 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(4, 4));
  assert.deepEqual(placementOf(result, 'b'), grid(1, 1));
});

test('resize：目标放大后冲突组件同样让位', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1)), item('b', grid(2, 1))],
    intent: { kind: 'resize', regionId: 'a', columnSpan: 2 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(1, 1, 2, 1));
  assert.deepEqual(placementOf(result, 'b'), grid(2, 2));
  assert.deepEqual(result.affectedRegionIds, ['a', 'b']);
  assertNoOverlap(result);
});

test('不向上填空：目标移走后原位置保留空位', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1)), item('b', grid(1, 2))],
    intent: { kind: 'move', regionId: 'a', column: 5, row: 5 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(5, 5));
  assert.deepEqual(placementOf(result, 'b'), grid(1, 2));
  assert.deepEqual(result.affectedRegionIds, ['a']);
});

test('不向上填空：缩小后保留空位，其他组件不上移', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1, 4, 2)), item('b', grid(1, 4, 4, 1))],
    intent: { kind: 'resize', regionId: 'a', columnSpan: 2, rowSpan: 1 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(1, 1, 2, 1));
  assert.deepEqual(placementOf(result, 'b'), grid(1, 4, 4, 1));
});

test('不向上填空：让位只向下，不交换位置', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(3, 3)), item('b', grid(1, 1))],
    intent: { kind: 'move', regionId: 'a', column: 1, row: 1 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(1, 1));
  assert.deepEqual(placementOf(result, 'b'), grid(1, 2));
});

test('确定性：同一输入与配置产生同一输出', () => {
  const input = {
    items: [item('a', grid(1, 1)), item('b', grid(1, 2, 2, 2)), item('c', grid(4, 1))],
    intent: { kind: 'move', regionId: 'a', column: 2, row: 2 },
  };

  const first = resolveGridLayout(input);
  const second = resolveGridLayout(input);
  assert.deepEqual(second, first);
});
