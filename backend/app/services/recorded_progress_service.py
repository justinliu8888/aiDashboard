"""已记录进度事实模块（工单 #15）。

回答「刚才那份做到哪了？」时的事实来源是**服务端会话记录**：
当前 sessionId 对应的持久化业务阶段、报表草稿、待确认修改计划、
已保存定义与报表预览。

三条硬约束：

    1. **只读**：只查不写。不保存、不校验、不生成预览、不推进业务。
       模块不调用任何会写库的会话接口 —— 包括名字带 load 但实际会
       迁移旧快照的 ``load_pending_plan``。
    2. **只报已记录**：没有实时任务状态来源，就不报百分比、不估剩余时间。
    3. **分清「没有」和「读不到」**：读取失败不能回答成「当前对话没有」。

事实结果的四种可核实状态（``availability``）：

    SUCCESS             读到了记录，且当前对话有可回报的业务对象
    NO_OBJECT           读到了记录，但当前对话确实没有报表对象
    UNAVAILABLE         读不到记录（会话不存在 / 读取失败 / 记录损坏）
    PERMISSION_DENIED   无权查看这个对话
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from app.schemas.session import PENDING_PLAN_KEY, PendingEditPlan
from app.services.definition_save_service import definition_save_service
from app.services.preview_service import preview_service
from app.services.session_service import session_service


logger = logging.getLogger(__name__)


#: 已记录进度事实契约版本。
#:
#: 接入方（工具注册、回答提交契约、前端展示）按这个版本对接。
#: 结果结构变化时递增；新增可选字段也算一次版本变化。
RECORDED_PROGRESS_CONTRACT_VERSION = "1.0"


# ---------- 事实结果的四种可核实状态 ----------

AVAILABILITY_SUCCESS = "SUCCESS"
AVAILABILITY_NO_OBJECT = "NO_OBJECT"
AVAILABILITY_UNAVAILABLE = "UNAVAILABLE"
AVAILABILITY_PERMISSION_DENIED = "PERMISSION_DENIED"


# ---------- 已记录的业务阶段 ----------

STAGE_UNKNOWN = "UNKNOWN"
STAGE_NO_OBJECT = "NO_OBJECT"
STAGE_REQUIREMENT_GATHERING = "REQUIREMENT_GATHERING"
STAGE_DRAFT_PENDING = "DRAFT_PENDING"
STAGE_DRAFT_EDITING = "DRAFT_EDITING"
STAGE_PENDING_CONFIRM = "PENDING_CONFIRM"
STAGE_SAVED = "SAVED"
STAGE_VALIDATING = "VALIDATING"
STAGE_VALIDATION_FAILED = "VALIDATION_FAILED"
STAGE_PREVIEWING = "PREVIEWING"
STAGE_PREVIEW_READY = "PREVIEW_READY"
STAGE_COMPLETED = "COMPLETED"
STAGE_BLOCKED = "BLOCKED"
STAGE_INTERRUPT_CONFIRMING = "INTERRUPT_CONFIRMING"

#: 服务端业务状态 → 已记录阶段。
#:
#: 只映射「已记录」的阶段语义，不引入实时执行阶段：
#: VALIDATING / PREVIEWING 表示流程**走到**这一步，不是此刻正在跑。
_STATE_STAGES: dict[str, tuple[str, str]] = {
    "ANALYZING": (STAGE_REQUIREMENT_GATHERING, "正在收集报表需求"),
    "NEEDS_CLARIFICATION": (STAGE_REQUIREMENT_GATHERING, "在等你补充报表条件"),
    "DEFINITION_DRAFT_READY": (STAGE_DRAFT_PENDING, "草稿已生成，等你确认"),
    "WAITING_USER_CONFIRM": (STAGE_DRAFT_PENDING, "草稿已生成，等你确认"),
    "USER_EDITING_TEMPLATE": (STAGE_DRAFT_EDITING, "草稿编辑中"),
    "SAVE_DEFINITION_DRAFT": (STAGE_SAVED, "草稿已保存，等校验"),
    "VALIDATING": (STAGE_VALIDATING, "已走到校验这一步"),
    "VALIDATION_FAILED": (STAGE_VALIDATION_FAILED, "校验未通过"),
    "PREVIEWING": (STAGE_PREVIEWING, "已走到生成预览这一步"),
    "PREVIEW_READY": (STAGE_PREVIEW_READY, "预览已就绪"),
    "RENDERING": (STAGE_PREVIEW_READY, "预览已就绪"),
    "READY": (STAGE_COMPLETED, "报表已完成"),
    "BLOCKED_OR_UNSUPPORTED": (STAGE_BLOCKED, "操作已被拦截"),
    "INTERRUPT_CONFIRMING": (STAGE_INTERRUPT_CONFIRMING, "等你确认是否打断当前流程"),
}

#: 已记录阶段 → 可确定的下一步。
#:
#: 下一步只是**说明**，模块自身不执行任何推进动作。
_STAGE_NEXT_STEPS: dict[str, str] = {
    STAGE_REQUIREMENT_GATHERING: "告诉我要生成哪份报表，我来接着做。",
    STAGE_DRAFT_PENDING: "确认这份草稿，或者告诉我要改哪里。",
    STAGE_DRAFT_EDITING: "继续说明要修改的地方，或者确认当前草稿。",
    STAGE_PENDING_CONFIRM: "接受或调整这份待确认的修改计划。",
    STAGE_SAVED: "等待校验结果。",
    STAGE_VALIDATING: "等待校验结果。",
    STAGE_VALIDATION_FAILED: "看看校验问题，采纳修正后重新校验。",
    STAGE_PREVIEWING: "等待预览生成完成。",
    STAGE_PREVIEW_READY: "查看预览内容。",
    STAGE_COMPLETED: "查看已完成的报表，或者继续修改。",
    STAGE_BLOCKED: "看一下拦截原因，再决定下一步。",
    STAGE_INTERRUPT_CONFIRMING: "确认是否要打断当前流程。",
}

#: 查询范围固定为当前对话。不自动遍历历史，不声称检索过所有会话。
SCOPE_CURRENT_CONVERSATION = "CURRENT_CONVERSATION"


def describe_recorded_progress(
    session_id: str | None,
    *,
    subject_ref: str | None = None,
    subject_id: str | None = None,
) -> dict[str, Any]:
    """回报当前对话**已记录**的业务阶段与相关对象。

    返回一个可直接交给模型观察或写进响应的事实字典。

    ``subject_ref`` / ``subject_id`` 是当前请求的业务对象范围。
    会话归属的项目属于别的业务对象时，不回报它的内容。
    """
    if not session_id:
        return _unavailable_result("没有绑定当前对话，无法确认已记录进度。")

    record, failure = _read_session_record(session_id)
    if record is None:
        return _unavailable_result(failure)

    denied = _check_permission(record, subject_ref, subject_id)
    if denied:
        return _permission_denied_result(denied)

    facts = _extract_facts(record, session_id)
    if not _has_any_object(facts):
        return _no_object_result(record, facts)

    return _success_result(record, facts)


#: 没有查看权限时的统一说法。不说对方是谁、不说里面有什么。
PERMISSION_DENIED_REASON = "当前账号没有查看这个对话的权限。"


# ---------- 读取服务端记录 ----------


def _read_session_record(session_id: str) -> tuple[Any | None, str]:
    """读服务端会话记录。返回 (记录, 失败原因)。

    只走只读接口：``repository.get_session`` 不会改库。
    """
    try:
        record = session_service.repository.get_session(session_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[recorded_progress] 读取会话记录失败 sessionId=%s", session_id, exc_info=True
        )
        return None, "服务端会话记录读取失败，无法确认当前进度。"
    if record is None:
        return None, "找不到这个对话的服务端记录，无法确认当前进度。"
    return record, ""


def _check_permission(
    record: Any, subject_ref: str | None, subject_id: str | None
) -> str | None:
    """会话归属的业务对象不在当前范围内时拒绝。返回拒绝原因，None 表示放行。

    会话没挂项目（绝大多数对话）时无从判断，放行；
    挂了项目就看项目绑的是哪个业务对象。
    """
    workspace_id = getattr(record, "workspaceId", None)
    if not workspace_id:
        return None
    try:
        workspace = session_service.repository.get_workspace(workspace_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[recorded_progress] 读取项目失败 workspaceId=%s", workspace_id, exc_info=True
        )
        return None
    if workspace is None:
        return None
    if workspace.subjectRef and subject_ref and workspace.subjectRef != subject_ref:
        return PERMISSION_DENIED_REASON
    if workspace.subjectId and subject_id and workspace.subjectId != subject_id:
        return PERMISSION_DENIED_REASON
    return None


# ---------- 事实抽取 ----------


def _extract_facts(record: Any, session_id: str | None = None) -> dict[str, Any]:
    """把服务端会话记录拆成各类业务对象的事实。

    草稿、待确认修改计划、已保存定义、报表预览分开处理，
    草稿 revision 与保存 revision 不混用。
    """
    snapshot = record.snapshot if isinstance(record.snapshot, dict) else {}

    definition = snapshot.get("definition")
    if isinstance(definition, dict):
        metadata = definition.get("metadata") or {}
        regions = definition.get("regions") or []
        draft = {
            "exists": True,
            "name": str(metadata.get("name") or ""),
            "revision": _as_int(metadata.get("revision")),
            "regionCount": len(regions),
        }
    else:
        draft = {
            "exists": False,
            "name": "",
            "revision": None,
            "regionCount": 0,
        }

    saved_definition = _extract_saved_definition(record)

    last_run = snapshot.get("lastReportRun")
    if isinstance(last_run, dict):
        preview = {
            "exists": True,
            "reportRunId": last_run.get("reportRunId"),
            "status": last_run.get("status"),
            "logicalTime": last_run.get("logicalTime"),
            "windowLabel": last_run.get("windowLabel"),
            "contentAvailable": _preview_content_available(
                last_run.get("reportRunId"), snapshot
            ),
            "matchesCurrentDraft": None,
        }
    else:
        preview = {
            "exists": False,
            "reportRunId": record.previewRunId,
            "status": None,
            "logicalTime": None,
            "windowLabel": None,
            "contentAvailable": False,
            "matchesCurrentDraft": None,
        }
    preview["matchesCurrentDraft"] = _resolve_preview_matches_draft(
        draft, saved_definition
    )

    return {
        "draft": draft,
        "savedDefinition": saved_definition,
        "preview": preview,
        "pendingEditPlan": _extract_pending_plan(record),
        "pendingChoice": _extract_pending_choice(record),
        "clarification": _extract_clarification(session_id),
    }


def _extract_pending_choice(record: Any) -> dict[str, Any]:
    """读当前挂着的必选菜单（选择锁）。

    它和待确认修改计划是两回事：一个是「必须从下拉里选」，
    一个是「有一份改动等你接受」。分别回报，不合并。
    """
    raw = getattr(record, "pendingChoice", None)
    if not isinstance(raw, dict):
        return {"exists": False, "questionId": None, "state": None}
    question = raw.get("question")
    question_id = question.get("id") if isinstance(question, dict) else None
    return {
        "exists": True,
        "questionId": question_id,
        "state": raw.get("state"),
    }


def _extract_clarification(session_id: str | None) -> dict[str, Any]:
    """读同一项待定修改的澄清进度。

    ``session_service.load_clarification`` 只读库，不写。
    """
    if not session_id:
        return {"exists": False, "rounds": 0, "maxRounds": 0, "lastQuestion": ""}
    try:
        clarification = session_service.load_clarification(session_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[recorded_progress] 读取澄清进度失败 sessionId=%s", session_id, exc_info=True
        )
        return {"exists": False, "rounds": 0, "maxRounds": 0, "lastQuestion": ""}
    if clarification is None:
        return {"exists": False, "rounds": 0, "maxRounds": 0, "lastQuestion": ""}
    return {
        "exists": True,
        "rounds": clarification.rounds,
        "maxRounds": clarification.maxRounds,
        "lastQuestion": clarification.lastQuestion,
    }


def _extract_saved_definition(record: Any) -> dict[str, Any]:
    """读已保存定义的事实。

    已保存定义在进程内存储（``definition_save_service``），后端重启后
    会话记录里的保存标识还在，但内容读不到了。
    这时要分清两件事：保存**发生过**（记录里有标识），
    保存**内容现在还读得到**（服务里查得到）。
    """
    recorded_id = record.reportDefinitionId
    if not recorded_id:
        return {
            "exists": False,
            "definitionId": None,
            "revision": None,
            "contentAvailable": False,
        }
    saved = None
    try:
        saved = definition_save_service.get(recorded_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[recorded_progress] 读取已保存定义失败 definitionId=%s",
            recorded_id,
            exc_info=True,
        )
    return {
        "exists": True,
        "definitionId": recorded_id,
        "revision": saved.revision if saved is not None else None,
        "contentAvailable": saved is not None,
    }


def _preview_content_available(run_id: Any, snapshot: dict[str, Any]) -> bool:
    """预览结果现在还读得到吗。"""
    if snapshot.get("previewPayload"):
        return True
    if not run_id:
        return False
    try:
        return preview_service.get(str(run_id)) is not None
    except Exception:  # noqa: BLE001
        logger.warning("[recorded_progress] 读取预览记录失败 runId=%s", run_id)
        return False


def _resolve_preview_matches_draft(
    draft: dict[str, Any], saved_definition: dict[str, Any]
) -> bool | None:
    """这次预览对应的是不是当前这份草稿。

    判据：草稿 revision 与**保存** revision 的先后。
        - 保存版本读不到 → 无法确认（None），不能推断成「一样」；
        - 草稿比保存版本新 → 预览是旧版本的（False）；
        - 其余 → 预览对应当前草稿（True）。

    草稿 revision 和保存 revision 各有各的用处，这里只做比较，不互相替代。
    """
    saved_revision = saved_definition.get("revision")
    if saved_revision is None:
        return None
    if not draft.get("exists"):
        return True
    draft_revision = draft.get("revision")
    if draft_revision is None:
        return None
    return draft_revision <= saved_revision


def _extract_pending_plan(record: Any) -> dict[str, Any]:
    """读待确认修改计划。

    **只读**：不走 ``session_service.load_pending_plan`` —— 那个接口在读到
    老会话快照里的计划时会顺手迁移并写库，本模块不能改任何东西。
    这里直接读记录列，老位置做一次纯读兜底。
    """
    raw = record.pendingEditPlan if isinstance(record.pendingEditPlan, dict) else None
    if raw is None:
        snapshot = record.snapshot if isinstance(record.snapshot, dict) else {}
        legacy = snapshot.get(PENDING_PLAN_KEY)
        raw = legacy if isinstance(legacy, dict) else None
    if raw is None:
        return {
            "exists": False,
            "planId": None,
            "summary": "",
            "draftRevision": None,
            "targetRegionId": "",
            "unreadable": False,
        }
    try:
        plan = PendingEditPlan.model_validate(raw)
    except ValidationError:
        # 计划存在但读不懂：说「读不懂」，不能说「没有计划」。
        return {
            "exists": True,
            "planId": None,
            "summary": "",
            "draftRevision": None,
            "targetRegionId": "",
            "unreadable": True,
        }
    return {
        "exists": True,
        "planId": plan.planId,
        "summary": plan.summary,
        "draftRevision": plan.draftRevision,
        "targetRegionId": plan.targetRegionId,
        "unreadable": False,
    }


def _has_any_object(facts: dict[str, Any]) -> bool:
    """当前对话是否有任何可回报的报表对象。"""
    return any(
        facts[key]["exists"]
        for key in ("draft", "savedDefinition", "preview", "pendingEditPlan")
    )


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------- 结果组装 ----------


def _base_result(
    *,
    availability: str,
    stage: str,
    stage_label: str,
    reason: str,
    recorded_state: str | None,
    facts: dict[str, Any],
    next_step: str,
    fact_lines: list[str] | None = None,
) -> dict[str, Any]:
    result = {
        "answerKind": "recorded_progress",
        "contractVersion": RECORDED_PROGRESS_CONTRACT_VERSION,
        "availability": availability,
        "scope": SCOPE_CURRENT_CONVERSATION,
        "stage": stage,
        "stageLabel": stage_label,
        "recordedState": recorded_state,
        "reason": reason,
        "nextStep": next_step,
        # 首版没有实时任务状态来源：明确说清楚，别让「已记录」被当成「正在跑」。
        "isRealtime": False,
        "realtime": {
            "available": False,
            "reason": "当前没有实时任务状态来源，只能回答已记录的业务阶段。",
        },
        "limitations": list(_LIMITATIONS),
        "facts": list(fact_lines) if fact_lines is not None else _describe_facts(facts),
    }
    result.update(facts)
    return result


#: 每份结果都带的限制说明。
_LIMITATIONS: tuple[str, ...] = (
    "这是服务端已记录的业务阶段，不是实时任务进度。",
    "没有实时任务状态来源，无法给出百分比或预计完成时间。",
)


def _describe_facts(facts: dict[str, Any]) -> list[str]:
    """把结构化事实写成可核实的短句。

    只写服务端记录里有的东西，不写推出来的结论。
    """
    lines: list[str] = []

    draft = facts["draft"]
    if draft["exists"]:
        lines.append(
            f"报表草稿「{draft['name']}」当前版本 {draft['revision']}，"
            f"含 {draft['regionCount']} 个区块。"
        )

    plan = facts["pendingEditPlan"]
    if plan["exists"]:
        summary = plan["summary"] or "（没有摘要）"
        lines.append(
            f"有一份待确认的修改计划：{summary}（计划 {plan['planId']}）。"
            "计划还没被接受，草稿没有被改动。"
        )

    choice = facts["pendingChoice"]
    if choice["exists"]:
        lines.append("有一个必选菜单还挂着，等你从菜单里选一项。")

    saved = facts["savedDefinition"]
    if saved["exists"]:
        if saved["contentAvailable"]:
            lines.append(
                f"已保存定义 {saved['definitionId']}，保存版本 {saved['revision']}。"
            )
        else:
            lines.append(
                f"服务端记录过已保存定义 {saved['definitionId']}，但当前读不到保存内容。"
            )

    preview = facts["preview"]
    if preview["exists"]:
        lines.append(
            f"报表预览 {preview['reportRunId']}（状态 {preview['status']}）已经生成。"
        )
        if preview["matchesCurrentDraft"] is False:
            lines.append("这份预览来自较早的保存版本，不代表当前草稿已经出数。")

    clarification = facts["clarification"]
    if clarification["exists"]:
        lines.append(
            f"同一项待定修改已追问 {clarification['rounds']} 次"
            f"（上限 {clarification['maxRounds']} 次）。"
        )

    return lines


def _unavailable_result(reason: str) -> dict[str, Any]:
    empty = _extract_facts(_EMPTY_RECORD, None)
    return _base_result(
        availability=AVAILABILITY_UNAVAILABLE,
        stage=STAGE_UNKNOWN,
        stage_label="无法确认",
        reason=reason,
        recorded_state=None,
        facts=empty,
        next_step="我无法确认这个对话的已记录进度，请稍后重试或换个说法再问。",
        fact_lines=[reason],
    )


def _permission_denied_result(reason: str) -> dict[str, Any]:
    empty = _extract_facts(_EMPTY_RECORD, None)
    return _base_result(
        availability=AVAILABILITY_PERMISSION_DENIED,
        stage=STAGE_UNKNOWN,
        stage_label="无法确认",
        reason=reason,
        recorded_state=None,
        facts=empty,
        next_step="换个当前账号能查看的对话再问，或者先确认你有这个项目的权限。",
        fact_lines=[reason],
    )


def _no_object_result(record: Any, facts: dict[str, Any]) -> dict[str, Any]:
    return _base_result(
        availability=AVAILABILITY_NO_OBJECT,
        stage=STAGE_NO_OBJECT,
        stage_label="当前对话还没有报表对象",
        reason="当前对话的服务端记录里没有报表草稿、已保存定义或预览。",
        recorded_state=record.state,
        facts=facts,
        next_step="告诉我你想看哪份报表，我来生成。",
        fact_lines=["当前对话的服务端记录里没有报表草稿、已保存定义或预览。"],
    )


def _success_result(record: Any, facts: dict[str, Any]) -> dict[str, Any]:
    stage, stage_label = _resolve_stage(record, facts)
    return _base_result(
        availability=AVAILABILITY_SUCCESS,
        stage=stage,
        stage_label=stage_label,
        reason="",
        recorded_state=record.state,
        facts=facts,
        next_step=_STAGE_NEXT_STEPS.get(stage, "告诉我下一步要做什么。"),
    )


def _resolve_stage(record: Any, facts: dict[str, Any]) -> tuple[str, str]:
    """由服务端记录的状态推出已记录阶段。

    阶段是「流程走到哪一步」，不是「此刻正在跑什么」。
    有待确认的修改计划时，阶段是待确认 —— 这是用户眼下的待办。
    """
    if facts.get("pendingEditPlan", {}).get("exists"):
        return STAGE_PENDING_CONFIRM, "有待确认的修改计划"
    state = str(record.state or "")
    mapped = _STATE_STAGES.get(state)
    if mapped is None:
        return STAGE_UNKNOWN, "已记录阶段（状态未知）"
    return mapped


class _EmptyRecord:
    """读不到记录时占位，让事实结构保持一致。"""

    state = None
    reportDefinitionId = None
    previewRunId = None
    snapshot = None
    pendingEditPlan = None


_EMPTY_RECORD = _EmptyRecord()
