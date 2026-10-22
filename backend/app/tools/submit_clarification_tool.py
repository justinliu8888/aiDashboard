"""submit_clarification：提交需要向用户追问的信息（终结工具，节点 2）。

终结工具：调用即代表本轮结论已定。
产出由确定性层消费：组装成 AgentQuestion，受必选下拉锁约束。
"""
from __future__ import annotations

from typing import Any

from app.schemas.agent import AgentState
from app.tools.base import CLARIFICATION_STATES, ToolCategory
from app.tools.schemas import ClarificationPayload
from app.tools.submission import submit_payload


NAME = "submit_clarification"
DESCRIPTION = (
    "提交需要向用户追问的信息，这是本轮的最终结论，调用后不要再调用其他工具。"
    "missingInfo 列出缺哪些信息（expects 指明是文本、单选、多选还是日期范围），"
    "nextQuestion 是要问用户的具体问题，能自动回填的放进 autoFillable。"
)
PARAMETERS = ClarificationPayload.model_json_schema()
CATEGORY = ToolCategory.TERMINAL
IS_READ_ONLY = False
# 追问在几乎所有业务节点都可能发生：用户在任何一步都可能说"其实我想看的是……"。
APPLICABLE_STATES = CLARIFICATION_STATES
# 两种结论：
#   带 missingInfo → 真的要用户补信息 → NEEDS_CLARIFICATION
#   不带 missingInfo → 闲聊 / 引导 → 停在 ANALYZING
CONCLUSION_STATES = (
    AgentState.NEEDS_CLARIFICATION.value,
    AgentState.ANALYZING.value,
)


def run(
    missingInfo: list[dict[str, Any]] | None = None,
    nextQuestion: str = "",
    autoFillable: dict[str, Any] | None = None,
    reply: str = "",
) -> dict[str, Any]:
    return submit_payload(
        NAME,
        ClarificationPayload,
        {
            "missingInfo": missingInfo or [],
            "nextQuestion": nextQuestion,
            "autoFillable": autoFillable,
            "reply": reply,
        },
    )
