"""SQLite 实现的会话存储。

设计要点：
    - 用 Python 标准库 sqlite3，requirements.txt 无需新增依赖；
    - WAL 模式，读写不互斥；
    - 单连接 + 线程锁：所有 SQL 走同一个连接，避免多 worker / 多线程下的连接竞争。
      未来换 Postgres 时只需替换本类，上层 session_service 不动。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.storage.models import MessageRecord, SessionRecord, WorkspaceRecord


logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    subject_ref TEXT,
    subject_id  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id                   TEXT PRIMARY KEY,
    workspace_id         TEXT REFERENCES workspaces(id) ON DELETE SET NULL,
    title                TEXT NOT NULL DEFAULT '新对话',
    state                TEXT NOT NULL,
    design_session_id    TEXT,
    report_definition_id TEXT,
    preview_run_id       TEXT,
    message_count        INTEGER NOT NULL DEFAULT 0,
    snapshot             TEXT,
    pending_choice       TEXT,
    pending_edit_plan    TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    deleted_at           TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    role          TEXT NOT NULL,
    content       TEXT NOT NULL DEFAULT '',
    payload       TEXT,
    client_msg_id TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE(session_id, seq),
    UNIQUE(session_id, client_msg_id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_ws      ON sessions(workspace_id);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _load(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[session_store] payload 解析失败，已丢弃")
        return None


class SqliteSessionRepository:
    """会话存储的 SQLite 实现。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    @property
    def db_path(self) -> Path:
        return self._db_path

    @db_path.setter
    def db_path(self, value: str | Path) -> None:
        """换库文件（测试用）。换之前先关掉旧连接。"""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self._db_path = Path(value)

    @property
    def conn(self) -> sqlite3.Connection:
        """惰性建连：第一次访问时才落文件，测试可以先改 db_path 再用。"""
        with self._lock:
            if self._conn is None:
                self._conn = self._connect()
            return self._conn

    @property
    def lock(self) -> threading.RLock:
        """共用写锁。

        设计存储（#19 的 C 模块）必须注入**同一条连接**和**同一把锁**：
        同一把锁保证两边的写不会交错，同一条连接才能真的落进同一笔事务。
        """
        return self._lock

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        self._migrate(conn)
        conn.commit()
        logger.info("[session_store] 已连接 SQLite：%s", self._db_path)
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """给已有库补列。

        `CREATE TABLE IF NOT EXISTS` 不会改已存在的表结构，
        新列必须单独 ALTER 一次，否则老库里读不到该字段。
        """
        existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(sessions)")
        }
        if "pending_edit_plan" not in existing:
            conn.execute("ALTER TABLE sessions ADD COLUMN pending_edit_plan TEXT")
            logger.info("[session_store] 已迁移 sessions.pending_edit_plan 列")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ---------- 项目 ----------

    def create_workspace(
        self,
        *,
        id: str,
        name: str,
        description: str = "",
        subjectRef: str | None = None,
        subjectId: str | None = None,
    ) -> WorkspaceRecord:
        now = _now()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO workspaces (id, name, description, subject_ref, subject_id,
                                       created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (id, name, description, subjectRef, subjectId, now, now),
            )
            self.conn.commit()
        return WorkspaceRecord(
            id=id,
            name=name,
            description=description,
            subjectRef=subjectRef,
            subjectId=subjectId,
            createdAt=now,
            updatedAt=now,
        )

    def list_workspaces(self) -> list[WorkspaceRecord]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT w.*,
                       (SELECT COUNT(*) FROM sessions s
                         WHERE s.workspace_id = w.id AND s.deleted_at IS NULL) AS session_count
                  FROM workspaces w
                 ORDER BY w.created_at ASC
                """
            ).fetchall()
        return [
            WorkspaceRecord(
                id=row["id"],
                name=row["name"],
                description=row["description"],
                subjectRef=row["subject_ref"],
                subjectId=row["subject_id"],
                createdAt=row["created_at"],
                updatedAt=row["updated_at"],
                sessionCount=row["session_count"],
            )
            for row in rows
        ]

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        for record in self.list_workspaces():
            if record.id == workspace_id:
                return record
        return None

    def update_workspace(
        self,
        workspace_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> WorkspaceRecord | None:
        with self._lock:
            current = self.conn.execute(
                "SELECT id FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
            if current is None:
                return None
            if name is not None:
                self.conn.execute(
                    "UPDATE workspaces SET name = ? WHERE id = ?", (name, workspace_id)
                )
            if description is not None:
                self.conn.execute(
                    "UPDATE workspaces SET description = ? WHERE id = ?",
                    (description, workspace_id),
                )
            self.conn.execute(
                "UPDATE workspaces SET updated_at = ? WHERE id = ?", (_now(), workspace_id)
            )
            self.conn.commit()
        return self.get_workspace(workspace_id)

    def delete_workspace(self, workspace_id: str) -> bool:
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM workspaces WHERE id = ?", (workspace_id,)
            )
            self.conn.commit()
            return cursor.rowcount > 0

    # ---------- 会话 ----------

    def _purge_soft_deleted(self, session_id: str) -> None:
        """物理清掉已软删的会话行。

        软删行对上层不可见（get_session 过滤 deleted_at），但主键还在。
        如果后续写入打在它身上，UPDATE 生效了上层却读不到，
        还会撞上回读断言。统一在这里清干净，让「已删除的会话」真的消失。
        """
        self.conn.execute(
            "DELETE FROM sessions WHERE id = ? AND deleted_at IS NOT NULL",
            (session_id,),
        )

    def upsert_session(
        self,
        *,
        session_id: str,
        state: str,
        title: str | None = None,
        workspaceId: str | None = None,
        designSessionId: str | None = None,
        reportDefinitionId: str | None = None,
        previewRunId: str | None = None,
        snapshot: dict[str, Any] | None = None,
        messageCount: int | None = None,
    ) -> SessionRecord:
        now = _now()
        with self._lock:
            existing = self.conn.execute(
                "SELECT id FROM sessions WHERE id = ? AND deleted_at IS NULL",
                (session_id,),
            ).fetchone()
            if existing is None:
                # 已软删的行不能被 UPDATE 复活：先物理清掉再重新建行。
                # 否则回读 get_session（过滤 deleted_at）会拿到 None。
                self._purge_soft_deleted(session_id)
                self.conn.execute(
                    """
                    INSERT INTO sessions (id, workspace_id, title, state, design_session_id,
                                          report_definition_id, preview_run_id, message_count,
                                          snapshot, pending_choice, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        session_id,
                        workspaceId,
                        title or "新对话",
                        state,
                        designSessionId,
                        reportDefinitionId,
                        previewRunId,
                        messageCount or 0,
                        _dump(snapshot),
                        now,
                        now,
                    ),
                )
            else:
                sets = ["state = ?", "updated_at = ?"]
                params: list[Any] = [state, now]
                if title is not None:
                    sets.append("title = ?")
                    params.append(title)
                if workspaceId is not None:
                    sets.append("workspace_id = ?")
                    params.append(workspaceId)
                if designSessionId is not None:
                    sets.append("design_session_id = ?")
                    params.append(designSessionId)
                if reportDefinitionId is not None:
                    sets.append("report_definition_id = ?")
                    params.append(reportDefinitionId)
                if previewRunId is not None:
                    sets.append("preview_run_id = ?")
                    params.append(previewRunId)
                if messageCount is not None:
                    sets.append("message_count = ?")
                    params.append(messageCount)
                if snapshot is not None:
                    sets.append("snapshot = ?")
                    params.append(_dump(snapshot))
                params.append(session_id)
                self.conn.execute(
                    f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params
                )
            self.conn.commit()
        record = self.get_session(session_id)
        assert record is not None  # 刚写入必然存在
        return record

    def set_pending_choice(
        self, session_id: str, choice: dict[str, Any] | None
    ) -> None:
        """写 / 清选择锁。传 None 表示清空。

        用 upsert：选择锁可能在会话行创建之前就先写进来（第一轮请求的顺序是
        先锁选项、后落会话行），纯 UPDATE 会把锁悄悄丢掉。
        """
        now = _now()
        with self._lock:
            # 已软删的会话不该再持有锁，先清掉再写。
            self._purge_soft_deleted(session_id)
            self.conn.execute(
                """
                INSERT INTO sessions (id, title, state, message_count,
                                      pending_choice, created_at, updated_at)
                VALUES (?, '新对话', 'ANALYZING', 0, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE
                    SET pending_choice = excluded.pending_choice
                    WHERE sessions.deleted_at IS NULL
                """,
                (session_id, _dump(choice), now, now),
            )
            self.conn.commit()

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM sessions WHERE id = ? AND deleted_at IS NULL",
                (session_id,),
            ).fetchone()
        return self._row_to_session(row) if row else None

    # ---------- 待确认的编辑计划 ----------
    #
    # 这三个操作不靠进程内锁，靠 SQLite 的写事务：
    # 多 worker / 多进程同时点「接受修改」时，同一份计划只能被取走一次。

    def save_pending_edit_plan(
        self, session_id: str, plan: dict[str, Any]
    ) -> None:
        """写入待确认计划（覆盖已有计划）。"""
        now = _now()
        with self._lock:
            self._purge_soft_deleted(session_id)
            self.conn.execute(
                """
                INSERT INTO sessions (id, title, state, message_count,
                                      pending_edit_plan, created_at, updated_at)
                VALUES (?, '新对话', 'ANALYZING', 0, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE
                    SET pending_edit_plan = excluded.pending_edit_plan,
                        updated_at = excluded.updated_at
                    WHERE sessions.deleted_at IS NULL
                """,
                (session_id, _dump(plan), now, now),
            )
            self.conn.commit()

    def save_pending_edit_plan_if_absent(
        self, session_id: str, plan: dict[str, Any]
    ) -> dict[str, Any]:
        """写入待确认计划；已经有计划时保留旧的并返回它。

        `BEGIN IMMEDIATE` 立刻拿写锁，「读旧值 + 写新值」在一个事务里完成，
        避免两个进程同时以为自己是第一个。
        """
        now = _now()
        with self._lock:
            conn = self.conn
            self._begin_immediate(conn)
            try:
                self._purge_soft_deleted(session_id)
                row = conn.execute(
                    "SELECT pending_edit_plan FROM sessions"
                    " WHERE id = ? AND deleted_at IS NULL",
                    (session_id,),
                ).fetchone()
                existing = _load(row["pending_edit_plan"]) if row else None
                if existing:
                    conn.commit()
                    return existing
                conn.execute(
                    """
                    INSERT INTO sessions (id, title, state, message_count,
                                          pending_edit_plan, created_at, updated_at)
                    VALUES (?, '新对话', 'ANALYZING', 0, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE
                        SET pending_edit_plan = excluded.pending_edit_plan,
                            updated_at = excluded.updated_at
                        WHERE sessions.deleted_at IS NULL
                    """,
                    (session_id, _dump(plan), now, now),
                )
                conn.commit()
                return plan
            except Exception:
                conn.rollback()
                raise

    def take_pending_edit_plan(
        self, session_id: str, plan_id: str
    ) -> dict[str, Any] | None:
        """原子取走指定计划。

        读计划 + 删计划在同一个写事务里：删掉了才说明这份计划归本次调用所有，
        重复点击「接受修改」拿到的是 None，不会改两遍草稿。
        """
        with self._lock:
            conn = self.conn
            self._begin_immediate(conn)
            try:
                row = conn.execute(
                    "SELECT pending_edit_plan FROM sessions"
                    " WHERE id = ? AND deleted_at IS NULL",
                    (session_id,),
                ).fetchone()
                plan = _load(row["pending_edit_plan"]) if row else None
                if not plan or str(plan.get("planId") or "") != str(plan_id):
                    conn.commit()
                    return None
                cursor = conn.execute(
                    "UPDATE sessions SET pending_edit_plan = NULL, updated_at = ?"
                    " WHERE id = ? AND pending_edit_plan IS NOT NULL",
                    (_now(), session_id),
                )
                conn.commit()
                return plan if cursor.rowcount > 0 else None
            except Exception:
                conn.rollback()
                raise

    def clear_pending_edit_plan(self, session_id: str) -> None:
        """清掉待确认计划。"""
        with self._lock:
            self.conn.execute(
                "UPDATE sessions SET pending_edit_plan = NULL, updated_at = ?"
                " WHERE id = ? AND deleted_at IS NULL",
                (_now(), session_id),
            )
            self.conn.commit()

    @staticmethod
    def _begin_immediate(conn: sqlite3.Connection) -> None:
        """开一个写事务。

        sqlite3 默认是隐式事务：上一条写语句可能还挂着事务，
        这时再 BEGIN 会报 "cannot start a transaction within a transaction"。
        """
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=row["id"],
            workspaceId=row["workspace_id"],
            title=row["title"],
            state=row["state"],
            designSessionId=row["design_session_id"],
            reportDefinitionId=row["report_definition_id"],
            previewRunId=row["preview_run_id"],
            messageCount=row["message_count"],
            snapshot=_load(row["snapshot"]),
            pendingChoice=_load(row["pending_choice"]),
            pendingEditPlan=_load(row["pending_edit_plan"]),
            createdAt=row["created_at"],
            updatedAt=row["updated_at"],
            deletedAt=row["deleted_at"],
        )

    def list_sessions(
        self,
        *,
        workspaceId: str | None = None,
        keyword: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SessionRecord], int]:
        conditions = ["deleted_at IS NULL", "message_count > 0"]
        params: list[Any] = []
        if workspaceId is not None:
            conditions.append("workspace_id = ?")
            params.append(workspaceId)
        if keyword:
            conditions.append("title LIKE ?")
            params.append(f"%{keyword}%")
        where = " AND ".join(conditions)
        with self._lock:
            total = self.conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE {where}", params
            ).fetchone()[0]
            rows = self.conn.execute(
                f"""
                SELECT * FROM sessions WHERE {where}
                 ORDER BY updated_at DESC, id DESC
                 LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return [self._row_to_session(row) for row in rows], total

    def update_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        workspaceId: str | None = None,
    ) -> SessionRecord | None:
        with self._lock:
            existing = self.conn.execute(
                "SELECT id FROM sessions WHERE id = ? AND deleted_at IS NULL",
                (session_id,),
            ).fetchone()
            if existing is None:
                return None
            if title is not None:
                self.conn.execute(
                    "UPDATE sessions SET title = ? WHERE id = ?", (title, session_id)
                )
            if workspaceId is not None:
                self.conn.execute(
                    "UPDATE sessions SET workspace_id = ? WHERE id = ?",
                    (workspaceId, session_id),
                )
            self.conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id)
            )
            self.conn.commit()
        return self.get_session(session_id)

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE sessions SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                (_now(), session_id),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    # ---------- 消息 ----------

    def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        payload: dict[str, Any] | None = None,
        clientMsgId: str | None = None,
    ) -> MessageRecord:
        now = _now()
        with self._lock:
            if clientMsgId:
                # 幂等：同一 (session_id, client_msg_id) 已存在则覆盖，不新增
                existing = self.conn.execute(
                    "SELECT * FROM messages WHERE session_id = ? AND client_msg_id = ?",
                    (session_id, clientMsgId),
                ).fetchone()
                if existing is not None:
                    self.conn.execute(
                        """
                        UPDATE messages SET role = ?, content = ?, payload = ?, created_at = ?
                         WHERE id = ?
                        """,
                        (role, content, _dump(payload), now, existing["id"]),
                    )
                    self.conn.commit()
                    return self._get_message(existing["id"])

            next_seq = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            cursor = self.conn.execute(
                """
                INSERT INTO messages (session_id, seq, role, content, payload,
                                      client_msg_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, next_seq, role, content, _dump(payload), clientMsgId, now),
            )
            self.conn.commit()
            return self._get_message(cursor.lastrowid)

    def _get_message(self, message_id: int) -> MessageRecord:
        row = self.conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        assert row is not None
        return self._row_to_message(row)

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(
            id=row["id"],
            sessionId=row["session_id"],
            seq=row["seq"],
            role=row["role"],
            content=row["content"],
            payload=_load(row["payload"]),
            clientMsgId=row["client_msg_id"],
            createdAt=row["created_at"],
        )

    def list_messages(self, session_id: str) -> list[MessageRecord]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY seq ASC",
                (session_id,),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def delete_messages_by_client_msg_id(
        self, session_id: str, client_msg_id: str
    ) -> int:
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND client_msg_id = ?",
                (session_id, client_msg_id),
            )
            self.conn.commit()
            return cursor.rowcount
