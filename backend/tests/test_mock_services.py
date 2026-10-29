"""Mock 目录 / 指标服务测试：FakeAgent 的事实来源不能被改坏。

后半段是**造数器扩展**（04 号票）的测试：模拟业务数据必须像真的——
卡片总量 = 设备表各行之和 = 能量趋势各桶之和；同一份定义 + 同一时间窗，
反复生成结果完全相同。
"""
from __future__ import annotations

from app.services import catalog_mock_service, metric_mock_service
from app.services.mock_data_service import ROOM_A_DEVICES
from app.services.simulated_data_service import (
    ALARM_ROW_COUNT,
    SIMULATED_ELECTRICITY_PRICE_PER_KWH,
    SimulatedDataError,
    generate_simulated_data,
)


def test_machine_room_catalog_registered():
    catalogs = catalog_mock_service.list_catalogs("machine_room")
    refs = {c["sourceRef"] for c in catalogs}
    assert "catalog.machine_room.energy" in refs


def test_minute_grain_is_not_supported():
    """首功能明确：机房能耗没有分钟级历史，样例 4 的拦截依赖这一点。"""
    source_ref = "catalog.machine_room.energy"
    assert catalog_mock_service.supports_grain(source_ref, "HOUR") is True
    assert catalog_mock_service.supports_grain(source_ref, "DAY") is True
    assert catalog_mock_service.supports_grain(source_ref, "MINUTE") is False


def test_metrics_registered():
    refs = {m["metricRef"] for m in metric_mock_service.list_metrics()}
    assert refs >= {
        "metric.machine_room.total_energy",
        "metric.machine_room.device_energy",
    }


def test_metric_units_are_kwh():
    for ref in (
        "metric.machine_room.total_energy",
        "metric.machine_room.device_energy",
    ):
        metric = metric_mock_service.get_metric(ref)
        assert metric is not None
        assert metric["unit"] == "kWh"


# ============================================================ 造数器扩展


def _region(region_id, name, role, component, *, kind=None, unit=None, metrics=None,
            columns=None):
    """造一个带 previewSpec 的区域（previewSpec 由 03 号票写入定义）。"""
    spec = {}
    if kind:
        spec["kind"] = kind
    if unit:
        spec["unit"] = unit
    if metrics:
        spec["metrics"] = metrics
    if columns is not None:
        spec["columns"] = columns
    return {
        "id": region_id,
        "name": name,
        "role": role,
        "component": component,
        "templateBinding": {"previewSpec": spec},
    }


def _metric_card(region_id, name, label, unit, precision=1, kind=None):
    """指标卡区域。

    ``kind`` 就是 03 号票写进 ``previewSpec`` 的**量身份**——造数器靠它
    决定这一块出的是什么量，不按标题猜。
    """
    region = _region(
        region_id,
        name,
        "SUMMARY",
        {
            "type": "METRIC_CARD",
            "metric": {
                "label": label,
                "format": {"kind": "NUMBER", "precision": precision, "unitLabel": unit},
            },
        },
        kind=kind,
        unit=unit,
    )
    if kind:
        region["templateBinding"]["previewSpec"]["metrics"] = [
            {
                "metricKey": kind,
                "label": label,
                "kind": kind,
                "unit": unit,
                "capabilityPending": kind in ("pue", "cost", "power", "generic"),
            }
        ]
    return region


def _table(region_id, name, columns, *, kind="generic"):
    return _region(
        region_id,
        name,
        "DETAIL",
        {"type": "TABLE", "rowNumber": True, "columns": []},
        kind=kind,
        columns=columns,
    )


def _definition(regions, *, timezone="Asia/Shanghai", grain="HOUR"):
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
        "regions": regions,
    }


def _context(**overrides):
    context = {
        "definitionId": "def_gen_1",
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


DEVICE_COLUMNS = [
    {"id": "deviceName", "label": "设备名称", "fieldRef": "deviceName"},
    {
        "id": "energyKwh",
        "label": "耗电量",
        "fieldRef": "energyKwh",
        "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
    },
]


def _energy_report():
    """一份含能耗系全部板块的定义（卡片 / 趋势 / 设备表）。"""
    return _definition(
        [
            _metric_card("r_total", "总耗电", "总耗电", "kWh", kind="energy"),
            _region(
                "r_trend",
                "耗电趋势",
                "TREND",
                {"type": "CHART", "chartKind": "LINE", "encodings": []},
                kind="energy",
                unit="kWh",
            ),
            _table("r_table", "设备耗电排名", DEVICE_COLUMNS, kind="energy"),
        ]
    )


def _with_kind(region_id, name, kind, unit, label=None):
    """带单一指标身份的指标卡区域（与 03 号票写进 previewSpec 的形状一致）。"""
    return _metric_card(region_id, name, label or name, unit, kind=kind)


# ---------------------------------------------------------------- 覆盖范围


def test_every_non_title_region_gets_a_payload():
    definition = _definition(
        [
            {
                "id": "region_report_title",
                "name": "机房能耗日报",
                "role": "REPORT_TITLE",
                "component": {"type": "TEXT", "content": {"template": "机房能耗日报"}},
            },
            _metric_card("r_total", "总耗电", "总耗电", "kWh", kind="energy"),
        ]
    )
    payloads = generate_simulated_data(definition, _context())
    assert "region_report_title" not in payloads
    assert set(payloads) == {"r_total"}


def test_payload_keys_match_definition_region_ids():
    definition = _energy_report()
    payloads = generate_simulated_data(definition, _context())
    assert set(payloads) == {r["id"] for r in definition["regions"]}


# ---------------------------------------------------------------- 勾稽


def test_card_total_equals_device_table_rows_sum():
    definition = _energy_report()
    payloads = generate_simulated_data(definition, _context())
    card = payloads["r_total"]["value"]
    rows = payloads["r_table"]["rows"]
    assert round(sum(row["energyKwh"] for row in rows), 6) == round(card, 6)


def test_card_total_equals_energy_trend_bucket_sum():
    definition = _energy_report()
    payloads = generate_simulated_data(definition, _context())
    card = payloads["r_total"]["value"]
    buckets = payloads["r_trend"]["yValues"]
    assert round(sum(buckets), 6) == round(card, 6)


def test_energy_trend_has_24_buckets_for_a_single_day():
    definition = _energy_report()
    payloads = generate_simulated_data(definition, _context())
    trend = payloads["r_trend"]
    assert len(trend["yValues"]) == 24
    assert len(trend["xLabels"]) == 24
    assert len(trend["points"]) == 24


def test_power_trend_integrates_back_to_total_energy():
    """功率趋势**按时长积分**回到总耗电（不是把 kWh 曲线再积分）。"""
    definition = _definition(
        [
            _metric_card("r_total", "总耗电", "总耗电", "kWh", kind="energy"),
            _region(
                "r_power_trend",
                "功率趋势",
                "TREND",
                {"type": "CHART", "chartKind": "LINE", "encodings": []},
                kind="power",
                unit="kW",
            ),
        ]
    )
    payloads = generate_simulated_data(definition, _context())
    total = payloads["r_total"]["value"]
    powers = payloads["r_power_trend"]["yValues"]
    assert len(powers) == 24
    integral = sum(power * 1.0 for power in powers)
    assert round(integral, 3) == round(total, 3)


def test_total_cost_equals_total_energy_times_unit_price():
    definition = _definition(
        [
            _metric_card("r_total", "总耗电", "总耗电", "kWh", kind="energy"),
            _with_kind("r_cost", "总电费", "cost", "元"),
        ]
    )
    payloads = generate_simulated_data(definition, _context())
    energy = payloads["r_total"]["value"]
    cost = payloads["r_cost"]["value"]
    assert round(cost, 6) == round(energy * SIMULATED_ELECTRICITY_PRICE_PER_KWH, 6)


def test_pue_falls_in_the_reasonable_range():
    definition = _definition([_with_kind("r_pue", "PUE", "pue", "")])
    payloads = generate_simulated_data(definition, _context())
    pue = payloads["r_pue"]["value"]
    assert 1.2 <= pue <= 1.8


# ---------------------------------------------------------------- 表格


def test_device_table_row_count_equals_device_count():
    definition = _energy_report()
    payloads = generate_simulated_data(definition, _context())
    assert len(payloads["r_table"]["rows"]) == len(ROOM_A_DEVICES)


def test_alarm_list_has_about_five_rows():
    columns = [
        {"id": "level", "label": "告警级别", "fieldRef": "level"},
        {"id": "deviceName", "label": "设备名称", "fieldRef": "deviceName"},
        {"id": "time", "label": "告警时间", "fieldRef": "time"},
    ]
    definition = _definition([_table("r_alarm", "当前告警列表", columns, kind="alarms")])
    payloads = generate_simulated_data(definition, _context())
    rows = payloads["r_alarm"]["rows"]
    assert len(rows) == ALARM_ROW_COUNT
    assert all(row["level"] for row in rows)
    assert all(row["deviceName"] for row in rows)
    assert all(row["time"] for row in rows)


def test_cabinet_status_row_count_equals_device_count():
    columns = [
        {"id": "deviceName", "label": "机柜名称", "fieldRef": "deviceName"},
        {"id": "status", "label": "运行状态", "fieldRef": "status"},
        {"id": "load", "label": "负载率", "fieldRef": "load"},
    ]
    definition = _definition(
        [_table("r_cabinet", "机柜状态列表", columns, kind="cabinet_status")]
    )
    payloads = generate_simulated_data(definition, _context())
    rows = payloads["r_cabinet"]["rows"]
    assert len(rows) == len(ROOM_A_DEVICES)
    assert all(row["status"] for row in rows)


def test_every_column_field_ref_has_a_cell_in_every_row():
    columns = [
        {"id": "level", "label": "告警级别", "fieldRef": "level"},
        {"id": "deviceName", "label": "设备名称", "fieldRef": "deviceName"},
        {"id": "time", "label": "告警时间", "fieldRef": "time"},
        {"id": "duration", "label": "持续时长", "fieldRef": "duration"},
    ]
    definition = _definition([_table("r_alarm", "当前告警列表", columns, kind="alarms")])
    payloads = generate_simulated_data(definition, _context())
    table = payloads["r_alarm"]
    for row in table["rows"]:
        for column in table["columns"]:
            assert column["fieldRef"] in row
            assert row[column["fieldRef"]] not in (None, "")


def test_table_without_columns_fails_explicitly():
    definition = _definition([_table("r_empty", "没有栏目的表", [], kind="energy")])
    try:
        generate_simulated_data(definition, _context())
    except SimulatedDataError as exc:
        assert exc.code == "MISSING_COLUMNS"
    else:
        raise AssertionError("缺列的表格必须明确失败，不能默默给空表")


# ---------------------------------------------------------------- 确定性


def test_same_definition_and_window_generate_identical_numbers():
    definition = _energy_report()
    first = generate_simulated_data(definition, _context())
    second = generate_simulated_data(definition, _context())
    assert first == second


def test_generator_does_not_write_files_or_persist_anything(tmp_path, monkeypatch):
    """生成器只在内存里出数：不落库、不写文件。"""
    monkeypatch.chdir(tmp_path)
    definition = _energy_report()
    generate_simulated_data(definition, _context())
    assert list(tmp_path.iterdir()) == []


def test_unmatched_metric_still_produces_a_simulated_value():
    """陌生指标不因目录未匹配而留空：给出**明确标注为模拟**的示例。"""
    definition = _definition([_with_kind("r_generic", "某种没接通的指标", "generic", "kWh")])
    payloads = generate_simulated_data(definition, _context())
    card = payloads["r_generic"]
    assert card["value"] is not None
    assert card["dataSource"] == "simulated"
    assert card["metrics"][0]["capabilityPending"] is True


# ---------------------------------------------------------------- 失败路径


def test_unresolvable_grain_fails_explicitly():
    definition = _energy_report()
    try:
        generate_simulated_data(definition, _context(grainUnit="FORTNIGHT"))
    except SimulatedDataError as exc:
        assert exc.code == "UNRESOLVED_GRAIN"
    else:
        raise AssertionError("无法解析的粒度必须明确失败")


def test_unknown_component_type_fails_explicitly():
    definition = _definition(
        [{"id": "r_x", "name": "怪板块", "role": "DETAIL", "component": {"type": "GAUGE"}}]
    )
    try:
        generate_simulated_data(definition, _context())
    except SimulatedDataError as exc:
        assert exc.code == "UNSUPPORTED_COMPONENT"
    else:
        raise AssertionError("未知组件类型必须明确失败")


def test_missing_window_fails_explicitly():
    definition = _energy_report()
    context = _context()
    context.pop("windowStart")
    context.pop("windowEnd")
    context.pop("anchorTime")
    try:
        generate_simulated_data(definition, context)
    except SimulatedDataError as exc:
        assert exc.code == "INVALID_CONTEXT"
    else:
        raise AssertionError("缺时间窗必须明确失败")


# ---------------------------------------------------------------- 口径范围


def test_filtered_scope_stays_reconciled():
    """带设备过滤的报表：卡片、表格、趋势落在**同一个**对象范围上。"""
    definition = _energy_report()
    conditions = [{"fieldRef": "deviceId", "op": "IN", "values": ["crac_01", "crac_02"]}]
    for region in definition["regions"]:
        region["data"] = {
            "pipeline": {
                "nodes": [
                    {"id": "n_filter", "type": "FILTER", "inputs": ["input:main"],
                     "conditions": conditions},
                    {"id": "n_metric", "type": "METRIC", "inputs": ["node:n_filter"],
                     "metricRef": "metric.machine_room.total_energy"},
                ]
            }
        }
    payloads = generate_simulated_data(definition, _context())
    card = payloads["r_total"]["value"]
    rows = payloads["r_table"]["rows"]
    assert len(rows) == 2
    assert round(sum(row["energyKwh"] for row in rows), 6) == round(card, 6)
    assert round(sum(payloads["r_trend"]["yValues"]), 6) == round(card, 6)


def test_multi_metric_region_consumes_every_metric():
    """多指标区域消费 metrics[]，不是只看主量。"""
    region = _metric_card("r_multi", "能耗与电费", "能耗与电费", "kWh", kind="energy")
    region["templateBinding"]["previewSpec"]["metrics"] = [
        {"metricKey": "energy", "label": "总耗电", "kind": "energy", "unit": "kWh"},
        {"metricKey": "cost", "label": "总电费", "kind": "cost", "unit": "元"},
    ]
    payloads = generate_simulated_data(_definition([region]), _context())
    metrics = payloads["r_multi"]["metrics"]
    assert [m["metricKey"] for m in metrics] == ["energy", "cost"]
    assert metrics[1]["value"] == round(
        metrics[0]["value"] * SIMULATED_ELECTRICITY_PRICE_PER_KWH, 2
    )


def test_payload_shape_matches_the_public_preview_contract():
    """载荷就是既有公开预览接口的数据对象：前端读同一个字段。"""
    definition = _definition(
        [
            _metric_card("r_total", "总耗电", "总耗电", "kWh", kind="energy"),
            _region(
                "r_trend",
                "耗电趋势",
                "TREND",
                {"type": "CHART", "chartKind": "LINE", "encodings": []},
                kind="energy",
                unit="kWh",
            ),
            _table("r_table", "设备耗电排名", DEVICE_COLUMNS, kind="energy"),
            {
                "id": "r_quality",
                "name": "数据质量",
                "role": "QUALITY",
                "component": {"type": "DATA_QUALITY", "fields": ["COVERAGE"]},
            },
            {
                "id": "r_text",
                "name": "说明",
                "role": "DESCRIPTION",
                "component": {"type": "TEXT", "content": {"template": "这是说明"}},
            },
        ]
    )
    payloads = generate_simulated_data(definition, _context())
    card = payloads["r_total"]
    assert {"label", "value", "unit", "precision", "windowLabel"} <= set(card)
    trend = payloads["r_trend"]
    assert {"chartKind", "grain", "xField", "yField", "xLabels", "yValues", "points"} <= set(trend)
    assert len(trend["points"]) == len(trend["xLabels"]) == len(trend["yValues"])
    table = payloads["r_table"]
    assert {"columns", "rowNumber", "rows"} <= set(table)
    quality = payloads["r_quality"]
    assert {"coverage", "missing", "duplicates", "freshnessSeconds"} <= set(quality)
    text = payloads["r_text"]
    assert {"rendered", "template"} <= set(text)
