"""会话存储的抽象协议。

业务层只依赖这里的 Protocol，不直接依赖 SQLite 实现；
将来换 Postgres / D1 时只需新增一个实现类，调用方不动。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.storage.models import MessageRecord, SessionRecord, WorkspaceRecord


@runtime_checkable
class SessionRepository(Protocol):
    """会话 / 项目 / 消息的持久化接口。"""

    # ---------- 项目 ----------

    def create_workspace(
        self,
        *,
        id: str,
        name: str,
        description: str = "",
        subjectRef: str | None = None,
        subjectId: str | None = None,
    ) -> WorkspaceRecord: ...

    def list_workspaces(self) -> list[WorkspaceRecord]: ...

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None: ...

    def update_workspace(
        self,
        workspace_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> WorkspaceRecord | None: ...

    def delete_workspace(self, workspace_id: str) -> bool: ...

    # ---------- 会话 ----------

    def upsert_session(
        self,
        *,
        session_id: str,
        state: str,
        title: str | None = None,
        workspaceId: str | None = None,
        designSessionId: str | None = None,
        reportDefinitionId: str | None = None,
        previewRunId: str | None = None,
        snapshot: dict[str, Any] | None = None,
        messageCount: int | None = None,
    ) -> SessionRecord: ...

    def set_pending_choice(
        self, session_id: str, choice: dict[str, Any] | None
    ) -> None:
        """写 / 清选择锁。传 None 表示清空（用户已经选过了）。"""

    # ---------- 待确认的编辑计划 ----------
    #
    # 这三个操作必须是**数据库层面的原子操作**，不能靠进程内锁：
    # 多 worker 部署时，同一份计划只允许被取出一次。

    def save_pending_edit_plan(
        self, session_id: str, plan: dict[str, Any]
    ) -> None:
        """写入待确认计划（覆盖已有计划）。"""

    def save_pending_edit_plan_if_absent(
        self, session_id: str, plan: dict[str, Any]
    ) -> dict[str, Any]:
        """写入待确认计划；已有计划时保留旧的并返回它。

        返回实际生效的那份计划（可能是之前已存在的）。
        """

    def take_pending_edit_plan(
        self, session_id: str, plan_id: str
    ) -> dict[str, Any] | None:
        """原子取走指定计划；计划不存在或 planId 不符时返回 None。"""

    def clear_pending_edit_plan(self, session_id: str) -> None:
        """清掉待确认计划。"""

    def get_session(self, session_id: str) -> SessionRecord | None: ...

    def list_sessions(
        self,
        *,
        workspaceId: str | None = None,
        keyword: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SessionRecord], int]: ...

    def update_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        workspaceId: str | None = None,
    ) -> SessionRecord | None: ...

    def delete_session(self, session_id: str) -> bool: ...

    # ---------- 消息 ----------

    def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        payload: dict[str, Any] | None = None,
        clientMsgId: str | None = None,
    ) -> MessageRecord:
        """写入一条消息。

        传入 clientMsgId 时按 (session_id, client_msg_id) 幂等：
        已存在则覆盖内容并返回原记录，不新增。
        """

    def list_messages(self, session_id: str) -> list[MessageRecord]: ...

    def delete_messages_by_client_msg_id(
        self, session_id: str, client_msg_id: str
    ) -> int: ...
