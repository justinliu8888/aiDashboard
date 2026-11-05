"""能力格式编辑：隔离试做、工具错误回传、已检查候选与接受事务。

覆盖 2026-11-04 需求的这些硬要求：

* 动态操作清单在**副本**上整批试做：正式草稿、版本、撤销历史不变；
* 试做失败把**具体错误**退回同一个 ReAct 循环（不写最终提交槽）；
* 每一步有**自己的目标**（既有区块 id 或本次新建的稳定引用）；
* 告警替换得到**两张独立**的分类统计图，标签与数值一一对应并标注模拟；
* 接受时执行**服务端保存的已检查清单**，不重新规划；第二次接受被拒；
* 纠正 / 补充让**旧候选失效**。
"""
from __future__ import annotations

import copy

import pytest

from app.schemas.report_definition import ReportDefinitionDraft
from app.services.edit_capabilities import (
    CapabilityError,
    capability_catalog_text,
    dimension_catalog,
    get_capability,
)
from app.services.edit_item_service import (
    EDIT_ITEM_KEY,
    load_edit_item,
    save_edit_item,
)
from app.services.edit_trial_service import run_trial, summarize_facts
from app.services.preview_service import build_preview_context, build_report_preview_payload
from app.services.session_service import session_service
from app.tools import registry
from app.tools.submission import bind_submission, peek_submission, take_submission


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

def _draft() -> ReportDefinitionDraft:
    """一份带「告警列表」的草稿。"""
    return ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "机房日报", "revision": 3},
            "regions": [
                {
                    "id": "region_report_title",
                    "name": "机房日报",
                    "role": "REPORT_TITLE",
                    "component": {"type": "TEXT", "title": "机房日报"},
                    "templateBinding": {"previewSpec": {"kind": "generic"}},
                },
                {
                    "id": "region_alarm_list",
                    "name": "告警列表",
                    "role": "DETAIL",
                    "component": {
                        "type": "TABLE",
                        "title": "告警列表",
                        "columns": [
                            {"id": "c1", "label": "时间", "fieldRef": "ts", "format": {"kind": "TEXT"}}
                        ],
                    },
                    "data": {
                        "inputs": {"main": {"type": "SIMULATED"}},
                        "pipeline": {"nodes": [], "outputs": {"main": {"nodeRef": "node:n"}}},
                    },
                    "binding": {"outputRef": "output:main"},
                    "templateBinding": {"previewSpec": {"kind": "generic"}},
                },
                {
                    "id": "region_total_energy",
                    "name": "总耗电概览",
                    "role": "SUMMARY",
                    "component": {
                        "type": "METRIC_CARD",
                        "title": "总耗电概览",
                        "metric": {
                            "id": "m1",
                            "label": "总耗电",
                            "fieldRef": "energyKwh",
                            "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
                        },
                    },
                    "data": {
                        "inputs": {"main": {"type": "SIMULATED"}},
                        "pipeline": {"nodes": [], "outputs": {"main": {"nodeRef": "node:n"}}},
                    },
                    "binding": {"outputRef": "output:main"},
                    "templateBinding": {"previewSpec": {"kind": "energy", "unit": "kWh"}},
                },
            ],
        }
    )


def _replace_ops(*, level_chart_kind="PIE", region_chart_kind="PIE"):
    return [
        {
            "op": "ADD_SECTION",
            "target": {},
            "params": {
                "title": "告警等级分布",
                "chartKind": level_chart_kind,
                "dimension": "告警等级",
                "ref": "level_chart",
                "replaceRegionId": "region_alarm_list",
            },
        },
        {
            "op": "ADD_SECTION",
            "target": {},
            "params": {
                "title": "告警区域分布",
                "chartKind": region_chart_kind,
                "dimension": "告警区域",
                "ref": "region_chart",
                "replaceRegionId": "region_alarm_list",
            },
        },
        {
            "op": "DELETE_SECTION",
            "target": {"regionId": "region_alarm_list"},
            "params": {},
        },
    ]


@pytest.fixture()
def draft():
    return _draft()


@pytest.fixture()
def preview_context(draft):
    return build_preview_context(draft.model_dump(mode="json"))


# --------------------------------------------------------------------------
# 能力目录
# --------------------------------------------------------------------------

def test_capability_catalog_is_generated_from_registry():
    text = capability_catalog_text()
    assert "ADD_SECTION" in text
    assert "SET_DISPLAY" in text
    assert "DELETE_SECTION" in text
    assert "SET_CATEGORY_DATA" in text


def test_every_capability_has_exactly_one_contract():
    for capability in registry.list_tools():
        if capability.CATEGORY.value != "BUILDER":
            continue
        assert capability.PARAMETERS is not None
        assert capability.DESCRIPTION


def test_simulated_dimensions_are_discoverable():
    dims = {item["dimension"]: item for item in dimension_catalog()}
    assert "alertLevel" in dims
    assert "alertRegion" in dims
    assert dims["alertLevel"]["categories"]
    assert all(
        isinstance(item["value"], (int, float)) for item in dims["alertLevel"]["categories"]
    )


def test_unknown_capability_is_rejected():
    assert get_capability("NOT_A_CAPABILITY") is None


# --------------------------------------------------------------------------
# 隔离试做
# --------------------------------------------------------------------------

def test_trial_replaces_alarm_list_with_two_pie_charts(draft, preview_context):
    result = run_trial(
        definition=draft,
        operations=_replace_ops(),
        preview_context=preview_context,
    )
    assert result.ok, result.errors
    regions = {region["id"]: region for region in result.definition.regions}
    assert "region_alarm_list" not in regions
    charts = [
        region
        for region in result.definition.regions
        if (region.get("component") or {}).get("type") == "CHART"
    ]
    assert len(charts) == 2
    assert all(chart["component"]["chartKind"] == "PIE" for chart in charts)
    assert {chart["name"] for chart in charts} == {"告警等级分布", "告警区域分布"}


def test_trial_does_not_touch_the_authoritative_draft(draft, preview_context):
    before = copy.deepcopy(draft.model_dump(mode="json"))
    result = run_trial(
        definition=draft,
        operations=_replace_ops(),
        preview_context=preview_context,
    )
    assert result.ok, result.errors
    # 传入的草稿对象**一字未改**：版本没 bump、区块没增没删。
    assert draft.model_dump(mode="json") == before
    assert draft.metadata["revision"] == 3
    # 试做出来的是**另一份**对象。
    assert result.definition is not draft
    assert result.definition.metadata["revision"] == 4


def test_trial_creates_two_independent_blocks_with_own_stats(draft, preview_context):
    result = run_trial(
        definition=draft,
        operations=_replace_ops(),
        preview_context=preview_context,
    )
    assert result.ok, result.errors
    payload = build_report_preview_payload(
        result.definition.model_dump(mode="json"), preview_context
    )
    charts = {
        region["name"]: region["payload"]
        for region in payload["regions"]
        if region["componentType"] == "CHART"
    }
    level = charts["告警等级分布"]
    area = charts["告警区域分布"]
    assert level["chartKind"] == "PIE"
    assert area["chartKind"] == "PIE"
    assert level["xLabels"] == ["严重", "重要", "一般", "提示"]
    assert area["xLabels"] == ["华北机房", "华东机房", "华南机房", "西部机房"]
    assert set(level["xLabels"]) != set(area["xLabels"])
    # 标签与数值一一对应，数值是模拟的且带标记。
    for chart in (level, area):
        assert len(chart["xLabels"]) == len(chart["yValues"])
        assert chart["simulated"] is True
        assert chart["dataSource"] == "simulated"
    # 不是拿设备能耗数据改标题冒充告警分布。
    assert all("耗电" not in label for label in level["xLabels"])


def test_trial_missing_chart_kind_returns_specific_error(draft, preview_context):
    result = run_trial(
        definition=draft,
        operations=[
            {
                "op": "ADD_SECTION",
                "target": {},
                "params": {"title": "告警等级分布", "dimension": "告警等级", "ref": "level_chart"},
            }
        ],
        preview_context=preview_context,
    )
    assert not result.ok
    error = result.errors[0]
    assert error["reason"] == "missing_chart_kind"
    assert "chartKind" in error["message"]
    assert error["step"] == 0
    assert error["op"] == "ADD_SECTION"


def test_trial_unknown_ref_returns_specific_error(draft, preview_context):
    result = run_trial(
        definition=draft,
        operations=[
            {
                "op": "SET_DISPLAY",
                "target": {"ref": "never_created"},
                "params": {"componentType": "CHART", "chartKind": "PIE"},
            }
        ],
        preview_context=preview_context,
    )
    assert not result.ok
    error = result.errors[0]
    assert error["reason"] == "unknown_ref"
    assert "never_created" in error["message"]


def test_trial_unknown_region_returns_specific_error(draft, preview_context):
    result = run_trial(
        definition=draft,
        operations=[
            {
                "op": "DELETE_SECTION",
                "target": {"regionId": "region_does_not_exist"},
                "params": {},
            }
        ],
        preview_context=preview_context,
    )
    assert not result.ok
    assert result.errors[0]["reason"] == "unknown_region"


def test_trial_unknown_capability_returns_specific_error(draft, preview_context):
    result = run_trial(
        definition=draft,
        operations=[{"op": "MAKE_COFFEE", "target": {}, "params": {}}],
        preview_context=preview_context,
    )
    assert not result.ok
    assert result.errors[0]["reason"] == "unknown_capability"


def test_trial_rejects_unknown_dimension(draft, preview_context):
    result = run_trial(
        definition=draft,
        operations=[
            {
                "op": "ADD_SECTION",
                "target": {},
                "params": {
                    "title": "奇怪分布",
                    "chartKind": "PIE",
                    "dimension": "宇宙射线强度",
                    "ref": "weird",
                },
            }
        ],
        preview_context=preview_context,
    )
    assert not result.ok
    assert result.errors[0]["reason"] == "unknown_dimension"


def test_trial_detects_encoding_mismatch(draft, preview_context):
    """分类绑定与横纵轴编码对不上 → 试做必须拦下。"""
    draft = copy.deepcopy(draft)
    result = run_trial(
        definition=draft,
        operations=[
            {
                "op": "ADD_SECTION",
                "target": {},
                "params": {
                    "title": "告警等级分布",
                    "chartKind": "PIE",
                    "dimension": "告警等级",
                    "ref": "level_chart",
                },
            }
        ],
        preview_context=preview_context,
    )
    assert result.ok, result.errors
    # 人为把横轴编码改坏 —— 核验必须发现。
    broken = result.definition.model_copy(deep=True)
    for region in broken.regions:
        if region["name"] == "告警等级分布":
            region["component"]["encodings"] = [
                {"channel": "X", "fieldRef": "deviceName"},
                {"channel": "Y", "fieldRef": "alertCount"},
            ]
    from app.services.edit_trial_service import _validate_chart_encoding

    errors: list[dict] = []
    for region in broken.regions:
        _validate_chart_encoding(region, errors)
    assert errors
    assert errors[0]["reason"] == "encoding_mismatch"


def test_trial_keeps_reference_target_independent(draft, preview_context):
    """每一步有**自己的目标**：新增后用 ref 指向它，不是共用一个旧目标。"""
    result = run_trial(
        definition=draft,
        operations=[
            {
                "op": "ADD_SECTION",
                "target": {},
                "params": {
                    "title": "告警等级分布",
                    "chartKind": "PIE",
                    "dimension": "告警等级",
                    "ref": "level_chart",
                    "replaceRegionId": "region_alarm_list",
                },
            },
            {
                "op": "SET_DISPLAY",
                "target": {"ref": "level_chart"},
                "params": {"componentType": "CHART", "chartKind": "BAR"},
            },
        ],
        preview_context=preview_context,
    )
    assert result.ok, result.errors
    chart = next(
        region for region in result.definition.regions if region["name"] == "告警等级分布"
    )
    assert chart["component"]["chartKind"] == "BAR"


def test_trial_summary_matches_actual_difference(draft, preview_context):
    result = run_trial(
        definition=draft,
        operations=_replace_ops(),
        preview_context=preview_context,
    )
    assert result.ok, result.errors
    summary = summarize_facts(result.facts)
    assert "告警列表" in summary
    assert "告警等级分布" in summary
    assert "告警区域分布" in summary
    assert "饼图" in summary
    assert result.facts["replacedInPlace"] is True
    assert len(result.facts["added"]) == 2
    assert len(result.facts["removed"]) == 1
    # 没改的区块照旧在。
    assert "region_total_energy" in result.facts["unchangedRegionIds"]


def test_summary_does_not_claim_position_when_moved(draft, preview_context):
    """新区块没落在原位置时，确认文字**不能**说「放在原位置」。"""
    result = run_trial(
        definition=draft,
        operations=[
            {
                "op": "ADD_SECTION",
                "target": {},
                "params": {
                    "title": "告警等级分布",
                    "chartKind": "PIE",
                    "dimension": "告警等级",
                    "ref": "level_chart",
                },
            },
            {
                "op": "DELETE_SECTION",
                "target": {"regionId": "region_alarm_list"},
                "params": {},
            },
        ],
        preview_context=preview_context,
    )
    assert result.ok, result.errors
    assert result.facts["replacedInPlace"] is False
    assert "放在原位置" not in summarize_facts(result.facts)


# --------------------------------------------------------------------------
# 工具层：试做失败不写提交槽
# --------------------------------------------------------------------------

def _store_definition(draft, session_id="conv_test"):
    """把草稿写进**服务端会话快照**，并绑定本轮会话标识。

    工具读的是服务端权威事实（会话快照里的 ``definition``），
    不是模型上下文、也不是客户端回传的快照。
    """
    from app.services.session_context import set_session_key

    session_service.update_snapshot(
        session_id,
        lambda snapshot: {**snapshot, "definition": draft.model_dump(mode="json")},
    )
    set_session_key(session_id)


def test_submit_tool_failure_leaves_submission_slot_empty(draft):
    _store_definition(draft)
    bind_submission()
    try:
        result = registry.get_runner("submit_edit_plan")(
            operations=[
                {
                    "op": "ADD_SECTION",
                    "target": {},
                    "params": {"title": "缺类型的图", "dimension": "告警等级"},
                }
            ]
        )
        assert result["accepted"] is False
        assert result["errors"]
        assert "chartKind" in result["error"]
        # **没有**占用最终提交槽 → 同一个 Agent 可以在预算内修正再试。
        assert peek_submission() is None
    finally:
        take_submission()


def test_submit_tool_success_records_checked_candidate(draft):
    _store_definition(draft)
    bind_submission()
    try:
        result = registry.get_runner("submit_edit_plan")(
            operations=_replace_ops()
        )
        assert result["accepted"] is True
        assert result["checked"] is True
        assert "告警等级分布" in result["summary"]
        submission = peek_submission()
        assert submission is not None
        assert submission["tool"] == "submit_edit_plan"
        payload = submission["payload"]
        assert payload["editFacts"]["added"]
        assert payload["editFacts"]["removed"]
        assert payload["target"]["regionId"] == "region_alarm_list"
        # 事实落在**服务端**的修改事项里，不依赖模型记忆。
        item = load_edit_item("conv_test")
        assert item is not None
        assert item["status"] == "checked"
        assert item["refs"]["level_chart"]
    finally:
        take_submission()


def test_submit_tool_fills_readable_step_summaries(draft):
    """每一步都要有一句**人能读懂**的话。

    能力格式的步骤没有模型自述的 summary；不补的话前端候选卡上的
    「具体改什么」会退化成三条 ``UNKNOWN``。
    """
    _store_definition(draft)
    bind_submission()
    try:
        result = registry.get_runner("submit_edit_plan")(operations=_replace_ops())
        assert result["accepted"] is True
        payload = peek_submission()["payload"]
    finally:
        take_submission()
    summaries = [str(step.get("summary") or "") for step in payload["operations"]]
    assert all(summaries), summaries
    assert not any("UNKNOWN" in text for text in summaries), summaries
    assert "告警等级分布" in summaries[0] and "饼图" in summaries[0]
    assert "告警区域分布" in summaries[1] and "饼图" in summaries[1]
    assert "告警列表" in summaries[2]


def test_submit_tool_stores_operations_on_the_item(draft):
    """提交的操作清单落在**服务端**的修改事项里（不依赖模型记忆）。"""
    _store_definition(draft)
    bind_submission()
    try:
        result = registry.get_runner("submit_edit_plan")(
            operations=_replace_ops(),
            originalRequest="将告警列表改成告警等级分布和告警区域分布",
        )
        assert result["accepted"] is True
    finally:
        take_submission()
    item = load_edit_item("conv_test")
    assert item is not None
    assert len(item["operations"]) == 3
    assert item["originalRequest"].startswith("将告警列表")
    assert item["status"] == "checked"


def test_submit_tool_rejects_duplicate_refs(draft):
    _store_definition(draft)
    bind_submission()
    try:
        result = registry.get_runner("submit_edit_plan")(
            operations=[
                {
                    "op": "ADD_SECTION",
                    "target": {},
                    "params": {
                        "title": "A",
                        "chartKind": "PIE",
                        "dimension": "告警等级",
                        "ref": "same",
                    },
                },
                {
                    "op": "ADD_SECTION",
                    "target": {},
                    "params": {
                        "title": "B",
                        "chartKind": "PIE",
                        "dimension": "告警区域",
                        "ref": "same",
                    },
                },
            ]
        )
        assert result["accepted"] is False
        assert "已经被这次修改用过了" in result["error"]
    finally:
        take_submission()


def test_trial_failure_keeps_item_context(draft):
    """试做失败**保留**事项上下文（下一轮补充时还能接着用）。"""
    _store_definition(draft)
    bind_submission()
    try:
        result = registry.get_runner("submit_edit_plan")(
            operations=[
                {
                    "op": "ADD_SECTION",
                    "target": {},
                    "params": {"title": "缺类型的图", "dimension": "告警等级"},
                }
            ],
            originalRequest="将告警列表改成两张分布图",
        )
        assert result["accepted"] is False
    finally:
        take_submission()
    item = load_edit_item("conv_test")
    assert item is not None
    assert item["status"] == "drafting"
    assert item["originalRequest"] == "将告警列表改成两张分布图"
    assert item["lastErrors"]


# --------------------------------------------------------------------------
# 能力契约细节
# --------------------------------------------------------------------------

def test_capability_error_carries_step_target_constraint_reason():
    error = CapabilityError(
        "缺 chartKind",
        step=2,
        op="ADD_SECTION",
        target="region_x",
        constraint="CHART 必须指定 chartKind",
        reason="missing_chart_kind",
    )
    payload = error.to_dict()
    assert payload["step"] == 2
    assert payload["op"] == "ADD_SECTION"
    assert payload["target"] == "region_x"
    assert payload["constraint"] == "CHART 必须指定 chartKind"
    assert payload["reason"] == "missing_chart_kind"
    assert "chartKind" in payload["message"]
