"""AI 对话入口路由：POST /api/agent/chat。"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from time import perf_counter
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.schemas.agent import AgentRequest, AgentResponse, AgentState
from app.services import edit_trace_service
from app.services import report_diagnostics_service as diagnostics
from app.services.agent_service import agent_service
from app.services.draft_workspace import DraftLocked, draft_workspace
from app.services.report_theme_service import theme_service
from app.services.report_log_safety import (
    log_info,
    log_warning,
    user_visible_error_message,
)
from app.services.request_timeline import log_timeline_step, start_timeline
from app.services.session_service import PendingPlanConsumedError, session_service


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])


class _PublicFailure(RuntimeError):
    """对外只说人话的失败。

    内部原因（异常类型、调用栈）已经写进日志。这里抛出去的异常
    **不带异常原文** —— 否则 Web 服务器会把原文写进自己的错误日志，
    用户可见的错误消息里也会夹带上游的技术细节。
    """


class _TurnFenced(RuntimeError):
    """这一轮已经不能落库了（被停掉 / 被撤销 / 锁在别人手里）。

    栅栏在**写会话之前**判：过了栅栏的东西一律不写
    （会话、工作区、旧菜单都不动）。
    """


#: 任务状态 → 人话（给用户看的拒绝原因）。
_TURN_STATE_LABEL = {
    "STOPPED": "被停掉了",
    "ROLLED_BACK": "被撤销了",
    "FAILED": "失败了",
}


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sse(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def _log_failure(message: str, exc: Exception) -> None:
    """记一次失败：**不留异常原文**，只留调用栈。

    上游异常文本可能带凭据或用户数据，所以异常对象换成 ``None``：
    栈还在（能看出哪一行炸的），消息没了。
    """
    request_id = "-"
    state = diagnostics.current_model_call()
    if state is not None:
        request_id = state.request_id
    log_warning(
        logger,
        "%s requestId=%s errorType=%s",
        message,
        request_id,
        type(exc).__name__,
        exc_info=(type(exc), None, exc.__traceback__),
    )


async def _emit_reply_stream(reply: str, emit) -> None:
    if not reply:
        return
    await emit("stage", {"stage": "reply_stream_start"})
    for char in reply:
        await emit("reply_delta", {"delta": char})
        await asyncio.sleep(0.018)
    await emit("stage", {"stage": "reply_stream_done"})


def _lock_owner_of(
    session_key: str, message_id: str | None, task_id: str | None
) -> str:
    """本轮对话的写锁持有者标识：**会话 + 消息 id**。

    同一会话 + 同一消息 id = 同一个持有者。
    锁的授权语义就靠它：**同一持有者允许重入**（这就是续跑），
    别的持有者持锁时照样 409（契约 0002 §7）。
    """
    return f"chat:{session_key}:{message_id or task_id or ''}"


def _acquire_turn_lock(
    session_key: str, request: AgentRequest, draft_id: str | None
) -> tuple[str, str | None]:
    """拿本轮的写锁。拿不到就 409（**不是**只禁前端按钮）。

    判定是**一条语句**（``INSERT ... ON CONFLICT DO NOTHING``）：
    同一个持有者重入会拿到 True（续跑），别的持有者拿到 False。
    """
    task_id = _task_id_of(request)
    lock_owner = _lock_owner_of(
        session_key, (request.messageId or "").strip(), task_id
    )
    if draft_id and not draft_workspace.acquire(draft_id, lock_owner):
        raise HTTPException(
            status_code=409,
            detail="这个草稿正在被另一个窗口修改，请先在那边停止或等它结束。",
        )
    return lock_owner, task_id


def _checkpoint_context(context: Any) -> dict[str, Any]:
    """检查点里带的上下文：能 JSON 化就整块带走，取不到就空字典。

    续跑要重建这一轮，前端才有原文与卡片目标可带；
    这里只负责**如实带走**，不解释、不改写。
    """
    if context is None:
        return {}
    dump = getattr(context, "model_dump", None)
    if callable(dump):
        try:
            return dict(dump())
        except Exception:  # noqa: BLE001 — 检查点不能因为序列化失败把整轮搞挂
            return {}
    if isinstance(context, dict):
        return dict(context)
    return {}


def _begin_turn_task(
    draft_id: str | None,
    task_id: str | None,
    lock_owner: str,
    session_key: str,
    request: AgentRequest,
) -> None:
    """登记本轮任务：检查点里带**服务端本轮身份**（会话 / 消息 / 草稿 / 锁）。

    续跑靠这些身份找回原来那一轮 —— 前端不用自己记，
    刷新之后也还能从库里读回来（契约 0002 §7）。
    """
    if not (draft_id and task_id):
        return
    message_id = (request.messageId or "").strip()
    try:
        draft_workspace.begin_task(
            draft_id,
            task_id=task_id,
            operation_id=message_id,
            checkpoint={
                "sessionId": session_key,
                "messageId": message_id,
                "draftId": draft_id,
                "lockOwner": lock_owner,
                "startedAt": _now_text(),
                # 续跑要能**重做这一轮**（契约 0002 §7）：
                # 刷新之后前端手里没有原请求体，只有身份的话，
                # 点「继续」只能「显示已继续」，没有任务执行。
                # 把原文与上下文一起放进检查点，前端才重建得出这一轮。
                "message": request.message or "",
                # **这一轮当时的状态**也得带上：续跑要重做的是同一轮，
                # 不是「同一句话在别的状态下重来」。
                # 少了它，续跑会被重放到 ANALYZING（生成态），
                # 于是「改这一版」被当成「重新生成一份」—— 改的不是同一个东西。
                "state": getattr(request.state, "value", None) or str(request.state or ""),
                "context": _checkpoint_context(request.context),
            },
        )
    except Exception as exc:  # noqa: BLE001 — 任务登记失败不阻断本轮对话
        logger.warning(
            "[agent.chat] 任务登记失败 taskId=%s draftId=%s errorType=%s",
            task_id,
            draft_id,
            type(exc).__name__,
        )


def _capture_edit_state(session_key: str) -> dict[str, Any]:
    """这一轮开始前的**候选现场**（待确认计划 + 选择锁）。

    落库失败时要能把它放回去 —— 不然用户会看到「待确认的计划不见了」。
    """
    try:
        plan = session_service.load_pending_plan(session_key)
    except Exception:  # noqa: BLE001 — 读不到就当没有
        plan = None
    try:
        choice = session_service.load_pending_choice(session_key)
    except Exception:  # noqa: BLE001
        choice = None
    return {"pendingPlan": plan, "pendingChoice": choice}


def _restore_pending_candidates(
    session_key: str, captured: dict[str, Any] | None
) -> None:
    """把候选现场放回本轮开始前的样子。

    失败的一轮**不算发生过**：它提的候选、它挂的菜单都不算数，
    本轮开始前的那份要原样还在，用户才能重试这一轮。
    """
    if not captured:
        return
    plan = captured.get("pendingPlan")
    try:
        if plan is None:
            session_service.clear_pending_plan(session_key)
        else:
            session_service.save_pending_plan(session_key, plan)
    except Exception:  # noqa: BLE001 — 恢复失败不掩盖原始错误
        logger.warning("[agent.chat] 恢复待确认计划失败 sessionId=%s", session_key)
    choice = captured.get("pendingChoice")
    try:
        session_service.save_pending_choice(session_id=session_key, choice=choice)
    except Exception:  # noqa: BLE001
        logger.warning("[agent.chat] 恢复选择锁失败 sessionId=%s", session_key)


def _abort_turn_task(task_id: str | None, *, reason: str) -> None:
    """这一轮没落成：任务收尾。

    续跑失败 → 回到 **STOPPED**：两个选项还在，用户还能处理这个现场；
    普通失败 → FAILED。已经停掉 / 撤销 / 完成的任务**不动**
    （现场由用户处理，别覆盖）。
    """
    if not task_id:
        return
    try:
        task = draft_workspace.load_task(task_id)
        if task is None:
            return
        state = str(getattr(task, "state", "") or "")
        if state not in ("RUNNING", "STOPPING"):
            return
        checkpoint = getattr(task, "checkpoint", None) or {}
        stoppable = int(checkpoint.get("resumeCount") or 0) > 0
        draft_workspace.abort_task(task_id, reason=reason, stoppable=stoppable)
    except Exception as exc:  # noqa: BLE001 — 收尾失败不阻断主流程
        logger.warning(
            "[agent.chat] 任务收尾失败 taskId=%s errorType=%s",
            task_id,
            type(exc).__name__,
        )


def _assert_turn_committable(
    session_key: str, task_id: str | None, request: AgentRequest
) -> None:
    """**栅栏**：这一轮还能不能落库。

    必须在写会话**之前**判 —— 停止确认之后，原任务的迟到结果
    不许写会话、不许写工作区、不许复活旧菜单（契约 0002 §7）。

    判据：
        - 任务已经 STOPPED / ROLLED_BACK / FAILED → 不许落库；
        - 写锁在**别的持有者**手里 → 不许落库
          （同一持有者重入 = 续跑，放行）。
    """
    if task_id:
        task = draft_workspace.load_task(task_id)
        if task is not None:
            state = str(getattr(task, "state", "") or "")
            if state in _TURN_STATE_LABEL:
                raise _TurnFenced(
                    f"这一轮{_TURN_STATE_LABEL[state]}，结果不再落库。"
                    "请选择继续完成剩下的工作，或撤销回到上一步。"
                )
    draft_id = session_service.load_draft_id(session_key)
    if draft_id:
        owner = _lock_owner_of(
            session_key, (request.messageId or "").strip(), task_id
        )
        try:
            draft_workspace.require_unlocked(draft_id, owner=owner)
        except DraftLocked as exc:
            raise _TurnFenced(str(exc.message)) from None


def _persist_turn(
    request: AgentRequest,
    response: AgentResponse,
    *,
    pre_turn_state: dict[str, Any] | None = None,
) -> None:
    """把这一轮对话落库。

    **会话写 + 草稿工作区写落在同一笔事务里**：栅栏检查 → 会话快照写 →
    定义 / 版本 / 历史写 → 步骤现场（任务）写。任何一步失败，整笔回滚：
    不留半份状态，也不返回成功 final（契约 0002 §6 / §7）。

    落库在请求成功返回之后才做：被中断的请求（用户点停止）不留下半截记录。

    设计流（#19）的成功轮例外：方案、阶段、操作结果与本轮消息是在
    **同一笔事务**里提交掉的（由设计存储调用），这里再落一遍会写出
    重复消息。响应上的 ``turnPersisted`` 就是这个信号。
    """
    session_key = session_service.resolve_session_id(request)
    task_id = _task_id_of(request)
    if getattr(response, "turnPersisted", False):
        # 设计流的成功轮：会话这一轮已经由设计存储**同事务**提交掉了，
        # 这里不能再记一遍（会写出重复消息）。
        #
        # 但「确认报表」这一轮要**建草稿基线**：草稿工作区是「草稿名 /
        # 备注」的落点，用户确认完还要能命名、写备注（契约 0002 §6）。
        _commit_confirmed_draft_workspace(
            session_key, response, request=request
        )
        return
    try:
        with session_service.repository.transaction():
            # 栅栏先行：**会话一个字都还没写**的时候就把不能落库的轮挡掉。
            _assert_turn_committable(session_key, task_id, request)
            session_service.record_turn(
                session_id=session_key,
                user_text=request.message,
                response=response,
                client_msg_id=request.messageId,
                request=request,
                strict=True,
            )
            _commit_draft_workspace(
                session_key, response, request=request, task_id=task_id
            )
            # 这一轮接受了一份待确认修改 → **消费掉它**（取走计划 + 清选择锁
            # + 清追问计数）。消费放在**同一笔事务里**：落库失败 → 整笔回滚
            # → 计划原样还在，用户还能重试这一轮（契约 0002 §6/§7）。
            session_service.commit_staged_plan_consumption(session_key)
            # 待审核经验记录（20211105）：**同一笔事务里**写入。
            # 与「更新草稿 + 消费候选」一起成败 —— 不会出现「接受失败却留下
            # 成功经验」，也不会出现「草稿改了却没记」。
            _commit_edit_archive(session_key)
            # 这一轮要写的**下一菜单**（选择锁）：同一笔事务里落库。
            # 必须排在计划消费**之后**：消费会清掉旧锁，下一菜单再写上去
            # （「先清旧卡、再挂新卡」）。两者任一失败 → 整笔回滚。
            session_service.commit_staged_pending_choice(session_key)
        # 事务提交成功 → 接受成功的那件事**结束**了 → 清掉修改事项。
        # （事务失败时上面就抛出去了，事项原样留着，用户还能重试。）
        if "template_edit_applied" in (response.uiActions or []):
            _clear_finished_edit_item(session_key)
    except PendingPlanConsumedError:
        # 计划已经被别人消费掉：这一轮基于的是**失效候选** → 整笔回滚。
        # **不恢复**候选：恢复会让同一份修改再落一遍。
        session_service.discard_staged_plan_consumption(session_key)
        session_service.discard_staged_pending_choice(session_key)
        session_service.discard_staged_edit_archive(session_key)
        raise
    except _TurnFenced:
        # 停止确认之后的迟到结果：**什么都不写**（会话、工作区、菜单都不动）。
        #
        # 候选消费与菜单写入现在都在**事务里**做，正常情况下这里什么都不用
        # 恢复；下面的 ``_restore_pending_candidates`` 是**兜底**：万一业务层
        # 有一笔事务外的写（历史遗留路径），被栅栏拒绝的这一轮也不能把用户
        # 那份待确认计划吃掉，用户要能重试（契约 0002 §7）。
        session_service.discard_staged_plan_consumption(session_key)
        session_service.discard_staged_pending_choice(session_key)
        # **已停止 / 已取消的任务不得迟到晋升**（契约 0003 §3.5）：
        # 暂存的待审核记录作废，绝不写进经验库。
        session_service.discard_staged_edit_archive(session_key)
        _restore_pending_candidates(session_key, pre_turn_state)
        raise
    except Exception:
        # 落库失败：把待处理候选放回去，用户还能重试这一轮。
        session_service.discard_staged_plan_consumption(session_key)
        session_service.discard_staged_pending_choice(session_key)
        session_service.discard_staged_edit_archive(session_key)
        _restore_pending_candidates(session_key, pre_turn_state)
        raise


def _commit_edit_archive(session_key: str) -> None:
    """在**最终提交事务里**写一条待审核经验记录。**在事务中调用。**

    没有暂存（普通轮次）→ 什么都不做。
    有暂存 → 写进经验库；**失败要抛出去**，让整笔事务回滚：
    经验库写失败却提交了事务，用户会拿到「草稿改了但记录没留」，
    而经验库正是管理员后续审核的依据 —— 半份状态比整笔失败更糟。

    幂等：同一 ``planId`` 重复归档返回同一条记录，不会重复。
    """
    payload = session_service.commit_staged_edit_archive(session_key)
    if not payload:
        return
    from app.services.edit_experience_service import record_pending_edit

    record_id = record_pending_edit(
        session_id=str(payload.get("session_id") or ""),
        draft_id=str(payload.get("draft_id") or ""),
        plan_id=str(payload.get("plan_id") or ""),
        item_id=str(payload.get("item_id") or ""),
        original_request=str(payload.get("original_request") or ""),
        supplement=str(payload.get("supplement") or ""),
        operations=payload.get("operations") or [],
        result=payload.get("result") or {},
        capability_version=str(payload.get("capability_version") or ""),
    )
    logger.info(
        "[agent.chat] 已归档待审核经验记录 sessionId=%s recordId=%s",
        session_key,
        record_id,
    )


def _clear_finished_edit_item(session_key: str) -> None:
    """接受成功 → 这件事结束了 → 清掉当前修改事项。

    **为什么**：修改事项是「同一件事」的现场。事情结束后它留着，
    下一次新修改会**复用**已结束的那份清单。

    **在事务之外调用**：清事项是「这一轮之后」的清理，不是本轮成败的一部分 ——
    事务失败时这一轮根本没生效，事项本来就该留着（用户还能重试）。
    只清 ``editItem``，不删聊天、不删撤销历史、不删草稿。
    """
    try:
        from app.services.edit_item_service import clear_edit_item

        clear_edit_item(session_key)
        # 这件事的**用户原话**也忘掉：下一件事不继承上一件的原话。
        session_service.forget_edit_request_text(session_key)
    except Exception as exc:  # noqa: BLE001 — 清理失败不该让这一轮失败
        logger.warning(
            "[agent.chat] 清理已结束的修改事项失败 sessionId=%s errorType=%s",
            session_key,
            type(exc).__name__,
        )


def _task_id_of(request: AgentRequest) -> str | None:
    """这一轮的任务 id。

    **由 messageId 推导**，不是随机生成：前端拿得到 messageId，就能
    在模型还在跑的时候拼出同一个 taskId 去点「停止」（契约 0002 §7）。
    """
    message_id = (request.messageId or "").strip()
    if not message_id:
        return None
    return f"task_{message_id}"


def _release_turn_lock(
    draft_id: str | None, lock_owner: str, task_id: str | None
) -> None:
    """这一轮结束：放掉写锁。

    **被停掉的那一轮不放**：STOPPED 时对象仍然锁着，输入与其他同对象
    写动作都要先选「继续」或「撤销」（契约 0002 §7.1）。
    """
    if not draft_id or not lock_owner:
        return
    try:
        if task_id:
            task = draft_workspace.load_task(task_id)
            if task is not None and str(getattr(task, "state", "")) == "STOPPED":
                logger.info(
                    "[agent.chat] 任务已停止，保留写锁 draftId=%s taskId=%s",
                    draft_id,
                    task_id,
                )
                return
        draft_workspace.release(draft_id, lock_owner)
    except Exception as exc:  # noqa: BLE001 — 放锁失败不阻断本轮对话
        logger.warning(
            "[agent.chat] 放写锁失败 draftId=%s errorType=%s",
            draft_id,
            type(exc).__name__,
        )


def _same_definition(left: Any, right: Any) -> bool:
    """两份定义内容是不是**同一份**（比内容，不比版本号）。

    用途：判断「这一轮有没有真的改了东西」。
    ``metadata.revision`` 不参与比较 —— 它只是乐观锁的数值，
    内容一样时它变不变都不该多出一个修改单位
    （**一个接受 = 一个修改单位**，续跑不能重复已提交的阶段）。
    """

    def _normalized(definition: Any) -> str:
        if not isinstance(definition, dict):
            return ""
        payload = dict(definition)
        metadata = dict(payload.get("metadata") or {})
        metadata.pop("revision", None)
        payload["metadata"] = metadata
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)

    return _normalized(left) == _normalized(right)


def _workspace_snapshot(
    session_key: str, response: AgentResponse, request: AgentRequest | None
) -> dict[str, Any]:
    """这一版落进草稿的**现场**：定义之外还有状态、提示、菜单。

    撤销 / 重做 / 「撤销（回到上一步）」都靠它把现场还原回去
    （契约 0002 §6.2 的 ``WorkspaceState``）。
    """
    snapshot = dict(session_service.load_snapshot(session_key))
    snapshot["businessState"] = str(
        getattr(response.state, "value", response.state) or ""
    )
    if request is not None and getattr(request, "message", ""):
        snapshot["prompt"] = str(request.message)
    # 这一轮要写的**下一菜单**已经暂存的话，工作区快照要记的就是它
    # （与提交后的会话状态一致）。没有暂存才读库里的当前值。
    staged, staged_choice = session_service.staged_pending_choice(session_key)
    if staged:
        choice = staged_choice
    else:
        try:
            choice = session_service.load_pending_choice(session_key)
        except Exception:  # noqa: BLE001 — 读不到就当没有
            choice = None
    snapshot["pendingChoice"] = choice if isinstance(choice, dict) else None
    try:
        plan = session_service.load_pending_plan(session_key)
    except Exception:  # noqa: BLE001
        plan = None
    snapshot["pendingEditPlan"] = (
        plan.model_dump(mode="json") if plan is not None else None
    )
    return snapshot


def _complete_turn_task(task_id: str | None, state: Any) -> None:
    """这一轮落库完成：任务收尾为 COMPLETED，并把已提交的进度写进检查点。

    检查点里的「已提交到哪」是续跑的依据：续跑要**接着没做完的做**，
    不能把已经落库的阶段再做一遍。

    **失败要抛出去**：外层事务据此整体回滚（会话、草稿、任务一起回滚），
    不留「会话写了、任务没完成」的半份状态 —— 那种状态会让续跑按旧的
    检查点重跑已经提交过的阶段。

    唯一的例外是「**本轮没有登记任务**」：第一轮还没有草稿对象时任务行
    根本不存在（见 `_begin_turn_task`），没有任务就没有「任务没完成」
    这半份状态 —— 不该因此判整轮失败。
    """
    if not task_id:
        return
    if draft_workspace.load_task(task_id) is None:
        logger.info(
            "[agent.chat] 本轮没有登记任务，跳过任务收尾 taskId=%s", task_id
        )
        return
    if state is not None:
        draft_workspace.checkpoint_task(
            task_id,
            {
                "committedRevision": int(getattr(state, "revision", 0) or 0),
                "committedUndoPointer": int(
                    getattr(state, "undoPointer", 0) or 0
                ),
                "committedAt": _now_text(),
            },
            merge=True,
        )
    draft_workspace.complete_task(task_id, commit_token=uuid.uuid4().hex[:12])


def _commit_confirmed_draft_workspace(
    session_key: str,
    response: AgentResponse,
    *,
    request: AgentRequest | None = None,
) -> Any:
    """「确认报表」这一轮：把确认下来的定义落成**草稿对象**。

    设计流的成功轮里，会话消息已经由设计存储提交掉了（``turnPersisted``），
    路由层不能再记一遍；但**草稿工作区还没建基线** —— 用户确认完还要
    能命名、写备注，而「草稿名 / 备注」的落点是草稿对象（契约 0002 §6）。

    **只在「确认报表」这一轮做**（``save_definition_draft``）：只读问答、
    澄清轮、失败轮都**不许**凭空建草稿。

    失败**不拖垮这一轮**：定义已经确认落库了，为一个草稿基线把整轮判
    失败，用户会以为「同意」没生效。记错误类型与调用栈，不记异常原文（#61）。
    """
    if "save_definition_draft" not in (response.uiActions or []):
        return None
    try:
        with session_service.repository.transaction():
            return _commit_draft_workspace(
                session_key,
                response,
                request=request,
                separate_draft_on_change=True,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[agent.chat] 确认报表后建草稿基线失败 sessionId=%s errorType=%s",
            session_key,
            type(exc).__name__,
            exc_info=(type(exc), None, exc.__traceback__),
        )
        return None


def _commit_draft_workspace(
    session_key: str,
    response: AgentResponse,
    *,
    request: AgentRequest | None = None,
    task_id: str | None = None,
    separate_draft_on_change: bool = False,
) -> Any:
    """把这一轮的定义落进**草稿工作区**（契约 0002 §6）。**在事务里调用。**

    - 第一次拿到定义 → 建立**基线**（revision 1，不产生撤销条目）；
    - 定义变了 → 提交一次修改（= 一个撤销单位）；
    - 定义没变 → **不提交**：内容一样就不该多出一个修改单位
      （续跑时已提交的阶段不能再落第二个）。

    `separate_draft_on_change=True` 时（**确认报表**那一轮），定义变了
    **另起一个草稿对象**而不是当成对上一份草稿的修改 —— 确认的是
    **另一份报表**时，两份报表不能挤进同一个草稿（用户刚命名过的草稿
    会被下一份报表顶掉）。

    **失败要抛出去**：外层事务据此回滚（会话与草稿一起回滚），
    不留半份状态。这里不吞异常 —— 吞掉就等于「会话写了、草稿没写」。
    """
    snapshot = session_service.load_snapshot(session_key)
    definition = snapshot.get("definition")
    if not isinstance(definition, dict) or not definition.get("regions"):
        _complete_turn_task(task_id, None)
        return None
    draft_id = session_service.load_draft_id(session_key)
    workspace_snapshot = _workspace_snapshot(session_key, response, request)
    if not draft_id:
        _, state = _create_baseline_draft(session_key, definition, workspace_snapshot)
    else:
        current = draft_workspace.load_draft(draft_id)
        if current is None:
            return None
        if _same_definition(current.definition, definition):
            # 内容没变 → 不产生新的修改单位（一个接受 = 一个修改单位）。
            state = current
        elif separate_draft_on_change:
            # 确认的是**另一份报表** → 另起一个草稿对象。
            _, state = _create_baseline_draft(session_key, definition, workspace_snapshot)
        else:
            state = draft_workspace.commit_edit(
                draft_id,
                definition=definition,
                snapshot=workspace_snapshot,
                label=",".join(response.uiActions or []),
            )
    if state is not None:
        response.domainObjects.draftWorkspace = _draft_workspace_payload(
            state, definition=definition
        )
    _complete_turn_task(task_id, state)
    return state


def _create_baseline_draft(
    session_key: str, definition: dict[str, Any], workspace_snapshot: dict[str, Any]
) -> tuple[str, Any]:
    """建一份**基线草稿**（revision 1，无撤销条目）并把会话绑上去。

    草稿名先取定义里的报表名 —— 用户确认后紧接着就能改名与写备注。
    """
    draft_id = f"draft_{uuid.uuid4().hex[:12]}"
    name = str((definition.get("metadata") or {}).get("name") or "报表草稿")
    state = draft_workspace.create_draft(
        draft_id,
        session_id=session_key,
        name=name,
        definition=definition,
        snapshot=workspace_snapshot,
    )
    session_service.set_draft_id(session_key, draft_id)
    return draft_id, state


def _draft_workspace_payload(state: Any, *, definition: Any = None) -> dict[str, Any]:
    """把工作区状态压成前端要的那几个字段。

    ``resolvedTheme`` 一起下发：主题是**服务端解析**的（契约 0002 §8），
    前端不拼主题注册表，也不自己解析继承链。
    """
    to_dict = getattr(state, "to_dict", None)
    payload = dict(to_dict()) if callable(to_dict) else {}
    resolved_theme = payload.get("resolvedTheme")
    if not resolved_theme and isinstance(definition, dict):
        try:
            resolved_theme = theme_service.resolve_theme(definition)
        except Exception:  # noqa: BLE001 — 主题解析失败不阻断本轮对话
            logger.warning("[agent.chat] 主题解析失败 sessionId 级")
    return {
        "draftId": payload.get("draftId"),
        "name": payload.get("name"),
        "description": payload.get("description"),
        "revision": payload.get("revision"),
        "savedRevision": payload.get("savedRevision"),
        "canUndo": bool(payload.get("canUndo")),
        "canRedo": bool(payload.get("canRedo")),
        "themeId": payload.get("themeId"),
        "resolvedTheme": resolved_theme,
    }


def _record_turn_persisted(
    request: AgentRequest, response: AgentResponse, session_key: str
) -> None:
    """事务**成功之后**才记「正式生效」与真实版本。

    为什么放在这里：只有在**既有持久化事务真的提交了**之后，
    才能说「正式草稿改了」。工具把提交槽写成功**不等于**草稿改了。

    事务内不写任何额外业务数据 —— 记录只发生在提交之后。
    """
    try:
        from app.services import edit_trace_service

        workspace = getattr(response.domainObjects, "draftWorkspace", None)
        workspace = workspace if isinstance(workspace, dict) else {}
        revision = workspace.get("revision")
        saved_revision = workspace.get("savedRevision")
        edit_trace_service.record(
            edit_trace_service.STAGE_APPLY,
            name="turn_persisted",
            params={
                "draftId": str(workspace.get("draftId") or ""),
                "revision": revision,
                "savedRevision": saved_revision,
            },
            result={
                "committed": True,
                "revision": revision,
                "savedRevision": saved_revision,
            },
            phase=edit_trace_service.PHASE_APPLIED,
            applied=True,
            draftChanged=True,
            checks={
                # 业务结论来自**执行层**：这里只声明持久化成功。
                "business": edit_trace_service.CHECK_UNKNOWN,
                "persistence": edit_trace_service.PERSIST_COMMITTED,
                # 本轮**没有**效果核对行为 —— 不推断符合或不符合。
                "effect": edit_trace_service.EFFECT_NOT_RUN,
            },
        )
    except Exception as exc:  # noqa: BLE001 — 记录失败不能令已成功的事务返回业务失败
        logger.warning(
            "[agent.chat] 记录生效事实失败 sessionId=%s errorType=%s",
            session_key,
            type(exc).__name__,
        )


def _record_turn_persist_failed(session_key: str, reason: str) -> None:
    """事务失败：记录**真实**失败与已知的回滚事实。

    不说「已生效」，也不说「部分生效」—— 只说没提交成。
    """
    try:
        from app.services import edit_trace_service

        edit_trace_service.record(
            edit_trace_service.STAGE_APPLY,
            name="turn_persist_failed",
            params={"reason": str(reason or "")},
            result={"committed": False, "reason": str(reason or "")},
            status="error",
            applied=False,
            draftChanged=False,
            checks={
                "business": edit_trace_service.CHECK_UNKNOWN,
                "persistence": edit_trace_service.PERSIST_FAILED,
                "effect": edit_trace_service.EFFECT_NOT_RUN,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[agent.chat] 记录落库失败事实失败 sessionId=%s", session_key
        )


@router.post("/chat", response_model=AgentResponse)
async def chat(request: AgentRequest) -> AgentResponse:
    started = perf_counter()
    start_timeline(request.conversationId)
    # 请求级上下文：这一轮里的模型调用靠它关联会话与消息。
    diagnostics.begin_request_context(
        conversation_id=request.conversationId,
        message_id=request.messageId,
    )
    log_timeline_step("router_received")
    # 只记长度，不记用户原话。
    log_info(
        logger,
        "[agent.chat] step=router_received conversationId=%s resumeOf=%s state=%s messageLength=%d",
        request.conversationId,
        request.resumeOfConversationId,
        request.state.value,
        len(request.message or ""),
    )
    # ---- 同一草稿对象的写互斥 + 本轮任务（契约 0002 §7）----
    # 非流式入口**也要有**同样的锁保护：不能只靠前端禁按钮。
    session_key = session_service.resolve_session_id(request)
    draft_id = session_service.load_draft_id(session_key)
    lock_owner, task_id = _acquire_turn_lock(session_key, request, draft_id)
    _begin_turn_task(draft_id, task_id, lock_owner, session_key, request)
    # ---- 请求作用域的追踪 ----
    #
    # 在**请求入口**开，覆盖三条路径：普通编辑、**同一事项补充**、
    # **接受**。作用域在 ``finally`` 里恢复 —— 不能串到下一个请求。
    _trace, _trace_token = edit_trace_service.begin_trace_scope(
        session_id=session_key,
        draft_id=draft_id or "",
        message_id=str(request.messageId or ""),
    )
    # 本轮开始前的候选现场：落库失败时要放回去（可重试）。
    pre_turn_state = _capture_edit_state(session_key)
    # 上一轮没落成的暂存作废：暂存没被消费 = 计划还在，用户还能再点一次接受。
    session_service.discard_staged_plan_consumption(session_key)
    # 上一轮没落成的**菜单**暂存同样作废：没提交 = 菜单没变。
    session_service.discard_staged_pending_choice(session_key)
    try:
        try:
            response = await agent_service.chat(request)
        except Exception as exc:  # noqa: BLE001
            log_timeline_step("router_failed")
            _log_failure(
                f"[agent.chat] step=router_failed conversationId={request.conversationId} "
                f"elapsed_ms={(perf_counter() - started) * 1000:.1f}",
                exc,
            )
            # 抛出去的异常不带原文：Web 服务器会把异常消息写进自己的错误日志。
            raise _PublicFailure(user_visible_error_message(exc)) from None
        log_timeline_step("router_return")
        # 「业务已返回」：这一事件说明业务结论已经出来了，
        # 不代表客户端已经收到 —— 客户端确认是另一件事。
        log_info(
            logger,
            "[agent.chat] step=router_return conversationId=%s event=business_returned "
            "elapsed_ms=%.1f response_state=%s uiActions=%s error=%s",
            request.conversationId,
            (perf_counter() - started) * 1000,
            response.state.value,
            response.uiActions,
            response.error.code if response.error else None,
        )
        try:
            _persist_turn(request, response, pre_turn_state=pre_turn_state)
        except _TurnFenced as exc:
            # 栅栏：这一轮已经不能落库（被停掉 / 被撤销 / 锁在别人手里）。
            log_timeline_step("persist_fenced")
            _abort_turn_task(task_id, reason="fenced")
            _record_turn_persist_failed(session_key, "fenced")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except Exception as exc:  # noqa: BLE001
            # 落库失败：**不返回成功 final**（这一轮没保存成功）。
            log_timeline_step("persist_failed")
            _log_failure(
                f"[agent.chat] step=persist_failed conversationId={request.conversationId} "
                f"elapsed_ms={(perf_counter() - started) * 1000:.1f}",
                exc,
            )
            _abort_turn_task(task_id, reason="persist_failed")
            _record_turn_persist_failed(session_key, "persist_failed")
            raise HTTPException(
                status_code=500, detail="这一轮没有保存成功，请重试。"
            ) from None
        # 事务真的提交了 → 现在才可以说「正式生效」，并带上真实版本。
        _record_turn_persisted(request, response, session_key)
        return response
    finally:
        _release_turn_lock(draft_id, lock_owner, task_id)
        # 请求结束：恢复追踪作用域（不能串到下一个请求）。
        edit_trace_service.end_trace_scope(_trace_token)


@router.post("/chat/stream")
async def chat_stream(request: AgentRequest) -> StreamingResponse:
    # ---- 同一草稿对象的写互斥 + 本轮任务（契约 0002 §7）----
    session_key = session_service.resolve_session_id(request)
    draft_id = session_service.load_draft_id(session_key)
    # 拿锁是**一条语句**的判定：同一持有者重入 = 续跑（放行），
    # 别的持有者持锁 → 409。不留「先看锁、再写」之间的窗口。
    lock_owner, task_id = _acquire_turn_lock(session_key, request, draft_id)
    _begin_turn_task(draft_id, task_id, lock_owner, session_key, request)

    async def event_generator():
        started = perf_counter()
        queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

        async def emit(event: str, data: dict[str, Any]) -> None:
            # 每条 SSE 事件都带上本轮的 ``messageId``：客户端据此判断
            # 「这条事件是不是本请求的」，迟到的事件不会串到别的请求上。
            data = {
                **data,
                "messageId": request.messageId,
                "elapsedMs": round((perf_counter() - started) * 1000),
            }
            try:
                await queue.put((event, data))
            except Exception as exc:  # noqa: BLE001
                # 队列已无人消费，忽略即可，不能影响主流程。
                # 但不留着：这一条现在能查到了（只记异常类型）。
                _log_failure(
                    f"[agent.chat.stream] step=emit_failed conversationId={request.conversationId} "
                    f"event={event}",
                    exc,
                )

        async def run_chat() -> None:
            start_timeline(request.conversationId)
            # ---- 请求作用域的追踪 ----
            #
            # 在**请求入口**开，覆盖三条路径：普通编辑、**同一事项补充**、
            # **接受**。作用域在 ``finally`` 里恢复 —— 不能串到下一个请求。
            _trace, _trace_token = edit_trace_service.begin_trace_scope(
                session_id=session_key,
                draft_id=draft_id or "",
                message_id=str(request.messageId or ""),
            )
            # 请求级上下文：这一轮里的模型调用靠它关联会话与消息。
            diagnostics.begin_request_context(
                conversation_id=request.conversationId,
                message_id=request.messageId,
            )
            log_timeline_step("router_received")
            # 只记长度，不记用户原话。
            log_info(
                logger,
                "[agent.chat.stream] step=router_received conversationId=%s resumeOf=%s state=%s messageLength=%d",
                request.conversationId,
                request.resumeOfConversationId,
                request.state.value,
                len(request.message or ""),
            )
            await emit(
                "stage",
                {
                    "stage": "router_received",
                    "state": request.state.value,
                    "message": request.message,
                },
            )
            cancelled = False
            # 本轮开始前的候选现场：落库失败时要放回去（可重试）。
            pre_turn_state = _capture_edit_state(session_key)
            # 上一轮没落成的暂存作废（见非流式入口的同名调用）。
            session_service.discard_staged_plan_consumption(session_key)
            # 上一轮没落成的**菜单**暂存同样作废（见非流式入口的同名调用）。
            session_service.discard_staged_pending_choice(session_key)
            try:
                response = await agent_service.chat(request, emit=emit)
                log_timeline_step("router_return")
                log_info(
                    logger,
                    "[agent.chat.stream] step=router_return conversationId=%s event=business_returned "
                    "elapsed_ms=%.1f response_state=%s uiActions=%s error=%s",
                    request.conversationId,
                    (perf_counter() - started) * 1000,
                    response.state.value,
                    response.uiActions,
                    response.error.code if response.error else None,
                )
                await _emit_reply_stream(response.reply, emit)
                # 落库放在发 final 之前：final 一到客户端就可能刷新会话列表，
                # 顺序反了会看到还没入库的旧数据。
                _persist_turn(request, response, pre_turn_state=pre_turn_state)
                # 事务真的提交了 → 现在才可以说「正式生效」，并带上真实版本。
                _record_turn_persisted(request, response, session_key)
                await emit(
                    "final",
                    response.model_dump(mode="json"),
                )
                # 「final 已入队」：只说明 final 进了发送队列，
                # **不等于**客户端已经收到 —— 没有客户端确认就不能这么说。
                log_info(
                    logger,
                    "[agent.chat.stream] step=final_enqueued conversationId=%s "
                    "event=final_enqueued elapsed_ms=%.1f",
                    request.conversationId,
                    (perf_counter() - started) * 1000,
                )
            except asyncio.CancelledError:
                cancelled = True
                log_timeline_step("router_cancelled")
                log_info(
                    logger,
                    "[agent.chat.stream] step=router_cancelled conversationId=%s elapsed_ms=%.1f",
                    request.conversationId,
                    (perf_counter() - started) * 1000,
                )
                raise
            except _TurnFenced as exc:
                # 栅栏：停止确认之后的迟到结果 —— **不发 final**，只发 error。
                log_timeline_step("persist_fenced")
                log_info(
                    logger,
                    "[agent.chat.stream] step=persist_fenced conversationId=%s taskId=%s",
                    request.conversationId,
                    task_id,
                )
                _abort_turn_task(task_id, reason="fenced")
                _record_turn_persist_failed(session_key, "fenced")
                await emit(
                    "error",
                    {"message": str(exc), "code": "TURN_FENCED"},
                )
            except Exception as exc:  # noqa: BLE001
                log_timeline_step("router_failed")
                _log_failure(
                    f"[agent.chat.stream] step=router_failed conversationId={request.conversationId} "
                    f"elapsed_ms={(perf_counter() - started) * 1000:.1f}",
                    exc,
                )
                _abort_turn_task(task_id, reason="persist_failed")
                _record_turn_persist_failed(session_key, "persist_failed")
                # 用户可见的错误消息里不带异常原文：只说「出了什么事、怎么办」。
                await emit(
                    "error",
                    {
                        "message": user_visible_error_message(exc),
                        "code": "INTERNAL_ERROR",
                    },
                )
            finally:
                # 「流结束或被取消」：这两个必须分开记。
                log_info(
                    logger,
                    "[agent.chat.stream] step=stream_end conversationId=%s event=%s elapsed_ms=%.1f",
                    request.conversationId,
                    "stream_cancelled" if cancelled else "stream_finished",
                    (perf_counter() - started) * 1000,
                )
                _release_turn_lock(draft_id, lock_owner, task_id)
                # 请求结束：恢复追踪作用域（不能串到下一个请求）。
                edit_trace_service.end_trace_scope(_trace_token)
                await queue.put(None)

        task = asyncio.create_task(run_chat())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                event, data = item
                yield _sse(event, data)
        finally:
            # 客户端断开（切走界面、关页面）= 取消本轮，不落库。
            # 「用户按停止」和「切走界面」在传输层无法区分，
            # 所以统一按取消处理；要保留历史请让请求自然跑完
            # （前端「新建对话」不再 abort，请求会跑完并落库）。
            task.cancel()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
