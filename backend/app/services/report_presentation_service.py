"""展示执行：分组、排版、样式、格式的**纯计算**批次（契约 0002 §3）。

这一层是纯函数：

- 不调用模型、不取数、不碰数据库、不碰前端、不写全局状态；
- 整批在一个深拷贝上执行，**任何一步失败就整批放弃**，
  失败时返回的定义与入参一致（不留半份定义）；
- 只接受**白名单**操作与**受约束**取值：拒绝脚本、任意 CSS 文本、
  任意属性路径以及循环容器挂接。

「分组」只管外观：移组、复制、整页样式都**不改变**卡片的时间范围、
设备范围与统计口径（契约 0002 §0）。

本模块同时是**显示格式的执行口径**（契约 0002 §3.5）：

- :func:`region_authoritative_format` —— 读出某区域的权威格式
  （``SET_FORMAT`` 写的那一份），载荷按它显示；
- :func:`format_display_value` —— 按权威格式把值渲染成**显示串**
  （底层值一个字节都不动：显示格式不改数据精度、不改统计结果）。

「应用主题」只清**该主题管理的那些**外观属性上的旧局部覆盖
（:data:`THEME_MANAGED_STYLE_KEYS`，唯一出处是主题服务）——
覆盖区域上的**全部实际样式层**（``region.style`` / ``region.styleRefs`` /
``region.component.style`` / ``region.component.styleHints[].style``）；
格式、布局、数据与不归该主题管理的键一律保留；清覆盖 ≠ 锁死。
"""
from __future__ import annotations

import copy
import logging
import re
from datetime import datetime
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_UP,
    Decimal,
)
from typing import Any, Iterable, Iterator

from app.schemas.change_set import ChangeSet, ChangeSetEntry
from app.schemas.report_definition import ReportDefinitionDraft
from app.schemas.report_presentation import PresentationResult
from app.services.report_theme_service import (
    THEME_MANAGED_STYLE_KEYS,
    clear_theme_managed_style_layers,
)


logger = logging.getLogger(__name__)

#: 容器组件类型：容器**不造数**、**不进载荷**，只负责排列与阅读顺序。
CONTAINER_COMPONENT_TYPE = "CONTAINER"

#: 允许的展示操作（白名单）。不在这里的一律整批失败。
ALLOWED_OPERATIONS = (
    "CREATE_GROUP",
    "SET_GROUP_ARRANGEMENT",
    "MOVE_REGION",
    "UNGROUP",
    "DELETE_GROUP",
    "COPY_REGION",
    "DELETE_REGION",
    "HIDE_REGION",
    "SHOW_REGION",
    "SET_PLACEMENT",
    "SET_STYLE",
    "SET_FORMAT",
    "SET_PAGE",
    "SET_METADATA",
    "SET_THEME",
)

#: 允许的排列模式。
ALLOWED_ARRANGEMENT_MODES = ("GRID", "FLOW")
ALLOWED_DIRECTIONS = ("HORIZONTAL", "VERTICAL")
ALLOWED_PLACEMENT_TYPES = ("GRID", "FLOW", "ABSOLUTE")
ALLOWED_TEXT_ALIGN = ("LEFT", "CENTER", "RIGHT")
ALLOWED_VERTICAL_ALIGN = ("TOP", "MIDDLE", "BOTTOM")
ALLOWED_BORDER_STYLE = ("SOLID", "DASHED", "DOTTED", "NONE")
ALLOWED_FORMAT_KIND = (
    "NUMBER",
    "PERCENT",
    "CURRENCY",
    "DURATION",
    "DATE",
    "DATETIME",
    "TEXT",
)
ALLOWED_ROUNDING_MODE = ("HALF_UP", "HALF_DOWN", "FLOOR", "CEIL")

#: 权威显示格式的取值域（契约 0002 §3.5）。
#:
#: 载荷里带的 ``format`` **只用这几个键** —— 与 ``_validate_format``
#: 同一份白名单，不另造键名。
ALLOWED_FORMAT_KEYS = (
    "kind",
    "precision",
    "unitLabel",
    "thousandSeparator",
    "dateTimePattern",
    "nullText",
    "roundingMode",
)


#: 主题管理的样式域（白名单键）**不在这里另写一份**：
#: 唯一出处是主题服务（契约 0002 §8.2），本模块直接 import ——
#: 免得两处各写一份、以后改一处漏一处。
#: 应用主题时清掉这些键上的旧局部覆盖；各样式层里不在这里的键**不动**
#: （清哪几层由主题服务的 ``clear_theme_managed_style_layers`` 决定）。

#: metadata 只开放这三个；ID / 版本 / 状态**不供用户直接改**。
ALLOWED_METADATA_KEYS = ("name", "description", "tags")

#: 明显不是样式值的东西：脚本、任意 CSS 文本、外链。
#:
#: ``<`` / ``>`` 一起拒（标签注入）；``{`` 与 ``}`` 同时出现也拒 ——
#: 那是把多段样式拼起来的写法，不是单个受控取值。
_UNSAFE_VALUE_PATTERN = re.compile(
    r"(<\s*/?)|(>)"
    r"|(javascript\s*:)|(expression\s*\()|(url\s*\()|(data\s*:\s*text/html)",
    re.IGNORECASE,
)

#: 一段样式里同时出现 ``{`` 与 ``}`` = 拼接写法（不是单个取值）。
_BRACE_CONCAT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


class PresentationError(Exception):
    """展示操作失败。`reason` 给机器读，`message` 给人看。

    ``details`` 放**结构化**补充信息（例如删除前的引用清单）。
    向后兼容：老调用方只传两个参数，``details`` 默认为空字典。
    """

    def __init__(
        self,
        reason: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    @property
    def code(self) -> str:
        """``reason`` 的别名：调用方习惯用 ``code`` 读失败分类。"""
        return self.reason


# --------------------------------------------------------------------------
# 区域树：唯一一套递归工具（契约 0002 §1.2）
# --------------------------------------------------------------------------


def _as_dict(definition: Any) -> dict[str, Any]:
    if isinstance(definition, ReportDefinitionDraft):
        return definition.model_dump()
    if isinstance(definition, dict):
        return definition
    raise PresentationError("invalid_definition", "报表定义必须是对象。")


def is_container(region: Any) -> bool:
    """这个区域是不是**容器**（分组）。"""
    if not isinstance(region, dict):
        return False
    component = region.get("component")
    if not isinstance(component, dict):
        return False
    return str(component.get("type") or "").upper() == CONTAINER_COMPONENT_TYPE


def iter_regions(
    definition: Any,
) -> Iterator[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """按阅读顺序产出 ``(region, ancestors)``，**容器与叶子都产出**。

    祖先链从根到父级，样式继承按这条链算。
    """
    root = _as_dict(definition)

    def walk(
        regions: Any, ancestors: list[dict[str, Any]]
    ) -> Iterator[tuple[dict[str, Any], list[dict[str, Any]]]]:
        if not isinstance(regions, list):
            return
        for region in regions:
            if not isinstance(region, dict):
                continue
            yield region, list(ancestors)
            if is_container(region):
                yield from walk(region.get("children"), ancestors + [region])

    yield from walk(root.get("regions"), [])


def iter_leaves(definition: Any) -> Iterator[dict[str, Any]]:
    """只产出**叶子**区域（容器跳过）。"""
    for region, _ in iter_regions(definition):
        if not is_container(region):
            yield region


def find_region(definition: Any, region_id: str) -> dict[str, Any] | None:
    """递归按 id 找区域（组内的卡片也找得到）。"""
    if not region_id:
        return None
    for region, _ in iter_regions(definition):
        if str(region.get("id") or "") == region_id:
            return region
    return None


def region_parent(definition: Any, region_id: str) -> dict[str, Any] | None:
    """区域所在的容器（在根返回 None）。"""
    for region, ancestors in iter_regions(definition):
        if str(region.get("id") or "") == region_id:
            return ancestors[-1] if ancestors else None
    return None


def region_ancestors(definition: Any, region_id: str) -> list[dict[str, Any]]:
    """从根到父级的祖先链。"""
    for region, ancestors in iter_regions(definition):
        if str(region.get("id") or "") == region_id:
            return ancestors
    return []


def region_index_path(definition: Any, region_id: str) -> list[int]:
    """区域在树里的路径（每层一个下标）。找不到返回空列表。"""

    def walk(regions: Any, prefix: list[int]) -> list[int] | None:
        if not isinstance(regions, list):
            return None
        for index, region in enumerate(regions):
            if not isinstance(region, dict):
                continue
            if str(region.get("id") or "") == region_id:
                return prefix + [index]
            if is_container(region):
                hit = walk(region.get("children"), prefix + [index])
                if hit is not None:
                    return hit
        return None

    root = _as_dict(definition)
    return walk(root.get("regions"), []) or []


def container_of(definition: Any, region_id: str) -> dict[str, Any] | None:
    """区域所在的容器（在根返回 None）。"""
    return region_parent(definition, region_id)


def effective_simulation_identity(region: dict[str, Any]) -> str:
    """区域的**数据身份**（契约 0002 §2）。

    新身份 = ``previewSpec.simulationIdentity``；老卡片没有就回退成它原来的
    区域 id —— 所以老卡片的种子与改造前一致，数值不漂。

    **绑定层的 ``templateBinding.simulationIdentity`` 是第二个出处**：
    复制件把源身份写在绑定层（**不碰** ``previewSpec``），
    这样老卡片被复制时不必凭空造一个空壳 ``previewSpec``
    （空壳会让 ``adapt_preview_spec`` 的回退路径绕过去 → 整批丢数）。
    """
    binding = region.get("templateBinding")
    identity = ""
    if isinstance(binding, dict):
        spec = binding.get("previewSpec")
        if isinstance(spec, dict):
            identity = str(spec.get("simulationIdentity") or "").strip()
        if not identity:
            identity = str(binding.get("simulationIdentity") or "").strip()
    return identity or str(region.get("id") or "")


# --------------------------------------------------------------------------
# 取值域校验
# --------------------------------------------------------------------------


def _reject_unsafe(value: Any) -> None:
    """值里出现脚本 / 任意 CSS 文本 / 外链 / 拼接写法 → 拒绝。

    逐层往下走（字典 / 列表里的每一个值都查）：白名单是**结构**上的
    约束，不靠调用方自觉。
    """
    if isinstance(value, str):
        if _UNSAFE_VALUE_PATTERN.search(value):
            raise PresentationError(
                "invalid_operation", "样式值里有不允许的内容（脚本 / 任意 CSS 文本）。"
            )
        if "{" in value and "}" in value and _BRACE_CONCAT_PATTERN.search(value):
            raise PresentationError(
                "invalid_operation", "样式值里有不允许的内容（拼接写法）。"
            )
        return
    if isinstance(value, dict):
        for item in value.values():
            _reject_unsafe(item)
        return
    if isinstance(value, list):
        for item in value:
            _reject_unsafe(item)


def _require_keys(params: dict[str, Any], required: tuple[str, ...], optional: tuple[str, ...]) -> None:
    allowed = set(required) | set(optional)
    for key in params:
        if key not in allowed:
            raise PresentationError(
                "invalid_operation", f"不支持的参数：{key!r}。"
            )
    for key in required:
        if key not in params:
            raise PresentationError(
                "invalid_operation", f"缺少参数：{key!r}。"
            )


def _as_int(value: Any, *, low: int, high: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PresentationError("out_of_range", f"{field} 必须是数字。")
    number = int(value)
    if number < low or number > high:
        raise PresentationError("out_of_range", f"{field} 必须在 {low} 到 {high} 之间。")
    return number


def _length(value: Any, *, field: str, low: int = 0, high: int = 400) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PresentationError("out_of_range", f"{field} 的取值格式不对。")
    unit = str(value.get("unit") or "PX").upper()
    if unit not in ("PX", "PERCENT"):
        raise PresentationError("out_of_range", f"{field} 的单位只能是 PX 或 PERCENT。")
    number = _as_int(value.get("value"), low=low, high=high, field=f"{field}.value")
    return {"value": number, "unit": unit}


def _validate_arrangement(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PresentationError("out_of_range", "排列参数格式不对。")
    for key in value:
        if key not in ("mode", "columns", "direction", "gap"):
            raise PresentationError("invalid_operation", f"排列参数不支持 {key!r}。")
    mode = str(value.get("mode") or "GRID").upper()
    if mode not in ALLOWED_ARRANGEMENT_MODES:
        raise PresentationError("out_of_range", "排列模式只能是 GRID 或 FLOW。")
    columns = _as_int(value.get("columns", 2), low=1, high=12, field="columns")
    direction = str(value.get("direction") or "HORIZONTAL").upper()
    if direction not in ALLOWED_DIRECTIONS:
        raise PresentationError("out_of_range", "方向只能是 HORIZONTAL 或 VERTICAL。")
    gap_value = value.get("gap", {"value": 12, "unit": "PX"})
    gap = _length(gap_value, field="gap", high=96)
    return {
        "mode": mode,
        "columns": columns,
        "direction": direction,
        "gap": gap,
    }


def _validate_placement(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PresentationError("out_of_range", "定位参数格式不对。")
    kind = str(value.get("type") or "").upper()
    if kind not in ALLOWED_PLACEMENT_TYPES:
        raise PresentationError("out_of_range", "定位方式只能是 GRID、FLOW 或 ABSOLUTE。")
    if kind == "GRID":
        allowed = ("type", "column", "columnSpan", "row", "rowSpan", "minHeight")
        for key in value:
            if key not in allowed:
                raise PresentationError("invalid_operation", f"网格定位不支持 {key!r}。")
        placement: dict[str, Any] = {
            "type": "GRID",
            "column": _as_int(value.get("column", 1), low=1, high=12, field="column"),
            "columnSpan": _as_int(value.get("columnSpan", 1), low=1, high=12, field="columnSpan"),
        }
        if value.get("row") is not None:
            placement["row"] = _as_int(value.get("row"), low=1, high=999, field="row")
        if value.get("rowSpan") is not None:
            placement["rowSpan"] = _as_int(value.get("rowSpan"), low=1, high=12, field="rowSpan")
        if value.get("minHeight") is not None:
            placement["minHeight"] = _length(value["minHeight"], field="minHeight")
        return placement
    if kind == "FLOW":
        allowed = ("type", "align", "width", "minHeight")
        for key in value:
            if key not in allowed:
                raise PresentationError("invalid_operation", f"流式定位不支持 {key!r}。")
        placement = {"type": "FLOW"}
        if value.get("align") is not None:
            align = str(value.get("align")).upper()
            if align not in ("START", "CENTER", "END", "STRETCH"):
                raise PresentationError("out_of_range", "对齐方式取值不对。")
            placement["align"] = align
        if value.get("width") is not None:
            placement["width"] = _length(value["width"], field="width")
        if value.get("minHeight") is not None:
            placement["minHeight"] = _length(value["minHeight"], field="minHeight")
        return placement
    allowed = ("type", "x", "y", "width", "height", "zIndex")
    for key in value:
        if key not in allowed:
            raise PresentationError("invalid_operation", f"固定定位不支持 {key!r}。")
    return {
        "type": "ABSOLUTE",
        "x": _as_int(value.get("x", 0), low=0, high=100000, field="x"),
        "y": _as_int(value.get("y", 0), low=0, high=100000, field="y"),
        "width": _as_int(value.get("width", 100), low=1, high=100000, field="width"),
        "height": _as_int(value.get("height", 100), low=1, high=100000, field="height"),
        "zIndex": _as_int(value.get("zIndex", 0), low=0, high=9999, field="zIndex"),
    }


def _validate_style(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PresentationError("out_of_range", "样式参数格式不对。")
    _reject_unsafe(value)
    for key in value:
        if key not in THEME_MANAGED_STYLE_KEYS:
            raise PresentationError(
                "invalid_operation", f"样式不支持 {key!r}（只接受受控的外观属性）。"
            )
    style: dict[str, Any] = {}
    if value.get("fontFamily") is not None:
        style["fontFamily"] = str(value["fontFamily"])[:200]
    if value.get("fontSize") is not None:
        style["fontSize"] = _length(value["fontSize"], field="fontSize", low=1, high=200)
    if value.get("fontWeight") is not None:
        style["fontWeight"] = _as_int(value["fontWeight"], low=100, high=900, field="fontWeight")
    if value.get("textColor") is not None:
        style["textColor"] = str(value["textColor"])[:64]
    if value.get("backgroundColor") is not None:
        style["backgroundColor"] = str(value["backgroundColor"])[:64]
    if value.get("textAlign") is not None:
        align = str(value["textAlign"]).upper()
        if align not in ALLOWED_TEXT_ALIGN:
            raise PresentationError("out_of_range", "水平对齐取值不对。")
        style["textAlign"] = align
    if value.get("verticalAlign") is not None:
        align = str(value["verticalAlign"]).upper()
        if align not in ALLOWED_VERTICAL_ALIGN:
            raise PresentationError("out_of_range", "垂直对齐取值不对。")
        style["verticalAlign"] = align
    if value.get("lineHeight") is not None:
        style["lineHeight"] = _length(value["lineHeight"], field="lineHeight", high=400)
    if value.get("padding") is not None:
        style["padding"] = _length(value["padding"], field="padding")
    if value.get("margin") is not None:
        style["margin"] = _length(value["margin"], field="margin", low=-200)
    if value.get("border") is not None:
        border = value["border"]
        if not isinstance(border, dict):
            raise PresentationError("out_of_range", "边框参数格式不对。")
        for key in border:
            if key not in ("width", "style", "color"):
                raise PresentationError("invalid_operation", f"边框不支持 {key!r}。")
        style["border"] = {
            "width": _as_int(border.get("width", 1), low=0, high=20, field="border.width"),
            "style": str(border.get("style") or "SOLID").upper(),
            "color": str(border.get("color") or "#e2e8f0")[:64],
        }
        if style["border"]["style"] not in ALLOWED_BORDER_STYLE:
            raise PresentationError("out_of_range", "边框线型取值不对。")
    if value.get("borderRadius") is not None:
        style["borderRadius"] = _as_int(value["borderRadius"], low=0, high=200, field="borderRadius")
    if value.get("opacity") is not None:
        opacity = value["opacity"]
        if isinstance(opacity, bool) or not isinstance(opacity, (int, float)):
            raise PresentationError("out_of_range", "透明度必须是数字。")
        if opacity < 0 or opacity > 1:
            raise PresentationError("out_of_range", "透明度必须在 0 到 1 之间。")
        style["opacity"] = float(opacity)
    return style


def _region_current_units(region: dict[str, Any]) -> dict[str, str]:
    """收集这张卡片**当前在用**的单位。

    契约 §3.5：``unitLabel`` 只允许**当前单位或空** —— 不允许把 ``Wh``
    改名成 ``kWh``（单位换算不在本批）。所以这里只认**定义里已经写着**
    的单位：卡片自身的 ``format``、主指标的 ``format``、各列的
    ``format``、以及数据管道里声明的 ``expectedFields[].unit``。

    返回 ``{规范写法: 原样写法}``；大小写等价时回写定义里的原拼写。
    """
    units: dict[str, str] = {}

    def _add(raw: Any) -> None:
        if raw is None:
            return
        text = str(raw).strip()
        if text:
            units.setdefault(text, text)

    def _scan_format(fmt: Any) -> None:
        if isinstance(fmt, dict):
            _add(fmt.get("unitLabel"))

    def _scan_columns(columns: Any) -> None:
        if isinstance(columns, list):
            for column in columns:
                if isinstance(column, dict):
                    _scan_format(column.get("format"))

    _scan_format(region.get("format"))

    component = region.get("component")
    if isinstance(component, dict):
        metric = component.get("metric")
        if isinstance(metric, dict):
            _scan_format(metric.get("format"))
        _scan_columns(component.get("columns"))

    binding = region.get("templateBinding")
    if isinstance(binding, dict):
        spec = binding.get("previewSpec")
        if isinstance(spec, dict):
            for item in spec.get("metrics") or []:
                if isinstance(item, dict):
                    _scan_format(item.get("format"))
            _scan_columns(spec.get("columns"))

    data = region.get("data")
    if isinstance(data, dict):
        _scan_columns(data.get("columns"))
        pipeline = data.get("pipeline")
        if isinstance(pipeline, dict):
            outputs = pipeline.get("outputs")
            if isinstance(outputs, dict):
                for output in outputs.values():
                    if not isinstance(output, dict):
                        continue
                    expected = output.get("expectedFields")
                    if not isinstance(expected, dict):
                        continue
                    for field in expected.values():
                        if isinstance(field, dict):
                            _add(field.get("unit"))
    return units


def _canonical_unit(label: str, current_units: dict[str, str]) -> str | None:
    """在当前单位里找等价写法；找不到返回 ``None``。"""
    if label in current_units:
        return current_units[label]
    lowered = label.lower()
    for key, value in current_units.items():
        if key.lower() == lowered:
            return value
    return None


def _validate_format(
    value: Any, current_units: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PresentationError("out_of_range", "格式参数格式不对。")
    _reject_unsafe(value)
    allowed = ALLOWED_FORMAT_KEYS
    for key in value:
        if key not in allowed:
            raise PresentationError("invalid_operation", f"显示格式不支持 {key!r}。")
    fmt: dict[str, Any] = {}
    if value.get("kind") is not None:
        kind = str(value["kind"]).upper()
        if kind not in ALLOWED_FORMAT_KIND:
            raise PresentationError("out_of_range", "显示格式类型取值不对。")
        fmt["kind"] = kind
    if value.get("precision") is not None:
        fmt["precision"] = _as_int(value["precision"], low=0, high=6, field="precision")
    if value.get("unitLabel") is not None:
        label = str(value["unitLabel"]).strip()
        if not label:
            # 契约 §3.5：允许**空**（清掉单位标签，不改口径）。
            fmt["unitLabel"] = ""
        else:
            canonical = _canonical_unit(label, current_units or {})
            if canonical is None:
                raise PresentationError(
                    "out_of_range",
                    "单位只能保持当前单位或留空（本批不支持单位换算）。",
                )
            fmt["unitLabel"] = canonical
    if value.get("thousandSeparator") is not None:
        if not isinstance(value["thousandSeparator"], bool):
            raise PresentationError("out_of_range", "千分位开关必须是布尔值。")
        fmt["thousandSeparator"] = value["thousandSeparator"]
    if value.get("dateTimePattern") is not None:
        fmt["dateTimePattern"] = str(value["dateTimePattern"])[:64]
    if value.get("nullText") is not None:
        fmt["nullText"] = str(value["nullText"])[:64]
    if value.get("roundingMode") is not None:
        mode = str(value["roundingMode"]).upper()
        if mode not in ALLOWED_ROUNDING_MODE:
            raise PresentationError("out_of_range", "舍入方式取值不对。")
        fmt["roundingMode"] = mode
    return fmt


# --------------------------------------------------------------------------
# 权威显示格式：读出来 + 按它渲染（契约 0002 §3.5）
# --------------------------------------------------------------------------


def region_authoritative_format(region: Any) -> dict[str, Any]:
    """这个区域的**权威显示格式**（契约 0002 §3.5）。

    就是 ``region["format"]`` —— ``SET_FORMAT`` 写下的那一份。载荷按它显示。

    只保留白名单（:data:`ALLOWED_FORMAT_KEYS`）里的键：别的来源写进
    ``region.format`` 的键不进载荷，载荷里的 ``format`` 结构因此是**封闭的**。
    """
    if not isinstance(region, dict):
        return {}
    raw = region.get("format")
    if not isinstance(raw, dict):
        return {}
    fmt: dict[str, Any] = {}
    for key in ALLOWED_FORMAT_KEYS:
        if key in raw and raw[key] is not None:
            fmt[key] = raw[key]
    return fmt


#: 舍入方式 → ``decimal`` 的舍入常量。
_ROUNDING_MODES = {
    "HALF_UP": ROUND_HALF_UP,
    "HALF_DOWN": ROUND_HALF_DOWN,
    "FLOOR": ROUND_FLOOR,
    "CEIL": ROUND_CEILING,
}

#: 没给 ``dateTimePattern`` 时的默认日期样式。
_DEFAULT_DATE_PATTERN = "%Y-%m-%d"
_DEFAULT_DATETIME_PATTERN = "%Y-%m-%d %H:%M"

#: 数值型的格式类型（它们只改**显示**，不改口径）。
_NUMERIC_FORMAT_KINDS = ("NUMBER", "PERCENT", "CURRENCY", "DURATION")

#: 日期型的格式类型。
_DATE_FORMAT_KINDS = ("DATE", "DATETIME")


def _is_numeric_value(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _format_number(value: Any, fmt: dict[str, Any]) -> str:
    """按格式渲染一个数值（**只改显示**，数值本身不动）。"""
    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001 —— 不是数字就原样返回，不猜
        return str(value)
    precision = fmt.get("precision")
    if precision is not None:
        try:
            digits = max(0, min(6, int(precision)))
        except (TypeError, ValueError):
            digits = 0
        mode = _ROUNDING_MODES.get(
            str(fmt.get("roundingMode") or "HALF_UP").upper(), ROUND_HALF_UP
        )
        number = number.quantize(Decimal(1).scaleb(-digits), rounding=mode)
    text = format(number, "f")
    if fmt.get("thousandSeparator"):
        sign = ""
        if text.startswith("-"):
            sign, text = "-", text[1:]
        integer, _, fraction = text.partition(".")
        if integer:
            integer = f"{int(integer):,}"
        text = sign + integer + (f".{fraction}" if fraction else "")
    return text


def _format_datetime(value: Any, fmt: dict[str, Any], kind: str) -> str:
    """按 ``dateTimePattern`` 渲染一个日期值（认不出来就原样返回）。"""
    moment: datetime | None = None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value or "").strip()
        if text:
            try:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                moment = None
    if moment is None:
        return str(value)
    pattern = str(fmt.get("dateTimePattern") or "").strip()
    if not pattern:
        pattern = (
            _DEFAULT_DATETIME_PATTERN if kind == "DATETIME" else _DEFAULT_DATE_PATTERN
        )
    try:
        return moment.strftime(pattern)
    except (ValueError, TypeError):
        return str(value)


def format_display_value(
    value: Any, fmt: Any = None, *, unit: str = ""
) -> str:
    """按**权威格式**把一个值渲染成显示串（底层值一个字节都不动）。

    - 数值：按 ``precision`` / ``thousandSeparator`` / ``roundingMode`` 显示；
    - 日期：按 ``dateTimePattern`` 显示（``kind`` 是 ``DATE`` / ``DATETIME``）；
    - 单位标签：格式里声明了 ``unitLabel`` 就用它（**可以空串** = 不带单位），
      没声明就用传入的 ``unit``。

    这就是「显示格式走权威格式」的**执行口径**：改的是显示，
    不是数据精度、也不是统计结果。
    """
    spec = dict(fmt) if isinstance(fmt, dict) else {}
    kind = str(spec.get("kind") or "").upper()
    if value is None or value == "":
        return str(spec.get("nullText") or "")
    numeric = False
    if kind == "TEXT":
        text = str(value)
    elif kind in _DATE_FORMAT_KINDS:
        text = _format_datetime(value, spec, kind)
    elif kind in _NUMERIC_FORMAT_KINDS:
        text = _format_number(value, spec)
        numeric = _is_numeric_value(value)
    elif isinstance(value, bool):
        text = str(value)
    elif _is_numeric_value(value):
        text = _format_number(value, spec)
        numeric = True
    elif spec.get("dateTimePattern"):
        text = _format_datetime(value, spec, kind or "DATETIME")
    else:
        text = str(value)
    if numeric:
        label = spec["unitLabel"] if "unitLabel" in spec else str(unit or "")
        label = str(label or "").strip()
        if label:
            text = f"{text} {label}"
    return text


# --------------------------------------------------------------------------
# 树操作（作用于深拷贝上的 dict 树）
# --------------------------------------------------------------------------


def _children_of(region: dict[str, Any]) -> list[dict[str, Any]]:
    children = region.get("children")
    return children if isinstance(children, list) else []


def _detach(root: dict[str, Any], region_id: str) -> dict[str, Any] | None:
    """把一个区域从它所在的位置**摘下来**（连同它的子树）。"""
    for region, _ in iter_regions(root):
        pass
    # 顶层
    top = root.get("regions")
    if isinstance(top, list):
        for index, region in enumerate(top):
            if isinstance(region, dict) and str(region.get("id") or "") == region_id:
                return top.pop(index)
            if isinstance(region, dict) and is_container(region):
                hit = _detach_from_children(region, region_id)
                if hit is not None:
                    return hit
    return None


def _detach_from_children(parent: dict[str, Any], region_id: str) -> dict[str, Any] | None:
    children = _children_of(parent)
    for index, region in enumerate(children):
        if not isinstance(region, dict):
            continue
        if str(region.get("id") or "") == region_id:
            return children.pop(index)
        if is_container(region):
            hit = _detach_from_children(region, region_id)
            if hit is not None:
                return hit
    return None


def _container_children(root: dict[str, Any], container_id: str) -> list[dict[str, Any]] | None:
    container = find_region(root, container_id)
    if container is None or not is_container(container):
        return None
    children = container.get("children")
    if not isinstance(children, list):
        # 空容器也要能接得住移进来的卡片：**就地**建列表，
        # 不然插进去的东西会跟着临时列表一起丢掉。
        children = []
        container["children"] = children
    return children


def _locate(
    root: dict[str, Any], region_id: str
) -> tuple[list[dict[str, Any]], int, dict[str, Any] | None] | None:
    """定位一个区域：``(所在列表, 下标, 父容器)``。

    所在列表要么是根 ``regions``，要么是某个容器的 ``children`` ——
    「原来所在的位置」就是它（UNGROUP / MOVE_REGION 要用它）。
    """
    top = _root_regions(root)
    for index, region in enumerate(top):
        if not isinstance(region, dict):
            continue
        if str(region.get("id") or "") == region_id:
            return top, index, None
        if is_container(region):
            hit = _locate_in_children(region, region_id)
            if hit is not None:
                return hit
    return None


def _locate_in_children(
    parent: dict[str, Any], region_id: str
) -> tuple[list[dict[str, Any]], int, dict[str, Any]] | None:
    children = _children_of(parent)
    for index, child in enumerate(children):
        if not isinstance(child, dict):
            continue
        if str(child.get("id") or "") == region_id:
            return children, index, parent
        if is_container(child):
            hit = _locate_in_children(child, region_id)
            if hit is not None:
                return hit
    return None


def _root_regions(root: dict[str, Any]) -> list[dict[str, Any]]:
    regions = root.get("regions")
    if not isinstance(regions, list):
        regions = []
        root["regions"] = regions
    return regions


def _auto_placements(children: list[dict[str, Any]], arrangement: dict[str, Any]) -> None:
    """按容器排列**重算**子区域的定位。

    GRID：按顺序每行 ``columns`` 个，column 从 1 起、row 递增；
    FLOW：统一 ``{"type": "FLOW"}``（顺序即阅读顺序）。
    """
    mode = str(arrangement.get("mode") or "GRID").upper()
    columns = int(arrangement.get("columns") or 1)
    for index, child in enumerate(children):
        if not isinstance(child, dict):
            continue
        layout = child.setdefault("layout", {})
        if not isinstance(layout, dict):
            layout = {}
            child["layout"] = layout
        if mode == "GRID":
            layout["placement"] = {
                "type": "GRID",
                "column": index % columns + 1,
                "columnSpan": 1,
                "row": index // columns + 1,
                "rowSpan": 1,
            }
        else:
            layout["placement"] = {"type": "FLOW"}


def _new_container(
    group_id: str,
    *,
    name: str,
    description: str,
    role: str,
    arrangement: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": group_id,
        "name": name,
        "description": description,
        "role": role,
        "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12}},
        "component": {"type": CONTAINER_COMPONENT_TYPE, "arrangement": arrangement},
        "children": [],
    }


# --------------------------------------------------------------------------
# 引用关系：删除前的影响检查
# --------------------------------------------------------------------------

#: 区域之间**显式声明**的引用字段（``templateBinding`` 里的列表）。
REFERENCE_LIST_KEYS = ("dependencies", "dependsOn", "references")

#: 失效引用在区域上留下的错误码。
BROKEN_REFERENCE_CODE = "broken_reference"


def _region_subtree_ids(region: dict[str, Any]) -> set[str]:
    """区域自己 + 它子树里所有区域的 id。"""
    ids: set[str] = set()
    stack: list[dict[str, Any]] = [region]
    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        region_id = str(item.get("id") or "")
        if region_id:
            ids.add(region_id)
        for child in item.get("children") or []:
            if isinstance(child, dict):
                stack.append(child)
    return ids


def _region_dependency_refs(region: dict[str, Any]) -> set[str]:
    """区域**显式声明**它引用了哪些区域 id。

    认 ``templateBinding.dependencies`` / ``dependsOn`` / ``references``
    三个列表。列表项可以是区域 id 字符串，也可以是 ``{"regionId": ...}``。
    缺字段容错 —— 老定义没有这些键。
    """
    binding = region.get("templateBinding")
    if not isinstance(binding, dict):
        return set()
    refs: set[str] = set()
    for key in REFERENCE_LIST_KEYS:
        items = binding.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    refs.add(text)
            elif isinstance(item, dict):
                text = str(item.get("regionId") or item.get("id") or "").strip()
                if text:
                    refs.add(text)
    return refs


def _region_metric_refs(region: dict[str, Any]) -> set[str]:
    """区域用到的 **metricRef** 集合。

    两个来源：``templateBinding.metricRefs`` 与
    ``data.pipeline.nodes[*].metricRef``。缺字段容错。
    """
    refs: set[str] = set()
    binding = region.get("templateBinding")
    if isinstance(binding, dict):
        items = binding.get("metricRefs")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, str):
                    text = item.strip()
                    if text:
                        refs.add(text)
                elif isinstance(item, dict):
                    text = str(item.get("metricRef") or item.get("ref") or "").strip()
                    if text:
                        refs.add(text)
    data = region.get("data")
    pipeline = data.get("pipeline") if isinstance(data, dict) else None
    nodes = pipeline.get("nodes") if isinstance(pipeline, dict) else None
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            text = str(node.get("metricRef") or "").strip()
            if text:
                refs.add(text)
    return refs


def _reference_matches(
    definition: Any,
    target_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """引用了目标区域的**外部区域**清单（含判定理由）。

    判定规则 —— 区域 X 被区域 Y 引用，当且仅当 Y **不在 X 自己的子树内**
    且满足任一条：

    1. Y 的 ``templateBinding.dependencies`` / ``dependsOn`` / ``references``
       里含 X 的 id；
    2. Y 与 X **共用同一 metricRef**（``templateBinding.metricRefs`` 或
       ``data.pipeline.nodes[*].metricRef`` 有交集）。

    每项形如 ``{regionId, name, reason, refRegionIds}``：
    ``refRegionIds`` 是它引用到的目标 id（一个区域可能引用多个目标）。
    """
    targets = {str(item) for item in target_ids if str(item)}
    if not targets:
        return []
    excluded: set[str] = set()
    target_metric_refs: set[str] = set()
    #: metricRef → 用了它的目标区域（共用口径时，报的是**被删的那张卡**）。
    targets_by_metric: dict[str, list[str]] = {}
    for region, _ in iter_regions(definition):
        region_id = str(region.get("id") or "")
        if region_id not in targets:
            continue
        excluded |= _region_subtree_ids(region)
        metric_refs = _region_metric_refs(region)
        target_metric_refs |= metric_refs
        for metric_ref in metric_refs:
            targets_by_metric.setdefault(metric_ref, []).append(region_id)
    matches: list[dict[str, Any]] = []
    for region, _ in iter_regions(definition):
        region_id = str(region.get("id") or "")
        if not region_id or region_id in excluded:
            continue
        dependency_hits = sorted(_region_dependency_refs(region) & targets)
        if dependency_hits:
            matches.append(
                {
                    "regionId": region_id,
                    "name": str(region.get("name") or region_id),
                    "reason": f"声明引用了 {'、'.join(dependency_hits)}",
                    "refRegionIds": dependency_hits,
                }
            )
            continue
        shared = sorted(_region_metric_refs(region) & target_metric_refs)
        if shared:
            # 共用口径：报的是**被删的那张卡**（不是指标本身）。
            ref_ids: list[str] = []
            for metric_ref in shared:
                for target_id in targets_by_metric.get(metric_ref, []):
                    if target_id not in ref_ids:
                        ref_ids.append(target_id)
            matches.append(
                {
                    "regionId": region_id,
                    "name": str(region.get("name") or region_id),
                    "reason": f"与它共用同一指标口径（{'、'.join(shared)}）",
                    "refRegionIds": ref_ids,
                }
            )
    return matches


def _reference_details(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """引用清单的对外形状：``{regionId, name, reason}``。"""
    return [
        {
            "regionId": str(match.get("regionId") or ""),
            "name": str(match.get("name") or ""),
            "reason": str(match.get("reason") or ""),
        }
        for match in matches
    ]


def _append_region_error(
    region: dict[str, Any],
    ref_region_id: str,
    ref_name: str,
) -> None:
    """给引用方记一条**失效引用**（追加，不覆盖已有内容）。

    同一个引用不重复记 —— 反复删除同一张卡片不会堆出一串一样的错误。
    """
    errors = region.get("errors")
    if not isinstance(errors, list):
        errors = []
        region["errors"] = errors
    for item in errors:
        if (
            isinstance(item, dict)
            and item.get("code") == BROKEN_REFERENCE_CODE
            and str(item.get("refRegionId") or "") == ref_region_id
        ):
            return
    errors.append(
        {
            "code": BROKEN_REFERENCE_CODE,
            "message": (
                f"这张卡片引用了已删除的卡片「{ref_name}」（{ref_region_id}），"
                "引用已失效。"
            ),
            "refRegionId": ref_region_id,
        }
    )


def _mark_broken_references(
    root: dict[str, Any],
    matches: list[dict[str, Any]],
    target_names: dict[str, str],
) -> None:
    """强制删除后：把每个引用方标成**失效引用**。

    其余卡片**完好** —— 只在引用方身上追加 ``errors``，不动别的内容。
    """
    for match in matches:
        region = find_region(root, str(match.get("regionId") or ""))
        if region is None:
            continue
        for ref_id in match.get("refRegionIds") or []:
            _append_region_error(
                region,
                str(ref_id),
                str(target_names.get(str(ref_id)) or ref_id),
            )


def collect_region_references(
    definition: Any,
    target_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """引用了目标区域的外部区域清单（``{regionId, name, reason}``）。

    删除前的影响提示用；空列表 = 没有外部引用。
    """
    return _reference_details(_reference_matches(definition, target_ids))


def check_deletable(
    definition: Any,
    target_ids: Iterable[str],
    *,
    force: bool = False,
) -> list[dict[str, Any]]:
    """删除前的**引用影响检查**。

    有外部引用且 ``force`` 为假 → 抛 :class:`PresentationError`
    （``reference_blocked``，``details`` 里带引用清单）；
    否则返回引用清单（空列表 = 没有外部引用）。
    """
    targets = [str(item) for item in target_ids if str(item)]
    matches = _reference_matches(definition, targets)
    details = _reference_details(matches)
    if matches and not force:
        listed = "、".join(
            f"{match.get('name')}（{match.get('regionId')}）" for match in details
        )
        raise PresentationError(
            "reference_blocked",
            f"要删的内容还被引用：{listed}。删除会让这些引用失效，"
            "确认要强制删除吗？",
            details={"references": details},
        )
    return details


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------


def apply_presentation(
    draft: ReportDefinitionDraft,
    operations: list[dict[str, Any]],
    *,
    base_revision: int | None = None,
) -> PresentationResult:
    """执行一批展示操作，返回新定义。

    **纯计算**：不调用模型、不取数、不碰数据库、不碰前端。
    整批失败不留下半份定义：失败时返回的 `definition` 与入参内容一致。
    """
    try:
        source = _as_dict(draft)
    except PresentationError as exc:
        return PresentationResult(
            ok=False,
            definition=draft,
            failure=exc.message,
            reason=exc.reason,
        )

    working = copy.deepcopy(source)
    affected: list[str] = []
    issues: list[dict[str, Any]] = []
    changes: list[ChangeSetEntry] = []

    try:
        if not isinstance(operations, list) or not operations:
            raise PresentationError("invalid_operation", "没有要执行的展示操作。")
        for raw in operations:
            if not isinstance(raw, dict):
                raise PresentationError("invalid_operation", "展示操作格式不对。")
            op = str(raw.get("op") or "")
            params = raw.get("params")
            if not isinstance(params, dict):
                params = {}
            if op not in ALLOWED_OPERATIONS:
                raise PresentationError(
                    "invalid_operation", f"还不支持这个展示操作：{op or '(空)'}。"
                )
            _reject_unsafe(params)
            _apply_one(working, op, params, affected, issues, changes)
    except PresentationError as exc:
        logger.info(
            "[report_presentation] 展示批次失败 reason=%s affected=%s",
            exc.reason,
            len(affected),
        )
        # 失败原因也进 ``issues``：契约 §3.2 的字段集是封闭的，
        # 结构化补充信息（例如 ``reference_blocked`` 的引用清单）走这里。
        failure_issue: dict[str, Any] = {
            "code": exc.reason,
            "message": exc.message,
        }
        if exc.details:
            failure_issue["details"] = dict(exc.details)
        return PresentationResult(
            ok=False,
            definition=draft,
            changeSet=None,
            affectedRegionIds=[],
            issues=[*issues, failure_issue],
            dataBindingChanged=False,
            failure=exc.message,
            reason=exc.reason,
        )

    revision = base_revision
    if revision is None:
        revision = int((source.get("metadata") or {}).get("revision") or 0)

    # **不**在这里改 ``metadata.revision``：版本号由引擎统一 +1
    # （所有其它意图都是这个约定）。这里是纯计算，不越权改版本。
    new_draft = ReportDefinitionDraft.model_validate(working)
    change_set = ChangeSet(
        baseDefinitionRevision=revision,
        changed=changes,
        unchanged=[],
        impact=[f"影响 {len(affected)} 个区域"],
    )
    return PresentationResult(
        ok=True,
        definition=new_draft,
        changeSet=change_set,
        affectedRegionIds=affected,
        issues=issues,
        dataBindingChanged=False,
    )


def _record_change(
    changes: list[ChangeSetEntry],
    region_id: str,
    path: str,
    before: Any,
    after: Any,
    summary: str,
) -> None:
    changes.append(
        ChangeSetEntry(
            regionId=region_id,
            path=path,
            before=before,
            after=after,
            summary=summary,
        )
    )


def _apply_one(
    root: dict[str, Any],
    op: str,
    params: dict[str, Any],
    affected: list[str],
    issues: list[dict[str, Any]],
    changes: list[ChangeSetEntry],
) -> None:
    handler = _HANDLERS[op]
    handler(root, params, affected, issues, changes)


# ---- 各操作实现 ----------------------------------------------------------


def _op_create_group(root, params, affected, issues, changes) -> None:
    _require_keys(params, ("groupId", "memberRegionIds"), ("name", "description", "role", "arrangement"))
    group_id = str(params["groupId"]).strip()
    if not group_id:
        raise PresentationError("invalid_operation", "分组需要一个 id。")
    if find_region(root, group_id) is not None:
        raise PresentationError("invalid_operation", f"已经有同名的分组 id：{group_id}。")
    members = params["memberRegionIds"]
    if not isinstance(members, list) or not members:
        raise PresentationError("invalid_operation", "分组至少要有一张卡片。")
    # **循环容器挂接**：成员之间不能互为祖先（否则树里出现环）。
    member_ids = [str(item) for item in members]
    for member_id in member_ids:
        ancestors = region_ancestors(root, member_id)
        if any(str(item.get("id") or "") in member_ids for item in ancestors):
            raise PresentationError("cycle", "不能把分组与它自己的子分组放进同一组。")
    arrangement = _validate_arrangement(
        params.get("arrangement") or {"mode": "GRID", "columns": len(members)}
    )
    role = str(params.get("role") or "DETAIL").upper()

    # 容器插到**第一个成员原来所在的位置**：位置要在摘成员**之前**算，
    # 摘完就找不到了。
    #
    # 成员可能来自不同的父容器（一个在根、一个在别的组里）——那就把容器
    # 放在**第一个成员**的位置上，成员各自从原处摘出来。
    member_ids = {str(member) for member in members}
    anchor = _locate(root, str(members[0]))
    if anchor is None:
        raise PresentationError(
            "unknown_region", f"找不到要分组的卡片：{members[0]}。"
        )
    anchor_list, anchor_index, anchor_parent = anchor
    insert_at = anchor_index
    if anchor_parent is None:
        insert_list = _root_regions(root)
    else:
        insert_list = _children_of(anchor_parent)

    # 再摘出成员：保留 regionId 与全部数据绑定。
    moved: list[dict[str, Any]] = []
    for member_id in members:
        member = _detach(root, str(member_id))
        if member is None:
            raise PresentationError(
                "unknown_region", f"找不到要分组的卡片：{member_id}。"
            )
        moved.append(member)

    container = _new_container(
        group_id,
        name=str(params.get("name") or "新分组"),
        description=str(params.get("description") or ""),
        role=role,
        arrangement=arrangement,
    )
    children = container["children"]
    children.extend(moved)
    _auto_placements(children, arrangement)

    # 摘成员会让下标前移 → 按**还剩下几个**把位置修正回来。
    offset = sum(
        1
        for index, item in enumerate(insert_list[:insert_at])
        if isinstance(item, dict) and str(item.get("id") or "") not in member_ids
    )
    insert_list.insert(min(offset, len(insert_list)), container)

    affected.append(group_id)
    affected.extend(str(member.get("id") or "") for member in moved)
    _record_change(
        changes, group_id, f"/regions/{group_id}", None, container,
        f"新建分组「{container['name']}」，{len(moved)} 张卡片并排",
    )


def _op_set_group_arrangement(root, params, affected, issues, changes) -> None:
    _require_keys(params, ("groupId", "arrangement"), ())
    group_id = str(params["groupId"])
    container = find_region(root, group_id)
    if container is None or not is_container(container):
        raise PresentationError("unknown_region", f"找不到这个分组：{group_id}。")
    arrangement = _validate_arrangement(params["arrangement"])
    component = container.setdefault("component", {})
    before = copy.deepcopy(component.get("arrangement"))
    component["arrangement"] = arrangement
    children = _children_of(container)
    _auto_placements(children, arrangement)
    affected.append(group_id)
    _record_change(
        changes, group_id, f"/regions/{group_id}/component/arrangement", before, arrangement,
        f"调整分组排列：{arrangement['mode']} / {arrangement['columns']} 列 / 间距 {arrangement['gap']['value']}",
    )


def _op_move_region(root, params, affected, issues, changes) -> None:
    _require_keys(params, ("regionId",), ("targetGroupId", "index"))
    region_id = str(params["regionId"])
    region = find_region(root, region_id)
    if region is None:
        raise PresentationError("unknown_region", f"找不到这张卡片：{region_id}。")
    target_group = params.get("targetGroupId")
    if target_group is not None:
        target_id = str(target_group)
        if target_id == region_id:
            raise PresentationError("cycle", "不能把分组移进它自己。")
        if find_region(root, target_id) is None:
            raise PresentationError(
                "unknown_region", f"找不到目标分组：{target_id}。"
            )
        # **循环容器挂接**：目标分组不能是自己或自己的后代。
        # 容器自己也是能移的 —— 所以这条判据对容器与叶子一视同仁。
        ancestors = region_ancestors(root, target_id)
        if any(str(item.get("id") or "") == region_id for item in ancestors):
            raise PresentationError("cycle", "不能把分组移进它自己的子分组。")
    moved = _detach(root, region_id)
    if moved is None:
        raise PresentationError("unknown_region", f"找不到这张卡片：{region_id}。")
    if target_group is None:
        siblings = _root_regions(root)
    else:
        siblings = _container_children(root, str(target_group))
        if siblings is None:
            raise PresentationError(
                "unknown_region", f"找不到目标分组：{target_group}。"
            )
    index = params.get("index")
    if index is None:
        siblings.append(moved)
    else:
        position = _as_int(index, low=0, high=max(len(siblings), 0), field="index")
        siblings.insert(position, moved)
    affected.append(region_id)
    _record_change(
        changes, region_id, f"/regions/{region_id}", None, moved,
        "移动卡片（id 与统计口径不变）",
    )


def _op_ungroup(root, params, affected, issues, changes) -> None:
    _require_keys(params, ("groupId",), ())
    group_id = str(params["groupId"])
    container = find_region(root, group_id)
    if container is None or not is_container(container):
        raise PresentationError("unknown_region", f"找不到这个分组：{group_id}。")
    children = list(_children_of(container))
    # 位置要在摘容器**之前**算（摘完就找不到了）。
    # 容器可能套在别的容器里 → 子卡要放回**它自己那一层**，
    # 不是一律回到根。
    anchor = _locate(root, group_id)
    if anchor is None:
        raise PresentationError("unknown_region", f"找不到这个分组：{group_id}。")
    insert_list, insert_at, _parent = anchor
    detached = _detach(root, group_id)
    if detached is None:
        raise PresentationError("unknown_region", f"找不到这个分组：{group_id}。")
    # **取消分组保留子卡**：按原相对顺序放回容器原来所在的位置。
    for offset, child in enumerate(children):
        insert_list.insert(min(insert_at + offset, len(insert_list)), child)
        affected.append(str(child.get("id") or ""))
    affected.append(group_id)
    _record_change(
        changes, group_id, f"/regions/{group_id}", detached, None,
        f"取消分组，保留 {len(children)} 张卡片",
    )


def _referenced_region_ids(root: dict[str, Any]) -> set[str]:
    """定义里**引用**了哪些区域。

    两个来源（契约 0002 §3.1）：``intentBindings`` 的 ``targetRegionIds``
    与 ``dependencies`` 里登记的区域引用。引用还在 → 删不得。
    """
    referenced: set[str] = set()
    for binding in root.get("intentBindings") or []:
        if not isinstance(binding, dict):
            continue
        for region_id in binding.get("targetRegionIds") or []:
            referenced.add(str(region_id))
    dependencies = root.get("dependencies")
    if isinstance(dependencies, dict):
        for key in ("regionIds", "regions", "targetRegionIds"):
            for region_id in dependencies.get(key) or []:
                referenced.add(str(region_id))
    return referenced


def _op_delete_group(root, params, affected, issues, changes) -> None:
    _require_keys(params, ("groupId",), ("force",))
    group_id = str(params["groupId"])
    container = find_region(root, group_id)
    if container is None or not is_container(container):
        raise PresentationError("unknown_region", f"找不到这个分组：{group_id}。")
    children = _children_of(container)
    forced = bool(params.get("force"))
    member_ids = {group_id} | {str(child.get("id") or "") for child in children}
    # 两类引用都要看：定义级登记的引用（老行为）与区域之间的引用。
    blocked = sorted(member_ids & _referenced_region_ids(root))
    matches = _reference_matches(root, member_ids)
    if (blocked or matches) and not forced:
        group_name = str(container.get("name") or group_id)
        parts: list[str] = []
        if blocked:
            parts.append(f"这个分组里还有卡片被引用（{'、'.join(blocked)}）")
        if matches:
            details = _reference_details(matches)
            listed = "、".join(
                f"{item['name']}（{item['regionId']}）" for item in details
            )
            parts.append(f"外部有 {len(details)} 处引用这个分组或它的子卡：{listed}")
        else:
            details = []
            parts.append("外部没有卡片引用这个分组或它的子卡")
        message = (
            f"分组「{group_name}」删除前需要确认：{'，'.join(parts)}。"
            "删除会影响这些引用，确认要一起删掉吗？"
        )
        issues.append(
            {
                "code": "reference_blocked",
                "message": message,
                "regionId": group_id,
            }
        )
        raise PresentationError(
            "reference_blocked",
            message,
            details={"references": details, "definitionReferences": blocked},
        )
    target_names = {group_id: str(container.get("name") or group_id)}
    for child in children:
        child_id = str(child.get("id") or "")
        target_names[child_id] = str(child.get("name") or child_id)
    detached = _detach(root, group_id)
    if detached is None:
        raise PresentationError("unknown_region", f"找不到这个分组：{group_id}。")
    # 强制删除：引用这个分组或它任一子卡的区域标成**失效引用**。
    _mark_broken_references(root, matches, target_names)
    affected.append(group_id)
    affected.extend(str(child.get("id") or "") for child in children)
    _record_change(
        changes, group_id, f"/regions/{group_id}", detached, None, "删除整个分组",
    )


def _copy_subtree_id_map(region: dict[str, Any], new_root_id: str) -> dict[str, str]:
    """复制件里「原 id → 新 id」的映射。

    **规则写死在这一处**（确定性，测试照它断言）：

    - 复制根 = 调用方给的 ``newRegionId``；
    - 后代 = ``f"{父的新 id}_{原 id}"``（逐层往下拼）。

    复制一整个分组时，组里的子卡 id **全部重建** —— 否则复制件与源件
    抢同一个 id，定位、编辑、数据绑定全都串味。
    """
    mapping: dict[str, str] = {}

    def walk(node: dict[str, Any], parent_new_id: str) -> None:
        for child in node.get("children") or []:
            if not isinstance(child, dict):
                continue
            old_id = str(child.get("id") or "")
            if not old_id:
                continue
            new_child_id = f"{parent_new_id}_{old_id}"
            mapping[old_id] = new_child_id
            walk(child, new_child_id)

    walk(region, new_root_id)
    return mapping


def _rebuild_copy_ids(
    copied: dict[str, Any], new_id: str, id_map: dict[str, str]
) -> None:
    """把复制件的展示 id 全部换成新的（递归）。"""
    copied["id"] = new_id
    for child in copied.get("children") or []:
        if not isinstance(child, dict):
            continue
        old_id = str(child.get("id") or "")
        _rebuild_copy_ids(child, id_map.get(old_id) or f"{new_id}_{old_id}", id_map)


def _remap_region_references(
    region: dict[str, Any], id_map: dict[str, str]
) -> None:
    """复制件里指向**自己人**的引用改成新 id（别指回源）。

    指到**复制件外面**的引用不动 —— 那些区域还在，引用照样有效。
    """
    binding = region.get("templateBinding")
    if not isinstance(binding, dict):
        return
    for key in REFERENCE_LIST_KEYS:
        items = binding.get(key)
        if not isinstance(items, list):
            continue
        remapped: list[Any] = []
        changed = False
        for item in items:
            if isinstance(item, str):
                new_id = id_map.get(item.strip())
                if new_id:
                    remapped.append(new_id)
                    changed = True
                    continue
                remapped.append(item)
            elif isinstance(item, dict):
                new_item = dict(item)
                for id_key in ("regionId", "id"):
                    old_id = str(new_item.get(id_key) or "").strip()
                    if old_id and old_id in id_map:
                        new_item[id_key] = id_map[old_id]
                        changed = True
                remapped.append(new_item)
            else:
                remapped.append(item)
        if changed:
            binding[key] = remapped


def _mark_copied_regions(
    source: dict[str, Any],
    copied: dict[str, Any],
    id_map: dict[str, str],
) -> None:
    """复制件逐个区域：源身份 + 复制件标记 + 组内引用映射。

    - **身份**：沿用源区域的**有效身份** → 复制件与源件数值一致；
    - **复制件标记**：不参与总计翻倍；
    - **引用**：组内引用指向**新 id**，不指回源。
    """
    source_id = str(source.get("id") or "")
    copied_id = str(copied.get("id") or "")
    binding = copied.get("templateBinding")
    if not isinstance(binding, dict):
        binding = {}
        copied["templateBinding"] = binding
    spec = binding.get("previewSpec")
    if not isinstance(spec, dict):
        spec = None

    # **身份写回规则**（契约 0002 §2.1）：只有当身份**不等于**新展示 id
    # 时才落库 —— 免得给历史区域批量加字段。
    identity = effective_simulation_identity(source)
    if identity and identity != copied_id:
        binding["simulationIdentity"] = identity
        if spec is not None:
            spec["simulationIdentity"] = identity

    # **复制件标记**：复制件与源件数值一致，但不参与总计。
    if spec is not None:
        spec["duplicateOfRegionId"] = source_id
    binding["duplicateOfRegionId"] = source_id
    if spec is None:
        # 源区域**没有** previewSpec：不要凭空造一个空的 ——
        # 空壳会让造数器以为这份定义已经登记过语义（``adapt_preview_spec``
        # 的回退路径就绕过去了），组内/历史卡片会**整批丢数**。
        # 复制件标记因此放在**区域级** ``previewSpec`` 上：后端不读它
        # （语义仍走 templateBinding），前端读它来把复制件排除出总计。
        own = copied.get("previewSpec")
        if not isinstance(own, dict):
            own = {}
            copied["previewSpec"] = own
        own["duplicateOfRegionId"] = source_id

    _remap_region_references(copied, id_map)

    source_children = [
        child for child in (source.get("children") or []) if isinstance(child, dict)
    ]
    copied_children = [
        child for child in (copied.get("children") or []) if isinstance(child, dict)
    ]
    for source_child, copied_child in zip(source_children, copied_children):
        _mark_copied_regions(source_child, copied_child, id_map)


def _op_copy_region(root, params, affected, issues, changes) -> None:
    _require_keys(params, ("regionId", "newRegionId"), ())
    region_id = str(params["regionId"])
    new_id = str(params["newRegionId"]).strip()
    region = find_region(root, region_id)
    if region is None:
        raise PresentationError("unknown_region", f"找不到这张卡片：{region_id}。")
    if not new_id:
        raise PresentationError("invalid_operation", "复制件需要一个新 id。")
    if find_region(root, new_id) is not None:
        raise PresentationError("invalid_operation", f"已经有同名的卡片 id：{new_id}。")

    duplicate = copy.deepcopy(region)
    # **递归重建全部展示 id**：复制根用新 id，后代按「父新 id_原 id」重建 ——
    # 全树唯一（契约 0002 §3.1：复制是新 id）。
    id_map = _copy_subtree_id_map(region, new_id)
    existing_ids = {str(item.get("id") or "") for item, _ in iter_regions(root)}
    for mapped in id_map.values():
        if mapped in existing_ids:
            raise PresentationError(
                "invalid_operation", f"复制件的 id 与现有卡片重名：{mapped}。"
            )
    _rebuild_copy_ids(duplicate, new_id, id_map)
    # 逐个区域带身份 / 复制件标记 / 组内引用映射。
    _mark_copied_regions(region, duplicate, id_map)

    parent = region_parent(root, region_id)
    if parent is not None:
        children = _children_of(parent)
        index = next(
            (i for i, item in enumerate(children) if str(item.get("id") or "") == region_id),
            len(children) - 1,
        )
        children.insert(index + 1, duplicate)
    else:
        top = _root_regions(root)
        index = next(
            (i for i, item in enumerate(top) if str(item.get("id") or "") == region_id),
            len(top) - 1,
        )
        top.insert(index + 1, duplicate)
    affected.append(new_id)
    affected.extend(id_map.values())
    issues.append(
        {
            "code": "duplicate_display",
            "message": "复制件与源件数值一致，但不参与总计。",
            "regionId": new_id,
        }
    )
    _record_change(
        changes, new_id, f"/regions/{new_id}", None, duplicate, "复制卡片",
    )


def _op_delete_region(root, params, affected, issues, changes) -> None:
    _require_keys(params, ("regionId",), ("force",))
    region_id = str(params["regionId"])
    region = find_region(root, region_id)
    if region is None:
        raise PresentationError("unknown_region", f"找不到这张卡片：{region_id}。")
    forced = bool(params.get("force"))
    # **删除前的引用影响检查**：有引用方且没 force → 整批失败，
    # 定义**一个字节都不改**（失败时抛错，不动 working）。
    matches = _reference_matches(root, [region_id])
    if matches and not forced:
        details = _reference_details(matches)
        listed = "、".join(f"{item['name']}（{item['regionId']}）" for item in details)
        region_name = str(region.get("name") or region_id)
        raise PresentationError(
            "reference_blocked",
            f"卡片「{region_name}」（{region_id}）被 {len(details)} 处引用：{listed}。"
            "删掉之后这些引用会失效。确认要强制删除吗？",
            details={"references": details},
        )
    target_names = {region_id: str(region.get("name") or region_id)}
    detached = _detach(root, region_id)
    if detached is None:
        raise PresentationError("unknown_region", f"找不到这张卡片：{region_id}。")
    # 强制删除：把每个引用方标成**失效引用**（追加，不覆盖已有内容），
    # 其余卡片完好。
    _mark_broken_references(root, matches, target_names)
    affected.append(region_id)
    affected.extend(str(match.get("regionId") or "") for match in matches)
    _record_change(
        changes, region_id, f"/regions/{region_id}", detached, None, "删除卡片",
    )


def _op_hide_region(root, params, affected, issues, changes) -> None:
    _require_keys(params, ("regionId",), ())
    region_id = str(params["regionId"])
    region = find_region(root, region_id)
    if region is None:
        raise PresentationError("unknown_region", f"找不到这张卡片：{region_id}。")
    before = region.get("hidden")
    region["hidden"] = True
    affected.append(region_id)
    _record_change(
        changes, region_id, f"/regions/{region_id}/hidden", before, True, "隐藏内容",
    )


def _op_show_region(root, params, affected, issues, changes) -> None:
    _require_keys(params, ("regionId",), ())
    region_id = str(params["regionId"])
    region = find_region(root, region_id)
    if region is None:
        raise PresentationError("unknown_region", f"找不到这张卡片：{region_id}。")
    before = region.get("hidden")
    region["hidden"] = False
    affected.append(region_id)
    _record_change(
        changes, region_id, f"/regions/{region_id}/hidden", before, False, "恢复显示",
    )


def _op_set_placement(root, params, affected, issues, changes) -> None:
    _require_keys(params, ("regionId", "placement"), ())
    region_id = str(params["regionId"])
    region = find_region(root, region_id)
    if region is None:
        raise PresentationError("unknown_region", f"找不到这张卡片：{region_id}。")
    placement = _validate_placement(params["placement"])
    layout = region.get("layout")
    if not isinstance(layout, dict):
        layout = {}
        region["layout"] = layout
    before = copy.deepcopy(layout.get("placement"))
    layout["placement"] = placement
    affected.append(region_id)
    _record_change(
        changes, region_id, f"/regions/{region_id}/layout/placement", before, placement,
        "调整排版位置",
    )


def _op_set_style(root, params, affected, issues, changes) -> None:
    _require_keys(params, ("regionId", "style"), ())
    region_id = str(params["regionId"])
    region = find_region(root, region_id)
    if region is None:
        raise PresentationError("unknown_region", f"找不到这张卡片：{region_id}。")
    style = _validate_style(params["style"])
    before = copy.deepcopy(region.get("style"))
    merged = dict(before) if isinstance(before, dict) else {}
    merged.update(style)
    region["style"] = merged
    affected.append(region_id)
    _record_change(
        changes, region_id, f"/regions/{region_id}/style", before, merged, "调整外观",
    )


def _op_set_format(root, params, affected, issues, changes) -> None:
    _require_keys(params, ("regionId", "format"), ())
    region_id = str(params["regionId"])
    region = find_region(root, region_id)
    if region is None:
        raise PresentationError("unknown_region", f"找不到这张卡片：{region_id}。")
    fmt = _validate_format(params["format"], _region_current_units(region))
    before = copy.deepcopy(region.get("format"))
    merged = dict(before) if isinstance(before, dict) else {}
    merged.update(fmt)
    region["format"] = merged
    affected.append(region_id)
    _record_change(
        changes, region_id, f"/regions/{region_id}/format", before, merged, "调整显示格式",
    )


def _op_set_page(root, params, affected, issues, changes) -> None:
    _require_keys(params, (), ("webMaxWidth", "arrangement"))
    page = root.get("page")
    if not isinstance(page, dict):
        page = {}
        root["page"] = page
    if params.get("webMaxWidth") is not None:
        width = _length(params["webMaxWidth"], field="webMaxWidth", low=640, high=2560)
        before = copy.deepcopy(page.get("webMaxWidth"))
        page["webMaxWidth"] = width
        _record_change(
            changes, "page", "/page/webMaxWidth", before, width, "设置网页最大宽度",
        )
    if params.get("arrangement") is not None:
        arrangement = _validate_arrangement(params["arrangement"])
        before = copy.deepcopy(page.get("arrangement"))
        page["arrangement"] = arrangement
        _record_change(
            changes, "page", "/page/arrangement", before, arrangement, "设置整页排列",
        )
    affected.append("page")


def _op_set_metadata(root, params, affected, issues, changes) -> None:
    _require_keys(params, (), ALLOWED_METADATA_KEYS)
    metadata = root.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        root["metadata"] = metadata
    for key, value in params.items():
        if key not in ALLOWED_METADATA_KEYS:
            raise PresentationError(
                "invalid_operation", f"metadata 不支持改 {key!r}（ID / 版本 / 状态不开放）。"
            )
        if key == "tags":
            if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
                raise PresentationError("out_of_range", "标签必须是字符串列表。")
            before = copy.deepcopy(metadata.get("tags"))
            metadata["tags"] = [str(t)[:64] for t in value]
        else:
            before = copy.deepcopy(metadata.get(key))
            metadata[key] = str(value)[:200]
        _record_change(
            changes, "metadata", f"/metadata/{key}", before, metadata[key], "修改报表信息",
        )
    affected.append("metadata")


def _clear_theme_managed_overrides(
    root: dict[str, Any],
    affected: list[str],
    changes: list[ChangeSetEntry],
) -> None:
    """清掉区域上**属于主题域**的旧局部外观覆盖。

    清**所有实际样式层**（``region.style`` / ``region.styleRefs`` /
    ``region.component.style`` / ``region.component.styleHints[].style``）——
    前端渲染器会应用这四层（见
    ``frontend/pages/workbench_presentation.js`` 的 ``nodeStyleSources()`` /
    ``nodeComponentStyleSources()``），只清一层的话别的层里压着的旧颜色
    还盖在新主题上，换主题看着没生效。

    只清 ``THEME_MANAGED_STYLE_KEYS`` 里的键；扩展位（不在这里的键）以及
    **格式 / 布局 / 数据**一个字节都不动 —— 清完主题的 ``defaultStyle``
    才重新生效。

    **清覆盖 ≠ 锁死**：清完之后用户再做新的 ``SET_STYLE`` 仍然是新的局部覆盖。
    """
    for region, _ in iter_regions(root):
        region_id = str(region.get("id") or "")
        touched = False
        for path, before, after in clear_theme_managed_style_layers(region):
            if not touched:
                affected.append(region_id)
                touched = True
            _record_change(
                changes,
                region_id,
                f"/regions/{region_id}{path}",
                before,
                after,
                "应用主题：清掉该主题管理的旧外观覆盖",
            )


def _op_set_theme(root, params, affected, issues, changes) -> None:
    _require_keys(params, ("themeId",), ())
    theme_id = str(params["themeId"]).strip()
    if not theme_id:
        raise PresentationError("invalid_operation", "主题需要一个 id。")
    before = copy.deepcopy(root.get("theme"))
    root["theme"] = {"themeRef": theme_id, "version": "1.0"}
    affected.append("theme")
    _record_change(
        changes, "theme", "/theme", before, root["theme"], f"应用主题 {theme_id}",
    )
    # **应用主题 = 清掉它管的那部分旧局部覆盖**（契约 0002 §8.2）：
    # 不清的话，卡片上还压着旧颜色，主题看起来没生效。
    _clear_theme_managed_overrides(root, affected, changes)


_HANDLERS = {
    "CREATE_GROUP": _op_create_group,
    "SET_GROUP_ARRANGEMENT": _op_set_group_arrangement,
    "MOVE_REGION": _op_move_region,
    "UNGROUP": _op_ungroup,
    "DELETE_GROUP": _op_delete_group,
    "COPY_REGION": _op_copy_region,
    "DELETE_REGION": _op_delete_region,
    "HIDE_REGION": _op_hide_region,
    "SHOW_REGION": _op_show_region,
    "SET_PLACEMENT": _op_set_placement,
    "SET_STYLE": _op_set_style,
    "SET_FORMAT": _op_set_format,
    "SET_PAGE": _op_set_page,
    "SET_METADATA": _op_set_metadata,
    "SET_THEME": _op_set_theme,
}
