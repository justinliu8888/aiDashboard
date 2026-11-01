"""工单 #63：方案版本记录的真实链路（走聊天接口）。

这一组用例走**真实的**聊天路由、真实的编排、真实的领域校验与真实的
SQLite 落库，只替换外部模型调用 —— 观察的是用户能直接看见的东西：

- 用户连着提两次方案要求，重新打开会话能看到**两版**方案，第一版正文
  没被改写，最新的一版在最后；
- 澄清那一轮**不产生版本**；
- 生成失败那一轮**不产生版本**；
- 同一条消息重复送达**不产生第二版**。
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.plan_version_service import PlanVersionService
from app.services.report_design_service import report_design_service
from app.services.session_service import session_service
from app.storage.sqlite_store import SqliteSessionRepository


# --------------------------------------------------------------------------
# 外部模型调用替身：只按脚本返回结果，不替服务端做校验、不落库
# --------------------------------------------------------------------------


def _candidate(sections: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "plan_candidate",
        "candidate": {
            "topic": "PUE 专题报表",
            "requirements": {
                "metrics": ["PUE"],
                "subject": None,
                "timeRange": None,
                "exclusions": [],
            },
            "sections": sections,
            "assumptions": ["PUE 统计口径待核实，数据待连接"],
            "suggestions": [],
            "referenceTemplates": [],
        },
    }


FIRST_SECTIONS = [
    {
        "id": "section_pue",
        "title": "PUE 总览",
        "metricIntents": ["PUE"],
        "componentType": "metric_card",
    }
]

SECOND_SECTIONS = [
    *FIRST_SECTIONS,
    {
        "id": "section_trend",
        "title": "PUE 趋势",
        "metricIntents": ["PUE"],
        "componentType": "line_chart",
    },
]


class ScriptedModel:
    """按调用顺序返回脚本里的结果；用完就返回最后一条。"""

    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls = 0

    async def __call__(self, messages: list[dict[str, Any]]) -> str:
        index = min(self.calls, len(self.results) - 1)
        self.calls += 1
        result = self.results[index]
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False)


@pytest.fixture(autouse=True)
def design_deps(monkeypatch, tmp_path):
    """每个用例一份干净的库。"""
    repo = SqliteSessionRepository(tmp_path / "sessions.db")
    monkeypatch.setattr(session_service, "_repo", repo)
    report_design_service.reset_for_tests()
    yield
    report_design_service.reset_for_tests()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _install(model: ScriptedModel) -> ScriptedModel:
    report_design_service.reset_for_tests(model_call=model)
    return model


def _post(client: TestClient, message: str, message_id: str, session_id: str):
    body = {
        "agentProtocolVersion": "1.0",
        "conversationId": "conv_versions",
        "sessionId": session_id,
        "messageId": message_id,
        "state": "ANALYZING",
        "message": message,
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    }
    return client.post("/api/agent/chat", json=body)


# --------------------------------------------------------------------------
# 1. 连着提交两次 → 两版
# --------------------------------------------------------------------------


def test_two_submissions_leave_two_versions(client):
    """用户提两次方案要求：会话里有两版，旧版正文原样保留。"""
    _install(ScriptedModel(_candidate(FIRST_SECTIONS), _candidate(SECOND_SECTIONS)))

    first = _post(client, "生成 PUE 报表", "msg_1", "sess_versions").json()
    second = _post(client, "再加一块 PUE 趋势", "msg_2", "sess_versions").json()

    assert first["domainObjects"]["reportPlan"]["revision"] == 1
    assert second["domainObjects"]["reportPlan"]["revision"] == 2

    versions = PlanVersionService().list_versions("sess_versions")
    assert len(versions) == 2
    assert [item["planRevision"] for item in versions] == [1, 2]
    # 第一版正文只有一块板块，第二版多一块 —— 旧版没被改写
    assert len(versions[0]["reportPlan"]["sections"]) == 1
    assert len(versions[1]["reportPlan"]["sections"]) == 2
    assert versions[0]["reportPlan"]["sections"][0]["title"] == "PUE 总览"


def test_reopening_session_restores_versions_in_save_order(client):
    """重新打开会话：历史按真实保存顺序，最新的一版是当前方案。"""
    _install(ScriptedModel(_candidate(FIRST_SECTIONS), _candidate(SECOND_SECTIONS)))

    _post(client, "生成 PUE 报表", "msg_1", "sess_versions")
    _post(client, "再加一块 PUE 趋势", "msg_2", "sess_versions")

    detail = client.get("/api/sessions/sess_versions").json()
    # 重新打开会话看到的当前方案是最新一版
    assert detail["snapshot"]["reportPlan"]["revision"] == 2

    versions = PlanVersionService().list_versions("sess_versions")
    assert [item["planRevision"] for item in versions] == [1, 2]
    assert versions[-1]["isCurrent"] is True
    assert versions[0]["isCurrent"] is False


# --------------------------------------------------------------------------
# 2. 澄清与失败不产生版本
# --------------------------------------------------------------------------


def test_clarification_round_does_not_create_version(client):
    """需要澄清的那一轮：不产生版本，当前方案原样保留。"""
    _install(
        ScriptedModel(
            _candidate(FIRST_SECTIONS),
            {"kind": "clarification", "question": "PUE 按 IT 设备口径还是全站口径？"},
        )
    )

    _post(client, "生成 PUE 报表", "msg_1", "sess_versions")
    payload = _post(client, "口径换成 IT 设备口径", "msg_2", "sess_versions").json()

    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert payload["domainObjects"]["reportPlan"]["revision"] == 1

    versions = PlanVersionService().list_versions("sess_versions")
    assert len(versions) == 1
    assert versions[0]["planRevision"] == 1


def test_failed_generation_does_not_create_version(client):
    """生成失败的那一轮：不产生版本。"""
    _install(ScriptedModel(_candidate(FIRST_SECTIONS), "模型这次没有给出可解析的内容"))

    _post(client, "生成 PUE 报表", "msg_1", "sess_versions")
    payload = _post(client, "再加一块趋势", "msg_2", "sess_versions").json()

    assert payload.get("error") is not None
    assert payload["domainObjects"]["reportPlan"]["revision"] == 1

    versions = PlanVersionService().list_versions("sess_versions")
    assert len(versions) == 1


def test_failed_first_generation_leaves_no_version(client):
    """第一次就失败：版本序列是空的。"""
    _install(ScriptedModel("模型这次没有给出可解析的内容"))

    payload = _post(client, "生成 PUE 报表", "msg_1", "sess_versions").json()

    assert payload.get("error") is not None
    assert PlanVersionService().list_versions("sess_versions") == []


# --------------------------------------------------------------------------
# 3. 重复送达 / 乱序：不重复追加
# --------------------------------------------------------------------------


def test_same_message_sent_twice_keeps_one_version(client):
    """同一条消息重复送达：还是只有一版。"""
    _install(ScriptedModel(_candidate(FIRST_SECTIONS)))

    _post(client, "生成 PUE 报表", "msg_1", "sess_versions")
    _post(client, "生成 PUE 报表", "msg_1", "sess_versions")

    versions = PlanVersionService().list_versions("sess_versions")
    assert len(versions) == 1
    assert versions[0]["planRevision"] == 1


def test_version_records_carry_operation_id(client):
    """每一版都记着是哪次操作提交上来的。"""
    _install(ScriptedModel(_candidate(FIRST_SECTIONS), _candidate(SECOND_SECTIONS)))

    _post(client, "生成 PUE 报表", "msg_1", "sess_versions")
    _post(client, "再加一块 PUE 趋势", "msg_2", "sess_versions")

    versions = PlanVersionService().list_versions("sess_versions")
    assert all(item["operationId"] for item in versions)
    assert len({item["operationId"] for item in versions}) == 2
