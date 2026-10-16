"""pytest 共享夹具。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def base_context() -> dict:
    return {
        "projectId": "project_001",
        "subjectRef": "machine_room",
        "subjectId": "room_a",
    }


def _build_request(message: str, state: str, context: dict) -> dict:
    return {
        "agentProtocolVersion": "1.0",
        "conversationId": "conv_test",
        "state": state,
        "message": message,
        "context": context,
    }


@pytest.fixture()
def post_chat(client: TestClient, base_context: dict):
    def _post(message: str, state: str = "ANALYZING", **context_override):
        ctx = {**base_context, **context_override}
        body = _build_request(message, state, ctx)
        return client.post("/api/agent/chat", json=body)

    return _post
