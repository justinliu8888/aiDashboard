"""会话与项目接口的请求 / 响应模型。

另外定义「编辑态跨轮状态」的两组模型：待确认计划（节点 5.5）和同一项待定修改的
澄清进度。字段口径见 20261022 template-editing react 架构改进方案 §8.1.2 的冻结
约定：计划 ID、草稿 revision、目标 region、操作列表、面向用户的摘要。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# 同一项待定修改最多具体追问三轮（方案 §二 Q3 / §五）。
MAX_CLARIFICATION_ROUNDS = 3

# 会话状态里存放编辑态跨轮字段的键名。
# 沿用 06_ReAct改造方案 §6.2 的 SessionStore 字段名（pendingPlan / clarificationCount）；
# 07_会话管理系统方案.md §10 要求两边冲突时以 §6.2 为准。
PENDING_PLAN_KEY = "pendingPlan"
CLARIFICATION_COUNT_KEY = "clarificationCount"
# §6.2 没有这一项：澄清轮数必须知道「属于哪一项待定修改」才算同一项的计数，
# 所以额外存一个标识，换标识 = 换需求 = 轮次重新计数。
CLARIFICATION_KEY_KEY = "clarificationKey"
CLARIFICATION_QUESTION_KEY = "clarificationQuestion"


class CreateSessionRequest(BaseModel):
    title: str | None = None
    workspaceId: str | None = None


class SessionSummary(BaseModel):
    sessionId: str
    title: str
    state: str
    workspaceId: str | None = None
    messageCount: int = 0
    createdAt: str = ""
    updatedAt: str = ""


class SessionListResponse(BaseModel):
    items: list[SessionSummary] = Field(default_factory=list)
    total: int = 0


class SessionMessage(BaseModel):
    seq: int
    role: str
    content: str = ""
    payload: dict[str, Any] | None = None
    createdAt: str = ""


class SessionDetail(BaseModel):
    sessionId: str
    title: str
    state: str
    workspaceId: str | None = None
    designSessionId: str | None = None
    reportDefinitionId: str | None = None
    previewRunId: str | None = None
    messageCount: int = 0
    snapshot: dict[str, Any] | None = None
    pendingChoice: dict[str, Any] | None = None
    # 待确认的编辑计划（独立列，不再藏在 snapshot 里）。
    pendingEditPlan: dict[str, Any] | None = None
    messages: list[SessionMessage] = Field(default_factory=list)
    createdAt: str = ""
    updatedAt: str = ""


class UpdateSessionRequest(BaseModel):
    title: str | None = None
    workspaceId: str | None = None


class CreateWorkspaceRequest(BaseModel):
    name: str
    description: str = ""
    subjectRef: str | None = None
    subjectId: str | None = None


class UpdateWorkspaceRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class WorkspaceItem(BaseModel):
    id: str
    name: str
    description: str = ""
    subjectRef: str | None = None
    subjectId: str | None = None
    sessionCount: int = 0
    createdAt: str = ""
    updatedAt: str = ""


# ---------- 编辑态跨轮状态 ----------


class PendingEditPlan(BaseModel):
    """待确认的编辑计划（节点 5.5，跨轮持久化）。

    字段对应 §8.1.2 冻结约定的五项：
        planId         计划 id
        draftRevision  计划对应的**草稿** revision
        targetRegionId 目标 region
        operations     操作列表
        summary        面向用户的摘要

    operations 里每一项描述「改什么」，具体字段由编辑引擎一侧定义，
    这里只当不透明数据存取，不复述一份，避免两处定义漂移。
    允许多带字段（extra=allow），别把调用方已经写好的信息丢掉。
    """

    model_config = ConfigDict(extra="allow")

    planId: str
    draftRevision: int = 0
    targetRegionId: str = ""
    operations: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""
    createdAt: str = ""


class EditClarification(BaseModel):
    """同一项待定修改的澄清进度。

    key   标识「是哪一项待定修改」；换 key 即换需求，轮次重新计数。
    rounds 只数真正抛给用户的追问；工具 / 模型故障不调用记录接口就不占轮次。
    """

    key: str = ""
    rounds: int = 0
    maxRounds: int = MAX_CLARIFICATION_ROUNDS
    lastQuestion: str = ""
