"""反馈存储：独立表 + 独立索引，**复用会话库的连接与锁**。

边界（20261104 确认稿 / 实施 Prompt）：

* 反馈数据落**同一个 SQLite 文件**（复用会话库），但表是**自己的**；
  既有表一个字节都不改，既有行一条都不动。
* 构造 ``FeedbackStore`` 时**不自己建默认数据库**：连接与锁由调用方
  （当前 ``session_service.repository``）注入，测试可以替换仓库。
* ``register_target(connection, ...)`` **只写不 commit** —— 它跑在调用方
  自己的事务里，由调用方决定何时落盘。
* 建表是**幂等迁移**：重跑一次脚本即可补齐，既有行保留。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, Iterable

logger = logging.getLogger(__name__)


#: 反馈保留期限（天）。到期清理只清反馈 Module 自己的记录。
DEFAULT_RETENTION_DAYS = 180

#: 白名单：允许进入定位快照的结构性字段。
#:
#: **只有这些能进快照** —— 聊天正文、原始 JSON、checkpoint、模型原文、
#: 图表业务数值一律不进（20261104 确认稿「怎么定位问题」）。
LOCATOR_WHITELIST = (
    "sessionId",
    "messageId",
    "messageSeq",
    "clientMsgId",
    "turnIndex",
    "attempt",
    "businessState",
    "designPhase",
    "planId",
    "planRevision",
    "draftId",
    "draftRevision",
    "errorCode",
    "diagnosticRef",
    "frontendVersion",
    "backendVersion",
    "factSource",
)

#: 受限引用里保留的正文长度（字符）。超出截断，不复制整段聊天。
EXCERPT_MAX_CHARS = 200


_SCHEMA = """
-- 评价目标：一轮最终回复 / 一次客户端观察。
CREATE TABLE IF NOT EXISTS feedback_targets (
    target_id         TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,          -- reply | client_failure | client_stopped
    dedupe_key        TEXT NOT NULL,
    session_id        TEXT,
    message_id        INTEGER,
    client_msg_id     TEXT,
    reply_fingerprint TEXT,
    request_excerpt   TEXT NOT NULL DEFAULT '',
    reply_excerpt     TEXT NOT NULL DEFAULT '',
    business_state    TEXT,
    design_phase      TEXT,
    locator_json      TEXT NOT NULL DEFAULT '{}',
    missing_items     TEXT NOT NULL DEFAULT '[]',
    fact_source       TEXT NOT NULL DEFAULT 'server',
    created_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL,
    UNIQUE (dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_feedback_targets_session ON feedback_targets(session_id);
CREATE INDEX IF NOT EXISTS idx_feedback_targets_created ON feedback_targets(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_targets_expires ON feedback_targets(expires_at);

-- 匿名浏览器身份：只用来防重复，**不是**管理权限。
CREATE TABLE IF NOT EXISTS feedback_visitors (
    visitor_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- 一条反馈（用户提交的评价）。
CREATE TABLE IF NOT EXISTS feedback_items (
    feedback_id   TEXT PRIMARY KEY,
    target_id     TEXT NOT NULL,
    visitor_id    TEXT NOT NULL,
    submission_id TEXT NOT NULL,
    rating        TEXT NOT NULL,             -- satisfied | neutral | dissatisfied
    reason        TEXT,
    comment       TEXT NOT NULL DEFAULT '',
    contact       TEXT,
    attachment_id TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending | in_progress | resolved
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    UNIQUE (target_id, visitor_id),
    UNIQUE (submission_id, visitor_id)
);

CREATE INDEX IF NOT EXISTS idx_feedback_items_created ON feedback_items(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_items_status ON feedback_items(status);
CREATE INDEX IF NOT EXISTS idx_feedback_items_rating ON feedback_items(rating);
CREATE INDEX IF NOT EXISTS idx_feedback_items_expires ON feedback_items(expires_at);
CREATE INDEX IF NOT EXISTS idx_feedback_items_target ON feedback_items(target_id);

-- 私有截图：只存**服务端生成的安全文件名**，不存原始路径。
CREATE TABLE IF NOT EXISTS feedback_attachments (
    attachment_id TEXT PRIMARY KEY,
    stored_name   TEXT NOT NULL,
    content_type  TEXT NOT NULL,
    byte_size     INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def iso_after(days: int, *, base: datetime | None = None) -> str:
    moment = base or datetime.now()
    return (moment + timedelta(days=days)).isoformat(timespec="seconds")


def fingerprint(text: str) -> str:
    """正文指纹：用来核对「这条原消息后来有没有被覆盖」。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def dedupe_key(*parts: str) -> str:
    raw = "".join(p or "" for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def excerpt(text: str, limit: int = EXCERPT_MAX_CHARS) -> str:
    """受限引用：截断正文，不复制整段聊天。"""
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def whitelist_locator(raw: dict[str, Any] | None) -> dict[str, Any]:
    """把定位信息裁成白名单快照。**白名单之外的键一律丢掉。**"""
    source = raw if isinstance(raw, dict) else {}
    return {key: source[key] for key in LOCATOR_WHITELIST if key in source}


class FeedbackStore:
    """反馈存储。连接与锁由外部注入（复用会话库）。"""

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        lock: Any,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self._conn = connection
        self._lock = lock or threading.RLock()
        self._retention_days = int(retention_days)
        self._schema_ready = False

    # ---------- 迁移 ----------

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @property
    def lock(self) -> Any:
        return self._lock

    def ensure_schema(self) -> None:
        """幂等建表。

        **不在别人的事务里跑**：DDL 会隐式结束事务（``executescript``
        会先 commit）。所以事务进行中时直接跳过，等事务外再补。
        """
        with self._lock:
            if self._schema_ready:
                return
            if self._conn.in_transaction:
                logger.info("[feedback_store] 事务进行中，建表延后")
                return
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._schema_ready = True
            logger.info("[feedback_store] 反馈表已就绪")

    # ---------- 目标登记 ----------

    def register_target(
        self,
        connection: sqlite3.Connection,
        *,
        kind: str,
        dedupe_key_value: str,
        session_id: str | None = None,
        message_id: int | None = None,
        client_msg_id: str | None = None,
        reply_fingerprint_value: str | None = None,
        request_excerpt_value: str = "",
        reply_excerpt_value: str = "",
        locator: dict[str, Any] | None = None,
        missing_items: Iterable[str] | None = None,
        fact_source: str = "server",
        created_at: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """登记一个评价目标。**只写不 commit。**

        幂等：同一个 ``dedupe_key_value`` 重复登记 → 返回**原来那个**目标，
        **不覆盖**内容（目标内容不可被后续编辑覆盖）。

        返回目标行（dict）。
        """
        created = created_at or now_iso()
        expiry = expires_at or iso_after(self._retention_days)
        snapshot = whitelist_locator(locator)
        missing = [str(item) for item in (missing_items or [])]
        with self._lock:
            connection.execute(
                """
                INSERT INTO feedback_targets (
                    target_id, kind, dedupe_key, session_id, message_id, client_msg_id,
                    reply_fingerprint, request_excerpt, reply_excerpt, business_state,
                    design_phase, locator_json, missing_items, fact_source,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (dedupe_key) DO NOTHING
                """,
                (
                    _new_target_id(),
                    kind,
                    dedupe_key_value,
                    session_id,
                    message_id,
                    client_msg_id,
                    reply_fingerprint_value,
                    excerpt(request_excerpt_value),
                    excerpt(reply_excerpt_value),
                    snapshot.get("businessState"),
                    snapshot.get("designPhase"),
                    json.dumps(snapshot, ensure_ascii=False),
                    json.dumps(missing, ensure_ascii=False),
                    fact_source,
                    created,
                    expiry,
                ),
            )
            row = connection.execute(
                "SELECT * FROM feedback_targets WHERE dedupe_key = ?",
                (dedupe_key_value,),
            ).fetchone()
        if row is None:  # pragma: no cover — 理论上不会发生
            raise RuntimeError("反馈目标登记失败")
        return dict(row)

    def get_target(self, target_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM feedback_targets WHERE target_id = ?", (target_id,)
            ).fetchone()
        return dict(row) if row else None

    def fill_target_message_ref(
        self, connection: sqlite3.Connection, target_id: str, message_id: int
    ) -> None:
        """补上**原消息引用**（消息 id）。**只写不 commit。**

        这不是「覆盖目标内容」：登记时消息还没写，消息 id 是登记过程的
        收尾。定位信息、缺失项、事实来源一概不动。
        """
        if not target_id or message_id is None:
            return
        with self._lock:
            connection.execute(
                "UPDATE feedback_targets SET message_id = ? WHERE target_id = ?",
                (int(message_id), target_id),
            )

    # ---------- 访客 ----------

    def ensure_visitor(self, visitor_id: str, *, expires_at: str | None = None) -> None:
        expiry = expires_at or iso_after(self._retention_days)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO feedback_visitors (visitor_id, created_at, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT (visitor_id) DO UPDATE SET expires_at = excluded.expires_at
                """,
                (visitor_id, now_iso(), expiry),
            )
            self._conn.commit()

    # ---------- 提交 ----------

    def insert_feedback(
        self,
        *,
        target_id: str,
        visitor_id: str,
        submission_id: str,
        rating: str,
        reason: str | None = None,
        comment: str = "",
        contact: str | None = None,
        attachment_id: str | None = None,
        created_at: str | None = None,
        expires_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """写入一条反馈。返回 ``(反馈行, 是否本次新建)``。

        幂等：同浏览器同目标、或同浏览器同 submissionId 重试 →
        返回**已有**反馈编号，**不重复插入**。
        """
        created = created_at or now_iso()
        expiry = expires_at or iso_after(self._retention_days)
        feedback_id = _new_feedback_id()
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO feedback_items (
                        feedback_id, target_id, visitor_id, submission_id, rating,
                        reason, comment, contact, attachment_id, status, note,
                        created_at, updated_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, ?, ?)
                    """,
                    (
                        feedback_id,
                        target_id,
                        visitor_id,
                        submission_id,
                        rating,
                        reason,
                        comment,
                        contact,
                        attachment_id,
                        created,
                        created,
                        expiry,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                # 撞唯一约束：同目标或同 submissionId 已有反馈 → 返回它。
                existing = self._find_existing_feedback(
                    target_id=target_id,
                    visitor_id=visitor_id,
                    submission_id=submission_id,
                )
                if existing is None:
                    raise
                return existing, False
            row = self._conn.execute(
                "SELECT * FROM feedback_items WHERE feedback_id = ?", (feedback_id,)
            ).fetchone()
        return dict(row), True

    def _find_existing_feedback(
        self, *, target_id: str, visitor_id: str, submission_id: str
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT * FROM feedback_items
             WHERE visitor_id = ? AND (target_id = ? OR submission_id = ?)
             ORDER BY created_at ASC LIMIT 1
            """,
            (visitor_id, target_id, submission_id),
        ).fetchone()
        return dict(row) if row else None

    def submitted_map(
        self, *, visitor_id: str, target_ids: list[str]
    ) -> dict[str, str]:
        """当前访客在给定目标上的已提交标记（``targetId -> feedbackId``）。"""
        if not target_ids:
            return {}
        placeholders = ",".join("?" for _ in target_ids)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT target_id, feedback_id FROM feedback_items
                 WHERE visitor_id = ? AND target_id IN ({placeholders})
                """,
                (visitor_id, *target_ids),
            ).fetchall()
        return {row["target_id"]: row["feedback_id"] for row in rows}

    # ---------- 附件 ----------

    def create_attachment(
        self, *, stored_name: str, content_type: str, byte_size: int
    ) -> str:
        attachment_id = _new_attachment_id()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO feedback_attachments
                    (attachment_id, stored_name, content_type, byte_size, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (attachment_id, stored_name, content_type, byte_size, now_iso()),
            )
            self._conn.commit()
        return attachment_id

    def get_attachment(self, attachment_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM feedback_attachments WHERE attachment_id = ?",
                (attachment_id,),
            ).fetchone()
        return dict(row) if row else None

    # ---------- 管理端 ----------

    def admin_list(
        self,
        *,
        rating: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if rating:
            clauses.append("i.rating = ?")
            params.append(rating)
        if status:
            clauses.append("i.status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM feedback_items i {where}", params
            ).fetchone()[0]
            rows = self._conn.execute(
                f"""
                SELECT i.*, t.business_state, t.kind AS target_kind,
                       t.request_excerpt, t.reply_excerpt, t.reply_fingerprint,
                       t.locator_json, t.missing_items, t.fact_source,
                       t.message_id, t.client_msg_id, t.session_id,
                       (SELECT COUNT(*) FROM feedback_attachments a
                         WHERE a.attachment_id = i.attachment_id) AS attachment_rows
                  FROM feedback_items i
                  LEFT JOIN feedback_targets t ON t.target_id = i.target_id
                  {where}
                 ORDER BY i.created_at DESC, i.rowid DESC
                 LIMIT ? OFFSET ?
                """,
                (*params, int(limit), int(offset)),
            ).fetchall()
        return [dict(row) for row in rows], int(total)

    def admin_get(self, feedback_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT i.*, t.business_state, t.kind AS target_kind,
                       t.request_excerpt, t.reply_excerpt, t.reply_fingerprint,
                       t.locator_json, t.missing_items, t.fact_source,
                       t.message_id, t.client_msg_id, t.session_id,
                       t.created_at AS target_created_at
                  FROM feedback_items i
                  LEFT JOIN feedback_targets t ON t.target_id = i.target_id
                 WHERE i.feedback_id = ?
                """,
                (feedback_id,),
            ).fetchone()
        return dict(row) if row else None

    def admin_update(
        self, feedback_id: str, *, status: str | None = None, note: str | None = None
    ) -> dict[str, Any] | None:
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if note is not None:
            sets.append("note = ?")
            params.append(note)
        if not sets:
            return self.admin_get(feedback_id)
        sets.append("updated_at = ?")
        params.append(now_iso())
        params.append(feedback_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE feedback_items SET {', '.join(sets)} WHERE feedback_id = ?",
                params,
            )
            self._conn.commit()
        return self.admin_get(feedback_id)

    def admin_export_rows(
        self, *, rating: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        rows, _ = self.admin_list(rating=rating, status=status, limit=100000, offset=0)
        return rows

    # ---------- 删除 ----------

    def admin_delete(self, feedback_id: str) -> dict[str, Any] | None:
        """删掉**一条**反馈记录与它引用的附件行。

        **只动反馈 Module 自己的表**：``feedback_items`` 与
        ``feedback_attachments``。目标（``feedback_targets``）与访客
        （``feedback_visitors``）**不动** —— 目标绑在**原轮**上，
        删一条评价不该把那一轮的评价入口也带走。

        返回被删掉那一行的关键信息（附件 id / 服务端生成的文件名），
        供调用方按文件名精确删文件；找不到这一条就返回 ``None``。
        """
        with self._lock:
            row = self._conn.execute(
                """
                SELECT i.feedback_id, i.attachment_id, a.stored_name
                  FROM feedback_items i
                  LEFT JOIN feedback_attachments a ON a.attachment_id = i.attachment_id
                 WHERE i.feedback_id = ?
                """,
                (feedback_id,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "DELETE FROM feedback_items WHERE feedback_id = ?", (feedback_id,)
            )
            if row["attachment_id"]:
                self._conn.execute(
                    "DELETE FROM feedback_attachments WHERE attachment_id = ?",
                    (row["attachment_id"],),
                )
            self._conn.commit()
        return dict(row)

    # ---------- 到期清理 ----------

    def cleanup_expired(self, *, now: str | None = None) -> dict[str, Any]:
        """清掉**到期**的反馈记录与它们引用的附件。

        只清反馈 Module 自己的表：**不动**用户会话、草稿、方案、消息，
        也不碰任何未知路径。附件由调用方按返回的 ``attachment_names`` 删除。
        """
        moment = now or now_iso()
        with self._lock:
            expired_targets = [
                row["target_id"]
                for row in self._conn.execute(
                    "SELECT target_id FROM feedback_targets WHERE expires_at <= ?",
                    (moment,),
                ).fetchall()
            ]
            rows = self._conn.execute(
                """
                SELECT i.feedback_id, i.attachment_id, i.target_id, a.stored_name
                  FROM feedback_items i
                  LEFT JOIN feedback_attachments a ON a.attachment_id = i.attachment_id
                 WHERE i.expires_at <= ?
                    OR i.target_id IN (
                        SELECT target_id FROM feedback_targets WHERE expires_at <= ?
                    )
                """,
                (moment, moment),
            ).fetchall()
            attachment_names = [
                row["stored_name"] for row in rows if row["stored_name"]
            ]
            attachment_ids = [
                row["attachment_id"] for row in rows if row["attachment_id"]
            ]
            feedback_ids = [row["feedback_id"] for row in rows]
            if feedback_ids:
                placeholders = ",".join("?" for _ in feedback_ids)
                self._conn.execute(
                    f"DELETE FROM feedback_items WHERE feedback_id IN ({placeholders})",
                    feedback_ids,
                )
            if attachment_ids:
                placeholders = ",".join("?" for _ in attachment_ids)
                self._conn.execute(
                    f"DELETE FROM feedback_attachments WHERE attachment_id IN ({placeholders})",
                    attachment_ids,
                )
            if expired_targets:
                placeholders = ",".join("?" for _ in expired_targets)
                self._conn.execute(
                    f"DELETE FROM feedback_targets WHERE target_id IN ({placeholders})",
                    expired_targets,
                )
            self._conn.execute(
                "DELETE FROM feedback_visitors WHERE expires_at <= ?", (moment,)
            )
            self._conn.commit()
        return {
            "feedbacks": len(feedback_ids),
            "attachments": len(attachment_ids),
            "targets": len(expired_targets),
            "attachment_names": attachment_names,
        }


# ---------- id ----------

def _new_target_id() -> str:
    import uuid

    return f"fbt_{uuid.uuid4().hex[:24]}"


def _new_feedback_id() -> str:
    import uuid

    return f"FB-{uuid.uuid4().hex[:10].upper()}"


def _new_attachment_id() -> str:
    import uuid

    return f"fba_{uuid.uuid4().hex[:24]}"
