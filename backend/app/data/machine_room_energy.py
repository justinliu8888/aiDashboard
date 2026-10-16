"""首功能机房耗电量的 mock 数据：目录能力、指标口径、机房资产。"""
from __future__ import annotations

from typing import Any


CATALOG_MACHINE_ROOM_ENERGY: dict[str, Any] = {
    "sourceRef": "catalog.machine_room.energy",
    "sourceVersion": "1.0",
    "subjectRef": "machine_room",
    "description": "机房耗电量数据能力：按设备、小时聚合的区间电量",
    "supportedGrains": ["HOUR", "DAY"],  # 不含分钟级
    "fields": {
        "deviceId": {"dataType": "STRING", "semanticType": "IDENTIFIER"},
        "deviceName": {"dataType": "STRING"},
        "timestamp": {"dataType": "DATETIME", "semanticType": "TIMESTAMP"},
        "energyKwh": {
            "dataType": "DECIMAL",
            "unit": "kWh",
            "semanticType": "INTERVAL_ENERGY",
        },
    },
    "permissionTags": ["project_scoped"],
}


METRIC_TOTAL_ENERGY: dict[str, Any] = {
    "metricRef": "metric.machine_room.total_energy",
    "metricVersion": "1.0",
    "label": "总耗电",
    "unit": "kWh",
    "aggregation": "SUM",
    "fieldRef": "energyKwh",
    "dimensions": ["machineRoomId"],
    "description": "机房所有设备区间电量求和",
}


METRIC_DEVICE_ENERGY: dict[str, Any] = {
    "metricRef": "metric.machine_room.device_energy",
    "metricVersion": "1.0",
    "label": "设备耗电",
    "unit": "kWh",
    "aggregation": "SUM",
    "fieldRef": "energyKwh",
    "dimensions": ["deviceId"],
    "description": "按设备聚合的区间电量",
}


# 当前用户在首功能阶段默认授权的项目/机房范围
AUTHORIZED_SCOPE: dict[str, Any] = {
    "projectIds": ["project_001"],
    "machineRoomIds": ["room_a"],
}
