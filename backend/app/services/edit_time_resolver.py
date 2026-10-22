"""时间表达解析：编辑态（USER_EDITING_TEMPLATE）专用。

给第二波集成方调用的**纯函数**模块：不改草稿、不调模型、不依赖编辑引擎。

三条硬规则（来自 20261022 template-editing react 架构改进方案）：

1. 「昨天下午」这类**带时段**的表达不能降格成整天：必须产出具体候选时间段，
   交回用户选择。绝不静默写成「昨天整天」。
2. 用户已经给出具体时间（如「昨天下午两点到六点」）就不再追问，直接给时间窗。
3. 没说具体钟点的整段表达（昨天 / 最近 N 天 / 本周）沿用 01_接口协议.md 的
   `CalendarWindow`（锚点 `runtime.logicalTime`），与现有 `EditOperation.timeWindow`
   结构完全一致，集成时可直接塞进 plan。

具体钟点使用本地 `FIXED_INTERVAL`（带时区的 `start` / `end` ISO 串），
已接入 Python 草稿与形状校验；Java 服务的协议兼容性仍需确认。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Shanghai"

# ---------- 状态 ----------

STATUS_RESOLVED = "RESOLVED"
STATUS_NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
STATUS_NOT_FOUND = "NOT_FOUND"


# ---------- 候选时段表 ----------
#
# 这些是**用户可选的候选**，不是默认值：命中「昨天下午」时全部作为选项摆出来，
# 由用户挑一个（或自己说一个），系统不替用户决定。
#
# 区间取业务上常见的切法；若产品口径不同，直接改这张表即可，解析逻辑不受影响。
PERIOD_CANDIDATES: dict[str, list[tuple[time, time]]] = {
    "凌晨": [(time(0, 0), time(5, 0)), (time(1, 0), time(5, 0)), (time(2, 0), time(6, 0))],
    "早上": [(time(6, 0), time(11, 0)), (time(7, 0), time(11, 0)), (time(8, 0), time(11, 0))],
    "上午": [(time(8, 0), time(12, 0)), (time(9, 0), time(12, 0)), (time(10, 0), time(12, 0))],
    "中午": [(time(11, 0), time(13, 0)), (time(12, 0), time(13, 0)), (time(12, 0), time(14, 0))],
    "下午": [(time(12, 0), time(18, 0)), (time(14, 0), time(18, 0)), (time(15, 0), time(18, 0))],
    "傍晚": [(time(16, 0), time(19, 0)), (time(17, 0), time(19, 0)), (time(18, 0), time(20, 0))],
    "晚上": [(time(18, 0), time(22, 0)), (time(19, 0), time(22, 0)), (time(20, 0), time(23, 59))],
    "夜里": [(time(20, 0), time(23, 59)), (time(21, 0), time(23, 59)), (time(22, 0), time(23, 59))],
}

# 时段词 → 归一化名称。命中多个时取最长的那个。
PERIOD_ALIASES: dict[str, str] = {
    "凌晨": "凌晨",
    "清晨": "凌晨",
    "晚上": "晚上",
    "夜里": "夜里",
    "夜间": "夜里",
    "早上": "早上",
    "早晨": "早上",
    "一早": "早上",
    "上午": "上午",
    "中午": "中午",
    "下午": "下午",
    "午后": "下午",
    "傍晚": "傍晚",
    "今晚": "晚上",
}

# 日锚点 → 相对逻辑时间当天的日历偏移（天）。
DAY_ANCHORS: dict[str, int] = {
    "今天": 0,
    "今日": 0,
    "当天": 0,
    "今晚": 0,
    "昨天": -1,
    "昨日": -1,
    "前天": -2,
    "大前天": -3,
    "明天": 1,
    "明日": 1,
}

# 相对区间表达 → (label 模板, unit, offset, count)。offset/count 里的 0 表示运行时算。
RELATIVE_WINDOWS: list[tuple[re.Pattern[str], str, str, int, int]] = [
    (re.compile(r"(最近|近)\s*(\d+)\s*天"), "最近 {n} 天", "DAY", 0, 0),
    (re.compile(r"(最近|近)\s*(\d+)\s*小时"), "最近 {n} 小时", "HOUR", 0, 0),
    (re.compile(r"(本周|这周|当周)"), "本周", "WEEK", 0, 1),
    (re.compile(r"(本月|这月|当月)"), "本月", "MONTH", 0, 1),
    (re.compile(r"(上个?月)"), "上月", "MONTH", -1, 1),
    (re.compile(r"(上个?周)"), "上周", "WEEK", -1, 1),
]

# 具体钟点：14:00-18:00 / 14点-18点 / 下午2点到6点
_CLOCK_RANGE_COLON = re.compile(
    r"(?P<h1>\d{1,2})\s*:\s*(?P<m1>\d{2})\s*(?:-|~|至|到)\s*(?P<h2>\d{1,2})\s*:\s*(?P<m2>\d{2})"
)
_CLOCK_RANGE_CN = re.compile(
    r"(?P<h1>\d{1,2}|[零一二两三四五六七八九十])\s*[点时]\s*"
    r"(?P<m1>\d{1,2}分?|半)?\s*"
    r"(?:-|~|至|到)\s*"
    r"(?P<h2>\d{1,2}|[零一二两三四五六七八九十])\s*[点时]\s*"
    r"(?P<m2>\d{1,2}分?|半)?"
)
_CLOCK_SINGLE = re.compile(
    r"(?P<h1>\d{1,2}|[零一二两三四五六七八九十])\s*[点时]\s*"
    r"(?P<m1>\d{1,2}分?|半)?"
)

# 「10点级」「3点趋势」这类不是钟点，别当时间解析。
_CLOCK_FALSE_FOLLOWERS = ("级", "位", "趋势", "粒度")

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


# ---------- 输出结构 ----------

@dataclass(frozen=True)
class TimeWindowOption:
    """一个可选时间窗候选。"""

    value: str
    label: str
    timeWindow: dict[str, Any]
    description: str = ""


@dataclass(frozen=True)
class ResolvedTimeWindow:
    """已确定的时间窗。"""

    start: datetime
    end: datetime
    timezone: str
    label: str
    # 与 01_接口协议.md CalendarWindow 对齐；仅整段表达有值
    calendarWindow: dict[str, Any] | None = None
    # 建议写入 EditOperation.timeWindow 的结构
    timeWindow: dict[str, Any] = field(default_factory=dict)
    anchorDateSource: str = "today"

    def to_calendar_window(self) -> dict[str, Any] | None:
        return self.calendarWindow


@dataclass(frozen=True)
class TimeResolution:
    """时间解析结果。"""

    status: str
    matchedText: str = ""
    label: str = ""
    window: ResolvedTimeWindow | None = None
    timeWindow: dict[str, Any] | None = None
    question: str = ""
    options: list[TimeWindowOption] = field(default_factory=list)
    reason: str = ""


# ---------- 工具 ----------

def _normalize(text: str) -> str:
    """全角数字/冒号归一 + 去空格，便于正则匹配。"""
    out: list[str] = []
    for ch in text or "":
        if "０" <= ch <= "９":
            out.append(str(ord(ch) - ord("０")))
        elif ch == "：":
            out.append(":")
        elif ch in "﹣－–—~～":
            out.append("-")
        elif ch.isspace():
            continue
        else:
            out.append(ch)
    return "".join(out)


def _cn_number(text: str) -> int | None:
    """把「两」「十二」「二十三」转成整数。"""
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "十" not in text:
        total = 0
        for ch in text:
            if ch not in _CN_DIGITS:
                return None
            total = total * 10 + _CN_DIGITS[ch]
        return total
    head, _, tail = text.partition("十")
    tens = 1 if head == "" else _CN_DIGITS.get(head)
    ones = 0 if tail == "" else _CN_DIGITS.get(tail)
    if tens is None or ones is None:
        return None
    return tens * 10 + ones


def _parse_clock_parts(
    hour_text: str, minute_text: str | None
) -> tuple[int, int] | None:
    """把「3点」「3点半」「3点20分」拆成 (时, 分)，不做时段换算。"""
    hour = _cn_number(hour_text)
    if hour is None:
        return None
    raw_minute = (minute_text or "").strip()
    if raw_minute == "半":
        minute = 30
    elif raw_minute:
        minute = _cn_number(raw_minute.rstrip("分"))
        if minute is None:
            minute = 0
    else:
        minute = 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour, minute


def _clock_minutes(hour_text: str, minute_text: str | None, period: str | None) -> int | None:
    """把「3点」「3点半」「3点20分」换算成当天分钟数（含下午 / 晚上换算）。"""
    parts = _parse_clock_parts(hour_text, minute_text)
    if parts is None:
        return None
    hour, minute = parts
    return _apply_period(hour * 60 + minute, period)


def _calendar_window(unit: str, offset: int, count: int, timezone: str) -> dict[str, Any]:
    """构造与 01_接口协议.md 一致的 CalendarWindow。"""
    return {
        "mode": "REPLACE",
        "value": {
            "type": "CALENDAR",
            "timezone": timezone,
            "anchor": {"type": "CONTEXT", "path": "runtime.logicalTime"},
            "unit": unit,
            "offset": offset,
            "count": count,
        },
    }


def _fixed_interval_window(start: datetime, end: datetime, timezone: str) -> dict[str, Any]:
    """构造本地协议的具体钟点窗口；Java 协议兼容性仍待确认。"""
    return {
        "mode": "REPLACE",
        "value": {
            "type": "FIXED_INTERVAL",
            "timezone": timezone,
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
    }


def _detect_period(text: str) -> tuple[str | None, str]:
    """返回 (归一化时段名, 命中的原文)。"""
    best: tuple[str, str] | None = None
    for alias, canonical in PERIOD_ALIASES.items():
        idx = text.find(alias)
        if idx < 0:
            continue
        if best is None or len(alias) > len(best[1]):
            best = (canonical, alias)
    return (best[0], best[1]) if best else (None, "")


def _detect_day_anchor(text: str) -> tuple[int | None, str]:
    """返回 (相对逻辑时间当天的日历偏移, 命中的原文)。"""
    best: tuple[int, str] | None = None
    for alias, offset in DAY_ANCHORS.items():
        idx = text.find(alias)
        if idx < 0:
            continue
        if best is None or len(alias) > len(best[1]):
            best = (offset, alias)
    return (best[0], best[1]) if best else (None, "")


def _detect_relative_window(text: str) -> tuple[str, str, int, int, str] | None:
    """返回 (label, unit, offset, count, 命中原文)。"""
    for pattern, label_tpl, unit, offset, count in RELATIVE_WINDOWS:
        match = pattern.search(text)
        if not match:
            continue
        if "{n}" in label_tpl:
            number = int(match.group(2))
            label = label_tpl.format(n=number)
            offset = -number
            count = number
        else:
            label = label_tpl
        return label, unit, offset, count, match.group(0)
    return None


def _day_label(offset: int) -> str:
    return {0: "今天", -1: "昨天", -2: "前天", -3: "大前天", 1: "明天"}.get(
        offset, f"{offset} 天前"
    )


def _draft_window_day_offset(current_time_window: dict[str, Any] | None) -> int | None:
    """从草稿当前时间窗里读出日历偏移（天）。"""
    if not isinstance(current_time_window, dict):
        return None
    value = current_time_window.get("value")
    if not isinstance(value, dict):
        return None
    if value.get("type") != "CALENDAR" or value.get("unit") != "DAY":
        return None
    offset = value.get("offset")
    return offset if isinstance(offset, int) else None


def _resolve_anchor_date(
    day_offset: int | None,
    logical_now: datetime,
    current_time_window: dict[str, Any] | None,
) -> tuple[date, str]:
    """确定这次表达指向的日历日期。

    * 原话里说了「昨天 / 今天」→ 用它；
    * 只给了钟点没给日锚点 → 沿用当前草稿时间窗的日期（草稿默认是「昨天」），
      这样「改成 14:00-18:00」不会莫名其妙跳到今天；
    * 都没有 → 逻辑时间当天。
    """
    if day_offset is not None:
        return logical_now.date() + timedelta(days=day_offset), "expression"

    draft_offset = _draft_window_day_offset(current_time_window)
    if draft_offset is not None:
        return logical_now.date() + timedelta(days=draft_offset), "draft_window"

    return logical_now.date(), "today"


# ---------- 对外接口 ----------

def resolve_time_expression(
    text: str,
    *,
    logical_now: datetime | None = None,
    timezone: str = DEFAULT_TIMEZONE,
    current_time_window: dict[str, Any] | None = None,
) -> TimeResolution:
    """解析一句中文时间表达。

    返回三态之一：

    * `RESOLVED` —— 已有确定时间窗（`window` / `timeWindow` 有值）；
    * `NEEDS_CLARIFICATION` —— 时段不够具体（典型如「昨天下午」），
      `options` 给出具体候选，`question` 直接可问；
    * `NOT_FOUND` —— 这句话里没有可解析的时间表达。

    参数：

    * `text`：用户原话（整句即可，内部自己找时间片段）；
    * `logical_now`：逻辑时间，决定「昨天」的基准日；默认取系统当前时间；
    * `timezone`：IANA 时区名，默认 `Asia/Shanghai`；
    * `current_time_window`：当前草稿已有的时间窗，用于「只给钟点没给日锚点」
      时沿用草稿口径的日期。
    """
    raw = text or ""
    normalized = _normalize(raw)
    if not normalized:
        return TimeResolution(status=STATUS_NOT_FOUND, reason="empty_text")

    tz = ZoneInfo(timezone)
    now = logical_now.astimezone(tz) if logical_now else datetime.now(tz)

    period, period_matched = _detect_period(normalized)
    day_offset, day_matched = _detect_day_anchor(normalized)
    clock = _detect_clock(normalized, period)

    # 1) 明确钟点 → 直接定窗，不再追问。
    if clock is not None:
        start_minute, end_minute, clock_matched = clock
        anchor_date, anchor_source = _resolve_anchor_date(
            day_offset, now, current_time_window
        )
        midnight = datetime.combine(anchor_date, time(0, 0), tzinfo=tz)
        start = midnight + timedelta(minutes=start_minute)
        end = midnight + timedelta(minutes=end_minute)
        if end <= start:
            end += timedelta(days=1)
        label = f"{anchor_date.isoformat()} {start:%H:%M}-{end:%H:%M}"
        window = ResolvedTimeWindow(
            start=start,
            end=end,
            timezone=timezone,
            label=label,
            calendarWindow=None,
            timeWindow=_fixed_interval_window(start, end, timezone),
            anchorDateSource=anchor_source,
        )
        return TimeResolution(
            status=STATUS_RESOLVED,
            matchedText=clock_matched,
            label=label,
            window=window,
            timeWindow=window.timeWindow,
            reason="explicit_clock",
        )

    # 2) 时段表达（「昨天下午」）→ 必须追问具体时间段，不落整天。
    if period is not None:
        anchor_date, _ = _resolve_anchor_date(day_offset, now, current_time_window)
        day_text = _day_label(day_offset) if day_offset is not None else "当天"
        options = _build_period_options(period, anchor_date, tz, timezone)
        matched = "".join(part for part in (day_matched, period_matched) if part)
        return TimeResolution(
            status=STATUS_NEEDS_CLARIFICATION,
            matchedText=matched or period_matched,
            label=f"{anchor_date.isoformat()} {period}",
            question=(
                f"「{day_text}{period}」具体要看哪一段时间？"
                f"选一个（{options[0].label} 等），或者直接告诉我起止时间。"
            ),
            options=options,
            reason="period_needs_exact_range",
        )

    # 3) 整段表达（昨天 / 最近 7 天 / 本周）→ CalendarWindow。
    if day_offset is not None:
        label = _day_label(day_offset)
        window_value = _calendar_window("DAY", day_offset, 1, timezone)
        day_start = (now + timedelta(days=day_offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        window = ResolvedTimeWindow(
            start=day_start,
            end=day_start + timedelta(days=1),
            timezone=timezone,
            label=label,
            calendarWindow=window_value,
            timeWindow=window_value,
            anchorDateSource="expression",
        )
        return TimeResolution(
            status=STATUS_RESOLVED,
            matchedText=day_matched,
            label=label,
            window=window,
            timeWindow=window_value,
            reason="day_anchor",
        )

    # 4) 相对区间（最近 7 天 / 本周 / 本月）→ CalendarWindow。
    relative = _detect_relative_window(normalized)
    if relative is not None:
        label, unit, offset, count, matched = relative
        window_value = _calendar_window(unit, offset, count, timezone)
        window = ResolvedTimeWindow(
            start=now,
            end=now,
            timezone=timezone,
            label=label,
            calendarWindow=window_value,
            timeWindow=window_value,
            anchorDateSource="expression",
        )
        return TimeResolution(
            status=STATUS_RESOLVED,
            matchedText=matched,
            label=label,
            window=window,
            timeWindow=window_value,
            reason="relative_window",
        )

    return TimeResolution(status=STATUS_NOT_FOUND, reason="no_time_expression")


def build_time_window_from_option(option: TimeWindowOption) -> dict[str, Any]:
    """把一个候选选项还原成可写入 `EditOperation.timeWindow` 的结构。"""
    return dict(option.timeWindow)


def _detect_clock(text: str, period: str | None) -> tuple[int, int, str] | None:
    """找出具体钟点区间，返回 (起始分钟, 结束分钟, 命中原文)。"""
    match = _CLOCK_RANGE_COLON.search(text)
    if match:
        start = _apply_period(
            int(match.group("h1")) * 60 + int(match.group("m1")), period
        )
        end = _apply_period(
            int(match.group("h2")) * 60 + int(match.group("m2")), period
        )
        if start is None or end is None:
            return None
        if end <= start:
            end += 24 * 60
        return start, end, match.group(0)

    match = _CLOCK_RANGE_CN.search(text)
    if match:
        start = _clock_minutes(match.group("h1"), match.group("m1"), period)
        end = _clock_minutes(match.group("h2"), match.group("m2") or "", period)
        if start is None or end is None:
            return None
        if end <= start:
            end += 24 * 60
        return start, end, match.group(0)

    match = _CLOCK_SINGLE.search(text)
    if match:
        tail = text[match.end(): match.end() + 2]
        if any(marker in tail for marker in _CLOCK_FALSE_FOLLOWERS):
            return None
        point = _clock_minutes(match.group("h1"), match.group("m1"), period)
        if point is None:
            return None
        # 只给了一个钟点：按该点起 1 小时算，具体区间写进 label 供用户核对。
        return point, point + 60, match.group(0)

    return None


def _apply_period(minutes: int, period: str | None) -> int | None:
    """下午/晚上等语境下把 12 小时制换算成 24 小时制。"""
    if period in ("下午", "傍晚", "晚上", "夜里", "中午") and minutes < 12 * 60:
        minutes += 12 * 60
    if minutes >= 24 * 60:
        return None
    return minutes


def _build_period_options(
    period: str,
    anchor_date: date,
    tz: ZoneInfo,
    timezone: str,
) -> list[TimeWindowOption]:
    """为「昨天下午」这类表达生成具体候选时间段。"""
    options: list[TimeWindowOption] = []
    for index, (start_t, end_t) in enumerate(PERIOD_CANDIDATES.get(period, []), start=1):
        start = datetime.combine(anchor_date, start_t, tzinfo=tz)
        end = datetime.combine(anchor_date, end_t, tzinfo=tz)
        if end <= start:
            end += timedelta(days=1)
        options.append(
            TimeWindowOption(
                value=f"{anchor_date.isoformat()}|{start_t:%H:%M}|{end_t:%H:%M}",
                label=f"{anchor_date.isoformat()} {start_t:%H:%M}-{end_t:%H:%M}",
                timeWindow=_fixed_interval_window(start, end, timezone),
                description=f"{period}候选 {index}",
            )
        )
    return options
