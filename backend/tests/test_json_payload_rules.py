"""用户 JSON 载荷层：指标卡的值、图表的轴与单位（code-review R2 🟠-1 / 🟡-3 / 🟡-4）。

这三条的共同点是同一句话：**宁可少说，不许说假话**。

- **🟠-1 指标卡的值**：值只来自**实际生效的处理结果**。有生效的汇总规则 →
  值就是那条规则算出来的结果；没有生效的汇总口径 → 单行输入直接显示那一行
  的值，多行输入**不编合计值**（不拿「第一行」冒充这个指标的值）；
  分组 / 占比的结果是多个值，没有单一合计 → 值留空并说明；
  规则**被跳过**（温度不默认求和）时，说明写**实话**，不写「按求和展示」。
- **🟡-3 图表的轴**：只按方案声明选（区域的 ``encodings``，或区域声明的列
  里带语义描述的那一列），**不按列名猜、不按顺序随便挑一列**。
- **🟡-4 图表的单位**：只认方案声明的 ``format.unitLabel``，没有就是空串
  —— 不猜 kWh。

本文件只做**单元层**断言（直接调 ``build_user_json_payloads``），
不新增端点、不新增测试框架。
"""
from __future__ import annotations

from typing import Any

from app.services.effective_combination import normalize_combination
from app.services.json_payload_service import build_user_json_payloads


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def _combination(
    mapping: list[dict[str, Any]],
    rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """一份生效组合（固定 9 键）。"""
    return normalize_combination(
        {
            "sourceMode": "user_json",
            "inputId": "inp_test",
            "inputVersion": 1,
            "fieldMapping": mapping,
            "processingRules": rules or [],
        }
    )


def _metric_card(column_id: str = "energy", label: str = "总耗电") -> dict[str, Any]:
    """一个指标卡区域。"""
    return {
        "regions": [
            {
                "id": "region_1",
                "role": "SUMMARY",
                "name": label,
                "component": {
                    "type": "METRIC_CARD",
                    "metric": {
                        "id": column_id,
                        "label": label,
                        "fieldRef": column_id,
                        "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
                    },
                },
            }
        ]
    }


def _chart(
    *,
    columns: list[dict[str, Any]] | None = None,
    encodings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """一个图表区域。"""
    component: dict[str, Any] = {"type": "CHART", "chartKind": "LINE"}
    if columns is not None:
        component["columns"] = columns
    if encodings is not None:
        component["encodings"] = encodings
    return {
        "regions": [
            {
                "id": "region_1",
                "role": "TREND",
                "name": "趋势",
                "component": component,
            }
        ]
    }


#: 三行数据：合计 60，第一行 10。
_ROWS: list[dict[str, Any]] = [
    {"ts": "2024-01-01", "device": "A", "energy": 10.0},
    {"ts": "2024-01-02", "device": "B", "energy": 20.0},
    {"ts": "2024-01-03", "device": "C", "energy": 30.0},
]


def _mapping(column_id: str = "energy", field_key: str = "energy") -> list[dict[str, Any]]:
    return [
        {"regionId": "region_1", "columnId": column_id, "inputFieldKey": field_key}
    ]


def _rule(
    operation: str,
    *,
    column_id: str = "energy",
    target: str = "energy",
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ruleId": f"rule_region_1_{column_id}_{operation}".lower(),
        "operation": operation,
        "targetFieldKey": target,
        "params": params or {},
    }


# --------------------------------------------------------------------------
# 🟠-1 —— 指标卡的值只来自实际生效的处理结果
# --------------------------------------------------------------------------


def test_metric_card_value_is_the_sum_rule_result_not_the_first_row() -> None:
    """有生效的求和规则 → 值是**规则算出来的结果**（60），不是第一行（10）。"""
    payloads = build_user_json_payloads(
        definition=_metric_card(),
        rows=_ROWS,
        combination=_combination(_mapping(), [_rule("SUM")]),
    )
    card = payloads["region_1"]
    assert card["value"] == 60.0
    # 卡片与指标清单里的值是同一个数。
    assert card["metrics"][0]["value"] == 60.0
    assert any("按求和展示" in note for note in card["notes"])


def test_metric_card_without_summary_rule_does_not_show_the_first_row() -> None:
    """没有汇总口径 + 多行输入 → **不编合计值**，也不拿第一行冒充。

    规格第 25 条：「默认直接展示输入值……依据不清时用明细表」。
    """
    payloads = build_user_json_payloads(
        definition=_metric_card(),
        rows=_ROWS,
        combination=_combination(_mapping()),
    )
    card = payloads["region_1"]
    # 值留空（前端渲染成「—」），**不是**第一行的 10。
    assert card["value"] is None
    assert card["metrics"][0]["value"] is None
    assert 10.0 not in card["notes"]
    notes = " ".join(card["notes"])
    assert "没有为这个指标声明汇总口径" in notes
    assert "3 行" in notes
    assert "没有编合计值" in notes
    assert "明细表" in notes


def test_metric_card_shows_the_only_input_value_when_there_is_one_row() -> None:
    """单行输入 → 直接显示那一行的值，并说清是「按输入值直接展示」。"""
    payloads = build_user_json_payloads(
        definition=_metric_card(),
        rows=[{"ts": "2024-01-01", "device": "A", "energy": 42.0}],
        combination=_combination(_mapping()),
    )
    card = payloads["region_1"]
    assert card["value"] == 42.0
    notes = " ".join(card["notes"])
    assert "按输入值直接展示" in notes
    assert "只有 1 行" in notes


def test_metric_card_ignores_a_rule_that_targets_another_field() -> None:
    """规则打在**别的字段**上 → 不算这个指标的口径，不拿那个合计冒充。"""
    payloads = build_user_json_payloads(
        definition=_metric_card(),
        rows=_ROWS,
        combination=_combination(
            _mapping(), [_rule("SUM", column_id="power", target="power")]
        ),
    )
    card = payloads["region_1"]
    assert card["value"] is None
    notes = " ".join(card["notes"])
    assert "没有为这个指标声明汇总口径" in notes
    assert "按求和展示" not in notes


def test_metric_card_explains_truthfully_when_the_sum_rule_was_skipped() -> None:
    """不可求和字段上的求和规则**没有执行** → 说明写实话，不写「按求和展示」。"""
    rows = [
        {"ts": "2024-01-01", "温度": 20.0},
        {"ts": "2024-01-02", "温度": 22.0},
    ]
    payloads = build_user_json_payloads(
        definition=_metric_card(column_id="温度", label="温度"),
        rows=rows,
        combination=_combination(
            _mapping(column_id="温度", field_key="温度"),
            [_rule("SUM", column_id="温度", target="温度")],
        ),
    )
    card = payloads["region_1"]
    # 规则没执行 → 没有合计值可显示，值留空（不是第一行的 20）。
    assert card["value"] is None
    notes = " ".join(card["notes"])
    assert "求和没有业务意义" in notes
    assert "按求和展示" not in notes
    assert "没有编合计值" in notes


def test_metric_card_with_group_rule_shows_none_of_the_grouped_values() -> None:
    """指标卡 + 分组规则 → **不显示分组结果里的任何一个数**。

    分组的结果是每组一个值，不是一个单独的数值；指标卡只显示一个数，
    拿第一组冒充就是编口径。
    """
    payloads = build_user_json_payloads(
        definition=_metric_card(),
        rows=_ROWS,
        combination=_combination(
            _mapping(),
            [
                _rule(
                    "GROUP_BY",
                    params={"groupBy": "device", "valueField": "energy"},
                )
            ],
        ),
    )
    card = payloads["region_1"]
    assert card["value"] is None
    assert card["metrics"][0]["value"] is None
    # 任何一个分组的数值都不许出现在这个指标上。
    assert card["value"] not in (10.0, 20.0, 30.0)
    notes = " ".join(card["notes"])
    assert "这不是一个单独的数值" in notes
    assert "明细表" in notes or "图表" in notes


def test_metric_card_on_a_text_column_leaves_the_value_empty_not_the_string_null() -> None:
    """求和打在**全是文字**的列上 → 值留空，**不是**字符串 ``"null"``。

    指标卡不是表格：表格单元格用 ``"null"`` 表示「这一格是 null」，
    指标卡只有一个值 —— **没有值就是没有值** → ``None``（前端渲染成「—」）。
    """
    rows = [
        {"energy": 10.0, "label": "甲"},
        {"energy": 20.0, "label": "乙"},
    ]
    payloads = build_user_json_payloads(
        definition=_metric_card(column_id="label", label="标签"),
        rows=rows,
        combination=_combination(
            _mapping(column_id="label", field_key="label"),
            [_rule("SUM", column_id="label", target="label")],
        ),
    )
    card = payloads["region_1"]
    assert card["value"] is None
    assert card["metrics"][0]["value"] is None
    # 说明要说清为什么没有数。
    notes = " ".join(card["notes"])
    assert "没有可求和的数值" in notes
    assert "没有编合计值" in notes
    # 没求出来就不许写「按求和展示（……用户已确认）」。
    assert "按求和展示" not in notes
    assert "用户已确认" not in notes


def test_metric_card_single_row_null_value_is_empty_not_the_string_null() -> None:
    """单行输入的值本身是 null → 指标卡的值留空（``None``），不是字符串 ``"null"``。"""
    payloads = build_user_json_payloads(
        definition=_metric_card(),
        rows=[{"ts": "2024-01-01", "device": "A", "energy": None}],
        combination=_combination(_mapping()),
    )
    card = payloads["region_1"]
    assert card["value"] is None
    assert card["metrics"][0]["value"] is None
    notes = " ".join(card["notes"])
    assert "没有值可显示" in notes
    assert "按输入值直接展示" not in notes


def test_metric_card_note_follows_the_actual_rule_not_always_ratio() -> None:
    """说明文字按**实际规则**说：求和口径不许被写成「占比」。

    形状：一条求和规则，但处理结果的形状认不出「这个指标的单一数值」
    （规则目标与指标映射的字段不是同一个）→ 落到「不是一个单独的数值」
    那一支。那一支的措辞必须按 operation 说，不能一律说成「占比」。
    """
    rows = [dict(row, power=1.0) for row in _ROWS]
    payloads = build_user_json_payloads(
        definition=_metric_card(),
        rows=rows,
        combination=_combination(
            _mapping(),
            [
                _rule(
                    "SUM",
                    column_id="power",
                    target="power",
                    params={"valueField": "energy"},
                )
            ],
        ),
    )
    card = payloads["region_1"]
    assert card["value"] is None
    notes = " ".join(card["notes"])
    assert "算不出一个单独的数值" in notes
    # 求和口径不许被说成占比，也不许说成「用户已确认」。
    assert "占比" not in notes
    assert "用户已确认" not in notes


# --------------------------------------------------------------------------
# 🟡-3 —— 图表的轴只按方案声明选取
# --------------------------------------------------------------------------


def test_chart_axes_come_from_the_declared_encodings() -> None:
    """encodings 声明的 X / Y 通道 → 轴照声明取。"""
    payloads = build_user_json_payloads(
        definition=_chart(
            encodings=[
                {"channel": "X", "fieldRef": "ts"},
                {"channel": "Y", "fieldRef": "energy"},
            ]
        ),
        rows=_ROWS,
        combination=_combination(
            [
                {"regionId": "region_1", "columnId": "ts", "inputFieldKey": "ts"},
                {"regionId": "region_1", "columnId": "energy", "inputFieldKey": "energy"},
            ]
        ),
    )
    chart = payloads["region_1"]
    assert chart["xField"] == "ts"
    assert chart["yField"] == "energy"
    assert len(chart["points"]) == 3


def test_chart_axes_fall_back_to_the_declared_columns_semantics() -> None:
    """encodings 没声明 → 用**区域声明的列**里带语义描述的那一列定轴。"""
    columns = [
        {"id": "ts", "label": "日期", "fieldRef": "ts", "valueType": "time"},
        {"id": "energy", "label": "耗电", "fieldRef": "energy", "valueType": "energy"},
    ]
    payloads = build_user_json_payloads(
        definition=_chart(columns=columns, encodings=[]),
        rows=_ROWS,
        combination=_combination(
            [
                {"regionId": "region_1", "columnId": "ts", "inputFieldKey": "ts"},
                {"regionId": "region_1", "columnId": "energy", "inputFieldKey": "energy"},
            ]
        ),
    )
    chart = payloads["region_1"]
    assert chart["xField"] == "ts"
    assert chart["yField"] == "energy"
    assert len(chart["points"]) == 3


def test_chart_axes_are_not_guessed_from_column_names_or_mapping_order() -> None:
    """列 id 含 date、映射第一列是字符串列 → **不猜**，诚实降级。

    旧实现：横轴按「列 id 含 date」猜，纵轴按「第一个不是横轴的列」猜 ——
    于是纵轴拿到字符串列 ``device``，有数据也画不出点。
    """
    rows = [
        {"device": "A", "date": "2024-01-01", "energy": 10.0},
        {"device": "B", "date": "2024-01-02", "energy": 20.0},
    ]
    payloads = build_user_json_payloads(
        definition=_chart(encodings=[]),
        rows=rows,
        combination=_combination(
            [
                {"regionId": "region_1", "columnId": "device", "inputFieldKey": "device"},
                {"regionId": "region_1", "columnId": "date", "inputFieldKey": "date"},
                {"regionId": "region_1", "columnId": "energy", "inputFieldKey": "energy"},
            ]
        ),
    )
    chart = payloads["region_1"]
    # 认不出轴 → 留空并降级，不拿别的列凑数。
    assert chart["xField"] == ""
    assert chart["yField"] == ""
    assert chart["points"] == []
    assert chart["yValues"] == []
    notes = " ".join(chart["notes"])
    assert "没有声明可用的横轴或纵轴字段" in notes
    assert "没有编趋势" in notes


# --------------------------------------------------------------------------
# 🟡-4 —— 图表的单位只认方案声明
# --------------------------------------------------------------------------


def test_chart_unit_is_the_declared_unit_label() -> None:
    """方案声明了 unitLabel → 图表带上这个单位。"""
    columns = [
        {"id": "ts", "label": "日期", "fieldRef": "ts", "valueType": "time"},
        {
            "id": "energy",
            "label": "耗电",
            "fieldRef": "energy",
            "valueType": "energy",
            "format": {"unitLabel": "kWh"},
        },
    ]
    payloads = build_user_json_payloads(
        definition=_chart(columns=columns, encodings=[]),
        rows=_ROWS,
        combination=_combination(
            [
                {"regionId": "region_1", "columnId": "ts", "inputFieldKey": "ts"},
                {"regionId": "region_1", "columnId": "energy", "inputFieldKey": "energy"},
            ]
        ),
    )
    assert payloads["region_1"]["unit"] == "kWh"


def test_chart_unit_is_empty_when_the_plan_declares_none() -> None:
    """方案没声明单位 → 空串，**不猜 kWh**。"""
    columns = [
        {"id": "ts", "label": "日期", "fieldRef": "ts", "valueType": "time"},
        {"id": "energy", "label": "耗电", "fieldRef": "energy", "valueType": "energy"},
    ]
    payloads = build_user_json_payloads(
        definition=_chart(columns=columns, encodings=[]),
        rows=_ROWS,
        combination=_combination(
            [
                {"regionId": "region_1", "columnId": "ts", "inputFieldKey": "ts"},
                {"regionId": "region_1", "columnId": "energy", "inputFieldKey": "energy"},
            ]
        ),
    )
    assert payloads["region_1"]["unit"] == ""
