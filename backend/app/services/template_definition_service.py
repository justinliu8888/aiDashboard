"""Deterministic adapter from the stored template to the legacy definition.

真相来源分成两半（GitHub #39）：

    - 板块数量 / 顺序 / 标题 / **组件类型** → **已确认方案** ``plan["sections"]``；
    - 指标绑定 / 数据状态 / 占位原因 → **模板区域** ``current.regions``（按
      ``sectionId`` 对齐）。方案里没有「绑到哪个口径」「为什么取不到数」。

所以草稿**长成用户确认的那份方案的样子**：板块一个不多一个不少、顺序一致、
组件类型按方案声明呈现。指标不支持的板块**保留方案声明的组件类型**（空壳卡片
+ 占位原因），**不降级成纯文字**。
"""
from copy import deepcopy
from typing import Any

from app.data.machine_room_energy import METRIC_DEVICE_ENERGY, METRIC_TOTAL_ENERGY
from app.schemas.report_definition import ReportDefinitionDraft
from app.schemas.report_plan import COMPONENT_TYPE_VALUES, PlanContractError
from app.schemas.template_design import TemplateDesign
from app.services.report_draft_service import build_definition_draft


#: 方案组件类型 → 草稿区域形状。
#: 方案声明的组件类型必须**原样**落到草稿里：暂不可取数的板块出空壳，
#: 不降级成纯文字区域（#39 硬要求）。
_PLAN_COMPONENT_SHAPES: dict[str, dict[str, Any]] = {
    "metric_card": {"role": "SUMMARY", "componentType": "METRIC_CARD"},
    "line_chart": {"role": "TREND", "componentType": "CHART", "chartKind": "LINE"},
    "bar_chart": {"role": "DETAIL", "componentType": "CHART", "chartKind": "BAR"},
    "pie_chart": {"role": "DETAIL", "componentType": "CHART", "chartKind": "PIE"},
    "table": {"role": "DETAIL", "componentType": "TABLE"},
    "text": {"role": "DESCRIPTION", "componentType": "TEXT"},
}


def _empty_shell(kind: str, *, title: str, reason: str) -> dict[str, Any]:
    """占位板块的空壳：保留方案声明的组件类型 + 占位原因，**不编任何数值**。"""
    shape = _PLAN_COMPONENT_SHAPES[kind]
    component: dict[str, Any] = {"type": shape["componentType"]}
    if shape["componentType"] == "METRIC_CARD":
        component["metric"] = {
            "id": "", "label": title, "fieldRef": "",
            "format": {"kind": "NUMBER", "precision": 1, "unitLabel": ""},
        }
    elif shape["componentType"] == "CHART":
        component["chartKind"] = shape["chartKind"]
        component["encodings"] = []
    elif shape["componentType"] == "TABLE":
        component["columns"] = []
        component["rowNumber"] = True
    elif shape["componentType"] == "TEXT":
        component["content"] = {"format": "PLAIN", "template": reason or "数据待连接"}
    return {"role": shape["role"], "component": component}


def _plan_context(time_window: dict[str, Any], grain: str) -> dict[str, Any]:
    """区域级上下文：时间窗与粒度**一律按方案**（取数区与占位区同源）。"""
    return {
        "timeWindow": deepcopy(time_window),
        "grain": {"mode": "REPLACE", "value": {"unit": grain}},
    }


def _plan_placement(kind: str, *, index: int) -> dict[str, Any]:
    """按方案顺序与声明的组件类型算出确定性占位（12 列网格）。

    **不读历史模板坐标**（#39 硬要求）：拖拽下线后布局不再从模板继承，
    同一份方案每次转换都得到同一份布局。
    """
    return {
        "type": "GRID", "column": 1, "row": index + 1,
        "columnSpan": 4 if kind == "metric_card" else 12, "rowSpan": 1,
    }


def build_template_definition(
    template: dict[str, Any], plan: dict[str, Any], subject_id: str,
) -> ReportDefinitionDraft:
    current = TemplateDesign.model_validate(template)
    window = (plan.get("requirements") or {}).get("timeRange")
    seed = build_definition_draft(current.title, subject_id, timeRange=window,
                                  componentRequests=["total"])
    # 区域级时间窗与粒度：一律取**方案时间窗**解析出来的结果（取数区与占位区同源）。
    # 复用草稿组装层的解析结果，而不是在转换层再解析一遍 → 两层口径不可能漂移。
    plan_time_window = deepcopy(seed.regions[1]["context"]["timeWindow"])
    plan_grain = str((seed.defaults.get("grain") or {}).get("unit") or "DAY")
    regions: list[dict[str, Any]] = []
    supported_refs = {METRIC_TOTAL_ENERGY["metricRef"], METRIC_DEVICE_ENERGY["metricRef"]}
    # 主循环按**方案板块**遍历（不是按模板区域）：数量 / 顺序 / 标题 / 组件类型
    # 全部以用户确认的那份方案为准。
    plan_sections = list(plan.get("sections") or [])
    regions_by_section = {r.sectionId: r for r in current.regions if r.sectionId}

    for index, section in enumerate(plan_sections):
        section_id = str(section.get("id") or f"section_{index + 1}")
        title = str(section.get("title") or f"板块 {index + 1}")
        kind = str(section.get("componentType") or "").strip()      # ← 组件类型取方案
        if kind not in COMPONENT_TYPE_VALUES:                        # ← 第三道闸：转换层也硬失败
            raise PlanContractError(
                f"板块 {index + 1}「{title}」的组件类型非法：{kind!r}；"
                f"合法取值为 {' / '.join(COMPONENT_TYPE_VALUES)}。",
                section_id=section_id, section_title=title, section_index=index,
            )
        region = regions_by_section.get(section_id)
        if region is None:
            raise PlanContractError(
                f"板块 {index + 1}「{title}」在模板里没有对应区域，"
                f"草稿无法与方案对齐，请重新生成模板。",
                section_id=section_id, section_title=title, section_index=index,
            )
        if region.dataStatus == "bound":
            if kind == "text":
                # 纯说明板块不绑定指标 —— 模板层不该把它标成 bound。
                raise PlanContractError(
                    f"板块 {index + 1}「{title}」是纯说明板块，不应该有指标绑定。",
                    section_id=section_id, section_title=title, section_index=index,
                )
            if len(region.metricRefs) != 1 or region.metricRefs[0] not in supported_refs:
                raise PlanContractError(
                    f"板块 {index + 1}「{title}」没有可安全执行的指标绑定，"
                    f"草稿无法与方案对齐。",
                    section_id=section_id, section_title=title, section_index=index,
                )
            device = region.metricRefs[0] == METRIC_DEVICE_ENERGY["metricRef"]
            key = "total" if kind == "metric_card" else "ranking" if device else "trend"
            built = build_definition_draft(current.title, subject_id, timeRange=window,
                                           componentRequests=[key], metricRefs=region.metricRefs)
            converted = deepcopy(built.regions[1])
            # 每个区域显式声明**方案粒度**（bound 区也要）：区域窗口与粒度同源，
            # 不能一个说 7 天、另一个已经是别的粒度。
            converted.setdefault("context", {})["grain"] = {
                "mode": "REPLACE", "value": {"unit": plan_grain}
            }
            if device:
                # 设备级指标卡：执行绑定指向设备耗电口径（这是**实现层**绑定，
                # 不是展示口径；展示口径一律按方案的时间窗 —— 不收窄到最后一天）。
                if kind == "metric_card":
                    for node in converted["data"]["pipeline"]["nodes"]:
                        if node.get("type") == "METRIC":
                            node["metricRef"] = METRIC_DEVICE_ENERGY["metricRef"]
                            node["metricVersion"] = METRIC_DEVICE_ENERGY["metricVersion"]
            if kind == "table" and not device:
                converted["component"] = {
                    "type": "TABLE", "columns": [
                        {"id": "timestamp", "label": "日期", "fieldRef": "timestamp"},
                        {"id": "energyKwh", "label": "耗电", "fieldRef": "energyKwh"},
                    ],
                }
            elif kind.endswith("_chart"):
                converted["component"] = {
                    "type": "CHART", "chartKind": kind.split("_")[0].upper(),
                    "encodings": [
                        {"channel": "X", "fieldRef": "deviceName" if device else "timestamp"},
                        {"channel": "Y", "fieldRef": "energyKwh"},
                    ],
                }
            elif kind == "metric_card":
                converted["component"]["metric"]["label"] = title
        elif region.dataStatus == "placeholder":
            # 方案声明的组件类型**原样保留**：出空壳卡片 + 占位原因，
            # 不降级成纯文字区域（#39 硬要求）。**不编任何数值**。
            converted = _empty_shell(
                kind, title=title, reason=region.placeholderReason or "数据待连接"
            )
            converted["context"] = _plan_context(plan_time_window, plan_grain)
        elif region.dataStatus == "text":
            text = (region.data or {}).get("template") or (region.data or {}).get("rendered") or title
            converted = {
                "role": "DESCRIPTION", "component": {
                    "type": "TEXT", "content": {"format": "PLAIN", "template": text or "数据待连接"},
                },
                "context": _plan_context(plan_time_window, plan_grain),
            }
        else:
            raise PlanContractError(
                f"板块 {index + 1}「{title}」的数据状态无法识别：{region.dataStatus!r}。",
                section_id=section_id, section_title=title, section_index=index,
            )
        converted.update({
            "id": region.id, "name": title,
            "layout": {"placement": _plan_placement(kind, index=index)},
            "templateBinding": {
                "sectionId": region.sectionId, "dataStatus": region.dataStatus,
                "placeholderReason": region.placeholderReason,
                "dataSourceLabel": region.dataSourceLabel,
                "metricRefs": list(region.metricRefs),
                # bound 区域：指标已绑定、草稿阶段尚未取数 → 原因从**模板层**
                # ``data.pendingReason`` 原样透传（#38 产出，#39 取用）。
                # 本票**不另写文案**，全链路只有一处来源。
                "pendingReason": (region.data or {}).get("pendingReason")
                if region.dataStatus == "bound" else None,
            },
        })
        regions.append(converted)

    if not regions:
        # 兜底（防御分支）：正常方案不可能没有板块（``ReportPlan.sections`` 至少一个）。
        # 与模板层兜底一致 —— 一个 TEXT 说明区，**不拿固定日报冒充方案**。
        fallback = _empty_shell(
            "text", title=current.title, reason="方案里没有可生成的板块。"
        )
        fallback["id"] = "region_1"
        fallback["name"] = current.title
        fallback["context"] = _plan_context(plan_time_window, plan_grain)
        regions = [fallback]

    seed.regions = regions
    seed.intentBindings = []
    seed.metadata["templateBinding"] = {
        "templateId": current.id, "templateRevision": current.revision,
        "planId": current.planId, "planRevision": current.planRevision,
    }
    seed.metadata["pendingNotes"] = list(current.pendingNotes)
    seed.dependencies["metricRefs"] = [
        item for item in seed.dependencies.get("metricRefs", [])
        if item["ref"] in {ref for r in current.regions if r.dataStatus == "bound" for ref in r.metricRefs}
    ]
    return seed
