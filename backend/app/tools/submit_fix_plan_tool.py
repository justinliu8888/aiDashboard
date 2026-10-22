"""submit_fix_plan：提交校验问题的修复方案（终结工具，节点 8）。

终结工具：调用即代表本轮结论已定。
产出由确定性层消费：应用修复后重新校验，形成「读问题 → 定修复 → 重校验」闭环。
"""
from __future__ import annotations

from typing import Any

from app.schemas.agent import AgentState
from app.tools.base import FIX_PLAN_STATES, ToolCategory
from app.tools.schemas import FixPlanPayload
from app.tools.submission import submit_payload


NAME = "submit_fix_plan"
DESCRIPTION = (
    "提交校验问题的修复方案，这是本轮的最终结论，调用后不要再调用其他工具。"
    "先用 get_validation_result 拿到真实问题，再针对每条问题给出修复动作，"
    "不要凭空猜测哪里有问题。"
)
PARAMETERS = FixPlanPayload.model_json_schema()
CATEGORY = ToolCategory.TERMINAL
IS_READ_ONLY = False
# 只有校验失败节点能交修复方案。
# VALIDATION_FAILED 的唯一合法去向就是 ANALYZING（重新生成），
# 交出去就能落回重新生成的循环里。
APPLICABLE_STATES = FIX_PLAN_STATES
CONCLUSION_STATES = (AgentState.ANALYZING.value,)


def run(
    fixes: list[dict[str, Any]] | None = None,
    reply: str = "",
) -> dict[str, Any]:
    return submit_payload(
        NAME,
        FixPlanPayload,
        {"fixes": fixes or [], "reply": reply},
    )
