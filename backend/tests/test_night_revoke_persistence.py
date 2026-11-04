"""T09/T12：撤销之后，左侧历史里那一步要**持久地**变成只读 / 已撤销。

BUG-008 的后半段：撤销只回滚了右侧定义，左侧会话现场没有一起回退。

前端（包 B）已经实现消费两种「失效清单」来源：

    1. 动作响应顶层的 ``revokedStepIds``（撤销那一笔当场给）；
    2. **历史消息上的 ``payload.revoked``**（刷新之后靠它保持一致）。

第 2 种此前服务端**没有写** —— 结果是：撤销当场看起来对（徽标出现了），
**刷新之后徽标消失**，那一步又能点了。本文件锁住服务端这一半：

    - 撤销 → 会话历史里属于该步骤的消息被标成 ``revoked``；
    - 刷新（重新读会话）→ 标记还在；
    - **消息文本一个字都不删**（历史保留，只是那一步不算数了）；
    - 撤销后定义真的回滚了（这一条是前提，不是本文件的新增判据）。

本文件走真实 HTTP（``/api/agent/chat`` + ``/api/drafts/{id}/actions``），
用**隔离库**（conftest 的 autouse 夹具），**真实模型调用 0 次**。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.services.session_service import session_service


SESSION_ID = "sess_night_revoke"

#: 契约里的那句「接受修改」。
EDIT_ACCEPT = "接受修改"


def _body(message: str, *, message_id: str, definition: dict[str, Any]) -> dict[str, Any]:
    return {
        "agentProtocolVersion": "1.0",
        "conversationId": "conv_night_revoke",
        "sessionId": SESSION_ID,
        "messageId": message_id,
        "state": "USER_EDITING_TEMPLATE",
        "message": message,
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
        "snapshots": {"reportDefinition": definition},
    }


def _current_definition() -> dict[str, Any]:
    snapshot = session_service.load_snapshot(SESSION_ID)
    definition = snapshot.get("definition")
    if isinstance(definition, dict) and definition.get("regions"):
        return definition
    from app.services.report_draft_service import build_definition_draft

    return build_definition_draft("A 机房能耗日报", "room_a").model_dump(mode="json")


def _post(client, message: str, *, message_id: str):
    return client.post(
        "/api/agent/chat",
        json=_body(message, message_id=message_id, definition=_current_definition()),
    )


def _messages(client) -> list[dict[str, Any]]:
    detail = client.get(f"/api/sessions/{SESSION_ID}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    return list(body.get("messages") or [])


def _step_ids_of(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    out: list[str] = []
    for question in payload.get("questions") or []:
        if isinstance(question, dict):
            step_id = str(question.get("stepId") or "").strip()
            if step_id:
                out.append(step_id)
    return out


def _revoked_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in messages if (m.get("payload") or {}).get("revoked") is True]


def test_undo_marks_history_step_revoked_and_survives_refresh(client):
    """撤销 → 历史消息落撤销标记 → 刷新后标记还在、文本保留。"""
    # ---- 1. 真实中文入口要一个候选 ----
    candidate = _post(client, "报表名称改成新日报", message_id="m1")
    assert candidate.status_code == 200, candidate.text
    candidate_body = candidate.json()
    assert candidate_body.get("questions"), "候选没出来"

    # ---- 2. 点「接受修改」 ----
    accepted = _post(client, EDIT_ACCEPT, message_id="m2")
    assert accepted.status_code == 200, accepted.text
    accepted_body = accepted.json()
    revision_after_accept = (
        accepted_body.get("domainObjects", {}).get("reportDefinitionDraft", {})
        .get("metadata", {})
        .get("revision")
    )
    assert revision_after_accept == 1, "接受之后版本没有推进"

    draft_id = session_service.load_draft_id(SESSION_ID)
    assert draft_id, "会话没有绑草稿对象"

    # 接受之前的历史（候选轮 + 接受轮）此刻**都还没**被标撤销。
    assert not _revoked_messages(_messages(client)), "撤销之前不该有已撤销标记"

    # ---- 3. 撤销 ----
    undo = client.post(f"/api/drafts/{draft_id}/actions", json={"action": "UNDO"})
    assert undo.status_code == 200, undo.text
    undo_body = undo.json()
    revoked_step_ids = list(undo_body.get("revokedStepIds") or [])
    assert revoked_step_ids, "撤销必须给出失效的步骤清单"

    # ---- 4. 刷新之后标记还在（这一条就是本文件锁的东西） ----
    messages = _messages(client)
    revoked = _revoked_messages(messages)
    assert revoked, "撤销之后历史消息上没有 revoked 标记，刷新后徽标会消失"

    marked_step_ids = set()
    for message in revoked:
        marked_step_ids.update(_step_ids_of(message.get("payload")))
    assert marked_step_ids, "被标记的消息必须能说出它属于哪一步"
    assert marked_step_ids & set(revoked_step_ids), (
        "标记的步骤必须来自服务端给的失效清单，不能是自己猜的"
    )

    # ---- 5. 历史保留：文本一个字都不删 ----
    contents = [str(m.get("content") or "") for m in messages]
    assert any("新日报" in c or "日报" in c for c in contents), "历史文本被删了"

    # ---- 6. 撤销之后定义真的回滚了（前提） ----
    after_undo = _current_definition()
    assert after_undo.get("metadata", {}).get("name") == "A 机房能耗日报", (
        "撤销之后定义没有回滚"
    )


def _applied_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for message in messages:
        payload = message.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if "template_edit_applied" in (payload.get("uiActions") or []):
            out.append(message)
    return out


def _revoked_applied(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """被标成已撤销的**「已生效修改」**消息。

    撤销同时也会让**当前那张菜单**失效（撤销后补发的新菜单同样会被下一次
    撤销作废）—— 那是「旧菜单失效」，不是「修改单位失效」。所以这里只数
    「已生效的修改」那几条。
    """
    return [
        message
        for message in _revoked_messages(messages)
        if "template_edit_applied" in ((message.get("payload") or {}).get("uiActions") or [])
    ]


def test_consecutive_undo_invalidates_each_applied_step_and_redo_restores_last(
    client,
):
    """连续撤销 B / A → 两步都失效；重做 → **最后撤掉的那一步**恢复。"""
    assert _post(client, "报表名称改成新日报", message_id="m1").status_code == 200
    assert _post(client, EDIT_ACCEPT, message_id="m2").status_code == 200
    assert _post(client, "报表名称改成夜日报", message_id="m3").status_code == 200
    assert _post(client, EDIT_ACCEPT, message_id="m4").status_code == 200

    applied_before = _applied_messages(_messages(client))
    assert len(applied_before) == 2, "两轮「已生效」的历史消息没有都在"

    draft_id = session_service.load_draft_id(SESSION_ID)
    assert draft_id

    # ---- 撤销 B ----
    undo_b = client.post(f"/api/drafts/{draft_id}/actions", json={"action": "UNDO"})
    assert undo_b.status_code == 200, undo_b.text
    revoked_b = _revoked_applied(_messages(client))
    assert len(revoked_b) == 1, "撤销 B 之后只有一步该失效"

    # ---- 再撤销 A ----
    undo_a = client.post(f"/api/drafts/{draft_id}/actions", json={"action": "UNDO"})
    assert undo_a.status_code == 200, undo_a.text
    revoked_after_a = _revoked_applied(_messages(client))
    assert len(revoked_after_a) == 2, "连续撤销之后两步都该失效（各自一个撤销单位）"

    # 撤掉的顺序是 LIFO：撤销 B 在前、撤销 A 在后。
    revoked_ids_b = {_step_ids_of(m.get("payload"))[0] for m in revoked_b if _step_ids_of(m.get("payload"))}
    revoked_ids_a = {_step_ids_of(m.get("payload"))[0] for m in revoked_after_a if _step_ids_of(m.get("payload"))}
    assert revoked_ids_b < revoked_ids_a, "失效清单没有只增不减"

    # 撤销之后**补发了一张当前合法的下一步菜单**
    #（契约 docs/agent/state-and-undo.md 第 50 行：完整现场含「可操作菜单」）。
    detail = client.get(f"/api/sessions/{SESSION_ID}").json()
    pending = detail.get("pendingChoice") or {}
    assert (pending.get("question") or {}).get("id") == "template_edit_confirm", (
        "撤销之后必须给一张可操作的下一步菜单"
    )

    # ---- 回到基线：两次撤销之后定义不该还有前一次的修改 ----
    after_two_undo = _current_definition()
    assert after_two_undo.get("metadata", {}).get("name") == "A 机房能耗日报"

    # ---- 重做：恢复**最后撤掉的那一个**（A）----
    redo = client.post(f"/api/drafts/{draft_id}/actions", json={"action": "REDO"})
    assert redo.status_code == 200, redo.text
    revoked_after_redo = _revoked_applied(_messages(client))
    assert len(revoked_after_redo) == 1, "重做之后只该剩一步失效"
    assert {
        _step_ids_of(m.get("payload"))[0] for m in revoked_after_redo if _step_ids_of(m.get("payload"))
    } == revoked_ids_b, "重做恢复的不是最后撤掉的那一步"

    # 重做之后 A 那一步重新生效（定义回到「新日报」）。
    assert _current_definition().get("metadata", {}).get("name") == "新日报"
