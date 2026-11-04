"""Focused draft naming, opening and safe deletion checks in the temporary DB."""

from app.services.draft_workspace import draft_workspace
from app.services.session_service import session_service


def make_draft(draft_id="draft_manage", session_id="sess_manage"):
    definition = {"metadata": {"name": "Original", "revision": 1}, "regions": []}
    session_service.repository.upsert_session(
        session_id=session_id, state="ANALYZING",
        snapshot={"draftId": draft_id, "definition": definition},
    )
    return draft_workspace.create_draft(
        draft_id, session_id=session_id, name="Original", definition=definition,
    )


def test_save_persists_trimmed_name(client):
    make_draft()
    response = client.post("/api/drafts/draft_manage/actions", json={
        "action": "SAVE", "name": "  Named report  ", "expectedRevision": 1,
    })
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Named report"
    assert response.json()["definition"]["metadata"]["name"] == "Named report"
    assert client.get("/api/drafts").json()[0]["name"] == "Named report"
    assert draft_workspace.load_draft("draft_manage").revision == 1
    opened = client.post("/api/drafts/draft_manage/open", json={"sessionId": "sess_reopen"})
    assert opened.status_code == 200
    assert opened.json()["definition"]["metadata"]["name"] == "Named report"


def test_blank_save_name_rejected_without_writing(client):
    before = make_draft()
    response = client.post("/api/drafts/draft_manage/actions", json={
        "action": "SAVE", "name": "   ", "expectedRevision": 1,
    })
    assert response.status_code == 422
    assert draft_workspace.load_draft("draft_manage").to_dict() == before.to_dict()


def test_open_binds_new_session(client):
    make_draft()
    response = client.post("/api/drafts/draft_manage/open", json={"sessionId": "sess_new"})
    assert response.status_code == 200, response.text
    assert response.json()["draftId"] == "draft_manage"
    assert session_service.load_draft_id("sess_new") == "draft_manage"
    assert session_service.load_snapshot("sess_new")["definition"]["metadata"]["name"] == "Original"


def test_delete_removes_draft_and_detaches_session_but_keeps_history(client):
    make_draft()
    session_service.repository.append_message(
        session_id="sess_manage", role="user", content="Keep this conversation",
    )
    response = client.delete("/api/drafts/draft_manage")
    assert response.status_code == 204, response.text
    assert draft_workspace.load_draft("draft_manage") is None
    assert session_service.load_draft_id("sess_manage") is None
    assert len(session_service.repository.list_messages("sess_manage")) == 1
    assert client.get("/api/drafts").json() == []
    assert client.delete("/api/drafts/draft_manage").status_code == 404


def test_delete_locked_draft_rejected(client):
    make_draft()
    assert draft_workspace.acquire("draft_manage", "other-window")
    response = client.delete("/api/drafts/draft_manage")
    assert response.status_code == 409
    assert draft_workspace.load_draft("draft_manage") is not None
    assert draft_workspace.lock_owner("draft_manage") == "other-window"


def test_save_as_names_copy_without_changing_source(client):
    before = make_draft()
    response = client.post("/api/drafts/draft_manage/actions", json={
        "action": "SAVE_AS", "name": "Copy", "expectedRevision": 1,
    })
    assert response.status_code == 200, response.text
    assert response.json()["definition"]["metadata"]["name"] == "Copy"
    assert draft_workspace.load_draft("draft_manage").to_dict() == before.to_dict()


def test_delete_failure_rolls_back_session_binding(client, monkeypatch):
    make_draft()

    def fail_delete(_draft_id):
        raise RuntimeError("Injected delete failure")

    monkeypatch.setattr(draft_workspace.adapter, "delete_draft_object", fail_delete)
    response = client.delete("/api/drafts/draft_manage")
    assert response.status_code == 500
    assert session_service.load_draft_id("sess_manage") == "draft_manage"
    assert draft_workspace.load_draft("draft_manage") is not None
