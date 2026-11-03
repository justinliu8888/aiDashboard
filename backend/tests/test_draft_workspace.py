"""草稿工作区（P2）的用例。

纪律：
    - 只用 `tmp_path` 里的临时 SQLite，**绝不碰** `backend/data/haewu.db`；
    - 只测这一包的行为：版本语义、撤销 / 重做、显式保存、另存为、
      乐观锁、事务原子性、任务状态机、锁。
"""
from __future__ import annotations

import pytest

from app.services.draft_workspace import (
    DraftLocked,
    DraftNotFound,
    DraftRedoUnavailable,
    DraftRevisionConflict,
    DraftTaskNotActive,
    DraftTaskNotFound,
    DraftWorkspace,
    DraftWorkspaceError,
)
from app.storage.sqlite_store import SqliteSessionRepository


@pytest.fixture()
def workspace(tmp_path):
    """一个落在临时目录里的工作区（不共享任何生产库）。"""
    repo = SqliteSessionRepository(tmp_path / "drafts.db")
    return DraftWorkspace(repository=repo)


def _baseline(workspace: DraftWorkspace, draft_id: str = "d1", **kwargs) -> None:
    workspace.create_draft(
        draft_id,
        session_id=kwargs.get("session_id", "s1"),
        name=kwargs.get("name", "报表草稿"),
        definition=kwargs.get("definition", {"v": 0}),
    )


def _contents(workspace: DraftWorkspace, draft_id: str) -> list[dict]:
    """该草稿的编辑单位内容（按版本号升序）。"""
    return [
        unit["definition"]
        for unit in workspace.adapter.list_draft_versions(draft_id)
        if unit["kind"] == "edit"
    ]


def _versions(workspace: DraftWorkspace, draft_id: str) -> list[dict]:
    return workspace.adapter.list_draft_versions(draft_id)


def _actions(workspace: DraftWorkspace, draft_id: str) -> list[str]:
    return [row["action"] for row in workspace.adapter.list_history(draft_id)]


# ---------- 基线 ----------

def test_create_draft_makes_baseline_without_undo_entry(workspace):
    """建基线：revision 1，不产生撤销条目（历史空、canUndo False）。"""
    state = workspace.create_draft(
        "d1", session_id="s1", name="报表草稿", definition={"v": 0}
    )

    assert state.revision == 1
    assert state.canUndo is False
    assert state.canRedo is False
    assert state.undoPointer == 0
    assert state.redoPointer == 0
    assert state.definition == {"v": 0}
    assert state.sessionId == "s1"
    assert state.name == "报表草稿"
    assert workspace.adapter.list_history("d1") == []


# ---------- 提交 ----------

def test_commit_edit_writes_version_and_history_atomically(workspace):
    """提交一次：版本表多一行 kind='edit'，历史多一条，版本号前进。"""
    _baseline(workspace)

    state = workspace.commit_edit(
        "d1",
        definition={"v": 1},
        snapshot={"businessState": "ANALYZING", "prompt": "改一下"},
        label="接受修改",
    )

    assert state.revision == 2
    assert state.definition == {"v": 1}
    assert state.canUndo is True
    assert state.canRedo is False
    assert state.redoPointer == 0
    # 会话侧的快照跟着这次提交落库（业务状态 / prompt 从快照里读回）
    assert state.businessState == "ANALYZING"
    assert state.prompt == "改一下"

    versions = _versions(workspace, "d1")
    assert [row["kind"] for row in versions] == ["edit", "edit"]
    assert versions[-1]["definition"] == {"v": 1}
    assert _actions(workspace, "d1") == ["commit"]
    # 一次接受 = 一个修改单位
    assert state.undoPointer == 1


def test_commit_edit_on_missing_draft_raises_not_found(workspace):
    """对不存在的草稿提交：抛 draft_not_found，而不是 KeyError。"""
    with pytest.raises(DraftNotFound) as excinfo:
        workspace.commit_edit("missing", definition={"v": 1})

    assert excinfo.value.code == "draft_not_found"
    assert isinstance(excinfo.value, DraftWorkspaceError)
    assert not isinstance(excinfo.value, KeyError)


# ---------- 撤销 / 重做 ----------

def test_undo_restores_previous_definition_and_keeps_revision_increasing(workspace):
    """撤销：内容回到上一版，但版本号继续单调递增（不回退乐观锁数值）。"""
    _baseline(workspace)
    workspace.commit_edit("d1", definition={"v": 1})
    before = workspace.commit_edit("d1", definition={"v": 2})
    assert before.revision == 3

    undone = workspace.undo("d1")

    assert undone.definition == {"v": 1}
    # 版本号**继续 +1**：不回退乐观锁数值
    assert undone.revision > before.revision
    assert undone.canUndo is True
    assert undone.canRedo is True
    assert undone.undoPointer == 1
    assert undone.redoPointer == 1
    assert _actions(workspace, "d1") == ["commit", "commit", "undo"]


def test_redo_reapplies_and_new_commit_clears_redo_branch(workspace):
    """重做可用；撤销之后再来一次提交，旧 redo 分支被清掉。"""
    _baseline(workspace)
    workspace.commit_edit("d1", definition={"v": 1})
    workspace.commit_edit("d1", definition={"v": 2})

    undone = workspace.undo("d1")
    assert undone.definition == {"v": 1}
    assert undone.canRedo is True
    assert undone.redoPointer == 1

    redone = workspace.redo("d1")
    assert redone.definition == {"v": 2}
    assert redone.canRedo is False
    assert redone.redoPointer == 0

    # 再撤销一次，然后提交新修改 —— 旧的 redo 分支（v2）被清掉。
    workspace.undo("d1")
    state = workspace.commit_edit("d1", definition={"v": 3})

    assert state.definition == {"v": 3}
    assert state.canRedo is False
    assert state.redoPointer == 0
    assert {"v": 2} not in _contents(workspace, "d1")
    with pytest.raises(DraftRedoUnavailable):
        workspace.redo("d1")


# ---------- 显式保存 ----------

def test_save_creates_saved_revision_distinct_from_edit_revision(workspace):
    """显式保存：留一条 saved 版本；savedRevision 与 revision 是两个值；不发布。"""
    _baseline(workspace)
    workspace.commit_edit("d1", definition={"v": 1})
    before = workspace.load_draft("d1")
    assert before is not None

    state = workspace.save("d1")

    # 两个值：编辑链版本号与显式保存版**版本号**互不占用
    assert state.savedRevision is not None
    assert state.savedRevision != state.revision
    # 保存不推进编辑链：当前编辑版本还是提交那一版的号
    assert state.revision == before.revision
    # savedRevision 指向一条真实存在的 saved 版本行
    saved_rows = [
        row for row in _versions(workspace, "d1") if row["kind"] == "saved"
    ]
    assert len(saved_rows) == 1
    assert saved_rows[0]["revision"] == state.savedRevision
    assert saved_rows[0]["definition"] == {"v": 1}
    # 不发布：内容与撤销单位都没变
    assert state.definition == {"v": 1}
    assert state.canUndo is True
    assert state.undoPointer == before.undoPointer
    assert len(_contents(workspace, "d1")) == 2
    assert _actions(workspace, "d1") == ["commit", "save"]


# ---------- 另存为 ----------

def test_save_as_creates_new_draft_without_history_or_redo(workspace):
    """另存为：新 draftId，历史与 redo 为空，原草稿不变。"""
    _baseline(workspace)
    workspace.commit_edit("d1", definition={"v": 1})
    workspace.commit_edit("d1", definition={"v": 2})
    original = workspace.load_draft("d1")
    assert original is not None
    history_before = workspace.adapter.list_history("d1")

    copied = workspace.save_as("d1", name="副本")

    assert copied.draftId != "d1"
    assert copied.name == "副本"
    assert copied.definition == {"v": 2}
    # 不复制聊天：新草稿没有绑定会话
    assert copied.sessionId is None
    # 不复制旧修改历史，也不带 redo 分支
    assert workspace.adapter.list_history(copied.draftId) == []
    assert copied.canRedo is False
    assert copied.canUndo is False
    assert copied.redoPointer == 0

    # 原草稿一个字节都没改
    after = workspace.load_draft("d1")
    assert after is not None
    assert after.revision == original.revision
    assert after.definition == original.definition
    assert after.canUndo == original.canUndo
    assert after.canRedo == original.canRedo
    # 另存为**不往原草稿写历史**
    assert workspace.adapter.list_history("d1") == history_before


# ---------- 乐观锁 ----------

def test_expected_revision_mismatch_raises_conflict(workspace):
    """版本对不上：抛冲突异常，状态一点没变。"""
    _baseline(workspace)
    workspace.commit_edit("d1", definition={"v": 1})
    before = workspace.load_draft("d1")
    assert before is not None

    with pytest.raises(DraftRevisionConflict) as excinfo:
        workspace.commit_edit(
            "d1", definition={"v": 99}, expected_revision=before.revision - 1
        )

    assert excinfo.value.code == "draft_revision_conflict"
    after = workspace.load_draft("d1")
    assert after is not None
    assert after.revision == before.revision
    assert after.definition == before.definition


# ---------- 旧版本保护（撤销 / 重做 / 另存为） ----------

def test_undo_redo_and_save_as_reject_stale_expected_revision(workspace):
    """旧版本的撤销 / 重做 / 另存为一律拒绝，状态一点没变。"""
    _baseline(workspace)
    workspace.commit_edit("d1", definition={"v": 1})
    workspace.commit_edit("d1", definition={"v": 2})
    before = workspace.load_draft("d1")
    assert before is not None
    stale = before.revision - 1
    history_before = _actions(workspace, "d1")

    with pytest.raises(DraftRevisionConflict):
        workspace.undo("d1", expected_revision=stale)
    with pytest.raises(DraftRevisionConflict):
        workspace.redo("d1", expected_revision=stale)
    with pytest.raises(DraftRevisionConflict):
        workspace.save_as("d1", name="副本", expected_revision=stale)

    after = workspace.load_draft("d1")
    assert after is not None
    assert after.revision == before.revision
    assert after.definition == before.definition
    assert after.undoPointer == before.undoPointer
    assert _actions(workspace, "d1") == history_before
    # 另存为被拒 → 没有多出草稿对象
    assert len(workspace.list_drafts()) == 1


def test_undo_redo_and_save_as_accept_current_expected_revision(workspace):
    """版本对得上：动作正常生效（撤销 / 重做 / 另存为都要能走）。"""
    _baseline(workspace)
    workspace.commit_edit("d1", definition={"v": 1})
    current = workspace.load_draft("d1")
    assert current is not None

    undone = workspace.undo("d1", expected_revision=current.revision)
    assert undone.definition == {"v": 0}

    redone = workspace.redo("d1", expected_revision=undone.revision)
    assert redone.definition == {"v": 1}

    copied = workspace.save_as(
        "d1", name="副本", expected_revision=redone.revision
    )
    assert copied.draftId != "d1"
    assert copied.definition == {"v": 1}


# ---------- 事务原子性 ----------

def test_commit_is_atomic_when_history_write_fails(workspace, monkeypatch):
    """历史写失败时，定义版本也不落库（不留半份）。"""
    _baseline(workspace)
    workspace.commit_edit("d1", definition={"v": 1})

    def boom(*_args, **_kwargs):
        raise RuntimeError("history write failed")

    monkeypatch.setattr(workspace.adapter, "append_history", boom)

    with pytest.raises(RuntimeError):
        workspace.commit_edit("d1", definition={"v": 2})

    monkeypatch.undo()
    state = workspace.load_draft("d1")
    assert state is not None
    assert state.definition == {"v": 1}
    assert state.revision == 2
    assert _contents(workspace, "d1") == [{"v": 0}, {"v": 1}]


def test_shared_transaction_rolls_back_session_and_draft_writes(workspace):
    """共享事务入口：会话写与草稿版本写进同一笔事务，失败时两边都不留。"""
    _baseline(workspace)
    repo = workspace.repository

    with pytest.raises(RuntimeError):
        with repo.transaction() as conn:
            conn.execute(
                "INSERT INTO sessions (id, title, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("s1", "w", "ANALYZING", "t", "t"),
            )
            workspace.commit_edit("d1", definition={"v": 1})
            raise RuntimeError("boom")

    row = repo.conn.execute(
        "SELECT COUNT(*) AS c FROM sessions WHERE id = ?", ("s1",)
    ).fetchone()
    assert row["c"] == 0
    state = workspace.load_draft("d1")
    assert state is not None
    assert state.definition == {"v": 0}
    assert state.revision == 1


# ---------- 任务状态机 ----------

def test_task_lifecycle_stop_and_resume(workspace):
    """任务：开 → 存检查点 → 停止 → 从检查点继续。"""
    _baseline(workspace)

    task = workspace.begin_task("d1", operation_id="op-1")
    assert task.state == "RUNNING"

    task = workspace.checkpoint_task(task.taskId, {"step": 2})
    assert task.checkpoint == {"step": 2}

    task = workspace.stop_task(task.taskId)
    assert task.state == "STOPPED"

    task = workspace.resume_task(task.taskId)
    assert task.state == "RUNNING"
    # 检查点**保留**，另外记下「这是第几次续跑、从哪个状态续的」——
    # 续跑失败时要靠它回到可处理的停止现场（契约 0002 §7）。
    assert set(task.checkpoint) == {"step", "resumeCount", "resumedAt", "resumedFrom"}
    assert task.checkpoint["step"] == 2
    assert task.checkpoint["resumeCount"] == 1
    assert task.checkpoint["resumedFrom"] == "STOPPED"
    assert isinstance(task.checkpoint["resumedAt"], str)
    assert task.checkpoint["resumedAt"]

    with pytest.raises(DraftTaskNotFound) as excinfo:
        workspace.stop_task("task_missing")
    assert excinfo.value.code == "task_not_found"


def test_resume_task_keeps_progress_and_records_resume_count(workspace):
    """续跑不清检查点：已提交的进度要留着，续跑次数累加。"""
    _baseline(workspace)
    task = workspace.begin_task("d1", operation_id="op-1")
    workspace.checkpoint_task(task.taskId, {"committedRevision": 7})

    workspace.stop_task(task.taskId)
    resumed = workspace.resume_task(task.taskId)

    assert resumed.state == "RUNNING"
    assert resumed.checkpoint["committedRevision"] == 7
    assert resumed.checkpoint["resumeCount"] == 1

    workspace.stop_task(task.taskId)
    again = workspace.resume_task(task.taskId)
    assert again.checkpoint["committedRevision"] == 7
    assert again.checkpoint["resumeCount"] == 2


def test_stop_task_rejects_finished_task(workspace):
    """已经结束的任务停不了：改成 STOPPED 会让对象一直被锁着。"""
    _baseline(workspace)
    task = workspace.begin_task("d1", operation_id="op-1")
    workspace.complete_task(task.taskId)

    with pytest.raises(DraftTaskNotActive) as excinfo:
        workspace.stop_task(task.taskId)

    assert excinfo.value.code == "task_not_active"
    assert workspace.load_task(task.taskId).state == "COMPLETED"


def test_resume_task_rejects_completed_or_rolled_back_task(workspace):
    """完成 / 已撤销的任务不能再续跑。"""
    _baseline(workspace)
    task = workspace.begin_task("d1", operation_id="op-1")
    workspace.complete_task(task.taskId)

    with pytest.raises(DraftTaskNotActive):
        workspace.resume_task(task.taskId)

    other = workspace.begin_task("d1", operation_id="op-2")
    workspace.rollback_task(other.taskId)
    with pytest.raises(DraftTaskNotActive):
        workspace.resume_task(other.taskId)


def test_begin_task_does_not_resurrect_stopped_task(workspace):
    """同一轮重发：停了就是停了，不能顺手复活（要走显式的 resume）。"""
    _baseline(workspace)
    task = workspace.begin_task("d1", operation_id="op-1")
    workspace.stop_task(task.taskId)

    again = workspace.begin_task(
        "d1", task_id=task.taskId, operation_id="op-1", checkpoint={"note": "x"}
    )

    assert again.state == "STOPPED"
    # 检查点是**合并**的：之前记的进度不清掉。
    assert again.checkpoint["note"] == "x"


def test_abort_task_keeps_stop_site_when_stoppable(workspace):
    """续跑失败：任务回到 STOPPED，两个选项还在（现场可继续处理）。"""
    _baseline(workspace)
    task = workspace.begin_task("d1", operation_id="op-1")
    workspace.stop_task(task.taskId)
    workspace.resume_task(task.taskId)

    aborted = workspace.abort_task(task.taskId, reason="boom", stoppable=True)

    assert aborted.state == "STOPPED"
    assert aborted.checkpoint["reason"] == "boom"


def test_abort_task_marks_failed_when_not_stoppable(workspace):
    """普通失败：任务收尾为 FAILED。"""
    _baseline(workspace)
    task = workspace.begin_task("d1", operation_id="op-1")

    aborted = workspace.abort_task(task.taskId, reason="boom")

    assert aborted.state == "FAILED"
    assert aborted.checkpoint["reason"] == "boom"


def test_active_task_includes_stopped_task(workspace):
    """停止现场要能从服务端恢复：activeTask 里能看到 STOPPED 的任务。"""
    _baseline(workspace)
    task = workspace.begin_task("d1", operation_id="op-1")
    workspace.stop_task(task.taskId)

    state = workspace.load_draft("d1")
    assert state is not None
    assert state.activeTask is not None
    assert state.activeTask["taskId"] == task.taskId
    assert state.activeTask["state"] == "STOPPED"
    # 完成的轮次不算「还没收尾」。
    workspace.complete_task(task.taskId)
    finished = workspace.load_draft("d1")
    assert finished is not None
    assert finished.activeTask is None


# ---------- 锁 ----------

def test_lock_blocks_concurrent_write(workspace):
    """同一草稿的写互斥；不同草稿互不影响。"""
    assert workspace.is_locked("d1") is False
    assert workspace.acquire("d1", "owner-a") is True
    assert workspace.is_locked("d1") is True
    assert workspace.lock_owner("d1") == "owner-a"
    # 第二个 owner 拿不到锁
    assert workspace.acquire("d1", "owner-b") is False
    # 同一个 owner 重复拿是幂等的
    assert workspace.acquire("d1", "owner-a") is True
    # 不是持有者放不掉
    assert workspace.release("d1", "owner-b") is False
    assert workspace.release("d1", "owner-a") is True
    assert workspace.is_locked("d1") is False
    # 不同草稿互不影响
    assert workspace.acquire("d2", "owner-b") is True


def test_lock_state_is_persisted_in_database(workspace):
    """锁状态存在库里（不另开连接），换一个工作区实例也看得见。"""
    workspace.acquire("d1", "owner-a")

    other = DraftWorkspace(repository=workspace.repository)
    assert other.is_locked("d1") is True
    assert other.lock_owner("d1") == "owner-a"
    assert other.acquire("d1", "owner-b") is False


def test_require_unlocked_allows_same_owner_reentry(workspace):
    """锁的授权语义：同一持有者重入放行（这就是续跑），别人持锁拒绝。"""
    workspace.acquire("d1", "owner-a")

    # 同一持有者：放行（重入）。
    workspace.require_unlocked("d1", owner="owner-a")
    # 别的持有者：拒绝。
    with pytest.raises(DraftLocked) as excinfo:
        workspace.require_unlocked("d1", owner="owner-b")
    assert excinfo.value.code == "draft_locked"
    # 不给持有者：任何锁都不放行（草稿级动作）。
    with pytest.raises(DraftLocked):
        workspace.require_unlocked("d1")
    # 没锁的时候一律放行。
    workspace.require_unlocked("d2")


def test_same_owner_may_reenter_lock(workspace):
    """同一个持有者重复拿锁是幂等的（续跑要靠它）。"""
    assert workspace.acquire("d1", "owner-a") is True
    assert workspace.acquire("d1", "owner-a") is True
    assert workspace.lock_owner("d1") == "owner-a"
    assert workspace.acquire("d1", "owner-b") is False
