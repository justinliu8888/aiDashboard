"""文字阶段 → 模板定义 → 可见草稿：跨阶段一致性与快照恢复的针对性用例。

这几条锁的是本次改动���的三件事：

    1. **跨阶段一致**：目录外 / 真实取数还接不上的指标，在**文字阶段**
       一样能走通到模板定义与可见草稿，并且屏上有数；区域说明与
       **编辑入口**同一套说法（不把「真实取数的限制」说成「这一块没数」）。
    2. **快照恢复**：方案改版、重新确认后，库里落的是**刚确认的那份草稿**，
       不是上一版方案的旧草稿；恢复出来的载荷与草稿同源。
    3. **追问卡片**：文字阶段的追问带具体选项 + 「其他」。

测试边界与 #19 集成测试一致：走真实 JSON 聊天路由、真实编排、
真实 SQLite 落库与恢复，只替换外部模型调用。
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.agent import AgentResponse, AgentResponseDomainObjects, AgentState
from app.schemas.report_definition import ReportDefinitionDraft
from app.services.report_design_service import (
    _clarification_question,
    report_design_service,
)
from app.services.session_service import session_service
from app.storage.sqlite_store import SqliteSessionRepository


# --------------------------------------------------------------------------
# 外部模型调用替身
# --------------------------------------------------------------------------


class RecordingModel:
    """按脚本返回候选（依次返回，最后一个重复用）。"""

    def __init__(self, *results: dict[str, Any]) -> None:
        self.results = list(results)
        self.calls = 0

    async def __call__(self, messages: list[dict[str, Any]]) -> str:
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return json.dumps(result, ensure_ascii=False)


def _candidate(
    *,
    topic: str = "机房日报",
    metrics: list[str] | None = None,
    sections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "kind": "plan_candidate",
        "candidate": {
            "topic": topic,
            "requirements": {
                "metrics": metrics if metrics is not None else ["总耗电量"],
                "subject": "room_a",
                "timeRange": "最近 7 天",
                "exclusions": [],
            },
            "sections": sections
            if sections is not None
            else [
                {
                    "id": "section_total",
                    "title": "总耗电趋势",
                    "metricIntents": ["总耗电量"],
                    "componentType": "line_chart",
                }
            ],
            "assumptions": [],
            "suggestions": [],
            "referenceTemplates": [],
        },
    }


#: 目录外的工厂指标（任何新东西都不该被挡下）。
_OUT_OF_CATALOG = _candidate(
    topic="机房日报",
    metrics=["总耗电量", "冷媒回风温度"],
    sections=[
        {
            "id": "section_total",
            "title": "总耗电趋势",
            "metricIntents": ["总耗电量"],
            "componentType": "line_chart",
        },
        {
            "id": "section_cold",
            "title": "冷媒温度",
            "metricIntents": ["冷媒回风温度"],
            "componentType": "table",
            "columns": [{"label": "回风温度"}],
        },
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


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _body(
    message: str,
    *,
    session_id: str = "sess_text_1",
    message_id: str = "msg_1",
    state: str = "ANALYZING",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "agentProtocolVersion": "1.0",
        "conversationId": "conv_text",
        "sessionId": session_id,
        "messageId": message_id,
        "state": state,
        "message": message,
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
            **(context or {}),
        },
    }


def _post(client: TestClient, message: str, **kwargs: Any):
    return client.post("/api/agent/chat", json=_body(message, **kwargs)).json()


def _confirm(client: TestClient, plan: dict[str, Any], message_id: str) -> dict[str, Any]:
    return _post(
        client,
        "确认",
        message_id=message_id,
        context={
            "designAction": "CONFIRM_PLAN",
            "planId": plan["id"],
            "basePlanRevision": plan["revision"],
        },
    )


def _install(monkeypatch, *results: dict[str, Any]) -> RecordingModel:
    model = RecordingModel(*results)
    report_design_service.reset_for_tests(model_call=model)
    return model


# --------------------------------------------------------------------------
# 1. 跨阶段一致：目录外指标不因为「不在目录里」被挡下
# --------------------------------------------------------------------------


def test_out_of_catalog_metric_reaches_visible_draft_with_values(client, monkeypatch):
    """目录外的指标名 → 方案 → 模板定义 → 可见草稿，且屏上有数。"""
    _install(monkeypatch, _OUT_OF_CATALOG)
    plan = _post(client, "生成机房日报")["domainObjects"]["reportPlan"]
    payload = _confirm(client, plan, "msg_confirm")

    assert payload["designPhase"] == "DRAFT_READY"
    draft = payload["domainObjects"]["reportDefinitionDraft"]
    names = [region["name"] for region in draft["regions"]]
    assert "冷媒温度" in names, "目录外指标的那块被丢了"

    # 载荷里有这一块，而且**有展示值**（不是只显示名称）。
    regions = {
        region["regionId"]: region for region in payload["reportPreviewPayload"]["regions"]
    }
    cold = regions["region_2"]
    assert cold["name"] == "冷媒温度"
    rows = (cold["payload"] or {}).get("rows") or []
    assert rows, "目录外指标那一块没有展示值"


def test_unbacked_metric_note_does_not_contradict_the_numbers_on_screen(client, monkeypatch):
    """登记在册但真实取数还接不上的指标：说明不与屏上的数字打架。"""
    candidate = _candidate(
        topic="PUE 专题",
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
    _install(monkeypatch, candidate)
    plan = _post(client, "生成 PUE 报表")["domainObjects"]["reportPlan"]
    payload = _confirm(client, plan, "msg_confirm_pue")

    region = payload["domainObjects"]["reportDefinitionDraft"]["regions"][0]
    reason = (region.get("templateBinding") or {}).get("placeholderReason") or ""
    # 「口径待核实」是给用户看的标注，保留。
    assert "口径待核实" in reason
    # 但不能说「算不出数值」—— 屏上正有数字；也不许出现字段名。
    assert "算不出数值" not in reason
    assert "fieldRef" not in reason and "pue 字段" not in reason
    # 说的是这一块的数值从哪来：与目录外那一套说法一致。
    assert "出数" in reason
    # 屏上确实有数字 —— 说明里的那句话说的是真的。
    region_payload = payload["reportPreviewPayload"]["regions"][0]["payload"]
    assert region_payload.get("value") is not None


def test_one_unknown_intent_does_not_swallow_the_registered_one(client, monkeypatch):
    """一块板块有多条指标意图时：一条没命中不许把整块都拖进兜底。"""
    from app.services.report_template_service import _match_metric

    ref, metric, _ = _match_metric(["完全没听过的一个指标", "总耗电量"])
    assert ref == "metric.machine_room.total_energy"
    assert metric is not None


# --------------------------------------------------------------------------
# 2. 快照恢复：落库的是刚确认的那一份
# --------------------------------------------------------------------------


def _draft(revision: int, name: str, plan_revision: int) -> ReportDefinitionDraft:
    return ReportDefinitionDraft.model_validate(
        {
            "metadata": {
                "name": name,
                "revision": revision,
                "status": "DRAFT",
                "templateBinding": {
                    "templateId": f"rtpl_{plan_revision}",
                    "templateRevision": 1,
                    "planId": "rplan_x",
                    "planRevision": plan_revision,
                    "definitionId": "rplan_x",
                    "definitionVersion": plan_revision,
                },
            },
            "regions": [],
        }
    )


def _commit_turn(draft: ReportDefinitionDraft) -> None:
    response = AgentResponse(
        conversationId="conv_text",
        state=AgentState.WAITING_USER_CONFIRM,
        reply="ok",
        domainObjects=AgentResponseDomainObjects(reportDefinitionDraft=draft),
    )
    session_service.record_turn(
        session_id="sess_text_1", request=None, response=response, user_text="hi"
    )


def test_new_plan_version_replaces_the_stored_draft(client, monkeypatch):
    """方案改版、重新确认后：库里落的是**新草稿**，不是上一版方案的旧稿。

    改一版方案就是**一份新草稿**（草稿版本号从 0 重新起算）。拿草稿
    版本号去比新旧，会把这份新草稿当成「旧稿」挡掉 —— 用户刷新页面
    看到的就是上一版的草稿，而不是刚确认的那一份。
    """
    _install(monkeypatch, _OUT_OF_CATALOG)
    # 上一版方案的草稿（已经编辑过一轮，版本号 1）。
    _commit_turn(_draft(1, "上一版方案的草稿", 1))
    # 方案改版后重新确认 → 全新的一份草稿（版本号重新从 0 起算）。
    _commit_turn(_draft(0, "刚确认的新草稿", 2))

    stored = session_service.repository.get_session("sess_text_1").snapshot["definition"]
    assert stored["metadata"]["name"] == "刚确认的新草稿"
    assert stored["metadata"]["templateBinding"]["planRevision"] == 2


def test_late_reply_still_cannot_roll_back_the_applied_draft(client, monkeypatch):
    """同一份定义版本里：迟到的旧回复仍然不许把新草稿回滚。"""
    _install(monkeypatch, _OUT_OF_CATALOG)
    _commit_turn(_draft(2, "已应用的新草稿", 1))
    _commit_turn(_draft(1, "迟到的旧草稿", 1))

    stored = session_service.repository.get_session("sess_text_1").snapshot["definition"]
    assert stored["metadata"]["name"] == "已应用的新草稿"


def test_restored_payload_matches_the_stored_draft(client, monkeypatch):
    """恢复出来的载荷与库里那份草稿**同源**（同一批区域）。"""
    _install(monkeypatch, _OUT_OF_CATALOG)
    plan = _post(client, "生成机房日报")["domainObjects"]["reportPlan"]
    confirmed = _confirm(client, plan, "msg_confirm")

    detail = client.get("/api/sessions/sess_text_1").json()
    stored = detail["snapshot"]["definition"]
    restored = detail["snapshot"]["reportPreviewPayload"]

    assert [r["id"] for r in stored["regions"]] == [
        r["regionId"] for r in restored["regions"]
    ]
    # 这一轮返回的载荷与恢复出来的载荷逐字段一致。
    assert confirmed["reportPreviewPayload"] == restored


# --------------------------------------------------------------------------
# 3. 追问卡片：具体选项 + 「其他」
# --------------------------------------------------------------------------


def test_clarification_card_carries_concrete_options_and_other():
    card = _clarification_question(
        "你说的「改成柱状图」是指哪一块板块？当前方案的板块有：总耗电趋势、冷媒温度。",
        options=["总耗电趋势", "冷媒温度"],
    )
    dumped = card.model_dump(mode="json")
    assert dumped["options"] == [
        {"value": "总耗电趋势", "label": "总耗电趋势"},
        {"value": "冷媒温度", "label": "冷媒温度"},
    ]
    assert dumped["allowsOther"] is True
    assert dumped["otherLabel"] == "其他"
    assert dumped["otherPlaceholder"]
    # 追问允许自由作答：输入框不被锁住。
    assert dumped["lockScope"] == "menu_only"


def test_clarification_options_are_user_words_not_internal_values():
    """选项是**用户看得懂**的东西：板块标题 / 指标意图，不是内部编号。"""
    from app.services.report_plan_service import resolve_plan

    resolution = resolve_plan(
        [
            {
                "title": "总耗电趋势",
                "metricIntents": ["总耗电量"],
                "componentType": "line_chart",
            },
            {
                "title": "冷媒温度",
                "metricIntents": ["冷媒回风温度"],
                "componentType": "table",
            },
        ],
        request_text="改成柱状图",
    )
    if resolution.question:
        assert all(
            option and "section_" not in option and "metric." not in option
            for option in resolution.options
        )
