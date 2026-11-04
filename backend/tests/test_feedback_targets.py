"""评价目标关联与**现场保护**专项（20261104 反馈功能）。

对应 K2 固定快检：

* 普通 ``record_turn`` 登记一个目标；
* 设计流 ``write_turn_inline``（同事务那条路径）登记一个目标；
* 推进到另一轮之后，反馈**旧**目标 → 目标仍是旧轮，缺失不补新事实；
* dropdown / 待选择现场：先拍快照 → 提交反馈 → 再拍快照，
  业务状态 / 设计阶段 / 定义 / 版本 / 锁 / 候选 / 菜单 / 撤销栈**完全相同**；
* 原先被拒绝的业务写请求**仍然被拒**，现场不变；
* 旧消息被同键覆盖后，原消息引用标**失效**（不拿新正文冒充旧回复）。

模式：真实服务层 + 隔离 SQLite（夹具已把仓库换成临时库）。
**不跑整套状态回归** —— 只测上面这几件事。
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.schemas.agent import (
    LOCK_SCOPE_INPUT_AND_MENU,
    AgentQuestion,
    AgentResponse,
    AgentState,
)
from app.schemas.session import PendingEditPlan
from app.services import feedback_service
from app.services.session_service import (
    PendingPlanConsumedError,
    session_service,
)
from app.storage.feedback_store import dedupe_key, fingerprint


SESSION_ID = "sess_fb_k2"


@pytest.fixture(autouse=True)
def feedback_env(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "feedback_attachment_dir", tmp_path / "attachments")
    return tmp_path


@pytest.fixture()
def fb_client() -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


def _response(reply: str, state: AgentState = AgentState.WAITING_USER_CONFIRM) -> AgentResponse:
    return AgentResponse(
        conversationId="conv_fb_k2",
        state=state,
        reply=reply,
        designSessionId="design_fb_k2",
        debug={"planId": "plan_fb_k2", "planRevision": 2},
    )


def _targets() -> list[dict[str, Any]]:
    store = feedback_service.store_for(session_service.repository)
    rows = store.connection.execute(
        "SELECT * FROM feedback_targets ORDER BY created_at ASC, rowid ASC"
    ).fetchall()
    return [dict(row) for row in rows]


# =====================================================================
# 目标登记
# =====================================================================

def test_k2_record_turn_registers_one_target():
    """普通落库路径（record_turn）登记**一个**目标，并写回响应。"""
    response = _response("已生成报表草稿。")
    session_service.record_turn(
        session_id=SESSION_ID,
        user_text="生成能耗日报",
        response=response,
        client_msg_id="msg_k2_1",
    )

    assert response.feedbackTarget is not None
    assert response.feedbackTarget.kind == "reply"
    assert response.feedbackTarget.id.startswith("fbt_")
    assert response.feedbackTarget.available is True

    targets = _targets()
    assert len(targets) == 1
    target = targets[0]
    assert target["session_id"] == SESSION_ID
    assert target["client_msg_id"] == "msg_k2_1:reply"
    # 定位信息取**登记当时**的状态
    assert target["business_state"] == "WAITING_USER_CONFIRM"
    assert target["design_phase"] is None or isinstance(target["design_phase"], str)


def test_k2_inline_design_path_registers_one_target():
    """设计流同事务路径（write_turn_inline）也登记**一个**目标。"""
    conn = session_service.repository.conn
    response = _response("方案已生成，请确认。", AgentState.WAITING_USER_CONFIRM)
    session_service.write_turn_inline(
        conn,
        session_id="sess_fb_k2_inline",
        user_text="给我一份能耗日报方案",
        response=response,
        client_msg_id="msg_k2_inline",
    )
    conn.commit()

    assert response.feedbackTarget is not None
    targets = _targets()
    assert len(targets) == 1
    assert targets[0]["session_id"] == "sess_fb_k2_inline"


def test_k2_advance_round_registers_no_target():
    """内部推进轮（__advance__）**不登记**目标：那一轮是进度提示，不是用户看到的一轮。"""
    response = _response("正在校验报表定义…", AgentState.VALIDATING)
    session_service.record_turn(
        session_id="sess_fb_k2_advance",
        user_text="__advance__",
        response=response,
        client_msg_id="msg_k2_advance",
    )
    assert response.feedbackTarget is None
    assert _targets() == []


# =====================================================================
# 绑定的是**原轮**，不是最新报表
# =====================================================================

def test_k2_feedback_binds_the_original_round(fb_client):
    """推进到另一轮之后，反馈旧目标 → 仍然是旧轮的内容与定位。"""
    first = _response("第一轮：已生成报表草稿。")
    session_service.record_turn(
        session_id=SESSION_ID,
        user_text="生成能耗日报",
        response=first,
        client_msg_id="msg_k2_a",
    )
    second = _response("第二轮：草稿已保存。", AgentState.SAVE_DEFINITION_DRAFT)
    session_service.record_turn(
        session_id=SESSION_ID,
        user_text="同意",
        response=second,
        client_msg_id="msg_k2_b",
    )

    first_target = first.feedbackTarget.id
    second_target = second.feedbackTarget.id
    assert first_target != second_target

    submitted = fb_client.post(
        "/api/feedback",
        data={"targetId": first_target, "submissionId": "k2-1", "rating": "satisfied"},
    )
    assert submitted.status_code == 201
    feedback_id = submitted.json()["feedbackId"]

    assert _login(fb_client).status_code == 200
    detail = fb_client.get(f"/api/feedback/admin/items/{feedback_id}").json()
    # 原需求 / 原回复都是**第一轮**的，不是第二轮、也不是最新报表
    assert detail["requestExcerpt"] == "生成能耗日报"
    assert "第一轮" in detail["replyExcerpt"]
    assert "第二轮" not in detail["replyExcerpt"]
    assert detail["locator"]["clientMsgId"] == "msg_k2_a:reply"


def _login(client: TestClient) -> Any:
    from app.services.feedback_auth import hash_password

    settings = get_settings()
    settings.feedback_admin_username = "keeper"
    settings.feedback_session_secret = "k2-secret"
    settings.feedback_admin_password_hash = hash_password("k2-pass")
    return client.post(
        "/api/feedback/admin/login", json={"username": "keeper", "password": "k2-pass"}
    )


def test_k2_missing_facts_are_not_backfilled(fb_client):
    """缺失项如实标「缺失」，**不从提交时的最新快照补齐**。"""
    response = _response("没有带方案的一轮。", AgentState.ANALYZING)
    session_service.record_turn(
        session_id="sess_fb_k2_missing",
        user_text="你好",
        response=response,
        client_msg_id="msg_k2_missing",
    )
    target_id = response.feedbackTarget.id
    submitted = fb_client.post(
        "/api/feedback",
        data={"targetId": target_id, "submissionId": "k2-missing", "rating": "neutral"},
    )
    feedback_id = submitted.json()["feedbackId"]

    assert _login(fb_client).status_code == 200
    detail = fb_client.get(f"/api/feedback/admin/items/{feedback_id}").json()
    assert "draftId" in detail["missingItems"]
    assert "planId" in detail["missingItems"]
    # 缺失的字段没有被「补」成后来的值
    assert not detail["locator"].get("draftId")


# =====================================================================
# 现场保护
# =====================================================================

def _scene_snapshot() -> dict[str, Any]:
    """当前业务现场的快照：状态 / 阶段 / 定义 / 版本 / 锁 / 候选 / 菜单。"""
    record = session_service.repository.get_session(SESSION_ID)
    snapshot = dict(record.snapshot or {}) if record else {}
    definition = snapshot.get("definition")
    return {
        "state": record.state if record else None,
        "designSessionId": record.designSessionId if record else None,
        "pendingChoice": record.pendingChoice if record else None,
        "pendingEditPlan": record.pendingEditPlan if record else None,
        "definition": json.dumps(definition, sort_keys=True, ensure_ascii=False)
        if isinstance(definition, dict)
        else None,
        "revision": (definition or {}).get("metadata", {}).get("revision")
        if isinstance(definition, dict)
        else None,
        "undoPointer": snapshot.get("undoPointer"),
        "redoPointer": snapshot.get("redoPointer"),
        "editingTargetRegionId": snapshot.get("editingTargetRegionId"),
        "messageCount": record.messageCount if record else None,
    }


def test_k2_feedback_does_not_touch_the_scene(fb_client):
    """dropdown / 待选择现场：提交反馈前后，报表现场**完全相同**。"""
    # 先有一轮真实的回复（拿目标）
    response = _response("已生成报表草稿。")
    session_service.record_turn(
        session_id=SESSION_ID,
        user_text="生成能耗日报",
        response=response,
        client_msg_id="msg_k2_scene",
    )
    target_id = response.feedbackTarget.id

    # 摆一份**待选择菜单**（锁输入）+ 一份候选定义
    question = AgentQuestion(
        id="template_edit_confirm",
        prompt="请选择下一步操作：",
        expects="SINGLE_CHOICE",
        lockScope=LOCK_SCOPE_INPUT_AND_MENU,
        options=[{"value": "同意", "label": "同意"}],
        stepId="step_k2",
        stepVersion=1,
    )
    session_service.save_pending_choice(
        SESSION_ID,
        {"state": "WAITING_USER_CONFIRM", "question": question.model_dump(mode="json")},
    )
    session_service.update_snapshot(
        SESSION_ID,
        lambda snapshot: {
            **snapshot,
            "definition": {"metadata": {"id": "def_k2", "revision": 7}, "regions": []},
            "undoPointer": 2,
            "redoPointer": 0,
            "editingTargetRegionId": "region_k2",
        },
    )

    before = _scene_snapshot()
    assert before["pendingChoice"] is not None

    submitted = fb_client.post(
        "/api/feedback",
        data={"targetId": target_id, "submissionId": "k2-scene", "rating": "satisfied"},
    )
    assert submitted.status_code == 201

    after = _scene_snapshot()
    assert after == before, "反馈提交改变了报表现场"


def test_k2_rejected_business_write_is_still_rejected(fb_client):
    """原先被拒的业务写请求**仍然被拒**，而且现场不变。"""
    response = _response("已生成报表草稿。")
    session_service.record_turn(
        session_id=SESSION_ID,
        user_text="生成能耗日报",
        response=response,
        client_msg_id="msg_k2_write",
    )
    # 摆一份待确认计划（会被「消费」掉的那一种）
    session_service.save_pending_plan(
        SESSION_ID,
        PendingEditPlan(
            planId="plan_k2",
            draftRevision=3,
            targetRegionId="region_k2",
            operations=[{"kind": "set_title", "title": "新标题"}],
            summary="改标题",
        ),
    )
    before = _scene_snapshot()

    # 第一份计划被消费掉
    session_service.stage_pending_plan_consumption(SESSION_ID, "plan_k2")
    assert session_service.commit_staged_plan_consumption(SESSION_ID) == "plan_k2"

    # 再消费一次 → 必须被拒（计划已经失效）
    session_service.stage_pending_plan_consumption(SESSION_ID, "plan_k2")
    with pytest.raises(PendingPlanConsumedError):
        session_service.commit_staged_plan_consumption(SESSION_ID)
    session_service.discard_staged_plan_consumption(SESSION_ID)

    after = _scene_snapshot()
    assert after["pendingEditPlan"] == before["pendingEditPlan"] or after["pendingEditPlan"] is None
    assert after["state"] == before["state"]
    assert after["definition"] == before["definition"]
    assert after["revision"] == before["revision"]


# =====================================================================
# 引用失效
# =====================================================================

def test_k2_overwritten_message_marks_reference_invalid(fb_client):
    """旧消息被同键覆盖后，原消息引用标**失效** —— 不读新正文冒充旧回复。"""
    reply_one = "第一版回复。"
    response = _response(reply_one)
    session_service.record_turn(
        session_id="sess_fb_k2_overwrite",
        user_text="生成能耗日报",
        response=response,
        client_msg_id="msg_k2_overwrite",
    )
    target_id = response.feedbackTarget.id
    submitted = fb_client.post(
        "/api/feedback",
        data={"targetId": target_id, "submissionId": "k2-overwrite", "rating": "neutral"},
    )
    feedback_id = submitted.json()["feedbackId"]

    assert _login(fb_client).status_code == 200
    detail = fb_client.get(f"/api/feedback/admin/items/{feedback_id}").json()
    assert detail["referenceState"] == "ok"

    # 同键重放但内容不同 → 消息被覆盖
    conn = session_service.repository.conn
    conn.execute(
        """
        UPDATE messages SET content = ? WHERE session_id = ? AND client_msg_id = ?
        """,
        ("第二版回复。", "sess_fb_k2_overwrite", "msg_k2_overwrite:reply"),
    )
    conn.commit()

    detail_after = fb_client.get(f"/api/feedback/admin/items/{feedback_id}").json()
    assert detail_after["referenceState"] == "overwritten"
    # 旧摘录**保持原样**：不拿新正文充当旧回复
    assert "第一版回复" in detail_after["replyExcerpt"]
    assert "第二版回复" not in detail_after["replyExcerpt"]


def test_k2_replay_of_same_response_reuses_target():
    """同一次响应重放（同一幂等键 + 同一内容）→ **复用**同一个目标。"""
    first = _response("同一份回复。")
    session_service.record_turn(
        session_id="sess_fb_k2_replay",
        user_text="生成能耗日报",
        response=first,
        client_msg_id="msg_k2_replay",
    )
    second = _response("同一份回复。")
    session_service.record_turn(
        session_id="sess_fb_k2_replay",
        user_text="生成能耗日报",
        response=second,
        client_msg_id="msg_k2_replay",
    )
    assert first.feedbackTarget.id == second.feedbackTarget.id
    assert len(_targets()) == 1


def test_k2_different_result_gets_its_own_target():
    """同逻辑 messageId 但**结果不同** → 各自保留独立目标。"""
    first = _response("版本 A。")
    session_service.record_turn(
        session_id="sess_fb_k2_diff",
        user_text="生成能耗日报",
        response=first,
        client_msg_id="msg_k2_diff",
    )
    second = _response("版本 B。")
    session_service.record_turn(
        session_id="sess_fb_k2_diff",
        user_text="生成能耗日报",
        response=second,
        client_msg_id="msg_k2_diff",
    )
    assert first.feedbackTarget.id != second.feedbackTarget.id
    assert len(_targets()) == 2


# =====================================================================
# 客户端观察
# =====================================================================

def test_k2_client_event_target_is_limited(fb_client):
    """客户端事件：服务端能核实的自己读，核实不了的只记受限观察。"""
    response = fb_client.post(
        "/api/feedback/client-events",
        json={
            "kind": "client_stopped",
            "clientAttemptId": "attempt_k2_1",
            "sessionId": SESSION_ID,
            "messageId": "msg_k2_stop",
            "observation": "user_stopped",
        },
    )
    assert response.status_code == 200
    target = response.json()
    assert target["kind"] == "client_stopped"
    assert target["id"].startswith("fbt_")

    # 同一次事件重试 → 幂等：还是同一个目标
    again = fb_client.post(
        "/api/feedback/client-events",
        json={
            "kind": "client_stopped",
            "clientAttemptId": "attempt_k2_1",
            "sessionId": SESSION_ID,
            "messageId": "msg_k2_stop",
            "observation": "user_stopped",
        },
    )
    assert again.json()["id"] == target["id"]

    # 客户端**不能**塞后台事实进来（严格字段封闭）
    rejected = fb_client.post(
        "/api/feedback/client-events",
        json={
            "kind": "client_failure",
            "clientAttemptId": "attempt_k2_2",
            "businessState": "READY",
            "draftId": "def_fake",
        },
    )
    assert rejected.status_code == 422
