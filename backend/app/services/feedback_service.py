"""反馈业务服务：目标登记、提交、客户端事件、管理查询与到期清理。

本模块**不 import** ``session_service``（避免循环依赖）：连接与锁由
调用方注入。这样测试可以换仓库，路由层负责把当前仓库传进来。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import struct
import tempfile
import threading
from pathlib import Path
from typing import Any

from app.schemas.feedback import (
    FeedbackTarget,
    FeedbackTargetKind,
)
from app.storage.feedback_store import (
    FeedbackStore,
    dedupe_key,
    fingerprint,
    excerpt,
    now_iso,
)

logger = logging.getLogger(__name__)


#: 单张截图上限（字节）。
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

#: 意见上限 / 署名上限（字符）。
MAX_COMMENT_CHARS = 2000
MAX_CONTACT_CHARS = 100

#: 图片尺寸上限（像素），防止解码炸弹。
MAX_IMAGE_DIMENSION = 12_000

#: 允许的截图类型。
ALLOWED_IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")


class FeedbackError(Exception):
    """反馈业务错误。``status`` 直接作为 HTTP 状态码。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------- 存储句柄 ----------

_store_lock = threading.Lock()
_store: FeedbackStore | None = None
_store_repository: Any = None


def store_for(repository: Any) -> FeedbackStore:
    """拿当前仓库对应的反馈存储。

    **不在 import 时创建默认数据库** —— 连接与锁来自传入的仓库。
    仓库换了（测试换库、服务重启换库）就跟着重建。
    """
    global _store, _store_repository
    with _store_lock:
        if _store is None or _store_repository is not repository:
            store = FeedbackStore(
                connection=repository.conn,
                lock=repository.lock,
                retention_days=_retention_days(),
            )
            try:
                store.ensure_schema()
            except Exception as exc:  # noqa: BLE001 — 建表失败不该把服务搞挂
                logger.warning(
                    "[feedback] 建表失败 errorType=%s",
                    type(exc).__name__,
                )
            _store = store
            _store_repository = repository
        return _store


def init_feedback_storage(repository: Any) -> FeedbackStore:
    """启动时把反馈表建好（幂等迁移）。

    **必须在事务外调用**：建表是 DDL，会隐式结束事务。
    路由中间件在第一次 ``/api/`` 请求前调它 —— 那时没有事务。
    """
    store = store_for(repository)
    store.ensure_schema()
    return store


def _retention_days() -> int:
    try:
        from app.config import get_settings

        return int(get_settings().feedback_retention_days)
    except Exception:  # noqa: BLE001
        return 180


def _attachment_dir(settings: Any) -> Path:
    return Path(settings.feedback_attachment_dir).expanduser()


# ---------- 目标登记 ----------

def register_reply_target(
    connection: Any,
    repository: Any,
    *,
    session_id: str,
    user_text: str,
    response: Any,
    message_id: int | None,
    client_msg_id: str | None,
) -> FeedbackTarget | None:
    """在**调用方的事务内**登记一条「最终回复」的评价目标。

    * 目标字段来自**登记当时**：业务状态、设计阶段、方案 / 草稿身份与
      版本都在这一刻取，之后不改。
    * 同一次响应重放 → 同一个 ``dedupe_key`` → 复用同一个目标。
    * 登记失败**不影响这一轮对话**（反馈是辅助能力，不是主链路）。
    """
    try:
        store = store_for(repository)
        reply_text = str(getattr(response, "reply", "") or "")
        fp = fingerprint(reply_text)
        state = getattr(getattr(response, "state", None), "value", None) or ""
        design_phase = str(getattr(response, "designPhase", "") or "")
        debug = getattr(response, "debug", None) or {}
        error = getattr(response, "error", None)

        plan = getattr(getattr(response, "domainObjects", None), "reportPlan", None)
        plan_id = str((plan or {}).get("planId") or "") if isinstance(plan, dict) else ""
        plan_revision = (
            str((plan or {}).get("revision") or "") if isinstance(plan, dict) else ""
        )
        draft = getattr(
            getattr(response, "domainObjects", None), "reportDefinitionDraft", None
        )
        draft_meta = getattr(draft, "metadata", None) if draft is not None else None
        draft_id = str(getattr(draft_meta, "id", "") or "")
        draft_revision = getattr(draft_meta, "revision", None)

        locator = {
            "sessionId": session_id,
            "messageId": message_id,
            "clientMsgId": client_msg_id,
            "businessState": state,
            "designPhase": design_phase,
            "planId": plan_id,
            "planRevision": plan_revision,
            "draftId": draft_id,
            "draftRevision": draft_revision,
            "errorCode": str(getattr(error, "code", "") or ""),
            "diagnosticRef": str(debug.get("diagnosticRef") or ""),
            "backendVersion": _backend_version(),
        }
        missing = [
            name
            for name in ("planId", "planRevision", "draftId", "draftRevision")
            if not locator.get(name)
        ]

        key = dedupe_key(
            FeedbackTargetKind.REPLY.value,
            session_id or "",
            client_msg_id or "",
            fp,
        )
        row = store.register_target(
            connection,
            kind=FeedbackTargetKind.REPLY.value,
            dedupe_key_value=key,
            session_id=session_id or None,
            message_id=message_id,
            client_msg_id=client_msg_id,
            reply_fingerprint_value=fp,
            request_excerpt_value=user_text or "",
            reply_excerpt_value=reply_text,
            locator=locator,
            missing_items=missing,
            fact_source="server",
        )
        return _public_target(row)
    except Exception as exc:  # noqa: BLE001 — 登记失败不影响主链路
        logger.warning(
            "[feedback] 目标登记失败 sessionId=%s errorType=%s",
            session_id,
            type(exc).__name__,
        )
        return None


def register_client_event_target(
    repository: Any,
    *,
    kind: str,
    client_attempt_id: str,
    session_id: str | None = None,
    message_id: str | None = None,
    task_id: str | None = None,
    observation: str | None = None,
    verifier: Any = None,
) -> FeedbackTarget:
    """登记一个**客户端事件**目标（失败 / 停止）。

    服务端能核实的字段**自己读**（会话行、草稿任务）；核实不了的只记成
    受限的客户端观察 —— 不接收客户端声称的任何「后台事实」。
    """
    store = store_for(repository)
    resolved_session = session_id or ""
    resolved_state = ""
    resolved_draft = ""
    fact_source = "client_observation"

    if verifier is not None and task_id:
        try:
            facts = verifier(task_id)
        except Exception:  # noqa: BLE001
            facts = None
        if facts:
            resolved_session = str(facts.get("sessionId") or resolved_session)
            resolved_state = str(facts.get("businessState") or "")
            resolved_draft = str(facts.get("draftId") or "")
            fact_source = "server_verified"

    locator = {
        "sessionId": resolved_session,
        "clientMsgId": message_id or "",
        "businessState": resolved_state,
        "draftId": resolved_draft,
        "backendVersion": _backend_version(),
        "factSource": fact_source,
    }
    missing = [
        name
        for name in ("businessState", "draftId", "designPhase")
        if not locator.get(name)
    ]
    key = dedupe_key(
        kind,
        resolved_session,
        client_attempt_id or "",
        task_id or "",
    )
    with store.lock:
        row = store.register_target(
            store.connection,
            kind=kind,
            dedupe_key_value=key,
            session_id=resolved_session or None,
            client_msg_id=message_id or task_id or None,
            locator=locator,
            missing_items=missing,
            fact_source=fact_source,
        )
        store.connection.commit()
    return _public_target(row)


def fill_target_message_ref(
    repository: Any,
    connection: Any,
    target_id: str,
    message_id: int | None,
) -> None:
    """补上原消息引用（消息 id）。**只写不 commit** —— 跟着调用方的事务走。

    登记时助手消息还没落库，消息 id 是登记过程的收尾；
    定位信息、缺失项、事实来源一概不动。
    """
    if not target_id or message_id is None:
        return
    try:
        store = store_for(repository)
        store.fill_target_message_ref(connection, target_id, int(message_id))
    except Exception as exc:  # noqa: BLE001 — 补引用失败不影响主链路
        logger.warning(
            "[feedback] 补原消息引用失败 targetId=%s errorType=%s",
            target_id,
            type(exc).__name__,
        )


def _public_target(row: dict[str, Any]) -> FeedbackTarget:
    """目标 → 公开协议。**只有四个字段进协议。**"""
    return FeedbackTarget(
        id=str(row.get("target_id") or ""),
        kind=str(row.get("kind") or ""),
        createdAt=str(row.get("created_at") or ""),
        available=True,
    )


def _backend_version() -> str:
    try:
        from app.main import app

        return str(getattr(app, "version", "") or "")
    except Exception:  # noqa: BLE001
        return ""


# ---------- 提交 ----------

def submit_feedback(
    repository: Any,
    *,
    target_id: str,
    visitor_id: str,
    submission_id: str,
    rating: str,
    reason: str | None = None,
    comment: str = "",
    contact: str | None = None,
    screenshot_bytes: bytes | None = None,
    screenshot_name: str | None = None,
    settings: Any = None,
) -> tuple[str, bool]:
    """提交一条反馈。返回 ``(反馈编号, 是否本次新建)``。

    * 目标不存在 → 404；目标不可用 → 409。
    * 同浏览器同目标 → 幂等：返回已有编号，**不重复插入**。
    * 意见 / 署名超长、文件超限、类型不对 → 明确错误码。
    """
    store = store_for(repository)
    target = store.get_target(target_id)
    if target is None:
        raise FeedbackError(404, "这个评价目标不存在。")
    if str(target.get("expires_at") or "") <= now_iso():
        raise FeedbackError(409, "这个评价目标已经过期，不能再提交了。")

    if not rating:
        raise FeedbackError(422, "请先选择满意度。")
    comment = (comment or "").strip()
    if len(comment) > MAX_COMMENT_CHARS:
        raise FeedbackError(422, f"意见最多 {MAX_COMMENT_CHARS} 字。")
    contact = (contact or "").strip() or None
    if contact and len(contact) > MAX_CONTACT_CHARS:
        raise FeedbackError(422, f"署名或联系方式最多 {MAX_CONTACT_CHARS} 字。")
    reason = (reason or "").strip() or None

    attachment_id: str | None = None
    if screenshot_bytes:
        attachment_id = _store_attachment(
            repository,
            screenshot_bytes,
            filename=screenshot_name,
            settings=settings,
        )

    store.ensure_visitor(visitor_id)
    row, created = store.insert_feedback(
        target_id=target_id,
        visitor_id=visitor_id,
        submission_id=submission_id,
        rating=rating,
        reason=reason,
        comment=comment,
        contact=contact,
        attachment_id=attachment_id,
    )
    return str(row.get("feedback_id") or ""), created


def submitted_map(
    repository: Any, *, visitor_id: str, target_ids: list[str]
) -> dict[str, str]:
    """当前访客在给定目标上的已提交标记。"""
    store = store_for(repository)
    return store.submitted_map(visitor_id=visitor_id, target_ids=target_ids)


# ---------- 附件 ----------

def _store_attachment(
    repository: Any,
    data: bytes,
    *,
    filename: str | None,
    settings: Any,
) -> str:
    """校验并私有保存一张截图。

    * **实际解码校验类型与尺寸**（不只看扩展名 / MIME 声明）。
    * 保存**随机安全文件名**，落在私有目录（不在公开静态目录里）。
    * 先写临时文件再改名：写失败不留半截文件。
    """
    limit = MAX_ATTACHMENT_BYTES
    if settings is not None:
        limit = int(getattr(settings, "feedback_max_attachment_bytes", limit))
    if len(data) > limit:
        raise FeedbackError(413, f"截图超过 {limit // (1024 * 1024)} MB 上限。")
    if not data:
        raise FeedbackError(422, "截图内容为空。")

    content_type, width, height = _probe_image(data)
    if content_type is None:
        raise FeedbackError(422, "只支持 PNG / JPEG / WebP 截图。")
    if width and height and (width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION):
        raise FeedbackError(422, "截图尺寸过大。")

    store = store_for(repository)
    directory = _attachment_dir(settings) if settings is not None else Path(
        os.getenv("AI_FEEDBACK_ATTACHMENT_DIR", "./data/feedback_attachments")
    )
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:  # pragma: no cover — 某些文件系统不支持
        pass

    suffix = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }.get(content_type, ".bin")
    stored_name = f"{secrets.token_hex(16)}{suffix}"
    final_path = directory / stored_name
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            dir=str(directory), prefix=".upload-", suffix=".tmp", delete=False
        ) as handle:
            tmp_path = handle.name
            handle.write(data)
        os.replace(tmp_path, final_path)
        tmp_path = ""
    except OSError as exc:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise FeedbackError(500, "截图保存失败，请重试。") from exc

    return store.create_attachment(
        stored_name=stored_name,
        content_type=content_type,
        byte_size=len(data),
    )


def _probe_image(data: bytes) -> tuple[str | None, int, int]:
    """按**实际字节**判定图片类型与尺寸。

    返回 ``(content_type, width, height)``；不是允许的类型 → ``(None, 0, 0)``。
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        width, height = _probe_png(data)
        return "image/png", width, height
    if data.startswith(b"\xff\xd8\xff"):
        width, height = _probe_jpeg(data)
        return "image/jpeg", width, height
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        width, height = _probe_webp(data)
        return "image/webp", width, height
    return None, 0, 0


def _probe_png(data: bytes) -> tuple[int, int]:
    try:
        if len(data) < 24 or data[12:16] != b"IHDR":
            return 0, 0
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    except (struct.error, ValueError):
        return 0, 0


def _probe_jpeg(data: bytes) -> tuple[int, int]:
    try:
        index = 2
        total = len(data)
        while index + 9 < total:
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            if marker == 0xD9:
                break
            length = struct.unpack(">H", data[index + 2 : index + 4])[0]
            if marker in (
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            ):
                height, width = struct.unpack(">HH", data[index + 5 : index + 9])
                return int(width), int(height)
            index += 2 + length
    except (struct.error, ValueError, IndexError):
        return 0, 0
    return 0, 0


def _probe_webp(data: bytes) -> tuple[int, int]:
    try:
        chunk = data[12:16]
        if chunk == b"VP8 ":
            width, height = struct.unpack("<HH", data[26:30])
            return int(width & 0x3FFF), int(height & 0x3FFF)
        if chunk == b"VP8L":
            bits = struct.unpack("<I", data[21:25])[0]
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return width, height
        if chunk == b"VP8X":
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return width, height
    except (struct.error, ValueError, IndexError):
        return 0, 0
    return 0, 0


def attachment_path(settings: Any, stored_name: str) -> Path | None:
    """附件真实路径。**只接受服务端生成的安全文件名。**

    目录之外一律拒绝（不接任意磁盘路径）。
    """
    if not stored_name or "/" in stored_name or "\\" in stored_name:
        return None
    if stored_name.startswith(".") or not stored_name.replace(".", "").replace(
        "_", ""
    ).replace("-", "").isalnum():
        return None
    directory = _attachment_dir(settings).resolve()
    candidate = (directory / stored_name).resolve()
    try:
        candidate.relative_to(directory)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


# ---------- 管理端视图 ----------

def _reference_state(
    store: FeedbackStore, target: dict[str, Any] | None
) -> str:
    """原消息引用状态：``ok`` / ``overwritten`` / ``missing`` / ``expired``。

    * 目标过期 → ``expired``；
    * 原消息不在了 → ``missing``；
    * 原消息**内容指纹对不上**（被同键覆盖）→ ``overwritten``。
      这时**不读新正文充当旧回复**。
    """
    if not target:
        return "missing"
    if str(target.get("expires_at") or "") <= now_iso():
        return "expired"
    message_id = target.get("message_id")
    stored_fp = str(target.get("reply_fingerprint") or "")
    if message_id:
        with store.lock:
            row = store.connection.execute(
                "SELECT content FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        if row is None:
            return "missing"
        current_fp = fingerprint(str(row["content"] or ""))
        if stored_fp and current_fp != stored_fp:
            return "overwritten"
    return "ok"


def admin_item_detail(
    repository: Any, feedback_id: str
) -> dict[str, Any] | None:
    """详情：**受限引用 + 白名单快照**，不返回完整 response / snapshot。"""
    store = store_for(repository)
    row = store.admin_get(feedback_id)
    if row is None:
        return None
    target = None
    if row.get("target_id"):
        target = store.get_target(str(row["target_id"]))
    locator = {}
    missing: list[str] = []
    if target is not None:
        try:
            locator = json.loads(str(target.get("locator_json") or "{}"))
        except json.JSONDecodeError:
            locator = {}
        try:
            missing = json.loads(str(target.get("missing_items") or "[]"))
        except json.JSONDecodeError:
            missing = []
        # 原消息 id 是登记后补的（消息还没落库时拿不到）→ 这里补回快照。
        if not locator.get("messageId") and target.get("message_id"):
            locator["messageId"] = target.get("message_id")
        if not locator.get("clientMsgId") and target.get("client_msg_id"):
            locator["clientMsgId"] = target.get("client_msg_id")
    return {
        "feedbackId": str(row.get("feedback_id") or ""),
        "targetId": str(row.get("target_id") or ""),
        "kind": str(row.get("target_kind") or row.get("kind") or ""),
        "rating": str(row.get("rating") or ""),
        "reason": row.get("reason"),
        "comment": str(row.get("comment") or ""),
        "contact": row.get("contact"),
        "status": str(row.get("status") or "pending"),
        "note": str(row.get("note") or ""),
        "createdAt": str(row.get("created_at") or ""),
        "updatedAt": str(row.get("updated_at") or ""),
        "hasAttachment": bool(row.get("attachment_id")),
        "requestExcerpt": str(row.get("request_excerpt") or ""),
        "replyExcerpt": str(row.get("reply_excerpt") or ""),
        "replyFingerprint": str(row.get("reply_fingerprint") or ""),
        "referenceState": _reference_state(store, target),
        "locator": locator,
        "missingItems": missing,
        "factSource": str(row.get("fact_source") or "server"),
    }


def admin_item_summaries(
    repository: Any,
    *,
    rating: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    store = store_for(repository)
    rows, total = store.admin_list(
        rating=rating, status=status, limit=limit, offset=offset
    )
    items = []
    for row in rows:
        items.append(
            {
                "feedbackId": str(row.get("feedback_id") or ""),
                "targetId": str(row.get("target_id") or ""),
                "kind": str(row.get("target_kind") or ""),
                "rating": str(row.get("rating") or ""),
                "reason": row.get("reason"),
                "commentExcerpt": excerpt(str(row.get("comment") or ""), 80),
                "contact": row.get("contact"),
                "hasAttachment": bool(row.get("attachment_id")),
                "status": str(row.get("status") or "pending"),
                "note": str(row.get("note") or ""),
                "createdAt": str(row.get("created_at") or ""),
                "updatedAt": str(row.get("updated_at") or ""),
                "businessState": row.get("business_state"),
            }
        )
    return items, total


def admin_update_item(
    repository: Any, feedback_id: str, *, status: str | None, note: str | None
) -> dict[str, Any] | None:
    store = store_for(repository)
    store.admin_update(feedback_id, status=status, note=note)
    return admin_item_detail(repository, feedback_id)


def admin_export_rows(
    repository: Any, *, rating: str | None = None, status: str | None = None
) -> list[dict[str, Any]]:
    store = store_for(repository)
    return store.admin_export_rows(rating=rating, status=status)


# ---------- 删除 ----------

def admin_delete_item(
    repository: Any, feedback_id: str, *, settings: Any = None
) -> dict[str, Any] | None:
    """删掉**一条**反馈，连同它的私有截图文件。

    边界（与到期清理同一套纪律）：

    * 只删**反馈 Module 自己的**这一条记录与它的附件行；
    * 附件文件按**服务端生成的文件名精确删除**（复用
      :func:`attachment_path`：不接任意路径、不扫描目录）；
    * **不动**目标、访客、会话、草稿、消息 —— 删一条评价不影响那一轮
      的其它状态；用户那边的「已反馈」标记会随之消失（记录没了）；
    * 找不到这一条 → 返回 ``None``（路由层据此回 404）。
    """
    store = store_for(repository)
    row = store.admin_delete(feedback_id)
    if row is None:
        return None
    attachment_removed = False
    stored_name = str(row.get("stored_name") or "")
    if stored_name and settings is not None:
        path = attachment_path(settings, stored_name)
        if path is not None:
            try:
                path.unlink()
                attachment_removed = True
            except OSError:  # pragma: no cover —— 权限 / 并发删除等
                logger.warning(
                    "[feedback_service] 附件删除失败（记录已删）：%s", stored_name
                )
    return {
        "feedback_id": str(row.get("feedback_id") or feedback_id),
        "attachment_removed": attachment_removed,
    }


# ---------- 到期清理 ----------

def cleanup_expired(repository: Any, *, settings: Any = None) -> dict[str, Any]:
    """清掉到期的反馈与它们引用的附件。

    * 只清**反馈 Module 自己的**到期记录；
    * **不清**用户会话、草稿、方案、消息；
    * 附件按服务端生成的文件名精确删除（不扫描整个数据目录）。
    """
    store = store_for(repository)
    result = store.cleanup_expired()
    directory = _attachment_dir(settings) if settings is not None else None
    removed = 0
    if directory is not None:
        for name in result.get("attachment_names") or []:
            path = directory / name
            try:
                if path.is_file() and path.resolve().parent == directory.resolve():
                    path.unlink()
                    removed += 1
            except OSError:  # pragma: no cover
                continue
    # 写失败留下的临时文件：只清本目录里自己写的那种。
    if directory is not None and directory.is_dir():
        for leftover in directory.glob(".upload-*.tmp"):
            try:
                leftover.unlink()
                removed += 1
            except OSError:  # pragma: no cover
                continue
    result["attachment_files_removed"] = removed
    result.pop("attachment_names", None)
    return result
