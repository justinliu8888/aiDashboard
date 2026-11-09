"""区块类能力：新增 / 删除区块、设置展示类型、设置分类统计口径。

这些能力都作用在**隔离副本**上（``ctx.definition``），只改副本；
正式草稿、版本、撤销历史、共享主题都不碰。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.services.validation_service import (
    SUPPORTED_CHART_KINDS,
    SUPPORTED_COMPONENT_TYPES,
)
from app.services.template_edit_engine import DISPLAY_OPTIONS_BY_SHAPE

from app.services.edit_capabilities.base import (
    CapabilityError,
    CapabilityOutcome,
    EditCapability,
    OperationContext,
    register,
)
from app.services.edit_capabilities.chart_data import (
    DEFAULT_CATEGORY_FIELD,
    DEFAULT_VALUE_FIELD,
    PREVIEW_SPEC_KEY,
    SIMULATED_DIMENSIONS,
    SIMULATED_KEY,
    normalize_categories,
    resolve_dimension,
)


#: 图表类型的中文说法（给候选卡上的「具体改什么」用）。
CHART_KIND_LABELS = {
    "PIE": "饼图",
    "DONUT": "环形图",
    "BAR": "柱状图",
    "STACKED_BAR": "堆叠柱状图",
    "LINE": "折线图",
    "AREA": "面积图",
}

#: 图表类型 / 组件类型的取值域 —— 能力总目录与错误反馈都读这一份。
#:
#: 写在这里（参数的**真实定义**里）而不是目录里另抄一份：
#: 改了这一份，取值域说明自动跟着变。
_CHART_KIND_VALUES = sorted(SUPPORTED_CHART_KINDS)
_COMPONENT_TYPE_VALUES = sorted(SUPPORTED_COMPONENT_TYPES)
#: 模拟分类维度的 key 目录（别名也能用，见 ``resolve_dimension``）。
_DIMENSION_VALUES = sorted(SIMULATED_DIMENSIONS)


def _chart_kind_label(chart_kind: Any) -> str:
    """图表类型 → 中文说法（认不出来就原样返回，不猜）。"""
    key = str(chart_kind or "").strip().upper()
    return CHART_KIND_LABELS.get(key, key)


# --------------------------------------------------------------------------
# 参数契约
# --------------------------------------------------------------------------

class CategoryItem(BaseModel):
    """一个分类：标签 + 数值。"""

    label: str = Field(min_length=1)
    value: float = 0

    @field_validator("label", mode="before")
    @classmethod
    def _label_text(cls, value: Any) -> str:
        return str(value or "").strip()


class AddSectionParams(BaseModel):
    """新增一个区块。

    ``title``        区块标题（用户看到的名字）
    ``chartKind``    图表类型；``componentType=CHART`` 时**必填**
    ``componentType``组件类型（默认 CHART）
    ``dimension``    模拟分类维度（key 或业务别名），给 PIE / 分类图用
    ``categories``   显式分类（给了就优先于维度自带的）
    ``unit``         数值单位
    ``replaceRegionId``位置锚点：新区块插到这一块**原来的位置**
    ``afterRegionId`` / ``beforeRegionId`` 相对位置锚点
    ``ref``          本次修改现场里的稳定引用名，后续步骤用它指这块
    """

    title: str = Field(min_length=1)
    componentType: str = Field(
        default="CHART",
        description="组件类型（TEXT / METRIC_CARD / TABLE / CHART）",
        json_schema_extra={"x-allowedValues": _COMPONENT_TYPE_VALUES},
    )
    chartKind: str = Field(
        default="",
        description="图表类型（componentType=CHART 时必填）",
        json_schema_extra={"x-allowedValues": _CHART_KIND_VALUES},
    )
    role: str = "DETAIL"
    dimension: str = Field(
        default="",
        description="模拟分类维度（key 或业务别名）",
        json_schema_extra={"x-allowedValues": _DIMENSION_VALUES},
    )
    categories: list[CategoryItem] = Field(default_factory=list)
    unit: str = ""
    replaceRegionId: str = ""
    afterRegionId: str = ""
    beforeRegionId: str = ""
    ref: str = ""

    @field_validator("title", mode="before")
    @classmethod
    def _title_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("componentType", mode="before")
    @classmethod
    def _component_upper(cls, value: Any) -> str:
        return str(value or "CHART").strip().upper()

    @field_validator("chartKind", mode="before")
    @classmethod
    def _chart_upper(cls, value: Any) -> str:
        return str(value or "").strip().upper()

    @field_validator("ref", mode="before")
    @classmethod
    def _ref_text(cls, value: Any) -> str:
        return str(value or "").strip()


class DeleteSectionParams(BaseModel):
    """删除一个区块。没有额外参数。"""


class SetDisplayParams(BaseModel):
    """改展示类型。"""

    componentType: str = Field(
        default="",
        description="组件类型（TEXT / METRIC_CARD / TABLE / CHART）",
        json_schema_extra={"x-allowedValues": _COMPONENT_TYPE_VALUES},
    )
    chartKind: str = Field(
        default="",
        description="图表类型（componentType=CHART 时必填）",
        json_schema_extra={"x-allowedValues": _CHART_KIND_VALUES},
    )

    @field_validator("componentType", mode="before")
    @classmethod
    def _component_upper(cls, value: Any) -> str:
        return str(value or "").strip().upper()

    @field_validator("chartKind", mode="before")
    @classmethod
    def _chart_upper(cls, value: Any) -> str:
        return str(value or "").strip().upper()


class SetCategoryDataParams(BaseModel):
    """设置分类统计口径。"""

    dimension: str = Field(
        default="",
        description="模拟分类维度（key 或业务别名）",
        json_schema_extra={"x-allowedValues": _DIMENSION_VALUES},
    )
    categories: list[CategoryItem] = Field(default_factory=list)
    unit: str = ""

    @field_validator("dimension", mode="before")
    @classmethod
    def _dimension_text(cls, value: Any) -> str:
        return str(value or "").strip()


class SetPresentationParams(BaseModel):
    """一批**受约束**的展示操作（样式 / 布局 / 分组）。

    复用既有展示服务：``{"op": "<操作名>", "params": {...}}``。
    校验与执行都走**同一份实现**（``report_presentation_service``），
    不在这里另写一套。

    ``operations`` 是**非空列表**：每一项形如
    ``{"op": "<展示操作名>", "params": {...}}``。``op`` 只能取展示服务的
    白名单（能力总目录里逐条列出）—— **它不是顶层能力**，只能放在
    ``SET_PRESENTATION`` 的这个列表里，不能直接出现在
    ``submit_edit_plan.operations`` 的顶层。
    """

    operations: list[dict[str, Any]] = Field(
        default_factory=list,
        min_length=1,
        description=(
            "外观修改包里的操作名单：非空列表，"
            '每一项形如 {"op": "<展示操作名>", "params": {...}}'
        ),
    )


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

def _slug(text: str) -> str:
    """中文标题 → id 片段（与既有 ``_slugify`` 同口径）。"""
    out: list[str] = []
    for ch in str(text or "").strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "_":
            out.append("_")
    slug = "".join(out).strip("_")
    return slug or "section"


def _unique_region_id(definition: Any, title: str) -> str:
    existing = {str(r.get("id") or "") for r in definition.regions or []}
    base = f"region_{_slug(title)}"
    if base not in existing:
        return base
    index = 2
    while f"{base}_{index}" in existing:
        index += 1
    return f"{base}_{index}"


def _region_index(definition: Any, region_id: str) -> int:
    for index, region in enumerate(definition.regions or []):
        if str(region.get("id") or "") == region_id:
            return index
    return -1


def _preview_spec(region: dict[str, Any]) -> dict[str, Any]:
    binding = region.get("templateBinding") or {}
    spec = binding.get("previewSpec") if isinstance(binding, dict) else None
    return spec if isinstance(spec, dict) else {}


def _write_preview_spec(region: dict[str, Any], spec: dict[str, Any]) -> None:
    binding = region.get("templateBinding")
    if not isinstance(binding, dict):
        binding = {}
        region["templateBinding"] = binding
    binding["previewSpec"] = {**(binding.get("previewSpec") or {}), **spec}


def _categorical_binding(region: dict[str, Any]) -> dict[str, Any] | None:
    spec = _preview_spec(region)
    binding = spec.get(PREVIEW_SPEC_KEY)
    return binding if isinstance(binding, dict) else None


def _data_shape_of(region: dict[str, Any]) -> str:
    """这个区块的数据形态。

    有分类绑定的 → 占比结构（饼图 / 柱状图都合理）；
    没有的 → 退回角色映射（与既有执行器同一张表）。
    """
    if _categorical_binding(region):
        return "SHARE_STRUCTURE"
    role = str(region.get("role") or "").upper()
    return {
        "REPORT_TITLE": "TEXT_HEADING",
        "SUMMARY": "SINGLE_NUMBER",
        "TREND": "TIME_SERIES",
        "DETAIL": "CATEGORY_RANKING",
        "QUALITY": "QUALITY_STATUS",
        "ANALYSIS": "TEXT_ANALYSIS",
    }.get(role, "CATEGORY_RANKING")


def _assert_display_supported(
    region: dict[str, Any], component_type: str, chart_kind: str
) -> None:
    """展示类型与数据形态是否兼容（复用既有那张表，不另写一份）。"""
    shape = _data_shape_of(region)
    allowed = DISPLAY_OPTIONS_BY_SHAPE.get(shape, {}).get("options", [])
    ok = any(
        item["componentType"] == component_type
        and (component_type != "CHART" or item["chartKind"] == chart_kind)
        for item in allowed
    )
    if ok:
        return
    labels = "、".join(
        f"{item['label']}" for item in allowed
    ) or "（无）"
    raise CapabilityError(
        f"这个区块的数据形态不支持「{component_type}"
        f"{'/' + chart_kind if chart_kind else ''}」；可用的展示方式：{labels}。",
        op="SET_DISPLAY",
        target=str(region.get("id") or ""),
        constraint="展示类型必须与数据形态兼容",
        reason="incompatible_display",
    )


def _build_categorical_region(
    *,
    region_id: str,
    title: str,
    chart_kind: str,
    role: str,
    dimension: dict[str, Any] | None,
    categories: list[dict[str, Any]],
    unit: str,
) -> dict[str, Any]:
    """造一个**分类统计**区块（模拟数据，标注模拟）。"""
    category_field = str(
        (dimension or {}).get("categoryField") or DEFAULT_CATEGORY_FIELD
    )
    value_field = str((dimension or {}).get("valueField") or DEFAULT_VALUE_FIELD)
    resolved_unit = unit or str((dimension or {}).get("unit") or "条")
    return {
        "id": region_id,
        "name": title,
        "role": role,
        "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12}},
        "data": {
            "inputs": {"main": {"type": "SIMULATED", "periodRef": "CURRENT"}},
            "pipeline": {
                "nodes": [
                    {
                        "id": "n_categorical",
                        "type": "CATEGORICAL_COUNT",
                        "inputs": ["input:main"],
                        "categoryField": category_field,
                        "valueField": value_field,
                    }
                ],
                "outputs": {
                    "main": {
                        "nodeRef": "node:n_categorical",
                        "expectedFields": {
                            value_field: {"dataType": "DECIMAL", "unit": resolved_unit}
                        },
                    }
                },
            },
        },
        "binding": {"outputRef": "output:main"},
        "component": {
            "type": "CHART",
            "chartKind": chart_kind,
            "title": title,
            "encodings": [
                {"channel": "X", "fieldRef": category_field},
                {"channel": "Y", "fieldRef": value_field},
            ],
        },
        "templateBinding": {
            "previewSpec": {
                "kind": "generic",
                "unit": resolved_unit,
                PREVIEW_SPEC_KEY: {
                    "categoryField": category_field,
                    "valueField": value_field,
                    "categories": categories,
                    "unit": resolved_unit,
                    SIMULATED_KEY: True,
                    "categoryLabel": str((dimension or {}).get("title") or ""),
                    "statistic": str((dimension or {}).get("statistic") or ""),
                },
            }
        },
    }


# --------------------------------------------------------------------------
# 执行实现
# --------------------------------------------------------------------------

def _add_section(
    ctx: OperationContext, params: AddSectionParams
) -> CapabilityOutcome:
    component_type = params.componentType
    if component_type not in SUPPORTED_COMPONENT_TYPES:
        raise CapabilityError(
            f"组件类型 {component_type!r} 不支持；可用："
            f"{'、'.join(sorted(SUPPORTED_COMPONENT_TYPES))}。",
            op="ADD_SECTION",
            constraint="组件类型必须在支持范围内",
            reason="unsupported_component_type",
        )
    chart_kind = params.chartKind
    if component_type == "CHART":
        if not chart_kind:
            raise CapabilityError(
                "选了 CHART，但没有给 chartKind（图表类型）。",
                op="ADD_SECTION",
                constraint="CHART 必须指定 chartKind",
                reason="missing_chart_kind",
            )
        if chart_kind not in SUPPORTED_CHART_KINDS:
            raise CapabilityError(
                f"图表类型 {chart_kind!r} 不支持；可用："
                f"{'、'.join(sorted(SUPPORTED_CHART_KINDS))}。",
                op="ADD_SECTION",
                constraint="chartKind 必须在支持范围内",
                reason="unsupported_chart_kind",
            )

    dimension = resolve_dimension(params.dimension) if params.dimension else None
    if params.dimension and dimension is None:
        raise CapabilityError(
            f"没有名为 {params.dimension!r} 的模拟分类维度；"
            "先用 list_simulated_dimensions 看有哪些维度，或直接给 categories。",
            op="ADD_SECTION",
            constraint="分类维度必须来自维度目录",
            reason="unknown_dimension",
        )
    categories = normalize_categories(
        [c.model_dump() for c in params.categories], dimension
    )
    if component_type == "CHART" and not categories:
        raise CapabilityError(
            "新增图表缺少统计口径：给 dimension（分类维度）或 categories（分类与数值）。",
            op="ADD_SECTION",
            constraint="图表必须有分类与数值",
            reason="missing_statistic",
        )

    if params.ref and params.ref in ctx.refs:
        raise CapabilityError(
            f"引用 {params.ref!r} 已经被这次修改用过了；换一个 ref 名。",
            op="ADD_SECTION",
            target=params.ref,
            constraint="同一份操作清单里 ref 必须唯一",
            reason="duplicate_ref",
        )

    region_id = _unique_region_id(ctx.definition, params.title)
    if component_type == "CHART":
        region = _build_categorical_region(
            region_id=region_id,
            title=params.title,
            chart_kind=chart_kind,
            role=params.role,
            dimension=dimension,
            categories=categories,
            unit=params.unit,
        )
    else:
        region = {
            "id": region_id,
            "name": params.title,
            "role": params.role,
            "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12}},
            "data": {
                "inputs": {"main": {"type": "SIMULATED", "periodRef": "CURRENT"}},
                "pipeline": {
                    "nodes": [
                        {
                            "id": "n_text",
                            "type": "TEXT",
                            "inputs": ["input:main"],
                        }
                    ],
                    "outputs": {"main": {"nodeRef": "node:n_text"}},
                },
            },
            "binding": {"outputRef": "output:main"},
            "component": {"type": component_type, "title": params.title},
            "templateBinding": {"previewSpec": {"kind": "generic"}},
        }

    regions = list(ctx.definition.regions or [])
    insert_at = len(regions)
    anchor = ""
    if params.replaceRegionId:
        index = _region_index(ctx.definition, params.replaceRegionId)
        if index < 0:
            raise CapabilityError(
                f"位置锚点 {params.replaceRegionId!r} 在当前草稿里不存在。",
                op="ADD_SECTION",
                target=params.replaceRegionId,
                constraint="位置锚点必须存在",
                reason="unknown_anchor",
            )
        anchor = params.replaceRegionId
        used = ctx.counters.get(anchor, 0)
        ctx.counters[anchor] = used + 1
        insert_at = index + used
    elif params.afterRegionId:
        index = _region_index(ctx.definition, params.afterRegionId)
        if index < 0:
            raise CapabilityError(
                f"位置锚点 {params.afterRegionId!r} 在当前草稿里不存在。",
                op="ADD_SECTION",
                target=params.afterRegionId,
                constraint="位置锚点必须存在",
                reason="unknown_anchor",
            )
        insert_at = index + 1
    elif params.beforeRegionId:
        index = _region_index(ctx.definition, params.beforeRegionId)
        if index < 0:
            raise CapabilityError(
                f"位置锚点 {params.beforeRegionId!r} 在当前草稿里不存在。",
                op="ADD_SECTION",
                target=params.beforeRegionId,
                constraint="位置锚点必须存在",
                reason="unknown_anchor",
            )
        insert_at = index
    regions.insert(insert_at, region)
    ctx.definition.regions = regions

    if params.ref:
        ctx.refs[params.ref] = region_id
    return CapabilityOutcome(
        ok=True,
        message=(
            f"新增区块「{params.title}」"
            f"（{_chart_kind_label(chart_kind)}）。"
        ),
        region_id=region_id,
        title=params.title,
        chart_kind=chart_kind,
        detail={
            "regionId": region_id,
            "ref": params.ref,
            "title": params.title,
            "chartKind": chart_kind,
            "categories": categories,
            "unit": str((dimension or {}).get("unit") or params.unit or "条"),
            "dimension": (dimension or {}).get("key", ""),
            "position": insert_at,
            **_business_facts(
                message=f"新增区块「{params.title}」（{_chart_kind_label(chart_kind)}）。",
                detail={
                    "regionId": region_id,
                    "ref": params.ref,
                    "title": params.title,
                    "chartKind": chart_kind,
                },
            ),
        },
    )


def _delete_section(
    ctx: OperationContext, params: DeleteSectionParams
) -> CapabilityOutcome:
    region_id = ctx.resolve_target(ctx.current_target)
    index = _region_index(ctx.definition, region_id)
    if index < 0:
        raise CapabilityError(
            f"目标区块 {region_id!r} 不存在。",
            op="DELETE_SECTION",
            target=region_id,
            constraint="目标必须存在",
            reason="unknown_region",
        )
    region = ctx.definition.regions[index]
    title = str(region.get("name") or region_id)
    regions = list(ctx.definition.regions or [])
    regions.pop(index)
    ctx.definition.regions = regions
    return CapabilityOutcome(
        ok=True,
        message=f"删除区块「{title}」。",
        region_id=region_id,
        title=title,
        detail={
            "regionId": region_id,
            "title": title,
            **_business_facts(
                message=f"删除区块「{title}」。",
                detail={"regionId": region_id, "title": title},
            ),
        },
    )


def _set_display(
    ctx: OperationContext, params: SetDisplayParams
) -> CapabilityOutcome:
    region_id = ctx.resolve_target(ctx.current_target)
    region = ctx.find_region(region_id)
    if region is None:
        raise CapabilityError(
            f"目标区块 {region_id!r} 不存在。",
            op="SET_DISPLAY",
            target=region_id,
            constraint="目标必须存在",
            reason="unknown_region",
        )
    component = region.get("component") or {}
    component_type = params.componentType or str(component.get("type") or "")
    chart_kind = params.chartKind or str(component.get("chartKind") or "")
    if not component_type:
        raise CapabilityError(
            "没有给 componentType（组件类型）。",
            op="SET_DISPLAY",
            target=region_id,
            constraint="必须说明改成什么组件类型",
            reason="missing_component_type",
        )
    if component_type not in SUPPORTED_COMPONENT_TYPES:
        raise CapabilityError(
            f"组件类型 {component_type!r} 不支持；可用："
            f"{'、'.join(sorted(SUPPORTED_COMPONENT_TYPES))}。",
            op="SET_DISPLAY",
            target=region_id,
            constraint="组件类型必须在支持范围内",
            reason="unsupported_component_type",
        )
    if component_type == "CHART":
        if not chart_kind:
            raise CapabilityError(
                "选了 CHART，但没有给 chartKind（图表类型）。",
                op="SET_DISPLAY",
                target=region_id,
                constraint="CHART 必须指定 chartKind",
                reason="missing_chart_kind",
            )
        if chart_kind not in SUPPORTED_CHART_KINDS:
            raise CapabilityError(
                f"图表类型 {chart_kind!r} 不支持；可用："
                f"{'、'.join(sorted(SUPPORTED_CHART_KINDS))}。",
                op="SET_DISPLAY",
                target=region_id,
                constraint="chartKind 必须在支持范围内",
                reason="unsupported_chart_kind",
            )
    _assert_display_supported(region, component_type, chart_kind)

    before_kind = str(component.get("chartKind") or "")
    before_type = str(component.get("type") or "")
    new_component = {**component, "type": component_type}
    if component_type == "CHART":
        new_component["chartKind"] = chart_kind
    else:
        new_component.pop("chartKind", None)
    region["component"] = new_component
    return CapabilityOutcome(
        ok=True,
        message=(
            f"「{str(region.get('name') or region_id)}」的展示方式改为"
            + (
                _chart_kind_label(chart_kind)
                if component_type == "CHART"
                else str(component_type)
            )
            + "。"
        ),
        region_id=region_id,
        title=str(region.get("name") or region_id),
        chart_kind=chart_kind,
        detail={
            "regionId": region_id,
            "title": str(region.get("name") or region_id),
            "beforeComponentType": before_type,
            "beforeChartKind": before_kind,
            "componentType": component_type,
            "chartKind": chart_kind,
            **_business_facts(
                message=(
                    f"「{str(region.get('name') or region_id)}」的展示方式改为"
                    + (
                        _chart_kind_label(chart_kind)
                        if component_type == "CHART"
                        else str(component_type)
                    )
                    + "。"
                ),
                detail={
                    "regionId": region_id,
                    "beforeComponentType": before_type,
                    "beforeChartKind": before_kind,
                    "componentType": component_type,
                    "chartKind": chart_kind,
                },
            ),
        },
    )


def _set_category_data(
    ctx: OperationContext, params: SetCategoryDataParams
) -> CapabilityOutcome:
    region_id = ctx.resolve_target(ctx.current_target)
    region = ctx.find_region(region_id)
    if region is None:
        raise CapabilityError(
            f"目标区块 {region_id!r} 不存在。",
            op="SET_CATEGORY_DATA",
            target=region_id,
            constraint="目标必须存在",
            reason="unknown_region",
        )
    dimension = resolve_dimension(params.dimension) if params.dimension else None
    if params.dimension and dimension is None:
        raise CapabilityError(
            f"没有名为 {params.dimension!r} 的模拟分类维度；"
            "先用 list_simulated_dimensions 看有哪些维度，或直接给 categories。",
            op="SET_CATEGORY_DATA",
            target=region_id,
            constraint="分类维度必须来自维度目录",
            reason="unknown_dimension",
        )
    categories = normalize_categories(
        [c.model_dump() for c in params.categories], dimension
    )
    if not categories:
        raise CapabilityError(
            "没有可用的分类与数值：给 dimension（分类维度）或 categories。",
            op="SET_CATEGORY_DATA",
            target=region_id,
            constraint="统计口径必须给出分类与数值",
            reason="missing_statistic",
        )
    labels = [item["label"] for item in categories]
    if len(set(labels)) != len(labels):
        raise CapabilityError(
            f"分类标签重复：{'、'.join(labels)}；每个分类必须唯一。",
            op="SET_CATEGORY_DATA",
            target=region_id,
            constraint="分类标签必须唯一",
            reason="duplicate_category",
        )
    existing = _categorical_binding(region) or {}
    category_field = str(
        existing.get("categoryField")
        or (dimension or {}).get("categoryField")
        or DEFAULT_CATEGORY_FIELD
    )
    value_field = str(
        existing.get("valueField")
        or (dimension or {}).get("valueField")
        or DEFAULT_VALUE_FIELD
    )
    unit = params.unit or str(
        existing.get("unit") or (dimension or {}).get("unit") or "条"
    )
    binding = {
        "categoryField": category_field,
        "valueField": value_field,
        "categories": categories,
        "unit": unit,
        SIMULATED_KEY: True,
        "categoryLabel": str(
            existing.get("categoryLabel") or (dimension or {}).get("title") or ""
        ),
        "statistic": str(
            existing.get("statistic") or (dimension or {}).get("statistic") or ""
        ),
    }
    _write_preview_spec(region, {PREVIEW_SPEC_KEY: binding})
    component = region.get("component") or {}
    new_component = {**component}
    new_component["encodings"] = [
        {"channel": "X", "fieldRef": category_field},
        {"channel": "Y", "fieldRef": value_field},
    ]
    region["component"] = new_component
    return CapabilityOutcome(
        ok=True,
        message=(
            f"「{str(region.get('name') or region_id)}」的统计口径设为"
            f"{binding['statistic'] or '分类计数'}（{len(categories)} 个分类，模拟数据）。"
        ),
        region_id=region_id,
        title=str(region.get("name") or region_id),
        chart_kind=str(new_component.get("chartKind") or ""),
        detail={
            "regionId": region_id,
            "title": str(region.get("name") or region_id),
            "categories": categories,
            "unit": unit,
            "dimension": (dimension or {}).get("key", ""),
            "statistic": binding["statistic"],
            **_business_facts(
                message=(
                    f"「{str(region.get('name') or region_id)}」的统计口径设为"
                    f"{binding['statistic'] or '分类计数'}"
                    f"（{len(categories)} 个分类，模拟数据）。"
                ),
                detail={
                    "regionId": region_id,
                    "statistic": binding["statistic"],
                    "categoryCount": len(categories),
                    "unit": unit,
                },
            ),
        },
    )


def _business_facts(
    *,
    message: str,
    detail: dict[str, Any] | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """能力执行结果里的**业务事实**（追踪层按这五个字段读真实业务结论）。

    ``businessOk`` 是三态：``True`` / ``False`` / ``None``（**未取得**）。
    这里写的是**执行器真的做了什么** —— 不是「没抛异常」。
    """
    return {
        "businessOk": True,
        "businessMessage": message,
        "businessReason": "",
        "businessIssues": [item for item in (issues or []) if isinstance(item, dict)],
        "businessDetail": dict(detail or {}),
    }


# --------------------------------------------------------------------------
# 精确前后变化 + 有界结构投影（追踪层的事实来源）
# --------------------------------------------------------------------------
#
# 展示服务是**只读**的：精确的 changeSet 从它那里来；**结构投影在这里
# 自实现**（只用它公开的只读导出：``iter_regions`` / ``find_region`` /
# ``region_parent`` / ``region_ancestors`` / ``is_container``）。
#
# 落进追踪的每个值都**有界**：只留排障需要的结构（容器排列 / 放置 /
# 样式 / 格式 / 成员顺序 / 标题），**不落盘**整份区块定义、数据行与
# 模拟数值；裁剪过就**标出来**（``truncation`` 说清裁了哪个字段）。

#: 追踪里单个值的字符上限。
MAX_TRACE_VALUE_CHARS = 600
#: 追踪里单个列表的项数上限。
MAX_TRACE_LIST_ITEMS = 20
#: 追踪里单个字典的键数上限。
MAX_TRACE_DICT_KEYS = 24
#: 追踪里变化条目的条数上限。
MAX_TRACE_CHANGE_ENTRIES = 50

#: 只保留这些键（排障需要的结构）。
_TRACE_KEEP_KEYS = frozenset(
    {
        "arrangement",
        "backgroundColor",
        "border",
        "borderRadius",
        "chartKind",
        "children",
        "color",
        "column",
        "columnSpan",
        "columns",
        "component",
        "dateTimePattern",
        "direction",
        "fontFamily",
        "fontSize",
        "fontWeight",
        "format",
        "gap",
        "height",
        "hidden",
        "id",
        "kind",
        "layout",
        "lineHeight",
        "margin",
        "memberIds",
        "memberOrder",
        "memberRegionIds",
        "members",
        "mode",
        "name",
        "nullText",
        "opacity",
        "padding",
        "pattern",
        "placement",
        "precision",
        "row",
        "rowSpan",
        "rounding",
        "roundingMode",
        "style",
        "textAlign",
        "textColor",
        "thousandSeparator",
        "title",
        "type",
        "unit",
        "unitLabel",
        "value",
        "verticalAlign",
        "width",
    }
)
#: 这些键的值是「成员列表」：**只记 id**（不落盘整棵子树）。
_TRACE_MEMBER_KEYS = frozenset(
    {"children", "members", "memberIds", "memberOrder", "memberRegionIds"}
)


def _as_dict_like(definition: Any) -> dict[str, Any]:
    """定义 → 普通的 dict（只读投影用；读不到就空字典）。"""
    if isinstance(definition, dict):
        return definition
    dump = getattr(definition, "model_dump", None)
    if callable(dump):
        try:
            data = dump()
        except Exception:  # noqa: BLE001 — 投影失败不该掀翻主流程
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _member_ids(value: Any) -> list[str]:
    """成员列表 → 只留 id（不落盘整棵子树）。"""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in list(value)[:MAX_TRACE_LIST_ITEMS]:
        if isinstance(item, dict):
            out.append(str(item.get("id") or ""))
        else:
            out.append(str(item or ""))
    return out


def _all_member_ids(value: Any) -> list[str]:
    """成员列表 → 全部 id（**不裁剪**，只用于比较）。"""
    if not isinstance(value, (list, tuple)):
        return []
    return [
        str(item.get("id") or "") if isinstance(item, dict) else str(item or "")
        for item in value
    ]


def _bounded_trace_value(
    value: Any, *, path: str, notes: list[dict[str, Any]], depth: int = 0
) -> tuple[Any, bool]:
    """把一个值变成**有界**的、可进追踪的形状。

    返回 ``(值, 是否裁剪过)``；裁剪痕迹追加到 ``notes``（字段级）。
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    if isinstance(value, str):
        if len(value) > MAX_TRACE_VALUE_CHARS:
            notes.append(
                {
                    "field": path,
                    "reason": "value_too_long",
                    "limit": MAX_TRACE_VALUE_CHARS,
                    "length": len(value),
                }
            )
            return value[:MAX_TRACE_VALUE_CHARS] + "[truncated]", True
        return value, False
    if isinstance(value, (list, tuple)):
        items = list(value)
        out: list[Any] = []
        cut = False
        for index, item in enumerate(items[:MAX_TRACE_LIST_ITEMS]):
            cleaned, item_cut = _bounded_trace_value(
                item, path=f"{path}[{index}]", notes=notes, depth=depth + 1
            )
            out.append(cleaned)
            cut = cut or item_cut
        if len(items) > MAX_TRACE_LIST_ITEMS:
            notes.append(
                {
                    "field": path,
                    "reason": "too_many_items",
                    "limit": MAX_TRACE_LIST_ITEMS,
                    "count": len(items),
                }
            )
            cut = True
        return out, cut
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        cut = False
        kept = 0
        for key, item in value.items():
            name = str(key)
            if name not in _TRACE_KEEP_KEYS:
                notes.append({"field": f"{path}.{name}", "reason": "key_dropped"})
                cut = True
                continue
            if kept >= MAX_TRACE_DICT_KEYS:
                notes.append(
                    {
                        "field": f"{path}.{name}",
                        "reason": "too_many_keys",
                        "limit": MAX_TRACE_DICT_KEYS,
                    }
                )
                cut = True
                break
            kept += 1
            if name in _TRACE_MEMBER_KEYS:
                out[name] = _member_ids(item)
                continue
            cleaned, item_cut = _bounded_trace_value(
                item, path=f"{path}.{name}", notes=notes, depth=depth + 1
            )
            out[name] = cleaned
            cut = cut or item_cut
        return out, cut
    text = str(value)
    if len(text) > MAX_TRACE_VALUE_CHARS:
        notes.append(
            {
                "field": path,
                "reason": "value_too_long",
                "limit": MAX_TRACE_VALUE_CHARS,
                "length": len(text),
            }
        )
        return text[:MAX_TRACE_VALUE_CHARS] + "[truncated]", True
    return text, False


def bounded_changes(entries: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """展示服务的 changeSet → **有界**的精确前后变化。

    每一项保持展示服务 changeSet 的口径（``path`` / ``regionId`` /
    ``before`` / ``after`` / ``summary``）；``before`` / ``after`` 被裁剪过，
    裁剪痕迹放在 ``truncation`` 里（**不塞进变化项本身**）。

    返回 ``(changes, truncation)``。
    """
    raw_entries = list(entries or [])
    changes: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    for entry in raw_entries[:MAX_TRACE_CHANGE_ENTRIES]:
        if isinstance(entry, dict):
            path = str(entry.get("path") or "")
            region_id = str(entry.get("regionId") or "")
            summary = str(entry.get("summary") or "")
            before_raw = entry.get("before")
            after_raw = entry.get("after")
        else:
            path = str(getattr(entry, "path", "") or "")
            region_id = str(getattr(entry, "regionId", "") or "")
            summary = str(getattr(entry, "summary", "") or "")
            before_raw = getattr(entry, "before", None)
            after_raw = getattr(entry, "after", None)
        before, _ = _bounded_trace_value(
            before_raw, path=f"{path}/before", notes=notes
        )
        after, _ = _bounded_trace_value(after_raw, path=f"{path}/after", notes=notes)
        changes.append(
            {
                "path": path,
                "regionId": region_id,
                "before": before,
                "after": after,
                "summary": summary,
            }
        )
    if len(raw_entries) > MAX_TRACE_CHANGE_ENTRIES:
        notes.append(
            {
                "field": "changes",
                "reason": "too_many_entries",
                "limit": MAX_TRACE_CHANGE_ENTRIES,
                "count": len(raw_entries),
            }
        )
    truncation = {"changes": notes[:MAX_TRACE_LIST_ITEMS]} if notes else {}
    return changes, truncation


def _parent_signature(parent: Any) -> dict[str, Any] | None:
    """父容器的**结构签名**（只用于比较父级有没有变化）。"""
    if not isinstance(parent, dict):
        return None
    component = parent.get("component")
    arrangement = component.get("arrangement") if isinstance(component, dict) else None
    return {
        "arrangement": arrangement if isinstance(arrangement, dict) else {},
        "memberIds": _all_member_ids(parent.get("children")),
    }


def project_structure(
    definition: Any, region_ids: Any, *, before: Any = None
) -> dict[str, Any]:
    """有界结构投影：受影响的区域 + 它们所在的容器。

    只记排障需要的结构：标题 / 父子关系 / 成员顺序 / 放置 / 分组排列 /
    父级有没有变化。**不落盘**整份区块定义、数据行与模拟数值。

    ``before`` 给的是**改动前**的定义：拿得到时 ``parentChanged`` 才有
    真值（true / false）；拿不到就是 ``None``（**未取得**，不猜）。
    """
    from app.services.report_presentation_service import (
        is_container,
        iter_regions,
        region_parent,
    )

    root = _as_dict_like(definition)
    root_before = _as_dict_like(before) if before is not None else None

    index: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for region, ancestors in iter_regions(root):
        index[str(region.get("id") or "")] = (region, list(ancestors))

    ordered: list[str] = []
    seen: set[str] = set()
    for item in region_ids or []:
        rid = str(item or "")
        if rid and rid not in seen:
            seen.add(rid)
            ordered.append(rid)

    affected: list[dict[str, Any]] = []
    containers: dict[str, dict[str, Any]] = {}

    for rid in ordered:
        found = index.get(rid)
        region = found[0] if found else None
        ancestors = found[1] if found else []
        parent = ancestors[-1] if ancestors else None

        if parent is not None:
            members = _all_member_ids(parent.get("children"))
        else:
            members = [
                str(item.get("id") or "")
                for item in (root.get("regions") or [])
                if isinstance(item, dict)
            ]
        member_index = members.index(rid) if rid in members else None

        layout = region.get("layout") if isinstance(region, dict) else None
        placement_raw = layout.get("placement") if isinstance(layout, dict) else None
        placement: dict[str, Any] = {}
        if isinstance(placement_raw, dict):
            for key in ("column", "row", "columnSpan", "rowSpan"):
                if key in placement_raw:
                    placement[key] = placement_raw.get(key)

        arrangement: dict[str, Any] = {}
        if parent is not None:
            component = parent.get("component")
            raw = component.get("arrangement") if isinstance(component, dict) else None
            arrangement = raw if isinstance(raw, dict) else {}
        gap = arrangement.get("gap")
        gap = gap if isinstance(gap, dict) else {}

        parent_changed: bool | None = None
        if root_before is not None:
            before_signature = _parent_signature(region_parent(root_before, rid))
            after_signature = _parent_signature(parent)
            if before_signature is not None and after_signature is not None:
                parent_changed = before_signature != after_signature

        affected.append(
            {
                "regionId": rid,
                "title": str((region or {}).get("name") or ""),
                "parentId": str((parent or {}).get("id") or ""),
                "parentTitle": str((parent or {}).get("name") or ""),
                "parentPath": [
                    str(item.get("name") or item.get("id") or "")
                    for item in ancestors
                ],
                "memberOrder": members[:MAX_TRACE_LIST_ITEMS],
                "memberIndex": member_index,
                "placement": placement,
                "groupColumns": arrangement.get("columns"),
                "groupArrangement": {
                    "columns": arrangement.get("columns"),
                    "gap": {"value": gap.get("value"), "unit": str(gap.get("unit") or "")},
                    "mode": str(arrangement.get("mode") or ""),
                },
                "parentChanged": parent_changed,
            }
        )

        # 容器：受影响区域所在的**祖先容器** + 它自己（是容器时）。
        chain = list(ancestors)
        if region is not None and is_container(region):
            chain = chain + [region]
        for position, container in enumerate(chain):
            cid = str(container.get("id") or "")
            if not cid or cid in containers:
                continue
            component = container.get("component")
            raw = component.get("arrangement") if isinstance(component, dict) else None
            container_arrangement = raw if isinstance(raw, dict) else {}
            container_gap = container_arrangement.get("gap")
            container_gap = container_gap if isinstance(container_gap, dict) else {}
            parent_container = chain[position - 1] if position > 0 else None
            containers[cid] = {
                "regionId": cid,
                "title": str(container.get("name") or ""),
                "columns": container_arrangement.get("columns"),
                "gap": {
                    "value": container_gap.get("value"),
                    "unit": str(container_gap.get("unit") or ""),
                },
                "memberIds": _member_ids(container.get("children")),
                "parentId": str((parent_container or {}).get("id") or ""),
            }

    return {"affected": affected, "containers": list(containers.values())}


def _presentation_failure(ctx: OperationContext, result: Any) -> CapabilityError:
    """展示批次**业务上没成** —— 真实原因 / 失败说明 / 结构化问题原样带走。

    ``apply_presentation`` 是纯计算：失败时它**不抛异常**，只把
    ``ok=False`` 和真实原因带回来。**没抛异常不等于成功**。
    """
    reason = str(getattr(result, "reason", "") or "presentation_failed")
    failure = str(getattr(result, "failure", "") or "")
    issues = [
        item
        for item in (getattr(result, "issues", None) or [])
        if isinstance(item, dict)
    ]
    message = (
        f"展示操作没有执行成功（{reason}）：{failure}"
        if failure
        else f"展示操作没有执行成功（{reason}）。"
    )
    return CapabilityError(
        message,
        step=ctx.step,
        op="SET_PRESENTATION",
        target="",
        constraint="展示操作必须能在当前草稿上真正执行",
        reason=reason,
        business={
            "businessOk": False,
            "businessMessage": failure or message,
            "businessReason": reason,
            "businessIssues": issues,
            "businessDetail": {
                "failure": failure,
                "reason": reason,
                "affectedRegionIds": list(
                    getattr(result, "affectedRegionIds", None) or []
                ),
                # 结构化问题原样带走（失败时**没有**前后变化可报 ——
                # 整批没执行成功，不伪造 changes）。
                "issues": issues,
            },
        },
    )


def _set_presentation(
    ctx: OperationContext, params: SetPresentationParams
) -> CapabilityOutcome:
    """一批展示操作：复用既有展示服务的**同一份**校验与执行实现。

    **业务结果先看，再落盘**：``apply_presentation`` 是纯计算 ——
    失败时它**不抛异常**，只把 ``ok=False`` 与真实原因带回来，并把
    **原定义原样带回**。所以这里必须先看 ``result.ok``：
    没抛异常**不等于**分组成功；业务上没成就把真实原因 / 失败说明 /
    结构化问题原样往上带，整批放弃。
    """
    from app.services.report_presentation_service import (
        apply_presentation,
        normalize_presentation_operations,
    )

    if not params.operations:
        raise CapabilityError(
            "没有给出展示操作：operations 形如 [{\"op\": \"SET_STYLE\", \"params\": {...}}]。",
            op="SET_PRESENTATION",
            constraint="至少一项展示操作",
            reason="missing_operations",
        )

    # **引用先解析**：本次修改现场里临时起的引用名（ADD_SECTION 的 ref）
    # 在这里换成真实区块 id —— 展示服务只认真实 id。引用表是**本次执行
    # 现场**的：试做与用户确认各自跑一遍、各自用自己的表。
    resolved_params = params.model_copy(
        update={
            "operations": ctx.resolve_presentation_references(params.operations)
        }
    )
    try:
        normalized = normalize_presentation_operations(
            resolved_params.operations, ctx.definition
        )
    except Exception as exc:  # noqa: BLE001 — 展示服务的校验错误原样带回
        raise CapabilityError(
            f"展示参数不合法：{exc}",
            op="SET_PRESENTATION",
            constraint="展示操作必须符合展示服务的白名单",
            reason="invalid_presentation_ops",
        ) from exc
    try:
        result = apply_presentation(ctx.definition, normalized)
    except Exception as exc:  # noqa: BLE001
        raise CapabilityError(
            f"展示操作执行失败：{exc}",
            op="SET_PRESENTATION",
            constraint="展示操作必须在草稿上可执行",
            reason="presentation_failed",
        ) from exc

    # **业务上到底成没成**：先看这个，再决定要不要落盘。
    if not result.ok:
        raise _presentation_failure(ctx, result)

    payload = result.definition.model_dump()
    # **先算事实再落盘**：结构投影要比对改动前后，``ctx.definition``
    # 必须还是**改动前**的样子。
    changed_entries = list(result.changeSet.changed) if result.changeSet else []
    changes, truncation = bounded_changes(changed_entries)
    affected_ids = [
        str(item) for item in (result.affectedRegionIds or []) if str(item)
    ]
    structure = project_structure(result.definition, affected_ids, before=ctx.definition)
    for key, value in payload.items():
        setattr(ctx.definition, key, value)
    issues = [item for item in (result.issues or []) if isinstance(item, dict)]
    return CapabilityOutcome(
        ok=True,
        message="已应用一批展示操作。",
        detail={
            # 既有键：保留原样（不删、不改名）。
            "operations": normalized,
            # 新增键：精确前后变化 + 有界结构投影（追踪层的事实来源）。
            "changes": changes,
            "structure": structure,
            "truncation": truncation,
            # 包装能力里的子操作（只记操作名与下标，不抄参数）。
            "subOperations": [
                {"op": str(item.get("op") or ""), "index": position}
                for position, item in enumerate(normalized)
                if isinstance(item, dict)
            ],
            **_business_facts(
                message=f"已应用 {len(normalized)} 项展示操作。",
                detail={
                    "affectedRegionIds": list(result.affectedRegionIds or []),
                    "changedCount": len(result.changeSet.changed) if result.changeSet else 0,
                },
                issues=issues,
            ),
        },
    )


# --------------------------------------------------------------------------
# 注册
# --------------------------------------------------------------------------

register(
    EditCapability(
        id="ADD_SECTION",
        family="新增与删除区块",
        title="新增区块",
        description=(
            "在草稿里新增一个区块（图表或文本）。"
            "图表必须给 chartKind（LINE / BAR / PIE / AREA）；"
            "分类统计图给 dimension（模拟分类维度）或 categories（分类与数值）。"
            "要用 ref 给这个新区块起一个稳定引用名，后续步骤用它指这块；"
            "replaceRegionId 让新区块插到那一块原来的位置。"
        ),
        params_schema=AddSectionParams,
        executor=_add_section,
        needs_target=False,
        creates_target=True,
    )
)

register(
    EditCapability(
        id="DELETE_SECTION",
        family="新增与删除区块",
        title="删除区块",
        description="删除草稿里的一个区块。目标用 target.regionId 或 target.ref 指定。",
        params_schema=DeleteSectionParams,
        executor=_delete_section,
    )
)

register(
    EditCapability(
        id="SET_DISPLAY",
        family="设置统计与展示",
        title="设置展示类型",
        description=(
            "改一个区块的展示类型。componentType 取 TEXT / METRIC_CARD / TABLE / CHART；"
            "componentType=CHART 时必须给 chartKind（LINE / BAR / PIE / AREA）。"
        ),
        params_schema=SetDisplayParams,
        executor=_set_display,
    )
)

register(
    EditCapability(
        id="SET_CATEGORY_DATA",
        family="设置统计与展示",
        title="设置分类统计口径",
        description=(
            "给一个图表区块设置分类统计口径：dimension（模拟分类维度，如告警等级 / 告警区域）"
            "或 categories（分类与数值）。数值是模拟的，会在图上标注。"
        ),
        params_schema=SetCategoryDataParams,
        executor=_set_category_data,
    )
)

register(
    EditCapability(
        id="SET_PRESENTATION",
        family="设置样式",
        title="设置样式与布局",
        description=(
            "一批受约束的展示操作（分组 / 排版 / 样式 / 格式 / 页面）。"
            "**这是一个包装能力**：展示操作名不是顶层能力，只能放在它的 "
            'params.operations 里，每一项形如 {"op": "<展示操作名>", "params": {...}}；'
            "operations 是**非空列表**。"
            "允许的 op、每个 op 的必填 / 可选键与取值域见能力总目录的"
            "「展示操作白名单」一节（由展示服务的白名单自动生成）。"
            "**区域级格式（SET_FORMAT）写的是整块卡片的格式**："
            "指标卡 / 指标组里某个指标项的数字显示格式请用 SET_METRIC_ITEM"
            "（指标级）—— 区域级格式会连带改掉这张卡上别的指标的单位。"
        ),
        params_schema=SetPresentationParams,
        executor=_set_presentation,
        needs_target=False,
    )
)
