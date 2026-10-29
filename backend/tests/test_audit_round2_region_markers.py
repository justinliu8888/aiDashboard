"""第二轮审查（#51 区域标记 / 旧口径收口 / 恢复会话可见）的回归用例。

四组事实，逐条对应规格：

1. **G4 区域级标记**（规格 #46 第 247 / 261 行）：示意载荷的每个区域都带
   ``dataSource`` 与 ``capabilityPending``。区域级 ``capabilityPending`` 是
   **各项指标的逻辑或**（第 245 行）—— 不是「这个区域是不是占位区」。
2. **载荷级标记不丢**：``payload.dataSource`` 与
   ``payload.metrics[].capabilityPending`` 照旧保留（前端区域级优先、
   载荷级回退，两边都读）。
3. **旧口径收口**（US15）：``preview_service`` / ``template_definition_service``
   里不再出现「数据待连接」，兜底文案与 ``report_template_service``
   **完全一致**。
4. **恢复会话看得见**：模拟载荷生成失败时，恢复响应**带着失败原因**、
   **没有载荷**（不伪造、不给空壳卡片）；正常会话恢复仍然带载荷。

除外部模型调用被替身接管外，其余（转换层、造数器、载荷层、路由、SQLite
落库与恢复）全是真实实现。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.services.preview_service import build_report_preview_payload
from app.services.report_template_service import build_template_design
from app.services.session_service import session_service
from app.services.template_definition_service import build_template_definition
from tests.test_audit_round2_s3_n3 import (
    _definition_from_agent,
    _plan_from_agent,
    _preview_context,
)


#: 旧口径的四个字：规格《回复口径》要求删掉，US15 要求页面上不出现。
LEGACY_PENDING_WORDING = "数据待连接"

#: 新口径的完整说法（与 ``report_template_service`` 逐字一致）。
SETTLED_PENDING_REASON = "能力待接通：当前已接通的能力里没有对应指标。"

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_SOURCE_FILES = {
    "preview_service": _BACKEND_DIR / "app" / "services" / "preview_service.py",
    "template_definition_service": (
        _BACKEND_DIR / "app" / "services" / "template_definition_service.py"
    ),
    "report_template_service": (
        _BACKEND_DIR / "app" / "services" / "report_template_service.py"
    ),
}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _mixed_candidate() -> dict[str, Any]:
    """一块**已接通**（总耗电）+ 一块**待接通**（温度分布，目录里没有）。"""
    return {
        "topic": "A 机房能耗与温度日报",
        "requirements": {
            "metrics": ["总耗电", "温度分布"],
            "subject": "A 机房",
            "timeRange": "最近 7 天",
            "exclusions": [],
        },
        "sections": [
            {
                "title": "总耗电概览",
                "metricIntents": ["总耗电"],
                "componentType": "metric_card",
            },
            {
                "title": "温度分布",
                "metricIntents": ["温度分布"],
                "componentType": "metric_card",
            },
        ],
        "assumptions": [],
        "suggestions": [],
        "referenceTemplates": [],
    }


def _mixed_definition() -> dict[str, Any]:
    """走真实的转换层两跳（方案 → 模板 → 草稿定义）。"""
    _plan, draft = _definition_from_agent(
        _mixed_candidate(), "给我一份能耗与温度日报"
    )
    return draft.model_dump(mode="json")


def _out_of_scope_definition() -> dict[str, Any]:
    """统计对象越权 → 区域是**占位区**，但能力**没**缺（授权范围问题）。"""
    candidate = {
        "topic": "Z 机房能耗日报",
        "requirements": {
            "metrics": ["总耗电"],
            "subject": "Z 机房",
            "timeRange": "最近 7 天",
            "exclusions": [],
        },
        "sections": [
            {
                "title": "总耗电概览",
                "metricIntents": ["总耗电"],
                "componentType": "metric_card",
            }
        ],
        "assumptions": [],
        "suggestions": [],
        "referenceTemplates": [],
    }
    _agent_candidate, plan = _plan_from_agent(candidate, "给我一份 Z 机房能耗日报")
    payload = plan.model_dump(mode="json")
    # subject_id 传 None：让模板层按**方案里的统计对象**判授权范围。
    template = build_template_design(payload, subject_id=None)
    draft = build_template_definition(
        template.model_dump(mode="json"), payload, "room_a"
    )
    return draft.model_dump(mode="json")


def _regions_by_id(definition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(region.get("id")): region for region in definition.get("regions") or []}


def _spec_metrics(definition: dict[str, Any], region_id: str) -> list[dict[str, Any]]:
    region = _regions_by_id(definition).get(region_id) or {}
    binding = region.get("templateBinding") or {}
    spec = binding.get("previewSpec") or {}
    metrics = spec.get("metrics") or []
    return [item for item in metrics if isinstance(item, dict)]


# --------------------------------------------------------------------------
# G4：区域级 dataSource / capabilityPending
# --------------------------------------------------------------------------


def test_every_region_carries_the_data_source_marker() -> None:
    """每个区域都带 ``dataSource == "simulated"``（规格第 247 / 261 行）。"""
    definition = _mixed_definition()
    payload = build_report_preview_payload(definition, _preview_context())

    regions = payload["regions"]
    assert regions, "示意载荷里没有区域"
    for region in regions:
        assert "dataSource" in region, region.get("regionId")
        assert region["dataSource"] == "simulated", region


def test_region_capability_pending_is_false_when_every_metric_is_connected() -> None:
    """全是已接通的区域 → 区域级 ``capabilityPending`` 是 False。"""
    definition = _mixed_definition()
    payload = build_report_preview_payload(definition, _preview_context())

    connected = [
        region
        for region in payload["regions"]
        if all(
            not metric["capabilityPending"]
            for metric in (region.get("payload") or {}).get("metrics") or []
        )
    ]
    assert connected, "这份定义里应当至少有一个全部接通的区域"
    for region in connected:
        assert region["capabilityPending"] is False, region


def test_region_capability_pending_is_true_when_any_metric_is_pending() -> None:
    """任一指标待接通的区域 → 区域级 ``capabilityPending`` 是 True。"""
    definition = _mixed_definition()
    payload = build_report_preview_payload(definition, _preview_context())

    pending = [
        region
        for region in payload["regions"]
        if any(
            metric["capabilityPending"]
            for metric in (region.get("payload") or {}).get("metrics") or []
        )
    ]
    assert pending, "这份定义里应当至少有一个待接通的区域"
    for region in pending:
        assert region["capabilityPending"] is True, region


def test_region_capability_pending_equals_the_or_of_its_metrics() -> None:
    """区域级能力布尔 = 各项指标的逻辑或（规格第 245 行，逐个区域验）。"""
    definition = _mixed_definition()
    payload = build_report_preview_payload(definition, _preview_context())

    for region in payload["regions"]:
        region_id = region["regionId"]
        metrics = _spec_metrics(definition, region_id)
        assert metrics, f"区域 {region_id} 没有指标条目"
        expected = any(bool(metric.get("capabilityPending")) for metric in metrics)
        assert region["capabilityPending"] is expected, region


def test_region_capability_pending_is_not_the_placeholder_flag() -> None:
    """区域级能力标记说的不是「是不是占位区」。

    统计对象越权 → 区域**是**占位区，但那是**授权范围**问题，不是能力缺失：
    区域级能力标记仍然是 False。
    """
    definition = _out_of_scope_definition()
    payload = build_report_preview_payload(definition, _preview_context())

    region = payload["regions"][0]
    binding = (
        _regions_by_id(definition).get(region["regionId"], {})
        .get("templateBinding", {})
    )
    assert binding.get("dataStatus") == "placeholder", binding
    assert region["capabilityPending"] is False, region


def test_title_region_still_stays_out_of_regions() -> None:
    """标题区（``role == REPORT_TITLE"``）照旧不进 ``regions[]``。"""
    definition = _mixed_definition()
    definition["regions"] = list(definition["regions"]) + [{
        "id": "region_title",
        "name": "A 机房能耗与温度日报",
        "role": "REPORT_TITLE",
        "component": {"type": "TEXT", "content": {"format": "PLAIN", "template": "标题"}},
        "context": definition["regions"][0].get("context") or {},
    }]

    payload = build_report_preview_payload(definition, _preview_context())

    roles = [region.get("role") for region in payload["regions"]]
    assert "REPORT_TITLE" not in roles, roles
    assert all(region.get("regionId") != "region_title" for region in payload["regions"])


# --------------------------------------------------------------------------
# 载荷级标记不许被搬走（前端两边都读）
# --------------------------------------------------------------------------


def test_payload_level_markers_are_preserved() -> None:
    """``payload.dataSource`` 与 ``payload.metrics[].capabilityPending`` 仍在。"""
    definition = _mixed_definition()
    payload = build_report_preview_payload(definition, _preview_context())

    for region in payload["regions"]:
        region_payload = region.get("payload") or {}
        assert region_payload.get("dataSource") == "simulated", region
        for metric in region_payload.get("metrics") or []:
            assert "capabilityPending" in metric, metric
            assert isinstance(metric["capabilityPending"], bool), metric


def test_payload_metrics_keep_their_own_capability_flags() -> None:
    """载荷里的逐指标标记与区域级一致（区域级是它们的逻辑或，不是搬运）。"""
    definition = _mixed_definition()
    payload = build_report_preview_payload(definition, _preview_context())

    for region in payload["regions"]:
        metrics = (region.get("payload") or {}).get("metrics") or []
        if not metrics:
            continue
        expected = any(bool(metric.get("capabilityPending")) for metric in metrics)
        assert region["capabilityPending"] is expected, region


# --------------------------------------------------------------------------
# 旧口径收口（US15）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("source_name", ["preview_service", "template_definition_service"])
def test_legacy_pending_wording_is_gone_from_sources(source_name: str) -> None:
    """这两个文件里不再出现旧口径「数据待连接」。"""
    source = _SOURCE_FILES[source_name].read_text(encoding="utf-8")
    assert LEGACY_PENDING_WORDING not in source, source_name


@pytest.mark.parametrize(
    "source_name", ["preview_service", "template_definition_service"]
)
def test_pending_reason_wording_matches_report_template_service(
    source_name: str,
) -> None:
    """兜底文案与 ``report_template_service`` **逐字一致**（两个文件口径统一）。"""
    source = _SOURCE_FILES[source_name].read_text(encoding="utf-8")
    assert SETTLED_PENDING_REASON in source, source_name


def test_placeholder_reason_fallback_uses_the_settled_wording() -> None:
    """上游没给占位原因时，落进区域内容的兜底文案是新口径。"""
    plan = {
        "id": "plan_wording",
        "revision": 1,
        "topic": "说明报表",
        "requirements": {"timeRange": "最近 7 天"},
        "sections": [
            {
                "id": "section_note",
                "title": "补充说明",
                "componentType": "text",
                "metricIntents": [],
            }
        ],
    }
    template = {
        "id": "tpl_wording",
        "revision": 1,
        "planId": "plan_wording",
        "planRevision": 1,
        "title": "说明报表",
        "dataStatus": "placeholder",
        "regions": [
            {
                "id": "region_note",
                "sectionId": "section_note",
                "title": "补充说明",
                "componentType": "text",
                "metricIntents": [],
                # 上游**没有**给占位原因 → 走兜底文案。
                "dataStatus": "placeholder",
            }
        ],
    }

    draft = build_template_definition(template, plan, "room_a")
    definition = draft.model_dump(mode="json")

    region = next(
        item for item in definition["regions"] if item["id"] == "region_note"
    )
    content = (region.get("component") or {}).get("content") or {}
    text = str(content.get("template") or "")
    assert text, "占位文字区应当有内容"
    assert LEGACY_PENDING_WORDING not in text, text
    assert "能力待接通" in text, text


# --------------------------------------------------------------------------
# 恢复会话：失败要看得见（任务 4）
# --------------------------------------------------------------------------


def _week_grain_definition() -> dict[str, Any]:
    """一份「按周统计」的定义 —— 造数器不支持按周 → 明确失败。"""
    definition = _mixed_definition()
    definition["defaults"] = {
        **(definition.get("defaults") or {}),
        "grain": {"unit": "WEEK"},
    }
    for region in definition.get("regions") or []:
        context = dict(region.get("context") or {})
        context["grain"] = {"mode": "REPLACE", "value": {"unit": "WEEK"}}
        region["context"] = context
    return definition


def test_session_restore_surfaces_the_simulated_data_failure(client) -> None:
    """模拟载荷生成失败：恢复响应带着**失败原因**，且**没有载荷**。

    用户要「按周统计」但按周不被支持 → 那一轮失败并给了人话提示；
    刷新页面后右侧不能是一片空白，原因要**看得见**。
    """
    session_service.repository.upsert_session(
        session_id="sess_grain_broken",
        state="READY",
        title="按周统计",
        snapshot={"definition": _week_grain_definition()},
    )

    detail = client.get("/api/sessions/sess_grain_broken").json()
    snapshot = detail["snapshot"]

    # 不伪造载荷、不给空壳卡片。
    assert "reportPreviewPayload" not in snapshot, snapshot.get("reportPreviewPayload")
    assert not (snapshot.get("reportPreviewPayload") or {}).get("regions")

    error = snapshot.get("error") or {}
    assert error, "恢复响应里没有失败原因"
    assert error.get("code") == "UNRESOLVED_GRAIN", error
    message = str(error.get("message") or "")
    assert message, "失败原因没有可读的消息"
    # 人话：说清楚是什么粒度不通、为什么、改成什么（不是只丢一个错误码）。
    assert message != error["code"], message
    assert "粒度" in message, message
    assert "WEEK" in message, message


def test_session_restore_still_returns_the_payload_for_a_healthy_session(client) -> None:
    """正常会话恢复的既有行为不变：仍然带着载荷，且没有失败标记。"""
    session_service.repository.upsert_session(
        session_id="sess_healthy",
        state="READY",
        title="正常报表",
        snapshot={"definition": _mixed_definition()},
    )

    detail = client.get("/api/sessions/sess_healthy").json()
    snapshot = detail["snapshot"]

    payload = snapshot.get("reportPreviewPayload")
    assert payload, "正常会话恢复没有带载荷"
    assert payload["regions"], "正常会话恢复的载荷是空的"
    assert "error" not in snapshot, snapshot.get("error")


def test_session_restore_failure_reason_is_not_written_to_storage(client) -> None:
    """失败原因只在**这次响应**里，不落库（响应级字段）。"""
    session_service.repository.upsert_session(
        session_id="sess_grain_broken_2",
        state="READY",
        title="按周统计",
        snapshot={"definition": _week_grain_definition()},
    )

    client.get("/api/sessions/sess_grain_broken_2")

    stored = session_service.repository.get_session("sess_grain_broken_2").snapshot
    assert "error" not in stored, stored.get("error")
    assert "reportPreviewPayload" not in stored, stored.get("reportPreviewPayload")


# --------------------------------------------------------------------------
# 载荷结构不被破坏（回归护栏）
# --------------------------------------------------------------------------


def test_region_keys_are_stable() -> None:
    """区域项的键集合是约定的 7 个（不多不少、不改名）。"""
    definition = _mixed_definition()
    payload = build_report_preview_payload(definition, _preview_context())

    expected = {
        "regionId", "name", "role", "componentType",
        "dataSource", "capabilityPending", "payload",
    }
    for region in payload["regions"]:
        assert set(region.keys()) == expected, region.get("regionId")


def test_payload_is_json_serialisable() -> None:
    """载荷能原样进 JSON 响应（区域级标记不能带不可序列化的东西）。"""
    definition = _mixed_definition()
    payload = build_report_preview_payload(definition, _preview_context())
    assert json.loads(json.dumps(payload, ensure_ascii=False))
