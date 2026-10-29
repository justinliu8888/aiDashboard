"""造数器确定性：种子必须带「区域」，同比必须按「量族」分（规格 #46）。

规格原文（「固定时间与造数」）：

    确定性：按对象、逻辑日期、**区域**与字段组合哈希生成，同一份定义与
    同一时间窗恒定。

这里的「区域」是**报表区域/板块**（不是地理区域）：同一份规格里 ``regions[]``、
「区域 payload」、「非标题区域」都指报表板块。

这份文件钉住四件事：

1. **区域是种子材料的一部分** —— 同一份定义里两个板块的同类指标不撞值
   （PUE 卡、告警行、机柜行、功率趋势、通用行…）；
2. **同比按量族给** —— 总耗电与总电费是同一个量族（能耗）→ 同一个同比；
   PUE / 实时功率 / 告警各自一族 → 各自的同比；
3. **确定性不破** —— 同一份定义 + 同一时间窗：重复生成逐字段全等，
   **两个独立进程**（连 ``PYTHONHASHSEED`` 都不同）生成的结果也逐字段全等；
4. **勾稽不破** —— 卡片总量 = 设备表行和 = 能量趋势桶和；总电费 = 总耗电 × 单价；
   功率趋势按时长积分回总量。

用例全部是**行为断言**：不检查源码里有没有某个字符串，只看造出来的数字。
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.services.simulated_data_service import (
    SIMULATED_ELECTRICITY_PRICE_PER_KWH,
    generate_simulated_data,
)


BACKEND_DIR = Path(__file__).resolve().parent.parent

#: 子进程里跑的同一段代码：读一份输入（定义 + 上下文），吐出造出来的载荷。
_CHILD_SCRIPT = """
import json
import sys

from app.services.simulated_data_service import generate_simulated_data

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    bundle = json.load(handle)

payloads = generate_simulated_data(bundle["definition"], bundle["context"])
json.dump(payloads, sys.stdout, ensure_ascii=False, sort_keys=True)
"""


# ---------------------------------------------------------------- 定义构造


def _region(
    region_id: str,
    name: str,
    role: str,
    component: dict[str, Any],
    *,
    kind: str | None = None,
    unit: str | None = None,
    metrics: list[dict[str, Any]] | None = None,
    columns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {}
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


def _card(
    region_id: str,
    name: str,
    label: str,
    unit: str,
    kind: str,
    metric_key: str,
    precision: int = 1,
) -> dict[str, Any]:
    """一个指标卡区域（量身份写在 ``previewSpec`` 里，不按标题猜）。"""
    return _region(
        region_id,
        name,
        "SUMMARY",
        {
            "type": "METRIC_CARD",
            "metric": {
                "label": label,
                "format": {
                    "kind": "NUMBER",
                    "precision": precision,
                    "unitLabel": unit,
                },
            },
        },
        kind=kind,
        unit=unit,
        metrics=[
            {
                "metricKey": metric_key,
                "label": label,
                "kind": kind,
                "unit": unit,
                "capabilityPending": kind in ("pue", "cost", "power", "generic"),
            }
        ],
    )


def _chart(region_id: str, name: str, kind: str, unit: str) -> dict[str, Any]:
    return _region(
        region_id,
        name,
        "TREND",
        {"type": "CHART", "chartKind": "LINE", "encodings": []},
        kind=kind,
        unit=unit,
    )


def _table(
    region_id: str, name: str, columns: list[dict[str, Any]], kind: str
) -> dict[str, Any]:
    return _region(
        region_id,
        name,
        "DETAIL",
        {"type": "TABLE", "rowNumber": True, "columns": []},
        kind=kind,
        columns=columns,
    )


DEVICE_COLUMNS = [
    {"id": "deviceName", "label": "设备名称", "fieldRef": "deviceName"},
    {
        "id": "energyKwh",
        "label": "耗电量",
        "fieldRef": "energyKwh",
        "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
    },
]

ALARM_COLUMNS = [
    {"id": "level", "label": "告警级别", "fieldRef": "level"},
    {"id": "deviceName", "label": "设备名称", "fieldRef": "deviceName"},
    {"id": "time", "label": "告警时间", "fieldRef": "time"},
    {"id": "duration", "label": "持续时长", "fieldRef": "duration"},
]

CABINET_COLUMNS = [
    {"id": "deviceName", "label": "机柜名称", "fieldRef": "deviceName"},
    {"id": "status", "label": "运行状态", "fieldRef": "status"},
    {"id": "load", "label": "负载率", "fieldRef": "load"},
]


def _definition(regions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "metadata": {"name": "机房能耗日报", "definitionId": "def_seed_region_1"},
        "defaults": {
            "scope": {"selectors": {"machineRoomId": ["room_a"]}},
            "timeWindow": {
                "type": "CALENDAR",
                "timezone": "Asia/Shanghai",
                "unit": "DAY",
                "offset": -1,
                "count": 1,
            },
            "grain": {"unit": "HOUR"},
        },
        "regions": regions,
    }


def _context(**overrides: Any) -> dict[str, Any]:
    context = {
        "definitionId": "def_seed_region_1",
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


# ---------------------------------------------------------------- 区域级种子


def test_two_pue_regions_of_the_same_report_do_not_collapse_to_one_value():
    """同一份定义里两个 PUE 板块 → 两个不同的值。

    规格要求种子按「区域」组合哈希：区域不同，噪声就不同。
    """
    definition = _definition(
        [
            _card("r_pue_a", "PUE 总览", "PUE", "", "pue", "metric.machine_room.pue", 2),
            _card("r_pue_b", "PUE 对比", "PUE", "", "pue", "metric.machine_room.pue", 2),
        ]
    )
    payloads = generate_simulated_data(definition, _context())

    assert payloads["r_pue_a"]["value"] != payloads["r_pue_b"]["value"]
    # 两边都还在 PUE 的合理区间里（规格：PUE 落在 1.2~1.8）。
    for region_id in ("r_pue_a", "r_pue_b"):
        assert 1.2 <= payloads[region_id]["value"] <= 1.8


def test_two_alarm_regions_do_not_produce_the_same_rows():
    """两个告警板块 → 两套不同的告警行。"""
    definition = _definition(
        [
            _table("r_alarm_a", "当前告警列表", ALARM_COLUMNS, "alarms"),
            _table("r_alarm_b", "告警历史", ALARM_COLUMNS, "alarms"),
        ]
    )
    payloads = generate_simulated_data(definition, _context())

    rows_a = payloads["r_alarm_a"]["rows"]
    rows_b = payloads["r_alarm_b"]["rows"]
    assert len(rows_a) == len(rows_b) == 5
    assert rows_a != rows_b


def test_two_cabinet_status_regions_do_not_produce_the_same_rows():
    """两个机柜状态板块 → 两套不同的状态行。"""
    definition = _definition(
        [
            _table("r_cabinet_a", "机柜状态列表", CABINET_COLUMNS, "cabinet_status"),
            _table("r_cabinet_b", "机柜状态（另一机房）", CABINET_COLUMNS, "cabinet_status"),
        ]
    )
    payloads = generate_simulated_data(definition, _context())

    rows_a = payloads["r_cabinet_a"]["rows"]
    rows_b = payloads["r_cabinet_b"]["rows"]
    assert len(rows_a) == len(rows_b)
    assert rows_a != rows_b


def test_two_power_trend_regions_do_not_produce_the_same_points():
    """两个功率趋势板块 → 两组不同的点（趋势本身按桶定位，也必须带区域）。"""
    definition = _definition(
        [
            _chart("r_power_a", "功率趋势", "power", "kW"),
            _chart("r_power_b", "另一段功率趋势", "power", "kW"),
        ]
    )
    payloads = generate_simulated_data(definition, _context())

    points_a = payloads["r_power_a"]["yValues"]
    points_b = payloads["r_power_b"]["yValues"]
    assert len(points_a) == len(points_b) == 24
    assert points_a != points_b


def test_two_generic_regions_do_not_produce_the_same_rows():
    """两个通用板块 → 两套不同的示例值。"""
    definition = _definition(
        [
            _table("r_generic_a", "某种指标", CABINET_COLUMNS, "generic"),
            _table("r_generic_b", "另一种指标", CABINET_COLUMNS, "generic"),
        ]
    )
    payloads = generate_simulated_data(definition, _context())

    assert payloads["r_generic_a"]["rows"] != payloads["r_generic_b"]["rows"]


def test_renaming_one_region_only_changes_that_region():
    """**区域标识确实是种子材料的一部分**（行为断言，不看源码）。

    只把第二个板块的 id 改掉：
    - 第一个板块的数字**一点不变**（它自己的种子没动）；
    - 被改名的板块数字**变了**（它的种子里有区域标识）。
    """
    first = _card("r_pue_a", "PUE 总览", "PUE", "", "pue", "metric.machine_room.pue", 2)
    second = _card("r_pue_b", "PUE 对比", "PUE", "", "pue", "metric.machine_room.pue", 2)
    definition = _definition([first, second])

    renamed = copy.deepcopy(definition)
    renamed["regions"][1]["id"] = "r_pue_c"

    before = generate_simulated_data(definition, _context())
    after = generate_simulated_data(renamed, _context())

    assert before["r_pue_a"] == after["r_pue_a"], "别的板块不该跟着变"
    assert before["r_pue_b"]["value"] != after["r_pue_c"]["value"], "区域标识必须进种子"


# ---------------------------------------------------------------- 同比按量族


def test_total_cost_card_and_total_energy_card_share_the_same_yoy_percent():
    """总电费卡与总耗电卡：**同一个量族 → 同一个同比%**。

    总电费 = 总耗电 × 单价，两张卡说的是同一个量的两个面；同比不同的话，
    同一份报表内部就自相矛盾。
    """
    definition = _definition(
        [
            _card(
                "r_total",
                "总耗电",
                "总耗电",
                "kWh",
                "energy",
                "metric.machine_room.total_energy",
            ),
            _card(
                "r_cost",
                "总电费",
                "能耗成本",
                "元",
                "cost",
                "metric.machine_room.energy_cost",
                2,
            ),
        ]
    )
    payloads = generate_simulated_data(definition, _context())

    assert payloads["r_total"]["yoyPercent"] == payloads["r_cost"]["yoyPercent"]


def test_pue_and_power_cards_have_their_own_yoy_percent():
    """PUE / 实时功率是**各自的量** → 各自的同比，不跟能耗族共用。"""
    definition = _definition(
        [
            _card(
                "r_total",
                "总耗电",
                "总耗电",
                "kWh",
                "energy",
                "metric.machine_room.total_energy",
            ),
            _card("r_pue", "PUE 总览", "PUE", "", "pue", "metric.machine_room.pue", 2),
            _card(
                "r_power",
                "实时功率",
                "实时功率",
                "kW",
                "power",
                "metric.machine_room.realtime_power",
                2,
            ),
        ]
    )
    payloads = generate_simulated_data(definition, _context())

    energy_yoy = payloads["r_total"]["yoyPercent"]
    assert payloads["r_pue"]["yoyPercent"] != energy_yoy
    assert payloads["r_power"]["yoyPercent"] != energy_yoy
    assert payloads["r_pue"]["yoyPercent"] != payloads["r_power"]["yoyPercent"]


def test_two_regions_of_the_same_quantity_share_the_same_yoy_percent():
    """同一个量的同比**跨区域共享**（同比按量族给，不按区域给）。

    两张能耗卡说的是同一个量（同一份定义、同一时间窗、同一对象范围），
    同比必须一样 —— 区域只影响噪声，不影响「同一个量的同比」。
    """
    definition = _definition(
        [
            _card(
                "r_total",
                "总耗电",
                "总耗电",
                "kWh",
                "energy",
                "metric.machine_room.total_energy",
            ),
            _card(
                "r_total_copy",
                "总耗电（另一个板块）",
                "总耗电",
                "kWh",
                "energy",
                "metric.machine_room.total_energy",
            ),
        ]
    )
    payloads = generate_simulated_data(definition, _context())

    assert payloads["r_total"]["yoyPercent"] == payloads["r_total_copy"]["yoyPercent"]


# ---------------------------------------------------------------- 确定性


def _full_report() -> dict[str, Any]:
    """一份覆盖全部造数路径的定义。"""
    return _definition(
        [
            _card(
                "r_total",
                "总耗电",
                "总耗电",
                "kWh",
                "energy",
                "metric.machine_room.total_energy",
            ),
            _card(
                "r_cost",
                "总电费",
                "能耗成本",
                "元",
                "cost",
                "metric.machine_room.energy_cost",
                2,
            ),
            _card("r_pue", "PUE 总览", "PUE", "", "pue", "metric.machine_room.pue", 2),
            _table("r_alarm", "当前告警列表", ALARM_COLUMNS, "alarms"),
            _table("r_cabinet", "机柜状态列表", CABINET_COLUMNS, "cabinet_status"),
            _table("r_devices", "设备耗电明细", DEVICE_COLUMNS, "energy"),
            _chart("r_trend", "总耗电趋势", "energy", "kWh"),
            _chart("r_power_trend", "功率趋势", "power", "kW"),
        ]
    )


def test_repeated_generation_is_field_by_field_identical():
    """同一份定义 + 同一时间窗 → 重复生成逐字段全等。"""
    definition = _full_report()
    context = _context()

    first = generate_simulated_data(definition, context)
    second = generate_simulated_data(definition, context)

    assert first == second
    # 逐区域再钉一遍（防止将来只在某个区域上破）。
    for region_id, payload in first.items():
        assert payload == second[region_id], region_id


def _generate_in_child_process(bundle: dict[str, Any], path: Path, hash_seed: str):
    """在**独立解释器**里跑同一段代码，把载荷吐回来。"""
    path.write_text(
        json.dumps(bundle, ensure_ascii=False), encoding="utf-8"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND_DIR)
    # 连哈希随机化都故意设成不同的：造数器不许受 PYTHONHASHSEED 影响。
    env["PYTHONHASHSEED"] = hash_seed
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT, str(path)],
        capture_output=True,
        text=True,
        cwd=str(BACKEND_DIR),
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, f"子进程跑失败：\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_two_independent_processes_produce_identical_payloads(tmp_path):
    """跨进程确定性：两个独立 Python 进程 → 载荷逐字段全等。

    两个进程的 ``PYTHONHASHSEED`` 故意不同（``0`` / ``12345``），
    所以这条同时证明造数器**不受进程级哈希随机化影响**。
    """
    bundle = {"definition": _full_report(), "context": _context()}

    first = _generate_in_child_process(bundle, tmp_path / "bundle_1.json", "0")
    second = _generate_in_child_process(bundle, tmp_path / "bundle_2.json", "12345")

    assert first == second
    # 跟本进程生成的也要一致（同一份定义 + 同一时间窗 → 同一组数）。
    assert first == json.loads(
        json.dumps(generate_simulated_data(bundle["definition"], bundle["context"]))
    )


def test_generator_version_change_still_changes_the_numbers():
    """生成器版本一变，数字跟着变（旧语义不许被改坏）。

    - 旧上下文重新生成 → 还是旧数字（版本没变，种子就没变）；
    - 新版本上下文 → 数字变了（不会拿旧版本的数字冒充新版本的示意）。
    """
    definition = _full_report()
    old_context = _context()
    old_payloads = generate_simulated_data(definition, old_context)

    new_context = _context(generatorVersion="simulated-data/2")
    new_payloads = generate_simulated_data(definition, new_context)

    assert new_payloads["r_pue"]["value"] != old_payloads["r_pue"]["value"]
    assert new_payloads["r_power_trend"]["yValues"] != old_payloads["r_power_trend"]["yValues"]
    # 旧上下文重新生成还是旧数字。
    again = generate_simulated_data(definition, old_context)
    assert again == old_payloads


# ---------------------------------------------------------------- 勾稽


def test_reconciliation_still_holds_with_the_region_seed():
    """加了区域种子之后，勾稽**一条都不能破**。

    - 卡片总量 = 设备表各行之和 = 能量趋势各桶之和；
    - 总电费 = 总耗电 × 单价；
    - 功率趋势按时长积分 → 回到总耗电。
    """
    definition = _full_report()
    payloads = generate_simulated_data(definition, _context())

    card = payloads["r_total"]["value"]
    rows_sum = sum(row["energyKwh"] for row in payloads["r_devices"]["rows"])
    buckets_sum = sum(payloads["r_trend"]["yValues"])

    assert round(rows_sum, 6) == round(card, 6)
    assert round(buckets_sum, 6) == round(card, 6)
    assert round(payloads["r_cost"]["value"], 6) == round(
        card * SIMULATED_ELECTRICITY_PRICE_PER_KWH, 6
    )
    # HOUR 粒度：每桶 1 小时，功率 × 时长之和回到总耗电。
    power_integral = sum(power * 1.0 for power in payloads["r_power_trend"]["yValues"])
    assert round(power_integral, 3) == round(card, 3)


def test_energy_totals_are_not_disturbed_by_the_region_seed():
    """主数值来自共享基础数据，**不随区域种子变**。

    同一个量（总耗电）在不同板块里必须还是同一个数 —— 区域只影响噪声项。
    """
    definition = _definition(
        [
            _card(
                "r_total",
                "总耗电",
                "总耗电",
                "kWh",
                "energy",
                "metric.machine_room.total_energy",
            ),
            _card(
                "r_total_copy",
                "总耗电（另一个板块）",
                "总耗电",
                "kWh",
                "energy",
                "metric.machine_room.total_energy",
            ),
        ]
    )
    payloads = generate_simulated_data(definition, _context())

    assert payloads["r_total"]["value"] == payloads["r_total_copy"]["value"]
