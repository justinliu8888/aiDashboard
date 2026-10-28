import test from 'node:test';
import assert from 'node:assert/strict';

import { resolveGridLayout, DEFAULT_GRID_CONFIG, GRID_COLUMNS } from '../../layout/grid-layout.mjs';

/**
 * 受影响集合语义（契约「受影响集合」小节）与固定列数口径的回归测试。
 *
 * 这两条是 Spec 审查后明确下来的约定，单独钉住，避免以后被无意改掉：
 *   1. 受影响集合 = 目标 + 实际被让位的区域 + 因输入非法而被补齐的区域；
 *      合法且未被让位的无关区域不在集合内。
 *   2. 列数固定 12，config.columns 不能改口径，布局层与页面层必须一致。
 */

function grid(column, row, columnSpan = 1, rowSpan = 1) {
  return { type: 'GRID', column, row, columnSpan, rowSpan };
}

function placementOf(result, regionId) {
  const found = result.placements.find((p) => p.regionId === regionId);
  assert.ok(found, `结果里没有 ${regionId}`);
  return found.placement;
}

test('受影响集合：合法且未被让位的无关区域不计入', () => {
  const result = resolveGridLayout({
    items: [
      { regionId: 'a', placement: grid(1, 1) },
      { regionId: 'b', placement: grid(5, 5, 2, 2) },
    ],
    intent: { kind: 'move', regionId: 'a', column: 2, row: 2 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(result.affectedRegionIds, ['a']);
  assert.deepEqual(placementOf(result, 'b'), grid(5, 5, 2, 2));
});

test('受影响集合：实际被让位的区域计入，且排在目标之后', () => {
  const result = resolveGridLayout({
    items: [
      { regionId: 'a', placement: grid(1, 1, 4) },
      { regionId: 'b', placement: grid(1, 2, 4) },
      { regionId: 'c', placement: grid(9, 1, 4) },
    ],
    intent: { kind: 'move', regionId: 'a', column: 1, row: 2 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(result.affectedRegionIds, ['a', 'b']);
  assert.ok(placementOf(result, 'b').row > 2, 'b 应该向下让位');
});

test('受影响集合：输入非法而被补齐的区域计入（补齐结果必须能回到宿主并被保存）', () => {
  // c 的 placement 缺 row 与 span，属于非法输入
  const result = resolveGridLayout({
    items: [
      { regionId: 'a', placement: grid(1, 1) },
      { regionId: 'c', placement: { type: 'GRID', column: 1 } },
    ],
    intent: { kind: 'move', regionId: 'a', column: 1, row: 3 },
  });

  assert.equal(result.kind, 'success');
  assert.ok(result.affectedRegionIds.includes('a'), '目标必须在集合内');
  assert.ok(
    result.affectedRegionIds.includes('c'),
    '补齐项必须回传，否则非法 placement 会一直留在宿主模型里',
  );
  assert.equal(result.affectedRegionIds[0], 'a', '目标排第一');
  const repaired = placementOf(result, 'c');
  assert.ok(Number.isInteger(repaired.row) && repaired.row >= 1, 'c 的 row 应被补齐');
  assert.ok(repaired.columnSpan >= 1 && repaired.rowSpan >= 1, 'c 的 span 应被补齐');
});

test('受影响集合：全量 placements 覆盖所有 item，顺序与 items 一致', () => {
  const result = resolveGridLayout({
    items: [
      { regionId: 'a', placement: grid(1, 1) },
      { regionId: 'b', placement: grid(4, 1, 2) },
      { regionId: 'c', placement: grid(7, 1, 2) },
    ],
    intent: { kind: 'move', regionId: 'a', column: 2, row: 1 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(
    result.placements.map((p) => p.regionId),
    ['a', 'b', 'c'],
  );
});

test('列数固定 12：config.columns 不改变布局口径', () => {
  const result = resolveGridLayout({
    items: [{ regionId: 'a', placement: grid(1, 1, 6) }],
    intent: { kind: 'move', regionId: 'a', column: 9, row: 1 },
    config: { columns: 16 },
  });

  // 16 列下 9+6-1=14 不越界；固定 12 列下应当越界
  assert.equal(result.kind, 'invalid');
  assert.equal(result.code, 'OUT_OF_BOUNDS');
});

test('列数固定 12：GRID_COLUMNS 与 DEFAULT_GRID_CONFIG.columns 都是 12', () => {
  assert.equal(GRID_COLUMNS, 12);
  assert.equal(DEFAULT_GRID_CONFIG.columns, 12);
});
