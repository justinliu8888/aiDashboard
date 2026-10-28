"""已记录进度事实模块的专项验收测试（工单 #15）。

只测**外部行为**：模块从服务端会话记录读到什么，就回报什么。
不测私有函数，不复述实现。

红线对应的验收点：
    - 据实回报已记录阶段，不把已记录状态包装成实时状态
    - 草稿 / 待确认修改计划 / 已保存定义 / 预览分别处理
    - 确实没有、读不到、没权限，三种情况说不同的话
    - 查询本身不改变任何业务对象
"""
from __future__ import annotations

import re

import pytest

from app.schemas.agent import AgentRequest, AgentRequestContext, AgentState
from app.schemas.report_definition import ReportDefinitionDraft
from app.services import recorded_progress_service
from app.services.definition_save_service import definition_save_service
from app.services.session_context import bind_session_key, clear_session_key
from app.services.session_service import session_service
from app.tools import get_recorded_progress_tool
from app.tools.base import LOOKUP_STATES, ToolCategory
from app.tools.context import ToolContext, bind_tool_context, clear_tool_context


# ---------- 造事实的辅助 ----------


def _seed_session(
    *,
    session_id: str = "sess_progress",
    state: str = "ANALYZING",
    definition: dict | None = None,
    last_report_run: dict | None = None,
    pending_edit_plan: dict | None = None,
    pending_choice: dict | None = None,
    report_definition_id: str | None = None,
    preview_run_id: str | None = None,
    workspace_id: str | None = None,
    snapshot_extra: dict | None = None,
) -> str:
    """按服务端会话记录的样子铺一条会话。

    走真实仓储，不 mock：本模块的事实来源就是这张表。
    """
    snapshot: dict = {}
    if definition is not None:
        snapshot["definition"] = definition
    if last_report_run is not None:
        snapshot["lastReportRun"] = last_report_run
    if snapshot_extra:
        snapshot.update(snapshot_extra)

    session_service.repository.upsert_session(
        session_id=session_id,
        state=state,
        title="测试会话",
        workspaceId=workspace_id,
        reportDefinitionId=report_definition_id,
        previewRunId=preview_run_id,
        snapshot=snapshot or None,
    )
    if pending_edit_plan is not None:
        session_service.repository.save_pending_edit_plan(session_id, pending_edit_plan)
    if pending_choice is not None:
        session_service.repository.set_pending_choice(session_id, pending_choice)
    return session_id


def _draft(
    *,
    name: str = "机房能耗日报",
    revision: int = 3,
    region_count: int = 2,
) -> dict:
    return {
        "metadata": {"name": name, "revision": revision},
        "regions": [{"id": f"r{i}", "name": f"区块{i}"} for i in range(region_count)],
    }


# ---------- 切片 1：会话根本不存在 ----------


def test_session_missing_is_reported_as_unverifiable_not_as_empty():
    """会话查不到时要说「无法确认」，不能说「没有草稿」。"""
    result = recorded_progress_service.describe_recorded_progress("sess_does_not_exist")

    assert result["availability"] == "UNAVAILABLE"
    assert result["stage"] == "UNKNOWN"
    assert result["reason"]
    # 不可得 ≠ 确实没有：不能出现任何「没有草稿」的事实断言
    assert result["draft"]["exists"] is False


# ---------- 切片 2：会话存在但确实没有报表对象 ----------


def test_empty_session_reports_no_object_in_current_conversation_only():
    """当前对话确实没有报表对象时据实说明，且只限当前对话。"""
    _seed_session(session_id="sess_empty", state="ANALYZING")

    result = recorded_progress_service.describe_recorded_progress("sess_empty")

    assert result["availability"] == "NO_OBJECT"
    assert result["stage"] == "NO_OBJECT"
    assert result["draft"]["exists"] is False
    assert result["savedDefinition"]["exists"] is False
    assert result["preview"]["exists"] is False
    # 不能声称检索过历史
    assert result["scope"] == "CURRENT_CONVERSATION"
    assert result["nextStep"]


# ---------- 切片 3：已记录阶段随服务端状态变化 ----------


def test_draft_waiting_confirmation_reports_draft_pending_stage():
    """草稿已生成、等人确认：阶段、名称与版本都按服务端记录回报。"""
    _seed_session(
        session_id="sess_pending",
        state="WAITING_USER_CONFIRM",
        definition=_draft(name="机房能耗日报", revision=3, region_count=2),
    )

    result = recorded_progress_service.describe_recorded_progress("sess_pending")

    assert result["availability"] == "SUCCESS"
    assert result["stage"] == "DRAFT_PENDING"
    assert result["recordedState"] == "WAITING_USER_CONFIRM"
    assert result["draft"]["exists"] is True
    assert result["draft"]["name"] == "机房能耗日报"
    assert result["draft"]["revision"] == 3
    assert result["draft"]["regionCount"] == 2
    # 草稿还没确认，不能说成已经出数
    assert result["savedDefinition"]["exists"] is False
    assert result["preview"]["exists"] is False
    assert result["nextStep"]


@pytest.mark.parametrize(
    ("recorded_state", "expected_stage"),
    [
        ("ANALYZING", "REQUIREMENT_GATHERING"),
        ("NEEDS_CLARIFICATION", "REQUIREMENT_GATHERING"),
        ("DEFINITION_DRAFT_READY", "DRAFT_PENDING"),
        ("USER_EDITING_TEMPLATE", "DRAFT_EDITING"),
        ("SAVE_DEFINITION_DRAFT", "SAVED"),
        ("VALIDATING", "VALIDATING"),
        ("VALIDATION_FAILED", "VALIDATION_FAILED"),
        ("PREVIEWING", "PREVIEWING"),
        ("PREVIEW_READY", "PREVIEW_READY"),
        ("RENDERING", "PREVIEW_READY"),
        ("READY", "COMPLETED"),
        ("BLOCKED_OR_UNSUPPORTED", "BLOCKED"),
        ("INTERRUPT_CONFIRMING", "INTERRUPT_CONFIRMING"),
    ],
)
def test_every_recorded_state_maps_to_a_known_stage(recorded_state, expected_stage):
    """服务端记录里的每个业务状态都有对应的已记录阶段。"""
    session_id = f"sess_state_{recorded_state.lower()}"
    _seed_session(
        session_id=session_id,
        state=recorded_state,
        definition=_draft(),
    )

    result = recorded_progress_service.describe_recorded_progress(session_id)

    assert result["stage"] == expected_stage
    assert result["recordedState"] == recorded_state
    assert result["nextStep"]


# ---------- 切片 4：待确认修改计划是独立状态，不是草稿 ----------


def test_pending_edit_plan_is_its_own_stage_and_not_applied_to_draft():
    """有待确认的修改计划时，阶段是待确认；计划本身不等于草稿已改。"""
    _seed_session(
        session_id="sess_plan",
        state="USER_EDITING_TEMPLATE",
        definition=_draft(revision=3),
        pending_edit_plan={
            "planId": "plan_abc",
            "draftRevision": 3,
            "targetRegionId": "r0",
            "operations": [{"op": "delete_region"}],
            "summary": "删除设备耗电排名卡片",
        },
    )

    result = recorded_progress_service.describe_recorded_progress("sess_plan")

    assert result["stage"] == "PENDING_CONFIRM"
    assert result["pendingEditPlan"]["exists"] is True
    assert result["pendingEditPlan"]["planId"] == "plan_abc"
    assert result["pendingEditPlan"]["summary"] == "删除设备耗电排名卡片"
    # 计划还没被接受：草稿版本不能变，也不能说成已改
    assert result["draft"]["revision"] == 3
    assert result["draft"]["regionCount"] == 2


# ---------- 切片 5：草稿 / 已保存定义 / 预览分别处理 ----------


def test_completed_run_separates_draft_saved_definition_and_preview():
    """预览跑完后的会话：草稿、已保存定义、预览各说各的，不混为一谈。"""
    definition_save_service.save(
        definitionId="def_completed",
        policyDraft={},
        definitionDraft=_draft(name="机房能耗日报", revision=1),
    )
    _seed_session(
        session_id="sess_done",
        state="READY",
        definition=_draft(name="机房能耗日报", revision=1),
        last_report_run={
            "reportRunId": "run_completed",
            "status": "SUCCEEDED",
            "logicalTime": "2026-10-27T00:00:00+08:00",
            "windowLabel": "昨日",
            "resultRef": "region:r0/output:main",
        },
        report_definition_id="def_completed",
        preview_run_id="run_completed",
        snapshot_extra={"previewPayload": {"regions": []}},
    )

    result = recorded_progress_service.describe_recorded_progress("sess_done")

    assert result["stage"] == "COMPLETED"
    assert result["draft"]["exists"] is True
    assert result["draft"]["revision"] == 1
    assert result["savedDefinition"]["exists"] is True
    assert result["savedDefinition"]["definitionId"] == "def_completed"
    assert result["savedDefinition"]["contentAvailable"] is True
    assert result["savedDefinition"]["revision"] == 1
    assert result["preview"]["exists"] is True
    assert result["preview"]["reportRunId"] == "run_completed"
    assert result["preview"]["status"] == "SUCCEEDED"


def test_preview_of_older_save_is_not_reported_as_matching_current_draft():
    """草稿比已保存版本新时，旧预览不能说成当前草稿已经出数。"""
    definition_save_service.save(
        definitionId="def_stale",
        policyDraft={},
        definitionDraft=_draft(name="机房能耗日报", revision=1),
    )
    _seed_session(
        session_id="sess_stale_preview",
        state="USER_EDITING_TEMPLATE",
        definition=_draft(name="机房能耗日报", revision=5),
        last_report_run={
            "reportRunId": "run_stale",
            "status": "SUCCEEDED",
            "logicalTime": "2026-10-27T00:00:00+08:00",
            "resultRef": "region:r0/output:main",
        },
        report_definition_id="def_stale",
        preview_run_id="run_stale",
    )

    result = recorded_progress_service.describe_recorded_progress("sess_stale_preview")

    assert result["draft"]["revision"] == 5
    assert result["savedDefinition"]["revision"] == 1
    # 预览跑的是旧版本：不能当成当前草稿已完成
    assert result["preview"]["matchesCurrentDraft"] is False


def test_unreadable_saved_definition_is_not_reported_as_absent():
    """保存内容读不到时：说读不到，不能说「没有已保存定义」，也不能断言预览是否匹配。"""
    _seed_session(
        session_id="sess_saved_gone",
        state="READY",
        definition=_draft(revision=1),
        last_report_run={
            "reportRunId": "run_001",
            "status": "SUCCEEDED",
            "logicalTime": "2026-10-27T00:00:00+08:00",
            "resultRef": "region:r0/output:main",
        },
        report_definition_id="def_vanished",
        preview_run_id="run_001",
    )

    result = recorded_progress_service.describe_recorded_progress("sess_saved_gone")

    # 服务端记录过保存标识，所以「存在」是真的；读不到内容要如实说读不到
    assert result["savedDefinition"]["exists"] is True
    assert result["savedDefinition"]["contentAvailable"] is False
    assert result["savedDefinition"]["revision"] is None
    # 读不到保存版本 → 无法判断预览对不对应，不能说「一样」
    assert result["preview"]["matchesCurrentDraft"] is None


# ---------- 切片 6：已记录 ≠ 实时，不编进度 ----------


def test_every_result_declares_it_is_recorded_not_realtime():
    """任何情况下都要说明这是已记录状态，不是实时观察。"""
    _seed_session(
        session_id="sess_recorded_only",
        state="PREVIEWING",
        definition=_draft(),
    )

    result = recorded_progress_service.describe_recorded_progress("sess_recorded_only")

    assert result["isRealtime"] is False
    assert result["realtime"]["available"] is False
    assert result["realtime"]["reason"]
    assert any("已记录" in item for item in result["limitations"])


def test_result_carries_no_percentage_or_time_estimate_field():
    """结果里没有任何可以塞百分比或剩余时间的字段，值里也没有编造的百分比。"""
    _seed_session(
        session_id="sess_no_eta",
        state="VALIDATING",
        definition=_draft(),
    )

    result = recorded_progress_service.describe_recorded_progress("sess_no_eta")

    forbidden_substrings = ("percent", "percentage", "remaining", "estimate", "progress")
    forbidden_exact = {"eta", "etaMinutes", "estimatedMinutes", "percentComplete"}
    percent_pattern = re.compile(r"\d+\s*%")

    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = key.lower()
                assert lowered not in forbidden_exact, key
                assert not any(word in lowered for word in forbidden_substrings), key
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)
        elif isinstance(node, str):
            assert not percent_pattern.search(node), node

    _walk(result)


# ---------- 切片 7：查询不改变任何业务对象 ----------


def test_reading_progress_changes_no_business_object():
    """查进度只是读：草稿、版本、预览、状态、待确认操作、澄清次数都不动。"""
    _seed_session(
        session_id="sess_readonly",
        state="USER_EDITING_TEMPLATE",
        definition=_draft(revision=7),
        pending_edit_plan={
            "planId": "plan_ro",
            "draftRevision": 7,
            "targetRegionId": "r0",
            "operations": [{"op": "delete_region"}],
            "summary": "删除设备耗电排名卡片",
        },
        pending_choice={"state": "USER_EDITING_TEMPLATE", "question": {"id": "q1"}},
    )
    session_service.record_clarification(
        "sess_readonly", key="ro", question="删哪个卡片？"
    )
    before = session_service.repository.get_session("sess_readonly")

    recorded_progress_service.describe_recorded_progress("sess_readonly")

    after = session_service.repository.get_session("sess_readonly")
    assert after.state == before.state
    assert after.snapshot == before.snapshot
    assert after.pendingEditPlan == before.pendingEditPlan
    assert after.pendingChoice == before.pendingChoice
    assert after.reportDefinitionId == before.reportDefinitionId
    assert after.previewRunId == before.previewRunId
    assert session_service.load_clarification("sess_readonly").rounds == 1


def test_reading_progress_does_not_migrate_legacy_pending_plan():
    """老会话的计划还在 snapshot 里时，查进度不能顺手改写快照。

    ``session_service.load_pending_plan`` 名字像只读，实际会迁移并写库；
    本模块不能踩这个坑。
    """
    _seed_session(
        session_id="sess_legacy_plan",
        state="USER_EDITING_TEMPLATE",
        definition=_draft(revision=2),
        snapshot_extra={
            "pendingPlan": {
                "planId": "plan_legacy",
                "draftRevision": 2,
                "targetRegionId": "r0",
                "operations": [],
                "summary": "老会话里的计划",
            }
        },
    )
    before = session_service.repository.get_session("sess_legacy_plan")

    result = recorded_progress_service.describe_recorded_progress("sess_legacy_plan")

    after = session_service.repository.get_session("sess_legacy_plan")
    assert after.snapshot == before.snapshot
    # 老位置的计划照样能被读到，只是不落库
    assert result["pendingEditPlan"]["exists"] is True
    assert result["pendingEditPlan"]["planId"] == "plan_legacy"


# ---------- 切片 8：澄清进度也是已记录事实，且不被消耗 ----------


def test_clarification_progress_is_reported_without_being_consumed():
    """查询要能看到已记录的澄清进度，且不能占掉修改的澄清次数。"""
    _seed_session(session_id="sess_clarify", state="USER_EDITING_TEMPLATE")
    session_service.record_clarification(
        "sess_clarify", key="clarify_k", question="要删哪个卡片？"
    )

    result = recorded_progress_service.describe_recorded_progress("sess_clarify")

    assert result["clarification"]["rounds"] == 1
    assert session_service.load_clarification("sess_clarify").rounds == 1


# ---------- 切片 9：读不到与没权限，不能说成「没有」 ----------


def test_repository_read_failure_is_reported_as_unverifiable(monkeypatch):
    """读取失败时说「无法确认」，不能说成当前对话没有草稿。"""
    _seed_session(
        session_id="sess_read_fail",
        state="WAITING_USER_CONFIRM",
        definition=_draft(name="机房能耗日报", revision=2),
    )

    def _boom(_session_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(session_service.repository, "get_session", _boom)

    result = recorded_progress_service.describe_recorded_progress("sess_read_fail")

    assert result["availability"] == "UNAVAILABLE"
    assert result["stage"] == "UNKNOWN"
    assert result["reason"]
    # 不能断言任何对象不存在
    assert result["draft"]["exists"] is False
    assert result["preview"]["exists"] is False


def test_session_of_another_subject_is_not_disclosed():
    """没有查看权限时，不泄露这个对话里的草稿内容。"""
    session_service.repository.create_workspace(
        id="ws_other",
        name="别的机房的项目",
        subjectRef="machine_room",
        subjectId="room_b",
    )
    _seed_session(
        session_id="sess_other_subject",
        state="WAITING_USER_CONFIRM",
        definition=_draft(name="别人的能耗日报", revision=2),
        workspace_id="ws_other",
    )

    result = recorded_progress_service.describe_recorded_progress(
        "sess_other_subject",
        subject_ref="machine_room",
        subject_id="room_a",
    )

    assert result["availability"] == "PERMISSION_DENIED"
    assert result["stage"] == "UNKNOWN"
    assert result["reason"]
    # 不泄露对方草稿的名称
    assert result["draft"]["exists"] is False
    assert result["draft"]["name"] == ""


def test_session_of_own_subject_is_readable():
    """同一业务对象下的会话照常可读。"""
    session_service.repository.create_workspace(
        id="ws_own",
        name="本机房的项目",
        subjectRef="machine_room",
        subjectId="room_a",
    )
    _seed_session(
        session_id="sess_own_subject",
        state="WAITING_USER_CONFIRM",
        definition=_draft(name="我的能耗日报", revision=2),
        workspace_id="ws_own",
    )

    result = recorded_progress_service.describe_recorded_progress(
        "sess_own_subject",
        subject_ref="machine_room",
        subject_id="room_a",
    )

    assert result["availability"] == "SUCCESS"
    assert result["draft"]["name"] == "我的能耗日报"


# ---------- 切片 9：必选菜单与待确认修改计划不是一回事 ----------


def test_pending_choice_menu_is_reported_separately_from_the_edit_plan():
    """必选菜单和待确认修改计划是两件事，分别回报，不合并。"""
    _seed_session(
        session_id="sess_choice",
        state="WAITING_USER_CONFIRM",
        definition=_draft(),
        pending_choice={
            "state": "WAITING_USER_CONFIRM",
            "question": {"id": "confirm_or_edit", "prompt": "同意还是修改？"},
        },
    )

    result = recorded_progress_service.describe_recorded_progress("sess_choice")

    assert result["pendingChoice"]["exists"] is True
    assert result["pendingChoice"]["questionId"] == "confirm_or_edit"
    assert result["pendingEditPlan"]["exists"] is False
    assert result["stage"] == "DRAFT_PENDING"


# ---------- 切片 10：事实工具 ----------


@pytest.fixture()
def bound_session():
    """把会话 key 与工具上下文绑上，用完清掉。"""

    def _bind(session_id: str, subject_id: str = "room_a"):
        request = AgentRequest(
            conversationId="conv_progress",
            sessionId=session_id,
            state=AgentState.ANALYZING,
            message="刚才那份做到哪了？",
            context=AgentRequestContext(
                projectId="project_001",
                subjectRef="machine_room",
                subjectId=subject_id,
            ),
        )
        bind_session_key(request)
        bind_tool_context(
            ToolContext(
                state=AgentState.ANALYZING,
                subject_ref="machine_room",
                subject_id=subject_id,
                project_id="project_001",
            )
        )
        return session_id

    yield _bind
    clear_session_key()
    clear_tool_context()


def test_tool_reports_recorded_progress_for_the_bound_session(bound_session):
    """工具按当前绑定的对话回报已记录进度。"""
    _seed_session(
        session_id="sess_tool",
        state="WAITING_USER_CONFIRM",
        definition=_draft(name="机房能耗日报", revision=3),
    )
    bound_session("sess_tool")

    result = get_recorded_progress_tool.run()

    assert result["answerKind"] == "recorded_progress"
    assert result["contractVersion"] == "1.0"
    assert result["availability"] == "SUCCESS"
    assert result["stage"] == "DRAFT_PENDING"
    assert result["draft"]["name"] == "机房能耗日报"
    assert result["isRealtime"] is False


def test_tool_reports_unverifiable_when_no_session_is_bound():
    """没有绑定对话时如实说无法确认。"""
    result = get_recorded_progress_tool.run()

    assert result["availability"] == "UNAVAILABLE"
    assert result["reason"]


def test_tool_is_a_read_only_fact_tool_available_in_every_business_state():
    """工具是只读事实工具，在所有业务节点都可用，且不声明结论状态。"""
    assert get_recorded_progress_tool.CATEGORY == ToolCategory.FACT
    assert get_recorded_progress_tool.IS_READ_ONLY is True
    assert get_recorded_progress_tool.APPLICABLE_STATES == LOOKUP_STATES
    # 事实工具不是终结工具，不声明结论状态
    assert not hasattr(get_recorded_progress_tool, "CONCLUSION_STATES")
    assert get_recorded_progress_tool.PARAMETERS["properties"] == {}


def test_tool_satisfies_the_registry_contract_when_registered(monkeypatch):
    """注册进工具表后契约校验要通过：接入方可以直接登记，不用改共用代码。"""
    from app.tools import registry

    monkeypatch.setattr(
        registry, "_TOOLS", [*registry._TOOLS, get_recorded_progress_tool]
    )

    assert registry.validate_tool_contracts() == []
    assert get_recorded_progress_tool in registry.tools_for_state(AgentState.ANALYZING)
    assert get_recorded_progress_tool in registry.tools_for_state(AgentState.READY)
    assert get_recorded_progress_tool in registry.tools_for_state(
        AgentState.USER_EDITING_TEMPLATE
    )


def test_tool_uses_the_stored_record_not_a_stale_client_snapshot(bound_session):
    """客户端带来的快照过时或缺失时，以服务端记录为准。

    工具上下文里带着本轮请求的快照（可能比服务端旧）。查进度不能信它。
    """
    _seed_session(
        session_id="sess_stale_snapshot",
        state="WAITING_USER_CONFIRM",
        definition=_draft(name="机房能耗日报", revision=5),
    )
    bound_session("sess_stale_snapshot")
    # 工具上下文里塞一份过时的草稿
    bind_tool_context(
        ToolContext(
            state=AgentState.WAITING_USER_CONFIRM,
            subject_ref="machine_room",
            subject_id="room_a",
            project_id="project_001",
            definition=ReportDefinitionDraft.model_validate(
                _draft(name="机房能耗日报", revision=2)
            ),
        )
    )

    result = get_recorded_progress_tool.run()

    assert result["availability"] == "SUCCESS"
    assert result["draft"]["revision"] == 5







