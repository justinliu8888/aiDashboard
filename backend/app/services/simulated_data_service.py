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

**确定性（种子材料）**
规格「按对象、逻辑日期、**区域**与字段组合哈希生成」：种子材料 =
报表级（定义 + 定义版本 + 生成器版本 + 时间窗）+ **区域标识** + 对象 +
逻辑日期 + 字段。区域级噪声（告警行、机柜行、趋势点、通用行、PUE、功率桶、
指标卡示例值）都带区域标识 —— 同一份定义里两个板块的同类指标不撞值。

同比（``yoyPercent``）**例外**：它按**量族**给，不按区域给 ——
同一个量的同比跨区域共享；总电费与总耗电是同一个量族（能耗），必然同一个同比，
否则同一份报表内部自相矛盾。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.data.machine_room_energy import METRIC_DEVICE_ENERGY, METRIC_TOTAL_ENERGY
from app.schemas.edit_plan import (
    DISPLAY_MODE_PLACEHOLDER_ONLY,
    DISPLAY_MODE_SIMULATED,
)
from app.services.data_source_mode import (
    SOURCE_MODE_KEY,
    SOURCE_MODE_USER_JSON,
    SourceMode,
    source_mode_of_definition,
    source_mode_of_region,
)
from app.services.chart_dimension_service import (
    canonical_field_of,
    is_device_dimension,
)
from app.services.display_binding_service import simulated_field
from app.services.edit_time_resolver import DEFAULT_TIMEZONE
from app.services.mock_data_service import (
    ROOM_A_DEVICES,
    build_snapshot,
    definition_timezone,
    hash_float,
    resolve_room_name,
    room_id_of_definition,
)
from app.services.report_presentation_service import (
    format_display_value,
    region_authoritative_format,
)
from app.services.simulated_field_semantics import (
    BINDING_KIND_SIMULATED,
    SEMANTIC_KIND_ALARMS,
    SEMANTIC_KIND_CABINET_STATUS,
    SEMANTIC_KIND_COST,
    SEMANTIC_KIND_ENERGY,
    SEMANTIC_KIND_GENERIC,
    SEMANTIC_KIND_POWER,
    SEMANTIC_KIND_PUE,
    VALUE_TYPE_COUNT,
    VALUE_TYPE_COST,
    VALUE_TYPE_CURRENT,
    VALUE_TYPE_DURATION,
    VALUE_TYPE_ENERGY,
    VALUE_TYPE_PERCENT,
    VALUE_TYPE_POWER,
    VALUE_TYPE_TEMPERATURE,
    VALUE_TYPE_VOLTAGE,
    description_of,
    describe_simulated_metric,
    metric_family_seed_key,
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
#:
#: 语义类别集中在 ``simulated_field_semantics``（模拟字段描述契约），
#: 这里只是**转出**同一组常量，方便本模块与外部按旧名字引用。
KIND_ENERGY = SEMANTIC_KIND_ENERGY
KIND_COST = SEMANTIC_KIND_COST
KIND_PUE = SEMANTIC_KIND_PUE
KIND_POWER = SEMANTIC_KIND_POWER
KIND_ALARMS = SEMANTIC_KIND_ALARMS
KIND_CABINET_STATUS = SEMANTIC_KIND_CABINET_STATUS
KIND_GENERIC = SEMANTIC_KIND_GENERIC

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

#: 语义未声明的列，造数器给的是**占位**。占位文案必须让人一眼看出这是
#: 占位、不是业务数（契约表 §3.4 的 ⑤ 级：语义未声明 → 明确标注）。
PLACEHOLDER_VALUE_SUFFIX = "（口径待确认）"

# ---------------------------------------------------------------- 展示模式
#
# 列上带 ``displayMode``：这一列**怎么展示**。
#
# 取值域只有两个，**唯一出处**是 ``app.schemas.edit_plan``（编辑层写、
# 这里读）：``simulated`` = 照常出模拟数值（缺省）；``placeholder_only`` =
# 只显示名称、**不显示数值**（用户明确要求「先占位 / 先加标签」时才用）。
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

    def identity_seed_scope(self, identity: str) -> str:
        """区域级种子材料：报表级种子 **+ 数据身份**。

        规格「确定性：按对象、逻辑日期、**区域**与字段组合哈希生成」——
        区域（报表板块）也是种子材料的一部分：同一份定义里两个板块的同类
        指标不能撞出同一个值（趋势、告警、机柜、PUE、功率……）。

        报表级前缀（定义 + 定义版本 + 生成器版本 + 时间窗）**原样保留**，
        所以两份不同方案仍然各自一套噪声，不会共用同一批数。

        **这里放的是"数据身份"而不是"展示 id"**（契约 0002 §2）：
        数据身份 = ``previewSpec.simulationIdentity``，老卡片缺这个字段时
        回退成它原来的区域 id —— 所以老卡片的数值与改造前**逐字节一致**。

        由此得到两条强约束：

        - 移动 / 成组 / 改样式 → 身份不变 → 数值不漂；
        - 复制 → 展示 id 换新的、身份沿用源的 → 复制件与源件数值一致。
        """
        return f"{self.seed_scope}|{identity}"

    def region_seed_scope(self, region_id: str) -> str:
        """区域级种子材料的**旧名**：按区域 id 取。

        等价于 :meth:`identity_seed_scope`。新代码请直接用
        :meth:`identity_seed_scope` 并显式给数据身份。
        """
        return self.identity_seed_scope(region_id)


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
    """把粒度归一成受支持的取值；明确给了非法值就报错。

    **不支持的粒度必须明确失败**（而不是悄悄换成天）：方案里按周 / 按月
    是用户确认过的口径，造不出来就要说清楚造不出来、为什么、改成什么。
    """
    if value is None or str(value).strip() == "":
        return None
    grain = str(value).strip().upper()
    if grain not in SUPPORTED_GRAINS:
        raise SimulatedDataError(
            "UNRESOLVED_GRAIN",
            f"无法解析的时间粒度：{value!r}；受支持的取值为 {' / '.join(SUPPORTED_GRAINS)}。"
            "示意数据只能按这几个粒度生成，请把方案里的时间粒度改成其中之一后重试。",
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


#: 容器组件类型：容器**不造数**、**不进载荷**，它只是排版与阅读顺序的组织者。
CONTAINER_COMPONENT_TYPE = "CONTAINER"


def is_container_region(region: dict[str, Any]) -> bool:
    """这个区域是不是**容器**（分组）。

    容器只管排列与阅读顺序：它没有数据、没有统计口径，
    因此造数与载荷两层都**跳过它**，只递归它的 ``children``。
    """
    component = region.get("component") or {}
    if not isinstance(component, dict):
        return False
    return str(component.get("type") or "").upper() == CONTAINER_COMPONENT_TYPE


def effective_simulation_identity(region: dict[str, Any]) -> str:
    """这个区域的**数据身份**（契约 0002 §2）。

    新身份 = ``templateBinding.previewSpec.simulationIdentity``；
    老卡片没有这个字段 → **回退成它原来的区域 id**，
    所以老卡片的种子与改造前完全一致（数值不漂）。

    **绑定层的 ``templateBinding.simulationIdentity`` 是第二个出处**：
    复制件把源身份写在绑定层（**不碰** ``previewSpec``）——
    老卡片被复制时不必凭空造一个空壳 ``previewSpec``（空壳会让
    ``adapt_preview_spec`` 的回退路径绕过去 → 整批丢数），
    复制件与源件读到的却是**同一个身份** → 数值一致。

    展示 id（``region["id"]``）与数据身份**分开**：
    复制件换展示 id、沿用数据身份 → 复制件与源件数值一致。
    """
    binding = region.get("templateBinding") or {}
    if not isinstance(binding, dict):
        binding = {}
    identity = str(_preview_spec(region).get("simulationIdentity") or "").strip()
    if not identity:
        identity = str(binding.get("simulationIdentity") or "").strip()
    if identity:
        return identity
    return str(region.get("id") or "")


def _column_formats(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """表格载荷里**每一列自己的格式**：``{fieldRef: format}``。

    显示串**按列自己的格式优先**，列没声明才回落到区域格式 ——
    区域级设了 ``DATE`` 之后，同区域的数值列不该也被按日期渲染
    （那不是显示，是语义错）。
    """
    formats: dict[str, dict[str, Any]] = {}
    columns = payload.get("columns")
    if not isinstance(columns, list):
        return formats
    for column in columns:
        if not isinstance(column, dict):
            continue
        field_ref = str(column.get("fieldRef") or "").strip()
        fmt = column.get("format")
        if not field_ref or not isinstance(fmt, dict) or not fmt:
            continue
        formats[field_ref] = fmt
    return formats


def _display_cell(
    value: Any, region_format: dict[str, Any], column_format: dict[str, Any]
) -> Any:
    """一格的显示串：**列级优先**，列没声明的键回落到区域级。

    两边都没有格式 → **原值**（这一列没有格式约定，就显示原值，
    不替它编一个显示串）。
    """
    effective = {**region_format, **column_format}
    if not effective:
        return value
    return format_display_value(value, effective)


def _item_format(region_format: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """区域格式落到**某一个指标项**上时用的那份格式。

    区域格式里的 ``unitLabel`` 是**这张卡**的单位，不该顺手改掉
    **别的指标项**的单位：指标卡上除主指标外还有别的指标（卡片追加过
    指标时清单里有好几项），把主指标的单位（台）写到 kWh 那一项头上，
    显示出来就是「3,033 台」这种不像话的数字。

    所以：**这一项自己声明了单位**时，区域格式的单位标签让位给这一项的
    单位；其余显示约定（精度、千分位）照旧由区域格式说了算。
    """
    item_unit = str(item.get("unit") or "").strip()
    label = str(region_format.get("unitLabel") or "").strip()
    if not item_unit or not label or label == item_unit:
        return region_format
    return {key: value for key, value in region_format.items() if key != "unitLabel"}


def _attach_authoritative_format(
    payload: dict[str, Any], region: dict[str, Any]
) -> dict[str, Any]:
    """把该区域的**权威格式**挂进载荷，并按它算出显示串。

    契约 0002 §3.5：显示格式走**权威格式**，
    **不通过改数据精度或改统计结果实现外观格式** —— 所以：

    - ``payload["format"]`` = 该区域的权威格式（``SET_FORMAT`` 写的那一份）；
    - ``payload["value"] / rows / points`` **一个字节都不动**（底层值原样）；
    - 显示串另开字段：``displayValue``（指标卡主值与逐项指标）、
      ``displayRows``（表格逐格）、``displayXLabels``（图表横轴）。

    **表格逐格按列自己的格式优先**（列没声明才回落到区域格式）：
    区域级是兜底，列级才是这一列的 —— 否则区域级一设 ``DATE``，
    同区域的数值列也跟着被按日期渲染。

    **区域没有格式时，列自己的格式照样生效**（第三轮 R07）：
    列级格式是这一列**自己**声明的，不该因为区域级是空的就整批不算 ——
    否则页面显示裸 ISO 串 / 裸数值。
    区域级与列级**都没有**时：不产出显示串（与改造前一致）。

    没有格式的区域照样带 ``format``（空对象）—— 载荷结构是**封闭**的，
    前端按同一个字段读，不用猜有没有。
    """
    fmt = region_authoritative_format(region)
    payload["format"] = fmt

    # 指标卡与图表横轴**只有区域级**格式可用（没有列的概念）。
    if fmt:
        if "value" in payload:
            payload["displayValue"] = format_display_value(
                payload.get("value"), fmt, unit=str(payload.get("unit") or "")
            )
        metrics = payload.get("metrics")
        if isinstance(metrics, list):
            for item in metrics:
                if isinstance(item, dict) and "value" in item:
                    item["displayValue"] = format_display_value(
                        item.get("value"),
                        _item_format(fmt, item),
                        unit=str(item.get("unit") or ""),
                    )
    rows = payload.get("rows")
    if isinstance(rows, list):
        column_formats = _column_formats(payload)
        # 有区域格式**或**有列格式，才算显示串（两边都没有 = 不产出）。
        if fmt or column_formats:
            payload["displayRows"] = [
                {
                    str(key): _display_cell(
                        value, fmt, column_formats.get(str(key), {})
                    )
                    for key, value in row.items()
                }
                for row in rows
                if isinstance(row, dict)
            ]
    x_field = str(payload.get("xField") or "")
    points = payload.get("points")
    if fmt and x_field and isinstance(points, list):
        payload["displayXLabels"] = [
            format_display_value(point.get(x_field), fmt)
            for point in points
            if isinstance(point, dict)
        ]
    return payload


def _iter_leaf_regions(regions: Any) -> Any:
    """按阅读顺序产出**要造数的叶子区域**。

    容器（分组）本身跳过，递归它的 ``children``；老平面定义没有
    ``children`` 时，产出结果与改造前**逐条一致**。
    """
    if not isinstance(regions, list):
        return
    for region in regions:
        if not isinstance(region, dict):
            raise SimulatedDataError(
                "INVALID_DEFINITION", "regions 里存在不是对象的区域定义。"
            )
        if is_container_region(region):
            yield from _iter_leaf_regions(region.get("children"))
            continue
        yield region


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


def _region_primary_metric_ref(region: dict[str, Any]) -> str:
    """区域**主指标**绑的目录指标引用（数据管道里的 METRIC 节点）。

    目录内指标的清单项 ``metricKey`` 就是这个引用 —— 靠它就能认出
    「清单里的哪一项是主指标」，不靠位置。
    """
    nodes = ((region.get("data") or {}).get("pipeline") or {}).get("nodes") or []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("type") or "").upper() == "METRIC":
            return str(node.get("metricRef") or "")
    return ""


def _primary_metric_entry(
    region: dict[str, Any], spec: dict[str, Any], kind: str,
) -> dict[str, Any] | None:
    """老式指标卡的**主指标**（``component.metric``）在指标清单里的那一条。

    卡片被**追加过指标**之后，``previewSpec.metrics[]`` 里只有追加的那些，
    主指标不在清单里 —— 载荷就会把追加的那一项当成主指标来渲染，
    单位 / 字段 / 数值全被主指标顶掉（反馈 F02-04：新增的「总告警数」
    显示成 ``5.0 kWh``）。

    所以这里把主指标**按它自己的身份**补成清单的第一条。

    * **只有真绑了字段的单指标卡才有主指标**（``component.metric.fieldRef``）：
      多指标卡里 ``component.metric.label`` 是**卡片标题**、不是某一项指标，
      按它补会补出一条重复项；
    * 「能力待接通」**必须带原因**：原因说不出来就不标（契约：标了就要说得清
      为什么）。
    """
    metric = (region.get("component") or {}).get("metric") or {}
    if not isinstance(metric, dict) or not metric:
        return None
    field_ref = str(metric.get("fieldRef") or "")
    if not field_ref:
        # 没绑字段：认不出主指标是哪一项 —— 不补（宁可少补，也不能造重复项）。
        return None
    label = str(metric.get("label") or "")
    metric_id = str(metric.get("id") or "")
    pending_reason = str(spec.get("pendingReason") or "")
    return {
        "metricKey": kind,
        "label": label or str(region.get("name") or "指标"),
        "kind": kind,
        "unit": str(
            spec.get("unit") or (metric.get("format") or {}).get("unitLabel") or ""
        ),
        "capabilityPending": bool(spec.get("capabilityPending")) and bool(pending_reason),
        "pendingReason": pending_reason or None,
        # **身份**：主指标是谁，靠这几个字段说清 —— 载荷层按它认主指标，
        # 不按「清单里的第一项」猜。
        "id": metric_id,
        "fieldRef": field_ref,
        "metricRef": _region_primary_metric_ref(region),
    }


def _entry_is_primary(entry: dict[str, Any], primary: dict[str, Any]) -> bool:
    """清单里的这一项是不是就是**主指标**那一条（按身份，不按位置）。"""
    if not isinstance(entry, dict) or not isinstance(primary, dict):
        return False
    primary_field = str(primary.get("fieldRef") or "")
    primary_id = str(primary.get("id") or "")
    primary_label = str(primary.get("label") or "")
    primary_key = str(primary.get("metricKey") or "")
    primary_ref = str(primary.get("metricRef") or "")
    if primary_field and (
        str(entry.get("fieldRef") or "") == primary_field
        or str(entry.get("metricKey") or "") == primary_field
    ):
        return True
    if primary_id and str(entry.get("id") or "") == primary_id:
        return True
    if primary_label and str(entry.get("label") or "") == primary_label:
        return True
    # 目录内指标：清单项的 ``metricKey`` 就是目录指标引用。
    if primary_ref and str(entry.get("metricKey") or "") == primary_ref:
        return True
    # 老式形状：主指标在清单里的 ``metricKey`` 就是区域量身份。
    if primary_key and str(entry.get("metricKey") or "") == primary_key:
        return True
    return False


def _resolve_metrics(
    region: dict[str, Any],
    spec: dict[str, Any],
    kind: str,
    *,
    columns: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """区域的指标清单（多指标消费 ``metrics[]``，不是只看主量）。

    描述侧的三个字段（``bindingKind`` / ``simulatedId`` /
    ``deterministicScope``）**原样带过来** —— 载荷层照着它们说清「这一项
    是目录内指标还是目录外模拟」。

    **合并**（与列同一套口径）：

    - ``previewSpec.metrics[]`` 是**底**；
    - 卡片的老式**主指标**（``component.metric``）**也要在清单里** ——
      卡片被追加过指标时清单里只有追加的那些，不补回去的话主指标会被
      挤掉，追加的那一项反而继承主指标的单位 / 字段（反馈 F02-04）；
    - 列里声明了、清单里**还没有**的**目录外指标**追加进来 ——
      目录外指标也要出现在 ``metrics[]`` 里，不能只活在列上；
    - 占位模式（``displayMode = "placeholder_only"``）的指标标上
      ``placeholderOnly``：只显示名称，**不显示数值**。
    """
    raw_metrics = spec.get("metrics") or []
    metrics: list[dict[str, Any]] = []
    for item in raw_metrics:
        if not isinstance(item, dict):
            continue
        metric_kind = str(item.get("kind") or "").strip().lower()
        if metric_kind not in KNOWN_KINDS:
            metric_kind = kind
        resolved = {
            "metricKey": str(item.get("metricKey") or metric_kind),
            "label": str(item.get("label") or region.get("name") or "指标"),
            "kind": metric_kind,
            "unit": str((item.get("unit") if "unit" in item else spec.get("unit")) or ""),
            "capabilityPending": bool(item.get("capabilityPending")),
            "pendingReason": item.get("pendingReason"),
        }
        for key in ("bindingKind", "simulatedId", "deterministicScope", "displayMode"):
            if key in item:
                resolved[key] = item[key]
        metrics.append(resolved)
    primary_entry = _primary_metric_entry(region, spec, kind)
    if not metrics:
        metrics = [
            {
                "metricKey": kind,
                "label": str((region.get("component") or {}).get("metric", {}).get("label")
                             or region.get("name") or "指标"),
                "kind": kind,
                "unit": str(
                    spec.get("unit")
                    or ((region.get("component") or {}).get("metric", {}).get("format") or {}).get("unitLabel")
                    or ""
                ),
                "capabilityPending": bool(spec.get("capabilityPending")),
                "pendingReason": spec.get("pendingReason"),
            }
        ]
    elif primary_entry is not None:
        matched = [item for item in metrics if _entry_is_primary(item, primary_entry)]
        if matched:
            # 清单里那一条**就是**主指标 → 名字与单位按**定义**说了算。
            #
            # ``previewSpec.metrics[]`` 是**目录快照**：卡片改过名字之后
            # （改名 / 换绑），快照里的标签就过期了。前端渲染的是这份清单
            # （``metrics[]`` 优先于载荷顶层的 ``label``），不刷新的话
            # **卡片上还是旧名字** —— 现场实证：定义里
            # ``component.metric.label`` 已经是「耗电格式验证」，页面上的
            # 指标卡却仍显示「总耗电」。
            for item in matched:
                if primary_entry.get("label"):
                    item["label"] = primary_entry["label"]
                if primary_entry.get("unit"):
                    item["unit"] = primary_entry["unit"]
        else:
            # 主指标不在清单里（卡片被追加过指标）→ **按身份补回去**，
            # 放第一条：主指标仍是卡片的主量。
            metrics = [primary_entry] + metrics
    region_columns = columns if columns is not None else _columns_of(region, spec)
    metrics.extend(_column_metrics(region_columns, metrics))
    for item in metrics:
        item["placeholderOnly"] = _is_placeholder_metric(item, region_columns)
    return metrics


def _column_metrics(
    columns: list[dict[str, Any]], metrics: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """列里声明的、**指标清单里还没有**的目录外指标 → 追加进清单。

    只收**目录外**的列（``bindingKind = "simulated"``）：维度列（设备名称、
    时间……）不是指标，进了清单会凭空多出一项。
    """
    extra: list[dict[str, Any]] = []
    for column in columns:
        if str(column.get("bindingKind") or "") != BINDING_KIND_SIMULATED:
            continue
        field_ref = str(column.get("fieldRef") or "").strip()
        label = str(column.get("label") or "").strip()
        if not field_ref and not label:
            continue
        if any(_metric_matches_column(item, column) for item in metrics):
            continue
        extra.append(
            {
                "metricKey": field_ref or label,
                "label": label or field_ref,
                "kind": str(column.get("semanticKind") or KIND_GENERIC),
                "unit": _column_unit(column),
                "capabilityPending": bool(column.get("capabilityPending")),
                "pendingReason": None,
                "bindingKind": BINDING_KIND_SIMULATED,
            }
        )
    return extra


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


# ---------------------------------------------------------------- 量族与同比


def _metric_family(kind: str, metric_key: str, unit: str) -> str:
    """量族：这个指标是**由哪个基础量算出来的**。

    同比（``yoyPercent``）按量族给，不是按区域给 —— 同一个量的同比只能有
    一个，否则同一份报表内部自相矛盾。

    判据集中在 ``simulated_field_semantics.metric_family_seed_key``（**全仓库
    只有这一处来源**）。

    **与描述里的 ``deterministicScope`` 的关系（如实记录，别当成约束）**：
    描述里的 ``deterministicScope`` **记录**该字段的预期作用域；当前造数器
    **按区域 / 量族自行推导**种子，**不由本字段决定** —— 也就是说，本函数
    返回的量族**不读**描述里的 ``seedKey``。两侧目前对得上，是靠「写入侧
    与本函数用同一套判据」保证的，不是靠字段本身约束的。
    """
    return metric_family_seed_key(kind, metric_key, unit)


def _yoy_for(ctx: _Context, family: str) -> float:
    """某个量族的同比百分比。

    种子**不含区域**：同一个量族（哪怕落在不同板块）给出同一个同比 ——
    同一个量的同比只有一个。不同量族才各给各的。
    """
    return round(hash_float(f"yoy/{ctx.seed_scope}/{family}", -8.0, 20.0), 1)


# ---------------------------------------------------------------- 列语义


def _column_value_types(columns: list[dict[str, Any]]) -> list[str]:
    """每一列的**值形态** —— 按**描述**读，不按列名或标题猜。

    描述落在 ``previewSpec.columns[]``（转换层写、这里读）。历史定义里没有
    描述时，按**字段身份 / 声明单位**解析一份（``description_of``）——
    仍然不看显示名里有没有「耗电」「率」这种字样。

    ADR 0004 §5.4：表格列必须来自方案声明，系统**不得按标题猜**。
    """
    value_types: list[str] = []
    for column in columns:
        description = description_of(column)
        value_types.append(description.valueType)
    return value_types


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
        # 语义没解析出来的列给的是**占位**，不是业务数。占位文案必须说清
        # 自己是占位（契约表 §3.4 的 ⑤ 级：语义未声明 → 明确标注），
        # 用户不能把「示例值 11」当成真的业务数值。
        return f"示例值 {int(hash_float(seed, 1, 99))}{PLACEHOLDER_VALUE_SUFFIX}"
    return round(hash_float(seed, 10.0, 500.0), 1)


#: 金额单位各自的**取值区间**（示意值）。每个单位用自己的量级 ——
#: 「万元」不是「元」除以一万的那种写法，是**另一个量纲**。
_MONEY_RANGES: dict[str, tuple[float, float]] = {
    "元": (500.0, 3600.0),
    "千元": (5.0, 36.0),
    "万元": (0.5, 3.6),
    "亿元": (0.05, 0.36),
}

#: 计数单位（目录外指标按单位识别出「这是个计数」时用它）。
_COUNT_UNITS: frozenset[str] = frozenset(
    {"个", "台", "条", "次", "件", "套", "只", "人", "人次", "户"}
)


def _money_range(unit: str) -> tuple[float, float] | None:
    """金额单位 → 它的取值区间；不是金额单位就是 ``None``。"""
    text = str(unit or "").strip()
    if text in _MONEY_RANGES:
        return _MONEY_RANGES[text]
    if text.lower() in ("cny", "rmb"):
        return _MONEY_RANGES["元"]
    return None


def _simulated_value(value_type: str, unit: str, seed: str) -> float:
    """目录外列的**通用示意数值**。

    认得出量纲就按量纲给一个量级合理的数（% → 0~100、kWh → 百位、
    计数 → 整数、金额 → 按单位量级），认不出就给一个通用数值 + 空单位。

    **不编造业务含义**：这是「这一列长什么样」的示意，不是业务结论。

    列上记的 ``valueType`` 是**口径判定**的结果（目录外列认不出口径 →
    ``text``）；**值本身**按单位给一个示意数值 —— 认不出口径不等于没有值
    （值形态取值域里没有「通用数值」这一项，所以按单位定形状）。
    """
    text = str(unit or "").strip()
    if value_type == VALUE_TYPE_PERCENT:
        return round(hash_float(f"simulated/{seed}", 8.0, 92.0), 1)
    if value_type == VALUE_TYPE_COUNT:
        return float(int(hash_float(f"simulated/{seed}", 1, 20)))
    if value_type == VALUE_TYPE_ENERGY:
        return round(hash_float(f"simulated/{seed}", 800.0, 5200.0), 1)
    if value_type == VALUE_TYPE_POWER:
        return round(hash_float(f"simulated/{seed}", 20.0, 400.0), 1)
    if value_type == VALUE_TYPE_COST:
        low, high = _money_range(text) or _MONEY_RANGES["元"]
        return round(hash_float(f"simulated/{seed}", low, high), 2)
    if value_type == VALUE_TYPE_TEMPERATURE:
        return round(hash_float(f"simulated/{seed}", 18.0, 29.0), 1)
    if value_type == VALUE_TYPE_VOLTAGE:
        return round(hash_float(f"simulated/{seed}", 218.0, 242.0), 1)
    if value_type == VALUE_TYPE_CURRENT:
        return round(hash_float(f"simulated/{seed}", 3.0, 42.0), 1)
    if value_type == VALUE_TYPE_DURATION:
        return round(hash_float(f"simulated/{seed}", 1.0, 120.0), 1)
    # 单位本身就说明了量纲（值形态没认出来时按单位再判一次）。
    if text in _COUNT_UNITS:
        return float(int(hash_float(f"simulated/{seed}", 1, 20)))
    if text in ("%", "％"):
        return round(hash_float(f"simulated/{seed}", 8.0, 92.0), 1)
    if text.lower() in ("kwh", "mwh", "wh"):
        return round(hash_float(f"simulated/{seed}", 800.0, 5200.0), 1)
    if text.lower() in ("kw", "mw", "w"):
        return round(hash_float(f"simulated/{seed}", 20.0, 400.0), 1)
    money = _money_range(text)
    if money is not None:
        return round(hash_float(f"simulated/{seed}", money[0], money[1]), 2)
    if text in ("℃", "°C", "°c"):
        return round(hash_float(f"simulated/{seed}", 18.0, 29.0), 1)
    if text in ("V", "v"):
        return round(hash_float(f"simulated/{seed}", 218.0, 242.0), 1)
    if text in ("A", "a"):
        return round(hash_float(f"simulated/{seed}", 3.0, 42.0), 1)
    if text in ("分钟", "min", "小时", "h"):
        return round(hash_float(f"simulated/{seed}", 1.0, 120.0), 1)
    # 认不出量纲 → **通用数值 + 空单位**（不是「不支持」）。
    return round(hash_float(f"simulated/{seed}", 10.0, 500.0), 1)


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
    # 目录外指标按**单位**给量级 —— 认得出量纲就按量纲给，认不出给通用数值。
    if unit in ("kWh", "kwh"):
        return round(hash_float(f"generic/{seed}", 800.0, 5200.0), 1)
    money = _money_range(unit)
    if money is not None:
        return round(hash_float(f"generic/{seed}", money[0], money[1]), 2)
    if unit in ("%", "％"):
        return round(hash_float(f"generic/{seed}", 10.0, 90.0), 1)
    if unit in _COUNT_UNITS:
        return float(int(hash_float(f"generic/{seed}", 1, 20)))
    if unit in ("℃", "°C", "°c"):
        return round(hash_float(f"generic/{seed}", 18.0, 29.0), 1)
    if unit in ("V", "v"):
        return round(hash_float(f"generic/{seed}", 218.0, 242.0), 1)
    if unit in ("A", "a"):
        return round(hash_float(f"generic/{seed}", 3.0, 42.0), 1)
    if unit in ("分钟", "min", "小时", "h"):
        return round(hash_float(f"generic/{seed}", 1.0, 120.0), 1)
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
    seed_scope: str,
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
    primary = next((item for item in metrics if item["kind"] == kind), None)
    # Keep the confirmed order of the remaining metrics, rather than grouping by kind.
    ordered = ([primary] + [item for item in metrics if item is not primary]) if primary else metrics
    # 同比按**主量的量族**给：总耗电与总电费同族 → 同一个同比；
    # PUE / 实时功率 / 告警各自一族 → 各自的同比。
    primary_metric = ordered[0] if ordered else None
    # 占位模式：主量只显示名称 → 卡片**不出大数字、不出趋势**。
    primary_is_placeholder = bool(primary_metric and primary_metric.get("placeholderOnly"))
    yoy = _yoy_for(
        ctx,
        _metric_family(
            str((primary_metric or {}).get("kind") or kind),
            str((primary_metric or {}).get("metricKey") or kind),
            str((primary_metric or {}).get("unit") or ""),
        ),
    )
    for item in ordered:
        precision = declared_precision
        if precision is None:
            precision = 2 if item["kind"] in (KIND_COST, KIND_PUE) else 1
        if item["kind"] == KIND_COST:
            # 电费按 2 位小数：总耗电（1 位）× 单价（1 位）= 2 位，两个卡片严格相等。
            precision = max(int(precision), 2)
        if item.get("placeholderOnly"):
            # 只显示名称：这一项**不带数值**（前端渲染成「—」）。
            metric_payload = {
                "metricKey": item["metricKey"],
                "label": item["label"],
                "kind": item["kind"],
                "unit": item.get("unit") or "",
                "precision": int(precision),
                "placeholderOnly": True,
                "capabilityPending": bool(item["capabilityPending"]),
                "pendingReason": item.get("pendingReason"),
            }
            for key in ("bindingKind", "simulatedId"):
                if item.get(key) is not None:
                    metric_payload[key] = item[key]
            metric_payloads.append(metric_payload)
            continue
        value = _metric_value(
            item["kind"],
            base=base,
            buckets=buckets,
            powers=powers,
            seed=f"{seed_scope}|{item['metricKey']}",
            unit=item.get("unit") or "",
        )
        value = round(float(value), int(precision))
        metric_payload = {
            "metricKey": item["metricKey"],
            "label": item["label"],
            "kind": item["kind"],
            "unit": item.get("unit") or "",
            "value": value,
            "precision": int(precision),
            "capabilityPending": bool(item["capabilityPending"]),
            "pendingReason": item.get("pendingReason"),
        }
        # 目录内 / 目录外随载荷带出去：前端与下游据此知道这一项的来源。
        for key in ("bindingKind", "simulatedId"):
            if item.get(key) is not None:
                metric_payload[key] = item[key]
        metric_payloads.append(metric_payload)
        if primary_value is None and not primary_is_placeholder:
            primary_value = value
            primary_unit = item.get("unit") or ""
            primary_precision = int(precision)

    if primary_value is None and not primary_is_placeholder:
        primary_value = 0.0

    unit = primary_unit or _unit_for(kind, spec, str(fmt.get("unitLabel") or ""))
    return {
        "label": str(metric.get("label") or region.get("name") or "指标"),
        "value": None if primary_is_placeholder else primary_value,
        "unit": unit,
        "precision": primary_precision,
        # 占位模式：不出趋势值（前端据 ``yoyPercent`` 为空就不画趋势）。
        "yoyPercent": None if primary_is_placeholder else yoy,
        "comparisonAvailable": not primary_is_placeholder,
        "windowLabel": base.window_label,
        "dataSource": "simulated",
        "metrics": metric_payloads,
    }


def _display_mode_of(column: dict[str, Any]) -> str:
    """列的**展示模式**（缺省 = 照常出数）。"""
    mode = str(column.get("displayMode") or "").strip().lower()
    return mode or DISPLAY_MODE_SIMULATED


def _is_placeholder_only(column: dict[str, Any]) -> bool:
    """这一列是不是**只显示名称**（占位模式）。"""
    return _display_mode_of(column) == DISPLAY_MODE_PLACEHOLDER_ONLY


def _merged_columns(region: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
    """区域的列：``component.columns`` 与 ``previewSpec.columns`` **合并**。

    **第二批起，``component.columns`` 是展示列的定义之源**（协议
    ``TableComponent.columns``）：它决定**列集合与顺序**。
    ``previewSpec.columns`` 退回到**语义描述的来源** —— 它带着
    ``semanticKind`` / ``valueType`` / ``deterministicScope``，但**不再**
    决定哪些列出现、按什么顺序出现。

    合并规则（两边都有时以谁为准是**定死的**）：

    - ``component.columns`` 非空 → **以它为准**（列集合 + 顺序）；
      ``previewSpec`` 里 ``fieldRef`` 对得上的列**只补语义描述**，
      对不上的（历史遗留的旧列）**不再进入展示**。
    - ``component.columns`` 为空（老定义）→ 退回旧口径：以
      ``previewSpec.columns`` 为底，组件列追加在后面 —— 与改造前一致。

    旧行为是「有 previewSpec.columns 就整份丢掉组件列」—— 目录外新增的列
    会连同它的展示值一起消失；**第二批修复的正是这个覆盖风险**：旧
    ``previewSpec`` 的列与顺序不能盖住新定义。
    """
    preview_raw = [
        item for item in (spec.get("columns") or []) if isinstance(item, dict)
    ]
    component_raw = [
        item
        for item in ((region.get("component") or {}).get("columns") or [])
        if isinstance(item, dict)
    ]
    if not component_raw:
        # 老定义：没有组件列 —— 退回旧口径（previewSpec 为底，组件列追加）。
        merged: list[dict[str, Any]] = []
        position_by_ref: dict[str, int] = {}
        for item in preview_raw:
            field_ref = str(item.get("fieldRef") or "").strip()
            if not field_ref:
                continue
            merged.append(dict(item))
            position_by_ref.setdefault(field_ref, len(merged) - 1)
        for item in component_raw:
            field_ref = str(item.get("fieldRef") or "").strip()
            if not field_ref:
                continue
            position = position_by_ref.get(field_ref)
            if position is None:
                merged.append(dict(item))
                position_by_ref[field_ref] = len(merged) - 1
                continue
            target = merged[position]
            for key in ("id", "label", "displayMode", "bindingKind"):
                value = item.get(key)
                if value is None:
                    continue
                if key == "label" and not str(value).strip():
                    continue
                target[key] = value
            component_format = item.get("format")
            if isinstance(component_format, dict) and component_format:
                base_format = target.get("format")
                target["format"] = (
                    {**base_format, **component_format}
                    if isinstance(base_format, dict)
                    else dict(component_format)
                )
        return merged

    # 新定义：组件列说了算（列集合 + 顺序）。
    semantic_by_ref: dict[str, dict[str, Any]] = {}
    for item in preview_raw:
        field_ref = str(item.get("fieldRef") or "").strip()
        if field_ref and field_ref not in semantic_by_ref:
            semantic_by_ref[field_ref] = item
    merged = []
    for item in component_raw:
        field_ref = str(item.get("fieldRef") or "").strip()
        if not field_ref:
            continue
        column = dict(item)
        source = semantic_by_ref.get(field_ref)
        if source:
            # 语义描述跟着 previewSpec 走（组件列没写的那几个键）。
            for key, value in source.items():
                if key in ("id", "label", "format", "displayMode", "bindingKind"):
                    continue
                column.setdefault(key, value)
            component_format = item.get("format")
            base_format = source.get("format")
            if isinstance(base_format, dict) and base_format:
                column["format"] = (
                    {**base_format, **component_format}
                    if isinstance(component_format, dict) and component_format
                    else dict(base_format)
                )
        merged.append(column)
    return merged


def _columns_of(region: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
    """区域声明的列（``previewSpec.columns`` 与 ``component.columns`` 合并后）。

    列的**语义描述**（``semanticKind`` / ``valueType`` / ``deterministicScope``）
    跟着列一起读；历史定义没有描述时按声明事实补一份（见
    :func:`_column_value_types`）。
    """
    columns: list[dict[str, Any]] = []
    for item in _merged_columns(region, spec):
        field_ref = str(item.get("fieldRef") or "").strip()
        if not field_ref:
            continue
        column = {
            "id": item.get("id"),
            "label": item.get("label"),
            "fieldRef": field_ref,
            "format": item.get("format"),
        }
        mode = _display_mode_of(item)
        if mode != DISPLAY_MODE_SIMULATED:
            column["displayMode"] = mode
        binding_kind = str(item.get("bindingKind") or "").strip()
        if binding_kind:
            column["bindingKind"] = binding_kind
        # 已经记着描述就原样带（转换层写的），没记的补一份。
        description = description_of(item)
        column.update(description.as_dict())
        columns.append(column)
    return columns


def _column_unit(column: dict[str, Any]) -> str:
    """列的**有效单位**：声明的单位优先，没有就按显示名**推断量纲**。

    推断走的是通用形态（单位符号 / 计数单位 / 比率后缀），不是按业务词猜。
    """
    declared = str(column.get("unit") or "").strip()
    if not declared:
        fmt = column.get("format")
        if isinstance(fmt, dict):
            declared = str(fmt.get("unitLabel") or "").strip()
    return describe_simulated_metric(
        str(column.get("label") or ""), unit=declared
    ).unit


def _metric_matches_column(metric: dict[str, Any], column: dict[str, Any]) -> bool:
    """这个指标是不是这一列说的那一个。

    判据是**键**（取值键 / 模拟身份 / 显示名），不是「像不像」——
    目录外的列与目录外的指标靠同一个键对上。
    """
    field_ref = str(column.get("fieldRef") or "").strip()
    label = str(column.get("label") or "").strip()
    if not field_ref and not label:
        return False
    for key in ("metricKey", "simulatedId", "label"):
        value = str(metric.get(key) or "").strip()
        if not value:
            continue
        if field_ref and value == field_ref:
            return True
        if label and value == label:
            return True
    return False


def _simulated_column_flags(
    columns: list[dict[str, Any]], metrics: list[dict[str, Any]]
) -> list[bool]:
    """每一列是不是**目录外**（模拟）的列。

    目录外的列要**出数值**：用户要的是「这个新指标长什么样」，
    给一段占位文字不算展示值。判据是**显式标记**：

    1. 列自己标了 ``bindingKind = "simulated"``；
    2. 这一列对得上一个**目录外指标**（模拟绑定的那一项）。

    目录内的列认不出来时仍然是占位文字（契约表 §3.4 的 ⑤ 级不变）。
    """
    simulated_keys: set[str] = set()
    for item in metrics:
        if str(item.get("bindingKind") or "") != BINDING_KIND_SIMULATED:
            continue
        for key in ("metricKey", "simulatedId", "label"):
            value = str(item.get(key) or "").strip()
            if value:
                simulated_keys.add(value)
    flags: list[bool] = []
    for column in columns:
        if str(column.get("bindingKind") or "") == BINDING_KIND_SIMULATED:
            flags.append(True)
            continue
        field_ref = str(column.get("fieldRef") or "").strip()
        label = str(column.get("label") or "").strip()
        flags.append(
            bool((field_ref and field_ref in simulated_keys)
                 or (label and label in simulated_keys))
        )
    return flags


def _is_placeholder_metric(
    metric: dict[str, Any], columns: list[dict[str, Any]]
) -> bool:
    """这个指标是不是**只显示名称**（占位模式）。

    指标自己标了 ``displayMode``，或者它对得上一个占位列 —— 两种都算。
    """
    if str(metric.get("displayMode") or "").strip().lower() == DISPLAY_MODE_PLACEHOLDER_ONLY:
        return True
    return any(
        _is_placeholder_only(column) and _metric_matches_column(metric, column)
        for column in columns
    )


def _row_value(
    value_kind: str,
    *,
    seed: str,
    device: dict[str, Any] | None,
    bucket: _Bucket | None,
    hours: float,
    simulated: bool = False,
    unit: str = "",
) -> Any:
    """一列在一个具体行里的值。"""
    if simulated:
        # 目录外的列：**通用示意数值**（口径待确认也要有数）。
        return _simulated_value(value_kind, unit, seed)
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
    seed_scope: str,
    columns: list[dict[str, Any]] | None = None,
    metrics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """表格载荷：按**列名**生成行，列的 fieldRef 一定能读到单元格。

    - **目录外的列照样出数值**（通用示意值），不跳列、不留空；
    - **占位模式**的列只显示名称：这一格**不写值**（前端渲染成空格子），
      列名照常出现在表头。
    """
    region_columns = columns if columns is not None else _columns_of(region, spec)
    if not region_columns:
        raise SimulatedDataError(
            "MISSING_COLUMNS",
            f"区域 {region.get('id')!r} 是表格，但没有声明任何列，无法生成行。",
        )
    columns = region_columns
    kinds = _column_value_types(columns)
    simulated_flags = _simulated_column_flags(columns, metrics or [])
    # 每一列的取值计划：值形态、是不是目录外、有效单位。
    plans = [
        (column, value_kind, simulated, _column_unit(column))
        for column, value_kind, simulated in zip(columns, kinds, simulated_flags)
    ]
    has_device = any(k in kinds for k in ("device_name", "device_id", "device_type"))
    has_time = "time" in kinds
    has_energy = "energy" in kinds

    window_hours = _window_hours(buckets)

    rows: list[dict[str, Any]] = []
    if kind == KIND_ALARMS:
        for index in range(ALARM_ROW_COUNT):
            device = base.devices[
                int(hash_float(f"alarm/{seed_scope}/{index}", 0, len(base.devices) - 1e-6))
                % len(base.devices)
            ] if base.devices else None
            row: dict[str, Any] = {}
            for column, value_kind, simulated, unit in plans:
                if _is_placeholder_only(column):
                    # 占位模式：只显示名称，不出数值。
                    continue
                seed = f"alarm/{seed_scope}/{index}/{column['fieldRef']}"
                if value_kind in ("device_name", "device_id", "device_type") and device:
                    row[column["fieldRef"]] = _row_value(
                        value_kind, seed=seed, device=device, bucket=None,
                        hours=window_hours, simulated=simulated, unit=unit,
                    )
                elif value_kind == "time":
                    moment = _moment_in_window(
                        ctx, index, f"alarm/{seed_scope}/{index}"
                    )
                    row[column["fieldRef"]] = moment.isoformat()
                else:
                    row[column["fieldRef"]] = _row_value(
                        value_kind, seed=seed, device=device, bucket=None,
                        hours=window_hours, simulated=simulated, unit=unit,
                    )
            rows.append(row)
    elif kind == KIND_CABINET_STATUS:
        for device in base.devices:
            row = {}
            for column, value_kind, simulated, unit in plans:
                if _is_placeholder_only(column):
                    continue
                seed = f"cabinet/{seed_scope}/{device['deviceId']}/{column['fieldRef']}"
                row[column["fieldRef"]] = _row_value(
                    value_kind, seed=seed, device=device, bucket=None,
                    hours=window_hours, simulated=simulated, unit=unit,
                )
            rows.append(row)
    elif has_device:
        # 设备粒度：每台设备一行。**时间列也要有值** —— 一张「设备 + 时间 +
        # 数值」的明细表里，时间列空着会让用户以为产品坏了。
        # 设备级是「窗口内每台设备各一条汇总」，时间取窗口里的最后一个桶起点
        # （报表口径的取数时刻），不是空白。
        device_bucket = buckets[-1] if buckets else None
        for device in base.devices:
            row = {}
            for column, value_kind, simulated, unit in plans:
                if _is_placeholder_only(column):
                    continue
                seed = f"device/{seed_scope}/{device['deviceId']}/{column['fieldRef']}"
                row[column["fieldRef"]] = _row_value(
                    value_kind, seed=seed, device=device, bucket=device_bucket,
                    hours=window_hours, simulated=simulated, unit=unit,
                )
            rows.append(row)
    elif has_time and has_energy:
        for bucket in buckets:
            row = {}
            for column, value_kind, simulated, unit in plans:
                if _is_placeholder_only(column):
                    continue
                seed = f"trend/{seed_scope}/{bucket.start.isoformat()}/{column['fieldRef']}"
                row[column["fieldRef"]] = _row_value(
                    value_kind, seed=seed, device=None, bucket=bucket,
                    hours=window_hours, simulated=simulated, unit=unit,
                )
            rows.append(row)
    elif has_time:
        for bucket in buckets:
            row = {}
            for column, value_kind, simulated, unit in plans:
                if _is_placeholder_only(column):
                    continue
                seed = f"series/{seed_scope}/{bucket.start.isoformat()}/{column['fieldRef']}"
                row[column["fieldRef"]] = _row_value(
                    value_kind, seed=seed, device=None, bucket=bucket,
                    hours=window_hours, simulated=simulated, unit=unit,
                )
            rows.append(row)
    else:
        for device in base.devices:
            row = {}
            for column, value_kind, simulated, unit in plans:
                if _is_placeholder_only(column):
                    continue
                seed = f"generic/{seed_scope}/{device['deviceId']}/{column['fieldRef']}"
                row[column["fieldRef"]] = _row_value(
                    value_kind, seed=seed, device=device, bucket=None,
                    hours=window_hours, simulated=simulated, unit=unit,
                )
            rows.append(row)

    return {
        "columns": columns,
        "rowNumber": bool((region.get("component") or {}).get("rowNumber", True)),
        "rows": rows,
        "dataSource": "simulated",
    }


def _moment_in_window(ctx: _Context, index: int, seed: str = "") -> datetime:
    """告警发生时间：窗口内的一个确定时刻。

    ``seed`` 非空时按种子在窗口内**错开**一点点：区域是种子材料的一部分，
    同一份定义里两个告警板块的行不该整行撞上。
    """
    span = _hours_between(ctx.start, ctx.end) * 3600.0
    if span <= 0:
        return ctx.start
    offset = span * ((index + 0.5) / ALARM_ROW_COUNT)
    if seed:
        offset += span * (hash_float(seed, 0.0, 1.0) - 0.5) / ALARM_ROW_COUNT
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
    seed_scope: str,
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
    # 横轴绑了**设备维度**字段时，这一块按设备出数（不是时间桶）——
    # 验收 11-01：用户说「横轴保持用设备名称」，这里原先把声明的
    # ``deviceName`` 强行改回 ``timestamp``，图上还是 0–23 的小时序号，
    # 「设备名称」反而被当成一条数值系列。
    x_is_device = is_device_dimension(str(declared_x or ""))

    def _y_field_for(metric_kind: str) -> str:
        return _CHART_Y_FIELDS.get(metric_kind, "value")

    declared_y_field = str(declared_y or "").strip()

    def _declared_numeric_values() -> tuple[list[float], str] | None:
        """声明的纵轴字段是**逐点数值字段**时，按它出数。

        验收 11-01：用户说「纵轴保持用耗电量」，纵轴字段就是 ``energyKwh``。
        那这一块每个点的耗电量必须是**那个字段自己的值**（设备级就是各台设备
        的能耗），不能因为区域的指标登记成「通用示意指标」就拿示意值顶上 ——
        那样图上标着 kWh、数字却是另一套口径。
        """
        if declared_y_field == "energyKwh":
            if x_is_device:
                return [d["energyKwh"] for d in base.devices], "energyKwh"
            return [b.energy for b in buckets], "energyKwh"
        if declared_y_field in ("costYuan", "energyCost"):
            price = SIMULATED_ELECTRICITY_PRICE_PER_KWH
            if x_is_device:
                return (
                    [round(d["energyKwh"] * price, 2) for d in base.devices],
                    declared_y_field,
                )
            return [round(b.energy * price, 2) for b in buckets], declared_y_field
        if declared_y_field == "powerKw":
            if x_is_device:
                return (
                    [
                        round(d["energyKwh"] / max(1.0, window_hours), 2)
                        for d in base.devices
                    ],
                    declared_y_field,
                )
            return list(powers), declared_y_field
        return None

    def _series_for(
        metric_kind: str
    ) -> tuple[list[str], list[float], str, list[Any]]:
        """返回 (x 标签, y 值, y 字段名, x 原值)。

        x 原值是**带时区的 ISO 时间戳**（趋势）或对象名（对比），
        与既有预览接口的 ``points`` 形状一致；``xLabels`` 才是给人看的标签。
        """
        declared_values = _declared_numeric_values()
        if declared_values is not None:
            values, y_field = declared_values
            if x_is_device:
                names = [d["deviceName"] for d in base.devices]
                return names, values, y_field, names
            return (
                _axis_labels(buckets, grain, multi_day),
                values,
                y_field,
                [b.start.isoformat() for b in buckets],
            )
        if role == "TREND" and not x_is_device:
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
                    round(hash_float(f"pue/{seed_scope}/{b.start.isoformat()}", 1.2, 1.8 - 1e-6), 2)
                    for b in buckets
                ]
            elif metric_kind == KIND_GENERIC:
                values = [
                    round(hash_float(f"generic/{seed_scope}/{b.start.isoformat()}", 10.0, 90.0), 1)
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
                round(hash_float(f"pue/{seed_scope}/{d['deviceId']}", 1.2, 1.8 - 1e-6), 2)
                for d in base.devices
            ]
        elif metric_kind == KIND_GENERIC:
            values = [
                round(hash_float(f"generic/{seed_scope}/{d['deviceId']}", 10.0, 90.0), 1)
                for d in base.devices
            ]
        else:
            values = [d["energyKwh"] for d in base.devices]
        names = [d["deviceName"] for d in base.devices]
        return names, values, _y_field_for(metric_kind), names

    # 占位模式的指标**不产生数据点**（整列不进 series）。
    visible = [item for item in metrics if not item.get("placeholderOnly")]
    # 横轴用的**分类字段**不是这一块的量：它已经当横轴用了，不能再当一条
    # 数值系列（验收 11-01：图例把「设备名称」和「耗电量」并排画成两个
    # 堆叠数值系列）。
    if declared_x:
        kept: list[dict[str, Any]] = []
        for item in visible:
            label = str(item.get("label") or "").strip()
            key = str(item.get("metricKey") or "").strip()
            canonical = canonical_field_of(label) or canonical_field_of(key)
            if canonical and canonical == str(declared_x):
                continue
            kept.append(item)
        visible = kept
    primary = (
        visible[0]
        if visible
        else {"kind": kind, "metricKey": kind, "label": region.get("name")}
    )
    if visible:
        x_labels, y_values, y_field, x_values = _series_for(primary["kind"])
    else:
        # 全是占位：仍然给一份**合法**的载荷（没有点，不报错）。
        x_labels = _axis_labels(buckets, grain, multi_day)
        y_values = []
        y_field = _y_field_for(kind)
        x_values = []
    if declared_y and y_field == "value":
        # 量身份认不出来时，认定义里声明的纵轴字段。
        y_field = declared_y
    x_field = declared_x or ("timestamp" if role == "TREND" else "deviceName")

    points = [
        {x_field: x_value, y_field: value}
        for x_value, value in zip(x_values, y_values)
    ]

    # 单位：规格要求数值型组件自带单位，前端从载荷读，不硬编码。
    units = _chart_units(spec, kind, visible or metrics)
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
    if len(visible) > 1:
        series = []
        for index, item in enumerate(visible):
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
    # **分类统计口径**（饼图 / 分类图）：区块上挂了分类绑定就按它出数。
    #
    # 这一支的横轴是**分类**（告警等级、告警区域…），不是时间，也不是设备 ——
    # 分类标签与数值一一对应，并标注「模拟」。
    categorical = _categorical_binding(region)
    if categorical:
        categories = [
            item
            for item in (categorical.get("categories") or [])
            if isinstance(item, dict)
        ]
        labels = [str(item.get("label") or "") for item in categories]
        values = [item.get("value") for item in categories]
        payload.update(
            {
                "chartKind": str(component.get("chartKind") or "PIE"),
                "grain": categorical.get("grain") or grain,
                "xField": str(categorical.get("categoryField") or x_field),
                "yField": str(categorical.get("valueField") or y_field),
                "unit": str(categorical.get("unit") or unit or ""),
                "xLabels": labels,
                "yValues": values,
                "points": [
                    {
                        str(categorical.get("categoryField") or "category"): label,
                        str(categorical.get("valueField") or "count"): value,
                    }
                    for label, value in zip(labels, values)
                ],
                "simulated": True,
                "categoryLabel": str(categorical.get("categoryLabel") or ""),
                "statistic": str(categorical.get("statistic") or ""),
                "dataSource": "simulated",
            }
        )
        payload.pop("series", None)
    return payload


def _categorical_binding(region: dict[str, Any]) -> dict[str, Any] | None:
    """区块上的**分类统计口径**绑定（``previewSpec.categoricalBinding``）。

    绑定是「分类 + 计数」的权威来源：标签与数值一一对应，
    数值是**模拟**的（``simulated`` 标记随载荷带出去）。
    """
    binding = region.get("templateBinding") or {}
    spec = binding.get("previewSpec") if isinstance(binding, dict) else None
    if not isinstance(spec, dict):
        return None
    categorical = spec.get("categoricalBinding")
    return categorical if isinstance(categorical, dict) else None


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


def _metric_group_payload(
    region: dict[str, Any],
    spec: dict[str, Any],
    *,
    field_values: dict[str, Any],
    comparison_values: dict[str, Any],
    base: _Base,
    ctx: _Context,
) -> dict[str, Any]:
    """多指标组的基础载荷：一个区域里的**指标项列表**。

    指标组的每一项绑定一个**已声明的单值字段**（模拟字段目录），值来自
    本区域的受控模拟供给 —— **不凭空造数**、不自动聚合、不生成子卡片。
    """
    component = region.get("component") or {}
    items = component.get("metrics")
    items = [item for item in (items or []) if isinstance(item, dict)]
    metrics: list[dict[str, Any]] = []
    for item in items:
        field_ref = str(item.get("fieldRef") or "")
        value = field_values.get(field_ref)
        declared = simulated_field(field_ref)
        metrics.append(
            {
                "metricKey": field_ref or str(item.get("id") or ""),
                "id": str(item.get("id") or ""),
                "label": str(item.get("label") or field_ref or "指标"),
                "kind": str((declared or {}).get("dataType") or "GENERIC").lower(),
                "unit": str((declared or {}).get("unit") or ""),
                "value": value,
                "precision": int((item.get("format") or {}).get("precision") or 0),
                "capabilityPending": False,
                "pendingReason": None,
            }
        )
    primary = metrics[0] if metrics else {}
    value = primary.get("value")
    return {
        "label": str(component.get("title") or region.get("name") or "指标组"),
        "value": value,
        "unit": str(primary.get("unit") or ""),
        "precision": int(primary.get("precision") or 0),
        "yoyPercent": None,
        "comparisonAvailable": any(
            item.get("fieldRef") in comparison_values for item in items
        ),
        "windowLabel": base.window_label,
        "dataSource": "simulated",
        "metrics": metrics,
        "arrangement": component.get("arrangement") or {},
    }


def _apply_metric_field_binding(
    region: dict[str, Any],
    payload: dict[str, Any],
    field_values: dict[str, Any],
) -> None:
    """指标卡绑定到**具体字段**时，数值真的跟着字段走（M01）。

    没绑字段（或字段取不到值）→ **保留原有口径**，不换数、不造数。

    **写在哪一项上按身份来，不按位置来**：卡片被追加过指标之后，清单里的
    第一项是**追加的那一项**（反馈 F02-04）。按位置写会把主指标的数值 /
    字段 / 单位盖到追加项上 —— 新增的「总告警数」就显示成 ``5.0 kWh``。
    """
    metric = (region.get("component") or {}).get("metric") or {}
    field_ref = str(metric.get("fieldRef") or "")
    if not field_ref:
        return
    if field_ref not in field_values:
        return
    value = field_values.get(field_ref)
    declared = simulated_field(field_ref)
    payload["value"] = value
    payload["unit"] = str((declared or {}).get("unit") or payload.get("unit") or "")
    metrics = payload.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        return
    index = _index_of_primary_metric(metrics, metric, field_ref)
    if index is None:
        # 清单里没有主指标这一项 → **不往别的项上写**。
        # 宁可主数值停在载荷顶层，也不把主指标的身份盖到别的指标上。
        return
    item = metrics[index]
    if not isinstance(item, dict):
        return
    item["value"] = value
    item["metricKey"] = field_ref
    item["unit"] = payload["unit"]


def _index_of_primary_metric(
    metrics: list[dict[str, Any]], metric: dict[str, Any], field_ref: str,
) -> int | None:
    """指标清单里**主指标**那一条的下标（认不出就是 ``None``）。

    判据是**身份**：字段引用 / 指标 id / 标签任一对上就是它。
    「清单里只有一项」时也认（老式指标卡：清单里就主指标一项）。
    """
    primary_id = str(metric.get("id") or "")
    primary_label = str(metric.get("label") or "")
    for index, item in enumerate(metrics):
        if not isinstance(item, dict):
            continue
        if str(item.get("fieldRef") or "") == field_ref:
            return index
        if str(item.get("metricKey") or "") == field_ref:
            return index
        if primary_id and str(item.get("id") or "") == primary_id:
            return index
        if primary_label and str(item.get("label") or "") == primary_label:
            return index
    if len(metrics) == 1:
        # 只有一项：它就是主指标（老式指标卡的历史形状）。
        return 0
    return None


def _chart_series_keys(region: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    """图表的**稳定系列标识**（图例配色按它认系列）。

    来源：组件指标项的 id / 指标键 / 字段键、``previewSpec`` 的指标键、
    分类绑定的分类标签、以及声明的编码字段。**按定义取**，不按顺序猜。
    """
    component = region.get("component") or {}
    keys: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        keys.append(text)

    for item in component.get("metrics") or []:
        if isinstance(item, dict):
            _add(item.get("id"))
            _add(item.get("metricKey"))
            _add(item.get("fieldRef"))
    metric = component.get("metric")
    if isinstance(metric, dict):
        _add(metric.get("id"))
        _add(metric.get("metricKey"))
        _add(metric.get("fieldRef"))
    for item in spec.get("metrics") or []:
        if isinstance(item, dict):
            _add(item.get("metricKey"))
    categorical = _categorical_binding(region)
    if categorical:
        for item in categorical.get("categories") or []:
            if isinstance(item, dict):
                _add(item.get("label"))
    for encoding in component.get("encodings") or []:
        if isinstance(encoding, dict):
            _add(encoding.get("fieldRef"))
    return keys


def _decorate_component_payload(
    region: dict[str, Any],
    payload: dict[str, Any],
    *,
    field_values: dict[str, Any],
    comparison_values: dict[str, Any],
    full_rows: list[dict[str, Any]],
    visible_rows: list[dict[str, Any]],
    summary_values: dict[str, Any],
    base: _Base,
    ctx: _Context,
    seed_scope: str,
    room_name: str = "",
) -> dict[str, Any]:
    """第二批的**组件专属载荷接线**（C0 侧）。

    这里只做**编排**：造数仍然是本模块的事（口径统一、确定性不变），
    各组件包负责「定义 → 组件载荷」的**纯转换**（协议字段落到载荷键）。

    * 文本      → ``text_metric_payload_service.build_text_payload``
    * 指标卡/指标组 → ``text_metric_payload_service.decorate_metric_payload``
    * 表格      → ``table_payload_service.decorate_table_payload``
    * 图表      → ``chart_payload_service.decorate_chart_payload``

    组件包**不取生产数据、不存库、不调模型** —— 这里也不给它们写权限。
    """
    component = region.get("component") or {}
    ctype = str(component.get("type") or "")
    spec = _preview_spec(region)
    ctx_payload = {
        "fieldValues": dict(field_values),
        "comparisonValues": dict(comparison_values),
        "roomName": room_name,
        "logicalDate": str(ctx.start.astimezone(ctx.tz).date().isoformat()),
        "fullRows": list(full_rows),
        "visibleRows": list(visible_rows),
        "summaryValues": dict(summary_values),
        "seriesKeys": _chart_series_keys(region, spec),
        "base": base,
        "seedScope": seed_scope,
    }
    if ctype == "TEXT":
        from app.services import text_metric_payload_service

        return text_metric_payload_service.build_text_payload(region, ctx=ctx_payload)
    if ctype in ("METRIC_CARD", "METRIC_GROUP"):
        from app.services import text_metric_payload_service

        _apply_metric_field_binding(region, payload, field_values)
        decorated = text_metric_payload_service.decorate_metric_payload(
            region, payload, ctx=ctx_payload
        )
        # 区域格式在**字段绑定之后**再算一遍显示串。
        #
        # 绑定把主指标的值换成了它真正绑的字段（9 台），显示串必须跟着
        # 换 —— 不然卡片上会出现「数值 9、显示 3,199 台」这种自相矛盾
        # 的载荷（实测：给指标卡设一次区域级格式就出现）。
        _attach_authoritative_format(decorated, region)
        return decorated
    if ctype == "TABLE":
        from app.services import table_payload_service

        return table_payload_service.decorate_table_payload(
            region, payload, ctx=ctx_payload
        )
    if ctype == "CHART":
        from app.services import chart_payload_service

        return chart_payload_service.decorate_chart_payload(
            region, payload, ctx=ctx_payload
        )
    return payload


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


# --------------------------------------------------------------------------
# 模拟字段目录的**受控单值**供给（第二批）
# --------------------------------------------------------------------------
#
# 第二批要绑的是「**已有输出里的字段**」。当前是模拟数据阶段，没有生产
# 计算引擎，因此这里按模拟字段目录
# （``display_binding_service.SIMULATED_FIELD_CATALOG``）给出**受控、
# 确定性**的单值：
#
# * 来源清楚：每个字段都来自本区域已有的模拟基础数据（能耗、设备、告警），
#   不是随手编的数字；
# * 确定性：按**区域种子**推导，同一份定义两次生成结果相同；
# * **不写进纯定义**、不迁移历史数据。

def _simulated_field_values(
    base: _Base,
    ctx: _Context,
    region: dict[str, Any],
    seed_scope: str,
    room_name: str,
) -> dict[str, Any]:
    """按模拟字段目录给出本区域的**单值**字典。"""
    device_count = len(base.devices)
    alarm_total = ALARM_ROW_COUNT
    serious = int(
        hash_float(f"serious/{seed_scope}", 1, max(1, alarm_total - 1))
    )
    processing = max(0, alarm_total - serious - 1)
    running = int(
        hash_float(f"running/{seed_scope}", max(1, device_count - 1), device_count)
    )
    pue = round(hash_float(f"pue/{seed_scope}", 1.2, 1.8 - 1e-6), 2)
    logical_date = ctx.start.astimezone(ctx.tz).date().isoformat()
    return {
        "totalAlarmCount": alarm_total,
        "seriousAlarmCount": serious,
        "processingAlarmCount": processing,
        "deviceCount": device_count,
        "runningDeviceCount": running,
        "totalEnergyKwh": round(float(base.total_kwh), 1),
        "totalCost": round(
            float(base.total_kwh) * SIMULATED_ELECTRICITY_PRICE_PER_KWH, 2
        ),
        "averagePue": pue,
        "roomName": room_name,
        "logicalDate": logical_date,
    }


def _comparison_field_values(
    field_values: dict[str, Any],
    seed_scope: str,
) -> dict[str, Any]:
    """比较字段的**对照值**（模拟的上一期口径）。

    口径写在 ``detail`` 里：同一个量的对照值按**量族**给（同一份报表里
    同一个量的对照一致），不是随机数。
    """
    out: dict[str, Any] = {}
    total_energy = field_values.get("totalEnergyKwh")
    if isinstance(total_energy, (int, float)):
        out["totalEnergyKwh"] = round(float(total_energy) * 0.92, 1)
    total_cost = field_values.get("totalCost")
    if isinstance(total_cost, (int, float)):
        out["totalCost"] = round(float(total_cost) * 0.92, 2)
    alarms = field_values.get("totalAlarmCount")
    if isinstance(alarms, (int, float)):
        out["totalAlarmCount"] = int(alarms)
        out["seriousAlarmCount"] = field_values.get("seriousAlarmCount")
        out["processingAlarmCount"] = field_values.get("processingAlarmCount")
    devices = field_values.get("deviceCount")
    if isinstance(devices, (int, float)):
        out["deviceCount"] = int(devices)
        out["runningDeviceCount"] = field_values.get("runningDeviceCount")
    pue = field_values.get("averagePue")
    if isinstance(pue, (int, float)):
        out["averagePue"] = round(float(pue) * 0.98, 2)
    return out


# --------------------------------------------------------------------------
# 展示裁剪（第二批 B03 / B04）
# --------------------------------------------------------------------------

def _display_pipeline_rows(
    region: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    allow_sort_and_topn: bool = True,
) -> list[dict[str, Any]]:
    """本区域的**展示记录**（别名 → 过滤 → 排序 → 前 N 项）。

    只作用这个区域的展示：源输出、全量总计、其他区域的输入**不动**。

    ``allow_sort_and_topn=False`` 时**只做别名与过滤** —— 时间序列的横轴
    顺序是数据的含义，排序与前 N 项会把它破坏掉（那不是展示裁剪，是改口径）。
    """
    from app.services.display_binding_service import apply_presentation_pipeline

    binding = region.get("binding") or {}
    if not isinstance(binding, dict):
        binding = {}
    return apply_presentation_pipeline(
        rows,
        field_aliases=binding.get("fieldAliases"),
        presentation_filters=binding.get("presentationFilters"),
        sort=binding.get("sort") if allow_sort_and_topn else None,
        top_n=binding.get("topN") if allow_sort_and_topn else None,
    )


def _summary_values(
    region: dict[str, Any],
    *,
    full_rows: list[dict[str, Any]],
    visible_rows: list[dict[str, Any]],
    field_values: dict[str, Any],
) -> dict[str, Any]:
    """汇总取值：**后台**算出来的受控模拟值（不在浏览器累加）。

    * ``FULL_RESULT``   → 全量结果口径（裁剪**之前**的记录）；
    * ``VISIBLE_ROWS``  → 可见行口径（裁剪**之后**的记录）。

    取不到合法口径时**不返回值**（载荷层据此显示「没有对应输出」，
    **不沿用旧数字、不临时求和**）。
    """
    from app.services.display_binding_service import resolve_summary_output

    summaries = (region.get("component") or {}).get("summaries")
    if not isinstance(summaries, list) or not summaries:
        return {}
    values: dict[str, Any] = {}
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        check = resolve_summary_output(region, summary)
        if not check.get("ok"):
            continue
        output_ref = str(check.get("outputRef") or "")
        field_ref = str(check.get("fieldRef") or "")
        basis = str(check.get("basis") or "")
        rows = full_rows if basis == "FULL_RESULT" else visible_rows
        numeric = [
            row.get(field_ref)
            for row in rows
            if isinstance(row, dict) and isinstance(row.get(field_ref), (int, float))
        ]
        if numeric:
            values[f"{output_ref}|{field_ref}|{basis}"] = round(
                float(sum(numeric)), 2
            )
            continue
        # 记录里没有该字段的数值时，只在**全量口径**下退回模拟字段目录的单值。
        if basis == "FULL_RESULT" and field_ref in field_values:
            value = field_values.get(field_ref)
            if isinstance(value, (int, float)):
                values[f"{output_ref}|{field_ref}|{basis}"] = value
    return values


# ---------------------------------------------------------------- 公开入口


def _source_mode_of_region(region: dict[str, Any], definition_mode: SourceMode) -> SourceMode:
    """这一个区域的来源模式：区域级记录优先，没记录就用定义级。

    区域级与定义级**不一致**是校验层的失败项（契约表 §1.4）—— 造数这一层
    不改写谁的值，只按服务端记录的那一份来标。
    """
    region_mode = source_mode_of_region(region)
    return region_mode if region_mode.recorded else definition_mode


def _generate_user_json_payloads(
    definition: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """用户 JSON 模式的载荷：用**完整已校验数据**渲染。

    数据从**已保存的原始输入**里取（生效组合绑定的那一版），
    不用模型看到的样本，也不重新随机选图（规格第 22 / 43 / 48 条）。

    没有绑定输入、或绑定的输入找不到了 → **明确失败**，
    不拿模拟数字冒充用户的真实数据（规格第 49 条）。
    """
    from app.services.effective_combination import combination_of_definition
    from app.services.json_payload_service import build_user_json_payloads
    from app.services.user_json_service import user_json_service

    combination = combination_of_definition(definition)
    input_id = combination.get("inputId")
    input_version = combination.get("inputVersion")
    if not input_id or input_version is None:
        raise SimulatedDataError(
            "JSON_INPUT_MISSING",
            "这份报表声明使用用户 JSON 数据，但没有绑定输入记录。",
        )
    record = user_json_service.get(str(input_id), int(input_version))
    if record is None:
        raise SimulatedDataError(
            "JSON_INPUT_MISSING",
            "绑定的用户 JSON 输入记录不存在，无法渲染。",
        )
    rows = user_json_service.rows_of(record)
    # 全局数据问题（`metadata.jsonInputIssues.dataIssues`）**不进区域载荷** ——
    # 它由报表面板顶部渲染一次；塞进每块板的 notes 就是同一句话说 N 遍。
    payloads = build_user_json_payloads(
        definition=definition,
        rows=rows,
        combination=combination,
    )
    # 权威格式随载荷带出去（与模拟模式同一套字段）。
    for region in _iter_leaf_regions(definition.get("regions")):
        region_id = str(region.get("id") or "")
        payload = payloads.get(region_id)
        if isinstance(payload, dict):
            _attach_authoritative_format(payload, region)
    for payload in payloads.values():
        payload[SOURCE_MODE_KEY] = SOURCE_MODE_USER_JSON
    return payloads


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

    definition_mode = source_mode_of_definition(definition)
    if definition_mode.is_user_json:
        # 用户 JSON 模式：数字来自用户粘贴并经服务端校验的数据，
        # 不是造数器编出来的示例值（ADR 0004 §8）。
        return _generate_user_json_payloads(definition)

    ctx = _build_context(definition, preview_context)
    base = _build_base(definition, ctx)
    room_name = resolve_room_name(room_id_of_definition(definition))
    buckets = _buckets_for_grain(ctx, base, ctx.grain)

    result: dict[str, dict[str, Any]] = {}
    for region in _iter_leaf_regions(regions):
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
        # 列先算一遍：指标清单（目录外指标）与表格 / 图表都用同一份列。
        columns = _columns_of(region, spec)
        metrics = _resolve_metrics(region, spec, kind, columns=columns)
        # 区域级种子材料：报表级种子 + **数据身份**（不是展示 id）。
        # 移动 / 成组 / 改样式 → 身份不变 → 数值不漂；
        # 复制 → 展示 id 换新的、身份沿用源的 → 复制件数值与源件一致。
        seed_scope = ctx.identity_seed_scope(effective_simulation_identity(region) or region_id)
        region_grain = _region_grain(region) or ctx.grain
        region_buckets = (
            buckets if region_grain == ctx.grain else _buckets_for_grain(ctx, base, region_grain)
        )
        # 功率桶的噪声也带区域：两个功率趋势板块不该给出同一组点。
        region_powers = _bucket_powers(base, region_buckets, seed_scope)

        if ctype == "METRIC_CARD":
            payload = _metric_card_payload(
                region, spec, kind, metrics,
                base=base, buckets=region_buckets, powers=region_powers,
                ctx=ctx, seed_scope=seed_scope,
            )
        elif ctype == "METRIC_GROUP":
            payload = _metric_group_payload(
                region, spec,
                field_values=field_values,
                comparison_values=comparison_values,
                base=base, ctx=ctx,
            )
        elif ctype == "TABLE":
            payload = _table_payload(
                region, spec, kind, base=base, buckets=region_buckets, ctx=ctx,
                seed_scope=seed_scope, columns=columns, metrics=metrics,
            )
        elif ctype == "CHART":
            payload = _chart_payload(
                region, spec, kind, metrics,
                base=base, buckets=region_buckets, powers=region_powers, ctx=ctx,
                seed_scope=seed_scope,
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
        # 来源模式随载荷带出去（契约表 §1.1：``regions[].payload.sourceMode``）。
        #
        # 区域级记录优先，没记录就用定义级 —— 一份报表不混两种来源的数字，
        # 真混了由校验层拦（``validation_service`` 的来源模式一致性规则）。
        payload[SOURCE_MODE_KEY] = _source_mode_of_region(region, definition_mode).mode
        # 该区域的**权威格式**随载荷带出去（显示格式走权威格式）。
        _attach_authoritative_format(payload, region)
        # -- 第二批：受控模拟单值 + 展示裁剪 + 汇总取值 -------------------
        field_values = _simulated_field_values(
            base, ctx, region, seed_scope, room_name
        )
        comparison_values = _comparison_field_values(field_values, seed_scope)
        full_rows: list[dict[str, Any]] = []
        visible_rows: list[dict[str, Any]] = []
        if ctype == "TABLE":
            full_rows = [
                row for row in (payload.get("rows") or []) if isinstance(row, dict)
            ]
            visible_rows = _display_pipeline_rows(region, full_rows)
        elif ctype == "CHART":
            full_rows = [
                row for row in (payload.get("points") or []) if isinstance(row, dict)
            ]
            # 时间序列的横轴顺序是数据的含义 —— 排序与前 N 项不作用在它上面。
            is_trend = str(region.get("role") or "").upper() == "TREND"
            visible_rows = _display_pipeline_rows(
                region, full_rows, allow_sort_and_topn=not is_trend
            )
        summary_values = _summary_values(
            region,
            full_rows=full_rows,
            visible_rows=visible_rows,
            field_values=field_values,
        )
        # -- 第二批：组件专属的载荷接线（文本 / 指标 / 表格 / 图表） --------
        payload = _decorate_component_payload(
            region, payload,
            field_values=field_values,
            comparison_values=comparison_values,
            full_rows=full_rows,
            visible_rows=visible_rows,
            summary_values=summary_values,
            base=base,
            ctx=ctx,
            seed_scope=seed_scope,
            room_name=room_name,
        )
        result[region_id] = payload
    return result
