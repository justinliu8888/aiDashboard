"""请求级会话上下文。

会话 key 的解析规则（见 07_会话管理系统方案.md §5.1）：
    sessionId 由后端生成，一条对话从建到删不变；
    conversationId 由前端生成，一次请求线程，停止生成时会换新。

后端内部凡是「按对话存状态」的地方（选择锁、模板预览流程）都统一用 session key，
这样停止生成换 conversationId 后状态不丢，恢复历史会话时也能按 sessionId 找回。
"""
from __future__ import annotations

from contextvars import ContextVar

from app.schemas.agent import AgentRequest


_session_key: ContextVar[str | None] = ContextVar("session_key", default=None)


def bind_session_key(request: AgentRequest) -> str:
    """绑定本次请求的会话 key，返回解析结果。"""
    key = request.sessionId or request.conversationId
    _session_key.set(key)
    return key


def current_session_key() -> str | None:
    return _session_key.get()


def clear_session_key() -> None:
    _session_key.set(None)
