"""设计持久化的 SQLite 实现（#19 首波 C）。

要点：
    - 连接与锁由调用方注入：自己不建全局数据库客户端，也不另造锁；
    - 只新增 `report_design_` 前缀的表，不动旧 schema，也不迁移旧数据；
    - 认领、提交、失败各自是独立事务，模型工作在事务之外；
    - 提交时设计对象、阶段、操作结果与协调者注入的 write_turn 回调同事务提交。

表设计：
    report_design_sessions     一个会话的当前设计状态（阶段 + 当前成功方案）
    report_design_operations   一次设计操作（认领 → 提交 / 失败）

两张表都不对旧表建外键：设计行可能先于聊天会话行写入，
由协调者的 write_turn 回调在同一个事务里补上会话行。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, NamedTuple

from app.storage.report_design_repository import (
    CLAIM_KIND_CLAIMED,
    CLAIM_KIND_CONFLICT,
    CLAIM_KIND_DUPLICATE,
    COMMIT_KIND_COMMITTED,
    COMMIT_KIND_CONFLICT,
    DESIGN_PHASE_PLAN_NEEDED,
    DESIGN_PHASE_PLAN_WAITING_CONFIRM,
    OPERATION_FAILED,
    OPERATION_PROCESSING,
    OPERATION_SUCCEEDED,
    ClaimResult,
    CommitResult,
    DesignOperationStateError,
    DesignPlanInvalidError,
    LoadResult,
    WriteTurn,
)


logger = logging.getLogger(__name__)

# 失败文案入库前截断：安全错误不该把长文本带进库里。
_MAX_ERROR_LENGTH = 2000


_SCHEMA = """
CREATE TABLE IF NOT EXISTS report_design_sessions (
    session_id    TEXT PRIMARY KEY,
    design_phase  TEXT NOT NULL,
    plan_id       TEXT,
    plan_revision INTEGER,
    plan_json     TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_design_operations (
    operation_id      TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    message_id        TEXT NOT NULL,
    expected_revision INTEGER,
    status            TEXT NOT NULL,
    plan_id           TEXT,
    plan_revision     INTEGER,
    error_code        TEXT,
    error_message     TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE(session_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_report_design_ops_session
    ON report_design_operations (session_id, created_at);

-- 同一会话的同一方案版本只允许一条成功操作。
-- 这是「重复确认只产生一个已提交对象」的底座：确认动作按
-- (session_id, plan_id, plan_revision) 唯一，不受处理中 / 失败的行影响。
CREATE UNIQUE INDEX IF NOT EXISTS idx_report_design_ops_plan
    ON report_design_operations (session_id, plan_id, plan_revision)
    WHERE status = 'SUCCEEDED' AND plan_id IS NOT NULL AND plan_revision IS NOT NULL;
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class _DesignState(NamedTuple):
    """一行设计状态。"""

    phase: str
    plan_id: str | None
    revision: int | None
    plan_json: str | None


class _OperationRow(NamedTuple):
    """一行设计操作。"""

    operation_id: str
    session_id: str
    message_id: str
    expected_revision: int | None
    status: str
    plan_id: str | None
    plan_revision: int | None
    error_code: str | None
    error_message: str | None


class SqliteReportDesignRepository:
    """独立设计会话存储的 SQLite 实现。"""

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        lock: threading.RLock,
    ) -> None:
        """注入连接与共用锁。

        构造时不建表、不迁移、不碰旧存储：建表由 `initialize_schema` 显式触发。
        """
        self._conn = connection
        self._lock = lock

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # ---------- 表 ----------

    def initialize_schema(self) -> None:
        """建好 `report_design_` 前缀的表。

        只 CREATE IF NOT EXISTS：旧 schema 一个字节都不改。
        """
        with self._lock:
            conn = self._conn
            conn.executescript(_SCHEMA)
            conn.commit()
        logger.info("[report_design] 设计存储表已就绪")

    # ---------- 读取 ----------

    def load(self, session_id: str) -> LoadResult | None:
        """读会话的设计状态；没进过设计流程时返回 None。"""
        with self._lock:
            state = self._fetch_design_state(self._conn, session_id)
        if state is None:
            return None
        return {"designPhase": state.phase, "reportPlan": _load_json(state.plan_json)}

    # ---------- 认领 ----------

    def claim_operation(
        self, session_id: str, message_id: str, expected_revision: int | None
    ) -> ClaimResult:
        """认领一条消息对应的设计操作。"""
        if not session_id:
            raise ValueError("session_id 不能为空")
        if not message_id:
            raise ValueError("message_id 不能为空")
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision 必须是 None 或正整数")

        with self._lock:
            conn = self._conn
            self._begin(conn)
            try:
                # 同一条消息重复到达：先认得出上一次的处理状态，
                # 再谈基础版本 —— 已经处理过的消息不该被判成冲突。
                existing = self._fetch_operation_by_message(conn, session_id, message_id)
                if existing is not None:
                    result = self._duplicate_result(conn, existing)
                    conn.commit()
                    return result

                # 认领前先核对基础版本：版本对不上就不认领，
                # 把当前成功方案带回去，调用方可以据此提示用户。
                state = self._fetch_design_state(conn, session_id)
                current_revision = state.revision if state is not None else None
                if expected_revision is None:
                    mismatched = current_revision is not None
                else:
                    mismatched = current_revision != expected_revision
                if mismatched:
                    conn.commit()
                    return {
                        "kind": CLAIM_KIND_CONFLICT,
                        "operationId": None,
                        "sessionId": session_id,
                        "messageId": message_id,
                        "expectedRevision": expected_revision,
                        "designPhase": state.phase if state is not None else None,
                        "currentPlan": _load_json(state.plan_json)
                        if state is not None
                        else None,
                        "currentRevision": current_revision,
                        "reason": "revision_mismatch",
                    }

                now = _now()
                operation_id = f"rdop_{uuid.uuid4().hex[:12]}"
                # 会话还没进过设计流程就先记下「需要一份方案」。
                # 已有行不动：修改方案的认领不能把阶段退回 PLAN_NEEDED。
                conn.execute(
                    """
                    INSERT OR IGNORE INTO report_design_sessions
                        (session_id, design_phase, plan_id, plan_revision,
                         plan_json, created_at, updated_at)
                    VALUES (?, ?, NULL, NULL, NULL, ?, ?)
                    """,
                    (session_id, DESIGN_PHASE_PLAN_NEEDED, now, now),
                )
                # 认领之后的实际状态：新建时是「待生成方案」，
                # 修改时是「已有方案在等确认」，调用方据此决定模型工作怎么起手。
                current = self._fetch_design_state(conn, session_id)
                conn.execute(
                    """
                    INSERT INTO report_design_operations
                        (operation_id, session_id, message_id, expected_revision,
                         status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        session_id,
                        message_id,
                        expected_revision,
                        OPERATION_PROCESSING,
                        now,
                        now,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return {
            "kind": CLAIM_KIND_CLAIMED,
            "operationId": operation_id,
            "sessionId": session_id,
            "messageId": message_id,
            "expectedRevision": expected_revision,
            "status": OPERATION_PROCESSING,
            "designPhase": current.phase if current is not None else None,
            "currentPlan": _load_json(current.plan_json)
            if current is not None
            else None,
            "currentRevision": current.revision if current is not None else None,
        }

    # ---------- 提交 ----------

    def commit_plan(
        self,
        operation_id: str,
        expected_revision: int | None,
        plan: Mapping[str, Any],
        write_turn: WriteTurn | None = None,
    ) -> CommitResult:
        """提交校验后的方案。

        设计对象、阶段、操作结果与 write_turn 回调在同一笔事务里提交：
        回调抛错就整体回滚，上一成功方案原样保留。
        """
        data = self._validate_plan(plan)

        with self._lock:
            conn = self._conn
            self._begin(conn)
            try:
                operation = self._fetch_operation(conn, operation_id)
                if operation is None:
                    raise DesignOperationStateError(f"未知操作：{operation_id}")
                if operation.status == OPERATION_SUCCEEDED:
                    # 已经提交过：原样返回那次的结果，不再写一遍消息。
                    state = self._fetch_design_state(conn, operation.session_id)
                    conn.commit()
                    return {
                        "kind": COMMIT_KIND_COMMITTED,
                        "operationId": operation_id,
                        "sessionId": operation.session_id,
                        "planId": operation.plan_id,
                        "revision": operation.plan_revision,
                        "designPhase": state.phase if state is not None else None,
                        "plan": _load_json(state.plan_json)
                        if state is not None
                        else None,
                        "alreadyCommitted": True,
                    }
                if operation.status == OPERATION_FAILED:
                    raise DesignOperationStateError(f"操作已失败：{operation_id}")
                if operation.expected_revision != expected_revision:
                    raise DesignOperationStateError(
                        f"提交的基础版本与认领时不一致：{operation_id}"
                    )

                state = self._fetch_design_state(conn, operation.session_id)
                current_revision = state.revision if state is not None else None
                if current_revision != expected_revision:
                    # 基础版本已被别的操作推进：本次结果不提交，操作记为失败，
                    # 上一成功方案原样保留。
                    message = (
                        f"基础版本已变：当前 {current_revision}，"
                        f"本次基于 {expected_revision}"
                    )
                    self._mark_failed(conn, operation_id, "revision_conflict", message)
                    conn.commit()
                    return {
                        "kind": COMMIT_KIND_CONFLICT,
                        "operationId": operation_id,
                        "sessionId": operation.session_id,
                        "currentPlan": _load_json(state.plan_json)
                        if state is not None
                        else None,
                        "currentRevision": current_revision,
                        "designPhase": state.phase if state is not None else None,
                        "reason": "revision_mismatch",
                        "error": {"code": "revision_conflict", "message": message},
                    }

                self._check_plan_base(data, expected_revision, state)

                now = _now()
                conn.execute(
                    """
                    INSERT INTO report_design_sessions
                        (session_id, design_phase, plan_id, plan_revision,
                         plan_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        design_phase = excluded.design_phase,
                        plan_id = excluded.plan_id,
                        plan_revision = excluded.plan_revision,
                        plan_json = excluded.plan_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        operation.session_id,
                        DESIGN_PHASE_PLAN_WAITING_CONFIRM,
                        data["id"],
                        data["revision"],
                        json.dumps(data, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                cursor = conn.execute(
                    """
                    UPDATE report_design_operations
                       SET status = ?, plan_id = ?, plan_revision = ?, updated_at = ?
                     WHERE operation_id = ? AND status = ?
                    """,
                    (
                        OPERATION_SUCCEEDED,
                        data["id"],
                        data["revision"],
                        now,
                        operation_id,
                        OPERATION_PROCESSING,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DesignOperationStateError(f"操作状态已变：{operation_id}")

                # 协调者注入的同事务写入：消息与会话行在这里落库。
                # 回调自己不能 commit，否则同事务提交就不成立。
                if write_turn is not None:
                    write_turn(conn)
                    if not conn.in_transaction:
                        logger.warning(
                            "[report_design] write_turn 回调自行结束了事务，"
                            "同事务提交无法保证：operation=%s",
                            operation_id,
                        )

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return {
            "kind": COMMIT_KIND_COMMITTED,
            "operationId": operation_id,
            "sessionId": operation.session_id,
            "planId": data["id"],
            "revision": data["revision"],
            "designPhase": DESIGN_PHASE_PLAN_WAITING_CONFIRM,
            "plan": data,
            "alreadyCommitted": False,
        }

    # ---------- 失败 ----------

    def fail_operation(
        self, operation_id: str, safe_error: str | Mapping[str, Any]
    ) -> None:
        """把操作标记为失败。

        上一成功方案不受影响：这里只写操作行，不碰设计状态。
        """
        code, message = _normalize_safe_error(safe_error)
        with self._lock:
            conn = self._conn
            operation = self._fetch_operation(conn, operation_id)
            if operation is None:
                raise DesignOperationStateError(f"未知操作：{operation_id}")
            if operation.status == OPERATION_SUCCEEDED:
                raise DesignOperationStateError(
                    f"操作已提交，不能标记失败：{operation_id}"
                )
            self._begin(conn)
            try:
                self._mark_failed(conn, operation_id, code, message)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ---------- 内部 ----------

    def _duplicate_result(
        self, conn: sqlite3.Connection, operation: _OperationRow
    ) -> ClaimResult:
        """重复消息的结果：区分处理中、成功、失败。"""
        state = self._fetch_design_state(conn, operation.session_id)
        current_plan = _load_json(state.plan_json) if state is not None else None
        current_revision = state.revision if state is not None else None
        # 这条消息提交的方案如果已经是当前版本，就把方案带回去；
        # 已被更新的版本取代时不冒充，调用方可以另查 load()。
        plan = (
            current_plan
            if operation.plan_revision is not None
            and current_revision == operation.plan_revision
            else None
        )
        result: ClaimResult = {
            "kind": CLAIM_KIND_DUPLICATE,
            "operationId": operation.operation_id,
            "sessionId": operation.session_id,
            "messageId": operation.message_id,
            "expectedRevision": operation.expected_revision,
            "status": operation.status,
            "designPhase": state.phase if state is not None else None,
            "currentPlan": current_plan,
            "currentRevision": current_revision,
            "planId": operation.plan_id,
            "planRevision": operation.plan_revision,
            "plan": plan,
        }
        if operation.status == OPERATION_FAILED:
            result["error"] = {
                "code": operation.error_code or "unknown",
                "message": operation.error_message or "",
            }
        return result

    @staticmethod
    def _validate_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
        """校验方案是可保存的 JSON 形态。

        领域语义（板块、指标、组件类型）由 A 校验，C 只看结构与基础版本。
        """
        if not isinstance(plan, Mapping):
            raise DesignPlanInvalidError("方案必须是 JSON mapping")
        data = dict(plan)
        plan_id = data.get("id")
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise DesignPlanInvalidError("方案缺少 id")
        revision = data.get("revision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise DesignPlanInvalidError("方案 revision 必须是正整数")
        try:
            json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise DesignPlanInvalidError(f"方案不是可保存的 JSON：{exc}") from exc
        return data

    @staticmethod
    def _check_plan_base(
        data: dict[str, Any],
        expected_revision: int | None,
        state: _DesignState | None,
    ) -> None:
        """提交的方案必须建立在当前成功方案之上。"""
        expected_new_revision = 1 if expected_revision is None else expected_revision + 1
        if data["revision"] != expected_new_revision:
            raise DesignPlanInvalidError(
                f"方案 revision 应为 {expected_new_revision}，实际 {data['revision']}"
            )
        if expected_revision is not None:
            current_id = state.plan_id if state is not None else None
            if current_id is not None and data["id"] != current_id:
                raise DesignPlanInvalidError(
                    f"方案 id 应为 {current_id}，实际 {data['id']}"
                )

    @staticmethod
    def _fetch_operation_by_message(
        conn: sqlite3.Connection, session_id: str, message_id: str
    ) -> _OperationRow | None:
        row = conn.execute(
            "SELECT operation_id, session_id, message_id, expected_revision,"
            " status, plan_id, plan_revision, error_code, error_message"
            " FROM report_design_operations"
            " WHERE session_id = ? AND message_id = ?",
            (session_id, message_id),
        ).fetchone()
        if row is None:
            return None
        return _OperationRow(*row)

    @staticmethod
    def _mark_failed(
        conn: sqlite3.Connection, operation_id: str, code: str, message: str
    ) -> None:
        """把操作标记为失败。失败信息是安全文案，不带客户原始数据。"""
        conn.execute(
            "UPDATE report_design_operations"
            " SET status = ?, error_code = ?, error_message = ?, updated_at = ?"
            " WHERE operation_id = ?",
            (OPERATION_FAILED, code, message[:_MAX_ERROR_LENGTH], _now(), operation_id),
        )

    @staticmethod
    def _fetch_operation(
        conn: sqlite3.Connection, operation_id: str
    ) -> _OperationRow | None:
        row = conn.execute(
            "SELECT operation_id, session_id, message_id, expected_revision,"
            " status, plan_id, plan_revision, error_code, error_message"
            " FROM report_design_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        return _OperationRow(*row)

    @staticmethod
    def _fetch_design_state(
        conn: sqlite3.Connection, session_id: str
    ) -> _DesignState | None:
        row = conn.execute(
            "SELECT design_phase, plan_id, plan_revision, plan_json"
            " FROM report_design_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return _DesignState(row[0], row[1], row[2], row[3])

    @staticmethod
    def _begin(conn: sqlite3.Connection) -> None:
        """开一个写事务。

        sqlite3 默认是隐式事务：上一条写语句可能还挂着事务，
        这时再 BEGIN 会报 "cannot start a transaction within a transaction"。
        """
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")


def _normalize_safe_error(safe_error: str | Mapping[str, Any]) -> tuple[str, str]:
    """安全错误归一化成 (code, message)。

    只保留可辨识的短码和安全文案，不落客户原始数据。
    """
    if isinstance(safe_error, Mapping):
        code = str(safe_error.get("code") or "unknown")[:64]
        message = str(safe_error.get("message") or "")
    else:
        code = "unknown"
        message = str(safe_error)
    return code, message


def _load_json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[report_design] 方案 JSON 解析失败，已丢弃")
        return None
    return parsed if isinstance(parsed, dict) else None
