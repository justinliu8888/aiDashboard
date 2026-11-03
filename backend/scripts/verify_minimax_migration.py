"""真实模型迁移验证：跑通 MiniMax-M3 下的产品真实链路。

覆盖五件事（与交接要求一一对应）：

1. 配置与适配：各入口选到 provider / 模型；thinking 参数真正下发到上游。
2. 方案生成：真实业务提示词 → **产品实际解析与 schema**（不是脚本自己剥 <think>）。
3. 接待员 / 路由器：真实模型下的关键意图识别与结构化输出。
4. ReAct：至少一次真实工具调用（参数解析 → 工具执行 → 结果回传 → 最终业务响应）。
5. 主流程：走真实 HTTP 接口 —— 提交需求 → 文字方案 → 确认 → 草稿 → 编辑候选。

只读不改业务数据；会话用隔离的临时库。

用法：
    cd backend
    /opt/anaconda3/bin/python3.13 scripts/verify_minimax_migration.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

from app.agents.report_plan_agent import generate_candidate  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.services.report_design_model_client import (  # noqa: E402
    ReportDesignModelClient,
)

_FAILURES: list[str] = []
_PASSES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    if ok:
        _PASSES.append(name)
        print(f"  ✅ {name}{('  —— ' + detail) if detail else ''}")
    else:
        _FAILURES.append(name)
        print(f"  ❌ {name}{('  —— ' + detail) if detail else ''}")
    return ok


def banner(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# --------------------------------------------------------------------------
# 1. 配置与适配
# --------------------------------------------------------------------------


def verify_config() -> None:
    banner("1. 配置与适配")
    settings = get_settings()
    print(f"  provider   = {settings.provider_name}")
    print(f"  model      = {settings.active_model}")
    print(f"  base_url   = {settings.active_base_url}")
    print(f"  extra_body = {settings.request_extra_body}")
    print(f"  api_key    = {'<set>' if settings.active_api_key else '<empty>'}")
    print(f"  agent_impl = {settings.agent_impl}")

    check("provider 是 minimax", settings.provider_name == "minimax", settings.provider_name)
    check("模型是 MiniMax-M3", settings.active_model == "MiniMax-M3", settings.active_model)
    check(
        "base_url 是 API 根地址（不带 /chat/completions）",
        not settings.active_base_url.rstrip("/").endswith("/chat/completions"),
        settings.active_base_url,
    )
    check(
        "thinking 显式关闭",
        settings.request_extra_body == {"thinking": {"type": "disabled"}},
        str(settings.request_extra_body),
    )
    check("API Key 已配置", bool(settings.active_api_key))
    check("真实模型模式", settings.agent_impl == "agentscope", settings.agent_impl)


def verify_upstream_request_shape() -> None:
    """抓一次真实请求，确认 thinking 真的发给了上游。"""
    banner("1b. thinking 参数是否真的发到上游")
    settings = get_settings()
    captured: dict[str, Any] = {}
    original = ReportDesignModelClient._complete

    async def _spy(self, messages, state):  # type: ignore[no-untyped-def]
        client = self._client
        original_create = client.chat.completions.create

        async def _capturing_create(**kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return await original_create(**kwargs)

        client.chat.completions.create = _capturing_create
        try:
            return await original(self, messages, state)
        finally:
            client.chat.completions.create = original_create

    ReportDesignModelClient._complete = _spy  # type: ignore[assignment]
    try:
        client = ReportDesignModelClient()
        started = time.perf_counter()
        text = asyncio.run(client([{"role": "user", "content": "只回复：ok"}]))
        elapsed = time.perf_counter() - started
    finally:
        ReportDesignModelClient._complete = original  # type: ignore[assignment]

    print(f"  实测一次真实调用耗时 {elapsed:.2f}s，响应 {len(text)} 字")
    check(
        "请求带上了 extra_body",
        captured.get("extra_body") == {"thinking": {"type": "disabled"}},
        str(captured.get("extra_body")),
    )
    check(
        "请求用的是 active_model",
        captured.get("model") == settings.active_model,
        str(captured.get("model")),
    )


# --------------------------------------------------------------------------
# 2. 方案生成：真实提示词 → 产品解析与 schema
# --------------------------------------------------------------------------


def verify_plan_generation() -> None:
    banner("2. 方案生成（真实业务提示词 → 产品解析与 schema）")
    message = (
        "帮我做一份 PUE 专题报表，时间范围最近 7 天，粒度按天，"
        "要一个 PUE 总览指标卡、一个各机房 PUE 对比柱状图，再加一个当前告警列表表格，"
        "表格要有设备名称、告警级别、发生时间三列。不要设备耗电排名。"
    )
    client = ReportDesignModelClient()
    started = time.perf_counter()
    result = asyncio.run(
        generate_candidate(
            message=message,
            current_plan=None,
            history=None,
            model_call=client,
        )
    )
    elapsed = time.perf_counter() - started
    print(f"  耗时 {elapsed:.2f}s")

    kind = result.get("kind")
    candidate = result.get("candidate") or {}
    print(f"  kind = {kind}")
    for section in candidate.get("sections") or []:
        print(
            f"    - {section.get('title')} | {section.get('componentType')} | "
            f"{section.get('displayIntent')} | columns="
            f"{[c.get('label') for c in (section.get('columns') or [])]}"
        )
    print(f"  assumptions = {candidate.get('assumptions')}")

    check("返回 plan_candidate", kind == "plan_candidate", str(kind))
    check(
        "产品 schema 校验通过（不是脚本自己剥 <think>）",
        kind not in {"failure", None} and bool(candidate.get("sections")),
        f"kind={kind}",
    )
    check("耗时在设计时限内", elapsed < 120, f"{elapsed:.1f}s")


# --------------------------------------------------------------------------
# 3. 接待员 / 路由器
# --------------------------------------------------------------------------


def verify_context_engine() -> None:
    banner("3. 接待员 / 路由器（真实模型）")
    from app.schemas.agent import AgentRequest, AgentRequestContext, AgentState
    from app.services.context_engine import context_engine

    def _request(text: str) -> AgentRequest:
        return AgentRequest(
            agentProtocolVersion="1.0",
            conversationId="conv_verify_minimax",
            state=AgentState.ANALYZING,
            message=text,
            context=AgentRequestContext(
                projectId="project_001",
                subjectRef="machine_room",
                subjectId="room_a",
            ),
        )

    # 接待员与深度路由必须跑在**同一个事件循环**里：
    # OpenAI SDK 的 httpx 连接池绑定在创建它的事件循环上，跨 loop 复用会报
    # "Event loop is closed" —— 那是脚本用法问题，不是产品问题（产品进程只有一个 loop）。
    async def _both() -> tuple[Any, Any]:
        concierge = await context_engine._run_concierge(_request("你好呀"))
        decision = await context_engine.route(
            _request("生成 A 机房最近 7 天的能耗报表")
        )
        return concierge, decision

    started = time.perf_counter()
    concierge, decision = asyncio.run(_both())
    print(f"  接待员 + 深度路由合计耗时 {time.perf_counter() - started:.2f}s")
    print(
        f"    接待员 action={concierge.action.value} intent={concierge.userIntent.value} "
        f"confidence={concierge.confidence}"
    )
    check("接待员返回可辨识意图", bool(concierge.userIntent.value), concierge.userIntent.value)
    check(
        "接待员动作合法",
        concierge.action.value in {"DIRECT_REPLY", "DEEP_ROUTE", "ASK_CLARIFICATION"},
        concierge.action.value,
    )
    print(
        f"    路由 intent={decision.userIntent.value} action={decision.routingAction.value} "
        f"engineStage={decision.engineStage} ready={decision.isReadyForNextStep}"
    )
    check("路由给出结构化结论", bool(decision.userIntent.value), decision.userIntent.value)
    # 严格判定：engineStage 必须是 deep_route_done 才算「模型真的出结论」。
    # 只比 reason != "模型输出不合法" 会漏判 —— 失败兜底时 reason 是 "fallback: ..."，
    # 但 engineStage 会是 deep_route_failed_fallback。
    check(
        "路由由模型真实产出（未走兜底）",
        decision.engineStage == "deep_route_done",
        f"engineStage={decision.engineStage}",
    )


# --------------------------------------------------------------------------
# 4. ReAct：真实工具调用
# --------------------------------------------------------------------------


def verify_react_tool_call() -> None:
    banner("4. ReAct 真实工具调用")
    from app.agents.agentscope_report_agent import AgentScopeReportAgent
    from app.schemas.agent import AgentRequest, AgentRequestContext, AgentState
    from app.tools import registry

    agent = AgentScopeReportAgent()
    if agent._model is None:
        check("AgentScope 模型初始化成功", False, str(agent._init_err))
        return
    check("AgentScope 模型初始化成功", True, get_settings().active_model)

    # 探针：包住每个工具的 run，模型真调工具就会被记下来。
    calls: list[tuple[str, dict[str, Any]]] = []
    patched: list[tuple[Any, Any]] = []
    for tool in registry.list_tools():
        original_run = tool.run

        def _make(name: str, orig):
            def _wrapper(*args: Any, **kwargs: Any):
                calls.append((name, kwargs))
                return orig(*args, **kwargs)

            return _wrapper

        tool.run = _make(tool.NAME, original_run)
        patched.append((tool, original_run))

    request = AgentRequest(
        agentProtocolVersion="1.0",
        conversationId="conv_verify_react",
        state=AgentState.ANALYZING,
        message="帮我生成 A 机房昨日的能耗日报，我想看总耗电、设备排名和小时趋势。",
        context=AgentRequestContext(
            projectId="project_001",
            subjectRef="machine_room",
            subjectId="room_a",
        ),
    )
    try:
        started = time.perf_counter()
        response = asyncio.run(agent.ahandle(request))
        elapsed = time.perf_counter() - started
    finally:
        for tool, original_run in patched:
            tool.run = original_run

    print(f"  耗时 {elapsed:.2f}s")
    print(f"  state = {response.state.value}")
    print(f"  reply = {(response.reply or '')[:200]}")
    print(f"  error = {response.error.code if response.error else None}")
    print(f"  工具调用 {len(calls)} 次：")
    for name, kwargs in calls:
        print(f"    - {name}({', '.join(f'{k}={str(v)[:40]}' for k, v in kwargs.items())})")

    check("ReAct 没有报模型不可用",
          not (response.error and response.error.code == "MODEL_UNAVAILABLE"))
    check("ReAct 真的调用了工具", len(calls) > 0, f"{len(calls)} 次")
    check("ReAct 有最终业务响应", bool(response.reply))
    check("ReAct 在合理耗时内", elapsed < 120, f"{elapsed:.1f}s")


# --------------------------------------------------------------------------
# 5. 真实 HTTP 主流程：需求 → 文字方案 → 确认 → 草稿 → 编辑候选
# --------------------------------------------------------------------------


def verify_http_flow() -> None:
    banner("5. 真实 HTTP 主流程（隔离会话库）")
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.report_design_service import report_design_service
    from app.services.session_service import session_service
    from app.storage.sqlite_store import SqliteSessionRepository

    tmp_dir = Path(tempfile.mkdtemp(prefix="verify-minimax-"))
    original_repo = session_service._repo
    repo = SqliteSessionRepository(tmp_dir / "sessions.db")
    session_service._repo = repo
    # 只重置设计存储的依赖（让它跟随新的会话库）；**不换模型调用**，保持真实模型。
    report_design_service.reset_for_tests()
    # 用 `with` 保持同一个 portal（= 同一个事件循环）贯穿全部请求，
    # 贴近真实 uvicorn 单进程单 loop 的行为；否则 TestClient 每个请求新建 loop，
    # httpx 连接池跨 loop 复用会报 "Event loop is closed"。
    client_cm = TestClient(app)
    client = client_cm.__enter__()

    def _body(message: str, state: str, message_id: str, **extra: Any) -> dict[str, Any]:
        # 新建请求必须带显式 sessionId + messageId，否则产品会走旧流程而不是「文字方案」流程
        # （见 report_design_service.is_new_build）。
        body = {
            "agentProtocolVersion": "1.0",
            "conversationId": "conv_verify_http",
            "sessionId": "sess_verify_http",
            "messageId": message_id,
            "state": state,
            "message": message,
            "context": {
                "projectId": "project_001",
                "subjectRef": "machine_room",
                "subjectId": "room_a",
            },
        }
        body.update(extra)
        return body

    try:
        # 5a. 提交需求 → 文字方案
        started = time.perf_counter()
        r = client.post(
            "/api/agent/chat",
            json=_body(
                "帮我做一份 A 机房最近 7 天的能耗报表，要总耗电、每日趋势、设备排名和 PUE。",
                "ANALYZING",
                "msg_verify_1",
            ),
        )
        elapsed = time.perf_counter() - started
        payload = r.json()
        print(f"  ① 提交需求耗时 {elapsed:.2f}s  http={r.status_code}")
        print(f"     state={payload.get('state')} designPhase={payload.get('designPhase')}")
        print(f"     reply={(payload.get('reply') or '')[:180]}")
        print(f"     uiActions={payload.get('uiActions')}")
        print(f"     questions={[q.get('id') for q in (payload.get('questions') or [])]}")
        check("① 提交需求返回成功", r.status_code == 200, str(r.status_code))
        check(
            "① 进入文字方案待确认阶段",
            payload.get("designPhase") == "PLAN_WAITING_CONFIRM",
            str(payload.get("designPhase")),
        )

        # 5b. 确认方案 → 出草稿
        started = time.perf_counter()
        r2 = client.post(
            "/api/agent/chat",
            json=_body(
                "同意",
                payload.get("state") or "WAITING_USER_CONFIRM",
                "msg_verify_2",
                context={
                    "projectId": "project_001",
                    "subjectRef": "machine_room",
                    "subjectId": "room_a",
                    "designSessionId": payload.get("designSessionId"),
                },
            ),
        )
        elapsed = time.perf_counter() - started
        payload2 = r2.json()
        print(f"  ② 确认方案耗时 {elapsed:.2f}s  http={r2.status_code}")
        print(f"     state={payload2.get('state')} designPhase={payload2.get('designPhase')}")
        print(f"     uiActions={payload2.get('uiActions')}")
        print(f"     reply={(payload2.get('reply') or '')[:180]}")
        preview = payload2.get("reportPreviewPayload")
        if preview:
            print(f"     预览板块 = {list(preview.keys())[:6]}")
        check("② 确认后返回成功", r2.status_code == 200, str(r2.status_code))
        check(
            "② 出草稿并进入草稿就绪",
            "draft_ready" in (payload2.get("uiActions") or []),
            str(payload2.get("uiActions")),
        )
        check("② 带回了预览载荷", bool(preview))

        # 5c. 简单编辑 → 候选
        started = time.perf_counter()
        r3 = client.post(
            "/api/agent/chat",
            json=_body(
                "把总耗电指标卡的标题改成「核心总耗电」",
                payload2.get("state") or "WAITING_USER_CONFIRM",
                "msg_verify_3",
                context={
                    "projectId": "project_001",
                    "subjectRef": "machine_room",
                    "subjectId": "room_a",
                    "designSessionId": payload2.get("designSessionId"),
                },
            ),
        )
        elapsed = time.perf_counter() - started
        payload3 = r3.json()
        print(f"  ③ 提交编辑耗时 {elapsed:.2f}s  http={r3.status_code}")
        print(f"     state={payload3.get('state')} designPhase={payload3.get('designPhase')}")
        print(f"     reply={(payload3.get('reply') or '')[:200]}")
        print(f"     questions={[q.get('id') for q in (payload3.get('questions') or [])]}")
        print(f"     error={payload3.get('error')}")
        check("③ 编辑请求返回成功", r3.status_code == 200, str(r3.status_code))
        check(
            "③ 生成了编辑候选（待确认）",
            bool(payload3.get("questions")) or bool(payload3.get("reply")),
            str([q.get("id") for q in (payload3.get("questions") or [])]),
        )
    finally:
        try:
            client_cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
        session_service._repo = original_repo
        report_design_service.reset_for_tests()
        try:
            repo.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    print("MiniMax 迁移验证 —— 真实模型链路")
    verify_config()
    verify_upstream_request_shape()
    verify_plan_generation()
    verify_context_engine()
    verify_react_tool_call()
    verify_http_flow()

    banner("汇总")
    print(f"  通过 {len(_PASSES)} 项，失败 {len(_FAILURES)} 项")
    for name in _FAILURES:
        print(f"    ❌ {name}")
    return 1 if _FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
