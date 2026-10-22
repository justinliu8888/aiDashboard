"""submit_change_plan：提交对当前草稿的修改方案（终结工具，节点 4）。

终结工具：调用即代表本轮结论已定。
产出由确定性层消费：组装成 ChangeSet，附在响应里给前端做三栏 diff。
"""
from __future__ import annotations

from typing import Any

from app.schemas.agent import AgentState
from app.tools.base import CHANGE_PLAN_STATES, ToolCategory
from app.tools.schemas import ChangePlanPayload
from app.tools.submission import submit_payload


NAME = "submit_change_plan"
DESCRIPTION = (
    "提交对当前草稿的修改方案，这是本轮的最终结论，调用后不要再调用其他工具。"
    "每处修改给出 regionId（改哪个区块）、path（改哪个属性）、"
    "after（改成什么）和 summary（为什么这么改）。"
)
PARAMETERS = ChangePlanPayload.model_json_schema()
CATEGORY = ToolCategory.TERMINAL
IS_READ_ONLY = False
# 交修改方案的节点：还在收集需求时、或者草稿已就绪/待确认时。
# 校验失败节点不行 —— 那里要交的是修复方案，不是修改方案。
APPLICABLE_STATES = CHANGE_PLAN_STATES
CONCLUSION_STATES = (AgentState.DEFINITION_DRAFT_READY.value,)


def run(
    baseDefinitionRevision: int = 0,
    changes: list[dict[str, Any]] | None = None,
    reply: str = "",
) -> dict[str, Any]:
    return submit_payload(
        NAME,
        ChangePlanPayload,
        {
            "baseDefinitionRevision": baseDefinitionRevision,
            "changes": changes or [],
            "reply": reply,
        },
    )
