"""草稿工作区的**路由级**安全用例（R02 / R03 / R04）。

这一组用例打的是**真实路由**（``/api/agent/chat``、``/api/agent/chat/stream``、
``/api/drafts/...``、``/api/draft-tasks/...``），不是底层工作区方法：
底层那笔共享事务成立，**不能**代替「生产路由真的用了那笔事务」的证据
（返工任务书 R03 的通过判据）。

纪律：
    - 只用临时 SQLite（``tempfile.mkdtemp()``），**绝不碰** ``backend/data/haewu.db``；
    - ``session_service._repo`` 临时换掉，用 try/finally 还原；
    - 断言落到**定义内容 / 版本号 / 修改历史 / 会话快照 / 锁状态**上，
      不拿 200 当通过；
    - 假 Agent 只支撑确定性故障注入，不做真实模型调用。
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.routers import agent as agent_router_module
from app.schemas.agent import AgentResponse, AgentResponseDomainObjects, AgentState
from app.schemas.report_definition import ReportDefinitionDraft
from app.schemas.session import PendingEditPlan
from app.services.draft_workspace import draft_workspace
from app.services.session_service import session_service
from app.storage.sqlite_store import SqliteSessionRepository


#: 用户库：只用来**核对它没被改动**，永远不写。
USER_DB = Path(__file__).resolve().parents[1] / "data" / "haewu.db"

SESSION_ID = "sess_probe"


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

    草稿工作区是**跟着会话库走**的（``_follow_session_repo``），
    所以换掉会话仓库就等于把整套草稿表换到临时库里。
    """
    tmp_dir = tempfile.mkdtemp(prefix="haewu-draft-probe-")
    db_path = Path(tmp_dir) / "probe.db"
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


class _ScriptedAgent:
    """按脚本吐响应的假 Agent：只支撑确定性故障注入。"""

    def __init__(self) -> None:
        self.pending: list[AgentResponse] = []
        self.requests: list[Any] = []

    def push(self, response: AgentResponse) -> None:
        self.pending.append(response)

    async def chat(self, request, emit=None):  # noqa: ANN001 — 路由按这个签名调
        self.requests.append(request)
        response = self.pending.pop(0) if self.pending else _response()
        if emit is not None:
            await emit("stage", {"stage": "probe"})
        return response


@pytest.fixture()
def scripted_agent(monkeypatch) -> _ScriptedAgent:
    agent = _ScriptedAgent()
    monkeypatch.setattr(agent_router_module.agent_service, "chat", agent.chat)
    return agent


# ---------- 请求 / 响应助手 ----------

def _definition(revision: int, marker: str) -> ReportDefinitionDraft:
    return ReportDefinitionDraft(
        metadata={"id": "def_probe", "revision": revision, "name": "探针报表"},
        regions=[{"id": "region_probe", "type": "card", "title": marker}],
    )


def _response(
    definition: ReportDefinitionDraft | None = None,
    *,
    state: str = "ANALYZING",
    reply: str = "探针回复",
) -> AgentResponse:
    return AgentResponse(
        conversationId="conv_probe",
        state=AgentState(state),
        reply=reply,
        domainObjects=AgentResponseDomainObjects(reportDefinitionDraft=definition),
    )


def _body(
    message: str,
    *,
    message_id: str,
    state: str = "ANALYZING",
    session_id: str = SESSION_ID,
    conversation_id: str = "conv_probe",
) -> dict[str, Any]:
    return {
        "agentProtocolVersion": "1.0",
        "conversationId": conversation_id,
        "sessionId": session_id,
        "messageId": message_id,
        "state": state,
        "message": message,
        "context": {
            "projectId": "project_001",
            "subjectRef": "machine_room",
            "subjectId": "room_a",
        },
    }


def _post_chat(client: TestClient, message: str, *, message_id: str, **kwargs):
    return client.post("/api/agent/chat", json=_body(message, message_id=message_id, **kwargs))


def _post_stream(client: TestClient, message: str, *, message_id: str, **kwargs):
    return client.post(
        "/api/agent/chat/stream", json=_body(message, message_id=message_id, **kwargs)
    )


def _sse_events(text: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = line[len("data: "):]
        if event:
            events.append((event, json.loads(data) if data else None))
    return events


def _sse_final(response) -> dict[str, Any]:
    """取流里的 final；出现 error 事件直接失败（失败不许伪装成成功 final）。"""
    events = _sse_events(response.text)
    errors = [data for event, data in events if event == "error"]
    assert not errors, f"流里出现 error 事件：{errors}"
    finals = [data for event, data in events if event == "final"]
    assert finals, f"流里没有 final 事件：{events}"
    return finals[-1]


def _units(draft_id: str) -> list[dict[str, Any]]:
    return [
        row
        for row in draft_workspace.adapter.list_draft_versions(draft_id)
        if row["kind"] == "edit"
    ]


def _history(draft_id: str) -> list[str]:
    return [row["action"] for row in draft_workspace.adapter.list_history(draft_id)]


def _accepted_draft(client: TestClient, scripted_agent: _ScriptedAgent) -> str:
    """先接受一份定义（A 轮），返回草稿 id。"""
    scripted_agent.push(_response(_definition(1, "A"), state="WAITING_USER_CONFIRM"))
    accepted = _post_chat(client, "生成报表", message_id="msg_a")
    assert accepted.status_code == 200, accepted.text
    payload = accepted.json()["domainObjects"]["draftWorkspace"]
    assert payload["draftId"]
    return str(payload["draftId"])


def _start_stopped_task(draft_id: str, *, message_id: str = "msg_b") -> tuple[str, str]:
    """模拟「B 正在执行」：注册任务并持写锁，然后停掉它。"""
    task_id = f"task_{message_id}"
    owner = f"chat:{SESSION_ID}:{message_id}"
    assert draft_workspace.acquire(draft_id, owner) is True
    draft_workspace.begin_task(
        draft_id,
        task_id=task_id,
        operation_id=message_id,
        checkpoint={
            "sessionId": SESSION_ID,
            "messageId": message_id,
            "draftId": draft_id,
            "lockOwner": owner,
        },
    )
    return task_id, owner


# ---------- R02：停止 → 继续 → 真正续跑 ----------

def test_resume_continues_same_task_and_commits_single_modification_unit(
    probe_env, client, scripted_agent
):
    """停止后点「继续」：B 真正跑完，一次接受只产生一个修改单位，A 不重复。"""
    draft_id = _accepted_draft(client, scripted_agent)
    task_id, _owner = _start_stopped_task(draft_id)

    stopped = client.post(f"/api/draft-tasks/{task_id}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["state"] == "STOPPED"
    assert stopped.json()["choices"] == ["继续完成剩下的工作", "撤销（回到上一步）"]

    # 停止现场要能从服务端恢复：刷新后仍看得到「已停止」。
    opened = client.get(f"/api/drafts/{draft_id}")
    assert opened.status_code == 200
    active = opened.json()["activeTask"]
    assert active is not None, "刷新后看不到停止现场"
    assert active["state"] == "STOPPED"
    assert active["taskId"] == task_id

    # 停止期间同对象写仍然被拒绝。
    blocked = client.post(f"/api/drafts/{draft_id}/actions", json={"action": "SAVE"})
    assert blocked.status_code == 409

    # 继续：服务端给出**本轮身份**与检查点。
    resumed = client.post(f"/api/draft-tasks/{task_id}/resume")
    assert resumed.status_code == 200, resumed.text
    resume_body = resumed.json()
    assert resume_body["taskId"] == task_id
    assert resume_body["state"] == "RUNNING"
    assert resume_body["operationId"] == "msg_b"
    checkpoint = resume_body["checkpoint"]
    assert checkpoint["messageId"] == "msg_b"
    assert checkpoint["sessionId"] == SESSION_ID
    assert checkpoint["draftId"] == draft_id

    # 续跑：同一个 messageId → 同一个任务；不再被写锁挡成 409。
    scripted_agent.push(_response(_definition(2, "B")))
    continued = _post_stream(client, "生成报表", message_id="msg_b")
    assert continued.status_code == 200, continued.text
    final = _sse_final(continued)
    assert final["domainObjects"]["draftWorkspace"]["draftId"] == draft_id

    # B 真正完成：定义内容、版本号、修改历史都对得上。
    state = client.get(f"/api/drafts/{draft_id}").json()
    assert state["definition"]["regions"][0]["title"] == "B"
    assert state["revision"] == 2, "一次接受只能产生一个修改单位"
    assert state["undoPointer"] == 1
    assert [row["definition"]["regions"][0]["title"] for row in _units(draft_id)] == ["A", "B"]
    assert _history(draft_id) == ["commit"], "A 不重复、B 只落一个修改单位"

    # 本轮结束后锁放掉，同对象可以继续正常编辑。
    assert draft_workspace.lock_owner(draft_id) is None


def test_resumed_turn_does_not_duplicate_already_committed_unit(
    probe_env, client, scripted_agent
):
    """续跑不重复已提交阶段：同一份内容再落一次，不产生第二个修改单位。"""
    draft_id = _accepted_draft(client, scripted_agent)
    task_id, _owner = _start_stopped_task(draft_id)
    client.post(f"/api/draft-tasks/{task_id}/stop")
    client.post(f"/api/draft-tasks/{task_id}/resume")

    scripted_agent.push(_response(_definition(2, "B")))
    continued = _post_stream(client, "生成报表", message_id="msg_b")
    assert continued.status_code == 200, continued.text
    _sse_final(continued)
    assert len(_units(draft_id)) == 2
    assert _history(draft_id) == ["commit"]

    # 同一轮再发一次（同样的内容）→ 仍然只有两个修改单位。
    scripted_agent.push(_response(_definition(2, "B")))
    again = _post_stream(client, "生成报表", message_id="msg_b")
    assert again.status_code == 200, again.text
    _sse_final(again)
    assert len(_units(draft_id)) == 2, "重复内容不能再造一个修改单位"
    assert _history(draft_id) == ["commit"]
    state = client.get(f"/api/drafts/{draft_id}").json()
    assert state["revision"] == 2


def test_discard_returns_to_previous_unit_and_unlocks_input(
    probe_env, client, scripted_agent
):
    """走「撤销」：回到 A 的定义 / 状态 / 提示，输入解锁，不退 A 的版本现场。"""
    draft_id = _accepted_draft(client, scripted_agent)
    task_id, _owner = _start_stopped_task(draft_id)
    client.post(f"/api/draft-tasks/{task_id}/stop")

    discarded = client.post(f"/api/draft-tasks/{task_id}/discard")
    assert discarded.status_code == 200, discarded.text
    body = discarded.json()
    assert body["taskId"] == task_id
    assert body["state"] == "ROLLED_BACK"
    assert body["draftId"] == draft_id
    # 回到 A 的定义 / 状态 / 提示 / 菜单
    assert body["definition"]["regions"][0]["title"] == "A"
    assert body["businessState"] == "WAITING_USER_CONFIRM"
    assert body["prompt"] == "生成报表"
    assert "pendingChoice" in body
    assert "pendingEditPlan" in body
    assert "reportPreviewPayload" in body
    assert "resolvedTheme" in body
    assert body["canUndo"] is False
    assert body["canRedo"] is False
    # 不退 A 的版本现场
    assert body["revision"] == 1
    # 输入解锁：写锁放掉，同对象写恢复
    assert draft_workspace.lock_owner(draft_id) is None
    saved = client.post(f"/api/drafts/{draft_id}/actions", json={"action": "SAVE"})
    assert saved.status_code == 200, saved.text


def test_discard_does_not_release_a_newer_lock(probe_env, client, scripted_agent):
    """旧任务的撤销不能把**新任务**的写锁放掉。"""
    draft_id = _accepted_draft(client, scripted_agent)
    old_task, _owner = _start_stopped_task(draft_id, message_id="msg_b")
    client.post(f"/api/draft-tasks/{old_task}/stop")

    # 用户先撤销了 B → 锁放掉；随后新一轮接手同一个对象。
    discarded = client.post(f"/api/draft-tasks/{old_task}/discard")
    assert discarded.status_code == 200, discarded.text
    new_owner = "chat:sess_probe:msg_c"
    assert draft_workspace.acquire(draft_id, new_owner) is True

    # B 的撤销**迟到**了一次：不能把 C 的锁放掉。
    late = client.post(f"/api/draft-tasks/{old_task}/discard")
    assert late.status_code == 200, late.text
    assert draft_workspace.lock_owner(draft_id) == new_owner, "撤销放掉了别人的锁"


# ---------- R03：接受的事务一致性与停止后的迟到写入 ----------

def test_workspace_write_failure_does_not_half_commit(
    probe_env, client, scripted_agent, monkeypatch
):
    """工作区写失败：会话 / 候选 / 定义 / 历史都不半提交，且可重试。"""
    draft_id = _accepted_draft(client, scripted_agent)
    before_state = client.get(f"/api/drafts/{draft_id}").json()
    before_snapshot = session_service.load_snapshot(SESSION_ID)
    before_messages = len(session_service.repository.list_messages(SESSION_ID))

    def boom(*_args, **_kwargs):
        raise RuntimeError("workspace write failed")

    original_commit = draft_workspace.commit_edit
    monkeypatch.setattr(draft_workspace, "commit_edit", boom)

    scripted_agent.push(_response(_definition(2, "B")))
    failed = _post_chat(client, "改一下", message_id="msg_b")
    assert failed.status_code >= 500, "失败不许返回成功 final"
    # 只还原这一个补丁：**不能**用 monkeypatch.undo()（它会把共享夹具里
    # 换库的补丁一起撤掉，工作区就指回用户库了）。
    monkeypatch.setattr(draft_workspace, "commit_edit", original_commit)

    # 权威编辑状态一点没变。
    after_state = client.get(f"/api/drafts/{draft_id}").json()
    assert after_state["revision"] == before_state["revision"]
    assert after_state["definition"] == before_state["definition"]
    assert after_state["undoPointer"] == before_state["undoPointer"]
    assert _history(draft_id) == []

    # 会话没被污染：消息没多、快照里的定义没变。
    assert len(session_service.repository.list_messages(SESSION_ID)) == before_messages
    after_snapshot = session_service.load_snapshot(SESSION_ID)
    assert after_snapshot.get("definition") == before_snapshot.get("definition")

    # 可重试：同一轮重发一次，正常落库。
    scripted_agent.push(_response(_definition(2, "B")))
    retried = _post_chat(client, "改一下", message_id="msg_b")
    assert retried.status_code == 200, retried.text
    final_state = client.get(f"/api/drafts/{draft_id}").json()
    assert final_state["revision"] == before_state["revision"] + 1
    assert final_state["definition"]["regions"][0]["title"] == "B"
    assert _history(draft_id) == ["commit"]


def test_late_result_after_stop_writes_nothing(
    probe_env, client, scripted_agent
):
    """停止确认之后，原任务的迟到结果：不写会话、不写工作区、不复活旧菜单。"""
    draft_id = _accepted_draft(client, scripted_agent)
    task_id, _owner = _start_stopped_task(draft_id)
    client.post(f"/api/draft-tasks/{task_id}/stop")

    before_state = client.get(f"/api/drafts/{draft_id}").json()
    before_snapshot = session_service.load_snapshot(SESSION_ID)
    before_messages = len(session_service.repository.list_messages(SESSION_ID))
    before_choice = session_service.load_pending_choice(SESSION_ID)

    # 原任务的迟到结果（同一个 messageId → 同一个任务）。
    scripted_agent.push(_response(_definition(2, "B")))
    late = _post_chat(client, "生成报表", message_id="msg_b")
    assert late.status_code >= 400, "迟到的结果不许当成功 final 返回"

    # 会话没被写。
    assert len(session_service.repository.list_messages(SESSION_ID)) == before_messages
    after_snapshot = session_service.load_snapshot(SESSION_ID)
    assert after_snapshot.get("definition") == before_snapshot.get("definition")
    # 旧菜单没被复活。
    assert session_service.load_pending_choice(SESSION_ID) == before_choice
    # 工作区没被写。
    after_state = client.get(f"/api/drafts/{draft_id}").json()
    assert after_state["revision"] == before_state["revision"]
    assert after_state["definition"] == before_state["definition"]
    assert _history(draft_id) == []
    # 停止现场还在：任务仍是 STOPPED，写锁仍持着（还能选继续 / 撤销）。
    assert draft_workspace.load_task(task_id).state == "STOPPED"
    assert draft_workspace.lock_owner(draft_id) is not None


def test_failed_turn_keeps_pending_candidate_for_retry(
    probe_env, client, scripted_agent, monkeypatch
):
    """落库失败：待处理候选**不丢**，用户还能重试这一轮。"""
    _accepted_draft(client, scripted_agent)
    plan = PendingEditPlan(
        planId="plan_probe",
        draftRevision=1,
        targetRegionId="region_probe",
        operations=[{"intent": "SET_TITLE"}],
        summary="改标题",
    )
    session_service.save_pending_plan(SESSION_ID, plan)
    session_service.save_pending_choice(
        SESSION_ID, {"state": "WAITING_USER_CONFIRM", "question": {"id": "q"}}
    )
    before_plan = session_service.load_pending_plan(SESSION_ID)
    assert before_plan is not None

    def boom(*_args, **_kwargs):
        raise RuntimeError("workspace write failed")

    original_commit = draft_workspace.commit_edit
    monkeypatch.setattr(draft_workspace, "commit_edit", boom)
    scripted_agent.push(_response(_definition(2, "B")))
    failed = _post_chat(client, "改一下", message_id="msg_b")
    assert failed.status_code >= 500
    monkeypatch.setattr(draft_workspace, "commit_edit", original_commit)

    # 候选还在：计划没丢，菜单也没丢。
    after_plan = session_service.load_pending_plan(SESSION_ID)
    assert after_plan is not None
    assert after_plan.planId == "plan_probe"
    assert session_service.load_pending_choice(SESSION_ID) is not None


def test_failed_resume_keeps_stop_site_for_user(
    probe_env, client, scripted_agent, monkeypatch
):
    """续跑失败：停止现场还在（两个选项不消失），用户能继续处理。"""
    draft_id = _accepted_draft(client, scripted_agent)
    task_id, _owner = _start_stopped_task(draft_id)
    client.post(f"/api/draft-tasks/{task_id}/stop")
    resumed = client.post(f"/api/draft-tasks/{task_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["state"] == "RUNNING"

    # 续跑这一轮失败（工作区写失败）。
    def boom(*_args, **_kwargs):
        raise RuntimeError("workspace write failed")

    original_commit = draft_workspace.commit_edit
    monkeypatch.setattr(draft_workspace, "commit_edit", boom)
    scripted_agent.push(_response(_definition(2, "B")))
    failed = _post_stream(client, "生成报表", message_id="msg_b")
    assert failed.status_code == 200
    events = _sse_events(failed.text)
    assert [event for event, _ in events if event == "final"] == [], "失败不许发 final"
    monkeypatch.setattr(draft_workspace, "commit_edit", original_commit)

    # 现场回到 STOPPED：两个选项还在，写锁还持着（不能当什么都没发生）。
    task = draft_workspace.load_task(task_id)
    assert task is not None
    assert task.state == "STOPPED"
    assert draft_workspace.lock_owner(draft_id) is not None
    opened = client.get(f"/api/drafts/{draft_id}").json()
    assert opened["activeTask"]["state"] == "STOPPED"


# ---------- R04：公开写入口的锁与旧版本保护 ----------

def test_locked_draft_rejects_non_stream_chat_and_draft_actions(
    probe_env, client, scripted_agent
):
    """同草稿已有任务持锁时：非流式接受与草稿动作都要被明确拒绝。"""
    draft_id = _accepted_draft(client, scripted_agent)
    _task_id, _owner = _start_stopped_task(draft_id)

    before_state = client.get(f"/api/drafts/{draft_id}").json()
    before_messages = len(session_service.repository.list_messages(SESSION_ID))

    scripted_agent.push(_response(_definition(2, "B")))
    rejected = _post_chat(client, "改一下", message_id="msg_other")
    assert rejected.status_code == 409, rejected.text

    action = client.post(f"/api/drafts/{draft_id}/actions", json={"action": "UNDO"})
    assert action.status_code == 409

    # 拒绝之后定义 / 历史 / 会话都没变。
    after_state = client.get(f"/api/drafts/{draft_id}").json()
    assert after_state["revision"] == before_state["revision"]
    assert after_state["definition"] == before_state["definition"]
    assert _history(draft_id) == []
    assert len(session_service.repository.list_messages(SESSION_ID)) == before_messages


def test_stale_revision_rejects_undo_redo_and_save_as(
    probe_env, client, scripted_agent
):
    """两窗口之一已提交后，另一个持旧 revision 请求撤销 / 重做 / 另存为 → 拒绝。"""
    draft_id = _accepted_draft(client, scripted_agent)
    scripted_agent.push(_response(_definition(2, "B")))
    accepted = _post_chat(client, "改一下", message_id="msg_b")
    assert accepted.status_code == 200, accepted.text
    current = client.get(f"/api/drafts/{draft_id}").json()
    stale = int(current["revision"]) - 1

    before_state = client.get(f"/api/drafts/{draft_id}").json()
    before_history = _history(draft_id)
    before_units = len(_units(draft_id))

    for action in ("UNDO", "REDO", "SAVE_AS"):
        response = client.post(
            f"/api/drafts/{draft_id}/actions",
            json={"action": action, "expectedRevision": stale, "name": "副本"},
        )
        assert response.status_code == 409, f"{action} 没有拒绝旧版本：{response.text}"

    after_state = client.get(f"/api/drafts/{draft_id}").json()
    assert after_state["revision"] == before_state["revision"]
    assert after_state["definition"] == before_state["definition"]
    assert after_state["undoPointer"] == before_state["undoPointer"]
    assert _history(draft_id) == before_history
    assert len(_units(draft_id)) == before_units


def test_current_revision_actions_succeed_after_stop_flow_is_cleared(
    probe_env, client, scripted_agent
):
    """解除停止流程后，正常动作成功。"""
    draft_id = _accepted_draft(client, scripted_agent)
    task_id, _owner = _start_stopped_task(draft_id)
    client.post(f"/api/draft-tasks/{task_id}/stop")
    client.post(f"/api/draft-tasks/{task_id}/discard")

    current = client.get(f"/api/drafts/{draft_id}").json()
    saved = client.post(
        f"/api/drafts/{draft_id}/actions",
        json={"action": "SAVE", "expectedRevision": current["revision"]},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["savedRevision"] is not None


def test_different_drafts_are_independent(probe_env, client, scripted_agent):
    """不同草稿仍能独立编辑（锁不串）。"""
    draft_a = _accepted_draft(client, scripted_agent)
    scripted_agent.push(_response(_definition(1, "B")))
    other = _post_chat(client, "生成报表", message_id="msg_b", session_id="sess_probe_2")
    assert other.status_code == 200, other.text
    draft_b = str(other.json()["domainObjects"]["draftWorkspace"]["draftId"])
    assert draft_b != draft_a

    _task_id, _owner = _start_stopped_task(draft_a)

    # A 被锁着，B 照样能改。
    scripted_agent.push(_response(_definition(2, "B2")))
    edited = _post_chat(client, "改一下", message_id="msg_c", session_id="sess_probe_2")
    assert edited.status_code == 200, edited.text
    state_b = client.get(f"/api/drafts/{draft_b}").json()
    assert state_b["definition"]["regions"][0]["title"] == "B2"
    assert state_b["revision"] == 2
