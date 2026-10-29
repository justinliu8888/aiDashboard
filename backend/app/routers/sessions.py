"""会话与项目管理接口。

会话 id 由后端生成（sessionId），一条对话从建到删永不改变；
conversationId 仍是一次请求线程，停止生成时会换新，两者不要混用。
"""
from __future__ import annotations

import logging
from datetime import datetime
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
from app.services.preview_service import (
    PreviewContextError,
    build_report_preview_payload,
    resolve_preview_context,
)
from app.services.session_service import resolve_placeholders, session_service
from app.services.simulated_data_service import SimulatedDataError
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


def _with_report_preview_payload(
    record: SessionRecord, snapshot: dict[str, Any] | None
) -> dict[str, Any] | None:
    """恢复会话时**重新生成**示意载荷，临时挂在 ``snapshot`` 上。

    ADR 0004 的存储边界：模拟业务数据只在响应与内存里，**不落盘**。
    所以恢复时按「定义 + 冻结的时间窗」重新生成一遍（确定性种子：
    同一定义 + 同一窗口 → 数字完全一致），返回给前端；这一份
    **不写回持久化快照**。

    - 快照里已有 ``previewContext`` → 原样复用（同一定义版本跨天、刷新、
      重启都复用同一份，数字不跳）；
    - 历史会话没有上下文 → 用会话**已有的创建时间**只读推导一份，
      **不自动确认、不迁移、不写回**；
    - 没有有效定义 → 不返回无关的示意。
    """
    if not isinstance(snapshot, dict):
        return snapshot
    definition = snapshot.get("definition")
    if not isinstance(definition, dict) or not definition.get("regions"):
        # 没有有效定义就不返回无关示意（也不凭空造一份上下文）。
        return snapshot
    plan = snapshot.get("reportPlan") if isinstance(snapshot.get("reportPlan"), dict) else None
    existing = snapshot.get("previewContext")
    if isinstance(existing, dict):
        context = resolve_preview_context(
            definition, plan=plan, existing=existing
        )
    else:
        # 历史会话没有上下文：按**已有的创建时间**只读推导。用创建时间
        # 而不是「此刻」—— 它是稳定的，推导出的时间窗跨天、跨重启都一样，
        # 数字不会因为刷新一次就跳。
        context = resolve_preview_context(
            definition,
            plan=plan,
            existing=None,
            anchor_time=_session_anchor(record.createdAt),
        )
    try:
        payload = build_report_preview_payload(definition, context, plan=plan)
    except (SimulatedDataError, PreviewContextError) as exc:
        logger.warning("[sessions] 恢复会话时示意载荷生成失败：%s", exc)
        return snapshot
    result = dict(snapshot)
    result["reportPreviewPayload"] = payload
    return result


def _session_anchor(created_at: str) -> datetime | None:
    """会话创建时间（读不出来就 None → 按「此刻」处理）。"""
    if not created_at:
        return None
    try:
        return datetime.fromisoformat(created_at)
    except ValueError:
        return None


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
    snapshot = _with_report_preview_payload(record, snapshot)
    # 模板态的试运行结果**不落库**（ADR 0004：模拟业务数据只存在于响应与内存），
    # 读会话时按**内存里**的运行结果挂回响应 —— 响应里可以有模拟数据，
    # 持久化快照里不行。非模板态的既有链路原样保留库里那份。
    snapshot = session_service.restore_trial_preview_payload(snapshot)
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
