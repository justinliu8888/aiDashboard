"""会话存储的数据模型。

三个概念：
    Workspace  项目（侧边栏「历史项目」分组），会话的归属容器
    Session    会话（一条对话），主键 sessionId
    Message    消息（一条用户或助手气泡）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkspaceRecord:
    id: str
    name: str
    description: str = ""
    subjectRef: str | None = None
    subjectId: str | None = None
    createdAt: str = ""
    updatedAt: str = ""
    sessionCount: int = 0


@dataclass
class SessionRecord:
    id: str
    workspaceId: str | None = None
    title: str = "新对话"
    state: str = "ANALYZING"
    designSessionId: str | None = None
    reportDefinitionId: str | None = None
    previewRunId: str | None = None
    messageCount: int = 0
    snapshot: dict[str, Any] | None = None
    pendingChoice: dict[str, Any] | None = None
    # 待确认的编辑计划。独立成列，不塞在 snapshot 里：
    # 取出计划必须是「条件删除」的原子操作，跨进程 / 多 worker 都要成立。
    pendingEditPlan: dict[str, Any] | None = None
    createdAt: str = ""
    updatedAt: str = ""
    deletedAt: str | None = None


@dataclass
class MessageRecord:
    id: int
    sessionId: str
    seq: int
    role: str
    content: str = ""
    payload: dict[str, Any] | None = None
    clientMsgId: str | None = None
    createdAt: str = ""
