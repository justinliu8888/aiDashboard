"""#19 首波集成测试：文字方案与待确认闭环的真实链路。

测试边界（规格 #18「已确认的测试边界」+ 冻结契约）：

    - 走**真实的** JSON / SSE 聊天路由、真实的编排、真实的领域校验、
      真实的 SQLite 落库与恢复；
    - **只**替换外部模型调用（``model_call``）。不替换整套 Agent，
      不注入最终快照，不预写方案。

这些用例观察的都是公开行为：接口返回什么、会话详情恢复出什么、
库里留下了什么。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.context_engine import context_engine
from app.services.report_design_service import (
    is_pure_confirmation,
    is_vague_approval,
    report_design_service,
)
from app.services.session_service import session_service
from app.storage.report_design_repository import (
    OPERATION_FAILED,
    OPERATION_SUCCEEDED,
)
from app.storage.sqlite_store import SqliteSessionRepository


# --------------------------------------------------------------------------
# 外部模型调用替身
#
# 只替换 model_call：这是冻结契约里唯一的外部边界。
# 它按消息里的用户需求返回一份**合法候选**，不替服务端做领域校验、
# 不替服务端分配版本、不落库。
# --------------------------------------------------------------------------


def _candidate(
    *,
    topic: str = "PUE 专题报表",
    metrics: list[str] | None = None,
    subject: str | None = None,
    time_range: str | None = None,
    exclusions: list[str] | None = None,
    sections: list[dict[str, Any]] | None = None,
    assumptions: list[str] | None = None,
    suggestions: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "kind": "plan_candidate",
        "candidate": {
            "topic": topic,
            "requirements": {
                "metrics": metrics if metrics is not None else ["PUE"],
                "subject": subject,
                "timeRange": time_range,
                "exclusions": exclusions if exclusions is not None else [],
            },
            "sections": sections
            if sections is not None
            else [
                {
                    "id": "section_pue",
                    "title": "PUE 总览",
                    "metricIntents": ["PUE"],
                    "componentType": "metric_card",
                }
            ],
            "assumptions": assumptions
            if assumptions is not None
            else ["PUE 统计口径待核实，数据待连接"],
            "suggestions": suggestions if suggestions is not None else [],
            "referenceTemplates": [],
        },
    }


class RecordingModel:
    """记录收到的消息，按脚本返回结果。

    ``responder`` 可以拿到 messages 自行决定返回什么 ——
    用来模拟「模型看到当前方案后，原样带回已有板块的 ID」这一真实行为。
    """

    def __init__(
        self, *results: dict[str, Any], responder: Any | None = None
    ) -> None:
        self.results = list(results)
        self.responder = responder
        self.calls: list[list[dict[str, Any]]] = []

    async def __call__(self, messages: list[dict[str, Any]]) -> str:
        self.calls.append(messages)
        if self.responder is not None:
            return json.dumps(self.responder(messages), ensure_ascii=False)
        if not self.results:
            return json.dumps(_candidate(), ensure_ascii=False)
        result = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        return json.dumps(result, ensure_ascii=False)


def _current_plan_from(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """从发给模型的消息里取出「当前已提交方案」。"""
    for message in messages:
        content = str(message.get("content") or "")
        marker = "当前已提交方案（JSON）：\n"
        if marker in content:
            return json.loads(content.split(marker, 1)[1])
    return None


@pytest.fixture(autouse=True)
def design_deps(monkeypatch, tmp_path):
    """每个用例一份干净的库，并接上模型替身。"""
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
    session_id: str = "sess_design_1",
    message_id: str = "msg_1",
    state: str = "ANALYZING",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "agentProtocolVersion": "1.0",
        "conversationId": "conv_design",
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
    return client.post("/api/agent/chat", json=_body(message, **kwargs))


# --------------------------------------------------------------------------
# 1. 新建请求 → 文字方案 + 待确认
# --------------------------------------------------------------------------


def test_new_build_returns_plan_and_waiting_confirm(client, monkeypatch):
    """新建请求返回 ReportPlan 与 PLAN_WAITING_CONFIRM。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    response = _post(client, "直接生成 PUE 报表")
    assert response.status_code == 200
    payload = response.json()

    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"
    plan = payload["domainObjects"]["reportPlan"]
    assert plan["id"]
    assert plan["revision"] == 1
    assert plan["topic"] == "PUE 专题报表"
    assert plan["sections"][0]["id"]
    assert plan["sections"][0]["componentType"] == "metric_card"


def test_new_build_has_no_template_no_data_no_run(client, monkeypatch):
    """方案轮不返回模板、真实数值或运行预览。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    payload = _post(client, "直接生成 PUE 报表").json()

    assert payload["domainObjects"].get("reportDefinitionDraft") is None
    assert payload["domainObjects"].get("reportRunPreview") is None
    assert payload["toolRequests"] == []
    assert "plan_ready" in payload["uiActions"]
    # 没有旧流程的取数 / 保存动作
    assert "preview_framework" not in payload["uiActions"]
    assert "confirm_framework" not in payload["uiActions"]


def test_plan_is_persisted_and_recoverable(client, monkeypatch):
    """方案、版本、阶段与消息持久化后才报告成功；刷新可恢复。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    payload = _post(client, "直接生成 PUE 报表").json()
    session_id = "sess_design_1"

    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["snapshot"]["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert detail["snapshot"]["reportPlan"]["revision"] == 1
    assert detail["snapshot"]["reportPlan"]["topic"] == "PUE 专题报表"

    # 这一轮的消息真的落库了（user + assistant 成对）
    roles = [message["role"] for message in detail["messages"]]
    assert roles == ["user", "assistant"]

    # 响应与立即恢复一致
    assert payload["domainObjects"]["reportPlan"] == detail["snapshot"]["reportPlan"]


def test_explicit_requirements_are_preserved(client, monkeypatch):
    """PUE、指标、对象、时间范围与排除项保留，不被默认值悄悄替换。"""
    install_model(
        monkeypatch,
        RecordingModel(
            _candidate(
                metrics=["PUE", "总耗电"],
                subject="A 机房",
                time_range="最近 7 天",
                exclusions=["设备耗电排名"],
            )
        ),
    )

    plan = _post(client, "生成 A 机房最近 7 天 PUE 报表，不要设备耗电排名").json()[
        "domainObjects"
    ]["reportPlan"]

    assert plan["requirements"]["metrics"] == ["PUE", "总耗电"]
    assert plan["requirements"]["subject"] == "A 机房"
    assert plan["requirements"]["timeRange"] == "最近 7 天"
    assert plan["requirements"]["exclusions"] == ["设备耗电排名"]


def test_missing_catalog_does_not_block_design(client, monkeypatch):
    """目录没有指标 / 没有数据能力，不阻断合理设计。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    payload = _post(client, "生成一份 PUE 专题分析").json()

    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert payload["domainObjects"]["reportPlan"] is not None
    assert payload.get("error") is None


def test_unaccepted_suggestions_stay_out_of_sections(client, monkeypatch):
    """未接受的额外建议不进板块，单独放在 suggestions 里。"""
    install_model(
        monkeypatch,
        RecordingModel(
            _candidate(
                suggestions=["可以再加一个告警板块"],
                sections=[
                    {
                        "id": "section_pue",
                        "title": "PUE 总览",
                        "metricIntents": ["PUE"],
                        "componentType": "metric_card",
                    }
                ],
            )
        ),
    )

    plan = _post(client, "生成 PUE 报表").json()["domainObjects"]["reportPlan"]

    assert plan["suggestions"] == ["可以再加一个告警板块"]
    assert [section["title"] for section in plan["sections"]] == ["PUE 总览"]


# --------------------------------------------------------------------------
# 2. 修改 → 新 revision，继续待确认
# --------------------------------------------------------------------------


def test_modification_creates_new_revision_and_keeps_waiting(client, monkeypatch):
    """修改方案创建新 revision 并继续等待确认。

    模型这一轮看到的是**服务端**的当前方案，所以它带回来的板块 ID
    就是服务端发出去的那些。这里让替身照这个行为回传 ——
    这正是交接里点名的「修改候选保留未要求改变的板块和原 ID」。
    """

    def responder(messages: list[dict[str, Any]]) -> dict[str, Any]:
        current = _current_plan_from(messages) or {}
        sections = [
            dict(section) for section in (current.get("sections") or [])
        ]
        sections.append(
            {
                "id": "section_trend",
                "title": "PUE 趋势",
                "metricIntents": ["PUE"],
                "componentType": "line_chart",
            }
        )
        return _candidate(sections=sections)

    install_model(monkeypatch, RecordingModel(responder=responder))

    first = _post(client, "生成 PUE 报表", message_id="msg_1").json()
    original_ids = [
        section["id"] for section in first["domainObjects"]["reportPlan"]["sections"]
    ]
    payload = _post(client, "再加一个趋势板块", message_id="msg_2").json()

    plan = payload["domainObjects"]["reportPlan"]
    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert plan["revision"] == 2
    # 方案 ID 不变
    assert plan["id"] == first["domainObjects"]["reportPlan"]["id"]
    # 已有板块 ID 保留
    assert [section["id"] for section in plan["sections"]][: len(original_ids)] == (
        original_ids
    )
    assert len(plan["sections"]) == len(original_ids) + 1


def test_modification_keeps_same_plan_id(client, monkeypatch):
    """修改沿用同一份方案的 ID。"""
    install_model(monkeypatch, RecordingModel(_candidate(), _candidate()))

    first = _post(client, "生成 PUE 报表", message_id="msg_1").json()
    second = _post(client, "把标题改成 PUE 分析", message_id="msg_2").json()

    assert (
        first["domainObjects"]["reportPlan"]["id"]
        == second["domainObjects"]["reportPlan"]["id"]
    )


# --------------------------------------------------------------------------
# 3. 提问 → 不改设计版本
# --------------------------------------------------------------------------


def test_question_does_not_change_design_version(client, monkeypatch):
    """普通提问不改设计版本，也不生成模板或取数。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    _post(client, "生成 PUE 报表", message_id="msg_1")
    before = client.get("/api/sessions/sess_design_1").json()["snapshot"][
        "reportPlan"
    ]

    payload = _post(client, "PUE 是什么意思？", message_id="msg_2").json()

    assert payload["turnKind"] == "query_answer"
    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert payload["domainObjects"]["reportPlan"]["revision"] == before["revision"]
    assert payload["toolRequests"] == []

    after = client.get("/api/sessions/sess_design_1").json()["snapshot"]["reportPlan"]
    assert after["revision"] == before["revision"]


def test_question_appends_message_only(client, monkeypatch):
    """查询轮只允许追加消息，业务对象不变。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    _post(client, "生成 PUE 报表", message_id="msg_1")
    before = client.get("/api/sessions/sess_design_1").json()

    _post(client, "PUE 是什么意思？", message_id="msg_2")

    after = client.get("/api/sessions/sess_design_1").json()
    assert after["messageCount"] == before["messageCount"] + 2
    assert after["snapshot"]["reportPlan"] == before["snapshot"]["reportPlan"]


# --------------------------------------------------------------------------
# 4. 失败 → 保留上一成功方案，不报假成功
# --------------------------------------------------------------------------


def test_model_failure_keeps_previous_plan(client, monkeypatch):
    """模型失败：保留上一成功方案，不显示假成功，操作不卡在处理中。"""
    install_model(
        monkeypatch,
        RecordingModel(
            _candidate(),
            {"kind": "failure", "code": "MODEL_CALL_FAILED", "message": "模型调用失败"},
        ),
    )

    _post(client, "生成 PUE 报表", message_id="msg_1")
    payload = _post(client, "改成柱状图", message_id="msg_2").json()

    assert payload.get("error") is not None
    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert payload["domainObjects"]["reportPlan"]["revision"] == 1
    assert "plan_retry" in payload["uiActions"]

    # 操作不能停在 PROCESSING：用户要能重试
    rows = _operations()
    assert all(row["status"] != "PROCESSING" for row in rows)
    assert rows[-1]["status"] == OPERATION_FAILED


def test_commit_failure_keeps_previous_plan(client, monkeypatch, monkeypatch2=None):
    """提交失败：上一成功方案保留，操作显式失败收尾。"""
    install_model(monkeypatch, RecordingModel(_candidate(), _candidate()))

    _post(client, "生成 PUE 报表", message_id="msg_1")

    repository = report_design_service.repository
    original = repository.commit_plan

    def exploding(*args: Any, **kwargs: Any):
        raise sqlite3.OperationalError("disk is full")

    repository.commit_plan = exploding  # type: ignore[method-assign]
    try:
        payload = _post(client, "再加一个板块", message_id="msg_2").json()
    finally:
        repository.commit_plan = original  # type: ignore[method-assign]

    assert payload.get("error") is not None
    detail = client.get("/api/sessions/sess_design_1").json()
    assert detail["snapshot"]["reportPlan"]["revision"] == 1

    rows = _operations()
    assert all(row["status"] != "PROCESSING" for row in rows)


# --------------------------------------------------------------------------
# 5. 重复与恢复
# --------------------------------------------------------------------------


def test_duplicate_message_does_not_regenerate(client, monkeypatch):
    """同一条消息重发：不重复生成，也不重复写消息。"""
    model = install_model(monkeypatch, RecordingModel(_candidate()))

    first = _post(client, "生成 PUE 报表", message_id="msg_same").json()
    second = _post(client, "生成 PUE 报表", message_id="msg_same").json()

    assert len(model.calls) == 1
    assert second["domainObjects"]["reportPlan"] == first["domainObjects"]["reportPlan"]

    detail = client.get("/api/sessions/sess_design_1").json()
    roles = [message["role"] for message in detail["messages"]]
    assert roles == ["user", "assistant"]


# --------------------------------------------------------------------------
# 6. 澄清：不是技术失败，阶段不变
# --------------------------------------------------------------------------


def test_clarification_keeps_phase_and_is_not_a_failure(client, monkeypatch):
    """必要追问：阶段保持 PLAN_NEEDED，不记成技术失败。"""
    install_model(
        monkeypatch,
        RecordingModel({"kind": "clarification", "question": "这份报表主要给谁看？"}),
    )

    payload = _post(client, "生成报表").json()

    assert payload["designPhase"] == "PLAN_NEEDED"
    assert payload["state"] == "NEEDS_CLARIFICATION"
    assert payload.get("error") is None
    assert "这份报表主要给谁看？" in payload["reply"]

    rows = _operations()
    assert rows[-1]["status"] == OPERATION_SUCCEEDED


# --------------------------------------------------------------------------
# 7. 确认边界：#20 未实施，不接旧确认报表链路
# --------------------------------------------------------------------------


def test_confirm_plan_does_not_trigger_save_or_data(client, monkeypatch):
    """确认方案不接旧的保存 / 出数链路，也不伪造完成状态。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    built = _post(client, "生成 PUE 报表", message_id="msg_1").json()
    plan = built["domainObjects"]["reportPlan"]

    payload = _post(
        client,
        "确认",
        message_id="msg_2",
        context={
            "designAction": "CONFIRM_PLAN",
            "planId": plan["id"],
            "basePlanRevision": plan["revision"],
        },
    ).json()

    assert payload["toolRequests"] == []
    assert payload["domainObjects"].get("reportDefinitionDraft") is None
    assert payload["domainObjects"].get("reportRunPreview") is None
    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"
    # 如实说明未就绪，不伪造完成
    assert "还没有接入" in payload["reply"]


def test_confirm_plan_with_stale_revision_is_rejected(client, monkeypatch):
    """基于过期版本的确认要可辨识地拒绝，不静默覆盖。"""
    install_model(monkeypatch, RecordingModel(_candidate(), _candidate()))

    built = _post(client, "生成 PUE 报表", message_id="msg_1").json()
    plan = built["domainObjects"]["reportPlan"]
    _post(client, "再加一个板块", message_id="msg_2")

    payload = _post(
        client,
        "确认",
        message_id="msg_3",
        context={
            "designAction": "CONFIRM_PLAN",
            "planId": plan["id"],
            "basePlanRevision": plan["revision"],
        },
    ).json()

    assert payload["error"]["code"] == "PLAN_REVISION_CONFLICT"
    assert payload["domainObjects"]["reportPlan"]["revision"] == 2


def test_confirm_plan_for_another_plan_is_rejected(client, monkeypatch):
    """确认的是另一份方案：同样要可辨识地拒绝。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    built = _post(client, "生成 PUE 报表", message_id="msg_1").json()
    plan = built["domainObjects"]["reportPlan"]

    payload = _post(
        client,
        "确认",
        message_id="msg_2",
        context={
            "designAction": "CONFIRM_PLAN",
            "planId": "rplan_someone_else",
            "basePlanRevision": plan["revision"],
        },
    ).json()

    assert payload["error"]["code"] == "PLAN_REVISION_CONFLICT"
    assert payload["domainObjects"]["reportPlan"]["revision"] == 1


# --------------------------------------------------------------------------
# 8. 自然语言确认、模糊认可、修改与确认同条消息
#
# 规格 #18：
#   - 「自然语言确认由服务端识别，并绑定服务端当前方案版本」；
#   - 「「差不多了」等不能确定是否认可的表达不直接推进，返回明确确认入口」；
#   - 「一次消息同时提出修改和确认时，先展示更新后的方案并等待对该版本的确认」。
#
# 工单 01 验收：「不把最初需求或模糊认可当作确认」。
# --------------------------------------------------------------------------


def test_natural_language_confirm_binds_current_plan_version(client, monkeypatch):
    """自然语言「确认」由服务端识别，绑定服务端当前方案版本。

    服务端认得这句是在认可当前方案，所以：
      - 不再跑一次模型（模型调用次数不变）；
      - 版本不变，方案 id 不变；
      - 行为与按钮确认一致：如实说明模板生成尚未接入。
    """
    model = install_model(monkeypatch, RecordingModel(_candidate()))
    built = _post(client, "生成 PUE 报表", message_id="msg_1").json()
    plan = built["domainObjects"]["reportPlan"]
    calls_before = len(model.calls)

    payload = _post(client, "确认", message_id="msg_2").json()

    assert len(model.calls) == calls_before
    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"
    confirmed = payload["domainObjects"]["reportPlan"]
    assert confirmed["revision"] == plan["revision"]
    assert confirmed["id"] == plan["id"]
    assert payload["toolRequests"] == []
    assert "还没有接入" in payload["reply"]


def test_vague_approval_does_not_advance_design_version(client, monkeypatch):
    """「差不多了」不推进设计版本，返回明确确认入口。"""
    model = install_model(monkeypatch, RecordingModel(_candidate()))
    built = _post(client, "生成 PUE 报表", message_id="msg_1").json()
    plan = built["domainObjects"]["reportPlan"]
    calls_before = len(model.calls)

    payload = _post(client, "差不多了", message_id="msg_2").json()

    assert len(model.calls) == calls_before
    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert payload["domainObjects"]["reportPlan"]["revision"] == plan["revision"]
    # 明确确认入口
    assert "确认" in payload["reply"]
    assert payload["toolRequests"] == []


def test_modify_and_confirm_same_message_shows_updated_version(client, monkeypatch):
    """一条消息同时提出修改和确认：先展示更新后的方案，再等确认。

    确认动作不能把同一句里的修改要求丢掉 —— 规格写明
    「一次消息同时提出修改和确认时，先展示更新后的方案并等待对该版本的确认」。
    """

    def responder(messages: list[dict[str, Any]]) -> dict[str, Any]:
        current = _current_plan_from(messages) or {}
        sections = [dict(s) for s in (current.get("sections") or [])]
        sections.append(
            {
                "id": "section_trend",
                "title": "PUE 趋势",
                "metricIntents": ["PUE"],
                "componentType": "line_chart",
            }
        )
        return _candidate(sections=sections)

    install_model(monkeypatch, RecordingModel(responder=responder))
    built = _post(client, "生成 PUE 报表", message_id="msg_1").json()
    plan = built["domainObjects"]["reportPlan"]

    payload = _post(
        client,
        "改成柱状图，确认",
        message_id="msg_2",
        context={
            "designAction": "CONFIRM_PLAN",
            "planId": plan["id"],
            "basePlanRevision": plan["revision"],
        },
    ).json()

    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"
    updated = payload["domainObjects"]["reportPlan"]
    assert updated["revision"] == plan["revision"] + 1
    assert updated["id"] == plan["id"]
    assert len(updated["sections"]) == len(plan["sections"]) + 1


def test_confirmation_phrase_with_modification_is_not_a_confirmation(
    client, monkeypatch
):
    """「可以」后面还带着要求，就不是纯确认，要按修改处理。"""
    install_model(monkeypatch, RecordingModel(_candidate()))
    built = _post(client, "生成 PUE 报表", message_id="msg_1").json()
    plan = built["domainObjects"]["reportPlan"]

    payload = _post(client, "可以，再加一个趋势板块", message_id="msg_2").json()

    assert payload["domainObjects"]["reportPlan"]["revision"] == plan["revision"] + 1


@pytest.mark.parametrize(
    "message",
    [
        "确认",
        "确认。",
        "好的",
        "可以",
        "就按这个方案来",
        "确认，按这个方案继续",
        "确认第2版",
        "没问题了",
        "同意",
    ],
)
def test_pure_confirmation_phrases_are_recognized(message: str) -> None:
    """明确认可的说法要被服务端认出来。"""
    assert is_pure_confirmation(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "改成柱状图，确认",
        "可以，再加一个趋势板块",
        "确认一下加个折线图",
        "PUE 报表确认一下",
    ],
)
def test_messages_with_requirements_are_not_pure_confirmation(message: str) -> None:
    """带着要求的句子不算纯确认。"""
    assert is_pure_confirmation(message) is False


@pytest.mark.parametrize(
    "message",
    ["差不多了", "行吧", "基本可以", "先这样吧", "差不多"],
)
def test_vague_approval_phrases_are_recognized(message: str) -> None:
    """模糊认可要被认出来。"""
    assert is_vague_approval(message) is True


@pytest.mark.parametrize(
    "message",
    ["差不多了，再加个板块", "生成 PUE 报表", "确认", "把标题改成 PUE 分析"],
)
def test_non_vague_messages_are_not_vague_approval(message: str) -> None:
    """带要求或明确的句子不算模糊认可。"""
    assert is_vague_approval(message) is False


# --------------------------------------------------------------------------
# 9. 旧流程保持原语义
# --------------------------------------------------------------------------


def test_existing_session_keeps_old_semantics(client, monkeypatch):
    """已有会话（落过库行、没进过设计阶段）保持旧协议语义。

    这一条模拟的是**旧需求**留下来的会话：它已经有会话行、
    已经有草稿，但从来没有进过新建的设计阶段。规格写明这类会话
    保持原协议语义，不能被新流程重新解释。
    """
    install_model(monkeypatch, RecordingModel(_candidate()))

    # 先落一个旧会话行（库里已有草稿与待确认菜单）
    session_service.repository.upsert_session(
        session_id="sess_old",
        state="WAITING_USER_CONFIRM",
        title="旧的能耗日报",
        snapshot={
            "definition": {
                "metadata": {"id": "def_old", "revision": 1},
                "regions": [],
            }
        },
    )
    session_service.repository.set_pending_choice(
        "sess_old",
        {"state": "WAITING_USER_CONFIRM", "question": {"id": "template_edit_confirm"}},
    )

    payload = client.post(
        "/api/agent/chat",
        json={
            "agentProtocolVersion": "1.0",
            "conversationId": "conv_old",
            "sessionId": "sess_old",
            "messageId": "msg_old_1",
            "state": "ANALYZING",
            "message": "生成 A 机房昨日能耗日报",
            "context": {
                "projectId": "project_001",
                "subjectRef": "machine_room",
                "subjectId": "room_a",
            },
        },
    ).json()

    assert payload.get("designPhase") is None
    assert payload["domainObjects"]["reportDefinitionDraft"] is not None


def test_request_without_message_id_keeps_old_semantics(client, monkeypatch):
    """没有 messageId 的请求不进设计流程（没有可认领的操作）。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    payload = client.post(
        "/api/agent/chat",
        json={
            "agentProtocolVersion": "1.0",
            "conversationId": "conv_no_msgid",
            "sessionId": "sess_no_msgid",
            "state": "ANALYZING",
            "message": "生成 A 机房昨日能耗日报",
            "context": {
                "projectId": "project_001",
                "subjectRef": "machine_room",
                "subjectId": "room_a",
            },
        },
    ).json()

    assert payload.get("designPhase") is None


def test_question_first_session_can_still_start_a_new_build(client, monkeypatch):
    """先问一句再提需求：后一句仍然进入新建流程。

    只读问答那一轮会留下会话行，但没留下任何业务对象 ——
    草稿、待确认菜单、待确认修改计划一个都没有。
    这样的会话仍然是「新建」，不能被挡在设计流程外面。
    """
    install_model(monkeypatch, RecordingModel(_candidate()))

    asked = _post(client, "这个报表支持分钟趋势吗？", message_id="q_1").json()
    assert asked["turnKind"] == "query_answer"
    assert asked.get("designPhase") is None

    built = _post(client, "生成一份 PUE 报表", message_id="q_2").json()
    assert built["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert built["domainObjects"]["reportPlan"]["revision"] == 1


def test_new_build_does_not_run_the_old_router(client, monkeypatch):
    """新建设计轮不跑旧路由。

    旧路由是一次真实模型往返（实测 60~150 秒），而它在新建轮的结果
    **没有人消费** —— 设计流程只用自己的方案上下文（当前方案 + 简短历史）。
    让用户先等一个用不上的结果才看到自己的方案，是白等的。

    顺带的好处：旧路由会把「昨日」「默认能耗指标」这类默认值写进
    collectedInfo 落进快照 —— 设计流程没有让用户指定这些。
    """
    calls: list[str] = []

    async def _record(request, emit=None):  # type: ignore[no-untyped-def]
        calls.append(getattr(request, "message", ""))
        raise AssertionError("新建设计轮不该跑旧路由")

    monkeypatch.setattr(context_engine, "route", _record)
    install_model(monkeypatch, RecordingModel(_candidate()))

    payload = _post(client, "生成 PUE 报表", message_id="msg_1").json()

    assert calls == []
    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"


def test_readonly_question_still_runs_the_old_router(client, monkeypatch):
    """只读提问轮仍然跑旧路由 —— 查询链路要用它的判定。"""
    calls: list[str] = []

    async def _record(request, emit=None):  # type: ignore[no-untyped-def]
        calls.append(getattr(request, "message", ""))
        from app.services.context_engine import RouteDecision

        return RouteDecision(reason="stub")

    monkeypatch.setattr(context_engine, "route", _record)
    install_model(monkeypatch, RecordingModel(_candidate()))

    payload = _post(client, "PUE 是什么意思？", message_id="msg_1").json()

    assert calls == ["PUE 是什么意思？"]
    assert payload["turnKind"] == "query_answer"


def test_confirmation_turn_does_not_run_the_old_router(client, monkeypatch):
    """纯确认轮不跑旧路由 —— 点一下确认不该白等 60~150 秒。

    确认不是提问，也不走查询链路，路由层的结果没有人消费。
    """
    calls: list[str] = []

    async def _record(request, emit=None):  # type: ignore[no-untyped-def]
        calls.append(getattr(request, "message", ""))
        raise AssertionError("确认轮不该跑旧路由")

    monkeypatch.setattr(context_engine, "route", _record)
    install_model(monkeypatch, RecordingModel(_candidate()))

    _post(client, "生成 PUE 报表", message_id="msg_1")
    payload = _post(client, "确认", message_id="msg_2").json()

    assert calls == []
    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert payload["domainObjects"]["reportPlan"]["revision"] == 1
    assert "还没有接入" in payload["reply"]


def test_readonly_question_is_not_hijacked_by_template_flow(client, monkeypatch):
    """只读提问不能被「先看模板」提前分流劫持。

    「当前预览的模板给我看看吗」既像提问又带模板关键词。
    模板流遇到表达不了的条件会直接 BLOCKED_OR_UNSUPPORTED ——
    那是用一句提问触发了一条执行分支（ADR 0002 §6.1 / 规格 #12）。
    """
    install_model(monkeypatch, RecordingModel(_candidate()))

    payload = _post(client, "当前预览的模板给我看看吗？", message_id="t_1").json()

    assert payload["turnKind"] == "query_answer"
    assert payload["state"] != "BLOCKED_OR_UNSUPPORTED"
    assert not payload.get("blocks")


def test_advance_message_in_design_session_does_not_generate_draft(
    client, monkeypatch
):
    """设计会话里的 __advance__ 不能生成草稿。

    __advance__ 是旧状态机的内部控制消息。在新建流程里它没有下一步，
    掉进业务 Agent 会平白生成一份草稿，绕开「先确认，再生成」。
    """
    install_model(monkeypatch, RecordingModel(_candidate()))

    _post(client, "生成 PUE 报表", message_id="a_1")
    payload = _post(client, "__advance__", message_id="a_2").json()

    assert payload["domainObjects"].get("reportDefinitionDraft") is None
    assert payload.get("designPhase") == "PLAN_WAITING_CONFIRM"


# --------------------------------------------------------------------------
# 9. SSE 入口
# --------------------------------------------------------------------------


def test_sse_chat_entry_reports_plan(client, monkeypatch):
    """SSE 聊天入口与 JSON 入口一致。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    with client.stream(
        "POST", "/api/agent/chat/stream", json=_body("直接生成 PUE 报表")
    ) as response:
        assert response.status_code == 200
        chunks = [
            line for line in response.iter_lines() if line.startswith("data: ")
        ]

    finals = [
        json.loads(chunk[len("data: ") :])
        for chunk in chunks
        if json.loads(chunk[len("data: ") :]).get("turnKind") is not None
    ]
    assert finals, "SSE 没有发出 final"
    final = finals[-1]
    assert final["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert final["domainObjects"]["reportPlan"]["revision"] == 1


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _operations() -> list[dict[str, Any]]:
    conn = session_service.repository.conn
    rows = conn.execute(
        "SELECT status FROM report_design_operations ORDER BY created_at, rowid"
    ).fetchall()
    return [dict(row) for row in rows]
