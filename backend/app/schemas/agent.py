"""AI 对话入口的请求/响应 schema。字段严格对齐 01_接口协议.md。"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr

from .change_set import ChangeSet
from .report_definition import ReportDefinitionDraft
from .report_policy import ReportPolicyDraft
from .session import PendingEditPlan


AGENT_PROTOCOL_VERSION = "1.0"

# 流程自动推进的内部指令。节点 6-12 之间，前端收到状态后自动再发一次此消息触发下一步。
# 用户从未输入过它，落库时要跳过（见 session_service.record_turn）。
ADVANCE_MESSAGE = "__advance__"


class AgentState(str, Enum):
    ANALYZING = "ANALYZING"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    BLOCKED_OR_UNSUPPORTED = "BLOCKED_OR_UNSUPPORTED"
    INTERRUPT_CONFIRMING = "INTERRUPT_CONFIRMING"
    DEFINITION_DRAFT_READY = "DEFINITION_DRAFT_READY"
    WAITING_USER_CONFIRM = "WAITING_USER_CONFIRM"
    USER_EDITING_TEMPLATE = "USER_EDITING_TEMPLATE"
    SAVE_DEFINITION_DRAFT = "SAVE_DEFINITION_DRAFT"
    VALIDATING = "VALIDATING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    PREVIEWING = "PREVIEWING"
    PREVIEW_READY = "PREVIEW_READY"
    RENDERING = "RENDERING"
    READY = "READY"


# §2.2 允许的状态转移
ALLOWED_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.ANALYZING: {
        AgentState.DEFINITION_DRAFT_READY,
        # 允许生成完就直接进入等待确认（草稿一步到位）
        AgentState.WAITING_USER_CONFIRM,
        AgentState.NEEDS_CLARIFICATION,
        AgentState.BLOCKED_OR_UNSUPPORTED,
        AgentState.INTERRUPT_CONFIRMING,
    },
    AgentState.NEEDS_CLARIFICATION: {
        AgentState.ANALYZING,
        AgentState.NEEDS_CLARIFICATION,
        AgentState.WAITING_USER_CONFIRM,
    },
    AgentState.INTERRUPT_CONFIRMING: {
        AgentState.ANALYZING,
        AgentState.WAITING_USER_CONFIRM,
        AgentState.VALIDATING,
        AgentState.PREVIEWING,
        AgentState.READY,
    },
    AgentState.DEFINITION_DRAFT_READY: {AgentState.WAITING_USER_CONFIRM},
    AgentState.WAITING_USER_CONFIRM: {
        AgentState.USER_EDITING_TEMPLATE,
        AgentState.SAVE_DEFINITION_DRAFT,
        AgentState.ANALYZING,
        # 允许用户在确认阶段触发局部修改，进入新的草稿就绪状态
        AgentState.DEFINITION_DRAFT_READY,
        # 允许在等待确认时追问 / 提问 / 闲聊，由 ContextEngine 前置判定
        AgentState.NEEDS_CLARIFICATION,
        AgentState.BLOCKED_OR_UNSUPPORTED,
        AgentState.INTERRUPT_CONFIRMING,
    },
    AgentState.USER_EDITING_TEMPLATE: {
        AgentState.WAITING_USER_CONFIRM,
        AgentState.SAVE_DEFINITION_DRAFT,
        AgentState.ANALYZING,
        AgentState.NEEDS_CLARIFICATION,
        AgentState.INTERRUPT_CONFIRMING,
    },
    AgentState.SAVE_DEFINITION_DRAFT: {
        AgentState.VALIDATING,
        AgentState.INTERRUPT_CONFIRMING,
    },
    AgentState.VALIDATING: {
        AgentState.PREVIEWING,
        AgentState.VALIDATION_FAILED,
        AgentState.INTERRUPT_CONFIRMING,
    },
    AgentState.VALIDATION_FAILED: {AgentState.ANALYZING},
    AgentState.PREVIEWING: {AgentState.PREVIEW_READY, AgentState.INTERRUPT_CONFIRMING},
    AgentState.PREVIEW_READY: {AgentState.RENDERING},
    AgentState.RENDERING: {AgentState.READY},
    AgentState.READY: {AgentState.ANALYZING, AgentState.USER_EDITING_TEMPLATE},
    AgentState.BLOCKED_OR_UNSUPPORTED: set(),
}


class AgentRequestContext(BaseModel):
    designSessionId: str | None = None
    reportPolicyId: str | None = None
    reportDefinitionId: str | None = None
    baseDefinitionRevision: int | None = None
    selectedRegionId: str | None = None
    visibleRegionIds: list[str] | None = None
    projectId: str
    subjectRef: str
    subjectId: str


class ValidationIssue(BaseModel):
    layer: str
    code: str
    path: str | None = None
    message: str
    suggestion: str | None = None


class ValidationResult(BaseModel):
    passed: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


class QualitySummary(BaseModel):
    coverage: float | None = None
    missing: int | None = None
    duplicates: int | None = None
    freshnessSeconds: int | None = None


class ReportRunPreview(BaseModel):
    reportRunId: str
    status: str  # RUNNING / SUCCEEDED / PARTIAL / FAILED
    logicalTime: str | None = None
    windowLabel: str | None = None
    qualitySummary: QualitySummary | None = None
    resultRef: str


class AgentRequestSnapshots(BaseModel):
    reportPolicy: ReportPolicyDraft | None = None
    reportDefinition: ReportDefinitionDraft | None = None
    lastChangeSet: ChangeSet | None = None
    lastValidationResult: ValidationResult | None = None
    lastReportRun: ReportRunPreview | None = None


class AgentRequest(BaseModel):
    agentProtocolVersion: str = AGENT_PROTOCOL_VERSION
    conversationId: str
    resumeOfConversationId: str | None = None
    # 会话 id：后端生成，一条对话从建到删不变。不传则退回 conversationId。
    sessionId: str | None = None
    # 本条用户消息的幂等键：同一轮重发时复用同一个 id，后端据此去重。
    messageId: str | None = None
    state: AgentState
    message: str
    context: AgentRequestContext
    snapshots: AgentRequestSnapshots = Field(default_factory=AgentRequestSnapshots)

    # 路由层（Deep Route）判定"已经问过、用户已经答过"的信息。
    # 不带进来业务 Agent 会重复追问用户已经说过的事。
    #
    # 这是**服务端内部字段**，不是协议字段：
    #   - 客户端提交不生效（不是 Pydantic 字段，额外键被忽略）；
    #   - 只有后端在路由后才写入；
    #   - 不进 model_dump()，不会外泄到协议响应里。
    # 所以 01_接口协议.md 的 AgentRequest 字段表不需要加它。
    _collected_info: dict[str, Any] = PrivateAttr(default_factory=dict)

    @property
    def collectedInfo(self) -> dict[str, Any]:
        return self._collected_info

    @collectedInfo.setter
    def collectedInfo(self, value: dict[str, Any] | None) -> None:
        self._collected_info = dict(value or {})


class QuestionOption(BaseModel):
    value: str
    label: str


class AgentQuestion(BaseModel):
    id: str
    prompt: str
    expects: str  # TEXT / SINGLE_CHOICE / MULTI_CHOICE / DATE_RANGE
    options: list[QuestionOption] = Field(default_factory=list)
    relatesTo: str | None = None
    selectionOnly: bool = False
    allowRestart: bool = False


class AgentBlock(BaseModel):
    code: str  # PERMISSION_DENIED / CAPABILITY_NOT_SUPPORTED / DANGEROUS_ACTION / OUT_OF_SCOPE
    message: str
    alternatives: list[str] = Field(default_factory=list)


class ToolRequest(BaseModel):
    id: str
    type: str
    target: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class AgentResponseDomainObjects(BaseModel):
    reportPolicyDraft: ReportPolicyDraft | None = None
    reportDefinitionDraft: ReportDefinitionDraft | None = None
    changeSet: ChangeSet | None = None
    reportRunPreview: ReportRunPreview | None = None
    pendingEditPlan: PendingEditPlan | None = None


class AgentResponse(BaseModel):
    agentProtocolVersion: str = AGENT_PROTOCOL_VERSION
    conversationId: str
    state: AgentState
    reply: str
    designSessionId: str | None = None
    domainObjects: AgentResponseDomainObjects = Field(
        default_factory=AgentResponseDomainObjects
    )
    questions: list[AgentQuestion] = Field(default_factory=list)
    blocks: list[AgentBlock] = Field(default_factory=list)
    toolRequests: list[ToolRequest] = Field(default_factory=list)
    uiActions: list[str] = Field(default_factory=list)
    error: AgentError | None = None
    debug: dict[str, Any] = Field(default_factory=dict)
