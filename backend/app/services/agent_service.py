"""Agent 服务层：状态流转校验 + 调用 FakeAgent + 协议错误包装。"""
from __future__ import annotations

from app.agents.fake_report_agent import FakeReportAgent
from app.schemas.agent import (
    ALLOWED_TRANSITIONS,
    AGENT_PROTOCOL_VERSION,
    AgentError,
    AgentRequest,
    AgentResponse,
    AgentState,
)


class AgentService:
    def __init__(self) -> None:
        self._agent = FakeReportAgent()

    def chat(self, request: AgentRequest) -> AgentResponse:
        # 协议版本检查
        if request.agentProtocolVersion != AGENT_PROTOCOL_VERSION:
            return self._protocol_error(
                request,
                code="INVALID_REQUEST",
                message=(
                    f"agentProtocolVersion 必须是 {AGENT_PROTOCOL_VERSION}，"
                    f"当前是 {request.agentProtocolVersion}"
                ),
            )

        response = self._agent.handle(request)

        # 状态转移校验（不含初始调用从外部传入的 state 本身）
        allowed = ALLOWED_TRANSITIONS.get(request.state, set())
        if response.state != request.state and response.state not in allowed:
            # Fake 阶段把不合法转移视作内部错误；真实接入时应避免发生
            return self._protocol_error(
                request,
                code="INVALID_STATE_TRANSITION",
                message=(
                    f"不允许从 {request.state.value} 转移到 "
                    f"{response.state.value}"
                ),
            )

        return response

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


agent_service = AgentService()
