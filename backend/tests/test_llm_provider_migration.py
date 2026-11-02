"""MiniMax 迁移专项：provider 解析、thinking 透传、缺配置明确失败。

只覆盖本次迁移**直接相关**的范围：
- 各入口选到正确的 provider / 模型；
- ``thinking.type=disabled`` 真正下发到上游（含 AgentScope 路径）；
- 显式切回方舟的配置路径仍能正确构造调用；
- 缺配置 / 上游失败时是**明确失败**，不是静默降级 FakeAgent。

不联网：上游一律用替身。真实链路的验证在迁移验证脚本里做。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import get_settings
from app.services.context_engine import ContextEngine
from app.services.report_design_model_client import ReportDesignModelClient


# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------


class _FakeUsage:
    prompt_tokens = 11
    completion_tokens = 22
    total_tokens = 33


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.id = "resp_fake"
        self.choices = [
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason="stop",
            )
        ]
        self.usage = _FakeUsage()


class _FakeOpenAI:
    """记录上游收到的每个 kwarg。"""

    def __init__(self, content: str = '{"kind":"plan_candidate"}') -> None:
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=self)
        self._content = content

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self.requests.append(kwargs)
        return _FakeResponse(self._content)


class _FakeStream:
    """最小可用的 SSE 流。"""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for text in self._chunks:
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=text))]
            )


class _FakeStreamingOpenAI:
    def __init__(self, chunks: list[str]) -> None:
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=self)
        self._chunks = chunks

    async def create(self, **kwargs: Any) -> _FakeStream:
        self.requests.append(kwargs)
        return _FakeStream(self._chunks)


# --------------------------------------------------------------------------
# 1. provider 解析
# --------------------------------------------------------------------------


def test_default_provider_is_minimax_with_thinking_disabled():
    """默认走 MiniMax，思考显式关闭。"""
    settings = get_settings()
    assert settings.provider_name == "minimax"
    assert settings.active_model == "MiniMax-M3"
    assert settings.request_extra_body == {"thinking": {"type": "disabled"}}


def test_active_base_url_is_api_root_not_full_endpoint():
    """base_url 必须是 API 根地址，不能带 /chat/completions。"""
    settings = get_settings()
    base = settings.active_base_url
    assert base == "https://api.minimax.cn/v1"
    assert not base.rstrip("/").endswith("/chat/completions")


def test_stage_models_follow_provider_by_default():
    """各阶段模型留空时跟随 provider 默认模型。"""
    settings = get_settings()
    assert settings.design_model == settings.active_model
    assert settings.context_concierge_model == settings.active_model
    assert settings.context_router_model == settings.active_model


def test_explicit_volcengine_provider_uses_volc_settings(monkeypatch):
    """显式切回方舟：走 VOLC_*，且不带 thinking 字段。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "volcengine")
    monkeypatch.setattr(settings, "volc_api_key", "ark-test")
    monkeypatch.setattr(settings, "volc_base_url", "https://ark.cn-beijing.volces.com/api/v3")
    monkeypatch.setattr(settings, "volc_model", "doubao-seed-2-1-lite-260915")

    assert settings.provider_name == "volcengine"
    assert settings.active_api_key == "ark-test"
    assert settings.active_base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert settings.active_model == "doubao-seed-2-1-lite-260915"
    # 方舟不认 thinking 字段，不能带过去
    assert settings.request_extra_body == {}


def test_generic_override_wins_over_provider_specific(monkeypatch):
    """LLM_* 通用覆盖优先级最高。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "generic-key")
    monkeypatch.setattr(settings, "llm_base_url", "https://example.test/v1")
    monkeypatch.setattr(settings, "llm_model", "some-model")
    assert settings.active_api_key == "generic-key"
    assert settings.active_base_url == "https://example.test/v1"
    assert settings.active_model == "some-model"


def test_unknown_provider_raises_instead_of_silently_picking_one(monkeypatch):
    """未知 provider 直接报错，不静默挑一家。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "not-a-provider")
    with pytest.raises(ValueError):
        settings.provider_name


# --------------------------------------------------------------------------
# 2. thinking 参数真的下发到上游
# --------------------------------------------------------------------------


def test_report_design_client_sends_thinking_disabled(monkeypatch):
    """方案设计：extra_body 逐请求下发。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "minimax")
    client = ReportDesignModelClient()
    fake = _FakeOpenAI()
    client._client = fake

    from app.services import report_diagnostics_service as diagnostics

    asyncio.run(
        client._complete(
            [{"role": "user", "content": "x"}],
            diagnostics.ensure_model_call_state(),
        )
    )
    assert fake.requests, "没有向上游发请求"
    request = fake.requests[-1]
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert request["model"] == settings.design_model


def test_context_engine_sends_thinking_disabled(monkeypatch):
    """接待员/路由器：extra_body 逐请求下发。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "minimax")
    engine = ContextEngine()
    fake = _FakeStreamingOpenAI(['{"intent":', '"new_report"}'])
    engine._client = fake

    asyncio.run(
        engine._chat_json_stream(
            model=settings.context_router_model,
            temperature=0,
            messages=[{"role": "user", "content": "x"}],
            response_format={"type": "json_object"},
            timeout=30,
            emit=None,
            chunk_stage="test_chunk",
        )
    )
    assert fake.requests, "没有向上游发请求"
    request = fake.requests[-1]
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert request["model"] == settings.context_router_model


def test_agentscope_model_carries_extra_body_and_base_url(monkeypatch):
    """AgentScope 路径：base_url 是 API 根地址，extra_body 挂在模型上。

    AgentScope 2.0 的 ``OpenAIChatModel`` 会把 ``extra_body`` 逐请求下发，
    所以这里断言模型实例上确实带着它 —— 不是「只在普通 SDK 路径加了」。
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "minimax")
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")

    from app.agents.agentscope_report_agent import AgentScopeReportAgent

    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._settings = settings
    model, err = agent._init_model()
    assert err is None, f"模型初始化失败：{err}"
    assert model is not None
    assert model.extra_body == {"thinking": {"type": "disabled"}}
    base_url = str(model.client.base_url)
    assert base_url.rstrip("/").endswith("/v1"), base_url
    assert "chat/completions" not in base_url, base_url


def test_agentscope_model_builds_against_volcengine_path(monkeypatch):
    """显式切回方舟时，AgentScope 也能正确构造（不带 thinking 字段）。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "volcengine")
    monkeypatch.setattr(settings, "volc_api_key", "ark-test")
    monkeypatch.setattr(settings, "volc_base_url", "https://ark.cn-beijing.volces.com/api/v3")

    from app.agents.agentscope_report_agent import AgentScopeReportAgent

    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._settings = settings
    model, err = agent._init_model()
    assert err is None, f"模型初始化失败：{err}"
    assert model.extra_body == {}
    assert "volces.com" in str(model.client.base_url)


# --------------------------------------------------------------------------
# 3. 缺配置 / 上游失败：明确失败，不静默降级
# --------------------------------------------------------------------------


def test_agentscope_agent_without_key_fails_explicitly(monkeypatch):
    """没配 key：明确失败（MODEL_UNAVAILABLE），不返回 FakeAgent 的回复。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "minimax")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "minimax_api_key", "")
    monkeypatch.setattr(settings, "volc_api_key", "")

    from app.agents.agentscope_report_agent import AgentScopeReportAgent
    from app.schemas.agent import AgentRequest, AgentRequestContext, AgentState

    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._settings = settings
    agent._model, agent._init_err = agent._init_model()
    assert agent._model is None
    assert "未配置" in agent._init_err

    request = AgentRequest(
        agentProtocolVersion="1.0",
        conversationId="conv_no_key",
        state=AgentState.ANALYZING,
        message="生成昨日能耗日报",
        context=AgentRequestContext(
            projectId="project_001",
            subjectRef="machine_room",
            subjectId="room_a",
        ),
    )
    response = asyncio.run(agent.ahandle(request))
    assert response.error is not None
    assert response.error.code == "MODEL_UNAVAILABLE"
    assert "没有生成结果" in response.reply


def test_report_design_client_without_key_raises(monkeypatch):
    """方案设计没配 key：抛错，不静默返回空文本。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "minimax_api_key", "")
    monkeypatch.setattr(settings, "volc_api_key", "")

    client = ReportDesignModelClient()
    assert client._client is None
    with pytest.raises(RuntimeError):
        asyncio.run(client([{"role": "user", "content": "x"}]))


def test_context_engine_without_client_fails_in_real_model_mode(monkeypatch):
    """真实模型模式：路由层没有模型客户端时明确失败，不退成规则路由。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_impl", "agentscope")
    engine = ContextEngine()
    engine._client = None

    from app.schemas.agent import AgentRequest, AgentRequestContext, AgentState

    request = AgentRequest(
        agentProtocolVersion="1.0",
        conversationId="conv_no_client",
        state=AgentState.ANALYZING,
        message="生成 PUE 报表",
        context=AgentRequestContext(
            projectId="project_001",
            subjectRef="machine_room",
            subjectId="room_a",
        ),
    )
    with pytest.raises(RuntimeError):
        asyncio.run(engine.route(request))


def test_context_engine_without_client_keeps_rule_fallback_in_fake_mode(monkeypatch):
    """fake 模式（显式测试）：没有模型就是没有模型，规则路由照旧。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_impl", "fake")
    engine = ContextEngine()
    engine._client = None

    from app.schemas.agent import AgentRequest, AgentRequestContext, AgentState

    request = AgentRequest(
        agentProtocolVersion="1.0",
        conversationId="conv_fake",
        state=AgentState.ANALYZING,
        message="生成 PUE 报表",
        context=AgentRequestContext(
            projectId="project_001",
            subjectRef="machine_room",
            subjectId="room_a",
        ),
    )
    decision = asyncio.run(engine.route(request))
    assert decision.engineStage == "fallback_no_client"
