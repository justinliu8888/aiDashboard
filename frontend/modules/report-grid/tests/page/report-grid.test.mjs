/**
 * G 网格页面模块 · 挂载与只读态
 *
 * 契约来源：frontend/modules/report-grid/CONTRACT.md 第 3 节、第 4 节。
 * 只通过公开接口 mountReportGrid(root, opts) 观察行为。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { createDom } from './dom-stub.mjs';
import { mountReportGrid } from '../../page/report-grid.mjs';

function makeItems() {
  return [
    {
      regionId: 'region-a',
      label: '区域 A · 能耗趋势',
      placement: { type: 'GRID', column: 1, row: 1, columnSpan: 6, rowSpan: 2 },
    },
    {
      regionId: 'region-b',
      label: '区域 B · 设备占比',
      placement: { type: 'GRID', column: 7, row: 1, columnSpan: 6, rowSpan: 3 },
    },
    {
      regionId: 'region-c',
      label: '区域 C · 运行日志',
      placement: { type: 'GRID', column: 1, row: 3, columnSpan: 12, rowSpan: 2 },
    },
  ];
}

test('缺省 readonly 为 true，挂载后根节点处于只读态', () => {
  const { document, root } = createDom();
  const grid = mountReportGrid(root, { items: makeItems() });

  assert.equal(root.classList.contains('rg-root'), true);
  assert.equal(root.hasAttribute('data-rg-root'), true);
  assert.equal(root.getAttribute('data-rg-readonly'), 'true');
  assert.equal(root.hasAttribute('data-rg-editable'), false);
  assert.ok(grid);
});

test('只读态不渲染缩放手柄', () => {
  const { document, root } = createDom();
  mountReportGrid(root, { items: makeItems() });

  assert.equal(root.querySelectorAll('.rg-handle').length, 0);
});

test('显式 readonly:false 进入编辑态并渲染 e/s/se 手柄', () => {
  const { document, root } = createDom();
  mountReportGrid(root, { items: makeItems(), readonly: false });

  assert.equal(root.getAttribute('data-rg-editable'), 'true');
  assert.equal(root.hasAttribute('data-rg-readonly'), false);

  const handles = root.querySelectorAll('.rg-handle');
  assert.equal(handles.length, 9);
  const kinds = new Set(handles.map((el) => el.getAttribute('data-rg-handle')));
  assert.deepEqual([...kinds].sort(), ['e', 's', 'se']);
});

test('区域按 data-region-id 渲染，并带 CSS Grid 坐标变量', () => {
  const { document, root } = createDom();
  mountReportGrid(root, { items: makeItems() });

  const regions = root.querySelectorAll('.rg-region');
  assert.equal(regions.length, 3);

  const a = root.querySelector('.rg-region[data-region-id="region-a"]');
  assert.ok(a);
  assert.equal(a.getAttribute('data-rg-region'), 'region-a');
  assert.equal(a.style.getPropertyValue('--rg-column'), '1');
  assert.equal(a.style.getPropertyValue('--rg-column-span'), '6');
  assert.equal(a.style.getPropertyValue('--rg-row'), '1');
  assert.equal(a.style.getPropertyValue('--rg-row-span'), '2');
});

test('网格容器带 data-rg-grid', () => {
  const { document, root } = createDom();
  mountReportGrid(root, { items: makeItems() });

  const grid = root.querySelector('.rg-grid');
  assert.ok(grid);
  assert.equal(grid.getAttribute('data-rg-grid'), '');
});

test('renderContent 收到 regionId / selected / readonly', () => {
  const { document, root } = createDom();
  const calls = [];
  mountReportGrid(root, {
    items: makeItems(),
    readonly: false,
    renderContent: (item, ctx) => {
      calls.push({ regionId: item.regionId, ...ctx });
      return document.createElement('span');
    },
  });

  assert.equal(calls.length, 3);
  assert.deepEqual(calls[0], { regionId: 'region-a', selected: false, readonly: false });
});
