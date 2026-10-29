"""工单 03 / GitHub #49：转换层 —— 方案声明的栏目落到区域组件、能力待接通标记。

这一票改的是**三跳**，漏一跳就拿不到列：

    1. schema（``TemplateRegion.columns``）→ 2. 透传（``_build_region``）
    → 3. 落组件（``build_template_definition``）

另外产出两样给下游用的东西：

    - ``templateBinding.previewSpec``：纯结构的示意语义（量身份、单位、
      列定义、能力标记），造数器（#50）与示意载荷（#51）按它造数；
    - ``metadata.templateBinding.definitionId / definitionVersion``：
      给 previewContext 锚定用。

边界（ADR 0004 §2.2）：**定义层面仍然不含任何业务数值**。
"""
from __future__ import annotations

from typing import Any

from app.services.report_template_service import build_template_design
from app.services.template_definition_service import (
    adapt_preview_spec,
    build_template_definition,
)
from app.services.validation_service import validate_definition


# --------------------------------------------------------------------------
# 造数据
# --------------------------------------------------------------------------


def _plan(
    sections: list[dict[str, Any]], *, revision: int = 1, subject: str = "A 机房",
) -> dict[str, Any]:
    return {
        "id": "rplan_convert", "revision": revision, "topic": "A 机房能耗日报",
        "requirements": {
            "metrics": ["总耗电"], "subject": subject,
            "timeRange": "最近 7 天", "exclusions": [],
        },
        "sections": sections,
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }


def _device_table_section() -> dict[str, Any]:
    return {
        "id": "section_device", "title": "设备耗电明细",
        "metricIntents": ["设备耗电"], "componentType": "table",
        "columns": [
            {"id": "col_device", "label": "设备名称", "fieldRef": "deviceName"},
            {
                "id": "col_energy", "label": "耗电 (kWh)", "fieldRef": "energyKwh",
                "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
            },
        ],
    }


def _convert(sections: list[dict[str, Any]], *, plan: dict[str, Any] | None = None,
             subject_id: str = "room_a"):
    """走完整三跳：方案 → 模板区域 → 草稿区域。"""
    plan = plan or _plan(sections)
    template = build_template_design(plan, subject_id=subject_id)
    draft = build_template_definition(template.model_dump(mode="json"), plan, subject_id)
    return template, draft


# --------------------------------------------------------------------------
# 1. 三跳贯通
# --------------------------------------------------------------------------


def test_plan_columns_reach_the_region_and_the_component():
    """方案声明的栏目 → 模板区域 → 草稿组件的列定义（顺序一致）。"""
    sections = [_device_table_section()]
    template, draft = _convert(sections)

    # 第二跳：模板区域带上定稿列。
    region = template.regions[0]
    assert [c.id for c in region.columns] == ["col_device", "col_energy"]
    assert [c.label for c in region.columns] == ["设备名称", "耗电 (kWh)"]
    assert [c.fieldRef for c in region.columns] == ["deviceName", "energyKwh"]

    # 第三跳：草稿组件的列定义就是方案声明的那些列，**不重建硬编码列**。
    columns = draft.regions[0]["component"]["columns"]
    assert columns == [
        {"id": "col_device", "label": "设备名称", "fieldRef": "deviceName"},
        {
            "id": "col_energy", "label": "耗电 (kWh)", "fieldRef": "energyKwh",
            "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
        },
    ]
    # 硬编码的旧列没有混进来。
    assert "timestamp" not in {c["fieldRef"] for c in columns}


def test_declared_columns_are_not_guessed_from_the_title():
    """列不按标题猜：标题写着「明细」也不影响方案声明的列。"""
    sections = [_device_table_section()]
    sections[0]["title"] = "告警明细"
    _, draft = _convert(sections)

    columns = draft.regions[0]["component"]["columns"]
    assert [c["fieldRef"] for c in columns] == ["deviceName", "energyKwh"]
    assert [c["label"] for c in columns] == ["设备名称", "耗电 (kWh)"]


def test_declared_column_order_is_kept():
    """列顺序按方案，不排序。"""
    section = _device_table_section()
    section["columns"] = list(reversed(section["columns"]))
    _, draft = _convert([section])

    columns = draft.regions[0]["component"]["columns"]
    assert [c["id"] for c in columns] == ["col_energy", "col_device"]


def test_duplicate_field_refs_are_made_unique_but_labels_may_repeat():
    """取值键重复必须修正；显示名重复可以保留（键不同就行）。"""
    section = _device_table_section()
    section["columns"] = [
        {"id": "col_a", "label": "耗电", "fieldRef": "energyKwh"},
        {"id": "col_b", "label": "耗电", "fieldRef": "energyKwh"},
    ]
    _, draft = _convert([section])

    columns = draft.regions[0]["component"]["columns"]
    keys = [c["fieldRef"] for c in columns]
    assert len(set(keys)) == len(keys)
    # 显示名保留，键不同。
    assert [c["label"] for c in columns] == ["耗电", "耗电"]


def test_table_without_declared_columns_keeps_the_existing_default():
    """方案没声明栏目 → 沿用既有补齐规则（列不凭空消失）。"""
    sections = [{
        "id": "section_total", "title": "总耗电明细",
        "metricIntents": ["总耗电"], "componentType": "table",
    }]
    template, draft = _convert(sections)

    assert template.regions[0].columns == []
    columns = draft.regions[0]["component"]["columns"]
    assert [c["id"] for c in columns] == ["timestamp", "energyKwh"]
    # 补齐列自带单位：载荷与前端不必硬编码 kWh。
    assert columns[1]["format"]["unitLabel"] == "kWh"


def test_placeholder_table_keeps_the_declared_columns():
    """占位分支也保留定稿列 —— 已绑定/占位两条分支的列结构一致。"""
    sections = [{
        "id": "section_pue_table", "title": "PUE 明细",
        "metricIntents": ["PUE"], "componentType": "table",
        "columns": [
            {"id": "col_a", "label": "机柜", "fieldRef": "cabinetName"},
            {"id": "col_b", "label": "PUE", "fieldRef": "pue"},
        ],
    }]
    template, draft = _convert(sections)

    assert template.regions[0].dataStatus == "placeholder"
    columns = draft.regions[0]["component"]["columns"]
    assert [c["id"] for c in columns] == ["col_a", "col_b"]
    assert [c["fieldRef"] for c in columns] == ["cabinetName", "pue"]
    # 占位仍然不编数值。
    assert "data" not in draft.regions[0]


def test_definition_still_carries_no_business_values():
    """ADR 0004 §2.2：列定义是结构，**定义层面仍然不含业务数值**。"""
    sections = [_device_table_section()]
    _, draft = _convert(sections)

    region = draft.regions[0]
    for key in ("value", "values", "yValues", "xLabels", "rows", "points"):
        assert key not in region, key
    assert "rows" not in region["component"]


# --------------------------------------------------------------------------
# 2. previewSpec：量身份、单位、能力标记
# --------------------------------------------------------------------------


def test_preview_spec_describes_a_bound_energy_region():
    """已接通区域：kind / unit / 列 / 指标身份齐全，能力标记为假。"""
    sections = [_device_table_section()]
    _, draft = _convert(sections)

    spec = draft.regions[0]["templateBinding"]["previewSpec"]
    assert spec["kind"] == "energy"
    assert spec["unit"] == "kWh"
    assert spec["capabilityPending"] is False
    assert [c["fieldRef"] for c in spec["columns"]] == ["deviceName", "energyKwh"]

    metric = spec["metrics"][0]
    assert metric["metricKey"] == "metric.machine_room.device_energy"
    assert metric["label"] == "设备耗电"
    assert metric["kind"] == "energy"
    assert metric["unit"] == "kWh"
    assert metric["capabilityPending"] is False
    assert metric["pendingReason"] is None


def test_preview_spec_marks_pue_as_capability_pending():
    """PUE：指标在册但底层字段没有 → 照实标「能力待接通」，不编口径。"""
    sections = [{
        "id": "section_pue", "title": "PUE 总览",
        "metricIntents": ["PUE"], "componentType": "metric_card",
    }]
    template, draft = _convert(sections)

    assert template.regions[0].capabilityPending is True
    spec = draft.regions[0]["templateBinding"]["previewSpec"]
    assert spec["kind"] == "pue"
    assert spec["capabilityPending"] is True
    metric = spec["metrics"][0]
    assert metric["metricKey"] == "metric.machine_room.pue"
    assert metric["kind"] == "pue"
    assert metric["capabilityPending"] is True
    # 原因照实透传，不编造 PUE 公式。
    assert "口径待核实" in metric["pendingReason"]
    assert "公式" not in metric["pendingReason"]


def test_preview_spec_marks_energy_cost_as_capability_pending():
    """总电费同样算不出来 —— 能力待接通。"""
    sections = [{
        "id": "section_cost", "title": "总电费",
        "metricIntents": ["电费"], "componentType": "metric_card",
    }]
    _, draft = _convert(sections)

    spec = draft.regions[0]["templateBinding"]["previewSpec"]
    assert spec["kind"] == "cost"
    assert spec["capabilityPending"] is True
    assert spec["metrics"][0]["metricKey"] == "metric.machine_room.energy_cost"


def test_preview_spec_recognizes_the_metrics_the_system_cannot_compute_yet():
    """实时功率 / 告警列表 / 机柜状态：身份认得出来，就标能力待接通。"""
    sections = [
        {"id": "section_power", "title": "实时功率",
         "metricIntents": ["实时功率"], "componentType": "line_chart"},
        {"id": "section_alarms", "title": "告警列表",
         "metricIntents": ["告警列表"], "componentType": "table"},
        {"id": "section_cabinet", "title": "机柜状态",
         "metricIntents": ["机柜状态列表"], "componentType": "table"},
    ]
    _, draft = _convert(sections)

    kinds = [
        region["templateBinding"]["previewSpec"]["kind"] for region in draft.regions
    ]
    assert kinds == ["power", "alarms", "cabinet_status"]
    for region in draft.regions:
        spec = region["templateBinding"]["previewSpec"]
        assert spec["capabilityPending"] is True
        assert spec["metrics"][0]["capabilityPending"] is True


def test_preview_spec_uses_a_generic_kind_for_an_unknown_metric():
    """陌生指标：给明确标注的 generic，不因目录没匹配上就留空。"""
    sections = [{
        "id": "section_footfall", "title": "客流量概览",
        "metricIntents": ["客流量"], "componentType": "metric_card",
    }]
    _, draft = _convert(sections)

    spec = draft.regions[0]["templateBinding"]["previewSpec"]
    assert spec["kind"] == "generic"
    assert spec["capabilityPending"] is True
    metric = spec["metrics"][0]
    assert metric["label"] == "客流量"
    assert metric["kind"] == "generic"
    # 身份键是稳定的，不靠标题猜。
    assert metric["metricKey"].startswith("metric.generic.")


def test_authorization_scope_is_not_a_capability_gap():
    """授权范围与「能力待接通」相互独立：越权不算能力缺失。"""
    plan = _plan([{
        "id": "section_total", "title": "总耗电概览",
        "metricIntents": ["总耗电"], "componentType": "metric_card",
    }], subject="B 机房")
    template = build_template_design(plan, subject_id="room_b")

    region = template.regions[0]
    assert region.dataStatus == "placeholder"
    assert "授权范围" in region.placeholderReason
    # 越权是**授权范围**问题，不是「这个指标还没接通」。
    assert region.capabilityPending is False

    # 越权的整轮确认在上游就被拒了，草稿根本不会生成；这里用一份在范围内的
    # 统计对象把这份模板转出来，只为验证转换层不把越权当成能力缺失。
    draft = build_template_definition(
        template.model_dump(mode="json"), plan, "room_a",
    )
    spec = draft.regions[0]["templateBinding"]["previewSpec"]
    assert spec["capabilityPending"] is False
    assert spec["metrics"][0]["capabilityPending"] is False
    # 既有三态不变：仍是占位。
    assert draft.regions[0]["templateBinding"]["dataStatus"] == "placeholder"


def test_preview_spec_of_a_text_region_is_empty():
    """纯说明板块：没有指标，metrics 为空，不是能力待接通。"""
    sections = [{
        "id": "section_note", "title": "说明",
        "metricIntents": [], "componentType": "text",
        "description": "本表统计 A 机房。",
    }]
    _, draft = _convert(sections)

    spec = draft.regions[0]["templateBinding"]["previewSpec"]
    assert spec["metrics"] == []
    assert spec["kind"] == "generic"
    assert spec["capabilityPending"] is False


# --------------------------------------------------------------------------
# 3. 单位与角色（载荷层据此写单位、带 role）
# --------------------------------------------------------------------------


def test_preview_spec_carries_the_region_role():
    """role 原样透传区域结构上那个（与 preview_service 的载荷同构）。"""
    sections = [_device_table_section()]
    _, draft = _convert(sections)

    region = draft.regions[0]
    assert region["templateBinding"]["previewSpec"]["role"] == region["role"]
    assert region["role"]


def test_preview_spec_carries_the_unit_of_the_quantity():
    """单位必须能从 previewSpec 读到：载荷与前端不硬编码 kWh。"""
    sections = [
        {"id": "section_energy", "title": "总耗电",
         "metricIntents": ["总耗电"], "componentType": "metric_card"},
        {"id": "section_cost", "title": "总电费",
         "metricIntents": ["电费"], "componentType": "metric_card"},
        {"id": "section_power", "title": "实时功率",
         "metricIntents": ["实时功率"], "componentType": "line_chart"},
        {"id": "section_pue", "title": "PUE",
         "metricIntents": ["PUE"], "componentType": "metric_card"},
    ]
    _, draft = _convert(sections)

    units = [
        region["templateBinding"]["previewSpec"]["unit"] for region in draft.regions
    ]
    # 能耗 kWh、电费 元、实时功率 kW；PUE 是无量纲比值（指标目录登记的就是空）。
    assert units == ["kWh", "元", "kW", ""]
    # 每个指标上也带着自己的单位。
    for region in draft.regions:
        metric = region["templateBinding"]["previewSpec"]["metrics"][0]
        assert metric["unit"] == region["templateBinding"]["previewSpec"]["unit"]


def test_preview_spec_unit_is_null_for_an_unknown_quantity():
    """陌生指标：单位**不知道**就给 null，不硬编一个单位上去。"""
    sections = [{
        "id": "section_footfall", "title": "客流量",
        "metricIntents": ["客流量"], "componentType": "metric_card",
    }]
    _, draft = _convert(sections)

    spec = draft.regions[0]["templateBinding"]["previewSpec"]
    assert spec["unit"] is None
    assert spec["metrics"][0]["unit"] is None


# --------------------------------------------------------------------------
# 3. 定义身份与版本
# --------------------------------------------------------------------------


def test_definition_metadata_carries_id_and_version():
    """previewContext 锚定用：定义身份 + 版本。"""
    sections = [{
        "id": "section_total", "title": "总耗电概览",
        "metricIntents": ["总耗电"], "componentType": "metric_card",
    }]
    _, draft = _convert(sections)

    binding = draft.metadata["templateBinding"]
    assert binding["definitionId"] == "rplan_convert"
    assert binding["definitionVersion"] == 1
    # 既有字段一个没少。
    assert binding["planId"] == "rplan_convert"
    assert binding["planRevision"] == 1
    assert binding["templateId"]
    assert binding["templateRevision"] == 1


def test_definition_version_changes_with_the_plan_revision():
    """方案一改就产生新版本 —— previewContext 换新锚点。"""
    sections = [{
        "id": "section_total", "title": "总耗电概览",
        "metricIntents": ["总耗电"], "componentType": "metric_card",
    }]
    _, draft = _convert(sections, plan=_plan(sections, revision=3))

    assert draft.metadata["templateBinding"]["definitionVersion"] == 3


# --------------------------------------------------------------------------
# 4. 历史定义的只读语义适配（#51 读取入口调用）
# --------------------------------------------------------------------------


def test_adapt_preview_spec_fills_a_historical_definition():
    """历史定义没有 previewSpec → 只读适配一份出来。"""
    historical = {
        "id": "region_1", "name": "PUE 总览",
        "component": {"type": "METRIC_CARD"},
        "templateBinding": {
            "sectionId": "section_pue", "dataStatus": "placeholder",
            "placeholderReason": "「PUE」口径待核实：当前数据源没有登记这个字段，算不出数值。",
            "metricRefs": ["metric.machine_room.pue"], "pendingReason": None,
        },
    }
    spec = adapt_preview_spec(historical)

    assert spec["kind"] == "pue"
    assert spec["capabilityPending"] is True
    assert spec["metrics"][0]["metricKey"] == "metric.machine_room.pue"
    assert "口径待核实" in spec["metrics"][0]["pendingReason"]


def test_adapt_preview_spec_prefers_the_plan_metric_intents():
    """有方案时优先用方案的指标意图，其次才是既有绑定引用。"""
    historical = {
        "id": "region_1", "name": "某块",
        "component": {"type": "TABLE", "columns": [
            {"id": "col_a", "label": "设备", "fieldRef": "deviceName"},
        ]},
        "templateBinding": {
            "sectionId": "section_1", "dataStatus": "placeholder",
            "placeholderReason": "数据待连接：当前已接通的能力里没有对应指标。",
            "metricRefs": [], "pendingReason": None,
        },
    }
    plan = _plan([{
        "id": "section_1", "title": "实时功率",
        "metricIntents": ["实时功率"], "componentType": "table",
    }])
    spec = adapt_preview_spec(historical, plan=plan)

    assert spec["kind"] == "power"
    assert spec["capabilityPending"] is True
    # 结构化列信息兜底可用。
    assert [c["fieldRef"] for c in spec["columns"]] == ["deviceName"]


def test_adapt_preview_spec_falls_back_to_a_marked_generic_description():
    """什么都认不出来 → 明确标注的 generic，不按区域标题猜口径。"""
    historical = {
        "id": "region_9", "name": "PUE 趋势",
        "component": {"type": "METRIC_CARD"},
        "templateBinding": {
            "sectionId": "section_9", "dataStatus": "placeholder",
            "placeholderReason": "数据待连接。", "metricRefs": [],
            "pendingReason": None,
        },
    }
    spec = adapt_preview_spec(historical)

    # 标题写着 PUE，但**不按标题猜** —— 标题不算指标意图。
    assert spec["kind"] == "generic"
    assert spec["capabilityPending"] is True
    assert spec["metrics"][0]["kind"] == "generic"


def test_adapt_preview_spec_keeps_an_existing_preview_spec():
    """已有 previewSpec 的定义原样返回。"""
    region = {
        "id": "region_1", "name": "总耗电",
        "component": {"type": "METRIC_CARD"},
        "templateBinding": {
            "dataStatus": "bound",
            "previewSpec": {"kind": "energy", "unit": "kWh", "capabilityPending": False,
                            "columns": [], "metrics": []},
        },
    }
    assert adapt_preview_spec(region)["kind"] == "energy"


# --------------------------------------------------------------------------
# 4. 模板态的列取值键不是数据源绑定 —— 草稿仍要过校验
# --------------------------------------------------------------------------


def test_declared_row_keys_do_not_block_validation():
    """方案声明的取值键落进草稿后，草稿**要过得了校验**。

    工单 02 的取值键是**行对象里的键**（``section_1.col_1`` 这种），不是数据源
    字段；工单 03 把它原样落进 ``component.columns``。校验层若把它当数据源
    字段查，草稿就永远停在 VALIDATION_FAILED，出不了预览（ADR 0004 §2.4）。
    """
    section = {
        "id": "section_1", "title": "设备耗电明细",
        "metricIntents": ["设备耗电"], "componentType": "table",
        "columns": [
            {"id": "col_1", "label": "设备名称", "fieldRef": "section_1.col_1"},
            {"id": "col_2", "label": "耗电量", "fieldRef": "section_1.col_2"},
        ],
    }
    _, draft = _convert([section])

    # 取值键原样落位（工单 03 的契约）。
    assert [c["fieldRef"] for c in draft.regions[0]["component"]["columns"]] == [
        "section_1.col_1", "section_1.col_2",
    ]
    # 但它不是数据源字段 —— 校验不能因此判失败。
    result = validate_definition(draft.model_dump(mode="json"))
    assert result.passed, [issue.message for issue in result.issues]
