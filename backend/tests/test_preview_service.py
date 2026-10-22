"""预览服务测试：预览数据必须跟随草稿里的 compilation 粒度。"""
from __future__ import annotations

from typing import Any

from app.services.preview_service import preview_service
from app.services.report_draft_service import REGION_IDS, build_definition_draft


def _trend_region(definition: dict[str, Any]) -> dict[str, Any]:
    return next(region for region in definition["regions"] if region["id"] == REGION_IDS["trend"])


def _trend_payload(definition: dict[str, Any]) -> dict[str, Any]:
    _preview, record = preview_service.run_preview(
        definitionId="def_preview_test",
        definitionDraft=definition,
    )
    return next(region for region in record.regions if region["regionId"] == REGION_IDS["trend"])["payload"]


def test_preview_uses_hourly_trend_by_default():
    definition = build_definition_draft("A 机房能耗日报", "room_a").model_dump(mode="json")

    payload = _trend_payload(definition)

    assert payload["grain"] == "HOUR"
    assert len(payload["xLabels"]) == 24
    assert payload["xLabels"][:2] == ["00:00", "01:00"]


def test_preview_uses_minute_trend_when_grain_is_minute():
    definition = build_definition_draft("A 机房能耗日报", "room_a").model_dump(mode="json")
    trend = _trend_region(definition)
    trend["context"]["grain"] = {"mode": "REPLACE", "value": {"unit": "MINUTE"}}

    payload = _trend_payload(definition)

    assert payload["grain"] == "MINUTE"
    assert len(payload["xLabels"]) == 24 * 60
    assert payload["xLabels"][:3] == ["00:00", "00:01", "00:02"]
