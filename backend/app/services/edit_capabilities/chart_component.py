"""图表组件能力包：图型 / 编码 / 坐标轴 / 图例 / 提示字段 / 参考线（CH01–CH06）。

**为什么有这一层**：图表的六项展示能力原先散落在提示词、前端渲染与
生成器里，各写一份口径 —— 同一次「改成环形图」在不同地方会被做成三件事
（存成 PIE、存成 BAR、渲染成饼图）。这里把**一项能力 = 一条记录**收在
一起：参数说明、取值域、校验与执行实现只写一次。

**协议是唯一依据**（``report-definition-v1.md`` 的 ``ChartComponent`` /
``Encoding`` / ``AxisSpec`` / ``FormatSpec``）：

* ``chartKind`` **逐字存**：``STACKED_BAR`` 就是 ``STACKED_BAR``，
  ``DONUT`` 就是 ``DONUT`` —— **不映射**成别的图型（映射到 BAR 是老缺陷）。
* ``legend`` **只有布尔值** —— 协议里没有图例位置 / 方向，这里也不编。
* ``AxisSpec`` **没有**刻度间隔 / 次坐标轴 / 自由图表库选项：
  只有 ``title`` / ``minimum`` / ``maximum`` / ``format`` / ``scale``。
* ``referenceLines`` 是 ``Map<string, Expr>`` —— **协议里参考线没有所属轴**，
  所以「画在哪根轴上」只能由**图型**决定：有单一数值轴的图型
  （LINE / AREA / BAR / STACKED_BAR）才收参考线；饼图 / 环形图没有轴，
  参考线无从安放 → 明确拒绝，不猜。

所有执行实现都遵守同一条纪律：**先校验、后落笔** —— 校验没过就抛
:class:`CapabilityError`（带中文说明、人话 constraint、机器 reason），
**绝不改了一半再报错**。
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.services.chart_dimension_service import (
    canonical_field_of,
    dataset_fields,
    ensure_device_dimension,
    field_data_type,
    is_dimension_field,
)
from app.services.display_binding_service import (
    declared_field_candidates,
    DisplayBindingError,
    require_declared_field,
)
from app.services.edit_capabilities.base import (
    CapabilityError,
    as_number,
    CapabilityOutcome,
    EditCapability,
    FAMILY_STAT,
    OperationContext,
    business_facts,
    register,
)
from app.services.report_presentation_service import (
    ALLOWED_FORMAT_KEYS,
    ALLOWED_FORMAT_KIND,
    ALLOWED_ROUNDING_MODE,
    FORMAT_PRECISION_MAX,
    FORMAT_PRECISION_MIN,
)
from app.services.validation_service import SUPPORTED_CHART_KINDS


# --------------------------------------------------------------------------
# 契约事实（取值域只在这里定义一次）
# --------------------------------------------------------------------------

#: 本批开放的图型（协议 chartKind 取值域里本批支持的那六个）。
CHART_KINDS: tuple[str, ...] = tuple(sorted(SUPPORTED_CHART_KINDS))

#: 有坐标轴的图型：LINE / AREA / BAR / STACKED_BAR（**单一数值轴**）。
CARTESIAN_KINDS = frozenset({"LINE", "AREA", "BAR", "STACKED_BAR"})

#: 没有坐标轴的图型：PIE / DONUT（分类即扇区）。
PIE_KINDS = frozenset({"PIE", "DONUT"})

#: 本批开放的视觉通道。
SUPPORTED_CHANNELS: tuple[str, ...] = ("X", "Y", "SERIES", "COLOR", "LABEL")

#: 协议里有、但本批**不开放**的通道（明确拒绝，不静默忽略）。
UNSUPPORTED_CHANNELS: tuple[str, ...] = (
    "SIZE",
    "LOWER",
    "UPPER",
    "FACET_ROW",
    "FACET_COLUMN",
)

#: 坐标轴键（协议里 ``axes`` 是自由键名，本批只开放横纵两根）。
AXIS_KEYS: tuple[str, ...] = ("x", "y")

#: 坐标轴刻度类型（协议 ``AxisSpec.scale`` 的取值域）。
AXIS_SCALES: tuple[str, ...] = ("LINEAR", "LOG", "TIME", "CATEGORY")

#: ``AxisSpec`` 的键（协议里就这几个，别的一律拒绝）。
AXIS_SPEC_KEYS: tuple[str, ...] = (
    "title",
    "minimum",
    "maximum",
    "format",
    "scale",
)

#: 图型中文说法（给消息与候选卡用）。
CHART_KIND_LABELS: dict[str, str] = {
    "LINE": "折线图",
    "AREA": "面积图",
    "BAR": "柱状图",
    "STACKED_BAR": "堆叠柱状图",
    "PIE": "饼图",
    "DONUT": "环形图",
}

#: 保守的颜色取值域：十六进制 / rgb() / hsl() / 颜色关键字或主题 token 名。
#:
#: **不接受 URL** —— 正则里没有 ``:`` ``/`` ``.``，外链一律进不来。
_COLOR_RE = re.compile(
    r"""^(?:
        \#(?:[0-9A-Fa-f]{3,4}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})
        |(?:rgb|rgba|hsl|hsla)\(\s*[0-9%.,/\s]+\)
        |[A-Za-z][A-Za-z0-9_-]*
    )$""",
    re.VERBOSE,
)


# --------------------------------------------------------------------------
# 参数契约
# --------------------------------------------------------------------------

class SetChartKindParams(BaseModel):
    """图表类型（只改图型，不动数据）。"""

    chartKind: str = Field(
        min_length=1,
        description="图表类型（LINE / AREA / BAR / STACKED_BAR / PIE / DONUT）",
        json_schema_extra={"x-allowedValues": list(CHART_KINDS)},
    )

    @field_validator("chartKind", mode="before")
    @classmethod
    def _kind_upper(cls, value: Any) -> str:
        return str(value or "").strip().upper()


class SetChartEncodingsParams(BaseModel):
    """视觉通道绑定（**完整清单**：给了就是全部）。"""

    encodings: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "完整的通道绑定清单：每一项形如 "
            '{"channel": "X", "fieldRef": "<字段名>", "format": {...}}；'
            "channel 取 X / Y / SERIES / COLOR / LABEL，每个通道最多一项。"
        ),
        json_schema_extra={
            "items": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "enum": list(SUPPORTED_CHANNELS),
                        "description": "视觉通道（X / Y / SERIES / COLOR / LABEL）",
                    },
                    "fieldRef": {
                        "type": "string",
                        "description": "数据字段名（必须已登记或属于模拟字段目录）",
                    },
                    "format": {
                        "type": "object",
                        "description": "该通道的显示格式（可选）",
                    },
                },
            }
        },
    )


class SetChartAxesParams(BaseModel):
    """坐标轴设置（按轴逐项合并；``clearAxes`` 清空全部）。"""

    axes: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "要设置的坐标轴：键是 'x' 或 'y'，值形如 "
            '{"title": "...", "minimum": 0, "maximum": 100, "format": {...}, "scale": "LINEAR"}；'
            "给哪个轴就改哪个轴，没给的轴保持原样。"
        ),
    )
    clearAxes: bool = Field(
        default=False,
        description="是否先清空全部坐标轴（true = 清空后再应用 axes 里的内容）",
    )


class SetChartLegendParams(BaseModel):
    """图例与系列配色。"""

    legend: bool | None = Field(
        default=None,
        description="是否显示图例（不填 = 不改；协议里图例只有开关，没有位置 / 方向）",
    )
    seriesColors: dict[str, str] = Field(
        default_factory=dict,
        description="稳定系列键 → 颜色（十六进制 / rgb() / 颜色关键字或主题 token 名）",
    )
    clearSeriesColors: bool = Field(
        default=False,
        description="是否先清空已有配色（true = 清空后再应用 seriesColors）",
    )


class SetChartTooltipParams(BaseModel):
    """提示框里额外显示的字段。"""

    tooltipFields: list[str] = Field(
        default_factory=list,
        description="提示里要额外显示的字段名（非空时按此完整清单替换）",
    )
    clearTooltipFields: bool = Field(
        default=False,
        description="是否清空提示字段（true = 清空；基础提示不受影响）",
    )


class SetChartReferenceLinesParams(BaseModel):
    """参考线（只有常量数值这一种表达式）。"""

    referenceLines: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "参考线：名称 → 表达式，形如 "
            '{"type": "LITERAL", "value": 100}；数值按图表数值轴的单位理解。'
        ),
    )
    clearReferenceLines: bool = Field(
        default=False,
        description="是否先清空已有参考线（true = 清空后再应用 referenceLines）",
    )


# --------------------------------------------------------------------------
# 区域工具
# --------------------------------------------------------------------------

def _component(region: dict[str, Any]) -> dict[str, Any]:
    component = region.get("component") if isinstance(region, dict) else None
    return component if isinstance(component, dict) else {}


def _preview_spec(region: dict[str, Any]) -> dict[str, Any]:
    binding = region.get("templateBinding") if isinstance(region, dict) else None
    spec = binding.get("previewSpec") if isinstance(binding, dict) else None
    return spec if isinstance(spec, dict) else {}


def _categorical_binding(region: dict[str, Any]) -> dict[str, Any] | None:
    """分类统计口径绑定（``previewSpec.categoricalBinding``）。"""
    binding = _preview_spec(region).get("categoricalBinding")
    return binding if isinstance(binding, dict) else None



def _has_category_output(region: dict[str, Any]) -> bool:
    """从已声明的输出识别分类与数值，不另建分类或复制业务数值。"""
    if _categorical_binding(region):
        return True
    encodings = _component(region).get("encodings") or []
    channels = {str(e.get("channel", "")).upper(): e.get("fieldRef")
                for e in encodings if isinstance(e, dict)}
    if channels.get("SERIES"):
        return False  # 多系列不能在换图型时静默丢掉。
    pipeline = (region.get("data") or {}).get("pipeline") or {}
    output_name = str((region.get("binding") or {}).get("outputRef") or "output:main").split(":")[-1]
    output = (pipeline.get("outputs") or {}).get(output_name) or {}
    fields = output.get("expectedFields") or {}
    numeric = {"INTEGER", "INT", "LONG", "DECIMAL", "FLOAT", "DOUBLE", "NUMBER"}
    y = channels.get("Y")
    if str((fields.get(y) or {}).get("dataType", "")).upper() not in numeric:
        return False
    x = channels.get("X")
    if str((fields.get(x) or {}).get("dataType", "")).upper() in {"STRING", "TEXT"}:
        return True
    # 老模板的 X 通道可能仍写 timestamp；按输出链里的真实分组事实识别。
    nodes = {n.get("id"): n for n in pipeline.get("nodes", []) if isinstance(n, dict)}
    ref = output.get("nodeRef", "")
    seen = set()
    while isinstance(ref, str) and ref.startswith("node:"):
        key = ref.split(":", 1)[1]
        if key in seen or key not in nodes:
            break
        seen.add(key)
        node = nodes[key]
        if node.get("type") == "RESAMPLE":
            # 有限时间桶本身也是现成的标签/数值配对；SUM/COUNT 可直接作扇区，
            # 保留每个桶及其原值。原始连续序列和均值不隐式改统计口径。
            return node.get("timeFieldRef") == x and any(
                m.get("asField") == y and m.get("aggregation") in {"SUM", "COUNT"}
                for m in node.get("measures", []) if isinstance(m, dict))
        if node.get("type") == "GROUP_AGGREGATE":
            return any(str((fields.get(f) or {}).get("dataType", "")).upper() in {"STRING", "TEXT"}
                       for f in node.get("groupBy", [])) and any(
                           m.get("asField") == y for m in node.get("measures", []) if isinstance(m, dict))
        if node.get("type") not in {"SORT", "LIMIT", "FILTER"}:
            break
        inputs = node.get("inputs") or []
        ref = inputs[0] if len(inputs) == 1 else ""
    return False


def _chart_kind(region: dict[str, Any]) -> str:
    return str(_component(region).get("chartKind") or "").strip().upper()


def _region_title(region: dict[str, Any], region_id: str) -> str:
    return str(region.get("name") or region_id or "区块")


def _require_chart_region(
    region: dict[str, Any], region_id: str, *, op: str
) -> dict[str, Any]:
    """目标必须是图表区块（类型明确写成别的就拒绝）。"""
    component_type = str(_component(region).get("type") or "").strip().upper()
    if component_type and component_type != "CHART":
        raise CapabilityError(
            f"「{_region_title(region, region_id)}」是 {component_type} 区块，不是图表；"
            "图表能力只能作用在 CHART 区块上。",
            op=op,
            target=region_id,
            constraint="图表能力只能作用在 CHART 区块上",
            reason="not_a_chart",
        )
    return _component(region)


def _require_field(
    region: dict[str, Any],
    field_ref: Any,
    *,
    what: str,
    op: str,
    target: str,
) -> str:
    """字段必须已登记（否则明确报错，不按名字猜）。"""
    try:
        return require_declared_field(region, str(field_ref or ""), what=what)
    except DisplayBindingError as exc:
        raise CapabilityError(
            exc.message,
            op=op,
            target=target,
            constraint=exc.constraint or f"{what}必须来自当前已登记的输出或模拟字段目录",
            reason=exc.reason,
        ) from exc


def _normalize_format(
    value: Any, *, op: str, target: str, where: str
) -> dict[str, Any]:
    """显示格式：只认项目**现有**的那套键（不另造键名）。"""
    if not isinstance(value, dict):
        raise CapabilityError(
            f"{where} 的显示格式必须是对象。",
            op=op,
            target=target,
            constraint="显示格式必须是对象",
            reason="invalid_format",
        )
    for key in value:
        if key not in ALLOWED_FORMAT_KEYS:
            raise CapabilityError(
                f"{where} 的显示格式不支持 {key!r}；可用键：{'、'.join(ALLOWED_FORMAT_KEYS)}。",
                op=op,
                target=target,
                constraint="显示格式只能用项目现有的一套键",
                reason="invalid_format",
            )
    fmt: dict[str, Any] = {}
    kind = value.get("kind")
    if kind is not None:
        text = str(kind).strip().upper()
        if text not in ALLOWED_FORMAT_KIND:
            raise CapabilityError(
                f"{where} 的显示格式类型 {text!r} 不支持；可用：{'、'.join(ALLOWED_FORMAT_KIND)}。",
                op=op,
                target=target,
                constraint="显示格式类型必须在支持范围内",
                reason="invalid_format",
            )
        fmt["kind"] = text
    precision = value.get("precision")
    if precision is not None:
        precision = as_number(precision)
        if isinstance(precision, float) and precision.is_integer():
            precision = int(precision)
        if isinstance(precision, bool) or not isinstance(precision, int):
            raise CapabilityError(
                f"{where} 的显示格式 precision 必须是整数。",
                op=op,
                target=target,
                constraint="precision 必须是整数",
                reason="invalid_format",
            )
        if not (FORMAT_PRECISION_MIN <= precision <= FORMAT_PRECISION_MAX):
            raise CapabilityError(
                f"{where} 的显示格式 precision 必须在 "
                f"{FORMAT_PRECISION_MIN} 到 {FORMAT_PRECISION_MAX} 之间。",
                op=op,
                target=target,
                constraint="precision 必须在支持范围内",
                reason="invalid_format",
            )
        fmt["precision"] = precision
    unit_label = value.get("unitLabel")
    if unit_label is not None:
        fmt["unitLabel"] = str(unit_label).strip()
    thousand = value.get("thousandSeparator")
    if thousand is not None:
        if not isinstance(thousand, bool):
            raise CapabilityError(
                f"{where} 的显示格式 thousandSeparator 必须是布尔值。",
                op=op,
                target=target,
                constraint="thousandSeparator 必须是布尔值",
                reason="invalid_format",
            )
        fmt["thousandSeparator"] = thousand
    pattern = value.get("dateTimePattern")
    if pattern is not None:
        fmt["dateTimePattern"] = str(pattern).strip()
    null_text = value.get("nullText")
    if null_text is not None:
        fmt["nullText"] = str(null_text)
    rounding = value.get("roundingMode")
    if rounding is not None:
        text = str(rounding).strip().upper()
        if text not in ALLOWED_ROUNDING_MODE:
            raise CapabilityError(
                f"{where} 的显示格式舍入方式 {text!r} 不支持；"
                f"可用：{'、'.join(ALLOWED_ROUNDING_MODE)}。",
                op=op,
                target=target,
                constraint="舍入方式必须在支持范围内",
                reason="invalid_format",
            )
        fmt["roundingMode"] = text
    return fmt


def _series_candidates(region: dict[str, Any]) -> list[str]:
    """这个图表**真实存在**的稳定系列标识（从定义里收，不编）。"""
    names: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        names.append(text)

    component = _component(region)
    for item in component.get("metrics") or []:
        if isinstance(item, dict):
            _add(item.get("id"))
            _add(item.get("metricKey"))
            _add(item.get("fieldRef"))
    metric = component.get("metric")
    if isinstance(metric, dict):
        _add(metric.get("id"))
        _add(metric.get("metricKey"))
        _add(metric.get("fieldRef"))
    for item in _preview_spec(region).get("metrics") or []:
        if isinstance(item, dict):
            _add(item.get("metricKey"))
            _add(item.get("fieldRef"))
            _add(item.get("id"))
    binding = _categorical_binding(region)
    if binding:
        _add(binding.get("categoryField"))
        _add(binding.get("valueField"))
        for item in binding.get("categories") or []:
            if isinstance(item, dict):
                _add(item.get("label"))
    for encoding in component.get("encodings") or []:
        if isinstance(encoding, dict):
            _add(encoding.get("fieldRef"))
    return names


def _series_label_map(region: dict[str, Any]) -> dict[str, str]:
    """**显示名 → 稳定系列标识**（用户说的系列名怎么落到稳定键上）。

    用户说的「耗电量这个系列」是**显示名**，定义里存的稳定键是
    ``metric.generic.section_4_2`` 这种标识。不做这层映射的实测后果：
    模型照用户的话把 ``seriesColors`` 写成 ``{"耗电量": "#2563eb"}``，
    预执行校验按「系列不存在」把整批操作打回 —— 用户明明指的就是
    那个系列（CH04 实测：预执行校验不通过，草稿没变）。

    映射只认**定义里真实存在的显示名**，且显示名唯一时才映射；
    认不出来就还是「未知系列」，不猜。
    """
    mapping: dict[str, str] = {}
    ambiguous: set[str] = set()

    def _put(label: Any, stable: Any) -> None:
        text = str(label or "").strip()
        key = str(stable or "").strip()
        if not text or not key:
            return
        if text in mapping and mapping[text] != key:
            ambiguous.add(text)
            return
        mapping[text] = key

    component = _component(region)
    for item in component.get("metrics") or []:
        if isinstance(item, dict):
            _put(
                item.get("label"),
                item.get("metricKey") or item.get("id") or item.get("fieldRef"),
            )
    metric = component.get("metric")
    if isinstance(metric, dict):
        _put(
            metric.get("label"),
            metric.get("metricKey") or metric.get("id") or metric.get("fieldRef"),
        )
    for item in _preview_spec(region).get("metrics") or []:
        if isinstance(item, dict):
            _put(
                item.get("label"),
                item.get("metricKey") or item.get("fieldRef") or item.get("id"),
            )
    for text in ambiguous:
        mapping.pop(text, None)
    return mapping


def _region_unit(region: dict[str, Any]) -> str:
    """这个区块声明的单位（参考线数值按它理解）。"""
    data = region.get("data") if isinstance(region, dict) else None
    pipeline = (data or {}).get("pipeline") if isinstance(data, dict) else None
    outputs = (pipeline or {}).get("outputs") if isinstance(pipeline, dict) else None
    for spec in (outputs or {}).values() if isinstance(outputs, dict) else []:
        fields = spec.get("expectedFields") if isinstance(spec, dict) else None
        for field in (fields or {}).values() if isinstance(fields, dict) else []:
            unit = str((field or {}).get("unit") or "").strip()
            if unit:
                return unit
    component = _component(region)
    for item in component.get("metrics") or []:
        if isinstance(item, dict):
            unit = str((item.get("format") or {}).get("unitLabel") or "").strip()
            if unit:
                return unit
    metric = component.get("metric")
    if isinstance(metric, dict):
        unit = str((metric.get("format") or {}).get("unitLabel") or "").strip()
        if unit:
            return unit
    return str(_preview_spec(region).get("unit") or "").strip()


# --------------------------------------------------------------------------
# 执行实现
# --------------------------------------------------------------------------

def _set_chart_kind(
    ctx: OperationContext, params: SetChartKindParams
) -> CapabilityOutcome:
    """改图表类型（只改图型，不动分类、系列与数值）。"""
    region_id = ctx.resolve_target(ctx.current_target)
    region = ctx.find_region(region_id)
    if region is None:
        raise CapabilityError(
            f"目标区块 {region_id!r} 不存在。",
            op="SET_CHART_KIND",
            target=region_id,
            constraint="目标必须存在",
            reason="unknown_region",
        )
    component = _require_chart_region(region, region_id, op="SET_CHART_KIND")
    kind = str(params.chartKind or "").strip().upper()
    if kind not in CHART_KINDS:
        raise CapabilityError(
            f"图表类型 {kind!r} 不支持；本批支持：{'、'.join(CHART_KINDS)}。",
            op="SET_CHART_KIND",
            target=region_id,
            constraint="chartKind 必须在支持范围内",
            reason="unsupported_chart_kind",
        )
    if kind in PIE_KINDS and not _has_category_output(region):
        raise CapabilityError(
            f"「{_region_title(region, region_id)}」当前的数据不是分类口径，"
            f"不能改成{CHART_KIND_LABELS.get(kind, kind)}："
            "饼图与环形图需要先有分类统计口径（分类字段 + 每个分类的数值）。",
            op="SET_CHART_KIND",
            target=region_id,
            constraint="饼图 / 环形图必须建立在分类统计口径上",
            reason="incompatible_data_shape",
        )
    before = str(component.get("chartKind") or "")
    region["component"] = {**component, "chartKind": kind}
    title = _region_title(region, region_id)
    message = (
        f"「{title}」的图表类型改为{CHART_KIND_LABELS.get(kind, kind)}"
        f"（分类、系列与数值不变）。"
    )
    return CapabilityOutcome(
        ok=True,
        message=message,
        region_id=region_id,
        title=title,
        chart_kind=kind,
        detail={
            "regionId": region_id,
            "title": title,
            "beforeChartKind": before,
            "chartKind": kind,
            "categorical": bool(_categorical_binding(region)),
            **business_facts(
                message=message,
                detail={
                    "regionId": region_id,
                    "title": title,
                    "beforeChartKind": before,
                    "chartKind": kind,
                },
            ),
        },
    )


def _set_chart_encodings(
    ctx: OperationContext, params: SetChartEncodingsParams
) -> CapabilityOutcome:
    """设置视觉通道绑定（完整清单）。"""
    region_id = ctx.resolve_target(ctx.current_target)
    region = ctx.find_region(region_id)
    if region is None:
        raise CapabilityError(
            f"目标区块 {region_id!r} 不存在。",
            op="SET_CHART_ENCODINGS",
            target=region_id,
            constraint="目标必须存在",
            reason="unknown_region",
        )
    component = _require_chart_region(region, region_id, op="SET_CHART_ENCODINGS")
    kind = _chart_kind(region)
    pie_like = kind in PIE_KINDS
    seen: set[str] = set()
    encodings: list[dict[str, Any]] = []
    raw_items = [item for item in (params.encodings or []) if isinstance(item, dict)]
    for index, raw in enumerate(raw_items):
        where = f"第 {index + 1} 项通道绑定"
        if not isinstance(raw, dict):
            raise CapabilityError(
                f"{where} 必须是对象（形如 {{\"channel\": \"X\", \"fieldRef\": \"...\"}}）。",
                op="SET_CHART_ENCODINGS",
                target=region_id,
                constraint="每一项通道绑定都必须是对象",
                reason="invalid_encoding",
            )
        channel = str(raw.get("channel") or "").strip().upper()
        if not channel:
            raise CapabilityError(
                f"{where} 没有给 channel（视觉通道）。",
                op="SET_CHART_ENCODINGS",
                target=region_id,
                constraint="channel 必填",
                reason="missing_channel",
            )
        if channel in UNSUPPORTED_CHANNELS:
            raise CapabilityError(
                f"{where} 的通道 {channel} 本批还不支持；"
                f"本批支持：{'、'.join(SUPPORTED_CHANNELS)}。",
                op="SET_CHART_ENCODINGS",
                target=region_id,
                constraint="本批只开放 X / Y / SERIES / COLOR / LABEL 通道",
                reason="unsupported_channel",
            )
        if channel not in SUPPORTED_CHANNELS:
            raise CapabilityError(
                f"{where} 的通道 {channel!r} 不认识；"
                f"本批支持：{'、'.join(SUPPORTED_CHANNELS)}。",
                op="SET_CHART_ENCODINGS",
                target=region_id,
                constraint="channel 必须在支持范围内",
                reason="unsupported_channel",
            )
        if channel in seen:
            raise CapabilityError(
                f"通道 {channel} 给了多次；每个通道最多绑一个字段。",
                op="SET_CHART_ENCODINGS",
                target=region_id,
                constraint="每个通道最多一项绑定",
                reason="duplicate_channel",
            )
        if pie_like and channel in ("X", "Y", "SERIES"):
            raise CapabilityError(
                f"{CHART_KIND_LABELS.get(kind, kind)}没有横纵轴，通道 {channel} 不适用；"
                "饼图与环形图只能用 COLOR 与 LABEL 通道。",
                op="SET_CHART_ENCODINGS",
                target=region_id,
                constraint="通道必须与图型相容",
                reason="channel_not_applicable",
            )
        field_ref = _resolve_encoding_field(
            ctx,
            region,
            raw.get("fieldRef"),
            channel=channel,
            value_field=_declared_value_field(raw_items),
            op="SET_CHART_ENCODINGS",
            target=region_id,
        )
        item: dict[str, Any] = {"channel": channel, "fieldRef": field_ref}
        raw_format = raw.get("format")
        if raw_format is not None:
            item["format"] = _normalize_format(
                raw_format,
                op="SET_CHART_ENCODINGS",
                target=region_id,
                where=f"通道 {channel}",
            )
        seen.add(channel)
        encodings.append(item)

    region["component"] = {**component, "encodings": encodings}
    # 横轴绑了**分类字段**时，它就不是这一块的「指标」了 ——
    # 留在指标清单里会被当成第二条**数值系列**（验收 11-01：图例把
    # 「设备名称」和「耗电量」并排画成两个堆叠数值系列）。
    _drop_dimension_metrics(region, encodings)
    title = _region_title(region, region_id)
    message = (
        f"「{title}」的通道绑定改为 {len(encodings)} 项"
        f"（{'、'.join(item['channel'] for item in encodings) or '（无）'}）。"
    )
    return CapabilityOutcome(
        ok=True,
        message=message,
        region_id=region_id,
        title=title,
        chart_kind=_chart_kind(region),
        detail={
            "regionId": region_id,
            "title": title,
            "encodings": encodings,
            **business_facts(
                message=message,
                detail={
                    "regionId": region_id,
                    "title": title,
                    "encodings": encodings,
                },
            ),
        },
    )


def _declared_value_field(raw_items: list[dict[str, Any]]) -> str:
    """这次提交里**纵轴**用的字段（横轴要按维度出数时，汇总的量用它）。"""
    for raw in raw_items:
        if str(raw.get("channel") or "").strip().upper() == "Y":
            return str(raw.get("fieldRef") or "").strip()
    return ""


def _field_is_numeric(
    ctx: OperationContext, region: dict[str, Any], name: str
) -> bool:
    """这个字段是不是**数值**字段（横轴不能放数值）。"""
    fields = dataset_fields(ctx.definition, region)
    data_type = field_data_type(fields, name)
    if not data_type:
        # 区域输出自己声明的类型也算数。
        output_fields = _region_output_field_types(region)
        data_type = output_fields.get(name, "")
    return data_type in ("DECIMAL", "INTEGER", "NUMBER", "DOUBLE", "FLOAT")


def _region_output_field_types(region: dict[str, Any]) -> dict[str, str]:
    """区域输出声明的字段类型（字段名 → 大写 dataType）。"""
    data = region.get("data") if isinstance(region, dict) else None
    data = data if isinstance(data, dict) else {}
    pipeline = data.get("pipeline") if isinstance(data.get("pipeline"), dict) else {}
    outputs = pipeline.get("outputs") if isinstance(pipeline.get("outputs"), dict) else {}
    types: dict[str, str] = {}
    for spec in outputs.values():
        expected = (spec or {}).get("expectedFields") if isinstance(spec, dict) else None
        for field_name, field in (expected or {}).items():
            types[str(field_name)] = str(
                (field or {}).get("dataType") or "" if isinstance(field, dict) else ""
            ).strip().upper()
    return types


def _resolve_encoding_field(
    ctx: OperationContext,
    region: dict[str, Any],
    raw_ref: Any,
    *,
    channel: str,
    value_field: str,
    op: str,
    target: str,
) -> str:
    """通道字段：已声明的直接用；**分类字段**先把区域改成按维度出数。

    验收 11-01 的实测后果：用户说「横轴用设备名称」，而 ``deviceName``
    既不在这个区域的输出里、也不在单值模拟字段目录里 → 直接被拒，
    「横轴保持用设备名称」根本设不上；硬写进去，造数器又按时间桶出数，
    横轴仍是小时序号。

    这里把**输入数据集里已经登记过的分类字段**接进来：管线按维度分组、
    输出里声明该字段，然后照常校验。
    """
    name = str(raw_ref or "").strip()
    if not name:
        return _require_field(region, name, what="字段", op=op, target=target)
    try:
        declared = require_declared_field(region, name, what="字段")
    except DisplayBindingError:
        declared = ""
    if declared:
        # 已声明的字段：横轴必须是**分类 / 时间**字段 —— 数值字段放横轴
        # 会把「设备名称」画成数值系列（验收 11-01 的图例就是两条数值系列）。
        if channel == "X" and _field_is_numeric(ctx, region, declared):
            raise CapabilityError(
                f"横轴不能放数值字段 {declared!r}：横轴要的是分类字段"
                "（设备名称 / 设备类型 / 时间…），数值请放纵轴。",
                op=op,
                target=target,
                constraint="横轴必须是分类 / 时间字段",
                reason="channel_field_mismatch",
            )
        return declared
    fields = dataset_fields(ctx.definition, region)
    if not is_dimension_field(fields, name):
        # 不是维度字段 → 照旧按契约明确拒绝（消息里列出可用字段）。
        return _require_field(region, name, what="字段", op=op, target=target)
    if channel not in ("X", "COLOR", "LABEL"):
        raise CapabilityError(
            f"通道 {channel} 用的是数值字段，不能绑分类字段 {name!r}；"
            "分类字段只能放在横轴（或饼图的 COLOR / LABEL 通道）。",
            op=op,
            target=target,
            constraint="分类字段只能绑到横轴通道",
            reason="channel_field_mismatch",
        )
    value = value_field or _current_value_field(region)
    changed = ensure_device_dimension(
        region,
        ctx.definition,
        dimension_field=name,
        value_field=value,
    )
    if not changed and name not in declared_field_candidates(region):
        raise CapabilityError(
            f"分类字段 {name!r} 当前用不了：这个区域的数据集里没有可按它分组的"
            "数值字段，改不了横轴。请改用这个区域已有的字段，或先补充数据来源。",
            op=op,
            target=target,
            constraint="分类字段要有可汇总的数值字段才能用",
            reason="dimension_not_available",
        )
    return _require_field(region, name, what="字段", op=op, target=target)


def _current_value_field(region: dict[str, Any]) -> str:
    """这个区域当前纵轴用的字段（没声明就空串）。"""
    component = region.get("component") if isinstance(region, dict) else None
    component = component if isinstance(component, dict) else {}
    for encoding in component.get("encodings") or []:
        if not isinstance(encoding, dict):
            continue
        if str(encoding.get("channel") or "").strip().upper() == "Y":
            return str(encoding.get("fieldRef") or "").strip()
    return ""


def _drop_dimension_metrics(region: dict[str, Any], encodings: list[dict[str, Any]]) -> None:
    """横轴绑了分类字段时，把它从这一块的**指标清单**里去掉。

    分类字段不是这一块的量：留在 ``previewSpec.metrics`` 里，造数器会照着
    多指标出**两条数值系列**，图例把「设备名称」和「耗电量」并排画成两个
    堆叠数值系列（验收 11-01 的截图）。
    """
    dimension = ""
    for encoding in encodings:
        if str(encoding.get("channel") or "").strip().upper() == "X":
            dimension = str(encoding.get("fieldRef") or "").strip()
            break
    if not dimension:
        return
    binding = region.get("templateBinding") if isinstance(region, dict) else None
    binding = binding if isinstance(binding, dict) else {}
    spec = binding.get("previewSpec") if isinstance(binding.get("previewSpec"), dict) else {}
    metrics = spec.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        return
    kept = []
    removed = 0
    for metric in metrics:
        if not isinstance(metric, dict):
            kept.append(metric)
            continue
        label = str(metric.get("label") or "").strip()
        key = str(metric.get("metricKey") or "").strip()
        canonical = canonical_field_of(label) or canonical_field_of(key)
        if canonical and canonical == dimension:
            removed += 1
            continue
        kept.append(metric)
    if not removed:
        return
    spec["metrics"] = kept
    binding["previewSpec"] = spec
    region["templateBinding"] = binding


def _normalize_axis_spec(
    spec: Any, *, op: str, target: str, where: str
) -> dict[str, Any]:
    """一根坐标轴的设置（协议 AxisSpec 的那几个键）。"""
    if not isinstance(spec, dict):
        raise CapabilityError(
            f"{where} 必须是对象。",
            op=op,
            target=target,
            constraint="坐标轴设置必须是对象",
            reason="invalid_axis_spec",
        )
    for key in spec:
        if key not in AXIS_SPEC_KEYS:
            raise CapabilityError(
                f"{where} 不支持 {key!r}；坐标轴只有这几个键：{'、'.join(AXIS_SPEC_KEYS)}。",
                op=op,
                target=target,
                constraint="坐标轴只能用协议里有的那几个键",
                reason="invalid_axis_spec",
            )
    out: dict[str, Any] = {}
    title = spec.get("title")
    if title is not None:
        if not isinstance(title, str):
            raise CapabilityError(
                f"{where} 的 title 必须是文字。",
                op=op,
                target=target,
                constraint="坐标轴标题必须是文字",
                reason="invalid_axis_spec",
            )
        out["title"] = title.strip()
    minimum = spec.get("minimum")
    maximum = spec.get("maximum")
    if minimum is not None:
        minimum = as_number(minimum)
        if isinstance(minimum, bool) or not isinstance(minimum, (int, float)):
            raise CapabilityError(
                f"{where} 的 minimum 必须是数字。",
                op=op,
                target=target,
                constraint="坐标轴下限必须是数字",
                reason="invalid_axis_range",
            )
        out["minimum"] = minimum
    if maximum is not None:
        maximum = as_number(maximum)
        if isinstance(maximum, bool) or not isinstance(maximum, (int, float)):
            raise CapabilityError(
                f"{where} 的 maximum 必须是数字。",
                op=op,
                target=target,
                constraint="坐标轴上限必须是数字",
                reason="invalid_axis_range",
            )
        out["maximum"] = maximum
    if (
        minimum is not None
        and maximum is not None
        and float(minimum) > float(maximum)
    ):
        raise CapabilityError(
            f"{where} 的下限 {minimum} 大于上限 {maximum}；坐标轴范围必须下限不高于上限。",
            op=op,
            target=target,
            constraint="坐标轴下限不能高于上限",
            reason="invalid_axis_range",
        )
    scale = spec.get("scale")
    if scale is not None:
        text = str(scale).strip().upper()
        if text not in AXIS_SCALES:
            raise CapabilityError(
                f"{where} 的轴类型 {text!r} 不支持；可用：{'、'.join(AXIS_SCALES)}。",
                op=op,
                target=target,
                constraint="坐标轴类型必须在支持范围内",
                reason="invalid_axis_scale",
            )
        out["scale"] = text
    raw_format = spec.get("format")
    if raw_format is not None:
        out["format"] = _normalize_format(
            raw_format, op=op, target=target, where=where
        )
    return out


def _set_chart_axes(
    ctx: OperationContext, params: SetChartAxesParams
) -> CapabilityOutcome:
    """设置坐标轴（按轴逐项合并）。"""
    region_id = ctx.resolve_target(ctx.current_target)
    region = ctx.find_region(region_id)
    if region is None:
        raise CapabilityError(
            f"目标区块 {region_id!r} 不存在。",
            op="SET_CHART_AXES",
            target=region_id,
            constraint="目标必须存在",
            reason="unknown_region",
        )
    component = _require_chart_region(region, region_id, op="SET_CHART_AXES")
    kind = _chart_kind(region)
    if kind in PIE_KINDS:
        raise CapabilityError(
            f"「{_region_title(region, region_id)}」是{CHART_KIND_LABELS.get(kind, kind)}，"
            "没有坐标轴（分类就是扇区），设不了坐标轴。",
            op="SET_CHART_AXES",
            target=region_id,
            constraint="饼图 / 环形图没有坐标轴",
            reason="axes_not_supported",
        )
    validated: dict[str, Any] = {}
    for key, spec in (params.axes or {}).items():
        axis_key = str(key).strip().lower()
        if axis_key not in AXIS_KEYS:
            raise CapabilityError(
                f"坐标轴 {key!r} 不存在；只支持 'x'（横轴）与 'y'（纵轴）。",
                op="SET_CHART_AXES",
                target=region_id,
                constraint="坐标轴只能是 x 或 y",
                reason="unknown_axis",
            )
        validated[axis_key] = _normalize_axis_spec(
            spec,
            op="SET_CHART_AXES",
            target=region_id,
            where=f"坐标轴 {axis_key}",
        )
    current = (
        {}
        if params.clearAxes
        else {
            str(name): value
            for name, value in (component.get("axes") or {}).items()
            if isinstance(value, dict)
        }
    )
    current.update(validated)
    region["component"] = {**component, "axes": current}
    title = _region_title(region, region_id)
    message = (
        f"「{title}」的坐标轴已更新"
        f"（{'、'.join(validated) or '（未指定轴）'}）。"
    )
    return CapabilityOutcome(
        ok=True,
        message=message,
        region_id=region_id,
        title=title,
        chart_kind=kind,
        detail={
            "regionId": region_id,
            "title": title,
            "axes": current,
            "changedAxes": sorted(validated),
            **business_facts(
                message=message,
                detail={
                    "regionId": region_id,
                    "title": title,
                    "axes": current,
                    "changedAxes": sorted(validated),
                },
            ),
        },
    )


def _set_chart_legend(
    ctx: OperationContext, params: SetChartLegendParams
) -> CapabilityOutcome:
    """图例开关与稳定系列配色。"""
    region_id = ctx.resolve_target(ctx.current_target)
    region = ctx.find_region(region_id)
    if region is None:
        raise CapabilityError(
            f"目标区块 {region_id!r} 不存在。",
            op="SET_CHART_LEGEND",
            target=region_id,
            constraint="目标必须存在",
            reason="unknown_region",
        )
    component = _require_chart_region(region, region_id, op="SET_CHART_LEGEND")
    candidates = _series_candidates(region)
    label_map = _series_label_map(region)
    colors: dict[str, str] = {}
    for key, value in (params.seriesColors or {}).items():
        name = str(key).strip()
        if not name:
            raise CapabilityError(
                "系列键不能为空。",
                op="SET_CHART_LEGEND",
                target=region_id,
                constraint="系列键不能为空",
                reason="unknown_series",
            )
        if name not in candidates:
            # 用户说的可能是**显示名**（「耗电量这个系列」）——
            # 映射到稳定系列标识再存，显示名改了配色不跟着丢。
            resolved = label_map.get(name)
            if resolved and resolved in candidates:
                name = resolved
        if name not in candidates:
            raise CapabilityError(
                f"系列 {name!r} 不存在；这个图表可用的系列键："
                f"{'、'.join(candidates) or '（无）'}。",
                op="SET_CHART_LEGEND",
                target=region_id,
                constraint="配色只能给这个图表真实存在的系列",
                reason="unknown_series",
            )
        color = str(value or "").strip()
        if not _COLOR_RE.match(color):
            raise CapabilityError(
                f"系列 {name!r} 的颜色 {color!r} 不认；"
                "只接受十六进制（如 #2563eb）、rgb()/hsl() 写法、颜色关键字或主题 token 名。",
                op="SET_CHART_LEGEND",
                target=region_id,
                constraint="系列颜色必须是颜色值或主题 token 名（不接受外链）",
                reason="invalid_series_color",
            )
        colors[name] = color
    new_component = {**component}
    legend_changed = params.legend is not None
    if legend_changed:
        new_component["legend"] = bool(params.legend)
    current_colors = (
        {}
        if params.clearSeriesColors
        else {
            str(name): str(value)
            for name, value in (component.get("seriesColors") or {}).items()
        }
    )
    current_colors.update(colors)
    new_component["seriesColors"] = current_colors
    region["component"] = new_component
    title = _region_title(region, region_id)
    parts = []
    if legend_changed:
        parts.append("图例" + ("显示" if params.legend else "隐藏"))
    if colors:
        parts.append(f"{len(colors)} 个系列配色")
    message = f"「{title}」的{'、'.join(parts) or '图例设置'}已更新。"
    return CapabilityOutcome(
        ok=True,
        message=message,
        region_id=region_id,
        title=title,
        chart_kind=_chart_kind(region),
        detail={
            "regionId": region_id,
            "title": title,
            "legend": new_component.get("legend"),
            "seriesColors": current_colors,
            "availableSeries": candidates,
            **business_facts(
                message=message,
                detail={
                    "regionId": region_id,
                    "title": title,
                    "legend": new_component.get("legend"),
                    "seriesColors": current_colors,
                },
            ),
        },
    )


def _set_chart_tooltip(
    ctx: OperationContext, params: SetChartTooltipParams
) -> CapabilityOutcome:
    """提示框里额外显示的字段（基础提示永远保留）。"""
    region_id = ctx.resolve_target(ctx.current_target)
    region = ctx.find_region(region_id)
    if region is None:
        raise CapabilityError(
            f"目标区块 {region_id!r} 不存在。",
            op="SET_CHART_TOOLTIP",
            target=region_id,
            constraint="目标必须存在",
            reason="unknown_region",
        )
    component = _require_chart_region(region, region_id, op="SET_CHART_TOOLTIP")
    fields: list[str] = []
    for raw in params.tooltipFields or []:
        name = _require_field(
            region,
            raw,
            what="提示字段",
            op="SET_CHART_TOOLTIP",
            target=region_id,
        )
        if name not in fields:
            fields.append(name)
    current = (
        []
        if params.clearTooltipFields
        else [str(item) for item in (component.get("tooltipFields") or [])]
    )
    if params.tooltipFields:
        current = list(fields)
    region["component"] = {**component, "tooltipFields": current}
    title = _region_title(region, region_id)
    message = (
        f"「{title}」的提示字段设为 {len(current)} 个"
        f"（{'、'.join(current) or '（无额外字段）'}；基础提示保留）。"
    )
    return CapabilityOutcome(
        ok=True,
        message=message,
        region_id=region_id,
        title=title,
        chart_kind=_chart_kind(region),
        detail={
            "regionId": region_id,
            "title": title,
            "tooltipFields": current,
            **business_facts(
                message=message,
                detail={
                    "regionId": region_id,
                    "title": title,
                    "tooltipFields": current,
                },
            ),
        },
    )


def _normalize_literal_expr(
    expr: Any, *, op: str, target: str, where: str, unit: str = ""
) -> dict[str, Any]:
    """参考线表达式：只认 ``LITERAL`` 数值（协议里没有别的这一批能算）。

    **顺带认下用户说的单位**：用户写「数值 500，单位 kWh」，模型就会
    把单位一起放进表达式（``{"type": "LITERAL", "value": 500, "unit": "kWh"}``）。
    协议里的表达式确实没有单位字段 —— 但参考线的数值本来就按**数值轴
    的单位**理解，所以单位对得上时**丢掉它**就行，不该把整条修改打回
    （实测：CH06「参考线，数值 500，单位 kWh」预执行校验不通过，草稿没变）。
    单位对不上才是真问题 —— 明确拒绝。
    """
    if not isinstance(expr, dict):
        raise CapabilityError(
            f"{where} 必须是表达式对象（形如 {{\"type\": \"LITERAL\", \"value\": 100}}）。",
            op=op,
            target=target,
            constraint="参考线表达式必须是对象",
            reason="unsupported_reference_expression",
        )
    declared_unit = str(expr.get("unit") or "").strip()
    extra = [key for key in expr if key not in ("type", "value", "unit")]
    if extra:
        raise CapabilityError(
            f"{where} 只支持常量表达式（type + value），"
            f"不支持 {'、'.join(sorted(extra))}（协议里的表达式没有单位字段）。",
            op=op,
            target=target,
            constraint="参考线只支持 LITERAL 数值表达式",
            reason="unsupported_reference_expression",
        )
    expr_type = str(expr.get("type") or "").strip().upper()
    if expr_type != "LITERAL":
        raise CapabilityError(
            f"{where} 只支持常量表达式（LITERAL）；"
            f"{expr_type or '（未给 type）'} 表达式没法核对单位与数值。",
            op=op,
            target=target,
            constraint="参考线只支持 LITERAL 数值表达式",
            reason="unsupported_reference_expression",
        )
    value = expr.get("value")
    value = as_number(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CapabilityError(
            f"{where} 的 value 必须是数字。",
            op=op,
            target=target,
            constraint="参考线数值必须是数字",
            reason="unsupported_reference_expression",
        )
    if declared_unit and unit and declared_unit != unit:
        # 单位对不上 → 真的拒绝：参考线的数值按数值轴的单位理解，
        # 换个单位就是换了个意思。
        raise CapabilityError(
            f"{where} 的单位是 {declared_unit}，与这个图的数值轴单位 {unit} 不一致 —— "
            f"参考线的数值按 {unit} 理解，请改成 {unit} 或换一个数值轴。",
            op=op,
            target=target,
            constraint="参考线单位必须与数值轴单位一致",
            reason="incompatible_unit",
        )
    # 单位对得上（或这个图没声明单位）→ 丢掉它：协议里的表达式没有单位字段。
    return {"type": "LITERAL", "value": value}


def _set_chart_reference_lines(
    ctx: OperationContext, params: SetChartReferenceLinesParams
) -> CapabilityOutcome:
    """参考线（只挂在有单一数值轴的图型上）。"""
    region_id = ctx.resolve_target(ctx.current_target)
    region = ctx.find_region(region_id)
    if region is None:
        raise CapabilityError(
            f"目标区块 {region_id!r} 不存在。",
            op="SET_CHART_REFERENCE_LINES",
            target=region_id,
            constraint="目标必须存在",
            reason="unknown_region",
        )
    component = _require_chart_region(
        region, region_id, op="SET_CHART_REFERENCE_LINES"
    )
    kind = _chart_kind(region)
    if kind in PIE_KINDS:
        raise CapabilityError(
            f"「{_region_title(region, region_id)}」是{CHART_KIND_LABELS.get(kind, kind)}，"
            "没有数值轴，参考线不知道该挂在哪根轴上（协议里的参考线也没有所属轴）。",
            op="SET_CHART_REFERENCE_LINES",
            target=region_id,
            constraint="参考线只能用在有单一数值轴的图型上",
            reason="reference_axis_ambiguous",
        )
    existing = {
        str(name): value
        for name, value in (component.get("referenceLines") or {}).items()
    }
    validated: dict[str, Any] = {}
    for name, expr in (params.referenceLines or {}).items():
        label = str(name).strip()
        if not label:
            raise CapabilityError(
                "参考线名称不能为空。",
                op="SET_CHART_REFERENCE_LINES",
                target=region_id,
                constraint="参考线名称不能为空",
                reason="invalid_reference_line",
            )
        if label in existing:
            raise CapabilityError(
                f"参考线 {label!r} 已经存在；要改它请先 clearReferenceLines 清空后再设。",
                op="SET_CHART_REFERENCE_LINES",
                target=region_id,
                constraint="参考线名称不能重复",
                reason="duplicate_reference_line",
            )
        validated[label] = _normalize_literal_expr(
            expr,
            op="SET_CHART_REFERENCE_LINES",
            target=region_id,
            where=f"参考线 {label}",
            unit=_region_unit(region),
        )
    if params.clearReferenceLines:
        existing = {}
    existing.update(validated)
    region["component"] = {**component, "referenceLines": existing}
    unit = _region_unit(region)
    title = _region_title(region, region_id)
    message = (
        f"「{title}」的参考线设为 {len(existing)} 条"
        f"（{'、'.join(existing) or '（无）'}"
        f"{'；数值按 ' + unit + ' 理解' if unit else ''}）。"
    )
    return CapabilityOutcome(
        ok=True,
        message=message,
        region_id=region_id,
        title=title,
        chart_kind=kind,
        detail={
            "regionId": region_id,
            "title": title,
            "referenceLines": existing,
            "unit": unit,
            **business_facts(
                message=message,
                detail={
                    "regionId": region_id,
                    "title": title,
                    "referenceLines": existing,
                    "unit": unit,
                },
            ),
        },
    )


# --------------------------------------------------------------------------
# 注册
# --------------------------------------------------------------------------

register(
    EditCapability(
        id="SET_CHART_KIND",
        family=FAMILY_STAT,
        title="设置图表类型",
        description=(
            "改一个图表区块的图表类型：LINE / AREA / BAR / STACKED_BAR / PIE / DONUT。"
            "**只改图型**，分类、系列与数值一律不动；类型逐字保留"
            "（STACKED_BAR 不折成 BAR，DONUT 不折成 PIE）。"
            "饼图 / 环形图复用当前分类绑定，或当前输出已声明的分类字段与数值字段；已有分组聚合输出、SUM/COUNT 的离散时间桶也可直接切换，保持原标签和逐点数值。无需先调用 SET_CATEGORY_DATA；不得为了切换图型另填分类或数值。未聚合的连续时间序列与多系列不能直接转换。"
        ),
        params_schema=SetChartKindParams,
        executor=_set_chart_kind,
    )
)


register(
    EditCapability(
        id="SET_CHART_ENCODINGS",
        family=FAMILY_STAT,
        title="设置图表通道绑定",
        description=(
            "设置图表的视觉通道绑定（给的是**完整清单**）："
            "每一项形如 {\"channel\": \"X\", \"fieldRef\": \"<字段名>\", \"format\": {...}}；"
            "channel 取 X / Y / SERIES / COLOR / LABEL，每个通道最多一项。"
            "字段必须已登记或属于当前模拟字段目录；"
            "饼图 / 环形图没有横纵轴，只接受 COLOR 与 LABEL。"
        ),
        params_schema=SetChartEncodingsParams,
        executor=_set_chart_encodings,
    )
)


register(
    EditCapability(
        id="SET_CHART_AXES",
        family=FAMILY_STAT,
        title="设置图表坐标轴",
        description=(
            "设置图表坐标轴：axes 的键是 'x' 或 'y'，"
            '值形如 {"title": "...", "minimum": 0, "maximum": 100, "format": {...}, "scale": "LINEAR"}；'
            "给哪个轴就改哪个轴，没给的轴保持原样（clearAxes=true 先清空全部）。"
            "scale 取 LINEAR / LOG / TIME / CATEGORY；下限不能高于上限。"
            "饼图 / 环形图没有坐标轴，设不了。"
        ),
        params_schema=SetChartAxesParams,
        executor=_set_chart_axes,
    )
)


register(
    EditCapability(
        id="SET_CHART_LEGEND",
        family=FAMILY_STAT,
        title="设置图例与系列配色",
        description=(
            "设置图例开关与稳定系列配色：legend 是布尔值（不填 = 不改，"
            "协议里图例只有开关，没有位置与方向）；"
            "seriesColors 是「稳定系列键 → 颜色」的映射"
            "（颜色写十六进制、rgb()/hsl()、颜色关键字或主题 token 名，不接受外链）；"
            "系列键必须是这个图表真实存在的系列（clearSeriesColors=true 先清空）。"
        ),
        params_schema=SetChartLegendParams,
        executor=_set_chart_legend,
    )
)


register(
    EditCapability(
        id="SET_CHART_TOOLTIP",
        family=FAMILY_STAT,
        title="设置图表提示字段",
        description=(
            "设置提示框里**额外**显示的字段：tooltipFields 给的是完整清单"
            "（字段必须已登记或属于当前模拟字段目录；clearTooltipFields=true 清空）。"
            "基础提示（分类名与数值）永远保留，不会被关掉。"
        ),
        params_schema=SetChartTooltipParams,
        executor=_set_chart_tooltip,
    )
)


register(
    EditCapability(
        id="SET_CHART_REFERENCE_LINES",
        family=FAMILY_STAT,
        title="设置图表参考线",
        description=(
            "设置参考线：referenceLines 是「名称 → 表达式」，"
            '形如 {"type": "LITERAL", "value": 100}（本批只支持常量数值表达式，'
            "数值按图表数值轴的单位理解）；"
            "参考线挂在有单一数值轴的图型上，饼图 / 环形图没有轴，设不了；"
            "名称不能重复（clearReferenceLines=true 先清空）。"
        ),
        params_schema=SetChartReferenceLinesParams,
        executor=_set_chart_reference_lines,
    )
)
