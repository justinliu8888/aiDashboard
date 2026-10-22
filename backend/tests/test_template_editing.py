"""节点 5.5 USER_EDITING_TEMPLATE 的解析、应用和接口闭环测试。"""
from __future__ import annotations

import pytest

from app.agents.agentscope_report_agent import AgentScopeReportAgent
from app.schemas.agent import AgentState
from app.schemas.edit_plan import EditIntent, EditPlan
from app.services.agent_service import agent_service
from app.services.preview_service import PreviewService
from app.services.report_draft_service import build_definition_draft, build_policy_draft
from app.services.template_edit_engine import template_edit_engine
from app.services.validation_service import validate_definition
from app.tools.context import ToolScope


SCOPE = ToolScope(project_id="project_001", subject_ref="machine_room", subject_id="room_a")


@pytest.fixture()
def definition():
    return build_definition_draft("A 机房能耗日报", "room_a")


def parse(definition, message: str, selected: str | None = None):
    return template_edit_engine.parse(
        message=message,
        definition=definition,
        selected_region_id=selected,
        scope=SCOPE,
    )


def apply(definition, message: str, selected: str | None = None):
    plan = parse(definition, message, selected)
    return template_edit_engine.apply(plan=plan, definition=definition, scope=SCOPE)


def region(definition, region_id: str):
    return next(item for item in definition.regions if item["id"] == region_id)


def test_time_range_wins_over_day_grain(definition):
    plan = parse(definition, "把趋势图改成最近 7 天")

    assert plan.intent == EditIntent.CHANGE_TIME_RANGE
    assert plan.target.regionId == "region_hourly_trend"
    assert plan.operation.timeWindow["value"]["count"] == 7


def test_explicit_target_wins_over_selected_region(definition):
    plan = parse(
        definition,
        "把设备耗电排名改成柱状图",
        selected="region_hourly_trend",
    )

    assert plan.target.regionId == "region_device_ranking"
    assert plan.intent == EditIntent.CHANGE_VISUALIZATION


@pytest.mark.parametrize(
    ("message", "selected", "intent", "path"),
    [
        ("报表名称改成A机房节能日报", None, EditIntent.RENAME_REPORT, "/metadata/name"),
        ("标题改成设备用电排行", "region_device_ranking", EditIntent.RENAME_SECTION,
         "/regions/region_device_ranking/name"),
        ("把设备耗电排名改成柱状图", None, EditIntent.CHANGE_VISUALIZATION,
         "/regions/region_device_ranking/component"),
        ("把趋势图改成最近 7 天", None, EditIntent.CHANGE_TIME_RANGE,
         "/regions/region_hourly_trend/context/timeWindow"),
        ("设备耗电排名只看前 5 名", None, EditIntent.CHANGE_TOP_N,
         "/regions/region_device_ranking/binding/topN"),
        ("设备耗电排名改成升序", None, EditIntent.CHANGE_SORT,
         "/regions/region_device_ranking/binding/sort"),
    ],
)
def test_common_edits_apply_and_bump_revision(
    definition, message, selected, intent, path
):
    result = apply(definition, message, selected)

    assert result.plan.intent == intent
    assert result.needsClarification is False
    assert result.changeSet.changed[0].path == path
    assert result.draftAfterEdit.metadata["revision"] == 1
    assert definition.metadata["revision"] == 0


def test_rule_match_skips_business_agent(post_chat, definition, monkeypatch):
    class UnexpectedAgent:
        async def ahandle(self, request):
            raise AssertionError("规则已命中，不应调用业务 Agent")

    monkeypatch.setattr(agent_service, "_agent", UnexpectedAgent())
    result = post_chat(
        "报表名称改成我要吃龙虾",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": definition.model_dump(mode="json")},
    ).json()
    # 规则命中后直接给候选计划：先确认，草稿此刻不变。
    assert result["debug"]["editPlanSource"] == "rule"
    assert result["debug"]["editOutcome"] == "candidate"
    assert result["uiActions"] == ["pending_edit_plan"]
    assert result["domainObjects"]["pendingEditPlan"] is not None
    assert result["domainObjects"]["changeSet"] is None
    assert result["domainObjects"]["reportDefinitionDraft"]["metadata"]["name"] == "A 机房能耗日报"
    assert definition.metadata["revision"] == 0


def test_uncertain_edit_reaches_business_agent_and_preserves_debug(
    post_chat, definition, monkeypatch
):
    calls = []
    assembler = AgentScopeReportAgent.__new__(AgentScopeReportAgent)

    class RecordingAgent:
        async def ahandle(self, request):
            calls.append((request.state, request.message))
            return assembler._assemble_edit_response(request, {
                "intent": "RENAME_REPORT",
                "target": {},
                "operation": {"newTitle": "我要吃龙虾"},
                "confidence": 0.9,
            })

    monkeypatch.setattr(agent_service, "_agent", RecordingAgent())
    response = post_chat(
        "A 机房能耗日报 改名成我要吃龙虾",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": definition.model_dump(mode="json")},
    ).json()
    assert calls == [(AgentState.USER_EDITING_TEMPLATE, "A 机房能耗日报 改名成我要吃龙虾")]
    assert response["state"] == "USER_EDITING_TEMPLATE"
    assert response["debug"]["editPlanSource"] == "react"
    assert response["debug"]["contextEngineStage"] == "template_edit_react"
    assert response["debug"]["editOutcome"] == "candidate"
    assert response["questions"][0]["id"] == "pending_edit_plan_confirm"
    assert response["questions"][0]["selectionOnly"] is True
    assert [item["value"] for item in response["questions"][0]["options"]] == [
        "接受修改", "继续调整", "取消",
    ]
    assert response["uiActions"] == ["pending_edit_plan"]
    # 候选计划只等确认，草稿此刻不变。
    assert response["domainObjects"]["pendingEditPlan"] is not None
    assert response["domainObjects"]["changeSet"] is None
    assert response["domainObjects"]["reportDefinitionDraft"]["metadata"]["name"] == "A 机房能耗日报"
    assert response["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 0
    assert definition.metadata["revision"] == 0


def test_delete_section_uses_selected_region_for_pronoun(definition):
    result = apply(definition, "删掉它", "region_total_energy")

    assert result.plan.intent == EditIntent.DELETE_SECTION
    assert result.changeSet.changed[0].after is None
    assert not any(
        item["id"] == "region_total_energy"
        for item in result.draftAfterEdit.regions
    )


def test_change_source_clones_shared_dataset(definition):
    result = apply(
        definition,
        "把设备耗电排名的数据源换成 EAS 能耗明细",
    )
    edited = result.draftAfterEdit
    ranking_ref = region(edited, "region_device_ranking")["data"]["inputs"]["main"][
        "datasetRef"
    ]
    trend_ref = region(edited, "region_hourly_trend")["data"]["inputs"]["main"][
        "datasetRef"
    ]

    assert ranking_ref != trend_ref
    assert edited.datasets[ranking_ref]["sourceRef"] == "eas_energy_detail"
    assert edited.datasets[trend_ref]["sourceRef"] == "catalog.machine_room.energy"


def test_minute_grain_rejected_without_mutating_draft(definition):
    result = apply(definition, "把趋势图按分钟级 compile")

    assert result.plan.intent == EditIntent.CHANGE_COMPILATION_GRAIN
    assert result.needsClarification is True
    assert result.changeSet is None
    assert result.draftAfterEdit.metadata["revision"] == 0
    assert region(result.draftAfterEdit, "region_hourly_trend")["name"] == "小时趋势"
    assert result.plan.operation.supportedGrains == ["HOUR", "DAY"]


def test_trend_grain_updates_default_title_and_can_change_back(definition):
    changed = apply(definition, "按天粒度", selected="region_hourly_trend")
    trend = region(changed.draftAfterEdit, "region_hourly_trend")

    assert changed.plan.intent == EditIntent.CHANGE_COMPILATION_GRAIN
    assert changed.needsClarification is False
    assert trend["name"] == "日趋势"
    assert trend["context"]["grain"]["value"]["unit"] == "DAY"
    assert trend["data"]["pipeline"]["nodes"][0]["grain"]["unit"] == "DAY"
    assert {entry.path for entry in changed.changeSet.changed} == {
        "/regions/region_hourly_trend/context/grain",
        "/regions/region_hourly_trend/name",
    }
    assert changed.draftAfterEdit.metadata["revision"] == 1
    assert definition.metadata["revision"] == 0
    assert region(definition, "region_hourly_trend")["name"] == "小时趋势"
    assert "日趋势" in changed.reply

    restored = apply(changed.draftAfterEdit, "按小时粒度", selected="region_hourly_trend")
    assert region(restored.draftAfterEdit, "region_hourly_trend")["name"] == "小时趋势"
    assert restored.draftAfterEdit.metadata["revision"] == 2


def test_trend_grain_preserves_custom_title(definition):
    region(definition, "region_hourly_trend")["name"] = "我的能耗变化"
    changed = apply(definition, "按天粒度", selected="region_hourly_trend")

    assert changed.needsClarification is False
    assert region(changed.draftAfterEdit, "region_hourly_trend")["name"] == "我的能耗变化"
    assert [entry.path for entry in changed.changeSet.changed] == [
        "/regions/region_hourly_trend/context/grain",
    ]


def test_day_grain_api_returns_updated_title(post_chat, definition):
    response = post_chat(
        "按天粒度",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": definition.model_dump(mode="json")},
        selectedRegionId="region_hourly_trend",
    ).json()

    assert response["state"] == "USER_EDITING_TEMPLATE"
    # 先给候选计划，等用户点「接受修改」才改草稿。
    assert response["uiActions"] == ["pending_edit_plan"]
    assert response["questions"][0]["id"] == "pending_edit_plan_confirm"
    assert response["domainObjects"]["pendingEditPlan"] is not None
    assert response["domainObjects"]["changeSet"] is None
    assert next(
        item for item in response["domainObjects"]["reportDefinitionDraft"]["regions"]
        if item["id"] == "region_hourly_trend"
    )["name"] == "小时趋势"

    accepted = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": definition.model_dump(mode="json")},
        selectedRegionId="region_hourly_trend",
    ).json()

    assert accepted["state"] == "USER_EDITING_TEMPLATE"
    assert accepted["uiActions"] == ["template_edit_applied", "confirm_template_edit"]
    assert next(
        item for item in accepted["domainObjects"]["reportDefinitionDraft"]["regions"]
        if item["id"] == "region_hourly_trend"
    )["name"] == "日趋势"
    assert {
        item["path"] for item in accepted["domainObjects"]["changeSet"]["changed"]
    } == {
        "/regions/region_hourly_trend/context/grain",
        "/regions/region_hourly_trend/name",
    }


def test_minute_grain_supported_after_switching_source(definition):
    source_result = apply(
        definition,
        "把趋势图的数据源换成 EAS 能耗明细",
    )
    grain_result = apply(
        source_result.draftAfterEdit,
        "把趋势图按分钟级 compile",
    )

    assert grain_result.needsClarification is False
    assert grain_result.plan.operation.supported is True
    assert (
        region(grain_result.draftAfterEdit, "region_hourly_trend")["context"][
            "grain"
        ]["value"]["unit"]
        == "MINUTE"
    )
    assert region(grain_result.draftAfterEdit, "region_hourly_trend")["name"] == "分钟趋势"


def test_suggest_visualization_returns_filtered_options(definition):
    result = apply(definition, "设备耗电排名可以怎么展示？")
    labels = [item["label"] for item in result.plan.operation.displayOptions]
    disabled = [item["label"] for item in result.plan.operation.disabledOptions]

    assert result.plan.intent == EditIntent.SUGGEST_VISUALIZATION
    assert labels == ["柱状图", "表格"]
    assert "饼图" in disabled
    assert result.changeSet is None


def test_change_metric_rejects_missing_source_field(definition):
    result = apply(definition, "把设备耗电排名的指标换成能耗成本")

    assert result.needsClarification is True
    assert result.changeSet is None
    assert result.draftAfterEdit.metadata["revision"] == 0
    assert "energyCost" in result.clarificationQuestion


@pytest.mark.parametrize(("intent", "operation"), [
    ("CHANGE_DATA_SOURCE", {"sourceRef": "eas_energy_detail"}),
    ("CHANGE_METRIC", {"metricRef": "metric.machine_room.device_energy"}),
])
def test_apply_rejects_unauthorized_references_without_mutation(definition, intent, operation):
    plan = EditPlan.model_validate({
        "intent": intent,
        "target": {"regionId": "region_total_energy"},
        "operation": operation,
    })
    denied = ToolScope(project_id="other_project", subject_ref="machine_room", subject_id="room_a")
    result = template_edit_engine.apply(plan=plan, definition=definition, scope=denied)
    assert result.needsClarification
    assert result.changeSet is None
    assert result.draftAfterEdit.metadata["revision"] == 0
    assert definition.metadata["revision"] == 0


def test_alias_matching_cannot_see_unauthorized_catalog(definition):
    denied = ToolScope(project_id="other_project", subject_ref="machine_room", subject_id="room_a")
    plan = template_edit_engine.parse(
        message="把趋势图的数据源换成 EAS 能耗明细",
        definition=definition,
        selected_region_id=None,
        scope=denied,
    )
    assert plan.operation.sourceRef is None
    assert plan.needsClarification


def test_change_metric_updates_registered_metric_card(definition):
    result = apply(definition, "把昨日总耗电的指标换成设备耗电")
    summary = region(result.draftAfterEdit, "region_total_energy")

    assert result.needsClarification is False
    assert summary["data"]["pipeline"]["nodes"][0]["metricRef"] == (
        "metric.machine_room.device_energy"
    )
    assert summary["component"]["metric"]["label"] == "设备耗电"


def test_change_source_rejects_missing_fields(definition):
    result = apply(definition, "把设备耗电排名的数据源换成能耗日汇总")

    assert result.needsClarification is True
    assert result.changeSet is None
    assert "deviceName" in result.clarificationQuestion


def test_rejects_visualization_not_matching_data_shape(definition):
    result = apply(definition, "把设备耗电排名改成饼图")

    assert result.needsClarification is True
    assert result.changeSet is None
    assert result.draftAfterEdit.metadata["revision"] == 0


def test_ranking_chart_preview_uses_device_ranking(definition):
    result = apply(definition, "把设备耗电排名改成柱状图")
    edited = result.draftAfterEdit.model_dump(mode="json")
    assert validate_definition(edited).passed
    _, record = PreviewService().run_preview(
        definitionId="test_definition", definitionDraft=edited
    )
    ranking = next(r for r in record.regions if r["regionId"] == "region_device_ranking")

    assert ranking["payload"]["chartKind"] == "BAR"
    assert ranking["payload"]["xLabels"] == [
        d.deviceName for d in record.snapshot.deviceRanking
    ]


def test_area_chart_passes_validation(definition):
    result = apply(definition, "把趋势图改成面积图")

    assert result.needsClarification is False
    assert validate_definition(result.draftAfterEdit.model_dump(mode="json")).passed


def test_trend_to_table_preview_uses_hourly_points(definition):
    result = apply(definition, "把趋势图改成表格")
    edited = result.draftAfterEdit.model_dump(mode="json")
    _, record = PreviewService().run_preview(
        definitionId="test_definition", definitionDraft=edited
    )
    trend = next(r for r in record.regions if r["regionId"] == "region_hourly_trend")

    assert trend["payload"]["columns"][0]["fieldRef"] == "timestamp"
    assert trend["payload"]["rows"][0]["timestamp"] == (
        record.snapshot.hourlyTrend[0].timestamp
    )


def test_add_section_uses_registered_metric_and_source(definition):
    result = apply(
        definition,
        "加一个总耗电对比柱状图 section",
    )

    assert result.plan.intent == EditIntent.ADD_SECTION
    assert result.needsClarification is False
    assert len(result.draftAfterEdit.regions) == len(definition.regions) + 1
    added = result.changeSet.changed[0].after
    assert added["component"]["type"] == "CHART"
    assert added["data"]["pipeline"]["nodes"][0]["metricRef"] == (
        "metric.machine_room.total_energy"
    )


def test_agent_api_applies_edit_and_returns_current_draft(post_chat, definition):
    response = post_chat(
        "把设备耗电排名改成柱状图",
        state="WAITING_USER_CONFIRM",
        designSessionId="session_edit",
        baseDefinitionRevision=0,
        snapshots={"reportDefinition": definition.model_dump(mode="json")},
    )
    data = response.json()

    assert response.status_code == 200
    assert data["state"] == "USER_EDITING_TEMPLATE"
    assert data["debug"]["editingIntent"] == "CHANGE_VISUALIZATION"
    # 候选阶段不改草稿：等用户点「接受修改」。
    assert data["uiActions"] == ["pending_edit_plan"]
    assert data["domainObjects"]["pendingEditPlan"] is not None
    assert data["domainObjects"]["changeSet"] is None
    assert data["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 0

    accepted = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        designSessionId="session_edit",
        snapshots={"reportDefinition": definition.model_dump(mode="json")},
    )
    applied = accepted.json()

    assert accepted.status_code == 200
    assert applied["state"] == "USER_EDITING_TEMPLATE"
    assert applied["domainObjects"]["changeSet"] is not None
    assert applied["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 1
    assert "confirm_template_edit" in applied["uiActions"]


def test_agent_api_keeps_editing_context_when_action_is_missing(
    post_chat, definition
):
    response = post_chat(
        "设备耗电排名这个 section",
        state="USER_EDITING_TEMPLATE",
        designSessionId="session_edit",
        snapshots={"reportDefinition": definition.model_dump(mode="json")},
    )
    data = response.json()

    assert data["state"] == "USER_EDITING_TEMPLATE"
    assert data["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 0
    assert data["domainObjects"]["changeSet"] is None
    assert "删除" in data["reply"]


def test_agent_api_can_confirm_edited_draft(post_chat, definition):
    definition.metadata["revision"] = 2
    response = post_chat(
        "同意",
        state="USER_EDITING_TEMPLATE",
        designSessionId="session_edit",
        snapshots={
            "reportPolicy": build_policy_draft(
                "A 机房能耗日报"
            ).model_dump(mode="json"),
            "reportDefinition": definition.model_dump(mode="json"),
        },
    )
    data = response.json()

    assert data["state"] == "SAVE_DEFINITION_DRAFT"
    assert data["toolRequests"][0]["type"] == "SAVE_DEFINITION_DRAFT"


# ---------- 组合修改（换源 + 改粒度）：一次确认、原子应用 ----------


def composite_plan(definition, *, region_id="region_hourly_trend", source_ref=None, grain_unit="MINUTE"):
    """构造一份「换源 + 改粒度」的组合计划。"""
    return EditPlan.model_validate({
        "intent": "CHANGE_COMPILATION_GRAIN",
        "target": {"regionId": region_id, "matchedText": "小时趋势", "confidence": 0.9},
        "operation": {"grain": {"unit": grain_unit}},
        "operations": [
            {
                "intent": "CHANGE_DATA_SOURCE",
                "operation": {"sourceRef": source_ref or "eas_energy_detail"},
                "summary": "数据源换成 EAS 能耗明细",
            },
            {
                "intent": "CHANGE_COMPILATION_GRAIN",
                "operation": {"grain": {"unit": grain_unit}},
                "summary": "compilation 粒度改成 MINUTE",
            },
        ],
    })


def test_composite_plan_source_and_grain_applies_atomically(definition):
    plan = composite_plan(definition)
    result = template_edit_engine.apply(plan=plan, definition=definition, scope=SCOPE)

    assert result.needsClarification is False
    assert result.changeSet is not None
    # 一次组合修改 = 一份 ChangeSet + revision 只 +1
    assert result.draftAfterEdit.metadata["revision"] == 1
    assert definition.metadata["revision"] == 0
    # 换源与改粒度两处都落到同一个 region
    assert {entry.regionId for entry in result.changeSet.changed} == {"region_hourly_trend"}
    assert {entry.path for entry in result.changeSet.changed} == {
        "/regions/region_hourly_trend/data/inputs/main/datasetRef",
        "/regions/region_hourly_trend/context/grain",
        "/regions/region_hourly_trend/name",
    }
    assert result.changeSet.baseDefinitionRevision == 0


def test_composite_plan_applies_grain_on_the_new_source(definition):
    """换源在前，粒度校验必须按新源的能力算。"""
    plan = composite_plan(definition)
    result = template_edit_engine.apply(plan=plan, definition=definition, scope=SCOPE)
    trend = region(result.draftAfterEdit, "region_hourly_trend")

    assert result.needsClarification is False
    assert trend["context"]["grain"]["value"]["unit"] == "MINUTE"
    assert trend["data"]["pipeline"]["nodes"][0]["grain"]["unit"] == "MINUTE"
    assert trend["name"] == "分钟趋势"
    source_ref = result.draftAfterEdit.datasets[
        trend["data"]["inputs"]["main"]["datasetRef"]
    ]["sourceRef"]
    assert source_ref == "eas_energy_detail"


def test_composite_plan_reorders_source_before_grain(definition):
    """步骤顺序由引擎决定：换源必须先于改粒度。"""
    plan = composite_plan(definition)
    plan.operations.reverse()
    result = template_edit_engine.apply(plan=plan, definition=definition, scope=SCOPE)

    assert result.needsClarification is False
    assert region(result.draftAfterEdit, "region_hourly_trend")["context"]["grain"][
        "value"
    ]["unit"] == "MINUTE"


def test_composite_plan_failure_leaves_draft_untouched(definition):
    """第二步失败时，第一步的换源也不能留下半成品。"""
    plan = composite_plan(definition, source_ref="catalog.machine_room.energy_daily_summary")
    result = template_edit_engine.apply(plan=plan, definition=definition, scope=SCOPE)

    assert result.needsClarification is True
    assert result.changeSet is None
    assert result.draftAfterEdit.metadata["revision"] == 0
    assert definition.metadata["revision"] == 0
    # 目标数据源缺 timestamp，字段兼容性校验必须拦下
    assert "timestamp" in result.clarificationQuestion
    # 目标区块的数据源没被换掉
    trend = region(result.draftAfterEdit, "region_hourly_trend")
    assert result.draftAfterEdit.datasets[
        trend["data"]["inputs"]["main"]["datasetRef"]
    ]["sourceRef"] == "catalog.machine_room.energy"
    assert trend["context"]["grain"]["value"]["unit"] == "HOUR"
    assert trend["name"] == "小时趋势"


def test_composite_plan_rejects_unauthorized_source(definition):
    plan = composite_plan(definition, source_ref="catalog.machine_room.energy_daily_summary")
    denied = ToolScope(project_id="other_project", subject_ref="machine_room", subject_id="room_a")
    result = template_edit_engine.apply(plan=plan, definition=definition, scope=denied)

    assert result.needsClarification is True
    assert result.changeSet is None
    assert result.draftAfterEdit.metadata["revision"] == 0
    assert definition.metadata["revision"] == 0
    assert "无权" in result.clarificationQuestion


def test_composite_plan_rejects_non_composable_intent(definition):
    plan = EditPlan.model_validate({
        "intent": "CHANGE_COMPILATION_GRAIN",
        "target": {"regionId": "region_hourly_trend"},
        "operations": [
            {"intent": "DELETE_SECTION", "operation": {}},
            {"intent": "CHANGE_COMPILATION_GRAIN", "operation": {"grain": {"unit": "MINUTE"}}},
        ],
    })
    result = template_edit_engine.apply(plan=plan, definition=definition, scope=SCOPE)

    assert result.needsClarification is True
    assert result.changeSet is None
    assert result.draftAfterEdit.metadata["revision"] == 0
    assert "DELETE_SECTION" in result.clarificationQuestion


def test_composite_plan_requires_target_region(definition):
    plan = composite_plan(definition)
    plan.target.regionId = ""
    result = template_edit_engine.apply(plan=plan, definition=definition, scope=SCOPE)

    assert result.needsClarification is True
    assert result.changeSet is None
    assert result.draftAfterEdit.metadata["revision"] == 0


def test_composite_grain_failure_exposes_supported_grains(definition):
    """粒度这一步失败时，把当前源支持的粒度带回顶层 operation。"""
    plan = EditPlan.model_validate({
        "intent": "CHANGE_COMPILATION_GRAIN",
        "target": {"regionId": "region_hourly_trend"},
        "operation": {"grain": {"unit": "MINUTE"}},
        "operations": [
            {
                "intent": "CHANGE_COMPILATION_GRAIN",
                "operation": {"grain": {"unit": "MINUTE"}},
            },
        ],
    })
    result = template_edit_engine.apply(plan=plan, definition=definition, scope=SCOPE)

    assert result.needsClarification is True
    assert result.changeSet is None
    assert result.plan.operation.supported is False
    assert result.plan.operation.supportedGrains == ["HOUR", "DAY"]


def test_composite_plan_summary_is_carried_on_result_plan(definition):
    """确认菜单要用的摘要随 EditPlan 一起带出去。"""
    plan = template_edit_engine.plan_source_and_grain_change(
        definition=definition,
        region_id="region_hourly_trend",
        grain_unit="MINUTE",
        scope=SCOPE,
    )
    result = template_edit_engine.apply(plan=plan, definition=definition, scope=SCOPE)

    assert result.plan.summary
    assert "eas_energy_detail" in result.plan.summary
    assert result.plan.model_dump(mode="json")["operations"]


def test_single_item_edit_still_works_after_composite_refactor(definition):
    """组合能力不能改变既有单项编辑行为。"""
    result = apply(definition, "把趋势图的数据源换成 EAS 能耗明细")

    assert result.plan.intent == EditIntent.CHANGE_DATA_SOURCE
    assert result.plan.operations == []
    assert result.needsClarification is False
    assert result.draftAfterEdit.metadata["revision"] == 1
    assert definition.metadata["revision"] == 0


def test_minute_grain_single_item_still_asks_for_clarification(definition):
    """单项改粒度仍按原样追问，不偷偷变成组合修改。"""
    result = apply(definition, "把趋势图按分钟级 compile")

    assert result.plan.intent == EditIntent.CHANGE_COMPILATION_GRAIN
    assert result.plan.operations == []
    assert result.needsClarification is True
    assert result.changeSet is None
    assert result.plan.operation.supportedGrains == ["HOUR", "DAY"]


# ---------- 组合候选计划的确定性构建 ----------


def test_plan_source_and_grain_change_builds_composite_candidate(definition):
    plan = template_edit_engine.plan_source_and_grain_change(
        definition=definition,
        region_id="region_hourly_trend",
        grain_unit="MINUTE",
        scope=SCOPE,
    )

    assert plan is not None
    assert [step.intent for step in plan.operations] == [
        EditIntent.CHANGE_DATA_SOURCE,
        EditIntent.CHANGE_COMPILATION_GRAIN,
    ]
    assert plan.operations[0].operation.sourceRef == "eas_energy_detail"
    assert plan.operations[1].operation.grain == {"unit": "MINUTE"}
    assert plan.intent == EditIntent.CHANGE_COMPILATION_GRAIN
    assert plan.operation.supportedGrains == ["MINUTE", "HOUR", "DAY"]


def test_plan_source_and_grain_change_summary_states_source_grain_and_limits(definition):
    plan = template_edit_engine.plan_source_and_grain_change(
        definition=definition,
        region_id="region_hourly_trend",
        grain_unit="MINUTE",
        scope=SCOPE,
    )

    assert "eas_energy_detail" in plan.summary
    assert "MINUTE" in plan.summary
    assert "小时趋势" in plan.summary
    # 不能声称真实编译已经完成
    assert "Mock" in plan.summary
    assert "尚未真实编译" in plan.summary


def test_plan_source_and_grain_change_returns_none_when_grain_already_supported(definition):
    plan = template_edit_engine.plan_source_and_grain_change(
        definition=definition,
        region_id="region_hourly_trend",
        grain_unit="DAY",
        scope=SCOPE,
    )

    assert plan is None


def test_plan_source_and_grain_change_returns_none_without_authorized_source(definition):
    denied = ToolScope(project_id="other_project", subject_ref="machine_room", subject_id="room_a")
    plan = template_edit_engine.plan_source_and_grain_change(
        definition=definition,
        region_id="region_hourly_trend",
        grain_unit="MINUTE",
        scope=denied,
    )

    assert plan is None


def test_plan_source_and_grain_change_skips_region_without_grain(definition):
    """设备耗电排名不按 compilation 粒度编译，没有粒度可改，不产出组合候选。"""
    plan = template_edit_engine.plan_source_and_grain_change(
        definition=definition,
        region_id="region_device_ranking",
        grain_unit="MINUTE",
        scope=SCOPE,
    )

    assert plan is None


def test_composite_plan_survives_pending_plan_storage(definition):
    """组合计划要能按待确认计划的契约存下来再取回来继续执行。"""
    from app.schemas.session import PendingEditPlan

    plan = template_edit_engine.plan_source_and_grain_change(
        definition=definition,
        region_id="region_hourly_trend",
        grain_unit="MINUTE",
        scope=SCOPE,
    )
    payload = plan.model_dump(mode="json")

    pending = PendingEditPlan(
        planId="plan-composite-1",
        draftRevision=definition.metadata["revision"],
        targetRegionId=plan.target.regionId,
        operations=payload["operations"],
        summary=payload["summary"],
    )
    restored = EditPlan.model_validate({
        "intent": payload["intent"],
        "target": payload["target"],
        "operation": payload["operation"],
        "operations": pending.operations,
        "summary": pending.summary,
    })
    result = template_edit_engine.apply(plan=restored, definition=definition, scope=SCOPE)

    assert result.needsClarification is False
    assert result.draftAfterEdit.metadata["revision"] == 1
    assert region(result.draftAfterEdit, "region_hourly_trend")["context"]["grain"][
        "value"
    ]["unit"] == "MINUTE"

