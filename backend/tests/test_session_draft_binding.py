"""R04：另存为之后，旧对象**不许**污染当前会话（契约 0002 §6.4）。

第三轮反馈指出的缺口：``draft_action`` 按**旧对象自己的** sessionId 无条件
把定义写回会话，没有核对这条会话当前绑的是哪个 draftId。另存为之后会话
已经切到新对象 B，旧对象 A 再被撤销 / 重做，就会造出「新 draftId + 旧定义」
的混合会话。

这一组用例：
    - 用**隔离库**（临时 SQLite），绝不碰用户库；
    - 走真实的 ``POST /api/drafts/{id}/actions``（不桩掉会话同步）；
    - 断言落到 **B 的身份 / 定义 / 会话快照**三者一致上，不拿 200 当通过；
    - 故障注入只写在测试里，不改生产代码加测试开关；
    - **真实模型对话请求 0 次**（走 ``FakeReportAgent`` 的确定性规则路径）。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.routers.drafts import _may_sync_session_snapshot
from app.services.draft_workspace import draft_workspace
from app.services.report_draft_service import build_definition_draft
from app.services.session_service import session_service
from app.storage.sqlite_store import SqliteSessionRepository


#: 用户库：只用来**核对它没被改动**，永远不写。
USER_DB = Path(__file__).resolve().parents[1] / "data" / "haewu.db"

SESSION_ID = "sess_binding"

#: 契约里的那句「接受修改」（`agent_service.EDIT_ACCEPT`）。
EDIT_ACCEPT = "接受修改"


# ---------- 夹具 ----------

@pytest.fixture(scope="session", autouse=True)
def user_database_untouched():
    """整场用例都不许碰用户库（改 mtime / size 都算碰过）。"""

    def _stat() -> tuple[int, int] | None:
        if not USER_DB.exists():
            return None
        info = USER_DB.stat()
        return (info.st_size, info.st_mtime_ns)

    before = _stat()
    yield
    assert _stat() == before, "用户库被这组用例改动了"


@pytest.fixture()
def probe_env():
    """临时库 + 临时替换 ``session_service._repo``（用完还原）。

    草稿工作区**跟着会话库走**，换掉会话仓库就等于把整套草稿表换到临时库。
    """
    tmp_dir = tempfile.mkdtemp(prefix="haewu-binding-")
    db_path = Path(tmp_dir) / "binding.db"
    repo = SqliteSessionRepository(db_path)
    previous_repo = session_service._repo
    session_service._repo = repo
    try:
        yield repo
    finally:
        # 收尾核对：草稿工作区必须还指着临时库（**不许指回用户库**）。
        assert draft_workspace.repo.db_path == db_path, "草稿工作区指回了用户库"
        session_service._repo = previous_repo
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------- 请求助手 ----------

def _current_definition() -> dict[str, Any]:
    """当前会话快照里的定义（下一轮请求要带回去的那一份）。"""
    snapshot = session_service.load_snapshot(SESSION_ID)
    definition = snapshot.get("definition")
    if isinstance(definition, dict) and definition.get("regions"):
        return definition
    return build_definition_draft("A 机房能耗日报", "room_a").model_dump(mode="json")


def _body(message: str, *, message_id: str) -> dict[str, Any]:
    return {
        "agentProtocolVersion": "1.0",
        "conversationId": "conv_binding",
        "sessionId": SESSION_ID,
        "messageId": message_id,
        "state": "USER_EDITING_TEMPLATE",
        "message": message,
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
        "snapshots": {"reportDefinition": _current_definition()},
    }


def _post(client: TestClient, message: str, *, message_id: str):
    return client.post(
        "/api/agent/chat", json=_body(message, message_id=message_id)
    )


def _name_of(state: Any) -> str:
    return str((state.definition or {}).get("metadata", {}).get("name") or "")


# ---------- 判据本身（纯函数） ----------

def test_may_sync_session_snapshot_judgement():
    """会话绑的是不是**本次操作的那个对象** —— 判据就是一个纯函数。"""
    # 会话没绑（历史 / 异常）→ 照旧同步（既有语义不变）。
    assert _may_sync_session_snapshot(None, "draft_a") is True
    assert _may_sync_session_snapshot("", "draft_a") is True
    # 绑的就是本次操作的对象 → 照旧同步。
    assert _may_sync_session_snapshot("draft_a", "draft_a") is True
    # 绑的是别的对象（另存为之后）→ **不许**把本对象的定义写进去。
    assert _may_sync_session_snapshot("draft_b", "draft_a") is False


# ---------- 主用例：另存为之后，旧对象的撤销 / 重做不许污染会话 ----------

def test_undo_on_old_object_after_save_as_does_not_overwrite_the_session(
    probe_env, client
):
    """A → 另存为 B → 编辑 B → 对**旧 A** 撤销 / 重做：B 的身份、定义、会话三者一致。

    旧 A 仍可作为**独立对象**做自己的撤销 / 重做（现有权限口径：对象没被
    锁住就能撤销），但**不许**把 A 的定义写进已经属于 B 的会话。
    """
    # ---- 建 A（两次修改，撤销游标停在第 2 个单位）----
    assert _post(client, "报表名称改成新日报", message_id="m1").status_code == 200
    assert _post(client, EDIT_ACCEPT, message_id="m2").status_code == 200
    draft_a = session_service.load_draft_id(SESSION_ID)
    assert draft_a
    assert _post(client, "报表名称改成夜日报", message_id="m3").status_code == 200
    assert _post(client, EDIT_ACCEPT, message_id="m4").status_code == 200
    state_a = draft_workspace.load_draft(draft_a)
    assert state_a is not None
    assert _name_of(state_a) == "夜日报"

    # ---- 另存为 B：会话切到 B ----
    saved = client.post(
        f"/api/drafts/{draft_a}/actions", json={"action": "SAVE_AS", "name": "副本"}
    )
    assert saved.status_code == 200, saved.text
    draft_b = str(saved.json()["draftId"])
    assert draft_b and draft_b != draft_a
    assert saved.json()["previousDraftId"] == draft_a
    assert session_service.load_draft_id(SESSION_ID) == draft_b

    # ---- 编辑 B：B 的定义变了 ----
    assert _post(client, "报表名称改成周日报", message_id="m5").status_code == 200
    accepted = _post(client, EDIT_ACCEPT, message_id="m6")
    assert accepted.status_code == 200, accepted.text
    state_b = draft_workspace.load_draft(draft_b)
    assert state_b is not None
    assert _name_of(state_b) == "周日报"
    assert session_service.load_draft_id(SESSION_ID) == draft_b
    snapshot_before = session_service.load_snapshot(SESSION_ID)
    assert snapshot_before["definition"]["metadata"]["name"] == "周日报"

    # ---- 对**旧 A** 执行撤销 ----
    undo = client.post(f"/api/drafts/{draft_a}/actions", json={"action": "UNDO"})
    assert undo.status_code == 200, undo.text

    # B 的**身份、定义、会话快照**三者一致：没有被旧定义覆盖。
    assert session_service.load_draft_id(SESSION_ID) == draft_b
    after_b = draft_workspace.load_draft(draft_b)
    assert after_b is not None
    assert after_b.revision == state_b.revision, "B 的版本被旧对象的撤销改了"
    assert after_b.definition == state_b.definition, "B 的定义被旧对象改了"
    after_snapshot = session_service.load_snapshot(SESSION_ID)
    assert after_snapshot["definition"] == snapshot_before["definition"], (
        "会话快照被旧对象的定义覆盖了（新 draftId + 旧定义的混合会话）"
    )

    # 旧 A 仍可作为**独立对象**撤销（现有权限口径：没被锁住就能撤销）。
    after_a = draft_workspace.load_draft(draft_a)
    assert after_a is not None
    assert after_a.revision > state_a.revision, "旧对象的撤销没有独立生效"
    assert _name_of(after_a) == "新日报"

    # ---- 对**旧 A** 执行重做 ----
    redo = client.post(f"/api/drafts/{draft_a}/actions", json={"action": "REDO"})
    assert redo.status_code == 200, redo.text

    # 仍然：B 的身份、定义、会话快照三者一致。
    assert session_service.load_draft_id(SESSION_ID) == draft_b
    after_b2 = draft_workspace.load_draft(draft_b)
    assert after_b2 is not None
    assert after_b2.revision == state_b.revision
    assert after_b2.definition == state_b.definition
    after_snapshot2 = session_service.load_snapshot(SESSION_ID)
    assert after_snapshot2["definition"] == snapshot_before["definition"]

    # 旧 A 自己的重做照旧生效（独立对象）。
    after_a2 = draft_workspace.load_draft(draft_a)
    assert after_a2 is not None
    assert after_a2.revision > after_a.revision
    assert _name_of(after_a2) == "夜日报"
