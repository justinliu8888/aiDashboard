"""Agent 服务层：ContextEngine 全局路由 + 调用业务 Agent + 协议错误包装。

支持两种 Agent 实现（通过 AGENT_IMPL 环境变量切换）：
    - fake:        FakeReportAgent（默认，关键词规则）
    - agentscope:  AgentScopeReportAgent（火山方舟 LLM，Q4 阶段只处理样例 1）

所有用户消息先经过 ContextEngine.route()：
    - 判断是否打断当前流程；
    - 判断是否需要二次确认；
    - 判断当前 state 所需信息是否足够；
    - 决定是原地回答、追问、跳状态，还是交给业务 Agent。
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from time import perf_counter
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from app.agents.fake_report_agent import FakeReportAgent
from app.config import get_settings
from app.schemas.agent import (
    ADVANCE_MESSAGE,
    ALLOWED_TRANSITIONS,
    AGENT_PROTOCOL_VERSION,
    DESIGN_ACTION_CONFIRM_PLAN,
    DESIGN_ACTION_CONFIRM_TEMPLATE,
    LOCK_SCOPE_INPUT_AND_MENU,
    LOCK_SCOPE_MENU_ONLY,
    LOCK_SCOPE_NONE,
    AgentBlock,
    AgentError,
    AgentQuestion,
    AgentRequest,
    AgentResponse,
    AgentResponseDomainObjects,
    AgentRequestSnapshots,
    AgentState,
    QuestionOption,
    ToolRequest,
    TURN_KIND_QUERY_ANSWER,
)
from app.schemas.edit_plan import EditIntent, EditPlan
from app.schemas.fact import RECORDED_STAGE_DISCLOSURE, SessionFacts
from app.schemas.report_definition import ReportDefinitionDraft
from app.schemas.report_policy import ReportPolicyDraft
from app.storage.report_design_repository import DESIGN_PHASE_DRAFT_READY
from app.schemas.session import MAX_CLARIFICATION_ROUNDS, PendingEditPlan
from app.services import routing_rules
from app.services.context_engine import (
    RouteDecision,
    RoutingAction,
    UserIntent,
    classify_message_intent,
    context_engine,
)
from app.services.definition_save_service import definition_save_service
from app.services.preview_service import preview_service
# 边界口径改用共用层（app.schemas.fact），不再从 recorded_progress_service
# 转述模型自己写的 limitations —— 那仍取决于模型有没有声明边界。
from app.services.report_design_service import (
    is_pure_confirmation,
    is_vague_approval,
    placeholder_reply,
    report_design_service,
)
from app.services.report_issue_analysis import IssueAnalysis, analyze_region
# 判定与兜底策略放在 query_answer_service（回答文本策略的家）。
from app.services.query_answer_service import (
    claims_live_progress,
    recorded_stage_disclosed,
    recorded_stage_fallback,
)
from app.services.request_timeline import log_timeline_step
from app.services.session_context import bind_session_key, current_session_key
from app.services.session_fact_service import session_fact_service
from app.services.session_service import session_service
from app.services.edit_reference_resolver import resolve_visible_reference
from app.services.edit_time_resolver import extract_change_value, resolve_time_expression
from app.services.metric_mock_service import list_metrics
from app.services.scope_service import filter_visible
from app.services.template_edit_engine import EditResult, template_edit_engine
from app.tools.context import ToolScope
from app.services.template_preview_flow import template_preview_flow
from app.services.validation_service import summarize_issues_for_user, validate_definition


# 等待用户确认阶段的三个按钮：同意 / 修改 / 重新开始
CONFIRM_BUTTON_VALUE = "同意"
MODIFY_BUTTON_VALUE = "修改"
RESTART_BUTTON_VALUE = "重新开始"
EDIT_ACCEPT = "接受修改"
EDIT_ADJUST = "继续调整"
EDIT_CANCEL = "取消"
EDIT_CHOICE_ID = "pending_edit_plan_confirm"

# 可能触发模板编辑意图识别的粗关键词；仅用于判断“是否进入 TemplateEditEngine”，
# 真正的意图拆解由 TemplateEditEngine.parse() 负责。
#
# 工单 #58：**不收裸时间词**。卡片名里带「昨日」「最近 7 天」很常见
# （「昨日能耗趋势」「最近 7 天总耗电」），收进来会让整句因为出现一个
# 时间词就被送进编辑快路 —— 那正是「一个点」误判的入口之一。
TEMPLATE_EDIT_HINT_WORDS = (
    "删", "删除", "去掉", "不要", "移除",
    "改成", "改为", "重命名", "叫做", "标题", "名字",
    "加一个", "新增", "增加", "补充",
    "数据源", "数据能力",
    "柱状图", "折线图", "饼图", "面积图", "表格", "明细表",
    "粒度", "分钟", "小时", "按天",
    "指标", "能耗成本", "pue",
    "前 ", "降序", "升序", "排序",
    "怎么展示", "展示方式", "展示形式", "可以展示",
    "section", "区域", "面板", "卡片", "模块",
)

# 只读能力查询的疑问词。命中之后才尝试用确定性证据直接回答，
# 避免把「把趋势图改成折线图」这类修改请求误判成查询。
READONLY_QUESTION_WORDS = (
    "哪些", "什么", "怎么", "能不能", "可不可以", "能否", "是否",
    "有没有", "支持", "吗", "？", "?",
)

# 明确的改动动词：出现这些词就是在下指令，不是在提问。
# 「这个能按分钟看吗」是提问，「改成按分钟粒度」是指令；
# 即使指令后面带问号（「改成按分钟粒度，可以吗」），也仍然是指令。
EDIT_COMMAND_WORDS = (
    "改成", "改为", "换成", "换为", "调成", "设为", "设定为",
)

# 「你自己决定 / 别问了」：用户把选择权交回给系统。
#
# 规格第 16 条：这时**停止阻塞追问**，改为以可见假设继续，
# **不代用户接受** —— 不自动接受计划、不替用户确认修改。
DELEGATION_WORDS = (
    "你自己决定", "你看着办", "你自己看着办", "你来决定", "你决定",
    "别问了", "不用问了", "不要问了", "别再问了", "不用再问",
    "随便你", "随便吧", "自行决定", "你随便",
)

# 交给用户「不再追问」之后的非阻塞说明里给出的可选替代（规格第 18 条）。
DELEGATION_ALTERNATIVES = (
    "① 把要改的对象和改法一次说完整（例如「把第 2 张卡片标题改成 X」），我直接出候选计划；",
    "② 保持当前报表不变，先问别的问题；",
    "③ 换一个对象重新提一次修改要求。",
)

# 目标定位依赖「用户实际看到的可见顺序」的解析方式。
# 序数和指代词必须靠可见顺序定位；精确名称和选中卡片本身就是可靠锚点，
# 不要求前端回传 visibleRegionIds。
ORDER_DEPENDENT_REASONS = frozenset({
    "ordinal", "relative_previous", "demonstrative_without_selection",
})

logger = logging.getLogger(__name__)

#: 答案里编了实时进度、又没法用会话事实换掉时的安全兜底。
#:
#: 不给用户看假数字，也不把编造的内容留在屏幕上。
QUERY_UNSAFE_ANSWER = (
    "这次查询没有完成，暂时无法确认当前对话的情况。"
    "草稿和待确认操作都没有变化，可以稍后再问一次。"
)
EmitEvent = Callable[[str, dict[str, Any]], Awaitable[None]]


def _question_from_pending(raw: dict[str, Any]) -> AgentQuestion:
    """从 ``sessions.pending_choice`` 里读回那份有效菜单。

    老会话的菜单**没有** ``lockScope`` 这个键（它是本轮新增的键）：
    按契约表 §5.3「旧数据的默认行为」一律当成 ``"menu_only"`` 处理 ——
    等于今天的行为（不锁输入）。

    这里必须显式补上这个键，不能让模型按阶段判据推导：否则老会话里那份
    「模板已生成、等待下一步操作」的菜单会在刷新后突然把输入锁住，
    与「老数据按 menu_only 处理」的规定相反。
    """
    data = dict(raw)
    data.setdefault("lockScope", LOCK_SCOPE_MENU_ONLY)
    return AgentQuestion(**data)



async def _emit(
    emit: EmitEvent | None, event: str, data: dict[str, Any]
) -> None:
    if emit is not None:
        await emit(event, data)


def _build_agent():
    """按配置选择 Agent 实现。"""
    impl = get_settings().agent_impl
    if impl == "agentscope":
        try:
            from app.agents.agentscope_report_agent import AgentScopeReportAgent

            logger.info("使用 AgentScopeReportAgent（火山方舟）")
            return AgentScopeReportAgent()
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "AgentScopeReportAgent 初始化失败，降级 FakeAgent：%s", e
            )
            return FakeReportAgent()
    logger.info("使用 FakeReportAgent")
    return FakeReportAgent()


class AgentService:
    def __init__(self) -> None:
        self._agent = _build_agent()

    def _remember_choice(self, response: AgentResponse) -> AgentResponse:
        if response.state == AgentState.WAITING_USER_CONFIRM:
            # 所有生成路径共用同一确认协议；旧 Agent 的 confirm_draft 不能绕过选项锁。
            response.questions = self._editing_confirm_questions()
        choice = next(
            (q for q in response.questions if q.selectionOnly and q.options), None
        )
        session_key = current_session_key() or response.conversationId
        if choice:
            # 选择锁存库（不是进程内存）：后端重启后恢复历史会话仍能拿到锁，
            # 不会出现「前端放行自由输入、后端拒收」的前后端不一致。
            session_service.save_pending_choice(
                session_key,
                {
                    "state": response.state.value,
                    "question": choice.model_dump(mode="json"),
                },
            )
        return response

    def _keep_choice_untouched(self, response: AgentResponse) -> AgentResponse:
        """只读查询轮：菜单照原样带回，但**不落库**。

        查询不是决定，不能替用户接受、取消或替换计划，也不能顺手
        「恢复」出一个原本不存在的选择锁（ADR 0002 §6.1：
        「查询回答后：原菜单、pendingChoice、待确认修改计划、澄清轮次全部不变」）。

        业务轮用 ``_remember_choice`` 落库选择锁；查询轮只把菜单放进响应，
        让前端照常渲染，剩下的事等用户真的选一项再说。
        """
        return response

    # ---------- 只读查询轮（工单 #13 / #14） ----------

    @staticmethod
    def _pending_choice_required(
        request: AgentRequest,
        state: AgentState,
        question: AgentQuestion,
    ) -> AgentResponse:
        """待确认期间收到非只读提问的输入：仍然必须从原菜单里选。

        菜单原样带回，草稿和待确认计划都不动。
        说不清是提问还是操作时，给一句针对性的澄清，而不是让用户重述。
        """
        if routing_rules.has_operation_command(request.message):
            reply = (
                "请先从下拉框选择一项再继续操作。"
                "如果只是想问当前对话的情况（草稿、进度、能力），直接问就行。"
            )
        else:
            reply = (
                "这句我没太明白：你是想修改这份报表（说清楚改哪一处），"
                "还是想先问当前对话的情况？"
                "原来的选择我给你留着，也可以直接从下拉框选。"
            )
        return AgentResponse(
            conversationId=request.conversationId,
            state=state,
            reply=reply,
            designSessionId=request.context.designSessionId,
            questions=[question],
            uiActions=["selection_required"],
        )

    def _is_readonly_query_turn(
        self,
        request: AgentRequest,
        route: RouteDecision,
        pending_guard: tuple[AgentState, AgentQuestion] | None,
    ) -> bool:
        """本轮是不是只读查询（查事实、只回答，不推进业务）。"""
        if request.message == ADVANCE_MESSAGE:
            # 内部控制消息，永远不是用户提问。
            return False
        if route.userIntent is UserIntent.ASK_QUESTION:
            return True
        if route.userIntent is UserIntent.ISSUE_FEEDBACK:
            # 问题反馈与咨询走同一套只读事实查证与回答契约
            # （规格第 52 条）：只解释与建议，不改定义、不产生待接受计划。
            return True
        if pending_guard is not None:
            # 待确认入口：模型没能给出可判定意图时，只有**明确的只读提问**
            # 才进查询链路；其它输入（操作指令、填选项、含糊话）仍然被拒，
            # 不能绕过菜单确认约束。
            return route.userIntent is UserIntent.UNKNOWN and (
                routing_rules.is_readonly_question(request.message)
            )
        if (
            request.state is AgentState.USER_EDITING_TEMPLATE
            and request.snapshots.reportDefinition is not None
        ):
            # 编辑态跳过了通用路由（避免把模糊编辑误判成闲聊），
            # 这里用规则层的只读提问判定兜底。
            return routing_rules.is_readonly_question(request.message)
        return False

    async def _handle_readonly_query(
        self,
        request: AgentRequest,
        route: RouteDecision,
        pending_guard: tuple[AgentState, AgentQuestion] | None,
    ) -> AgentResponse:
        """只读查询轮：查事实、回答，本轮不推进业务。"""
        request.queryMode = True
        handle = getattr(self._agent, "ahandle", None)
        try:
            if handle is not None:
                response = await handle(request)
            else:
                response = self._agent.handle(request)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[agent.chat] step=readonly_query_failed conversationId=%s",
                request.conversationId,
            )
            response = self._query_failure_response(request)
        response.debug = {
            **(response.debug or {}),
            "contextEngineStage": route.engineStage or "unknown",
            "contextEngineIntent": route.userIntent.value,
            "contextEngineReason": route.reason,
        }
        return self._keep_choice_untouched(
            self._normalize_query_answer(request, response, pending_guard)
        )

    def _normalize_query_answer(
        self,
        request: AgentRequest,
        response: AgentResponse,
        pending_guard: tuple[AgentState, AgentQuestion] | None,
    ) -> AgentResponse:
        """把查询轮收敛成「不推进业务」的契约。

        这里做的是**服务端强制**，不依赖模型自觉，也不只靠 Prompt：
            - 状态回到本轮进入时的状态；
            - 业务对象用服务端可信记录，不回写请求快照；
            - 原待确认菜单保留，不出现无关生成选项；
            - 不发起任何执行类工具请求。
        """
        response.state = request.state
        response.turnKind = TURN_KIND_QUERY_ANSWER
        response.toolRequests = []
        response.uiActions = ["query_answer"]

        facts = session_fact_service.load(
            session_id=current_session_key() or request.conversationId,
            project_id=request.context.projectId,
            subject_ref=request.context.subjectRef,
            subject_id=request.context.subjectId,
            request_has_draft=request.snapshots.reportDefinition is not None,
        )
        response.domainObjects = AgentResponseDomainObjects(
            reportDefinitionDraft=(
                facts.definition or request.snapshots.reportDefinition
            ),
            reportPolicyDraft=facts.policy or request.snapshots.reportPolicy,
        )
        response.questions = self._preserved_menu(request, pending_guard)
        response.debug = {
            **(response.debug or {}),
            "queryMode": True,
            "factStatus": facts.status.value,
        }
        self._ensure_recorded_stage_disclosure(request, response, facts)
        return response

    @staticmethod
    def _ensure_recorded_stage_disclosure(
        request: AgentRequest,
        response: AgentResponse,
        facts: SessionFacts,
    ) -> None:
        """只读答案的边界校正：不说假话，也不漏掉必须说的边界。

        工单 #03：「回答明确是已记录状态，不冒充实时任务监控」；
        父规格 #12：「数据可能不是实时的，应以用户可理解的方式说明」。

        这条要求不能只靠模型自觉：模型可能把边界写进 ``limitations``
        （结构化字段，页面不渲染），也可能干脆没写。这里由服务端用
        **共用层的口径**处理：

            - 编了实时进度（百分比 / 正在跑）：任何查询回答都不能把假数字
              给用户，也不能靠再补一句否认来抵消 —— 两句话会打架；
              问进度时用服务端事实换掉整句回答，其它问题走安全兜底；
            - 问进度时：答案没同时说清「已记录」与「不是实时」，补上共用层
              的口径句；澄清轮与技术失败轮不补（那不是进度回答）；
            - 问草稿、问能力不补这条边界（口径不能加错地方）。
        """
        reply = response.reply or ""
        if claims_live_progress(reply):
            response.reply = (
                recorded_stage_fallback(facts)
                if routing_rules.is_progress_question(request.message)
                else QUERY_UNSAFE_ANSWER
            )
            return
        if not routing_rules.is_progress_question(request.message):
            return
        outcome = (response.debug or {}).get("queryOutcome")
        if outcome in {"clarification", "technical_failure"}:
            return
        if recorded_stage_disclosed(reply):
            return
        response.reply = (
            f"{reply.strip()}\n{RECORDED_STAGE_DISCLOSURE}"
        ).strip()

    def _preserved_menu(
        self,
        request: AgentRequest,
        pending_guard: tuple[AgentState, AgentQuestion] | None,
    ) -> list[AgentQuestion]:
        """查询回答之后仍然要能用的原业务菜单。

        只有**原来就有**的菜单才带回。库里没有就不给：
        ADR 0002 §4.2「questions = 原有待确认菜单；没有菜单时为空」，
        规格「查询前后保持…待确认选项…不变」。

        凭空造一个 template_edit_confirm 出来，前端会把它渲染成可点的
        确认按钮，但服务端并没有这把锁 —— 用户点了会落空，
        而且 pendingChoice 也被查询轮改掉了。
        """
        if pending_guard is not None:
            return [pending_guard[1]]
        raw = session_service.load_pending_choice(
            current_session_key() or request.conversationId
        )
        if isinstance(raw, dict):
            try:
                AgentState(raw["state"])
                return [_question_from_pending(raw["question"])]
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "[agent.chat] step=pending_choice_invalid conversationId=%s raw=%s",
                    request.conversationId,
                    raw,
                )
        return []

    @staticmethod
    def _query_failure_response(request: AgentRequest) -> AgentResponse:
        """查询失败：如实说失败，不降级成「请补充生成条件」的引导。"""
        message = (
            "这次查询没有完成，暂时无法确认当前对话的情况。"
            "草稿和待确认操作都没有变化，可以稍后再问一次。"
        )
        return AgentResponse(
            conversationId=request.conversationId,
            state=request.state,
            reply=message,
            designSessionId=request.context.designSessionId,
            error=AgentError(code="QUERY_UNAVAILABLE", message=message),
            debug={"queryMode": True, "queryOutcome": "technical_failure"},
        )

    async def chat(
        self, request: AgentRequest, emit: EmitEvent | None = None
    ) -> AgentResponse:
        total_started = perf_counter()
        log_timeline_step("service_start")
        await _emit(
            emit,
            "stage",
            {"stage": "service_start", "state": request.state.value},
        )
        logger.info(
            "[agent.chat] step=service_start conversationId=%s state=%s impl=%s",
            request.conversationId,
            request.state.value,
            self._agent.__class__.__name__,
        )

        # 协议版本检查
        if request.agentProtocolVersion != AGENT_PROTOCOL_VERSION:
            logger.warning(
                "[agent.chat] step=protocol_version_invalid conversationId=%s expected=%s actual=%s",
                request.conversationId,
                AGENT_PROTOCOL_VERSION,
                request.agentProtocolVersion,
            )
            return self._protocol_error(
                request,
                code="INVALID_REQUEST",
                message=(
                    f"agentProtocolVersion 必须是 {AGENT_PROTOCOL_VERSION}，"
                    f"当前是 {request.agentProtocolVersion}"
                ),
            )

        session_key = bind_session_key(request)
        design_state = report_design_service.load_state(request)
        if design_state is not None:
            record = session_service.repository.get_session(session_key)
            snapshot = (record.snapshot if record else None) or {}
            scope = snapshot.get("scope") or {}
            if any(scope.get(key) not in (None, getattr(request.context, key))
                   for key in ("projectId", "subjectRef", "subjectId")):
                response = self._protocol_error(request, code="SESSION_SCOPE_MISMATCH",
                                                message="请求范围与当前会话不一致，未执行操作。")
                response.state = AgentState(record.state) if record else request.state
                # Read-only refusal must not write the mismatched scope into the session.
                response.turnKind = TURN_KIND_QUERY_ANSWER
                return response
            if design_state.get("designPhase") == DESIGN_PHASE_DRAFT_READY:
                request.state = AgentState(record.state) if record else request.state
                request.snapshots = AgentRequestSnapshots(
                    reportDefinition=ReportDefinitionDraft(**snapshot["definition"]) if snapshot.get("definition") else None,
                    reportPolicy=ReportPolicyDraft(**snapshot["policy"]) if snapshot.get("policy") else None,
                    lastReportRun=snapshot.get("lastReportRun"),
                )
                request.context.reportDefinitionId = record.reportDefinitionId if record else None
                request.context.baseDefinitionRevision = (
                    (snapshot.get("definition") or {}).get("metadata", {}).get("revision")
                )
                if (request.context.designAction or "").strip().upper() == DESIGN_ACTION_CONFIRM_PLAN:
                    # 重复点「确认方案」：仍然从**「确认方案」入口**发起，
                    # 走既有确认路径复用同一份草稿（幂等），不产生第二份。
                    #
                    # 只有带 designAction 的请求能走到这里；自由输入没有
                    # designAction，仍由下面的待确认菜单挡住。
                    if request.message.strip() and not is_pure_confirmation(request.message):
                        return self._protocol_error(request, code="PLAN_ALREADY_CONFIRMED",
                                                    message="方案已经确认成草稿，请从当前草稿的下一步操作继续。")
                    return report_design_service.confirm_plan(request, state=design_state)
                if (request.context.designAction or "").strip().upper() == DESIGN_ACTION_CONFIRM_TEMPLATE:
                    if request.message.strip() and not is_pure_confirmation(request.message):
                        return self._protocol_error(request, code="TEMPLATE_ALREADY_CONFIRMED",
                                                    message="模板已确认，请从当前草稿的修改入口继续。")
                    return report_design_service.confirm_template(request, state=design_state)
            else:
                # Template confirmation never accepts executable client snapshots.
                request.snapshots = AgentRequestSnapshots()
                if request.message == ADVANCE_MESSAGE:
                    return report_design_service.keep_state_response(request, design_state)
        # 恢复上一轮路由层收集到的槽位：本轮路由在它基础上继续补全，
        # 不会重新问用户已经答过的问题。
        loaded_collected = session_service.load_collected_info(session_key)
        # resolvedInputs / systemDefaults 是上一轮路由算出来的派生结果，
        # 下一轮要重算，不能当作「用户说过的槽位」继承。
        for reserved in ("resolvedInputs", "systemDefaults"):
            loaded_collected.pop(reserved, None)
        request.collectedInfo = loaded_collected
        pending_raw = session_service.load_pending_choice(session_key)
        pending = None
        if pending_raw:
            try:
                pending_state = AgentState(pending_raw["state"])
                pending = (pending_state, _question_from_pending(pending_raw["question"]))
            except (KeyError, ValueError):
                logger.warning(
                    "[agent.chat] step=pending_choice_invalid conversationId=%s raw=%s",
                    request.conversationId,
                    pending_raw,
                )
                session_service.save_pending_choice(session_key, None)
        if session_service.load_pending_plan(session_key):
            # 待决计划比旧的其他选择锁更权威；确认不能被迟到的题目覆盖。
            pending = (AgentState.USER_EDITING_TEMPLATE, self._pending_edit_question())

        # 待确认入口（工单 #14）：
        #
        #   - 选项值 -> 走原来的确认流程，一行不改；
        #   - 其它自由输入 -> 先不拒绝，交给路由判定这是不是**只读提问**。
        #     是只读提问就放行进查询链路，问完原菜单和待确认计划照旧；
        #     不是只读提问，仍然按原来的「必须从菜单里选」拒绝。
        #
        # pending_guard 非空时，本轮**不允许**触发任何执行动作
        # （接受修改 / 确认报表 / 保存 / 校验 / 预览 / 生成草稿）。
        pending_guard: tuple[AgentState, AgentQuestion] | None = None
        if pending:
            pending_state, question = pending
            if request.state != pending_state:
                # 客户端状态与服务端的待确认状态不符（客户端过期 / 不同步）。
                #
                # 原来是直接拒绝，让客户端回去按菜单选。但「菜单在」这件事
                # 由服务端说了算，客户端状态过期不该让**只读提问**也被拒：
                # 只读提问没有任何可绕过的执行权限，放行进查询链路，
                # 问完原菜单和待确认计划照旧（查询轮不落库选择锁、
                # 不重建业务对象）。
                #
                # 操作指令仍然照旧拒绝 —— 这个口子不能变成绕过确认的入口。
                if routing_rules.is_readonly_question(request.message):
                    pending_guard = (pending_state, question)
                else:
                    return self._pending_choice_required(
                        request, pending_state, question
                    )
            else:
                allowed_values = {option.value for option in question.options} | (
                    {"重新开始"} if question.allowRestart else set()
                )
                if request.message not in allowed_values:
                    pending_guard = (pending_state, question)
                else:
                    session_service.save_pending_choice(session_key, None)
                    if question.id == EDIT_CHOICE_ID:
                        return self._handle_pending_edit_choice(request, session_key)
                    if question.id.startswith("template_preview_"):
                        result = template_preview_flow.answer(request, question.id)
                        await _emit(emit, "stage", {
                            "stage": "template_preview_choice",
                            "choice": question.id,
                            "responseState": result.state.value,
                        })
                        return self._remember_choice(result)
                    if question.id == "template_edit_confirm":
                        if request.message == CONFIRM_BUTTON_VALUE:
                            return self._save_placeholder(request, RouteDecision())
                        if request.message == RESTART_BUTTON_VALUE:
                            # 「重新开始」要**落库**地把设计阶段复位回
                            # 「需要生成方案」（工单 #44）：不复位的话这个
                            # 会话会永远停在 DRAFT_READY，再也拿不到新的
                            # 文字方案。复位由设计服务做，阶段与这一轮
                            # 的消息同事务提交。
                            return report_design_service.restart_design(
                                request, state=design_state
                            )
                        return self._editing_prompt_response(
                            request,
                            reply="好的，请描述你想怎样修改当前草稿。",
                        )
                    if question.id == "ready_action":
                        if request.message == "继续修改":
                            if request.snapshots.reportDefinition is None:
                                return self._remember_choice(AgentResponse(
                                    conversationId=request.conversationId,
                                    state=AgentState.READY,
                                    reply="当前报表草稿未随请求带回，无法继续修改。请恢复草稿后重试。",
                                    designSessionId=request.context.designSessionId,
                                    questions=self._ready_questions(),
                                    uiActions=["show_ready_actions"],
                                ))
                            return self._editing_prompt_response(
                                request,
                                reply="好的，请描述你想怎样修改当前报表。",
                            )
                        action = "发布" if request.message == "发布" else "导出 PDF"
                        return self._remember_choice(AgentResponse(
                            conversationId=request.conversationId,
                            state=AgentState.READY,
                            reply=f"{action}流程尚未接入（Mock），请继续从下拉框选择。",
                            designSessionId=request.context.designSessionId,
                            questions=self._ready_questions(),
                            uiActions=["show_ready_actions"],
                        ))

        if pending_guard is None and request.message.strip() in {
            EDIT_ACCEPT, EDIT_ADJUST, EDIT_CANCEL,
        }:
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.USER_EDITING_TEMPLATE,
                reply="当前没有待确认的修改计划，请重新说明要修改什么。",
                designSessionId=request.context.designSessionId,
                domainObjects=AgentResponseDomainObjects(
                    reportDefinitionDraft=request.snapshots.reportDefinition,
                    reportPolicyDraft=request.snapshots.reportPolicy,
                ),
            )

        if pending_guard is None and request.state == AgentState.READY:
            # 单进程重启后待选项可能丢失；仍先恢复必选题，不允许直接路由自由文本。
            return self._remember_choice(AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.READY,
                reply="报表已就绪，请先从下拉框选择下一步操作。",
                designSessionId=request.context.designSessionId,
                questions=self._ready_questions(),
                uiActions=["show_ready_actions"],
            ))

        if pending_guard is None and request.message.strip() in {
            "继续工作", "继续上一条", "继续刚才",
        }:
            logger.info(
                "[agent.chat] step=resume_unavailable conversationId=%s state=%s",
                request.conversationId,
                request.state.value,
            )
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.ANALYZING,
                reply="当前没有可恢复的上一条请求，请重新描述要处理的内容。",
                uiActions=["resume_unavailable"],
            )

        # 设计流（#19）：新建流程先给文字方案。
        #
        # 这里只做两件事：认出「这一轮归设计流程管」，
        # 以及把「先看模板」提前分流挡在设计流程外面。
        #
        #   - 模板流在旧流程里跑在路由之前，所以它会先抢走「先看模板」。
        #     规格写明这类提前分流必须进入新建流程，所以设计轮不走模板流。
        #   - 只读提问不能被模板流抢走：模板流遇到表达不了的条件会直接
        #     BLOCKED_OR_UNSUPPORTED —— 那是用一句提问触发一条执行分支
        #     （ADR 0002 §6.1 / 规格 #12）。
        design_turn = (
            (design_state is not None and design_state.get("designPhase") != DESIGN_PHASE_DRAFT_READY)
            or (design_state is None and report_design_service.is_new_build(request))
        )
        readonly_rule = routing_rules.is_readonly_question(request.message)
        # 「纯确认」与「模糊认可」这两类消息：都不是提问，也不走查询链路。
        # 服务端自己能认出来（规格 #18），不需要再问一次路由层。
        design_acknowledgement = is_pure_confirmation(
            request.message
        ) or is_vague_approval(request.message)
        # 这些设计轮**不跑**旧路由：
        #   - 新建轮：路由结果没有人消费；
        #   - 纯确认 / 模糊认可：都不是提问，不走查询链路；
        #   - 没有疑问语气：这一轮是要求，不是提问，路由判定用不上。
        #
        # 旧路由是一次真实模型往返（实测 60~150 秒）。在这些轮次里，
        # 用户要先等一个用不上的结果才看到自己的方案。
        #
        # 有疑问语气的轮次仍然跑旧路由：规则层只覆盖「指向报表业务对象」
        # 的提问，「你能做什么？」这类元问题要靠路由层再判一次，
        # 免得一句提问平白多出一版方案。
        design_skips_router = design_turn and (
            (design_state is None and not readonly_rule)
            or design_acknowledgement
            or not routing_rules.looks_like_question(request.message)
        )

        if (
            pending_guard is None
            and not design_turn
            and not readonly_rule
            and template_preview_flow.is_request(request)
        ):
            result = template_preview_flow.start(request)
            await _emit(emit, "stage", {
                "stage": "template_preview_start",
                "responseState": result.state.value,
                "questionIds": [question.id for question in result.questions],
            })
            return self._remember_choice(result)

        # 节点 6-11 的自动推进：前端收到上一个状态后再发一次 __advance__
        # 跳过 ContextEngine，直接按 state 走对应 handler。
        if pending_guard is None and request.message == ADVANCE_MESSAGE:
            auto_started = perf_counter()
            start_stage_by_state = {
                AgentState.SAVE_DEFINITION_DRAFT: "auto_advance_start",
                AgentState.VALIDATING: "validation_start",
                AgentState.PREVIEWING: "preview_start",
                AgentState.PREVIEW_READY: "render_prepare_start",
                AgentState.RENDERING: "render_done",
            }
            done_stage_by_state = {
                AgentState.SAVE_DEFINITION_DRAFT: "auto_advance_done",
                AgentState.VALIDATING: "validation_done",
                AgentState.PREVIEWING: "preview_done",
                AgentState.PREVIEW_READY: "render_prepare_done",
                AgentState.RENDERING: "render_done",
            }
            await _emit(
                emit,
                "stage",
                {
                    "stage": start_stage_by_state.get(request.state, "auto_advance_start"),
                    "state": request.state.value,
                    "message": request.message,
                },
            )
            auto = self._auto_advance(request)
            if auto is not None:
                log_timeline_step("auto_advance")
                await _emit(
                    emit,
                    "stage",
                    {
                        "stage": "auto_advance",
                        "fromState": request.state.value,
                        "toState": auto.state.value,
                    },
                )
                await _emit(
                    emit,
                    "stage",
                    {
                        "stage": done_stage_by_state.get(request.state, "auto_advance_done"),
                        "state": request.state.value,
                        "responseState": auto.state.value,
                        "stepElapsedMs": round((perf_counter() - auto_started) * 1000),
                    },
                )
                return self._remember_choice(auto)

        # 待确认期间不接受自由输入触发的编辑动作：编辑只能从原菜单走。
        #
        # 工单 #58：意图识别排在**任何会产生编辑候选的规则之前**。
        # 这一道门同时覆盖确定性编辑快路（下面这一行）与模型编辑支路
        # （后面的 template_edit_react）：咨询、问题反馈、目录外新增三类
        # 都不产生待接受的修改计划，也不改定义。
        template_edit = None
        classified_intent: UserIntent | None = None
        if pending_guard is None:
            intent = self._classify_intent(request)
            classified_intent = intent
            logger.info(
                "[agent.chat] step=intent_classified conversationId=%s state=%s intent=%s",
                request.conversationId,
                request.state.value,
                intent.value,
            )
            intent_response = await self._handle_intent_before_edit(request, intent)
            if intent_response is not None:
                await _emit(
                    emit,
                    "stage",
                    {
                        "stage": "intent_handled",
                        "state": request.state.value,
                        "intent": intent.value,
                        "responseState": intent_response.state.value,
                    },
                )
                return intent_response
            if intent is not UserIntent.OUT_OF_CATALOG_ADD:
                template_edit = await self._try_handle_template_edit(request)
        if template_edit is not None:
            await _emit(
                emit,
                "stage",
                {
                    "stage": "template_edit_done",
                    "state": request.state.value,
                    "responseState": template_edit.state.value,
                    "editingTargetRegionId": template_edit.debug.get("editingTargetRegionId"),
                    "editingIntent": template_edit.debug.get("editingIntent"),
                },
            )
            return self._remember_choice(
                self._finalize_template_edit(request, template_edit)
            )

        route_started = perf_counter()
        log_timeline_step("context_route_start")
        await _emit(
            emit,
            "stage",
            {"stage": "context_route_start", "state": request.state.value},
        )
        logger.info(
            "[agent.chat] step=context_route_start conversationId=%s state=%s",
            request.conversationId,
            request.state.value,
        )
        # 模糊编辑已经由规则分流确认；通用路由可能把它误判为闲聊或重新生成。
        if request.state == AgentState.USER_EDITING_TEMPLATE and request.snapshots.reportDefinition:
            route = RouteDecision(
                # 工单 #58：把规则层判出的意图带进路由决策，观测时能看出
                # 这一轮到底是「明确修改」还是「目录外新增」等。
                userIntent=classified_intent or UserIntent.UNKNOWN,
                routingAction=RoutingAction.CONTINUE_CURRENT,
                isTemplateEdit=True,
                engineStage="template_edit_react",
                reason="规则无法确定编辑计划，交给受约束 ReAct 查证",
                collectedInfo=dict(request.collectedInfo),
            )
        elif design_skips_router:
            # 这些设计轮**不跑**旧路由。
            #
            # 旧路由是一次真实模型往返，实测 60~150 秒；在这些轮次里它的
            # 结果没有人消费 —— 等于让用户先等一个用不上的结果，才看到
            # 自己要的答案。
            #
            # 顺带的好处：旧路由会把「昨日」「默认能耗指标」这类默认值
            # 写进 collectedInfo 落进快照 —— 设计流程没有让用户指定这些，
            # 不该由默认值无声替代。
            route = RouteDecision(
                userIntent=UserIntent.NEW_TASK,
                routingAction=RoutingAction.CONTINUE_CURRENT,
                engineStage="design_direct",
                reason="设计流程：这一轮直接进设计流程，不跑旧路由",
                collectedInfo=dict(request.collectedInfo),
            )
        else:
            route = await context_engine.route(request, emit=emit)
        # 路由层已经把「历史已收集」和「本轮新收集」合并好了。
        # 写回请求对象：业务 Agent 要用它，落库也要靠它跨轮保留。
        request.collectedInfo = dict(route.collectedInfo or {})
        # resolvedInputs：定义组装层真正要消费的结构化结果（用户指定 > 系统暂选）。
        # 单独放一个键，避免系统默认值被当成「用户说过的」在下一轮压住新值。
        if route.resolvedInputs:
            request.collectedInfo["resolvedInputs"] = dict(route.resolvedInputs)
            request.collectedInfo["systemDefaults"] = {
                key: route.resolvedInputs[key]
                for key in route.defaultInputKeys
                if key in route.resolvedInputs
            }
        log_timeline_step("context_route_done")
        await _emit(
            emit,
            "stage",
            {
                "stage": "context_route_done",
                "intent": route.userIntent.value,
                "action": route.routingAction.value,
                "targetState": route.targetState.value if route.targetState else None,
                "engineStage": route.engineStage,
                "isReadyForNextStep": route.isReadyForNextStep,
                "missingInfo": route.missingInfo,
                "reason": route.reason,
            },
        )
        logger.info(
            "[agent.chat] step=context_route_done conversationId=%s elapsed_ms=%.1f state=%s intent=%s action=%s target=%s ready=%s missing=%s reason=%r",
            request.conversationId,
            (perf_counter() - route_started) * 1000,
            request.state.value,
            route.userIntent.value,
            route.routingAction.value,
            route.targetState.value if route.targetState else None,
            route.isReadyForNextStep,
            route.missingInfo,
            route.reason,
        )

        state_before_apply = request.state.value
        # 设计流（#19）：这一轮归设计流程管。
        #
        # 「提问不改设计版本」是硬要求，所以这里要把**提问**和**修改**分开：
        #
        #   - 规则层判定为只读提问 → 直接走旧查询链路；
        #   - 已经有方案在等确认，而规则层没认出来（规则层只覆盖
        #     「问当前对话 / 当前产品的事」）→ 问路由层，用与旧流程
        #     同一套判定，避免一句元问题平白多出一版方案；
        #   - 其余 → 设计轮（新建或修改）。
        if design_turn:
            if request.message == ADVANCE_MESSAGE:
                # 设计流程不走旧的自动推进。
                # __advance__ 是旧状态机的内部控制消息，在设计流程里必须
                # 原样返回 —— 掉下去会被业务 Agent 当成一次生成请求，
                # 平白生成一份草稿，绕开了「先确认，再生成」。
                return report_design_service.keep_state_response(
                    request, design_state
                )
            is_question = readonly_rule
            if (
                not is_question
                and design_state is not None
                # 纯确认 / 模糊认可不是提问，别再问一次路由层。
                and not design_acknowledgement
                # 这一轮没跑旧路由（没有疑问语气），路由判定用不上。
                and not design_skips_router
            ):
                is_question = self._is_readonly_query_turn(
                    request, route, pending_guard
                )
            if is_question:
                response = await self._handle_readonly_query(
                    request, route, pending_guard
                )
                # 查询轮不改设计对象，但要把设计阶段与方案带回响应：
                # 前端要据此继续显示那份待确认的方案。
                report_design_service.attach_design_state(response, design_state)
                return response
            # 设计轮不把旧路由的收集结果带进快照。
            #
            # 旧路由是旧流程的槽位采集器，它会把「昨日」「默认能耗指标」
            # 这类**系统暂选**写进 collectedInfo。设计会话里方案才是需求的
            # 权威记录，把这些默认值写进快照，等于让用户没指定的条件
            # 悄悄顶替 —— 工单 01 验收明确要求「未指定的条件注明假设，
            # 不被默认值无声替代」。
            #
            # 只在这一条（设计轮）清：查询轮的回答要用会话事实，
            # 旧流程的轮次一个字都不动。
            request.collectedInfo = {}
            design_response = await report_design_service.handle(
                request, emit=emit, state=design_state
            )
            if design_response is not None:
                return design_response

        # 只读查询轮：查事实、回答，不推进业务。
        # 必须在 _apply_context_route 之前判定 —— 那里的 JUMP_STATE 会真的
        # 保存草稿，等于绕过待确认菜单。
        if self._is_readonly_query_turn(request, route, pending_guard):
            return await self._handle_readonly_query(request, route, pending_guard)
        if pending_guard is not None:
            # 不是只读提问：维持原来的约束，必须从菜单里选。
            return self._pending_choice_required(
                request, pending_guard[0], pending_guard[1]
            )
        intercepted = self._apply_context_route(request, route)
        await _emit(
            emit,
            "stage",
            {
                "stage": "apply_context_route",
                "state": state_before_apply,
                "action": route.routingAction.value,
                "targetState": route.targetState.value if route.targetState else None,
                "debugParams": {
                    "路由判定": {
                        "intent": route.userIntent.value,
                        "action": route.routingAction.value,
                        "targetState": route.targetState.value if route.targetState else None,
                        "confidence": route.confidence,
                        "ready": route.isReadyForNextStep,
                        "requiresUserConfirm": route.requiresUserConfirm,
                        "requiredInfo": route.requiredInfo,
                        "collectedInfoKeys": list(route.collectedInfo),
                        "missingInfo": route.missingInfo,
                        "nextQuestion": route.nextQuestion,
                        "reason": route.reason,
                    },
                    "执行结果": {
                        "fromState": state_before_apply,
                        "effectiveState": request.state.value,
                        "outcome": "INTERCEPTED" if intercepted else "CONTINUE_TO_BUSINESS_AGENT",
                        "responseState": intercepted.state.value if intercepted else None,
                    },
                },
            },
        )
        if intercepted is not None:
            intercepted.debug = {**self._build_debug(route), **(intercepted.debug or {})}
            log_timeline_step("context_route_intercepted")
            await _emit(
                emit,
                "stage",
                {
                    "stage": "context_route_intercepted",
                    "responseState": intercepted.state.value,
                },
            )
            logger.info(
                "[agent.chat] step=context_route_intercepted conversationId=%s elapsed_total_ms=%.1f response_state=%s uiActions=%s",
                request.conversationId,
                (perf_counter() - total_started) * 1000,
                intercepted.state.value,
                intercepted.uiActions,
            )
            return self._remember_choice(intercepted)

        if request.state == AgentState.USER_EDITING_TEMPLATE and isinstance(
            self._agent, FakeReportAgent
        ):
            notice = self._edit_notice(
                request,
                "当前模型不可用，无法查证这项编辑问题；草稿没有变化，请稍后重试。",
                code="EDIT_VERIFICATION_UNAVAILABLE",
            )
            # 工单 #58：把这一轮判出的意图带进调试信息，观测时能看出
            # 「目录外新增」这类请求是被意图门分流到通用路径的。
            notice.debug = {**self._build_debug(route), **(notice.debug or {})}
            return notice

        handle = getattr(self._agent, "ahandle", None)
        agent_started = perf_counter()
        log_timeline_step("business_agent_start")
        await _emit(
            emit,
            "stage",
            {
                "stage": "business_agent_start",
                "impl": self._agent.__class__.__name__,
                "state": request.state.value,
                "debugParams": {
                    "Agent 配置": {
                        "impl": self._agent.__class__.__name__,
                        "execution": "async" if handle is not None else "sync",
                        "configuredModel": (
                            get_settings().volc_model if self._agent.__class__.__name__ == "AgentScopeReportAgent"
                            else "local_rules"
                        ),
                    },
                    "输入上下文": {
                        "state": request.state.value,
                        "message": request.message,
                        "projectId": request.context.projectId,
                        "subjectRef": request.context.subjectRef,
                        "subjectId": request.context.subjectId,
                        "designSessionId": request.context.designSessionId,
                        "selectedRegionId": request.context.selectedRegionId,
                        "baseDefinitionRevision": request.context.baseDefinitionRevision,
                        "hasPolicy": request.snapshots.reportPolicy is not None,
                        "hasDefinition": request.snapshots.reportDefinition is not None,
                        "hasPreview": request.snapshots.lastReportRun is not None,
                    },
                },
            },
        )
        logger.info(
            "[agent.chat] step=business_agent_start conversationId=%s impl=%s async=%s state=%s",
            request.conversationId,
            self._agent.__class__.__name__,
            handle is not None,
            request.state.value,
        )
        if handle is not None:
            response = await handle(request)
        else:
            response = self._agent.handle(request)
        log_timeline_step("business_agent_done")
        await _emit(
            emit,
            "stage",
            {
                "stage": "business_agent_done",
                "responseState": response.state.value,
                "toolRequests": [tool.type for tool in response.toolRequests],
                "uiActions": response.uiActions,
                "stepElapsedMs": round((perf_counter() - agent_started) * 1000),
                "debugParams": {
                    "响应": {
                        "fromState": request.state.value,
                        "responseState": response.state.value,
                        "reply": response.reply,
                        "errorCode": response.error.code if response.error else None,
                    },
                    "草稿与变更": {
                        "policyName": (
                            response.domainObjects.reportPolicyDraft.name
                            if response.domainObjects.reportPolicyDraft else None
                        ),
                        "regionIds": (
                            [region.get("id") for region in response.domainObjects.reportDefinitionDraft.regions]
                            if response.domainObjects.reportDefinitionDraft else []
                        ),
                        "hasChangeSet": response.domainObjects.changeSet is not None,
                        "changePaths": (
                            [entry.path for entry in response.domainObjects.changeSet.changed]
                            if response.domainObjects.changeSet else []
                        ),
                        "hasPreview": response.domainObjects.reportRunPreview is not None,
                    },
                    "交互": {
                        "toolRequests": [
                            {"type": tool.type, "target": tool.target}
                            for tool in response.toolRequests
                        ],
                        "uiActions": response.uiActions,
                        "questions": [
                            {"id": question.id, "expects": question.expects}
                            for question in response.questions
                        ],
                        "blocks": [block.code for block in response.blocks],
                    },
                },
            },
        )
        logger.info(
            "[agent.chat] step=business_agent_done conversationId=%s elapsed_ms=%.1f response_state=%s uiActions=%s toolRequests=%s",
            request.conversationId,
            (perf_counter() - agent_started) * 1000,
            response.state.value,
            response.uiActions,
            [tool.type for tool in response.toolRequests],
        )

        # 状态转移校验（不含初始调用从外部传入的 state 本身）
        allowed = ALLOWED_TRANSITIONS.get(request.state, set())
        if response.state != request.state and response.state not in allowed:
            # Fake 阶段把不合法转移视作内部错误；真实接入时应避免发生
            logger.warning(
                "[agent.chat] step=state_transition_invalid conversationId=%s from_state=%s to_state=%s allowed=%s",
                request.conversationId,
                request.state.value,
                response.state.value,
                [state.value for state in allowed],
            )
            return self._protocol_error(
                request,
                code="INVALID_STATE_TRANSITION",
                message=(
                    f"不允许从 {request.state.value} 转移到 "
                    f"{response.state.value}"
                ),
            )

        response.debug = {**self._build_debug(route), **(response.debug or {})}
        log_timeline_step("service_return")
        await _emit(
            emit,
            "stage",
            {"stage": "service_return", "responseState": response.state.value},
        )
        logger.info(
            "[agent.chat] step=service_return conversationId=%s elapsed_total_ms=%.1f response_state=%s",
            request.conversationId,
            (perf_counter() - total_started) * 1000,
            response.state.value,
        )
        return self._remember_choice(self._finalize_template_edit(request, response))

    @staticmethod
    def _build_debug(route: RouteDecision) -> dict[str, Any]:
        return {
            "contextEngineStage": route.engineStage or "unknown",
            "contextEngineIntent": route.userIntent.value,
            "contextEngineAction": route.routingAction.value,
            "contextEngineReason": route.reason,
            "contextEngineIsTemplateEdit": route.isTemplateEdit,
        }

    @staticmethod
    def _pending_edit_question() -> AgentQuestion:
        # 待确认修改计划出现时**恢复选择锁**（契约表 §5.2）：
        # 这一阶段要连输入框一起锁住，用户先选「接受修改 / 继续调整 / 取消」。
        return AgentQuestion(
            id=EDIT_CHOICE_ID,
            prompt="确认这次修改：",
            expects="SINGLE_CHOICE",
            selectionOnly=True,
            lockScope=LOCK_SCOPE_INPUT_AND_MENU,
            options=[
                QuestionOption(value=value, label=value)
                for value in (EDIT_ACCEPT, EDIT_ADJUST, EDIT_CANCEL)
            ],
        )

    @staticmethod
    def _draft_revision(definition: ReportDefinitionDraft) -> int:
        return int((definition.metadata or {}).get("revision") or 0)

    @staticmethod
    def _is_delegated_decision(message: str) -> bool:
        """用户是不是把选择权交回给系统（规格第 16 条）。

        「你自己决定」「别问了」这类话出现时，继续追问就是**阻塞**用户：
        他已经说过了不要再问。
        """
        text = (message or "").strip()
        if not text:
            return False
        return any(word in text for word in DELEGATION_WORDS)

    @staticmethod
    def _is_pure_delegation(message: str) -> bool:
        """整句只有「你自己决定」这类交代，别的什么都没说。

        「你自己决定吧」是纯交代 → 立刻停止阻塞。
        「把标题改成 X，你看着办」是**带着要求的交代** → 仍要出候选，
        只是不再追问。
        """
        text = (message or "").strip()
        if not AgentService._is_delegated_decision(text):
            return False
        rest = text
        for word in DELEGATION_WORDS:
            rest = rest.replace(word, "")
        rest = re.sub(r"[\s,，。.!！?？~、;；:：的了吧呀啊]", "", rest)
        return not rest

    def _delegated_assumption(
        self, request: AgentRequest, definition: ReportDefinitionDraft | None
    ) -> str:
        """写在明面上的假设（规格第 16、18 条）。

        可假设的是**模拟示例的主题、栏目与布局**；不是权限、实际字段，
        更不是用户的接受动作 —— 所以这里只说「沿用什么」，不说「替你定了什么」。
        """
        parts: list[str] = []
        if definition is not None:
            name = (definition.metadata or {}).get("name") or "当前报表"
            regions = definition.regions or []
            parts.append(f"沿用「{name}」现有的栏目、口径与时间窗")
            parts.append(f"共 {len(regions)} 张卡片都不动")
        else:
            parts.append("沿用当前报表现有的栏目与口径")
        parts.append("不替你接受任何改动")
        return "；".join(parts) + "。"

    def _delegated_response(
        self,
        request: AgentRequest,
        definition: ReportDefinitionDraft | None,
        *,
        extra: str = "",
    ) -> AgentResponse:
        """「你自己决定 / 别问了」之后：**停止阻塞**，给可见假设。

        三条硬边界（规格第 16、18 条）：

        1. 不再抛问题（``questions`` 为空）—— 不阻塞用户；
        2. 假设写在明面上，用户能看见、能推翻；
        3. **不代用户接受** —— 不生成待接受计划、不改草稿、不自动接受。
        """
        assumption = self._delegated_assumption(request, definition)
        reply = (
            "好，不再追问了。我按下面的假设继续："
            f"{assumption}"
            f"{extra}"
            "这份报表没有变化，也没有生成待接受的修改计划。"
            "你可以选一种继续方式："
            + "".join(DELEGATION_ALTERNATIVES)
        )
        return AgentResponse(
            conversationId=request.conversationId,
            state=request.state,
            reply=reply,
            designSessionId=request.context.designSessionId,
            domainObjects=AgentResponseDomainObjects(
                reportPolicyDraft=request.snapshots.reportPolicy,
                reportDefinitionDraft=definition or request.snapshots.reportDefinition,
            ),
            questions=[],
            uiActions=["template_editing", "delegated_assumption"],
            debug={
                "editOutcome": "delegated_assumption",
                "editingIntent": "UNKNOWN",
            },
        )

    def _clarification_or_delegated(
        self,
        request: AgentRequest,
        definition: ReportDefinitionDraft | None,
        question: str,
        *,
        choices: list[QuestionOption] | None = None,
        intent: str = "UNKNOWN",
        region_id: str = "",
        delegated: bool = False,
    ) -> AgentResponse:
        """要追问的地方，先看用户是不是已经说了「别问了」。"""
        if delegated:
            return self._delegated_response(request, definition)
        return self._edit_clarification_response(
            request, question, choices=choices, intent=intent, region_id=region_id
        )

    @staticmethod
    def _reference_is_reliable(reference, context) -> bool:
        """这次解析出来的目标是否可信到可以直接执行修改。

        * 序数 / 指代词：必须依赖「用户实际看到的可见顺序」，
          所以要求前端回传了 visibleRegionIds，且里面没有失效 ID
          （草稿变过，顺序对不上，得重新核对）。
        * 精确名称 / 选中卡片：名字和选中项本身就是可靠锚点，
          不要求前端回传可见顺序。
        """
        if reference.status != "RESOLVED":
            return False
        if reference.reason in ORDER_DEPENDENT_REASONS:
            return bool(context.visibleRegionIds) and not reference.staleRegionIds
        return True

    def _finalize_template_edit(
        self, request: AgentRequest, response: AgentResponse
    ) -> AgentResponse:
        """把业务 Agent 的候选落到服务端，并限制同一项修改的追问次数。"""
        if response.state != AgentState.USER_EDITING_TEMPLATE:
            return response
        session_key = current_session_key() or request.conversationId
        raw = (response.debug or {}).get("pendingEditPlan")
        if raw is not None:
            try:
                candidate = PendingEditPlan.model_validate({
                    **raw,
                    "projectId": request.context.projectId,
                    "subjectRef": request.context.subjectRef,
                    "subjectId": request.context.subjectId,
                })
                draft = request.snapshots.reportDefinition
                plan = EditPlan.model_validate(raw["editPlan"])
                record = session_service.repository.get_session(session_key)
                stored = (record.snapshot or {}).get("definition") if record else None
                if (
                    draft is None
                    or candidate.draftRevision != self._draft_revision(draft)
                    or (
                        isinstance(stored, dict)
                        and candidate.draftRevision != self._draft_revision(
                            ReportDefinitionDraft.model_validate(stored)
                        )
                    )
                    or candidate.targetRegionId != plan.target.regionId
                    or plan.needsClarification
                    or plan.intent == EditIntent.UNKNOWN
                ):
                    raise ValueError("候选计划与当前草稿不一致")
                reference_text = request.collectedInfo.get("pendingEditRequest") or request.message
                reference = resolve_visible_reference(
                    reference_text, draft.regions,
                    visible_region_ids=request.context.visibleRegionIds,
                    selected_region_id=request.context.selectedRegionId,
                )
                if reference.status != "NOT_FOUND" and (
                    reference.regionId != candidate.targetRegionId
                    or not self._reference_is_reliable(reference, request.context)
                ):
                    raise ValueError("可见卡片顺序或目标尚未确认")
            except (KeyError, TypeError, ValueError) as exc:
                reason = str(exc) if isinstance(exc, ValueError) else "计划字段不完整"
                return self._edit_notice(
                    request, f"{reason}，草稿没有变化；请重新说明可执行的修改。"
                )
            active = session_service.save_pending_plan_if_absent(
                session_key, candidate
            )
            if active.planId != candidate.planId:
                response.reply = "上一份修改计划仍待确认，请先处理该计划。"
                response.debug["editingTargetRegionId"] = active.targetRegionId or None
            # 这里**不**清澄清计数（契约表 §7.2、规格第 19 条）：
            # 候选计划落库不是新任务，随后的**局部调整**不重置额度。
            # 换一件事（额度键变了）才由 record_clarification 自然重开。
            request.collectedInfo.pop("pendingEditRequest", None)
            request.collectedInfo.pop("pendingEditTimeAnchor", None)
            response.domainObjects.pendingEditPlan = PendingEditPlan(
                planId=active.planId,
                draftRevision=active.draftRevision,
                targetRegionId=active.targetRegionId,
                operations=active.operations,
                summary=active.summary,
                createdAt=active.createdAt,
            )
            response.questions = [self._pending_edit_question()]
            response.debug.pop("pendingEditPlan", None)
            return response

        if (response.debug or {}).get("editOutcome") == "answer":
            # 工单 #58（规格第 14、19 条）：咨询与普通回复**不重置**澄清额度。
            # 额度按「事项」计，同一件事的补充回答接着上一轮算；
            # 换一件事才由键的变化重新计数，这里不再替它清零。
            request.collectedInfo.pop("pendingEditRequest", None)
            request.collectedInfo.pop("pendingEditTimeAnchor", None)
            return response

        if response.error is not None:
            return response
        is_clarification = (
            (response.debug or {}).get("editOutcome") == "clarification"
            or bool(((response.debug or {}).get("editPlan") or {}).get("needsClarification"))
            or any(
                question.id in {"template_edit_clarification",
                                "compilation_grain", "edit_time_clarification",
                                "edit_reference_clarification"}
                for question in response.questions
            )
        )
        if not is_clarification:
            return response
        prior = request.collectedInfo.get("pendingEditRequest")
        intent = (response.debug or {}).get("editingIntent") or "UNKNOWN"
        target = (response.debug or {}).get("editingTargetRegionId") or ""
        current = session_service.load_clarification(session_key)
        # 额度按「事项」计（规格第 14 条）：键 = 「这一项要做的事」。
        #   - 上一轮就是在追同一件事 → 沿用旧键，轮数接着算（不重置）；
        #   - 换了一件事 → 键变了，轮数从零重新计；
        #   - 连要改什么都说不清 → 固定的「未定位」键，仍然只问一轮。
        key = (
            current.key if prior and current
            else f"{intent}:{target}" if intent != "UNKNOWN" or target
            else "edit:unresolved"
        )
        if not session_service.can_ask_clarification(session_key, key=key):
            # 一轮之后仍缺安全目标（规格第 18 条）：保留当前报告，给**非阻塞**
            # 说明与可选替代 —— 不猜目标、不造空计划、不继续循环强制询问。
            response.reply = (
                f"这项修改我已经追问过 {MAX_CLARIFICATION_ROUNDS} 轮，还是没能确定："
                f"{response.reply}"
                "当前报表保持原样，草稿没有修改，也没有替你接受任何改动。"
                "你可以选一种继续方式："
                + "".join(DELEGATION_ALTERNATIVES)
            )
            response.questions = []
            response.uiActions = ["template_edit_clarification_limit"]
            return response
        session_service.record_clarification(
            session_key, key=key, question=response.reply
        )
        if not prior:
            request.collectedInfo["pendingEditRequest"] = request.message
        return response

    def _edit_notice(
        self, request: AgentRequest, message: str, *, code: str | None = None,
        questions: list[AgentQuestion] | None = None,
        ui_actions: list[str] | None = None,
    ) -> AgentResponse:
        record = session_service.repository.get_session(
            current_session_key() or request.conversationId
        )
        snapshot = record.snapshot or {} if record else {}
        stored_draft = snapshot.get("definition")
        stored_policy = snapshot.get("policy")
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.USER_EDITING_TEMPLATE,
            reply=message,
            designSessionId=request.context.designSessionId,
            domainObjects=AgentResponseDomainObjects(
                reportDefinitionDraft=(
                    ReportDefinitionDraft.model_validate(stored_draft)
                    if isinstance(stored_draft, dict)
                    else request.snapshots.reportDefinition
                ),
                reportPolicyDraft=(
                    stored_policy
                    if isinstance(stored_policy, dict)
                    else request.snapshots.reportPolicy
                ),
            ),
            questions=questions or [],
            uiActions=list(ui_actions or []),
            error=AgentError(code=code, message=message) if code else None,
        )

    def _handle_pending_edit_choice(
        self, request: AgentRequest, session_key: str
    ) -> AgentResponse:
        pending = session_service.load_pending_plan(session_key)
        if pending is None:
            return self._edit_notice(
                request, "这份修改计划已失效，草稿没有变化；请重新提出修改。"
            )
        if request.message in {EDIT_CANCEL, EDIT_ADJUST}:
            session_service.clear_pending_plan(session_key)
            if request.message == EDIT_CANCEL:
                # 取消 = 这件事结束了，下一件事按额度键规则重新开。
                session_service.reset_clarification(session_key)
                return self._edit_notice(
                    request, "已取消这次修改，草稿没有变化。"
                )
            # 「继续调整」（规格第 58 条）：旧计划不再可接受，模板本身不变，
            # 回到**统一对话** —— 用户可以接着问、接着提反馈、接着改。
            #
            # 这不是新任务：澄清额度**不重开**（契约表 §7.2「普通回复与局部
            # 调整不重置额度」），所以这里不 reset_clarification。
            return self._edit_notice(
                request,
                "草稿没有变化。请告诉我这份修改计划需要怎样调整。",
                questions=self._editing_prompt_questions(),
                ui_actions=["template_editing", "ask_template_edit_detail"],
            )

        record = session_service.repository.get_session(session_key)
        stored = (record.snapshot or {}).get("definition") if record else None
        try:
            if not isinstance(stored, dict):
                raise ValueError("服务端没有保存原草稿")
            definition = ReportDefinitionDraft.model_validate(stored)
            scope_matches = all(
                (pending.model_extra or {}).get(key) == getattr(request.context, attr)
                for key, attr in (
                    ("projectId", "projectId"),
                    ("subjectRef", "subjectRef"),
                    ("subjectId", "subjectId"),
                )
            )
            if not scope_matches:
                raise ValueError("修改范围与生成计划时不一致")
            incoming = request.snapshots.reportDefinition
            if (
                incoming is None
                or self._draft_revision(incoming) != self._draft_revision(definition)
                or session_service.is_pending_plan_stale(
                    session_key, self._draft_revision(definition)
                )
            ):
                raise ValueError("草稿已变化，需要重新核对修改目标")
            plan = EditPlan.model_validate((pending.model_extra or {})["editPlan"])
            if plan.target.regionId != pending.targetRegionId:
                raise ValueError("修改目标与待确认计划不一致")
        except (KeyError, TypeError, ValueError) as exc:
            session_service.clear_pending_plan(session_key)
            return self._edit_notice(
                request, f"{exc}，本次没有修改草稿。请重新核对目标。",
                code="STALE_EDIT_PLAN",
            )

        scope = ToolScope(
            project_id=request.context.projectId,
            subject_ref=request.context.subjectRef,
            subject_id=request.context.subjectId,
        )
        if session_service.take_pending_plan(session_key, pending.planId) is None:
            return self._edit_notice(
                request, "这份修改计划已经被处理，草稿没有重复修改。",
                code="EDIT_PLAN_ALREADY_HANDLED",
            )
        try:
            result = template_edit_engine.apply(
                plan=plan, definition=definition, scope=scope
            )
        except Exception:  # noqa: BLE001
            logger.exception("待确认编辑计划执行失败 planId=%s", pending.planId)
            return self._edit_notice(
                request, "本轮修改执行遇到技术问题，草稿没有变化；请重新发起修改。",
                code="EDIT_EXECUTION_FAILED",
            )
        session_service.reset_clarification(session_key)
        if result.needsClarification:
            return self._edit_notice(
                request,
                result.clarificationQuestion
                or "修改校验未通过，草稿没有变化；请重新核对目标或数据能力。",
                code="EDIT_VALIDATION_FAILED",
            )
        return self._remember_choice(self._build_template_edit_response(request, result))

    def _try_answer_readonly_query(
        self, request: AgentRequest, definition: ReportDefinitionDraft
    ) -> AgentResponse | None:
        """能用确定性证据回答的能力查询，直接回答。

        只读查询的三条硬约束（20261022 template-editing react 架构改进方案）：

        1. 不改草稿；
        2. 不产生待确认计划；
        3. 不占「同一项修改最多一轮」的澄清轮次。

        这里只覆盖两类有登记证据的问题：可展示方式、已登记的编译粒度。
        其余能力问题无法用确定性证据回答，交给受约束的 ReAct 查证。
        """
        message = request.message.strip()
        if not message or not self._is_readonly_question(message):
            return None

        scope = ToolScope(
            project_id=request.context.projectId,
            subject_ref=request.context.subjectRef,
            subject_id=request.context.subjectId,
        )
        plan = template_edit_engine.parse(
            message=message,
            definition=definition,
            selected_region_id=self._readonly_target_region(request, definition),
            scope=scope,
        )
        if plan.intent == EditIntent.SUGGEST_VISUALIZATION and not plan.needsClarification:
            options = "、".join(
                str(item["label"])
                for item in plan.operation.displayOptions
                if item.get("label")
            )
            # 回答要指明说的是哪一块：用户问的是「某张卡片能不能换个画法」。
            target = plan.target.matchedText or "当前卡片"
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.USER_EDITING_TEMPLATE,
                reply=(
                    f"「{target}」当前可用的展示方式：{options or '未登记'}。"
                    "这是这块卡片当前数据形态支持的方式；草稿没有变化，"
                    "也没有生成待接受的修改计划。要换成其中某一种，说一声即可。"
                ),
                designSessionId=request.context.designSessionId,
                domainObjects=AgentResponseDomainObjects(
                    reportPolicyDraft=request.snapshots.reportPolicy,
                    reportDefinitionDraft=definition,
                ),
                debug={
                    "editOutcome": "readonly_answer",
                    "editPlanSource": "rule_readonly",
                    "editingTargetRegionId": plan.target.regionId or None,
                },
            )
        if plan.intent in {
            EditIntent.SUGGEST_VISUALIZATION,
            EditIntent.CHANGE_COMPILATION_GRAIN,
        } and not plan.target.regionId:
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.USER_EDITING_TEMPLATE,
                reply="请指出要查询的正文卡片或数据源，我才能核对其能力；草稿没有变化。",
                designSessionId=request.context.designSessionId,
                domainObjects=AgentResponseDomainObjects(
                    reportPolicyDraft=request.snapshots.reportPolicy,
                    reportDefinitionDraft=definition,
                ),
                debug={"editOutcome": "readonly_answer"},
            )
        if (
            plan.intent == EditIntent.CHANGE_COMPILATION_GRAIN
            and plan.operation.supportedGrains
        ):
            # 「能按哪些粒度编译」是问能力，不是要求改粒度；
            # 「这个能按分钟看吗」也是问能力：即使点名了粒度，
            # 用户也只是在问行不行，不是让改。
            return self._build_readonly_grain_answer(request, plan)
        return None

    @staticmethod
    def _is_readonly_question(message: str) -> bool:
        """这句是在问，还是在下指令。

        疑问词只是必要条件：「改成按分钟粒度，可以吗」带着问号，
        但用户是在下指令。有明确改动动词的，一律按指令处理。
        """
        if any(word in message for word in EDIT_COMMAND_WORDS):
            return False
        return any(word in message for word in READONLY_QUESTION_WORDS)

    def _readonly_target_region(
        self, request: AgentRequest, definition: ReportDefinitionDraft
    ) -> str | None:
        """只读查询要回答哪张卡：精确名称 / 选中卡片优先，序数其次。"""
        selected_region_id = request.context.selectedRegionId
        reference = resolve_visible_reference(
            request.message,
            definition.regions,
            visible_region_ids=request.context.visibleRegionIds,
            selected_region_id=selected_region_id,
        )
        if reference.status == "RESOLVED" and self._reference_is_reliable(
            reference, request.context
        ):
            return reference.regionId
        return selected_region_id

    @staticmethod
    def _build_readonly_grain_answer(
        request: AgentRequest, plan: EditPlan
    ) -> AgentResponse:
        """回答「这个数据能按哪些粒度编译」：给登记事实，不改草稿。"""
        labels = {"MINUTE": "分钟", "HOUR": "小时", "DAY": "天"}
        grains = list(plan.operation.supportedGrains or [])
        source_ref = plan.operation.sourceRef or "当前数据源"
        requested = (plan.operation.grain or {}).get("unit")
        verdict = (
            f"当前数据源{'支持' if requested in grains else '不支持'}"
            f"{labels.get(requested, requested)}粒度。"
            if requested else ""
        )
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.USER_EDITING_TEMPLATE,
            reply=(
                f"{verdict}当前数据源「{source_ref}」登记支持的编译粒度："
                f"{'、'.join(labels.get(grain, grain) for grain in grains) or '未登记'}。"
                "这是能力登记里的事实，草稿没有变化；"
                "要改成哪个粒度，直接告诉我即可。"
            ),
            designSessionId=request.context.designSessionId,
            domainObjects=AgentResponseDomainObjects(
                reportPolicyDraft=request.snapshots.reportPolicy,
                reportDefinitionDraft=request.snapshots.reportDefinition,
            ),
            debug={
                "editOutcome": "readonly_answer",
                "editPlanSource": "rule_readonly",
                "editingIntent": plan.intent.value,
                "editingTargetRegionId": plan.target.regionId or None,
                "supportedGrains": grains,
            },
        )

    # ---------- 意图识别门（工单 #58） ----------

    def _known_metric_names(self, request: AgentRequest) -> list[str]:
        """当前授权范围内已登记的指标名（供目录外新增判定用）。"""
        try:
            metrics = filter_visible(
                list_metrics(),
                project_id=request.context.projectId,
                subject_ref=request.context.subjectRef,
                subject_id=request.context.subjectId,
            )
        except Exception:  # noqa: BLE001
            logger.exception("[agent.chat] step=metric_scope_lookup_failed")
            return []
        names: list[str] = []
        for item in metrics:
            for key in ("label", "metricRef"):
                value = str(item.get(key) or "").strip()
                if value:
                    names.append(value)
        return names

    def _classify_intent(self, request: AgentRequest) -> UserIntent:
        """编辑态的规则层意图判定（工单 #58）。

        意图识别必须排在任何会产生编辑候选的规则之前，所以这里算出来的
        意图会盖过规则快路自己判出来的操作 —— 规则自设的高置信度
        （时间窗 0.88）不能替代意图判断。
        """
        message = (request.message or "").strip()
        if not message:
            return UserIntent.UNKNOWN
        return classify_message_intent(
            message,
            known_metrics=self._known_metric_names(request),
            # 编辑态里「对象」就是当前这张草稿，不要求句子里点名业务对象。
            subject_required=False,
        )

    async def _handle_intent_before_edit(
        self, request: AgentRequest, intent: UserIntent
    ) -> AgentResponse | None:
        """意图识别门：排在任何会产生编辑候选的规则之前。

        工单 #58（规格第 45 条）：这一道门必须同时覆盖**确定性编辑快路**
        与**模型编辑支路**。咨询、问题反馈、目录外新增三类都不产生
        待接受的修改计划，也不改定义。

        只在会产生编辑候选的编辑态生效（WAITING_USER_CONFIRM /
        USER_EDITING_TEMPLATE）。新建流程（文字方案阶段）有自己的意图
        判定与回答契约，不走这一道门。
        """
        if request.state not in {
            AgentState.WAITING_USER_CONFIRM,
            AgentState.USER_EDITING_TEMPLATE,
        }:
            return None
        if self._is_pure_delegation(request.message):
            # 「你自己决定 / 别问了」：用户已经明说不要再问。
            # 这一句不能被当成提问（去回答「好」）、也不能被当成修改指令。
            # 停止阻塞、给可见假设、**不代用户接受**（规格第 16 条）。
            return self._delegated_response(
                request, request.snapshots.reportDefinition
            )
        if intent is UserIntent.ISSUE_FEEDBACK:
            return self._analyze_issue_feedback(request)
        if intent is UserIntent.ASK_QUESTION:
            definition = request.snapshots.reportDefinition
            if definition is not None:
                # 能用确定性证据答的（有登记的展示方式 / 编译粒度）直接答。
                readonly = self._try_answer_readonly_query(request, definition)
                if readonly is not None:
                    return readonly
            route = RouteDecision(
                userIntent=UserIntent.ASK_QUESTION,
                routingAction=RoutingAction.CONTINUE_CURRENT,
                engineStage="intent_rule",
                reason="规则层判定为咨询：走只读事实查证与回答契约",
                collectedInfo=dict(request.collectedInfo),
            )
            return await self._handle_readonly_query(request, route, None)
        return None

    def _issue_analysis_region(
        self, request: AgentRequest, definition: ReportDefinitionDraft
    ) -> dict[str, Any] | None:
        """问题反馈说的是哪一张卡片。

        先按用户**看到的**可见顺序 / 精确名称定位；定位不到再退回
        名称与角色的模糊匹配。定位不到就是「证据不足」，不能猜。
        """
        reference = resolve_visible_reference(
            request.message,
            definition.regions,
            visible_region_ids=request.context.visibleRegionIds,
            selected_region_id=request.context.selectedRegionId,
        )
        if reference.status == "RESOLVED" and self._reference_is_reliable(
            reference, request.context
        ):
            region = next(
                (
                    item
                    for item in (definition.regions or [])
                    if item.get("id") == reference.regionId
                ),
                None,
            )
            if region is not None:
                return region
        return template_edit_engine.resolve_target_region(
            message=request.message,
            definition=definition,
            selected_region_id=request.context.selectedRegionId,
        )

    def _rendered_point_count(
        self, definition: ReportDefinitionDraft, region_id: str
    ) -> int | None:
        """渲染载荷里这张卡片实际渲染出多少个数据点。

        只取**点数**，不取任何业务数值：模拟载荷只在内存里读，
        不进日志、不进会话库（规格第 53 条）。
        """
        try:
            _, record = preview_service.run_preview(
                definitionId=str(
                    (definition.metadata or {}).get("name") or "current_definition"
                ),
                definitionDraft=definition.model_dump(mode="json"),
            )
        except Exception:  # noqa: BLE001
            logger.exception("[agent.chat] step=issue_analysis_preview_failed")
            return None
        for item in record.regions or []:
            if item.get("regionId") != region_id:
                continue
            payload = item.get("payload") or {}
            points = payload.get("points")
            if isinstance(points, list):
                return len(points)
            rows = payload.get("rows")
            if isinstance(rows, list):
                return len(rows)
            return None
        return None

    def _analyze_issue_feedback(
        self, request: AgentRequest
    ) -> AgentResponse:
        """问题反馈：读实际定义、窗口、粒度与渲染载荷，解释并给建议。

        工单 #58：「昨日能耗趋势好丑啊，一个点……」不产生任何修改计划，
        改走问题分析。四条硬约束：

        1. 不改定义、不产生待接受计划；
        2. 走只读契约（query_answer）：业务阶段、待确认计划都不动；
        3. 证据不足就说证据不足，或给一个针对性的澄清；
        4. **不凭关键词指定修改值** —— 不因为句子里有「昨日」就把时间窗
           写成昨天（卡片名里的时间词只用来定位对象）。
        """
        definition = request.snapshots.reportDefinition
        if definition is None:
            return self._issue_analysis_response(
                request,
                analyze_region(definition=ReportDefinitionDraft(), region=None),
            )
        region = self._issue_analysis_region(request, definition)
        rendered = (
            self._rendered_point_count(definition, str(region.get("id") or ""))
            if region is not None
            else None
        )
        analysis = analyze_region(
            definition=definition, region=region, rendered_points=rendered
        )
        return self._issue_analysis_response(request, analysis)

    def _issue_analysis_response(
        self, request: AgentRequest, analysis: IssueAnalysis
    ) -> AgentResponse:
        """把问题分析收敛成「不推进业务」的只读回答。"""
        reply = analysis.summary
        if not analysis.evidence_sufficient:
            reply = (
                f"{reply}\n请告诉我是哪一张卡片、哪里看着不对"
                "（比如「趋势图只有一根线」），我按实际定义给你核对。"
            )
        return AgentResponse(
            conversationId=request.conversationId,
            state=request.state,
            turnKind=TURN_KIND_QUERY_ANSWER,
            toolRequests=[],
            reply=reply,
            designSessionId=request.context.designSessionId,
            domainObjects=AgentResponseDomainObjects(
                reportDefinitionDraft=request.snapshots.reportDefinition,
                reportPolicyDraft=request.snapshots.reportPolicy,
            ),
            questions=self._preserved_menu(request, None),
            uiActions=["query_answer"],
            debug={
                "queryMode": True,
                "userIntent": UserIntent.ISSUE_FEEDBACK.value,
                "editOutcome": "issue_analysis",
                "issueAnalysis": {
                    "regionId": analysis.region_id or None,
                    "targetLabel": analysis.target_label,
                    "windowLabel": analysis.window_label,
                    "grainLabel": analysis.grain_label,
                    "expectedPoints": analysis.expected_points,
                    "renderedPoints": analysis.rendered_points,
                    "evidenceSufficient": analysis.evidence_sufficient,
                },
            },
        )

    async def _try_handle_template_edit(
        self, request: AgentRequest
    ) -> AgentResponse | None:
        """明确规则走确定性快路径；不明确的编辑交给业务 Agent 查证。"""
        if request.state not in {
            AgentState.WAITING_USER_CONFIRM,
            AgentState.USER_EDITING_TEMPLATE,
        }:
            return None

        message = request.message.strip()
        if not message:
            return None

        # USER_EDITING_TEMPLATE 下的三按钮：同意 / 重新开始 / 继续修改
        if request.state == AgentState.USER_EDITING_TEMPLATE:
            if message == CONFIRM_BUTTON_VALUE:
                return self._save_placeholder(request, RouteDecision())
            if message == RESTART_BUTTON_VALUE:
                return AgentResponse(
                    conversationId=request.conversationId,
                    state=AgentState.ANALYZING,
                    reply="好的，我们重新开始。请描述你想生成的新报表。",
                    designSessionId=request.context.designSessionId,
                    uiActions=["restart_from_template_editing"],
                )
            if message == MODIFY_BUTTON_VALUE:
                # 用户选"继续修改"：回到追问
                return self._editing_prompt_response(
                    request,
                    reply="好的，请告诉我接下来想怎么改：删除、改标题、改指标、改数据源、改展示方式、改时间范围或 compilation 粒度？",
                )

        definition = request.snapshots.reportDefinition
        if definition is None:
            return None

        # 入口判断：WAITING_USER_CONFIRM 下只有命中编辑关键词才进入；USER_EDITING_TEMPLATE 下所有非按钮都进入
        in_editing = request.state == AgentState.USER_EDITING_TEMPLATE
        hinted = any(word in message.lower() for word in TEMPLATE_EDIT_HINT_WORDS) or message == MODIFY_BUTTON_VALUE
        if not in_editing and not hinted:
            return None

        # 只读能力查询：能用确定性证据答的就直接答。
        # 不改草稿、不产生待确认计划，也不占修改澄清轮次。
        readonly = self._try_answer_readonly_query(request, definition)
        if readonly is not None:
            return readonly

        # 用户说「你自己决定 / 别问了」：不再追问，但仍要把能做的改完。
        delegated = self._is_delegated_decision(message)

        prior = request.collectedInfo.get("pendingEditRequest")
        continuing = bool(prior) and not any(
            word in message for word in ("删除", "删掉", "改成", "改为", "换成", "新增", "取消")
        )
        effective_message = f"{prior} {message}" if continuing else message
        reference = resolve_visible_reference(
            effective_message,
            definition.regions,
            visible_region_ids=request.context.visibleRegionIds,
            selected_region_id=request.context.selectedRegionId,
        )
        selected_region_id = request.context.selectedRegionId
        if reference.status != "NOT_FOUND":
            if reference.status != "RESOLVED":
                # 指代词没选中对象、序数越界：先问清楚是哪一张。
                return self._clarification_or_delegated(
                    request, definition,
                    reference.question or "请指出要改的正文卡片名称。",
                    intent="UNKNOWN", delegated=delegated,
                )
            if not self._reference_is_reliable(reference, request.context):
                return self._clarification_or_delegated(
                    request, definition,
                    "当前页面卡片顺序无法核对，请指出要改的卡片名称。",
                    intent="UNKNOWN", delegated=delegated,
                )
            selected_region_id = reference.regionId

        anchor = request.collectedInfo.get("pendingEditTimeAnchor")
        logical_now = (
            datetime.fromisoformat(anchor)
            if isinstance(anchor, str)
            else datetime.now(ZoneInfo("Asia/Shanghai"))
        )
        # 工单 #58：时间窗的新值只来自**明确的改动动词片段**，
        # 或者「有效的澄清答案」（整句就是新值）。
        #
        # 没有改动动词时整句不是新值 —— 卡片名里的「昨日」只用来定位对象。
        # 旧实现在这里把整句送进时间解析，于是「昨日能耗趋势好丑啊，一个点……」
        # 被日锚点判成「已解析」，问题被静默吞掉。
        change_value = extract_change_value(effective_message)
        if not change_value and prior:
            # 上一轮在追问，这句是澄清答案：整句就是用户给的新值。
            change_value = effective_message
        resolved_time = resolve_time_expression(
            change_value, logical_now=logical_now
        )
        if resolved_time.status == "NEEDS_CLARIFICATION":
            request.collectedInfo["pendingEditTimeAnchor"] = logical_now.isoformat()
            choices = [
                QuestionOption(
                    value=effective_message.replace(
                        resolved_time.matchedText,
                        f"{resolved_time.matchedText} {option.label.split()[-1]}",
                        1,
                    ),
                    label=option.label,
                )
                for option in resolved_time.options
            ]
            return self._clarification_or_delegated(
                request, definition, resolved_time.question, choices=choices,
                intent=EditIntent.CHANGE_TIME_RANGE.value,
                region_id=selected_region_id or "",
                delegated=delegated,
            )

        scope = ToolScope(
            project_id=request.context.projectId,
            subject_ref=request.context.subjectRef,
            subject_id=request.context.subjectId,
        )
        plan = template_edit_engine.parse(
            message=effective_message,
            definition=definition,
            selected_region_id=selected_region_id,
            scope=scope,
            change_value=change_value,
        )
        if (
            plan.intent == EditIntent.CHANGE_DATA_SOURCE
            and re.search(r"按(?:分钟|小时|天)|(?:分钟|小时|天|日)级", effective_message)
        ):
            grain_unit = template_edit_engine._parse_grain(effective_message)
            composite = (
                template_edit_engine.plan_source_and_grain_change(
                    definition=definition,
                    region_id=plan.target.regionId,
                    grain_unit=grain_unit,
                    source_ref=plan.operation.sourceRef,
                    scope=scope,
                )
                if grain_unit and plan.target.regionId and plan.operation.sourceRef
                else None
            )
            if composite is None:
                return self._clarification_or_delegated(
                    request, definition,
                    "指定的数据源无法同时满足该卡片的指标字段和目标粒度；"
                    "本次没有修改草稿，请重新指定数据源或粒度。",
                    intent=EditIntent.CHANGE_COMPILATION_GRAIN.value,
                    region_id=plan.target.regionId,
                    delegated=delegated,
                )
            composite.confidence = plan.confidence
            plan = composite
        if (
            resolved_time.status == "RESOLVED"
            and plan.intent == EditIntent.CHANGE_TIME_RANGE
            and not plan.needsClarification
        ):
            plan.operation.timeWindow = resolved_time.timeWindow
            plan.operation.timeWindowLabel = resolved_time.label
        elif (
            resolved_time.status == "RESOLVED"
            and (resolved_time.timeWindow or {}).get("value", {}).get("type")
            == "FIXED_INTERVAL"
        ):
            # 明确钟点区间比「天」这类粒度误判更可信：
            # 用户给了起止时间，改的就是时间窗，走 FIXED_INTERVAL 协议。
            plan.intent = EditIntent.CHANGE_TIME_RANGE
            plan.needsClarification = not plan.target.regionId
            plan.clarificationQuestion = (
                "" if not plan.needsClarification
                else "你想把哪个 section 的时间范围改成这个具体时间段？"
            )
            if not plan.needsClarification:
                plan.operation.timeWindow = resolved_time.timeWindow
                plan.operation.timeWindowLabel = resolved_time.label
        if (
            plan.intent == EditIntent.CHANGE_COMPILATION_GRAIN
            and plan.needsClarification
            and plan.target.regionId
            and (plan.operation.grain or {}).get("unit")
        ):
            composite = template_edit_engine.plan_source_and_grain_change(
                definition=definition,
                region_id=plan.target.regionId,
                grain_unit=plan.operation.grain["unit"],
                scope=scope,
            )
            if composite is not None:
                composite.confidence = plan.confidence
                plan = composite
        if plan.intent == EditIntent.SUGGEST_VISUALIZATION:
            return self._build_template_edit_response(
                request, template_edit_engine.apply(
                    plan=plan, definition=definition, scope=scope
                )
            )
        if plan.intent == EditIntent.UNKNOWN or plan.needsClarification or plan.confidence < 0.7:
            if delegated:
                # 用户说了「别问了」：不再追问，改成可见假设 + 可选替代。
                # **不猜目标、不造空计划**（规格第 16、18 条）。
                return self._delegated_response(request, definition)
            if isinstance(self._agent, FakeReportAgent):
                if plan.needsClarification or plan.intent == EditIntent.UNKNOWN:
                    result = template_edit_engine.apply(
                        plan=plan, definition=definition, scope=scope
                    )
                    response = self._build_template_edit_response(request, result)
                    response.debug["editPlanSource"] = "rule_no_model"
                    return response
                return self._edit_clarification_response(
                    request, plan.clarificationQuestion or "请再明确目标和修改内容。",
                    intent=plan.intent.value,
                    region_id=plan.target.regionId,
                )
            request.state = AgentState.USER_EDITING_TEMPLATE
            return None
        operation_steps = (
            [step.model_dump(mode="json") for step in plan.operations]
            if plan.operations else [{
                "intent": plan.intent.value,
                "operation": plan.operation.model_dump(mode="json", exclude_none=True),
                "summary": self._describe_rule_edit(plan, definition),
            }]
        )
        summary = plan.summary or self._describe_rule_edit(plan, definition)
        # 用户说了「你自己决定」：候选照出，但假设写在明面上，
        # 而且仍然要他自己点「接受修改」—— **不代用户接受**（规格第 16 条）。
        delegated_note = (
            f"\n（你让我自己决定，我按下面这个假设出的候选："
            f"{self._delegated_assumption(request, definition)}）"
            if delegated else ""
        )
        candidate = {
            "planId": f"plan_{uuid.uuid4().hex}",
            "draftRevision": self._draft_revision(definition),
            "targetRegionId": plan.target.regionId,
            "operations": operation_steps,
            "summary": summary,
            "editPlan": plan.model_dump(mode="json"),
        }
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.USER_EDITING_TEMPLATE,
            reply=(
                f"待确认的修改：{summary}\n接受后才会校验并修改草稿。"
                f"{delegated_note}"
            ),
            designSessionId=request.context.designSessionId,
            domainObjects=AgentResponseDomainObjects(
                reportDefinitionDraft=definition,
                reportPolicyDraft=request.snapshots.reportPolicy,
            ),
            questions=[self._pending_edit_question()],
            uiActions=["pending_edit_plan"],
            debug={
                "editOutcome": "candidate",
                "editPlanSource": "rule",
                "editingTargetRegionId": plan.target.regionId or None,
                "editingIntent": plan.intent.value,
                "pendingEditPlan": candidate,
            },
        )

    @staticmethod
    def _describe_rule_edit(
        plan: EditPlan, definition: ReportDefinitionDraft
    ) -> str:
        label = plan.target.matchedText or (definition.metadata or {}).get("name") or "当前报表"
        operation = plan.operation
        action = {
            EditIntent.RENAME_REPORT: "修改报表名称",
            EditIntent.DELETE_SECTION: "删除卡片",
            EditIntent.RENAME_SECTION: "修改卡片标题",
            EditIntent.ADD_SECTION: "新增卡片",
            EditIntent.CHANGE_DATA_SOURCE: "更换数据源",
            EditIntent.CHANGE_COMPILATION_GRAIN: "修改编译粒度",
            EditIntent.CHANGE_TIME_RANGE: "修改时间窗",
            EditIntent.CHANGE_METRIC: "更换指标",
            EditIntent.CHANGE_VISUALIZATION: "更换展示形式",
            EditIntent.CHANGE_TOP_N: "修改排名数量",
            EditIntent.CHANGE_SORT: "修改排序",
        }.get(plan.intent, "修改")
        details = [
            f"{name}：{value}"
            for name, value in (
                ("新标题", operation.newTitle),
                ("数据源", operation.sourceRef),
                ("指标", operation.metricRef),
                ("粒度", (operation.grain or {}).get("unit")),
                ("时间窗", operation.timeWindowLabel),
                ("排名数量", operation.topN),
                ("展示形式", operation.chartKind or operation.componentType),
            )
            if value is not None and value != ""
        ]
        suffix = "；".join(details)
        return (
            f"对「{label}」{action}" + (f"（{suffix}）" if suffix else "") + "。"
            "当前预览仍为 Mock 数据，尚未真实编译。"
        )

    @staticmethod
    def _edit_clarification_response(
        request: AgentRequest, question: str, *,
        choices: list[QuestionOption] | None = None,
        intent: str = "UNKNOWN",
        region_id: str = "",
    ) -> AgentResponse:
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.USER_EDITING_TEMPLATE,
            reply=question,
            designSessionId=request.context.designSessionId,
            domainObjects=AgentResponseDomainObjects(
                reportDefinitionDraft=request.snapshots.reportDefinition,
                reportPolicyDraft=request.snapshots.reportPolicy,
            ),
            questions=[AgentQuestion(
                id="edit_time_clarification" if choices else "edit_reference_clarification",
                prompt=question,
                expects="SINGLE_CHOICE" if choices else "TEXT",
                options=choices or [],
                selectionOnly=bool(choices),
            )],
            uiActions=["ask_template_edit_detail"],
            debug={
                "editOutcome": "clarification",
                "editingIntent": intent,
                "editingTargetRegionId": region_id or None,
            },
        )

    def _build_template_edit_response(
        self, request: AgentRequest, result: EditResult
    ) -> AgentResponse:
        """把 TemplateEditEngine 的 EditResult 包成 AgentResponse。"""
        intent_value = result.plan.intent.value
        debug: dict[str, Any] = {
            "editingTargetRegionId": result.editingTargetRegionId,
            "editingIntent": intent_value,
            "editPlan": result.plan.model_dump(mode="json"),
        }
        if result.markdownPreview:
            debug["markdownPreview"] = result.markdownPreview
        debug.update(result.debug or {})

        # 需要追问：保留当前草稿，不写 ChangeSet
        if result.needsClarification:
            questions = self._editing_prompt_questions()
            if (
                result.plan.intent == EditIntent.CHANGE_COMPILATION_GRAIN
                and result.plan.operation.supportedGrains
            ):
                grain_labels = {
                    "MINUTE": "分钟",
                    "HOUR": "小时",
                    "DAY": "天",
                }
                questions = [
                    AgentQuestion(
                        id="compilation_grain",
                        prompt="请选择当前数据源支持的粒度：",
                        expects="SINGLE_CHOICE",
                        options=[
                            QuestionOption(
                                value=f"按{grain_labels.get(grain, grain)}粒度",
                                label=grain_labels.get(grain, grain),
                            )
                            for grain in result.plan.operation.supportedGrains
                        ],
                        selectionOnly=True,
                    )
                ]
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.USER_EDITING_TEMPLATE,
                reply=result.clarificationQuestion
                or result.reply
                or "请再明确一下你想怎么修改。",
                designSessionId=request.context.designSessionId,
                domainObjects=AgentResponseDomainObjects(
                    reportPolicyDraft=request.snapshots.reportPolicy,
                    reportDefinitionDraft=result.draftBeforeEdit
                    or request.snapshots.reportDefinition,
                ),
                questions=questions,
                uiActions=["template_editing", "ask_template_edit_detail"],
                debug=debug,
            )

        # SUGGEST_VISUALIZATION：只给候选，不改草稿
        if result.plan.intent == EditIntent.SUGGEST_VISUALIZATION:
            option_values = [
                str(opt.get("label"))
                for opt in result.plan.operation.displayOptions
                if opt.get("label")
            ]
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.USER_EDITING_TEMPLATE,
                reply=result.reply or "以下是可选的展示方式：",
                designSessionId=request.context.designSessionId,
                domainObjects=AgentResponseDomainObjects(
                    reportPolicyDraft=request.snapshots.reportPolicy,
                    reportDefinitionDraft=result.draftBeforeEdit
                    or request.snapshots.reportDefinition,
                ),
                questions=[
                    AgentQuestion(
                        id="suggest_visualization",
                        prompt="请选择展示方式：",
                        expects="SINGLE_CHOICE",
                        options=[
                            QuestionOption(value=val, label=val) for val in option_values
                        ]
                        or [QuestionOption(value="取消", label="取消")],
                        selectionOnly=True,
                    )
                ],
                uiActions=["template_editing", "suggest_visualization"],
                debug=debug,
            )

        # 正常应用成功：返回新草稿 + ChangeSet + 三按钮
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.USER_EDITING_TEMPLATE,
            reply=result.reply or "已更新草稿，请确认。",
            designSessionId=request.context.designSessionId,
            domainObjects=AgentResponseDomainObjects(
                reportPolicyDraft=request.snapshots.reportPolicy,
                reportDefinitionDraft=result.draftAfterEdit,
                changeSet=result.changeSet,
            ),
            questions=self._editing_confirm_questions(),
            uiActions=["template_edit_applied", "confirm_template_edit"],
            debug=debug,
        )

    def _editing_prompt_response(
        self, request: AgentRequest, *, reply: str
    ) -> AgentResponse:
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.USER_EDITING_TEMPLATE,
            reply=reply,
            designSessionId=request.context.designSessionId,
            domainObjects=AgentResponseDomainObjects(
                reportPolicyDraft=request.snapshots.reportPolicy,
                reportDefinitionDraft=request.snapshots.reportDefinition,
            ),
            questions=self._editing_prompt_questions(),
            uiActions=["template_editing", "ask_template_edit_detail"],
            debug={"editingIntent": "UNKNOWN"},
        )

    @staticmethod
    def _editing_prompt_questions() -> list[AgentQuestion]:
        # 「已选修改 / 继续调整」之后的**统一对话**入口：这里不锁，
        # 用户可以一起问、一起提反馈、一起改（规格第 58 条）。
        return [
            AgentQuestion(
                id="template_edit_action",
                prompt="请选择或直接输入修改要求：",
                expects="SINGLE_CHOICE",
                lockScope=LOCK_SCOPE_NONE,
                options=[
                    QuestionOption(value="删除这个 section", label="删除这个 section"),
                    QuestionOption(value="改标题", label="改标题"),
                    QuestionOption(value="改指标", label="改指标"),
                    QuestionOption(value="改数据源", label="改数据源"),
                    QuestionOption(value="改展示方式", label="改展示方式"),
                    QuestionOption(value="改时间范围", label="改时间范围"),
                    QuestionOption(value="改 compilation 粒度", label="改 compilation 粒度"),
                ],
            )
        ]

    @staticmethod
    def _editing_confirm_questions() -> list[AgentQuestion]:
        # 「确认编辑后的草稿」是确认态，不是「等待下一步操作选择」：
        # 输入框保持可用，用户在确认前仍能问一句（契约表 §5.2 的 menu_only）。
        return [
            AgentQuestion(
                id="template_edit_confirm",
                prompt="请确认当前编辑后的草稿：",
                expects="SINGLE_CHOICE",
                lockScope=LOCK_SCOPE_MENU_ONLY,
                options=[
                    QuestionOption(value=CONFIRM_BUTTON_VALUE, label="同意"),
                    QuestionOption(value=MODIFY_BUTTON_VALUE, label="修改"),
                    QuestionOption(value=RESTART_BUTTON_VALUE, label="重新开始"),
                ],
                selectionOnly=True,
            )
        ]

    @staticmethod
    def _protocol_error(
        request: AgentRequest, *, code: str, message: str
    ) -> AgentResponse:
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.BLOCKED_OR_UNSUPPORTED,
            reply=message,
            designSessionId=request.context.designSessionId,
            error=AgentError(code=code, message=message),
        )

    def _apply_context_route(
        self, request: AgentRequest, route: RouteDecision
    ) -> AgentResponse | None:
        """执行 ContextEngine 的路由决策。

        返回 None 表示继续交给业务 Agent；返回 AgentResponse 表示已短路处理。
        """
        action = route.routingAction
        log_timeline_step("apply_context_route")
        logger.info(
            "[agent.chat] step=apply_context_route conversationId=%s action=%s target=%s",
            request.conversationId,
            action.value,
            route.targetState.value if route.targetState else None,
        )

        if action == RoutingAction.CONTINUE_CURRENT:
            return None

        if action == RoutingAction.JUMP_STATE:
            target = route.targetState or AgentState.ANALYZING
            if target == AgentState.ANALYZING:
                request.state = AgentState.ANALYZING
                return None
            if target == AgentState.SAVE_DEFINITION_DRAFT:
                return self._save_placeholder(request, route)
            request.state = target
            return None

        if action == RoutingAction.ASK_INTERRUPT_CONFIRMATION:
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.INTERRUPT_CONFIRMING,
                reply=route.reply or "当前流程还在进行中。你确定要打断并重新开始吗？",
                designSessionId=request.context.designSessionId,
                questions=[
                    AgentQuestion(
                        id="interrupt_confirm",
                        prompt="是否确认打断当前流程？",
                        expects="SINGLE_CHOICE",
                        options=[
                            QuestionOption(value="确认，重新开始", label="确认，重新开始"),
                            QuestionOption(value="取消，继续当前流程", label="取消，继续当前流程"),
                        ],
                    )
                ],
                uiActions=["confirm_interrupt", "cancel_interrupt"],
            )

        if action == RoutingAction.ANSWER_IN_PLACE:
            return AgentResponse(
                conversationId=request.conversationId,
                state=request.state,
                reply=route.reply or "我在。你可以继续告诉我是要确认、修改、重做还是提问。",
                designSessionId=request.context.designSessionId,
                questions=self._suggestion_questions(request),
                uiActions=["suggest_prompt"],
            )

        if action == RoutingAction.ASK_CLARIFICATION:
            question = route.nextQuestion or route.reply or "请再补充一下你想看什么对象、时间范围和指标。"
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.NEEDS_CLARIFICATION,
                reply=question,
                designSessionId=request.context.designSessionId,
                questions=[
                    AgentQuestion(
                        id="context_missing_info",
                        prompt=question,
                        expects="TEXT",
                        relatesTo=",".join(route.missingInfo) if route.missingInfo else None,
                    )
                ],
                uiActions=["ask_clarification"],
            )

        if action == RoutingAction.BLOCK:
            return self._blocked_by_route(request, route)

        return None

    def _blocked_by_route(
        self, request: AgentRequest, route: RouteDecision
    ) -> AgentResponse:
        """路由层明确阻断：越权范围 / 未登记指标。

        必须给前端可展示的 blocks（code / message / alternatives），
        而不是只回一个协议错误 —— 否则用户看到的是「操作失败」，
        不知道到底哪里越权、哪个指标没登记。
        """
        message = route.blockMessage or route.reply or "这个操作当前不支持。"
        reply = route.reply or message
        code = route.blockCode or "OUT_OF_SCOPE"
        logger.info(
            "[agent.chat] step=blocked_by_route conversationId=%s code=%s message=%r",
            request.conversationId,
            code,
            message,
        )
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.BLOCKED_OR_UNSUPPORTED,
            reply=reply,
            designSessionId=request.context.designSessionId,
            blocks=[
                AgentBlock(
                    code=code,
                    message=message,
                    alternatives=list(route.blockAlternatives),
                )
            ],
            error=AgentError(code="BLOCKED_BY_CONTEXT_ENGINE", message=reply),
            uiActions=["continue_editing"],
        )

    def _save_placeholder(
        self, request: AgentRequest, route: RouteDecision
    ) -> AgentResponse:
        """用户确认草稿后真正落库。

        数据来源优先级：
            1. 请求 snapshots.reportDefinition / reportPolicy（前端带回当前草稿）；
            2. 没有的话返回错误，提示先重新生成。
        """
        policy = request.snapshots.reportPolicy
        definition = request.snapshots.reportDefinition
        if policy is None or definition is None:
            logger.warning(
                "[agent.chat] step=save_missing_draft conversationId=%s",
                request.conversationId,
            )
            return self._protocol_error(
                request,
                code="SAVE_MISSING_DRAFT",
                message="没有可保存的草稿，请先重新生成报表草稿再确认。",
            )

        saved = definition_save_service.save(
            definitionId=request.context.reportDefinitionId,
            policyDraft=policy.model_dump(mode="json"),
            definitionDraft=definition.model_dump(mode="json"),
        )
        if definition.metadata.get("templateBinding"):
            definition = definition.model_copy(deep=True)
            definition.metadata.update({"definitionId": saved.definitionId, "revision": saved.revision})
            saved.definitionDraft = definition.model_dump(mode="json")
        logger.info(
            "[agent.chat] step=save_done conversationId=%s definitionId=%s revision=%s",
            request.conversationId,
            saved.definitionId,
            saved.revision,
        )

        reply = (
            route.reply
            or f"已保存草稿（{saved.definitionId}，revision={saved.revision}），"
            "下一步进入后端校验。"
        )
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.SAVE_DEFINITION_DRAFT,
            reply=reply,
            designSessionId=request.context.designSessionId,
            domainObjects=AgentResponseDomainObjects(
                reportPolicyDraft=policy,
                reportDefinitionDraft=definition,
            ),
            toolRequests=[
                ToolRequest(
                    id="req_save_confirmed",
                    type="SAVE_DEFINITION_DRAFT",
                    target="JavaReportService",
                    arguments={
                        "definitionId": saved.definitionId,
                        "revision": saved.revision,
                    },
                ),
                ToolRequest(
                    id="req_validate_after_save",
                    type="VALIDATE_DEFINITION",
                    target="JavaReportService",
                    arguments={
                        "definitionId": saved.definitionId,
                        "revision": saved.revision,
                    },
                ),
            ],
            uiActions=["save_definition_draft", "advance_to_validating"],
        )

    # ---------- 节点 6-11 自动推进 ----------

    def _auto_advance(self, request: AgentRequest) -> AgentResponse | None:
        """前端发来 __advance__ 时，根据当前 state 走对应 handler。"""
        state = request.state
        if state == AgentState.SAVE_DEFINITION_DRAFT:
            return self._handle_save_to_validating(request)
        if state == AgentState.VALIDATING:
            return self._handle_validating(request)
        if state == AgentState.PREVIEWING:
            return self._handle_previewing(request)
        if state == AgentState.PREVIEW_READY:
            return self._handle_preview_ready(request)
        if state == AgentState.RENDERING:
            return self._handle_rendering(request)
        return None

    def _handle_save_to_validating(self, request: AgentRequest) -> AgentResponse:
        """节点 6 → 7：SAVE_DEFINITION_DRAFT → VALIDATING（展示 loading）。"""
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.VALIDATING,
            reply="正在校验报表定义（结构 / 字段 / 指标 / 权限 / 能力）…",
            designSessionId=request.context.designSessionId,
            uiActions=["start_validating", "advance_to_validating"],
        )

    def _handle_validating(self, request: AgentRequest) -> AgentResponse:
        """节点 7：VALIDATING → PREVIEWING 或 VALIDATION_FAILED。"""
        definition = request.snapshots.reportDefinition
        definition_dict = (
            definition.model_dump(mode="json") if definition is not None else None
        )
        # 若 snapshot 没带，从内存版 saved 取
        if definition_dict is None and request.context.reportDefinitionId:
            saved = definition_save_service.get(request.context.reportDefinitionId)
            if saved is not None:
                definition_dict = saved.definitionDraft

        result = validate_definition(definition_dict)

        if result.passed:
            reply = "校验通过（结构 / 字段 / 指标 / 权限 / 能力），开始跑预览…"
            return AgentResponse(
                conversationId=request.conversationId,
                state=AgentState.PREVIEWING,
                reply=reply,
                designSessionId=request.context.designSessionId,
                toolRequests=[
                    ToolRequest(
                        id="req_run_preview",
                        type="RUN_PREVIEW",
                        target="JavaPreviewEngine",
                        arguments={
                            "definitionId": request.context.reportDefinitionId,
                            "revision": request.context.baseDefinitionRevision,
                        },
                    )
                ],
                uiActions=["validation_passed", "advance_to_previewing"],
                debug={"validation": result.model_dump(mode="json")},
            )

        # 校验失败
        reply = "校验未通过，有以下问题：\n" + summarize_issues_for_user(result)
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.VALIDATION_FAILED,
            reply=reply,
            designSessionId=request.context.designSessionId,
            uiActions=["validation_failed", "offer_fix"],
            debug={"validation": result.model_dump(mode="json")},
        )

    def _handle_previewing(self, request: AgentRequest) -> AgentResponse:
        """节点 9：PREVIEWING → PREVIEW_READY。"""
        definition = request.snapshots.reportDefinition
        definition_dict = (
            definition.model_dump(mode="json") if definition is not None else None
        )
        definition_id = request.context.reportDefinitionId or "def_unknown"
        if definition_dict is None and request.context.reportDefinitionId:
            saved = definition_save_service.get(request.context.reportDefinitionId)
            if saved is not None:
                definition_dict = saved.definitionDraft

        if definition_dict is None:
            return self._protocol_error(
                request,
                code="PREVIEW_MISSING_DEFINITION",
                message="没有可预览的报表定义，请先保存并校验草稿。",
            )

        preview, record = preview_service.run_preview(
            definitionId=definition_id,
            definitionDraft=definition_dict,
        )

        reply = (
            f"预览已生成（{preview.reportRunId}）。"
            f"{record.snapshot.roomName} {record.snapshot.logicalDate} 总耗电 "
            f"{record.snapshot.totalEnergyKwh} kWh。"
        )
        if (definition_dict.get("metadata") or {}).get("templateBinding"):
            # 占位区域**分两类**说：能力待接通 vs 范围授权（规格 #46 第 246 行）。
            # 授权类不是能力缺失 —— 不能混算成「能力尚未接通」。
            placeholder_note = placeholder_reply(definition_dict.get("regions") or [])
            reply = f"预览已生成（{preview.reportRunId}），按已确认模板的各区域口径展示。"
            if placeholder_note:
                # ADR 0004 之后口径反转：草稿阶段就已经用模拟业务数据渲染，
                # 这里不再说「保留占位、没有替它们生成数值」。
                reply += f"{placeholder_note}。"
            reply += "本次试运行同样使用能耗模拟数据，不是生产事实表。"
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.PREVIEW_READY,
            reply=reply,
            designSessionId=request.context.designSessionId,
            domainObjects=AgentResponseDomainObjects(reportRunPreview=preview),
            uiActions=["preview_ready", "advance_to_rendering"],
        )

    def _handle_preview_ready(self, request: AgentRequest) -> AgentResponse:
        """节点 10：PREVIEW_READY → RENDERING。附带一句 AI 解读。"""
        last_run = request.snapshots.lastReportRun
        run_id = last_run.reportRunId if last_run is not None else None
        analysis = ""
        if run_id:
            payload = preview_service.get_result_payload(run_id)
            if payload is not None:
                for region in payload["regions"]:
                    if region["componentType"] == "ANALYSIS":
                        analysis = region["payload"].get("analysis", "")
                        break

        reply = analysis or "预览结果已就绪，开始渲染报表界面…"
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.RENDERING,
            reply=reply,
            designSessionId=request.context.designSessionId,
            uiActions=["start_rendering", "advance_to_ready"],
        )

    def _handle_rendering(self, request: AgentRequest) -> AgentResponse:
        """节点 11：RENDERING → READY。渲染在前端做，这里只记一下完成。"""
        return AgentResponse(
            conversationId=request.conversationId,
            state=AgentState.READY,
            reply="报表已就绪。你可以继续调整、保存、发布或导出。",
            designSessionId=request.context.designSessionId,
            questions=self._ready_questions(),
            uiActions=["render_done", "show_ready_actions"],
        )

    @staticmethod
    def _ready_questions() -> list[AgentQuestion]:
        # 旧流程的 READY 菜单：契约表 §5.2 没有把它列进「锁输入」的阶段，
        # 保持既有行为（只锁菜单，输入框可用）。
        return [AgentQuestion(
            id="ready_action",
            prompt="请选择下一步操作：",
            expects="SINGLE_CHOICE",
            lockScope=LOCK_SCOPE_MENU_ONLY,
            options=[
                QuestionOption(value="继续修改", label="继续修改"),
                QuestionOption(value="发布", label="发布"),
                QuestionOption(value="导出 PDF", label="导出 PDF"),
            ],
            selectionOnly=True,
        )]

    @staticmethod
    def _suggestion_questions(request: AgentRequest) -> list[AgentQuestion]:
        state = request.state
        has_draft = request.snapshots.reportDefinition is not None
        if state == AgentState.WAITING_USER_CONFIRM and has_draft:
            options = [
                QuestionOption(value=CONFIRM_BUTTON_VALUE, label="同意"),
                QuestionOption(value=MODIFY_BUTTON_VALUE, label="修改"),
                QuestionOption(value=RESTART_BUTTON_VALUE, label="重新开始"),
            ]
            prompt = "请对当前草稿做出选择："
        elif has_draft:
            options = [
                QuestionOption(value="继续修改当前报表", label="继续修改当前报表"),
                QuestionOption(value="重新生成一版报表", label="重新生成一版报表"),
            ]
            prompt = "你可以继续调整当前草稿："
        else:
            options = [
                QuestionOption(value="生成 A 机房昨日能耗日报", label="生成能耗日报"),
                QuestionOption(value="生成 A 机房昨日能耗日报，包含总耗电、设备排名和趋势分析", label="生成完整能耗日报"),
            ]
            prompt = "你可以试试这些："
        return [
            AgentQuestion(
                id="context_suggestions",
                prompt=prompt,
                expects="SINGLE_CHOICE",
                options=options,
            )
        ]


agent_service = AgentService()
