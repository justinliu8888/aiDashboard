"""新建流程的「文字方案」编排（#19 协调者接线）。

这一层把三个独立模块串成一条真实链路：

    C 认领操作 → （事务外）B 调模型出候选 → A 领域校验 →
    C 同事务提交方案 + 阶段 + 本轮消息

边界：

    - 不建模型、不复制凭证（模型客户端在 ``report_design_model_client``）；
    - 不确认、不取数、不调用旧报表 builder（那是 #20 / #05 的范围）；
    - 只允许追加消息；查询轮不改设计对象；
    - 模型工作在事务之外，提交时核对基础版本。

**进入条件**（这是本工单最需要说清的一处判断）：

    1. 会话已经有设计记录 → 本轮在设计流程里；
    2. 或者：请求带了显式 ``sessionId`` + ``messageId``，
       且这个会话**还没有旧流程的业务状态**（草稿 / 待确认菜单 /
       待确认修改计划）→ 这是**新建**会话的需求，进入设计流程。

第 2 条不是随手挑的：

    - ``messageId`` 是操作认领的幂等键。C 的 ``claim_operation`` 明确要求
      消息 id，规格也写明「新建请求使用显式 sessionId、messageId 关联
      会话和操作」；没有它就没有可认领的操作。
    - 「没有旧流程的业务状态」= 这条对话还没开始做任何业务动作，
      也就是**新建**。用户可以先问一句再提需求，那轮只留下只读问答的
      记录，不该把后面的新建请求挡在设计流程外面。
      已有会话（已经有草稿、菜单锁或待确认计划、但没进过设计阶段）
      保持旧协议语义，老草稿编辑与恢复不受影响
      （规格：「对未进入新建阶段的已有会话保持原协议语义」）。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from app.agents.report_plan_agent import (
    KIND_CLARIFICATION,
    KIND_FAILURE,
    KIND_PLAN_CANDIDATE,
    generate_candidate,
)
from app.schemas.agent import (
    DESIGN_ACTION_CONFIRM_PLAN,
    DESIGN_ACTION_CONFIRM_TEMPLATE,
    AgentError,
    AgentQuestion,
    AgentRequest,
    AgentResponse,
    AgentResponseDomainObjects,
    AgentState,
)
from app.schemas.report_plan import (
    PlanContractError,
    ReportPlan,
    ReportPlanError,
    validate_section_contract,
)
from app.schemas.session import (
    CLARIFICATION_COUNT_KEY,
    CLARIFICATION_KEY_KEY,
    CLARIFICATION_QUESTION_KEY,
)
from app.services.report_plan_service import apply_candidate
from app.services.report_template_service import build_template_design
from app.services.report_draft_service import build_policy_draft
from app.services.template_definition_service import build_template_definition
from app.schemas.report_definition import ReportDefinitionDraft
from app.services.session_context import current_session_key
from app.services.session_service import session_service
from app.storage.report_design_repository import (
    CLAIM_KIND_CONFLICT,
    CLAIM_KIND_DUPLICATE,
    COMMIT_KIND_CONFLICT,
    DESIGN_PHASE_PLAN_NEEDED,
    DESIGN_PHASE_PLAN_WAITING_CONFIRM,
    DESIGN_PHASE_TEMPLATE_READY,
    DESIGN_PHASE_DRAFT_READY,
    OPERATION_SUCCEEDED,
    DesignOperationStateError,
    DesignPlanInvalidError,
    DesignTemplateConflictError,
    DesignTemplateNotFoundError,
    ReportDesignRepository,
)
from app.storage.report_design_sqlite import SqliteReportDesignRepository


logger = logging.getLogger(__name__)

#: 方案 id 前缀。与旧草稿的 plan_xxx（待确认修改计划）区分开。
PLAN_ID_PREFIX = "rplan_"

#: 新建流程的 UI 动作。刻意与旧的确认报表动作（confirm_framework 等）
#: 分开，避免「确认方案」被当成「确认报表」而触发保存与出数。
UI_ACTION_PLAN_READY = "plan_ready"
UI_ACTION_PLAN_CLARIFICATION = "plan_clarification"
UI_ACTION_PLAN_CONFIRM_PENDING = "plan_confirm_pending"
UI_ACTION_PLAN_RETRY = "plan_retry"
#: 模板已就绪。刻意与旧的确认报表动作分开。
UI_ACTION_TEMPLATE_READY = "template_ready"


# --------------------------------------------------------------------------
# 确认语气的识别
#
# 规格 #18：「按钮明确传递动作与版本；自然语言确认由服务端识别，
# 并绑定服务端当前方案版本」，以及「「差不多了」等不能确定是否认可的
# 表达不直接推进，返回明确确认入口」「一次消息同时提出修改和确认时，
# 先展示更新后的方案并等待对该版本的确认」。
#
# 判定方式是「剥掉确认词之后还剩不剩实质内容」：
# 只有确认词 → 纯确认；剩下还有要求 → 不是确认，是修改。
# --------------------------------------------------------------------------

#: 空白与标点。判断语义时先去掉它们。
_NOISE_PATTERN = re.compile(r"[\s，。！？、；：,.!?;:~…—\-·\"'“”‘’（）()【】\[\]]+")

#: 明确认可的说法。
_CONFIRM_MARKERS: tuple[str, ...] = (
    "确认",
    "确定",
    "同意",
    "没问题",
    "就按这个",
    "就按这份",
    "就按它",
    "照这个",
    "照这份",
    "就这样",
    "可以",
    "好的",
    "好",
    "行",
    "ok",
)

#: 确认时常见的、没有信息量的填充词。剥掉它们才知道这句话是不是「只有确认」。
_CONFIRM_FILLERS: tuple[str, ...] = (
    "方案",
    "模板",
    "模版",
    "这个",
    "这份",
    "当前",
    "这一",
    "最新",
    "一下",
    "一份",
    "吧",
    "了",
    "的",
    "啊",
    "呀",
    "哦",
)

#: 确认句里可以剩下的连接词。它们只是把确认语气连上，不带要求。
#:
#: 判定宁可保守：残留里出现任何**别的**内容，就当这条消息还带着要求 ——
#: 规格写明「一次消息同时提出修改和确认时，先展示更新后的方案」，
#: 误当成修改只是多出一版方案，误当成确认会**丢掉**用户的修改要求。
_CONFIRM_CONNECTORS: tuple[str, ...] = (
    "按",
    "照",
    "继续",
    "执行",
    "往下",
    "下去",
    "就这样",
    "就行",
    "这么办",
    "来",
    "走",
    "办",
)

#: 模糊认可：不能确定是否认可。规格 #18 明确要求「不直接推进」。
_VAGUE_APPROVAL_MARKERS: tuple[str, ...] = (
    "差不多了",
    "差不太多",
    "差不多",
    "就那样",
    "就这样吧",
    "先这样",
    "基本可以",
    "基本行",
    "可以吧",
    "行吧",
)

#: 「第 2 版」这类版本指代。确认哪一版由服务端决定，不需要用户再说一遍。
_VERSION_PATTERN = re.compile(r"第\d+版")


# --------------------------------------------------------------------------
# 方案契约校验
#
# 判据本身**只有一份**：``app.schemas.report_plan.validate_section_contract``
# （那个模块里写明「唯一权威判据」）。编排层**不抄第二份**。
#
# 为什么编排层要再查一遍：方案校验与模板生成之间的任何一条路径，
# 都可能带着一个没声明组件类型、或声明了非法类型的板块进来。组件类型
# 缺失或非法**必须明确失败**：
#
#   - 不能猜一个类型顶上（猜出来的草稿与方案对不上，用户毫无察觉）；
#   - 不能当成「已接上数据的成功区域」（那是把非法输入当成成功）。
#
# 失败原因要**点名是哪个板块**：只说「componentType 不合法」，
# 用户根本不知道要改哪一块。权威判据已经把板块标识写进异常消息里，
# 这里**原样**透传。
#
# 为什么不再抄一份弱判据：抄的那份只查组件类型、不查取数板块的指标意图。
# 两份一旦漂移，编排层就会放过非法板块，把本该在这里拦下的错误推迟到
# 模板层才炸，错误文案也随之改变。
# --------------------------------------------------------------------------


def _reason_text(exc: Exception) -> str:
    """异常的可读原因，**原样**透传给用户。

    不做二次概括、不截断：下游（方案契约层、模板层）已经把「哪个板块
    出了什么事」写进异常消息里了，这里再概括一次等于把可定位的信息
    丢掉。截断尤其危险 —— 板块名往往在消息末尾。
    """
    message = str(exc).strip()
    return message or exc.__class__.__name__


def _plan_contract_error(sections: Any) -> str | None:
    """检查方案板块的契约（组件类型 + 取数板块的指标意图）。

    返回可读原因（点名板块）；合法时返回 ``None``。

    这是**确认前的最后一道关**，也是「方案落库前」的兜底：模型适配层与
    领域层已经各有一道校验，这里再挡一次，是为了拦住绕过那两层的路径
    （历史数据、手工改库、将来新增的调用方）。

    判据直接调用方案契约层的 ``validate_section_contract`` —— 编排层
    **不保留第二份实现**，否则两份判据会漂移。
    """
    if not isinstance(sections, list):
        return None
    for index, section in enumerate(sections):
        try:
            validate_section_contract(section, index=index)
        except PlanContractError as exc:
            return _reason_text(exc)
    return None


def _normalize(message: str) -> str:
    """去掉空白与标点，只留下可判定的字。"""
    return _NOISE_PATTERN.sub("", message or "")


def _strip_markers(text: str, markers: tuple[str, ...]) -> str:
    rest = text
    for marker in markers:
        rest = rest.replace(marker, "")
    return rest


def is_pure_confirmation(message: str) -> bool:
    """这句话是不是**纯粹**在认可当前方案。

    剥掉确认词与填充词之后，剩下���能是连接词（「按」「继续」这类）——
    只要还剩下别的内容，就说明这条消息**带着要求**，那是修改而不是确认。

    「确认」是；「确认，按这个方案继续」也是；
    「改成柱状图，确认」不是。
    """
    text = _normalize(message)
    if not text:
        return False
    # 模糊认可优先：「行吧」这类不算明确确认（调用方按模糊认可处理）。
    if is_vague_approval(text):
        return False
    if not any(marker in text for marker in _CONFIRM_MARKERS):
        return False
    rest = _strip_markers(text, _CONFIRM_MARKERS)
    rest = _VERSION_PATTERN.sub("", rest)
    rest = _strip_markers(rest, _CONFIRM_FILLERS)
    rest = _strip_markers(rest, _CONFIRM_CONNECTORS)
    return not rest


def is_vague_approval(message: str) -> bool:
    """这句话是不是「不能确定是否认可」的说法。

    「差不多了」「行吧」这类。规格要求它们**不推进**设计版本，
    只返回明确确认入口。
    """
    text = _normalize(message)
    if not text:
        return False
    if not any(marker in text for marker in _VAGUE_APPROVAL_MARKERS):
        return False
    rest = _strip_markers(text, _VAGUE_APPROVAL_MARKERS)
    rest = _strip_markers(rest, _CONFIRM_FILLERS)
    return not rest


class ReportDesignService:
    """文字方案的编排。"""

    def __init__(
        self,
        repository: ReportDesignRepository | None = None,
        model_call: Any | None = None,
    ) -> None:
        self._injected_repository = repository
        self._repository: ReportDesignRepository | None = None
        self._repository_source: Any = None
        self._model_call = model_call

    # ---------- 依赖 ----------

    @property
    def repository(self) -> ReportDesignRepository:
        """设计存储。

        连接与锁都跟会话存储**同一条**、**同一把**：
        同一条连接才能落进同一笔事务，同一把锁才不会两边交错写。

        会话存储换了（测试换库、服务重启换库）时，设计存储跟着重建 ——
        拿着旧连接写新库，会把方案写到一个谁也读不到的地方。
        """
        if self._injected_repository is not None:
            return self._injected_repository

        session_repo = session_service.repository
        if self._repository is None or self._repository_source is not session_repo:
            self._repository = SqliteReportDesignRepository(
                connection=session_repo.conn,
                lock=session_repo.lock,
            )
            self._repository.initialize_schema()
            self._repository_source = session_repo
        return self._repository

    @property
    def model_call(self) -> Any:
        if self._model_call is None:
            from app.services.report_design_model_client import ReportDesignModelClient

            self._model_call = ReportDesignModelClient()
        return self._model_call

    def reset_for_tests(
        self,
        *,
        repository: ReportDesignRepository | None = None,
        model_call: Any | None = None,
    ) -> None:
        """换掉依赖（测试用）。

        ``repository=None`` 表示恢复成「跟随会话存储」的自建模式。
        """
        self._injected_repository = repository
        if repository is None:
            self._repository = None
            self._repository_source = None
        if model_call is not None:
            self._model_call = model_call

    # ---------- 判断 ----------

    @staticmethod
    def session_key(request: AgentRequest) -> str:
        return current_session_key() or session_service.resolve_session_id(request)

    def load_state(self, request: AgentRequest) -> dict[str, Any] | None:
        """读会话的设计状态；没进过设计流程返回 None。"""
        try:
            return self.repository.load(self.session_key(request))
        except Exception:  # noqa: BLE001
            logger.exception("[report_design] 读设计状态失败")
            return None

    def is_new_build(self, request: AgentRequest) -> bool:
        """这是不是一条**新建**会话的需求。

        两个条件：

            1. 请求带了显式 ``sessionId`` + ``messageId``。
               ``messageId`` 是操作认领的幂等键 —— C 的 ``claim_operation``
               明确要求消息 id，规格也写明「新建请求使用显式 sessionId、
               messageId 关联会话和操作」；没有它就没有可认领的操作。
            2. 这个会话**还没有旧流程的业务状态**。

        第 2 条不是「库里没有行」：用户可以先问一句「你能做什么」，
        那轮会留下会话行，但只留下**只读问答**的记录 —— 草稿、待确认菜单、
        待确认修改计划一个都没有。这样的会话仍然是**新建**：
        下一句「做一份 PUE 报表」就该得到方案，而不是掉回旧流程。
        """
        if not request.sessionId or not request.messageId:
            return False
        if request.message.strip() == "__advance__":
            return False
        session_key = self.session_key(request)
        try:
            existing = session_service.repository.get_session(session_key)
        except Exception:  # noqa: BLE001
            logger.exception("[report_design] 读会话失败")
            return False
        if existing is None:
            return True
        return not self._has_old_flow_business_state(existing)

    @staticmethod
    def _has_old_flow_business_state(existing: Any) -> bool:
        """这个会话是不是已经在**旧流程**里跑起来了。

        旧流程的业务状态有四样：有草稿、有待确认菜单锁、有待确认修改计划、
        追问到一半（有澄清轮次或已经推进到业务节点）。
        只要有一样，这个会话就不是「新建」，保持旧协议语义。
        """
        snapshot = existing.snapshot or {}
        if isinstance(snapshot.get("definition"), dict):
            return True
        if existing.pendingChoice:
            return True
        if existing.pendingEditPlan:
            return True
        if any(
            key in snapshot
            for key in (
                CLARIFICATION_COUNT_KEY,
                CLARIFICATION_KEY_KEY,
                CLARIFICATION_QUESTION_KEY,
            )
        ):
            return True
        if existing.state not in ("ANALYZING", "NEEDS_CLARIFICATION"):
            return True
        return False

    # ---------- 查询轮挂回设计状态 ----------

    def keep_state_response(
        self, request: AgentRequest, state: dict[str, Any] | None
    ) -> AgentResponse:
        """原样带回当前设计状态，不推进任何东西。

        用于设计流程里的内部控制消息（``__advance__``）：它是旧状态机的
        内部指令，在新建流程里没有对应的下一步，原样返回即可 ——
        掉进业务 Agent 会平白生成一份草稿。
        """
        current_plan = (state or {}).get("reportPlan")
        phase = (state or {}).get("designPhase") or DESIGN_PHASE_PLAN_NEEDED
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.ANALYZING,
            reply="",
            designSessionId=request.context.designSessionId,
            designPhase=phase,
            domainObjects=AgentResponseDomainObjects(
                reportPlan=current_plan if isinstance(current_plan, dict) else None,
                templateDesign=(state or {}).get("templateDesign"),
            ),
            debug={"designPhase": phase, "designOutcome": "noop"},
        )

    @staticmethod
    def attach_design_state(
        response: AgentResponse, state: dict[str, Any] | None
    ) -> AgentResponse:
        """把设计阶段与方案挂回一个**只读查询**的响应。

        规格：查询前后要保持原 state、designPhase、方案 revision、
        旧菜单和业务对象；查询只允许追加消息。

        所以这里只挂设计状态，不改 state、不改 turnKind、不碰菜单 ——
        菜单由旧查询链路的 ``_preserved_menu`` 负责。
        """
        if not state:
            return response
        phase = state.get("designPhase")
        if phase:
            response.designPhase = phase
        plan = state.get("reportPlan")
        if isinstance(plan, dict):
            response.domainObjects.reportPlan = plan
        template = state.get("templateDesign")
        if isinstance(template, dict):
            response.domainObjects.templateDesign = template
        response.debug = {
            **(response.debug or {}),
            "designPhase": phase,
            "designQuery": True,
        }
        return response

    # ---------- 编排 ----------

    async def handle(
        self,
        request: AgentRequest,
        *,
        emit: Any | None = None,
        state: dict[str, Any] | None = None,
    ) -> AgentResponse | None:
        """处理一轮设计请求。

        返回 None 表示「这一轮不归设计流程管」，调用方继续走旧流程。

        **提问与修改的区分由调用方决定**（见 agent_service）：
        提问要走旧查询链路，不能在这里变成一次方案生成。
        """
        if state is None:
            state = self.load_state(request)
        if state is None and not self.is_new_build(request):
            return None
        if request.message.strip() == "__advance__":
            # 内部控制消息不是用户需求。设计流程不走旧的自动推进。
            return None

        action = (request.context.designAction or "").strip().upper()
        current_plan = (state or {}).get("reportPlan")

        if (state or {}).get("designPhase") == DESIGN_PHASE_TEMPLATE_READY:
            if _normalize(request.message) in {"修改", "修改方案", "修改模板", "改方案", "调整方案"}:
                response = self._template_response(
                    request, plan=current_plan, template=(state or {}).get("templateDesign")
                )
                response.reply = (
                    "请说明要修改方案的哪些内容。当前方案保持原样，"
                    "修改后的方案重新确认后直接生成草稿。"
                )
                response.uiActions = ["modify_plan", "plan_edit_requested"]
                return response
            if is_vague_approval(request.message):
                return self._handle_vague_approval(request, state=state)

        if action == DESIGN_ACTION_CONFIRM_TEMPLATE or (
            (state or {}).get("designPhase") == DESIGN_PHASE_TEMPLATE_READY
            and not action and is_pure_confirmation(request.message)
        ):
            if action and request.message.strip() and not is_pure_confirmation(request.message):
                return await self._handle_design_turn(request, state=state, emit=emit)
            return self.confirm_template(request, state=state)

        # 模糊认可先判：「差不多了」这类不能确定是否认可，
        # 不能推进设计版本，只能返回明确确认入口（规格 #18）。
        if isinstance(current_plan, dict) and current_plan:
            if is_vague_approval(request.message):
                return self._handle_vague_approval(request, state=state)

        if action == DESIGN_ACTION_CONFIRM_PLAN:
            # 按钮确认。消息里还夹着修改要求时不能只确认旧版本 ——
            # 规格写明「一次消息同时提出修改和确认时，先展示更新后的方案
            # 并等待对该版本的确认」。
            if is_pure_confirmation(request.message):
                return self._handle_confirm_action(request, state=state)
            return await self._handle_design_turn(
                request, state=state, emit=emit
            )

        # 自然语言确认：服务端识别，绑定服务端当前方案版本（规格 #18）。
        if is_pure_confirmation(request.message):
            return self._handle_confirm_action(request, state=state)

        return await self._handle_design_turn(request, state=state, emit=emit)

    def confirm_plan(
        self, request: AgentRequest, *, state: dict[str, Any] | None
    ) -> AgentResponse:
        """「确认方案」入口：确认当前这一版方案，直接产出报表草稿。

        按钮确认与自然语言确认走**同一条**路径（``_handle_confirm_action``），
        所以两者绑定的方案版本一致、幂等行为一致。

        这个入口在草稿阶段仍然可用：重复点「确认方案」会复用同一份草稿，
        既不产生第二份，也不推高版本号。
        """
        return self._handle_confirm_action(request, state=state)

    def confirm_template(
        self, request: AgentRequest, *, state: dict[str, Any] | None,
    ) -> AgentResponse:
        template = (state or {}).get("templateDesign")
        plan = (state or {}).get("reportPlan")
        phase = (state or {}).get("designPhase")
        code = None
        if not template or not plan:
            code = "TEMPLATE_NOT_AVAILABLE"
        elif (phase not in {DESIGN_PHASE_TEMPLATE_READY, DESIGN_PHASE_DRAFT_READY}
              or template["planId"] != plan["id"] or template["planRevision"] != plan["revision"]
              or request.context.templateId not in (None, template["id"])
              or request.context.baseTemplateRevision not in (None, template["revision"])
              or request.context.planId not in (None, plan["id"])
              or request.context.basePlanRevision not in (None, plan["revision"])):
            code = "TEMPLATE_REVISION_CONFLICT"
        if code:
            response = self._failure_response(request, state, code=code,
                                              message="当前模板不可确认或版本已变化，请查看当前模板后重试。")
            if phase == DESIGN_PHASE_DRAFT_READY:
                response.state = request.state
            return response
        if not request.messageId:
            return self._failure_response(request, state, code="TEMPLATE_OPERATION_KEY_MISSING",
                                          message="这次确认缺少消息标识，请重试。")
        if phase == DESIGN_PHASE_DRAFT_READY:
            # 这一版方案已经确认过：复用**当前**那份草稿，不重复生成。
            # 取的是会话快照里那份 —— 用户可能已经改过，回一份旧草稿
            # 会把已接受的修改覆盖回去。
            record = session_service.repository.get_session(self.session_key(request))
            pending = record.pendingChoice if record else None
            response = self._draft_response(
                request,
                plan=plan,
                template=template,
                draft=self._current_draft(self.session_key(request)),
                duplicate=True,
            )
            response.state = (
                AgentState(record.state) if record else request.state
            )
            response.designSessionId = (
                record.designSessionId if record else request.context.designSessionId
            )
            if pending:
                try:
                    response.questions = [AgentQuestion(**pending["question"])]
                except Exception:  # noqa: BLE001
                    logger.warning("[report_design] 待确认菜单结构异常，按默认菜单返回")
            return response
        operation_id = None
        try:
            claim = self.repository.claim_operation(self.session_key(request), request.messageId, plan["revision"])
            if claim["kind"] == CLAIM_KIND_CONFLICT:
                return self._conflict_response(request, claim)
            if claim["kind"] == CLAIM_KIND_DUPLICATE:
                response = self._failure_response(request, state, code="TEMPLATE_OPERATION_DUPLICATE",
                                                  message="这条消息已经处理过或仍在处理中，没有重复执行。请使用新的消息标识重试。")
                return response
            operation_id = claim["operationId"]
            draft = build_template_definition(template, plan, request.context.subjectId)
            title = template.get("title") or plan.get("topic") or "报表"
            plan_revision = template.get("planRevision") or plan.get("revision")
            response = AgentResponse(
                conversationId=request.conversationId, state=AgentState.WAITING_USER_CONFIRM,
                designSessionId=request.context.designSessionId,
                designPhase=DESIGN_PHASE_DRAFT_READY,
                reply=(
                    f"已按第 {plan_revision} 版方案「{title}」生成报表草稿。"
                    "草稿已生成，请在右侧选择下一步。"
                    "请确认草稿后继续校验、试运行；占位区域仍保留原因，不会编造数值。"
                ),
                domainObjects=AgentResponseDomainObjects(
                    reportDefinitionDraft=draft, reportPolicyDraft=build_policy_draft(template["title"]),
                    reportPlan=plan, templateDesign=template,
                ),
                questions=[AgentQuestion(
                    id="template_edit_confirm", prompt="请选择下一步操作：", expects="SINGLE_CHOICE",
                    selectionOnly=True, options=[{"value": v, "label": v} for v in ("同意", "修改", "重新开始")],
                )],
                uiActions=["confirm_framework", "draft_ready"],
            )
            write_turn = self._make_write_turn(request, response)

            def write_confirmation(connection):
                write_turn(connection)
                connection.execute(
                    "UPDATE sessions SET pending_choice = ? WHERE id = ?",
                    (json.dumps({"state": response.state.value,
                                 "question": response.questions[0].model_dump(mode="json")}, ensure_ascii=False),
                     self.session_key(request)),
                )

            commit = self.repository.commit_definition(
                self.session_key(request), template["id"], template["revision"], draft.model_dump(mode="json"),
                write_confirmation, operation_id=operation_id,
            )
            if commit.get("alreadyCommitted"):
                return self.confirm_template(request, state=self.load_state(request))
            response.turnPersisted = True
            return response
        except (DesignTemplateConflictError, DesignTemplateNotFoundError):
            if operation_id:
                self._fail_operation(operation_id, "TEMPLATE_REVISION_CONFLICT", "模板已变化")
            return self._failure_response(request, self.load_state(request), code="TEMPLATE_REVISION_CONFLICT",
                                          message="模板已变化，本次没有覆盖它，请刷新后重试。")
        except Exception as exc:  # noqa: BLE001
            logger.exception("[report_design] 模板确认失败")
            if operation_id:
                self._fail_operation(operation_id, "TEMPLATE_CONFIRM_FAILED", _reason_text(exc))
            # 失败原因**原样**透传：下游已经写清了「哪个板块出了什么事」，
            # 这里再概括成一句通用文案就把可定位的信息丢掉了。
            return self._failure_response(request, state, code="TEMPLATE_CONFIRM_FAILED",
                                          message=(
                                              f"模板未能安全转换为草稿：{_reason_text(exc)}"
                                              "当前模板保持原样，请修改方案或重试。"
                                          ))

    def _handle_vague_approval(
        self,
        request: AgentRequest,
        *,
        state: dict[str, Any] | None,
    ) -> AgentResponse:
        """模糊认可：不推进版本，返回明确确认入口。

        规格 #18：「「差不多了」等不能确定是否认可的表达不直接推进，
        返回明确确认入口。」工单 01 验收也写明「不把最初需求或模糊认可
        当作确认」。

        所以这里既不确认，也不生成新方案 —— 方案与版本原样保留，
        只把「确认方案」这个入口摆在用户面前。
        """
        current_plan = (state or {}).get("reportPlan") or {}
        current_revision = current_plan.get("revision")
        if (state or {}).get("designPhase") == DESIGN_PHASE_TEMPLATE_READY:
            response = self._template_response(request, plan=current_plan, template=(state or {}).get("templateDesign"))
            response.reply = (
                "当前方案保持原样。发送「确认」即可按当前方案直接生成草稿；"
                "要调整请说明要修改方案的哪一处。"
            )
            return response
        message = (
            "我理解你对这份方案大致认可，但还不能确定是不是要按它生成，"
            "所以我没有改动方案，也没有替你确认。"
            f"当前是第 {current_revision} 版，请点「确认方案」明确确认，"
            "或者告诉我要改哪里。"
        )
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.ANALYZING,
            reply=message,
            designSessionId=request.context.designSessionId,
            designPhase=(state or {}).get("designPhase")
            or DESIGN_PHASE_PLAN_WAITING_CONFIRM,
            domainObjects=AgentResponseDomainObjects(reportPlan=current_plan),
            uiActions=[UI_ACTION_PLAN_CONFIRM_PENDING],
            debug={
                "designOutcome": "vague_approval_needs_explicit_confirm",
                "planRevision": current_revision,
                "planId": current_plan.get("id"),
            },
        )

    def _handle_confirm_action(
        self,
        request: AgentRequest,
        *,
        state: dict[str, Any] | None,
    ) -> AgentResponse:
        """确认当前文字方案 → 生成模板并直接落库报表草稿。

        这一步做的是：

            - 校验动作确实指向当前这一版方案（版本过期要可辨识地拒绝）；
            - **生成模板**：按已确认的方案逐板块出可视区域，
              区域与方案板块一一对应；
            - **逐区域接数**：当前已接通能力算得出来的区域填数据，
              算不出来的保留占位并写明原因（数据待连接 / 口径待核实）；
            - **转换成报表草稿**：模板转成可确认的报表草稿；
            - **一次事务落库**：模板、草稿、确认关系、阶段
              （``DRAFT_READY``）、下一步菜单与本轮消息同生共死。

        对外成功落点是「草稿就绪 + 等待用户确认」：用户不需要再确认一次
        模板，也不需要在聊天里额外输入一句「确认模板」。

        用户口径（需求访谈 Q13）：「数据库里有啥数据就展示啥，没有的
        就按 Q11 和 Q12 的方案来」—— 所以这里**不能**一律出占位，
        更不能把流程断在这里。

        明确**不**接旧的「确认报表」链路：确认方案不校验、不预览、
        不改旧草稿的保存与运行推进。设计确认与确认报表是两件事
        （规格补充说明 2）。
        """
        current_plan = (state or {}).get("reportPlan")
        if not isinstance(current_plan, dict) or not current_plan:
            message = "当前还没有可确认的文字方案，请先描述你的报表需求。"
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.ANALYZING,
                reply=message,
                designSessionId=request.context.designSessionId,
                designPhase=(state or {}).get("designPhase") or DESIGN_PHASE_PLAN_NEEDED,
                error=AgentError(code="PLAN_NOT_AVAILABLE", message=message),
                debug={"designOutcome": "confirm_without_plan"},
            )

        current_revision = current_plan.get("revision")
        current_plan_id = current_plan.get("id")
        base_revision = request.context.basePlanRevision
        base_plan_id = request.context.planId

        # 版本与方案 id 都要核对：按钮明确传递了「我确认的是哪一份、哪一版」，
        # 对不上就说明用户看到的是旧的一份（ADR 0002 / 规格 #18「过期生成不能覆盖新版本」）。
        stale = (
            base_revision is not None and base_revision != current_revision
        ) or (
            base_plan_id is not None and base_plan_id != current_plan_id
        )
        if stale:
            message = (
                f"这份方案已经更新到第 {current_revision} 版，"
                f"你确认的是第 {base_revision} 版。请看过当前版本再确认。"
            )
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.ANALYZING,
                reply=message,
                designSessionId=request.context.designSessionId,
                designPhase=(state or {}).get("designPhase") or DESIGN_PHASE_PLAN_WAITING_CONFIRM,
                domainObjects=AgentResponseDomainObjects(reportPlan=current_plan),
                error=AgentError(code="PLAN_REVISION_CONFLICT", message=message),
                debug={
                    "designOutcome": "confirm_conflict",
                    "currentRevision": current_revision,
                },
            )

        session_key = self.session_key(request)

        # 这一版方案已经确认过：**不重复生成**，复用同一份草稿。
        # 重复点确认按钮、同一条消息重发，都走这里。
        existing = self._existing_confirmation(
            session_key, str(current_plan_id or ""), current_revision
        )
        if existing is not None:
            response = self._draft_response(
                request,
                plan=current_plan,
                template=existing,
                draft=self._current_draft(session_key),
                duplicate=True,
            )
            response.state = (
                self._current_session_state(session_key) or response.state
            )
            return response

        message_id = request.messageId
        if not message_id:
            # 没有幂等键就没法认领操作。方案与阶段原样保留，
            # 这一轮按失败收尾，用户可以重试。
            return self._failure_response(
                request,
                state,
                code="PLAN_OPERATION_KEY_MISSING",
                message="这次确认缺少消息标识，没法安全地记录这次操作。方案保持原样，请重试。",
            )

        # ---- 认领（事务外） ----
        try:
            claim = self.repository.claim_operation(
                session_key, message_id, current_revision
            )
        except Exception:  # noqa: BLE001
            logger.exception("[report_design] 认领确认操作失败")
            return self._failure_response(
                request,
                state,
                code="PLAN_CLAIM_FAILED",
                message="这次确认没能开始，方案保持原样，请重试。",
            )

        if claim["kind"] == CLAIM_KIND_CONFLICT:
            return self._conflict_response(request, claim)

        if claim["kind"] == CLAIM_KIND_DUPLICATE:
            # 同一条消息重复到达：处理中就如实说处理中；
            # 已经成功就复用已生成的草稿，不重复生成。
            if claim.get("status") == OPERATION_SUCCEEDED:
                existing = self._existing_confirmation(
                    session_key, str(current_plan_id or ""), current_revision
                )
                if existing is not None:
                    response = self._draft_response(
                        request,
                        plan=current_plan,
                        template=existing,
                        draft=self._current_draft(session_key),
                        duplicate=True,
                    )
                    response.state = (
                        self._current_session_state(session_key) or response.state
                    )
                    return response
            return self._duplicate_response(request, claim)

        operation_id = claim["operationId"]
        assert operation_id is not None

        # ---- 方案契约检查（事务外，生成草稿前的最后一道关） ----
        contract_error = _plan_contract_error(current_plan.get("sections"))
        if contract_error:
            logger.info("[report_design] 方案组件类型非法：%s", contract_error)
            self._fail_operation(
                operation_id, "PLAN_COMPONENT_TYPE_INVALID", contract_error
            )
            return self._failure_response(
                request,
                state,
                code="PLAN_COMPONENT_TYPE_INVALID",
                message=f"{contract_error}方案保持原样，请修改后重试。",
                retryable=True,
            )

        # ---- 生成模板（事务外） ----
        try:
            template = build_template_design(
                current_plan,
                subject_id=request.context.subjectId,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[report_design] 生成模板失败")
            self._fail_operation(operation_id, "TEMPLATE_BUILD_FAILED", str(exc))
            return self._failure_response(
                request,
                state,
                code="TEMPLATE_BUILD_FAILED",
                message=(
                    f"这次没能生成模板：{_reason_text(exc)}"
                    "方案保持原样，请修改后重试。"
                ),
                retryable=True,
            )

        template_data = template.model_dump(mode="json")

        # ---- 转换成报表草稿（事务外） ----
        #
        # 转换也在事务外：它要读数据源、算数值，不能占着写事务。
        # 转换失败同样在提交之前就失败 —— 确认关系、草稿、菜单一个都
        # 不会落库，不会留下「已确认成功但没有草稿」的半截状态。
        try:
            draft = build_template_definition(
                template_data, current_plan, request.context.subjectId
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[report_design] 模板转草稿失败")
            self._fail_operation(operation_id, "TEMPLATE_BUILD_FAILED", str(exc))
            return self._failure_response(
                request,
                state,
                code="TEMPLATE_BUILD_FAILED",
                message=(
                    f"这次没能把方案转换成报表草稿：{_reason_text(exc)}"
                    "方案保持原样，请修改后重试。"
                ),
                retryable=True,
            )

        draft_data = draft.model_dump(mode="json")
        response = self._draft_response(
            request,
            plan=current_plan,
            template=template_data,
            draft=draft,
        )
        write_turn = self._make_write_turn(request, response)

        def write_confirmation(connection):
            # 顺序不能反：write_turn_inline 对新会话是 INSERT，
            # 菜单的 UPDATE 必须在会话行建好之后。
            write_turn(connection)
            connection.execute(
                "UPDATE sessions SET pending_choice = ? WHERE id = ?",
                (json.dumps({"state": response.state.value,
                             "question": response.questions[0].model_dump(mode="json")},
                            ensure_ascii=False),
                 session_key),
            )

        # ---- 提交（模板 + 草稿 + 确认关系 + 阶段 + 菜单 + 本轮消息，同一事务） ----
        try:
            commit = self.repository.commit_template_and_definition(
                operation_id,
                current_revision,
                template_data,
                draft_data,
                write_confirmation,
            )
        except (DesignPlanInvalidError, DesignOperationStateError) as exc:
            logger.warning("[report_design] 提交草稿被拒：%s", exc)
            self._fail_operation(operation_id, "TEMPLATE_COMMIT_REJECTED", str(exc))
            return self._failure_response(
                request,
                state,
                code="TEMPLATE_COMMIT_REJECTED",
                message=(
                    f"这次草稿没能保存下来：{_reason_text(exc)}"
                    "方案保持原样，请重试。"
                ),
                retryable=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[report_design] 提交草稿失败")
            # 提交异常回滚后，操作会停在 PROCESSING：必须显式失败收尾，
            # 否则这条消息会一直显示「处理中」，用户也拿不到重试入口。
            self._fail_operation(operation_id, "TEMPLATE_COMMIT_FAILED", str(exc))
            return self._failure_response(
                request,
                state,
                code="TEMPLATE_COMMIT_FAILED",
                message=(
                    f"这次草稿没能保存下来：{_reason_text(exc)}"
                    "方案保持原样，请重试。"
                ),
                retryable=True,
            )

        if commit["kind"] == COMMIT_KIND_CONFLICT:
            return self._conflict_response(request, commit)

        if commit.get("alreadyCommitted"):
            # 别的进程已经确认过同一版方案：复用**当前**那份草稿
            # （用户可能已经改过），不重复写消息，让路由层按普通轮落库。
            response = self._draft_response(
                request,
                plan=current_plan,
                template=commit.get("template") or template_data,
                draft=self._current_draft(session_key) or commit.get("definitionDraft"),
                duplicate=True,
            )
            response.state = (
                self._current_session_state(session_key) or response.state
            )
            return response

        # 只有真正共同提交成功，才把这一轮标成「已落库」。
        response.turnPersisted = True
        return response

    def _existing_confirmation(
        self,
        session_key: str,
        plan_id: str,
        plan_revision: int | None,
    ) -> dict[str, Any] | None:
        """这一版方案确认过没有；确认过就返回已提交的模板。"""
        if not plan_id or plan_revision is None:
            return None
        try:
            confirmation = self.repository.find_confirmation(
                session_key, plan_id, int(plan_revision)
            )
        except Exception:  # noqa: BLE001
            logger.exception("[report_design] 查确认关系失败")
            return None
        if not confirmation:
            return None
        try:
            return self.repository.load_template(session_key)
        except Exception:  # noqa: BLE001
            logger.exception("[report_design] 读已提交模板失败")
            return None

    def _current_session_state(self, session_key: str) -> AgentState | None:
        """会话当前的状态。

        重复确认要**沿用**当前状态：用户可能已经点过「同意」，状态已经
        走到 ``SAVE_DEFINITION_DRAFT``；这时回一句「等待确认」就是在把
        状态倒回去 —— 旧确认结果的重放不许覆盖后续已接受的状态。
        """
        try:
            record = session_service.repository.get_session(session_key)
        except Exception:  # noqa: BLE001
            logger.exception("[report_design] 读会话失败")
            return None
        if record is None:
            return None
        try:
            return AgentState(record.state)
        except ValueError:
            return None

    def _current_draft(self, session_key: str) -> ReportDefinitionDraft | None:
        """读会话里**当前**那份草稿。

        重复确认要复用同一份草稿，而且必须是**当前**那份：用户可能
        已经在草稿上改过，回一份旧草稿会把已接受的修改覆盖回去。
        """
        try:
            record = session_service.repository.get_session(session_key)
        except Exception:  # noqa: BLE001
            logger.exception("[report_design] 读会话失败")
            return None
        snapshot = (record.snapshot if record else None) or {}
        raw = snapshot.get("definition")
        if not isinstance(raw, dict):
            # 会话快照里没有（会话行被重建过）就退回设计存储里那份初版
            # 草稿：宁可回初版，也不要把「没有草稿」当成结果。
            try:
                stored = self.repository.load(session_key) or {}
            except Exception:  # noqa: BLE001
                logger.exception("[report_design] 读设计存储失败")
                return None
            raw = stored.get("definitionDraft")
        if not isinstance(raw, dict):
            return None
        try:
            return ReportDefinitionDraft(**raw)
        except Exception:  # noqa: BLE001
            logger.exception("[report_design] 读当前草稿失败")
            return None

    async def _handle_design_turn(
        self,
        request: AgentRequest,
        *,
        state: dict[str, Any] | None,
        emit: Any | None = None,
    ) -> AgentResponse:
        """一轮真正的设计请求：认领 → 模型 → 校验 → 提交。"""
        session_key = self.session_key(request)
        message_id = request.messageId
        if not message_id:
            # 没有幂等键就没法认领操作。这一轮按失败收尾，
            # 上一成功方案原样保留。
            return self._failure_response(
                request,
                state,
                code="PLAN_OPERATION_KEY_MISSING",
                message="这次请求缺少消息标识，无法安全地关联设计操作。方案保持原样，请重试。",
            )

        current_plan = (state or {}).get("reportPlan")
        expected_revision = (
            current_plan.get("revision") if isinstance(current_plan, dict) else None
        )

        # ---- 认领（事务外，模型工作前后都不在事务里） ----
        try:
            claim = self.repository.claim_operation(
                session_key, message_id, expected_revision
            )
        except Exception:  # noqa: BLE001
            logger.exception("[report_design] 认领操作失败")
            return self._failure_response(
                request,
                state,
                code="PLAN_CLAIM_FAILED",
                message="这次设计操作没能开始，方案保持原样，请重试。",
            )

        if claim["kind"] == CLAIM_KIND_CONFLICT:
            return self._conflict_response(request, claim)

        if claim["kind"] == CLAIM_KIND_DUPLICATE:
            return self._duplicate_response(request, claim)

        operation_id = claim["operationId"]
        assert operation_id is not None

        # ---- 模型工作（事务外） ----
        result = await generate_candidate(
            message=request.message,
            current_plan=current_plan,
            history=self._history(request),
            model_call=self.model_call,
            emit=self._plan_emit(emit),
        )

        kind = result.get("kind")

        if kind == KIND_FAILURE:
            # 技术失败：显式失败收尾，保留上一成功方案。
            code = str(result.get("code") or "PLAN_MODEL_FAILED")
            message = str(result.get("message") or "方案生成失败，请重试。")
            self._fail_operation(operation_id, code, message)
            return self._failure_response(
                request,
                state,
                code=code,
                message=message,
                retryable=True,
            )

        if kind == KIND_CLARIFICATION:
            return await self._commit_clarification(
                request,
                operation_id=operation_id,
                expected_revision=expected_revision,
                question=str(result.get("question") or "").strip(),
                state=state,
            )

        if kind != KIND_PLAN_CANDIDATE:
            self._fail_operation(operation_id, "PLAN_OUTPUT_INVALID", "方案结果无法识别")
            return self._failure_response(
                request,
                state,
                code="PLAN_OUTPUT_INVALID",
                message="这次没能得到可用的方案，方案保持原样，请重试。",
                retryable=True,
            )

        # ---- 领域校验（A） ----
        candidate = result.get("candidate")
        try:
            plan = apply_candidate(
                candidate,
                previous=current_plan,
                plan_id=(
                    current_plan.get("id")
                    if isinstance(current_plan, dict)
                    else f"{PLAN_ID_PREFIX}{uuid.uuid4().hex[:12]}"
                ),
                expected_revision=expected_revision,
            )
        except ReportPlanError as exc:
            code = getattr(exc, "code", "PLAN_INVALID_STRUCTURE")
            logger.info("[report_design] 候选未通过领域校验 code=%s", code)
            # 失败原因要**原样**透传给用户，不能一律换成「请重试」。
            #
            # 方案契约层（模型适配层 / 领域层）已经把「哪个板块、哪个
            # 字段」写进异常消息里了 —— 这里**原样送出**，不做二次概括：
            # 再概括一次会把板块名丢掉，用户就没法定位要改哪一块。
            reason = str(exc).strip()
            self._fail_operation(operation_id, code, reason or "方案未通过领域校验")
            message = (
                f"这次生成的方案没有通过校验：{reason}方案保持原样，请修改后重试。"
                if reason
                else "这次生成的方案没有通过校验（模型输出里有不该进方案的内容），"
                "方案保持原样，请修改后重试。"
            )
            return self._failure_response(
                request,
                state,
                code=code,
                message=message,
                retryable=True,
            )

        # ---- 提交（C，同事务） ----
        return self._commit_plan(
            request,
            operation_id=operation_id,
            expected_revision=expected_revision,
            plan=plan,
            state=state,
        )

    # ---------- 提交 ----------

    def _commit_plan(
        self,
        request: AgentRequest,
        *,
        operation_id: str,
        expected_revision: int | None,
        plan: ReportPlan,
        state: dict[str, Any] | None,
    ) -> AgentResponse:
        plan_data = plan.model_dump(mode="json")
        response = self._plan_response(
            request,
            plan=plan_data,
            phase=DESIGN_PHASE_PLAN_WAITING_CONFIRM,
            reply=self._plan_reply(plan_data),
        )
        write_turn = self._make_write_turn(request, response)

        try:
            commit = self.repository.commit_plan(
                operation_id,
                expected_revision,
                plan_data,
                write_turn,
            )
        except (DesignPlanInvalidError, DesignOperationStateError) as exc:
            logger.warning("[report_design] 提交方案被拒：%s", exc)
            self._fail_operation(operation_id, "PLAN_COMMIT_REJECTED", str(exc))
            return self._failure_response(
                request,
                state,
                code="PLAN_COMMIT_REJECTED",
                message="这次方案没能保存下来，方案保持原样，请重试。",
                retryable=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("[report_design] 提交方案失败")
            # 提交异常回滚后，操作可能停在 PROCESSING：必须显式失败收尾，
            # 否则这条消息会一直显示「处理中」，用户也拿不到可理解的重试入口。
            self._fail_operation(operation_id, "PLAN_COMMIT_FAILED", "提交方案失败")
            return self._failure_response(
                request,
                state,
                code="PLAN_COMMIT_FAILED",
                message="这次方案没能保存下来，方案保持原样，请重试。",
                retryable=True,
            )

        if commit["kind"] == COMMIT_KIND_CONFLICT:
            return self._conflict_response(request, commit)

        if commit.get("alreadyCommitted"):
            # 同一操作已经提交过：不重复写消息，原样返回当前状态。
            committed_plan = commit.get("plan") or plan_data
            return self._plan_response(
                request,
                plan=committed_plan,
                phase=commit.get("designPhase") or DESIGN_PHASE_PLAN_WAITING_CONFIRM,
                reply=self._plan_reply(committed_plan),
                duplicate=True,
            )

        # 只有真正共同提交成功，才把这一轮标成「已落库」。
        # 路由层看到它就不会再落一遍。
        response.turnPersisted = True
        return response

    async def _commit_clarification(
        self,
        request: AgentRequest,
        *,
        operation_id: str,
        expected_revision: int | None,
        question: str,
        state: dict[str, Any] | None,
    ) -> AgentResponse:
        """提交一次必要追问。

        追问不是技术失败：阶段不动，上一成功方案保留，
        这一轮的消息与操作状态同事务落库。
        """
        if not question:
            self._fail_operation(operation_id, "PLAN_CLARIFICATION_EMPTY", "澄清内容为空")
            return self._failure_response(
                request,
                state,
                code="PLAN_CLARIFICATION_EMPTY",
                message="这次没能问出需要确认的问题，方案保持原样，请重试。",
                retryable=True,
            )

        phase = (state or {}).get("designPhase") or DESIGN_PHASE_PLAN_NEEDED
        current_plan = (state or {}).get("reportPlan")
        response = AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.NEEDS_CLARIFICATION,
            reply=question,
            designSessionId=request.context.designSessionId,
            designPhase=phase,
            domainObjects=AgentResponseDomainObjects(
                reportPlan=current_plan if isinstance(current_plan, dict) else None
            ),
            uiActions=[UI_ACTION_PLAN_CLARIFICATION],
            debug={"designPhase": phase, "planClarification": True},
        )
        write_turn = self._make_write_turn(request, response)

        try:
            commit = self.repository.commit_clarification(
                operation_id,
                expected_revision,
                question,
                write_turn,
            )
        except Exception:  # noqa: BLE001
            logger.exception("[report_design] 提交澄清失败")
            self._fail_operation(operation_id, "PLAN_CLARIFICATION_FAILED", "提交澄清失败")
            return self._failure_response(
                request,
                state,
                code="PLAN_CLARIFICATION_FAILED",
                message="这次没能把追问记下来，方案保持原样，请重试。",
                retryable=True,
            )

        if commit["kind"] == COMMIT_KIND_CONFLICT:
            return self._conflict_response(request, commit)

        response.turnPersisted = True
        return response

    # ---------- 同事务写入 ----------

    def _make_write_turn(self, request: AgentRequest, response: AgentResponse):
        """构造 ``write_turn(connection)`` 回调。

        回调只能用传入的那条连接直接写 SQL，**不能**调用会自行 commit 的
        旧 Repository 方法（``append_message`` / ``upsert_session`` /
        ``record_turn``）—— 一旦被调用就会提前结束事务，同事务提交就不成立。
        """
        session_key = self.session_key(request)

        def write_turn(connection) -> None:
            session_service.write_turn_inline(
                connection,
                session_id=session_key,
                user_text=request.message,
                response=response,
                client_msg_id=request.messageId,
                request=request,
            )

        return write_turn

    # ---------- 失败与冲突 ----------

    def _fail_operation(self, operation_id: str, code: str, message: str) -> None:
        """显式失败收尾。

        提交异常回滚后，操作会停在 PROCESSING。不显式收尾，
        这条消息就会一直显示「处理中」—— 用户被假忙碌永久卡住。
        """
        try:
            self.repository.fail_operation(
                operation_id, {"code": code, "message": message}
            )
        except Exception:  # noqa: BLE001
            logger.exception("[report_design] 失败收尾没能落库 operation=%s", operation_id)

    def _failure_response(
        self,
        request: AgentRequest,
        state: dict[str, Any] | None,
        *,
        code: str,
        message: str,
        retryable: bool = True,
    ) -> AgentResponse:
        """失败响应：如实说失败，保留上一成功方案。"""
        current_plan = (state or {}).get("reportPlan")
        phase = (state or {}).get("designPhase") or DESIGN_PHASE_PLAN_NEEDED
        ui_actions = [UI_ACTION_PLAN_RETRY] if retryable else []
        return AgentResponse(
            conversationId=request.conversationId,
            state=request.state if phase == DESIGN_PHASE_DRAFT_READY else AgentState.ANALYZING,
            reply=message,
            designSessionId=request.context.designSessionId,
            designPhase=phase,
            domainObjects=AgentResponseDomainObjects(
                reportPlan=current_plan if isinstance(current_plan, dict) else None
            ),
            error=AgentError(code=code, message=message),
            uiActions=ui_actions,
            debug={"designPhase": phase, "designOutcome": "failure", "errorCode": code},
        )

    def _conflict_response(
        self,
        request: AgentRequest,
        claim: dict[str, Any],
    ) -> AgentResponse:
        """版本冲突：说清当前是第几版，不静默覆盖。"""
        current_plan = claim.get("currentPlan")
        current_revision = claim.get("currentRevision")
        message = (
            f"这份方案已经更新到第 {current_revision} 版，"
            "这次的内容是基于旧版本生成的，没有覆盖它。请基于当前版本再说一次要改什么。"
        )
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.ANALYZING,
            reply=message,
            designSessionId=request.context.designSessionId,
            designPhase=claim.get("designPhase") or DESIGN_PHASE_PLAN_NEEDED,
            domainObjects=AgentResponseDomainObjects(
                reportPlan=current_plan if isinstance(current_plan, dict) else None
            ),
            error=AgentError(code="PLAN_REVISION_CONFLICT", message=message),
            uiActions=[UI_ACTION_PLAN_RETRY],
            debug={
                "designPhase": claim.get("designPhase"),
                "designOutcome": "conflict",
                "currentRevision": current_revision,
            },
        )

    def _duplicate_response(
        self,
        request: AgentRequest,
        claim: dict[str, Any],
    ) -> AgentResponse:
        """同一条消息重复到达。

        处理中 → 如实说还在处理；成功 → 原样返回当前方案；
        失败 → 如实说失败并给重试入口。
        """
        status = claim.get("status")
        current_plan = claim.get("currentPlan")
        phase = claim.get("designPhase") or DESIGN_PHASE_PLAN_NEEDED

        if status == "PROCESSING":
            message = "这次设计还在处理中，没有重复执行。请稍后再看。"
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.ANALYZING,
                reply=message,
                designSessionId=request.context.designSessionId,
                designPhase=phase,
                domainObjects=AgentResponseDomainObjects(
                    reportPlan=current_plan if isinstance(current_plan, dict) else None
                ),
                debug={"designPhase": phase, "designOutcome": "duplicate_processing"},
            )

        if status == "FAILED":
            error = claim.get("error") or {}
            message = str(error.get("message") or "上次这次设计没有完成，可以重试。")
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.ANALYZING,
                reply=message,
                designSessionId=request.context.designSessionId,
                designPhase=phase,
                domainObjects=AgentResponseDomainObjects(
                    reportPlan=current_plan if isinstance(current_plan, dict) else None
                ),
                error=AgentError(code=str(error.get("code") or "PLAN_FAILED"), message=message),
                uiActions=[UI_ACTION_PLAN_RETRY],
                debug={"designPhase": phase, "designOutcome": "duplicate_failed"},
            )

        # SUCCEEDED：原样返回当前方案，不重复生成。
        plan = claim.get("plan") or current_plan
        return self._plan_response(
            request,
            plan=plan if isinstance(plan, dict) else None,
            phase=phase,
            reply=self._plan_reply(plan) if isinstance(plan, dict) else "这份方案已经生成过了。",
            duplicate=True,
        )

    # ---------- 响应 ----------

    def _plan_response(
        self,
        request: AgentRequest,
        *,
        plan: dict[str, Any] | None,
        phase: str,
        reply: str,
        duplicate: bool = False,
    ) -> AgentResponse:
        ui_actions = [UI_ACTION_PLAN_READY]
        if duplicate:
            ui_actions.append("plan_duplicate")
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.ANALYZING,
            reply=reply,
            designSessionId=request.context.designSessionId,
            designPhase=phase,
            domainObjects=AgentResponseDomainObjects(reportPlan=plan),
            uiActions=ui_actions,
            debug={
                "designPhase": phase,
                "designOutcome": "plan_ready",
                "planRevision": (plan or {}).get("revision"),
                "planId": (plan or {}).get("id"),
            },
        )

    def _template_response(
        self,
        request: AgentRequest,
        *,
        plan: dict[str, Any] | None,
        template: dict[str, Any] | None,
        duplicate: bool = False,
    ) -> AgentResponse:
        """历史会话（``TEMPLATE_READY``）的响应。

        ``TEMPLATE_READY`` 现在是**纯历史态**：新流程的落点只有
        ``DRAFT_READY``（#36 起「确认方案」一次提交模板 + 草稿），
        只有改造前创建的会话才可能停在这个阶段。

        所以这里的文案与 ``ui_actions`` 都按**草稿就绪**的语义说 ——
        需求稿 §2.4 明令「要求用户输入『确认模板』的提示」要下线：
        既不再要求用户去「确认模板」，也不把模板中间页当成一个要停留
        的落点。下一步只有一条：按这版方案直接生成草稿。
        """
        template = template or {}
        regions = template.get("regions") or []
        bound = [r for r in regions if r.get("dataStatus") == "bound"]
        placeholders = [r for r in regions if r.get("dataStatus") == "placeholder"]
        title = template.get("title") or (plan or {}).get("topic") or "报表"
        plan_revision = template.get("planRevision") or (plan or {}).get("revision")

        reply = f"已确认第 {plan_revision} 版方案「{title}」。"
        if bound:
            # 同 _draft_response：#38 起模板阶段不取数，不能说「已接上数据」。
            reply += f"{len(bound)} 个区域已绑定指标，数值待试运行产生"
            if placeholders:
                reply += f"，{len(placeholders)} 个区域保留占位"
            reply += "。"
        elif placeholders:
            reply += f"{len(placeholders)} 个区域保留占位。"
        notes = template.get("pendingNotes") or []
        if notes:
            # 原因本身已经带句号了，直接拼会变成「。。」。
            cleaned = [
                str(note).rstrip("。；;，,") for note in notes[:4] if str(note).strip()
            ]
            if cleaned:
                reply += "占位原因：" + "；".join(cleaned) + "。"
        if duplicate:
            reply += "（这一版方案之前已经确认过，直接沿用已生成的草稿，没有重复生成。）"
        else:
            reply += "方案已经确认，刷新后仍然在。"
        reply += (
            "发送「确认」即可按这版方案直接生成草稿，"
            "然后在右侧草稿继续修改、校验与试运行。"
        )

        # 不再给「确认模板」这个动作：模板中间页已下线，下一步是草稿。
        ui_actions = [UI_ACTION_TEMPLATE_READY, "draft_ready", "modify_plan"]
        if duplicate:
            ui_actions.append("template_duplicate")
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.ANALYZING,
            reply=reply,
            designSessionId=request.context.designSessionId,
            designPhase=DESIGN_PHASE_TEMPLATE_READY,
            domainObjects=AgentResponseDomainObjects(
                reportPlan=plan,
                templateDesign=template or None,
            ),
            uiActions=ui_actions,
            debug={
                "designPhase": DESIGN_PHASE_TEMPLATE_READY,
                "designOutcome": "template_ready",
                "templateId": template.get("id"),
                "templateRevision": template.get("revision"),
                "planRevision": plan_revision,
                "dataStatus": template.get("dataStatus"),
                "boundRegions": len(bound),
                "placeholderRegions": len(placeholders),
            },
        )

    def _draft_response(
        self,
        request: AgentRequest,
        *,
        plan: dict[str, Any] | None,
        template: dict[str, Any] | None,
        draft: ReportDefinitionDraft | dict[str, Any] | None,
        duplicate: bool = False,
    ) -> AgentResponse:
        """草稿已就绪的响应。

        对外成功落点：**草稿就绪 + 等待用户确认**。模板就绪不再是必须
        展示的中间落点（模板本身仍在 ``domainObjects`` 里，供前端折叠
       对照展示）。

        回复文案带**实际已确认的标题与方案版本号**，不写死示例文本。
        """
        template = template or {}
        regions = template.get("regions") or []
        bound = [r for r in regions if r.get("dataStatus") == "bound"]
        placeholders = [r for r in regions if r.get("dataStatus") == "placeholder"]
        title = template.get("title") or (plan or {}).get("topic") or "报表"
        plan_revision = template.get("planRevision") or (plan or {}).get("revision")

        reply = (
            f"已按第 {plan_revision} 版方案「{title}」生成报表草稿。"
            "草稿已生成，请在右侧选择下一步。"
        )
        if bound:
            # #38 起，模板阶段只做能力核对、**不取数**：bound 的含义是
            # 「指标已绑定、能力可用」，数值一律等试运行产生。
            # 这里不能说「已接上数据」—— 那是不存在的事实。
            reply += f"{len(bound)} 个区域已绑定指标，数值待试运行产生"
            if placeholders:
                reply += f"，{len(placeholders)} 个区域保留占位"
            reply += "。"
        elif placeholders:
            reply += f"{len(placeholders)} 个区域保留占位。"
        notes = template.get("pendingNotes") or []
        if notes:
            # 原因本身已经带句号了，直接拼会变成「。。」。
            cleaned = [
                str(note).rstrip("。；;，,") for note in notes[:4] if str(note).strip()
            ]
            if cleaned:
                reply += "占位原因：" + "；".join(cleaned) + "。"
        if duplicate:
            reply += "（这一版方案之前已经确认过，直接沿用已生成的草稿，没有重复生成。）"

        draft_obj: ReportDefinitionDraft | None
        if isinstance(draft, ReportDefinitionDraft):
            draft_obj = draft
        elif isinstance(draft, dict):
            try:
                draft_obj = ReportDefinitionDraft(**draft)
            except Exception:  # noqa: BLE001
                logger.exception("[report_design] 草稿结构无法还原")
                draft_obj = None
        else:
            draft_obj = None

        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.WAITING_USER_CONFIRM,
            designSessionId=request.context.designSessionId,
            designPhase=DESIGN_PHASE_DRAFT_READY,
            reply=reply,
            domainObjects=AgentResponseDomainObjects(
                reportDefinitionDraft=draft_obj,
                # 策略优先用**服务端**当前那份（重复确认时用户可能已经改过），
                # 没有才按标题生成一份默认的。
                reportPolicyDraft=request.snapshots.reportPolicy or build_policy_draft(title),
                reportPlan=plan,
                templateDesign=template or None,
            ),
            # 下一步菜单复用既有的待确认选项契约，不新造 question id。
            questions=[AgentQuestion(
                id="template_edit_confirm", prompt="请选择下一步操作：", expects="SINGLE_CHOICE",
                selectionOnly=True, options=[{"value": v, "label": v} for v in ("同意", "修改", "重新开始")],
            )],
            uiActions=["confirm_framework", "draft_ready"],
            debug={
                "designPhase": DESIGN_PHASE_DRAFT_READY,
                "designOutcome": "draft_duplicate" if duplicate else "draft_ready",
                "templateId": template.get("id"),
                "templateRevision": template.get("revision"),
                "planRevision": plan_revision,
                "planId": (plan or {}).get("id"),
                "dataStatus": template.get("dataStatus"),
                "boundRegions": len(bound),
                "placeholderRegions": len(placeholders),
            },
        )

    @staticmethod
    def _plan_reply(plan: dict[str, Any] | None) -> str:
        """给用户的简短文字方案说明。

        正文由前端方案模块渲染；这里给一句可读的摘要，
        读屏与「只看文本」的场景也不丢信息。
        """
        if not plan:
            return "已准备好文字方案，请查看右侧方案卡片。"
        topic = plan.get("topic") or "报表"
        sections = plan.get("sections") or []
        titles = "、".join(
            str(section.get("title") or "") for section in sections[:4] if section
        )
        revision = plan.get("revision")
        head = f"已根据你的要求整理出文字方案（第 {revision} 版）：{topic}。"
        if titles:
            head += f"包含板块：{titles}。"
        head += "请在右侧查看完整方案；确认前不会生成模板，也不会取数。"
        return head

    # ---------- 辅助 ----------

    @staticmethod
    def _history(request: AgentRequest) -> list[dict[str, Any]]:
        """给模型的简短上下文。

        只取会话里最近几条文本，不带 debug、不带结构化业务对象 ——
        方案设计只需要知道用户在聊什么。
        """
        session_key = current_session_key() or request.conversationId
        try:
            records = session_service.repository.list_messages(session_key)
        except Exception:  # noqa: BLE001
            logger.exception("[report_design] 读历史消息失败")
            return []
        history: list[dict[str, Any]] = []
        for record in records[-6:]:
            text = (record.content or "").strip()
            if not text or text == "__advance__":
                continue
            history.append({"role": record.role, "text": text})
        return history

    @staticmethod
    def _plan_emit(emit: Any | None):
        """把 B 的进度事件接到 SSE。

        B 的事件名是 ``plan_design_started`` 等，这里转成 SSE 的
        ``stage`` 事件，界面据此显示忙碌态。
        """
        if emit is None:
            return None

        async def forward(event: dict[str, Any]) -> None:
            name = str(event.get("event") or "")
            payload = {
                "stage": name,
                "designPhase": DESIGN_PHASE_PLAN_NEEDED,
            }
            if event.get("code"):
                payload["code"] = event["code"]
            await emit("stage", payload)

        return forward


report_design_service = ReportDesignService()
