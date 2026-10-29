"""机房能耗假数据服务：为节点 9 PREVIEWING 提供可渲染的真实数据。

生产版会由 Java 预览引擎跑实际 SQL/ClickHouse 查询。这里用确定性的伪随机，
让同一个 roomId + logicalDate 多次预览结果一致，方便前端联调和截图。

**时区口径（第二轮修复 A1-P3-02）**：
报表定义里的时间窗带 `timezone`（默认 ``Asia/Shanghai``，与
``edit_time_resolver.DEFAULT_TIMEZONE`` 同源）。趋势点的时间戳必须与
**同一个时区**自洽：``2026-10-25T00:00:00+08:00`` 表示的就是**上海时间**
的 10-25 00:00。此前这里硬编码 ``tzinfo=timezone.utc``，标着
``2026-10-25T00:00:00+00:00``（= 上海 10-25 08:00），与定义里的
``Asia/Shanghai`` 自相矛盾：下游若按真实时刻解析，整条「昨日」窗口会偏 8 小时。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.data.machine_room_energy import (
    DEVICE_TYPE_CRAC,
    DEVICE_TYPE_PDU,
    DEVICE_TYPE_RACK,
    DEVICE_TYPE_UPS,
)
from app.services.edit_time_resolver import DEFAULT_TIMEZONE


# 机房 A 的设备清单（10 台）
ROOM_A_DEVICES: list[dict[str, str]] = [
    {"deviceId": "rack_01", "deviceName": "A-1 机架", "deviceType": DEVICE_TYPE_RACK},
    {"deviceId": "rack_02", "deviceName": "A-2 机架", "deviceType": DEVICE_TYPE_RACK},
    {"deviceId": "rack_03", "deviceName": "A-3 机架", "deviceType": DEVICE_TYPE_RACK},
    {"deviceId": "rack_04", "deviceName": "A-4 机架", "deviceType": DEVICE_TYPE_RACK},
    {"deviceId": "rack_05", "deviceName": "A-5 机架", "deviceType": DEVICE_TYPE_RACK},
    {"deviceId": "pdu_01", "deviceName": "PDU-01 配电", "deviceType": DEVICE_TYPE_PDU},
    {"deviceId": "pdu_02", "deviceName": "PDU-02 配电", "deviceType": DEVICE_TYPE_PDU},
    {"deviceId": "crac_01", "deviceName": "CRAC-01 空调", "deviceType": DEVICE_TYPE_CRAC},
    {"deviceId": "crac_02", "deviceName": "CRAC-02 空调", "deviceType": DEVICE_TYPE_CRAC},
    {"deviceId": "ups_01", "deviceName": "UPS-01 不间断", "deviceType": DEVICE_TYPE_UPS},
]


def _hash_float(seed: str, lo: float, hi: float) -> float:
    """用 seed 哈希到 [lo, hi) 的伪随机 float。"""
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()
    frac = int(digest[:8], 16) / 0xFFFFFFFF
    return lo + (hi - lo) * frac


def hash_float(seed: str, lo: float, hi: float) -> float:
    """公开的确定性取值入口：造数器各模块共用**同一种子算法**。

    模拟业务数据的确定性（同一份定义 + 同一时间窗 → 数字恒定）靠的就是它；
    换模块不换算法，两处造出来的数才不会各算各的。
    """
    return _hash_float(seed, lo, hi)


@dataclass
class HourlyPoint:
    timestamp: str
    energyKwh: float


@dataclass
class DeviceEnergy:
    deviceId: str
    deviceName: str
    energyKwh: float
    deviceType: str = ""


@dataclass
class RoomEnergySnapshot:
    """某一天某机房的能耗预览数据。"""

    roomId: str
    roomName: str
    logicalDate: str  # YYYY-MM-DD
    totalEnergyKwh: float
    yoyPercent: float  # 相比昨日的变化百分比
    hourlyTrend: list[HourlyPoint]
    minuteTrend: list[HourlyPoint]
    deviceRanking: list[DeviceEnergy]
    quality: dict[str, Any]
    anomalies: list[dict[str, Any]]
    timezone: str = DEFAULT_TIMEZONE  # 趋势点时间戳所用的 IANA 时区


def _minute_points(
    *, hourly: list[HourlyPoint], room_id: str, logical_date: str
) -> list[HourlyPoint]:
    """把小时趋势拆成分钟趋势，保持每小时总量基本一致。"""
    minute_points: list[HourlyPoint] = []
    for hour_index, hour_point in enumerate(hourly):
        hour_dt = datetime.fromisoformat(hour_point.timestamp)
        weights = [
            _hash_float(f"minute/{room_id}/{logical_date}/{hour_index}/{m}", 0.86, 1.14)
            for m in range(60)
        ]
        weight_sum = sum(weights)
        for minute, weight in enumerate(weights):
            value = round(hour_point.energyKwh * (weight / weight_sum), 3)
            minute_points.append(
                HourlyPoint(
                    timestamp=(hour_dt + timedelta(minutes=minute)).isoformat(),
                    energyKwh=value,
                )
            )
    return minute_points


def _hourly_shape() -> list[float]:
    """24 小时相对耗电占比形状：夜低、工作时段高、下午次高峰。"""
    base = [
        0.6, 0.55, 0.52, 0.50, 0.52, 0.60,  # 0-5 深夜
        0.72, 0.85, 1.00, 1.15, 1.20, 1.25,  # 6-11 工作时段爬坡
        1.22, 1.30, 1.35, 1.32, 1.28, 1.20,  # 12-17 午后次高峰
        1.10, 1.00, 0.95, 0.85, 0.75, 0.65,  # 18-23 下滑
    ]
    return base


def _resolve_timezone(name: str | None) -> ZoneInfo:
    """把 IANA 时区名解析成 ZoneInfo；认不出来就回落到报表默认时区。

    非法时区名不能让预览直接炸掉（预览是链路最后一步，炸了就出不来数），
    但也不能悄悄按 UTC 出数——回落到定义侧的默认时区，两边口径才一致。
    """
    if not name:
        return ZoneInfo(DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def definition_timezone(definition: dict[str, Any] | None) -> str:
    """从报表定义里取时间窗时区（取不到就用报表默认时区）。

    调用方（预览层）拿它给 :func:`build_snapshot` 传参，
    保证**数据侧的时间戳与定义侧声明的时区是同一个**。
    """
    if not isinstance(definition, dict):
        return DEFAULT_TIMEZONE
    defaults = definition.get("defaults") or {}
    window = defaults.get("timeWindow") or {}
    value = window.get("value") if isinstance(window.get("value"), dict) else window
    tz = value.get("timezone") or window.get("timezone")
    return str(tz) if tz else DEFAULT_TIMEZONE


def _default_logical_date(tz: ZoneInfo) -> str:
    """「昨天」= 报表时区里的昨天。

    不能用 ``date.today()``：那是**机器本地时区**的今天，和报表时区
    不一定同一天；跨时区部署时会把「昨日」窗口整体挪一天。
    """
    return (datetime.now(tz).date() - timedelta(days=1)).isoformat()


def _normalize_logical_date(logical_date: str | None, tz: ZoneInfo) -> str:
    if logical_date:
        return logical_date
    return _default_logical_date(tz)


def build_snapshot(
    *,
    room_id: str,
    room_name: str,
    logical_date: str | None = None,
    timezone: str = DEFAULT_TIMEZONE,
) -> RoomEnergySnapshot:
    """生成某机房某天的能耗快照。

    ``timezone`` 必须与报表定义 ``defaults.timeWindow.timezone`` 一致，
    否则趋势点的 ISO 串会与定义自相矛盾（见模块头说明）。
    """
    tz = _resolve_timezone(timezone)
    ld = _normalize_logical_date(logical_date, tz)

    # 总耗电：机房 A 典型值在 4800~5600 kWh
    total = round(_hash_float(f"total/{room_id}/{ld}", 4800, 5600), 1)
    # 相比昨日波动
    yoy = round(_hash_float(f"yoy/{room_id}/{ld}", -8.0, 20.0), 1)

    # 小时趋势：按形状分配 total，并加入噪声
    shape = _hourly_shape()
    shape_sum = sum(shape)
    hourly: list[HourlyPoint] = []
    # 一天从**报表时区的 00:00** 开始；点上的钟点就是报表时区的钟点。
    base_dt = datetime.combine(date.fromisoformat(ld), time(0, 0), tzinfo=tz)
    for h, w in enumerate(shape):
        noise = _hash_float(f"hour/{room_id}/{ld}/{h}", 0.9, 1.1)
        value = round(total * (w / shape_sum) * noise, 1)
        hourly.append(
            HourlyPoint(
                timestamp=(base_dt + timedelta(hours=h)).isoformat(),
                energyKwh=value,
            )
        )

    # 校正总和，避免累加误差
    adjust_factor = total / max(sum(p.energyKwh for p in hourly), 0.01)
    for p in hourly:
        p.energyKwh = round(p.energyKwh * adjust_factor, 1)

    minute_trend = _minute_points(hourly=hourly, room_id=room_id, logical_date=ld)

    # 设备排名：按占比给每台设备分能耗
    device_weights = [
        _hash_float(f"dev/{room_id}/{ld}/{d['deviceId']}", 0.3, 1.3)
        for d in ROOM_A_DEVICES
    ]
    weight_sum = sum(device_weights)
    devices: list[DeviceEnergy] = []
    for d, w in zip(ROOM_A_DEVICES, device_weights):
        devices.append(
            DeviceEnergy(
                deviceId=d["deviceId"],
                deviceName=d["deviceName"],
                energyKwh=round(total * (w / weight_sum), 1),
                deviceType=d.get("deviceType", ""),
            )
        )
    devices.sort(key=lambda x: x.energyKwh, reverse=True)

    # 数据质量
    coverage = round(_hash_float(f"cov/{room_id}/{ld}", 0.92, 1.0), 3)
    missing = int(_hash_float(f"missing/{room_id}/{ld}", 0, 6))
    freshness = int(_hash_float(f"fresh/{room_id}/{ld}", 30, 180))

    # 异常事件：10:00-11:00 偶尔断线
    anomalies: list[dict[str, Any]] = []
    if _hash_float(f"anom/{room_id}/{ld}", 0, 1) > 0.4:
        anomalies.append(
            {
                "timeRange": "10:00-11:00",
                "affectedDevices": 3,
                "note": "A-3 机架、PDU-01 配电、CRAC-02 空调 短暂断线，数据可能偏低",
            }
        )
    if _hash_float(f"anom2/{room_id}/{ld}", 0, 1) > 0.75:
        anomalies.append(
            {
                "timeRange": "15:00-15:30",
                "affectedDevices": 1,
                "note": "CRAC-01 空调 温控异常，耗电偏高",
            }
        )

    return RoomEnergySnapshot(
        roomId=room_id,
        roomName=room_name,
        logicalDate=ld,
        totalEnergyKwh=round(sum(p.energyKwh for p in hourly), 1),
        yoyPercent=yoy,
        hourlyTrend=hourly,
        minuteTrend=minute_trend,
        deviceRanking=devices,
        quality={
            "coverage": coverage,
            "missing": missing,
            "duplicates": 0,
            "freshnessSeconds": freshness,
        },
        anomalies=anomalies,
        timezone=str(tz.key),
    )


def snapshot_to_dict(snap: RoomEnergySnapshot) -> dict[str, Any]:
    return {
        "roomId": snap.roomId,
        "roomName": snap.roomName,
        "logicalDate": snap.logicalDate,
        "totalEnergyKwh": snap.totalEnergyKwh,
        "yoyPercent": snap.yoyPercent,
        "timezone": snap.timezone,
        "hourlyTrend": [
            {"timestamp": p.timestamp, "energyKwh": p.energyKwh}
            for p in snap.hourlyTrend
        ],
        "minuteTrend": [
            {"timestamp": p.timestamp, "energyKwh": p.energyKwh}
            for p in snap.minuteTrend
        ],
        "deviceRanking": [
            {
                "deviceId": d.deviceId,
                "deviceName": d.deviceName,
                "energyKwh": d.energyKwh,
                "deviceType": d.deviceType,
            }
            for d in snap.deviceRanking
        ],
        "quality": snap.quality,
        "anomalies": snap.anomalies,
    }


ROOM_NAME_MAP = {
    "room_a": "A 机房",
    "room_b": "B 机房",
    "room_c": "C 机房",
}


def resolve_room_name(room_id: str) -> str:
    return ROOM_NAME_MAP.get(room_id, room_id)


def render_template(text: str, room_id: str, logical_date: str = "") -> str:
    """把模板串里的 {{占位符}} 换成真实值。

    草稿里写的是「{{roomName}} 昨日能耗日报」这种模板，前端不认占位符，
    直接显示会露出 `{{roomName}}`。造草稿、存快照、跑预览都过一遍这里，
    保证任何路径下拿到的都是「A 机房 昨日能耗日报」这样的成品文本。
    """
    if not text:
        return text
    out = text.replace("{{roomName}}", resolve_room_name(room_id))
    if logical_date:
        out = out.replace("{{logicalDate}}", logical_date)
    return out


def room_id_of_definition(definition: dict[str, Any] | None) -> str:
    """从草稿里取机房 id（取不到时给默认机房 A）。"""
    if not isinstance(definition, dict):
        return "room_a"
    scope = (definition.get("defaults") or {}).get("scope") or {}
    selectors = scope.get("selectors") or {}
    ids = selectors.get("machineRoomId") or []
    return str(ids[0]) if ids else "room_a"


def __getattr__(name: str) -> Any:
    """把造数器的公开入口也挂在这一层，避免调用方找错模块。

    真正的实现在 :mod:`app.services.simulated_data_service`；这里做**惰性**转发，
    以免两个模块在导入期互相等待。
    """
    if name in ("generate_simulated_data", "SimulatedDataError"):
        from app.services import simulated_data_service

        return getattr(simulated_data_service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
