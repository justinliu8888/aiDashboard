"""可信的当前会话事实读取。

只读问答的事实来源必须是**服务端会话记录**，不是本轮请求带回来的快照：

    - 请求快照（``request.snapshots``）是客户端的界面上下文，可能缺失、
      过旧，甚至和库里不一致。快照缺失**不能**当成「当前对话没有草稿」。
    - 服务端会话记录（SQLite ``sessions`` 行 + ``snapshot``）才是已落库的
      草稿、业务阶段和待确认操作。

这里只读不写：查询不创建草稿、不改草稿、不碰待确认计划，也不消耗修改澄清轮次。

结果用 ``app.schemas.fact.FactStatus`` 区分「核实到了 / 确认没有 / 读不到 /
范围不符」四种情况，让调用方能如实回答，而不是把失败说成没有。
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from app.schemas.fact import DraftFacts, FactStatus, SessionFacts
from app.schemas.report_definition import ReportDefinitionDraft
from app.services.scope_service import is_subject_authorized
from app.services.session_service import session_service


logger = logging.getLogger(__name__)


#: 已记录的业务阶段 -> 这份草稿还差什么才能往下走。
#
#: 这里描述的是**草稿层面**还差什么（工单 #13 要回答的「待完成步骤」）。
#: 「做到哪一步了」这种进度口径由进度领域模块负责，不要在这里扩写成
#: 实时任务监控、百分比或预计完成时间。
_STAGE_PENDING_STEPS: dict[str, list[str]] = {
    "ANALYZING": ["描述报表需求，生成报表草稿"],
    "NEEDS_CLARIFICATION": ["补充还缺的条件，生成报表草稿"],
    "BLOCKED_OR_UNSUPPORTED": [],
    "INTERRUPT_CONFIRMING": ["确认是否打断当前流程"],
    "DEFINITION_DRAFT_READY": ["确认这份修改方案"],
    "WAITING_USER_CONFIRM": ["确认这份报表草稿"],
    "USER_EDITING_TEMPLATE": ["确认当前编辑后的草稿"],
    "SAVE_DEFINITION_DRAFT": ["保存报表草稿"],
    "VALIDATING": ["校验报表定义"],
    "VALIDATION_FAILED": ["按校验问题修复草稿"],
    "PREVIEWING": ["生成报表预览"],
    "PREVIEW_READY": ["渲染报表界面"],
    "RENDERING": ["渲染报表界面"],
    "READY": [],
}


def _region_summary(region: dict[str, Any]) -> dict[str, Any]:
    """区块概览：只保留模型判断需要的字段。"""
    component = region.get("component") or {}
    metric = component.get("metric") or {}
    return {
        "regionId": region.get("id", ""),
        "name": region.get("name", ""),
        "role": region.get("role", ""),
        "componentType": component.get("type", ""),
        "metricRef": metric.get("id"),
        "metricLabel": metric.get("label"),
    }


def _public_pending_plan(raw: Any) -> dict[str, Any] | None:
    """待确认修改计划的公开摘要：只留展示字段，执行计划和授权范围留在服务端。"""
    if not isinstance(raw, dict):
        return None
    return {
        key: raw[key]
        for key in (
            "planId", "draftRevision", "targetRegionId",
            "operations", "summary", "createdAt",
        )
        if key in raw
    }


class SessionFactService:
    """按 sessionId 读取服务端会话事实。"""

    def load(
        self,
        *,
        session_id: str | None,
        project_id: str = "",
        subject_ref: str = "",
        subject_id: str = "",
        request_has_draft: bool = False,
    ) -> SessionFacts:
        """读取当前对话的事实。

        参数
            session_id          当前会话 id；为空表示没有会话标识。
            project_id /
            subject_ref /
            subject_id         本轮请求的授权范围，用于判断事实来源是否越界。
            request_has_draft  本轮请求是否带了草稿快照。只用来区分
                               「新建对话（确实没有草稿）」和
                               「页面有草稿但服务端记录查不到（不可确认）」，
                               **不作为草稿存在与否的依据**。
        """
        if not session_id:
            return SessionFacts(
                status=FactStatus.UNAVAILABLE,
                sessionExists=False,
                reason="没有会话标识，无法核对当前对话的记录",
            )

        try:
            record = session_service.repository.get_session(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[session_fact] 读取会话记录失败 sessionId=%s", session_id)
            return SessionFacts(
                status=FactStatus.UNAVAILABLE,
                sessionExists=False,
                reason=f"读取当前对话的服务端记录失败，暂时无法确认：{exc}",
            )

        if record is None:
            if request_has_draft:
                # 页面上有草稿、服务端却查不到记录：两边不一致，不能说「没有」。
                return SessionFacts(
                    status=FactStatus.UNAVAILABLE,
                    sessionExists=False,
                    reason=(
                        "页面上显示有报表草稿，但当前对话的服务端记录查不到，"
                        "暂时无法确认草稿状态"
                    ),
                )
            return SessionFacts(
                status=FactStatus.ABSENT,
                sessionExists=False,
                reason="当前对话还没有服务端记录，也没有已保存的报表草稿",
            )

        forbidden = self._scope_conflict(record, project_id, subject_ref, subject_id)
        if forbidden is not None:
            return SessionFacts(
                status=FactStatus.FORBIDDEN,
                sessionExists=True,
                reason=forbidden,
            )

        snapshot = dict(record.snapshot or {})
        stage = record.state or None

        draft: DraftFacts | None = None
        raw_definition = snapshot.get("definition")
        if isinstance(raw_definition, dict):
            try:
                definition = ReportDefinitionDraft.model_validate(raw_definition)
            except ValidationError as exc:
                logger.warning(
                    "[session_fact] 服务端草稿无法解析 sessionId=%s err=%s",
                    session_id, exc,
                )
                return SessionFacts(
                    status=FactStatus.UNAVAILABLE,
                    sessionExists=True,
                    businessStage=stage,
                    draft=DraftFacts(
                        status=FactStatus.UNAVAILABLE,
                        exists=False,
                        source="server_session",
                    ),
                    reason="服务端保存的报表草稿无法解析，暂时无法确认草稿内容",
                )
            draft = self._build_draft(definition, stage, snapshot)
        else:
            # 会话记录读到了，里面确实没有草稿：这是「确认没有」，不是读不到。
            draft = DraftFacts(
                status=FactStatus.ABSENT,
                exists=False,
                source="server_session",
            )

        pending_plan = _public_pending_plan(record.pendingEditPlan) or (
            _public_pending_plan(snapshot.get("pendingPlan"))
        )
        pending_choice = (
            dict(record.pendingChoice) if isinstance(record.pendingChoice, dict) else None
        )
        saved_definition = self._saved_definition(record.reportDefinitionId)
        preview = self._preview_fact(snapshot)

        steps = list(_STAGE_PENDING_STEPS.get(stage or "", []))
        if pending_plan:
            steps.append("处理待确认的修改计划")

        return SessionFacts(
            status=FactStatus.FOUND,
            sessionExists=True,
            businessStage=stage,
            draft=draft,
            pendingEditPlan=pending_plan,
            pendingChoice=pending_choice,
            savedDefinition=saved_definition,
            preview=preview,
            clarificationRounds=self._clarification_rounds(snapshot),
            reason="" if draft.exists else "当前对话没有报表草稿",
            definition=self._definition_object(snapshot),
            policy=self._policy_object(snapshot),
        )

    # ---------- 内部 ----------

    @staticmethod
    def _scope_conflict(
        record: Any, project_id: str, subject_ref: str, subject_id: str
    ) -> str | None:
        """事实来源与本轮请求范围不一致时返回公开说明，否则返回 None。"""
        snapshot = record.snapshot or {}
        stored = snapshot.get("scope")
        if isinstance(stored, dict):
            pairs = (
                ("projectId", project_id),
                ("subjectRef", subject_ref),
                ("subjectId", subject_id),
            )
            for key, current in pairs:
                expected = stored.get(key)
                if expected and current and expected != current:
                    return (
                        "当前请求的范围与这条会话记录不一致，"
                        "无法确认其中的草稿与状态"
                    )
        if subject_id and not is_subject_authorized(subject_ref, subject_id):
            return "当前请求的业务对象不在授权范围内，无法确认其中的草稿与状态"
        return None

    @staticmethod
    def _build_draft(
        definition: ReportDefinitionDraft, stage: str | None, snapshot: dict[str, Any]
    ) -> DraftFacts:
        regions = definition.regions or []
        metadata = definition.metadata or {}
        preview = SessionFactService._preview_fact(snapshot)
        return DraftFacts(
            status=FactStatus.FOUND,
            exists=True,
            reportName=str(metadata.get("name") or ""),
            revision=int(metadata.get("revision") or 0),
            regionCount=len(regions),
            regions=[_region_summary(region) for region in regions],
            pendingSteps=list(_STAGE_PENDING_STEPS.get(stage or "", [])),
            savedDefinition=None,
            preview=preview,
            source="server_session",
        )

    @staticmethod
    def _preview_fact(snapshot: dict[str, Any]) -> dict[str, Any] | None:
        """已生成的报表预览：只报运行标识和状态，不报数据。"""
        last_run = snapshot.get("lastReportRun")
        if not isinstance(last_run, dict):
            return None
        return {
            "reportRunId": str(last_run.get("reportRunId") or ""),
            "status": str(last_run.get("status") or ""),
            "logicalTime": last_run.get("logicalTime"),
        }

    @staticmethod
    def _saved_definition(definition_id: str | None) -> dict[str, Any] | None:
        """已保存的定义。

        保存服务当前是进程内存储，重启后可能读不到；读不到就报「不可得」，
        不能当成「没有保存过」。
        """
        if not definition_id:
            return None
        from app.services.definition_save_service import definition_save_service

        saved = definition_save_service.get(definition_id)
        if saved is None:
            return {"definitionId": definition_id, "revision": None, "available": False}
        return {
            "definitionId": saved.definitionId,
            "revision": saved.revision,
            "available": True,
        }

    @staticmethod
    def _clarification_rounds(snapshot: dict[str, Any]) -> int:
        try:
            return int(snapshot.get("clarificationCount") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _definition_object(snapshot: dict[str, Any]) -> ReportDefinitionDraft | None:
        """服务端保存的完整草稿对象（响应组装用，不进模型上下文）。"""
        raw = snapshot.get("definition")
        if not isinstance(raw, dict):
            return None
        try:
            return ReportDefinitionDraft.model_validate(raw)
        except ValidationError:
            return None

    @staticmethod
    def _policy_object(snapshot: dict[str, Any]) -> Any | None:
        """服务端保存的策略草稿。"""
        raw = snapshot.get("policy")
        if not isinstance(raw, dict):
            return None
        from app.schemas.report_policy import ReportPolicyDraft

        try:
            return ReportPolicyDraft.model_validate(raw)
        except ValidationError:
            return None


session_fact_service = SessionFactService()


__all__ = ["SessionFactService", "session_fact_service"]
