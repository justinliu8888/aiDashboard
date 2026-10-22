"""B 线专属测试：时间表达解析 + 可见对象解析。

覆盖 20261022 template-editing react 架构改进方案里的两条硬口径：

* 「昨天下午」不能被降格成「昨天整天」；
* 「第三个」按客户可见的正文卡片顺序定位，不把隐藏标题区算进去。

本文件只测纯函数，不触碰共享 SQLite、不起服务、不占端口。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.edit_reference_resolver import (
    STATUS_NEEDS_CLARIFICATION as REF_NEEDS_CLARIFICATION,
)
from app.services.edit_reference_resolver import (
    STATUS_NOT_FOUND as REF_NOT_FOUND,
)
from app.services.edit_reference_resolver import (
    STATUS_RESOLVED as REF_RESOLVED,
)
from app.services.edit_reference_resolver import (
    list_visible_cards,
    resolve_visible_reference,
)
from app.services.edit_time_resolver import (
    STATUS_NEEDS_CLARIFICATION as TIME_NEEDS_CLARIFICATION,
)
from app.services.edit_time_resolver import (
    STATUS_NOT_FOUND as TIME_NOT_FOUND,
)
from app.services.edit_time_resolver import (
    STATUS_RESOLVED as TIME_RESOLVED,
)
from app.services.edit_time_resolver import (
    build_time_window_from_option,
    resolve_time_expression,
)


TZ = ZoneInfo("Asia/Shanghai")
LOGICAL_NOW = datetime(2026, 10, 22, 9, 30, tzinfo=TZ)


# ---------- 时间解析 ----------

def test_afternoon_yesterday_is_not_collapsed_into_whole_day():
    """「昨天下午」不能被当成「昨天整天」，必须追问具体时间段。"""
    result = resolve_time_expression("改成昨天下午", logical_now=LOGICAL_NOW)

    assert result.status == TIME_NEEDS_CLARIFICATION
    assert result.timeWindow is None
    assert result.options, "必须给出具体的时间段候选让用户选"
    assert result.question

    # 候选都是「昨天下午」的具体区间，不是整天
    for option in result.options:
        value = option.timeWindow["value"]
        assert value["type"] == "FIXED_INTERVAL"
        assert value["start"][:10] == "2026-10-21"
        assert value["start"] != value["end"]
        assert not value["start"].endswith("T00:00:00+08:00") or not value["end"].endswith(
            "T00:00:00+08:00"
        )


def test_afternoon_yesterday_options_all_carry_real_intervals():
    result = resolve_time_expression("把趋势改成昨天下午", logical_now=LOGICAL_NOW)

    assert result.status == TIME_NEEDS_CLARIFICATION
    labels = [option.label for option in result.options]
    assert labels, "候选要有可读标签"
    assert all("2026-10-21" in label for label in labels)
    # 每个候选都必须是下午的真实区间（结束时间晚于开始时间）
    for option in result.options:
        start, end = option.timeWindow["value"]["start"], option.timeWindow["value"]["end"]
        assert end > start


def test_plain_yesterday_still_resolves_to_calendar_window():
    """「昨天」这种整段表达沿用协议里的 CalendarWindow，行为不变。"""
    result = resolve_time_expression("把趋势改成昨天", logical_now=LOGICAL_NOW)

    assert result.status == TIME_RESOLVED
    value = result.timeWindow["value"]
    assert value["type"] == "CALENDAR"
    assert value["unit"] == "DAY"
    assert value["offset"] == -1
    assert value["count"] == 1
    assert value["anchor"] == {"type": "CONTEXT", "path": "runtime.logicalTime"}


def test_explicit_clock_range_does_not_ask_again():
    """用户已经给出具体钟点时不再追问。"""
    result = resolve_time_expression(
        "改成昨天下午两点到六点", logical_now=LOGICAL_NOW
    )

    assert result.status == TIME_RESOLVED
    assert result.options == []
    value = result.timeWindow["value"]
    assert value["type"] == "FIXED_INTERVAL"
    assert value["start"].startswith("2026-10-21T14:00")
    assert value["end"].startswith("2026-10-21T18:00")


def test_explicit_colon_clock_range_is_understood():
    result = resolve_time_expression("改成昨天 14:00-18:00", logical_now=LOGICAL_NOW)

    assert result.status == TIME_RESOLVED
    value = result.timeWindow["value"]
    assert value["start"].startswith("2026-10-21T14:00")
    assert value["end"].startswith("2026-10-21T18:00")


def test_morning_period_also_needs_exact_range():
    result = resolve_time_expression("改成今天上午", logical_now=LOGICAL_NOW)

    assert result.status == TIME_NEEDS_CLARIFICATION
    assert result.options


def test_relative_window_stays_calendar():
    result = resolve_time_expression("把趋势改成最近 7 天", logical_now=LOGICAL_NOW)

    assert result.status == TIME_RESOLVED
    value = result.timeWindow["value"]
    assert value["type"] == "CALENDAR"
    assert value["unit"] == "DAY"
    assert value["offset"] == -7
    assert value["count"] == 7


def test_no_time_expression_returns_not_found():
    result = resolve_time_expression("把趋势图改成柱状图", logical_now=LOGICAL_NOW)

    assert result.status == TIME_NOT_FOUND
    assert result.timeWindow is None
    assert result.options == []


def test_clock_only_expression_keeps_draft_date():
    """只给钟点时沿用草稿当前时间窗的日期，不跳到今天。"""
    draft_window = {
        "mode": "REPLACE",
        "value": {
            "type": "CALENDAR",
            "timezone": "Asia/Shanghai",
            "anchor": {"type": "CONTEXT", "path": "runtime.logicalTime"},
            "unit": "DAY",
            "offset": -1,
            "count": 1,
        },
    }
    result = resolve_time_expression(
        "时间改成 09:00-11:00",
        logical_now=LOGICAL_NOW,
        current_time_window=draft_window,
    )

    assert result.status == TIME_RESOLVED
    assert result.window.anchorDateSource == "draft_window"
    assert result.timeWindow["value"]["start"].startswith("2026-10-21")


def test_option_can_be_converted_back_to_time_window():
    result = resolve_time_expression("改成昨天下午", logical_now=LOGICAL_NOW)
    option = result.options[0]

    window = build_time_window_from_option(option)
    assert window["mode"] == "REPLACE"
    assert window["value"]["type"] == "FIXED_INTERVAL"


def test_grain_words_are_not_mistaken_for_clock():
    """「10 点级」这种不是钟点，不能当时间解析。"""
    result = resolve_time_expression("改成 10 点级粒度", logical_now=LOGICAL_NOW)

    assert result.status == TIME_NOT_FOUND


def test_noon_period_maps_noon_hours():
    """「中午」语境下 12 点仍是 12 点，1 点算 13 点。"""
    noon = resolve_time_expression("改成中午12点到1点", logical_now=LOGICAL_NOW)
    assert noon.status == TIME_RESOLVED
    assert noon.timeWindow["value"]["start"].startswith("2026-10-22T12:00")
    assert noon.timeWindow["value"]["end"].startswith("2026-10-22T13:00")


def test_evening_period_maps_to_24h_clock():
    result = resolve_time_expression("改成昨天晚上8点到10点", logical_now=LOGICAL_NOW)

    assert result.status == TIME_RESOLVED
    assert result.timeWindow["value"]["start"].startswith("2026-10-21T20:00")
    assert result.timeWindow["value"]["end"].startswith("2026-10-21T22:00")


def test_calendar_window_shape_matches_engine_contract():
    """整段表达产出的结构必须与 EditOperation.timeWindow 现状一致。"""
    result = resolve_time_expression("最近 30 天", logical_now=LOGICAL_NOW)

    value = result.timeWindow["value"]
    assert value["type"] == "CALENDAR"
    assert value["timezone"] == "Asia/Shanghai"
    assert set(value) == {"type", "timezone", "anchor", "unit", "offset", "count"}
    assert result.timeWindow["mode"] == "REPLACE"


# ---------- 可见对象解析 ----------

def _regions() -> list[dict]:
    """模拟草稿：标题区 + 4 张正文卡片。"""
    return [
        {"id": "region_report_title", "name": "A 机房 昨日能耗日报", "role": "REPORT_TITLE"},
        {"id": "region_total_energy", "name": "昨日总耗电", "role": "SUMMARY"},
        {"id": "region_device_ranking", "name": "设备耗电排名", "role": "DETAIL"},
        {"id": "region_hourly_trend", "name": "小时趋势", "role": "TREND"},
        {"id": "region_data_quality", "name": "数据质量", "role": "QUALITY"},
    ]


def test_visible_cards_exclude_hidden_title_region():
    cards, stale, source = list_visible_cards(_regions())

    assert [card.regionId for card in cards] == [
        "region_total_energy",
        "region_device_ranking",
        "region_hourly_trend",
        "region_data_quality",
    ]
    assert source == "draft"
    assert stale == []


def test_third_card_skips_hidden_title_region():
    """「第三个」= 第 3 张正文卡片，隐藏标题区不占序号。"""
    result = resolve_visible_reference("删除第三个", _regions())

    assert result.status == REF_RESOLVED
    assert result.regionId == "region_hourly_trend"
    assert result.label == "小时趋势"
    assert result.visibleIndex == 3
    assert result.visibleCount == 4


def test_ordinal_uses_frontend_visible_order():
    """前端回传的可见顺序优先于草稿顺序。"""
    visible = ["region_data_quality", "region_hourly_trend", "region_total_energy"]
    result = resolve_visible_reference(
        "删除第三个", _regions(), visible_region_ids=visible
    )

    assert result.status == REF_RESOLVED
    assert result.regionId == "region_total_energy"
    assert result.orderSource == "frontend"


def test_ordinal_out_of_range_asks_with_candidates():
    result = resolve_visible_reference("删除第五个", _regions())

    assert result.status == REF_NEEDS_CLARIFICATION
    assert result.options
    assert "4" in result.question
    assert result.regionId == ""


def test_chinese_numerals_ordinal():
    result = resolve_visible_reference("把第二个改成柱状图", _regions())

    assert result.status == REF_RESOLVED
    assert result.regionId == "region_device_ranking"


def test_reverse_ordinal_counts_from_the_end():
    result = resolve_visible_reference("删除倒数第一个", _regions())

    assert result.status == REF_RESOLVED
    assert result.regionId == "region_data_quality"


def test_last_card_ordinal():
    result = resolve_visible_reference("删除最后一个", _regions())

    assert result.status == REF_RESOLVED
    assert result.regionId == "region_data_quality"


def test_demonstrative_with_selection_resolves():
    result = resolve_visible_reference(
        "把这个删掉", _regions(), selected_region_id="region_hourly_trend"
    )

    assert result.status == REF_RESOLVED
    assert result.regionId == "region_hourly_trend"


def test_demonstrative_without_selection_asks_with_candidates():
    result = resolve_visible_reference("把这个删掉", _regions())

    assert result.status == REF_NEEDS_CLARIFICATION
    assert result.options
    assert "小时趋势" in result.question


def test_no_reference_returns_not_found():
    result = resolve_visible_reference("改成柱状图", _regions())

    assert result.status == REF_NOT_FOUND
    assert result.regionId == ""


def test_stale_visible_ids_are_reported():
    """页面顺序里的 ID 在草稿中已不存在时，要报出来让集成层重新核对。"""
    result = resolve_visible_reference(
        "删除第一个",
        _regions(),
        visible_region_ids=["region_removed", "region_total_energy"],
    )

    assert "region_removed" in result.staleRegionIds


def test_empty_draft_asks_instead_of_guessing():
    result = resolve_visible_reference("删除第三个", [])

    assert result.status == REF_NEEDS_CLARIFICATION
    assert result.reason == "no_visible_cards"


@pytest.mark.parametrize(
    "text,expected_index",
    [
        ("删除第一个", 1),
        ("删除第二个", 2),
        ("删除第三个", 3),
        ("删除第四个", 4),
    ],
)
def test_ordinal_indexes(text, expected_index):
    result = resolve_visible_reference(text, _regions())

    assert result.status == REF_RESOLVED
    assert result.visibleIndex == expected_index
