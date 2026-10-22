"""search_catalog：查数据目录（只读事实）。

参数：
    sourceRef:  可选，查某个具体数据源
    subjectRef: 可选，按 subject 过滤

返回：
    {items: [{sourceRef, sourceVersion, subjectRef, supportedGrains, fields}],
     total: int, truncated: bool}

返回结果只包含当前项目 / 业务对象授权范围内的数据源。
"""
from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.services.catalog_mock_service import get_catalog, list_catalogs
from app.services.scope_service import filter_visible
from app.tools.base import LOOKUP_STATES, ToolCategory
from app.tools.context import require_scope


NAME = "search_catalog"
DESCRIPTION = (
    "查询当前项目授权范围内可用的数据目录（数据源）。参数 sourceRef 查具体数据源；"
    "参数 subjectRef 按业务对象过滤。"
    "返回的数据源包含 supportedGrains（支持的时间粒度）和 fields（可用字段），"
    "生成报表前必须先确认目标数据源存在且支持所需粒度。"
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "sourceRef": {"type": "string", "description": "可选，数据源 ID"},
        "subjectRef": {"type": "string", "description": "可选，业务对象，如 machine_room"},
        "limit": {
            "type": "integer",
            "description": "可选，最多返回几条；超出会标 truncated=true 并给出 total",
        },
    },
}
CATEGORY = ToolCategory.FACT
IS_READ_ONLY = True
APPLICABLE_STATES = LOOKUP_STATES


def run(
    sourceRef: str | None = None,
    subjectRef: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    scope = require_scope()
    if sourceRef:
        catalog = get_catalog(sourceRef)
        items = [catalog] if catalog else []
    else:
        items = list_catalogs(subjectRef)
    items = filter_visible(
        items,
        project_id=scope.project_id,
        subject_ref=scope.subject_ref,
        subject_id=scope.subject_id,
    )
    return _bounded(items, limit)


def _bounded(items: list[dict[str, Any]], limit: int | None) -> dict[str, Any]:
    """裁剪 Observation：真实目录规模可能是几百条，不能整包塞进上下文。"""
    max_items = limit or get_settings().observation_max_items
    total = len(items)
    return {
        "items": items[:max_items],
        "total": total,
        "truncated": total > max_items,
    }
