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
    SIMULATED_KEY,
    normalize_categories,
    resolve_dimension,
)


#: 图表类型的中文说法（给候选卡上的「具体改什么」用）。
CHART_KIND_LABELS = {
    "PIE": "饼图",
    "BAR": "柱状图",
    "LINE": "折线图",
    "AREA": "面积图",
}


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
    componentType: str = "CHART"
    chartKind: str = ""
    role: str = "DETAIL"
    dimension: str = ""
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

    componentType: str = ""
    chartKind: str = ""

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

    dimension: str = ""
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
    """

    operations: list[dict[str, Any]] = Field(default_factory=list)


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
        detail={"regionId": region_id, "title": title},
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
        },
    )


def _set_presentation(
    ctx: OperationContext, params: SetPresentationParams
) -> CapabilityOutcome:
    """一批展示操作：复用既有展示服务的**同一份**校验与执行实现。"""
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
    try:
        normalized = normalize_presentation_operations(
            params.operations, ctx.definition
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
    payload = result.definition.model_dump()
    for key, value in payload.items():
        setattr(ctx.definition, key, value)
    return CapabilityOutcome(
        ok=True,
        message="已应用一批展示操作。",
        detail={"operations": normalized},
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
            "operations 形如 [{\"op\": \"SET_STYLE\", \"params\": {...}}]，"
            "允许的 op：CREATE_GROUP、SET_GROUP_ARRANGEMENT、MOVE_REGION、UNGROUP、"
            "DELETE_GROUP、COPY_REGION、DELETE_REGION、HIDE_REGION、SHOW_REGION、"
            "SET_PLACEMENT、SET_STYLE、SET_FORMAT、SET_PAGE、SET_METADATA、SET_THEME。"
        ),
        params_schema=SetPresentationParams,
        executor=_set_presentation,
        needs_target=False,
    )
)
