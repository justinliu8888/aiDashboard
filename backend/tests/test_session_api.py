"""会话与项目接口单测，以及对话落库行为。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.session_service import session_service
from app.storage.sqlite_store import SqliteSessionRepository


def _post_chat(client: TestClient, message: str, state: str = "ANALYZING", **extra):
    body = {
        "agentProtocolVersion": "1.0",
        "conversationId": "conv_test",
        "state": state,
        "message": message,
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    }
    body.update(extra)
    return client.post("/api/agent/chat", json=body)


# ---------- 会话接口 ----------


def test_create_session_returns_backend_generated_id(client: TestClient):
    response = client.post("/api/sessions", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["sessionId"].startswith("sess_")
    assert body["state"] == "ANALYZING"


def test_create_session_does_not_create_row(client: TestClient):
    """只铸 id 不建库行，侧边栏才不会堆空会话。"""
    session_id = client.post("/api/sessions", json={}).json()["sessionId"]
    assert session_service.repository.get_session(session_id) is None


def test_list_sessions_is_empty_before_any_chat(client: TestClient):
    response = client.get("/api/sessions")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


def test_chat_creates_session_with_user_and_assistant_messages(client: TestClient):
    session_id = client.post("/api/sessions", json={}).json()["sessionId"]
    _post_chat(client, "帮我看异常设备", sessionId=session_id)

    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["messageCount"] == 2
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["user", "assistant"]
    assert detail["messages"][0]["content"] == "帮我看异常设备"
    assert detail["title"] == "帮我看异常设备"


def test_list_sessions_after_chat(client: TestClient):
    session_id = client.post("/api/sessions", json={}).json()["sessionId"]
    _post_chat(client, "生成日报", sessionId=session_id)

    body = client.get("/api/sessions").json()
    assert body["total"] == 1
    assert body["items"][0]["sessionId"] == session_id
    assert body["items"][0]["title"] == "生成日报"


def test_advance_message_is_not_stored_as_user_message(client: TestClient):
    """__advance__ 是内部控制消息，用户没输入过它。"""
    session_id = client.post("/api/sessions", json={}).json()["sessionId"]
    _post_chat(client, "生成日报", sessionId=session_id, state="ANALYZING")
    _post_chat(client, "__advance__", sessionId=session_id, state="SAVE_DEFINITION_DRAFT")

    detail = client.get(f"/api/sessions/{session_id}").json()
    user_messages = [m for m in detail["messages"] if m["role"] == "user"]
    assert [m["content"] for m in user_messages] == ["生成日报"]


def test_replayed_message_with_same_id_does_not_duplicate(client: TestClient):
    """「继续工作」重发同一条消息时靠 messageId 去重。"""
    session_id = client.post("/api/sessions", json={}).json()["sessionId"]
    _post_chat(client, "生成日报", sessionId=session_id, messageId="m1")
    _post_chat(client, "生成日报", sessionId=session_id, messageId="m1")

    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["messageCount"] == 2


def test_update_session_title_and_workspace(client: TestClient):
    session_id = client.post("/api/sessions", json={}).json()["sessionId"]
    _post_chat(client, "生成日报", sessionId=session_id)

    workspace_id = client.post("/api/workspaces", json={"name": "原型"}).json()["id"]
    response = client.patch(
        f"/api/sessions/{session_id}",
        json={"title": "新标题", "workspaceId": workspace_id},
    )
    assert response.status_code == 200
    assert response.json()["title"] == "新标题"
    assert response.json()["workspaceId"] == workspace_id


def test_delete_session_soft_deletes(client: TestClient):
    session_id = client.post("/api/sessions", json={}).json()["sessionId"]
    _post_chat(client, "生成日报", sessionId=session_id)

    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert client.get(f"/api/sessions/{session_id}").status_code == 404
    assert client.get("/api/sessions").json()["total"] == 0


def test_get_missing_session_returns_404(client: TestClient):
    assert client.get("/api/sessions/sess_nope").status_code == 404


# ---------- 项目接口 ----------


def test_create_and_list_workspace(client: TestClient):
    created = client.post("/api/workspaces", json={"name": "智能报表原型"})
    assert created.status_code == 200
    workspace_id = created.json()["id"]

    listed = client.get("/api/workspaces").json()
    assert [w["id"] for w in listed] == [workspace_id]
    assert listed[0]["sessionCount"] == 0


def test_workspace_session_count_reflects_sessions(client: TestClient):
    workspace_id = client.post("/api/workspaces", json={"name": "A"}).json()["id"]
    session_id = client.post("/api/sessions", json={}).json()["sessionId"]
    _post_chat(client, "生成日报", sessionId=session_id)
    client.patch(f"/api/sessions/{session_id}", json={"workspaceId": workspace_id})

    assert client.get("/api/workspaces").json()[0]["sessionCount"] == 1


def test_create_workspace_rejects_blank_name(client: TestClient):
    assert client.post("/api/workspaces", json={"name": "  "}).status_code == 400


def test_delete_workspace_keeps_session(client: TestClient):
    workspace_id = client.post("/api/workspaces", json={"name": "A"}).json()["id"]
    session_id = client.post("/api/sessions", json={}).json()["sessionId"]
    _post_chat(client, "生成日报", sessionId=session_id)
    client.patch(f"/api/sessions/{session_id}", json={"workspaceId": workspace_id})

    assert client.delete(f"/api/workspaces/{workspace_id}").status_code == 204
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["workspaceId"] is None
