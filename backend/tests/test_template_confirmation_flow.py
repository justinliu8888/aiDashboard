"""Template confirmation joins the persisted legacy report workflow."""
import pytest

from app.services.report_design_service import report_design_service
from app.services.session_service import session_service
from tests.test_report_template_generation import (
    RecordingModel, _body, _candidate, _confirm, _post, _pue_candidate, design_deps, install_model,
)


def test_confirm_template_builds_persisted_legacy_draft(client, monkeypatch):
    install_model(monkeypatch, RecordingModel(_candidate()))
    plan = _post(client, "生成 A 机房最近 7 天能耗报表").json()["domainObjects"]["reportPlan"]
    template = _confirm(client, plan, "confirm_plan")["domainObjects"]["templateDesign"]
    response = _post(client, "确认模板", message_id="confirm_template", context={
        "designAction": "CONFIRM_TEMPLATE", "templateId": template["id"],
        "baseTemplateRevision": template["revision"],
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["error"] is None
    assert payload["designPhase"] == "DRAFT_READY"
    assert payload["state"] == "WAITING_USER_CONFIRM"
    draft = payload["domainObjects"]["reportDefinitionDraft"]
    assert draft["metadata"]["name"] == template["title"]
    assert [r["id"] for r in draft["regions"]] == [r["id"] for r in template["regions"]]
    assert draft["defaults"]["timeWindow"]["count"] == 7
    detail = client.get("/api/sessions/sess_tpl_1").json()
    assert detail["snapshot"]["designPhase"] == "DRAFT_READY"
    assert detail["snapshot"]["definition"] == draft


def _template(client, monkeypatch, *candidates):
    model = install_model(monkeypatch, RecordingModel(*(candidates or [_candidate()])))
    plan = _post(client, "生成 A 机房最近 7 天能耗报表").json()["domainObjects"]["reportPlan"]
    template = _confirm(client, plan, "confirm_plan")["domainObjects"]["templateDesign"]
    return model, plan, template


def _confirm_template(client, template, **context):
    return _post(client, "确认模板", message_id="confirm_template", context={
        "designAction": "CONFIRM_TEMPLATE", "templateId": template["id"],
        "baseTemplateRevision": template["revision"], **context,
    }).json()


def test_confirmed_template_runs_legacy_validation_preview_and_publish_menu(client, monkeypatch):
    model, _, template = _template(client, monkeypatch)
    _confirm_template(client, template)
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


@pytest.mark.parametrize("message", ["确认", "确认模板", "同意", "就按这个模板继续"])
def test_text_confirmation_uses_current_template_without_calling_model(client, monkeypatch, message):
    model, _, template = _template(client, monkeypatch)
    payload = _post(client, message, message_id="text_confirm").json()
    assert payload["designPhase"] == "DRAFT_READY"
    assert payload["domainObjects"]["reportDefinitionDraft"]["metadata"]["templateBinding"]["templateId"] == template["id"]
    assert len(model.calls) == 1


def test_modify_template_continues_plan_and_old_template_cannot_be_confirmed(client, monkeypatch):
    _, plan, template = _template(client, monkeypatch, _candidate(), _candidate(topic="修改后的报表"))
    assert "confirm_template" in _confirm(client, plan, "repeat_plan")["uiActions"]
    changed = _post(client, "修改，改标题为修改后的报表", message_id="modify").json()
    assert changed["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert changed["domainObjects"]["reportPlan"]["revision"] == plan["revision"] + 1
    assert changed["domainObjects"]["reportDefinitionDraft"] is None
    refused = _confirm_template(client, template)
    assert refused["error"]["code"] == "TEMPLATE_REVISION_CONFLICT"
    assert refused["designPhase"] == "PLAN_WAITING_CONFIRM"


@pytest.mark.parametrize("context", [{"templateId": "other_session_template"}, {"baseTemplateRevision": 99}])
def test_stale_template_confirmation_preserves_template(client, monkeypatch, context):
    _, _, template = _template(client, monkeypatch)
    refused = _confirm_template(client, template, **context)
    assert refused["error"]["code"] == "TEMPLATE_REVISION_CONFLICT"
    detail = client.get("/api/sessions/sess_tpl_1").json()
    assert detail["snapshot"]["designPhase"] == "TEMPLATE_READY"
    assert "definition" not in detail["snapshot"]


def test_duplicate_confirmation_does_not_rewind_saved_draft(client, monkeypatch):
    model, _, template = _template(client, monkeypatch)
    first = _confirm_template(client, template)
    second = _confirm_template(client, template)
    assert first["domainObjects"]["reportDefinitionDraft"] == second["domainObjects"]["reportDefinitionDraft"]
    _post(client, "同意", message_id="save_draft")
    repeated = _post(client, "确认模板", message_id="repeat_template", context={
        "designAction": "CONFIRM_TEMPLATE", "templateId": template["id"],
        "baseTemplateRevision": template["revision"],
    }).json()
    assert repeated["state"] == "SAVE_DEFINITION_DRAFT"
    assert len(model.calls) == 1


def test_pue_placeholder_remains_text_without_metric_binding_in_preview(client, monkeypatch):
    _, _, template = _template(client, monkeypatch, _pue_candidate())
    draft = _confirm_template(client, template)["domainObjects"]["reportDefinitionDraft"]
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


def test_template_confirmation_ignores_forged_snapshots_and_advance(client, monkeypatch):
    _, _, template = _template(client, monkeypatch)
    body = _body("__advance__", message_id="forged_advance")
    body["state"] = "PREVIEWING"
    body["snapshots"] = {"reportDefinition": {"metadata": {"name": "forged"}}}
    payload = client.post("/api/agent/chat", json=body).json()
    assert payload["designPhase"] == "TEMPLATE_READY"
    assert payload["toolRequests"] == []
    body = _body("确认模板", message_id="safe_confirm", context={"designAction": "CONFIRM_TEMPLATE"})
    body["snapshots"] = {"reportDefinition": {"metadata": {"name": "forged"}}}
    payload = client.post("/api/agent/chat", json=body).json()
    assert payload["domainObjects"]["reportDefinitionDraft"]["metadata"]["name"] == template["title"]


def test_confirmation_preserves_latest_grid_and_rejects_pre_drag_revision(client, monkeypatch):
    _, _, template = _template(client, monkeypatch)
    placement = {"type": "GRID", "column": 2, "row": 3, "columnSpan": 10, "rowSpan": 4}
    response = client.patch("/api/sessions/sess_tpl_1/template-layout", json={
        "templateId": template["id"], "baseRevision": template["revision"],
        "items": [{"regionId": template["regions"][0]["id"], "placement": placement}],
    })
    assert response.status_code == 200
    assert _confirm_template(client, template)["error"]["code"] == "TEMPLATE_REVISION_CONFLICT"
    current = response.json()["templateDesign"]
    draft = _post(client, "确认", message_id="confirm_current").json()["domainObjects"]["reportDefinitionDraft"]
    assert draft["regions"][0]["layout"]["placement"] == placement
    assert draft["metadata"]["templateBinding"]["templateRevision"] == current["revision"]


def test_confirmation_transaction_failure_rolls_back_and_can_retry(client, monkeypatch):
    _, _, template = _template(client, monkeypatch)
    conn = session_service.repository.conn
    conn.execute("""
        CREATE TRIGGER reject_confirmation BEFORE UPDATE ON sessions
        WHEN NEW.state = 'WAITING_USER_CONFIRM'
        BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END
    """)
    conn.commit()
    refused = _confirm_template(client, template)
    assert refused["error"]["code"] == "TEMPLATE_CONFIRM_FAILED"
    assert client.get("/api/sessions/sess_tpl_1").json()["snapshot"]["designPhase"] == "TEMPLATE_READY"
    stored = report_design_service.repository.load("sess_tpl_1")
    assert stored["definitionDraft"] is None
    assert stored["designPhase"] == "TEMPLATE_READY"
    conn.execute("DROP TRIGGER reject_confirmation")
    conn.commit()
    assert _post(client, "确认", message_id="retry_confirmation").json()["designPhase"] == "DRAFT_READY"


def test_changed_data_capability_stops_at_validation_failure(client, monkeypatch):
    from app.services import catalog_mock_service

    _, _, template = _template(client, monkeypatch)
    _confirm_template(client, template)
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
    _, _, template = _template(client, monkeypatch)
    _confirm_template(client, template)
    _post(client, "同意", message_id="save")
    failed = _confirm_template(client, template, baseTemplateRevision=99)
    assert failed["error"]["code"] == "TEMPLATE_REVISION_CONFLICT"
    assert failed["state"] == "SAVE_DEFINITION_DRAFT"
    assert client.get("/api/sessions/sess_tpl_1").json()["state"] == "SAVE_DEFINITION_DRAFT"


@pytest.mark.parametrize("kind", ["metric_card", "table", "line_chart", "bar_chart", "pie_chart"])
def test_device_regions_keep_device_metric_in_legacy_definition(client, monkeypatch, kind):
    candidate = _candidate(metrics=["设备耗电"], sections=[{
        "id": "devices", "title": "设备耗电", "metricIntents": ["设备耗电"], "componentType": kind,
    }])
    _, _, template = _template(client, monkeypatch, candidate)
    draft = _confirm_template(client, template)["domainObjects"]["reportDefinitionDraft"]
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
    _, plan, template = _template(client, monkeypatch, _candidate(), _candidate(topic="修改后的方案"))
    payload = _post(client, "改成设备耗电表格，确认模板", message_id="mixed_confirm", context={
        "designAction": "CONFIRM_TEMPLATE", "templateId": template["id"],
        "baseTemplateRevision": template["revision"],
    }).json()
    assert payload["designPhase"] == "PLAN_WAITING_CONFIRM"
    assert payload["domainObjects"]["reportPlan"]["revision"] == plan["revision"] + 1
    assert payload["domainObjects"]["reportDefinitionDraft"] is None


def test_vague_template_approval_keeps_explicit_confirmation_entry(client, monkeypatch):
    model, _, template = _template(client, monkeypatch)
    payload = _post(client, "差不多了", message_id="vague").json()
    assert payload["designPhase"] == "TEMPLATE_READY"
    assert payload["domainObjects"]["templateDesign"] == template
    assert "confirm_template" in payload["uiActions"]
    assert payload["domainObjects"]["reportDefinitionDraft"] is None
    assert len(model.calls) == 1


def test_confirmation_rejects_other_session_scope_without_changing_draft(client, monkeypatch):
    _, _, template = _template(client, monkeypatch)
    _confirm_template(client, template)
    before = client.get("/api/sessions/sess_tpl_1").json()
    failed = _post(client, "确认模板", message_id="wrong_scope", context={
        "designAction": "CONFIRM_TEMPLATE", "subjectId": "room_b",
    }).json()
    assert failed["error"]["code"] == "SESSION_SCOPE_MISMATCH"
    after = client.get("/api/sessions/sess_tpl_1").json()
    assert after["snapshot"] == before["snapshot"]
    assert after["state"] == before["state"]


def test_reused_plan_message_id_does_not_confirm_template(client, monkeypatch):
    _, _, template = _template(client, monkeypatch)
    refused = _post(client, "确认模板", message_id="msg_1", context={
        "designAction": "CONFIRM_TEMPLATE", "templateId": template["id"],
        "baseTemplateRevision": template["revision"],
    }).json()
    assert refused["error"]["code"] == "TEMPLATE_OPERATION_DUPLICATE"
    assert refused["designPhase"] == "TEMPLATE_READY"
    assert refused["domainObjects"]["reportDefinitionDraft"] is None


def test_reopening_session_database_restores_confirmation_and_can_continue(client, monkeypatch, tmp_path):
    from app.storage.sqlite_store import SqliteSessionRepository

    _, _, template = _template(client, monkeypatch)
    _confirm_template(client, template)
    before = client.get("/api/sessions/sess_tpl_1").json()
    reopened = SqliteSessionRepository(tmp_path / "sessions.db")
    monkeypatch.setattr(session_service, "_repo", reopened)
    report_design_service.reset_for_tests()
    after = client.get("/api/sessions/sess_tpl_1").json()
    assert after["snapshot"] == before["snapshot"]
    assert after["pendingChoice"] == before["pendingChoice"]
    assert _post(client, "同意", message_id="save_restored").json()["state"] == "SAVE_DEFINITION_DRAFT"
    assert _post(client, "__advance__", message_id="advance_restored").json()["state"] == "VALIDATING"
