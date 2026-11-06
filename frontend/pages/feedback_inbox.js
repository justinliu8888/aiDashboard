/**
 * 管理员入口页面（20261105-admin1）。
 *
 * 两个区域：
 *   - **用户反馈**：原有反馈功能（列表 / 详情 / 状态 / 备注 / 删除 / 导出 / 附件）原样保留；
 *   - **编辑经验**：按契约 0003 §3.3 对接 `/api/edit-experiences`（执行记录审核 + 可信经验整理）。
 *
 * 纪律：
 *   - **先登录再加载数据**：静态壳可以公开，管理数据必须服务端鉴权；
 *   - 未登录时任何管理接口都返回 401 / 503 —— 这里如实显示，不假装有数据；
 *   - 列表 / 详情 / 改状态 / 备注 / 导出 / 删除都走后端鉴权接口，
 *     不在前端缓存任何管理数据；
 *   - 经验接口的错误**如实显示后端 detail**（401 / 403 / 404 / 409 / 422 / 503），
 *     不统一说「操作失败」；
 *   - 状态以**服务端为准**：每次写操作后重新拉列表/详情，不靠前端内存。
 */
(function () {
  "use strict";

  const API_BASE = window.AI_REPORT_API_BASE || window.location.origin;

  const RATING_LABELS = { satisfied: "满意", neutral: "一般", dissatisfied: "不满意" };
  const STATUS_LABELS = { pending: "待处理", in_progress: "处理中", resolved: "已解决" };
  const REASON_LABELS = {
    not_understood: "没理解需求",
    wrong_result: "结果不对",
    bad_display: "展示不好",
    operation_failed: "操作失败",
    other: "其他",
  };
  const REFERENCE_LABELS = {
    ok: "引用正常",
    overwritten: "原消息已被覆盖（引用失效）",
    missing: "原消息已不存在",
    expired: "引用已过期",
  };

  const $ = (id) => document.getElementById(id);

  let currentId = null;
  let currentDetail = null;

  // ---------- 小工具 ----------

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function detailText(payload) {
    const detail = payload && payload.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item) => (item && item.msg ? String(item.msg) : ""))
        .filter(Boolean)
        .join("；") || "请求不合法。";
    }
    return "请求失败。";
  }

  async function api(path, options) {
    const response = await fetch(API_BASE + path, {
      credentials: "same-origin",
      ...(options || {}),
    });
    let payload = null;
    const text = await response.text();
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (parseError) {
        payload = null;
      }
    }
    return { ok: response.ok, status: response.status, payload };
  }

  function showLoginError(message) {
    const node = $("feedback-admin-login-error");
    node.textContent = message;
    node.classList.remove("hidden");
  }

  function clearLoginError() {
    const node = $("feedback-admin-login-error");
    node.textContent = "";
    node.classList.add("hidden");
  }

  function setLoggedIn(loggedIn, username) {
    $("feedback-admin-login").classList.toggle("hidden", loggedIn);
    $("feedback-admin-app").classList.toggle("hidden", !loggedIn);
    $("feedback-admin-app").classList.toggle("flex", loggedIn);
    // 「导出 CSV」属于反馈区域；切到编辑经验时不该露出它。
    $("feedback-admin-export").classList.toggle("hidden", !loggedIn || activeTab !== "feedback");
    $("feedback-admin-logout").classList.toggle("hidden", !loggedIn);
    const who = $("feedback-admin-who");
    who.classList.toggle("hidden", !loggedIn);
    who.textContent = loggedIn ? `维护者：${username || ""}` : "";
  }

  // ---------- 登录 ----------

  function showNotConfigured(host) {
    const hint = $("feedback-admin-login-hint");
    const where = host ? `当前实例：${host}。` : "";
    hint.textContent =
      `${where}这个实例没有配置维护者凭据，管理入口是关闭的（服务端返回 503）。` +
      "本机请用已经配置好凭据的那个实例（访问它的 /api/feedback/admin/session，看 configured 是否为 true）；" +
      "正式环境需要在部署环境设置 AI_FEEDBACK_ADMIN_USERNAME / AI_FEEDBACK_ADMIN_PASSWORD_HASH / AI_FEEDBACK_SESSION_SECRET。";
    hint.classList.remove("hidden");
  }

  async function login(event) {
    if (event) event.preventDefault();
    clearLoginError();
    const username = ($("feedback-admin-username").value || "").trim();
    const password = $("feedback-admin-password").value || "";
    if (!username || !password) {
      showLoginError("请填写用户名和密码。");
      return;
    }
    const result = await api("/api/feedback/admin/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (result.status === 503) {
      const host = (result.payload && result.payload.host) || "";
      showNotConfigured(host);
      showLoginError("管理入口未配置。");
      return;
    }
    if (!result.ok) {
      showLoginError(result.status === 429 ? "失败次数过多，请稍后再试。" : detailText(result.payload));
      return;
    }
    $("feedback-admin-password").value = "";
    setLoggedIn(true, (result.payload && result.payload.username) || username);
    await loadList();
    if (activeTab === "experience") await loadExperienceAll();
  }

  async function logout() {
    await api("/api/feedback/admin/logout", { method: "POST" });
    setLoggedIn(false, "");
    currentId = null;
    currentDetail = null;
    renderDetail(null);
    renderList([], 0);
    resetExperienceUi();
  }

  // ---------- 列表 ----------

  async function loadList() {
    const rating = $("feedback-filter-rating").value || "";
    const status = $("feedback-filter-status").value || "";
    const query = new URLSearchParams({ limit: "100", offset: "0" });
    if (rating) query.set("rating", rating);
    if (status) query.set("status", status);
    const result = await api(`/api/feedback/admin/items?${query.toString()}`);
    if (result.status === 401) {
      setLoggedIn(false, "");
      showLoginError("登录已过期，请重新登录。");
      return;
    }
    if (result.status === 503) {
      setLoggedIn(false, "");
      showNotConfigured((result.payload && result.payload.host) || "");
      return;
    }
    if (!result.ok) {
      renderList([], 0, "列表加载失败。");
      return;
    }
    const items = (result.payload && result.payload.items) || [];
    renderList(items, (result.payload && result.payload.total) || items.length);
  }

  function ratingTone(rating) {
    if (rating === "satisfied") return "bg-emerald-50 text-emerald-700 border-emerald-200";
    if (rating === "neutral") return "bg-slate-100 text-slate-600 border-slate-200";
    return "bg-rose-50 text-rose-700 border-rose-200";
  }

  function statusTone(status) {
    if (status === "resolved") return "bg-emerald-50 text-emerald-700 border-emerald-200";
    if (status === "in_progress") return "bg-amber-50 text-amber-700 border-amber-200";
    return "bg-slate-100 text-slate-600 border-slate-200";
  }

  function renderList(items, total, errorMessage) {
    const list = $("feedback-inbox-list");
    list.innerHTML = "";
    $("feedback-inbox-total").textContent = errorMessage
      ? ""
      : `共 ${total} 条`;
    if (errorMessage) {
      list.appendChild(el("div", "text-[11px] text-rose-600 px-2 py-3", errorMessage));
      return;
    }
    if (!items.length) {
      list.appendChild(el("div", "text-[11px] text-slate-400 px-2 py-3", "没有符合条件的反馈。"));
      return;
    }
    items.forEach((item) => {
      const card = el("button", "fb-item");
      card.type = "button";
      card.dataset.feedbackId = item.feedbackId;
      if (item.feedbackId === currentId) card.classList.add("is-active");

      const top = el("div", "fb-item-top");
      const rating = el("span", `fb-tag ${ratingTone(item.rating)}`, RATING_LABELS[item.rating] || item.rating);
      top.appendChild(rating);
      if (item.reason) {
        top.appendChild(el("span", "fb-tag bg-slate-100 text-slate-600 border-slate-200", REASON_LABELS[item.reason] || item.reason));
      }
      const status = el("span", `fb-tag ${statusTone(item.status)}`, STATUS_LABELS[item.status] || item.status);
      top.appendChild(status);
      const time = el("span", "fb-item-time mono", (item.createdAt || "").replace("T", " "));
      top.appendChild(time);
      card.appendChild(top);

      const excerpt = el("div", "fb-item-excerpt", item.commentExcerpt || "（没有填写意见）");
      card.appendChild(excerpt);

      const foot = el("div", "fb-item-foot");
      foot.appendChild(el("span", "mono", item.feedbackId));
      if (item.contact) foot.appendChild(el("span", "", item.contact));
      if (item.hasAttachment) foot.appendChild(el("span", "fb-tag bg-slate-100 text-slate-500 border-slate-200", "有截图"));
      card.appendChild(foot);

      card.addEventListener("click", () => openDetail(item.feedbackId));
      list.appendChild(card);
    });
  }

  // ---------- 详情 ----------

  async function openDetail(feedbackId) {
    currentId = feedbackId;
    clearNotice();
    document.querySelectorAll("#feedback-inbox-list .fb-item").forEach((node) => {
      node.classList.toggle("is-active", node.dataset.feedbackId === feedbackId);
    });
    const result = await api(`/api/feedback/admin/items/${encodeURIComponent(feedbackId)}`);
    if (result.status === 401) {
      setLoggedIn(false, "");
      return;
    }
    if (!result.ok) {
      renderDetail(null, detailText(result.payload));
      return;
    }
    currentDetail = result.payload;
    renderDetail(currentDetail);
  }

  function locatorRows(locator) {
    const rows = [];
    const push = (label, value) => {
      if (value === undefined || value === null || value === "") return;
      rows.push([label, String(value)]);
    };
    push("会话", locator.sessionId);
    push("消息", locator.messageId);
    push("消息键", locator.clientMsgId);
    push("业务状态", locator.businessState);
    push("设计阶段", locator.designPhase);
    push("方案", locator.planId);
    push("方案版本", locator.planRevision);
    push("草稿", locator.draftId);
    push("草稿版本", locator.draftRevision);
    push("错误码", locator.errorCode);
    push("诊断编号", locator.diagnosticRef);
    push("前端版本", locator.frontendVersion);
    push("后端版本", locator.backendVersion);
    return rows;
  }

  function renderDetail(detail, errorMessage) {
    const host = $("feedback-detail");
    host.innerHTML = "";
    if (errorMessage) {
      host.appendChild(el("div", "text-[11px] text-rose-600 px-2 py-3", errorMessage));
      return;
    }
    if (!detail) {
      host.appendChild(el("div", "text-[11px] text-slate-400 px-2 py-3", "从左边选一条反馈看详情。"));
      return;
    }

    const card = el("div", "fb-detail-card");

    const head = el("div", "fb-detail-head");
    const rating = el("span", `fb-tag ${ratingTone(detail.rating)}`, RATING_LABELS[detail.rating] || detail.rating);
    head.appendChild(rating);
    if (detail.reason) {
      head.appendChild(el("span", "fb-tag bg-slate-100 text-slate-600 border-slate-200", REASON_LABELS[detail.reason] || detail.reason));
    }
    head.appendChild(el("span", "fb-tag bg-slate-100 text-slate-600 border-slate-200", detail.kind));
    head.appendChild(el("span", "fb-item-time mono", (detail.createdAt || "").replace("T", " ")));
    card.appendChild(head);

    const idRow = el("div", "fb-detail-id mono", `反馈编号 ${detail.feedbackId}　目标 ${detail.targetId}`);
    card.appendChild(idRow);

    const section = (title, contentNode) => {
      const block = el("div", "fb-detail-section");
      block.appendChild(el("div", "fb-detail-label", title));
      block.appendChild(contentNode);
      card.appendChild(block);
    };

    section("意见", el("div", "fb-detail-text", detail.comment || "（用户没有填写意见）"));
    if (detail.contact) {
      section("署名 / 联系方式", el("div", "fb-detail-text", detail.contact));
    }

    // 附件
    const shotBox = el("div");
    if (detail.hasAttachment) {
      const link = el("a", "fb-link", "下载用户截图（私有附件）");
      link.href = `/api/feedback/admin/items/${encodeURIComponent(detail.feedbackId)}/attachment`;
      link.rel = "noopener";
      shotBox.appendChild(link);
      shotBox.appendChild(el("p", "fb-hint", "截图只对维护者可见；导出 CSV 不包含截图。"));
    } else {
      shotBox.appendChild(el("div", "fb-detail-text", "（没有上传截图）"));
    }
    section("截图", shotBox);

    // 受限引用
    const refBox = el("div");
    refBox.appendChild(el("div", "fb-detail-text", detail.requestExcerpt || "（原需求不可得）"));
    refBox.appendChild(el("div", "fb-detail-text fb-muted", detail.replyExcerpt || "（原回复不可得）"));
    refBox.appendChild(
      el("p", "fb-hint", `原消息引用：${REFERENCE_LABELS[detail.referenceState] || detail.referenceState}${detail.replyFingerprint ? `　指纹 ${detail.replyFingerprint}` : ""}`)
    );
    section("原需求与最终回复（受限引用）", refBox);

    // 定位信息
    const locBox = el("div");
    const table = el("table", "fb-locator");
    const tbody = el("tbody");
    locatorRows(detail.locator || {}).forEach(([label, value]) => {
      const tr = el("tr");
      tr.appendChild(el("th", "", label));
      tr.appendChild(el("td", "mono", value));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    locBox.appendChild(table);
    if ((detail.missingItems || []).length) {
      locBox.appendChild(el("p", "fb-hint", `缺失项：${detail.missingItems.join("、")}`));
    }
    locBox.appendChild(el("p", "fb-hint", `事实来源：${detail.factSource === "server" ? "服务端记录" : "客户端观察（未核实）"}`));
    section("本轮定位信息", locBox);

    // 处理状态 + 备注
    const actionBox = el("div", "fb-detail-actions");
    const statusSelect = document.createElement("select");
    statusSelect.id = "feedback-detail-status";
    statusSelect.className = "fb-input";
    ["pending", "in_progress", "resolved"].forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = STATUS_LABELS[value];
      if (detail.status === value) option.selected = true;
      statusSelect.appendChild(option);
    });
    actionBox.appendChild(statusSelect);

    const noteInput = document.createElement("textarea");
    noteInput.id = "feedback-detail-note";
    noteInput.className = "fb-input fb-textarea";
    noteInput.rows = 2;
    noteInput.maxLength = 2000;
    noteInput.placeholder = "处理备注（只有维护者可见）";
    noteInput.value = detail.note || "";
    actionBox.appendChild(noteInput);

    const saveBtn = el("button", "fb-btn fb-btn-primary", "保存");
    saveBtn.type = "button";
    saveBtn.id = "feedback-detail-save";
    saveBtn.addEventListener("click", () => saveDetail(detail.feedbackId, statusSelect.value, noteInput.value));

    // 删除是**破坏性操作**：先给一个明确的二次确认，再真删。
    const deleteBtn = el("button", "fb-btn fb-btn-danger", "删除");
    deleteBtn.type = "button";
    deleteBtn.id = "feedback-detail-delete";
    deleteBtn.addEventListener("click", () => askDelete(detail.feedbackId));

    const buttonRow = el("div", "fb-detail-buttons");
    buttonRow.appendChild(saveBtn);
    buttonRow.appendChild(deleteBtn);
    actionBox.appendChild(buttonRow);

    const statusLine = el("p", "fb-hint");
    statusLine.id = "feedback-detail-saved";
    actionBox.appendChild(statusLine);
    section("处理状态与备注", actionBox);

    host.appendChild(card);
  }

  async function saveDetail(feedbackId, status, note) {
    const line = $("feedback-detail-saved");
    const result = await api(`/api/feedback/admin/items/${encodeURIComponent(feedbackId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status, note }),
    });
    if (result.status === 401) {
      setLoggedIn(false, "");
      return;
    }
    if (!result.ok) {
      if (line) line.textContent = `保存失败：${detailText(result.payload)}`;
      return;
    }
    if (line) line.textContent = "已保存。";
    await loadList();
  }

  // ---------- 删除 ----------

  function showNotice(message, tone) {
    const node = $("feedback-inbox-notice");
    if (!node) return;
    node.textContent = message;
    node.className = `fb-notice ${tone === "err" ? "fb-notice-err" : "fb-notice-ok"}`;
  }

  function clearNotice() {
    const node = $("feedback-inbox-notice");
    if (!node) return;
    node.textContent = "";
    node.classList.add("hidden");
  }

  function askDelete(feedbackId) {
    const host = $("feedback-detail");
    const card = host.querySelector(".fb-detail-card");
    if (!card) return;
    const old = $("feedback-delete-confirm");
    if (old) old.remove();

    const bar = el("div", "fb-confirm");
    bar.id = "feedback-delete-confirm";
    bar.appendChild(
      el("p", "fb-hint", "确定删除这条反馈？删除后不可恢复，它的私有截图会一并删除。")
    );
    const row = el("div", "fb-confirm-row");
    const okBtn = el("button", "fb-btn fb-btn-danger", "确认删除");
    okBtn.type = "button";
    okBtn.id = "feedback-delete-confirm-ok";
    okBtn.addEventListener("click", () => deleteDetail(feedbackId));
    const cancelBtn = el("button", "fb-btn fb-btn-ghost", "取消");
    cancelBtn.type = "button";
    cancelBtn.id = "feedback-delete-cancel";
    cancelBtn.addEventListener("click", () => bar.remove());
    row.appendChild(okBtn);
    row.appendChild(cancelBtn);
    bar.appendChild(row);
    card.appendChild(bar);
  }

  async function deleteDetail(feedbackId) {
    const bar = $("feedback-delete-confirm");
    const result = await api(`/api/feedback/admin/items/${encodeURIComponent(feedbackId)}`, {
      method: "DELETE",
    });
    if (result.status === 401) {
      setLoggedIn(false, "");
      return;
    }
    if (!result.ok) {
      showNotice(`删除失败：${detailText(result.payload)}`, "err");
      if (bar) bar.remove();
      return;
    }
    if (bar) bar.remove();
    currentId = null;
    currentDetail = null;
    renderDetail(null);
    await loadList();
    showNotice(
      result.payload && result.payload.attachmentRemoved
        ? "已删除这条反馈（含私有截图）。"
        : "已删除这条反馈。",
      "ok"
    );
  }

  // ---------- 导出 ----------

  async function exportCsv() {
    const rating = $("feedback-filter-rating").value || "";
    const status = $("feedback-filter-status").value || "";
    const query = new URLSearchParams();
    if (rating) query.set("rating", rating);
    if (status) query.set("status", status);
    const response = await fetch(`${API_BASE}/api/feedback/admin/export?${query.toString()}`, {
      credentials: "same-origin",
    });
    if (!response.ok) {
      const hint = $("feedback-admin-login-hint");
      hint.textContent = "导出失败：请确认已登录，或筛选条件是否合法。";
      hint.classList.remove("hidden");
      return;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "feedback-export.csv";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }

  // =========================================================
  // 区域二：编辑经验（契约 0003 §3.3）
  //
  // 前缀 `/api/edit-experiences`：
  //   GET   /admin/records?status=&limit=&offset=
  //   GET   /admin/records/{record_id}
  //   GET   /admin/experiences?includeDisabled=&limit=&offset=
  //   POST  /admin/records/{record_id}/approve  {method, conditions, expectedVersion}
  //   POST  /admin/records/{record_id}/reject   {reason?, expectedVersion}
  //   PATCH /admin/experiences/{experience_id}   {method?, conditions?, disabled?, expectedVersion}
  //
  // 鉴权**复用**反馈那边的登录 cookie（fb_admin），不另造体系。
  // 写操作带 expectedVersion（乐观锁）：版本不符 → 409，**数据不变**，如实显示。
  // =========================================================

  const EXP_PREFIX = "/api/edit-experiences";
  const RECORD_STATUS_LABELS = { pending: "待审", approved: "已批准", rejected: "已拒绝" };
  const EXPERIENCE_STATUS_LABELS = { approved: "已批准", disabled: "已禁用" };

  let activeTab = "feedback";
  let currentRecordId = null;
  let currentRecord = null;
  let currentExperience = null;

  // ---------- 区域切换 ----------

  function switchTab(tab) {
    activeTab = tab === "experience" ? "experience" : "feedback";
    const isFeedback = activeTab === "feedback";
    $("btn-tab-feedback").classList.toggle("is-active", isFeedback);
    $("btn-tab-experience").classList.toggle("is-active", !isFeedback);
    $("btn-tab-feedback").setAttribute("aria-selected", isFeedback ? "true" : "false");
    $("btn-tab-experience").setAttribute("aria-selected", isFeedback ? "false" : "true");
    $("feedback-panel").classList.toggle("hidden", !isFeedback);
    $("experience-panel").classList.toggle("hidden", isFeedback);
    // 「导出 CSV」只属于反馈区域。
    $("feedback-admin-export").classList.toggle("hidden", !isFeedback);
    if (!isFeedback) loadExperienceAll();
  }

  async function loadExperienceAll() {
    // 没登录时管理数据一律不取。
    if ($("feedback-admin-app").classList.contains("hidden")) return;
    await loadExperienceRecords();
    await loadExperienceList();
  }

  function resetExperienceUi() {
    currentRecordId = null;
    currentRecord = null;
    currentExperience = null;
    const list = $("experience-record-list");
    if (list) list.innerHTML = "";
    const detail = $("experience-record-detail");
    if (detail) detail.innerHTML = "";
    const expList = $("experience-list");
    if (expList) expList.innerHTML = "";
    const edit = $("experience-edit");
    if (edit) edit.innerHTML = "";
    const total = $("experience-record-total");
    if (total) total.textContent = "";
    const expTotal = $("experience-list-total");
    if (expTotal) expTotal.textContent = "";
    clearExpNotice();
  }

  // ---------- 错误语义 ----------

  function expFailure(result, fallback) {
    const detail = detailText(result.payload);
    if (result.status === 401) return `未登录（401）：${detail}`;
    if (result.status === 403) return `跨源写请求被拒绝（403）：${detail}`;
    if (result.status === 404) return `对象不存在（404）：${detail}`;
    if (result.status === 409) return `版本冲突，数据未改动（409）：${detail}`;
    if (result.status === 422) return `请求不合法（422）：${detail}`;
    if (result.status === 503) return `管理员未配置，管理入口关闭（503）：${detail}`;
    return `${fallback}（${result.status}）：${detail}`;
  }

  // 401 / 503 会让整个管理入口失效 —— 如实回到登录页，不假装有数据。
  // 后端返回的 detail 也**如实显示**（不换成笼统说法）。
  function expAuthFailure(result) {
    if (result.status === 401) {
      setLoggedIn(false, "");
      showLoginError(`未登录（401）：${detailText(result.payload)}`);
      return true;
    }
    if (result.status === 503) {
      setLoggedIn(false, "");
      showNotConfigured((result.payload && result.payload.host) || "");
      showLoginError(`管理员未配置，管理入口关闭（503）：${detailText(result.payload)}`);
      return true;
    }
    return false;
  }

  function showExpNotice(message, tone) {
    const node = $("experience-notice");
    if (!node) return;
    node.textContent = message;
    node.className = `fb-notice ${tone === "err" ? "fb-notice-err" : "fb-notice-ok"}`;
  }

  function clearExpNotice() {
    const node = $("experience-notice");
    if (!node) return;
    node.textContent = "";
    node.classList.add("hidden");
  }

  function setExpEditMessage(message, isError) {
    const node = $("experience-edit-message");
    if (!node) return;
    node.textContent = message;
    node.className = isError ? "fb-hint fb-warn" : "fb-hint";
  }

  // 提交期间禁用按钮，防重复提交。
  async function withBusy(button, task) {
    if (button) button.disabled = true;
    try {
      return await task();
    } finally {
      if (button) button.disabled = false;
    }
  }

  function truncateText(text, max) {
    const value = String(text || "");
    return value.length > max ? `${value.slice(0, max)}…` : value;
  }

  function prettyJson(value) {
    if (value === undefined || value === null) return "";
    try {
      return JSON.stringify(value, null, 2);
    } catch (error) {
      return String(value);
    }
  }

  function recordStatusTone(status) {
    if (status === "approved") return "bg-emerald-50 text-emerald-700 border-emerald-200";
    if (status === "pending") return "bg-amber-50 text-amber-700 border-amber-200";
    return "bg-rose-50 text-rose-700 border-rose-200";
  }

  function experienceStatusTone(status) {
    if (status === "disabled") return "bg-slate-100 text-slate-600 border-slate-200";
    return "bg-emerald-50 text-emerald-700 border-emerald-200";
  }

  // ---------- 执行记录 ----------

  async function loadExperienceRecords() {
    const status = $("experience-filter-status").value || "";
    const query = new URLSearchParams({ limit: "50", offset: "0" });
    if (status) query.set("status", status);
    const result = await api(`${EXP_PREFIX}/admin/records?${query.toString()}`);
    if (expAuthFailure(result)) return;
    if (!result.ok) {
      renderExperienceRecords([], 0, expFailure(result, "记录列表加载失败"));
      return;
    }
    const items = (result.payload && result.payload.items) || [];
    renderExperienceRecords(items, (result.payload && result.payload.total) || items.length);
  }

  function renderExperienceRecords(items, total, errorMessage) {
    const list = $("experience-record-list");
    list.innerHTML = "";
    $("experience-record-total").textContent = errorMessage ? "" : `共 ${total} 条`;
    if (errorMessage) {
      list.appendChild(el("div", "text-[11px] text-rose-600 px-2 py-3", errorMessage));
      return;
    }
    if (!items.length) {
      list.appendChild(el("div", "text-[11px] text-slate-400 px-2 py-3", "没有符合条件的执行记录。"));
      return;
    }
    items.forEach((item) => {
      const card = el("button", "fb-item");
      card.type = "button";
      card.dataset.recordId = item.recordId;
      if (item.recordId === currentRecordId) card.classList.add("is-active");

      const top = el("div", "fb-item-top");
      top.appendChild(el("span", `fb-tag ${recordStatusTone(item.status)}`, RECORD_STATUS_LABELS[item.status] || item.status));
      top.appendChild(el("span", "fb-item-time mono", (item.createdAt || "").replace("T", " ")));
      card.appendChild(top);

      card.appendChild(
        el("div", "fb-item-excerpt", item.originalRequest ? truncateText(item.originalRequest, 120) : "（原要求不可得）")
      );
      if (item.supplement) {
        card.appendChild(el("div", "fb-item-excerpt fb-muted", `补充：${truncateText(item.supplement, 80)}`));
      }

      const foot = el("div", "fb-item-foot");
      foot.appendChild(el("span", "mono", item.recordId));
      if (item.planId) foot.appendChild(el("span", "mono", `plan ${item.planId}`));
      if (item.capabilityVersion) foot.appendChild(el("span", "mono", `能力 ${item.capabilityVersion}`));
      card.appendChild(foot);

      card.addEventListener("click", () => {
        clearExpNotice();
        openExperienceRecord(item.recordId);
      });
      list.appendChild(card);
    });
  }

  async function openExperienceRecord(recordId) {
    currentRecordId = recordId;
    currentRecord = null;
    document.querySelectorAll("#experience-record-list .fb-item").forEach((node) => {
      node.classList.toggle("is-active", node.dataset.recordId === recordId);
    });
    const result = await api(`${EXP_PREFIX}/admin/records/${encodeURIComponent(recordId)}`);
    if (expAuthFailure(result)) return;
    if (!result.ok) {
      renderExperienceRecord(null, expFailure(result, "记录详情加载失败"));
      return;
    }
    currentRecord = result.payload || null;
    renderExperienceRecord(currentRecord);
  }

  function renderExperienceRecord(record, errorMessage) {
    const host = $("experience-record-detail");
    host.innerHTML = "";
    if (errorMessage) {
      host.appendChild(el("div", "text-[11px] text-rose-600 px-2 py-3", errorMessage));
      return;
    }
    if (!record) {
      host.appendChild(el("div", "text-[11px] text-slate-400 px-2 py-3", "从左边选一条执行记录看详情与审核。"));
      return;
    }

    const card = el("div", "fb-detail-card");

    const head = el("div", "fb-detail-head");
    head.appendChild(el("span", `fb-tag ${recordStatusTone(record.status)}`, RECORD_STATUS_LABELS[record.status] || record.status));
    head.appendChild(el("span", "fb-tag bg-slate-100 text-slate-600 border-slate-200", `版本 v${record.version}`));
    head.appendChild(el("span", "fb-item-time mono", (record.createdAt || "").replace("T", " ")));
    card.appendChild(head);

    card.appendChild(el("div", "fb-detail-id mono", `记录 ${record.recordId}`));

    const section = (title, contentNode) => {
      const block = el("div", "fb-detail-section");
      block.appendChild(el("div", "fb-detail-label", title));
      block.appendChild(contentNode);
      card.appendChild(block);
    };

    const metaBox = el("div");
    const metaTable = el("table", "fb-locator");
    const metaBody = el("tbody");
    [
      ["计划", record.planId],
      ["会话", record.sessionId],
      ["草稿", record.draftId],
      ["事项", record.itemId],
      ["能力版本", record.capabilityVersion],
      ["审核人", record.reviewer],
      ["审核时间", record.reviewedAt],
      ["拒绝理由", record.reason],
    ].forEach(([label, value]) => {
      if (value === undefined || value === null || value === "") return;
      const tr = el("tr");
      tr.appendChild(el("th", "", label));
      tr.appendChild(el("td", "mono", String(value)));
      metaBody.appendChild(tr);
    });
    metaTable.appendChild(metaBody);
    metaBox.appendChild(metaTable);
    section("来源与审核信息", metaBox);

    section("原要求", el("div", "fb-detail-text", record.originalRequest || "（原要求不可得）"));
    if (record.supplement) {
      section("补充要求", el("div", "fb-detail-text", record.supplement));
    }
    // 后端已经脱敏，这里**只展示**，不自己再猜哪些字段敏感。
    section("操作清单（服务端已脱敏）", el("pre", "fb-json", prettyJson(record.operations)));
    section("结果摘要（服务端已脱敏）", el("pre", "fb-json", prettyJson(record.result)));

    if (record.status === "pending") {
      section("审核", buildReviewForm(record));
    } else {
      const done = el("div");
      done.appendChild(
        el("div", "fb-detail-text", `这条记录已经审核过（${RECORD_STATUS_LABELS[record.status] || record.status}），不能重复批准。`)
      );
      if (record.reason) done.appendChild(el("div", "fb-detail-text fb-muted", `理由：${record.reason}`));
      section("审核", done);
    }

    host.appendChild(card);
  }

  function buildReviewForm(record) {
    const box = el("div", "fb-detail-actions");

    const methodInput = document.createElement("textarea");
    methodInput.id = "experience-method";
    methodInput.className = "fb-input fb-textarea";
    methodInput.rows = 3;
    methodInput.placeholder = "可复用的方法描述（写可移植的步骤，别写具体区块 ID）";

    const conditionsInput = document.createElement("textarea");
    conditionsInput.id = "experience-conditions";
    conditionsInput.className = "fb-input fb-textarea";
    conditionsInput.rows = 2;
    conditionsInput.placeholder = "在什么情况下可以复用这个方法";

    const reasonInput = document.createElement("input");
    reasonInput.id = "experience-reason";
    reasonInput.type = "text";
    reasonInput.className = "fb-input";
    reasonInput.placeholder = "拒绝理由（只在拒绝时使用，可留空）";

    box.appendChild(el("label", "fb-field-label", "可复用方法（必填）"));
    box.appendChild(methodInput);
    box.appendChild(el("label", "fb-field-label", "适用条件"));
    box.appendChild(conditionsInput);
    box.appendChild(el("label", "fb-field-label", "拒绝理由"));
    box.appendChild(reasonInput);

    const row = el("div", "fb-detail-buttons");
    const approveBtn = el("button", "fb-btn fb-btn-primary", "批准并生成经验");
    approveBtn.type = "button";
    approveBtn.id = "experience-approve";
    approveBtn.addEventListener("click", () => approveExperienceRecord());

    const rejectBtn = el("button", "fb-btn fb-btn-danger", "拒绝");
    rejectBtn.type = "button";
    rejectBtn.id = "experience-reject";
    rejectBtn.addEventListener("click", () => rejectExperienceRecord());

    row.appendChild(approveBtn);
    row.appendChild(rejectBtn);
    box.appendChild(row);

    const hint = el("p", "fb-hint", `提交会带上当前版本 v${record.version}。记录被别人改动过 → 409，数据不变，请刷新后重试。`);
    box.appendChild(hint);
    return box;
  }

  async function approveExperienceRecord() {
    const record = currentRecord;
    if (!record) return;
    const method = ($("experience-method").value || "").trim();
    if (!method) {
      showExpNotice("请先填写「可复用方法」再批准。", "err");
      return;
    }
    const conditions = ($("experience-conditions").value || "").trim();
    await withBusy($("experience-approve"), async () => {
      const result = await api(`${EXP_PREFIX}/admin/records/${encodeURIComponent(record.recordId)}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ method, conditions, expectedVersion: Number(record.version) }),
      });
      if (expAuthFailure(result)) return;
      if (!result.ok) {
        showExpNotice(expFailure(result, "批准失败"), "err");
        return;
      }
      const created = (result.payload && result.payload.experience) || null;
      const experienceId = (created && created.experienceId) || "";
      showExpNotice(`已批准，生成可信经验 ${experienceId}。`, "ok");
      await loadExperienceRecords();
      await loadExperienceList();
      await openExperienceRecord(record.recordId);
    });
  }

  async function rejectExperienceRecord() {
    const record = currentRecord;
    if (!record) return;
    const reason = ($("experience-reason").value || "").trim();
    await withBusy($("experience-reject"), async () => {
      const result = await api(`${EXP_PREFIX}/admin/records/${encodeURIComponent(record.recordId)}/reject`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason, expectedVersion: Number(record.version) }),
      });
      if (expAuthFailure(result)) return;
      if (!result.ok) {
        showExpNotice(expFailure(result, "拒绝失败"), "err");
        return;
      }
      showExpNotice("已拒绝这条记录，没有产生可复用经验。", "ok");
      await loadExperienceRecords();
      await openExperienceRecord(record.recordId);
    });
  }

  // ---------- 可信经验 ----------

  async function loadExperienceList() {
    const includeDisabled = $("experience-include-disabled").checked;
    const query = new URLSearchParams({ limit: "50", offset: "0" });
    if (includeDisabled) query.set("includeDisabled", "true");
    const result = await api(`${EXP_PREFIX}/admin/experiences?${query.toString()}`);
    if (expAuthFailure(result)) return;
    if (!result.ok) {
      renderExperienceList([], 0, expFailure(result, "经验列表加载失败"));
      return;
    }
    const items = (result.payload && result.payload.items) || [];
    renderExperienceList(items, (result.payload && result.payload.total) || items.length);
  }

  function renderExperienceList(items, total, errorMessage) {
    const list = $("experience-list");
    list.innerHTML = "";
    $("experience-list-total").textContent = errorMessage ? "" : `共 ${total} 条`;
    if (errorMessage) {
      list.appendChild(el("div", "text-[11px] text-rose-600 px-2 py-3", errorMessage));
      return;
    }
    if (!items.length) {
      list.appendChild(el("div", "text-[11px] text-slate-400 px-2 py-3", "还没有已批准的经验。"));
      return;
    }
    items.forEach((item) => {
      const card = el("div", "fb-item fb-item-static");
      card.dataset.experienceId = item.experienceId;

      const top = el("div", "fb-item-top");
      top.appendChild(el("span", `fb-tag ${experienceStatusTone(item.status)}`, EXPERIENCE_STATUS_LABELS[item.status] || item.status));
      top.appendChild(el("span", "fb-item-time mono", `v${item.version}`));
      card.appendChild(top);

      card.appendChild(el("div", "fb-detail-text", item.method || "（方法为空）"));
      if (item.conditions) {
        card.appendChild(el("div", "fb-item-excerpt fb-muted", `适用条件：${item.conditions}`));
      }

      const foot = el("div", "fb-item-foot");
      foot.appendChild(el("span", "mono", item.experienceId));
      if (item.recordId) foot.appendChild(el("span", "mono", `来源 ${item.recordId}`));
      if (item.capabilityVersion) foot.appendChild(el("span", "mono", `能力 ${item.capabilityVersion}`));
      card.appendChild(foot);

      const row = el("div", "fb-detail-buttons");
      const editBtn = el("button", "fb-btn fb-btn-ghost", "整理");
      editBtn.type = "button";
      editBtn.id = "experience-item-edit";
      editBtn.addEventListener("click", () => {
        clearExpNotice();
        openExperienceEditor(item);
      });
      row.appendChild(editBtn);

      const toggleBtn = el("button", "fb-btn fb-btn-ghost", item.status === "disabled" ? "恢复" : "禁用");
      toggleBtn.type = "button";
      toggleBtn.id = "experience-item-toggle";
      toggleBtn.addEventListener("click", () => toggleExperience(item, item.status !== "disabled"));
      row.appendChild(toggleBtn);

      card.appendChild(row);
      list.appendChild(card);
    });
  }

  async function toggleExperience(item, disabled) {
    const result = await api(`${EXP_PREFIX}/admin/experiences/${encodeURIComponent(item.experienceId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ disabled: !!disabled, expectedVersion: Number(item.version) }),
    });
    if (expAuthFailure(result)) return;
    if (!result.ok) {
      showExpNotice(expFailure(result, disabled ? "禁用失败" : "恢复失败"), "err");
      return;
    }
    showExpNotice(disabled ? "已禁用这条经验（检索时不再返回）。" : "已恢复这条经验。", "ok");
    await loadExperienceList();
  }

  function openExperienceEditor(item) {
    currentExperience = item;
    const host = $("experience-edit");
    host.innerHTML = "";
    const card = el("div", "fb-detail-card");

    const head = el("div", "fb-detail-head");
    head.appendChild(el("span", `fb-tag ${experienceStatusTone(item.status)}`, EXPERIENCE_STATUS_LABELS[item.status] || item.status));
    head.appendChild(el("span", "fb-tag bg-slate-100 text-slate-600 border-slate-200", `版本 v${item.version}`));
    card.appendChild(head);
    card.appendChild(el("div", "fb-detail-id mono", `经验 ${item.experienceId}`));

    const section = (title, contentNode) => {
      const block = el("div", "fb-detail-section");
      block.appendChild(el("div", "fb-detail-label", title));
      block.appendChild(contentNode);
      card.appendChild(block);
    };

    const metaBox = el("div");
    const metaTable = el("table", "fb-locator");
    const metaBody = el("tbody");
    [
      ["来源记录", item.recordId],
      ["能力版本", item.capabilityVersion],
      ["审核人", item.reviewer],
      ["创建时间", item.createdAt],
      ["更新时间", item.updatedAt],
    ].forEach(([label, value]) => {
      if (value === undefined || value === null || value === "") return;
      const tr = el("tr");
      tr.appendChild(el("th", "", label));
      tr.appendChild(el("td", "mono", String(value)));
      metaBody.appendChild(tr);
    });
    metaTable.appendChild(metaBody);
    metaBox.appendChild(metaTable);
    section("来源信息", metaBox);

    const methodInput = document.createElement("textarea");
    methodInput.id = "experience-edit-method";
    methodInput.className = "fb-input fb-textarea";
    methodInput.rows = 3;
    methodInput.value = item.method || "";
    methodInput.placeholder = "可复用的方法描述";

    const conditionsInput = document.createElement("textarea");
    conditionsInput.id = "experience-edit-conditions";
    conditionsInput.className = "fb-input fb-textarea";
    conditionsInput.rows = 2;
    conditionsInput.value = item.conditions || "";
    conditionsInput.placeholder = "在什么情况下可以复用这个方法";

    const form = el("div", "fb-detail-actions");
    form.appendChild(el("label", "fb-field-label", "可复用方法（必填）"));
    form.appendChild(methodInput);
    form.appendChild(el("label", "fb-field-label", "适用条件"));
    form.appendChild(conditionsInput);

    const row = el("div", "fb-detail-buttons");
    const saveBtn = el("button", "fb-btn fb-btn-primary", "保存");
    saveBtn.type = "button";
    saveBtn.id = "experience-edit-save";
    saveBtn.addEventListener("click", () => saveExperience());
    const cancelBtn = el("button", "fb-btn fb-btn-ghost", "关闭");
    cancelBtn.type = "button";
    cancelBtn.id = "experience-edit-cancel";
    cancelBtn.addEventListener("click", () => {
      currentExperience = null;
      host.innerHTML = "";
      host.appendChild(el("div", "text-[11px] text-slate-400 px-2 py-3", "选一条经验做「整理 / 禁用 / 恢复」。"));
    });
    row.appendChild(saveBtn);
    row.appendChild(cancelBtn);
    form.appendChild(row);

    const msg = el("p", "fb-hint", `保存会带上当前版本 v${item.version}。经验被别人改动过 → 409，数据不变，请刷新后重试。`);
    msg.id = "experience-edit-message";
    form.appendChild(msg);
    section("整理", form);

    host.appendChild(card);
  }

  async function saveExperience() {
    const item = currentExperience;
    if (!item) return;
    const method = ($("experience-edit-method").value || "").trim();
    if (!method) {
      setExpEditMessage("请填写「可复用方法」。", true);
      return;
    }
    const conditions = ($("experience-edit-conditions").value || "").trim();
    await withBusy($("experience-edit-save"), async () => {
      const result = await api(`${EXP_PREFIX}/admin/experiences/${encodeURIComponent(item.experienceId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ method, conditions, expectedVersion: Number(item.version) }),
      });
      if (expAuthFailure(result)) return;
      if (!result.ok) {
        setExpEditMessage(expFailure(result, "保存失败"), true);
        return;
      }
      await loadExperienceList();
      const updated = (result.payload && result.payload.experience) || null;
      if (updated) openExperienceEditor(updated);
      setExpEditMessage("已保存。", false);
    });
  }

  // ---------- 启动 ----------

  function bind() {
    $("feedback-admin-login-form").addEventListener("submit", login);
    $("feedback-admin-logout").addEventListener("click", logout);
    $("feedback-admin-export").addEventListener("click", exportCsv);
    $("feedback-filter-refresh").addEventListener("click", loadList);
    $("feedback-filter-rating").addEventListener("change", loadList);
    $("feedback-filter-status").addEventListener("change", loadList);

    $("btn-tab-feedback").addEventListener("click", () => switchTab("feedback"));
    $("btn-tab-experience").addEventListener("click", () => switchTab("experience"));
    $("experience-records-refresh").addEventListener("click", loadExperienceRecords);
    $("experience-filter-status").addEventListener("change", loadExperienceRecords);
    $("experience-list-refresh").addEventListener("click", loadExperienceList);
    $("experience-include-disabled").addEventListener("change", loadExperienceList);
  }

  async function boot() {
    bind();
    // 先问会话状态（这个接口**永远 200**，不返回反馈数据）：
    // 没配置 → 管理入口关闭；已登录 → 直接进收件箱；没登录 → 停在登录页。
    const result = await api("/api/feedback/admin/session");
    const payload = result.payload || {};
    if (payload.configured === false) {
      showNotConfigured(payload.host || "");
    }
    if (result.ok && payload.authenticated) {
      setLoggedIn(true, payload.username || "");
      await loadList();
      if (activeTab === "experience") await loadExperienceAll();
      return;
    }
    setLoggedIn(false, "");
  }

  boot();
})();
