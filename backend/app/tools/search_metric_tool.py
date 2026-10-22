"""search_metric：查指标目录（只读事实）。

参数：
    metricRef:  可选，查具体指标
    subjectRef: 可选，按 subject 过滤

返回：
    {items: [{metricRef, metricVersion, name, unit, dimensions, ...}],
     total: int, truncated: bool}

返回结果只包含当前项目 / 业务对象授权范围内的指标。
"""
from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.services.metric_mock_service import get_metric, list_metrics
from app.services.scope_service import filter_visible
from app.tools.base import LOOKUP_STATES, ToolCategory
from app.tools.context import require_scope


NAME = "search_metric"
DESCRIPTION = (
    "查询当前项目授权范围内可用的指标口径。参数 metricRef 查具体指标；"
    "参数 subjectRef 按业务对象过滤。"
    "返回的指标包含 unit（单位）和 fieldRef（底层字段），"
    "必须使用查询到的指标，禁止凭空编造指标名或单位。"
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "metricRef": {"type": "string", "description": "可选，指标 ID"},
        "subjectRef": {"type": "string", "description": "可选，业务对象"},
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
    metricRef: str | None = None,
    subjectRef: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    scope = require_scope()
    if metricRef:
        metric = get_metric(metricRef)
        items = [metric] if metric else []
    else:
        items = list_metrics(subjectRef)
    items = filter_visible(
        items,
        project_id=scope.project_id,
        subject_ref=scope.subject_ref,
        subject_id=scope.subject_id,
    )
    return _bounded(items, limit)


def _bounded(items: list[dict[str, Any]], limit: int | None) -> dict[str, Any]:
    """裁剪 Observation：真实指标目录规模可能很大，不能整包塞进上下文。"""
    max_items = limit or get_settings().observation_max_items
    total = len(items)
    return {
        "items": items[:max_items],
        "total": total,
        "truncated": total > max_items,
    }
