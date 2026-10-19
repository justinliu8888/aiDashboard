"""MetricSkill：查指标目录。

参数：
    metricRef:  可选，查具体指标
    subjectRef: 可选，按 subject 过滤

返回：
    [{metricRef, metricVersion, name, unit, dimensions, ...}]
"""
from __future__ import annotations

from typing import Any

from app.services.metric_mock_service import get_metric, list_metrics


NAME = "search_metric"
DESCRIPTION = (
    "查询可用的指标口径。参数 metricRef 查具体指标；"
    "参数 subjectRef 按业务对象过滤。"
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "metricRef": {"type": "string", "description": "可选，指标 ID"},
        "subjectRef": {"type": "string", "description": "可选，业务对象"},
    },
}


def run(metricRef: str | None = None, subjectRef: str | None = None) -> dict[str, Any]:
    if metricRef:
        metric = get_metric(metricRef)
        return {"items": [metric] if metric else []}
    return {"items": list_metrics(subjectRef)}
