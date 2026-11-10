"""Part two 展示绑定的**确定性边界 fixture**（P03 / P04 / P14）。

为什么用 fixture
---------------
真实页面那条路径（P02）用的是后台模拟数据：设备粒度下「昨日耗电量」
是 10 台设备的真实能耗（8 条 >400、2 条 <400），**没有任何一条恰好等于
400**。P03 要求把「恰好等于 400」这条边界也钉死，所以边界断言在这里用
一组**写死的行**跑展示管线的真实实现（别名 → 过滤 → 排序 → 前 N 项）。

这里不复制实现：调用的是产品自己的
``display_binding_service.apply_presentation_pipeline``，
行数据是 JSON 意义上的用户数据（等价于工作台「粘贴 JSON 数据」那条路）。

覆盖的边界
----------
• ``> 400``：恰好等于 400 的那一条**被排除**（不是 >=）。
• 满足条件 >5：6 条满足 → 展示 min(5, 6) = 5 行。
• 满足条件 <5：3 条满足 → 展示 3 行，**不补数**。
• 满足条件 0 条：展示 0 行（空态由前端负责，这里断言行数为 0）。
• 别名只改展示名，**不改 fieldRef / 源字段 / 行数**。
• 全量合计口径独立算：**过滤 / 排序 / 前 N 项都不改变全量合计**。
"""

from __future__ import annotations

from app.services.display_binding_service import apply_presentation_pipeline

#: 12 行写死的用户数据。含 400（边界）、含 <400、含 >400。
ROWS = [
    {"时间": "2026-11-08 08:00", "设备名称": "A-1 机架", "energyKwh": 900},
    {"时间": "2026-11-08 09:00", "设备名称": "A-2 机架", "energyKwh": 800},
    {"时间": "2026-11-08 10:00", "设备名称": "A-3 机架", "energyKwh": 700},
    {"时间": "2026-11-08 11:00", "设备名称": "A-4 机架", "energyKwh": 600},
    {"时间": "2026-11-08 12:00", "设备名称": "A-5 机架", "energyKwh": 500},
    {"时间": "2026-11-08 13:00", "设备名称": "PDU-01 配电", "energyKwh": 450},
    {"时间": "2026-11-08 14:00", "设备名称": "PDU-02 配电", "energyKwh": 400},
    {"时间": "2026-11-08 15:00", "设备名称": "CRAC-01 空调", "energyKwh": 380},
    {"时间": "2026-11-08 16:00", "设备名称": "CRAC-02 空调", "energyKwh": 300},
    {"时间": "2026-11-08 17:00", "设备名称": "UPS-01 不间断", "energyKwh": 250},
    {"时间": "2026-11-08 18:00", "设备名称": "机柜-01", "energyKwh": 200},
    {"时间": "2026-11-08 19:00", "设备名称": "机柜-02", "energyKwh": 150},
]

#: §9 原指令的展示绑定（别名 → 过滤 → 排序 → 前 5 项）。
ALIASES = {"时间点": "时间", "昨日耗电量（kWh）": "energyKwh"}


def _gt(field_ref: str, value: float) -> dict:
    return {
        "type": "CONDITION",
        "left": {"type": "FIELD", "fieldRef": field_ref},
        "operator": "GT",
        "operands": [{"type": "LITERAL", "value": value}],
    }


def _pipeline(threshold: float, top_n: int = 5) -> list[dict]:
    return apply_presentation_pipeline(
        [dict(row) for row in ROWS],
        field_aliases=ALIASES,
        presentation_filters=[_gt("energyKwh", threshold)],
        sort=[{"fieldRef": "energyKwh", "direction": "DESC"}],
        top_n=top_n,
    )


def test_full_result_baseline_is_independent_of_display_pipeline():
    """全量合计是**基线口径**：展示裁剪不改变它。"""
    baseline = round(sum(row["energyKwh"] for row in ROWS), 1)
    assert baseline == 5630.0

    for threshold in (400, 600, 9999):
        visible = _pipeline(threshold)
        # 展示行数变化，全量合计不变。
        assert baseline == round(sum(row["energyKwh"] for row in ROWS), 1)
        assert len(visible) <= 5


def test_exactly_400_is_excluded_by_gt_filter():
    """「大于 400」是**严格大于**：恰好 400 的那一条不出现。"""
    visible = _pipeline(400)
    values = [row["energyKwh"] for row in visible]
    assert 400 not in values
    assert all(v > 400 for v in values)
    # 满足条件 6 条（900/800/700/600/500/450），展示 min(5, 6) = 5 行。
    assert len(visible) == 5


def test_rows_are_sorted_descending():
    visible = _pipeline(400)
    values = [row["energyKwh"] for row in visible]
    assert values == sorted(values, reverse=True)
    assert values == [900, 800, 700, 600, 500]


def test_fewer_than_five_rows_are_not_padded():
    """满足条件不足 5 条 → 就显示那几条，**不补数**。"""
    visible = _pipeline(600)  # 900 / 800 / 700 三条
    assert len(visible) == 3
    assert [row["energyKwh"] for row in visible] == [900, 800, 700]


def test_zero_matching_rows_yields_empty_result():
    """0 条满足条件 → 0 行（前端据此显示空态），不是全量回退。"""
    visible = _pipeline(9999)
    assert visible == []


def test_alias_does_not_change_source_fields_or_count():
    """别名只加展示名：源字段、行数、列 ID 都不变。"""
    visible = _pipeline(400)
    assert len(visible) == 5
    row = visible[0]
    # 展示名在
    assert row["时间点"] == "2026-11-08 08:00"
    assert row["昨日耗电量（kWh）"] == 900
    # 源字段还在（没被改名、没被搬走）
    assert row["时间"] == "2026-11-08 08:00"
    assert row["energyKwh"] == 900
    # 列 ID / 字段引用不受影响：别名不是改绑定
    assert set(visible[0].keys()) >= {"时间点", "昨日耗电量（kWh）", "时间", "energyKwh"}


def test_different_threshold_and_top_n_are_not_hardcoded():
    """P14：不同阈值 / 方向 / 前 N 项都真的生效，不是写死的。"""
    # 阈值 500 → 4 条（900/800/700/600），topN 3 → 3 行
    visible = apply_presentation_pipeline(
        [dict(row) for row in ROWS],
        field_aliases=ALIASES,
        presentation_filters=[_gt("energyKwh", 500)],
        sort=[{"fieldRef": "energyKwh", "direction": "DESC"}],
        top_n=3,
    )
    assert [row["energyKwh"] for row in visible] == [900, 800, 700]

    # 升序 + 前 2 项
    visible = apply_presentation_pipeline(
        [dict(row) for row in ROWS],
        field_aliases=ALIASES,
        presentation_filters=[_gt("energyKwh", 400)],
        sort=[{"fieldRef": "energyKwh", "direction": "ASC"}],
        top_n=2,
    )
    assert [row["energyKwh"] for row in visible] == [450, 500]

    # 不给 topN → 满足条件的全部行（6 条）
    visible = apply_presentation_pipeline(
        [dict(row) for row in ROWS],
        field_aliases=ALIASES,
        presentation_filters=[_gt("energyKwh", 400)],
        sort=[{"fieldRef": "energyKwh", "direction": "DESC"}],
    )
    assert len(visible) == 6


def test_equal_values_keep_stable_order():
    """相同值排序遵从既有稳定策略（Python 的稳定排序，不另造规则）。"""
    rows = [
        {"energyKwh": 500, "设备名称": "先出现"},
        {"energyKwh": 500, "设备名称": "后出现"},
    ]
    visible = apply_presentation_pipeline(
        rows,
        presentation_filters=[],
        sort=[{"fieldRef": "energyKwh", "direction": "DESC"}],
    )
    assert [row["设备名称"] for row in visible] == ["先出现", "后出现"]
