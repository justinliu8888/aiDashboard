"""路由规则层：确定性抽取结构化槽位 + 识别明确的阻断场景。

ContextEngine 依赖模型输出，但模型输出不稳（枚举不匹配、JSON 不合法、超时）。
这一层把「用户话术里明确说过的东西」用规则确定性地抽出来，保证：

    1. 路由结果里始终带上 sourceRef / grainUnit / timeWindow 等定义组装层要用的字段；
    2. 越权范围、未知指标这类「必须明确阻断」的场景不再退化成追问时间或粒度；
    3. 默认能力（A 机房昨日能耗）不需要追问数据源和粒度。

槽位取值口径与 ``app/tools/schemas.py::DraftPlanPayload`` 保持一致
（定义组装层直接消费，不做二次翻译）：

    sourceRef         str
    grainUnit         str            HOUR / DAY / MINUTE
    timeRange         str            自然语言时间窗，如「昨日」「最近7个完整自然日」
    metricRefs        list[str]
    componentRequests list[str]      total / ranking / trend / quality / analysis
    excludedRegions   list[str]      明确不要的业务区域（同一套组件键）
    filters           list[dict]     {fieldRef, op, values}
    topN              int
    derivedColumns    list[dict]     {kind, label}
    styleHints        list[dict]     {fieldRef, rule, comparison, threshold, style}

设计约束：
    - 纯函数、无 IO、无模型调用；
    - 只在「用户本轮明确说过」和「系统默认值」之间做区分，绝不臆造第三个来源；
    - 对外只暴露 dict，不和 Pydantic schema 耦合，方便 ContextEngine 与模板流共用。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from app.data.machine_room_energy import DEVICE_TYPE_ALIASES
from app.services.catalog_mock_service import match_source_by_alias
from app.services.metric_mock_service import list_metrics, match_metric_by_alias


# ---------- 默认能力（首功能 Mock 登记的能力） ----------

DEFAULT_SOURCE_REF = "catalog.machine_room.energy"
DEFAULT_TREND_GRAIN = "HOUR"
DEFAULT_SUMMARY_GRAIN = "DAY"
DEFAULT_TIME_RANGE = "昨日"
DEFAULT_METRIC_REF = "metric.machine_room.total_energy"
DEFAULT_DEVICE_METRIC_REF = "metric.machine_room.device_energy"
#: 排名表默认行数。用户没说「前几名」时按 10 行出，不要为此追问用户。
DEFAULT_TOP_N = 10

#: 路由层产出、并被定义组装层直接消费的槽位。
SLOT_KEYS: tuple[str, ...] = (
    "subjectId",
    "subjectRef",
    "subjectName",
    "timeRange",
    "metricRefs",
    "grainUnit",
    "reportName",
    "sourceRef",
    "filters",
    "excludedRegions",
    "topN",
    "derivedColumns",
    "styleHints",
    "componentRequests",
    "requestedComponents",
)

#: 槽位 -> 展示名，供前端草稿面板标注「系统暂选 / 用户指定」。
SLOT_LABELS: dict[str, str] = {
    "subjectId": "对象",
    "timeRange": "时间范围",
    "metricRefs": "指标",
    "grainUnit": "粒度",
    "reportName": "报表名",
    "sourceRef": "数据源",
    "filters": "过滤条件",
    "excludedRegions": "排除区域",
    "topN": "TopN",
    "derivedColumns": "派生列",
    "styleHints": "样式",
    "componentRequests": "内容区块",
}

#: 定义组装层认识的组件键。
COMPONENT_KEYS: tuple[str, ...] = ("total", "ranking", "trend", "quality", "analysis")


# ---------- 词法规则 ----------

# 机房：字母前面不能再有字母，避免把 "EAS机房" 误判成 S 机房。
# 两种写法都要认：「A 机房」和「机房 A」。
ROOM_PATTERN = re.compile(
    r"(?<![A-Za-z])([A-Z])\s*机房|机房\s*([A-Z])(?![A-Za-z])"
)
ROOM_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9_])room_([a-z0-9_]+)", re.IGNORECASE)

_RE_N_ABSOLUTE = re.compile(r"(20\d{2}[-年/]\d{1,2}(?:[-月/]\d{1,2})?)")
_RE_N_DAYS = re.compile(r"(?:最近|近|过去)\s*(\d+)\s*个?\s*(?:完整)?\s*(?:自然)?\s*(?:日|天)")
_RE_N_WEEKS = re.compile(r"(?:最近|近|过去)\s*(\d+)\s*个?\s*(?:完整)?\s*(?:周|星期)")
_RE_N_HOURS = re.compile(r"(?:最近|近|过去)\s*(\d+)\s*个?\s*(?:完整)?\s*(?:小时)")
_RE_YESTERDAY = re.compile(r"(昨天|昨日)")
_RE_TODAY = re.compile(r"(今天|今日)")
_RE_TOMORROW = re.compile(r"(明天|明日)")
_RE_DAY_BEFORE = re.compile(r"(前天)")
_RE_THIS_WEEK = re.compile(r"(本周|这周|本星期)")
_RE_LAST_WEEK = re.compile(r"(上周|上个星期)")
_RE_THIS_MONTH = re.compile(r"(本月|这个月)")
_RE_LAST_MONTH = re.compile(r"(上月|上个月)")

_RE_GRAIN_MINUTE = re.compile(r"(分钟级|分钟|minute)", re.IGNORECASE)
_RE_GRAIN_HOUR = re.compile(r"(小时级|每小时|小时|hour)", re.IGNORECASE)
_RE_GRAIN_DAY = re.compile(r"(按天|每日|每天|日粒度|天粒度|日汇总|天汇总|daily)", re.IGNORECASE)
_RE_DAILY_SUMMARY = re.compile(r"(日汇总|按天|每日|每天|日粒度|天粒度)")

_RE_TREND = re.compile(r"(趋势|曲线|折线|走势)")
_RE_NEGATION = re.compile(r"(不要|不需要|不用|去掉|删除|移除|别放|不展示|不含|别加)")

_COMPONENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("total", re.compile(r"(总耗电|总用电|总能耗|总电量|能耗总量|总量)")),
    ("ranking", re.compile(r"(排名|排行|排序|榜单|top\s*\d)")),
    ("trend", re.compile(r"(趋势|曲线|折线|走势)")),
    ("quality", re.compile(r"(数据质量|质量分|完整度)")),
    ("analysis", re.compile(r"(分析|解读|洞察|结论|说明段)")),
)

_RE_DEVICE_IDS = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,6}[-_]\d{1,4})(?![A-Za-z0-9])")
_RE_FILTER_MARKER = re.compile(
    r"(只统计|只包含|只看|仅统计|仅包含|仅限|限定|只要|仅|只)"
)
_RE_NEGATED_SEGMENT = re.compile(
    r"(?:不要|不需要|不用|不统计|别统计|不包含|去掉|删除)"
    r"(?:统计|包含|看|纳入)?\s*([^，。；;,.!?！？]{1,40})"
)

_RE_TOPN = re.compile(
    r"(?:top\s*(\d+))|(?:前\s*(\d+)\s*(?:台|名|位|个))|(?:排名前\s*(\d+))|(?:排名前列\s*(\d+))",
    re.IGNORECASE,
)

_DERIVED_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PREVIOUS_PERIOD", re.compile(r"(?:前一日|前一天|上日|昨日)耗电|前一日耗电")),
    ("GROWTH_RATE", re.compile(r"(?:日增长率|日增|增长率|环比)")),
    ("SHARE_OF_TOTAL", re.compile(r"(?:占比|占[^，。]{0,10}总耗电[^，。]{0,6}比例|占总耗电)")),
)

_DERIVED_LABELS: dict[str, str] = {
    "PREVIOUS_PERIOD": "前一日耗电 (kWh)",
    "GROWTH_RATE": "日增长率 (%)",
    "SHARE_OF_TOTAL": "占机房总耗电比例 (%)",
}

_STYLE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("highlightRed", re.compile(r"(标红|高亮[^，。]{0,4}红|红色标记|红色标出)")),
    ("sortDescending", re.compile(r"(降序|从高到低|由高到低)")),
    ("sortAscending", re.compile(r"(升序|从低到高|由低到高)")),
)
_RE_THRESHOLD = re.compile(r"(?:超过|大于|高于|>\s*)\s*(\d+(?:\.\d+)?)\s*%?")

_RE_REPORT_NOUN = re.compile(r"(报表|报告|日报|周报|月报|简报|看板)")
_RE_NAME_EXTRACT = re.compile(
    r"(?:生成|做|给我|来|出|整|画)\s*(?:一?[份个张])?\s*"
    r"(.+?)\s*(?:的)?\s*(?:模[板版]|模板|报[表告]|日报|周报|月报|简报)"
)

# ---------- 阻断场景 ----------

_RE_CROSS_SCOPE = re.compile(
    r"(所有项目|全部项目|跨项目|所有机房|全部机房|各个机房|所有设备间|全机房|不限\s*[A-Za-z0-9\s]{0,6}机房)"
)
_RE_QUOTED = re.compile(r"[「『\"“]([^\」』\"”]{2,20})[」』\"”]")
_RE_METRIC_SUFFIX = re.compile(r"([一-龥A-Za-z]{2,8})(值|率|度)")
#: 派生列 / 业务词，不当成「未登记指标」候选。
_RE_DERIVED_HINTS = (
    "增长率", "占比", "比例", "排名", "趋势", "日增", "环比", "同比",
    "数量", "台数", "数值", "额度", "进度", "日期", "时间",
)
#: 指标候选词的特征字，命中才算「像指标名」。
_METRIC_WORD_HINTS = ("值", "率", "度", "心情", "指数", "评分", "健康", "体感", "满意度")
_RE_REPORT_REQUEST = re.compile(
    r"(生成|做|统计|展示|出一?|画|报表|报告|日报|周报|趋势|指标)"
)

#: 引用型非指标词（用户引用时间、房间时不要当成指标名）。
_QUOTED_STOPWORDS = (
    "昨日", "昨天", "今天", "今日", "明天", "前天", "本周", "上周", "本月", "上月",
    "A", "B", "C", "a", "b", "c",
)


# ---------- 基础抽取 ----------

def match_room_id(message: str) -> str | None:
    """从话术里抽出机房 id，例如「A 机房」/「机房 A」→ room_a。"""
    text = message or ""
    match = ROOM_PATTERN.search(text)
    if match:
        for group in match.groups():
            if group:
                return f"room_{group.lower()}"
    token = ROOM_TOKEN_PATTERN.search(text)
    if token:
        return f"room_{token.group(1).lower()}"
    return None


def detect_time_range(message: str) -> str | None:
    """抽出用户明确说出的时间范围（自然语言，组装层可直接解析）。"""
    text = message or ""
    if not text:
        return None
    match = _RE_N_ABSOLUTE.search(text)
    if match:
        return match.group(1)
    match = _RE_N_DAYS.search(text)
    if match:
        suffix = "个完整自然日" if "完整" in match.group(0) else "天"
        return f"最近{match.group(1)}{suffix}"
    match = _RE_N_WEEKS.search(text)
    if match:
        return f"最近{match.group(1)}周"
    match = _RE_N_HOURS.search(text)
    if match:
        return f"最近{match.group(1)}小时"
    for pattern, label in (
        (_RE_YESTERDAY, "昨日"),
        (_RE_TODAY, "今日"),
        (_RE_TOMORROW, "明日"),
        (_RE_DAY_BEFORE, "前天"),
        (_RE_THIS_WEEK, "本周"),
        (_RE_LAST_WEEK, "上周"),
        (_RE_THIS_MONTH, "本月"),
        (_RE_LAST_MONTH, "上月"),
    ):
        if pattern.search(text):
            return label
    return None


def detect_grain(message: str) -> str | None:
    """抽出用户明确说出的粒度。"""
    text = message or ""
    if not text:
        return None
    hits: list[tuple[int, str]] = []
    for pattern, unit in (
        (_RE_GRAIN_MINUTE, "MINUTE"),
        (_RE_GRAIN_HOUR, "HOUR"),
        (_RE_GRAIN_DAY, "DAY"),
    ):
        match = pattern.search(text)
        if match:
            hits.append((match.start(), unit))
    if not hits:
        return None
    hits.sort(key=lambda item: (item[0], {"MINUTE": 0, "HOUR": 1, "DAY": 2}[item[1]]))
    return hits[0][1]


def default_grain(message: str) -> str:
    """没有明说粒度时的默认值：日汇总按天，其余按小时。"""
    text = message or ""
    if _RE_DAILY_SUMMARY.search(text):
        return DEFAULT_SUMMARY_GRAIN
    return DEFAULT_TREND_GRAIN


def detect_source_ref(message: str) -> str | None:
    """抽出用户明确指定的数据源。"""
    return match_source_by_alias(message or "")


def detect_metric_refs(message: str) -> list[str]:
    """抽出用户明确指定的指标（可能多个）。"""
    text = (message or "").lower()
    if not text:
        return []
    refs: list[str] = []
    for metric in list_metrics("machine_room"):
        ref = metric.get("metricRef")
        label = (metric.get("label") or "").lower()
        if ref and label and label in text and ref not in refs:
            refs.append(ref)
    if not refs:
        single = match_metric_by_alias(message)
        if single:
            refs.append(single)
    if re.search(r"(排名|各设备|每台设备|设备级|设备耗电|设备排行)", text):
        if DEFAULT_DEVICE_METRIC_REF not in refs:
            refs.append(DEFAULT_DEVICE_METRIC_REF)
    return refs


def detect_top_n(message: str) -> int | None:
    """抽出 TopN / 前 N 名。"""
    match = _RE_TOPN.search(message or "")
    if not match:
        return None
    for group in match.groups():
        if group:
            try:
                value = int(group)
            except ValueError:
                continue
            if value > 0:
                return value
    return None


def _normalize_device_id(raw: str) -> str:
    """把用户写的「CRAC-01」对齐成目录里的 deviceId 形态「crac_01」。"""
    return (raw or "").strip().lower().replace("-", "_")


def _detect_device_types(segment: str) -> list[str]:
    """从一段话里认出设备类别枚举（机架 / 配电 / 空调 / UPS）。"""
    lowered = (segment or "").lower()
    found: list[str] = []
    for alias, device_type in DEVICE_TYPE_ALIASES.items():
        key = alias.lower()
        if not key:
            continue
        if key in lowered and device_type not in found:
            found.append(device_type)
    return found


def detect_filters(message: str) -> list[dict[str, Any]] | None:
    """抽出设备过滤条件。

    - 「只统计 CRAC-01 和 CRAC-02」→ deviceId IN
    - 「只统计空调设备」→ deviceType IN
    - 「不要统计机架、PDU 和 UPS」→ deviceType NOT IN
    """
    text = message or ""
    if not text:
        return None
    filters: list[dict[str, Any]] = []

    device_ids = _RE_DEVICE_IDS.findall(text)
    if device_ids and _RE_FILTER_MARKER.search(text):
        values = [_normalize_device_id(item) for item in device_ids]
        unique = list(dict.fromkeys(values))
        if unique:
            filters.append({"fieldRef": "deviceId", "op": "IN", "values": unique})
    elif _RE_FILTER_MARKER.search(text):
        # 没点名具体设备时，才考虑「只统计空调设备」这种类别过滤。
        device_types = _detect_device_types(text)
        if device_types:
            filters.append(
                {"fieldRef": "deviceType", "op": "IN", "values": device_types}
            )

    for match in _RE_NEGATED_SEGMENT.finditer(text):
        excluded_types = _detect_device_types(match.group(1))
        if excluded_types:
            # op 用下划线形态（NOT_IN）：组装层和预览层都按这个枚举比对。
            filters.append(
                {"fieldRef": "deviceType", "op": "NOT_IN", "values": excluded_types}
            )
    return filters or None


def _is_negated(position: int, negation_spans: list[tuple[int, int]]) -> bool:
    """判断某个命中点是否落在否定词的影响范围内。"""
    for start, end in negation_spans:
        if start <= position <= end + 10:
            return True
    return False


def detect_component_requests(message: str) -> tuple[list[str], list[str]]:
    """抽出要生成 / 明确不要的业务区块。

    返回 (requested, excluded)：
        requested —— 用户点名要生成的组件键；
        excluded —— 用户明确说不要的组件键（和 requested 用同一套键）。
    """
    text = message or ""
    if not text:
        return [], []
    negation_spans = [m.span() for m in _RE_NEGATION.finditer(text)]
    requested: list[str] = []
    excluded: list[str] = []
    for name, pattern in _COMPONENT_RULES:
        match = pattern.search(text)
        if not match:
            continue
        if _is_negated(match.start(), negation_spans):
            if name not in excluded:
                excluded.append(name)
        elif name not in requested:
            requested.append(name)
    return requested, excluded


def detect_components(message: str) -> list[str] | None:
    """只要生成哪些区块（向后兼容的便捷入口）。"""
    requested, _excluded = detect_component_requests(message)
    return requested or None


def detect_derived_columns(message: str) -> list[dict[str, Any]] | None:
    """抽出「前一日耗电 / 日增长率 / 占比」这类派生列。"""
    text = message or ""
    if not text:
        return None
    columns: list[dict[str, Any]] = []
    for kind, pattern in _DERIVED_RULES:
        if not pattern.search(text):
            continue
        if any(column["kind"] == kind for column in columns):
            continue
        columns.append({"kind": kind, "label": _DERIVED_LABELS[kind]})
    return columns or None


def detect_style_hints(message: str) -> list[dict[str, Any]] | None:
    """抽出「增长超过 20% 标红」这类条件样式。"""
    text = message or ""
    if not text:
        return None
    hints: list[dict[str, Any]] = []
    threshold_match = _RE_THRESHOLD.search(text)
    threshold = float(threshold_match.group(1)) if threshold_match else 0.0
    for name, pattern in _STYLE_RULES:
        if not pattern.search(text):
            continue
        if name == "highlightRed":
            hints.append(
                {
                    "fieldRef": "growthRatePercent",
                    "rule": "HIGHLIGHT_ABOVE_THRESHOLD",
                    "comparison": "GT",
                    "threshold": threshold,
                    "style": "RED",
                }
            )
        else:
            hints.append(
                {
                    "fieldRef": "energyKwh",
                    "rule": "SORT",
                    "comparison": "DESC" if name == "sortDescending" else "ASC",
                    "threshold": 0.0,
                    "style": "DEFAULT",
                }
            )
    return hints or None


# --------------------------------------------------------------------------
# 当前模板不支持的版式/样式诉求（确定性识别）
# --------------------------------------------------------------------------

#: (kind, 用户能看懂的诉求名, 识别正则, 当前模板的真实行为)
#:
#: 这些能力在模板层是写死的：
#:   - 趋势图 ``component.chartKind`` 写死 ``LINE``（``_trend_region``）
#:   - 总耗电卡片 ``columnSpan`` 写死 4、排名与趋势各写死 12（同上）
#:   - 标题区没有任何样式字段（``_title_region``）
#: 实现它们属于**新增产品能力**，本轮只做「明确披露未生效」，不做实现。
#:
#: 识别必须是**确定性**的，不能依赖模型是否把诉求写进 ``styleHints``：
#: 第一轮建立的披露链路就栽在这里 —— 模型把诉求写进 ``collectedInfo`` 自由文本、
#: 定义的 ``styleHints`` 里一条都没有，装配层无拒项，披露链路整条没被触发。
#:
#: 正则同时覆盖中文自然语言和 snake_case 标识符
#: （Deep Router 会把诉求写成 ``total_card_occupy_full_row`` 这类串）。
UNSUPPORTED_STYLE_REQUESTS: tuple[tuple[str, str, str, str], ...] = (
    (
        "LAYOUT_FULL_ROW",
        "版式：独占一行",
        r"(独占[一1]行|独占整行|占满[一1]行|占一整行|单独[一1]行|单独占[一1]行"
        r"|full_row|occupy_full_row)",
        "当前总耗电卡片占 4/12 列、与其它区块并排，不是独占一行",
    ),
    (
        "LAYOUT_SIDE_BY_SIDE",
        "版式：左右并排等宽",
        r"(左右等宽|左右各占[一1]半|各占[一1]半|各占一半|各一半|一半宽|半宽|并排"
        r"|在左[边侧]?|在右[边侧]?|equal_width|left_trend_right|side_by_side|two_column)",
        "当前排名表与趋势图各占 12/12 整行、上下堆叠，不是左右并排等宽",
    ),
    (
        "CHART_KIND",
        "图形类型：柱状图",
        r"(柱状图|条形图|柱图|bar[_\s-]?chart|chart[_\s-]?(?:kind|type))",
        "当前趋势图固定为折线图（LINE），不支持柱状图",
    ),
    (
        "TITLE_FONT_SIZE",
        "标题样式：字号（{match}）",
        r"(\d+(?:\.\d+)?\s*pt(?![A-Za-z0-9_])"
        r"|标题[^\n，。；]{0,6}(?:字号|字体大小)"
        r"|font[_\s-]?size|fontsize|title[_\s-]?size)",
        "当前标题区没有字号设置",
    ),
    (
        "TITLE_ALIGN",
        "标题样式：对齐方式（{match}）",
        r"(左对齐|右对齐|居中对齐"
        r"|标题[^\n，。；、]{0,6}对齐"
        r"|title[_\s-]?align|align[_\s-]?left|align[_\s-]?right)",
        "当前标题区没有对齐设置",
    ),
    (
        "TITLE_COLOR",
        "标题样式：颜色（{match}）",
        r"(标题[^\n，。；、]{0,6}颜色|文字颜色|字体颜色|图表颜色|配色"
        r"|RGB\s*\(\s*[\d\s,]*\)|rgba\s*\(\s*[\d\s,.]*\)"
        r"|#[0-9a-fA-F]{6}(?![0-9a-fA-F])"
        r"|title[_\s-]?color|font[_\s-]?color|text[_\s-]?color|chart[_\s-]?color)",
        "当前标题区与图表都没有颜色设置",
    ),
)


def detect_unsupported_style_requests(message: str) -> list[dict[str, Any]]:
    """确定性识别「当前模板不支持」的版式/样式诉求。

    只认三类：**版式**（独占一行 / 左右等宽）、**图形类型**（柱状图）、
    **标题样式**（字号 / 对齐 / 颜色）。

    行级条件样式（增长超阈值标红、按数值排序）是**支持**的能力，
    这里绝不能把它误判成「不支持」——那是把能做的事说成做不到。

    返回 ``[{"kind", "label", "match", "currentBehavior"}]``，按 kind 去重。
    """
    text = message or ""
    if not text:
        return []
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind, label, pattern, current in UNSUPPORTED_STYLE_REQUESTS:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        if kind in seen:
            continue
        seen.add(kind)
        snippet = match.group(0)
        # 片段只有「有中文或数字」时才带进标签：
        # Deep Router 会写出 title_font_size 这种标识符，直接贴给用户看很难懂。
        if not re.search(r"[\u4e00-\u9fff0-9]", snippet):
            snippet = ""
        found.append(
            {
                "kind": kind,
                "label": label.replace("{match}", snippet).replace("（）", ""),
                "match": match.group(0),
                "currentBehavior": current,
            }
        )
    return found


def _flatten_collected_text(value: Any) -> str:
    """把路由槽位里的值摊平成一段文本，供正则扫描。"""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return "\n".join(_flatten_collected_text(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(_flatten_collected_text(item) for item in value.values())
    return ""


def collect_unsupported_style_requests(
    *,
    message: str | None = None,
    collected_info: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """统一核对「原始输入」与「路由槽位」两处来源。

    披露不能依赖模型是否把诉求写进 ``definition.styleHints``：

    - **原始输入**：用户原话里写了什么。这是最可靠的确定性兜底。
    - **路由槽位**：路由层收集到的诉求，可能是模型写的自由文本或标识符
      （Deep Router 会把版式诉求写成 ``["title_font_size_22pt", ...]``）。

    两处结果按 kind 去重合并：同一个诉求只披露一次。
    """
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(items: list[dict[str, Any]]) -> None:
        for item in items:
            kind = str(item.get("kind") or "")
            if not kind or kind in seen:
                continue
            seen.add(kind)
            merged.append(item)

    _add(detect_unsupported_style_requests(message or ""))
    for value in (collected_info or {}).values():
        _add(detect_unsupported_style_requests(_flatten_collected_text(value)))
    return merged


def detect_report_name(message: str, default: str | None = None) -> str | None:
    """从话术里抽出报表名，例如「生成 A 机房昨日能耗日报模版」→ A 机房昨日能耗日报。"""
    text = (message or "").strip()
    match = _RE_NAME_EXTRACT.search(text)
    if not match:
        return default
    name = match.group(1).strip(" 　的,，。.;；、")
    if not (2 <= len(name) <= 30):
        return default
    if not _RE_REPORT_NOUN.search(name):
        if re.search(r"(能耗|耗电|用电)", name):
            name = f"{name}日报"
        else:
            return default
    return name


def wants_trend(message: str) -> bool | None:
    """用户是否要趋势图。None 表示没提。"""
    text = message or ""
    if not text:
        return None
    negation_spans = [m.span() for m in _RE_NEGATION.finditer(text)]
    match = _RE_TREND.search(text)
    if not match:
        return None
    if _is_negated(match.start(), negation_spans):
        return False
    return True


# ---------- 汇总：显式槽位 / 默认槽位 ----------

def explicit_slots(message: str, *, subject_id: str | None = None) -> dict[str, Any]:
    """只抽取用户在本轮话术里明确说过的槽位。"""
    text = message or ""
    if not text.strip():
        return {}
    slots: dict[str, Any] = {}

    room_id = match_room_id(text)
    if room_id:
        slots["subjectId"] = room_id
        slots["subjectName"] = room_id.removeprefix("room_").upper() + " 机房"

    time_range = detect_time_range(text)
    if time_range:
        slots["timeRange"] = time_range

    grain = detect_grain(text)
    if grain:
        slots["grainUnit"] = grain

    source_ref = detect_source_ref(text)
    if source_ref:
        slots["sourceRef"] = source_ref

    metric_refs = detect_metric_refs(text)
    if metric_refs:
        slots["metricRefs"] = metric_refs

    top_n = detect_top_n(text)
    if top_n is not None:
        slots["topN"] = top_n

    filters = detect_filters(text)
    if filters:
        slots["filters"] = filters

    derived = detect_derived_columns(text)
    if derived:
        slots["derivedColumns"] = derived

    styles = detect_style_hints(text)
    if styles:
        slots["styleHints"] = styles

    # 当前模板不支持的版式/样式诉求（独占一行 / 左右等宽 / 柱状图 / 标题样式）。
    # 这不是一个「要实现的条件」，而是一份**要明确披露的清单**：
    # 识别放在路由层，模型怎么表达都覆盖得到，披露链路不再依赖模型配合。
    unsupported_styles = detect_unsupported_style_requests(text)
    if unsupported_styles:
        slots["unsupportedStyleRequests"] = unsupported_styles

    requested, excluded = detect_component_requests(text)
    if requested:
        # 「只要 X」是白名单语义：componentRequests 直接当白名单传下去。
        # 「包含 X」只是包含语义：没点名不代表不要，componentRequests 当白名单
        # 会把模板自带的数据质量卡片也删掉，所以只作为参考信息带出去。
        if is_exclusive_request(text):
            slots["componentRequests"] = requested
        else:
            slots["requestedComponents"] = requested
    if excluded:
        slots["excludedRegions"] = excluded

    report_name = detect_report_name(text)
    if report_name:
        slots["reportName"] = report_name

    # 上下文里的 subjectId 只算「系统暂选」，不放进显式槽位：
    # 用户没点名机房时，不该把对象标成「用户指定」。
    return slots


def default_slots(message: str, *, subject_id: str | None = None) -> dict[str, Any]:
    """系统默认能力：只在用户没说的时候兜底。"""
    defaults: dict[str, Any] = {
        "sourceRef": DEFAULT_SOURCE_REF,
        "grainUnit": default_grain(message),
        "timeRange": DEFAULT_TIME_RANGE,
        "metricRefs": [DEFAULT_METRIC_REF],
        "topN": DEFAULT_TOP_N,
    }
    if subject_id:
        defaults["subjectId"] = subject_id
    return defaults


def resolve_inputs(
    explicit: dict[str, Any],
    defaults: dict[str, Any],
) -> tuple[dict[str, Any], set[str]]:
    """合并显式槽位与默认值。

    返回 (resolved, default_keys)：
        - resolved 是定义组装层真正要消费的最终值；
        - default_keys 是其中来自系统默认的键名，方便前端标「系统暂选」。
    """
    resolved: dict[str, Any] = dict(defaults)
    default_keys = set(defaults.keys())
    for key, value in explicit.items():
        if value is None or value == "":
            continue
        resolved[key] = value
        default_keys.discard(key)
    return resolved, default_keys


# ---------- 阻断场景 ----------

def detect_scope_violation(
    message: str,
    *,
    subject_ref: str | None = None,
    subject_id: str | None = None,
    authorized_subject_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    """识别越权范围：跨项目 / 全机房 / 未授权机房。"""
    text = message or ""
    if not text:
        return None
    if _RE_CROSS_SCOPE.search(text):
        return {
            "code": "PERMISSION_DENIED",
            "message": "跨项目 / 全机房范围超出当前账号的 IAM 授权",
            "reply": "当前账号只被授权了 A 机房，无法生成跨项目或全机房的能耗排名。请把范围限定在 A 机房。",
            "alternatives": ["限定在 A 机房", "限定在有权限的机房列表"],
        }
    room_id = match_room_id(text)
    if room_id and authorized_subject_ids:
        if room_id not in authorized_subject_ids:
            label = room_id.removeprefix("room_").upper()
            return {
                "code": "PERMISSION_DENIED",
                "message": f"{label} 机房不在当前账号的授权范围内",
                "reply": f"当前账号只被授权了 A 机房，没有 {label} 机房的授权，无法生成该机房的报表。",
                "alternatives": ["改为 A 机房", "联系管理员申请授权"],
            }
    return None


def detect_unknown_metric(
    message: str,
    *,
    subject_ref: str | None = None,
) -> dict[str, Any] | None:
    """识别「指标未登记」：用户点名的指标在目录里查不到。"""
    text = message or ""
    if not text:
        return None
    if not _RE_REPORT_REQUEST.search(text):
        return None
    # 只要命中了已登记指标，就不判定为未知指标（保守，避免误伤）。
    if detect_metric_refs(text):
        return None

    candidates = _metric_candidates(text)
    if not candidates:
        return None

    name = max(candidates, key=len)
    return {
        "code": "CAPABILITY_NOT_SUPPORTED",
        "message": f"指标「{name}」未在指标目录中登记",
        "reply": (
            f"指标「{name}」没有在指标目录里登记，当前无法生成这个指标的报表。"
            "我不会用耗电量或随机数代替它。可以换成已登记的指标，或先让管理员登记该指标。"
        ),
        "alternatives": ["总耗电", "设备耗电", "PUE", "能耗成本"],
        "metricName": name,
    }


def detect_block(
    message: str,
    *,
    subject_ref: str | None = None,
    subject_id: str | None = None,
    authorized_subject_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    """先判越权，再判未知指标；命中即返回阻断说明。"""
    scope = detect_scope_violation(
        message,
        subject_ref=subject_ref,
        subject_id=subject_id,
        authorized_subject_ids=authorized_subject_ids,
    )
    if scope:
        return scope
    return detect_unknown_metric(message, subject_ref=subject_ref)


# ---------- 其它 ----------

_RE_GENERATION_VERBS = re.compile(
    r"(生成|做一?[份个张]|出一?[份个张]|来一?[份个张]|给我|帮我做|画一?[张个]|绘制|整一?[份个]|输出)"
)
_RE_REPORT_NOUNS = re.compile(
    r"(报表|报告|日报|周报|能耗|耗电|用电|趋势|排名|指标卡|看板|电费)"
)


def is_clear_generation_request(message: str) -> bool:
    """是否是「明确要生成报表」的新任务。

    这类请求不需要 Concierge 兜底，直接进 Deep Router：
    先白等 Concierge 再失败，是首轮耗时里最大的一块无产出开销。
    """
    text = message or ""
    if not text.strip():
        return False
    return bool(_RE_GENERATION_VERBS.search(text)) and bool(_RE_REPORT_NOUNS.search(text))


# --------------------------------------------------------------------------
# 只读提问的判定（降级路径用）
# --------------------------------------------------------------------------
#
# 正常路径由 ContextEngine 的模型判定意图（ASK_QUESTION）。这一层只在
# **模型不可用 / 超时** 的降级路径上兜底：没有模型就没有意图，
# 「你有未完成的草稿吗」和「把趋势改成折线图」必须还能分开。
#
# 判定要求同时满足三条，避免把闲聊和指令吞进来：
#     1. 有疑问标记；
#     2. 指向报表业务对象（不是万能聊天）；
#     3. 没有明确的改动动词（带问号的指令仍然是指令）。

#: 疑问标记。
_QUESTION_MARKERS: tuple[str, ...] = (
    "吗", "呢", "？", "?", "什么", "怎么", "哪些", "是否", "有没有",
    "能不能", "可不可以", "能否", "多少", "几个", "为什么", "咋",
)

#: 疑问必须指向的报表业务对象。没有它就是闲聊，不属于本产品的问答范围。
#:
#: 「图」是**单字**业务对象：「这个图是什么意思」问的就是报表里那张图，
#: 少了它这句话就指不到业务对象，掉进普通流程、被当成改稿指令。
_QUESTION_SUBJECTS: tuple[str, ...] = (
    "报表", "草稿", "预览", "图表", "卡片", "指标", "数据源", "粒度",
    "分钟", "成本", "pue", "能耗", "耗电", "进度", "做到哪", "哪一步",
    "刚才", "之前", "上次", "这份", "那份", "当前", "现在", "支持",
    "图",
)

#: 上面那张表是**子串匹配**，单字「图」会误伤这些**与报表无关**的词
#: （「图书」「地图」里的「图」不是报表对象）。
#:
#: 命中它们时先把词从句子里去掉，再按同一张表匹配 —— 这样
#: 「图书库存那张图是什么意思」里的「图」仍然算业务对象，
#: 「这本图书是什么意思」里的「图」不算。
#:
#: 这是一张**封闭词表**（与本项目其它词表一致），需随产品词表同步：
#: 收录的是「**含**『图』但与报表无关」的整词，不是所有带图的词 ——
#: 「图表」「本图」「这张图」「图书库存那张图」里的「图」都是报表对象，
#: 不许收进来。
_NON_REPORT_GRAPH_WORDS: tuple[str, ...] = (
    "图书", "地图", "图标", "图片", "蓝图", "企图",
    "图腾", "图案", "版图", "意图",
    "图纸", "图章", "图钉", "构图",
)

#: 明确的改动动词：出现这些就是在下指令，不是在提问。
_OPERATION_VERBS: tuple[str, ...] = (
    "生成", "新建", "创建", "删除", "删掉", "删了", "去掉", "移除",
    "改成", "改为", "换成", "换为", "调成", "新增", "加一个", "加个",
    "应用", "保存", "导出", "发布", "接受修改", "继续调整", "重做",
    "重新开始", "重新生成",
)

# Bare alignment clauses are commands; capability questions containing the
# same words (e.g. "支持右对齐吗") are not.
_APPEARANCE_COMMAND_PATTERN = re.compile(
    r"(?:文字|字体|字号|圆角)(?:大小)?(?:调大|调小|放大|缩小|设为|设置为)"
    r"|(?:^|[，,；;、])\s*(?:左对齐|右对齐|居中对齐)(?=$|[，,；;。.!！?？])"
)


def looks_like_question(message: str) -> bool:
    """这句话有没有**疑问语气**。

    与 :func:`is_readonly_question` 的区别：那个还要求疑问**指向**报表业务
    对象，这个只看语气。用来回答「这句话有没有可能是提问」——

        比如设计流程里规则层没认出来（没指向业务对象的元问题）时，
        要不要再请路由层判一次。

    判定要求：

        1. 有疑问标记；
        2. 没有明确的改动动词（带问号的指令仍然是指令）。
    """
    text = (message or "").strip()
    if not text:
        return False
    if is_clear_generation_request(text):
        return False
    if has_operation_command(text):
        return False
    return any(marker in text for marker in _QUESTION_MARKERS)


def has_operation_command(message: str) -> bool:
    """这句话里有没有明确的下指令动词。

    用于把「操作」和「其它」分开：带明确改动动词的输入不能被当成提问，
    待确认期间也不允许它绕过菜单。
    """
    text = (message or "").strip()
    if not text:
        return False
    return (any(word in text for word in _OPERATION_VERBS)
            or bool(_APPEARANCE_COMMAND_PATTERN.search(text)))


def command_verbs_in(message: str) -> tuple[str, ...]:
    """这句里带了哪些**命令动词**（:data:`_OPERATION_VERBS`）。

    给「这一轮没有执行」那层实话用：命中几个就提几个，调用方按出现顺序排列。

    长词优先、互不重叠：「重新生成」里含「生成」，只算「重新生成」一个 ——
    否则同一处命令会被报成两个。
    """
    text = (message or "").strip()
    if not text:
        return ()
    hits: list[tuple[int, int, str]] = []
    for verb in _OPERATION_VERBS:
        start = text.find(verb)
        while start != -1:
            hits.append((start, start + len(verb), verb))
            start = text.find(verb, start + 1)
    for match in _APPEARANCE_COMMAND_PATTERN.finditer(text):
        hits.append((match.start(), match.end(), match.group().lstrip("，,；;、 ")))
    # 同一处起点取最长的词；再按出现位置从前往后挑不重叠的。
    hits.sort(key=lambda item: (item[0], -item[1]))
    picked: list[tuple[int, int, str]] = []
    for start, end, verb in hits:
        if any(not (end <= taken_start or start >= taken_end)
               for taken_start, taken_end, _ in picked):
            continue
        picked.append((start, end, verb))
    ordered: list[str] = []
    for _, _, verb in sorted(picked, key=lambda item: item[0]):
        if verb not in ordered:
            ordered.append(verb)
    return tuple(ordered)


def mentions_report_object(message: str) -> bool:
    """这句话有没有指向**报表业务对象**。

    判定口径只有这一份（:data:`_QUESTION_SUBJECTS` 的子串匹配）——
    只读提问与问题反馈两处都用它，不各写一遍。

    「图」是单字业务对象，子串匹配会误伤「图书」「地图」这类与报表无关的
    词：先把这几个无关词（:data:`_NON_REPORT_GRAPH_WORDS`，**封闭词表**，
    需随产品词表同步）从句子里去掉，再按同一张表匹配。
    """
    text = (message or "").strip().lower()
    for word in _NON_REPORT_GRAPH_WORDS:
        text = text.replace(word, "")
    return any(subject in text for subject in _QUESTION_SUBJECTS)


def is_readonly_question(message: str) -> bool:
    """这句是不是「只读提问」。

    只用于降级路径和待确认入口的兜底判定。命中意味着：
    这句话在**问当前对话 / 当前产品**的事，不是在下指令。
    """
    text = (message or "").strip()
    if not text:
        return False
    if is_clear_generation_request(text):
        return False
    if has_operation_command(text):
        return False
    if not any(marker in text for marker in _QUESTION_MARKERS):
        return False
    # 业务对象表里 PUE 是小写，但用户在句首写大写 PUE 很常见。
    # 认不出一句提问，后果是「提问」被当成「修改」——
    # 设计流程里那会平白多出一版方案，所以这里按大小写不敏感匹配。
    return mentions_report_object(text)


# --------------------------------------------------------------------------
# 确定性快路的「只读提问」判定词表
# --------------------------------------------------------------------------
#
# 这两张表**只**服务于确定性快路（``agent_service._try_answer_readonly_query``），
# 与上面意图门那一套**故意不同**，各判各的、不合并：
#
#   - **意图门**问的是「这条消息是**命令**还是**咨询**」：有命令动词就是命令；
#     必须是疑问句**且**指向报表业务对象（:func:`mentions_report_object`）
#     才算咨询。
#   - **确定性快路**问的是「这条消息是不是**在问这块卡片的能力**」
#     （可展示方式 / 可用粒度）：它**不要求**指向报表业务对象 ——
#     「这个可以展示吗」就该答。它排除的只是「**要改这块卡**」的那几个动词；
#     「导出 / 保存 / 发布 / 重新开始」这些**不是对卡片的改动**，
#     所以**不在**快路的排除表里。
#
# 放在本模块是为了让它们与意图门那套**同处一地**：漂移风险从「跨文件复制
# 粘贴」降成「同文件内两个具名集合」。
#
# 词表**内容一个字都不许改**（改了就是改既有业务输出）—— 只搬家、只改引用。

# 只读能力查询的疑问词。命中之后才尝试用确定性证据直接回答，
# 避免把「把趋势图改成折线图」这类修改请求误判成查询。
READONLY_QUESTION_WORDS = (
    "哪些", "什么", "怎么", "能不能", "可不可以", "能否", "是否",
    "有没有", "支持", "吗", "？", "?",
)

# 明确的改动动词：出现这些词就是在下指令，不是在提问。
# 「这个能按分钟看吗」是提问，「改成按分钟粒度」是指令；
# 即使指令后面带问号（「改成按分钟粒度，可以吗」），也仍然是指令。
EDIT_COMMAND_WORDS = (
    "改成", "改为", "换成", "换为", "调成", "设为", "设定为",
)


# --------------------------------------------------------------------------
# 问题反馈与目录外新增（#58）
# --------------------------------------------------------------------------
#
# 这两类都不在既有的九类意图里，但必须落在**同一个**意图体系上扩展，
# 不能另起一套平行判定。它们各自解决一个具体误判：
#
#   - 问题反馈：「好丑啊，一个点……」没有疑问标记，:func:`is_readonly_question`
#     的四条判据（疑问标记 + 业务对象 + 无改动动词 + 非生成请求）不成立，
#     于是直接掉进编辑快路，被当成「明确修改」。
#   - 目录外新增：用户要加一个目录里没有的指标，走通用模拟策略，
#     不能静默映射到「总耗电」这类已有指标。

#: 对当前这份产出不满的表达。
#:
#: 只收**评价当前结果**的说法。用户如果同时给了改动动词
#: （「好丑，改成柱状图」），那是明确修改，不走这里。
_ISSUE_FEEDBACK_MARKERS: tuple[str, ...] = (
    "好丑", "太丑", "难看", "不好看",
    "不满意", "失望", "离谱", "莫名其妙", "奇怪",
    "不合理", "看不懂", "不明白",
    "一个点", "只有一个点", "就一个点", "一个数据点",
    "点太少", "太少点", "就一个", "只有一个",
    "没出来", "没显示", "不显示", "空白",
    "重叠", "不清晰", "不直观", "看不出趋势", "看不出变化",
)

#: 「就 2 个点」这类数量异常的说法。
#:
#: 「点级」「点位」不是数据点（那是粒度），排除掉。
_RE_ISSUE_POINT_COUNT = re.compile(
    r"(?:\d+|[一二三四五六七八九十两])\s*个?\s*(?:数据)?点(?!级|位)"
)

#: 「对当前结果不满」的**评价性形容词**（痛点词）。
#:
#: 固定词表（``_ISSUE_FEEDBACK_MARKERS``）只登记了「好丑」「看不懂」这类**直接**
#: 说辞；「颜色太扎眼了」「趋势太单调了」这种带**程度副词**的抱怨不在词表里，
#: 会被判成 ``UNKNOWN`` → 掉进编辑快路（工单 #58 独立验收的 P3 缺口）。
#:
#: 这里补的是**结构性特征**：程度副词 + 评价性形容词、否定式体验说法。
#: 句子不必逐条登记 —— 配上「没有改动动词、没有疑问标记」就能判出来。
#:
#: 只收**负面**评价词。「好看」「漂亮」「不错」这类正面评价不在这里：
#: 它们不是「问题反馈」，也不是修改指令，交给别的层判。
_ISSUE_FEEDBACK_ADJECTIVES: tuple[str, ...] = (
    # 观感
    "扎眼", "刺眼", "晃眼", "单调", "难看", "不好看", "模糊", "潦草",
    "拥挤", "简陋", "粗糙", "做作", "磨叽", "花哨",
    # 可读性
    "费劲", "费眼", "费力", "难懂", "难用", "费解", "迷惑", "冗长",
    "冗余", "累赘", "空洞", "没用", "无意义", "不清晰", "不直观",
    # 体验
    "不友好", "不专业", "反直觉", "头疼", "尴尬", "糟心", "不适应",
    "不舒服", "不方便", "别扭",
    # 数值观感
    "太大", "太小", "太多", "太少", "偏高", "偏低", "过长", "过短",
    # 单字评价词：只在「程度副词 + 单字」或「单字 + 了」这两种形状里用
    "丑", "挤", "糊", "差", "糟", "烂", "乱", "密", "闷", "晕", "烦", "累",
)

#: 抱怨句里最常见的**程度副词**（「太 X 了」「很 X」「有点 X」）。
_ISSUE_DEGREE_WORDS: tuple[str, ...] = (
    "有点", "有些", "过于", "非常", "特别", "格外", "相当", "十分",
    "极其", "略微", "完全", "太", "很", "极", "挺", "真", "巨", "超",
)

#: 否定式体验说法：说不清哪里不好，但明确表示「用不下去 / 看不明白」。
_ISSUE_NEGATIVE_USE: tuple[str, ...] = (
    "看不下去", "看不下", "没法看", "没法用", "不能看", "用不了",
    "不好用", "搞不懂", "看不明白", "受不了", "忍不了",
)


def _alternation(words: tuple[str, ...]) -> str:
    """把一组词拼成正则分支（长的排前面，避免「太大」被「太」抢先匹配）。"""
    return "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))


_ISSUE_ADJ_PATTERN = _alternation(_ISSUE_FEEDBACK_ADJECTIVES)
_ISSUE_DEGREE_PATTERN = _alternation(_ISSUE_DEGREE_WORDS)

#: 「程度副词 + 评价性形容词」：「太扎眼了」「很费劲」「有点糊」。
_RE_ISSUE_DEGREE_ADJ = re.compile(
    rf"(?:{_ISSUE_DEGREE_PATTERN})\s*(?:地)?\s*(?:{_ISSUE_ADJ_PATTERN})"
)

#: 「评价性形容词 + 了」：「单调了」「难用了」。
_RE_ISSUE_ADJ_DONE = re.compile(rf"(?:{_ISSUE_ADJ_PATTERN})了")

#: 「加一个 / 新增」这类新增请求的说法。
_ADD_REQUEST_WORDS: tuple[str, ...] = (
    "加一个", "加个", "再加一个", "再加个", "新增", "增加", "添加",
    "补充一个", "补充一个", "加张", "加个板块", "加个卡片",
)


def _has_structural_complaint(text: str) -> bool:
    """句子的**形状**像不像「对当前结果不满」。

    固定词表认不出的抱怨句（「颜色太扎眼了」「一眼看过去很费劲」）
    用形状来认（工单 #58 独立验收的 P3 缺口）：

    1. 「程度副词 + 评价性形容词」——「太扎眼了」「很费劲」「有点糊」；
    2. 「评价性形容词 + 了」——「单调了」「难用了」；
    3. 否定式体验说法——「看不下去」「没法看」。

    带**疑问标记**的句子不在这里当抱怨吞掉：它更可能是提问，
    交给 :func:`is_readonly_question` 那一层判。
    """
    if any(marker in text for marker in _QUESTION_MARKERS):
        return False
    if any(word in text for word in _ISSUE_NEGATIVE_USE):
        return True
    if _RE_ISSUE_DEGREE_ADJ.search(text):
        return True
    return bool(_RE_ISSUE_ADJ_DONE.search(text))


def _has_issue_marker(text: str) -> bool:
    """句子里有没有「对当前结果不满」的信号。"""
    if any(marker in text for marker in _ISSUE_FEEDBACK_MARKERS):
        return True
    if _RE_ISSUE_POINT_COUNT.search(text):
        return True
    return _has_structural_complaint(text)


def detect_issue_feedback(message: str, *, subject_required: bool = True) -> bool:
    """这句话是不是「对当前这份报表的问题反馈」。

    与 :func:`is_readonly_question` 的关键区别：**不要求疑问标记**。
    「昨日能耗趋势好丑啊，一个点……」一个问号都没有，但它既不是
    提问也不是指令 —— 它是在说当前这份结果哪里不对劲。

    判据：

        1. 不是生成请求；
        2. 没有明确改动动词（带改动动词的仍然是明确修改）；
        3. 有「不满 / 数量异常」的表达 —— 固定词表，或者
           :func:`_has_structural_complaint` 认出来的**结构特征**
           （程度副词 + 评价性形容词等）；
        4. 指向报表业务对象 —— ``subject_required=False`` 时跳过，
           因为编辑态里「对象」就是当前这张草稿（规格第 51 条）。
    """
    text = (message or "").strip()
    if not text:
        return False
    if is_clear_generation_request(text):
        return False
    if has_operation_command(text):
        return False
    if not _has_issue_marker(text):
        return False
    if not subject_required:
        return True
    return mentions_report_object(text)


def _metric_candidates(text: str) -> list[str]:
    """从句子里抽出「像指标名」的候选词。"""
    candidates: list[str] = []
    for match in _RE_QUOTED.finditer(text):
        token = match.group(1).strip()
        if not token or token in _QUOTED_STOPWORDS:
            continue
        if any(hint in token for hint in _METRIC_WORD_HINTS):
            candidates.append(token)
    for match in _RE_METRIC_SUFFIX.finditer(text):
        token = match.group(0)
        if any(hint in token for hint in _RE_DERIVED_HINTS):
            continue
        if any(hint in token for hint in _METRIC_WORD_HINTS):
            candidates.append(token)
    return candidates


def detect_out_of_catalog_add(
    message: str, *, known_metrics: Iterable[str] | None = None
) -> bool:
    """这句话是不是「要新增一个目录里没有的指标」。

    目录外新增按通用模拟策略走（规格第 6 条）：要么先澄清，要么
    标成显式假设后给候选，**不能**静默映射到已有指标。

    ``known_metrics`` 传当前授权范围内已登记的指标名（label 或 alias）。
    没有它就判不了 —— 返回 False，交给既有流程按普通新增处理。
    """
    text = (message or "").strip()
    if not text:
        return False
    if not any(word in text for word in _ADD_REQUEST_WORDS):
        return False
    if not _RE_REPORT_REQUEST.search(text):
        return False
    candidates = _metric_candidates(text)
    if not candidates:
        return False
    known = {str(item).strip().lower() for item in (known_metrics or [])}
    if not known:
        return False
    name = max(candidates, key=len)
    return name.lower() not in known


#: 问的是「当前对话已记录的阶段 / 进度」的说法。
#:
#: 只收**进度语义**的说法。「预计时间」「百分之」这类词在本产品里另有含义
#: （时间范围筛选、指标口径），不能拿它们当进度信号，否则能力问题会被
#: 误判成进度问题。
_PROGRESS_QUESTION_MARKERS: tuple[str, ...] = (
    "进度", "做到哪", "到哪一步", "哪一步了", "做到哪儿", "进行到",
    "跑到多少", "还要多久", "还剩几步", "完了吗", "完成了吗",
    "进展", "什么状态", "当前状态", "现在状态", "目前状态", "到哪儿了",
)


def is_progress_question(message: str) -> bool:
    """这句是不是在问「当前对话已记录的业务阶段 / 进度」。

    命中意味着：回答里必须说清「这是已记录的状态，不是实时进度」
    （工单 #03 / 父规格 #12）。问的是别的事（查草稿、问能力）时不命中 ——
    给能力回答补一句进度边界，是把口径加错地方。
    """
    text = (message or "").strip()
    if not text:
        return False
    return any(marker in text for marker in _PROGRESS_QUESTION_MARKERS)


# --------------------------------------------------------------------------
# 和确定性组装层的对接
# --------------------------------------------------------------------------

#: 业务槽位 → builder 参数名的候选列表（camelCase / snake_case 都兼容）。
#: builder 还没支持某个参数时，调用方不能把该条件悄悄丢掉。
SLOT_TO_KWARG: dict[str, tuple[str, ...]] = {
    "sourceRef": ("sourceRef", "source_ref"),
    "grainUnit": ("grainUnit", "grain_unit"),
    "timeRange": ("timeRange", "timeWindow", "time_window"),
    "metricRefs": ("metricRefs", "metric_refs"),
    "componentRequests": ("componentRequests", "component_requests"),
    "excludedRegions": ("excludedRegions", "excluded_regions"),
    "filters": ("filters",),
    "topN": ("topN", "top_n", "limit"),
    "derivedColumns": ("derivedColumns", "derived_columns"),
    "styleHints": ("styleHints", "style_hints"),
    # 未生效诉求清单：不是给 builder 的实现条件，是让它写进
    # metadata.unsupportedStyleRequests 好让上层明确披露「没生效」。
    "unsupportedStyleRequests": (
        "unsupportedStyleRequests",
        "unsupported_style_requests",
    ),
}

_BUILDER_CACHE: tuple[Any, frozenset[str], bool] | None = None


def builder_capability() -> tuple[frozenset[str], bool]:
    """builder 当前接受哪些参数（按函数对象缓存，builder 被替换时缓存自动失效）。"""
    global _BUILDER_CACHE
    from app.services.report_draft_service import build_definition_draft

    if _BUILDER_CACHE is not None and _BUILDER_CACHE[0] is build_definition_draft:
        return _BUILDER_CACHE[1], _BUILDER_CACHE[2]
    import inspect

    try:
        signature = inspect.signature(build_definition_draft)
    except (TypeError, ValueError):  # pragma: no cover - 极端情况
        params: frozenset[str] = frozenset()
        has_var_kw = False
    else:
        params = frozenset(signature.parameters)
        has_var_kw = any(
            param.kind is inspect.Parameter.VAR_KEYWORD
            for param in signature.parameters.values()
        )
    _BUILDER_CACHE = (build_definition_draft, params, has_var_kw)
    return params, has_var_kw


def plan_builder_kwargs(slots: dict[str, Any]) -> dict[str, Any]:
    """把槽位映射成 builder 能接受的参数。

    只传 builder 真正支持的参数；接不住的条件由调用方提前交回正常路由，
    不在这里被静默丢弃。
    """
    params, has_var_kw = builder_capability()
    kwargs: dict[str, Any] = {}
    for slot, names in SLOT_TO_KWARG.items():
        if slot not in slots:
            continue
        value = slots[slot]
        if value is None or value == "" or value == [] or value == {}:
            continue
        for name in names:
            if name in params:
                kwargs[name] = value
                break
        else:
            if has_var_kw:
                kwargs[names[0]] = value
    return kwargs


def unsupported_slots(slots: dict[str, Any]) -> list[str]:
    """当前流程接不住、必须交回正常路由的槽位名。

    两类来源：

    1. builder 接不住的参数（传下去条件会丢）；
    2. **当前模板不支持的版式/样式诉求**（独占一行 / 左右等宽 / 柱状图 /
       标题字号·对齐·颜色）。这类诉求模板流表达不了 —— 它是固定模板，
       没有版式和标题样式的表达位。交回正常路由，由正常路由统一披露
       「没有生效」，不能在模板流里静默丢掉，更不能悄悄按默认版式生成、
       却跟用户说「已按要求处理」。
    """
    params, has_var_kw = builder_capability()
    unsupported: list[str] = []
    for slot, names in SLOT_TO_KWARG.items():
        if slot not in slots:
            continue
        value = slots[slot]
        if value is None or value == "" or value == [] or value == {}:
            continue
        if not any(name in params for name in names):
            unsupported.append(slot)
    if not has_var_kw and slots.get("unsupportedStyleRequests"):
        # 模板流接不住这类诉求：固定模板没有版式/标题样式的表达位。
        # 交回正常路由，由那边明确披露「没有生效」。
        unsupported.append("unsupportedStyleRequests")
    return unsupported


_RE_EXCLUSIVE_WORDS = re.compile(
    r"(只要|只要一个|仅要|仅限|只放|只保留|只生成|不要其他|不要别的|不要其它|只统计以下)"
)


def is_exclusive_request(message: str) -> bool:
    """用户说的是「只要这些」还是「包含这些」。

    「只要 / 只放」是白名单语义（没点名的区块一律不要）；
    「包含 / 需要」是包含语义（没点名的不代表不要）。
    """
    return bool(_RE_EXCLUSIVE_WORDS.search(message or ""))


def resolve_include_trend(slots: dict[str, Any]) -> bool:
    """要不要趋势图。

    - 白名单语义（「只要总耗电和排名」）→ 看白名单里有没有 trend；
    - 包含语义 / 未指定 → 跟模板默认一致，除非用户明确说了「不要趋势」。
    """
    components = slots.get("componentRequests")
    if isinstance(components, list) and components:
        return "trend" in components
    excluded = slots.get("excludedRegions") or []
    if "trend" in excluded:
        return False
    return True


# --------------------------------------------------------------------------
# 用户 JSON 输入（工单 #64）
# --------------------------------------------------------------------------


#: 数组位置标记行。用户在消息里写明记录数组在报文里的位置
#: （规格第 31 条），服务端按它取数组，不自己挑。
#:
#: **冒号可省**：契约表 §4.3 登记了两种写法 —— ``数组位置: data.records``
#: 与 ``@arrayPath data.records``。后者**没有冒号**，所以这里的冒号是
#: 可选的：两种写法都要认（写了冒号也认，不写也认）。
_ARRAY_PATH_MARKER = re.compile(
    r"^\s*(?:@arrayPath|arrayPath|数组位置)\s*(?:[:：]\s*)?(?P<path>(?![:：])[^\s]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def extract_array_path(message: str) -> str | None:
    """从消息里取「数组位置」标记。

    约定：单独一行写成 ``数组位置: data.records`` 或 ``@arrayPath data.records``
    （契约表 §4.3 登记的两种写法，**冒号可省**）。
    没有这一行 → ``None``（服务端按「顶层数组」或「唯一数组」处理）。
    """
    match = _ARRAY_PATH_MARKER.search(message or "")
    if not match:
        return None
    path = match.group("path").strip()
    return path or None


def _scan_json_span(text: str, start: int) -> tuple[str, bool]:
    """从 ``start`` 开始扫一段 JSON，返回 ``(片段, 是否闭合)``。

    括号计数**跳过字符串字面量**：JSON 里的 ``]``、``{`` 可能在字符串里。
    """
    pairs = {"[": "]", "{": "}"}
    stack: list[str] = []
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char in pairs:
            stack.append(pairs[char])
            continue
        if char in ("]", "}"):
            if not stack or stack[-1] != char:
                return text[start : index + 1], True
            stack.pop()
            if not stack:
                return text[start : index + 1], True
    return text[start:], False


def _looks_like_json(fragment: str) -> bool:
    """这段文字像不像一份 JSON 数据。

    判据保守：必须以 ``[`` / ``{`` 开头，**并且**含引号 ——
    ``[重要]``、``[1, 2]`` 这种不是记录数据，不该被当成 JSON 输入。
    宁可漏判（当成普通消息），不可误判（把一句中文报成语法错）。
    """
    text = fragment.strip()
    if not text or text[0] not in "[{":
        return False
    return '"' in text


def detect_json_payload(message: str) -> str | None:
    """从一条聊天消息里找出用户贴的 JSON 数据段。

    返回**原文片段**（调用方负责校验与存储）；没有 JSON 段时返回 ``None``。

    宁可漏判不可误判：普通中文消息里带方括号的，不当成 JSON 输入。

    这条判据**只认含引号的片段** —— 判据与取舍理由见
    :func:`_looks_like_json`。没引号但明显是数据的那类（``[]``、
    ``[1,2,3]``）由 :func:`detect_data_array_payload` 单独认，
    两条路径互不干扰。
    """
    text = message or ""
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        fragment, _closed = _scan_json_span(text, index)
        if _looks_like_json(fragment):
            return fragment
    return None


# --------------------------------------------------------------------------
# 没引号、但明显是数据的方括号数组（工单 #66E）
# --------------------------------------------------------------------------

#: 「用户想给 JSON」的**登记在案**的信号：契约表 §4.3 的数组位置标记。
#:
#: 契约里登记的 JSON 输入写法就这两种（``数组位置: data.records`` /
#: ``@arrayPath data.records``），消息里有它就说明用户想给 JSON。
#:
#: 只认**独立成行**的标记（与 :func:`extract_array_path` 同一口径）：
#: 标记后面**还写了别的字**就不认 —— 那是聊天，不是标记。
#: 「只写标记、不写路径」也算（说明用户想给数据，只是没说清位置）。
_BARE_ARRAY_PATH_MARKER = re.compile(
    r"^[ \t]*(?:@arrayPath|arrayPath)[ \t]*[:：]?[ \t]*$"
    r"|^[ \t]*数组位置[ \t]*[:：]?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

#: 一串数字（可带小数与指数），或什么都没有 —— 方括号里**只可能是数据**的内容。
_NUMBER_TOKEN = r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
_NUMERIC_ARRAY_BODY = re.compile(
    rf"^\s*(?:{_NUMBER_TOKEN}\s*(?:,\s*{_NUMBER_TOKEN}\s*)*)?$"
)

#: 片段之后允许跟的**句末**内容：空白与中英文句读。
#: 粘数据的习惯是「说明 + 数据」，数据后面通常只有标点。
_TRAILING_PUNCTUATION = re.compile(r"^[\s。，、；：！？,.;:!?]*$")


def _bracket_spans(text: str) -> list[tuple[int, int, str]]:
    """消息里所有**完整**的 ``[...]`` 片段（起止位置与原文）。

    扫到一段就跳过整段，所以嵌套的方括号不会被单独当成一段。
    """
    spans: list[tuple[int, int, str]] = []
    index = 0
    while index < len(text):
        if text[index] != "[":
            index += 1
            continue
        fragment, closed = _scan_json_span(text, index)
        if not closed:
            break
        spans.append((index, index + len(fragment), fragment))
        index += len(fragment)
    return spans


def detect_data_array_payload(message: str) -> str | None:
    """认出「没有引号、但明显是数据」的方括号数组（工单 #66E）。

    为什么要有它：:func:`detect_json_payload` 要求片段**含引号**，于是
    ``[]``、``[1,2,3]`` 这类输入**根本不会被当成 JSON 输入轮** ——
    用户粘了数据，服务端却当普通需求处理，给了一份**模拟示例**方案，
    界面一句提示都没有（规格第 30 条要避免的正是这件事）。

    判据保守（宁可漏判不可误判 —— 与 :func:`_looks_like_json` 同一条原则）：

    1. 片段是**完整的** ``[...]``，内容**只可能是数字**（或空的）——
       ``[重要]``、``[a,b]`` 这种不是数据，不认；
    2. 消息里**只有这一处**这样的片段 —— 有两处就分不清用户指的是哪一段，
       **不猜**（与契约表 §4.3「服务端不替用户挑数组」同一条道理）；
    3. 这一段在**消息末尾**（后面只剩空白或句读）—— 粘数据的习惯是
       「说明 + 数据」，数据放在最后（前端 JSON 粘贴入口也是这个顺序）。

    返回原文片段（调用方负责校验与存储）。
    """
    text = message or ""
    candidates: list[tuple[int, int, str]] = []
    for start, end, fragment in _bracket_spans(text):
        if not (fragment.startswith("[") and fragment.endswith("]")):
            continue
        if not _NUMERIC_ARRAY_BODY.match(fragment[1:-1]):
            continue
        candidates.append((start, end, fragment))
    if len(candidates) != 1:
        return None
    _start, end, fragment = candidates[0]
    if not _TRAILING_PUNCTUATION.match(text[end:]):
        return None
    return fragment


#: 「路径」长得像路径：ASCII 词或点分路径（``data.records`` / ``records``）。
_PATH_TOKEN = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")


def has_json_input_intent(message: str) -> bool:
    """消息里有没有「用户想给 JSON」的**登记在案**的信号。

    契约表 §4.3 登记的数组位置标记只出现在 JSON 输入里 —— 消息里有它，
    就说明用户想给 JSON。

    认的是**独立成行**的标记（``数组位置: data.records`` /
    ``@arrayPath data.records``，或者只写标记不写路径），与
    :func:`extract_array_path` 同一口径：标记后面还写了别的字就不认
    （那是聊天，不是标记）。

    路径还得**长得像路径**（``data.records`` / ``records``）：
    「数组位置怎么填」这种问句里也带标记字样，但后面跟的是中文，
    不是路径 —— 那是在聊天，不是在给数据。
    """
    text = message or ""
    path = extract_array_path(text)
    if path is not None and _PATH_TOKEN.fullmatch(path):
        return True
    return bool(_BARE_ARRAY_PATH_MARKER.search(text))


def strip_detected_json(message: str, payload: str) -> str:
    """把消息里**已经认出来的那一段** JSON 去掉，只留人话部分。

    与 :func:`strip_json_payload` 同一套口径，但「认哪一段」由调用方决定 ——
    聊天入口认出来的可能是含引号的片段，也可能是不含引号的数组形状
    （工单 #66E）。
    """
    if not payload:
        return (message or "").strip()
    text = (message or "").replace(payload, " ")
    return re.sub(r"\s{2,}", " ", text).strip()


def strip_json_payload(message: str) -> str:
    """把消息里的 JSON 段去掉，只留人话部分。

    用于**聊天消息只保存安全摘要与引用**（规格第 21 条）：
    落库的用户消息里不留完整数组。
    """
    fragment = detect_json_payload(message)
    return strip_detected_json(message, fragment or "")
