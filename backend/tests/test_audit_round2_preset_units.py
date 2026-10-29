"""第二轮审查：系统补齐的数值列自带单位 + 提示词不再教旧口径。

对应两处缺陷：

- **单位缺失**（规格《契约缺口补充》A2）：``reportPreviewPayload.regions[].payload``
  必须携带单位信息，表格列的 ``format.unitLabel`` 同理。系统补齐的「负载率」
  「耗电量」是数值列，之前补出来的列**永远没有单位**（``ColumnSpec(label=label)``），
  表头读不出量纲。非数值列（设备名称、告警级别……）**不能**被硬塞单位 ——
  否则表头出现莫名其妙的括号。
- **旧口径残留**（US15 /《回复口径》）：提示词还在教模型在 ``assumptions`` 里写
  「数据待连接」，那句话会**原样出现在左侧对话区的方案卡里**。改成仓库里已经在用的
  「能力待接通」「口径待核实」。

测试只替换外部模型调用，其余（方案领域层 → 模板 → 草稿定义 → 造数器 → 载荷）
全是真实实现。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.preview_service import build_report_preview_payload
from app.services.report_plan_service import (
    apply_candidate,
    resolve_table_columns,
)
from app.services.report_template_service import build_template_design
from app.services.template_definition_service import build_template_definition


#: 方案提示词：提示词本身是交付物，措辞是被验收的契约。
_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "agents" / "prompts" / "report_plan.md"
)


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def _candidate(
    title: str,
    *,
    intents: list[str],
    metrics: list[str] | None = None,
) -> dict[str, Any]:
    """一份**模型不声明栏目**的候选 → 系统补齐这条路径。"""
    return {
        "topic": title,
        "requirements": {
            "metrics": metrics or intents,
            "subject": "A 机房",
            "timeRange": "最近 7 天",
            "exclusions": [],
        },
        "sections": [
            {
                "title": title,
                "metricIntents": intents,
                "componentType": "table",
            }
        ],
        "assumptions": [],
        "suggestions": [],
        "referenceTemplates": [],
    }


def _preview_context() -> dict[str, Any]:
    return {
        "definitionId": "def_round2_1",
        "definitionVersion": 1,
        "timezone": "Asia/Shanghai",
        "anchorTime": "2026-10-29T00:00:00+08:00",
        "windowStart": "2026-10-28T00:00:00+08:00",
        "windowEnd": "2026-10-29T00:00:00+08:00",
        "grainUnit": "HOUR",
        "generatorVersion": "simulated-data/1",
    }


def _payload_for(candidate: dict[str, Any]) -> dict[str, Any]:
    """候选 → 方案 → 模板 → 草稿定义 → 报表示意载荷（真实全链路）。"""
    plan = apply_candidate(candidate, plan_id="plan_round2")
    payload = plan.model_dump(mode="json")
    template = build_template_design(payload, subject_id="room_a")
    draft = build_template_definition(
        template.model_dump(mode="json"), payload, subject_id="room_a"
    )
    return build_report_preview_payload(
        draft.model_dump(mode="json"), _preview_context()
    )


def _table_region(preview: dict[str, Any]) -> dict[str, Any]:
    """载荷里的表格区域（这份候选只有一个表格板块）。"""
    regions = [
        region
        for region in preview["regions"]
        if region["componentType"] == "TABLE"
    ]
    assert regions, preview["regions"]
    return regions[0]


def _column_by_label(payload: dict[str, Any], label: str) -> dict[str, Any]:
    for column in payload.get("columns") or []:
        if column["label"] == label:
            return column
    raise AssertionError(f"载荷里没有「{label}」这一列：{payload.get('columns')}")


# --------------------------------------------------------------------------
# 1 & 2：系统补齐的数值列自带单位
# --------------------------------------------------------------------------


def test_system_filled_cabinet_table_load_ratio_carries_percent_unit() -> None:
    """「机柜运行状态」补出来的「负载率」带 ``unitLabel == "%"`` 且是数值型。"""
    resolution = resolve_table_columns(
        {"title": "机柜运行状态", "metricIntents": ["机柜状态"]}
    )

    assert resolution.kind == "inferred"
    load_ratio = next(spec for spec in resolution.specs if spec.label == "负载率")
    assert load_ratio.format is not None
    assert load_ratio.format["unitLabel"] == "%"
    assert load_ratio.format["kind"] == "NUMBER"


def test_system_filled_energy_table_consumption_carries_kwh_unit() -> None:
    """「能耗」表补出来的「耗电量」带 ``unitLabel == "kWh"``。"""
    resolution = resolve_table_columns(
        {"title": "设备能耗明细", "metricIntents": ["设备耗电"]}
    )

    assert resolution.kind == "inferred"
    consumption = next(spec for spec in resolution.specs if spec.label == "耗电量")
    assert consumption.format is not None
    assert consumption.format["unitLabel"] == "kWh"
    assert consumption.format["kind"] == "NUMBER"


def test_the_preset_unit_reaches_the_committed_plan_column() -> None:
    """补齐的单位一路落到方案的定稿栏目上（表头才有单位）。"""
    plan = apply_candidate(
        _candidate("机柜运行状态", intents=["机柜状态"]), plan_id="plan_round2"
    )

    columns = {column.label: column for column in plan.sections[0].columns}
    assert columns["负载率"].format is not None
    assert columns["负载率"].format.unitLabel == "%"
    assert columns["负载率"].format.kind == "NUMBER"


# --------------------------------------------------------------------------
# 3：非数值列不带单位
# --------------------------------------------------------------------------


def test_system_filled_non_numeric_columns_carry_no_unit_label() -> None:
    """名称、状态、时间、级别这类离散列**没有**单位标注。

    给它们硬塞单位，表头就会多出莫名其妙的括号。
    """
    cases = {
        "当前告警列表": (["告警级别"], ["设备名称", "时间", "告警级别"]),
        "机柜运行状态": (["机柜状态"], ["机柜名称", "状态"]),
        "设备能耗明细": (["设备耗电"], ["设备名称"]),
    }
    for title, (intents, non_numeric) in cases.items():
        resolution = resolve_table_columns(
            {"title": title, "metricIntents": intents}
        )
        assert resolution.kind == "inferred", title
        for spec in resolution.specs:
            if spec.label not in non_numeric:
                continue
            assert spec.format is None, (title, spec.label, spec.format)
            assert not (spec.format or {}).get("unitLabel"), (title, spec.label)


def test_non_numeric_columns_have_no_unit_in_the_payload() -> None:
    """载荷里的非数值列同样不带 ``unitLabel``（不是只在这一层过滤）。"""
    preview = _payload_for(_candidate("当前告警列表", intents=["告警级别"]))

    payload = _table_region(preview)["payload"]
    for label in ("设备名称", "时间", "告警级别"):
        column = _column_by_label(payload, label)
        assert not (column.get("format") or {}).get("unitLabel"), column


# --------------------------------------------------------------------------
# 4：补齐说明文案不变
# --------------------------------------------------------------------------


def test_the_inferred_note_text_is_unchanged() -> None:
    """补齐说明的**用户可见文字**一个字都不许变。

    预设的形状改了（列名 + 单位），拼出来的说明必须还是原来那句。
    """
    resolution = resolve_table_columns(
        {"title": "机柜运行状态", "metricIntents": ["机柜状态"]}
    )
    assert resolution.note == "系统为「机柜运行状态」补齐了栏目：机柜名称、状态、负载率。"

    energy = resolve_table_columns(
        {"title": "设备能耗明细", "metricIntents": ["设备耗电"]}
    )
    assert energy.note == "系统为「设备能耗明细」补齐了栏目：设备名称、耗电量。"


def test_the_inferred_note_text_is_unchanged_in_the_plan() -> None:
    """同一条说明进方案假设时也还是那句话。"""
    plan = apply_candidate(
        _candidate("机柜运行状态", intents=["机柜状态"]), plan_id="plan_round2"
    )

    assert any(
        "系统为「机柜运行状态」补齐了栏目：机柜名称、状态、负载率。" in assumption
        for assumption in plan.assumptions
    ), plan.assumptions


# --------------------------------------------------------------------------
# 实测：补齐的列在载荷里是合理的数值，不是占位串
# --------------------------------------------------------------------------


def test_the_filled_load_ratio_is_a_real_number_in_the_payload() -> None:
    """实测：负载率在载荷里是带一位小数的百分比数值，不是「示例值 69」。"""
    preview = _payload_for(_candidate("机柜运行状态", intents=["机柜状态"]))

    payload = _table_region(preview)["payload"]
    column = _column_by_label(payload, "负载率")
    assert column["format"]["unitLabel"] == "%"
    assert column["format"]["kind"] == "NUMBER"

    rows = payload["rows"]
    assert rows, "表格区域应当出行"
    for row in rows:
        value = row[column["fieldRef"]]
        assert isinstance(value, (int, float)), (row, value)
        assert not str(value).startswith("示例值"), row
        assert 0.0 <= float(value) <= 100.0, row


def test_the_filled_consumption_is_a_real_number_in_the_payload() -> None:
    """实测：耗电量在载荷里是 kWh 数值，不是占位串。"""
    preview = _payload_for(_candidate("设备能耗明细", intents=["设备耗电"]))

    payload = _table_region(preview)["payload"]
    column = _column_by_label(payload, "耗电量")
    assert column["format"]["unitLabel"] == "kWh"

    rows = payload["rows"]
    assert rows, "表格区域应当出行"
    for row in rows:
        value = row[column["fieldRef"]]
        assert isinstance(value, (int, float)), (row, value)
        assert not str(value).startswith("示例值"), row
        assert float(value) > 0.0, row


# --------------------------------------------------------------------------
# 5：提示词不再教模型写旧口径
# --------------------------------------------------------------------------


def test_the_prompt_no_longer_teaches_the_old_wording() -> None:
    """提示词里不再出现「数据待连接」。

    那句话会**原样出现在左侧对话区的方案卡里** —— 旧口径等于回到界面上。
    """
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")

    assert "数据待连接" not in prompt


def test_the_prompt_uses_the_existing_wording() -> None:
    """改用仓库里已经在说的「能力待接通」「口径待核实」，不新造第三种说法。"""
    prompt = _PROMPT_PATH.read_text(encoding="utf-8")

    assert "能力待接通" in prompt
    assert "口径待核实" in prompt
    # 教模型按「指标名 + 原因」组织，而不是背一句固定的话。
    assert "指标名" in prompt
