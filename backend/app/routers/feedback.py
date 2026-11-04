"""反馈接口：提交、状态查询、客户端事件、维护者管理。

路由表（20261104 实施 Prompt 冻结契约）：

| 计划接口 | 输入/输出与权限 |
| --- | --- |
| ``POST /api/feedback/client-events`` | 客户端失败/停止观察，返回目标 |
| ``POST /api/feedback`` | multipart 提交，返回反馈编号 |
| ``POST /api/feedback/status`` | 有界目标 ID 列表，返回自己的已提交标记 |
| ``POST /api/feedback/admin/login`` | 独立用户名密码登录（独立 cookie） |
| ``POST /api/feedback/admin/logout`` | 清除维护者会话 |
| ``GET  /api/feedback/admin/items`` | 鉴权：分页 + 筛选 |
| ``GET  /api/feedback/admin/items/{id}`` | 鉴权：安全详情 |
| ``PATCH /api/feedback/admin/items/{id}`` | 鉴权：只改处理状态与备注 |
| ``GET  /api/feedback/admin/items/{id}/attachment`` | 鉴权：私有截图 |
| ``GET  /api/feedback/admin/export`` | 鉴权：CSV 导出 |

**追加的第 11 个接口**（20261104 业务侧追加要求「收件箱要能删」）：

| 追加接口 | 输入/输出与权限 |
| --- | --- |
| ``DELETE /api/feedback/admin/items/{id}`` | 鉴权 + 同源保护：删掉这一条反馈与它的私有截图 |

上面那 10 个接口的语义**一个字没动**；删除是**纯追加**。

**Demo Middleware 继续保护分享站点**：这些接口都在 ``/api`` 下，
Basic Auth 依然生效。维护者登录用 JSON body + **独立 cookie**，
不改全局 ``Authorization``、不扩张 CORS。
"""
from __future__ import annotations

import csv
import io
import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from app.config import get_settings
from app.schemas.feedback import (
    AdminItemDeleteResponse,
    AdminItemDetail,
    AdminItemListResponse,
    AdminItemUpdate,
    AdminLoginRequest,
    AdminLoginResponse,
    AdminLogoutResponse,
    ClientEventRequest,
    FeedbackStatusResponse,
    FeedbackSubmissionResponse,
    FeedbackTarget,
)
from app.services import feedback_auth
from app.services import feedback_service
from app.services.feedback_service import FeedbackError
from app.services.session_service import session_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/feedback", tags=["feedback"])

settings = get_settings()


# ---------- 访客身份与节流 ----------

class _SubmitThrottle:
    """提交节流：按匿名访客限速（进程内）。"""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            stamps = [
                value for value in self._hits.get(key, []) if now - value < self._window
            ]
            if len(stamps) >= self._limit:
                self._hits[key] = stamps
                return False
            stamps.append(now)
            self._hits[key] = stamps
            return True


_throttle = _SubmitThrottle(
    int(getattr(settings, "feedback_submit_rate_limit", 20)),
    int(getattr(settings, "feedback_submit_rate_window_seconds", 600)),
)


def _visitor_id(request: Request, response: Response) -> str:
    """匿名浏览器身份：服务端签发的独立随机 cookie。

    **不是**管理权限 —— 客户端不能在表单里指定别人的 visitor。
    """
    existing = request.cookies.get(feedback_auth.VISITOR_COOKIE_NAME)
    if existing and existing.startswith("fbv_") and len(existing) >= 16:
        return existing
    visitor_id = feedback_auth.new_visitor_id()
    response.set_cookie(
        feedback_auth.VISITOR_COOKIE_NAME,
        visitor_id,
        max_age=int(getattr(settings, "feedback_visitor_ttl_days", 180)) * 86400,
        httponly=True,
        samesite="lax",
        secure=feedback_auth.cookie_secure_flag(request),
        path="/",
    )
    return visitor_id


def _require_same_origin(request: Request) -> None:
    if not feedback_auth.request_origin_ok(request):
        raise HTTPException(status_code=403, detail="同源写请求保护：不接受跨站提交。")


def _repository() -> Any:
    return session_service.repository


def _not_configured_detail(request: Request) -> str:
    """管理入口没配置时的说明。**带上当前实例**。

    同名页面可能开在好几个端口上（本地预览、既有 Demo 服务……），凭据只配在
    其中一个。不说清楚是哪个实例，用户只会看到一句「未配置」，不知道自己是
    走错了端口还是配置真没做。
    """
    host = request.headers.get("host") or "未知主机"
    return (
        f"收件箱管理入口未配置（当前实例 {host}）。"
        "本机预览请用配置好凭据的实例（127.0.0.1:8011）；"
        "正式环境需要设置 AI_FEEDBACK_ADMIN_USERNAME / "
        "AI_FEEDBACK_ADMIN_PASSWORD_HASH / AI_FEEDBACK_SESSION_SECRET。"
    )


def _require_admin(request: Request) -> str:
    """维护者鉴权。没配置 → 503；没登录 → 401。"""
    if not feedback_auth.admin_configured(settings):
        raise HTTPException(
            status_code=503, detail=_not_configured_detail(request)
        )
    username = feedback_auth.read_admin_username(request, settings)
    if not username:
        raise HTTPException(status_code=401, detail="请先登录收件箱。")
    return username


def _task_facts(task_id: str) -> dict[str, Any] | None:
    """服务端**自己核实**客户端事件关联的事实。

    读不到就返回 None —— 核实不了的部分只记成受限的客户端观察。
    """
    if not task_id:
        return None
    try:
        from app.services.draft_workspace import draft_workspace

        task = draft_workspace.load_task(task_id)
    except Exception:  # noqa: BLE001
        return None
    if task is None:
        return None
    checkpoint = dict(getattr(task, "checkpoint", None) or {})
    return {
        "sessionId": str(checkpoint.get("sessionId") or ""),
        "businessState": str(checkpoint.get("state") or ""),
        "draftId": str(getattr(task, "draftId", "") or ""),
        "messageId": str(checkpoint.get("messageId") or ""),
    }


# ---------- 客户端事件 ----------

@router.post("/client-events", response_model=FeedbackTarget)
def create_client_event(payload: ClientEventRequest, request: Request, response: Response) -> FeedbackTarget:
    """客户端失败 / 停止观察：签发一个评价目标。

    * **不能**写报表、不能停止任务 —— 这里只登记一条观察。
    * 服务端能核实的字段自己读；核实不了的只记受限观察。
    * 同一次事件重试（同一个 ``clientAttemptId``）幂等：返回同一个目标。
    """
    _require_same_origin(request)
    kind = payload.kind.value
    if kind == "reply":
        raise HTTPException(status_code=422, detail="客户端事件不能登记为回复目标。")
    target = feedback_service.register_client_event_target(
        _repository(),
        kind=kind,
        client_attempt_id=payload.clientAttemptId,
        session_id=payload.sessionId,
        message_id=payload.messageId,
        task_id=payload.taskId,
        observation=payload.observation,
        verifier=_task_facts,
    )
    _visitor_id(request, response)
    return target


# ---------- 提交 ----------

@router.post("", response_model=FeedbackSubmissionResponse)
async def submit_feedback(
    request: Request,
    response: Response,
    targetId: str = Form(...),
    submissionId: str = Form(...),
    rating: str = Form(...),
    reason: str | None = Form(default=None),
    comment: str | None = Form(default=None),
    contact: str | None = Form(default=None),
    screenshot: UploadFile | None = File(default=None),
) -> FeedbackSubmissionResponse:
    """提交一条反馈。

    * 满意度必选；意见 / 署名 / 联系方式 / 单张截图选填。
    * 首次成功 **201**；幂等重试 **200**（返回已有编号，不重复插入）。
    * 反馈弹窗**不调用模型、不改变业务状态** —— 这里只写反馈自己的表。
    """
    _require_same_origin(request)
    if not targetId or not submissionId:
        raise HTTPException(status_code=422, detail="缺少目标或提交标识。")
    if rating not in ("satisfied", "neutral", "dissatisfied"):
        raise HTTPException(status_code=422, detail="满意度取值不合法。")
    if len(submissionId) > 96:
        raise HTTPException(status_code=422, detail="提交标识过长。")

    visitor_id = _visitor_id(request, response)
    if not _throttle.allow(visitor_id):
        raise HTTPException(status_code=429, detail="提交太频繁，请稍后再试。")

    payload: bytes | None = None
    filename: str | None = None
    if screenshot is not None and screenshot.filename:
        limit = int(getattr(settings, "feedback_max_attachment_bytes", 5 * 1024 * 1024))
        payload = await _read_upload(screenshot, limit)
        filename = screenshot.filename

    try:
        feedback_id, created = feedback_service.submit_feedback(
            _repository(),
            target_id=targetId,
            visitor_id=visitor_id,
            submission_id=submissionId,
            rating=rating,
            reason=reason,
            comment=comment or "",
            contact=contact,
            screenshot_bytes=payload,
            screenshot_name=filename,
            settings=settings,
        )
    except FeedbackError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None

    if not created:
        response.status_code = 200
    else:
        response.status_code = 201
    return FeedbackSubmissionResponse(feedbackId=feedback_id, submitted=True)


async def _read_upload(upload: UploadFile, limit: int) -> bytes:
    """读上传内容。**超限立刻停**（不把整个大文件读进内存）。"""
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"截图超过 {limit // (1024 * 1024)} MB 上限。")
    return data


# ---------- 已提交标记 ----------

@router.post("/status", response_model=FeedbackStatusResponse)
def feedback_status(request: Request, response: Response, payload: dict[str, Any]) -> FeedbackStatusResponse:
    """当前匿名浏览器自己的已提交标记。

    * **只返回自己的**：不返回意见、署名、附件，也不返回别人的反馈。
    * 目标 ID 列表有界（最多 200 个）。
    """
    _require_same_origin(request)
    target_ids = payload.get("targetIds")
    if not isinstance(target_ids, list):
        raise HTTPException(status_code=422, detail="缺少目标列表。")
    if len(target_ids) > 200:
        raise HTTPException(status_code=422, detail="目标列表过长。")
    cleaned = [str(item) for item in target_ids if isinstance(item, str)][:200]
    visitor_id = _visitor_id(request, response)
    return FeedbackStatusResponse(
        submitted=feedback_service.submitted_map(
            _repository(), visitor_id=visitor_id, target_ids=cleaned
        )
    )


# ---------- 维护者 ----------

@router.post("/admin/login", response_model=AdminLoginResponse)
def admin_login(payload: AdminLoginRequest, request: Request, response: Response) -> AdminLoginResponse:
    """维护者登录：**独立** cookie，不动 Demo 的 Basic Auth。"""
    _require_same_origin(request)
    if not feedback_auth.admin_configured(settings):
        raise HTTPException(
            status_code=503, detail=_not_configured_detail(request)
        )
    client = request.client.host if request.client else "-"
    if feedback_auth.login_throttle.is_blocked(payload.username, client):
        raise HTTPException(status_code=429, detail="登录失败次数过多，请稍后再试。")
    username = (settings.feedback_admin_username or "").strip()
    ok_user = payload.username == username
    ok_pass = feedback_auth.verify_password(
        payload.password, settings.feedback_admin_password_hash or ""
    )
    if not (ok_user and ok_pass):
        feedback_auth.login_throttle.record_failure(payload.username, client)
        raise HTTPException(status_code=401, detail="用户名或密码不对。")
    feedback_auth.login_throttle.clear(payload.username, client)
    token = feedback_auth.sign_admin_token(
        username,
        settings.feedback_session_secret,
        ttl_seconds=int(getattr(settings, "feedback_admin_session_seconds", 8 * 3600)),
    )
    feedback_auth.set_admin_cookie(
        response, token, request=request,
        ttl_seconds=int(getattr(settings, "feedback_admin_session_seconds", 8 * 3600)),
    )
    response.headers["Cache-Control"] = "no-store"
    return AdminLoginResponse(ok=True, username=username)


@router.post("/admin/logout", response_model=AdminLogoutResponse)
def admin_logout(request: Request, response: Response) -> AdminLogoutResponse:
    """退出维护者会话。**不注销 Demo、不重置报表、不动聊天。**"""
    feedback_auth.clear_admin_cookie(response)
    response.headers["Cache-Control"] = "no-store"
    return AdminLogoutResponse(ok=True)


@router.get("/admin/session")
def admin_session(request: Request) -> dict[str, Any]:
    """当前维护者会话状态。**永远 200**，不返回任何反馈数据。

    收件箱页面靠它决定「显示登录框还是收件箱」。用一个**注定 401**
    的管理接口去探测不合适：那会在每次打开页面时留下一条报错，
    也会让静态自检把「未登录」误判成页面坏了。

    * 没配置维护者凭据 → ``configured=false``（管理入口安全关闭）；
    * 没登录 → ``authenticated=false``。
    """
    configured = feedback_auth.admin_configured(settings)
    username = (
        feedback_auth.read_admin_username(request, settings) if configured else None
    )
    return {
        "configured": configured,
        "authenticated": bool(username),
        "username": username or "",
        # 当前是**哪个实例**：同名页面可能开在好几个端口上（本地预览、
        # 既有 Demo 服务……），凭据只配在其中一个。页面要能自己说清楚，
        # 不然用户只会看到一句「未配置」，不知道是自己走错了端口。
        "host": request.headers.get("host") or "",
    }


@router.get("/admin/items", response_model=AdminItemListResponse)
def admin_items(
    request: Request,
    rating: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminItemListResponse:
    """列表：按新到旧，支持满意度 / 处理状态筛选。"""
    _require_admin(request)
    if rating and rating not in ("satisfied", "neutral", "dissatisfied"):
        raise HTTPException(status_code=422, detail="满意度筛选值不合法。")
    if status and status not in ("pending", "in_progress", "resolved"):
        raise HTTPException(status_code=422, detail="处理状态筛选值不合法。")
    items, total = feedback_service.admin_item_summaries(
        _repository(), rating=rating, status=status, limit=limit, offset=offset
    )
    return AdminItemListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/admin/items/{feedback_id}", response_model=AdminItemDetail)
def admin_item_detail(feedback_id: str, request: Request) -> AdminItemDetail:
    """详情：受限的原消息引用 + 白名单定位快照。

    **不返回**完整 response / snapshot / 原始 JSON / checkpoint。
    """
    _require_admin(request)
    detail = feedback_service.admin_item_detail(_repository(), feedback_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="反馈不存在。")
    return AdminItemDetail(**detail)


@router.patch("/admin/items/{feedback_id}", response_model=AdminItemDetail)
def admin_item_update(
    feedback_id: str, payload: AdminItemUpdate, request: Request
) -> AdminItemDetail:
    """只更新处理状态与备注 —— **不能**改评价内容。"""
    _require_admin(request)
    detail = feedback_service.admin_update_item(
        _repository(),
        feedback_id,
        status=payload.status.value if payload.status else None,
        note=payload.note,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="反馈不存在。")
    return AdminItemDetail(**detail)


@router.delete("/admin/items/{feedback_id}", response_model=AdminItemDeleteResponse)
def admin_item_delete(feedback_id: str, request: Request) -> AdminItemDeleteResponse:
    """删掉一条反馈（**连同它的私有截图**）。

    * **破坏性操作**：维护者登录 + 同源写请求保护，两道都要过；
    * 只删**这一条**反馈记录与它的附件行 —— **不动**目标、访客、会话、
      草稿、消息（原轮该在还在）；
    * 附件文件按**服务端生成的文件名精确删除**（不接任意路径、不扫目录）；
    * 找不到这一条 → **404**（重复删也是 404，不静默成功）。
    """
    _require_same_origin(request)
    username = _require_admin(request)
    result = feedback_service.admin_delete_item(
        _repository(), feedback_id, settings=settings
    )
    if result is None:
        raise HTTPException(status_code=404, detail="反馈不存在。")
    logger.info(
        "[feedback_router] 维护者 %s 删除了反馈 %s（附件文件已清理：%s）",
        username,
        feedback_id,
        "是" if result.get("attachment_removed") else "否",
    )
    return AdminItemDeleteResponse(
        ok=True,
        feedbackId=str(result.get("feedback_id") or feedback_id),
        attachmentRemoved=bool(result.get("attachment_removed")),
    )


@router.get("/admin/items/{feedback_id}/attachment")
def admin_item_attachment(feedback_id: str, request: Request) -> FileResponse:
    """私有截图。服务端固定文件映射，**不接任意磁盘路径**。"""
    _require_admin(request)
    store = feedback_service.store_for(_repository())
    row = store.admin_get(feedback_id)
    if row is None:
        raise HTTPException(status_code=404, detail="反馈不存在。")
    attachment_id = row.get("attachment_id")
    if not attachment_id:
        raise HTTPException(status_code=404, detail="这条反馈没有附件。")
    attachment = store.get_attachment(str(attachment_id))
    if attachment is None:
        raise HTTPException(status_code=404, detail="附件不存在或已过期。")
    path = feedback_service.attachment_path(settings, str(attachment.get("stored_name") or ""))
    if path is None:
        raise HTTPException(status_code=404, detail="附件不存在或已过期。")
    return FileResponse(
        path,
        media_type=str(attachment.get("content_type") or "application/octet-stream"),
        filename=f"{feedback_id}.png",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/admin/export")
def admin_export(
    request: Request,
    rating: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> PlainTextResponse:
    """CSV 导出：列表字段、意见与处理备注。

    * **不打包**私有截图与完整聊天；
    * 用户输入里的公式开头会被中和（防 CSV 公式注入）。
    """
    _require_admin(request)
    rows = feedback_service.admin_export_rows(
        _repository(), rating=rating, status=status
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "反馈编号",
            "提交时间",
            "类型",
            "满意度",
            "原因",
            "意见",
            "署名/联系方式",
            "处理状态",
            "处理备注",
            "目标编号",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                _csv_safe(row.get("feedback_id")),
                _csv_safe(row.get("created_at")),
                _csv_safe(row.get("target_kind")),
                _csv_safe(row.get("rating")),
                _csv_safe(row.get("reason")),
                _csv_safe(row.get("comment")),
                _csv_safe(row.get("contact")),
                _csv_safe(row.get("status")),
                _csv_safe(row.get("note")),
                _csv_safe(row.get("target_id")),
            ]
        )
    return PlainTextResponse(
        "﻿" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="feedback-export.csv"',
            "Cache-Control": "no-store",
        },
    )


def _csv_safe(value: Any) -> str:
    """CSV 单元格：**中和公式注入**。

    以 ``=`` / ``+`` / ``-`` / ``@`` / 制表符 / 回车开头的单元格，
    前面补一个单引号，Excel 就不会把它当公式执行。
    """
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text
