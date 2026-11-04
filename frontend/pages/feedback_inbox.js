/**
 * 反馈收件箱页面（20261104）。
 *
 * 纪律：
 *   - **先登录再加载数据**：静态壳可以公开，管理数据必须服务端鉴权；
 *   - 未登录时任何管理接口都返回 401 / 503 —— 这里如实显示，不假装有数据；
 *   - 列表 / 详情 / 改状态 / 备注 / 导出 / 删除都走后端鉴权接口，
 *     不在前端缓存任何管理数据。
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
    $("feedback-admin-export").classList.toggle("hidden", !loggedIn);
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
  }

  async function logout() {
    await api("/api/feedback/admin/logout", { method: "POST" });
    setLoggedIn(false, "");
    currentId = null;
    currentDetail = null;
    renderDetail(null);
    renderList([], 0);
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

  // ---------- 启动 ----------

  function bind() {
    $("feedback-admin-login-form").addEventListener("submit", login);
    $("feedback-admin-logout").addEventListener("click", logout);
    $("feedback-admin-export").addEventListener("click", exportCsv);
    $("feedback-filter-refresh").addEventListener("click", loadList);
    $("feedback-filter-rating").addEventListener("change", loadList);
    $("feedback-filter-status").addEventListener("change", loadList);
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
      return;
    }
    setLoggedIn(false, "");
  }

  boot();
})();
