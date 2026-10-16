"""Mock 目录 / 指标服务测试：FakeAgent 的事实来源不能被改坏。"""
from __future__ import annotations

from app.services import catalog_mock_service, metric_mock_service


def test_machine_room_catalog_registered():
    catalogs = catalog_mock_service.list_catalogs("machine_room")
    refs = {c["sourceRef"] for c in catalogs}
    assert "catalog.machine_room.energy" in refs


def test_minute_grain_is_not_supported():
    """首功能明确：机房能耗没有分钟级历史，样例 4 的拦截依赖这一点。"""
    source_ref = "catalog.machine_room.energy"
    assert catalog_mock_service.supports_grain(source_ref, "HOUR") is True
    assert catalog_mock_service.supports_grain(source_ref, "DAY") is True
    assert catalog_mock_service.supports_grain(source_ref, "MINUTE") is False


def test_metrics_registered():
    refs = {m["metricRef"] for m in metric_mock_service.list_metrics()}
    assert refs >= {
        "metric.machine_room.total_energy",
        "metric.machine_room.device_energy",
    }


def test_metric_units_are_kwh():
    for ref in (
        "metric.machine_room.total_energy",
        "metric.machine_room.device_energy",
    ):
        metric = metric_mock_service.get_metric(ref)
        assert metric is not None
        assert metric["unit"] == "kWh"
