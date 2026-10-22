"""get_current_draft：读当前草稿概览（只读事实）。

草稿可能很大，塞进模型参数既浪费 token 又容易被模型改坏，
所以本工具从会话上下文取草稿，只返回**概览**（有哪些区块、分别是什么类型）。

用户说"把趋势图改成 7 天""删掉某个卡片"时，先调用它确认目标区块存在，
要细看某一块再用 get_current_region。
"""
from __future__ import annotations

from typing import Any

from app.tools.base import LOOKUP_STATES, ToolCategory
from app.tools.context import current_tool_context


NAME = "get_current_draft"
DESCRIPTION = (
    "读取当前草稿的概览：报表名、版本、包含哪些 region（区块）及各自类型。"
    "用户在说'把趋势图改成 7 天''删掉某个卡片'时，先调用它确认目标区块是否存在，"
    "再决定改哪个 region。"
)
PARAMETERS = {
    "type": "object",
    "properties": {},
}
CATEGORY = ToolCategory.FACT
IS_READ_ONLY = True
APPLICABLE_STATES = LOOKUP_STATES


def _region_summary(region: dict[str, Any]) -> dict[str, Any]:
    """region 概览：只保留模型判断需要的信息。"""
    component = region.get("component") or {}
    metric = component.get("metric") or {}
    return {
        "regionId": region.get("id", ""),
        "name": region.get("name", ""),
        "role": region.get("role", ""),
        "componentType": component.get("type", ""),
        "chartKind": component.get("chartKind"),
        "metricRef": metric.get("id"),
        "metricLabel": metric.get("label"),
    }


def run() -> dict[str, Any]:
    ctx = current_tool_context()
    if ctx is None or ctx.definition is None:
        return {"exists": False, "reason": "当前没有草稿，请先生成或加载草稿。"}

    definition = ctx.definition
    regions = definition.regions or []
    return {
        "exists": True,
        "reportName": (definition.metadata or {}).get("name", ""),
        "revision": (definition.metadata or {}).get("revision", 0),
        "baseDefinitionRevision": ctx.base_definition_revision,
        "selectedRegionId": ctx.selected_region_id,
        "regions": [_region_summary(r) for r in regions],
        "total": len(regions),
        "truncated": False,
    }
