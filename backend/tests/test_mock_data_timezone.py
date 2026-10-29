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


def _expected_logical_date() -> str:
    """预览数据对应的业务日期。

    原断言把「昨日」写死成 2026-10-25，那是写测试那天的昨日。
    数据层按上海时区取昨日，日期一变断言就过期 —— 这跟被测行为无关。
    这里改成从数据层自己算出的业务日期取值：
    断言的**意图**（首点=业务日 00:00、末点=业务日 23:00 / 23:59）
    保持不变，只是不再把日历日期钉死。
    """
    snap = build_snapshot(room_id="room_a", room_name=resolve_room_name("room_a"))
    return snap.logicalDate


def test_preview_hourly_trend_is_24_points_in_report_timezone():
    definition = _default_definition()
    payload = _trend_payload(_draft_dict(definition))
    points = payload["points"]
    logical_date = _expected_logical_date()
    assert len(points) == 24
    assert payload["grain"] == "HOUR"
    assert points[0]["timestamp"].endswith("+08:00")
    assert points[-1]["timestamp"].endswith("+08:00")
    assert points[0]["timestamp"].startswith(f"{logical_date}T00:00:00")
    assert points[-1]["timestamp"].startswith(f"{logical_date}T23:00:00")
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
    logical_date = _expected_logical_date()
    assert len(points) == 1440
    assert payload["grain"] == "MINUTE"
    assert points[0]["timestamp"].startswith(f"{logical_date}T00:00:00")
    assert points[-1]["timestamp"].startswith(f"{logical_date}T23:59:00")
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


# ==================================================== 造数器：窗口与分桶
#
# 模拟业务数据必须严格按**输入的时间窗**分桶：左闭右开、单日小时 24 桶、
# 多日按实际小时数、天粒度每天一桶；夏令时按实际窗口，不写死 24。

from app.services.simulated_data_service import generate_simulated_data  # noqa: E402


def _sim_definition(*, timezone="Asia/Shanghai", grain="HOUR"):
    return {
        "metadata": {"name": "机房能耗日报"},
        "defaults": {
            "scope": {"selectors": {"machineRoomId": ["room_a"]}},
            "timeWindow": {
                "type": "CALENDAR",
                "timezone": timezone,
                "unit": "DAY",
                "offset": -1,
                "count": 1,
            },
            "grain": {"unit": grain},
        },
        "regions": [
            {
                "id": "r_total",
                "name": "总耗电",
                "role": "SUMMARY",
                "component": {
                    "type": "METRIC_CARD",
                    "metric": {
                        "label": "总耗电",
                        "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
                    },
                },
                "templateBinding": {
                    "previewSpec": {
                        "kind": "energy",
                        "unit": "kWh",
                        "metrics": [
                            {
                                "metricKey": "energy",
                                "label": "总耗电",
                                "kind": "energy",
                                "unit": "kWh",
                            }
                        ],
                    }
                },
            },
            {
                "id": "r_trend",
                "name": "耗电趋势",
                "role": "TREND",
                "component": {"type": "CHART", "chartKind": "LINE", "encodings": []},
                "templateBinding": {"previewSpec": {"kind": "energy", "unit": "kWh"}},
            },
        ],
    }


def _sim_context(**overrides):
    context = {
        "definitionId": "def_tz_gen",
        "definitionVersion": 1,
        "timezone": "Asia/Shanghai",
        "anchorTime": "2026-10-29T00:00:00+08:00",
        "windowStart": "2026-10-28T00:00:00+08:00",
        "windowEnd": "2026-10-29T00:00:00+08:00",
        "grainUnit": "HOUR",
        "generatorVersion": "simulated-data/1",
    }
    context.update(overrides)
    return context


def test_single_day_hour_window_has_24_buckets():
    payloads = generate_simulated_data(_sim_definition(), _sim_context())
    trend = payloads["r_trend"]
    assert len(trend["yValues"]) == 24
    assert len(trend["xLabels"]) == 24
    assert trend["grain"] == "HOUR"


def test_window_is_left_closed_right_open():
    """窗口左闭右开：第一个桶从 windowStart 起，最后一个桶止于 windowEnd。"""
    payloads = generate_simulated_data(_sim_definition(), _sim_context())
    labels = payloads["r_trend"]["xLabels"]
    assert labels[0] == "00:00"
    assert labels[-1] == "23:00"
    assert len(labels) == 24


def test_multi_day_hour_window_expands_to_actual_hours():
    context = _sim_context(
        windowStart="2026-10-22T00:00:00+08:00",
        windowEnd="2026-10-29T00:00:00+08:00",
    )
    payloads = generate_simulated_data(_sim_definition(), context)
    assert len(payloads["r_trend"]["yValues"]) == 7 * 24


def test_day_grain_has_one_bucket_per_day():
    context = _sim_context(
        windowStart="2026-10-22T00:00:00+08:00",
        windowEnd="2026-10-29T00:00:00+08:00",
        grainUnit="DAY",
    )
    payloads = generate_simulated_data(_sim_definition(grain="DAY"), context)
    trend = payloads["r_trend"]
    assert len(trend["yValues"]) == 7
    assert trend["xLabels"][0] == "10-22"
    assert trend["xLabels"][-1] == "10-28"


def test_day_grain_buckets_sum_to_the_card_total():
    context = _sim_context(
        windowStart="2026-10-22T00:00:00+08:00",
        windowEnd="2026-10-29T00:00:00+08:00",
        grainUnit="DAY",
    )
    payloads = generate_simulated_data(_sim_definition(grain="DAY"), context)
    assert round(sum(payloads["r_trend"]["yValues"]), 6) == round(
        payloads["r_total"]["value"], 6
    )


def test_dst_spring_forward_day_has_23_buckets():
    """美国夏令时开始日：本地一天只有 23 个真实小时，不能写死 24。"""
    context = _sim_context(
        timezone="America/New_York",
        windowStart="2026-03-08T00:00:00-05:00",
        windowEnd="2026-03-09T00:00:00-04:00",
    )
    payloads = generate_simulated_data(
        _sim_definition(timezone="America/New_York"), context
    )
    assert len(payloads["r_trend"]["yValues"]) == 23


def test_dst_fall_back_day_has_25_buckets():
    """夏令时结束日：本地一天有 25 个真实小时。"""
    context = _sim_context(
        timezone="America/New_York",
        windowStart="2026-11-01T00:00:00-04:00",
        windowEnd="2026-11-02T00:00:00-05:00",
    )
    payloads = generate_simulated_data(
        _sim_definition(timezone="America/New_York"), context
    )
    assert len(payloads["r_trend"]["yValues"]) == 25


def test_trend_timestamps_carry_the_report_timezone_offset():
    payloads = generate_simulated_data(_sim_definition(), _sim_context())
    points = payloads["r_trend"]["points"]
    for point in points:
        stamp = point["timestamp"]
        assert stamp.endswith("+08:00")


def test_generated_numbers_follow_the_input_window_not_today():
    """跨午夜打开：数字来自**输入窗口**，不是当前日期。"""
    old = generate_simulated_data(
        _sim_definition(),
        _sim_context(
            windowStart="2026-01-05T00:00:00+08:00",
            windowEnd="2026-01-06T00:00:00+08:00",
        ),
    )
    recent = generate_simulated_data(
        _sim_definition(),
        _sim_context(
            windowStart="2026-10-28T00:00:00+08:00",
            windowEnd="2026-10-29T00:00:00+08:00",
        ),
    )
    assert old["r_total"]["value"] != recent["r_total"]["value"]
    assert old["r_total"]["windowLabel"].startswith("2026-01-05")


def test_same_window_in_a_different_timezone_shifts_the_buckets():
    """同一绝对窗口换报表时区：桶数按**实际窗口**走，时间戳跟着时区走。"""
    context = _sim_context(
        timezone="UTC",
        windowStart="2026-10-27T16:00:00+00:00",
        windowEnd="2026-10-28T16:00:00+00:00",
    )
    payloads = generate_simulated_data(_sim_definition(timezone="UTC"), context)
    trend = payloads["r_trend"]
    assert len(trend["yValues"]) == 24
    for point in trend["points"]:
        assert point["timestamp"].endswith("+00:00")
