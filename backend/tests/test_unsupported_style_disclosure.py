"""未支持诉求的确定性披露：M-16 版式 / 标题样式没生效必须明确说出来。

对应本轮修复的缺陷 A3-P1-01：
    模型把版式诉求写进 collectedInfo 自由文本、定义的 styleHints 里一条都没有
    → 装配层无拒项 → metadata.unsupportedStyleRequests 未生成
    → 披露链路整条没被触发 → 回复只说「已作为样式要求一并提交」。

修法：披露不能依赖模型是否把诉求写进 styleHints。
原始输入、路由槽位、最终定义三处来源统一核对，缺一不可。
"""
from __future__ import annotations

from typing import Any

from app.agents.agentscope_report_agent import (
    AgentScopeReportAgent,
    _neutralize_applied_style_claims,
)
from app.schemas.agent import AgentRequest, AgentRequestContext, AgentState
from app.services import routing_rules
from app.services.report_draft_service import (
    build_definition_draft,
    compose_unsupported_style_disclosure,
)

#: M-16 原始输入。
M16 = (
    "生成 A 机房昨日能耗报表：总耗电卡片独占一行，排名表在左，"
    "小时耗电柱状图在右，左右等宽。标题22pt、左对齐、颜色RGB(58,95,171)。"
)

#: M-11 原始输入：行级条件样式（标红）是**支持**的能力，不能被误判。
M11 = "生成 A 机房昨日各设备耗电表，增加前一日耗电、日增长率，并将增长严格超过20%的设备行标红。"

#: 第一轮 / 第二轮反复跑过的正常请求。
STANDARD = "生成 A 机房昨日能耗日报，包含总耗电、设备排名和小时趋势分析。"
EXCLUSIVE = "生成 A 机房昨日能耗日报，只要总耗电和设备排名，不要趋势图。"
TEMPLATE = "生成 A 机房昨日能耗日报模版，包含总耗电、设备排名和趋势分析。"

#: Deep Router 把版式诉求写成的自由文本（第二轮 A3 实际观测到的形态）。
ROUTER_SLOT_IDS = [
    "total_card_occupy_full_row",
    "ranking_left_trend_right_equal_width",
    "title_font_size_22pt",
    "title_align_left",
    "title_color_RGB(58,95,171)",
]


def _request(message: str = STANDARD, collected: dict | None = None) -> AgentRequest:
    request = AgentRequest(
        conversationId="conv_style_disclosure",
        message=message,
        state=AgentState.ANALYZING,
        context=AgentRequestContext(
            projectId="project_001",
            subjectRef="machine_room",
            subjectId="room_a",
        ),
    )
    # collectedInfo 是服务端内部字段（PrivateAttr），只能走属性赋值
    request.collectedInfo = collected or {}
    return request


def _agent() -> Any:
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._fallback = None
    return agent


def _kinds(items: list[dict[str, Any]]) -> set[str]:
    return {str(item.get("kind")) for item in items}


# ------------------------------------------------------------------ 路由层


def test_m16_layout_and_title_requests_are_detected():
    """M-16 的 6 项诉求都要被确定性识别出来。"""
    items = routing_rules.detect_unsupported_style_requests(M16)
    assert _kinds(items) == {
        "LAYOUT_FULL_ROW",
        "LAYOUT_SIDE_BY_SIDE",
        "CHART_KIND",
        "TITLE_FONT_SIZE",
        "TITLE_ALIGN",
        "TITLE_COLOR",
    }
    labels = " ".join(item["label"] for item in items)
    assert "独占一行" in labels
    assert "左右并排等宽" in labels
    assert "柱状图" in labels
    assert "22pt" in labels
    assert "左对齐" in labels
    assert "RGB(58,95,171)" in labels


def test_legit_row_conditional_style_is_not_flagged_as_unsupported():
    """行级标红是支持的能力，不能被误判成「不支持」。"""
    assert routing_rules.detect_unsupported_style_requests(M11) == []


def test_normal_requests_are_not_flagged():
    """标准日报 / 排除趋势 / 带「模版」的日报都不该被当成未支持诉求。"""
    for message in (STANDARD, EXCLUSIVE, TEMPLATE):
        assert routing_rules.detect_unsupported_style_requests(message) == [], message


def test_router_slot_identifiers_are_detected():
    """Deep Router 把诉求写成标识符时也要识别得到。"""
    items = routing_rules.detect_unsupported_style_requests("\n".join(ROUTER_SLOT_IDS))
    assert _kinds(items) == {
        "LAYOUT_FULL_ROW",
        "LAYOUT_SIDE_BY_SIDE",
        "TITLE_FONT_SIZE",
        "TITLE_ALIGN",
        "TITLE_COLOR",
    }


def test_sources_are_unioned_and_deduplicated():
    """原始输入 + 路由槽位两处来源合并，同一诉求只留一条。"""
    merged = routing_rules.collect_unsupported_style_requests(
        message=M16,
        collected_info={"styleHints": ROUTER_SLOT_IDS},
    )
    assert len(merged) == 6
    assert len(_kinds(merged)) == 6


def test_explicit_slots_carries_unsupported_style_requests():
    """路由槽位里要能拿到这份清单（模板流据此交回正常路由）。"""
    slots = routing_rules.explicit_slots(M16)
    assert slots.get("unsupportedStyleRequests")
    assert "CHART_KIND" in _kinds(slots["unsupportedStyleRequests"])


def test_template_flow_defers_when_style_request_present():
    """模板流是固定模板，表达不了版式诉求 → 交回正常路由统一披露。"""
    slots = routing_rules.explicit_slots(M16)
    assert "unsupportedStyleRequests" in routing_rules.unsupported_slots(slots)
    # 没有版式诉求时，模板流照常处理，不受影响
    normal = routing_rules.explicit_slots(TEMPLATE)
    assert "unsupportedStyleRequests" not in routing_rules.unsupported_slots(normal)


# ------------------------------------------------------------------ 组装层


def test_definition_metadata_lists_unsupported_requests():
    """没生效的诉求要留在定义里，上层才有东西可以披露。"""
    detected = routing_rules.detect_unsupported_style_requests(M16)
    definition = build_definition_draft(
        "A 机房昨日能耗日报",
        "room_a",
        include_trend=True,
        unsupportedStyleRequests=detected,
    )
    requests = (definition.metadata or {}).get("unsupportedStyleRequests")
    assert requests
    assert len(requests) == 6
    assert all(item.get("label") for item in requests)


def test_model_rejected_style_hint_is_merged_not_duplicated():
    """模型把 title.fontSize 写进 styleHints：拒项要和确定性结果合并，不重复。"""
    detected = routing_rules.detect_unsupported_style_requests(M16)
    definition = build_definition_draft(
        "A 机房昨日能耗日报",
        "room_a",
        include_trend=True,
        unsupportedStyleRequests=detected,
        styleHints=[
            {
                "fieldRef": "title.fontSize",
                "rule": "HIGHLIGHT_ABOVE_THRESHOLD",
                "comparison": "GT",
                "threshold": 20,
                "style": "RED",
            }
        ],
    )
    requests = (definition.metadata or {}).get("unsupportedStyleRequests")
    assert len(requests) == 6
    assert len(_kinds(requests)) == 6


def test_legit_style_hint_is_kept_and_not_listed_as_unsupported():
    """合法的行级条件样式要保留，且不能出现在未支持清单里。

    注意契约：条件样式必须指向真实拿得到的字段 ——
    ``growthRatePercent`` 是派生列，所以派生列要一起声明。
    """
    definition = build_definition_draft(
        "A 机房昨日能耗日报",
        "room_a",
        include_trend=True,
        unsupportedStyleRequests=[],
        derivedColumns=[
            {
                "id": "growth",
                "label": "日增长率",
                "fieldRef": "growthRatePercent",
                "kind": "GROWTH_RATE",
                "sourceFieldRef": "energyKwh",
            }
        ],
        styleHints=[
            {
                "targetRegionId": "region_device_ranking",
                "fieldRef": "growthRatePercent",
                "rule": "HIGHLIGHT_ABOVE_THRESHOLD",
                "comparison": "GT",
                "threshold": 20,
                "style": "RED",
            }
        ],
    )
    requests = (definition.metadata or {}).get("unsupportedStyleRequests")
    assert not requests
    ranking = next(
        r for r in definition.regions if r["id"] == "region_device_ranking"
    )
    hints = (ranking.get("component") or {}).get("styleHints") or []
    assert hints
    assert hints[0]["fieldRef"] == "growthRatePercent"
    assert hints[0]["comparison"] == "GT"
    assert hints[0]["threshold"] == 20


def test_style_hint_on_unknown_field_is_reported_readably():
    """条件样式指向拿不到的字段时，要说人话，不能甩一个字段名。"""
    definition = build_definition_draft(
        "A 机房昨日能耗日报",
        "room_a",
        include_trend=True,
        styleHints=[
            {
                "targetRegionId": "region_device_ranking",
                "fieldRef": "growthRatePercent",
                "rule": "HIGHLIGHT_ABOVE_THRESHOLD",
                "comparison": "GT",
                "threshold": 20,
                "style": "RED",
            }
        ],
    )
    requests = (definition.metadata or {}).get("unsupportedStyleRequests")
    assert requests
    label = requests[0]["label"]
    assert label.startswith("条件样式")
    assert requests[0]["currentBehavior"]


# ------------------------------------------------------------------ 披露文案


def test_disclosure_lists_every_item_and_says_not_applied():
    """披露要说清楚「没有生效」，并逐项列出。"""
    detected = routing_rules.detect_unsupported_style_requests(M16)
    text = compose_unsupported_style_disclosure(detected)
    assert "没有生效" in text
    assert "当前模板不支持" in text
    assert "6 项" in text
    for item in detected:
        assert item["label"] in text
        assert item["currentBehavior"] in text


def test_disclosure_is_empty_when_nothing_unsupported():
    assert compose_unsupported_style_disclosure([]) == ""
    assert compose_unsupported_style_disclosure(None) == ""


def test_neutralize_rewrites_applied_claim():
    """「已作为样式要求一并提交」会被读成已做到，必须纠正。"""
    reply = (
        "你要求的布局（总耗电卡片独占一行）和标题样式（22pt）"
        "已作为样式要求一并提交。"
    )
    fixed = _neutralize_applied_style_claims(reply)
    assert "已作为样式要求一并提交" not in fixed
    assert "当前模板不支持，未生效" in fixed


def test_neutralize_keeps_normal_replies():
    """如实描述不能被误伤。"""
    for reply in (
        "已生成 A 机房昨日能耗日报 草稿，请确认。",
        "数据源用的是 catalog.machine_room.energy，已确认支持小时粒度。",
        "报表包含两块：① 总耗电指标卡；② 设备耗电排名表。",
        "如果要加上趋势图、调整排名行数，或者加同比/环比列，随时跟我说。",
    ):
        assert _neutralize_applied_style_claims(reply) == reply


# ------------------------------------------------------------------ Agent 层


def test_agent_discloses_even_when_model_writes_no_style_hints():
    """A3-P1-01 的核心回归：模型**没有**把诉求写进 styleHints 也要披露。"""
    response = _agent()._dispatch_submission(
        _request(message=M16),
        {
            "tool": "submit_draft_plan",
            "payload": {
                "reportName": "A 机房昨日能耗报表",
                "roomId": "room_a",
                "includeTrend": True,
                # 模型这次什么 styleHints 都没写
                "reply": (
                    "你要求的布局和标题样式已作为样式要求一并提交。"
                    "另外说明一下：当前趋势图默认生成为折线图，"
                    "你提到的是柱状图，如需改成柱状图随时告诉我。"
                ),
            },
        },
    )
    assert response.state == AgentState.WAITING_USER_CONFIRM
    reply = response.reply
    assert "没有生效" in reply
    assert "当前模板不支持" in reply
    assert "已作为样式要求一并提交" not in reply
    for label in (
        "版式：独占一行",
        "版式：左右并排等宽",
        "图形类型：柱状图",
        "标题样式：字号",
        "标题样式：对齐方式",
        "标题样式：颜色",
    ):
        assert label in reply, label
    # 定义里也要留档
    definition = response.domainObjects.reportDefinitionDraft
    requests = (definition.metadata or {}).get("unsupportedStyleRequests")
    assert requests and len(requests) == 6


def test_agent_discloses_when_model_puts_request_in_style_hints():
    """模型把诉求写进 styleHints（第一轮那条路径）也要披露，且不重复列。"""
    response = _agent()._dispatch_submission(
        _request(message=M16),
        {
            "tool": "submit_draft_plan",
            "payload": {
                "reportName": "A 机房昨日能耗报表",
                "roomId": "room_a",
                "includeTrend": True,
                "reply": "已生成草稿。",
                "styleHints": [
                    {
                        "fieldRef": "title.fontSize",
                        "rule": "HIGHLIGHT_ABOVE_THRESHOLD",
                        "comparison": "GT",
                        "threshold": 22,
                        "style": "RED",
                    },
                    {
                        "fieldRef": "layout.rankingTrendEqualSplit",
                        "rule": "HIGHLIGHT_ABOVE_THRESHOLD",
                        "comparison": "GT",
                        "threshold": 6,
                        "style": "RED",
                    },
                ],
            },
        },
    )
    assert response.state == AgentState.WAITING_USER_CONFIRM
    assert "没有生效" in response.reply
    definition = response.domainObjects.reportDefinitionDraft
    requests = (definition.metadata or {}).get("unsupportedStyleRequests")
    assert len(requests) == 6
    assert len(_kinds(requests)) == 6


def test_agent_discloses_from_router_slots_when_raw_input_is_generic():
    """原始输入没写版式、但路由槽位里有诉求时也要披露。

    Deep Router 实际写出来的 collectedInfo 里**没有**图形类型那一条
    （第二轮 A3 实测的 5 条），所以这里预期 5 项，不含柱状图。
    """
    response = _agent()._dispatch_submission(
        _request(message=STANDARD, collected={"styleHints": ROUTER_SLOT_IDS}),
        {
            "tool": "submit_draft_plan",
            "payload": {
                "reportName": "A 机房昨日能耗日报",
                "roomId": "room_a",
                "includeTrend": True,
                "reply": "已生成草稿。",
            },
        },
    )
    assert response.state == AgentState.WAITING_USER_CONFIRM
    assert "没有生效" in response.reply
    assert "5 项" in response.reply
    for label in (
        "版式：独占一行",
        "版式：左右并排等宽",
        "标题样式：字号",
        "标题样式：对齐方式",
        "标题样式：颜色",
    ):
        assert label in response.reply, label


def test_agent_reply_unchanged_for_normal_request():
    """正常请求不出现任何未生效提示。"""
    response = _agent()._dispatch_submission(
        _request(message=STANDARD),
        {
            "tool": "submit_draft_plan",
            "payload": {
                "reportName": "A 机房昨日能耗日报",
                "roomId": "room_a",
                "includeTrend": True,
                "reply": "已生成草稿，请确认。",
            },
        },
    )
    assert response.state == AgentState.WAITING_USER_CONFIRM
    assert "没有生效" not in response.reply
    assert response.reply == "已生成草稿，请确认。"
    definition = response.domainObjects.reportDefinitionDraft
    assert not (definition.metadata or {}).get("unsupportedStyleRequests")


def test_agent_reply_unchanged_for_legit_row_style_request():
    """M-11 的行级标红不受影响：不披露、样式保留。"""
    response = _agent()._dispatch_submission(
        _request(message=M11),
        {
            "tool": "submit_draft_plan",
            "payload": {
                "reportName": "A 机房昨日各设备耗电表",
                "roomId": "room_a",
                "includeTrend": False,
                "reply": "列：设备、耗电、前一日耗电、日增长率；增长超 20% 的行标红。",
            },
        },
    )
    assert response.state == AgentState.WAITING_USER_CONFIRM
    assert "没有生效" not in response.reply
    definition = response.domainObjects.reportDefinitionDraft
    assert not (definition.metadata or {}).get("unsupportedStyleRequests")
