"""阶段 0 测试：ReAct 预算、输入补全、终结工具 → 协议响应。"""
from __future__ import annotations

from app.agents.agentscope_report_agent import AgentScopeReportAgent
from app.config import get_settings
from app.schemas.agent import (
    AgentRequest,
    AgentRequestContext,
    AgentRequestSnapshots,
    AgentState,
)
from app.schemas.report_definition import ReportDefinitionDraft
from app.services.report_draft_service import build_definition_draft


def _request(
    message: str = "生成 A 机房昨日能耗日报",
    state: AgentState = AgentState.ANALYZING,
    definition: ReportDefinitionDraft | None = None,
    collected: dict | None = None,
    selected_region_id: str | None = None,
) -> AgentRequest:
    request = AgentRequest(
        conversationId="conv_1",
        state=state,
        message=message,
        context=AgentRequestContext(
            projectId="project_001",
            subjectRef="machine_room",
            subjectId="room_a",
            selectedRegionId=selected_region_id,
        ),
        snapshots=AgentRequestSnapshots(reportDefinition=definition),
    )
    # collectedInfo 是服务端内部字段（PrivateAttr），只能走属性赋值
    request.collectedInfo = collected or {}
    return request


# ---------- 预算 ----------

def test_react_budget_shrinks_on_edit_and_fix_states():
    """编辑和修复场景步数要更少，不能和生成场景用同一个预算。"""
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._settings = get_settings()

    generate = agent._build_react_config(AgentState.ANALYZING)
    edit = agent._build_react_config(AgentState.USER_EDITING_TEMPLATE)
    fix = agent._build_react_config(AgentState.VALIDATION_FAILED)

    assert generate.max_iters == get_settings().react_max_iters
    assert edit.max_iters == get_settings().react_max_iters_edit
    assert fix.max_iters == get_settings().react_max_iters_fix
    assert edit.max_iters < generate.max_iters
    assert fix.max_iters < generate.max_iters


def test_react_budget_is_far_below_agentscope_default():
    """AgentScope 默认 50 步，报表场景用不了那么多。"""
    settings = get_settings()
    assert settings.react_max_iters <= 10
    assert settings.react_max_iters_edit <= 6


# ---------- 输入补全 ----------

def test_compose_user_message_includes_draft_summary():
    """改报表时模型必须知道有哪些区块，否则只能瞎猜。"""
    definition = ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "A 机房能耗日报"},
            "regions": [
                {
                    "id": "region_1",
                    "name": "能耗总览",
                    "component": {"type": "kpi", "metric": {"id": "energy_total"}},
                }
            ],
        }
    )
    request = _request(definition=definition)

    message = AgentScopeReportAgent._compose_user_message(request)
    assert "A 机房能耗日报" in message
    assert "region_1" in message
    assert "能耗总览" in message
    assert "区块数：1" in message


def test_compose_user_message_includes_collected_info():
    """路由层已收集的信息要带过去，避免重复追问用户。"""
    request = _request(collected={"timeRange": "昨天", "room": "A 机房"})

    message = AgentScopeReportAgent._compose_user_message(request)
    assert "已收集的信息" in message
    assert "timeRange" in message
    assert "昨天" in message


def test_compose_user_message_includes_selected_region():
    request = _request(selected_region_id="region_2")
    message = AgentScopeReportAgent._compose_user_message(request)
    assert "region_2" in message


def test_compose_user_message_omits_empty_sections():
    """没有草稿、没收集信息时不要留空标签。"""
    message = AgentScopeReportAgent._compose_user_message(_request())
    assert "已收集的信息" not in message
    assert "当前草稿" not in message
    assert "用户选中的区块" not in message


# ---------- 终结工具 → 协议响应 ----------

def test_dispatch_submission_generate():
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._fallback = None

    request = _request()
    response = agent._dispatch_submission(
        request,
        {
            "tool": "submit_draft_plan",
            "payload": {
                "reportName": "A 机房能耗日报",
                "roomId": "room_a",
                "includeTrend": True,
                "reply": "已生成草稿",
            },
        },
    )
    assert response.state == AgentState.WAITING_USER_CONFIRM
    assert response.domainObjects.reportDefinitionDraft is not None
    assert response.reply == "已生成草稿"


def test_dispatch_submission_clarification():
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._fallback = None

    response = agent._dispatch_submission(
        _request(),
        {
            "tool": "submit_clarification",
            "payload": {
                "missingInfo": [
                    {"field": "timeRange", "label": "要看哪段时间？", "expects": "TEXT"}
                ],
                "nextQuestion": "要看哪段时间？",
            },
        },
    )
    assert response.state == AgentState.NEEDS_CLARIFICATION
    assert len(response.questions) == 1
    assert response.questions[0].id == "timeRange"


def test_dispatch_submission_clarification_without_missing_is_chitchat():
    """没有 missingInfo 的澄清提交就是闲聊，别再追问。"""
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._fallback = None

    response = agent._dispatch_submission(
        _request(),
        {
            "tool": "submit_clarification",
            "payload": {"missingInfo": [], "reply": "你好，我能帮你生成报表。"},
        },
    )
    assert response.state == AgentState.ANALYZING
    assert "你好" in response.reply


def test_dispatch_submission_blocked():
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._fallback = None

    response = agent._dispatch_submission(
        _request(),
        {
            "tool": "submit_blocked",
            "payload": {
                "layer": "CAPABILITY",
                "message": "该数据源不支持分钟粒度",
                "alternatives": ["改为小时粒度"],
            },
        },
    )
    assert response.state == AgentState.BLOCKED_OR_UNSUPPORTED
    assert len(response.blocks) == 1
    assert response.blocks[0].alternatives == ["改为小时粒度"]


def test_dispatch_submission_change_plan():
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._fallback = None

    response = agent._dispatch_submission(
        _request(),
        {
            "tool": "submit_change_plan",
            "payload": {
                "changes": [
                    {"regionId": "region_1", "path": "grain", "summary": "改成小时级"}
                ],
                "reply": "改好了",
            },
        },
    )
    assert response.state == AgentState.DEFINITION_DRAFT_READY
    assert "region_1" in response.reply


def test_dispatch_submission_edit_plan_without_target_asks():
    """编辑提交说不清改哪块时，要追问而不是硬改。"""
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._fallback = None

    response = agent._dispatch_submission(
        _request(),
        {
            "tool": "submit_edit_plan",
            "payload": {"intent": "UNKNOWN", "summary": ""},
        },
    )
    assert response.state == AgentState.USER_EDITING_TEMPLATE
    assert response.questions[0].id == "template_edit_clarification"
    assert response.debug["editOutcome"] == "clarification"


def test_dispatch_submission_edit_plan_waits_for_acceptance():
    """候选计划只展示摘要；接受前草稿与版本都不变。"""
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._fallback = None

    definition = build_definition_draft("A 机房能耗日报", "room_a")
    request = _request(
            message="把昨日总耗电标题改成昨日总耗电量",
            state=AgentState.USER_EDITING_TEMPLATE,
            definition=definition,
        )
    response = agent._dispatch_submission(
        request,
        {
            "tool": "submit_edit_plan",
            "payload": {
                "intent": "RENAME_SECTION",
                "target": {"regionId": "region_total_energy"},
                "operation": {"newTitle": "昨日总耗电量"},
                "confidence": 0.9,
                "reply": "请确认",
            },
        },
    )
    assert response.state == AgentState.USER_EDITING_TEMPLATE
    assert response.domainObjects.changeSet is None
    assert response.domainObjects.reportDefinitionDraft.metadata["revision"] == 0
    assert definition.metadata["revision"] == 0
    assert response.questions[0].id == "pending_edit_plan_confirm"
    assert response.questions[0].selectionOnly is True
    assert [item.value for item in response.questions[0].options] == [
        "接受修改", "继续调整", "取消",
    ]
    assert response.uiActions == ["pending_edit_plan"]
    assert response.debug["editingTargetRegionId"] == "region_total_energy"
    candidate = response.debug["pendingEditPlan"]
    assert candidate["targetRegionId"] == "region_total_energy"
    assert candidate["draftRevision"] == 0
    assert candidate["operations"][0]["operation"]["newTitle"] == "昨日总耗电量"
    assert "昨日总耗电" in candidate["summary"]


def test_edit_answer_does_not_create_plan_or_change_set():
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    definition = build_definition_draft("能耗日报", "room_a")
    response = agent._dispatch_submission(
        _request("这个数据支持哪些粒度？", AgentState.USER_EDITING_TEMPLATE, definition),
        {"tool": "submit_edit_answer", "payload": {
            "reply": "当前数据源支持天和小时粒度。",
            "facts": ["支持 DAY", "支持 HOUR"],
            "suggestions": ["可申请改为小时级"],
        }},
    )
    assert response.reply == "当前数据源支持天和小时粒度。"
    assert response.domainObjects.changeSet is None
    assert response.domainObjects.reportDefinitionDraft.metadata["revision"] == 0
    assert response.questions == []
    assert response.debug["editOutcome"] == "answer"
    assert "pendingEditPlan" not in response.debug


def test_model_failure_is_not_user_clarification():
    response = AgentScopeReportAgent._unavailable_edit_response(
        _request(state=AgentState.USER_EDITING_TEMPLATE)
    )
    assert response.error.code == "EDIT_VERIFICATION_UNAVAILABLE"
    assert response.questions == []
    assert response.debug["editOutcome"] == "technical_failure"
    assert "补充具体区块" not in response.reply


def test_low_confidence_asks_without_applying():
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    definition = build_definition_draft("能耗日报", "room_a")
    response = agent._assemble_edit_response(
        _request("删除趋势图", AgentState.USER_EDITING_TEMPLATE, definition),
        {"intent": "DELETE_SECTION", "target": {"regionId": "region_hourly_trend"},
         "confidence": 0.3, "clarificationQuestion": "请确认是否删除小时趋势？"},
    )
    assert response.debug["editOutcome"] == "clarification"
    assert response.domainObjects.changeSet is None
    assert definition.metadata["revision"] == 0


def test_yesterday_afternoon_is_not_reduced_to_whole_day():
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    definition = build_definition_draft("能耗日报", "room_a")
    response = agent._assemble_edit_response(
        _request("把趋势改成昨天下午", AgentState.USER_EDITING_TEMPLATE, definition),
        {"intent": "CHANGE_TIME_RANGE", "target": {"regionId": "region_hourly_trend"},
         "confidence": 0.9, "operation": {"timeWindow": {"unit": "DAY", "offset": -1}}},
    )
    assert response.debug["editOutcome"] == "clarification"
    assert "pendingEditPlan" not in response.debug


def test_ordinal_requires_visible_order_from_integration():
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    definition = build_definition_draft("能耗日报", "room_a")
    response = agent._assemble_edit_response(
        _request("删除第三个", AgentState.USER_EDITING_TEMPLATE, definition),
        {"intent": "DELETE_SECTION", "target": {"regionId": "region_hourly_trend"},
         "confidence": 0.9},
    )
    assert response.debug["editOutcome"] == "clarification"
    assert "可见正文卡片" in response.reply


def test_combined_candidate_keeps_both_steps_without_mutation():
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    definition = build_definition_draft("能耗日报", "room_a")
    response = agent._assemble_edit_response(
        _request("趋势图换源并改成分钟粒度", AgentState.USER_EDITING_TEMPLATE, definition),
        {
            "intent": "CHANGE_COMPILATION_GRAIN",
            "target": {"regionId": "region_hourly_trend"},
            "operation": {"grain": {"unit": "MINUTE"}},
            "confidence": 0.9,
            "operations": [
                {"intent": "CHANGE_DATA_SOURCE",
                 "operation": {"sourceRef": "catalog.machine_room.minute"}},
                {"intent": "CHANGE_COMPILATION_GRAIN",
                 "operation": {"grain": {"unit": "MINUTE"}}},
            ],
        },
    )
    candidate = response.debug["pendingEditPlan"]
    assert [item["intent"] for item in candidate["operations"]] == [
        "CHANGE_DATA_SOURCE", "CHANGE_COMPILATION_GRAIN",
    ]
    assert "数据源" in candidate["summary"] and "粒度" in candidate["summary"]
    assert response.domainObjects.changeSet is None
    assert definition.metadata["revision"] == 0


def test_missing_grain_stays_clarification_even_with_high_confidence():
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    definition = build_definition_draft("能耗日报", "room_a")
    response = agent._assemble_edit_response(
        _request("改一下趋势图粒度", AgentState.USER_EDITING_TEMPLATE, definition),
        {"intent": "CHANGE_COMPILATION_GRAIN",
         "target": {"regionId": "region_hourly_trend"}, "confidence": 0.99},
    )
    assert response.debug["editOutcome"] == "clarification"
    assert "grain.unit" in response.reply
    assert response.domainObjects.changeSet is None


def test_dispatch_submission_edit_plan_suggest_visualization_offers_options():
    """表达含糊时不替用户决定，要给候选选项。"""
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._fallback = None

    definition = build_definition_draft("A 机房能耗日报", "room_a")
    response = agent._dispatch_submission(
        _request(
            state=AgentState.USER_EDITING_TEMPLATE,
            definition=definition,
        ),
        {
            "tool": "submit_edit_plan",
            "payload": {
                "intent": "SUGGEST_VISUALIZATION",
                "target": {"regionId": "region_hourly_trend"},
                "operation": {},
            },
        },
    )
    assert response.state == AgentState.USER_EDITING_TEMPLATE
    assert response.questions[0].expects == "SINGLE_CHOICE"
    assert response.questions[0].options
    assert response.questions[0].selectionOnly is True


def test_visual_preference_options_are_grounded_in_engine_capabilities():
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    response = agent._assemble_edit_response(
        _request(
            message="把趋势图改好看点",
            state=AgentState.USER_EDITING_TEMPLATE,
            definition=definition,
        ),
        {
            "intent": "UNKNOWN",
            "target": {"regionId": "region_hourly_trend"},
            "options": [{"value": "area_smooth", "label": "渐变平滑面积图"}],
        },
    )
    assert response.state == AgentState.USER_EDITING_TEMPLATE
    assert response.domainObjects.changeSet is None
    assert response.questions[0].selectionOnly
    assert [option.label for option in response.questions[0].options] == [
        "折线图", "面积图", "表格",
    ]
    assert all("area_smooth" != option.value for option in response.questions[0].options)


def test_compose_repair_hint_lists_only_available_terminal_tools():
    """修复提示只列当前节点调得到的终结工具。"""
    request = _request(state=AgentState.ANALYZING)
    hint = AgentScopeReportAgent._compose_repair_hint(request)
    assert "submit_draft_plan" in hint
    assert "submit_edit_plan" not in hint

    editing = _request(state=AgentState.USER_EDITING_TEMPLATE)
    editing_hint = AgentScopeReportAgent._compose_repair_hint(editing)
    assert "submit_edit_plan" in editing_hint
    assert "submit_edit_answer" in editing_hint
    assert "submit_draft_plan" not in editing_hint


def test_dispatch_submission_fix_plan():
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._fallback = None

    response = agent._dispatch_submission(
        _request(),
        {
            "tool": "submit_fix_plan",
            "payload": {"fixes": [{"issueCode": "X", "summary": "补齐时间范围"}]},
        },
    )
    assert response.state == AgentState.ANALYZING
    assert "补齐时间范围" in response.reply


def test_dispatch_submission_unknown_tool_returns_none():
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._fallback = None

    assert (
        agent._dispatch_submission(_request(), {"tool": "submit_unknown", "payload": {}})
        is None
    )
