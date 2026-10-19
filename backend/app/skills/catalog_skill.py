"""CatalogSkill：查数据目录。

参数：
    sourceRef:  可选，查某个具体数据源
    subjectRef: 可选，按 subject 过滤

返回：
    [{sourceRef, sourceVersion, subjectRef, supportedGrains, fields}]
"""
from __future__ import annotations

from typing import Any

from app.services.catalog_mock_service import get_catalog, list_catalogs


NAME = "search_catalog"
DESCRIPTION = (
    "查询可用的数据目录（数据源）。参数 sourceRef 查具体数据源；"
    "参数 subjectRef 按业务对象过滤。"
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "sourceRef": {"type": "string", "description": "可选，数据源 ID"},
        "subjectRef": {"type": "string", "description": "可选，业务对象，如 machine_room"},
    },
}


def run(sourceRef: str | None = None, subjectRef: str | None = None) -> dict[str, Any]:
    if sourceRef:
        catalog = get_catalog(sourceRef)
        return {"items": [catalog] if catalog else []}
    return {"items": list_catalogs(subjectRef)}
