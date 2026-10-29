"""从**已确认的文字方案**生成模板设计（规格 #18 / 工单 02）。

用户口径（需求访谈 Q13）：

    「先别加了吧，数据库里有啥数据就展示啥，如果没有的就按 Q11 和 Q12 的方案来。」

所以这一步**不是**「一律出占位」，而是：

    1. 把方案的每个板块变成一个可视区域；
    2. 逐区域核对**当前已接通的能力**（目录 + 指标 + 授权范围）；
    3. 接得到的区域填数据，并标明数据来源性质；
    4. 接不到的区域保留占位，写明原因（数据待连接 / 口径待核实）。

同时守住两条边界：

    - **设计自由度不被缺数据拦住**（Q2：「先自由发挥，不要先确定数据库里
      有没有数据被拦截了」）—— 没有数据也要出模板，只是那一块是占位；
    - **绝不编造数值**（Q11）—— 接不到就是占位，不是假数。

数据来源说明：当前这条链路上唯一可取数的是能耗 Mock 源
（``mock_data_service``），不是生产能耗事实表。所以每个接上数据的区域
都会带上 ``dataSourceLabel``，界面必须把它显示出来 ——
把演示数值当成生产数据，是比缺数据更严重的问题。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping

from app.schemas.template_design import (
    RegionDataStatus,
    TemplateDesign,
    TemplateRegion,
)
from app.services.catalog_mock_service import get_catalog
from app.data.machine_room_energy import (
    METRIC_DEVICE_ENERGY,
    METRIC_ENERGY_COST,
    METRIC_PUE,
    METRIC_TOTAL_ENERGY,
)
from app.services.metric_mock_service import get_metric, match_metric_by_alias
from app.services.mock_data_service import build_snapshot, resolve_room_name
from app.services.scope_service import authorized_subject_ids, is_subject_authorized


logger = logging.getLogger(__name__)

#: 模板 id 前缀。与方案 id（rplan_）、旧草稿的待确认计划（plan_）都不同。
TEMPLATE_ID_PREFIX = "rtpl_"

#: 默认数据源：机房能耗目录。这是当前唯一有数据的源。
DEFAULT_SOURCE_REF = "catalog.machine_room.energy"

#: 数据来源标识与展示文案。界面必须把文案显示出来。
DATA_SOURCE = "mock_energy"
DATA_SOURCE_LABEL = "模拟数据（能耗 Mock 源，非生产事实表）"

#: 指标口径可核对的底层字段。指标登记了但字段不在数据源里，
#: 就说明当前能力算不出这个数 —— 只能占位，不能编。
_SOURCE = get_catalog(DEFAULT_SOURCE_REF) or {}
_SOURCE_FIELDS = _SOURCE.get("fields") or {}

#: 时间粒度没对齐时的最大展示行数。
_MAX_TABLE_ROWS = 10

#: 指标意图的常见措辞 → 指标口径。
#:
#: 刻意**只用于模板生成**：指标目录里的别名是旧流程的口径，
#: 不该被这次改动带着变。这里补的是「方案里模型会怎么写」这一层。
_INTENT_PATTERNS: tuple[tuple[str, str], ...] = (
    # 设备级
    ("设备耗电量", METRIC_DEVICE_ENERGY["metricRef"]),
    ("设备耗电", METRIC_DEVICE_ENERGY["metricRef"]),
    ("设备用电", METRIC_DEVICE_ENERGY["metricRef"]),
    ("设备能耗", METRIC_DEVICE_ENERGY["metricRef"]),
    ("单机耗电", METRIC_DEVICE_ENERGY["metricRef"]),
    ("分设备耗电", METRIC_DEVICE_ENERGY["metricRef"]),
    # 总量级
    ("总耗电量", METRIC_TOTAL_ENERGY["metricRef"]),
    ("总耗电", METRIC_TOTAL_ENERGY["metricRef"]),
    ("总用电", METRIC_TOTAL_ENERGY["metricRef"]),
    ("总能耗", METRIC_TOTAL_ENERGY["metricRef"]),
    ("总电量", METRIC_TOTAL_ENERGY["metricRef"]),
    ("耗电总量", METRIC_TOTAL_ENERGY["metricRef"]),
    ("电量汇总", METRIC_TOTAL_ENERGY["metricRef"]),
    ("整体能耗", METRIC_TOTAL_ENERGY["metricRef"]),
    ("机房能耗", METRIC_TOTAL_ENERGY["metricRef"]),
    ("每日耗电", METRIC_TOTAL_ENERGY["metricRef"]),
    ("耗电量", METRIC_TOTAL_ENERGY["metricRef"]),
    ("用电量", METRIC_TOTAL_ENERGY["metricRef"]),
    # 裸「能耗」：目录别名匹配不到时兜到总量级。
    #
    # 这一条**不会**把 PUE 顶替成能耗：目录别名先匹配，
    # 「PUE」「能耗成本」都先被目录别名接走，走不到这里。
    ("能耗", METRIC_TOTAL_ENERGY["metricRef"]),
    # 成本与 PUE
    ("能耗成本", METRIC_ENERGY_COST["metricRef"]),
    ("电费", METRIC_ENERGY_COST["metricRef"]),
    ("电能使用效率", METRIC_PUE["metricRef"]),
)


@dataclass(frozen=True)
class _Window:
    """报表时间窗。"""

    unit: str
    count: int
    offset: int
    timezone: str

    @property
    def label(self) -> str:
        return f"{self.count} {self.unit}"


def _default_window() -> _Window:
    """默认时间窗：昨日一整个自然日（与旧流程默认一致）。"""
    from app.services.edit_time_resolver import DEFAULT_TIMEZONE

    return _Window(unit="DAY", count=1, offset=-1, timezone=DEFAULT_TIMEZONE)


def _resolve_window(time_range: Any) -> _Window:
    """把方案里的时间范围解析成时间窗。

    优先复用草稿组装层的时间窗解析 —— 同一句「最近 7 天」在两个流程里
    必须落在同一个窗口上，否则用户看到的报表和设计对不上。
    """
    from app.services.report_draft_service import resolve_time_window

    try:
        resolved = resolve_time_window(time_range)
    except Exception:  # noqa: BLE001
        logger.exception("[report_template] 时间窗解析失败")
        resolved = None
    if not isinstance(resolved, dict):
        return _default_window()
    unit = str(resolved.get("unit") or "DAY").upper()
    if unit not in {"DAY", "WEEK", "MONTH"}:
        unit = "DAY"
    count = int(resolved.get("count") or 1)
    offset = resolved.get("offset")
    if offset is None:
        offset = -count
    timezone = str(resolved.get("timezone") or _default_window().timezone)
    return _Window(unit=unit, count=max(1, count), offset=int(offset), timezone=timezone)


def _period_dates(logical_date: str, count: int, offset: int) -> list[str]:
    """时间窗覆盖的自然日列表。

    口径与预览层一致：``logical_date`` 代表「昨天」，窗口从
    ``D + offset`` 开始、覆盖 ``count`` 天。
    """
    base = date.fromisoformat(logical_date)
    start = base + timedelta(days=1 + offset)
    return [(start + timedelta(days=index)).isoformat() for index in range(count)]


def _resolve_room(subject_text: Any, subject_id: Any = None) -> tuple[str | None, str | None]:
    """按方案里的统计对象确定机房。

    返回 ``(room_id, 原因)``。原因非空表示**接不到数** ——
    但那不是拒绝生成模板的理由：区域会转成占位，模板照常出。
    """
    candidates = authorized_subject_ids("machine_room")
    if not candidates:
        return None, "当前账号没有可统计的机房授权范围。"

    # 请求上下文里的对象优先（它是服务端权威值）。
    if subject_id:
        normalized = str(subject_id).strip()
        if is_subject_authorized("machine_room", normalized):
            return normalized, None

    text = str(subject_text or "").strip()
    if not text:
        return candidates[0], None

    for room_id in candidates:
        name = resolve_room_name(room_id)
        if text == room_id or text == name or (text and text in name):
            return room_id, None
    return None, f"当前账号没有「{text}」的授权范围。"


def _match_metric(intents: list[str]) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """按区域里的指标意图匹配指标口径。

    两层匹配：

        1. 指标目录自己的别名（``match_metric_by_alias``）；
        2. 目录别名之外的**常见措辞**（``_INTENT_PATTERNS``）。

    第二层是必要的：方案里的指标意图是模型写的自由措辞（「总能耗」、
    「设备耗电量」），而目录里的别名是给旧流程用的词表（「总耗电」、
    「设备耗电」）。用户说的是同一件事，不能因为用词不同就把一个
    **算得出数的**指标判成「没有对应指标」。

    匹配不到口径不算错误 —— 那个区域会变成占位，并说明
    「当前能力里没有这个指标」。
    """
    for intent in intents:
        ref = match_metric_by_alias(intent)
        if not ref:
            ref = _match_intent_by_pattern(intent)
        if not ref:
            continue
        metric = get_metric(ref)
        if metric is None:
            return None, None, f"指标「{intent}」没有登记，算不出数值。"
        return ref, metric, None
    return None, None, None


def _match_intent_by_pattern(text: str) -> str | None:
    """按常见措辞匹配指标口径。

    取**最长**匹配：「设备耗电总量」归设备级，「总耗电」归总量级。
    """
    normalized = (text or "").lower()
    if not normalized:
        return None
    best: tuple[int, str] | None = None
    for pattern, metric_ref in _INTENT_PATTERNS:
        if pattern.lower() in normalized:
            score = len(pattern)
            if best is None or score > best[0]:
                best = (score, metric_ref)
    return best[1] if best else None


def _metric_is_backed(metric: Mapping[str, Any]) -> tuple[bool, str | None]:
    """这个指标当前算不算得出数。

    指标登记了不等于能出数：还要看它的底层字段在数据源目录里没有。
    PUE 就是这种 —— 登记在册，但数据源没有 ``pue`` 字段，算不出来。
    """
    field_ref = str(metric.get("fieldRef") or "")
    if field_ref and field_ref in _SOURCE_FIELDS:
        return True, None
    label = str(metric.get("label") or metric.get("metricRef") or "该指标")
    return False, f"「{label}」口径待核实：当前数据源没有登记这个字段，算不出数值。"


def _snapshot(room_id: str, logical_date: str, timezone: str):
    return build_snapshot(
        room_id=room_id,
        room_name=resolve_room_name(room_id),
        logical_date=logical_date,
        timezone=timezone,
    )


def _daily_totals(room_id: str, days: list[str], timezone: str) -> list[float]:
    return [
        _snapshot(room_id, day, timezone).totalEnergyKwh for day in days
    ]


def _device_rows(snapshot: Any) -> list[dict[str, Any]]:
    return [
        {
            "deviceId": device.deviceId,
            "deviceName": device.deviceName,
            "deviceType": device.deviceType,
            "energyKwh": device.energyKwh,
        }
        for device in snapshot.deviceRanking
    ]


def _window_label(days: list[str]) -> str:
    if not days:
        return ""
    if len(days) == 1:
        return f"{days[0]} 全天"
    return f"{days[0]} ~ {days[-1]}（{len(days)} 天）"


def _metric_card_payload(
    metric_ref: str,
    room_id: str,
    window: _Window,
    logical_date: str,
) -> dict[str, Any] | None:
    """指标卡：总量 + 环比。"""
    days = _period_dates(logical_date, window.count, window.offset)
    previous_days = _period_dates(logical_date, window.count, window.offset - window.count)

    if "device_energy" in metric_ref:
        # 设备耗电合计：取窗口最后一天的设备明细之和。
        snapshot = _snapshot(room_id, days[-1], window.timezone)
        value = round(
            sum(row["energyKwh"] for row in _device_rows(snapshot)), 1
        )
        previous = _snapshot(room_id, previous_days[-1], window.timezone)
        previous_value = round(
            sum(row["energyKwh"] for row in _device_rows(previous)), 1
        )
    else:
        totals = _daily_totals(room_id, days, window.timezone)
        value = round(sum(totals), 1)
        previous_totals = _daily_totals(room_id, previous_days, window.timezone)
        previous_value = round(sum(previous_totals), 1)

    percent: float | None = None
    if previous_value > 0:
        percent = round((value - previous_value) / previous_value * 100, 1)

    return {
        "label": "设备耗电" if "device_energy" in metric_ref else "总耗电",
        "value": value,
        "unit": "kWh",
        "precision": 1,
        "yoyPercent": percent,
        "comparisonAvailable": percent is not None,
        "windowLabel": _window_label(days),
    }


def _table_payload(
    metric_ref: str,
    room_id: str,
    window: _Window,
    logical_date: str,
) -> dict[str, Any] | None:
    """表格：设备明细行，或按天的耗电汇总行。"""
    days = _period_dates(logical_date, window.count, window.offset)

    if "device_energy" in metric_ref:
        snapshot = _snapshot(room_id, days[-1], window.timezone)
        rows = sorted(
            _device_rows(snapshot), key=lambda row: row["energyKwh"], reverse=True
        )[:_MAX_TABLE_ROWS]
        return {
            "columns": [
                {"id": "deviceName", "label": "设备", "fieldRef": "deviceName"},
                {"id": "deviceType", "label": "类型", "fieldRef": "deviceType"},
                {
                    "id": "energyKwh",
                    "label": "耗电（kWh）",
                    "fieldRef": "energyKwh",
                },
            ],
            "rows": rows,
            "rowNumber": True,
        }

    totals = _daily_totals(room_id, days, window.timezone)
    rows = [
        {"logicalDate": day, "energyKwh": total}
        for day, total in zip(days, totals)
    ]
    return {
        "columns": [
            {"id": "logicalDate", "label": "日期", "fieldRef": "logicalDate"},
            {
                "id": "energyKwh",
                "label": "总耗电（kWh）",
                "fieldRef": "energyKwh",
            },
        ],
        "rows": rows,
        "rowNumber": False,
    }


def _chart_payload(
    component_type: str,
    metric_ref: str,
    room_id: str,
    window: _Window,
    logical_date: str,
) -> dict[str, Any] | None:
    """折线 / 柱状 / 饼图。"""
    days = _period_dates(logical_date, window.count, window.offset)
    snapshot = _snapshot(room_id, days[-1], window.timezone)

    if "device_energy" in metric_ref:
        rows = sorted(
            _device_rows(snapshot), key=lambda row: row["energyKwh"], reverse=True
        )[:_MAX_TABLE_ROWS]
        x_labels = [str(row["deviceName"]) for row in rows]
        y_values = [row["energyKwh"] for row in rows]
    elif window.count > 1:
        totals = _daily_totals(room_id, days, window.timezone)
        x_labels = list(days)
        y_values = totals
    else:
        points = snapshot.hourlyTrend
        x_labels = [str(point.timestamp)[11:16] for point in points]
        y_values = [point.energyKwh for point in points]

    chart_kind = {
        "line_chart": "LINE",
        "bar_chart": "BAR",
        "pie_chart": "PIE",
    }.get(component_type, "LINE")

    return {
        "chartKind": chart_kind,
        "xLabels": x_labels,
        "yValues": y_values,
        "points": [{"label": label, "value": value} for label, value in zip(x_labels, y_values)],
        "unit": "kWh",
    }


def _text_payload(section: Mapping[str, Any], room_name: str) -> dict[str, Any]:
    """文字块：本来就是说明文字，没有数值。"""
    content = str(section.get("description") or section.get("title") or "")
    return {"rendered": content, "template": content}


def _build_region(
    section: Mapping[str, Any],
    *,
    index: int,
    room_id: str | None,
    room_reason: str | None,
    window: _Window,
    logical_date: str,
) -> TemplateRegion:
    """把方案里的一个板块变成一个模板区域。"""
    section = dict(section or {})
    section_id = str(section.get("id") or f"section_{index + 1}")
    title = str(section.get("title") or f"板块 {index + 1}")
    component_type = str(section.get("componentType") or "metric_card")
    intents = [
        str(item).strip()
        for item in (section.get("metricIntents") or [])
        if str(item).strip()
    ]

    def placeholder(reason: str, metric_refs: list[str] | None = None) -> TemplateRegion:
        return TemplateRegion(
            id=f"region_{index + 1}",
            sectionId=section_id,
            title=title,
            componentType=component_type,
            metricIntents=intents,
            metricRefs=metric_refs or [],
            dataStatus=RegionDataStatus.PLACEHOLDER.value,
            placeholderReason=reason,
        )

    if component_type == "text":
        return TemplateRegion(
            id=f"region_{index + 1}",
            sectionId=section_id,
            title=title,
            componentType=component_type,
            metricIntents=intents,
            dataStatus=RegionDataStatus.TEXT.value,
            data=_text_payload(section, resolve_room_name(room_id) if room_id else ""),
        )

    metric_ref, metric, intent_reason = _match_metric(intents)
    if metric_ref is None or metric is None:
        return placeholder(intent_reason or "数据待连接：当前已接通的能力里没有对应指标。")

    backed, reason = _metric_is_backed(metric)
    if not backed:
        return placeholder(reason or "数据待连接。", [metric_ref])

    if room_id is None:
        return placeholder(room_reason or "数据待连接：统计对象不在授权范围内。", [metric_ref])

    try:
        if component_type == "metric_card":
            data = _metric_card_payload(metric_ref, room_id, window, logical_date)
        elif component_type == "table":
            data = _table_payload(metric_ref, room_id, window, logical_date)
        else:
            data = _chart_payload(
                component_type, metric_ref, room_id, window, logical_date
            )
    except Exception:  # noqa: BLE001
        logger.exception("[report_template] 区域取数失败 region=%s", title)
        return placeholder("数据待连接：取数过程中出现异常。", [metric_ref])

    if not data:
        return placeholder("数据待连接：当前能力没有返回这个区域的数据。", [metric_ref])

    return TemplateRegion(
        id=f"region_{index + 1}",
        sectionId=section_id,
        title=title,
        componentType=component_type,
        metricIntents=intents,
        metricRefs=[metric_ref],
        dataStatus=RegionDataStatus.BOUND.value,
        data=data,
        dataSource=DATA_SOURCE,
        dataSourceLabel=DATA_SOURCE_LABEL,
    )


def build_template_design(
    plan: Mapping[str, Any],
    *,
    subject_id: Any = None,
) -> TemplateDesign:
    """从已确认的文字方案生成模板设计。

    **不抛异常**：能力不足不该让整份模板消失 —— 缺数据的区域转成占位，
    方案与板块一个都不丢。
    """
    plan = dict(plan or {})
    requirements = dict(plan.get("requirements") or {})
    sections = list(plan.get("sections") or [])

    topic = str(plan.get("topic") or "报表")
    plan_id = str(plan.get("id") or "")
    plan_revision = int(plan.get("revision") or 1)

    room_id, room_reason = _resolve_room(requirements.get("subject"), subject_id)
    window = _resolve_window(requirements.get("timeRange"))

    from app.services.mock_data_service import _default_logical_date
    from zoneinfo import ZoneInfo

    logical_date = _default_logical_date(ZoneInfo(window.timezone))

    regions: list[TemplateRegion] = []
    for index, section in enumerate(sections):
        regions.append(
            _build_region(
                section,
                index=index,
                room_id=room_id,
                room_reason=room_reason,
                window=window,
                logical_date=logical_date,
            )
        )

    if not regions:
        regions = [
            TemplateRegion(
                id="region_1",
                sectionId=None,
                title=topic,
                componentType="text",
                dataStatus=RegionDataStatus.TEXT.value,
                data={"rendered": "方案里没有可生成的板块。"},
            )
        ]

    bound = [r for r in regions if r.dataStatus == RegionDataStatus.BOUND.value]
    placeholders = [
        r for r in regions if r.dataStatus == RegionDataStatus.PLACEHOLDER.value
    ]
    if placeholders and bound:
        overall = "partial"
    elif placeholders:
        overall = "placeholder"
    else:
        overall = "bound"

    pending_notes = [
        f"「{region.title}」{region.placeholderReason}"
        for region in placeholders
        if region.placeholderReason
    ]

    return TemplateDesign(
        id=f"{TEMPLATE_ID_PREFIX}{uuid.uuid4().hex[:12]}",
        revision=1,
        planId=plan_id,
        planRevision=plan_revision,
        title=topic,
        regions=regions,
        layout={
            "columns": 2,
            "items": [
                {"regionId": region.id, "column": index % 2, "row": index // 2}
                for index, region in enumerate(regions)
            ],
        },
        referenceTemplates=list(plan.get("referenceTemplates") or []),
        dataStatus=overall,
        pendingNotes=pending_notes,
    )
