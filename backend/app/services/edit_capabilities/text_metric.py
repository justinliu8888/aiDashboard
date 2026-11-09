"""文本与指标组件能力包：正文模板 / 指标项 / 指标组（T01–T03、M01–M04、MG01–MG03）。

**为什么有这一层**：文本、单指标卡、多指标组这三类组件原先只有「造数」，
没有「改」的口径 —— 模型说「把这段文字改成 Markdown」「把这个指标改名」
「多指标组排成两列」，落不到定义上。

这里把**一项能力 = 一条记录**收在一起：参数说明、取值域、校验与执行实现
只写一次，模型工具与规则快路共用同一份。

**协议是唯一依据**（``report-definition-v1.md``）：

* ``TextComponent.content`` 是**对象** ``{format, template, bindings}`` ——
  不是一段裸字符串；``headingLevel`` 是**目录层级**，不是字号；
* ``MetricItem`` = ``{id, label, outputRef, fieldRef, format,
  comparisonFieldRef, iconRef}``；``format`` 是**必填**；
* ``MetricGroupComponent.metrics`` 是**一个区域里的指标项列表** ——
  **不是**容器、**没有** children（协议里只有 CONTAINER / REPEATER 能有
  children）；
* ``ContainerArrangement`` = ``{mode, columns, gap, direction}``。

**几条硬纪律**：

1. **先校验、后落笔**：任何一项不合法就**整体拒绝**，原定义一个字节都不改；
2. **只改该改的**：改指标名**不碰**数值、字段绑定、单位与格式；
   改指标组只改**展示项**列表，不删任何源数据 / 输出 / 列 / 区块；
3. **不造数、不猜值**：字段必须已登记或属于模拟字段目录；图标只认注册清单；
4. **单位一致才敢比**：对比字段与指标字段的单位不一致 → 明确拒绝，
   **不静默换标签**。
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.services.display_binding_service import (
    DisplayBindingError,
    require_declared_field,
    resolve_icon_ref,
    simulated_field,
)
from app.services.edit_capabilities.base import (
    CapabilityError,
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


# --------------------------------------------------------------------------
# 契约事实（取值域只在这里定义一次）
# --------------------------------------------------------------------------

#: 文本的受控格式（协议 ``TextTemplate.format``）。
TEXT_FORMATS: tuple[str, ...] = ("PLAIN", "MARKDOWN")

#: 占位符名（协议 ``{{bindingName}}``）：必须是合法的标识符。
BINDING_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 模板里占位符的写法（允许占位符两侧有空格）。
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

#: **固定替换**的两个占位符：不靠 bindings 绑定，也不算「未绑定」。
#:
#: 它们是既有实现里一直有的固定替换（机房名 / 逻辑日期），**必须继续可用**。
FIXED_PLACEHOLDERS: tuple[str, ...] = ("roomName", "logicalDate")

#: 协议允许的绑定表达式类型（``TextTemplate.bindings`` 的取值域）。
BINDING_EXPR_TYPES: tuple[str, ...] = ("FIELD", "LITERAL", "CONTEXT", "PARAMETER", "CALL")

#: 排列模式（协议 ``ContainerArrangement.mode``）。
#:
#: **ABSOLUTE 也在协议里** —— 项目展示服务那份白名单只列了 GRID / FLOW，
#: 这里按**协议**收下 ABSOLUTE 并如实存下（渲染侧回落成 GRID 行为）。
ARRANGEMENT_MODES: tuple[str, ...] = ("GRID", "FLOW", "ABSOLUTE")

#: 排列方向（协议 ``ContainerArrangement.direction``）。
ARRANGEMENT_DIRECTIONS: tuple[str, ...] = ("VERTICAL", "HORIZONTAL")

#: 排列参数的键（协议里就这几个，别的一律拒绝）。
ARRANGEMENT_KEYS: tuple[str, ...] = ("mode", "columns", "direction", "gap")

#: 长度单位（像素 / 百分比）。
LENGTH_UNITS: tuple[str, ...] = ("PX", "PERCENT")

#: 网格列数范围。
ARRANGEMENT_COLUMNS_MIN = 1
ARRANGEMENT_COLUMNS_MAX = 12

#: 间距范围。
ARRANGEMENT_GAP_MIN = 0
ARRANGEMENT_GAP_MAX = 96

#: 指标组没声明排列时的兜底（与展示服务同一份默认口径）。
DEFAULT_ARRANGEMENT: dict[str, Any] = {
    "mode": "GRID",
    "columns": 2,
    "direction": "HORIZONTAL",
    "gap": {"value": 12, "unit": "PX"},
}

#: 指标项没声明格式时的兜底（协议里 ``format`` 是必填）。
DEFAULT_METRIC_FORMAT: dict[str, Any] = {"kind": "NUMBER"}


# --------------------------------------------------------------------------
# 参数契约
# --------------------------------------------------------------------------

class SetTextContentParams(BaseModel):
    """文本正文设置（模板 / 格式 / 目录层级 / 占位符绑定）。"""

    template: str = Field(
        default="",
        description="正文文本（空 = 不改）；里面的 {{占位符}} 按 bindings 取值",
    )
    clearTemplate: bool = Field(
        default=False,
        description="是否清空正文（true = 清空后再应用 template）",
    )
    format: str = Field(
        default="",
        description="文本格式：PLAIN（纯文本，保留换行）或 MARKDOWN（受控 Markdown）",
    )
    headingLevel: int | None = Field(
        default=None,
        description="目录层级（0 起的整数；不填 = 不改）—— 不是字号",
    )
    clearHeadingLevel: bool = Field(
        default=False,
        description="是否清空目录层级",
    )
    bindings: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "占位符 → 表达式：键是占位符名，值形如 "
            '{"type": "FIELD", "fieldRef": "<字段名>"}；'
            "给的项按名字合并进去，没给的保持原样。"
        ),
    )
    clearBindings: bool = Field(
        default=False,
        description="是否先清空全部占位符绑定（true = 清空后再应用 bindings）",
    )

    @field_validator("format", mode="before")
    @classmethod
    def _format_text(cls, value: Any) -> str:
        return str(value or "").strip().upper()

    @field_validator("template", mode="before")
    @classmethod
    def _template_text(cls, value: Any) -> str:
        return "" if value is None else str(value)

    @field_validator("bindings", mode="before")
    @classmethod
    def _bindings_map(cls, value: Any) -> dict[str, dict[str, Any]]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            return value  # 交给 pydantic 报错（形状不对就说清楚）
        return value


class SetMetricItemParams(BaseModel):
    """单指标卡设置（标签 / 字段 / 输出 / 格式 / 对比字段 / 图标）。"""

    label: str = Field(
        default="",
        description="指标名称（空 = 不改）；改名字不碰数值、字段、单位与格式",
    )
    fieldRef: str = Field(
        default="",
        description="数值字段名（必须已登记或属于当前模拟字段目录；空 = 不改）",
    )
    outputRef: str = Field(
        default="",
        description="覆盖区域默认输出的引用（output:<name>，name 必须是本区域已登记的计算输出）",
    )
    clearOutputRef: bool = Field(
        default=False,
        description="是否清空输出覆盖（回到区域默认输出）",
    )
    format: dict[str, Any] = Field(
        default_factory=dict,
        description="指标的显示格式（空 = 不改）：kind / precision / unitLabel / thousandSeparator / dateTimePattern / nullText / roundingMode",
    )
    clearFormat: bool = Field(
        default=False,
        description="是否清空指标格式",
    )
    comparisonFieldRef: str = Field(
        default="",
        description="对比字段名（必须已登记；单位要与指标字段一致）",
    )
    clearComparisonFieldRef: bool = Field(
        default=False,
        description="是否清空对比字段",
    )
    iconRef: str = Field(
        default="",
        description="注册图标引用（只认注册清单里的键；不接受图片 URL）",
    )
    clearIconRef: bool = Field(
        default=False,
        description="是否清空图标",
    )

    @field_validator("label", "fieldRef", "outputRef", "comparisonFieldRef", "iconRef", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()


class SetMetricGroupParams(BaseModel):
    """多指标组设置（指标项清单 / 内部排版）。"""

    metrics: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "该指标组的**完整**指标项清单：每一项形如 "
            '{"id": "m_1", "label": "总耗电量", "fieldRef": "totalEnergyKwh", '
            '"format": {"kind": "NUMBER", "precision": 1}}；'
            "带 id 的项按 id 就地更新（id 必须已存在），不带 id 的是新项；"
            "清单里没有的项从展示清单里移除（**不删任何源数据 / 输出 / 列 / 区块**）。"
            "**不传这一项 = 只改组内排版，组里已有的指标项原样保留**"
            "（缺省不等于「清空」——清空会把用户没提的指标项悄悄抹掉）。"
        ),
    )
    arrangement: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "组内排版：{'mode': 'GRID'|'FLOW'|'ABSOLUTE', 'columns': 1..12, "
            "'direction': 'VERTICAL'|'HORIZONTAL', 'gap': {'value': 0..96, 'unit': 'PX'|'PERCENT'}}"
        ),
    )
    create: bool = Field(
        default=False,
        description="是否把目标区块改成多指标组（指标卡 → 指标组；正文为空的文本区块 → 指标组）",
    )

    @field_validator("arrangement", mode="before")
    @classmethod
    def _raw(cls, value: Any) -> Any:
        return {} if value is None else value


# --------------------------------------------------------------------------
# 区域工具
# --------------------------------------------------------------------------

def _component(region: dict[str, Any]) -> dict[str, Any]:
    component = region.get("component") if isinstance(region, dict) else None
    return component if isinstance(component, dict) else {}


def _region_title(region: dict[str, Any], region_id: str) -> str:
    return str(region.get("name") or region_id or "区块")


def _content_of(region: dict[str, Any]) -> dict[str, Any]:
    """正文内容（**永远是对象**）。

    旧定义里正文曾是一段裸字符串 —— 读的时候当作模板，**不擅自改原定义**。
    """
    content = _component(region).get("content")
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        return {"template": content}
    return {}


def _has_text_content(region: dict[str, Any]) -> bool:
    """这个文本区块**还有没有正文**（决定能不能被改成指标组）。"""
    content = _content_of(region)
    template = str(content.get("template") or "").strip()
    bindings = content.get("bindings")
    return bool(template) or bool(isinstance(bindings, dict) and bindings)


def _require_region(ctx: OperationContext, op: str) -> tuple[str, dict[str, Any]]:
    region_id = ctx.resolve_target(ctx.current_target)
    region = ctx.find_region(region_id)
    if region is None:
        raise CapabilityError(
            f"目标区块 {region_id!r} 不存在。",
            op=op,
            target=region_id,
            constraint="目标必须存在",
            reason="unknown_region",
        )
    return region_id, region


def _require_component_type(
    region: dict[str, Any],
    region_id: str,
    *,
    op: str,
    allowed: tuple[str, ...],
    what: str,
) -> str:
    """目标组件类型必须对得上（类型明确写成别的就拒绝）。"""
    component_type = str(_component(region).get("type") or "").strip().upper()
    if component_type not in allowed:
        raise CapabilityError(
            f"「{_region_title(region, region_id)}」是 "
            f"{component_type or '未声明类型'} 区块，不是{what}；"
            f"这项能力只能作用在 {' / '.join(allowed)} 区块上。",
            op=op,
            target=region_id,
            constraint=f"目标组件类型必须是 {' / '.join(allowed)}",
            reason="incompatible_component_type",
        )
    return component_type


def _require_field(
    region: dict[str, Any],
    field_ref: Any,
    *,
    what: str,
    op: str,
    target: str,
) -> str:
    """字段必须已登记（或属于模拟字段目录）—— 不按名字猜。"""
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


def _require_icon(icon_ref: str, *, op: str, target: str) -> str:
    """图标必须是注册清单里的引用（**不接受图片 URL**）。"""
    try:
        resolve_icon_ref(icon_ref)
    except DisplayBindingError as exc:
        raise CapabilityError(
            exc.message,
            op=op,
            target=target,
            constraint=exc.constraint or "图标必须是注册清单里的引用",
            reason=exc.reason,
        ) from exc
    return str(icon_ref).strip()


def _declared_unit(field_ref: str) -> str:
    """字段声明的单位（模拟字段目录里登记的那一份；没有就是空）。"""
    spec = simulated_field(str(field_ref or "").strip())
    return str((spec or {}).get("unit") or "").strip()


def _registered_outputs(region: dict[str, Any]) -> dict[str, Any]:
    data = region.get("data") if isinstance(region, dict) else None
    pipeline = (data or {}).get("pipeline") if isinstance(data, dict) else None
    outputs = (pipeline or {}).get("outputs") if isinstance(pipeline, dict) else None
    return outputs if isinstance(outputs, dict) else {}


def _require_output_ref(
    region: dict[str, Any],
    output_ref: str,
    *,
    op: str,
    target: str,
) -> str:
    """输出引用必须是本区域**已登记**的计算输出（``output:<name>``）。"""
    text = str(output_ref or "").strip()
    if not text.startswith("output:"):
        raise CapabilityError(
            f"输出引用 {text!r} 的写法不对：只支持 output:<name>。",
            op=op,
            target=target,
            constraint="输出引用必须写成 output:<name>",
            reason="invalid_output_ref",
        )
    name = text[len("output:") :].strip()
    outputs = _registered_outputs(region)
    if not name or name not in outputs:
        available = "、".join(sorted(outputs)) or "（无）"
        raise CapabilityError(
            f"输出引用 {text!r} 不存在：这个区域没有登记该计算输出；"
            f"当前已登记：{available}。",
            op=op,
            target=target,
            constraint="输出引用必须指向当前区域已登记的计算输出",
            reason="unknown_output_ref",
        )
    return text


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
        precision = _as_int(precision)
        if precision is None:
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
            # **等价写法照样认**："true" / "是" / 1 就是真的开启千分位。
            # 认不出来的仍然明确拒绝（不放水）。
            text = str(thousand).strip().lower()
            if text in {"true", "yes", "1", "是", "开", "on"}:
                fmt["thousandSeparator"] = True
                thousand = None
            elif text in {"false", "no", "0", "否", "关", "off"}:
                fmt["thousandSeparator"] = False
                thousand = None
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


def _validate_bindings(
    region: dict[str, Any],
    bindings: dict[str, Any],
    *,
    op: str,
    target: str,
) -> dict[str, Any]:
    """占位符 → 表达式：名字合法、字段已登记（**先校验、后落笔**）。"""
    validated: dict[str, Any] = {}
    for raw_name, raw_expr in (bindings or {}).items():
        name = str(raw_name or "").strip()
        if not BINDING_NAME_RE.match(name):
            raise CapabilityError(
                f"占位符名 {raw_name!r} 不合法：只能是字母或下划线开头的标识符"
                "（字母、数字、下划线）。",
                op=op,
                target=target,
                constraint="占位符名必须是合法标识符",
                reason="invalid_binding_name",
            )
        if not isinstance(raw_expr, dict):
            raise CapabilityError(
                f"占位符 {name!r} 的绑定必须是对象（形如 "
                '{"type": "FIELD", "fieldRef": "<字段名>"}）。',
                op=op,
                target=target,
                constraint="绑定必须是表达式对象",
                reason="invalid_binding",
            )
        expr_type = str(raw_expr.get("type") or "FIELD").strip().upper()
        if expr_type not in BINDING_EXPR_TYPES:
            raise CapabilityError(
                f"占位符 {name!r} 的绑定类型 {expr_type!r} 不支持；"
                f"可用：{'、'.join(BINDING_EXPR_TYPES)}。",
                op=op,
                target=target,
                constraint="绑定类型必须在协议取值域内",
                reason="invalid_binding_expr",
            )
        expr = dict(raw_expr)
        expr["type"] = expr_type
        if expr_type == "FIELD":
            field_ref = str(expr.get("fieldRef") or "").strip()
            if not field_ref:
                raise CapabilityError(
                    f"占位符 {name!r} 是字段绑定，但没有给出字段名（fieldRef）。",
                    op=op,
                    target=target,
                    constraint="字段绑定必须给出字段名",
                    reason="missing_field_ref",
                )
            expr["fieldRef"] = _require_field(
                region, field_ref, what="绑定字段", op=op, target=target
            )
        elif expr_type == "LITERAL" and "value" not in expr:
            raise CapabilityError(
                f"占位符 {name!r} 是常量绑定，但没有给出 value。",
                op=op,
                target=target,
                constraint="常量绑定必须给出 value",
                reason="invalid_binding",
            )
        elif expr_type == "CONTEXT" and not str(expr.get("path") or "").strip():
            raise CapabilityError(
                f"占位符 {name!r} 是上下文绑定，但没有给出 path。",
                op=op,
                target=target,
                constraint="上下文绑定必须给出 path",
                reason="invalid_binding",
            )
        validated[name] = expr
    return validated


def _unbound_placeholders(template: str, bindings: dict[str, Any]) -> list[str]:
    """模板里**没有绑定**的占位符（固定替换的两个不算）。

    这些占位符**不报错** —— 既有实现里它们照样显示原样；
    这里只是**说清楚**，让模型知道哪些占位符没有绑定。
    """
    names: list[str] = []
    for match in PLACEHOLDER_RE.finditer(str(template or "")):
        name = match.group(1)
        if name in FIXED_PLACEHOLDERS or name in bindings:
            continue
        if name not in names:
            names.append(name)
    return names


def _as_int(value: Any) -> int | None:
    """**整数的等价写法**收成整数；不是整数就返回 ``None``。

    模型常把数字写成字符串（``"2"`` / ``" 16 "``）。那是**同一个值**的
    另一种写法，不是别的值 —— 收下它，别因为一个引号把整轮修改判死。
    真不是整数（``"两列"``、``1.5``、布尔）就返回 ``None``，由调用方按
    契约明确拒绝。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                number = float(text)
            except ValueError:
                return None
            return int(number) if number.is_integer() else None
    return None


def _validate_arrangement(
    value: Any, *, op: str, target: str
) -> dict[str, Any]:
    """组内排版：按**协议**校验（ABSOLUTE 也收下，如实存下）。"""
    if not isinstance(value, dict):
        raise CapabilityError(
            "排列参数必须是对象。",
            op=op,
            target=target,
            constraint="排列参数必须是对象",
            reason="invalid_arrangement",
        )
    for key in value:
        if key not in ARRANGEMENT_KEYS:
            raise CapabilityError(
                f"排列参数不支持 {key!r}；可用键：{'、'.join(ARRANGEMENT_KEYS)}。",
                op=op,
                target=target,
                constraint="排列参数只能用协议里那几个键",
                reason="invalid_arrangement",
            )
    mode = str(value.get("mode") or "GRID").strip().upper()
    if mode not in ARRANGEMENT_MODES:
        raise CapabilityError(
            f"排列模式 {mode!r} 不支持；可用：{'、'.join(ARRANGEMENT_MODES)}。",
            op=op,
            target=target,
            constraint="排列模式必须在协议取值域内",
            reason="invalid_arrangement",
        )
    columns = _as_int(value.get("columns", DEFAULT_ARRANGEMENT["columns"]))
    if columns is None:
        raise CapabilityError(
            "排列参数 columns 必须是整数。",
            op=op,
            target=target,
            constraint="columns 必须是整数",
            reason="invalid_arrangement",
        )
    if not (ARRANGEMENT_COLUMNS_MIN <= columns <= ARRANGEMENT_COLUMNS_MAX):
        raise CapabilityError(
            f"排列参数 columns 必须在 {ARRANGEMENT_COLUMNS_MIN} 到 "
            f"{ARRANGEMENT_COLUMNS_MAX} 之间。",
            op=op,
            target=target,
            constraint="columns 必须在支持范围内",
            reason="invalid_arrangement",
        )
    direction = str(value.get("direction") or DEFAULT_ARRANGEMENT["direction"]).strip().upper()
    if direction not in ARRANGEMENT_DIRECTIONS:
        raise CapabilityError(
            f"排列方向 {direction!r} 不支持；可用：{'、'.join(ARRANGEMENT_DIRECTIONS)}。",
            op=op,
            target=target,
            constraint="排列方向必须在协议取值域内",
            reason="invalid_arrangement",
        )
    raw_gap = value.get("gap", DEFAULT_ARRANGEMENT["gap"])
    if not isinstance(raw_gap, dict):
        raise CapabilityError(
            "排列参数 gap 必须是对象（形如 {'value': 12, 'unit': 'PX'}）。",
            op=op,
            target=target,
            constraint="gap 必须是长度对象",
            reason="invalid_arrangement",
        )
    for key in raw_gap:
        if key not in ("value", "unit"):
            raise CapabilityError(
                f"排列参数 gap 不支持 {key!r}；可用键：value、unit。",
                op=op,
                target=target,
                constraint="gap 只能用 value 与 unit",
                reason="invalid_arrangement",
            )
    unit = str(raw_gap.get("unit") or "PX").strip().upper()
    if unit not in LENGTH_UNITS:
        raise CapabilityError(
            f"排列参数 gap.unit {unit!r} 不支持；可用：{'、'.join(LENGTH_UNITS)}。",
            op=op,
            target=target,
            constraint="gap 单位必须在支持范围内",
            reason="invalid_arrangement",
        )
    gap_value = _as_int(raw_gap.get("value", DEFAULT_ARRANGEMENT["gap"]["value"]))
    if gap_value is None:
        raise CapabilityError(
            "排列参数 gap.value 必须是整数。",
            op=op,
            target=target,
            constraint="gap.value 必须是整数",
            reason="invalid_arrangement",
        )
    if not (ARRANGEMENT_GAP_MIN <= gap_value <= ARRANGEMENT_GAP_MAX):
        raise CapabilityError(
            f"排列参数 gap.value 必须在 {ARRANGEMENT_GAP_MIN} 到 "
            f"{ARRANGEMENT_GAP_MAX} 之间。",
            op=op,
            target=target,
            constraint="gap.value 必须在支持范围内",
            reason="invalid_arrangement",
        )
    return {
        "mode": mode,
        "columns": columns,
        "direction": direction,
        "gap": {"value": gap_value, "unit": unit},
    }


def _next_metric_id(used: set[str]) -> str:
    """新指标项的稳定 id（``m_<n>``，不与既有 id 撞号）。"""
    index = 1
    while f"m_{index}" in used:
        index += 1
    return f"m_{index}"


# --------------------------------------------------------------------------
# 执行实现
# --------------------------------------------------------------------------

def _set_text_content(
    ctx: OperationContext, params: SetTextContentParams
) -> CapabilityOutcome:
    """设置文本正文：模板 / 格式 / 目录层级 / 占位符绑定（T01–T03）。"""
    op = "SET_TEXT_CONTENT"
    region_id, region = _require_region(ctx, op)
    _require_component_type(region, region_id, op=op, allowed=("TEXT",), what="文本区块")

    content = dict(_content_of(region))

    # ---- 校验（先校验，后落笔） ----------------------------------------
    fmt = str(params.format or "").strip().upper()
    if fmt and fmt not in TEXT_FORMATS:
        raise CapabilityError(
            f"文本格式 {fmt!r} 不支持；可用：{'、'.join(TEXT_FORMATS)}。",
            op=op,
            target=region_id,
            constraint="文本格式必须在协议取值域内",
            reason="unsupported_text_format",
        )
    heading_level = params.headingLevel
    if heading_level is not None and heading_level < 0:
        raise CapabilityError(
            f"目录层级 {heading_level} 不合法：必须是 0 或正整数。",
            op=op,
            target=region_id,
            constraint="headingLevel 必须是 0 或正整数",
            reason="invalid_heading_level",
        )
    bindings = _validate_bindings(region, params.bindings, op=op, target=region_id)

    template = str(content.get("template") or "")
    if params.clearTemplate:
        template = ""
    elif params.template:
        template = params.template

    # ---- 落笔（只改正文与目录层级，**标题不动**） ----------------------
    new_content = dict(content)
    if params.clearTemplate:
        new_content["template"] = ""
    elif params.template:
        new_content["template"] = template
    if fmt:
        new_content["format"] = fmt
    if params.clearBindings:
        new_content["bindings"] = {}
    elif params.bindings:
        merged = dict(new_content.get("bindings") or {})
        merged.update(bindings)
        new_content["bindings"] = merged

    new_component = dict(_component(region))
    if params.clearTemplate or params.template or fmt or params.clearBindings or params.bindings:
        new_component["content"] = new_content
    elif "content" in _component(region):
        new_component["content"] = new_content
    if params.clearHeadingLevel:
        new_component["headingLevel"] = None
    elif heading_level is not None:
        new_component["headingLevel"] = heading_level
    region["component"] = new_component

    title = _region_title(region, region_id)
    effective_bindings = new_content.get("bindings") or {}
    unbound = _unbound_placeholders(template, effective_bindings)
    notes = []
    if unbound:
        notes.append(
            "模板里有未绑定的占位符（按原样显示）：" + "、".join(unbound)
        )
    message = (
        f"「{title}」的正文已更新"
        f"（格式 {str(new_content.get('format') or 'PLAIN').upper()}，"
        f"绑定 {len(effective_bindings)} 项）"
        + ("；" + "；".join(notes) if notes else "")
        + "（卡片标题不变）。"
    )
    detail = {
        "regionId": region_id,
        "title": title,
        "template": template,
        "format": str(new_content.get("format") or "PLAIN").upper(),
        "headingLevel": new_component.get("headingLevel"),
        "bindings": effective_bindings,
        "unboundPlaceholders": unbound,
        "notes": notes,
    }
    return CapabilityOutcome(
        ok=True,
        message=message,
        region_id=region_id,
        title=title,
        detail={**detail, **business_facts(message=message, detail=detail)},
    )


def _set_metric_item(
    ctx: OperationContext, params: SetMetricItemParams
) -> CapabilityOutcome:
    """设置单指标卡（标签 / 字段 / 输出 / 格式 / 对比字段 / 图标）（M01–M04）。"""
    op = "SET_METRIC_ITEM"
    region_id, region = _require_region(ctx, op)
    _require_component_type(
        region, region_id, op=op, allowed=("METRIC_CARD",), what="指标卡"
    )
    component = _component(region)
    metric = dict(component.get("metric") or {})

    # ---- 校验（先校验，后落笔） ----------------------------------------
    field_ref = str(params.fieldRef or "").strip()
    if field_ref:
        field_ref = _require_field(
            region, field_ref, what="指标字段", op=op, target=region_id
        )
    effective_field = field_ref or str(metric.get("fieldRef") or "").strip()
    primary_unit = _declared_unit(effective_field)

    comparison_ref = str(params.comparisonFieldRef or "").strip()
    if comparison_ref:
        comparison_ref = _require_field(
            region, comparison_ref, what="对比字段", op=op, target=region_id
        )
        comparison_unit = _declared_unit(comparison_ref)
        if primary_unit and comparison_unit and primary_unit != comparison_unit:
            raise CapabilityError(
                f"对比字段的单位是 {comparison_unit}，与指标字段的单位 {primary_unit} 不一致 —— "
                "不同单位的值不能直接对比。请改用同单位的对比字段。",
                op=op,
                target=region_id,
                constraint="对比字段的单位必须与指标字段一致",
                reason="incompatible_unit",
            )

    # 新字段没有登记单位时，**旧字段**的单位标签要摘掉（别串单位）。
    drop_stale_unit = False

    fmt: dict[str, Any] | None = None
    if params.format:
        fmt = _normalize_format(
            params.format, op=op, target=region_id, where="指标格式"
        )
        # **没给的键沿用既有值** —— 用户说「单位标签保持不变」时，模型常常
        # 只把要改的那几项传过来；静默丢掉单位标签会让单位串掉。
        old_format = metric.get("format") if isinstance(metric.get("format"), dict) else {}
        if not str(fmt.get("unitLabel") or "").strip():
            old_label = str(old_format.get("unitLabel") or "").strip()
            if field_ref and primary_unit:
                # 换了字段 → 用**新字段声明的单位**（不是旧标签）。
                fmt["unitLabel"] = primary_unit
            elif field_ref:
                # 换了字段，而新字段**没有登记单位** —— 旧标签是**旧字段**的
                # 单位，带过去就是「单位串掉」（验收 08-03）。
                # 宁可不显示单位，也不显示一个错的单位。
                pass
            elif old_label and (not primary_unit or old_label == primary_unit):
                fmt["unitLabel"] = old_label
        if not str(fmt.get("kind") or "").strip() and old_format.get("kind"):
            fmt["kind"] = str(old_format["kind"]).strip().upper()
        unit_label = str(fmt.get("unitLabel") or "").strip()
        if unit_label and primary_unit and unit_label != primary_unit:
            raise CapabilityError(
                f"指标格式里的单位标签是 {unit_label}，与指标字段声明的单位 "
                f"{primary_unit} 不一致 —— 不能悄悄换标签。请改成 {primary_unit} 或换一个字段。",
                op=op,
                target=region_id,
                constraint="单位标签必须与字段声明的单位一致",
                reason="incompatible_unit",
            )
    elif field_ref and not params.clearFormat:
        # 换了字段，却留着旧格式里的单位标签 —— **明确拒绝，不静默换标签**。
        # （给了新格式就按新格式算，这里只管「没给新格式」的情况。）
        old_label = str((metric.get("format") or {}).get("unitLabel") or "").strip()
        if old_label and primary_unit and old_label != primary_unit:
            raise CapabilityError(
                f"指标字段换成了 {field_ref}（单位 {primary_unit}），"
                f"但指标格式里的单位标签还是 {old_label} —— 不能悄悄换标签。"
                f"请把 unitLabel 改成 {primary_unit}，或换一个单位相同的字段。",
                op=op,
                target=region_id,
                constraint="单位标签必须与字段声明的单位一致",
                reason="incompatible_unit",
            )
        if old_label and not primary_unit and field_ref != str(
            metric.get("fieldRef") or ""
        ).strip():
            # 新字段没有登记单位：旧标签是**旧字段**的单位 —— 摘掉，别串单位。
            drop_stale_unit = True

    output_ref = str(params.outputRef or "").strip()
    if output_ref:
        output_ref = _require_output_ref(region, output_ref, op=op, target=region_id)

    icon_ref = str(params.iconRef or "").strip()
    if icon_ref:
        icon_ref = _require_icon(icon_ref, op=op, target=region_id)

    label = str(params.label or "").strip()

    # ---- 落笔（**只改给定的项**；改名字不碰数值 / 字段 / 单位 / 格式） --
    new_metric = dict(metric)
    metric_id = str(new_metric.get("id") or "").strip()
    if not metric_id:
        metric_id = _next_metric_id({str(metric.get("id") or "")})
    new_metric["id"] = metric_id
    if label:
        new_metric["label"] = label
    if field_ref:
        new_metric["fieldRef"] = field_ref
    if params.clearOutputRef:
        new_metric["outputRef"] = None
    elif output_ref:
        new_metric["outputRef"] = output_ref
    if params.clearFormat:
        new_metric.pop("format", None)
    elif fmt is not None:
        new_metric["format"] = fmt
    if drop_stale_unit and isinstance(new_metric.get("format"), dict):
        # 新字段没有登记单位：旧字段的单位标签摘掉（别串单位，验收 08-03）。
        new_metric["format"] = {
            key: value
            for key, value in new_metric["format"].items()
            if key != "unitLabel"
        }
    if params.clearComparisonFieldRef:
        new_metric["comparisonFieldRef"] = None
    elif comparison_ref:
        new_metric["comparisonFieldRef"] = comparison_ref
    if params.clearIconRef:
        new_metric["iconRef"] = None
    elif icon_ref:
        new_metric["iconRef"] = icon_ref

    new_component = dict(component)
    new_component["metric"] = new_metric
    region["component"] = new_component

    # 区块**原名**（说明里用它，用户能对上自己指的那块）。
    title = _region_title(region, region_id)

    # 单指标卡：区块里**只有这一个指标** —— 「指标名称」与「区块业务名称」
    # 说的是同一件事。不同步的话，右侧外层标题还停在旧指标含义上，
    # 用户看到的就是「改名没生效」「9 台 挂在耗电标题下」（验收 08-01/08-02）。
    # **多指标组不同步** —— 组名与组内项名是两件事（契约保留独立命名）。
    name_synced = False
    if label:
        old_region_name = str(region.get("name") or "").strip()
        if old_region_name != label:
            region["name"] = label
            name_synced = True

    # 「已改了什么」按**实际差异**算，不按「请求里写了什么」算 ——
    # 否则同一个名字改两次，第二次也说「已改：名称」，和「没有变化」自相矛盾。
    changed: list[str] = []
    if label and str(new_metric.get("label") or "") != str(metric.get("label") or ""):
        changed.append("名称")
    if field_ref and str(new_metric.get("fieldRef") or "") != str(metric.get("fieldRef") or ""):
        changed.append("字段")
    if output_ref or params.clearOutputRef:
        if new_metric.get("outputRef") != metric.get("outputRef"):
            changed.append("输出")
    if fmt is not None or params.clearFormat:
        if new_metric.get("format") != metric.get("format"):
            changed.append("格式")
    if comparison_ref or params.clearComparisonFieldRef:
        if new_metric.get("comparisonFieldRef") != metric.get("comparisonFieldRef"):
            changed.append("对比字段")
    if icon_ref or params.clearIconRef:
        if new_metric.get("iconRef") != metric.get("iconRef"):
            changed.append("图标")

    if changed:
        message = (
            f"「{title}」的指标已改：{'、'.join(changed)}"
            f"（数值、单位与口径不变）。"
        )
    elif label:
        message = (
            f"「{title}」的指标名称已经是「{label}」，"
            "本次没有实际改动（数值、单位与口径不变）。"
        )
    else:
        message = f"「{title}」的指标未变（数值、单位与口径不变）。"
    detail = {
        "regionId": region_id,
        "title": title,
        "metricId": metric_id,
        "label": new_metric.get("label"),
        "fieldRef": new_metric.get("fieldRef"),
        "outputRef": new_metric.get("outputRef"),
        "format": new_metric.get("format"),
        "comparisonFieldRef": new_metric.get("comparisonFieldRef"),
        "iconRef": new_metric.get("iconRef"),
        "unit": primary_unit,
        "changed": changed,
        "regionNameSynced": name_synced,
    }
    return CapabilityOutcome(
        ok=True,
        message=message,
        region_id=region_id,
        title=title,
        detail={**detail, **business_facts(message=message, detail=detail)},
    )


def _set_metric_group(
    ctx: OperationContext, params: SetMetricGroupParams
) -> CapabilityOutcome:
    """设置多指标组（指标项清单 / 组内排版）（MG01–MG03）。"""
    op = "SET_METRIC_GROUP"
    region_id, region = _require_region(ctx, op)
    component = _component(region)
    component_type = str(component.get("type") or "").strip().upper()
    create = bool(params.create)

    if create:
        if component_type not in ("METRIC_CARD", "TEXT"):
            raise CapabilityError(
                f"「{_region_title(region, region_id)}」是 {component_type or '未声明类型'} 区块，"
                "不能改成多指标组；只能改指标卡或正文为空的文本区块。",
                op=op,
                target=region_id,
                constraint="目标组件类型必须是 METRIC_CARD 或 TEXT",
                reason="incompatible_component_type",
            )
        if component_type == "TEXT" and _has_text_content(region):
            raise CapabilityError(
                f"「{_region_title(region, region_id)}」还有正文内容，不能直接改成多指标组；"
                "请先清空正文（或只改指标卡）。",
                op=op,
                target=region_id,
                constraint="文本区块必须先清空正文才能改成多指标组",
                reason="incompatible_component_type",
            )
    else:
        _require_component_type(
            region, region_id, op=op, allowed=("METRIC_GROUP",), what="多指标组"
        )

    # 既有指标项：指标组取 component.metrics；指标卡取那一张卡上的 metric。
    existing_items: list[dict[str, Any]] = []
    if component_type == "METRIC_GROUP":
        for item in component.get("metrics") or []:
            if isinstance(item, dict):
                existing_items.append(dict(item))
    elif component_type == "METRIC_CARD":
        card_metric = component.get("metric")
        if isinstance(card_metric, dict) and card_metric:
            existing_items.append(dict(card_metric))

    existing_by_id: dict[str, dict[str, Any]] = {}
    for item in existing_items:
        item_id = str(item.get("id") or "").strip()
        if item_id:
            existing_by_id[item_id] = item
    used_ids = set(existing_by_id) | {
        str(item.get("id") or "").strip()
        for item in existing_items
        if str(item.get("id") or "").strip()
    }

    # ---- 校验并组装新的指标项清单 --------------------------------------
    #
    # **没给 metrics = 只改排版，组里已有的指标项原样保留。**
    # 缺省不等于「清空」—— 模型只提交 arrangement（改列数 / 间距）时，
    # 把组里的指标项抹成 0 项，是把用户没提的东西悄悄抹掉
    # （现场实证：``draft_ca7be1809836`` rev4→rev5 三项变 0 项，
    # 而回复却说「组内指标均未改动」）。
    #
    # 给了 metrics = 那份**完整清单**说了算（清单里没有的项移除），
    # 这与能力说明里的「完整清单」语义一致。
    metrics_given = params.metrics is not None
    if metrics_given:
        raw_items: list[dict[str, Any]] = list(params.metrics or [])
    else:
        carry = [dict(item) for item in existing_items]
        if create:
            # 由指标卡 / 空文本区块**转成**组：只有那张卡自己已经有字段
            # 时才带过来 —— 占位空壳（没有 fieldRef）带过来必然校验不过。
            carry = [
                item for item in carry
                if str(item.get("fieldRef") or "").strip()
            ]
        raw_items = carry
    new_items: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise CapabilityError(
                f"第 {index + 1} 个指标项必须是对象。",
                op=op,
                target=region_id,
                constraint="指标项必须是对象",
                reason="invalid_metric_item",
            )
        given_id = str(raw.get("id") or "").strip()
        if given_id:
            if given_id not in existing_by_id:
                known = "、".join(sorted(existing_by_id)) or "（无）"
                raise CapabilityError(
                    f"第 {index + 1} 个指标项的 id {given_id!r} 不存在；"
                    f"该指标组当前的指标 id 是 {known}。"
                    "要新增指标项就别带 id。",
                    op=op,
                    target=region_id,
                    constraint="带 id 的指标项必须已经存在于该指标组",
                    reason="unknown_metric_id",
                )
            item = dict(existing_by_id[given_id])
            item["id"] = given_id
        else:
            item = {}
            item["id"] = _next_metric_id(used_ids)
        used_ids.add(item["id"])

        # 给了的键覆盖过去；没给的键保持原样（**就地更新**，不丢已有格式）。
        for key in ("label", "fieldRef", "outputRef", "comparisonFieldRef", "iconRef", "format"):
            if key in raw:
                item[key] = raw[key]

        label = str(item.get("label") or "").strip()
        if not label:
            raise CapabilityError(
                f"第 {index + 1} 个指标项没有名称（label）—— 协议里它是必填。",
                op=op,
                target=region_id,
                constraint="指标项必须有名称",
                reason="invalid_metric_item",
            )
        field_ref = str(item.get("fieldRef") or "").strip()
        if not field_ref:
            raise CapabilityError(
                f"第 {index + 1} 个指标项没有字段名（fieldRef）—— 协议里它是必填。",
                op=op,
                target=region_id,
                constraint="指标项必须有字段名",
                reason="invalid_metric_item",
            )
        field_ref = _require_field(
            region, field_ref, what="指标字段", op=op, target=region_id
        )
        primary_unit = _declared_unit(field_ref)

        comparison_ref = str(item.get("comparisonFieldRef") or "").strip()
        if comparison_ref:
            comparison_ref = _require_field(
                region, comparison_ref, what="对比字段", op=op, target=region_id
            )
            comparison_unit = _declared_unit(comparison_ref)
            if primary_unit and comparison_unit and primary_unit != comparison_unit:
                raise CapabilityError(
                    f"第 {index + 1} 个指标项：对比字段的单位是 {comparison_unit}，"
                    f"与指标字段的单位 {primary_unit} 不一致 —— 不同单位的值不能直接对比。",
                    op=op,
                    target=region_id,
                    constraint="对比字段的单位必须与指标字段一致",
                    reason="incompatible_unit",
                )

        fmt: dict[str, Any] | None = None
        if isinstance(item.get("format"), dict) and item.get("format"):
            fmt = _normalize_format(
                item.get("format"),
                op=op,
                target=region_id,
                where=f"第 {index + 1} 个指标项的格式",
            )
            unit_label = str(fmt.get("unitLabel") or "").strip()
            if unit_label and primary_unit and unit_label != primary_unit:
                raise CapabilityError(
                    f"第 {index + 1} 个指标项：格式里的单位标签是 {unit_label}，"
                    f"与字段声明的单位 {primary_unit} 不一致 —— 不能悄悄换标签。",
                    op=op,
                    target=region_id,
                    constraint="单位标签必须与字段声明的单位一致",
                    reason="incompatible_unit",
                )
        else:
            fmt = dict(DEFAULT_METRIC_FORMAT)

        output_ref = str(item.get("outputRef") or "").strip()
        if output_ref:
            output_ref = _require_output_ref(
                region, output_ref, op=op, target=region_id
            )

        icon_ref = str(item.get("iconRef") or "").strip()
        if icon_ref:
            icon_ref = _require_icon(icon_ref, op=op, target=region_id)

        new_items.append(
            {
                "id": item["id"],
                "label": label,
                "outputRef": output_ref or None,
                "fieldRef": field_ref,
                "format": fmt,
                "comparisonFieldRef": comparison_ref or None,
                "iconRef": icon_ref or None,
            }
        )

    # ---- 排列 ----------------------------------------------------------
    if params.arrangement:
        arrangement = _validate_arrangement(params.arrangement, op=op, target=region_id)
    elif create:
        arrangement = dict(DEFAULT_ARRANGEMENT)
    else:
        existing_arrangement = component.get("arrangement")
        arrangement = dict(existing_arrangement) if isinstance(existing_arrangement, dict) else {}

    # ---- 落笔（**只改展示清单**；源数据 / 输出 / 列 / 区块一个都不动） --
    old_title = str(component.get("title") or region.get("name") or "")
    if create:
        new_component = {
            key: value
            for key, value in component.items()
            if key not in ("type", "title", "metric", "metrics", "arrangement")
        }
        new_component["type"] = "METRIC_GROUP"
        new_component["title"] = old_title
    else:
        new_component = dict(component)
    new_component["metrics"] = new_items
    new_component["arrangement"] = arrangement
    region["component"] = new_component

    mode = str(arrangement.get("mode") or "").strip().upper()
    # ABSOLUTE 是协议里的合法取值，但渲染侧按 GRID 行为回落 —— 如实报出来。
    mode_applied = "GRID" if mode == "ABSOLUTE" else mode
    title = _region_title(region, region_id)
    message = (
        f"「{title}」已设为多指标组：{len(new_items)} 个指标项"
        f"（{'、'.join(str(item['id']) for item in new_items) or '（无）'}）"
        + (f"，排版 {mode}" if mode else "")
        + ("；未给指标项清单，组内原有指标项原样保留" if not metrics_given else "")
        + "（只改展示清单，源数据与输出不变）。"
    )
    detail = {
        "regionId": region_id,
        "title": title,
        "created": create,
        "metricsGiven": metrics_given,
        "metricIds": [str(item["id"]) for item in new_items],
        "metrics": new_items,
        "arrangement": arrangement,
        "arrangementMode": mode,
        "arrangementModeApplied": mode_applied,
    }
    return CapabilityOutcome(
        ok=True,
        message=message,
        region_id=region_id,
        title=title,
        detail={**detail, **business_facts(message=message, detail=detail)},
    )


# --------------------------------------------------------------------------
# 注册
# --------------------------------------------------------------------------

register(
    EditCapability(
        id="SET_TEXT_CONTENT",
        family=FAMILY_STAT,
        title="设置文本正文",
        description=(
            "设置文本区块的正文：template（正文文本）、format（PLAIN / MARKDOWN）、"
            "headingLevel（目录层级，不是字号）、bindings（占位符 → 表达式，"
            '形如 {"type": "FIELD", "fieldRef": "<字段名>"}）。'
            "**卡片标题与正文互不相干** —— 这里只改正文。"
            "模板里没有绑定的占位符不会报错，但会列在结果里；"
            "{{roomName}} 与 {{logicalDate}} 仍按固定替换生效。"
        ),
        params_schema=SetTextContentParams,
        executor=_set_text_content,
    )
)


register(
    EditCapability(
        id="SET_METRIC_ITEM",
        family=FAMILY_STAT,
        title="设置指标卡",
        description=(
            "设置单指标卡：label（指标名称）、fieldRef（数值字段）、"
            "outputRef（覆盖区域默认输出，必须是本区域已登记的 output:<name>）、"
            "format（指标级显示格式）、comparisonFieldRef（对比字段）、"
            "iconRef（注册图标，只认注册清单里的键）。"
            "**改名称不碰数值、字段绑定、单位与格式。**"
            "对比字段与指标字段的单位必须一致，单位标签不许悄悄改。"
            "**指标卡 / 指标组的数字显示格式（精度、千分位、单位标签、空值文本）"
            "一律用本能力**：它写在指标项自己的 format 上；区域级的 "
            "SET_FORMAT 是整块卡片的格式，不是指标项的格式 —— "
            "用它改指标卡会连带改掉这张卡上别的指标的单位。"
        ),
        params_schema=SetMetricItemParams,
        executor=_set_metric_item,
    )
)


register(
    EditCapability(
        id="SET_METRIC_GROUP",
        family=FAMILY_STAT,
        title="设置多指标组",
        description=(
            "设置多指标组：metrics 是该组的**完整**指标项清单"
            "（每项形如 {'id': 'm_1', 'label': '总耗电量', 'fieldRef': 'totalEnergyKwh', "
            "'format': {'kind': 'NUMBER'}}）；带 id 的项按 id 就地更新，"
            "不带 id 的是新项，清单里没有的项从展示清单里移除"
            "（**不删任何源数据 / 输出 / 列 / 区块**）。"
            "**不传 metrics = 只改排版，组里已有的指标项原样保留** —— "
            "只改列数 / 间距这类要求不要顺手把指标项清成 0 项。"
            "arrangement 设组内排版（mode / columns / direction / gap）。"
            "create=true 可把指标卡（或正文为空的文本区块）改成多指标组。"
        ),
        params_schema=SetMetricGroupParams,
        executor=_set_metric_group,
    )
)
