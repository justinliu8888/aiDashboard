"""「同意」之后弹保存草稿对话框：草稿名 + 备注真正落库。

覆盖三件事：
    1. 同意那一轮**真的建出草稿对象**（此前没有 —— 前端拿不到 draftId）；
    2. 保存动作能带上**备注**，落到 ``draft_objects.description``；
    3. 备注**不进草稿列表**（契约 0002 §6.2：列表只有 id / 名字 / 时间）。
"""
from __future__ import annotations

from app.services.session_service import session_service
from tests.test_report_template_generation import _body, _candidate
from tests.test_template_confirmation_flow import _confirm_plan, _plan


def _agree(client, monkeypatch, message_id: str = "agree_remark"):
    """跑到「同意」这一步，返回同意那一轮的响应。"""
    _, plan = _plan(client, monkeypatch, _candidate(), _candidate(topic="新报表"))
    ready = _confirm_plan(client, plan)
    body = _body("同意", message_id=message_id)
    body["state"] = ready["state"]
    body["snapshots"] = {
        "reportDefinition": ready["domainObjects"]["reportDefinitionDraft"],
        "reportPolicy": ready["domainObjects"]["reportPolicyDraft"],
    }
    response = client.post("/api/agent/chat", json=body).json()
    return ready, response


def test_agree_creates_draft_object(client, monkeypatch):
    """同意之后草稿对象要真的存在，前端才有 draftId 可以保存。"""
    _, agreed = _agree(client, monkeypatch)

    workspace = agreed["domainObjects"]["draftWorkspace"]
    assert workspace is not None
    assert workspace["draftId"]
    assert workspace["revision"] == 1
    # 草稿对象也进了会话：右侧面板的「保存草稿」按钮靠它。
    assert session_service.load_draft_id("sess_tpl_1") == workspace["draftId"]
    # 草稿列表里能看到这一份。
    listed = client.get("/api/drafts").json()
    assert [row["draftId"] for row in listed] == [workspace["draftId"]]


def test_save_persists_name_and_remark(client, monkeypatch):
    """保存动作带上草稿名与备注，两者都要落库。"""
    _, agreed = _agree(client, monkeypatch)
    draft_id = agreed["domainObjects"]["draftWorkspace"]["draftId"]

    saved = client.post(
        f"/api/drafts/{draft_id}/actions",
        json={"action": "SAVE", "name": "机房日报", "remark": "给领导看的那份"},
    )

    assert saved.status_code == 200
    detail = client.get(f"/api/drafts/{draft_id}").json()
    assert detail["name"] == "机房日报"
    assert detail["description"] == "给领导看的那份"
    assert detail["savedRevision"] is not None


def test_save_without_remark_keeps_existing_remark(client, monkeypatch):
    """没带备注就保存：原备注**不能被抹掉**。"""
    _, agreed = _agree(client, monkeypatch)
    draft_id = agreed["domainObjects"]["draftWorkspace"]["draftId"]
    client.post(
        f"/api/drafts/{draft_id}/actions",
        json={"action": "SAVE", "name": "机房日报", "remark": "第一版备注"},
    )

    client.post(
        f"/api/drafts/{draft_id}/actions",
        json={"action": "SAVE", "name": "机房日报"},
    )

    detail = client.get(f"/api/drafts/{draft_id}").json()
    assert detail["description"] == "第一版备注"


def test_remark_is_not_in_draft_list(client, monkeypatch):
    """备注是**详情字段**：草稿列表仍然只有 id / 名字 / 时间（契约 0002 §6.2）。"""
    _, agreed = _agree(client, monkeypatch)
    draft_id = agreed["domainObjects"]["draftWorkspace"]["draftId"]
    client.post(
        f"/api/drafts/{draft_id}/actions",
        json={"action": "SAVE", "name": "机房日报", "remark": "给领导看的那份"},
    )

    listed = client.get("/api/drafts").json()
    assert listed
    for row in listed:
        assert set(row) == {"draftId", "name", "updatedAt"}
    assert listed[0]["name"] == "机房日报"


def test_remark_too_long_is_rejected(client, monkeypatch):
    """备注超长要挡下来，不能把库撑坏。"""
    _, agreed = _agree(client, monkeypatch)
    draft_id = agreed["domainObjects"]["draftWorkspace"]["draftId"]

    response = client.post(
        f"/api/drafts/{draft_id}/actions",
        json={"action": "SAVE", "name": "机房日报", "remark": "长" * 501},
    )

    assert response.status_code == 422


def test_save_as_carries_remark(client, monkeypatch):
    """另存为：给了备注就用它，没给就沿用源草稿的备注。"""
    _, agreed = _agree(client, monkeypatch)
    draft_id = agreed["domainObjects"]["draftWorkspace"]["draftId"]
    client.post(
        f"/api/drafts/{draft_id}/actions",
        json={"action": "SAVE", "name": "机房日报", "remark": "源草稿备注"},
    )

    copied = client.post(
        f"/api/drafts/{draft_id}/actions",
        json={"action": "SAVE_AS", "name": "机房日报副本"},
    )
    assert copied.status_code == 200
    new_draft_id = copied.json()["draftId"]
    assert new_draft_id != draft_id
    assert client.get(f"/api/drafts/{new_draft_id}").json()["description"] == "源草稿备注"


def test_readonly_question_does_not_create_draft(client, monkeypatch):
    """只读问答**不许**凭空建草稿。"""
    from tests.test_template_confirmation_flow import _post

    _plan(client, monkeypatch)
    _post(client, "当前对话是什么进度？", message_id="question")

    assert client.get("/api/drafts").json() == []
    assert session_service.load_draft_id("sess_tpl_1") is None
