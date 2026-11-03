"""R07（第三轮）：显示格式的**入口规则**与**消费口径**专项。

第三轮点名的两个缺口，各对应一块：

1. **消费**（缺口一）：区域级**没有**格式时，**列自己的格式照样生效**
   （列级优先，区域级只作兜底）；底层原始值一个字节不动。
2. **入口**（缺口二）：中文的「把日期明细表的时间列显示格式设为日期」
   经过 ``template_edit_engine.parse``（**真实规则路径**）形成一条真的
   ``SET_FORMAT`` 计划 —— 不是脚本手工改 intent 造出来的。

全程进程内纯函数调用：**0 次模型请求**、不落库、不碰用户库。
"""
from __future__ import annotations

from datetime import datetime

from app.schemas.edit_plan import EditIntent
from app.schemas.report_definition import ReportDefinitionDraft
from app.services.report_presentation_service import (
    ALLOWED_FORMAT_KIND,
    apply_presentation,
    find_region,
)
from app.services.simulated_data_service import _attach_authoritative_format
from app.services.template_edit_engine import template_edit_engine

#: 第三轮验收用的**原中文指令**（与第二轮浏览器现场用的是同一句）。
CHINESE_DATE_INSTRUCTION = "把日期明细表的时间列显示格式设为日期"

RAW_ISO = "2026-11-02T00:00:00+08:00"


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def _table_definition(
    *,
    region_format: dict | None = None,
    time_column_format: dict | None = None,
) -> ReportDefinitionDraft:
    """**两列最小 fixture**：一张日期明细表（时间列 + 耗电列）。

    - ``region_format``：区域级格式（``None`` = 没有）。
    - ``time_column_format``：时间列**自己的**格式（``None`` = 没有）。
    """
    time_column = {
        "id": "col_time",
        "label": "时间",
        "fieldRef": "section_r07.col_time",
        "valueType": "time",
    }
    if time_column_format is not None:
        time_column["format"] = dict(time_column_format)
    region: dict = {
        "id": "region_table_r07",
        "name": "日期明细表",
        "role": "DETAIL",
        "component": {
            "type": "TABLE",
            "columns": [
                time_column,
                {
                    "id": "col_kwh",
                    "label": "耗电",
                    "fieldRef": "section_r07.col_kwh",
                    "valueType": "energy",
                    "semanticKind": "energy",
                    "unit": "kWh",
                    "format": {
                        "kind": "NUMBER",
                        "precision": 1,
                        "unitLabel": "kWh",
                    },
                },
            ],
        },
        "templateBinding": {
            "previewSpec": {
                "columns": [
                    {
                        "id": "col_time",
                        "label": "时间",
                        "fieldRef": "section_r07.col_time",
                        "valueType": "time",
                    },
                    {
                        "id": "col_kwh",
                        "label": "耗电",
                        "fieldRef": "section_r07.col_kwh",
                        "valueType": "energy",
                        "semanticKind": "energy",
                        "unit": "kWh",
                    },
                ]
            }
        },
    }
    if region_format is not None:
        region["format"] = dict(region_format)
    return ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "R07 显示格式", "revision": 1},
            "page": {"arrangement": {"mode": "GRID", "columns": 12}},
            "theme": {"themeRef": "theme_default", "version": "1.0"},
            "regions": [
                region,
                {
                    "id": "region_chart_r07",
                    "name": "能耗趋势",
                    "role": "TREND",
                    "component": {"type": "CHART", "chartType": "line"},
                },
            ],
        }
    )


def _payloads(definition: dict) -> dict:
    """按定义造载荷（纯内存，不落库）。"""
    from app.services.preview_service import (
        build_preview_context,
        build_report_preview_payload,
    )

    preview = build_report_preview_payload(definition, build_preview_context(definition))
    return {item["regionId"]: item["payload"] for item in preview["regions"]}


def _raw_payload(
    *, region_format: dict | None, column_formats: dict[str, dict]
) -> dict:
    """一个**只有表格载荷**的最小载荷（``_attach_authoritative_format`` 的入参）。"""
    return {
        "columns": [
            {"fieldRef": field_ref, "label": field_ref, "format": fmt}
            for field_ref, fmt in column_formats.items()
        ],
        "rows": [
            {
                "section_r07.col_time": RAW_ISO,
                "section_r07.col_kwh": 135.4,
            }
        ],
    }


# --------------------------------------------------------------------------
# 缺口一：消费 —— 区域没有格式时，列自己的格式照样生效
# --------------------------------------------------------------------------


def test_a_column_format_applies_even_when_the_region_has_no_format():
    """区域**无**格式：列 A 按自己的 DATE 渲染；列 B 没有格式 → 显示原值。"""
    payload = _raw_payload(
        region_format=None,
        column_formats={"section_r07.col_time": {"kind": "DATE"}},
    )
    region = {"id": "region_table_r07"}

    attached = _attach_authoritative_format(payload, region)

    # 区域没有格式：载荷里仍然带 format（空对象）—— 结构是封闭的
    assert attached["format"] == {}
    # **有列格式就算显示串**（旧行为在这里整批返回，没有 displayRows）
    assert "displayRows" in attached
    shown = attached["displayRows"][0]
    # 列 A：按 strftime 渲染（默认 %Y-%m-%d）
    assert shown["section_r07.col_time"] == datetime.fromisoformat(
        RAW_ISO
    ).strftime("%Y-%m-%d")
    # 列 B：没有格式 → **显示原值**
    assert shown["section_r07.col_kwh"] == 135.4
    # **底层原始值一个字节都没动**
    assert attached["rows"][0]["section_r07.col_time"] == RAW_ISO
    assert attached["rows"][0]["section_r07.col_kwh"] == 135.4


def test_the_column_format_wins_over_the_region_format():
    """区域有 DATE：列 A 走自己的 DATETIME；列 B 回落到区域级；数值列不被按日期渲染。"""
    payload = _raw_payload(
        region_format={"kind": "DATE"},
        column_formats={
            "section_r07.col_time": {"kind": "DATETIME"},
            "section_r07.col_kwh": {
                "kind": "NUMBER",
                "precision": 1,
                "unitLabel": "kWh",
            },
        },
    )
    region = {"id": "region_table_r07", "format": {"kind": "DATE"}}

    attached = _attach_authoritative_format(payload, region)

    assert attached["format"] == {"kind": "DATE"}
    shown = attached["displayRows"][0]
    # 列级优先：这一列走 DATETIME（不是区域级的 DATE）
    assert shown["section_r07.col_time"] == datetime.fromisoformat(
        RAW_ISO
    ).strftime("%Y-%m-%d %H:%M")
    # 区域级 DATE **没有污染**数值列（列自带 NUMBER）
    assert shown["section_r07.col_kwh"] == "135.4 kWh"
    # 底层原始值不动
    assert attached["rows"][0]["section_r07.col_time"] == RAW_ISO


def test_a_region_format_is_the_fallback_for_a_column_without_its_own_format():
    """列没声明格式 → 回落到区域级格式。"""
    payload = _raw_payload(
        region_format={"kind": "DATE"},
        column_formats={},
    )
    region = {"id": "region_table_r07", "format": {"kind": "DATE"}}

    attached = _attach_authoritative_format(payload, region)

    shown = attached["displayRows"][0]
    # 日期列没有列级格式 → 区域级 DATE 兜底
    assert shown["section_r07.col_time"] == "2026-11-02"
    # 数值列认不出日期 → **原样返回，不猜**（不被按日期渲染）
    assert shown["section_r07.col_kwh"] == "135.4"
    assert attached["rows"][0]["section_r07.col_kwh"] == 135.4


def test_neither_region_nor_columns_have_a_format_produces_no_display_rows():
    """区域与列**都没有**格式：不产出显示串（与改造前一致）。"""
    payload = _raw_payload(region_format=None, column_formats={})
    region = {"id": "region_table_r07"}

    attached = _attach_authoritative_format(payload, region)

    assert attached["format"] == {}
    assert "displayRows" not in attached
    assert "displayValue" not in attached
    # 底层原始值原样
    assert attached["rows"][0]["section_r07.col_time"] == RAW_ISO


def test_the_payload_computes_column_formats_without_a_region_format():
    """真实载荷：区域级为空时，**两列各自**的格式照样算出显示串；原始值不动。"""
    definition = _table_definition(
        region_format=None, time_column_format={"kind": "DATE"}
    )
    before = _payloads(definition.model_dump(mode="json"))
    payloads = _payloads(definition.model_dump(mode="json"))

    table = payloads["region_table_r07"]
    assert table["format"] == {}
    assert "displayRows" in table
    raw_row = table["rows"][0]
    shown_row = table["displayRows"][0]
    # 时间列：按**列自己的** DATE（默认 %Y-%m-%d）
    assert shown_row["section_r07.col_time"] == datetime.fromisoformat(
        raw_row["section_r07.col_time"]
    ).strftime("%Y-%m-%d")
    # 耗电列：按**列自己的** NUMBER（精度 1 + 单位标签）
    assert shown_row["section_r07.col_kwh"] == f"{raw_row['section_r07.col_kwh']:.1f} kWh"
    # **底层原始值一个字节都没动**（模拟值跟着当天走，所以只比「前后一致」）
    assert table["rows"] == before["region_table_r07"]["rows"]
    assert isinstance(raw_row["section_r07.col_kwh"], (int, float))


def test_a_metric_card_without_a_region_format_keeps_no_display_value():
    """指标卡没有列的概念：区域没格式 → 不出显示串（行为不变）。"""
    payload = {"value": 5499.6, "unit": "kWh"}
    region = {"id": "region_card"}

    attached = _attach_authoritative_format(payload, region)

    assert attached["format"] == {}
    assert "displayValue" not in attached
    assert attached["value"] == 5499.6


# --------------------------------------------------------------------------
# 缺口二：入口规则 —— 原中文指令形成真的 SET_FORMAT 计划
# --------------------------------------------------------------------------


def test_the_chinese_date_instruction_produces_a_set_format_plan():
    """原中文指令 → APPLY_PRESENTATION + **一条** SET_FORMAT（kind=DATE）。"""
    definition = _table_definition(region_format=None)

    plan = template_edit_engine.parse(
        message=CHINESE_DATE_INSTRUCTION,
        definition=definition,
        selected_region_id=None,
    )

    assert plan.intent == EditIntent.APPLY_PRESENTATION
    assert plan.needsClarification is False
    operations = list(plan.operation.presentationOperations or [])
    assert len(operations) == 1, operations
    assert operations[0]["op"] == "SET_FORMAT"
    # 目标卡片 = 消息里点名的那张（「日期明细表」）
    assert operations[0]["params"]["regionId"] == "region_table_r07"
    # 只写 kind：**不编造** dateTimePattern 之类的键（日期走 strftime）
    assert operations[0]["params"]["format"] == {"kind": "DATE"}
    assert operations[0]["params"]["format"]["kind"] in ALLOWED_FORMAT_KIND
    assert plan.target.regionId == "region_table_r07"


def test_the_set_format_plan_is_applied_and_the_date_is_rendered():
    """这条计划**真的落库**：区域级格式 = {"kind": "DATE"}；显示串按 strftime；原值不动。"""
    definition = _table_definition(region_format=None)
    plan = template_edit_engine.parse(
        message=CHINESE_DATE_INSTRUCTION,
        definition=definition,
        selected_region_id=None,
    )

    result = apply_presentation(
        definition, list(plan.operation.presentationOperations or [])
    )

    assert result.ok is True, result.failure
    region = find_region(result.definition, "region_table_r07")
    assert region["format"] == {"kind": "DATE"}
    # 显示串按 strftime 渲染；**底层原始值不动**
    after = _payloads(result.definition.model_dump(mode="json"))
    table = after["region_table_r07"]
    raw_row = table["rows"][0]
    shown_row = table["displayRows"][0]
    assert shown_row["section_r07.col_time"] == datetime.fromisoformat(
        raw_row["section_r07.col_time"]
    ).strftime("%Y-%m-%d")
    assert raw_row["section_r07.col_time"] != shown_row["section_r07.col_time"]


def test_the_other_display_format_words_map_to_whitelisted_kinds():
    """数值 / 百分比 / 金额 / 时长 / 时间 → 白名单内的 kind。"""
    definition = _table_definition(region_format=None)
    expected = {
        "把日期明细表的时间列显示格式设为数值": "NUMBER",
        "把日期明细表的时间列显示格式设为数字": "NUMBER",
        "把日期明细表的时间列显示格式设为百分比": "PERCENT",
        "把日期明细表的时间列显示格式设为金额": "CURRENCY",
        "把日期明细表的时间列显示格式设为时长": "DURATION",
        "把日期明细表的时间列显示格式设为时间": "DATETIME",
        "把日期明细表的时间列显示格式设为日期格式": "DATE",
        "日期明细表的时间列显示成日期": "DATE",
    }

    for message, kind in expected.items():
        plan = template_edit_engine.parse(
            message=message,
            definition=definition,
            selected_region_id=None,
        )
        operations = list(plan.operation.presentationOperations or [])
        assert plan.intent == EditIntent.APPLY_PRESENTATION, message
        assert len(operations) == 1, (message, operations)
        assert operations[0]["op"] == "SET_FORMAT", message
        assert operations[0]["params"]["format"] == {"kind": kind}, message
        assert kind in ALLOWED_FORMAT_KIND


def test_a_format_request_without_a_target_is_not_guessed():
    """没有目标卡片 → **不猜**：宁可 UNKNOWN，也不要高置信度的空操作。"""
    definition = _table_definition(region_format=None)

    plan = template_edit_engine.parse(
        message="把这张表的时间列格式设为日期",
        definition=definition,
        selected_region_id=None,
    )

    assert plan.intent != EditIntent.CHANGE_VISUALIZATION
    operations = list(plan.operation.presentationOperations or [])
    # 要么不产出操作，要么产出的操作**不是空的**
    assert not (plan.intent == EditIntent.APPLY_PRESENTATION and not operations)

    # 选中了卡片时（真实入口的常态）→ 走那张卡
    selected = template_edit_engine.parse(
        message="把这张表的时间列格式设为日期",
        definition=definition,
        selected_region_id="region_table_r07",
    )
    selected_ops = list(selected.operation.presentationOperations or [])
    assert selected.intent == EditIntent.APPLY_PRESENTATION
    assert len(selected_ops) == 1
    assert selected_ops[0]["params"]["regionId"] == "region_table_r07"


def test_the_existing_presentation_rules_do_not_regress():
    """隐藏 / 复制 / 组成一组 / 折线图：四条既有规则行为不变。"""
    definition = _table_definition(region_format=None)

    hidden = template_edit_engine.parse(
        message="隐藏日期明细表", definition=definition, selected_region_id=None
    )
    assert hidden.intent == EditIntent.APPLY_PRESENTATION
    assert hidden.operation.presentationOperations[0]["op"] == "HIDE_REGION"

    copied = template_edit_engine.parse(
        message="复制一张日期明细表", definition=definition, selected_region_id=None
    )
    assert copied.intent == EditIntent.APPLY_PRESENTATION
    assert copied.operation.presentationOperations[0]["op"] == "COPY_REGION"

    grouped = template_edit_engine.parse(
        message="把前两张卡片组成一组并排显示，间距 16",
        definition=definition,
        selected_region_id=None,
    )
    assert grouped.intent == EditIntent.APPLY_PRESENTATION
    assert grouped.operation.presentationOperations[0]["op"] == "CREATE_GROUP"

    charted = template_edit_engine.parse(
        message="把能耗趋势改成折线图", definition=definition, selected_region_id=None
    )
    assert charted.intent == EditIntent.CHANGE_VISUALIZATION
    assert charted.operation.chartKind == "LINE"


def test_a_chart_request_is_not_mistaken_for_a_format_request():
    """「格式改成折线图」这种话**不是**显示格式请求：仍走 CHANGE_VISUALIZATION。"""
    definition = _table_definition(region_format=None)

    plan = template_edit_engine.parse(
        message="把日期明细表的格式改成折线图",
        definition=definition,
        selected_region_id=None,
    )

    assert plan.intent == EditIntent.CHANGE_VISUALIZATION
    assert not plan.operation.presentationOperations
