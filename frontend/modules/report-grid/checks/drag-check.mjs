/**
 * G 网格模块 · 真实鼠标拖拽 / 缩放检查
 *
 * 用**真实鼠标事件**（CDP Input.dispatchMouseEvent）完成拖动与拉宽 / 拉高，
 * 逐步记录：起止坐标、受影响 placement、回调次数、每步 JS 错误、截图。
 *
 * 严禁用「直接调布局方法 / 改 DOM / 注入最终快照」冒充拖拽：
 * 本脚本只允许 (1) 真实鼠标事件驱动页面，(2) 读取页面状态做断言。
 *
 * 浏览器：独立临时 profile + 随机 CDP 端口（不接管 9222 或他人页面）。
 * 静态服务：默认 8930（端口被占用时报错退出，不停止他人进程）。
 *
 * 用法：node frontend/modules/report-grid/checks/drag-check.mjs --out-dir <目录>
 */

import { spawn } from 'node:child_process';
import { mkdir, writeFile, mkdtemp, rm } from 'node:fs/promises';
import { existsSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..', '..', '..');

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
].filter(Boolean);

/* ----------------------------------------------------------------- 参数 */

function parseArgs(argv) {
  const out = { outDir: path.join(repoRoot, '.agent-runtime', 'grid-prep-g', 'page', 'drag'), port: 8930 };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--out-dir') out.outDir = path.resolve(argv[++i]);
    else if (argv[i] === '--port') out.port = Number(argv[++i]);
  }
  return out;
}

const args = parseArgs(process.argv.slice(2));

/* --------------------------------------------------------- 极简 CDP 客户端 */

class MiniCDP {
  constructor() {
    this.ws = null;
    this.seq = 0;
    this.pending = new Map();
    this.handlers = new Map();
  }

  connect(url) {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(url);
      this.ws = ws;
      const timer = setTimeout(() => reject(new Error('CDP 连接超时')), 15000);
      ws.addEventListener('open', () => {
        clearTimeout(timer);
        resolve();
      });
      ws.addEventListener('error', (e) => {
        clearTimeout(timer);
        reject(new Error('CDP 连接失败: ' + (e.message || 'unknown')));
      });
      ws.addEventListener('message', (ev) => {
        let msg;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg.id && this.pending.has(msg.id)) {
          const { resolve: rs, reject: rj } = this.pending.get(msg.id);
          this.pending.delete(msg.id);
          if (msg.error) rj(new Error(msg.error.message));
          else rs(msg.result);
          return;
        }
        if (msg.method) {
          const key = `${msg.sessionId || ''}:${msg.method}`;
          for (const fn of this.handlers.get(key) || []) {
            try {
              fn(msg.params, msg.sessionId);
            } catch (error) {
              console.error('handler error', error);
            }
          }
        }
      });
    });
  }

  send(method, params = {}, sessionId) {
    const id = ++this.seq;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify(payload));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP 超时: ${method}`));
        }
      }, 30000);
    });
  }

  on(method, fn, sessionId = '') {
    const key = `${sessionId}:${method}`;
    const list = this.handlers.get(key) || [];
    list.push(fn);
    this.handlers.set(key, list);
    return () => {
      const cur = this.handlers.get(key) || [];
      const i = cur.indexOf(fn);
      if (i >= 0) cur.splice(i, 1);
    };
  }
}

function findChrome() {
  for (const candidate of CHROME_CANDIDATES) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error('未找到 Chrome，可用 CHROME_PATH 环境变量指定');
}

async function launchChrome() {
  const bin = findChrome();
  const userDataDir = await mkdtemp(path.join(tmpdir(), 'rg-drag-chrome-'));
  const proc = spawn(
    bin,
    [
      '--headless=new',
      '--disable-gpu',
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-background-networking',
      '--disable-sync',
      '--disable-extensions',
      '--window-size=1440,900',
      '--remote-debugging-port=0',
      `--user-data-dir=${userDataDir}`,
      'about:blank',
    ],
    { stdio: ['ignore', 'ignore', 'ignore'] },
  );
  const portFile = path.join(userDataDir, 'DevToolsActivePort');
  let port = null;
  for (let i = 0; i < 120; i += 1) {
    if (existsSync(portFile)) {
      const content = readFileSync(portFile, 'utf8');
      const first = content.split('\n')[0].trim();
      if (first) {
        port = Number(first);
        break;
      }
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  if (!port) {
    proc.kill('SIGKILL');
    throw new Error('Chrome 启动失败，未拿到调试端口');
  }
  const wsPath = readFileSync(portFile, 'utf8').split('\n')[1] || '';
  return {
    proc,
    userDataDir,
    port,
    wsUrl: `ws://127.0.0.1:${port}${wsPath.startsWith('/') ? wsPath : '/' + wsPath}`,
  };
}

/* ------------------------------------------------------------ 静态服务器 */

async function waitForPort(port, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/`);
      if (res.status < 500) return true;
    } catch {
      /* retry */
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  return false;
}

async function ensureServer(port) {
  const script = path.join(repoRoot, 'frontend/modules/report-grid/checks/static-server.mjs');
  const child = spawn(process.execPath, [script, '--port', String(port), '--root', repoRoot], {
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stderr = '';
  child.stderr.on('data', (d) => {
    stderr += String(d);
  });
  const exited = new Promise((resolve) => child.on('exit', (code) => resolve(code)));
  const ok = await waitForPort(port, 8000);
  if (!ok) {
    const code = await Promise.race([exited, Promise.resolve(null)]);
    throw new Error(
      `静态服务未在 ${port} 端口就绪${code !== null ? `（退出码 ${code}）` : ''}。${stderr.trim()}\n` +
        '端口被占用时不要停止他人进程，请换端口重试。',
    );
  }
  return child;
}

/* -------------------------------------------------------------- 页面操作 */

async function evaluate(cdp, sessionId, expression) {
  const res = await cdp.send(
    'Runtime.evaluate',
    { expression, returnByValue: true, awaitPromise: true },
    sessionId,
  );
  if (res.exceptionDetails) {
    const d = res.exceptionDetails;
    throw new Error('页面脚本执行失败: ' + ((d.exception && d.exception.description) || d.text));
  }
  return res.result.value;
}

async function mouseEvent(cdp, sessionId, { type, x, y, button = 'left', buttons = 0, clickCount = 0 }) {
  await cdp.send('Input.dispatchMouseEvent', { type, x, y, button, buttons, clickCount }, sessionId);
}

async function realClick(cdp, sessionId, x, y) {
  await mouseEvent(cdp, sessionId, { type: 'mouseMoved', x, y, buttons: 0 });
  await mouseEvent(cdp, sessionId, { type: 'mousePressed', x, y, buttons: 1, clickCount: 1 });
  await mouseEvent(cdp, sessionId, { type: 'mouseReleased', x, y, buttons: 0, clickCount: 1 });
}

async function realDrag(cdp, sessionId, from, to, { steps = 8, holdMs = 30 } = {}) {
  await mouseEvent(cdp, sessionId, { type: 'mouseMoved', x: from.x, y: from.y, buttons: 0 });
  await mouseEvent(cdp, sessionId, { type: 'mousePressed', x: from.x, y: from.y, buttons: 1, clickCount: 1 });
  for (let i = 1; i <= steps; i += 1) {
    const x = from.x + ((to.x - from.x) * i) / steps;
    const y = from.y + ((to.y - from.y) * i) / steps;
    await mouseEvent(cdp, sessionId, { type: 'mouseMoved', x, y, buttons: 1 });
    await new Promise((r) => setTimeout(r, holdMs));
  }
  await mouseEvent(cdp, sessionId, { type: 'mouseReleased', x: to.x, y: to.y, buttons: 0, clickCount: 1 });
  await new Promise((r) => setTimeout(r, 120));
}

async function screenshot(cdp, sessionId, file) {
  const shot = await cdp.send('Page.captureScreenshot', { format: 'png' }, sessionId);
  await writeFile(file, Buffer.from(shot.data, 'base64'));
  return file;
}

/* -------------------------------------------------------------- 主流程 */

const steps = [];
const failures = [];

function check(name, condition, detail) {
  const ok = Boolean(condition);
  if (!ok) failures.push({ name, detail });
  return ok;
}

async function main() {
  await mkdir(args.outDir, { recursive: true });

  const server = await ensureServer(args.port);
  const chrome = await launchChrome();
  const cdp = new MiniCDP();
  const errors = [];

  try {
    await cdp.connect(chrome.wsUrl);
    const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });

    await cdp.send('Page.enable', {}, sessionId);
    await cdp.send('Runtime.enable', {}, sessionId);
    await cdp.send('Network.enable', {}, sessionId);
    await cdp.send('Log.enable', {}, sessionId);

    cdp.on(
      'Runtime.consoleAPICalled',
      (p) => {
        if (p.type === 'error' || p.type === 'assert') {
          const text = (p.args || [])
            .map((a) => (a.value !== undefined ? String(a.value) : a.description || a.type))
            .join(' ');
          errors.push({ kind: 'console-error', detail: text });
        }
      },
      sessionId,
    );
    cdp.on(
      'Runtime.exceptionThrown',
      (p) => {
        const d = p.exceptionDetails || {};
        errors.push({
          kind: 'js-exception',
          detail: String((d.exception && d.exception.description) || d.text || '未知异常').split('\n')[0],
        });
      },
      sessionId,
    );
    cdp.on(
      'Log.entryAdded',
      (p) => {
        const e = p.entry || {};
        if (e.level === 'error' && !/favicon\.ico/.test(e.url || '')) {
          errors.push({ kind: 'log-error', detail: e.text || '' });
        }
      },
      sessionId,
    );
    cdp.on(
      'Network.responseReceived',
      (p) => {
        const res = p.response || {};
        if (res.status >= 400 && !/favicon\.ico/.test(res.url || '')) {
          errors.push({ kind: 'http-error', detail: `${res.status} ${res.url}` });
        }
      },
      sessionId,
    );

    const url = `http://127.0.0.1:${args.port}/frontend/modules/report-grid/harness/index.html`;
    await cdp.send('Page.navigate', { url }, sessionId);
    await new Promise((r) => setTimeout(r, 1500));

    const ready = await evaluate(cdp, sessionId, 'Boolean(window.__rgHarness && window.__rgHarness.ready)');
    check('页面就绪', ready, 'window.__rgHarness.ready 未就绪');

    const regionIds = await evaluate(cdp, sessionId, 'window.__rgHarness.regionIds');
    check('初始区域数量 >= 3', regionIds.length >= 3, `实际 ${regionIds.length}`);

    /* 读取网格几何（独立按契约算列宽 / 行距） */
    const geom = await evaluate(
      cdp,
      sessionId,
      `(() => {
        const grid = document.querySelector('.rg-grid');
        const r = grid.getBoundingClientRect();
        const cs = getComputedStyle(grid);
        const gap = parseFloat(cs.getPropertyValue('--rg-gap')) || 0;
        const cols = parseInt(cs.getPropertyValue('--rg-columns'), 10) || 12;
        const rowUnit = parseFloat(cs.getPropertyValue('--rg-row-unit')) || 48;
        return {
          gridWidth: r.width,
          gridLeft: r.left,
          gridTop: r.top,
          gap,
          cols,
          colWidth: (r.width - gap * (cols - 1)) / cols,
          colPitch: (r.width - gap * (cols - 1)) / cols + gap,
          rowPitch: rowUnit + gap,
        };
      })()`,
    );

    const rectOf = (selector) =>
      evaluate(
        cdp,
        sessionId,
        `(() => {
          const el = document.querySelector(${JSON.stringify(selector)});
          if (!el) return null;
          const r = el.getBoundingClientRect();
          return { x: r.left, y: r.top, width: r.width, height: r.height, cx: r.left + r.width / 2, cy: r.top + r.height / 2 };
        })()`,
      );

    const intentCount = () => evaluate(cdp, sessionId, 'window.__rgHarness.intentCount');
    const intents = () => evaluate(cdp, sessionId, 'window.__rgHarness.intents');
    const placements = () => evaluate(cdp, sessionId, 'window.__rgHarness.placements');
    const mode = () => evaluate(cdp, sessionId, 'window.__rgHarness.mode');
    const selections = () => evaluate(cdp, sessionId, 'window.__rgHarness.selections');
    const placementOf = (regionId) =>
      evaluate(
        cdp,
        sessionId,
        `(() => { const p = window.__rgHarness.placements.find((x) => x.regionId === ${JSON.stringify(regionId)}); return p || null; })()`,
      );

    async function runStep(name, fn) {
      const before = errors.length;
      const entry = { name, jsErrors: [], screenshot: null, ok: true };
      try {
        await fn(entry);
      } catch (error) {
        entry.ok = false;
        entry.error = String(error && error.message);
        failures.push({ name, detail: entry.error });
      }
      entry.jsErrors = errors.slice(before);
      steps.push(entry);
      const mark = entry.ok && entry.jsErrors.length === 0 ? '✔' : '✖';
      console.log(`${mark} ${name}${entry.jsErrors.length ? ` （JS 错误 ${entry.jsErrors.length}）` : ''}`);
      return entry;
    }

    /* ---------------------------------------------------- 步骤 1：进入编辑态 */
    await runStep('进入编辑态（真实点击按钮）', async (entry) => {
      const box = await rectOf('#rg-toggle-mode');
      await realClick(cdp, sessionId, box.cx, box.cy);
      entry.start = { x: box.cx, y: box.cy };
      entry.end = { x: box.cx, y: box.cy };
      await new Promise((r) => setTimeout(r, 200));
      entry.mode = await mode();
      check('切换后为编辑态', entry.mode === 'editable', `实际 ${entry.mode}`);
      const handleCount = await evaluate(cdp, sessionId, 'document.querySelectorAll(".rg-handle").length');
      check('编辑态出现缩放手柄', handleCount > 0, `手柄数 ${handleCount}`);
      entry.screenshot = await screenshot(cdp, sessionId, path.join(args.outDir, '01-editable-mode.png'));
    });

    /* ---------------------------------------------------- 步骤 2：拖动一列 */
    await runStep('真实拖动区域 A 右移一列', async (entry) => {
      const before = await intentCount();
      const box = await rectOf('.rg-region[data-region-id="region-a"]');
      const from = { x: box.cx, y: box.cy };
      const to = { x: box.cx + geom.colPitch, y: box.cy };
      await realDrag(cdp, sessionId, from, to);
      entry.start = from;
      entry.end = to;
      entry.callbackCount = (await intentCount()) - before;
      entry.newIntents = (await intents()).slice(before);
      entry.placements = await placements();
      check('拖动后回调 1 次', entry.callbackCount === 1, `实际 ${entry.callbackCount}`);
      const intent = entry.newIntents[0];
      check('回调 kind 为 move', intent && intent.kind === 'move', JSON.stringify(intent));
      check('回调目标为 region-a', intent && intent.regionId === 'region-a', JSON.stringify(intent));
      entry.screenshot = await screenshot(cdp, sessionId, path.join(args.outDir, '02-drag-move.png'));
    });

    /* ---------------------------------------------------- 步骤 3：拉宽 */
    await runStep('真实拖拽 e 手柄拉宽', async (entry) => {
      const before = await intentCount();
      const beforePlacement = await placementOf('region-a');
      const box = await rectOf('.rg-region[data-region-id="region-a"] .rg-handle[data-rg-handle="e"]');
      const from = { x: box.cx, y: box.cy };
      const to = { x: box.cx + geom.colPitch, y: box.cy };
      await realDrag(cdp, sessionId, from, to);
      entry.start = from;
      entry.end = to;
      entry.callbackCount = (await intentCount()) - before;
      entry.newIntents = (await intents()).slice(before);
      entry.placements = await placements();
      entry.before = beforePlacement;
      check('缩放回调 1 次', entry.callbackCount === 1, `实际 ${entry.callbackCount}`);
      const intent = entry.newIntents[0];
      check('回调 kind 为 resize', intent && intent.kind === 'resize', JSON.stringify(intent));
      const target = intent && intent.placements.find((p) => p.regionId === 'region-a');
      check('跨列变大', target && target.columnSpan > beforePlacement.columnSpan, JSON.stringify(target));
      check('跨行未变', target && target.rowSpan === beforePlacement.rowSpan, JSON.stringify(target));
      entry.screenshot = await screenshot(cdp, sessionId, path.join(args.outDir, '03-resize-width.png'));
    });

    /* ---------------------------------------------------- 步骤 4：拉高 */
    await runStep('真实拖拽 s 手柄拉高', async (entry) => {
      const before = await intentCount();
      const beforePlacement = await placementOf('region-a');
      const box = await rectOf('.rg-region[data-region-id="region-a"] .rg-handle[data-rg-handle="s"]');
      const from = { x: box.cx, y: box.cy };
      const to = { x: box.cx, y: box.cy + geom.rowPitch };
      await realDrag(cdp, sessionId, from, to);
      entry.start = from;
      entry.end = to;
      entry.callbackCount = (await intentCount()) - before;
      entry.newIntents = (await intents()).slice(before);
      entry.placements = await placements();
      entry.before = beforePlacement;
      check('拉高回调 1 次', entry.callbackCount === 1, `实际 ${entry.callbackCount}`);
      const target = entry.newIntents[0] && entry.newIntents[0].placements.find((p) => p.regionId === 'region-a');
      check('跨行变大', target && target.rowSpan > beforePlacement.rowSpan, JSON.stringify(target));
      check('跨列未变', target && target.columnSpan === beforePlacement.columnSpan, JSON.stringify(target));
      entry.screenshot = await screenshot(cdp, sessionId, path.join(args.outDir, '04-resize-height.png'));
    });

    /* ---------------------------------------------------- 步骤 5：右下角同时改 */
    await runStep('真实拖拽 se 手柄同时改宽高', async (entry) => {
      const before = await intentCount();
      const beforePlacement = await placementOf('region-a');
      const box = await rectOf('.rg-region[data-region-id="region-a"] .rg-handle[data-rg-handle="se"]');
      const from = { x: box.cx, y: box.cy };
      const to = { x: box.cx + geom.colPitch, y: box.cy + geom.rowPitch };
      await realDrag(cdp, sessionId, from, to);
      entry.start = from;
      entry.end = to;
      entry.callbackCount = (await intentCount()) - before;
      entry.newIntents = (await intents()).slice(before);
      entry.placements = await placements();
      entry.before = beforePlacement;
      check('se 手柄回调 1 次', entry.callbackCount === 1, `实际 ${entry.callbackCount}`);
      const target = entry.newIntents[0] && entry.newIntents[0].placements.find((p) => p.regionId === 'region-a');
      check(
        '宽高同时变大',
        target && target.columnSpan > beforePlacement.columnSpan && target.rowSpan > beforePlacement.rowSpan,
        JSON.stringify(target),
      );
      entry.screenshot = await screenshot(cdp, sessionId, path.join(args.outDir, '05-resize-se.png'));
    });

    /* ---------------------------------------------------- 步骤 6：边界夹取 */
    await runStep('真实拖拽越过右边界，列号被夹住', async (entry) => {
      const before = await intentCount();
      const box = await rectOf('.rg-region[data-region-id="region-a"]');
      const from = { x: box.cx, y: box.cy };
      // 终点留在视口内（视口宽 1440），保证 pointerup 被页面收到
      const to = { x: Math.min(1400, box.cx + geom.colPitch * 6), y: box.cy };
      entry.hitTarget = await evaluate(
        cdp,
        sessionId,
        `(() => {
          const el = document.elementFromPoint(${from.x}, ${from.y});
          if (!el) return 'null';
          const region = el.closest ? el.closest('.rg-region') : null;
          return el.className + '|' + (region ? region.getAttribute('data-region-id') : 'no-region');
        })()`,
      );
      await realDrag(cdp, sessionId, from, to);
      entry.previewCountAfter = await evaluate(cdp, sessionId, 'document.querySelectorAll(".rg-preview").length');
      entry.draggingAfter = await evaluate(
        cdp,
        sessionId,
        'document.querySelectorAll(\'[data-rg-dragging="true"]\').length',
      );
      entry.start = from;
      entry.end = to;
      entry.callbackCount = (await intentCount()) - before;
      entry.newIntents = (await intents()).slice(before);
      entry.placements = await placements();
      const target = (entry.newIntents[0] && entry.newIntents[0].placements.find((p) => p.regionId === 'region-a')) ||
        (await placementOf('region-a'));
      check('横向不越界', target && target.column + target.columnSpan - 1 <= geom.cols, JSON.stringify(target));
      const maxColumn = target ? geom.cols - target.columnSpan + 1 : null;
      check('列号夹在上限', target && target.column === maxColumn, `实际列 ${target && target.column}，上限 ${maxColumn}`);
      entry.screenshot = await screenshot(cdp, sessionId, path.join(args.outDir, '06-boundary-clamp.png'));
    });

    /* ---------------------------------------------------- 步骤 7：最小尺寸 */
    await runStep('真实拖拽越过最小尺寸，跨列被夹住', async (entry) => {
      const before = await intentCount();
      const box = await rectOf('.rg-region[data-region-id="region-a"] .rg-handle[data-rg-handle="e"]');
      const from = { x: box.cx, y: box.cy };
      const to = { x: Math.max(40, box.cx - geom.colPitch * 10), y: box.cy };
      await realDrag(cdp, sessionId, from, to);
      entry.start = from;
      entry.end = to;
      entry.callbackCount = (await intentCount()) - before;
      entry.newIntents = (await intents()).slice(before);
      entry.placements = await placements();
      const target = (entry.newIntents[0] && entry.newIntents[0].placements.find((p) => p.regionId === 'region-a')) ||
        (await placementOf('region-a'));
      check('跨列不小于 1', target && target.columnSpan >= 1, JSON.stringify(target));
      check('跨列被夹到最小值 1', target && target.columnSpan === 1, JSON.stringify(target));
      entry.screenshot = await screenshot(cdp, sessionId, path.join(args.outDir, '07-min-size.png'));
    });

    /* ---------------------------------------------------- 步骤 8：稳定 ID */
    await runStep('区域 ID 稳定（改位置不改 ID）', async (entry) => {
      const all = await intents();
      const ids = new Set();
      for (const intent of all) {
        ids.add(intent.regionId);
        for (const p of intent.placements) ids.add(p.regionId);
      }
      entry.regionIds = [...ids];
      entry.placements = await placements();
      const unknown = [...ids].filter((id) => !regionIds.includes(id));
      check('回调里没有陌生区域 ID', unknown.length === 0, `陌生 ID: ${unknown.join(',')}`);
      check('回调目标集合未越界', all.every((i) => regionIds.includes(i.regionId)), '存在未知目标');
      entry.screenshot = await screenshot(cdp, sessionId, path.join(args.outDir, '08-stable-ids.png'));
    });

    /* ---------------------------------------------------- 步骤 9：选择 */
    await runStep('真实点击区域触发选择', async (entry) => {
      const before = (await selections()).length;
      const box = await rectOf('.rg-region[data-region-id="region-c"]');
      // 区域 C 横跨整行，右侧被区域 D 覆盖，点左侧空白处
      const target = { x: box.x + 16, y: box.cy };
      await realClick(cdp, sessionId, target.x, target.y);
      entry.start = target;
      entry.end = target;
      await new Promise((r) => setTimeout(r, 150));
      const after = await selections();
      entry.selections = after;
      check('选择回调新增 1 条', after.length === before + 1, `${before} → ${after.length}`);
      check('选中区域为 region-c', after[after.length - 1] === 'region-c', JSON.stringify(after));
      const selected = await evaluate(
        cdp,
        sessionId,
        'document.querySelectorAll(\'.rg-region[data-rg-selected="true"]\').length',
      );
      check('DOM 有 1 个选中区域', selected === 1, `实际 ${selected}`);
      entry.screenshot = await screenshot(cdp, sessionId, path.join(args.outDir, '09-selection.png'));
    });

    /* ------------------------------------ 步骤 10：手势中途只读加锁 */
    await runStep('手势中途切只读：取消手势且不回调', async (entry) => {
      // 先复位布局，保证几何可预期
      const resetBox = await rectOf('#rg-reset');
      await realClick(cdp, sessionId, resetBox.cx, resetBox.cy);
      await new Promise((r) => setTimeout(r, 250));

      const before = await intentCount();
      const box = await rectOf('.rg-region[data-region-id="region-a"]');
      // 按在区域 A 的左下角：那里没有被其他区域覆盖
      const from = { x: box.x + 16, y: box.y + box.height - 16 };
      const to = { x: from.x + geom.colPitch * 2, y: from.y };
      await mouseEvent(cdp, sessionId, { type: 'mouseMoved', x: from.x, y: from.y, buttons: 0 });
      await mouseEvent(cdp, sessionId, { type: 'mousePressed', x: from.x, y: from.y, buttons: 1, clickCount: 1 });
      for (let i = 1; i <= 4; i += 1) {
        await mouseEvent(cdp, sessionId, {
          type: 'mouseMoved',
          x: from.x + ((to.x - from.x) * i) / 4,
          y: from.y,
          buttons: 1,
        });
        await new Promise((r) => setTimeout(r, 40));
      }
      const previewDuring = await evaluate(cdp, sessionId, 'document.querySelectorAll(".rg-preview").length');
      entry.previewCountDuringGesture = previewDuring;
      check('手势中有落位预览', previewDuring >= 1, `实际 ${previewDuring}`);

      // 鼠标仍按着时，用键盘触发宿主自己的「切换到只读态」按钮
      await evaluate(cdp, sessionId, `document.getElementById('rg-toggle-mode').focus()`);
      await cdp.send(
        'Input.dispatchKeyEvent',
        { type: 'keyDown', windowsVirtualKeyCode: 13, key: 'Enter', code: 'Enter', text: '\r' },
        sessionId,
      );
      await cdp.send(
        'Input.dispatchKeyEvent',
        { type: 'keyUp', windowsVirtualKeyCode: 13, key: 'Enter', code: 'Enter' },
        sessionId,
      );
      await new Promise((r) => setTimeout(r, 250));

      await mouseEvent(cdp, sessionId, { type: 'mouseReleased', x: to.x, y: to.y, buttons: 0, clickCount: 1 });
      await new Promise((r) => setTimeout(r, 250));

      entry.start = from;
      entry.end = to;
      entry.callbackCount = (await intentCount()) - before;
      entry.mode = await mode();
      const previewAfter = await evaluate(cdp, sessionId, 'document.querySelectorAll(".rg-preview").length');
      entry.previewCountAfter = previewAfter;
      check('加锁后无回调', entry.callbackCount === 0, `实际 ${entry.callbackCount}`);
      check('加锁后预览被丢弃', previewAfter === 0, `实际 ${previewAfter}`);
      check('加锁后进入只读态', entry.mode === 'readonly', `实际 ${entry.mode}`);
      entry.screenshot = await screenshot(cdp, sessionId, path.join(args.outDir, '10-lock-mid-gesture.png'));
    });

    /* ------------------------------------ 步骤 11：窄窗口（尺寸变、坐标不变） */
    await runStep('窄窗口：只重算渲染尺寸，坐标不变', async (entry) => {
      const before = await evaluate(
        cdp,
        sessionId,
        `(() => {
          const el = document.querySelector('.rg-region[data-region-id="region-a"]');
          const cs = getComputedStyle(el);
          const r = el.getBoundingClientRect();
          return {
            column: cs.getPropertyValue('--rg-column').trim(),
            row: cs.getPropertyValue('--rg-row').trim(),
            columnSpan: cs.getPropertyValue('--rg-column-span').trim(),
            rowSpan: cs.getPropertyValue('--rg-row-span').trim(),
            width: r.width,
          };
        })()`,
      );
      await cdp.send(
        'Emulation.setDeviceMetricsOverride',
        { width: 760, height: 900, deviceScaleFactor: 1, mobile: false },
        sessionId,
      );
      await new Promise((r) => setTimeout(r, 600));
      const after = await evaluate(
        cdp,
        sessionId,
        `(() => {
          const el = document.querySelector('.rg-region[data-region-id="region-a"]');
          const cs = getComputedStyle(el);
          const r = el.getBoundingClientRect();
          return {
            column: cs.getPropertyValue('--rg-column').trim(),
            row: cs.getPropertyValue('--rg-row').trim(),
            columnSpan: cs.getPropertyValue('--rg-column-span').trim(),
            rowSpan: cs.getPropertyValue('--rg-row-span').trim(),
            width: r.width,
          };
        })()`,
      );
      entry.before = before;
      entry.after = after;
      check('列坐标不变', after.column === before.column, `${before.column} → ${after.column}`);
      check('行坐标不变', after.row === before.row, `${before.row} → ${after.row}`);
      check('跨列不变', after.columnSpan === before.columnSpan, `${before.columnSpan} → ${after.columnSpan}`);
      check('渲染宽度变窄', after.width < before.width, `${before.width} → ${after.width}`);
      entry.screenshot = await screenshot(cdp, sessionId, path.join(args.outDir, '11-narrow-window.png'));
      await cdp.send(
        'Emulation.setDeviceMetricsOverride',
        { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false },
        sessionId,
      );
      await new Promise((r) => setTimeout(r, 400));
    });

    /* ------------------------------------ 步骤 12：销毁与重建 */
    await runStep('销毁并重建模块', async (entry) => {
      const destroyBox = await rectOf('#rg-destroy');
      await realClick(cdp, sessionId, destroyBox.cx, destroyBox.cy);
      await new Promise((r) => setTimeout(r, 250));
      const aliveAfterDestroy = await evaluate(cdp, sessionId, 'window.__rgHarness.alive');
      const regionCountAfterDestroy = await evaluate(
        cdp,
        sessionId,
        'document.querySelectorAll(".rg-region").length',
      );
      check('销毁后模块不再存活', aliveAfterDestroy === false, `实际 ${aliveAfterDestroy}`);
      check('销毁后区域被清理', regionCountAfterDestroy === 0, `实际 ${regionCountAfterDestroy}`);
      entry.screenshot = await screenshot(cdp, sessionId, path.join(args.outDir, '12-destroyed.png'));

      const rebuildBox = await rectOf('#rg-rebuild');
      await realClick(cdp, sessionId, rebuildBox.cx, rebuildBox.cy);
      await new Promise((r) => setTimeout(r, 300));
      const aliveAfterRebuild = await evaluate(cdp, sessionId, 'window.__rgHarness.alive');
      const regionCountAfterRebuild = await evaluate(
        cdp,
        sessionId,
        'document.querySelectorAll(".rg-region").length',
      );
      check('重建后模块存活', aliveAfterRebuild === true, `实际 ${aliveAfterRebuild}`);
      check('重建后区域恢复', regionCountAfterRebuild === 4, `实际 ${regionCountAfterRebuild}`);
      entry.screenshot = await screenshot(cdp, sessionId, path.join(args.outDir, '13-rebuilt.png'));
    });

    /* ------------------------------------ 步骤 13：碰撞布局（留给碰撞验证） */
    await runStep('载入碰撞布局：两个相邻区域重叠', async (entry) => {
      const box = await rectOf('#rg-collision');
      await realClick(cdp, sessionId, box.cx, box.cy);
      await new Promise((r) => setTimeout(r, 300));
      entry.placements = await placements();
      const a = entry.placements.find((p) => p.regionId === 'region-a');
      const b = entry.placements.find((p) => p.regionId === 'region-b');
      const overlap = a && b && a.row <= b.row + b.rowSpan - 1 && b.row <= a.row + a.rowSpan - 1 &&
        a.column <= b.column + b.columnSpan - 1 && b.column <= a.column + a.columnSpan - 1;
      check('存在重叠区域对', overlap, JSON.stringify({ a, b }));
      entry.screenshot = await screenshot(cdp, sessionId, path.join(args.outDir, '14-collision-layout.png'));
    });

    const report = {
      generatedAt: new Date().toISOString(),
      url,
      chromePort: chrome.port,
      staticPort: args.port,
      geometry: geom,
      totalCallbacks: await intentCount(),
      jsErrorCount: errors.length,
      jsErrors: errors,
      failures,
      steps,
    };
    await writeFile(path.join(args.outDir, 'report.json'), JSON.stringify(report, null, 2), 'utf8');

    console.log('');
    console.log(`回调总数: ${report.totalCallbacks}`);
    console.log(`JS 错误数: ${report.jsErrorCount}`);
    console.log(`失败断言: ${failures.length}`);
    if (failures.length) {
      for (const f of failures) console.log(`  ✗ ${f.name}: ${f.detail}`);
    }
    console.log(`报告: ${path.join(args.outDir, 'report.json')}`);
    console.log(`截图目录: ${args.outDir}`);

    process.exitCode = failures.length || errors.length ? 1 : 0;
  } finally {
    try {
      cdp.ws && cdp.ws.close();
    } catch {
      /* ignore */
    }
    chrome.proc.kill('SIGKILL');
    await rm(chrome.userDataDir, { recursive: true, force: true }).catch(() => {});
    server.kill('SIGKILL');
  }
}

main().catch((error) => {
  console.error('拖拽检查脚本异常:', error && error.message ? error.message : error);
  process.exit(2);
});
