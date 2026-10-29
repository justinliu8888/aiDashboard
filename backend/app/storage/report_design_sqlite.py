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
    DESIGN_PHASE_TEMPLATE_READY,
    DESIGN_PHASE_DRAFT_READY,
    OPERATION_FAILED,
    OPERATION_PROCESSING,
    OPERATION_SUCCEEDED,
    ClaimResult,
    CommitResult,
    ConfirmationResult,
    DesignOperationStateError,
    DesignPlanInvalidError,
    DesignTemplateConflictError,
    DesignTemplateNotFoundError,
    LoadResult,
    PhaseResetResult,
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

-- 已提交的模板设计（工单 02）。一个会话一份当前模板。
CREATE TABLE IF NOT EXISTS report_design_templates (
    session_id        TEXT PRIMARY KEY,
    template_id       TEXT NOT NULL,
    template_revision INTEGER NOT NULL,
    plan_id           TEXT NOT NULL,
    plan_revision     INTEGER NOT NULL,
    data_status       TEXT NOT NULL,
    template_json     TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- 确认关系：哪一版方案被确认过，产出了哪个模板。
--
-- 唯一键在这里，而**不在**操作表上：操作表那组 (session_id, plan_id,
-- plan_revision) 已经被「提交方案」那次操作占用了，确认动作不能往同一组
-- 键上再写一条 —— 否则第二次确认会撞库。规格写的是「确认另按 sessionId、
-- planId、planRevision 保证唯一」，就是这张表。
CREATE TABLE IF NOT EXISTS report_design_confirmations (
    session_id    TEXT NOT NULL,
    plan_id       TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    template_id   TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (session_id, plan_id, plan_revision)
);

CREATE TABLE IF NOT EXISTS report_design_definitions (
    session_id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL,
    template_revision INTEGER NOT NULL,
    definition_json TEXT NOT NULL
);
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
            template = self._fetch_template(self._conn, session_id)
            draft = self._conn.execute(
                "SELECT definition_json FROM report_design_definitions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if state is None:
            return None
        return {
            "designPhase": state.phase,
            "reportPlan": _load_json(state.plan_json),
            "templateDesign": template,
            "definitionDraft": _load_json(draft[0]) if draft else None,
        }

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

    # ---------- 澄清（协调者补充的 v2 接口） ----------

    def commit_clarification(
        self,
        operation_id: str,
        expected_revision: int | None,
        clarification: str,
        write_turn: WriteTurn | None = None,
    ) -> CommitResult:
        """提交一次必要追问。

        追问是**正常的一轮**，不是技术失败：

            - 不改当前成功方案，上一版原样保留；
            - 不推进阶段：没有方案时仍是 PLAN_NEEDED，
              有方案时仍是 PLAN_WAITING_CONFIRM；
            - 操作记成 SUCCEEDED 且不关联方案版本，
              这样同一条消息重发时能认出「已经问过了」，
              不会又问一遍。

        仍与 write_turn 回调同事务提交：追问这一轮的消息和操作状态
        要么一起落库，要么一起回滚。
        """
        text = str(clarification or "").strip()
        if not text:
            raise DesignPlanInvalidError("澄清内容不能为空")

        with self._lock:
            conn = self._conn
            self._begin(conn)
            try:
                operation = self._fetch_operation(conn, operation_id)
                if operation is None:
                    raise DesignOperationStateError(f"未知操作：{operation_id}")
                if operation.status == OPERATION_SUCCEEDED:
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

                now = _now()
                # 阶段不动：没有方案时是 PLAN_NEEDED，有方案时是
                # PLAN_WAITING_CONFIRM。澄清不是方案提交，不推进阶段。
                cursor = conn.execute(
                    """
                    UPDATE report_design_operations
                       SET status = ?, plan_id = NULL, plan_revision = NULL,
                           updated_at = ?
                     WHERE operation_id = ? AND status = ?
                    """,
                    (OPERATION_SUCCEEDED, now, operation_id, OPERATION_PROCESSING),
                )
                if cursor.rowcount != 1:
                    raise DesignOperationStateError(f"操作状态已变：{operation_id}")

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
            "planId": state.plan_id if state is not None else None,
            "revision": state.revision if state is not None else None,
            "designPhase": state.phase if state is not None else None,
            "plan": _load_json(state.plan_json) if state is not None else None,
            "alreadyCommitted": False,
        }

    # ---------- 模板（工单 02） ----------
    #
    # #36 起「确认方案」一次提交模板 + 草稿（``commit_template_and_definition``）。
    # 旧的「只提交模板」写入路径（会把阶段写成 ``TEMPLATE_READY``）已删除：
    # 它没有调用方，却会把阶段写回已下线的模板中间页状态。

    def find_confirmation(
        self, session_id: str, plan_id: str, plan_revision: int
    ) -> ConfirmationResult | None:
        """查某一版方案有没有被确认过。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT session_id, plan_id, plan_revision, template_id, created_at"
                " FROM report_design_confirmations"
                " WHERE session_id = ? AND plan_id = ? AND plan_revision = ?",
                (session_id, plan_id, plan_revision),
            ).fetchone()
        if row is None:
            return None
        return {
            "sessionId": row[0],
            "planId": row[1],
            "planRevision": row[2],
            "templateId": row[3],
            "createdAt": row[4],
        }

    def load_template(self, session_id: str) -> dict[str, Any] | None:
        """读会话当前已提交的模板设计。"""
        with self._lock:
            return self._fetch_template(self._conn, session_id)

    def commit_definition(
        self, session_id: str, template_id: str, expected_revision: int,
        definition: Mapping[str, Any], write_turn: WriteTurn, *, operation_id: str,
    ) -> dict[str, Any]:
        with self._lock:
            conn = self._conn
            self._begin(conn)
            try:
                operation = self._fetch_operation(conn, operation_id)
                if (operation.session_id != session_id or operation.status != OPERATION_PROCESSING):
                    raise DesignOperationStateError("模板确认操作未认领或已结束")
                current = self._fetch_template(conn, session_id)
                state = self._fetch_design_state(conn, session_id)
                if current is None:
                    raise DesignTemplateNotFoundError("当前会话没有模板")
                if (current["id"] != template_id or current["revision"] != expected_revision
                        or state is None or current["planId"] != state.plan_id
                        or current["planRevision"] != state.revision
                        or state.phase not in {DESIGN_PHASE_TEMPLATE_READY, DESIGN_PHASE_DRAFT_READY}):
                    raise DesignTemplateConflictError("模板或方案已变化，请刷新后重试")
                existing = conn.execute(
                    "SELECT template_id, template_revision, definition_json"
                    " FROM report_design_definitions WHERE session_id = ?", (session_id,),
                ).fetchone()
                if existing and existing[:2] == (template_id, expected_revision):
                    conn.execute(
                        "UPDATE report_design_operations SET status = ?, updated_at = ? WHERE operation_id = ?",
                        (OPERATION_SUCCEEDED, _now(), operation_id),
                    )
                    conn.commit()
                    return {"definitionDraft": _load_json(existing[2]), "alreadyCommitted": True}
                if state.phase == DESIGN_PHASE_DRAFT_READY:
                    raise DesignTemplateConflictError("草稿已经生成，不能再次替换")
                conn.execute(
                    "INSERT INTO report_design_definitions VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(session_id) DO UPDATE SET template_id = excluded.template_id,"
                    " template_revision = excluded.template_revision, definition_json = excluded.definition_json",
                    (session_id, template_id, expected_revision, json.dumps(dict(definition), ensure_ascii=False)),
                )
                conn.execute(
                    "UPDATE report_design_sessions SET design_phase = ?, updated_at = ? WHERE session_id = ?",
                    (DESIGN_PHASE_DRAFT_READY, _now(), session_id),
                )
                write_turn(conn)
                conn.execute(
                    "UPDATE report_design_operations SET status = ?, updated_at = ? WHERE operation_id = ?",
                    (OPERATION_SUCCEEDED, _now(), operation_id),
                )
                conn.commit()
                return {"definitionDraft": dict(definition), "alreadyCommitted": False}
            except Exception:
                conn.rollback()
                raise

    def commit_template_and_definition(
        self,
        operation_id: str,
        expected_revision: int | None,
        template: Mapping[str, Any],
        definition: Mapping[str, Any],
        write_turn: WriteTurn | None = None,
    ) -> dict[str, Any]:
        """一次提交「模板 + 草稿 + 下一步菜单」。

        「确认方案」是**一次**操作：内部模板结构、报表草稿、确认关系、
        阶段（``DRAFT_READY``）、操作结果与本轮消息必须同生共死。

        同一笔事务里写全：确认关系、模板、草稿、阶段、操作结果、
        write_turn 回调（会话行 / 快照 / 消息 / 待确认菜单）。
        中途任何一步失败就整体回滚 —— 上一成功方案与阶段原样保留，
        不会留下「已确认成功但没有草稿」的半截状态。

        **幂等键是方案版本**（``plan_id`` + ``plan_revision``），不是
        模板版本：同一版方案重复确认要复用同一份草稿，既不产生第二份，
        也不推高模板版本号。
        """
        data = self._validate_template(template)
        draft_data = dict(definition)
        try:
            json.dumps(draft_data, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise DesignPlanInvalidError(f"草稿不是可保存的 JSON：{exc}") from exc

        with self._lock:
            conn = self._conn
            self._begin(conn)
            try:
                operation = self._fetch_operation(conn, operation_id)
                if operation is None:
                    raise DesignOperationStateError(f"未知操作：{operation_id}")
                if operation.status == OPERATION_SUCCEEDED:
                    # 同一条消息已经提交过：原样带回已提交的对象，不重写。
                    state = self._fetch_design_state(conn, operation.session_id)
                    existing = self._fetch_template(conn, operation.session_id)
                    existing_draft = self._fetch_definition(conn, operation.session_id)
                    conn.commit()
                    return {
                        "kind": COMMIT_KIND_COMMITTED,
                        "operationId": operation_id,
                        "sessionId": operation.session_id,
                        "planId": data["planId"],
                        "revision": data["planRevision"],
                        "designPhase": state.phase if state is not None else None,
                        "plan": _load_json(state.plan_json)
                        if state is not None
                        else None,
                        "template": existing,
                        "templateId": existing.get("id") if existing else None,
                        "templateRevision": existing.get("revision") if existing else None,
                        "definitionDraft": existing_draft,
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
                    # 基础版本已变：本次确认基于旧方案，不覆盖新版。
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

                # 模板与草稿必须对应**已确认的那一版**方案。
                if state is None or state.plan_id != data["planId"]:
                    raise DesignPlanInvalidError(
                        "模板对应的方案 id 与当前成功方案不一致"
                    )
                if current_revision != data["planRevision"]:
                    raise DesignPlanInvalidError(
                        f"模板对应的方案版本应为 {current_revision}，"
                        f"实际 {data['planRevision']}"
                    )

                now = _now()
                # 幂等：同一版方案已经确认过 → 复用已提交的模板与草稿，
                # 不重复生成、不推高版本号。
                already = self._fetch_confirmation(
                    conn, operation.session_id, data["planId"], data["planRevision"]
                )
                if already is not None:
                    self._mark_succeeded(conn, operation_id, now)
                    conn.commit()
                    existing = self._fetch_template(conn, operation.session_id)
                    existing_draft = self._fetch_definition(conn, operation.session_id)
                    logger.info(
                        "[report_design] 同一版方案已确认过，复用已有草稿 "
                        "session=%s plan=%s rev=%s",
                        operation.session_id,
                        data["planId"],
                        data["planRevision"],
                    )
                    return {
                        "kind": COMMIT_KIND_COMMITTED,
                        "operationId": operation_id,
                        "sessionId": operation.session_id,
                        "planId": data["planId"],
                        "revision": data["planRevision"],
                        "designPhase": DESIGN_PHASE_DRAFT_READY,
                        "plan": _load_json(state.plan_json)
                        if state is not None
                        else None,
                        "template": existing,
                        "templateId": existing.get("id") if existing else None,
                        "templateRevision": existing.get("revision") if existing else None,
                        "definitionDraft": existing_draft,
                        "alreadyCommitted": True,
                    }

                try:
                    conn.execute(
                        """
                        INSERT INTO report_design_confirmations
                            (session_id, plan_id, plan_revision, template_id,
                             created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            operation.session_id,
                            data["planId"],
                            data["planRevision"],
                            data["id"],
                            now,
                        ),
                    )
                except sqlite3.IntegrityError:
                    # 另一个进程已经确认过同一版方案：不重复生成。
                    conn.rollback()
                    # 操作不能停在 PROCESSING：显式收尾，否则这条消息
                    # 永远显示「处理中」，用户也拿不到重试入口。
                    self._mark_succeeded(conn, operation_id, _now())
                    conn.commit()
                    existing = self._fetch_template(conn, operation.session_id)
                    existing_draft = self._fetch_definition(conn, operation.session_id)
                    logger.info(
                        "[report_design] 同一版方案已被确认过，不重复生成 "
                        "session=%s plan=%s rev=%s",
                        operation.session_id,
                        data["planId"],
                        data["planRevision"],
                    )
                    return {
                        "kind": COMMIT_KIND_COMMITTED,
                        "operationId": operation_id,
                        "sessionId": operation.session_id,
                        "planId": data["planId"],
                        "revision": data["planRevision"],
                        "designPhase": DESIGN_PHASE_DRAFT_READY,
                        "plan": _load_json(state.plan_json)
                        if state is not None
                        else None,
                        "template": existing,
                        "templateId": existing.get("id") if existing else None,
                        "templateRevision": existing.get("revision") if existing else None,
                        "definitionDraft": existing_draft,
                        "alreadyCommitted": True,
                    }

                previous = self._fetch_template(conn, operation.session_id)
                template_revision = (
                    int(previous.get("revision") or 0) + 1 if previous else 1
                )
                conn.execute(
                    """
                    INSERT INTO report_design_templates
                        (session_id, template_id, template_revision, plan_id,
                         plan_revision, data_status, template_json,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        template_id = excluded.template_id,
                        template_revision = excluded.template_revision,
                        plan_id = excluded.plan_id,
                        plan_revision = excluded.plan_revision,
                        data_status = excluded.data_status,
                        template_json = excluded.template_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        operation.session_id,
                        data["id"],
                        template_revision,
                        data["planId"],
                        data["planRevision"],
                        str(data.get("dataStatus") or "placeholder"),
                        json.dumps(data, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO report_design_definitions
                        (session_id, template_id, template_revision, definition_json)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        template_id = excluded.template_id,
                        template_revision = excluded.template_revision,
                        definition_json = excluded.definition_json
                    """,
                    (
                        operation.session_id,
                        data["id"],
                        template_revision,
                        json.dumps(draft_data, ensure_ascii=False),
                    ),
                )
                conn.execute(
                    "UPDATE report_design_sessions"
                    " SET design_phase = ?, updated_at = ? WHERE session_id = ?",
                    (DESIGN_PHASE_DRAFT_READY, now, operation.session_id),
                )
                # 协调者注入的同事务写入：会话行、快照、本轮消息与
                # 待确认菜单都在这里落库。回调自己不能 commit。
                if write_turn is not None:
                    write_turn(conn)
                    if not conn.in_transaction:
                        logger.warning(
                            "[report_design] write_turn 回调自行结束了事务，"
                            "同事务提交无法保证：operation=%s",
                            operation_id,
                        )
                cursor = conn.execute(
                    """
                    UPDATE report_design_operations
                       SET status = ?, plan_id = NULL, plan_revision = NULL,
                           updated_at = ?
                     WHERE operation_id = ? AND status = ?
                    """,
                    (OPERATION_SUCCEEDED, now, operation_id, OPERATION_PROCESSING),
                )
                if cursor.rowcount != 1:
                    raise DesignOperationStateError(f"操作状态已变：{operation_id}")

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return {
            "kind": COMMIT_KIND_COMMITTED,
            "operationId": operation_id,
            "sessionId": operation.session_id,
            "planId": data["planId"],
            "revision": data["planRevision"],
            "designPhase": DESIGN_PHASE_DRAFT_READY,
            "plan": data,
            "template": data,
            "templateId": data["id"],
            "templateRevision": template_revision,
            "definitionDraft": draft_data,
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

    # ---------- 阶段复位 ----------

    def reset_design_phase(
        self, session_id: str, write_turn: WriteTurn | None = None
    ) -> PhaseResetResult:
        """把设计阶段复位回 ``PLAN_NEEDED``（工单 #44）。

        只改阶段：当前成功方案（``plan_id`` / ``plan_revision`` /
        ``plan_json``）**原样保留**。清空方案等于删历史，而且会把
        版本号压回 1 与旧方案撞版本；保留之后新一轮沿用同一 id、
        版本号 +1，方案血缘不断。

        幂等：本来就已经是 ``PLAN_NEEDED`` 时再复位一次，结果相同，
        不报错、不动方案、也不动 ``updated_at``。
        """
        if not session_id:
            raise ValueError("session_id 不能为空")

        with self._lock:
            conn = self._conn
            self._begin(conn)
            try:
                state = self._fetch_design_state(conn, session_id)
                now = _now()
                if state is None:
                    # 会话还没进过设计流程：记下「需要一份方案」。
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO report_design_sessions
                            (session_id, design_phase, plan_id, plan_revision,
                             plan_json, created_at, updated_at)
                        VALUES (?, ?, NULL, NULL, NULL, ?, ?)
                        """,
                        (session_id, DESIGN_PHASE_PLAN_NEEDED, now, now),
                    )
                    result: PhaseResetResult = {
                        "sessionId": session_id,
                        "designPhase": DESIGN_PHASE_PLAN_NEEDED,
                        "planId": None,
                        "planRevision": None,
                        "changed": True,
                    }
                elif state.phase == DESIGN_PHASE_PLAN_NEEDED:
                    # 幂等：已经是「需要生成方案」，什么都不用改。
                    result = {
                        "sessionId": session_id,
                        "designPhase": DESIGN_PHASE_PLAN_NEEDED,
                        "planId": state.plan_id,
                        "planRevision": state.revision,
                        "changed": False,
                    }
                else:
                    conn.execute(
                        "UPDATE report_design_sessions"
                        " SET design_phase = ?, updated_at = ?"
                        " WHERE session_id = ?",
                        (DESIGN_PHASE_PLAN_NEEDED, now, session_id),
                    )
                    result = {
                        "sessionId": session_id,
                        "designPhase": DESIGN_PHASE_PLAN_NEEDED,
                        "planId": state.plan_id,
                        "planRevision": state.revision,
                        "changed": True,
                    }
                if write_turn is not None:
                    write_turn(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        logger.info(
            "[report_design] 设计阶段已复位 session=%s phase=%s plan=%s rev=%s changed=%s",
            session_id,
            result["designPhase"],
            result["planId"],
            result["planRevision"],
            result["changed"],
        )
        return result

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
    def _validate_template(template: Mapping[str, Any]) -> dict[str, Any]:
        """校验模板是可保存的 JSON 形态。

        领域语义（区域与方案的对应、占位与数据的比例）由模板服务保证，
        这里只守结构与版本绑定。
        """
        if not isinstance(template, Mapping):
            raise DesignPlanInvalidError("模板必须是 JSON mapping")
        data = dict(template)
        template_id = data.get("id")
        if not isinstance(template_id, str) or not template_id.strip():
            raise DesignPlanInvalidError("模板缺少 id")
        revision = data.get("revision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise DesignPlanInvalidError("模板 revision 必须是正整数")
        plan_id = data.get("planId")
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise DesignPlanInvalidError("模板缺少 planId")
        plan_revision = data.get("planRevision")
        if (
            isinstance(plan_revision, bool)
            or not isinstance(plan_revision, int)
            or plan_revision < 1
        ):
            raise DesignPlanInvalidError("模板 planRevision 必须是正整数")
        regions = data.get("regions")
        if not isinstance(regions, list) or not regions:
            raise DesignPlanInvalidError("模板至少要有一个区域")
        try:
            json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise DesignPlanInvalidError(f"模板不是可保存的 JSON：{exc}") from exc
        return data

    @staticmethod
    def _fetch_template(
        conn: sqlite3.Connection, session_id: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT template_json FROM report_design_templates WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return _load_json(row[0])

    @staticmethod
    def _fetch_definition(
        conn: sqlite3.Connection, session_id: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT definition_json FROM report_design_definitions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return _load_json(row[0])

    @staticmethod
    def _fetch_confirmation(
        conn: sqlite3.Connection,
        session_id: str,
        plan_id: str,
        plan_revision: int,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT session_id, plan_id, plan_revision, template_id, created_at"
            " FROM report_design_confirmations"
            " WHERE session_id = ? AND plan_id = ? AND plan_revision = ?",
            (session_id, plan_id, plan_revision),
        ).fetchone()
        if row is None:
            return None
        return {
            "sessionId": row[0],
            "planId": row[1],
            "planRevision": row[2],
            "templateId": row[3],
            "createdAt": row[4],
        }

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
    def _mark_succeeded(
        conn: sqlite3.Connection, operation_id: str, now: str
    ) -> None:
        """把操作标记为成功。

        幂等分支用：这一版方案已经确认过，不重复生成，但操作不能停在
        PROCESSING —— 那会让这条消息永远显示「处理中」。
        确认操作**不**在操作表上记方案版本：那组键留给方案提交，
        确认的方案版本记在确认关系表里。
        """
        conn.execute(
            "UPDATE report_design_operations"
            " SET status = ?, plan_id = NULL, plan_revision = NULL,"
            " updated_at = ?"
            " WHERE operation_id = ? AND status = ?",
            (OPERATION_SUCCEEDED, now, operation_id, OPERATION_PROCESSING),
        )

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
