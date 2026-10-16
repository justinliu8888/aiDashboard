"""调试用 mock 目录与指标接口。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.services import catalog_mock_service, metric_mock_service


router = APIRouter(prefix="/api/mock", tags=["mock"])


@router.get("/catalogs")
def list_catalogs(subjectRef: str | None = None) -> list[dict[str, Any]]:
    return catalog_mock_service.list_catalogs(subjectRef)


@router.get("/metrics")
def list_metrics(subjectRef: str | None = None) -> list[dict[str, Any]]:
    return metric_mock_service.list_metrics(subjectRef)
