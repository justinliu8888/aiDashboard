"""get_validation_result：读上一次校验的问题（只读事实）。

节点 8（VALIDATION_FAILED）需要"读问题 → 定修复方案 → 重校验"的闭环，
第一步就是让模型看到真实的校验问题，而不是后端翻译好的一句话。
"""
from __future__ import annotations

from typing import Any

from app.tools.base import FIX_STATES, ToolCategory
from app.tools.context import current_tool_context


NAME = "get_validation_result"
DESCRIPTION = (
    "读取当前草稿的校验结果，返回每条问题的 code、所在路径、原因和修改建议。"
    "校验没过时要修复草稿，先调用它拿到真实问题，再据此提出修改方案，"
    "不要凭空猜测哪里有问题。"
)
PARAMETERS = {
    "type": "object",
    "properties": {},
}
CATEGORY = ToolCategory.FACT
IS_READ_ONLY = True
APPLICABLE_STATES = FIX_STATES


def run() -> dict[str, Any]:
    ctx = current_tool_context()
    if ctx is None:
        return {"available": False, "reason": "当前没有可读的校验上下文。"}

    result = ctx.last_validation_result
    if not result:
        return {
            "available": False,
            "reason": "当前没有校验结果，请先保存草稿并执行校验。",
        }

    issues = result.get("issues") or []
    return {
        "available": True,
        "passed": bool(result.get("passed", False)),
        "issues": issues,
        "total": len(issues),
        "truncated": False,
    }
