"""会话存储层单测。"""
from __future__ import annotations

import pytest

from app.storage.sqlite_store import SqliteSessionRepository


@pytest.fixture()
def repo(tmp_path) -> SqliteSessionRepository:
    return SqliteSessionRepository(tmp_path / "store.db")


# ---------- 项目 ----------


def test_create_and_list_workspace(repo):
    created = repo.create_workspace(id="ws_1", name="智能报表原型")
    assert created.name == "智能报表原型"

    listed = repo.list_workspaces()
    assert [w.id for w in listed] == ["ws_1"]
    assert listed[0].sessionCount == 0


def test_update_and_delete_workspace(repo):
    repo.create_workspace(id="ws_1", name="A", description="旧描述")
    updated = repo.update_workspace("ws_1", name="B", description="新描述")
    assert updated is not None
    assert updated.name == "B"
    assert updated.description == "新描述"

    assert repo.delete_workspace("ws_1") is True
    assert repo.delete_workspace("ws_1") is False
    assert repo.list_workspaces() == []


def test_delete_workspace_keeps_sessions(repo):
    repo.create_workspace(id="ws_1", name="A")
    repo.upsert_session(session_id="sess_1", state="ANALYZING", workspaceId="ws_1")

    repo.delete_workspace("ws_1")

    session = repo.get_session("sess_1")
    assert session is not None
    assert session.workspaceId is None


# ---------- 会话 ----------


def test_upsert_session_creates_then_updates(repo):
    first = repo.upsert_session(session_id="sess_1", state="ANALYZING")
    assert first.title == "新对话"
    assert first.state == "ANALYZING"

    second = repo.upsert_session(session_id="sess_1", state="READY", title="改名了")
    assert second.state == "READY"
    assert second.title == "改名了"
    assert second.createdAt == first.createdAt


def test_list_sessions_filters_empty_and_sorts_by_updated(repo):
    repo.upsert_session(session_id="sess_empty", state="ANALYZING")
    repo.upsert_session(session_id="sess_a", state="ANALYZING", title="A")
    repo.append_message(session_id="sess_a", role="user", content="hi")
    repo.upsert_session(session_id="sess_a", state="ANALYZING", messageCount=1)
    repo.upsert_session(session_id="sess_b", state="ANALYZING", title="B")
    repo.append_message(session_id="sess_b", role="user", content="hi")
    repo.upsert_session(session_id="sess_b", state="ANALYZING", messageCount=1)

    items, total = repo.list_sessions()
    assert total == 2
    assert {s.id for s in items} == {"sess_a", "sess_b"}


def test_list_sessions_filters_by_workspace_and_keyword(repo):
    repo.create_workspace(id="ws_1", name="A")
    repo.upsert_session(session_id="sess_a", state="ANALYZING", title="能耗日报", workspaceId="ws_1")
    repo.append_message(session_id="sess_a", role="user", content="hi")
    repo.upsert_session(session_id="sess_a", state="ANALYZING", messageCount=1)
    repo.upsert_session(session_id="sess_b", state="ANALYZING", title="值班表")
    repo.append_message(session_id="sess_b", role="user", content="hi")
    repo.upsert_session(session_id="sess_b", state="ANALYZING", messageCount=1)

    by_ws, total = repo.list_sessions(workspaceId="ws_1")
    assert total == 1
    assert by_ws[0].id == "sess_a"

    by_kw, total = repo.list_sessions(keyword="值班")
    assert total == 1
    assert by_kw[0].id == "sess_b"


def test_delete_session_is_soft(repo):
    repo.upsert_session(session_id="sess_1", state="ANALYZING")
    assert repo.delete_session("sess_1") is True
    assert repo.get_session("sess_1") is None
    assert repo.delete_session("sess_1") is False


def test_upsert_session_recreates_soft_deleted_row(repo):
    """已软删的会话被再次写入时要重新建行，不能打在看不见的行上。

    否则 UPDATE 打在 deleted_at 非空的行上，回读 get_session 拿到 None，
    整轮落库被吞掉。
    """
    repo.upsert_session(session_id="sess_1", state="ANALYZING", title="旧标题")
    repo.delete_session("sess_1")

    record = repo.upsert_session(session_id="sess_1", state="READY", title="新标题")
    assert record is not None
    assert record.title == "新标题"
    assert repo.get_session("sess_1") is not None


# ---------- 消息 ----------


def test_append_message_assigns_sequential_seq(repo):
    repo.upsert_session(session_id="sess_1", state="ANALYZING")
    repo.append_message(session_id="sess_1", role="user", content="第一句")
    repo.append_message(session_id="sess_1", role="assistant", content="回复")

    messages = repo.list_messages("sess_1")
    assert [m.seq for m in messages] == [1, 2]
    assert [m.role for m in messages] == ["user", "assistant"]


def test_append_message_is_idempotent_by_client_msg_id(repo):
    repo.upsert_session(session_id="sess_1", state="ANALYZING")
    first = repo.append_message(
        session_id="sess_1", role="user", content="原消息", clientMsgId="m1"
    )
    second = repo.append_message(
        session_id="sess_1", role="user", content="原消息", clientMsgId="m1"
    )

    assert first.id == second.id
    assert len(repo.list_messages("sess_1")) == 1


def test_append_message_without_client_msg_id_always_appends(repo):
    repo.upsert_session(session_id="sess_1", state="ANALYZING")
    repo.append_message(session_id="sess_1", role="user", content="a")
    repo.append_message(session_id="sess_1", role="user", content="a")

    assert len(repo.list_messages("sess_1")) == 2


def test_message_payload_roundtrip(repo):
    repo.upsert_session(session_id="sess_1", state="ANALYZING")
    repo.append_message(
        session_id="sess_1",
        role="assistant",
        content="好的",
        payload={"questions": [{"id": "q1"}]},
    )

    stored = repo.list_messages("sess_1")[0]
    assert stored.payload == {"questions": [{"id": "q1"}]}


# ---------- 选择锁 ----------


def test_set_pending_choice_creates_row_if_missing(repo):
    """选择锁可能在会话行创建之前先写进来，不能悄悄丢掉。"""
    repo.set_pending_choice("sess_1", {"state": "READY", "question": {"id": "q1"}})

    record = repo.get_session("sess_1")
    assert record is not None
    assert record.pendingChoice == {"state": "READY", "question": {"id": "q1"}}


def test_set_pending_choice_overwrites_and_clears(repo):
    repo.upsert_session(session_id="sess_1", state="READY")
    repo.set_pending_choice("sess_1", {"state": "READY", "question": {"id": "q1"}})
    assert repo.get_session("sess_1").pendingChoice is not None

    repo.set_pending_choice("sess_1", None)
    assert repo.get_session("sess_1").pendingChoice is None


def test_pending_choice_survives_reopen(repo):
    """后端重启（重新打开同一个库文件）后选择锁还在。"""
    db = repo.db_path
    repo.set_pending_choice("sess_1", {"state": "READY", "question": {"id": "q1"}})
    repo.close()

    reopened = SqliteSessionRepository(db)
    record = reopened.get_session("sess_1")
    assert record is not None
    assert record.pendingChoice["question"]["id"] == "q1"


# ---------- 编辑态跨轮状态 ----------


def test_snapshot_roundtrips_edit_state_keys(repo):
    """待确认计划和澄清轮数存在 snapshot 里，不需要新表新列。"""
    repo.upsert_session(session_id="sess_1", state="USER_EDITING_TEMPLATE")
    repo.upsert_session(
        session_id="sess_1",
        state="USER_EDITING_TEMPLATE",
        snapshot={
            "pendingPlan": {"planId": "plan_1", "draftRevision": 3},
            "clarificationCount": 2,
            "clarificationKey": "change_region_2",
            "definition": {"metadata": {"revision": 3}},
        },
    )

    record = repo.get_session("sess_1")
    assert record.snapshot["pendingPlan"]["planId"] == "plan_1"
    assert record.snapshot["clarificationCount"] == 2
    assert record.snapshot["clarificationKey"] == "change_region_2"
    assert record.snapshot["definition"]["metadata"]["revision"] == 3


def test_edit_state_survives_reopen(repo):
    """编辑态跨轮状态在库里，后端重启后还在。"""
    db = repo.db_path
    repo.upsert_session(
        session_id="sess_1",
        state="USER_EDITING_TEMPLATE",
        snapshot={"pendingPlan": {"planId": "plan_1", "draftRevision": 3}},
    )
    repo.close()

    reopened = SqliteSessionRepository(db)
    record = reopened.get_session("sess_1")
    assert record.snapshot["pendingPlan"]["planId"] == "plan_1"
