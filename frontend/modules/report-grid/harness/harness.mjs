/**
 * G 网格模块测试页 · 演示宿主
 *
 * 本页是**演示宿主**，不是生产工作台：
 *   - 用本地视图接受模块的 onIntent 回调：把受影响区域的 placement 应用到本地视图，
 *     再调用 update() 回灌模块，用来回显「新的视图」。
 *   - 不发 HTTP、不落库、不调用模型、不代表任何内容已被保存。
 *   - 区域内容是测试占位文字，不含真实客户数据。
 *
 * 对外暴露 window.__rgHarness 供检查脚本**只读**观察状态（回调次数、placement、模式）。
 */

import { mountReportGrid } from '../page/report-grid.mjs';

const host = document.getElementById('rg-host');
const statusEl = document.getElementById('rg-status');
const intentLogEl = document.getElementById('rg-intent-log');
const selectLogEl = document.getElementById('rg-select-log');

/** 默认布局：4 个测试区域 */
function defaultItems() {
  return [
    {
      regionId: 'region-a',
      label: '区域 A · 能耗趋势',
      placement: { type: 'GRID', column: 1, row: 1, columnSpan: 6, rowSpan: 3 },
    },
    {
      regionId: 'region-b',
      label: '区域 B · 设备占比',
      placement: { type: 'GRID', column: 7, row: 1, columnSpan: 6, rowSpan: 2 },
    },
    {
      regionId: 'region-c',
      label: '区域 C · 运行日志',
      placement: { type: 'GRID', column: 1, row: 4, columnSpan: 12, rowSpan: 2 },
    },
    {
      regionId: 'region-d',
      label: '区域 D · 巡检记录',
      placement: { type: 'GRID', column: 7, row: 3, columnSpan: 6, rowSpan: 3 },
    },
  ];
}

/** 碰撞布局：区域 A 与区域 B 相邻且横向重叠，留给后续碰撞验证 */
function collisionItems() {
  return [
    {
      regionId: 'region-a',
      label: '区域 A · 能耗趋势',
      placement: { type: 'GRID', column: 1, row: 1, columnSpan: 6, rowSpan: 2 },
    },
    {
      regionId: 'region-b',
      label: '区域 B · 设备占比',
      placement: { type: 'GRID', column: 5, row: 1, columnSpan: 6, rowSpan: 2 },
    },
    {
      regionId: 'region-c',
      label: '区域 C · 运行日志',
      placement: { type: 'GRID', column: 1, row: 3, columnSpan: 12, rowSpan: 2 },
    },
    {
      regionId: 'region-d',
      label: '区域 D · 巡检记录',
      placement: { type: 'GRID', column: 1, row: 5, columnSpan: 12, rowSpan: 2 },
    },
  ];
}

let items = defaultItems();
let readonly = true;
let instance = null;
const intentLog = [];
const selectLog = [];

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[ch]);
}

/** 区域内容插槽：region-c 走 HTML 字符串分支，其余走 Node 分支 */
function renderContent(item, { regionId, selected, readonly: isReadonly }) {
  if (regionId === 'region-c') {
    return (
      `<div class="rg-harness-card"><strong>${escapeHtml(item.label)}</strong>` +
      `<p>演示占位内容 · ${isReadonly ? '只读' : '编辑'} · ${selected ? '已选中' : '未选中'}</p></div>`
    );
  }
  const box = document.createElement('div');
  box.className = 'rg-harness-card';
  const title = document.createElement('strong');
  title.textContent = item.label || regionId;
  const note = document.createElement('p');
  note.textContent = `演示占位内容 · ${isReadonly ? '只读' : '编辑'} · ${selected ? '已选中' : '未选中'}`;
  box.append(title, note);
  return box;
}

function renderLog() {
  intentLogEl.replaceChildren();
  for (const entry of intentLog) {
    const li = document.createElement('li');
    const places = entry.placements
      .map((p) => `${p.regionId}(列${p.column} 行${p.row} 宽${p.columnSpan} 高${p.rowSpan})`)
      .join('、');
    li.textContent = `${entry.kind} · 目标 ${entry.regionId} · 受影响 ${entry.affectedRegionIds.join('、')} · ${places}`;
    intentLogEl.appendChild(li);
  }
  if (!intentLog.length) {
    const li = document.createElement('li');
    li.textContent = '（暂无意图回调）';
    intentLogEl.appendChild(li);
  }
}

function renderSelectLog() {
  selectLogEl.textContent = selectLog.length
    ? `选择记录：${selectLog.map((id) => (id === null ? '取消选择' : id)).join('、')}`
    : '尚未选择区域';
}

function updateStatus() {
  const mode = readonly ? '只读' : '编辑';
  const alive = instance ? '已挂载' : '已销毁';
  statusEl.textContent = `模块：${alive} · 当前模式：${mode} · 本地视图区域数：${items.length} · 未保存`;
}

/** 演示宿主：用本地视图接受意图，再把新视图写回模块 */
function handleIntent(payload) {
  const byId = new Map(items.map((item) => [item.regionId, item]));
  for (const placement of payload.placements) {
    const prev = byId.get(placement.regionId);
    if (!prev) continue;
    byId.set(placement.regionId, {
      ...prev,
      placement: {
        type: 'GRID',
        column: placement.placement.column,
        row: placement.placement.row,
        columnSpan: placement.placement.columnSpan,
        rowSpan: placement.placement.rowSpan,
      },
    });
  }
  items = items.map((item) => byId.get(item.regionId) || item);

  intentLog.push({
    kind: payload.kind,
    regionId: payload.regionId,
    affectedRegionIds: [...payload.affectedRegionIds],
    placements: payload.placements.map((p) => ({ regionId: p.regionId, ...p.placement })),
  });
  renderLog();

  if (instance) instance.update({ items });
  updateStatus();
}

function mount() {
  if (instance) instance.destroy();
  instance = mountReportGrid(host, {
    items,
    readonly,
    renderContent,
    onIntent: handleIntent,
    onSelect: (regionId) => {
      selectLog.push(regionId);
      renderSelectLog();
      updateStatus();
    },
  });
  updateStatus();
}

function toggleMode() {
  readonly = !readonly;
  if (instance) instance.update({ readonly });
  document.getElementById('rg-toggle-mode').textContent = readonly ? '切换到编辑态' : '切换到只读态';
  updateStatus();
}

function destroyModule() {
  if (!instance) return;
  instance.destroy();
  instance = null;
  updateStatus();
}

function rebuildModule() {
  mount();
}

function loadCollision() {
  items = collisionItems();
  if (instance) instance.update({ items });
  updateStatus();
}

function resetLayout() {
  items = defaultItems();
  if (instance) instance.update({ items });
  updateStatus();
}

document.getElementById('rg-toggle-mode').addEventListener('click', toggleMode);
document.getElementById('rg-destroy').addEventListener('click', destroyModule);
document.getElementById('rg-rebuild').addEventListener('click', rebuildModule);
document.getElementById('rg-collision').addEventListener('click', loadCollision);
document.getElementById('rg-reset').addEventListener('click', resetLayout);

mount();
renderLog();
renderSelectLog();

/** 供检查脚本只读观察状态 */
window.__rgHarness = {
  ready: true,
  get mode() {
    return readonly ? 'readonly' : 'editable';
  },
  get alive() {
    return instance !== null;
  },
  get intentCount() {
    return intentLog.length;
  },
  get intents() {
    return intentLog.map((entry) => ({
      kind: entry.kind,
      regionId: entry.regionId,
      affectedRegionIds: [...entry.affectedRegionIds],
      placements: entry.placements.map((p) => ({ ...p })),
    }));
  },
  get selections() {
    return [...selectLog];
  },
  get placements() {
    return items.map((item) => ({ regionId: item.regionId, ...item.placement }));
  },
  get regionIds() {
    return items.map((item) => item.regionId);
  },
  get bannerText() {
    return document.querySelector('.rg-harness-banner strong').textContent;
  },
};
