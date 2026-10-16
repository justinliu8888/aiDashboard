"""报表草稿内部结构测试：report-definition-v1.md 的硬性约束。"""
from __future__ import annotations

from app.schemas.change_set import ChangeSet
from app.services.report_draft_service import (
    REGION_IDS,
    build_change_set_remove_trend,
    build_change_set_trend_7d,
    build_definition_draft,
    build_policy_draft,
)


def test_policy_draft_defaults():
    policy = build_policy_draft("A 机房能耗日报")
    assert policy.subjectRef == "machine_room"
    assert policy.executionMode == "MANUAL_ONLY"
    assert "TIME_WINDOW" in policy.allowedOverrides


def test_definition_regions_have_required_shape():
    definition = build_definition_draft(
        report_name="A 机房能耗日报",
        room_id="room_a",
        include_trend=True,
    )

    # 固定区域 ID 不能变（前端 / Java 后端依赖）
    region_ids = [r["id"] for r in definition.regions]
    assert region_ids == list(REGION_IDS.values())

    for region in definition.regions:
        assert region["id"]
        assert region["role"]
        assert region["component"]["type"]
        assert "placement" in region["layout"]


def test_definition_without_trend_skips_region_and_intent_binding():
    definition = build_definition_draft(
        report_name="A 机房能耗日报",
        room_id="room_a",
        include_trend=False,
    )
    region_ids = [r["id"] for r in definition.regions]
    assert REGION_IDS["trend"] not in region_ids

    intent_ids = {ib["id"] for ib in definition.intentBindings}
    assert "intent_hourly_trend" not in intent_ids


def test_definition_datasets_use_real_source_ref():
    definition = build_definition_draft(
        report_name="A 机房能耗日报",
        room_id="room_a",
    )
    ds = definition.datasets["ds_room_energy"]
    assert ds["sourceRef"] == "catalog.machine_room.energy"
    # 单位必须真实
    assert ds["fields"]["energyKwh"]["unit"] == "kWh"


def test_metric_card_unit_matches_dataset_unit():
    definition = build_definition_draft(
        report_name="A 机房能耗日报",
        room_id="room_a",
    )
    total_region = next(
        r for r in definition.regions if r["id"] == REGION_IDS["total"]
    )
    card_unit = total_region["component"]["metric"]["format"]["unitLabel"]
    ds_unit = definition.datasets["ds_room_energy"]["fields"]["energyKwh"]["unit"]
    assert card_unit == ds_unit, "指标卡的 unitLabel 必须与数据集字段单位一致"


def test_trend_7d_change_set_shape():
    cs: ChangeSet = build_change_set_trend_7d(base_revision=2)
    assert cs.baseDefinitionRevision == 2
    assert len(cs.changed) == 1
    entry = cs.changed[0]
    # path 使用 JSON Pointer 格式，必须以 /regions/ 开头
    assert entry.path.startswith("/regions/")
    assert entry.path.endswith("/context/timeWindow")
    # 不改动的区域必须显式标出，避免前端误判
    unchanged_ids = {u.regionId for u in cs.unchanged}
    assert REGION_IDS["total"] in unchanged_ids


def test_remove_trend_change_set_shape():
    cs: ChangeSet = build_change_set_remove_trend(base_revision=4)
    assert cs.baseDefinitionRevision == 4
    assert len(cs.changed) == 1
    entry = cs.changed[0]
    assert entry.regionId == REGION_IDS["trend"]
    assert entry.after is None  # 删除语义


def test_change_set_paths_target_stable_region_ids():
    """所有 ChangeSet 的 path 必须命中真实区域 ID，不能是 /regions/0 之类位置索引。"""
    for cs in (
        build_change_set_trend_7d(1),
        build_change_set_remove_trend(1),
    ):
        for entry in cs.changed:
            assert entry.path.startswith("/regions/region_"), (
                f"path 必须用区域 ID，不能用位置索引: {entry.path}"
            )
