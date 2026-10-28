"""设计持久化模块（#19 首波 C）单测。

被测边界只有 C 自己的公开接口（冻结契约 §C）：
    initialize_schema / load / claim_operation / commit_plan / fail_operation

领域语义由 A 的 `apply_candidate` 校验，模型适配在 B，路由接线在协调者，
这三层都不在这里出现。测试只保证 C 承诺的事：
    - 只新增 `report_design_` 前缀的表，不动旧 schema；
    - 认领与提交都核对基础版本，重复操作能区分处理中 / 成功 / 失败；
    - 设计对象、阶段、操作结果与协调者注入的 write_turn 回调同事务提交；
    - 回调失败整体回滚，上一成功方案保留；
    - 加载与重开恢复同一成功方案。
"""
from __future__ import annotations

import logging
import sqlite3
import threading

import pytest

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
    DesignPlanInvalidError,
)
from app.storage.report_design_sqlite import SqliteReportDesignRepository
from app.storage.sqlite_store import SqliteSessionRepository


# ---------- 测试装置 ----------

# 契约里的共同样例：不是生产数据，只是可保存的方案形状。
PLAN_V1 = {
    "id": "plan_example_pue",
    "revision": 1,
    "topic": "PUE 专题报表",
    "requirements": {
        "metrics": ["PUE"],
        "subject": None,
        "timeRange": None,
        "exclusions": ["设备耗电排名"],
    },
    "sections": [
        {
            "id": "section_pue",
            "title": "PUE 总览",
            "metricIntents": ["PUE"],
            "componentType": "metric_card",
        }
    ],
    "assumptions": ["PUE 统计口径待核实，数据待连接"],
    "suggestions": [],
    "referenceTemplates": [],
}

# 修改后的方案：同一个 ID，revision 递增。
PLAN_V2 = {**PLAN_V1, "revision": 2}
PLAN_V3 = {**PLAN_V1, "revision": 3}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "design.db"


@pytest.fixture()
def conn(db_path):
    """连接由调用方注入：C 不建全局库，也不另造锁。"""
    session_repo = SqliteSessionRepository(db_path)
    connection = session_repo.conn
    yield connection
    connection.close()


@pytest.fixture()
def lock():
    return threading.RLock()


@pytest.fixture()
def store(conn, lock):
    repo = SqliteReportDesignRepository(connection=conn, lock=lock)
    repo.initialize_schema()
    return repo


def _write_turn(session_id: str, content: str = "方案已生成，等待确认"):
    """协调者注入的同事务写入回调。

    只用传入连接直接写 SQL，**不自行 commit**，也不调用会自行 commit 的旧
    Repository 方法 —— 否则同事务提交就不成立了。
    """

    def _callback(connection: sqlite3.Connection) -> None:
        now = "2026-10-28T00:00:00"
        connection.execute(
            "INSERT OR IGNORE INTO sessions (id, title, state, message_count,"
            " created_at, updated_at) VALUES (?, '新对话', 'ANALYZING', 0, ?, ?)",
            (session_id, now, now),
        )
        next_seq = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO messages (session_id, seq, role, content, created_at)"
            " VALUES (?, ?, 'assistant', ?, ?)",
            (session_id, next_seq, content, now),
        )

    return _callback


def _message_count(conn: sqlite3.Connection, session_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
    ).fetchone()[0]


def _failing_write_turn(session_id: str):
    """先写一行消息再抛错：用来证明回调的写入也在这笔事务里。"""

    def _callback(connection: sqlite3.Connection) -> None:
        _write_turn(session_id)(connection)
        raise RuntimeError("消息写入失败")

    return _callback


# ---------- 切片 1：建表、认领与阶段 ----------


def test_claim_new_plan_records_plan_needed_phase(store):
    """新建请求被认领后，会话进入 PLAN_NEEDED，还没有方案。"""
    result = store.claim_operation("sess_1", "msg_1", None)

    assert result["kind"] == CLAIM_KIND_CLAIMED
    assert result["operationId"]

    state = store.load("sess_1")
    assert state is not None
    assert state["designPhase"] == DESIGN_PHASE_PLAN_NEEDED
    assert state["reportPlan"] is None


def test_claim_leaves_no_open_transaction(store, conn):
    """认领后连接上没有挂着的写事务，模型工作在事务外进行。"""
    store.claim_operation("sess_1", "msg_1", None)

    assert not conn.in_transaction


def test_load_returns_none_for_unknown_session(store):
    """没进过设计流程的会话读不到设计状态。"""
    assert store.load("sess_unknown") is None


# ---------- 切片 2：提交新方案 ----------


def test_commit_new_plan_then_load_returns_waiting_confirm(store):
    """提交成功后，阶段推进到待确认，方案能原样读回来。"""
    claim = store.claim_operation("sess_1", "msg_1", None)

    result = store.commit_plan(
        claim["operationId"], None, PLAN_V1, _write_turn("sess_1")
    )

    assert result["kind"] == COMMIT_KIND_COMMITTED
    assert result["revision"] == 1
    assert result["designPhase"] == DESIGN_PHASE_PLAN_WAITING_CONFIRM

    state = store.load("sess_1")
    assert state is not None
    assert state["designPhase"] == DESIGN_PHASE_PLAN_WAITING_CONFIRM
    assert state["reportPlan"] == PLAN_V1


def test_commit_writes_message_and_design_in_one_transaction(store, conn):
    """聊天消息与设计对象在同一笔事务里落库，不只是 JSON 往返。"""
    claim = store.claim_operation("sess_1", "msg_1", None)

    store.commit_plan(claim["operationId"], None, PLAN_V1, _write_turn("sess_1"))

    assert _message_count(conn, "sess_1") == 1
    assert store.load("sess_1")["reportPlan"]["id"] == "plan_example_pue"


# ---------- 切片 3：修改方案与回调失败回滚 ----------


def test_update_plan_creates_new_revision(store):
    """修改方案在原方案 ID 上把 revision 递增一位。"""
    first = store.claim_operation("sess_1", "msg_1", None)
    store.commit_plan(first["operationId"], None, PLAN_V1, _write_turn("sess_1"))

    second = store.claim_operation("sess_1", "msg_2", 1)
    result = store.commit_plan(
        second["operationId"], 1, PLAN_V2, _write_turn("sess_1")
    )

    assert result["kind"] == COMMIT_KIND_COMMITTED
    assert result["revision"] == 2
    state = store.load("sess_1")
    assert state["reportPlan"]["id"] == "plan_example_pue"
    assert state["reportPlan"]["revision"] == 2


def test_failed_commit_rolls_back_design_and_message_together(store, conn):
    """回调失败整体回滚：消息没落库，设计对象也没落库。"""
    claim = store.claim_operation("sess_1", "msg_1", None)

    with pytest.raises(RuntimeError):
        store.commit_plan(
            claim["operationId"], None, PLAN_V1, _failing_write_turn("sess_1")
        )

    # 回调写的那行消息不在了：证明它和设计对象在同一笔事务里。
    assert _message_count(conn, "sess_1") == 0
    state = store.load("sess_1")
    assert state["designPhase"] == DESIGN_PHASE_PLAN_NEEDED
    assert state["reportPlan"] is None


def test_failed_commit_keeps_previous_successful_plan(store, conn):
    """提交失败不留半截：上一成功方案原样保留。"""
    first = store.claim_operation("sess_1", "msg_1", None)
    store.commit_plan(first["operationId"], None, PLAN_V1, _write_turn("sess_1"))

    second = store.claim_operation("sess_1", "msg_2", 1)
    with pytest.raises(RuntimeError):
        store.commit_plan(
            second["operationId"], 1, PLAN_V2, _failing_write_turn("sess_1")
        )

    assert _message_count(conn, "sess_1") == 1
    state = store.load("sess_1")
    assert state["reportPlan"] == PLAN_V1
    assert state["designPhase"] == DESIGN_PHASE_PLAN_WAITING_CONFIRM


# ---------- 切片 4：基础版本冲突与方案校验 ----------


def test_claim_conflicts_when_base_revision_stale(store):
    """基础版本对不上就认领不了，并把当前成功方案带回去。"""
    first = store.claim_operation("sess_1", "msg_1", None)
    store.commit_plan(first["operationId"], None, PLAN_V1, _write_turn("sess_1"))

    result = store.claim_operation("sess_1", "msg_2", 5)

    assert result["kind"] == CLAIM_KIND_CONFLICT
    assert result["operationId"] is None
    assert result["currentRevision"] == 1
    assert result["currentPlan"] == PLAN_V1


def test_claim_conflicts_when_plan_exists_but_new_plan_expected(store):
    """已经有成功方案时，不能再按新建来认领。"""
    first = store.claim_operation("sess_1", "msg_1", None)
    store.commit_plan(first["operationId"], None, PLAN_V1, _write_turn("sess_1"))

    result = store.claim_operation("sess_1", "msg_2", None)

    assert result["kind"] == CLAIM_KIND_CONFLICT
    assert result["currentRevision"] == 1


def test_commit_conflicts_when_revision_moved_and_keeps_winner(store, conn):
    """并发修改获胜后，旧生成结果不能提交覆盖。"""
    first = store.claim_operation("sess_1", "msg_1", None)
    store.commit_plan(first["operationId"], None, PLAN_V1, _write_turn("sess_1"))

    stale = store.claim_operation("sess_1", "msg_2", 1)
    winner = store.claim_operation("sess_1", "msg_3", 1)
    store.commit_plan(winner["operationId"], 1, PLAN_V2, _write_turn("sess_1"))

    result = store.commit_plan(
        stale["operationId"], 1, PLAN_V3, _write_turn("sess_1")
    )

    assert result["kind"] == COMMIT_KIND_CONFLICT
    assert result["currentRevision"] == 2
    assert result["currentPlan"] == PLAN_V2
    # 冲突时不写消息、不改方案：胜出的版本原样保留。
    assert _message_count(conn, "sess_1") == 2
    assert store.load("sess_1")["reportPlan"] == PLAN_V2


def test_commit_rejects_plan_without_id(store):
    """没有 id 的方案不落库，上一成功方案不受影响。"""
    claim = store.claim_operation("sess_1", "msg_1", None)

    with pytest.raises(DesignPlanInvalidError):
        store.commit_plan(
            claim["operationId"], None, {"revision": 1}, _write_turn("sess_1")
        )

    assert store.load("sess_1")["reportPlan"] is None


def test_commit_rejects_plan_that_ignores_base_revision(store):
    """方案版本必须接在基础版本上，不能跳版本。"""
    claim = store.claim_operation("sess_1", "msg_1", None)

    with pytest.raises(DesignPlanInvalidError):
        store.commit_plan(
            claim["operationId"], None, PLAN_V2, _write_turn("sess_1")
        )

    assert store.load("sess_1")["reportPlan"] is None


# ---------- 切片 5：重复操作与失败 ----------


def test_duplicate_claim_while_processing(store):
    """同一条消息重复到达时，正在处理中的那次不算新操作。"""
    first = store.claim_operation("sess_1", "msg_1", None)

    again = store.claim_operation("sess_1", "msg_1", None)

    assert again["kind"] == CLAIM_KIND_DUPLICATE
    assert again["status"] == OPERATION_PROCESSING
    assert again["operationId"] == first["operationId"]


def test_claim_for_update_reports_current_plan(store):
    """修改方案的认领要带回当前成功方案，调用方据此构造候选。"""
    first = store.claim_operation("sess_1", "msg_1", None)
    store.commit_plan(first["operationId"], None, PLAN_V1, _write_turn("sess_1"))

    second = store.claim_operation("sess_1", "msg_2", 1)

    assert second["kind"] == CLAIM_KIND_CLAIMED
    assert second["designPhase"] == DESIGN_PHASE_PLAN_WAITING_CONFIRM
    assert second["currentPlan"] == PLAN_V1
    assert second["currentRevision"] == 1


def test_duplicate_claim_after_success_returns_committed_plan(store):
    """同一条消息重试时，返回那次已经提交成功的方案。"""
    claim = store.claim_operation("sess_1", "msg_1", None)
    store.commit_plan(claim["operationId"], None, PLAN_V1, _write_turn("sess_1"))

    again = store.claim_operation("sess_1", "msg_1", None)

    assert again["kind"] == CLAIM_KIND_DUPLICATE
    assert again["status"] == OPERATION_SUCCEEDED
    assert again["plan"] == PLAN_V1
    assert again["planRevision"] == 1


def test_duplicate_claim_after_failure(store):
    """失败过的操作，重复到达时仍然认不出来做。"""
    claim = store.claim_operation("sess_1", "msg_1", None)
    store.fail_operation(
        claim["operationId"], {"code": "model_timeout", "message": "模型调用超时"}
    )

    again = store.claim_operation("sess_1", "msg_1", None)

    assert again["kind"] == CLAIM_KIND_DUPLICATE
    assert again["status"] == OPERATION_FAILED
    assert again["error"]["code"] == "model_timeout"


def test_fail_operation_keeps_previous_successful_plan(store):
    """操作失败后，上一成功方案和阶段都保留。"""
    first = store.claim_operation("sess_1", "msg_1", None)
    store.commit_plan(first["operationId"], None, PLAN_V1, _write_turn("sess_1"))

    second = store.claim_operation("sess_1", "msg_2", 1)
    store.fail_operation(second["operationId"], "模型调用超时")

    state = store.load("sess_1")
    assert state["reportPlan"] == PLAN_V1
    assert state["designPhase"] == DESIGN_PHASE_PLAN_WAITING_CONFIRM


def test_commit_replay_does_not_write_message_twice(store, conn):
    """重复提交同一个操作不会再写一遍消息。"""
    claim = store.claim_operation("sess_1", "msg_1", None)
    store.commit_plan(claim["operationId"], None, PLAN_V1, _write_turn("sess_1"))

    again = store.commit_plan(
        claim["operationId"], None, PLAN_V1, _write_turn("sess_1")
    )

    assert again["kind"] == COMMIT_KIND_COMMITTED
    assert again["alreadyCommitted"] is True
    assert _message_count(conn, "sess_1") == 1


def test_conflicted_operation_is_not_left_processing(store):
    """冲突判负的操作记为失败，不会一直挂在处理中。"""
    first = store.claim_operation("sess_1", "msg_1", None)
    store.commit_plan(first["operationId"], None, PLAN_V1, _write_turn("sess_1"))

    stale = store.claim_operation("sess_1", "msg_2", 1)
    winner = store.claim_operation("sess_1", "msg_3", 1)
    store.commit_plan(winner["operationId"], 1, PLAN_V2, _write_turn("sess_1"))
    store.commit_plan(stale["operationId"], 1, PLAN_V3, _write_turn("sess_1"))

    again = store.claim_operation("sess_1", "msg_2", 1)
    assert again["kind"] == CLAIM_KIND_DUPLICATE
    assert again["status"] == OPERATION_FAILED


# ---------- 切片 6：重开恢复与旧 schema 隔离 ----------


def test_reopen_recovers_same_committed_plan(store, conn, db_path):
    """进程重启后恢复的是同一份成功方案。"""
    claim = store.claim_operation("sess_1", "msg_1", None)
    store.commit_plan(claim["operationId"], None, PLAN_V1, _write_turn("sess_1"))

    conn.close()
    reopened = sqlite3.connect(str(db_path))
    try:
        repo = SqliteReportDesignRepository(
            connection=reopened, lock=threading.RLock()
        )
        repo.initialize_schema()
        state = repo.load("sess_1")
        assert state is not None
        assert state["designPhase"] == DESIGN_PHASE_PLAN_WAITING_CONFIRM
        assert state["reportPlan"] == PLAN_V1
    finally:
        reopened.close()


def test_constructor_does_not_touch_schema(conn):
    """构造不建表、不迁移：旧存储的迁移不由本模块触发。"""
    before = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }

    SqliteReportDesignRepository(connection=conn, lock=threading.RLock())

    after = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert after == before


def test_initialize_schema_only_creates_report_design_tables(conn):
    """建表只加 report_design_ 前缀的表，旧 schema 一个字节都不改。"""
    before = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
        )
    }

    repo = SqliteReportDesignRepository(connection=conn, lock=threading.RLock())
    repo.initialize_schema()

    after = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
        )
    }
    added = set(after) - set(before)
    assert added, "应当新建设计表"
    assert all(name.startswith("report_design_") for name in added)
    for name, sql in before.items():
        assert after[name] == sql, f"旧表 {name} 不该被改动"


def test_write_turn_that_commits_early_is_reported(store, caplog):
    """回调自己 commit 会破坏同事务提交：模块要能发现并告警。"""

    def _committing_callback(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO sessions (id, title, state, message_count,"
            " created_at, updated_at) VALUES ('sess_1', '新对话', 'ANALYZING', 0,"
            " '2026-10-28T00:00:00', '2026-10-28T00:00:00')"
        )
        connection.commit()  # 违约：回调自行结束事务

    claim = store.claim_operation("sess_1", "msg_1", None)
    with caplog.at_level(logging.WARNING, logger="app.storage.report_design_sqlite"):
        store.commit_plan(
            claim["operationId"], None, PLAN_V1, _committing_callback
        )

    assert any("write_turn" in record.message for record in caplog.records)


# ---------- 切片 7：确认去重的唯一约束 ----------


def _insert_operation(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    session_id: str,
    message_id: str,
    status: str,
    plan_id: str | None = None,
    plan_revision: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO report_design_operations (operation_id, session_id, message_id,"
        " expected_revision, status, plan_id, plan_revision, created_at, updated_at)"
        " VALUES (?, ?, ?, 1, ?, ?, ?, '2026-10-28T00:00:00', '2026-10-28T00:00:00')",
        (operation_id, session_id, message_id, status, plan_id, plan_revision),
    )


def test_successful_operation_is_unique_per_plan_revision(conn):
    """同一会话的同一方案版本只允许一条成功操作。

    这是「重复确认只产生一个已提交对象」的底座。确认动作属于工单 02，
    首波还没有它的公开接口，所以这条唯一约束只能直接在存储层验证。
    """
    repo = SqliteReportDesignRepository(connection=conn, lock=threading.RLock())
    repo.initialize_schema()

    _insert_operation(
        conn,
        operation_id="rdop_a",
        session_id="sess_1",
        message_id="msg_a",
        status="SUCCEEDED",
        plan_id="plan_x",
        plan_revision=1,
    )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_operation(
            conn,
            operation_id="rdop_b",
            session_id="sess_1",
            message_id="msg_b",
            status="SUCCEEDED",
            plan_id="plan_x",
            plan_revision=1,
        )

    # 处理中和失败的行不受这条约束限制：失败的那次不算已提交。
    _insert_operation(
        conn,
        operation_id="rdop_c",
        session_id="sess_1",
        message_id="msg_c",
        status="PROCESSING",
        plan_id="plan_x",
        plan_revision=1,
    )
    _insert_operation(
        conn,
        operation_id="rdop_d",
        session_id="sess_1",
        message_id="msg_d",
        status="FAILED",
        plan_id="plan_x",
        plan_revision=1,
    )
