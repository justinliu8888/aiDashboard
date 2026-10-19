// 智能报表 FakeAgent 联调脚本
// 用最少代码跑通 FakeAgent 的 6 个样例

const API_BASE = window.location.origin || "http://127.0.0.1:8000";
document.getElementById("backend-url").textContent = API_BASE;

// 会话状态
const session = {
  conversationId: "conv_" + Date.now(),
  state: "ANALYZING",
  designSessionId: null,
  reportDefinitionId: null,
  baseDefinitionRevision: null,
  lastDefinition: null,
};

let activeRequest = null;

// 预设样例：直接发送即可触发对应分支
const SAMPLES = {
  1: {
    message: "生成 A 机房昨日能耗日报，包含总耗电、设备排名和趋势分析",
    state: "ANALYZING",
    context: { projectId: "project_001", subjectRef: "machine_room", subjectId: "room_a" },
  },
  2: {
    message: "帮我看异常设备",
    state: "ANALYZING",
    context: { projectId: "project_001", subjectRef: "machine_room", subjectId: "room_a" },
  },
  3: {
    message: "帮我看所有项目的能耗排名",
    state: "ANALYZING",
    context: { projectId: "project_001", subjectRef: "machine_room", subjectId: "room_a" },
  },
  4: {
    message: "帮我看 A 机房分钟级历史能耗",
    state: "ANALYZING",
    context: { projectId: "project_001", subjectRef: "machine_room", subjectId: "room_a" },
  },
  5: {
    message: "把趋势图改成最近 7 天",
    state: "WAITING_USER_CONFIRM",
    context: { projectId: "project_001", subjectRef: "machine_room", subjectId: "room_a" },
    extra: {
      designSessionId: "session_demo",
      reportDefinitionId: "def_demo",
      baseDefinitionRevision: 3,
      selectedRegionId: "region_hourly_trend",
    },
  },
  6: {
    message: "不要趋势图，改成只要总耗电和设备排名",
    state: "WAITING_USER_CONFIRM",
    context: { projectId: "project_001", subjectRef: "machine_room", subjectId: "room_a" },
    extra: {
      designSessionId: "session_demo",
      reportDefinitionId: "def_demo",
      baseDefinitionRevision: 5,
    },
  },
};

// ---------- DOM 工具 ----------
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---------- 中文词典（用户视角渲染） ----------
const ROLE_LABEL = {
  REPORT_TITLE: { text: "报告标题", color: "slate" },
  SUMMARY: { text: "关键指标", color: "indigo" },
  DETAIL: { text: "明细数据", color: "sky" },
  TREND: { text: "趋势分析", color: "emerald" },
  QUALITY: { text: "数据质量", color: "amber" },
  ANALYSIS: { text: "智能解读", color: "fuchsia" },
};

const COMPONENT_LABEL = {
  TEXT: "文本",
  METRIC_CARD: "指标卡片",
  TABLE: "表格",
  CHART: "图表",
  DATA_QUALITY: "数据质量面板",
  ANALYSIS: "AI 智能分析",
};

const CHART_KIND_LABEL = {
  LINE: "折线图",
  BAR: "柱状图",
  AREA: "面积图",
  PIE: "饼图",
};

const UNIT_LABEL = {
  DAY: "天",
  HOUR: "小时",
  MINUTE: "分钟",
  WEEK: "周",
  MONTH: "月",
};

const EXECUTION_MODE_LABEL = {
  MANUAL_ONLY: "仅手动触发",
  AUTO_SCHEDULE: "自动调度",
  MANUAL_AND_AUTO: "手动与自动均可",
};

const OVERRIDE_LABEL = {
  TIME_WINDOW: "时间范围",
  GRAIN: "时间粒度",
  SCOPE: "作用范围",
  FILTER: "筛选条件",
};

const STATUS_LABEL = {
  DRAFT: "草稿",
  REVIEW: "审核中",
  PUBLISHED: "已发布",
};

// 把 timeWindow 的 JSON 翻译成中文一句话
function formatTimeWindow(tw) {
  if (!tw) return "";
  const v = tw.value || tw;
  if (!v || v.type !== "CALENDAR") return JSON.stringify(v);
  const unitZh = UNIT_LABEL[v.unit] || v.unit;
  const offset = v.offset;
  const count = v.count || 1;
  if (offset === -1 && count === 1) return "昨日";
  if (offset === 0 && count === 1) return "今日";
  if (count === 1 && offset < 0) return `${-offset} ${unitZh}前的那一${unitZh}`;
  if (count > 1 && offset < 0 && -offset === count) return `最近 ${count} ${unitZh}`;
  return `从 ${offset} ${unitZh}开始，共 ${count} ${unitZh}`;
}

function formatGrain(g) {
  if (!g) return "";
  const v = g.value || g;
  if (!v || !v.unit) return "";
  return `按${UNIT_LABEL[v.unit] || v.unit}聚合`;
}

// 徽章颜色映射到 tailwind
function roleBadge(role) {
  const info = ROLE_LABEL[role] || { text: role, color: "slate" };
  const colorMap = {
    slate: "bg-slate-100 text-slate-700 border-slate-200",
    indigo: "bg-indigo-50 text-indigo-700 border-indigo-200",
    sky: "bg-sky-50 text-sky-700 border-sky-200",
    emerald: "bg-emerald-50 text-emerald-700 border-emerald-200",
    amber: "bg-amber-50 text-amber-700 border-amber-200",
    fuchsia: "bg-fuchsia-50 text-fuchsia-700 border-fuchsia-200",
  };
  return `<span class="text-[11px] px-1.5 py-0.5 rounded border ${colorMap[info.color]}">${esc(info.text)}</span>`;
}

function appendMessage(role, text, extra = "") {
  const bubble = document.createElement("div");
  if (role === "user") {
    bubble.className = "flex justify-end";
    bubble.innerHTML = `<div class="max-w-[85%] bg-blue-600 text-white text-sm rounded-lg px-3 py-2">${esc(text)}</div>`;
  } else if (role === "ai") {
    bubble.className = "flex justify-start";
    bubble.innerHTML = `
      <div class="max-w-[85%] bg-slate-100 text-slate-800 text-sm rounded-lg px-3 py-2">
        ${esc(text)}
        ${extra ? `<div class="text-[11px] text-slate-500 mt-1">${extra}</div>` : ""}
      </div>`;
  } else {
    bubble.className = "text-center text-[11px] text-slate-400";
    bubble.textContent = text;
  }
  const list = $("messages");
  list.appendChild(bubble);
  list.scrollTop = list.scrollHeight;
  return bubble;
}

function updateMessage(bubble, text, extra = "") {
  if (!bubble) return;
  bubble.innerHTML = `
    <div class="max-w-[85%] bg-slate-100 text-slate-800 text-sm rounded-lg px-3 py-2">
      ${esc(text)}
      ${extra ? `<div class="text-[11px] text-slate-500 mt-1 whitespace-pre-line">${esc(extra)}</div>` : ""}
    </div>`;
  const list = $("messages");
  list.scrollTop = list.scrollHeight;
}

function setSending(isSending) {
  const btn = $("send-btn");
  if (isSending) {
    btn.textContent = "中止";
    btn.className = "shrink-0 bg-rose-600 hover:bg-rose-700 text-white text-sm font-medium px-4 py-2 rounded-lg";
  } else {
    btn.textContent = "发送";
    btn.className = "shrink-0 bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium px-4 py-2 rounded-lg";
  }
}

function setState(state) {
  session.state = state;
  const badge = $("state-badge");
  badge.textContent = state;
  const palette = {
    ANALYZING: "bg-slate-50 text-slate-600 border-slate-200",
    NEEDS_CLARIFICATION: "bg-amber-50 text-amber-700 border-amber-200",
    BLOCKED_OR_UNSUPPORTED: "bg-rose-50 text-rose-700 border-rose-200",
    INTERRUPT_CONFIRMING: "bg-orange-50 text-orange-700 border-orange-200",
    WAITING_USER_CONFIRM: "bg-blue-50 text-blue-700 border-blue-200",
    DEFINITION_DRAFT_READY: "bg-fuchsia-50 text-fuchsia-700 border-fuchsia-200",
  };
  badge.className = "ml-auto text-[11px] px-2 py-0.5 rounded border mono " + (palette[state] || "bg-slate-50 text-slate-600 border-slate-200");
}

function stageLabel(stage) {
  const labels = {
    router_received: "后端已接收请求",
    service_start: "服务层开始处理",
    context_route_start: "ContextEngine 开始判断状态机走向",
    concierge_start: "Concierge 开始轻量判断",
    concierge_chunk: "Concierge 正在流式返回",
    concierge_done: "Concierge 判断完成",
    concierge_failed: "Concierge 超时或失败",
    concierge_direct_reply: "Concierge 判定可直接回复",
    deep_route_start: "Deep Router 开始深度判断",
    deep_route_chunk: "Deep Router 正在流式返回",
    deep_route_done: "Deep Router 判断完成",
    deep_route_failed: "Deep Router 超时或失败",
    context_route_done: "ContextEngine 判断完成",
    context_route_intercepted: "ContextEngine 已拦截并直接返回",
    business_agent_start: "业务 Agent 开始处理",
    business_agent_done: "业务 Agent 处理完成",
    reply_stream_start: "开始生成最终回复",
    reply_stream_done: "最终回复生成完成",
    service_return: "服务层准备返回",
  };
  return labels[stage] || stage || "处理中";
}

async function readSseResponse(res, onEvent) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const part of parts) {
      let event = "message";
      const dataLines = [];
      for (const line of part.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (!dataLines.length) continue;
      onEvent(event, JSON.parse(dataLines.join("\n")));
    }
  }
}

// ---------- 请求 ----------
async function sendMessage(payload) {
  if (activeRequest) {
    appendMessage("system", "已有请求在处理中，请先中止或等待返回");
    return;
  }

  const body = {
    agentProtocolVersion: "1.0",
    conversationId: session.conversationId,
    state: payload.state || session.state,
    message: payload.message,
    context: payload.context,
    ...(payload.extra || {}),
  };

  appendMessage("user", payload.message);
  appendMessage("system", `发送 state=${body.state}`);

  const controller = new AbortController();
  const startedAt = performance.now();
  const progress = appendMessage("ai", "AI 思考中...", "0s · 已发送请求，等待后端接收");
  let currentStage = "已发送请求，等待后端接收";
  let chunkPreview = "";
  const timer = setInterval(() => {
    const elapsed = Math.floor((performance.now() - startedAt) / 1000);
    const preview = chunkPreview ? `\n模型片段：${chunkPreview.slice(-80)}` : "";
    updateMessage(progress, "AI 思考中...", `${elapsed}s · ${currentStage}${preview}`);
  }, 1000);

  activeRequest = { controller, timer, progress, startedAt };
  setSending(true);

  let data;
  let replyBubble = null;
  let streamedReply = "";
  try {
    const res = await fetch(API_BASE + "/api/agent/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
    await readSseResponse(res, (event, payload) => {
      if (event === "stage") {
        currentStage = stageLabel(payload.stage);
      } else if (event === "model_chunk") {
        currentStage = stageLabel(payload.stage);
        chunkPreview += payload.delta || "";
      } else if (event === "reply_delta") {
        streamedReply += payload.delta || "";
        if (!replyBubble) {
          replyBubble = appendMessage("ai", "");
        }
        updateMessage(replyBubble, streamedReply);
      } else if (event === "final") {
        data = payload;
      } else if (event === "error") {
        throw new Error(payload.message || "SSE 请求失败");
      }
      const elapsed = ((performance.now() - startedAt) / 1000).toFixed(1);
      const preview = chunkPreview ? `\n模型片段：${chunkPreview.slice(-80)}` : "";
      updateMessage(progress, "AI 思考中...", `${elapsed}s · ${currentStage}${preview}`);
    });
    if (!data) throw new Error("SSE 未返回 final 事件");
  } catch (err) {
    const elapsed = ((performance.now() - startedAt) / 1000).toFixed(1);
    if (err.name === "AbortError") {
      updateMessage(progress, "已中止本次请求", `${elapsed}s · 浏览器已停止等待后端响应`);
    } else {
      updateMessage(progress, "请求失败：" + err.message, `${elapsed}s · 请求未完成`);
    }
    clearInterval(timer);
    activeRequest = null;
    setSending(false);
    return;
  }

  const elapsed = ((performance.now() - startedAt) / 1000).toFixed(1);
  clearInterval(timer);
  updateMessage(
    progress,
    "服务器已返回，开始渲染结果",
    [
      `elapsed=${elapsed}s`,
      `state=${data.state || "—"}`,
      `contextEngineStage=${data.debug?.contextEngineStage || "—"}`,
      `contextEngineIntent=${data.debug?.contextEngineIntent || "—"}`,
      `contextEngineAction=${data.debug?.contextEngineAction || "—"}`,
    ].join("\n")
  );
  activeRequest = null;
  setSending(false);

  renderResponse(data, { skipSummary: Boolean(replyBubble) });
}

// ---------- 渲染响应 ----------
function renderResponse(data, options = {}) {
  $("raw-payload").textContent = JSON.stringify(data, null, 2);

  if (data.error) {
    appendMessage("ai", `[${data.error.code}] ${data.error.message}`);
    return;
  }

  setState(data.state);
  session.designSessionId = data.designSessionId || session.designSessionId;

  // 概览
  $("ov-state").textContent = data.state;
  $("ov-conv").textContent = data.conversationId || "—";
  $("ov-session").textContent = data.designSessionId || "—";
  $("ov-rev").textContent =
    data.domainObjects?.changeSet?.baseDefinitionRevision ??
    data.domainObjects?.reportDefinitionDraft?.metadata?.revision ??
    "—";

  renderQuestions(data.questions || []);
  renderBlocks(data.blocks || []);
  renderPolicy(data.domainObjects?.reportPolicyDraft);
  renderDefinition(data.domainObjects?.reportDefinitionDraft);
  renderChangeSet(data.domainObjects?.changeSet);
  renderUiActions(data.uiActions || []);
  renderToolRequests(data.toolRequests || []);

  // AI 气泡摘要
  if (!options.skipSummary) {
    const summary = summarize(data);
    appendMessage("ai", summary);
  }
}

function summarize(data) {
  // 优先用后端自己的自然语言回复
  if (data.reply) return data.reply;
  switch (data.state) {
    case "NEEDS_CLARIFICATION":
      return `我需要你补充 ${data.questions?.length || 0} 个信息再继续。`;
    case "BLOCKED_OR_UNSUPPORTED":
      return `这个请求被拦截了：${data.blocks?.map((b) => b.code).join(", ")}。`;
    case "WAITING_USER_CONFIRM":
      return "已生成报表框架，请你确认是否符合预期。";
    case "DEFINITION_DRAFT_READY":
      return "已生成修改方案，请查看变更详情。";
    default:
      return `当前状态：${data.state}`;
  }
}

// ---------- 分区渲染 ----------
function renderQuestions(list) {
  const card = $("questions-card");
  if (!list.length) return card.classList.add("hidden");
  card.classList.remove("hidden");
  $("questions-list").innerHTML = list
    .map((q) => {
      const promptText = q.prompt || q.text || "";
      const options = q.options || [];
      const optionsHtml = options.length
        ? `<div class="flex flex-wrap gap-1.5 mt-2">${options
            .map(
              (o) =>
                `<button class="suggestion-btn text-[12px] bg-white hover:bg-amber-100 text-amber-800 border border-amber-200 rounded px-2 py-1 transition" data-value="${esc(
                  o.value
                )}">${esc(o.label)}</button>`
            )
            .join("")}</div>`
        : "";
      return `
    <div class="bg-amber-50 border border-amber-200 rounded p-2.5">
      <div class="text-amber-900">${esc(promptText)}</div>
      ${optionsHtml}
    </div>`;
    })
    .join("");

  // 候选项点一下 → 自动填入输入框并发送
  card.querySelectorAll(".suggestion-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const text = btn.dataset.value;
      $("input").value = text;
      $("send-btn").click();
    });
  });
}

function renderBlocks(list) {
  const card = $("blocks-card");
  if (!list.length) return card.classList.add("hidden");
  card.classList.remove("hidden");
  $("blocks-list").innerHTML = list
    .map(
      (b) => `
    <div class="bg-white border border-rose-200 rounded p-2.5">
      <div class="font-medium text-rose-800 mono">${esc(b.code)}</div>
      <div class="text-slate-700 mt-1">${esc(b.message)}</div>
      ${
        b.alternatives?.length
          ? `<div class="mt-2 text-[11px] text-slate-500">
              替代方案：
              <ul class="list-disc ml-4 mt-1">${b.alternatives.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>
            </div>`
          : ""
      }
    </div>`
    )
    .join("");
}

function renderPolicy(policy) {
  const card = $("policy-card");
  if (!policy) return card.classList.add("hidden");
  card.classList.remove("hidden");

  const overrides = (policy.allowedOverrides || [])
    .map((o) => `<span class="text-[11px] bg-indigo-50 text-indigo-700 border border-indigo-200 rounded px-1.5 py-0.5">${esc(OVERRIDE_LABEL[o] || o)}</span>`)
    .join(" ");

  const execMode = EXECUTION_MODE_LABEL[policy.executionMode] || policy.executionMode;

  const friendly = `
    <div class="space-y-2 text-sm">
      <div><span class="text-slate-500">报表名称：</span><span class="font-medium text-slate-900">${esc(policy.name)}</span></div>
      ${policy.description ? `<div><span class="text-slate-500">说明：</span>${esc(policy.description)}</div>` : ""}
      <div><span class="text-slate-500">触发方式：</span>${esc(execMode)}</div>
      ${overrides ? `<div><span class="text-slate-500">可调整项：</span>${overrides}</div>` : ""}
    </div>
    <details class="mt-3">
      <summary class="cursor-pointer text-[11px] text-slate-400">查看技术字段</summary>
      <pre class="payload mt-2">${esc(JSON.stringify(policy, null, 2))}</pre>
    </details>
  `;
  $("policy-body").innerHTML = friendly;
}

// 把一个 region 翻译成"用户看得懂"的卡片
function regionToFriendlyCard(r) {
  const roleInfo = ROLE_LABEL[r.role] || { text: r.role };
  const compType = COMPONENT_LABEL[r.component?.type] || r.component?.type || "";
  const timeWindow = formatTimeWindow(r.context?.timeWindow);
  const grain = formatGrain(r.context?.grain);

  // 标题
  let title = r.name || roleInfo.text;
  if (r.component?.type === "TEXT" && r.component?.content?.template) {
    title = r.component.content.template.replace(/\{\{[^}]+\}\}/g, "…");
  } else if (r.component?.metric?.label) {
    title = r.component.metric.label;
  }

  // 这个区域在做什么的一句话总结
  let summary = "";
  if (r.component?.type === "METRIC_CARD") {
    const m = r.component.metric;
    summary = `展示 <span class="text-slate-900 font-medium">${esc(m?.label || "")}</span>${
      m?.format?.unitLabel ? `（单位 ${esc(m.format.unitLabel)}）` : ""
    }`;
  } else if (r.component?.type === "TABLE") {
    const cols = (r.component.columns || []).map((c) => c.label).join("、");
    summary = `明细表格：${esc(cols)}${r.binding?.topN ? `，取 Top ${r.binding.topN}` : ""}`;
  } else if (r.component?.type === "CHART") {
    const kind = CHART_KIND_LABEL[r.component.chartKind] || r.component.chartKind || "图表";
    summary = `${esc(kind)}`;
  } else if (r.component?.type === "DATA_QUALITY") {
    const fields = r.component.fields || [];
    const map = { COVERAGE: "覆盖度", MISSING: "缺失率", FRESHNESS: "新鲜度" };
    summary = `展示数据质量：${fields.map((f) => map[f] || f).join("、")}`;
  } else if (r.component?.type === "ANALYSIS") {
    summary = esc(r.component.goal || "AI 根据上面数据生成分析说明");
  }

  const metaParts = [];
  if (timeWindow) metaParts.push(`<span class="text-slate-500">时间范围：</span>${esc(timeWindow)}`);
  if (grain) metaParts.push(`<span class="text-slate-500">粒度：</span>${esc(grain)}`);
  const meta = metaParts.join("　·　");

  return `
    <div class="border border-slate-200 rounded-lg p-3 bg-white">
      <div class="flex items-center gap-2 mb-1">
        ${roleBadge(r.role)}
        <span class="font-medium text-slate-900">${esc(title)}</span>
        <span class="ml-auto text-[11px] text-slate-400">${esc(compType)}</span>
      </div>
      ${summary ? `<div class="text-xs text-slate-600 mt-1">${summary}</div>` : ""}
      ${meta ? `<div class="text-[11px] text-slate-500 mt-1">${meta}</div>` : ""}
      <details class="mt-2">
        <summary class="cursor-pointer text-[11px] text-slate-400">查看技术字段</summary>
        <div class="text-[11px] text-slate-500 mt-1 space-y-0.5 mono">
          <div>regionId: <span class="text-slate-700">${esc(r.id)}</span></div>
          <div>role: <span class="text-slate-700">${esc(r.role)}</span></div>
          <div>component.type: <span class="text-slate-700">${esc(r.component?.type || "")}</span></div>
          ${r.component?.metric?.metricRef ? `<div>metricRef: <span class="text-slate-700">${esc(r.component.metric.metricRef)}</span></div>` : ""}
        </div>
      </details>
    </div>`;
}

function renderDefinition(def) {
  const card = $("definition-card");
  if (!def) return card.classList.add("hidden");
  card.classList.remove("hidden");
  session.lastDefinition = def;
  session.reportDefinitionId = def?.metadata?.id || null;
  session.baseDefinitionRevision = def?.metadata?.revision ?? null;

  const statusZh = STATUS_LABEL[def.metadata?.status] || def.metadata?.status || "";
  $("definition-meta").textContent = `${statusZh}　·　版本 ${def.metadata?.revision ?? 0}　·　共 ${(def.regions || []).length} 个区域`;

  const summaryLine = `
    <div class="text-sm mb-3 text-slate-700">
      <span class="text-slate-500">报表名称：</span><span class="font-medium text-slate-900">${esc(def.metadata?.name || "")}</span>
      ${def.defaults?.timeWindow ? `　·　<span class="text-slate-500">默认时间范围：</span>${esc(formatTimeWindow(def.defaults.timeWindow))}` : ""}
      ${def.defaults?.grain ? `　·　<span class="text-slate-500">默认粒度：</span>${esc(formatGrain(def.defaults.grain))}` : ""}
    </div>
  `;

  const regionCards = (def.regions || []).map(regionToFriendlyCard).join("");

  const techDetail = `
    <details class="mt-3">
      <summary class="cursor-pointer text-[11px] text-slate-400">查看完整技术字段（JSON）</summary>
      <pre class="payload mt-2">${esc(JSON.stringify(def, null, 2))}</pre>
    </details>
  `;

  $("definition-body").innerHTML = summaryLine + `<div class="space-y-2">${regionCards}</div>` + techDetail;
}

// 根据原 definition 找到某个 region 的用户友好名称
function regionDisplayName(regionId) {
  const def = session.lastDefinition;
  if (!def) return regionId;
  const r = (def.regions || []).find((x) => x.id === regionId);
  if (!r) return regionId;
  const roleText = (ROLE_LABEL[r.role] || {}).text || r.role;
  if (r.name) return `${r.name}（${roleText}）`;
  if (r.component?.metric?.label) return `${r.component.metric.label}（${roleText}）`;
  return roleText;
}

// 把一条 changed 翻译成"XX 做了什么"
function changeToSentence(e) {
  const name = regionDisplayName(e.regionId);
  // 整个 region 被删
  if (e.after === null || (e.path && e.path.endsWith("]") && !e.path.includes("."))) {
    return `删除「${name}」`;
  }
  // 新增 region（before 为 null）
  if (e.before === null && e.after) {
    return `新增「${name}」`;
  }
  // 时间窗口变更
  if (e.path && e.path.includes("timeWindow")) {
    const newTw = e.after?.value;
    const oldTw = e.before?.value || e.before;
    const newZh = newTw ? formatTimeWindow(newTw) : "";
    const oldZh = oldTw ? formatTimeWindow(oldTw) : "";
    if (newZh && oldZh) return `将「${name}」的时间范围从 ${oldZh} 改为 ${newZh}`;
    if (newZh) return `将「${name}」的时间范围调整为 ${newZh}`;
    return `调整「${name}」的时间范围`;
  }
  // 粒度变更
  if (e.path && e.path.includes("grain")) {
    const newG = e.after?.value;
    const oldG = e.before?.value || e.before;
    const newZh = newG ? formatGrain(newG) : "";
    const oldZh = oldG ? formatGrain(oldG) : "";
    if (newZh && oldZh) return `将「${name}」的粒度从 ${oldZh} 改为 ${newZh}`;
    if (newZh) return `将「${name}」的粒度调整为 ${newZh}`;
    return `调整「${name}」的粒度`;
  }
  // 其它：兜底
  return `修改「${name}」`;
}

function renderChangeSet(cs) {
  const card = $("changeset-card");
  if (!cs) return card.classList.add("hidden");
  card.classList.remove("hidden");
  $("cs-meta").textContent = `基于版本 ${cs.baseDefinitionRevision}`;

  const changedList = cs.changed || [];
  const changed = changedList
    .map((e) => {
      const sentence = changeToSentence(e);
      const isDelete = e.after === null;
      const colorCls = isDelete
        ? "border-rose-200 bg-rose-50"
        : e.before === null
        ? "border-emerald-200 bg-emerald-50"
        : "border-fuchsia-200 bg-fuchsia-50";
      const iconCls = isDelete ? "text-rose-600" : e.before === null ? "text-emerald-600" : "text-fuchsia-600";
      const icon = isDelete ? "✕" : e.before === null ? "＋" : "↻";
      return `
    <div class="border ${colorCls} rounded p-2.5">
      <div class="flex items-start gap-2">
        <span class="text-sm ${iconCls} mt-0.5">${icon}</span>
        <div class="flex-1 text-sm text-slate-800">${esc(sentence)}</div>
      </div>
      <details class="mt-2">
        <summary class="cursor-pointer text-[11px] text-slate-400">查看技术字段</summary>
        <div class="text-[11px] text-slate-500 mt-1 space-y-0.5 mono">
          <div>path: <span class="text-slate-700">${esc(e.path || "")}</span></div>
          <div>regionId: <span class="text-slate-700">${esc(e.regionId || "")}</span></div>
          ${e.after && e.after.mode ? `<div>mode: <span class="text-slate-700">${esc(e.after.mode)}</span></div>` : ""}
        </div>
      </details>
    </div>`;
    })
    .join("");

  const unchangedCount = (cs.unchanged || []).length;
  const unchangedNames = (cs.unchanged || [])
    .map((u) => regionDisplayName(u.regionId))
    .map((n) => `<span class="text-[11px] bg-slate-100 text-slate-600 rounded px-1.5 py-0.5">${esc(n)}</span>`)
    .join(" ");

  const impact = (cs.impact || [])
    .map((i) => `<div class="text-xs text-slate-600">「${esc(regionDisplayName(i.regionId))}」：${esc(i.note)}</div>`)
    .join("");

  $("changeset-body").innerHTML = `
    <div>
      <div class="text-[11px] text-slate-500 mb-1">本次改动（${changedList.length} 项）</div>
      <div class="space-y-2">${changed || '<div class="text-[11px] text-slate-400">无</div>'}</div>
    </div>
    ${
      unchangedCount
        ? `<div class="mt-3">
            <div class="text-[11px] text-slate-500 mb-1">保持不变（${unchangedCount} 项）</div>
            <div class="flex flex-wrap gap-1">${unchangedNames}</div>
          </div>`
        : ""
    }
    ${
      impact
        ? `<div class="mt-3">
            <div class="text-[11px] text-slate-500 mb-1">影响说明</div>
            ${impact}
          </div>`
        : ""
    }
    <details class="mt-3">
      <summary class="cursor-pointer text-[11px] text-slate-400">查看完整 ChangeSet（JSON）</summary>
      <pre class="payload mt-2">${esc(JSON.stringify(cs, null, 2))}</pre>
    </details>
  `;
}

function renderUiActions(actions) {
  $("ui-list").innerHTML = actions.length
    ? actions.map((a) => `<span class="text-[11px] bg-emerald-50 text-emerald-700 border border-emerald-200 rounded px-2 py-0.5 mono">${esc(a)}</span>`).join("")
    : '<span class="text-[11px] text-slate-400">无</span>';
}

function renderToolRequests(tools) {
  $("tool-list").innerHTML = tools.length
    ? tools.map((t) => `<div><span class="mono bg-slate-100 rounded px-1">${esc(t.type)}</span> ${t.description ? esc(t.description) : ""}</div>`).join("")
    : '<span class="text-[11px] text-slate-400">无</span>';
}

// ---------- 事件 ----------
document.querySelectorAll(".sample-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (activeRequest) {
      appendMessage("system", "已有请求在处理中，请先中止或等待返回");
      return;
    }
    const sample = SAMPLES[btn.dataset.sample];
    if (sample) sendMessage(sample);
  });
});

$("send-btn").addEventListener("click", () => {
  if (activeRequest) {
    activeRequest.controller.abort();
    return;
  }

  const text = $("input").value.trim();
  if (!text) return;
  $("input").value = "";
  sendMessage({
    message: text,
    state: session.state,
    context: { projectId: "project_001", subjectRef: "machine_room", subjectId: "room_a" },
    extra: session.designSessionId
      ? {
          designSessionId: session.designSessionId,
          reportDefinitionId: session.reportDefinitionId,
          baseDefinitionRevision: session.baseDefinitionRevision,
        }
      : undefined,
  });
});

$("input").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") $("send-btn").click();
});

$("reset-btn").addEventListener("click", () => {
  session.conversationId = "conv_" + Date.now();
  session.state = "ANALYZING";
  session.designSessionId = null;
  session.reportDefinitionId = null;
  session.baseDefinitionRevision = null;
  $("messages").innerHTML = "";
  ["questions-card", "blocks-card", "policy-card", "definition-card", "changeset-card"].forEach((id) => $(id).classList.add("hidden"));
  $("ui-list").innerHTML = '<span class="text-[11px] text-slate-400">无</span>';
  $("tool-list").innerHTML = '<span class="text-[11px] text-slate-400">无</span>';
  $("raw-payload").textContent = "—";
  setState("ANALYZING");
  appendMessage("system", "会话已重置");
});

// 启动检查
(async () => {
  try {
    const res = await fetch(API_BASE + "/health");
    if (res.ok) {
      $("health-badge").classList.remove("hidden");
      appendMessage("system", "后端连接正常，点击上方样例按钮开始体验");
    }
  } catch (e) {
    appendMessage("system", "后端未连接：请先启动 uvicorn app.main:app --reload");
  }
  setState("ANALYZING");
})();
