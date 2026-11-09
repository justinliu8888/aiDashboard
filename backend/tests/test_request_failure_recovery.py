"""请求执行异常后保留菜单与草稿，释放写锁并允许下一次请求。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.agent import AgentResponse, AgentState
from app.services.agent_service import agent_service
from app.services.draft_workspace import draft_workspace
from app.services.session_service import session_service
from tests.test_draft_open_scene import _make_draft, QUESTION


@pytest.mark.parametrize("stream", [False, True])
def test_execution_error_restores_choice_and_allows_retry(client, monkeypatch, stream):
    draft_id, session_id = "draft_failure", "sess_failure"
    before = _make_draft(draft_id, session_id)
    choice = {"state": "USER_EDITING_TEMPLATE", "question": QUESTION}
    session_service.save_pending_choice(session_id, choice)

    async def fail(request, **kwargs):
        session_service.save_pending_choice(session_id, {
            "state": "NEEDS_CLARIFICATION", "question": {"id": "uncommitted"},
        })
        raise RuntimeError("injected upstream HTTP error")

    monkeypatch.setattr(agent_service, "chat", fail)
    body = {
        "agentProtocolVersion": "1.0", "conversationId": session_id,
        "sessionId": session_id, "messageId": "failure_attempt",
        "state": "USER_EDITING_TEMPLATE", "message": "修改",
        "context": {"projectId": "project_001", "subjectRef": "machine_room", "subjectId": "room_a"},
    }
    endpoint = "/api/agent/chat" + ("/stream" if stream else "")
    with TestClient(app, raise_server_exceptions=False) as transport:
        result = transport.post(endpoint, json=body)
    if stream:
        assert "event: error" in result.text
        assert "event: final" not in result.text
    else:
        assert result.status_code == 500
    assert session_service.load_pending_choice(session_id) == choice
    after = draft_workspace.load_draft(draft_id)
    assert after.definition == before.definition
    assert after.revision == before.revision
    assert draft_workspace.load_task("task_failure_attempt").state == "FAILED"
    assert draft_workspace.acquire(draft_id, "retry-probe")
    draft_workspace.release(draft_id, "retry-probe")

    async def succeed(request, **kwargs):
        return AgentResponse(conversationId=session_id, state=AgentState.USER_EDITING_TEMPLATE, reply="可以继续选择")

    monkeypatch.setattr(agent_service, "chat", succeed)
    body["messageId"] = "retry_attempt"
    result = client.post(endpoint, json=body)
    assert result.status_code == 200
    if stream:
        assert "event: final" in result.text
    assert draft_workspace.load_task("task_retry_attempt").state == "COMPLETED"
