"""数据目录 mock 服务，供 FakeAgent 和 TemplateEditEngine 查询可用数据源与粒度。"""
from __future__ import annotations

from typing import Any

from app.data.machine_room_energy import (
    CATALOG_EAS_ENERGY_DETAIL,
    CATALOG_ENERGY_DAILY_SUMMARY,
    CATALOG_MACHINE_ROOM_ENERGY,
)


_CATALOG: dict[str, dict[str, Any]] = {
    CATALOG_MACHINE_ROOM_ENERGY["sourceRef"]: CATALOG_MACHINE_ROOM_ENERGY,
    CATALOG_EAS_ENERGY_DETAIL["sourceRef"]: CATALOG_EAS_ENERGY_DETAIL,
    CATALOG_ENERGY_DAILY_SUMMARY["sourceRef"]: CATALOG_ENERGY_DAILY_SUMMARY,
}


# 对外展示的数据源别名，用户常用自然语言可以匹配
_SOURCE_ALIASES: dict[str, list[str]] = {
    CATALOG_EAS_ENERGY_DETAIL["sourceRef"]: [
        "eas 能耗明细",
        "eas能耗明细",
        "eas 明细",
        "eas能耗",
        "能耗明细",
    ],
    CATALOG_ENERGY_DAILY_SUMMARY["sourceRef"]: [
        "日汇总",
        "能耗日汇总",
        "汇总数据",
    ],
    CATALOG_MACHINE_ROOM_ENERGY["sourceRef"]: [
        "机房能耗",
        "能耗数据",
    ],
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


def supported_grains(source_ref: str) -> list[str]:
    catalog = get_catalog(source_ref)
    if not catalog:
        return []
    return list(catalog.get("supportedGrains", []))


def match_source_by_alias(text: str, allowed_refs: set[str] | None = None) -> str | None:
    """根据自然语言匹配数据源 sourceRef，匹配不到返回 None。"""
    normalized = (text or "").lower()
    if not normalized:
        return None
    for source_ref, aliases in _SOURCE_ALIASES.items():
        if allowed_refs is not None and source_ref not in allowed_refs:
            continue
        for alias in aliases:
            if alias.lower() in normalized:
                return source_ref
    # 直接命中 sourceRef 本身
    for source_ref in _CATALOG:
        if allowed_refs is not None and source_ref not in allowed_refs:
            continue
        if source_ref.lower() in normalized:
            return source_ref
    return None
