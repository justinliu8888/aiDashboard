"""数据目录 mock 服务，供 FakeAgent 查询可用数据源与粒度。"""
from __future__ import annotations

from typing import Any

from app.data.machine_room_energy import CATALOG_MACHINE_ROOM_ENERGY


_CATALOG: dict[str, dict[str, Any]] = {
    CATALOG_MACHINE_ROOM_ENERGY["sourceRef"]: CATALOG_MACHINE_ROOM_ENERGY,
}


def list_catalogs(subject_ref: str | None = None) -> list[dict[str, Any]]:
    items = list(_CATALOG.values())
    if subject_ref:
        items = [c for c in items if c["subjectRef"] == subject_ref]
    return items


def get_catalog(source_ref: str) -> dict[str, Any] | None:
    return _CATALOG.get(source_ref)


def supports_grain(source_ref: str, grain_unit: str) -> bool:
    catalog = get_catalog(source_ref)
    if not catalog:
        return False
    return grain_unit in catalog.get("supportedGrains", [])
