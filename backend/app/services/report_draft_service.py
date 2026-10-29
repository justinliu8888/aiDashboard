"""组装机房能耗日报的策略草稿、定义草稿和修改草稿。

字段严格对齐 report-definition-v1.md 1.0；AI 层面只负责提供符合协议的结构，
真实值/口径/权限仍由 Java 后端和注册表校验。

设计要点（本轮修复的核心）：

    **路由层解析出来的结构化条件必须原样进入定义草稿。**

旧实现把时间窗、粒度、数据源、区域集合全部写死成
``_default_time_window_yesterday()`` / ``_hourly_trend_region()`` / ``_default_datasets()``，
于是「模型查到了、也提交了」的条件在组装这一步被默认值覆盖回去 ——
用户要过去 7 个完整自然日，定义里还是昨日；用户要 ``eas_energy_detail``，
定义里还是 ``catalog.machine_room.energy``。

现在 ``build_definition_draft()`` 接收一份**结构化计划**（数据源 / 粒度 / 时间窗 /
指标 / 过滤 / TopN / 派生列 / 条件样式 / 排除区域），逐项落到定义的对应位置；
凡是当前目录 / 指标 / 授权范围**不支持**的条件，抛 ``DefinitionBuildError``
明确阻断，绝不降级成"看起来能跑、其实是假数据"的草稿。

所有新增参数都是可选的，且缺省时行为与旧版完全一致（旧调用方无需改动）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.data.machine_room_energy import (
    CATALOG_MACHINE_ROOM_ENERGY,
    METRIC_DEVICE_ENERGY,
    METRIC_TOTAL_ENERGY,
)
from app.schemas.change_set import ChangeSet, ChangeSetEntry, ChangeSetUnchanged
from app.schemas.report_definition import ReportDefinitionDraft
from app.schemas.report_policy import ReportPolicyDraft
from app.services.mock_data_service import resolve_room_name


REGION_IDS = {
    "title": "region_report_title",
    "total": "region_total_energy",
    "ranking": "region_device_ranking",
    "trend": "region_hourly_trend",
    "quality": "region_data_quality",
    "analysis": "region_analysis",
}

# 组件键（业务区域）-> 区域 ID。只有这些键可以被请求或排除。
COMPONENT_KEYS = ("total", "ranking", "trend", "quality", "analysis")

# 组件键的中文 / 别名 -> 规范组件键。模型用自然语言表达时也能对上。
_COMPONENT_ALIASES: dict[str, str] = {
    "total": "total",
    "total_energy": "total",
    "totalenergy": "total",
    "summary": "total",
    "metric_card": "total",
    "metriccard": "total",
    "总耗电": "total",
    "总用电": "total",
    "总计": "total",
    "ranking": "ranking",
    "rank": "ranking",
    "device_ranking": "ranking",
    "deviceranking": "ranking",
    "table": "ranking",
    "设备排名": "ranking",
    "排名": "ranking",
    "trend": "trend",
    "trend_chart": "trend",
    "chart": "trend",
    "line": "trend",
    "趋势": "trend",
    "趋势图": "trend",
    "quality": "quality",
    "data_quality": "quality",
    "dataquality": "quality",
    "数据质量": "quality",
    "analysis": "analysis",
    "分析": "analysis",
    "分析段": "analysis",
    "分析说明": "analysis",
}

# 粒度 -> 趋势区域名
_GRAIN_REGION_NAMES = {
    "MINUTE": "分钟趋势",
    "HOUR": "小时趋势",
    "DAY": "日趋势",
    "WEEK": "周趋势",
    "MONTH": "月趋势",
}

# 派生列类型
DERIVED_PREVIOUS_PERIOD = "PREVIOUS_PERIOD"
DERIVED_GROWTH_RATE = "GROWTH_RATE"
DERIVED_SHARE_OF_TOTAL = "SHARE_OF_TOTAL"

_DERIVED_ALIASES = {
    "PREVIOUS_PERIOD": DERIVED_PREVIOUS_PERIOD,
    "PREVIOUS_DAY": DERIVED_PREVIOUS_PERIOD,
    "PREV": DERIVED_PREVIOUS_PERIOD,
    "LAST_PERIOD": DERIVED_PREVIOUS_PERIOD,
    "GROWTH_RATE": DERIVED_GROWTH_RATE,
    "GROWTH": DERIVED_GROWTH_RATE,
    "DAY_GROWTH_RATE": DERIVED_GROWTH_RATE,
    "GROWTH_PERCENT": DERIVED_GROWTH_RATE,
    "SHARE_OF_TOTAL": DERIVED_SHARE_OF_TOTAL,
    "SHARE": DERIVED_SHARE_OF_TOTAL,
    "PERCENT_OF_TOTAL": DERIVED_SHARE_OF_TOTAL,
    "OCCUPANCY": DERIVED_SHARE_OF_TOTAL,
}


class DefinitionBuildError(Exception):
    """确定性组装层拒绝组装。

    触发场景：未知数据源 / 未知指标 / 粒度不被数据源支持 /
    统计对象超出授权范围 / 请求了当前数据源支撑不了的组件。

    出现这个异常时**不能**退回默认草稿 —— 那样用户会拿到一份
    「看起来对、其实统计的是另一件事」的报表。必须阻断并说明原因。
    """

    def __init__(
        self,
        code: str,
        message: str,
        alternatives: list[str] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.alternatives = alternatives or []


def build_policy_draft(report_name: str) -> ReportPolicyDraft:
    return ReportPolicyDraft(
        name=report_name,
        description="由 AI 根据用户输入生成的首版策略草稿",
        subjectRef="machine_room",
        allowedOverrides=["TIME_WINDOW", "GRAIN"],
        executionMode="MANUAL_ONLY",
    )


# ---------- 时间窗 ----------

def _default_time_window_yesterday() -> dict[str, Any]:
    return _calendar_window(unit="DAY", count=1, offset=-1)


def _calendar_window(
    unit: str = "DAY",
    count: int = 1,
    offset: int | None = None,
    timezone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """相对时间窗（``runtime.logicalTime`` 为锚点）。

    ``offset`` 为 None 时按 ``-count`` 推导，即"过去 count 个完整自然日"
    （不含今天，从 D-count 到 D-1）。
    """
    if offset is None:
        offset = -count
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


# 自然语言时间表述 -> 相对窗口。顺序敏感：更具体的规则排在前面。
_TIME_TEXT_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(过去|最近|近)\s*(\d+)\s*个?\s*(完整)?\s*(自然)?\s*日"), "DAY"),
    (re.compile(r"(过去|最近|近)\s*(\d+)\s*(天|日)"), "DAY"),
    (re.compile(r"(过去|最近|近)\s*(\d+)\s*(小时|时)"), "HOUR"),
    (re.compile(r"(过去|最近|近)\s*(\d+)\s*(分钟|分)"), "MINUTE"),
    (re.compile(r"(过去|最近|近)\s*(\d+)\s*(周|星期)"), "WEEK"),
    (re.compile(r"(过去|最近|近)\s*(\d+)\s*(个月|月)"), "MONTH"),
]

# 绝对日期：2026-10-21 / 2026年10月21日
_ABSOLUTE_DATE_PATTERN = re.compile(
    r"(20\d{2})\s*[-年/]\s*(\d{1,2})\s*[-月/]\s*(\d{1,2})\s*日?"
)


def _parse_time_window_text(text: str) -> dict[str, Any] | None:
    """把「过去7个完整自然日」这类表述解析成相对窗口。"""
    normalized = (text or "").strip()
    if not normalized:
        return None
    for pattern, unit in _TIME_TEXT_RULES:
        match = pattern.search(normalized)
        if match:
            count = int(match.group(2))
            return {"unit": unit, "count": count, "offset": -count}
    absolute = _ABSOLUTE_DATE_PATTERN.search(normalized)
    if absolute:
        day = absolute.group(1).replace("年", "-").replace("月", "-").replace("日", "")
        parts = [p for p in day.split("-") if p]
        if len(parts) == 3:
            return {
                "type": "FIXED_INTERVAL",
                "date": "-".join(f"{int(p):02d}" if i else p for i, p in enumerate(parts)),
            }
    if re.search(r"(昨天|昨日)", normalized):
        return {"unit": "DAY", "count": 1, "offset": -1}
    if re.search(r"(前天)", normalized):
        return {"unit": "DAY", "count": 1, "offset": -2}
    if re.search(r"(今天|今日|本日)", normalized):
        return {"unit": "DAY", "count": 1, "offset": 0}
    if re.search(r"(明天|明日)", normalized):
        return {"unit": "DAY", "count": 1, "offset": 1}
    if re.search(r"(上周)", normalized):
        return {"unit": "WEEK", "count": 1, "offset": -1}
    if re.search(r"(本周|这周|这个星期)", normalized):
        return {"unit": "WEEK", "count": 1, "offset": 0}
    if re.search(r"(本月|这个月|当月)", normalized):
        return {"unit": "MONTH", "count": 1, "offset": 0}
    if re.search(r"(上月)", normalized):
        return {"unit": "MONTH", "count": 1, "offset": -1}
    return None


def _resolve_time_window(
    time_window: dict[str, Any] | str | None,
) -> dict[str, Any] | None:
    """把计划里的时间窗解析成统一的窗口描述。"""
    if time_window is None:
        return None
    if isinstance(time_window, str):
        return _parse_time_window_text(time_window)
    if not isinstance(time_window, dict) or not time_window:
        return None
    # 已经是协议里的窗口形状（mode/value）就直接取 value
    if time_window.get("type") == "CALENDAR" and "unit" in time_window:
        return {
            "type": "CALENDAR",
            "unit": str(time_window.get("unit", "DAY")).upper(),
            "count": int(time_window.get("count", 1) or 1),
            "offset": int(time_window.get("offset", 0) or 0),
            "timezone": time_window.get("timezone", "Asia/Shanghai"),
        }
    value = time_window.get("value")
    if isinstance(value, dict) and value.get("type") == "CALENDAR":
        return {
            "type": "CALENDAR",
            "unit": str(value.get("unit", "DAY")).upper(),
            "count": int(value.get("count", 1) or 1),
            "offset": int(value.get("offset", 0) or 0),
            "timezone": value.get("timezone", "Asia/Shanghai"),
        }
    if isinstance(value, dict) and value.get("type") == "FIXED_INTERVAL":
        return {
            "type": "FIXED_INTERVAL",
            "start": value.get("start"),
            "end": value.get("end"),
            "timezone": value.get("timezone", "Asia/Shanghai"),
        }
    if time_window.get("type") == "FIXED_INTERVAL":
        timezone = time_window.get("timezone", "Asia/Shanghai")
        if time_window.get("date") and not time_window.get("start"):
            day = str(time_window["date"])
            return {
                "type": "FIXED_INTERVAL",
                "start": f"{day}T00:00:00",
                "end": f"{day}T23:59:59",
                "timezone": timezone,
            }
        return {
            "type": "FIXED_INTERVAL",
            "start": time_window.get("start"),
            "end": time_window.get("end"),
            "timezone": timezone,
        }
    unit = str(time_window.get("unit", "DAY")).upper()
    count = int(time_window.get("count", 1) or 1)
    offset = time_window.get("offset")
    return {
        "type": "CALENDAR",
        "unit": unit,
        "count": count,
        "offset": int(offset) if offset is not None else None,
        "timezone": time_window.get("timezone", "Asia/Shanghai"),
    }


def resolve_time_window(
    time_window: dict[str, Any] | str | None,
) -> dict[str, Any] | None:
    """公开的时间窗解析入口。

    同一句「最近 7 天」在草稿组装与模板生成里必须落在**同一个窗口**上，
    否则用户看到的模板和设计对不上。所以这里公开一份，而不是让调用方
    各自复制一份解析逻辑。
    """
    return _resolve_time_window(time_window)


# ---------- 组件键归一 ----------

def _normalize_component(name: str) -> str | None:
    """把组件请求 / 排除区域归一成规范组件键。"""
    key = (name or "").strip()
    if not key:
        return None
    # 允许 "trend:DAY" 这种带粒度的写法
    if ":" in key:
        key = key.split(":", 1)[0].strip()
    lowered = key.lower()
    if lowered in _COMPONENT_ALIASES:
        return _COMPONENT_ALIASES[lowered]
    for component_key, region_id in REGION_IDS.items():
        if lowered in (component_key, region_id.lower()):
            return component_key
    return None


def _split_component_request(name: str) -> tuple[str | None, str | None]:
    """解析 "trend:DAY" -> ("trend", "DAY")。"""
    raw = (name or "").strip()
    if ":" in raw:
        base, _, suffix = raw.partition(":")
        suffix = suffix.strip().upper()
        return _normalize_component(base), (suffix or None)
    return _normalize_component(raw), None


# ---------- 规格 ----------

@dataclass
class _BuildSpec:
    """一次组装所需的全部确定性输入。"""

    report_name: str
    room_id: str
    source: dict[str, Any]
    dataset_id: str
    time_window: dict[str, Any]
    grain: str
    components: list[str]
    filters: list[dict[str, Any]] = field(default_factory=list)
    top_n: int = 10
    derived_columns: list[dict[str, Any]] = field(default_factory=list)
    style_hints: list[dict[str, Any]] = field(default_factory=list)
    total_metric: dict[str, Any] = field(default_factory=dict)
    device_metric: dict[str, Any] = field(default_factory=dict)
    capability_evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def source_ref(self) -> str:
        return str(self.source.get("sourceRef", ""))

    def region_time_window(self) -> dict[str, Any]:
        """区域级时间窗（协议形状）。"""
        window = self.time_window
        if window.get("type") == "FIXED_INTERVAL":
            return {
                "mode": "REPLACE",
                "value": {
                    "type": "FIXED_INTERVAL",
                    "timezone": window.get("timezone", "Asia/Shanghai"),
                    "start": window["start"],
                    "end": window["end"],
                },
            }
        return _calendar_window(
            unit=window["unit"],
            count=window["count"],
            offset=window.get("offset"),
            timezone=window.get("timezone", "Asia/Shanghai"),
        )


# ---------- 目录 / 指标 / 授权校验 ----------

def _resolve_source(source_ref: str | None) -> dict[str, Any]:
    """查数据源；查不到就阻断，不许悄悄退回默认数据源。"""
    from app.services.catalog_mock_service import get_catalog

    if not source_ref:
        return CATALOG_MACHINE_ROOM_ENERGY
    catalog = get_catalog(source_ref)
    if catalog is None:
        raise DefinitionBuildError(
            "SOURCE_REF_UNKNOWN",
            f"数据源 {source_ref} 不在已登记的目录里，无法据此生成报表。",
            alternatives=[
                "catalog.machine_room.energy（机房能耗，默认）",
                "eas_energy_detail（EAS 能耗明细）",
            ],
        )
    return catalog


def _resolve_room(room_id: str) -> str:
    """机房必须在授权范围内。"""
    from app.services.scope_service import is_subject_authorized

    normalized = (room_id or "").strip()
    if not normalized:
        raise DefinitionBuildError(
            "SCOPE_NOT_AUTHORIZED",
            "没有指定统计对象，当前账号可统计的机房是 A 机房。",
            alternatives=["A 机房（room_a）"],
        )
    if not is_subject_authorized("machine_room", normalized):
        raise DefinitionBuildError(
            "SCOPE_NOT_AUTHORIZED",
            f"当前账号没有 {normalized} 的授权范围，无法生成跨范围的能耗报表。",
            alternatives=["A 机房（room_a）"],
        )
    return normalized


def _resolve_metrics(metric_refs: list[str] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """解析指标引用；未登记的指标必须阻断（M-15：不能拿耗电量冒充电费/心情值）。"""
    from app.services.metric_mock_service import get_metric

    total_metric = dict(METRIC_TOTAL_ENERGY)
    device_metric = dict(METRIC_DEVICE_ENERGY)

    refs = [r for r in (metric_refs or []) if r]
    for ref in refs:
        if get_metric(ref) is None:
            raise DefinitionBuildError(
                "METRIC_NOT_REGISTERED",
                f"指标 {ref} 没有登记，无法计算。",
                alternatives=[
                    "metric.machine_room.total_energy（总耗电）",
                    "metric.machine_room.device_energy（设备耗电）",
                ],
            )

    for ref in refs:
        if "total_energy" in ref:
            total_metric = dict(get_metric(ref) or total_metric)
        elif "device_energy" in ref:
            device_metric = dict(get_metric(ref) or device_metric)

    extra = [r for r in refs if r not in (total_metric["metricRef"], device_metric["metricRef"])]
    if extra:
        # 登记了但当前模板还不支持的指标：明确说明，不要假装算了。
        raise DefinitionBuildError(
            "METRIC_NOT_SUPPORTED",
            f"指标 {extra[0]} 虽然已登记，但当前机房能耗模板还不能按它出数。",
            alternatives=[
                "metric.machine_room.total_energy（总耗电）",
                "metric.machine_room.device_energy（设备耗电）",
            ],
        )
    return total_metric, device_metric


def _resolve_grain(source: dict[str, Any], grain_unit: str | None) -> str:
    """粒度必须被数据源支持。"""
    supported = list(source.get("supportedGrains", []))
    if not grain_unit:
        return "HOUR" if "HOUR" in supported else (supported[0] if supported else "HOUR")
    grain = str(grain_unit).strip().upper()
    if grain not in supported:
        raise DefinitionBuildError(
            "GRAIN_NOT_SUPPORTED",
            f"数据源 {source.get('sourceRef')} 不支持 {grain} 粒度，"
            f"它支持的是 {'、'.join(supported) or '（无）'}。",
            alternatives=[f"{g} 粒度" for g in supported],
        )
    return grain


def _normalize_filters(filters: list[Any] | None) -> list[dict[str, Any]]:
    """归一过滤条件。"""
    normalized: list[dict[str, Any]] = []
    for item in filters or []:
        if not isinstance(item, dict):
            continue
        field_ref = str(
            item.get("fieldRef") or item.get("field") or item.get("fieldName") or ""
        ).strip()
        if not field_ref:
            continue
        op = str(item.get("op") or item.get("operator") or "IN").strip().upper()
        # 路由层可能给 "NOT IN"，统一成 "NOT_IN"
        op = op.replace(" ", "_").replace("-", "_")
        values = item.get("values")
        if values is None:
            values = item.get("value")
        if values is None:
            values = []
        if isinstance(values, (str, int, float)):
            values = [values]
        normalized.append(
            {
                "fieldRef": field_ref,
                "op": op,
                "values": [str(v) for v in values],
            }
        )
    return normalized


_DERIVED_CANONICAL_FIELD_REF = {
    DERIVED_PREVIOUS_PERIOD: "prevPeriodEnergyKwh",
    DERIVED_GROWTH_RATE: "growthRatePercent",
    DERIVED_SHARE_OF_TOTAL: "energySharePercent",
}


def _normalize_derived_columns(
    derived_columns: list[Any] | None,
    field_aliases: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """归一派生列定义。

    派生列的 fieldRef 由 kind 决定，不采信模型自己写的名字：
    模型经常把 fieldRef 写成源字段（例如 energyKwh），那样派生列会和基础列
    重名，真正的派生列（prevPeriodEnergyKwh / growthRatePercent /
    energySharePercent）根本不会出现，条件样式引用的字段就取不到，
    整份报表会在校验阶段被判失败。派生列的字段名同时被 DERIVE 节点
    （baselineFieldRef / denominatorFieldRef）和预览层按 kind 硬编码引用，
    必须保持唯一且稳定。

    模型自定义过的别名会记进 field_aliases，供条件样式改写引用。
    """
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(derived_columns or []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or item.get("type") or "").strip().upper()
        kind = _DERIVED_ALIASES.get(kind, kind)
        if kind not in (
            DERIVED_PREVIOUS_PERIOD,
            DERIVED_GROWTH_RATE,
            DERIVED_SHARE_OF_TOTAL,
        ):
            raise DefinitionBuildError(
                "DERIVED_COLUMN_NOT_SUPPORTED",
                f"派生列类型 {kind or '（空）'} 不支持，"
                f"当前支持 {DERIVED_PREVIOUS_PERIOD}、{DERIVED_GROWTH_RATE}、"
                f"{DERIVED_SHARE_OF_TOTAL}。",
                alternatives=["前一日值", "日增长率", "占机房总耗电比例"],
            )
        canonical_ref = _DERIVED_CANONICAL_FIELD_REF[kind]
        requested_ref = str(
            item.get("fieldRef") or item.get("asField") or item.get("field") or ""
        ).strip()
        if requested_ref and requested_ref != canonical_ref and field_aliases is not None:
            # 记住模型的别名：条件样式可能引用的是它，要一起改写过来。
            field_aliases.setdefault(requested_ref, canonical_ref)
        field_ref = canonical_ref
        label = str(item.get("label") or item.get("name") or "").strip()
        if not label:
            label = {
                DERIVED_PREVIOUS_PERIOD: "前一日耗电 (kWh)",
                DERIVED_GROWTH_RATE: "日增长率 (%)",
                DERIVED_SHARE_OF_TOTAL: "占机房总耗电比例 (%)",
            }[kind]
        fmt = item.get("format") or {}
        if not isinstance(fmt, dict) or not fmt:
            fmt = {
                DERIVED_PREVIOUS_PERIOD: {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
                DERIVED_GROWTH_RATE: {"kind": "PERCENT", "precision": 1, "unitLabel": "%"},
                DERIVED_SHARE_OF_TOTAL: {"kind": "PERCENT", "precision": 1, "unitLabel": "%"},
            }[kind]
        normalized.append(
            {
                "id": str(item.get("id") or item.get("columnId") or f"col_derived_{index + 1}"),
                "label": label,
                "fieldRef": field_ref,
                "kind": kind,
                "sourceFieldRef": str(item.get("sourceFieldRef") or "energyKwh"),
                "format": fmt,
            }
        )
    return normalized


#: 模型把版式/样式诉求塞进 styleHints 时用的字段名 → 用户能看懂的说法。
_STYLE_REQUEST_LABELS = {
    "layout.totalCardFullRow": "总耗电卡片独占一行",
    "layout.rankingTrendEqualSplit": "排名表与图表左右等宽",
    "layout.columnSpan": "区块列宽",
    "layout.placement": "区块版式",
    "title.fontSize": "标题字号",
    "title.align": "标题对齐方式",
    "title.color": "标题颜色",
    "title.style": "标题样式",
    "chart.type": "图形类型（如柱状图）",
    "chart.kind": "图形类型（如柱状图）",
    "chart.color": "图表配色",
}

#: 模型写的 styleHints 字段名 → 统一诉求类型。
#: 用来和路由层确定性识别到的结果去重：同一个诉求只披露一次。
_STYLE_REQUEST_KINDS = {
    "layout.totalCardFullRow": "LAYOUT_FULL_ROW",
    "layout.rankingTrendEqualSplit": "LAYOUT_SIDE_BY_SIDE",
    "layout.columnSpan": "LAYOUT_COLUMN_SPAN",
    "layout.placement": "LAYOUT_PLACEMENT",
    "title.fontSize": "TITLE_FONT_SIZE",
    "title.align": "TITLE_ALIGN",
    "title.color": "TITLE_COLOR",
    "title.style": "TITLE_STYLE",
    "chart.type": "CHART_KIND",
    "chart.kind": "CHART_KIND",
    "chart.color": "CHART_COLOR",
}


def _style_request_kind(field_ref: str) -> str:
    """模型写的字段名 → 统一诉求类型。"""
    ref = (field_ref or "").strip()
    if ref in _STYLE_REQUEST_KINDS:
        return _STYLE_REQUEST_KINDS[ref]
    if ref.startswith("layout."):
        return "LAYOUT"
    if ref.startswith("title."):
        return "TITLE"
    if ref.startswith("chart."):
        return "CHART"
    return ref


def _merge_unsupported_style_requests(
    detected: list[Any] | None,
    rejected: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """把「确定性识别到的」与「模型写进 styleHints 被拒的」合成一份清单。

    两处来源可能指向同一个诉求（模型可能同时写了 ``title.fontSize``），
    按统一诉求类型去重：确定性识别到的更具体（带用户原话片段），排前面。
    """
    normalized: list[dict[str, Any]] = []
    for item in detected or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        kind = str(item.get("kind") or "").strip()
        if not label and not kind:
            continue
        entry = dict(item)
        entry.setdefault("source", "detected")
        normalized.append(entry)

    for item in rejected or []:
        if not isinstance(item, dict):
            continue
        field_ref = str(item.get("fieldRef") or "").strip()
        label = str(item.get("label") or "").strip()
        current = str(item.get("currentBehavior") or "").strip()
        if not label:
            # 没登记过的字段名：别把生硬的字段名直接甩给用户，
            # 说清楚是「一条条件样式没生效」以及为什么。
            label = f"条件样式（{field_ref}）" if field_ref else "条件样式"
        if not current:
            # 装配层拒掉的条件样式：说明「没生效」和「为什么」。
            current = (
                f"当前报表里拿不到字段 {field_ref}，这条条件样式没有生效"
                if field_ref
                else "当前报表里没有可作用的条件字段，这条条件样式没有生效"
            )
        normalized.append(
            {
                "kind": _style_request_kind(field_ref),
                "label": label,
                "fieldRef": field_ref,
                "currentBehavior": current,
                "source": "model_style_hint",
            }
        )

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in normalized:
        key = str(entry.get("kind") or entry.get("label") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    return merged


def compose_unsupported_style_disclosure(
    requests: list[dict[str, Any]] | None,
) -> str:
    """把「没生效的版式/样式诉求」写成用户看得懂的说明。

    关键点：说清楚**没有生效**，而不是说「已提交」「已一并处理」——
    后者会被读成已经做到了，正是本轮要修的偏离。
    """
    items = [item for item in (requests or []) if isinstance(item, dict)]
    if not items:
        return ""
    lines = []
    for index, item in enumerate(items, start=1):
        label = str(item.get("label") or item.get("fieldRef") or "未命名诉求").strip()
        current = str(item.get("currentBehavior") or "").strip()
        lines.append(
            f"{index}. {label}" + (f" —— {current}" if current else "")
        )
    count = len(items)
    return (
        f"⚠️ 版式/样式说明：报表已按你指定的范围、时间窗口和数据口径生成，"
        f"但以下 {count} 项版式/样式要求**当前模板不支持、没有生效**（已按默认版式生成）：\n"
        + "\n".join(lines)
    )


def _describe_style_request(field_ref: str) -> str:
    """把模型写的字段名翻译成用户看得懂的诉求。"""
    if field_ref in _STYLE_REQUEST_LABELS:
        return _STYLE_REQUEST_LABELS[field_ref]
    if field_ref.startswith("layout."):
        return "版式（%s）" % field_ref.split(".", 1)[1]
    if field_ref.startswith("title."):
        return "标题样式（%s）" % field_ref.split(".", 1)[1]
    if field_ref.startswith("chart."):
        return "图表样式（%s）" % field_ref.split(".", 1)[1]
    # 未登记的字段名：多半是模型指向了拿不到的数据字段（例如派生列没声明），
    # 不能把生硬的字段名直接甩给用户。
    return "条件样式（%s）" % field_ref if field_ref else "条件样式"


def _normalize_style_hints(
    style_hints: list[Any] | None,
    field_aliases: dict[str, str] | None = None,
    available_fields: set[str] | None = None,
    rejected: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """归一条件样式。

    field_aliases 是派生列别名映射：模型可能把条件样式指向它自己写的
    派生列名字，而派生列最终落地的字段名是规范名，这里跟着改写，
    否则样式引用的字段在报表里根本不存在，校验会判失败。

    available_fields 是这块报表真正拿得到的字段。指向拿不到的字段的条件样式
    会被**拒绝**而不是原样塞进定义：styleHints 是「行级条件样式」通道，
    模型拿它来传版式/样式诉求（title.fontSize、layout.xxx）时，
    原样透传会让整份报表在校验阶段判失败、连预览都出不来。
    被拒的诉求要记下来明确告诉用户，不能悄悄丢掉也不能假装做到了。
    """
    normalized: list[dict[str, Any]] = []
    for item in style_hints or []:
        if not isinstance(item, dict):
            continue
        field_ref = str(
            item.get("fieldRef") or item.get("field") or item.get("columnRef") or ""
        ).strip()
        if not field_ref:
            continue
        if field_aliases:
            field_ref = field_aliases.get(field_ref, field_ref)
        rule = str(item.get("rule") or "HIGHLIGHT_ABOVE_THRESHOLD").strip().upper()
        comparison = str(item.get("comparison") or item.get("op") or "GT").strip().upper()
        threshold = item.get("threshold")
        if threshold is None:
            threshold = item.get("value", 0)
        try:
            threshold_value = float(threshold)
        except (TypeError, ValueError):
            threshold_value = 0.0
        if available_fields is not None and field_ref not in available_fields:
            # 拿不到的字段：拒绝这条条件样式，并记下它代表什么诉求。
            if rejected is not None:
                rejected.append(
                    {
                        "fieldRef": field_ref,
                        "label": _describe_style_request(field_ref),
                    }
                )
            continue
        normalized.append(
            {
                "targetRegionId": str(
                    item.get("targetRegionId") or item.get("regionId") or ""
                ),
                "rule": rule,
                "fieldRef": field_ref,
                "comparison": comparison,
                "threshold": threshold_value,
                "style": str(item.get("style") or "RED").strip().upper(),
            }
        )
    return normalized


# ---------- 区域构造 ----------

def _title_region(room_id: str, spec: _BuildSpec) -> dict[str, Any]:
    """报告标题区。

    title 直接给出可读的真实标题；template 里保留同名占位，后端渲染时会用
    真实值覆盖，两边不会打架。

    标题跟着这次组装的真实口径走：默认（昨日）保持「A 机房 昨日能耗日报」，
    窗口或粒度变了（近 7 个自然日 / 分钟粒度）就跟着改，
    不能出现「窗口是 7 天、标题还写昨日」这种自相矛盾的定义。
    """
    title = spec.report_name.strip() or f"{resolve_room_name(room_id)} 昨日能耗日报"
    return {
        "id": REGION_IDS["title"],
        "name": title,
        "role": "REPORT_TITLE",
        "layout": {
            "placement": {"type": "GRID", "column": 1, "columnSpan": 12}
        },
        "component": {
            "type": "TEXT",
            "content": {
                "format": "PLAIN",
                "template": title,
                "bindings": {
                    "roomName": {"type": "CONTEXT", "path": "scope.machineRoomName"}
                },
            },
            "headingLevel": 1,
        },
    }


def _filter_node(spec: _BuildSpec, node_id: str, inputs: list[str]) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "FILTER",
        "inputs": list(inputs),
        "conditions": [dict(f) for f in spec.filters],
    }


def _total_energy_region(spec: _BuildSpec) -> dict[str, Any]:
    metric = spec.total_metric
    unit = metric.get("unit", "kWh")
    nodes: list[dict[str, Any]] = []
    total_inputs = ["input:main"]
    if spec.filters:
        # 「只统计 CRAC-01/CRAC-02」这种过滤对整张报表生效，
        # 总耗电卡片也必须只算被选中的设备，否则卡片和表格口径不一致。
        nodes.append(_filter_node(spec, "n_filter", total_inputs))
        total_inputs = ["node:n_filter"]
    nodes.append(
        {
            "id": "n_total",
            "type": "METRIC",
            "inputs": total_inputs,
            "metricRef": metric["metricRef"],
            "metricVersion": metric["metricVersion"],
        }
    )
    return {
        "id": REGION_IDS["total"],
        "name": "总耗电",
        "role": "SUMMARY",
        "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 4}},
        "context": {"timeWindow": spec.region_time_window()},
        "data": {
            "inputs": {
                "main": {
                    "type": "DATASET",
                    "datasetRef": spec.dataset_id,
                    "periodRef": "CURRENT",
                }
            },
            "pipeline": {
                "nodes": nodes,
                "outputs": {
                    "main": {
                        "nodeRef": "node:n_total",
                        "expectedFields": {
                            "energyKwh": {"dataType": "DECIMAL", "unit": unit}
                        },
                    }
                },
            },
        },
        "binding": {"outputRef": "output:main"},
        "component": {
            "type": "METRIC_CARD",
            "metric": {
                "id": "m_total",
                "label": "总耗电",
                "fieldRef": "energyKwh",
                "format": {"kind": "NUMBER", "precision": 1, "unitLabel": unit},
            },
        },
    }


def _ranking_region(spec: _BuildSpec) -> dict[str, Any]:
    metric = spec.device_metric
    unit = metric.get("unit", "kWh")

    nodes: list[dict[str, Any]] = []
    device_inputs = ["input:main"]
    if spec.filters:
        nodes.append(_filter_node(spec, "n_filter", device_inputs))
        device_inputs = ["node:n_filter"]
    nodes.append(
        {
            "id": "n_device",
            "type": "GROUP_AGGREGATE",
            "inputs": device_inputs,
            "groupBy": ["deviceId", "deviceName"],
            "measures": [
                {
                    "asField": "energyKwh",
                    "aggregation": metric.get("aggregation", "SUM"),
                    "fieldRef": metric.get("fieldRef", "energyKwh"),
                }
            ],
        }
    )
    nodes.append(
        {
            "id": "n_sort",
            "type": "SORT",
            "inputs": ["node:n_device"],
            "orderBy": [{"fieldRef": "energyKwh", "direction": "DESC"}],
        }
    )
    nodes.append(
        {
            "id": "n_limit",
            "type": "LIMIT",
            "inputs": ["node:n_sort"],
            "count": spec.top_n,
        }
    )
    tail_node = "node:n_limit"

    columns: list[dict[str, Any]] = [
        {
            "id": "col_device",
            "label": "设备",
            "fieldRef": "deviceName",
            "format": {"kind": "TEXT"},
        },
        {
            "id": "col_energy",
            "label": "耗电 (kWh)",
            "fieldRef": "energyKwh",
            "format": {"kind": "NUMBER", "precision": 1, "unitLabel": unit},
        },
    ]
    binding: dict[str, Any] = {
        "outputRef": "output:main",
        "topN": spec.top_n,
        "sort": [{"fieldRef": "energyKwh", "direction": "DESC"}],
    }

    if spec.derived_columns:
        derive_columns: list[dict[str, Any]] = []
        for column in spec.derived_columns:
            derive_columns.append(
                {
                    "asField": column["fieldRef"],
                    "kind": column["kind"],
                    "sourceFieldRef": column["sourceFieldRef"],
                    "label": column["label"],
                    "format": column["format"],
                }
            )
            if column["kind"] == DERIVED_SHARE_OF_TOTAL:
                derive_columns[-1]["denominatorOutputRef"] = (
                    f"region:{REGION_IDS['total']}/output:main"
                )
                derive_columns[-1]["denominatorFieldRef"] = "energyKwh"
            if column["kind"] == DERIVED_GROWTH_RATE:
                derive_columns[-1]["baselineKind"] = DERIVED_PREVIOUS_PERIOD
                derive_columns[-1]["baselineFieldRef"] = "prevPeriodEnergyKwh"
            columns.append(
                {
                    "id": column["id"],
                    "label": column["label"],
                    "fieldRef": column["fieldRef"],
                    "format": column["format"],
                }
            )
        nodes.append(
            {
                "id": "n_derive",
                "type": "DERIVE",
                "inputs": [tail_node],
                "columns": derive_columns,
            }
        )
        tail_node = "node:n_derive"
        binding["derivedColumns"] = [dict(c) for c in spec.derived_columns]

    region: dict[str, Any] = {
        "id": REGION_IDS["ranking"],
        "name": "设备耗电排名",
        "role": "DETAIL",
        "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12}},
        "context": {"timeWindow": spec.region_time_window()},
        "data": {
            "inputs": {
                "main": {
                    "type": "DATASET",
                    "datasetRef": spec.dataset_id,
                    "periodRef": "CURRENT",
                }
            },
            "pipeline": {
                "nodes": nodes,
                "outputs": {
                    "main": {
                        "nodeRef": tail_node,
                        "expectedFields": {
                            "deviceId": {"dataType": "STRING"},
                            "deviceName": {"dataType": "STRING"},
                            "energyKwh": {"dataType": "DECIMAL", "unit": unit},
                        },
                    }
                },
            },
        },
        "binding": binding,
        "component": {
            "type": "TABLE",
            "columns": columns,
            "rowNumber": True,
        },
    }
    if spec.style_hints:
        region["component"]["styleHints"] = [dict(h) for h in spec.style_hints]
    return region


def _trend_region(spec: _BuildSpec) -> dict[str, Any]:
    grain = spec.grain
    metric = spec.total_metric
    unit = metric.get("unit", "kWh")
    region_name = _GRAIN_REGION_NAMES.get(grain, "趋势")

    nodes: list[dict[str, Any]] = []
    trend_inputs = ["input:main"]
    if spec.filters:
        nodes.append(_filter_node(spec, "n_filter", trend_inputs))
        trend_inputs = ["node:n_filter"]
    nodes.append(
        {
            "id": "n_resample",
            "type": "RESAMPLE",
            "inputs": trend_inputs,
            "timeFieldRef": "timestamp",
            "grain": {"unit": grain},
            "measures": [
                {
                    "asField": "energyKwh",
                    "aggregation": metric.get("aggregation", "SUM"),
                    "fieldRef": metric.get("fieldRef", "energyKwh"),
                }
            ],
        }
    )

    return {
        "id": REGION_IDS["trend"],
        "name": region_name,
        "role": "TREND",
        "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12}},
        "context": {
            "timeWindow": spec.region_time_window(),
            "grain": {"mode": "REPLACE", "value": {"unit": grain}},
        },
        "data": {
            "inputs": {
                "main": {
                    "type": "DATASET",
                    "datasetRef": spec.dataset_id,
                    "periodRef": "CURRENT",
                }
            },
            "pipeline": {
                "nodes": nodes,
                "outputs": {
                    "main": {
                        "nodeRef": "node:n_resample",
                        "expectedFields": {
                            "timestamp": {"dataType": "DATETIME"},
                            "energyKwh": {"dataType": "DECIMAL", "unit": unit},
                        },
                    }
                },
            },
        },
        "binding": {"outputRef": "output:main"},
        "component": {
            "type": "CHART",
            "chartKind": "LINE",
            "encodings": [
                {"channel": "X", "fieldRef": "timestamp"},
                {"channel": "Y", "fieldRef": "energyKwh"},
            ],
            "legend": True,
        },
    }


def _quality_region(spec: _BuildSpec) -> dict[str, Any]:
    return {
        "id": REGION_IDS["quality"],
        "name": "数据质量",
        "role": "QUALITY",
        "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 6}},
        "component": {
            "type": "DATA_QUALITY",
            "fields": ["COVERAGE", "MISSING", "FRESHNESS"],
            "showDetails": True,
        },
    }


def _analysis_region(spec: _BuildSpec) -> dict[str, Any]:
    """分析段。facts 只引用**实际存在**的区域，避免出现悬空的 outputRef。"""
    facts: list[dict[str, Any]] = []
    if "total" in spec.components:
        facts.append(
            {
                "name": "total_energy",
                "outputRef": f"region:{REGION_IDS['total']}/output:main",
                "fieldRefs": ["energyKwh"],
            }
        )
    if "ranking" in spec.components:
        facts.append(
            {
                "name": "device_ranking",
                "outputRef": f"region:{REGION_IDS['ranking']}/output:main",
                "fieldRefs": ["deviceId", "deviceName", "energyKwh"],
            }
        )
    if "trend" in spec.components:
        facts.append(
            {
                "name": "energy_trend",
                "outputRef": f"region:{REGION_IDS['trend']}/output:main",
                "fieldRefs": ["timestamp", "energyKwh"],
            }
        )
    goal = "基于总耗电、设备排名和小时趋势，解释关键变化"
    if "trend" not in spec.components:
        goal = "基于总耗电和设备排名，解释关键变化"
    return {
        "id": REGION_IDS["analysis"],
        "name": "分析说明",
        "role": "ANALYSIS",
        "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12}},
        "component": {
            "type": "ANALYSIS",
            "mode": "LLM",
            "goal": goal,
            "facts": facts,
            "promptTemplateRef": "prompt.machine_room.energy_daily_analysis",
            "requireEvidence": True,
        },
    }


# ---------- 数据集 / 意图绑定 ----------

def _dataset_for(source: dict[str, Any], dataset_id: str) -> dict[str, Any]:
    """按数据源的真实字段生成数据集声明（不再写死一个数据源）。"""
    fields: dict[str, Any] = {}
    for name, spec in (source.get("fields") or {}).items():
        field = {
            "sourceFieldRef": name,
            "dataType": spec.get("dataType", "STRING"),
        }
        if spec.get("semanticType"):
            field["semanticType"] = spec["semanticType"]
        if spec.get("unit"):
            field["unit"] = spec["unit"]
        fields[name] = field
    return {
        "sourceRef": source["sourceRef"],
        "sourceVersion": source["sourceVersion"],
        "subjectRef": source["subjectRef"],
        "fields": fields,
    }


def _default_datasets() -> dict[str, Any]:
    return {
        "ds_room_energy": _dataset_for(CATALOG_MACHINE_ROOM_ENERGY, "ds_room_energy")
    }


def _intent_bindings(spec: _BuildSpec) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    if "total" in spec.components:
        bindings.append(
            {
                "id": "intent_total_energy",
                "sourceText": "总耗电",
                "domain": "METRIC",
                "targetRegionIds": [REGION_IDS["total"]],
                "targetPaths": [f"/regions/{REGION_IDS['total']}"],
                "status": "RESOLVED",
            }
        )
    if "ranking" in spec.components:
        bindings.append(
            {
                "id": "intent_device_ranking",
                "sourceText": "设备排名",
                "domain": "METRIC",
                "targetRegionIds": [REGION_IDS["ranking"]],
                "targetPaths": [f"/regions/{REGION_IDS['ranking']}"],
                "status": "RESOLVED",
            }
        )
    if "trend" in spec.components:
        trend_intent = (
            "intent_hourly_trend" if spec.grain == "HOUR" else f"intent_{spec.grain.lower()}_trend"
        )
        bindings.append(
            {
                "id": trend_intent,
                "sourceText": "趋势分析",
                "domain": "CALCULATION",
                "targetRegionIds": [REGION_IDS["trend"]],
                "targetPaths": [f"/regions/{REGION_IDS['trend']}"],
                "status": "RESOLVED",
            }
        )
    if spec.filters:
        bindings.append(
            {
                "id": "intent_filters",
                "sourceText": "、".join(
                    f"{f['fieldRef']} {f['op']} {','.join(f['values'])}" for f in spec.filters
                ),
                "domain": "FILTER",
                "targetRegionIds": [
                    REGION_IDS[key] for key in spec.components if key in REGION_IDS
                ],
                "targetPaths": [
                    f"/regions/{REGION_IDS[key]}/data/pipeline" for key in spec.components
                ],
                "status": "RESOLVED",
            }
        )
    if spec.derived_columns:
        bindings.append(
            {
                "id": "intent_derived_columns",
                "sourceText": "、".join(c["label"] for c in spec.derived_columns),
                "domain": "CALCULATION",
                "targetRegionIds": [REGION_IDS["ranking"]],
                "targetPaths": [f"/regions/{REGION_IDS['ranking']}/binding/derivedColumns"],
                "status": "RESOLVED",
            }
        )
    return bindings


# ---------- 组装入口 ----------

def _resolve_components(
    component_requests: list[str] | None,
    excluded_regions: list[str] | None,
    include_trend: bool,
) -> list[str]:
    """决定要生成哪些业务区域。

    - ``componentRequests`` 非空 → 白名单，用户没点名的一律不生成（M-03）。
    - ``excludedRegions`` 非空 → 黑名单，用户说不要的一律不生成。
    - 两者都空 → 沿用旧行为（total + ranking + [trend] + quality + analysis）。
    """
    if component_requests:
        selected: list[str] = []
        for raw in component_requests:
            key, _grain = _split_component_request(raw)
            if key and key not in selected:
                selected.append(key)
        unknown = [
            raw
            for raw in component_requests
            if _split_component_request(raw)[0] is None
        ]
        if unknown:
            raise DefinitionBuildError(
                "COMPONENT_NOT_SUPPORTED",
                f"不支持的报表区块：{'、'.join(unknown)}。",
                alternatives=list(COMPONENT_KEYS),
            )
        # 白名单按固定顺序渲染，保证同一份计划每次渲染顺序一致
        components = [key for key in COMPONENT_KEYS if key in selected]
        if not components:
            raise DefinitionBuildError(
                "COMPONENT_NOT_SUPPORTED",
                "报表里至少要有一个数据区块。",
                alternatives=list(COMPONENT_KEYS),
            )
    else:
        components = ["total", "ranking"]
        if include_trend:
            components.append("trend")
        components.extend(["quality", "analysis"])

    if excluded_regions:
        excluded: list[str] = []
        for raw in excluded_regions:
            key, _grain = _split_component_request(raw)
            if key is None:
                raise DefinitionBuildError(
                    "COMPONENT_NOT_SUPPORTED",
                    f"不支持的报表区块：{raw}。",
                    alternatives=list(COMPONENT_KEYS),
                )
            if key not in excluded:
                excluded.append(key)
        components = [key for key in components if key not in excluded]

    if not components:
        raise DefinitionBuildError(
            "COMPONENT_NOT_SUPPORTED",
            "按用户的要求排除了所有数据区块，报表没有可展示的内容。",
            alternatives=list(COMPONENT_KEYS),
        )
    return components


def build_definition_draft(
    report_name: str,
    room_id: str,
    include_trend: bool = True,
    *,
    sourceRef: str | None = None,
    grainUnit: str | None = None,
    timeWindow: dict[str, Any] | str | None = None,
    timeRange: dict[str, Any] | str | None = None,
    metricRefs: list[str] | None = None,
    componentRequests: list[str] | None = None,
    excludedRegions: list[str] | None = None,
    filters: list[Any] | None = None,
    limit: int | None = None,
    topN: int | None = None,
    derivedColumns: list[Any] | None = None,
    styleHints: list[Any] | None = None,
    unsupportedStyleRequests: list[Any] | None = None,
    capabilityEvidence: dict[str, Any] | None = None,
) -> ReportDefinitionDraft:
    """按结构化计划组装合法的 ReportDefinitionDraft。

    参数命名与路由层槽位、接口协议保持一致（camelCase）：
    ``sourceRef / grainUnit / timeWindow / metricRefs / componentRequests /
    excludedRegions / filters / limit / topN / derivedColumns / styleHints /
    unsupportedStyleRequests / capabilityEvidence``。``timeRange`` 是
    ``timeWindow`` 的别名
    （路由层的槽位名就是 timeRange，两者都收）。

    所有新参数都是可选的；不传时行为与旧版完全一致。
    条件不被目录 / 指标 / 授权范围支持时抛 ``DefinitionBuildError``，
    绝不静默降级成另一份"看起来能跑"的草稿。
    """
    # ---- 1. 先把条件解析成确定性的规格，解析不了就直接阻断 ----
    resolved_room = _resolve_room(room_id)
    source = _resolve_source(sourceRef)
    total_metric, device_metric = _resolve_metrics(metricRefs)
    components = _resolve_components(
        componentRequests, excludedRegions, include_trend
    )

    window = _resolve_time_window(timeWindow if timeWindow is not None else timeRange) or {
        "unit": "DAY",
        "count": 1,
        "offset": -1,
        "timezone": "Asia/Shanghai",
    }

    # 粒度：显式给的优先；没给且窗口跨天 → 日粒度；否则小时。
    explicit_grain = grainUnit
    if not explicit_grain:
        for raw in componentRequests or []:
            _key, suffix = _split_component_request(raw)
            if suffix:
                explicit_grain = suffix
                break
    if explicit_grain:
        grain = _resolve_grain(source, explicit_grain)
    elif window.get("type") == "FIXED_INTERVAL":
        grain = _resolve_grain(source, "DAY")
    elif window["unit"] == "DAY" and window.get("count", 1) > 1:
        grain = _resolve_grain(source, "DAY")
    else:
        grain = _resolve_grain(source, None)

    # 数据源支撑不了的组件：直接阻断，不假装能算。
    source_fields = set((source.get("fields") or {}).keys())
    if "ranking" in components and not {"deviceId", "deviceName"} <= source_fields:
        raise DefinitionBuildError(
            "COMPONENT_NOT_SUPPORTED",
            f"数据源 {source['sourceRef']} 没有设备维度，无法出设备排名。",
            alternatives=["去掉设备排名区块", "改用带设备维度的数据源"],
        )
    if "trend" in components and "timestamp" not in source_fields:
        raise DefinitionBuildError(
            "COMPONENT_NOT_SUPPORTED",
            f"数据源 {source['sourceRef']} 没有时间字段，无法出趋势。",
            alternatives=["去掉趋势区块"],
        )

    normalized_filters = _normalize_filters(filters)
    for spec in normalized_filters:
        if spec["fieldRef"] not in source_fields:
            raise DefinitionBuildError(
                "FILTER_FIELD_UNKNOWN",
                f"数据源 {source['sourceRef']} 没有字段 {spec['fieldRef']}，无法按它过滤。",
                alternatives=sorted(source_fields),
            )

    derived_field_aliases: dict[str, str] = {}
    normalized_derived = _normalize_derived_columns(derivedColumns, derived_field_aliases)
    # 条件样式只能作用在这块报表真正拿得到的字段上。
    # 可用字段 = 数据源字段 ∪ 派生列字段（与校验层口径一致）。
    style_available_fields = set(source_fields) | {
        column["fieldRef"] for column in normalized_derived
    }
    rejected_style_requests: list[dict[str, Any]] = []
    normalized_styles = _normalize_style_hints(
        styleHints,
        derived_field_aliases,
        style_available_fields,
        rejected_style_requests,
    )
    resolved_top_n = int(topN or limit or 10)
    if resolved_top_n <= 0:
        raise DefinitionBuildError(
            "TOPN_INVALID", "排名行数必须大于 0。", alternatives=["3", "5", "10"]
        )

    spec = _BuildSpec(
        report_name=report_name,
        room_id=resolved_room,
        source=source,
        dataset_id="ds_room_energy",
        time_window=window,
        grain=grain,
        components=components,
        filters=normalized_filters,
        top_n=resolved_top_n,
        derived_columns=normalized_derived,
        style_hints=normalized_styles,
        total_metric=total_metric,
        device_metric=device_metric,
        capability_evidence=dict(capabilityEvidence or {}),
    )

    # ---- 2. 逐个区域组装 ----
    regions: list[dict[str, Any]] = [_title_region(resolved_room, spec)]
    builders = {
        "total": _total_energy_region,
        "ranking": _ranking_region,
        "trend": _trend_region,
        "quality": _quality_region,
        "analysis": _analysis_region,
    }
    for key in components:
        regions.append(builders[key](spec))

    defaults_window: dict[str, Any]
    if window.get("type") == "FIXED_INTERVAL":
        defaults_window = {
            "type": "FIXED_INTERVAL",
            "timezone": window.get("timezone", "Asia/Shanghai"),
            "start": window.get("start"),
            "end": window.get("end"),
        }
    else:
        defaults_window = {
            "type": "CALENDAR",
            "timezone": window.get("timezone", "Asia/Shanghai"),
            "anchor": {"type": "CONTEXT", "path": "runtime.logicalTime"},
            "unit": window["unit"],
            "offset": (
                window.get("offset")
                if window.get("offset") is not None
                else -window["count"]
            ),
            "count": window["count"],
        }

    metric_refs_out = [
        {"ref": total_metric["metricRef"], "version": total_metric["metricVersion"]},
        {"ref": device_metric["metricRef"], "version": device_metric["metricVersion"]},
    ]

    metadata: dict[str, Any] = {
        "name": report_name,
        "description": f"{report_name} 由 AI 生成的首版草稿",
        "revision": 0,
        "status": "DRAFT",
        "tags": ["machine_room", "energy", "daily"],
    }
    if rejected_style_requests or unsupportedStyleRequests:
        # 没生效的版式/样式诉求要留在定义里：上层据此明确告诉用户
        # 「这些当前模板不支持、没有生效」，不能假装做到了也不能悄悄丢掉。
        # 两处来源合并：路由层确定性识别到的 + 模型写进 styleHints 被拒的。
        merged_style_requests = _merge_unsupported_style_requests(
            unsupportedStyleRequests, rejected_style_requests
        )
        if merged_style_requests:
            metadata["unsupportedStyleRequests"] = merged_style_requests

    return ReportDefinitionDraft(
        schemaVersion="1.0",
        metadata=metadata,
        parameters={},
        policyBinding={
            "policyRef": "policy_pending",
            "allowedOverrides": ["TIME_WINDOW", "GRAIN"],
            "executionMode": "MANUAL_ONLY",
        },
        defaults={
            "locale": "zh-CN",
            "scope": {
                "subjectRef": "machine_room",
                "selectors": {"machineRoomId": [resolved_room]},
            },
            # 选中的数据源：前端草稿面板 / 校验层都按这里核对口径，
            # 不允许「数据集里写着一个源、defaults 里写着另一个」。
            "source": {
                "sourceRef": source["sourceRef"],
                "sourceVersion": source["sourceVersion"],
                "subjectRef": source["subjectRef"],
            },
            "timeWindow": defaults_window,
            "grain": {"unit": grain},
            "preFilters": [dict(f) for f in normalized_filters],
        },
        page={
            "arrangement": {"mode": "GRID", "columns": 12, "gap": {"value": 12, "unit": "PX"}},
            "margins": {
                "top": {"value": 10, "unit": "MM"},
                "right": {"value": 10, "unit": "MM"},
                "bottom": {"value": 10, "unit": "MM"},
                "left": {"value": 10, "unit": "MM"},
            },
        },
        theme={"themeRef": "theme_default", "version": "1.0"},
        datasets={spec.dataset_id: _dataset_for(source, spec.dataset_id)},
        pipelines={},
        regions=regions,
        outputs={
            "html": {
                "format": "HTML",
                "fileNameTemplate": "machine_room_energy_{{logicalDate}}.html",
            }
        },
        intentBindings=_intent_bindings(spec),
        dependencies={
            "dataCatalogVersion": "1.0",
            "componentRegistryVersion": "1.0",
            "operatorRegistryVersion": "1.0",
            "functionRegistryVersion": "1.0",
            "metricRefs": metric_refs_out,
            "sourceRef": source["sourceRef"],
            "capabilityEvidence": {
                **spec.capability_evidence,
                "sourceRef": source["sourceRef"],
                "grainUnit": grain,
                "supportedGrains": list(source.get("supportedGrains", [])),
                "timeWindow": {
                    "type": window.get("type", "CALENDAR"),
                    "unit": window.get("unit"),
                    "count": window.get("count"),
                    "offset": window.get("offset"),
                },
            },
        },
    )


# ---------- ChangeSet 构造 ----------

def build_change_set_trend_7d(base_revision: int) -> ChangeSet:
    """样例 5：把趋势图改成最近 7 天。"""
    return ChangeSet(
        baseDefinitionRevision=base_revision,
        changed=[
            ChangeSetEntry(
                regionId=REGION_IDS["trend"],
                path=f"/regions/{REGION_IDS['trend']}/context/timeWindow",
                after=_calendar_window(offset=-7, count=7),
                summary="趋势图窗口改为近 7 个完整自然日",
            )
        ],
        unchanged=[
            ChangeSetUnchanged(
                regionId=REGION_IDS["total"],
                summary="总耗电卡片仍使用昨日窗口",
            )
        ],
        impact=[
            "需要重新解析窗口",
            "需要重取趋势图数据",
            "不影响昨日总耗电卡片",
        ],
        intentBindings=[
            {
                "id": "intent_trend_7d",
                "sourceText": "把趋势图改成最近 7 天",
                "domain": "TIME_WINDOW",
                "targetRegionIds": [REGION_IDS["trend"]],
                "targetPaths": [
                    f"/regions/{REGION_IDS['trend']}/context/timeWindow"
                ],
                "status": "RESOLVED",
            }
        ],
    )


def build_change_set_remove_trend(base_revision: int) -> ChangeSet:
    """样例 6：删除趋势图，保留总耗电和设备排名。"""
    return ChangeSet(
        baseDefinitionRevision=base_revision,
        changed=[
            ChangeSetEntry(
                regionId=REGION_IDS["trend"],
                path=f"/regions/{REGION_IDS['trend']}",
                after=None,
                summary="删除趋势图区域",
            )
        ],
        unchanged=[
            ChangeSetUnchanged(
                regionId=REGION_IDS["total"], summary="总耗电卡片保留"
            ),
            ChangeSetUnchanged(
                regionId=REGION_IDS["ranking"], summary="设备排名表保留"
            ),
        ],
        impact=["区域列表长度 -1", "不影响总耗电与设备排名计算"],
        intentBindings=[
            {
                "id": "intent_remove_trend",
                "sourceText": "不要趋势图，改成只要总耗电和设备排名",
                "domain": "CONTENT",
                "targetRegionIds": [REGION_IDS["trend"]],
                "targetPaths": [f"/regions/{REGION_IDS['trend']}"],
                "status": "RESOLVED",
            }
        ],
    )
