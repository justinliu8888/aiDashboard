// 智能报表工作台 - FakeAgent 对接实现
// 核心能力：
// 1. 发请求 → 根据 state 分发到不同的 UI 渲染
// 2. 根据 reportDefinitionDraft.regions[] 动态渲染预览
// 3. 根据 changeSet 应用变更并高亮

const API_BASE = window.location.origin;

// -------- 会话状态 --------
const session = {
  conversationId: "conv_" + Date.now(),
  state: "ANALYZING",
  designSessionId: null,
  reportDefinitionId: null,
  baseDefinitionRevision: null,
  definition: null, // 当前草稿（用于应用 changeSet）
};

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

function scrollChatBottom() {
  chatScroll.scrollTop = chatScroll.scrollHeight;
}

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

  const palette = {
    ANALYZING: "bg-slate-100 text-slate-600 border-slate-200",
    NEEDS_CLARIFICATION: "bg-amber-50 text-amber-700 border-amber-200",
    BLOCKED_OR_UNSUPPORTED: "bg-rose-50 text-rose-700 border-rose-200",
    WAITING_USER_CONFIRM: "bg-blue-50 text-blue-700 border-blue-200",
    DEFINITION_DRAFT_READY: "bg-fuchsia-50 text-fuchsia-700 border-fuchsia-200",
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
  scrollChatBottom();
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
  scrollChatBottom();
  return wrap;
}

function appendAiText(text) {
  aiBubble(`<div class="rounded-xl rounded-tl-sm border border-slate-200 bg-white px-3.5 py-3 text-sm text-slate-700 leading-relaxed">${esc(text)}</div>`);
}

function appendAiQuestions(questions) {
  const html = questions
    .map(
      (q) => `
    <div class="px-3.5 py-2.5 border border-amber-200 bg-white rounded-lg">
      <div class="font-medium text-amber-800 text-sm">${esc(q.text)}</div>
      <div class="text-[11px] text-slate-500 mt-1">${esc(q.expects)}${q.required ? " · 必填" : ""}</div>
      ${
        q.options?.length
          ? `<div class="flex flex-wrap gap-1 mt-2">${q.options
              .map(
                (o) =>
                  `<button class="opt-btn text-[11px] bg-amber-50 hover:bg-amber-100 text-amber-800 border border-amber-200 rounded px-1.5 py-0.5" data-answer="${esc(o.label)}">${esc(o.label)}</button>`
              )
              .join("")}</div>`
          : ""
      }
    </div>`
    )
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

function appendAiChangeSet(changeSet) {
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

  const wrap = aiBubble(`
    <div class="rounded-xl border border-fuchsia-200 bg-white overflow-hidden">
      <div class="px-3.5 py-2.5 bg-fuchsia-50 border-b border-fuchsia-100 flex items-center gap-2">
        <iconify-icon class="text-fuchsia-700" icon="mdi:file-swap-outline"></iconify-icon>
        <span class="text-sm font-semibold text-fuchsia-900">AI 变更确认</span>
        <span class="text-[10px] text-fuchsia-700 bg-white border border-fuchsia-200 rounded px-1.5 py-px ml-auto">baseRev=${changeSet.baseDefinitionRevision}</span>
      </div>
      <div class="p-3 space-y-3">
        <div class="space-y-1.5">${changed || '<div class="text-xs text-slate-400">无变更</div>'}</div>
        ${unchangedTags ? `<div><div class="text-[11px] text-slate-500 mb-1">未变化</div><div class="flex flex-wrap gap-1">${unchangedTags}</div></div>` : ""}
        ${impactList ? `<div><div class="text-[11px] text-slate-500 mb-1">影响说明</div><ul class="space-y-0.5 text-xs">${impactList}</ul></div>` : ""}
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

// -------- 请求 --------
async function sendMessage(text) {
  if (!text) return;
  appendUser(text);

  const body = {
    agentProtocolVersion: "1.0",
    conversationId: session.conversationId,
    state: session.state,
    message: text,
    context: DEFAULT_CONTEXT,
  };
  if (session.designSessionId) body.designSessionId = session.designSessionId;
  if (session.reportDefinitionId) body.reportDefinitionId = session.reportDefinitionId;
  if (session.baseDefinitionRevision != null) body.baseDefinitionRevision = session.baseDefinitionRevision;

  // 修改语义：命中某个区域时提示 Agent
  if (session.state === "WAITING_USER_CONFIRM" && /趋势/.test(text)) {
    body.selectedRegionId = "region_hourly_trend";
  }

  let data;
  try {
    const res = await fetch(API_BASE + "/api/agent/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    data = await res.json();
  } catch (err) {
    appendAiText("请求失败：" + err.message);
    return;
  }

  if (data.error) {
    appendAiText(`[${data.error.code}] ${data.error.message}`);
    return;
  }

  // 同步会话态
  setState(data.state);
  if (data.designSessionId) session.designSessionId = data.designSessionId;

  // 分发渲染
  switch (data.state) {
    case "NEEDS_CLARIFICATION":
      appendAiText("我需要你再补充一些信息：");
      appendAiQuestions(data.questions || []);
      break;
    case "BLOCKED_OR_UNSUPPORTED":
      appendAiBlocks(data.blocks || []);
      break;
    case "WAITING_USER_CONFIRM":
      session.definition = data.domainObjects?.reportDefinitionDraft || session.definition;
      session.reportDefinitionId = session.definition?.metadata?.id || session.reportDefinitionId;
      session.baseDefinitionRevision = session.definition?.metadata?.revision ?? session.baseDefinitionRevision;
      appendAiText("已生成报表草稿，请在右侧预览区确认结构是否符合预期。");
      renderPreview(session.definition);
      break;
    case "DEFINITION_DRAFT_READY":
      if (data.domainObjects?.changeSet) {
        appendAiChangeSet(data.domainObjects.changeSet);
      } else {
        appendAiText("已生成修改方案。");
      }
      break;
    default:
      appendAiText(`当前状态：${data.state}`);
  }

  updateMeta();
}

// -------- 应用 ChangeSet --------
function applyChangeSet(cs, bubbleWrap) {
  if (!session.definition) {
    toast("当前没有草稿，无法应用变更");
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

  renderPreview(definition, cs.changed.map((e) => e.regionId));
  setState("WAITING_USER_CONFIRM"); // 应用后回到确认态
  updateMeta();
  toast("变更已应用，revision → " + definition.metadata.revision);
}

// -------- 渲染预览 --------
function renderPreview(definition, highlightRegionIds = []) {
  if (!definition || !definition.regions?.length) {
    previewRoot.innerHTML = "";
    previewRoot.appendChild(previewEmpty);
    return;
  }
  previewRoot.innerHTML = "";

  // 顶部摘要
  const summary = document.createElement("div");
  summary.className = "bg-white rounded-xl border border-slate-200 p-5";
  summary.innerHTML = `
    <div class="flex items-start justify-between gap-4">
      <div>
        <div class="flex items-center gap-2 flex-wrap">
          <h2 class="text-lg font-semibold text-slate-900">${esc(definition.name || "报表草稿")}</h2>
          <span class="inline-flex items-center rounded-md text-xs font-medium px-2 py-1 border bg-blue-50 text-blue-700 border-blue-200">${esc(definition.schemaVersion || "1.0")}</span>
          <span class="inline-flex items-center gap-1 rounded-md text-xs font-medium px-2 py-1 border bg-amber-50 text-amber-700 border-amber-200">
            <iconify-icon class="text-sm" icon="mdi:robot-outline"></iconify-icon>${esc(definition.metadata?.status || "DRAFT")}
          </span>
        </div>
        <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
          <span>revision: <span class="mono">${definition.metadata?.revision ?? "—"}</span></span>
          <span>区域数: <span class="mono">${definition.regions.length}</span></span>
          <span>数据集: ${Object.keys(definition.datasets || {}).map((d) => `<span class="mono">${esc(d)}</span>`).join(", ") || "—"}</span>
        </div>
      </div>
    </div>
  `;
  previewRoot.appendChild(summary);

  // 各区域
  for (const region of definition.regions) {
    const card = renderRegion(region);
    if (highlightRegionIds.includes(region.id)) {
      card.classList.add("region-highlight");
    }
    previewRoot.appendChild(card);
  }
}

function renderRegion(region) {
  const card = document.createElement("div");
  card.className = "bg-white rounded-xl border border-slate-200 p-5";
  card.dataset.regionId = region.id;

  const header = `
    <div class="flex items-start justify-between gap-4">
      <div>
        <h3 class="font-semibold text-slate-900">${esc(region.name || region.component?.title || region.role)}</h3>
        <p class="text-xs text-slate-500 mt-0.5">role: <span class="mono">${esc(region.role)}</span> · component: <span class="mono">${esc(region.component?.type)}</span>${region.component?.chartKind ? ' / <span class="mono">' + esc(region.component.chartKind) + "</span>" : ""}</p>
      </div>
      <span class="mono text-[10px] text-slate-500 bg-slate-100 border border-slate-200 rounded px-1.5 py-0.5 shrink-0">${esc(region.id)}</span>
    </div>`;

  const body = renderRegionBody(region);

  card.innerHTML = header + '<div class="mt-3">' + body + "</div>";
  return card;
}

function renderRegionBody(region) {
  const type = region.component?.type;
  if (type === "TEXT") return renderText(region);
  if (type === "METRIC_CARD") return renderMetricCard(region);
  if (type === "TABLE") return renderTable(region);
  if (type === "CHART") return renderChartPlaceholder(region);
  if (type === "DATA_QUALITY") return renderDataQuality(region);
  if (type === "ANALYSIS") return renderAnalysis(region);
  return `<pre class="mono text-[11px] text-slate-500 bg-slate-50 rounded p-2 overflow-x-auto">${esc(JSON.stringify(region.component, null, 2).slice(0, 400))}</pre>`;
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

// -------- 事件绑定 --------
document.querySelectorAll("[data-quick]").forEach((btn) => {
  btn.addEventListener("click", () => sendMessage(btn.dataset.quick));
});

$("btn-send").addEventListener("click", () => {
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

$("btn-clear").addEventListener("click", () => {
  session.conversationId = "conv_" + Date.now();
  session.state = "ANALYZING";
  session.designSessionId = null;
  session.reportDefinitionId = null;
  session.baseDefinitionRevision = null;
  session.definition = null;
  chatScroll.innerHTML = "";
  previewRoot.innerHTML = "";
  previewRoot.appendChild(previewEmpty);
  setState("ANALYZING");
  updateMeta();
  appendAiText("会话已清空。描述你的报表需求，我会生成结构化报表草稿。");
});

$("btn-regenerate").addEventListener("click", () => {
  session.state = "ANALYZING";
  sendMessage("生成 A 机房昨日能耗日报，包含总耗电、设备排名和趋势分析");
});

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
})();
