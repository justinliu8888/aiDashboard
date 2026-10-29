"""工单 02：确认方案后生成模板（真实链路）。

用户口径（需求访谈 Q13）：「数据库里有啥数据就展示啥，没有的
就按 Q11 和 Q12 的方案来」—— 所以：

    - 确认后**必须**出模板，不能把流程断在「还没接入」；
    - 当前能力算得出数的区域填真实数据；
    - 算不出数的区域保留占位，并写明原因（数据待连接 / 口径待核实）；
    - 占位区域**不填任何数值** —— 不许拿假数糊弄。

测试边界与 #19 集成测试一致：走真实 JSON 聊天路由、真实编排、
真实 SQLite 落库与恢复，只替换外部模型调用。
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.report_design_service import report_design_service
from app.services.session_service import session_service
from app.storage.sqlite_store import SqliteSessionRepository


# --------------------------------------------------------------------------
# 外部模型调用替身
# --------------------------------------------------------------------------


class RecordingModel:
    """记录收到的消息，按脚本返回结果。"""

    def __init__(self, *results: dict[str, Any]) -> None:
        self.results = list(results)
        self.calls: list[list[dict[str, Any]]] = []

    async def __call__(self, messages: list[dict[str, Any]]) -> str:
        self.calls.append(messages)
        if not self.results:
            return json.dumps(_candidate(), ensure_ascii=False)
        result = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        return json.dumps(result, ensure_ascii=False)


def _candidate(
    *,
    topic: str = "A 机房能耗日报",
    metrics: list[str] | None = None,
    subject: str | None = "A 机房",
    time_range: str | None = "最近 7 天",
    sections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "kind": "plan_candidate",
        "candidate": {
            "topic": topic,
            "requirements": {
                "metrics": metrics if metrics is not None else ["总耗电"],
                "subject": subject,
                "timeRange": time_range,
                "exclusions": [],
            },
            "sections": sections
            if sections is not None
            else [
                {
                    "id": "section_total",
                    "title": "总耗电概览",
                    "metricIntents": ["总耗电"],
                    "componentType": "metric_card",
                }
            ],
            "assumptions": [],
            "suggestions": [],
            "referenceTemplates": [],
        },
    }


def _pue_candidate() -> dict[str, Any]:
    return _candidate(
        topic="PUE 专题报表",
        metrics=["PUE"],
        sections=[
            {
                "id": "section_pue",
                "title": "PUE 总览",
                "metricIntents": ["PUE"],
                "componentType": "metric_card",
            }
        ],
    )


@pytest.fixture(autouse=True)
def design_deps(monkeypatch, tmp_path):
    """每个用例一份干净的库。"""
    repo = SqliteSessionRepository(tmp_path / "sessions.db")
    monkeypatch.setattr(session_service, "_repo", repo)
    monkeypatch.setattr(
        report_design_service, "_injected_repository", None, raising=False
    )
    report_design_service.reset_for_tests()
    yield
    report_design_service.reset_for_tests()


def install_model(monkeypatch, model: RecordingModel) -> RecordingModel:
    report_design_service.reset_for_tests(model_call=model)
    return model


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _body(
    message: str,
    *,
    session_id: str = "sess_tpl_1",
    message_id: str = "msg_1",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "agentProtocolVersion": "1.0",
        "conversationId": "conv_tpl",
        "sessionId": session_id,
        "messageId": message_id,
        "state": "ANALYZING",
        "message": message,
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
            **(context or {}),
        },
    }


def _post(client: TestClient, message: str, **kwargs: Any):
    return client.post("/api/agent/chat", json=_body(message, **kwargs))


def _confirm(client: TestClient, plan: dict[str, Any], message_id: str) -> dict[str, Any]:
    payload = _post(
        client,
        "确认",
        message_id=message_id,
        context={
            "designAction": "CONFIRM_PLAN",
            "planId": plan["id"],
            "basePlanRevision": plan["revision"],
        },
    ).json()
    return payload


# --------------------------------------------------------------------------
# 1. 确认 → 出模板
# --------------------------------------------------------------------------


def test_daily_energy_wording_binds_available_trend_data(client, monkeypatch):
    candidate = _candidate(metrics=["每日耗电量"], sections=[{
        "id": "section_daily", "title": "每日耗电趋势", "metricIntents": ["每日耗电量"],
        "componentType": "line_chart",
    }])
    install_model(monkeypatch, RecordingModel(candidate))
    plan = _post(client, "生成最近7天每日耗电趋势").json()["domainObjects"]["reportPlan"]
    template = _confirm(client, plan, "msg_confirm_daily")["domainObjects"]["templateDesign"]
    region = template["regions"][0]
    assert region["dataStatus"] == "bound"
    assert len(region["data"]["yValues"]) == 7


def test_confirm_generates_template_bound_to_the_confirmed_plan(client, monkeypatch):
    """确认后阶段推进到 TEMPLATE_READY，模板绑定的是确认的那一版方案。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    built = _post(client, "做一份 A 机房最近 7 天的能耗报表").json()
    plan = built["domainObjects"]["reportPlan"]

    payload = _confirm(client, plan, "msg_2")

    assert payload["designPhase"] == "TEMPLATE_READY"
    assert payload["error"] is None

    template = payload["domainObjects"]["templateDesign"]
    assert template["id"]
    assert template["planId"] == plan["id"]
    assert template["planRevision"] == plan["revision"]
    assert template["title"] == plan["topic"]

    # 区域与方案板块一一对应（靠 sectionId，不是只对标题）
    plan_sections = {section["id"] for section in plan["sections"]}
    region_sections = {region["sectionId"] for region in template["regions"]}
    assert region_sections == plan_sections

    assert "template_ready" in payload["uiActions"]


def test_confirm_does_not_fall_through_to_the_old_report_flow(client, monkeypatch):
    """确认方案仍然**不**接旧的保存 / 出数链路。

    生成模板是新建流程自己的事：不能顺手把旧草稿存了、预览跑了。
    """
    install_model(monkeypatch, RecordingModel(_candidate()))

    built = _post(client, "做一份 A 机房最近 7 天的能耗报表").json()
    plan = built["domainObjects"]["reportPlan"]

    payload = _confirm(client, plan, "msg_2")

    assert payload["toolRequests"] == []
    assert payload["domainObjects"].get("reportDefinitionDraft") is None
    assert payload["domainObjects"].get("reportRunPreview") is None


# --------------------------------------------------------------------------
# 2. 有数接数、没数占位
# --------------------------------------------------------------------------


def test_available_metric_region_gets_real_data(client, monkeypatch):
    """当前能力算得出数的区域要接上数据，不是占位。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    built = _post(client, "做一份 A 机房最近 7 天的能耗报表").json()
    plan = built["domainObjects"]["reportPlan"]

    template = _confirm(client, plan, "msg_2")["domainObjects"]["templateDesign"]

    region = template["regions"][0]
    assert region["dataStatus"] == "bound"
    assert region["placeholderReason"] is None
    # 真的取到了数：不是 None、不是空串、不是 0 冒充
    assert isinstance(region["data"]["value"], (int, float))
    assert region["data"]["value"] > 0
    assert region["data"]["unit"] == "kWh"
    # 数据来源必须标明性质，不能让人把演示数值当成生产数据
    assert region["dataSourceLabel"]
    assert template["dataStatus"] == "bound"


def test_pue_region_stays_placeholder_and_says_why(client, monkeypatch):
    """算不出数的区域保留占位，并写明原因。"""
    install_model(monkeypatch, RecordingModel(_pue_candidate()))

    built = _post(client, "生成 PUE 报表").json()
    plan = built["domainObjects"]["reportPlan"]

    payload = _confirm(client, plan, "msg_2")
    template = payload["domainObjects"]["templateDesign"]

    region = template["regions"][0]
    assert region["dataStatus"] == "placeholder"
    assert region["data"] is None
    assert "口径待核实" in region["placeholderReason"]
    assert template["dataStatus"] == "placeholder"
    assert template["pendingNotes"]
    # 占位原因要对用户说清楚
    assert "口径待核实" in payload["reply"]


def test_placeholder_region_never_carries_fake_numbers(client, monkeypatch):
    """占位区域不能带任何数值 —— 规格里的硬要求。"""
    install_model(monkeypatch, RecordingModel(_pue_candidate()))

    built = _post(client, "生成 PUE 报表").json()
    plan = built["domainObjects"]["reportPlan"]

    template = _confirm(client, plan, "msg_2")["domainObjects"]["templateDesign"]

    for region in template["regions"]:
        if region["dataStatus"] == "placeholder":
            assert region["data"] is None
            assert region["dataSource"] is None


def test_mixed_report_keeps_both_bound_and_placeholder_regions(client, monkeypatch):
    """混合场景：有数据的区域接数，没数据的区域占位，两边都在。"""
    install_model(
        monkeypatch,
        RecordingModel(
            _candidate(
                topic="能耗与 PUE 专题",
                metrics=["总耗电", "PUE"],
                sections=[
                    {
                        "id": "section_total",
                        "title": "总耗电概览",
                        "metricIntents": ["总耗电"],
                        "componentType": "metric_card",
                    },
                    {
                        "id": "section_pue",
                        "title": "PUE 总览",
                        "metricIntents": ["PUE"],
                        "componentType": "metric_card",
                    },
                ],
            )
        ),
    )

    built = _post(client, "做一份能耗和 PUE 的报表").json()
    plan = built["domainObjects"]["reportPlan"]

    template = _confirm(client, plan, "msg_2")["domainObjects"]["templateDesign"]

    # 板块 id 由领域层统一分配，这里按标题找回真实 id。
    by_title = {section["title"]: section["id"] for section in plan["sections"]}
    by_section = {region["sectionId"]: region for region in template["regions"]}
    assert by_section[by_title["总耗电概览"]]["dataStatus"] == "bound"
    assert by_section[by_title["PUE 总览"]]["dataStatus"] == "placeholder"
    assert template["dataStatus"] == "partial"


# --------------------------------------------------------------------------
# 3. 重复确认不重复生成
# --------------------------------------------------------------------------


def test_repeated_confirm_with_new_message_returns_same_template(client, monkeypatch):
    """换个消息再点一次确认：不重新生成，返回同一个模板。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    built = _post(client, "做一份 A 机房最近 7 天的能耗报表").json()
    plan = built["domainObjects"]["reportPlan"]

    first = _confirm(client, plan, "msg_2")["domainObjects"]["templateDesign"]
    second = _confirm(client, plan, "msg_3")["domainObjects"]["templateDesign"]

    assert second["id"] == first["id"]
    assert second["planRevision"] == first["planRevision"]


def test_repeated_confirm_with_same_message_returns_same_template(client, monkeypatch):
    """同一条消息重发（网络重试）：返回同一个模板。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    built = _post(client, "做一份 A 机房最近 7 天的能耗报表").json()
    plan = built["domainObjects"]["reportPlan"]

    first = _confirm(client, plan, "msg_2")["domainObjects"]["templateDesign"]
    second = _confirm(client, plan, "msg_2")["domainObjects"]["templateDesign"]

    assert second["id"] == first["id"]


def test_confirm_after_plan_revision_rejects_stale_version(client, monkeypatch):
    """基于过期版本的确认要可辨识地拒绝，不静默覆盖。"""
    install_model(monkeypatch, RecordingModel(_candidate(), _candidate()))

    built = _post(client, "做一份 A 机房最近 7 天的能耗报表").json()
    plan = built["domainObjects"]["reportPlan"]
    _post(client, "再加一个板块", message_id="msg_2")

    payload = _confirm(client, plan, "msg_3")

    assert payload["error"]["code"] == "PLAN_REVISION_CONFLICT"
    assert payload["designPhase"] != "TEMPLATE_READY"


# --------------------------------------------------------------------------
# 4. 刷新 / 恢复
# --------------------------------------------------------------------------


def test_session_detail_restores_the_template(client, monkeypatch):
    """刷新后能从会话详情拿回同一份模板，不用重新确认。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    built = _post(client, "做一份 A 机房最近 7 天的能耗报表").json()
    plan = built["domainObjects"]["reportPlan"]
    template = _confirm(client, plan, "msg_2")["domainObjects"]["templateDesign"]

    detail = client.get("/api/sessions/sess_tpl_1").json()

    assert detail["snapshot"]["designPhase"] == "TEMPLATE_READY"
    restored = detail["snapshot"]["templateDesign"]
    assert restored["id"] == template["id"]
    assert restored["regions"]


def test_template_is_persisted_in_the_design_store(client, monkeypatch):
    """模板要落进设计存储 —— 刷新与服务重启都靠它。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    built = _post(client, "做一份 A 机房最近 7 天的能耗报表").json()
    plan = built["domainObjects"]["reportPlan"]
    template = _confirm(client, plan, "msg_2")["domainObjects"]["templateDesign"]

    stored = report_design_service.repository.load_template("sess_tpl_1")
    assert stored is not None
    assert stored["id"] == template["id"]
    assert stored["planRevision"] == plan["revision"]
