"""Mock 数据层时区口径测试（第二轮修复 A1-P3-02）。

背景：报表定义里的时间窗带 ``timezone``（默认 ``Asia/Shanghai``），
但 Mock 数据层此前把趋势点硬编码成 ``tzinfo=timezone.utc``，于是
``2026-10-25T00:00:00+00:00`` 实际是**上海 10-25 08:00** ——
标注的时区与定义自相矛盾，「昨日」窗口整体偏 8 小时。

这里锁住三条不变量：
1. 趋势点时间戳的时区标注 == 报表定义时间窗的时区；
2. 「昨日」= 报表时区里的昨日，不是机器本地时区的昨日；
3. 点数与首末时刻自洽（小时 24 点 / 分钟 1440 点 / 天 N 点）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.edit_time_resolver import DEFAULT_TIMEZONE
from app.services.mock_data_service import (
    build_snapshot,
    definition_timezone,
    resolve_room_name,
    snapshot_to_dict,
)
from app.services.preview_service import preview_service
from app.services.report_draft_service import build_definition_draft

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _default_definition(**kwargs):
    return build_definition_draft(
        "A 机房昨日能耗日报",
        "room_a",
        **kwargs,
    )


def _draft_dict(definition) -> dict:
    if hasattr(definition, "model_dump"):
        return definition.model_dump(mode="json")
    return dict(definition)


# ---------------------------------------------------------------- 时区标注


def test_hourly_points_carry_report_timezone_offset():
    """小时点必须带报表时区的偏移，不能再是 +00:00。"""
    snap = build_snapshot(room_id="room_a", room_name="A 机房")
    assert snap.timezone == DEFAULT_TIMEZONE
    for point in snap.hourlyTrend:
        assert _ts(point.timestamp).utcoffset() == timedelta(hours=8)
        assert point.timestamp.endswith("+08:00")


def test_minute_points_carry_report_timezone_offset():
    snap = build_snapshot(room_id="room_a", room_name="A 机房")
    for point in snap.minuteTrend:
        assert point.timestamp.endswith("+08:00")


def test_first_and_last_hour_are_local_midnight_and_23h():
    """第一点必须是报表时区的 00:00，最后一点是 23:00。"""
    snap = build_snapshot(room_id="room_a", room_name="A 机房")
    first = _ts(snap.hourlyTrend[0].timestamp)
    last = _ts(snap.hourlyTrend[-1].timestamp)
    assert (first.hour, first.minute) == (0, 0)
    assert (last.hour, last.minute) == (23, 0)
    assert first.astimezone(SHANGHAI).date() == date.fromisoformat(snap.logicalDate)


def test_first_and_last_minute_are_local_0000_and_2359():
    snap = build_snapshot(room_id="room_a", room_name="A 机房")
    first = _ts(snap.minuteTrend[0].timestamp)
    last = _ts(snap.minuteTrend[-1].timestamp)
    assert (first.hour, first.minute) == (0, 0)
    assert (last.hour, last.minute) == (23, 59)
    assert first.astimezone(SHANGHAI).date() == date.fromisoformat(snap.logicalDate)


def test_shanghai_midnight_is_not_utc_midnight():
    """回归锁定：上海午夜不能被标成 UTC 午夜（那等于上海 08:00）。"""
    snap = build_snapshot(room_id="room_a", room_name="A 机房")
    first = _ts(snap.hourlyTrend[0].timestamp)
    assert first.hour == 0
    assert first.utcoffset() != timedelta(0)
    # 上海 00:00 对应的 UTC 时刻是前一天的 16:00
    assert first.astimezone(ZoneInfo("UTC")).hour == 16


# ---------------------------------------------------------------- 昨日窗口


def test_logical_date_is_yesterday_in_report_timezone():
    snap = build_snapshot(room_id="room_a", room_name="A 机房")
    expected = (datetime.now(SHANGHAI).date() - timedelta(days=1)).isoformat()
    assert snap.logicalDate == expected


def test_explicit_logical_date_is_preserved():
    snap = build_snapshot(
        room_id="room_a", room_name="A 机房", logical_date="2026-10-25"
    )
    assert snap.logicalDate == "2026-10-25"
    assert snap.hourlyTrend[0].timestamp.startswith("2026-10-25T00:00:00")


# ---------------------------------------------------------------- 显式时区


def test_explicit_timezone_is_honoured():
    snap = build_snapshot(
        room_id="room_a", room_name="A 机房", timezone="UTC"
    )
    assert snap.timezone == "UTC"
    assert snap.hourlyTrend[0].timestamp.endswith("+00:00")


def test_unknown_timezone_falls_back_to_default_instead_of_crashing():
    """非法时区名不能让预览炸掉，也不能悄悄按 UTC 出数。"""
    snap = build_snapshot(
        room_id="room_a", room_name="A 机房", timezone="Not/AZone"
    )
    assert snap.timezone == DEFAULT_TIMEZONE
    assert snap.hourlyTrend[0].timestamp.endswith("+08:00")


def test_snapshot_to_dict_exposes_timezone():
    snap = build_snapshot(room_id="room_a", room_name="A 机房")
    payload = snapshot_to_dict(snap)
    assert payload["timezone"] == DEFAULT_TIMEZONE
    assert payload["hourlyTrend"][0]["timestamp"].endswith("+08:00")


# ------------------------------------------------- 与报表定义的口径契约


def test_definition_timezone_matches_mock_data_timezone():
    """**核心契约**：数据侧时区必须等于定义侧声明的时区。

    定义由 report_draft_service 生成、Mock 数据由 mock_data_service 生成，
    两边各写各的默认值时就可能漂移；这条测试把漂移钉死。
    """
    definition = _default_definition()
    payload = _draft_dict(definition)
    tz_from_definition = definition_timezone(payload)
    snap = build_snapshot(room_id="room_a", room_name=resolve_room_name("room_a"))
    assert snap.timezone == tz_from_definition
    assert tz_from_definition == DEFAULT_TIMEZONE


def test_definition_timezone_reads_fixed_interval_window():
    payload = {
        "defaults": {
            "timeWindow": {
                "type": "FIXED_INTERVAL",
                "timezone": "Asia/Tokyo",
                "start": "2026-10-25T00:00:00",
                "end": "2026-10-25T23:59:59",
            }
        }
    }
    assert definition_timezone(payload) == "Asia/Tokyo"


def test_definition_timezone_defaults_when_absent():
    assert definition_timezone(None) == DEFAULT_TIMEZONE
    assert definition_timezone({}) == DEFAULT_TIMEZONE


# ------------------------------------------------- 预览层端到端自洽性


def _trend_payload(definition_payload: dict) -> dict:
    _preview, record = preview_service.run_preview(
        definitionId="def_tz_acceptance",
        definitionDraft=definition_payload,
    )
    trend = next(r for r in record.regions if r["regionId"] == "region_hourly_trend")
    return trend["payload"]


def test_preview_hourly_trend_is_24_points_in_report_timezone():
    definition = _default_definition()
    payload = _trend_payload(_draft_dict(definition))
    points = payload["points"]
    assert len(points) == 24
    assert payload["grain"] == "HOUR"
    assert points[0]["timestamp"].endswith("+08:00")
    assert points[-1]["timestamp"].endswith("+08:00")
    assert points[0]["timestamp"].startswith("2026-10-25T00:00:00")
    assert points[-1]["timestamp"].startswith("2026-10-25T23:00:00")
    # 横轴标签仍然是 00:00 ~ 23:00（图表观感不变）
    assert payload["xLabels"][0] == "00:00"
    assert payload["xLabels"][-1] == "23:00"


def test_preview_minute_trend_is_1440_points_in_report_timezone():
    """分钟粒度只能走 EAS 源（默认源不支持 MINUTE，会被能力校验拦下）。"""
    definition = _default_definition(
        sourceRef="eas_energy_detail", grainUnit="MINUTE"
    )
    payload = _trend_payload(_draft_dict(definition))
    points = payload["points"]
    assert len(points) == 1440
    assert payload["grain"] == "MINUTE"
    assert points[0]["timestamp"].startswith("2026-10-25T00:00:00")
    assert points[-1]["timestamp"].startswith("2026-10-25T23:59:00")
    assert points[0]["timestamp"].endswith("+08:00")


def test_preview_day_trend_matches_window_dates_and_timezone():
    """七天窗口：日粒度 7 个点，日期与窗口一致，时区与定义一致。"""
    definition = _default_definition(
        grainUnit="DAY",
        timeWindow={
            "type": "CALENDAR",
            "timezone": "Asia/Shanghai",
            "anchor": {"type": "CONTEXT", "path": "runtime.logicalTime"},
            "unit": "DAY",
            "offset": -7,
            "count": 7,
        },
    )
    payload = _draft_dict(definition)
    trend = _trend_payload(payload)
    points = trend["points"]
    assert len(points) == 7
    window = payload["defaults"]["timeWindow"]
    assert window["timezone"] == "Asia/Shanghai"
    assert window["count"] == 7
    assert window["offset"] == -7
    # 每个点都带定义声明的时区
    for point in points:
        assert point["timestamp"].endswith("+08:00")
    # 日期连续、与窗口首尾自洽
    dates = [p["timestamp"][:10] for p in points]
    assert dates == sorted(dates)
    parsed = [date.fromisoformat(d) for d in dates]
    assert all((b - a).days == 1 for a, b in zip(parsed, parsed[1:]))
    assert trend["xLabels"][0] == dates[0][5:10]


def test_preview_hourly_trend_points_match_snapshot_values():
    """趋势点的数值不受时区修正影响（只改标注，不改数据）。"""
    definition = _default_definition()
    payload = _draft_dict(definition)
    trend = _trend_payload(payload)
    snap = build_snapshot(room_id="room_a", room_name=resolve_room_name("room_a"))
    assert [p["energyKwh"] for p in trend["points"]] == [
        p.energyKwh for p in snap.hourlyTrend
    ]
