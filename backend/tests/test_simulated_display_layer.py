"""载荷层：目录外指标的**虚拟展示**（合并 / 占位模式 / 通用量纲推断）。

覆盖五件事（都是「载荷层」的判据，不碰模型、不碰真实数据）：

1. **合并**：``previewSpec.columns`` 与 ``component.columns`` 合并 ——
   目录外新增的列只写在组件列上时也要渲染出来（新增是追加，不是替换）；
   同一 ``fieldRef`` 两边都有时，身份信息以组件列为准、语义描述沿用
   ``previewSpec``；
2. **目录外的列出数值**：目录里没有的 ``fieldRef`` 照样有示意值，
   不是报错、不是跳过、不是空列；
3. **占位模式**（``displayMode = "placeholder_only"``）：只显示名称，
   不显示数值（表格不写这一格、指标卡不出大数字与趋势、图表不进 series）；
4. **通用量纲推断**：任意新名字都能推出单位与值形态，**推不出来也出数**
   （通用数值 + 空单位），没有任何业务词特判；
5. **构建定义这一层不做真实字段准入**：绑不上的指标照样产出合法定义
   （模拟绑定），不丢弃、不报错。
"""
from __future__ import annotations

from typing import Any

from app.services.preview_service import build_preview_context, build_report_preview_payload
from app.services.report_template_service import build_template_design
from app.services.simulated_field_semantics import (
    infer_shape_from_name,
    resolve_metric_semantics,
)
from app.services.template_definition_service import build_template_definition


def _plan(section: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "rplan_display", "revision": 1, "topic": "A 机房能耗日报",
        "requirements": {
            "metrics": ["总耗电"], "subject": "A 机房",
            "timeRange": "最近 7 天", "exclusions": [],
        },
        "sections": [section],
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }


def _definition(section: dict[str, Any]) -> dict[str, Any]:
    plan = _plan(section)
    template = build_template_design(plan, subject_id="room_a")
    return build_template_definition(
        template.model_dump(mode="json"), plan, "room_a"
    ).model_dump(mode="json")


def _payload(definition: dict[str, Any], region_id: str = "region_1") -> dict[str, Any]:
    preview = build_report_preview_payload(
        definition, build_preview_context(definition)
    )
    return next(
        item["payload"] for item in preview["regions"]
        if item["regionId"] == region_id
    )


def _add_column(definition: dict[str, Any], column: dict[str, Any]) -> None:
    """模拟编辑层追加一列：组件列与 previewSpec 列**两处都有**。"""
    region = definition["regions"][0]
    region["component"].setdefault("columns", []).append(dict(column))
    region["templateBinding"]["previewSpec"].setdefault("columns", []).append(dict(column))


# --------------------------------------------------------------------------
# 1 + 2. 合并：目录外新增的列照样渲染出数值
# --------------------------------------------------------------------------


def test_an_out_of_catalog_column_renders_a_number():
    """目录里没有的列**出数值**，不是占位文字、不是空列。"""
    definition = _definition({
        "id": "section_1", "title": "冰箱明细", "metricIntents": ["冰箱明细"],
        "componentType": "table",
        "columns": [
            {"id": "col_name", "label": "名称"},
            {"id": "col_value", "label": "冰箱明细"},
        ],
    })
    payload = _payload(definition)
    value = payload["rows"][0]["section_1_col_value"]

    assert isinstance(value, (int, float)), value


def test_a_column_written_only_into_the_component_is_still_rendered():
    """组件列独有的列要渲染出来（新增是**追加**，不是替换）。"""
    definition = _definition({
        "id": "section_1", "title": "总耗电", "metricIntents": ["总耗电"],
        "componentType": "table",
        "columns": [
            {"id": "col_name", "label": "设备名称"},
            {"id": "col_value", "label": "总耗电"},
        ],
    })
    _add_column(definition, {
        "fieldRef": "simulated.section_1.2", "label": "冰箱明细",
        "format": {"unitLabel": "kWh"},
        "displayMode": "simulated", "bindingKind": "simulated",
    })
    payload = _payload(definition)

    labels = [item["label"] for item in payload["columns"]]
    assert "冰箱明细" in labels, labels
    assert isinstance(
        payload["rows"][0]["simulated.section_1.2"], (int, float)
    )


def test_the_component_column_wins_on_identity_and_the_spec_on_semantics():
    """同一 fieldRef：身份信息以组件列为准，语义描述沿用 previewSpec。"""
    definition = _definition({
        "id": "section_1", "title": "总耗电", "metricIntents": ["总耗电"],
        "componentType": "table",
        "columns": [{"id": "col_value", "label": "总耗电"}],
    })
    region = definition["regions"][0]
    region["component"]["columns"] = [{
        "id": "col_value", "label": "总耗电（改名）", "fieldRef": "section_1_col_value",
        "format": {"kind": "NUMBER", "precision": 2, "unitLabel": "kWh"},
    }]
    payload = _payload(definition)
    column = payload["columns"][0]

    assert column["label"] == "总耗电（改名）"
    assert column["format"]["precision"] == 2
    assert column["valueType"] == "energy"


# --------------------------------------------------------------------------
# 3. 占位模式：只显示名称
# --------------------------------------------------------------------------


def test_a_placeholder_column_shows_the_name_and_no_value():
    """占位列：列名照常出现，这一格不出数值。"""
    definition = _definition({
        "id": "section_1", "title": "总耗电", "metricIntents": ["总耗电"],
        "componentType": "table",
        "columns": [{"id": "col_value", "label": "总耗电"}],
    })
    _add_column(definition, {
        "fieldRef": "simulated.section_1.9", "label": "待确认指标",
        "displayMode": "placeholder_only", "bindingKind": "simulated",
    })
    payload = _payload(definition)

    assert "待确认指标" in [item["label"] for item in payload["columns"]]
    assert "simulated.section_1.9" not in payload["rows"][0]


def test_a_placeholder_metric_card_shows_no_big_number_and_no_trend():
    """占位指标卡：只有名称，没有大数字、没有趋势值。"""
    definition = _definition({
        "id": "section_1", "title": "总耗电概览", "metricIntents": ["总耗电"],
        "componentType": "metric_card",
    })
    region = definition["regions"][0]
    placeholder = {
        "fieldRef": "metric.machine_room.total_energy", "label": "总耗电",
        "displayMode": "placeholder_only", "bindingKind": "simulated",
    }
    region["component"]["columns"] = [dict(placeholder)]
    region["templateBinding"]["previewSpec"]["columns"] = [dict(placeholder)]
    payload = _payload(definition)

    assert payload["value"] is None
    assert payload["yoyPercent"] is None
    assert payload["comparisonAvailable"] is False
    assert payload["metrics"][0]["label"] == "总耗电"
    assert "value" not in payload["metrics"][0]


def test_a_placeholder_metric_produces_no_chart_points():
    """占位指标不进 series，也不产生数据点。"""
    definition = _definition({
        "id": "section_1", "title": "总耗电趋势", "metricIntents": ["总耗电"],
        "componentType": "line_chart",
    })
    region = definition["regions"][0]
    placeholder = {
        "fieldRef": "metric.machine_room.total_energy", "label": "总耗电",
        "displayMode": "placeholder_only", "bindingKind": "simulated",
    }
    region["component"]["columns"] = [dict(placeholder)]
    region["templateBinding"]["previewSpec"]["columns"] = [dict(placeholder)]
    payload = _payload(definition)

    assert not payload.get("series")
    assert payload["yValues"] == []
    assert payload["points"] == []


# --------------------------------------------------------------------------
# 4. 通用量纲推断（**没有业务词特判**）
# --------------------------------------------------------------------------


def test_the_shape_inference_only_looks_at_dimensions():
    """认的是**量纲**（单位 / 计数单位 / 比率），不是业务词。"""
    assert infer_shape_from_name("耗电 (kWh)") == ("kWh", "energy")
    assert infer_shape_from_name("工单数（条）") == ("条", "count")
    assert infer_shape_from_name("工单 12 条") == ("条", "count")
    assert infer_shape_from_name("本月成本（万元）") == ("万元", "cost")
    assert infer_shape_from_name("温度（℃）") == ("℃", "temperature")
    assert infer_shape_from_name("客户占比") == ("%", "percent")
    # 认不出来 → 通用数值 + 空单位（**不是「不支持」**）。
    assert infer_shape_from_name("冰箱明细") == ("", "")
    assert infer_shape_from_name("A 机房能耗日报") == ("", "")


def test_an_unknown_metric_still_gets_a_value_and_a_unit():
    """任意新名字都能出数；单位按量纲推，推不出就是空单位。"""
    semantics = resolve_metric_semantics(intent="工单数（条）")
    assert semantics.status == "out_of_catalog"
    assert semantics.unit == "条"

    definition = _definition({
        "id": "section_1", "title": "工单概览", "metricIntents": ["工单数（条）"],
        "componentType": "metric_card",
    })
    payload = _payload(definition)
    assert isinstance(payload["value"], (int, float))
    assert payload["unit"] == "条"


# --------------------------------------------------------------------------
# 5. 构建定义这一层不做真实字段准入
# --------------------------------------------------------------------------


def test_a_bound_but_unexecutable_metric_still_yields_a_valid_definition():
    """绑不上的指标不是错误：照样产出合法定义（模拟绑定）+ 示意数值。"""
    plan = _plan({
        "id": "section_1", "title": "PUE 概览", "metricIntents": ["PUE"],
        "componentType": "metric_card",
    })
    template = {
        "id": "tpl_display", "revision": 1, "planId": plan["id"],
        "planRevision": 1, "title": "A 机房能耗日报", "dataStatus": "partial",
        "layout": {},
        "regions": [{
            "id": "region_1", "sectionId": "section_1", "title": "PUE 概览",
            "componentType": "metric_card", "metricIntents": ["PUE"],
            # 登记了、但当前数据源没有这个字段 → 执行不了。
            "metricRefs": ["metric.machine_room.pue"], "columns": [],
            "dataStatus": "bound",
        }],
        "pendingNotes": [],
    }
    definition = build_template_definition(template, plan, "room_a").model_dump(
        mode="json"
    )
    region = definition["regions"][0]
    metric = region["templateBinding"]["previewSpec"]["metrics"][0]

    assert metric["bindingKind"] == "simulated"
    assert metric["simulatedId"]
    assert region["templateBinding"]["metricRefs"] == []
    payload = _payload(definition)
    assert isinstance(payload["value"], (int, float))
