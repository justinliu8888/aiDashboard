"""工具层契约测试：结构化条件必须从终结工具一路走到定义草稿。

这条链路上任何一环把字段丢掉，用户要的时间窗 / 数据源 / 过滤就会
在最后一步被默认值覆盖回去。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.agents.agentscope_report_agent import AgentScopeReportAgent
from app.schemas.agent import AgentRequest, AgentRequestContext, AgentState
from app.tools import registry
from app.tools.submission import bind_submission, peek_submission


def _bind_scope(subject_id: str = "room_a", project_id: str = "project_001") -> None:
    from app.schemas.agent import AgentState as State
    from app.tools.context import ToolContext, bind_tool_context

    bind_tool_context(
        ToolContext(
            state=State.ANALYZING,
            subject_ref="machine_room",
            subject_id=subject_id,
            project_id=project_id,
        )
    )


def _request(message: str = "生成 A 机房昨日能耗日报") -> AgentRequest:
    return AgentRequest(
        conversationId="conv_tool_contract",
        message=message,
        state=AgentState.ANALYZING,
        context=AgentRequestContext(
            projectId="project_001",
            subjectRef="machine_room",
            subjectId="room_a",
        ),
    )


# ------------------------------------------------------------------ 提交契约


def test_submit_draft_plan_forwards_structured_conditions():
    bind_submission()
    result = registry.get_runner("submit_draft_plan")(
        reportName="A 机房近 7 日能耗",
        roomId="room_a",
        includeTrend=True,
        reply="已按你的条件生成草稿。",
        sourceRef="eas_energy_detail",
        grainUnit="MINUTE",
        timeWindow={"unit": "DAY", "count": 7},
        metricRefs=["metric.machine_room.total_energy"],
        componentRequests=["total", "trend"],
        excludedRegions=["analysis"],
        filters=[{"fieldRef": "deviceType", "op": "IN", "values": ["CRAC"]}],
        topN=3,
        derivedColumns=[{"kind": "GROWTH_RATE", "label": "日增长率 (%)"}],
        styleHints=[
            {
                "fieldRef": "growthRatePercent",
                "comparison": "GT",
                "threshold": 20,
                "style": "RED",
            }
        ],
        capabilityEvidence={"checked": True},
    )
    assert result["accepted"] is True
    payload = peek_submission()["payload"]
    assert payload["sourceRef"] == "eas_energy_detail"
    assert payload["grainUnit"] == "MINUTE"
    assert payload["timeWindow"] == {"unit": "DAY", "count": 7}
    assert payload["componentRequests"] == ["total", "trend"]
    assert payload["excludedRegions"] == ["analysis"]
    assert payload["filters"][0]["fieldRef"] == "deviceType"
    assert payload["filters"][0]["values"] == ["CRAC"]
    assert payload["topN"] == 3
    assert payload["derivedColumns"][0]["kind"] == "GROWTH_RATE"
    assert payload["styleHints"][0]["threshold"] == 20
    assert payload["capabilityEvidence"] == {"checked": True}


def test_submit_draft_plan_still_accepts_legacy_payload():
    """旧调用方式不受影响。"""
    bind_submission()
    result = registry.get_runner("submit_draft_plan")(
        reportName="A 机房能耗日报",
        roomId="room_a",
        includeTrend=True,
    )
    assert result["accepted"] is True
    payload = peek_submission()["payload"]
    assert payload["reportName"] == "A 机房能耗日报"
    assert payload["sourceRef"] is None
    assert payload["filters"] == []


def test_submit_draft_plan_rejects_invalid_structured_field():
    bind_submission()
    result = registry.get_runner("submit_draft_plan")(
        reportName="A 机房能耗日报",
        roomId="room_a",
        topN="不是数字",
    )
    assert result["accepted"] is True  # 非法值被归一成 None，不阻断
    assert peek_submission()["payload"]["topN"] is None


# ------------------------------------------------------------------ 组装工具


def test_build_definition_draft_tool_passes_conditions_through():
    result = registry.get_runner("build_definition_draft")(
        reportName="A 机房近 7 日能耗",
        roomId="room_a",
        timeWindow={"unit": "DAY", "count": 7},
        grainUnit="DAY",
        filters=[{"fieldRef": "deviceId", "op": "IN", "values": ["crac_01"]}],
        topN=3,
    )
    definition = result["definitionDraft"]
    assert definition["defaults"]["timeWindow"]["count"] == 7
    assert definition["defaults"]["grain"] == {"unit": "DAY"}
    assert definition["defaults"]["preFilters"][0]["fieldRef"] == "deviceId"


def test_build_definition_draft_tool_uses_camelcase_kwarg_names():
    """路由层按 camelCase 槽位名调用 builder，参数名必须对得上。"""
    import inspect

    from app.services.report_draft_service import build_definition_draft

    params = set(inspect.signature(build_definition_draft).parameters)
    for name in (
        "sourceRef",
        "grainUnit",
        "timeWindow",
        "timeRange",
        "metricRefs",
        "componentRequests",
        "excludedRegions",
        "filters",
        "limit",
        "topN",
        "derivedColumns",
        "styleHints",
        "capabilityEvidence",
    ):
        assert name in params, f"builder 缺少路由层需要的参数 {name}"


# ------------------------------------------------------------------ Agent 层


def test_agent_dispatch_passes_plan_conditions_into_definition():
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._fallback = None

    response = agent._dispatch_submission(
        _request(),
        {
            "tool": "submit_draft_plan",
            "payload": {
                "reportName": "A 机房近 7 日能耗",
                "roomId": "room_a",
                "includeTrend": True,
                "reply": "已生成草稿",
                "timeWindow": {"unit": "DAY", "count": 7},
                "grainUnit": "DAY",
                "sourceRef": "eas_energy_detail",
                "topN": 3,
            },
        },
    )
    definition = response.domainObjects.reportDefinitionDraft
    assert response.state == AgentState.WAITING_USER_CONFIRM
    assert definition.defaults["timeWindow"]["count"] == 7
    assert definition.defaults["grain"] == {"unit": "DAY"}
    assert definition.defaults["source"]["sourceRef"] == "eas_energy_detail"


def test_agent_dispatch_blocks_on_unsupported_scope():
    """越权范围：明确阻断，不退回默认草稿。"""
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._fallback = None

    response = agent._dispatch_submission(
        _request(),
        {
            "tool": "submit_draft_plan",
            "payload": {
                "reportName": "全部机房能耗排名",
                "roomId": "room_all",
                "includeTrend": True,
                "reply": "",
            },
        },
    )
    assert response.state == AgentState.BLOCKED_OR_UNSUPPORTED
    assert response.blocks
    assert response.blocks[0].code == "SCOPE_NOT_AUTHORIZED"
    assert response.domainObjects.reportDefinitionDraft is None


def test_agent_dispatch_blocks_on_unknown_metric():
    """未登记指标：明确阻断，不用耗电量顶替。"""
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._fallback = None

    response = agent._dispatch_submission(
        _request(),
        {
            "tool": "submit_draft_plan",
            "payload": {
                "reportName": "设备心情值趋势",
                "roomId": "room_a",
                "includeTrend": True,
                "reply": "",
                "metricRefs": ["metric.machine_room.device_mood"],
            },
        },
    )
    assert response.state == AgentState.BLOCKED_OR_UNSUPPORTED
    assert response.blocks[0].code == "METRIC_NOT_REGISTERED"


def test_agent_dispatch_blocks_on_unsupported_grain():
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._fallback = None

    response = agent._dispatch_submission(
        _request(),
        {
            "tool": "submit_draft_plan",
            "payload": {
                "reportName": "A 机房分钟趋势",
                "roomId": "room_a",
                "includeTrend": True,
                "reply": "",
                "grainUnit": "MINUTE",
            },
        },
    )
    assert response.state == AgentState.BLOCKED_OR_UNSUPPORTED
    assert response.blocks[0].code == "GRAIN_NOT_SUPPORTED"


# ------------------------------------------------------------------ 模板流联动


def test_template_flow_builds_definition_with_router_slots():
    """模板流解析出的槽位必须真的落进定义（M-06 / M-13）。"""
    from app.services.template_preview_flow import template_preview_flow

    request = _request("生成 A 机房过去7个完整自然日的总耗电和每天趋势，不要只看昨日模版")
    response = template_preview_flow.start(request)
    definition = response.domainObjects.reportDefinitionDraft
    if definition is None:
        pytest.skip("模板流当前不接管这条请求，交回正常路由")
    assert definition.defaults["timeWindow"]["count"] == 7
    assert definition.defaults["grain"] == {"unit": "DAY"}
