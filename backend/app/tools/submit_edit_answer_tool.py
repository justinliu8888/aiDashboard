"""提交编辑态查询的只回答结论，不创建修改计划。"""
from __future__ import annotations

from app.schemas.agent import AgentState
from app.tools.base import EDIT_PLAN_STATES, ToolCategory
from app.tools.schemas import EditAnswerPayload
from app.tools.submission import submit_payload


NAME = "submit_edit_answer"
DESCRIPTION = (
    "查证当前草稿、授权数据源和能力后，只回答用户的问题。"
    "facts 填已核实事实，limitations 填限制，suggestions 只能提出建议；"
    "不得提交目标修改、操作或声称草稿已更改。调用后不要再调用工具。"
)
PARAMETERS = EditAnswerPayload.model_json_schema()
CATEGORY = ToolCategory.TERMINAL
IS_READ_ONLY = False
APPLICABLE_STATES = EDIT_PLAN_STATES
CONCLUSION_STATES = (AgentState.USER_EDITING_TEMPLATE.value,)


def run(
    reply: str,
    facts: list[str] | None = None,
    limitations: list[str] | None = None,
    suggestions: list[str] | None = None,
) -> dict:
    return submit_payload(
        NAME,
        EditAnswerPayload,
        {
            "reply": reply,
            "facts": facts or [],
            "limitations": limitations or [],
            "suggestions": suggestions or [],
        },
    )
