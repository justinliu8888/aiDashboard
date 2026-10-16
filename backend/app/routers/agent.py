"""AI 对话入口路由：POST /api/agent/chat。"""
from __future__ import annotations

from fastapi import APIRouter

from app.schemas.agent import AgentRequest, AgentResponse
from app.services.agent_service import agent_service


router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.post("/chat", response_model=AgentResponse)
def chat(request: AgentRequest) -> AgentResponse:
    return agent_service.chat(request)
