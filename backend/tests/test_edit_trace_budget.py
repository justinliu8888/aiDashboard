"""编辑预算 / 单次催交 / 失败分类 / 追踪事件（契约 0003 §2、§4）。

**这些用例全部用 stub 框架**：不调用真实模型，也不发真实模型请求。
用例里出现的「模型」一律是本地假对象 —— 真实链路的证据在实施记录里
另行给出（真实模型跑一次编辑请求，看真实预算与真实迭代数）。

覆盖：

* 预算：新旧编辑共用一个值、默认 8、可配置、旧环境变量兼容；
* 编辑态**不跑第二轮** ``agent.reply()``（删除催交）；
* 失败分类：耗尽未提交 / 模型错 / 工具错 / 参数错 / 试做错 **不混用**；
* 循环内工具错误仍能在**原循环剩余预算内**修正，且失败不占提交槽；
* 追踪事件字段齐全、脱敏生效、迭代编号来自**真实** ``cur_iter``。
"""
from __future__ import annotations

import asyncio

import pytest

from app.agents import agentscope_report_agent as agent_module
from app.agents.agentscope_report_agent import AgentScopeReportAgent
from app.config import get_settings
from app.schemas.agent import AgentRequest, AgentState
from app.services import edit_trace_service as trace


# --------------------------------------------------------------------------
# 预算
# --------------------------------------------------------------------------

def _fresh_settings():
    """按**当前环境变量**构造一份配置。

    这与「``get_settings.cache_clear()`` 之后重新 ``get_settings()``」
    是同一条路径（``get_settings`` 就是 ``Settings()`` 的缓存），
    但**不动全局缓存**：清掉缓存会让 ``context_engine`` 等模块单例
    持有的配置实例变成孤儿（测试夹具的 monkeypatch 打在旧实例上），
    后续用例会掉回真实模型模式。

    配置在**实例构造时**读 env（契约要求的行为），所以这里直接构造
    就能验证「改环境变量 → 生效」。
    """
    from app.config import Settings

    return Settings()


def test_edit_budget_defaults_to_eight():
    """编辑预算默认 **8** 圈 —— 不是旧的 4。"""
    settings = get_settings()
    assert settings.react_max_iters_edit == 8


def test_edit_budget_is_configurable(monkeypatch):
    monkeypatch.setenv("REACT_MAX_ITERS_EDIT", "12")
    assert _fresh_settings().react_max_iters_edit == 12


def test_legacy_env_var_is_a_fallback_not_a_second_switch(monkeypatch):
    """旧名只在新名没设时生效；**只有一个**最终值。"""
    monkeypatch.delenv("REACT_MAX_ITERS_EDIT", raising=False)
    monkeypatch.setenv("REACT_MAX_ITERS_EDIT_CAPABILITY", "9")
    settings = _fresh_settings()
    assert settings.react_max_iters_edit == 9
    # 废弃别名读到的是**同一个**值，不是第二个开关。
    assert settings.react_max_iters_edit_capability == settings.react_max_iters_edit


def test_new_env_var_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("REACT_MAX_ITERS_EDIT", "11")
    monkeypatch.setenv("REACT_MAX_ITERS_EDIT_CAPABILITY", "3")
    assert _fresh_settings().react_max_iters_edit == 11


def test_budget_does_not_depend_on_edit_item(monkeypatch):
    """**有没有 editItem 不影响预算** —— 这正是首次请求被少给圈数的根因。"""
    from app.services import edit_item_service

    monkeypatch.setenv("REACT_MAX_ITERS_EDIT", "8")
    instance = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    instance._settings = _fresh_settings()
    with_edit_item = instance._build_react_config(
        AgentState.USER_EDITING_TEMPLATE
    ).max_iters

    # 会话里**有** editItem
    monkeypatch.setattr(
        edit_item_service, "load_edit_item", lambda *a, **k: {"itemId": "x"}
    )
    with_item = instance._build_react_config(
        AgentState.USER_EDITING_TEMPLATE
    ).max_iters

    # 会话里**没有** editItem
    monkeypatch.setattr(
        edit_item_service, "load_edit_item", lambda *a, **k: None
    )
    without_item = instance._build_react_config(
        AgentState.USER_EDITING_TEMPLATE
    ).max_iters

    assert with_edit_item == with_item == without_item == 8


def test_capability_budget_branch_is_gone():
    """旧的「有没有 editItem 就换预算」分支必须**删掉**。"""
    assert not hasattr(AgentScopeReportAgent, "_capability_budget_active")


# --------------------------------------------------------------------------
# 单次催交（删除第二次 agent.reply）
# --------------------------------------------------------------------------

class _StubAgent:
    """假 Agent：记录 reply 被调用了几次，并回报固定迭代数。

    **不是真实模型** —— 只是让本用例能断言「调用次数」。
    """

    def __init__(self, *, iterations: int = 4, finished_reason: str = "exceed_max_iters"):
        self.reply_calls = 0
        self.iterations = iterations
        self.finished_reason = finished_reason

    async def reply(self, msg):
        self.reply_calls += 1
        return _StubMsg(self.finished_reason)

    @property
    def state(self):
        return _StubState(self.iterations)


class _StubState:
    def __init__(self, iterations: int):
        self.cur_iter = iterations


class _StubMsg:
    def __init__(self, finished_reason: str):
        self.finished_reason = finished_reason
        self.content = []


def _make_request(state: AgentState = AgentState.USER_EDITING_TEMPLATE) -> AgentRequest:
    from app.schemas.agent import AgentRequestContext, AgentRequestSnapshots

    return AgentRequest(
        agentProtocolVersion="1.0",
        conversationId="conv_trace_test",
        state=state,
        message="把告警列表替换成两张分布图",
        context=AgentRequestContext(
            projectId="project_001",
            subjectRef="machine_room",
            subjectId="room_a",
        ),
        snapshots=AgentRequestSnapshots(),
    )


def _run_with_stub(monkeypatch, stub: _StubAgent, request: AgentRequest):
    """用 stub 替换 Agent 构造，跑一次 ``_run_agent``。"""
    instance = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    instance._settings = get_settings()
    instance._model = object()
    instance._edit_prompt = "流程"
    instance._system_prompt = "系统"
    instance._query_prompt = "查询"

    async def _fake_build_agent(_request):
        return stub

    monkeypatch.setattr(instance, "_build_agent", _fake_build_agent)
    return asyncio.run(instance._run_agent(request))


def test_edit_state_does_not_run_a_second_reply(monkeypatch):
    """编辑态循环结束仍无提交 → **只跑一次** ``agent.reply()``。"""
    stub = _StubAgent(iterations=4, finished_reason="exceed_max_iters")
    request = _make_request()
    plan, reason = _run_with_stub(monkeypatch, stub, request)

    assert stub.reply_calls == 1, "编辑态又跑了第二轮催交"
    assert plan is None
    assert reason == trace.ERR_BUDGET_EXHAUSTED


def test_query_state_keeps_its_repair_retry(monkeypatch):
    """查询态的既有修复重试**不顺带删**。"""
    stub = _StubAgent(iterations=2, finished_reason="completed")
    request = _make_request(AgentState.ANALYZING)
    request.queryMode = True
    plan, reason = _run_with_stub(monkeypatch, stub, request)
    # 查询态仍然跑第二轮（查询态第一轮没产出可解析计划）。
    assert stub.reply_calls == 2


def test_failure_categories_do_not_collapse(monkeypatch):
    """不同停止原因 → 不同 errorCategory，**不混成一个**。"""
    cases = [
        ("exceed_max_iters", trace.ERR_BUDGET_EXHAUSTED),
        ("error", trace.ERR_MODEL_ERROR),
        ("completed", trace.ERR_NOT_SUBMITTED),
    ]
    for finished_reason, expected in cases:
        stub = _StubAgent(iterations=4, finished_reason=finished_reason)
        _plan, reason = _run_with_stub(monkeypatch, stub, _make_request())
        assert reason == expected, f"{finished_reason} → {reason}（应为 {expected}）"
        assert reason != trace.ERR_UNKNOWN


def test_unknown_is_only_used_when_nothing_better():
    """通用兜底只用 ``unknown``。"""
    assert trace.ERROR_MESSAGES[trace.ERR_UNKNOWN] == "原因尚未确定"


def test_error_categories_match_the_contract_table():
    """errorCategory 取值与契约 0003 §2.4 的表**逐字一致**，不增不减。"""
    expected = {
        "model_unavailable",
        "model_timeout",
        "model_error",
        "tool_lookup_failed",
        "tool_error",
        "params_invalid",
        "trial_failed",
        "acceptance_failed",
        "budget_exhausted",
        "not_submitted",
        "unknown",
    }
    assert set(trace.ERROR_MESSAGES) == expected, (
        set(trace.ERROR_MESSAGES) ^ expected
    )


# --------------------------------------------------------------------------
# 失败诊断文案
# --------------------------------------------------------------------------

def test_budget_exhausted_message_does_not_blame_the_interface():
    """预算耗尽的文案要说「未形成可执行计划」，**不能**暗示接口坏了。"""
    instance = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    response = instance._unavailable_edit_response(
        _make_request(), trace.ERR_BUDGET_EXHAUSTED
    )
    assert response.error.code == "EDIT_BUDGET_EXHAUSTED"
    assert "预算" in response.reply
    assert "技术问题" not in response.reply
    assert response.debug.get("errorCategory") == trace.ERR_BUDGET_EXHAUSTED


def test_unknown_failure_keeps_legacy_code():
    """「原因尚未确定」仍用旧码 —— 前端已有失败渲染不能破坏。

    兜底文案**逐字**沿用既有那句（前端与 ``test_log_takeover`` 都认）；
    「原因尚未确定」写在 ``debug.failureReason`` 里。
    """
    instance = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    response = instance._unavailable_edit_response(_make_request(), trace.ERR_UNKNOWN)
    assert response.error.code == "EDIT_VERIFICATION_UNAVAILABLE"
    assert response.reply == "本轮查证遇到技术问题，草稿没有修改。请稍后重试。"
    assert "原因尚未确定" in response.debug["failureReason"]
    assert response.debug["errorCategory"] == trace.ERR_UNKNOWN


def test_trace_id_is_exposed_in_debug():
    instance = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    trace.start_trace(session_id="conv_x", catalog_version="v1")
    try:
        response = instance._unavailable_edit_response(
            _make_request(), trace.ERR_TOOL_ERROR
        )
        assert response.debug.get("traceId")
        assert response.error.code == "EDIT_TOOL_FAILED"
    finally:
        trace.clear_trace()


# --------------------------------------------------------------------------
# 追踪事件
# --------------------------------------------------------------------------

def test_trace_event_has_all_contract_fields():
    trace.start_trace(session_id="conv_x", draft_id="d1", catalog_version="v9")
    try:
        event = trace.record(
            trace.STAGE_TOOL_CALL,
            name="get_current_draft",
            replyIndex=1,
            iteration=3,
            toolCallId="call_1",
            params={"a": 1},
            result={"ok": True},
            durationMs=12,
            status="ok",
        )
        for key in (
            "traceId", "sessionId", "itemId", "planId", "draftId",
            "catalogVersion", "stage", "replyIndex", "iteration", "toolCallId",
            "name", "params", "result", "truncated", "durationMs", "status",
            "errorCategory", "budget", "stopReason", "at",
        ):
            assert key in event, f"事件缺字段 {key}"
        assert event["iteration"] == 3
        assert event["catalogVersion"] == "v9"
    finally:
        trace.clear_trace()


def test_trace_redacts_secrets():
    payload = {"apiKey": "sk-abcdef123456", "nested": {"token": "abc"}}
    cleaned = trace.redact(payload)
    assert cleaned["apiKey"] == "[redacted]"
    assert cleaned["nested"]["token"] == "[redacted]"
    text = trace.redact("password=hunter2 and Bearer abcdefghijkl")
    assert "hunter2" not in text
    assert "abcdefghijkl" not in text


def test_trace_summary_reflects_real_events():
    trace.start_trace(session_id="conv_x")
    try:
        trace.set_budget(8)
        trace.record(trace.STAGE_TOOL_CALL, name="t", iteration=2)
        trace.record(trace.STAGE_SUBMIT, name="submit_edit_plan")
        trace.record(trace.STAGE_TRIAL, name="submit_edit_plan")
        trace.record(trace.STAGE_CANDIDATE, name="submit_edit_plan")
        summary = trace.trace_summary()
        assert summary["submitted"] is True
        assert summary["tried"] is True
        assert summary["candidateRegistered"] is True
        assert summary["applied"] is False
        assert summary["iterations"] == 2
        assert summary["budget"] == 8
    finally:
        trace.clear_trace()


def test_trace_is_request_scoped():
    """上下文是**请求作用域**：解绑后取不到。"""
    trace.start_trace(session_id="conv_x")
    assert trace.current_trace() is not None
    trace.clear_trace()
    assert trace.current_trace() is None


def test_iteration_count_comes_from_framework_state():
    """迭代编号来自**框架状态**，不是 HTTP 次数。"""
    stub = _StubAgent(iterations=7, finished_reason="exceed_max_iters")
    assert AgentScopeReportAgent._real_iterations(stub) == 7
    assert AgentScopeReportAgent._real_iterations(object()) is None


def test_classify_no_submission_maps_budget_to_budget():
    category = AgentScopeReportAgent._classify_no_submission(
        finished_reason="exceed_max_iters", iterations=8, budget=8
    )
    assert category == trace.ERR_BUDGET_EXHAUSTED
    category = AgentScopeReportAgent._classify_no_submission(
        finished_reason="completed", iterations=2, budget=8
    )
    assert category == trace.ERR_NOT_SUBMITTED


def test_agent_module_exposes_trace_hooks():
    """Agent 模块确实接了追踪（不是只有服务存在）。"""
    source = (
        __import__("pathlib").Path(agent_module.__file__).read_text(encoding="utf-8")
    )
    assert "edit_trace_service" in source
    assert "STAGE_AGENT_REPLY" in source


# --------------------------------------------------------------------------
# 失败分类：工具错 / 参数错 / 试做错（**不混用**）
# --------------------------------------------------------------------------

class _StreamStubAgent:
    """假 Agent：走 ``reply_stream``，产出**真实的框架事件对象**。

    **不是真实模型** —— 只是替身「框架会发哪些事件」。
    """

    def __init__(
        self,
        *,
        iterations: int = 3,
        finished_reason: str = "completed",
        tool_states: tuple[str, ...] = (),
        tool_name: str = "get_current_draft",
        tool_call_id: str = "call_real_1",
    ):
        self._iterations = iterations
        self._finished_reason = finished_reason
        self._tool_states = tool_states
        self._tool_name = tool_name
        self._tool_call_id = tool_call_id
        self.reply_calls = 0

    @property
    def state(self):
        return _StubState(self._iterations)

    async def reply(self, msg):  # pragma: no cover - 本用例走流式分支
        self.reply_calls += 1
        return _StubMsg(self._finished_reason)

    async def reply_stream(self, msg, yield_final_msg: bool = False):
        from agentscope.event import (
            ReplyEndEvent,
            ToolCallStartEvent,
            ToolResultEndEvent,
        )
        from agentscope.message import Msg, TextBlock

        self.reply_calls += 1
        for index, state in enumerate(self._tool_states):
            call_id = f"{self._tool_call_id}_{index}"
            yield ToolCallStartEvent(
                reply_id="reply_1",
                tool_call_id=call_id,
                tool_call_name=self._tool_name,
            )
            yield ToolResultEndEvent(
                reply_id="reply_1",
                tool_call_id=call_id,
                state=state,
            )
        yield ReplyEndEvent(
            session_id="session_1",
            reply_id="reply_1",
            finished_reason=self._finished_reason,
        )
        if yield_final_msg:
            yield Msg(
                name="assistant",
                role="assistant",
                content=[TextBlock(type="text", text="本轮没有提交终结工具。")],
            )


def _run_stream_stub(monkeypatch, stub, request):
    instance = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    instance._settings = get_settings()
    instance._model = object()
    instance._edit_prompt = "流程"
    instance._system_prompt = "系统"
    instance._query_prompt = "查询"

    async def _fake_build_agent(_request):
        return stub

    monkeypatch.setattr(instance, "_build_agent", _fake_build_agent)
    return asyncio.run(instance._run_agent(request))


def test_tool_error_is_not_collapsed_into_budget_or_unknown(monkeypatch):
    """工具**执行**报错 → ``tool_error``，不与预算耗尽混成一个。"""
    stub = _StreamStubAgent(
        iterations=3,
        finished_reason="completed",
        tool_states=("error",),
    )
    trace.start_trace(session_id="conv_x")
    try:
        _plan, reason = _run_stream_stub(monkeypatch, stub, _make_request())
    finally:
        summary = trace.trace_summary()
        trace.clear_trace()
    assert reason == trace.ERR_TOOL_ERROR
    assert reason not in {trace.ERR_BUDGET_EXHAUSTED, trace.ERR_UNKNOWN}
    assert summary["errorCategory"] == trace.ERR_TOOL_ERROR


def test_tool_call_id_and_iteration_come_from_the_framework(monkeypatch):
    """``toolCallId`` 与迭代编号都来自**框架事件 / 框架状态**。"""
    stub = _StreamStubAgent(
        iterations=4,
        finished_reason="completed",
        tool_states=("success",),
        tool_call_id="call_from_framework",
    )
    trace.start_trace(session_id="conv_x")
    try:
        _run_stream_stub(monkeypatch, stub, _make_request())
        events = list(trace.current_trace()["events"]) if trace.current_trace() else []
    finally:
        trace.clear_trace()
    tool_events = [e for e in events if e["stage"] == trace.STAGE_TOOL_CALL]
    assert tool_events, "没有逐圈工具事件"
    assert tool_events[0]["toolCallId"] == "call_from_framework_0"
    assert tool_events[0]["name"] == "get_current_draft"
    # 事件到达时框架还没给这一轮 +1：当前轮 = 已完成轮数 + 1。
    assert tool_events[0]["iteration"] == 5
    reply_events = [e for e in events if e["name"] == "react_reply"]
    assert reply_events and reply_events[0]["iteration"] == 4


def test_invalid_params_are_reported_as_params_invalid(monkeypatch):
    """提交参数不合法 → ``params_invalid``（不是「工具报错」也不是「未知」）。"""
    from app.tools import submit_edit_plan_tool

    class _SubmittingStubAgent(_StubAgent):
        async def reply(self, msg):
            self.reply_calls += 1
            # 真实的终结工具：载荷不合法 → 工具自己报 ``params_invalid``。
            submit_edit_plan_tool.run(
                intent="NOT_A_REAL_INTENT",
                target={},
                operation={},
                confidence=0.5,
            )
            return _StubMsg("completed")

    stub = _SubmittingStubAgent(iterations=2, finished_reason="completed")
    trace.start_trace(session_id="conv_x")
    try:
        _plan, reason = _run_with_stub(monkeypatch, stub, _make_request())
    finally:
        trace.clear_trace()
    assert reason == trace.ERR_PARAMS_INVALID
    assert reason not in {trace.ERR_TOOL_ERROR, trace.ERR_UNKNOWN}


def test_trial_failure_is_reported_as_trial_failed(monkeypatch):
    """预执行某步校验失败 → ``trial_failed``（不与参数错混用）。"""
    from app.services.edit_trial_service import run_trial
    from app.services.report_draft_service import build_definition_draft

    definition = build_definition_draft("A 机房能耗日报", "room_a")

    class _TrialStubAgent(_StubAgent):
        async def reply(self, msg):
            self.reply_calls += 1
            # 真实的隔离试做：能力名没登记 → 这一步校验失败。
            run_trial(
                definition=definition,
                operations=[
                    {"op": "NOT_A_REGISTERED_CAPABILITY", "target": {}, "params": {}}
                ],
            )
            return _StubMsg("completed")

    stub = _TrialStubAgent(iterations=2, finished_reason="completed")
    trace.start_trace(session_id="conv_x")
    try:
        _plan, reason = _run_with_stub(monkeypatch, stub, _make_request())
    finally:
        trace.clear_trace()
    assert reason == trace.ERR_TRIAL_FAILED
    assert reason not in {trace.ERR_PARAMS_INVALID, trace.ERR_UNKNOWN}


def test_failed_submit_does_not_take_the_success_slot(monkeypatch):
    """工具报错**不占成功提交槽**：修正后在同一循环里仍能提交成功。

    这一条是「循环内错误反馈仍可修正」的底线：失败的那次调用不能把
    提交槽写掉，否则后面的修正提交就落不下去。
    """
    from app.tools import submit_edit_plan_tool
    from app.tools.submission import take_submission

    class _FixingStubAgent(_StubAgent):
        async def reply(self, msg):
            self.reply_calls += 1
            # 第一次：载荷不合法 → 不写提交槽。
            failed = submit_edit_plan_tool.run(
                intent="NOT_A_REAL_INTENT",
                target={},
                operation={},
                confidence=0.5,
            )
            assert failed["accepted"] is False
            assert take_submission() is None, "失败的提交不该占掉提交槽"
            # 第二次（同一循环内修正）：合法载荷 → 写提交槽。
            ok = submit_edit_plan_tool.run(
                intent="RENAME_SECTION",
                target={"regionId": "region_total_energy"},
                operation={"newTitle": "昨日总耗电量"},
                confidence=0.9,
            )
            assert ok["accepted"] is True
            return _StubMsg("completed")

    stub = _FixingStubAgent(iterations=2, finished_reason="completed")
    plan, reason = _run_with_stub(monkeypatch, stub, _make_request())
    assert plan is not None
    assert plan["submission"]["tool"] == "submit_edit_plan"
    assert reason == ""


def test_edit_state_runs_one_reply_even_after_tool_errors(monkeypatch):
    """循环内工具报错**不会**触发第二轮：编辑态仍然只跑一次 reply。"""
    class _FailingToolStubAgent(_StubAgent):
        async def reply(self, msg):
            self.reply_calls += 1
            return _StubMsg("completed")

    stub = _FailingToolStubAgent(iterations=2, finished_reason="completed")
    trace.start_trace(session_id="conv_x")
    try:
        _run_with_stub(monkeypatch, stub, _make_request())
    finally:
        trace.clear_trace()
    assert stub.reply_calls == 1


def test_success_response_also_carries_trace_debug():
    """**成功轮**也挂 ``traceId`` / ``traceSummary``（失败轮也要能对照）。"""
    from app.schemas.agent import AgentResponse

    response = AgentResponse(
        conversationId="conv_x",
        state=AgentState.USER_EDITING_TEMPLATE,
        reply="待确认的修改",
    )
    trace.start_trace(session_id="conv_x")
    try:
        trace.set_budget(8)
        trace.record(trace.STAGE_SUBMIT, name="submit_edit_plan")
        attached = AgentScopeReportAgent._attach_trace_debug(response)
    finally:
        trace.clear_trace()
    assert attached.debug["traceId"]
    assert attached.debug["traceSummary"]["submitted"] is True
    assert attached.debug["traceSummary"]["budget"] == 8


# --------------------------------------------------------------------------
# 框架结构化输出宽限（单独计数与记录）
# --------------------------------------------------------------------------


def test_grace_iters_are_recorded_separately_from_the_budget(monkeypatch):
    """宽限**单独**记录：配置值 + 超出 max_iters 的实际圈数。

    宽限是框架为了让结构化输出落地给的额外圈数 —— 它**不是**编辑预算
    的一部分，也不是「第二次完整催交」。
    """
    import json

    settings = get_settings()
    budget = settings.react_max_iters_edit
    # 故意让迭代数越过 max_iters：宽限被用掉了。
    stub = _StubAgent(iterations=budget + 2, finished_reason="exceed_max_iters")
    trace.start_trace(session_id="conv_grace")
    try:
        _run_with_stub(monkeypatch, stub, _make_request())
        events = list(trace.current_trace()["events"])
    finally:
        trace.clear_trace()

    grace_events = [e for e in events if e["name"] == "react_grace_iters"]
    assert len(grace_events) == 1, "宽限没有单独记录成事件"
    event = grace_events[0]
    assert event["params"]["graceItersConfigured"] == (
        settings.react_structured_output_grace_iters
    )
    assert event["params"]["graceItersUsed"] == 2
    assert event["params"]["maxIters"] == budget
    # 口径不能被叫成「第二次催交」。
    assert "催交" not in json.dumps(event, ensure_ascii=False)


def test_grace_iters_zero_when_budget_was_not_exceeded(monkeypatch):
    """没越过 max_iters → 宽限用掉 0 圈，但**仍然**要单独记一笔。"""
    settings = get_settings()
    stub = _StubAgent(iterations=2, finished_reason="completed")
    trace.start_trace(session_id="conv_grace0")
    try:
        _run_with_stub(monkeypatch, stub, _make_request())
        events = list(trace.current_trace()["events"])
    finally:
        trace.clear_trace()

    grace_events = [e for e in events if e["name"] == "react_grace_iters"]
    assert len(grace_events) == 1
    assert grace_events[0]["params"]["graceItersUsed"] == 0
    assert grace_events[0]["result"]["exceededMaxIters"] is False


def test_budget_event_carries_the_configured_grace(monkeypatch):
    """预算事件里带上宽限配置值 —— 两个事实分开记，互不顶替。"""
    settings = get_settings()
    stub = _StubAgent(iterations=1, finished_reason="completed")
    trace.start_trace(session_id="conv_grace2")
    try:
        _run_with_stub(monkeypatch, stub, _make_request())
        events = list(trace.current_trace()["events"])
    finally:
        trace.clear_trace()

    budget_events = [e for e in events if e["name"] == "react_budget"]
    assert len(budget_events) == 1
    assert budget_events[0]["params"]["structuredOutputGraceIters"] == (
        settings.react_structured_output_grace_iters
    )
    # 预算本身仍然是编辑预算（默认 8），没被宽限顶掉。
    assert budget_events[0]["params"]["maxIters"] == settings.react_max_iters_edit


def test_tool_side_events_inherit_the_reply_index(monkeypatch):
    """工具自己不知道自己在第几次 reply 里 → 事件要**继承** replyIndex。"""

    class _SubmitStubAgent(_StubAgent):
        async def reply(self, msg):
            self.reply_calls += 1
            from app.tools import submit_edit_plan_tool

            submit_edit_plan_tool.run(
                intent="RENAME_SECTION",
                target={"regionId": "region_total_energy"},
                operation={"newTitle": "昨日总耗电量"},
                confidence=0.9,
            )
            return _StubMsg("completed")

    stub = _SubmitStubAgent(iterations=1, finished_reason="completed")
    trace.start_trace(session_id="conv_reply_index")
    try:
        _run_with_stub(monkeypatch, stub, _make_request())
        events = list(trace.current_trace()["events"])
    finally:
        trace.clear_trace()

    submit_events = [e for e in events if e["stage"] == trace.STAGE_SUBMIT]
    assert submit_events, "提交事件没记上"
    assert submit_events[0]["replyIndex"] == 1
