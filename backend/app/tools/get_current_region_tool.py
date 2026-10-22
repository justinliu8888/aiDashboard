"""get_current_region：读当前草稿中某个区块的详情（只读事实）。

先从 get_current_draft 拿到 regionId，再用本工具看该区块的完整内容：
数据源、指标、时间范围、粒度、展示方式。
"""
from __future__ import annotations

from typing import Any

from app.tools.base import EDIT_STATES, FIX_STATES, ToolCategory
from app.tools.context import current_tool_context


NAME = "get_current_region"
DESCRIPTION = (
    "读取当前草稿中某个 region（区块）的完整内容，包含数据源、指标、"
    "时间范围、粒度和展示方式。用户要修改某个具体区块时，"
    "先用 get_current_draft 找到 regionId，再用本工具看细节。"
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "regionId": {
            "type": "string",
            "description": "目标区块 ID，先从 get_current_draft 拿到",
        },
    },
    "required": ["regionId"],
}
CATEGORY = ToolCategory.FACT
IS_READ_ONLY = True
APPLICABLE_STATES = EDIT_STATES + FIX_STATES


def run(regionId: str) -> dict[str, Any]:
    ctx = current_tool_context()
    if ctx is None or ctx.definition is None:
        return {"found": False, "reason": "当前没有草稿，无法读取区块详情。"}

    for region in ctx.definition.regions or []:
        if region.get("id") == regionId:
            return {"found": True, "region": region}
    return {
        "found": False,
        "reason": f"草稿中不存在 regionId={regionId}，请先调用 get_current_draft 确认。",
        "availableRegionIds": [
            r.get("id", "") for r in (ctx.definition.regions or [])
        ],
    }
