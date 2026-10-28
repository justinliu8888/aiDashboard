//
// 运行：node --test frontend/modules/report-grid/tests/page/*.test.mjs
// （目录参数在 Node 24 上不匹配默认测试文件规则，要显式给 glob；
//   跑单个文件 node --test frontend/modules/report-grid/tests/page/selection.test.mjs 也可以）
// 不依赖任何第三方包（用 Node 内置 test runner），不改动共用依赖配置。
/**
 * G 网格页面模块 · 选择行为
 *
 * 契约来源：CONTRACT.md 3.2（只读仍可选中查看）、3.10（选中区域消失时 onSelect(null)）、
 * 3.12（选择与取消选择；重复点同一区域不重复触发）。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { createDom, makePointerEvent } from './dom-stub.mjs';
import { mountReportGrid } from '../../page/report-grid.mjs';

function items() {
  return [
    { regionId: 'region-a', placement: { type: 'GRID', column: 1, row: 1, columnSpan: 6, rowSpan: 2 } },
    { regionId: 'region-b', placement: { type: 'GRID', column: 7, row: 1, columnSpan: 6, rowSpan: 3 } },
  ];
}

function clickRegion(root, regionId, { x = 100, y = 100 } = {}) {
  const region = root.querySelector(`.rg-region[data-region-id="${regionId}"]`);
  region.dispatchEvent(makePointerEvent('pointerdown', { x, y }));
  region.dispatchEvent(makePointerEvent('pointerup', { x, y }));
}

test('只读态点击区域触发 onSelect(regionId)', () => {
  const { root } = createDom();
  const selected = [];
  mountReportGrid(root, { items: items(), onSelect: (id) => selected.push(id) });

  clickRegion(root, 'region-a');

  assert.deepEqual(selected, ['region-a']);
});

test('选中态打在区域上，带 data-rg-selected="true"', () => {
  const { root } = createDom();
  mountReportGrid(root, { items: items() });

  clickRegion(root, 'region-b');

  const region = root.querySelector('.rg-region[data-region-id="region-b"]');
  assert.equal(region.getAttribute('data-rg-selected'), 'true');
});

test('重复点击同一区域不重复触发 onSelect', () => {
  const { root } = createDom();
  const selected = [];
  mountReportGrid(root, { items: items(), onSelect: (id) => selected.push(id) });

  clickRegion(root, 'region-a');
  clickRegion(root, 'region-a');

  assert.deepEqual(selected, ['region-a']);
});

test('点击另一个区域会回调新的 regionId', () => {
  const { root } = createDom();
  const selected = [];
  mountReportGrid(root, { items: items(), onSelect: (id) => selected.push(id) });

  clickRegion(root, 'region-a');
  clickRegion(root, 'region-b');

  assert.deepEqual(selected, ['region-a', 'region-b']);
});

test('点击网格空白处取消选择，回调 onSelect(null)', () => {
  const { root } = createDom();
  const selected = [];
  mountReportGrid(root, { items: items(), onSelect: (id) => selected.push(id) });

  clickRegion(root, 'region-a');
  const grid = root.querySelector('.rg-grid');
  grid.dispatchEvent(makePointerEvent('pointerdown', { x: 5, y: 5 }));
  grid.dispatchEvent(makePointerEvent('pointerup', { x: 5, y: 5 }));

  assert.deepEqual(selected, ['region-a', null]);
});

test('update 后选中区域仍在时不调用 onSelect', () => {
  const { root } = createDom();
  const selected = [];
  const grid = mountReportGrid(root, { items: items(), onSelect: (id) => selected.push(id) });

  clickRegion(root, 'region-a');
  grid.update({ items: items() });

  assert.deepEqual(selected, ['region-a']);
});

test('update 后选中区域消失时调用 onSelect(null)', () => {
  const { root } = createDom();
  const selected = [];
  const grid = mountReportGrid(root, { items: items(), onSelect: (id) => selected.push(id) });

  clickRegion(root, 'region-a');
  grid.update({ items: [items()[1]] });

  assert.deepEqual(selected, ['region-a', null]);
});
