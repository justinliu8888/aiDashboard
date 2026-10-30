"""工单 #62（统一开发 07）：编辑态与选择阶段 —— 选择锁与统一对话。

覆盖工单 10 条验收标准里**后端能作证**的那些：

    - 菜单带 lockScope（none / menu_only / input_and_menu），响应与
      sessions.pending_choice 落库副本一致（契约表 §5）；
    - 模板已生成、等待下一步操作选择 → 锁输入；待确认修改计划出现 → 恢复锁；
    - 选「修改 / 继续调整」后回到统一对话，旧计划不再可接受，模板本身不变；
    - 咨询与问题反馈只追加聊天：不改定义版本、不动待确认计划、不占澄清额度；
    - 澄清额度一轮：一轮之后给非阻塞说明与可选替代；局部调整不重置额度；
    - 「你自己决定 / 别问了」停止阻塞并给可见假设，不代用户接受；
    - 界面锁定只是引导：直接调接口仍不能绕过确认与权限。

前端那一半（输入框 / 发送 / Enter / 快捷入口真的被锁住）在
frontend/tests/workbench_edit_lock.test.mjs。
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.agent import (
    LOCK_SCOPE_INPUT_AND_MENU,
    LOCK_SCOPE_MENU_ONLY,
    LOCK_SCOPE_NONE,
    AgentQuestion,
)
from app.services.report_design_service import report_design_service
from app.services.report_draft_service import build_definition_draft
from app.services.session_service import session_service
from app.storage.sqlite_store import SqliteSessionRepository

DESIGN_SESSION_ID = "sess_design_lock"
DESIGN_CONVERSATION_ID = "conv_design_lock"

#: 只读事实的四态（app.schemas.fact.FactStatus）。
FOUR_FACT_STATUSES = {"FOUND", "ABSENT", "UNAVAILABLE", "FORBIDDEN"}


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture()
def definition():
    return build_definition_draft("A 机房能耗日报", "room_a")


@pytest.fixture()
def design_client(monkeypatch, tmp_path):
    """走真实设计流程的客户端：只替换外部模型调用这一处边界。"""
    repo = SqliteSessionRepository(tmp_path / "design_sessions.db")
    monkeypatch.setattr(session_service, "_repo", repo)
    monkeypatch.setattr(
        report_design_service, "_injected_repository", None, raising=False
    )
    report_design_service.reset_for_tests(model_call=_StubModel())
    yield TestClient(app)
    report_design_service.reset_for_tests()


def _snap(definition):
    return {"reportDefinition": definition.model_dump(mode="json")}


class _StubModel:
    """设计流程的模型替身：返回一份合法的方案候选。"""

    async def __call__(self, messages: list[dict[str, Any]]) -> str:
        return json.dumps(
            {
                "kind": "plan_candidate",
                "candidate": {
                    "topic": "PUE 专题报表",
                    "requirements": {
                        "metrics": ["PUE"],
                        "subject": None,
                        "timeRange": None,
                        "exclusions": [],
                    },
                    "sections": [
                        {
                            "id": "section_pue",
                            "title": "PUE 总览",
                            "metricIntents": ["PUE"],
                            "componentType": "metric_card",
                        }
                    ],
                    "assumptions": ["PUE 统计口径待核实，数据待连接"],
                    "suggestions": [],
                },
            },
            ensure_ascii=False,
        )


def _design_body(message: str, *, message_id: str, **context):
    return {
        "agentProtocolVersion": "1.0",
        "conversationId": DESIGN_CONVERSATION_ID,
        "sessionId": DESIGN_SESSION_ID,
        "messageId": message_id,
        "state": "ANALYZING",
        "message": message,
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
            **context,
        },
    }


def _post_design(client: TestClient, message: str, *, message_id: str, **context):
    return client.post(
        "/api/agent/chat", json=_design_body(message, message_id=message_id, **context)
    )


@pytest.fixture()
def design_ready(design_client):
    """把会话推到「模板已生成、等待下一步操作选择」这一步。

    返回 (client, 响应)。
    """
    client = design_client
    built = _post_design(client, "生成 PUE 报表", message_id="msg_1").json()
    plan = built["domainObjects"]["reportPlan"]
    payload = _post_design(
        client,
        "确认",
        message_id="msg_2",
        designAction="CONFIRM_PLAN",
        planId=plan["id"],
        basePlanRevision=plan["revision"],
    ).json()
    return client, payload


# --------------------------------------------------------------------------
# AC1 等待下一步操作选择 → 锁输入；文字方案阶段不锁
# --------------------------------------------------------------------------


def test_design_draft_ready_menu_locks_the_input(design_ready):
    """模板已生成、等待下一步操作选择：菜单写 input_and_menu。"""
    _client, payload = design_ready

    menu = payload["questions"][0]
    assert menu["id"] == "template_edit_confirm"
    assert menu["prompt"] == "请选择下一步操作："
    assert menu["selectionOnly"] is True
    assert menu["lockScope"] == LOCK_SCOPE_INPUT_AND_MENU


def test_lock_scope_is_persisted_in_pending_choice(design_ready):
    """同一份菜单既有响应又有落库副本（契约表 §5.3）：两处必须一致。"""
    client, payload = design_ready

    detail = client.get(f"/api/sessions/{DESIGN_SESSION_ID}").json()
    stored = detail["pendingChoice"]["question"]
    assert stored["lockScope"] == LOCK_SCOPE_INPUT_AND_MENU
    assert stored["id"] == payload["questions"][0]["id"]
    assert stored["lockScope"] == payload["questions"][0]["lockScope"]


def test_plan_stage_does_not_lock_the_input(design_client):
    """文字方案阶段没有「只能选」的菜单 → 自由输入不被一并锁死。"""
    payload = _post_design(design_client, "生成 PUE 报表", message_id="msg_1").json()

    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"
    locked = [
        question
        for question in payload.get("questions", [])
        if question.get("selectionOnly")
        and question.get("options")
        and question.get("lockScope") == LOCK_SCOPE_INPUT_AND_MENU
    ]
    assert locked == []


def test_pending_edit_plan_menu_locks_the_input(post_chat, definition):
    """待确认修改计划出现时恢复选择锁。"""
    payload = post_chat(
        "把趋势图改成按分钟粒度",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    menu = payload["questions"][0]
    assert menu["id"] == "pending_edit_plan_confirm"
    assert menu["lockScope"] == LOCK_SCOPE_INPUT_AND_MENU


def test_old_pending_choice_without_lock_scope_reads_as_menu_only(post_chat, definition):
    """老会话的菜单没有 lockScope → 按 menu_only 处理（契约表 §5.3）。"""
    session_service.repository.upsert_session(
        session_id="conv_test",
        state="WAITING_USER_CONFIRM",
        snapshot={},
    )
    session_service.repository.set_pending_choice(
        "conv_test",
        {
            "state": "WAITING_USER_CONFIRM",
            "question": {
                "id": "template_edit_confirm",
                "prompt": "请选择下一步操作：",
                "expects": "SINGLE_CHOICE",
                "selectionOnly": True,
                "options": [{"value": "同意", "label": "同意"}],
            },
        },
    )

    payload = post_chat("随便说点什么", state="WAITING_USER_CONFIRM").json()

    # 老数据被读回来时明确按 menu_only 处理 —— 不锁输入，也不凭空解锁。
    assert payload["questions"][0]["lockScope"] == LOCK_SCOPE_MENU_ONLY


# --------------------------------------------------------------------------
# AC2 选「修改 / 调整」后恢复自由输入并进入统一对话
# --------------------------------------------------------------------------


def test_modify_choice_returns_to_unified_dialog(design_ready):
    """选「修改」：回到统一对话，输入不再锁，也不用再选一次「询问」。"""
    client, _payload = design_ready

    response = _post_design(client, "修改", message_id="msg_3").json()

    assert response["state"] == "USER_EDITING_TEMPLATE"
    # 统一对话的入口问题不是「只能选」的菜单 → 前端不锁输入。
    menus = [
        question
        for question in response["questions"]
        if question.get("selectionOnly") and question.get("options")
    ]
    assert menus == []
    assert response["questions"][0]["lockScope"] == LOCK_SCOPE_NONE
    # 菜单已失效：服务端不再挂着待确认选择。
    assert session_service.load_pending_choice(DESIGN_SESSION_ID) is None


def test_continue_adjust_returns_to_unified_dialog(post_chat, definition):
    """选「继续调整」：回到统一对话，旧计划不再可接受，模板本身不变。"""
    candidate = post_chat(
        "把趋势图改成按分钟粒度",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()
    assert candidate["domainObjects"]["pendingEditPlan"] is not None
    revision_before = definition.metadata["revision"]

    response = post_chat(
        "继续调整",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert response["state"] == "USER_EDITING_TEMPLATE"
    # 旧计划撤销：不能再接受。
    assert session_service.load_pending_plan("conv_test") is None
    # 模板本身不变。
    assert definition.metadata["revision"] == revision_before
    assert response["domainObjects"]["pendingEditPlan"] is None
    # 回到统一对话：给的是可继续说的入口，不是「只能选」的菜单。
    menus = [
        question
        for question in response["questions"]
        if question.get("selectionOnly") and question.get("options")
    ]
    assert menus == []
    assert response["questions"][0]["lockScope"] == LOCK_SCOPE_NONE


# --------------------------------------------------------------------------
# AC3 咨询与问题反馈只追加聊天
# --------------------------------------------------------------------------


def test_edit_state_consultation_only_appends_chat(post_chat, definition):
    """编辑态咨询：不改定义版本、不动待确认计划、不占澄清额度。"""
    response = post_chat(
        "这个能按分钟看吗？",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        selectedRegionId="region_hourly_trend",
    ).json()

    assert response["turnKind"] == "query_answer"
    assert response["state"] == "USER_EDITING_TEMPLATE"
    assert response["toolRequests"] == []
    assert response["domainObjects"]["pendingEditPlan"] is None
    assert response["domainObjects"]["changeSet"] is None
    assert response["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 0
    assert definition.metadata["revision"] == 0
    assert session_service.load_clarification("conv_test") is None


def test_edit_state_issue_feedback_only_appends_chat(post_chat, definition):
    """问题反馈走同一套只读契约：只追加聊天。"""
    response = post_chat(
        "好丑啊，一个点……",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert response["turnKind"] == "query_answer"
    assert response["toolRequests"] == []
    assert response["domainObjects"]["pendingEditPlan"] is None
    assert response["domainObjects"]["changeSet"] is None
    assert definition.metadata["revision"] == 0
    assert session_service.load_clarification("conv_test") is None


def test_consultation_does_not_auto_save_validate_or_run(post_chat, definition):
    """咨询不触发自动保存、校验或试运行。"""
    response = post_chat(
        "这个能按分钟看吗？",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        selectedRegionId="region_hourly_trend",
    ).json()

    assert response["toolRequests"] == []
    assert response["uiActions"] == ["query_answer"]
    assert response["state"] not in {
        "SAVE_DEFINITION_DRAFT",
        "VALIDATING",
        "PREVIEWING",
    }


# --------------------------------------------------------------------------
# AC4 复用既有的只读事实查证与回答契约；查询与执行工具权限不合并
# --------------------------------------------------------------------------


def test_edit_state_consultation_uses_the_readonly_fact_contract(post_chat, definition):
    """编辑态咨询复用同一套四态事实语义。"""
    response = post_chat(
        "这个能按分钟看吗？",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
        selectedRegionId="region_hourly_trend",
    ).json()

    debug = response["debug"]
    assert debug["queryMode"] is True
    assert debug["factStatus"] in FOUR_FACT_STATUSES


def test_query_toolset_has_no_execution_capability():
    """查询工具与执行工具的权限不合并（ADR 0002）。"""
    from app.tools import registry

    query_names = {tool.NAME for tool in registry.tools_for_query()}

    assert "submit_query_answer" in query_names
    for execution_only in ("submit_edit_plan", "submit_edit_answer"):
        assert execution_only not in query_names


# --------------------------------------------------------------------------
# AC5 / AC6 / AC7 选择锁按服务端当前状态判断
# --------------------------------------------------------------------------


def test_answered_menu_stops_locking_the_input(design_ready):
    """菜单被答掉之后就不再锁：历史菜单不永久锁住输入。"""
    client, _payload = design_ready

    _post_design(client, "修改", message_id="msg_3")

    assert session_service.load_pending_choice(DESIGN_SESSION_ID) is None


def test_restore_reads_the_lock_scope_from_the_server(design_ready):
    """刷新后按服务端阶段恢复锁或不锁（后端侧：pending_choice 是权威源）。"""
    client, _payload = design_ready

    detail = client.get(f"/api/sessions/{DESIGN_SESSION_ID}").json()

    assert detail["pendingChoice"]["question"]["lockScope"] == LOCK_SCOPE_INPUT_AND_MENU


def test_direct_api_call_cannot_bypass_the_locked_menu(design_ready):
    """界面锁定只是引导：直接调接口在等待选择期间仍不能绕过确认。"""
    client, _payload = design_ready

    response = _post_design(
        client, "把标题改成新标题", message_id="msg_bypass"
    ).json()

    assert response["uiActions"] == ["selection_required"]
    assert response["domainObjects"]["pendingEditPlan"] is None
    assert response["domainObjects"].get("changeSet") is None
    # 待确认菜单还在：没有被这一句自由输入顶掉。
    assert session_service.load_pending_choice(DESIGN_SESSION_ID) is not None


# --------------------------------------------------------------------------
# AC9 澄清额度：一轮、局部调整不重置、咨询不计入
# --------------------------------------------------------------------------


def test_one_round_then_non_blocking_explanation(post_chat, definition):
    """一轮之后仍缺安全目标：保留当前报告，给非阻塞说明与可选替代。"""
    first = post_chat(
        "改一下", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition)
    ).json()
    assert first["questions"], "第一轮应该真的抛出追问"

    second = post_chat(
        "改一下", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition)
    ).json()

    assert "追问过 1 轮" in second["reply"]
    assert "草稿没有修改" in second["reply"]
    # 非阻塞：不再抛问题。
    assert second["questions"] == []
    assert second["uiActions"] == ["template_edit_clarification_limit"]
    # 保留当前报告：定义还在、版本没变。
    assert second["domainObjects"]["reportDefinitionDraft"] is not None
    assert definition.metadata["revision"] == 0
    # 可选替代要说清楚下一步能怎么做。
    assert "你可以选一种继续方式" in second["reply"]


def test_local_adjustment_does_not_reset_the_quota(post_chat, definition):
    """局部调整不重置额度：候选计划落库后同一项仍然只能问一轮。"""
    post_chat("改一下", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition)).json()
    assert session_service.load_clarification("conv_test").rounds == 1

    # 局部调整：把这项修改说完整，产出候选计划。
    candidate = post_chat(
        "把趋势图改成按分钟粒度",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()
    assert candidate["domainObjects"]["pendingEditPlan"] is not None
    # 候选计划落库**不**清额度（规格第 19 条）。
    assert session_service.load_clarification("conv_test").rounds == 1

    # 「继续调整」把这项修改接着往下调：额度也不重开。
    adjust = post_chat(
        "继续调整", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition)
    ).json()
    assert adjust["state"] == "USER_EDITING_TEMPLATE"
    assert session_service.load_clarification("conv_test").rounds == 1

    # 同一项再来一次：额度仍然被占着 → 给非阻塞说明，不重复追问。
    again = post_chat(
        "改一下", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition)
    ).json()
    assert "追问过 1 轮" in again["reply"]
    assert again["questions"] == []


def test_continue_adjust_does_not_reopen_the_quota(post_chat, definition):
    """「继续调整」不是新任务：额度不重开。"""
    post_chat("改一下", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition)).json()
    assert session_service.load_clarification("conv_test").rounds == 1

    post_chat(
        "把趋势图改成按分钟粒度",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()
    post_chat("继续调整", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition)).json()

    assert session_service.load_clarification("conv_test").rounds == 1


# --------------------------------------------------------------------------
# AC9 / 规格第 16 条：「你自己决定 / 别问了」停止阻塞
# --------------------------------------------------------------------------


def test_delegation_stops_blocking_and_shows_assumptions(post_chat, definition):
    """用户说「别问了」：不再追问，给可见假设，不代用户接受。"""
    response = post_chat(
        "别问了",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    # 不阻塞：没有抛问题。
    assert response["questions"] == []
    # 可见假设：说清楚按什么假设继续。
    assert "假设" in response["reply"]
    assert "不替你接受任何改动" in response["reply"]
    # 不代用户接受：没有候选计划、没有改草稿。
    assert response["domainObjects"]["pendingEditPlan"] is None
    assert response["domainObjects"].get("changeSet") is None
    assert definition.metadata["revision"] == 0
    assert session_service.load_pending_plan("conv_test") is None
    # 不占澄清额度。
    assert session_service.load_clarification("conv_test") is None


def test_delegation_does_not_consume_the_quota(post_chat, definition):
    """「你自己决定」不消耗额度，也不把额度重开。"""
    post_chat("改一下", state="USER_EDITING_TEMPLATE", snapshots=_snap(definition)).json()
    assert session_service.load_clarification("conv_test").rounds == 1

    response = post_chat(
        "你自己决定吧",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert response["questions"] == []
    assert session_service.load_clarification("conv_test").rounds == 1


def test_delegation_with_a_complete_request_still_produces_a_candidate(
    post_chat, definition
):
    """「把标题改成 X，你看着办」：仍然出候选，但假设写明、仍要用户接受。"""
    response = post_chat(
        "把趋势图改成按分钟粒度，你看着办",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    assert response["domainObjects"]["pendingEditPlan"] is not None
    assert "假设" in response["reply"]
    # 仍然要用户点「接受修改」才改草稿 —— 不代用户接受。
    assert "接受后才会校验并修改草稿" in response["reply"]
    assert definition.metadata["revision"] == 0


# --------------------------------------------------------------------------
# AC10 不新增独立「询问」按钮来绕过选择锁
# --------------------------------------------------------------------------


def test_pending_edit_menu_has_no_separate_inquiry_option(post_chat, definition):
    """待确认修改计划的选项只有契约冻结的三个，没有「询问」。"""
    payload = post_chat(
        "把趋势图改成按分钟粒度",
        state="USER_EDITING_TEMPLATE",
        snapshots=_snap(definition),
    ).json()

    values = [option["value"] for option in payload["questions"][0]["options"]]
    assert values == ["接受修改", "继续调整", "取消"]
    assert "询问" not in values


# --------------------------------------------------------------------------
# 契约表 §5 的字段口径（回归钉住）
# --------------------------------------------------------------------------


def test_lock_scope_derivation_follows_the_contract():
    """阶段判据：契约表 §5.2。"""
    design_menu = AgentQuestion(
        id="template_edit_confirm",
        prompt="请选择下一步操作：",
        expects="SINGLE_CHOICE",
        selectionOnly=True,
        options=[{"value": "同意", "label": "同意"}],
    )
    edit_confirm_menu = AgentQuestion(
        id="template_edit_confirm",
        prompt="请确认当前编辑后的草稿：",
        expects="SINGLE_CHOICE",
        selectionOnly=True,
        options=[{"value": "同意", "label": "同意"}],
    )
    pending_plan_menu = AgentQuestion(
        id="pending_edit_plan_confirm",
        prompt="确认这次修改：",
        expects="SINGLE_CHOICE",
        selectionOnly=True,
        options=[{"value": "接受修改", "label": "接受修改"}],
    )

    assert design_menu.lockScope == LOCK_SCOPE_INPUT_AND_MENU
    assert pending_plan_menu.lockScope == LOCK_SCOPE_INPUT_AND_MENU
    # 确认编辑后的草稿不是「等待下一步操作选择」→ 不锁输入。
    assert edit_confirm_menu.lockScope == LOCK_SCOPE_MENU_ONLY
    # 显式给的值不被推导覆盖（老数据按 menu_only 处理）。
    explicit = AgentQuestion(
        id="template_edit_confirm",
        prompt="请选择下一步操作：",
        expects="SINGLE_CHOICE",
        lockScope=LOCK_SCOPE_MENU_ONLY,
    )
    assert explicit.lockScope == LOCK_SCOPE_MENU_ONLY
