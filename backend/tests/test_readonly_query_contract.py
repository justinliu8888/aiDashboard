"""只读查询契约（工单 #13 查当前对话草稿 / #14 待确认时也能提问）。

后端这一侧的验收边界：
    - 可信事实来自服务端会话记录，请求快照缺失或过旧都不能当成「没有草稿」
    - 事实结果能区分「核实到了 / 确认没有 / 读不到 / 范围不符」
    - 查询回答不推进业务：草稿、revision、预览、待确认计划、澄清轮次都不变
    - 待确认菜单存在时允许只读提问，答完原菜单与计划仍然可用
    - 明确生成 / 修改 / 确认仍走原有流程

接口测试走聊天 HTTP 入口与会话读取，不测私有函数。
"""
from __future__ import annotations

import pytest

from app.schemas.agent import (
    TURN_KIND_ACTION,
    AgentResponse,
    AgentState,
)
from app.schemas.fact import FactStatus
from app.services.session_fact_service import session_fact_service
from app.services.session_service import session_service
from app.tools import registry
from app.tools.base import SAME_STATE_CONCLUSION, ToolCategory


# ---------- 夹具 ----------

def _definition():
    from app.services.report_draft_service import build_definition_draft

    return build_definition_draft("A 机房能耗日报", "room_a")


def _upsert_session(
    session_id: str = "sess_q",
    *,
    state: str = "WAITING_USER_CONFIRM",
    definition=None,
    snapshot: dict | None = None,
    pending_choice: dict | None = None,
):
    payload = dict(snapshot or {})
    if definition is not None:
        payload["definition"] = definition.model_dump(mode="json")
    record = session_service.repository.upsert_session(
        session_id=session_id,
        state=state,
        title="测试会话",
        snapshot=payload,
    )
    if pending_choice is not None:
        session_service.repository.set_pending_choice(session_id, pending_choice)
        record = session_service.repository.get_session(session_id)
    return record


# ---------- 可信会话事实 ----------


def test_session_facts_report_absent_when_session_has_no_draft():
    _upsert_session("sess_empty", state="ANALYZING")

    facts = session_fact_service.load(session_id="sess_empty")

    # 会话记录读到了，但里面确实没有草稿：草稿这一项是「确认没有」。
    assert facts.status is FactStatus.FOUND
    assert facts.sessionExists is True
    assert facts.draft is not None
    assert facts.draft.status is FactStatus.ABSENT
    assert facts.draft.exists is False
    assert facts.businessStage == "ANALYZING"


def test_session_facts_report_found_draft_with_name_and_pending_steps():
    _upsert_session("sess_draft", definition=_definition())

    facts = session_fact_service.load(session_id="sess_draft")

    assert facts.status is FactStatus.FOUND
    assert facts.draft is not None
    assert facts.draft.exists is True
    assert facts.draft.reportName == "A 机房能耗日报"
    assert facts.draft.revision == 0
    assert facts.draft.regionCount > 0
    assert facts.draft.source == "server_session"
    # 待完成步骤按已记录的业务阶段推导。
    assert facts.draft.pendingSteps == ["确认这份报表草稿"]


def test_session_facts_report_absent_for_new_conversation_without_record():
    """新建对话还没有服务端记录：确实没有草稿，不是「读不到」。"""
    facts = session_fact_service.load(session_id="sess_never_seen")

    assert facts.status is FactStatus.ABSENT
    assert facts.sessionExists is False


def test_session_facts_report_unavailable_when_client_shows_draft_but_record_missing():
    """页面有草稿、服务端却查不到：两边不一致，不能说「没有草稿」。"""
    facts = session_fact_service.load(
        session_id="sess_never_seen", request_has_draft=True
    )

    assert facts.status is FactStatus.UNAVAILABLE
    assert facts.sessionExists is False


def test_session_facts_report_unavailable_when_read_fails(monkeypatch):
    class Boom:
        def get_session(self, session_id):
            raise RuntimeError("db down")

    monkeypatch.setattr(session_service, "_repo", Boom())

    facts = session_fact_service.load(session_id="sess_x")

    assert facts.status is FactStatus.UNAVAILABLE
    assert facts.sessionExists is False
    assert "无法确认" in facts.reason


def test_session_facts_report_forbidden_when_scope_does_not_match():
    _upsert_session(
        "sess_scoped",
        definition=_definition(),
        snapshot={
            "scope": {
                "projectId": "project_001",
                "subjectRef": "machine_room",
                "subjectId": "room_a",
            }
        },
    )

    facts = session_fact_service.load(
        session_id="sess_scoped",
        project_id="project_001",
        subject_ref="machine_room",
        subject_id="room_b",
    )

    assert facts.status is FactStatus.FORBIDDEN
    assert facts.draft is None


def test_session_facts_ignore_client_snapshot_when_server_has_draft():
    """服务端有草稿时，事实以服务端为准（请求快照可能缺失或过旧）。"""
    _upsert_session("sess_server_draft", definition=_definition())

    facts = session_fact_service.load(session_id="sess_server_draft")

    assert facts.status is FactStatus.FOUND
    assert facts.draft.reportName == "A 机房能耗日报"


def test_session_facts_expose_pending_choice_and_plan():
    _upsert_session(
        "sess_pending",
        state="USER_EDITING_TEMPLATE",
        definition=_definition(),
        pending_choice={
            "state": "USER_EDITING_TEMPLATE",
            "question": {
                "id": "pending_edit_plan_confirm",
                "prompt": "确认这次修改：",
                "expects": "SINGLE_CHOICE",
                "selectionOnly": True,
                "options": [{"value": "接受修改", "label": "接受修改"}],
            },
        },
    )

    facts = session_fact_service.load(session_id="sess_pending")

    assert facts.pendingChoice is not None
    assert facts.pendingChoice["question"]["id"] == "pending_edit_plan_confirm"


# ---------- 只读回答契约 ----------


def test_agent_response_defaults_to_action_turn():
    response = AgentResponse(conversationId="c1", state=AgentState.ANALYZING, reply="x")

    assert response.turnKind == TURN_KIND_ACTION
    assert response.isQueryAnswer is False


def test_query_answer_tool_is_registered_as_terminal_with_same_state_conclusion():
    tool = next(t for t in registry.list_tools() if t.NAME == "submit_query_answer")

    assert tool.CATEGORY is ToolCategory.TERMINAL
    assert tool.CONCLUSION_STATES == (SAME_STATE_CONCLUSION,)
    assert registry.get_runner("submit_query_answer") is not None


def test_tool_contract_validation_still_passes():
    """新增「原地回答」结论后，工具契约自检必须仍然全绿。"""
    assert registry.validate_tool_contracts() == []


def test_query_mode_toolset_has_no_execution_capability():
    """查询链路在结构上就没有执行权限：只能读事实 + 提交答案。"""
    names = {tool.NAME for tool in registry.tools_for_query()}

    assert "submit_query_answer" in names
    assert "search_catalog" in names
    assert "get_current_draft" in names
    for forbidden in (
        "build_definition_draft",
        "submit_draft_plan",
        "submit_change_plan",
        "submit_edit_plan",
        "submit_fix_plan",
        "submit_blocked",
        "submit_clarification",
        "submit_edit_answer",
    ):
        assert forbidden not in names, forbidden


def test_query_answer_payload_rejects_empty_reply():
    from app.tools.schemas import QueryAnswerPayload

    with pytest.raises(Exception):
        QueryAnswerPayload.model_validate({"reply": "   "})


def test_query_answer_payload_accepts_clarification_branch():
    from app.tools.schemas import QueryAnswerPayload

    payload = QueryAnswerPayload.model_validate({
        "reply": "你说的那份是指哪一份？",
        "needsClarification": True,
        "clarificationQuestion": "你说的是哪一份报表草稿？",
    })

    assert payload.needsClarification is True
    assert payload.clarificationQuestion == "你说的是哪一份报表草稿？"


# ---------- 草稿事实工具（服务端可信） ----------


@pytest.fixture()
def bound_context(monkeypatch):
    """绑定本轮工具上下文 + 会话 key，模拟一次真实请求。"""
    from app.services.session_context import bind_session_key
    from app.tools.context import ToolContext, bind_tool_context, clear_tool_context

    class _Request:
        sessionId = None
        conversationId = "conv_tools"

    bind_session_key(_Request())
    bind_tool_context(
        ToolContext(
            state=AgentState.ANALYZING,
            subject_ref="machine_room",
            subject_id="room_a",
            project_id="project_001",
            definition=None,
        )
    )
    yield
    clear_tool_context()


def _run_draft_tool():
    from app.tools.get_current_draft_tool import run

    return run()


def _run_status_tool():
    from app.tools.get_current_session_status_tool import run

    return run()


def test_draft_tool_reports_found_draft_from_server(bound_context):
    _upsert_session("conv_tools", definition=_definition())

    result = _run_draft_tool()

    assert result["status"] == "FOUND"
    assert result["exists"] is True
    assert result["reportName"] == "A 机房能耗日报"
    assert result["source"] == "server_session"
    assert result["pendingSteps"] == ["确认这份报表草稿"]


def test_draft_tool_does_not_treat_missing_snapshot_as_no_draft(bound_context):
    """请求快照缺失，但服务端有草稿：不能说没有。"""
    _upsert_session("conv_tools", definition=_definition())

    result = _run_draft_tool()

    assert result["exists"] is True
    assert result["reportName"] == "A 机房能耗日报"


def test_draft_tool_reports_absent_when_server_session_has_no_draft(bound_context):
    _upsert_session("conv_tools", state="ANALYZING")

    result = _run_draft_tool()

    assert result["status"] == "ABSENT"
    assert result["exists"] is False


def test_draft_tool_reports_unavailable_when_read_fails(bound_context, monkeypatch):
    class Boom:
        def get_session(self, session_id):
            raise RuntimeError("db down")

    monkeypatch.setattr(session_service, "_repo", Boom())

    result = _run_draft_tool()

    assert result["status"] == "UNAVAILABLE"
    assert result["exists"] is False
    assert result["reason"]


def test_session_status_tool_exposes_recorded_stage_and_pending_choice(bound_context):
    _upsert_session(
        "conv_tools",
        state="USER_EDITING_TEMPLATE",
        definition=_definition(),
        pending_choice={
            "state": "USER_EDITING_TEMPLATE",
            "question": {
                "id": "pending_edit_plan_confirm",
                "prompt": "确认这次修改：",
                "expects": "SINGLE_CHOICE",
                "selectionOnly": True,
                "options": [{"value": "接受修改", "label": "接受修改"}],
            },
        },
    )

    result = _run_status_tool()

    assert result["status"] == "FOUND"
    assert result["businessStage"] == "USER_EDITING_TEMPLATE"
    assert result["hasDraft"] is True
    assert result["pendingChoice"]["question"]["id"] == "pending_edit_plan_confirm"
    assert result["clarificationRounds"] == 0


# ---------- 意图路由：路由器不能提前给事实结论 ----------


def _route_request(message: str, state: AgentState = AgentState.ANALYZING):
    from app.schemas.agent import AgentRequest, AgentRequestContext

    return AgentRequest(
        conversationId="conv_route",
        state=state,
        message=message,
        context=AgentRequestContext(
            projectId="project_001",
            subjectRef="machine_room",
            subjectId="room_a",
        ),
    )


def test_direct_reply_is_downgraded_for_ask_question():
    """路由器没有事实，不能对「有没有草稿」这类问题直接作答。"""
    from app.services.context_engine import (
        ConciergeAction,
        ConciergeDecision,
        UserIntent,
        context_engine,
    )

    concierge = ConciergeDecision(
        action=ConciergeAction.DIRECT_REPLY,
        userIntent=UserIntent.ASK_QUESTION,
        reply="我在。你可以继续告诉我是要确认、修改、重做还是提问。",
    )

    guarded = context_engine._guard_direct_reply(concierge, _route_request("你有草稿吗"))

    assert guarded.action is ConciergeAction.DEEP_ROUTE
    assert guarded.reply == ""


def test_direct_reply_is_still_allowed_for_chitchat():
    from app.services.context_engine import (
        ConciergeAction,
        ConciergeDecision,
        UserIntent,
        context_engine,
    )

    concierge = ConciergeDecision(
        action=ConciergeAction.DIRECT_REPLY,
        userIntent=UserIntent.CHITCHAT,
        reply="你好～",
    )

    guarded = context_engine._guard_direct_reply(concierge, _route_request("你好"))

    assert guarded.action is ConciergeAction.DIRECT_REPLY
    assert guarded.reply == "你好～"


def test_fallback_route_treats_readonly_question_as_ask_question(monkeypatch):
    """模型不可用时也要能区分「问当前对话」和「下指令」。"""
    from app.services.context_engine import UserIntent, context_engine

    monkeypatch.setattr(context_engine, "_client", None)

    decision = context_engine._fallback_route(
        _route_request("你有未完成的草稿吗"), "no client"
    )

    assert decision.userIntent is UserIntent.ASK_QUESTION
    assert decision.routingAction.value == "CONTINUE_CURRENT"


def test_fallback_route_keeps_edit_command_out_of_query_mode(monkeypatch):
    from app.services.context_engine import UserIntent, context_engine

    monkeypatch.setattr(context_engine, "_client", None)

    decision = context_engine._fallback_route(
        _route_request("把趋势图改成折线图，可以吗"), "no client"
    )

    assert decision.userIntent is not UserIntent.ASK_QUESTION


def test_readonly_question_classifier_separates_question_from_command():
    from app.services.routing_rules import is_readonly_question

    assert is_readonly_question("你有未完成的草稿吗？") is True
    assert is_readonly_question("刚才那份做到哪了？") is True
    assert is_readonly_question("你支持分钟趋势吗？") is True
    assert is_readonly_question("把趋势图改成折线图") is False
    assert is_readonly_question("生成 A 机房昨日能耗日报") is False
    assert is_readonly_question("今天天气怎么样") is False


# ---------- 端到端：聊天 HTTP 入口 ----------

_SNAP = {"reportDefinition": _definition().model_dump(mode="json")}


def _seed_session_with_draft(session_id="conv_test", **kwargs):
    return _upsert_session(session_id, definition=_definition(), **kwargs)


def test_query_answers_draft_name_and_keeps_state(post_chat):
    """当前对话有草稿：据实回答名称和待完成步骤，状态不变。"""
    _seed_session_with_draft()

    response = post_chat("你有未完成的草稿吗？", state="WAITING_USER_CONFIRM").json()

    assert response["state"] == "WAITING_USER_CONFIRM"
    assert response["turnKind"] == "query_answer"
    assert "A 机房能耗日报" in response["reply"]
    assert response["domainObjects"]["pendingEditPlan"] is None
    assert response["domainObjects"]["changeSet"] is None
    assert response["toolRequests"] == []


def test_query_does_not_create_draft_when_session_has_none(post_chat):
    """当前对话确实无草稿：明确说没有，也不凭空建一个。"""
    _upsert_session("conv_test", state="ANALYZING")

    response = post_chat("你有未完成的草稿吗？", state="ANALYZING").json()

    assert response["state"] == "ANALYZING"
    assert response["turnKind"] == "query_answer"
    assert "没有报表草稿" in response["reply"]
    assert response["domainObjects"]["reportDefinitionDraft"] is None
    assert session_service.repository.get_session("conv_test").snapshot.get("definition") is None


def test_query_uses_server_draft_not_stale_snapshot(post_chat):
    """服务端有草稿，请求快照缺失：不能回答没有，也不能回写旧快照。"""
    _seed_session_with_draft()

    response = post_chat("你有未完成的草稿吗？", state="WAITING_USER_CONFIRM").json()

    assert "A 机房能耗日报" in response["reply"]
    stored = session_service.repository.get_session("conv_test").snapshot["definition"]
    assert stored["metadata"]["name"] == "A 机房能耗日报"
    assert stored["metadata"]["revision"] == 0


def test_query_does_not_write_back_stale_client_snapshot(post_chat):
    """客户端带了过旧的草稿快照：服务端最新的草稿不能被覆盖。"""
    _seed_session_with_draft()
    stale = _definition().model_dump(mode="json")
    stale["metadata"]["name"] = "旧名字"
    stale["metadata"]["revision"] = 0

    post_chat(
        "你有未完成的草稿吗？",
        state="WAITING_USER_CONFIRM",
        snapshots={"reportDefinition": stale},
    )

    stored = session_service.repository.get_session("conv_test").snapshot["definition"]
    assert stored["metadata"]["name"] == "A 机房能耗日报"


def test_query_leaves_draft_revision_and_clarification_count_untouched(post_chat):
    _seed_session_with_draft(state="USER_EDITING_TEMPLATE")

    post_chat("你有未完成的草稿吗？", state="USER_EDITING_TEMPLATE")

    record = session_service.repository.get_session("conv_test")
    assert record.snapshot["definition"]["metadata"]["revision"] == 0
    assert session_service.load_clarification("conv_test") is None
    assert session_service.load_pending_plan("conv_test") is None


def test_query_does_not_append_unrelated_generation_options(post_chat):
    _seed_session_with_draft()

    response = post_chat("你有未完成的草稿吗？", state="WAITING_USER_CONFIRM").json()

    # 待确认菜单保留，但不带「生成能耗日报」这类无关生成选项。
    labels = [
        option["label"]
        for question in response["questions"]
        for option in question.get("options", [])
    ]
    assert "生成能耗日报" not in labels
    assert "生成 A 机房昨日能耗日报" not in labels


def test_query_guidance_for_history_question(post_chat):
    """问别的对话的草稿：引导到历史列表，不虚构跨会话检索。"""
    _seed_session_with_draft()

    response = post_chat("我之前那份草稿还在吗？", state="WAITING_USER_CONFIRM").json()

    assert "会话列表" in response["reply"]
    assert "当前对话" in response["reply"]


def test_query_failure_does_not_fall_back_to_generation_prompt(post_chat):
    """服务端草稿读不出来：如实说无法确认，不降级成要求补充生成条件。"""
    _upsert_session(
        "conv_test", state="ANALYZING", snapshot={"definition": {"regions": "不是列表"}}
    )

    response = post_chat("你有未完成的草稿吗？", state="ANALYZING").json()

    assert "无法确认" in response["reply"]
    assert "时间范围" not in response["reply"]
    assert "粒度" not in response["reply"]


def test_query_tool_error_is_not_reported_as_no_draft():
    """事实工具报错时，事实结果必须是「读不到」，不能是「没有」。"""
    from app.services.query_answer_service import compose_query_reply

    reply, facts, limitations = compose_query_reply(
        message="你有未完成的草稿吗？",
        results=[("get_current_draft", {"error": "db down"})],
    )

    assert "无法确认" in reply
    assert limitations
    assert not any("没有报表草稿" in fact for fact in facts)


# ---------- 工单 #14：待确认时也能提问 ----------


def _seed_pending_edit_plan(session_id="conv_test"):
    """造一个「有待确认修改计划」的会话：菜单锁 + 计划都在。"""
    from app.schemas.session import PendingEditPlan

    session_service.repository.upsert_session(
        session_id=session_id,
        state="USER_EDITING_TEMPLATE",
        title="测试会话",
        snapshot={"definition": _definition().model_dump(mode="json")},
    )
    session_service.save_pending_plan(
        session_id,
        PendingEditPlan(
            planId="plan_q1",
            draftRevision=0,
            targetRegionId="region_total_energy",
            operations=[{"intent": "RENAME_REPORT"}],
            summary="把报表名称改成新日报",
        ),
    )
    session_service.save_pending_choice(
        session_id,
        {
            "state": "USER_EDITING_TEMPLATE",
            "question": {
                "id": "pending_edit_plan_confirm",
                "prompt": "确认这次修改：",
                "expects": "SINGLE_CHOICE",
                "selectionOnly": True,
                "options": [
                    {"value": "接受修改", "label": "接受修改"},
                    {"value": "继续调整", "label": "继续调整"},
                    {"value": "取消", "label": "取消"},
                ],
            },
        },
    )


def test_pending_lock_allows_readonly_question(post_chat):
    """待确认菜单在时也能提问，并得到有事实依据的回答。"""
    _seed_pending_edit_plan()

    response = post_chat(
        "你有未完成的草稿吗？", state="USER_EDITING_TEMPLATE"
    ).json()

    assert response["turnKind"] == "query_answer"
    assert response["state"] == "USER_EDITING_TEMPLATE"
    assert "A 机房能耗日报" in response["reply"]


def test_pending_lock_keeps_menu_and_plan_after_question(post_chat):
    """回答之后原菜单、pendingChoice、待确认修改计划都还在。"""
    _seed_pending_edit_plan()

    response = post_chat(
        "你有未完成的草稿吗？", state="USER_EDITING_TEMPLATE"
    ).json()

    options = [
        option["value"]
        for question in response["questions"]
        for option in question.get("options", [])
    ]
    assert options == ["接受修改", "继续调整", "取消"]
    assert session_service.load_pending_choice("conv_test") is not None
    plan = session_service.load_pending_plan("conv_test")
    assert plan is not None
    assert plan.planId == "plan_q1"
    assert plan.summary == "把报表名称改成新日报"


def test_pending_lock_question_does_not_touch_business_objects(post_chat):
    """提问不接受、不取消、不替换计划，也不改草稿、不占澄清轮次。"""
    _seed_pending_edit_plan()

    post_chat("你有未完成的草稿吗？", state="USER_EDITING_TEMPLATE")

    record = session_service.repository.get_session("conv_test")
    assert record.snapshot["definition"]["metadata"]["revision"] == 0
    assert record.snapshot["definition"]["metadata"]["name"] == "A 机房能耗日报"
    assert session_service.load_clarification("conv_test") is None
    assert session_service.load_pending_plan("conv_test") is not None


def test_pending_lock_still_rejects_non_readonly_input(post_chat):
    """非只读输入不能绕过菜单确认约束。"""
    _seed_pending_edit_plan()

    response = post_chat("把趋势图改成折线图", state="USER_EDITING_TEMPLATE").json()

    assert response["uiActions"] == ["selection_required"]
    assert response["domainObjects"]["pendingEditPlan"] is None
    assert response["domainObjects"]["changeSet"] is None
    plan = session_service.load_pending_plan("conv_test")
    assert plan is not None and plan.planId == "plan_q1"


def test_pending_lock_rejects_filling_option_with_free_text(post_chat):
    """用自由文本冒充选项值不行：原菜单必须原样点选。"""
    _seed_pending_edit_plan()

    response = post_chat("好的，接受修改吧", state="USER_EDITING_TEMPLATE").json()

    assert response["uiActions"] == ["selection_required"]
    assert session_service.load_pending_plan("conv_test") is not None
    record = session_service.repository.get_session("conv_test")
    assert record.snapshot["definition"]["metadata"]["revision"] == 0


def test_after_question_original_plan_can_still_be_accepted(post_chat):
    """提问之后原操作仍然可用：走原入口接受修改，草稿照常改一次。"""
    post_chat("报表名称改成新日报", state="USER_EDITING_TEMPLATE", snapshots=_SNAP)
    assert session_service.load_pending_plan("conv_test") is not None

    asked = post_chat(
        "你有未完成的草稿吗？", state="USER_EDITING_TEMPLATE", snapshots=_SNAP
    ).json()
    assert asked["turnKind"] == "query_answer"
    assert session_service.load_pending_plan("conv_test") is not None

    accepted = post_chat(
        "接受修改", state="USER_EDITING_TEMPLATE", snapshots=_SNAP
    ).json()

    assert accepted["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 1
    assert accepted["domainObjects"]["reportDefinitionDraft"]["metadata"]["name"] == "新日报"
    assert session_service.load_pending_plan("conv_test") is None


def test_query_does_not_consume_edit_clarification_rounds(post_chat):
    """查询澄清不消耗修改的澄清轮次。"""
    _seed_pending_edit_plan()

    for _ in range(4):
        post_chat("你有未完成的草稿吗？", state="USER_EDITING_TEMPLATE")

    assert session_service.load_clarification("conv_test") is None


# ---------- 流式入口与刷新恢复 ----------


def _read_stream_final(response) -> dict:
    import json

    for chunk in response.text.split("\n\n"):
        if chunk.startswith("event: final"):
            data = chunk.split("data: ", 1)[1]
            return json.loads(data)
    raise AssertionError("流式响应里没有 final 事件")


def test_stream_entry_has_same_readonly_query_semantics(client):
    """流式入口和普通入口一样带只读回答语义。"""
    session_id = "conv_stream_q"
    _upsert_session(session_id, definition=_definition())

    response = client.post("/api/agent/chat/stream", json={
        "conversationId": session_id,
        "sessionId": session_id,
        "state": "WAITING_USER_CONFIRM",
        "message": "你有未完成的草稿吗？",
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    })

    final = _read_stream_final(response)
    assert final["turnKind"] == "query_answer"
    assert final["state"] == "WAITING_USER_CONFIRM"
    assert "A 机房能耗日报" in final["reply"]


def test_session_restore_after_query_keeps_draft_and_pending_choice(client):
    """查询之后刷新 / 恢复当前对话：草稿、revision 和有效待确认操作都一致。"""
    session_id = "conv_restore_q"
    _upsert_session(session_id, definition=_definition(), state="USER_EDITING_TEMPLATE")
    session_service.save_pending_choice(
        session_id,
        {
            "state": "USER_EDITING_TEMPLATE",
            "question": {
                "id": "pending_edit_plan_confirm",
                "prompt": "确认这次修改：",
                "expects": "SINGLE_CHOICE",
                "selectionOnly": True,
                "options": [{"value": "接受修改", "label": "接受修改"}],
            },
        },
    )

    client.post("/api/agent/chat", json={
        "conversationId": session_id,
        "sessionId": session_id,
        "state": "USER_EDITING_TEMPLATE",
        "message": "你有未完成的草稿吗？",
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    })

    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["snapshot"]["definition"]["metadata"]["name"] == "A 机房能耗日报"
    assert detail["snapshot"]["definition"]["metadata"]["revision"] == 0
    assert detail["pendingChoice"]["question"]["id"] == "pending_edit_plan_confirm"
    assert detail["state"] == "USER_EDITING_TEMPLATE"


# ---------- C / D 接入口 ----------


def test_new_fact_tool_joins_query_toolset_automatically(monkeypatch):
    """C / D 的事实工具注册后自动进入查询链路，不需要改共用编排。

    这里用一个**开发期替身**工具验证接入口：真实模块由 C / D 自己实现。
    """
    from app.tools import registry
    from app.tools.base import ToolCategory

    class _DevCapabilityTool:
        NAME = "get_product_capability"
        DESCRIPTION = "（开发期替身）查当前产品实际支持的能力"
        PARAMETERS = {"type": "object", "properties": {}}
        CATEGORY = ToolCategory.FACT
        IS_READ_ONLY = True
        APPLICABLE_STATES = ()

        @staticmethod
        def run():
            return {
                "status": "FOUND",
                "summary": "默认源只登记了小时和天粒度，分钟粒度要走 EAS 明细源。",
            }

    monkeypatch.setattr(
        registry, "_TOOLS", [*registry._TOOLS, _DevCapabilityTool]
    )

    assert "get_product_capability" in {
        tool.NAME for tool in registry.tools_for_query()
    }


def test_new_fact_tool_summary_reaches_the_query_answer(monkeypatch, post_chat):
    """事实工具返回的 summary 会被并进回答（C / D 的事实要能出现在答案里）。"""
    from app.tools import registry
    from app.tools.base import ToolCategory

    class _DevCapabilityTool:
        NAME = "get_product_capability"
        DESCRIPTION = "（开发期替身）查当前产品实际支持的能力"
        PARAMETERS = {"type": "object", "properties": {}}
        CATEGORY = ToolCategory.FACT
        IS_READ_ONLY = True
        APPLICABLE_STATES = ()

        @staticmethod
        def run():
            return {
                "status": "FOUND",
                "summary": "分钟趋势要走 EAS 明细源，默认源不支持。",
            }

    monkeypatch.setattr(
        registry, "_TOOLS", [*registry._TOOLS, _DevCapabilityTool]
    )
    _seed_session_with_draft()

    response = post_chat("你支持分钟趋势吗？", state="ANALYZING").json()

    assert response["turnKind"] == "query_answer"
    assert "分钟趋势要走 EAS 明细源" in response["reply"]
