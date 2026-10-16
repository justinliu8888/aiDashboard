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
}

function setState(state) {
  session.state = state;
  const badge = $("state-badge");
  badge.textContent = state;
  const palette = {
    ANALYZING: "bg-slate-50 text-slate-600 border-slate-200",
    NEEDS_CLARIFICATION: "bg-amber-50 text-amber-700 border-amber-200",
    BLOCKED_OR_UNSUPPORTED: "bg-rose-50 text-rose-700 border-rose-200",
    WAITING_USER_CONFIRM: "bg-blue-50 text-blue-700 border-blue-200",
    DEFINITION_DRAFT_READY: "bg-fuchsia-50 text-fuchsia-700 border-fuchsia-200",
  };
  badge.className = "ml-auto text-[11px] px-2 py-0.5 rounded border mono " + (palette[state] || "bg-slate-50 text-slate-600 border-slate-200");
}

// ---------- 请求 ----------
async function sendMessage(payload) {
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

  let data;
  try {
    const res = await fetch(API_BASE + "/api/agent/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    data = await res.json();
  } catch (err) {
    appendMessage("ai", "请求失败：" + err.message);
    return;
  }

  renderResponse(data);
}

// ---------- 渲染响应 ----------
function renderResponse(data) {
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
  const summary = summarize(data);
  appendMessage("ai", summary);
}

function summarize(data) {
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
    .map(
      (q) => `
    <div class="bg-white border border-amber-200 rounded p-2.5">
      <div class="font-medium text-amber-800">${esc(q.text)}</div>
      <div class="text-[11px] text-slate-500 mt-1">id=${esc(q.id)} · ${esc(q.expects)}${q.required ? " · 必填" : ""}</div>
      ${
        q.options?.length
          ? `<div class="flex flex-wrap gap-1 mt-2">${q.options
              .map((o) => `<span class="text-[11px] bg-amber-100 text-amber-800 rounded px-1.5 py-0.5">${esc(o.label)}</span>`)
              .join("")}</div>`
          : ""
      }
    </div>`
    )
    .join("");
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
  $("policy-body").innerHTML = `
    <div><span class="text-slate-400">name:</span> ${esc(policy.name)}</div>
    <div><span class="text-slate-400">subjectRef:</span> <span class="mono">${esc(policy.subjectRef)}</span></div>
    <div><span class="text-slate-400">executionMode:</span> <span class="mono">${esc(policy.executionMode)}</span></div>
    <div><span class="text-slate-400">allowedOverrides:</span> ${(policy.allowedOverrides || []).map((o) => `<span class="mono bg-slate-100 rounded px-1 mr-1">${esc(o)}</span>`).join("")}</div>
  `;
}

function renderDefinition(def) {
  const card = $("definition-card");
  if (!def) return card.classList.add("hidden");
  card.classList.remove("hidden");
  session.lastDefinition = def;
  session.reportDefinitionId = def?.metadata?.id || null;
  session.baseDefinitionRevision = def?.metadata?.revision ?? null;

  $("definition-meta").textContent = `id=${def.metadata?.id || "-"} · revision=${def.metadata?.revision ?? "-"} · ${def.metadata?.status || "-"}`;

  $("definition-body").innerHTML = (def.regions || [])
    .map(
      (r) => `
    <div class="border border-slate-200 rounded p-2.5 text-sm">
      <div class="flex items-center gap-2">
        <span class="mono text-[11px] bg-slate-100 text-slate-600 rounded px-1.5 py-0.5">${esc(r.id)}</span>
        <span class="font-medium">${esc(r.component?.title || r.role)}</span>
        <span class="ml-auto text-[11px] text-slate-500">${esc(r.component?.type)}</span>
      </div>
      ${
        r.component?.metric
          ? `<div class="text-[11px] text-slate-500 mt-1">指标 <span class="mono">${esc(r.component.metric.metricRef)}</span> · 单位 ${esc(r.component.metric.format?.unitLabel || "")}</div>`
          : ""
      }
    </div>`
    )
    .join("");
}

function renderChangeSet(cs) {
  const card = $("changeset-card");
  if (!cs) return card.classList.add("hidden");
  card.classList.remove("hidden");
  $("cs-meta").textContent = `baseRevision=${cs.baseDefinitionRevision}`;

  const changed = (cs.changed || [])
    .map(
      (e) => `
    <div class="border border-fuchsia-200 bg-fuchsia-50 rounded p-2.5">
      <div class="mono text-[11px] text-fuchsia-700">${esc(e.path)}</div>
      <div class="text-xs text-slate-700 mt-1">regionId: <span class="mono">${esc(e.regionId)}</span></div>
      ${e.after === null ? '<div class="text-xs text-rose-600 mt-1">→ 删除</div>' : ""}
      ${
        e.after && e.after.mode === "REPLACE" && e.after.value
          ? `<div class="text-xs text-slate-700 mt-1">新窗口: <span class="mono">${esc(e.after.value.unit)} offset=${e.after.value.offset} count=${e.after.value.count}</span></div>`
          : ""
      }
    </div>`
    )
    .join("");

  const unchanged = (cs.unchanged || [])
    .map((u) => `<span class="mono text-[11px] bg-slate-100 text-slate-600 rounded px-1.5 py-0.5">${esc(u.regionId)}</span>`)
    .join("");

  const impact = (cs.impact || [])
    .map((i) => `<div class="text-xs text-slate-600"><span class="mono">${esc(i.regionId)}</span>: ${esc(i.note)}</div>`)
    .join("");

  $("changeset-body").innerHTML = `
    <div>
      <div class="text-[11px] text-slate-500 mb-1">changed</div>
      <div class="space-y-2">${changed || '<div class="text-[11px] text-slate-400">无</div>'}</div>
    </div>
    <div>
      <div class="text-[11px] text-slate-500 mb-1 mt-2">unchanged</div>
      <div class="flex flex-wrap gap-1">${unchanged || '<span class="text-[11px] text-slate-400">无</span>'}</div>
    </div>
    ${impact ? `<div><div class="text-[11px] text-slate-500 mb-1 mt-2">impact</div>${impact}</div>` : ""}
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
    const sample = SAMPLES[btn.dataset.sample];
    if (sample) sendMessage(sample);
  });
});

$("send-btn").addEventListener("click", () => {
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
