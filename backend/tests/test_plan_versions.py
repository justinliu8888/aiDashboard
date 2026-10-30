"""工单 #63：方案版本记录 —— 服务端保存每一版正文与操作标识。

这里测的是**外部可见的行为**，不是内部实现：

- 用户提交两次方案后，重新打开会话能看到**两版**，每版正文是当时那一份，
  旧版正文不被改写；
- **澄清与失败不产生版本**（问一句不算提交，出错更不算）；
- 同一份重复响应、乱序响应、重新连接都**不重复追加**，也不夺取确认资格；
- 版本序列的顺序就是真实保存顺序；
- 老会话没有版本历史时，按**实际存储**恢复可用方案，**不伪造**过去的版本；
- 版本记录里**没有**模拟图表载荷与业务数值 —— 只有纯方案结构。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from app.services.plan_version_service import PlanVersionService
from app.storage.report_design_repository import (
    CLAIM_KIND_CLAIMED,
    CLAIM_KIND_CONFLICT,
    CLAIM_KIND_DUPLICATE,
    COMMIT_KIND_COMMITTED,
    COMMIT_KIND_CONFLICT,
    OPERATION_SUCCEEDED,
)
from app.storage.report_design_sqlite import SqliteReportDesignRepository
from app.storage.sqlite_store import SqliteSessionRepository


# ---------- 夹具 ----------

# 契约里的共同样例：不是生产数据，只是可保存的方案形状。
PLAN_V1 = {
    "id": "rplan_example_pue",
    "revision": 1,
    "topic": "PUE 专题报表",
    "requirements": {
        "metrics": ["PUE"],
        "subject": None,
        "timeRange": None,
        "exclusions": [],
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

# 第二版：同一个方案 ID，版本 +1，正文多一块板块。
PLAN_V2 = {
    **PLAN_V1,
    "revision": 2,
    "sections": [
        *PLAN_V1["sections"],
        {
            "id": "section_trend",
            "title": "PUE 趋势",
            "metricIntents": ["PUE"],
            "componentType": "line_chart",
        },
    ],
}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "design.db"


@pytest.fixture()
def conn(db_path):
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


@pytest.fixture()
def versions(store):
    """版本序列的读取方（与设计存储同一条连接）。"""
    return PlanVersionService(repository=store)


def _write_turn(session_id: str, content: str = "方案已生成，等待确认"):
    """协调者注入的同事务写入回调（不自行 commit）。"""

    def _callback(connection: sqlite3.Connection) -> None:
        now = "2026-10-30T00:00:00"
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


def _commit(store, session_id: str, message_id: str, plan: dict[str, Any], expected=None):
    """走一条完整的认领 → 提交路径，返回提交结果。"""
    claim = store.claim_operation(session_id, message_id, expected)
    assert claim["kind"] == CLAIM_KIND_CLAIMED
    return store.commit_plan(
        claim["operationId"], expected, plan, _write_turn(session_id)
    )


# --------------------------------------------------------------------------
# 1. 连续提交两次 → 两版，旧版正文不被改写
# --------------------------------------------------------------------------


def test_two_submissions_produce_two_versions(store, versions):
    """提交两次方案后，会话里有两版，版本号递增，第一版正文原样保留。"""
    _commit(store, "sess_1", "msg_1", PLAN_V1)

    _commit(store, "sess_1", "msg_2", PLAN_V2, expected=1)

    records = versions.list_versions("sess_1")
    assert len(records) == 2
    assert [item["planRevision"] for item in records] == [1, 2]
    assert {item["planId"] for item in records} == {PLAN_V1["id"]}
    # 第一版正文没有被第二版改写
    assert records[0]["reportPlan"] == PLAN_V1
    assert records[1]["reportPlan"] == PLAN_V2
    # 每一版都有自己的稳定 ID
    assert records[0]["versionId"] != records[1]["versionId"]
    assert all(item["versionId"].startswith("planver_") for item in records)


def test_old_body_survives_overwrite(store, versions):
    """第二版写进当前方案后，第一版正文仍能单独查回来（不是整份覆盖）。"""
    _commit(store, "sess_1", "msg_1", PLAN_V1)
    _commit(store, "sess_1", "msg_2", PLAN_V2, expected=1)

    # 当前方案只剩最新一版
    current = store.load("sess_1")["reportPlan"]
    assert current["revision"] == 2
    assert current["sections"][0]["id"] == "section_pue"

    # 但历史那一版还在版本序列里，正文一字未改
    history = versions.list_versions("sess_1")[0]
    assert history["planRevision"] == 1
    assert history["reportPlan"]["sections"] == PLAN_V1["sections"]
    assert history["isCurrent"] is False


def test_each_version_carries_its_operation_id(store, versions):
    """每一版都记着是哪次操作提交上来的（操作标识）。"""
    claim_one = store.claim_operation("sess_1", "msg_1", None)
    store.commit_plan(claim_one["operationId"], None, PLAN_V1, _write_turn("sess_1"))
    claim_two = store.claim_operation("sess_1", "msg_2", 1)
    store.commit_plan(
        claim_two["operationId"], 1, PLAN_V2, _write_turn("sess_1")
    )

    records = versions.list_versions("sess_1")
    assert records[0]["operationId"] == claim_one["operationId"]
    assert records[1]["operationId"] == claim_two["operationId"]


def test_sequence_matches_real_save_order(store, versions):
    """版本序列的顺序就是真实保存顺序。"""
    _commit(store, "sess_1", "msg_1", PLAN_V1)
    _commit(store, "sess_1", "msg_2", PLAN_V2, expected=1)

    records = versions.list_versions("sess_1")
    assert [item["sequence"] for item in records] == [1, 2]
    assert records[-1]["isCurrent"] is True
    assert records[0]["isCurrent"] is False


# --------------------------------------------------------------------------
# 2. 澄清与失败不产生版本
# --------------------------------------------------------------------------


def test_clarification_does_not_create_version(store, versions):
    """澄清是问用户一句，不是提交：不产生版本，也不改当前方案。"""
    _commit(store, "sess_1", "msg_1", PLAN_V1)

    claim = store.claim_operation("sess_1", "msg_2", 1)
    store.commit_clarification(
        claim["operationId"], 1, "PUE 是按 IT 设备口径还是按全站口径？", _write_turn("sess_1")
    )

    records = versions.list_versions("sess_1")
    assert len(records) == 1
    assert records[0]["planRevision"] == 1
    # 当前方案没被澄清动过
    assert store.load("sess_1")["reportPlan"]["revision"] == 1


def test_failure_does_not_create_version(store, versions):
    """技术失败不产生版本，上一版方案原样保留。"""
    _commit(store, "sess_1", "msg_1", PLAN_V1)

    claim = store.claim_operation("sess_1", "msg_2", 1)
    store.fail_operation(
        claim["operationId"],
        {"code": "PLAN_MODEL_FAILED", "message": "方案生成失败，请重试。"},
    )

    records = versions.list_versions("sess_1")
    assert len(records) == 1
    assert records[0]["planRevision"] == 1
    assert store.load("sess_1")["reportPlan"]["revision"] == 1


def test_clarification_before_any_plan_leaves_no_version(store, versions):
    """连一版都没有时澄清，版本序列仍然是空的（不凭空造第 0 版）。"""
    claim = store.claim_operation("sess_1", "msg_1", None)
    store.commit_clarification(
        claim["operationId"], None, "要看哪个机房？", _write_turn("sess_1")
    )

    assert versions.list_versions("sess_1") == []


# --------------------------------------------------------------------------
# 3. 重复响应 / 乱序响应 / 重连：不重复追加、不夺取确认资格
# --------------------------------------------------------------------------


def test_duplicate_response_does_not_append_again(store, versions):
    """同一次提交被重复送达：只追加一次，确认资格不被夺走。"""
    claim = store.claim_operation("sess_1", "msg_1", None)
    first = store.commit_plan(
        claim["operationId"], None, PLAN_V1, _write_turn("sess_1")
    )
    assert first["kind"] == COMMIT_KIND_COMMITTED

    # 同一个操作再提交一次（重复响应）
    second = store.commit_plan(
        claim["operationId"], None, PLAN_V1, _write_turn("sess_1")
    )
    assert second["kind"] == COMMIT_KIND_COMMITTED
    assert second["alreadyCommitted"] is True

    records = versions.list_versions("sess_1")
    assert len(records) == 1
    assert records[0]["planRevision"] == 1
    assert records[0]["isCurrent"] is True


def test_duplicate_submission_keeps_confirmation_eligibility(store, versions):
    """重复响应不会改掉当前方案那一版的确认资格。"""
    claim = store.claim_operation("sess_1", "msg_1", None)
    store.commit_plan(claim["operationId"], None, PLAN_V1, _write_turn("sess_1"))

    # 重复响应：同一操作再走一遍
    store.commit_plan(claim["operationId"], None, PLAN_V1, _write_turn("sess_1"))

    # 当前方案没变，确认关系仍然指向同一版
    state = store.load("sess_1")
    assert state["reportPlan"]["revision"] == 1
    assert store.find_confirmation("sess_1", PLAN_V1["id"], 1) is None
    latest = versions.latest_version("sess_1")
    assert latest["planId"] == PLAN_V1["id"]
    assert latest["planRevision"] == 1


def test_reconnect_does_not_append_again(store, versions):
    """重连：同一条消息重新到达，被认成重复，不再产生新版本。"""
    _commit(store, "sess_1", "msg_1", PLAN_V1)

    again = store.claim_operation("sess_1", "msg_1", None)
    assert again["kind"] == CLAIM_KIND_DUPLICATE
    assert again["status"] == OPERATION_SUCCEEDED

    records = versions.list_versions("sess_1")
    assert len(records) == 1
    assert records[0]["planRevision"] == 1


def test_out_of_order_response_does_not_append(store, versions):
    """乱序响应：迟到的旧结果不追加版本，也不夺走当前方案。"""
    # 两条操作都基于「还没有方案」被认领
    early = store.claim_operation("sess_1", "msg_early", None)
    late = store.claim_operation("sess_1", "msg_late", None)
    assert early["kind"] == CLAIM_KIND_CLAIMED
    assert late["kind"] == CLAIM_KIND_CLAIMED

    # 后发起的先到，先落库
    store.commit_plan(late["operationId"], None, PLAN_V1, _write_turn("sess_1"))
    # 迟到的旧结果：基础版本已经变了 → 冲突，不提交、不追加
    conflict = store.commit_plan(
        early["operationId"], None, PLAN_V2, _write_turn("sess_1")
    )
    assert conflict["kind"] == COMMIT_KIND_CONFLICT

    records = versions.list_versions("sess_1")
    assert len(records) == 1
    assert records[0]["planRevision"] == 1
    assert records[0]["reportPlan"] == PLAN_V1
    assert store.load("sess_1")["reportPlan"]["revision"] == 1


def test_conflict_does_not_touch_confirmation_eligibility(store, versions):
    """乱序响应之后，先落库那一版仍然是当前方案、仍然可确认。"""
    early = store.claim_operation("sess_1", "msg_early", None)
    late = store.claim_operation("sess_1", "msg_late", None)
    store.commit_plan(late["operationId"], None, PLAN_V1, _write_turn("sess_1"))
    store.commit_plan(early["operationId"], None, PLAN_V2, _write_turn("sess_1"))

    latest = versions.latest_version("sess_1")
    assert latest["planId"] == PLAN_V1["id"]
    assert latest["planRevision"] == 1
    assert latest["isCurrent"] is True


def test_stale_expected_revision_is_rejected_at_claim(store, versions):
    """带着旧基础版本来认领：直接冲突，不会多出版本。"""
    _commit(store, "sess_1", "msg_1", PLAN_V1)

    stale = store.claim_operation("sess_1", "msg_stale", 99)
    assert stale["kind"] == CLAIM_KIND_CONFLICT

    assert len(versions.list_versions("sess_1")) == 1


# --------------------------------------------------------------------------
# 4. 老会话没有版本历史 → 按实际存储恢复，不伪造历史
# --------------------------------------------------------------------------


def test_legacy_session_recovers_only_the_stored_plan(store, versions, conn):
    """老会话（只有当前方案、没有版本历史）：恢复出这一版，不编造更多版本。"""
    # 造一条「老会话」：只有当前成功方案，没有版本记录
    conn.execute(
        "INSERT INTO report_design_sessions"
        " (session_id, design_phase, plan_id, plan_revision, plan_json,"
        "  created_at, updated_at)"
        " VALUES (?, 'PLAN_WAITING_CONFIRM', ?, ?, ?, '2026-10-20T00:00:00',"
        " '2026-10-20T00:00:00')",
        ("sess_legacy", PLAN_V1["id"], PLAN_V1["revision"], json.dumps(PLAN_V1)),
    )
    conn.commit()

    records = versions.list_versions("sess_legacy")
    assert len(records) == 1
    assert records[0]["planId"] == PLAN_V1["id"]
    assert records[0]["planRevision"] == 1
    assert records[0]["reportPlan"] == PLAN_V1
    assert records[0]["recovered"] is True
    assert records[0]["isCurrent"] is True
    # 没有凭空多出「第 2 版」「第 3 版」
    assert [item["planRevision"] for item in records] == [1]


def test_legacy_session_then_new_commit_keeps_both_versions(store, versions, conn):
    """老会话提交新方案后：旧版被补记下来，新版追加在后，顺序正确。"""
    conn.execute(
        "INSERT INTO report_design_sessions"
        " (session_id, design_phase, plan_id, plan_revision, plan_json,"
        "  created_at, updated_at)"
        " VALUES (?, 'PLAN_WAITING_CONFIRM', ?, ?, ?, '2026-10-20T00:00:00',"
        " '2026-10-20T00:00:00')",
        ("sess_legacy", PLAN_V1["id"], PLAN_V1["revision"], json.dumps(PLAN_V1)),
    )
    conn.commit()

    _commit(store, "sess_legacy", "msg_1", PLAN_V2, expected=1)

    records = versions.list_versions("sess_legacy")
    assert [item["planRevision"] for item in records] == [1, 2]
    assert records[0]["reportPlan"] == PLAN_V1
    assert records[1]["reportPlan"] == PLAN_V2
    assert records[-1]["isCurrent"] is True
    assert records[0]["isCurrent"] is False


def test_session_without_plan_has_no_versions(store, versions):
    """还没提交过方案的会话：版本序列为空。"""
    assert versions.list_versions("sess_empty") == []
    assert versions.latest_version("sess_empty") is None


# --------------------------------------------------------------------------
# 5. 版本记录里没有模拟图表载荷与业务数值
# --------------------------------------------------------------------------


def test_version_record_holds_pure_plan_structure(store, versions):
    """版本正文就是纯方案结构：没有模拟载荷，没有业务数字。"""
    _commit(store, "sess_1", "msg_1", PLAN_V1)

    record = versions.list_versions("sess_1")[0]
    body = record["reportPlan"]
    assert body == PLAN_V1

    # 模拟图表载荷的常见字段一个都不许出现
    forbidden = {
        "data",
        "series",
        "values",
        "rows",
        "cells",
        "payload",
        "chartData",
        "previewSpec",
        "reportRunPreview",
        "metrics",
    }
    assert forbidden.isdisjoint(body.keys())
    assert forbidden.isdisjoint(body["sections"][0].keys())
    # 指标位置写的是「意图」，不是实测值
    assert body["sections"][0]["metricIntents"] == ["PUE"]
    assert body["requirements"]["metrics"] == ["PUE"]
    # 整条记录里没有任何带业务单位的数字
    raw = json.dumps(record, ensure_ascii=False)
    assert "kWh" not in raw and "kwh" not in raw
    assert "%" not in raw


def test_version_record_does_not_contain_response_payload(store, versions):
    """版本记录只存方案本身，不把响应载荷整份塞进来。"""
    _commit(store, "sess_1", "msg_1", PLAN_V1)

    record = versions.list_versions("sess_1")[0]
    assert set(record.keys()) == {
        "versionId",
        "sessionId",
        "planId",
        "planRevision",
        "operationId",
        "sequence",
        "reportPlan",
        "createdAt",
        "recovered",
        "isCurrent",
        "confirmed",
    }


def test_version_reference_carries_identity_only(store, versions):
    """版本引用只带身份，不带正文。"""
    _commit(store, "sess_1", "msg_1", PLAN_V1)

    reference = versions.version_reference(versions.list_versions("sess_1")[0])
    assert reference == {
        "versionId": versions.list_versions("sess_1")[0]["versionId"],
        "planId": PLAN_V1["id"],
        "planRevision": 1,
    }
    assert "reportPlan" not in reference


# --------------------------------------------------------------------------
# 6. 既有确认边界不变
# --------------------------------------------------------------------------


def test_confirmation_boundary_is_unchanged(store, versions):
    """版本记录不参与确认：确认关系仍按 (会话, 方案, 版本) 唯一。"""
    _commit(store, "sess_1", "msg_1", PLAN_V1)

    # 确认关系是既有边界的一部分：老会话没有这一版 → 查不到
    assert store.find_confirmation("sess_1", PLAN_V1["id"], 1) is None
    record = versions.list_versions("sess_1")[0]
    assert record["confirmed"] is False


def test_confirmed_version_is_marked_without_rewriting_body(store, versions, conn):
    """确认之后只更新标记，旧版正文不被改写。"""
    _commit(store, "sess_1", "msg_1", PLAN_V1)
    conn.execute(
        "INSERT INTO report_design_confirmations"
        " (session_id, plan_id, plan_revision, template_id, created_at)"
        " VALUES (?, ?, ?, ?, '2026-10-30T00:00:00')",
        ("sess_1", PLAN_V1["id"], 1, "tpl_1"),
    )
    conn.commit()

    record = versions.list_versions("sess_1")[0]
    assert record["confirmed"] is True
    assert record["reportPlan"] == PLAN_V1
