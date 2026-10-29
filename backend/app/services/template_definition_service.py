"""Deterministic adapter from the stored template to the legacy definition.

真相来源分成两半（GitHub #39）：

    - 板块数量 / 顺序 / 标题 / **组件类型** / **定稿栏目** → **已确认方案**
      ``plan["sections"]``；
    - 指标绑定 / 数据状态 / 占位原因 → **模板区域** ``current.regions``（按
      ``sectionId`` 对齐）。方案里没有「绑到哪个口径」「为什么取不到数」。

所以草稿**长成用户确认的那份方案的样子**：板块一个不多一个不少、顺序一致、
组件类型按方案声明呈现、**表格列按方案声明落位**。指标不支持的板块
**保留方案声明的组件类型与栏目**（+ 占位原因），**不降级成纯文字**。

区域上另外挂一份**纯结构**的示意语义描述（``templateBinding.previewSpec``）：
量身份、单位、列定义与「能力待接通」标记都在里面，**不含任何样例数值** ——
造数器（#50）与示意载荷（#51）按它造数，前端（#53）按它标注。
"""
from copy import deepcopy
from typing import Any, Mapping

from app.data.machine_room_energy import METRIC_DEVICE_ENERGY, METRIC_TOTAL_ENERGY
from app.schemas.report_definition import ReportDefinitionDraft
from app.schemas.report_plan import COMPONENT_TYPE_VALUES, PlanContractError
from app.schemas.template_design import TemplateDesign, TemplateRegion
from app.services.report_draft_service import build_definition_draft
from app.services.report_template_service import classify_metric_kind
from app.services.metric_mock_service import get_metric


#: 方案组件类型 → 草稿区域形状。
#: 方案声明的组件类型必须**原样**落到草稿里：暂不可取数的板块保留组件类型
#: 与定稿栏目，不降级成纯文字区域（#39 硬要求）。
_PLAN_COMPONENT_SHAPES: dict[str, dict[str, Any]] = {
    "metric_card": {"role": "SUMMARY", "componentType": "METRIC_CARD"},
    "line_chart": {"role": "TREND", "componentType": "CHART", "chartKind": "LINE"},
    "bar_chart": {"role": "DETAIL", "componentType": "CHART", "chartKind": "BAR"},
    "pie_chart": {"role": "DETAIL", "componentType": "CHART", "chartKind": "PIE"},
    "table": {"role": "DETAIL", "componentType": "TABLE"},
    "text": {"role": "DESCRIPTION", "componentType": "TEXT"},
}

#: 方案没声明栏目时，表格区域沿用的既有补齐列（保持既有行为，列不凭空消失）。
#:
#: 这两列就是**能耗趋势表**：列自带单位，载荷与前端不必硬编码 kWh。
_DEFAULT_TABLE_COLUMNS: list[dict[str, Any]] = [
    {"id": "timestamp", "label": "日期", "fieldRef": "timestamp"},
    {
        "id": "energyKwh", "label": "耗电", "fieldRef": "energyKwh",
        "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
    },
]

#: 语义量身份 → 该量的默认单位（指标目录没登记单位时用这个）。
#:
#: 「没有单位」是**明确事实**（告警列表、机柜状态是离散记录；PUE 是无量纲
#: 比值），不是「不知道」—— 载荷层据此决定要不要在图上写单位。
_KIND_DEFAULT_UNITS: dict[str, str] = {
    "energy": "kWh",
    "cost": "元",
    "power": "kW",
    "pue": "",
    "alarms": "",
    "cabinet_status": "",
}

#: 定义组件类型 → 方案组件类型（历史定义的只读适配用）。
_DEFINITION_COMPONENT_TYPES: dict[str, str] = {
    "METRIC_CARD": "metric_card",
    "TABLE": "table",
    "TEXT": "text",
}
_DEFINITION_CHART_TYPES: dict[str, str] = {
    "LINE": "line_chart",
    "BAR": "bar_chart",
    "PIE": "pie_chart",
}


def _empty_shell(
    kind: str, *, title: str, reason: str,
    columns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """占位板块的空壳：保留方案声明的组件类型与**定稿栏目** + 占位原因。

    **不编任何数值** —— 数值由草稿阶段的模拟业务数据渲染产生（ADR 0004），
    而且那张卡片要标「能力待接通」。
    """
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
        # 占位表格也保留方案声明的栏目 —— 定稿列不因占位而丢。
        component["columns"] = [dict(column) for column in (columns or [])]
        component["rowNumber"] = True
    elif shape["componentType"] == "TEXT":
        component["content"] = {"format": "PLAIN", "template": reason or "数据待连接"}
    return {"role": shape["role"], "component": component}


def _declared_columns(
    region: TemplateRegion, section: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """这一块的**定稿列**。

    优先用模板区域带来的（它就是方案透传过来的，已经定稿）；模板是旧的
    没有列时，退回方案板块自己声明的。两处都没有 → 空列表，由调用方按
    既有规则补齐。
    """
    if region.columns:
        return [
            column.model_dump(mode="json", exclude_none=True)
            for column in region.columns
        ]
    raw = section.get("columns")
    if not isinstance(raw, (list, tuple)):
        return []
    return [
        dict(item) for item in raw
        if isinstance(item, Mapping) and str(item.get("label") or "").strip()
    ]


def _slug(text: str) -> str:
    """把任意文本压成可放进标识符的一段。"""
    return "".join(
        char if char.isalnum() else "_" for char in str(text or "")
    ).strip("_").lower() or "x"


def _resolve_unit(catalog: Mapping[str, Any], kind: str) -> str | None:
    """这一量的单位。

    指标目录登记了单位就以目录为准（PUE 登记的是**无量纲**）；目录没登记
    时按语义量身份取默认单位（实时功率 → kW）。实在没有（陌生指标）才给
    ``None`` —— 前端据此**不写单位**，不硬编码 kWh。
    """
    unit = catalog.get("unit")
    if isinstance(unit, str):
        return unit
    return _KIND_DEFAULT_UNITS.get(kind)


def _build_preview_spec(
    *,
    section_id: str,
    role: str,
    component_type: str,
    intents: list[str],
    metric_refs: list[str],
    capability_pending: bool,
    pending_reason: str | None,
    columns: list[dict[str, Any]],
) -> dict[str, Any]:
    """生成区域的**纯结构**示意语义描述。

    形状（跨工单契约「模拟语义与能力」）::

        {
            "role": "SUMMARY" | "TREND" | "DETAIL" | "DESCRIPTION" | ...,
            "kind": "energy" | "cost" | "pue" | "power" | "alarms"
                    | "cabinet_status" | "generic",
            "unit": str | None,
            "capabilityPending": bool,
            "columns": [ {"id", "label", "fieldRef", "format"?} ],
            "metrics": [
                {"metricKey", "label", "kind", "unit",
                 "capabilityPending", "pendingReason"}, ...
            ],
        }

    区域 ``kind`` / ``unit`` 表示**主展示量**；多指标身份以 ``metrics``
    为准；区域的能力布尔是各项的**逻辑或**。``columns`` 只在表格板块
    复用定稿列。

    ``role`` 是区域结构上那个角色的**原样透传**（与 ``preview_service``
    产出的载荷同构：``REPORT_TITLE`` 是标题区，``SUMMARY`` / ``TREND`` /
    ``DETAIL`` 是正文区）—— 载荷层组装 ``regions[]`` 时照它带，别自己猜。

    ``unit`` 是这一量的单位：载荷与前端**从这里读**，不硬编码 kWh。
    指标目录登记的单位优先（PUE 登记的是**无量纲**）；目录没登记时按语义量
    身份取默认单位（实时功率 → kW）；实在没有（陌生指标）才是 ``None``。

    表格列的 ``format``（含 ``unitLabel``）**原样透传方案声明**，转换层
    不替它补 —— 混单位的一张表按区域单位乱填会写错单位。方案没声明格式
    时那一列就没有单位标注（不写错的单位）。

    这里**不保存任何样例业务结果** —— 只有结构、单位与原因。
    """
    metrics: list[dict[str, Any]] = []
    if metric_refs:
        for metric_ref in metric_refs:
            catalog = get_metric(metric_ref) or {}
            label = str(catalog.get("label") or metric_ref)
            kind = classify_metric_kind(intent=label, metric_ref=metric_ref)
            metrics.append({
                "metricKey": metric_ref,
                "label": label,
                "kind": kind,
                "unit": _resolve_unit(catalog, kind),
                "capabilityPending": bool(capability_pending),
                "pendingReason": pending_reason if capability_pending else None,
            })
    else:
        for index, intent in enumerate(intents):
            kind = classify_metric_kind(intent=intent)
            metrics.append({
                # 陌生指标也给一个**稳定**的身份键，不靠标题猜。
                "metricKey": f"metric.{kind}.{_slug(section_id)}_{index + 1}",
                "label": intent,
                "kind": kind,
                "unit": _KIND_DEFAULT_UNITS.get(kind),
                "capabilityPending": bool(capability_pending),
                "pendingReason": pending_reason if capability_pending else None,
            })
    primary = metrics[0] if metrics else {}
    if not metrics and capability_pending:
        # 一个指标都认不出来，但这一块确实接不到数 → 给一个**明确标注**的
        # generic 条目，能力标记不能因为没有指标而丢掉。
        metrics.append({
            "metricKey": f"metric.generic.{_slug(section_id)}_1",
            "label": "未识别指标",
            "kind": "generic",
            "unit": None,
            "capabilityPending": True,
            "pendingReason": pending_reason,
        })
    return {
        "role": role,
        "kind": str(primary.get("kind") or "generic"),
        "unit": primary.get("unit"),
        "capabilityPending": any(item["capabilityPending"] for item in metrics),
        "columns": (
            [dict(column) for column in columns]
            if component_type == "table" else []
        ),
        "metrics": metrics,
    }


def _plan_context(time_window: dict[str, Any], grain: str) -> dict[str, Any]:
    """区域级上下文：时间窗与粒度**一律按方案**（取数区与占位区同源）。"""
    return {
        "timeWindow": deepcopy(time_window),
        "grain": {"mode": "REPLACE", "value": {"unit": grain}},
    }


def _plan_grain_unit(plan: Mapping[str, Any] | None) -> str | None:
    """方案里定稿的时间粒度（``requirements.grainUnit``）。

    **方案是粒度的唯一权威**：用户在方案里确认的粒度，转换层不许自己
    再推定一个（那正是「用户说按小时、图上出日点」这种静默收窄的来源）。
    方案没写（历史方案 / 字段缺失）时返回 None，由调用方按既有规则兜底。
    """
    requirements = (plan or {}).get("requirements") or {}
    if not isinstance(requirements, Mapping):
        return None
    raw = requirements.get("grainUnit")
    if raw is None:
        return None
    # 枚举（GrainUnit）取它的值；普通字符串原样用。
    value = getattr(raw, "value", raw)
    unit = str(value).strip().upper()
    return unit or None


def _plan_placement(kind: str, *, index: int) -> dict[str, Any]:
    """按方案顺序与声明的组件类型算出确定性占位（12 列网格）。

    **不读历史模板坐标**（#39 硬要求）：拖拽下线后布局不再从模板继承，
    同一份方案每次转换都得到同一份布局。
    """
    return {
        "type": "GRID", "column": 1, "row": index + 1,
        "columnSpan": 4 if kind == "metric_card" else 12, "rowSpan": 1,
    }


def _align_resample_grain(region: dict[str, Any], grain: str) -> None:
    """把区域管道里重采样节点的粒度对齐到方案粒度。

    区域上下文（``context.grain``）说的是「这个板块按什么粒度画」，
    管道里的 ``RESAMPLE`` 节点说的是「执行层按什么粒度出数」——
    两者必须一致，否则同一份报表两套口径（界面按小时、执行按天）。
    """
    data = region.get("data")
    if not isinstance(data, dict):
        return
    pipeline = data.get("pipeline")
    if not isinstance(pipeline, dict):
        return
    nodes = pipeline.get("nodes")
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if isinstance(node, dict) and node.get("type") == "RESAMPLE":
            node["grain"] = {"unit": grain}


def build_template_definition(
    template: dict[str, Any], plan: dict[str, Any], subject_id: str,
) -> ReportDefinitionDraft:
    current = TemplateDesign.model_validate(template)
    window = (plan.get("requirements") or {}).get("timeRange")
    seed = build_definition_draft(current.title, subject_id, timeRange=window,
                                  componentRequests=["total"])
    # 区域级时间窗：一律取**方案时间窗**解析出来的结果（取数区与占位区同源）。
    # 复用草稿组装层的解析结果，而不是在转换层再解析一遍 → 两层口径不可能漂移。
    plan_time_window = deepcopy(seed.regions[1]["context"]["timeWindow"])
    # 粒度：**方案定稿的那一份**（唯一权威）。
    # 转换层不再从草稿组装层另取一个 —— 那样「用户说按周、定义里是按天」
    # 就成了静默收窄（规格：方案、模板、区域定义和窗口解析完整传递，
    # 不在转换或渲染时临时收窄）。方案没写粒度时才回退到组装层的默认。
    plan_grain = _plan_grain_unit(plan) or str(
        (seed.defaults.get("grain") or {}).get("unit") or "DAY"
    )
    # 定义级默认粒度同样跟方案一致：造数器与预览上下文都从 defaults.grain 读它。
    seed.defaults["grain"] = {"unit": plan_grain}
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
        # 定稿列：模板区域带来的（方案透传）优先，其次方案板块自己声明的。
        declared_columns = _declared_columns(region, section)
        intents = [
            str(item).strip()
            for item in (region.metricIntents or section.get("metricIntents") or [])
            if str(item).strip()
        ]
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
            # 管道里的重采样粒度跟区域上下文同源：否则执行层按天出数、
            # 界面按小时画点，同一份报表两套口径。
            _align_resample_grain(converted, plan_grain)
            if device:
                # 设备级指标卡：执行绑定指向设备耗电口径（这是**实现层**绑定，
                # 不是展示口径；展示口径一律按方案的时间窗 —— 不收窄到最后一天）。
                if kind == "metric_card":
                    for node in converted["data"]["pipeline"]["nodes"]:
                        if node.get("type") == "METRIC":
                            node["metricRef"] = METRIC_DEVICE_ENERGY["metricRef"]
                            node["metricVersion"] = METRIC_DEVICE_ENERGY["metricVersion"]
            if kind == "table":
                # 表格列**以方案声明为准**：定稿列的 id/label/fieldRef 与顺序
                # 原样落位，不重建硬编码列、不按标题猜列。
                if declared_columns:
                    if device:
                        converted["component"]["columns"] = declared_columns
                    else:
                        converted["component"] = {
                            "type": "TABLE", "columns": declared_columns,
                        }
                elif not device:
                    # 方案没声明栏目 → 沿用既有补齐规则（列不凭空消失）。
                    converted["component"] = {
                        "type": "TABLE", "columns": deepcopy(_DEFAULT_TABLE_COLUMNS),
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
            # 方案声明的组件类型与**定稿栏目**原样保留：空壳 + 占位原因，
            # 不降级成纯文字区域（#39 硬要求）。**不编任何数值** ——
            # 数值由模拟业务数据渲染，能力待接通的还要标出来。
            converted = _empty_shell(
                kind, title=title, reason=region.placeholderReason or "数据待连接",
                columns=declared_columns,
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
        # 角色取**区域结构上那个**（与 preview_service 产出的载荷同构），
        # 载荷层组装 regions[] 时照它带，不自己猜。
        preview_spec = _build_preview_spec(
            section_id=region.sectionId or section_id,
            role=str(converted.get("role") or ""),
            component_type=kind,
            intents=intents,
            metric_refs=list(region.metricRefs),
            capability_pending=bool(region.capabilityPending),
            pending_reason=region.placeholderReason,
            columns=declared_columns,
        )
        converted.update({
            "id": region.id, "name": title,
            "layout": {"placement": _plan_placement(kind, index=index)},
            "templateBinding": {
                "sectionId": region.sectionId, "dataStatus": region.dataStatus,
                "placeholderReason": region.placeholderReason,
                "dataSourceLabel": region.dataSourceLabel,
                "metricRefs": list(region.metricRefs),
                # bound 区域：指标已绑定、当前显示的是模拟业务数据 → 原因从
                # **模板层** ``data.pendingReason`` 原样透传（#38 产出，#39 取用）。
                # 本票**不另写文案**，全链路只有一处来源。
                "pendingReason": (region.data or {}).get("pendingReason")
                if region.dataStatus == "bound" else None,
                # 纯结构的示意语义：量身份、单位、列定义、能力标记。
                # 造数器（#50）与示意载荷（#51）按它造数，不猜口径。
                "previewSpec": preview_spec,
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
        fallback["templateBinding"] = {
            "sectionId": None, "dataStatus": "text",
            "placeholderReason": None, "dataSourceLabel": None,
            "metricRefs": [], "pendingReason": None,
            "previewSpec": _build_preview_spec(
                section_id="", role=str(fallback.get("role") or ""),
                component_type="text", intents=[], metric_refs=[],
                capability_pending=False, pending_reason=None, columns=[],
            ),
        }
        regions = [fallback]

    seed.regions = regions
    seed.intentBindings = []
    seed.metadata["templateBinding"] = {
        "templateId": current.id, "templateRevision": current.revision,
        "planId": current.planId, "planRevision": current.planRevision,
        # 定义身份与版本：给 previewContext 锚定用（#51 建上下文时读这两个）。
        # 身份取**方案 id**（同一份方案的多次确认共用一个定义身份），
        # 版本取**方案 revision**（方案一改就产生新版本，旧上下文保留）。
        "definitionId": current.planId or current.id,
        "definitionVersion": current.planRevision,
    }
    seed.metadata["pendingNotes"] = list(current.pendingNotes)
    seed.dependencies["metricRefs"] = [
        item for item in seed.dependencies.get("metricRefs", [])
        if item["ref"] in {ref for r in current.regions if r.dataStatus == "bound" for ref in r.metricRefs}
    ]
    return seed


# --------------------------------------------------------------------------
# 历史定义的只读语义适配（#51 读取入口调用）
# --------------------------------------------------------------------------


def _plan_section(
    plan: Mapping[str, Any] | None, section_id: str,
) -> Mapping[str, Any] | None:
    """按板块 id 在方案里找板块。"""
    if not plan or not section_id:
        return None
    for section in (plan.get("sections") or []):
        if isinstance(section, Mapping) and str(section.get("id") or "") == section_id:
            return section
    return None


def _definition_component_type(component: Mapping[str, Any]) -> str:
    """定义里的组件类型 → 方案组件类型（只用于判断「是不是表格」）。"""
    component_type = str(component.get("type") or "").upper()
    if component_type in _DEFINITION_COMPONENT_TYPES:
        return _DEFINITION_COMPONENT_TYPES[component_type]
    if component_type == "CHART":
        chart_kind = str(component.get("chartKind") or "LINE").upper()
        return _DEFINITION_CHART_TYPES.get(chart_kind, "line_chart")
    return ""


def _capability_pending_from_reason(data_status: str, reason: Any) -> bool:
    """历史定义没有 ``capabilityPending`` 标记 —— 按占位原因还原。

    **授权范围**不是能力缺失；口径待核实 / 指标缺失才是「能力待接通」。
    """
    if str(data_status) != "placeholder":
        return False
    text = str(reason or "")
    if "授权范围" in text:
        return False
    return True


def adapt_preview_spec(
    region: Mapping[str, Any], *, plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """给**历史定义**补一份纯内存的示意语义描述。

    ADR 0004 之前落库的定义没有 ``templateBinding.previewSpec``。本函数按
    跨工单契约「历史定义缺 previewSpec」的顺序做**只读**适配：

        1. 已有方案的指标意图（传了 ``plan`` 时）；
        2. 既有绑定指标引用 ``templateBinding.metricRefs``；
        3. 既有结构化列信息（``component.columns``）。

    都认不出来时给一个**明确标注**的 ``generic`` 描述并标成能力待接通 ——
    **不按区域标题猜真实口径**，也**不回写**旧方案 / 旧定义。

    已有 ``previewSpec`` 的定义原样返回（本函数不做二次加工）。
    """
    binding = region.get("templateBinding") or {}
    if not isinstance(binding, Mapping):
        binding = {}
    existing = binding.get("previewSpec")
    if isinstance(existing, Mapping) and existing:
        return dict(existing)

    component = region.get("component") or {}
    if not isinstance(component, Mapping):
        component = {}
    section_id = str(binding.get("sectionId") or "")
    plan_section = _plan_section(plan, section_id)
    intents = [
        str(item).strip()
        for item in ((plan_section or {}).get("metricIntents") or [])
        if str(item).strip()
    ]
    metric_refs = [
        str(item).strip()
        for item in (binding.get("metricRefs") or [])
        if str(item).strip()
    ]
    columns = [
        dict(item) for item in (component.get("columns") or [])
        if isinstance(item, Mapping)
    ]
    if not columns and plan_section is not None:
        columns = [
            dict(item) for item in (plan_section.get("columns") or [])
            if isinstance(item, Mapping)
        ]
    reason = binding.get("placeholderReason")
    return _build_preview_spec(
        section_id=section_id,
        role=str(region.get("role") or ""),
        component_type=_definition_component_type(component),
        intents=intents,
        metric_refs=metric_refs,
        capability_pending=_capability_pending_from_reason(
            str(binding.get("dataStatus") or ""), reason,
        ),
        pending_reason=str(reason) if reason else None,
        columns=columns,
    )
