"""pytest 共享夹具。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.fake_report_agent import FakeReportAgent
from app.main import app
from app.services.agent_service import agent_service
from app.services.context_engine import context_engine
from app.services.session_service import session_service
from app.storage.sqlite_store import SqliteSessionRepository


@pytest.fixture(autouse=True)
def disable_context_model(monkeypatch, tmp_path):
    """测试只验证本地状态机和规则，不访问外部模型。"""
    monkeypatch.setattr(context_engine, "_client", None)
    monkeypatch.setattr(agent_service, "_agent", FakeReportAgent())
    # 每个测试用独立的 SQLite 文件，避免用例之间串数据
    repo = SqliteSessionRepository(tmp_path / "sessions.db")
    monkeypatch.setattr(session_service, "_repo", repo)


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
    def _post(
        message: str,
        state: str = "ANALYZING",
        snapshots: dict | None = None,
        **context_override,
    ):
        ctx = {**base_context, **context_override}
        body = _build_request(message, state, ctx)
        if snapshots is not None:
            body["snapshots"] = snapshots
        return client.post("/api/agent/chat", json=body)

    return _post
