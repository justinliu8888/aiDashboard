"""Accepted edits that fail validation retain the draft and restore NEXT_ACTION."""
from __future__ import annotations

from app.services.report_draft_service import build_definition_draft
from app.services.session_service import session_service
from app.storage.sqlite_store import SqliteSessionRepository


def _failed_acceptance(post_chat):
    definition = build_definition_draft("Energy report", "room_a")
    snapshot = {"reportDefinition": definition.model_dump(mode="json")}
    candidate = post_chat(
        "\u62a5\u8868\u540d\u79f0\u6539\u6210 New report",
        state="USER_EDITING_TEMPLATE",
        snapshots=snapshot,
    ).json()
    assert candidate["domainObjects"]["pendingEditPlan"] is not None
    # A changed effective combination invalidates the candidate even at the same revision.
    record = session_service.repository.get_session("conv_test")
    stored = record.snapshot
    stored["definition"]["metadata"]["effectiveCombination"] = {
        "sourceMode": "simulated",
        "inputId": "test_input",
        "inputVersion": 2,
        "fieldMapping": [],
        "processingRules": [],
    }
    session_service.repository.upsert_session(
        session_id="conv_test", state=record.state, snapshot=stored,
    )
    accepted = post_chat(
        "\u63a5\u53d7\u4fee\u6539",
        state="USER_EDITING_TEMPLATE",
        snapshots=snapshot,
    ).json()
    assert accepted["error"]["code"] == "EDIT_VALIDATION_FAILED"
    return accepted, stored["definition"]


def _assert_next_action(menu):
    assert menu["id"] == "template_edit_confirm"
    assert menu["selectionOnly"] is True
    assert menu["lockScope"] == "input_and_menu"
    assert [option["value"] for option in menu["options"]] == [
        "\u540c\u610f", "\u4fee\u6539", "\u91cd\u65b0\u5f00\u59cb",
    ]


def test_failed_acceptance_restores_action_menu_and_keeps_previous_draft(post_chat):
    accepted, previous = _failed_acceptance(post_chat)

    assert accepted["state"] == "USER_EDITING_TEMPLATE"
    assert accepted["domainObjects"]["reportDefinitionDraft"] == previous
    assert accepted["domainObjects"]["changeSet"] is None
    assert accepted["domainObjects"]["pendingEditPlan"] is None
    assert accepted["toolRequests"] == []
    assert len(accepted["questions"]) == 1
    _assert_next_action(accepted["questions"][0])


def test_failed_acceptance_menu_survives_database_reconnect(
    post_chat, client, monkeypatch,
):
    accepted, previous = _failed_acceptance(post_chat)
    repo = session_service.repository
    path = repo.db_path
    repo.close()
    reopened = SqliteSessionRepository(path)
    monkeypatch.setattr(session_service, "_repo", reopened)
    try:
        response = client.get("/api/sessions/conv_test")
        assert response.status_code == 200
        detail = response.json()
        assert detail["state"] == "USER_EDITING_TEMPLATE"
        assert detail["snapshot"]["definition"] == previous
        assert detail["pendingEditPlan"] is None
        assert detail["pendingChoice"] is not None
        assert detail["pendingChoice"]["state"] == accepted["state"]
        _assert_next_action(detail["pendingChoice"]["question"])
        assert detail["pendingChoice"]["question"] == accepted["questions"][0]

        # Answering the restored menu must unlock a usable editing path.
        resumed = post_chat(
            "\u4fee\u6539", state=detail["state"],
            snapshots={"reportDefinition": detail["snapshot"]["definition"]},
        ).json()
        assert "selection_required" not in resumed["uiActions"]
        assert resumed["domainObjects"]["reportDefinitionDraft"] == previous
        restored = client.get("/api/sessions/conv_test").json()
        assert restored["pendingChoice"] is None
    finally:
        reopened.close()
