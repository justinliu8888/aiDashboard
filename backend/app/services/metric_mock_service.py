"""指标目录 mock 服务。"""
from __future__ import annotations

from typing import Any

from app.data.machine_room_energy import (
    METRIC_DEVICE_ENERGY,
    METRIC_ENERGY_COST,
    METRIC_PUE,
    METRIC_TOTAL_ENERGY,
)


_METRICS: dict[str, dict[str, Any]] = {
    METRIC_TOTAL_ENERGY["metricRef"]: METRIC_TOTAL_ENERGY,
    METRIC_DEVICE_ENERGY["metricRef"]: METRIC_DEVICE_ENERGY,
    METRIC_ENERGY_COST["metricRef"]: METRIC_ENERGY_COST,
    METRIC_PUE["metricRef"]: METRIC_PUE,
}


_METRIC_ALIASES: dict[str, list[str]] = {
    METRIC_TOTAL_ENERGY["metricRef"]: ["总耗电", "总用电", "总用电量"],
    METRIC_DEVICE_ENERGY["metricRef"]: ["设备耗电", "设备用电"],
    METRIC_ENERGY_COST["metricRef"]: ["能耗成本", "电费", "电费成本"],
    METRIC_PUE["metricRef"]: ["pue"],
}


def list_metrics(subject_ref: str | None = None) -> list[dict[str, Any]]:
    items = list(_METRICS.values())
    if subject_ref:
        items = [m for m in items if m.get("subjectRef") == subject_ref]
    return items


def get_metric(metric_ref: str) -> dict[str, Any] | None:
    return _METRICS.get(metric_ref)


def match_metric_by_alias(text: str, allowed_refs: set[str] | None = None) -> str | None:
    """根据自然语言匹配 metricRef，匹配不到返回 None。"""
    normalized = (text or "").lower()
    if not normalized:
        return None
    best: tuple[int, str] | None = None
    for metric_ref, aliases in _METRIC_ALIASES.items():
        if allowed_refs is not None and metric_ref not in allowed_refs:
            continue
        for alias in aliases:
            if alias.lower() in normalized:
                score = len(alias)
                if best is None or score > best[0]:
                    best = (score, metric_ref)
    return best[1] if best else None
