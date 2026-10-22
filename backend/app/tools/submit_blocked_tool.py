"""submit_blocked：提交无法处理的结论（终结工具，节点 3）。

终结工具：调用即代表本轮结论已定。
产出由确定性层消费：组装成 AgentBlock（拦截原因 + 替代方案）。
"""
from __future__ import annotations

from typing import Any

from app.schemas.agent import AgentState
from app.tools.base import BLOCKED_STATES, ToolCategory
from app.tools.schemas import BlockedPayload
from app.tools.submission import submit_payload


NAME = "submit_blocked"
DESCRIPTION = (
    "提交无法处理的结论（能力不支持 / 权限越界 / 诉求超出范围），"
    "这是本轮的最终结论，调用后不要再调用其他工具。"
    "message 说清原因，alternatives 给出用户可选的替代方案。"
)
PARAMETERS = BlockedPayload.model_json_schema()
CATEGORY = ToolCategory.TERMINAL
IS_READ_ONLY = False
# 只有"还能重新起个头"的节点能交拦截结论。
# 拦截是死路：BLOCKED_OR_UNSUPPORTED 的允许转移集合为空，
# 交了就出不来，所以不能放在已经走到一半的节点上。
APPLICABLE_STATES = BLOCKED_STATES
CONCLUSION_STATES = (AgentState.BLOCKED_OR_UNSUPPORTED.value,)


def run(
    layer: str = "CAPABILITY",
    code: str = "",
    message: str = "",
    alternatives: list[str] | None = None,
    reply: str = "",
) -> dict[str, Any]:
    return submit_payload(
        NAME,
        BlockedPayload,
        {
            "layer": layer,
            "code": code,
            "message": message,
            "alternatives": alternatives or [],
            "reply": reply,
        },
    )
