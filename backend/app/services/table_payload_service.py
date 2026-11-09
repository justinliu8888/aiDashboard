"""表格载荷装饰：把**定义**（列 / 分组 / 分页 / 冻结 / 汇总）落到载荷上。

**为什么有这一层**：表格的「显示什么」有两份事实 ——
定义里的 ``component.columns``（**定义之记录**：列的集合、顺序、标题、宽度、
表头路径、汇总引用）与载荷里的 ``columns``（**运行时产物**：由造数器按
``previewSpec`` 与组件列合并生成，还带着语义描述）。

两份事实不一致时，用户看到的表格就与用户改的不一致（本批 TB01 的原缺陷：
预览列留在表里，删不掉）。这里把两者**合成一份**：

* 列的**集合与顺序**取定义（定义没声明列时保持载荷原样，向后兼容）；
* 载荷那一侧带来的**语义描述**（``semanticKind`` / ``valueType`` /
  ``deterministicScope``）与**显示模式标记**（``displayMode`` /
  ``bindingKind``）继续跟着列走；
* ``displayRows`` 按**可见行**重算（**列格式优先于区域格式**）。

**这个模块是纯变换**：不取数、不存库、不调模型、不改任何原始数值。
分组（``groupRowsBy``）**不在这里算**：它只是前端怎么排版的事，
**不产生小计、不产生聚合**。

**汇总值由调用方给**：``ctx["summaryValues"]`` 里是
``{"<outputRef>|<fieldRef>|<basis>": value}``。**没有对应键就不显示数字** ——
绝不自己求和、绝不编一个数（口径不一致时宁可空着，也要让人看得见）。
"""
from __future__ import annotations

from typing import Any

from app.services.report_presentation_service import (
    format_display_value,
    region_authoritative_format,
)


#: 列上跟着走的那几个**语义描述 / 显示模式**键（载荷那一侧带来的）。
INHERITED_COLUMN_KEYS = (
    "semanticKind",
    "valueType",
    "deterministicScope",
    "displayMode",
    "bindingKind",
)

#: 汇总口径的中文说法（前端与后端共用同一份）。
SUMMARY_BASIS_LABELS: dict[str, str] = {
    "FULL_RESULT": "全量口径",
    "VISIBLE_ROWS": "可见行口径",
}

#: 汇总值缺失时的说明（**不给数字**）。
SUMMARY_MISSING_NOTE = "当前没有与该口径一致的已登记汇总输出"


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _component(region: dict[str, Any]) -> dict[str, Any]:
    component = region.get("component") if isinstance(region, dict) else None
    return component if isinstance(component, dict) else {}


def _definition_columns(region: dict[str, Any]) -> list[dict[str, Any]]:
    """定义里声明的列（**定义之记录**）。"""
    return [
        column
        for column in (_component(region).get("columns") or [])
        if isinstance(column, dict)
    ]


def _base_columns(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """载荷里带来的列（带语义描述与显示模式标记）。"""
    columns = payload.get("columns") if isinstance(payload, dict) else None
    return [column for column in (columns or []) if isinstance(column, dict)]


# --------------------------------------------------------------------------
# 列：定义 × 载荷
# --------------------------------------------------------------------------

def merge_table_columns(
    region: dict[str, Any], payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """定义里的列与载荷里的列**合成一份**。

    * 定义声明了列 → **列的集合与顺序取定义**（载荷里多出来的预览列丢掉）；
      每一列再从载荷那一侧**继承**语义描述与显示模式标记。
    * 定义**没**声明列 → 保持载荷原样（第一批的表格照旧渲染）。
    """
    definition_columns = _definition_columns(region)
    base_columns = _base_columns(payload)
    if not definition_columns:
        return [dict(column) for column in base_columns]

    base_by_field: dict[str, dict[str, Any]] = {}
    base_by_id: dict[str, dict[str, Any]] = {}
    for column in base_columns:
        field_ref = str(column.get("fieldRef") or "").strip()
        if field_ref and field_ref not in base_by_field:
            base_by_field[field_ref] = column
        column_id = str(column.get("id") or "").strip()
        if column_id and column_id not in base_by_id:
            base_by_id[column_id] = column

    merged: list[dict[str, Any]] = []
    for column in definition_columns:
        field_ref = str(column.get("fieldRef") or "").strip()
        column_id = str(column.get("id") or "").strip()
        # 先按 fieldRef 找，再按 id 找（**不猜**：两个都没命中就没有可继承的）。
        base = base_by_field.get(field_ref) or base_by_id.get(column_id) or {}
        merged_column: dict[str, Any] = {
            "id": column_id or None,
            "label": str(column.get("label") or ""),
            "fieldRef": field_ref,
        }
        for key in INHERITED_COLUMN_KEYS:
            value = base.get(key)
            if value is not None:
                merged_column[key] = value
        # 格式：定义的优先；定义没写才沿用载荷那一侧的。
        if isinstance(column.get("format"), dict):
            merged_column["format"] = dict(column["format"])
        elif isinstance(base.get("format"), dict):
            merged_column["format"] = dict(base["format"])
        else:
            merged_column["format"] = {}
        # 列宽 / 列样式 / 多层表头路径：**只在声明了的时候**才带上。
        for key in ("width", "style", "headerPath"):
            if column.get(key) is not None:
                merged_column[key] = column[key]
        # **只有一层、而且这一层就是列标题**的表头路径 = 没有多层表头。
        #
        # 前端按表头路径排出多层表头：路径有几层，表头就有几行（最后一层
        # 是列标题）。所以「单层路径 + 同样的文字」会让表头**多出一行
        # 重复的文字**（实测：清除多层表头时模型给每列写了一条同名的
        # 单层路径，页面上表头变成两行「时间 / 时间」）。
        #
        # 这里把它当成「没有多层表头」处理：这一列占满整个表头高度。
        path = merged_column.get("headerPath")
        if isinstance(path, list):
            levels = [str(level).strip() for level in path if str(level or "").strip()]
            label = str(column.get("label") or "").strip()
            if len(levels) <= 1 and (not levels or (label and levels[0] == label)):
                merged_column.pop("headerPath", None)
        merged.append(merged_column)
    return merged


# --------------------------------------------------------------------------
# 显示串：列格式优先于区域格式
# --------------------------------------------------------------------------

def _display_cell(
    value: Any, region_format: dict[str, Any], column_format: dict[str, Any]
) -> Any:
    """一格的显示串：**列级优先**，列没声明的键回落到区域级。

    两边都没有格式 → **原值**（这一列没有格式约定，就显示原值）。
    与 ``simulated_data_service._display_cell`` 同一口径。
    """
    effective = {**region_format, **column_format}
    if not effective:
        return value
    return format_display_value(value, effective)


def build_display_rows(
    region: dict[str, Any],
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按**可见行**算逐格显示串（与可见行**平行**，按 fieldRef 取）。"""
    region_format = region_authoritative_format(region)
    column_formats = {
        str(column.get("fieldRef") or ""): (
            column.get("format") if isinstance(column.get("format"), dict) else {}
        )
        for column in columns
    }
    return [
        {
            str(key): _display_cell(
                value, region_format, column_formats.get(str(key), {})
            )
            for key, value in row.items()
        }
        for row in rows
        if isinstance(row, dict)
    ]


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------

def summary_value_key(output_ref: str, field_ref: str, basis: str) -> str:
    """汇总值在 ``ctx["summaryValues"]`` 里的键。"""
    return f"{output_ref}|{field_ref}|{basis}"


def resolve_table_summaries(
    region: dict[str, Any],
    columns: list[dict[str, Any]],
    summary_values: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把 ``component.summaries`` 解析成可显示的汇总行。

    返回 ``(汇总行, 说明)``：

    * **有**对应口径的已登记输出值 → 显示数值与口径；
    * **没有** → **不显示数字**（``value`` 为 ``None``、``displayValue`` 为空串），
      并给出一句说明 —— 让口径不一致**看得见**，而不是显示一个陈旧的数。
    """
    values = summary_values if isinstance(summary_values, dict) else {}
    region_format = region_authoritative_format(region)
    formats_by_field = {
        str(column.get("fieldRef") or ""): (
            column.get("format") if isinstance(column.get("format"), dict) else {}
        )
        for column in columns
    }
    resolved: list[dict[str, Any]] = []
    notes: list[str] = []
    for summary in _component(region).get("summaries") or []:
        if not isinstance(summary, dict):
            continue
        label = str(summary.get("label") or "")
        output_ref = str(summary.get("outputRef") or "")
        field_ref = str(summary.get("fieldRef") or "")
        basis = str(summary.get("basis") or "").strip().upper()
        key = summary_value_key(output_ref, field_ref, basis)
        value = values.get(key, None)
        if key not in values or value is None:
            resolved.append(
                {
                    "label": label,
                    "value": None,
                    "displayValue": "",
                    "basis": basis,
                    "basisLabel": SUMMARY_BASIS_LABELS.get(basis, basis),
                    "outputRef": output_ref,
                    "fieldRef": field_ref,
                    "available": False,
                    "note": SUMMARY_MISSING_NOTE,
                }
            )
            notes.append(
                f"汇总「{label}」{SUMMARY_BASIS_LABELS.get(basis, basis)}"
                f"没有对应的已登记汇总输出，未显示数值。"
            )
            continue
        column_format = formats_by_field.get(field_ref, {})
        effective = {**region_format, **column_format}
        display = (
            format_display_value(value, effective)
            if effective
            else format_display_value(value, {})
        )
        resolved.append(
            {
                "label": label,
                "value": value,
                "displayValue": display,
                "basis": basis,
                "basisLabel": SUMMARY_BASIS_LABELS.get(basis, basis),
                "outputRef": output_ref,
                "fieldRef": field_ref,
                "available": True,
                "note": "",
            }
        )
    return resolved, notes


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def decorate_table_payload(
    region: dict[str, Any], payload: dict[str, Any], ctx: dict[str, Any] | None = None
) -> dict[str, Any]:
    """把表格的定义（列 / 分组 / 分页 / 冻结 / 汇总）落到载荷上。

    ``ctx`` = ``{"visibleRows": [...], "fullRows": [...], "summaryValues": {...}}``：

    * ``visibleRows`` —— 走完展示处理（别名 → 过滤 → 排序 → 前 N 项）后
      **真正显示**的那批行；
    * ``fullRows``   —— 展示处理**之前**的全量行；
    * ``summaryValues`` —— ``{"<outputRef>|<fieldRef>|<basis>": value}``
      （**由后端算好**；这里**不自己求和、不编数字**）。

    返回**同一个**载荷对象（就地装饰）。
    """
    if not isinstance(payload, dict):
        return payload
    options = ctx if isinstance(ctx, dict) else {}
    component = _component(region)

    columns = merge_table_columns(region, payload)
    aliases = (region.get("binding") or {}).get("fieldAliases") or {}
    by_source = {actual: display for display, actual in aliases.items()}
    columns = [{**column, "label": by_source.get(column.get("fieldRef"), column.get("label"))}
               for column in columns]
    payload["columns"] = columns

    base_rows = payload.get("rows")
    base_rows = [row for row in (base_rows or []) if isinstance(row, dict)]

    visible_raw = options.get("visibleRows")
    if visible_raw is None:
        # 调用方**没给**可见行 → 就用载荷自带的行（别把表格画成空的）。
        visible_rows = base_rows
    else:
        visible_rows = [
            row for row in (visible_raw or []) if isinstance(row, dict)
        ]

    # 定义声明了列 → 显示串按**合并后的列**重算（列格式优先于区域格式）。
    # 定义没声明列 → 保持载荷自带的 displayRows（第一批的表格照旧）。
    if _definition_columns(region):
        payload["displayRows"] = build_display_rows(region, columns, visible_rows)
    payload["visibleRows"] = visible_rows

    payload["rowNumber"] = bool(component.get("rowNumber", True))

    group_rows_by = [
        str(field) for field in (component.get("groupRowsBy") or []) if str(field)
    ]
    payload["groupRowsBy"] = group_rows_by

    page_size = component.get("pageSize")
    payload["pageSize"] = int(page_size) if isinstance(page_size, int) else None

    frozen = component.get("frozenColumnCount")
    payload["frozenColumnCount"] = int(frozen) if isinstance(frozen, int) else 0

    summaries, notes = resolve_table_summaries(
        region, columns, options.get("summaryValues")
    )
    payload["summaries"] = summaries

    # 说明：**追加**（不覆盖载荷自带的说明，比如 JSON 模式的行数说明）。
    existing_notes = payload.get("notes")
    if isinstance(existing_notes, list):
        merged_notes = [str(item) for item in existing_notes]
    elif existing_notes in (None, ""):
        merged_notes = []
    else:
        merged_notes = [str(existing_notes)]
    payload["notes"] = merged_notes + list(notes)

    return payload


__all__ = [
    "INHERITED_COLUMN_KEYS",
    "SUMMARY_BASIS_LABELS",
    "SUMMARY_MISSING_NOTE",
    "build_display_rows",
    "decorate_table_payload",
    "merge_table_columns",
    "resolve_table_summaries",
    "summary_value_key",
]
