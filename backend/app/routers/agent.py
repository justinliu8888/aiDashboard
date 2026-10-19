"""AI 对话入口路由：POST /api/agent/chat。"""
from __future__ import annotations

import asyncio
import json
import logging
from time import perf_counter
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.schemas.agent import AgentRequest, AgentResponse
from app.services.agent_service import agent_service
from app.services.request_timeline import log_timeline_step, start_timeline


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])


def _sse(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


async def _emit_reply_stream(reply: str, emit) -> None:
    if not reply:
        return
    await emit("stage", {"stage": "reply_stream_start"})
    for char in reply:
        await emit("reply_delta", {"delta": char})
        await asyncio.sleep(0.018)
    await emit("stage", {"stage": "reply_stream_done"})


@router.post("/chat", response_model=AgentResponse)
async def chat(request: AgentRequest) -> AgentResponse:
    started = perf_counter()
    start_timeline(request.conversationId)
    log_timeline_step("router_received")
    logger.info(
        "[agent.chat] step=router_received conversationId=%s state=%s message=%r",
        request.conversationId,
        request.state.value,
        request.message,
    )
    try:
        response = await agent_service.chat(request)
    except Exception:  # noqa: BLE001
        log_timeline_step("router_failed")
        logger.exception(
            "[agent.chat] step=router_failed conversationId=%s elapsed_ms=%.1f",
            request.conversationId,
            (perf_counter() - started) * 1000,
        )
        raise
    log_timeline_step("router_return")
    logger.info(
        "[agent.chat] step=router_return conversationId=%s elapsed_ms=%.1f response_state=%s uiActions=%s error=%s",
        request.conversationId,
        (perf_counter() - started) * 1000,
        response.state.value,
        response.uiActions,
        response.error.code if response.error else None,
    )
    return response


@router.post("/chat/stream")
async def chat_stream(request: AgentRequest) -> StreamingResponse:
    async def event_generator():
        started = perf_counter()
        queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

        async def emit(event: str, data: dict[str, Any]) -> None:
            data = {**data, "elapsedMs": round((perf_counter() - started) * 1000)}
            await queue.put((event, data))

        async def run_chat() -> None:
            start_timeline(request.conversationId)
            log_timeline_step("router_received")
            await emit(
                "stage",
                {
                    "stage": "router_received",
                    "state": request.state.value,
                    "message": request.message,
                },
            )
            try:
                response = await agent_service.chat(request, emit=emit)
                log_timeline_step("router_return")
                await _emit_reply_stream(response.reply, emit)
                await emit(
                    "final",
                    response.model_dump(mode="json"),
                )
            except Exception as exc:  # noqa: BLE001
                log_timeline_step("router_failed")
                logger.exception(
                    "[agent.chat.stream] step=router_failed conversationId=%s elapsed_ms=%.1f",
                    request.conversationId,
                    (perf_counter() - started) * 1000,
                )
                await emit("error", {"message": str(exc)})
            finally:
                await queue.put(None)

        task = asyncio.create_task(run_chat())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                event, data = item
                yield _sse(event, data)
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
