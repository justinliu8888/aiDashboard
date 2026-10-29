"""方案确认一次到位：草稿、确认记录与下一步菜单同一次事务落库。

对外成功落点是「草稿就绪 + 等待用户确认」：用户点一次「确认方案」，
同一轮就带回报表草稿与「同意 / 修改 / 重新开始」菜单，不需要再发一句
「确认模板」。

测试边界与工单 02 一致：走真实 JSON 聊天路由、真实编排、真实 SQLite
落库与恢复，只替换外部模型调用。
"""
import json

import pytest

from app.services.report_design_service import report_design_service
from app.services.session_service import session_service
from tests.test_report_template_generation import (
    RecordingModel, _body, _candidate, _post, _pue_candidate, design_deps, install_model,
)


def _plan(client, monkeypatch, *candidates):
    """跑一轮设计，拿到文字方案（还没确认）。"""
    model = install_model(monkeypatch, RecordingModel(*(candidates or [_candidate()])))
    plan = _post(client, "生成 A 机房最近 7 天能耗报表").json()["domainObjects"]["reportPlan"]
    return model, plan


def _confirm_plan(client, plan, message_id="confirm_plan"):
    """点「确认方案」：同一轮就出草稿与下一步菜单。"""
    return _post(client, "确认", message_id=message_id, context={
        "designAction": "CONFIRM_PLAN",
        "planId": plan["id"],
        "basePlanRevision": plan["revision"],
    }).json()


def _confirm_template(client, template, **context):
    """历史兼容入口：确认模板（新流程不再产生这个阶段）。"""
    return _post(client, "确认模板", message_id="confirm_template", context={
        "designAction": "CONFIRM_TEMPLATE", "templateId": template["id"],
        "baseTemplateRevision": template["revision"], **context,
    }).json()


# --------------------------------------------------------------------------
# 1. 一次确认就出草稿
# --------------------------------------------------------------------------


def test_confirm_plan_builds_draft_and_publishes_next_step_menu(client, monkeypatch):
    """点一次「确认方案」：同一轮就带回草稿与「同意/修改/重新开始」菜单。"""
    install_model(monkeypatch, RecordingModel(_candidate()))
    plan = _post(client, "生成 A 机房最近 7 天能耗报表").json()["domainObjects"]["reportPlan"]

    payload = _confirm_plan(client, plan)

    assert payload["error"] is None
    assert payload["designPhase"] == "DRAFT_READY"
    assert payload["state"] == "WAITING_USER_CONFIRM"
    draft = payload["domainObjects"]["reportDefinitionDraft"]
    template = payload["domainObjects"]["templateDesign"]
    assert draft["metadata"]["name"] == template["title"]
    assert [r["id"] for r in draft["regions"]] == [r["id"] for r in template["regions"]]
    assert draft["defaults"]["timeWindow"]["count"] == 7
    # 下一步菜单复用既有待确认选项契约，不新造 question id。
    assert payload["questions"][0]["id"] == "template_edit_confirm"
    assert [o["value"] for o in payload["questions"][0]["options"]] == ["同意", "修改", "重新开始"]
    # 草稿、阶段与菜单同一次事务落库。
    detail = client.get("/api/sessions/sess_tpl_1").json()
    assert detail["snapshot"]["designPhase"] == "DRAFT_READY"
    assert detail["snapshot"]["definition"] == draft
    assert detail["pendingChoice"]["question"]["id"] == "template_edit_confirm"


def test_confirm_plan_carries_the_confirmed_plan_for_the_collapsed_view(client, monkeypatch):
    """草稿阶段的响应仍然带着**已确认的那一版**方案。"""
    install_model(monkeypatch, RecordingModel(_candidate()))
    plan = _post(client, "生成 A 机房最近 7 天能耗报表").json()["domainObjects"]["reportPlan"]

    payload = _confirm_plan(client, plan)

    confirmed = payload["domainObjects"]["reportPlan"]
    assert confirmed["id"] == plan["id"]
    assert confirmed["revision"] == plan["revision"]
    assert confirmed["topic"] == plan["topic"]


def test_confirm_plan_reply_states_actual_title_and_plan_revision(client, monkeypatch):
    """回复文案带**实际已确认的标题与方案版本号**，不写死示例文本。"""
    install_model(monkeypatch, RecordingModel(_candidate(topic="B 机房周报")))
    plan = _post(client, "生成 B 机房周报").json()["domainObjects"]["reportPlan"]

    payload = _confirm_plan(client, plan)

    assert plan["topic"] == "B 机房周报"
    assert "B 机房周报" in payload["reply"]
    assert f"第 {plan['revision']} 版" in payload["reply"]
    assert "草稿已生成，请在右侧选择下一步" in payload["reply"]


def test_confirmed_plan_runs_legacy_validation_preview_and_publish_menu(client, monkeypatch):
    """草稿就绪后「同意」，走既有的保存 → 校验 → 预览 → 渲染链路。"""
    model, plan = _plan(client, monkeypatch)
    _confirm_plan(client, plan)

    saved = _post(client, "同意", message_id="save_draft").json()
    assert saved["state"] == "SAVE_DEFINITION_DRAFT"
    assert saved["error"] is None
    definition_id = saved["toolRequests"][0]["arguments"]["definitionId"]
    for state in ["VALIDATING", "PREVIEWING", "PREVIEW_READY", "RENDERING", "READY"]:
        payload = _post(client, "__advance__", message_id="advance_" + state).json()
        assert payload["error"] is None
        assert payload["state"] == state
        if state == "PREVIEWING":
            assert payload["toolRequests"][0]["arguments"] == {"definitionId": definition_id, "revision": 1}
        if state == "PREVIEW_READY":
            assert payload["domainObjects"]["reportRunPreview"]["status"] in {"SUCCEEDED", "PARTIAL"}
    detail = client.get("/api/sessions/sess_tpl_1").json()
    assert detail["snapshot"]["previewPayload"]["definitionId"] == definition_id
    assert detail["state"] == "READY"
    published = _post(client, "发布", message_id="publish").json()
    assert published["state"] == "READY"
    assert "发布流程尚未接入（Mock）" in published["reply"]
    assert published["error"] is None
    assert len(model.calls) == 1


@pytest.mark.parametrize("message", ["确认", "同意", "就按这个方案继续"])
def test_text_confirmation_produces_draft_without_calling_model(client, monkeypatch, message):
    """自然语言确认由服务端识别，绑定当前方案版本，直接产出草稿。"""
    model, plan = _plan(client, monkeypatch)

    payload = _post(client, message, message_id="text_confirm").json()

    assert payload["designPhase"] == "DRAFT_READY"
    assert payload["domainObjects"]["reportDefinitionDraft"] is not None
    binding = payload["domainObjects"]["reportDefinitionDraft"]["metadata"]["templateBinding"]
    assert binding["planId"] == plan["id"]
    assert binding["planRevision"] == plan["revision"]
    assert len(model.calls) == 1


# --------------------------------------------------------------------------
# 2. 菜单契约：自由输入不能绕过菜单
# --------------------------------------------------------------------------


def test_modify_choice_from_draft_menu_enters_template_editing(client, monkeypatch):
    """「修改」走既有 template_edit_confirm → USER_EDITING_TEMPLATE 链路。"""
    _, plan = _plan(client, monkeypatch)
    _confirm_plan(client, plan)

    payload = _post(client, "修改", message_id="modify_choice").json()

    assert payload["state"] == "USER_EDITING_TEMPLATE"
    assert payload["error"] is None


def test_free_input_cannot_bypass_the_next_step_menu(client, monkeypatch):
    """已有待确认菜单时，自由输入不能触发生成草稿。"""
    _, plan = _plan(client, monkeypatch)
    confirmed = _confirm_plan(client, plan)
    draft = confirmed["domainObjects"]["reportDefinitionDraft"]

    payload = _post(client, "再生成一版报表", message_id="free_input").json()

    assert payload["uiActions"] == ["selection_required"]
    assert payload["domainObjects"].get("reportDefinitionDraft") is None
    # 原来的草稿没有被覆盖，菜单也还在。
    detail = client.get("/api/sessions/sess_tpl_1").json()
    assert detail["snapshot"]["definition"] == draft
    assert detail["pendingChoice"]["question"]["id"] == "template_edit_confirm"


def test_readonly_question_does_not_create_draft(client, monkeypatch):
    """只读提问不生成草稿，设计阶段与方案原样保留。"""
    _, plan = _plan(client, monkeypatch)

    payload = _post(client, "当前对话是什么进度？", message_id="question").json()

    assert payload["turnKind"] == "query_answer"
    assert payload["domainObjects"].get("reportDefinitionDraft") is None
    stored = report_design_service.repository.load("sess_tpl_1")
    assert stored["definitionDraft"] is None
    assert stored["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert stored["reportPlan"]["revision"] == plan["revision"]


# --------------------------------------------------------------------------
# 3. 幂等：重复确认同一版方案复用同一份草稿
# --------------------------------------------------------------------------


def test_duplicate_confirmation_reuses_the_same_draft(client, monkeypatch):
    """重复确认同一版方案：复用同一份草稿，不产生第二份、不推高版本号。"""
    _, plan = _plan(client, monkeypatch)

    first = _confirm_plan(client, plan, "confirm_plan")
    second = _confirm_plan(client, plan, "confirm_plan_again")

    assert first["domainObjects"]["reportDefinitionDraft"] == second["domainObjects"]["reportDefinitionDraft"]
    assert second["designPhase"] == "DRAFT_READY"
    assert second["state"] == "WAITING_USER_CONFIRM"
    assert second["questions"][0]["id"] == "template_edit_confirm"
    # 模板版本号没有被推高，库里也只有一份草稿。
    stored = report_design_service.repository.load("sess_tpl_1")
    assert stored["templateDesign"]["revision"] == first["domainObjects"]["templateDesign"]["revision"]
    assert stored["definitionDraft"] == first["domainObjects"]["reportDefinitionDraft"]


def test_duplicate_confirmation_does_not_rewind_saved_draft(client, monkeypatch):
    """旧确认结果重放不覆盖后续已接受的草稿修改，也不倒退会话状态。"""
    model, plan = _plan(client, monkeypatch)
    _confirm_plan(client, plan)
    _post(client, "同意", message_id="save_draft")
    saved = client.get("/api/sessions/sess_tpl_1").json()["snapshot"]["definition"]

    repeated = _confirm_plan(client, plan, "repeat_plan")

    assert repeated["state"] == "SAVE_DEFINITION_DRAFT"
    # 回的是**当前**那份草稿（已被保存流程接受过），不是初版。
    assert repeated["domainObjects"]["reportDefinitionDraft"] == saved
    assert client.get("/api/sessions/sess_tpl_1").json()["snapshot"]["definition"] == saved
    assert len(model.calls) == 1


def test_stale_plan_confirmation_does_not_overwrite_the_newer_plan(client, monkeypatch):
    """过期方案确认不覆盖新版。"""
    _, plan = _plan(client, monkeypatch, _candidate(), _candidate(topic="修改后的方案"))
    changed = _post(client, "修改，改标题为修改后的方案", message_id="modify").json()
    assert changed["domainObjects"]["reportPlan"]["revision"] == plan["revision"] + 1

    refused = _confirm_plan(client, plan, "stale_confirm")

    assert refused["error"]["code"] == "PLAN_REVISION_CONFLICT"
    assert refused["designPhase"] == "PLAN_WAITING_CONFIRM"
    stored = report_design_service.repository.load("sess_tpl_1")
    assert stored["reportPlan"]["revision"] == plan["revision"] + 1
    assert stored["definitionDraft"] is None


# --------------------------------------------------------------------------
# 4. 事务边界：失败整体回滚
# --------------------------------------------------------------------------


def test_confirmation_transaction_failure_rolls_back_and_can_retry(client, monkeypatch):
    """中途失败不留「已成功确认但无草稿」：确认记录、草稿、菜单一并回滚。"""
    _, plan = _plan(client, monkeypatch)
    conn = session_service.repository.conn
    conn.execute("""
        CREATE TRIGGER reject_confirmation BEFORE UPDATE ON sessions
        WHEN NEW.state = 'WAITING_USER_CONFIRM'
        BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END
    """)
    conn.commit()

    refused = _confirm_plan(client, plan, "confirm_plan")

    assert refused["error"]["code"] == "TEMPLATE_COMMIT_FAILED"
    assert refused["error"]["message"]
    # 方案与阶段原样保留。
    detail = client.get("/api/sessions/sess_tpl_1").json()
    assert detail["snapshot"]["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert "definition" not in detail["snapshot"]
    assert detail["pendingChoice"] is None
    stored = report_design_service.repository.load("sess_tpl_1")
    assert stored["definitionDraft"] is None
    assert stored["templateDesign"] is None
    assert stored["designPhase"] == "PLAN_WAITING_CONFIRM"

    conn.execute("DROP TRIGGER reject_confirmation")
    conn.commit()
    # 重试仍绑定同一方案 ID / revision，并成功出草稿。
    retried = _confirm_plan(client, plan, "retry_confirmation")
    assert retried["error"] is None
    assert retried["designPhase"] == "DRAFT_READY"
    assert retried["domainObjects"]["reportDefinitionDraft"] is not None


def test_conversion_failure_leaves_no_confirmed_but_draftless_state(client, monkeypatch):
    """模板转草稿失败：确认关系、草稿、菜单一个都不落库。"""
    from app.services import report_design_service as design_module

    _, plan = _plan(client, monkeypatch)

    def boom(*args, **kwargs):
        raise ValueError("Template metric has no safe legacy execution binding")

    monkeypatch.setattr(design_module, "build_template_definition", boom)

    payload = _confirm_plan(client, plan, "confirm_plan")

    assert payload["error"]["code"] == "TEMPLATE_BUILD_FAILED"
    # 失败原因透传给用户，而不是一句「请重试」。
    assert "no safe legacy execution binding" in payload["error"]["message"]
    stored = report_design_service.repository.load("sess_tpl_1")
    assert stored["templateDesign"] is None
    assert stored["definitionDraft"] is None
    assert stored["designPhase"] == "PLAN_WAITING_CONFIRM"
    detail = client.get("/api/sessions/sess_tpl_1").json()
    assert detail["pendingChoice"] is None
    assert "definition" not in detail["snapshot"]


def test_legacy_template_confirmation_reports_the_real_reason(client, monkeypatch):
    """历史兼容入口失败时，原因也要**原样**透传，不能被通用文案盖掉。"""
    from app.services import report_design_service as design_module
    from app.services.report_template_service import build_template_design

    _, plan = _plan(client, monkeypatch)
    # 造一个历史会话：模板已落库、阶段停在 TEMPLATE_READY。
    template = build_template_design(plan, subject_id="room_a").model_dump(mode="json")
    conn = session_service.repository.conn
    conn.execute(
        "INSERT INTO report_design_templates"
        " (session_id, template_id, template_revision, plan_id, plan_revision,"
        "  data_status, template_json, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "sess_tpl_1", template["id"], template["revision"], plan["id"],
            plan["revision"], template.get("dataStatus"),
            json.dumps(template, ensure_ascii=False), "2026-10-29T00:00:00",
            "2026-10-29T00:00:00",
        ),
    )
    conn.execute(
        "UPDATE report_design_sessions SET design_phase = 'TEMPLATE_READY'"
        " WHERE session_id = ?", ("sess_tpl_1",),
    )
    conn.commit()

    def boom(*args, **kwargs):
        raise ValueError("板块 2「PUE 趋势」的组件类型非法：'radar'")

    monkeypatch.setattr(design_module, "build_template_definition", boom)

    payload = _confirm_template(client, template)

    assert payload["error"]["code"] == "TEMPLATE_CONFIRM_FAILED"
    # 原因原样送达：板块名没有被概括掉。
    assert "板块 2「PUE 趋势」的组件类型非法：'radar'" in payload["error"]["message"]


def test_invalid_component_type_in_plan_fails_with_the_real_reason(client, monkeypatch):
    """组件类型非法要**明确失败**，并把真实原因透传给用户。"""
    candidate = _candidate(sections=[{
        "id": "section_bad", "title": "PUE 总览", "metricIntents": ["PUE"],
        "componentType": "radar",
    }])
    install_model(monkeypatch, RecordingModel(candidate))

    payload = _post(client, "生成 PUE 报表").json()

    assert payload["error"] is not None
    assert payload["error"]["code"] == "MODEL_OUTPUT_INVALID"
    # 原因没有被覆盖成「请重试」这类通用文案。
    assert "componentType" in payload["error"]["message"]
    assert "请重试" not in payload["error"]["message"]
    # 方案没有落库，阶段也没推进。
    assert payload["domainObjects"]["reportPlan"] is None
    assert payload["designPhase"] != "PLAN_WAITING_CONFIRM"


def test_invalid_component_type_at_confirmation_fails_and_names_the_section(client, monkeypatch):
    """绕过方案校验的脏数据，在确认期同样要明确失败并指出板块。"""
    import json

    _, plan = _plan(client, monkeypatch)
    conn = session_service.repository.conn
    row = conn.execute(
        "SELECT plan_json FROM report_design_sessions WHERE session_id = ?",
        ("sess_tpl_1",),
    ).fetchone()
    dirty = json.loads(row[0])
    dirty["sections"][0]["componentType"] = "radar"
    conn.execute(
        "UPDATE report_design_sessions SET plan_json = ? WHERE session_id = ?",
        (json.dumps(dirty, ensure_ascii=False), "sess_tpl_1"),
    )
    conn.commit()

    payload = _confirm_plan(client, plan, "confirm_plan")

    assert payload["error"]["code"] == "PLAN_COMPONENT_TYPE_INVALID"
    assert "总耗电概览" in payload["error"]["message"]
    assert "radar" in payload["error"]["message"]
    # 既没有猜类型，也没有当成成功占位：库里没有模板，也没有草稿。
    stored = report_design_service.repository.load("sess_tpl_1")
    assert stored["templateDesign"] is None
    assert stored["definitionDraft"] is None


# --------------------------------------------------------------------------
# 5. 历史兼容入口与既有链路
# --------------------------------------------------------------------------


@pytest.mark.parametrize("context", [{"templateId": "other_session_template"}, {"baseTemplateRevision": 99}])
def test_stale_template_confirmation_preserves_the_draft(client, monkeypatch, context):
    """历史兼容入口被拒时，已确认的草稿不受影响。"""
    _, plan = _plan(client, monkeypatch)
    confirmed = _confirm_plan(client, plan)
    template = confirmed["domainObjects"]["templateDesign"]
    draft = confirmed["domainObjects"]["reportDefinitionDraft"]

    refused = _confirm_template(client, template, **context)

    assert refused["error"]["code"] == "TEMPLATE_REVISION_CONFLICT"
    detail = client.get("/api/sessions/sess_tpl_1").json()
    assert detail["snapshot"]["designPhase"] == "DRAFT_READY"
    assert detail["snapshot"]["definition"] == draft


def test_pue_placeholder_remains_text_without_metric_binding_in_preview(client, monkeypatch):
    """占位区域不编造数值。"""
    _, plan = _plan(client, monkeypatch, _pue_candidate())

    draft = _confirm_plan(client, plan)["domainObjects"]["reportDefinitionDraft"]

    region = draft["regions"][0]
    assert region["component"]["type"] == "TEXT"
    assert "口径待核实" in region["component"]["content"]["template"]
    assert "data" not in region
    assert draft["dependencies"]["metricRefs"] == []
    _post(client, "同意", message_id="save")
    for expected in ["VALIDATING", "PREVIEWING", "PREVIEW_READY"]:
        assert _post(client, "__advance__", message_id="advance_" + expected).json()["state"] == expected
    preview = client.get("/api/sessions/sess_tpl_1").json()["snapshot"]["previewPayload"]["regions"][0]
    assert preview["componentType"] == "TEXT"
    assert "口径待核实" in preview["payload"]["rendered"]
    assert "value" not in preview["payload"]


def test_confirmation_ignores_forged_snapshots_and_advance(client, monkeypatch):
    """伪造的快照与内部控制消息都不能造出草稿。"""
    _, plan = _plan(client, monkeypatch)

    body = _body("__advance__", message_id="forged_advance")
    body["state"] = "PREVIEWING"
    body["snapshots"] = {"reportDefinition": {"metadata": {"name": "forged"}}}
    payload = client.post("/api/agent/chat", json=body).json()
    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert payload["toolRequests"] == []
    assert payload["domainObjects"].get("reportDefinitionDraft") is None

    body = _body("确认", message_id="safe_confirm", context={"designAction": "CONFIRM_PLAN"})
    body["snapshots"] = {"reportDefinition": {"metadata": {"name": "forged"}}}
    payload = client.post("/api/agent/chat", json=body).json()
    assert payload["domainObjects"]["reportDefinitionDraft"]["metadata"]["name"] != "forged"


def test_changed_data_capability_stops_at_validation_failure(client, monkeypatch):
    """能力变化 → 校验失败，既有链路不受影响。"""
    from app.services import catalog_mock_service

    _, plan = _plan(client, monkeypatch)
    _confirm_plan(client, plan)
    _post(client, "同意", message_id="save")
    original = catalog_mock_service.get_catalog

    def hourly_only(source_ref):
        catalog = original(source_ref)
        return {**catalog, "supportedGrains": ["HOUR"]} if catalog else None

    monkeypatch.setattr(catalog_mock_service, "get_catalog", hourly_only)
    assert _post(client, "__advance__", message_id="validating").json()["state"] == "VALIDATING"
    failed = _post(client, "__advance__", message_id="failed").json()
    assert failed["state"] == "VALIDATION_FAILED"
    assert failed["debug"]["validation"]["passed"] is False
    assert failed["toolRequests"] == []
    assert "previewPayload" not in client.get("/api/sessions/sess_tpl_1").json()["snapshot"]


def test_stale_confirm_after_save_keeps_saved_state(client, monkeypatch):
    """保存之后再来过期的确认，状态不倒退。"""
    _, plan = _plan(client, monkeypatch)
    confirmed = _confirm_plan(client, plan)
    template = confirmed["domainObjects"]["templateDesign"]
    _post(client, "同意", message_id="save")

    failed = _confirm_template(client, template, baseTemplateRevision=99)

    assert failed["error"]["code"] == "TEMPLATE_REVISION_CONFLICT"
    assert client.get("/api/sessions/sess_tpl_1").json()["state"] == "SAVE_DEFINITION_DRAFT"


@pytest.mark.parametrize("kind", ["metric_card", "table", "line_chart", "bar_chart", "pie_chart"])
def test_device_regions_keep_device_metric_in_legacy_definition(client, monkeypatch, kind):
    """设备指标在旧定义里保住。"""
    candidate = _candidate(metrics=["设备耗电"], sections=[{
        "id": "devices", "title": "设备耗电", "metricIntents": ["设备耗电"], "componentType": kind,
    }])
    _, plan = _plan(client, monkeypatch, candidate)

    confirmed = _confirm_plan(client, plan)
    template = confirmed["domainObjects"]["templateDesign"]
    draft = confirmed["domainObjects"]["reportDefinitionDraft"]
    region = draft["regions"][0]
    assert draft["dependencies"]["metricRefs"] == [{"ref": "metric.machine_room.device_energy", "version": "1.0"}]
    if kind == "metric_card":
        metric_nodes = [node for node in region["data"]["pipeline"]["nodes"] if node.get("metricRef")]
        assert metric_nodes[0]["metricRef"] == "metric.machine_room.device_energy"
    _post(client, "同意", message_id="save_devices")
    for expected in ["VALIDATING", "PREVIEWING", "PREVIEW_READY"]:
        assert _post(client, "__advance__", message_id="device_" + expected).json()["state"] == expected
    preview = client.get("/api/sessions/sess_tpl_1").json()["snapshot"]["previewPayload"]["regions"][0]["payload"]
    original = template["regions"][0]["data"]
    if kind == "metric_card":
        assert preview["value"] == original["value"]
    elif kind == "table":
        assert [row["energyKwh"] for row in preview["rows"]] == [row["energyKwh"] for row in original["rows"]]
    else:
        assert preview["yValues"] == original["yValues"]


def test_modify_and_confirm_in_one_message_only_updates_plan(client, monkeypatch):
    """一条消息里既改又确认 → 只更新方案，不生成草稿。"""
    _, plan = _plan(client, monkeypatch, _candidate(), _candidate(topic="修改后的方案"))

    payload = _post(client, "改成设备耗电表格，确认", message_id="mixed_confirm", context={
        "designAction": "CONFIRM_PLAN", "planId": plan["id"],
        "basePlanRevision": plan["revision"],
    }).json()

    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert payload["domainObjects"]["reportPlan"]["revision"] == plan["revision"] + 1
    assert payload["domainObjects"]["reportDefinitionDraft"] is None


def test_vague_approval_keeps_explicit_confirmation_entry(client, monkeypatch):
    """「差不多了」不推进版本，给出明确确认入口。"""
    model, plan = _plan(client, monkeypatch)

    payload = _post(client, "差不多了", message_id="vague").json()

    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert payload["domainObjects"]["reportPlan"]["revision"] == plan["revision"]
    assert "plan_confirm_pending" in payload["uiActions"]
    assert payload["domainObjects"]["reportDefinitionDraft"] is None
    assert len(model.calls) == 1


def test_confirmation_rejects_other_session_scope_without_changing_draft(client, monkeypatch):
    """跨范围确认被拒且 snapshot / state 不变。"""
    _, plan = _plan(client, monkeypatch)
    _confirm_plan(client, plan)
    before = client.get("/api/sessions/sess_tpl_1").json()

    failed = _post(client, "确认模板", message_id="wrong_scope", context={
        "designAction": "CONFIRM_TEMPLATE", "subjectId": "room_b",
    }).json()

    assert failed["error"]["code"] == "SESSION_SCOPE_MISMATCH"
    after = client.get("/api/sessions/sess_tpl_1").json()
    assert after["snapshot"] == before["snapshot"]
    assert after["state"] == before["state"]


def test_reused_plan_message_id_does_not_confirm_plan(client, monkeypatch):
    """复用已处理过的消息 id 不会推进确认。"""
    _, plan = _plan(client, monkeypatch)

    refused = _post(client, "确认", message_id="msg_1", context={
        "designAction": "CONFIRM_PLAN", "planId": plan["id"],
        "basePlanRevision": plan["revision"],
    }).json()

    assert refused["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert refused["domainObjects"]["reportDefinitionDraft"] is None
    stored = report_design_service.repository.load("sess_tpl_1")
    assert stored["definitionDraft"] is None


def test_reopening_session_database_restores_confirmation_and_can_continue(client, monkeypatch, tmp_path):
    """重开数据库仍能恢复确认现场并继续。"""
    from app.storage.sqlite_store import SqliteSessionRepository

    _, plan = _plan(client, monkeypatch)
    _confirm_plan(client, plan)
    before = client.get("/api/sessions/sess_tpl_1").json()

    reopened = SqliteSessionRepository(tmp_path / "sessions.db")
    monkeypatch.setattr(session_service, "_repo", reopened)
    report_design_service.reset_for_tests()

    after = client.get("/api/sessions/sess_tpl_1").json()
    assert after["snapshot"] == before["snapshot"]
    assert after["pendingChoice"] == before["pendingChoice"]
    assert _post(client, "同意", message_id="save_restored").json()["state"] == "SAVE_DEFINITION_DRAFT"
    assert _post(client, "__advance__", message_id="advance_restored").json()["state"] == "VALIDATING"
