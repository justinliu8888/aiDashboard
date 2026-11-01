"""TemplateEditEngine：节点 5.5 USER_EDITING_TEMPLATE 的核心引擎。

职责：
    1. 从用户自然语言（当前阶段用规则）解析出 EditPlan；
    2. 根据 EditPlan 应用到 ReportDefinitionDraft，产出新草稿 + ChangeSet；
    3. 产出面向用户的 Markdown / summary / 影响说明；
    4. 校验 compilation 粒度 / 数据源切换 / 展示形式是否可行。

ContextEngine 负责判断「这句话是不是模板编辑」；具体执行由本引擎完成。
AgentService 只负责编排状态和包装 AgentResponse。
"""
from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.schemas.change_set import ChangeSet, ChangeSetEntry, ChangeSetUnchanged
from app.schemas.edit_plan import (
    DISPLAY_MODE_PLACEHOLDER_ONLY,
    DISPLAY_MODE_SIMULATED,
    EditIntent,
    EditMetricAddition,
    EditOperation,
    EditOperationStep,
    EditPlan,
    EditTarget,
)
from app.schemas.report_definition import ReportDefinitionDraft
from app.schemas.report_plan import ComponentType
from app.services.catalog_mock_service import (
    list_catalogs,
    match_source_by_alias,
)
from app.services.data_source_mode import source_mode_of_definition
from app.services.effective_combination import (
    combination_matches,
    combination_of_definition,
)
from app.services.edit_time_resolver import (
    STATUS_RESOLVED,
    extract_change_value,
    resolve_time_expression,
)
from app.services.metric_mock_service import list_metrics, match_metric_by_alias
from app.services.out_of_catalog_candidate import (
    STATUS_OUT_OF_CATALOG,
    OutOfCatalogCandidate,
    candidate_from_dict,
    out_of_catalog_region_reason,
    resolve_out_of_catalog_candidate,
    select_component_type,
    selection_for_existing_type,
    simulated_metric_binding,
    split_metric_intent,
)
from app.services.report_template_service import (
    DATA_SOURCE_LABEL,
    classify_metric_kind,
)
from app.services.scope_service import filter_visible
from app.services.simulated_field_semantics import (
    SCOPE_METRIC_FAMILY,
    deterministic_scope,
    intent_names_registered_metric,
    metric_family_seed_key,
    simulated_metric_id,
)
from app.services.template_definition_service import adapt_preview_spec
from app.services.validation_service import fixed_interval_error
from app.tools.context import ToolScope


logger = logging.getLogger(__name__)


# ---------- 关键词匹配 ----------

DELETE_WORDS = ("删掉", "删除", "去掉", "不要", "移除")
RENAME_WORDS = ("标题改成", "标题改为", "名字改成", "名字改为", "重命名为", "改名为", "叫做")
REPORT_RENAME_WORDS = (
    "报表名称改成",
    "报表名称改为",
    "报表名改成",
    "报表名改为",
    "报告名称改成",
    "报告名称改为",
)
ADD_WORDS = (
    # 长说法在前：先剥掉更长的那个，剥出来的才是指标意图。
    # 只收「加一个 / 加个」这类**量词**说法：光一个「增加」「添加」太宽，
    # 「把点数增加一点」会被当成新增请求。
    "再加一个", "再加个", "增加一个", "增加个", "补充一个", "补充个",
    "添加一个", "添加个", "新增一个", "新增个",
    "加一个", "加个", "加张", "加条",
    "新增",
)
#: 说明用户谈的是「粒度」这件事的**话题词**（与具体取值无关）。
#:
#: 这里**不收取值词**（「按小时」「小时级」…）：取值只认明确点名的写法
#: （``GRAIN_EXPLICIT_FROM_TEXT``），取值词出现在卡片名里时不是新值。
#: 旧表把取值词也当成话题词，于是「小时趋势」这种卡片名会被算成粒度话题。
GRAIN_WORDS = ("粒度", "compile", "compilation")

CHART_KIND_MAP = {
    "柱状图": "BAR",
    "条形图": "BAR",
    "折线图": "LINE",
    "面积图": "AREA",
    "饼图": "PIE",
    "环图": "PIE",
}
TABLE_WORDS = ("表格", "明细表", "数据表")

#: 方案组件类型 → 编辑入口直接写**定义层**区域时的形状。
#:
#: 编辑引擎改的是**定义草稿**（不是方案），所以这里给的是定义层的组件
#: 类型与角色。方案侧那一跳由 ``template_definition_service`` 负责。
EDIT_COMPONENT_SHAPES: dict[str, dict[str, str]] = {
    "metric_card": {"componentType": "METRIC_CARD", "chartKind": "", "role": "SUMMARY"},
    "line_chart": {"componentType": "CHART", "chartKind": "LINE", "role": "TREND"},
    "bar_chart": {"componentType": "CHART", "chartKind": "BAR", "role": "DETAIL"},
    "pie_chart": {"componentType": "CHART", "chartKind": "PIE", "role": "DETAIL"},
    "table": {"componentType": "TABLE", "chartKind": "", "role": "DETAIL"},
    "text": {"componentType": "TEXT", "chartKind": "", "role": "DESCRIPTION"},
}

#: 定义层组件类型（+ 图形）→ 方案组件类型。选型依据要按**同一类型**回查
#: 规则库（用户自己说了展示形式时，类型不能被静默改写，依据也不能空白）。
PLAN_TYPE_BY_DEFINITION: dict[tuple[str, str], str] = {
    ("METRIC_CARD", ""): "metric_card",
    ("CHART", "LINE"): "line_chart",
    ("CHART", "BAR"): "bar_chart",
    ("CHART", "PIE"): "pie_chart",
    ("TABLE", ""): "table",
    ("TEXT", ""): "text",
}

#: 「可以怎么展示」的各种说法。
#:
#: 旧表只认「怎么展示 / 可以展示 / 展示方式 / 展示形式」，用户说
#: 「可以用其他展示方法吗？」时一个都不命中，结果是既没回答也没产出
#: 候选，只能回一句「你想改哪个 section 的展示方式？」（工单 #58）。
SUGGEST_VISUALIZATION_WORDS = (
    "怎么展示", "可以展示", "展示方式", "展示形式",
    "展示方法", "其它展示", "其他展示", "别的展示", "换个展示",
)

#: 时间窗词表**不再自带一份**。
#:
#: 工单 #58：编辑态有两个时间表达解析器（这里与 ``edit_time_resolver``），
#: 两边各带一张表就会各说各话 —— 「昨日」在一处是无锚点裸词、在另一处
#: 是日锚点。现在统一走 ``edit_time_resolver.resolve_time_expression``，
#: 同一套锚点规则、同一份标签。


GRAIN_FROM_TEXT = {
    "分钟": "MINUTE",
    "分钟级": "MINUTE",
    "按分钟": "MINUTE",
    "minute": "MINUTE",
    "小时": "HOUR",
    "小时级": "HOUR",
    "按小时": "HOUR",
    "hour": "HOUR",
    "天": "DAY",
    "天级": "DAY",
    "日级": "DAY",
    "按天": "DAY",
    "day": "DAY",
}

# 明确点名粒度的说法。「小时趋势」这类卡片名里也含「小时」，
# 只有这些写法能代表用户真的要改粒度。
GRAIN_EXPLICIT_FROM_TEXT = {
    "按分钟": "MINUTE",
    "分钟级": "MINUTE",
    "分钟粒度": "MINUTE",
    "按小时": "HOUR",
    "小时级": "HOUR",
    "小时粒度": "HOUR",
    "按天": "DAY",
    "天级": "DAY",
    "天粒度": "DAY",
    "日级": "DAY",
    "日粒度": "DAY",
    "按日": "DAY",
}

TREND_GRAIN_TITLES = {
    "MINUTE": "分钟趋势",
    "HOUR": "小时趋势",
    "DAY": "日趋势",
}


#: 「追加」语义的说法 —— **新增默认是追加**。
#:
#: 用户说「再加 X」「还需要包括 X、Y」时，**保留**原有内容并新增；
#: 只有明确说了「替换 / 只留 / 换成」才走替换（见 ``REPLACE_WORDS``）。
#:
#: 这里收的是**意图词**，不是指标名：具体指标由用户原话抽，不写死词表。
#:
#: 只收**明确带追加意思**的说法：光一个「增加」太宽（「把温度增加一点」
#: 会被当成新增指标），带量词的那一档由 ``ADD_WORDS`` 负责。
APPEND_WORDS = (
    "追加", "再加", "还要", "还需要", "也需要", "也要加", "也加上",
    "再增加", "再添加", "再新增", "再补", "再添",
    "补充", "加上",
)

#: 「替换」语义的说法 —— 只有明确说了这些，追加才让位给替换。
REPLACE_WORDS = (
    "换成", "替换", "只留", "只保留", "改用", "不要了", "去掉", "移除",
)

#: 「把 A 换成 B」里的**替换动词**。
#:
#: 「不要 X，改成 Y」这句话里既有否定词（DELETE_WORDS）又有替换动词 ——
#: 它说的是**替换**，不是删除。判据只看这两个词表的共现，**不认具体
#: 指标名**，所以任何「不要/去掉 A，改成 B」的说法都走同一条路。
REPLACE_CLAUSE_VERBS = (
    "换成", "换为", "改成", "改为", "调成", "替换", "改用", "只留", "只保留", "代替",
)

#: 指标清单里表示「还要 / 并且」的连接词（剥掉它们才看得出指标名）。
_APPEND_CONNECTORS = (
    "还包括", "包括", "包含", "含有", "要有", "以及", "还有", "要", "有",
)

#: 指标清单里的分隔符（顿号 / 逗号 / 连词）。
_METRIC_LIST_SEPARATORS = (
    "、", "，", ",", "；", ";", "以及", "还有", "和",
)

#: 指标名末尾的成分词（不是指标名的一部分）。
_METRIC_NAME_TAIL_NOISE = ("的指标", "指标", "这几项", "这些指标", "项指标")

#: 只由「数量词」组成的片段 —— 「再加三个指标」里的「三个」是**数量**，
#: 不是指标名。认出来就说明用户还没说清要加哪几个。
_COUNT_ONLY_CHARS = set("一二三四五六七八九十两百千万个项条多少几若干数字")

#: 「先占位」的说法 —— 用户**明确**要求时，新指标只显示名称不塞数值。
PLACEHOLDER_MODE_WORDS = (
    "占位", "只显示名称", "只显示名字", "先加标签", "加个标签", "只加标签",
    "先显示名字", "不显示数值", "不要数值", "先不显示数值", "只显示标题",
    "先放个名字", "先出个名字",
)

#: 「直接出数值」的说法。
SIMULATED_MODE_WORDS = (
    "显示出数", "显示数值", "带数值", "有数值", "直接出数", "直接显示",
    "填上数值", "按示例出数", "照常出数", "出数值",
)

#: 组合修改允许的意图：都只作用在同一个已有 region 上，
#: 不新增 / 删除 / 改名 region，也不改报表本身。
COMPOSABLE_INTENTS = frozenset({
    EditIntent.CHANGE_DATA_SOURCE,
    EditIntent.CHANGE_COMPILATION_GRAIN,
    EditIntent.CHANGE_METRIC,
    EditIntent.CHANGE_VISUALIZATION,
    EditIntent.CHANGE_TIME_RANGE,
    EditIntent.CHANGE_TOP_N,
    EditIntent.CHANGE_SORT,
    EditIntent.RENAME_SECTION,
})


# ---------- 展示形式候选 ----------

ROLE_DATA_SHAPE = {
    "REPORT_TITLE": "TEXT_HEADING",
    "SUMMARY": "SINGLE_NUMBER",
    "TREND": "TIME_SERIES",
    "DETAIL": "CATEGORY_RANKING",
    "QUALITY": "QUALITY_STATUS",
    "ANALYSIS": "TEXT_ANALYSIS",
}


DISPLAY_OPTIONS_BY_SHAPE: dict[str, dict[str, Any]] = {
    "SINGLE_NUMBER": {
        "options": [
            {"label": "KPI 卡", "componentType": "METRIC_CARD", "chartKind": "", "recommended": True,
             "reason": "单个核心数值用 KPI 卡最直观"},
            {"label": "摘要卡", "componentType": "METRIC_CARD", "chartKind": "", "recommended": False,
             "reason": "适合展示单一摘要指标"},
        ],
        "disabled": [
            {"label": "折线图", "chartKind": "LINE", "reason": "单个数值无时间序列"},
            {"label": "饼图", "chartKind": "PIE", "reason": "单个数值无占比结构"},
        ],
    },
    "TIME_SERIES": {
        "options": [
            {"label": "折线图", "componentType": "CHART", "chartKind": "LINE", "recommended": True,
             "reason": "时间序列的变化趋势优先用折线图"},
            {"label": "面积图", "componentType": "CHART", "chartKind": "AREA", "recommended": False,
             "reason": "适合突出总量变化"},
            {"label": "表格", "componentType": "TABLE", "chartKind": "", "recommended": False,
             "reason": "适合查看明细数值"},
        ],
        "disabled": [
            {"label": "饼图", "chartKind": "PIE", "reason": "时间序列不适合用饼图展示"},
        ],
    },
    "CATEGORY_RANKING": {
        "options": [
            {"label": "柱状图", "componentType": "CHART", "chartKind": "BAR", "recommended": True,
             "reason": "分类排名用柱状图最便于对比大小"},
            {"label": "表格", "componentType": "TABLE", "chartKind": "", "recommended": False,
             "reason": "适合查看明细数值"},
        ],
        "disabled": [
            {"label": "折线图", "chartKind": "LINE", "reason": "分类排名不是时间序列"},
            {"label": "饼图", "chartKind": "PIE", "reason": "当前数据不是占比结构"},
        ],
    },
    "SHARE_STRUCTURE": {
        "options": [
            {"label": "饼图", "componentType": "CHART", "chartKind": "PIE", "recommended": True,
             "reason": "占比结构优先用饼图"},
            {"label": "堆叠柱状图", "componentType": "CHART", "chartKind": "BAR", "recommended": False,
             "reason": "适合对比多个类别的占比"},
            {"label": "表格", "componentType": "TABLE", "chartKind": "", "recommended": False,
             "reason": "适合查看明细占比"},
        ],
        "disabled": [
            {"label": "折线图", "chartKind": "LINE", "reason": "占比结构不是时间序列"},
        ],
    },
    "QUALITY_STATUS": {
        "options": [
            {"label": "状态卡", "componentType": "DATA_QUALITY", "chartKind": "", "recommended": True,
             "reason": "数据质量用状态卡展示覆盖率、缺失、新鲜度"},
            {"label": "表格", "componentType": "TABLE", "chartKind": "", "recommended": False,
             "reason": "适合查看明细质量指标"},
        ],
        "disabled": [
            {"label": "折线图", "chartKind": "LINE", "reason": "质量状态不是时间序列"},
            {"label": "饼图", "chartKind": "PIE", "reason": "质量状态不是占比结构"},
        ],
    },
    "TEXT_HEADING": {
        "options": [
            {"label": "标题文本", "componentType": "TEXT", "chartKind": "", "recommended": True,
             "reason": "标题区域只能用文本展示"},
        ],
        "disabled": [],
    },
    "TEXT_ANALYSIS": {
        "options": [
            {"label": "AI 解读", "componentType": "ANALYSIS", "chartKind": "", "recommended": True,
             "reason": "解读内容由 AI 文本输出"},
        ],
        "disabled": [],
    },
}


# ---------- 结果数据结构 ----------

@dataclass
class EditResult:
    """TemplateEditEngine 对外统一返回结构。"""

    plan: EditPlan
    draftBeforeEdit: ReportDefinitionDraft | None = None
    draftAfterEdit: ReportDefinitionDraft | None = None
    changeSet: ChangeSet | None = None
    reply: str = ""
    needsClarification: bool = False
    clarificationQuestion: str = ""
    markdownPreview: str = ""
    editingTargetRegionId: str | None = None
    debug: dict[str, Any] = field(default_factory=dict)


@dataclass
class _IntentOutcome:
    """单个意图在草稿副本上的校验 / 应用结果。"""

    change_set: ChangeSet | None = None
    reply: str = ""
    markdown: str = ""
    failure: str = ""   # 非空表示失败，内容是面向用户的说明
    reason: str = ""    # 失败分类，写进 debug.reason
    mark_plan_clarification: bool = False

    @property
    def is_failed(self) -> bool:
        return self.failure != ""

    @classmethod
    def failed(
        cls, message: str, *, reason: str, markdown: str = ""
    ) -> "_IntentOutcome":
        return cls(
            failure=message,
            reason=reason,
            markdown=markdown,
            mark_plan_clarification=reason == "incompatible_fields",
        )


# ---------- 工具 ----------

def _normalize_text(text: str) -> str:
    text = (text or "").lower()
    for token in ("这个", "那个", "section", "区域", "面板", "卡片", "模块", "部分", " "):
        text = text.replace(token, "")
    return text.strip()


def _region_label(region: dict[str, Any]) -> str:
    component = region.get("component") if isinstance(region.get("component"), dict) else {}
    metric = component.get("metric") if isinstance(component.get("metric"), dict) else {}
    return str(
        region.get("name")
        or component.get("title")
        or metric.get("label")
        or region.get("id")
        or "未命名 section"
    )


def _source_display_label(catalogs: dict[str, dict], source_ref: str | None) -> str:
    """数据源的业务说法（**不点内部标识**）。

    目录里登记的数据源都带一句业务描述（「EAS 能耗明细：按设备、分钟级
    采样的区间电量」），取描述的**前半句**当名字；目录里没有就退回
    「当前数据源」，不把 ``sourceRef`` 摆给用户看。
    """
    catalog = catalogs.get(str(source_ref or ""))
    description = str((catalog or {}).get("description") or "")
    name = description.split("：")[0].strip()
    return name or "当前数据源"


def _metric_display_label(metrics: dict[str, dict], metric_ref: str | None) -> str:
    """指标的业务说法（**不点内部标识**）。

    目录里登记的指标带一个用户看得懂的名字（「总耗电」）；查不到就退回
    「原指标」，不把 ``metricRef`` 摆给用户看。
    """
    metric = metrics.get(str(metric_ref or ""))
    return str((metric or {}).get("label") or "原指标")


#: 组件类型 → 用户看得懂的展示形式说法。
#:
#: 认不出就退回「图表」—— **不把枚举值原样摆给用户**（TABLE / BAR 这些
#: 是内部取值，用户看不懂）。
_COMPONENT_DISPLAY_LABELS: dict[str, str] = {
    "TEXT": "文字说明",
    "METRIC_CARD": "指标卡",
    "TABLE": "表格",
    "CHART": "图表",
    "DATA_QUALITY": "数据质量",
    "ANALYSIS": "分析说明",
}


def _component_display_label(component_type: str | None) -> str:
    """展示形式的业务说法（**不点内部枚举**）。"""
    key = str(component_type or "").strip().upper()
    return _COMPONENT_DISPLAY_LABELS.get(key, "图表")


#: 统计粒度 → 用户看得懂的说法。
_GRAIN_DISPLAY_LABELS: dict[str, str] = {
    "MINUTE": "分钟",
    "HOUR": "小时",
    "DAY": "天",
}


def _grain_display_label(grain: str | None) -> str:
    """统计粒度的业务说法（**不点内部枚举**）。"""
    key = str(grain or "").strip().upper()
    return _GRAIN_DISPLAY_LABELS.get(key, key or "当前粒度")


def _grain_display_labels(grains: Any) -> str:
    """一组粒度的业务说法（**不点内部枚举**）。"""
    items = grains if isinstance(grains, (list, tuple)) else []
    labels = [_grain_display_label(item) for item in items]
    return "、".join(label for label in labels if label)


#: 排序方向 → 用户看得懂的说法。
_SORT_DISPLAY_LABELS: dict[str, str] = {
    "ASC": "从小到大",
    "DESC": "从大到小",
}


def _sort_display_label(direction: str | None) -> str:
    """排序方向的业务说法（**不点内部枚举**）。"""
    key = str(direction or "").strip().upper()
    return _SORT_DISPLAY_LABELS.get(key, key or "当前排序")


#: 角色 → 用户的口语说法。
#:
#: 按**角色**给别名，不写死具体卡片名：用户说「明细」指的是明细类板块，
#: 说「趋势」指的是趋势类板块。这样「冰箱耗电明细可以用其他展示方法吗？」
#: 能定位到当前那张明细卡片，而不是落空（工单 #58）。
ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "DETAIL": ("明细", "明细表"),
    "TREND": ("趋势", "趋势图", "曲线"),
    "SUMMARY": ("总览", "概览"),
    "QUALITY": ("数据质量",),
    "ANALYSIS": ("分析说明",),
}


def _region_aliases(region: dict[str, Any]) -> list[str]:
    component = region.get("component") if isinstance(region.get("component"), dict) else {}
    metric = component.get("metric") if isinstance(component.get("metric"), dict) else {}
    aliases = [
        region.get("id"),
        region.get("name"),
        region.get("role"),
        component.get("title"),
        metric.get("label"),
    ]
    role = str(region.get("role") or "").strip().upper()
    aliases.extend(ROLE_ALIASES.get(role, ()))
    if region.get("id") == "region_total_energy":
        aliases.extend(["总耗电", "昨日总耗电", "总用电", "昨日总用电量"])
    if region.get("id") == "region_device_ranking":
        aliases.extend(["设备排名", "设备耗电排名", "耗电排名"])
    if region.get("id") == "region_hourly_trend":
        aliases.extend(["趋势", "小时趋势", "趋势图"])
    return [str(alias) for alias in aliases if alias]


def _definition_revision(definition: ReportDefinitionDraft) -> int:
    return int((definition.metadata or {}).get("revision") or 0)

def _bump_revision(definition: ReportDefinitionDraft) -> None:
    definition.metadata = dict(definition.metadata or {})
    definition.metadata["revision"] = int(definition.metadata.get("revision") or 0) + 1


def _current_combination(definition: ReportDefinitionDraft) -> dict[str, Any]:
    """这份定义当前**生效**的组合（契约表 §2.1，固定 9 键）。

    读 #64 的规范实现（``metadata.effectiveCombination``），再把**当前
    来源模式**对齐：组合里记的是它被写下时的快照，而渲染真正依据的是
    定义上的 ``metadata.sourceMode``。两者不一致 = 来源换了
    （模拟示例 ⇄ 用户 JSON），接受候选时必须拒。

    **只读**：编辑链路不改写这个键 —— 它由定义构建路径统一写入，
    这里只拿来比对。
    """
    combination = combination_of_definition(definition.model_dump(mode="json"))
    live_mode = source_mode_of_definition(definition.model_dump(mode="json")).mode
    if combination.get("sourceMode") != live_mode:
        combination = {**combination, "sourceMode": live_mode}
    return combination


def _default_dataset_source_ref(definition: ReportDefinitionDraft) -> str | None:
    datasets = definition.datasets or {}
    for ds in datasets.values():
        if isinstance(ds, dict) and ds.get("sourceRef"):
            return ds["sourceRef"]
    return None


def _region_dataset_source_ref(
    region: dict[str, Any], definition: ReportDefinitionDraft
) -> str | None:
    data = region.get("data") if isinstance(region.get("data"), dict) else {}
    inputs = data.get("inputs") if isinstance(data.get("inputs"), dict) else {}
    for inp in inputs.values():
        if isinstance(inp, dict) and inp.get("datasetRef"):
            dataset_ref = inp["datasetRef"]
            dataset = (definition.datasets or {}).get(dataset_ref)
            if isinstance(dataset, dict) and dataset.get("sourceRef"):
                return dataset["sourceRef"]
    return _default_dataset_source_ref(definition)


def _required_source_fields(region: dict[str, Any], metrics: dict[str, dict[str, Any]]) -> set[str]:
    """Find source fields consumed before aggregation or resampling."""
    required: set[str] = set()
    data = region.get("data") or {}
    pipeline = data.get("pipeline") or {}
    for node in pipeline.get("nodes") or []:
        if node.get("type") == "RESAMPLE":
            required.add(node.get("timeFieldRef", ""))
        if node.get("type") == "GROUP_AGGREGATE":
            required.update(node.get("groupBy") or [])
        for measure in node.get("measures") or []:
            required.add(measure.get("fieldRef", ""))
        if node.get("type") == "METRIC":
            metric = metrics.get(node.get("metricRef", ""))
            if metric:
                required.add(metric["fieldRef"])
    return required - {""}


def _region_has_grain(region: dict[str, Any]) -> bool:
    """该区块是否按 compilation 粒度编译（有 grain 上下文或 RESAMPLE 节点）。"""
    context = region.get("context") if isinstance(region.get("context"), dict) else {}
    if isinstance(context.get("grain"), dict):
        return True
    pipeline = ((region.get("data") or {}).get("pipeline") or {})
    return any(
        isinstance(node, dict) and node.get("type") == "RESAMPLE"
        for node in pipeline.get("nodes") or []
    )


def _metrics_compatible(
    region: dict[str, Any],
    metrics: dict[str, dict[str, Any]],
    catalog: dict[str, Any],
) -> bool:
    """目标数据源是否带齐该区块用到的指标字段。"""
    fields = set(catalog.get("fields") or {})
    nodes = ((region.get("data") or {}).get("pipeline") or {}).get("nodes") or []
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") != "METRIC":
            continue
        metric = metrics.get(str(node.get("metricRef")))
        if metric and metric["fieldRef"] not in fields:
            return False
    return True


# ---------- Engine ----------

class TemplateEditEngine:
    """模板编辑引擎：解析 + 应用。"""

    @staticmethod
    def _visible_items(scope: ToolScope | None) -> tuple[dict[str, dict], dict[str, dict]]:
        if scope is None:
            return {}, {}
        filters = {
            "project_id": scope.project_id,
            "subject_ref": scope.subject_ref,
            "subject_id": scope.subject_id,
        }
        catalogs = filter_visible(list_catalogs(), **filters)
        metrics = filter_visible(list_metrics(), **filters)
        return (
            {item["sourceRef"]: item for item in catalogs},
            {item["metricRef"]: item for item in metrics},
        )

    # ===== 对外接口 =====

    def resolve_target_region(
        self,
        *,
        message: str,
        definition: ReportDefinitionDraft,
        selected_region_id: str | None = None,
    ) -> dict[str, Any] | None:
        """定位用户说的那张卡片。

        供意图识别与问题分析使用：**只定位对象，不解析要改什么**。
        卡片名里的时间 / 指标 / 图表类型在这里只用来认出「是哪一张」。
        """
        return self._find_region(
            definition.regions or [], message, selected_region_id
        )

    def plan_source_and_grain_change(
        self,
        *,
        definition: ReportDefinitionDraft,
        region_id: str,
        grain_unit: str,
        scope: ToolScope | None = None,
        source_ref: str | None = None,
    ) -> EditPlan | None:
        """为「换源 + 改粒度」组合挑一个确定性的候选计划。

        当前数据源不支持目标粒度时，从授权范围内找一个既支持该粒度、
        又带齐该区块所需字段的数据源，组成一次原子修改。

        返回 None 表示授权范围内没有可用组合，调用方应据此追问或说明卡点。
        """
        catalogs, metrics = self._visible_items(scope)
        region = next(
            (r for r in (definition.regions or []) if r.get("id") == region_id),
            None,
        )
        if region is None:
            return None

        current_source = _region_dataset_source_ref(region, definition)
        current_catalog = catalogs.get(current_source or "")
        if not current_catalog:
            return None
        if source_ref is None and grain_unit in (current_catalog.get("supportedGrains") or []):
            # 当前源已支持该粒度，不需要组合修改
            return None
        if not _region_has_grain(region):
            # 该区块没有 compilation 粒度可言，组合修改无处落脚
            return None

        required_fields = _required_source_fields(region, metrics)
        candidates = [
            catalog
            for catalog in catalogs.values()
            if grain_unit in (catalog.get("supportedGrains") or [])
            and (source_ref is None or catalog.get("sourceRef") == source_ref)
            and required_fields <= set(catalog.get("fields") or {})
            # 指标口径变化要先说明并确认：指标所需字段必须在目标源里
            and _metrics_compatible(region, metrics, catalog)
        ]
        if not candidates:
            return None
        # 排序保证同样输入得到同样的候选
        candidates.sort(key=lambda item: str(item.get("sourceRef")))
        target_source = candidates[0]
        target_ref = str(target_source.get("sourceRef"))

        steps = [
            EditOperationStep(
                intent=EditIntent.CHANGE_DATA_SOURCE,
                operation=EditOperation(sourceRef=target_ref),
                summary=f"数据源换成 {_source_display_label(catalogs, target_ref)}",
            ),
            EditOperationStep(
                intent=EditIntent.CHANGE_COMPILATION_GRAIN,
                operation=EditOperation(grain={"unit": grain_unit}),
                summary=f"统计粒度改成 {grain_unit}",
            ),
        ]
        target_grains = list(target_source.get("supportedGrains") or [])
        return EditPlan(
            isTemplateEdit=True,
            intent=EditIntent.CHANGE_COMPILATION_GRAIN,
            confidence=0.0,
            target=EditTarget(
                regionId=region_id,
                matchedText=_region_label(region),
                confidence=0.9,
            ),
            # 顶层 operation 指向用户真正要求的那一步（粒度），
            # 并带上目标源实际支持的粒度，供确认菜单直接展示。
            operation=EditOperation(
                grain={"unit": grain_unit},
                sourceRef=target_ref,
                sourceVersion=str(target_source.get("sourceVersion") or ""),
                supported=True,
                supportedGrains=target_grains,
            ),
            operations=steps,
            summary=self._composite_summary(
                region=region,
                current_source=current_source,
                target_source=target_ref,
                grain_unit=grain_unit,
                metrics=metrics,
                target_catalog=target_source,
            ),
        )

    @staticmethod
    def _composite_summary(
        *,
        region: dict[str, Any],
        current_source: str | None,
        target_source: str,
        grain_unit: str,
        metrics: dict[str, dict[str, Any]],
        target_catalog: dict[str, Any],
    ) -> str:
        """组合修改的面向用户摘要：写清楚改什么、来源、粒度和未验证能力。"""
        label = _region_label(region)
        # 目标数据源有目录条目，用它的业务描述；来源数据源这里拿不到目录，
        # 统一说「当前数据源」—— 两边都不摆 sourceRef。
        target_label = _source_display_label({target_source: target_catalog}, target_source)
        current_label = _source_display_label({}, current_source)
        lines = [
            f"将「{label}」的数据源从 {current_label} 换成 {target_label}，"
            f"并把统计粒度改成 {_grain_display_label(grain_unit)}。",
            f"目标数据源支持的粒度：{_grain_display_labels(target_catalog.get('supportedGrains')) or '未登记'}。",
        ]
        # 换源后指标口径可能变化，必须先说明再确认
        for node in (
            ((region.get("data") or {}).get("pipeline") or {}).get("nodes") or []
        ):
            if isinstance(node, dict) and node.get("type") == "METRIC":
                metric = metrics.get(str(node.get("metricRef")))
                if metric:
                    lines.append(
                        f"指标「{metric['label']}」的口径可能随数据源变化，"
                        "接受后仍需重新校验字段映射。"
                    )
                break
        lines.append("当前预览仍为 Mock 数据，尚未真实编译；接受修改后需要重新校验并预览。")
        return "\n".join(lines)

    def parse(
        self,
        *,
        message: str,
        definition: ReportDefinitionDraft | None,
        selected_region_id: str | None,
        scope: ToolScope | None = None,
        change_value: str | None = None,
    ) -> EditPlan:
        """从用户消息解析出 EditPlan。规则优先，匹配不到交给 UNKNOWN。

        ``change_value`` 是**新值片段**：时间窗、粒度、数据源、指标的新值
        只从这里取。调用方可以传自己算好的片段（澄清答案整句就是新值的
        情况）；不传就从消息里按「明确的改动动词」取。片段为空 = 没有新值。
        """
        plan = EditPlan(isTemplateEdit=True, confidence=0.0)
        # 这份计划所基于的**生效组合快照**（契约表 §2.1 / §4.2）：
        # 接受时逐字段比对，对不上就拒绝。
        if definition is not None:
            plan.baseCombination = _current_combination(definition)
        text = (message or "").strip()
        if not text or definition is None:
            plan.intent = EditIntent.UNKNOWN
            plan.needsClarification = True
            plan.clarificationQuestion = (
                "请告诉我要怎么改当前草稿：删掉某张卡片、改标题、改指标、改数据源、改图表，"
                "还是改统计粒度？"
            )
            return plan
        catalogs, metrics = self._visible_items(scope)

        # 新值片段：调用方算好的优先，否则按「明确的改动动词」从消息里取。
        # 片段为空 = 这句话没有给出任何新值（规格第 48 条）。
        if change_value is None:
            change_value = self._extract_change_value(text)
        new_value = (change_value or "").strip()

        # 定位目标 region
        regions = definition.regions or []
        target_region = self._find_region(regions, text, selected_region_id)
        if target_region is not None:
            plan.target = EditTarget(
                regionId=str(target_region.get("id")),
                matchedText=_region_label(target_region),
                confidence=0.9,
            )

        # ===== 1. 改报表名 =====
        report_name = self._extract_report_name(text)
        if report_name:
            plan.intent = EditIntent.RENAME_REPORT
            plan.confidence = 0.95
            plan.target = EditTarget(
                regionId="",
                matchedText=str((definition.metadata or {}).get("name") or "当前报表"),
                confidence=1.0,
            )
            plan.operation = EditOperation(newTitle=report_name)
            return plan

        # ===== 2. 删除 =====
        #
        # 「不要 X，改成 Y」是**替换**，不是删除：一句话里既出现否定词又
        # 出现替换动词时，替换说了算 —— 用户要的是「把 X 换成 Y」，不是
        # 「把整张卡片删掉」。
        #
        # 判据是通用词法（否定词 + 替换动词共现），不认具体指标名。
        # 只有否定词、没有替换动词时，才是删除。
        delete_hit = any(w in text for w in DELETE_WORDS)
        replace_hit = any(w in text for w in REPLACE_CLAUSE_VERBS)
        if delete_hit and not replace_hit:
            plan.intent = EditIntent.DELETE_SECTION
            plan.confidence = 0.9
            if target_region is None:
                plan.needsClarification = True
                plan.clarificationQuestion = "你想删掉哪张卡片？可以直接说：删掉昨日总耗电。"
            return plan

        # ===== 3. 在已有区块里追加指标 =====
        #
        # 「再加 X」「还需要包括 X、Y」是**追加**：保留该区块原有指标，把
        # 新指标加进去；只有明确说「替换 / 只留 / 换成」才走替换。
        #
        # 这一段必须排在「新增卡片」**之前**：
        #   - 句子里有「指标」两个字时，「再加三个指标」会被误当成「把指标
        #     换成三个」（缺陷 2 的根因）；
        #   - 句子里有「加」时，「再加 A、B、C」会被误当成**新增一张卡片**，
        #     三个指标名挤成一个标题（缺陷 2 的另一半）。
        #
        # 追加的目标卡片：句子里点名的那张 > 用户当前选中的那张 >
        # 草稿里**唯一**的那张正文卡片。三者都定不下来才追问。
        if not any(word in text for word in REPLACE_WORDS) and any(
            word in text for word in APPEND_WORDS
        ):
            append_target = target_region or self._sole_body_region(definition)
            if append_target is not None:
                plan.target = EditTarget(
                    regionId=str(append_target.get("id")),
                    matchedText=_region_label(append_target),
                    confidence=0.9,
                )
            append_plan = self._plan_append_metrics(
                text, append_target, metrics, definition,
            )
            if append_plan is not None:
                return append_plan
            if append_target is None:
                # 看得出是要**加**东西，但没说要加到哪张卡片、也没说加哪几个
                # → 在同一个待办里问清楚。**不能**掉到「换指标」那条路上：
                # 「再加 X」是追加，不是替换。
                return EditPlan(
                    isTemplateEdit=True,
                    intent=EditIntent.ADD_METRIC,
                    confidence=0.6,
                    target=EditTarget(regionId="", matchedText="", confidence=0.0),
                    operation=EditOperation(
                        baseDefinitionRevision=_definition_revision(definition),
                    ),
                    needsClarification=True,
                    clarificationQuestion=(
                        "要往哪张卡片上追加指标？"
                        "说出卡片名字和你想加的指标就行（比如「总览再加 A、B」）。"
                    ),
                )

        # ===== 4. 新增卡片 =====
        #
        # 触发条件有两类：
        #   - 带量词的说法（「加一个 X」「新增 X」）；
        #   - **句首**的「加 / 再加」后面确实是一个指标（「加 PUE」）。
        #     只认句首：句中的「加个两度」不是新增指标。
        add_hit = any(w in text for w in ADD_WORDS)
        if not add_hit:
            add_hit = self._leading_add_is_metric(text, metrics)
        if add_hit:
            plan.intent = EditIntent.ADD_SECTION
            plan.confidence = 0.85
            # 「用户说的指标」是已登记指标还是目录外候选，**一处判定**：
            # 别名命中 ≠ 识别成功（见 ``_resolve_metric_target``）。
            metric_ref, candidate = self._resolve_metric_target(
                text, metrics, strip_add_prefix=True,
            )
            source_ref = match_source_by_alias(text, set(catalogs)) or _default_dataset_source_ref(definition)
            if source_ref not in catalogs:
                source_ref = None
            chart_kind, component_type, type_word = self._parse_component_type(text)
            # 选型（#65）：用户自己说了展示形式就沿用（**不被静默改写**），
            # 没说就按规则库定 —— 理由来自**真实命中**。
            selection = self._select_component_type(
                text, chart_kind, component_type, type_word,
            )
            chart_kind = selection["chartKind"]
            component_type = selection["componentType"]
            # 目录外指标（#65）：解析不出真实指标标识时**不再拒绝**，
            # 转成可澄清或带显式假设的模拟候选（上面已判定过）。
            section_title = (
                candidate.label if candidate is not None
                else self._extract_add_title(text)
                or (metrics[metric_ref]["label"] if metric_ref else "")
            )
            plan.operation = EditOperation(
                sectionTitle=section_title,
                metricRef=metric_ref,
                metricVersion=metrics[metric_ref]["metricVersion"] if metric_ref else None,
                metricIntent=candidate.intent if candidate is not None else None,
                metricCandidate=candidate.as_dict() if candidate is not None else None,
                sourceRef=source_ref,
                sourceVersion=catalogs[source_ref].get("sourceVersion") if source_ref else None,
                componentType=component_type or "TABLE",
                chartKind=chart_kind or "",
                baseDefinitionRevision=_definition_revision(definition),
                componentSelection=selection["evidence"],
            )
            missing: list[str] = []
            if not plan.operation.sectionTitle:
                missing.append("卡片标题")
            if not plan.operation.metricRef and not plan.operation.metricIntent:
                missing.append("指标")
            # 目录外指标这一块**不绑数据源**（它没有真实口径），不因
            # 「数据源没找到」把它挡回去。
            if not plan.operation.sourceRef and candidate is None:
                missing.append("数据源")
            if missing:
                plan.needsClarification = True
                plan.clarificationQuestion = (
                    "新增卡片还缺：" + "、".join(missing) + "。你想看什么指标、用哪个数据源？"
                )
            elif candidate is not None and candidate.is_ambiguous:
                # 含义不清（电脑耗电率？耗电量？占比？功率？）→ 先澄清口径，
                # **不替他选一个**当结论给。
                plan.needsClarification = True
                plan.clarificationQuestion = candidate.clarification_question
            return plan

        # ===== 5. 改数据源 =====
        if ("数据源" in text and ("换" in text or "改" in text)) or re.search(r"换成.+(明细|汇总|数据)", text):
            source_ref = match_source_by_alias(new_value, set(catalogs))
            plan.intent = EditIntent.CHANGE_DATA_SOURCE
            plan.confidence = 0.85
            plan.operation = EditOperation(
                sourceRef=source_ref,
                sourceVersion=catalogs[source_ref].get("sourceVersion") if source_ref else None,
                fieldMappingRequired=True,
                reason="用户要求切换该 section 的数据源",
            )
            if target_region is None:
                plan.needsClarification = True
                plan.clarificationQuestion = "你想换哪张卡片的数据源？"
                return plan
            if not source_ref:
                plan.needsClarification = True
                plan.clarificationQuestion = (
                    "你想换成哪个数据源？例如 EAS 能耗明细、能耗日汇总，或者机房能耗数据。"
                )
            return plan

        # ===== 5. 改展示形式（CHANGE_VISUALIZATION 优先于 RENAME）=====
        chart_kind, component_type, _type_word = self._parse_component_type(text)
        if chart_kind or component_type:
            plan.intent = EditIntent.CHANGE_VISUALIZATION
            plan.confidence = 0.9
            plan.operation = EditOperation(
                componentType=component_type or "CHART",
                chartKind=chart_kind or "",
            )
            if target_region is None:
                plan.needsClarification = True
                plan.clarificationQuestion = "你想把哪张卡片改成这个展示形式？"
            else:
                shape = ROLE_DATA_SHAPE.get(str(target_region.get("role")), "CATEGORY_RANKING")
                options = DISPLAY_OPTIONS_BY_SHAPE.get(shape, {}).get("options", [])
                if not any(
                    option["componentType"] == component_type
                    and (component_type != "CHART" or option["chartKind"] == chart_kind)
                    for option in options
                ):
                    plan.needsClarification = True
                    available = "、".join(option["label"] for option in options)
                    plan.clarificationQuestion = (
                        f"「{_region_label(target_region)}」的数据形态不适合这种展示方式。"
                        f"当前可选：{available or '暂无'}。"
                    )
            return plan

        # 查看可展示方式
        if any(word in text for word in SUGGEST_VISUALIZATION_WORDS):
            plan.intent = EditIntent.SUGGEST_VISUALIZATION
            plan.confidence = 0.85
            if target_region is None:
                plan.needsClarification = True
                plan.clarificationQuestion = "你想看哪张卡片的展示方式？"
                return plan
            shape = ROLE_DATA_SHAPE.get(str(target_region.get("role")), "CATEGORY_RANKING")
            option_bundle = DISPLAY_OPTIONS_BY_SHAPE.get(shape, {"options": [], "disabled": []})
            plan.operation = EditOperation(
                dataShape=shape,
                displayOptions=option_bundle["options"],
                disabledOptions=option_bundle["disabled"],
            )
            return plan

        # ===== 6. 改时间范围 =====
        # 时间范围必须先于粒度判断，否则“最近 7 天”会被误识别为 DAY 粒度。
        #
        # 新值只来自**明确的改动动词片段**（规格第 48 条）：卡片名里的
        # 「昨日」「最近 7 天」只用来定位对象，不是要写进去的时间窗。
        # 没有改动动词就没有新值 —— 这里拿到的是空串，不会命中任何时间表达。
        time_window, label = self._parse_time_range(new_value)
        if time_window:
            plan.intent = EditIntent.CHANGE_TIME_RANGE
            plan.confidence = 0.88
            plan.operation = EditOperation(
                timeWindow=time_window,
                timeWindowLabel=label,
            )
            if target_region is None:
                plan.needsClarification = True
                plan.clarificationQuestion = f"你想把哪张卡片的时间范围改成「{label}」？"
            return plan

        # ===== 7. 改 compilation 粒度 =====
        #
        # 粒度新值与时间窗**同口径**（规格第 48 条 / 契约表 §7）：
        # 只认**明确点名的写法**（按小时 / 小时级 / 按天粒度）。
        # 卡片名里的「小时」「天」（「小时趋势」）只用来定位对象，不是新值。
        #
        # 新值片段的来源：
        #   * 有改动动词片段（``new_value``）→ 只从片段里取明确点名的写法；
        #   * 没有改动动词 → 整句里**去掉卡片名**之后取明确点名的写法。
        #     （不去掉卡片名，「把小时趋势的标题改一下」会被当成改粒度。）
        #
        # 澄清答案走的是**第一档**：调用方把整句塞进 ``change_value``，
        # 整句就是用户给的新值（「按小时」），这里按同一口径取明确点名。
        topic_text = self._text_without_region_label(text, target_region)
        grain_source = new_value if new_value else topic_text
        grain_unit = self._parse_grain(grain_source, explicit_only=True)
        # 「能不能按哪些粒度」这类问句没有新值，但仍然是粒度话题：
        # 话题词只认「粒度 / compile」，不拿取值词当话题词 ——
        # 「小时趋势」里的「小时」既不是新值，也不是话题。
        if grain_unit or any(w in topic_text for w in GRAIN_WORDS):
            plan.intent = EditIntent.CHANGE_COMPILATION_GRAIN
            plan.confidence = 0.85
            if target_region is None:
                plan.needsClarification = True
                plan.clarificationQuestion = (
                    "你想改哪张卡片的统计粒度？"
                )
                return plan
            source_ref = _region_dataset_source_ref(target_region, definition)
            grains = list(catalogs.get(source_ref, {}).get("supportedGrains", []))
            if grain_unit:
                is_supported = grain_unit in grains if grains else False
                plan.operation = EditOperation(
                    grain={"unit": grain_unit},
                    sourceRef=source_ref,
                    supported=is_supported,
                    supportedGrains=grains,
                )
                if not is_supported:
                    plan.needsClarification = True
                    plan.clarificationQuestion = (
                        f"当前数据源不支持 {grain_unit} 粒度。可用粒度：{'、'.join(grains) or '未登记'}。"
                        "你想改用支持该粒度的数据源，还是改成可用粒度？"
                    )
                return plan
            # 用户只是在问“可以按哪些粒度”
            plan.operation = EditOperation(
                sourceRef=source_ref,
                supportedGrains=grains,
            )
            plan.needsClarification = True
            plan.clarificationQuestion = (
                f"当前数据源支持的粒度：{'、'.join(grains) or '未登记'}。你想改成哪一个？"
            )
            return plan

        # ===== 8. 改指标 =====
        #
        # 触发条件只有两种：
        #   - 句子里点名了「指标」；
        #   - **否定 + 替换**（「不要 X，改成 Y」）—— 替换后的新值能解析成
        #     一个指标时，这句话就是在换指标。
        #
        # 这里**不认具体指标名** —— 不写「耗电/电费/pue/用电」这类清单。
        # 新值能不能解析成一个指标，由解析器说了算；解析不出来就不在这里
        # 硬判，落到后面的分支去。
        #
        # 目录外指标（#65）：换成一个目录里没有的指标同样**不再以
        # 「未登记」拒绝** —— 转成可澄清或带显式假设的模拟候选。
        # 与新增分支**同一处判定**：别名命中 ≠ 识别成功。
        metric_ref, candidate = None, None
        if "指标" in text or (delete_hit and replace_hit):
            metric_ref, candidate = self._resolve_metric_target(
                new_value, metrics, strip_add_prefix=False,
            )
        if "指标" in text or metric_ref or candidate:
            plan.intent = EditIntent.CHANGE_METRIC
            plan.confidence = 0.8
            plan.operation = EditOperation(
                metricRef=metric_ref,
                metricVersion=metrics[metric_ref].get("metricVersion") if metric_ref else None,
                metricIntent=candidate.intent if candidate is not None else None,
                metricCandidate=candidate.as_dict() if candidate is not None else None,
                baseDefinitionRevision=_definition_revision(definition),
            )
            if target_region is None:
                plan.needsClarification = True
                plan.clarificationQuestion = "你想换哪张卡片的指标？"
                return plan
            if not metric_ref and candidate is None:
                plan.needsClarification = True
                plan.clarificationQuestion = (
                    "你想换成哪个指标？例如总耗电、设备耗电、能耗成本、PUE。"
                )
            elif candidate is not None and candidate.is_ambiguous:
                plan.needsClarification = True
                plan.clarificationQuestion = candidate.clarification_question
            return plan

        # ===== 9. TopN / 排序 =====
        topn_match = re.search(r"(?:前|取|只看)\s*(\d+)\s*(?:名|条|个)?", text)
        if topn_match:
            plan.intent = EditIntent.CHANGE_TOP_N
            plan.confidence = 0.82
            plan.operation = EditOperation(topN=int(topn_match.group(1)))
            if target_region is None:
                plan.needsClarification = True
                plan.clarificationQuestion = "你想在哪张卡片看前 N 名？"
            return plan

        if "降序" in text or "升序" in text or "按" in text and "排" in text:
            plan.intent = EditIntent.CHANGE_SORT
            plan.confidence = 0.7
            direction = "ASC" if "升序" in text else "DESC"
            plan.operation = EditOperation(sortDirection=direction)
            if target_region is None:
                plan.needsClarification = True
                plan.clarificationQuestion = "你想改哪张卡片的排序？"
            return plan

        # ===== 10. 改标题（放在最后，必须明确触发词）=====
        new_title = self._extract_rename_title(text)
        if new_title:
            plan.intent = EditIntent.RENAME_SECTION
            plan.confidence = 0.85
            plan.operation = EditOperation(newTitle=new_title)
            if target_region is None:
                plan.needsClarification = True
                plan.clarificationQuestion = f"你想把哪张卡片的标题改成「{new_title}」？"
            return plan

        # ===== 11. 兜底 =====
        plan.intent = EditIntent.UNKNOWN
        plan.confidence = 0.3
        plan.needsClarification = True
        if target_region is not None:
            plan.clarificationQuestion = (
                f"你想把「{_region_label(target_region)}」删除、改标题、改指标、"
                "改数据源、改展示形式、改统计粒度，还是改时间范围？"
            )
        else:
            plan.clarificationQuestion = (
                "请告诉我要怎么改当前草稿：删掉某张卡片、改标题、改指标、改数据源、改图表，"
                "还是改统计粒度？"
            )
        return plan

    def apply(
        self,
        *,
        plan: EditPlan,
        definition: ReportDefinitionDraft,
        scope: ToolScope | None = None,
    ) -> EditResult:
        """根据 EditPlan 修改定义草稿；returns EditResult（含新草稿和 ChangeSet）。"""
        before = definition.model_copy(deep=True)
        base_revision = _definition_revision(definition)
        catalogs, metrics = self._visible_items(scope)

        # 需要追问 / 无法执行 → 返回 clarification
        if plan.needsClarification or plan.intent == EditIntent.UNKNOWN:
            return EditResult(
                plan=plan,
                draftBeforeEdit=before,
                draftAfterEdit=before,
                needsClarification=True,
                clarificationQuestion=plan.clarificationQuestion,
                editingTargetRegionId=plan.target.regionId or None,
                debug={"intent": plan.intent.value, "reason": "clarification"},
            )

        # 版本校验（工单 #65，契约表 §4.2）：候选是基于**旧版本草稿**做的，
        # 或者它依据的生效组合已经变了 —— 服务端直接拒绝，草稿一个字节
        # 都不动。安全边界在服务端，不靠禁用按钮。
        stale = self._stale_reason(plan, definition)
        if stale is not None:
            message, reason = stale
            return EditResult(
                plan=plan,
                draftBeforeEdit=before,
                draftAfterEdit=before,
                needsClarification=True,
                clarificationQuestion=message,
                editingTargetRegionId=plan.target.regionId or None,
                debug={"intent": plan.intent.value, "reason": reason},
            )

        # 组合修改：多个操作一次确认、原子应用
        if plan.operations:
            return self._apply_composite(
                plan=plan,
                definition=definition,
                before=before,
                base_revision=base_revision,
                catalogs=catalogs,
                metrics=metrics,
            )

        if plan.intent == EditIntent.SUGGEST_VISUALIZATION:
            # 只是回建议，不改草稿
            md = self._markdown_display_options(plan)
            return EditResult(
                plan=plan,
                draftBeforeEdit=before,
                draftAfterEdit=before,
                reply=f"「{plan.target.matchedText}」支持以下展示方式，请在候选中选择。",
                markdownPreview=md,
                editingTargetRegionId=plan.target.regionId or None,
                debug={"intent": plan.intent.value},
            )

        updated = definition.model_copy(deep=True)
        outcome = self._apply_one_intent(
            plan=plan,
            draft=updated,
            base_revision=base_revision,
            catalogs=catalogs,
            metrics=metrics,
        )
        if outcome.is_failed:
            if outcome.mark_plan_clarification:
                plan.needsClarification = True
                plan.clarificationQuestion = outcome.failure
            return EditResult(
                plan=plan,
                draftBeforeEdit=before,
                draftAfterEdit=before,
                needsClarification=True,
                clarificationQuestion=outcome.failure,
                markdownPreview=outcome.markdown,
                editingTargetRegionId=plan.target.regionId or None,
                debug={"intent": plan.intent.value, "reason": outcome.reason},
            )

        _bump_revision(updated)
        # 修改形成了新一版 → 这一版**沿用同一份生效组合**（契约表 §2.1）。
        #
        # 生效组合**不重建、不改写**：它由定义构建路径（#64）统一写入，
        # 里面记着用户 JSON 的输入 ID 与版本、字段映射、处理规则。
        # 编辑链路只**读**它（接受时逐字段比对，见 ``_stale_reason``），
        # 原样带过去 —— 重建会把用户 JSON 的输入绑定弄丢，渲染就会
        # 因为「没有绑定输入」而失败。
        return EditResult(
            plan=plan,
            draftBeforeEdit=before,
            draftAfterEdit=updated,
            changeSet=outcome.change_set,
            reply=outcome.reply,
            markdownPreview=outcome.markdown,
            editingTargetRegionId=plan.target.regionId or None,
            debug={"intent": plan.intent.value},
        )

    @staticmethod
    def _stale_reason(
        plan: EditPlan, definition: ReportDefinitionDraft,
    ) -> tuple[str, str] | None:
        """这份计划是不是**过期 / 冲突**了（契约表 §4.2）。

        两条判据：

        1. 候选所基于的**草稿版本**与当前生效定义不一致 → 过期；
        2. 候选所依据的**生效组合**与当前不一致 → 冲突。

        返回 ``(面向用户的话, 失败分类)``；没问题返回 ``None``。
        """
        base_revision = plan.operation.baseDefinitionRevision
        if (
            base_revision is not None
            and int(base_revision) != _definition_revision(definition)
        ):
            return (
                "这份修改是基于旧版本的草稿提的，草稿已经变了；"
                "请按当前草稿重新说明一次要改什么。",
                "stale_edit_plan",
            )
        base_combination = plan.baseCombination
        if base_combination is None:
            return None
        if not combination_matches(
            base_combination, _current_combination(definition),
        ):
            return (
                "这份修改所依据的生效组合已经变了（来源或输入换了），"
                "请按当前状态重新提出修改。",
                "combination_conflict",
            )
        return None

    def _apply_one_intent(
        self,
        *,
        plan: EditPlan,
        draft: ReportDefinitionDraft,
        base_revision: int,
        catalogs: dict[str, dict],
        metrics: dict[str, dict],
    ) -> _IntentOutcome:
        """在一个草稿副本上校验并应用单个意图。

        ``draft`` 由调用方持有：校验通过才会写入，失败时调用方丢弃该副本，
        因此组合修改可以逐步执行而不留下半成品。
        """
        source_ref = plan.operation.sourceRef
        metric_ref = plan.operation.metricRef
        # 来源模式（模拟示例 / 用户 JSON）。**只有模拟示例模式**才放松
        # 「真实字段准入」—— 用户 JSON 模式下的校验一律保留。
        mode = source_mode_of_definition(draft.model_dump(mode="json"))
        virtual_metric = bool(str(plan.operation.metricIntent or "").strip())
        if source_ref and source_ref not in catalogs:
            return _IntentOutcome.failed(
                "当前范围无权使用该数据源。", reason="incompatible_fields",
            )
        if metric_ref and metric_ref not in metrics:
            return _IntentOutcome.failed(
                "当前范围无权使用该指标。", reason="incompatible_fields",
            )
        if plan.intent in {EditIntent.CHANGE_DATA_SOURCE, EditIntent.ADD_SECTION} and not source_ref:
            # 目录外指标**没有**真实口径，不绑数据源 —— 不因「没找到数据源」
            # 把用户的增改请求挡回去（模拟示例模式下）。
            if not (virtual_metric and mode.mode != "user_json"):
                return _IntentOutcome.failed(
                    "请先选择授权范围内的数据源。", reason="incompatible_fields",
                )
        if plan.intent in {EditIntent.CHANGE_METRIC, EditIntent.ADD_SECTION} and not metric_ref:
            # 目录外指标（#65）：带着**候选**来的不再以「未登记」拒绝 ——
            # 它走通用模拟能力出数，并标「能力待接通 / 口径待确认」。
            if not str(plan.operation.metricIntent or "").strip():
                return _IntentOutcome.failed(
                    "请先选择授权范围内的指标。", reason="incompatible_fields",
                )
        if source_ref:
            plan.operation.sourceVersion = catalogs[source_ref].get("sourceVersion")
        if metric_ref:
            plan.operation.metricVersion = metrics[metric_ref].get("metricVersion")

        target_region = next(
            (r for r in draft.regions if r.get("id") == plan.target.regionId),
            None,
        )
        if plan.intent in {EditIntent.CHANGE_COMPILATION_GRAIN, EditIntent.CHANGE_METRIC} and target_region:
            current_source = _region_dataset_source_ref(target_region, draft)
            if current_source not in catalogs:
                return _IntentOutcome.failed(
                    "当前范围无权使用该区块的数据源。", reason="incompatible_fields",
                )
        if plan.intent == EditIntent.CHANGE_COMPILATION_GRAIN and target_region:
            current_source = _region_dataset_source_ref(target_region, draft)
            grains = list(catalogs[current_source].get("supportedGrains", []))
            plan.operation.sourceRef = current_source
            plan.operation.supportedGrains = grains
            plan.operation.supported = (plan.operation.grain or {}).get("unit") in grains
        if plan.intent == EditIntent.CHANGE_VISUALIZATION and target_region:
            shape = ROLE_DATA_SHAPE.get(str(target_region.get("role")), "CATEGORY_RANKING")
            allowed = DISPLAY_OPTIONS_BY_SHAPE.get(shape, {}).get("options", [])
            component_type = plan.operation.componentType
            chart_kind = plan.operation.chartKind or ""
            if not any(
                item["componentType"] == component_type
                and (component_type != "CHART" or item["chartKind"] == chart_kind)
                for item in allowed
            ):
                return _IntentOutcome.failed(
                    "该区块的数据形态不支持此展示方式。", reason="incompatible_fields",
                )
        if plan.intent == EditIntent.CHANGE_COMPILATION_GRAIN and plan.operation.supported is False:
            return _IntentOutcome.failed(
                plan.clarificationQuestion
                or "当前数据源不支持该粒度，请选择支持的粒度或切换数据源。",
                reason="grain_unsupported",
                markdown=self._markdown_grain_unsupported(plan),
            )

        if plan.intent == EditIntent.CHANGE_DATA_SOURCE and target_region is not None:
            nodes = ((target_region.get("data") or {}).get("pipeline") or {}).get("nodes") or []
            if any(
                node.get("type") == "METRIC" and node.get("metricRef") not in metrics
                for node in nodes if isinstance(node, dict)
            ):
                return _IntentOutcome.failed(
                    "当前范围无法校验该区块的指标。", reason="incompatible_fields",
                )
            source = catalogs.get(plan.operation.sourceRef or "")
            fields = set((source or {}).get("fields") or {})
            missing = sorted(_required_source_fields(target_region, metrics) - fields)
            if missing:
                return _IntentOutcome.failed(
                    f"目标数据源缺少当前卡片所需字段：{'、'.join(missing)}。"
                    "请换用兼容的数据源。",
                    reason="incompatible_fields",
                )
        if plan.intent in {EditIntent.CHANGE_METRIC, EditIntent.ADD_SECTION}:
            metric = metrics.get(plan.operation.metricRef or "")
            source_ref = (
                plan.operation.sourceRef if plan.intent == EditIntent.ADD_SECTION
                else _region_dataset_source_ref(target_region, draft)
                if target_region is not None else None
            )
            source = catalogs.get(source_ref or "")
            if metric and source and metric["fieldRef"] not in (source.get("fields") or {}):
                if mode.mode == "user_json":
                    # 真实数据模式：字段准入**保留**。
                    # 面向用户**不点字段名**（那是内部标识）：只说清楚
                    # 「这个指标现在取不到数，换个数据源」。
                    return _IntentOutcome.failed(
                        f"当前数据源取不到指标「{metric['label']}」的数据，"
                        "请先选择兼容的数据源。",
                        reason="incompatible_fields",
                    )
                # 模拟示例模式：真实字段准入**让位** —— 不能因为「数据源没
                # 这个字段」拒绝用户的增改请求。这一项降级成**虚拟指标**：
                # 不伪造 metricRef，按模拟示例出数并标「能力待接通」。
                self._downgrade_to_virtual_metric(plan, metric)

        change_set: ChangeSet | None = None
        reply = ""
        markdown = ""

        if plan.intent == EditIntent.RENAME_REPORT:
            change_set, reply = self._apply_rename_report(draft, plan, base_revision)
        elif plan.intent == EditIntent.DELETE_SECTION:
            change_set, reply = self._apply_delete(draft, plan, base_revision)
        elif plan.intent == EditIntent.RENAME_SECTION:
            change_set, reply = self._apply_rename(draft, plan, base_revision)
        elif plan.intent == EditIntent.ADD_SECTION:
            change_set, reply = self._apply_add(draft, plan, base_revision, catalogs, metrics)
        elif plan.intent == EditIntent.ADD_METRIC:
            change_set, reply = self._apply_add_metric(
                draft, plan, base_revision, metrics,
            )
        elif plan.intent == EditIntent.CHANGE_DATA_SOURCE:
            change_set, reply = self._apply_change_source(draft, plan, base_revision, catalogs)
        elif plan.intent == EditIntent.CHANGE_VISUALIZATION:
            change_set, reply = self._apply_change_visualization(draft, plan, base_revision)
        elif plan.intent == EditIntent.CHANGE_COMPILATION_GRAIN:
            change_set, reply, markdown = self._apply_change_grain(draft, plan, base_revision)
        elif plan.intent == EditIntent.CHANGE_TIME_RANGE:
            change_set, reply = self._apply_change_time_range(draft, plan, base_revision)
        elif plan.intent == EditIntent.CHANGE_METRIC:
            change_set, reply = self._apply_change_metric(draft, plan, base_revision, metrics)
        elif plan.intent == EditIntent.CHANGE_TOP_N:
            change_set, reply = self._apply_change_top_n(draft, plan, base_revision)
        elif plan.intent == EditIntent.CHANGE_SORT:
            change_set, reply = self._apply_change_sort(draft, plan, base_revision)
        else:
            return _IntentOutcome.failed(
                "该编辑意图尚未实现，请换一种表达。", reason="not_implemented",
            )

        if change_set is None:
            # 没找到目标 region 等情况
            return _IntentOutcome.failed(
                reply or "没有找到要修改的卡片，请再明确一下。", reason="no_change",
            )

        return _IntentOutcome(
            change_set=change_set,
            reply=reply,
            markdown=markdown,
        )

    def _apply_composite(
        self,
        *,
        plan: EditPlan,
        definition: ReportDefinitionDraft,
        before: ReportDefinitionDraft,
        base_revision: int,
        catalogs: dict[str, dict],
        metrics: dict[str, dict],
    ) -> EditResult:
        """应用一次组合修改：换源 + 改粒度等多步，全部成功才提交。

        关键点：
        1. 每一步都跑在**同一份临时草稿**上，后续步骤看到的是前一步的结果
           （换源后粒度支持情况会跟着变），所以不能各自独立校验；
        2. 任一步失败就整体放弃，返回的原草稿和 revision 都不变，
           不留下“只换了一半”的草稿；
        3. 全部成功才写出一份合并的 ChangeSet，revision 只 +1。
        """
        steps = plan.operations
        region_id = plan.target.regionId
        if not region_id:
            return self._composite_failed(
                plan, before, "组合修改必须指明要改哪张卡片。", reason_code="no_target",
            )

        # 组合修改只允许作用在同一个 region 上，避免跨区块的隐式耦合
        for step in steps:
            if step.intent not in COMPOSABLE_INTENTS:
                return self._composite_failed(
                    plan, before,
                    f"「{step.intent.value}」暂不支持与其它修改组合执行。",
                    reason_code="unsupported_composite_intent",
                )
            if step.operation.sourceRef and step.operation.sourceRef not in catalogs:
                return self._composite_failed(
                    plan, before, "当前范围无权使用该数据源。",
                    reason_code="incompatible_fields",
                )
            if step.operation.metricRef and step.operation.metricRef not in metrics:
                return self._composite_failed(
                    plan, before, "当前范围无权使用该指标。",
                    reason_code="incompatible_fields",
                )

        # 临时草稿：所有步骤共用，成功才提交
        working = definition.model_copy(deep=True)
        changed: list[ChangeSetEntry] = []
        impacts: list[str] = []
        replies: list[str] = []
        markdowns: list[str] = []

        for index, step in enumerate(self._ordered_steps(steps)):
            step_plan = EditPlan(
                isTemplateEdit=True,
                intent=step.intent,
                confidence=plan.confidence,
                target=plan.target,
                operation=step.operation.model_copy(deep=True),
            )
            outcome = self._apply_one_intent(
                plan=step_plan,
                draft=working,
                base_revision=base_revision,
                catalogs=catalogs,
                metrics=metrics,
            )
            if outcome.is_failed:
                # 整组放弃：原草稿不动
                if step.intent == EditIntent.CHANGE_COMPILATION_GRAIN:
                    # 把该步查到的源与可用粒度带回顶层 operation，
                    # 现有按 supportedGrains 出选项的下拉框才能继续用。
                    plan.operation.sourceRef = step_plan.operation.sourceRef
                    plan.operation.supportedGrains = step_plan.operation.supportedGrains
                    plan.operation.supported = step_plan.operation.supported
                return self._composite_failed(
                    plan, before, outcome.failure, reason_code=outcome.reason,
                    step_index=index, markdown=outcome.markdown,
                )
            assert outcome.change_set is not None
            changed.extend(outcome.change_set.changed)
            impacts.extend(outcome.change_set.impact)
            if outcome.reply:
                replies.append(outcome.reply)
            if outcome.markdown:
                markdowns.append(outcome.markdown)

        _bump_revision(working)
        merged = ChangeSet(
            baseDefinitionRevision=base_revision,
            changed=changed,
            unchanged=[
                ChangeSetUnchanged(regionId=str(r.get("id")), summary="未修改")
                for r in working.regions
                if r.get("id") != region_id
            ],
            impact=impacts,
        )
        reply = " ".join(replies)
        return EditResult(
            plan=plan,
            draftBeforeEdit=before,
            draftAfterEdit=working,
            changeSet=merged,
            reply=reply,
            markdownPreview="\n\n".join(markdowns),
            editingTargetRegionId=region_id or None,
            debug={
                "intent": plan.intent.value,
                "reason": "composite_applied",
                "compositeSteps": [step.intent.value for step in steps],
            },
        )

    @staticmethod
    def _ordered_steps(steps: list[EditOperationStep]) -> list[EditOperationStep]:
        """组合修改的固定执行顺序：先换源，再改其它。

        粒度是否可用取决于数据源登记的 supportedGrains，所以换源必须排在
        改粒度前面，否则后一步会拿旧数据源的能力做判断。
        同一类步骤之间保持调用方给定的顺序。
        """
        order = {
            EditIntent.CHANGE_DATA_SOURCE: 0,
            EditIntent.CHANGE_METRIC: 1,
            EditIntent.CHANGE_COMPILATION_GRAIN: 2,
        }
        return sorted(
            steps,
            key=lambda step: order.get(step.intent, 3),
        )

    @staticmethod
    def _composite_failed(
        plan: EditPlan,
        before: ReportDefinitionDraft,
        reason: str,
        *,
        reason_code: str,
        step_index: int | None = None,
        markdown: str = "",
    ) -> EditResult:
        """组合修改整体失败：不写草稿、不写 ChangeSet。"""
        plan.needsClarification = True
        plan.clarificationQuestion = reason
        debug: dict[str, Any] = {"intent": plan.intent.value, "reason": reason_code}
        if step_index is not None:
            debug["failedStepIndex"] = step_index
        return EditResult(
            plan=plan,
            draftBeforeEdit=before,
            draftAfterEdit=before,
            needsClarification=True,
            clarificationQuestion=reason,
            markdownPreview=markdown,
            editingTargetRegionId=plan.target.regionId or None,
            debug=debug,
        )

    # ===== 解析辅助 =====

    @staticmethod
    def _find_region(
        regions: list[dict[str, Any]],
        text: str,
        selected_region_id: str | None,
    ) -> dict[str, Any] | None:
        normalized = _normalize_text(text)
        best: tuple[int, dict[str, Any]] | None = None
        for region in regions:
            aliases = _region_aliases(region)
            score = 0
            for alias in aliases:
                normalized_alias = _normalize_text(alias)
                if not normalized_alias:
                    continue
                if normalized_alias in normalized:
                    score = max(score, len(normalized_alias) + 20)
                elif normalized in normalized_alias and len(normalized) >= 2:
                    score = max(score, len(normalized))
            if score and (best is None or score > best[0]):
                best = (score, region)
        if best:
            return best[1]
        if selected_region_id:
            return next(
                (region for region in regions if region.get("id") == selected_region_id),
                None,
            )
        return None

    @staticmethod
    def _sole_body_region(
        definition: ReportDefinitionDraft,
    ) -> dict[str, Any] | None:
        """草稿里**只有一张**正文卡片时，返回它。

        「再加 X」没点名是哪张卡片 —— 草稿里就一张时，那就是它（没有歧义，
        不用追问）；有两张以上时返回 ``None``，由上层问清楚。

        标题区（``role == "REPORT_TITLE"``）不算正文卡片：它是报表名，
        不是能加指标的地方。
        """
        regions = [
            region for region in (definition.regions or [])
            if isinstance(region, dict)
            and str(region.get("role") or "").upper() != "REPORT_TITLE"
        ]
        return regions[0] if len(regions) == 1 else None

    @staticmethod
    def _parse_component_type(text: str) -> tuple[str, str, str]:
        """返回 (chartKind, componentType, 命中的原话)。

        命中的**原话**要留着：组件类型不被系统静默改写时，选型依据写的是
        用户自己说的那个词（「柱状图」），不是内部枚举值。
        """
        for word in TABLE_WORDS:
            if word in text:
                return "", "TABLE", word
        for word, kind in CHART_KIND_MAP.items():
            if word in text:
                return kind, "CHART", word
        return "", "", ""

    @staticmethod
    def _select_component_type(
        text: str, chart_kind: str, component_type: str, type_word: str = "",
    ) -> dict[str, Any]:
        """给新增的板块选组件类型，**理由来自真实命中**（工单 #60 / #65）。

        优先级与方案侧一致：**用户明确指定 > 展示目的映射 > 兜底**。
        用户自己在原话里点了展示形式（柱状图 / 表格）就**沿用** ——
        组件类型不被系统静默改写；依据仍然取自规则库里的真实命中，
        不是事后编的。
        """
        if component_type:
            plan_type = PLAN_TYPE_BY_DEFINITION.get((component_type, chart_kind or ""))
            detail = f"用户原话里的展示形式「{type_word or component_type}」"
            if plan_type:
                selection = selection_for_existing_type(
                    ComponentType(plan_type), detail=detail,
                )
                evidence = selection.as_dict()
            else:
                # 面积图这类没有方案侧对应类型：沿用用户原话，依据只记原话。
                evidence = {
                    "ruleId": "",
                    "componentType": component_type,
                    "detail": detail,
                    "explain": f"{detail}。",
                }
            return {
                "componentType": component_type,
                "chartKind": chart_kind,
                "evidence": evidence,
            }
        selection = select_component_type(text)
        shape = EDIT_COMPONENT_SHAPES.get(selection.componentType.value, {})
        return {
            "componentType": shape.get("componentType", "TABLE"),
            "chartKind": shape.get("chartKind", ""),
            "evidence": selection.as_dict(),
        }

    @staticmethod
    def _apply_candidate_to_spec(
        spec: dict[str, Any], candidate: OutOfCatalogCandidate, reason: str,
        *, simulated: bool = False,
    ) -> dict[str, Any]:
        """把**已解析的候选**（含用户拍板过的口径）写回示意语义描述。

        示意语义描述是转换层按**指标意图**生成的；用户拍板口径之后
        （耗电量？占比？功率？），单位与澄清选项要跟着改成**用户选的那
        一个** —— 否则屏上的单位与用户拍板的不一致。
        """
        if not isinstance(spec, dict):
            return spec
        for item in spec.get("metrics") or []:
            if not isinstance(item, dict):
                continue
            if candidate.unit:
                item["unit"] = candidate.unit
            item["pendingReason"] = reason
            if simulated:
                item["capabilityPending"] = False
            item["deterministicScope"] = deterministic_scope(
                SCOPE_METRIC_FAMILY,
                metric_family_seed_key(
                    str(item.get("kind") or ""),
                    str(item.get("metricKey") or ""),
                    candidate.unit,
                ),
            )
        if candidate.unit:
            spec["unit"] = candidate.unit
        if not candidate.is_ambiguous:
            spec["clarification"] = None
        if simulated:
            spec["capabilityPending"] = False
        return spec

    @staticmethod
    def _candidate_of(plan: EditPlan) -> OutOfCatalogCandidate | None:
        """这份计划里的**目录外候选**。

        优先用计划里**已经解析好的**那份（用户拍板过口径的以它为准），
        没有才按意图重新解析一遍。
        """
        carried = candidate_from_dict(plan.operation.metricCandidate)
        if carried is not None:
            return carried
        return resolve_out_of_catalog_candidate(
            str(plan.operation.metricIntent or "").strip()
        )

    @staticmethod
    def _resolve_out_of_catalog(
        text: str, *, strip_add_prefix: bool = True,
    ) -> OutOfCatalogCandidate | None:
        """把一句话解析成**目录外指标候选**（工单 #65）。

        目录里没有的指标**不再被拒绝**：解析得出候选就带着走（含义不清
        的还要先澄清口径）；解析不出返回 ``None`` —— 上层照既有口径追问，
        不硬造一个候选。
        """
        intent, answer = split_metric_intent(text, strip_add_prefix=strip_add_prefix)
        if not intent:
            return None
        return resolve_out_of_catalog_candidate(intent, answer=answer)

    @staticmethod
    def _leading_add_intent(text: str) -> str:
        """**句首**的「加 / 再加」后面那一段（候选的指标意图）。

        句首才认：句中的「加个两度」不是新增指标（「把温度加个两度」）。
        """
        value = str(text or "").strip()
        for filler in ("帮我", "麻烦", "请", "我想", "我要", "给我"):
            if value.startswith(filler):
                value = value[len(filler):].strip()
                break
        for prefix in ("再加", "加"):
            if value.startswith(prefix):
                return value[len(prefix):].strip(" 　：:，,。.；;！!？?、")
        return ""

    @staticmethod
    def _strip_metric_tail_noise(name: str) -> str:
        """去掉指标名末尾的成分词（「三个指标」→「三个」）。"""
        value = str(name or "").strip()
        for noise in _METRIC_NAME_TAIL_NOISE:
            if value.endswith(noise) and len(value) > len(noise):
                return value[: -len(noise)].strip()
        return value

    def _leading_add_is_metric(
        self, text: str, metrics: dict[str, dict],
    ) -> bool:
        """句首「加 X」里的 X 是不是一个指标（已登记或目录外候选）。

        问句不算（「加了多少度？」是提问，不是新增请求）。
        """
        if any(mark in text for mark in ("？", "?", "吗", "多少", "几")):
            return False
        intent = self._leading_add_intent(text)
        if len(intent) < 2:
            return False
        intent = self._strip_metric_tail_noise(intent)
        if len(intent) < 2:
            return False
        if all(char in _COUNT_ONLY_CHARS for char in intent):
            # 「再加三个指标」里的「三个」是**数量**，不是指标名。
            return False
        if match_metric_by_alias(intent, set(metrics)):
            return True
        return self._resolve_out_of_catalog(intent, strip_add_prefix=False) is not None

    @staticmethod
    def _resolve_metric_target(
        text: str, metrics: dict[str, dict[str, Any]], *, strip_add_prefix: bool,
    ) -> tuple[str | None, OutOfCatalogCandidate | None]:
        """判定「用户说的指标」落在哪一边：已登记指标还是目录外候选。

        返回 ``(metricRef, 候选)``：

        - 命中**已登记指标**时候选是 ``None``（照既有口径走）；
        - 落在**目录外**时 ``metricRef`` 是 ``None``，候选带着口径走
          （含义不清的带可澄清的问句，语义明确的带显式假设）。

        **别名命中 ≠ 识别成功**（规格第 6 条 / 契约表 §3.5）：别名的匹配
        是**子串**，「设备耗电率」包含别名「设备耗电」—— 但用户问的是
        比率，不是 kWh 的耗电量。口径对不上就**不静默映射**到已登记指标。
        """
        candidate = TemplateEditEngine._resolve_out_of_catalog(
            text, strip_add_prefix=strip_add_prefix,
        )
        metric_ref = match_metric_by_alias(text, set(metrics))
        if not metric_ref or candidate is None:
            # 没提到已登记指标 → 目录外候选；抽不出指标意图 → 照既有口径走。
            return (None, candidate) if candidate is not None else (metric_ref, None)
        if intent_names_registered_metric(candidate.intent, metric_ref, metrics):
            return metric_ref, None
        # 口径对不上 → 目录外候选，**不静默映射**到已登记指标。
        return None, candidate

    # ------------------------------------------------------------------
    # 追加指标（新增默认是追加）
    # ------------------------------------------------------------------

    def _plan_append_metrics(
        self,
        text: str,
        target_region: dict[str, Any] | None,
        metrics: dict[str, dict],
        definition: ReportDefinitionDraft,
    ) -> EditPlan | None:
        """「在已有区块里追加指标」的计划。

        返回 ``None`` = 这句话**不是**在已有区块里追加指标（上层照既有
        分支继续走）。返回计划但 ``needsClarification`` = 认出了追加的
        意图，但还没说清要加哪几个 —— 同一个待办里追问，不另开一轮。
        """
        if target_region is None:
            # 没有指到具体卡片 → 不是「往这张卡片里加指标」。
            return None
        names = self._extract_appended_metric_names(text, target_region)
        target = EditTarget(
            regionId=str(target_region.get("id")),
            matchedText=_region_label(target_region),
            confidence=0.9,
        )
        if not names:
            # 看得出是要往这张卡片里加东西，但没说要加哪几个 → 问清楚。
            plan = EditPlan(
                isTemplateEdit=True,
                intent=EditIntent.ADD_METRIC,
                confidence=0.6,
                target=target,
                operation=EditOperation(
                    baseDefinitionRevision=_definition_revision(definition),
                ),
                needsClarification=True,
                clarificationQuestion=(
                    f"要在「{_region_label(target_region)}」这张卡片上追加哪几个指标？"
                    "把指标名字列出来就行。"
                ),
            )
            return plan

        additions: list[EditMetricAddition] = []
        for name in names:
            metric_ref, candidate = self._resolve_metric_target(
                name, metrics, strip_add_prefix=False,
            )
            if metric_ref:
                catalog = metrics.get(metric_ref) or {}
                additions.append(EditMetricAddition(
                    intent=name,
                    label=str(catalog.get("label") or name),
                    unit=str(catalog.get("unit") or ""),
                    displayMode=DISPLAY_MODE_SIMULATED,
                    metricRef=metric_ref,
                ))
                continue
            if candidate is None:
                # 这个名字认不出来 → 不猜，回去问清楚。
                return None
            if candidate.is_ambiguous:
                # 含义不清 → 先澄清口径（同一个待办，不另开一轮）。
                plan = EditPlan(
                    isTemplateEdit=True,
                    intent=EditIntent.ADD_METRIC,
                    confidence=0.6,
                    target=target,
                    operation=EditOperation(
                        addMetrics=[EditMetricAddition(
                            intent=name,
                            label=candidate.label,
                            unit=candidate.unit,
                            displayMode=DISPLAY_MODE_SIMULATED,
                            metricCandidate=candidate.as_dict(),
                        )],
                        baseDefinitionRevision=_definition_revision(definition),
                    ),
                    needsClarification=True,
                    clarificationQuestion=candidate.clarification_question,
                )
                return plan
            additions.append(EditMetricAddition(
                intent=name,
                label=candidate.label,
                unit=candidate.unit,
                displayMode=DISPLAY_MODE_SIMULATED,
                metricCandidate=candidate.as_dict(),
            ))
        if not additions:
            return None
        return EditPlan(
            isTemplateEdit=True,
            intent=EditIntent.ADD_METRIC,
            confidence=0.85,
            target=target,
            operation=EditOperation(
                addMetrics=additions,
                baseDefinitionRevision=_definition_revision(definition),
            ),
        )

    @staticmethod
    def _split_metric_list(text: str) -> list[str]:
        """把一段话按**清单分隔符**拆成一个个指标名。

        通用文本处理，不认具体指标名：顿号 / 逗号 / 分号 / 以及 / 还有 / 和
        都是分隔符；末尾的成分词（「的指标」「这几项」）剥掉；光有数量词
        的片段（「三个」）是**数量**不是名字，丢掉。

        拆不出名字宁可只有一项 —— 上层回去问清楚，不猜。
        """
        parts = [str(text or "")]
        for separator in _METRIC_LIST_SEPARATORS:
            expanded: list[str] = []
            for part in parts:
                expanded.extend(part.split(separator))
            parts = expanded
        names: list[str] = []
        for part in parts:
            name = part.strip(" 　：:，,。.；;！!？?、")
            name = TemplateEditEngine._strip_metric_tail_noise(name)
            if len(name) < 2:
                continue
            if all(char in _COUNT_ONLY_CHARS for char in name):
                # 「再加三个指标」里的「三个」是**数量**，不是指标名。
                continue
            if name not in names:
                names.append(name)
        return names

    @staticmethod
    def _extract_appended_metric_names(
        text: str, region: dict[str, Any] | None,
    ) -> list[str]:
        """从这句话里抽出「要追加的指标清单」。

        做法是**通用**的文本处理：去掉指向目标卡片的那一段 → 在「追加」
        词上切开 → 剥掉连接词 → 按清单分隔符拆开 → 去掉成分词。

        抽不出来的名字宁可没有 —— 上层回去问清楚，不猜。
        """
        rest = TemplateEditEngine._text_without_region_label(text, region)
        best_index = -1
        best_word = ""
        for word in APPEND_WORDS:
            index = rest.find(word)
            if index >= 0 and index > best_index:
                best_index = index
                best_word = word
        if best_index >= 0:
            tail = rest[best_index + len(best_word):].strip()
            head = rest[:best_index].strip()
            rest = tail or head
        for connector in _APPEND_CONNECTORS:
            if rest.startswith(connector):
                rest = rest[len(connector):].strip()
                break
        rest = rest.strip(" 　：:，,。.；;！!？?、")
        return TemplateEditEngine._split_metric_list(rest)

    @staticmethod
    def _parse_grain(text: str, *, explicit_only: bool = False) -> str | None:
        """解析粒度。

        先看明确点名（按天粒度 / 天级）的说法，取最后出现的那次：
        用户自己刚说的选择，要盖过句子里顺带出现的词，
        也盖过卡片名里的字（「小时趋势」里的「小时」不算要改粒度）。

        ``explicit_only=True`` 时**只认明确点名**的写法：改动片段里的
        裸词「小时」「天」多半来自卡片名，不是新值（规格第 48 条）。
        """
        lowered = (text or "").lower()
        explicit: list[tuple[int, str]] = []
        for word, grain in GRAIN_EXPLICIT_FROM_TEXT.items():
            index = lowered.find(word)
            if index >= 0:
                explicit.append((index, grain))
        if explicit:
            return max(explicit)[1]
        if explicit_only:
            return None
        for word, grain in GRAIN_FROM_TEXT.items():
            if word in lowered:
                return grain
        return None

    @staticmethod
    def _text_without_region_label(text: str, region: dict[str, Any] | None) -> str:
        """去掉句子里指向目标卡片的那部分。

        卡片名里的「小时」「天」（「小时趋势」）只用来**定位对象**，
        不是新值：留着它，「把小时趋势的标题改一下」就会被当成改粒度。

        找不到目标卡片、或卡片名没出现在句子里时原样返回。
        """
        if not region:
            return text
        label = _region_label(region)
        if not label or len(label) < 2:
            return text
        if label not in text:
            return text
        return text.replace(label, " ")

    @staticmethod
    def _parse_time_range(text: str) -> tuple[dict[str, Any] | None, str]:
        """解析时间窗新值。

        与编辑态的另一个时间表达解析器**共用同一口径**
        （``edit_time_resolver.resolve_time_expression``）：同一套锚点
        规则、同一份标签、同一套窗口结构。这里不再自带一份词表 ——
        两边各带一张表就会各说各话（「昨日」在一处是无锚点裸词）。
        """
        fragment = (text or "").strip()
        if not fragment:
            return None, ""
        resolution = resolve_time_expression(fragment)
        if resolution.status != STATUS_RESOLVED or not resolution.timeWindow:
            return None, ""
        return resolution.timeWindow, resolution.label

    @staticmethod
    def _extract_rename_title(text: str) -> str | None:
        for word in RENAME_WORDS:
            if word in text:
                after = text.split(word, 1)[1].strip(" ：:，,。.")
                after = re.sub(
                    r"^(这个|它|该)?(section|区域|面板|卡片)?", "", after
                ).strip(" ：:，,。.")
                return after or None
        return None

    @staticmethod
    def _extract_report_name(text: str) -> str | None:
        for word in REPORT_RENAME_WORDS:
            if word in text:
                return text.split(word, 1)[1].strip(" ：:，,。.“”\"'") or None
        return None

    @staticmethod
    def _extract_change_value(text: str) -> str:
        """取「明确的改动动词」之后的新值片段。

        没有改动动词就返回**空串** —— 旧实现回退成整句，于是
        「昨日能耗趋势好丑啊，一个点……」里的「昨日」被当成要写进去的
        时间窗（工单 #58）。卡片名里的时间词只用来定位对象。
        """
        return extract_change_value(text)

    @staticmethod
    def _extract_add_title(text: str) -> str | None:
        for word in ADD_WORDS:
            if word in text:
                after = text.split(word, 1)[1].strip(" ：:，,。.")
                after = re.sub(
                    r"(section|区域|面板|卡片|模块|部分)?$", "", after
                ).strip(" ：:，,。.")
                return after or None
        return None

    # ===== 应用：各 intent =====

    @staticmethod
    def _apply_rename_report(
        definition: ReportDefinitionDraft, plan: EditPlan, base_revision: int
    ) -> tuple[ChangeSet | None, str]:
        new_name = plan.operation.newTitle or ""
        if not new_name:
            return None, "新报表名称为空，请再提供一次。"
        definition.metadata = dict(definition.metadata or {})
        before_name = str(definition.metadata.get("name") or "未命名报表")
        definition.metadata["name"] = new_name
        change_set = ChangeSet(
            baseDefinitionRevision=base_revision,
            changed=[
                ChangeSetEntry(
                    regionId="__report__",
                    path="/metadata/name",
                    before=before_name,
                    after=new_name,
                    summary=f"将报表名称从「{before_name}」改为「{new_name}」",
                )
            ],
            unchanged=[
                ChangeSetUnchanged(regionId=str(region.get("id")), summary="未修改")
                for region in definition.regions
            ],
            impact=["仅修改报表名称，不影响数据、计算和展示结构。"],
        )
        return change_set, f"已把报表名称改成「{new_name}」。请检查右侧草稿，满意后点「同意」保存。"

    @staticmethod
    def _apply_delete(
        definition: ReportDefinitionDraft, plan: EditPlan, base_revision: int
    ) -> tuple[ChangeSet | None, str]:
        region_id = plan.target.regionId
        before_regions = list(definition.regions)
        target = next((r for r in before_regions if r.get("id") == region_id), None)
        if target is None:
            return None, "没有找到要删除的卡片。"
        definition.regions = [r for r in before_regions if r.get("id") != region_id]
        label = _region_label(target)
        change_set = ChangeSet(
            baseDefinitionRevision=base_revision,
            changed=[
                ChangeSetEntry(
                    regionId=region_id,
                    path=f"/regions/{region_id}",
                    before=target,
                    after=None,
                    summary=f"删除「{label}」卡片",
                )
            ],
            unchanged=[
                ChangeSetUnchanged(regionId=str(r.get("id")), summary="未修改")
                for r in definition.regions
            ],
            impact=[f"已从草稿中移除「{label}」。"],
        )
        return change_set, f"已删除「{label}」这张卡片。请检查右侧草稿，满意后点「同意」保存。"

    @staticmethod
    def _apply_rename(
        definition: ReportDefinitionDraft, plan: EditPlan, base_revision: int
    ) -> tuple[ChangeSet | None, str]:
        region_id = plan.target.regionId
        new_title = plan.operation.newTitle or ""
        if not new_title:
            return None, "新标题为空，请再提供一次。"
        target = next((r for r in definition.regions if r.get("id") == region_id), None)
        if target is None:
            return None, "没有找到要改标题的卡片。"
        before_title = _region_label(target)
        target["name"] = new_title
        if isinstance(target.get("component"), dict):
            target["component"]["title"] = new_title
        change_set = ChangeSet(
            baseDefinitionRevision=base_revision,
            changed=[
                ChangeSetEntry(
                    regionId=region_id,
                    path=f"/regions/{region_id}/name",
                    before=before_title,
                    after=new_title,
                    summary=f"将「{before_title}」改名为「{new_title}」",
                )
            ],
            unchanged=[
                ChangeSetUnchanged(regionId=str(r.get("id")), summary="未修改")
                for r in definition.regions
                if r.get("id") != region_id
            ],
            impact=[f"已更新卡片标题：{before_title} → {new_title}。"],
        )
        return change_set, f"已把「{before_title}」改成「{new_title}」。请检查右侧草稿，满意后点「同意」保存。"

    def _apply_add(
        self, definition: ReportDefinitionDraft, plan: EditPlan, base_revision: int,
        catalogs: dict[str, dict], metrics: dict[str, dict],
    ) -> tuple[ChangeSet | None, str]:
        op = plan.operation
        # 目录外指标（#65）：走通用模拟能力出数，不伪造 metricRef。
        if str(op.metricIntent or "").strip() and not op.metricRef:
            return self._apply_add_out_of_catalog(
                definition, plan, base_revision,
            )
        if not (op.sectionTitle and op.metricRef and op.sourceRef):
            return None, "新增卡片还缺标题、指标或数据源，请补充。"

        metric = metrics[op.metricRef]
        new_region_id = f"region_{self._slugify(op.sectionTitle)}"
        # 避免 id 冲突
        existing_ids = {r.get("id") for r in definition.regions}
        base_id = new_region_id
        i = 1
        while new_region_id in existing_ids:
            i += 1
            new_region_id = f"{base_id}_{i}"

        dataset_ref = f"ds_{self._slugify(op.sectionTitle)}"
        # 复用已有 dataset（若 sourceRef 已经登记过）
        reused_dataset_ref: str | None = None
        for ds_name, ds in (definition.datasets or {}).items():
            if isinstance(ds, dict) and ds.get("sourceRef") == op.sourceRef:
                reused_dataset_ref = ds_name
                break
        if reused_dataset_ref:
            dataset_ref = reused_dataset_ref
        else:
            definition.datasets = dict(definition.datasets or {})
            catalog = catalogs[op.sourceRef]
            definition.datasets[dataset_ref] = {
                "sourceRef": op.sourceRef,
                "sourceVersion": op.sourceVersion or catalog.get("sourceVersion", "1.0"),
                "subjectRef": catalog.get("subjectRef", "machine_room"),
                "fields": {
                    name: {
                        "sourceFieldRef": name,
                        **{k: v for k, v in field_meta.items() if k != "sourceFieldRef"},
                    }
                    for name, field_meta in (catalog.get("fields") or {}).items()
                },
            }

        chart_kind = op.chartKind or ""
        component_type = op.componentType or "TABLE"
        if component_type == "CHART":
            component = {
                "type": "CHART",
                "chartKind": chart_kind or "BAR",
                "title": op.sectionTitle,
                "encodings": [
                    {"channel": "X", "fieldRef": "deviceName"},
                    {"channel": "Y", "fieldRef": metric.get("fieldRef", "energyKwh")},
                ],
            }
        elif component_type == "METRIC_CARD":
            component = {
                "type": "METRIC_CARD",
                "title": op.sectionTitle,
                "metric": {
                    "id": f"m_{self._slugify(op.sectionTitle)}",
                    "label": op.sectionTitle,
                    "fieldRef": metric.get("fieldRef", "energyKwh"),
                    "format": {
                        "kind": "NUMBER",
                        "precision": 1,
                        "unitLabel": metric.get("unit") or "",
                    },
                },
            }
        else:
            component = {
                "type": "TABLE",
                "title": op.sectionTitle,
                "columns": [
                    {"id": "col_name", "label": "名称", "fieldRef": "deviceName",
                     "format": {"kind": "TEXT"}},
                    {"id": "col_value", "label": metric.get("label", "数值"),
                     "fieldRef": metric.get("fieldRef", "energyKwh"),
                     "format": {"kind": "NUMBER", "precision": 1,
                                "unitLabel": metric.get("unit") or ""}},
                ],
                "rowNumber": True,
            }

        new_region: dict[str, Any] = {
            "id": new_region_id,
            "name": op.sectionTitle,
            "role": "DETAIL",
            "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12}},
            "data": {
                "inputs": {
                    "main": {
                        "type": "DATASET",
                        "datasetRef": dataset_ref,
                        "periodRef": "CURRENT",
                    }
                },
                "pipeline": {
                    "nodes": [
                        {
                            "id": "n_metric",
                            "type": "METRIC",
                            "inputs": ["input:main"],
                            "metricRef": op.metricRef,
                            "metricVersion": op.metricVersion
                            or metric.get("metricVersion", "1.0"),
                        }
                    ],
                    "outputs": {
                        "main": {
                            "nodeRef": "node:n_metric",
                            "expectedFields": {
                                metric.get("fieldRef", "energyKwh"): {
                                    "dataType": "DECIMAL",
                                    "unit": metric.get("unit") or "",
                                }
                            },
                        }
                    },
                },
            },
            "binding": {"outputRef": "output:main"},
            "component": component,
        }

        regions = list(definition.regions)
        insert_index = len(regions)
        if op.insertAfterRegionId:
            for idx, r in enumerate(regions):
                if r.get("id") == op.insertAfterRegionId:
                    insert_index = idx + 1
                    break
        regions.insert(insert_index, new_region)
        definition.regions = regions

        change_set = ChangeSet(
            baseDefinitionRevision=base_revision,
            changed=[
                ChangeSetEntry(
                    regionId=new_region_id,
                    path=f"/regions/{new_region_id}",
                    before=None,
                    after=new_region,
                    summary=f"新增「{op.sectionTitle}」卡片",
                )
            ],
            unchanged=[
                ChangeSetUnchanged(regionId=str(r.get("id")), summary="未修改")
                for r in regions
                if r.get("id") != new_region_id
            ],
            impact=[
                f"新增卡片「{op.sectionTitle}」，数据来自 {_source_display_label(catalogs, op.sourceRef)}，"
                f"指标是「{metric.get('label') or op.sectionTitle}」。",
                "保存前需要重新校验并预览。",
            ],
        )
        return change_set, f"已新增卡片「{op.sectionTitle}」。请检查右侧草稿，满意后点「同意」保存。"

    # ------------------------------------------------------------------
    # 目录外指标（工单 #65）
    # ------------------------------------------------------------------

    def _apply_add_out_of_catalog(
        self, definition: ReportDefinitionDraft, plan: EditPlan, base_revision: int,
    ) -> tuple[ChangeSet | None, str]:
        """新增一个**目录外指标**的板块。

        目录外指标**不伪造 metricRef** —— 它只有一个稳定的模拟身份
        （契约表 §3.5）。区域标「能力待接通」，说明里写明「按模拟示例
        出数、口径待确认」；栏目身份与取值键由**服务端**定稿，不按标题
        猜列。
        """
        op = plan.operation
        candidate = self._candidate_of(plan)
        if candidate is None:
            return None, "没有解析出要新增的指标，请再说明一次。"
        title = str(op.sectionTitle or candidate.label)
        section_id = self._next_section_id(definition)
        region_id = f"region_{self._slugify(title)}"
        existing_ids = {r.get("id") for r in definition.regions}
        base_id = region_id
        index = 1
        while region_id in existing_ids:
            index += 1
            region_id = f"{base_id}_{index}"

        component_type = str(op.componentType or "TABLE")
        chart_kind = str(op.chartKind or "")
        columns = self._out_of_catalog_columns(candidate, section_id, component_type)
        component = self._out_of_catalog_component(
            candidate, title, component_type, chart_kind, columns,
        )
        mode = source_mode_of_definition(definition.model_dump(mode="json"))
        reason = (
            f"模拟示例数据，仅用于核对版式。{candidate.assumption_note()}"
            if mode.mode == "simulated"
            else out_of_catalog_region_reason(candidate, source_mode=mode.mode)
        )
        region: dict[str, Any] = {
            "id": region_id,
            "name": title,
            "role": self._edit_region_role(component_type, chart_kind),
            "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12}},
            "component": component,
            "templateBinding": {
                "sectionId": section_id,
                # 不伪造真实绑定；模拟示意可以正常出数。
                "dataStatus": "placeholder",
                "placeholderReason": reason,
                "dataSourceLabel": DATA_SOURCE_LABEL,
                # 目录外指标**没有**真实口径：这里不写 metricRef。
                "metricRefs": [],
                "sourceMode": mode.mode,
                # 编辑路径只描述当前展示，不承诺未来接入真实数据。
                "pendingReason": reason,
            },
        }
        context = self._region_context(definition)
        if context:
            region["context"] = context
        # 示意语义描述（语义类别 / 单位 / 稳定模拟身份 / 澄清选项 / 列的
        # 值形态）复用 #59 的能力层，不另起一套。
        region["templateBinding"]["previewSpec"] = self._apply_candidate_to_spec(
            adapt_preview_spec(
                region,
                plan={
                    "sections": [
                        {
                            "id": section_id,
                            "metricIntents": [candidate.intent],
                            "columns": columns,
                        }
                    ]
                },
            ),
            candidate,
            reason,
            simulated=mode.mode == "simulated",
        )

        regions = list(definition.regions)
        insert_index = len(regions)
        if op.insertAfterRegionId:
            for idx, item in enumerate(regions):
                if item.get("id") == op.insertAfterRegionId:
                    insert_index = idx + 1
                    break
        regions.insert(insert_index, region)
        definition.regions = regions

        change_set = ChangeSet(
            baseDefinitionRevision=base_revision,
            changed=[
                ChangeSetEntry(
                    regionId=region_id,
                    path=f"/regions/{region_id}",
                    before=None,
                    after=region,
                    summary=f"新增「{title}」卡片（目录外指标：{candidate.label}）",
                )
            ],
            unchanged=[
                ChangeSetUnchanged(regionId=str(item.get("id")), summary="未修改")
                for item in regions
                if item.get("id") != region_id
            ],
            impact=[
                f"新增卡片「{title}」，指标「{candidate.label}」按模拟示例出数。",
                f"假设：{candidate.assumption_note()}",
                "保存前需要重新校验并预览。",
            ],
        )
        return change_set, (
            f"已新增卡片「{title}」。指标「{candidate.label}」按模拟示例出数；"
            f"{candidate.assumption_note()} "
            "请检查右侧草稿，满意后点「同意」保存。"
        )

    # ------------------------------------------------------------------
    # 追加指标（新增默认是追加）
    # ------------------------------------------------------------------

    @staticmethod
    def _expand_metric_additions(
        additions: list[EditMetricAddition],
    ) -> list[EditMetricAddition]:
        """把「一条里塞了好几个指标」拆成**一条一个指标**。

        用户一句话说了几个指标（「再加总实时功率、运行中的服务器数和告警
        总数」），上游有时把整串当成**一个**指标名。落到定义里就只剩一个
        叫「总实时功率、运行中的服务器数和告警总数」的标签 —— 用户要的是
        **三个**标签，三条都真的出现在右侧。

        拆法是通用文本处理（清单分隔符），不认具体指标名。

        已登记指标（带 ``metricRef``）不拆：它的显示名就是目录里的名字，
        拆开反而认不出。
        """
        expanded: list[EditMetricAddition] = []
        for addition in additions:
            name = str(addition.label or addition.intent or "").strip()
            parts = (
                [name] if addition.metricRef
                else TemplateEditEngine._split_metric_list(name)
            )
            if not parts:
                parts = [name]
            if len(parts) <= 1:
                expanded.append(addition)
                continue
            for part in parts:
                expanded.append(EditMetricAddition(
                    intent=part,
                    label=part,
                    # 单位按**各自的名字**去推断更准 —— 原来那个单位是整串的。
                    unit="",
                    displayMode=addition.displayMode,
                    metricRef=None,
                    metricCandidate=None,
                    note=addition.note,
                ))
        return expanded

    def _apply_add_metric(
        self, definition: ReportDefinitionDraft, plan: EditPlan, base_revision: int,
        metrics: dict[str, dict],
    ) -> tuple[ChangeSet | None, str]:
        """往一个**已有区块**里追加一个/多个指标（**保留**原有指标）。

        追加 ≠ 替换：这里只**加**，原有那一项一个字节都不动。

        一次追加多个当**一次原子修改**：全部成功才落到草稿；任一步失败
        整体不生效 —— 调用方丢弃草稿副本，原草稿保持不变。
        """
        additions = self._expand_metric_additions(plan.operation.addMetrics)
        if not additions:
            return None, "没有解析出要追加的指标。"
        for addition in additions:
            if not str(addition.label or addition.intent or "").strip():
                # 认不出来的那一个 → 整批都不生效（原草稿保持不变）。
                return None, "有指标没认出名字，请再说一次要追加哪几个指标。"
        region_id = str(plan.target.regionId or "")
        target = next(
            (item for item in definition.regions if item.get("id") == region_id), None,
        )
        if target is None:
            return None, "没有找到要追加指标的卡片。"

        binding = target.get("templateBinding")
        binding = dict(binding) if isinstance(binding, dict) else {}
        section_id = str(binding.get("sectionId") or "") or region_id
        component = target.get("component")
        component = copy.deepcopy(component) if isinstance(component, dict) else {}

        spec = binding.get("previewSpec")
        spec = copy.deepcopy(spec) if isinstance(spec, dict) else {}
        if not spec:
            spec = adapt_preview_spec(target)
        spec_metrics = [
            item for item in (spec.get("metrics") or []) if isinstance(item, dict)
        ]
        existing_columns = [
            dict(column) for column in (component.get("columns") or [])
            if isinstance(column, dict)
        ]

        new_columns: list[dict[str, Any]] = []
        new_metrics: list[dict[str, Any]] = []
        for offset, addition in enumerate(additions):
            candidate = candidate_from_dict(addition.metricCandidate)
            label = str(addition.label or addition.intent or "").strip()
            unit = str(addition.unit or (candidate.unit if candidate else "") or "")
            if addition.metricRef:
                kind = classify_metric_kind(
                    intent=label, metric_ref=addition.metricRef,
                )
            elif candidate is not None:
                kind = str(candidate.kind or "generic")
            else:
                kind = "generic"
            # 同一板块里的第几个指标 → 同一个模拟身份：接着已有的往后排。
            metric_index = len(spec_metrics) + offset
            simulated_id = simulated_metric_id(section_id, metric_index)
            display_mode = (
                DISPLAY_MODE_PLACEHOLDER_ONLY
                if str(addition.displayMode or "") == DISPLAY_MODE_PLACEHOLDER_ONLY
                else DISPLAY_MODE_SIMULATED
            )
            new_columns.append({
                "fieldRef": simulated_id,
                "label": label,
                "format": {"unitLabel": unit},
                # 占位 / 非占位**记在定义里**：渲染层照它决定这一列要不要出数值。
                "displayMode": display_mode,
                "bindingKind": "simulated",
            })
            metric_entry = simulated_metric_binding(
                section_id, metric_index, kind, unit,
            )
            metric_entry["label"] = label
            # 展示模式**两处都记**：列上记一份，指标描述上也记一份 ——
            # 渲染层两处都读（口径一致，不会一处占位一处出数）。
            metric_entry["displayMode"] = display_mode
            metric_entry["capabilityPending"] = False
            metric_entry["pendingReason"] = (
                "按用户要求只显示名称，不出数值。"
                if display_mode == DISPLAY_MODE_PLACEHOLDER_ONLY
                else "模拟示例数据，仅用于核对版式。"
            )
            new_metrics.append(metric_entry)

        # 原有指标**保留**：列与指标描述都是「原有 + 新增」。
        component["columns"] = existing_columns + new_columns
        target["component"] = component
        spec["columns"] = [
            dict(column) for column in (component.get("columns") or [])
            if isinstance(column, dict)
        ]
        spec["metrics"] = spec_metrics + new_metrics
        binding["previewSpec"] = spec
        target["templateBinding"] = binding

        labels = "、".join(
            str(item.label or item.intent) for item in additions
        )
        placeholder_labels = "、".join(
            str(item.label or item.intent)
            for item in additions
            if str(item.displayMode or "") == DISPLAY_MODE_PLACEHOLDER_ONLY
        )
        change_set = ChangeSet(
            baseDefinitionRevision=base_revision,
            changed=[ChangeSetEntry(
                regionId=region_id,
                path=f"/regions/{region_id}/component/columns",
                before={"columns": existing_columns},
                after={"columns": component["columns"]},
                summary=f"追加指标：{labels}",
            )],
            unchanged=[
                ChangeSetUnchanged(regionId=str(item.get("id")), summary="未修改")
                for item in definition.regions
                if item.get("id") != region_id
            ],
            impact=[
                f"「{_region_label(target)}」追加了 {len(additions)} 项指标：{labels}；"
                "原有指标保留。",
                *(
                    [f"其中 {placeholder_labels} 按占位显示：只显示名称，不出数值。"]
                    if placeholder_labels else []
                ),
                "保存前需要重新校验并预览。",
            ],
        )
        reply = (
            f"已在「{_region_label(target)}」这张卡片上追加 {len(additions)} 项指标："
            f"{labels}。原有指标保留。"
        )
        if placeholder_labels:
            reply += f"其中 {placeholder_labels} 按你说的先占位：只显示名称，不出数值。"
        return change_set, reply

    @staticmethod
    def _downgrade_to_virtual_metric(plan: EditPlan, metric: dict[str, Any]) -> None:
        """把「目录里有、但当前数据源没这个字段」的指标降级成**虚拟指标**。

        模拟示例模式（``sourceMode == simulated``）下，真实字段准入**让位**：
        不能因为「数据源没这个字段」就拒绝用户的增改请求。

        降级后**不伪造 metricRef** —— 它按目录外口径处理：只有稳定的模拟
        身份，按模拟示例出数，并标「能力待接通」。
        """
        label = str(metric.get("label") or "").strip()
        if not label:
            return
        candidate = OutOfCatalogCandidate(
            intent=label,
            kind=str(metric.get("kind") or "generic"),
            unit=str(metric.get("unit") or ""),
            status=STATUS_OUT_OF_CATALOG,
            note=(
                "这一项按虚拟模拟示例展示，仅用于核对版式。"
            ),
        )
        plan.operation.metricRef = None
        plan.operation.metricVersion = None
        plan.operation.metricIntent = label
        plan.operation.metricCandidate = candidate.as_dict()

    # ------------------------------------------------------------------
    # 纠正（同一个待办里的补充说明）
    # ------------------------------------------------------------------

    def correct_plan(
        self,
        plan: EditPlan,
        correction: str,
        *,
        definition: ReportDefinitionDraft,
        scope: ToolScope | None = None,
    ) -> EditPlan | None:
        """把用户对**当前待办**的纠正落到这份计划上。

        「其他」自由文本是在**同一张卡片里**写的 —— 它是对这个问题的
        纠正，承接上下文，**不是**另一条无关消息。所以纠正落到的是
        **同一份计划**上，不重开一轮普通对话。

        返回 ``None`` = 这句纠正还没说清要改成什么样（上层在同一张卡片里
        继续追问，不新开一轮）。
        """
        text = str(correction or "").strip()
        if not text:
            return None

        # 1) 展示方式（先占位 / 直接出数值）—— 改的是**同一批**指标。
        display_mode = self._display_mode_from_text(text)
        if display_mode is not None and plan.operation.addMetrics:
            corrected = plan.model_copy(deep=True)
            for addition in corrected.operation.addMetrics:
                addition.displayMode = display_mode
            return corrected

        # 2) 纠正里又点了新的指标名 → 并进**同一批**。
        if plan.operation.addMetrics and any(
            word in text for word in APPEND_WORDS
        ):
            extra = self._extract_appended_metric_names(text, None)
            if extra:
                corrected = plan.model_copy(deep=True)
                catalogs, metrics = self._visible_items(scope)
                for name in extra:
                    metric_ref, candidate = self._resolve_metric_target(
                        name, metrics, strip_add_prefix=False,
                    )
                    if metric_ref:
                        catalog = metrics.get(metric_ref) or {}
                        corrected.operation.addMetrics.append(EditMetricAddition(
                            intent=name,
                            label=str(catalog.get("label") or name),
                            unit=str(catalog.get("unit") or ""),
                            displayMode=DISPLAY_MODE_SIMULATED,
                            metricRef=metric_ref,
                        ))
                        continue
                    if candidate is None:
                        continue
                    corrected.operation.addMetrics.append(EditMetricAddition(
                        intent=name,
                        label=candidate.label,
                        unit=candidate.unit,
                        displayMode=(
                            display_mode
                            if display_mode is not None
                            else DISPLAY_MODE_SIMULATED
                        ),
                        metricCandidate=candidate.as_dict(),
                    ))
                return corrected

        # 3) 纠正说的是同一张卡片上的另一件事 → 按纠正重新解析一份计划。
        #    **目标必须还是那张卡片**：换一张卡片的指令是**新任务**，不是对
        #    当前待办的纠正（不能借「其他」这个口子改别的地方）。
        reparsed = self.parse(
            message=text,
            definition=definition,
            selected_region_id=plan.target.regionId or None,
            scope=scope,
        )
        if reparsed.intent == EditIntent.UNKNOWN or reparsed.needsClarification:
            return None
        if plan.target.regionId and reparsed.target.regionId != plan.target.regionId:
            return None
        return reparsed

    @staticmethod
    def _display_mode_from_text(text: str) -> str | None:
        """这句话说的是「先占位」还是「直接出数值」？

        认不出来返回 ``None`` —— 不猜展示方式。
        """
        value = str(text or "")
        if any(word in value for word in PLACEHOLDER_MODE_WORDS):
            return DISPLAY_MODE_PLACEHOLDER_ONLY
        if any(word in value for word in SIMULATED_MODE_WORDS):
            return DISPLAY_MODE_SIMULATED
        return None

    @staticmethod
    def _next_section_id(definition: ReportDefinitionDraft) -> str:
        """新板块的板块 id：接着已有的排下去，不与别人重号。"""
        used: set[str] = set()
        for region in definition.regions or []:
            binding = region.get("templateBinding")
            if isinstance(binding, dict) and binding.get("sectionId"):
                used.add(str(binding["sectionId"]))
        index = 1
        while f"section_{index}" in used:
            index += 1
        return f"section_{index}"

    @staticmethod
    def _region_context(definition: ReportDefinitionDraft) -> dict[str, Any]:
        """新板块的上下文：与其它区域**同源**（时间窗 / 粒度）。"""
        for region in definition.regions or []:
            context = region.get("context")
            if isinstance(context, dict) and context:
                return copy.deepcopy(context)
        return {}

    @staticmethod
    def _edit_region_role(component_type: str, chart_kind: str) -> str:
        """新板块在报表里的角色（载荷层照它带，不自己猜）。"""
        plan_type = PLAN_TYPE_BY_DEFINITION.get((component_type, chart_kind or ""))
        if plan_type:
            shape = EDIT_COMPONENT_SHAPES.get(plan_type, {})
            role = shape.get("role")
            if role:
                return role
        return {"METRIC_CARD": "SUMMARY", "TEXT": "DESCRIPTION"}.get(component_type, "DETAIL")

    @staticmethod
    def _out_of_catalog_columns(
        candidate: OutOfCatalogCandidate, section_id: str, component_type: str,
    ) -> list[dict[str, Any]]:
        """目录外指标的**定稿栏目**（服务端定稿，不按标题猜列）。

        表格板块给两列：对象名 + 这一项指标。列身份（``id``）与取值键
        （``fieldRef``）都是服务端定的；取值键是**行对象键**
        （``section_N.col_M``），不是数据源字段。列的值形态**声明**出来
        （``valueType``），造数器按描述造数，不按标题猜。
        """
        if component_type != "TABLE":
            return []
        value_format: dict[str, Any] = {"kind": "NUMBER", "precision": 1}
        if candidate.unit:
            value_format["unitLabel"] = candidate.unit
        return [
            {
                "id": "col_name", "label": "名称",
                "fieldRef": f"{section_id}.col_1",
                "format": {"kind": "TEXT"},
                "valueType": "device_name",
            },
            {
                "id": "col_value", "label": candidate.label,
                "fieldRef": f"{section_id}.col_2",
                "format": value_format,
            },
        ]

    @staticmethod
    def _out_of_catalog_component(
        candidate: OutOfCatalogCandidate,
        title: str,
        component_type: str,
        chart_kind: str,
        columns: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """目录外指标那一块的组件（**不写 metricRef / 数据源字段**）。"""
        if component_type == "CHART":
            return {
                "type": "CHART",
                "chartKind": chart_kind or "BAR",
                "title": title,
                # 纵轴按区域的**语义类别**取值，不指认某个数据源字段。
                "encodings": [],
            }
        if component_type == "METRIC_CARD":
            fmt: dict[str, Any] = {"kind": "NUMBER", "precision": 1}
            if candidate.unit:
                fmt["unitLabel"] = candidate.unit
            return {
                "type": "METRIC_CARD",
                "title": title,
                "metric": {
                    "id": f"m_{title}",
                    "label": candidate.label,
                    "fieldRef": "",
                    "format": fmt,
                },
            }
        return {
            "type": "TABLE",
            "title": title,
            "columns": [dict(column) for column in columns],
            "rowNumber": True,
        }

    @staticmethod
    def _apply_change_source(
        definition: ReportDefinitionDraft, plan: EditPlan, base_revision: int,
        catalogs: dict[str, dict],
    ) -> tuple[ChangeSet | None, str]:
        region_id = plan.target.regionId
        new_source = plan.operation.sourceRef
        if not new_source:
            return None, "没有解析出新的数据源。"
        target = next((r for r in definition.regions if r.get("id") == region_id), None)
        if target is None:
            return None, "没有找到要改数据源的卡片。"
        data = target.get("data") if isinstance(target.get("data"), dict) else {}
        inputs = data.get("inputs") if isinstance(data.get("inputs"), dict) else {}
        changed_dataset: str | None = None
        before_source: str | None = None
        input_name: str | None = None
        for name, inp in inputs.items():
            if isinstance(inp, dict) and inp.get("datasetRef"):
                ds_ref = inp["datasetRef"]
                dataset = (definition.datasets or {}).get(ds_ref)
                if isinstance(dataset, dict):
                    before_source = dataset.get("sourceRef")
                    catalog = catalogs[new_source]
                    usage_count = 0
                    for region in definition.regions:
                        region_inputs = (
                            ((region.get("data") or {}).get("inputs") or {})
                            if isinstance(region.get("data"), dict)
                            else {}
                        )
                        usage_count += sum(
                            1
                            for region_input in region_inputs.values()
                            if isinstance(region_input, dict)
                            and region_input.get("datasetRef") == ds_ref
                        )

                    new_dataset = {
                        **copy.deepcopy(dataset),
                        "sourceRef": new_source,
                        "sourceVersion": (
                            plan.operation.sourceVersion
                            or catalog.get("sourceVersion", "1.0")
                        ),
                        "subjectRef": catalog.get(
                            "subjectRef", dataset.get("subjectRef", "machine_room")
                        ),
                        "fields": {
                            field_name: {
                                "sourceFieldRef": field_name,
                                **copy.deepcopy(field_meta),
                            }
                            for field_name, field_meta in (catalog.get("fields") or {}).items()
                        },
                    }
                    if usage_count > 1:
                        suffix = re.sub(r"[^\w]+", "_", new_source).strip("_").lower()
                        new_ds_ref = f"{ds_ref}_{suffix}"
                        candidate = new_ds_ref
                        counter = 2
                        while candidate in (definition.datasets or {}):
                            candidate = f"{new_ds_ref}_{counter}"
                            counter += 1
                        definition.datasets[candidate] = new_dataset
                        inp["datasetRef"] = candidate
                        changed_dataset = candidate
                    else:
                        definition.datasets[ds_ref] = new_dataset
                        changed_dataset = ds_ref
                    input_name = str(name)
                    break
        if changed_dataset is None:
            return None, "这张卡片未绑定数据集，无法切换数据源。"
        change_set = ChangeSet(
            baseDefinitionRevision=base_revision,
            changed=[
                ChangeSetEntry(
                    regionId=region_id,
                    path=f"/regions/{region_id}/data/inputs/{input_name}/datasetRef",
                    before={"sourceRef": before_source},
                    after={
                        "datasetRef": changed_dataset,
                        "sourceRef": new_source,
                        "sourceVersion": plan.operation.sourceVersion,
                    },
                    summary=(
                        "切换数据源："
                        f"{_source_display_label(catalogs, before_source)}"
                        f" → {_source_display_label(catalogs, new_source)}"
                    ),
                )
            ],
            unchanged=[
                ChangeSetUnchanged(regionId=str(r.get("id")), summary="未修改")
                for r in definition.regions
                if r.get("id") != region_id
            ],
            impact=[
                "需要重新校验字段映射与权限。",
                "保存前需要重新预览。",
            ],
        )
        reply = (
            f"已把「{_region_label(target)}」的数据源换成 "
            f"{_source_display_label(catalogs, new_source)}。"
            "保存前需要重新校验字段映射，请检查后再确认。"
        )
        return change_set, reply

    @staticmethod
    def _apply_change_visualization(
        definition: ReportDefinitionDraft, plan: EditPlan, base_revision: int
    ) -> tuple[ChangeSet | None, str]:
        region_id = plan.target.regionId
        new_component_type = plan.operation.componentType or "CHART"
        new_chart_kind = plan.operation.chartKind or ""
        target = next((r for r in definition.regions if r.get("id") == region_id), None)
        if target is None:
            return None, "没有找到要改展示形式的卡片。"
        before_component = copy.deepcopy(target.get("component") or {})
        component = dict(before_component)
        component["type"] = new_component_type
        if new_component_type == "CHART":
            component["chartKind"] = new_chart_kind or "BAR"
            # 保守保留 encodings；若没有则给默认
            x_field = (
                "timestamp" if target.get("role") == "TREND" else "deviceName"
            )
            component.setdefault(
                "encodings",
                [
                    {"channel": "X", "fieldRef": x_field},
                    {"channel": "Y", "fieldRef": "energyKwh"},
                ],
            )
            # TABLE 独有字段清掉
            component.pop("columns", None)
            component.pop("rowNumber", None)
        elif new_component_type == "TABLE":
            component.pop("chartKind", None)
            component.pop("encodings", None)
            is_trend = target.get("role") == "TREND"
            component.setdefault(
                "columns",
                [
                    {"id": "col_name", "label": "时间" if is_trend else "名称",
                     "fieldRef": "timestamp" if is_trend else "deviceName",
                     "format": {"kind": "TEXT"}},
                    {"id": "col_value", "label": "数值", "fieldRef": "energyKwh",
                     "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"}},
                ],
            )
            component.setdefault("rowNumber", True)
        target["component"] = component
        change_set = ChangeSet(
            baseDefinitionRevision=base_revision,
            changed=[
                ChangeSetEntry(
                    regionId=region_id,
                    path=f"/regions/{region_id}/component",
                    before=before_component,
                    after=component,
                    summary=(
                        f"展示形式：{before_component.get('type')} → {new_component_type}"
                        + (f"({new_chart_kind})" if new_chart_kind else "")
                    ),
                )
            ],
            unchanged=[
                ChangeSetUnchanged(regionId=str(r.get("id")), summary="未修改")
                for r in definition.regions
                if r.get("id") != region_id
            ],
            impact=["建议重新校验字段映射。"],
        )
        reply = f"已把「{_region_label(target)}」改成{_component_display_label(new_component_type)}展示。"
        if new_chart_kind:
            reply += f"（图表类型：{new_chart_kind}）"
        return change_set, reply

    @staticmethod
    def _apply_change_grain(
        definition: ReportDefinitionDraft, plan: EditPlan, base_revision: int
    ) -> tuple[ChangeSet | None, str, str]:
        region_id = plan.target.regionId
        grain = plan.operation.grain or {}
        new_unit = grain.get("unit")
        if not new_unit:
            return None, "没有解析出新的粒度。", ""
        target = next((r for r in definition.regions if r.get("id") == region_id), None)
        if target is None:
            return None, "没有找到要改粒度的卡片。", ""
        before_label = _region_label(target)
        context = target.get("context") if isinstance(target.get("context"), dict) else {}
        before_grain = copy.deepcopy(context.get("grain"))
        old_value = before_grain.get("value") if isinstance(before_grain, dict) else None
        old_unit = old_value.get("unit") if isinstance(old_value, dict) else None
        new_grain_obj = {"mode": "REPLACE", "value": {"unit": new_unit}}
        context["grain"] = new_grain_obj
        target["context"] = context

        # 同步 pipeline 节点里的 resample grain
        pipeline = (
            (target.get("data") or {}).get("pipeline")
            if isinstance(target.get("data"), dict)
            else None
        )
        if isinstance(pipeline, dict):
            for node in pipeline.get("nodes") or []:
                if isinstance(node, dict) and node.get("type") == "RESAMPLE":
                    node_grain = node.get("grain") if isinstance(node.get("grain"), dict) else {}
                    node_grain["unit"] = new_unit
                    node["grain"] = node_grain

        changed = [
            ChangeSetEntry(
                regionId=region_id,
                path=f"/regions/{region_id}/context/grain",
                before=before_grain,
                after=new_grain_obj,
                summary=f"统计粒度：{old_unit or '原'} → {new_unit}",
            )
        ]
        impact = [f"按 {new_unit} 粒度重新编译，保存前需要重新校验并预览。"]
        new_title = TREND_GRAIN_TITLES.get(new_unit)
        if (
            target.get("role") == "TREND"
            and old_unit in TREND_GRAIN_TITLES
            and target.get("name") == TREND_GRAIN_TITLES.get(old_unit)
            and new_title
            and new_title != before_label
        ):
            target["name"] = new_title
            component = target.get("component")
            if isinstance(component, dict) and component.get("title") == before_label:
                component["title"] = new_title
            changed.append(ChangeSetEntry(
                regionId=region_id,
                path=f"/regions/{region_id}/name",
                before=before_label,
                after=new_title,
                summary=f"趋势标题：{before_label} → {new_title}",
            ))
            impact.append(f"趋势标题同步为「{new_title}」。")

        change_set = ChangeSet(
            baseDefinitionRevision=base_revision,
            changed=changed,
            unchanged=[
                ChangeSetUnchanged(regionId=str(r.get("id")), summary="未修改")
                for r in definition.regions
                if r.get("id") != region_id
            ],
            impact=impact,
        )
        md = (
            f"### 统计粒度变更\n\n"
            f"已将「{before_label}」的统计粒度改为{_grain_display_label(new_unit)}。"
            f"后续预览会按该粒度重新编译；"
            + ("注意分钟级一天约 1440 个点，可能影响渲染性能。" if new_unit == "MINUTE" else "")
        )
        reply = f"已把「{before_label}」的统计粒度改成{_grain_display_label(new_unit)}。"
        if target.get("name") != before_label:
            reply += f"趋势标题同步为「{target['name']}」。"
        return change_set, reply, md

    @staticmethod
    def _apply_change_time_range(
        definition: ReportDefinitionDraft, plan: EditPlan, base_revision: int
    ) -> tuple[ChangeSet | None, str]:
        region_id = plan.target.regionId
        new_window = plan.operation.timeWindow
        label = plan.operation.timeWindowLabel or ""
        if not new_window:
            return None, "没有解析出新的时间范围。"
        if error := fixed_interval_error(new_window):
            return None, error
        target = next((r for r in definition.regions if r.get("id") == region_id), None)
        if target is None:
            return None, "没有找到要改时间范围的卡片。"
        context = target.get("context") if isinstance(target.get("context"), dict) else {}
        before_window = copy.deepcopy(context.get("timeWindow"))
        context["timeWindow"] = new_window
        target["context"] = context
        change_set = ChangeSet(
            baseDefinitionRevision=base_revision,
            changed=[
                ChangeSetEntry(
                    regionId=region_id,
                    path=f"/regions/{region_id}/context/timeWindow",
                    before=before_window,
                    after=new_window,
                    summary=f"时间范围改为「{label}」",
                )
            ],
            unchanged=[
                ChangeSetUnchanged(regionId=str(r.get("id")), summary="未修改")
                for r in definition.regions
                if r.get("id") != region_id
            ],
            impact=["查询窗口变化，保存前需要重新预览。"],
        )
        return change_set, f"已把「{_region_label(target)}」的时间范围改成「{label}」。"

    def _apply_change_metric(
        self,
        definition: ReportDefinitionDraft, plan: EditPlan, base_revision: int,
        metrics: dict[str, dict],
    ) -> tuple[ChangeSet | None, str]:
        region_id = plan.target.regionId
        new_metric_ref = plan.operation.metricRef
        out_of_catalog_intent = str(plan.operation.metricIntent or "").strip()
        if not new_metric_ref and not out_of_catalog_intent:
            return None, "没有解析出新的指标。"
        metric = metrics.get(new_metric_ref) if new_metric_ref else None
        if new_metric_ref and not metric:
            return None, "这个指标当前不在可选范围内，请换一个指标。"
        target = next((r for r in definition.regions if r.get("id") == region_id), None)
        if target is None:
            return None, "没有找到要改指标的卡片。"
        if not new_metric_ref:
            # 目录外指标（#65）：不再以「未登记」拒绝 —— 换成模拟指标，
            # 标「能力待接通」，口径待确认。
            return self._apply_change_metric_out_of_catalog(
                definition, plan, base_revision, target, metrics,
            )
        pipeline = (
            (target.get("data") or {}).get("pipeline")
            if isinstance(target.get("data"), dict)
            else None
        )
        before_metric: dict[str, Any] | None = None
        old_field_ref: str | None = None
        metric_updated = False
        if isinstance(pipeline, dict):
            for node in pipeline.get("nodes") or []:
                if isinstance(node, dict) and node.get("type") == "METRIC":
                    before_metric = {
                        "metricRef": node.get("metricRef"),
                        "metricVersion": node.get("metricVersion"),
                    }
                    node["metricRef"] = new_metric_ref
                    node["metricVersion"] = plan.operation.metricVersion or metric["metricVersion"]
                    metric_updated = True
                    break
            if not metric_updated:
                for node in pipeline.get("nodes") or []:
                    measures = node.get("measures") if isinstance(node, dict) else None
                    if not isinstance(measures, list) or not measures:
                        continue
                    measure = next(
                        (item for item in measures if isinstance(item, dict)),
                        None,
                    )
                    if measure is None:
                        continue
                    old_field_ref = str(
                        measure.get("asField") or measure.get("fieldRef") or ""
                    ) or None
                    before_metric = {
                        "fieldRef": measure.get("fieldRef"),
                        "asField": measure.get("asField"),
                    }
                    measure["fieldRef"] = metric["fieldRef"]
                    measure["asField"] = metric["fieldRef"]
                    metric_updated = True
                    break
        if not metric_updated:
            return None, "该卡片没有可替换的指标计算节点。"

        # 更新 component.metric 展示信息
        component = target.get("component") if isinstance(target.get("component"), dict) else {}
        if component.get("type") == "METRIC_CARD" and isinstance(component.get("metric"), dict):
            old_field_ref = old_field_ref or component["metric"].get("fieldRef")
            component["metric"]["label"] = metric["label"]
            component["metric"]["fieldRef"] = metric["fieldRef"]
            fmt = component["metric"].get("format")
            if isinstance(fmt, dict):
                fmt["unitLabel"] = metric.get("unit") or fmt.get("unitLabel", "")
        for column in component.get("columns") or []:
            if isinstance(column, dict) and (
                not old_field_ref or column.get("fieldRef") == old_field_ref
            ):
                column["fieldRef"] = metric["fieldRef"]
                column["label"] = metric["label"]
                fmt = column.get("format")
                if isinstance(fmt, dict):
                    fmt["unitLabel"] = metric.get("unit") or ""
        for encoding in component.get("encodings") or []:
            if isinstance(encoding, dict) and encoding.get("channel") == "Y":
                encoding["fieldRef"] = metric["fieldRef"]

        binding = target.get("binding") if isinstance(target.get("binding"), dict) else {}
        for sort in binding.get("sort") or []:
            if isinstance(sort, dict) and (
                not old_field_ref or sort.get("fieldRef") == old_field_ref
            ):
                sort["fieldRef"] = metric["fieldRef"]

        change_set = ChangeSet(
            baseDefinitionRevision=base_revision,
            changed=[
                ChangeSetEntry(
                    regionId=region_id,
                    path=f"/regions/{region_id}/data/pipeline/metric",
                    before=before_metric,
                    after={
                        "metricRef": new_metric_ref,
                        "metricVersion": plan.operation.metricVersion or metric["metricVersion"],
                    },
                    summary=(
                        "指标："
                        f"{_metric_display_label(metrics, (before_metric or {}).get('metricRef'))}"
                        f" → {_metric_display_label(metrics, new_metric_ref)}"
                    ),
                )
            ],
            unchanged=[
                ChangeSetUnchanged(regionId=str(r.get("id")), summary="未修改")
                for r in definition.regions
                if r.get("id") != region_id
            ],
            impact=[
                "指标口径变化，保存前需要重新校验与预览。",
            ],
        )
        return change_set, f"已把「{_region_label(target)}」的指标换成「{metric['label']}」。"

    def _apply_change_metric_out_of_catalog(
        self, definition: ReportDefinitionDraft, plan: EditPlan, base_revision: int,
        target: dict[str, Any], metrics: dict[str, dict],
    ) -> tuple[ChangeSet | None, str]:
        """把某一块的指标换成一个**目录外指标**（工单 #65）。

        目录外指标**不伪造 metricRef**：原来的指标引用被清掉，区域改挂
        **模拟身份**（``bindingKind = "simulated"``），并标「能力待接通 /
        口径待确认」。这一块照样按模拟示例出数。
        """
        candidate = self._candidate_of(plan)
        if candidate is None:
            return None, "没有解析出新的指标。"
        region_id = str(target.get("id") or "")
        binding = target.get("templateBinding")
        binding = dict(binding) if isinstance(binding, dict) else {}
        section_id = str(binding.get("sectionId") or "") or self._next_section_id(definition)
        mode = source_mode_of_definition(definition.model_dump(mode="json"))
        reason = (
            f"模拟示例数据，仅用于核对版式。{candidate.assumption_note()}"
            if mode.mode == "simulated"
            else out_of_catalog_region_reason(candidate, source_mode=mode.mode)
        )

        # 原来的真实指标引用清掉：目录外指标没有真实口径。
        before_metric: dict[str, Any] | None = None
        old_field_ref: str | None = None
        data = target.get("data") if isinstance(target.get("data"), dict) else {}
        pipeline = data.get("pipeline") if isinstance(data.get("pipeline"), dict) else None
        if isinstance(pipeline, dict):
            for node in pipeline.get("nodes") or []:
                if isinstance(node, dict) and node.get("type") == "METRIC":
                    before_metric = {
                        "metricRef": node.get("metricRef"),
                        "metricVersion": node.get("metricVersion"),
                    }
                    # 换掉之前那一列指认的字段（表格的「数值」列）。
                    old_metric = metrics.get(str(node.get("metricRef") or ""))
                    if isinstance(old_metric, dict):
                        old_field_ref = str(old_metric.get("fieldRef") or "") or None
                    node["metricRef"] = ""
                    node.pop("metricVersion", None)
                    break
        component = target.get("component") if isinstance(target.get("component"), dict) else {}
        component = copy.deepcopy(component)
        if component.get("type") == "METRIC_CARD" and isinstance(component.get("metric"), dict):
            old_field_ref = str(component["metric"].get("fieldRef") or "") or None
            component["metric"]["label"] = candidate.label
            component["metric"]["fieldRef"] = ""
            fmt = component["metric"].get("format")
            if isinstance(fmt, dict):
                fmt["unitLabel"] = candidate.unit or ""
        for column in component.get("columns") or []:
            if not isinstance(column, dict):
                continue
            # 只换**指标那一列**；对象名 / 时间列不动。
            column_id = str(column.get("id") or "")
            is_value_column = column_id in ("col_value", "col_energy") or (
                old_field_ref and column.get("fieldRef") == old_field_ref
            )
            if is_value_column:
                column["label"] = candidate.label
                fmt = column.get("format")
                if isinstance(fmt, dict):
                    fmt["unitLabel"] = candidate.unit or ""
                column.pop("valueType", None)
                # 取值键改成**行对象键**：这一列不再指认某个数据源字段。
                column["fieldRef"] = f"{section_id}.col_value"
        if component.get("type") == "CHART":
            # 纵轴按区域的**语义类别**取值，不指认某个数据源字段。
            component["encodings"] = []
        target["component"] = component

        columns = [
            dict(column) for column in (component.get("columns") or [])
            if isinstance(column, dict)
        ]
        binding.update({
            "sectionId": section_id,
            "dataStatus": "placeholder",
            "placeholderReason": reason,
            # 目录外指标**没有**真实口径：这里不写 metricRef。
            "metricRefs": [],
            "sourceMode": mode.mode,
            # 同上：区域级占位文案复用模板层那一份（按来源模式分叉）。
            "pendingReason": reason,
        })
        target["templateBinding"] = binding
        # 示意语义描述复用 #59 的能力层：语义类别 / 单位 / 稳定模拟身份 /
        # 澄清选项 / 列的值形态。
        binding["previewSpec"] = self._apply_candidate_to_spec(
            adapt_preview_spec(
                target,
                plan={
                    "sections": [
                        {
                            "id": section_id,
                            "metricIntents": [candidate.intent],
                            "columns": columns,
                        }
                    ]
                },
            ),
            candidate,
            reason,
            simulated=mode.mode == "simulated",
        )

        change_set = ChangeSet(
            baseDefinitionRevision=base_revision,
            changed=[
                ChangeSetEntry(
                    regionId=region_id,
                    path=f"/regions/{region_id}/data/pipeline/metric",
                    before=before_metric,
                    after={
                        "metricRef": "",
                        "bindingKind": "simulated",
                        "simulatedId": binding["previewSpec"]["metrics"][0].get("simulatedId")
                        if binding["previewSpec"].get("metrics") else None,
                    },
                    summary=(
                        f"指标：{(before_metric or {}).get('metricRef') or '未知'}"
                        f" → {candidate.label}（目录外模拟）"
                    ),
                )
            ],
            unchanged=[
                ChangeSetUnchanged(regionId=str(item.get("id")), summary="未修改")
                for item in definition.regions
                if item.get("id") != region_id
            ],
            impact=[
                f"指标换成「{candidate.label}」：按模拟示例出数。",
                f"假设：{candidate.assumption_note()}",
                "指标口径变化，保存前需要重新校验与预览。",
            ],
        )
        return change_set, (
            f"已把「{_region_label(target)}」的指标换成「{candidate.label}」。"
            "这一块按模拟示例出数；"
            f"{candidate.assumption_note()}"
        )

    @staticmethod
    def _apply_change_top_n(
        definition: ReportDefinitionDraft, plan: EditPlan, base_revision: int
    ) -> tuple[ChangeSet | None, str]:
        region_id = plan.target.regionId
        top_n = plan.operation.topN
        if not top_n:
            return None, "没有解析出要展示多少条。"
        target = next((r for r in definition.regions if r.get("id") == region_id), None)
        if target is None:
            return None, "没有找到要改展示条数的卡片。"
        binding = target.get("binding") if isinstance(target.get("binding"), dict) else {}
        before_topn = binding.get("topN")
        binding["topN"] = top_n
        target["binding"] = binding
        change_set = ChangeSet(
            baseDefinitionRevision=base_revision,
            changed=[
                ChangeSetEntry(
                    regionId=region_id,
                    path=f"/regions/{region_id}/binding/topN",
                    before=before_topn,
                    after=top_n,
                    summary=f"展示条数：{before_topn} → {top_n}",
                )
            ],
            unchanged=[
                ChangeSetUnchanged(regionId=str(r.get("id")), summary="未修改")
                for r in definition.regions
                if r.get("id") != region_id
            ],
            impact=["输出条数变化，建议重新预览。"],
        )
        return change_set, f"已把「{_region_label(target)}」的展示条数改成 {top_n} 条。"

    @staticmethod
    def _apply_change_sort(
        definition: ReportDefinitionDraft, plan: EditPlan, base_revision: int
    ) -> tuple[ChangeSet | None, str]:
        region_id = plan.target.regionId
        direction = plan.operation.sortDirection or "DESC"
        target = next((r for r in definition.regions if r.get("id") == region_id), None)
        if target is None:
            return None, "没有找到要改排序的 section。"
        binding = target.get("binding") if isinstance(target.get("binding"), dict) else {}
        before_sort = copy.deepcopy(binding.get("sort"))
        sort_field = plan.operation.sortField or (
            before_sort[0]["fieldRef"] if before_sort else "energyKwh"
        )
        new_sort = [{"fieldRef": sort_field, "direction": direction}]
        binding["sort"] = new_sort
        target["binding"] = binding
        change_set = ChangeSet(
            baseDefinitionRevision=base_revision,
            changed=[
                ChangeSetEntry(
                    regionId=region_id,
                    path=f"/regions/{region_id}/binding/sort",
                    before=before_sort,
                    after=new_sort,
                    summary=f"排序：{before_sort} → {new_sort}",
                )
            ],
            unchanged=[
                ChangeSetUnchanged(regionId=str(r.get("id")), summary="未修改")
                for r in definition.regions
                if r.get("id") != region_id
            ],
            impact=["排序方向变化，建议重新预览。"],
        )
        return change_set, f"已把「{_region_label(target)}」的排序改成{_sort_display_label(direction)}。"

    # ===== Markdown =====

    @staticmethod
    def _markdown_display_options(plan: EditPlan) -> str:
        op = plan.operation
        lines = [f"### 「{plan.target.matchedText}」可选展示方式\n"]
        for item in op.displayOptions:
            tag = "✅ 推荐" if item.get("recommended") else "可选"
            lines.append(f"- **{item.get('label')}**（{tag}）：{item.get('reason', '')}")
        if op.disabledOptions:
            lines.append("\n#### 不建议")
            for item in op.disabledOptions:
                lines.append(f"- {item.get('label')}：{item.get('reason', '')}")
        return "\n".join(lines)

    @staticmethod
    def _markdown_grain_unsupported(plan: EditPlan) -> str:
        op = plan.operation
        unit = (op.grain or {}).get("unit", "")
        return (
            f"### 无法改为 {unit} 粒度\n\n"
            f"当前数据源 `{op.sourceRef}` 支持粒度：{'、'.join(op.supportedGrains) or '未登记'}，"
            f"不支持 `{unit}`。可以改用支持该粒度的数据源，或改为支持的粒度。"
        )

    @staticmethod
    def _slugify(text: str) -> str:
        # 保留字母数字下划线；中文按 unicode 保留
        text = (text or "").strip()
        text = re.sub(r"\s+", "_", text)
        text = re.sub(r"[^\w\u4e00-\u9fa5]+", "_", text)
        return text.strip("_").lower() or "section"


template_edit_engine = TemplateEditEngine()
