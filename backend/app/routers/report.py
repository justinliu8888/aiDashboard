"""报表预览数据接口：GET /api/report/preview/{reportRunId}。

前端在 PREVIEW_READY → RENDERING 时，通过 reportRunId 把每个 region 的实际数据
拉下来渲染（KPI / 表格 / 图表 / 质量 / 分析）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.preview_service import preview_service


router = APIRouter(prefix="/api/report", tags=["report"])


@router.get("/preview/{report_run_id}")
def get_report_preview(report_run_id: str) -> dict:
    payload = preview_service.get_result_payload(report_run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="preview not found")
    return payload
