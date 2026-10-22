"""端到端测试：POST /api/agent/chat 覆盖 00_统筹.md 第 6 章 6 个验收样例。

这些测试保护「接口契约」：返回的 state / domainObjects / blocks / uiActions
结构必须稳定，前端和 Java 后端依赖这些字段。
"""
from __future__ import annotations

import json

from app.agents.agentscope_report_agent import AgentScopeReportAgent
from app.agents.fake_report_agent import FakeReportAgent
from app.services.agent_service import agent_service
from app.services.context_engine import (
    ConciergeAction, ConciergeDecision, RouteDecision, RoutingAction, UserIntent,
    context_engine,
)
from app.services.report_draft_service import build_definition_draft


def test_bare_continue_does_not_start_a_report(client, base_context, monkeypatch):
    def unexpected_agent(request):
        raise AssertionError("无待恢复任务时不应进入业务 Agent")

    monkeypatch.setattr(agent_service._agent, "handle", unexpected_agent)
    response = client.post("/api/agent/chat", json={
        "conversationId": "conv_resume_missing",
        "state": "ANALYZING",
        "message": "继续工作",
        "context": base_context,
    }).json()
    assert response["state"] == "ANALYZING"
    assert response["uiActions"] == ["resume_unavailable"]
    assert response["domainObjects"]["reportDefinitionDraft"] is None


def test_resume_replays_original_message_not_continue(client, base_context):
    response = client.post("/api/agent/chat", json={
        "conversationId": "conv_resumed",
        "resumeOfConversationId": "conv_cancelled",
        "state": "ANALYZING",
        "message": "我想吃龙虾",
        "context": base_context,
    }).json()
    assert response["state"] != "WAITING_USER_CONFIRM"
    assert response["domainObjects"]["reportDefinitionDraft"] is None


def test_template_preview_zero_info_direct_draft(client, base_context):
    body = {"conversationId": "conv_template_zero", "context": base_context}
    start = client.post("/api/agent/chat", json={
        **body, "state": "ANALYZING", "message": "给我一个模版看看",
    }).json()
    assert start["state"] == "NEEDS_CLARIFICATION"
    question = start["questions"][0]
    assert question["id"] == "template_preview_mode"
    assert question["selectionOnly"] is True
    assert question["allowRestart"] is True
    assert {option["value"] for option in question["options"]} == {"补齐信息", "直接生成模版"}

    for message, state in (("随便生成", "NEEDS_CLARIFICATION"), ("__advance__", "ANALYZING")):
        rejected = client.post("/api/agent/chat", json={
            **body, "state": state, "message": message,
        }).json()
        assert rejected["uiActions"] == ["selection_required"]
        assert rejected["questions"][0]["id"] == question["id"]

    generated = client.post("/api/agent/chat", json={
        **body, "state": "NEEDS_CLARIFICATION", "message": "直接生成模版",
    }).json()
    assert generated["state"] == "WAITING_USER_CONFIRM"
    assert "Mock" in generated["reply"]
    assert "SAVE_DEFINITION_DRAFT" in {tool["type"] for tool in generated["toolRequests"]}
    draft = generated["domainObjects"]["reportDefinitionDraft"]
    assert draft["defaults"]["scope"]["selectors"]["machineRoomId"] == ["room_a"]
    assert {item["source"] for item in draft["metadata"]["generationInputs"].values()} == {"系统暂选"}
    assert generated["domainObjects"]["reportPolicyDraft"] is not None


def test_generated_draft_requires_confirmation_dropdown(client, base_context):
    body = {"conversationId": "conv_confirm_dropdown", "context": base_context}
    client.post("/api/agent/chat", json={
        **body, "state": "ANALYZING", "message": "给我一个模版看看",
    })
    generated = client.post("/api/agent/chat", json={
        **body, "state": "NEEDS_CLARIFICATION", "message": "直接生成模版",
    }).json()
    question = generated["questions"][0]
    assert question["id"] == "template_edit_confirm"
    assert question["selectionOnly"] is True
    assert {option["value"] for option in question["options"]} == {"同意", "修改", "重新开始"}
    snapshots = {
        "reportDefinition": generated["domainObjects"]["reportDefinitionDraft"],
        "reportPolicy": generated["domainObjects"]["reportPolicyDraft"],
    }
    for message in ("把趋势图改成最近 7 天", "__advance__"):
        rejected = client.post("/api/agent/chat", json={
            **body, "state": "WAITING_USER_CONFIRM",
            "message": message, "snapshots": snapshots,
        }).json()
        assert rejected["state"] == "WAITING_USER_CONFIRM"
        assert rejected["uiActions"] == ["selection_required"]
        assert rejected["questions"][0]["id"] == question["id"]

    editing = client.post("/api/agent/chat", json={
        **body, "state": "WAITING_USER_CONFIRM",
        "message": "修改", "snapshots": snapshots,
    }).json()
    assert editing["state"] == "USER_EDITING_TEMPLATE"
    assert editing["questions"][0]["selectionOnly"] is False
    edited = client.post("/api/agent/chat", json={
        **body, "state": "USER_EDITING_TEMPLATE",
        "message": "把趋势图改成最近 7 天", "snapshots": snapshots,
    }).json()
    assert edited["uiActions"] != ["selection_required"]


def test_agentscope_generation_uses_locked_confirmation_contract(
    client, base_context, monkeypatch
):
    """真实 AgentScope 组装响应，但用确定性计划代替外部模型调用。"""
    agent = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
    agent._model = object()
    agent._fallback = FakeReportAgent()

    async def fake_plan(request):
        return {
            "mode": "GENERATE",
            "reportName": "A 机房能耗日报",
            "roomId": "room_a",
            "includeTrend": True,
            "reply": "已生成草稿，请确认。",
        }

    async def continue_current(request, emit=None):
        return RouteDecision(
            userIntent=UserIntent.NEW_TASK,
            routingAction=RoutingAction.CONTINUE_CURRENT,
        )

    monkeypatch.setattr(agent, "_run_agent", fake_plan)
    monkeypatch.setattr(agent_service, "_agent", agent)
    monkeypatch.setattr(context_engine, "route", continue_current)

    for choice, expected_state in (
        ("同意", "SAVE_DEFINITION_DRAFT"),
        ("修改", "USER_EDITING_TEMPLATE"),
        ("重新开始", "ANALYZING"),
    ):
        body = {
            "conversationId": f"conv_agentscope_confirm_{choice}",
            "context": base_context,
        }
        generated = client.post("/api/agent/chat", json={
            **body, "state": "ANALYZING", "message": "生成昨日能耗日报",
        }).json()
        assert generated["state"] == "WAITING_USER_CONFIRM"
        assert generated["reply"] == "已生成草稿，请确认。"
        assert len(generated["questions"]) == 1
        question = generated["questions"][0]
        assert question["id"] == "template_edit_confirm"
        assert question["selectionOnly"] is True
        assert {option["value"] for option in question["options"]} == {
            "同意", "修改", "重新开始",
        }
        snapshots = {
            "reportDefinition": generated["domainObjects"]["reportDefinitionDraft"],
            "reportPolicy": generated["domainObjects"]["reportPolicyDraft"],
        }
        rejected = client.post("/api/agent/chat", json={
            **body, "state": "WAITING_USER_CONFIRM",
            "message": "直接修改标题", "snapshots": snapshots,
        }).json()
        assert rejected["uiActions"] == ["selection_required"]
        assert rejected["questions"][0]["id"] == "template_edit_confirm"

        selected = client.post("/api/agent/chat", json={
            **body, "state": "WAITING_USER_CONFIRM",
            "message": choice, "snapshots": snapshots,
        }).json()
        assert selected["state"] == expected_state


def test_confirm_dropdown_save_and_restart(client, base_context):
    for choice, expected_state in (("同意", "SAVE_DEFINITION_DRAFT"), ("重新开始", "ANALYZING")):
        body = {"conversationId": f"conv_confirm_{choice}", "context": base_context}
        generated = client.post("/api/agent/chat", json={
            **body, "state": "ANALYZING",
            "message": "给我 A 机房昨日能耗模版",
        }).json()
        assert generated["state"] == "WAITING_USER_CONFIRM"
        assert generated["questions"][0]["selectionOnly"] is True
        chosen = client.post("/api/agent/chat", json={
            **body, "state": "WAITING_USER_CONFIRM", "message": choice,
            "snapshots": {
                "reportDefinition": generated["domainObjects"]["reportDefinitionDraft"],
                "reportPolicy": generated["domainObjects"]["reportPolicyDraft"],
            },
        }).json()
        assert chosen["state"] == expected_state


def test_ready_requires_dropdown_before_editing(client, base_context):
    body = {"conversationId": "conv_ready_dropdown", "context": base_context}
    ready = client.post("/api/agent/chat", json={
        **body, "state": "RENDERING", "message": "__advance__",
    }).json()
    assert ready["state"] == "READY"
    question = ready["questions"][0]
    assert question["id"] == "ready_action"
    assert question["selectionOnly"] is True
    assert {option["value"] for option in question["options"]} == {
        "继续修改", "发布", "导出 PDF",
    }

    for state, message in (("READY", "帮我改一下"), ("ANALYZING", "生成新报表"),
                           ("READY", "__advance__")):
        rejected = client.post("/api/agent/chat", json={
            **body, "state": state, "message": message,
        }).json()
        assert rejected["state"] == "READY"
        assert rejected["uiActions"] == ["selection_required"]
        assert rejected["questions"][0]["id"] == "ready_action"

    for action in ("发布", "导出 PDF"):
        unavailable = client.post("/api/agent/chat", json={
            **body, "state": "READY", "message": action,
        }).json()
        assert unavailable["state"] == "READY"
        assert "Mock" in unavailable["reply"]
        assert unavailable["questions"][0]["selectionOnly"] is True
        rejected = client.post("/api/agent/chat", json={
            **body, "state": "READY", "message": "直接改标题",
        }).json()
        assert rejected["uiActions"] == ["selection_required"]

    missing_draft = client.post("/api/agent/chat", json={
        **body, "state": "READY", "message": "继续修改",
    }).json()
    assert missing_draft["state"] == "READY"
    assert missing_draft["questions"][0]["selectionOnly"] is True

    definition = build_definition_draft("A 机房能耗日报", "room_a")
    editing = client.post("/api/agent/chat", json={
        **body, "state": "READY", "message": "继续修改",
        "snapshots": {"reportDefinition": definition.model_dump(mode="json")},
    }).json()
    assert editing["state"] == "USER_EDITING_TEMPLATE"
    assert editing["domainObjects"]["reportDefinitionDraft"] is not None
    assert editing["questions"][0]["selectionOnly"] is False
    changed = client.post("/api/agent/chat", json={
        **body, "state": "USER_EDITING_TEMPLATE", "message": "报表名称改成新日报",
        "snapshots": {"reportDefinition": definition.model_dump(mode="json")},
    }).json()
    # 先给候选计划，草稿此刻不动。
    assert changed["uiActions"] == ["pending_edit_plan"]
    assert changed["domainObjects"]["pendingEditPlan"] is not None
    assert changed["domainObjects"]["reportDefinitionDraft"]["metadata"]["name"] == "A 机房能耗日报"

    accepted = client.post("/api/agent/chat", json={
        **body, "state": "USER_EDITING_TEMPLATE", "message": "接受修改",
        "snapshots": {"reportDefinition": definition.model_dump(mode="json")},
    }).json()
    assert accepted["domainObjects"]["reportDefinitionDraft"]["metadata"]["name"] == "新日报"


def test_ready_without_pending_choice_restores_dropdown(client, base_context):
    response = client.post("/api/agent/chat", json={
        "conversationId": "conv_ready_restored",
        "context": base_context,
        "state": "READY",
        "message": "生成另一个报表",
    }).json()
    assert response["state"] == "READY"
    assert response["questions"][0]["id"] == "ready_action"
    assert response["questions"][0]["selectionOnly"] is True


def test_template_preview_partial_info_dropdown_completion(client, base_context):
    body = {"conversationId": "conv_template_partial", "context": base_context}
    start = client.post("/api/agent/chat", json={
        **body, "state": "ANALYZING", "message": "给我 A 机房的模版看看",
    }).json()
    assert start["questions"][0]["id"] == "template_preview_mode"
    time = client.post("/api/agent/chat", json={
        **body, "state": "NEEDS_CLARIFICATION", "message": "补齐信息",
    }).json()
    assert time["questions"][0]["id"] == "template_preview_time"
    assert time["questions"][0]["selectionOnly"] is True
    assert [option["value"] for option in time["questions"][0]["options"]] == ["昨日"]

    rejected = client.post("/api/agent/chat", json={
        **body, "state": "NEEDS_CLARIFICATION", "message": "今天",
    }).json()
    assert rejected["uiActions"] == ["selection_required"]
    assert rejected["questions"][0]["id"] == "template_preview_time"
    metric = client.post("/api/agent/chat", json={
        **body, "state": "NEEDS_CLARIFICATION", "message": "昨日",
    }).json()
    assert metric["questions"][0]["id"] == "template_preview_metric"
    assert metric["questions"][0]["selectionOnly"] is True
    generated = client.post("/api/agent/chat", json={
        **body, "state": "NEEDS_CLARIFICATION",
        "message": metric["questions"][0]["options"][0]["value"],
    }).json()
    assert generated["state"] == "WAITING_USER_CONFIRM"
    sources = generated["domainObjects"]["reportDefinitionDraft"]["metadata"]["generationInputs"]
    assert {field: item["source"] for field, item in sources.items()} == {
        "room": "用户指定", "time": "用户指定", "metric": "用户指定",
    }


def test_template_preview_zero_info_completion_starts_with_room(client, base_context):
    body = {"conversationId": "conv_template_room", "context": base_context}
    client.post("/api/agent/chat", json={
        **body, "state": "ANALYZING", "message": "给我一个模版",
    })
    room = client.post("/api/agent/chat", json={
        **body, "state": "NEEDS_CLARIFICATION", "message": "补齐信息",
    }).json()
    assert room["questions"][0]["id"] == "template_preview_room"
    assert room["questions"][0]["selectionOnly"] is True
    assert [option["value"] for option in room["questions"][0]["options"]] == ["A 机房"]
    time = client.post("/api/agent/chat", json={
        **body, "state": "NEEDS_CLARIFICATION", "message": "A 机房",
    }).json()
    assert time["questions"][0]["id"] == "template_preview_time"


def test_template_preview_restart_clears_pending_choice(client, base_context):
    body = {"conversationId": "conv_template_restart", "context": base_context}
    client.post("/api/agent/chat", json={
        **body, "state": "ANALYZING", "message": "给我一个模版",
    })
    restarted = client.post("/api/agent/chat", json={
        **body, "state": "NEEDS_CLARIFICATION", "message": "重新开始",
    }).json()
    assert restarted["state"] == "ANALYZING"
    assert not restarted["questions"]
    next_request = client.post("/api/agent/chat", json={
        **body, "state": "ANALYZING", "message": "给我 A 机房昨日能耗模版",
    }).json()
    assert next_request["state"] == "WAITING_USER_CONFIRM"
    assert {item["source"] for item in
            next_request["domainObjects"]["reportDefinitionDraft"]["metadata"]["generationInputs"].values()} == {"用户指定"}


def test_template_preview_does_not_substitute_unsupported_scope(client, base_context):
    for index, message in enumerate((
        "给我 B 机房的模版", "给我机房 C 的模版", "给我今天的能耗模版",
        "给我明天的模版", "给我 PUE 模版", "给我所有项目的模版",
    )):
        result = client.post("/api/agent/chat", json={
            "conversationId": f"conv_template_unsupported_{index}",
            "context": base_context, "state": "ANALYZING", "message": message,
        }).json()
        assert result["state"] == "BLOCKED_OR_UNSUPPORTED"
        assert result["domainObjects"]["reportDefinitionDraft"] is None
        assert result["blocks"]

    unauthorized = client.post("/api/agent/chat", json={
        "conversationId": "conv_template_unauthorized", "state": "ANALYZING",
        "message": "给我一个模版",
        "context": {**base_context, "projectId": "project_002"},
    }).json()
    assert unauthorized["state"] == "BLOCKED_OR_UNSUPPORTED"
    assert unauthorized["domainObjects"]["reportDefinitionDraft"] is None


def test_stream_debug_exposes_route_and_agent_parameters(client, base_context, monkeypatch):
    """流式调试展示路由输入/输出和业务结果，但不泄漏模型凭据。"""
    raw = json.dumps({
        "userIntent": "NEW_TASK",
        "routingAction": "CONTINUE_CURRENT",
        "confidence": 0.92,
        "isReadyForNextStep": True,
        "reason": "信息足够",
    })

    async def fake_concierge(request, emit=None):
        assert request.state.value == "ANALYZING"
        assert emit is not None
        return ConciergeDecision(action=ConciergeAction.DEEP_ROUTE, userIntent=UserIntent.NEW_TASK)

    async def fake_stream(*, emit, chunk_stage, **kwargs):
        assert kwargs["response_format"] == {"type": "json_object"}
        await emit("model_chunk", {"stage": chunk_stage, "delta": raw})
        return raw

    monkeypatch.setattr(context_engine, "_client", object())
    monkeypatch.setattr(context_engine, "_run_concierge", fake_concierge)
    monkeypatch.setattr(context_engine, "_chat_json_stream", fake_stream)
    result = client.post("/api/agent/chat/stream", json={
        "conversationId": "conv_debug",
        "state": "ANALYZING",
        "message": "生成 A 机房昨日能耗日报",
        "context": base_context,
    })
    assert result.status_code == 200
    events = []
    for block in result.text.split("\n\n"):
        if block.startswith("event: "):
            event, data = block.split("\n", 1)
            events.append((event.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    stages = {data["stage"]: data for event, data in events if event == "stage" and "stage" in data}

    start = stages["deep_route_start"]["debugParams"]
    assert start["路由输入"]["state"] == "ANALYZING"
    assert start["模型调用"]["responseFormat"] == "json_object"
    assert "api_key" not in json.dumps(start).lower()
    assert any(
        event == "model_chunk" and data["stage"] == "deep_route_chunk"
        for event, data in events
    )
    output = stages["deep_route_done"]["debugParams"]
    assert output["模型输出"]["rawOutput"] == raw
    assert output["解析结果"]["confidence"] == 0.92
    assert stages["deep_route_done"]["stepElapsedMs"] >= 0
    applied = stages["apply_context_route"]["debugParams"]
    assert applied["路由判定"]["action"] == "CONTINUE_CURRENT"
    assert applied["执行结果"]["outcome"] == "CONTINUE_TO_BUSINESS_AGENT"
    agent_start = stages["business_agent_start"]["debugParams"]
    assert agent_start["输入上下文"]["subjectId"] == "room_a"
    agent_done = stages["business_agent_done"]["debugParams"]
    assert agent_done["响应"]["responseState"] == "WAITING_USER_CONFIRM"
    assert "region_hourly_trend" in agent_done["草稿与变更"]["regionIds"]
    assert agent_done["交互"]["toolRequests"][0]["type"] == "SAVE_DEFINITION_DRAFT"
    assert stages["business_agent_done"]["stepElapsedMs"] >= 0


def test_readonly_visualization_query_does_not_lock_input_but_edit_plan_does(client, base_context):
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    body = {
        "conversationId": "conv_required_dropdown",
        "state": "USER_EDITING_TEMPLATE",
        "context": {**base_context, "selectedRegionId": "region_device_ranking"},
        "snapshots": {"reportDefinition": definition.model_dump(mode="json")},
    }
    result = client.post("/api/agent/chat", json={
        **body, "message": "设备耗电排名可以怎么展示？"
    }).json()
    assert result["questions"] == []
    assert "柱状图" in result["reply"] and "表格" in result["reply"]
    assert result["domainObjects"]["pendingEditPlan"] is None
    assert result["domainObjects"]["changeSet"] is None

    chosen = client.post("/api/agent/chat", json={
        **body, "message": "把设备耗电排名改成柱状图"
    }).json()
    assert chosen["state"] == "USER_EDITING_TEMPLATE"
    assert chosen["uiActions"] != ["selection_required"]
    # 选了展示形式后先给候选计划，等用户点「接受修改」才改草稿。
    assert chosen["uiActions"] == ["pending_edit_plan"]
    assert chosen["domainObjects"]["pendingEditPlan"] is not None
    assert chosen["domainObjects"]["changeSet"] is None
    assert chosen["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 0

    rejected = client.post("/api/agent/chat", json={
        **body, "message": "重新开始"
    }).json()
    assert rejected["uiActions"] == ["selection_required"]
    assert rejected["questions"][0]["selectionOnly"] is True




def test_sample_1_generate_full_draft(post_chat):
    r = post_chat(
        "生成 A 机房昨日能耗日报，包含总耗电、设备排名和趋势分析"
    )
    assert r.status_code == 200
    data = r.json()

    assert data["state"] == "WAITING_USER_CONFIRM"
    assert data["designSessionId"]
    assert data["error"] is None

    # 策略草稿
    policy = data["domainObjects"]["reportPolicyDraft"]
    assert policy is not None
    assert policy["subjectRef"] == "machine_room"
    assert policy["executionMode"] == "MANUAL_ONLY"
    assert "TIME_WINDOW" in policy["allowedOverrides"]

    # 定义草稿 6 个区域
    definition = data["domainObjects"]["reportDefinitionDraft"]
    assert definition is not None
    region_ids = [r["id"] for r in definition["regions"]]
    assert region_ids == [
        "region_report_title",
        "region_total_energy",
        "region_device_ranking",
        "region_hourly_trend",
        "region_data_quality",
        "region_analysis",
    ]
    assert definition["schemaVersion"] == "1.0"
    assert definition["metadata"]["status"] == "DRAFT"
    assert definition["defaults"]["scope"]["selectors"]["machineRoomId"] == [
        "room_a"
    ]

    # 指标引用必须真实（不允许 AI 编造）
    metric_refs = {m["ref"] for m in definition["dependencies"]["metricRefs"]}
    assert "metric.machine_room.total_energy" in metric_refs
    assert "metric.machine_room.device_energy" in metric_refs

    # 必须给出保存草稿的 toolRequest
    tool_types = {t["type"] for t in data["toolRequests"]}
    assert "SAVE_DEFINITION_DRAFT" in tool_types

    # 前端 UI 动作
    assert set(data["uiActions"]) >= {
        "preview_framework",
        "confirm_framework",
        "request_modification",
    }


# ---------- 样例 2：信息不足 ----------

def test_sample_2_needs_clarification(post_chat):
    r = post_chat("帮我看异常设备")
    assert r.status_code == 200
    data = r.json()

    assert data["state"] == "NEEDS_CLARIFICATION"
    # 不能同时给出草稿
    assert data["domainObjects"]["reportDefinitionDraft"] is None
    assert data["domainObjects"]["reportPolicyDraft"] is None

    question_ids = {q["id"] for q in data["questions"]}
    assert {"q_anomaly_type", "q_scope", "q_time_window"} <= question_ids

    # 异常类型是单选且选项非空
    anomaly_q = next(q for q in data["questions"] if q["id"] == "q_anomaly_type")
    assert anomaly_q["expects"] == "SINGLE_CHOICE"
    assert len(anomaly_q["options"]) >= 2


# ---------- 样例 3：权限越界 ----------

def test_sample_3_permission_denied(post_chat):
    r = post_chat("帮我看所有项目的能耗排名")
    assert r.status_code == 200
    data = r.json()

    assert data["state"] == "BLOCKED_OR_UNSUPPORTED"
    assert len(data["blocks"]) == 1
    block = data["blocks"][0]
    assert block["code"] == "PERMISSION_DENIED"
    assert len(block["alternatives"]) >= 1
    # 不能顺手把草稿给出去
    assert data["domainObjects"]["reportDefinitionDraft"] is None


# ---------- 样例 4：能力不支持 ----------

def test_sample_4_capability_not_supported(post_chat):
    r = post_chat("帮我看 A 机房分钟级历史能耗")
    assert r.status_code == 200
    data = r.json()

    assert data["state"] == "BLOCKED_OR_UNSUPPORTED"
    block = data["blocks"][0]
    assert block["code"] == "CAPABILITY_NOT_SUPPORTED"
    assert "小时" in " ".join(block["alternatives"]) or "日" in " ".join(
        block["alternatives"]
    )
    # 应该调用过目录查询工具
    tool_types = {t["type"] for t in data["toolRequests"]}
    assert "QUERY_CATALOG" in tool_types


# ---------- 样例 5：修改趋势图窗口 ----------

def test_sample_5_change_trend_window(post_chat):
    r = post_chat(
        "把趋势图改成最近 7 天",
        state="WAITING_USER_CONFIRM",
        designSessionId="session_001",
        reportDefinitionId="def_001",
        baseDefinitionRevision=3,
        selectedRegionId="region_hourly_trend",
    )
    assert r.status_code == 200
    data = r.json()

    assert data["state"] == "DEFINITION_DRAFT_READY"
    change_set = data["domainObjects"]["changeSet"]
    assert change_set is not None
    assert change_set["baseDefinitionRevision"] == 3

    changed_paths = [entry["path"] for entry in change_set["changed"]]
    assert (
        "/regions/region_hourly_trend/context/timeWindow" in changed_paths
    ), f"实际路径: {changed_paths}"

    # 必须声明「总耗电卡片未受影响」
    unchanged_ids = {u["regionId"] for u in change_set["unchanged"]}
    assert "region_total_energy" in unchanged_ids

    # 新窗口必须是日历窗口、offset=-7、count=7
    new_window = next(
        entry["after"]
        for entry in change_set["changed"]
        if entry["path"].endswith("/context/timeWindow")
    )
    assert new_window["mode"] == "REPLACE"
    assert new_window["value"]["unit"] == "DAY"
    assert new_window["value"]["offset"] == -7
    assert new_window["value"]["count"] == 7

    # ChangeSet 阶段不应覆盖整份定义草稿
    assert data["domainObjects"]["reportDefinitionDraft"] is None


# ---------- 样例 6：删除趋势图 ----------

def test_sample_6_remove_trend_region(post_chat):
    r = post_chat(
        "不要趋势图，改成只要总耗电和设备排名",
        state="WAITING_USER_CONFIRM",
        designSessionId="session_001",
        reportDefinitionId="def_001",
        baseDefinitionRevision=5,
    )
    assert r.status_code == 200
    data = r.json()

    assert data["state"] == "DEFINITION_DRAFT_READY"
    change_set = data["domainObjects"]["changeSet"]
    assert change_set["baseDefinitionRevision"] == 5

    # 唯一变更是整个趋势图区域被设为 null（删除）
    assert len(change_set["changed"]) == 1
    entry = change_set["changed"][0]
    assert entry["regionId"] == "region_hourly_trend"
    assert entry["path"] == "/regions/region_hourly_trend"
    assert entry["after"] is None

    # 另外两个核心区域必须明确保留
    unchanged_ids = {u["regionId"] for u in change_set["unchanged"]}
    assert {"region_total_energy", "region_device_ranking"} <= unchanged_ids


# ---------- 协议版本错误 ----------

def test_protocol_version_mismatch(client, base_context):
    body = {
        "agentProtocolVersion": "9.9",
        "conversationId": "conv_bad",
        "state": "ANALYZING",
        "message": "生成 A 机房昨日能耗日报",
        "context": base_context,
    }
    r = client.post("/api/agent/chat", json=body)
    data = r.json()
    assert data["error"] is not None
    assert data["error"]["code"] == "INVALID_REQUEST"


# ---------- health ----------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
