"""草稿回复与卡内编辑协议专项，外部模型只使用预设返回值。"""
from __future__ import annotations

import json

import pytest

from app.schemas.agent import AgentRequest
from app.services.report_design_service import report_design_service
from app.services.report_draft_service import build_definition_draft
from app.services.report_template_service import build_template_design
from app.services.session_service import session_service
from tests.test_template_confirmation_flow import (
    _confirm_plan,
    _confirm_template,
    _plan,
)
from tests.test_report_template_generation import _body, _pue_candidate, design_deps


def _post(client, message, *, state, **kwargs):
    body = _body(message, **kwargs)
    body["state"] = state
    return client.post("/api/agent/chat", json=body)


def _legacy_template(plan):
    template = build_template_design(plan, subject_id="room_a").model_dump(mode="json")
    conn = session_service.repository.conn
    conn.execute(
        "INSERT INTO report_design_templates"
        " (session_id, template_id, template_revision, plan_id, plan_revision,"
        " data_status, template_json, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "sess_tpl_1", template["id"], template["revision"], plan["id"],
            plan["revision"], template.get("dataStatus"), json.dumps(template),
            "2026-11-01T00:00:00", "2026-11-01T00:00:00",
        ),
    )
    conn.execute(
        "UPDATE report_design_sessions SET design_phase = 'TEMPLATE_READY' WHERE session_id = ?",
        ("sess_tpl_1",),
    )
    conn.commit()
    return template


def _assert_next_action(menu):
    assert menu["id"] == "template_edit_confirm"
    assert menu["prompt"] == "请选择下一步操作："
    assert menu["allowsOther"] is True
    assert menu["otherLabel"] == "其他"
    assert menu["otherPlaceholder"]
    assert [item["value"] for item in menu["options"]] == ["同意", "修改", "重新开始"]


@pytest.mark.parametrize("legacy", [False, True])
def test_simulated_draft_reply_and_persisted_next_action(client, monkeypatch, legacy):
    _, plan = _plan(client, monkeypatch, _pue_candidate())
    payload = (
        _confirm_template(client, _legacy_template(plan))
        if legacy else _confirm_plan(client, plan)
    )
    assert payload["error"] is None
    assert "模拟业务数据" in payload["reply"]
    assert "名称占位" in payload["reply"]
    for forbidden in ("接入真实数据", "自动替换", "待接通", "尚未接通", "能力已接通"):
        assert forbidden not in payload["reply"]
    menu = payload["questions"][0]
    _assert_next_action(menu)
    stored = client.get("/api/sessions/sess_tpl_1").json()
    assert stored["pendingChoice"]["question"] == menu
    assert stored["snapshot"]["definition"] == payload["domainObjects"]["reportDefinitionDraft"]


def test_duplicate_confirmation_keeps_inline_next_action(client, monkeypatch):
    _, plan = _plan(client, monkeypatch, _pue_candidate())
    first = _confirm_plan(client, plan)
    again = _confirm_plan(client, plan, "confirm_again")
    assert again["error"] is None
    assert again["domainObjects"]["reportDefinitionDraft"] == first["domainObjects"]["reportDefinitionDraft"]
    _assert_next_action(again["questions"][0])
    assert "待接通" not in again["reply"]
    assert "接入真实数据" not in again["reply"]


@pytest.mark.parametrize("handshake", [False, True])
def test_inline_text_direct_and_two_request_handshake(client, monkeypatch, handshake):
    _, plan = _plan(client, monkeypatch)
    ready = _confirm_plan(client, plan)
    draft = ready["domainObjects"]["reportDefinitionDraft"]
    context: dict = {}
    if handshake:
        action = _post(client, "修改", message_id="inline_action", state=ready["state"]).json()
        assert action["error"] is None
        assert action["state"] == "USER_EDITING_TEMPLATE"
        assert action["domainObjects"]["pendingEditPlan"] is None
        menu = action["questions"][0]
        # 「修改」进入**受限修改输入**这一步：步骤身份持久记在库里，
        # 下一轮提交必须带上它（权威步骤契约）。
        assert menu["id"] == "template_edit_request"
        assert menu["allowsOther"] is True
        assert menu["selectionOnly"] is False
        assert menu["lockScope"] == "input_and_menu"
        assert menu["stepKind"] == "edit_request"
        stored = session_service.load_pending_choice("sess_tpl_1")
        assert stored is not None
        assert stored["question"]["stepId"] == menu["stepId"]
        context = {"stepId": menu["stepId"], "stepVersion": menu["stepVersion"]}
    proposal = _post(
        client, "报表名称改成内联日报", message_id="inline_text",
        state="USER_EDITING_TEMPLATE" if handshake else ready["state"],
        context=context,
    ).json()
    assert proposal["error"] is None
    assert proposal["uiActions"] == ["pending_edit_plan"]
    assert proposal["domainObjects"]["reportDefinitionDraft"] == draft
    assert proposal["domainObjects"]["changeSet"] is None
    assert session_service.load_pending_plan("sess_tpl_1") is not None


def test_user_json_draft_reply_keeps_its_source_wording(client, monkeypatch):
    _, plan = _plan(client, monkeypatch, _pue_candidate())
    ready = _confirm_plan(client, plan)
    draft = ready["domainObjects"]["reportDefinitionDraft"]
    draft["metadata"]["sourceMode"] = "user_json"
    monkeypatch.setattr(report_design_service, "_attach_report_preview_payload", lambda request, response, **kwargs: response)
    request = AgentRequest.model_validate({
        "agentProtocolVersion": "1.0", "conversationId": "conv_mode",
        "state": "WAITING_USER_CONFIRM", "message": "确认",
        "context": {"projectId": "project_001", "subjectRef": "machine_room", "subjectId": "room_a"},
    })
    response = report_design_service._draft_response(
        request, plan=plan, template=ready["domainObjects"]["templateDesign"], draft=draft,
    )
    assert "用户提供的数据，未经业务核验" in response.reply
    assert "模拟业务数据" not in response.reply
    assert "能力待接通" in response.reply


@pytest.mark.parametrize("accepted", [False, True])
def test_edit_menus_support_inline_action_then_text(post_chat, accepted):
    draft = build_definition_draft("A 机房能耗日报", "room_a").model_dump(mode="json")
    snapshot = {"reportDefinition": draft}
    post_chat("报表名称改成首轮日报", state="USER_EDITING_TEMPLATE", snapshots=snapshot)
    if accepted:
        result = post_chat("接受修改", state="USER_EDITING_TEMPLATE", snapshots=snapshot).json()
        draft = result["domainObjects"]["reportDefinitionDraft"]
        snapshot = {"reportDefinition": draft}
    action = post_chat(
        "修改" if accepted else "继续修改", state="USER_EDITING_TEMPLATE", snapshots=snapshot,
    ).json()
    assert action["error"] is None
    assert action["domainObjects"]["pendingEditPlan"] is None
    assert action["questions"][0]["allowsOther"] is True
    assert action["questions"][0]["selectionOnly"] is False
    context = {}
    if accepted:
        # 「修改」进入**受限修改输入**这一步：步骤身份持久记在库里，
        # 下一轮提交必须带上它（权威步骤契约）。
        menu = action["questions"][0]
        assert menu["id"] == "template_edit_request"
        assert menu["stepKind"] == "edit_request"
        assert menu["lockScope"] == "input_and_menu"
        assert session_service.load_pending_choice("conv_test") is not None
        context = {"stepId": menu["stepId"], "stepVersion": menu["stepVersion"]}
    else:
        # 「继续修改」回到**统一对话**：自由输入入口照旧可用。
        assert action["questions"][0]["lockScope"] == "none"
        assert session_service.load_pending_choice("conv_test") is None
    proposal = post_chat(
        "报表名称改成内联后续日报", state="USER_EDITING_TEMPLATE", snapshots=snapshot,
        **context,
    ).json()
    assert proposal["error"] is None
    assert proposal["uiActions"] == ["pending_edit_plan"]
    assert proposal["domainObjects"]["reportDefinitionDraft"] == draft
