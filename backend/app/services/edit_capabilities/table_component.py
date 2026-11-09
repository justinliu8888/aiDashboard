"""表格组件能力包：列的增删改与排序、表格展示设置、汇总引用（TB01–TB07）。

**为什么有这一层**：表格的七项展示能力原先散落在提示词、前端渲染与
生成器里，各写一份口径 —— 同一次「加一列」在不同地方会被做成三件事
（改字段、改标签、把预览列留着）。这里把**一项能力 = 一条记录**收在
一起：参数说明、取值域、校验与执行实现只写一次。

**协议是唯一依据**（``report-definition-v1.md`` 的 ``TableComponent`` /
``TableColumn`` / ``TableSummary`` / ``Length`` / ``StyleSpec`` /
``FormatSpec``）：

* ``columns``       —— 列的**完整清单**（顺序即显示顺序），每列有**稳定 id**；
* ``label`` 与 ``fieldRef`` **互相独立** —— 改标题不改取值，改取值不改标题；
* ``width`` / ``style`` / ``headerPath`` —— 列宽、列样式、多层表头路径；
* ``groupRowsBy``   —— **只是展示分组**，不产生任何聚合、小计或透视；
* ``rowNumber`` / ``pageSize`` / ``frozenColumnCount`` —— 序号、网页分页、冻结列；
* ``summaries``     —— **引用已登记的汇总输出**（不新算、不临时求和）。

所有执行实现都遵守同一条纪律：**先校验、后落笔** —— 校验没过就抛
:class:`CapabilityError`（带中文说明、人话 constraint、机器 reason），
**绝不改了一半再报错**。

**这一层不做什么**：

* 不删源字段、不删输出、不删指标 —— 少一列只是**不显示**它；
* 不限制取数、不限制导出 —— ``pageSize`` 只是**网页展示**的分页；
* 不算分组小计 —— ``groupRowsBy`` 只影响前端怎么排版。
"""
from __future__ import annotations

import ast
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.display_binding_service import (
    DisplayBindingError,
    require_declared_field,
    resolve_summary_output,
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
from app.services.report_theme_service import STYLE_SPEC_KEYS


# --------------------------------------------------------------------------
# 契约事实（取值域只在这里定义一次）
# --------------------------------------------------------------------------

#: 表格组件类型（协议 ``TableComponent.type``）。
TABLE_COMPONENT_TYPE = "TABLE"

#: 列宽的取值范围（本批）。
COLUMN_WIDTH_MIN = 0
COLUMN_WIDTH_MAX = 2000

#: 列宽的单位（与项目现有的长度单位一致：像素 / 百分比）。
COLUMN_WIDTH_UNITS = ("PX", "PERCENT")

#: 汇总口径（协议 ``TableSummary.basis``）。
SUMMARY_BASES = ("FULL_RESULT", "VISIBLE_ROWS")

#: 汇总口径的中文说法（前端与消息共用同一份）。
SUMMARY_BASIS_LABELS: dict[str, str] = {
    "FULL_RESULT": "全量口径",
    "VISIBLE_ROWS": "可见行口径",
}

#: 新列 id 的前缀（稳定、可读、不与既有 id 撞）。
NEW_COLUMN_ID_PREFIX = "col_"

#: 样式里那些「长度类」的键（项目里统一是 ``{"value", "unit"}``）。
_STYLE_LENGTH_KEYS = ("fontSize", "lineHeight", "padding", "margin")

#: 边框允许的键（协议 ``Border``）。
_BORDER_KEYS = ("width", "style", "color")

#: 边框线型（与展示服务同一份取值域）。
_BORDER_STYLES = ("SOLID", "DASHED", "DOTTED", "NONE")

#: 水平对齐（项目取值域 + 协议里的 JUSTIFY）。
_TEXT_ALIGNS = ("LEFT", "CENTER", "RIGHT", "JUSTIFY")

#: 垂直对齐。
_VERTICAL_ALIGNS = ("TOP", "MIDDLE", "BOTTOM")

#: 字重：协议的 NORMAL / MEDIUM / BOLD，以及项目里的百分数字重。
_FONT_WEIGHT_NAMES = ("NORMAL", "MEDIUM", "BOLD")

#: 字号范围（像素）。
_FONT_SIZE_MIN = 1
_FONT_SIZE_MAX = 200

#: 行高范围。
_LINE_HEIGHT_MIN = 0
_LINE_HEIGHT_MAX = 400

#: 内边距 / 外边距范围（外边距可为负）。
_PADDING_MIN = 0
_PADDING_MAX = 400
_MARGIN_MIN = -200
_MARGIN_MAX = 400

#: 边框宽度范围。
_BORDER_WIDTH_MIN = 0
_BORDER_WIDTH_MAX = 20

#: 圆角范围。
_BORDER_RADIUS_MIN = 0
_BORDER_RADIUS_MAX = 200

#: 透明度范围。
_OPACITY_MIN = 0
_OPACITY_MAX = 1

#: 明显不是样式值的东西：脚本、任意 CSS 文本、外链。
#:
#: 与展示服务同一份判定（样式值会进 inline style，必须挡住注入）。
_UNSAFE_VALUE_PATTERN = re.compile(
    r"(<\s*/?)|(>)"
    r"|(javascript\s*:)|(expression\s*\()|(url\s*\()|(data\s*:\s*text/html)",
    re.IGNORECASE,
)

#: 一段样式里同时出现 ``{`` 与 ``}`` = 拼接写法（不是单个取值）。
_BRACE_CONCAT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


# --------------------------------------------------------------------------
# 参数契约
# --------------------------------------------------------------------------

class TableColumnParam(BaseModel):
    """一列。

    ``id``      给了就是**改这一列**（id 必须已存在）；不给就是**新增一列**。
    ``label``   列标题（与 ``fieldRef`` **互相独立**）。
    ``fieldRef`` 取值字段（必须是当前已登记的字段）。
    ``format``  这一列的显示格式（只认项目现有的一套键）。
    ``width``   列宽 ``{"value": 120, "unit": "PX"}``。
    ``style``   列样式（只认项目现有的外观键）。
    ``headerPath`` 多层表头路径（每一层一段表头文字；``[]`` 表示清空）。

    **没给的项保持原样**：不给 ``format`` 就沿用这一列原来的格式，
    不给 ``headerPath`` 就沿用原来的表头路径。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default="", description="列的稳定 id（改已有列时给，新增列不给）")
    label: str = Field(default="", description="列标题（与取值字段互相独立）")
    fieldRef: str = Field(default="", description="取值字段名（必须已登记）")
    format: dict[str, Any] | None = Field(
        default=None, description="这一列的显示格式（可选；不给则沿用原格式）"
    )
    width: dict[str, Any] | None = Field(
        default=None, description='列宽 {"value": 120, "unit": "PX"}（可选）'
    )
    style: dict[str, Any] | None = Field(
        default=None, description="这一列的样式（可选；只认受控的外观键）"
    )
    headerPath: list[str] | None = Field(
        default=None,
        description="多层表头路径（可选；每一层一段表头文字；[] 表示清空）",
    )

    @field_validator("id", "label", "fieldRef", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("headerPath", mode="before")
    @classmethod
    def _path(cls, value: Any) -> list[str] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            return [str(value)]
        # 每一层可以是**一段文字**，也可以是 ``{"label": "能耗"}`` 这样的
        # **对象**（模型照着协议里表头节点的形状给的）。对象取其中的文字
        # （label / text / title / name）。
        #
        # 不做这一步的实测后果：``[{"label": "能耗"}]`` 被 ``str(item)``
        # 写成 ``["{'label': '能耗'}"]`` —— 协议要求的字符串数组里多出
        # 一段 Python 字面量，前端把它当表头文字直接显示出来。
        return [_header_level_text(item) for item in value]


class TableSummaryParam(BaseModel):
    """一条汇总引用（**引用已登记的汇总输出**，不新算）。"""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, description="汇总标签")
    outputRef: str = Field(default="", description="已登记的计算输出（output:<name>）")
    fieldRef: str = Field(default="", description="汇总字段名")
    basis: str = Field(
        default="",
        description="汇总口径（FULL_RESULT 全量口径 / VISIBLE_ROWS 可见行口径）",
    )

    @field_validator("label", "outputRef", "fieldRef", "basis", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("basis", mode="before")
    @classmethod
    def _basis_upper(cls, value: Any) -> str:
        return str(value or "").strip().upper()


class SetTableColumnsParams(BaseModel):
    """设置表格的**列清单**（完整清单：给了就是全部）。"""

    columns: list[TableColumnParam] = Field(
        default_factory=list,
        description=(
            "完整的列清单（顺序即显示顺序）：每一项形如 "
            '{"id": "col_1", "label": "机房名称", "fieldRef": "roomName", '
            '"format": {"kind": "TEXT"}, "width": {"value": 120, "unit": "PX"}, '
            '"style": {"textAlign": "LEFT"}, "headerPath": ["能耗", "总量"]}；'
            "给了 id 就是改这一列（id 必须已存在），不给 id 就是新增一列。"
            "清单里没有的列**不再显示**（源字段、输出与指标一个都不删）。"
        ),
    )


class SetTableDisplayParams(BaseModel):
    """设置表格的**展示方式**（分组 / 序号 / 分页 / 冻结列）。"""

    groupRowsBy: list[str] = Field(
        default_factory=list,
        description=(
            "按哪些字段分组显示行（只是展示分组，**不产生小计或聚合**）；"
            "清空用 clearGroupRowsBy"
        ),
    )
    clearGroupRowsBy: bool = Field(
        default=False, description="是否清空行分组字段"
    )
    rowNumber: bool | None = Field(
        default=None, description="是否显示序号列（不填 = 不改）"
    )
    pageSize: int | None = Field(
        default=None,
        description="网页展示每页行数（只是**展示**分页，不限制取数与导出）",
    )
    clearPageSize: bool = Field(
        default=False, description="是否清掉每页行数（恢复成不分页）"
    )
    frozenColumnCount: int | None = Field(
        default=None, description="冻结前几列（不填 = 不改）"
    )
    clearFrozenColumnCount: bool = Field(
        default=False, description="是否清掉冻结列设置"
    )

    @field_validator("groupRowsBy", mode="before")
    @classmethod
    def _fields(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            return [str(value)]
        return [str(item or "").strip() for item in value]


class SetTableSummariesParams(BaseModel):
    """设置表格的**汇总引用**（完整清单：给了就是全部）。"""

    summaries: list[TableSummaryParam] = Field(
        default_factory=list,
        description=(
            "完整的汇总清单：每一项形如 "
            '{"label": "告警总数", "outputRef": "output:alarms", '
            '"fieldRef": "totalAlarmCount", "basis": "FULL_RESULT"}；'
            "outputRef 必须是这个区域**已登记**的计算输出，"
            "basis 只能是 FULL_RESULT（全量口径）或 VISIBLE_ROWS（可见行口径）。"
        ),
    )
    clearSummaries: bool = Field(
        default=False, description="是否清空全部汇总引用"
    )


# --------------------------------------------------------------------------
# 区域工具
# --------------------------------------------------------------------------

def _component(region: dict[str, Any]) -> dict[str, Any]:
    component = region.get("component") if isinstance(region, dict) else None
    return component if isinstance(component, dict) else {}


def _region_title(region: dict[str, Any], region_id: str) -> str:
    return str(region.get("name") or region_id or "区块")


def _require_table_region(
    region: dict[str, Any], region_id: str, *, op: str
) -> dict[str, Any]:
    """目标必须是**表格**区块（类型不是 TABLE 就拒绝）。"""
    component = _component(region)
    component_type = str(component.get("type") or "").strip().upper()
    if component_type != TABLE_COMPONENT_TYPE:
        shown = component_type or "未声明类型"
        raise CapabilityError(
            f"「{_region_title(region, region_id)}」是 {shown} 区块，不是表格；"
            "表格能力只能作用在 TABLE 区块上。",
            op=op,
            target=region_id,
            constraint="表格能力只能作用在 TABLE 区块上",
            reason="incompatible_component_type",
        )
    return component


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


def _registered_output_names(region: dict[str, Any]) -> list[str]:
    """这个区域**已登记**的计算输出名（不编）。"""
    data = region.get("data") if isinstance(region, dict) else None
    pipeline = (data or {}).get("pipeline") if isinstance(data, dict) else None
    outputs = (pipeline or {}).get("outputs") if isinstance(pipeline, dict) else None
    return list(outputs.keys()) if isinstance(outputs, dict) else []


def _next_column_id(used: set[str]) -> str:
    """一个新的、**不与既有 id 撞**的列 id。"""
    index = 1
    while f"{NEW_COLUMN_ID_PREFIX}{index}" in used:
        index += 1
    return f"{NEW_COLUMN_ID_PREFIX}{index}"


#: 列对象里**协议规定**的键（比较两列时只看这些）。
_PROTOCOL_COLUMN_KEYS = ("id", "label", "fieldRef", "format", "width", "style", "headerPath")


def _protocol_keys(column: dict[str, Any]) -> dict[str, Any]:
    """一列的协议键（别的键不算 —— 定义里可能还带着别的来源信息）。"""
    return {key: column.get(key) for key in _PROTOCOL_COLUMN_KEYS}


# --------------------------------------------------------------------------
# 取值规范化（格式 / 宽度 / 样式 / 表头路径）
# --------------------------------------------------------------------------

def _reject_unsafe_value(value: Any) -> None:
    """样式值里出现脚本 / 任意 CSS 文本 / 外链 → 拒绝。"""
    if isinstance(value, str):
        if _UNSAFE_VALUE_PATTERN.search(value):
            raise ValueError("样式值里有不允许的内容（脚本 / 任意 CSS 文本）")
        if "{" in value and "}" in value and _BRACE_CONCAT_PATTERN.search(value):
            raise ValueError("样式值里有不允许的内容（拼接写法）")
        return
    if isinstance(value, dict):
        for item in value.values():
            _reject_unsafe_value(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _reject_unsafe_value(item)


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


def _normalize_width(
    value: Any, *, op: str, target: str, where: str
) -> dict[str, Any]:
    """列宽：``{"value": 120, "unit": "PX"}``（像素 / 百分比）。"""
    if not isinstance(value, dict):
        raise CapabilityError(
            f"{where} 的列宽格式不对：{'、'.join(COLUMN_WIDTH_UNITS)}。",
            op=op,
            target=target,
            constraint="列宽必须是对象（value + unit）",
            reason="invalid_width",
        )
    unit = str(value.get("unit") or "PX").strip().upper()
    if unit not in COLUMN_WIDTH_UNITS:
        raise CapabilityError(
            f"{where} 的列宽单位 {unit!r} 不支持；可用：{'、'.join(COLUMN_WIDTH_UNITS)}。",
            op=op,
            target=target,
            constraint="列宽单位只能是 PX 或 PERCENT",
            reason="invalid_width",
        )
    raw = value.get("value")
    raw = as_number(raw)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CapabilityError(
            f"{where} 的列宽 value 必须是数字。",
            op=op,
            target=target,
            constraint="列宽 value 必须是数字",
            reason="invalid_width",
        )
    number = int(raw)
    if number < COLUMN_WIDTH_MIN or number > COLUMN_WIDTH_MAX:
        raise CapabilityError(
            f"{where} 的列宽 {number} 超出范围（{COLUMN_WIDTH_MIN} 到 {COLUMN_WIDTH_MAX}）。",
            op=op,
            target=target,
            constraint="列宽必须在支持范围内",
            reason="invalid_width",
        )
    return {"value": number, "unit": unit}


def _style_length(
    value: Any, *, op: str, target: str, where: str, low: int, high: int
) -> dict[str, Any]:
    """样式里的长度：``{"value", "unit"}``（也接受裸数字 = 像素）。"""
    if isinstance(value, dict):
        unit = str(value.get("unit") or "PX").strip().upper()
        raw = value.get("value")
    else:
        unit = "PX"
        raw = value
    if unit not in ("PX", "PERCENT"):
        raise CapabilityError(
            f"{where} 的长度单位 {unit!r} 不支持；可用：PX、PERCENT。",
            op=op,
            target=target,
            constraint="长度单位只能是 PX 或 PERCENT",
            reason="invalid_style",
        )
    raw = as_number(raw)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CapabilityError(
            f"{where} 的长度值必须是数字。",
            op=op,
            target=target,
            constraint="长度值必须是数字",
            reason="invalid_style",
        )
    number = int(raw)
    if number < low or number > high:
        raise CapabilityError(
            f"{where} 的长度 {number} 超出范围（{low} 到 {high}）。",
            op=op,
            target=target,
            constraint="长度必须在支持范围内",
            reason="invalid_style",
        )
    return {"value": number, "unit": unit}


def _normalize_style(
    value: Any, *, op: str, target: str, where: str
) -> dict[str, Any]:
    """列样式：只认项目**现有**的外观键（与展示服务同一份白名单）。"""
    if not isinstance(value, dict):
        raise CapabilityError(
            f"{where} 的样式格式不对（必须是对象）。",
            op=op,
            target=target,
            constraint="样式必须是对象",
            reason="invalid_style",
        )
    for key in value:
        if key not in STYLE_SPEC_KEYS:
            raise CapabilityError(
                f"{where} 的样式不支持 {key!r}；可用：{'、'.join(STYLE_SPEC_KEYS)}。",
                op=op,
                target=target,
                constraint="样式只能用项目现有的一套键",
                reason="invalid_style",
            )
    try:
        _reject_unsafe_value(value)
    except ValueError as exc:
        raise CapabilityError(
            f"{where} 的样式值不合法：{exc}。",
            op=op,
            target=target,
            constraint="样式值里不能有脚本 / 任意 CSS 文本",
            reason="invalid_style",
        ) from exc

    style: dict[str, Any] = {}
    if value.get("fontFamily") is not None:
        style["fontFamily"] = str(value["fontFamily"])[:200]
    if value.get("fontSize") is not None:
        style["fontSize"] = _style_length(
            value["fontSize"],
            op=op,
            target=target,
            where=f"{where} 的 fontSize",
            low=_FONT_SIZE_MIN,
            high=_FONT_SIZE_MAX,
        )
    if value.get("fontWeight") is not None:
        weight = value["fontWeight"]
        if isinstance(weight, bool):
            raise CapabilityError(
                f"{where} 的 fontWeight 取值不对。",
                op=op,
                target=target,
                constraint="fontWeight 必须是数字或 NORMAL / MEDIUM / BOLD",
                reason="invalid_style",
            )
        if isinstance(weight, int):
            style["fontWeight"] = int(weight)
        else:
            text = str(weight).strip().upper()
            if text not in _FONT_WEIGHT_NAMES:
                raise CapabilityError(
                    f"{where} 的 fontWeight {text!r} 不支持；"
                    f"可用：{'、'.join(_FONT_WEIGHT_NAMES)}。",
                    op=op,
                    target=target,
                    constraint="fontWeight 必须是数字或 NORMAL / MEDIUM / BOLD",
                    reason="invalid_style",
                )
            style["fontWeight"] = text
    for key in ("textColor", "backgroundColor"):
        if value.get(key) is not None:
            style[key] = str(value[key])[:64]
    if value.get("textAlign") is not None:
        text = str(value["textAlign"]).strip().upper()
        if text not in _TEXT_ALIGNS:
            raise CapabilityError(
                f"{where} 的 textAlign {text!r} 不支持；可用：{'、'.join(_TEXT_ALIGNS)}。",
                op=op,
                target=target,
                constraint="textAlign 必须在支持范围内",
                reason="invalid_style",
            )
        style["textAlign"] = text
    if value.get("verticalAlign") is not None:
        text = str(value["verticalAlign"]).strip().upper()
        if text not in _VERTICAL_ALIGNS:
            raise CapabilityError(
                f"{where} 的 verticalAlign {text!r} 不支持；"
                f"可用：{'、'.join(_VERTICAL_ALIGNS)}。",
                op=op,
                target=target,
                constraint="verticalAlign 必须在支持范围内",
                reason="invalid_style",
            )
        style["verticalAlign"] = text
    if value.get("lineHeight") is not None:
        style["lineHeight"] = _style_length(
            value["lineHeight"],
            op=op,
            target=target,
            where=f"{where} 的 lineHeight",
            low=_LINE_HEIGHT_MIN,
            high=_LINE_HEIGHT_MAX,
        )
    if value.get("padding") is not None:
        style["padding"] = _style_length(
            value["padding"],
            op=op,
            target=target,
            where=f"{where} 的 padding",
            low=_PADDING_MIN,
            high=_PADDING_MAX,
        )
    if value.get("margin") is not None:
        style["margin"] = _style_length(
            value["margin"],
            op=op,
            target=target,
            where=f"{where} 的 margin",
            low=_MARGIN_MIN,
            high=_MARGIN_MAX,
        )
    if value.get("border") is not None:
        border = value["border"]
        if not isinstance(border, dict):
            raise CapabilityError(
                f"{where} 的 border 格式不对（必须是对象）。",
                op=op,
                target=target,
                constraint="border 必须是对象",
                reason="invalid_style",
            )
        for key in border:
            if key not in _BORDER_KEYS:
                raise CapabilityError(
                    f"{where} 的 border 不支持 {key!r}；可用：{'、'.join(_BORDER_KEYS)}。",
                    op=op,
                    target=target,
                    constraint="border 只能用 width / style / color",
                    reason="invalid_style",
                )
        width = border.get("width", 1)
        width = as_number(width)
        if isinstance(width, float) and width.is_integer():
            width = int(width)
        if isinstance(width, bool) or not isinstance(width, int):
            raise CapabilityError(
                f"{where} 的 border.width 必须是整数。",
                op=op,
                target=target,
                constraint="border.width 必须是整数",
                reason="invalid_style",
            )
        if width < _BORDER_WIDTH_MIN or width > _BORDER_WIDTH_MAX:
            raise CapabilityError(
                f"{where} 的 border.width {width} 超出范围"
                f"（{_BORDER_WIDTH_MIN} 到 {_BORDER_WIDTH_MAX}）。",
                op=op,
                target=target,
                constraint="border.width 必须在支持范围内",
                reason="invalid_style",
            )
        line_style = str(border.get("style") or "SOLID").strip().upper()
        if line_style not in _BORDER_STYLES:
            raise CapabilityError(
                f"{where} 的 border.style {line_style!r} 不支持；"
                f"可用：{'、'.join(_BORDER_STYLES)}。",
                op=op,
                target=target,
                constraint="border.style 必须在支持范围内",
                reason="invalid_style",
            )
        style["border"] = {
            "width": width,
            "style": line_style,
            "color": str(border.get("color") or "#e2e8f0")[:64],
        }
    if value.get("borderRadius") is not None:
        radius = value["borderRadius"]
        if isinstance(radius, dict):
            style["borderRadius"] = _style_length(
                radius,
                op=op,
                target=target,
                where=f"{where} 的 borderRadius",
                low=_BORDER_RADIUS_MIN,
                high=_BORDER_RADIUS_MAX,
            )
        elif (radius := as_number(radius)) is None or (
            isinstance(radius, bool) or not isinstance(radius, int)
        ):
            raise CapabilityError(
                f"{where} 的 borderRadius 必须是整数。",
                op=op,
                target=target,
                constraint="borderRadius 必须是整数",
                reason="invalid_style",
            )
        elif radius < _BORDER_RADIUS_MIN or radius > _BORDER_RADIUS_MAX:
            raise CapabilityError(
                f"{where} 的 borderRadius {radius} 超出范围"
                f"（{_BORDER_RADIUS_MIN} 到 {_BORDER_RADIUS_MAX}）。",
                op=op,
                target=target,
                constraint="borderRadius 必须在支持范围内",
                reason="invalid_style",
            )
        else:
            style["borderRadius"] = radius
    if value.get("opacity") is not None:
        opacity = value["opacity"]
        opacity = as_number(opacity)
        if isinstance(opacity, bool) or not isinstance(opacity, (int, float)):
            raise CapabilityError(
                f"{where} 的 opacity 必须是数字。",
                op=op,
                target=target,
                constraint="opacity 必须是数字",
                reason="invalid_style",
            )
        if opacity < _OPACITY_MIN or opacity > _OPACITY_MAX:
            raise CapabilityError(
                f"{where} 的 opacity 必须在 {_OPACITY_MIN} 到 {_OPACITY_MAX} 之间。",
                op=op,
                target=target,
                constraint="opacity 必须在支持范围内",
                reason="invalid_style",
            )
        style["opacity"] = float(opacity)
    return style


def _header_level_text(level: Any) -> str:
    """一层表头路径的**文字**。

    三种写法都认：

    1. ``"能耗"`` —— 协议要求的那种（字符串）；
    2. ``{"label": "能耗"}`` —— 模型照着协议里表头节点的形状给的**对象**；
    3. ``"{'label': '能耗'}"`` —— **已经被字符串化过的对象**（历史定义里
       留下的字面量，模型照抄回来时也是这个形状）。

    第 3 种是第 2 种的"化石"：早先用 ``str(level)`` 写进去的。现在读回来
    一并还原成文字，不再让 ``{'label': '能耗'}`` 这种字面量出现在表头上。
    """
    if isinstance(level, dict):
        for key in ("label", "text", "title", "name"):
            if level.get(key):
                return str(level.get(key)).strip()
        return ""
    text = str(level if level is not None else "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return text
        if isinstance(parsed, dict):
            for key in ("label", "text", "title", "name"):
                if parsed.get(key):
                    return str(parsed.get(key)).strip()
    return text


def _normalize_header_path(
    value: Any, *, op: str, target: str, where: str
) -> list[str]:
    """多层表头路径：每一层一段表头文字（空文本不算一层）。

    协议里 ``headerPath`` 是 ``List<string>``（协议 §TableColumn），
    但模型常常把每一层写成 ``{"label": "能耗"}`` 这样的**对象**
    （照着协议里表头节点的形状来的）。那种写法**不拒绝**：
    取其中的文字（``label`` / ``text`` / ``title``）当作那一层的表头文字。

    不做这件事的实测后果：模型给的 ``[{"label": "设备名称"}]`` 被
    ``str(level)`` 写成 ``["{'label': '设备名称'}"]`` —— 协议要求的
    字符串数组里多出一段 Python 字面量，前端把它当表头文字直接显示。
    """
    if not isinstance(value, list):
        raise CapabilityError(
            f"{where} 的表头路径格式不对（必须是字符串数组）。",
            op=op,
            target=target,
            constraint="表头路径必须是字符串数组",
            reason="invalid_header_path",
        )
    path: list[str] = []
    for level in value:
        text = _header_level_text(level)
        if not text:
            raise CapabilityError(
                f"{where} 的表头路径有一层是空的；每一层都要有表头文字。",
                op=op,
                target=target,
                constraint="表头路径的每一层都要有文字",
                reason="invalid_header_path",
            )
        path.append(text)
    return path


# --------------------------------------------------------------------------
# 执行实现
# --------------------------------------------------------------------------

def _set_table_columns(
    ctx: OperationContext, params: SetTableColumnsParams
) -> CapabilityOutcome:
    """设置表格的列清单（增 / 改 / 删 / 排序，id 稳定）。"""
    op = "SET_TABLE_COLUMNS"
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
    component = _require_table_region(region, region_id, op=op)
    if "columns" not in params.model_fields_set:
        raise CapabilityError(
            "没有给出 columns：这一项要的是**完整的列清单**（给了就是全部）。",
            op=op,
            target=region_id,
            constraint="必须给出完整的列清单",
            reason="no_changes",
        )

    existing = [
        column
        for column in (component.get("columns") or [])
        if isinstance(column, dict)
    ]
    by_id: dict[str, dict[str, Any]] = {}
    for column in existing:
        column_id = str(column.get("id") or "").strip()
        if column_id and column_id not in by_id:
            by_id[column_id] = column
    used_ids = set(by_id)

    new_columns: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    for index, item in enumerate(params.columns):
        where = f"第 {index + 1} 列"
        item_id = str(item.id or "").strip()
        previous = by_id.get(item_id) if item_id else None
        if item_id and previous is None:
            raise CapabilityError(
                f"{where}的 id {item_id!r} 在当前表格里不存在："
                "改已有列要给它的原 id，新增列不要给 id。",
                op=op,
                target=region_id,
                constraint="列 id 必须已存在（新增列不给 id）",
                reason="unknown_column_id",
            )

        # 标题与取值字段**互相独立**：没给的沿用原来的。
        label = str(item.label or "").strip()
        field_ref = str(item.fieldRef or "").strip()
        if previous is not None:
            label = label or str(previous.get("label") or "").strip()
            field_ref = field_ref or str(previous.get("fieldRef") or "").strip()
        if not label:
            raise CapabilityError(
                f"{where}没有列标题（label）。",
                op=op,
                target=region_id,
                constraint="列标题不能为空",
                reason="missing_column_label",
            )
        if not field_ref:
            raise CapabilityError(
                f"{where}没有取值字段（fieldRef）。",
                op=op,
                target=region_id,
                constraint="列的取值字段不能为空",
                reason="missing_column_field_ref",
            )
        field_ref = _require_field(
            region, field_ref, what=f"{where}的字段", op=op, target=region_id
        )

        column_id = item_id or _next_column_id(used_ids)
        used_ids.add(column_id)

        if item.format is not None:
            fmt = _normalize_format(
                item.format, op=op, target=region_id, where=where
            )
        elif previous is not None and isinstance(previous.get("format"), dict):
            fmt = dict(previous["format"])
        else:
            fmt = {"kind": "NUMBER"}

        column: dict[str, Any] = {
            "id": column_id,
            "label": label,
            "fieldRef": field_ref,
            "format": fmt,
        }

        if item.width is not None:
            column["width"] = _normalize_width(
                item.width, op=op, target=region_id, where=where
            )
        elif previous is not None and isinstance(previous.get("width"), dict):
            column["width"] = dict(previous["width"])

        if item.style is not None:
            column["style"] = _normalize_style(
                item.style, op=op, target=region_id, where=where
            )
        elif previous is not None and isinstance(previous.get("style"), dict):
            column["style"] = dict(previous["style"])

        if item.headerPath is not None:
            column["headerPath"] = _normalize_header_path(
                item.headerPath, op=op, target=region_id, where=where
            )
        elif previous is not None and isinstance(previous.get("headerPath"), list):
            column["headerPath"] = [
                str(level) for level in previous["headerPath"]
            ]

        if previous is None:
            changes.append({"field": "component.columns", "action": "added", "id": column_id})
        elif _protocol_keys(previous) != _protocol_keys(column):
            changes.append({"field": "component.columns", "action": "updated", "id": column_id})
        new_columns.append(column)

    kept_ids = {str(column.get("id") or "").strip() for column in new_columns}
    removed = [
        str(column.get("id") or "").strip()
        for column in existing
        if str(column.get("id") or "").strip() not in kept_ids
    ]
    if not new_columns:
        # 一列都不留的表格**画不出来** —— 与其留一张空表，不如说清楚。
        raise CapabilityError(
            f"「{_region_title(region, region_id)}」的表格至少要保留一列："
            "这次的列清单是空的，会把所有列都移除。",
            op=op,
            target=region_id,
            constraint="表格至少要保留一列",
            reason="empty_column_list",
        )
    for column_id in removed:
        changes.append({"field": "component.columns", "action": "removed", "id": column_id})

    region["component"] = {**component, "columns": new_columns}
    title = _region_title(region, region_id)
    message = (
        f"「{title}」的表格列已更新：共 {len(new_columns)} 列"
        f"（新增 {sum(1 for item in changes if item['action'] == 'added')}、"
        f"修改 {sum(1 for item in changes if item['action'] == 'updated')}、"
        f"不再显示 {len(removed)}）；源字段、输出与指标未改动。"
    )
    return CapabilityOutcome(
        ok=True,
        message=message,
        region_id=region_id,
        title=title,
        detail={
            "regionId": region_id,
            "title": title,
            "columns": new_columns,
            "columnIds": [str(column.get("id") or "") for column in new_columns],
            "removedColumnIds": removed,
            "changes": changes,
            **business_facts(
                message=message,
                detail={
                    "regionId": region_id,
                    "title": title,
                    "columnCount": len(new_columns),
                    "removedCount": len(removed),
                },
            ),
        },
    )


def _set_table_display(
    ctx: OperationContext, params: SetTableDisplayParams
) -> CapabilityOutcome:
    """设置表格的展示方式（分组 / 序号 / 分页 / 冻结列）。"""
    op = "SET_TABLE_DISPLAY"
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
    component = _require_table_region(region, region_id, op=op)

    requested = False
    changes: list[dict[str, Any]] = []
    new_component = dict(component)

    # -- 行分组（只是展示分组，不产生任何聚合） --------------------------
    if params.clearGroupRowsBy:
        requested = True
        before = list(component.get("groupRowsBy") or [])
        new_component["groupRowsBy"] = []
        if before:
            changes.append(
                {"field": "component.groupRowsBy", "before": before, "after": []}
            )
    elif params.groupRowsBy:
        requested = True
        fields: list[str] = []
        for index, raw in enumerate(params.groupRowsBy):
            name = str(raw or "").strip()
            if not name:
                continue
            fields.append(
                _require_field(
                    region,
                    name,
                    what=f"分组字段 groupRowsBy[{index}]",
                    op=op,
                    target=region_id,
                )
            )
        before = list(component.get("groupRowsBy") or [])
        new_component["groupRowsBy"] = fields
        if before != fields:
            changes.append(
                {"field": "component.groupRowsBy", "before": before, "after": fields}
            )

    # -- 序号列 -----------------------------------------------------------
    if params.rowNumber is not None:
        requested = True
        before = component.get("rowNumber")
        new_component["rowNumber"] = bool(params.rowNumber)
        if before != new_component["rowNumber"]:
            changes.append(
                {
                    "field": "component.rowNumber",
                    "before": before,
                    "after": new_component["rowNumber"],
                }
            )

    # -- 网页分页（只是展示分页，不限制取数与导出） ------------------------
    if params.clearPageSize:
        requested = True
        before = component.get("pageSize")
        new_component["pageSize"] = None
        if before is not None:
            changes.append(
                {"field": "component.pageSize", "before": before, "after": None}
            )
    elif params.pageSize is not None:
        requested = True
        if params.pageSize < 1:
            raise CapabilityError(
                f"每页行数 {params.pageSize} 不合法：必须是 1 或更多。",
                op=op,
                target=region_id,
                constraint="每页行数必须 ≥ 1",
                reason="invalid_page_size",
            )
        before = component.get("pageSize")
        new_component["pageSize"] = int(params.pageSize)
        if before != new_component["pageSize"]:
            changes.append(
                {
                    "field": "component.pageSize",
                    "before": before,
                    "after": new_component["pageSize"],
                }
            )

    # -- 冻结列 -----------------------------------------------------------
    if params.clearFrozenColumnCount:
        requested = True
        before = component.get("frozenColumnCount")
        new_component["frozenColumnCount"] = 0
        if before != 0:
            changes.append(
                {
                    "field": "component.frozenColumnCount",
                    "before": before,
                    "after": 0,
                }
            )
    elif params.frozenColumnCount is not None:
        requested = True
        count = int(params.frozenColumnCount)
        if count < 0:
            raise CapabilityError(
                f"冻结列数 {count} 不合法：不能是负数。",
                op=op,
                target=region_id,
                constraint="冻结列数必须 ≥ 0",
                reason="invalid_frozen_column_count",
            )
        display_columns = [
            column
            for column in (component.get("columns") or [])
            if isinstance(column, dict)
        ]
        if display_columns and count > len(display_columns):
            raise CapabilityError(
                f"冻结列数 {count} 超过当前表格的 {len(display_columns)} 列。",
                op=op,
                target=region_id,
                constraint="冻结列数不能超过表格列数",
                reason="invalid_frozen_column_count",
            )
        before = component.get("frozenColumnCount")
        new_component["frozenColumnCount"] = count
        if before != count:
            changes.append(
                {
                    "field": "component.frozenColumnCount",
                    "before": before,
                    "after": count,
                }
            )

    if not requested:
        raise CapabilityError(
            "没有给出任何要改的表格展示设置"
            "（groupRowsBy / rowNumber / pageSize / frozenColumnCount）。",
            op=op,
            target=region_id,
            constraint="至少要给一项要改的设置",
            reason="no_changes",
        )

    region["component"] = new_component
    title = _region_title(region, region_id)
    message = f"「{title}」的表格展示设置已更新（{len(changes)} 项）。"
    return CapabilityOutcome(
        ok=True,
        message=message,
        region_id=region_id,
        title=title,
        detail={
            "regionId": region_id,
            "title": title,
            "groupRowsBy": list(new_component.get("groupRowsBy") or []),
            "rowNumber": new_component.get("rowNumber"),
            "pageSize": new_component.get("pageSize"),
            "frozenColumnCount": new_component.get("frozenColumnCount"),
            "changes": changes,
            **business_facts(
                message=message,
                detail={
                    "regionId": region_id,
                    "title": title,
                    "changedCount": len(changes),
                    "fields": [item["field"] for item in changes],
                },
            ),
        },
    )


def _set_table_summaries(
    ctx: OperationContext, params: SetTableSummariesParams
) -> CapabilityOutcome:
    """设置表格的汇总引用（**引用已登记的输出**，不新算）。"""
    op = "SET_TABLE_SUMMARIES"
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
    component = _require_table_region(region, region_id, op=op)

    summaries: list[dict[str, Any]] = []
    if params.clearSummaries:
        requested = True
    elif "summaries" in params.model_fields_set:
        requested = True
        output_names = _registered_output_names(region)
        for index, item in enumerate(params.summaries):
            where = f"第 {index + 1} 条汇总"
            basis = str(item.basis or "").strip().upper()
            resolved = resolve_summary_output(
                region,
                {
                    "outputRef": str(item.outputRef or ""),
                    "fieldRef": str(item.fieldRef or ""),
                    "basis": basis,
                },
            )
            if not resolved.get("ok"):
                raise CapabilityError(
                    str(resolved.get("message") or f"{where}不可用。"),
                    op=op,
                    target=region_id,
                    constraint="汇总必须引用已登记的输出、字段与合法口径",
                    reason=str(resolved.get("reason") or "invalid_summary"),
                )
            output_ref = str(item.outputRef or "")
            name = output_ref.split(":", 1)[-1]
            if not (output_ref.startswith("output:") and name in output_names):
                available = "、".join(f"output:{key}" for key in output_names) or "（无）"
                raise CapabilityError(
                    f"{where}引用的输出 {output_ref!r} 不是这个区域已登记的计算输出；"
                    "汇总必须**引用**一条与该口径一致的引擎输出，不能凭空生成。"
                    f"当前已登记：{available}。",
                    op=op,
                    target=region_id,
                    constraint="汇总输出必须指向当前区域已登记的计算输出",
                    reason="unknown_output_ref",
                )
            summaries.append(
                {
                    "label": str(item.label or "").strip(),
                    "outputRef": output_ref,
                    "fieldRef": str(item.fieldRef or "").strip(),
                    "basis": basis,
                }
            )
    else:
        raise CapabilityError(
            "没有给出 summaries：这一项要的是**完整的汇总清单**（给了就是全部），"
            "清空用 clearSummaries。",
            op=op,
            target=region_id,
            constraint="必须给出完整的汇总清单",
            reason="no_changes",
        )

    before = [dict(item) for item in (component.get("summaries") or []) if isinstance(item, dict)]
    region["component"] = {**component, "summaries": summaries}
    title = _region_title(region, region_id)
    message = (
        f"「{title}」的表格汇总已更新（{len(summaries)} 条，"
        "数值由已登记的引擎输出提供）。"
    )
    return CapabilityOutcome(
        ok=True,
        message=message,
        region_id=region_id,
        title=title,
        detail={
            "regionId": region_id,
            "title": title,
            "summaries": summaries,
            "beforeSummaries": before,
            "changed": before != summaries,
            **business_facts(
                message=message,
                detail={
                    "regionId": region_id,
                    "title": title,
                    "summaryCount": len(summaries),
                },
            ),
        },
    )


# --------------------------------------------------------------------------
# 注册
# --------------------------------------------------------------------------

register(
    EditCapability(
        id="SET_TABLE_COLUMNS",
        family=FAMILY_STAT,
        title="设置表格列",
        description=(
            "设置表格的**列清单**（完整清单：给了就是全部，顺序即显示顺序）。"
            "每一项形如 "
            '{"id": "col_1", "label": "机房名称", "fieldRef": "roomName", '
            '"format": {"kind": "TEXT"}, "width": {"value": 120, "unit": "PX"}, '
            '"style": {"textAlign": "LEFT"}, "headerPath": ["能耗", "总量"]}；'
            "给了 id 就是改这一列（id 必须已存在），不给 id 就是新增一列。"
            "标题（label）与取值字段（fieldRef）**互相独立**；"
            "清单里没有的列**不再显示**，但源字段、输出与指标一个都不删。"
            "**只改其中几列时，清单里其余列的相对顺序要照抄当前表格的顺序**"
            "（先按 get_current_region 给的顺序列出，再改你要改的那几项）—— "
            "**不要按 id 重排**：清单顺序就是显示顺序，按 id 排会把用户刚放好的列挪走。"
        ),
        params_schema=SetTableColumnsParams,
        executor=_set_table_columns,
    )
)

register(
    EditCapability(
        id="SET_TABLE_DISPLAY",
        family=FAMILY_STAT,
        title="设置表格展示方式",
        description=(
            "设置表格的**展示方式**：groupRowsBy（按字段分组显示行 —— 只是排版，"
            "**不产生小计或聚合**；清空用 clearGroupRowsBy）、rowNumber（是否显示序号）、"
            "pageSize（网页展示每页行数 —— 只是展示分页，**不限制取数与导出**；"
            "清空用 clearPageSize）、frozenColumnCount（冻结前几列；清空用 "
            "clearFrozenColumnCount）。没给的项保持原样。"
        ),
        params_schema=SetTableDisplayParams,
        executor=_set_table_display,
    )
)

register(
    EditCapability(
        id="SET_TABLE_SUMMARIES",
        family=FAMILY_STAT,
        title="设置表格汇总",
        description=(
            "设置表格的**汇总引用**（完整清单：给了就是全部；清空用 clearSummaries）。"
            "每一项形如 "
            '{"label": "告警总数", "outputRef": "output:alarms", '
            '"fieldRef": "totalAlarmCount", "basis": "FULL_RESULT"}；'
            "outputRef 必须是这个区域**已登记**的计算输出（不会凭空生成汇总），"
            "basis 只能是 FULL_RESULT（全量口径）或 VISIBLE_ROWS（可见行口径）。"
        ),
        params_schema=SetTableSummariesParams,
        executor=_set_table_summaries,
    )
)
