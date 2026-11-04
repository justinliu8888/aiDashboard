"""Draft agreement saves locally without starting the downstream pipeline."""
from __future__ import annotations

import pytest

from app.schemas.agent import AgentRequest, AgentState
from app.services.agent_service import agent_service
from tests.test_report_template_generation import _body, _candidate, design_deps
from tests.test_template_confirmation_flow import _confirm_plan, _plan


@pytest.mark.parametrize("editing", [False, True])
def test_agree_stops_after_saving(client, monkeypatch, editing):
    _, plan = _plan(client, monkeypatch, _candidate(), _candidate(topic="新报表"))
    ready = _confirm_plan(client, plan)
    body = _body("同意", message_id="agree_cutoff")
    body["state"] = "USER_EDITING_TEMPLATE" if editing else ready["state"]
    body["snapshots"] = {
        "reportDefinition": ready["domainObjects"]["reportDefinitionDraft"],
        "reportPolicy": ready["domainObjects"]["reportPolicyDraft"],
    }
    response = client.post("/api/agent/chat", json=body)
    assert response.status_code == 200
    agreed = response.json()
    assert agreed["error"] is None
    assert agreed["state"] == "ANALYZING"
    assert agreed["reply"] == "草稿已保存，后续流程待接入。"
    assert agreed["uiActions"] == ["save_definition_draft"]
    assert [tool["type"] for tool in agreed["toolRequests"]] == ["SAVE_DEFINITION_DRAFT"]
    assert agreed["domainObjects"]["reportDefinitionDraft"] is not None
    assert agreed["reportPreviewPayload"] is not None
    assert agreed["questions"] == []
    assert agreed["designPhase"] == "PLAN_NEEDED"
    restored = client.get("/api/sessions/sess_tpl_1").json()
    assert restored["state"] == "ANALYZING"
    assert restored["pendingChoice"] is None
    assert restored["snapshot"]["definition"] == agreed["domainObjects"]["reportDefinitionDraft"]

    next_body = _body("生成 A 机房昨日能耗日报", message_id="new_report_after_save")
    next_body["state"] = agreed["state"]
    next_response = client.post("/api/agent/chat", json=next_body).json()
    assert next_response["error"] is None
    assert next_response["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert "plan_ready" in next_response["uiActions"]


def test_stale_client_advance_does_not_start_validation():
    request = AgentRequest(
        conversationId="cutoff_stale_client",
        state=AgentState.SAVE_DEFINITION_DRAFT,
        message="__advance__",
        context={"projectId": "project_001", "subjectRef": "machine_room", "subjectId": "room_a"},
    )
    response = agent_service._auto_advance(request)
    assert response.state == AgentState.ANALYZING
    assert response.reply == "草稿已保存，后续流程待接入。"
    assert response.uiActions == []
    assert response.toolRequests == []
