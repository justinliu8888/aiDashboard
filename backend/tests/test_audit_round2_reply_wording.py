"""审查第二轮（#51 / #53）：回复正文不得把**授权范围**类占位说成「能力未接通」。

规格 #46 第 246 行：

    「``kind`` 来自方案的指标意图和系统语义归一化，不在造数/渲染阶段只靠
    区域标题猜测。**能力待接通与范围授权、既有绑定三态相互独立；不把所有
    占位原因都标为能力缺失**，不突破授权范围或暴露真实数据。」

缺陷：回复正文原来按「区域是不是占位区」统计「有几个区域的能力尚未接通」，
于是**统计对象不在授权范围内**的那类区域也被算成能力缺失。

本文件覆盖：

    - 判据层（``report_template_service``）：能力类 / 授权类分别识别；
    - 文案层（``report_design_service`` / ``agent_service``）：两类分别计数、
      措辞互不混淆，且不再出现旧口径「数据待连接」。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.schemas.agent import AgentRequest, AgentState
from app.services.report_design_service import (
    pending_notes_reply,
    placeholder_reply,
    report_design_service,
)
from app.services.report_template_service import (
    SCOPE_PLACEHOLDER_REASON,
    build_template_design,
    region_capability_pending,
    split_placeholder_regions,
)

#: 旧口径：占位一律说「数据待连接」。它已经被「能力待接通」取代，
#: 任何回复正文里都不该再出现。
OLD_WORDING = "数据待连接"

#: 能力类的说法。
CAPABILITY_SAYINGS = ("能力尚未接通", "能力待接通")

#: 授权类的说法（与能力类**明确区分**）。
SCOPE_SAYINGS = ("不在授权范围", "授权范围")


# --------------------------------------------------------------------------
# 夹具：按**真实领域层**造出模板
# --------------------------------------------------------------------------


def _plan(*sections: dict[str, Any], subject: str | None = None) -> dict[str, Any]:
    return {
        "id": "rplan_probe",
        "revision": 1,
        "topic": "探针报表",
        "requirements": {
            "subject": subject,
            "metrics": [s["title"] for s in sections],
            "timeRange": None,
            "exclusions": [],
        },
        "sections": list(sections),
    }


def _section(section_id: str, title: str, intent: str) -> dict[str, Any]:
    return {
        "id": section_id,
        "title": title,
        "metricIntents": [intent],
        "componentType": "metric_card",
    }


def _template(subject: str | None, *sections: dict[str, Any]) -> dict[str, Any]:
    """用**真实**的模板层造一份模板（``model_dump`` 后的形状）。

    ``subject`` 给一个**不在授权范围内**的机房 → 该区域是授权类占位；
    给 ``None`` → 走已授权机房，能力算不出来的区域是能力类占位。
    """
    plan = _plan(*sections, subject=subject)
    subject_id = "room_b" if subject else "room_a"
    design = build_template_design(plan, subject_id=subject_id)
    return design.model_dump(mode="json")


def _request() -> AgentRequest:
    return AgentRequest(
        conversationId="conv_probe",
        sessionId="sess_probe",
        messageId="msg_probe",
        state=AgentState.ANALYZING,
        message="确认",
        context={
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    )


def _capability_template() -> dict[str, Any]:
    """只有能力类占位（PUE 在册但没有底层字段）。"""
    return _template(None, _section("s1", "PUE 总览", "PUE"))


def _scope_template() -> dict[str, Any]:
    """只有授权类占位（统计对象不在授权范围内）。"""
    return _template("机房B", _section("s1", "总耗电概览", "总耗电"))


def _mixed_template() -> dict[str, Any]:
    """两类都有：总耗电越权（授权类）+ 实时功率没接通（能力类）。"""
    return _template(
        "机房B",
        _section("s1", "总耗电概览", "总耗电"),
        _section("s2", "实时功率", "实时功率"),
    )


def _bound_template() -> dict[str, Any]:
    """既有绑定区域（总耗电，机房已授权）。"""
    return _template(None, _section("s1", "总耗电概览", "总耗电"))


# --------------------------------------------------------------------------
# 1. 判据层：能力类 / 授权类分别识别
# --------------------------------------------------------------------------


def test_scope_region_is_not_capability_pending():
    """授权类占位：能力是通的，``capabilityPending`` 必须是 False。

    这条是整个修复的地基 —— 规格第 246 行明写「不把所有占位原因都标为
    能力缺失」。
    """
    template = _scope_template()
    region = template["regions"][0]
    assert region["dataStatus"] == "placeholder"
    assert region["capabilityPending"] is False
    assert region_capability_pending(region) is False


def test_capability_region_is_capability_pending():
    """能力类占位：``capabilityPending`` 为 True。"""
    template = _capability_template()
    region = template["regions"][0]
    assert region["dataStatus"] == "placeholder"
    assert region["capabilityPending"] is True
    assert region_capability_pending(region) is True


def test_split_placeholder_regions_counts_the_two_kinds_separately():
    """两类占位**分别**归桶，不混算；非占位区不进这两桶。"""
    capability, scope = split_placeholder_regions(_mixed_template()["regions"])
    assert [r["title"] for r in capability] == ["实时功率"]
    assert [r["title"] for r in scope] == ["总耗电概览"]

    bound = _bound_template()["regions"]
    assert split_placeholder_regions(bound) == ([], [])


def test_bound_region_is_neither_capability_nor_scope():
    """既有绑定是第三态：既不是能力缺失，也不是授权问题。"""
    region = _bound_template()["regions"][0]
    assert region["dataStatus"] == "bound"
    assert region_capability_pending(region) is False


def test_definition_layer_uses_preview_spec_capability_pending():
    """定义层按 ``templateBinding.previewSpec`` 判（规格第 245 行）。

    ``previewSpec`` 的区域级布尔是各项指标的**逻辑或** —— 这条判据在
    定义层与模板层指的是同一件事。
    """
    capability_region = {
        "templateBinding": {
            "dataStatus": "placeholder",
            "placeholderReason": "「PUE」口径待核实：当前数据源没有登记这个字段，算不出数值。",
            "previewSpec": {
                "capabilityPending": True,
                "metrics": [{"metricKey": "m1", "capabilityPending": True}],
            },
        }
    }
    scope_region = {
        "templateBinding": {
            "dataStatus": "placeholder",
            "placeholderReason": SCOPE_PLACEHOLDER_REASON,
            "previewSpec": {
                "capabilityPending": False,
                "metrics": [{"metricKey": "m1", "capabilityPending": False}],
            },
        }
    }
    assert region_capability_pending(capability_region) is True
    assert region_capability_pending(scope_region) is False
    capability, scope = split_placeholder_regions([capability_region, scope_region])
    assert len(capability) == 1 and len(scope) == 1


def test_preview_spec_without_region_flag_falls_back_to_metric_logical_or():
    """``previewSpec`` 只有 ``metrics[]`` 时：区域能力布尔 = 各项逻辑或。"""
    region = {
        "templateBinding": {
            "dataStatus": "placeholder",
            "placeholderReason": "口径待核实",
            "previewSpec": {
                "metrics": [
                    {"metricKey": "m1", "capabilityPending": False},
                    {"metricKey": "m2", "capabilityPending": True},
                ]
            },
        }
    }
    assert region_capability_pending(region) is True


def test_legacy_region_without_capability_flag_is_restored_from_reason():
    """历史结构没有标记 → 按占位原因还原（授权范围不算能力缺失）。

    与 ``template_definition_service`` 的历史定义适配同一条规则。
    """
    legacy_capability = {
        "dataStatus": "placeholder",
        "placeholderReason": "「PUE」口径待核实：当前数据源没有登记这个字段，算不出数值。",
    }
    legacy_scope = {
        "dataStatus": "placeholder",
        "placeholderReason": SCOPE_PLACEHOLDER_REASON,
    }
    assert region_capability_pending(legacy_capability) is True
    assert region_capability_pending(legacy_scope) is False


# --------------------------------------------------------------------------
# 2. 文案层：只有能力类占位
# --------------------------------------------------------------------------


def test_reply_with_only_capability_placeholder_says_capability_pending():
    """只有能力类占位 → 说「能力尚未接通」，**不**说授权范围。"""
    reply = report_design_service._template_response(
        _request(), plan=None, template=_capability_template()
    ).reply
    assert "能力尚未接通" in reply
    assert "能力待接通" in reply
    assert "不在授权范围" not in reply
    assert "授权范围" not in reply
    assert OLD_WORDING not in reply


def test_draft_reply_with_only_capability_placeholder_says_capability_pending():
    """草稿那条回复（另一处调用点）同样只说能力待接通。"""
    reply = report_design_service._draft_response(
        _request(), plan=None, template=_capability_template(), draft=None
    ).reply
    assert "能力尚未接通" in reply
    assert "不在授权范围" not in reply
    assert OLD_WORDING not in reply


# --------------------------------------------------------------------------
# 3. 文案层：只有授权类占位
# --------------------------------------------------------------------------


def test_reply_with_only_scope_placeholder_does_not_say_capability_pending():
    """只有授权类占位 → **不**出现「能力尚未接通」，改说授权范围。

    授权类不是能力缺失：那个指标的能力是通的，缺的是这个统计对象的授权。
    """
    reply = report_design_service._template_response(
        _request(), plan=None, template=_scope_template()
    ).reply
    assert "能力尚未接通" not in reply
    assert "能力待接通" not in reply
    assert "尚未接通" not in reply
    assert "不在授权范围" in reply
    assert "当前账号没有这个统计对象的授权" in reply
    assert OLD_WORDING not in reply


def test_draft_reply_with_only_scope_placeholder_does_not_say_capability_pending():
    """草稿那条回复同样不把授权类说成能力未接通。"""
    reply = report_design_service._draft_response(
        _request(), plan=None, template=_scope_template(), draft=None
    ).reply
    assert "能力尚未接通" not in reply
    assert "能力待接通" not in reply
    assert "不在授权范围" in reply
    assert OLD_WORDING not in reply


def test_scope_reply_reuses_the_placeholder_reason_wording():
    """授权类的说法与 ``placeholderReason`` **同一套说法**。

    模板层的授权类原因文案是 ``SCOPE_PLACEHOLDER_REASON``（唯一来源），
    回复正文引用的是同一套措辞，不自造第二套。
    """
    template = _scope_template()
    reply = report_design_service._template_response(
        _request(), plan=None, template=template
    ).reply
    # 原因清单里带的是模板层那条原文。
    assert template["pendingNotes"][0].split("」", 1)[1] in reply
    # 回复正文里的授权说明与模板层的原因文案语义一致。
    assert "该指标能力已接通" in reply
    assert "当前账号没有这个统计对象的授权" in reply


# --------------------------------------------------------------------------
# 4. 文案层：两类都有 —— 分别计数，不混算
# --------------------------------------------------------------------------


def test_reply_with_both_kinds_counts_them_separately():
    """两类都有 → 两个计数分别正确，互不混算。"""
    template = _mixed_template()
    reply = report_design_service._template_response(
        _request(), plan=None, template=template
    ).reply
    assert "1 个区域的能力尚未接通" in reply
    assert "1 个区域不在授权范围" in reply
    # 不能把两类合成一个数说。
    assert "2 个区域的能力尚未接通" not in reply
    assert "2 个区域不在授权范围" not in reply


def test_placeholder_reply_fragment_counts_each_kind():
    """文案片段函数：两类分别计数。"""
    fragment = placeholder_reply(_mixed_template()["regions"])
    assert "1 个区域的能力尚未接通" in fragment
    assert "1 个区域不在授权范围" in fragment


def test_pending_notes_lead_in_matches_the_placeholder_kinds():
    """原因清单的引导语按占位类别选：授权类**不是**「尚未接通的原因」。"""
    assert pending_notes_reply(_capability_template()).startswith("尚未接通的原因：")
    scope_notes = pending_notes_reply(_scope_template())
    assert scope_notes.startswith("不在授权范围的原因：")
    assert "尚未接通" not in scope_notes
    assert pending_notes_reply(_mixed_template()).startswith("各区域的具体原因：")


# --------------------------------------------------------------------------
# 5. 三态措辞互不混淆（能力待接通 / 范围授权 / 既有绑定）
# --------------------------------------------------------------------------


def test_three_states_wording_does_not_confuse_each_other():
    """既有绑定 + 能力类 + 授权类同时出现 → 三类措辞互不混淆。"""
    template = _bound_template()
    template["regions"].extend(_mixed_template()["regions"])
    template["pendingNotes"] = template["pendingNotes"] + _mixed_template()["pendingNotes"]

    reply = report_design_service._draft_response(
        _request(), plan=None, template=template, draft=None
    ).reply

    # 既有绑定：绑定了指标、出了示意数值 —— 不说能力、不说授权。
    assert "1 个区域已绑定指标并出了示意数值" in reply
    # 能力类：说「能力尚未接通」。
    assert "1 个区域的能力尚未接通" in reply
    # 授权类：说授权范围，**不说**能力没接通。
    assert "1 个区域不在授权范围" in reply
    # 计数不混。
    assert "2 个区域的能力尚未接通" not in reply
    assert "2 个区域不在授权范围" not in reply
    # 授权类那句话本身不能出现「能力待接通 / 能力尚未接通」。
    scope_sentence = reply.split("；")[1]
    assert "能力" not in scope_sentence or "能力已接通" in scope_sentence
    assert OLD_WORDING not in reply


# --------------------------------------------------------------------------
# 6. 预览那条回复（agent_service）
# --------------------------------------------------------------------------


def _definition_with(regions: list[dict[str, Any]]) -> dict[str, Any]:
    """造一份定义草稿（形状与转换层产物一致）。"""
    return {
        "title": "探针报表",
        "regions": regions,
        "metadata": {
            "templateBinding": {
                "templateId": "rtpl_probe",
                "templateRevision": 1,
                "planId": "rplan_probe",
                "planRevision": 1,
                "definitionId": "rplan_probe",
                "definitionVersion": 1,
            }
        },
    }


def _definition_region(
    *, data_status: str, capability_pending: bool, reason: str | None = None
) -> dict[str, Any]:
    return {
        "id": "region_1",
        "name": "探针区域",
        "role": "SUMMARY",
        "component": {"type": "METRIC_CARD"},
        "templateBinding": {
            "sectionId": "s1",
            "dataStatus": data_status,
            "placeholderReason": reason,
            "metricRefs": [],
            "pendingReason": None,
            "previewSpec": {
                "role": "SUMMARY",
                "kind": "energy",
                "unit": "kWh",
                "capabilityPending": capability_pending,
                "columns": [],
                "metrics": [
                    {
                        "metricKey": "metric.energy.s1_1",
                        "label": "总耗电",
                        "kind": "energy",
                        "unit": "kWh",
                        "capabilityPending": capability_pending,
                        "pendingReason": reason if capability_pending else None,
                    }
                ],
            },
        },
    }


def _preview_reply(definition: dict[str, Any]) -> str:
    """跑「预览已生成」那条回复（``agent_service._handle_previewing``）。"""
    from app.schemas.report_definition import ReportDefinitionDraft
    from app.services.agent_service import agent_service

    request = _request()
    request.state = AgentState.PREVIEWING
    request.snapshots.reportDefinition = ReportDefinitionDraft(**definition)  # type: ignore[assignment]
    response = agent_service._handle_previewing(request)
    return response.reply


def test_preview_reply_with_only_capability_placeholder():
    """预览那条回复：只有能力类占位 → 说能力待接通，不说授权范围。"""
    definition = _definition_with([
        _definition_region(
            data_status="placeholder",
            capability_pending=True,
            reason="「PUE」口径待核实：当前数据源没有登记这个字段，算不出数值。",
        )
    ])
    reply = _preview_reply(definition)
    assert "能力尚未接通" in reply
    assert "能力待接通" in reply
    assert "不在授权范围" not in reply
    assert OLD_WORDING not in reply


def test_preview_reply_with_only_scope_placeholder():
    """预览那条回复：只有授权类占位 → **不**说能力未接通。"""
    definition = _definition_with([
        _definition_region(
            data_status="placeholder",
            capability_pending=False,
            reason=SCOPE_PLACEHOLDER_REASON,
        )
    ])
    reply = _preview_reply(definition)
    assert "能力尚未接通" not in reply
    assert "能力待接通" not in reply
    assert "不在授权范围" in reply
    assert OLD_WORDING not in reply


def test_preview_reply_with_both_kinds_counts_them_separately():
    """预览那条回复：两类都有 → 分别计数。"""
    definition = _definition_with([
        _definition_region(
            data_status="placeholder",
            capability_pending=True,
            reason="「PUE」口径待核实：当前数据源没有登记这个字段，算不出数值。",
        ),
        _definition_region(
            data_status="placeholder",
            capability_pending=False,
            reason=SCOPE_PLACEHOLDER_REASON,
        ),
    ])
    reply = _preview_reply(definition)
    assert "1 个区域的能力尚未接通" in reply
    assert "1 个区域不在授权范围" in reply
    assert "2 个区域的能力尚未接通" not in reply
    assert OLD_WORDING not in reply


# --------------------------------------------------------------------------
# 7. 回归：没有占位区域时回复里不该冒出占位说明
# --------------------------------------------------------------------------


def test_reply_without_placeholder_says_nothing_about_placeholders():
    """全是既有绑定 → 回复里既不说能力待接通，也不说授权范围。"""
    reply = report_design_service._draft_response(
        _request(), plan=None, template=_bound_template(), draft=None
    ).reply
    assert "1 个区域已绑定指标并出了示意数值" in reply
    assert "能力尚未接通" not in reply
    assert "不在授权范围" not in reply
    assert OLD_WORDING not in reply


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
