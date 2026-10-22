"""请求日志必须落盘，包含流式入口及其最终状态。"""
from __future__ import annotations

import logging
import stat
from logging.handlers import RotatingFileHandler

from app.logging_config import configure_app_file_logging


def test_stream_request_is_written_to_private_rotating_log(client, base_context, tmp_path):
    path = tmp_path / "backend.log"
    logger = logging.getLogger("app")
    previous = set(logger.handlers)
    configure_app_file_logging(path)
    new_handlers = set(logger.handlers) - previous
    assert any(isinstance(handler, RotatingFileHandler) for handler in new_handlers)
    try:
        result = client.post("/api/agent/chat/stream", json={
            "conversationId": "conv_resume_new",
            "resumeOfConversationId": "conv_resume_old",
            "state": "ANALYZING",
            "message": "我想吃龙虾",
            "context": base_context,
        })
        assert result.status_code == 200
    finally:
        for handler in new_handlers:
            logger.removeHandler(handler)
            handler.close()

    content = path.read_text(encoding="utf-8")
    assert "step=router_received conversationId=conv_resume_new" in content
    assert "resumeOf=conv_resume_old" in content
    assert "step=router_return conversationId=conv_resume_new" in content
    assert "[timeline] conversationId=conv_resume_new" in content
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
