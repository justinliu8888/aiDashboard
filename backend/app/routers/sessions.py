"""会话与项目管理接口。

会话 id 由后端生成（sessionId），一条对话从建到删永不改变；
conversationId 仍是一次请求线程，停止生成时会换新，两者不要混用。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas.session import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    SessionDetail,
    SessionListResponse,
    SessionMessage,
    SessionSummary,
    UpdateSessionRequest,
    UpdateWorkspaceRequest,
    WorkspaceItem,
)
from app.services.session_service import resolve_placeholders, session_service
from app.storage.models import MessageRecord, SessionRecord, WorkspaceRecord


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["sessions"])


def _to_summary(record: SessionRecord) -> SessionSummary:
    return SessionSummary(
        sessionId=record.id,
        title=record.title,
        state=record.state,
        workspaceId=record.workspaceId,
        messageCount=record.messageCount,
        createdAt=record.createdAt,
        updatedAt=record.updatedAt,
    )


def _to_message(record: MessageRecord) -> SessionMessage:
    return SessionMessage(
        seq=record.seq,
        role=record.role,
        content=record.content,
        payload=record.payload,
        createdAt=record.createdAt,
    )


def _to_workspace(record: WorkspaceRecord) -> WorkspaceItem:
    return WorkspaceItem(
        id=record.id,
        name=record.name,
        description=record.description,
        subjectRef=record.subjectRef,
        subjectId=record.subjectId,
        sessionCount=record.sessionCount,
        createdAt=record.createdAt,
        updatedAt=record.updatedAt,
    )


# ---------- 会话 ----------


@router.post("/sessions", response_model=SessionSummary)
def create_session(payload: CreateSessionRequest) -> SessionSummary:
    """新建会话：只铸 id，不建库行。

    库行在第一轮对话成功完成后随消息一起创建，
    这样侧边栏不会堆积「点开是空的」的会话条目。
    """
    session_id = session_service.new_session_id()
    return SessionSummary(
        sessionId=session_id,
        title=payload.title or "新对话",
        state="ANALYZING",
        workspaceId=payload.workspaceId,
        messageCount=0,
    )


@router.get("/sessions", response_model=SessionListResponse)
def list_sessions(
    workspaceId: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> SessionListResponse:
    records, total = session_service.repository.list_sessions(
        workspaceId=workspaceId, keyword=q, limit=limit, offset=offset
    )
    return SessionListResponse(
        items=[_to_summary(record) for record in records], total=total
    )


@router.get("/sessions/{session_id}", response_model=SessionDetail)
def get_session(session_id: str) -> SessionDetail:
    record = session_service.repository.get_session(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = session_service.repository.list_messages(session_id)
    snapshot = dict(record.snapshot) if record.snapshot else None
    # 待确认计划独立成列。执行计划和授权范围只留在服务端，
    # 会话恢复只需要展示字段。
    pending_plan = _public_pending_plan(record.pendingEditPlan)
    if snapshot and isinstance(snapshot.get("pendingPlan"), dict):
        # 老会话的计划还留在 snapshot 里，兼容读一次。
        pending_plan = pending_plan or _public_pending_plan(snapshot["pendingPlan"])
        snapshot.pop("pendingPlan", None)
    if snapshot and isinstance(snapshot.get("definition"), dict):
        # 老会话的快照里可能还留着 {{roomName}} 之类占位符，
        # 返回前解析一次，恢复出来的标题就是成品文本。
        snapshot = dict(snapshot)
        snapshot["definition"] = resolve_placeholders(snapshot["definition"])
    return SessionDetail(
        sessionId=record.id,
        title=record.title,
        state=record.state,
        workspaceId=record.workspaceId,
        designSessionId=record.designSessionId,
        reportDefinitionId=record.reportDefinitionId,
        previewRunId=record.previewRunId,
        messageCount=record.messageCount,
        snapshot=snapshot,
        pendingChoice=record.pendingChoice,
        pendingEditPlan=pending_plan,
        messages=[_to_message(message) for message in messages],
        createdAt=record.createdAt,
        updatedAt=record.updatedAt,
    )


def _public_pending_plan(plan: Any) -> dict[str, Any] | None:
    """裁掉内部字段，只回传前端要展示的部分。"""
    if not isinstance(plan, dict):
        return None
    return {
        key: plan[key]
        for key in (
            "planId", "draftRevision", "targetRegionId",
            "operations", "summary", "createdAt",
        )
        if key in plan
    }


@router.patch("/sessions/{session_id}", response_model=SessionSummary)
def update_session(session_id: str, payload: UpdateSessionRequest) -> SessionSummary:
    record = session_service.repository.update_session(
        session_id, title=payload.title, workspaceId=payload.workspaceId
    )
    if record is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return _to_summary(record)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str) -> None:
    if not session_service.repository.delete_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")


# ---------- 项目 ----------


@router.get("/workspaces", response_model=list[WorkspaceItem])
def list_workspaces() -> list[WorkspaceItem]:
    return [_to_workspace(record) for record in session_service.list_workspaces()]


@router.post("/workspaces", response_model=WorkspaceItem)
def create_workspace(payload: CreateWorkspaceRequest) -> WorkspaceItem:
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="项目名不能为空")
    record = session_service.create_workspace(
        name=payload.name.strip(),
        description=payload.description,
        subjectRef=payload.subjectRef,
        subjectId=payload.subjectId,
    )
    return _to_workspace(record)


@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceItem)
def update_workspace(
    workspace_id: str, payload: UpdateWorkspaceRequest
) -> WorkspaceItem:
    record = session_service.update_workspace(
        workspace_id, name=payload.name, description=payload.description
    )
    if record is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return _to_workspace(record)


@router.delete("/workspaces/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: str) -> None:
    if not session_service.delete_workspace(workspace_id):
        raise HTTPException(status_code=404, detail="项目不存在")
