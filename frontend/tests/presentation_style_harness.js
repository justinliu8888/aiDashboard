/* This harness executes the current production card renderer without starting the workbench. */
async function runPresentationStyleCheck() {
  const requestedWidth = Number(new URLSearchParams(location.search).get('width'));
  const cardWidth = [160, 210].includes(requestedWidth) ? requestedWidth : 160;
  const source = await (await fetch('../pages/workbench.js')).text();
  function extract(name) {
    const start = source.indexOf('function ' + name + '(');
    if (start < 0) throw new Error('Missing production function: ' + name);
    let depth = 0;
    let opened = false;
    for (let i = source.indexOf(') {', start) + 2; i < source.length; i++) {
      if (source[i] === '{') { depth++; opened = true; }
      if (source[i] === '}' && --depth === 0 && opened) return source.slice(start, i + 1);
    }
    throw new Error('Unbalanced production function: ' + name);
  }
  const definition = {
    metadata: { revision: 7, savedRevision: 5 },
    regions: [{
      id: 'group', component: { type: 'CONTAINER', arrangement: { mode: 'FLOW', direction: 'VERTICAL' } },
      layout: { placement: { type: 'GRID', column: 1, columnSpan: 12 } },
      style: { fontSize: { value: 24, unit: 'PX' }, textAlign: 'RIGHT', borderRadius: 18 },
      children: [
        { id: 'text', name: '文字卡片', component: { type: 'TEXT' }, style: { fontSize: { value: 28, unit: 'PX' }, textAlign: 'CENTER', borderRadius: 0 } },
        { id: 'metric', name: '窄卡指标', component: { type: 'METRIC_CARD' }, layout: { placement: { type: 'FLOW', width: { value: cardWidth, unit: 'PX' } } } },
        { id: 'table', name: '实际表头对齐', component: { type: 'TABLE' } }
      ]
    }, { id: 'legacy', name: '默认文字', component: { type: 'TEXT' }, layout: { placement: { type: 'GRID', column: 1, columnSpan: 12 } } }]
  };
  const previewPayload = { regions: [
    { regionId: 'metric', name: '窄卡指标', componentType: 'METRIC_CARD', payload: { value: 1234.5, displayValue: '1,234.50 kWh', unit: 'kWh' } },
    { regionId: 'text', name: '文字卡片', componentType: 'TEXT', payload: { rendered: '实际文字应为 28px 居中，直角卡片' } },
    { regionId: 'legacy', name: '默认文字', componentType: 'TEXT', payload: { rendered: '未设置样式，保留默认字号' } },
    { regionId: 'table', name: '实际表头对齐', componentType: 'TABLE', payload: { columns: [{ fieldRef: 'label', label: '文字列' }], rows: [{ label: '应右对齐' }] } }
  ] };
  const history = { cursor: 2, undo: ['v1', 'v2'], redo: ['v3'], lock: { owner: 'fixture', revision: 7 } };
  const before = JSON.stringify({ definition, previewPayload, history });
  const find = (id) => definition.regions.flatMap(r => r.children || [r]).find(r => r.id === id);
  const renderer = new Function('document', 'findDefinitionRegion', 'readPayloadUnit', 'readCapabilityNotes', 'esc', 'formatMetricValue', 'isEditingEnabled', 'sendTemplateEditAction', 'readTableTruncation',
    extract('renderRegionPayloadBody') + '\n' + extract('renderRegionCard') + '\nreturn renderRegionCard;'
  )(document, (_def, id) => find(id), () => 'kWh', () => [],
    value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    String, () => false, () => { window.editCalls = (window.editCalls || 0) + 1; }, () => null);
  const root = document.querySelector('#preview-root');
  WorkbenchPresentation.render(root, { definition, previewPayload }, {
    renderLeaf: (_leaf, payload, ctx) => renderer(payload, ctx.definition, ctx.payload, ctx.totalsByUnit)
  });
  window.editCalls = 0;
  document.querySelector('#mode-toggle').onclick = () => root.classList.toggle('preview-editable');
  await new Promise(resolve => setTimeout(resolve, 200));
  const text = root.querySelector('.wp-leaf[data-region-id="text"] .region-body > div');
  const card = root.querySelector('.wp-leaf[data-region-id="text"] > div');
  const metric = root.querySelector('[data-region-metric] > div:last-child');
  const narrow = root.querySelector('.wp-leaf[data-region-id="metric"] > div');
  const rect = narrow.getBoundingClientRect();
  const checks = {
    fontSize: getComputedStyle(text).fontSize === '28px',
    textAlign: getComputedStyle(text).textAlign === 'center',
    tableTextAlign: getComputedStyle(root.querySelector('th')).textAlign === 'right',
    borderRadius: getComputedStyle(card).borderRadius === '0px',
    inheritedFont: getComputedStyle(metric).fontSize === '24px',
    inheritedAlign: getComputedStyle(metric).textAlign === 'right',
    narrowButtons: [...narrow.querySelectorAll('button')].every(button => {
      const b = button.getBoundingClientRect();
      return b.width > 0 && b.left >= rect.left && b.right <= rect.right + 1 && b.bottom <= rect.bottom + 1;
    }),
    noHorizontalOverflow: narrow.scrollWidth <= narrow.clientWidth + 1,
    noBlankSpace: [...root.querySelectorAll('.wp-leaf')].every(leaf => leaf.clientHeight <= leaf.firstElementChild.offsetHeight + 1),
    defaultFont: getComputedStyle(root.querySelector('.wp-leaf[data-region-id="legacy"] .region-body > div')).fontSize === '20px',
    dataHistoryLockUnchanged: JSON.stringify({ definition, previewPayload, history }) === before,
    formattedValue: metric.textContent === '1,234.50 kWh'
  };
  const result = document.querySelector('#result');
  result.textContent = JSON.stringify(checks, null, 2);
  result.dataset.pass = String(Object.values(checks).every(Boolean));
  window.presentationChecks = checks;
}
runPresentationStyleCheck().catch(error => { console.error(error); document.querySelector('#result').textContent = String(error); });
