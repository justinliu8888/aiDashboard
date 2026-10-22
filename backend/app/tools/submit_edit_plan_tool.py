"""submit_edit_plan：提交模板编辑计划（终结工具，节点 5.5）。

终结工具：调用即代表本轮结论已定。
产出由确定性层消费：仍要走一遍 grounded 校验 → TemplateEditEngine.apply，
模型只产候选计划，不直接改草稿。
"""
from __future__ import annotations

from typing import Any

from app.schemas.agent import AgentState
from app.tools.base import EDIT_PLAN_STATES, ToolCategory
from app.tools.schemas import EditPlanPayload
from app.tools.submission import submit_payload


NAME = "submit_edit_plan"
DESCRIPTION = (
    "提交对当前草稿某个区块的编辑计划，这是本轮的最终结论，"
    "调用后不要再调用其他工具。"
    "target 指明改哪个区块（regionId），operation 指明怎么改，"
    "needsClarification 为 true 时说明还缺参数并给出 clarificationQuestion。"
    "用户表达含糊（比如'改好看点'）时，把候选解释放进 options 让用户选，"
    "不要自己替用户决定。"
)
PARAMETERS = EditPlanPayload.model_json_schema()
CATEGORY = ToolCategory.TERMINAL
IS_READ_ONLY = False
# 交编辑计划的节点：待确认和编辑中。
# 这两个节点的允许转移里都有 USER_EDITING_TEMPLATE，所以结论落得下去。
APPLICABLE_STATES = EDIT_PLAN_STATES
# 修改和局部追问都保持编辑上下文。
CONCLUSION_STATES = (AgentState.USER_EDITING_TEMPLATE.value,)


def run(
    isTemplateEdit: bool = True,
    intent: str = "UNKNOWN",
    target: dict[str, Any] | None = None,
    operation: dict[str, Any] | None = None,
    confidence: float = 0.0,
    needsClarification: bool = False,
    clarificationQuestion: str = "",
    operations: list[dict[str, Any]] | None = None,
    summary: str = "",
    options: list[dict[str, Any]] | None = None,
    reply: str = "",
) -> dict[str, Any]:
    return submit_payload(
        NAME,
        EditPlanPayload,
        {
            "isTemplateEdit": isTemplateEdit,
            "intent": intent,
            "target": target or {},
            "operation": operation or {},
            "confidence": confidence,
            "needsClarification": needsClarification,
            "clarificationQuestion": clarificationQuestion,
            "operations": operations or [],
            "summary": summary,
            "options": options or [],
            "reply": reply,
        },
    )
