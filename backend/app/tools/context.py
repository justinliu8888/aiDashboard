"""工具的会话上下文。

工具本身是无状态的纯函数，但有几个工具必须能读到「当前这一轮」的草稿、
上次校验结果、用户选中的 region —— 这些不属于工具的入参（塞进模型参数
既浪费 token 又容易被模型改坏），而属于**会话状态**。

这里用 ContextVar 绑定，与 ``app.services.session_context`` 同一套做法：
    - 请求进入时 bind；
    - 工具执行时 read；
    - 请求结束时 clear。

绑定对象只放**只读快照**，工具不得回写。
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from app.schemas.agent import AgentState
from app.schemas.change_set import ChangeSet
from app.schemas.report_definition import ReportDefinitionDraft
from app.schemas.report_policy import ReportPolicyDraft


@dataclass(frozen=True)
class ToolContext:
    """一轮请求内，工具可见的只读上下文。"""

    state: AgentState
    subject_ref: str = ""
    subject_id: str = ""
    # 项目范围。查目录 / 查指标这类工具要按它做授权过滤，
    # 否则模型能问到当前项目看不到的数据源。
    project_id: str = ""
    selected_region_id: str | None = None
    base_definition_revision: int | None = None
    definition: ReportDefinitionDraft | None = None
    policy: ReportPolicyDraft | None = None
    last_change_set: ChangeSet | None = None
    last_validation_result: dict[str, Any] | None = None
    last_report_run: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_tool_context: ContextVar[ToolContext | None] = ContextVar(
    "tool_context",
    default=None,
)


def bind_tool_context(ctx: ToolContext) -> None:
    """绑定本轮上下文。"""
    _tool_context.set(ctx)


def current_tool_context() -> ToolContext | None:
    """取本轮上下文；未绑定时返回 None（工具需自行处理无上下文的情况）。"""
    return _tool_context.get()


def clear_tool_context() -> None:
    """清除本轮上下文。"""
    _tool_context.set(None)


@dataclass(frozen=True)
class ToolScope:
    """工具查询的授权范围。"""

    project_id: str
    subject_ref: str
    subject_id: str


def require_scope() -> ToolScope:
    """取本轮的授权范围。

    查目录 / 查指标 / 查能力这类工具必须先过这一关：
    没有范围就没法判断能不能访问，只能拒绝执行 —— 宁可查不到，
    也不能让模型问到当前项目看不到的数据。
    """
    ctx = current_tool_context()
    if ctx is None:
        raise PermissionError("未绑定本轮上下文，无法确定可查询的数据范围")
    return ToolScope(
        project_id=ctx.project_id,
        subject_ref=ctx.subject_ref,
        subject_id=ctx.subject_id,
    )
