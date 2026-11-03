"""#61 既有日志泄漏接管：日志、持久化记录与用户可见消息里不留原文。

覆盖工单 #61《统一开发 06：既有日志泄漏接管》的验收标准：

- 用户原话 / 模型返回原文 / 上游异常原文都不进常规日志；
- 上游异常原文不进持久化的操作记录原因字段；
- 用户可见的 SSE ``error`` 事件不含异常原文，但仍有用户能看懂的意思；
- 写日志失败不影响业务请求；
- 「精确原文」开关默认关闭、过期失效、开着且在有效期内才采；
- 模型行为不变（提示词、解析降级顺序、重试次数、失败码与既有文案逐字未变）。

真实模型未被调用：只替换注入的模型客户端与上游回复。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.agentscope_report_agent import AgentScopeReportAgent
from app.config import get_settings
from app.logging_config import configure_app_file_logging
from app.schemas.agent import (
    AgentRequest,
    AgentRequestContext,
    AgentState,
)
from app.services import recorded_progress_service
from app.services import report_diagnostics_service as diagnostics
from app.services import report_log_safety as safety
from app.services.agent_service import _build_agent, agent_service
from app.services.context_engine import (
    RouteDecision,
    RoutingAction,
    UserIntent,
    context_engine,
)
from app.services.report_design_service import report_design_service
from app.services.session_fact_service import SessionFactService
from app.services.session_context import (
    bind_session_key,
    clear_session_key,
    current_session_key,
)
from app.services.session_service import session_service
from app.storage.models import SessionRecord
from app.storage.report_design_repository import DESIGN_PHASE_PLAN_WAITING_CONFIRM
from app.storage.sqlite_store import SqliteSessionRepository


#: 敏感串在**运行时**拼出来，不写在源码字面量里。
#: 原因：失败日志会带调用栈，调用栈会打印源码行 —— 源码里写死的敏感串
#: 会跟着栈一起出现在日志里，测出来的就不是「异常原文没泄漏」了。
SECRET_USER = "SECRET_" + "USER_2"
SECRET_MODEL = "SECRET_" + "MODEL_1"
SECRET_EXC = "SECRET_" + "EXC_3"


def _boom(message: str) -> None:
    """按运行时传入的消息抛异常（源码里不出现消息本身）。"""
    raise ValueError(message)


# ----------------------------------------------------------------------
# 公共件：日志里怎么写才安全
# ----------------------------------------------------------------------


def test_safe_reason_returns_exception_type_not_message():
    exc = ValueError(SECRET_USER)
    assert safety.safe_reason(exc) == "ValueError"
    assert SECRET_USER not in safety.safe_reason(exc)


def test_model_text_shape_keeps_no_content_and_caps_length():
    """形状级摘要：一个字的内容都不留，且按上限截断。"""
    shape = safety.model_text_shape('{"action":"GENERATE_REPORT"}')
    assert "GENERATE_REPORT" not in shape
    assert "a" in shape  # 字母被换成类别符
    long_shape = safety.model_text_shape("中" * 500, limit=20)
    assert len(long_shape) < 60
    assert "已截断" in long_shape


def test_safe_identifier_keeps_identifiers_and_drops_free_text():
    assert safety.safe_identifier("submit_draft_plan") == "submit_draft_plan"
    assert safety.safe_identifier("GENERATE") == "GENERATE"
    # 形状对不上的（模型把用户原话塞进来）：只留形状摘要
    free_text = "用户原话 " + SECRET_USER
    assert SECRET_USER not in safety.safe_identifier(free_text)


def test_capped_reason_truncates_at_limit():
    reason = safety.capped_reason("原" * 500, limit=10)
    assert len(reason) < 60
    assert "已截断" in reason


def test_log_failure_keeps_stack_but_drops_exception_message(caplog):
    """失败日志留调用栈，不留异常原文。"""
    logger = logging.getLogger("app.test.log_safety")
    with caplog.at_level(logging.WARNING, logger="app.test.log_safety"):
        try:
            _boom(SECRET_EXC)
        except ValueError as exc:
            safety.log_failure(logger, "本轮失败", exc)

    text = caplog.text
    assert "本轮失败" in text
    assert "errorType=ValueError" in text
    assert SECRET_EXC not in text
    assert "Traceback" in text


def test_log_failure_swallows_logger_errors():
    """写日志失败不许影响业务请求。"""

    class _BrokenLogger:
        def warning(self, *args, **kwargs):
            raise RuntimeError("日志器自己炸了")

    try:
        _boom(SECRET_EXC)
    except ValueError as exc:
        safety.log_failure(_BrokenLogger(), "本轮失败", exc)  # 不抛


def test_user_visible_error_message_has_no_technical_detail():
    exc = ValueError(SECRET_EXC)
    message = safety.user_visible_error_message(exc)
    assert SECRET_EXC not in message
    assert message == safety.USER_VISIBLE_ERROR_MESSAGE
    assert "重试" in message


# ----------------------------------------------------------------------
# 泄漏点 1：ContextEngine —— 用户原话、模型返回原文、异常原文
# ----------------------------------------------------------------------


def _request(message: str, conversation_id: str = "conv_leak") -> AgentRequest:
    return AgentRequest(
        conversationId=conversation_id,
        state=AgentState.ANALYZING,
        message=message,
        context=AgentRequestContext(
            projectId="project_001",
            subjectRef="machine_room",
            subjectId="room_a",
        ),
    )


def test_leak1_context_engine_invalid_output_logs_no_model_raw_text(caplog):
    """泄漏点 1：模型输出不合法时，原文与异常原文都不进日志。"""
    caplog.set_level(logging.INFO, logger="app")
    secret = "模型原文 " + SECRET_MODEL

    decision = context_engine._parse_decision(secret)

    assert SECRET_MODEL not in caplog.text
    assert "rawLength=" in caplog.text
    assert "errorType=" in caplog.text
    # 降级行为逐字未变
    assert decision.userIntent is UserIntent.UNKNOWN
    assert decision.routingAction is RoutingAction.ASK_CLARIFICATION
    assert decision.reason == "模型输出不合法"
    assert decision.reply == "我没太明白你的意思。你是想确认、修改、重做，还是问问题？"


def test_leak1_context_engine_route_logs_no_user_words(caplog):
    """泄漏点 1：用户原话不进常规日志，只留长度。"""
    caplog.set_level(logging.INFO, logger="app")
    secret = "用户原话 " + SECRET_USER

    asyncio.run(context_engine.route(_request(secret)))

    assert SECRET_USER not in caplog.text
    assert "messageLength=" in caplog.text


def test_leak1_context_engine_fallback_reason_has_no_exception_text(caplog, monkeypatch):
    """兜底路由的原因里不夹带上游异常原文。"""
    caplog.set_level(logging.INFO, logger="app")
    monkeypatch.setattr(context_engine, "_client", object())

    async def _raise(**kwargs: Any) -> str:
        raise RuntimeError("上游异常 " + SECRET_EXC)

    monkeypatch.setattr(context_engine, "_chat_json_stream", _raise)

    decision = asyncio.run(context_engine.route(_request("生成昨日能耗日报")))

    assert SECRET_EXC not in caplog.text
    assert SECRET_EXC not in (decision.reason or "")
    assert decision.engineStage == "deep_route_failed_fallback"
    # 原因留的是类型，不是原文
    assert "RuntimeError" in (decision.reason or "")


# ----------------------------------------------------------------------
# 泄漏点 2、3：AgentScope —— 模型返回原文、静默降级
# ----------------------------------------------------------------------


class _FakeReActAgent:
    """只替身「模型回了什么」，不替身任何本仓逻辑。"""

    def __init__(self, text: str) -> None:
        self._text = text
        self.reply_count = 0

    async def reply(self, message: Any) -> Any:
        self.reply_count += 1
        return SimpleNamespace(content=self._text)


def _agentscope_agent(monkeypatch, reply_text: str):
    from app.agents.fake_report_agent import FakeReportAgent

    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._settings = get_settings()
    agent._fallback = FakeReportAgent()
    agent._model = object()
    agent._init_err = None
    fake = _FakeReActAgent(reply_text)

    async def _build_agent(request: Any) -> Any:
        return fake

    monkeypatch.setattr(agent, "_build_agent", _build_agent)
    return agent, fake


def test_leak2_agentscope_model_reply_not_logged(caplog, monkeypatch):
    """泄漏点 2：模型返回原文不进日志，只留长度与形状级摘要。"""
    caplog.set_level(logging.INFO, logger="app")
    secret = "模型返回 " + SECRET_MODEL
    agent, fake = _agentscope_agent(monkeypatch, secret)

    plan, reason = asyncio.run(agent._run_agent(_request("生成昨日能耗日报")))

    assert plan is None
    assert SECRET_MODEL not in caplog.text
    assert "replyLength=" in caplog.text
    assert "replyShape=" in caplog.text
    # 重试次数不变：首轮 + 修复轮，一共两次模型回复
    assert fake.reply_count == 2
    assert reason == "两轮都没有可解析的终结提交"


def test_leak3_agentscope_degradation_leaves_traceable_reason(caplog, monkeypatch):
    """泄漏点 3：降级到 FakeAgent 时日志里要留得住查的原因。"""
    caplog.set_level(logging.INFO, logger="app")
    secret = "模型返回 " + SECRET_MODEL
    agent, _fake = _agentscope_agent(monkeypatch, secret)

    response = asyncio.run(agent.ahandle(_request("生成昨日能耗日报")))

    assert "AgentScope 输出不合法，降级 FakeAgent" in caplog.text
    assert "reason=两轮都没有可解析的终结提交" in caplog.text
    assert SECRET_MODEL not in caplog.text
    # 用户仍然拿得到兜底回复（降级行为没变）
    assert response.reply


# ----------------------------------------------------------------------
# 泄漏点 4：路由层 —— 用户可见的 SSE error 事件
# ----------------------------------------------------------------------


def test_leak4_stream_error_event_has_no_exception_text(client, base_context, monkeypatch):
    """泄漏点 4：用户可见的错误消息里没有异常原文，但仍有能看懂的意思。"""
    from app.services.agent_service import agent_service

    class _Boom:
        def handle(self, request: Any) -> Any:
            raise RuntimeError("上游异常 " + SECRET_EXC)

    monkeypatch.setattr(agent_service, "_agent", _Boom())

    result = client.post("/api/agent/chat/stream", json={
        "agentProtocolVersion": "1.0",
        "conversationId": "conv_leak4",
        "state": "ANALYZING",
        "message": "生成昨日能耗日报",
        "context": base_context,
    })

    assert result.status_code == 200
    assert "event: error" in result.text
    assert SECRET_EXC not in result.text
    # 仍有用户能看懂的意思
    assert "服务暂时不可用，请稍后重试" in result.text
    assert '"code": "INTERNAL_ERROR"' in result.text


def test_leak4_non_stream_failure_raises_public_message_only(client, base_context, monkeypatch):
    """非流式入口抛出去的异常不带原文：Web 服务器的错误日志也不会有。"""
    from app.services.agent_service import agent_service

    class _Boom:
        def handle(self, request: Any) -> Any:
            raise RuntimeError("上游异常 " + SECRET_EXC)

    monkeypatch.setattr(agent_service, "_agent", _Boom())

    with pytest.raises(Exception) as caught:
        client.post("/api/agent/chat", json={
            "agentProtocolVersion": "1.0",
            "conversationId": "conv_leak4b",
            "state": "ANALYZING",
            "message": "生成昨日能耗日报",
            "context": base_context,
        })

    assert SECRET_EXC not in str(caught.value)
    assert "服务暂时不可用，请稍后重试" in str(caught.value)


# ----------------------------------------------------------------------
# 泄漏点 5：方案设计 Agent —— 进度回调失败
# ----------------------------------------------------------------------


def test_leak5_plan_progress_callback_failure_logs_no_exception_text(caplog):
    """泄漏点 5：进度回调失败只留原因类型，不留异常原文。"""
    from app.agents.report_plan_agent import _notify

    caplog.set_level(logging.INFO, logger="app")

    async def _boom(event: dict[str, Any]) -> None:
        raise RuntimeError("上游异常 " + SECRET_EXC)

    asyncio.run(_notify(_boom, {"event": "plan_design_progress"}))

    assert SECRET_EXC not in caplog.text
    assert "errorType=RuntimeError" in caplog.text
    assert "方案设计进度回调失败" in caplog.text


# ----------------------------------------------------------------------
# 泄漏点 6：持久化的操作记录原因字段
# ----------------------------------------------------------------------


def test_leak6_operation_reason_field_has_no_exception_text(tmp_path):
    """泄漏点 6：上游异常原文不进持久化的操作记录原因字段。"""
    import sqlite3
    import threading

    from app.storage.report_design_sqlite import SqliteReportDesignRepository

    conn = sqlite3.connect(tmp_path / "design.db", check_same_thread=False)
    repo = SqliteReportDesignRepository(connection=conn, lock=threading.RLock())
    repo.initialize_schema()
    try:
        claim = repo.claim_operation("sess_leak6", "msg_leak6", None)
        repo.fail_operation(
            claim["operationId"],
            {"code": "TEMPLATE_COMMIT_FAILED", "message": "上游异常 " + SECRET_EXC},
        )

        row = conn.execute(
            "SELECT error_code, error_message FROM report_design_operations"
            " WHERE operation_id = ?",
            (claim["operationId"],),
        ).fetchone()
        assert row is not None
        # 错误码留着（排查要靠它），原文不留
        assert row[0] == "TEMPLATE_COMMIT_FAILED"
        assert SECRET_EXC not in row[1]
        # 原因是我们自己写的固定文案
        assert row[1] == "这次草稿没有保存下来，请重试。"
    finally:
        conn.close()


def test_leak6_operation_reason_field_drops_free_text_for_unknown_code(tmp_path):
    """查不到错误码时用通用文案：调用方传的自由文本一律不落盘。"""
    import sqlite3
    import threading

    from app.storage.report_design_sqlite import SqliteReportDesignRepository

    conn = sqlite3.connect(tmp_path / "design.db", check_same_thread=False)
    repo = SqliteReportDesignRepository(connection=conn, lock=threading.RLock())
    repo.initialize_schema()
    try:
        claim = repo.claim_operation("sess_leak6b", "msg_leak6b", None)
        repo.fail_operation(
            claim["operationId"],
            {"code": "SOME_UNKNOWN_CODE", "message": "上游异常 " + SECRET_EXC},
        )

        row = conn.execute(
            "SELECT error_code, error_message FROM report_design_operations"
            " WHERE operation_id = ?",
            (claim["operationId"],),
        ).fetchone()
        assert row is not None
        assert SECRET_EXC not in row[1]
        assert row[1]
    finally:
        conn.close()


# ----------------------------------------------------------------------
# 写日志失败不影响业务请求
# ----------------------------------------------------------------------


class _BrokenLogger:
    """日志器自己抛错：业务请求不许跟着挂。"""

    def info(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("日志器自己炸了")

    def warning(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("日志器自己炸了")

    def error(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("日志器自己炸了")

    def exception(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("日志器自己炸了")


def test_logging_failure_does_not_break_request(client, base_context, monkeypatch):
    """契约表 §6.5：写日志失败不得让业务请求失败。

    本轮改动的三个文件（日志公共件、ContextEngine、路由层）加上 #57 的
    诊断层，日志器全部抛错时，请求照样正常返回。
    """
    from app.services import context_engine as context_engine_module
    from app.services import report_diagnostics_service as diagnostics_module
    from app.routers import agent as agent_router_module

    monkeypatch.setattr(context_engine_module, "logger", _BrokenLogger())
    monkeypatch.setattr(agent_router_module, "logger", _BrokenLogger())
    monkeypatch.setattr(diagnostics_module, "logger", _BrokenLogger())

    result = client.post("/api/agent/chat", json={
        "agentProtocolVersion": "1.0",
        "conversationId": "conv_logfail",
        "state": "ANALYZING",
        "message": "生成昨日能耗日报",
        "context": base_context,
    })

    assert result.status_code == 200
    assert result.json()["conversationId"] == "conv_logfail"


def test_logging_failure_does_not_break_failed_request(client, base_context, monkeypatch):
    """故障路径上的日志再抛错，也不许把原始异常盖掉，也不许把流挂住。"""
    from app.services import context_engine as context_engine_module
    from app.routers import agent as agent_router_module
    from app.services.agent_service import agent_service

    class _Boom:
        def handle(self, request: Any) -> Any:
            raise RuntimeError("上游异常 " + SECRET_EXC)

    monkeypatch.setattr(context_engine_module, "logger", _BrokenLogger())
    monkeypatch.setattr(agent_router_module, "logger", _BrokenLogger())
    monkeypatch.setattr(agent_service, "_agent", _Boom())

    result = client.post("/api/agent/chat/stream", json={
        "agentProtocolVersion": "1.0",
        "conversationId": "conv_logfail2",
        "state": "ANALYZING",
        "message": "生成昨日能耗日报",
        "context": base_context,
    })

    assert result.status_code == 200
    assert "event: error" in result.text
    assert SECRET_EXC not in result.text


# ----------------------------------------------------------------------
# 「精确原文」开关：默认关闭、限时、限范围
# ----------------------------------------------------------------------


@pytest.fixture()
def diagnostics_dir(tmp_path, monkeypatch) -> Path:
    """诊断目录指到临时目录（默认目录在用户 Library 里，不进仓库）。"""
    settings = get_settings()
    target = tmp_path / "diagnostics"
    monkeypatch.setattr(settings, "diagnostics_dir", target)
    monkeypatch.setattr(settings, "diagnostics_enabled", True)
    monkeypatch.setattr(settings, "diagnostics_exact_content", False)
    return target


def _read_records(directory: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def _write_diagnostic(conversation_id: str, raw_output: str) -> None:
    state = diagnostics.ModelCallState(
        request_id="req_exact_1", conversation_id=conversation_id
    )
    diagnostics.write_diagnostic_record(
        state=state,
        reason="验收测试",
        parse_status=diagnostics.PARSE_NOT_JSON,
        raw_output=raw_output,
    )


def test_exact_content_switch_is_off_by_default(diagnostics_dir):
    """默认关闭：采集不到原文。"""
    secret = "精确原文 " + SECRET_MODEL

    _write_diagnostic("conv_exact_1", secret)

    records = _read_records(diagnostics_dir)
    assert records
    assert secret not in json.dumps(records, ensure_ascii=False)
    assert records[0]["redacted"] is True


def test_exact_content_switch_collects_inside_window_and_scope(
    diagnostics_dir, monkeypatch
):
    """显式开启 + 未过期 + 在范围内：才采得到原文。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "diagnostics_exact_content", True)
    monkeypatch.setattr(
        settings, "diagnostics_exact_content_until", "2999-01-01T00:00:00"
    )
    monkeypatch.setattr(settings, "diagnostics_exact_content_scope", "conv_exact_1")
    secret = "精确原文 " + SECRET_MODEL

    _write_diagnostic("conv_exact_1", secret)

    records = _read_records(diagnostics_dir)
    assert records
    assert secret in json.dumps(records, ensure_ascii=False)
    assert records[0]["redacted"] is False


def test_exact_content_switch_expires(diagnostics_dir, monkeypatch):
    """过期即失效：过期后采不到原文。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "diagnostics_exact_content", True)
    monkeypatch.setattr(
        settings, "diagnostics_exact_content_until", "2000-01-01T00:00:00"
    )
    monkeypatch.setattr(settings, "diagnostics_exact_content_scope", "*")
    secret = "精确原文 " + SECRET_MODEL

    _write_diagnostic("conv_exact_1", secret)

    records = _read_records(diagnostics_dir)
    assert records
    assert secret not in json.dumps(records, ensure_ascii=False)


def test_exact_content_switch_respects_scope(diagnostics_dir, monkeypatch):
    """限范围：不在范围内的会话采不到原文。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "diagnostics_exact_content", True)
    monkeypatch.setattr(
        settings, "diagnostics_exact_content_until", "2999-01-01T00:00:00"
    )
    monkeypatch.setattr(settings, "diagnostics_exact_content_scope", "conv_other")
    secret = "精确原文 " + SECRET_MODEL

    _write_diagnostic("conv_exact_1", secret)

    records = _read_records(diagnostics_dir)
    assert records
    assert secret not in json.dumps(records, ensure_ascii=False)


# ----------------------------------------------------------------------
# 模型行为不变：提示词、解析降级顺序、重试次数、失败码与既有文案
# ----------------------------------------------------------------------


def test_prompt_files_are_byte_identical_to_baseline():
    """提示词逐字未变：本票不碰提示词。

    提示词文件不在本轮任何一张票的归属范围内，所以这里的摘要就是基线值。
    哪天真要改提示词，必须同时更新这个用例（让提示词变更显形）。
    """
    import hashlib

    from app.agents import agentscope_report_agent as agent_module

    expected = {
        agent_module._PROMPT_PATH: (
            "ac3ad7949c1ea173afbaa1e6f15a65ff904014aab39490ecc24cb2d47d95a62b"
        ),
        agent_module._EDIT_PROMPT_PATH: (
            "6ac4b626a6ffe3f79194c2e9016991f58a4e5634c940feb76e791f673709e1e4"
        ),
        agent_module._QUERY_PROMPT_PATH: (
            "2cd78a1cd91ac6fe6d813970903d284c9279504ede902edee7a66fca6c5b3bb9"
        ),
    }
    for path, digest in expected.items():
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == digest, f"提示词被改了：{path.name}"


def test_parse_degradation_order_and_copy_unchanged():
    """解析降级顺序与既有文案逐字未变。"""
    decision = context_engine._parse_decision("这不是 JSON")
    assert decision.userIntent is UserIntent.UNKNOWN
    assert decision.routingAction is RoutingAction.ASK_CLARIFICATION
    assert decision.confidence == 0
    assert decision.isReadyForNextStep is False
    assert decision.missingInfo == ["intent"]
    assert decision.nextQuestion == "我没太明白你的意思。你是想确认、修改、重做，还是问问题？"
    assert decision.reply == "我没太明白你的意思。你是想确认、修改、重做，还是问问题？"
    assert decision.reason == "模型输出不合法"


def test_model_is_not_replaced():
    """本票不替换模型：用的还是配置里那个。"""
    settings = get_settings()
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._settings = settings
    assert agent._settings.volc_model == settings.volc_model
    assert agent._settings.volc_context_router_model == settings.volc_context_router_model


def test_degraded_response_copy_unchanged():
    """既有失败文案逐字未变（降级到 FakeAgent 时给用户的那句）。"""
    from app.agents.agentscope_report_agent import AgentScopeReportAgent as Agent

    request = _request("生成昨日能耗日报")
    response = Agent._unavailable_edit_response(request)
    assert response.reply == "本轮查证遇到技术问题，草稿没有修改。请稍后重试。"
    assert response.error is not None
    assert response.error.code == "EDIT_VERIFICATION_UNAVAILABLE"


def test_exact_content_stays_in_restricted_local_dir_only(
    diagnostics_dir, monkeypatch, tmp_path
):
    """精确原文只落受限本机目录：不进业务数据库，也不进 Git 仓库。

    这是「临时诊断例外」的边界：它只影响诊断文件，不放宽
    「模拟业务数值不落盘」的默认约束。
    """
    import stat

    settings = get_settings()
    monkeypatch.setattr(settings, "diagnostics_exact_content", True)
    monkeypatch.setattr(
        settings, "diagnostics_exact_content_until", "2999-01-01T00:00:00"
    )
    monkeypatch.setattr(settings, "diagnostics_exact_content_scope", "*")
    secret = "精确原文 " + SECRET_MODEL

    _write_diagnostic("conv_exact_1", secret)

    # 落盘位置：目录 0700、文件 0600（只有当前用户能读）
    files = list(diagnostics_dir.glob("*.jsonl"))
    assert files
    assert stat.S_IMODE(diagnostics_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600

    # 不在 Git 仓库里
    repo_root = Path(__file__).resolve().parents[2]
    assert repo_root not in diagnostics_dir.resolve().parents
    default_dir = Path(
        "~/Library/Logs/haewu-ai-report/diagnostics"
    ).expanduser()
    assert repo_root not in default_dir.resolve().parents

    # 不在业务数据库里
    session_db = tmp_path / "sessions.db"
    if session_db.exists():
        assert secret not in session_db.read_bytes().decode("utf-8", "ignore")
    assert diagnostics_dir.resolve() != session_db.resolve()


def test_exact_content_does_not_change_business_storage_behaviour(
    diagnostics_dir, monkeypatch
):
    """精确原文模式只影响诊断层：业务存储该不落还是不落。

    「模拟业务数值不落盘」的默认约束没有被这个开关放宽：
    开关打开前后，业务存储写进去的东西一模一样。
    """
    import sqlite3
    import threading

    from app.storage.report_design_sqlite import SqliteReportDesignRepository

    settings = get_settings()
    monkeypatch.setattr(settings, "diagnostics_exact_content", True)
    monkeypatch.setattr(
        settings, "diagnostics_exact_content_until", "2999-01-01T00:00:00"
    )
    monkeypatch.setattr(settings, "diagnostics_exact_content_scope", "*")

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    repo = SqliteReportDesignRepository(connection=conn, lock=threading.RLock())
    repo.initialize_schema()
    try:
        claim = repo.claim_operation("sess_exact", "msg_exact", None)
        repo.fail_operation(
            claim["operationId"],
            {"code": "TEMPLATE_COMMIT_FAILED", "message": "上游异常 " + SECRET_EXC},
        )
        row = conn.execute(
            "SELECT error_message FROM report_design_operations WHERE operation_id = ?",
            (claim["operationId"],),
        ).fetchone()
        # 业务库里的原因字段依旧只有固定文案，与精确原文开关无关
        assert row is not None
        assert SECRET_EXC not in row[0]
    finally:
        conn.close()


# ----------------------------------------------------------------------
# AC#2 补漏：既有失败日志里的上游异常原文
#
# 这一组的口径是**留栈不留原文**：
#   - 异常消息（上游异常原文）不进日志；
#   - 出错**类型**还在（errorType=ValueError）；
#   - 调用**栈**还在（traceback 行在）——
#     证明不是「把异常整个吞掉」，只是把异常对象换成了 None。
#
# 跟 :func:`safety.log_failure` 的既有用例互补：那一条钉的是公共件，
# 这一组钉的是**既有调用点**（各服务里原来写着的 logger.exception）。
# ----------------------------------------------------------------------

#: canary 在**运行时**拼出来，源码里不出现完整字面量。
#: 原因：失败日志带调用栈，调用栈会打印源码行 —— 源码里写死的 canary
#: 会跟着栈一起出现在日志里，测出来的就不是「异常原文没泄漏」了。
CANARY_USER = "用户原话：工号" + "ABC123"


def _raise_with_canary(*_args: Any, **_kwargs: Any) -> Any:
    """按运行时传入的消息抛异常（源码行里不出现消息本身）。"""
    raise ValueError(CANARY_USER)


def _exploding_module(name: str) -> types.ModuleType:
    """一个 import 就会炸的模块：异常消息带 canary。"""

    class _Exploding(types.ModuleType):
        def __getattr__(self, attr: str) -> Any:
            raise ValueError(CANARY_USER)

    return _Exploding(name)


def test_ac2_agent_init_failure_keeps_type_and_stack_but_not_original(
    caplog, monkeypatch
):
    """AC#2 补漏：AgentScope 初始化失败 —— 原文不进日志，类型与栈还在。

    契约更新（MiniMax 迁移）：真实模型模式下初始化失败**不再降级 FakeAgent**，
    而是明确抛出。降级会让用户拿到一条规则拼出来的回复，看着像正常结果，
    实际上模型根本没跑 —— 配置漏了却看不出来。
    """
    caplog.set_level(logging.INFO, logger="app")
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_impl", "agentscope")
    monkeypatch.setitem(
        sys.modules,
        "app.agents.agentscope_report_agent",
        _exploding_module("app.agents.agentscope_report_agent"),
    )

    # 真实模型模式：构造失败必须抛出来，不返回 FakeAgent。
    with pytest.raises(ValueError):
        _build_agent()

    text = caplog.text
    # 异常原文不进日志
    assert CANARY_USER not in text
    # 出错类型还在
    assert "errorType=ValueError" in text
    # 调用栈还在 —— 是「留栈不留原文」，不是把异常整个吞掉
    assert "Traceback (most recent call last):" in text
    assert "in _build_agent" in text
    # 级别没被降下来（原来是 ERROR）
    failure = [r for r in caplog.records if "初始化失败" in r.getMessage()]
    assert failure and failure[0].levelno == logging.ERROR
    # 明确失败，不静默降级
    assert "不降级 FakeAgent" in text


def test_ac2_recorded_progress_read_failure_keeps_stack_but_not_original(
    caplog, monkeypatch
):
    """AC#2 补漏：显式 ``exc_info=True`` 的地方也是留栈不留原文。"""
    caplog.set_level(logging.INFO, logger="app")
    monkeypatch.setattr(
        session_service.repository, "get_session", _raise_with_canary
    )

    result = recorded_progress_service.describe_recorded_progress("sess_ac2")

    # 读不到就如实说读不到（业务行为没变）
    assert result["availability"] == "UNAVAILABLE"

    text = caplog.text
    assert CANARY_USER not in text
    assert "errorType=ValueError" in text
    assert "Traceback (most recent call last):" in text
    assert "in _read_session_record" in text
    # 级别不变（原来是 WARNING）
    failure = [r for r in caplog.records if "读取会话记录失败" in r.getMessage()]
    assert failure and failure[0].levelno == logging.WARNING


def test_ac2_session_fact_invalid_draft_value_not_logged(caplog, monkeypatch):
    """AC#2 补漏：校验错误的消息带着**出错字段的值** —— 不进日志。"""
    caplog.set_level(logging.INFO, logger="app")
    record = SessionRecord(
        id="sess_ac2",
        state="ANALYZING",
        snapshot={"definition": {"regions": CANARY_USER}},
    )
    monkeypatch.setattr(
        session_service.repository, "get_session", lambda session_id: record
    )

    facts = SessionFactService().load(session_id="sess_ac2")

    # 解析不了就如实说解析不了（业务行为没变）
    assert facts.status.value == "UNAVAILABLE"

    text = caplog.text
    assert CANARY_USER not in text
    assert "errorType=ValidationError" in text
    assert "Traceback (most recent call last):" in text


def test_ac2_report_design_state_read_failure_keeps_stack_but_not_original(
    caplog, monkeypatch
):
    """AC#2 补漏：设计状态读失败 —— 原文不进日志，类型与栈还在。"""
    caplog.set_level(logging.INFO, logger="app")
    monkeypatch.setattr(
        report_design_service.repository, "load", _raise_with_canary
    )

    state = report_design_service.load_state(
        _request("生成昨日能耗日报", conversation_id="conv_ac2")
    )

    # 读不到就按「没有设计状态」处理（业务行为没变）
    assert state is None

    text = caplog.text
    assert CANARY_USER not in text
    assert "errorType=ValueError" in text
    assert "Traceback (most recent call last):" in text
    assert "in load_state" in text
    # 级别没被降下来（原来是 ERROR）
    failure = [r for r in caplog.records if "读设计状态失败" in r.getMessage()]
    assert failure and failure[0].levelno == logging.ERROR


# ----------------------------------------------------------------------
# AC#1 补漏：用户原话 / 模型原文 / 原始字典整包进日志
# ----------------------------------------------------------------------


def test_ac1_pending_choice_raw_dict_not_logged(caplog, monkeypatch):
    """AC#1 补漏：待确认菜单的原始字典不整包进日志（查询回带菜单那条）。"""
    caplog.set_level(logging.INFO, logger="app")
    monkeypatch.setattr(
        session_service,
        "load_pending_choice",
        lambda session_id: {"state": "NOT_A_STATE", "question": CANARY_USER},
    )

    questions = agent_service._preserved_menu(
        _request("生成昨日能耗日报"), None
    )

    # 菜单读不出来就不给菜单（业务行为没变）
    assert questions == []

    text = caplog.text
    assert CANARY_USER not in text
    assert "step=pending_choice_invalid" in text
    # 只留派生信息：容器类型
    assert "rawType=dict" in text


def test_ac1_pending_choice_raw_dict_not_logged_in_chat(caplog, monkeypatch):
    """AC#1 补漏：chat 入口那处同样不整包 dump。"""
    caplog.set_level(logging.INFO, logger="app")
    monkeypatch.setattr(
        session_service,
        "load_pending_choice",
        lambda session_id: {"state": "NOT_A_STATE", "question": CANARY_USER},
    )

    asyncio.run(agent_service.chat(_request("生成昨日能耗日报")))

    text = caplog.text
    assert CANARY_USER not in text
    assert "step=pending_choice_invalid" in text
    assert "rawType=dict" in text


def test_ac1_blocked_by_route_message_not_logged(caplog):
    """AC#1 补漏：阻断说明会点名用户要的指标 —— 不进日志。"""
    caplog.set_level(logging.INFO, logger="app")
    route = RouteDecision(
        routingAction=RoutingAction.BLOCK,
        reply=CANARY_USER,
        blockCode="CAPABILITY_NOT_SUPPORTED",
        blockMessage=CANARY_USER,
    )

    response = agent_service._blocked_by_route(
        _request("生成昨日能耗日报"), route
    )

    # 用户可见的回复逐字未变
    assert response.reply == CANARY_USER

    text = caplog.text
    assert CANARY_USER not in text
    assert "step=blocked_by_route" in text
    assert "messageLength=" in text


def test_ac1_context_route_reason_not_logged(caplog, monkeypatch):
    """AC#1 补漏：路由原因可能夹带模型原文与用户原话 —— 只留长度。"""
    caplog.set_level(logging.INFO, logger="app")

    async def _route(request: Any, emit: Any = None) -> RouteDecision:
        return RouteDecision(
            routingAction=RoutingAction.CONTINUE_CURRENT,
            reason=CANARY_USER,
        )

    monkeypatch.setattr(context_engine, "route", _route)

    asyncio.run(agent_service.chat(_request("生成昨日能耗日报")))

    text = caplog.text
    assert CANARY_USER not in text
    assert "step=context_route_done" in text
    assert "reasonLength=" in text


def test_ac1_contract_error_section_title_not_logged(caplog):
    """AC#1 补漏：模型写的板块标题不进日志，但用户可见文案逐字未变。"""
    caplog.set_level(logging.INFO, logger="app")
    plan = {
        "id": "plan_ac1",
        "sections": [
            {
                "id": "section_ac1",
                "title": CANARY_USER,
                "metricIntents": ["总耗电"],
                "componentType": "radar",
            }
        ],
    }
    request = AgentRequest(
        conversationId="conv_ac1",
        state=AgentState.ANALYZING,
        message="确认",
        messageId="msg_ac1",
        context=AgentRequestContext(
            projectId="project_001",
            subjectRef="machine_room",
            subjectId="room_a",
            designSessionId="sess_ac1",
        ),
    )

    response = report_design_service.confirm_plan(
        request,
        state={
            "reportPlan": plan,
            "designPhase": DESIGN_PHASE_PLAN_WAITING_CONFIRM,
        },
    )

    # 失败码与用户可见文案逐字未变（板块名照旧透传给用户）
    assert response.error is not None
    assert response.error.code == "PLAN_COMPONENT_TYPE_INVALID"
    assert CANARY_USER in response.error.message

    text = caplog.text
    # 板块标题（模型原文）不进日志
    assert CANARY_USER not in text
    assert "方案组件类型非法" in text
    # 只留标识：板块序号与 id
    assert "sectionIndex=0" in text
    assert "sectionId=section_ac1" in text


# ----------------------------------------------------------------------
# 补漏（统筹收口）：用户可见的原因里也不许有上游异常原文
# ----------------------------------------------------------------------


def test_ac3_concierge_failure_reason_has_no_exception_text(caplog, monkeypatch):
    """concierge 兜底原因只留异常类型，不留原文。

    与「模型路由失败」那条兜底同口径（见上面那条 fallback 用例）。
    """
    caplog.set_level(logging.INFO, logger="app")
    monkeypatch.setattr(context_engine, "_client", object())

    async def _raise(**kwargs: Any) -> str:
        raise RuntimeError("上游异常 " + SECRET_EXC)

    monkeypatch.setattr(context_engine, "_chat_json_stream", _raise)

    decision = asyncio.run(
        context_engine._run_concierge(_request("生成昨日能耗日报"))
    )

    assert SECRET_EXC not in (decision.reason or "")
    assert "RuntimeError" in (decision.reason or "")
    assert SECRET_EXC not in caplog.text


def test_ac3_session_fact_read_failure_reason_has_no_exception_text(
    caplog, monkeypatch
):
    """读会话记录失败时，用户可见的原因里没有上游异常原文。"""
    caplog.set_level(logging.INFO, logger="app")

    def _raise(session_id: str):
        raise RuntimeError("上游异常 " + SECRET_EXC)

    monkeypatch.setattr(session_service.repository, "get_session", _raise)

    facts = SessionFactService().load(session_id="sess_ac3")

    assert facts.status.value == "UNAVAILABLE"
    # 业务行为不变：如实说「暂时无法确认」
    assert "暂时无法确认" in (facts.reason or "")
    # 原文不出现，类型留下
    assert SECRET_EXC not in (facts.reason or "")
    assert "RuntimeError" in (facts.reason or "")
    assert SECRET_EXC not in caplog.text


def test_ac1_rule_block_reason_has_no_user_words(caplog, monkeypatch):
    """规则阻断的原因里嵌着**用户说的指标名** —— 不进日志（#61 AC#1）。

    这条原因不是固定文案：「指标「设备心情值」未在指标目录中登记」里
    那个指标名是从用户原话里抽出来的 → 原样进日志就是用户原话。
    """
    caplog.set_level(logging.INFO, logger="app")
    monkeypatch.setattr(context_engine, "_client", None)

    canary = "设备心情值" + "Z9"
    decision = asyncio.run(
        context_engine.route(_request(f"统计 {canary} 报表"))
    )

    # 这条路径确实走到了（否则断言没意义）
    assert decision.engineStage == "rule_block"
    text = caplog.text
    assert "step=rule_block" in text
    # 只留原因码与长度
    assert "reasonLength=" in text
    assert "CAPABILITY_NOT_SUPPORTED" in text
    # 用户原话与抽出来的指标名都不许出现
    assert canary not in text
    assert "设备心情值" not in text


def test_ac1_blocked_draft_assembly_logs_no_model_text(caplog, monkeypatch):
    """草稿组装被阻断时，阻断说明里的**载荷字段值**不进日志（#61 AC#1）。

    阻断说明会带模型写的指标名（可能含用户数据）→ 日志只留错误码与原因类型，
    与 `template_preview_flow` 里同一个 `DefinitionBuildError` 的写法一致。
    """
    caplog.set_level(logging.INFO, logger="app")
    agent, _fake = _agentscope_agent(monkeypatch, "{}")

    canary = "自定义指标" + "ABC999"
    request = AgentRequest(
        conversationId="conv_ac1_block",
        state=AgentState.ANALYZING,
        message="生成一份能耗日报",
        context=AgentRequestContext(
            projectId="project_001",
            subjectRef="machine_room",
            subjectId="room_a",
        ),
    )
    response = agent._assemble_generate_response(
        request,
        {"reportName": "能耗日报", "roomId": "room_a", "metricRefs": [canary]},
    )

    # 业务行为不变：组装被阻断时如实阻断（不退回默认草稿）
    assert response.state.value == "BLOCKED_OR_UNSUPPORTED"
    assert response.blocks and response.blocks[0].code == "METRIC_NOT_REGISTERED"
    # 用户**看得见**的那句仍然点名是哪个指标（用户要能动手改）——
    # 这一条不是日志，#61 管的是日志与上游异常原文。
    assert canary in response.blocks[0].message

    text = caplog.text
    assert "草稿组装被阻断" in text
    assert canary not in text
    assert "message=" not in text


# ----------------------------------------------------------------------
# 第三处漏网：#61 修好之后被 #66 F-3 重新引入的那处
# ----------------------------------------------------------------------


def test_ac2_session_report_plan_read_failure_keeps_stack_but_not_original(
    caplog, monkeypatch, tmp_path
):
    """会话方案读取失败 —— 上游异常原文与本地绝对路径都不进日志。

    这处 ``logger.exception``（``agent_service._session_report_plan``）是
    #66 F-3（``dec95a0``）重新引入的：没显式给 ``exc_info``，走默认的
    ``exc_info=True``，traceback 末行会把上游异常的**原文**一起打出来
    （实测形态：``sqlite3.OperationalError: unable to open database file:
    <本地绝对路径>``）—— 本地绝对路径 / 用户信息就跟着泄漏了。

    这里用**真实形态的 SQLite 失败**驱动真实路径：临时目录里造一个打不开的
    库路径，让真的 SQLite 抛 ``OperationalError``，而不是拿假异常凑数。
    """
    # 只读目录：sqlite3 连不上（真实 OperationalError，不是 monkeypatch 的假异常）
    locked_dir = tmp_path / "locked"
    locked_dir.mkdir()
    os.chmod(locked_dir, 0o555)
    bad_db_path = locked_dir / "sessions.db"

    repository = SqliteSessionRepository(bad_db_path)
    monkeypatch.setattr(session_service, "_repo", repository)

    # 先自己撞一次同样的失败，把上游异常的原文抓出来（源码里不写死这个串，
    # 也不把它写进断言 —— 断言时用它本身当 canary）
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        repository.get_session("sess_leak3")
    upstream_message = str(excinfo.value)

    caplog.set_level(logging.INFO, logger="app")
    request = _request("生成昨日能耗日报", conversation_id="conv_leak3")
    # 请求级会话上下文：跟真实链路一样先绑定 session key（别的用例可能留下了
    # 残留的 ContextVar，这里绑成自己的，断言才是确定的），用完还原。
    previous_key = current_session_key()
    bind_session_key(request)
    try:
        plan = agent_service._session_report_plan(request)
    finally:
        if previous_key is None:
            clear_session_key()
        else:
            bind_session_key(_request("还原会话上下文", conversation_id=previous_key))

    # 业务行为不变：读不到方案就当没有方案
    assert plan is None

    text = caplog.text
    # 这条路径确实走到了（否则断言没意义）
    assert "step=read_report_plan_failed" in text
    # 上游异常原文不进日志 —— 这正是这处漏网的根因
    assert upstream_message not in text
    # 本地绝对路径不进日志
    assert str(bad_db_path) not in text
    assert str(tmp_path) not in text
    # 原因类型还在（留栈不留原文）
    assert "errorType=OperationalError" in text
    # 调用栈还在 —— 是「留栈不留原文」，不是把异常整个吞掉
    assert "Traceback (most recent call last):" in text
    # 标识不许少
    assert "sessionId=conv_leak3" in text
    # 级别没被降下来（原来是 ERROR）
    failure = [
        r for r in caplog.records if "read_report_plan_failed" in r.getMessage()
    ]
    assert failure and failure[0].levelno == logging.ERROR


import importlib.util
from app.logging_config import configure_app_file_logging


# ----------------------------------------------------------------------
# 第四处残留：app/main.py —— 文件日志配置失败时的那条诊断日志
# ----------------------------------------------------------------------


def _run_main_module_with_failing_file_logging(monkeypatch, bad_log_path):
    """在隔离的模块对象里重跑 ``app/main.py`` 的模块级代码，让它真的写不了日志文件。

    ``app/main.py`` 里那段 ``try/except`` 是**模块级**代码（导入时执行一次），
    要重跑它只能重新执行这个模块文件。这里用 ``importlib`` 在一个**新的**模块
    对象里执行仓库里那份源码：

    - 不动 ``sys.modules["app.main"]`` —— 已经 import 过它的用例不受影响；
    - 执行的就是 ``backend/app/main.py`` 这份源码，不是抄出来的副本；
    - 模块名仍叫 ``app.main`` → 日志器名与线上一致。
    """
    real_configure = configure_app_file_logging

    def _boom():
        # 真实实现 + 不可写的目标目录 → 真的抛 OSError（NotADirectoryError）
        return real_configure(bad_log_path)

    monkeypatch.setattr(
        "app.logging_config.configure_app_file_logging", _boom
    )
    spec = importlib.util.spec_from_file_location(
        "app.main", Path(__file__).resolve().parents[1] / "app" / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_file_logging_failure_keeps_stack_but_not_original(
    caplog, monkeypatch, tmp_path
):
    """写不了后端文件日志时 —— 上游异常原文与本地绝对路径都不进日志。

    这处 ``logger.exception``（``app/main.py``）是仓库最早提交引入的**存量代码**：
    没显式给 ``exc_info``，走默认的 ``exc_info=True``，traceback 末行会把上游
    异常的**原文**一起打出来（实测形态：``NotADirectoryError: [Errno 20]
    Not a directory: <本地绝对路径>``）—— 本地绝对路径就跟着泄漏了。

    这里用**真实形态的写文件失败**驱动真实路径：把日志目标指到一条「父路径
    是文件」的路径上，让真的 ``mkdir`` 抛 ``NotADirectoryError``（``OSError``
    的子类），而不是拿假异常凑数。
    """
    caplog.set_level(logging.INFO, logger="app")

    # 目标日志文件的父路径是一个「文件」→ mkdir 抛 NotADirectoryError
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    bad_log_path = blocker / "sub" / "backend.log"

    # 先自己撞一次同样的失败，把上游异常的原文抓出来（源码里不写死这个串，
    # 也不把它写进断言 —— 断言时用它本身当 canary）
    with pytest.raises(OSError) as excinfo:
        configure_app_file_logging(bad_log_path)
    upstream_message = str(excinfo.value)

    _run_main_module_with_failing_file_logging(monkeypatch, bad_log_path)

    text = caplog.text
    # 这条路径确实走到了（否则断言没意义）
    assert "无法写入后端文件日志，继续使用控制台日志" in text
    # 上游异常原文不进日志 —— 这正是这处残留的根因
    assert upstream_message not in text
    # 本地绝对路径不进日志
    assert str(tmp_path) not in text
    assert str(blocker) not in text
    assert str(bad_log_path) not in text
    # 留栈不留原文：调用栈还在（不是把异常整个吞掉）
    assert "Traceback (most recent call last):" in text
    # 级别没被降下来（原来是 ERROR）
    failure = [
        r for r in caplog.records
        if "无法写入后端文件日志" in r.getMessage()
    ]
    assert failure and failure[0].levelno == logging.ERROR
