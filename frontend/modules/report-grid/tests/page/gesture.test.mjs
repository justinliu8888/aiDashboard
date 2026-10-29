//
// 运行：node --test frontend/modules/report-grid/tests/page/*.test.mjs
// （目录参数在 Node 24 上不匹配默认测试文件规则，要显式给 glob；
//   跑单个文件 node --test frontend/modules/report-grid/tests/page/gesture.test.mjs 也可以）
// 不依赖任何第三方包（用 Node 内置 test runner），不改动共用依赖配置。
/**
 * G 网格页面模块 · 拖拽与缩放手势
 *
 * 契约来源：CONTRACT.md 3.3（一次手势一次回调）、3.4（回调载荷）、3.5（零变更不回调）、
 * 3.6（回调只是提案）、3.7（落位示意仅是临时状态）。
 *
 * 几何约定（契约第 3 节 + 网格几何）：
 *   列宽 = (网格容器宽 - gap * (columns - 1)) / columns
 *   列距 = 列宽 + gap，行距 = rowUnitPx + gap
 * 本文件用容器宽 1200 计算：列宽 89、列距 101、行距 60。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { createDom, makePointerEvent } from './dom-stub.mjs';
import { mountReportGrid } from '../../page/report-grid.mjs';

const GRID_WIDTH = 1200;
const COL_PITCH = 101;
const ROW_PITCH = 60;

function items() {
  return [
    { regionId: 'region-a', placement: { type: 'GRID', column: 1, row: 1, columnSpan: 6, rowSpan: 2 } },
    { regionId: 'region-b', placement: { type: 'GRID', column: 7, row: 1, columnSpan: 6, rowSpan: 3 } },
    { regionId: 'region-c', placement: { type: 'GRID', column: 1, row: 3, columnSpan: 12, rowSpan: 2 } },
  ];
}

function setup(options = {}) {
  const dom = createDom();
  const intents = [];
  const selects = [];
  const grid = mountReportGrid(dom.root, {
    items: items(),
    readonly: false,
    onIntent: (payload) => intents.push(payload),
    onSelect: (id) => selects.push(id),
    ...options,
  });
  dom.setGridWidth(GRID_WIDTH); // 挂载后才有 .rg-grid，宽度必须后设
  return { ...dom, intents, selects, grid };
}

function regionEl(root, id) {
  return root.querySelector(`.rg-region[data-region-id="${id}"]`);
}

function handleEl(root, id, kind) {
  const region = regionEl(root, id);
  return region.querySelector(`.rg-handle[data-rg-handle="${kind}"]`);
}

function drag(dom, target, from, to) {
  target.dispatchEvent(makePointerEvent('pointerdown', { x: from.x, y: from.y }));
  const steps = 4;
  for (let i = 1; i <= steps; i += 1) {
    const x = from.x + ((to.x - from.x) * i) / steps;
    const y = from.y + ((to.y - from.y) * i) / steps;
    target.dispatchEvent(makePointerEvent('pointermove', { x, y }));
  }
  target.dispatchEvent(makePointerEvent('pointerup', { x: to.x, y: to.y }));
}

test('编辑态拖动区域，pointerup 时只回调一次 move 意图', () => {
  const dom = setup();
  drag(dom, regionEl(dom.root, 'region-a'), { x: 100, y: 100 }, { x: 100 + COL_PITCH, y: 100 });

  assert.equal(dom.intents.length, 1);
  assert.equal(dom.intents[0].kind, 'move');
  assert.equal(dom.intents[0].regionId, 'region-a');
});

test('pointermove 过程不回调，只在 pointerup 回调一次', () => {
  const dom = setup();
  const region = regionEl(dom.root, 'region-a');
  region.dispatchEvent(makePointerEvent('pointerdown', { x: 100, y: 100 }));
  for (let i = 1; i <= 5; i += 1) {
    region.dispatchEvent(makePointerEvent('pointermove', { x: 100 + i * 20, y: 100 }));
  }
  assert.equal(dom.intents.length, 0);

  region.dispatchEvent(makePointerEvent('pointerup', { x: 100 + COL_PITCH, y: 100 }));
  assert.equal(dom.intents.length, 1);
});

test('手势结束后不会重复回调', () => {
  const dom = setup();
  const region = regionEl(dom.root, 'region-a');
  drag(dom, region, { x: 100, y: 100 }, { x: 100 + COL_PITCH, y: 100 });
  region.dispatchEvent(makePointerEvent('pointerup', { x: 100 + COL_PITCH, y: 100 }));
  region.dispatchEvent(makePointerEvent('pointermove', { x: 140, y: 140 }));

  assert.equal(dom.intents.length, 1);
});

test('move 意图带新的 column 与 row', () => {
  const dom = setup();
  drag(dom, regionEl(dom.root, 'region-c'), { x: 100, y: 400 }, { x: 100, y: 400 + ROW_PITCH });

  const payload = dom.intents[0];
  assert.equal(payload.kind, 'move');
  assert.equal(payload.regionId, 'region-c');
  const moved = payload.placements.find((p) => p.regionId === 'region-c');
  assert.equal(moved.placement.column, 1);
  assert.equal(moved.placement.row, 4);
});

test('placements 只含受影响区域，不含无关组件', () => {
  const dom = setup();
  drag(dom, regionEl(dom.root, 'region-c'), { x: 100, y: 400 }, { x: 100, y: 400 + ROW_PITCH });

  const payload = dom.intents[0];
  assert.deepEqual(payload.placements.map((p) => p.regionId), ['region-c']);
  assert.deepEqual(payload.affectedRegionIds, ['region-c']);
});

test('零变更不回调，视图回到输入布局', () => {
  const dom = setup();
  const region = regionEl(dom.root, 'region-a');
  drag(dom, region, { x: 100, y: 100 }, { x: 100 + 40, y: 100 + 20 });

  assert.equal(dom.intents.length, 0);
  assert.equal(region.style.getPropertyValue('--rg-column'), '1');
  assert.equal(region.style.getPropertyValue('--rg-row'), '1');
});

test('拖动超出右边界时 column 被夹在 columns - columnSpan + 1', () => {
  const dom = setup();
  drag(dom, regionEl(dom.root, 'region-a'), { x: 100, y: 100 }, { x: 1100, y: 100 });

  const payload = dom.intents[0];
  const moved = payload.placements.find((p) => p.regionId === 'region-a');
  assert.equal(moved.placement.column, 7);
});

test('缩放手柄 e：只改 columnSpan', () => {
  const dom = setup();
  drag(dom, handleEl(dom.root, 'region-a', 'e'), { x: 100, y: 100 }, { x: 100 + COL_PITCH, y: 100 });

  const payload = dom.intents[0];
  assert.equal(payload.kind, 'resize');
  assert.equal(payload.regionId, 'region-a');
  const resized = payload.placements.find((p) => p.regionId === 'region-a');
  assert.equal(resized.placement.column, 1);
  assert.equal(resized.placement.columnSpan, 7);
  assert.equal(resized.placement.rowSpan, 2);
});

test('缩放手柄 s：只改 rowSpan', () => {
  const dom = setup();
  drag(dom, handleEl(dom.root, 'region-a', 's'), { x: 100, y: 100 }, { x: 100, y: 100 + ROW_PITCH });

  const payload = dom.intents[0];
  const resized = payload.placements.find((p) => p.regionId === 'region-a');
  assert.equal(resized.placement.rowSpan, 3);
  assert.equal(resized.placement.columnSpan, 6);
});

test('缩放手柄 se：同时改 columnSpan 与 rowSpan', () => {
  const dom = setup();
  drag(dom, handleEl(dom.root, 'region-a', 'se'), { x: 100, y: 100 }, { x: 100 + COL_PITCH, y: 100 + ROW_PITCH });

  const payload = dom.intents[0];
  const resized = payload.placements.find((p) => p.regionId === 'region-a');
  assert.equal(resized.placement.columnSpan, 7);
  assert.equal(resized.placement.rowSpan, 3);
});

test('缩到最小尺寸时不会小于生效最小跨列', () => {
  const dom = createDom();
  const intents = [];
  mountReportGrid(dom.root, {
    readonly: false,
    onIntent: (payload) => intents.push(payload),
    items: [
      {
        regionId: 'region-a',
        minColumnSpan: 2,
        placement: { type: 'GRID', column: 1, row: 1, columnSpan: 6, rowSpan: 2 },
      },
    ],
  });
  dom.setGridWidth(GRID_WIDTH);

  drag(dom, handleEl(dom.root, 'region-a', 'e'), { x: 100, y: 100 }, { x: 100 - 1000, y: 100 });

  const resized = intents[0].placements.find((p) => p.regionId === 'region-a');
  assert.equal(resized.placement.columnSpan, 2);
});

test('手势过程中显示落位示意，松手后示意消失并回到输入布局', () => {
  const dom = setup();
  const region = regionEl(dom.root, 'region-c');
  region.dispatchEvent(makePointerEvent('pointerdown', { x: 100, y: 400 }));
  region.dispatchEvent(makePointerEvent('pointermove', { x: 100, y: 400 + ROW_PITCH }));

  const previews = dom.root.querySelectorAll('.rg-drop-ghost');
  assert.ok(previews.length >= 1);
  assert.ok(previews.some((el) => el.getAttribute('data-rg-drop-ghost') === 'region-c'));
  assert.equal(region.getAttribute('data-rg-dragging'), 'true');

  region.dispatchEvent(makePointerEvent('pointerup', { x: 100, y: 400 + ROW_PITCH }));

  assert.equal(dom.root.querySelectorAll('.rg-drop-ghost').length, 0);
  assert.equal(region.getAttribute('data-rg-dragging'), null);
  assert.equal(region.style.getPropertyValue('--rg-row'), '3');
});

test('只读态拖动不触发 onIntent', () => {
  const dom = createDom();
  const intents = [];
  mountReportGrid(dom.root, { items: items(), onIntent: (payload) => intents.push(payload) });
  dom.setGridWidth(GRID_WIDTH);

  drag(dom, regionEl(dom.root, 'region-a'), { x: 100, y: 100 }, { x: 100 + COL_PITCH, y: 100 });

  assert.equal(intents.length, 0);
});

test('onIntent 载荷不含 operationId / revision', () => {
  const dom = setup();
  drag(dom, regionEl(dom.root, 'region-c'), { x: 100, y: 400 }, { x: 100, y: 400 + ROW_PITCH });

  const payload = dom.intents[0];
  assert.deepEqual(Object.keys(payload).sort(), ['affectedRegionIds', 'kind', 'placements', 'regionId']);
});
