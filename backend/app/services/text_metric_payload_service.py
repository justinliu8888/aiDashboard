"""文本区 / 指标卡 / 指标组的**载荷计算**（T01–T03、M01–M04、MG01–MG03 的载荷侧）。

**这一层是纯函数**：不取生产数据、不存库、不调模型、不碰数据库、不写全局状态。

两个入口：

* :func:`build_text_payload` —— 文本区载荷。把模板里的 ``{{占位符}}`` 按
  **受控绑定**换成真值；**取不到值 / 取值不唯一时绝不猜** —— 占位符原样留着，
  并写一条人话说明（「多行歧义先澄清，不擅自求和」）。
* :func:`decorate_metric_payload` —— 指标载荷。给基础载荷补上**指标项自己**
  的 id / 标签 / 字段 / 输出 / 格式 / 比较值 / 图标，并用**指标自己的格式**
  重算显示串 —— 显示格式只改显示，不改数值、不改统计结果。

显示串一律走**同一个格式化器**
（:func:`app.services.report_presentation_service.format_display_value`）——
不另写一套数字格式化，两边迟早会对不上。

**字段事实**（单位）也只认一份：:func:`declared_field_unit` 先读模拟字段目录，
再读区域已登记输出的 ``expectedFields``；认不出来就是「没有单位」，
**不猜单位**。
"""
from __future__ import annotations

import re
from typing import Any

from app.services.display_binding_service import (
    DisplayBindingError,
    resolve_icon_ref,
    simulated_field,
)
from app.services.report_presentation_service import format_display_value


#: 遗留的**固定替换**占位符：不靠 bindings，也照样能取值
#: （老卡片里的 ``{{roomName}}`` / ``{{logicalDate}}`` 就是这一类）。
LEGACY_FIXED_PLACEHOLDERS: tuple[str, ...] = ("roomName", "logicalDate")

#: 模板里的占位符：``{{名字}}``（名字是合法标识符）。
PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

#: 文本区的受控文本格式。
TEXT_FORMATS: tuple[str, ...] = ("PLAIN", "MARKDOWN")

#: 指标项稳定 id 的前缀。
METRIC_ID_PREFIX = "m_"


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _component(region: Any) -> dict[str, Any]:
    """区域上的组件（不是字典就当没有）。"""
    component = region.get("component") if isinstance(region, dict) else None
    return component if isinstance(component, dict) else {}


def _content(region: Any) -> dict[str, Any]:
    """组件上的文本内容（``content`` 必须是**对象**，不是裸字符串）。"""
    content = _component(region).get("content")
    return content if isinstance(content, dict) else {}


def _bindings(region: Any) -> dict[str, dict[str, Any]]:
    """组件上声明的占位符绑定。"""
    raw = _content(region).get("bindings")
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): (expr if isinstance(expr, dict) else {})
        for name, expr in raw.items()
    }


def _pipeline_outputs(region: Any) -> dict[str, Any]:
    """区域登记的计算输出。"""
    data = region.get("data") if isinstance(region, dict) else None
    pipeline = (data or {}).get("pipeline") if isinstance(data, dict) else None
    outputs = (pipeline or {}).get("outputs") if isinstance(pipeline, dict) else None
    return outputs if isinstance(outputs, dict) else {}


def declared_field_unit(region: Any, field_ref: Any) -> str:
    """字段的**实际单位**（认不出来就是空字符串 —— 不猜）。

    出处两份，**顺序固定**：

    1. 模拟字段目录（当前开发阶段可用的受控单值字段）；
    2. 区域已登记输出自己声明的 ``expectedFields``。
    """
    name = str(field_ref or "").strip()
    if not name:
        return ""
    info = simulated_field(name)
    if info is not None:
        return str(info.get("unit") or "")
    for spec in _pipeline_outputs(region).values():
        fields = spec.get("expectedFields") if isinstance(spec, dict) else None
        if not isinstance(fields, dict):
            continue
        field = fields.get(name)
        if isinstance(field, dict):
            return str(field.get("unit") or "")
    return ""


def _candidate_values(raw: Any) -> list[Any]:
    """一个字段取值 → **候选值列表**。

    正常的模拟单值就是一个标量；列表 / ``{"candidates": [...]}`` /
    ``{"ambiguous": true}`` 是「取不唯一」的形状 —— 它们**不被求和、不被取第一行**。
    """
    if isinstance(raw, dict):
        if "candidates" in raw:
            candidates = raw.get("candidates")
            return list(candidates) if isinstance(candidates, list) else []
        if raw.get("ambiguous") is True:
            return []
        if "value" in raw:
            return [raw.get("value")]
        return [raw]
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return [raw]


def _resolve_field_value(
    field_ref: str, field_values: dict[str, Any]
) -> tuple[Any, bool, str]:
    """字段取值 → ``(值, 是否确定, 说明)``。

    **多行歧义先澄清，不擅自求和**：取不到、或有多个**不同**的候选值时，
    返回「不确定」—— 由调用方把占位符原样留着，不编一个数。
    """
    name = str(field_ref or "").strip()
    if not name:
        return None, False, "没有给字段名"
    if name not in field_values:
        return None, False, f"字段 {name} 当前没有取值"
    raw = field_values[name]
    if isinstance(raw, dict) and raw.get("ambiguous") is True:
        return None, False, f"字段 {name} 有多个候选值，取值不唯一（多行歧义），先澄清再显示"
    candidates = _candidate_values(raw)
    if not candidates:
        return None, False, f"字段 {name} 当前没有取值"
    distinct: list[Any] = []
    for candidate in candidates:
        if not any(repr(candidate) == repr(seen) for seen in distinct):
            distinct.append(candidate)
    if len(distinct) > 1:
        return (
            None,
            False,
            f"字段 {name} 有多个不同的候选值，取值不唯一（多行歧义），先澄清再显示",
        )
    value = distinct[0]
    if value is None or value == "":
        return value, False, f"字段 {name} 当前没有取值"
    return value, True, ""


def _dedupe(items: list[str]) -> list[str]:
    """去重（保持顺序）。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


# --------------------------------------------------------------------------
# 文本区载荷
# --------------------------------------------------------------------------

def build_text_payload(region: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """文本区载荷：把模板里的占位符按**受控绑定**换成真值。

    ``ctx`` 由调用方给齐（**这里不取数**）::

        {"roomName": str, "logicalDate": str, "fieldValues": {字段名: 值}}

    * ``{{roomName}}`` / ``{{logicalDate}}`` —— 遗留的固定替换，照旧生效；
    * ``{{名字}}`` 且 ``名字`` 在 ``content.bindings`` 里 —— 按绑定的
      ``fieldRef`` 去 ``fieldValues`` 取值；
    * **取不到 / 取不唯一** —— 占位符**原样留着**，绑定标记为未解析，
      并写一条人话说明。**不猜、不求和、不取第一行**。
    """
    component = _component(region)
    content = _content(region)
    template = str(content.get("template") or "")
    text_format = str(content.get("format") or "PLAIN").strip().upper()
    if text_format not in TEXT_FORMATS:
        text_format = "PLAIN"
    heading_level = component.get("headingLevel")
    if isinstance(heading_level, bool) or not isinstance(heading_level, int):
        heading_level = None

    context = ctx if isinstance(ctx, dict) else {}
    raw_values = context.get("fieldValues")
    field_values = raw_values if isinstance(raw_values, dict) else {}
    room_name = str(context.get("roomName") or "")
    logical_date = str(context.get("logicalDate") or "")

    bindings = _bindings(region)
    notes: list[str] = []
    binding_rows: list[dict[str, Any]] = []
    #: 占位符名 → (值, 是否确定, 说明)
    states: dict[str, tuple[Any, bool, str]] = {}

    for name, expr in bindings.items():
        expr_type = str(expr.get("type") or "").strip().upper()
        if expr_type == "FIELD":
            field_ref = str(expr.get("fieldRef") or "").strip()
            value, resolved, note = _resolve_field_value(field_ref, field_values)
        else:
            # LITERAL 之类的表达式：值就在表达式里，直接取。
            field_ref = ""
            value = expr.get("value")
            resolved = value is not None
            note = "" if resolved else f"绑定 {name} 没有可用的字面值"
        states[name] = (value, resolved, note)
        binding_rows.append(
            {
                "name": name,
                "fieldRef": field_ref,
                "value": value if resolved else None,
                "resolved": resolved,
                "note": note,
            }
        )

    unresolved: dict[str, str] = {}

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in states:
            value, resolved, note = states[name]
            if resolved:
                return "" if value is None else str(value)
            unresolved[name] = note
            return match.group(0)
        if name == "roomName":
            if room_name:
                return room_name
            unresolved[name] = "当前没有机房名称"
            return match.group(0)
        if name == "logicalDate":
            if logical_date:
                return logical_date
            unresolved[name] = "当前没有逻辑日期"
            return match.group(0)
        # 没有绑定关系 —— 原样留着（**不编一个值**）。
        return match.group(0)

    rendered = PLACEHOLDER_PATTERN.sub(_replace, template)

    placeholder_names: list[str] = []
    for match in PLACEHOLDER_PATTERN.finditer(template):
        name = match.group(1)
        if name in states or name in LEGACY_FIXED_PLACEHOLDERS:
            continue
        placeholder_names.append(name)
    unbound = _dedupe(placeholder_names)
    for name in unbound:
        notes.append(f"占位符 {{{{{name}}}}} 没有绑定关系，保持原样。")
    for name, note in unresolved.items():
        notes.append(note)

    return {
        "rendered": rendered,
        "template": template,
        "format": text_format,
        "headingLevel": heading_level,
        "bindings": binding_rows,
        "unboundPlaceholders": unbound,
        "notes": _dedupe(notes),
        "dataSource": "simulated",
    }


# --------------------------------------------------------------------------
# 指标载荷
# --------------------------------------------------------------------------

def _metric_definitions(region: Any, component_type: str) -> list[dict[str, Any]]:
    """定义里声明的**指标项**（按声明顺序）。

    * ``METRIC_CARD`` → ``component.metric``（一项）；
    * ``METRIC_GROUP`` → ``component.metrics``（一项一项的清单）。
    """
    component = _component(region)
    if component_type == "METRIC_GROUP":
        raw = component.get("metrics")
        return [item for item in (raw or []) if isinstance(item, dict)]
    if component_type == "METRIC_CARD":
        metric = component.get("metric")
        return [dict(metric)] if isinstance(metric, dict) else []
    return []


def _match_items(
    definitions: list[dict[str, Any]], base_items: list[dict[str, Any]]
) -> dict[int, int]:
    """定义项下标 → 基础载荷项下标。

    匹配顺序（**先紧后松**，命中就不再往下找）：

    1. **id 相同** —— 指标项的稳定 id 就是它的身份；
    2. **字段相同**（``fieldRef`` 或基础载荷的 ``metricKey``）——
       同一字段就是同一项；
    3. **标签相同**；
    4. **位置相同**（前面都没命中时的兜底）。
    """
    used: set[int] = set()
    mapping: dict[int, int] = {}

    def _find(predicate: Any) -> int:
        for position, item in enumerate(base_items):
            if position in used:
                continue
            if predicate(item):
                return position
        return -1

    for index, definition in enumerate(definitions):
        metric_id = str(definition.get("id") or "")
        field_ref = str(definition.get("fieldRef") or "")
        label = str(definition.get("label") or "")
        found = -1
        if metric_id:
            found = _find(lambda item: str(item.get("id") or "") == metric_id)
        if found < 0 and field_ref:
            found = _find(
                lambda item: field_ref
                in (
                    str(item.get("fieldRef") or ""),
                    str(item.get("metricKey") or ""),
                )
            )
        if found < 0 and label:
            found = _find(lambda item: str(item.get("label") or "") == label)
        if found < 0 and index < len(base_items) and index not in used:
            # 位置兜底**只对认得出自己是谁的定义项**生效 ——
            # 认不出的定义项（老式指标卡）不许凭位置去认领别人的指标。
            #
            # 还要**对方也没有自己的身份**：基础载荷那一项已经指名道姓地
            # 认了另一个指标（metricKey / 标签对不上），就不能按位置把它
            # 认领过来 —— 卡片被追加过指标之后，位置说的不是同一件事
            # （反馈 F02-04：新增的「总告警数」被主指标顶成 kWh）。
            candidate = base_items[index]
            candidate_key = str(candidate.get("metricKey") or "")
            candidate_label = str(candidate.get("label") or "")
            takes_other = bool(
                (candidate_key and field_ref and candidate_key != field_ref)
                or (candidate_label and label and candidate_label != label)
            )
            found = (
                index
                if _identifies_item(definition) and not takes_other
                else -1
            )
        if found >= 0:
            used.add(found)
        mapping[index] = found
    return mapping


def _identifies_item(definition: dict[str, Any]) -> bool:
    """这个定义项**指名道姓**地认出某一个指标项吗？

    认得出的凭据只有两个：**稳定 id** 与 **fieldRef**。

    没有它们（老式指标卡的 ``component.metric`` 里 ``fieldRef`` 是空的）
    就**认不出任何一项** —— 那张卡的指标清单由 ``previewSpec.metrics`` 说了算，
    这里一个字都不许改。
    """
    return bool(
        str(definition.get("id") or "").strip()
        or str(definition.get("fieldRef") or "").strip()
    )


def _region_authoritative_format(region: Any) -> dict[str, Any]:
    """区域的**权威格式**（``SET_FORMAT`` 写的那一份，契约 0002 §3.5）。"""
    fmt = region.get("format") if isinstance(region, dict) else None
    return fmt if isinstance(fmt, dict) else {}


def _enrich_item(
    definition: dict[str, Any],
    base: dict[str, Any],
    region: dict[str, Any],
    comparison_values: dict[str, Any],
    fallback_unit: str,
    region_format: dict[str, Any],
) -> dict[str, Any]:
    """一个基础载荷项 + 它的定义 → 补齐后的项。

    **显示格式的优先级**（这一层只认这一条）：

    1. **区域的权威格式**（``region.format``，契约 0002 §3.5）—— 有它就用它，
       指标自己的格式**不覆盖**它；
    2. 区域没有权威格式 → **指标自己的格式**生效（M02：指标级格式必须真的
       驱动显示串）。

    底层值（``value``）一个字节都不动 —— 显示格式只改显示。
    """
    item = dict(base)
    raw_format = definition.get("format")
    fmt = dict(raw_format) if isinstance(raw_format, dict) else {}
    field_ref = str(definition.get("fieldRef") or item.get("fieldRef") or "")
    label = str(definition.get("label") or item.get("label") or "")
    metric_id = str(definition.get("id") or item.get("id") or "")
    if metric_id:
        item["id"] = metric_id
    if label:
        item["label"] = label
    if field_ref:
        item["fieldRef"] = field_ref
    output_ref = definition.get("outputRef")
    if output_ref:
        item["outputRef"] = output_ref
    if fmt:
        item["format"] = fmt

    unit = (
        declared_field_unit(region, field_ref)
        or str(item.get("unit") or "")
        or str(fallback_unit or "")
    )
    if not region_format:
        # 区域没有权威格式 → **指标自己的格式**驱动显示串（M02）。
        item["displayValue"] = format_display_value(
            item.get("value"), fmt, unit=unit
        )
    if fmt.get("precision") is not None:
        item["precision"] = fmt["precision"]

    comparison_ref = definition.get("comparisonFieldRef")
    if comparison_ref:
        comparison_ref = str(comparison_ref)
        item["comparisonFieldRef"] = comparison_ref
        comparison_unit = declared_field_unit(region, comparison_ref)
        comparison_value, resolved, _note = _resolve_field_value(
            comparison_ref, comparison_values
        )
        item["comparisonValue"] = comparison_value if resolved else None
        item["comparisonDisplayValue"] = (
            format_display_value(comparison_value, fmt, unit=comparison_unit)
            if resolved
            else ""
        )
        item["comparisonUnit"] = comparison_unit

    icon_ref = definition.get("iconRef")
    if icon_ref:
        try:
            icon_info = resolve_icon_ref(str(icon_ref))
        except DisplayBindingError:
            # 定义里的图标引用没注册：**不渲染图标**，但不因此让整个载荷失败
            # （载荷是给人看的，图标只是装饰）。
            icon_info = None
        if icon_info is not None:
            item["iconRef"] = icon_info["ref"]
            item["icon"] = icon_info["icon"]
    return item


def decorate_metric_payload(
    region: dict[str, Any], payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """给基础指标载荷补上**定义里声明**的东西（**就地改，返回同一个对象**）。

    基础载荷由上游给（``label / value / unit / precision / yoyPercent /
    comparisonAvailable / windowLabel / dataSource / metrics[]``）；这里补：

    * 每项的 ``id`` / ``label`` / ``fieldRef`` / ``outputRef`` / ``format``；
    * ``displayValue`` —— 按**区域的权威格式优先、指标自己的格式兜底**重算；
    * ``comparisonFieldRef`` / ``comparisonValue`` / ``comparisonDisplayValue`` /
      ``comparisonUnit``；
    * ``iconRef`` / ``icon``（注册图标）；
    * ``precision``。

    **只改「定义指名道姓认出的那一项」**：老式指标卡的 ``component.metric``
    没有 ``fieldRef`` / ``id``，它认不出清单里的任何一项 —— 那张卡的指标清单
    由 ``previewSpec.metrics`` 说了算，这里一个字都不动。

    ``METRIC_GROUP`` 还会把 ``metrics`` **重排成定义里声明的顺序**，
    定义里没有的旧项**追加到最后**（不静默丢）。
    """
    if not isinstance(payload, dict):
        return {}
    context = ctx if isinstance(ctx, dict) else {}
    raw_comparison = context.get("comparisonValues")
    comparison_values = raw_comparison if isinstance(raw_comparison, dict) else {}
    component = _component(region)
    component_type = str(component.get("type") or "").strip().upper()
    definitions = _metric_definitions(region, component_type)
    base_items = [
        item for item in (payload.get("metrics") or []) if isinstance(item, dict)
    ]
    fallback_unit = str(payload.get("unit") or "")
    region_format = _region_authoritative_format(region)

    if component_type == "METRIC_GROUP":
        mapping = _match_items(definitions, base_items)
        ordered: list[dict[str, Any]] = []
        for index, definition in enumerate(definitions):
            position = mapping.get(index, -1)
            base = base_items[position] if position >= 0 else {}
            ordered.append(
                _enrich_item(
                    definition,
                    base,
                    region,
                    comparison_values,
                    fallback_unit,
                    region_format,
                )
            )
        matched = {position for position in mapping.values() if position >= 0}
        for position, base in enumerate(base_items):
            if position not in matched:
                # 定义里没有的旧项：追加到最后，**不静默丢**。
                ordered.append(dict(base))
        payload["metrics"] = ordered
    else:
        # 单指标卡：定义只认得出**一项**；认不出就一项都不碰。
        mapping = _match_items(definitions, base_items)
        enriched: list[dict[str, Any]] = []
        for position, base in enumerate(base_items):
            definition = next(
                (
                    definitions[index]
                    for index, target in mapping.items()
                    if target == position
                ),
                None,
            )
            if definition is None or not _identifies_item(definition):
                enriched.append(dict(base))
                continue
            enriched.append(
                _enrich_item(
                    definition,
                    base,
                    region,
                    comparison_values,
                    fallback_unit,
                    region_format,
                )
            )
        payload["metrics"] = enriched
        if definitions and _identifies_item(definitions[0]):
            definition = definitions[0]
            raw_format = definition.get("format")
            fmt = dict(raw_format) if isinstance(raw_format, dict) else {}
            if fmt and not region_format:
                # 区域没有权威格式 → 指标自己的格式驱动**卡片主值**的显示串。
                unit = (
                    declared_field_unit(region, definition.get("fieldRef"))
                    or str(payload.get("unit") or "")
                )
                payload["displayValue"] = format_display_value(
                    payload.get("value"), fmt, unit=unit
                )
            if fmt.get("precision") is not None:
                payload["precision"] = fmt["precision"]

    if component_type == "METRIC_GROUP":
        arrangement = component.get("arrangement")
        payload["arrangement"] = (
            dict(arrangement) if isinstance(arrangement, dict) else {}
        )
        items = [item for item in (payload.get("metrics") or []) if isinstance(item, dict)]
        first = items[0] if items else {}
        # 组没有单一的值 —— 顶层的值取**第一项**。
        # 顶层的标签由基础载荷按区块标题给（这里不改它）。
        payload["value"] = first.get("value")
        if first.get("unit") is not None:
            payload["unit"] = first.get("unit")
        if first.get("precision") is not None:
            payload["precision"] = first.get("precision")
        # 基础载荷的 comparisonAvailable 是「组里有没有对比值」；
        # 这里只**补**不**减**（第一项有对比值时也报得出来）。
        payload["comparisonAvailable"] = bool(payload.get("comparisonAvailable")) or (
            first.get("comparisonValue") is not None
        )
        payload["metricGroup"] = {
            "metricCount": len(items),
            "metricIds": [str(item.get("id") or "") for item in items],
            "arrangementMode": str(payload["arrangement"].get("mode") or "GRID").upper(),
        }
    return payload


__all__ = [
    "LEGACY_FIXED_PLACEHOLDERS",
    "PLACEHOLDER_PATTERN",
    "TEXT_FORMATS",
    "build_text_payload",
    "decorate_metric_payload",
    "declared_field_unit",
]
