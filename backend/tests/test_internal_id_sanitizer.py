"""用户可见文字里的**内部标识**清洗（``internal_id_sanitizer``）。

判据全是**形状**，不认具体名字：这里不放任何业务词表。
"""

from __future__ import annotations

from app.services.internal_id_sanitizer import (
    sanitize_change_set_summaries,
    sanitize_internal_ids,
)


def test_前缀加分隔符的内部标识被换掉或去掉():
    # 目录里登记过的指标 → 换成业务名
    out = sanitize_internal_ids("数据源 ds_room_energy 的 metric.machine_room.total_energy")
    assert "ds_room_energy" not in out
    assert "metric.machine_room.total_energy" not in out
    assert "总耗电" in out


def test_卡片标识被去掉():
    out = sanitize_internal_ids("在「核心运行指标总览」指标卡（region_1）中追加 PUE")
    assert "region_1" not in out
    assert "核心运行指标总览" in out
    assert "PUE" in out


def test_字段名与内部枚举值被去掉():
    out = sanitize_internal_ids("因该区块当前为模拟占位（capabilityPending）")
    assert "capabilityPending" not in out
    assert "模拟占位" in out

    out = sanitize_internal_ids("expects: SINGLE_CHOICE，bindingKind=simulated")
    assert "SINGLE_CHOICE" not in out
    assert "bindingKind" not in out
    assert "simulated" not in out


def test_业务词与单位不受影响():
    text = "总耗电 5581.4kWh，比上一周期上涨 11.7%，口径按 DAY 统计"
    assert sanitize_internal_ids(text) == text
    text = "PUE 1.6，机柜平均温度 IT 设备"
    assert sanitize_internal_ids(text) == text


def test_留下的空括号被清掉():
    out = sanitize_internal_ids("指标（metric.machine_room.pue）保留")
    assert "（）" not in out
    assert "（）" not in out
    assert "保留" in out


def test_只剩等号或冒号的括号残渣被清掉():
    # 模型把 metricKey 写进摘要时：标识先被替换、字段名后被去掉，
    # 括号里只剩一个等号。这种残渣不能留在用户可见文字里。
    out = sanitize_internal_ids("先删除指标「实时功率」（metricKey=metric.power.section_1_2）")
    assert "（=）" not in out
    assert "=" not in out
    assert "实时功率" in out

    out = sanitize_internal_ids("（fieldRef: energyKwh）")
    assert "（:）" not in out
    assert ":" not in out


def test_变更集摘要也洗一遍():
    steps = [
        {"intent": "ADD_METRIC", "summary": "追加 PUE（metric.machine_room.pue）"},
        {"intent": "CHANGE_METRIC", "summary": "换成 机柜平均温度"},
    ]
    out = sanitize_change_set_summaries(steps)
    assert "metric.machine_room.pue" not in out[0]["summary"]
    assert out[1]["summary"] == "换成 机柜平均温度"
    # 原对象不被改（调用方可能还要拿原件落库）
    assert "metric.machine_room.pue" in steps[0]["summary"]
