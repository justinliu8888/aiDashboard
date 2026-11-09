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
from dataclasses import dataclass, field
from datetime import datetime
from decimal import (
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Decimal,
)
from typing import Any, Iterable, Iterator

from pydantic import ValidationError

from app.schemas.change_set import ChangeSet, ChangeSetEntry
from app.schemas.report_definition import ReportDefinitionDraft
from app.schemas.report_presentation import PresentationResult
from app.services.report_theme_service import (
    THEME_MANAGED_STYLE_KEYS,
    clear_theme_managed_style_layers,
    theme_service,
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


# --------------------------------------------------------------------------
# 契约事实：参数契约层与能力总目录**读这一份**
# --------------------------------------------------------------------------
#
# 下面这些常量是「真实约束」的可导入形态：校验函数用它们判合法性，
# 能力总目录（``capability_catalog_service``）与共享契约
# （``edit_capabilities/contract.py``）也读它们写参数说明 ——
# **同一份数字、同一份取值域**，不允许目录里另抄一份。

#: 长度类字段的单位（像素 / 百分比）。
LENGTH_UNITS = ("PX", "PERCENT")

#: 长度类字段的默认取值范围（各字段另有更严的范围时用自己的）。
LENGTH_MIN = 0
LENGTH_MAX = 400

#: 排列参数（``arrangement``）的键。
ARRANGEMENT_KEYS = ("mode", "columns", "direction", "gap")

#: 排列参数的列数范围。
ARRANGEMENT_COLUMNS_MIN = 1
ARRANGEMENT_COLUMNS_MAX = 12

#: 排列参数的间距（``gap``）范围。
ARRANGEMENT_GAP_MIN = 0
ARRANGEMENT_GAP_MAX = 96

#: 页面最大宽度（``webMaxWidth``）范围。
PAGE_WIDTH_MIN = 640
PAGE_WIDTH_MAX = 2560

#: 定位（``placement``）的网格列范围。
PLACEMENT_COLUMN_MIN = 1
PLACEMENT_COLUMN_MAX = 12

#: 定位的网格行范围。
PLACEMENT_ROW_MIN = 1
PLACEMENT_ROW_MAX = 999

#: 定位的网格行跨度范围。
PLACEMENT_ROW_SPAN_MIN = 1
PLACEMENT_ROW_SPAN_MAX = 12

#: 固定定位（ABSOLUTE）的坐标范围。
PLACEMENT_ABSOLUTE_MIN = 0
PLACEMENT_ABSOLUTE_MAX = 100000

#: 固定定位的层级（``zIndex``）范围。
PLACEMENT_ZINDEX_MIN = 0
PLACEMENT_ZINDEX_MAX = 9999

#: 流式定位的对齐方式。
PLACEMENT_ALIGNMENTS = ("START", "CENTER", "END", "STRETCH")

#: 样式字号范围（像素）。
STYLE_FONT_SIZE_MIN = 1
STYLE_FONT_SIZE_MAX = 200

#: 样式字重范围（百分数）。
STYLE_FONT_WEIGHT_MIN = 100
STYLE_FONT_WEIGHT_MAX = 900

#: 样式行高范围。
STYLE_LINE_HEIGHT_MIN = 0
STYLE_LINE_HEIGHT_MAX = 400

#: 样式外边距范围（可负）。
STYLE_MARGIN_MIN = -200
STYLE_MARGIN_MAX = 400

#: 样式边框宽度范围。
STYLE_BORDER_WIDTH_MIN = 0
STYLE_BORDER_WIDTH_MAX = 20

#: 样式圆角范围。
STYLE_BORDER_RADIUS_MIN = 0
STYLE_BORDER_RADIUS_MAX = 200

#: 样式透明度范围。
STYLE_OPACITY_MIN = 0
STYLE_OPACITY_MAX = 1

#: 显示格式的小数位范围。
FORMAT_PRECISION_MIN = 0
FORMAT_PRECISION_MAX = 6

#: 展示操作里承载「区块 / 分组引用」的字段：字段 → 取值来源。
#:
#: 这些字段的取值**必须来自当前草稿**，不许编 —— 目录里的「引用规则」
#: 与错误反馈里的「引用说明」都读这一份。
PRESENTATION_REFERENCE_FIELDS: dict[str, str] = {
    "regionId": "当前草稿里的区块 id（get_current_draft 返回的正文卡片 id 清单）",
    "newRegionId": "新卡片使用的 id（不能与现有区块 id 重复）",
    "groupId": "当前草稿里的分组 id（CREATE_GROUP 建出来的分组）",
    "memberRegionIds": "当前草稿里的区块 id 列表（至少一张卡片）",
    "targetGroupId": "当前草稿里的分组 id",
    "afterRegionId": "当前草稿里的区块 id（相对位置锚点：放到这张卡后面）",
    "beforeRegionId": "当前草稿里的区块 id（相对位置锚点：放到这张卡前面）",
}

#: 引用字段里哪些是**id 列表**（其余是单个 id）。
PRESENTATION_REFERENCE_LIST_FIELDS = ("memberRegionIds",)

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


def _length(value: Any, *, field: str, low: int = LENGTH_MIN, high: int = LENGTH_MAX) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PresentationError("out_of_range", f"{field} 的取值格式不对。")
    unit = str(value.get("unit") or "PX").upper()
    if unit not in LENGTH_UNITS:
        raise PresentationError("out_of_range", f"{field} 的单位只能是 PX 或 PERCENT。")
    number = _as_int(value.get("value"), low=low, high=high, field=f"{field}.value")
    return {"value": number, "unit": unit}


def _validate_arrangement(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PresentationError("out_of_range", "排列参数格式不对。")
    for key in value:
        if key not in ARRANGEMENT_KEYS:
            raise PresentationError("invalid_operation", f"排列参数不支持 {key!r}。")
    mode = str(value.get("mode") or "GRID").upper()
    if mode not in ALLOWED_ARRANGEMENT_MODES:
        raise PresentationError("out_of_range", "排列模式只能是 GRID 或 FLOW。")
    columns = _as_int(
        value.get("columns", 2),
        low=ARRANGEMENT_COLUMNS_MIN,
        high=ARRANGEMENT_COLUMNS_MAX,
        field="columns",
    )
    direction = str(value.get("direction") or "HORIZONTAL").upper()
    if direction not in ALLOWED_DIRECTIONS:
        raise PresentationError("out_of_range", "方向只能是 HORIZONTAL 或 VERTICAL。")
    gap_value = value.get("gap", {"value": 12, "unit": "PX"})
    gap = _length(gap_value, field="gap", high=ARRANGEMENT_GAP_MAX)
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
            "column": _as_int(
                value.get("column", 1),
                low=PLACEMENT_COLUMN_MIN,
                high=PLACEMENT_COLUMN_MAX,
                field="column",
            ),
            "columnSpan": _as_int(
                value.get("columnSpan", 1),
                low=PLACEMENT_COLUMN_MIN,
                high=PLACEMENT_COLUMN_MAX,
                field="columnSpan",
            ),
        }
        if value.get("row") is not None:
            placement["row"] = _as_int(
                value.get("row"),
                low=PLACEMENT_ROW_MIN,
                high=PLACEMENT_ROW_MAX,
                field="row",
            )
        if value.get("rowSpan") is not None:
            placement["rowSpan"] = _as_int(
                value.get("rowSpan"),
                low=PLACEMENT_ROW_SPAN_MIN,
                high=PLACEMENT_ROW_SPAN_MAX,
                field="rowSpan",
            )
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
            if align not in PLACEMENT_ALIGNMENTS:
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
        "x": _as_int(
            value.get("x", 0),
            low=PLACEMENT_ABSOLUTE_MIN,
            high=PLACEMENT_ABSOLUTE_MAX,
            field="x",
        ),
        "y": _as_int(
            value.get("y", 0),
            low=PLACEMENT_ABSOLUTE_MIN,
            high=PLACEMENT_ABSOLUTE_MAX,
            field="y",
        ),
        "width": _as_int(
            value.get("width", 100),
            low=PLACEMENT_COLUMN_MIN,
            high=PLACEMENT_ABSOLUTE_MAX,
            field="width",
        ),
        "height": _as_int(
            value.get("height", 100),
            low=PLACEMENT_COLUMN_MIN,
            high=PLACEMENT_ABSOLUTE_MAX,
            field="height",
        ),
        "zIndex": _as_int(
            value.get("zIndex", 0),
            low=PLACEMENT_ZINDEX_MIN,
            high=PLACEMENT_ZINDEX_MAX,
            field="zIndex",
        ),
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
        style["fontSize"] = _length(
            value["fontSize"],
            field="fontSize",
            low=STYLE_FONT_SIZE_MIN,
            high=STYLE_FONT_SIZE_MAX,
        )
    if value.get("fontWeight") is not None:
        style["fontWeight"] = _as_int(
            value["fontWeight"],
            low=STYLE_FONT_WEIGHT_MIN,
            high=STYLE_FONT_WEIGHT_MAX,
            field="fontWeight",
        )
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
        style["lineHeight"] = _length(
            value["lineHeight"],
            field="lineHeight",
            low=STYLE_LINE_HEIGHT_MIN,
            high=STYLE_LINE_HEIGHT_MAX,
        )
    if value.get("padding") is not None:
        style["padding"] = _length(value["padding"], field="padding")
    if value.get("margin") is not None:
        style["margin"] = _length(
            value["margin"],
            field="margin",
            low=STYLE_MARGIN_MIN,
            high=STYLE_MARGIN_MAX,
        )
    if value.get("border") is not None:
        border = value["border"]
        if not isinstance(border, dict):
            raise PresentationError("out_of_range", "边框参数格式不对。")
        for key in border:
            if key not in ("width", "style", "color"):
                raise PresentationError("invalid_operation", f"边框不支持 {key!r}。")
        style["border"] = {
            "width": _as_int(
                border.get("width", 1),
                low=STYLE_BORDER_WIDTH_MIN,
                high=STYLE_BORDER_WIDTH_MAX,
                field="border.width",
            ),
            "style": str(border.get("style") or "SOLID").upper(),
            "color": str(border.get("color") or "#e2e8f0")[:64],
        }
        if style["border"]["style"] not in ALLOWED_BORDER_STYLE:
            raise PresentationError("out_of_range", "边框线型取值不对。")
    if value.get("borderRadius") is not None:
        style["borderRadius"] = _as_int(
            value["borderRadius"],
            low=STYLE_BORDER_RADIUS_MIN,
            high=STYLE_BORDER_RADIUS_MAX,
            field="borderRadius",
        )
    if value.get("opacity") is not None:
        opacity = value["opacity"]
        if isinstance(opacity, bool) or not isinstance(opacity, (int, float)):
            raise PresentationError("out_of_range", "透明度必须是数字。")
        if opacity < STYLE_OPACITY_MIN or opacity > STYLE_OPACITY_MAX:
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
        fmt["precision"] = _as_int(
            value["precision"],
            low=FORMAT_PRECISION_MIN,
            high=FORMAT_PRECISION_MAX,
            field="precision",
        )
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
#:
#: 项目既有的一套（第一批）之外，**协议 ``FormatSpec.roundingMode``** 的
#: ``HALF_EVEN`` / ``DOWN`` 也认 —— 两套取值域并���，不改既有行为。
_ROUNDING_MODES = {
    "HALF_UP": ROUND_HALF_UP,
    "HALF_DOWN": ROUND_HALF_DOWN,
    "FLOOR": ROUND_FLOOR,
    "CEIL": ROUND_CEILING,
    "HALF_EVEN": ROUND_HALF_EVEN,
    "DOWN": ROUND_DOWN,
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


def _percent_ratio(value: Any) -> Any:
    """百分比的无量纲比率 → 百分数（只用于**显示**，底层值不动）。

    认不出来就原样返回 —— 不猜、不补 100。
    """
    try:
        number = Decimal(str(value)) * 100
    except Exception:  # noqa: BLE001 — 不是数字就交给原来的格式化分支
        return value
    try:
        # 去掉 ``20.0`` 这种多出来的尾零（精度由格式自己再定）。
        return number.normalize()
    except Exception:  # noqa: BLE001
        return number


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
    - **百分比**：按**无量纲比率**显示（``0.2`` → ``20%``）——
      协议 ``FormatSpec.kind = PERCENT`` 的语义就是比率，不是百分数；
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
        if kind == "PERCENT":
            # 百分比 = 无量纲比率 × 100。底层值不动，只改显示。
            text = _format_number(_percent_ratio(value), spec)
        else:
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
        if not label and kind == "PERCENT" and "unitLabel" not in spec:
            # 百分比默认带 ``%``；**显式**给了 ``unitLabel``（含空串）就听它的。
            label = "%"
        if label:
            text = f"{text} {label}" if label != "%" else f"{text}%"
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


def _region_slots(
    root: dict[str, Any], region_id: str
) -> list[tuple[list[dict[str, Any]], int, dict[str, Any] | None, dict[str, Any]]]:
    """列出某个 id 在树里的**所有**位置。

    每项是 ``(所在列表, 下标, 父容器, 区域本身)`` —— 父容器为 ``None``
    表示它在根层。

    正常定义里 id 唯一；返回多项说明**同一个 id 出现了多次**：
    那就是"目标不唯一"，要澄清（``ambiguous_target``），不能自己挑一个。
    """
    slots: list[
        tuple[list[dict[str, Any]], int, dict[str, Any] | None, dict[str, Any]]
    ] = []
    target = str(region_id or "")
    if not target:
        return slots

    def walk(regions: Any, parent: dict[str, Any] | None) -> None:
        if not isinstance(regions, list):
            return
        for index, region in enumerate(regions):
            if not isinstance(region, dict):
                continue
            if str(region.get("id") or "") == target:
                slots.append((regions, index, parent, region))
            if is_container(region):
                walk(region.get("children"), region)

    walk(_root_regions(root), None)
    return slots


def _resolve_relative_move(
    root: dict[str, Any],
    region_id: str,
    after_region_id: Any,
    before_region_id: Any,
) -> tuple[str | None, int]:
    """把「移到某张卡后面 / 前面」解析成 ``(目标容器 id 或 None, 插入下标)``。

    **下标按"源卡已从树上摘出之后"算**（契约 N2）：
    源卡与参照卡**同容器**、且源卡在参照卡**前面**时，摘掉源卡会让参照卡
    前移一格 —— 所以参照卡的下标要减一。不减一就会插错位置
    （"移到 X 后面"变成"移到 X 后面那张的后面"）。

    参照卡在根层时目标容器为 ``None``（= 根层列表）。

    这份计算**在源卡还在树上时**做一次即可：规范化层与执行器用的是**同一个**
    函数，所以"候选里写的下标"与"真正执行的下标"必然一致。
    """
    after_id = str(after_region_id or "").strip()
    before_id = str(before_region_id or "").strip()
    if after_id and before_id:
        raise PresentationError(
            "invalid_operation",
            "移动参数重复：不能同时指定「移到某张卡后面」和「移到某张卡前面」。",
        )
    target_id = after_id or before_id
    if not target_id:
        raise PresentationError("invalid_operation", "相对位置需要指明参照卡片。")
    if target_id == region_id:
        raise PresentationError("invalid_operation", "不能把卡片移到它自己后面。")

    source_slots = _region_slots(root, region_id)
    if not source_slots:
        raise PresentationError("unknown_region", f"找不到这张卡片：{region_id}。")
    _source_list, source_index, _source_parent, source_region = source_slots[0]

    target_slots = _region_slots(root, target_id)
    if not target_slots:
        raise PresentationError("unknown_region", f"找不到要参照的卡片：{target_id}。")
    if len(target_slots) > 1:
        raise PresentationError(
            "ambiguous_target",
            f"有 {len(target_slots)} 张卡片用了同一个 id（{target_id}），"
            "没法确定要移到哪一张后面，请说得更具体些。",
        )
    siblings, target_index, parent, _target_region = target_slots[0]
    if parent is not None and not is_container(parent):
        raise PresentationError(
            "invalid_operation", "参照卡片所在的层级不是分组，无法按相对位置移动。"
        )
    # **循环挂接**：不能把容器挪到自己孩子后面。
    if target_id in _region_subtree_ids(source_region):
        raise PresentationError("cycle", "不能把分组移到它自己的子卡片后面。")
    # 源卡与参照卡同容器、且源卡在它前面 → 摘出源卡后参照卡会前移一格。
    if siblings is source_slots[0][0] and source_index < target_index:
        target_index -= 1
    index = target_index + 1 if after_id else target_index
    target_group_id = str(parent.get("id") or "") if parent is not None else ""
    return (target_group_id or None), index


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
    """把若干张卡片放进一个**新的分组**（容器）里：给 groupId、memberRegionIds，可选 name / arrangement。"""
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
    """改一个**分组**里卡片的排列：给 groupId 与 arrangement（列数 / 方向 / 间距）。"""
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
    """**移动**一张卡片：给 regionId，可选 targetGroupId + index，或 afterRegionId / beforeRegionId。"""
    # ``afterRegionId`` / ``beforeRegionId``：**相对位置**（契约 N2 / §1.3）。
    # 执行器**直接**认这两个参数 —— 即使有计划绕过规范化层，也不会再报
    # "不支持的参数：'afterRegionId'"。下标语义与规范化层**完全一致**
    # （同一个 ``_resolve_relative_move``）。
    _require_keys(
        params,
        ("regionId",),
        ("targetGroupId", "index", "afterRegionId", "beforeRegionId"),
    )
    region_id = str(params["regionId"])
    region = find_region(root, region_id)
    if region is None:
        raise PresentationError("unknown_region", f"找不到这张卡片：{region_id}。")
    after_id = str(params.get("afterRegionId") or "").strip()
    before_id = str(params.get("beforeRegionId") or "").strip()
    relative = bool(after_id or before_id)
    if relative and (
        params.get("targetGroupId") is not None or params.get("index") is not None
    ):
        raise PresentationError(
            "invalid_operation",
            "移动参数重复：不能同时给目标位置和「移到某张卡后面」。",
        )
    target_group = params.get("targetGroupId")
    if not relative and target_group is not None:
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

    if relative:
        # 相对位置：插入下标**在摘出之前**算好（摘出后列表会前移）。
        relative_group_id, index = _resolve_relative_move(
            root, region_id, after_id, before_id
        )
        moved = _detach(root, region_id)
        if moved is None:
            raise PresentationError("unknown_region", f"找不到这张卡片：{region_id}。")
        siblings = (
            _root_regions(root)
            if relative_group_id is None
            else _container_children(root, relative_group_id)
        )
        if siblings is None:
            raise PresentationError(
                "unknown_region", f"找不到目标分组：{relative_group_id}。"
            )
        position = _as_int(index, low=0, high=max(len(siblings), 0), field="index")
        siblings.insert(position, moved)
        affected.append(region_id)
        _record_change(
            changes, region_id, f"/regions/{region_id}", None, moved,
            "移动卡片（id 与统计口径不变）",
        )
        return

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
    """**解散分组**：给 groupId，里面的卡片回到原来的层级。"""
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
    insert_list, insert_at, parent = anchor
    arrangement = (
        (parent.get("component") or {}).get("arrangement") or {}
        if parent is not None else (root.get("page") or {}).get("arrangement") or {}
    )
    outer = (container.get("layout") or {}).get("placement") or {}
    # Group-local coordinates cannot be reused in the parent's grid.
    if str(arrangement.get("mode") or "GRID").upper() == "GRID":
        columns = int(arrangement.get("columns") or (1 if parent else 12))
        column = min(int(outer.get("column") or 1), columns)
        span = min(int(outer.get("columnSpan") or columns), columns - column + 1)
        # An automatic parent row must stay automatic after promotion.
        explicit_row = outer.get("row") is not None
        row = int(outer.get("row") or 1)
        old_end = row + int(outer.get("rowSpan") or 1)
        next_row = row
        for child in children:
            layout = child.setdefault("layout", {})
            before = copy.deepcopy(layout.get("placement"))
            height = int((before or {}).get("rowSpan") or 1)
            layout["placement"] = {
                "type": "GRID", "column": column, "columnSpan": span,
                "rowSpan": height,
            }
            if explicit_row:
                layout["placement"]["row"] = next_row
            next_row += height
            _record_change(changes, str(child.get("id") or ""),
                           f"/regions/{child.get('id')}/layout/placement", before,
                           layout["placement"], "取消分组后转换到父层定位")
        delta = max(0, next_row - old_end) if explicit_row else 0
        for sibling in insert_list[insert_at + 1:]:
            placement = (sibling.get("layout") or {}).get("placement") or {}
            if delta and placement.get("type") == "GRID" and int(placement.get("row") or 0) >= old_end:
                before = copy.deepcopy(placement)
                placement["row"] += delta
                sibling_id = str(sibling.get("id") or "")
                affected.append(sibling_id)
                _record_change(changes, sibling_id,
                               f"/regions/{sibling_id}/layout/placement", before,
                               placement, "取消分组后为子卡预留行")
    else:
        for child in children:
            layout = child.setdefault("layout", {})
            before = copy.deepcopy(layout.get("placement"))
            layout["placement"] = {"type": "FLOW"}
            if outer.get("type") == "FLOW" and outer.get("width"):
                layout["placement"]["width"] = copy.deepcopy(outer["width"])
            _record_change(changes, str(child.get("id") or ""),
                           f"/regions/{child.get('id')}/layout/placement", before,
                           layout["placement"], "取消分组后转换到父层定位")
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
    """**删除分组**：给 groupId；组里还有卡片时默认整批失败（force=true 才强制）。"""
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
    """**复制**一张卡片：给 regionId 与 newRegionId（不能与现有 id 重复）。"""
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
    """**删除**一张卡片：给 regionId；有引用方时默认整批失败（force=true 才强制）。"""
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
    """**隐藏**一张卡片（不删）：给 regionId。"""
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
    """**恢复显示**一张隐藏的卡片：给 regionId。"""
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
    """改**定位**：给 regionId 与 placement（GRID / FLOW / ABSOLUTE）。"""
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
    """改**外观**：给 regionId 与 style（字体 / 字号 / 字重 / 颜色 / 对齐 / 行高 / 内外边距 / 边框 / 圆角 / 透明度）。"""
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
    """改**数字显示格式**：给 regionId 与 format（kind / precision / unitLabel / thousandSeparator / dateTimePattern / nullText / roundingMode）。"""
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
    """改**整页设置**：给 webMaxWidth 与 / 或 arrangement。"""
    _require_keys(params, (), ("webMaxWidth", "arrangement"))
    page = root.get("page")
    if not isinstance(page, dict):
        page = {}
        root["page"] = page
    if params.get("webMaxWidth") is not None:
        width = _length(
            params["webMaxWidth"],
            field="webMaxWidth",
            low=PAGE_WIDTH_MIN,
            high=PAGE_WIDTH_MAX,
        )
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
    """改**报表元数据**：给 name / description / tags。"""
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
    """把一个**共享主题**应用到这份报表：给 themeId（取自 availableThemes）。"""
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


# --------------------------------------------------------------------------
# 规范计划（canonical plan）：把松散参数规范成执行器认得的形状
# --------------------------------------------------------------------------
#
# 为什么要有这一层：模型 / 规则给出的参数是**松散**的（``fontSize: 28``、
# ``gap: 16``、``afterRegionId: "region_x"``），而执行器只认**一种**写法。
# 缺了这一层，非法参数会一路走到"用户点了接受修改"才炸
# （BUG-001 字号 / BUG-002 移动 / BUG-004 间距 / BUG-006 层级）。
#
# 这一层只做**语义保持**的规范化：把等价的写法换成执行器要的那个形状。
# 认不出来的语义**抛 PresentationError**，绝不静默丢弃字段。


#: 长度类字段（契约 N1）：统一成 ``{"value": n, "unit": "PX"}``。
_LENGTH_STYLE_FIELDS = ("fontSize", "lineHeight", "padding", "margin")

#: 定位里的长度字段（契约 N1）：只有 GRID / FLOW 有（ABSOLUTE 的 width /
#: height 是**整数像素**，那是执行器的口径，不能动）。
_LENGTH_PLACEMENT_FIELDS = ("width", "minHeight")

#: 长度只允许这两种单位；其它单位报错，不猜。
_ALLOWED_LENGTH_UNITS = LENGTH_UNITS

#: 排列参数里的键（契约 N4）。
_ARRANGEMENT_KEYS = ARRANGEMENT_KEYS

#: 排列字段出现在 **placement** 里 = 层级错（契约 N3）。
_ARRANGEMENT_MARKERS = ("columns", "mode", "direction", "gap")

#: 主题同义词（契约 N5）：用户说的是「暗色」，内置主题名是「深色主题」。
#:
#: 这不是**另一份主题清单** —— 主题本身仍然只来自主题服务
#: （``list_themes``）；这里只是"用户说法 → 内置 id"的对照表，
#: 命中的 id 必须在 ``list_themes`` 里真的存在才用。
_THEME_SYNONYMS = {
    "暗色": "theme_dark",
    "深色": "theme_dark",
    "dark": "theme_dark",
    "浅色": "theme_default",
    "默认": "theme_default",
    "light": "theme_default",
    "default": "theme_default",
    "柔和": "theme_soft",
    "soft": "theme_soft",
    "打印": "theme_print",
    "print": "theme_print",
}

#: ``"28px"`` / ``"28"`` / ``"28%"`` 的解析：数值 + 可选单位。
_LENGTH_PATTERN = re.compile(r"^(-?\d+(?:\.\d+)?)\s*([a-z%]*)$", re.IGNORECASE)


def _normalize_length(value: Any, *, field: str) -> dict[str, Any]:
    """把长度写法统一成 ``{"value": n, "unit": "PX"}``（契约 N1）。

    接受：``28``（数字）/ ``"28px"`` / ``"28"`` / ``{"value": 28}``。
    单位只允许 **PX / PERCENT** —— 其它单位报错，不猜。

    长度对象里除 ``value`` / ``unit`` 外的键执行器本来就不读，规范化后按
    规范形状输出（不引入新语义）。
    """
    if isinstance(value, dict):
        if "value" not in value:
            raise PresentationError("out_of_range", f"{field} 的取值格式不对。")
        number = value["value"]
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise PresentationError("out_of_range", f"{field} 的取值格式不对。")
        unit = str(value.get("unit") or "PX").upper()
        if unit not in _ALLOWED_LENGTH_UNITS:
            raise PresentationError(
                "out_of_range", f"{field} 的单位只能是 PX 或 PERCENT。"
            )
        return {"value": number, "unit": unit}
    if isinstance(value, bool):
        raise PresentationError("out_of_range", f"{field} 的取值格式不对。")
    if isinstance(value, (int, float)):
        return {"value": value, "unit": "PX"}
    if isinstance(value, str):
        match = _LENGTH_PATTERN.match(value.strip())
        if match is None:
            raise PresentationError("out_of_range", f"{field} 的取值格式不对。")
        number_text, unit_text = match.group(1), match.group(2).lower()
        unit = {"": "PX", "px": "PX", "%": "PERCENT", "percent": "PERCENT"}.get(unit_text)
        if unit is None:
            raise PresentationError(
                "out_of_range", f"{field} 的单位只能是 PX 或 PERCENT。"
            )
        number = int(number_text) if "." not in number_text else float(number_text)
        return {"value": number, "unit": unit}
    raise PresentationError("out_of_range", f"{field} 的取值格式不对。")


def _normalize_arrangement(value: Any, *, field: str = "arrangement") -> dict[str, Any]:
    """排列参数规范化（契约 N1 + N4）。

    ``gap`` 走 N1；``columns`` 整数 1..12；未知键报错。

    规范化结果与执行器 ``_validate_arrangement`` 的输出**逐字一致** ——
    候选摘要与真正执行的是同一份参数（契约 §1.4）。
    """
    if not isinstance(value, dict):
        raise PresentationError("out_of_range", "排列参数格式不对。")
    for key in value:
        if key not in _ARRANGEMENT_KEYS:
            raise PresentationError("invalid_operation", f"排列参数不支持 {key!r}。")
    normalized = dict(value)
    if normalized.get("gap") is not None:
        normalized["gap"] = _normalize_length(normalized["gap"], field="gap")
    # 用执行器自己的校验收口：取值域（列数 1..12、模式、方向）与默认值
    # 只有一处定义 —— 规范化与执行不会漂。
    return _validate_arrangement(normalized)


def _normalize_style(value: Any) -> dict[str, Any]:
    """样式参数规范化（契约 N1 + N7）。

    长度类字段统一成 ``{"value","unit"}``；**未知键照样报错** ——
    白名单与执行器共用同一份（``THEME_MANAGED_STYLE_KEYS``），不会漂。
    """
    if not isinstance(value, dict):
        raise PresentationError("out_of_range", "样式参数格式不对。")
    for key in value:
        if key not in THEME_MANAGED_STYLE_KEYS:
            raise PresentationError(
                "invalid_operation", f"样式不支持 {key!r}（只接受受控的外观属性）。"
            )
    normalized = dict(value)
    for key in _LENGTH_STYLE_FIELDS:
        if normalized.get(key) is not None:
            normalized[key] = _normalize_length(normalized[key], field=key)
    return normalized


def _normalize_placement(value: Any) -> dict[str, Any]:
    """定位参数规范化（契约 N1）。

    只有 GRID / FLOW 的 ``width`` / ``minHeight`` 是**长度**；
    ABSOLUTE 的 width / height 是**整数像素**（执行器口径），不动 ——
    把它改成长度对象反而会让执行器报错。
    """
    if not isinstance(value, dict):
        raise PresentationError("out_of_range", "定位参数格式不对。")
    kind = str(value.get("type") or "").upper()
    if kind not in ("GRID", "FLOW"):
        return dict(value)
    normalized = dict(value)
    for key in _LENGTH_PLACEMENT_FIELDS:
        if normalized.get(key) is not None:
            normalized[key] = _normalize_length(normalized[key], field=key)
    return normalized


def _strip_theme_suffix(text: str) -> str:
    """去掉主题名末尾的「主题 / theme」：「深色主题」→「深色」。"""
    value = str(text or "").strip()
    for suffix in ("主题", "theme"):
        if value.lower().endswith(suffix) and len(value) > len(suffix):
            return value[: -len(suffix)]
    return value


def _resolve_theme_id(value: Any) -> str:
    """把主题**名字**解析成真实 themeId（契约 N5）。

    唯一主题来源是主题服务（``list_themes``）—— 这里只额外加一张
    **同义词表**（用户说「暗色」，内置主题名是「深色主题」）。
    解析不到就抛 ``unknown_theme``（候选期澄清，不是执行期炸）。
    """
    theme_id = str(value or "").strip()
    if not theme_id:
        raise PresentationError("invalid_operation", "主题需要一个 id。")
    themes = theme_service.list_themes()
    known_ids = {str(item.get("themeId") or "") for item in themes}
    # 1) 已经是真实 id → 原样用。
    if theme_id in known_ids:
        return theme_id
    # 2) 按**主题名**匹配（「深色主题」）。
    for item in themes:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        if theme_id == name or theme_id == _strip_theme_suffix(name):
            return str(item.get("themeId") or "")
    # 3) 同义词（「暗色」→ theme_dark）。命中的 id 必须**真的存在**。
    synonym = _THEME_SYNONYMS.get(theme_id.lower())
    if not synonym:
        synonym = _THEME_SYNONYMS.get(_strip_theme_suffix(theme_id).lower())
    if synonym and synonym in known_ids:
        return synonym
    names = "、".join(
        str(item.get("name") or item.get("themeId") or "") for item in themes
    )
    raise PresentationError(
        "unknown_theme", f"没有这个主题：{theme_id}。可选：{names}。"
    )


def _arrangement_from_placement(placement: dict[str, Any]) -> dict[str, Any]:
    """从 placement 里取出**排列**部分（契约 N3）。

    ``type`` 等价于 ``mode``（GRID / FLOW）；其余定位字段（column / row /
    align / x / width …）与排列混在一起时**报错** —— 静默丢掉字段是不允许的
    （契约 N7）。
    """
    arrangement: dict[str, Any] = {}
    leftover: list[str] = []
    for key, value in placement.items():
        if key in ("columns", "direction", "gap"):
            arrangement[key] = value
        elif key == "mode":
            arrangement["mode"] = value
        elif key == "type":
            mode = str(value or "").upper()
            if mode not in ALLOWED_ARRANGEMENT_MODES:
                raise PresentationError(
                    "invalid_operation",
                    "定位方式不能转成分组排列（只有 GRID / FLOW 可以）。",
                )
            if "mode" in arrangement and str(arrangement["mode"] or "").upper() != mode:
                raise PresentationError(
                    "invalid_operation", "定位方式与排列方式冲突，说清楚要哪一种。"
                )
            arrangement["mode"] = mode
        else:
            leftover.append(key)
    if leftover:
        raise PresentationError(
            "invalid_operation",
            f"定位参数与排列参数混在一起：{'、'.join(sorted(leftover))}。"
            "请只说列数 / 间距 / 排列方式。",
        )
    return arrangement


def _normalize_move_operation(
    params: dict[str, Any], root: dict[str, Any]
) -> dict[str, Any]:
    """MOVE_REGION 的相对位置解析（契约 N2）。

    ``afterRegionId`` / ``beforeRegionId`` → ``targetGroupId`` + ``index``；
    **不允许把 afterRegionId 原样穿透给执行器**。
    """
    after_id = str(params.get("afterRegionId") or "").strip()
    before_id = str(params.get("beforeRegionId") or "").strip()
    new_params = {
        key: value
        for key, value in params.items()
        if key not in ("afterRegionId", "beforeRegionId")
    }
    if not after_id and not before_id:
        return {"op": "MOVE_REGION", "params": new_params}
    if params.get("targetGroupId") is not None or params.get("index") is not None:
        raise PresentationError(
            "invalid_operation",
            "移动参数重复：不能同时给目标位置和「移到某张卡后面」。",
        )
    region_id = str(params.get("regionId") or "").strip()
    if not region_id:
        raise PresentationError("invalid_operation", "移动需要指明要移动哪张卡片。")
    target_group_id, index = _resolve_relative_move(root, region_id, after_id, before_id)
    new_params["index"] = index
    if target_group_id:
        new_params["targetGroupId"] = target_group_id
    return {"op": "MOVE_REGION", "params": new_params}


def _normalize_placement_operation(
    op: str, params: dict[str, Any], root: dict[str, Any]
) -> dict[str, Any]:
    """SET_PLACEMENT 的层级纠正（契约 N3）。

    ``placement`` 里出现 ``columns`` / ``mode`` / ``direction`` / ``gap``
    = 层级错：

    - 目标是**容器** → 整条转成 ``SET_GROUP_ARRANGEMENT {groupId, arrangement}``；
    - 目标是**叶子卡** → 抛 ``arrangement_not_placement``（**不许静默删字段**）。
    """
    placement = params.get("placement")
    if not isinstance(placement, dict):
        # 形状不对交给执行器报错（它的文案更贴上下文）。
        return {"op": op, "params": dict(params)}
    markers = [key for key in _ARRANGEMENT_MARKERS if key in placement]
    if not markers:
        return {
            "op": op,
            "params": {**params, "placement": _normalize_placement(placement)},
        }
    region_id = str(params.get("regionId") or "").strip()
    region = find_region(root, region_id) if region_id else None
    if region is None:
        raise PresentationError("unknown_region", f"找不到这张卡片：{region_id}。")
    if not is_container(region):
        raise PresentationError(
            "arrangement_not_placement",
            f"「{region.get('name') or region_id}」是单张卡片，"
            f"{'、'.join(markers)} 是**分组**的属性，不能设在这张卡上。"
            "要改它的位置请说「移到…」，要改排列请先选一个分组。",
        )
    arrangement = _arrangement_from_placement(placement)
    return {
        "op": "SET_GROUP_ARRANGEMENT",
        "params": {
            "groupId": region_id,
            "arrangement": _normalize_arrangement(arrangement),
        },
    }


def _normalize_one_operation(raw: Any, root: dict[str, Any]) -> dict[str, Any]:
    """规范化**一条**操作（契约 §1.2 的 N1–N7）。"""
    if not isinstance(raw, dict):
        raise PresentationError("invalid_operation", "展示操作格式不对。")
    op = str(raw.get("op") or "")
    if op not in ALLOWED_OPERATIONS:
        raise PresentationError(
            "invalid_operation", f"还不支持这个展示操作：{op or '(空)'}。"
        )
    params = raw.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise PresentationError("invalid_operation", "展示操作参数格式不对。")
    # 安全值检查**不放松**：脚本 / 任意 CSS 文本照样拒。
    _reject_unsafe(params)

    if op == "MOVE_REGION":
        return _normalize_move_operation(params, root)

    if op == "SET_PLACEMENT":
        return _normalize_placement_operation(op, params, root)

    if op == "SET_STYLE":
        new_params = dict(params)
        if params.get("style") is not None:
            new_params["style"] = _normalize_style(params["style"])
        return {"op": op, "params": new_params}

    if op in ("SET_GROUP_ARRANGEMENT", "CREATE_GROUP"):
        new_params = dict(params)
        if params.get("arrangement") is not None:
            new_params["arrangement"] = _normalize_arrangement(params["arrangement"])
        return {"op": op, "params": new_params}

    if op == "SET_PAGE":
        new_params = dict(params)
        if params.get("webMaxWidth") is not None:
            new_params["webMaxWidth"] = _normalize_length(
                params["webMaxWidth"], field="webMaxWidth"
            )
        if params.get("arrangement") is not None:
            new_params["arrangement"] = _normalize_arrangement(params["arrangement"])
        return {"op": op, "params": new_params}

    if op == "SET_THEME":
        new_params = dict(params)
        new_params["themeId"] = _resolve_theme_id(params.get("themeId"))
        return {"op": op, "params": new_params}

    if op == "SET_FORMAT":
        new_params = dict(params)
        fmt = params.get("format")
        if isinstance(fmt, dict):
            for key in fmt:
                if key not in ALLOWED_FORMAT_KEYS:
                    raise PresentationError(
                        "invalid_operation", f"显示格式不支持 {key!r}。"
                    )
        return {"op": op, "params": new_params}

    return {"op": op, "params": dict(params)}


def normalize_presentation_operations(
    operations: list[dict[str, Any]],
    definition: ReportDefinitionDraft,
) -> list[dict[str, Any]]:
    """把模型 / 规则给出的松散参数规范成执行器认得的形状（契约 §1.2）。

    **只做语义保持的规范化**：把等价的写法换成执行器要的那个形状。
    认不出来的语义**抛 PresentationError**，绝不静默丢弃。

    规则（契约 N1–N7）：

    - **N1** 长度类字段统一成 ``{"value": n, "unit": "PX"}``；
    - **N2** ``MOVE_REGION`` 的相对位置 → ``targetGroupId`` + ``index``；
    - **N3** placement 里出现排列字段 = 层级错（容器则转 op，叶子卡则报错）；
    - **N4** 排列参数：``gap`` 走 N1、``columns`` 1..12、未知键报错；
    - **N5** ``SET_THEME`` 的主题**名字** → 真实 themeId；
    - **N6** ``SET_FORMAT`` 只认 ``ALLOWED_FORMAT_KEYS``；
    - **N7** 未知 op / 未知参数键**仍然报错** —— 规范化不放松校验。

    **幂等**：对已经规范化的结果再跑一遍，结果不变 —— 所以调用方可以
    「先规范化再预检」，预检里再规范化一次也不怕。

    **无副作用**：不改传入的 definition（定位判断都在副本上做）。
    """
    if not isinstance(operations, list):
        raise PresentationError("invalid_operation", "展示操作格式不对。")
    # 在**副本**上定位：不许改调用方传进来的定义。
    root = copy.deepcopy(_as_dict(definition))
    return [_normalize_one_operation(raw, root) for raw in operations]


@dataclass
class PreflightResult:
    """预检结果（契约 §1.1）。

    - ``ok``：整批是否可执行；
    - ``operations``：**规范化后**的那一批（= 将来真正执行的那一批）；
    - ``failure``：面向用户的中文失败 / 澄清理由；
    - ``reason``：机器可读码（``out_of_range`` / ``invalid_operation`` /
      ``unknown_region`` / ``arrangement_not_placement`` / ``unknown_theme`` /
      ``ambiguous_target`` …）；
    - ``affectedRegionIds``：预检会影响的区域；
    - ``changes``：ChangeSet.changed（摘要可用）。
    """

    ok: bool
    operations: list[dict[str, Any]] = field(default_factory=list)
    failure: str | None = None
    reason: str | None = None
    affectedRegionIds: list[str] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)


def preflight_presentation(
    definition: ReportDefinitionDraft,
    operations: list[dict[str, Any]],
) -> PreflightResult:
    """在**副本**上把整批跑一遍（契约 §1.1）。

    **无副作用**：不落库、不产生撤销单位、不改传入的 definition。

    失败时 ``ok=False`` 且 ``failure`` 给出可直接给用户看的中文理由；
    成功时 ``operations`` 是**规范化后的那一份**（不是入参那一份）——
    候选摘要与执行器真正跑的参数是同一份数据（契约 §1.4）。
    """
    try:
        normalized = normalize_presentation_operations(operations, definition)
    except PresentationError as exc:
        return PreflightResult(ok=False, failure=exc.message, reason=exc.reason)
    except Exception as exc:  # noqa: BLE001 —— 预检**绝不能**把异常甩给调用方
        # 规范化会读主题服务（解析主题名），那一步可能碰到**存储层**的异常
        # （数据库不可用等）。预检是候选期的**闸门**：它抛出去，整个请求就炸了 ——
        # 而这一批本来只是"参数不合法"，应该变成一句澄清。
        logger.warning("[report_presentation] 预检规范化异常：%s", type(exc).__name__)
        return PreflightResult(
            ok=False,
            failure="这次修改的参数暂时没法校验，草稿没有变化，也没有生成待接受的修改计划。",
            reason="invalid_operation",
        )

    try:
        working = ReportDefinitionDraft.model_validate(
            copy.deepcopy(_as_dict(definition))
        )
    except (PresentationError, ValidationError) as exc:
        message = getattr(exc, "message", "") or "报表定义读不出来。"
        reason = getattr(exc, "reason", "") or "invalid_definition"
        return PreflightResult(ok=False, failure=message, reason=reason)
    except Exception as exc:  # noqa: BLE001 —— 同上：预检不抛异常
        logger.warning("[report_presentation] 预检读取定义异常：%s", type(exc).__name__)
        return PreflightResult(
            ok=False,
            failure="报表定义暂时读不出来，草稿没有变化，也没有生成待接受的修改计划。",
            reason="invalid_definition",
        )

    # 执行器本身是纯计算（内部深拷贝、失败不留半份）——
    # 这里再传一份副本，保证"预检绝不碰调用方的定义"。
    try:
        result = apply_presentation(working, normalized)
    except Exception as exc:  # noqa: BLE001 —— 预检是闸门，不抛异常
        logger.warning("[report_presentation] 预检执行异常：%s", type(exc).__name__)
        return PreflightResult(
            ok=False,
            operations=normalized,
            failure="这次修改没法执行，草稿没有变化，也没有生成待接受的修改计划。",
            reason="invalid_operation",
        )
    if not result.ok:
        return PreflightResult(
            ok=False,
            operations=normalized,
            failure=result.failure or "这次修改没有生效，草稿保持不变。",
            reason=result.reason or "invalid_operation",
        )
    change_set = result.changeSet
    changes = [
        entry.model_dump() for entry in (change_set.changed if change_set else [])
    ]
    return PreflightResult(
        ok=True,
        operations=normalized,
        failure=None,
        reason=None,
        affectedRegionIds=list(result.affectedRegionIds),
        changes=changes,
    )
