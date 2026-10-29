"""get_current_session_status：读当前对话的已记录状态（只读事实）。

草稿事实回答「当前对话有没有报表草稿」；本工具回答「当前对话现在处在
哪个已记录阶段、还有什么待确认的事」：

    - businessStage        已落库的业务阶段（会话 state）。它是**已记录**的阶段，
                           不是正在执行的实时任务状态。
    - pendingEditPlan      待确认修改计划的公开摘要。
    - pendingChoice        待确认选项（必须选择的菜单）。
    - savedDefinition      已保存的定义。
    - preview              已生成的报表预览。
    - clarificationRounds  同一项待定修改已追问的轮数。

同样只读服务端会话记录；读不到就说读不到。
"""
from __future__ import annotations

from typing import Any

from app.services.session_context import current_session_key
from app.services.session_fact_service import session_fact_service
from app.tools.base import LOOKUP_STATES, ToolCategory
from app.tools.context import current_tool_context


NAME = "get_current_session_status"
DESCRIPTION = (
    "读取当前对话服务端已记录的业务阶段和待确认事项："
    "businessStage（已记录的阶段，不是实时任务状态）、"
    "pendingEditPlan（待确认修改计划）、pendingChoice（必须选择的菜单）、"
    "savedDefinition（已保存的定义）、preview（已生成的报表预览）、"
    "clarificationRounds（同一项修改已追问的轮数）。"
    "返回的 status 是事实状态：ABSENT 表示当前对话确实没有这些内容，"
    "UNAVAILABLE 表示暂时无法确认。不要把读不到说成没有，"
    "也不要据此编造实时进度、百分比或预计完成时间。"
)
PARAMETERS = {
    "type": "object",
    "properties": {},
}
CATEGORY = ToolCategory.FACT
IS_READ_ONLY = True
APPLICABLE_STATES = LOOKUP_STATES


def run() -> dict[str, Any]:
    ctx = current_tool_context()
    facts = session_fact_service.load(
        session_id=current_session_key(),
        project_id=ctx.project_id if ctx else "",
        subject_ref=ctx.subject_ref if ctx else "",
        subject_id=ctx.subject_id if ctx else "",
        request_has_draft=bool(ctx and ctx.definition is not None),
    )
    draft = facts.draft
    return {
        "status": facts.status.value,
        "scope": facts.scope,
        "sessionExists": facts.sessionExists,
        "businessStage": facts.businessStage,
        "hasDraft": bool(draft.exists) if draft is not None else False,
        "reportName": draft.reportName if draft is not None else "",
        "draftRevision": draft.revision if draft is not None else 0,
        "pendingSteps": list(draft.pendingSteps) if draft is not None else [],
        "pendingEditPlan": facts.pendingEditPlan,
        "pendingChoice": facts.pendingChoice,
        "savedDefinition": facts.savedDefinition,
        "preview": facts.preview,
        "clarificationRounds": facts.clarificationRounds,
        "source": draft.source if draft is not None else "server_session",
        "reason": facts.reason,
    }
