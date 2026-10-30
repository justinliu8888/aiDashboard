"""问题分析（工单 #58）：读实际定义、窗口、粒度与渲染载荷，解释并给建议。

用户说「昨日能耗趋势好丑啊，一个点……」时，系统**不猜**他要改什么。
先把当前这份报表的真实状态读出来 —— 卡片定义、时间窗、编译粒度、
渲染载荷 —— 把「为什么看起来是这样」讲清楚，再给可选方向。

三条硬约束（规格第 51、52、53、53 条）：

    1. 不改定义、不产生待接受的修改计划；
    2. 结论必须有证据：读不到就说读不到，或给一个针对性的澄清，
       **不凭关键词指定修改值**（不因为句子里出现「昨日」就把时间窗
       写成昨天 —— 卡片名里的时间词只用来定位对象）；
    3. 分析用的模拟载荷只在内存里读，不进日志、不进会话库。

本模块只做「读 + 讲」，不碰草稿，也不产出 EditPlan。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.schemas.report_definition import ReportDefinitionDraft


#: 一个时间窗单位里，按各粒度能切出多少个点。
POINTS_PER_UNIT: dict[str, int] = {
    "MINUTE": 24 * 60,
    "HOUR": 24,
    "DAY": 1,
}

#: 粒度的人话标签。
GRAIN_LABELS: dict[str, str] = {
    "MINUTE": "分钟",
    "HOUR": "小时",
    "DAY": "天",
}

#: 组件类型的人话标签。
COMPONENT_LABELS: dict[str, str] = {
    "METRIC_CARD": "指标卡",
    "CHART": "图表",
    "TABLE": "表格",
    "DATA_QUALITY": "数据质量块",
    "ANALYSIS": "分析说明块",
    "TEXT": "标题",
}

#: 时间窗单位换算成「几天」。
_UNIT_TO_DAYS: dict[str, float] = {"DAY": 1.0, "WEEK": 7.0, "MONTH": 30.0}


@dataclass(frozen=True)
class IssueAnalysis:
    """一次问题分析的结果。"""

    region_id: str = ""
    target_label: str = ""
    component_label: str = ""
    window_label: str = ""
    window_days: float | None = None
    grain_label: str = ""
    grain_unit: str = ""
    expected_points: int | None = None
    rendered_points: int | None = None
    findings: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    evidence_sufficient: bool = True
    source_ref: str = ""

    @property
    def summary(self) -> str:
        """面向用户的一段说明（不含任何模拟业务数值）。"""
        if not self.evidence_sufficient:
            return (
                "我没能从当前这份报表里读出足够的信息来判断你说的是哪一块、"
                "哪里不对劲。草稿没有变化，也没有生成待接受的修改计划。"
            )
        lines = list(self.findings)
        if self.suggestions:
            lines.append("可以往这几个方向看看：" + "；".join(self.suggestions) + "。")
        lines.append("本次只做了分析：草稿没有变化，也没有生成待接受的修改计划。")
        return "\n".join(lines)


def _region_window(region: dict[str, Any]) -> dict[str, Any]:
    """取区域声明的时间窗（没有就返回空 dict）。"""
    context = region.get("context") if isinstance(region.get("context"), dict) else {}
    window = context.get("timeWindow") or {}
    value = window.get("value") if isinstance(window.get("value"), dict) else window
    return value if isinstance(value, dict) else {}


def _region_grain(region: dict[str, Any], definition: ReportDefinitionDraft) -> str:
    """取区域的编译粒度（没有就退回报表默认粒度）。"""
    context = region.get("context") if isinstance(region.get("context"), dict) else {}
    grain = context.get("grain") or {}
    value = grain.get("value") if isinstance(grain.get("value"), dict) else grain
    unit = str(value.get("unit") or "").strip().upper()
    if unit:
        return unit
    defaults = definition.defaults or {}
    default_grain = defaults.get("grain") or {}
    return str(default_grain.get("unit") or "").strip().upper()


def _window_label(value: dict[str, Any]) -> tuple[str, float | None]:
    """把时间窗结构翻译成人话，并算出它覆盖多少天。"""
    unit = str(value.get("unit") or "").strip().upper()
    count = value.get("count")
    count = int(count) if isinstance(count, int) and count > 0 else 1
    offset = value.get("offset")
    offset = int(offset) if isinstance(offset, int) else -1
    days = _UNIT_TO_DAYS.get(unit)
    span = days * count if days is not None else None
    if unit == "DAY":
        if offset == -1 and count == 1:
            return "昨天整天（1 天）", span
        if offset < 0:
            return f"最近 {abs(offset)} 个完整自然日（{count} 天）", span
        return f"以今天为起点的 {count} 天", span
    if unit == "WEEK":
        return f"按周（{count} 周）", span
    if unit == "MONTH":
        return f"按月（{count} 个月）", span
    if not unit:
        return "没有声明时间窗", None
    return f"按 {unit}（{count} 个）", span


def _expected_points(window_days: float | None, grain_unit: str) -> int | None:
    """这个时间窗按这个粒度最多能切出多少个点。"""
    if window_days is None:
        return None
    per_unit = POINTS_PER_UNIT.get(grain_unit)
    if per_unit is None:
        return None
    return max(1, int(window_days * per_unit))


def _component_label(region: dict[str, Any]) -> str:
    component = region.get("component") if isinstance(region.get("component"), dict) else {}
    ctype = str(component.get("type") or "").strip().upper()
    label = COMPONENT_LABELS.get(ctype, ctype or "板块")
    chart_kind = str(component.get("chartKind") or "").strip().upper()
    if chart_kind:
        label = f"{label}（{chart_kind}）"
    return label


def _source_ref(region: dict[str, Any]) -> str:
    context = region.get("context") if isinstance(region.get("context"), dict) else {}
    for holder in (context, region.get("data") or {}):
        if not isinstance(holder, dict):
            continue
        for key in ("sourceRef",):
            value = holder.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def analyze_region(
    *,
    definition: ReportDefinitionDraft,
    region: dict[str, Any] | None,
    rendered_points: int | None = None,
) -> IssueAnalysis:
    """读一张卡片的真实状态，给出问题分析与建议。

    参数：

    * ``definition``：当前草稿；
    * ``region``：定位到的那张卡片；``None`` 表示证据不足；
    * ``rendered_points``：渲染载荷里这张卡片实际渲染出的数据点数
      （只有**点数**进答复，业务数值不进）。
    """
    if not region:
        return IssueAnalysis(
            evidence_sufficient=False,
            findings=(
                "我没找到你说的是哪一张卡片，所以还不能判断问题出在哪。",
            ),
        )

    name = str(region.get("name") or region.get("id") or "当前卡片")
    window_value = _region_window(region)
    window_label, window_days = _window_label(window_value)
    grain_unit = _region_grain(region, definition)
    grain_label = GRAIN_LABELS.get(grain_unit, grain_unit or "未声明")
    expected = _expected_points(window_days, grain_unit)
    component_label = _component_label(region)

    findings: list[str] = []
    findings.append(
        f"「{name}」这块现在的时间窗是{window_label}，编译粒度是{grain_label}，"
        f"展示形式是{component_label}。"
    )
    if expected is not None:
        findings.append(
            f"按这个组合最多能画出 {expected} 个点"
            + (
                f"，当前预览实际渲染出 {rendered_points} 个点。"
                if rendered_points is not None
                else "。"
            )
        )
    if rendered_points is not None and expected is not None and rendered_points < expected:
        findings.append(
            f"实际点数比理论上少，差额通常来自这块自带的过滤条件或数据缺口。"
        )

    suggestions: list[str] = []
    if expected is not None and expected <= 1:
        suggestions.append("要看出变化，把时间跨度拉长，或把粒度调到与时间窗匹配的档位")
    elif rendered_points is not None and rendered_points <= 1:
        suggestions.append("先确认这块的过滤条件是不是把数据收得太窄")
    else:
        suggestions.append("可以看看是时间跨度、粒度还是过滤条件让这块不满足预期")
    if grain_unit and window_days is not None:
        suggestions.append("也可以换一张时间跨度更合适的卡片来看同一件事")

    return IssueAnalysis(
        region_id=str(region.get("id") or ""),
        target_label=name,
        component_label=component_label,
        window_label=window_label,
        window_days=window_days,
        grain_label=grain_label,
        grain_unit=grain_unit,
        expected_points=expected,
        rendered_points=rendered_points,
        findings=tuple(findings),
        suggestions=tuple(suggestions),
        evidence_sufficient=True,
        source_ref=_source_ref(region),
    )
