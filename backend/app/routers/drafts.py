"""草稿工作区路由（契约 0002 §7）。

这一组路由是**草稿对象**的入口，不是第二套对话提交逻辑：
原来的对话入口（提候选 → 接受）继续照旧，本组路由只负责
「打开 / 保存 / 撤销 / 重做 / 另存为」这类**草稿级**动作。

服务端做版本与锁检查；前端不自己算能不能撤销 —— 它按服务端给的
``canUndo`` / ``canRedo`` 决定「撤销上一步」这一项能不能点。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from app.services.agent_service import (
    CONFIRM_BUTTON_VALUE,
    MODIFY_BUTTON_VALUE,
    RESTART_BUTTON_VALUE,
)
from app.services.draft_workspace import draft_workspace
from app.services.preview_service import (
    PreviewContextError,
    build_report_preview_payload,
    resolve_preview_context,
)
from app.services.session_service import session_service
from app.services.simulated_data_service import SimulatedDataError
from app.services.report_theme_service import theme_service


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["drafts"])


#: 允许的动作。**只有这四个** —— 不接受任意动作名（避免变成通用补丁通道）。
DRAFT_ACTIONS = ("SAVE", "UNDO", "REDO", "SAVE_AS")


class DraftActionRequest(BaseModel):
    """草稿动作请求。"""

    action: str
    #: 基本版本检查：客户端看到的版本与服务端不一致就拒绝（契约 0002 §6）。
    expectedRevision: int | None = None
    #: 保存或另存为的名字。
    name: str | None = None
    #: 草稿备注：用户自己写的一句话说明（落在 `draft_objects.description`）。
    #: **没给就不动原备注**；给空字符串是「清空备注」。
    remark: str | None = None


class DraftOpenRequest(BaseModel):
    """打开草稿的请求体。"""

    #: 要切到哪个草稿对象上的会话。
    sessionId: str | None = None


def _state_payload(state: Any) -> dict[str, Any]:
    """把工作区状态转成 JSON 友好 dict。"""
    if state is None:
        return {}
    if isinstance(state, dict):
        return dict(state)
    to_dict = getattr(state, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    return {
        key: getattr(state, key)
        for key in (
            "draftId",
            "revision",
            "savedRevision",
            "definition",
            "businessState",
            "prompt",
            "pendingChoice",
            "pendingEditPlan",
            "undoPointer",
            "redoPointer",
            "activeTask",
            "themeId",
            "updatedAt",
            "name",
            "description",
            "canUndo",
            "canRedo",
        )
        if hasattr(state, key)
    }


def _session_id_of(state: Any) -> str | None:
    payload = _state_payload(state)
    session_id = payload.get("sessionId")
    return str(session_id) if session_id else None


def _build_preview(definition: Any, session_id: str | None) -> dict[str, Any] | None:
    """按当前定义**重新生成**示意载荷。

    撤销 / 重做之后，前端要立刻看到恢复后的排版，不能等下一次对话。
    载荷不落盘（ADR 0004）：这里只是按「定义 + 冻结的时间窗」重算一份。
    """
    if not isinstance(definition, dict) or not definition.get("regions"):
        return None
    snapshot: dict[str, Any] = {}
    if session_id:
        try:
            snapshot = session_service.load_snapshot(session_id)
        except Exception:  # noqa: BLE001 — 载荷是尽力而为，失败不阻断动作
            snapshot = {}
    plan = snapshot.get("reportPlan") if isinstance(snapshot.get("reportPlan"), dict) else None
    existing = snapshot.get("previewContext") if isinstance(snapshot.get("previewContext"), dict) else None
    try:
        context = resolve_preview_context(definition, plan=plan, existing=existing)
        return build_report_preview_payload(definition, context, plan=plan)
    except (SimulatedDataError, PreviewContextError) as exc:
        logger.warning(
            "[drafts] 重新生成示意载荷失败 code=%s errorType=%s",
            str(getattr(exc, "code", "") or ""),
            type(exc).__name__,
        )
        return None


def _may_sync_session_snapshot(
    session_draft_id: str | None, action_draft_id: str
) -> bool:
    """能不能把**本次操作对象**的定义写回这条会话。

    判据只有一条：**这条会话当前绑定的草稿对象，是不是本次操作的那个**。

    - 会话**没有**绑定（历史 / 异常）→ 照旧同步（既有语义一个字不变）；
    - 会话绑的就是本次操作的对象 → 照旧同步；
    - 会话绑的是**别的**对象（另存为之后切走了）→ **不**同步：
      旧对象的定义写进已经属于新对象的会话，会造出「新 draftId + 旧定义」
      的混合会话（旧窗口 / 旧对象的撤销会覆盖新对象的内容）。

    纯函数（不碰库、不看事务）：便于单测，不散落在事务体里。
    """
    if not session_draft_id:
        return True
    return str(session_draft_id) == str(action_draft_id)


def _sync_session_snapshot(session_id: str | None, definition: Any, revision: int | None) -> None:
    """把恢复后的定义写回**会话快照**。

    不写回去的话：撤销只改了草稿对象，刷新页面又会跳回撤销前的版本 ——
    用户会觉得「撤销根本没生效」。

    **失败要抛出去**：调用方在**同一笔事务**里用它 —— 写不回去就整笔回滚，
    不留「工作区已经撤销、会话仍是旧草稿」的半份状态（那正是刷新后
    跳回旧内容的原因）。
    """
    if not session_id or not isinstance(definition, dict):
        return
    payload = dict(definition)
    metadata = dict(payload.get("metadata") or {})
    if revision is not None:
        # 写入版本**单调递增**：撤销不把乐观锁数值退回去（契约 0002 §6.3）。
        metadata["revision"] = revision
    payload["metadata"] = metadata

    def mutate(snapshot: dict[str, Any]) -> dict[str, Any]:
        updated = dict(snapshot)
        updated["definition"] = payload
        return updated

    try:
        session_service.update_snapshot(session_id, mutate)
    except Exception as exc:
        # 异常原文不进日志（#61）：留栈不留原文。
        logger.exception(
            "[drafts] 写回会话快照失败 sessionId=%s errorType=%s",
            session_id,
            type(exc).__name__,
            exc_info=(type(exc), None, exc.__traceback__),
        )
        raise


def _resolve_draft_session(
    draft_id: str, owner_session_id: str | None
) -> tuple[str | None, bool]:
    """这份草稿的**现场会话**是哪一条，以及它**有没有对话**。

    打开草稿 = 回到那份草稿的对话现场（对话、待决菜单、状态一起回来）。
    现场会话 = **当前指向这份草稿、并且有对话记录**的那条会话。

    为什么不能只看草稿自己记的那个会话：打开草稿的旧做法是给草稿**另开**
    一条空会话、再把草稿搬过去 —— 那条空会话没有对话（连会话列表里都
    不出现），打开后左边一片空白，而真正有对话的那条会话被落在了后面。

    找不到有对话的，就退回候选里最近更新的那条；再不行用草稿自己记的。

    返回 ``(sessionId, 有没有对话)``。
    """
    candidates = session_service.repository.find_sessions_by_draft_id(draft_id)
    with_dialog = [item for item in candidates if int(item[1] or 0) > 0]
    pool = with_dialog or candidates
    if pool:
        picked = max(pool, key=lambda item: str(item[2] or ""))
        return picked[0], int(picked[1] or 0) > 0
    return owner_session_id, False


def _sync_session_scene(session_id: str | None, state: Any) -> None:
    """把草稿的**现场**接到一条**空会话**上：状态 + 待决菜单。

    **只在「这条会话还没有对话」时写**（调用方用 `has_dialog` 判定）：
    已经有对话的那条会话，状态与菜单是**它自己的**（它可能已经走到草稿
    快照之后的那一步），覆盖它就是把会话往回退。

    两个值都取**服务端给的权威现场**（契约 0002 §6.2）：状态取草稿落库
    那一版的 `businessState`，菜单取它的 `pendingChoice`。

    **失败要抛出去**：调用方在**同一笔事务**里用它 —— 写不回去就整笔
    回滚，不留「草稿切了、状态没切」的半份状态。
    """
    if not session_id:
        return
    payload = _state_payload(state)
    business_state = str(payload.get("businessState") or "").strip()
    if business_state:
        session_service.save_state(session_id, business_state)
    session_service.save_pending_choice(
        session_id, _pending_choice_of(payload.get("pendingChoice"))
    )


def _pending_choice_of(raw: Any) -> dict[str, Any] | None:
    """把工作区状态里的待决选择还原成会话那一份结构。

    工作区状态里 `pendingChoice` 声明为**文本**（结构化的内容用 JSON 文本
    带着，见 `draft_workspace._as_text`）；会话那一列存的是结构化对象。
    这里把文本解析回对象 —— 解析不出来就当「没有待决菜单」，不硬塞半份。
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _revoke_session_step(session_id: str | None, *, action: str = "UNDO") -> list[str]:
    """撤销 / 重做之后，让**左侧会话的当前待决步骤**失效。

    BUG-008：撤销只回滚了右侧的定义，左侧仍然挂着那一轮的菜单与摘要 ——
    用户看到的是"报表已经撤销了，对话却还停在刚才那一步"。

    这里做三件事：

    1. **清掉会话的待决选择与待确认计划**：那张旧菜单不再是权威待决步骤，
       旧步骤的提交会被后端按"步骤关联对不上"拒掉（前后端都关掉）。
    2. **返回被失效的步骤 id**：前端据此把旧菜单标成"已撤销"（保留文字，
       不再能操作）。
    3. **撤销 / 重做对应到「一个修改单位」**：
       一次接受整批算一个撤销单位（契约 §2.3）。所以连续撤销 B / A 时，
       第二次撤的是**再往前一个已生效的修改**；重做则把**最后撤掉的**
       那一个恢复回来。

    **历史消息保留**：聊天记录不清 —— 只是不再是当前有效现场。
    """
    if not session_id:
        return []
    revoked: list[str] = []
    try:
        record = session_service.repository.get_session(session_id)
    except Exception:  # noqa: BLE001 — 取不到就当没有待决步骤
        logger.warning("[drafts] 读取会话待决步骤失败 sessionId=%s", session_id)
        return []
    choice = getattr(record, "pendingChoice", None) or {}
    question = choice.get("question") if isinstance(choice, dict) else None
    if isinstance(question, dict):
        step_id = str(question.get("stepId") or "")
        if step_id:
            revoked.append(step_id)
    pending_plan = getattr(record, "pendingEditPlan", None) or {}
    if isinstance(pending_plan, dict):
        plan = pending_plan.get("editPlan")
        plan = plan if isinstance(plan, dict) else {}
        step_id = str(plan.get("stepId") or "")
        if step_id and step_id not in revoked:
            revoked.append(step_id)
    try:
        session_service.repository.set_pending_choice(session_id, None)
        session_service.repository.clear_pending_edit_plan(session_id)
    except Exception:  # noqa: BLE001 — 清不掉不算致命，步骤关联本身也会拒
        logger.warning("[drafts] 清理会话待决步骤失败 sessionId=%s", session_id)
    if action == "REDO":
        # 重做：把**最后撤掉**的那一个已生效修改恢复回来（撤销是 LIFO）。
        session_service.restore_last_revoked_applied_step(session_id)
    else:
        # 撤销：还要失效**最近一个已生效的修改** —— 它留在历史里的
        # 「已完成：……」在撤销之后已经不算数了。
        applied_step = session_service.revoke_latest_applied_step(session_id)
        if applied_step and applied_step not in revoked:
            revoked.append(applied_step)
    # **历史消息上落撤销标记**：不落的话，刷新之后左侧那一步又是可操作的
    # —— 用户看到的是"报表撤了，对话里那一步还能点"（BUG-008）。
    # 标记只让那一步变只读，**消息文本与更早历史一个字都不删**。
    for step_id in revoked:
        session_service.mark_step_revoked(session_id, step_id)
    return revoked


def _restore_session_menu(
    session_id: str | None,
    *,
    draft_id: str | None,
    definition: Any,
    reply: str,
) -> None:
    """撤销 / 重做之后，重新给会话发一张**当前合法的下一步菜单**。

    契约（``docs/agent/state-and-undo.md`` 第 50 行）：
    「完整现场包括草稿定义、业务状态、提示、**可操作菜单**及权限」；
    第 44 行：「每次撤销最新单位……**当前步骤提供入口**」。

    旧菜单随那一轮一起失效了（否则用户能对着已经撤销的步骤继续操作），
    所以要**补一张新的** —— 否则撤销之后左侧只剩历史回放，
    用户既没有可操作的菜单，普通输入也因为「撤销后没有待决选择」
    而处于一种说不清的状态。

    新菜单**写进历史消息**（前端刷新 / 回放时把它画出来），
    同时写进 ``pending_choice``（服务端权威待决选择 = 选择锁的来源）。

    尽力而为：发不出去不掀翻已经成功的撤销（定义已经回滚了）。
    """
    if not session_id:
        return
    question = {
        "id": "template_edit_confirm",
        "prompt": "请选择下一步操作：",
        "expects": "SINGLE_CHOICE",
        "selectionOnly": True,
        "allowsOther": True,
        "otherLabel": "其他",
        "otherPlaceholder": "直接输入修改要求",
        "options": [
            {"value": CONFIRM_BUTTON_VALUE, "label": CONFIRM_BUTTON_VALUE},
            {"value": MODIFY_BUTTON_VALUE, "label": MODIFY_BUTTON_VALUE},
            {"value": RESTART_BUTTON_VALUE, "label": RESTART_BUTTON_VALUE},
        ],
        "lockScope": "input_and_menu",
        # 新步骤身份：旧菜单的那一步已经失效，不能复用同一个 id。
        "stepId": f"step_{uuid.uuid4().hex}",
        "stepVersion": 1,
        "stepKind": "menu",
        "stepDraftId": draft_id,
    }
    payload: dict[str, Any] = {
        "state": "USER_EDITING_TEMPLATE",
        "reply": reply,
        "questions": [question],
        "uiActions": ["template_editing"],
    }
    if isinstance(definition, dict):
        payload["domainObjects"] = {"reportDefinitionDraft": definition}
    try:
        session_service.save_state(session_id, "USER_EDITING_TEMPLATE")
        session_service.repository.append_message(
            session_id=session_id,
            role="assistant",
            content=reply,
            payload=payload,
        )
        session_service.save_pending_choice(
            session_id,
            {"state": "USER_EDITING_TEMPLATE", "question": question},
        )
    except Exception:  # noqa: BLE001 — 补菜单失败不掀翻已成功的撤销
        logger.warning("[drafts] 撤销后补发菜单失败 sessionId=%s", session_id)


def _respond(state: Any, *, session_id: str | None = None) -> dict[str, Any]:
    payload = _state_payload(state)
    definition = payload.get("definition")
    payload["reportPreviewPayload"] = _build_preview(definition, session_id)
    # 主题由**服务端解析**后下发（契约 0002 §8）：前端不拼主题注册表。
    if not payload.get("resolvedTheme") and isinstance(definition, dict):
        try:
            payload["resolvedTheme"] = theme_service.resolve_theme(definition)
        except Exception:  # noqa: BLE001 — 主题解析失败不阻断草稿动作
            logger.warning("[drafts] 主题解析失败")
    return payload


def _draft_error(exc: Exception) -> HTTPException:
    """把草稿工作区的异常映射成合适的 HTTP 状态。

    - 冲突（版本对不上）→ 409：客户端该刷新，不是它错；
    - 不存在 → 404；
    - 锁住 / 无可撤销 → 409：同一对象的并发写要先停下来；
    - 其它（会话快照写失败之类的基础设施故障）→ 500：不是用户的错，
      而且**不回内部异常原文**，只说人话。
    """
    code = str(getattr(exc, "code", "") or "")
    message = str(getattr(exc, "message", "") or "草稿操作失败")
    if isinstance(exc, HTTPException):
        # 已经是定好的对外状态：原样透传，别再套一层。
        return exc
    if code == "draft_not_found" or code == "task_not_found":
        status = 404
    elif code in (
        "draft_revision_conflict",
        "draft_locked",
        "draft_undo_unavailable",
        "draft_redo_unavailable",
        "task_not_active",
    ):
        status = 409
    elif not code:
        # 基础设施故障（会话快照写失败等）：不是草稿语义错误。
        logger.warning("[drafts] 草稿动作遇到基础设施故障 errorType=%s", type(exc).__name__)
        return HTTPException(status_code=500, detail="草稿操作失败，请重试。")
    else:
        status = 400
    return HTTPException(status_code=status, detail=message)


def _task_lock_owner(task: Any, session_id: str | None = None) -> str:
    """这一轮当时的写锁持有者。

    检查点里记着它（``begin_task`` 时写进去的）；老任务没有就按
    「会话 + 消息 id」推 —— 消息 id 就是任务 id 的来源。
    """
    checkpoint = getattr(task, "checkpoint", None) or {}
    recorded = str(checkpoint.get("lockOwner") or "")
    if recorded:
        return recorded
    operation_id = str(getattr(task, "operationId", "") or "")
    return f"chat:{session_id or ''}:{operation_id}"


def _task_session_id(task: Any, draft_id: str) -> str | None:
    """这一轮所属的会话：检查点里记的，没有就取草稿对象绑定的会话。"""
    checkpoint = getattr(task, "checkpoint", None) or {}
    recorded = str(checkpoint.get("sessionId") or "")
    if recorded:
        return recorded
    if not draft_id:
        return None
    state = draft_workspace.load_draft(draft_id)
    return _session_id_of(state)


@router.get("/drafts")
def list_drafts() -> list[dict[str, Any]]:
    """打开草稿列表：**只有名称与最近修改时间**（契约 0002 §6.2）。"""
    try:
        return list(draft_workspace.list_drafts())
    except Exception as exc:  # noqa: BLE001
        logger.exception("[drafts] 读取草稿列表失败")
        raise HTTPException(status_code=500, detail="读取草稿列表失败") from exc


@router.get("/drafts/{draft_id}")
def get_draft(draft_id: str) -> dict[str, Any]:
    """打开一个草稿：读回它的定义、版本与撤销指针。"""
    try:
        state = draft_workspace.load_draft(draft_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[drafts] 读取草稿失败 draftId=%s", draft_id)
        raise HTTPException(status_code=500, detail="读取草稿失败") from exc
    if state is None:
        raise HTTPException(status_code=404, detail="这个草稿不存在。")
    return _respond(state, session_id=_session_id_of(state))


@router.delete("/drafts/{draft_id}", status_code=204)
def delete_draft(draft_id: str) -> Response:
    try:
        draft_workspace.delete(draft_id)
    except Exception as exc:  # noqa: BLE001
        raise _draft_error(exc) from exc
    return Response(status_code=204)


@router.post("/drafts/{draft_id}/open")
def open_draft(draft_id: str, body: DraftOpenRequest) -> dict[str, Any]:
    """打开一个草稿：把工作台**切到这份草稿的现场会话**。

    切的是**对象身份**（契约 0002 §6.4）：切完之后，后续对话的提交落到
    这个对象，而不是还挂在原来那个对象上；原草稿一个字节都不改。

    **现场会话** = 当前指向这份草稿、并且有对话记录的那条会话（见
    ``_resolve_draft_session``）。打开草稿不是给草稿另开一条空会话 ——
    那条空会话没有对话，打开后左边一片空白，真正有对话的那条会话被
    落在后面。

    body 里的 ``sessionId`` = **调用方指定**要绑到哪条会话上；**没指定**
    就用那份草稿的现场会话。响应里的 ``sessionId`` 是实际用到的那一条。

    **状态与待决菜单也一起切**（契约 0002 §6.3「重开恢复同一游标与菜单」）：
    只切内容不切状态的话，用户看到的是「右边是这份草稿、左边还停在
    上一份报表的那一步」。

    「打开列表」只给名称与最近修改时间；定义与版本在这里读回。
    """
    try:
        state = draft_workspace.load_draft(draft_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[drafts] 打开草稿失败 draftId=%s", draft_id)
        raise HTTPException(status_code=500, detail="打开草稿失败") from exc
    if state is None:
        raise HTTPException(status_code=404, detail="这个草稿不存在。")

    scene_session, _scene_has_dialog = _resolve_draft_session(
        draft_id, _session_id_of(state)
    )
    # 调用方指定了就照它来（它要在这条会话上打开这份草稿）；
    # 没指定就用那份草稿的现场会话。
    session_id = (body.sessionId or "").strip() or scene_session
    if session_id:
        has_dialog = (
            _scene_has_dialog
            if session_id == scene_session
            else session_service.repository.count_messages(session_id) > 0
        )
        try:
            # 切换与快照写回在**同一笔事务**里：写不回去就整笔回滚，
            # 不留「对象切了一半」的半份状态。
            with draft_workspace.repo.transaction():
                draft_workspace.bind_session(draft_id, session_id)
                session_service.set_draft_id(session_id, draft_id)
                _sync_session_snapshot(
                    session_id, state.definition, state.revision
                )
                if not has_dialog:
                    # **空会话**才写草稿的现场（状态 + 待决菜单）：已经有
                    # 对话的那条会话，状态与菜单是它自己的，不许用草稿
                    # 快照覆盖（契约 0002 §6.3）。
                    _sync_session_scene(session_id, state)
                else:
                    # 兼容旧撤销现场：菜单已恢复到编辑态，会话却仍留在保存后的
                    # ANALYZING。只修复属于本草稿的权威编辑菜单，不覆盖活动任务。
                    record = session_service.repository.get_session(session_id)
                    pending = record.pendingChoice if record else None
                    question = (pending or {}).get("question") or {}
                    if (
                        record and record.state == "ANALYZING"
                        and not record.pendingEditPlan
                        and (pending or {}).get("state") == "USER_EDITING_TEMPLATE"
                        and question.get("id") == "template_edit_confirm"
                        and question.get("stepDraftId") == draft_id
                        and not draft_workspace.lock_owner(draft_id)
                    ):
                        task = state.activeTask or {}
                        if task.get("state") == "RUNNING":
                            draft_workspace.fail_task(
                                task["taskId"], reason="reopen_orphaned_turn_without_lock"
                            )
                            state = draft_workspace.load_draft(draft_id)
                        if not state.activeTask:
                            session_service.save_state(session_id, "USER_EDITING_TEMPLATE")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[drafts] 切换草稿对象失败 draftId=%s code=%s errorType=%s",
                draft_id,
                str(getattr(exc, "code", "") or ""),
                type(exc).__name__,
            )
            raise HTTPException(status_code=409, detail="切换草稿对象失败，请重试。") from exc
    payload = _respond(state, session_id=session_id)
    # 告诉前端**切到了哪条会话**（实际用到的那条）—— 前端据此把工作台
    # 换过去（对话、待决菜单、状态一起回来）；并把现场会话也带出去，
    # 方便调用方直接问「这份草稿的现场在哪条会话上」。
    payload["sessionId"] = session_id
    payload["sceneSessionId"] = scene_session
    return payload


@router.post("/drafts/{draft_id}/actions")
def draft_action(draft_id: str, body: DraftActionRequest) -> dict[str, Any]:
    """草稿级动作：SAVE / UNDO / REDO / SAVE_AS。

    **锁授权与版本检查在同一笔事务里**：先看锁、再写，中间不留
    可被抢占的窗口（别的窗口抢不到锁，也进不来这笔事务）。

    `expectedRevision` 对**四个动作都生效**：客户端看到的版本与服务端
    不一致就拒绝 —— 拿旧版本号来的撤销 / 重做 / 另存为不能落到新内容上。
    """
    action = (body.action or "").strip().upper()
    if action not in DRAFT_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的草稿动作：{body.action!r}。允许：{'、'.join(DRAFT_ACTIONS)}。",
        )
    if action in ("SAVE", "SAVE_AS") and body.name is not None:
        body.name = body.name.strip()
        if not body.name or len(body.name) > 100:
            raise HTTPException(status_code=422, detail="草稿名称不能为空，且不能超过 100 个字。")
    if action in ("SAVE", "SAVE_AS") and body.remark is not None:
        # 备注是**用户自己写的一句话**：可以留空（就是没备注），
        # 但不能长到把草稿列表撑破。
        body.remark = body.remark.strip()
        if len(body.remark) > 500:
            raise HTTPException(status_code=422, detail="草稿备注不能超过 500 个字。")
    state: Any = None
    session_id: str | None = None
    payload: dict[str, Any] = {}
    #: 撤销 / 重做之后**失效的步骤 id**（前端据此把旧菜单标成"已撤销"）。
    revoked_step_ids: list[str] = []
    try:
        with draft_workspace.repo.transaction():
            # 锁检查与写入在**同一笔事务**里：事务一开始就拿住写锁，
            # 别的进程要写同一个对象只能等 —— 中间没有可抢占的窗口。
            draft_workspace.require_unlocked(draft_id)
            before = draft_workspace.load_draft(draft_id)
            before_session = _session_id_of(before)
            if action == "SAVE":
                state = draft_workspace.save(
                    draft_id,
                    expected_revision=body.expectedRevision,
                    name=body.name,
                    remark=body.remark,
                )
            elif action == "UNDO":
                state = draft_workspace.undo(
                    draft_id, expected_revision=body.expectedRevision
                )
            elif action == "REDO":
                state = draft_workspace.redo(
                    draft_id, expected_revision=body.expectedRevision
                )
            else:
                state = draft_workspace.save_as(
                    draft_id,
                    name=body.name or "未命名草稿",
                    expected_revision=body.expectedRevision,
                    remark=body.remark,
                )

            session_id = _session_id_of(state) or before_session
            payload = _state_payload(state)
            if action in ("UNDO", "REDO"):
                # 撤销 / 重做之后，把恢复的定义写回会话快照 —— 刷新才保持得住。
                # **在事务里**：写不回去就整笔回滚，不留「工作区已经撤销、
                # 会话仍是旧草稿」的半份状态。
                #
                # **先核对会话当前绑的是哪个对象**：另存为之后会话已经切到
                # 新对象 B，旧对象 A 再被撤销 / 重做时**不许**把 A 的定义
                # 写进那条会话 —— 否则会造出「新 draftId + 旧定义」的混合
                # 会话（旧对象的撤销覆盖了新对象的内容）。
                session_draft_id = (
                    session_service.load_draft_id(session_id) if session_id else None
                )
                if _may_sync_session_snapshot(session_draft_id, draft_id):
                    _sync_session_snapshot(
                        session_id, payload.get("definition"), payload.get("revision")
                    )
                    # **左侧会话现场同步回退**（BUG-008）：只回滚定义不够 ——
                    # 旧菜单 / 旧候选仍是当前待决步骤的话，用户看到的
                    # 仍然是"报表撤销了、对话没回退"。
                    revoked_step_ids = _revoke_session_step(session_id, action=action)
                    # **补一张当前合法的下一步菜单**：完整现场要包含
                    # 「可操作菜单」（docs/agent/state-and-undo.md 第 50 行），
                    # 旧菜单随那一轮失效了，得给用户留一个能走的下一步。
                    _restore_session_menu(
                        session_id,
                        draft_id=draft_id,
                        definition=payload.get("definition"),
                        reply=(
                            "已撤销上一步修改。"
                            if action == "UNDO"
                            else "已重做上一步修改。"
                        ),
                    )
                else:
                    # 旧对象仍可作为**独立对象**做自己的撤销 / 重做
                    # （它自己的版本与内容照旧生效），只是不再回写会话。
                    logger.info(
                        "[drafts] 会话已绑到别的草稿对象，跳过会话同步 "
                        "sessionId=%s actionDraftId=%s sessionDraftId=%s",
                        session_id,
                        draft_id,
                        session_draft_id,
                    )
            if action == "SAVE_AS":
                # 另存为：**新定义事务成功之后**才切新 draftId/会话（契约 0002 §6.4）。
                # 新对象不复制聊天、redo 与旧修改历史，只带当前内容与主题引用。
                #
                # 切换也在**同一笔事务**里：切失败就整笔回滚 —— 不留
                # 「新对象建了一半、旧对象也切了」的半份状态。
                new_draft_id = str(payload.get("draftId") or "")
                if new_draft_id and session_id:
                    draft_workspace.bind_session(new_draft_id, session_id)
                    session_service.set_draft_id(session_id, new_draft_id)
                    _sync_session_snapshot(
                        session_id, payload.get("definition"), payload.get("revision")
                    )
    except Exception as exc:  # noqa: BLE001
        code = str(getattr(exc, "code", "") or "")
        logger.warning(
            "[drafts] 草稿动作失败 draftId=%s action=%s code=%s errorType=%s",
            draft_id,
            action,
            code,
            type(exc).__name__,
        )
        raise _draft_error(exc) from exc

    result = _respond(state, session_id=session_id)
    if action == "SAVE_AS":
        result["previousDraftId"] = draft_id
    if action in ("UNDO", "REDO"):
        # **总是**给这个键（可能是空数组）：前端据此知道"这一步之后没有
        # 被失效的步骤"，而不是"服务端没回这个字段、不知道是什么情况"。
        result["revokedStepIds"] = revoked_step_ids
    return result


@router.post("/draft-tasks/{task_id}/stop")
def stop_draft_task(task_id: str) -> dict[str, Any]:
    """停止一个正在跑的操作。

    停止**不是**断 SSE 即成功：服务端把任务置为 STOPPED，并作废它的
    提交凭证；之后只给两个选项：「继续完成剩下的工作」「撤销（回到上一步）」。

    停止之后这个草稿**继续被锁着**：输入与其他同对象写动作都要先选一个
    选项，不能当作什么都没发生继续写（契约 0002 §7.1）。

    已经结束（完成 / 撤销）的那一轮停不了：把它改成 STOPPED 会让对象
    一直被锁着，谁也解不开。
    """
    try:
        state = draft_workspace.stop_task(task_id)
    except Exception as exc:  # noqa: BLE001
        code = str(getattr(exc, "code", "") or "")
        if code == "task_not_found":
            raise HTTPException(status_code=404, detail="这个任务不存在。") from exc
        if code == "task_not_active":
            raise HTTPException(
                status_code=409, detail="这一轮已经结束了，停不了。"
            ) from exc
        logger.exception("[drafts] 停止任务失败 taskId=%s", task_id)
        raise HTTPException(status_code=500, detail="停止失败") from exc
    return {
        "taskId": task_id,
        "state": str(getattr(state, "state", "") or "STOPPED"),
        "choices": ["继续完成剩下的工作", "撤销（回到上一步）"],
        "draftId": str(getattr(state, "draftId", "") or ""),
    }


@router.post("/draft-tasks/{task_id}/discard")
def discard_draft_task(task_id: str) -> dict[str, Any]:
    """撤销（回到上一步）：丢弃这次被停止的操作。

    「A 已完成、B 没写入就停止」时，这里只清 B 留下的现场（待确认计划、
    选择锁），**不退 A** —— A 是上一个已完成单位，本来就没被 B 动过
    （契约 0002 §7.3）。

    如果 B 已经有落库的阶段，那属于「撤销整个 B 单位」，走草稿的
    UNDO 动作，不走这里。

    放锁只放**自己那一把**：旧任务迟到的撤销不能把新任务的锁放掉。
    """
    task = draft_workspace.load_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="这个任务不存在。")
    draft_id = str(getattr(task, "draftId", "") or "")
    try:
        draft_workspace.rollback_task(task_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[drafts] 回滚任务失败 taskId=%s errorType=%s", task_id, type(exc).__name__
        )
        raise HTTPException(status_code=409, detail="撤销失败，请重试。") from exc

    # 对象回到可写状态：放掉**本任务**的写锁（别的持有者的锁不动）。
    session_id = None
    if draft_id:
        state = draft_workspace.load_draft(draft_id)
        session_id = _session_id_of(state)
    owner = draft_workspace.lock_owner(draft_id) if draft_id else None
    expected = _task_lock_owner(task, session_id)
    operation_id = str(getattr(task, "operationId", "") or "")
    is_own_lock = owner == expected or (
        bool(operation_id) and bool(owner) and owner.endswith(f":{operation_id}")
    )
    if owner and is_own_lock:
        draft_workspace.release(draft_id, owner)

    # 清 B 留下的待确认现场：待确认计划 + 选择锁。
    if session_id:
        session_service.clear_pending_plan(session_id)
        session_service.save_pending_choice(session_id, None)

    result: dict[str, Any] = {
        "taskId": task_id,
        "state": "ROLLED_BACK",
        "draftId": draft_id,
        "sessionId": session_id,
    }
    if draft_id:
        state = draft_workspace.load_draft(draft_id)
        if state is not None:
            result.update(_respond(state, session_id=session_id))
    return result


@router.post("/draft-tasks/{task_id}/resume")
def resume_draft_task(task_id: str) -> dict[str, Any]:
    """从服务端检查点继续：只完成**本次剩余**的工作，不重复已完成阶段。

    续跑拿的是**服务端本轮身份**（会话 + 消息 id → 同一个任务 id）与
    检查点：前端拿同一个 messageId 重发，落到**同一个任务**上，
    写锁**重入**（同一持有者 = 续跑），不会被自己的锁挡成 409。

    响应里带 ``operationId`` 与 ``checkpoint``：前端据此重发同一轮，
    刷新之后也还能从服务端把现场读回来。
    """
    task = draft_workspace.load_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="这个任务不存在。")
    draft_id = str(getattr(task, "draftId", "") or "")
    session_id = _task_session_id(task, draft_id)
    try:
        with draft_workspace.repo.transaction():
            # 交接原锁：写锁回到**本轮身份**手里（同一持有者重入 = 续跑）。
            owner = _task_lock_owner(task, session_id)
            if draft_id and owner:
                if not draft_workspace.acquire(draft_id, owner):
                    raise HTTPException(
                        status_code=409,
                        detail="这个草稿正在被另一个窗口修改，请先在那边停止或等它结束。",
                    )
            state = draft_workspace.resume_task(task_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        code = str(getattr(exc, "code", "") or "")
        if code == "task_not_found":
            raise HTTPException(status_code=404, detail="这个任务不存在。") from exc
        if code == "task_not_active":
            raise HTTPException(
                status_code=409, detail="这一轮已经结束了，不能再继续。"
            ) from exc
        logger.exception("[drafts] 继续任务失败 taskId=%s", task_id)
        raise HTTPException(status_code=500, detail="继续失败") from exc
    return {
        "taskId": task_id,
        "state": str(getattr(state, "state", "") or "RUNNING"),
        "checkpoint": getattr(state, "checkpoint", None),
        "operationId": str(getattr(state, "operationId", "") or ""),
        "draftId": draft_id,
        "sessionId": session_id,
        "choices": ["继续完成剩下的工作", "撤销（回到上一步）"],
    }
