/**
 * 反馈模块（20261104「回复评价与反馈收件箱」）。
 *
 * 它只做三件事，接口刻意保持很小：
 *   - attach(host, target)：给一个**稳定宿主**挂一个「反馈」入口；
 *   - refreshSubmitted(targetIds)：问服务端「这个浏览器提交过哪些」；
 *   - destroy()：拆掉弹窗与监听。
 *
 * 纪律（务必守住）：
 *   - **不调用模型**：不碰 sendMessage / queueAdvance / 停止 / 接受 / 保存；
 *   - **不改业务状态**：报表输入锁、菜单、定义、版本一概不动；
 *   - 反馈弹窗**独立**于报表输入锁 —— 锁着输入时照样能打开；
 *   - 动态 DOM 一律用 textContent 渲染文字（不拼 innerHTML）。
 */
(function () {
  "use strict";

  const API_BASE = window.AI_REPORT_API_BASE || window.location.origin;

  const RATINGS = [
    { value: "satisfied", label: "满意" },
    { value: "neutral", label: "一般" },
    { value: "dissatisfied", label: "不满意" },
  ];

  const REASONS = [
    { value: "", label: "不选" },
    { value: "not_understood", label: "没理解需求" },
    { value: "wrong_result", label: "结果不对" },
    { value: "bad_display", label: "展示不好" },
    { value: "operation_failed", label: "操作失败" },
    { value: "other", label: "其他" },
  ];

  const MAX_COMMENT = 2000;
  const MAX_CONTACT = 100;

  /** targetId -> { button, host } */
  const triggers = new Map();

  let dialog = null;
  let current = null; // { target, host, submissionId }

  // ---------- 小工具 ----------

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function newSubmissionId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return "s_" + window.crypto.randomUUID().replace(/-/g, "");
    }
    return "s_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 12);
  }

  function detailText(payload) {
    const detail = payload && payload.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item) => (item && item.msg ? String(item.msg) : ""))
        .filter(Boolean)
        .join("；") || "提交内容不合法。";
    }
    return "提交失败，请重试。";
  }

  // ---------- 弹窗 ----------

  function ensureDialog() {
    if (dialog && document.body.contains(dialog)) return dialog;

    dialog = document.createElement("div");
    dialog.id = "feedback-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-labelledby", "feedback-dialog-title");
    dialog.className = "fb-overlay hidden";

    const backdrop = el("div", "fb-backdrop");
    backdrop.addEventListener("click", closeDialog);

    const panel = el("div", "fb-panel");

    const head = el("div", "fb-head");
    const title = el("h2", "fb-title", "这次结果怎么样？");
    title.id = "feedback-dialog-title";
    head.appendChild(title);
    const closeBtn = el("button", "fb-icon-btn", "×");
    closeBtn.type = "button";
    closeBtn.setAttribute("aria-label", "关闭");
    closeBtn.addEventListener("click", closeDialog);
    head.appendChild(closeBtn);
    panel.appendChild(head);

    const body = el("div", "fb-body");
    body.id = "feedback-body";

    // 满意度（必选）
    const ratingField = el("div", "fb-field");
    ratingField.appendChild(el("label", "fb-label", "满意度（必选）"));
    const ratingRow = el("div", "fb-rating-row");
    ratingRow.setAttribute("role", "radiogroup");
    RATINGS.forEach((item) => {
      const label = el("label", "fb-rating");
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "rating";
      input.value = item.value;
      input.addEventListener("change", syncReasonRow);
      label.appendChild(input);
      label.appendChild(el("span", "fb-rating-text", item.label));
      ratingRow.appendChild(label);
    });
    ratingField.appendChild(ratingRow);
    body.appendChild(ratingField);

    // 不满意时的原因（选填）
    const reasonField = el("div", "fb-field hidden");
    reasonField.id = "feedback-reason-field";
    reasonField.appendChild(el("label", "fb-label", "不满意的原因（选填）"));
    const reasonSelect = document.createElement("select");
    reasonSelect.id = "feedback-reason";
    reasonSelect.className = "fb-input";
    REASONS.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = item.label;
      reasonSelect.appendChild(option);
    });
    reasonField.appendChild(reasonSelect);
    body.appendChild(reasonField);

    // 意见
    const commentField = el("div", "fb-field");
    commentField.appendChild(el("label", "fb-label", `意见（选填，最多 ${MAX_COMMENT} 字）`));
    const comment = document.createElement("textarea");
    comment.id = "feedback-comment";
    comment.className = "fb-input fb-textarea";
    comment.rows = 3;
    comment.maxLength = MAX_COMMENT;
    comment.placeholder = "哪里好、哪里不对，写下来会更有帮助";
    commentField.appendChild(comment);
    body.appendChild(commentField);

    // 署名 / 联系方式
    const contactField = el("div", "fb-field");
    contactField.appendChild(el("label", "fb-label", `姓名或联系方式（选填，最多 ${MAX_CONTACT} 字）`));
    const contact = document.createElement("input");
    contact.id = "feedback-contact";
    contact.type = "text";
    contact.className = "fb-input";
    contact.maxLength = MAX_CONTACT;
    contact.placeholder = "留个称呼，方便回访";
    contactField.appendChild(contact);
    body.appendChild(contactField);

    // 截图
    const shotField = el("div", "fb-field");
    shotField.appendChild(el("label", "fb-label", "截图（选填，单张 PNG/JPEG/WebP，5 MB 以内）"));
    const shot = document.createElement("input");
    shot.id = "feedback-screenshot";
    shot.type = "file";
    shot.accept = "image/png,image/jpeg,image/webp";
    shot.addEventListener("change", () => {
      const file = shot.files && shot.files[0];
      const hint = document.getElementById("feedback-screenshot-hint");
      if (!hint) return;
      if (!file) {
        hint.textContent = "截图可能包含敏感信息，提交前请检查或裁剪。";
        hint.classList.remove("fb-warn");
        return;
      }
      const ok = file.size <= 5 * 1024 * 1024;
      hint.textContent = ok
        ? `已选择：${file.name}（${Math.round(file.size / 1024)} KB）。截图可能包含敏感信息，提交前请检查或裁剪。`
        : `文件超过 5 MB，请换一张小一点的。`;
      hint.classList.toggle("fb-warn", !ok);
    });
    shotField.appendChild(shot);
    const shotHint = el("p", "fb-hint", "截图可能包含敏感信息，提交前请检查或裁剪。");
    shotHint.id = "feedback-screenshot-hint";
    shotField.appendChild(shotHint);
    body.appendChild(shotField);

    // 附带信息说明（提交前告知范围）
    const scope = el("p", "fb-hint");
    scope.id = "feedback-scope";
    scope.textContent =
      "提交会附带这次回复的结构性定位信息（阶段、方案/草稿版本、错误码等），不含完整对话、原始数据与报表数值。";
    body.appendChild(scope);

    const error = el("p", "fb-error hidden");
    error.id = "feedback-error";
    body.appendChild(error);

    const receipt = el("div", "fb-receipt hidden");
    receipt.id = "feedback-receipt";
    body.appendChild(receipt);

    const actions = el("div", "fb-actions");
    const cancelBtn = el("button", "fb-btn fb-btn-ghost", "取消");
    cancelBtn.type = "button";
    cancelBtn.id = "feedback-cancel";
    cancelBtn.addEventListener("click", closeDialog);
    actions.appendChild(cancelBtn);
    const submitBtn = el("button", "fb-btn fb-btn-primary", "提交");
    submitBtn.type = "button";
    submitBtn.id = "feedback-submit";
    submitBtn.addEventListener("click", submitFeedback);
    actions.appendChild(submitBtn);
    body.appendChild(actions);

    panel.appendChild(body);
    dialog.appendChild(backdrop);
    dialog.appendChild(panel);
    document.body.appendChild(dialog);

    dialog.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeDialog();
    });
    return dialog;
  }

  function syncReasonRow() {
    const field = document.getElementById("feedback-reason-field");
    if (!field) return;
    const checked = dialog.querySelector('input[name="rating"]:checked');
    const dissatisfied = checked && checked.value === "dissatisfied";
    field.classList.toggle("hidden", !dissatisfied);
  }

  function showError(message) {
    const node = document.getElementById("feedback-error");
    if (!node) return;
    node.textContent = message;
    node.classList.remove("hidden");
  }

  function clearError() {
    const node = document.getElementById("feedback-error");
    if (!node) return;
    node.textContent = "";
    node.classList.add("hidden");
  }

  function showReceipt(feedbackId) {
    const receipt = document.getElementById("feedback-receipt");
    if (!receipt) return;
    receipt.textContent = `已收到，反馈编号 ${feedbackId}。`;
    receipt.classList.remove("hidden");
    const submitBtn = document.getElementById("feedback-submit");
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = "已提交";
    }
    const cancel = document.getElementById("feedback-cancel");
    if (cancel) cancel.textContent = "关闭";
  }

  // ---------- 打开 / 关闭 ----------

  function openDialog(target, host) {
    ensureDialog();
    if (!target || !target.id) return;
    current = { target, host, submissionId: newSubmissionId() };

    const form = dialog.querySelector("#feedback-body");
    const reasonField = document.getElementById("feedback-reason-field");
    const error = document.getElementById("feedback-error");
    const receipt = document.getElementById("feedback-receipt");
    const submitBtn = document.getElementById("feedback-submit");
    const cancel = document.getElementById("feedback-cancel");
    if (reasonField) reasonField.classList.add("hidden");
    if (error) {
      error.textContent = "";
      error.classList.add("hidden");
    }
    if (receipt) {
      receipt.textContent = "";
      receipt.classList.add("hidden");
    }
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = "提交";
    }
    if (cancel) cancel.textContent = "取消";

    // 重置上一次填的内容（失败重试保留，所以只在**新开**时清）
    dialog.querySelectorAll('input[name="rating"]').forEach((input) => {
      input.checked = false;
    });
    const comment = document.getElementById("feedback-comment");
    const contact = document.getElementById("feedback-contact");
    const shot = document.getElementById("feedback-screenshot");
    const reason = document.getElementById("feedback-reason");
    if (comment) comment.value = "";
    if (contact) contact.value = "";
    if (shot) shot.value = "";
    if (reason) reason.value = "";

    const scope = document.getElementById("feedback-scope");
    if (scope) {
      scope.textContent =
        target.kind === "reply"
          ? "提交会附带这次回复的结构性定位信息（阶段、方案/草稿版本、错误码等），不含完整对话、原始数据与报表数值。"
          : "提交会附带这次请求的结构性定位信息，不含完整对话、原始数据与报表数值。";
    }

    dialog.classList.remove("hidden");
    document.documentElement.classList.add("fb-lock");
    const first = dialog.querySelector('input[name="rating"]');
    if (first) first.focus();
  }

  function closeDialog() {
    if (!dialog) return;
    dialog.classList.add("hidden");
    document.documentElement.classList.remove("fb-lock");
    current = null;
  }

  // ---------- 提交 ----------

  async function submitFeedback() {
    if (!current) return;
    const checked = dialog.querySelector('input[name="rating"]:checked');
    const rating = checked ? checked.value : "";
    if (!rating) {
      showError("请先选择满意度。");
      return;
    }
    const fileInput = document.getElementById("feedback-screenshot");
    const file = fileInput && fileInput.files ? fileInput.files[0] : null;
    if (file && file.size > 5 * 1024 * 1024) {
      showError("截图超过 5 MB，请换一张小一点的。");
      return;
    }
    clearError();

    const form = new FormData();
    form.append("targetId", current.target.id);
    form.append("submissionId", current.submissionId);
    form.append("rating", rating);
    const reason = document.getElementById("feedback-reason");
    if (reason && reason.value) form.append("reason", reason.value);
    const comment = document.getElementById("feedback-comment");
    if (comment && comment.value.trim()) form.append("comment", comment.value.trim());
    const contact = document.getElementById("feedback-contact");
    if (contact && contact.value.trim()) form.append("contact", contact.value.trim());
    if (file) form.append("screenshot", file, file.name);

    const submitBtn = document.getElementById("feedback-submit");
    if (submitBtn) submitBtn.disabled = true;

    try {
      const response = await fetch(API_BASE + "/api/feedback", {
        method: "POST",
        body: form,
        credentials: "same-origin",
      });
      let payload = {};
      try {
        payload = await response.json();
      } catch (parseError) {
        payload = {};
      }
      if (!response.ok) {
        // 失败**保留表单**：用户改一下就能重试（同一个 submissionId）。
        showError(detailText(payload));
        return;
      }
      markSubmitted(current.target.id, payload.feedbackId || "");
      showReceipt(payload.feedbackId || "");
    } catch (error) {
      showError("提交失败，请重试。");
    } finally {
      if (submitBtn && !submitBtn.textContent.startsWith("已提交")) {
        submitBtn.disabled = false;
      }
    }
  }

  // ---------- 已反馈标记 ----------

  function markSubmitted(targetId, feedbackId) {
    const entry = triggers.get(targetId);
    if (!entry) return;
    entry.button.textContent = "已反馈";
    entry.button.disabled = true;
    entry.button.classList.add("fb-done");
    entry.button.title = feedbackId ? `反馈编号 ${feedbackId}` : "已反馈";
  }

  async function refreshSubmitted(targetIds) {
    const ids = (targetIds || []).filter((id) => typeof id === "string" && id);
    if (!ids.length) return;
    const known = ids.filter((id) => triggers.has(id));
    if (!known.length) return;
    try {
      const response = await fetch(API_BASE + "/api/feedback/status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ targetIds: known.slice(0, 200) }),
      });
      if (!response.ok) return;
      const payload = await response.json();
      const submitted = payload && payload.submitted ? payload.submitted : {};
      Object.keys(submitted).forEach((targetId) => {
        markSubmitted(targetId, submitted[targetId]);
      });
    } catch (error) {
      // 查不到就当没提交过：不猜、不假装。
    }
  }

  // ---------- 对外接口 ----------

  function attach(host, target) {
    if (!host || !target || !target.id) return;
    const existing = triggers.get(target.id);
    if (existing) {
      if (existing.host === host) return;
      if (existing.button && existing.button.parentNode) {
        existing.button.parentNode.removeChild(existing.button);
      }
    }
    const button = el("button", "fb-trigger feedback-trigger", "反馈");
    button.type = "button";
    button.dataset.feedbackTrigger = "1";
    button.title = "对这条回复提一条反馈";
    if (target.available === false) {
      button.textContent = "不可反馈";
      button.disabled = true;
    }
    button.addEventListener("click", () => openDialog(target, host));
    host.appendChild(button);
    triggers.set(target.id, { button, host });
  }

  function destroy() {
    if (dialog && dialog.parentNode) dialog.parentNode.removeChild(dialog);
    dialog = null;
    current = null;
    triggers.clear();
    document.documentElement.classList.remove("fb-lock");
  }

  window.WorkbenchFeedback = { attach, refreshSubmitted, destroy };
})();
