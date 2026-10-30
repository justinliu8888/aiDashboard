"""#66g：模型调用失败这条路，日志里也要留调用栈。

补的是**可排查性**，不是放宽「不许泄漏」：

- 路由层失败 → 日志里本来就有栈（既有行为，这一票不许变）；
- 模型调用失败 → 日志里**也要有**栈（这一票补的）；
- 两种情况下日志里都**没有**异常消息、用户原话、模型返回原文。

栈里只有**代码位置**（哪一行炸的），异常消息不进日志 —— 口径与路由层
完全一致：``exc_info=(异常类型, None, traceback)``，异常对象被换掉。

敏感串在**运行时**拼出来，不写在源码字面量里：失败日志会带调用栈，
调用栈会打印源码行 —— 源码里写死的敏感串会跟着栈一起出现在日志里，
测出来的就不是「异常原文没泄漏」了。
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.report_plan_agent import generate_candidate
from app.services import report_diagnostics_service as diagnostics
from app.services.report_design_model_client import ReportDesignModelClient


#: 敏感串在**运行时**拼出来，不写在源码字面量里（原因见模块注释）。
SECRET_USER = "SECRET_" + "USER_2"
SECRET_MODEL = "SECRET_" + "MODEL_1"
SECRET_EXC = "SECRET_" + "EXC_3"


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _records(caplog: Any, marker: str) -> list[Any]:
    """消息里带指定标记的日志记录。"""
    return [
        record for record in caplog.records if marker in record.getMessage()
    ]


def _model_call_record(caplog: Any) -> Any | None:
    """``step=model_call`` 那一行（模型调用适配层写的元信息行）。"""
    records = _records(caplog, "step=model_call")
    return records[-1] if records else None


class _BoomOpenAI:
    """上游客户端替身：调用就抛异常（消息带可识别标记）。

    源码里只有变量名，没有标记本身 —— 调用栈打印源码行时不会带出去。
    """

    def __init__(self, message: str) -> None:
        self._message = message
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs: Any) -> Any:
        raise RuntimeError(self._message)


def _failing_client(message: str) -> ReportDesignModelClient:
    """一个「调用就炸」的模型适配层客户端。"""
    client = ReportDesignModelClient()
    client._client = _BoomOpenAI(message)
    return client


# --------------------------------------------------------------------------
# 模型调用失败（适配层）→ 日志里有栈，没有异常消息
# --------------------------------------------------------------------------


def test_model_call_failure_keeps_stack_but_drops_exception_message(caplog) -> None:
    """适配层：模型调用失败 → 日志里有调用栈，异常消息不进日志。"""
    caplog.set_level(logging.INFO, logger="app")
    client = _failing_client("upstream failed: " + SECRET_EXC)

    with pytest.raises(RuntimeError):
        asyncio.run(client([{"role": "user", "content": "生成报表"}]))

    record = _model_call_record(caplog)
    assert record is not None, "模型调用失败时适配层那一行必须在"
    # 既有元信息不许少
    assert "errorType=RuntimeError" in record.getMessage()
    # 补上的那一条：**有栈**
    assert record.exc_info is not None
    assert "Traceback" in caplog.text
    # 栈里有代码位置：炸在替身上游客户端的那一行
    assert "test_ticket66g_model_call_stack.py" in caplog.text
    assert "report_design_model_client.py" in caplog.text
    # 异常消息不进日志
    assert SECRET_EXC not in caplog.text


def test_model_call_failure_stack_has_no_model_raw_text(caplog) -> None:
    """上游报错文本里夹带的模型返回原文，也不进日志。"""
    caplog.set_level(logging.INFO, logger="app")
    client = _failing_client(
        "upstream failed: " + SECRET_EXC + " raw=" + SECRET_MODEL
    )

    with pytest.raises(RuntimeError):
        asyncio.run(client([{"role": "user", "content": "生成报表"}]))

    record = _model_call_record(caplog)
    assert record is not None
    assert record.exc_info is not None
    assert SECRET_EXC not in caplog.text
    assert SECRET_MODEL not in caplog.text


def test_successful_model_call_keeps_no_stack(caplog) -> None:
    """成功的那一次调用**不挂栈**：栈只属于失败。"""
    caplog.set_level(logging.INFO, logger="app")
    client = ReportDesignModelClient()

    class _Ok:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=self)

        async def create(self, **kwargs: Any) -> Any:
            return SimpleNamespace(
                id="resp_66g_ok",
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="好的"),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )

    client._client = _Ok()

    asyncio.run(client([{"role": "user", "content": "生成报表"}]))

    record = _model_call_record(caplog)
    assert record is not None
    assert record.exc_info is None


def test_log_model_call_without_a_handled_exception_has_no_stack(caplog) -> None:
    """拿不到异常时**不挂栈** —— 宁可少写，也不写错的那一条。"""
    caplog.set_level(logging.INFO, logger="app")
    state = diagnostics.ModelCallState(request_id="req_66g_no_exc")
    state.error_type = "RuntimeError"

    diagnostics.log_model_call(state)

    record = _model_call_record(caplog)
    assert record is not None
    assert record.exc_info is None
    assert "errorType=RuntimeError" in record.getMessage()


def test_log_model_call_accepts_an_explicit_exception(caplog) -> None:
    """显式递进来的异常：留栈、不留消息；``errorType`` 也补齐。"""
    caplog.set_level(logging.INFO, logger="app")
    state = diagnostics.ModelCallState(request_id="req_66g_explicit")

    try:
        raise RuntimeError(SECRET_EXC)
    except RuntimeError as exc:
        diagnostics.log_model_call(state, exc=exc)

    record = _model_call_record(caplog)
    assert record is not None
    assert record.exc_info is not None
    assert "errorType=RuntimeError" in record.getMessage()
    assert SECRET_EXC not in caplog.text


# --------------------------------------------------------------------------
# 模型调用失败（方案设计 Agent）→ 日志里有栈，没有用户原话与异常消息
# --------------------------------------------------------------------------


def test_plan_agent_model_call_failure_keeps_stack(caplog) -> None:
    """方案设计：模型调用失败 → 日志里有调用栈。"""
    caplog.set_level(logging.INFO, logger="app")

    async def _boom(messages: list[dict[str, Any]]) -> str:
        raise RuntimeError("upstream said: " + SECRET_EXC)

    result = asyncio.run(
        generate_candidate(
            message="把昨日能耗画成折线图",
            current_plan=None,
            history=[],
            model_call=_boom,
        )
    )

    # 既有业务输出逐字未变
    assert result["kind"] == "failure"
    assert result["code"] == "MODEL_CALL_FAILED"
    assert result["message"] == "模型调用失败，请稍后重试"

    records = _records(caplog, "方案设计模型调用失败")
    assert records, "模型调用失败时方案设计那一行必须在"
    assert records[0].exc_info is not None
    assert "errorType=RuntimeError" in records[0].getMessage()
    # 栈里有代码位置
    assert "report_plan_agent.py" in caplog.text
    assert "test_ticket66g_model_call_stack.py" in caplog.text


def test_plan_agent_model_call_failure_drops_user_and_model_text(caplog) -> None:
    """同一场景：异常消息、用户原话、模型返回原文都不进日志。"""
    caplog.set_level(logging.INFO, logger="app")
    user_text = "把昨日能耗画成折线图 " + SECRET_USER

    async def _boom(messages: list[dict[str, Any]]) -> str:
        raise RuntimeError(
            "upstream said: " + SECRET_EXC + " raw=" + SECRET_MODEL
        )

    result = asyncio.run(
        generate_candidate(
            message=user_text,
            current_plan=None,
            history=[],
            model_call=_boom,
        )
    )

    assert result["code"] == "MODEL_CALL_FAILED"
    records = _records(caplog, "方案设计模型调用失败")
    assert records and records[0].exc_info is not None
    # 三样都不许出现
    assert SECRET_EXC not in caplog.text
    assert SECRET_USER not in caplog.text
    assert SECRET_MODEL not in caplog.text
    # 用户拿到的那句话里也没有
    assert SECRET_EXC not in result["message"]
    assert SECRET_USER not in result["message"]


# --------------------------------------------------------------------------
# 路由层：既有行为不变 —— 失败时仍然留栈，仍然不留异常消息
# --------------------------------------------------------------------------


def test_router_failure_still_keeps_stack(
    client, base_context, monkeypatch, caplog
) -> None:
    """路由层留栈是**既有行为**：这一票不许把它改掉。"""
    from app.services.agent_service import agent_service

    caplog.set_level(logging.INFO, logger="app")

    class _Boom:
        def handle(self, request: Any) -> Any:
            raise RuntimeError("upstream failed: " + SECRET_EXC)

    monkeypatch.setattr(agent_service, "_agent", _Boom())

    with pytest.raises(Exception):
        client.post("/api/agent/chat", json={
            "agentProtocolVersion": "1.0",
            "conversationId": "conv_66g_router",
            "state": "ANALYZING",
            "message": "生成昨日能耗日报",
            "context": base_context,
        })

    records = _records(caplog, "step=router_failed")
    assert records, "路由层失败那一行必须在"
    assert records[0].exc_info is not None
    assert "errorType=RuntimeError" in records[0].getMessage()
    assert SECRET_EXC not in caplog.text
