"""模拟业务数据生成器：把一份报表定义按**固定时间窗**渲染成区域数据。

**为什么单独一个模块**
``mock_data_service`` 是既有的**基础数据层**（机房能耗快照：确定性种子、
小时形状、设备排名）。本模块是**生成层**：在基础数据之上，按区域语义
（能耗 / 电费 / PUE / 功率 / 告警 / 机柜状态 / 通用）产出**既有公开预览接口
那种 componentType 数据对象**，让「报表示意」与将来的「报表预览」共用一套渲染逻辑。

**跨工单契约（规格「固定时间与造数」）**

    generate_simulated_data(definition, preview_context) -> {regionId: payload}

- ``preview_context`` 由 05 号票管理：``definitionId`` / ``definitionVersion`` /
  ``timezone`` / ``anchorTime`` / ``windowStart`` / ``windowEnd`` / ``grainUnit`` /
  ``generatorVersion``。
- 时间窗**左闭右开**（``[windowStart, windowEnd)``），按报表时区解析；
  跨午夜用**输入窗口**，不用当前日期。
- 返回**所有非标题区域**的 ``{regionId: payload}``；``payload`` 就是既有公开预览
  接口该 componentType 的数据对象，不是另一套原始快照。
- 结构非法或粒度无法解析 → **明确抛 :class:`SimulatedDataError`**，不返回半份
  成功载荷、不默默空表。
- **不落库、不写文件**，只在内存里出数。

**勾稽（同一对象范围、同一窗口共用一套基础数据）**
卡片总量 = 设备表各行之和 = 能量趋势各桶之和；总电费 = 总耗电 × 单价；
功率趋势按时长积分（积分回总耗电）；PUE 落在 1.2~1.8；告警约 5 条；
机柜状态行数 = 设备数。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.data.machine_room_energy import METRIC_DEVICE_ENERGY, METRIC_TOTAL_ENERGY
from app.services.edit_time_resolver import DEFAULT_TIMEZONE
from app.services.mock_data_service import (
    ROOM_A_DEVICES,
    build_snapshot,
    definition_timezone,
    hash_float,
    resolve_room_name,
    room_id_of_definition,
)


#: 生成器版本。随 ``previewContext.generatorVersion`` 冻结；
#: 变更生成器版本不得无声改变旧版本示意（规格「固定时间与造数」）。
GENERATOR_VERSION = "simulated-data/1"

#: 模拟电价（元/kWh）。总电费 = 总耗电 × 单价，所以总耗电保留 1 位小数、
#: 单价保留 1 位小数 → 乘积恰好 2 位小数，卡片上的两个数**严格相等**。
SIMULATED_ELECTRICITY_PRICE_PER_KWH = 0.7

#: 告警列表行数（约 5 条，需求稿 4.5）。
ALARM_ROW_COUNT = 5

#: 区域量身份（``previewSpec.kind`` / ``metrics[].kind``）。
KIND_ENERGY = "energy"
KIND_COST = "cost"
KIND_PUE = "pue"
KIND_POWER = "power"
KIND_ALARMS = "alarms"
KIND_CABINET_STATUS = "cabinet_status"
KIND_GENERIC = "generic"

KNOWN_KINDS = (
    KIND_ENERGY,
    KIND_COST,
    KIND_PUE,
    KIND_POWER,
    KIND_ALARMS,
    KIND_CABINET_STATUS,
    KIND_GENERIC,
)

#: 支持的趋势粒度。目录里分钟粒度只有 EAS 源支持，但**出数**不挑数据源。
SUPPORTED_GRAINS = ("MINUTE", "HOUR", "DAY")

#: 标题区域不产出数据载荷（规格：覆盖所有**非标题**区域）。
TITLE_ROLES = ("REPORT_TITLE",)

#: 告警级别取值池（示例值，非真实告警）。
_ALARM_LEVELS = ("严重", "重要", "一般", "提示", "警告")

#: 告警描述模板（示例值，非真实告警）。
_ALARM_ISSUES = (
    "送风温度偏高，制冷效率下降",
    "配电回路电流接近告警阈值",
    "市电中断，切换到 UPS 供电",
    "机柜进风温度超过建议值",
    "单机柜负载率持续偏高",
)

#: 设备运行状态取值池。
_CABINET_STATUSES = ("运行中", "运行中", "运行中", "运行中", "运行中", "告警", "维护中")

#: 图表纵轴字段：按量身份映射（前端按这个字段名取数）。
_CHART_Y_FIELDS = {
    KIND_ENERGY: "energyKwh",
    KIND_COST: "costYuan",
    KIND_POWER: "powerKw",
    KIND_PUE: "pueValue",
}

#: 图表纵轴单位：按纵轴字段反推。量身份认不出来、但定义里声明了
#: 纵轴字段时用它兜底（``pueValue`` 无量纲，用空串）。
_CHART_Y_FIELD_UNITS = {
    "energyKwh": "kWh",
    "costYuan": "元",
    "powerKw": "kW",
    "pueValue": "",
}


class SimulatedDataError(RuntimeError):
    """模拟数据生成失败。

    触发场景：定义结构非法、区域组件类型未知、表格区域没有列定义、
    时间窗或粒度无法解析。

    调用方（05 号票）必须把它转成**明确的失败响应**——不能退回空表、
    不能宣称成功。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------- 基础结构


@dataclass
class _Context:
    """解析后的生成上下文（时间窗已冻结在报表时区里）。"""

    timezone: str
    tz: ZoneInfo
    start: datetime
    end: datetime
    grain: str
    definition_id: str
    definition_version: str
    generator_version: str

    @property
    def seed_scope(self) -> str:
        """报表级种子材料：同一份定义 + 同一时间窗 → 同一组数。

        把**生成器版本**也算进来：版本一变，种子就变，界面上的数字跟着变，
        不会拿旧版本的数字冒充新版本的示意。
        """
        return (
            f"{self.definition_id}|{self.definition_version}|{self.generator_version}|"
            f"{self.start.isoformat()}|{self.end.isoformat()}"
        )


@dataclass
class _DayEntry:
    """窗口覆盖的一个报表本地自然日。"""

    day: date
    snapshot: Any
    #: 该自然日被窗口覆盖的比例（整天覆盖时为 1.0）。
    fraction: float
    #: 该自然日在真实时间轴上的小时数（夏令时日可能是 23 / 25）。
    full_hours: float


@dataclass
class _Bucket:
    """一个趋势桶。"""

    start: datetime
    end: datetime
    hours: float
    energy: float


@dataclass
class _Base:
    """同一对象范围 + 同一时间窗共用的基础数据。"""

    total_kwh: float
    devices: list[dict[str, Any]]
    window_label: str
    days: list[_DayEntry] = field(default_factory=list)
    #: 过滤后的总量与房间总量之比（桶按它缩放，桶和才与卡片一致）。
    scale: float = 1.0


# ---------------------------------------------------------------- 通用工具


def _round_to_total(values: list[float], target: float, ndigits: int = 1) -> list[float]:
    """把一组比例值按 ``target`` 归一，并**保证舍入后总和精确等于 target**。

    用「最大余数法」分配尾差：先各自向下取整到最小刻度，再把剩下的刻度
    按小数部分从大到小补给各项。��是各区域**统一舍入**的唯一入口——
    卡片、表格、趋势都走它，三者的和才不会差 0.1。
    """
    if not values:
        return []
    step = 10 ** (-ndigits)
    target_units = int(round(target / step))
    raw_total = sum(values)
    if raw_total <= 0:
        # 全零：均摊刻度
        base = target_units // len(values)
        units = [base] * len(values)
        for i in range(target_units - base * len(values)):
            units[i] += 1
        return [round(u * step, ndigits) for u in units]
    scaled = [v * (target / raw_total) for v in values]
    units = [int(math.floor(v / step + 1e-9)) for v in scaled]
    deficit = target_units - sum(units)
    if deficit > 0:
        order = sorted(
            range(len(values)),
            key=lambda i: (-(scaled[i] - units[i] * step), i),
        )
        for k in range(deficit):
            units[order[k % len(order)]] += 1
    elif deficit < 0:
        order = sorted(
            range(len(values)),
            key=lambda i: ((scaled[i] - units[i] * step), i),
        )
        for k in range(-deficit):
            units[order[k % len(order)]] -= 1
    # 再按刻度收一次口：避免 7849 * 0.1 这种二进制浮点尾巴漏到界面上。
    return [round(u * step, ndigits) for u in units]


def _hours_between(start: datetime, end: datetime) -> float:
    """两个时刻之间的**真实**小时数。

    两个时刻共用同一个 ``tzinfo`` 时，Python 的相减会**忽略时区偏移**，
    只算墙上时钟之差 —— 夏令时那天就会算成 24 小时。必须先换算到 UTC。
    """
    return (
        end.astimezone(dt_timezone.utc) - start.astimezone(dt_timezone.utc)
    ).total_seconds() / 3600.0


def _parse_iso(value: str, tz: ZoneInfo) -> datetime:
    """解析 ISO 时间串；没有偏移量时按**报表时区**理解。"""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise SimulatedDataError(
            "INVALID_CONTEXT", f"时间窗不是合法的 ISO 时间串：{value!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def _resolve_zone(name: str | None) -> tuple[str, ZoneInfo]:
    """解析报表时区。**认不出来就明确失败**（不静默按 UTC 出数）。"""
    tz_name = str(name or DEFAULT_TIMEZONE)
    try:
        return tz_name, ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise SimulatedDataError(
            "INVALID_CONTEXT", f"无法解析报表时区：{tz_name!r}"
        ) from exc


def _normalize_grain(value: Any) -> str | None:
    """把粒度归一成受支持的取值；明确给了非法值就报错。"""
    if value is None or str(value).strip() == "":
        return None
    grain = str(value).strip().upper()
    if grain not in SUPPORTED_GRAINS:
        raise SimulatedDataError(
            "UNRESOLVED_GRAIN",
            f"无法解析的时间粒度：{value!r}；受支持的取值为 {' / '.join(SUPPORTED_GRAINS)}。",
        )
    return grain


def _definition_grain(definition: dict[str, Any]) -> str | None:
    defaults = definition.get("defaults") or {}
    grain = defaults.get("grain") or {}
    value = grain.get("value") if isinstance(grain.get("value"), dict) else grain
    return _normalize_grain(value.get("unit"))


def _region_grain(region: dict[str, Any]) -> str | None:
    context = region.get("context") if isinstance(region.get("context"), dict) else {}
    grain = context.get("grain") or {}
    value = grain.get("value") if isinstance(grain.get("value"), dict) else grain
    return _normalize_grain(value.get("unit"))


def _window_from_calendar(
    definition: dict[str, Any], anchor: datetime, tz: ZoneInfo
) -> tuple[datetime, datetime] | None:
    """上下文没给绝对窗口时，用**定义里既有的日历窗口** + 锚点推导。

    口径与预览层一致：``offset = -1`` 是「昨天」，``offset = -7, count = 7``
    是「过去 7 个完整自然日」。
    """
    defaults = definition.get("defaults") or {}
    window = defaults.get("timeWindow") or {}
    value = window.get("value") if isinstance(window.get("value"), dict) else window
    if str(value.get("type") or "").upper() != "CALENDAR":
        return None
    unit = str(value.get("unit") or "DAY").upper()
    if unit != "DAY":
        return None
    count = max(1, int(value.get("count") or 1))
    offset = int(value.get("offset") if value.get("offset") is not None else -1)
    anchor_date = anchor.astimezone(tz).date()
    start_date = anchor_date + timedelta(days=offset)
    end_date = start_date + timedelta(days=count)
    return (
        datetime.combine(start_date, time(0, 0), tzinfo=tz),
        datetime.combine(end_date, time(0, 0), tzinfo=tz),
    )


def _build_context(
    definition: dict[str, Any], preview_context: dict[str, Any]
) -> _Context:
    """把 ``previewContext`` 解析成生成上下文。"""
    if not isinstance(preview_context, dict):
        raise SimulatedDataError(
            "INVALID_CONTEXT", "previewContext 必须是对象（05 号票负责提供）。"
        )
    if not isinstance(definition, dict):
        raise SimulatedDataError("INVALID_DEFINITION", "报表定义必须是对象。")

    timezone, tz = _resolve_zone(
        preview_context.get("timezone") or definition_timezone(definition)
    )

    start_raw = preview_context.get("windowStart")
    end_raw = preview_context.get("windowEnd")
    if start_raw and end_raw:
        start = _parse_iso(start_raw, tz)
        end = _parse_iso(end_raw, tz)
    else:
        anchor_raw = preview_context.get("anchorTime")
        if not anchor_raw:
            raise SimulatedDataError(
                "INVALID_CONTEXT",
                "previewContext 必须提供 windowStart / windowEnd（时间窗左闭右开）。",
            )
        anchor = _parse_iso(anchor_raw, tz)
        derived = _window_from_calendar(definition, anchor, tz)
        if derived is None:
            raise SimulatedDataError(
                "INVALID_CONTEXT",
                "previewContext 缺少 windowStart / windowEnd，且定义里没有可推导的日历窗口。",
            )
        start, end = derived

    if end <= start:
        raise SimulatedDataError(
            "INVALID_CONTEXT",
            f"时间窗非法：windowStart={start.isoformat()} 不早于 windowEnd={end.isoformat()}。",
        )

    grain = (
        _normalize_grain(preview_context.get("grainUnit"))
        or _definition_grain(definition)
        or "HOUR"
    )

    metadata = definition.get("metadata") or {}
    return _Context(
        timezone=timezone,
        tz=tz,
        start=start,
        end=end,
        grain=grain,
        definition_id=str(
            preview_context.get("definitionId") or metadata.get("definitionId") or "definition"
        ),
        definition_version=str(
            preview_context.get("definitionVersion")
            or metadata.get("definitionVersion")
            or "1"
        ),
        generator_version=str(preview_context.get("generatorVersion") or GENERATOR_VERSION),
    )


# ---------------------------------------------------------------- 基础数据


def _day_entries(
    ctx: _Context, room_id: str, room_name: str
) -> list[_DayEntry]:
    """窗口覆盖到的每个报表本地自然日 + 它的能耗快照与覆盖比例。"""
    entries: list[_DayEntry] = []
    tz = ctx.tz
    day = ctx.start.astimezone(tz).date()
    last_day = (ctx.end - timedelta(microseconds=1)).astimezone(tz).date()
    while day <= last_day:
        snapshot = build_snapshot(
            room_id=room_id,
            room_name=room_name,
            logical_date=day.isoformat(),
            timezone=ctx.timezone,
        )
        day_start = datetime.combine(day, time(0, 0), tzinfo=tz)
        next_day = day + timedelta(days=1)
        day_end = datetime.combine(next_day, time(0, 0), tzinfo=tz)
        full_hours = _hours_between(day_start, day_end)
        covered_start = max(day_start, ctx.start)
        covered_end = min(day_end, ctx.end)
        covered_hours = max(0.0, _hours_between(covered_start, covered_end))
        fraction = (covered_hours / full_hours) if full_hours > 0 else 0.0
        entries.append(
            _DayEntry(
                day=day,
                snapshot=snapshot,
                fraction=fraction,
                full_hours=full_hours,
            )
        )
        day = next_day
    return entries


def _build_base(
    definition: dict[str, Any], ctx: _Context
) -> _Base:
    """同一对象范围 + 同一时间窗的**共同基础数据**。

    能耗总量、设备明细按同一份日快照累加，再统一舍入——所以卡片、表格、
    趋势用的是同一套数，不会各造各的。
    """
    room_id = room_id_of_definition(definition)
    room_name = resolve_room_name(room_id)
    entries = _day_entries(ctx, room_id, room_name)

    total = sum(entry.snapshot.totalEnergyKwh * entry.fraction for entry in entries)

    device_ids = [d["deviceId"] for d in ROOM_A_DEVICES]
    device_names = {d["deviceId"]: d["deviceName"] for d in ROOM_A_DEVICES}
    device_types = {d["deviceId"]: d.get("deviceType", "") for d in ROOM_A_DEVICES}
    raw_devices: list[float] = []
    for device_id in device_ids:
        amount = 0.0
        for entry in entries:
            for row in entry.snapshot.deviceRanking:
                if row.deviceId == device_id:
                    amount += row.energyKwh * entry.fraction
        raw_devices.append(amount)
    # 总量先定死（1 位小数），设备行再按它归一 —— 卡片与表格的和才严格相等。
    total = round(total, 1)
    rounded_devices = _round_to_total(raw_devices, total, ndigits=1)

    devices = [
        {
            "deviceId": device_id,
            "deviceName": device_names.get(device_id, device_id),
            "deviceType": device_types.get(device_id, ""),
            "energyKwh": rounded_devices[index],
        }
        for index, device_id in enumerate(device_ids)
    ]
    devices.sort(key=lambda row: row["energyKwh"], reverse=True)

    # 报表口径的过滤：缩小**对象范围**。卡片、设备表、趋势一起缩小，
    # 三者的和才不会各算各的。
    room_total = total
    filters = _report_filters(definition)
    if filters:
        devices = [
            row
            for row in devices
            if all(_matches_condition(row, condition) for condition in filters)
        ]
        total = round(sum(row["energyKwh"] for row in devices), 1)
    scale = (total / room_total) if room_total > 0 else 0.0

    return _Base(
        total_kwh=total,
        devices=devices,
        window_label=_window_label(ctx, entries),
        days=entries,
        scale=scale,
    )


def _window_label(ctx: _Context, entries: list[_DayEntry]) -> str:
    """窗口标签：单日「某天 全天」，多日「首日 ~ 末日（N 天）」。"""
    if not entries:
        return ""
    days = [entry.day.isoformat() for entry in entries]
    if len(days) == 1:
        return f"{days[0]} 全天"
    return f"{days[0]} ~ {days[-1]}（{len(days)} 天）"


def _segments(
    start: datetime, end: datetime, step: timedelta, tz: ZoneInfo
) -> list[tuple[datetime, datetime]]:
    """在**真实时间轴**上切段（不是墙上时钟）。

    夏令时那天，本地自然日可能是 23 或 25 小时：在 UTC 上步进才能得到
    真实时长，桶数自然等于实际窗口长度，不会写死 24。
    """
    segments: list[tuple[datetime, datetime]] = []
    cursor = start.astimezone(dt_timezone.utc)
    stop = end.astimezone(dt_timezone.utc)
    while cursor < stop:
        nxt = min(cursor + step, stop)
        segments.append((cursor.astimezone(tz), nxt.astimezone(tz)))
        cursor = nxt
    return segments


def _buckets_for_grain(
    ctx: _Context, base: _Base, grain: str
) -> list[_Bucket]:
    """按粒度切桶。

    - ``HOUR``：每**实际小时**一个桶（单日正常 24 个；夏令时日 23/25 个）。
    - ``DAY``：每个报表本地自然日一个桶。
    - ``MINUTE``：每实际分钟一个桶。
    """
    tz = ctx.tz
    if grain == "DAY":
        raw: list[float] = []
        spans: list[tuple[datetime, datetime, float]] = []
        for entry in base.days:
            day_start = datetime.combine(entry.day, time(0, 0), tzinfo=tz)
            day_end = datetime.combine(
                entry.day + timedelta(days=1), time(0, 0), tzinfo=tz
            )
            raw.append(entry.snapshot.totalEnergyKwh * entry.fraction)
            spans.append((day_start, day_end, entry.fraction))
        values = _round_to_total(
            [value * base.scale for value in raw], base.total_kwh, ndigits=1
        )
        return [
            _Bucket(start=start, end=end, hours=_hours_between(start, end),
                    energy=value)
            for (start, end, _fraction), value in zip(spans, values)
        ]

    step = timedelta(hours=1) if grain == "HOUR" else timedelta(minutes=1)
    raw_values: list[float] = []
    spans: list[tuple[datetime, datetime]] = []
    for entry in base.days:
        shape = [point.energyKwh for point in entry.snapshot.hourlyTrend]
        if not shape:
            continue
        day_start = datetime.combine(entry.day, time(0, 0), tzinfo=tz)
        day_end = datetime.combine(
            entry.day + timedelta(days=1), time(0, 0), tzinfo=tz
        )
        segments = _segments(day_start, day_end, step, tz)
        covered = [
            seg
            for seg in segments
            if seg[1] > ctx.start and seg[0] < ctx.end
        ]
        if not covered:
            continue
        # 该日的桶按**本地钟点**取小时形状；夏令时重复的那一小时会取到同一条形状。
        day_raw = [
            shape[min(23, max(0, seg[0].astimezone(tz).hour))]
            / (60 if grain == "MINUTE" else 1)
            for seg in covered
        ]
        day_target = entry.snapshot.totalEnergyKwh * entry.fraction
        day_sum = sum(day_raw)
        scale = (day_target / day_sum) if day_sum > 0 else 0.0
        for seg, value in zip(covered, day_raw):
            raw_values.append(value * scale)
            spans.append(seg)

    values = _round_to_total(
        [value * base.scale for value in raw_values], base.total_kwh, ndigits=1
    )
    return [
        _Bucket(
            start=start,
            end=end,
            hours=_hours_between(start, end),
            energy=value,
        )
        for (start, end), value in zip(spans, values)
    ]


def _bucket_powers(base: _Base, buckets: list[_Bucket], seed: str = "") -> list[float]:
    """每个桶的平均功率（kW），**按真实时长**换算。

    功率趋势的积分必须回到总耗电，所以先给桶能耗，再除以时长得到功率，
    加一点确定性噪声后再整体缩放，最后把尾差补回最后一个桶——
    这样 ``Σ(功率 × 时长)`` 与总量一致。
    """
    powers: list[float] = []
    for index, bucket in enumerate(buckets):
        hours = bucket.hours if bucket.hours > 0 else 1.0
        average = bucket.energy / hours
        noise = hash_float(f"power/{seed or base.window_label}/{index}", 0.88, 1.12)
        powers.append(average * noise)
    integral = sum(p * (b.hours if b.hours > 0 else 1.0) for p, b in zip(powers, buckets))
    if integral > 0 and buckets:
        scale = base.total_kwh / integral
        powers = [round(p * scale, 3) for p in powers]
        residual = base.total_kwh - sum(
            p * (b.hours if b.hours > 0 else 1.0) for p, b in zip(powers, buckets)
        )
        if buckets:
            last_hours = buckets[-1].hours if buckets[-1].hours > 0 else 1.0
            powers[-1] = round(powers[-1] + residual / last_hours, 6)
    return powers


# ---------------------------------------------------------------- 区域语义


def _preview_spec(region: dict[str, Any]) -> dict[str, Any]:
    binding = region.get("templateBinding") or {}
    spec = binding.get("previewSpec") if isinstance(binding, dict) else None
    return spec if isinstance(spec, dict) else {}


def _binding_metric_refs(region: dict[str, Any]) -> set[str]:
    """区域既有执行绑定里出现的指标引用。"""
    refs: set[str] = set()
    binding = region.get("binding") or {}
    if isinstance(binding, dict):
        refs.update(str(r) for r in (binding.get("metricRefs") or []))
    data = region.get("data") or {}
    nodes = ((data.get("pipeline") or {}).get("nodes") or []) if isinstance(data, dict) else []
    for node in nodes:
        if isinstance(node, dict) and node.get("metricRef"):
            refs.add(str(node["metricRef"]))
    return refs


def _region_filters(region: dict[str, Any]) -> list[dict[str, Any]]:
    """区域 pipeline 里的过滤条件。"""
    data = region.get("data") or {}
    nodes = ((data.get("pipeline") or {}).get("nodes") or []) if isinstance(data, dict) else []
    for node in nodes:
        if isinstance(node, dict) and node.get("type") == "FILTER":
            return [c for c in (node.get("conditions") or []) if isinstance(c, dict)]
    return []


def _report_filters(definition: dict[str, Any]) -> list[dict[str, Any]]:
    """报表口径的过滤条件。

    定义里的过滤是**报表级**的（每个区域都带同一份 FILTER 节点），
    所以取一份统一套用：卡片、设备表、趋势才会落在同一个对象范围上。
    """
    for region in definition.get("regions") or []:
        if not isinstance(region, dict):
            continue
        conditions = _region_filters(region)
        if conditions:
            return conditions
    return []


def _matches_condition(row: dict[str, Any], condition: dict[str, Any]) -> bool:
    """过滤条件是否命中一行（口径与既有预览层一致）。"""
    field_ref = condition.get("fieldRef")
    if not field_ref:
        return True
    values = condition.get("values") or []
    op = str(condition.get("op") or "IN").upper().replace(" ", "_").replace("-", "_")
    actual = row.get(field_ref)
    if op == "IN":
        return str(actual) in {str(v) for v in values}
    if op == "NOT_IN":
        return str(actual) not in {str(v) for v in values}
    if op == "EQ":
        return str(actual) == str(values[0] if values else "")
    if op == "NE":
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


def _resolve_kind(region: dict[str, Any], spec: dict[str, Any]) -> str:
    """区域的主展示量身份。

    优先用 03 号票写在 ``previewSpec.kind`` 的量身份；**不按区域标题猜量**。
    历史定义没有 previewSpec 时，退回**既有执行绑定**的指标引用；
    认不出来就是 ``generic``（明确标注为模拟的示例），不因此留空。
    """
    kind = str(spec.get("kind") or "").strip().lower()
    if kind in KNOWN_KINDS:
        return kind
    refs = _binding_metric_refs(region)
    if refs & {METRIC_TOTAL_ENERGY["metricRef"], METRIC_DEVICE_ENERGY["metricRef"]}:
        return KIND_ENERGY
    return KIND_GENERIC


def _resolve_metrics(
    region: dict[str, Any], spec: dict[str, Any], kind: str
) -> list[dict[str, Any]]:
    """区域的指标清单（多指标消费 ``metrics[]``，不是只看主量）。"""
    raw_metrics = spec.get("metrics") or []
    metrics: list[dict[str, Any]] = []
    for item in raw_metrics:
        if not isinstance(item, dict):
            continue
        metric_kind = str(item.get("kind") or "").strip().lower()
        if metric_kind not in KNOWN_KINDS:
            metric_kind = kind
        metrics.append(
            {
                "metricKey": str(item.get("metricKey") or metric_kind),
                "label": str(item.get("label") or region.get("name") or "指标"),
                "kind": metric_kind,
                "unit": str(item.get("unit") or spec.get("unit") or ""),
                "capabilityPending": bool(item.get("capabilityPending")),
                "pendingReason": item.get("pendingReason"),
            }
        )
    if metrics:
        return metrics
    metric = (region.get("component") or {}).get("metric") or {}
    return [
        {
            "metricKey": kind,
            "label": str(metric.get("label") or region.get("name") or "指标"),
            "kind": kind,
            "unit": str(spec.get("unit") or (metric.get("format") or {}).get("unitLabel") or ""),
            "capabilityPending": bool(spec.get("capabilityPending")),
            "pendingReason": spec.get("pendingReason"),
        }
    ]


def _unit_for(kind: str, spec: dict[str, Any], fallback: str = "") -> str:
    """区域主量的单位：优先定义里声明的，其次按量身份的默认值。"""
    declared = str(spec.get("unit") or "").strip()
    if declared:
        return declared
    defaults = {
        KIND_ENERGY: "kWh",
        KIND_COST: "元",
        KIND_POWER: "kW",
        KIND_PUE: "",
        KIND_ALARMS: "条",
        KIND_CABINET_STATUS: "个",
        KIND_GENERIC: fallback or "",
    }
    return defaults.get(kind, fallback or "")


def _chart_units(
    spec: dict[str, Any], kind: str, metrics: list[dict[str, Any]]
) -> list[str]:
    """图表里每个系列各自的单位（顺序与 ``metrics`` 一致）。

    单位来源优先级（规格：数值型组件带 ``unit``，前端从载荷读，不得硬编码）：
    1. 该指标自己声明的单位（``previewSpec.metrics[].unit``）；
    2. 区域声明的单位（``previewSpec.unit``）——**只对区域主量**套用：
       多指标时把主量的单位盖到别的指标上会显示错误单位；
    3. 按量身份（``kind``）的默认单位（``energy``→``kWh``、``cost``→``元``…）；
    4. 按纵轴字段反推（``energyKwh``→``kWh``…）——由调用方兜底。

    ``previewSpec.unit`` 还没落地（03 号票）时自动落到 3 级——
    **代码容忍它缺失**，不因此留空或瞎猜。
    """
    declared: dict[str, str] = {}
    for raw in spec.get("metrics") or []:
        if isinstance(raw, dict):
            key = str(raw.get("metricKey") or "").strip()
            if key:
                declared[key] = str(raw.get("unit") or "").strip()

    units: list[str] = []
    for item in metrics:
        key = str(item.get("metricKey") or "").strip()
        metric_kind = str(item.get("kind") or kind or "").strip().lower()
        unit = declared.get(key, "")
        if not unit and metric_kind == kind:
            # 区域主量：区域声明的单位就是它的单位（单指标区域同样适用）。
            unit = str(item.get("unit") or "").strip()
        if not unit:
            unit = _unit_for(metric_kind, {}, "")
        units.append(unit)
    return units


# ---------------------------------------------------------------- 列分类


def _column_value_kind(label: str, field_ref: str) -> str:
    """按**列名**判断这一列该出什么样的值。

    顺序敏感：先判告警级别/状态/时间，再判设备，最后才落到数值与兜底。
    """
    text = f"{label or ''} {field_ref or ''}".lower()
    if any(k in text for k in ("级别", "等级", "严重", "severity", "level")):
        return "level"
    if any(k in text for k in ("状态", "status", "运行情况")):
        return "status"
    if any(k in text for k in ("时间", "时刻", "日期", "timestamp", "datetime", "time", "date")):
        return "time"
    if any(k in text for k in ("设备编号", "设备id", "机柜编号", "资产编号", "deviceid", "assetid")):
        return "device_id"
    if any(k in text for k in ("设备类型", "类型", "devicetype")):
        return "device_type"
    if any(k in text for k in ("设备名称", "设备名", "机柜名称", "机柜", "名称", "devicename", "device")):
        return "device_name"
    if any(k in text for k in ("pue", "能效")):
        return "pue"
    if any(k in text for k in ("功率", "power")):
        return "power"
    if any(k in text for k in ("电费", "费用", "成本", "金额", "cost", "fee")):
        return "cost"
    if any(k in text for k in ("能耗", "耗电", "用电", "电量", "kwh", "energy")):
        return "energy"
    if any(k in text for k in ("占比", "比例", "比率", "率", "percent", "ratio", "share")):
        return "percent"
    if any(k in text for k in ("数量", "次数", "台数", "条数", "count", "num")):
        return "count"
    if any(k in text for k in ("耗时", "时长", "duration")):
        return "duration"
    if any(k in text for k in ("温度", "temperature", "℃")):
        return "temperature"
    if any(k in text for k in ("电压", "voltage")):
        return "voltage"
    if any(k in text for k in ("电流", "current")):
        return "current"
    if any(k in text for k in ("描述", "说明", "详情", "备注", "desc", "note", "message")):
        return "description"
    return "text"


# ---------------------------------------------------------------- 值生成


def _generic_value(kind: str, seed: str) -> Any:
    """按列的值类型给一个合理的示例值。"""
    if kind == "level":
        index = int(hash_float(seed, 0, len(_ALARM_LEVELS) - 1e-6))
        return _ALARM_LEVELS[index % len(_ALARM_LEVELS)]
    if kind == "status":
        index = int(hash_float(seed, 0, len(_CABINET_STATUSES) - 1e-6))
        return _CABINET_STATUSES[index % len(_CABINET_STATUSES)]
    if kind == "percent":
        return round(hash_float(seed, 8.0, 92.0), 1)
    if kind == "count":
        return int(hash_float(seed, 1, 20))
    if kind == "duration":
        return round(hash_float(seed, 1.0, 120.0), 1)
    if kind == "temperature":
        return round(hash_float(seed, 18.0, 29.0), 1)
    if kind == "voltage":
        return round(hash_float(seed, 218.0, 242.0), 1)
    if kind == "current":
        return round(hash_float(seed, 3.0, 42.0), 1)
    if kind == "pue":
        return round(hash_float(seed, 1.2, 1.8 - 1e-6), 2)
    if kind == "text":
        return f"示例值 {int(hash_float(seed, 1, 99))}"
    return round(hash_float(seed, 10.0, 500.0), 1)


def _metric_value(
    kind: str,
    *,
    base: _Base,
    buckets: list[_Bucket],
    powers: list[float],
    seed: str,
    unit: str,
) -> float:
    """某个量身份对应的数值（卡片、指标清单共用同一个口径）。"""
    if kind == KIND_ENERGY:
        return base.total_kwh
    if kind == KIND_COST:
        return round(base.total_kwh * SIMULATED_ELECTRICITY_PRICE_PER_KWH, 2)
    if kind == KIND_PUE:
        # PUE 只给一个**标注为模拟**的合理区间值，不编造真实计算公式。
        return round(hash_float(f"pue/{seed}", 1.2, 1.8 - 1e-6), 2)
    if kind == KIND_POWER:
        return powers[-1] if powers else 0.0
    if kind == KIND_ALARMS:
        return float(ALARM_ROW_COUNT)
    if kind == KIND_CABINET_STATUS:
        return float(len(base.devices))
    # generic：一个量级合理的示例值（明确标注为模拟）。
    if unit in ("kWh", "kwh"):
        return round(hash_float(f"generic/{seed}", 800.0, 5200.0), 1)
    if unit in ("元", "CNY", "RMB"):
        return round(hash_float(f"generic/{seed}", 500.0, 3600.0), 2)
    if unit in ("%", "％"):
        return round(hash_float(f"generic/{seed}", 10.0, 90.0), 1)
    return round(hash_float(f"generic/{seed}", 5.0, 95.0), 1)


# ---------------------------------------------------------------- 载荷构造


def _metric_card_payload(
    region: dict[str, Any],
    spec: dict[str, Any],
    kind: str,
    metrics: list[dict[str, Any]],
    *,
    base: _Base,
    buckets: list[_Bucket],
    powers: list[float],
    ctx: _Context,
    yoy: float,
) -> dict[str, Any]:
    component = region.get("component") or {}
    metric = component.get("metric") or {}
    fmt = metric.get("format") or {}
    declared_precision = fmt.get("precision")

    metric_payloads: list[dict[str, Any]] = []
    primary_value: float | None = None
    primary_unit = ""
    primary_precision = 1
    # 主量：区域 kind 声明的那个指标；没有就取清单里的第一个。
    ordered = [item for item in metrics if item["kind"] == kind] + [
        item for item in metrics if item["kind"] != kind
    ]
    for item in ordered:
        value = _metric_value(
            item["kind"],
            base=base,
            buckets=buckets,
            powers=powers,
            seed=f"{ctx.seed_scope}|{item['metricKey']}",
            unit=item.get("unit") or "",
        )
        precision = declared_precision
        if precision is None:
            precision = 2 if item["kind"] in (KIND_COST, KIND_PUE) else 1
        if item["kind"] == KIND_COST:
            # 电费按 2 位小数：总耗电（1 位）× 单价（1 位）= 2 位，两个卡片严格相等。
            precision = max(int(precision), 2)
        value = round(float(value), int(precision))
        metric_payloads.append(
            {
                "metricKey": item["metricKey"],
                "label": item["label"],
                "kind": item["kind"],
                "unit": item.get("unit") or "",
                "value": value,
                "precision": int(precision),
                "capabilityPending": bool(item["capabilityPending"]),
                "pendingReason": item.get("pendingReason"),
            }
        )
        if primary_value is None:
            primary_value = value
            primary_unit = item.get("unit") or ""
            primary_precision = int(precision)

    if primary_value is None:
        primary_value = 0.0

    unit = primary_unit or _unit_for(kind, spec, str(fmt.get("unitLabel") or ""))
    return {
        "label": str(metric.get("label") or region.get("name") or "指标"),
        "value": primary_value,
        "unit": unit,
        "precision": primary_precision,
        "yoyPercent": yoy,
        "comparisonAvailable": True,
        "windowLabel": base.window_label,
        "dataSource": "simulated",
        "metrics": metric_payloads,
    }


def _columns_of(region: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
    """区域声明的列：优先 ``previewSpec.columns``（03 号票定稿），否则用组件列。"""
    raw = spec.get("columns")
    if not isinstance(raw, list) or not raw:
        raw = (region.get("component") or {}).get("columns") or []
    columns: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        field_ref = str(item.get("fieldRef") or "")
        if not field_ref:
            continue
        columns.append(
            {
                "id": item.get("id"),
                "label": item.get("label"),
                "fieldRef": field_ref,
                "format": item.get("format"),
            }
        )
    return columns


def _row_value(
    value_kind: str,
    *,
    seed: str,
    device: dict[str, Any] | None,
    bucket: _Bucket | None,
    hours: float,
) -> Any:
    """一列在一个具体行里的值。"""
    if value_kind == "device_id":
        return (device or {}).get("deviceId", "")
    if value_kind == "device_name":
        return (device or {}).get("deviceName", "")
    if value_kind == "device_type":
        return (device or {}).get("deviceType", "")
    if value_kind == "energy":
        if device is not None:
            return device["energyKwh"]
        if bucket is not None:
            return bucket.energy
        return 0.0
    if value_kind == "cost":
        amount = (
            device["energyKwh"]
            if device is not None
            else (bucket.energy if bucket is not None else 0.0)
        )
        return round(amount * SIMULATED_ELECTRICITY_PRICE_PER_KWH, 2)
    if value_kind == "power":
        amount = (
            device["energyKwh"]
            if device is not None
            else (bucket.energy if bucket is not None else 0.0)
        )
        return round(amount / hours if hours > 0 else 0.0, 2)
    if value_kind == "time":
        moment = bucket.start if bucket is not None else None
        return moment.isoformat() if moment is not None else ""
    if value_kind == "level":
        index = int(hash_float(seed, 0, len(_ALARM_LEVELS) - 1e-6))
        return _ALARM_LEVELS[index % len(_ALARM_LEVELS)]
    if value_kind == "status":
        index = int(hash_float(seed, 0, len(_CABINET_STATUSES) - 1e-6))
        return _CABINET_STATUSES[index % len(_CABINET_STATUSES)]
    if value_kind == "description":
        issue = _ALARM_ISSUES[
            int(hash_float(seed, 0, len(_ALARM_ISSUES) - 1e-6)) % len(_ALARM_ISSUES)
        ]
        return issue
    return _generic_value(value_kind, seed)


def _table_payload(
    region: dict[str, Any],
    spec: dict[str, Any],
    kind: str,
    *,
    base: _Base,
    buckets: list[_Bucket],
    ctx: _Context,
) -> dict[str, Any]:
    """表格载荷：按**列名**生成行，列的 fieldRef 一定能读到单元格。"""
    columns = _columns_of(region, spec)
    if not columns:
        raise SimulatedDataError(
            "MISSING_COLUMNS",
            f"区域 {region.get('id')!r} 是表格，但没有声明任何列，无法生成行。",
        )
    kinds = [
        _column_value_kind(str(c.get("label") or ""), str(c.get("fieldRef") or ""))
        for c in columns
    ]
    has_device = any(k in kinds for k in ("device_name", "device_id", "device_type"))
    has_time = "time" in kinds
    has_energy = "energy" in kinds

    window_hours = _window_hours(buckets)

    rows: list[dict[str, Any]] = []
    if kind == KIND_ALARMS:
        for index in range(ALARM_ROW_COUNT):
            device = base.devices[
                int(hash_float(f"alarm/{ctx.seed_scope}/{index}", 0, len(base.devices) - 1e-6))
                % len(base.devices)
            ] if base.devices else None
            row: dict[str, Any] = {}
            for column, value_kind in zip(columns, kinds):
                seed = f"alarm/{ctx.seed_scope}/{index}/{column['fieldRef']}"
                if value_kind in ("device_name", "device_id", "device_type") and device:
                    row[column["fieldRef"]] = _row_value(
                        value_kind, seed=seed, device=device, bucket=None,
                        hours=window_hours,
                    )
                elif value_kind == "time":
                    moment = _moment_in_window(ctx, index)
                    row[column["fieldRef"]] = moment.isoformat()
                else:
                    row[column["fieldRef"]] = _row_value(
                        value_kind, seed=seed, device=device, bucket=None,
                        hours=window_hours,
                    )
            rows.append(row)
    elif kind == KIND_CABINET_STATUS:
        for device in base.devices:
            row = {}
            for column, value_kind in zip(columns, kinds):
                seed = f"cabinet/{ctx.seed_scope}/{device['deviceId']}/{column['fieldRef']}"
                row[column["fieldRef"]] = _row_value(
                    value_kind, seed=seed, device=device, bucket=None,
                    hours=window_hours,
                )
            rows.append(row)
    elif has_device:
        for device in base.devices:
            row = {}
            for column, value_kind in zip(columns, kinds):
                seed = f"device/{ctx.seed_scope}/{device['deviceId']}/{column['fieldRef']}"
                row[column["fieldRef"]] = _row_value(
                    value_kind, seed=seed, device=device, bucket=None,
                    hours=window_hours,
                )
            rows.append(row)
    elif has_time and has_energy:
        for bucket in buckets:
            row = {}
            for column, value_kind in zip(columns, kinds):
                seed = f"trend/{ctx.seed_scope}/{bucket.start.isoformat()}/{column['fieldRef']}"
                row[column["fieldRef"]] = _row_value(
                    value_kind, seed=seed, device=None, bucket=bucket,
                    hours=window_hours,
                )
            rows.append(row)
    elif has_time:
        for bucket in buckets:
            row = {}
            for column, value_kind in zip(columns, kinds):
                seed = f"series/{ctx.seed_scope}/{bucket.start.isoformat()}/{column['fieldRef']}"
                row[column["fieldRef"]] = _row_value(
                    value_kind, seed=seed, device=None, bucket=bucket,
                    hours=window_hours,
                )
            rows.append(row)
    else:
        for device in base.devices:
            row = {}
            for column, value_kind in zip(columns, kinds):
                seed = f"generic/{ctx.seed_scope}/{device['deviceId']}/{column['fieldRef']}"
                row[column["fieldRef"]] = _row_value(
                    value_kind, seed=seed, device=device, bucket=None,
                    hours=window_hours,
                )
            rows.append(row)

    return {
        "columns": columns,
        "rowNumber": bool((region.get("component") or {}).get("rowNumber", True)),
        "rows": rows,
        "dataSource": "simulated",
    }


def _moment_in_window(ctx: _Context, index: int) -> datetime:
    """告警发生时间：窗口内的一个确定时刻。"""
    span = _hours_between(ctx.start, ctx.end) * 3600.0
    if span <= 0:
        return ctx.start
    offset = span * ((index + 0.5) / ALARM_ROW_COUNT)
    return ctx.start + timedelta(seconds=offset)


def _axis_labels(buckets: list[_Bucket], grain: str, multi_day: bool) -> list[str]:
    """横轴标签：与点数一一对应，且能看出是哪一天。"""
    labels: list[str] = []
    for bucket in buckets:
        stamp = bucket.start
        if grain == "DAY":
            labels.append(stamp.strftime("%m-%d"))
        elif grain == "MINUTE":
            labels.append(
                stamp.strftime("%m-%d %H:%M") if multi_day else stamp.strftime("%H:%M")
            )
        else:
            labels.append(
                stamp.strftime("%m-%d %H:00") if multi_day else stamp.strftime("%H:%M")
            )
    return labels


def _chart_payload(
    region: dict[str, Any],
    spec: dict[str, Any],
    kind: str,
    metrics: list[dict[str, Any]],
    *,
    base: _Base,
    buckets: list[_Bucket],
    powers: list[float],
    ctx: _Context,
) -> dict[str, Any]:
    """图表载荷：轴、序列与点，横轴时间和点数一一对应。"""
    component = region.get("component") or {}
    role = str(region.get("role") or "").upper()
    encodings = component.get("encodings") or []
    declared_x = next(
        (e.get("fieldRef") for e in encodings if e.get("channel") == "X"), None
    )
    declared_y = next(
        (e.get("fieldRef") for e in encodings if e.get("channel") == "Y"), None
    )
    grain = _region_grain(region) or ctx.grain
    multi_day = len({b.start.date() for b in buckets}) > 1

    def _y_field_for(metric_kind: str) -> str:
        return _CHART_Y_FIELDS.get(metric_kind, "value")

    def _series_for(
        metric_kind: str
    ) -> tuple[list[str], list[float], str, list[Any]]:
        """返回 (x 标签, y 值, y 字段名, x 原值)。

        x 原值是**带时区的 ISO 时间戳**（趋势）或对象名（对比），
        与既有预览接口的 ``points`` 形状一致；``xLabels`` 才是给人看的标签。
        """
        if role == "TREND":
            if metric_kind == KIND_COST:
                # 各桶电费之和 == 卡片上的总电费（同一套舍入，不差分毫）。
                values = _round_to_total(
                    [b.energy * SIMULATED_ELECTRICITY_PRICE_PER_KWH for b in buckets],
                    round(base.total_kwh * SIMULATED_ELECTRICITY_PRICE_PER_KWH, 2),
                    ndigits=2,
                )
            elif metric_kind == KIND_POWER:
                values = list(powers)
            elif metric_kind == KIND_PUE:
                values = [
                    round(hash_float(f"pue/{ctx.seed_scope}/{b.start.isoformat()}", 1.2, 1.8 - 1e-6), 2)
                    for b in buckets
                ]
            elif metric_kind == KIND_GENERIC:
                values = [
                    round(hash_float(f"generic/{ctx.seed_scope}/{b.start.isoformat()}", 10.0, 90.0), 1)
                    for b in buckets
                ]
            else:
                values = [b.energy for b in buckets]
            return (
                _axis_labels(buckets, grain, multi_day),
                values,
                _y_field_for(metric_kind),
                [b.start.isoformat() for b in buckets],
            )
        # 对象对比（柱状/占比）：横轴是对象名，纵轴是该对象的量。
        if metric_kind == KIND_POWER:
            values = [
                round(d["energyKwh"] / max(1.0, _window_hours(buckets)), 2)
                for d in base.devices
            ]
        elif metric_kind == KIND_COST:
            values = [
                round(d["energyKwh"] * SIMULATED_ELECTRICITY_PRICE_PER_KWH, 2)
                for d in base.devices
            ]
        elif metric_kind == KIND_PUE:
            values = [
                round(hash_float(f"pue/{ctx.seed_scope}/{d['deviceId']}", 1.2, 1.8 - 1e-6), 2)
                for d in base.devices
            ]
        elif metric_kind == KIND_GENERIC:
            values = [
                round(hash_float(f"generic/{ctx.seed_scope}/{d['deviceId']}", 10.0, 90.0), 1)
                for d in base.devices
            ]
        else:
            values = [d["energyKwh"] for d in base.devices]
        names = [d["deviceName"] for d in base.devices]
        return names, values, _y_field_for(metric_kind), names

    primary = metrics[0] if metrics else {"kind": kind, "metricKey": kind, "label": region.get("name")}
    x_labels, y_values, y_field, x_values = _series_for(primary["kind"])
    if declared_y and y_field == "value":
        # 量身份认不出来时，认定义里声明的纵轴字段。
        y_field = declared_y
    x_field = declared_x or ("timestamp" if role == "TREND" else "deviceName")
    if x_field == "deviceName" and role == "TREND":
        x_field = "timestamp"

    points = [
        {x_field: x_value, y_field: value}
        for x_value, value in zip(x_values, y_values)
    ]

    # 单位：规格要求数值型组件自带单位，前端从载荷读，不硬编码。
    units = _chart_units(spec, kind, metrics)
    unit = units[0] if units else ""
    if not unit:
        # 量身份认不出来时，按纵轴字段反推（energyKwh→kWh …）。
        unit = _CHART_Y_FIELD_UNITS.get(y_field, "")

    payload: dict[str, Any] = {
        "chartKind": str(component.get("chartKind") or "LINE"),
        "grain": grain,
        "xField": x_field,
        "yField": y_field,
        "unit": unit,
        "xLabels": x_labels,
        "yValues": y_values,
        "points": points,
        "dataSource": "simulated",
    }
    if len(metrics) > 1:
        series = []
        for index, item in enumerate(metrics):
            labels, values, field, x_values = _series_for(item["kind"])
            series_unit = units[index] if index < len(units) else ""
            if not series_unit:
                series_unit = _CHART_Y_FIELD_UNITS.get(field, "")
            series.append(
                {
                    "metricKey": item["metricKey"],
                    "label": item["label"],
                    "kind": item["kind"],
                    "unit": series_unit,
                    "yField": field,
                    "yValues": values,
                    "points": [
                        {x_field: x_value, field: value}
                        for x_value, value in zip(x_values, values)
                    ],
                }
            )
        payload["series"] = series
    return payload


def _window_hours(buckets: list[_Bucket]) -> float:
    if not buckets:
        return 1.0
    return max(_hours_between(b.start, b.end) for b in buckets)


def _data_quality_payload(base: _Base) -> dict[str, Any]:
    """数据质量载荷：取窗口内最后一天的快照质量（确定性）。"""
    quality: dict[str, Any] = {}
    if base.days:
        quality = base.days[-1].snapshot.quality or {}
    return {
        "coverage": quality.get("coverage"),
        "missing": quality.get("missing"),
        "duplicates": quality.get("duplicates", 0),
        "freshnessSeconds": quality.get("freshnessSeconds"),
        "dataSource": "simulated",
    }


def _analysis_payload(
    region: dict[str, Any],
    *,
    base: _Base,
    buckets: list[_Bucket],
    ctx: _Context,
) -> dict[str, Any]:
    """分析段：只说这份模拟数据里已经算出来的量。"""
    component = region.get("component") or {}
    facts = component.get("facts") or []
    parts = [
        f"窗口 {base.window_label} 的总耗电为 {base.total_kwh} kWh，"
        f"按电价 {SIMULATED_ELECTRICITY_PRICE_PER_KWH} 元/kWh 折算约为 "
        f"{round(base.total_kwh * SIMULATED_ELECTRICITY_PRICE_PER_KWH, 2)} 元。"
    ]
    if base.devices:
        top = base.devices[0]
        parts.append(f"其中 {top['deviceName']} 耗电最高，为 {top['energyKwh']} kWh。")
    if buckets:
        peak = max(buckets, key=lambda b: b.energy)
        parts.append(
            f"耗电峰值出现在 {peak.start.strftime('%m-%d %H:%M')}，"
            f"该时段耗电 {peak.energy} kWh。"
        )
    return {
        "goal": component.get("goal", "基于数据给出一段解读"),
        "analysis": "".join(parts),
        "evidenceRefs": [
            {"name": f.get("name"), "outputRef": f.get("outputRef")} for f in facts
        ],
        "dataSource": "simulated",
    }


def _text_payload(
    region: dict[str, Any], ctx: _Context, room_name: str
) -> dict[str, Any]:
    """说明区：把占位符换成模拟口径的说法。"""
    component = region.get("component") or {}
    template = str((component.get("content") or {}).get("template") or "")
    rendered = template.replace("{{roomName}}", room_name).replace(
        "{{logicalDate}}", ctx.start.astimezone(ctx.tz).date().isoformat()
    )
    return {"rendered": rendered, "template": template, "dataSource": "simulated"}


# ---------------------------------------------------------------- 公开入口


def generate_simulated_data(
    definition: dict[str, Any], preview_context: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """按定义与冻结的时间窗生成**区域模拟数据**（只在内存里）。

    返回 ``{regionId: payload}``，覆盖所有**非标题**区域。``payload`` 就是
    既有公开预览接口该 componentType 的数据对象：

    - ``METRIC_CARD`` → ``{label, value, unit, precision, yoyPercent,
      comparisonAvailable, windowLabel, dataSource, metrics[]}``
    - ``TABLE`` → ``{columns[], rowNumber, rows[], dataSource}``
    - ``CHART`` → ``{chartKind, grain, xField, yField, unit, xLabels[], yValues[],
      points[], series[]?, dataSource}``
    - ``DATA_QUALITY`` → ``{coverage, missing, duplicates, freshnessSeconds}``
    - ``ANALYSIS`` → ``{goal, analysis, evidenceRefs[]}``
    - ``TEXT`` → ``{rendered, template}``

    结构非法或粒度无法解析时抛 :class:`SimulatedDataError`。
    """
    regions = definition.get("regions")
    if not isinstance(regions, list):
        raise SimulatedDataError(
            "INVALID_DEFINITION", "报表定义缺少 regions 列表。"
        )

    ctx = _build_context(definition, preview_context)
    base = _build_base(definition, ctx)
    room_name = resolve_room_name(room_id_of_definition(definition))
    buckets = _buckets_for_grain(ctx, base, ctx.grain)
    powers = _bucket_powers(base, buckets, ctx.seed_scope)
    yoy = round(hash_float(f"yoy/{ctx.seed_scope}", -8.0, 20.0), 1)

    result: dict[str, dict[str, Any]] = {}
    for region in regions:
        if not isinstance(region, dict):
            raise SimulatedDataError(
                "INVALID_DEFINITION", "regions 里存在不是对象的区域定义。"
            )
        region_id = str(region.get("id") or "")
        if not region_id:
            raise SimulatedDataError(
                "INVALID_DEFINITION", "存在没有 id 的区域定义。"
            )
        role = str(region.get("role") or "").upper()
        if role in TITLE_ROLES:
            # 标题区域不产出数据载荷。
            continue
        component = region.get("component") or {}
        ctype = str(component.get("type") or "")
        spec = _preview_spec(region)
        kind = _resolve_kind(region, spec)
        metrics = _resolve_metrics(region, spec, kind)
        region_grain = _region_grain(region) or ctx.grain
        region_buckets = (
            buckets if region_grain == ctx.grain else _buckets_for_grain(ctx, base, region_grain)
        )
        region_powers = (
            powers if region_grain == ctx.grain
            else _bucket_powers(base, region_buckets, ctx.seed_scope)
        )

        if ctype == "METRIC_CARD":
            payload = _metric_card_payload(
                region, spec, kind, metrics,
                base=base, buckets=region_buckets, powers=region_powers,
                ctx=ctx, yoy=yoy,
            )
        elif ctype == "TABLE":
            payload = _table_payload(
                region, spec, kind, base=base, buckets=region_buckets, ctx=ctx,
            )
        elif ctype == "CHART":
            payload = _chart_payload(
                region, spec, kind, metrics,
                base=base, buckets=region_buckets, powers=region_powers, ctx=ctx,
            )
        elif ctype == "DATA_QUALITY":
            payload = _data_quality_payload(base)
        elif ctype == "ANALYSIS":
            payload = _analysis_payload(region, base=base, buckets=region_buckets, ctx=ctx)
        elif ctype == "TEXT":
            payload = _text_payload(region, ctx, room_name)
        else:
            raise SimulatedDataError(
                "UNSUPPORTED_COMPONENT",
                f"区域 {region_id!r} 的组件类型 {ctype!r} 无法生成模拟数据。",
            )
        result[region_id] = payload
    return result
