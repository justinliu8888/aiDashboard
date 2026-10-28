"""get_recorded_progress：读当前对话**已记录**的业务进度（只读事实）。

用户问「刚才那份做到哪了」「现在什么状态」「进度如何」时调用。

事实来源是服务端会话记录，不是本轮请求带回来的客户端快照：
快照可能缺失或过时，不能拿它当「当前对话有没有草稿」的依据。

结果只反映**已记录**的阶段 —— 系统没有实时任务状态来源，
所以既不报百分比，也不估剩余时间。

查进度不需要（也不允许）保存、校验、预览或推进任何业务：
本工具只读，不写库。
"""
from __future__ import annotations

from typing import Any

from app.services.recorded_progress_service import describe_recorded_progress
from app.services.session_context import current_session_key
from app.tools.base import LOOKUP_STATES, ToolCategory
from app.tools.context import current_tool_context


NAME = "get_recorded_progress"
DESCRIPTION = (
    "读取当前对话已记录的业务进度：已记录阶段、报表草稿（名称与版本）、"
    "待确认修改计划、已保存定义、报表预览，以及可确定的下一步。"
    "用户问「刚才那份做到哪了」「现在什么状态」「进度如何」时调用。"
    "结果只反映服务端已记录的阶段，不是实时任务监控：没有百分比，也没有预计完成时间。"
    "查进度不需要也不能调用保存、校验或预览。"
)
PARAMETERS = {
    "type": "object",
    "properties": {},
}
CATEGORY = ToolCategory.FACT
IS_READ_ONLY = True
APPLICABLE_STATES = LOOKUP_STATES


def run() -> dict[str, Any]:
    """查当前对话的已记录进度。

    会话 id 从请求级上下文取（``session_context``），业务对象范围从工具上下文取。
    """
    ctx = current_tool_context()
    return describe_recorded_progress(
        current_session_key(),
        subject_ref=ctx.subject_ref if ctx is not None else None,
        subject_id=ctx.subject_id if ctx is not None else None,
    )
