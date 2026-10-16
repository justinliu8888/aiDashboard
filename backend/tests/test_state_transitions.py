"""状态机合法性测试：ALLOWED_TRANSITIONS 不能被改坏。"""
from __future__ import annotations

from app.schemas.agent import ALLOWED_TRANSITIONS, AgentState


def test_blocked_or_unsupported_is_terminal():
    """被拦截/不支持是终止态，不允许往其他状态跳。"""
    assert ALLOWED_TRANSITIONS[AgentState.BLOCKED_OR_UNSUPPORTED] == set()


def test_analyzing_can_reach_draft_or_block_or_clarify():
    allowed = ALLOWED_TRANSITIONS[AgentState.ANALYZING]
    assert AgentState.NEEDS_CLARIFICATION in allowed
    assert AgentState.BLOCKED_OR_UNSUPPORTED in allowed
    # 既允许一步到位的 WAITING_USER_CONFIRM，也允许先 DEFINITION_DRAFT_READY
    assert (
        AgentState.WAITING_USER_CONFIRM in allowed
        or AgentState.DEFINITION_DRAFT_READY in allowed
    )


def test_waiting_user_confirm_can_save_or_restart_or_patch():
    allowed = ALLOWED_TRANSITIONS[AgentState.WAITING_USER_CONFIRM]
    assert AgentState.SAVE_DEFINITION_DRAFT in allowed
    # 允许用户在确认阶段继续修改
    assert (
        AgentState.ANALYZING in allowed
        or AgentState.DEFINITION_DRAFT_READY in allowed
    )


def test_validating_must_lead_to_preview_or_fail():
    allowed = ALLOWED_TRANSITIONS[AgentState.VALIDATING]
    assert allowed == {AgentState.PREVIEWING, AgentState.VALIDATION_FAILED}


def test_render_pipeline_order():
    assert ALLOWED_TRANSITIONS[AgentState.PREVIEWING] == {AgentState.PREVIEW_READY}
    assert ALLOWED_TRANSITIONS[AgentState.PREVIEW_READY] == {AgentState.RENDERING}
    assert ALLOWED_TRANSITIONS[AgentState.RENDERING] == {AgentState.READY}


def test_all_states_have_entry_in_transition_table():
    for state in AgentState:
        assert state in ALLOWED_TRANSITIONS, f"缺少 {state} 的转移定义"


def test_invalid_transition_is_rejected_by_service(post_chat):
    """从 BLOCKED_OR_UNSUPPORTED 发起对话，服务不应允许它跳到 WAITING_USER_CONFIRM。

    FakeAgent 当前对 BLOCKED 态走的是 analyzing 分支，正常会尝试生成草稿；
    agent_service 应该在转移校验层把这种非法跳转拦截掉。
    """
    r = post_chat(
        "生成 A 机房昨日能耗日报",
        state="BLOCKED_OR_UNSUPPORTED",
    )
    data = r.json()
    assert data["error"] is not None
    assert data["error"]["code"] == "INVALID_STATE_TRANSITION"
