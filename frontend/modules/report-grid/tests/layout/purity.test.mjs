//
// 运行：node --test frontend/modules/report-grid/tests/layout/*.test.mjs
// （目录参数在 Node 24 上不匹配默认测试文件规则，要显式给 glob；
//   跑单个文件 node --test frontend/modules/report-grid/tests/layout/purity.test.mjs 也可以）
// 不依赖任何第三方包（用 Node 内置 test runner），不改动共用依赖配置。
import test from 'node:test';
import assert from 'node:assert/strict';

import { DEFAULT_GRID_CONFIG, resolveGridLayout } from '../../layout/grid-layout.mjs';
import { grid, item } from '../helpers/fixtures.mjs';

test('初始化不会就地修改输入 items', () => {
  const items = [item('a', grid(1, 1)), item('b', { type: 'GRID' }), item('c', grid(4, 2, 2, 2))];
  const snapshot = structuredClone(items);

  resolveGridLayout({ items, intent: null });

  assert.deepEqual(items, snapshot);
});

test('move 不会就地修改输入 items', () => {
  const items = [item('a', grid(1, 1)), item('b', grid(1, 2))];
  const snapshot = structuredClone(items);

  resolveGridLayout({ items, intent: { kind: 'move', regionId: 'a', column: 1, row: 2 } });

  assert.deepEqual(items, snapshot);
});

test('resize 不会就地修改输入 items', () => {
  const items = [item('a', grid(1, 1, 2, 2)), item('b', grid(4, 1))];
  const snapshot = structuredClone(items);

  resolveGridLayout({ items, intent: { kind: 'resize', regionId: 'a', columnSpan: 4 } });

  assert.deepEqual(items, snapshot);
});

test('返回的 placement 是全新对象，改动结果不会回写输入', () => {
  const items = [item('a', grid(1, 1))];
  const result = resolveGridLayout({
    items,
    intent: { kind: 'move', regionId: 'a', column: 2, row: 2 },
  });

  const output = result.placements[0].placement;
  assert.notEqual(output, items[0].placement);
  output.column = 99;
  assert.equal(items[0].placement.column, 1);
  assert.equal(items[0].placement.row, 1);
});

test('返回的 placements 条目不是输入 item 本身', () => {
  const items = [item('a', grid(1, 1))];
  const result = resolveGridLayout({ items, intent: null });

  assert.notEqual(result.placements[0], items[0]);
  assert.equal(result.placements[0].regionId, 'a');
  assert.deepEqual(Object.keys(result.placements[0].placement).sort(), [
    'column',
    'columnSpan',
    'row',
    'rowSpan',
    'type',
  ]);
});

test('DEFAULT_GRID_CONFIG 是只读常量', () => {
  assert.ok(Object.isFrozen(DEFAULT_GRID_CONFIG));
  assert.equal(DEFAULT_GRID_CONFIG.columns, 12);
});

test('传入的 config 不会被就地修改', () => {
  const config = { defaultMinColumnSpan: 2 };
  const snapshot = { ...config };

  resolveGridLayout({ items: [item('a', grid(1, 1))], intent: null, config });

  assert.deepEqual(config, snapshot);
});

test('config 与默认值浅合并，未给出的项沿用默认', () => {
  const result = resolveGridLayout({
    items: [item('a', { type: 'GRID' })],
    intent: null,
    config: { defaultMinColumnSpan: 3 },
  });

  assert.equal(result.kind, 'success');
  assert.deepEqual(result.placements[0].placement, grid(1, 1, 3, 1));
});
