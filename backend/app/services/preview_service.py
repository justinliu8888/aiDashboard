"""预览服务（节点 9 PREVIEWING）。

真实版由 Java 预览引擎跑真实 SQL/ClickHouse 查询。本模块：
    - 读取 saved definition；
    - 为每个 region 根据组件类型从 mock_data_service 拿对应数据；
    - 产出 ReportRunPreview + resultRef（指向内存里的真实数据）。

数据协议说明：
    - 每个 region 的实际渲染数据存在 _preview_results 字典里，key = reportRunId；
    - 前端拿到 reportRunId 后，调 GET /api/report/preview/{reportRunId} 读详细数据渲染。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from threading import Lock
from typing import Any

from app.schemas.agent import QualitySummary, ReportRunPreview
from app.services.mock_data_service import (
    RoomEnergySnapshot,
    build_snapshot,
    resolve_room_name,
    snapshot_to_dict,
)


logger = logging.getLogger(__name__)


@dataclass
class PreviewRecord:
    reportRunId: str
    definitionId: str
    snapshot: RoomEnergySnapshot
    regions: list[dict[str, Any]]
    qualitySummary: QualitySummary
    logicalDate: str
    reportName: str
    anomalies: list[dict[str, Any]]


class PreviewService:
    def __init__(self) -> None:
        self._store: dict[str, PreviewRecord] = {}
        self._lock = Lock()

    def run_preview(
        self,
        *,
        definitionId: str,
        definitionDraft: dict[str, Any],
    ) -> tuple[ReportRunPreview, PreviewRecord]:
        """根据报表定义跑一次预览。"""
        scope = (definitionDraft.get("defaults") or {}).get("scope") or {}
        selectors = scope.get("selectors") or {}
        room_ids = selectors.get("machineRoomId") or ["room_a"]
        room_id = room_ids[0]
        room_name = resolve_room_name(room_id)

        snapshot = build_snapshot(room_id=room_id, room_name=room_name)
        regions = self._build_region_results(definitionDraft, snapshot)

        quality_summary = QualitySummary(
            coverage=snapshot.quality.get("coverage"),
            missing=snapshot.quality.get("missing"),
            duplicates=snapshot.quality.get("duplicates"),
            freshnessSeconds=snapshot.quality.get("freshnessSeconds"),
        )

        report_name = (definitionDraft.get("metadata") or {}).get("name") or "报表"
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        logical_time = f"{snapshot.logicalDate}T00:00:00+08:00"
        window_label = f"{snapshot.logicalDate} 全天"

        status = "SUCCEEDED"
        if snapshot.anomalies:
            status = "PARTIAL"

        preview = ReportRunPreview(
            reportRunId=run_id,
            status=status,
            logicalTime=logical_time,
            windowLabel=window_label,
            qualitySummary=quality_summary,
            resultRef=f"inmemory://report_runs/{run_id}",
        )

        record = PreviewRecord(
            reportRunId=run_id,
            definitionId=definitionId,
            snapshot=snapshot,
            regions=regions,
            qualitySummary=quality_summary,
            logicalDate=snapshot.logicalDate,
            reportName=report_name,
            anomalies=snapshot.anomalies,
        )
        with self._lock:
            self._store[run_id] = record

        logger.info(
            "[preview_service] run_preview definitionId=%s runId=%s status=%s regions=%d",
            definitionId,
            run_id,
            status,
            len(regions),
        )
        return preview, record

    def get(self, run_id: str) -> PreviewRecord | None:
        return self._store.get(run_id)

    def get_result_payload(self, run_id: str) -> dict[str, Any] | None:
        record = self.get(run_id)
        if record is None:
            return None
        return {
            "reportRunId": record.reportRunId,
            "definitionId": record.definitionId,
            "reportName": record.reportName,
            "logicalDate": record.logicalDate,
            "snapshot": snapshot_to_dict(record.snapshot),
            "regions": record.regions,
            "qualitySummary": {
                "coverage": record.qualitySummary.coverage,
                "missing": record.qualitySummary.missing,
                "duplicates": record.qualitySummary.duplicates,
                "freshnessSeconds": record.qualitySummary.freshnessSeconds,
            },
            "anomalies": record.anomalies,
        }

    # ---------- 内部：按组件类型拼区域数据 ----------

    def _build_region_results(
        self,
        definitionDraft: dict[str, Any],
        snapshot: RoomEnergySnapshot,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for region in definitionDraft.get("regions") or []:
            component = region.get("component") or {}
            ctype = component.get("type")
            payload = self._payload_for(region, ctype, snapshot)
            results.append(
                {
                    "regionId": region.get("id"),
                    "name": region.get("name"),
                    "role": region.get("role"),
                    "componentType": ctype,
                    "payload": payload,
                }
            )
        return results

    def _payload_for(
        self,
        region: dict[str, Any],
        ctype: str | None,
        snap: RoomEnergySnapshot,
    ) -> dict[str, Any]:
        component = region.get("component") or {}
        if ctype == "TEXT":
            tpl = (component.get("content") or {}).get("template", "")
            return {
                "rendered": tpl.replace("{{roomName}}", snap.roomName).replace(
                    "{{logicalDate}}", snap.logicalDate
                ),
                "template": tpl,
            }

        if ctype == "METRIC_CARD":
            m = component.get("metric") or {}
            return {
                "label": m.get("label", "指标"),
                "value": snap.totalEnergyKwh,
                "unit": (m.get("format") or {}).get("unitLabel", "kWh"),
                "precision": (m.get("format") or {}).get("precision", 1),
                "yoyPercent": snap.yoyPercent,
            }

        if ctype == "TABLE":
            cols = component.get("columns") or []
            if region.get("role") == "TREND":
                trend_points, _grain = self._trend_points(region, snap)
                rows = [
                    {"timestamp": p.timestamp, "energyKwh": p.energyKwh}
                    for p in trend_points
                ]
            else:
                rows = [
                    {
                        "deviceName": d.deviceName,
                        "deviceId": d.deviceId,
                        "energyKwh": d.energyKwh,
                    }
                    for d in snap.deviceRanking
                ]
            return {
                "columns": [
                    {
                        "id": c.get("id"),
                        "label": c.get("label"),
                        "fieldRef": c.get("fieldRef"),
                        "format": c.get("format"),
                    }
                    for c in cols
                ],
                "rowNumber": component.get("rowNumber", False),
                "rows": rows,
            }

        if ctype == "CHART":
            encodings = component.get("encodings") or []
            x_field = next(
                (e.get("fieldRef") for e in encodings if e.get("channel") == "X"),
                "timestamp",
            )
            y_field = next(
                (e.get("fieldRef") for e in encodings if e.get("channel") == "Y"),
                "energyKwh",
            )
            if region.get("role") == "DETAIL":
                points = [
                    {x_field: d.deviceName, y_field: d.energyKwh}
                    for d in snap.deviceRanking
                ]
                x_labels = [d.deviceName for d in snap.deviceRanking]
                y_values = [d.energyKwh for d in snap.deviceRanking]
                grain = ""
            else:
                trend_points, grain = self._trend_points(region, snap)
                points = [
                    {x_field: p.timestamp, y_field: p.energyKwh}
                    for p in trend_points
                ]
                x_labels = [p.timestamp[11:16] for p in trend_points]
                y_values = [p.energyKwh for p in trend_points]
            return {
                "chartKind": component.get("chartKind", "LINE"),
                "grain": grain,
                "xField": x_field,
                "yField": y_field,
                "xLabels": x_labels,
                "yValues": y_values,
                "points": points,
            }

        if ctype == "DATA_QUALITY":
            return {
                "coverage": snap.quality.get("coverage"),
                "missing": snap.quality.get("missing"),
                "duplicates": snap.quality.get("duplicates", 0),
                "freshnessSeconds": snap.quality.get("freshnessSeconds"),
            }

        if ctype == "ANALYSIS":
            return {
                "goal": component.get("goal", "基于数据给出一段解读"),
                "analysis": self._build_analysis_text(snap),
                "evidenceRefs": [
                    {"name": f.get("name"), "outputRef": f.get("outputRef")}
                    for f in (component.get("facts") or [])
                ],
            }

        return {"raw": component}

    @staticmethod
    def _trend_points(
        region: dict[str, Any], snap: RoomEnergySnapshot
    ) -> tuple[list[Any], str]:
        context = region.get("context") if isinstance(region.get("context"), dict) else {}
        grain = ((context.get("grain") or {}).get("value") or {}).get("unit") or "HOUR"
        if grain == "MINUTE":
            return snap.minuteTrend, grain
        return snap.hourlyTrend, grain

    @staticmethod
    def _build_analysis_text(snap: RoomEnergySnapshot) -> str:
        top = snap.deviceRanking[0] if snap.deviceRanking else None
        trend_peak_idx = max(
            range(len(snap.hourlyTrend)),
            key=lambda i: snap.hourlyTrend[i].energyKwh,
            default=0,
        )
        peak_hour = snap.hourlyTrend[trend_peak_idx].timestamp[11:16] if snap.hourlyTrend else "—"
        yoy_dir = "上升" if snap.yoyPercent >= 0 else "下降"
        parts = [
            f"{snap.roomName} 在 {snap.logicalDate} 的总耗电为 {snap.totalEnergyKwh} kWh，"
            f"相较昨日{yoy_dir} {abs(snap.yoyPercent)}%。",
        ]
        if top:
            parts.append(
                f"用电占比最高的是「{top.deviceName}」，当日耗电 {top.energyKwh} kWh。"
            )
        parts.append(f"当日耗电峰值出现在 {peak_hour}，需关注空调和配电负载。")
        if snap.anomalies:
            parts.append(
                f"注意：当日记录到 {len(snap.anomalies)} 处数据质量异常，相关时段数值可能偏低。"
            )
        return " ".join(parts)


preview_service = PreviewService()
