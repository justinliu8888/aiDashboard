"""打开草稿 = 切到那份草稿的**现场会话**。

「现场」= 这份草稿现在停在哪一步：它的对话、待决菜单、状态。
此前打开草稿的做法是给草稿**另开一条空会话**并把草稿搬过去 —— 那条空
会话没有对话（连会话列表里都不出现），打开后左边一片空白，而真正有
对话的那条会话被落在了后面。

契约 0002 §6.3：「重开恢复同一游标与菜单」。
"""
from __future__ import annotations

from app.services.draft_workspace import draft_workspace
from app.services.session_service import session_service

QUESTION = {
    "id": "template_edit_confirm",
    "prompt": "请选择下一步操作：",
    "options": [
        {"value": "同意", "label": "同意"},
        {"value": "修改", "label": "修改"},
        {"value": "重新开始", "label": "重新开始"},
    ],
}


def _make_draft(draft_id="draft_scene", session_id="sess_scene", snapshot=None):
    definition = {"metadata": {"name": "机房日报", "revision": 1}, "regions": []}
    session_service.repository.upsert_session(
        session_id=session_id,
        state="ANALYZING",
        snapshot={"draftId": draft_id, "definition": definition},
    )
    return draft_workspace.create_draft(
        draft_id,
        session_id=session_id,
        name="机房日报",
        definition=definition,
        snapshot=snapshot,
    )


def _make_target_session(session_id="sess_target"):
    session_service.repository.upsert_session(
        session_id=session_id, state="ANALYZING", snapshot={}
    )
    return session_id


def test_open_switches_to_the_session_that_has_the_dialog(client):
    """打开草稿要回到**有对话**的那条会话 —— 空会话不是现场。

    打开草稿的旧做法是给草稿另开一条空会话并把草稿搬过去：那条空会话
    没有对话，打开后左边一片空白，而真正有对话的那条会话被落在后面。
    """
    _make_draft(draft_id="draft_scene2", session_id="sess_with_dialog")
    session_service.repository.append_message(
        session_id="sess_with_dialog", role="user", content="生成 A 机房日报"
    )
    # 打开草稿的旧做法留下的空会话：它也指向这份草稿，但不是现场。
    session_service.repository.upsert_session(
        session_id="sess_empty", state="ANALYZING",
        snapshot={"draftId": "draft_scene2"},
    )

    # **没指定会话**：服务端切到那份草稿的现场会话。
    response = client.post("/api/drafts/draft_scene2/open", json={})

    assert response.status_code == 200, response.text
    assert response.json()["sessionId"] == "sess_with_dialog"
    assert response.json()["sceneSessionId"] == "sess_with_dialog"
    # 草稿也重新绑回那条会话（旧的绑定是被空会话顶掉的）。
    assert session_service.load_draft_id("sess_with_dialog") == "draft_scene2"


def test_open_respects_an_explicitly_requested_session(client):
    """调用方**指定**了会话就照它来 —— 它要在这条会话上打开这份草稿。"""
    _make_draft(draft_id="draft_scene4", session_id="sess_with_dialog2")
    session_service.repository.append_message(
        session_id="sess_with_dialog2", role="user", content="生成 A 机房日报"
    )
    _make_target_session("sess_requested")

    response = client.post(
        "/api/drafts/draft_scene4/open", json={"sessionId": "sess_requested"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["sessionId"] == "sess_requested"
    assert session_service.load_draft_id("sess_requested") == "draft_scene4"


def test_open_writes_the_draft_scene_into_an_empty_session(client):
    """现场会话是**空会话**时：草稿的现场（状态 + 待决菜单）写进去。"""
    _make_draft(
        draft_id="draft_empty_scene",
        session_id="sess_empty_scene",
        snapshot={
            "businessState": "WAITING_USER_CONFIRM",
            "prompt": "同意",
            "pendingChoice": {"state": "WAITING_USER_CONFIRM", "question": QUESTION},
        },
    )

    response = client.post(
        "/api/drafts/draft_empty_scene/open", json={"sessionId": "sess_empty_scene"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["sessionId"] == "sess_empty_scene"
    record = session_service.repository.get_session("sess_empty_scene")
    assert record.state == "WAITING_USER_CONFIRM"
    assert record.pendingChoice["question"]["id"] == "template_edit_confirm"


def test_open_clears_the_previous_menu_on_an_empty_session(client):
    """草稿没有待决菜单：空会话上旧的菜单要清掉。"""
    _make_draft(draft_id="draft_no_menu", session_id="sess_no_menu")
    session_service.save_pending_choice(
        "sess_no_menu", {"state": "WAITING_USER_CONFIRM", "question": QUESTION}
    )

    response = client.post(
        "/api/drafts/draft_no_menu/open", json={"sessionId": "sess_no_menu"}
    )

    assert response.status_code == 200, response.text
    record = session_service.repository.get_session("sess_no_menu")
    assert record.pendingChoice is None


def test_open_does_not_rewrite_a_session_that_already_has_a_dialog(client):
    """已经有对话的那条会话：状态与菜单是**它自己的**，不许被草稿快照顶掉。"""
    _make_draft(
        draft_id="draft_scene3",
        session_id="sess_own_state",
        snapshot={
            "businessState": "WAITING_USER_CONFIRM",
            "pendingChoice": {"state": "WAITING_USER_CONFIRM", "question": QUESTION},
        },
    )
    session_service.repository.append_message(
        session_id="sess_own_state", role="user", content="继续"
    )
    session_service.repository.upsert_session(
        session_id="sess_own_state", state="ANALYZING",
        snapshot={"draftId": "draft_scene3"},
    )
    session_service.save_pending_choice(
        "sess_own_state", {"state": "ANALYZING", "question": {"id": "own_menu"}}
    )

    response = client.post(
        "/api/drafts/draft_scene3/open", json={"sessionId": "sess_own_state"}
    )

    assert response.status_code == 200, response.text
    record = session_service.repository.get_session("sess_own_state")
    assert record.state == "ANALYZING"
    assert record.pendingChoice["question"]["id"] == "own_menu"


def test_open_does_not_change_the_draft_object(client):
    """切会话不该动草稿对象本身：它还是原来那份内容与版本。"""
    before = _make_draft(
        draft_id="draft_untouched",
        snapshot={"businessState": "WAITING_USER_CONFIRM"},
    )
    _make_target_session("sess_target3")

    response = client.post("/api/drafts/draft_untouched/open", json={"sessionId": "sess_target3"})

    assert response.status_code == 200, response.text
    after = draft_workspace.load_draft("draft_untouched")
    assert after.revision == before.revision
    assert after.definition == before.definition
    assert after.businessState == "WAITING_USER_CONFIRM"


def test_agree_then_open_goes_back_to_the_conversation(client, monkeypatch):
    """整条链：同意 → 建出草稿（绑在对话会话上）→ 打开草稿 → 回到那条对话会话。"""
    from tests.test_report_template_generation import _body, _candidate
    from tests.test_template_confirmation_flow import _confirm_plan, _plan

    _, plan = _plan(client, monkeypatch, _candidate(), _candidate(topic="新报表"))
    ready = _confirm_plan(client, plan)
    body = _body("同意", message_id="agree_open")
    body["state"] = ready["state"]
    body["snapshots"] = {
        "reportDefinition": ready["domainObjects"]["reportDefinitionDraft"],
        "reportPolicy": ready["domainObjects"]["reportPolicyDraft"],
    }
    agreed = client.post("/api/agent/chat", json=body).json()
    draft_id = agreed["domainObjects"]["draftWorkspace"]["draftId"]
    # 同意那一轮把草稿建在**对话会话**上（sess_tpl_1）。
    assert session_service.load_draft_id("sess_tpl_1") == draft_id

    # 打开它（没指定会话）：要回到产生它的那条对话会话。
    _make_target_session("sess_elsewhere")
    response = client.post(f"/api/drafts/{draft_id}/open", json={})

    assert response.status_code == 200, response.text
    assert response.json()["sessionId"] == "sess_tpl_1"
    assert session_service.load_draft_id("sess_tpl_1") == draft_id
