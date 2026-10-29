"""工单 02 / GitHub #38：确认方案后生成模板（真实链路），**模板阶段不取数**。

用户口径（需求访谈 Q13）：「数据库里有啥数据就展示啥，没有的
就按 Q11 和 Q12 的方案来」—— 所以：

    - 确认后**必须**出模板，不能把流程断在「还没接入」；
    - 当前能力算得出数的区域**绑定指标 + 写明时间窗**，并标明数据来源性质；
    - 算不出数的区域保留占位，并写明原因（数据待连接 / 口径待核实）；
    - 占位区域**不填任何数值** —— 不许拿假数糊弄。

**工单 04 / #38 追加的边界**：模板阶段**不读业务数据、不算出任何数值**。
数值一律留到**试运行**时产生。所以这里的 ``dataStatus == "bound"``
意思是「指标已绑定、能力可用」，**不是**「已经取到数」；区域里带的是
**绑定描述符**（指标绑定 + 时间窗），没有任何业务数值。

测试边界与 #19 集成测试一致：走真实 JSON 聊天路由、真实编排、
真实 SQLite 落库与恢复，只替换外部模型调用。
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.preview_service import preview_service
from app.services.report_design_service import report_design_service
from app.services.report_template_service import build_template_design
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


def _confirm(
    client: TestClient,
    plan: dict[str, Any],
    message_id: str,
    *,
    session_id: str = "sess_tpl_1",
) -> dict[str, Any]:
    payload = _post(
        client,
        "确认",
        message_id=message_id,
        session_id=session_id,
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
    # #38：模板阶段不取数 —— 区域里没有趋势数值，只有绑定与时间窗。
    assert region["data"].get("bindingStatus") == "pending_execution"
    assert "yValues" not in region["data"]
    # 指标匹配判定保留（这就是试运行取数要用的执行绑定）。
    assert region["metricRefs"] == ["metric.machine_room.total_energy"]


def test_confirm_generates_template_bound_to_the_confirmed_plan(client, monkeypatch):
    """确认后阶段推进到 DRAFT_READY（草稿就绪），模板绑定的是确认的那一版方案。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    built = _post(client, "做一份 A 机房最近 7 天的能耗报表").json()
    plan = built["domainObjects"]["reportPlan"]

    payload = _confirm(client, plan, "msg_2")

    assert payload["designPhase"] == "DRAFT_READY"
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

    assert "draft_ready" in payload["uiActions"]


def test_confirm_does_not_fall_through_to_the_old_report_flow(client, monkeypatch):
    """确认方案后草稿直出，但**不**顺手触发旧的保存 / 取数 / 预览链路。

    草稿是**新流程自己**生成的（#36：确认方案一次到位直接出草稿），
    不是掉进旧链路的产物；而旧的保存、校验、预览一个都不跑
    （#38：草稿生成阶段停止提前取数）。
    """
    install_model(monkeypatch, RecordingModel(_candidate()))

    built = _post(client, "做一份 A 机房最近 7 天的能耗报表").json()
    plan = built["domainObjects"]["reportPlan"]

    payload = _confirm(client, plan, "msg_2")

    # 旧链路一个工具请求都没发
    assert payload["toolRequests"] == []
    # 草稿是新流程直出的（不是旧链路落下的）
    assert payload["domainObjects"].get("reportDefinitionDraft") is not None
    # 但没有顺手把预览跑了 —— 预览取数只发生在用户点「试运行」之后
    assert payload["domainObjects"].get("reportRunPreview") is None


# --------------------------------------------------------------------------
# 2. 能力核对：绑得上指标 / 绑不上占位（**不取数**）
# --------------------------------------------------------------------------


def test_available_metric_region_is_bound_without_fetching_data(client, monkeypatch):
    """能力算得出的区域**绑上指标**，但区域里没有任何业务数值（#38）。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    built = _post(client, "做一份 A 机房最近 7 天的能耗报表").json()
    plan = built["domainObjects"]["reportPlan"]

    template = _confirm(client, plan, "msg_2")["domainObjects"]["templateDesign"]

    region = template["regions"][0]
    assert region["dataStatus"] == "bound"
    assert region["placeholderReason"] is None
    # #38：不再「接上数据」，只写明绑了哪个指标、用哪个时间窗。
    assert "value" not in region["data"]
    assert "yoyPercent" not in region["data"]
    # 单位是元数据，允许显示
    assert region["data"]["unit"] == "kWh"
    # 区域仍然带时间窗（时间参数是允许显示的元数据）
    assert region["data"]["timeWindow"]["count"] == 7
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
    """混合场景：能绑定指标的区域是 bound，绑不上的区域占位，两边都在。"""
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
    # bound 区域带的是绑定描述符，不是数值
    assert "value" not in by_section[by_title["总耗电概览"]]["data"]


# --------------------------------------------------------------------------
# 3. #38 专有：模板阶段不取数，能力判定一条都不能少
# --------------------------------------------------------------------------


def test_template_generation_performs_no_business_fetch(client, monkeypatch):
    """模板生成阶段不允许发生任何业务取数（#38 硬要求）。

    把取数入口打成炸弹：只要模板阶段碰了业务数据，这里立刻炸。
    """
    import app.services.mock_data_service as mock_data_service
    import app.services.preview_service as preview_service_module
    import app.services.report_template_service as report_template_service

    calls: list[tuple] = []

    def boom(*args, **kwargs):
        calls.append(args)
        raise AssertionError("模板生成阶段不允许取数")

    # 三处都打：mock_data_service 是源头，preview_service 与
    # report_template_service 用的是「模块内绑定名」，只改源头对它们无效。
    # report_template_service 那一处用 raising=False：#38 之后模块里已经
    # 没有这个名字，此时「压根没有可调用的取数入口」本身就是正确状态。
    monkeypatch.setattr(mock_data_service, "build_snapshot", boom)
    monkeypatch.setattr(preview_service_module, "build_snapshot", boom)
    monkeypatch.setattr(report_template_service, "build_snapshot", boom, raising=False)

    install_model(monkeypatch, RecordingModel(_candidate()))
    built = _post(client, "做一份 A 机房最近 7 天的能耗报表").json()
    plan = built["domainObjects"]["reportPlan"]

    template = _confirm(client, plan, "msg_2")["domainObjects"]["templateDesign"]

    assert calls == []
    # 能力判定没被误伤：仍然判定为「指标已绑定」
    region = template["regions"][0]
    assert region["dataStatus"] == "bound"
    assert region["metricRefs"] == ["metric.machine_room.total_energy"]


def _numbers_in(obj: Any) -> list[float]:
    """递归收集数值（bool 不算）。"""
    if isinstance(obj, bool):
        return []
    if isinstance(obj, (int, float)):
        return [obj]
    if isinstance(obj, dict):
        return [n for v in obj.values() for n in _numbers_in(v)]
    if isinstance(obj, list):
        return [n for v in obj for n in _numbers_in(v)]
    return []


def test_bound_region_carries_binding_and_window_but_no_business_value(
    client, monkeypatch
):
    """区域保留指标绑定与时间窗，但区域里没有任何业务数值（#38 硬要求）。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    built = _post(client, "做一份 A 机房最近 7 天的能耗报表").json()
    plan = built["domainObjects"]["reportPlan"]

    template = _confirm(client, plan, "msg_2")["domainObjects"]["templateDesign"]
    region = template["regions"][0]

    # ① 能力判定保留：指标匹配上了 → bound
    assert region["dataStatus"] == "bound"
    assert region["placeholderReason"] is None
    assert region["metricRefs"] == ["metric.machine_room.total_energy"]

    # ② 区域仍带着时间窗（时间参数是允许显示的元数据）
    assert region["data"]["timeWindow"]["type"] == "CALENDAR"
    assert region["data"]["timeWindow"]["count"] == 7
    assert region["data"]["timeWindow"]["unit"] == "DAY"
    assert region["data"]["windowLabel"]

    # ③ 区域里没有任何业务数值
    for key in ("value", "values", "yValues", "xLabels", "rows", "points",
                "yoyPercent", "comparisonAvailable"):
        assert key not in region["data"], key
    # 只剩时间参数与标签这类元数据，没有小数型业务值
    assert [n for n in _numbers_in(region["data"]) if isinstance(n, float)] == []
    assert region["data"]["bindingStatus"] == "pending_execution"


def test_capability_checks_survive_without_fetching_data(client, monkeypatch):
    """不取数不等于不判定：口径待核实 / 指标缺失两条分支都还在。"""
    # ① 口径待核实：PUE 登记在册，但数据源没有这个字段，算不出数
    install_model(monkeypatch, RecordingModel(_pue_candidate()))
    plan = _post(client, "生成 PUE 报表").json()["domainObjects"]["reportPlan"]
    template = _confirm(client, plan, "msg_2")["domainObjects"]["templateDesign"]
    region = template["regions"][0]
    assert region["dataStatus"] == "placeholder"
    assert "口径待核实" in region["placeholderReason"]
    assert region["data"] is None
    # 指标还是识别出来了，只是算不出数
    assert region["metricRefs"] == ["metric.machine_room.pue"]

    # ② 指标缺失：当前已接通的能力里没有对应指标
    install_model(
        monkeypatch,
        RecordingModel(
            _candidate(
                topic="客流量报表",
                metrics=["客流量"],
                sections=[
                    {
                        "id": "section_footfall",
                        "title": "客流量概览",
                        "metricIntents": ["客流量"],
                        "componentType": "metric_card",
                    }
                ],
            )
        ),
    )
    plan2 = _post(
        client, "生成客流量报表", session_id="sess_tpl_2", message_id="msg_1"
    ).json()["domainObjects"]["reportPlan"]
    template2 = _confirm(
        client, plan2, "msg_2", session_id="sess_tpl_2"
    )["domainObjects"]["templateDesign"]
    region2 = template2["regions"][0]
    assert region2["dataStatus"] == "placeholder"
    assert "数据待连接" in region2["placeholderReason"]
    assert region2["data"] is None
    assert region2["metricRefs"] == []


def test_authorization_scope_check_survives_without_fetching_data():
    """统计对象不在授权范围内 → 区域占位（#38 只停取数，不停授权判定）。

    这一条走服务直调：走聊天路由时授权检查在更上游就先报
    ``SCOPE_NOT_AUTHORIZED`` 拒掉整轮确认，区域级的占位看不到。
    """
    plan = {
        "id": "rplan_scope",
        "revision": 1,
        "topic": "B 机房能耗日报",
        "requirements": {"subject": "B 机房", "timeRange": "最近 7 天"},
        "sections": [
            {
                "id": "section_total",
                "title": "总耗电概览",
                "metricIntents": ["总耗电"],
                "componentType": "metric_card",
            }
        ],
    }

    design = build_template_design(plan, subject_id="room_b")

    region = design.regions[0]
    assert region.dataStatus == "placeholder"
    assert "授权范围" in region.placeholderReason
    assert region.data is None
    # 指标判定没被误伤：指标绑上了，只是统计对象接不到
    assert region.metricRefs == ["metric.machine_room.total_energy"]


# --------------------------------------------------------------------------
# 4. 试运行仍然出数（#38 只停模板阶段取数，不停试运行取数）
# --------------------------------------------------------------------------


def test_trial_run_still_produces_numbers(client, monkeypatch):
    """试运行仍然出数。

    如果这里取不到数，就说明「停止提前取数」被实现成了「永久屏蔽数值」——
    那是不允许的：数值本来就该在试运行时才产生。
    """
    install_model(monkeypatch, RecordingModel(_candidate()))
    built = _post(client, "做一份 A 机房最近 7 天的能耗报表").json()
    plan = built["domainObjects"]["reportPlan"]

    payload = _confirm(client, plan, "msg_2")
    template = payload["domainObjects"]["templateDesign"]
    draft = payload["domainObjects"]["reportDefinitionDraft"]

    # 模板侧：只有绑定描述符，没有数值
    assert "value" not in template["regions"][0]["data"]

    # 草稿侧：执行绑定完整透传，试运行才有数可取
    binding = draft["regions"][0]["templateBinding"]
    assert binding["dataStatus"] == "bound"
    assert binding["metricRefs"] == ["metric.machine_room.total_energy"]

    # 试运行侧：出数
    preview, record = preview_service.run_preview(
        definitionId="def_tpl_1", definitionDraft=draft
    )
    assert preview.status in {"SUCCEEDED", "PARTIAL"}
    payload_of_region = record.regions[0]["payload"]
    assert isinstance(payload_of_region["value"], (int, float))
    assert payload_of_region["value"] > 0
    assert payload_of_region["unit"] == "kWh"


# --------------------------------------------------------------------------
# 5. 重复确认不重复生成
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
    # 停在「等确认」，没有推进到草稿就绪
    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"


# --------------------------------------------------------------------------
# 6. 刷新 / 恢复
# --------------------------------------------------------------------------


def test_session_detail_restores_the_template(client, monkeypatch):
    """刷新后能从会话详情拿回同一份模板，不用重新确认。"""
    install_model(monkeypatch, RecordingModel(_candidate()))

    built = _post(client, "做一份 A 机房最近 7 天的能耗报表").json()
    plan = built["domainObjects"]["reportPlan"]
    template = _confirm(client, plan, "msg_2")["domainObjects"]["templateDesign"]

    detail = client.get("/api/sessions/sess_tpl_1").json()

    assert detail["snapshot"]["designPhase"] == "DRAFT_READY"
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
