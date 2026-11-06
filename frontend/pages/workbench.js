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
  // 恢复会话时「示意数据没生成出来」的原因（后端放在 snapshot.error 上）。
  // 有它就说明这一轮**没出数**：右侧要写明原因，不伪造载荷、不给空壳卡片。
  // 载荷真的到了（聊天响应或恢复响应带来 reportPreviewPayload）就清掉。
  reportPreviewError: null,
  pendingResume: null, // 停止后等待重新执行的原请求
  workspaceId: null, // 归属项目
  // 草稿对象身份（契约 0002 §6）：独立于 sessionId/conversationId/messageId。
  // 同一草稿跨会话共享身份与锁；后端下发，前端只读。
  draftId: null,
  // 草稿版本：revision = 当前编辑版本；savedRevision = 显式保存版。
  // 两者不同义：没显式保存过 savedRevision 就是 null。
  draftRevision: null,
  draftSavedRevision: null,
  // 草稿备注：用户自己写的一句话说明（服务端下发，前端只读）。
  draftRemark: "",
  // 撤销 / 重做游标：能否撤销、能否重做由服务端给，前端不自己算。
  canUndo: false,
  canRedo: false,
  // 解析好的主题（服务端下发）。前端不拼主题注册表。
  resolvedTheme: null,
  // 「显示隐藏内容」开关：纯视图状态，**不写报表定义**。
  showHiddenContent: false,
  // 被停止的那一轮：停完之后**只剩两个选项**（继续 / 撤销），输入与其他
  // 同对象写动作都锁着（契约 0002 §7.1）。taskId 由 messageId 推导。
  stoppedTaskId: null,
  stoppedChoices: [],
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

// -------- 草稿工作区接口（契约 0002 §7） --------
// 草稿是**数据库对象**：有自己的 id、版本、撤销历史。
// 这几个动作走服务端，不在前端自己改定义 —— 前端改了就和服务端对不上。
const draftApi = {
  async request(path, options = {}) {
    const res = await fetch(API_BASE + path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!res.ok) {
      let detail = "";
      try {
        const body = await res.json();
        detail = body?.detail || body?.message || "";
      } catch (e) { /* 响应体不是 JSON：只报状态码 */ }
      throw new Error(detail ? `HTTP ${res.status}：${detail}` : `HTTP ${res.status}`);
    }
    if (res.status === 204) return null;
    return res.json();
  },
  list() {
    return this.request("/api/drafts");
  },
  detail(draftId) {
    return this.request("/api/drafts/" + encodeURIComponent(draftId));
  },
  remove(draftId) {
    return this.request("/api/drafts/" + encodeURIComponent(draftId), { method: "DELETE" });
  },
  action(draftId, action, payload = {}) {
    return this.request("/api/drafts/" + encodeURIComponent(draftId) + "/actions", {
      method: "POST",
      body: JSON.stringify({ action, ...payload }),
    });
  },
  // 打开草稿：把这条会话切到那个对象上（切的是对象身份，不是复制一份）。
  open(draftId, sessionId) {
    return this.request("/api/drafts/" + encodeURIComponent(draftId) + "/open", {
      method: "POST",
      body: JSON.stringify({ sessionId: sessionId || null }),
    });
  },
  stopTask(taskId) {
    return this.request("/api/draft-tasks/" + encodeURIComponent(taskId) + "/stop", {
      method: "POST",
    });
  },
  resumeTask(taskId) {
    return this.request("/api/draft-tasks/" + encodeURIComponent(taskId) + "/resume", {
      method: "POST",
    });
  },
  discardTask(taskId) {
    return this.request("/api/draft-tasks/" + encodeURIComponent(taskId) + "/discard", {
      method: "POST",
    });
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
// 文字方案（#19 先确认，再生成；#66 底部追加最新版）
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
//
// #66 之后方案卡不再是「会移动的气泡」：
//   - 每来**一版**方案，就在左侧对话区**底部追加**一张新卡；
//   - 旧卡**留在原地**，标为历史，确认入口失效；
//   - 同一份方案（按稳定 versionId 判重）不追加第二遍；
//   - 用户在上方读历史时**不强行滚动**，给一个可点的「有最新方案」提示。
// --------------------------------------------------------------------------

const planState = {
  designPhase: null,
  reportPlan: null,
  busy: false,
  error: null,
  // 已确认方案的折叠视图是否展开。默认收起（需求稿 §2.8）。
  planExpanded: false,
  // 卡片外壳（<details>）是否展开。最新一版默认展开 —— 它在对话区底部，要能读全。
  cardExpanded: true,
  // 当前这一版的稳定 ID（契约表 §4.1.2）。前端按它去重。
  versionId: "",
  // 卡片前的那一句简短说明（与实际提交改动一致）。
  summaryLine: "",
};

// --------------------------------------------------------------------------
// 方案卡片登记表（#66）
//
// 去重键 → 卡片句柄。**别名**都指向同一张卡：
//   "ver:<versionId>"      服务端给的稳定 ID（契约表 §4.1.2）
//   "plan:<planId>#<rev>"  方案身份 + 版本号（契约表 §4.1，与上面一一对应）
// 两个别名都记上，刷新前后拿到的键名不一样也不会把同一份方案追加两遍。
// --------------------------------------------------------------------------

const planCards = {
  /** 别名 → 卡片句柄 */
  byAlias: new Map(),
  /** 当前待确认的那一版（最新一版）的卡片句柄 */
  current: null,
  /** 自增序号：给每张卡片一个稳定的 DOM 标识 */
  serial: 0,
};

/** 卡片登记表清空（对话区被清空时用）。 */
function resetPlanCards() {
  planCards.byAlias.clear();
  planCards.current = null;
  planCards.serial = 0;
}

/** 清掉已经不在页面上的卡片登记：别让去重表记住一张已经摘掉的卡。 */
function pruneDisconnectedPlanCards() {
  for (const [alias, card] of Array.from(planCards.byAlias.entries())) {
    if (!card || !card.root || !card.root.isConnected) planCards.byAlias.delete(alias);
  }
  const current = planCards.current;
  if (current && (!current.root || !current.root.isConnected)) planCards.current = null;
}

/** 这一份方案是不是已经在屏上了（按稳定版本键判重）。 */
function findPlanCard(version) {
  const api = window.WorkbenchPlan;
  if (!api || !version) return null;
  pruneDisconnectedPlanCards();
  for (const alias of api.versionAliasKeys(version)) {
    const known = planCards.byAlias.get(alias);
    if (known) return known;
  }
  return null;
}

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

/**
 * 建一条承载方案模块的 AI 气泡。
 * @param {string} rootId 挂载根的 DOM id（当前卡是 plan-root，历史卡另起一个）
 */
function createPlanBubble(rootId) {
  const bubble = document.createElement("div");
  bubble.className = "flex gap-2.5";
  bubble.setAttribute("data-plan-bubble", "");

  const avatar = document.createElement("div");
  avatar.className = "w-8 h-8 shrink-0 rounded-full bg-gradient-to-br from-blue-500 to-blue-700 flex items-center justify-center";
  avatar.innerHTML = '<iconify-icon class="text-white" icon="mdi:robot-outline"></iconify-icon>';

  const body = document.createElement("div");
  body.className = "flex-1 min-w-0";

  const root = document.createElement("div");
  root.id = rootId || "plan-root";

  body.appendChild(root);
  bubble.appendChild(avatar);
  bubble.appendChild(body);
  return { bubble, root };
}

/** 建一张方案卡片的句柄（先不渲染）。 */
function makePlanCard(bubble, root, version) {
  planCards.serial += 1;
  return {
    serial: planCards.serial,
    bubble,
    root,
    version: version || null,
    aliases: [],
    isHistory: false,
    cardExpanded: true,
    summaryLine: "",
    designPhase: null,
    reportPlan: null,
  };
}

/** 把一版的别名登记进表（去重靠它）。 */
function registerPlanCard(card, version) {
  const api = window.WorkbenchPlan;
  if (!api || !card) return;
  const aliases = api.versionAliasKeys(version);
  card.aliases = aliases;
  for (const alias of aliases) planCards.byAlias.set(alias, card);
}

/**
 * 拿到方案容器（必要时重建），并挂到对话流里。
 * 对话区被清空过 → 旧容器已经不在文档里了 → 重建一条。
 * @returns {{root: Element|null, fresh: boolean}} fresh=true 表示这次是新挂上去的
 */
function mountPlanHost() {
  if (!chatScroll) return { root: null, fresh: false };
  pruneDisconnectedPlanCards();
  if (!planCards.current || !planRoot || !planRoot.isConnected) {
    const host = createPlanBubble("plan-root");
    planBubble = host.bubble;
    planRoot = host.root;
    planCards.current = makePlanCard(planBubble, planRoot, null);
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

/**
 * 把当前这一张卡**降级**成历史卡：留在原地，标为历史，确认入口失效。
 *
 * 降级只改「这张卡是什么身份」，**不改它的原文** —— 旧版正文保留在原地。
 */
function demoteCurrentPlanCard() {
  const api = window.WorkbenchPlan;
  const card = planCards.current;
  if (!api || !card || !card.root) return;
  card.isHistory = true;
  card.cardExpanded = false;
  // 历史卡的 DOM id 让开：当前卡用 plan-root，历史卡另起一个。
  card.root.id = "plan-root-history-" + card.serial;
  api.render(
    card.root,
    {
      designPhase: card.designPhase,
      reportPlan: card.reportPlan,
      busy: false,
      error: null,
      canConfirm: false,
      planExpanded: card.cardExpanded,
      versionId: card.version ? card.version.versionId : "",
      isHistory: true,
      collapsible: true,
      cardExpanded: card.cardExpanded,
      cardTitle: planHistoryCardTitle(card),
      summaryLine: card.summaryLine,
    },
    { onAction: handlePlanAction }
  );
}

/** 历史卡的标题：「历史版本 · 版本 N」（版本号拿不到就只说历史版本）。 */
function planHistoryCardTitle(card) {
  const api = window.WorkbenchPlan;
  const base = (api && api.PLAN_CARD_TITLE_HISTORY) || "历史版本";
  const revision = card && card.reportPlan ? card.reportPlan.revision : null;
  if (revision === null || revision === undefined) return base;
  return base + " · 版本 " + revision;
}

/**
 * 在对话区**底部追加**一张新的方案卡片（#66）。
 *
 * - 旧卡留在原地（降级成历史卡）；
 * - 同一份方案不追加第二遍（按稳定版本键判重）；
 * - 追加完按用户所在位置决定要不要跟随滚动。
 *
 * @returns {boolean} true = 真的追加了一张
 */
function appendPlanCard(version, options = {}) {
  const api = window.WorkbenchPlan;
  if (!api || !chatScroll || !version || !version.reportPlan) return false;
  pruneDisconnectedPlanCards();
  // 去重：重复响应、乱序响应、重连都不追加同一份方案。
  if (findPlanCard(version)) return false;

  // 旧卡留在原地：当前这一张先降级成历史卡。
  demoteCurrentPlanCard();

  const host = createPlanBubble("plan-root-pending-" + (planCards.serial + 1));
  const card = makePlanCard(host.bubble, host.root, version);
  registerPlanCard(card, version);
  card.designPhase = options.designPhase || null;
  card.reportPlan = version.reportPlan;
  card.summaryLine = options.summaryLine || "";
  card.cardExpanded = true;

  // 新卡成为「当前待确认的那一版」。
  planCards.current = card;
  planBubble = card.bubble;
  planRoot = card.root;
  card.root.id = "plan-root";
  chatScroll.appendChild(card.bubble);
  chatBubbleObserver.observe(card.bubble);

  renderPlanModule();
  // 挪到本轮回复的下面（延到这一轮渲染结束）。
  anchorPlanBubble();
  // 用户不在底部时**不强行滚动**，给一个可点的提示。
  // 恢复会话时（follow=false）不跟：整段历史一次性铺开，最后统一贴底。
  if (options.follow !== false) followLatestPlan();
  return true;
}

/**
 * 新方案来了之后要不要跟着滚到底。
 *
 * 用户在底部 → 跟正常新消息一样滚到底；
 * 用户在上方读历史 → **不强行滚动**，给一个可点的「有最新方案」提示。
 */
function followLatestPlan() {
  if (chatStickToBottom) {
    scrollChatBottom();
    hideLatestPlanHint();
    return;
  }
  showLatestPlanHint();
}

/** 显示「有最新方案」提示（用户在读历史，没被强行拽到底）。 */
function showLatestPlanHint() {
  const btn = $("btn-latest-plan");
  if (btn) btn.classList.remove("hidden");
}

/** 收起「有最新方案」提示。 */
function hideLatestPlanHint() {
  const btn = $("btn-latest-plan");
  if (btn) btn.classList.add("hidden");
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
    // 卡片不在屏上了，去重表也别记着它。
    const card = planCards.current;
    if (card) {
      for (const alias of card.aliases) planCards.byAlias.delete(alias);
    }
    planCards.current = null;
    planBubble = null;
    planRoot = null;
    syncInputControls();
    return;
  }
  const { root, fresh } = mountPlanHost();
  if (!root) return;
  // 记住用户有没有展开折叠视图：重渲染（busy 抖动 / 新响应）时不把它弹回去。
  const existing = root.querySelector("details.confirmed-plan-details");
  if (existing) planState.planExpanded = existing.open === true;
  const shell = root.querySelector("details.plan-card-details");
  if (shell) planState.cardExpanded = shell.open === true;
  api.render(
    root,
    {
      designPhase: planState.designPhase,
      reportPlan: planState.reportPlan,
      busy: planState.busy,
      error: planState.error,
      canConfirm: planCanConfirm(),
      planExpanded: planState.planExpanded,
      versionId: planState.versionId,
      isHistory: false,
      collapsible: true,
      cardExpanded: planState.cardExpanded,
      cardTitle: api.PLAN_CARD_TITLE_CURRENT,
      summaryLine: planState.summaryLine,
    },
    { onAction: handlePlanAction }
  );
  // 当前卡的内容记在卡片自己身上：它降级成历史卡时要还原原文。
  const card = planCards.current;
  if (card) {
    card.designPhase = planState.designPhase;
    card.reportPlan = planState.reportPlan;
    card.summaryLine = planState.summaryLine;
    card.version = {
      versionId: planState.versionId,
      planId: planState.reportPlan?.id || "",
      planRevision: planState.reportPlan?.revision ?? null,
    };
  }
  if (fresh) anchorPlanBubble();
  // The response can add a menu after request cleanup has unlocked ordinary chat.
  syncInputControls();
}

/**
 * 这一版是不是比当前那一版**新**。
 *
 * 判据是方案版本号（契约表 §4.1：修改时 revision 递增）。
 * 版本号拿不到 → 认为是新的：宁可多画一张，也不把服务端给的新版本挡掉。
 */
function isNewerPlanVersion(version, currentVersion) {
  if (!version) return false;
  if (!currentVersion) return true;
  const next = version.planRevision;
  const current = currentVersion.planRevision;
  if (next === null || next === undefined) return true;
  if (current === null || current === undefined) return true;
  return next > current;
}

/**
 * 卡片前的那一句简短说明。
 *
 * **只用服务端的话**：这一轮响应带的回复（首句）。
 * 前端不自己描述「改了什么」—— 自己编出来的说明会与实际提交不一致（工单 #66）。
 */
function buildPlanSummaryLine(data, version) {
  const reply = typeof data?.reply === "string" ? data.reply.trim() : "";
  if (reply) return firstSentence(reply);
  const revision = version ? version.planRevision : null;
  return revision === null || revision === undefined
    ? "文字方案已更新。"
    : "文字方案已更新到版本 " + revision + "。";
}

/** 取第一句（到第一个句号 / 感叹号 / 问号为止），并限长。 */
function firstSentence(text, limit = 80) {
  const flat = String(text || "").replace(/\s+/g, " ").trim();
  if (!flat) return "";
  const matched = flat.match(/^[\s\S]*?[。！？!?]/);
  let sentence = matched ? matched[0] : flat;
  if (sentence.length > limit) sentence = sentence.slice(0, limit) + "…";
  return sentence.trim();
}

/** 从一次响应里同步方案状态。 */
/**
 * 两份版本是不是**同一版**。
 *
 * 按去重键比（versionId 与「方案身份 + 版本号」两个别名都算），
 * 不比字符串：两个来源给的键名不一样时也要能认出是同一版。
 */
function isSamePlanVersion(left, right) {
  const api = window.WorkbenchPlan;
  if (!api || !left || !right) return false;
  const leftKeys = api.versionAliasKeys(left);
  const rightKeys = api.versionAliasKeys(right);
  if (!leftKeys.length || !rightKeys.length) return false;
  return leftKeys.some((key) => rightKeys.includes(key));
}

/** 从一次响应里同步方案状态。 */
function syncPlanFromResponse(data) {
  if (!data) return;
  const phase = data.designPhase || null;
  const plan = data.domainObjects?.reportPlan || null;
  if (!phase && !plan) return;
  const api = window.WorkbenchPlan;
  const version = api ? api.readPlanVersion(data) : null;

  // 这一轮有没有带来**新的一版**方案？
  const known = version ? findPlanCard(version) : null;
  const currentCard = planCards.current;
  const currentVersion = currentCard ? currentCard.version : null;
  const isNewVersion = !!version && !known && isNewerPlanVersion(version, currentVersion);

  // 当前这张卡的内容先记下来：它马上要降级成历史卡，原文要留在原地。
  if (currentCard && isNewVersion) {
    currentCard.designPhase = planState.designPhase;
    currentCard.reportPlan = planState.reportPlan;
    currentCard.summaryLine = planState.summaryLine;
  }

  if (isNewVersion) {
    // 新的一版：底部**追加**一张卡（旧的留在原地）。
    planState.designPhase = phase || planState.designPhase;
    planState.reportPlan = plan || planState.reportPlan;
    planState.versionId = version.versionId || "";
    planState.summaryLine = buildPlanSummaryLine(data, version);
    planState.cardExpanded = true;
    appendPlanCard(version, {
      designPhase: planState.designPhase,
      summaryLine: planState.summaryLine,
    });
    return;
  }

  // 不是新的一版。
  //
  // **迟到的旧版响应不夺取确认资格**：当前卡上那一版比它新，就不拿它覆盖 ——
  // 覆盖了就等于让旧版把当前待确认的那一版顶下去（工单 #66）。
  const sameAsCurrent = isSamePlanVersion(version, currentVersion);
  const aboutPlan = !!(plan || version);
  if (!aboutPlan || sameAsCurrent) {
    planState.designPhase = phase || planState.designPhase;
  }
  if (plan && (sameAsCurrent || (!known && !currentCard))) {
    planState.reportPlan = plan;
  }
  // 出错时展示错误条；成功时清掉上一次的错误。
  //
  // 失败**不**把错误写进方案卡：失败时上一版方案仍然有效（工单 #66），
  // 失败的原因由对话区说（见 appendFailureNotice），卡片保持原样、仍然可确认。
  planState.error = null;
  renderPlanModule();
  // 草稿已出：把右侧滚回顶部，让用户第一眼看到草稿而不是聊天记录。
  if (phase === "DRAFT_READY") previewRoot.parentElement.scrollTop = 0;
}

/** 从会话详情里恢复方案（刷新 / 重建服务后）。 */
function restorePlanFromDetail(detail) {
  const snapshot = detail?.snapshot || {};
  const api = window.WorkbenchPlan;
  const versions = api ? api.readPlanVersionSequence(snapshot) : [];
  planState.designPhase = snapshot.designPhase || null;
  planState.reportPlan = snapshot.reportPlan || null;
  planState.busy = false;
  planState.error = null;
  // 刷新后默认收起（需求稿 §2.8）。
  planState.planExpanded = false;
  planState.cardExpanded = true;
  planState.versionId = "";
  planState.summaryLine = "";
  if (versions.length) {
    // 有版本序列：按**真实保存顺序**还原历史卡片与最新版。
    restorePlanVersions(snapshot, versions);
    return;
  }
  // 老会话没有版本序列：只按**实际存储**恢复当前那一版可用方案，
  // **不伪造**过去的版本（契约表 §4.1.2）。
  renderPlanModule();
}

/** 把屏上的方案卡片全摘掉（对话区被清空时用）。 */
function clearPlanCards() {
  const api = window.WorkbenchPlan;
  if (api && planRoot) api.clear(planRoot);
  if (chatScroll) {
    for (const node of Array.from(chatScroll.querySelectorAll("[data-plan-bubble]"))) {
      if (node.parentNode) node.parentNode.removeChild(node);
    }
  }
  resetPlanCards();
  planBubble = null;
  planRoot = null;
}

/**
 * 按**真实保存顺序**还原方案版本卡片（刷新 / 重新打开会话）。
 *
 * 每一版一张卡：前面的是历史卡（留在原地、标为历史、确认入口失效），
 * 最后一张是当前待确认的那一版。
 */
function restorePlanVersions(snapshot, versions) {
  const api = window.WorkbenchPlan;
  if (!api) return;
  const usable = versions.filter((record) => record && record.reportPlan);
  if (!usable.length) return;

  // 只画了当前方案的那张卡先摘掉：下面按真实保存顺序逐版重建。
  clearPlanCards();

  const phase = snapshot.designPhase || null;
  for (let index = 0; index < usable.length; index += 1) {
    const record = usable[index];
    const isLast = index === usable.length - 1;
    // 中间那些版当时是「待确认」，现在只是历史 —— 不摆阶段徽标。
    if (planCards.current) planCards.current.designPhase = null;
    planState.designPhase = isLast ? phase : null;
    planState.reportPlan = record.reportPlan;
    planState.versionId = record.versionId || "";
    planState.summaryLine = "";
    planState.cardExpanded = true;
    appendPlanCard(record, { designPhase: planState.designPhase, follow: false });
  }
  // 最后一版就是当前待确认的那一版：确认资格按服务端阶段算。
  planState.designPhase = phase;
  renderPlanModule();
}

/** 确认回调：模块只交回意图，真正的请求由上层发。 */
function handlePlanAction(action) {
  if (isBusy() || !action) return;
  if (!planCanConfirm()) return;
  if (action.planId !== planState.reportPlan?.id || action.basePlanRevision !== planState.reportPlan?.revision) return;
  if (action.designAction === "MODIFY_PLAN") {
    const text = String(action.text || "").trim();
    if (text) sendMessage(text, { fromPlan: true });
    return;
  }
  if (action.designAction !== "CONFIRM_PLAN") return;
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

// --------------------------------------------------------------------------
// 输入锁（契约表 §5「有效菜单与输入锁」）
// --------------------------------------------------------------------------
//
// 锁的范围由服务端菜单上的 lockScope 决定，取值固定三档：
//
//   none            不锁
//   menu_only       只锁下拉框与快捷入口；输入框与发送按钮保持可用
//   input_and_menu  输入框 + 发送按钮 + Enter + 快捷入口 + 下拉框全锁
//
// 「下拉框锁」的实现口径：**下拉框锁定到当前有效菜单** —— 页面上只有那份
// 有效菜单的下拉框能点，其余下拉框（历史轮回放出来的）一律禁用。
// 有效菜单自己的下拉框必须留着可点：它是这张卡唯一的作答入口，
// 把它也锁死，用户就一条路都走不通了。
//
// 「模板已生成、等待下一步操作选择」这一阶段服务端写 input_and_menu；
// 老会话的菜单没有这个键 → 按 menu_only 处理（等于今天的行为）。
const LOCK_SCOPE_NONE = "none";
const LOCK_SCOPE_MENU_ONLY = "menu_only";
const LOCK_SCOPE_INPUT_AND_MENU = "input_and_menu";

// 锁住时的界面提示（规格第 76 条）。
const INPUT_LOCK_HINT = "请先选择下一步操作";

/** 这份菜单当前锁到哪一层。 */
function lockScopeOf(question) {
  const scope = question?.lockScope;
  if (
    scope === LOCK_SCOPE_NONE ||
    scope === LOCK_SCOPE_MENU_ONLY ||
    scope === LOCK_SCOPE_INPUT_AND_MENU
  ) {
    return scope;
  }
  // 老数据没有这个字段：按 menu_only 处理（契约表 §5.3）。
  return LOCK_SCOPE_MENU_ONLY;
}

/** 这一份菜单是不是把输入框也锁住了。 */
function isInputLocked(question) {
  return lockScopeOf(question) === LOCK_SCOPE_INPUT_AND_MENU;
}

/**
 * 这一步是不是**文字型待决**（服务端说「直接填写」）。
 *
 * 文字型待决的合法入口是**卡片里的专用输入**，不是底部那个普通聊天框。
 * 所以它一旦成为当前待决步骤，普通输入 / 发送 / Enter / 快捷入口
 * 都要锁住（契约 §3.3）—— 与「允许其他」的菜单同一套口径。
 */
function expectsFreeText(question) {
  return String(question?.expects || "").trim().toUpperCase() === "TEXT";
}

/**
 * 这份菜单是不是服务端**当前待决**的那一张。
 *
 * 刷新后的历史回放会兜底算出一张**旧菜单**（``restoreSession`` 里的
 * ``resolveOpenMenuAfterReplay``）。那张菜单**不是**当前待决步骤 ——
 * 权威状态是后端的 ``pendingChoice``：它说「现在没有待选项」，
 * 输入就不该被锁住（规格第 60 条、契约表 §5.2）。
 *
 * ``authoritative: false`` 就是这条「非权威」的标记，**只有历史兜底那条
 * 路径会打**。服务端 ``pendingChoice`` 带来的有效 dropdown 不带这个标记，
 * 仍然全锁 —— 不能为了修历史误锁就把有效 dropdown 的锁放开。
 */
function isAuthoritativeChoice(choice) {
  return choice?.authoritative !== false;
}

/**
 * 这份菜单**锁不锁输入与发送**（前端引导；真正的边界在后端）。
 *
 * - 非权威菜单（历史兜底出来的旧菜单）→ 只摆出来，**不锁**；
 * - 有效 dropdown（服务端 pendingChoice）→ 一律全锁，不受这里影响；
 * - 有待确认计划时（``planWaiting``）→ 锁（计划卡里确认，不能走普通输入）。
 */
function menuLocksInput(choice, planWaiting = false) {
  if (planWaiting) return true;
  if (!isAuthoritativeChoice(choice)) return false;
  const scope = lockScopeOf(choice);
  return (
    scope === LOCK_SCOPE_INPUT_AND_MENU
    || Boolean(choice?.options?.length)
    || choice?.allowsOther === true
    // 文字型待决（「直接填写」）：合法入口是卡片里的**专用输入**，
    // 普通聊天框必须锁住 —— 否则这一步就有两个入口，
    // 用户能从普通输入绕过这一步的约束（契约 §3.3）。
    //
    // 判据就地写成表达式（不调用 expectsFreeText）：这段函数会被既有单测
    // 单独抽进沙箱跑，沙箱里只有它原有的依赖 —— 少一个符号就直接抛错。
    || String(choice?.expects || "").trim().toUpperCase() === "TEXT"
  );
}

/**
 * 输入区各控件的可用状态与提示文案。
 *
 * 单独抽成一个纯函数：界面锁定只是**引导**（规格第 56 条），
 * 真正的边界在后端对确认与权限的校验。这里只决定「看起来能不能打字」。
 */
function inputControlState(lockedChoice, busy, planWaiting = false) {
  const scope = lockScopeOf(lockedChoice);
  // 锁不锁输入：**有效 dropdown 全锁**（正确要求，不动）；
  // 历史兜底出来的旧菜单不锁（那不是当前待决步骤）。
  const locked = menuLocksInput(lockedChoice, planWaiting);
  // 文字型待决的提示指向**卡片里的专用输入**（与方案待确认同一句口径）。
  // 判据就地写成表达式：不调用 expectsFreeText —— 这段函数会被既有单测
  // 单独抽进沙箱跑，沙箱里只有它原有的依赖。
  const freeTextStep = String(lockedChoice?.expects || "").trim().toUpperCase() === "TEXT";
  const lockedHint = planWaiting
    ? "请在方案卡片内确认或填写修改要求"
    : freeTextStep
      ? "请在卡片内填写后提交"
      : INPUT_LOCK_HINT;
  return {
    scope,
    inputLocked: locked,
    inputDisabled: Boolean(busy) || locked,
    sendDisabled: Boolean(busy) || locked,
    // 快捷入口按菜单本身判断：menu_only 的语义是「锁下拉框与快捷入口」，
    // 输入框与发送按钮保持可用（见 LOCK_SCOPE_* 的注释）。
    quickDisabled: Boolean(busy) || Boolean(lockedChoice) || planWaiting,
    placeholder: locked
      ? lockedHint
      : lockedChoice
        ? "可以先问只读问题，或从上方下拉框选择"
        : "描述报表需求，或输入修改要求…",
    hint: locked ? lockedHint : "",
  };
}

function hasPendingPlan() {
  return planState.designPhase === "PLAN_WAITING_CONFIRM" && Boolean(planState.reportPlan?.id);
}

// 锁住时在输入框下面挂一条**看得见**的提示。
// 输入框是禁用状态，placeholder 会被浏览器淡化；这条提示让用户明确知道
// 为什么现在不能打字（规格第 76 条）。
let inputLockHintEl = null;

function syncInputLockHint(controls) {
  const input = $("chat-input");
  const host = input?.parentElement;
  if (!input || !host) return;
  if (!inputLockHintEl) {
    inputLockHintEl = document.createElement("div");
    inputLockHintEl.className = "mt-1.5 text-[11px] text-amber-700";
    inputLockHintEl.setAttribute("data-input-lock-hint", "");
    (host.parentElement || host).appendChild(inputLockHintEl);
  }
  inputLockHintEl.textContent = controls.hint;
  inputLockHintEl.classList.toggle("hidden", !controls.hint);
}

function syncInputControls() {
  const busy = isBusy();
  const controls = inputControlState(lockedChoice, busy, hasPendingPlan());
  const input = $("chat-input");
  // Pending choices are answered inside their card, never through ordinary chat.
  input.disabled = controls.inputDisabled;
  input.placeholder = controls.placeholder;
  const send = $("btn-send");
  send.disabled = controls.sendDisabled;
  send.classList.toggle("opacity-50", controls.sendDisabled);
  send.title = controls.hint;
  // 被停止的那一轮：输入锁着，直到用户选「继续」或「撤销」。
  // session 可能不存在（这函数也会被单独抽出来跑），没有会话就没有停止现场。
  if (session?.stoppedTaskId) {
    input.disabled = true;
    send.disabled = true;
    send.classList.add("opacity-50");
    input.placeholder = "已停止，请选择「继续完成剩下的工作」或「撤销（回到上一步）」";
    send.title = "已停止：先选择继续或撤销";
  }
  $("btn-stop").classList.toggle("hidden", !busy);
  syncInputLockHint(controls);
  document.querySelectorAll("[data-quick], #btn-regenerate").forEach((button) => {
    // 快捷生成 / 重新生成是**操作**，不是提问：待确认菜单没选完之前仍然不给点。
    button.disabled = controls.quickDisabled;
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
  // **同一步**的判定用步骤身份（stepId）。
  //
  // 为什么：同一轮里服务端可能给**两张同一步**的卡 —— 一张是澄清菜单
  // （带选项），一张是那张「直接填写」的专用输入卡（实测：模型路径的
  // 澄清轮就是这样）。契约 §3.3 要开放的是「**本步**合法选项与专用输入」，
  // 两样都属于这一步，所以两张卡上的控件都要开着 —— 只开一张会把用户
  // 挡在模型给的那道问题外面。
  //
  // 步骤身份取卡片容器上的 data-step-id（没有步骤身份的旧会话退回
  // id 对比：同 id 的多张卡里只有最后一张算当前）。
  const currentStepId = lockedChoice?.stepId || "";
  const stepIdOf = (el) => {
    const card = typeof el?.closest === "function" ? el.closest("[data-step-id]") : null;
    return card?.getAttribute?.("data-step-id") || "";
  };
  selects.forEach((select, index) => {
    // 「修改历史」下拉（工具条上的撤销 / 重做）**不是**问答菜单。
    //
    // 它带着 question-select 这个类（样式上要与别的下拉一致），但它归
    // **服务端给的 canUndo / canRedo** 管，不归选择锁管：按问答菜单那套
    // 规则算，它在「有菜单待答」时被锁、在「没有菜单」时又被另一条规则
    // 锁 —— 撤销入口就永远点不开了（BUG-008：撤销这条路必须能走）。
    //
    // 而且接受一次之后服务端会**再问一句「请选择下一步操作」** ——
    // 也就是说「有菜单待答」恰恰是用户想撤销的时刻。撤销是**回退**，
    // 不是新任务，不该被菜单锁挡住（契约 §2.3 的验收路径就是
    // 接受 → 撤销）。真正该拦它的是「正在发请求」（别在请求途中改草稿）。
    //
    // 「能不能撤销」由服务端给的 canUndo / canRedo 决定（选项自带 disabled）。
    if (select.getAttribute?.("data-wp-history") === "1") {
      select.disabled = busy;
      return;
    }
    // 当前卡片的判定：id 对得上（当前那张），**或**属于同一步（这一步的
    // 另一张卡：澄清菜单 / 专用输入）。
    //
    // 原来在没有匹配项时退化成「页面最后一个下拉框算当前的」—— 文字型
    // 待决那一步**没有**下拉框（它只有卡片内的专用输入），这时最后一个
    // 下拉框其实是**历史轮**的菜单，退化会让旧菜单重新可点，
    // 打破「被撤销 / 已失效的旧步骤不能再选」（契约 §2.3 第 2 条）。
    const sameStep = Boolean(currentStepId) && stepIdOf(select) === currentStepId;
    const isActive = activeId ? (sameStep || index === activeIndex) : index === selects.length - 1;
    // 被撤销的那一步：旧 dropdown **不能再选**（契约 §2.3 第 2 条）。
    // 标记由 applyRevokedSteps 打在控件上 —— 重新同步一次也不能把它打开。
    const revoked = select.dataset?.revoked === "1";
    const lockedHasEntry = Boolean(lockedChoice?.options?.length || lockedChoice?.allowsOther);
    select.disabled = busy || revoked || !(lockedHasEntry || sameStep) || !isActive;
  });
  // 「重新开始」入口跟下拉框同一条规则：只有当前有效菜单那一个能点。
  const restarts = document.querySelectorAll(".question-restart");
  let menuRestartIndex = -1;
  if (activeId) {
    for (let i = restarts.length - 1; i >= 0; i--) {
      if (restarts[i].dataset.questionId === activeId) {
        menuRestartIndex = i;
        break;
      }
    }
  }
  restarts.forEach((button, index) => {
    // 同下拉框：当前卡片必须 id 对得上（见上面 isActive 的说明）。
    const isActive = activeId ? index === menuRestartIndex : index === restarts.length - 1;
    button.disabled = busy || !lockedChoice?.options?.length || !isActive;
  });
  // 「其他」输入框：只对**当前待答**的那张卡开放。
  // 已经答过的卡、历史回放出来的卡，输入框与提交按钮都是灰的 ——
  // 答案只能写进正在等的那张卡（刷新后由服务端 pending_choice 恢复，同样只开一张）。
  document.querySelectorAll("[data-question-other]").forEach((box) => {
    // 卡片所属的下拉框是不是本步可点的那个（上面刚算过它的启用状态）。
    const selectActive = Boolean(box._questionSelect) && !box._questionSelect.disabled;
    const sameStep = Boolean(currentStepId) && stepIdOf(box) === currentStepId;
    const isCurrent = (box.dataset.questionOther === (lockedChoice?.id || "") || sameStep) &&
      selectActive && Boolean(box.dataset.answerMode);
    const input = box.querySelector(".question-other-input");
    const submit = box.querySelector(".question-other-submit");
    if (input) {
      // 被撤销的那一步：专用输入也不能再提交。
      input.disabled = busy || input.dataset?.revoked === "1" || !isCurrent;
      input.classList.toggle("opacity-50", input.disabled);
    }
    if (submit) {
      submit.disabled = busy || submit.dataset?.revoked === "1" || !isCurrent;
      submit.classList.toggle("opacity-50", submit.disabled);
    }
  });
  // 文字型待决的**专用输入**：只对**当前这一步**开放。
  // 这一步的普通输入框是锁着的（契约 §3.3），这个输入框是本步的合法入口，
  // 所以它必须可填 —— 否则用户被锁在门外。
  //
  // 「当前这一步」按**步骤身份**判：同一轮里澄清菜单与这张专用输入卡
  // 共用同一个 stepId，两张都属于这一步，两个入口都要开着。
  document.querySelectorAll("[data-question-text]").forEach((box) => {
    const sameStep = Boolean(currentStepId) && stepIdOf(box) === currentStepId;
    const isCurrent = sameStep || box.dataset.questionText === (lockedChoice?.id || "");
    const input = box.querySelector(".question-text-input");
    const submit = box.querySelector(".question-text-submit");
    if (input) {
      // 被撤销的那一步：专用输入也不能再提交。
      input.disabled = busy || input.dataset?.revoked === "1" || !isCurrent;
      input.classList.toggle("opacity-50", input.disabled);
    }
    if (submit) {
      submit.disabled = busy || submit.dataset?.revoked === "1" || !isCurrent;
      submit.classList.toggle("opacity-50", submit.disabled);
    }
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
  let stopMessageId = null;
  if (activeRequest) {
    const { controller, thinking, debugCard, resumeRequest } = activeRequest;
    // 确认/自动推进可能已有保存等副作用，不自动重放这些操作。
    resumable = !!resumeRequest && !resumeRequest.fromChoice && !resumeRequest.isAdvance;
    session.pendingResume = resumable
      ? { body: resumeRequest.body, conversationId: resumeRequest.body.conversationId }
      : null;
    stopMessageId = resumeRequest?.body?.messageId || null;
    activeRequest = null;
    controller.abort();
    if (thinking) removeBubble(thinking);
    finishDebugTrace(debugCard, "cancelled");
  } else session.pendingResume = null;
  // 后续消息改用新的请求会话，隔离已经发往后端但来不及终止的模型响应。
  session.conversationId = "conv_" + Date.now() + "_" + requestEpoch;
  clearChoiceLock();
  // **服务端也停**：断 SSE 不等于停成功 —— 服务端把任务置为 STOPPED、
  // 作废提交凭证，之后只剩「继续」与「撤销」两个选项（契约 0002 §7）。
  if (stopMessageId) stopTurnOnServer(stopMessageId);
  syncInputControls();
  appendAiText(resumable
    ? "已停止。输入「继续工作」会重新执行上一条消息。"
    : "已停止。此步骤可能已经在后端执行，请检查结果；不能安全地自动重试。");
}

/**
 * 让**服务端**停掉这一轮。
 *
 * 任务 id 由 messageId 推导（服务端同一套规则），所以前端不用等
 * 服务端回传就能在模型还在跑的时候拼出它。
 */
async function stopTurnOnServer(messageId) {
  if (!session.draftId || !messageId) return;
  const taskId = "task_" + messageId;
  // 服务端在**收到请求时**建任务；前端点停可能比那早几十毫秒 ——
  // 这种情况重试两下就好，不要因此把「停止」判成失败。
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const result = await draftApi.stopTask(taskId);
      session.stoppedTaskId = taskId;
      session.stoppedChoices = result?.choices || ["继续完成剩下的工作", "撤销（回到上一步）"];
      renderStoppedTurn();
      syncInputControls();
      return;
    } catch (err) {
      if (attempt < 2) {
        await new Promise((resolve) => setTimeout(resolve, 400));
        continue;
      }
      // 停不下来的情况不打断对话：这一轮照常结束。
      toast("停止失败：" + (err.message || err));
    }
  }
}

/** 停掉之后的那两个选项：继续 / 撤销。 */
function renderStoppedTurn() {
  const old = document.querySelector("[data-wp-stopped]");
  if (old) old.remove();
  if (!session.stoppedTaskId) return;
  const box = document.createElement("div");
  box.className = "wp-stopped flex flex-wrap items-center gap-2 mt-2 text-xs";
  box.dataset.wpStopped = "1";
  const label = document.createElement("span");
  label.className = "text-slate-600";
  label.textContent = "已停止，接下来只能：";
  box.appendChild(label);
  const resumeBtn = document.createElement("button");
  resumeBtn.type = "button";
  resumeBtn.className = "wp-btn-stop-resume rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-700";
  resumeBtn.dataset.wpStopResume = "1";
  resumeBtn.textContent = "继续完成剩下的工作";
  resumeBtn.addEventListener("click", resumeStoppedTurn);
  box.appendChild(resumeBtn);
  const discardBtn = document.createElement("button");
  discardBtn.type = "button";
  discardBtn.className = "wp-btn-stop-discard rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-700";
  discardBtn.dataset.wpStopDiscard = "1";
  discardBtn.textContent = "撤销（回到上一步）";
  discardBtn.addEventListener("click", discardStoppedTurn);
  box.appendChild(discardBtn);
  const host = document.querySelector("#chat-scroll")
    || document.querySelector("#chat-messages")
    || document.querySelector("#chat-panel");
  if (host) host.appendChild(box);
  else {
    // 找不到聊天容器就退到页面顶部：选项**必须看得见**，
    // 否则用户看到的是「停了，但什么都没发生」。
    document.body.appendChild(box);
  }
  // 停止提示旁也有一个反馈入口（20261104）：针对**这次已停止的请求**，
  // 明确是客户端观察，不冒充最终回复。
  // 原菜单（继续 / 撤销）与草稿锁**一律不动**。
  if (typeof registerFeedbackClientEvent !== "function") return;
  registerFeedbackClientEvent("client_stopped", {
    taskId: session.stoppedTaskId,
    observation: "user_stopped",
  }).then((target) => {
    if (!target) return;
    let feedbackHost = box.querySelector("[data-feedback-host]");
    if (!feedbackHost) {
      feedbackHost = document.createElement("div");
      feedbackHost.className = "feedback-host";
      feedbackHost.dataset.feedbackHost = "1";
      box.appendChild(feedbackHost);
    }
    const module = feedbackModule();
    if (module) module.attach(feedbackHost, target);
  });
}

/** 继续：从服务端检查点接着做，**不重复已提交阶段**。 */
async function resumeStoppedTurn() {
  const taskId = session.stoppedTaskId;
  if (!taskId) return;
  let resumed = null;
  try {
    resumed = await draftApi.resumeTask(taskId);
  } catch (err) {
    toast("继续失败：" + (err.message || err));
    return;
  }
  const pending = session.pendingResume;
  session.stoppedTaskId = null;
  session.stoppedChoices = [];
  document.querySelector("[data-wp-stopped]")?.remove();
  if (!pending) {
    // 刷新之后续跑：前端手里没有原来的请求体。
    // 但**服务端**的检查点里有本轮身份与原文（契约 0002 §7）——
    // 用它重建这一轮再重发（同一个 messageId，幂等键不变），
    // 否则就只是「显示已继续」，**没有任务执行**。
    const checkpoint = (resumed && resumed.checkpoint) || {};
    const messageId = checkpoint.messageId || (resumed && resumed.operationId) || null;
    if (!messageId) {
      syncInputControls();
      toast("已继续，可以接着说下一步。");
      return;
    }
    // 快照也要带上：服务端靠它知道「当前这份草稿长什么样」。
    // 没有它，改稿请求会被当成新建（改的不是同一个东西）。
    const snapshots = {};
    if (session.policy) snapshots.reportPolicy = session.policy;
    if (session.definition) snapshots.reportDefinition = session.definition;
    if (session.lastReportRun) snapshots.lastReportRun = session.lastReportRun;
    session.pendingResume = {
      body: {
        agentProtocolVersion: "1.0",
        conversationId: session.conversationId,
        sessionId: checkpoint.sessionId || session.sessionId,
        messageId,
        // **用这一轮当时的状态**：续跑是重做同一轮，不是把同一句话换个状态重跑。
        // 写死 ANALYZING 会把「改这一版」变成「重新生成一份」。
        state: checkpoint.state || "ANALYZING",
        message: checkpoint.message || "继续工作",
        context: { ...DEFAULT_CONTEXT, ...(checkpoint.context || {}) },
        ...(Object.keys(snapshots).length ? { snapshots } : {}),
      },
      conversationId: session.conversationId,
    };
  }
  // 走既有「继续工作」通道：复用原来的 messageId 重发，后端按幂等键处理，
  // **已提交的阶段不会再落一次**（提交层按定义版本去重）。
  await sendMessage("继续工作");
}

/** 撤销（回到上一步）：丢掉这次被停掉的操作，回到上一个完成状态。 */
async function discardStoppedTurn() {
  const taskId = session.stoppedTaskId;
  if (!taskId) return;
  try {
    const result = await draftApi.discardTask(taskId);
    session.stoppedTaskId = null;
    session.stoppedChoices = [];
    session.pendingResume = null;
    document.querySelector("[data-wp-stopped]")?.remove();
    if (result?.definition) {
      session.definition = result.definition;
      session.baseDefinitionRevision = result.definition?.metadata?.revision ?? null;
    }
    applyWorkspaceState(result);
    if (result?.reportPreviewPayload) {
      session.reportPreviewPayload = result.reportPreviewPayload;
      session.reportPreviewError = null;
    } else {
      await refreshPreviewPayload();
    }
    renderReportPanel();
    updateMeta();
    syncInputControls();
    toast("已撤销，回到上一步。");
  } catch (err) {
    toast("撤销失败：" + (err.message || err));
  }
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
  trace_round: "ReAct 逐轮",
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
    // 用户自己回到底部了：「有最新方案」提示就没用了。
    if (chatStickToBottom) hideLatestPlanHint();
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
//
// 状态机取值是**内部枚举**（``ANALYZING`` / ``WAITING_USER_CONFIRM``…），
// 直接摆在界面上用户读不懂。这里换成业务说法。
const STATE_LABELS = {
  IDLE: "空闲",
  ANALYZING: "正在生成",
  NEEDS_CLARIFICATION: "等你补充信息",
  BLOCKED_OR_UNSUPPORTED: "当前做不到",
  WAITING_USER_CONFIRM: "等你确认",
  USER_EDITING_TEMPLATE: "等你提修改要求",
  DEFINITION_DRAFT_READY: "草稿已就绪",
  SAVE_DEFINITION_DRAFT: "正在保存草稿",
  VALIDATING: "正在校验",
  VALIDATION_FAILED: "校验没通过",
  PREVIEWING: "正在预览",
  PREVIEW_READY: "预览已就绪",
  RENDERING: "正在渲染",
  READY: "已就绪",
};

/** 状态 → 业务说法。未知取值回落成「处理中」，不把英文枚举露出去。 */
function stateDisplayLabel(state) {
  const key = String(state || "").trim().toUpperCase();
  if (!key) return "空闲";
  return Object.prototype.hasOwnProperty.call(STATE_LABELS, key) ? STATE_LABELS[key] : "处理中";
}

function setState(state) {
  session.state = state;
  const badge = $("state-badge");
  badge.textContent = stateDisplayLabel(state);

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
  badge.className = "inline-flex items-center gap-1 rounded-md text-xs font-medium px-2 py-1 border " + (palette[state] || palette.ANALYZING);
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
  // 顶栏只说业务状态：版本号、会话 id 这类内部标识不摆给用户看。
  // 草稿的版本（编辑链版本号）仍然记在会话状态里，回传时要用它做过期校验 ——
  // 但那是给服务端看的，不是给用户看的。
  $("meta-rev").textContent = session.definition ? "已生成，可继续修改" : "尚未生成";

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
  // ai-bubble / ai-bubble-body：给这一条气泡一个**稳定锚点**。
  // 反馈入口挂在这条气泡里（20261104），不靠猜「最后一个气泡」。
  wrap.className = "ai-bubble flex gap-2.5";
  wrap.innerHTML = `
    <div class="w-8 h-8 shrink-0 rounded-full bg-gradient-to-br from-blue-500 to-blue-700 flex items-center justify-center">
      <iconify-icon class="text-white" icon="mdi:robot-outline"></iconify-icon>
    </div>
    <div class="ai-bubble-body flex-1 min-w-0 space-y-2.5">${innerHTML}</div>`;
  chatScroll.appendChild(wrap);
  chatBubbleObserver.observe(wrap);
  scrollChatBottom();
  return wrap;
}

function appendAiText(text) {
  return aiBubble(`<div class="rounded-xl rounded-tl-sm border border-slate-200 bg-white px-3.5 py-3 text-sm text-slate-700 leading-relaxed">${esc(text)}</div>`);
}

// ===== 反馈入口（20261104 回复评价与反馈收件箱）=====
//
// 纪律（这一段只做「挂接」，不改任何业务行为）：
//   - 反馈 Module 只暴露 attach / refreshSubmitted / destroy；
//   - **不调用** sendMessage / queueAdvance / 停止 / 接受 / 保存；
//   - **不改**报表状态、定义、版本、锁、候选、菜单；
//   - 目标来自**服务端这一轮给的冻结值**，不靠全局最新会话去猜。

function feedbackModule() {
  return window.WorkbenchFeedback || null;
}

/** 已挂上的评价目标（刷新后用它问服务端「这个浏览器提交过哪些」）。 */
const feedbackTargetIds = new Set();

/** 当前对话区里已画出的 AI 气泡数量（用于定位「这一轮的第一条气泡」）。 */
function aiBubbleCount() {
  return chatScroll.querySelectorAll(".ai-bubble").length;
}

/**
 * 给这一轮挂一个反馈入口。
 * @param target 服务端登记好的冻结目标（没有就不挂）。
 * @param sinceIndex 这一轮开始前的气泡数 —— 本轮的第一条气泡就是它。
 */
function mountFeedbackTarget(target, sinceIndex = 0) {
  const module = feedbackModule();
  if (!module || !target || !target.id) return;
  const bubbles = chatScroll.querySelectorAll(".ai-bubble");
  const bubble = bubbles[sinceIndex] || bubbles[bubbles.length - 1];
  if (!bubble) return;
  const body = bubble.querySelector(".ai-bubble-body") || bubble;
  let host = body.querySelector("[data-feedback-host]");
  if (!host) {
    host = document.createElement("div");
    host.className = "feedback-host";
    host.dataset.feedbackHost = "1";
    body.appendChild(host);
  }
  module.attach(host, target);
  feedbackTargetIds.add(target.id);
}

/**
 * 登记一个**客户端事件**（传输失败 / 用户主动停止），拿到评价目标。
 *
 * 服务端能核实的字段自己读；核实不了的只记受限的客户端观察 ——
 * 这里**不**上传任何「后台事实」。
 */
async function registerFeedbackClientEvent(kind, options = {}) {
  const module = feedbackModule();
  if (!module) return null;
  const taskId = options.taskId || session.stoppedTaskId || null;
  const messageId =
    options.messageId || (taskId ? String(taskId).replace(/^task_/, "") : null);
  const clientAttemptId = options.clientAttemptId || messageId || newMessageId();
  try {
    const response = await fetch(API_BASE + "/api/feedback/client-events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({
        kind,
        clientAttemptId,
        sessionId: session.sessionId || undefined,
        messageId: messageId || undefined,
        taskId: taskId || undefined,
        observation: options.observation || undefined,
      }),
    });
    if (!response.ok) return null;
    const target = await response.json();
    return target && target.id ? target : null;
  } catch (err) {
    // 登记失败**不打扰用户**：反馈是辅助能力，不该在这里报错。
    return null;
  }
}

function appendDebugTrace({ text, state, isAdvance, history = false }) {
  const title = history
    ? "历史 Debug Trace"
    : (isAdvance ? "自动推进 Debug Trace" : "请求 Debug Trace");
  // 历史卡片是**回放**出来的，已经结束了 —— 不挂「running」。
  const statusClass = history
    ? "debug-status shrink-0 rounded-full bg-slate-100 text-slate-600 border border-slate-200 text-[10px] px-2 py-0.5"
    : "debug-status shrink-0 rounded-full bg-blue-50 text-blue-700 border border-blue-100 text-[10px] px-2 py-0.5";
  const statusText = history ? "历史" : "running";
  const wrap = aiBubble(`
    <div class="debug-card is-collapsed rounded-xl rounded-tl-sm border border-slate-200 bg-white overflow-hidden">
      <button type="button" data-debug-toggle class="debug-header flex w-full items-center justify-between gap-2 px-3.5 py-2.5 bg-slate-50 border-b border-slate-200 text-left cursor-pointer select-none hover:bg-slate-100 transition-colors" aria-expanded="false">
        <div class="flex items-center gap-2 min-w-0">
          <iconify-icon class="text-slate-500 shrink-0" icon="mdi:timeline-clock-outline"></iconify-icon>
          <span class="text-xs font-semibold text-slate-700 truncate">${esc(title)}</span>
          <span class="${statusClass}">${statusText}</span>
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
  // 最终结果里带着逐轮摘要（与流式事件**同一份数据**）：一并渲染，
  // 这样非流式路径也看得见逐轮过程。
  renderTraceRoundsFromSummary(card, data.traceSummary);

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

// --------------------------------------------------------------------------
// Debug Trace：ReAct 逐轮人话摘要
// --------------------------------------------------------------------------
//
// 「ReAct 第 N 轮」= **一次真实的框架循环**（想 → 调工具 → 收结果）。
// 同一轮里的多个工具调用属于同一轮，并排列出，不编造先后。
//
// 这一块只做**渲染**：正文与数据都来自服务端同一份事件数据
// （流式事件与最终摘要用的是同一份），前端不自己编内容。

// ===== TRACE RENDER PURE BEGIN =====
// 纯函数区：**只拼 HTML 字符串**，不碰 DOM。
//
// 自检脚本按这两个标记把本段截出来，在 node 里直接跑 ——
// 不开浏览器、不起服务、不装依赖。渲染逻辑与浏览器里跑的是同一份代码。
//
// 本段只依赖外层唯一的 `esc`（HTML 转义），其余都是本段内定义。

/** 逐轮行的去重键：同一个 traceId + 同一轮 = 同一行（后来的覆盖先来的）。 */
function traceRoundKey(data) {
  const round = data?.roundKey ?? data?.key ?? data?.round ?? "unknown";
  return `${data?.traceId || ""}::${round}`;
}

// 缺失事实的名字 → 人话（缺什么就说什么，不静悄悄省略）。
// 口径与服务端 MISSING_LABELS 一份。
const TRACE_MISSING_LABELS = {
  itemId: "修改事项标识",
  planId: "候选计划标识",
  iteration: "轮次",
  replyIndex: "回复序号",
  callContext: "调用归属",
  batchId: "试做批次",
  toolCallId: "工具调用 id",
  thinkingSummary: "思考摘要",
  businessResult: "后台结果",
  detailTruncated: "部分内容",
  changes: "前后变化",
  structure: "结构投影",
  effectCheck: "效果核对",
  persistenceCheck: "持久化结果",
};

function traceMissingText(missing) {
  return (missing || [])
    .map((item) => TRACE_MISSING_LABELS[item] || item)
    .join("、");
}

// 历史记录里**根本没有**的字段（旧消息早于这些字段）：明说没记，不补造。
const TRACE_HISTORY_DETAIL_MISSING = "历史 Trace 未记录该详情";
// 服务端自己的「变化细节未取得」口径 —— 空变化用它，不写「已应用一批操作」这种缩略句。
const TRACE_CHANGES_MISSING = "变化细节未取得";

/** 「实际变化」这一层的正文。 */
function traceChangesText(data) {
  const text = data?.changesText;
  if (text === undefined || text === null) return TRACE_HISTORY_DETAIL_MISSING;
  const flat = String(text).trim();
  if (!flat) return TRACE_CHANGES_MISSING;
  return flat;
}

/** 「是否生效」这一层的正文（禁止用 done / 「操作正常返回」代替）。 */
function traceEffectText(data) {
  const text = data?.effectText;
  if (text === undefined || text === null) return TRACE_HISTORY_DETAIL_MISSING;
  const flat = String(text).trim();
  if (!flat) return "是否生效结果未取得";
  return flat;
}

/** 这一轮有没有修订号（没有修订号 ≠ 拒绝更新，只是不知道新旧）。 */
function traceHasRevision(data) {
  const raw = data?.revision;
  if (raw === undefined || raw === null || raw === "") return false;
  return Number.isFinite(Number(raw));
}

/** 这一轮里有没有「归属来源有冲突」的调用。 */
function traceHasAttributionConflict(data) {
  const calls = Array.isArray(data?.calls) ? data.calls : [];
  return calls.some((call) => call?.attribution?.conflict === true);
}

/** 技术细节里的值：对象转成可读 JSON，没拿到就明说没拿到。 */
function traceFormatValue(value) {
  if (value === undefined) return "未取得";
  if (value === null) return "（空）";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value, null, 2);
    } catch (err) {
      return String(value);
    }
  }
  return String(value);
}

/**
 * 一步的状态：人话 + 配色。
 *
 * `notExecuted` 优先 —— 未执行就是未执行，不能显示成成功，也不能显示成「无变化」。
 */
function traceStepStatus(step) {
  if (step?.notExecuted === true) return { kind: "skip", text: "未执行" };
  const text = String(step?.statusText || "").trim();
  if (text) {
    if (text === "通过") return { kind: "ok", text };
    if (text === "未通过") return { kind: "fail", text };
    if (text === "未执行") return { kind: "skip", text };
    return { kind: "unknown", text };
  }
  if (step?.ok === true) return { kind: "ok", text: "通过" };
  if (step?.ok === false) return { kind: "fail", text: "未通过" };
  return { kind: "unknown", text: "结果未取得" };
}

/**
 * 把一次调用的步骤按**试做批次**分组。
 *
 * 批次 id 是技术信息 → 正文只说「第 1 次试做 / 第 2 次试做」；
 * 两次试做各自的「第 1 步」绝不混成一步。
 */
function traceStepGroups(call) {
  const steps = Array.isArray(call?.steps) ? call.steps : [];
  const order = [];
  const byBatch = new Map();
  const addBatch = (batchId) => {
    if (byBatch.has(batchId)) return byBatch.get(batchId);
    const group = { batchId, label: "", steps: [] };
    byBatch.set(batchId, group);
    order.push(group);
    return group;
  };
  (Array.isArray(call?.batchIds) ? call.batchIds : []).forEach((raw) => addBatch(String(raw || "")));
  steps.forEach((step) => {
    const batchId = String(step?.batchId || "");
    if (batchId) addBatch(batchId);
  });
  const loose = { batchId: "", label: "", steps: [] };
  steps.forEach((step) => {
    const batchId = String(step?.batchId || "");
    const group = batchId ? byBatch.get(batchId) : null;
    if (group) group.steps.push(step);
    else loose.steps.push(step);
  });
  order.forEach((group, i) => { group.label = `第 ${i + 1} 次试做`; });
  const filled = order.filter((group) => group.steps.length);
  if (loose.steps.length) {
    loose.label = filled.length ? "未标批次的试做" : "第 1 次试做";
    filled.push(loose);
  }
  return filled;
}

/** 一个试做批次里的步骤。 */
function renderTraceStepGroup(group) {
  return `
    <div class="debug-round-batch">
      <div class="debug-round-batch-title">${esc(group.label)}</div>
      ${group.steps.map(renderTraceStep).join("")}
    </div>`;
}

/** 一步：序号 + 可见名称 + 状态 + 这一步的实际变化。 */
function renderTraceStep(step) {
  const rawIndex = Number(step?.index);
  const index = Number.isFinite(rawIndex) && rawIndex > 0 ? rawIndex : null;
  const name = String(step?.name || "").trim() || "名称未取得";
  const status = traceStepStatus(step);
  const change = String(step?.changeText || "").trim();
  const reason = String(step?.reason || "").trim();
  const constraint = String(step?.constraint || "").trim();
  // 包装能力里的**子操作**：与它的父步骤不是同一件事 ——
  // 标出来，别让人以为重复执行了一遍。
  const isSub = step?.kind === "substep";
  const subIndex = Number.isFinite(step?.subStepIndex) ? Number(step.subStepIndex) + 1 : null;
  const stepLabel = isSub
    ? `子步骤${subIndex ? ` ${subIndex}` : ""}`
    : (index ? `第 ${index} 步` : "步骤");
  return `
    <div class="debug-round-step${isSub ? " is-substep" : ""}">
      <div class="debug-round-step-head">
        <span class="debug-round-step-index">${esc(stepLabel)}</span>
        <span class="debug-round-step-name">${esc(name)}</span>
        <span class="debug-round-step-status is-${status.kind}">${esc(status.text)}</span>
      </div>
      <div class="debug-round-step-change${change ? "" : " is-empty"}">${esc(change || "该步变化未取得")}</div>
      ${reason ? `<div class="debug-round-step-reason">原因：${esc(reason)}</div>` : ""}
      ${constraint ? `<div class="debug-round-step-reason">约束：${esc(constraint)}</div>` : ""}
    </div>`;
}

/**
 * 一个调用块：标题用**可见说法**（toolCallId 只进技术细节）。
 *
 * 服务端给了 ``label``（人话说法，如「第 1 次调用（提交了修改计划）」）就用它；
 * 没有就退回原始工具名 —— 但原始工具名只当**兜底**，不是首选。
 */
function renderTraceCallBlock(call) {
  const label = String(call?.label || "").trim();
  const name = String(call?.name || "").trim();
  const title = label || name || "工具名称未取得";
  const hasSteps = Array.isArray(call?.steps);
  let body;
  if (!hasSteps) {
    body = `<div class="debug-round-step-change is-empty">${esc(TRACE_HISTORY_DETAIL_MISSING)}</div>`;
  } else {
    const groups = traceStepGroups(call);
    body = groups.length
      ? groups.map(renderTraceStepGroup).join("")
      : `<div class="debug-round-step-change is-empty">该调用没有步骤记录</div>`;
  }
  return `
    <div class="debug-round-call">
      <div class="debug-round-call-name">${esc(title)}</div>
      ${body}
    </div>`;
}

/**
 * 一轮里的**并列**调用块。
 *
 * 同一轮的多次调用并排摆出来 —— **不编先后**（不写「先…后…」）。
 */
function renderTraceRoundCalls(data) {
  const calls = data?.calls;
  if (!Array.isArray(calls)) {
    return `
    <div class="debug-round-calls mt-1">
      <div class="debug-round-calls-title">本轮调用（并列）</div>
      <div class="debug-round-calls-note">${esc(TRACE_HISTORY_DETAIL_MISSING)}</div>
    </div>`;
  }
  if (!calls.length) {
    return `
    <div class="debug-round-calls mt-1">
      <div class="debug-round-calls-title">本轮调用（并列）</div>
      <div class="debug-round-calls-note">本轮没有并列调用记录</div>
    </div>`;
  }
  return `
    <div class="debug-round-calls mt-1">
      <div class="debug-round-calls-title">本轮调用（并列）</div>
      <div class="debug-round-call-grid">${calls.map(renderTraceCallBlock).join("")}</div>
    </div>`;
}

/**
 * 一轮的五层正文：
 * 公开决策摘要 / 实际调用及提交计划 / 后台逐步结果 / 实际变化 / 是否生效。
 */
function renderTraceRoundLayers(data) {
  const thinking = data?.thinking || "思考摘要未提供";
  const action = data?.action || "实际行动未取得";
  const backend = data?.backend || "后台结果未取得";
  // 「根据实际调用/提交计划整理」这层标签**只在真的整理出来时**出现 ——
  // 没整理出来就说没整理出来，不挂一个空标签。
  const actionLabel = data?.actionSource === "missing"
    ? "实际调用及提交计划"
    : "实际调用及提交计划（根据实际调用/提交计划整理）";
  const layers = [
    ["公开决策摘要", thinking, ""],
    [actionLabel, action, ""],
    ["后台逐步结果", backend, ""],
    ["实际变化", traceChangesText(data), " is-changes"],
    ["是否生效", traceEffectText(data), " is-effect"],
  ];
  const missing = Array.isArray(data?.missing) ? data.missing : [];
  const missingNote = missing.length
    ? `<div class="mt-0.5 text-amber-600">未取得：${esc(traceMissingText(missing))}</div>`
    : "";
  return `
    ${layers.map(([label, value, cls]) => `
      <div class="debug-round-layer mt-1${cls}">
        <span class="debug-round-label">${esc(label)}</span>
        <span class="debug-round-value">${esc(value)}</span>
      </div>`).join("")}
    ${renderTraceRoundCalls(data)}
    ${data?.draftChangedText ? `<div class="mt-0.5 text-slate-500">${esc(data.draftChangedText)}</div>` : ""}
    ${missingNote}`;
}

/** 一轮的标题条：轮次 + 阶段说明 + 是否被截断。 */
function renderTraceRoundHeader(data) {
  const label = data?.roundLabel || "ReAct 轮次未取得";
  const phase = data?.phaseLabel || "";
  const badge = phase
    ? `<span class="debug-round-phase">${esc(phase)}</span>`
    : "";
  const truncated = data?.truncated === true
    ? `<span class="debug-round-truncated">内容被截断</span>`
    : "";
  return `
    <div class="flex items-start justify-between gap-2">
      <span class="font-semibold text-slate-700">${esc(label)}</span>
      <div class="flex items-center gap-1.5 shrink-0">${badge}${truncated}</div>
    </div>`;
}

/** 技术细节：「逐调用前后变化」—— 每条按调用分组，前后值折叠（可能很大）。 */
function renderTraceTechnicalChanges(items) {
  if (!Array.isArray(items) || !items.length) return "";
  const blocks = items.map((item) => {
    const changes = Array.isArray(item?.changes) ? item.changes : [];
    const callId = String(item?.toolCallId || item?.callKey || "");
    const rows = changes.map((change) => {
      const path = String(change?.path || "");
      const summary = String(change?.summary || "");
      return `
        <details class="debug-round-tech-item">
          <summary class="cursor-pointer select-none text-blue-600">${esc(path || "路径未取得")}${summary ? ` · ${esc(summary)}` : ""}</summary>
          <div class="debug-round-tech-item-body">
            <div class="debug-round-tech-kv">
              <span class="debug-round-tech-key">before</span>
              <pre class="debug-round-tech-pre">${esc(traceFormatValue(change?.before))}</pre>
            </div>
            <div class="debug-round-tech-kv">
              <span class="debug-round-tech-key">after</span>
              <pre class="debug-round-tech-pre">${esc(traceFormatValue(change?.after))}</pre>
            </div>
          </div>
        </details>`;
    }).join("");
    return `
      <details class="debug-round-tech-item">
        <summary class="cursor-pointer select-none text-blue-600">调用 ${esc(callId || "未取得")} · ${changes.length} 处变化</summary>
        <div class="debug-round-tech-item-body">${rows || "该调用没有记录前后变化"}</div>
      </details>`;
  }).join("");
  return `
    <div class="debug-round-tech-group">
      <div class="debug-round-tech-group-title">逐调用前后变化</div>
      ${blocks}
    </div>`;
}

/** 技术细节：「结构投影」（affected / containers）。 */
function renderTraceTechnicalStructure(items) {
  if (!Array.isArray(items) || !items.length) return "";
  const blocks = items.map((item) => {
    const structure = item?.structure && typeof item.structure === "object" ? item.structure : {};
    const affected = Array.isArray(structure.affected) ? structure.affected : [];
    const containers = Array.isArray(structure.containers) ? structure.containers : [];
    const callKey = String(item?.callKey || "");
    return `
      <details class="debug-round-tech-item">
        <summary class="cursor-pointer select-none text-blue-600">调用 ${esc(callKey || "未取得")} · 受影响 ${affected.length} · 容器 ${containers.length}</summary>
        <div class="debug-round-tech-item-body">
          <div class="debug-round-tech-kv">
            <span class="debug-round-tech-key">affected</span>
            <pre class="debug-round-tech-pre">${esc(traceFormatValue(affected))}</pre>
          </div>
          <div class="debug-round-tech-kv">
            <span class="debug-round-tech-key">containers</span>
            <pre class="debug-round-tech-pre">${esc(traceFormatValue(containers))}</pre>
          </div>
        </div>
      </details>`;
  }).join("");
  return `
    <div class="debug-round-tech-group">
      <div class="debug-round-tech-group-title">结构投影</div>
      ${blocks}
    </div>`;
}

/** 技术细节：调用归属明细（含「归属来源有冲突」标记）。 */
function renderTraceTechnicalAttribution(data) {
  const calls = Array.isArray(data?.calls) ? data.calls : [];
  if (!calls.length) return "";
  const rows = calls.map((call) => {
    const attribution = call?.attribution && typeof call.attribution === "object" ? call.attribution : {};
    const parts = [
      `toolCallId=${String(call?.toolCallId || "") || "未取得"}`,
      `replyIndex=${attribution.replyIndex ?? "未取得"}`,
      `iteration=${attribution.iteration ?? "未取得"}`,
    ];
    if (attribution.conflict === true) parts.push("归属来源有冲突");
    return `<div class="debug-round-tech-line">${esc(parts.join(" · "))}</div>`;
  }).join("");
  return `
    <div class="debug-round-tech-group">
      <div class="debug-round-tech-group-title">调用归属明细</div>
      ${rows}
    </div>`;
}

/**
 * 一轮的技术细节（可展开）。
 *
 * 能力名、参数名、id、错误码都在这里 —— 正文只给人话。
 */
function renderTraceRoundTechnical(data, options = {}) {
  const technical = data?.technical && typeof data.technical === "object" ? data.technical : null;
  const notes = [];
  if (!traceHasRevision(data)) notes.push("修订号未取得");
  if (traceHasAttributionConflict(data)) notes.push("归属来源有冲突");
  const lines = [];
  if (technical) {
    Object.entries(technical)
      .filter(([key, value]) => key !== "逐调用前后变化" && key !== "结构投影")
      .filter(([, value]) => value !== undefined && value !== null && value !== "")
      .forEach(([key, value]) => {
        const display = typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
        lines.push(`${key}:\n${display}`);
      });
  }
  const changesBlock = renderTraceTechnicalChanges(technical?.["逐调用前后变化"]);
  const structureBlock = renderTraceTechnicalStructure(technical?.["结构投影"]);
  const attributionBlock = renderTraceTechnicalAttribution(data);
  if (!notes.length && !lines.length && !changesBlock && !structureBlock && !attributionBlock) return "";
  return `
    <details class="debug-round-tech mt-1 text-slate-500"${options.open ? " open" : ""}>
      <summary class="cursor-pointer select-none text-blue-600">技术细节</summary>
      <div class="mt-1 space-y-2">
        ${notes.length ? `<div class="debug-round-tech-note">${esc(notes.join("；"))}</div>` : ""}
        ${lines.length ? `<pre class="debug-round-tech-pre mt-1 whitespace-pre-wrap break-all text-slate-600">${esc(lines.join("\n"))}</pre>` : ""}
        ${changesBlock}
        ${structureBlock}
        ${attributionBlock}
      </div>
    </details>`;
}

/**
 * 一轮的完整正文（标题 + 五层 + 并列调用 + 技术细节）—— 纯函数。
 *
 * 自检脚本直接调它；DOM 更新（addTraceRoundRow）也调它 —— 同一份渲染。
 */
function renderTraceRoundBody(data, options = {}) {
  return `
    ${renderTraceRoundHeader(data)}
    ${renderTraceRoundLayers(data)}
    ${renderTraceRoundTechnical(data, { open: options.techOpen === true })}
  `;
}

// 自检出口：把这几个纯函数挂出去，node 自检脚本按同一份实现跑。
if (typeof window !== "undefined") {
  window.WorkbenchTraceRender = {
    traceRoundKey,
    traceMissingText,
    traceChangesText,
    traceEffectText,
    traceStepStatus,
    traceStepGroups,
    renderTraceRoundBody,
    renderTraceRoundLayers,
    renderTraceRoundCalls,
    renderTraceRoundTechnical,
  };
}

// ===== TRACE RENDER PURE END =====

/**
 * 渲染（或更新）一行「ReAct 第 N 轮」。
 *
 * 同一轮会随着事件到达被**多次**推送 → 这里按 key **更新**同一行，
 * 不追加重复行。
 *
 * 两道保护：
 *   - **请求隔离**：traceId / messageId 与本卡片不一致 → 整条忽略
 *     （别的事件流 / 别的消息的轮次不串进这张卡）。
 *   - **旧修订保护**：同一轮的修订号比已存的**旧**修订号小 → 忽略这次更新，
 *     不让旧内容盖掉新内容。修订号缺失按「无修订信息」处理 ——
 *     **不**因为缺修订号就拒绝更新（旧数据没有这个字段）。
 */
function addTraceRoundRow(card, data) {
  if (!card || !data) return;
  if (!card._roundRows) card._roundRows = new Map();
  if (!card._roundRevisions) card._roundRevisions = new Map();
  // 请求隔离：这条事件是不是**本卡片**那条 trace 的？
  const traceId = String(data.traceId || "");
  if (traceId && card._traceId && card._traceId !== traceId) return;
  if (traceId) card._traceId = traceId;
  // 请求隔离：这条轮次是不是**本卡片**那条消息的？
  const messageId = String(data.messageId || "");
  if (messageId && card._messageId && card._messageId !== messageId) return;
  if (messageId) card._messageId = messageId;

  const key = traceRoundKey(data);
  // 旧修订保护：更小的修订号 = 更旧的内容 → 忽略，不覆盖。
  const hasRevision = traceHasRevision(data);
  const incoming = hasRevision ? Number(data.revision) : null;
  const stored = card._roundRevisions.get(key);
  if (stored !== undefined && hasRevision && incoming < stored) return;

  let row = card._roundRows.get(key);
  if (!row) {
    row = document.createElement("div");
    row.className = "debug-round px-3.5 py-2 text-[11px] leading-relaxed";
    card.querySelector(".debug-rows").appendChild(row);
    if (card._rowObserver) card._rowObserver.observe(row);
    card._roundRows.set(key, row);
  }
  const wasOpen = row.querySelector("details")?.open ?? false;
  row.innerHTML = renderTraceRoundBody(data, { techOpen: wasOpen });
  if (hasRevision) {
    card._roundRevisions.set(key, stored === undefined ? incoming : Math.max(stored, incoming));
  }
  refreshDebugCard(card);
  scrollDebugRows(card);
  scrollChatBottom();
}

/** 把最终摘要里的逐轮数据也渲染出来（非流式路径 / final 事件 / 历史回放）。 */
function renderTraceRoundsFromSummary(card, summary) {
  const rounds = summary?.rounds;
  if (!Array.isArray(rounds)) return;
  rounds.forEach((round) => {
    addTraceRoundRow(card, {
      ...round,
      traceId: round.traceId || summary.traceId || "",
      messageId: round.messageId || summary.messageId || "",
    });
  });
}

/**
 * 历史回放：把这一轮**存下来的**逐轮摘要画成一张 Debug Trace 卡片。
 *
 * 走的是**同一套**渲染（renderTraceRoundsFromSummary → addTraceRoundRow）——
 * 历史与流式看到的是同一种正文。旧消息没有的字段由渲染函数自己说
 * 「历史 Trace 未记录该详情」，**不**补造轮次或结构。
 */
function appendTraceHistoryCard(payload) {
  const summary = payload?.debug?.traceSummary;
  const rounds = summary?.rounds;
  if (!Array.isArray(rounds) || !rounds.length) return null;
  const card = appendDebugTrace({
    text: payload?.reply || "",
    state: payload?.state || "",
    isAdvance: false,
    history: true,
  });
  renderTraceRoundsFromSummary(card, summary);
  return card;
}

// --------------------------------------------------------------------------
// 「其他」：问题卡片里的自由文本（这是**这张卡片**的回答）
// --------------------------------------------------------------------------
//
// 服务端在菜单上给 allowsOther=true 时，这张卡片除了选项列表，还提供
// 一个「其他」入口与一个**卡片内**的输入框：用户在里面写的内容按
// **这张卡片的答案（纠正）**处理 —— 承接上文的同一个待办，不是另一条
// 无关消息（需求采访第二轮第 2、3 条）。
//
// 「其他」是下拉入口，选择后才展开输入；空内容不提交。
//
// 提交走既有的 sendMessage 通道，并带 fromChoice —— 那就是「这是该卡片的
// 回答」的既有标识。后端按「消息文本不在选项里 + 这张卡允许其他」把它
// 认成纠正，**不新增协议字段**。
//
// 输入框的启用状态跟着**当前待答的那张卡**走（见 syncInputControls）：
// 已经答过的卡、历史回放出来的卡，输入框是灰的 —— 只有当前那张待答卡
// 能写。刷新之后由服务端 pending_choice 恢复，同样只开当前那张。
// --------------------------------------------------------------------------

/** 「其他」这一项的显示文案（服务端没给就用默认的「其他」）。 */
function otherAnswerLabel(question) {
  const label = String(question?.otherLabel || "").trim();
  return label || "其他";
}

/** 卡片内输入框的提示文案（服务端没给就不写提示）。 */
function otherAnswerPlaceholder(question) {
  return String(question?.otherPlaceholder || "").trim();
}

const OTHER_ANSWER_VALUE = "__question_other__";

function inlineAnswerAction(question, value) {
  const option = question?.options?.find((item) => String(item.value ?? item.label) === value);
  const label = String(option?.label ?? value);
  if (question?.allowsOther === true &&
      (value === OTHER_ANSWER_VALUE || label === otherAnswerLabel(question) || /^(其他|OTHER)$/i.test(value))) {
    return "other";
  }
  if (question?.inlineEdit === true && /^(修改|继续修改|继续调整)$/.test(label)) return "edit";
  return "";
}

function renderQuestionOptions(question) {
  const options = (question.options || []).filter((option) => {
    const value = String(option.value ?? option.label);
    const label = String(option.label ?? option.value);
    return value !== OTHER_ANSWER_VALUE && !/^(其他|OTHER)$/i.test(value.trim()) &&
      !/^(其他|OTHER)$/i.test(label.trim()) && inlineAnswerAction(question, value) !== "other";
  });
  return options.map((option) => `<option value="${esc(option.value ?? option.label)}">${esc(option.label ?? option.value)}</option>`).join("");
}

function bindQuestionSelects(container, questions) {
  container.querySelectorAll(".question-select").forEach((select) => {
    const question = questions.find((item) => item.id === select.dataset.questionId);
    if (!question) return;
    const box = Array.from(container.querySelectorAll(".question-other")).find((item) => item.dataset.questionOther === question.id);
    if (box) box._questionSelect = select;
    select.addEventListener("change", () => {
      if (select.disabled || isBusy() || lockedChoice?.id !== question.id) return;
      const mode = inlineAnswerAction(question, select.value);
      if (box) {
        box.dataset.answerMode = mode;
        box.dataset.editAction = mode === "edit" ? select.value : "";
        box.classList.toggle("hidden", !mode);
        const label = box.querySelector(".question-other-label");
        if (label) label.textContent = mode === "edit" ? "修改要求" : otherAnswerLabel(question);
        const input = box.querySelector(".question-other-input");
        if (input) input.placeholder = mode === "edit" ? "请填写具体修改要求" : otherAnswerPlaceholder(question);
      }
      syncInputControls();
      if (mode) box?.querySelector(".question-other-input")?.focus();
      else if (select.value) sendMessage(select.value, { fromChoice: true });
    });
  });
}

/** Dropdown text entry is only available through the edit action. */
function renderOtherAnswerHtml(question) {
  if (question?.inlineEdit !== true) return "";
  return `
    <div class="question-other hidden mt-2 pt-2 border-t border-amber-100" data-question-other="${esc(question.id || "")}">
      <div class="question-other-label text-[11px] text-amber-800 mb-1">${esc(otherAnswerLabel(question))}</div>
      <div class="flex items-center gap-2">
        <input type="text" disabled class="question-other-input flex-1 min-w-0 rounded-md border border-amber-200 bg-white px-2 py-1.5 text-xs text-slate-700 outline-none focus:border-blue-400 focus:ring-1 focus:ring-blue-100" placeholder="${esc(otherAnswerPlaceholder(question))}" />
        <button type="button" disabled class="question-other-submit shrink-0 rounded-md border border-amber-200 bg-amber-50 hover:bg-amber-100 text-amber-800 text-xs px-2 py-1.5">提交</button>
      </div>
      <div class="question-other-hint hidden text-[11px] text-amber-700 mt-1"></div>
    </div>`;
}

/** 「其他」输入框下面那行提示（说清为什么没提交上去）。 */
function showOtherAnswerHint(hint, text) {
  if (!hint) return;
  hint.textContent = text;
  hint.classList.remove("hidden");
}

/**
 * 「其他」输入框的交互。
 *
 * - Enter **只**提交这张卡的文本：事件在输入框上拦下，不冒泡成页面级发送；
 * - 提交后清空输入框、禁用输入框与提交按钮：这张卡已经答过；
 *   服务端给出新菜单时整张卡会被换掉。
 *
 * 同一段里也接**文字型待决**的专用输入（``.question-text``）：
 * 服务端说这一步要一段文字时，合法入口就是卡片里那个输入框 ——
 * 底部普通聊天框在这一步锁着（契约 §3.3 / BUG-009）。
 */
function bindOtherAnswers(container) {
  const boxes = container?.querySelectorAll?.(".question-other") || [];
  Array.prototype.forEach.call(boxes, (box) => {
    const questionId = String(box.dataset?.questionOther || "");
    const input = box.querySelector(".question-other-input");
    const submit = box.querySelector(".question-other-submit");
    const hint = box.querySelector(".question-other-hint");
    if (!input || !submit) return;

    const submitAnswer = async () => {
      if (input.disabled || submit.disabled || isBusy() || !box.dataset.answerMode || box._questionSelect?.disabled) return;
      const text = String(input.value || "").trim();
      if (!text) {
        showOtherAnswerHint(hint, "先写下你的想法，再提交。");
        input.focus();
        return;
      }
      // 只有**当前待答**的那张卡才认这个输入框：已经答过的卡与历史回放
      // 出来的卡都不该把答案发到现在的待办上。
      if (lockedChoice?.id !== questionId) {
        showOtherAnswerHint(hint, "请先回答当前待确认的那张卡片。");
        return;
      }
      if (hint) hint.classList.add("hidden");
      // 这张卡已经答过：先禁掉，别重复提交。
      input.disabled = true;
      submit.disabled = true;
      input.classList.add("opacity-50");
      submit.classList.add("opacity-50");
      // fromChoice = 这是**这张卡片**的回答（不是另一条普通聊天）。
      const epoch = requestEpoch;
      const result = box.dataset.answerMode === "edit"
        ? (lockedChoice.inlineEditStarted
          ? await sendMessage(text, { fromChoice: true, fromInline: true })
          : await sendMessage(box.dataset.editAction, { fromChoice: true, inlineEditText: text }))
        : await sendMessage(text, { fromChoice: true, fromOther: true });
      if (epoch !== requestEpoch) return;
      if (result && !result.error) input.value = "";
      syncInputControls();
    };

    submit.addEventListener("click", submitAnswer);
    input.addEventListener("keydown", (event) => {
      // 卡片内的按键不冒泡：Enter 只提交这张卡的文本。
      event.stopPropagation();
      if (event.key !== "Enter" || event.shiftKey) return;
      event.preventDefault();
      submitAnswer();
    });
  });

  // 文字型待决的专用输入：Enter 与提交按钮都只提交这张卡的文本。
  const textBoxes = container?.querySelectorAll?.(".question-text") || [];
  Array.prototype.forEach.call(textBoxes, (box) => {
    const questionId = String(box.dataset?.questionText || "");
    const input = box.querySelector(".question-text-input");
    const submit = box.querySelector(".question-text-submit");
    const hint = box.querySelector(".question-text-hint");
    if (!input || !submit) return;

    const submitAnswer = async () => {
      if (input.disabled || submit.disabled || isBusy()) return;
      const text = String(input.value || "").trim();
      if (!text) {
        showOtherAnswerHint(hint, "先写下你的想法，再提交。");
        input.focus();
        return;
      }
      // 只有**当前待答**的那张卡才认这个输入框。
      if (lockedChoice?.id !== questionId) {
        showOtherAnswerHint(hint, "请先回答当前待确认的那张卡片。");
        return;
      }
      if (hint) hint.classList.add("hidden");
      input.disabled = true;
      submit.disabled = true;
      const epoch = requestEpoch;
      // fromQuestionText：这一段文字就是**这一步**的答案（澄清待决的专用入口）。
      const result = await sendMessage(text, { fromChoice: true, fromQuestionText: true });
      if (epoch !== requestEpoch) return;
      if (result && !result.error) input.value = "";
      syncInputControls();
    };

    submit.addEventListener("click", submitAnswer);
    input.addEventListener("keydown", (event) => {
      // 卡片内的按键不冒泡：Enter 只提交这张卡的文本。
      event.stopPropagation();
      if (event.key !== "Enter" || event.shiftKey) return;
      event.preventDefault();
      submitAnswer();
    });
  });
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
  question = { ...question, inlineEdit: true };
  // 步骤身份挂到卡片上：撤销之后要按它把「被撤销那一步」标出来
  // （契约 §2.3 第 2 条：旧步骤失效，但消息文本保留）。
  const stepAttr = question.stepId ? ` data-step-id="${esc(question.stepId)}"` : "";
  const wrap = aiBubble(`
    <div class="rounded-xl rounded-tl-sm border border-slate-200 bg-white px-3.5 py-3"${stepAttr}>
      <label class="block text-sm text-slate-700 leading-relaxed mb-2.5">${esc(question.prompt || "请先选择如何处理当前草稿：")}</label>
      <select class="question-select w-full rounded-md border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700" data-question-id="${esc(question.id || "")}">
        <option value="">请选择</option>
        ${renderQuestionOptions(question)}
      </select>
      ${renderOtherAnswerHtml(question)}
    </div>
  `);
  if (lock) setChoiceLock(question);
  else businessMenuReplayed = true;
  bindQuestionSelects(wrap, [question]);
  bindOtherAnswers(wrap);
  syncInputControls();
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
    // 锁的范围跟着菜单一起走（契约表 §5）：归一化不能把锁弄丢。
    lockScope: question.lockScope,
    // 「其他」也一起带过去：归一化不能把卡片内自由文本的入口弄丢。
    allowsOther: question.allowsOther === true,
    otherLabel: question.otherLabel,
    otherPlaceholder: question.otherPlaceholder,
    // 步骤身份也要一起带过去：归一化不能把「这一步是谁」弄丢 ——
    // 丢了，下一次提交就关联不上，后端会拒。
    stepId: question.stepId,
    stepVersion: question.stepVersion,
    stepKind: question.stepKind,
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

/**
 * 只读回答之后把**受限步骤**的锁接回去。
 *
 * 只读回答**不结束**这一步（契约 §3.3：「返回了一段文字」不是解锁理由）：
 * 用户在受限输入里问了一句只读问题，普通输入必须**继续锁着**，合法入口
 * 仍是那张卡的专用输入 —— 否则这一步就有两个入口，用户能从普通输入
 * 绕过这一步的约束。
 *
 * 服务端把当前那一步带在响应里（questions）：按它上锁。屏幕上已经有
 * 这张卡就**只上锁、不重画**（appendAiQuestions 内部按 stepId 去重）；
 * 响应里没带就问服务端要一句（syncChoiceLockFromServer）。
 */
function restoreStepLockAfterReadOnly(data) {
  const step = (data?.questions || []).find((q) => q?.stepId);
  if (step) {
    setChoiceLock(step);
    syncInputControls();
    return null;
  }
  if (typeof syncChoiceLockFromServer === "function") {
    return syncChoiceLockFromServer();
  }
  return null;
}

/**
 * 屏幕上有没有**同一步骤**（stepId 相同）的卡。
 *
 * 同一步骤的卡只应画一张：受限修改输入那一步，用户在卡里提了一句要
 * 信息的话，服务端回了一段说明、这一轮**没有**给出新菜单，但这一步
 * **还在**（服务端 pending_choice 仍记着它）。服务端把这张卡带回响应
 * 时，前端再画一张，用户看到的就是「回答完了又跳出一张一模一样的
 * 『请描述你想怎样修改当前草稿』」（用户现场截图）。
 *
 * 用**步骤身份**而不是 question.id：同一轮里澄清菜单与专用输入卡共用
 * 一个 stepId，那是**同一步**的两张卡（两张的控件都要开着）。
 */
function renderedStepCard(stepId, questionKey = null) {
  const id = String(stepId || "").trim();
  if (!id) return null;
  const cards = document.querySelectorAll("[data-step-id]");
  for (const card of cards) {
    if (card.getAttribute("data-step-id") === id &&
        (questionKey === null || card.getAttribute("data-step-question") === questionKey)) return card;
  }
  return null;
}

function appendAiQuestions(questions, { lock = true } = {}) {
  let list = (questions || []).filter(Boolean);
  // Older responses attach a separate edit input to the clarification menu.
  const editInput = list.find((q) => q.stepKind === "edit_request" && !q.options?.length);
  const choice = list.find((q) => q !== editInput && q.options?.length && q.selectionOnly);
  if (editInput && choice) {
    list = list.filter((q) => q !== editInput).map((q) => q === choice ? {
      ...editInput, ...q, id: editInput.id, stepKind: editInput.stepKind,
      stepId: editInput.stepId, stepVersion: editInput.stepVersion,
      stepDraftId: editInput.stepDraftId, allowedSubmissions: editInput.allowedSubmissions,
      lockScope: editInput.lockScope, allowsOther: true,
      otherLabel: "其他", otherPlaceholder: editInput.otherPlaceholder,
    } : q);
  }
  // 没有问题就别画一张空卡：空卡只有边框没有内容，用户看起来像卡住了。
  if (!list.length) {
    if (lock) appendAiText("暂时没有需要补充的信息，请直接描述你的报表需求。");
    return;
  }
  // 回答方式 → 业务说法。
  //
  // 原来这里直接渲染 `q.expects`（`SINGLE_CHOICE` / `TEXT` …）——
  // 那是内部枚举，用户读不懂。改成用户看得懂的说法：
  //   - 已知取值 → 一句话说明这里要填什么；
  //   - 未知取值 → **不回落英文 key**（英文 key 正是要藏的东西），不显示这一行。
  const EXPECTS_LABELS = {
    SINGLE_CHOICE: "选一项",
    MULTI_CHOICE: "可以选多项",
    TEXT: "直接填写",
    DATE_RANGE: "选择日期范围",
  };
  const expectsLabel = (raw) => {
    const key = String(raw == null ? "" : raw).trim().toUpperCase();
    if (!key) return "";
    return Object.prototype.hasOwnProperty.call(EXPECTS_LABELS, key) ? EXPECTS_LABELS[key] : "";
  };
  // 待答的那张卡（要抢选择锁的那一张）。
  //
  // 「文字型待决」（服务端说「直接填写」）也是待决的一种：合法入口就是
  // 卡片里那个**专用输入**，普通聊天框在这一步锁着（契约 §3.3 / BUG-009）。
  //
  // 判据只看两件事：**没有选项** + **expects=TEXT**。
  //
  // 为什么不再看 allowsOther：服务端给的澄清卡（template_edit_request /
  // template_edit_clarification）都是「TEXT + allowsOther + 无选项」。
  // 这种卡如果按 allowsOther 走「其他」那条路，卡片上就只有一句
  // 「直接填写」和一个下拉框，**下面没有可写的控件** —— 这正是 BUG-009
  // 截图里的现象（澄清卡开着，普通输入也开着，卡片内却没地方填）。
  // 所以这一类一律画专用输入，并且**不再**画下拉框与「其他」块：
  // 这一步只有「写一段文字」一个入口，画两个入口会让人以为可以不写
  // 文字直接提交。
  //
  // 判据就地写成表达式：不调用 expectsFreeText —— 这段函数会被既有单测
  // 单独抽进沙箱跑，沙箱里只有它原有的依赖。
  const isTextOnly = (q) =>
    !q?.options?.length &&
    String(q?.expects || "").trim().toUpperCase() === "TEXT";
  const required =
    // 受限修改输入那一步（stepKind=edit_request）优先。
    //
    // 为什么：服务端存 pending_choice 时用的是同一个口径
    // （backend/app/services/agent_service.py：先取 stepKind==edit_request
    // 的那张，再取 selectionOnly 的菜单）。模型路径可能一轮里同时给
    // 一张澄清卡和那一步本身，锁必须落在**权威那一张**上 —— 否则刷新后
    // （按 pending_choice 恢复）可填的那张卡会跟刷新前不是同一张。
    list.find((q) => q?.stepKind === "edit_request") ||
    list.find(
      (q) => ((q.selectionOnly || q.allowsOther) && (q.options?.length || q.allowsOther)) || isTextOnly(q)
    );
  // The same edit step can move from text input to a new choice menu.
  const questionKey = (q) => JSON.stringify([
    q.id, q.expects, q.prompt || q.text || "", q.options || [], q.stepVersion,
  ]);
  const toDraw = list.filter((q) =>
    !q.stepId || !renderedStepCard(q.stepId, questionKey(q))
  );
  if (!toDraw.length) {
    // 全是屏幕上已有的那几张：不重画，但**锁要接着**（这一步还没结束，
    // 普通输入继续锁着，专用输入仍然可填 —— 契约 §3.3）。
    if (required && lock) setChoiceLock(required);
    syncInputControls();
    return;
  }
  const html = toDraw
    .map((q) => {
      // 文字型待决只画专用输入（下拉框那一支见下面 useSelect 的说明）。
      const useSelect = !isTextOnly(q) && (q.selectionOnly || q.allowsOther);
      let controls = "";
      if (useSelect && (q.options?.length || q.allowsOther)) {
        controls = `
          <div class="question-select-wrap flex items-center gap-2 mt-2">
            <select class="question-select flex-1 min-w-0 rounded-md border border-amber-200 bg-white px-2 py-1.5 text-xs text-slate-700" data-question-id="${esc(q.id || "")}">
              <option value="">请选择</option>
              ${renderQuestionOptions(q)}
            </select>
            ${q.allowRestart ? `<button class="question-restart shrink-0 text-xs text-amber-800 underline" type="button" data-question-id="${esc(q.id || "")}">重新开始</button>` : ""}
          </div>`;
      } else if (q.options?.length) {
        controls = `<div class="flex flex-wrap gap-1 mt-2">${q.options
          .map(
            (o) =>
              `<button class="opt-btn text-[11px] bg-amber-50 hover:bg-amber-100 text-amber-800 border border-amber-200 rounded px-1.5 py-0.5" data-answer="${esc(o.value || o.label)}">${esc(o.label)}</button>`
          )
          .join("")}</div>`;
      }
      // 文字型待决的**专用输入**：没有它，澄清卡开着却只能去底部普通
      // 输入框打字 —— 那正是 BUG-009 截图里的现象。
      //
      // 输入框上面那行字用服务端给的 otherLabel（例如「修改要求」「补充说明」）；
      // 服务端没给就用「直接填写」—— 与题型提示同一句口径，用户一眼能看到
      // 这一步该在这里写。
      const textLabel = String(q.otherLabel || "").trim() || "直接填写";
      const textControls = isTextOnly(q)
        ? `
          <div class="question-text mt-2" data-question-text="${esc(q.id || "")}">
            <div class="question-text-label text-[11px] text-amber-800 mb-1">${esc(textLabel)}</div>
            <textarea class="question-text-input w-full rounded-md border border-amber-200 bg-white px-2 py-1.5 text-xs text-slate-700 outline-none focus:border-blue-400 focus:ring-1 focus:ring-blue-100" rows="2" placeholder="${esc(q.otherPlaceholder || "请在这里填写你的说明")}" data-question-text-input="${esc(q.id || "")}"></textarea>
            <div class="flex items-center gap-2 mt-1.5">
              <button type="button" class="question-text-submit shrink-0 rounded-md border border-amber-200 bg-amber-50 hover:bg-amber-100 text-amber-800 text-xs px-2 py-1.5" data-question-text-submit="${esc(q.id || "")}">提交</button>
              <span class="question-text-hint hidden text-[11px] text-amber-700"></span>
            </div>
          </div>`
        : "";
      const hint = [expectsLabel(q.expects), q.required ? "必填" : ""].filter(Boolean).join(" · ");
      // 步骤身份挂到这张卡上：撤销之后按它把「被撤销那一步」标出来
      // （契约 §2.3 第 2 条：旧步骤失效，消息文本保留）。
      const stepAttr = q.stepId
        ? ` data-step-id="${esc(q.stepId)}" data-step-question="${esc(questionKey(q))}"`
        : "";
      return `
        <div class="px-3.5 py-2.5 border border-amber-200 bg-white rounded-lg"${stepAttr}>
          <div class="font-medium text-amber-800 text-sm">${esc(q.prompt || q.text || "请选择：")}</div>
          ${hint ? `<div class="text-[11px] text-slate-500 mt-1">${esc(hint)}</div>` : ""}
          ${controls}
          ${textControls}
          ${isTextOnly(q) ? "" : renderOtherAnswerHtml(q)}
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
  if (required && lock) setChoiceLock(required);
  bindQuestionSelects(wrap, toDraw);
  wrap.querySelectorAll(".question-restart").forEach((button) => {
    button.addEventListener("click", () => {
      if (lockedChoice?.id === required?.id) sendMessage("重新开始", { fromChoice: true });
    });
  });
  // 「其他」：卡片内自由文本。启用状态由 syncInputControls 按
  // 「当前待答的那张卡」统一管（历史回放出来的卡是灰的）。
  bindOtherAnswers(wrap);
  syncInputControls();
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

/**
 * 响应里那一张**受限修改输入**的卡（带步骤身份的那张）。
 *
 * 「修改」进入的下一步是受限输入：普通聊天在这一步锁着，只有这张卡的
 * 专用输入能提交。取它是为了拿到**服务端的权威步骤身份** —— 下一轮
 * 提交要靠它关联，后端才认得出这段文字确实来自当前这一步。
 */
function resolveEditStepQuestion(data) {
  return (data?.questions || []).find((question) => question?.stepKind === "edit_request") || null;
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

/**
 * 待确认修改计划对应的那份菜单对象。
 *
 * 锁的范围（lockScope）由服务端那份菜单决定并原样带过来：
 * 待确认修改计划出现时要**恢复选择锁**（契约表 §5.2）。
 */
function pendingEditMenuQuestion(question, options) {
  return {
    id: question?.id || "pending_edit_plan_confirm",
    selectionOnly: true,
    inlineEdit: true,
    options,
    lockScope: question?.lockScope,
    // 「其他」：这张卡允许在卡片里直接写要怎么改（纠正）。
    allowsOther: question?.allowsOther === true,
    otherLabel: question?.otherLabel,
    otherPlaceholder: question?.otherPlaceholder,
    // 步骤身份：候选菜单也要绑定**当前这一步** ——
    // 旧卡片的答案不能被当成新菜单的答案。
    stepId: question?.stepId,
    stepVersion: question?.stepVersion,
    stepKind: question?.stepKind,
  };
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
  const simulated = readReportSourceMode(session.definition, session.reportPreviewPayload) === "simulated";

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
    <div class="rounded-xl border border-amber-200 bg-white overflow-hidden" data-pending-plan="${esc(plan.planId || "")}"${question?.stepId ? ` data-step-id="${esc(question.stepId)}"` : ""}>
      <div class="px-3.5 py-2.5 bg-amber-50 border-b border-amber-100 flex items-center gap-2">
        <iconify-icon class="text-amber-600" icon="mdi:clipboard-text-clock-outline"></iconify-icon>
        <span class="text-sm font-semibold text-amber-900">待确认的修改</span>
      </div>
      <div class="p-3 space-y-2 text-xs text-slate-700" data-plan-body>
        <div>
          <span class="text-slate-500">目标：</span>
          ${
            !plan.targetRegionId
              // 整页级修改（排版、间距…）本来就没有「某一张卡」这个目标：
              // 说成「不在当前可见正文顺序里」是误导 —— 那是**目标卡片失效**的提示。
              ? `<span class="font-medium text-slate-800">整页（全部正文卡片）</span>`
              : targetIndex >= 0
                ? `<span class="font-medium text-slate-800">第 ${targetIndex + 1} 个正文卡片「${esc(targetLabel)}」</span>`
                : `<span class="text-rose-600">目标卡片不在当前可见正文顺序里</span>`
          }
        </div>
        ${plan.summary ? `<div class="whitespace-pre-wrap">${esc(plan.summary)}</div>` : ""}
        ${
          operationItems
            ? `<div><div class="text-[11px] text-slate-500 mb-1">具体改什么</div><ul class="list-disc pl-4 space-y-1">${operationItems}</ul></div>`
            : ""
        }
        ${plan.unverified && !simulated ? `<div class="text-[11px] text-amber-800">未验证能力：${esc(plan.unverified)}</div>` : ""}
        <div class="text-[11px] text-slate-400">草稿版本 ${esc(plan.draftRevision ?? "—")}</div>
        <div class="text-[11px] text-slate-400">${simulated ? "展示数据均为模拟；明确要求只显示名称的项不填数值。" : "接受修改后才会更新草稿。"}</div>
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
  //
  // **按值对齐，不按序号对齐**：服务端可能在选项里多插一项
  // （例如「先占位，只显示名称」），按序号对齐会整体错位 ——
  // 用户看到「接受修改」，发出去的却是「继续调整」。
  // 服务端给了哪些选项就画哪些（一个都不吞），契约里的三个词按值认。
  const backendOptions = Array.isArray(question?.options) ? question.options : [];
  const contractChoices = new Set(Object.values(EDIT_PLAN_CONTRACT.choice));
  const options = backendOptions.length
    ? backendOptions
        .map((option) => {
          const rawValue = String(option?.value ?? option?.label ?? "").trim();
          const rawLabel = String(option?.label ?? option?.value ?? "").trim();
          if (!rawValue && !rawLabel) return null;
          // 契约里的三个词：值与文案都用契约值（后端按值认）。
          const displayLabel = (value) => value === EDIT_PLAN_CONTRACT.choice.adjust ? "继续修改" : value === EDIT_PLAN_CONTRACT.choice.cancel ? "撤回上一步" : value;
          if (contractChoices.has(rawValue)) return { value: rawValue, label: displayLabel(rawValue) };
          if (contractChoices.has(rawLabel)) return { value: rawLabel, label: displayLabel(rawLabel) };
          return { value: rawValue || rawLabel, label: rawLabel || rawValue };
        })
        .filter(Boolean)
    : [
        { value: EDIT_PLAN_CONTRACT.choice.accept, label: EDIT_PLAN_CONTRACT.choice.accept },
        { value: EDIT_PLAN_CONTRACT.choice.cancel, label: "撤回上一步" },
        { value: EDIT_PLAN_CONTRACT.choice.adjust, label: "继续修改" },
      ];
  const questionId = question?.id || "pending_edit_plan_confirm";

  // 菜单对象带上服务端的锁范围与「其他」入口（归一化不能把这两样弄丢）。
  const menuQuestion = pendingEditMenuQuestion(question, options);

  const menu = document.createElement("div");
  menu.className = "px-3 pb-3";
  menu.innerHTML = `
    <label class="block text-sm text-slate-700 leading-relaxed mb-2">确认后才会修改草稿，请选择：</label>
    <select class="question-select w-full rounded-md border border-amber-200 bg-white px-3 py-2 text-sm text-slate-700" data-question-id="${esc(questionId)}">
      <option value="">请选择</option>
      ${renderQuestionOptions(menuQuestion)}
    </select>
    ${renderOtherAnswerHtml(menuQuestion)}
  `;
  body.appendChild(menu);

  bindQuestionSelects(menu, [menuQuestion]);

  if (lock) setChoiceLock(menuQuestion);
  else businessMenuReplayed = true;
  // 「其他」：卡片内自由文本。启用状态由 syncInputControls 按
  // 「当前待答的那张卡」统一管（历史回放出来的卡是灰的）。
  bindOtherAnswers(menu);
  syncInputControls();
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
    } else if (event === "trace_round") {
      // 「ReAct 第 N 轮」：同一轮会被多次推送 → 卡片里**更新**同一行。
      addTraceRoundRow(debugCard, payload);
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

async function sendMessage(text, { fromChoice = false, designAction = null, fromOther = false, fromPlan = false, inlineEditText = "", fromInline = false, fromQuestionText = false, targetRegionId = null } = {}) {
  if (!text || activeRequest || advanceTimers.size) return;
  const inlineChoice = inlineEditText ? lockedChoice : null;
  if (
    !fromChoice &&
    !fromPlan &&
    !designAction &&
    text !== "__advance__" &&
    menuLocksInput(lockedChoice, hasPendingPlan())
  ) {
    // 界面锁定只是**引导**（规格第 56 条）：这里挡的是「误发自由输入」，
    // 真正的边界在后端 —— 直接调接口在等待选择期间同样不能绕过确认。
    toast(INPUT_LOCK_HINT);
    return;
  }
  if (lockedChoice && fromChoice) {
    // 从菜单里选的必须是菜单自己的选项。
    // 自由输入不再被前端拦（只读提问要走这条路），执行约束由后端把关。
    const allowed =
      lockedChoice.options?.some((o) => (o.value || o.label) === text) ||
      (lockedChoice.allowRestart && text === "重新开始");
    // 「其他」：这张卡允许在卡片里写自由文本时，卡片输入框里写的内容就是
    // **这张卡的答案**（纠正），不要求命中选项 —— 它不进选项列表，
    // 后端按「文本不在选项里 + 这张卡允许其他」把它认成纠正。
    const otherAnswer = fromOther && lockedChoice.allowsOther === true;
    const inlineAnswer = fromInline && lockedChoice.inlineEdit === true && lockedChoice.inlineEditStarted === true;
    // 文字型待决：卡片里那个专用输入写的内容，就是**这一步**的答案。
    // 判据就地写成表达式（不调用 expectsFreeText）：这段函数会被既有单测
    // 单独抽进沙箱跑，沙箱里只有它原有的依赖。
    const questionTextAnswer =
      fromQuestionText && String(lockedChoice.expects || "").trim().toUpperCase() === "TEXT";
    if (!allowed && !otherAnswer && !inlineAnswer && !questionTextAnswer) {
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
  // 本轮带不带卡片目标（context.selectedRegionId）—— 20261102 工单「临时卡片目标释放」。
  //
  // 临时卡片目标只属于**当前这一次操作**，不能从上一轮漏到下一轮：
  //   - 显式卡片动作（用户点了某张卡片的操作入口，sendTemplateEditAction 传进来）：
  //     带这张卡的 id；
  //   - 延续当前操作（回答后端给出的菜单/问题：接受待确认计划、回答刚才的追问）：
  //     带当前操作的目标 —— 未完成的澄清/待接受计划要保留目标，直到这次操作结束；
  //   - 新的普通一轮（草稿里的自由输入、普通「修改」的内联两段请求、内部推进）：
  //     草稿级，**不带** id。
  //
  // 以前这里是 `if (session.editingTargetRegionId) context.selectedRegionId = ...`：
  // 上一轮选中的卡片会一直黏在后面每一轮上，新需求因此被旧卡片截断
  // （用户看到的「选中卡片以后增加不了、一直缩在卡片上」）。
  const isMenuAnswer = fromChoice || fromOther || fromPlan || fromQuestionText;
  const isInlineTwoStage = Boolean(inlineEditText) || fromInline;
  // 追问状态（NEEDS_CLARIFICATION）下的自由输入是**回答刚才的追问**，算延续。
  const continuesCurrentOperation =
    !isAdvance && !designAction && !isInlineTwoStage &&
    (isMenuAnswer || session.state === "NEEDS_CLARIFICATION");
  const roundTargetRegionId =
    targetRegionId || (continuesCurrentOperation ? session.editingTargetRegionId : null);
  if (roundTargetRegionId) context.selectedRegionId = roundTargetRegionId;
  // 步骤关联（权威步骤契约）：本轮请求带上「当前这一步」的身份。
  //
  // 服务端拿它跟**持久化**的权威记录（pending_choice）核对 —— 不是相信
  // 前端的布尔来源标志（fromChoice / fromOther / fromInline 只是本地参数，
  // HTTP 请求里根本没有，后端校验不了）。
  //
  // 少了它：受限修改输入那一步会拒绝这次提交（旧的直接调用接口也绕不过）。
  const stepRef = lockedChoice;
  if (stepRef?.stepId) {
    context.stepId = stepRef.stepId;
    context.stepVersion = stepRef.stepVersion ?? null;
  }
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
    // 传输失败：**客户端观察**，不冒充服务端已停止或业务失败。
    // 失败这一条也给一个反馈入口（独立接口签发目标）。
    const failureMark = typeof aiBubbleCount === "function" ? aiBubbleCount() : 0;
    appendAiText("请求失败：" + err.message);
    if (typeof registerFeedbackClientEvent !== "function") return;
    registerFeedbackClientEvent("client_failure", {
      clientAttemptId: body.messageId || newMessageId(),
      messageId: body.messageId || undefined,
      observation: "transport_error",
    }).then((target) => mountFeedbackTarget(target, failureMark));
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
  // 这一轮的气泡锚点：反馈入口挂在**本轮第一条** AI 气泡里
  // （同一轮多个气泡/卡片共用一个入口，不逐个挂）。
  // typeof 守卫：只抽走部分函数的测试沙箱里没有这个函数 —— 不该因此炸掉。
  const feedbackBubbleMark = typeof aiBubbleCount === "function" ? aiBubbleCount() : 0;
  // 方案状态跟着响应走：成功、失败、冲突都如实反映到方案卡片上。
  syncPlanFromResponse(data);
  if (data.error) {
    // JSON 输入被拒：面板里也摆一份**可修正**的提示（哪里不对、怎么改）。
    showJsonPasteErrorHint(data.error);
    // 失败原因要看得见：对话区一条 AI 回复（与恢复会话那处同一套写法）。
    appendAgentError(data.error);
    // 失败**不**更新方案：上一版仍然有效（工单 #66）。
    appendPlanFailureNotice();
    if (isTemplateEditFailure(data)) {
      setState(data.state);
      restoreEditFailureMenu(data);
      updateMeta();
      renderSessionList();
    }
    // 失败回复**也可以评价**：服务端这一轮登记过的目标照样给入口。
    if (typeof mountFeedbackTarget === "function") mountFeedbackTarget(data.feedbackTarget, feedbackBubbleMark);
    return data;
  }
  // 回答菜单之后**不主动解锁**：解锁由服务端这一轮给的权威菜单决定
  // （新菜单会自己 setChoiceLock 接管旧锁）。
  //
  // **受限步骤**（用户在专用输入里写的要求）例外：这一步只由服务端解除。
  // 服务端这一轮可能只回了一段文字（没有新的菜单）—— 那**不代表**这一步
  // 结束了（BUG-003：提了修改要求之后回了一段分析，回复完普通输入就开了）。
  // 「没有候选」「返回了一段文字」**不是**解锁理由（契约 §3.3）。
  // 受限步骤：用户在**专用输入**里写的要求（卡片内的文字输入 / 内联两段式）。
  // 这一步只由服务端解除。
  const restrictedTurn = Boolean(inlineEditText) || fromInline || fromQuestionText;
  if (fromChoice) clearChoiceLock();

  // JSON 输入被收下：说清这份数据的来路（契约表 §1.1）。
  if (data.debug?.jsonInput?.accepted === true) {
    appendJsonSourceNote();
    closeJsonPastePanel();
    clearJsonPasteInputs();
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
  // 临时卡片目标的三态（20261102 工单）：
  //   - **缺字段**：这一轮不更新目标，保持原值；
  //   - **显式 null**：释放目标（这次操作结束，回到草稿级）；
  //   - **非空**：本轮的目标就是这张卡。
  //
  // 只读回答不参与：只读咨询不结束正在进行的操作，不能擅自释放目标。
  if ((!readOnlyKind || data.debug?.queryReturnedToDraft === true) && data.debug && Object.prototype.hasOwnProperty.call(data.debug, "editingTargetRegionId")) {
    const nextTargetRegionId = data.debug.editingTargetRegionId || null;
    if (!nextTargetRegionId && session.editingTargetRegionId) {
      // 释放：屏上那张卡不该还留着「被选中」的痕迹。
      clearRegionHighlight();
    }
    session.editingTargetRegionId = nextTargetRegionId;
  }
  // Keep the legacy action handshake at submit time, not when opening the input.
  if (inlineEditText && !readOnlyKind && data.state === "USER_EDITING_TEMPLATE" && !readPendingEditPlan(data)) {
    // 以**服务端这一轮**给的步骤卡为准：它带步骤身份与锁范围，
    // 下一轮提交要靠它关联。沿用上一张旧卡会把身份带错。
    const stepQuestion = resolveEditStepQuestion(data) || inlineChoice;
    setChoiceLock({ ...stepQuestion, inlineEdit: true, inlineEditStarted: true });
    return sendMessage(inlineEditText, { fromChoice: true, fromInline: true });
  }

  if (readOnlyKind) {
    // 只读回答单独展示：不画业务确认菜单、不改右侧预览、不触发自动推进。
    renderReadOnlyAnswer(data, readOnlyKind);
    if (data.debug?.queryReturnedToDraft === true) {
      clearChoiceLock();
      appendConfirmActions(data.questions?.[0]);
    }
    // 查询轮带回的原菜单：屏幕上还没有菜单时才补画（已有就不重复画）。
    if (!lockedChoice) {
      const preservedMenu = resolveQueryPreservedMenu(data);
      if (preservedMenu) appendConfirmActions(preservedMenu);
    }
    // 受限步骤：只读回答**不结束**这一步（契约 §3.3：「返回了一段文字」
    // 不是解锁理由）。用户在受限输入里问了一句只读问题，普通输入必须
    // **继续锁着**，合法入口仍是那张卡的专用输入。
    if (restrictedTurn && !lockedChoice) {
      await restoreStepLockAfterReadOnly(data);
    }
    updateMeta();
    renderSessionList();
    // 只读回答也是一轮真实回复：同样可以评价。
    if (typeof mountFeedbackTarget === "function") mountFeedbackTarget(data.feedbackTarget, feedbackBubbleMark);
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
  //
  // 草稿的版本取**草稿自己的 `metadata.revision`**（编辑链版本号，每改一次
  // +1）：保存次数另记在 `metadata.savedRevision`，两者互不占用 ——
  // 拿保存次数当草稿版本，下一次编辑会被服务端当成旧草稿拦掉。
  // 这一轮的权威值就是响应里的 `domainObjects.reportDefinitionDraft`。
  const draftDefinition = data.domainObjects?.reportDefinitionDraft;
  if (draftDefinition) {
    session.definition = draftDefinition;
    session.policy = data.domainObjects?.reportPolicyDraft || session.policy;
    session.reportDefinitionId = draftDefinition?.metadata?.id || session.reportDefinitionId;
    session.baseDefinitionRevision = draftDefinition?.metadata?.revision ?? session.baseDefinitionRevision;
  }

  // 草稿工作区状态（契约 0002 §6）：草稿 id、版本、能不能撤销 / 重做。
  // 服务端给什么就是什么 —— 前端不自己数历史。
  const draftWorkspace = data.domainObjects?.draftWorkspace;
  if (draftWorkspace) applyWorkspaceState(draftWorkspace);

  // 「同意报表」这一轮：服务端已经把确认下来的定义落库，并建好了草稿对象
  // （`draftId` 就在上面那份 `draftWorkspace` 里）。
  //
  // 这一轮之后弹「保存草稿」对话框，让用户给草稿起名、写备注 ——
  // 名字与备注的落点是草稿对象，走的也是既有的草稿动作。
  if (data.uiActions?.includes("save_definition_draft")) {
    promptSaveConfirmedDraft().catch((err) => {
      toast("保存草稿失败：" + (err?.message || err));
    });
  }

  // 报示意载荷：草稿确认的**同一轮**响应就带它（不做二次请求）。
  //
  // 草稿与载荷说的是**同一份模板**（服务端用同一份定义生成载荷），
  // 所以这一轮带来的载荷就是这一轮草稿的载荷 —— 以它为准，
  // 不拿版本校对把它当过期丢掉（丢掉的后果是「草稿已经变了，
  // 屏幕上的数字还是旧的」，右侧看着像没刷新）。
  //
  // 版本校对留给**会话恢复**那条路：那里的载荷是服务端按当前定义
  // 重新生成的，对不上就说明它不该上屏。
  const roundPayload = readReportPreviewPayload(data);
  if (roundPayload) {
    if (draftDefinition) {
      session.reportPreviewPayload = roundPayload;
      // 载荷到了，之前那条「没生成出来」的理由就不成立了。
      session.reportPreviewError = null;
    } else {
      // 这一轮只来了载荷、没来草稿：照旧按版本校对收（对不上就丢掉）。
      applyReportPreviewPayload(roundPayload);
    }
  }

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
        session.reportPreviewError = null;
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
      // 后端保存后带回了草稿身份。
      //
      // **版本号取草稿自己的 `metadata.revision`**（编辑链版本：每改一次 +1）：
      // 保存次数另记在 `metadata.savedRevision`，两者不是一回事 ——
      // 拿保存次数当草稿版本，下一次编辑会被服务端当成旧草稿拦掉。
      // 上面「定义先落位」那一步已经按草稿的权威值写好了，这里只补身份。
      {
        const saveTool = (data.toolRequests || []).find((t) => t.type === "SAVE_DEFINITION_DRAFT");
        if (saveTool?.arguments?.definitionId) {
          session.reportDefinitionId = saveTool.arguments.definitionId;
        }
        const savedDraft = data.domainObjects?.reportDefinitionDraft;
        if (savedDraft?.metadata?.revision != null) {
          session.baseDefinitionRevision = savedDraft.metadata.revision;
        }
      }
      appendAiText(data.reply || "草稿已保存，后续流程待接入。");
      // 草稿确认到此结束，暂不进入后续校验和预览流程。
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
  // 受限步骤：这一轮**没有**给出新的权威菜单 → 不因为「请求结束」就解锁。
  // 「没有候选」「返回了一段文字」不是解锁理由（契约 §3.3）。
  // 问服务端一句「现在还有没有待决步骤」，按答案上锁 / 解锁 ——
  // 锁只由服务端说了算，前端不自己判断这一步结束了没有。
  //
  // typeof 守卫：这段函数会被既有单测单独抽进沙箱跑，沙箱里没有
  // syncChoiceLockFromServer —— 跳过即可（**不是**功能降级：
  // 真实页面里它与主脚本同文件，一定在）。
  if (restrictedTurn && !lockedChoice && typeof syncChoiceLockFromServer === "function") {
    await syncChoiceLockFromServer();
    syncInputControls();
  }
  // 这一轮的反馈入口：一个回复一个入口（挂在本轮第一条气泡里）。
  if (typeof mountFeedbackTarget === "function") mountFeedbackTarget(data.feedbackTarget, feedbackBubbleMark);
  return data;
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
  // 本轮**显式**指定这张卡：请求带上它的 id。
  // 普通的新一轮（自由输入、内联两段请求）不会带 —— 目标不从上一轮漏过来。
  return sendMessage(message, { targetRegionId: regionId });
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
  question = { ...question, inlineEdit: true };
  const wrap = aiBubble(`
    <div class="rounded-xl rounded-tl-sm border border-emerald-200 bg-white px-3.5 py-3">
      <label class="block text-sm text-slate-700 leading-relaxed mb-2.5">${esc(question.prompt)}</label>
      <select class="question-select w-full rounded-md border border-emerald-200 bg-white px-3 py-2 text-sm text-slate-700" data-question-id="${esc(question.id || "")}">
        <option value="">请选择</option>
        ${renderQuestionOptions(question)}
      </select>
      ${renderOtherAnswerHtml(question)}
    </div>
  `);
  if (lock) setChoiceLock(question);
  else businessMenuReplayed = true;
  bindQuestionSelects(wrap, [question]);
  bindOtherAnswers(wrap);
  syncInputControls();
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
  toast("变更已应用，草稿版本 → " + definition.metadata.revision);
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
 *
 * 口径：previewContext 的 definitionId / definitionVersion 对应的是**方案**身份
 * （方案 id + 方案 revision），落库在 definition.metadata.templateBinding 里。
 * definition.metadata.revision 是**定义的编辑计数**（另一条轴），拿它去比会把
 * 同一份载荷误判成过期、导致草稿阶段的数字被丢掉 —— 所以优先读 templateBinding，
 * 历史定义没有 templateBinding 时才回退到 metadata.id / metadata.revision。
 */
function isStaleReportPayload(payload) {
  const ctx = payload?.previewContext || {};
  const definition = session.definition;
  const meta = definition?.metadata || {};
  const binding = meta.templateBinding || {};
  // 定义身份：空串不算身份（回退到旧字段）。
  const definitionId = binding.definitionId || meta.id;
  // 版本号：0 是合法版本号，只能用 ?? 不能用 ||。
  const definitionVersion = binding.definitionVersion ?? meta.revision;
  if (ctx.definitionId && definitionId && String(ctx.definitionId) !== String(definitionId)) {
    return true;
  }
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
  // 载荷到了，之前那条「没生成出来」的理由就不成立了（恢复失败 → 又出数了）。
  session.reportPreviewError = null;
  return true;
}

// --------------------------------------------------------------------------
// 结构化失败的显示口径（#53）
//
// 后端有两处把「这一轮没成」结构化地带回来：
//   - 聊天响应顶层 `error`（AgentResponse.error）；
//   - 会话恢复响应 `snapshot.error`（恢复时模拟载荷生成不出来）。
//
// 两处用**同一套口径**显示：[错误码] 人话原因。
// 聊天响应那处本来就有（对话区一条 AI 回复）；恢复会话那处原来没有，
// 于是用户刷新一下页面，之前那句「按周不支持」就没了，只剩一片空白的
// 右侧面板 —— 用户不知道发生了什么。
//
// 显示沿用页面**已有**的机制，不另造一套：
//   - 对话区：沿用 appendAiText（与聊天响应的失败写法逐字一致）；
//   - 右侧面板：一条失败提示，用页面既有的 rose 色板（与状态徽标
//     VALIDATION_FAILED / BLOCKED_OR_UNSUPPORTED 同一套），不另造配色。
//
// 失败时**不画载荷、不画空壳卡片**：只说清楚为什么没出来。
// --------------------------------------------------------------------------

/** 结构化失败 → 一句人话（[错误码] 原因）。 */
function formatAgentError(error) {
  if (!error || typeof error !== "object") return "";
  const code = error.code ? String(error.code) : "";
  const message = error.message ? String(error.message) : "";
  if (!code) return message;
  if (!message) return `[${code}]`;
  return `[${code}] ${message}`;
}

/** 把结构化失败显示成对话区的一条 AI 回复（聊天响应与恢复会话共用同一套）。 */
function appendAgentError(error) {
  const text = formatAgentError(error);
  if (!text) return;
  appendAiText(text);
}

function isTemplateEditFailure(data) {
  return data?.state === "USER_EDITING_TEMPLATE" && /^EDIT_/.test(String(data.error?.code || ""));
}

function restoreEditFailureMenu(data, { lock = true, pending = null } = {}) {
  // 失败这一轮**不一定**消费了旧计划：部分执行失败时后端仍留着它，
  // 候选菜单也就还有效。旧代码在这里一律「假定旧计划已消费」→ 清锁 →
  // 放开普通聊天，后端却还留着计划 —— 两边对不上，用户能在计划还在的
  // 时候去改别的东西。
  //
  // 现在按**明确语义**恢复：响应里给了新的权威菜单就换锁；没给就**保持
  // 原锁**，普通聊天继续锁着（后端仍有的计划 / 当前步骤还在等用户处理）。
  const questions = (data.questions || []).filter(Boolean).map((question) =>
    pending?.id === question.id ? pending : question
  );
  const confirm = questions.find((question) => question.id === "template_edit_confirm");
  if (confirm) {
    lastPendingPlan = null;
    return appendConfirmActions(confirm, { lock });
  }
  if (questions.length) {
    lastPendingPlan = null;
    appendAiQuestions(questions, { lock });
    return questions.find((question) =>
      (question.selectionOnly || question.allowsOther) && (question.options?.length || question.allowsOther)
    ) || null;
  }
  // 没有新的权威菜单：**保持原来的锁**。旧计划 / 当前步骤还在，
  // 普通聊天不能因为一次失败就被放开。
  lastPendingPlan = null;
  syncInputControls();
  return null;
}

/**
 * 失败时补一句：本次没更新，上一版仍然有效（工单 #66）。
 *
 * 失败**不**生成假装改好的方案、不出现成功摘要；屏上那张卡原样留着，
 * 仍然可确认（真正的边界在后端的版本校验 —— 契约表 §8 不变量 6）。
 * 屏上还没有方案时没什么「上一版」可保，就不说这句。
 */
function appendPlanFailureNotice() {
  if (!planState.reportPlan) return;
  appendAiText("本次未更新，上一版仍有效。");
}

/**
 * JSON 输入被拒：面板里也摆一份**可修正**的提示（哪里不对、怎么改）。
 *
 * 不是 JSON 失败就什么都不做 —— 别的失败原因由对话区说。
 */
function showJsonPasteErrorHint(error) {
  const code = String((error && error.code) || "");
  if (!WorkbenchQuery.isJsonErrorCode(code)) return;
  showJsonPasteHint(error);
}

/**
 * JSON 输入被收下时，在对话区说清这份数据的来路（契约表 §1.1）。
 *
 * 只说「用户提供的数据，未经业务核验」—— **不宣称已连接真实业务库**。
 */
function appendJsonSourceNote() {
  appendAiText("这份数据的来源：" + WorkbenchQuery.USER_JSON_SOURCE_LABEL + "。");
}

/**
 * 读会话恢复响应里的失败原因。
 *
 * 后端在模拟载荷生成不出来时（典型：方案要「按周统计」，示意数据只能按
 * 分钟 / 小时 / 天生成）把原因挂在 `snapshot.error` 上（code + 人话 message），
 * 而不是静默返回空快照。没有这个键 = 正常会话（带 reportPreviewPayload）。
 */
function readSessionRestoreError(detail) {
  const error = detail?.snapshot?.error;
  if (!error || typeof error !== "object") return null;
  const code = error.code ? String(error.code).trim() : "";
  const message = error.message ? String(error.message).trim() : "";
  if (!code && !message) return null;
  return { code, message: message || code };
}

/** 右侧面板顶部的失败提示条（样式沿用页面既有的失败色板）。 */
function renderPreviewErrorNotice(error) {
  const text = formatAgentError(error);
  if (!text) return null;
  const box = document.createElement("div");
  box.className = "rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700";
  box.setAttribute("data-preview-error", "");
  box.setAttribute("role", "alert");
  const line = document.createElement("div");
  line.className = "leading-relaxed";
  line.textContent = text;
  box.appendChild(line);
  return box;
}

/** 定义里的区域（按 id 找）：载荷只带 regionId，编辑入口与 topN 都要回定义里取。 */
/**
 * 在定义里按 id 找区域。
 *
 * **递归**（契约 0002 §1）：区域可能在分组（容器）的 children 里，
 * 只在顶层找会漏掉组内的卡片 —— 漏了就渲染不出那张卡。
 */
function findDefinitionRegion(definition, regionId) {
  if (!definition || !regionId) return null;
  const search = (regions) => {
    for (const item of regions || []) {
      if (!item || typeof item !== "object") continue;
      if (item.id === regionId) return item;
      const children = item.children;
      if (Array.isArray(children)) {
        const hit = search(children);
        if (hit) return hit;
      }
    }
    return null;
  };
  return search(definition.regions);
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
 * 「待接通」标注要说明的具体项（**按类别分组**，每类一条）。
 *
 * 契约（**两个来源都支持**）：
 *   - 区域级：region.capabilityPending（布尔）—— 区域整体待接通。
 *     后端在响应区域上带这个字段（规格 #46：区域额外带来源与能力标记）；
 *     载荷里另有逐项标记，两个来源都认。
 *   - 逐项级：payload.metrics[] 与定义 previewSpec.metrics[] 里的
 *     capabilityPending / pendingReason —— 哪几项待接通、为什么。
 * 两处**合并**（不是二选一）：逐项说明永远保留；区域级标记在没有逐项说明时
 * 自己出一条 —— 不因为某一处缺失就丢掉另一处的信息。
 *
 * 类别（规格《模拟语义与能力》：能力待接通 / 范围授权 / 既有绑定三态相互独立，
 * **不把所有占位原因都标为能力缺失**）：
 *   - 统计对象**不在授权范围内**是授权问题，不是能力缺失 → 角标「不在授权范围」；
 *   - 其余（能力没接通）→ 角标「能力待接通」；
 *   - 原因文本为空 → 按「能力待接通」兜底（没写原因就分不出是不是授权问题）。
 * 判类别用**关键词**（授权 / 权限 / 越权 / 范围），不写死整句 ——
 * 后端正在并行改这几条文案，句子会变，关键词稳。
 *
 * 这里只如实说明**哪个指标还没接通、为什么**，不承诺计算口径或公式。
 */
function readCapabilityNotes(region, payload, defRegion, sourceMode) {
  // 判「是不是授权问题」的关键词 —— 与后端
  // `report_template_service._SCOPE_KEYWORDS` **同一套词**：
  // 授权 / 权限 / 越权。两边不一致时，同一段原因会被一方说成
  // 「能力待接通」、另一方说成「不在授权范围」。
  // 刻意**不含**「范围」：能力类原因里出现「范围」（例如
  // 「不在支持的数据范围」）时，那仍然是能力问题，不是授权问题。
  const SCOPE_KEYWORDS = ["授权", "权限", "越权"];
  const KIND_LABELS = { capability: "能力待接通", scope: "不在授权范围" };
  // JSON 模式下，服务端那条 pendingReason 可能还写着「模拟业务数据」——
  // 那是**说假话**（这份数据是用户贴进来的，不是模拟的）。
  // 宁可少说：这种原因不当作能力原因显示；数据来路由界面自己的来源标注说。
  //
  // 取值 "user_json" 是契约表 §1.1 定死的来源模式枚举；
  // 这里写死字面量是因为本函数会被接缝探针**单独抽出来**跑，
  // 不能依赖模块里的别的符号。
  const isUserJson = sourceMode === "user_json";

  const groups = new Map(); // 类别 → 该类别的具体项
  const seen = new Set();
  const collect = (metrics) => {
    if (!Array.isArray(metrics)) return;
    for (const metric of metrics) {
      if (!metric || typeof metric !== "object") continue;
      if (metric.capabilityPending !== true) continue;
      const label = String(metric.label || metric.metricKey || "指标").trim();
      if (!label || seen.has(label)) continue;
      seen.add(label);
      const rawReason = String(metric.pendingReason || "").trim();
      const mentionsSource = rawReason.includes("模拟业务数据")
        || rawReason.includes("模拟示例")
        || rawReason.includes("接入真实数据");
      const reason = isUserJson && mentionsSource ? "" : rawReason;
      const kind = !reason
        ? "capability"
        : SCOPE_KEYWORDS.some((kw) => reason.includes(kw))
          ? "scope"
          : "capability";
      if (!groups.has(kind)) groups.set(kind, []);
      groups.get(kind).push(reason ? `${label}（${reason}）` : label);
    }
  };
  collect(payload?.metrics);
  collect(defRegion?.templateBinding?.previewSpec?.metrics);
  // 区域级标记：逐项说明已经说清楚时不再重复说；没有逐项说明时它自己出一条。
  if (!groups.size && region?.capabilityPending === true) groups.set("capability", ["该指标尚未接通"]);
  // 每个类别一条：角标文案按类别分清楚，不把所有原因都写成「能力待接通」。
  return [...groups.entries()]
    .filter(([kind]) => sourceMode !== "simulated" || kind === "scope")
    .map(([kind, items]) => `${KIND_LABELS[kind]}：${items.join("、")}`);
}

/**
 * 这块板**只绑了一个**指标时的指标键。
 * 绑了多个就分不清哪个是主量 —— 返回空（不猜）。
 */
function readBoundMetricKey(defRegion) {
  const refs = defRegion?.templateBinding?.metricRefs;
  if (Array.isArray(refs) && refs.length === 1) {
    const key = String(refs[0] || "").trim();
    if (key) return key;
  }
  const specMetrics = defRegion?.templateBinding?.previewSpec?.metrics;
  if (Array.isArray(specMetrics) && specMetrics.length === 1) {
    const key = String(specMetrics[0]?.metricKey || "").trim();
    if (key) return key;
  }
  return "";
}

/**
 * 指标卡的主量对应 metrics[] 里的**哪一条**。
 *
 * 真实载荷里 payload.label 是**区域名**（「总电费概览」），
 * metrics[].label 是**目录里的指标名**（「能耗成本」）—— 两者不同名。
 * 所以「label 相等」判不出主量（主量会在「其它指标」清单里再出现一次）。
 *
 * 判定依据（用**值**与**键**，不用名）：
 *   1. **值 + 单位**：主量的值与单位原样放在卡片顶层（payload.value / payload.unit），
 *      值相同、单位不冲突的那一条就是主量 —— 与名字无关；
 *   2. **指标键**：定义侧只绑了一个指标键时，它就是主量；值相同时用它定夺候选；
 *   3. **位置**：载荷没给数值时取 metrics[] 第一条 —— 造数器把主量排在最前。
 *
 * 都不命中 → null：主量不在清单里，清单里**全部**都是「其它指标」（不猜、不丢）。
 */
function readPrimaryMetric(metrics, payload, defRegion) {
  const list = Array.isArray(metrics) ? metrics.filter((m) => m && typeof m === "object") : [];
  if (!list.length) return null;
  const p = payload || {};
  const boundKey = readBoundMetricKey(defRegion);

  const sameValue = list.filter((m) => {
    const mv = Number(m.value);
    const pv = Number(p.value);
    if (!Number.isFinite(mv) || !Number.isFinite(pv) || mv !== pv) return false;
    const mu = String(m.unit ?? "").trim();
    const pu = String(p.unit ?? "").trim();
    return !mu || !pu || mu === pu; // 单位没写就算没冲突，不拿别的单位顶替
  });
  if (sameValue.length) {
    if (boundKey) {
      const byKey = sameValue.find((m) => String(m.metricKey || "") === boundKey);
      if (byKey) return byKey;
    }
    return sameValue[0];
  }
  if (boundKey) {
    const byKey = list.find((m) => String(m.metricKey || "") === boundKey);
    if (byKey) return byKey;
  }
  // 载荷没给数值：造数器把主量排在 metrics[] 最前。
  if (!Number.isFinite(Number(p.value))) return list[0];
  // 载荷的数值在清单里找不到：主量不在清单里，清单里全是「其它指标」。
  return null;
}

/** 载荷里的总量卡片（按单位索引）：表格勾稽要用它。
 *
 * `excludedRegionIds`：**复制件**（契约 0002 §2.2）不参与总计 ——
 * 源件与复制件数值相同，都算进去就把总量说大了。
 * 排除口径由展示模块提供（`duplicateRegionIds`，唯一出处）。
 */
function buildTotalsByUnit(regions, excludedRegionIds) {
  const totals = new Map();
  const excluded = excludedRegionIds instanceof Set ? excludedRegionIds : new Set();
  for (const region of regions) {
    if (region?.componentType !== "METRIC_CARD") continue;
    if (excluded.has(region.regionId)) continue;
    const p = region.payload || {};
    const unit = String(p.unit || "").trim();
    // 没有值就是没有值：null / 空串**不是 0**（Number(null) === 0 会把
    // 「不知道」说成「零」）。指标卡没值 → 这一格不参与总量。
    if (p.value == null || p.value === "") continue;
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
  // JSON 模式（契约表 §4.3）：载荷自己报「总共几行、这一块显示了几行」。
  // recordCount > displayedRows 就是明细表被截断了 —— 这一条与定义侧的
  // topN 无关，也**不看行和**（JSON 模式的汇总是按全部行算的，
  // 行和不是这里的判据；拿行和反推反而会漏判）。
  const recordCount = Number(p.recordCount);
  const displayedRows = Number(p.displayedRows);
  if (Number.isFinite(recordCount) && Number.isFinite(displayedRows) && recordCount > displayedRows) {
    return {
      truncated: true,
      shown: displayedRows,
      limit: displayedRows,
      total: recordCount,
      source: "json",
    };
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
// 权威显示格式（契约 0002 §3.5 / BUG-010）
// --------------------------------------------------------------------------
//
// 格式的**唯一出处**是服务端写下的那一份：
// `backend/app/services/report_presentation_service.py` 的
// `region_authoritative_format()` —— 它读的是区域定义里的 `region.format`
// （`SET_FORMAT` 写下的那一份），只保留白名单里的键。
//
// 前端**不自己造格式**：这里按同一份白名单读同一份定义，再按服务端
// `format_display_value()` 的同一套口径把值渲染成显示串。
// 服务端已经在载荷里算好显示串时优先用服务端的（两边各拼一次迟早对不上），
// 拿不到时才用这里这份 —— 两边口径一致，结果一致。
//
// 注意：显示格式**只改显示**，底层数值一个字节都不动
// （不改数据精度、不改统计结果、不改单位口径）。

/** 权威格式的白名单键（与后端 ALLOWED_FORMAT_KEYS 一一对应）。 */
const AUTHORITATIVE_FORMAT_KEYS = [
  "kind",
  "precision",
  "unitLabel",
  "thousandSeparator",
  "dateTimePattern",
  "nullText",
  "roundingMode",
];

/** 数值型的格式类型（与后端 _NUMERIC_FORMAT_KINDS 一致）。 */
const AUTHORITATIVE_NUMERIC_FORMAT_KINDS = ["NUMBER", "PERCENT", "CURRENCY", "DURATION"];
/** 日期型的格式类型（与后端 _DATE_FORMAT_KINDS 一致）。 */
const AUTHORITATIVE_DATE_FORMAT_KINDS = ["DATE", "DATETIME"];

/**
 * 这个区域的**权威显示格式**（与后端 region_authoritative_format() 同一口径）。
 *
 * 来源是**定义**里的 `region.format` —— 那是服务端写下的那一份。
 * 定义里没有就退回载荷里的 `format`（服务端也是从同一份定义抄过去的）。
 */
function regionAuthoritativeFormat(defRegion, payload) {
  const raw = defRegion && typeof defRegion === "object" && defRegion.format && typeof defRegion.format === "object"
    ? defRegion.format
    : payload && typeof payload === "object" && payload.format && typeof payload.format === "object"
      ? payload.format
      : null;
  if (!raw) return {};
  const out = {};
  for (const key of AUTHORITATIVE_FORMAT_KEYS) {
    if (Object.prototype.hasOwnProperty.call(raw, key) && raw[key] !== null && raw[key] !== undefined) {
      out[key] = raw[key];
    }
  }
  return out;
}

/** 舍入方式 → toFixed 之前用的取整函数（与后端 _ROUNDING_MODES 同一组取值）。 */
const AUTHORITATIVE_ROUNDING_MODES = {
  HALF_UP: (value) => (value < 0 ? -Math.round(-value) : Math.round(value)),
  HALF_DOWN: (value) => (value < 0 ? -Math.round(-value - 0.5) : Math.round(value - 0.5)),
  FLOOR: (value) => Math.floor(value),
  CEIL: (value) => Math.ceil(value),
};

/**
 * 按权威格式把一个数值渲染成显示串（只改显示，数值本身不动）。
 *
 * 与后端 `_format_number()` 同一口径：精度 → 舍入 → 千分位。
 */
function formatAuthoritativeNumber(value, fmt) {
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  const spec = fmt || {};
  let text;
  const precision = spec.precision;
  if (precision !== undefined && precision !== null) {
    const digits = Math.max(0, Math.min(6, Number.parseInt(precision, 10) || 0));
    const mode = AUTHORITATIVE_ROUNDING_MODES[String(spec.roundingMode || "HALF_UP").toUpperCase()]
      || AUTHORITATIVE_ROUNDING_MODES.HALF_UP;
    const factor = 10 ** digits;
    const rounded = mode(num * factor) / factor;
    text = rounded.toFixed(digits);
  } else {
    text = String(num);
  }
  if (spec.thousandSeparator === true) {
    const negative = text.startsWith("-");
    const body = negative ? text.slice(1) : text;
    const [integer, fraction] = body.split(".");
    const grouped = integer ? integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",") : integer;
    text = (negative ? "-" : "") + grouped + (fraction !== undefined ? "." + fraction : "");
  }
  return text;
}

/** 日期值按 dateTimePattern 渲染（认不出来就原样返回，不猜）。 */
function formatAuthoritativeDate(value, fmt, kind) {
  const raw = typeof value === "string" ? value.trim() : "";
  let date = null;
  if (raw) {
    const parsed = new Date(raw);
    if (!Number.isNaN(parsed.getTime())) date = parsed;
  }
  if (!date) return String(value);
  const pattern = String(fmt?.dateTimePattern || "").trim();
  const pad = (num) => String(num).padStart(2, "0");
  if (!pattern) {
    const day = kind === "DATETIME"
      ? `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
      : `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
    return day;
  }
  // 只支持日期格式里最常用的一组占位符；认不出来的原样输出，
  // 宁可少格式化，也不显示错日期。
  return pattern.replace(/%[YmdHMS]/g, (token) => {
    switch (token) {
      case "%Y": return String(date.getFullYear());
      case "%m": return pad(date.getMonth() + 1);
      case "%d": return pad(date.getDate());
      case "%H": return pad(date.getHours());
      case "%M": return pad(date.getMinutes());
      case "%S": return pad(date.getSeconds());
      default: return token;
    }
  });
}

/**
 * 按**权威格式**把一个值渲染成显示串（与后端 format_display_value() 同一口径）。
 *
 * - 数值：按 precision / thousandSeparator / roundingMode 显示；
 * - 日期：按 dateTimePattern 显示；
 * - 单位标签：格式里声明了 unitLabel 就用它（可以空串 = 不带单位），
 *   没声明就用传入的 unit。
 */
function formatDisplayValueByFormat(value, fmt, unit = "") {
  const spec = fmt && typeof fmt === "object" ? fmt : {};
  const kind = String(spec.kind || "").toUpperCase();
  if (value === null || value === undefined || value === "") {
    return String(spec.nullText || "");
  }
  let text;
  let numeric = false;
  if (kind === "TEXT") {
    text = String(value);
  } else if (AUTHORITATIVE_DATE_FORMAT_KINDS.includes(kind)) {
    text = formatAuthoritativeDate(value, spec, kind);
  } else if (AUTHORITATIVE_NUMERIC_FORMAT_KINDS.includes(kind)) {
    text = formatAuthoritativeNumber(value, spec);
    numeric = typeof value === "number" && Number.isFinite(value);
  } else if (typeof value === "boolean") {
    text = String(value);
  } else if (typeof value === "number" && Number.isFinite(value)) {
    text = formatAuthoritativeNumber(value, spec);
    numeric = true;
  } else if (spec.dateTimePattern) {
    text = formatAuthoritativeDate(value, spec, kind || "DATETIME");
  } else {
    text = String(value);
  }
  if (numeric) {
    const label = String(
      Object.prototype.hasOwnProperty.call(spec, "unitLabel") ? spec.unitLabel : unit || ""
    ).trim();
    if (label) text = `${text} ${label}`;
  }
  return text;
}

/**
 * 这条数据**按权威格式**该显示成什么。
 *
 * - 服务端已经按权威格式算好显示串（displayValue）→ 用它
 *   （两边各拼一次迟早对不上，以服务端那份为准）；
 * - 否则按同一份权威格式本地渲染（服务端那份拿不到时用，例如用户 JSON 模式）；
 * - 这份格式**没有改显示** → 返回 null，调用方退回原来的写法。
 *
 * columnFormat：表格列自己的格式（列优先，列没声明才继承区域这一份，
 * 与后端 `{**region_format, **column_format}` 的口径一致）。
 *
 * 真实页面里这个函数一定在（与主脚本同文件）；既有单测把渲染函数单独抽进
 * 沙箱跑，那边没有这个符号 —— 调用方用 typeof 守卫跳过（**不是**功能降级）。
 */
function authoritativeDisplayValue(value, defRegion, payload, unit, serverDisplay, columnFormat) {
  if (serverDisplay !== null && serverDisplay !== undefined && serverDisplay !== "") {
    return String(serverDisplay);
  }
  const fmt = regionAuthoritativeFormat(defRegion, payload);
  const spec = columnFormat && typeof columnFormat === "object" ? { ...fmt, ...columnFormat } : fmt;
  if (!hasAuthoritativeFormat(spec)) return null;
  return formatDisplayValueByFormat(value, spec, unit || "");
}

/**
 * 这份格式**有没有真的改显示**。
 *
 * 只声明 `kind` 不算 —— 那一档服务端与前端原本的口径一致
 * （表格按 kind=NUMBER 走 toFixed），别为了统一而改掉既有观感。
 */
function hasAuthoritativeFormat(fmt) {
  if (!fmt || typeof fmt !== "object") return false;
  return ["precision", "unitLabel", "thousandSeparator", "dateTimePattern", "nullText", "roundingMode"]
    .some((key) => Object.prototype.hasOwnProperty.call(fmt, key) && fmt[key] !== null && fmt[key] !== undefined);
}


// --------------------------------------------------------------------------
// 渲染：右侧报表面板（草稿阶段与预览阶段共用一套）
//
// 入口只有一个：renderReportPanel()。
// 数据来源只有一个：session.reportPreviewPayload（模拟业务数据的区域载荷）。
// 组件类型、板块标题、表格栏目一律取自载荷与已确认方案 —— 前端不改写、不按标题猜。
// --------------------------------------------------------------------------

/**
 * 清掉卡片上的高亮痕迹。
 *
 * 临时卡片目标被释放时调用：屏上不该再留着「这张卡还选着」的痕迹
 * （高亮是渲染时加的 class，不重画面板就得单独摘掉）。
 */
function clearRegionHighlight() {
  if (typeof previewRoot === "undefined" || !previewRoot) return;
  const cards = previewRoot.querySelectorAll?.(".region-highlight") || [];
  Array.prototype.forEach.call(cards, (card) => card.classList.remove("region-highlight"));
}

function renderReportPanel(highlightRegionIds = []) {
  // 先销毁旧图表实例：直接 innerHTML="" 只会移除 DOM，
  // ECharts 实例仍然持有对已脱离文档节点的引用，内存泄漏 + 后续 resize 报错。
  destroyCharts();
  previewRoot.innerHTML = "";

  // 恢复会话失败（模拟载荷没生成出来）：先把原因摆在面板顶部，
  // 别让用户对着空白面板猜。下面仍然是空态，不画载荷、不画空壳卡片。
  const previewError = session.reportPreviewError;
  if (previewError) {
    const notice = renderPreviewErrorNotice(previewError);
    if (notice) previewRoot.appendChild(notice);
  }

  const payload = session.reportPreviewPayload;
  const regions = Array.isArray(payload?.regions) ? payload.regions : [];
  // 标题区（role == REPORT_TITLE）不出数，也不占一块。
  const bodyRegions = regions.filter((region) => !isTitleRegion(region));
  const definition = session.definition;

  if (!bodyRegions.length) {
    if (definition) previewRoot.appendChild(renderPanelHeader(definition, payload || {}, 0));
    if (previewError) {
      // 失败就说失败：上面已经是原因条了，不再摆「示意数据还没生成」的
      // 空态（失败时那句话是错的），也不画空壳卡片。
      return;
    }
    // 没有载荷就没有数可画：回到空态，不画空壳卡片。
    setPreviewEmptyHint(definition ? "draft" : "none");
    previewRoot.appendChild(previewEmpty);
    return;
  }

  // 排版由**定义树**决定（契约 0002 §5）：分组、列数、间距、隐藏都由
  // WorkbenchPresentation 排；载荷只提供数值（按 regionId 关联）。
  // 新模块没加载时退回老的平铺路径 —— 页面不会白屏。
  const presentation = window.WorkbenchPresentation;
  // 总量表：**复制件不参与**（契约 0002 §2.2）。
  // 排除口径由展示模块给（`duplicateRegionIds`，唯一出处）——
  // 主脚本自己算一份的话，两边迟早对不上，总量就会把复制件算进去。
  const duplicateIds = (presentation && typeof presentation.duplicateRegionIds === "function")
    ? presentation.duplicateRegionIds(definition)
    : new Set();
  const totalsByUnit = buildTotalsByUnit(bodyRegions, duplicateIds);

  if (presentation && typeof presentation.render === "function" && definition) {
    // 模块的 render() 会先 clear(root)，把它自己的内容清干净。所以它
    // 渲染在**自己的子容器**里，面板头另放在前面 —— 顺序不能反。
    const panel = document.createElement("div");
    panel.className = "wp-panel";
    previewRoot.appendChild(panel);
    presentation.render(
      panel,
      {
        definition,
        previewPayload: payload,
        resolvedTheme: session.resolvedTheme || null,
        showHidden: !!session.showHiddenContent,
      },
      {
        renderLeaf: (leafRegion, payloadRegion, ctx) => {
          const region = payloadRegion || {
            regionId: leafRegion?.id || "",
            name: leafRegion?.name || "",
            role: leafRegion?.role || "",
            componentType: (leafRegion?.component || {}).type || "",
            payload: {},
          };
          const card = renderRegionCard(region, definition, payload, (ctx || {}).totalsByUnit);
          const highlight = (ctx || {}).highlightRegionIds || highlightRegionIds || [];
          if (highlight.includes(region.regionId)) card.classList.add("region-highlight");
          return card;
        },
        totalsByUnit,
        highlightRegionIds,
        // 容器尺寸变了（窄屏列数翻转、单栏/双栏切换）→ 图表跟着量新尺寸。
        // 走工作台自己的 resize 逻辑：跳过隐藏 / 零尺寸容器，幂等不重渲染。
        onResize: () => {
          if (typeof resizeWorkbenchCharts === "function") resizeWorkbenchCharts();
        },
      },
    );
    // 面板头排在内容前面。
    panel.insertBefore(renderPanelHeader(definition, payload, bodyRegions.length), panel.firstChild);
  } else {
    previewRoot.appendChild(renderPanelHeader(definition, payload, bodyRegions.length));
    for (const region of bodyRegions) {
      const card = renderRegionCard(region, definition, payload, totalsByUnit);
      if (highlightRegionIds.includes(region.regionId)) card.classList.add("region-highlight");
      previewRoot.appendChild(card);
    }
  }
  syncPreviewEditability();
  requestAnimationFrame(() => {
    // 统一走工作台自己的 resize 逻辑（跳过隐藏 / 零尺寸容器）。
    // 沙箱里没有这个函数时退回老写法，不抛错。
    if (typeof resizeWorkbenchCharts === "function") {
      resizeWorkbenchCharts();
      return;
    }
    _chartInstances.forEach((chart) => {
      try { chart.resize(); } catch (e) {}
    });
  });
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

// --------------------------------------------------------------------------
// 数据来源模式（契约表 §1）
//
// 界面上的来源标注**只按服务端给的信号说**，前端不自己猜：
//   1. definition.metadata.effectiveCombination.sourceMode —— 生效组合（权威）；
//   2. definition.metadata.sourceMode —— 定义级；
//   3. region.dataSource / payload.dataSource —— 载荷侧的区域级与逐区域标记；
//   4. region.templateBinding.sourceMode / defRegion.templateBinding.sourceMode。
//
// **拿不到信号就不显示来源标注** —— 宁可少说，不许说假话。
// 特别地：JSON 模式下界面只说「用户提供的数据，未经业务核验」，
// **不宣称已连接真实业务库**（契约表 §1.1 / §1.2）。
// --------------------------------------------------------------------------

/** 来源模式 → 界面上的说法。 */
function sourceModeText(sourceMode) {
  if (sourceMode === WorkbenchQuery.SOURCE_MODE_USER_JSON) {
    return WorkbenchQuery.USER_JSON_SOURCE_LABEL;
  }
  if (sourceMode === WorkbenchQuery.SOURCE_MODE_SIMULATED) {
    return WorkbenchQuery.SIMULATED_SOURCE_LABEL;
  }
  return "";
}

/**
 * 这一块（整份报表）是什么来源模式。
 *
 * 返回 "" = 拿不到信号 → 界面上不显示来源标注。
 */
function readSourceMode(definition, region, payload, defRegion) {
  const candidates = [
    definition?.metadata?.effectiveCombination?.sourceMode,
    definition?.metadata?.sourceMode,
    region?.dataSource,
    payload?.sourceMode,
    payload?.dataSource,
    region?.templateBinding?.sourceMode,
    defRegion?.templateBinding?.sourceMode,
  ];
  for (const candidate of candidates) {
    const key = String(candidate == null ? "" : candidate).trim();
    // 只认契约里定死的两个取值；「mock_energy」这类数据源标识**不是**来源模式。
    if (key === "user_json") return key;
    if (key === "simulated") return key;
  }
  return "";
}

/**
 * 整份报表的来源模式：先看定义级信号，再看载荷里各区域带的标记。
 *
 * 定义级优先（它是权威）；定义是老形状（没有 sourceMode）时，
 * 载荷区域上的 dataSource 就是唯一依据（契约表 §1.1）。
 */
function readReportSourceMode(definition, payload) {
  const fromDefinition = readSourceMode(definition, null, null, null);
  if (fromDefinition) return fromDefinition;
  const regions = Array.isArray(payload?.regions) ? payload.regions : [];
  let sawSimulated = false;
  for (const region of regions) {
    const key = String(region?.dataSource || "").trim();
    if (key === "user_json") return key;
    if (key === "simulated") sawSimulated = true;
  }
  return sawSimulated ? "simulated" : "";
}

/** 面板顶部那句来源说明：按来源模式说。 */
function renderSourceNoteHtml(sourceMode) {
  if (sourceMode === WorkbenchQuery.SOURCE_MODE_USER_JSON) {
    return "<span>当前显示的是<b>用户粘贴的 JSON 数据</b>：" +
      WorkbenchQuery.USER_JSON_SOURCE_LABEL + "。</span>";
  }
  if (sourceMode === WorkbenchQuery.SOURCE_MODE_SIMULATED) {
    return "<span><b>模拟业务数据</b>，仅用于核对版式；明确名称的占位项保留，不显示数值。</span>";
  }
  return "";
}

function readRegionDisplayNotes(payload, sourceMode) {
  const raw = Array.isArray(payload?.notes) ? payload.notes : (payload?.notes == null ? [] : [payload.notes]);
  const notes = raw.map((item) => String(item == null ? "" : item).trim()).filter(Boolean);
  if (sourceMode !== "simulated") return notes;
  return notes.filter((text) => {
    // Keep placeholder and scope facts; only retire legacy simulation/compilation notices.
    if (/占位|只显示名称|仅显示名称|没有编值|授权|权限|越权/.test(text)) return true;
    return !/能力待接通|尚未接通|模拟业务数据|模拟示例|接入真实数据|真实(?:数据)?编译|真实数据.*编译/.test(text);
  });
}

/**
 * 这条原因里有没有「数据来源」的说法。
 *
 * JSON 模式下服务端那条 pendingReason 可能还写着「模拟业务数据」——
 * 那是**说假话**（这份数据是用户贴进来的）。宁可少说：这种原因不当作
 * 能力原因显示；数据来路由界面自己的来源标注说。
 */
function mentionsSourceClaim(reason) {
  const text = String(reason || "");
  return text.includes("模拟业务数据") || text.includes("模拟示例") || text.includes("接入真实数据");
}

/**
 * JSON 模式的数据问题（契约表 §2）：`metadata.jsonInputIssues`
 * = `{unmappedColumns, dataIssues}` —— 「哪些列没绑上」「数据里有哪些问题」。
 *
 * 契约表 §2 写明它「供界面把话说清楚」，所以要端到屏上。
 * 服务端写好的事实，前端只把结构摆出来，**不改写、不总结**
 * （契约表 §8 不变量 8：宁可少说，不许说假话）：
 * 未绑定列的 `reason`、数据问题的原文都逐字照搬。
 *
 * 注意：未绑定列的「（未绑定）」是写在**单元格值**里的，表格那边本来就画得出来；
 * 这里补的是「哪一列、为什么没绑上」这一层，不重复画单元格。
 *
 * 只依赖 `esc`（与页面其它地方同一把转义函数）—— 会被单测抽进沙箱跑。
 */
function renderJsonInputIssuesHtml(definition) {
  const issues = definition?.metadata?.jsonInputIssues;
  if (!issues || typeof issues !== "object") return "";
  const regionNameOf = (regionId) => {
    const hit = (definition?.regions || []).find(
      (item) => String(item?.id || "") === String(regionId || "")
    );
    return String(hit?.name || "").trim();
  };

  const unmappedLines = [];
  for (const item of Array.isArray(issues.unmappedColumns) ? issues.unmappedColumns : []) {
    if (item == null) continue;
    if (typeof item === "string") {
      const text = String(item).trim();
      if (text) unmappedLines.push(text);
      continue;
    }
    const label = String(item.label || item.columnId || "").trim();
    const reason = String(item.reason || "").trim();
    if (!label && !reason) continue;
    const regionName = regionNameOf(item.regionId);
    const where = regionName ? `（${regionName}）` : "";
    unmappedLines.push(`「${label}」${where}${reason ? "——" + reason : ""}`);
  }

  const dataLines = [];
  for (const item of Array.isArray(issues.dataIssues) ? issues.dataIssues : []) {
    const text = String(item == null ? "" : item).trim();
    if (text) dataLines.push(text);
  }

  const blocks = [];
  if (unmappedLines.length) {
    blocks.push(
      `<div>以下栏目没能绑定到输入字段：</div>` +
      unmappedLines.map((line) => `<div>· ${esc(line)}</div>`).join("")
    );
  }
  if (dataLines.length) {
    blocks.push(
      `<div>数据里的问题：</div>` +
      dataLines.map((line) => `<div>· ${esc(line)}</div>`).join("")
    );
  }
  if (!blocks.length) return "";

  return `
    <div class="mt-3 text-[11px] leading-relaxed text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1.5" data-json-input-issues="true">
      ${blocks.join("")}
    </div>`;
}

/**
 * 业务语言标签：草稿状态。
 *
 * 状态是**内部枚举**（`DRAFT` / `PUBLISHED`）。直接渲染出来用户读不懂，
 * 也等于把内部标识摆到界面上。规则：
 *   - 已知取值 → 中文；
 *   - 未知取值 → **不回落英文 key**（英文 key 对用户没有意义，且正是要藏的东西），
 *     回落成通用说法「草稿」。
 *
 * 卡片作用 / 展示形式 / 图表类型那几张表**不放在这里**：
 * 那个渲染函数会被既有单测单独抽进沙箱跑，模块级符号在那儿取不到，
 * 所以那几张表写死在那个函数内部（见 renderRegionCard 里的说明）。
 */
const DEFINITION_STATUS_LABELS = {
  DRAFT: "草稿",
  PUBLISHED: "已发布",
  ARCHIVED: "已归档",
};

/** 枚举 → 中文标签。未知取值返回空串（调用方据此不渲染这一段）。 */
function enumLabel(labels, raw) {
  const key = String(raw == null ? "" : raw).trim().toUpperCase();
  if (!key) return "";
  return Object.prototype.hasOwnProperty.call(labels, key) ? labels[key] : "";
}

/** 草稿状态 → 中文（「草稿」…）。 */
function definitionStatusLabel(raw) {
  return enumLabel(DEFINITION_STATUS_LABELS, raw) || "草稿";
}

/** 面板顶部：报表名 + 一行来源说明（按来源模式说）。 */
/**
 * 草稿工具条：修改历史（撤销 / 重做）、显示隐藏内容、保存 / 另存为 / 打开草稿。
 *
 * 这些是**按钮与下拉**入口，不是"通用属性面板"：本批的排版与外观修改
 * 全部通过对话完成（契约 0002 §0）。
 *
 * 撤销 / 重做的能力**由服务端给**（session.canUndo / session.canRedo），
 * 前端不自己数历史 —— 自己数会和���务端对不上。
 */
function renderDraftToolbar() {
  const bar = document.createElement("div");
  bar.className = "wp-toolbar flex flex-wrap items-center gap-2 mt-3";
  bar.dataset.wpToolbar = "1";

  const canUndo = !!session.canUndo;
  const canRedo = !!session.canRedo;

  const history = document.createElement("select");
  history.className = "wp-history-select question-select rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-700";
  history.setAttribute("data-wp-history", "1");
  history.setAttribute("aria-label", "修改历史");
  history.innerHTML = `
    <option value="">修改历史</option>
    <option value="undo"${canUndo ? "" : " disabled"}>撤销上一步</option>
    <option value="redo"${canRedo ? "" : " disabled"}>重做</option>
  `;
  history.addEventListener("change", () => {
    const action = history.value;
    history.value = "";
    if (action === "undo") runDraftAction("UNDO");
    else if (action === "redo") runDraftAction("REDO");
  });
  bar.appendChild(history);

  const hiddenBtn = document.createElement("button");
  hiddenBtn.type = "button";
  hiddenBtn.className = "wp-btn-show-hidden rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-700";
  hiddenBtn.dataset.wpShowHidden = "1";
  hiddenBtn.textContent = session.showHiddenContent ? "隐藏隐藏内容" : "显示隐藏内容";
  hiddenBtn.addEventListener("click", () => {
    // 开关**不写报表定义**：只是视图状态（契约 0002 §9）。
    session.showHiddenContent = !session.showHiddenContent;
    renderReportPanel();
  });
  bar.appendChild(hiddenBtn);

  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "wp-btn-save rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-700";
  saveBtn.dataset.wpSave = "1";
  saveBtn.textContent = "保存草稿";
  saveBtn.addEventListener("click", () => runDraftAction("SAVE"));
  bar.appendChild(saveBtn);

  const saveAsBtn = document.createElement("button");
  saveAsBtn.type = "button";
  saveAsBtn.className = "wp-btn-save-as rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-700";
  saveAsBtn.dataset.wpSaveAs = "1";
  saveAsBtn.textContent = "另存为";
  saveAsBtn.addEventListener("click", () => runDraftAction("SAVE_AS"));
  bar.appendChild(saveAsBtn);

  const openBtn = document.createElement("button");
  openBtn.type = "button";
  openBtn.className = "wp-btn-open-drafts rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-700";
  openBtn.dataset.wpOpenDrafts = "1";
  openBtn.textContent = "打开草稿";
  openBtn.addEventListener("click", () => openDraftPicker());
  bar.appendChild(openBtn);

  const manageBtn = openBtn.cloneNode(false);
  manageBtn.removeAttribute("data-wp-open-drafts");
  manageBtn.dataset.wpManageDrafts = "1";
  manageBtn.textContent = "管理草稿";
  manageBtn.addEventListener("click", () => openDraftPicker(true));
  bar.appendChild(manageBtn);

  if (session.draftId) {
    const tag = document.createElement("span");
    tag.className = "wp-draft-id text-[11px] text-slate-400";
    tag.dataset.wpDraftId = session.draftId;
    tag.textContent = "草稿 " + session.draftId;
    bar.appendChild(tag);
  }
  return bar;
}

/**
 * 弹「保存草稿 / 另存为」对话框：**草稿名 + 备注**。
 *
 * 两个字段是**两件事**：名字是标识（草稿列表按它认草稿），备注是给
 * 人看的一句话说明（落在草稿对象上）。两者都在「保存草稿」这一次动作里
 * 一起提交，不额外分一次请求。
 *
 * 取消返回 `null` —— 调用方要能分清「用户取消了」和「用户填了空名字」。
 */
function requestDraftName(action) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "rounded-xl border border-slate-200 p-5 shadow-xl";
    dialog.style.cssText = "width:min(420px,calc(100vw - 32px));";
    dialog.innerHTML = `<form method="dialog" class="space-y-4">
      <h2 class="text-lg font-semibold">${action === "SAVE_AS" ? "另存为草稿" : "保存草稿"}</h2>
      <label class="block text-sm">草稿名称
        <input name="draftName" required maxlength="100" class="block w-full mt-2 rounded-lg border border-slate-300 p-2" />
      </label>
      <label class="block text-sm">备注（可选）
        <textarea name="draftRemark" rows="3" maxlength="500"
          class="block w-full mt-2 rounded-lg border border-slate-300 p-2"
          placeholder="这份草稿的用途，比如「给领导看的那份」"></textarea>
      </label>
      <div class="flex justify-end gap-2">
        <button type="button" data-cancel class="rounded-lg border px-4 py-2">取消</button>
        <button value="save" class="rounded-lg bg-blue-600 text-white px-4 py-2">保存</button>
      </div></form>`;
    const input = dialog.querySelector("input");
    const remark = dialog.querySelector("textarea");
    input.value = session.definition?.metadata?.name || "报表草稿";
    remark.value = session.draftRemark || "";
    dialog.querySelector("form").addEventListener("submit", (event) => {
      if (!input.value.trim()) {
        event.preventDefault();
        input.setCustomValidity("请输入草稿名称");
        input.reportValidity();
      }
    });
    input.addEventListener("input", () => input.setCustomValidity(""));
    dialog.querySelector("[data-cancel]").addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => {
      const confirmed = dialog.returnValue === "save";
      const name = confirmed ? input.value.trim() : null;
      const note = confirmed ? remark.value.trim() : "";
      dialog.remove();
      resolve(name ? { name, remark: note } : null);
    }, { once: true });
    document.body.appendChild(dialog);
    dialog.showModal();
    input.select();
  });
}

/**
 * 「同意」之后弹**保存草稿**对话框：让用户起名、写备注。
 *
 * 同意那一轮服务端已经把定义落库，也把草稿对象建好了（`draftId` 在
 * `domainObjects.draftWorkspace` 里）。这里只补「叫什么名字 / 什么备注」
 * —— 走的是既有的草稿动作，不另起一套提交逻辑。
 *
 * 取消**不算失败**：定义已经确认落库，对话框取消只是这次没改名，
 * 用户还能在右侧面板再点「保存草稿」。
 */
async function promptSaveConfirmedDraft() {
  if (!session.draftId) return;
  const draft = await requestDraftName("SAVE");
  if (!draft) return;
  await runDraftAction("SAVE", { name: draft.name, remark: draft.remark });
}

/**
 * 打开草稿列表：只显示**名称与最近修改时间**（契约 0002 §6.2）。
 */
async function openDraftPicker(manage = false) {
  let drafts = [];
  try {
    drafts = await draftApi.list();
  } catch (err) {
    toast("打开草稿列表失败：" + (err.message || err));
    return;
  }
  const dialog = document.createElement("dialog");
  dialog.className = "rounded-xl border border-slate-200 p-5 shadow-xl";
  dialog.style.cssText = "width:min(640px,calc(100vw - 32px));max-height:80vh;overflow:auto;";
  dialog.innerHTML = `<div class="flex justify-between items-center gap-4 mb-4">
    <h2 class="text-lg font-semibold">${manage ? "管理草稿" : "打开草稿"}</h2>
    <button type="button" data-close class="rounded-lg border px-3 py-1">关闭</button>
    </div><div data-draft-list class="space-y-3"></div>`;
  const list = dialog.querySelector("[data-draft-list]");
  const renderList = () => {
    list.replaceChildren();
    if (!drafts.length) list.textContent = "还没有草稿。";
    for (const draft of drafts) {
      const row = document.createElement("div");
      row.className = "flex items-center gap-3 rounded-lg border border-slate-200 p-3";
      const info = document.createElement("div");
      info.className = "flex-1 min-w-0 break-words";
      const title = document.createElement("div");
      title.className = "font-medium";
      title.textContent = (draft.name || "未命名草稿") + (draft.draftId === session.draftId ? "（当前打开）" : "");
      const time = document.createElement("div");
      time.className = "text-xs text-slate-500 mt-1";
      time.textContent = (draft.updatedAt || "").replace("T", " ");
      info.append(title, time);
      row.appendChild(info);
      const open = document.createElement("button");
      open.type = "button";
      open.className = "rounded-lg border px-3 py-1 text-sm";
      open.textContent = "打开";
      open.addEventListener("click", async () => {
        open.disabled = true;
        if (await openDraft(draft.draftId)) dialog.close();
        else open.disabled = false;
      });
      row.appendChild(open);
      if (manage) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "rounded-lg border px-3 py-1 text-sm text-red-600 disabled:opacity-40";
        remove.textContent = "删除";
        remove.disabled = draft.draftId === session.draftId;
        remove.title = remove.disabled ? "请先打开其他草稿或新建对话，再删除这份草稿" : "删除这份草稿";
        remove.addEventListener("click", async () => {
          if (!window.confirm(`确定删除「${draft.name || "未命名草稿"}」？草稿及修改历史将永久删除，对话记录保留。`)) return;
          remove.disabled = true;
          open.disabled = true;
          try {
            await draftApi.remove(draft.draftId);
            drafts = drafts.filter((item) => item.draftId !== draft.draftId);
            renderList();
            toast("草稿已删除");
          } catch (err) {
            toast("删除失败：" + (err.message || err));
            remove.disabled = false;
            open.disabled = false;
          }
        });
        row.appendChild(remove);
      }
      list.appendChild(row);
    }
  };
  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  renderList();
  document.body.appendChild(dialog);
  dialog.showModal();
}

/**
 * 打开一个草稿：把工作台**切到这份草稿的现场会话**。
 *
 * 「现场」= 这份草稿现在停在哪一步 —— 它的对话、待决菜单、状态。
 * 服务端在响应里告诉我们切到了哪条会话（`sessionId`），这边把那条会话
 * 整个恢复回来：右边换成这份草稿，左边回到它自己的对话现场。
 *
 * **不自己另开一条空会话**：空会话没有对话（连会话列表里都不出现），
 * 打开后左边一片空白，而真正有对话的那条会话被落在了后面。
 */
async function openDraft(draftId) {
  if (activeRequest || advanceTimers.size) {
    toast("请先等当前请求结束，再打开草稿。");
    return false;
  }
  try {
    // 先让服务端**切对象身份**，并问出这份草稿的现场会话是哪一条：
    // 切完之后后续对话提交落到这个草稿上，而不是还挂在原来那个对象上
    // （原草稿一个字节都不改）。
    //
    // **不传会话**：服务端会切到那份草稿的**现场会话**（有对话的那条）。
    // 自己指定一条会话反而会把草稿搬离它的对话现场 —— 左边一片空白。
    let state = await draftApi.open(draftId, "");
    if (!state) {
      toast("这个草稿不存在。");
      return false;
    }
    let targetSessionId = String(state.sessionId || "").trim();
    if (!targetSessionId) {
      // 这份草稿还没有任何会话（从没在对话里用过）：给它开一条。
      // 绑定是后续那一轮写锁与任务登记的前提（契约 0002 §7）——
      // 后端是按 `会话 → 草稿` 找写锁对象的（`load_draft_id`），
      // 没有绑定的话，打开草稿后再提修改要求就**不受写锁保护**。
      try {
        const created = await sessionApi.create();
        targetSessionId = created.sessionId;
        state = await draftApi.open(draftId, targetSessionId);
      } catch (err) {
        toast("创建会话失败：" + err.message);
        return false;
      }
    }
    // 把工作台换到那条会话：对话、待决菜单、状态一起回来。
    // **状态与菜单始终是会话自己的** —— 服务端只在「这条会话还是空的」
    // 时把草稿的现场写进去，之后一律以会话为准。
    if (targetSessionId) await restoreSession(targetSessionId);
    applyWorkspaceState(state);
    if (state.definition) {
      session.definition = state.definition;
      session.baseDefinitionRevision = state.definition?.metadata?.revision ?? null;
    }
    // 载荷用**打开接口返回的那一份**：示意载荷不落库（ADR 0004），
    // 会话里没有；再向会话要会把刚打开的草稿内容覆盖回去。
    session.reportPreviewPayload = state.reportPreviewPayload || null;
    session.reportPreviewError = state.reportPreviewError || null;
    renderReportPanel();
    updateMeta();
    if (readPaneMode() === "chat") applyPaneMode("split");
    toast("已打开草稿「" + (state.name || draftId) + "」");
    return true;
  } catch (err) {
    toast("打开草稿失败：" + (err.message || err));
    return false;
  }
}

/** 把服务端给的草稿状态收进 session（撤销能力、版本、主题）。 */
function applyWorkspaceState(state) {
  if (!state || typeof state !== "object") return;
  if (state.draftId) session.draftId = state.draftId;
  if (state.revision !== undefined && state.revision !== null) session.draftRevision = state.revision;
  if (state.savedRevision !== undefined) session.draftSavedRevision = state.savedRevision;
  if (state.description !== undefined) session.draftRemark = state.description || "";
  if (state.canUndo !== undefined) session.canUndo = !!state.canUndo;
  if (state.canRedo !== undefined) session.canRedo = !!state.canRedo;
  // 主题要**完整**才生效：只有 themeId 的残缺主题没有 token，
  // 拿它盖住已解析好的主题会让外观退回默认（契约 0002 §8）。
  if (state.resolvedTheme && state.resolvedTheme.tokens) {
    session.resolvedTheme = state.resolvedTheme;
  } else if (state.themeId && !session.resolvedTheme) {
    session.resolvedTheme = { themeId: state.themeId };
  }
  restoreStoppedTurn(state.activeTask);
}

/**
 * 把「被停掉的那一轮」恢复到界面上。
 *
 * 服务端把还没收尾的任务放在 `activeTask` 里（**STOPPED 也在里面**，
 * 契约 0002 §7）：被停掉的那一轮用户还没选「继续」或「撤销」，
 * 对象也还锁着。刷新之后如果前端不认这个字段，用户看到的就是
 * 「输入能打字、但服务端在锁着」——既看不到那两个选项，
 * 也以为已经停干净了（实际那把写锁还在）。
 *
 * 所以这里按 activeTask 恢复现场：填回 taskId、画出两个选项、锁住输入。
 */
function restoreStoppedTurn(activeTask) {
  const task = activeTask && typeof activeTask === "object" ? activeTask : null;
  const stopped = task && String(task.state || "").toUpperCase() === "STOPPED" && task.taskId;
  if (!stopped) {
    // 没有待处理的停止现场：把界面上残留的选项清掉。
    if (session.stoppedTaskId) {
      session.stoppedTaskId = null;
      session.stoppedChoices = [];
      document.querySelector("[data-wp-stopped]")?.remove();
      syncInputControls();
    }
    return;
  }
  if (session.stoppedTaskId === task.taskId) return;
  session.stoppedTaskId = task.taskId;
  session.stoppedChoices = ["继续完成剩下的工作", "撤销（回到上一步）"];
  renderStoppedTurn();
  syncInputControls();
}

/**
 * 走一次草稿动作（撤销 / 重做 / 保存 / 另存为）。
 *
 * 动作在**服务端**做：前端不改定义，只把服务端返回的权威状态收回来重画。
 *
 * 撤销 / 重做**还要把左侧的会话现场整份回退**（BUG-008）：
 * 只重画右边的话，左边还留着被撤销那一步的摘要、旧菜单与旧候选 ——
 * 右边已经撤了，左边还说「生效成功」，用户看到的是两份互相矛盾的现场。
 */
async function runDraftAction(action, payload = {}) {
  if (!session.draftId) {
    toast("这份报表还没有草稿对象，先做一次修改。");
    return;
  }
  if ((action === "SAVE" || action === "SAVE_AS") && payload.name === undefined) {
    const draft = await requestDraftName(action);
    if (!draft) return;
    payload = { ...payload, name: draft.name, remark: draft.remark };
  }
  try {
    const result = await draftApi.action(session.draftId, action, {
      expectedRevision: session.draftRevision ?? undefined,
      ...payload,
    });
    applyWorkspaceState(result);
    if (result?.definition) {
      session.definition = result.definition;
      session.baseDefinitionRevision = result.definition?.metadata?.revision ?? null;
    }
    if (result?.reportPreviewPayload) {
      session.reportPreviewPayload = result.reportPreviewPayload;
      session.reportPreviewError = null;
    } else if (action === "UNDO" || action === "REDO") {
      await refreshPreviewPayload();
    }
    if (action === "UNDO" || action === "REDO") {
      await restoreSessionSceneAfterHistoryAction(action, result);
      // 场景恢复用的是会话快照；**动作响应里那一份**才是这次动作的权威结果，
      // 重新盖上去（会话绑到别的草稿对象时快照可能是旧的，不能拿它覆盖）。
      if (result?.definition) {
        session.definition = result.definition;
        session.baseDefinitionRevision = result.definition?.metadata?.revision ?? null;
      }
      if (result?.reportPreviewPayload) {
        session.reportPreviewPayload = result.reportPreviewPayload;
        session.reportPreviewError = null;
      }
    }
    renderReportPanel();
    updateMeta();
    const label = { UNDO: "已撤销上一步", REDO: "已重做", SAVE: "草稿已保存", SAVE_AS: "已另存为新草稿" };
    toast(label[action] || "完成");
  } catch (err) {
    toast("操作失败：" + (err.message || err));
  }
}

// --------------------------------------------------------------------------
// 撤销 / 重做之后：左侧会话现场的整份恢复（BUG-008）
// --------------------------------------------------------------------------
//
// **权威现场从服务端来**，前端不自己算「哪一步被撤销了」：
//
//   1. 动作响应里的 `sessionScene`（契约 §2.3 第 3 条，优先）；
//   2. 退一步：`GET /api/sessions/{id}` 的 `pendingChoice` 与 `messages`
//      （既有权威接口 —— 会话快照与消息历史都在那里）。
//
// 前端**不自己决定**能不能撤销、不自己清锁、不自己算失效步骤：
// 服务端没给失效清单时，前端只保证「旧 dropdown 不能再点」
// （只有当前有效菜单那一个能点），**不写**「已撤销」——
// 不拿猜测当事实。

/** 这条消息是不是被服务端标成了「已撤销 / 只读」。 */
function isRevokedMessagePayload(payload) {
  return payload?.revoked === true;
}

/** 权威现场里被撤销的步骤 id（服务端给的清单）。 */
function revokedStepIdsOf(actionResult) {
  const raw = actionResult?.revokedStepIds;
  if (!Array.isArray(raw)) return [];
  return raw.map((id) => String(id || "").trim()).filter(Boolean);
}

/** 一条消息带出来的步骤 id（它画出来的那些卡片各自属于哪一步）。 */
function revokedStepIdsOfPayload(payload) {
  const out = [];
  const questions = Array.isArray(payload?.questions) ? payload.questions : [];
  for (const question of questions) {
    const stepId = String(question?.stepId || "").trim();
    if (stepId) out.push(stepId);
  }
  return out;
}

/**
 * 把服务端给的现场归一成前端要的那一份。
 *
 * 失效步骤两个来源：
 *   1. 动作响应里的 `revokedStepIds`（服务端在撤销那一笔事务里给的）；
 *   2. 会话历史里被标成已撤销的消息（`payload.revoked`）——
 *      刷新后靠它保持一致（消息文本保留，只是那一步失效）。
 *
 * 当前有效菜单：会话的 `pendingChoice`（服务端的权威待决选择）。
 * 撤销之后服务端已经把它清掉了 → 这时**不锁**旧的菜单。
 */
function authoritativeSceneOf(actionResult, sessionDetail) {
  const revokedStepIds = new Set(revokedStepIdsOf(actionResult));
  const messages = Array.isArray(sessionDetail?.messages) ? sessionDetail.messages : [];
  for (const message of messages) {
    if (!isRevokedMessagePayload(message?.payload)) continue;
    for (const stepId of revokedStepIdsOfPayload(message.payload)) revokedStepIds.add(stepId);
  }
  return {
    // 当前有效菜单：服务端说没有待决选择就是没有（撤销之后正是如此）。
    pendingQuestion: sessionDetail?.pendingChoice?.question || null,
    state: sessionDetail?.state || null,
    revokedStepIds: Array.from(revokedStepIds),
    // 服务端有没有给出失效清单：没有的话前端**不写**「已撤销」。
    hasRevokedList: revokedStepIds.size > 0,
  };
}

/**
 * 把被撤销的那一步在界面上标出来：**保留消息文本**，但旧控件失效。
 *
 * - 下拉框 / 输入框 / 按钮一律 disabled（不能再选、不能再提交）；
 * - 挂一条「已撤销」标记，明确说这一步已经不算数了；
 * - 消息正文与更早的历史**一个字都不删**（契约 §2.3 第 2 条）。
 */
function applyRevokedSteps(stepIds) {
  const ids = new Set((stepIds || []).map((id) => String(id || "").trim()).filter(Boolean));
  if (!ids.size) return 0;
  let marked = 0;
  document.querySelectorAll("[data-step-id]").forEach((el) => {
    const stepId = el.getAttribute("data-step-id");
    if (!stepId || !ids.has(stepId)) return;
    if (el.getAttribute("data-revoked") === "1") return;
    el.setAttribute("data-revoked", "1");
    el.classList.add("wp-revoked");
    // 旧控件失效：不能选、不能提交。
    el.querySelectorAll("select, input, textarea, button").forEach((control) => {
      control.disabled = true;
      control.classList.add("opacity-50");
      // 同步控件状态时（syncInputControls）要认这个标记 ——
      // 否则重新同步一次会把旧 dropdown 又打开。
      if (control.dataset) control.dataset.revoked = "1";
    });
    // 明确标为「已撤销」（历史保留，不删消息）。
    const badge = document.createElement("div");
    badge.className = "wp-revoked-badge";
    badge.setAttribute("data-revoked-badge", "1");
    badge.textContent = "已撤销";
    el.appendChild(badge);
    marked += 1;
  });
  return marked;
}

/**
 * 受限步骤的锁**只由服务端解除**。
 *
 * 服务端这一轮可能只回了一段文字（没有新的菜单）—— 那**不代表**这一步
 * 结束了（BUG-003：提了修改要求之后回了一段分析，回复完普通输入就开了）。
 * 前端不自己判断「结束了没」，只向服务端问一句「现在还有没有待决步骤」，
 * 按服务端的答案上锁 / 解锁（契约 §3.3：「没有候选」「返回了一段文字」
 * 不是解锁理由）。
 */
async function syncChoiceLockFromServer() {
  const sessionId = session.sessionId;
  if (!sessionId) return;
  let detail = null;
  try {
    detail = await sessionApi.detail(sessionId);
  } catch (err) {
    // 问不到就不动锁：宁可锁着（后端仍可能锁着），也不擅自放开。
    return;
  }
  const pending = detail?.pendingChoice?.question || null;
  if (!pending) {
    // 服务端说这一步不在了 → 放锁。
    clearChoiceLock();
    return;
  }
  // 服务端说这一步还在 → 把那张卡画出来（专用输入要真的在屏幕上，
  // 用户才有地方作答），并按它上锁。
  appendAiQuestions([pending], { lock: true });
}

/**
 * 撤销 / 重做之后：把**左侧会话现场**整份恢复成服务端的权威状态。
 *
 * 做法：按服务端的会话详情把左侧重画一遍（历史保留、菜单按权威阶段恢复、
 * 锁按服务端那份菜单重新上），再把被撤销的步骤标出来。
 */
async function restoreSessionSceneAfterHistoryAction(action, result) {
  const sessionId = session.sessionId;
  if (!sessionId) return;
  let detail = null;
  try {
    detail = await sessionApi.detail(sessionId);
  } catch (err) {
    // 拿不到权威现场就**不动**左侧：宁可保持原样，也不自己编一份现场。
    return;
  }
  if (!detail) return;
  const scene = authoritativeSceneOf(result, detail);
  await restoreSession(sessionId, { silent: true, detail, scene });
  applyRevokedSteps(scene.revokedStepIds);
  // 服务端没有给出失效清单：前端**不写**「已撤销」（不拿猜测当事实），
  // 但要说清楚左侧现在是什么状态 —— 否则右边撤了、左边那条「已生效」
  // 还挂着，用户看到的是矛盾。
  if (!scene.hasRevokedList) {
    appendAiText("已" + (action === "UNDO" ? "撤销" : "重做") + "上一步。服务端没有给出失效步骤清单，左侧历史按当前记录显示，旧步骤的控件已停用。");
  }
}

/**
 * 恢复草稿工作区状态：草稿 id、撤销 / 重做能力、解析好的主题。
 *
 * 快照里只存了 draftId（对象身份）；「能不能撤销」「当前主题是什么」要向
 * **草稿对象**问 —— 前端不自己数历史，也不自己拼主题。
 */
async function restoreDraftWorkspace(detail) {
  const draftId = detail?.snapshot?.draftId || null;
  session.draftId = draftId;
  session.canUndo = false;
  session.canRedo = false;
  if (!draftId) return;
  try {
    const state = await draftApi.detail(draftId);
    applyWorkspaceState(state);
  } catch (err) {
    // 草稿对象取不到（老会话 / 库被换过）：不清空定义，也不报错打扰用户。
    session.canUndo = false;
    session.canRedo = false;
  }
}

/** 按当前定义重新生成示意载荷（打开草稿 / 撤销后用）。 */
async function refreshPreviewPayload(options = {}) {
  if (!session.sessionId) return;
  const keepDefinition = options.keepDefinition === true;
  try {
    const detail = await sessionApi.detail(session.sessionId);
    const payload = detail?.reportPreviewPayload || detail?.snapshot?.reportPreviewPayload;
    if (payload) {
      session.reportPreviewPayload = payload;
      session.reportPreviewError = null;
    }
    const definition = detail?.snapshot?.definition;
    // keepDefinition：刚打开的草稿内容**不能**被会话里的定义覆盖掉。
    if (definition && !keepDefinition) {
      session.definition = definition;
      session.baseDefinitionRevision = definition?.metadata?.revision ?? null;
    }
  } catch (err) {
    /* 载荷拿不到就保留原来的，不清空。 */
  }
}

function renderPanelHeader(definition, payload, regionCount) {
  const header = document.createElement("div");
  header.className = "bg-white rounded-xl border border-slate-200 p-5";
  const name = definition?.metadata?.name || "报表草稿";
  const windowLabel = readPayloadWindowLabel(payload);
  // 来源模式只按服务端给的信号说；拿不到就不写来源说明（宁可少说，不许说假话）。
  const sourceMode = readReportSourceMode(definition, payload);
  const sourceNote = renderSourceNoteHtml(sourceMode);
  header.innerHTML = `
    <div class="flex items-start justify-between gap-4">
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2 flex-wrap">
          <h2 class="report-name editable-field text-lg font-semibold text-slate-900">${esc(name)}</h2>
          <span class="inline-flex items-center gap-1 rounded-md text-xs font-medium px-2 py-1 border bg-amber-50 text-amber-700 border-amber-200 draft-status-badge">
            <iconify-icon class="text-sm" icon="mdi:robot-outline"></iconify-icon>${esc(definitionStatusLabel(definition?.metadata?.status))}
          </span>
          <span class="edit-hint text-[11px] text-slate-400 inline-flex items-center gap-1"><iconify-icon icon="mdi:pencil-outline"></iconify-icon>点击名称可直接编辑</span>
        </div>
        <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500 draft-meta-row">
          <span>草稿版本: <span class="mono">${definition?.metadata?.revision ?? "—"}</span></span>
          <span>卡片数: <span class="mono">${regionCount}</span></span>
          ${windowLabel ? `<span>时间范围: <span class="mono">${esc(windowLabel)}</span></span>` : ""}
        </div>
        ${renderGenerationInputs(definition)}
      </div>
    </div>
    ${sourceNote ? `
    <div class="mt-3 flex items-start gap-1.5 text-[11px] leading-relaxed ${sourceMode === "simulated" ? "text-slate-500" : "text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1.5"}" data-simulated-note="${esc(sourceMode)}">
      <iconify-icon class="text-sm mt-px" icon="mdi:flask-outline"></iconify-icon>
      ${sourceNote}
    </div>
    ` : ""}
    ${renderJsonInputIssuesHtml(definition)}
  `;

  // 草稿工具条（撤销 / 重做 / 显示隐藏 / 保存 / 另存为 / 打开草稿）。
  //
  // 用 typeof 守卫：有些既有单测把 renderPanelHeader 单独抽进沙箱跑，
  // 那里没有 renderDraftToolbar —— 直接调用会 ReferenceError。
  // 拿不到工具条就不画（**不是**功能降级：真实页面里它一直在）。
  if (typeof renderDraftToolbar === "function") {
    const toolbar = renderDraftToolbar();
    if (toolbar) header.appendChild(toolbar);
  }

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
  // 来源模式（契约表 §1）：只按服务端给的信号说。
  //
  // 这里的 typeof 兜一下是给「接缝探针 / 既有单测把本函数单独抽进沙箱」用的：
  // 那边没有模块级的 readSourceMode。缺了就当「拿不到来源模式」——
  // 拿不到就不显示来源角标（宁可少说，不许说假话）。
  const sourceMode = typeof readSourceMode === "function"
    ? readSourceMode(definition, region, p, defRegion)
    : "";
  const capabilityNotes = readCapabilityNotes(region, p, defRegion, sourceMode);
  // 用户 JSON 模式的角标说明。
  // 这里写死字面量：本函数会被接缝探针与既有单测**单独抽进沙箱**跑，
  // 那边没有模块级的符号可用。同一句话的权威副本在
  // workbench_query.js 的 USER_JSON_SOURCE_LABEL，两边一致由
  // workbench_66b_frontend.test.mjs 盯着。
  const USER_JSON_BADGE_NOTE = "用户提供的数据，未经业务核验";

  // 卡片作用 / 展示形式 / 图表类型 → 中文标签。
  //
  // 这三张表**写死在本函数内**（理由同 USER_JSON_BADGE_NOTE）：本函数会被
  // 接缝探针与既有单测**单独抽进沙箱**跑，那边没有模块级的符号可用。
  //
  // 原来的写法是直接渲染 `role: SUMMARY · component: METRIC_CARD` ——
  // 那是内部枚举，用户读不懂，等于把内部标识摆在界面上。改成业务词：
  //   - 已知取值 → 中文；
  //   - 未知取值 → **不回落英文 key**（英文 key 正是要藏的东西），不渲染这一段。
  //
  // 这里刻意**不复用** workbench_plan.js 的 `componentTypeLabel`：那份的键是小写
  // （metric_card），载荷里是大写（METRIC_CARD），两套取值域不一样，各自维护。
  const ROLE_LABELS = {
    REPORT_TITLE: "报表标题",
    SUMMARY: "总览",
    TREND: "趋势",
    DETAIL: "明细",
    TOTAL: "汇总",
  };
  const TYPE_LABELS = {
    METRIC_CARD: "指标卡",
    CHART: "图表",
    TABLE: "表格",
    TEXT: "文字块",
    TEXT_HEADING: "标题",
    SINGLE_NUMBER: "单值",
    TIME_SERIES: "时间序列",
    CATEGORY_RANKING: "分类排行",
    DESCRIPTION: "说明",
  };
  const KIND_LABELS = {
    LINE: "折线图",
    BAR: "柱状图",
    PIE: "饼图",
  };
  const labelOf = (labels, raw) => {
    const key = String(raw == null ? "" : raw).trim().toUpperCase();
    if (!key) return "";
    return Object.prototype.hasOwnProperty.call(labels, key) ? labels[key] : "";
  };

  const card = document.createElement("div");
  card.className = "bg-white rounded-xl border border-slate-200 p-5 group";
  card.dataset.regionId = region.regionId || "";

  const title = region.name || defRegion?.name || region.componentType || "板块";
  const chartKind = p.chartKind || defRegion?.component?.chartKind || "";
  // 卡片头部那一行说明：「作用：总览 · 展示形式：折线图」。
  const roleText = labelOf(ROLE_LABELS, region.role || defRegion?.role);
  const kindText = labelOf(KIND_LABELS, chartKind);
  // 图表卡片：chartKind 比 componentType 更具体，用它替掉泛泛的「图表」。
  const formText = kindText || labelOf(TYPE_LABELS, region.componentType || defRegion?.component?.type);
  const metaParts = [];
  if (roleText) metaParts.push("作用：" + esc(roleText));
  if (formText) metaParts.push("展示形式：" + esc(formText));
  const metaHtml = metaParts.length
    ? `<p class="text-xs text-slate-500 mt-0.5">${metaParts.join(" · ")}</p>`
    : "";
  const header = `
    <div class="flex items-start justify-between gap-4">
      <div class="flex-1 min-w-0">
        <h3 class="region-name editable-field font-semibold text-slate-900">${esc(title)}</h3>
        ${metaHtml}
      </div>
      <div class="flex items-center gap-1 shrink-0">
        ${sourceMode === "user_json" ? `
        <span class="region-badge inline-flex items-center gap-1 rounded-md text-[11px] font-medium px-2 py-0.5 border bg-slate-100 text-slate-600 border-slate-200" data-region-badge="user_json" title="${esc(USER_JSON_BADGE_NOTE)}">
          <iconify-icon class="text-xs" icon="mdi:database-outline"></iconify-icon>用户数据
        </span>
        ` : sourceMode === "simulated" ? `
        <span class="region-badge inline-flex items-center gap-1 rounded-md text-[11px] font-medium px-2 py-0.5 border bg-slate-100 text-slate-600 border-slate-200" data-region-badge="simulated" title="模拟业务数据，仅用于核对版式">
          <iconify-icon class="text-xs" icon="mdi:flask-outline"></iconify-icon>示意
        </span>
        ` : ""}
        ${defRegion ? `
        <span class="region-tools">
          <button class="btn-display-options h-7 rounded border border-slate-200 hover:bg-blue-50 hover:border-blue-200 hover:text-blue-700 text-[11px] text-slate-500 px-2 transition" title="查看适合当前数据的展示方式">展示方式</button>
          <button class="btn-grain-options h-7 rounded border border-slate-200 hover:bg-blue-50 hover:border-blue-200 hover:text-blue-700 text-[11px] text-slate-500 px-2 transition" title="查看当前数据源支持的粒度">粒度</button>
          <button class="btn-del-region w-7 h-7 rounded border border-slate-200 hover:bg-rose-50 hover:border-rose-200 hover:text-rose-600 text-slate-400 flex items-center justify-center transition" title="删除此区域">
            <iconify-icon class="text-base" icon="mdi:trash-can-outline"></iconify-icon>
          </button>
        </span>
        ` : ""}
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

  // 载荷自带的说明文字（JSON 模式：契约表 §4.3 的 `payload.notes`）。
  //
  // 服务端把「共 N 行，已显示前 200 行；汇总计算仍按全部 N 行。」、
  // 「这个指标没能绑定到输入字段，没有编值。」这类**实话**写在了载荷里；
  // 前端只把原文端出来 —— 不改写、不总结、不自己编
  // （契约表 §8 不变量 8：宁可少说，不许说假话）。
  //
  // 样式沿用页面既有的说明文字（与「能力待接通」那条同一个色板），不新造一套。
  const payloadNotes = typeof readRegionDisplayNotes === "function"
    ? readRegionDisplayNotes(p, sourceMode)
    : (Array.isArray(p.notes) ? p.notes : (p.notes == null ? [] : [p.notes]))
        .map((item) => String(item == null ? "" : item).trim()).filter(Boolean);
  if (payloadNotes.length) {
    const noteBox = document.createElement("div");
    noteBox.className =
      "region-payload-notes text-[11px] leading-relaxed text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1 mt-2";
    noteBox.dataset.regionPayloadNotes = "true";
    for (const line of payloadNotes) {
      const lineEl = document.createElement("div");
      lineEl.textContent = line;
      noteBox.appendChild(lineEl);
    }
    body.appendChild(noteBox);
  }

  // 「能力待接通 / 不在授权范围」：角标之外额外写明哪些指标还没接通、为什么。
  // 文案按原因分好类（授权类不是能力缺失）—— 不承诺计算口径或公式，
  // 那不是前端能说的事。
  if (capabilityNotes.length) {
    const note = document.createElement("div");
    note.className = "region-capability-note text-[11px] leading-relaxed text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1 mt-2";
    note.dataset.regionCapability = "pending";
    note.textContent = capabilityNotes.join("；");
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
    sendTemplateEditAction("这张卡片可以怎么展示？", defRegion.id);
  });
  card.querySelector(".btn-grain-options")?.addEventListener("click", () => {
    sendTemplateEditAction("这个数据能按哪些粒度编译？", defRegion.id);
  });

  // 区域删除
  card.querySelector(".btn-del-region")?.addEventListener("click", () => {
    if (!confirm(`确定删除「${originalName}」吗？`)) return;
    sendTemplateEditAction("删除这张卡片", defRegion.id);
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
    // 权威格式：服务端写进定义里的那一份 region.format（契约 0002 §3.5 / BUG-010）。
    // 服务端已经按它算好了 displayValue；这里优先用服务端的，
    // 拿不到（例如用户 JSON 模式）才按同一份格式本地渲染。
    //
    // typeof 守卫：这段渲染函数会被既有单测单独抽进沙箱跑，那边没有
    // authoritativeDisplayValue —— 跳过即可（**不是**功能降级：
    // 真实页面里它与主脚本同文件，一定在）。
    const metrics = Array.isArray(p.metrics) ? p.metrics.filter((m) => m && typeof m === "object") : [];
    const items = metrics.length ? metrics : [{
      metricKey: p.metricKey, label: p.label || "指标", value: p.value,
      precision: p.precision, unit, placeholderOnly: p.placeholderOnly,
      displayValue: p.displayValue,
    }];
    const cards = items.map((m) => {
      const hasValue = p.placeholderOnly !== true && m.placeholderOnly !== true && m.value != null && m.value !== "";
      // 显示串优先用**服务端按权威格式算好的** displayValue（契约 0002 §5）：
      // 精度、千分位、单位标签都在服务端那一份格式里，
      // 前端不再自己拼一套 —— 两边各拼一次迟早对不上。
      const formatted = hasValue && typeof authoritativeDisplayValue === "function"
        ? authoritativeDisplayValue(m.value, ctx.defRegion, p, m.unit || unit, m.displayValue)
        : null;
      // 服务端已经按权威格式算好的显示串（最高优先）。
      const serverDisplay = m.displayValue != null && m.displayValue !== "" ? String(m.displayValue) : null;
      const display = formatted !== null
        ? esc(formatted)
        : serverDisplay !== null
          ? esc(serverDisplay)
          : `${esc(formatMetricValue(m.value, m.precision))}<span class="text-[11px] font-normal text-slate-400 ml-1">${esc(m.unit || "")}</span>`;
      return `
          <div class="rounded-lg border border-slate-200 bg-white px-3 py-2" data-region-metric="${esc(m.metricKey || m.label || "")}">
            <div class="text-[11px] text-slate-500">${esc(m.label || m.metricKey || "指标")}</div>
            <div class="text-base font-semibold text-slate-900 mt-0.5 min-h-6">${hasValue ? display : ""}</div>
          </div>`;
    }).join("");
    container.innerHTML = `
      <div class="grid gap-2 sm:grid-cols-2">${cards}</div>`;
    return;
  }

  if (type === "TABLE") {
    const columns = Array.isArray(p.columns) ? p.columns : [];
    const rows = Array.isArray(p.rows) ? p.rows : [];
    // 逐格显示串：服务端按权威格式算好的（{fieldRef: 显示串} 每行一份）。
    // 有它就用它，没有才退回旧的本地拼法。
    const displayRows = Array.isArray(p.displayRows) ? p.displayRows : null;
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
            // 这一格**没有值**：留空。不拿 0 / NaN / undefined 冒充一个数
            // （用户明确说「先占位」的列就是这样：列名在，数值不在）。
            if (val == null || val === "") {
              return `<td class="px-3 py-2.5 text-slate-700"></td>`;
            }
            const display = displayRows && displayRows[idx]
              ? displayRows[idx][c.fieldRef]
              : undefined;
            if (display != null && display !== "") {
              return `<td class="px-3 py-2.5 text-slate-700 mono">${esc(String(display))}</td>`;
            }
            // 列自己的格式优先，列没声明才继承区域这一份（服务端同一口径）。
            // typeof 守卫：这段渲染函数会被既有单测单独抽进沙箱跑，
            // 那边没有 authoritativeDisplayValue —— 跳过即可。
            const columnFmt = c.format && typeof c.format === "object" ? c.format : null;
            const formatted = typeof authoritativeDisplayValue === "function"
              ? authoritativeDisplayValue(val, ctx.defRegion, p, "", display, columnFmt)
              : null;
            if (formatted !== null) {
              return `<td class="px-3 py-2.5 text-slate-700 mono">${esc(formatted)}</td>`;
            }
            if (c.format?.kind === "NUMBER") {
              const num = Number(val);
              const text = Number.isFinite(num) ? num.toFixed(c.format.precision ?? 1) : String(val);
              return `<td class="px-3 py-2.5 text-slate-700 mono">${esc(text)}</td>`;
            }
            return `<td class="px-3 py-2.5 text-slate-700">${esc(val)}</td>`;
          })
          .join("");
        return `<tr class="hover:bg-slate-50">${p.rowNumber ? `<td class="px-3 py-2.5 text-slate-400 mono">${idx + 1}</td>` : ""}${cells}</tr>`;
      })
      .join("");

    // 行被截断时必须说清楚：不然「各行之和 ≠ 报表总量」会变成用户眼里的矛盾。
    const truncation = readTableTruncation(p, ctx.defRegion, ctx.totalsByUnit);
    // 截断的原因按「判据从哪来」说：
    //   - 定义侧（模拟模式）：行数被截断，行和因此小于报表总量；
    //   - JSON 模式：明细表最多显示 200 行（契约表 §4.3），行数本身就被截断了
    //     （汇总仍按全部行算，行和不是这里的判据）。
    // 两条**共用同一条提示条**（data-table-truncated），不另造一条。
    const truncationReason = truncation?.source === "json"
      ? `共 ${truncation.total} 行，表格行数被截断`
      : "表格行数被截断，各行之和小于报表总量";
    const truncationNote = truncation
      ? `<div class="text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1 mt-2" data-table-truncated="true">仅显示前 ${truncation.limit} 行（${truncationReason}）</div>`
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
// 饼图渲染：**扇区**，不是折线也不是柱条。
//
// 两条渲染后端，同一套数据：
//   - ECharts 可用 → 真饼图（type:"pie"，每个分类一个扇区，标签带分类名与数值）；
//   - ECharts 不可用 → SVG 兜底**也画扇区**（<path> 的圆弧），不是横条。
//
// 数值是**模拟**时，图下要标出来（载荷里的 simulated 标记）。
function renderPiePayload(container, region, p, labels, values, axisLabel, unit) {
  const simulated = p.simulated === true;
  const name = String(p.metrics?.[0]?.label || p.categoryLabel || region.name || "数值");

  if (hasEcharts()) {
    const div = document.createElement("div");
    div.style.height = "260px";
    container.appendChild(div);
    try {
      const chart = window.echarts.init(div);
      _chartInstances.push(chart);
      const data = labels.map((label, index) => ({
        name: String(label),
        value: Number(values[index] ?? 0),
      }));
      chart.setOption({
        tooltip: {
          trigger: "item",
          formatter: (params) => {
            const value = params && params.value != null ? params.value : 0;
            const suffix = unit ? ` ${unit}` : "";
            return `${params.name}<br/>${value}${suffix}`;
          },
        },
        // 饼图没有坐标轴 —— 不给 xAxis / yAxis，分类就是扇区。
        series: [
          {
            name,
            type: "pie",
            radius: ["38%", "68%"],
            center: ["50%", "52%"],
            avoidLabelOverlap: true,
            itemStyle: { borderColor: "#fff", borderWidth: 2 },
            label: {
              formatter: (params) => `${params.name}\n${params.value}${unit ? " " + unit : ""}`,
              fontSize: 11,
              color: axisLabel.color,
            },
            labelLine: { show: true },
            data,
          },
        ],
      });
      appendChartNote(container, p, unit, labels.length, simulated);
      return;
    } catch (err) {
      console.error("[AI Report] 饼图初始化失败", err);
      container.innerHTML = "";
    }
  }

  // ECharts 不可用：SVG 兜底**也画扇区**（不是横条）。
  const api = window.WorkbenchPlan;
  if (api && typeof api.renderChart === "function") {
    try {
      const node = api.renderChart(document, {
        componentType: "pie_chart",
        title: region.name || "",
        data: { xLabels: labels, yValues: values, unit, simulated },
      });
      container.appendChild(node);
      appendChartNote(container, p, unit, labels.length, simulated);
      return;
    } catch (err) {
      console.error("[AI Report] 饼图渲染失败", err);
    }
  }

  // 兜底：至少把点列出来，数据不丢。
  container.innerHTML = `
    <div class="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
      <div class="text-[11px] text-slate-500 mb-1">${esc(unit || "数值")}</div>
      ${labels.map((label, index) => `<div class="text-xs text-slate-700 mono">${esc(label)}: ${esc(values[index] ?? "")}</div>`).join("")}
    </div>`;
}

// 图下的说明行：单位 + 数据点个数 + **模拟标记**。
// 两条渲染后端产出**同一种**可断言标记（data-chart-unit / data-chart-simulated）。
function appendChartNote(container, p, unit, count, simulated) {
  const note = document.createElement("div");
  note.className = "text-[11px] text-slate-400 mt-1";
  if (unit) note.dataset.chartUnit = unit;
  if (simulated) {
    note.dataset.chartSimulated = "true";
    note.textContent = `单位：${unit || "数值"}，共 ${count} 个分类（模拟数据）`;
  } else {
    note.textContent = `单位：${unit || "数值"}，共 ${count} 个分类`;
  }
  container.appendChild(note);
}

function renderChartPayload(container, region, p, unit) {
  const kind = String(p.chartKind || "LINE").toUpperCase();
  // 横轴标签：优先用服务端按权威格式算好的 displayXLabels
  // （比如日期格式 2026/11/03）；没有才退回原始 xLabels。
  const labels = Array.isArray(p.displayXLabels) && p.displayXLabels.length
    ? p.displayXLabels
    : (Array.isArray(p.xLabels) ? p.xLabels : []);
  const values = Array.isArray(p.yValues) ? p.yValues : [];
  const series = Array.isArray(p.series) && p.series.length ? p.series : null;
  // 主题生效时，**图里的文字**也要跟着主题走（BUG-009）：
  // 画布上的坐标轴文字不受 CSS 影响，深色主题下默认的深色字会糊在深底上。
  // 颜色取服务端解析好的主题 token（前端不自己造主题）。
  const themeTextColor = (() => {
    if (typeof session === "undefined" || !session) return "";
    const color = session.resolvedTheme?.tokens?.colorText;
    return typeof color === "string" ? color : "";
  })();
  const axisLabel = themeTextColor ? { fontSize: 11, color: themeTextColor } : { fontSize: 11 };

  if (!labels.length || (!values.length && !(series && series[0]?.yValues?.length))) {
    container.innerHTML = `<div class="rounded-lg bg-slate-50 border border-slate-200 px-4 py-3 text-xs text-slate-500">这个图表还没有数据点。</div>`;
    return;
  }

  // 饼图：**真的画扇区**。
  //
  // 以前 PIE 落到 ``kind === "BAR" ? "bar" : "line"`` 的 else 分支 ——
  // 卡片标题写着「饼图」，画出来却是折线。饼图有自己的一套 option
  // （没有坐标轴，分类是扇区，标签跟着扇区走）。
  if (kind === "PIE") {
    renderPiePayload(container, region, p, labels, values, axisLabel, unit);
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
          axisLabel,
        },
        yAxis: {
          type: "value",
          // 纵轴单位一律从载荷读：PUE、总电费、实时功率都不是 kWh。
          name: unit || undefined,
          nameTextStyle: { fontSize: 10, color: themeTextColor || "#64748b" },
          axisLabel,
        },
        series: seriesOption,
      });
      // 单位标记：两条渲染后端产出**同一种**可断言标记（data-chart-unit）。
      // ECharts 把单位画在纵轴名上（canvas 里），DOM 里抓不到 —— 这里补一个元素，
      // 让「单位来自载荷」在 ECharts 路径下也能被同一条断言抓到。
      // 单位取自载荷（readPayloadUnit 的回退链），绝不写死 kWh / 耗电。
      if (unit) {
        const unitNote = document.createElement("div");
        unitNote.className = "text-[11px] text-slate-400 mt-1";
        unitNote.dataset.chartUnit = unit;
        unitNote.textContent = "单位：" + unit + "，共 " + labels.length + " 个数据点";
        container.appendChild(unitNote);
      }
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
        data: {
          xLabels: labels,
          yValues: values,
          unit,
          // 模拟标记透传：SVG 兜底也要能标出「这是模拟数据」。
          simulated: p.simulated === true,
        },
      });
      container.appendChild(node);
      // 折线渲染器自带「单位：…」说明；柱条那条没有 —— 这里补上，
      // 保证「图上的单位写清楚」这条在两条渲染后端上都成立。
      if (unit && !String(node.textContent || "").includes("单位：")) {
        const note = document.createElement("div");
        note.className = "text-[11px] text-slate-400 mt-1";
        // 标记语义与 ECharts 分支一致：属性的值就是载荷里的单位，
        // 两条渲染后端用同一套选择器断言（渲染内容不变）。
        note.dataset.chartUnit = unit;
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

/**
 * 那张待决菜单**是不是已经画在屏上**了。
 *
 * 不能只看「回放过没有」—— 回放画菜单有三条路径（确认卡 / 待确认计划卡 /
 * 普通问题卡），只有其中两条会登记「画过了」。这里直接查屏上有没有这张卡：
 * 卡的 select 上带 `data-question-id`，按 id 认。
 */
function pendingMenuCardShown(question) {
  const id = String(question?.id || "").trim();
  if (!id) return false;
  return Array.from(document.querySelectorAll(".question-select")).some(
    (el) => el.getAttribute("data-question-id") === id
  );
}

// 还原会话：把消息逐条回放，再恢复状态机位置、报表预览和选择锁
// 返回 { ok, missing }：
//   ok=true            恢复成功
//   ok=false,missing  会话已经不存在（404），自动恢复的落点可以清掉
//   ok=false,其他      忙 / 网络故障 / 被更新的操作作废，落点要留着下次再试
//
// detail / scene 是**外部已经取好的权威现场**：撤销之后左侧要整份恢复时，
// 由调用方把会话详情与权威现场一起带进来，省掉一次重复请求。
// scene.pendingChoice 优先于 detail 里的 pendingChoice —— 动作响应里那份
// 是这次动作的权威现场（契约 §2.3）。
async function restoreSession(sessionId, { silent = false, token = null, detail: preloadedDetail = null, scene = null } = {}) {
  if (isBusy()) {
    if (!silent) toast("请先停止正在生成的请求");
    return { ok: false, missing: false };
  }
  const myToken = token ?? ++sessionRestoreToken;
  let detail = preloadedDetail;
  if (!detail) {
    try {
      detail = await sessionApi.detail(sessionId);
    } catch (err) {
      const missing = /404/.test(err.message || "");
      if (!silent) toast(missing ? "这个会话已经不存在了。" : "打开会话失败：" + err.message);
      return { ok: false, missing };
    }
  }
  // 恢复途中用户点了「新建对话」：这次恢复作废，不要把旧会话画到新会话界面上。
  if (myToken !== sessionRestoreToken) return { ok: false, missing: false };

  clearChoiceLock();
  lastPendingPlan = null;
  businessMenuReplayed = false;
  destroyCharts();
  chatScroll.innerHTML = "";
  // 对话区清空了：屏上的方案卡片（含历史卡）与去重登记一并清掉。
  clearPlanCards();
  previewRoot.innerHTML = "";

  session.sessionId = detail.sessionId;
  session.conversationId = "conv_" + Date.now() + "_" + requestEpoch;
  // 阶段：动作响应里带的权威现场优先（撤销之后要回到被撤销前的那一步）。
  session.state = scene?.state || detail.state;
  session.designSessionId = detail.designSessionId;
  session.reportDefinitionId = detail.reportDefinitionId;
  session.workspaceId = detail.workspaceId;
  session.pendingResume = null;
  // 恢复临时卡片目标：跟服务端业务状态一致（20261102 工单）。
  //   - 还有待确认计划 = 这次操作还没结束：目标取计划自己的 targetRegionId；
  //   - 没有待确认计划 = 操作已经结束：按快照里的临时目标
  //     （服务端在释放时必须把这个字段清掉，否则刷新会带回一张「还选着」的卡）。
  const pendingPlanTargetRegionId = detail.pendingEditPlan?.targetRegionId || null;
  session.editingTargetRegionId =
    pendingPlanTargetRegionId ?? detail.snapshot?.editingTargetRegionId ?? null;
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
  // 恢复失败的原因（#53）：模拟载荷生成不出来时，后端把原因放在
  // snapshot.error 上（code + 人话 message），而不是静默返回空快照。
  // 记下来，由 renderReportPanel 摆在右侧面板顶部，对话区也说一遍 ——
  // 刷新后原因不能只剩一片空白。
  session.reportPreviewError = readSessionRestoreError(detail);
  writeLastSessionId(detail.sessionId);

  // 草稿工作区状态（契约 0002 §6）：草稿 id、能不能撤销 / 重做、解析好的主题。
  // 快照里只存了 draftId；撤销能力与主题要向草稿对象问 —— 前端不自己数历史。
  await restoreDraftWorkspace(detail);

  // 恢复文字方案（#19）：方案与阶段存在服务端快照里，
  // 不依赖聊天正文、debug 或浏览器本地变量。
  restorePlanFromDetail(detail);

  // 选择锁以后端为准：回放时用它替换历史问题对象，
  // 这样回放出来的下拉框与 lockedChoice 是同一个引用，选完能正常继续。
  //
  // 撤销之后：动作响应里带的权威现场优先（契约 §2.3）——
  // 服务端在撤销那一笔事务里作废了待决选择，那份才是「现在该锁什么」。
  const pending = scene && Object.prototype.hasOwnProperty.call(scene, "pendingChoice")
    ? scene.pendingChoice?.question || null
    : detail.pendingChoice?.question || null;

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
      const bubbleMark = aiBubbleCount();
      const menu = replayAssistantMessage(message.payload, pending);
      replayEvents.push({ type: "assistant", response: message.payload, menu: menu || null });
      // 被撤销的那一步：消息文本**保留**，但那一步的控件失效并标成「已撤销」
      // （契约 §2.3 第 2 条）。标记由服务端给的 revoked 决定 —— 前端不自己猜。
      if (isRevokedMessagePayload(message.payload)) {
        applyRevokedSteps(revokedStepIdsOfPayload(message.payload));
      }
      // 历史评价目标（20261104）：**能可靠关联的才挂**。
      // 老消息没有目标 → 明确不支持，不按最新回复补绑。
      const historyTarget = message.feedbackTarget || message.payload?.feedbackTarget || null;
      if (typeof mountFeedbackTarget === "function") mountFeedbackTarget(historyTarget, bubbleMark);
      // 历史 Debug Trace：这一轮**存下来的**逐轮摘要，走与流式**同一套**渲染
      // （renderTraceRoundsFromSummary → addTraceRoundRow）。
      //
      // 画在**这一轮回答之后**：反馈入口按「第几条 AI 气泡」定位，先挂好再追加
      // 气泡，它的落点不会被这张新卡挪走。旧消息没有的字段由渲染函数自己说
      // 「历史 Trace 未记录该详情」，**不**补造轮次或结构。
      appendTraceHistoryCard(message.payload);
    } else {
      appendAiText(message.content);
    }
  }

  // 恢复失败：在对话区说清楚原因（与聊天响应的失败写法一致）。
  // 放在回放之后：这条不是历史消息，是**这一次恢复**的结果。
  if (session.reportPreviewError) appendAgentError(session.reportPreviewError);

  // 刷新后恢复「已反馈」标记（同一个浏览器的匿名身份）。
  // 服务端只返回**自己**提交过的目标，不返回别人的反馈。
  const feedbackModuleApi = feedbackModule();
  if (feedbackModuleApi && feedbackTargetIds.size) {
    feedbackModuleApi.refreshSubmitted(Array.from(feedbackTargetIds));
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
  if (pending) {
    // 服务端说还有待决菜单、但**屏上没有那张卡**（会话是空的 —— 比如刚
    // 打开一个草稿，对话区还没有历史可以回放它）：把它补上。
    // 只有锁没有卡 = 用户看着输入框被锁住，却找不到那张卡可答。
    if (!pendingMenuCardShown(pending)) appendAiQuestions([pending], { lock: true });
    else setChoiceLock(pending);
  } else {
    const openMenu = WorkbenchQuery.resolveOpenMenuAfterReplay(replayEvents);
    // 回放兜底出来的菜单**不是**服务端当前的有效菜单：
    // 刷新后按服务端阶段恢复锁或不锁，不按历史消息推断（规格第 60 条、
    // 契约表 §5.2）。所以这里只把菜单摆出来，不锁输入。
    //
    // `authoritative: false` 就是「这不是当前待决的那张菜单」的标记：
    // 权威状态（后端 pendingChoice）说没有待选项 → 输入与发送保持可用。
    // 有效 dropdown 走上面 `pending` 分支，不带这个标记，全锁不变。
    if (openMenu) {
      setChoiceLock({
        ...openMenu,
        lockScope: LOCK_SCOPE_MENU_ONLY,
        authoritative: false,
      });
    }
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
    return questions.map((q) => {
      if (q?.id !== pending.id) return q;
      // **已经带自己步骤身份**的历史问题不换掉：换掉之后那张旧卡片就
      // 失去了步骤身份，撤销标记（服务端给的 ``revokedStepIds``）找不到
      // 对应卡片，「已撤销」徽标也就挂不出来（BUG-008）。
      //
      // 当前那一步的问题带着**当前**步骤身份（与服务端 pending_choice
      // 一致），照旧换成服务端那份权威菜单：下拉框与 lockedChoice 是
      // 同一个引用，选完能正常继续。
      if (q?.stepId && pending?.stepId && q.stepId !== pending.stepId) return q;
      return pending;
    });
  };

  if (isTemplateEditFailure(data)) {
    appendAgentError(data.error);
    return restoreEditFailureMenu(data, { lock: false, pending });
  }

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
  session.reportPreviewError = null;
  session.pendingResume = null;
  session.workspaceId = null;
  // 新对话还没有自己的 sessionId（要等第一条消息才向后端要），
  // 先把自动恢复的落点清掉，免得刷新后又跳回上一条对话。
  writeLastSessionId(null);
  chatScroll.innerHTML = "";
  previewRoot.innerHTML = "";
  previewRoot.appendChild(previewEmpty);
  // 新对话没有方案：清掉上一条对话的方案卡片（含历史卡与去重登记）。
  clearPlanCards();
  planState.designPhase = null;
  planState.reportPlan = null;
  planState.busy = false;
  planState.error = null;
  planState.planExpanded = false;
  planState.cardExpanded = true;
  planState.versionId = "";
  planState.summaryLine = "";
  renderPlanModule();
  setState("ANALYZING");
  updateMeta();
  renderSessionList();
}

// --------------------------------------------------------------------------
// JSON 粘贴入口（#66）
//
// 左侧对话区要给用户一个**明确**的入口：把记录数组贴进来，并附一句自然语言。
// 服务端已经在既有 POST /api/agent/chat 上认 JSON 段（routing_rules.detect_json_payload），
// 前端不新增端点、不新增协议。
//
// 客户端先自查一遍是为了**当场**给出「哪里不对、怎么改」；
// 服务端仍然是唯一的边界（契约表 §8 不变量 6）。
// --------------------------------------------------------------------------

/** 打开 / 收起 JSON 粘贴面板。 */
function toggleJsonPastePanel(open) {
  const panel = $("json-paste-panel");
  if (!panel) return;
  const show = open === undefined ? panel.classList.contains("hidden") : Boolean(open);
  panel.classList.toggle("hidden", !show);
  if (show) {
    const data = $("json-paste-data");
    if (data && typeof data.focus === "function") data.focus();
  }
}

/** 关掉 JSON 粘贴面板。 */
function closeJsonPastePanel() {
  toggleJsonPastePanel(false);
}

/** 在粘贴面板里摆一条**可修正**的提示。 */
function showJsonPasteHint(error) {
  const hint = $("json-paste-hint");
  if (!hint) return;
  const text = WorkbenchQuery.jsonErrorHint(error);
  hint.textContent = text;
  hint.classList.toggle("hidden", !text);
}

/** 清掉粘贴面板里的提示。 */
function clearJsonPasteHint() {
  const hint = $("json-paste-hint");
  if (!hint) return;
  hint.textContent = "";
  hint.classList.add("hidden");
}

/** 清掉粘贴框里的内容（服务端收下之后才清）。 */
function clearJsonPasteInputs() {
  const data = $("json-paste-data");
  const note = $("json-paste-note");
  if (data) data.value = "";
  if (note) note.value = "";
  clearJsonPasteHint();
}

/**
 * 把粘贴的 JSON 发出去。
 *
 * 先自查：本地不通过就**当场**给出「哪里不对、怎么改」，不发给服务端；
 * 本地通过才发（服务端还会再校验一遍）。
 */
function submitJsonPaste() {
  const dataEl = $("json-paste-data");
  const noteEl = $("json-paste-note");
  const raw = dataEl ? String(dataEl.value || "") : "";
  const note = noteEl ? String(noteEl.value || "") : "";
  const verdict = WorkbenchQuery.parseJsonPaste(raw);
  if (!verdict.ok) {
    showJsonPasteHint(verdict.error);
    return;
  }
  clearJsonPasteHint();
  const message = WorkbenchQuery.buildJsonPasteMessage(note, raw);
  if (!message.trim()) {
    showJsonPasteHint({ code: "JSON_INPUT_EMPTY", message: "请把记录数组贴进来再试。" });
    return;
  }
  sendMessage(message);
}

// -------- 事件绑定 --------
$("btn-json-paste")?.addEventListener("click", () => toggleJsonPastePanel());
$("btn-json-paste-cancel")?.addEventListener("click", () => {
  clearJsonPasteHint();
  closeJsonPastePanel();
});
$("btn-json-paste-send")?.addEventListener("click", () => submitJsonPaste());

// 「有最新方案」：用户在上方读历史时点它跳到底部（不强行拽）。
$("btn-latest-plan")?.addEventListener("click", () => {
  chatStickToBottom = true;
  chatScroll.scrollTop = chatScroll.scrollHeight;
  updateJumpButton();
  hideLatestPlanHint();
});

document.querySelector("[data-quick]").addEventListener("click", (event) => {
  sendMessage(event.currentTarget.dataset.quick);
});

$("btn-send").addEventListener("click", () => {
  // 待确认菜单存在时也允许发送：只读提问要走这条路（工单 02）。
  // 执行操作的约束由后端把关，这里不再拦自由输入。
  //
  // 例外：服务端说「正等下一步操作选择」时，发送按钮是锁住的，
  // 这里再挡一次 —— 免得 DOM 被绕过时把自由输入发出去（规格第 75 条）。
  //
  // 判据与输入框的 disabled **同一处**（inputControlState）：原来这里用的
  // isInputLocked 只看 lockScope，比真实的锁窄 —— 「有选项但 lockScope 是
  // menu_only」的菜单同样锁着输入，发送按钮却还能发。两处口径必须一致。
  if (activeRequest || advanceTimers.size) return;
  const controls = inputControlState(lockedChoice, isBusy(), hasPendingPlan());
  if (controls.inputDisabled) {
    toast(controls.hint || INPUT_LOCK_HINT);
    return;
  }
  const text = $("chat-input").value.trim();
  if (!text) return;
  $("chat-input").value = "";
  $("chat-input").style.height = "auto";
  sendMessage(text);
});

$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    // Enter 发送与输入框、发送按钮一起锁（规格第 75 条）。
    // 判据与输入框的 disabled **同一处**（inputControlState），
    // 别用只看 lockScope 的 isInputLocked —— 那比真实的锁窄。
    const controls = inputControlState(lockedChoice, isBusy(), hasPendingPlan());
    if (controls.inputDisabled) {
      toast(controls.hint || INPUT_LOCK_HINT);
      return;
    }
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
$("btn-save-draft").addEventListener("click", () => runDraftAction("SAVE"));
$("btn-manage-drafts").addEventListener("click", () => openDraftPicker(true));

/**
 * 主题联动：窗口重新拿到焦点时，向服务端要一次**权威主题**。
 *
 * 共享主题是数据库里的对象（契约 0002 §8）：别的对象改了主题，
 * 这边**已打开的视图**不能永远停在旧缓存上。主题由服务端解析后
 * 下发，前端不拼主题注册表、也不自己算版本。
 */
let lastResolvedThemeRevision = null;
async function refreshResolvedTheme() {
  if (!session.draftId) return;
  try {
    const state = await draftApi.detail(session.draftId);
    const theme = state?.resolvedTheme;
    if (!theme) return;
    const revision = theme.themeRevision ?? null;
    if (session.resolvedTheme && revision === lastResolvedThemeRevision) return;
    lastResolvedThemeRevision = revision;
    session.resolvedTheme = theme;
    renderReportPanel();
  } catch (err) {
    /* 取不到就保持原样：主题刷新失败不该打断正在做的事。 */
  }
}
window.addEventListener("focus", refreshResolvedTheme);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshResolvedTheme();
});

// --------------------------------------------------------------------------
// 工作台三视图模式：只看对话 / 双栏显示 / 只看草稿
//
// 这里只改**布局**，不动任何业务状态：会话、草稿、草稿 ID、版本、撤销/重做
// 记录、未发送的文字、方案卡的折叠状态全都原样保留；切换不发任何请求。
//
// 模式值只存在内存里（#workspace-panes 上的 data-pane-mode 属性），
// 刷新页面回到双栏，不写 localStorage。切换会话、收到新回复、草稿重渲染
// 都不会动这个属性，所以当前模式不会被无故重置。
//
// 隐藏面板一律 display:none（CSS 侧）：既不占空间，也不在键盘 Tab 顺序里。
// --------------------------------------------------------------------------

/**
 * 按 id 取元素。
 *
 * 页面结构变了（或者精简 harness 里根本没有外壳）就返回 null，
 * 绝不抛错 —— 这一段代码不能把工作台带崩。
 */
function paneElement(id) {
  try {
    if (typeof document === "undefined" || !document) return null;
    if (typeof document.getElementById !== "function") return null;
    return document.getElementById(id) || null;
  } catch (e) {
    return null;
  }
}

// 滚动位置记忆：元素 → { top, mode }。
// mode 记的是「这个位置是在哪个模式下取得的」——
// 恢复时只应用与**当前模式**一致的那一份：别的模式的位置在当前模式下没有意义，
// 而且单栏模式下被截断出来的 0 不能覆盖双栏时记住的位置。
//
// 为什么要记 mode：窄屏上下排时，真正承担滚动的是外层 #workspace-panes。
// 切到单栏后内容变短，这个容器滚不动，浏览器会把它的 scrollTop 截断成 0；
// 那个 0 不是用户的位置，不能拿它去覆盖双栏时记住的真实位置。
const paneScrollMemory = new Map();

/** 元素当前有没有渲染出来（display:none 的元素没有布局盒）。 */
function paneElementRendered(el) {
  if (!el) return false;
  try {
    if (typeof el.getClientRects === "function") return el.getClientRects().length > 0;
  } catch (e) { /* 取不到就按可见算 */ }
  return true;
}

/** 元素当前是不是一个真的能滚的容器（内容比可视区高）。 */
function paneElementScrollable(el) {
  if (!el) return false;
  try {
    return el.scrollHeight > el.clientHeight + 1;
  } catch (e) {
    return false;
  }
}

/**
 * 记下当前滚动容器的位置。
 *
 * 三种情况分开处理，不能一刀切：
 * - **隐藏着**（display:none）：scrollTop 已被浏览器清零，那个 0 是被隐藏强制的，
 *   不是用户的位置 → 既不记，也不删（记忆留着，等它显示时还原）。
 * - **渲染着且滚得动**：scrollTop 就是用户的位置（含合法的 0：用户主动滚回了顶部）
 *   → 记下来，覆盖本模式之前的值。
 * - **渲染着但滚不动**：本模式下没有有效位置（单栏时内容变短，外层的 0 是截断出来的）
 *   → 只清掉**本模式**那一份，其它模式记住的位置不能被这个 0 顶掉。
 */
function capturePaneScrollPositions() {
  // 已经被替换掉的旧节点（重渲染过）不再记着，别让 Map 一直涨。
  try {
    for (const el of Array.from(paneScrollMemory.keys())) {
      if (!el || el.isConnected === false) paneScrollMemory.delete(el);
    }
  } catch (e) { /* 忽略 */ }

  const mode = readPaneMode();
  const panes = paneElement("workspace-panes");
  const chat = paneElement("chat-scroll");
  const draft = paneElement("draft-pane");
  const targets = [];
  // 外层容器：窄屏上下排（< lg）时，整列滚动的是它，不是里面的面板。
  if (panes) targets.push(panes);
  if (chat) targets.push(chat);
  if (draft) {
    targets.push(draft);
    try {
      const nodes = typeof draft.querySelectorAll === "function" ? draft.querySelectorAll("*") : [];
      for (const node of Array.prototype.slice.call(nodes || [])) {
        if (!node || node === draft) continue;
        // 只记真正能滚的容器（长表格、分组区域那些）。
        if ((node.scrollTop || 0) > 0 || node.scrollHeight > node.clientHeight + 1) targets.push(node);
      }
    } catch (e) { /* 取不到就只记外层几个 */ }
  }
  for (const el of targets) {
    // 隐藏着：它的 0 是被隐藏强制的，不记也不删。
    if (!paneElementRendered(el)) continue;
    if (paneElementScrollable(el)) {
      // 现在滚得动：这个 scrollTop 就是用户的位置（合法的 0 也在内）。
      try { paneScrollMemory.set(el, { top: el.scrollTop || 0, mode }); } catch (e) { /* 忽略 */ }
      continue;
    }
    // 现在滚不动：本模式下的位置已失效（可能是被截断的 0），只清本模式那一份。
    try {
      const previous = paneScrollMemory.get(el);
      if (previous && previous.mode === mode) paneScrollMemory.delete(el);
    } catch (e) { /* 忽略 */ }
  }
}

/**
 * 按记忆恢复滚动位置。
 *
 * 只应用与当前模式一致的那一份：位置是「在某个模式下读到的」，
 * 换个模式就不该直接套用。仍然隐藏着的面板恢复不了，记忆留着下次显示时再恢复。
 */
function restorePaneScrollPositions() {
  const mode = readPaneMode();
  for (const [el, entry] of paneScrollMemory.entries()) {
    if (!el || el.isConnected === false) continue;
    if (!paneElementRendered(el)) continue;
    if (!entry || entry.mode !== mode) continue;
    const top = entry.top || 0;
    try {
      if ((el.scrollTop || 0) !== top) el.scrollTop = top;
    } catch (e) { /* 忽略 */ }
  }
}

/**
 * 布局变了以后，让**还活着**的图表重新量一次尺寸。
 *
 * 独立于展示模块（WorkbenchPresentation）：平铺 fallback 那条路也得能 resize，
 * 所以这里自己遍历 _chartInstances。幂等：只调 chart.resize()，
 * 不重建报表、不递归触发渲染。
 * 隐藏容器与零尺寸容器一律跳过 —— resize 到 0 尺寸会把图画坏。
 */
function resizeWorkbenchCharts() {
  const instances = (typeof _chartInstances !== "undefined" && Array.isArray(_chartInstances)) ? _chartInstances : [];
  for (const chart of instances) {
    try {
      if (!chart || typeof chart.resize !== "function") continue;
      const dom = typeof chart.getDom === "function" ? chart.getDom() : null;
      if (dom) {
        // 隐藏容器（单栏模式把对面那块藏起来时）：这会儿量不到尺寸，跳过。
        if (dom.offsetParent === null) continue;
        // 零尺寸容器：还没量出宽高，跳过。
        if (!dom.clientWidth || !dom.clientHeight) continue;
      }
      chart.resize();
    } catch (e) { /* 单个图失败不影响其它 */ }
  }
}

/** 当前模式：split | chat | draft。认不出来就当 split。 */
function readPaneMode() {
  const panes = paneElement("workspace-panes");
  const value = panes && typeof panes.getAttribute === "function"
    ? panes.getAttribute("data-pane-mode")
    : null;
  return ["split", "chat", "draft"].indexOf(value) >= 0 ? value : "split";
}

/** 菜单里标出当前模式（勾号由 CSS 按 aria-checked 显示）。 */
function syncPaneModeMenu(mode) {
  const current = mode || readPaneMode();
  const menu = paneElement("pane-mode-menu");
  if (!menu || typeof menu.querySelectorAll !== "function") return;
  let items = [];
  try { items = Array.prototype.slice.call(menu.querySelectorAll("[data-pane-mode-option]") || []); } catch (e) { items = []; }
  for (const item of items) {
    if (!item || typeof item.setAttribute !== "function") continue;
    item.setAttribute("aria-checked", item.getAttribute("data-pane-mode-option") === current ? "true" : "false");
  }
}

/** 等布局落定（两帧）之后恢复滚动位置并让图表重新量尺寸。 */
function schedulePaneLayoutRefresh() {
  const raf = typeof requestAnimationFrame === "function" ? requestAnimationFrame : null;
  if (!raf) {
    restorePaneScrollPositions();
    resizeWorkbenchCharts();
    return;
  }
  raf(() => {
    restorePaneScrollPositions();
    raf(() => {
      restorePaneScrollPositions();
      resizeWorkbenchCharts();
    });
  });
}

/**
 * 切到某个视图模式。
 *
 * 顺序很重要：先记滚动位置（隐藏之后浏览器会把 scrollTop 清零），
 * 再改属性让 CSS 生效，最后等布局落定后恢复位置并让图表重新量尺寸。
 * **不调用 renderReportPanel()** —— 报表不重建，只换布局。
 */
function applyPaneMode(mode) {
  const next = ["split", "chat", "draft"].indexOf(mode) >= 0 ? mode : "split";
  const panes = paneElement("workspace-panes");
  capturePaneScrollPositions();
  if (panes && typeof panes.setAttribute === "function") {
    panes.setAttribute("data-pane-mode", next);
  }
  syncPaneModeMenu(next);
  schedulePaneLayoutRefresh();
}

/** 开合小菜单。open 省略时是切换。 */
function setPaneMenuOpen(open) {
  const menu = paneElement("pane-mode-menu");
  const button = paneElement("btn-pane-mode");
  if (!menu || !menu.classList || !button) return;
  const shouldOpen = open === undefined ? !menu.classList.contains("is-open") : Boolean(open);
  menu.classList.toggle("is-open", shouldOpen);
  if (typeof button.setAttribute === "function") {
    button.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
  }
  if (shouldOpen) {
    let first = null;
    try { first = menu.querySelector("[data-pane-mode-option]"); } catch (e) { first = null; }
    if (first && typeof first.focus === "function") first.focus();
  }
}

/** 关闭小菜单；restoreFocus 不为 false 时把焦点还给按钮。 */
function closePaneMenu(restoreFocus) {
  const menu = paneElement("pane-mode-menu");
  if (!menu || !menu.classList) return;
  const wasOpen = menu.classList.contains("is-open");
  menu.classList.remove("is-open");
  const button = paneElement("btn-pane-mode");
  if (button && typeof button.setAttribute === "function") {
    button.setAttribute("aria-expanded", "false");
  }
  if (wasOpen && restoreFocus !== false && button && typeof button.focus === "function") {
    button.focus();
  }
}

// ---- 面板模式的按钮与小菜单 ----
// 外壳不在（精简 harness 只有局部骨架）时整段跳过，不初始化异常。
(function bindPaneMode() {
  const button = paneElement("btn-pane-mode");
  const menu = paneElement("pane-mode-menu");
  if (!button || !menu) return;

  button.addEventListener("click", (event) => {
    // 别让文档级监听器接着把刚打开的菜单又关掉。
    if (event && typeof event.stopPropagation === "function") event.stopPropagation();
    setPaneMenuOpen();
  });

  let items = [];
  try { items = Array.prototype.slice.call(menu.querySelectorAll("[data-pane-mode-option]") || []); } catch (e) { items = []; }
  for (const item of items) {
    if (!item || typeof item.addEventListener !== "function") continue;
    item.addEventListener("click", () => {
      applyPaneMode(item.getAttribute("data-pane-mode-option"));
      closePaneMenu(true);
    });
  }

  // 点外面：关掉菜单。焦点不抢 —— 用户刚点到的输入框还要能继续打字。
  document.addEventListener("click", (event) => {
    if (!menu.classList.contains("is-open")) return;
    const target = event ? event.target : null;
    if (target && typeof menu.contains === "function" && menu.contains(target)) return;
    if (target && typeof button.contains === "function" && button.contains(target)) return;
    closePaneMenu(false);
    const active = document.activeElement;
    if (!active || active === document.body) {
      if (typeof button.focus === "function") button.focus();
    }
  });

  // 键盘：Escape 关闭（焦点回按钮）；上下/Home/End 在菜单项之间移动。
  document.addEventListener("keydown", (event) => {
    if (!event || !menu.classList.contains("is-open")) return;
    if (event.key === "Escape") {
      closePaneMenu(true);
      return;
    }
    let list = [];
    try { list = Array.prototype.slice.call(menu.querySelectorAll("[data-pane-mode-option]") || []); } catch (e) { list = []; }
    if (!list.length) return;
    const index = list.indexOf(document.activeElement);
    if (index < 0) return;
    let next = -1;
    if (event.key === "ArrowDown") next = (index + 1) % list.length;
    else if (event.key === "ArrowUp") next = (index - 1 + list.length) % list.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = list.length - 1;
    if (next < 0) return;
    if (typeof event.preventDefault === "function") event.preventDefault();
    if (list[next] && typeof list[next].focus === "function") list[next].focus();
  });

  // 首次进入：按外壳上的 data-pane-mode 同步菜单勾选。
  syncPaneModeMenu(readPaneMode());
})();

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
  // 深链：``?session=<sessionId>`` 直接打开那条会话（预览 / 自检要用它
  // 定位到有评价目标的那一轮）。没带这个参数时，沿用「接回上次那条」。
  const requestedSession = new URLSearchParams(window.location.search).get("session");
  if (requestedSession) {
    const opened = await restoreSession(requestedSession, { silent: true });
    if (!opened.ok) {
      toast("没能打开链接里的会话，历史入口在左边。");
    }
  } else {
    // 刷新后自动接回上次那条会话；接不回来也会明确提示，历史入口始终保留。
    await autoRestoreSession();
  }
})();
