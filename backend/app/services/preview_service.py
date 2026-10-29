"""预览服务（节点 9 PREVIEWING）。

真实版由 Java 预览引擎跑真实 SQL/ClickHouse 查询。本模块：
    - 读取 saved definition；
    - 为每个 region 根据组件类型从 mock_data_service 拿对应数据；
    - 产出 ReportRunPreview + resultRef（指向内存里的真实数据）。

**预览必须服从定义**：草稿里写了过滤 / TopN / 派生列 / 条件样式 / 时间窗，
预览就按它出数。否则用户看到的和草稿承诺的不是一回事。

数据协议说明：
    - 每个 region 的实际渲染数据存在 _preview_results 字典里，key = reportRunId；
    - 前端拿到 reportRunId 后，调 GET /api/report/preview/{reportRunId} 读详细数据渲染。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

from app.schemas.agent import QualitySummary, ReportRunPreview
from app.services.edit_time_resolver import DEFAULT_TIMEZONE
from app.services.mock_data_service import (
    HourlyPoint,
    RoomEnergySnapshot,
    build_snapshot,
    definition_timezone,
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
    # 报表口径下的总量与环比（可能是过滤后/多日窗口的，和原始房间快照不同）
    scopeTotalKwh: float | None = None
    scopeYoyPercent: float | None = None


def _region_window(region: dict[str, Any]) -> dict[str, Any]:
    """取区域声明的相对时间窗（没有就按昨日）。"""
    context = region.get("context") if isinstance(region.get("context"), dict) else {}
    window = context.get("timeWindow") or {}
    value = window.get("value") if isinstance(window.get("value"), dict) else window
    if value.get("type") == "CALENDAR":
        return {
            "unit": str(value.get("unit", "DAY")).upper(),
            "count": int(value.get("count", 1) or 1),
            "offset": int(value.get("offset", -1) if value.get("offset") is not None else -1),
        }
    return {"unit": "DAY", "count": 1, "offset": -1}


def _window_dates(snap: RoomEnergySnapshot, region: dict[str, Any]) -> list[str]:
    """窗口覆盖的自然日列表。

    快照的 logicalDate 代表「昨天」（offset = -1）。窗口从
    ``D + offset`` 开始、覆盖 ``count`` 天，D = logicalDate + 1 天。
    """
    window = _region_window(region)
    if window["unit"] != "DAY":
        return [snap.logicalDate]
    base = date.fromisoformat(snap.logicalDate)
    start = base + timedelta(days=1 + window["offset"])
    return [
        (start + timedelta(days=index)).isoformat()
        for index in range(max(1, window["count"]))
    ]


def _previous_period_dates(
    snap: RoomEnergySnapshot, region: dict[str, Any]
) -> list[str]:
    """上一周期的自然日列表（用于「前一日耗电」）。"""
    window = _region_window(region)
    current = _window_dates(snap, region)
    if window["unit"] != "DAY" or len(current) <= 1:
        base = date.fromisoformat(current[-1])
        return [(base - timedelta(days=1)).isoformat()]
    span = len(current)
    base = date.fromisoformat(current[0])
    return [
        (base - timedelta(days=span) + timedelta(days=index)).isoformat()
        for index in range(span)
    ]


def _region_filters(region: dict[str, Any]) -> list[dict[str, Any]]:
    """取区域 pipeline 里的过滤条件。"""
    nodes = (((region.get("data") or {}).get("pipeline") or {}).get("nodes") or [])
    for node in nodes:
        if node.get("type") == "FILTER":
            return list(node.get("conditions") or [])
    return []


def _match_condition(row: dict[str, Any], condition: dict[str, Any]) -> bool:
    field_ref = condition.get("fieldRef")
    if not field_ref:
        return True
    values = condition.get("values") or []
    op = str(condition.get("op") or "IN").upper().replace(" ", "_").replace("-", "_")
    actual = row.get(field_ref)
    if op in ("IN",):
        return str(actual) in {str(v) for v in values}
    if op in ("NOT_IN",):
        return str(actual) not in {str(v) for v in values}
    if op in ("EQ",):
        return str(actual) == str(values[0] if values else "")
    if op in ("NE",):
        return str(actual) != str(values[0] if values else "")
    try:
        left = float(actual)
        right = float(values[0] if values else 0)
    except (TypeError, ValueError):
        return True
    if op == "GT":
        return left > right
    if op == "GTE":
        return left >= right
    if op == "LT":
        return left < right
    if op == "LTE":
        return left <= right
    return True


def _apply_filters(
    rows: list[dict[str, Any]], conditions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """按过滤条件筛行。"""
    if not conditions:
        return rows
    return [
        row
        for row in rows
        if all(_match_condition(row, condition) for condition in conditions)
    ]


def _device_rows(snap: RoomEnergySnapshot) -> list[dict[str, Any]]:
    return [
        {
            "deviceId": d.deviceId,
            "deviceName": d.deviceName,
            "deviceType": d.deviceType,
            "energyKwh": d.energyKwh,
        }
        for d in snap.deviceRanking
    ]


def _daily_total(
    room_id: str,
    room_name: str,
    logical_date: str,
    timezone: str = DEFAULT_TIMEZONE,
) -> float:
    return build_snapshot(
        room_id=room_id,
        room_name=room_name,
        logical_date=logical_date,
        timezone=timezone,
    ).totalEnergyKwh


def _scope_total(
    region: dict[str, Any],
    snap: RoomEnergySnapshot,
    *,
    room_id: str,
    room_name: str,
    timezone: str = DEFAULT_TIMEZONE,
) -> float:
    """报表口径的总量：当前时间窗 + 当前过滤条件下的总耗电。

    指标卡、页面摘要、环比分母、分析段全部走这一个函数，
    保证同一份报表里不会出现两种总量口径。
    """
    dates = _window_dates(snap, region)
    conditions = _region_filters(region)
    device_metric = any(
        node.get("metricRef") == "metric.machine_room.device_energy"
        for node in ((region.get("data") or {}).get("pipeline") or {}).get("nodes", [])
    )
    if conditions or device_metric:
        # 有过滤时，总耗电只算被选中的设备，口径和表格保持一致。
        total = 0.0
        for logical_date in dates:
            day_snapshot = build_snapshot(
                room_id=room_id,
                room_name=room_name,
                logical_date=logical_date,
                timezone=timezone,
            )
            total += sum(
                row.get("energyKwh", 0)
                for row in _apply_filters(_device_rows(day_snapshot), conditions)
            )
        return round(total, 1)
    if len(dates) > 1:
        return round(
            sum(_daily_total(room_id, room_name, d, timezone) for d in dates), 1
        )
    return snap.totalEnergyKwh


def _previous_scope_total(
    region: dict[str, Any],
    snap: RoomEnergySnapshot,
    *,
    room_id: str,
    room_name: str,
    timezone: str = DEFAULT_TIMEZONE,
) -> float | None:
    """上一周期、**同一过滤条件**下的总耗电（环比的分母）。

    没有上一周期数据时返回 None——调用方要如实说明「无法比较」，
    不能拿全机房数据兜底。
    """
    dates = _previous_period_dates(snap, region)
    if not dates:
        return None
    conditions = _region_filters(region)
    total = 0.0
    for logical_date in dates:
        day_snapshot = build_snapshot(
            room_id=room_id,
            room_name=room_name,
            logical_date=logical_date,
            timezone=timezone,
        )
        total += sum(
            row.get("energyKwh", 0)
            for row in _apply_filters(_device_rows(day_snapshot), conditions)
        )
    return round(total, 1)


def _scope_comparison(
    region: dict[str, Any],
    snap: RoomEnergySnapshot,
    *,
    room_id: str,
    room_name: str,
    timezone: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    """同口径环比：当前窗口总量 vs 上一周期同过滤条件总量。

    ``percent`` 为 None 表示无法比较（上一周期无数据或分母为 0），
    调用方必须显式说明，不得退回全机房口径。
    """
    current = _scope_total(
        region, snap, room_id=room_id, room_name=room_name, timezone=timezone
    )
    previous = _previous_scope_total(
        region, snap, room_id=room_id, room_name=room_name, timezone=timezone
    )
    days = len(_window_dates(snap, region))
    percent: float | None = None
    if previous is not None and previous > 0:
        percent = round((current - previous) / previous * 100, 1)
    return {
        "current": current,
        "previous": previous,
        "percent": percent,
        "days": days,
        "available": percent is not None,
    }


def _region_id_from_ref(output_ref: str | None) -> str | None:
    """从 ``region:<regionId>/output:main`` 里取区域 id。"""
    text = str(output_ref or "")
    if not text.startswith("region:"):
        return None
    return text[len("region:") :].split("/", 1)[0] or None


def _scope_region(regions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """挑一个代表报表口径（过滤 + 时间窗）的区域。

    优先用指标卡：它同时承载过滤条件和时间窗，是报表口径的权威来源。
    """
    for wanted in ("METRIC_CARD", "TABLE", "CHART"):
        for region in regions:
            if (region.get("component") or {}).get("type") == wanted:
                return region
    for region in regions:
        if _region_filters(region):
            return region
    return None


def _window_scope_label(dates: list[str]) -> str:
    """分析段用的窗口标签：单日就是那一天，多日给区间。"""
    if not dates:
        return "—"
    if len(dates) == 1:
        return dates[0]
    return f"{dates[0]} ~ {dates[-1]}（{len(dates)} 天）"


def _trend_peak_label(payload: dict[str, Any] | None) -> str | None:
    """从趋势区**已经算好**的数据里取峰值所在的横轴标签。"""
    if not payload:
        return None
    labels = payload.get("xLabels")
    values = payload.get("yValues")
    if labels and values and len(labels) == len(values):
        best = max(range(len(values)), key=lambda i: values[i])
        return str(labels[best])
    rows = payload.get("rows") or []
    if rows:
        best = max(rows, key=lambda r: r.get("energyKwh") or 0)
        timestamp = str(best.get("timestamp") or "")
        return timestamp[11:16] if len(timestamp) >= 16 else timestamp
    return None


def _device_rows_for_window(
    region: dict[str, Any],
    snap: RoomEnergySnapshot,
    *,
    room_id: str,
    room_name: str,
    timezone: str = DEFAULT_TIMEZONE,
) -> list[dict[str, Any]]:
    """窗口覆盖多天时，按设备把各天的耗电累加；单日就是当天明细。

    否则多日窗口的排名表会停留在单日数值，和卡片总量、趋势图不同窗口。
    """
    dates = _window_dates(snap, region)
    if len(dates) <= 1:
        return _device_rows(snap)
    totals: dict[str, dict[str, Any]] = {}
    for logical_date in dates:
        day_snapshot = build_snapshot(
            room_id=room_id,
            room_name=room_name,
            logical_date=logical_date,
            timezone=timezone,
        )
        for row in _device_rows(day_snapshot):
            acc = totals.setdefault(row["deviceId"], {**row, "energyKwh": 0.0})
            acc["energyKwh"] = acc["energyKwh"] + row.get("energyKwh", 0)
    return [
        {**row, "energyKwh": round(row["energyKwh"], 1)} for row in totals.values()
    ]


def _previous_device_rows(
    room_id: str,
    room_name: str,
    dates: list[str],
    timezone: str = DEFAULT_TIMEZONE,
) -> dict[str, float]:
    """上一周期每台设备的耗电（按 deviceId 索引）。"""
    totals: dict[str, float] = {}
    for logical_date in dates:
        snapshot = build_snapshot(
            room_id=room_id,
            room_name=room_name,
            logical_date=logical_date,
            timezone=timezone,
        )
        for device in snapshot.deviceRanking:
            totals[device.deviceId] = totals.get(device.deviceId, 0.0) + device.energyKwh
    return totals


def _derive_columns(
    rows: list[dict[str, Any]],
    region: dict[str, Any],
    snap: RoomEnergySnapshot,
    *,
    room_id: str,
    room_name: str,
    total_energy: float,
    timezone: str = DEFAULT_TIMEZONE,
) -> None:
    """就地补齐派生列的值。"""
    derived = (region.get("binding") or {}).get("derivedColumns") or []
    if not derived:
        return
    previous = _previous_device_rows(
        room_id,
        room_name,
        _previous_period_dates(snap, region),
        timezone,
    )
    for row in rows:
        prev_value = previous.get(row.get("deviceId"))
        for column in derived:
            kind = str(column.get("kind") or "").upper()
            field_ref = column.get("fieldRef")
            if not field_ref:
                continue
            if kind == "PREVIOUS_PERIOD":
                row[field_ref] = round(prev_value, 1) if prev_value is not None else None
            elif kind == "GROWTH_RATE":
                if prev_value:
                    row[field_ref] = round(
                        (row.get("energyKwh", 0) - prev_value) / prev_value * 100, 1
                    )
                else:
                    row[field_ref] = None
            elif kind == "SHARE_OF_TOTAL":
                if total_energy:
                    row[field_ref] = round(
                        row.get("energyKwh", 0) / total_energy * 100, 1
                    )
                else:
                    row[field_ref] = None


def _style_rows(rows: list[dict[str, Any]], hints: list[dict[str, Any]]) -> None:
    """就地补上行级条件样式标记。

    只处理"高亮"类规则（``HIGHLIGHT_*``）；``SORT`` 是排序指令，
    不是行样式，不能当成高亮条件。
    """
    if not hints:
        return
    highlight_hints = [
        hint
        for hint in hints
        if str(hint.get("rule") or "HIGHLIGHT_ABOVE_THRESHOLD").upper().startswith(
            "HIGHLIGHT"
        )
    ]
    if not highlight_hints:
        return
    for row in rows:
        matched: list[dict[str, Any]] = []
        for hint in highlight_hints:
            field_ref = hint.get("fieldRef")
            if not field_ref:
                continue
            value = row.get(field_ref)
            if value is None:
                continue
            threshold = hint.get("threshold")
            comparison = str(hint.get("comparison") or "GT").upper()
            try:
                left = float(value)
                right = float(threshold if threshold is not None else 0)
            except (TypeError, ValueError):
                continue
            hit = (
                left > right
                if comparison == "GT"
                else left >= right
                if comparison == "GTE"
                else left < right
                if comparison == "LT"
                else left <= right
                if comparison == "LTE"
                else left == right
                if comparison == "EQ"
                else left != right
            )
            if hit:
                matched.append(
                    {
                        "style": hint.get("style", "RED"),
                        "fieldRef": field_ref,
                        "value": left,
                        "threshold": right,
                        "comparison": comparison,
                    }
                )
        if matched:
            row["styleHints"] = matched
            row["rowStyle"] = {"class": matched[0]["style"].lower()}


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

        # 预览层按**定义里声明的时区**取数：数据侧的时间戳必须和
        # defaults.timeWindow.timezone 是同一个时区（A1-P3-02）。
        timezone = definition_timezone(definitionDraft)
        snapshot = build_snapshot(
            room_id=room_id, room_name=room_name, timezone=timezone
        )
        regions = self._build_region_results(
            definitionDraft,
            snapshot,
            room_id=room_id,
            room_name=room_name,
            timezone=timezone,
        )

        quality_summary = QualitySummary(
            coverage=snapshot.quality.get("coverage"),
            missing=snapshot.quality.get("missing"),
            duplicates=snapshot.quality.get("duplicates"),
            freshnessSeconds=snapshot.quality.get("freshnessSeconds"),
        )

        report_name = (definitionDraft.get("metadata") or {}).get("name") or "报表"
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        logical_time = f"{snapshot.logicalDate}T00:00:00+08:00"

        # 报表口径（过滤条件 + 时间窗）由指标卡 / 排名表 / 趋势图共同承载，
        # 页面摘要必须和它们一致：带设备过滤的报表不能显示全机房总量。
        scope_region = _scope_region(definitionDraft.get("regions") or [])
        scope_total: float | None = None
        scope_yoy: float | None = None
        window_label = f"{snapshot.logicalDate} 全天"
        if scope_region is not None:
            comparison = _scope_comparison(
                scope_region,
                snapshot,
                room_id=room_id,
                room_name=room_name,
                timezone=timezone,
            )
            scope_total = comparison["current"]
            scope_yoy = comparison["percent"]
            window_label = self._window_label(_window_dates(snapshot, scope_region))

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
            scopeTotalKwh=scope_total,
            scopeYoyPercent=scope_yoy,
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
        snapshot = snapshot_to_dict(record.snapshot)
        # 摘要里的总量/环比要和报表口径一致：
        # 带设备过滤或多日窗口的报表不能显示全机房单日总量。
        if record.scopeTotalKwh is not None:
            snapshot["totalEnergyKwh"] = record.scopeTotalKwh
        if record.scopeYoyPercent is not None:
            snapshot["yoyPercent"] = record.scopeYoyPercent
        return {
            "reportRunId": record.reportRunId,
            "definitionId": record.definitionId,
            "reportName": record.reportName,
            "logicalDate": record.logicalDate,
            "snapshot": snapshot,
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
        *,
        room_id: str = "room_a",
        room_name: str = "A 机房",
        timezone: str = DEFAULT_TIMEZONE,
    ) -> list[dict[str, Any]]:
        regions = definitionDraft.get("regions") or []
        region_map = {r.get("id"): r for r in regions if r.get("id")}
        payload_map: dict[str, dict[str, Any]] = {}
        prepared: list[tuple[dict[str, Any], dict[str, Any] | None]] = []

        # 第一遍：先算非分析区，分析段才有「同报表已算好的结果」可引用。
        for region in regions:
            ctype = (region.get("component") or {}).get("type")
            if ctype == "ANALYSIS":
                prepared.append((region, None))
                continue
            payload = self._payload_for(
                region,
                ctype,
                snapshot,
                room_id=room_id,
                room_name=room_name,
                timezone=timezone,
            )
            payload_map[region.get("id")] = payload
            prepared.append((region, payload))

        results: list[dict[str, Any]] = []
        for region, payload in prepared:
            ctype = (region.get("component") or {}).get("type")
            if payload is None:
                payload = self._analysis_payload(
                    region,
                    snapshot,
                    region_map=region_map,
                    payload_map=payload_map,
                    room_id=room_id,
                    room_name=room_name,
                    timezone=timezone,
                )
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

    def _analysis_payload(
        self,
        region: dict[str, Any],
        snap: RoomEnergySnapshot,
        *,
        region_map: dict[str, dict[str, Any]],
        payload_map: dict[str, dict[str, Any]],
        room_id: str,
        room_name: str,
        timezone: str = DEFAULT_TIMEZONE,
    ) -> dict[str, Any]:
        """分析段：事实只引用**同一份报表里已经算好**的结果。

        绝不直接读未过滤的房间快照——否则带设备过滤的报表会在分析段里
        说出全机房的总量，还会点名用户明确排除的设备。
        取不到事实时如实说明「无法给出」，不回退到全机房数据。
        """
        component = region.get("component") or {}
        facts = component.get("facts") or []

        def _fact(name: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
            for fact in facts:
                if str(fact.get("name")) != name:
                    continue
                region_id = _region_id_from_ref(fact.get("outputRef"))
                if not region_id:
                    return None, None
                return region_map.get(region_id), payload_map.get(region_id)
            return None, None

        total_region, total_payload = _fact("total_energy")
        ranking_region, ranking_payload = _fact("device_ranking")
        trend_region, trend_payload = _fact("energy_trend")

        # 口径基准：优先指标卡（它同时承载过滤条件与时间窗）
        scope_region = total_region or ranking_region or trend_region
        dates = (
            _window_dates(snap, scope_region) if scope_region else [snap.logicalDate]
        )
        days = len(dates)
        period = "当日" if days == 1 else "该周期"
        if scope_region is not None:
            comparison = _scope_comparison(
                scope_region,
                snap,
                room_id=room_id,
                room_name=room_name,
                timezone=timezone,
            )
        else:
            comparison = {
                "current": None,
                "previous": None,
                "percent": None,
                "days": days,
                "available": False,
            }

        parts: list[str] = []

        # 1) 总量：取指标卡已经算好的值（同一窗口 + 同一过滤条件）
        total_value = (total_payload or {}).get("value")
        if total_region is not None and total_value is not None:
            sentence = (
                f"{room_name} 在 {_window_scope_label(dates)} 的总耗电为 {total_value} kWh"
            )
            if comparison["available"]:
                direction = "上升" if comparison["percent"] >= 0 else "下降"
                base = "前一日" if days == 1 else f"前 {days} 天"
                sentence += f"，较{base}{direction} {abs(comparison['percent'])}%。"
            else:
                sentence += "。前一期同口径数据缺失，未计算变化幅度。"
            parts.append(sentence)
        elif total_region is not None:
            parts.append("本次报表的总耗电指标没有算出数值，无法给出总耗电量。")
        else:
            parts.append("本次报表未包含总耗电指标，无法给出总耗电量。")

        # 2) 用电量最高的设备：取排名表已经算好的行
        rows = (ranking_payload or {}).get("rows") or []
        if ranking_region is not None and rows:
            top = rows[0]
            parts.append(
                f"用电量最高的是「{top.get('deviceName')}」，"
                f"{period}耗电 {top.get('energyKwh')} kWh。"
            )
        elif ranking_region is not None:
            parts.append("设备排名结果为空，无法给出用电量最高的设备。")
        else:
            parts.append("本次报表未包含设备排名，无法给出用电量最高的设备。")

        # 3) 峰值时间：取趋势区已经算好的点
        peak = _trend_peak_label(trend_payload)
        if trend_region is not None and peak:
            parts.append(f"{period}耗电峰值出现在 {peak}，需关注高负载设备。")
        elif trend_region is not None:
            parts.append("趋势数据为空，无法给出耗电峰值时间。")
        else:
            parts.append("本次报表未包含趋势数据，无法给出耗电峰值时间。")

        # 4) 数据质量：数据源本身的质量情况，不随设备过滤变化
        if snap.anomalies:
            parts.append(
                f"注意：{period}记录到 {len(snap.anomalies)} 处数据质量异常，"
                "相关时段数值可能偏低。"
            )

        return {
            "goal": component.get("goal", "基于数据给出一段解读"),
            "analysis": " ".join(parts),
            "evidenceRefs": [
                {"name": f.get("name"), "outputRef": f.get("outputRef")}
                for f in facts
            ],
        }

    def _payload_for(
        self,
        region: dict[str, Any],
        ctype: str | None,
        snap: RoomEnergySnapshot,
        *,
        room_id: str = "room_a",
        room_name: str = "A 机房",
        timezone: str = DEFAULT_TIMEZONE,
    ) -> dict[str, Any]:
        component = region.get("component") or {}
        # 没有可执行绑定 = 占位区域（#39）：只回占位原因，**不编数值**。
        # 占位板块保留方案声明的组件类型（空壳卡片），预览若照常算数就会把
        # 总耗电贴在「PUE 总览」这种标签下面 —— 那就是编造。
        bound = (((region.get("data") or {}).get("pipeline") or {}).get("nodes") or [])
        if ctype in {"METRIC_CARD", "TABLE", "CHART"} and not bound:
            reason = ((region.get("templateBinding") or {}).get("placeholderReason")
                      or "数据待连接：当前已接通的能力里没有对应指标。")
            return {"placeholder": True, "reason": reason,
                    "chartKind": component.get("chartKind", "LINE"),
                    "columns": component.get("columns") or [], "rows": []}
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
            dates = _window_dates(snap, region)
            # 总量与环比都按「本区域的时间窗 + 过滤条件」算，
            # 和排名表、趋势图、分析段是同一口径。
            value = _scope_total(
                region,
                snap,
                room_id=room_id,
                room_name=room_name,
                timezone=timezone,
            )
            comparison = _scope_comparison(
                region,
                snap,
                room_id=room_id,
                room_name=room_name,
                timezone=timezone,
            )
            return {
                "label": m.get("label", "指标"),
                "value": value,
                "unit": (m.get("format") or {}).get("unitLabel", "kWh"),
                "precision": (m.get("format") or {}).get("precision", 1),
                "yoyPercent": comparison["percent"],
                "comparisonAvailable": comparison["available"],
                "windowLabel": self._window_label(dates),
            }

        if ctype == "TABLE":
            cols = component.get("columns") or []
            if region.get("role") == "TREND":
                trend_points, _grain = self._trend_points(
                    region,
                    snap,
                    room_id=room_id,
                    room_name=room_name,
                    timezone=timezone,
                )
                rows = [
                    {"timestamp": p.timestamp, "energyKwh": p.energyKwh}
                    for p in trend_points
                ]
            else:
                rows = self._detail_rows(
                    region,
                    snap,
                    room_id=room_id,
                    room_name=room_name,
                    timezone=timezone,
                )
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
                rows = self._detail_rows(
                    region,
                    snap,
                    room_id=room_id,
                    room_name=room_name,
                    timezone=timezone,
                )
                points = [
                    {x_field: r.get("deviceName"), y_field: r.get("energyKwh")}
                    for r in rows
                ]
                x_labels = [r.get("deviceName") for r in rows]
                y_values = [r.get("energyKwh") for r in rows]
                grain = ""
            else:
                trend_points, grain = self._trend_points(
                    region,
                    snap,
                    room_id=room_id,
                    room_name=room_name,
                    timezone=timezone,
                )
                points = [
                    {x_field: p.timestamp, y_field: p.energyKwh}
                    for p in trend_points
                ]
                x_labels = [self._axis_label(p.timestamp, grain) for p in trend_points]
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
            # 分析段要引用**同报表里已经算好**的结果，
            # 需要拿到兄弟区域的 payload，由 _analysis_payload 处理。
            return {"raw": component}

        return {"raw": component}

    @staticmethod
    def _window_label(dates: list[str]) -> str:
        if len(dates) <= 1:
            return f"{dates[0]} 全天" if dates else ""
        return f"{dates[0]} ~ {dates[-1]}（{len(dates)} 天）"

    @staticmethod
    def _axis_label(timestamp: str, grain: str) -> str:
        if grain == "DAY":
            return timestamp[5:10] if len(timestamp) >= 10 else timestamp
        return timestamp[11:16] if len(timestamp) >= 16 else timestamp

    def _detail_rows(
        self,
        region: dict[str, Any],
        snap: RoomEnergySnapshot,
        *,
        room_id: str,
        room_name: str,
        timezone: str = DEFAULT_TIMEZONE,
    ) -> list[dict[str, Any]]:
        """设备明细行：过滤 → 排序 → TopN → 派生列 → 条件样式。"""
        rows = _apply_filters(
            _device_rows_for_window(
                region,
                snap,
                room_id=room_id,
                room_name=room_name,
                timezone=timezone,
            ),
            _region_filters(region),
        )
        rows.sort(key=lambda r: r.get("energyKwh", 0), reverse=True)

        top_n = (region.get("binding") or {}).get("topN")
        if isinstance(top_n, int) and top_n > 0:
            rows = rows[:top_n]

        # 占比分母与指标卡、摘要、分析段同口径（同窗口 + 同过滤条件）
        total_energy = _scope_total(
            region, snap, room_id=room_id, room_name=room_name, timezone=timezone
        )

        _derive_columns(
            rows,
            region,
            snap,
            room_id=room_id,
            room_name=room_name,
            total_energy=total_energy,
            timezone=timezone,
        )
        _style_rows(rows, (region.get("component") or {}).get("styleHints") or [])
        return rows

    def _trend_points(
        self,
        region: dict[str, Any],
        snap: RoomEnergySnapshot,
        *,
        room_id: str = "room_a",
        room_name: str = "A 机房",
        timezone: str = DEFAULT_TIMEZONE,
    ) -> tuple[list[Any], str]:
        context = region.get("context") if isinstance(region.get("context"), dict) else {}
        grain = ((context.get("grain") or {}).get("value") or {}).get("unit") or "HOUR"
        if grain == "MINUTE":
            points: list[Any] = list(snap.minuteTrend)
        elif grain == "DAY":
            tz = ZoneInfo(timezone)
            points = [
                HourlyPoint(
                    timestamp=datetime.combine(
                        date.fromisoformat(d), time(0, 0), tzinfo=tz
                    ).isoformat(),
                    energyKwh=_daily_total(room_id, room_name, d, timezone),
                )
                for d in _window_dates(snap, region)
            ]
        else:
            points = list(snap.hourlyTrend)
        return self._scale_trend_to_scope(
            points,
            region,
            snap,
            room_id=room_id,
            room_name=room_name,
            timezone=timezone,
        ), grain

    def _scale_trend_to_scope(
        self,
        points: list[Any],
        region: dict[str, Any],
        snap: RoomEnergySnapshot,
        *,
        room_id: str,
        room_name: str,
        timezone: str = DEFAULT_TIMEZONE,
    ) -> list[Any]:
        """趋势曲线也要服从本区域的过滤条件。

        Mock 的小时/分钟曲线是按机房总量铺形状的，没有设备级明细；
        这里保留曲线形状，按「过滤后总量 / 机房总量」整体缩放，
        让趋势与指标卡、排名表落在同一口径上。
        """
        if not points or not _region_filters(region):
            return points
        scope_total = _scope_total(
            region, snap, room_id=room_id, room_name=room_name, timezone=timezone
        )
        room_total = sum(point.energyKwh for point in points)
        if room_total <= 0:
            return points
        ratio = scope_total / room_total
        if abs(ratio - 1.0) < 1e-9:
            return points
        return [
            HourlyPoint(
                timestamp=point.timestamp,
                energyKwh=round(point.energyKwh * ratio, 3),
            )
            for point in points
        ]


preview_service = PreviewService()
