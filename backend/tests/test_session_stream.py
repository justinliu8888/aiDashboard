"""流式入口（/api/agent/chat/stream）的落库行为。

前端只走流式，所以流式路径的落库必须和 /api/agent/chat 一致：
成对写、跳过 __advance__ 的 user 消息、messageId 幂等。
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _post_stream(client: TestClient, message: str, state: str = "ANALYZING", **extra):
    body = {
        "agentProtocolVersion": "1.0",
        "conversationId": "conv_stream",
        "state": state,
        "message": message,
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    }
    body.update(extra)
    return client.post("/api/agent/chat/stream", json=body)


def test_stream_chat_persists_user_and_assistant_messages(client: TestClient):
    session_id = client.post("/api/sessions", json={}).json()["sessionId"]
    _post_stream(client, "生成日报", sessionId=session_id)

    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["messageCount"] == 2
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["content"] == "生成日报"
    assert detail["title"] == "生成日报"


def test_stream_chat_appears_in_session_list(client: TestClient):
    session_id = client.post("/api/sessions", json={}).json()["sessionId"]
    _post_stream(client, "生成日报", sessionId=session_id)

    body = client.get("/api/sessions").json()
    assert body["total"] == 1
    assert body["items"][0]["sessionId"] == session_id


def test_stream_advance_is_not_stored_as_user_message(client: TestClient):
    session_id = client.post("/api/sessions", json={}).json()["sessionId"]
    _post_stream(client, "生成日报", sessionId=session_id, state="ANALYZING")
    _post_stream(
        client,
        "__advance__",
        sessionId=session_id,
        state="SAVE_DEFINITION_DRAFT",
    )

    detail = client.get(f"/api/sessions/{session_id}").json()
    user_messages = [m for m in detail["messages"] if m["role"] == "user"]
    assert [m["content"] for m in user_messages] == ["生成日报"]


def test_stream_replayed_message_with_same_id_does_not_duplicate(client: TestClient):
    session_id = client.post("/api/sessions", json={}).json()["sessionId"]
    _post_stream(client, "生成日报", sessionId=session_id, messageId="m1")
    _post_stream(client, "生成日报", sessionId=session_id, messageId="m1")

    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["messageCount"] == 2


def test_stream_chat_stores_selection_lock(client: TestClient):
    """流式路径同样要落选择锁，恢复会话时才有得放行。"""
    session_id = client.post("/api/sessions", json={}).json()["sessionId"]
    _post_stream(client, "生成 A 机房昨日能耗日报", sessionId=session_id)

    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["pendingChoice"] is not None
    assert detail["pendingChoice"]["question"]["id"]
