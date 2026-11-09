from app.routers.drafts import _restore_session_menu
from app.services.session_service import session_service


def test_restored_edit_menu_and_session_state_agree(client):
    session_service.repository.upsert_session(
        session_id="sess_restore_menu", state="ANALYZING", snapshot={}
    )
    _restore_session_menu(
        "sess_restore_menu", draft_id="draft_restore_menu",
        definition={"regions": []}, reply="已撤销上一步修改。",
    )
    record = session_service.repository.get_session("sess_restore_menu")
    assert record.state == "USER_EDITING_TEMPLATE"
    assert record.pendingChoice["state"] == record.state
    assert record.pendingChoice["question"]["stepDraftId"] == "draft_restore_menu"


def test_reopen_repairs_legacy_undo_scene_without_changing_content(client):
    from tests.test_draft_open_scene import _make_draft
    from app.services.draft_workspace import draft_workspace

    before = _make_draft("draft_legacy_menu", "sess_legacy_menu")
    session_service.repository.append_message(
        session_id="sess_legacy_menu", role="user", content="撤销"
    )
    _restore_session_menu(
        "sess_legacy_menu", draft_id="draft_legacy_menu",
        definition=before.definition, reply="已撤销",
    )
    session_service.save_state("sess_legacy_menu", "ANALYZING")
    draft_workspace.begin_task("draft_legacy_menu", task_id="task_orphaned_menu")
    response = client.post("/api/drafts/draft_legacy_menu/open", json={})
    assert response.status_code == 200
    assert session_service.repository.get_session("sess_legacy_menu").state == "USER_EDITING_TEMPLATE"
    after = draft_workspace.load_draft("draft_legacy_menu")
    assert after.definition == before.definition
    assert after.revision == before.revision
    assert after.activeTask is None
    assert draft_workspace.load_task("task_orphaned_menu").state == "FAILED"
