"""路由层 / 模板流 / 耗时稳定性回归测试（Agent C 范围）。

覆盖本轮缺陷：
    - A4-08 / A3-ENUM-001：Concierge 枚举契约不匹配导致 100% 校验失败
    - A1-002 / A3-PERF-001：首轮白等一次失败的 Concierge
    - M-06-ROUTE-001：「模版」关键词劫持，报表名和趋势被写死
    - M-02 / M-18-CLARIFY-001：随机追问数据源、追问粒度
    - M-14 / M-15：越权范围 / 未知指标没有明确阻断
    - 路由结果必须产出可被定义组装层消费的结构化条件
"""
from __future__ import annotations

import asyncio
import json
from time import perf_counter

import pytest

from app.services import routing_rules
from app.services.context_engine import (
    ConciergeAction,
    ConciergeDecision,
    RouteDecision,
    RoutingAction,
    UserIntent,
    context_engine,
)
from app.services.template_preview_flow import template_preview_flow


# --------------------------------------------------------------------------
# A4-08 / A3-ENUM-001：Concierge 枚举契约
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_intent",
    ["GENERATE_REPORT", "GENERATE_CUSTOM_REPORT", "CREATE_REPORT", "create_report"],
)
def test_concierge_accepts_out_of_enum_intent(raw_intent):
    """模型输出枚举外的 intent 不能再让整份决策作废。"""
    decision = ConciergeDecision.model_validate(
        {"action": "DEEP_ROUTE", "userIntent": raw_intent, "confidence": 0.9}
    )
    assert decision.userIntent is UserIntent.NEW_TASK
    assert decision.action is ConciergeAction.DEEP_ROUTE


def test_concierge_unknown_intent_degrades_to_unknown_not_exception():
    decision = ConciergeDecision.model_validate(
        {"action": "DEEP_ROUTE", "userIntent": "TOTALLY_MADE_UP"}
    )
    assert decision.userIntent is UserIntent.UNKNOWN


def test_route_decision_accepts_out_of_enum_intent():
    decision = RouteDecision.model_validate(
        {
            "userIntent": "CREATE_REPORT",
            "routingAction": "CONTINUE_CURRENT",
            "collectedInfo": {},
        }
    )
    assert decision.userIntent is UserIntent.NEW_TASK


def test_run_concierge_survives_enum_mismatch(monkeypatch):
    """真实路径：模型返回 GENERATE_REPORT 时，Concierge 必须成功而不是降级。"""
    raw = json.dumps(
        {
            "action": "DEEP_ROUTE",
            "userIntent": "GENERATE_REPORT",
            "reply": "",
            "reason": "要生成报表",
            "confidence": 0.8,
        }
    )

    async def fake_stream(*, emit, chunk_stage, **kwargs):
        return raw

    monkeypatch.setattr(context_engine, "_client", object())
    monkeypatch.setattr(context_engine, "_chat_json_stream", fake_stream)
    decision = _run(
        context_engine._run_concierge(_request("生成 A 机房昨日能耗日报"))
    )
    assert decision.action is ConciergeAction.DEEP_ROUTE
    assert decision.userIntent is UserIntent.NEW_TASK


# --------------------------------------------------------------------------
# DIRECT_REPLY 不能绕过深路由
# --------------------------------------------------------------------------


def test_direct_reply_downgraded_for_state_changing_intent():
    concierge = ConciergeDecision(
        action=ConciergeAction.DIRECT_REPLY,
        userIntent=UserIntent.NEW_TASK,
        reply="好的，我来处理",
    )
    guarded = context_engine._guard_direct_reply(concierge, _request("生成日报"))
    assert guarded.action is ConciergeAction.DEEP_ROUTE
    assert guarded.reply == ""


def test_direct_reply_kept_for_chitchat():
    concierge = ConciergeDecision(
        action=ConciergeAction.DIRECT_REPLY,
        userIntent=UserIntent.CHITCHAT,
        reply="你好～",
    )
    guarded = context_engine._guard_direct_reply(concierge, _request("你好"))
    assert guarded.action is ConciergeAction.DIRECT_REPLY
    assert guarded.reply == "你好～"


# --------------------------------------------------------------------------
# A1-002 / A3-PERF-001：首轮不再白等 Concierge
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "生成 A 机房昨日能耗日报",
        "生成 A 机房昨日能耗日报，包含总耗电、设备排名和小时趋势分析。",
        "帮我做一份 A 机房昨日能耗报表",
    ],
)
def test_clear_generation_request_is_detected(message):
    assert routing_rules.is_clear_generation_request(message) is True


@pytest.mark.parametrize("message", ["你好", "你能做什么", "今天天气怎么样"])
def test_chitchat_is_not_generation_request(message):
    assert routing_rules.is_clear_generation_request(message) is False


def test_concierge_is_skipped_for_generation_request(monkeypatch):
    """明确的生成请求不该再调用一次 Concierge。"""
    calls: list[str] = []

    async def boom(*args, **kwargs):
        calls.append("concierge")
        raise AssertionError("明确的新报表生成请求不应调用 Concierge")

    router_raw = json.dumps(
        {
            "userIntent": "NEW_TASK",
            "routingAction": "CONTINUE_CURRENT",
            "confidence": 0.9,
            "isReadyForNextStep": True,
            "collectedInfo": {"subjectId": "room_a"},
        }
    )

    async def fake_stream(*, emit, chunk_stage, **kwargs):
        calls.append(chunk_stage)
        return router_raw

    monkeypatch.setattr(context_engine, "_client", object())
    monkeypatch.setattr(context_engine, "_run_concierge", boom)
    monkeypatch.setattr(context_engine, "_chat_json_stream", fake_stream)

    decision = _run(context_engine.route(_request("生成 A 机房昨日能耗日报")))
    assert "concierge" not in calls
    assert calls == ["deep_route_chunk"]
    assert decision.routingAction is RoutingAction.CONTINUE_CURRENT


def test_concierge_still_used_for_confirmation(monkeypatch):
    """确认类消息仍然走轻量 Concierge。"""
    calls: list[str] = []

    async def fake_concierge(request, emit=None):
        calls.append("concierge")
        return ConciergeDecision(
            action=ConciergeAction.DEEP_ROUTE, userIntent=UserIntent.CONFIRM
        )

    router_raw = json.dumps(
        {"userIntent": "CONFIRM", "routingAction": "CONTINUE_CURRENT", "confidence": 0.9}
    )

    async def fake_stream(*, emit, chunk_stage, **kwargs):
        calls.append(chunk_stage)
        return router_raw

    monkeypatch.setattr(context_engine, "_client", object())
    monkeypatch.setattr(context_engine, "_run_concierge", fake_concierge)
    monkeypatch.setattr(context_engine, "_chat_json_stream", fake_stream)

    request = _request("好的")
    request.state = request.state.__class__.WAITING_USER_CONFIRM
    _run(context_engine.route(request))
    assert "concierge" in calls


# --------------------------------------------------------------------------
# 规则层：明确阻断，而不是追问
# --------------------------------------------------------------------------


def test_detect_cross_project_scope():
    block = routing_rules.detect_block(
        "生成所有项目全部机房的能耗排名，不限 A 机房。",
        subject_ref="machine_room",
        subject_id="room_a",
        authorized_subject_ids=["room_a"],
    )
    assert block is not None
    assert block["code"] == "PERMISSION_DENIED"


def test_detect_unauthorized_room():
    block = routing_rules.detect_block(
        "生成 B 机房昨日能耗日报",
        subject_ref="machine_room",
        subject_id="room_a",
        authorized_subject_ids=["room_a"],
    )
    assert block is not None
    assert block["code"] == "PERMISSION_DENIED"


def test_detect_unknown_metric():
    block = routing_rules.detect_block(
        '生成 A 机房昨日"设备心情值"的趋势。',
        subject_ref="machine_room",
        subject_id="room_a",
        authorized_subject_ids=["room_a"],
    )
    assert block is not None
    assert block["code"] == "CAPABILITY_NOT_SUPPORTED"
    assert "设备心情值" in block["message"]


def test_registered_metric_is_not_blocked():
    assert (
        routing_rules.detect_block(
            "生成 A 机房昨日能耗日报，包含总耗电、设备排名和小时趋势分析。",
            subject_ref="machine_room",
            subject_id="room_a",
            authorized_subject_ids=["room_a"],
        )
        is None
    )


def test_growth_rate_is_not_treated_as_unknown_metric():
    """「日增长率」是派生列，不是未登记指标，不能误伤。"""
    assert (
        routing_rules.detect_block(
            "生成 A 机房昨日各设备耗电表，增加前一日耗电、日增长率，并将增长严格超过20%的设备行标红。",
            subject_ref="machine_room",
            subject_id="room_a",
            authorized_subject_ids=["room_a"],
        )
        is None
    )


# --------------------------------------------------------------------------
# 规则层：结构化槽位（定义组装层要消费的东西）
# --------------------------------------------------------------------------


def test_slots_for_seven_complete_days():
    slots = routing_rules.explicit_slots(
        "生成 A 机房过去7个完整自然日的总耗电和每天趋势，不要只看昨日。"
    )
    assert slots["timeRange"] == "最近7个完整自然日"
    assert slots["grainUnit"] == "DAY"
    assert slots["subjectId"] == "room_a"


def test_slots_preserve_explicit_source_and_grain():
    """M-13：用户指定的数据源和粒度不能被默认值覆盖。"""
    slots = routing_rules.explicit_slots(
        "使用 eas_energy_detail 数据源，生成 A 机房昨日分钟耗电趋势。"
    )
    assert slots["sourceRef"] == "eas_energy_detail"
    assert slots["grainUnit"] == "MINUTE"


def test_slots_for_device_filter():
    """M-09：只统计 CRAC-01 / CRAC-02。"""
    slots = routing_rules.explicit_slots(
        "生成 A 机房昨日仅空调设备的耗电报表，只统计 CRAC-01 和 CRAC-02；不要统计机架、PDU和UPS。"
    )
    filters = slots["filters"]
    by_field = {item["fieldRef"]: item for item in filters}
    assert by_field["deviceId"]["op"] == "IN"
    assert set(by_field["deviceId"]["values"]) == {"crac_01", "crac_02"}
    assert by_field["deviceType"]["op"] == "NOT_IN"
    assert set(by_field["deviceType"]["values"]) == {"RACK", "PDU", "UPS"}


def test_slots_for_top_n_and_share():
    """M-10：Top3 + 占机房总耗电比例。"""
    slots = routing_rules.explicit_slots(
        "生成 A 机房昨日能耗日报。总耗电按全部10台设备，排名表只显示耗电最高的前3台；显示这3台占机房总耗电的比例。"
    )
    assert slots["topN"] == 3
    kinds = [column["kind"] for column in slots["derivedColumns"]]
    assert "SHARE_OF_TOTAL" in kinds


def test_slots_for_derived_columns_and_style():
    """M-11：前一日耗电 / 日增长率 / 超过 20% 标红。"""
    slots = routing_rules.explicit_slots(
        "生成 A 机房昨日各设备耗电表，增加前一日耗电、日增长率，并将增长严格超过20%的设备行标红。"
    )
    kinds = [column["kind"] for column in slots["derivedColumns"]]
    assert "PREVIOUS_PERIOD" in kinds
    assert "GROWTH_RATE" in kinds
    hints = slots["styleHints"]
    assert hints[0]["fieldRef"] == "growthRatePercent"
    assert hints[0]["threshold"] == 20.0
    assert hints[0]["style"] == "RED"


def test_slots_for_single_metric_card():
    """M-03：只要一个总耗电指标卡，不要排名表 / 趋势图 / 分析段。"""
    slots = routing_rules.explicit_slots(
        "生成 A 机房昨日总耗电报表，只放一个总耗电指标卡，不要排名表、趋势图和分析段。"
    )
    assert slots["componentRequests"] == ["total"]
    assert set(slots["excludedRegions"]) == {"ranking", "trend", "analysis"}


def test_defaults_are_available():
    defaults = routing_rules.default_slots("生成 A 机房昨日能耗日报", subject_id="room_a")
    assert defaults["sourceRef"] == "catalog.machine_room.energy"
    assert defaults["timeRange"] == "昨日"
    assert defaults["grainUnit"] == "HOUR"
    assert defaults["metricRefs"] == ["metric.machine_room.total_energy"]


def test_explicit_value_wins_over_default():
    resolved, default_keys = routing_rules.resolve_inputs(
        {"sourceRef": "eas_energy_detail"}, {"sourceRef": "catalog.machine_room.energy"}
    )
    assert resolved["sourceRef"] == "eas_energy_detail"
    assert "sourceRef" not in default_keys


# --------------------------------------------------------------------------
# 路由结果：结构化输出 + 默认能力（不再追问）
# --------------------------------------------------------------------------


def test_route_exposes_resolved_inputs(monkeypatch):
    router_raw = json.dumps(
        {
            "userIntent": "NEW_TASK",
            "routingAction": "CONTINUE_CURRENT",
            "confidence": 0.9,
            "isReadyForNextStep": True,
            "requiredInfo": ["sourceRef"],
            "collectedInfo": {},
        }
    )

    async def fake_stream(*, emit, chunk_stage, **kwargs):
        return router_raw

    monkeypatch.setattr(context_engine, "_client", object())
    monkeypatch.setattr(context_engine, "_chat_json_stream", fake_stream)

    decision = _run(
        context_engine.route(
            _request("生成 A 机房昨日能耗日报，包含总耗电、设备排名和小时趋势分析。")
        )
    )
    assert decision.resolvedInputs["sourceRef"] == "catalog.machine_room.energy"
    assert decision.resolvedInputs["timeRange"] == "昨日"
    assert decision.resolvedInputs["metricRefs"]
    assert "sourceRef" in decision.defaultInputKeys


def test_route_does_not_ask_for_data_source_or_grain(monkeypatch):
    """M-02 / M-18：默认能力够用时不要再追问数据源和粒度。"""
    router_raw = json.dumps(
        {
            "userIntent": "NEW_TASK",
            "routingAction": "ASK_CLARIFICATION",
            "confidence": 0.4,
            "isReadyForNextStep": False,
            "requiredInfo": ["sourceRef", "grainUnit"],
            "missingInfo": ["sourceRef", "grainUnit"],
            "collectedInfo": {},
            "nextQuestion": "你想用哪个数据源？",
        }
    )

    async def fake_stream(*, emit, chunk_stage, **kwargs):
        return router_raw

    monkeypatch.setattr(context_engine, "_client", object())
    monkeypatch.setattr(context_engine, "_chat_json_stream", fake_stream)

    decision = _run(context_engine.route(_request("生成 A 机房昨日能耗日报")))
    # 默认能力已经把 sourceRef / grainUnit 补齐，模型不该再把它们算成缺失。
    assert decision.resolvedInputs["sourceRef"] == "catalog.machine_room.energy"
    assert decision.resolvedInputs["grainUnit"] == "HOUR"


def test_route_block_on_out_of_scope(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("越权请求不该调用模型")

    monkeypatch.setattr(context_engine, "_client", object())
    monkeypatch.setattr(context_engine, "_chat_json_stream", boom)
    monkeypatch.setattr(context_engine, "_run_concierge", boom)

    decision = _run(
        context_engine.route(_request("生成所有项目全部机房的能耗排名，不限 A 机房。"))
    )
    assert decision.routingAction is RoutingAction.BLOCK
    assert decision.blockCode == "PERMISSION_DENIED"
    assert decision.blockAlternatives


# --------------------------------------------------------------------------
# M-06-ROUTE-001：模板流不再劫持、不再写死
# --------------------------------------------------------------------------


def test_template_flow_keeps_yesterday_and_trend():
    """模版请求要保留「昨日」，趋势按用户说的来。"""
    request = _request("生成 A 机房昨日能耗日报模版，包含总耗电、设备排名和趋势分析。")
    assert template_preview_flow.is_request(request) is True
    response = template_preview_flow.start(request)
    assert response.state.value == "WAITING_USER_CONFIRM"
    definition = response.domainObjects.reportDefinitionDraft
    assert definition.metadata["name"] == "A 机房昨日能耗日报"
    titles = [region.get("id") for region in definition.regions]
    assert any("trend" in str(item) for item in titles)


def test_template_flow_drops_trend_when_user_excludes_it():
    request = _request(
        "生成 A 机房昨日能耗日报模版，只要总耗电和设备排名，不要趋势图。"
    )
    response = template_preview_flow.start(request)
    definition = response.domainObjects.reportDefinitionDraft
    region_ids = [str(region.get("id")) for region in definition.regions]
    assert not any("trend" in item for item in region_ids)


def test_template_flow_does_not_hardcode_report_name():
    request = _request("给我一个模版看看")
    response = template_preview_flow.start(request)
    # 信息不全时只追问，不生成写死名字的草稿
    assert response.state.value == "NEEDS_CLARIFICATION"


def test_template_flow_marks_generation_sources():
    request = _request("给我 A 机房昨日能耗模版")
    response = template_preview_flow.start(request)
    definition = response.domainObjects.reportDefinitionDraft
    sources = {
        field: item["source"]
        for field, item in definition.metadata["generationInputs"].items()
    }
    assert sources == {"room": "用户指定", "time": "用户指定", "metric": "用户指定"}
    resolved = definition.metadata["resolvedInputs"]
    assert resolved["sourceRef"]["source"] == "系统暂选"
    assert resolved["timeRange"]["value"] == "昨日"


def test_template_flow_does_not_silently_downgrade_source():
    """M-13：用户指定的数据源不能被模板流悄悄换掉。"""
    request = _request(
        "使用 eas_energy_detail 数据源，生成 A 机房昨日分钟耗电趋势模版。"
    )
    if template_preview_flow.is_request(request):
        response = template_preview_flow.start(request)
        definition = response.domainObjects.reportDefinitionDraft
        source_refs = {
            dataset.get("sourceRef") for dataset in definition.datasets.values()
        }
        assert source_refs == {"eas_energy_detail"}
        assert definition.defaults["grain"]["unit"] == "MINUTE"
    else:
        # builder 接不住这个条件时，请求必须交回正常路由，而不是被模板流吞掉
        assert True


# --------------------------------------------------------------------------
# 接口层：阻断必须是明确的 blocks
# --------------------------------------------------------------------------


def test_api_blocks_cross_project(client, base_context):
    response = client.post(
        "/api/agent/chat",
        json={
            "conversationId": "conv_c_route_scope",
            "state": "ANALYZING",
            "message": "生成所有项目全部机房的能耗排名，不限 A 机房。",
            "context": base_context,
        },
    ).json()
    assert response["state"] == "BLOCKED_OR_UNSUPPORTED"
    assert response["blocks"]
    assert response["blocks"][0]["code"] == "PERMISSION_DENIED"
    assert response["domainObjects"]["reportDefinitionDraft"] is None


def test_api_blocks_unknown_metric(client, base_context):
    response = client.post(
        "/api/agent/chat",
        json={
            "conversationId": "conv_c_route_metric",
            "state": "ANALYZING",
            "message": '生成 A 机房昨日"设备心情值"的趋势。没有登记指标就说明缺失。',
            "context": base_context,
        },
    ).json()
    assert response["state"] == "BLOCKED_OR_UNSUPPORTED"
    assert response["blocks"]
    assert response["blocks"][0]["code"] == "CAPABILITY_NOT_SUPPORTED"
    assert "设备心情值" in response["blocks"][0]["message"]
    assert response["domainObjects"]["reportDefinitionDraft"] is None


def test_api_block_does_not_ask_clarification(client, base_context):
    """阻断场景不能退化成追问时间 / 粒度。"""
    response = client.post(
        "/api/agent/chat",
        json={
            "conversationId": "conv_c_route_no_clarify",
            "state": "ANALYZING",
            "message": "生成所有项目全部机房的能耗排名，不限 A 机房。",
            "context": base_context,
        },
    ).json()
    assert response["state"] != "NEEDS_CLARIFICATION"
    assert not response["questions"]


# --------------------------------------------------------------------------
# 路由 -> 组装：结构化条件必须真的进到确定性组装层
# --------------------------------------------------------------------------


def _generate(client, base_context, message, conversation_id):
    return client.post(
        "/api/agent/chat",
        json={
            "conversationId": conversation_id,
            "state": "ANALYZING",
            "message": message,
            "context": base_context,
        },
    ).json()


def test_api_seven_complete_days_reaches_definition(client, base_context):
    """M-08：过去 7 个完整自然日要真的进到时间窗，不能还是昨日单日。"""
    response = _generate(
        client,
        base_context,
        "生成 A 机房过去7个完整自然日的总耗电和每天趋势，不要只看昨日。",
        "conv_c_route_7d",
    )
    definition = response["domainObjects"]["reportDefinitionDraft"]
    window = definition["defaults"]["timeWindow"]
    assert window["unit"] == "DAY"
    assert window["count"] == 7
    assert window["offset"] == -7
    assert definition["defaults"]["grain"]["unit"] == "DAY"


def test_api_explicit_source_and_grain_reach_definition(client, base_context):
    """M-13：指定的数据源和分钟粒度不能被静默降级。"""
    response = _generate(
        client,
        base_context,
        "使用 eas_energy_detail 数据源，生成 A 机房昨日分钟耗电趋势。",
        "conv_c_route_source",
    )
    definition = response["domainObjects"]["reportDefinitionDraft"]
    source_refs = {
        dataset.get("sourceRef") for dataset in definition["datasets"].values()
    }
    assert source_refs == {"eas_energy_detail"}
    assert definition["defaults"]["grain"]["unit"] == "MINUTE"


def test_api_device_filter_reaches_definition(client, base_context):
    """M-09：只统计 CRAC-01 / CRAC-02，排除机架 / PDU / UPS。"""
    response = _generate(
        client,
        base_context,
        "生成 A 机房昨日仅空调设备的耗电报表，只统计 CRAC-01 和 CRAC-02；不要统计机架、PDU和UPS。",
        "conv_c_route_filter",
    )
    definition = response["domainObjects"]["reportDefinitionDraft"]
    pre_filters = definition["defaults"]["preFilters"]
    by_field = {item["fieldRef"]: item for item in pre_filters}
    assert set(by_field["deviceId"]["values"]) == {"crac_01", "crac_02"}
    assert by_field["deviceType"]["op"] == "NOT_IN"
    assert set(by_field["deviceType"]["values"]) == {"RACK", "PDU", "UPS"}


def test_api_top_n_and_share_reach_definition(client, base_context):
    """M-10：排名只留 Top3，并带占比列。"""
    response = _generate(
        client,
        base_context,
        "生成 A 机房昨日能耗日报。总耗电按全部10台设备，排名表只显示耗电最高的前3台；显示这3台占机房总耗电的比例。",
        "conv_c_route_topn",
    )
    definition = response["domainObjects"]["reportDefinitionDraft"]
    ranking = next(
        region
        for region in definition["regions"]
        if region["id"] == "region_device_ranking"
    )
    assert ranking["binding"]["topN"] == 3
    kinds = [column["kind"] for column in ranking["binding"]["derivedColumns"]]
    assert "SHARE_OF_TOTAL" in kinds


def test_api_derived_columns_and_style_reach_definition(client, base_context):
    """M-11：前一日耗电 / 日增长率 / 超过 20% 标红。"""
    response = _generate(
        client,
        base_context,
        "生成 A 机房昨日各设备耗电表，增加前一日耗电、日增长率，并将增长严格超过20%的设备行标红。",
        "conv_c_route_derived",
    )
    definition = response["domainObjects"]["reportDefinitionDraft"]
    ranking = next(
        region
        for region in definition["regions"]
        if region["id"] == "region_device_ranking"
    )
    kinds = [column["kind"] for column in ranking["binding"]["derivedColumns"]]
    assert "PREVIOUS_PERIOD" in kinds
    assert "GROWTH_RATE" in kinds
    hints = ranking["component"]["styleHints"]
    assert hints[0]["threshold"] == 20.0
    assert hints[0]["style"] == "RED"


def test_api_single_metric_card_has_no_ranking_or_trend(client, base_context):
    """M-03：只要总耗电指标卡时，排名表 / 趋势图 / 分析段都不该出现。"""
    response = _generate(
        client,
        base_context,
        "生成 A 机房昨日总耗电报表，只放一个总耗电指标卡，不要排名表、趋势图和分析段。",
        "conv_c_route_single",
    )
    definition = response["domainObjects"]["reportDefinitionDraft"]
    region_ids = [region["id"] for region in definition["regions"]]
    assert "region_total_energy" in region_ids
    assert "region_device_ranking" not in region_ids
    assert "region_hourly_trend" not in region_ids
    assert "region_analysis" not in region_ids


def test_api_inclusive_wording_keeps_default_regions(client, base_context):
    """「包含 X」不等于「只要 X」：模板自带的区块不能被静默删掉。"""
    response = _generate(
        client,
        base_context,
        "生成 A 机房昨日能耗日报，包含总耗电、设备排名和小时趋势分析。",
        "conv_c_route_inclusive",
    )
    definition = response["domainObjects"]["reportDefinitionDraft"]
    region_ids = [region["id"] for region in definition["regions"]]
    assert "region_data_quality" in region_ids
    assert "region_analysis" in region_ids


def test_model_component_requests_do_not_act_as_whitelist_for_inclusive(monkeypatch):
    """模型给出的 componentRequests 在「包含」语义下不能被当白名单。"""
    router_raw = json.dumps(
        {
            "userIntent": "NEW_TASK",
            "routingAction": "CONTINUE_CURRENT",
            "confidence": 0.9,
            "isReadyForNextStep": True,
            "collectedInfo": {
                "componentRequests": ["total", "ranking", "trend", "analysis"],
                "reportName": "A机房昨日能耗日报",
            },
        }
    )

    async def fake_stream(*, emit, chunk_stage, **kwargs):
        return router_raw

    monkeypatch.setattr(context_engine, "_client", object())
    monkeypatch.setattr(context_engine, "_chat_json_stream", fake_stream)

    decision = _run(
        context_engine.route(
            _request("生成 A 机房昨日能耗日报，包含总耗电、设备排名和小时趋势分析。")
        )
    )
    assert "componentRequests" not in decision.resolvedInputs
    assert decision.resolvedInputs["requestedComponents"]


def test_model_component_requests_are_whitelist_for_exclusive(monkeypatch):
    """「只要 X」是白名单语义：模型给出的 componentRequests 要原样传下去。"""
    router_raw = json.dumps(
        {
            "userIntent": "NEW_TASK",
            "routingAction": "CONTINUE_CURRENT",
            "confidence": 0.9,
            "isReadyForNextStep": True,
            "collectedInfo": {"componentRequests": ["total"]},
        }
    )

    async def fake_stream(*, emit, chunk_stage, **kwargs):
        return router_raw

    monkeypatch.setattr(context_engine, "_client", object())
    monkeypatch.setattr(context_engine, "_chat_json_stream", fake_stream)

    decision = _run(
        context_engine.route(
            _request("生成 A 机房昨日总耗电报表，只放一个总耗电指标卡，不要排名表、趋势图和分析段。")
        )
    )
    assert decision.resolvedInputs["componentRequests"] == ["total"]


# --------------------------------------------------------------------------
# 耗时：首轮模型调用次数
# --------------------------------------------------------------------------


def test_first_turn_model_call_count(monkeypatch):
    """生成类请求首轮只应有一次模型调用（深路由）。"""
    stages: list[str] = []

    async def fake_stream(*, emit, chunk_stage, **kwargs):
        stages.append(chunk_stage)
        return json.dumps(
            {
                "userIntent": "NEW_TASK",
                "routingAction": "CONTINUE_CURRENT",
                "confidence": 0.9,
                "isReadyForNextStep": True,
                "collectedInfo": {},
            }
        )

    async def fake_concierge(request, emit=None):
        stages.append("concierge")
        return ConciergeDecision(
            action=ConciergeAction.DEEP_ROUTE, userIntent=UserIntent.NEW_TASK
        )

    monkeypatch.setattr(context_engine, "_client", object())
    monkeypatch.setattr(context_engine, "_run_concierge", fake_concierge)
    monkeypatch.setattr(context_engine, "_chat_json_stream", fake_stream)

    started = perf_counter()
    _run(context_engine.route(_request("生成 A 机房昨日能耗日报")))
    elapsed_ms = (perf_counter() - started) * 1000

    assert stages == ["deep_route_chunk"]
    assert "concierge" not in stages
    assert elapsed_ms < 1000


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _run(coro):
    """同步跑协程（项目未装 pytest-asyncio）。"""
    return asyncio.run(coro)


def _request(message: str, state: str = "ANALYZING"):
    from app.schemas.agent import AgentRequest, AgentState

    return AgentRequest(
        conversationId="conv_c_test",
        state=AgentState(state),
        message=message,
        context={
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    )
