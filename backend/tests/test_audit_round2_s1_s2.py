"""审查第二轮（S1 粒度贯穿 / G3 不静默降级 / S2 历史定义身份）的回归用例。

三件事，逐条对应上一轮审查报出的缺陷：

1. **粒度贯穿**（S1）：方案定稿的粒度要一路传到 ``definition.defaults.grain``、
   区域 ``context.grain``、``previewContext.grainUnit``。中间任何一环收窄成
   天，用户说的「按小时」就变成了「按天」（US22 / US26 / US43 不成立）。

2. **WEEK / MONTH 不静默降级**（G3）：造数器造不出来的粒度必须**明确失败**
   （错误码可辨、消息可读），绝不能是「上下文里 grainUnit=DAY 且没有失败」。
   造数器支持集（``SUPPORTED_GRAINS``）本轮**不扩张**。

3. **历史定义恢复不被判过期**（S2）：没有 ``metadata.templateBinding`` 的
   历史定义，上下文的身份/版本必须取定义自身的 ``metadata.id`` /
   ``metadata.revision`` —— 与前端的回退口径一致，否则载荷会被当成过期
   版本丢掉，右侧就是空面板（ADR 0004 §5.10 / US71）。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.services.preview_service import resolve_preview_context
from app.services.session_service import session_service
from tests.test_report_template_generation import (
    RecordingModel,
    _candidate,
    _post,
    install_model,
)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _plan_for(client, monkeypatch, text: str, candidate: dict[str, Any] | None = None):
    install_model(monkeypatch, RecordingModel(candidate or _candidate()))
    return _post(client, text).json()["domainObjects"]["reportPlan"]


def _confirm(client, plan: dict[str, Any], message_id: str = "confirm_plan"):
    """点「确认方案」：同一轮就出草稿与示意载荷。"""
    return _post(client, "确认", message_id=message_id, context={
        "designAction": "CONFIRM_PLAN",
        "planId": plan["id"],
        "basePlanRevision": plan["revision"],
    }).json()


def _region_grain(region: dict[str, Any]) -> str | None:
    """区域上下文里的粒度（``context.grain.value.unit``）。"""
    context = region.get("context") or {}
    grain = context.get("grain") or {}
    value = grain.get("value") if isinstance(grain.get("value"), dict) else grain
    unit = str((value or {}).get("unit") or "").strip().upper()
    return unit or None


def _historical_definition() -> dict[str, Any]:
    """一份**改造前落库**的历史定义：没有 ``metadata.templateBinding``。

    身份与版本落在定义自己的 ``metadata.id`` / ``metadata.revision`` 上 ——
    前端就是按这两个字段回退的（``workbench.js`` 的 ``isStaleReportPayload``）。
    """
    return {
        "schemaVersion": "1.0",
        "metadata": {
            "name": "历史能耗日报",
            "description": "改造前生成的报表定义",
            "revision": 0,
            "id": "def_hist_001",
        },
        "defaults": {
            "grain": {"unit": "DAY"},
            "timeWindow": {
                "type": "CALENDAR", "unit": "DAY", "count": 7, "offset": -7,
                "timezone": "Asia/Shanghai",
            },
        },
        "regions": [{
            "id": "region_1",
            "name": "总耗电",
            "role": "SUMMARY",
            "component": {"type": "METRIC_CARD", "metric": {"label": "总耗电"}},
            "context": {
                "timeWindow": {
                    "type": "CALENDAR", "unit": "DAY", "count": 7, "offset": -7,
                    "timezone": "Asia/Shanghai",
                },
                "grain": {"mode": "REPLACE", "value": {"unit": "DAY"}},
            },
        }],
    }


def _frontend_is_stale(payload: dict[str, Any], definition: dict[str, Any]) -> bool:
    """前端 ``isStaleReportPayload`` 的等价实现（两条版本校对轴）。

    前端读的是 ``definition.metadata.templateBinding``，读不到就回退到
    ``metadata.id`` / ``metadata.revision``。后端必须跟它算出同一套身份，
    前端才不会把这份载荷当过期版本丢掉。
    """
    meta = (definition or {}).get("metadata") or {}
    binding = meta.get("templateBinding") or {}
    definition_id = binding.get("definitionId") or meta.get("id")
    definition_version = binding.get("definitionVersion")
    if definition_version is None:
        definition_version = meta.get("revision")

    ctx = (payload or {}).get("previewContext") or {}
    if ctx.get("definitionId") and definition_id:
        if str(ctx["definitionId"]) != str(definition_id):
            return True
    if ctx.get("definitionVersion") is not None and definition_version is not None:
        if int(ctx["definitionVersion"]) != int(definition_version):
            return True
    return False


# --------------------------------------------------------------------------
# 1. 粒度贯穿（方案 → 定义 → 区域 → 预览上下文）
# --------------------------------------------------------------------------


def test_grain_reaches_definition_and_preview_context(client, monkeypatch):
    """用户说「按小时」，定义、区域、预览上下文都得是小时。

    只断言 ``plan.requirements.grainUnit`` 有值是不够的 —— 上一轮就是
    「方案里是 HOUR、定义里是 DAY、图上出 7 个日点」。
    """
    plan = _plan_for(client, monkeypatch, "生成 A 机房最近 7 天能耗报表，按小时统计")
    assert plan["requirements"]["grainUnit"] == "HOUR"

    confirmed = _confirm(client, plan)

    definition = confirmed["domainObjects"]["reportDefinitionDraft"]
    # 定义级默认粒度
    assert definition["defaults"]["grain"]["unit"] == "HOUR"
    # 每个区域的上下文粒度
    grains = [_region_grain(region) for region in definition["regions"]]
    assert grains, "定义里没有区域"
    assert all(grain == "HOUR" for grain in grains), grains
    # 预览上下文粒度
    context = confirmed["reportPreviewPayload"]["previewContext"]
    assert context["grainUnit"] == "HOUR"


def test_hourly_grain_produces_hourly_points_not_daily_points(client, monkeypatch):
    """按小时的图按小时出点（US26）：点数对得上横轴，不是「按天凑数」。"""
    candidate = _candidate(metrics=["每日耗电量"], sections=[{
        "id": "section_daily", "title": "每日耗电趋势",
        "metricIntents": ["每日耗电量"], "componentType": "line_chart",
    }])
    plan = _plan_for(
        client, monkeypatch, "生成最近 7 天每日耗电趋势，按小时统计", candidate,
    )
    assert plan["requirements"]["grainUnit"] == "HOUR"

    confirmed = _confirm(client, plan)
    payload = confirmed["reportPreviewPayload"]
    chart = next(
        region for region in payload["regions"]
        if region["componentType"] == "CHART"
    )
    assert chart["payload"]["grain"] == "HOUR"
    # 7 天 × 24 小时 = 168 个点（按天只有 7 个）
    assert len(chart["payload"]["points"]) == 7 * 24


# --------------------------------------------------------------------------
# 2. WEEK / MONTH 不静默降级
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("keyword", "grain"),
    [("按周统计", "WEEK"), ("按月统计", "MONTH")],
)
def test_unsupported_grain_fails_explicitly_instead_of_downgrading(
    client, monkeypatch, keyword, grain,
):
    """按周 / 按月：**明确失败**，不偷偷换成天（G3）。

    造数器只支持 MINUTE / HOUR / DAY（``SUPPORTED_GRAINS`` 本轮不扩张）。
    用户要按周就必须得到一个说得清楚的失败，而不是一份按天的图。
    """
    plan = _plan_for(
        client, monkeypatch, f"生成 A 机房最近 7 天能耗报表，{keyword}",
    )
    assert plan["requirements"]["grainUnit"] == grain

    confirmed = _confirm(client, plan)
    definition = confirmed["domainObjects"]["reportDefinitionDraft"]

    # 定义里仍然是用户确认的粒度（没有被换成 DAY）
    assert definition["defaults"]["grain"]["unit"] == grain
    assert all(_region_grain(region) == grain for region in definition["regions"])

    preview = confirmed["reportPreviewPayload"]
    if preview is None:
        # 没出数就要**明确失败**：错误码可辨、消息可读（不是静默 500）。
        error = confirmed["error"]
        assert error is not None, "没有出数、又没有明确失败 = 静默收窄"
        assert error["code"] == "UNRESOLVED_GRAIN"
        assert grain in error["message"]
        assert "受支持的取值" in error["message"]
    else:
        # 出了数就必须是真的这个粒度，绝不是 DAY。
        assert preview["previewContext"]["grainUnit"] == grain

    # 上下文本身也必须是这个粒度：不是「上下文显示 DAY 且没有任何失败」。
    context = resolve_preview_context(definition, plan=plan)
    assert context["grainUnit"] == grain


def test_simulated_generator_supported_grains_are_not_expanded():
    """造数器支持集本轮**不扩张**（按周 / 按月仍然是造不出来的）。"""
    from app.services.simulated_data_service import SUPPORTED_GRAINS

    assert SUPPORTED_GRAINS == ("MINUTE", "HOUR", "DAY")


# --------------------------------------------------------------------------
# 3. 历史定义恢复不被判过期
# --------------------------------------------------------------------------


def test_legacy_definition_context_identity_uses_its_own_metadata():
    """历史定义：上下文身份 = 定义自己的 metadata.id / metadata.revision。

    不是字面量 ``"definition"`` / ``1`` —— 那与前端的回退值永远对不上，
    前端会把后端已经生成好的载荷当成过期版本丢掉。
    """
    definition = _historical_definition()

    context = resolve_preview_context(definition, plan=None, existing=None)

    assert context["definitionId"] == "def_hist_001"
    assert context["definitionVersion"] == 0
    assert context["definitionId"] != "definition"
    assert context["definitionVersion"] != 1


def test_legacy_definition_payload_is_not_stale_for_the_frontend():
    """用前端的判过期口径核对：历史定义恢复出来的载荷**不算过期**。"""
    definition = _historical_definition()

    context = resolve_preview_context(definition, plan=None, existing=None)
    payload = {"previewContext": context, "regions": []}

    assert not _frontend_is_stale(payload, definition)


def test_legacy_session_restore_returns_a_payload_the_frontend_keeps(client, monkeypatch):
    """恢复历史会话：载荷带着定义自己的身份，前端不会把它丢掉（US71）。

    端到端走一遍 ``GET /api/sessions/{id}``：后端确实出了数，身份/版本
    与定义自身一致，前端按 ``metadata.id`` / ``metadata.revision`` 回退
    之后两条轴都对得上。
    """
    plan = _plan_for(client, monkeypatch, "生成 A 机房最近 7 天能耗报表")
    _confirm(client, plan)

    # 把快照改造成「改造前落库」的会话：定义没有 templateBinding 登记，
    # 会话也没有冻结的示意上下文。
    record = session_service.repository.get_session("sess_tpl_1")
    snapshot = dict(record.snapshot)
    definition = dict(snapshot["definition"])
    metadata = dict(definition.get("metadata") or {})
    metadata.pop("templateBinding", None)
    metadata["id"] = "def_hist_001"
    metadata["revision"] = 0
    definition["metadata"] = metadata
    snapshot["definition"] = definition
    snapshot.pop("previewContext", None)
    session_service.repository.upsert_session(
        session_id="sess_tpl_1", state=record.state, snapshot=snapshot,
    )

    detail = client.get("/api/sessions/sess_tpl_1").json()
    payload = detail["snapshot"]["reportPreviewPayload"]

    assert payload is not None, "历史会话没有恢复出示意载荷"
    assert payload["regions"], "恢复出来的载荷是空的"
    context = payload["previewContext"]
    assert context["definitionId"] == "def_hist_001"
    assert context["definitionVersion"] == 0
    # 前端按它自己的回退口径核对 → 不算过期 → 载荷收得下。
    assert not _frontend_is_stale(payload, definition)


def test_new_definition_context_identity_still_matches_the_binding(client, monkeypatch):
    """新定义不受影响：身份仍是 ``templateBinding`` 上登记的那一套。"""
    plan = _plan_for(client, monkeypatch, "生成 A 机房最近 7 天能耗报表")
    confirmed = _confirm(client, plan)

    definition = confirmed["domainObjects"]["reportDefinitionDraft"]
    binding = definition["metadata"]["templateBinding"]
    context = confirmed["reportPreviewPayload"]["previewContext"]

    assert context["definitionId"] == binding["definitionId"]
    assert context["definitionVersion"] == binding["definitionVersion"]
    assert not _frontend_is_stale(
        confirmed["reportPreviewPayload"], definition,
    )
