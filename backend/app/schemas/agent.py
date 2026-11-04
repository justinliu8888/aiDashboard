"""AI 对话入口的请求/响应 schema。字段严格对齐 01_接口协议.md。"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from .change_set import ChangeSet
from .feedback import FeedbackTarget
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


# --------------------------------------------------------------------------
# 输入锁范围（契约表 §5「有效菜单与输入锁」）
# --------------------------------------------------------------------------
#
#   none            不锁
#   menu_only       只锁下拉框与快捷入口；输入框与发送按钮保持可用
#   input_and_menu  输入框 + 发送按钮 + Enter + 快捷入口 + 下拉框全锁
#
# 权威落点是 ``sessions.pending_choice.question.lockScope`` —— 既有列里的
# **新增键**，不另开表、不另开列（契约表 §5.1、§5.3）。
LOCK_SCOPE_NONE = "none"
LOCK_SCOPE_MENU_ONLY = "menu_only"
LOCK_SCOPE_INPUT_AND_MENU = "input_and_menu"
LOCK_SCOPES = (LOCK_SCOPE_NONE, LOCK_SCOPE_MENU_ONLY, LOCK_SCOPE_INPUT_AND_MENU)

# 「模板已生成、等待下一步操作选择」这一阶段的菜单身份（契约表 §5.2）。
LOCKED_INPUT_MENU_QUESTION_ID = "template_edit_confirm"
LOCKED_INPUT_MENU_PROMPT = "请选择下一步操作："
# 「待确认修改计划」这一阶段的菜单身份（契约表 §5.2）。
PENDING_EDIT_MENU_QUESTION_ID = "pending_edit_plan_confirm"

# --------------------------------------------------------------------------
# 权威步骤契约
# --------------------------------------------------------------------------
#
# 「当前这一步」到底是什么，**服务端**要持久记着：步骤身份、会话/草稿归属、
# 步骤版本、允许提交什么。前端请求带上**步骤关联**，后端拿它跟权威记录核对
# —— 不是相信前端的布尔来源标志（``fromChoice`` / ``fromOther`` / ``fromInline``
# 只是本地参数，HTTP 请求里根本没有，校验不了）。
#
# 落点：``sessions.pending_choice.question`` 里新增的键（既有列里的**新增键**，
# 不另开表、不另开列，与 ``lockScope`` 同一口径）。刷新 / 重连后从库里读回的
# 就是同一份权威记录。

#: 菜单步骤：用户从下拉框里选一项。
STEP_KIND_MENU = "menu"
#: 受限修改输入步骤：用户正在**专用输入**里写修改要求。
STEP_KIND_EDIT_REQUEST = "edit_request"
#: 受限步骤允许的提交方式：提交当前步骤的修改要求文字。
STEP_SUBMISSION_EDIT_TEXT = "edit_text"
#: 缺步骤关联（受限步骤）时的拒绝码。
STEP_REF_REQUIRED_CODE = "STEP_REF_REQUIRED"
#: 步骤关联与权威记录对不上时的拒绝码。
STEP_REF_MISMATCH_CODE = "STEP_REF_MISMATCH"

# --------------------------------------------------------------------------
# 方案版本身份（契约表 §4.1.2，工单 #66）
# --------------------------------------------------------------------------
#
# 「本次响应带的是哪一版方案」的稳定 ID 落在 ``AgentResponse.debug`` 的
# 这个键上。
#
# 为什么落在 ``debug`` 而不是 ``domainObjects.reportPlan``：
#
#   - ``debug`` 是**既有字段**，往里加一个键不新增顶层字段、不改
#     ``AGENT_PROTOCOL_VERSION``（工单 #66 的硬边界）；
#   - ``domainObjects.reportPlan`` 是**方案正文**，而方案结构是**字段封闭**
#     的（``ReportPlan`` 用 ``extra="forbid"``）—— 往正文里塞一个不是方案
#     字段的键，等于悄悄改了方案契约；
#   - ``debug`` 里本来就并排放着 ``planId`` / ``planRevision``
#     （设计流程的方案就绪响应），版本 ID 放在它们旁边是同一口径。
#
# 这个 ID 与版本序列里的 ``versionId`` **同源同值**（都从「会话 + 方案 +
# 版本」派生），所以前端拿响应里的 ID 与恢复出来的版本序列直接比就行。
PLAN_VERSION_ID_KEY = "planVersionId"


def derive_lock_scope(question_id: str, prompt: str, options: Any = None) -> str:
    """按契约表 §5.2 的阶段判据推出输入锁范围。

    判据只看**服务端当前这一份有效菜单**的身份，不看历史消息：

    - **有待答选项**（``options`` 非空）→ 锁输入（``input_and_menu``）：
      用户得先从这张卡片里选一个，输入框、发送按钮、Enter 与快捷入口
      一起锁住；
    - **没有选项**（纯文本题 / 说明题）→ 只锁菜单，不锁输入。

    判据的落点是**服务端当前这一份有效菜单**，不是历史消息 ——
    菜单被答掉之后就不再锁。

    显式指定优先：调用方写了 ``lockScope`` 就不推导（见
    ``AgentQuestion._derive_lock_scope``）。
    """
    if options:
        return LOCK_SCOPE_INPUT_AND_MENU
    return LOCK_SCOPE_MENU_ONLY


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
        AgentState.ANALYZING,
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
DESIGN_ACTION_CONFIRM_TEMPLATE = "CONFIRM_TEMPLATE"
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
    templateId: str | None = None
    baseTemplateRevision: int | None = Field(default=None, ge=1)

    # ---------- 权威步骤关联 ----------
    #
    # 本轮请求关联的**步骤**。后端拿它跟持久化的权威记录（``pending_choice``
    # 里那一份）核对：对不上就拒，不执行任何业务动作。
    stepId: str | None = None
    stepVersion: int | None = None


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
    # 输入锁范围（契约表 §5.1）。缺省按 "menu_only" 处理。
    lockScope: str = LOCK_SCOPE_MENU_ONLY

    # ---------- 「其他」自由文本（卡片内纠错入口） ----------
    #
    # ``allowsOther=True`` 时，这张卡片除了选项列表，还提供**一个**「其他」
    # 项与一个**卡片内**的文本输入框。用户在里面写的内容按**这张卡片的
    # 答案（纠正）**处理 —— 承接上文的同一个待办，不是另一条无关消息。
    #
    # 「其他」**不是**选项列表里的一个普通 value：选了「其他」却没填内容
    # 等于没回答。前端按 ``allowsOther`` 渲染「其他」这一项与输入框。
    allowsOther: bool = False
    #: 「其他」这一项的显示文案。
    otherLabel: str = "其他"
    #: 卡片内输入框的提示文案（可空）。
    otherPlaceholder: str = ""

    # ---------- 权威步骤契约（落在 pending_choice.question 里） ----------
    #
    #: 这一步的**身份**。同一步在刷新 / 重连之后必须还是同一个 id ——
    #: 前端拿它关联下一次提交，后端拿它核对。
    stepId: str | None = None
    #: 这一步的**版本**。步骤内容变了就 +1；旧版本的提交要被拒。
    stepVersion: int | None = None
    #: 步骤类型：``"menu"``（选一项）/ ``"edit_request"``（受限修改输入）。
    stepKind: str | None = None
    #: 这一步归属的草稿 id（会话 / 草稿归属）。
    stepDraftId: str | None = None
    #: 这一步允许提交什么（``"edit_text"`` …）。
    allowedSubmissions: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _derive_lock_scope(cls, data: Any) -> Any:
        """没给 lockScope 时，按契约表 §5.2 的阶段判据推导。

        为什么放在模型里推导：同一份菜单对象**既要进响应、又要进
        ``sessions.pending_choice``**（契约表 §5.3「同一个菜单对象既有响应
        又有落库副本，不允许两处各拼一份」）。推导放在模型里，两份副本
        天然一致 —— 生产者（设计服务、编辑服务）不必各写一遍。

        调用方**显式**给了 lockScope 时不推导：

        - 存量的老记录没有这个键 → 按 "menu_only" 处理（契约表 §5.3
          「旧数据的默认行为」），由读回菜单的那一处显式补上；
        - 「已选继续调整 / 修改」这类明确不锁的场景 → 写 "none"。

        判据本身：**有待答选项就锁输入**，没有选项就只锁菜单。
        """
        if isinstance(data, dict) and "lockScope" not in data:
            derived = derive_lock_scope(
                str(data.get("id") or ""),
                str(data.get("prompt") or ""),
                data.get("options"),
            )
            if derived != LOCK_SCOPE_MENU_ONLY:
                return {**data, "lockScope": derived}
        return data

    @field_validator("lockScope", mode="before")
    @classmethod
    def _normalize_lock_scope(cls, value: Any) -> str:
        """非法取值归一成 ``"menu_only"``（契约表 §5.3 的旧数据默认行为）。

        契约表 §5.1 把取值域定死为三个，但服务端**不保证**生产者的写入一定合法
        （手改的库、旧数据、外部写入都可能带进第四个值）。读回时统一归一，
        免得前后端对同一份坏数据给出**不同的锁**——前端的 ``lockScopeOf``
        对认不出的值本来就兜底成 ``menu_only``，这里与它保持同一口径。

        归一后**不锁输入**，是三个合法取值里最弱的一档；真正的安全边界在
        后端对确认与权限的校验（契约表 §8 不变量 6：锁只是引导）。
        """
        text = str(value or "").strip()
        return text if text in LOCK_SCOPES else LOCK_SCOPE_MENU_ONLY


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
    # 草稿工作区状态（契约 0002 §6）：草稿对象身份、版本、能不能撤销 / 重做。
    # 用 dict：它是**工作区快照**，字段随存储演进，不进领域类型。
    # 前端只读它来决定「撤销上一步」这一项能不能点 —— 前端不自己数历史。
    draftWorkspace: dict[str, Any] | None = None


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
    # 报表示意载荷（#51）。草稿阶段就用模拟业务数据渲染的那一份：
    # 方案确认的**同一轮**响应里就带着它，前端不需要二次请求。
    #
    # 结构：``{"previewContext": {...}, "regions": [...]}``，区域载荷与
    # 既有公开预览载荷逐字段同构。
    #
    # **只在响应里**：落库侧按允许列表只写上下文，不写这份载荷
    # （见 session_service 的快照写入）。
    reportPreviewPayload: dict[str, Any] | None = None
    # 本轮性质。见 TURN_KIND_ACTION / TURN_KIND_QUERY_ANSWER。
    turnKind: str = TURN_KIND_ACTION

    # 这一轮的评价目标（20261104 反馈功能）。**可选新增**字段：
    # 旧客户端不认识它，缺了不影响；服务端没登记目标时就是 None。
    #
    # 只有四个字段（id / kind / createdAt / available）—— 定位信息留在服务端。
    feedbackTarget: FeedbackTarget | None = None

    # 这一轮是不是已经落库了。设计流的成功轮由设计存储的同事务提交
    # 一起写掉了会话行与消息，路由层不能再落一遍。
    # 同样是**服务端内部字段**：不进 model_dump()。
    _turn_persisted: bool = PrivateAttr(default=False)

    @property
    def isQueryAnswer(self) -> bool:
        """本轮是不是只读查询回答（不推进业务）。"""
        return self.turnKind == TURN_KIND_QUERY_ANSWER

    @property
    def planVersionId(self) -> str | None:
        """本轮响应带出的方案版本稳定 ID（工单 #66）。

        没有带方案的那一轮返回 None。前端按它去重：重复响应、乱序
        响应与重连不重复追加同一份方案。

        它是读出来的便捷视图，不是协议字段 —— 真正的落点是
        debug.planVersionId（见 PLAN_VERSION_ID_KEY）。
        """
        value = (self.debug or {}).get(PLAN_VERSION_ID_KEY)
        text = str(value).strip() if value else ""
        return text or None

    @property
    def turnPersisted(self) -> bool:
        """本轮是否已经落库（设计流的同事务提交会置位）。"""
        return self._turn_persisted

    @turnPersisted.setter
    def turnPersisted(self, value: bool) -> None:
        self._turn_persisted = bool(value)
