/**
 * G 网格页面模块 · 取消、只读加锁、销毁与视图更新
 *
 * 契约来源：CONTRACT.md 3.8（只读中途加锁）、3.9（取消）、3.10（update）、
 * 3.11（destroy）、3.15（窗口变窄只重算渲染尺寸，不改坐标）。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { createDom, makeEvent, makePointerEvent } from './dom-stub.mjs';
import { mountReportGrid } from '../../page/report-grid.mjs';

const GRID_WIDTH = 1200;

function items() {
  return [
    { regionId: 'region-a', placement: { type: 'GRID', column: 1, row: 1, columnSpan: 6, rowSpan: 2 } },
    { regionId: 'region-b', placement: { type: 'GRID', column: 7, row: 1, columnSpan: 6, rowSpan: 3 } },
  ];
}

function setup({ readonly = false } = {}) {
  const dom = createDom();
  const intents = [];
  const selects = [];
  const grid = mountReportGrid(dom.root, {
    items: items(),
    readonly,
    onIntent: (payload) => intents.push(payload),
    onSelect: (id) => selects.push(id),
  });
  dom.setGridWidth(GRID_WIDTH);
  return { ...dom, intents, selects, grid };
}

function regionEl(root, id) {
  return root.querySelector(`.rg-region[data-region-id="${id}"]`);
}

function startDrag(dom, id, { x = 100, y = 100 } = {}) {
  const region = regionEl(dom.root, id);
  region.dispatchEvent(makePointerEvent('pointerdown', { x, y }));
  region.dispatchEvent(makePointerEvent('pointermove', { x: x + 202, y: y + 120 }));
  return region;
}

test('pointercancel 取消手势，不回调且丢弃预览', () => {
  const dom = setup();
  const region = startDrag(dom, 'region-a');
  assert.ok(dom.root.querySelectorAll('.rg-preview').length >= 1);

  region.dispatchEvent(makePointerEvent('pointercancel', { x: 300, y: 220 }));

  assert.equal(dom.intents.length, 0);
  assert.equal(dom.root.querySelectorAll('.rg-preview').length, 0);
  assert.equal(region.getAttribute('data-rg-dragging'), null);
});

test('Escape 取消手势，不回调且丢弃预览', () => {
  const dom = setup();
  startDrag(dom, 'region-a');

  dom.document.dispatchEvent(makeEvent('keydown', { key: 'Escape' }));

  assert.equal(dom.intents.length, 0);
  assert.equal(dom.root.querySelectorAll('.rg-preview').length, 0);
});

test('手势中途 update({readonly:true}) 立即加锁，不回调且丢弃预览', () => {
  const dom = setup();
  const region = startDrag(dom, 'region-a');

  dom.grid.update({ readonly: true });

  assert.equal(dom.intents.length, 0);
  assert.equal(dom.root.querySelectorAll('.rg-preview').length, 0);
  assert.equal(region.getAttribute('data-rg-dragging'), null);
  assert.equal(dom.root.getAttribute('data-rg-readonly'), 'true');
  assert.equal(dom.root.querySelectorAll('.rg-handle').length, 0);
});

test('手势中途 update 换视图同样取消手势，不回调', () => {
  const dom = setup();
  startDrag(dom, 'region-a');

  dom.grid.update({ items: items() });

  assert.equal(dom.intents.length, 0);
  assert.equal(dom.root.querySelectorAll('.rg-preview').length, 0);
});

test('destroy() 取消手势，不回调', () => {
  const dom = setup();
  startDrag(dom, 'region-a');

  dom.grid.destroy();

  assert.equal(dom.intents.length, 0);
  assert.equal(dom.root.querySelectorAll('.rg-preview').length, 0);
});

test('destroy() 只清理自有 DOM', () => {
  const dom = setup();
  dom.grid.destroy();

  assert.equal(dom.root.querySelectorAll('.rg-region').length, 0);
  assert.equal(dom.root.querySelectorAll('.rg-grid').length, 0);
  assert.equal(dom.root.classList.contains('rg-root'), false);
  assert.equal(dom.root.hasAttribute('data-rg-root'), false);
});

test('destroy() 之后不再响应指针事件，不产生任何回调', () => {
  const dom = setup();
  const region = regionEl(dom.root, 'region-a');
  dom.grid.destroy();

  region.dispatchEvent(makePointerEvent('pointerdown', { x: 10, y: 10 }));
  region.dispatchEvent(makePointerEvent('pointerup', { x: 10, y: 10 }));

  assert.deepEqual(dom.selects, []);
  assert.deepEqual(dom.intents, []);
});

test('destroy() 之后 update() 不再改变 DOM', () => {
  const dom = setup();
  dom.grid.destroy();

  dom.grid.update({ items: items(), readonly: false });

  assert.equal(dom.root.querySelectorAll('.rg-region').length, 0);
  assert.equal(dom.root.hasAttribute('data-rg-editable'), false);
});

test('update 不带 readonly 时沿用当前只读状态', () => {
  const dom = setup({ readonly: false });
  dom.grid.update({ items: items() });
  assert.equal(dom.root.getAttribute('data-rg-editable'), 'true');

  const dom2 = setup({ readonly: true });
  dom2.grid.update({ items: items() });
  assert.equal(dom2.root.getAttribute('data-rg-readonly'), 'true');
});

test('窗口变窄只重算渲染尺寸，区域坐标不变', () => {
  const dom = setup();
  const region = regionEl(dom.root, 'region-a');
  const before = {
    column: region.style.getPropertyValue('--rg-column'),
    row: region.style.getPropertyValue('--rg-row'),
    columnSpan: region.style.getPropertyValue('--rg-column-span'),
    rowSpan: region.style.getPropertyValue('--rg-row-span'),
  };

  dom.setGridWidth(600);
  dom.triggerResize();

  assert.equal(region.style.getPropertyValue('--rg-column'), before.column);
  assert.equal(region.style.getPropertyValue('--rg-row'), before.row);
  assert.equal(region.style.getPropertyValue('--rg-column-span'), before.columnSpan);
  assert.equal(region.style.getPropertyValue('--rg-row-span'), before.rowSpan);

  const grid = dom.root.querySelector('.rg-grid');
  assert.equal(grid.style.getPropertyValue('--rg-col-width'), '39px');
});
