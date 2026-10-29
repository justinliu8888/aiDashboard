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
  previewPayload: null, // 试运行结果（/api/report/preview/{runId} 的产物）
  // 报表示意载荷：草稿阶段的模拟业务数据（聊天响应顶层 reportPreviewPayload，
  // 会话恢复时在 snapshot.reportPreviewPayload 临时带回）。
  // 与上面的 previewPayload 是两回事：一个是模拟示意，一个是试运行产物，
  // 语义不同、互不覆盖、互不替代（规格 #46「跨工单契约」）。
  // 它只活在内存里：不写 localStorage、不写任何持久化位置。
  reportPreviewPayload: null,
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

// --------------------------------------------------------------------------
// 文字方案的挂载位置：左侧对话区里的一条 AI 回复。
//
// 工单 06 / 规格 #46：文字方案出现在左侧对话区，作为一条 AI 回复里的可折叠块；
// 右侧报表面板不再出现文字方案（容器一并移除，不留任何入口）。
//
// 方案模块（workbench_plan.js）仍然只接收一个根节点、只往那个根节点里写，
// 所以这里换的只是「那个根节点在哪儿」：
//   - planBubble：承载方案的一条 AI 气泡（头像 + 卡片），与对话流里的气泡同构；
//   - planRoot：方案模块的挂载根（气泡里的 #plan-root）。
//
// 气泡是**动态建**的：对话区会被清空（新建对话 / 恢复会话），
// 静态写在页面里的容器会被一起清掉，所以按需重建。
// --------------------------------------------------------------------------

let planBubble = null;
let planRoot = null;

/** 建一条承载方案模块的 AI 气泡。 */
function createPlanBubble() {
  const bubble = document.createElement("div");
  bubble.className = "flex gap-2.5";
  bubble.setAttribute("data-plan-bubble", "");

  const avatar = document.createElement("div");
  avatar.className = "w-8 h-8 shrink-0 rounded-full bg-gradient-to-br from-blue-500 to-blue-700 flex items-center justify-center";
  avatar.innerHTML = '<iconify-icon class="text-white" icon="mdi:robot-outline"></iconify-icon>';

  const body = document.createElement("div");
  body.className = "flex-1 min-w-0";

  const root = document.createElement("div");
  root.id = "plan-root";

  body.appendChild(root);
  bubble.appendChild(avatar);
  bubble.appendChild(body);
  return { bubble, root };
}

/**
 * 拿到方案容器（必要时重建），并挂到对话流里。
 * 对话区被清空过 → 旧容器已经不在文档里了 → 重建一条。
 * @returns {{root: Element|null, fresh: boolean}} fresh=true 表示这次是新挂上去的
 */
function mountPlanHost() {
  if (!chatScroll) return { root: null, fresh: false };
  if (!planBubble || !planRoot || !planRoot.isConnected) {
    const host = createPlanBubble();
    planBubble = host.bubble;
    planRoot = host.root;
  }
  if (planBubble.parentNode !== chatScroll) {
    chatScroll.appendChild(planBubble);
    chatBubbleObserver.observe(planBubble);
    return { root: planRoot, fresh: true };
  }
  return { root: planRoot, fresh: false };
}

/**
 * 把方案气泡挪到对话流的末尾。
 *
 * 只在气泡**刚挂进对话流**时做：延到本轮渲染之后再挪，
 * 响应处理是同步的，等它跑完，卡片就落在这一轮 AI 回复文本的下面 ——
 * 读起来就是「AI 回复里的一块」。
 *
 * 已经在对话流里的卡片不再挪：它留在自己那条回复的位置上，
 * 后来的对话接在它下面，用户看着不跳。
 */
let planAnchorScheduled = false;
function anchorPlanBubble() {
  if (!planBubble || !chatScroll) return;
  if (planAnchorScheduled) return;
  planAnchorScheduled = true;
  const run = () => {
    planAnchorScheduled = false;
    if (!planBubble || !chatScroll || !planBubble.isConnected) return;
    chatScroll.appendChild(planBubble);
    scrollChatBottom();
  };
  if (typeof queueMicrotask === "function") queueMicrotask(run);
  else setTimeout(run, 0);
}

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
  const api = window.WorkbenchPlan;
  if (!api) return;
  // 草稿阶段也要保留方案卡片（折叠的已确认方案）—— 不再排除 DRAFT_READY。
  // 只有**真的有方案**才挂卡片：澄清追问（栏目推不出来时的那句提问）这类响应
  // 可能只带 designPhase、不带方案，挂上去会变成一张「尚未生成方案」的空卡片。
  const hasPlan = !!planState.reportPlan;
  // 草稿空态只在「有方案、但还没出草稿」时让位。
  // DRAFT_READY 时若草稿还没到，宁可显示空态，也不要留一块空白。
  const hasDraft = !!(session.definition && Array.isArray(session.definition.regions) && session.definition.regions.length);
  const hideEmpty = hasPlan && !hasDraft && planState.designPhase !== "DRAFT_READY";
  previewRoot.classList.toggle("plan-active", hideEmpty);
  if (!hasPlan) {
    // 没有方案：把气泡从对话流里摘掉，不留空白位置。
    if (planRoot) api.clear(planRoot);
    if (planBubble && planBubble.parentNode) planBubble.parentNode.removeChild(planBubble);
    return;
  }
  const { root, fresh } = mountPlanHost();
  if (!root) return;
  // 记住用户有没有展开折叠视图：重渲染（busy 抖动 / 新响应）时不把它弹回去。
  const existing = root.querySelector("details.confirmed-plan-details");
  if (existing) planState.planExpanded = existing.open === true;
  api.render(
    root,
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
  if (fresh) anchorPlanBubble();
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
  if (phase === "DRAFT_READY") previewRoot.parentElement.scrollTop = 0;
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

// --------------------------------------------------------------------------
// 草稿徽标
//
// 规格 #46 / ADR 0004 §2.4：
//   - 草稿阶段（右侧已经是带数的报表）→ 「示意」；
//   - 点「同意」并**校验成功后** → 「已校验 / 待接真数」；
//   - 校验失败或进行中 → **不得**提前显示成功徽标，原来的「示意」保持可见。
//
// 校验通过的判据是状态机：VALIDATING 通过后才进 PREVIEWING，
// 所以「过了 VALIDATING」才算校验成功。
// --------------------------------------------------------------------------
const VALIDATED_AGENT_STATES = new Set(["PREVIEWING", "PREVIEW_READY", "RENDERING", "READY"]);

function draftBadgeView() {
  const payload = session.reportPreviewPayload;
  const hasPayload = !!(payload && Array.isArray(payload.regions) && payload.regions.length);
  if (!hasPayload) {
    // 草稿在、示意还没到：说「草稿」，不说「示意」（屏幕上还没有数）。
    if (session.definition) return { key: "draft", text: "草稿", tone: "amber" };
    return { key: "none", text: "尚无草稿", tone: "slate" };
  }
  if (VALIDATED_AGENT_STATES.has(session.state)) {
    return { key: "validated", text: "已校验 / 待接真数", tone: "emerald" };
  }
  // 校验进行中 / 失败：示意保持可见，只是换个颜色提示当前状态。
  const tone = session.state === "VALIDATION_FAILED" ? "rose" : "amber";
  return { key: "simulated", text: "示意", tone };
}

const DRAFT_BADGE_TONES = {
  slate: "bg-slate-100 text-slate-500 border-slate-200",
  amber: "bg-amber-50 text-amber-700 border-amber-200",
  rose: "bg-rose-50 text-rose-700 border-rose-200",
  emerald: "bg-emerald-50 text-emerald-700 border-emerald-200",
};

function updateMeta() {
  $("meta-rev").textContent = session.baseDefinitionRevision ?? "—";
  $("meta-session").textContent = session.designSessionId ?? "—";

  const draftBadge = $("draft-badge");
  const view = draftBadgeView();
  draftBadge.className = "inline-flex items-center gap-1 rounded-md text-xs font-medium px-2 py-1 border " + (DRAFT_BADGE_TONES[view.tone] || DRAFT_BADGE_TONES.slate);
  draftBadge.dataset.draftBadge = view.key;
  draftBadge.innerHTML = '<iconify-icon class="text-sm" icon="mdi:robot-outline"></iconify-icon>' + esc(view.text);
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

  // 定义先落位（各状态共用）：示意载荷的版本校对拿它当基准。
  const draftDefinition = data.domainObjects?.reportDefinitionDraft;
  if (draftDefinition) {
    session.definition = draftDefinition;
    session.policy = data.domainObjects?.reportPolicyDraft || session.policy;
    session.reportDefinitionId = draftDefinition?.metadata?.id || session.reportDefinitionId;
    session.baseDefinitionRevision = draftDefinition?.metadata?.revision ?? session.baseDefinitionRevision;
  }

  // 报示意载荷：草稿确认的**同一轮**响应就带它（不做二次请求）。
  // 旧版本的载荷（定义已经改过）直接丢掉，不覆盖新模板。
  applyReportPreviewPayload(readReportPreviewPayload(data));

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
        session.reportPreviewPayload = null;
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
      session.editingTargetRegionId = null;
      appendAiText(data.reply || '已生成报表草稿，右侧可以直接编辑；满意后点"同意"。');
      renderReportPanel();
      if (pendingPlan) {
        // 后端把待确认计划放在这个状态时，按新契约走计划卡，不画旧的草稿确认菜单
        renderPendingPlan();
      } else {
        appendConfirmActions(data.questions?.find((question) => question.id === "template_edit_confirm"));
      }
      break;
    case "USER_EDITING_TEMPLATE":
      appendAiText(data.reply || "请继续说明要怎么编辑当前草稿。");
      if (!pendingPlan) {
        appendTemplateEditSummary(
          data.domainObjects?.changeSet,
          data.debug?.markdownPreview
        );
      }
      renderReportPanel(session.editingTargetRegionId ? [session.editingTargetRegionId] : []);
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
      // 试运行照跑（后端照旧推进状态机），但它的产物**不再单独渲染**：
      // 草稿阶段右侧已经是带数的报表，点「同意」之后屏幕上的数字不动。
      // 二次请求的代码留在主线上（fetchAndStorePreview），将来接真实数据直接复用。
      if (epoch !== requestEpoch) return;
      if (WorkbenchQuery.shouldAutoAdvance(data)) queueAdvance(500, epoch);
      break;
    case "RENDERING":
      appendAiText(data.reply || "正在渲染报表界面…");
      // 不再按预览状态重画一块面板：示意数字保持不动。
      // 面板上还没有载荷（例如中途刷新）时才补画一次。
      if (!session.reportPreviewPayload) {
        try {
          renderReportPanel();
        } catch (err) {
          console.error("[AI Report] 渲染报表失败", err);
          appendAiText("报表渲染遇到问题，已跳过部分内容，流程继续。");
        }
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
      appendAiText(data.reply || "已生成修改方案。");
      if (data.domainObjects?.changeSet) {
        appendAiChangeSet(data.domainObjects.changeSet);
      }
      renderReportPanel();
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

/**
 * 拉一次试运行结果。
 *
 * 规格 #46 取消了独立的报表预览面板：草稿阶段右侧已经是带数的报表，
 * 示意数据随草稿确认的同一轮响应就回来（reportPreviewPayload），**不再二次请求**，
 * 所以这条链路当前不会被调用。
 *
 * 代码与它的产物都保留在主线上（ADR 0004 §2.4）：将来接真实数据时，
 * 试运行结果直接复用这条路径。
 *
 * 注意它写进的是 `session.previewPayload`（**试运行结果**），
 * 与 `session.reportPreviewPayload`（草稿阶段的模拟示意）是两个键：
 * 语义不同、互不覆盖、互不替代。
 */
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
  renderReportPanel(cs.changed.map((e) => e.regionId));
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

// --------------------------------------------------------------------------
// 报示意载荷（reportPreviewPayload）
//
// 规格 #46 / ADR 0004：草稿阶段不再画空壳，右侧直接用**模拟业务数据**渲染。
//
// 载荷从两处来，结构同构：
//   - 聊天响应顶层 `reportPreviewPayload`：草稿确认的**同一轮**就带它，不做二次请求；
//   - 会话读取响应 `snapshot.reportPreviewPayload`：后端按同一份定义与上下文
//     重新生成的同构载荷（响应级字段，没有落库；前端也不缓存它）。
//
// 与既有键 `session.previewPayload`（**试运行结果**）是两回事：
// 一个是草稿阶段的模拟示意，一个是试运行的产物 —— 语义不同、互不覆盖、互不替代。
// --------------------------------------------------------------------------

/**
 * 从一次响应里取出报示意载荷。
 * 载荷必须是对象且带 regions 数组，否则当作没有载荷（不猜、不补、不造）。
 */
function readReportPreviewPayload(data) {
  const payload = data?.reportPreviewPayload;
  if (!payload || typeof payload !== "object") return null;
  if (!Array.isArray(payload.regions)) return null;
  return payload;
}

/** 版本号归一：只认「整数形态」的版本号，形态对不上就不比较（宁可放过，不误杀）。 */
function normalizeVersionNumber(value) {
  const text = String(value ?? "").trim().replace(/^v/i, "");
  return /^\d+$/.test(text) ? Number(text) : null;
}

/**
 * 载荷是不是过期版本。
 *
 * 契约：previewContext 带 definitionId / definitionVersion。
 * 旧版本的异步响应不能覆盖新模板 —— 对不上就丢掉这份载荷。
 */
function isStaleReportPayload(payload) {
  const ctx = payload?.previewContext || {};
  const definition = session.definition;
  const definitionId = definition?.metadata?.id;
  if (ctx.definitionId && definitionId && String(ctx.definitionId) !== String(definitionId)) {
    return true;
  }
  const definitionVersion = definition?.metadata?.revision;
  if (ctx.definitionVersion != null && definitionVersion != null) {
    const left = normalizeVersionNumber(ctx.definitionVersion);
    const right = normalizeVersionNumber(definitionVersion);
    if (left != null && right != null && left !== right) return true;
  }
  return false;
}

/** 收下一份示意载荷（过期就丢掉）。返回是否收下了。 */
function applyReportPreviewPayload(payload) {
  if (!payload) return false;
  if (isStaleReportPayload(payload)) {
    console.warn("[AI Report] 丢弃过期版本的示意载荷（定义已经改过）");
    return false;
  }
  session.reportPreviewPayload = payload;
  return true;
}

/** 定义里的区域（按 id 找）：载荷只带 regionId，编辑入口与 topN 都要回定义里取。 */
function findDefinitionRegion(definition, regionId) {
  if (!definition || !regionId) return null;
  return (definition.regions || []).find((item) => item?.id === regionId) || null;
}

/**
 * 单位一律从载荷里读，绝不硬编码 kWh / 耗电。
 *
 * 契约：数值型组件的载荷带 `unit`（指标卡、图表）；
 * 多指标时逐条序列也带 `unit`；表格列的单位在 `columns[].format.unitLabel`。
 * 都没有才退回定义里声明的单位（templateBinding.previewSpec.unit）——
 * 那也是声明值，不是猜的。再没有就不写单位，不拿别的单位顶替。
 */
function readPayloadUnit(region, payload, defRegion) {
  const p = payload || {};
  const candidates = [
    p.unit,
    Array.isArray(p.series) ? p.series[0]?.unit : null,
    Array.isArray(p.metrics) ? p.metrics[0]?.unit : null,
    region?.unit,
    defRegion?.templateBinding?.previewSpec?.unit,
  ];
  for (const candidate of candidates) {
    const text = String(candidate ?? "").trim();
    if (text) return text;
  }
  return "";
}

/**
 * 「能力待接通」标注要说明的具体项。
 *
 * 契约：区域带 capabilityPending（布尔）；多指标区域在 metrics[] 里逐项带
 * capabilityPending / pendingReason（载荷与定义的 previewSpec 都可能带）。
 * 这里只如实说明**哪个指标还没接通、为什么**，不承诺计算口径或公式。
 */
function readCapabilityNotes(region, payload, defRegion) {
  const notes = [];
  const seen = new Set();
  const collect = (metrics) => {
    if (!Array.isArray(metrics)) return;
    for (const metric of metrics) {
      if (!metric || typeof metric !== "object") continue;
      if (metric.capabilityPending !== true) continue;
      const label = String(metric.label || metric.metricKey || "指标").trim();
      if (!label || seen.has(label)) continue;
      seen.add(label);
      const reason = String(metric.pendingReason || "").trim();
      notes.push(reason ? `${label}（${reason}）` : label);
    }
  };
  collect(payload?.metrics);
  collect(defRegion?.templateBinding?.previewSpec?.metrics);
  if (!notes.length && region?.capabilityPending === true) notes.push("该指标尚未接通");
  return notes;
}

/** 载荷里的总量卡片（按单位索引）：表格勾稽要用它。 */
function buildTotalsByUnit(regions) {
  const totals = new Map();
  for (const region of regions) {
    if (region?.componentType !== "METRIC_CARD") continue;
    const p = region.payload || {};
    const unit = String(p.unit || "").trim();
    const value = Number(p.value);
    if (!unit || !Number.isFinite(value)) continue;
    if (!totals.has(unit)) totals.set(unit, value);
  }
  return totals;
}

function readPositiveInt(value) {
  const num = Number(value);
  return Number.isInteger(num) && num > 0 ? num : null;
}

/** 表格某一列的行和（只算数值列；非数值列返回 null）。 */
function sumColumnValues(rows, column) {
  let sum = 0;
  let seen = false;
  for (const row of rows) {
    const value = Number(row?.[column.fieldRef]);
    if (!Number.isFinite(value)) continue;
    sum += value;
    seen = true;
  }
  return seen ? sum : null;
}

/**
 * 表格行是不是被截断了。
 *
 * 硬约束是「表格各行之和 = 报表总量」（开发规格的勾稽）：
 * 行被截断时行和会小于总量，渲染层必须说清楚，不能让两者悄悄不等。
 *
 * 判定分两层：
 *   1. 载荷自己说了截断（truncated / totalRows）：直接采信；
 *   2. 载荷没说：行数**达到了这张表声明的 topN**，且行和明显小于同口径的总量卡片。
 *      两件事都成立才敢这么说 —— 只满足一条时不能断言「还有别的行没显示」
 *      （行数不足可能只是对象本来就少，行和小可能只是这张表管的是另一段范围）。
 */
function readTableTruncation(payload, defRegion, totalsByUnit) {
  const p = payload || {};
  const rows = Array.isArray(p.rows) ? p.rows : [];
  const topN = readPositiveInt(defRegion?.binding?.topN);
  const declaredTotal = [p.totalRows, p.totalRowCount, p.objectCount]
    .map((value) => Number(value))
    .find((value) => Number.isFinite(value));

  if (p.truncated === true) {
    return { truncated: true, shown: rows.length, limit: topN ?? declaredTotal ?? rows.length };
  }
  if (declaredTotal != null && declaredTotal > rows.length) {
    return { truncated: true, shown: rows.length, limit: topN ?? rows.length, total: declaredTotal };
  }
  if (topN == null || rows.length < topN || !totalsByUnit) return null;

  for (const column of p.columns || []) {
    const unit = String(column?.format?.unitLabel || "").trim();
    if (!unit) continue;
    const total = totalsByUnit.get(unit);
    if (total == null) continue;
    const sum = sumColumnValues(rows, column);
    if (sum == null) continue;
    const tolerance = Math.max(0.1, Math.abs(total) * 0.01);
    if (sum < total - tolerance) {
      return { truncated: true, shown: rows.length, limit: rows.length };
    }
  }
  return null;
}

/** 数字按载荷给的精度显示；不是数字就原样显示。 */
function formatMetricValue(value, precision) {
  if (value == null || value === "") return "—";
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  const digits = Number.isFinite(Number(precision)) ? Number(precision) : 1;
  return num.toFixed(Math.max(0, Math.min(6, digits)));
}

// --------------------------------------------------------------------------
// 渲染：右侧报表面板（草稿阶段与预览阶段共用一套）
//
// 入口只有一个：renderReportPanel()。
// 数据来源只有一个：session.reportPreviewPayload（模拟业务数据的区域载荷）。
// 组件类型、板块标题、表格栏目一律取自载荷与已确认方案 —— 前端不改写、不按标题猜。
// --------------------------------------------------------------------------

function renderReportPanel(highlightRegionIds = []) {
  // 先销毁旧图表实例：直接 innerHTML="" 只会移除 DOM，
  // ECharts 实例仍然持有对已脱离文档节点的引用，内存泄漏 + 后续 resize 报错。
  destroyCharts();
  previewRoot.innerHTML = "";

  const payload = session.reportPreviewPayload;
  const regions = Array.isArray(payload?.regions) ? payload.regions : [];
  // 标题区（role == REPORT_TITLE）不出数，也不占一块。
  const bodyRegions = regions.filter((region) => !isTitleRegion(region));
  const definition = session.definition;

  if (!bodyRegions.length) {
    // 没有载荷就没有数可画：回到空态，不画空壳卡片。
    setPreviewEmptyHint(definition ? "draft" : "none");
    previewRoot.appendChild(previewEmpty);
    return;
  }

  const totalsByUnit = buildTotalsByUnit(bodyRegions);
  previewRoot.appendChild(renderPanelHeader(definition, payload, bodyRegions.length));

  for (const region of bodyRegions) {
    const card = renderRegionCard(region, definition, payload, totalsByUnit);
    if (highlightRegionIds.includes(region.regionId)) card.classList.add("region-highlight");
    previewRoot.appendChild(card);
  }
  syncPreviewEditability();
  requestAnimationFrame(() => _chartInstances.forEach((chart) => {
    try { chart.resize(); } catch (e) {}
  }));
}

/** 空态提示：没有草稿与「草稿已在、示意还没到」是两种情况，说清楚。 */
function setPreviewEmptyHint(kind) {
  const hint = previewEmpty.querySelector("[data-preview-empty-hint]");
  if (!hint) return;
  hint.textContent = kind === "draft"
    ? "草稿已保存，示意数据还没生成"
    : "还没有草稿，试试左侧「生成能耗日报」";
}

/** 载荷里的窗口标签：previewContext 只给起止时间，标签自己拼。 */
function readPayloadWindowLabel(payload) {
  const ctx = payload?.previewContext || {};
  if (ctx.windowLabel) return String(ctx.windowLabel);
  const start = ctx.windowStart ? String(ctx.windowStart).slice(0, 10) : "";
  const end = ctx.windowEnd ? String(ctx.windowEnd).slice(0, 10) : "";
  if (start && end) return start === end ? start : `${start} ~ ${end}`;
  return start || end || "";
}

/** 面板顶部：报表名 + 一行「这是模拟业务数据」的说明。 */
function renderPanelHeader(definition, payload, regionCount) {
  const header = document.createElement("div");
  header.className = "bg-white rounded-xl border border-slate-200 p-5";
  const name = definition?.metadata?.name || "报表草稿";
  const windowLabel = readPayloadWindowLabel(payload);
  header.innerHTML = `
    <div class="flex items-start justify-between gap-4">
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2 flex-wrap">
          <h2 class="report-name editable-field text-lg font-semibold text-slate-900">${esc(name)}</h2>
          <span class="inline-flex items-center rounded-md text-xs font-medium px-2 py-1 border bg-blue-50 text-blue-700 border-blue-200">${esc(definition?.schemaVersion || "1.0")}</span>
          <span class="inline-flex items-center gap-1 rounded-md text-xs font-medium px-2 py-1 border bg-amber-50 text-amber-700 border-amber-200 draft-status-badge">
            <iconify-icon class="text-sm" icon="mdi:robot-outline"></iconify-icon>${esc(definition?.metadata?.status || "DRAFT")}
          </span>
          <span class="edit-hint text-[11px] text-slate-400 inline-flex items-center gap-1"><iconify-icon icon="mdi:pencil-outline"></iconify-icon>点击名称可直接编辑</span>
        </div>
        <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500 draft-meta-row">
          <span>revision: <span class="mono">${definition?.metadata?.revision ?? "—"}</span></span>
          <span>区域数: <span class="mono">${regionCount}</span></span>
          ${windowLabel ? `<span>时间窗: <span class="mono">${esc(windowLabel)}</span></span>` : ""}
        </div>
        ${renderGenerationInputs(definition)}
      </div>
    </div>
    <div class="mt-3 flex items-start gap-1.5 text-[11px] leading-relaxed text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1.5" data-simulated-note>
      <iconify-icon class="text-sm mt-px" icon="mdi:flask-outline"></iconify-icon>
      <span>当前显示的是<b>模拟业务数据</b>，用于核对版式；接入真实数据后会自动替换。</span>
    </div>
  `;

  const nameEl = header.querySelector(".report-name");
  nameEl.addEventListener("blur", () => {
    if (!isEditingEnabled()) {
      nameEl.textContent = name;
      return;
    }
    const newName = nameEl.textContent.trim();
    if (newName && newName !== name) {
      nameEl.textContent = name;
      sendTemplateEditAction(`报表名称改成${newName}`);
    } else {
      nameEl.textContent = name;
    }
  });
  nameEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      nameEl.blur();
    }
  });
  return header;
}

/**
 * 一个板块的卡片：外壳（标题、角标、编辑入口）+ 正文（按载荷渲染）。
 *
 * 外壳里的编辑入口只对**定义里存在的板块**开放 —— 载荷没有对应定义时
 * 没有可改的对象，就不摆入口。
 */
function renderRegionCard(region, definition, payload, totalsByUnit) {
  const defRegion = findDefinitionRegion(definition, region.regionId);
  const p = region.payload || {};
  const unit = readPayloadUnit(region, p, defRegion);
  const capabilityNotes = readCapabilityNotes(region, p, defRegion);

  const card = document.createElement("div");
  card.className = "bg-white rounded-xl border border-slate-200 p-5 group";
  card.dataset.regionId = region.regionId || "";

  const title = region.name || defRegion?.name || region.componentType || "板块";
  const chartKind = p.chartKind || defRegion?.component?.chartKind || "";
  const header = `
    <div class="flex items-start justify-between gap-4">
      <div class="flex-1 min-w-0">
        <h3 class="region-name editable-field font-semibold text-slate-900">${esc(title)}</h3>
        <p class="text-xs text-slate-500 mt-0.5">role: <span class="mono">${esc(region.role || defRegion?.role || "")}</span> · component: <span class="mono">${esc(region.componentType || defRegion?.component?.type || "")}</span>${chartKind ? ' / <span class="mono">' + esc(chartKind) + "</span>" : ""}</p>
      </div>
      <div class="flex items-center gap-1 shrink-0">
        <span class="region-badge inline-flex items-center gap-1 rounded-md text-[11px] font-medium px-2 py-0.5 border bg-slate-100 text-slate-600 border-slate-200" data-region-badge="simulated" title="模拟业务数据，接入真实数据后自动替换">
          <iconify-icon class="text-xs" icon="mdi:flask-outline"></iconify-icon>示意
        </span>
        ${defRegion ? `
        <span class="region-tools">
          <button class="btn-display-options h-7 rounded border border-slate-200 hover:bg-blue-50 hover:border-blue-200 hover:text-blue-700 text-[11px] text-slate-500 px-2 transition" title="查看适合当前数据的展示方式">展示方式</button>
          <button class="btn-grain-options h-7 rounded border border-slate-200 hover:bg-blue-50 hover:border-blue-200 hover:text-blue-700 text-[11px] text-slate-500 px-2 transition" title="查看当前数据源支持的粒度">粒度</button>
          <button class="btn-del-region w-7 h-7 rounded border border-slate-200 hover:bg-rose-50 hover:border-rose-200 hover:text-rose-600 text-slate-400 flex items-center justify-center transition" title="删除此区域">
            <iconify-icon class="text-base" icon="mdi:trash-can-outline"></iconify-icon>
          </button>
        </span>
        ` : ""}
        <span class="mono text-[10px] text-slate-500 bg-slate-100 border border-slate-200 rounded px-1.5 py-0.5 shrink-0">${esc(region.regionId || "")}</span>
      </div>
    </div>`;

  card.innerHTML = header + '<div class="mt-3 region-body"></div>';

  // 正文：按载荷渲染（草稿阶段与预览阶段共用这一套）。
  const body = card.querySelector(".region-body");
  try {
    renderRegionPayloadBody(body, region, payload, { unit, defRegion, totalsByUnit });
  } catch (err) {
    console.error("[AI Report] 板块渲染失败", region?.regionId, err);
    body.innerHTML = `
      <div class="rounded-lg bg-rose-50 border border-rose-200 p-3 text-xs text-rose-700">
        这个板块渲染失败：${esc(err?.message || String(err))}。其余板块和后续流程不受影响。
      </div>`;
  }

  // 「能力待接通」：角标之外额外写明哪些指标还没接通、为什么。
  // 不承诺计算口径或公式 —— 那不是前端能说的事。
  if (capabilityNotes.length) {
    const note = document.createElement("div");
    note.className = "region-capability-note text-[11px] leading-relaxed text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1 mt-2";
    note.dataset.regionCapability = "pending";
    note.textContent = "能力待接通：" + capabilityNotes.join("、");
    card.appendChild(note);
  }

  if (!defRegion) return card;

  // 区域名编辑
  const nameEl = card.querySelector(".region-name");
  const originalName = title;
  nameEl.addEventListener("blur", () => {
    if (!isEditingEnabled()) {
      nameEl.textContent = originalName;
      return;
    }
    const newName = nameEl.textContent.trim();
    if (newName && newName !== originalName) {
      nameEl.textContent = originalName;
      sendTemplateEditAction(`标题改成${newName}`, defRegion.id);
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
    sendTemplateEditAction("这个 section 可以怎么展示？", defRegion.id);
  });
  card.querySelector(".btn-grain-options")?.addEventListener("click", () => {
    sendTemplateEditAction("这个数据能按哪些粒度编译？", defRegion.id);
  });

  // 区域删除
  card.querySelector(".btn-del-region")?.addEventListener("click", () => {
    if (!confirm(`确定删除「${originalName}」吗？`)) return;
    sendTemplateEditAction("删除这个 section", defRegion.id);
  });

  return card;
}

/**
 * 板块正文：**按载荷渲染**。
 *
 * 这是草稿阶段与预览阶段共用的唯一一套渲染逻辑（规格 #46）：
 * 数据一律从 `region.payload` 读，前端不造数、不按标题猜口径、不改写组件类型。
 */
function renderRegionPayloadBody(container, region, payload, ctx = {}) {
  const type = region.componentType;
  const p = region.payload || {};
  const unit = ctx.unit ?? readPayloadUnit(region, p, ctx.defRegion);

  if (type === "TEXT") {
    container.innerHTML = `<div class="text-xl font-semibold text-slate-800">${esc(p.rendered || p.template || "")}</div>`;
    return;
  }

  if (type === "METRIC_CARD") {
    const metrics = Array.isArray(p.metrics) ? p.metrics.filter((m) => m && typeof m === "object") : [];
    // 主量已经在大卡片上显示了，其余指标单独列出来（多指标区域不能只剩一个数）。
    const others = metrics.filter((m) => !(String(m.label || "") === String(p.label || "") && Number(m.value) === Number(p.value)));
    const yoy = Number(p.yoyPercent);
    const showYoy = p.comparisonAvailable !== false && p.yoyPercent != null && Number.isFinite(yoy);
    container.innerHTML = `
      <div class="rounded-lg border border-slate-200 bg-slate-50 px-5 py-4 inline-block">
        <div class="text-[11px] text-slate-500">${esc(p.label || "指标")}</div>
        <div class="text-3xl font-semibold text-slate-900 mt-1">${esc(formatMetricValue(p.value, p.precision))}<span class="text-sm font-normal text-slate-400 ml-1">${esc(unit)}</span></div>
        ${showYoy ? `<div class="text-[11px] mt-1 ${yoy >= 0 ? "text-rose-600" : "text-emerald-600"}">${yoy >= 0 ? "↑" : "↓"} ${Math.abs(yoy)}% 较上一周期</div>` : ""}
        ${p.windowLabel ? `<div class="text-[11px] text-slate-400 mt-1">${esc(p.windowLabel)}</div>` : ""}
      </div>
      ${others.length ? `
      <div class="mt-3 grid gap-2 sm:grid-cols-2">
        ${others.map((m) => `
          <div class="rounded-lg border border-slate-200 bg-white px-3 py-2" data-region-metric="${esc(m.metricKey || m.label || "")}">
            <div class="text-[11px] text-slate-500">${esc(m.label || m.metricKey || "指标")}</div>
            <div class="text-base font-semibold text-slate-900 mt-0.5">${esc(formatMetricValue(m.value, m.precision))}<span class="text-[11px] font-normal text-slate-400 ml-1">${esc(m.unit || "")}</span></div>
          </div>`).join("")}
      </div>` : ""}`;
    return;
  }

  if (type === "TABLE") {
    const columns = Array.isArray(p.columns) ? p.columns : [];
    const rows = Array.isArray(p.rows) ? p.rows : [];
    const headCells = columns
      .map(
        (c) =>
          `<th class="text-left font-medium px-3 py-2 text-slate-500">${esc(c.label)}${c.format?.unitLabel ? ` <span class="text-slate-400 font-normal">(${esc(c.format.unitLabel)})</span>` : ""}</th>`
      )
      .join("");
    const bodyRows = rows
      .map((row, idx) => {
        const cells = columns
          .map((c) => {
            const val = row?.[c.fieldRef];
            if (c.format?.kind === "NUMBER") {
              const num = Number(val);
              const text = Number.isFinite(num) ? num.toFixed(c.format.precision ?? 1) : val == null ? "" : String(val);
              return `<td class="px-3 py-2.5 text-slate-700 mono">${esc(text)}</td>`;
            }
            return `<td class="px-3 py-2.5 text-slate-700">${esc(val ?? "")}</td>`;
          })
          .join("");
        return `<tr class="hover:bg-slate-50">${p.rowNumber ? `<td class="px-3 py-2.5 text-slate-400 mono">${idx + 1}</td>` : ""}${cells}</tr>`;
      })
      .join("");

    // 行被截断时必须说清楚：不然「各行之和 ≠ 报表总量」会变成用户眼里的矛盾。
    const truncation = readTableTruncation(p, ctx.defRegion, ctx.totalsByUnit);
    const truncationNote = truncation
      ? `<div class="text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1 mt-2" data-table-truncated="true">仅显示前 ${truncation.limit} 行（表格行数被截断，各行之和小于报表总量）</div>`
      : "";

    container.innerHTML = `
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-xs">
          <tr>${p.rowNumber ? '<th class="text-left font-medium px-3 py-2 text-slate-500 w-10">#</th>' : ""}${headCells}</tr>
        </thead>
        <tbody class="divide-y divide-slate-100">${bodyRows}</tbody>
      </table>
      ${truncationNote}`;
    return;
  }

  if (type === "CHART") {
    renderChartPayload(container, region, p, unit);
    return;
  }

  if (type === "DATA_QUALITY") {
    const coverage = Number(p.coverage);
    const coverageText = p.coverage == null
      ? "—"
      : Number.isFinite(coverage)
        ? (Math.abs(coverage) <= 1 ? (coverage * 100).toFixed(1) + "%" : coverage.toFixed(1) + "%")
        : String(p.coverage);
    const items = [
      { label: "覆盖率", value: coverageText },
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
    return;
  }

  if (type === "ANALYSIS") {
    container.innerHTML = `
      <div class="rounded-lg bg-blue-50 border border-blue-200 p-4">
        <div class="flex items-start gap-2">
          <iconify-icon class="text-blue-600 text-lg mt-0.5" icon="mdi:lightbulb-on-outline"></iconify-icon>
          <div class="flex-1 min-w-0">
            <div class="text-[11px] text-blue-700 mb-1">AI 解读</div>
            <div class="text-sm text-slate-800 leading-relaxed">${esc(p.analysis || "")}</div>
          </div>
        </div>
      </div>`;
    return;
  }

  container.innerHTML = `<pre class="mono text-[11px] text-slate-500 bg-slate-50 rounded p-2 overflow-x-auto">${esc(JSON.stringify(p, null, 2).slice(0, 400))}</pre>`;
}

// 草稿区域的 chartKind（LINE / BAR / PIE）→ 图表渲染器认识的 componentType。
const CHART_KIND_TO_COMPONENT_TYPE = {
  LINE: "line_chart",
  BAR: "bar_chart",
  PIE: "pie_chart",
  AREA: "line_chart",
};

// 多指标图表的系列配色（按顺序取）。
const CHART_SERIES_COLORS = ["#2563eb", "#16a34a", "#d97706", "#9333ea", "#0891b2"];

/**
 * 图表区域：按载荷真的把图画出来。
 *
 * 两条渲染后端，同一套逻辑：
 *   - ECharts 可用 → 画真图表（纵轴名与系列名都从载荷读，绝不硬编码 kWh / 耗电）；
 *   - ECharts 不可用（CDN 挂了 / 断网）→ 用方案模块导出的 SVG 渲染器
 *     （需求稿 §3.3 保护的那段代码），照样把曲线 / 柱条画出来。
 * 两条都不通 → 把点列出来，数据仍然可见，不留空壳。
 */
function renderChartPayload(container, region, p, unit) {
  const kind = String(p.chartKind || "LINE").toUpperCase();
  const labels = Array.isArray(p.xLabels) ? p.xLabels : [];
  const values = Array.isArray(p.yValues) ? p.yValues : [];
  const series = Array.isArray(p.series) && p.series.length ? p.series : null;

  if (!labels.length || (!values.length && !(series && series[0]?.yValues?.length))) {
    container.innerHTML = `<div class="rounded-lg bg-slate-50 border border-slate-200 px-4 py-3 text-xs text-slate-500">这个图表还没有数据点。</div>`;
    return;
  }

  if (hasEcharts()) {
    const div = document.createElement("div");
    div.style.height = "260px";
    container.appendChild(div);
    try {
      const chart = window.echarts.init(div);
      _chartInstances.push(chart);
      // 系列名从载荷读：多指标取各自 label，单指标取载荷里的指标名，再退到单位。
      const seriesOption = series
        ? series.map((item, index) => ({
            name: String(item.label || item.metricKey || unit || "指标"),
            data: Array.isArray(item.yValues) ? item.yValues : [],
            type: kind === "BAR" ? "bar" : "line",
            smooth: true,
            areaStyle: kind === "AREA" ? { opacity: 0.3 } : undefined,
            itemStyle: { color: CHART_SERIES_COLORS[index % CHART_SERIES_COLORS.length] },
            lineStyle: { color: CHART_SERIES_COLORS[index % CHART_SERIES_COLORS.length] },
          }))
        : [{
            name: String(p.metrics?.[0]?.label || unit || "数值"),
            data: values,
            type: kind === "BAR" ? "bar" : "line",
            smooth: true,
            areaStyle: kind === "AREA" ? { opacity: 0.3 } : undefined,
            itemStyle: { color: CHART_SERIES_COLORS[0] },
            lineStyle: { color: CHART_SERIES_COLORS[0] },
          }];
      chart.setOption({
        tooltip: { trigger: "axis" },
        grid: { left: 60, right: 20, top: 24, bottom: 36 },
        xAxis: {
          type: "category",
          data: labels,
          axisLabel: { fontSize: 11 },
        },
        yAxis: {
          type: "value",
          // 纵轴单位一律从载荷读：PUE、总电费、实时功率都不是 kWh。
          name: unit || undefined,
          nameTextStyle: { fontSize: 10, color: "#64748b" },
          axisLabel: { fontSize: 11 },
        },
        series: seriesOption,
      });
      return;
    } catch (err) {
      console.error("[AI Report] 图表初始化失败", err);
      container.innerHTML = "";
    }
  }

  // ECharts 不可用：走方案模块的 SVG 渲染器（不依赖 CDN，照样画曲线 / 柱条）。
  const api = window.WorkbenchPlan;
  if (api && typeof api.renderChart === "function") {
    try {
      const node = api.renderChart(document, {
        componentType: CHART_KIND_TO_COMPONENT_TYPE[kind] || "line_chart",
        title: region.name || "",
        data: { xLabels: labels, yValues: values, unit },
      });
      container.appendChild(node);
      // 折线渲染器自带「单位：…」说明；柱条那条没有 —— 这里补上，
      // 保证「图上的单位写清楚」这条在两条渲染后端上都成立。
      if (unit && !String(node.textContent || "").includes("单位：")) {
        const note = document.createElement("div");
        note.className = "text-[11px] text-slate-400 mt-1";
        note.dataset.chartUnit = "true";
        note.textContent = "单位：" + unit + "，共 " + labels.length + " 个数据点";
        container.appendChild(note);
      }
      return;
    } catch (err) {
      console.error("[AI Report] 图表渲染失败", err);
    }
  }

  // 兜底：至少把点列出来，数据不丢。
  container.innerHTML = `
    <div class="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
      <div class="text-[11px] text-slate-500 mb-1">${esc(unit || "数值")}</div>
      ${labels.map((label, index) => `<div class="text-xs text-slate-700 mono">${esc(label)}: ${esc(values[index] ?? "")}</div>`).join("")}
    </div>`;
}

// -------- 节点 11 RENDERING：用真实预览数据渲染 --------
const _chartInstances = [];

// 图表库可用性：CDN 挂了（断网 / 被墙 / CSP 拦了）也不能让整条状态机卡住。
// 以前这里是 `echarts.init(...)` 裸调，CDN 一失败就抛 ReferenceError，
// 整个渲染 reject，queueAdvance 永远不会执行 —— 页面永远停在 RENDERING。
// 现在图表还有一条不依赖 CDN 的退路（方案模块的 SVG 渲染器），画不出图也不会卡流程。
function hasEcharts() {
  return typeof window !== "undefined" && !!window.echarts && typeof window.echarts.init === "function";
}

function destroyCharts() {
  for (const c of _chartInstances.splice(0)) {
    try { c.dispose(); } catch (e) {}
  }
}

// 独立的预览面板渲染器（renderFinalReport / renderFinalRegion）已下线：
// 规格 #46 取消了独立的报表预览阶段 —— 草稿阶段右侧已经是带数的报表，
// 点「同意」之后屏幕上的数字不动，只由徽标与顶部说明反映进度。
// 校验与试运行照跑（状态机照旧推进到 READY），产物保留在主线，
// 将来接真实数据时直接复用；前端不再按预览状态切换渲染入口。
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
  // 两个键，语义不同：
  //   - previewPayload：试运行结果（持久化的快照里就有）；
  //   - reportPreviewPayload：草稿阶段的模拟示意 —— 后端按同一份定义与上下文
  //     重新生成的同构载荷，**临时附加**在这次会话读取响应上（没有落库）。
  session.previewPayload = detail.snapshot?.previewPayload ?? null;
  session.baseDefinitionRevision = session.definition?.metadata?.revision ?? null;
  applyReportPreviewPayload(detail.snapshot?.reportPreviewPayload ?? null);
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

  // 恢复右侧报表面板：只有一条渲染入口，数据只来自示意载荷。
  try {
    renderReportPanel();
  } catch (err) {
    console.error("[AI Report] 恢复报表失败", err);
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
  session.reportPreviewPayload = null;
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
