"""请求级耗时链路日志。"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from time import perf_counter


logger = logging.getLogger("app.timeline")

_timeline_start: ContextVar[float | None] = ContextVar(
    "timeline_start", default=None
)
_conversation_id: ContextVar[str] = ContextVar("timeline_conversation_id", default="-")


def start_timeline(conversation_id: str) -> None:
    _timeline_start.set(perf_counter())
    _conversation_id.set(conversation_id)


def log_timeline_step(step: str) -> None:
    start = _timeline_start.get()
    elapsed_ms = 0 if start is None else round((perf_counter() - start) * 1000)
    logger.info(
        "[timeline] conversationId=%s %-32s %sms",
        _conversation_id.get(),
        step,
        elapsed_ms,
    )
