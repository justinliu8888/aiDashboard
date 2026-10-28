"""新建流程的「文字方案」编排（#19 协调者接线）。

这一层把三个独立模块串成一条真实链路：

    C 认领操作 → （事务外）B 调模型出候选 → A 领域校验 →
    C 同事务提交方案 + 阶段 + 本轮消息

边界：

    - 不建模型、不复制凭证（模型客户端在 ``report_design_model``）；
    - 不确认、不取数、不调用旧报表 builder（那是 #20 / #05 的范围）；
    - 只允许追加消息；查询轮不改设计对象；
    - 模型工作在事务之外，提交时核对基础版本。

**进入条件**（这是本工单最需要说清的一处判断）：

    1. 会话已经有设计记录 → 本轮在设计流程里；
    2. 或者：请求带了显式 ``sessionId`` + ``messageId``，
       且这个会话还没有落过库行 → 这是**新建**会话的第一轮，
       进入设计流程。

第 2 条不是随手挑的：

    - ``messageId`` 是操作认领的幂等键。C 的 ``claim_operation`` 明确要求
      消息 id，规格也写明「新建请求使用显式 sessionId、messageId 关联
      会话和操作」；没有它就没有可认领的操作。
    - 「会话还没有落过库行」= 这条对话还没成功完成过任何一轮，
      也就是**新建**。已有会话（落过库行、但没进过设计阶段）保持旧协议
      语义，老草稿编辑与恢复不受影响（规格：「对未进入新建阶段的已有会话
      保持原协议语义」）。
"""
from __future__ import annotations

import logging
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
    AgentError,
    AgentRequest,
    AgentResponse,
    AgentResponseDomainObjects,
    AgentState,
)
from app.schemas.report_plan import ReportPlanError
from app.services import routing_rules
from app.services.report_plan_service import apply_candidate
from app.services.session_context import current_session_key
from app.services.session_service import session_service
from app.storage.report_design_repository import (
    CLAIM_KIND_CLAIMED,
    CLAIM_KIND_CONFLICT,
    CLAIM_KIND_DUPLICATE,
    COMMIT_KIND_COMMITTED,
    COMMIT_KIND_CONFLICT,
    DESIGN_PHASE_PLAN_NEEDED,
    DESIGN_PHASE_PLAN_WAITING_CONFIRM,
    DesignOperationStateError,
    DesignPlanInvalidError,
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
            from app.services.report_design_model import ReportDesignModelClient

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
        """这是不是一条**新建**会话的第一轮。

        需要显式 ``sessionId`` + ``messageId``（操作认领的幂等键），
        且这个会话还没有落过库行（= 还没成功完成过任何一轮）。
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
        return existing is None

    def in_design_flow(self, request: AgentRequest) -> bool:
        """本轮是否在设计流程里。"""
        return self.load_state(request) is not None

    @staticmethod
    def is_readonly_question(message: str) -> bool:
        """这句话是不是只读提问。

        设计流程里的提问**不改设计版本**，要走旧查询链路。
        这里用规则层判定，与旧流程降级路径同一口径：
        模型不可用时也能把「问当前对话的事」和「下指令」分开。
        """
        return routing_rules.is_readonly_question(message)

    # ---------- 查询轮挂回设计状态 ----------

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
    ) -> AgentResponse | None:
        """处理一轮设计请求。

        返回 None 表示「这一轮不归设计流程管」，调用方继续走旧流程。
        """
        state = self.load_state(request)
        if state is None and not self.is_new_build(request):
            return None
        if request.message.strip() == "__advance__":
            # 内部控制消息不是用户需求。设计流程不走旧的自动推进。
            return None
        if self.is_readonly_question(request.message):
            # 提问不改设计版本：由调用方交给旧查询链路，
            # 回答完再把设计状态挂回响应。
            return None

        action = (request.context.designAction or "").strip().upper()
        if action == DESIGN_ACTION_CONFIRM_PLAN:
            return self._handle_confirm_action(request, state=state)

        return await self._handle_design_turn(request, state=state, emit=emit)

    def _handle_confirm_action(
        self,
        request: AgentRequest,
        *,
        state: dict[str, Any] | None,
    ) -> AgentResponse:
        """确认当前文字方案。

        **首波边界**：模板生成是工单 02 的范围。所以这里做的是

            - 校验动作确实指向当前这一版方案（版本过期要可辨识地拒绝）；
            - 如实说明「模板生成尚未接入」，**不伪造完成状态**；
            - 方案与阶段原样保留（仍是 PLAN_WAITING_CONFIRM）。

        明确**不**接旧的「确认报表」链路：确认方案不保存草稿、不校验、
        不预览、不取数。设计确认与确认报表是两件事（规格补充说明 2）。
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
        base_revision = request.context.basePlanRevision
        if base_revision is not None and base_revision != current_revision:
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

        message = (
            f"已收到你对第 {current_revision} 版方案的确认。"
            "模板生成还没有接入（这一步由工单 02 交付），"
            "所以现在只保留方案、继续等待，"
            "不会生成模板，也不会保存草稿或取数。"
        )
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.ANALYZING,
            reply=message,
            designSessionId=request.context.designSessionId,
            designPhase=(state or {}).get("designPhase") or DESIGN_PHASE_PLAN_WAITING_CONFIRM,
            domainObjects=AgentResponseDomainObjects(reportPlan=current_plan),
            uiActions=[UI_ACTION_PLAN_CONFIRM_PENDING],
            debug={
                "designOutcome": "confirm_pending_template",
                "planRevision": current_revision,
                "planId": current_plan.get("id"),
            },
        )

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
            self._fail_operation(operation_id, code, "方案未通过领域校验")
            return self._failure_response(
                request,
                state,
                code=code,
                message="这次生成的方案没有通过校验（模型输出里有不该进方案的内容），方案保持原样，请重试。",
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
            state=AgentState.ANALYZING,
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
