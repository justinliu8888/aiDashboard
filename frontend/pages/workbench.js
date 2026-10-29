// 智能报表工作台 - AgentScope + Mock 数据链路实现
// 核心能力：
// 1. 发请求 → 根据 state 分发到不同的 UI 渲染
// 2. 根据 reportDefinitionDraft.regions[] 动态渲染预览
// 3. 根据 changeSet 应用变更并高亮

const API_BASE = window.AI_REPORT_API_BASE || window.location.origin;

// 只读问答的前端判断规则（纯函数模块，无 DOM 依赖）。
// 本文件和 workbench_query.js 必须成对加载：缺了它就没法判断「本轮是不是只读问答」，
// 而这条判断决定要不要推进业务——宁可明确报错，也不要在缺规则的情况下照常跑。
const WorkbenchQuery = window.WorkbenchQuery;
if (!WorkbenchQuery) {
  throw new Error("workbench_query.js 未加载：无法判断只读问答，工作台拒绝以不确定的规则运行。");
}

// -------- 会话状态 --------
// sessionId  由后端生成，一条对话从建到删永不改变（数据库主键）
// conversationId 前端生成，一次请求线程；停止生成时会换新
// messageId  本条用户消息的幂等键，同一轮重发时复用同一个
const session = {
  sessionId: null,
  conversationId: "conv_" + Date.now(),
  state: "ANALYZING",
  designSessionId: null,
  reportDefinitionId: null,
  baseDefinitionRevision: null,
  definition: null, // 当前草稿（用于应用 changeSet 和用户编辑）
  policy: null,     // 当前报表策略草稿
  editingTargetRegionId: null, // 5.5 用户编辑模版中当前指向的 section
  lastReportRun: null, // 预览运行记录（ReportRunPreview）
  previewPayload: null, // 从 /api/report/preview/{runId} 拉下来的真实数据
  pendingResume: null, // 停止后等待重新执行的原请求
  workspaceId: null, // 归属项目
};

// -------- 会话接口客户端 --------
const sessionApi = {
  async request(path, options = {}) {
    const res = await fetch(API_BASE + path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (res.status === 204) return null;
    return res.json();
  },
  create() {
    return this.request("/api/sessions", { method: "POST", body: JSON.stringify({}) });
  },
  list(params = {}) {
    const query = new URLSearchParams();
    if (params.q) query.set("q", params.q);
    if (params.workspaceId) query.set("workspaceId", params.workspaceId);
    const suffix = query.toString() ? "?" + query.toString() : "";
    return this.request("/api/sessions" + suffix);
  },
  detail(sessionId) {
    return this.request("/api/sessions/" + sessionId);
  },
  remove(sessionId) {
    return this.request("/api/sessions/" + sessionId, { method: "DELETE" });
  },
  rename(sessionId, title) {
    return this.request("/api/sessions/" + sessionId, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    });
  },
  workspaces() {
    return this.request("/api/workspaces");
  },
};

function newMessageId() {
  return "msg_" + Date.now() + "_" + Math.random().toString(36).slice(2, 8);
}

// -------- 上次会话的落点 --------
// 刷新后默认回到空态、要人工从历史列表里翻，是这个工作台最容易被投诉的一点。
// 把当前 sessionId 记在 localStorage，页面启动时自动接回来；恢复不了也给明确提示，
// 历史入口始终保留。
const LAST_SESSION_KEY = "ai-report:last-session-id";

function readLastSessionId() {
  try {
    return window.localStorage.getItem(LAST_SESSION_KEY) || null;
  } catch (e) {
    return null;
  }
}

function writeLastSessionId(sessionId) {
  try {
    if (sessionId) window.localStorage.setItem(LAST_SESSION_KEY, sessionId);
    else window.localStorage.removeItem(LAST_SESSION_KEY);
  } catch (e) {
    // 隐私模式 / 存储被禁用：不影响主流程，只是刷新后不能自动恢复。
  }
}

const DEFAULT_CONTEXT = {
  projectId: "project_001",
  subjectRef: "machine_room",
  subjectId: "room_a",
};

// -------- DOM 工具 --------
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const chatScroll = $("chat-scroll");
const previewRoot = $("preview-root");
const previewEmpty = $("preview-empty");
const planRoot = $("plan-root");
let activeRequest = null;
let requestEpoch = 0;
let lockedChoice = null;
const advanceTimers = new Set();

// 会话恢复的竞态令牌：恢复途中用户点了「新建对话」就让这次恢复作废。
let sessionRestoreToken = 0;
// 历史回放期间是否已经画过业务菜单。
// 查询轮也会把原待确认菜单带回来，已经画过就别再画一张重复的。
let businessMenuReplayed = false;

// --------------------------------------------------------------------------
// 文字方案（#19 先确认，再生成）
//
// 方案模块（workbench_plan.js）只负责「把方案显示出来、把确认意图交回来」：
//   - 它不发 HTTP；
//   - 它不发 __advance__；
//   - 它不改旧菜单。
//
// 所以这里的职责是：
//   1. 把服务端的 designPhase / reportPlan 交给它渲染；
//   2. busy / error / canConfirm 由**这一处**统一决策，
//      不从历史文案里猜「用户到底确认了没有」；
//   3. 收到确认回调后，由上层发请求（请求里带动作与所依据的版本）。
// --------------------------------------------------------------------------

const planState = {
  designPhase: null,
  reportPlan: null,
  busy: false,
  error: null,
  // 已确认方案的折叠视图是否展开。默认收起（需求稿 §2.8）。
  planExpanded: false,
};

/** 能不能确认：只由明确的几个条件决定，不靠文案猜。 */
function planCanConfirm(state = planState) {
  if (!state) return false;
  if (state.busy) return false;
  if (state.error) return false;
  if (state.designPhase !== "PLAN_WAITING_CONFIRM") return false;
  const plan = state.reportPlan;
  if (!plan || !plan.id || !plan.revision) return false;
  return true;
}

function renderPlanModule() {
  if (!planRoot) return;
  const api = window.WorkbenchPlan;
  if (!api) return;
  // 草稿阶段也要保留方案面板（折叠的已确认方案）—— 不再排除 DRAFT_READY。
  const hasPlan = !!planState.reportPlan || !!planState.designPhase;
  // 草稿空态只在「有方案、但还没出草稿」时让位。
  // DRAFT_READY 时若草稿还没到，宁可显示空态，也不要留一块空白。
  const hasDraft = !!(session.definition && Array.isArray(session.definition.regions) && session.definition.regions.length);
  const hideEmpty = hasPlan && !hasDraft && planState.designPhase !== "DRAFT_READY";
  previewRoot.classList.toggle("plan-active", hideEmpty);
  if (!hasPlan) {
    api.clear(planRoot);
    return;
  }
  // 记住用户有没有展开折叠视图：重渲染（busy 抖动 / 新响应）时不把它弹回去。
  const existing = planRoot.querySelector("details.confirmed-plan-details");
  if (existing) planState.planExpanded = existing.open === true;
  api.render(
    planRoot,
    {
      designPhase: planState.designPhase,
      reportPlan: planState.reportPlan,
      busy: planState.busy,
      error: planState.error,
      canConfirm: planCanConfirm(),
      planExpanded: planState.planExpanded,
    },
    { onAction: handlePlanAction }
  );
}

/** 从一次响应里同步方案状态。 */
function syncPlanFromResponse(data) {
  if (!data) return;
  const phase = data.designPhase || null;
  const plan = data.domainObjects?.reportPlan || null;
  if (!phase && !plan) return;
  planState.designPhase = phase || planState.designPhase;
  if (plan) planState.reportPlan = plan;
  // 出错时展示错误条；成功时清掉上一次的错误。
  planState.error = data.error ? data.error.message || data.error.code || null : null;
  renderPlanModule();
  // 草稿已出：把右侧滚回顶部，让用户第一眼看到草稿而不是聊天记录。
  if (phase === "DRAFT_READY") planRoot.parentElement.scrollTop = 0;
}

/** 从会话详情里恢复方案（刷新 / 重建服务后）。 */
function restorePlanFromDetail(detail) {
  const snapshot = detail?.snapshot || {};
  planState.designPhase = snapshot.designPhase || null;
  planState.reportPlan = snapshot.reportPlan || null;
  planState.busy = false;
  planState.error = null;
  // 刷新后默认收起（需求稿 §2.8）。
  planState.planExpanded = false;
  renderPlanModule();
}

/** 确认回调：模块只交回意图，真正的请求由上层发。 */
function handlePlanAction(action) {
  if (isBusy() || !action) return;
  if (!action || action.designAction !== "CONFIRM_PLAN") return;
  if (!planCanConfirm()) return;
  sendMessage("确认", {
    designAction: {
      designAction: "CONFIRM_PLAN",
      planId: action.planId,
      basePlanRevision: action.basePlanRevision,
    },
  });
}

// 对话区贴底跟随：用户上滑后暂停自动滚动，回到底部后恢复
const CHAT_BOTTOM_THRESHOLD = 48;
// Debug Trace 卡片内层滚动容器的贴底判定阈值
const ROWS_BOTTOM_THRESHOLD = 16;
let chatStickToBottom = true;

// 「忙」= 有请求在跑，或还有自动推进的定时器没跑完。
// 生成期间不允许切换会话界面：点历史会话已经有这个限制，
// 「新建对话」「清空」也走同一套，避免生成到一半把界面切走。
function isBusy() {
  return !!activeRequest || advanceTimers.size > 0;
}

function syncInputControls() {
  const busy = isBusy();
  const input = $("chat-input");
  // 待确认菜单存在时**不锁输入**：只读提问要能发出去（工单 02 / 规格 #12）。
  // 旧版本在这里把输入整个锁死，用户在待确认期间问一句都问不了。
  // 执行侧的约束由后端负责——自由输入不能绕过接受修改 / 确认报表。
  input.disabled = busy;
  input.placeholder = lockedChoice
    ? "可以先问只读问题，或从上方下拉框选择"
    : "描述报表需求，或输入修改要求…";
  $("btn-send").disabled = busy;
  $("btn-send").classList.toggle("opacity-50", busy);
  $("btn-stop").classList.toggle("hidden", !busy);
  document.querySelectorAll("[data-quick], #btn-regenerate").forEach((button) => {
    // 快捷生成 / 重新生成是**操作**，不是提问：待确认菜单没选完之前仍然不给点。
    button.disabled = busy || !!lockedChoice;
  });
  const selects = document.querySelectorAll(".question-select");
  // 优先启用与当前锁对应的那一个（恢复历史会话时页面上可能有多个下拉框）。
  // 同 id 会出现多次（每一轮编辑都画一个），要取**最后一个**——那才是当前轮刚画的；
  // 取第一个会点到历史轮的下拉框，当前轮的反而是灰的、点不开。
  const activeId = lockedChoice?.id;
  let activeIndex = -1;
  if (activeId) {
    for (let i = selects.length - 1; i >= 0; i--) {
      if (selects[i].dataset.questionId === activeId) {
        activeIndex = i;
        break;
      }
    }
  }
  selects.forEach((select, index) => {
    const isActive = activeIndex >= 0 ? index === activeIndex : index === selects.length - 1;
    select.disabled = busy || !lockedChoice?.options?.length || !isActive;
  });
  const restarts = document.querySelectorAll(".question-restart");
  restarts.forEach((button, index) => {
    button.disabled = busy || !lockedChoice?.options?.length || index !== restarts.length - 1;
  });
}

function clearChoiceLock() {
  lockedChoice = null;
  document.querySelectorAll(".question-select").forEach((select) => {
    select.disabled = true;
  });
  syncPreviewEditability();
  syncInputControls();
}

function setChoiceLock(question) {
  lockedChoice = question;
  $("chat-input").value = "";
  syncPreviewEditability();
  syncInputControls();
}

function queueAdvance(delay, epoch) {
  const timer = setTimeout(() => {
    advanceTimers.delete(timer);
    if (epoch === requestEpoch) sendMessage("__advance__");
    syncInputControls();
  }, delay);
  advanceTimers.add(timer);
  syncInputControls();
}

// 切换会话界面时，把在途请求「摘」下来但**不掐断**它。
// 掐断（abort）会让后端认为请求被取消而不落库，用户已经说出口的话就丢了；
// 摘下来让它跑完落库，历史里才留得住，同时用 requestEpoch 让它的响应
// 不再渲染到新会话的界面上。
function detachActiveRequest() {
  requestEpoch++;
  for (const timer of advanceTimers) clearTimeout(timer);
  advanceTimers.clear();
  if (activeRequest) {
    const { thinking, debugCard } = activeRequest;
    activeRequest = null;
    // 界面节点已被新会话清空，这里直接移除，不再往已脱离文档的节点上写。
    thinking?.remove();
    debugCard?.remove();
  }
  session.pendingResume = null;
  session.conversationId = "conv_" + Date.now() + "_" + requestEpoch;
  clearChoiceLock();
  syncInputControls();
}

function cancelActiveRequest() {
  requestEpoch++;
  for (const timer of advanceTimers) clearTimeout(timer);
  advanceTimers.clear();
  let resumable = false;
  if (activeRequest) {
    const { controller, thinking, debugCard, resumeRequest } = activeRequest;
    // 确认/自动推进可能已有保存等副作用，不自动重放这些操作。
    resumable = !!resumeRequest && !resumeRequest.fromChoice && !resumeRequest.isAdvance;
    session.pendingResume = resumable
      ? { body: resumeRequest.body, conversationId: resumeRequest.body.conversationId }
      : null;
    activeRequest = null;
    controller.abort();
    if (thinking) removeBubble(thinking);
    finishDebugTrace(debugCard, "cancelled");
  } else session.pendingResume = null;
  // 后续消息改用新的请求会话，隔离已经发往后端但来不及终止的模型响应。
  session.conversationId = "conv_" + Date.now() + "_" + requestEpoch;
  clearChoiceLock();
  syncInputControls();
  appendAiText(resumable
    ? "已停止。输入「继续工作」会重新执行上一条消息。"
    : "已停止。此步骤可能已经在后端执行，请检查结果；不能安全地自动重试。");
}

const DEBUG_STAGE_LABELS = {
  router_received: "Router 收到请求",
  service_start: "AgentService 开始",
  context_route_start: "ContextEngine 开始路由",
  concierge_start: "Concierge 开始",
  concierge_chunk: "Concierge 模型输出",
  concierge_done: "Concierge 完成",
  concierge_decision: "Concierge 决策",
  concierge_direct_reply: "Concierge 直接回复",
  deep_route_start: "Deep Router 开始",
  deep_route_chunk: "Deep Router 模型输出",
  deep_route_done: "Deep Router 完成",
  context_route_done: "ContextEngine 决策完成",
  apply_context_route: "应用路由决策",
  context_route_intercepted: "ContextEngine 已拦截",
  business_agent_start: "业务 Agent 开始",
  business_agent_done: "业务 Agent 完成",
  template_edit_done: "用户编辑模版处理完成",
  auto_advance: "自动推进",
  auto_advance_start: "自动推进开始",
  auto_advance_done: "自动推进完成",
  validation_start: "定义校验开始",
  validation_done: "定义校验完成",
  preview_start: "预览运行开始",
  preview_done: "预览运行完成",
  render_prepare_start: "渲染准备开始",
  render_prepare_done: "渲染准备完成",
  render_done: "渲染完成",
  service_return: "AgentService 返回",
  reply_stream_start: "回复流开始",
  reply_stream_done: "回复流完成",
  router_return: "Router 返回",
  router_failed: "Router 失败",
  fallback_no_client: "ContextEngine 兜底：无模型客户端",
  deep_route_failed: "Deep Router 失败",
  concierge_failed: "Concierge 失败",
};

// -------- 滚动跟随 --------
function isChatAtBottom() {
  return chatScroll.scrollHeight - chatScroll.scrollTop - chatScroll.clientHeight <= CHAT_BOTTOM_THRESHOLD;
}

function updateJumpButton() {
  const btn = $("btn-jump-bottom");
  if (btn) btn.classList.toggle("hidden", chatStickToBottom);
}

// 仅在贴底状态下滚动；用户上滑后停止跟随，回到底部后恢复
function scrollChatBottom(force = false) {
  if (force) chatStickToBottom = true;
  if (chatStickToBottom) chatScroll.scrollTop = chatScroll.scrollHeight;
  updateJumpButton();
}

chatScroll.addEventListener("scroll", () => {
  const atBottom = isChatAtBottom();
  if (atBottom !== chatStickToBottom) {
    chatStickToBottom = atBottom;
    updateJumpButton();
  }
}, { passive: true });

// 气泡高度变化（内容追加、图表渲染）后重新贴底，避免末尾内容露不全
const chatBubbleObserver = new ResizeObserver(() => {
  if (chatStickToBottom) chatScroll.scrollTop = chatScroll.scrollHeight;
});

$("btn-jump-bottom").addEventListener("click", () => {
  chatStickToBottom = true;
  chatScroll.scrollTop = chatScroll.scrollHeight;
  updateJumpButton();
});

let toastTimer = null;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 2600);
}

// -------- 状态更新 --------
function setState(state) {
  session.state = state;
  const badge = $("state-badge");
  const footer = $("footer-state");
  badge.textContent = state;
  footer.textContent = state;

  // 编辑入口的显隐跟着状态走（选了「修改」才出现）。
  syncPreviewEditability();

  const palette = {
    ANALYZING: "bg-slate-100 text-slate-600 border-slate-200",
    NEEDS_CLARIFICATION: "bg-amber-50 text-amber-700 border-amber-200",
    BLOCKED_OR_UNSUPPORTED: "bg-rose-50 text-rose-700 border-rose-200",
    WAITING_USER_CONFIRM: "bg-blue-50 text-blue-700 border-blue-200",
    USER_EDITING_TEMPLATE: "bg-orange-50 text-orange-700 border-orange-200",
    DEFINITION_DRAFT_READY: "bg-fuchsia-50 text-fuchsia-700 border-fuchsia-200",
    SAVE_DEFINITION_DRAFT: "bg-emerald-50 text-emerald-700 border-emerald-200",
    VALIDATING: "bg-indigo-50 text-indigo-700 border-indigo-200",
    VALIDATION_FAILED: "bg-rose-50 text-rose-700 border-rose-200",
    PREVIEWING: "bg-indigo-50 text-indigo-700 border-indigo-200",
    PREVIEW_READY: "bg-emerald-50 text-emerald-700 border-emerald-200",
    RENDERING: "bg-indigo-50 text-indigo-700 border-indigo-200",
    READY: "bg-emerald-50 text-emerald-700 border-emerald-200",
  };
  badge.className = "inline-flex items-center gap-1 rounded-md text-xs font-medium px-2 py-1 border mono " + (palette[state] || palette.ANALYZING);
}

function updateMeta() {
  $("meta-rev").textContent = session.baseDefinitionRevision ?? "—";
  $("meta-session").textContent = session.designSessionId ?? "—";

  const draftBadge = $("draft-badge");
  if (session.definition) {
    draftBadge.className = "inline-flex items-center gap-1 rounded-md text-xs font-medium px-2 py-1 border bg-amber-50 text-amber-700 border-amber-200";
    draftBadge.innerHTML = '<iconify-icon class="text-sm" icon="mdi:robot-outline"></iconify-icon>AI 生成草稿 v' + (session.baseDefinitionRevision ?? "?");
  } else {
    draftBadge.className = "inline-flex items-center gap-1 rounded-md text-xs font-medium px-2 py-1 border bg-slate-100 text-slate-500 border-slate-200";
    draftBadge.innerHTML = '<iconify-icon class="text-sm" icon="mdi:robot-outline"></iconify-icon>尚无草稿';
  }
}

// -------- 对话气泡 --------
function appendUser(text) {
  const wrap = document.createElement("div");
  wrap.className = "flex justify-end";
  wrap.innerHTML = `<div class="max-w-[86%] rounded-2xl rounded-tr-sm bg-blue-600 text-white text-sm px-3.5 py-2.5 leading-relaxed">${esc(text)}</div>`;
  chatScroll.appendChild(wrap);
  chatBubbleObserver.observe(wrap);
  scrollChatBottom(true);
}

function aiBubble(innerHTML) {
  const wrap = document.createElement("div");
  wrap.className = "flex gap-2.5";
  wrap.innerHTML = `
    <div class="w-8 h-8 shrink-0 rounded-full bg-gradient-to-br from-blue-500 to-blue-700 flex items-center justify-center">
      <iconify-icon class="text-white" icon="mdi:robot-outline"></iconify-icon>
    </div>
    <div class="flex-1 min-w-0 space-y-2.5">${innerHTML}</div>`;
  chatScroll.appendChild(wrap);
  chatBubbleObserver.observe(wrap);
  scrollChatBottom();
  return wrap;
}

function appendAiText(text) {
  aiBubble(`<div class="rounded-xl rounded-tl-sm border border-slate-200 bg-white px-3.5 py-3 text-sm text-slate-700 leading-relaxed">${esc(text)}</div>`);
}

function appendDebugTrace({ text, state, isAdvance }) {
  const title = isAdvance ? "自动推进 Debug Trace" : "请求 Debug Trace";
  const wrap = aiBubble(`
    <div class="debug-card rounded-xl rounded-tl-sm border border-slate-200 bg-white overflow-hidden">
      <button type="button" data-debug-toggle class="debug-header flex w-full items-center justify-between gap-2 px-3.5 py-2.5 bg-slate-50 border-b border-slate-200 text-left cursor-pointer select-none hover:bg-slate-100 transition-colors" aria-expanded="true">
        <div class="flex items-center gap-2 min-w-0">
          <iconify-icon class="text-slate-500 shrink-0" icon="mdi:timeline-clock-outline"></iconify-icon>
          <span class="text-xs font-semibold text-slate-700 truncate">${esc(title)}</span>
          <span class="debug-status shrink-0 rounded-full bg-blue-50 text-blue-700 border border-blue-100 text-[10px] px-2 py-0.5">running</span>
          <span class="debug-unread hidden shrink-0 rounded-full bg-blue-100 text-blue-700 border border-blue-100 text-[10px] px-1.5 py-0.5"></span>
        </div>
        <div class="flex items-center gap-2 shrink-0">
          <span class="debug-rowcount mono text-[10px] text-slate-400">0 行</span>
          <span class="debug-elapsed mono text-[10px] text-slate-500">0ms</span>
          <iconify-icon class="debug-chevron text-slate-400" icon="mdi:chevron-down"></iconify-icon>
        </div>
      </button>
      <div class="debug-body">
        <div class="debug-body-inner">
          <div class="px-3.5 py-2 border-b border-slate-100 text-[11px] text-slate-500 leading-relaxed">
            <span class="font-medium text-slate-600">state</span>: <span class="debug-state mono">${esc(state)}</span>
            <span class="mx-1.5 text-slate-300">|</span>
            <span class="font-medium text-slate-600">message</span>: <span class="mono">${esc(text)}</span>
          </div>
          <div class="debug-rows max-h-56 overflow-y-auto no-bar divide-y divide-slate-100"></div>
        </div>
      </div>
    </div>
  `);
  const card = wrap.querySelector(".debug-card");
  card._chunkStats = {};
  card._chunkPreview = {};
  card._readRows = 0;
  card._rowsStick = true;

  const rows = card.querySelector(".debug-rows");
  rows.addEventListener("scroll", () => {
    card._rowsStick = rows.scrollHeight - rows.scrollTop - rows.clientHeight <= ROWS_BOTTOM_THRESHOLD;
  }, { passive: true });
  // 展开/折叠行内 details 后高度变化，保持贴底
  rows.addEventListener("toggle", (e) => {
    if (e.target.tagName !== "DETAILS") return;
    requestAnimationFrame(() => scrollDebugRows(card));
  }, true);
  // 行内容重建（如流式输出刷新）会改变行高，监听后重新贴底
  card._rowObserver = new ResizeObserver(() => scrollDebugRows(card));

  card.querySelector("[data-debug-toggle]").addEventListener("click", () => toggleDebugCard(card));
  return card;
}

// -------- Debug Trace 卡片折叠 --------
function setDebugCollapsed(card, collapsed) {
  card.classList.toggle("is-collapsed", collapsed);
  const header = card.querySelector("[data-debug-toggle]");
  header.setAttribute("aria-expanded", String(!collapsed));
  if (!collapsed) {
    card._readRows = card.querySelector(".debug-rows").children.length;
    refreshDebugCard(card);
    requestAnimationFrame(() => scrollDebugRows(card));
  }
}

function toggleDebugCard(card) {
  setDebugCollapsed(card, !card.classList.contains("is-collapsed"));
}

// 刷新卡片标题栏：行数 + 折叠期间的未读行数
function refreshDebugCard(card) {
  const rows = card.querySelector(".debug-rows");
  const total = rows.children.length;
  card.querySelector(".debug-rowcount").textContent = `${total} 行`;
  const badge = card.querySelector(".debug-unread");
  if (card.classList.contains("is-collapsed")) {
    const unread = total - (card._readRows || 0);
    if (unread > 0) {
      badge.textContent = `+${unread}`;
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
  } else {
    card._readRows = total;
    badge.classList.add("hidden");
  }
}

// 内层滚动容器跟随最新一行
function scrollDebugRows(card) {
  const rows = card.querySelector(".debug-rows");
  if (!rows || !card._rowsStick) return;
  rows.scrollTop = rows.scrollHeight;
}
function formatDebugDetail(data) {
  const parts = [];
  const pairs = [
    ["state", data.state],
    ["response", data.responseState],
    ["from", data.fromState],
    ["to", data.toState],
    ["intent", data.intent],
    ["action", data.action],
    ["target", data.targetState],
    ["engine", data.engineStage],
    ["impl", data.impl],
    ["model", data.model],
    ["confidence", data.confidence],
    ["ready", data.isReadyForNextStep],
    ["missing", Array.isArray(data.missingInfo) ? data.missingInfo.join(",") : data.missingInfo],
    ["tools", Array.isArray(data.toolRequests) ? data.toolRequests.join(",") : data.toolRequests],
    ["ui", Array.isArray(data.uiActions) ? data.uiActions.join(",") : data.uiActions],
    ["step", data.stepElapsedMs != null ? `${data.stepElapsedMs}ms` : null],
    ["reason", data.reason],
    ["message", data.message],
    ["error", data.error],
  ];
  pairs.forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") parts.push(`${key}=${value}`);
  });
  return parts.join(" · ");
}

function renderDebugParams(params) {
  if (!params || typeof params !== "object") return "";
  const groups = Object.entries(params).map(([group, values]) => {
    const fields = Object.entries(values || {}).map(([key, value]) => {
      const display = value === null || value === undefined
        ? "—"
        : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
      return `
        <div class="grid grid-cols-[minmax(85px,auto)_1fr] gap-2 border-t border-slate-100 py-1.5">
          <span class="mono text-slate-500">${esc(key)}</span>
          <span class="mono text-slate-700 whitespace-pre-wrap break-all">${esc(display)}</span>
        </div>`;
    }).join("");
    return `<div class="mt-2"><div class="font-semibold text-slate-600">${esc(group)}</div>${fields}</div>`;
  }).join("");
  return `<details open class="mt-1.5 text-slate-500">
    <summary class="cursor-pointer select-none text-blue-600">关键参数</summary>
    <div class="pl-2">${groups}</div>
  </details>`;
}

function addDebugEvent(card, event, data = {}) {
  if (!card) return;
  const stage = data.stage || event;
  const elapsed = data.elapsedMs ?? 0;
  card.querySelector(".debug-elapsed").textContent = `${elapsed}ms`;
  if (data.state) card.querySelector(".debug-state").textContent = data.state;

  if (event === "model_chunk") {
    const key = stage;
    card._chunkStats[key] = (card._chunkStats[key] || 0) + String(data.delta || "").length;
    card._chunkPreview[key] = (card._chunkPreview[key] || "").slice(0, 4000);
    if (card._chunkPreview[key].length < 4000) {
      card._chunkPreview[key] += String(data.delta || "").slice(0, 4000 - card._chunkPreview[key].length);
    }
    let row = card.querySelector(`[data-debug-key="${key}"]`);
    if (!row) {
      row = document.createElement("div");
      row.dataset.debugKey = key;
      row.className = "px-3.5 py-2 text-[11px] leading-relaxed";
      card.querySelector(".debug-rows").appendChild(row);
      card._rowObserver.observe(row);
    }
    const wasOpen = row.querySelector("details")?.open ?? false;
    row.innerHTML = `
      <div class="flex items-center justify-between gap-2">
        <span class="font-medium text-slate-700">${esc(DEBUG_STAGE_LABELS[key] || key)}</span>
        <span class="mono text-slate-400">${elapsed}ms</span>
      </div>
      <div class="mt-0.5 text-slate-500">stream chars=${card._chunkStats[key]}</div>
      <details ${wasOpen ? "open" : ""} class="mt-1 text-slate-500">
        <summary class="cursor-pointer select-none text-blue-600">查看流式输出</summary>
        <pre class="mt-1 whitespace-pre-wrap break-all text-slate-700">${esc(card._chunkPreview[key])}${card._chunkStats[key] > 4000 ? "\n...<truncated>" : ""}</pre>
      </details>
    `;
    refreshDebugCard(card);
    scrollDebugRows(card);
    scrollChatBottom();
    return;
  }
  const row = document.createElement("div");
  row.className = "px-3.5 py-2 text-[11px] leading-relaxed";
  const detail = formatDebugDetail(data);
  row.innerHTML = `
    <div class="flex items-center justify-between gap-2">
      <span class="font-medium text-slate-700">${esc(DEBUG_STAGE_LABELS[stage] || DEBUG_STAGE_LABELS[event] || stage)}</span>
      <span class="mono text-slate-400">${elapsed}ms</span>
    </div>
    ${detail ? `<div class="mt-0.5 text-slate-500 break-words">${esc(detail)}</div>` : ""}
    ${renderDebugParams(data.debugParams)}
  `;
  card.querySelector(".debug-rows").appendChild(row);
  card._rowObserver.observe(row);
  console.debug("[AI Report Debug]", event, data);
  refreshDebugCard(card);
  scrollDebugRows(card);
  scrollChatBottom();
}

function finishDebugTrace(card, status) {
  if (!card) return;
  const badge = card.querySelector(".debug-status");
  badge.textContent = status;
  badge.className = status === "done"
    ? "debug-status rounded-full bg-emerald-50 text-emerald-700 border border-emerald-100 text-[10px] px-2 py-0.5"
    : status === "cancelled"
      ? "debug-status rounded-full bg-amber-50 text-amber-700 border border-amber-100 text-[10px] px-2 py-0.5"
      : "debug-status rounded-full bg-rose-50 text-rose-700 border border-rose-100 text-[10px] px-2 py-0.5";
}

// 草稿确认阶段只接受后端给出的单选值；选“修改”后才开放编辑输入。
// lock=false 用于回放历史消息：渲染出选项卡，但不抢占当前输入锁
// 返回值：实际画出来的业务菜单（没有菜单时返回 null），供历史回放判断菜单是否仍然有效。
function appendConfirmActions(question, { lock = true } = {}) {
  if (!question?.selectionOnly || !question.options?.length) {
    // 确认项缺失：只提示，不去抢输入锁。
    // 抢锁会写一个空 options 的锁，把页面上已有的下拉框全部 disable 掉，
    // 结果就是「输入框锁死了，但一个能点的下拉框都没有」。
    if (lock) {
      appendAiText("确认选项未就绪，请新建对话后重新生成草稿。");
    }
    return null;
  }
  const wrap = aiBubble(`
    <div class="rounded-xl rounded-tl-sm border border-slate-200 bg-white px-3.5 py-3">
      <label class="block text-sm text-slate-700 leading-relaxed mb-2.5">${esc(question.prompt || "请先选择如何处理当前草稿：")}</label>
      <select class="question-select w-full rounded-md border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700" data-question-id="${esc(question.id || "")}">
        <option value="">请选择</option>
        ${question.options.map((option) => `<option value="${esc(option.value ?? option.label)}">${esc(option.label ?? option.value)}</option>`).join("")}
      </select>
    </div>
  `);
  if (lock) setChoiceLock(question);
  else businessMenuReplayed = true;
  wrap.querySelector(".question-select").addEventListener("change", (event) => {
    if (lockedChoice?.id === question?.id && event.target.value) {
      sendMessage(event.target.value, { fromChoice: true });
    }
  });
  return question;
}

// -------- 确认问题归一化 --------
// 后端给的问题对象不一定带 selectionOnly（例如业务 Agent 的 confirm_change）。
// 前端要的是一个**能点的下拉框**：把选项补齐、强制走单选，再交给 appendConfirmActions。
function normalizeConfirmQuestion(question) {
  if (!question || typeof question !== "object") return null;
  const options = (question.options || [])
    .filter((option) => option && (option.value || option.label))
    .map((option) => ({ value: option.value ?? option.label, label: option.label ?? option.value }));
  if (!options.length) return null;
  return {
    id: question.id || "definition_draft_confirm",
    prompt: question.prompt || "请选择如何处理这份草稿：",
    expects: question.expects || "SINGLE_CHOICE",
    selectionOnly: true,
    allowRestart: !!question.allowRestart,
    options,
  };
}

// DEFINITION_DRAFT_READY 优先认这些问题 id；认不到再退回「第一个带选项的问题」。
const DEFINITION_DRAFT_CONFIRM_IDS = [
  "confirm_change",
  "confirm_draft",
  "definition_draft_confirm",
  "template_edit_confirm",
  "confirm",
];

function resolveDefinitionDraftQuestion(data) {
  const questions = (data?.questions || []).filter(Boolean);
  if (!questions.length) return null;
  const picked =
    questions.find((question) => DEFINITION_DRAFT_CONFIRM_IDS.includes(question.id)) ||
    questions.find((question) => question.selectionOnly && question.options?.length) ||
    questions.find((question) => question.options?.length);
  return normalizeConfirmQuestion(picked);
}

// 后端只给了 changeSet、没给问题时的兜底确认项。
// 没有它，用户在 DEFINITION_DRAFT_READY 就只剩一张变更卡，没有任何「继续」的办法。
const FALLBACK_DRAFT_CONFIRM_QUESTION = {
  id: "definition_draft_confirm",
  prompt: "请选择如何处理这份草稿：",
  expects: "SINGLE_CHOICE",
  selectionOnly: true,
  options: [
    { value: "同意", label: "同意" },
    { value: "修改", label: "修改" },
    { value: "重新开始", label: "重新开始" },
  ],
};

function appendAiThinking() {
  const bubble = aiBubble(`
    <div class="rounded-xl rounded-tl-sm border border-slate-200 bg-white px-3.5 py-3 text-sm text-slate-700 leading-relaxed inline-flex items-center gap-2">
      <iconify-icon class="text-base animate-spin text-blue-600" icon="mdi:loading"></iconify-icon>
      <span>AI 思考中...</span>
      <button class="btn-cancel-thinking ml-2 text-xs text-rose-600 hover:text-rose-800" type="button">停止</button>
    </div>`);
  bubble.querySelector(".btn-cancel-thinking").addEventListener("click", cancelActiveRequest);
  return bubble;
}

function removeBubble(bubble) {
  bubble?.remove();
  scrollChatBottom();
}

function appendAiQuestions(questions, { lock = true } = {}) {
  const list = (questions || []).filter(Boolean);
  // 没有问题就别画一张空卡：空卡只有边框没有内容，用户看起来像卡住了。
  if (!list.length) {
    if (lock) appendAiText("暂时没有需要补充的信息，请直接描述你的报表需求。");
    return;
  }
  const html = list
    .map((q) => {
      const useSelect = q.selectionOnly;
      let controls = "";
      if (q.options?.length && useSelect) {
        controls = `
          <div class="question-select-wrap flex items-center gap-2 mt-2">
            <select class="question-select flex-1 min-w-0 rounded-md border border-amber-200 bg-white px-2 py-1.5 text-xs text-slate-700" data-question-id="${esc(q.id || "")}">
              <option value="">请选择</option>
              ${q.options.map((o) => `<option value="${esc(o.value || o.label)}">${esc(o.label)}</option>`).join("")}
            </select>
            ${q.allowRestart ? '<button class="question-restart shrink-0 text-xs text-amber-800 underline" type="button">重新开始</button>' : ""}
          </div>`;
      } else if (q.options?.length) {
        controls = `<div class="flex flex-wrap gap-1 mt-2">${q.options
          .map(
            (o) =>
              `<button class="opt-btn text-[11px] bg-amber-50 hover:bg-amber-100 text-amber-800 border border-amber-200 rounded px-1.5 py-0.5" data-answer="${esc(o.value || o.label)}">${esc(o.label)}</button>`
          )
          .join("")}</div>`;
      }
      return `
        <div class="px-3.5 py-2.5 border border-amber-200 bg-white rounded-lg">
          <div class="font-medium text-amber-800 text-sm">${esc(q.prompt || q.text || "请选择：")}</div>
          <div class="text-[11px] text-slate-500 mt-1">${esc(q.expects)}${q.required ? " · 必填" : ""}</div>
          ${controls}
        </div>`;
    })
    .join("");

  const wrap = aiBubble(`
    <div class="rounded-xl border border-amber-200 bg-amber-50/40 overflow-hidden">
      <div class="px-3.5 py-2.5 bg-amber-50 border-b border-amber-100 flex items-center gap-2">
        <iconify-icon class="text-amber-600" icon="mdi:help-circle-outline"></iconify-icon>
        <span class="text-sm font-semibold text-amber-900">需要你补充信息</span>
      </div>
      <div class="p-3 space-y-2">${html}</div>
    </div>
  `);

  wrap.querySelectorAll(".opt-btn").forEach((btn) => {
    btn.addEventListener("click", () => sendMessage(btn.dataset.answer));
  });
  const required = list.find((q) => q.selectionOnly && q.options?.length);
  if (required && lock) setChoiceLock(required);
  wrap.querySelectorAll(".question-select").forEach((select) => {
    select.addEventListener("change", () => {
      // 用 id 判定而不是对象引用：恢复历史会话时回放出来的是新对象
      if (select.value && lockedChoice?.id === select.dataset.questionId) {
        sendMessage(select.value, { fromChoice: true });
      }
    });
  });
  wrap.querySelectorAll(".question-restart").forEach((button) => {
    button.addEventListener("click", () => {
      if (lockedChoice?.id === required?.id) sendMessage("重新开始", { fromChoice: true });
    });
  });
}

// -------- 只读问答的展示 --------
// 只读回答与必须选择的业务菜单分开展示：
//   - 回答不套「需要你补充信息」的壳，也不带无关的生成选项；
//   - 只读澄清单独成卡，不占用业务菜单的选择锁，原待确认操作保持可用；
//   - 两张卡都不改草稿、不改 revision、不动右侧预览。
function appendAiReadOnlyAnswer(text) {
  aiBubble(
    // whitespace-pre-wrap：答案里的换行要保留。
    // 只读回答可能带多段（模型的分段、以及服务端补的「已记录 / 非实时」边界句），
    // 不保留换行就会被压成一长段。
    `<div data-readonly-answer="1" class="rounded-xl rounded-tl-sm border border-slate-200 bg-white px-3.5 py-3 text-sm text-slate-700 leading-relaxed whitespace-pre-wrap">${esc(text)}</div>`
  );
}

function appendAiReadOnlyClarification(text) {
  const wrap = aiBubble(`
    <div data-readonly-clarification="1" class="rounded-xl border border-slate-200 bg-slate-50/70 overflow-hidden">
      <div class="px-3.5 py-2.5 bg-slate-100/70 border-b border-slate-200 flex items-center gap-2">
        <iconify-icon class="text-slate-500" icon="mdi:help-circle-outline"></iconify-icon>
        <span class="text-sm font-semibold text-slate-700">先确认一下你要问什么</span>
      </div>
      <div class="p-3 space-y-2">
        <div class="text-sm text-slate-700 leading-relaxed whitespace-pre-wrap">${esc(text)}</div>
      </div>
    </div>
  `);
  // 只读澄清不带选项：后端契约里澄清问题是一句自由文本（拼在 reply 里），
  // 而 questions 装的是**原业务菜单**——绝不能把它当成澄清选项再画一遍。
  return wrap;
}

/**
 * 渲染一轮只读问答。
 * @param kind "ANSWER" | "CLARIFICATION"
 */
function renderReadOnlyAnswer(data, kind) {
  const reply = data.reply || "";
  if (kind === WorkbenchQuery.READ_ONLY_CLARIFICATION) {
    appendAiReadOnlyClarification(reply || "我想先确认一下你要问什么。");
    return;
  }
  appendAiReadOnlyAnswer(reply || "这次没有查到可以确认的内容。");
}

// 查询轮会把原待确认菜单原样带回（后端共用契约 ADR 0002）。
// 页面上已经有菜单时不重复画一张；确实没有菜单时才补画，
// 保证「答案与菜单分开展示、且原操作始终可用」。
function resolveQueryPreservedMenu(data) {
  const questions = (data?.questions || []).filter(Boolean);
  if (!questions.length) return null;
  const picked =
    questions.find((question) => DEFINITION_DRAFT_CONFIRM_IDS.includes(question.id)) ||
    questions.find((question) => question.selectionOnly && question.options?.length) ||
    questions.find((question) => question.options?.length);
  return picked ? normalizeConfirmQuestion(picked) : null;
}

function appendAiBlocks(blocks) {
  const html = blocks
    .map(
      (b) => `
    <div class="rounded-xl border border-rose-200 bg-rose-50 px-3.5 py-3">
      <div class="flex items-center gap-2 mb-1.5">
        <iconify-icon class="text-rose-600" icon="mdi:alert-octagon-outline"></iconify-icon>
        <span class="text-sm font-semibold text-rose-900">${b.code === "PERMISSION_DENIED" ? "超出数据权限范围" : b.code === "CAPABILITY_NOT_SUPPORTED" ? "数据能力不支持" : "被拦截"}</span>
        <span class="mono text-[10px] text-rose-700 bg-white border border-rose-200 rounded px-1.5 py-0.5 ml-auto">${esc(b.code)}</span>
      </div>
      <p class="text-xs text-rose-900/90 leading-relaxed">${esc(b.message)}</p>
      ${
        b.alternatives?.length
          ? `<div class="mt-2 pt-2 border-t border-rose-200">
              <div class="text-[11px] text-rose-800 font-medium mb-1">替代方案：</div>
              <ul class="list-disc ml-4 text-[11px] text-rose-900/80 space-y-0.5">${b.alternatives.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>
            </div>`
          : ""
      }
    </div>`
    )
    .join("");
  aiBubble(html);
}

function appendAiChangeSet(changeSet, { interactive = true } = {}) {
  const changed = (changeSet.changed || [])
    .map((e) => {
      let detail = "";
      if (e.after === null) {
        detail = `<div class="text-xs text-rose-700 mt-1 font-medium">→ 删除整个区域</div>`;
      } else if (e.after?.mode === "REPLACE" && e.after.value) {
        const v = e.after.value;
        detail = `<div class="text-xs text-slate-700 mt-1">新窗口: <span class="mono">${esc(v.unit)} · offset=${v.offset} · count=${v.count}</span></div>`;
      } else if (e.after) {
        detail = `<div class="text-xs text-slate-500 mt-1 mono">${esc(JSON.stringify(e.after).slice(0, 120))}</div>`;
      }
      return `
      <div class="rounded-lg bg-white border border-fuchsia-200 px-3 py-2">
        <div class="text-xs"><span class="text-fuchsia-700 font-medium">变更</span> <span class="mono text-[10px] text-slate-600 bg-slate-100 border border-slate-200 rounded px-1">${esc(e.regionId)}</span></div>
        <div class="mono text-[10px] text-slate-500 mt-1">${esc(e.path)}</div>
        ${detail}
      </div>`;
    })
    .join("");

  const unchangedTags = (changeSet.unchanged || [])
    .map((u) => `<span class="mono text-[10px] text-slate-600 bg-slate-100 border border-slate-200 rounded px-1.5 py-0.5">${esc(u.regionId)}</span>`)
    .join(" ");

  const impactList = (changeSet.impact || [])
    .map((i) => `<li class="flex gap-1.5"><span class="mono text-[11px] text-slate-700">${esc(i.regionId)}</span><span class="text-slate-600">${esc(i.note)}</span></li>`)
    .join("");

  // 卡片正文单独抽出来：可交互版和历史归档版共用同一份内容。
  // （以前归档版引用了一个作用域外的 `html`，回放历史会话时直接抛 ReferenceError，
  //   整个 restoreSession 崩掉，右侧草稿、选择锁、确认下拉全部恢复不了。）
  const bodyHtml = `
    <div class="space-y-1.5">${changed || '<div class="text-xs text-slate-400">无变更</div>'}</div>
    ${unchangedTags ? `<div><div class="text-[11px] text-slate-500 mb-1">未变化</div><div class="flex flex-wrap gap-1">${unchangedTags}</div></div>` : ""}
    ${impactList ? `<div><div class="text-[11px] text-slate-500 mb-1">影响说明</div><ul class="space-y-0.5 text-xs">${impactList}</ul></div>` : ""}`;

  const headerHtml = `
    <div class="px-3.5 py-2.5 bg-fuchsia-50 border-b border-fuchsia-100 flex items-center gap-2">
      <iconify-icon class="text-fuchsia-700" icon="mdi:file-swap-outline"></iconify-icon>
      <span class="text-sm font-semibold text-fuchsia-900">AI 变更确认</span>
      <span class="text-[10px] text-fuchsia-700 bg-white border border-fuchsia-200 rounded px-1.5 py-px ml-auto">baseRev=${changeSet.baseDefinitionRevision}</span>
    </div>`;

  if (!interactive) {
    // 历史回放：只展示内容，不给「接受/拒绝」按钮。
    aiBubble(`
    <div class="rounded-xl border border-fuchsia-200 bg-white overflow-hidden">
      ${headerHtml}
      <div class="p-3 space-y-3">
        ${bodyHtml}
        <div class="pt-1 text-[11px] text-slate-400">历史变更（已归档，不可再次操作）</div>
      </div>
    </div>
    `);
    return;
  }

  const wrap = aiBubble(`
    <div class="rounded-xl border border-fuchsia-200 bg-white overflow-hidden">
      ${headerHtml}
      <div class="p-3 space-y-3">
        ${bodyHtml}
        <div class="flex gap-1.5 pt-1">
          <button class="btn-accept inline-flex items-center gap-1 rounded-lg bg-fuchsia-700 hover:bg-fuchsia-800 text-white text-xs font-medium px-2.5 py-1.5 transition">
            <iconify-icon class="text-sm" icon="mdi:check"></iconify-icon>接受变更
          </button>
          <button class="btn-reject inline-flex items-center gap-1 rounded-lg bg-white hover:bg-slate-50 text-slate-700 text-xs font-medium px-2.5 py-1.5 border border-slate-200 transition">
            <iconify-icon class="text-sm text-slate-500" icon="mdi:close"></iconify-icon>拒绝
          </button>
        </div>
      </div>
    </div>
  `);

  wrap.querySelector(".btn-accept").addEventListener("click", () => applyChangeSet(changeSet, wrap));
  wrap.querySelector(".btn-reject").addEventListener("click", () => {
    wrap.querySelector(".btn-accept").parentElement.innerHTML =
      '<span class="inline-flex items-center gap-1 rounded-lg bg-slate-100 text-slate-600 border border-slate-200 text-xs font-medium px-2.5 py-1.5"><iconify-icon icon="mdi:close" class="text-sm"></iconify-icon>已拒绝，草稿保持不变</span>';
    toast("已拒绝该变更");
  });
}

function appendTemplateEditSummary(changeSet, markdown) {
  const changed = (changeSet?.changed || [])
    .map((entry) => `<li>${esc(entry.summary || entry.path)}</li>`)
    .join("");
  const impact = (changeSet?.impact || [])
    .map((item) => `<li>${esc(typeof item === "string" ? item : item.note || "")}</li>`)
    .join("");
  if (!changed && !impact && !markdown) return;
  aiBubble(`
    <div class="rounded-lg border border-blue-200 bg-blue-50 p-3 text-xs text-slate-700">
      <div class="font-semibold text-blue-900 mb-1">本次草稿编辑</div>
      ${changed ? `<ul class="list-disc pl-4 space-y-1">${changed}</ul>` : ""}
      ${impact ? `<ul class="list-disc pl-4 mt-2 space-y-1 text-slate-500">${impact}</ul>` : ""}
      ${markdown ? `<div class="mt-2 whitespace-pre-wrap">${esc(markdown)}</div>` : ""}
    </div>
  `);
}

// -------- 待确认编辑计划：前后端契约（方案 8.1 冻结） --------
// 请求：context.visibleRegionIds = 客户可见正文卡片 ID 的有序列表（隐藏标题区不算）
// 响应：domainObjects.pendingEditPlan = { planId, draftRevision, targetRegionId, operations, summary }
// 选择：下拉框选中后，消息正文直接是「接受修改 / 继续调整 / 取消」三个中文词；
//       后端从自己的会话存储取待确认计划并校验 revision，前端不再另带 planId。
// 读侧对字段名做了别名兼容，后端字段微调不需要前端整片重写；
// 写侧只有 visibleRegionIds 一个新字段，后端加同名同类型字段即可接通。
const EDIT_PLAN_CONTRACT = {
  requestVisibleRegionIdsField: "visibleRegionIds",
  choice: {
    accept: "接受修改",
    adjust: "继续调整",
    cancel: "取消",
  },
  planIdKeys: ["planId", "plan_id", "id"],
  draftRevisionKeys: ["draftRevision", "draft_revision", "baseDefinitionRevision", "baseRevision", "revision"],
  targetRegionKeys: ["targetRegionId", "target_region_id", "regionId", "region_id"],
  operationsKeys: ["operations", "ops", "operationList"],
  summaryKeys: ["summary", "userSummary", "displaySummary", "summaryText"],
  unverifiedKeys: ["unverified", "unverifiedCapabilities", "limitations", "warnings", "notes", "caveat"],
};

// 同一份待确认计划只渲染一次：迟到或重复的响应不再重复画卡、也不再重新抢输入锁
let lastPendingPlan = null;

function pickValue(source, keys) {
  if (!source || typeof source !== "object") return undefined;
  for (const key of keys) {
    const value = source[key];
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return undefined;
}

// 把各种形态的值（字符串 / 对象 / 数组）压成一句人能读的话
function readText(value) {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value.trim();
  if (typeof value === "number") return String(value);
  if (Array.isArray(value)) {
    return value.map(readText).filter(Boolean).join("；");
  }
  if (typeof value === "object") {
    const text = pickValue(value, ["text", "label", "title", "description", "summary", "detail", "name"]);
    if (text !== undefined) return readText(text);
    return "";
  }
  return String(value);
}

function readLabel(value) {
  if (value === undefined || value === null || value === "") return "";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (typeof value === "object") {
    const label = pickValue(value, ["name", "label", "title", "displayName", "id", "code"]);
    return label === undefined ? "" : String(label);
  }
  return "";
}

// 时间窗：可能是文案，也可能是 { mode, value: { unit, offset, count } } 这种结构
function formatTimeWindow(value) {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value.trim();
  if (typeof value === "number") return String(value);
  if (typeof value === "object") {
    const label = pickValue(value, ["label", "text", "display", "description"]);
    if (label !== undefined) return readText(label);
    const inner = pickValue(value, ["value", "window", "range"]);
    if (inner !== undefined && inner && typeof inner === "object") {
      const parts = [];
      const unit = pickValue(inner, ["unit", "grain", "granularity"]);
      const offset = pickValue(inner, ["offset"]);
      const count = pickValue(inner, ["count"]);
      if (unit !== undefined) parts.push(String(unit));
      if (offset !== undefined) parts.push("offset=" + String(offset));
      if (count !== undefined) parts.push("count=" + String(count));
      if (parts.length) return parts.join(" · ");
    }
    if (inner !== undefined) return readText(inner);
  }
  return "";
}

// 把一条操作变成「改什么 + 数据源 / 粒度 / 时间窗 / 未验证能力」
function describeEditOperation(op) {
  if (op === undefined || op === null) return null;
  if (typeof op !== "object") return { title: String(op), details: [] };

  const title =
    readText(pickValue(op, ["summary", "description", "title", "label", "intent", "action", "type"])) || "修改";
  const details = [];

  const source = readLabel(
    pickValue(op, ["source", "dataSource", "dataset", "datasetId", "sourceId", "sourceName", "dataSourceId"])
  );
  if (source) details.push("数据源：" + source);

  const grain = readLabel(
    pickValue(op, ["grain", "granularity", "grainLevel", "interval", "timeGrain", "aggregation"])
  );
  if (grain) details.push("粒度：" + grain);

  const timeWindow = formatTimeWindow(
    pickValue(op, ["timeWindow", "timeRange", "window", "period", "timeWindowValue"])
  );
  if (timeWindow) details.push("时间窗：" + timeWindow);

  const unverified = readText(pickValue(op, EDIT_PLAN_CONTRACT.unverifiedKeys));
  if (unverified) details.push("未验证能力：" + unverified);

  return { title, details };
}

// 读出后端下发的待确认计划，字段名做了别名兼容
function readPendingEditPlan(data) {
  const raw = data?.domainObjects?.pendingEditPlan;
  if (!raw || typeof raw !== "object") return null;

  let target = pickValue(raw, EDIT_PLAN_CONTRACT.targetRegionKeys);
  if (target && typeof target === "object") {
    target = pickValue(target, ["regionId", "region_id", "id"]);
  }
  const operations = pickValue(raw, EDIT_PLAN_CONTRACT.operationsKeys);
  const planId = pickValue(raw, EDIT_PLAN_CONTRACT.planIdKeys);
  const draftRevision = pickValue(raw, EDIT_PLAN_CONTRACT.draftRevisionKeys);

  return {
    raw,
    planId: planId === undefined ? null : String(planId),
    draftRevision:
      draftRevision === undefined || draftRevision === null ? null : Number(draftRevision),
    targetRegionId: target === undefined || target === null ? null : String(target),
    operations: Array.isArray(operations) ? operations : operations ? [operations] : [],
    summary: readText(pickValue(raw, EDIT_PLAN_CONTRACT.summaryKeys)),
    unverified: readText(pickValue(raw, EDIT_PLAN_CONTRACT.unverifiedKeys)),
  };
}

// 问题对象里的选项是不是契约冻结的三个（接受修改 / 继续调整 / 取消）
// value 和 label 任一命中就算：后端可能用英文 value + 中文 label。
function hasContractChoices(question) {
  if (!question?.options?.length) return false;
  const values = question.options.map((option) => String(option.value || option.label));
  const labels = question.options.map((option) => String(option.label || option.value));
  return Object.values(EDIT_PLAN_CONTRACT.choice).every(
    (choice) => values.includes(choice) || labels.includes(choice)
  );
}

// 后端如果同时给了确认问题，就用后端那份（id 对得上它的 pending_choice 存储）；
// 没有就用契约里冻结的三个选项。
function resolvePlanQuestion(data) {
  return (data?.questions || []).find(hasContractChoices) || null;
}

// 客户可见正文卡片的顺序：以右侧实际渲染出来的正文卡片 DOM 顺序为准。
// REPORT_TITLE 隐藏标题区根本不渲染，天然不在顺序里，
// 「第三个」按这个顺序数，才是客户眼睛看到的第三个。
function visibleBodyRegionIds() {
  const ids = [];
  previewRoot.querySelectorAll("[data-region-id]").forEach((el) => {
    const id = el.getAttribute("data-region-id");
    if (id && !ids.includes(id)) ids.push(id);
  });
  if (ids.length) return ids;
  // 预览还没画出来时退回草稿里的正文 region 顺序（同样不含隐藏标题区）
  return (session.definition?.regions || [])
    .filter((region) => !isTitleRegion(region))
    .map((region) => region.id);
}

function visibleRegionLabel(regionId) {
  const region = definitionRegionById(regionId);
  if (!region) return regionId || "";
  return region.name || region.component?.title || region.role || region.id;
}

function definitionRegionById(regionId) {
  return (session.definition?.regions || []).find((item) => item.id === regionId);
}

function regionDatasetSourceRef(region) {
  const input = Object.values(region?.data?.inputs || {}).find((item) => item?.datasetRef);
  const datasetRef = input?.datasetRef;
  return datasetRef ? session.definition?.datasets?.[datasetRef]?.sourceRef : "";
}

function finalPreviewBadge(region) {
  const definitionRegion = definitionRegionById(region.regionId);
  if (!definitionRegion) return region.regionId || "";
  const grain = region.payload?.grain || definitionRegion.context?.grain?.value?.unit || "";
  const sourceRef = regionDatasetSourceRef(definitionRegion);
  if (grain && sourceRef) return `${grain} · ${sourceRef}`;
  if (grain) return grain;
  if (sourceRef) return sourceRef;
  return definitionRegion.role || region.componentType || region.regionId || "";
}

// 计划绑定的草稿版本和当前草稿不一致 = 这份计划已经过期，不能直接接受
function isPendingPlanStale(plan) {
  if (!plan || plan.draftRevision === null) return false;
  const current = session.baseDefinitionRevision;
  if (current === null || current === undefined) return false;
  return Number(plan.draftRevision) !== Number(current);
}

function isRepeatedPlan(plan) {
  if (!plan?.planId || !lastPendingPlan) return false;
  return (
    lastPendingPlan.planId === plan.planId &&
    String(lastPendingPlan.draftRevision) === String(plan.draftRevision)
  );
}

// 渲染「待确认的修改」：具体摘要 + 接受 / 继续调整 / 取消。
// 这张卡只描述计划，不动草稿；真正写草稿由后端收到「接受修改」后做。
// 返回值：这份计划对应的业务菜单（过期计划没有可选菜单，返回 null）。
function appendPendingEditPlan(plan, { lock = true, question = null } = {}) {
  if (!plan) return null;

  const visibleIds = visibleBodyRegionIds();
  const targetIndex = plan.targetRegionId ? visibleIds.indexOf(plan.targetRegionId) : -1;
  const targetLabel = plan.targetRegionId ? visibleRegionLabel(plan.targetRegionId) : "";
  const stale = isPendingPlanStale(plan);

  const operationItems = plan.operations
    .map((op) => {
      const described = describeEditOperation(op);
      if (!described) return "";
      const details = described.details.length
        ? `<div class="text-[11px] text-slate-500 mt-0.5">${esc(described.details.join(" · "))}</div>`
        : "";
      return `<li class="text-xs text-slate-700">${esc(described.title)}${details}</li>`;
    })
    .join("");

  const wrap = aiBubble(`
    <div class="rounded-xl border border-amber-200 bg-white overflow-hidden" data-pending-plan="${esc(plan.planId || "")}">
      <div class="px-3.5 py-2.5 bg-amber-50 border-b border-amber-100 flex items-center gap-2">
        <iconify-icon class="text-amber-600" icon="mdi:clipboard-text-clock-outline"></iconify-icon>
        <span class="text-sm font-semibold text-amber-900">待确认的修改</span>
        <span class="ml-auto mono text-[10px] text-slate-500 bg-white border border-slate-200 rounded px-1.5 py-0.5">${esc(plan.planId || "no-plan-id")}</span>
      </div>
      <div class="p-3 space-y-2 text-xs text-slate-700" data-plan-body>
        <div>
          <span class="text-slate-500">目标：</span>
          ${
            targetIndex >= 0
              ? `<span class="font-medium text-slate-800">第 ${targetIndex + 1} 个正文卡片「${esc(targetLabel)}」</span> <span class="mono text-[10px] text-slate-500">${esc(plan.targetRegionId || "")}</span>`
              : `<span class="text-rose-600">目标卡片不在当前可见正文顺序里</span> <span class="mono text-[10px] text-slate-500">${esc(plan.targetRegionId || "")}</span>`
          }
        </div>
        ${plan.summary ? `<div class="whitespace-pre-wrap">${esc(plan.summary)}</div>` : ""}
        ${
          operationItems
            ? `<div><div class="text-[11px] text-slate-500 mb-1">具体改什么</div><ul class="list-disc pl-4 space-y-1">${operationItems}</ul></div>`
            : ""
        }
        ${plan.unverified ? `<div class="text-[11px] text-amber-800">未验证能力：${esc(plan.unverified)}</div>` : ""}
        <div class="text-[11px] text-slate-400">草稿版本 baseRev=${esc(plan.draftRevision ?? "—")}</div>
        <div class="text-[11px] text-slate-400">当前为 Mock 预览，真实数据编译尚未完成。</div>
      </div>
    </div>
  `);

  const body = wrap.querySelector("[data-plan-body]");

  if (stale) {
    body.insertAdjacentHTML(
      "beforeend",
      `<div class="text-[11px] text-rose-600">这份修改是对着旧草稿做的，已经过期，不能直接接受。请重新说明要怎么改。</div>`
    );
    // 过期计划不再提供可选项，同时把之前那轮的输入锁放掉
    if (lock) clearChoiceLock();
    return null;
  }

  // 选项值固定用契约里的三个中文词（发回后端的消息正文就是它），
  // 后端给了问题对象就沿用它的 id 和文案，锁的 id 才对得上 pending_choice。
  const options = [
    { value: EDIT_PLAN_CONTRACT.choice.accept, label: EDIT_PLAN_CONTRACT.choice.accept },
    { value: EDIT_PLAN_CONTRACT.choice.adjust, label: EDIT_PLAN_CONTRACT.choice.adjust },
    { value: EDIT_PLAN_CONTRACT.choice.cancel, label: EDIT_PLAN_CONTRACT.choice.cancel },
  ].map((option, index) => {
    const backendOption = question?.options?.[index];
    if (!backendOption) return option;
    const label = backendOption.label || backendOption.value || option.label;
    return { value: option.value, label: String(label) };
  });
  const questionId = question?.id || "pending_edit_plan_confirm";

  const menu = document.createElement("div");
  menu.className = "px-3 pb-3";
  menu.innerHTML = `
    <label class="block text-sm text-slate-700 leading-relaxed mb-2">确认后才会修改草稿，请选择：</label>
    <select class="question-select w-full rounded-md border border-amber-200 bg-white px-3 py-2 text-sm text-slate-700" data-question-id="${esc(questionId)}">
      <option value="">请选择</option>
      ${options.map((option) => `<option value="${esc(option.value || option.label)}">${esc(option.label)}</option>`).join("")}
    </select>
  `;
  body.appendChild(menu);

  const select = menu.querySelector(".question-select");
  select.addEventListener("change", (event) => {
    if (lockedChoice?.id === questionId && event.target.value) {
      sendMessage(event.target.value, { fromChoice: true });
    }
  });

  const menuQuestion = { id: questionId, selectionOnly: true, options };
  if (lock) setChoiceLock(menuQuestion);
  else businessMenuReplayed = true;
  return menuQuestion;
}

// -------- 请求 --------
async function postAgentStream(body, debugCard, signal) {
  const res = await fetch(API_BASE + "/api/agent/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let finalData = null;

  const handleBlock = (block) => {
    if (!block.trim()) return;
    let event = "message";
    const dataLines = [];
    block.split("\n").forEach((line) => {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    });
    if (!dataLines.length) return;
    const payload = JSON.parse(dataLines.join("\n"));
    if (event === "final") {
      finalData = payload;
      addDebugEvent(debugCard, "final", { ...payload.debug, stage: "router_return", state: payload.state, elapsedMs: payload.elapsedMs });
    } else if (event === "error") {
      addDebugEvent(debugCard, "router_failed", payload);
      throw new Error(payload.message || "stream error");
    } else if (event === "reply_delta") {
      // 最终回复仍由现有渲染逻辑统一展示，避免同一句话重复出现。
    } else {
      addDebugEvent(debugCard, event, payload);
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const part of parts) handleBlock(part);
    if (done) break;
  }
  if (buffer.trim()) handleBlock(buffer);
  if (!finalData) throw new Error("stream finished without final response");
  return finalData;
}

async function sendMessage(text, { fromChoice = false, designAction = null } = {}) {
  if (!text || activeRequest || advanceTimers.size) return;
  if (lockedChoice && fromChoice) {
    // 从菜单里选的必须是菜单自己的选项。
    // 自由输入不再被前端拦（只读提问要走这条路），执行约束由后端把关。
    const allowed =
      lockedChoice.options?.some((o) => (o.value || o.label) === text) ||
      (lockedChoice.allowRestart && text === "重新开始");
    if (!allowed) {
      toast("请先从下拉框选择");
      return;
    }
  }
  // 首次对话前先向后端要一个会话 id
  if (!session.sessionId && !session.pendingResume) {
    try {
      const created = await sessionApi.create();
      session.sessionId = created.sessionId;
      // 记下当前会话，刷新后能自动接回来
      writeLastSessionId(session.sessionId);
    } catch (err) {
      toast("创建会话失败：" + err.message);
      return;
    }
  }
  const visibleText = text;
  const resumeCommand = /^(继续工作|继续上一条|继续刚才)$/.test(text.trim());
  const pendingResume = resumeCommand ? session.pendingResume : null;
  if (resumeCommand && !pendingResume) {
    appendUser(visibleText);
    appendAiText("当前没有可恢复的上一条请求，请重新描述要处理的内容。");
    return;
  }
  session.pendingResume = null;
  if (pendingResume) text = pendingResume.body.message;
  const epoch = requestEpoch;
  const isAdvance = text === "__advance__";
  if (!isAdvance) appendUser(visibleText);

  const context = { ...DEFAULT_CONTEXT };
  if (session.designSessionId) context.designSessionId = session.designSessionId;
  if (session.reportDefinitionId) context.reportDefinitionId = session.reportDefinitionId;
  if (session.baseDefinitionRevision != null) context.baseDefinitionRevision = session.baseDefinitionRevision;
  // 设计动作（#19）：按钮明确传递动作与所依据的版本。
  // 服务端据此做版本冲突检查 —— 客户端带的版本只用来识别「过期」。
  if (designAction) Object.assign(context, designAction);
  if (session.editingTargetRegionId) context.selectedRegionId = session.editingTargetRegionId;
  // 客户可见正文卡片的顺序：后端按这个顺序理解「第三个」这类说法
  const visibleRegionIds = visibleBodyRegionIds();
  if (visibleRegionIds.length) {
    context[EDIT_PLAN_CONTRACT.requestVisibleRegionIdsField] = visibleRegionIds;
  }

  const body = pendingResume ? {
    ...pendingResume.body,
    conversationId: session.conversationId,
    resumeOfConversationId: pendingResume.conversationId,
    // 重发同一轮消息，复用原来的 messageId，让后端按幂等键覆盖而不是新增
    messageId: pendingResume.body.messageId,
  } : {
    agentProtocolVersion: "1.0",
    conversationId: session.conversationId,
    sessionId: session.sessionId,
    messageId: newMessageId(),
    state: session.state,
    message: text,
    context,
  };

  // 把当前草稿作为 snapshots 带回后端，确认时需要用于保存
  if (!pendingResume && (session.definition || session.policy || session.lastReportRun)) {
    body.snapshots = {};
    if (session.policy) body.snapshots.reportPolicy = session.policy;
    if (session.definition) body.snapshots.reportDefinition = session.definition;
    if (session.lastReportRun) body.snapshots.lastReportRun = session.lastReportRun;
  }

  // 修改语义：命中某个区域时提示 Agent。
  // 只在明确是「要改」的说法时给；「你支持分钟趋势吗？」是提问，
  // 不该被当成要改趋势图（规格 #12：问能力不得夹带执行）。
  if (!pendingResume && session.state === "WAITING_USER_CONFIRM" && WorkbenchQuery.shouldHintTrendRegion(text)) {
    context.selectedRegionId = "region_hourly_trend";
  }

  const thinking = appendAiThinking();
  const debugCard = appendDebugTrace({ text, state: body.state, isAdvance });
  const controller = new AbortController();
  activeRequest = {
    controller, thinking, debugCard,
    resumeRequest: { body: JSON.parse(JSON.stringify(body)), fromChoice, isAdvance },
  };
  // 方案在处理中：确认入口置灰，避免用户对着上一版点确认。
  if (planState.designPhase || planState.reportPlan) {
    planState.busy = true;
    planState.error = null;
    renderPlanModule();
  }
  syncInputControls();
  let data;
  try {
    data = await postAgentStream(body, debugCard, controller.signal);
    if (epoch !== requestEpoch) {
      // 请求已被「新建对话」摘下：不往新会话的界面渲染，
      // 但后端在发 final 之前已经落库了，刷新侧栏让用户能找回这条会话。
      await renderSessionList();
      return;
    }
    finishDebugTrace(debugCard, "done");
  } catch (err) {
    if (epoch !== requestEpoch || err.name === "AbortError") return;
    finishDebugTrace(debugCard, "failed");
    appendAiText("请求失败：" + err.message);
    return;
  } finally {
    if (activeRequest?.controller === controller) {
      activeRequest = null;
      removeBubble(thinking);
      syncInputControls();
    }
    // 本轮结束：方案不再「处理中」。确认入口恢复可用与否，
    // 交给 planCanConfirm 按当前状态重新判定。
    if (planState.busy) {
      planState.busy = false;
      renderPlanModule();
    }
  }

  if (epoch !== requestEpoch) return;
  // 方案状态跟着响应走：成功、失败、冲突都如实反映到方案卡片上。
  syncPlanFromResponse(data);
  if (fromChoice) clearChoiceLock();
  if (data.error) {
    appendAiText(`[${data.error.code}] ${data.error.message}`);
    return;
  }

  // 只读问答：不推进业务。
  // 这一轮只是回答问题或澄清意图，业务状态、待确认菜单、待确认修改计划、
  // 右侧预览和 __advance__ 自动推进全部保持原样（规格 #12 / 工单 01、02）。
  const readOnlyKind = WorkbenchQuery.readOnlyAnswerKind(data);

  // 同步会话态
  if (!WorkbenchQuery.shouldPreserveBusinessState(data)) {
    setState(data.state);
  }
  if (data.designSessionId) session.designSessionId = data.designSessionId;
  if (!readOnlyKind && data.debug && Object.prototype.hasOwnProperty.call(data.debug, "editingTargetRegionId")) {
    session.editingTargetRegionId = data.debug.editingTargetRegionId || null;
  }

  if (readOnlyKind) {
    // 只读回答单独展示：不画业务确认菜单、不改右侧预览、不触发自动推进。
    renderReadOnlyAnswer(data, readOnlyKind);
    // 查询轮带回的原菜单：屏幕上还没有菜单时才补画（已有就不重复画）。
    if (!lockedChoice) {
      const preservedMenu = resolveQueryPreservedMenu(data);
      if (preservedMenu) appendConfirmActions(preservedMenu);
    }
    updateMeta();
    renderSessionList();
    return;
  }

  // 待确认编辑计划：后端下发候选计划，前端只负责展示 + 等用户选择，绝不自己改草稿。
  // 同一份计划重复/迟到到达时不重复画卡，也不重新抢输入锁。
  const pendingPlan = readPendingEditPlan(data);
  let pendingPlanRendered = false;
  const renderPendingPlan = ({ lock = true } = {}) => {
    if (!pendingPlan || pendingPlanRendered) return;
    if (isRepeatedPlan(pendingPlan)) return;
    appendPendingEditPlan(pendingPlan, { lock, question: resolvePlanQuestion(data) });
    lastPendingPlan = { planId: pendingPlan.planId, draftRevision: pendingPlan.draftRevision };
    pendingPlanRendered = true;
  };

  // 分发渲染
  switch (data.state) {
    case "ANALYZING":
      if (data.uiActions?.includes("restart_from_template_editing") || text === "重新开始") {
        session.definition = null;
        session.policy = null;
        session.reportDefinitionId = null;
        session.baseDefinitionRevision = null;
        session.editingTargetRegionId = null;
        session.lastReportRun = null;
        session.previewPayload = null;
        destroyCharts();
        previewRoot.innerHTML = "";
        previewRoot.appendChild(previewEmpty);
      }
      appendAiText(data.reply || "我在。你可以直接描述想生成什么报表，或者问我能做什么。");
      if (data.questions?.length) appendAiQuestions(data.questions);
      break;
    case "NEEDS_CLARIFICATION":
      appendAiText(data.reply || "我需要你再补充一些信息：");
      if (pendingPlan) {
        renderPendingPlan();
      } else if (data.questions?.length) {
        appendAiQuestions(data.questions);
      }
      break;
    case "BLOCKED_OR_UNSUPPORTED":
      if (data.blocks?.length) appendAiBlocks(data.blocks);
      else appendAiText(data.reply || "这个请求当前不能继续处理。");
      break;
    case "WAITING_USER_CONFIRM":
      session.definition = data.domainObjects?.reportDefinitionDraft || session.definition;
      session.policy = data.domainObjects?.reportPolicyDraft || session.policy;
      session.editingTargetRegionId = null;
      session.reportDefinitionId = session.definition?.metadata?.id || session.reportDefinitionId;
      session.baseDefinitionRevision = session.definition?.metadata?.revision ?? session.baseDefinitionRevision;
      appendAiText(data.reply || '已生成报表草稿，右侧可以直接编辑；满意后点"同意"。');
      renderPreview(session.definition);
      if (pendingPlan) {
        // 后端把待确认计划放在这个状态时，按新契约走计划卡，不画旧的草稿确认菜单
        renderPendingPlan();
      } else {
        appendConfirmActions(data.questions?.find((question) => question.id === "template_edit_confirm"));
      }
      break;
    case "USER_EDITING_TEMPLATE":
      session.definition = data.domainObjects?.reportDefinitionDraft || session.definition;
      session.policy = data.domainObjects?.reportPolicyDraft || session.policy;
      session.baseDefinitionRevision = session.definition?.metadata?.revision ?? session.baseDefinitionRevision;
      appendAiText(data.reply || "请继续说明要怎么编辑当前草稿。");
      if (!pendingPlan) {
        appendTemplateEditSummary(
          data.domainObjects?.changeSet,
          data.debug?.markdownPreview
        );
      }
      if (session.definition) renderPreview(session.definition, session.editingTargetRegionId ? [session.editingTargetRegionId] : []);
      if (pendingPlan) {
        // 计划卡片自带「接受修改 / 继续调整 / 取消」，不再画旧的「同意 / 修改 / 重新开始」
        renderPendingPlan();
      } else if (data.uiActions?.includes("confirm_template_edit")) {
        appendConfirmActions(data.questions?.find((question) => question.id === "template_edit_confirm"));
      } else if (data.questions?.length) {
        appendAiQuestions(data.questions);
      }
      break;
    case "SAVE_DEFINITION_DRAFT":
      // 后端保存后带回了 saved definitionId / revision
      {
        const saveTool = (data.toolRequests || []).find((t) => t.type === "SAVE_DEFINITION_DRAFT");
        if (saveTool?.arguments?.definitionId) {
          session.reportDefinitionId = saveTool.arguments.definitionId;
          session.baseDefinitionRevision = saveTool.arguments.revision;
        }
      }
      appendAiText(data.reply || "草稿已保存，接下来进入校验。");
      // 自动推进到 VALIDATING（只读问答不推进）
      if (WorkbenchQuery.shouldAutoAdvance(data)) queueAdvance(450, epoch);
      break;
    case "VALIDATING":
      appendAiText(data.reply || "正在校验报表定义…");
      // 需要再推一次，这次会真正校验并给出通过/失败（只读问答不推进）
      if (WorkbenchQuery.shouldAutoAdvance(data)) queueAdvance(650, epoch);
      break;
    case "VALIDATION_FAILED":
      appendAiText(data.reply || "校验未通过，请检查报表定义。");
      break;
    case "PREVIEWING":
      appendAiText(data.reply || "正在跑预览…");
      if (WorkbenchQuery.shouldAutoAdvance(data)) queueAdvance(900, epoch);
      break;
    case "PREVIEW_READY":
      if (data.domainObjects?.reportRunPreview) {
        session.lastReportRun = data.domainObjects.reportRunPreview;
      }
      appendAiText(data.reply || "预览已就绪。");
      await fetchAndStorePreview(epoch);
      if (epoch !== requestEpoch) return;
      if (WorkbenchQuery.shouldAutoAdvance(data)) queueAdvance(500, epoch);
      break;
    case "RENDERING":
      appendAiText(data.reply || "正在渲染报表界面…");
      // 渲染出问题（图表库挂了、某个区域数据异常）不能把状态机按死在 RENDERING。
      try {
        await renderFinalReport();
      } catch (err) {
        console.error("[AI Report] 渲染报表失败", err);
        appendAiText("报表渲染遇到问题，已跳过部分内容，流程继续。");
      }
      if (epoch !== requestEpoch) return;
      if (WorkbenchQuery.shouldAutoAdvance(data)) queueAdvance(450, epoch);
      break;
    case "READY":
      appendAiText(data.reply || "报表已就绪。");
      appendReadyActions(data.questions?.find((question) => question.id === "ready_action"));
      break;
    case "DEFINITION_DRAFT_READY": {
      // 这个状态以前只画一张变更卡：右侧没有草稿、没有确认下拉、也没有输入锁，
      // 用户既看不到草稿也走不下去（死路）。
      // 现在：草稿同步到右侧 + 变更卡 + 一个一定能点的确认下拉。
      const draft = data.domainObjects?.reportDefinitionDraft;
      if (draft) {
        session.definition = draft;
        session.policy = data.domainObjects?.reportPolicyDraft || session.policy;
        session.reportDefinitionId = draft?.metadata?.id || session.reportDefinitionId;
        session.baseDefinitionRevision = draft?.metadata?.revision ?? session.baseDefinitionRevision;
        renderPreview(session.definition);
      }
      appendAiText(data.reply || "已生成修改方案。");
      if (data.domainObjects?.changeSet) {
        appendAiChangeSet(data.domainObjects.changeSet);
      }
      if (pendingPlan) {
        // 有待确认计划时，计划卡自带「接受修改 / 继续调整 / 取消」，不再叠一层确认下拉。
        renderPendingPlan();
      } else {
        // 后端给了 questions 就用它的（id、选项值要和后端的 pending_choice 对得上）；
        // 只给了 changeSet 就补一套兜底确认项，保证这个状态永远有路可走。
        const confirmQuestion = resolveDefinitionDraftQuestion(data) || FALLBACK_DRAFT_CONFIRM_QUESTION;
        appendConfirmActions(confirmQuestion);
      }
      break;
    }
    default:
      appendAiText(`当前状态：${data.state}`);
      // 后端新增的状态如果带了待确认计划，一样要画出来并锁住输入
      if (pendingPlan) renderPendingPlan();
  }

  updateMeta();
  renderSessionList();
}

async function fetchAndStorePreview(epoch) {
  const runId = session.lastReportRun?.reportRunId;
  if (!runId) return;
  const controller = new AbortController();
  const thinking = appendAiThinking();
  activeRequest = { controller, thinking, debugCard: null };
  syncInputControls();
  try {
    const res = await fetch(API_BASE + "/api/report/preview/" + runId, { signal: controller.signal });
    if (!res.ok) throw new Error("preview fetch failed: " + res.status);
    const payload = await res.json();
    if (epoch === requestEpoch) session.previewPayload = payload;
  } catch (err) {
    if (epoch === requestEpoch && err.name !== "AbortError") toast("预览数据拉取失败：" + err.message);
  } finally {
    if (activeRequest?.controller === controller) {
      activeRequest = null;
      removeBubble(thinking);
      syncInputControls();
    }
  }
}

function sendTemplateEditAction(message, regionId = null) {
  if (lockedChoice || activeRequest || advanceTimers.size) {
    toast("请先完成当前选择，或停止正在生成的请求");
    return;
  }
  session.editingTargetRegionId = regionId;
  setState("USER_EDITING_TEMPLATE");
  return sendMessage(message);
}

// 节点 12 READY：先选下一步，选“继续修改”后才开放输入。
// lock=false 用于回放历史消息：渲染出下拉框，但不抢占当前输入锁
// 返回值：实际画出来的业务菜单（没有菜单时返回 null）。
function appendReadyActions(question, { lock = true } = {}) {
  if (!question?.selectionOnly || !question.options?.length) {
    // 同 appendConfirmActions：缺选项时不抢输入锁，避免把已有下拉框全 disable 掉。
    if (lock) {
      appendAiText("就绪操作未就绪，请新建对话后重试。");
    }
    return null;
  }
  const wrap = aiBubble(`
    <div class="rounded-xl rounded-tl-sm border border-emerald-200 bg-white px-3.5 py-3">
      <label class="block text-sm text-slate-700 leading-relaxed mb-2.5">${esc(question.prompt)}</label>
      <select class="question-select w-full rounded-md border border-emerald-200 bg-white px-3 py-2 text-sm text-slate-700" data-question-id="${esc(question.id || "")}">
        <option value="">请选择</option>
        ${question.options.map((option) => `<option value="${esc(option.value)}">${esc(option.label)}</option>`).join("")}
      </select>
    </div>
  `);
  if (lock) setChoiceLock(question);
  else businessMenuReplayed = true;
  wrap.querySelector(".question-select").addEventListener("change", (event) => {
    if (lockedChoice?.id === question?.id && event.target.value) {
      sendMessage(event.target.value, { fromChoice: true });
    }
  });
  return question;
}

// -------- 应用 ChangeSet --------
function applyChangeSet(cs, bubbleWrap) {
  if (!session.definition?.regions) {
    // 没有草稿可改：别让用户以为按钮坏了，明确告诉他走下面的确认下拉。
    toast("当前没有可修改的草稿，请在下方确认下拉里选择如何处理。");
    return;
  }

  const definition = session.definition;

  for (const entry of cs.changed || []) {
    const regionIdx = definition.regions.findIndex((r) => r.id === entry.regionId);

    if (entry.after === null) {
      // 删除
      if (regionIdx >= 0) definition.regions.splice(regionIdx, 1);
      continue;
    }

    if (entry.path === `/regions/${entry.regionId}/context/timeWindow`) {
      if (regionIdx >= 0) {
        definition.regions[regionIdx].context = definition.regions[regionIdx].context || {};
        definition.regions[regionIdx].context.timeWindow = entry.after;
      }
    }
  }

  // 自增 revision
  if (definition.metadata) {
    definition.metadata.revision = (definition.metadata.revision ?? 0) + 1;
    session.baseDefinitionRevision = definition.metadata.revision;
  }

  // UI 反馈
  bubbleWrap.querySelector(".btn-accept").parentElement.innerHTML =
    '<span class="inline-flex items-center gap-1 rounded-lg bg-emerald-50 text-emerald-700 border border-emerald-200 text-xs font-medium px-2.5 py-1.5"><iconify-icon icon="mdi:check" class="text-sm"></iconify-icon>变更已应用</span>';

  setState("WAITING_USER_CONFIRM"); // 应用后回到确认态
  renderPreview(definition, cs.changed.map((e) => e.regionId));
  updateMeta();
  toast("变更已应用，revision → " + definition.metadata.revision);
}

// -------- 渲染预览 --------
// 报告标题区（REPORT_TITLE）不单独占一块：顶部摘要卡已经是报表标题，
// 再渲染一次会出现两个标题（顶部「报表名」+ 下面「A 机房 昨日能耗日报」）。
function isTitleRegion(region) {
  return region?.role === "REPORT_TITLE";
}

// 区域编辑入口（改标题 / 展示方式 / 粒度 / 删除）只在编辑态开放：
// 用户在下拉菜单选了「修改」之后才出现，没选之前右侧是只读的。
// 还有一种情况是编辑到一半又弹出「同意 / 修改 / 重新开始」的选择框，
// 这时输入被锁着，编辑按钮点了也只会提示，先收起来。
function isEditingEnabled() {
  return session.state === "USER_EDITING_TEMPLATE" && !lockedChoice;
}

// 编辑入口的显隐跟着状态走，不用重画整块预览：
// 显隐用 CSS 类（.region-tools / .edit-hint），标题能不能直接改用 contenteditable 属性。
function syncPreviewEditability() {
  const editable = isEditingEnabled();
  previewRoot.classList.toggle("preview-editable", editable);
  previewRoot.querySelectorAll(".report-name, .region-name").forEach((el) => {
    if (editable) {
      el.setAttribute("contenteditable", "true");
      el.setAttribute("spellcheck", "false");
    } else {
      el.removeAttribute("contenteditable");
      el.removeAttribute("spellcheck");
    }
  });
}

// -------- 生成参数来源（系统暂选 / 用户指定） --------
// 后端把每个生成参数的来源写在 definition.metadata.generationInputs 里。
// 以前只在聊天气泡和顶部 metadata 里出现，右侧草稿面板看不到，
// 用户分不清哪些是自己提的、哪些是系统替他选的。
const GENERATION_INPUT_LABELS = {
  room: "机房",
  time: "时间",
  metric: "指标",
  scope: "范围",
  source: "数据源",
  grain: "粒度",
  device: "设备",
};

function renderGenerationInputs(definition) {
  const inputs = definition?.metadata?.generationInputs;
  if (!inputs || typeof inputs !== "object") return "";
  const entries = Object.entries(inputs).filter(([, item]) => item && typeof item === "object");
  if (!entries.length) return "";

  const chips = entries
    .map(([field, item]) => {
      const source = readText(item.source) || "未标注";
      const value = readText(item.value);
      const fromUser = /用户/.test(source);
      const chipClass = fromUser
        ? "bg-blue-50 text-blue-700 border-blue-200"
        : "bg-slate-100 text-slate-600 border-slate-200";
      const badgeClass = fromUser
        ? "bg-white border border-blue-200 text-blue-700"
        : "bg-white border border-slate-200 text-slate-600";
      const label = GENERATION_INPUT_LABELS[field] || field;
      return `
      <span class="inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] ${chipClass}" data-generation-input="${esc(field)}" data-generation-source="${esc(source)}">
        <span class="text-slate-500">${esc(label)}</span>
        ${value ? `<span class="font-medium text-slate-800">${esc(value)}</span>` : ""}
        <span class="rounded px-1 py-px text-[10px] ${badgeClass}">${esc(source)}</span>
      </span>`;
    })
    .join("");

  return `
    <div class="mt-3 pt-3 border-t border-slate-100" data-generation-inputs>
      <div class="flex items-center gap-1.5 text-[11px] text-slate-500 mb-1.5">
        <iconify-icon class="text-slate-400" icon="mdi:source-branch"></iconify-icon>生成参数来源
      </div>
      <div class="flex flex-wrap gap-1.5">${chips}</div>
    </div>`;
}

function renderPreview(definition, highlightRegionIds = []) {
  // 先销毁旧图表实例：直接 innerHTML="" 只会移除 DOM，
  // ECharts 实例仍然持有对已脱离文档节点的引用，内存泄漏 + 后续 resize 报错。
  destroyCharts();
  if (!definition || !definition.regions?.length) {
    previewRoot.innerHTML = "";
    previewRoot.appendChild(previewEmpty);
    return;
  }
  previewRoot.innerHTML = "";

  const bodyRegions = definition.regions.filter((r) => !isTitleRegion(r));

  // 顶部摘要（报表名）
  const summary = document.createElement("div");
  summary.className = "bg-white rounded-xl border border-slate-200 p-5";
  summary.innerHTML = `
    <div class="flex items-start justify-between gap-4">
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2 flex-wrap">
          <h2 class="report-name editable-field text-lg font-semibold text-slate-900">${esc(definition.metadata?.name || "报表草稿")}</h2>
          <span class="inline-flex items-center rounded-md text-xs font-medium px-2 py-1 border bg-blue-50 text-blue-700 border-blue-200">${esc(definition.schemaVersion || "1.0")}</span>
          <span class="inline-flex items-center gap-1 rounded-md text-xs font-medium px-2 py-1 border bg-amber-50 text-amber-700 border-amber-200 draft-status-badge">
            <iconify-icon class="text-sm" icon="mdi:robot-outline"></iconify-icon>${esc(definition.metadata?.status || "DRAFT")}
          </span>
          <span class="edit-hint text-[11px] text-slate-400 inline-flex items-center gap-1"><iconify-icon icon="mdi:pencil-outline"></iconify-icon>点击名称可直接编辑</span>
        </div>
        <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500 draft-meta-row">
          <span>revision: <span class="mono">${definition.metadata?.revision ?? "—"}</span></span>
          <span>区域数: <span class="mono">${bodyRegions.length}</span></span>
          <span>数据集: ${Object.keys(definition.datasets || {}).map((d) => `<span class="mono">${esc(d)}</span>`).join(", ") || "—"}</span>
        </div>
        ${renderGenerationInputs(definition)}
      </div>
    </div>
  `;
  const nameEl = summary.querySelector(".report-name");
  nameEl.addEventListener("blur", () => {
    if (!isEditingEnabled()) {
      nameEl.textContent = definition.metadata?.name || "报表草稿";
      return;
    }
    const newName = nameEl.textContent.trim();
    const originalName = definition.metadata?.name || "报表草稿";
    if (newName && newName !== originalName) {
      nameEl.textContent = originalName;
      sendTemplateEditAction(`报表名称改成${newName}`);
    } else {
      nameEl.textContent = originalName;
    }
  });
  nameEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      nameEl.blur();
    }
  });
  previewRoot.appendChild(summary);

  // 各区域
  for (const region of bodyRegions) {
    const card = renderRegion(region);
    if (highlightRegionIds.includes(region.id)) {
      card.classList.add("region-highlight");
    }
    previewRoot.appendChild(card);
  }
  syncPreviewEditability();
}

function renderRegion(region) {
  const card = document.createElement("div");
  card.className = "bg-white rounded-xl border border-slate-200 p-5 group";
  card.dataset.regionId = region.id;

  const header = `
    <div class="flex items-start justify-between gap-4">
      <div class="flex-1 min-w-0">
        <h3 class="region-name editable-field font-semibold text-slate-900">${esc(region.name || region.component?.title || region.role)}</h3>
        <p class="text-xs text-slate-500 mt-0.5">role: <span class="mono">${esc(region.role)}</span> · component: <span class="mono">${esc(region.component?.type)}</span>${region.component?.chartKind ? ' / <span class="mono">' + esc(region.component.chartKind) + "</span>" : ""}</p>
      </div>
      <div class="flex items-center gap-1 shrink-0">
        <span class="region-tools">
          ${region.data ? `
            <button class="btn-display-options h-7 rounded border border-slate-200 hover:bg-blue-50 hover:border-blue-200 hover:text-blue-700 text-[11px] text-slate-500 px-2 transition" title="查看适合当前数据的展示方式">展示方式</button>
            <button class="btn-grain-options h-7 rounded border border-slate-200 hover:bg-blue-50 hover:border-blue-200 hover:text-blue-700 text-[11px] text-slate-500 px-2 transition" title="查看当前数据源支持的粒度">粒度</button>
          ` : ""}
          <button class="btn-del-region w-7 h-7 rounded border border-slate-200 hover:bg-rose-50 hover:border-rose-200 hover:text-rose-600 text-slate-400 flex items-center justify-center transition" title="删除此区域">
            <iconify-icon class="text-base" icon="mdi:trash-can-outline"></iconify-icon>
          </button>
        </span>
        <span class="mono text-[10px] text-slate-500 bg-slate-100 border border-slate-200 rounded px-1.5 py-0.5 shrink-0">${esc(region.id)}</span>
      </div>
    </div>`;

  const { html: body, node: bodyNode } = renderRegionBody(region);

  card.innerHTML = header + '<div class="mt-3 region-body">' + body + "</div>";

  // 图表区域：真的拿到数据点时画真实图表（renderChart 由方案模块导出，见 W-14）。
  if (bodyNode) {
    const bodyWrap = card.querySelector(".region-body");
    if (bodyWrap) bodyWrap.appendChild(bodyNode);
  }

  // 占位原因与数据来源性质（#39 ↔ #40 接口约定，见实施票）。
  // 字段缺失时静默降级：不渲染这两行，不报错、不留空框。
  // ⚠️ 这是全前端唯一一处读 region.templateBinding 的地方：
  //    字段名就是为这件事设计的，不要当成意外耦合删掉。
  const binding = region.templateBinding || {};
  if (binding.dataStatus === "placeholder" && binding.placeholderReason) {
    const reason = document.createElement("div");
    reason.className = "region-placeholder-reason text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1 mt-2";
    reason.dataset.regionPlaceholderReason = "true";
    reason.textContent = "暂无数值：" + binding.placeholderReason;
    card.appendChild(reason);
  } else if (binding.dataStatus === "bound" && binding.pendingReason) {
    // bound 区域：指标已绑定但草稿阶段还没取数（#39 新增的 templateBinding.pendingReason）。
    // 读法与占位原因对称：都从 templateBinding 取，都用 textContent 写。
    const pending = document.createElement("div");
    pending.className = "region-pending-reason text-[11px] text-slate-400 mt-2";
    pending.dataset.regionPendingReason = "true";
    pending.textContent = binding.pendingReason;
    card.appendChild(pending);
  }
  if (binding.dataSourceLabel) {
    const source = document.createElement("div");
    source.className = "region-source-note text-[11px] text-slate-400 mt-2";
    source.dataset.regionSourceNote = "true";
    source.textContent = binding.dataSourceLabel;
    card.appendChild(source);
  }

  // 区域名编辑
  const nameEl = card.querySelector(".region-name");
  const originalName = region.name || region.component?.title || region.role;
  nameEl.addEventListener("blur", () => {
    if (!isEditingEnabled()) {
      nameEl.textContent = originalName;
      return;
    }
    const newName = nameEl.textContent.trim();
    if (newName && newName !== originalName) {
      nameEl.textContent = originalName;
      sendTemplateEditAction(`标题改成${newName}`, region.id);
    } else {
      nameEl.textContent = originalName;
    }
  });
  nameEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      nameEl.blur();
    }
  });

  card.querySelector(".btn-display-options")?.addEventListener("click", () => {
    sendTemplateEditAction("这个 section 可以怎么展示？", region.id);
  });
  card.querySelector(".btn-grain-options")?.addEventListener("click", () => {
    sendTemplateEditAction("这个数据能按哪些粒度编译？", region.id);
  });

  // 区域删除
  card.querySelector(".btn-del-region")?.addEventListener("click", () => {
    if (!confirm(`确定删除「${region.name || region.id}」吗？`)) return;
    sendTemplateEditAction("删除这个 section", region.id);
  });

  return card;
}

/**
 * 区域正文。
 *
 * 返回 { html, node }：
 *   - html：直接拼进卡片的 HTML 片段；
 *   - node：需要用真 DOM 画的内容（目前只有图表），由调用方挂上去。
 */
function renderRegionBody(region) {
  const type = region.component?.type;
  if (type === "TEXT") return { html: renderText(region), node: null };
  if (type === "METRIC_CARD") return { html: renderMetricCard(region), node: null };
  if (type === "TABLE") return { html: renderTable(region), node: null };
  if (type === "CHART") {
    // 事实：草稿路径的 region.data 是**执行绑定描述符**（{inputs, pipeline, outputs}），
    // 不含 xLabels / yValues，所以这里拿不到数据点、始终回落到空壳卡片（不编造数值）。
    // 该渲染器按需求稿 §3.3 保留；草稿路径真的拿到数据点时才会被用到（当前无此数据源）。
    const chartNode = renderRealChart(region);
    if (chartNode) return { html: "", node: chartNode };
    return { html: renderChartPlaceholder(region), node: null };
  }
  if (type === "DATA_QUALITY") return { html: renderDataQuality(region), node: null };
  if (type === "ANALYSIS") return { html: renderAnalysis(region), node: null };
  return {
    html: `<pre class="mono text-[11px] text-slate-500 bg-slate-50 rounded p-2 overflow-x-auto">${esc(JSON.stringify(region.component, null, 2).slice(0, 400))}</pre>`,
    node: null,
  };
}

function renderText(region) {
  const tpl = region.component?.content?.template || "";
  return `<div class="text-lg font-semibold text-slate-800">${esc(tpl)}</div>
    <div class="text-[11px] text-slate-400 mt-1">模板变量将在渲染时由后端注入</div>`;
}

function renderMetricCard(region) {
  const m = region.component?.metric || {};
  return `
    <div class="rounded-lg border border-slate-200 bg-slate-50 px-5 py-4 inline-block">
      <div class="text-[11px] text-slate-500">${esc(m.label || "指标")}</div>
      <div class="text-2xl font-semibold text-slate-900 mt-1">—<span class="text-xs font-normal text-slate-400 ml-1">${esc(m.format?.unitLabel || "")}</span></div>
      <div class="text-[11px] text-slate-400 mt-1">metricRef: <span class="mono">${esc(region.data?.pipeline?.nodes?.[0]?.metricRef || "")}</span></div>
    </div>`;
}

function renderTable(region) {
  const cols = region.component?.columns || [];
  const head = cols
    .map(
      (c) =>
        `<th class="text-left font-medium px-3 py-2 text-slate-500">${esc(c.label)}${c.format?.unitLabel ? ' <span class="text-slate-400 font-normal">(' + esc(c.format.unitLabel) + ")</span>" : ""}</th>`
    )
    .join("");
  const sampleRow = `<tr>${cols.map(() => '<td class="px-3 py-2.5 text-slate-300">—</td>').join("")}</tr>`;
  return `
    <table class="w-full text-sm">
      <thead class="bg-slate-50 text-xs">
        <tr>${region.component?.rowNumber ? '<th class="text-left font-medium px-3 py-2 text-slate-500 w-10">#</th>' : ""}${head}</tr>
      </thead>
      <tbody class="divide-y divide-slate-100">
        ${Array.from({ length: 3 }, () => `<tr>${region.component?.rowNumber ? '<td class="px-3 py-2.5 text-slate-300">—</td>' : ""}${sampleRow.replace(/<\/?tr>/g, "")}</tr>`).join("")}
      </tbody>
    </table>
    <div class="text-[11px] text-slate-400 mt-2">表格样例占位（真实数据将在后端渲染）</div>`;
}

function renderChartPlaceholder(region) {
  const window = region.context?.timeWindow?.value || {};
  return `
    <div class="rounded-lg bg-slate-50 border border-slate-200 h-[200px] flex items-center justify-center">
      <div class="text-center text-slate-400">
        <iconify-icon class="text-4xl" icon="mdi:chart-line"></iconify-icon>
        <div class="text-sm mt-1">${esc(region.component?.chartKind || "LINE")} 图表占位</div>
        <div class="text-[11px] mt-1 mono">${esc(window.unit || "?")} · offset=${window.offset ?? "?"} · count=${window.count ?? "?"}</div>
      </div>
    </div>`;
}

// 草稿区域的 chartKind（LINE / BAR / PIE）→ 图表渲染器认识的 componentType。
const CHART_KIND_TO_COMPONENT_TYPE = {
  LINE: "line_chart",
  BAR: "bar_chart",
  PIE: "pie_chart",
};

/**
 * 图表区域：区域里真的带数据点时画真实图表。
 *
 * 事实（下线拖拽后）：
 *   - 草稿路径的 `region.data` 是**执行绑定描述符**（{inputs, pipeline, outputs}），
 *     **不含** xLabels / yValues —— 所以这里**始终**返回 null，继续显示空壳卡片
 *     （需求稿 §2.6：不编造数值）；
 *   - 真正带 xLabels / yValues 的是**预览 payload**，走的是 ECharts 那条路
 *     （renderFinalRegion），不走这里。
 *
 * 本函数按需求稿 §3.3「严禁删除折线图 SVG 渲染」保留：草稿路径哪天真的拿到
 * 数据点就能直接用上（当前没有这样的数据源）。
 * 调用前必须判空、调用失败必须吞掉：绝不允许因为接图表把草稿面板渲染搞崩。
 */
function renderRealChart(region) {
  const data = region?.data;
  if (!data || typeof data !== "object") return null;
  if (!Array.isArray(data.xLabels) || !data.xLabels.length) return null;
  if (!Array.isArray(data.yValues) || !data.yValues.length) return null;
  const api = window.WorkbenchPlan;
  if (!api || typeof api.renderChart !== "function") return null;
  const kind = String(region.component?.chartKind || "LINE").toUpperCase();
  try {
    return api.renderChart(document, {
      componentType: CHART_KIND_TO_COMPONENT_TYPE[kind] || "line_chart",
      title: region.name || region.component?.title || "",
      data,
    });
  } catch (err) {
    console.error("[AI Report] 图表渲染失败", err);
    return null;
  }
}

function renderDataQuality(region) {
  const fields = region.component?.fields || [];
  return `<div class="flex gap-3">${fields
    .map(
      (f) =>
        `<div class="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2.5 flex-1">
          <div class="text-[11px] text-slate-500">${esc(f)}</div>
          <div class="text-sm font-semibold text-slate-800 mt-1">—</div>
        </div>`
    )
    .join("")}</div>`;
}

function renderAnalysis(region) {
  return `
    <div class="rounded-lg bg-slate-50 border border-slate-200 p-4">
      <div class="text-[11px] text-slate-500 mb-1">分析目标</div>
      <div class="text-sm text-slate-700">${esc(region.component?.goal || "—")}</div>
      <div class="text-[11px] text-slate-400 mt-2">模式: <span class="mono">${esc(region.component?.mode || "")}</span> · promptTemplate: <span class="mono">${esc(region.component?.promptTemplateRef || "")}</span></div>
    </div>`;
}

// -------- 节点 11 RENDERING：用真实预览数据渲染 --------
const _chartInstances = [];

// 图表库可用性：CDN 挂了（断网 / 被墙 / CSP 拦了）也不能让整条状态机卡在 RENDERING。
// 以前这里是 `echarts.init(...)` 裸调，CDN 一失败就抛 ReferenceError，
// renderFinalReport 整个 reject，queueAdvance 永远不会执行 —— 页面永远停在 RENDERING。
function hasEcharts() {
  return typeof window !== "undefined" && !!window.echarts && typeof window.echarts.init === "function";
}

function destroyCharts() {
  for (const c of _chartInstances.splice(0)) {
    try { c.dispose(); } catch (e) {}
  }
}

async function renderFinalReport() {
  const payload = session.previewPayload;
  if (!payload) {
    toast("没有预览数据，无法渲染");
    return;
  }
  destroyCharts();
  previewRoot.innerHTML = "";
  previewRoot.classList.remove("preview-editable"); // 成稿不可编辑

  const snapshot = payload.snapshot || {};
  const yoy = Number(snapshot.yoyPercent || 0);
  const yoyColor = yoy >= 0 ? "text-rose-600" : "text-emerald-600";
  const yoyArrow = yoy >= 0 ? "mdi:arrow-up" : "mdi:arrow-down";

  // 顶部摘要
  const header = document.createElement("div");
  header.className = "bg-white rounded-xl border border-slate-200 p-5";
  header.innerHTML = `
    <div class="flex items-start justify-between gap-4 flex-wrap">
      <div class="min-w-0">
        <div class="flex items-center gap-2 flex-wrap">
          <h2 class="text-lg font-semibold text-slate-900">${esc(payload.reportName || "报表预览")}</h2>
          <span class="inline-flex items-center gap-1 rounded-md text-xs font-medium px-2 py-1 border bg-emerald-50 text-emerald-700 border-emerald-200">
            <iconify-icon class="text-sm" icon="mdi:check-circle-outline"></iconify-icon>预览就绪
          </span>
        </div>
        <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
          <span>机房：<span class="mono">${esc(snapshot.roomName || "—")}</span></span>
          <span>日期：<span class="mono">${esc(payload.logicalDate || "—")}</span></span>
          <span>runId：<span class="mono">${esc(payload.reportRunId || "—")}</span></span>
        </div>
      </div>
      <div class="rounded-lg border border-slate-200 bg-slate-50 px-5 py-3">
        <div class="text-[11px] text-slate-500">总耗电（kWh）</div>
        <div class="text-2xl font-semibold text-slate-900 mt-1">${esc(snapshot.totalEnergyKwh ?? "—")}</div>
        <div class="text-[11px] mt-1 inline-flex items-center gap-0.5 ${yoyColor}">
          <iconify-icon icon="${yoyArrow}" class="text-sm"></iconify-icon>${Math.abs(yoy)}% 较前一日
        </div>
      </div>
    </div>
    ${renderGenerationInputs(session.definition)}
  `;
  previewRoot.appendChild(header);

  // 异常提示
  if (payload.anomalies?.length) {
    const warn = document.createElement("div");
    warn.className = "bg-amber-50 border border-amber-200 rounded-xl p-4";
    warn.innerHTML = `
      <div class="flex items-start gap-2">
        <iconify-icon icon="mdi:alert-outline" class="text-amber-600 text-lg mt-0.5"></iconify-icon>
        <div class="flex-1 min-w-0">
          <div class="text-sm font-semibold text-amber-900 mb-1">数据质量提示</div>
          <ul class="text-xs text-amber-800 space-y-0.5 list-disc ml-4">
            ${payload.anomalies.map((a) => `<li><span class="mono">${esc(a.timeRange)}</span> · ${esc(a.note)}</li>`).join("")}
          </ul>
        </div>
      </div>`;
    previewRoot.appendChild(warn);
  }

  // 每个 region 一张卡（标题区跳过：头部已经显示报表名）
  // 单个区域渲染失败只影响它自己：其余区域照常出，状态机照常往前走。
  const failedRegions = [];
  for (const region of payload.regions || []) {
    if (isTitleRegion(region)) continue;
    const card = document.createElement("div");
    card.className = "bg-white rounded-xl border border-slate-200 p-5";
    card.innerHTML = `
      <div class="flex items-start justify-between gap-4 mb-3">
        <h3 class="font-semibold text-slate-900">${esc(region.name || region.componentType)}</h3>
        <span class="mono text-[10px] text-slate-500 bg-slate-100 border border-slate-200 rounded px-1.5 py-0.5">${esc(finalPreviewBadge(region))}</span>
      </div>
      <div class="region-body"></div>
    `;
    const body = card.querySelector(".region-body");
    previewRoot.appendChild(card);
    try {
      renderFinalRegion(body, region, payload);
    } catch (err) {
      console.error("[AI Report] 区域渲染失败", region?.regionId, err);
      failedRegions.push(region?.regionId || region?.componentType || "未知区域");
      body.innerHTML = `
        <div class="rounded-lg bg-rose-50 border border-rose-200 p-3 text-xs text-rose-700">
          这个区域渲染失败：${esc(err?.message || String(err))}。其余区域和后续流程不受影响。
        </div>`;
    }
  }

  if (failedRegions.length) {
    const failBanner = document.createElement("div");
    failBanner.className = "bg-rose-50 border border-rose-200 rounded-xl p-4";
    failBanner.innerHTML = `
      <div class="flex items-start gap-2">
        <iconify-icon icon="mdi:alert-circle-outline" class="text-rose-600 text-lg mt-0.5"></iconify-icon>
        <div class="flex-1 min-w-0">
          <div class="text-sm font-semibold text-rose-900 mb-1">部分区域没有渲染出来</div>
          <div class="text-xs text-rose-800">${failedRegions.map((id) => `<span class="mono">${esc(id)}</span>`).join("、")} 渲染失败，其余内容已正常展示。</div>
        </div>
      </div>`;
    previewRoot.appendChild(failBanner);
  }

  requestAnimationFrame(() => _chartInstances.forEach((chart) => {
    try { chart.resize(); } catch (e) {}
  }));
}

function renderFinalRegion(container, region, payload) {
  const t = region.componentType;
  const p = region.payload || {};
  // #39 起，占位区域的预览 payload 是 {placeholder: true, reason, ...} ——
  // **没有** value / label / unit / rows / yValues。
  // 任何组件类型都要如实显示「暂无数值 + 原因」：
  //   · 不显示原因 = 用户只看到一个空卡片，不知道为什么没有数；
  //   · 直接插 ${p.value} = 页面上出现字面量 "undefined"。
  if (p.placeholder) {
    container.innerHTML = `
      <div class="rounded-lg border border-dashed border-amber-200 bg-amber-50 px-4 py-3">
        <div class="text-sm font-medium text-amber-900">暂无数值</div>
        <div class="text-[11px] text-amber-800 mt-1">${esc(p.reason || "数据待连接：当前已接通的能力里没有对应指标。")}</div>
      </div>`;
    return;
  }
  if (t === "TEXT") {
    container.innerHTML = `<div class="text-xl font-semibold text-slate-800">${esc(p.rendered || p.template || "")}</div>`;
  } else if (t === "METRIC_CARD") {
    container.innerHTML = `
      <div class="rounded-lg border border-slate-200 bg-slate-50 px-5 py-4 inline-block">
        <div class="text-[11px] text-slate-500">${esc(p.label)}</div>
        <div class="text-3xl font-semibold text-slate-900 mt-1">${esc(p.value ?? "—")}<span class="text-sm font-normal text-slate-400 ml-1">${esc(p.unit)}</span></div>
      </div>`;
  } else if (t === "TABLE") {
    const headCells = (p.columns || [])
      .map(
        (c) =>
          `<th class="text-left font-medium px-3 py-2 text-slate-500">${esc(c.label)}${c.format?.unitLabel ? ` <span class="text-slate-400 font-normal">(${esc(c.format.unitLabel)})</span>` : ""}</th>`
      )
      .join("");
    const bodyRows = (p.rows || [])
      .map((row, idx) => {
        const cells = (p.columns || [])
          .map((c) => {
            const val = row[c.fieldRef];
            const kind = c.format?.kind;
            if (kind === "NUMBER") {
              const prec = c.format.precision ?? 1;
              return `<td class="px-3 py-2.5 text-slate-700 mono">${Number(val).toFixed(prec)}</td>`;
            }
            return `<td class="px-3 py-2.5 text-slate-700">${esc(val ?? "")}</td>`;
          })
          .join("");
        return `<tr class="hover:bg-slate-50">${p.rowNumber ? `<td class="px-3 py-2.5 text-slate-400 mono">${idx + 1}</td>` : ""}${cells}</tr>`;
      })
      .join("");
    container.innerHTML = `
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-xs">
          <tr>${p.rowNumber ? '<th class="text-left font-medium px-3 py-2 text-slate-500 w-10">#</th>' : ""}${headCells}</tr>
        </thead>
        <tbody class="divide-y divide-slate-100">${bodyRows}</tbody>
      </table>`;
  } else if (t === "CHART") {
    // 图表库不可用时给占位，不抛异常：数据已经就绪，不能因为一个 CDN 挂了就卡住整条流程。
    if (!hasEcharts()) {
      container.innerHTML = `
      <div class="rounded-lg bg-amber-50 border border-amber-200 h-[260px] flex items-center justify-center px-4">
        <div class="text-center text-amber-700">
          <iconify-icon class="text-3xl" icon="mdi:chart-line-variant"></iconify-icon>
          <div class="text-sm mt-1">图表库加载失败，但数据已就绪</div>
          <div class="text-[11px] mt-1">ECharts CDN 不可用；该区域的图表暂时画不出来，后续流程不受影响。</div>
        </div>
      </div>`;
      return;
    }
    const div = document.createElement("div");
    div.style.height = "260px";
    container.appendChild(div);
    const kind = p.chartKind || "LINE";
    let chart = null;
    try {
      chart = window.echarts.init(div);
      _chartInstances.push(chart);
      chart.setOption({
        tooltip: { trigger: "axis" },
        grid: { left: 50, right: 20, top: 24, bottom: 36 },
        xAxis: {
          type: "category",
          data: p.xLabels || [],
          axisLabel: { fontSize: 11 },
        },
        yAxis: {
          type: "value",
          name: "kWh",
          nameTextStyle: { fontSize: 10, color: "#64748b" },
          axisLabel: { fontSize: 11 },
        },
        series: [
          {
            name: "耗电",
            data: p.yValues || [],
            type: kind === "BAR" ? "bar" : "line",
            smooth: true,
            areaStyle: kind === "AREA" ? { opacity: 0.3 } : undefined,
            itemStyle: { color: "#2563eb" },
            lineStyle: { color: "#2563eb" },
          },
        ],
      });
    } catch (err) {
      console.error("[AI Report] 图表初始化失败", err);
      container.innerHTML = `
      <div class="rounded-lg bg-amber-50 border border-amber-200 h-[260px] flex items-center justify-center px-4">
        <div class="text-center text-amber-700">
          <iconify-icon class="text-3xl" icon="mdi:chart-line-variant"></iconify-icon>
          <div class="text-sm mt-1">图表库加载失败，但数据已就绪</div>
          <div class="text-[11px] mt-1">${esc(err?.message || String(err))}</div>
        </div>
      </div>`;
    }
  } else if (t === "DATA_QUALITY") {
    const items = [
      { label: "覆盖率", value: p.coverage != null ? (p.coverage * 100).toFixed(1) + "%" : "—" },
      { label: "缺失点", value: p.missing ?? "—" },
      { label: "重复点", value: p.duplicates ?? 0 },
      { label: "新鲜度 (s)", value: p.freshnessSeconds ?? "—" },
    ];
    container.innerHTML = `
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
        ${items
          .map(
            (it) =>
              `<div class="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2.5">
                <div class="text-[11px] text-slate-500">${esc(it.label)}</div>
                <div class="text-sm font-semibold text-slate-800 mt-1">${esc(String(it.value))}</div>
              </div>`
          )
          .join("")}
      </div>`;
  } else if (t === "ANALYSIS") {
    container.innerHTML = `
      <div class="rounded-lg bg-blue-50 border border-blue-200 p-4">
        <div class="flex items-start gap-2">
          <iconify-icon icon="mdi:lightbulb-on-outline" class="text-blue-600 text-lg mt-0.5"></iconify-icon>
          <div class="flex-1 min-w-0">
            <div class="text-[11px] text-blue-700 mb-1">AI 解读</div>
            <div class="text-sm text-slate-800 leading-relaxed">${esc(p.analysis || "")}</div>
          </div>
        </div>
      </div>`;
  } else {
    container.innerHTML = `<pre class="mono text-[11px] text-slate-500 bg-slate-50 rounded p-2">${esc(JSON.stringify(p, null, 2).slice(0, 400))}</pre>`;
  }
}

// -------- 会话导航栏 --------
const sessionListRoot = $("session-list");

// 按天分组：今天 / 昨天 / 更早
function groupByDay(items) {
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  const startOfYesterday = new Date(startOfToday);
  startOfYesterday.setDate(startOfYesterday.getDate() - 1);
  const groups = { today: [], yesterday: [], earlier: [] };
  for (const item of items) {
    const time = new Date(item.updatedAt);
    if (Number.isNaN(time.getTime())) {
      groups.earlier.push(item);
    } else if (time >= startOfToday) {
      groups.today.push(item);
    } else if (time >= startOfYesterday) {
      groups.yesterday.push(item);
    } else {
      groups.earlier.push(item);
    }
  }
  return groups;
}

function sessionItemHtml(item, activeId) {
  const active = item.sessionId === activeId;
  return `
    <div class="session-item group flex items-center gap-1 rounded-lg pl-2 pr-1 py-1.5 cursor-pointer transition ${
      active ? "nav-item-active" : "hover:bg-slate-50"
    }" data-session-id="${esc(item.sessionId)}" title="${esc(item.title)}">
      <iconify-icon class="text-sm shrink-0 ${active ? "text-blue-600" : "text-slate-400"}" icon="mdi:message-text-outline"></iconify-icon>
      <span class="flex-1 min-w-0 text-xs truncate ${active ? "font-medium" : "text-slate-600"}">${esc(item.title)}</span>
      <button class="session-del opacity-0 group-hover:opacity-100 shrink-0 text-slate-400 hover:text-rose-600 p-0.5 rounded" data-delete-session="${esc(item.sessionId)}" title="删除会话">
        <iconify-icon class="text-xs" icon="mdi:delete-outline"></iconify-icon>
      </button>
    </div>`;
}

async function renderSessionList() {
  const keyword = $("session-search").value.trim();
  try {
    const data = await sessionApi.list(keyword ? { q: keyword } : {});
    const groups = groupByDay(data.items);
    const sections = [
      ["今天", groups.today],
      ["昨天", groups.yesterday],
      ["更早", groups.earlier],
    ].filter(([, items]) => items.length);

    if (!sections.length) {
      sessionListRoot.innerHTML = '<div class="text-[11px] text-slate-400 px-2 py-3">还没有历史对话</div>';
      return;
    }

    sessionListRoot.innerHTML = sections
      .map(
        ([label, items]) => `
        <div>
          <div class="text-[10px] font-medium text-slate-400 px-2 py-1 tracking-wide">${label}</div>
          <div class="space-y-0.5">${items.map((item) => sessionItemHtml(item, session.sessionId)).join("")}</div>
        </div>`
      )
      .join("");

    sessionListRoot.querySelectorAll("[data-session-id]").forEach((node) => {
      node.addEventListener("click", (event) => {
        if (event.target.closest("[data-delete-session]")) return;
        restoreSession(node.dataset.sessionId);
      });
    });
    sessionListRoot.querySelectorAll("[data-delete-session]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteSession(button.dataset.deleteSession);
      });
    });
  } catch (err) {
    sessionListRoot.innerHTML = `<div class="text-[11px] text-rose-500 px-2 py-3">加载失败：${esc(err.message)}</div>`;
  }
}

async function deleteSession(sessionId) {
  if (!confirm("确定删除这个会话吗？")) return;
  try {
    await sessionApi.remove(sessionId);
    if (session.sessionId === sessionId) startNewSession();
    await renderSessionList();
    toast("会话已删除");
  } catch (err) {
    toast("删除失败：" + err.message);
  }
}

// 还原会话：把消息逐条回放，再恢复状态机位置、报表预览和选择锁
// 返回 { ok, missing }：
//   ok=true            恢复成功
//   ok=false,missing  会话已经不存在（404），自动恢复的落点可以清掉
//   ok=false,其他      忙 / 网络故障 / 被更新的操作作废，落点要留着下次再试
async function restoreSession(sessionId, { silent = false, token = null } = {}) {
  if (isBusy()) {
    if (!silent) toast("请先停止正在生成的请求");
    return { ok: false, missing: false };
  }
  const myToken = token ?? ++sessionRestoreToken;
  let detail;
  try {
    detail = await sessionApi.detail(sessionId);
  } catch (err) {
    const missing = /404/.test(err.message || "");
    if (!silent) toast(missing ? "这个会话已经不存在了。" : "打开会话失败：" + err.message);
    return { ok: false, missing };
  }
  // 恢复途中用户点了「新建对话」：这次恢复作废，不要把旧会话画到新会话界面上。
  if (myToken !== sessionRestoreToken) return { ok: false, missing: false };

  clearChoiceLock();
  lastPendingPlan = null;
  businessMenuReplayed = false;
  destroyCharts();
  chatScroll.innerHTML = "";
  previewRoot.innerHTML = "";

  session.sessionId = detail.sessionId;
  session.conversationId = "conv_" + Date.now() + "_" + requestEpoch;
  session.state = detail.state;
  session.designSessionId = detail.designSessionId;
  session.reportDefinitionId = detail.reportDefinitionId;
  session.workspaceId = detail.workspaceId;
  session.pendingResume = null;
  session.editingTargetRegionId = detail.snapshot?.editingTargetRegionId ?? null;
  session.definition = detail.snapshot?.definition ?? null;
  session.policy = detail.snapshot?.policy ?? null;
  session.lastReportRun = detail.snapshot?.lastReportRun ?? null;
  session.previewPayload = detail.snapshot?.previewPayload ?? null;
  session.baseDefinitionRevision = session.definition?.metadata?.revision ?? null;
  writeLastSessionId(detail.sessionId);

  // 恢复文字方案（#19）：方案与阶段存在服务端快照里，
  // 不依赖聊天正文、debug 或浏览器本地变量。
  restorePlanFromDetail(detail);

  // 选择锁以后端为准：回放时用它替换历史问题对象，
  // 这样回放出来的下拉框与 lockedChoice 是同一个引用，选完能正常继续
  const pending = detail.pendingChoice?.question || null;

  // 回放消息：assistant 消息带 payload，能重建结构化卡片
  // 顺带记下回放过程：哪些是业务菜单、哪些是只读问答，
  // 用来判断刷新后原来那份菜单是不是还开着（别把已经答过的菜单恢复出来）。
  const replayEvents = [];
  for (const message of detail.messages) {
    // __advance__ 是前端自动推进用的内部控制消息，不是用户说的话。
    // 后端一般不会入库（万一入库了也别在刷新后显示成一条用户气泡）。
    if (message.role === "user" && String(message.content || "").trim() === "__advance__") continue;
    if (message.role === "user") {
      appendUser(message.content);
      replayEvents.push({ type: "user", content: message.content });
    } else if (message.payload) {
      const menu = replayAssistantMessage(message.payload, pending);
      replayEvents.push({ type: "assistant", response: message.payload, menu: menu || null });
    } else {
      appendAiText(message.content);
    }
  }

  // 恢复右侧报表预览
  if (session.previewPayload) {
    try {
      await renderFinalReport();
    } catch (err) {
      console.error("[AI Report] 恢复预览失败", err);
    }
  } else if (session.definition) {
    renderPreview(session.definition);
  } else {
    previewRoot.appendChild(previewEmpty);
  }

  // 恢复状态徽标
  setState(detail.state);
  updateMeta();

  // 恢复选择锁：后端 pending_choice 是权威来源；
  // 后端没存（例如只有 changeSet 的变更方案）就用回放过程判断出来的那份菜单兜底，
  // 但只认**还没被答掉**的那份：问一句只读问题不会关掉菜单，
  // 答完菜单或后续业务动作则视为菜单已失效，恢复时不能把过时确认项摆回来。
  if (pending) setChoiceLock(pending);
  else {
    const openMenu = WorkbenchQuery.resolveOpenMenuAfterReplay(replayEvents);
    if (openMenu) setChoiceLock(openMenu);
  }

  await renderSessionList();
  scrollChatBottom(true);
  return { ok: true, missing: false };
}

// 刷新后自动接回上次那条会话。
// 恢复不了就明确告诉用户「没接回来，但历史还在」，而不是让人对着一片空白猜。
// 只有会话确实被删了（404）才丢掉落点；后端临时连不上要留着，下次刷新还能接回来。
async function autoRestoreSession() {
  const savedId = readLastSessionId();
  if (!savedId) return false;
  const token = ++sessionRestoreToken;
  const result = await restoreSession(savedId, { silent: true, token });
  if (result.ok) return true;
  if (result.missing) {
    writeLastSessionId(null);
    toast("上次的对话已经被删除，请从左侧历史里重新选择。");
  } else {
    toast("上次的对话没能自动恢复（后端可能没连上），已保留在左侧历史里，请手动选择。");
  }
  return false;
}

// 回放一条历史 assistant 消息：复用 sendMessage 的渲染分支，但不触发自动推进
// pending 是后端给出的当前选择锁；用它替换历史问题对象，
// 这样回放出来的下拉框与 lockedChoice 是同一个对象，选项仍然可用
// 返回值：这条消息画出来的业务菜单（没有菜单时返回 null）。
function replayAssistantMessage(data, pending = null) {
  const bindPending = (questions) => {
    if (!pending || !questions?.length) return questions;
    return questions.map((q) => (q.id === pending.id ? pending : q));
  };

  // 只读问答的历史回放：照常展示回答，但不重画业务菜单、不改状态、不推进。
  const readOnlyKind = WorkbenchQuery.readOnlyAnswerKind(data);
  if (readOnlyKind) {
    renderReadOnlyAnswer(data, readOnlyKind);
    // 查询轮带回的原菜单：还没画过业务菜单时才补画一张。
    if (!businessMenuReplayed) {
      const preservedMenu = resolveQueryPreservedMenu(data);
      if (preservedMenu) return appendConfirmActions(preservedMenu, { lock: false });
    }
    return null;
  }

  switch (data.state) {
    case "ANALYZING":
    case "NEEDS_CLARIFICATION":
      appendAiText(data.reply || "");
      if (data.questions?.length) appendAiQuestions(bindPending(data.questions), { lock: false });
      return null;
    case "BLOCKED_OR_UNSUPPORTED":
      if (data.blocks?.length) appendAiBlocks(data.blocks);
      else appendAiText(data.reply || "");
      return null;
    case "WAITING_USER_CONFIRM": {
      const questions = bindPending(data.questions || []);
      appendAiText(data.reply || "");
      return appendConfirmActions(questions.find((q) => q.id === "template_edit_confirm"), { lock: false });
    }
    case "READY": {
      const questions = bindPending(data.questions || []);
      appendAiText(data.reply || "");
      return appendReadyActions(questions.find((q) => q.id === "ready_action"), { lock: false });
    }
    case "USER_EDITING_TEMPLATE": {
      appendAiText(data.reply || "");
      // 历史里带过待确认计划：回放时把计划卡和它的下拉框画出来。
      // 这里不抢输入锁，真正的锁由后端 pending_choice 恢复（见 restoreSession）。
      const plan = readPendingEditPlan(data);
      if (plan) {
        const planQuestion = resolvePlanQuestion(data) || (hasContractChoices(pending) ? pending : null);
        const menu = appendPendingEditPlan(plan, { lock: false, question: planQuestion });
        lastPendingPlan = { planId: plan.planId, draftRevision: plan.draftRevision };
        return menu;
      }
      appendTemplateEditSummary(data.domainObjects?.changeSet, data.debug?.markdownPreview);
      // 草稿编辑态有一组必选的确认项（同意/修改/重新开始）。
      // 回放时必须把下拉框画出来，否则恢复后输入框被锁住却没有可点的选项。
      const questions = bindPending(data.questions || []);
      if (data.uiActions?.includes("confirm_template_edit")) {
        return appendConfirmActions(questions.find((q) => q.id === "template_edit_confirm"), { lock: false });
      } else if (questions.length) {
        appendAiQuestions(questions, { lock: false });
      }
      return null;
    }
    case "DEFINITION_DRAFT_READY": {
      // 历史回放：把变更卡和确认下拉都画出来。
      // 少了下拉框，恢复后的会话就是一个「看得见、点不动」的死路。
      const questions = bindPending(data.questions || []);
      appendAiText(data.reply || "");
      if (data.domainObjects?.changeSet) appendAiChangeSet(data.domainObjects.changeSet, { interactive: false });
      const confirmQuestion =
        resolveDefinitionDraftQuestion({ questions }) ||
        (data.domainObjects?.changeSet ? FALLBACK_DRAFT_CONFIRM_QUESTION : null);
      if (confirmQuestion) return appendConfirmActions(confirmQuestion, { lock: false });
      return null;
    }
    default:
      appendAiText(data.reply || "");
      return null;
  }
}

// 新建对话：清空现场，向后端要一个新的会话 id
function startNewSession() {
  // 「新建对话」时如果还有恢复任务在跑，让它作废：不要把旧会话画到新会话界面上。
  sessionRestoreToken++;
  // 用「摘下」而不是「掐断」：在途请求继续跑完并落库，
  // 用户之前说的话才会留在历史里（掐断会让后端不落库，那一轮就凭空消失了）。
  // 摘下来之后它的响应靠 requestEpoch 判定，不会再画到这个新会话的界面上。
  if (activeRequest || advanceTimers.size) detachActiveRequest();
  clearChoiceLock();
  lastPendingPlan = null;
  destroyCharts();
  session.sessionId = null;
  session.conversationId = "conv_" + Date.now() + "_" + requestEpoch;
  session.state = "ANALYZING";
  session.designSessionId = null;
  session.reportDefinitionId = null;
  session.baseDefinitionRevision = null;
  session.definition = null;
  session.policy = null;
  session.editingTargetRegionId = null;
  session.lastReportRun = null;
  session.previewPayload = null;
  session.pendingResume = null;
  session.workspaceId = null;
  // 新对话还没有自己的 sessionId（要等第一条消息才向后端要），
  // 先把自动恢复的落点清掉，免得刷新后又跳回上一条对话。
  writeLastSessionId(null);
  chatScroll.innerHTML = "";
  previewRoot.innerHTML = "";
  previewRoot.appendChild(previewEmpty);
  // 新对话没有方案：清掉上一条对话的方案卡片。
  planState.designPhase = null;
  planState.reportPlan = null;
  planState.busy = false;
  planState.error = null;
  planState.planExpanded = false;
  renderPlanModule();
  setState("ANALYZING");
  updateMeta();
  renderSessionList();
}

// -------- 事件绑定 --------
document.querySelector("[data-quick]").addEventListener("click", (event) => {
  sendMessage(event.currentTarget.dataset.quick);
});

$("btn-send").addEventListener("click", () => {
  // 待确认菜单存在时也允许发送：只读提问要走这条路（工单 02）。
  // 执行操作的约束由后端把关，这里不再拦自由输入。
  if (activeRequest || advanceTimers.size) return;
  const text = $("chat-input").value.trim();
  if (!text) return;
  $("chat-input").value = "";
  $("chat-input").style.height = "auto";
  sendMessage(text);
});

$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("btn-send").click();
  }
});

$("chat-input").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 96) + "px";
});

$("btn-new-session").addEventListener("click", () => {
  // 生成中也允许新建：在途请求被「摘下」继续在后台跑完并落库，
  // 它的响应靠 requestEpoch 判定，不会污染这个新会话（详见 detachActiveRequest）。
  // 上一条对话随时可以从左侧历史里点回来。
  const wasBusy = isBusy();
  startNewSession();
  if (wasBusy) {
    toast("上一个请求还在后台生成，完成后可在左侧历史对话里查看。");
  }
  appendAiText("新对话已就绪。描述你的报表需求，我会生成结构化报表草稿。");
});

$("session-search").addEventListener("input", () => {
  renderSessionList();
});

$("btn-regenerate").addEventListener("click", () => {
  if (lockedChoice || activeRequest || advanceTimers.size) return;
  session.state = "ANALYZING";
  sendMessage("生成 A 机房昨日能耗日报，包含总耗电、设备排名和趋势分析");
});
$("btn-stop").addEventListener("click", cancelActiveRequest);

// 启动检查
(async () => {
  try {
    const res = await fetch(API_BASE + "/health");
    if (res.ok) {
      $("health-badge").classList.remove("hidden");
    }
  } catch (e) {
    toast("后端未连接，请先启动 uvicorn");
  }
  setState("ANALYZING");
  updateMeta();
  await renderSessionList();
  // 刷新后自动接回上次那条会话；接不回来也会明确提示，历史入口始终保留。
  await autoRestoreSession();
})();
