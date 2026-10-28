/**
 * G 独立网格模块 · 页面挂载与鼠标交互
 *
 * 契约来源：frontend/modules/report-grid/CONTRACT.md（第 3 节 页面行为、第 4 节 DOM 契约）。
 *
 * 职责边界：
 *   - 本模块负责：展示布局、处理真实鼠标拖拽与缩放、把「移动 / 缩放意图」回调给宿主。
 *   - 宿主负责：设计、保存、AI、保存视图回灌。
 *   - 本模块**不**发 HTTP、**不**自带 echarts、**不**复制完整报表 renderer。
 *
 * 公开接口（冻结）：
 *   mountReportGrid(root, { items, readonly, config, renderContent, onIntent, onSelect })
 *     -> { update(view), destroy() }
 *
 * 样式：宿主需自行引入 ../styles/report-grid.css（本模块的 CSS 全部限定在 .rg-root 之下）。
 */

import { resolveGridLayout, DEFAULT_GRID_CONFIG, GRID_COLUMNS } from '../layout/grid-layout.mjs';

/** 缩放手柄种类：右侧 / 底部 / 右下角 */
const HANDLE_KINDS = Object.freeze(['e', 's', 'se']);

/** 视为「点击」的位移阈值（px） */
const CLICK_THRESHOLD_PX = 3;

function isNodeLike(value) {
  return Boolean(value) && typeof value === 'object' && typeof value.nodeType === 'number';
}

function clamp(value, min, max) {
  if (max < min) return min;
  return Math.min(Math.max(value, min), max);
}

function positiveInt(value) {
  return Number.isInteger(value) && value >= 1 ? value : null;
}

/**
 * 挂载网格页面。
 *
 * @param {Element} root 宿主提供的根元素（模块会加上 .rg-root）
 * @param {object} [options]
 * @param {Array}  [options.items]          区域列表（只读使用，不修改）
 * @param {boolean} [options.readonly=true] 只读态；只有显式 false 才进入编辑态
 * @param {object} [options.config]         展示配置，与 DEFAULT_GRID_CONFIG 浅合并
 * @param {Function} [options.renderContent] (item, { regionId, selected, readonly }) => Node | string | undefined
 * @param {Function} [options.onIntent]     (payload) => void，布局提案，不是已保存结果
 * @param {Function} [options.onSelect]     (regionId | null) => void
 * @returns {{ update: Function, destroy: Function }}
 */
export function mountReportGrid(root, options = {}) {
  if (!root || typeof root !== 'object') {
    throw new TypeError('mountReportGrid 需要一个根元素');
  }

  const doc = root.ownerDocument;
  if (!doc || typeof doc.createElement !== 'function') {
    throw new TypeError('mountReportGrid 的根元素必须属于一个 document');
  }

  const state = {
    items: Array.isArray(options.items) ? options.items : [],
    readonly: options.readonly === false ? false : true,
    config: { ...DEFAULT_GRID_CONFIG, ...(options.config || {}) },
    selectedRegionId: null,
    renderContent: typeof options.renderContent === 'function' ? options.renderContent : null,
    onIntent: typeof options.onIntent === 'function' ? options.onIntent : null,
    onSelect: typeof options.onSelect === 'function' ? options.onSelect : null,
    destroyed: false,
  };

  // 契约 §1.1/§1.4：列数固定 12，不随 config 变化，避免渲染与几何校验口径不一致
  const columns = GRID_COLUMNS;
  const regionEntries = new Map();
  const dropGhostEls = new Map();
  let currentPlacements = new Map();

  /* ------------------------------------------------------------ DOM 骨架 */

  root.classList.add('rg-root');
  root.setAttribute('data-rg-root', '');

  const grid = doc.createElement('div');
  grid.className = 'rg-grid';
  grid.setAttribute('data-rg-grid', '');
  root.appendChild(grid);

  const gridlines = doc.createElement('div');
  gridlines.className = 'rg-gridlines';
  gridlines.setAttribute('aria-hidden', 'true');
  grid.appendChild(gridlines);

  /* ------------------------------------------------------------ 渲染 */

  function syncRootState() {
    if (state.readonly) {
      root.setAttribute('data-rg-readonly', 'true');
      root.removeAttribute('data-rg-editable');
    } else {
      root.setAttribute('data-rg-editable', 'true');
      root.removeAttribute('data-rg-readonly');
    }
    // 注意：长度型变量必须带单位，否则 grid-auto-rows / gap 会因非法值而失效
    grid.style.setProperty('--rg-columns', String(columns));
    grid.style.setProperty('--rg-row-unit', `${state.config.rowUnitPx}px`);
    grid.style.setProperty('--rg-gap', `${state.config.gapPx}px`);
    grid.style.setProperty('--rg-row-pitch', `${state.config.rowUnitPx + state.config.gapPx}px`);
  }

  /**
   * 输入不合法时退回「只渲染自身 placement 合法的区域」，避免整页崩掉。
   * 合法性口径与布局层一致：GRID、1 基正整数、横向不越界（固定 12 列）。
   * 重复 regionId 保留第一个，不静默覆盖成另一个。
   */
  function computeDisplayPlacements() {
    const map = new Map();
    const resolved = resolveGridLayout({ items: state.items, intent: null, config: state.config });
    if (resolved.kind === 'success') {
      for (const entry of resolved.placements) map.set(entry.regionId, entry.placement);
      return map;
    }
    for (const item of state.items) {
      if (!item || typeof item.regionId !== 'string' || item.regionId === '') continue;
      const p = item.placement;
      if (!p || p.type !== 'GRID') continue;
      if (!positiveInt(p.column) || !positiveInt(p.row)) continue;
      if (!positiveInt(p.columnSpan) || !positiveInt(p.rowSpan)) continue;
      if (p.column + p.columnSpan - 1 > columns) continue;
      if (map.has(item.regionId)) continue;
      map.set(item.regionId, {
        type: 'GRID', column: p.column, row: p.row, columnSpan: p.columnSpan, rowSpan: p.rowSpan,
      });
    }
    return map;
  }

  function applyPlacement(el, placement) {
    el.style.setProperty('--rg-column', String(placement.column));
    el.style.setProperty('--rg-column-span', String(placement.columnSpan));
    el.style.setProperty('--rg-row', String(placement.row));
    el.style.setProperty('--rg-row-span', String(placement.rowSpan));
  }

  function syncHandles(entry) {
    const wanted = state.readonly ? [] : HANDLE_KINDS;
    for (const [kind, el] of [...entry.handles]) {
      if (!wanted.includes(kind)) {
        el.remove();
        entry.handles.delete(kind);
      }
    }
    for (const kind of wanted) {
      if (entry.handles.has(kind)) continue;
      const el = doc.createElement('div');
      el.className = 'rg-handle';
      el.setAttribute('data-rg-handle', kind);
      entry.el.appendChild(el);
      entry.handles.set(kind, el);
    }
  }

  function renderContent(entry, item, selected) {
    const box = entry.content;
    box.replaceChildren();
    const out = state.renderContent
      ? state.renderContent(item, { regionId: item.regionId, selected, readonly: state.readonly })
      : undefined;
    if (isNodeLike(out)) {
      box.appendChild(out);
    } else if (typeof out === 'string') {
      box.innerHTML = out;
    } else if (item.label) {
      box.textContent = item.label;
    }
  }

  function render() {
    if (state.destroyed) return;
    syncRootState();

    const placements = computeDisplayPlacements();
    currentPlacements = placements;

    for (const [id, entry] of [...regionEntries]) {
      if (!placements.has(id)) {
        entry.el.remove();
        regionEntries.delete(id);
      }
    }

    for (const item of state.items) {
      if (!item || typeof item.regionId !== 'string' || item.regionId === '') continue;
      const placement = placements.get(item.regionId);
      if (!placement) continue;

      let entry = regionEntries.get(item.regionId);
      if (!entry) {
        const el = doc.createElement('div');
        el.className = 'rg-region';
        el.setAttribute('data-region-id', item.regionId);
        el.setAttribute('data-rg-region', item.regionId);
        const content = doc.createElement('div');
        content.className = 'rg-region-content';
        el.appendChild(content);
        entry = {
          el,
          content,
          handles: new Map(),
          renderedItem: null,
          renderedSelected: null,
          renderedReadonly: null,
        };
        regionEntries.set(item.regionId, entry);
        grid.appendChild(el);
      }

      applyPlacement(entry.el, placement);

      const selected = state.selectedRegionId === item.regionId;
      if (selected) entry.el.setAttribute('data-rg-selected', 'true');
      else entry.el.removeAttribute('data-rg-selected');

      if (
        entry.renderedItem !== item ||
        entry.renderedSelected !== selected ||
        entry.renderedReadonly !== state.readonly
      ) {
        renderContent(entry, item, selected);
        entry.renderedItem = item;
        entry.renderedSelected = selected;
        entry.renderedReadonly = state.readonly;
      }

      syncHandles(entry);
    }
  }

  render();

  /* ------------------------------------------------------------ 选择 */

  /** 返回是否真的发生了变化（未变化时不回调 onSelect） */
  function setSelection(regionId) {
    if (state.selectedRegionId === regionId) return false;
    state.selectedRegionId = regionId;
    if (state.onSelect) state.onSelect(regionId);
    return true;
  }

  /* ------------------------------------------------------------ 手势 */

  const gesture = {
    active: false,
    kind: null,
    handle: null,
    regionId: null,
    startX: 0,
    startY: 0,
    startPlacement: null,
    metrics: null,
    el: null,
    pointerId: null,
  };

  const pointer = { active: false, regionId: null, startX: 0, startY: 0 };

  function clientX(ev) {
    return typeof ev.clientX === 'number' ? ev.clientX : 0;
  }

  function clientY(ev) {
    return typeof ev.clientY === 'number' ? ev.clientY : 0;
  }

  function itemOf(regionId) {
    for (const item of state.items) {
      if (item && item.regionId === regionId) return item;
    }
    return null;
  }

  function regionIdFromEvent(ev) {
    const el = ev.target && typeof ev.target.closest === 'function' ? ev.target.closest('.rg-region') : null;
    if (!el) return null;
    return el.getAttribute('data-region-id') || null;
  }

  function handleKindFromEvent(ev) {
    const el = ev.target && typeof ev.target.closest === 'function' ? ev.target.closest('.rg-handle') : null;
    if (!el) return null;
    const kind = el.getAttribute('data-rg-handle');
    return HANDLE_KINDS.includes(kind) ? kind : null;
  }

  /** 当前网格的像素度量（每次手势开始时重新测量，窗口变窄只会让每列更窄） */
  function measure() {
    const rect = typeof grid.getBoundingClientRect === 'function' ? grid.getBoundingClientRect() : null;
    const width = rect && typeof rect.width === 'number' ? rect.width : 0;
    const gap = Number(state.config.gapPx) || 0;
    const colWidth = Math.max(1, (width - gap * (columns - 1)) / columns);
    const rowUnit = Number(state.config.rowUnitPx) || 0;
    return {
      colWidth,
      colPitch: colWidth + gap,
      rowPitch: rowUnit + gap,
    };
  }

  /** 刷新网格线用的列距变量（只影响渲染尺寸，不改坐标） */
  function refreshGridMetrics() {
    const metrics = measure();
    grid.style.setProperty('--rg-col-width', `${Math.round(metrics.colWidth * 100) / 100}px`);
    grid.style.setProperty('--rg-col-pitch', `${Math.round(metrics.colPitch * 100) / 100}px`);
  }

  function minSpansOf(item) {
    return {
      minColumnSpan: positiveInt(item && item.minColumnSpan) || positiveInt(state.config.defaultMinColumnSpan) || 1,
      minRowSpan: positiveInt(item && item.minRowSpan) || positiveInt(state.config.defaultMinRowSpan) || 1,
    };
  }

  /** 由当前位移算出布局意图；无变化返回 null */
  function computeIntent(dx, dy) {
    if (!gesture.active || !gesture.startPlacement) return null;
    const p = gesture.startPlacement;
    const metrics = gesture.metrics;
    if (!metrics) return null;

    if (gesture.kind === 'move') {
      const deltaColumns = Math.round(dx / metrics.colPitch);
      const deltaRows = Math.round(dy / metrics.rowPitch);
      const column = clamp(p.column + deltaColumns, 1, columns - p.columnSpan + 1);
      const row = Math.max(1, p.row + deltaRows);
      if (column === p.column && row === p.row) return null;
      return { kind: 'move', regionId: gesture.regionId, column, row };
    }

    const { minColumnSpan, minRowSpan } = minSpansOf(itemOf(gesture.regionId));
    let columnSpan = p.columnSpan;
    let rowSpan = p.rowSpan;
    if (gesture.handle === 'e' || gesture.handle === 'se') {
      columnSpan = clamp(
        p.columnSpan + Math.round(dx / metrics.colPitch),
        minColumnSpan,
        columns - p.column + 1,
      );
    }
    if (gesture.handle === 's' || gesture.handle === 'se') {
      rowSpan = Math.max(minRowSpan, p.rowSpan + Math.round(dy / metrics.rowPitch));
    }
    if (columnSpan === p.columnSpan && rowSpan === p.rowSpan) return null;
    return {
      kind: 'resize',
      regionId: gesture.regionId,
      column: p.column,
      row: p.row,
      columnSpan,
      rowSpan,
    };
  }

  /* ------------------------------------------------------------ 落位示意 */

  function clearDropGhosts() {
    for (const el of dropGhostEls.values()) el.remove();
    dropGhostEls.clear();
  }

  function renderDropGhosts(result, ids) {
    for (const [id, el] of [...dropGhostEls]) {
      if (!ids.includes(id)) {
        el.remove();
        dropGhostEls.delete(id);
      }
    }
    for (const id of ids) {
      const entry = result.placements.find((p) => p.regionId === id);
      if (!entry) continue;
      let el = dropGhostEls.get(id);
      if (!el) {
        el = doc.createElement('div');
        el.className = 'rg-drop-ghost';
        el.setAttribute('data-rg-drop-ghost', id);
        grid.appendChild(el);
        dropGhostEls.set(id, el);
      }
      applyPlacement(el, entry.placement);
      const item = itemOf(id);
      el.textContent = (item && item.label) || id;
    }
  }

  function updateDropGhosts(dx, dy) {
    const intent = computeIntent(dx, dy);
    if (!intent) {
      clearDropGhosts();
      return;
    }
    const result = resolveGridLayout({ items: state.items, intent, config: state.config });
    if (result.kind !== 'success') {
      clearDropGhosts();
      return;
    }
    renderDropGhosts(result, result.affectedRegionIds);
  }

  /* ------------------------------------------------------------ 手势收尾 */

  function endGestureVisuals() {
    clearDropGhosts();
    if (gesture.el) {
      gesture.el.removeAttribute('data-rg-dragging');
      if (gesture.pointerId != null && typeof gesture.el.releasePointerCapture === 'function') {
        try {
          gesture.el.releasePointerCapture(gesture.pointerId);
        } catch {
          /* 忽略：指针可能已经释放 */
        }
      }
    }
  }

  function resetGesture() {
    gesture.active = false;
    gesture.kind = null;
    gesture.handle = null;
    gesture.regionId = null;
    gesture.startPlacement = null;
    gesture.metrics = null;
    gesture.el = null;
    gesture.pointerId = null;
  }

  /** 取消手势：丢弃落位示意、回到输入布局、不发回调 */
  function cancelGesture() {
    const wasActive = gesture.active;
    endGestureVisuals();
    resetGesture();
    pointer.active = false;
    pointer.regionId = null;
    if (wasActive) render();
  }

  /** 手势结束：可能回调一次 onIntent */
  function finishGesture(dx, dy) {
    const kind = gesture.kind;
    const regionId = gesture.regionId;
    const moved = Math.hypot(dx, dy) > CLICK_THRESHOLD_PX;
    const intent = moved ? computeIntent(dx, dy) : null;

    endGestureVisuals();
    resetGesture();
    pointer.active = false;
    pointer.regionId = null;

    if (!moved) {
      // 视为点击：只选择，不产生意图
      if (regionId && setSelection(regionId)) render();
      return;
    }

    if (!intent) {
      // 零变更：不回调，视图回到输入布局
      render();
      return;
    }

    const result = resolveGridLayout({ items: state.items, intent, config: state.config });
    if (result.kind !== 'success') {
      render();
      return;
    }

    // 受影响集合直接用布局层的结果，不再在页面层另算一套规则
    const affected = result.affectedRegionIds;
    if (!affected.length) {
      render();
      return;
    }
    const payload = {
      kind: kind === 'move' ? 'move' : 'resize',
      regionId,
      placements: result.placements.filter((p) => affected.includes(p.regionId)),
      affectedRegionIds: affected,
    };

    if (state.onIntent) state.onIntent(payload);

    // 回调只是提案：视图仍以宿主输入为准，等宿主 update() 写入新视图
    render();
  }

  /* ------------------------------------------------------------ 指针事件 */

  function onPointerDown(ev) {
    if (state.destroyed) return;
    if (typeof ev.button === 'number' && ev.button !== 0) return;

    pointer.active = true;
    pointer.regionId = regionIdFromEvent(ev);
    pointer.startX = clientX(ev);
    pointer.startY = clientY(ev);

    if (state.readonly) return; // 只读态不开始手势

    // 上一个手势若没有收到 pointerup（例如指针移出窗口），先收尾，不发回调
    if (gesture.active) {
      endGestureVisuals();
      resetGesture();
    }

    const regionId = pointer.regionId;
    if (!regionId) return;
    const placement = currentPlacements.get(regionId);
    if (!placement) return;

    const handleKind = handleKindFromEvent(ev);
    gesture.active = true;
    gesture.kind = handleKind ? 'resize' : 'move';
    gesture.handle = handleKind;
    gesture.regionId = regionId;
    gesture.startX = pointer.startX;
    gesture.startY = pointer.startY;
    gesture.startPlacement = { ...placement };
    gesture.metrics = measure();
    gesture.pointerId = ev.pointerId == null ? null : ev.pointerId;

    const entry = regionEntries.get(regionId);
    gesture.el = entry ? entry.el : null;
    if (gesture.el) {
      gesture.el.setAttribute('data-rg-dragging', 'true');
      if (gesture.pointerId != null && typeof gesture.el.setPointerCapture === 'function') {
        try {
          gesture.el.setPointerCapture(gesture.pointerId);
        } catch {
          /* 忽略：部分环境不支持指针捕获 */
        }
      }
    }
  }

  function onPointerMove(ev) {
    if (state.destroyed) return;
    if (gesture.active) {
      updateDropGhosts(clientX(ev) - gesture.startX, clientY(ev) - gesture.startY);
      return;
    }
    if (!pointer.active) return;
  }

  function onPointerUp(ev) {
    if (state.destroyed) return;

    if (gesture.active) {
      finishGesture(clientX(ev) - gesture.startX, clientY(ev) - gesture.startY);
      return;
    }

    if (!pointer.active) return;
    const dx = clientX(ev) - pointer.startX;
    const dy = clientY(ev) - pointer.startY;
    const regionId = pointer.regionId;
    pointer.active = false;
    pointer.regionId = null;
    if (Math.hypot(dx, dy) > CLICK_THRESHOLD_PX) return; // 拖动不是点击
    if (regionId) {
      if (setSelection(regionId)) render();
    } else if (setSelection(null)) {
      render();
    }
  }

  function onPointerCancel() {
    if (state.destroyed) return;
    cancelGesture();
  }

  function onKeyDown(ev) {
    if (state.destroyed) return;
    if (ev && ev.key === 'Escape') cancelGesture();
  }

  grid.addEventListener('pointerdown', onPointerDown);
  grid.addEventListener('pointermove', onPointerMove);
  grid.addEventListener('pointerup', onPointerUp);
  grid.addEventListener('pointercancel', onPointerCancel);
  doc.addEventListener('keydown', onKeyDown);

  refreshGridMetrics();
  const ResizeObserverCtor = doc.defaultView && doc.defaultView.ResizeObserver;
  const observer = ResizeObserverCtor ? new ResizeObserverCtor(() => refreshGridMetrics()) : null;
  if (observer) observer.observe(grid);

  /* ------------------------------------------------------------ 公开接口 */

  function update(view = {}) {
    if (state.destroyed) return;
    cancelGesture(); // 取消进行中的手势：丢弃落位示意、不发回调
    if (view && Array.isArray(view.items)) state.items = view.items;
    if (view && view.readonly !== undefined) state.readonly = view.readonly === false ? false : true;
    if (view && view.config) state.config = { ...DEFAULT_GRID_CONFIG, ...view.config };
    const stillThere =
      state.selectedRegionId === null ||
      state.items.some((item) => item && item.regionId === state.selectedRegionId);
    if (!stillThere) setSelection(null);
    render();
  }

  function destroy() {
    if (state.destroyed) return;
    state.destroyed = true;
    cancelGesture();
    grid.removeEventListener('pointerdown', onPointerDown);
    grid.removeEventListener('pointermove', onPointerMove);
    grid.removeEventListener('pointerup', onPointerUp);
    grid.removeEventListener('pointercancel', onPointerCancel);
    doc.removeEventListener('keydown', onKeyDown);
    if (observer) observer.disconnect();
    for (const entry of regionEntries.values()) entry.el.remove();
    regionEntries.clear();
    grid.remove();
    root.classList.remove('rg-root');
    root.removeAttribute('data-rg-root');
    root.removeAttribute('data-rg-editable');
    root.removeAttribute('data-rg-readonly');
  }

  return { update, destroy };
}
