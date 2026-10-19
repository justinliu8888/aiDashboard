"""CapabilitySkill：检查数据源是否支持某个粒度。

参数：
    sourceRef:  必填
    grainUnit:  必填，MINUTE / HOUR / DAY / WEEK / MONTH

返回：
    {sourceRef, grainUnit, supported: bool}
"""
from __future__ import annotations

from app.services.catalog_mock_service import supports_grain


NAME = "check_capability"
DESCRIPTION = (
    "检查数据源是否支持某个时间粒度。"
    "若用户要求的粒度不支持，Agent 必须返回 BLOCKED_OR_UNSUPPORTED。"
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "sourceRef": {"type": "string", "description": "数据源 ID"},
        "grainUnit": {
            "type": "string",
            "enum": ["MINUTE", "HOUR", "DAY", "WEEK", "MONTH"],
            "description": "时间粒度",
        },
    },
    "required": ["sourceRef", "grainUnit"],
}


def run(sourceRef: str, grainUnit: str) -> dict:
    return {
        "sourceRef": sourceRef,
        "grainUnit": grainUnit,
        "supported": supports_grain(sourceRef, grainUnit),
    }
