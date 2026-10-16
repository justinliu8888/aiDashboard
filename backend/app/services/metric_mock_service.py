"""指标目录 mock 服务。"""
from __future__ import annotations

from typing import Any

from app.data.machine_room_energy import METRIC_DEVICE_ENERGY, METRIC_TOTAL_ENERGY


_METRICS: dict[str, dict[str, Any]] = {
    METRIC_TOTAL_ENERGY["metricRef"]: METRIC_TOTAL_ENERGY,
    METRIC_DEVICE_ENERGY["metricRef"]: METRIC_DEVICE_ENERGY,
}


def list_metrics(subject_ref: str | None = None) -> list[dict[str, Any]]:
    # subject_ref 预留；首功能两条指标都用于 machine_room
    return list(_METRICS.values())


def get_metric(metric_ref: str) -> dict[str, Any] | None:
    return _METRICS.get(metric_ref)
