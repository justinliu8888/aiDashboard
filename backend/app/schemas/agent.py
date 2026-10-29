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

# 本轮的性质。
#
#   action        正常业务轮：可能推进状态机（默认）
#   query_answer  只读查询回答轮：只回答，不推进业务
#
# 前端靠 turnKind 判断「这一轮不要自动推进」：查询回答可能保留着
# SAVE_DEFINITION_DRAFT / VALIDATING 这类会自动 __advance__ 的状态，
# 不能只看 state 就再推一次。
TURN_KIND_ACTION = "action"
TURN_KIND_QUERY_ANSWER = "query_answer"


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


# 设计动作（规格 #18「聊天与恢复接口」）：
#   CONFIRM_PLAN  确认当前文字方案
#   REQUEST_DATA  明确要求连接数据或出数
#
# 首波只实现 CONFIRM_PLAN 的**边界校验**（版本冲突、过期拒绝）；
# 模板生成属于工单 02，REQUEST_DATA 属于工单 05，本工单不接。
DESIGN_ACTION_CONFIRM_PLAN = "CONFIRM_PLAN"
DESIGN_ACTION_REQUEST_DATA = "REQUEST_DATA"


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

    # 新建设计流程的动作与版本（规格 #18）。
    # 按钮必须明确传递动作与所依据的版本，服务端据此做冲突检查；
    # 客户端带的版本不能当授权依据，只用来识别「过期」。
    designAction: str | None = None
    planId: str | None = None
    basePlanRevision: int | None = None


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

    # 本轮是不是只读查询轮。同样是服务端内部字段：只有 AgentService
    # 在路由判定后写入，业务 Agent 据此换工具白名单和系统提示词。
    _query_mode: bool = PrivateAttr(default=False)

    @property
    def collectedInfo(self) -> dict[str, Any]:
        return self._collected_info

    @collectedInfo.setter
    def collectedInfo(self, value: dict[str, Any] | None) -> None:
        self._collected_info = dict(value or {})

    @property
    def queryMode(self) -> bool:
        """本轮是不是只读查询轮（只查事实、只提交答案，不推进业务）。"""
        return self._query_mode

    @queryMode.setter
    def queryMode(self, value: bool) -> None:
        self._query_mode = bool(value)


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
    # 文字方案（规格 #18）。用 dict 而不是领域类型：
    # 老会话恢复时可能缺新增字段，缺字段的旧记录仍要能读出来，
    # 不能因为一个可选对象把整条会话读挂。
    reportPlan: dict[str, Any] | None = None
    # 模板设计（工单 02）。同样是 dict：老会话没有这个字段时要能读出来。
    templateDesign: dict[str, Any] | None = None


class AgentResponse(BaseModel):
    agentProtocolVersion: str = AGENT_PROTOCOL_VERSION
    conversationId: str
    state: AgentState
    reply: str
    designSessionId: str | None = None
    # 新建设计阶段（规格 #18）。独立于 state：
    # 旧草稿的 state 与只读问答的 turnKind 保持原义，
    # 新建流程的「待确认方案」不借用 state 来表达。
    designPhase: str | None = None
    domainObjects: AgentResponseDomainObjects = Field(
        default_factory=AgentResponseDomainObjects
    )
    questions: list[AgentQuestion] = Field(default_factory=list)
    blocks: list[AgentBlock] = Field(default_factory=list)
    toolRequests: list[ToolRequest] = Field(default_factory=list)
    uiActions: list[str] = Field(default_factory=list)
    error: AgentError | None = None
    debug: dict[str, Any] = Field(default_factory=dict)
    # 本轮性质。见 TURN_KIND_ACTION / TURN_KIND_QUERY_ANSWER。
    turnKind: str = TURN_KIND_ACTION

    # 这一轮是不是已经落库了。设计流的成功轮由设计存储的同事务提交
    # 一起写掉了会话行与消息，路由层不能再落一遍。
    # 同样是**服务端内部字段**：不进 model_dump()。
    _turn_persisted: bool = PrivateAttr(default=False)

    @property
    def isQueryAnswer(self) -> bool:
        """本轮是不是只读查询回答（不推进业务）。"""
        return self.turnKind == TURN_KIND_QUERY_ANSWER

    @property
    def turnPersisted(self) -> bool:
        """本轮是否已经落库（设计流的同事务提交会置位）。"""
        return self._turn_persisted

    @turnPersisted.setter
    def turnPersisted(self, value: bool) -> None:
        self._turn_persisted = bool(value)
