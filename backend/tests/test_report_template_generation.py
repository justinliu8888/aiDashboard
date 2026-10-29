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


def _texts_in(obj: Any) -> list[str]:
    """递归收集所有字符串（只看值，不看键名）。"""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [t for v in obj.values() for t in _texts_in(v)]
    if isinstance(obj, list):
        return [t for v in obj for t in _texts_in(v)]
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
    # 旧口径「数据待连接」已删（规格《回复口径》/ US15）：陌生指标标「能力待接通」。
    assert "数据待连接" not in region2["placeholderReason"]
    assert "能力待接通" in region2["placeholderReason"]
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


# --------------------------------------------------------------------------
# 7. #39 专有：组件类型合法性、空壳占位、时间窗与布局
# --------------------------------------------------------------------------


def _template_with(
    plan_sections,
    *,
    metric_refs=None,
    data_status="placeholder",
    reason="「PUE」口径待核实：当前数据源没有登记这个字段，算不出数值。",
    component_type="metric_card",
    region_data=None,
):
    """造一份模板对象：区域按方案板块顺序，数据状态可控。"""
    return {
        "id": "rtpl_test", "revision": 1, "planId": "plan_x", "planRevision": 1,
        "title": "A 机房能耗日报",
        "regions": [
            {
                "id": f"region_{index + 1}",
                "sectionId": section["id"],
                "title": section["title"],
                "componentType": section.get("componentType", component_type),
                "metricIntents": section.get("metricIntents", []),
                "metricRefs": metric_refs or [],
                "dataStatus": data_status,
                "placeholderReason": reason if data_status == "placeholder" else None,
                **({"data": region_data} if region_data is not None else {}),
            }
            for index, section in enumerate(plan_sections)
        ],
        # 历史 12 列网格坐标：转换层**必须不再读它**
        "layout": {"columns": 12, "items": [
            {"regionId": "region_1",
             "placement": {"type": "GRID", "column": 2, "row": 3,
                           "columnSpan": 10, "rowSpan": 4}},
        ]},
        "referenceTemplates": [],
        "dataStatus": "placeholder",
        "pendingNotes": [],
    }


def _plan_with(plan_sections, *, time_range="最近 7 天"):
    return {
        "id": "plan_x", "revision": 1, "topic": "A 机房能耗日报",
        "requirements": {"metrics": [], "subject": "A 机房",
                         "timeRange": time_range, "exclusions": []},
        "sections": plan_sections,
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }


def test_illegal_component_type_error_names_the_section():
    """模板层第二道闸：组件类型非法 → 硬失败，并指出是哪个板块。"""
    from app.schemas.report_plan import PlanContractError
    from app.services.report_template_service import build_template_design

    plan = {
        "id": "plan_x", "revision": 1, "topic": "A 机房能耗日报",
        "requirements": {"metrics": ["总耗电"], "subject": "A 机房",
                         "timeRange": "最近 7 天", "exclusions": []},
        "sections": [
            {"id": "section_1", "title": "总耗电概览",
             "metricIntents": ["总耗电"], "componentType": "metric_card"},
            {"id": "section_2", "title": "PUE 趋势",
             "metricIntents": ["PUE"], "componentType": "radar"},
        ],
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }

    with pytest.raises(PlanContractError) as excinfo:
        build_template_design(plan, subject_id="room_a")

    assert excinfo.value.code == "PLAN_CONTRACT_INVALID"
    assert "板块 2" in excinfo.value.message
    assert "PUE 趋势" in excinfo.value.message
    assert excinfo.value.section_index == 1
    assert excinfo.value.section_title == "PUE 趋势"


def test_missing_component_type_error_names_the_section():
    """组件类型缺失同样是硬失败，不静默补成 metric_card。"""
    from app.schemas.report_plan import PlanContractError
    from app.services.report_template_service import build_template_design

    plan = {
        "id": "plan_x", "revision": 1, "topic": "T",
        "requirements": {"metrics": [], "subject": "A 机房",
                         "timeRange": "最近 7 天", "exclusions": []},
        "sections": [
            {"id": "section_1", "title": "设备排名",
             "metricIntents": ["设备耗电"]},          # 没有 componentType
        ],
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }

    with pytest.raises(PlanContractError) as excinfo:
        build_template_design(plan, subject_id="room_a")

    assert "板块 1" in excinfo.value.message
    assert "设备排名" in excinfo.value.message


def test_plan_contract_rejects_non_text_section_without_metric_intents():
    """落库前的领域校验：取数板块必须有指标意图；纯说明板块不强制。"""
    from app.schemas.report_plan import PlanContractError
    from app.services.report_plan_service import apply_candidate

    base = {
        "topic": "T",
        "requirements": {"metrics": [], "subject": None, "timeRange": None, "exclusions": []},
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }

    with pytest.raises(PlanContractError) as excinfo:
        apply_candidate(
            {**base, "sections": [
                {"title": "设备排名", "metricIntents": [], "componentType": "table"},
            ]},
            plan_id="plan_001",
        )
    assert "板块 1" in excinfo.value.message
    assert "设备排名" in excinfo.value.message

    # 纯说明板块：没有指标意图也合法
    plan = apply_candidate(
        {**base, "sections": [
            {"title": "说明", "metricIntents": [], "componentType": "text"},
        ]},
        plan_id="plan_002",
    )
    assert plan.sections[0].componentType == "text"


def test_illegal_component_type_fails_the_turn_and_keeps_the_plan(client, monkeypatch):
    """走真实链路：非法组件类型 → 本轮失败，方案保持原样（不死路）。

    只绕过**模型适配层**的枚举检查，让非法值落到**领域层**（带板块名的
    判据在那儿）。领域层在**落库前**就拒绝 → 这一版方案根本没进库，
    用户看到的仍然是上一版成功方案，可以直接改。
    """
    import app.agents.report_plan_agent as report_plan_agent

    monkeypatch.setattr(
        report_plan_agent, "COMPONENT_TYPES",
        tuple(report_plan_agent.COMPONENT_TYPES) + ("radar",),
    )

    install_model(monkeypatch, RecordingModel(
        _candidate(),
        _candidate(sections=[{
            "id": "section_pue", "title": "PUE 趋势",
            "metricIntents": ["PUE"], "componentType": "radar",
        }]),
    ))
    first = _post(client, "生成 A 机房能耗日报").json()["domainObjects"]["reportPlan"]

    payload = _post(client, "再加一个 PUE 趋势板块", message_id="msg_bad").json()

    # 领域层硬失败，错误码与文案都带板块名。
    assert payload["error"]["code"] == "PLAN_CONTRACT_INVALID"
    assert "板块 1" in payload["error"]["message"]
    assert "PUE 趋势" in payload["error"]["message"]
    # 方案**没有被覆盖**：仍然是上一版成功方案（不是「确认完才炸」的死路）。
    stored = report_design_service.repository.load("sess_tpl_1")
    assert stored["reportPlan"]["id"] == first["id"]
    assert stored["reportPlan"]["revision"] == first["revision"]
    assert stored["definitionDraft"] is None


def test_draft_keeps_declared_component_type_for_unavailable_metric():
    """方案要一个没有的指标 → 草稿保留声明的组件类型，出空壳 + 占位原因。"""
    from app.services.template_definition_service import build_template_definition

    sections = [
        {"id": "section_1", "title": "总耗电概览", "metricIntents": ["总耗电"],
         "componentType": "metric_card"},
        {"id": "section_2", "title": "PUE 总览", "metricIntents": ["PUE"],
         "componentType": "metric_card"},
    ]
    draft = build_template_definition(
        _template_with(sections), _plan_with(sections), "room_a",
    )

    region = draft.regions[1]
    assert region["component"]["type"] == "METRIC_CARD"     # 不是 TEXT
    assert region["role"] == "SUMMARY"
    assert region["component"]["metric"]["label"] == "PUE 总览"
    # 占位原因仍在（#40 要读它）
    assert "口径待核实" in region["templateBinding"]["placeholderReason"]
    assert region["templateBinding"]["dataStatus"] == "placeholder"
    # 空壳不编数值、也不可执行
    assert "data" not in region
    assert draft.dependencies["metricRefs"] == []


def test_draft_placeholder_keeps_the_declared_chart_shape():
    """占位的图表板块也保留方案声明的组件类型（BAR / LINE），不降级成文字。"""
    from app.services.template_definition_service import build_template_definition

    sections = [
        {"id": "section_1", "title": "设备能耗排名", "metricIntents": ["PUE"],
         "componentType": "bar_chart"},
    ]
    draft = build_template_definition(
        _template_with(sections), _plan_with(sections), "room_a",
    )

    region = draft.regions[0]
    assert region["component"]["type"] == "CHART"
    assert region["component"]["chartKind"] == "BAR"
    assert region["component"]["encodings"] == []
    assert "data" not in region
    # 占位区域同样带方案时间窗与粒度
    assert region["context"]["timeWindow"]["value"]["count"] == 7
    assert region["context"]["grain"]["value"]["unit"] == "DAY"


def test_draft_device_region_uses_the_plan_window_and_grain():
    """设备类区域的时间窗与粒度一律按方案，不再被收窄到最后一天。"""
    from app.services.template_definition_service import build_template_definition

    sections = [
        {"id": "section_1", "title": "设备耗电排名", "metricIntents": ["设备耗电"],
         "componentType": "table"},
    ]
    template = _template_with(
        sections, metric_refs=["metric.machine_room.device_energy"],
        data_status="bound", reason=None,
    )
    draft = build_template_definition(template, _plan_with(sections), "room_a")

    window = draft.regions[0]["context"]["timeWindow"]["value"]
    assert window["unit"] == "DAY"
    assert window["count"] == 7          # 方案是 7 天，不是 1 天
    assert window["offset"] == -7
    assert draft.regions[0]["context"]["grain"]["value"]["unit"] == "DAY"


def test_device_region_preview_value_is_the_seven_day_total(client, monkeypatch):
    """断言 8：设备类区域的预览数值 == 7 天合计，**不是**最后一天。

    需求稿 §2.7：方案写「最近 7 天」，设备耗电排名与设备类指标卡就按最近七
    天算，**不再单独收窄到最后一天**。

    #39 删掉了那个收窄（实测设备指标卡从 5551.0 kWh 变成 37357.3 kWh，差
    6.7 倍）—— 这是需求要的行为，不是回归。但 #39 同时把断言从「等于某个值」
    降级成了「是个数字」，于是这个数**没有任何断言钉住**：将来有人把收窄
    加回来，没有测试会红。

    这里**独立**按 7 天算一遍（直接用 mock 取数源
    ``mock_data_service.build_snapshot``），再和预览值比对：等于 7 天合计，
    且**不等于**最后一天。

    ⚠️ 期望值按**方案**的 7 天窗口算，不按区域自己声明的窗口算 —— 否则实现
    一旦把区域收窄成 1 天，期望值会跟着收窄，这条用例就白写了。
    """
    from datetime import date, timedelta

    from app.services.mock_data_service import build_snapshot

    candidate = _candidate(
        topic="A 机房最近 7 天设备耗电",
        time_range="最近 7 天",
        metrics=["设备耗电"],
        sections=[
            {
                "id": "section_device", "title": "设备耗电",
                "metricIntents": ["设备耗电"], "componentType": "metric_card",
            }
        ],
    )
    install_model(monkeypatch, RecordingModel(candidate))
    plan = _post(client, "生成 A 机房最近 7 天设备耗电报表").json()["domainObjects"]["reportPlan"]
    assert plan["requirements"]["timeRange"] == "最近 7 天"

    _confirm(client, plan, "msg_confirm_device")
    _post(client, "同意", message_id="msg_agree_device")
    for state in ("VALIDATING", "PREVIEWING", "PREVIEW_READY"):
        advanced = _post(client, "__advance__", message_id="advance_" + state).json()
        assert advanced["error"] is None
        assert advanced["state"] == state

    payload = client.get("/api/sessions/sess_tpl_1").json()["snapshot"]["previewPayload"]
    region = payload["regions"][0]
    assert region["componentType"] == "METRIC_CARD"

    logical_date = date.fromisoformat(payload["logicalDate"])
    room_id = payload["snapshot"]["roomId"]
    room_name = payload["snapshot"]["roomName"]

    def _device_total(days: list[str]) -> float:
        return round(sum(
            device.energyKwh
            for day in days
            for device in build_snapshot(
                room_id=room_id,
                room_name=room_name,
                logical_date=day,
                timezone="Asia/Shanghai",
            ).deviceRanking
        ), 1)

    # 方案写的是最近 7 天 → 窗口是 logicalDate 往前 6 天直到 logicalDate。
    seven_days = [
        (logical_date - timedelta(days=6 - index)).isoformat() for index in range(7)
    ]
    expected_total = _device_total(seven_days)
    last_day_only = _device_total([logical_date.isoformat()])

    # 7 天合计，不是最后一天。
    assert region["payload"]["value"] == expected_total
    assert region["payload"]["value"] != last_day_only
    assert "7 天" in region["payload"]["windowLabel"]


def test_draft_ignores_historical_twelve_column_placement():
    """转换过程不读历史 12 列网格 placement（#39 硬要求）。"""
    from app.services.template_definition_service import build_template_definition

    sections = [
        {"id": "section_1", "title": "总耗电概览", "metricIntents": ["总耗电"],
         "componentType": "metric_card"},
    ]
    template = _template_with(
        sections, metric_refs=["metric.machine_room.total_energy"],
        data_status="bound", reason=None,
    )
    draft = build_template_definition(template, _plan_with(sections), "room_a")

    placement = draft.regions[0]["layout"]["placement"]
    assert placement != {"type": "GRID", "column": 2, "row": 3,
                         "columnSpan": 10, "rowSpan": 4}
    assert placement["column"] == 1
    assert placement["columnSpan"] == 4
    # 12 列网格约束
    assert placement["column"] + placement["columnSpan"] - 1 <= 12


def test_draft_regions_match_the_confirmed_plan_one_to_one():
    """草稿区域与已确认方案逐项一致：数量、顺序、标题、组件类型。"""
    from app.services.template_definition_service import build_template_definition

    sections = [
        {"id": "section_1", "title": "总耗电概览", "metricIntents": ["总耗电"],
         "componentType": "metric_card"},
        {"id": "section_2", "title": "设备耗电排名", "metricIntents": ["设备耗电"],
         "componentType": "table"},
        {"id": "section_3", "title": "能耗趋势", "metricIntents": ["总耗电"],
         "componentType": "line_chart"},
    ]
    template = _template_with(
        sections, metric_refs=["metric.machine_room.total_energy"],
        data_status="bound", reason=None,
    )
    draft = build_template_definition(template, _plan_with(sections), "room_a")

    assert len(draft.regions) == len(sections)
    assert [r["name"] for r in draft.regions] == [s["title"] for s in sections]
    assert [r["templateBinding"]["sectionId"] for r in draft.regions] == \
           [s["id"] for s in sections]
    assert [r["component"]["type"] for r in draft.regions] == \
           ["METRIC_CARD", "TABLE", "CHART"]
    assert draft.regions[2]["component"]["chartKind"] == "LINE"


@pytest.mark.parametrize(
    ("plan_type", "expected_type", "expected_chart_kind"),
    [
        ("metric_card", "METRIC_CARD", None),
        ("line_chart", "CHART", "LINE"),
        ("bar_chart", "CHART", "BAR"),
        ("pie_chart", "CHART", "PIE"),
        ("table", "TABLE", None),
        ("text", "TEXT", None),
    ],
)
def test_draft_component_type_follows_the_plan_declaration(
    client, monkeypatch, plan_type, expected_type, expected_chart_kind
):
    """断言 3：六种方案组件类型 → 草稿组件类型的完整映射。

    需求稿 §2.5：指标卡、柱状图、折线图、表格、文本按确认结果映射；
    **展示为占位不等于改掉组件类型**。

    已有用例只覆盖了其中一部分（``metric_card`` / ``line_chart`` / ``table``，
    以及**占位**路径下的 ``bar_chart``）。这里把六种映射**逐条**钉死，补上
    bound 路径下的 ``bar_chart`` / ``pie_chart`` 与 ``text``。
    """
    is_text = plan_type == "text"
    candidate = _candidate(
        topic=f"A 机房 {plan_type} 报表",
        metrics=[] if is_text else ["总耗电"],
        sections=[
            {
                "id": "section_one", "title": "单一板块",
                "metricIntents": [] if is_text else ["总耗电"],
                "componentType": plan_type,
            }
        ],
    )
    install_model(monkeypatch, RecordingModel(candidate))
    plan = _post(client, f"生成 A 机房 {plan_type} 报表").json()["domainObjects"]["reportPlan"]
    assert plan["sections"][0]["componentType"] == plan_type

    draft = _confirm(client, plan, "msg_confirm_type")["domainObjects"]["reportDefinitionDraft"]

    region = draft["regions"][0]
    assert region["component"]["type"] == expected_type
    if expected_chart_kind is None:
        assert "chartKind" not in region["component"]
    else:
        assert region["component"]["chartKind"] == expected_chart_kind


def test_draft_metrics_are_not_swapped_with_another_metric():
    """指标一致性：bound 区域的指标**没有被替换成另一个指标**（#39 §0.4.2）。

    方案板块声明的 metricIntents → 模板层匹配到的 metricRef →
    草稿的 ``templateBinding.metricRefs``，三者必须是同一个口径。
    """
    from app.services.report_template_service import build_template_design
    from app.services.template_definition_service import build_template_definition

    sections = [
        {"id": "section_total", "title": "总耗电概览",
         "metricIntents": ["总耗电"], "componentType": "metric_card"},
        {"id": "section_device", "title": "设备耗电排名",
         "metricIntents": ["设备耗电"], "componentType": "table"},
    ]
    plan = {
        "id": "plan_x", "revision": 1, "topic": "A 机房能耗日报",
        "requirements": {"metrics": ["总耗电", "设备耗电"], "subject": "A 机房",
                         "timeRange": "最近 7 天", "exclusions": []},
        "sections": sections,
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }

    template = build_template_design(plan, subject_id="room_a").model_dump(mode="json")
    draft = build_template_definition(template, plan, "room_a")

    by_section = {r["templateBinding"]["sectionId"]: r for r in draft.regions}
    for section in sections:
        region = by_section[section["id"]]
        assert region["templateBinding"]["dataStatus"] == "bound"
    # 方案意图 → 草稿指标口径，一一对应，**没有互换**。
    assert by_section["section_total"]["templateBinding"]["metricRefs"] == \
           ["metric.machine_room.total_energy"]
    assert by_section["section_device"]["templateBinding"]["metricRefs"] == \
           ["metric.machine_room.device_energy"]


def test_draft_binding_passes_the_pending_reason_through():
    """``pendingReason`` 是模板层 ``data.pendingReason`` 的**透传**（#39 §0.4.1）。

    文案唯一来源是模板层（#38 产出）；草稿层不另写一句。

    文案本身按 ADR 0004 换过口径：草稿阶段显示的是**模拟业务数据**，
    不再是「数值待试运行产生」。
    """
    from app.services.template_definition_service import build_template_definition

    sections = [
        {"id": "section_1", "title": "总耗电概览", "metricIntents": ["总耗电"],
         "componentType": "metric_card"},
    ]
    template = _template_with(
        sections, metric_refs=["metric.machine_room.total_energy"],
        data_status="bound", reason=None,
        region_data={
            "bindingStatus": "pending_execution",
            "pendingReason": "本区域数值为模拟业务数据，接入真实数据后自动替换。",
        },
    )
    draft = build_template_definition(template, _plan_with(sections), "room_a")

    binding = draft.regions[0]["templateBinding"]
    assert binding["pendingReason"] == "本区域数值为模拟业务数据，接入真实数据后自动替换。"
    # 旧口径（「数值待试运行产生」）已经作废：草稿阶段一律用模拟业务数据渲染。
    assert "待试运行" not in binding["pendingReason"]
    # bound 区域不是占位：placeholderReason 仍然是 None。
    assert binding["placeholderReason"] is None
    assert binding["dataStatus"] == "bound"
    # #40 要读的字段一个都没少。
    assert binding["sectionId"] == "section_1"
    assert binding["dataSourceLabel"] is None
    assert binding["metricRefs"] == ["metric.machine_room.total_energy"]


def test_draft_stage_has_no_business_values_and_the_metric_slot_is_empty(
    client, monkeypatch
):
    """断言 5：草稿阶段没有业务数值；指标值位是空的（面板渲染成「—」）。

    需求稿 §2.1：生成草稿时不执行业务取数。指标值用 ``—``，图表和表格保持
    占位；**不得编造数值，也不提前展示 Mock 数值**。

    口径提醒：「无 ``kWh``」**只能对占位卡断**。占位卡的 ``unitLabel`` 是
    空串（``template_definition_service._empty_shell``），整张卡里没有
    ``kWh``；而 **bound 区的 ``kWh`` 是单位标签**，本来就应该显示，不能拿它
    当成「编造数值」。所以这里分两路断：

      - 占位卡：没有 ``kWh``、没有小数、没有 ``%``、没有数据块；
      - bound 指标卡：有 ``kWh``（单位），但**没有带单位的数值**，值位是空的。
    """
    candidate = _candidate(
        topic="A 机房能耗与 PUE 专题",
        metrics=["总耗电", "PUE"],
        sections=[
            {
                "id": "section_total", "title": "总耗电概览",
                "metricIntents": ["总耗电"], "componentType": "metric_card",
            },
            {
                "id": "section_pue", "title": "PUE 总览",
                "metricIntents": ["PUE"], "componentType": "metric_card",
            },
        ],
    )
    install_model(monkeypatch, RecordingModel(candidate))
    plan = _post(client, "生成 A 机房能耗与 PUE 报表").json()["domainObjects"]["reportPlan"]
    draft = _confirm(client, plan, "msg_confirm_no_values")["domainObjects"]["reportDefinitionDraft"]
    by_name = {region["name"]: region for region in draft["regions"]}

    bound = by_name["总耗电概览"]
    placeholder = by_name["PUE 总览"]

    # ① 指标值位是空的 —— 面板据此渲染成「—」（bound 卡与占位卡都是）。
    for region in (bound, placeholder):
        assert "value" not in region["component"]["metric"], region["name"]
        assert "values" not in region["component"]["metric"], region["name"]
        assert "value" not in (region.get("data") or {}), region["name"]

    # ② 占位卡：没有 kWh、没有小数、没有百分比、没有数据块。
    placeholder_text = json.dumps(placeholder, ensure_ascii=False)
    assert "kWh" not in placeholder_text
    assert "%" not in placeholder_text
    assert "data" not in placeholder
    assert [n for n in _numbers_in(placeholder) if isinstance(n, float)] == []

    # ③ bound 指标卡：kWh 是**单位标签**，允许出现；但不能出现「数值 + kWh」。
    assert bound["component"]["metric"]["format"]["unitLabel"] == "kWh"
    assert not [
        text for text in _texts_in(bound)
        if "kWh" in text and any(char.isdigit() for char in text)
    ]
    assert "%" not in json.dumps(bound, ensure_ascii=False)
    assert [n for n in _numbers_in(bound) if isinstance(n, float)] == []


def test_placeholder_region_binding_carries_the_four_fields(client, monkeypatch):
    """断言 7：占位区域的 ``templateBinding`` 四个字段齐全。

    需求稿 §2.5 + #39 §0.4.1：``dataStatus`` / ``placeholderReason`` /
    ``pendingReason`` / ``dataSourceLabel`` 都要在。bound 区域
    ``pendingReason`` 有值、``placeholderReason`` 是 ``None``；占位区域反过来
    —— ``placeholderReason`` 有值、``pendingReason`` 是 ``None``，
    ``dataSourceLabel`` 也是 ``None``（没有数据源可标）。
    """
    candidate = _candidate(
        topic="A 机房能耗与 PUE 专题",
        metrics=["总耗电", "PUE"],
        sections=[
            {
                "id": "section_total", "title": "总耗电概览",
                "metricIntents": ["总耗电"], "componentType": "metric_card",
            },
            {
                "id": "section_pue", "title": "PUE 总览",
                "metricIntents": ["PUE"], "componentType": "metric_card",
            },
        ],
    )
    install_model(monkeypatch, RecordingModel(candidate))
    plan = _post(client, "生成 A 机房能耗与 PUE 报表").json()["domainObjects"]["reportPlan"]
    draft = _confirm(client, plan, "msg_confirm_binding")["domainObjects"]["reportDefinitionDraft"]
    by_name = {region["name"]: region for region in draft["regions"]}

    placeholder = by_name["PUE 总览"]["templateBinding"]
    for field in ("dataStatus", "placeholderReason", "pendingReason", "dataSourceLabel"):
        assert field in placeholder, field
    assert placeholder["dataStatus"] == "placeholder"
    assert placeholder["placeholderReason"]
    # pendingReason 只对 bound 区域有意义（那里是「还没取数」）。
    assert placeholder["pendingReason"] is None
    assert placeholder["dataSourceLabel"] is None

    bound = by_name["总耗电概览"]["templateBinding"]
    for field in ("dataStatus", "placeholderReason", "pendingReason", "dataSourceLabel"):
        assert field in bound, field
    assert bound["dataStatus"] == "bound"
    assert bound["pendingReason"]
    assert bound["placeholderReason"] is None
    assert bound["dataSourceLabel"]
    assert len(bound["metricRefs"]) == 1


def test_unaccepted_suggestions_do_not_enter_the_draft(client, monkeypatch):
    """断言 12：方案里的可选建议**不自动进入草稿**。

    需求稿 §2.5：方案中的可选建议、未接受修改不自动进入草稿。建议只是建议
    —— 用户没接受，它就不该变成一个板块，也不该出现在草稿的任何地方。
    """
    candidate = _candidate(
        topic="A 机房能耗日报",
        metrics=["总耗电"],
        sections=[
            {
                "id": "section_total", "title": "总耗电概览",
                "metricIntents": ["总耗电"], "componentType": "metric_card",
            }
        ],
    )
    candidate["candidate"]["suggestions"] = ["建议追加 PUE 板块", "建议改成柱状图"]
    install_model(monkeypatch, RecordingModel(candidate))
    plan = _post(client, "生成 A 机房能耗日报").json()["domainObjects"]["reportPlan"]
    assert plan["suggestions"] == ["建议追加 PUE 板块", "建议改成柱状图"]

    draft = _confirm(client, plan, "msg_confirm_suggestions")["domainObjects"]["reportDefinitionDraft"]

    # 建议没有变成板块。
    assert len(draft["regions"]) == len(plan["sections"]) == 1
    assert [region["name"] for region in draft["regions"]] == ["总耗电概览"]
    # 建议的文字也没有出现在草稿的任何地方。
    draft_text = json.dumps(draft, ensure_ascii=False)
    assert "建议追加 PUE 板块" not in draft_text
    assert "建议改成柱状图" not in draft_text
    assert "PUE" not in draft_text
