"""check_capability：检查数据源是否支持某个粒度（只读事实）。

参数：
    sourceRef:  必填
    grainUnit:  必填，MINUTE / HOUR / DAY / WEEK / MONTH

返回：
    {sourceRef, grainUnit, supported: bool, supportedGrains: [...]}

即使调用方直接给出 sourceRef，也必须先过授权校验：
列表里看不到的数据源，不能靠"我知道 ID"来探测它的能力。
"""
from __future__ import annotations

from typing import Any

from app.services.catalog_mock_service import get_catalog, supports_grain, supported_grains
from app.services.scope_service import is_item_visible
from app.tools.base import LOOKUP_STATES, ToolCategory
from app.tools.context import require_scope


NAME = "check_capability"
DESCRIPTION = (
    "检查数据源是否支持某个时间粒度，返回 supported 和该数据源支持的粒度列表。"
    "若用户要求的粒度不支持，不要硬改，应改用支持的粒度或换数据源，"
    "并在回复中说明原因。"
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
CATEGORY = ToolCategory.FACT
IS_READ_ONLY = True
APPLICABLE_STATES = LOOKUP_STATES


def run(sourceRef: str, grainUnit: str) -> dict[str, Any]:
    scope = require_scope()
    catalog = get_catalog(sourceRef)
    if catalog is None:
        return {
            "sourceRef": sourceRef,
            "grainUnit": grainUnit,
            "supported": False,
            "supportedGrains": [],
        }
    if not is_item_visible(
        catalog,
        project_id=scope.project_id,
        subject_ref=scope.subject_ref,
        subject_id=scope.subject_id,
    ):
        # 越权查询不返回任何能力细节，避免探测。
        return {
            "sourceRef": sourceRef,
            "grainUnit": grainUnit,
            "supported": False,
            "supportedGrains": [],
        }
    return {
        "sourceRef": sourceRef,
        "grainUnit": grainUnit,
        "supported": supports_grain(sourceRef, grainUnit),
        "supportedGrains": supported_grains(sourceRef),
    }
