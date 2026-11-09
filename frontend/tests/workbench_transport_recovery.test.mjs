import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../pages/workbench.js', import.meta.url), 'utf8');
function extract(name) {
  const start = source.indexOf(`async function ${name}(`);
  assert.ok(start >= 0);
  return source.slice(start, source.indexOf('\n}', start) + 2);
}
function recovery(detail) {
  const observations = { messages: [], cleared: false, menu: null };
  let fireTimeout;
  const context = vm.createContext({
    AbortController, session: { sessionId: 's' }, requestEpoch: 1,
    sessionApi: { detail },
    setTimeout(callback, ms) { assert.equal(ms, 4000); fireTimeout = callback; return 1; },
    clearTimeout() { observations.timerCleared = true; },
    appendAiText(text) { observations.messages.push(text); },
    setState(state) { observations.state = state; },
    clearChoiceLock() { observations.cleared = true; },
    appendAiQuestions(questions) { observations.menu = questions[0]; },
  });
  vm.runInContext(extract('syncChoiceLockFromServer'), context);
  return { context, observations, timeout: () => fireTimeout() };
}

test('状态核对挂起时会中止请求，保留原菜单并完成收尾', async () => {
  const r = recovery((_id, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new Error('timeout')));
  }));
  const running = r.context.syncChoiceLockFromServer();
  r.timeout();
  await running;
  assert.equal(r.observations.cleared, false);
  assert.equal(r.observations.menu, null);
  assert.equal(r.observations.timerCleared, true);
  assert.equal(r.observations.messages.length, 1);
});

test('核对成功后按服务端菜单恢复', async () => {
  const r = recovery(async () => ({ state: 'USER_EDITING_TEMPLATE', pendingChoice: { question: { id: 'pending' } } }));
  await r.context.syncChoiceLockFromServer();
  assert.equal(r.observations.menu.id, 'pending');
  assert.equal(r.observations.timerCleared, true);
});

test('切换会话后的迟到核对不覆盖当前会话', async () => {
  let resolve;
  const r = recovery(() => new Promise(done => { resolve = done; }));
  const running = r.context.syncChoiceLockFromServer();
  r.context.requestEpoch = 2;
  resolve({ pendingChoice: null });
  await running;
  assert.equal(r.observations.cleared, false);
  assert.equal(r.observations.timerCleared, true);
});

test('502 保留 HTTP 状态，供错误 Trace 与提示区分', async () => {
  const context = vm.createContext({ API_BASE: '', fetch: async () => ({ ok: false, status: 502 }) });
  vm.runInContext(extract('postAgentStream'), context);
  await assert.rejects(context.postAgentStream({}, null, undefined), error => error.status === 502);
});
