"""分析段口径一致性回归测试（A1-P1-01 / PT-A2-01）。

背景：
    带设备过滤的报表（只统计 CRAC-01/CRAC-02）里，分析说明曾经直接读
    **未过滤的房间快照**，于是写出「总耗电为 5460.4 kWh」「用电占比最高的是
    「UPS-01 不间断」」——与同表卡片（1094.6 kWh）、排名表（只有两台空调）
    自相矛盾，还点名了用户明确排除的设备。

    本文件锁住三条规则：
      1. 分析事实引用**同报表、同范围、同窗口**已算好的结果；
      2. 取不到事实时明确说明，不回退到全机房数据；
      3. 环比文案说「较前一日」，不说「相较昨日」/「同比」。

用例不硬编码具体数值：Mock 数据按「昨天」生成，总量每天会变，
所有期望值都由当天的快照算出来。
"""
from __future__ import annotations

from typing import Any

from app.services.mock_data_service import build_snapshot
from app.services.preview_service import preview_service
from app.services.report_draft_service import REGION_IDS, build_definition_draft

CRAC_FILTERS = [
    {"fieldRef": "deviceId", "op": "IN", "values": ["crac_01", "crac_02"]},
    {"fieldRef": "deviceType", "op": "NOT_IN", "values": ["RACK", "PDU", "UPS"]},
]


def _room_snapshot():
    return build_snapshot(room_id="room_a", room_name="A 机房")


def _device_energy(snap, device_ids: set[str]) -> float:
    return round(
        sum(d.energyKwh for d in snap.deviceRanking if d.deviceId in device_ids), 1
    )


def _preview(definition: Any):
    payload = definition.model_dump(mode="json")
    _preview_meta, record = preview_service.run_preview(
        definitionId="def_analysis_scope_test",
        definitionDraft=payload,
    )
    regions = {r["regionId"]: r["payload"] for r in record.regions}
    result = preview_service.get_result_payload(record.reportRunId)
    return regions, result


def _analysis(regions: dict[str, Any]) -> str:
    return (regions.get(REGION_IDS["analysis"]) or {}).get("analysis", "")


def _trend_points(regions: dict[str, Any]) -> list[dict[str, Any]]:
    payload = regions.get(REGION_IDS["trend"]) or {}
    return payload.get("points") or payload.get("rows") or []


def _ranking_rows(regions: dict[str, Any]) -> list[dict[str, Any]]:
    return (regions.get(REGION_IDS["ranking"]) or {}).get("rows") or []


# ---------------------------------------------------------------- A1-P1-01


def test_analysis_of_filtered_report_never_mentions_excluded_devices():
    """只统计 CRAC 时，分析段不能提到 UPS / 机架 / PDU。"""
    definition = build_definition_draft(
        "A 机房昨日仅空调设备的耗电报表", "room_a", filters=CRAC_FILTERS
    )
    regions, _result = _preview(definition)

    analysis = _analysis(regions)
    assert analysis, "分析段不能为空"
    for excluded in ("UPS-01", "UPS", "机架", "配电"):
        assert excluded not in analysis, f"分析段泄漏了被排除的设备/类型：{excluded} —— {analysis}"
    rows = _ranking_rows(regions)
    assert {r["deviceId"] for r in rows} == {"crac_01", "crac_02"}
    # Mock 排名随日期变化，分析应引用过滤后的排名首项。
    assert rows[0]["deviceName"] in analysis, f"分析段没有引用范围内最高耗电设备：{analysis}"


def test_analysis_total_matches_filtered_card_not_room_total():
    """分析段里的总量必须是过滤后的值，不能是全机房总量。"""
    snap = _room_snapshot()
    room_total = snap.totalEnergyKwh
    crac_total = _device_energy(snap, {"crac_01", "crac_02"})
    assert room_total != crac_total  # 前提：过滤确实改变了总量

    definition = build_definition_draft(
        "A 机房昨日仅空调设备的耗电报表", "room_a", filters=CRAC_FILTERS
    )
    regions, _result = _preview(definition)
    analysis = _analysis(regions)

    card = regions[REGION_IDS["total"]]
    assert card["value"] == crac_total
    assert f"{crac_total}" in analysis, f"分析段没有出现过滤后的总量 {crac_total}：{analysis}"
    assert f"{room_total}" not in analysis, f"分析段出现全机房总量 {room_total}：{analysis}"


def test_filtered_report_card_ranking_trend_analysis_share_one_scope():
    """卡片 / 排名 / 趋势 / 分析必须是同一个口径。"""
    definition = build_definition_draft(
        "A 机房昨日仅空调设备的耗电报表", "room_a", filters=CRAC_FILTERS
    )
    regions, result = _preview(definition)

    card_value = regions[REGION_IDS["total"]]["value"]
    rows = _ranking_rows(regions)
    points = _trend_points(regions)

    assert card_value == round(sum(r["energyKwh"] for r in rows), 1)
    assert card_value == round(sum(p["energyKwh"] for p in points), 1)
    assert {r["deviceName"] for r in rows} == {"CRAC-01 空调", "CRAC-02 空调"}
    assert result["snapshot"]["totalEnergyKwh"] == card_value

    analysis = _analysis(regions)
    assert f"{card_value}" in analysis


def test_trend_curve_respects_device_filter():
    """趋势曲线的总量也要落在过滤后的口径上。"""
    snap = _room_snapshot()
    crac_total = _device_energy(snap, {"crac_01", "crac_02"})

    definition = build_definition_draft(
        "A 机房昨日仅空调设备的耗电报表", "room_a", filters=CRAC_FILTERS
    )
    regions, _result = _preview(definition)

    points = _trend_points(regions)
    assert len(points) == 24
    assert round(sum(p["energyKwh"] for p in points), 1) == crac_total


def test_summary_comparison_uses_same_scope():
    """页面摘要里的环比必须是过滤后的环比，不是房间级 yoyPercent。"""
    snap = _room_snapshot()
    definition = build_definition_draft(
        "A 机房昨日仅空调设备的耗电报表", "room_a", filters=CRAC_FILTERS
    )
    regions, result = _preview(definition)

    card = regions[REGION_IDS["total"]]
    assert card["yoyPercent"] != snap.yoyPercent, "过滤报表的环比不应等于房间级 yoyPercent"
    assert result["snapshot"]["yoyPercent"] == card["yoyPercent"]


# ------------------------------------------------------- 事实缺失时如实说明


def test_analysis_states_missing_region_instead_of_falling_back():
    """报表里没有排名区时，要说「未包含设备排名」，不能改说全机房最高设备。"""
    definition = build_definition_draft(
        "A 机房昨日总耗电报表",
        "room_a",
        componentRequests=["total", "analysis"],
        excludedRegions=["ranking", "trend"],
    )
    regions, _result = _preview(definition)
    analysis = _analysis(regions)

    assert "未包含设备排名" in analysis, analysis
    assert "未包含趋势数据" in analysis, analysis
    assert "UPS-01" not in analysis, analysis


def test_analysis_says_no_comparison_when_previous_period_missing(monkeypatch):
    """上一周期没有数据时如实说明「未计算变化幅度」，不编一个数。"""
    import app.services.preview_service as preview_module

    definition = build_definition_draft("A 机房昨日能耗日报", "room_a")
    monkeypatch.setattr(
        preview_module, "_previous_scope_total", lambda *a, **k: None
    )

    regions, _result = _preview(definition)
    analysis = _analysis(regions)

    assert "未计算变化幅度" in analysis, analysis
    assert "较前一日" not in analysis, analysis


# ------------------------------------------------------------- PT-A2-01 文案


def test_analysis_wording_uses_previous_day_not_yoy():
    """文案说「较前一日」，不说「相较昨日」，更不能叫「同比」。"""
    definition = build_definition_draft("A 机房昨日能耗日报", "room_a")
    regions, _result = _preview(definition)
    analysis = _analysis(regions)

    assert "较前一日" in analysis, analysis
    assert "相较昨日" not in analysis, analysis
    assert "同比" not in analysis, analysis


def test_analysis_does_not_call_itself_comparison_with_yesterday():
    """报告本身就是「昨日」，文案不能拿自己当比较对象。"""
    definition = build_definition_draft("A 机房昨日能耗日报", "room_a")
    regions, _result = _preview(definition)

    assert "相较昨日" not in _analysis(regions)
    assert "相比昨日" not in _analysis(regions)


# --------------------------------------------------------------- 七天窗口口径


def test_seven_day_analysis_uses_seven_day_scope():
    """七天窗口的分析必须用七天口径，不能退回昨日单日。"""
    definition = build_definition_draft(
        "A 机房近7日总耗电日报",
        "room_a",
        timeWindow={"unit": "DAY", "offset": -7, "count": 7},
        grainUnit="DAY",
    )
    regions, _result = _preview(definition)

    card = regions[REGION_IDS["total"]]
    assert card["value"] != _room_snapshot().totalEnergyKwh, "七天总量不应等于单日总量"
    assert "（7 天）" in card["windowLabel"], card["windowLabel"]

    analysis = _analysis(regions)
    assert "7 天" in analysis, analysis
    assert f"{card['value']}" in analysis, analysis
    assert "较前 7 天" in analysis, analysis

    points = _trend_points(regions)
    assert len(points) == 7
    assert round(sum(p["energyKwh"] for p in points), 1) == card["value"]


def test_seven_day_ranking_rows_are_window_sums():
    """多日窗口的排名表按窗口累加，行合计要和卡片一致。"""
    definition = build_definition_draft(
        "A 机房近7日总耗电日报",
        "room_a",
        timeWindow={"unit": "DAY", "offset": -7, "count": 7},
        grainUnit="DAY",
    )
    regions, _result = _preview(definition)

    card_value = regions[REGION_IDS["total"]]["value"]
    rows = _ranking_rows(regions)
    assert abs(round(sum(r["energyKwh"] for r in rows), 1) - card_value) < 2.0
    # 单日的 UPS-01 是 846.0；七天窗口必须明显更大
    assert rows[0]["energyKwh"] > 1000


# ------------------------------------------------------------- 标准日报不回归


def test_standard_daily_report_not_regressed():
    """标准日报：卡片/排名/趋势/分析仍然是全机房昨日口径。"""
    snap = _room_snapshot()
    definition = build_definition_draft("A 机房昨日能耗日报", "room_a")
    regions, result = _preview(definition)

    card = regions[REGION_IDS["total"]]
    assert card["value"] == snap.totalEnergyKwh
    assert card["windowLabel"].endswith("全天")

    rows = _ranking_rows(regions)
    assert len(rows) == 10
    assert [r["energyKwh"] for r in rows] == sorted(
        [r["energyKwh"] for r in rows], reverse=True
    )
    # Mock 里每台设备各自取整，合计与机房总量允许 1 kWh 的取整差
    assert abs(round(sum(r["energyKwh"] for r in rows), 1) - snap.totalEnergyKwh) < 1.0

    points = _trend_points(regions)
    assert len(points) == 24
    assert round(sum(p["energyKwh"] for p in points), 1) == snap.totalEnergyKwh

    analysis = _analysis(regions)
    assert f"{snap.totalEnergyKwh}" in analysis
    assert "UPS-01" in analysis, "未过滤的日报本就应提到耗电最高的设备"
    assert result["snapshot"]["totalEnergyKwh"] == snap.totalEnergyKwh


def test_standard_daily_report_preview_window_label_unchanged():
    """标准日报的窗口标签保持单日「全天」。"""
    definition = build_definition_draft("A 机房昨日能耗日报", "room_a")
    regions, _result = _preview(definition)
    assert regions[REGION_IDS["total"]]["windowLabel"].endswith("全天")


def test_trend_timestamps_follow_definition_timezone():
    """趋势点时间戳必须带定义声明的时区（不是 UTC 午夜）。"""
    definition = build_definition_draft("A 机房昨日能耗日报", "room_a")
    regions, _result = _preview(definition)

    points = _trend_points(regions)
    assert points[0]["timestamp"].endswith("+08:00"), points[0]["timestamp"]
    assert points[0]["timestamp"].startswith(
        _room_snapshot().logicalDate
    ), points[0]["timestamp"]
