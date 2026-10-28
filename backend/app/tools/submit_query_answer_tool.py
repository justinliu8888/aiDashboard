"""submit_query_answer：提交只读查询的答案（不推进业务）。

查询链路的终结工具。用户问「当前对话有没有草稿」「刚才那份做到哪一步了」
「你支持分钟趋势吗」这类问题时，模型先用事实工具查证，再调用本工具提交答案。

和 ``submit_edit_answer`` 的区别：

    - ``submit_edit_answer`` 只在编辑态可用，结论状态是 USER_EDITING_TEMPLATE；
    - 本工具在所有业务节点可用，结论是「原地保持」（SAME_STATE_CONCLUSION），
      状态一个字节都不动。

提交后本轮就结束了：不会生成草稿、不会保存、不会校验、不会预览、
不会接受或取消待确认修改计划、不会确认报表。
"""
from __future__ import annotations

from app.tools.base import SAME_STATE_CONCLUSION, ToolCategory
from app.tools.base import BUSINESS_AGENT_STATES
from app.tools.schemas import QueryAnswerPayload
from app.tools.submission import submit_payload


NAME = "submit_query_answer"
DESCRIPTION = (
    "提交对用户问题的只读回答：facts 填已核实的事实，limitations 填限制与"
    "无法确认的原因，suggestions 只给建议。回答必须区分报表草稿、待确认修改计划"
    "和报表预览，不能把未确认的内容说成已经出数，也不能编造实时进度、百分比或"
    "预计完成时间。提交后本轮结束，不会生成、修改、保存、校验或预览任何报表。"
    "问题指向的对象不明确时，把 needsClarification 设为 true 并在"
    "clarificationQuestion 里问一个具体问题，不要替用户选择操作。"
)
PARAMETERS = QueryAnswerPayload.model_json_schema()
CATEGORY = ToolCategory.TERMINAL
#: 终结工具要写入本轮的结论提交槽，所以不是只读；它改变的是「提交槽」，
#: 不是任何业务对象。
IS_READ_ONLY = False
APPLICABLE_STATES = BUSINESS_AGENT_STATES
CONCLUSION_STATES = (SAME_STATE_CONCLUSION,)
#: 只在只读查询模式注册：普通业务轮看不到「提交只读答案」这个出口。
IS_QUERY_ONLY = True


def run(
    reply: str,
    facts: list[str] | None = None,
    limitations: list[str] | None = None,
    suggestions: list[str] | None = None,
    needsClarification: bool = False,
    clarificationQuestion: str = "",
) -> dict:
    return submit_payload(
        NAME,
        QueryAnswerPayload,
        {
            "reply": reply,
            "facts": facts or [],
            "limitations": limitations or [],
            "suggestions": suggestions or [],
            "needsClarification": bool(needsClarification),
            "clarificationQuestion": clarificationQuestion or "",
        },
    )
