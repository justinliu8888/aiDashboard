"""本轮原子的**专项**用例：R03（栅栏原子性）与 R04（撤销 / 重做后的会话同步）。

这一组打的是**真实路由 + 真实 agent_service 接受分支**，不是底层方法：
    - 「接受修改」走真的 ``agent_service`` 接受路径（消息正文就是契约里的
      那句），不用桩函数冒充 —— 桩函数**不会**消费待确认计划，
      正是那个缺口藏身的地方；
    - 故障注入只写在测试里（monkeypatch），**不改生产代码加测试开关**；
    - 断言落到**候选现场 / 会话消息与快照 / 定义内容 / 版本号 / 修改历史 /
      任务状态**上，不拿 200 当通过；
    - 只用临时 SQLite，**绝不碰** ``backend/data/haewu.db``；
    - **不触发任何真实模型调用**：走 ``FakeReportAgent`` 的确定性路径。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services.draft_workspace import draft_workspace
from app.services.report_draft_service import build_definition_draft
from app.services.session_service import session_service
from app.storage.sqlite_store import SqliteSessionRepository


#: 用户库：只用来**核对它没被改动**，永远不写。
USER_DB = Path(__file__).resolve().parents[1] / "data" / "haewu.db"

SESSION_ID = "sess_atomic"

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
    tmp_dir = tempfile.mkdtemp(prefix="haewu-atomic-")
    db_path = Path(tmp_dir) / "atomic.db"
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
        "conversationId": "conv_atomic",
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


def _units(draft_id: str) -> list[dict[str, Any]]:
    return [
        row
        for row in draft_workspace.adapter.list_draft_versions(draft_id)
        if row["kind"] == "edit"
    ]


def _history(draft_id: str) -> list[str]:
    return [row["action"] for row in draft_workspace.adapter.list_history(draft_id)]


def _versions(draft_id: str) -> list[dict[str, Any]]:
    """草稿的全部版本行（含每版的定义与现场快照），逐字比。"""
    return list(draft_workspace.adapter.list_draft_versions(draft_id))


def _message_count() -> int:
    return len(session_service.repository.list_messages(SESSION_ID))


def _messages() -> list[dict[str, Any]]:
    """这条会话的**全部消息行**（逐字比，不只是数数）。"""
    return [
        {
            "seq": row.seq,
            "role": row.role,
            "content": row.content,
            "payload": row.payload,
            "clientMsgId": row.clientMsgId,
        }
        for row in session_service.repository.list_messages(SESSION_ID)
    ]


def _draft_with_two_edits(client: TestClient) -> str:
    """走真实接受路径，落一个**两次修改**的草稿，返回 draftId。

    版本行 = 基线 1 条 + 两次接受各 1 条（共 3 条）；
    撤销游标停在第 2 个单位（内容 = 「新日报」）。
    """
    assert _post(client, "报表名称改成新日报", message_id="m1").status_code == 200
    accepted = _post(client, EDIT_ACCEPT, message_id="m2")
    assert accepted.status_code == 200, accepted.text
    draft_id = session_service.load_draft_id(SESSION_ID)
    assert draft_id
    assert _post(client, "报表名称改成夜日报", message_id="m3").status_code == 200
    accepted2 = _post(client, EDIT_ACCEPT, message_id="m4")
    assert accepted2.status_code == 200, accepted2.text
    state = draft_workspace.load_draft(draft_id)
    assert state is not None
    # 基线 1 条 + 两次接受 2 条。
    assert len(_units(draft_id)) == 3
    assert state.undoPointer == 2
    assert state.definition["metadata"]["name"] == "夜日报"
    return draft_id


# ---------- R03-1：真实接受路径消费候选后，工作区写失败 ----------

def test_acceptance_workspace_failure_keeps_everything_and_allows_retry(
    probe_env, client, monkeypatch
):
    """真实接受路径消费掉候选后写工作区失败：候选、会话、草稿、任务现场全部原样，且能重试。"""
    # A 轮：真实 agent_service 路径提候选。
    assert _post(client, "报表名称改成新日报", message_id="m1").status_code == 200
    pending_a = session_service.load_pending_plan(SESSION_ID)
    assert pending_a is not None, "候选计划没落库"
    assert session_service.load_pending_choice(SESSION_ID) is not None

    # 接受 A → 建基线草稿。
    accepted = _post(client, EDIT_ACCEPT, message_id="m2")
    assert accepted.status_code == 200, accepted.text
    draft_id = session_service.load_draft_id(SESSION_ID)
    assert draft_id
    base = draft_workspace.load_draft(draft_id)
    assert base is not None and base.revision == 2

    # B 轮：再提一个候选（这一份就是要被保住的那份）。
    assert _post(client, "报表名称改成夜日报", message_id="m3").status_code == 200
    pending_b = session_service.load_pending_plan(SESSION_ID)
    assert pending_b is not None and pending_b.planId != pending_a.planId
    choice_b = session_service.load_pending_choice(SESSION_ID)
    assert choice_b is not None

    # 注入**确定性**故障：这一轮写工作区时抛异常（注入只写在测试里）。
    def boom(*_args, **_kwargs):
        raise RuntimeError("workspace write failed")

    original_commit = draft_workspace.commit_edit
    monkeypatch.setattr(draft_workspace, "commit_edit", boom)

    before_messages = _message_count()
    before_snapshot = session_service.load_snapshot(SESSION_ID)
    before_state = draft_workspace.load_draft(draft_id)
    before_history = _history(draft_id)
    assert before_state is not None

    failed = _post(client, EDIT_ACCEPT, message_id="m4")
    assert failed.status_code >= 500, "落库失败不许返回成功 final"
    # 只还原这一个补丁：不能用 monkeypatch.undo()（它会把共享夹具里换库的
    # 补丁一起撤掉，工作区就指回用户库了）。
    monkeypatch.setattr(draft_workspace, "commit_edit", original_commit)

    # ---- 候选现场原样：真实接受路径已经消费过它们，这里必须放回来 ----
    after_plan = session_service.load_pending_plan(SESSION_ID)
    assert after_plan is not None, "待确认计划被吃掉了，用户没法重试这一轮"
    assert after_plan.planId == pending_b.planId
    assert session_service.load_pending_choice(SESSION_ID) == choice_b

    # ---- 会话原样：消息没多、快照没变 ----
    assert _message_count() == before_messages
    assert session_service.load_snapshot(SESSION_ID) == before_snapshot

    # ---- 草稿原样：版本、内容、修改历史 ----
    after_state = draft_workspace.load_draft(draft_id)
    assert after_state is not None
    assert after_state.revision == before_state.revision
    assert after_state.definition == before_state.definition
    assert after_state.undoPointer == before_state.undoPointer
    assert _history(draft_id) == before_history

    # ---- 任务现场：没有「提交了一半」的痕迹 ----
    task = draft_workspace.load_task("task_m4")
    assert task is not None
    assert task.state != "COMPLETED", "任务标成完成了，但这一轮没提交"
    assert "committedRevision" not in (task.checkpoint or {})

    # ---- 可以重试：同一轮重发，正常落库 ----
    retried = _post(client, EDIT_ACCEPT, message_id="m4")
    assert retried.status_code == 200, retried.text
    final_state = draft_workspace.load_draft(draft_id)
    assert final_state is not None
    assert final_state.revision == before_state.revision + 1
    assert final_state.definition["metadata"]["name"] == "夜日报"
    assert _history(draft_id) == before_history + ["commit"]
    assert draft_workspace.load_task("task_m4").state == "COMPLETED"


# ---------- R03-2：STOPPED 迟到拒绝 ----------

def test_stopped_late_acceptance_does_not_consume_candidates(probe_env, client):
    """任务已 STOPPED 时那一轮被栅栏拒绝：菜单与待确认计划**没被提前改动**。"""
    assert _post(client, "报表名称改成新日报", message_id="m1").status_code == 200
    assert _post(client, EDIT_ACCEPT, message_id="m2").status_code == 200
    draft_id = session_service.load_draft_id(SESSION_ID)
    assert draft_id

    # 再提一个候选：这一份要活到「迟到」那一轮之后。
    assert _post(client, "报表名称改成夜日报", message_id="m3").status_code == 200
    pending = session_service.load_pending_plan(SESSION_ID)
    assert pending is not None
    plan_id = pending.planId
    choice = session_service.load_pending_choice(SESSION_ID)
    assert choice is not None

    # 预置一个**已停止**的任务：下一轮用同一个 messageId → 同一个 taskId。
    # 写锁也用同一个持有者（同一持有者重入 = 放行），好让这一轮走到栅栏。
    task_id = "task_m9"
    owner = f"chat:{SESSION_ID}:m9"
    assert draft_workspace.acquire(draft_id, owner) is True
    draft_workspace.begin_task(
        draft_id,
        task_id=task_id,
        operation_id="m9",
        checkpoint={
            "sessionId": SESSION_ID,
            "messageId": "m9",
            "draftId": draft_id,
            "lockOwner": owner,
        },
    )
    stopped = client.post(f"/api/draft-tasks/{task_id}/stop")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["state"] == "STOPPED"

    before_messages = _message_count()
    before_snapshot = session_service.load_snapshot(SESSION_ID)
    before_state = draft_workspace.load_draft(draft_id)
    before_history = _history(draft_id)
    assert before_state is not None

    # 迟到的结果：真实接受路径会消费候选，然后被栅栏拒绝。
    late = _post(client, EDIT_ACCEPT, message_id="m9")
    assert late.status_code == 409, "迟到的结果不许当成功返回"

    # 菜单与待确认计划**原样**：不接受之前就已经清掉是不行的。
    after_plan = session_service.load_pending_plan(SESSION_ID)
    assert after_plan is not None, "被栅栏拒绝的一轮把待确认计划吃掉了"
    assert after_plan.planId == plan_id
    assert session_service.load_pending_choice(SESSION_ID) == choice

    # 会话与草稿原样。
    assert _message_count() == before_messages
    assert session_service.load_snapshot(SESSION_ID) == before_snapshot
    after_state = draft_workspace.load_draft(draft_id)
    assert after_state is not None
    assert after_state.revision == before_state.revision
    assert after_state.definition == before_state.definition
    assert _history(draft_id) == before_history

    # 停止现场还在：用户还能选「继续」或「撤销」。
    task = draft_workspace.load_task(task_id)
    assert task is not None and task.state == "STOPPED"
    assert draft_workspace.lock_owner(draft_id) == owner


# ---------- R03-2：任务收尾失败 → 整笔回滚 ----------

def test_task_completion_failure_rolls_back_the_whole_turn(
    probe_env, client, monkeypatch
):
    """任务收尾失败：整笔回滚 —— 不留「会话写了、任务没完成」的半份状态。"""
    # 先落一个草稿对象：这样这一轮才有任务可收尾（第一轮没有草稿对象时
    # 任务行不存在，见 ``_begin_turn_task``）。
    assert _post(client, "报表名称改成新日报", message_id="m1").status_code == 200
    assert _post(client, EDIT_ACCEPT, message_id="m2").status_code == 200
    draft_id = session_service.load_draft_id(SESSION_ID)
    assert draft_id

    # 再提一个候选：这一轮接受时会写工作区 + 收尾任务。
    assert _post(client, "报表名称改成夜日报", message_id="m3").status_code == 200
    pending = session_service.load_pending_plan(SESSION_ID)
    assert pending is not None
    choice = session_service.load_pending_choice(SESSION_ID)
    assert choice is not None

    # 注入确定性故障：任务收尾（标完成）失败。
    def boom(*_args, **_kwargs):
        raise RuntimeError("task completion failed")

    original_complete = draft_workspace.complete_task
    monkeypatch.setattr(draft_workspace, "complete_task", boom)

    before_messages = _message_count()
    before_snapshot = session_service.load_snapshot(SESSION_ID)
    before_state = draft_workspace.load_draft(draft_id)
    before_history = _history(draft_id)
    assert before_state is not None

    failed = _post(client, EDIT_ACCEPT, message_id="m4")
    assert failed.status_code >= 500, "任务收尾失败却返回成功"
    monkeypatch.setattr(draft_workspace, "complete_task", original_complete)

    # 会话与草稿**一起**回滚：没有半份状态。
    assert _message_count() == before_messages
    assert session_service.load_snapshot(SESSION_ID) == before_snapshot
    after_state = draft_workspace.load_draft(draft_id)
    assert after_state is not None
    assert after_state.revision == before_state.revision
    assert after_state.definition == before_state.definition
    assert _history(draft_id) == before_history

    # 任务没被标成完成，检查点里也没有「已提交到哪」。
    task = draft_workspace.load_task("task_m4")
    assert task is not None
    assert task.state != "COMPLETED"
    assert "committedRevision" not in (task.checkpoint or {})

    # 候选还在：这一轮可以重试。
    after_plan = session_service.load_pending_plan(SESSION_ID)
    assert after_plan is not None and after_plan.planId == pending.planId
    assert session_service.load_pending_choice(SESSION_ID) == choice


# ---------- R04-1：会话快照写失败 → 整笔回滚 ----------

def test_undo_rolls_back_when_session_snapshot_write_fails(
    probe_env, client, monkeypatch
):
    """会话快照写失败：撤销**没有生效**（版本与内容原样），接口返回错误而不是成功。"""
    draft_id = _draft_with_two_edits(client)

    before = client.get(f"/api/drafts/{draft_id}").json()
    before_snapshot = session_service.load_snapshot(SESSION_ID)
    before_history = _history(draft_id)

    # 注入确定性故障：写回会话快照失败。
    def boom(*_args, **_kwargs):
        raise RuntimeError("session snapshot write failed")

    original_update = session_service.update_snapshot
    monkeypatch.setattr(session_service, "update_snapshot", boom)

    undo = client.post(f"/api/drafts/{draft_id}/actions", json={"action": "UNDO"})
    assert undo.status_code >= 500, "撤销没生效却返回成功"
    monkeypatch.setattr(session_service, "update_snapshot", original_update)

    # 草稿一点没动：版本、内容、游标、修改历史都原样。
    after = client.get(f"/api/drafts/{draft_id}").json()
    assert after["revision"] == before["revision"]
    assert after["definition"] == before["definition"]
    assert after["undoPointer"] == before["undoPointer"]
    assert after["canUndo"] == before["canUndo"]
    assert _history(draft_id) == before_history

    # 会话快照也原样：不会出现「工作区已撤销、会话仍是旧草稿」。
    after_snapshot = session_service.load_snapshot(SESSION_ID)
    assert after_snapshot.get("definition") == before_snapshot.get("definition")


def test_save_as_binding_failure_is_an_error_not_a_silent_success(
    probe_env, client, monkeypatch
):
    """另存为后切换对象失败：必须**变成错误返回**，不能日志之后 200 成功。"""
    draft_id = _draft_with_two_edits(client)
    before = client.get(f"/api/drafts/{draft_id}").json()
    before_history = _history(draft_id)

    def boom(*_args, **_kwargs):
        raise RuntimeError("bind session failed")

    original_bind = draft_workspace.bind_session
    monkeypatch.setattr(draft_workspace, "bind_session", boom)

    saved = client.post(
        f"/api/drafts/{draft_id}/actions", json={"action": "SAVE_AS", "name": "副本"}
    )
    assert saved.status_code >= 500, "切换失败却返回成功"
    monkeypatch.setattr(draft_workspace, "bind_session", original_bind)

    # 半份状态不许留下：新对象没建出来，旧对象也没被切走。
    listed = client.get("/api/drafts").json()
    assert [row["draftId"] for row in listed] == [draft_id]
    assert session_service.load_draft_id(SESSION_ID) == draft_id
    after = client.get(f"/api/drafts/{draft_id}").json()
    assert after["revision"] == before["revision"]
    assert _history(draft_id) == before_history


# ---------- R04-2：撤销 / 重做后的 revision 自洽 ----------

def test_undo_redo_response_revision_is_self_consistent(probe_env, client):
    """撤销 / 重做之后：顶层 revision 与 definition.metadata.revision 一致，且单调递增。"""
    draft_id = _draft_with_two_edits(client)

    before = client.get(f"/api/drafts/{draft_id}").json()
    before_revision = int(before["revision"])

    undo = client.post(f"/api/drafts/{draft_id}/actions", json={"action": "UNDO"})
    assert undo.status_code == 200, undo.text
    undone = undo.json()

    # 撤销**不回退**乐观锁数值（契约 0002 §6.3）。
    assert undone["revision"] > before_revision, "撤销把版本号退回去了"
    # 顶层版本号与定义里的版本号是**同一个数**。
    assert undone["definition"]["metadata"]["revision"] == undone["revision"]
    # 内容回到上一个修改单位。
    assert undone["definition"]["metadata"]["name"] == "新日报"

    # 会话快照里的定义同样自洽（刷新后不会跳回旧内容）。
    snapshot_definition = session_service.load_snapshot(SESSION_ID)["definition"]
    assert snapshot_definition["metadata"]["revision"] == undone["revision"]

    # 打开草稿也是同一口径。
    opened = client.get(f"/api/drafts/{draft_id}").json()
    assert opened["revision"] == undone["revision"]
    assert opened["definition"]["metadata"]["revision"] == undone["revision"]

    redo = client.post(f"/api/drafts/{draft_id}/actions", json={"action": "REDO"})
    assert redo.status_code == 200, redo.text
    redone = redo.json()

    assert redone["revision"] > undone["revision"], "重做把版本号退回去了"
    assert redone["definition"]["metadata"]["revision"] == redone["revision"]
    assert redone["definition"]["metadata"]["name"] == "夜日报"
    snapshot_definition2 = session_service.load_snapshot(SESSION_ID)["definition"]
    assert snapshot_definition2["metadata"]["revision"] == redone["revision"]


# ---------- R03-3：候选的「消费」必须发生在最终提交事务里 ----------

def test_pending_plan_is_not_consumed_before_the_commit_transaction(
    probe_env, client, monkeypatch
):
    """候选的**消费**必须在最终提交事务里：事务失败时计划原样还在。

    这条用例**关掉补偿式恢复**（``_restore_pending_candidates`` 置空）：
    - 旧行为（业务层先消费计划，失败后再放回去）：关掉恢复，计划就没了 →
      断言失败；
    - 新行为（消费推迟到提交事务里，失败整笔回滚）：计划压根没被消费过 →
      断言成立。

    所以这条用例真的在验证「消费在事务内」，不是验证「失败后能恢复」。
    """
    from app.routers import agent as agent_router

    # 先走一轮真实的候选路径（规则直达，零模型）。
    assert _post(client, "报表名称改成新日报", message_id="m1").status_code == 200
    pending = session_service.load_pending_plan(SESSION_ID)
    assert pending is not None, "候选计划没落库"
    plan_id = pending.planId

    # 补偿式恢复**关掉**：只剩「消费在事务内」这一条能保住这份计划。
    monkeypatch.setattr(agent_router, "_restore_pending_candidates", lambda *a, **k: None)

    def boom(*_args, **_kwargs):
        raise RuntimeError("workspace write failed")

    original_commit = draft_workspace.commit_edit
    monkeypatch.setattr(draft_workspace, "commit_edit", boom)

    failed = _post(client, EDIT_ACCEPT, message_id="m2")
    assert failed.status_code >= 500, "落库失败不许返回成功 final"

    monkeypatch.setattr(draft_workspace, "commit_edit", original_commit)

    # 计划**压根没被消费过**（不是「消费了又被放回去」）。
    still = session_service.load_pending_plan(SESSION_ID)
    assert still is not None, "候选计划在事务外被消费掉了"
    assert still.planId == plan_id, "候选计划被换成了另一份"

    # 再正常接受一次：这次成功 → 计划在提交事务里被消费掉。
    accepted = _post(client, EDIT_ACCEPT, message_id="m3")
    assert accepted.status_code == 200, accepted.text
    assert session_service.load_pending_plan(SESSION_ID) is None, "计划没被消费掉"


# ---------- R03-4：候选**消费之后**的持久写失败 → 整体回滚 + 菜单没变过 ----------


def _spy_pending_choice_writes(monkeypatch) -> list[tuple[Any, bool]]:
    """记录这一轮里每一次「写选择锁」，并标注它**在不在事务里**。

    返回的每一项是 ``(写入的值, 是否在事务里)``。

    为什么记这个：只比「失败后菜单与失败前相等」不够 ——
    「提前写了、失败后再放回去」也长得一模一样。这里直接看**写的那一下**
    在不在提交事务里：在事务里 = 失败会跟着回滚；不在事务里 = 提前写了。
    """
    writes: list[tuple[Any, bool]] = []
    repo = session_service.repository
    original_set = repo.set_pending_choice

    def spy(session_id: str, choice: Any) -> None:
        in_transaction = int(getattr(repo, "_tx_depth", 0)) > 0
        writes.append((choice, in_transaction))
        return original_set(session_id, choice)

    monkeypatch.setattr(repo, "set_pending_choice", spy)
    return writes


def test_consumption_write_failure_rolls_back_everything_and_allows_retry(
    probe_env, client, monkeypatch
):
    """计划**已经消费之后**的持久写失败：候选/菜单/会话/草稿整体回滚，能重试。

    注入点：最终提交事务里、**计划已经被取走、锁已经被清之后**的那一步持久
    写（消费要清追问计数）失败。整笔回滚必须把「消费」也退回去：候选与
    菜单原样还在，用户能重试这一轮。

    这条用例**关掉补偿式恢复**（``_restore_pending_candidates`` 置空）：
    补偿恢复会让「提前写过、失败后再放回去」也看起来是对的 —— 关掉它，
    才真的在验证「菜单从未提前写」。
    """
    from app.routers import agent as agent_router

    # A 轮：真实候选路径（规则直达，零模型）。
    assert _post(client, "报表名称改成新日报", message_id="m1").status_code == 200
    accepted = _post(client, EDIT_ACCEPT, message_id="m2")
    assert accepted.status_code == 200, accepted.text
    draft_id = session_service.load_draft_id(SESSION_ID)
    assert draft_id

    # B 轮：再提一个候选（这一份就是要被保住的那份）。
    assert _post(client, "报表名称改成夜日报", message_id="m3").status_code == 200
    pending_b = session_service.load_pending_plan(SESSION_ID)
    assert pending_b is not None
    choice_b = session_service.load_pending_choice(SESSION_ID)
    assert choice_b is not None

    before_messages = _message_count()
    before_message_rows = _messages()
    before_snapshot = session_service.load_snapshot(SESSION_ID)
    before_state = draft_workspace.load_draft(draft_id)
    before_history = _history(draft_id)
    before_versions = _versions(draft_id)
    assert before_state is not None

    # 补偿式恢复**关掉**：只剩「从未提前写」这一条能保住菜单。
    monkeypatch.setattr(agent_router, "_restore_pending_candidates", lambda *a, **k: None)
    writes = _spy_pending_choice_writes(monkeypatch)

    # 注入**确定性**故障：提交事务里、**计划已经被消费之后**的那一步持久写
    # 失败（消费要清追问计数）。这一刻计划已经被取走、锁已经被清 ——
    # 整笔回滚必须把「消费」也退回去。
    def boom(*_args, **_kwargs):
        raise RuntimeError("clarification reset failed")

    original_reset = session_service.reset_clarification
    monkeypatch.setattr(session_service, "reset_clarification", boom)

    failed = _post(client, EDIT_ACCEPT, message_id="m4")
    assert failed.status_code >= 500, "落库失败不许返回成功 final"
    monkeypatch.setattr(session_service, "reset_clarification", original_reset)

    # ---- 候选现场：计划原样还在，菜单**逐字**没变 ----
    after_plan = session_service.load_pending_plan(SESSION_ID)
    assert after_plan is not None, "候选计划被吃掉了，用户没法重试这一轮"
    assert after_plan.planId == pending_b.planId, "候选计划被换成了另一份"
    after_choice = session_service.load_pending_choice(SESSION_ID)
    assert after_choice == choice_b, "失败轮的菜单变了（提交前菜单必须没变）"

    # ---- 菜单**从未在事务外被写**：每一笔写都在提交事务里 ----
    assert writes, "这一轮根本没写过选择锁（用例没打中）"
    assert all(in_tx for _, in_tx in writes), (
        "选择锁在提交事务之外被提前写了：失败轮的菜单不是「没变」，"
        "是「变了又被放回去」"
    )

    # ---- 会话原样：消息没多、快照没变 ----
    assert _message_count() == before_messages
    assert _messages() == before_message_rows
    assert session_service.load_snapshot(SESSION_ID) == before_snapshot

    # ---- 草稿原样：版本、内容、撤销指针、版本行、修改历史 ----
    after_state = draft_workspace.load_draft(draft_id)
    assert after_state is not None
    assert after_state.revision == before_state.revision
    assert after_state.definition == before_state.definition
    assert after_state.undoPointer == before_state.undoPointer
    assert _versions(draft_id) == before_versions
    assert _history(draft_id) == before_history

    # ---- 任务现场：没有「提交了一半」的痕迹 ----
    task = draft_workspace.load_task("task_m4")
    assert task is not None
    assert task.state != "COMPLETED", "任务标成完成了，但这一轮没提交"
    assert "committedRevision" not in (task.checkpoint or {})

    # ---- 能重试：同一 messageId 重发 → 200，这次才真的消费掉候选 ----
    retried = _post(client, EDIT_ACCEPT, message_id="m4")
    assert retried.status_code == 200, retried.text
    assert session_service.load_pending_plan(SESSION_ID) is None, "计划没被消费掉"
    final_choice = session_service.load_pending_choice(SESSION_ID)
    assert final_choice is not None, "下一菜单没落库"
    assert final_choice["question"]["id"] == "template_edit_confirm"
    final_state = draft_workspace.load_draft(draft_id)
    assert final_state is not None
    assert final_state.revision == before_state.revision + 1
    assert final_state.definition["metadata"]["name"] == "夜日报"
    assert _history(draft_id) == before_history + ["commit"]
    assert draft_workspace.load_task("task_m4").state == "COMPLETED"


def test_plan_consumption_write_failure_rolls_back_everything(
    probe_env, client, monkeypatch
):
    """**消费本身**那一步的写失败：同样整体回滚，候选与菜单原样还在。

    注入点：候选消费要清选择锁时写库失败 —— 计划已经被取走（同一笔事务
    里），但整笔回滚必须把它也退回去。
    """
    from app.routers import agent as agent_router

    assert _post(client, "报表名称改成新日报", message_id="m1").status_code == 200
    assert _post(client, EDIT_ACCEPT, message_id="m2").status_code == 200
    draft_id = session_service.load_draft_id(SESSION_ID)
    assert draft_id
    assert _post(client, "报表名称改成夜日报", message_id="m3").status_code == 200
    pending_b = session_service.load_pending_plan(SESSION_ID)
    assert pending_b is not None
    choice_b = session_service.load_pending_choice(SESSION_ID)
    assert choice_b is not None

    before_messages = _message_count()
    before_message_rows = _messages()
    before_snapshot = session_service.load_snapshot(SESSION_ID)
    before_state = draft_workspace.load_draft(draft_id)
    before_history = _history(draft_id)
    assert before_state is not None

    monkeypatch.setattr(agent_router, "_restore_pending_candidates", lambda *a, **k: None)

    # 注入：写选择锁这一步失败（消费计划时要清锁）。
    def boom(*_args, **_kwargs):
        raise RuntimeError("lock write failed")

    repo = session_service.repository
    original_set = repo.set_pending_choice
    monkeypatch.setattr(repo, "set_pending_choice", boom)

    failed = _post(client, EDIT_ACCEPT, message_id="m4")
    assert failed.status_code >= 500, "落库失败不许返回成功 final"
    monkeypatch.setattr(repo, "set_pending_choice", original_set)

    # 候选与菜单原样。
    after_plan = session_service.load_pending_plan(SESSION_ID)
    assert after_plan is not None
    assert after_plan.planId == pending_b.planId
    assert session_service.load_pending_choice(SESSION_ID) == choice_b

    # 会话与草稿原样。
    assert _message_count() == before_messages
    assert _messages() == before_message_rows
    assert session_service.load_snapshot(SESSION_ID) == before_snapshot
    after_state = draft_workspace.load_draft(draft_id)
    assert after_state is not None
    assert after_state.revision == before_state.revision
    assert after_state.definition == before_state.definition
    assert after_state.undoPointer == before_state.undoPointer
    assert _history(draft_id) == before_history

    # 能重试。
    retried = _post(client, EDIT_ACCEPT, message_id="m4")
    assert retried.status_code == 200, retried.text
    assert session_service.load_pending_plan(SESSION_ID) is None
