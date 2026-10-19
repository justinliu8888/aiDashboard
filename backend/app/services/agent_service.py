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
from time import perf_counter
from typing import Any, Awaitable, Callable

from app.agents.fake_report_agent import FakeReportAgent
from app.config import get_settings
from app.schemas.agent import (
    ALLOWED_TRANSITIONS,
    AGENT_PROTOCOL_VERSION,
    AgentError,
    AgentQuestion,
    AgentRequest,
    AgentResponse,
    AgentState,
    QuestionOption,
    ToolRequest,
)
from app.services.context_engine import RouteDecision, RoutingAction, context_engine
from app.services.definition_save_service import definition_save_service
from app.services.request_timeline import log_timeline_step


# 等待用户确认阶段的三个按钮：同意 / 修改 / 重新开始
CONFIRM_BUTTON_VALUE = "同意"
MODIFY_BUTTON_VALUE = "修改"
RESTART_BUTTON_VALUE = "重新开始"


logger = logging.getLogger(__name__)
EmitEvent = Callable[[str, dict[str, Any]], Awaitable[None]]


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
        route = await context_engine.route(request, emit=emit)
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

        intercepted = self._apply_context_route(request, route)
        if intercepted is not None:
            intercepted.debug = self._build_debug(route)
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
            return intercepted

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

        response.debug = self._build_debug(route)
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
        return response

    @staticmethod
    def _build_debug(route: RouteDecision) -> dict[str, Any]:
        return {
            "contextEngineStage": route.engineStage or "unknown",
            "contextEngineIntent": route.userIntent.value,
            "contextEngineAction": route.routingAction.value,
            "contextEngineReason": route.reason,
        }

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
                questions=self._suggestion_questions(request.state),
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
            return self._protocol_error(
                request,
                code="BLOCKED_BY_CONTEXT_ENGINE",
                message=route.reply or "这个操作当前不支持。",
            )

        return None

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
            uiActions=["save_definition_draft", "start_validating"],
        )

    @staticmethod
    def _suggestion_questions(state: AgentState) -> list[AgentQuestion]:
        if state == AgentState.WAITING_USER_CONFIRM:
            options = [
                QuestionOption(value=CONFIRM_BUTTON_VALUE, label="同意"),
                QuestionOption(value=MODIFY_BUTTON_VALUE, label="修改"),
                QuestionOption(value=RESTART_BUTTON_VALUE, label="重新开始"),
            ]
            prompt = "请对当前草稿做出选择："
        else:
            options = [
                QuestionOption(value="生成 A 机房昨日能耗日报", label="生成能耗日报"),
                QuestionOption(value="修改当前报表", label="修改当前报表"),
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
