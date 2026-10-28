"""定义组装层的验收测试：路由解析出来的条件必须真实进入报表定义。

对应本轮修复的缺陷：
    M-08 过去 7 个完整自然日 + 每天趋势 → 仍是昨日单日 + HOUR 24 点
    M-13 eas_energy_detail + MINUTE → 静默切回默认数据源 + HOUR
    M-03 只要一个总耗电指标卡 → 又加回排名表 / 趋势图 / 分析段
    M-09 只统计 CRAC-01/CRAC-02 → 排名仍是全量 10 台
    M-10 Top3 + 占比 → 10 行且无占比
    M-11 前一日耗电 / 日增长率 / 标红 → 没有列也没有样式
    M-14 所有项目全部机房 → 应基于授权范围明确阻断
    M-15 设备心情值 → 应明确说明指标未登记
"""
from __future__ import annotations

from typing import Any

import pytest

from app.services.preview_service import preview_service
from app.services.report_draft_service import (
    REGION_IDS,
    DefinitionBuildError,
    build_definition_draft,
)
from app.services.validation_service import validate_definition


def _regions(definition: Any) -> list[dict[str, Any]]:
    if hasattr(definition, "regions"):
        return list(definition.regions)
    return list(definition["regions"])


def _region(definition: Any, key: str) -> dict[str, Any]:
    region_id = REGION_IDS[key]
    return next(r for r in _regions(definition) if r["id"] == region_id)


def _nodes(region: dict[str, Any]) -> list[dict[str, Any]]:
    return (((region.get("data") or {}).get("pipeline") or {}).get("nodes")) or []


def _node(region: dict[str, Any], node_type: str) -> dict[str, Any] | None:
    return next((n for n in _nodes(region) if n.get("type") == node_type), None)


def _window_value(region: dict[str, Any]) -> dict[str, Any]:
    return ((region.get("context") or {}).get("timeWindow") or {}).get("value") or {}


def _preview_payload(definition: Any, region_id: str) -> dict[str, Any]:
    payload = definition.model_dump(mode="json") if hasattr(definition, "model_dump") else definition
    _preview, record = preview_service.run_preview(
        definitionId="def_acceptance",
        definitionDraft=payload,
    )
    return next(
        r for r in record.regions if r["regionId"] == region_id
    )["payload"]


# ---------------------------------------------------------------- M-08


def test_past_7_complete_days_keeps_day_window_and_daily_trend():
    """过去7个完整自然日 + 每天趋势：时间窗是 7 天，趋势是 DAY 粒度。"""
    definition = build_definition_draft(
        "A 机房近 7 日能耗",
        "room_a",
        timeWindow={"unit": "DAY", "count": 7},
        grainUnit="DAY",
    )

    total = _region(definition, "total")
    window = _window_value(total)
    assert window["unit"] == "DAY"
    assert window["count"] == 7
    assert window["offset"] == -7, "过去 7 个完整自然日 = D-7 ~ D-1，不含今天"

    trend = _region(definition, "trend")
    assert trend["context"]["grain"]["value"]["unit"] == "DAY"
    assert _window_value(trend)["count"] == 7

    # 24 个小时点说明粒度没换回来
    resample = _node(trend, "RESAMPLE")
    assert resample["grain"]["unit"] == "DAY"


def test_past_7_days_can_be_supplied_as_natural_language():
    """路由层给出自然语言时间表述时也要能解析成同一个窗口。"""
    definition = build_definition_draft(
        "A 机房近 7 日能耗",
        "room_a",
        timeWindow="过去7个完整自然日",
        grainUnit="DAY",
    )
    window = _window_value(_region(definition, "total"))
    assert (window["unit"], window["count"], window["offset"]) == ("DAY", 7, -7)


def test_multi_day_window_defaults_to_day_grain_when_grain_not_given():
    """没显式给粒度、但窗口跨了多天 → 趋势按天出，不按 24 小时点出。"""
    definition = build_definition_draft(
        "A 机房近 7 日能耗",
        "room_a",
        timeWindow={"unit": "DAY", "count": 7},
    )
    assert _region(definition, "trend")["context"]["grain"]["value"]["unit"] == "DAY"


def test_yesterday_window_still_defaults_to_hour_grain():
    """旧行为不变：昨日窗口 → 小时趋势。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    assert _region(definition, "trend")["context"]["grain"]["value"]["unit"] == "HOUR"
    assert _window_value(_region(definition, "total"))["count"] == 1


# ---------------------------------------------------------------- M-13


def test_eas_source_with_minute_grain_is_preserved():
    """eas_energy_detail + MINUTE：数据源和粒度都不能被静默换掉。"""
    definition = build_definition_draft(
        "A 机房昨日分钟耗电趋势",
        "room_a",
        sourceRef="eas_energy_detail",
        grainUnit="MINUTE",
    )

    dataset = definition.datasets["ds_room_energy"]
    assert dataset["sourceRef"] == "eas_energy_detail"

    trend = _region(definition, "trend")
    assert trend["context"]["grain"]["value"]["unit"] == "MINUTE"
    assert _node(trend, "RESAMPLE")["grain"]["unit"] == "MINUTE"
    assert trend["data"]["inputs"]["main"]["datasetRef"] == "ds_room_energy"
    assert definition.dependencies["sourceRef"] == "eas_energy_detail"


def test_minute_grain_on_default_source_is_blocked_not_downgraded():
    """默认数据源不支持分钟级 → 明确阻断，不能悄悄降级成 HOUR。"""
    with pytest.raises(DefinitionBuildError) as excinfo:
        build_definition_draft(
            "A 机房昨日分钟耗电趋势",
            "room_a",
            grainUnit="MINUTE",
        )
    assert excinfo.value.code == "GRAIN_NOT_SUPPORTED"
    assert "MINUTE" in excinfo.value.message


def test_unknown_source_ref_is_blocked_not_swapped():
    """未知数据源 → 阻断，不退回默认数据源。"""
    with pytest.raises(DefinitionBuildError) as excinfo:
        build_definition_draft(
            "A 机房能耗日报", "room_a", sourceRef="catalog.not_exists"
        )
    assert excinfo.value.code == "SOURCE_REF_UNKNOWN"


# ---------------------------------------------------------------- M-03


def test_only_total_card_produces_only_total_region():
    """只要一个总耗电指标卡 → 不要再加排名表 / 趋势图 / 分析段。"""
    definition = build_definition_draft(
        "A 机房昨日总耗电",
        "room_a",
        componentRequests=["total"],
    )

    region_ids = [r["id"] for r in _regions(definition)]
    assert REGION_IDS["total"] in region_ids
    assert REGION_IDS["ranking"] not in region_ids
    assert REGION_IDS["trend"] not in region_ids
    assert REGION_IDS["analysis"] not in region_ids
    assert REGION_IDS["quality"] not in region_ids

    intent_ids = {b["id"] for b in definition.intentBindings}
    assert intent_ids == {"intent_total_energy"}


def test_excluded_regions_are_dropped():
    """明确排除的区块也不能被加回来。"""
    definition = build_definition_draft(
        "A 机房昨日总耗电和设备排名",
        "room_a",
        excludedRegions=["trend", "analysis"],
    )
    region_ids = [r["id"] for r in _regions(definition)]
    assert REGION_IDS["ranking"] in region_ids
    assert REGION_IDS["trend"] not in region_ids
    assert REGION_IDS["analysis"] not in region_ids


def test_analysis_region_does_not_reference_missing_regions():
    """分析段的事实引用不能指向已经不存在的区域。"""
    definition = build_definition_draft(
        "A 机房昨日总耗电",
        "room_a",
        componentRequests=["total"],
    )
    facts = _region(definition, "analysis") if False else []
    analysis = next(
        (r for r in _regions(definition) if r["id"] == REGION_IDS["analysis"]), None
    )
    if analysis is not None:
        refs = [f["outputRef"] for f in analysis["component"]["facts"]]
        assert all(REGION_IDS["ranking"] not in r for r in refs)
        assert all(REGION_IDS["trend"] not in r for r in refs)
    assert facts == []


# ---------------------------------------------------------------- M-09


def test_only_crac_devices_filter_is_in_the_definition():
    """只统计 CRAC-01/CRAC-02：定义里必须有设备过滤。"""
    definition = build_definition_draft(
        "A 机房昨日空调耗电",
        "room_a",
        filters=[
            {
                "fieldRef": "deviceName",
                "op": "IN",
                "values": ["CRAC-01 空调", "CRAC-02 空调"],
            }
        ],
    )

    ranking = _region(definition, "ranking")
    filter_node = _node(ranking, "FILTER")
    assert filter_node is not None
    condition = filter_node["conditions"][0]
    assert condition["fieldRef"] == "deviceName"
    assert set(condition["values"]) == {"CRAC-01 空调", "CRAC-02 空调"}

    # 过滤必须作用在 group aggregate 之前
    node_ids = [n["id"] for n in _nodes(ranking)]
    assert node_ids.index("n_filter") < node_ids.index("n_device")


def test_only_crac_devices_filter_applies_to_preview_rows():
    """预览出来的排名也只能有 CRAC-01 / CRAC-02 两台。"""
    definition = build_definition_draft(
        "A 机房昨日空调耗电",
        "room_a",
        filters=[
            {
                "fieldRef": "deviceName",
                "op": "IN",
                "values": ["CRAC-01 空调", "CRAC-02 空调"],
            }
        ],
    )

    payload = _preview_payload(definition, REGION_IDS["ranking"])
    names = {row["deviceName"] for row in payload["rows"]}
    assert names == {"CRAC-01 空调", "CRAC-02 空调"}


def test_device_type_filter_selects_crac_devices():
    """按设备类别过滤（只统计空调设备）同样生效。"""
    definition = build_definition_draft(
        "A 机房昨日仅空调设备的耗电报表",
        "room_a",
        filters=[{"fieldRef": "deviceType", "op": "IN", "values": ["CRAC"]}],
    )

    payload = _preview_payload(definition, REGION_IDS["ranking"])
    assert {row["deviceName"] for row in payload["rows"]} == {
        "CRAC-01 空调",
        "CRAC-02 空调",
    }


def test_filtered_total_card_uses_same_scope_as_table():
    """过滤对整张报表生效，总耗电卡片不能还按全机房算。"""
    definition = build_definition_draft(
        "A 机房昨日仅空调设备的耗电报表",
        "room_a",
        filters=[
            {
                "fieldRef": "deviceName",
                "op": "IN",
                "values": ["CRAC-01 空调", "CRAC-02 空调"],
            }
        ],
    )
    total_payload = _preview_payload(definition, REGION_IDS["total"])
    ranking_payload = _preview_payload(definition, REGION_IDS["ranking"])
    table_sum = round(sum(row["energyKwh"] for row in ranking_payload["rows"]), 1)
    assert total_payload["value"] == table_sum


def test_filter_on_unknown_field_is_blocked():
    with pytest.raises(DefinitionBuildError) as excinfo:
        build_definition_draft(
            "A 机房能耗日报",
            "room_a",
            filters=[{"fieldRef": "notAField", "op": "IN", "values": ["x"]}],
        )
    assert excinfo.value.code == "FILTER_FIELD_UNKNOWN"


# ---------------------------------------------------------------- M-10


def test_top3_ranking_has_three_rows_and_share_column():
    """排名表只显示耗电最高的前 3 台，并给出占机房总耗电的比例。"""
    definition = build_definition_draft(
        "A 机房昨日能耗日报",
        "room_a",
        topN=3,
        derivedColumns=[
            {
                "id": "col_share",
                "label": "占机房总耗电比例 (%)",
                "fieldRef": "energySharePercent",
                "kind": "SHARE_OF_TOTAL",
            }
        ],
    )

    ranking = _region(definition, "ranking")
    assert ranking["binding"]["topN"] == 3
    limit_node = _node(ranking, "LIMIT")
    assert limit_node["count"] == 3

    share_column = next(
        c for c in ranking["component"]["columns"] if c["fieldRef"] == "energySharePercent"
    )
    assert share_column["label"] == "占机房总耗电比例 (%)"


def test_top3_preview_has_three_rows_with_share_values():
    definition = build_definition_draft(
        "A 机房昨日能耗日报",
        "room_a",
        topN=3,
        derivedColumns=[
            {
                "id": "col_share",
                "label": "占机房总耗电比例 (%)",
                "fieldRef": "energySharePercent",
                "kind": "SHARE_OF_TOTAL",
            }
        ],
    )

    payload = _preview_payload(definition, REGION_IDS["ranking"])
    assert len(payload["rows"]) == 3
    values = [row["energySharePercent"] for row in payload["rows"]]
    assert all(value is not None for value in values)
    # 前 3 台占的比例不能超过 100%
    assert 0 < sum(values) <= 100


def test_top3_share_is_relative_to_room_total():
    """占比的分母是机房总耗电，不是前 3 台之和。"""
    definition = build_definition_draft(
        "A 机房昨日能耗日报",
        "room_a",
        topN=3,
        derivedColumns=[
            {
                "id": "col_share",
                "label": "占机房总耗电比例 (%)",
                "fieldRef": "energySharePercent",
                "kind": "SHARE_OF_TOTAL",
            }
        ],
    )
    ranking_payload = _preview_payload(definition, REGION_IDS["ranking"])
    total_payload = _preview_payload(definition, REGION_IDS["total"])
    top_row = ranking_payload["rows"][0]
    expected = round(top_row["energyKwh"] / total_payload["value"] * 100, 1)
    assert top_row["energySharePercent"] == expected


# ---------------------------------------------------------------- M-11


def test_previous_day_and_growth_rate_columns_are_generated():
    """前一日耗电 + 日增长率要真的变成列。"""
    definition = build_definition_draft(
        "A 机房昨日各设备耗电表",
        "room_a",
        derivedColumns=[
            {
                "id": "col_prev",
                "label": "前一日耗电 (kWh)",
                "fieldRef": "prevPeriodEnergyKwh",
                "kind": "PREVIOUS_PERIOD",
            },
            {
                "id": "col_growth",
                "label": "日增长率 (%)",
                "fieldRef": "growthRatePercent",
                "kind": "GROWTH_RATE",
            },
        ],
    )

    ranking = _region(definition, "ranking")
    field_refs = {c["fieldRef"] for c in ranking["component"]["columns"]}
    assert "prevPeriodEnergyKwh" in field_refs
    assert "growthRatePercent" in field_refs

    derive = _node(ranking, "DERIVE")
    assert derive is not None
    kinds = {c["kind"] for c in derive["columns"]}
    assert kinds == {"PREVIOUS_PERIOD", "GROWTH_RATE"}


def test_growth_above_20_percent_is_highlighted_red():
    """日增长率严格超过 20% 的行要标红。"""
    definition = build_definition_draft(
        "A 机房昨日各设备耗电表",
        "room_a",
        derivedColumns=[
            {
                "id": "col_growth",
                "label": "日增长率 (%)",
                "fieldRef": "growthRatePercent",
                "kind": "GROWTH_RATE",
            }
        ],
        styleHints=[
            {
                "targetRegionId": REGION_IDS["ranking"],
                "fieldRef": "growthRatePercent",
                "comparison": "GT",
                "threshold": 20,
                "style": "RED",
            }
        ],
    )

    ranking = _region(definition, "ranking")
    hints = ranking["component"]["styleHints"]
    assert hints[0]["comparison"] == "GT"
    assert hints[0]["threshold"] == 20
    assert hints[0]["style"] == "RED"

    payload = _preview_payload(definition, REGION_IDS["ranking"])
    for row in payload["rows"]:
        growth = row.get("growthRatePercent")
        highlighted = bool(row.get("styleHints"))
        if growth is None:
            continue
        if growth > 20:
            assert highlighted, f"{row['deviceName']} 增长 {growth}% 应该标红"
            assert row["rowStyle"]["class"] == "red"
        else:
            assert not highlighted, f"{row['deviceName']} 增长 {growth}% 不该标红"


def test_style_hint_threshold_is_strict():
    """「严格超过 20%」：等于 20% 不标红。"""
    definition = build_definition_draft(
        "A 机房昨日各设备耗电表",
        "room_a",
        derivedColumns=[
            {
                "id": "col_growth",
                "label": "日增长率 (%)",
                "fieldRef": "growthRatePercent",
                "kind": "GROWTH_RATE",
            }
        ],
        styleHints=[
            {
                "fieldRef": "growthRatePercent",
                "comparison": "GT",
                "threshold": 20,
                "style": "RED",
            }
        ],
    )
    payload = _preview_payload(definition, REGION_IDS["ranking"])
    for row in payload["rows"]:
        growth = row.get("growthRatePercent")
        if growth is not None and growth == 20:
            assert not row.get("styleHints")


def test_previous_period_value_is_present_in_preview():
    definition = build_definition_draft(
        "A 机房昨日各设备耗电表",
        "room_a",
        derivedColumns=[
            {
                "id": "col_prev",
                "label": "前一日耗电 (kWh)",
                "fieldRef": "prevPeriodEnergyKwh",
                "kind": "PREVIOUS_PERIOD",
            }
        ],
    )
    payload = _preview_payload(definition, REGION_IDS["ranking"])
    assert all(row.get("prevPeriodEnergyKwh") is not None for row in payload["rows"])


# ------------------------------------------------- 集成缺陷：派生列字段名冲突


def test_derived_column_keeps_canonical_field_ref_when_model_reuses_source_field():
    """模型把派生列的 fieldRef 写成源字段时，必须落回规范名。

    真实模型会这么干：M-11 的计划里两个派生列的 fieldRef 都写成了
    energyKwh。照抄的话派生列会和基础列重名，真正的 prevPeriodEnergyKwh /
    growthRatePercent 根本不会出现，条件样式引用的字段取不到，
    整份报表在校验阶段被判失败（实测 VALIDATION_FAILED，拿不到任何预览）。
    """
    definition = build_definition_draft(
        "A 机房昨日各设备耗电表",
        "room_a",
        derivedColumns=[
            {"id": "col_prev", "label": "前一日耗电 (kWh)", "fieldRef": "energyKwh", "kind": "PREVIOUS_PERIOD"},
            {"id": "col_growth", "label": "日增长率 (%)", "fieldRef": "energyKwh", "kind": "GROWTH_RATE"},
        ],
        styleHints=[
            {
                "targetRegionId": REGION_IDS["ranking"],
                "fieldRef": "growthRatePercent",
                "comparison": "GT",
                "threshold": 20,
                "style": "RED",
            }
        ],
    )

    ranking = _region(definition, "ranking")
    field_refs = [c["fieldRef"] for c in ranking["component"]["columns"]]
    assert field_refs.count("energyKwh") == 1, "派生列不能和基础列重名"
    assert "prevPeriodEnergyKwh" in field_refs
    assert "growthRatePercent" in field_refs

    # 条件样式引用的字段必须真的存在，否则校验一定失败。
    validation = validate_definition(definition.model_dump(mode="json"))
    assert validation.passed, [issue.message for issue in validation.issues]

    payload = _preview_payload(definition, REGION_IDS["ranking"])
    assert all(row.get("prevPeriodEnergyKwh") is not None for row in payload["rows"])
    assert any(row.get("growthRatePercent") is not None for row in payload["rows"])


def test_style_hint_alias_is_rewritten_to_canonical_derived_field():
    """模型用自定义别名写派生列时，条件样式要跟着改写，不悬空。"""
    definition = build_definition_draft(
        "A 机房昨日各设备耗电表",
        "room_a",
        derivedColumns=[
            {"id": "col_growth", "label": "日增长率 (%)", "fieldRef": "myGrowth", "kind": "GROWTH_RATE"},
        ],
        styleHints=[
            {
                "targetRegionId": REGION_IDS["ranking"],
                "fieldRef": "myGrowth",
                "comparison": "GT",
                "threshold": 20,
                "style": "RED",
            }
        ],
    )

    ranking = _region(definition, "ranking")
    hints = ranking["component"]["styleHints"]
    assert hints and hints[0]["fieldRef"] == "growthRatePercent"

    validation = validate_definition(definition.model_dump(mode="json"))
    assert validation.passed, [issue.message for issue in validation.issues]


# --------------------------------------- 集成缺陷：版式诉求塞进 styleHints 通道


def test_layout_request_in_style_hints_is_rejected_not_forwarded():
    """模型把版式诉求塞进 styleHints 时，不能把整份报表拖死。

    实测 M-16：模型把「卡片独占一行 / 左右等宽 / 标题22pt / 标题颜色」
    写成 styleHints（字段名 layout.xxx、title.xxx），原样透传后校验判
    「条件样式引用了拿不到的字段」，整份报表停在 VALIDATION_FAILED，
    连预览都出不来。styleHints 是行级条件样式通道，指向拿不到的字段
    必须在组装层被拒绝。
    """
    definition = build_definition_draft(
        "A 机房昨日能耗报表",
        "room_a",
        styleHints=[
            {"fieldRef": "layout.totalCardFullRow", "threshold": 1, "style": "RED"},
            {"fieldRef": "layout.rankingTrendEqualSplit", "threshold": 1, "style": "RED"},
            {"fieldRef": "title.fontSize", "threshold": 22, "style": "RED"},
            {"fieldRef": "title.color", "threshold": 1, "style": "RED"},
        ],
    )

    ranking = _region(definition, "ranking")
    assert not (ranking["component"].get("styleHints") or [])

    # 报表本身要能校验通过、跑出预览
    validation = validate_definition(definition.model_dump(mode="json"))
    assert validation.passed, [issue.message for issue in validation.issues]

    payload = _preview_payload(definition, REGION_IDS["ranking"])
    assert payload["rows"]


def test_rejected_layout_requests_are_recorded_in_metadata():
    """被拒绝的版式诉求要留在定义里，让上层能明确告诉用户。"""
    definition = build_definition_draft(
        "A 机房昨日能耗报表",
        "room_a",
        styleHints=[
            {"fieldRef": "title.fontSize", "threshold": 22, "style": "RED"},
            {"fieldRef": "title.align", "threshold": 1, "style": "RED"},
        ],
    )
    unsupported = definition.metadata.get("unsupportedStyleRequests")
    assert unsupported
    assert {item["fieldRef"] for item in unsupported} == {"title.fontSize", "title.align"}
    assert all(item["label"] for item in unsupported)


def test_valid_data_field_style_hint_is_still_kept():
    """指向真实数据字段的条件样式不能被误伤。"""
    definition = build_definition_draft(
        "A 机房昨日各设备耗电表",
        "room_a",
        styleHints=[
            {
                "targetRegionId": REGION_IDS["ranking"],
                "fieldRef": "energyKwh",
                "comparison": "GT",
                "threshold": 500,
                "style": "RED",
            }
        ],
    )
    ranking = _region(definition, "ranking")
    hints = ranking["component"]["styleHints"]
    assert len(hints) == 1
    assert hints[0]["fieldRef"] == "energyKwh"
    assert not definition.metadata.get("unsupportedStyleRequests")


# ------------------------------------------------- 集成缺陷：标题区口径不一致


def test_title_region_follows_the_real_report_window():
    """窗口不是昨日时，标题区不能还写「昨日能耗日报」。

    定义里写的是近 7 个自然日、标题区却写「A 机房 昨日能耗日报」，
    导出的定义自相矛盾；预览的标题区也会渲染出这句错的。
    """
    definition = build_definition_draft(
        "A 机房过去 7 天总耗电日报",
        "room_a",
        timeWindow={"unit": "DAY", "count": 7},
        grainUnit="DAY",
    )
    title = _region(definition, "title")
    assert title["component"]["content"]["template"] == "A 机房过去 7 天总耗电日报"
    assert "昨日" not in title["component"]["content"]["template"]


def test_title_region_defaults_to_report_name():
    """默认口径下标题区与报表名保持一致。"""
    definition = build_definition_draft("A 机房昨日能耗日报", "room_a")
    title = _region(definition, "title")
    assert title["component"]["content"]["template"] == "A 机房昨日能耗日报"
    assert title["name"] == "A 机房昨日能耗日报"


# ---------------------------------------------------------------- M-14


def test_all_rooms_request_is_blocked_by_authorized_scope():
    """所有项目全部机房 → 基于授权范围明确阻断。"""
    with pytest.raises(DefinitionBuildError) as excinfo:
        build_definition_draft("全部机房能耗排名", "room_all")
    assert excinfo.value.code == "SCOPE_NOT_AUTHORIZED"
    assert "room_all" in excinfo.value.message


def test_unauthorized_room_is_blocked():
    with pytest.raises(DefinitionBuildError) as excinfo:
        build_definition_draft("B 机房能耗日报", "room_b")
    assert excinfo.value.code == "SCOPE_NOT_AUTHORIZED"


# ---------------------------------------------------------------- M-15


def test_unregistered_metric_is_blocked():
    """「设备心情值」没有登记指标 → 明确说明缺失，不能拿耗电量顶替。"""
    with pytest.raises(DefinitionBuildError) as excinfo:
        build_definition_draft(
            "A 机房昨日设备心情值趋势",
            "room_a",
            metricRefs=["metric.machine_room.device_mood"],
        )
    assert excinfo.value.code == "METRIC_NOT_REGISTERED"
    assert "device_mood" in excinfo.value.message


def test_registered_but_unsupported_metric_is_blocked():
    """已登记但当前模板算不出来的指标也要说明，不能假装算了。"""
    with pytest.raises(DefinitionBuildError) as excinfo:
        build_definition_draft(
            "A 机房昨日 PUE 报表",
            "room_a",
            metricRefs=["metric.machine_room.pue"],
        )
    assert excinfo.value.code == "METRIC_NOT_SUPPORTED"


# ---------------------------------------------------------------- 校验层


def test_validation_passes_for_assembled_definition():
    definition = build_definition_draft(
        "A 机房近 7 日能耗",
        "room_a",
        timeWindow={"unit": "DAY", "count": 7},
        grainUnit="DAY",
        topN=3,
        filters=[{"fieldRef": "deviceType", "op": "IN", "values": ["CRAC"]}],
        derivedColumns=[
            {
                "id": "col_share",
                "label": "占机房总耗电比例 (%)",
                "fieldRef": "energySharePercent",
                "kind": "SHARE_OF_TOTAL",
            }
        ],
        styleHints=[
            {
                "fieldRef": "energySharePercent",
                "comparison": "GT",
                "threshold": 20,
                "style": "RED",
            }
        ],
    )
    result = validate_definition(definition.model_dump(mode="json"))
    assert result.passed, [i.model_dump() for i in result.issues]


def test_validation_catches_unknown_source_ref():
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    payload = definition.model_dump(mode="json")
    payload["datasets"]["ds_room_energy"]["sourceRef"] = "catalog.not_exists"
    result = validate_definition(payload)
    assert not result.passed
    assert any(i.code == "SOURCE_REF_UNKNOWN" for i in result.issues)


def test_validation_catches_unsupported_grain():
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    payload = definition.model_dump(mode="json")
    payload["defaults"]["grain"] = {"unit": "MINUTE"}
    result = validate_definition(payload)
    assert not result.passed
    assert any(i.code == "GRAIN_NOT_SUPPORTED" for i in result.issues)


def test_validation_catches_unregistered_metric_ref():
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    payload = definition.model_dump(mode="json")
    payload["regions"][1]["data"]["pipeline"]["nodes"][0]["metricRef"] = (
        "metric.machine_room.device_mood"
    )
    result = validate_definition(payload)
    assert not result.passed
    assert any(i.code == "METRIC_REF_UNKNOWN" for i in result.issues)


def test_validation_catches_unauthorized_scope_instance():
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    payload = definition.model_dump(mode="json")
    payload["defaults"]["scope"]["selectors"]["machineRoomId"] = ["room_b"]
    result = validate_definition(payload)
    assert not result.passed
    assert any(i.code == "SCOPE_INSTANCE_NOT_AUTHORIZED" for i in result.issues)


def test_validation_catches_filter_on_unknown_field():
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    payload = definition.model_dump(mode="json")
    payload["defaults"]["preFilters"] = [
        {"fieldRef": "notAField", "op": "IN", "values": ["x"]}
    ]
    result = validate_definition(payload)
    assert not result.passed
    assert any(i.code == "FILTER_FIELD_UNKNOWN" for i in result.issues)


def test_validation_catches_unsupported_derived_kind():
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    payload = definition.model_dump(mode="json")
    ranking = next(
        r for r in payload["regions"] if r["id"] == REGION_IDS["ranking"]
    )
    ranking["binding"]["derivedColumns"] = [
        {"id": "col_x", "label": "X", "fieldRef": "xValue", "kind": "MAGIC"}
    ]
    result = validate_definition(payload)
    assert not result.passed
    assert any(i.code == "DERIVED_KIND_UNSUPPORTED" for i in result.issues)


def test_validation_catches_inconsistent_default_source():
    """defaults.source 和数据集写不同的数据源 → 口径不一致，要拦住。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    payload = definition.model_dump(mode="json")
    payload["defaults"]["source"]["sourceRef"] = "eas_energy_detail"
    result = validate_definition(payload)
    assert not result.passed
    assert any(i.code == "SOURCE_REF_INCONSISTENT" for i in result.issues)


def test_defaults_source_matches_dataset_source():
    """默认数据源必须和数据集声明的一致。"""
    definition = build_definition_draft(
        "A 机房分钟趋势", "room_a", sourceRef="eas_energy_detail", grainUnit="MINUTE"
    )
    assert definition.defaults["source"]["sourceRef"] == "eas_energy_detail"
    assert definition.datasets["ds_room_energy"]["sourceRef"] == "eas_energy_detail"


def test_multi_day_preview_has_one_point_per_day():
    """7 天窗口的日趋势预览要有 7 个点（不是 24 个小时点）。"""
    definition = build_definition_draft(
        "A 机房近 7 日能耗",
        "room_a",
        timeWindow={"unit": "DAY", "count": 7},
        grainUnit="DAY",
    )
    payload = _preview_payload(definition, REGION_IDS["trend"])
    assert payload["grain"] == "DAY"
    assert len(payload["xLabels"]) == 7
    assert len(payload["yValues"]) == 7


def test_multi_day_total_card_sums_the_window():
    """7 天窗口的总耗电卡片要等于 7 天的和，不是单日值。"""
    definition = build_definition_draft(
        "A 机房近 7 日能耗",
        "room_a",
        timeWindow={"unit": "DAY", "count": 7},
        grainUnit="DAY",
    )
    payload = _preview_payload(definition, REGION_IDS["total"])
    single = _preview_payload(
        build_definition_draft("A 机房能耗日报", "room_a"), REGION_IDS["total"]
    )
    assert payload["value"] > single["value"]


# ---------------------------------------------------------------- 兼容性


def test_legacy_call_signature_behaviour_is_unchanged():
    """旧调用方式（只给 reportName / roomId）的行为不变。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    assert [r["id"] for r in definition.regions] == list(REGION_IDS.values())
    assert definition.datasets["ds_room_energy"]["sourceRef"] == (
        "catalog.machine_room.energy"
    )
    assert definition.defaults["grain"] == {"unit": "HOUR"}
    assert definition.defaults["timeWindow"]["count"] == 1


def test_legacy_include_trend_false_behaviour_is_unchanged():
    definition = build_definition_draft(
        "A 机房能耗日报", "room_a", include_trend=False
    )
    region_ids = [r["id"] for r in definition.regions]
    assert REGION_IDS["trend"] not in region_ids
    assert "intent_hourly_trend" not in {
        b["id"] for b in definition.intentBindings
    }
