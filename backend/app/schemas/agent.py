"""AI 对话入口的请求/响应 schema。字段严格对齐 01_接口协议.md。"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .change_set import ChangeSet
from .report_definition import ReportDefinitionDraft
from .report_policy import ReportPolicyDraft


AGENT_PROTOCOL_VERSION = "1.0"


class AgentState(str, Enum):
    ANALYZING = "ANALYZING"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    BLOCKED_OR_UNSUPPORTED = "BLOCKED_OR_UNSUPPORTED"
    DEFINITION_DRAFT_READY = "DEFINITION_DRAFT_READY"
    WAITING_USER_CONFIRM = "WAITING_USER_CONFIRM"
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
    },
    AgentState.NEEDS_CLARIFICATION: {AgentState.ANALYZING},
    AgentState.DEFINITION_DRAFT_READY: {AgentState.WAITING_USER_CONFIRM},
    AgentState.WAITING_USER_CONFIRM: {
        AgentState.SAVE_DEFINITION_DRAFT,
        AgentState.ANALYZING,
        # 允许用户在确认阶段触发局部修改，进入新的草稿就绪状态
        AgentState.DEFINITION_DRAFT_READY,
    },
    AgentState.SAVE_DEFINITION_DRAFT: {AgentState.VALIDATING},
    AgentState.VALIDATING: {AgentState.PREVIEWING, AgentState.VALIDATION_FAILED},
    AgentState.VALIDATION_FAILED: {AgentState.ANALYZING},
    AgentState.PREVIEWING: {AgentState.PREVIEW_READY},
    AgentState.PREVIEW_READY: {AgentState.RENDERING},
    AgentState.RENDERING: {AgentState.READY},
    AgentState.READY: {AgentState.ANALYZING},
    AgentState.BLOCKED_OR_UNSUPPORTED: set(),
}


class AgentRequestContext(BaseModel):
    designSessionId: str | None = None
    reportPolicyId: str | None = None
    reportDefinitionId: str | None = None
    baseDefinitionRevision: int | None = None
    selectedRegionId: str | None = None
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
    state: AgentState
    message: str
    context: AgentRequestContext
    snapshots: AgentRequestSnapshots = Field(default_factory=AgentRequestSnapshots)


class QuestionOption(BaseModel):
    value: str
    label: str


class AgentQuestion(BaseModel):
    id: str
    prompt: str
    expects: str  # TEXT / SINGLE_CHOICE / MULTI_CHOICE / DATE_RANGE
    options: list[QuestionOption] = Field(default_factory=list)
    relatesTo: str | None = None


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
