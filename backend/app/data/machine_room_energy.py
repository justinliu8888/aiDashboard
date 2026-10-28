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
        "deviceType": {"dataType": "STRING", "semanticType": "CATEGORY"},
        "timestamp": {"dataType": "DATETIME", "semanticType": "TIMESTAMP"},
        "energyKwh": {
            "dataType": "DECIMAL",
            "unit": "kWh",
            "semanticType": "INTERVAL_ENERGY",
        },
    },
    "permissionTags": ["project_scoped"],
}


# 额外的明细数据源，用于演示“改数据源”和分钟级能力
CATALOG_EAS_ENERGY_DETAIL: dict[str, Any] = {
    "sourceRef": "eas_energy_detail",
    "sourceVersion": "v1",
    "subjectRef": "machine_room",
    "description": "EAS 能耗明细：按设备、分钟级采样的区间电量",
    "supportedGrains": ["MINUTE", "HOUR", "DAY"],
    "fields": {
        "deviceId": {"dataType": "STRING", "semanticType": "IDENTIFIER"},
        "deviceName": {"dataType": "STRING"},
        "deviceType": {"dataType": "STRING", "semanticType": "CATEGORY"},
        "timestamp": {"dataType": "DATETIME", "semanticType": "TIMESTAMP"},
        "energyKwh": {
            "dataType": "DECIMAL",
            "unit": "kWh",
            "semanticType": "INTERVAL_ENERGY",
        },
    },
    "permissionTags": ["project_scoped"],
}


# 日汇总数据源，仅支持 DAY 粒度，用于演示“粒度不支持”
CATALOG_ENERGY_DAILY_SUMMARY: dict[str, Any] = {
    "sourceRef": "catalog.machine_room.energy_daily_summary",
    "sourceVersion": "1.0",
    "subjectRef": "machine_room",
    "description": "机房能耗日汇总：仅支持天粒度",
    "supportedGrains": ["DAY"],
    "fields": {
        "machineRoomId": {"dataType": "STRING", "semanticType": "IDENTIFIER"},
        "logicalDate": {"dataType": "DATE"},
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
    "subjectRef": "machine_room",
    "label": "总耗电",
    "unit": "kWh",
    "aggregation": "SUM",
    "fieldRef": "energyKwh",
    "dimensions": ["machineRoomId"],
    "description": "机房所有设备区间电量求和",
    "permissionTags": ["project_scoped"],
}


METRIC_DEVICE_ENERGY: dict[str, Any] = {
    "metricRef": "metric.machine_room.device_energy",
    "metricVersion": "1.0",
    "subjectRef": "machine_room",
    "label": "设备耗电",
    "unit": "kWh",
    "aggregation": "SUM",
    "fieldRef": "energyKwh",
    "dimensions": ["deviceId"],
    "description": "按设备聚合的区间电量",
    "permissionTags": ["project_scoped"],
}


METRIC_ENERGY_COST: dict[str, Any] = {
    "metricRef": "metric.machine_room.energy_cost",
    "metricVersion": "1.0",
    "subjectRef": "machine_room",
    "label": "能耗成本",
    "unit": "元",
    "aggregation": "SUM",
    "fieldRef": "energyCost",
    "dimensions": ["machineRoomId"],
    "description": "按单价换算的能耗成本",
    "permissionTags": ["project_scoped"],
}


METRIC_PUE: dict[str, Any] = {
    "metricRef": "metric.machine_room.pue",
    "metricVersion": "1.0",
    "subjectRef": "machine_room",
    "label": "PUE",
    "unit": "",
    "aggregation": "AVG",
    "fieldRef": "pue",
    "dimensions": ["machineRoomId"],
    "description": "机房 PUE 平均值",
    "permissionTags": ["project_scoped"],
}


# 当前用户在首功能阶段默认授权的项目/机房范围
AUTHORIZED_SCOPE: dict[str, Any] = {
    "projectIds": ["project_001"],
    # subjectRef -> 该业务对象下被授权的实例 id。
    # 工具层按它过滤目录 / 指标，防止模型跨对象探测。
    "subjectIds": {"machine_room": ["room_a"]},
}


# ---------- 设备类别 ----------
#
# 「只统计空调设备」「不要统计机架、PDU 和 UPS」这类过滤需要按设备类别走，
# 所以设备类别是目录里的一等字段（deviceType），不是从名字猜的。
# 值用稳定的英文枚举，中文只作为别名给模型 / 用户看。
DEVICE_TYPE_RACK = "RACK"
DEVICE_TYPE_PDU = "PDU"
DEVICE_TYPE_CRAC = "CRAC"
DEVICE_TYPE_UPS = "UPS"

DEVICE_TYPES: dict[str, str] = {
    DEVICE_TYPE_RACK: "机架",
    DEVICE_TYPE_PDU: "配电",
    DEVICE_TYPE_CRAC: "空调",
    DEVICE_TYPE_UPS: "不间断电源",
}

# 自然语言 -> 设备类别枚举。模型解析用户表述时用它对齐到 deviceType。
DEVICE_TYPE_ALIASES: dict[str, str] = {
    "机架": DEVICE_TYPE_RACK,
    "机柜": DEVICE_TYPE_RACK,
    "rack": DEVICE_TYPE_RACK,
    "配电": DEVICE_TYPE_PDU,
    "pdu": DEVICE_TYPE_PDU,
    "空调": DEVICE_TYPE_CRAC,
    "crac": DEVICE_TYPE_CRAC,
    "精密空调": DEVICE_TYPE_CRAC,
    "不间断": DEVICE_TYPE_UPS,
    "ups": DEVICE_TYPE_UPS,
    "不间断电源": DEVICE_TYPE_UPS,
}

