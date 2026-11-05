"""修改事项（edit item）：同一件事在多轮之间的事实载体。

需求稿要求：「后续补充要关联**同一个修改事项**，保留原要求、已确认的目标、
有效修改信息及候选状态」「即使上一版试做或执行失败，仍要保留足够信息供下一轮
补充或纠正。这些事实由**后端现场**保存，不能只依赖模型对话记忆。」

所以这里把「一件事」落到**会话快照**的一个键里：

* ``operations``     本次的动态操作清单（模型给的结构化参数）
* ``resolvedOperations`` 最近一次**试做通过**后真正落下的结果（引用、标题、图表类型）
* ``refs``           新建区块的稳定引用表
* ``status``         drafting（还在攒）/ checked（已有已检查候选）/ superseded（旧候选已失效）
* ``originalRequest`` 用户最初那句话
* ``baseRevision``   这一件事基于的草稿版本

试做失败也**保留**这里的内容 —— 失败只影响 ``status``，不清空事实。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.schemas.report_definition import ReportDefinitionDraft
from app.services.session_context import current_session_key
from app.services.session_service import session_service


#: 会话快照里的键名。
EDIT_ITEM_KEY = "editItem"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 权威草稿
# --------------------------------------------------------------------------

def load_authoritative_definition(
    session_id: str | None = None,
    *,
    fallback: Any = None,
) -> tuple[ReportDefinitionDraft | None, str]:
    """读当前草稿，返回 ``(草稿, 来源)``。

    权威来源是**服务端会话记录**（快照里的 ``definition``）—— 模型记忆、
    客户端回传的快照都不能覆盖服务端事实。

    会话里**还没有**草稿时，退回本轮请求带回来的那份（``fallback``），
    并把来源标成 ``request_snapshot``：这时它只是上一轮服务端自己的数据
    绕了一圈回来，不是服务端现场。与 ``get_current_draft`` 同一套口径。
    """
    key = session_id or current_session_key()
    if key:
        snapshot = session_service.load_snapshot(key)
        raw = snapshot.get("definition")
        if isinstance(raw, dict):
            try:
                return ReportDefinitionDraft.model_validate(raw), "server_session"
            except Exception:  # noqa: BLE001 — 快照坏了就当读不到，不猜
                pass
    if fallback is not None:
        return fallback, "request_snapshot"
    return None, ""


def load_preview_context(definition: ReportDefinitionDraft) -> dict[str, Any] | None:
    """给这份草稿配一份示意上下文（造数用）。

    优先复用会话快照里已经冻好的上下文（时间窗要稳定），否则按定义现算。
    """
    key = current_session_key()
    snapshot = session_service.load_snapshot(key) if key else {}
    context = snapshot.get("previewContext")
    if isinstance(context, dict) and context:
        return context
    from app.services.preview_service import build_preview_context

    try:
        return build_preview_context(definition.model_dump(mode="json"))
    except Exception:  # noqa: BLE001 — 上下文算不出来就不做载荷核验
        return None


# --------------------------------------------------------------------------
# 事项的读 / 写
# --------------------------------------------------------------------------

def load_edit_item(session_id: str | None = None) -> dict[str, Any] | None:
    """读回当前修改事项；没有返回 ``None``。"""
    key = session_id or current_session_key()
    if not key:
        return None
    snapshot = session_service.load_snapshot(key)
    item = snapshot.get(EDIT_ITEM_KEY)
    return dict(item) if isinstance(item, dict) else None


def save_edit_item(
    item: dict[str, Any], session_id: str | None = None
) -> dict[str, Any]:
    """把修改事项写回会话快照。"""
    key = session_id or current_session_key()
    if not key:
        raise ValueError("没有会话标识，修改事项无处可存。")
    payload = dict(item)
    payload["updatedAt"] = _now()
    session_service.update_snapshot(
        key, lambda snapshot: {**snapshot, EDIT_ITEM_KEY: payload}
    )
    return payload


def update_edit_item(
    session_id: str | None = None, **changes: Any
) -> dict[str, Any] | None:
    """局部更新当前修改事项（不存在就返回 None）。"""
    key = session_id or current_session_key()
    item = load_edit_item(key)
    if item is None:
        return None
    item.update(changes)
    return save_edit_item(item, key)


def clear_edit_item(session_id: str | None = None) -> None:
    """清掉当前修改事项（取消 / 接受完成之后）。"""
    key = session_id or current_session_key()
    if not key:
        return
    session_service.update_snapshot(
        key,
        lambda snapshot: {
            name: value
            for name, value in snapshot.items()
            if name != EDIT_ITEM_KEY
        },
    )


def new_item(
    *,
    original_request: str,
    base_revision: int,
    draft_id: str = "",
    item_id: str = "",
) -> dict[str, Any]:
    """开一件新事项。"""
    return {
        "itemId": item_id or f"item_{uuid.uuid4().hex}",
        "originalRequest": original_request,
        "baseRevision": int(base_revision),
        "draftId": draft_id,
        "operations": [],
        "resolvedOperations": [],
        "refs": {},
        "candidateId": "",
        "candidatePlanId": "",
        "status": "drafting",
        "updatedAt": _now(),
    }


def is_capability_item(item: dict[str, Any] | None) -> bool:
    """这份事项是不是**能力格式**（操作清单里带 op）。"""
    if not item:
        return False
    for op in item.get("operations") or []:
        if isinstance(op, dict) and str(op.get("op") or "").strip():
            return True
    return False
