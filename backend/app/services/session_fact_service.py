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

from app.schemas.fact import (
    LIVE_PROGRESS_CLAIM_MARKERS,
    LIVE_PROGRESS_PERCENT_PATTERN,
    RECORDED_NOT_LIVE_MARKERS,
    RECORDED_STAGE_DISCLOSURE,
    RECORDED_STATE_MARKERS,
    DraftFacts,
    FactStatus,
    SessionFacts,
)
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


def recorded_stage_disclosed(reply: str) -> bool:
    """用户可见答案里是否说清了「已记录、不是实时」这条边界。

    两半都要说才算数：

        - 「这是已记录的状态」（工单 #03：回答明确是已记录状态）；
        - 「不是实时任务监控」（同一条：不冒充实时任务监控）。

    只说一半不行：日常说法（「你的需求已记录」）不含「不是实时」，
    而「没有实时任务状态来源」也不等于说清了这是已记录的状态。
    """
    text = reply or ""
    stated = any(marker in text for marker in RECORDED_STATE_MARKERS)
    not_live = any(marker in text for marker in RECORDED_NOT_LIVE_MARKERS)
    return stated and not_live


def claims_live_progress(reply: str) -> bool:
    """答案里是不是在冒充实时任务监控（编百分比 / 说正在跑）。

    工单 #03 的红线：首版没有实时任务状态来源，也不能编百分比或剩余时间。

    先剥掉否定式说法（「不是实时进度」「没有实时任务状态来源」……）：
    那些是**守住**红线的表述，不是违反红线。
    """
    text = reply or ""
    for marker in RECORDED_NOT_LIVE_MARKERS:
        text = text.replace(marker, "")
    if LIVE_PROGRESS_PERCENT_PATTERN.search(text):
        return True
    return any(marker in text for marker in LIVE_PROGRESS_CLAIM_MARKERS)


def recorded_stage_fallback(facts: SessionFacts) -> str:
    """编造了实时进度时，用服务端事实替换整句回答。

    服务端不能靠「再补一句否认」来抵消一个编出来的进度 ——
    两句话会互相打架，用户读到的是假数字。
    这里直接用服务端记录换掉它：说明下一步，并说清这条边界。
    """
    steps = list(facts.draft.pendingSteps) if facts.draft else []
    head = (
        f"当前对话已记录的下一步：{'、'.join(steps)}。"
        if steps
        else "当前对话已记录了报表的业务阶段。"
    )
    return f"{head}{RECORDED_STAGE_DISCLOSURE}"


def _region_summary(region: dict[str, Any]) -> dict[str, Any]:
    """区块概览：只保留模型判断需要的字段。

    区块是自由字典（``regions: list[dict[str, Any]]``），里面每一项的
    类型没人保证。读不懂的那部分就当没有 —— 一个坏区块不能让整条会话
    的事实读取崩掉（崩掉的话，用户看到的是 500，而不是「无法确认」）。
    """
    component = region.get("component")
    if not isinstance(component, dict):
        component = {}
    metric = component.get("metric")
    if not isinstance(metric, dict):
        metric = {}
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

        forbidden = self.scope_conflict(record, project_id, subject_ref, subject_id)
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
    def scope_conflict(
        record: Any, project_id: str, subject_ref: str, subject_id: str
    ) -> str | None:
        """事实来源与本轮请求范围不一致时返回公开说明，否则返回 None。

        这是共用层的**唯一**范围校验口径。领域模块（进度 / 能力）也复用它，
        避免各自另建一套判定 —— 两处判定漂移过一次：
        进度模块原先只看会话挂没挂项目，没挂项目就放行，
        于是同一条会话在草稿查询里是 FORBIDDEN、在进度查询里却把草稿念了出来。
        """
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
