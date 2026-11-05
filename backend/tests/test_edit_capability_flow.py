"""能力格式编辑的**端到端**链路：候选 → 接受 → 生效 → 撤销语义。

这里不打真实模型：用一只**桩 Agent**走真实的工具链（``submit_edit_plan``
→ 隔离试做 → 候选组装 → 服务层落库 → 接受事务），因此覆盖的是**服务层与
事务**的真实行为，而不是模型的选择。

覆盖：
    - 候选阶段草稿不动，接受后才整批生效（告警列表 → 两张饼图）
    - 接受一次后重复接受被拒（不落两遍）
    - 试做失败不留候选、不留接受菜单，草稿与版本不变
    - 纠正 / 补充让**旧候选失效**，新候选顶上
    - 跨草稿 / 越权接受被服务端拒绝
"""
from __future__ import annotations

import pytest

from app.agents.agentscope_report_agent import AgentScopeReportAgent
from app.schemas.agent import AgentRequest
from app.services.agent_service import agent_service
from app.services.session_service import session_service
from app.tools import registry
from app.tools.context import ToolContext, bind_tool_context, clear_tool_context
from app.tools.schemas import EditPlanPayload
from app.tools.submission import bind_submission, take_submission


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

def _draft():
    from app.schemas.report_definition import ReportDefinitionDraft

    return ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "机房日报", "revision": 3},
            "regions": [
                {
                    "id": "region_report_title",
                    "name": "机房日报",
                    "role": "REPORT_TITLE",
                    "component": {"type": "TEXT", "title": "机房日报"},
                    "templateBinding": {"previewSpec": {"kind": "generic"}},
                },
                {
                    "id": "region_alarm_list",
                    "name": "告警列表",
                    "role": "DETAIL",
                    "component": {
                        "type": "TABLE",
                        "title": "告警列表",
                        "columns": [
                            {"id": "c1", "label": "时间", "fieldRef": "ts", "format": {"kind": "TEXT"}}
                        ],
                    },
                    "data": {
                        "inputs": {"main": {"type": "SIMULATED"}},
                        "pipeline": {"nodes": [], "outputs": {"main": {"nodeRef": "node:n"}}},
                    },
                    "binding": {"outputRef": "output:main"},
                    "templateBinding": {"previewSpec": {"kind": "generic"}},
                },
                {
                    "id": "region_total_energy",
                    "name": "总耗电概览",
                    "role": "SUMMARY",
                    "component": {
                        "type": "METRIC_CARD",
                        "title": "总耗电概览",
                        "metric": {
                            "id": "m1",
                            "label": "总耗电",
                            "fieldRef": "energyKwh",
                            "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
                        },
                    },
                    "data": {
                        "inputs": {"main": {"type": "SIMULATED"}},
                        "pipeline": {"nodes": [], "outputs": {"main": {"nodeRef": "node:n"}}},
                    },
                    "binding": {"outputRef": "output:main"},
                    "templateBinding": {"previewSpec": {"kind": "energy", "unit": "kWh"}},
                },
            ],
        }
    )


def _ops(*, level_kind="PIE", region_kind="PIE"):
    return [
        {
            "op": "ADD_SECTION",
            "target": {},
            "params": {
                "title": "告警等级分布",
                "chartKind": level_kind,
                "dimension": "告警等级",
                "ref": "level_chart",
                "replaceRegionId": "region_alarm_list",
            },
        },
        {
            "op": "ADD_SECTION",
            "target": {},
            "params": {
                "title": "告警区域分布",
                "chartKind": region_kind,
                "dimension": "告警区域",
                "ref": "region_chart",
                "replaceRegionId": "region_alarm_list",
            },
        },
        {"op": "DELETE_SECTION", "target": {"regionId": "region_alarm_list"}, "params": {}},
    ]


class _CapabilityStubAgent:
    """桩 Agent：按能力格式提交一份操作清单。

    走的是**真实**的工具（试做、错误反馈）与**真实**的候选组装，
    只有「模型选了哪份清单」是固定的。
    """

    def __init__(self, operations):
        self.operations = operations
        self.last_tool_result: dict | None = None

    def _tool_context(self, request) -> ToolContext:
        ctx = request.context
        return ToolContext(
            state=request.state,
            subject_ref=ctx.subjectRef,
            subject_id=ctx.subjectId,
            project_id=ctx.projectId,
            selected_region_id=ctx.selectedRegionId,
            base_definition_revision=ctx.baseDefinitionRevision,
            definition=request.snapshots.reportDefinition,
            policy=request.snapshots.reportPolicy,
        )

    def handle(self, request: AgentRequest):
        bind_tool_context(self._tool_context(request))
        bind_submission()
        try:
            result = registry.get_runner("submit_edit_plan")(
                operations=self.operations,
                originalRequest=request.message,
            )
            self.last_tool_result = result
            submission = take_submission()
        finally:
            clear_tool_context()
        if submission is None:
            # 试做失败：**没有**提交 → 走「没有可解析结论」那条路。
            real = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
            return real._unavailable_edit_response(request)
        plan = EditPlanPayload.model_validate(submission["payload"])
        real = AgentScopeReportAgent.__new__(AgentScopeReportAgent)
        return real._assemble_capability_candidate(request, plan)


@pytest.fixture()
def draft():
    return _draft()


@pytest.fixture()
def install_stub(monkeypatch):
    def _install(operations):
        stub = _CapabilityStubAgent(operations)
        monkeypatch.setattr(agent_service, "_agent", stub)
        return stub

    return _install


def _snap(definition):
    return {"reportDefinition": definition.model_dump(mode="json")}


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------

def test_candidate_does_not_touch_draft_then_accept_applies_batch(
    post_chat, draft, install_stub
):
    """候选阶段草稿不动；接受后整批生效：告警列表 → 两张饼图。"""
    install_stub(_ops())
    candidate = post_chat(
        "将告警列表改成告警等级分布和告警区域分布",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(draft),
    ).json()

    pending = candidate["domainObjects"]["pendingEditPlan"]
    assert pending is not None
    assert pending["operations"]
    # 接受菜单在，且确认文字依据**实际差异**。
    assert candidate["questions"][0]["id"] == "pending_edit_plan_confirm"
    assert "告警等级分布" in candidate["reply"]
    assert "告警区域分布" in candidate["reply"]
    # 接受前草稿没动。
    assert candidate["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 3
    assert "region_alarm_list" in {
        region["id"]
        for region in candidate["domainObjects"]["reportDefinitionDraft"]["regions"]
    }

    accepted = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(draft),
    ).json()

    applied = accepted["domainObjects"]["reportDefinitionDraft"]
    ids = {region["id"] for region in applied["regions"]}
    names = {region["name"] for region in applied["regions"]}
    assert "region_alarm_list" not in ids, "原列表要被替换掉"
    assert "告警等级分布" in names
    assert "告警区域分布" in names
    assert "总耗电概览" in names, "其他内容保留"
    charts = [
        region
        for region in applied["regions"]
        if (region.get("component") or {}).get("type") == "CHART"
    ]
    assert len(charts) == 2
    assert all(chart["component"]["chartKind"] == "PIE" for chart in charts)
    assert accepted["domainObjects"]["changeSet"] is not None


def test_accepting_twice_does_not_apply_twice(post_chat, draft, install_stub):
    install_stub(_ops())
    post_chat(
        "将告警列表改成告警等级分布和告警区域分布",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(draft),
    )
    first = post_chat(
        "接受修改", state="USER_EDITING_TEMPLATE", snapshots=_snap(draft)
    ).json()
    assert first["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 4

    second = post_chat(
        "接受修改", state="USER_EDITING_TEMPLATE", snapshots=_snap(draft)
    ).json()
    # 候选已经被消费 → 服务端拒绝，**版本不再往前推**（没有落第二遍）。
    assert second["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 4
    assert "没有重复修改" in second["reply"] or "没有待确认的修改计划" in second["reply"]


def test_trial_failure_leaves_no_candidate_and_no_menu(post_chat, draft, install_stub):
    """试做失败：不产生候选、不出现接受菜单、草稿与版本不变。"""
    stub = install_stub(
        [
            {
                "op": "ADD_SECTION",
                "target": {},
                "params": {"title": "告警等级分布", "dimension": "告警等级"},
            }
        ]
    )
    response = post_chat(
        "将告警列表改成告警等级分布",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(draft),
    ).json()

    # 工具层拿到了**具体错误**（缺 chartKind）。
    assert stub.last_tool_result["accepted"] is False
    assert "chartKind" in stub.last_tool_result["error"]
    # 没有候选、没有接受菜单。
    assert response["domainObjects"]["pendingEditPlan"] is None
    assert response["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 3
    assert session_service.load_pending_plan("conv_test") is None
    # 现场保留着（下一轮还能接着改）。
    item = session_service.load_snapshot("conv_test").get("editItem")
    assert item is not None
    assert item["status"] == "drafting"


def test_followup_updates_candidate_and_invalidates_the_old_one(
    post_chat, draft, install_stub, monkeypatch
):
    """「两张都使用饼图」承接**同一件事**：新候选顶上，旧候选不可再接受。"""
    install_stub(_ops(level_kind="BAR", region_kind="BAR"))
    first = post_chat(
        "将告警列表改成告警等级分布和告警区域分布",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(draft),
    ).json()
    old_plan_id = first["domainObjects"]["pendingEditPlan"]["planId"]
    assert session_service.load_pending_plan("conv_test") is not None

    # 第二轮：同一件事的补充 → 桩 Agent 交一份**新**清单（两张都改成饼图）。
    stub = install_stub(_ops(level_kind="PIE", region_kind="PIE"))
    followup = post_chat(
        "两张都使用饼图",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(draft),
    ).json()

    pending = session_service.load_pending_plan("conv_test")
    assert pending is not None
    assert pending.planId != old_plan_id, "纠正后旧候选必须失效"
    assert followup["domainObjects"]["pendingEditPlan"]["planId"] != old_plan_id
    # 草稿仍然没动（还没接受）。
    assert followup["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 3


def test_cross_session_candidate_cannot_be_accepted(client, draft, install_stub):
    """别的会话的候选不能在本会话接受。"""
    install_stub(_ops())
    client.post(
        "/api/agent/chat",
        json={
            "agentProtocolVersion": "1.0",
            "conversationId": "conv_test",
            "state": "USER_EDITING_TEMPLATE",
            "message": "将告警列表改成告警等级分布和告警区域分布",
            "context": {
                "projectId": "project_001",
                "subjectRef": "machine_room",
                "subjectId": "room_a",
            },
            "snapshots": _snap(draft),
        },
    )
    pending = session_service.load_pending_plan("conv_test")
    assert pending is not None

    # 把这份计划挂到**另一个会话**上，再在那个会话里接受。
    session_service.save_pending_plan("conv_other", pending)
    accepted = client.post(
        "/api/agent/chat",
        json={
            "agentProtocolVersion": "1.0",
            "conversationId": "conv_other",
            "state": "USER_EDITING_TEMPLATE",
            "message": "接受修改",
            "context": {
                "projectId": "project_001",
                "subjectRef": "machine_room",
                "subjectId": "room_a",
            },
            "snapshots": _snap(draft),
        },
    ).json()
    # 那个会话**没有自己的草稿** → 服务端拒绝，版本不动。
    assert accepted["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 3
    # 原来的会话那份候选还在（没有被这次越权接受消费掉）。
    assert session_service.load_pending_plan("conv_test") is not None
