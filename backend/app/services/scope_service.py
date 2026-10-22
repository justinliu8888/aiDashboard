"""项目 / 业务对象的授权范围。

模型在 ReAct 循环里可以自由调工具，所以「能不能看到某个数据源」必须在
工具层就按当前请求的上下文过滤，不能只在上层路由拦一次 ——
否则模型只要知道 sourceRef，就能绕过列表接口直接探测别的项目的数据。
"""
from __future__ import annotations

from typing import Any

from app.data.machine_room_energy import AUTHORIZED_SCOPE


def authorized_project_ids() -> list[str]:
    """当前账号被授权的项目。"""
    return list(AUTHORIZED_SCOPE.get("projectIds", []))


def authorized_subject_ids(subject_ref: str | None) -> list[str]:
    """某个业务对象下被授权的实例 id。"""
    if not subject_ref:
        return []
    mapping = AUTHORIZED_SCOPE.get("subjectIds", {})
    return list(mapping.get(subject_ref, []))


def is_project_authorized(project_id: str | None) -> bool:
    if not project_id:
        return False
    return project_id in authorized_project_ids()


def is_subject_authorized(subject_ref: str | None, subject_id: str | None) -> bool:
    if not subject_id:
        return False
    return subject_id in authorized_subject_ids(subject_ref)


def is_item_visible(
    item: dict[str, Any],
    *,
    project_id: str | None,
    subject_ref: str | None,
    subject_id: str | None,
) -> bool:
    """一条目录 / 指标对当前请求是否可见。

    规则：
        1. permissionTags 含 project_scoped → 项目必须在授权范围内；
        2. 业务对象必须与请求一致；
        3. 该业务对象的实例必须在授权范围内。
    """
    tags = item.get("permissionTags") or []
    if "project_scoped" in tags and not is_project_authorized(project_id):
        return False

    item_subject = item.get("subjectRef")
    if subject_ref and item_subject and item_subject != subject_ref:
        return False
    if item_subject and not is_subject_authorized(item_subject, subject_id):
        return False
    return True


def filter_visible(
    items: list[dict[str, Any]],
    *,
    project_id: str | None,
    subject_ref: str | None,
    subject_id: str | None,
) -> list[dict[str, Any]]:
    """按授权范围过滤条目。"""
    return [
        item
        for item in items
        if is_item_visible(
            item,
            project_id=project_id,
            subject_ref=subject_ref,
            subject_id=subject_id,
        )
    ]
