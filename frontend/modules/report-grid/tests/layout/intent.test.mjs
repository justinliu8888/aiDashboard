//
// 运行：node --test frontend/modules/report-grid/tests/layout/*.test.mjs
// （目录参数在 Node 24 上不匹配默认测试文件规则，要显式给 glob；
//   跑单个文件 node --test frontend/modules/report-grid/tests/layout/intent.test.mjs 也可以）
// 不依赖任何第三方包（用 Node 内置 test runner），不改动共用依赖配置。
import test from 'node:test';
import assert from 'node:assert/strict';

import { resolveGridLayout } from '../../layout/grid-layout.mjs';
import { grid, item, placementOf } from '../helpers/fixtures.mjs';

test('move：目标落到请求的 (column, row)', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1))],
    intent: { kind: 'move', regionId: 'a', column: 5, row: 3 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(5, 3));
  assert.deepEqual(result.affectedRegionIds, ['a']);
});

test('move：目标的跨列跨行保持不变', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1, 3, 2))],
    intent: { kind: 'move', regionId: 'a', column: 4, row: 2 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(4, 2, 3, 2));
});

test('move：无关组件完全不动', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1)), item('b', grid(5, 5, 2, 2))],
    intent: { kind: 'move', regionId: 'a', column: 2, row: 2 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'b'), grid(5, 5, 2, 2));
  assert.deepEqual(result.affectedRegionIds, ['a']);
});

test('move：column/row 非正整数返回 OUT_OF_BOUNDS', () => {
  const base = { items: [item('a', grid(1, 1))] };
  assert.equal(resolveGridLayout({ ...base, intent: { kind: 'move', regionId: 'a', column: 0, row: 1 } }).code, 'OUT_OF_BOUNDS');
  assert.equal(resolveGridLayout({ ...base, intent: { kind: 'move', regionId: 'a', column: 1, row: 0 } }).code, 'OUT_OF_BOUNDS');
  assert.equal(resolveGridLayout({ ...base, intent: { kind: 'move', regionId: 'a', column: 1.5, row: 1 } }).code, 'OUT_OF_BOUNDS');
  assert.equal(resolveGridLayout({ ...base, intent: { kind: 'move', regionId: 'a', column: 1 } }).code, 'OUT_OF_BOUNDS');
});

test('move：横向越界返回 OUT_OF_BOUNDS', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1, 4, 1))],
    intent: { kind: 'move', regionId: 'a', column: 10, row: 1 },
  });

  assert.equal(result.kind, 'invalid');
  assert.equal(result.code, 'OUT_OF_BOUNDS');
});

test('move：落到当前位置不产生任何变化', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(3, 3))],
    intent: { kind: 'move', regionId: 'a', column: 3, row: 3 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(result.affectedRegionIds, []);
});

test('resize：缺省项沿用目标当前值', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(2, 4, 3, 2))],
    intent: { kind: 'resize', regionId: 'a', columnSpan: 5 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(2, 4, 5, 2));
  assert.deepEqual(result.affectedRegionIds, ['a']);
});

test('resize：可以只改行号', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(2, 4, 3, 2))],
    intent: { kind: 'resize', regionId: 'a', row: 9 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(2, 9, 3, 2));
});

test('resize：可以同时改列号与跨行', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(2, 4, 3, 2))],
    intent: { kind: 'resize', regionId: 'a', column: 6, rowSpan: 3 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(6, 4, 3, 3));
});

test('resize：小于 item 覆盖的最小跨列返回 MIN_SIZE_VIOLATION', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1, 4, 1), { minColumnSpan: 2 })],
    intent: { kind: 'resize', regionId: 'a', columnSpan: 1 },
  });

  assert.equal(result.kind, 'invalid');
  assert.equal(result.code, 'MIN_SIZE_VIOLATION');
});

test('resize：小于 item 覆盖的最小跨行返回 MIN_SIZE_VIOLATION', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1, 1, 4), { minRowSpan: 2 })],
    intent: { kind: 'resize', regionId: 'a', rowSpan: 1 },
  });

  assert.equal(result.kind, 'invalid');
  assert.equal(result.code, 'MIN_SIZE_VIOLATION');
});

test('resize：小于 config 默认最小尺寸返回 MIN_SIZE_VIOLATION', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1, 4, 4))],
    intent: { kind: 'resize', regionId: 'a', columnSpan: 1 },
    config: { defaultMinColumnSpan: 2 },
  });

  assert.equal(result.kind, 'invalid');
  assert.equal(result.code, 'MIN_SIZE_VIOLATION');
});

test('resize：横向越界返回 OUT_OF_BOUNDS', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(1, 1, 2, 1))],
    intent: { kind: 'resize', regionId: 'a', column: 12 },
  });

  assert.equal(result.kind, 'invalid');
  assert.equal(result.code, 'OUT_OF_BOUNDS');
});

test('resize：不改任何字段时不产生变化', () => {
  const result = resolveGridLayout({
    items: [item('a', grid(3, 3, 2, 2))],
    intent: { kind: 'resize', regionId: 'a' },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(result.affectedRegionIds, []);
});

test('move：目标本身位置非法时先补齐再落位', () => {
  const result = resolveGridLayout({
    items: [item('a', { type: 'GRID' })],
    intent: { kind: 'move', regionId: 'a', column: 3, row: 3 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(3, 3));
  assert.deepEqual(result.affectedRegionIds, ['a']);
});

test('resize：目标本身位置非法时缺省项沿用补齐后的值', () => {
  const result = resolveGridLayout({
    items: [item('a', { type: 'GRID' })],
    intent: { kind: 'resize', regionId: 'a', columnSpan: 3 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(placementOf(result, 'a'), grid(1, 1, 3, 1));
  assert.deepEqual(result.affectedRegionIds, ['a']);
});
