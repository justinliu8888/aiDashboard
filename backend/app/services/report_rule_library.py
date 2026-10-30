"""展示规则库（工单 #60 / 统一开发 05）。

规格依据：《开发规格 - 报表智能生成与调整》第二章「规则库与选型」第 9–11 条。

这份模块是**方案侧选型规则的唯一来源**：

- 规则随代码维护：没有规则数据库、没有后台、没有自动学习（规格第 9 条）。
- 每条规则都带**稳定 ID**、适用条件、建议展示、禁用条件、优先级与解释模板。
- 优先级是**显式字段**，不再是元组排列顺序的副作用（规格第 10 条）。
- 模型只给语义候选，组件类型由服务端按规则前提定稿；理由由**命中的规则**
  渲染，不是事后编的（规格第 11 条）。
- 命中依据（规则 ID + 命中细节）与规则版本随方案一起持久化；规则版本变了
  也不追溯改写历史方案已经给出的理由。

规则族：

====================  ====================================================
规则族                 作用
====================  ====================================================
``intent``            展示目的关键词 → 展示目的（模型的语义候选）
``mapping``           展示目的 → 建议的组件类型
``explicit``          用户明确指定的组件类型（类型词 + 指令词，且没被否定）
``column``            表格栏目的系统补齐依据（预设 → 用户点名的栏目）
``grain``             时间粒度关键词 → 粒度
``fallback``          没有更具体依据时的兜底（模型原选 / 纯说明板块）
====================  ====================================================

选型的三档优先级是**显式**的：用户明确指定 300 > 按展示目的映射 200 >
模型原选 100。这三档与规则表里的 ``priority`` 是同一套数字，不另开一套。
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.schemas.report_plan import ComponentType, DisplayIntent, GrainUnit


#: 规则库版本。规则表的**语义**变了才改它（改了要同步
#: ``docs/rules/report-rule-library.md``）。方案里记着它，所以历史方案
#: 看得出「当时是哪一版规则给出的理由」。
#:
#: 1.0.1 —— 补回指令词正则里被漏掉的否定 lookbehind（「不要那种柱状图」
#: 里的「要」不再被当成指令词），并让禁用条件与指令词层同口径。
RULE_LIBRARY_VERSION = "1.0.1"


def rule_library_version() -> str:
    """当前规则库版本。调用时读模块全局，方便测试模拟「规则库升版」。"""
    return RULE_LIBRARY_VERSION


# ---------- 规则族 ----------

FAMILY_INTENT = "intent"
FAMILY_MAPPING = "mapping"
FAMILY_EXPLICIT = "explicit"
FAMILY_COLUMN = "column"
FAMILY_GRAIN = "grain"
FAMILY_FALLBACK = "fallback"


# ---------- 条件原语 ----------

#: 文本里出现这个短语即成立。
COND_KEYWORD = "keyword"
#: 这个短语前面不远处有指令词（改用/换成/用…）即成立 —— 用户**明确指定**。
COND_DIRECTIVE_KEYWORD = "directive_keyword"
#: 这个短语被否定（不要/不用/别用）即成立 —— 「不要柱状图」不算「用柱状图」。
COND_NEGATED_KEYWORD = "negated_keyword"
#: 规则建议的组件类型当前**不可渲染**即成立。
COND_TYPE_NOT_RENDERABLE = "type_not_renderable"
#: 模型的候选组件类型等于这个值即成立。
COND_MODEL_TYPE_IS = "model_type_is"
#: 模型的候选组件类型不等于这个值即成立。
COND_MODEL_TYPE_IS_NOT = "model_type_is_not"
#: 这块板块的展示目的等于这个值即成立。
COND_DISPLAY_INTENT_IS = "display_intent_is"
#: 模型已经声明了栏目即成立（系统不再补）。
COND_MODEL_DECLARED_COLUMNS = "model_declared_columns"
#: 方案已经声明了粒度即成立（用户说过就不推定）。
COND_GRAIN_DECLARED = "grain_declared"
#: 用户在需求里点名了栏目即成立。
COND_COLUMN_HINTS_PRESENT = "column_hints_present"


# ---------- 指令与否定 ----------

#: 指令词：出现在类型词前面不远处，才算「用户明确指定」。
#: 「能耗柱状图」是板块标题，不是指定 —— 这里只管**有没有指令词**。
#:
#: 否定写法由两层挡，两层口径一致：
#:
#: 1. **指令词层**（本正则）：否定词紧挨着指令词时，指令词不成立 ——
#:    ``不要柱状图`` 里的「要」前面是「不」，``别用柱状图`` 里的「用」前面
#:    是「别」，所以它们**不是**指令词。改造前的指令词正则就是这样挡的，
#:    本模块第一版把这两处 lookbehind 漏掉了，导致「不要那种柱状图」里的
#:    「要」被当成了指令词 —— 现已补回。
#: 2. **规则表禁用条件**（``COND_NEGATED_KEYWORD``）：见 ``_negated``。
#:
#: lookbehind 里排除的那几个字（户使适利运）是「这个「用」不是指令词」的
#: 字：``户用`` / ``使用`` / ``适用`` / ``利用`` / ``运用`` 里的「用」单独
#: 不算指令词。注意 ``费用`` / ``重要`` **不在**名单里 —— 那两个字不挡
#: 「用／要」。（``使用`` 本身另有一个指令词写法，``用户`` 的「用」在词首 ——
#: 详见规则库文档 §2「指令词的例外」。）
_DIRECTIVE_RE = re.compile(
    r"(?:改用|改成|换成|做成|画成|变成|呈现为|显示为|表示为|展示为|使用|采用|画"
    r"|(?<![不别户使适利运])用"
    r"|(?<![不别])要"
    r"|以)"
    r"[^，,。；;！!？?\n]{0,4}$"
)

#: 否定写法：这几种写法**不是**明确指定，规则表里对应的规则被禁用。
_NEGATIONS: tuple[str, ...] = ("不要", "不用", "别用", "别要")


# ---------- 句子切分 ----------

#: 句子切分：指令、栏目提示、展示目的都按句子看。
_CLAUSE_SPLIT_RE = re.compile(r"[，,。；;！!？?\n]")


def split_clauses(text: str) -> list[str]:
    """按标点切句，空白句子丢掉。"""
    return [clause.strip() for clause in _CLAUSE_SPLIT_RE.split(text or "") if clause.strip()]


# ---------- 标签 ----------

COMPONENT_TYPE_LABEL: dict[ComponentType, str] = {
    ComponentType.METRIC_CARD: "指标卡",
    ComponentType.LINE_CHART: "折线图",
    ComponentType.BAR_CHART: "柱状图",
    ComponentType.PIE_CHART: "占比图",
    ComponentType.TABLE: "表格",
    ComponentType.TEXT: "文字板块",
}

DISPLAY_INTENT_LABEL: dict[DisplayIntent, str] = {
    DisplayIntent.TOTAL: "看总量",
    DisplayIntent.TREND: "看随时间的变化",
    DisplayIntent.COMPARISON: "比较不同对象",
    DisplayIntent.COMPOSITION: "看构成占比",
    DisplayIntent.RECORDS: "看具体记录",
}


#: 当前**可渲染**的组件类型：选型能给出的图形/表格类型。
#: 纯说明板块（text）是另一回事 —— 它不是「看数据的展示」，不在这里。
RENDERABLE_COMPONENT_TYPES: frozenset[ComponentType] = frozenset(
    {
        ComponentType.METRIC_CARD,
        ComponentType.LINE_CHART,
        ComponentType.BAR_CHART,
        ComponentType.PIE_CHART,
        ComponentType.TABLE,
    }
)


# ---------- 规则的数据形状 ----------

@dataclass(frozen=True)
class RuleCondition:
    """一条适用条件或禁用条件。

    ``kind`` 取值见上面的 ``COND_*``；``value`` 是这个条件作用的取值
    （关键词、组件类型值…）。没有取值的条件只看上下文。
    """

    kind: str
    value: str = ""


@dataclass(frozen=True)
class DisplayRule:
    """一条展示规则。

    - ``rule_id``：**稳定 ID**。规则内容可以改，但只要语义没变，ID 不变 ——
      方案里的命中依据靠它回溯「当时为什么这么选」。
    - ``family``：规则族（见模块文档的表）。
    - ``priority``：**显式优先级**。数字大的先命中；同族内同级时按
      ``rule_id`` 排，结果是确定的。
    - ``applies_when``：适用条件。任一条件成立即适用（空 = 恒适用）。
    - ``suggested_display``：建议展示。机器可用的取值（组件类型值 /
      展示目的值 / 粒度值 / 栏目标签串）。
    - ``disabled_when``：禁用条件。任一条件成立即**不**适用（空 = 不禁用）。
    - ``explain_template``：解释模板。命中后用它渲染给用户看的理由 ——
      理由来自真实命中，不是事后编的。
    - ``reason_template``：短理由模板（进方案的假设行）。
    - ``match_values``：这条规则用来匹配的关键词。
    - ``payload``：族特有的附加数据（栏目模板等）。
    """

    rule_id: str
    family: str
    priority: int
    applies_when: tuple[RuleCondition, ...] = ()
    suggested_display: str = ""
    disabled_when: tuple[RuleCondition, ...] = ()
    explain_template: str = ""
    reason_template: str = ""
    match_values: tuple[str, ...] = ()
    payload: Any = None


@dataclass(frozen=True)
class PresetColumn:
    """预设补齐的**一列**：列名 + 可选的单位与精度。

    单位只给**数值型**列（负载率、耗电量）。名称、状态、时间这类离散列
    没有单位 —— 给它们硬塞单位，表头就会出现莫名其妙的括号。
    """

    label: str
    #: 单位标注（``%`` / ``kWh`` …）。None = 这一列不是数值列，不带单位。
    unit: str | None = None
    #: 显示精度（小数位数）。None = 不限定精度。
    precision: int | None = None

    def format(self) -> dict[str, Any] | None:
        """这一列的展示格式。非数值列没有格式（也就没有单位标注）。"""
        if self.unit is None:
            return None
        out: dict[str, Any] = {"kind": "NUMBER", "unitLabel": self.unit}
        if self.precision is not None:
            out["precision"] = self.precision
        return out


@dataclass(frozen=True)
class RuleHit:
    """一次**真实命中**。理由与依据都从它来，不在别处编。"""

    #: 命中的规则 ID。
    rule_id: str
    family: str
    priority: int
    #: 命中依据：具体是哪个词、哪个前提成立的。
    detail: str
    #: 给用户看的完整理由。
    explain: str
    #: 进方案假设行的短理由。
    reason: str
    #: 建议展示（机器可用的取值）。
    suggested_display: str = ""
    #: 建议展示的机器可读值（组件类型 / 展示目的 / 粒度 / 栏目模板）。
    payload: Any = None


@dataclass(frozen=True)
class RuleContext:
    """规则判定用的上下文。只放真正影响判定的信息。"""

    #: 被检文本（句子 / 板块标题与指标意图 / 请求全文）。
    text: str = ""
    #: 模型的候选组件类型。
    model_component_type: ComponentType | None = None
    #: 这块板块的展示目的（已解析出来的语义）。
    display_intent: DisplayIntent | None = None
    #: 板块标题（拼补齐说明时用）。
    section_title: str = ""
    #: 模型已经声明了栏目。
    has_declared_columns: bool = False
    #: 方案已经声明了粒度。
    has_declared_grain: bool = False
    #: 用户在需求里点名的栏目。
    column_hints: tuple[str, ...] = ()
    #: 当前可渲染的组件类型。
    renderable_component_types: frozenset[ComponentType] = RENDERABLE_COMPONENT_TYPES
    #: 展示目的**是怎么来的**（「关键词「对比」」/「模型给的语义候选」）。
    #: 命中依据要能一路追溯到真实命中，不只是「展示目的=comparison」。
    intent_source: str = ""
    #: 正在判定的关键词（明确指定这一族按关键词逐条判）。
    candidate_keyword: str = ""
    #: 正在判定的那一次出现（-1 = 不限，按关键词的任意一次出现判）。
    candidate_index: int = -1


# ---------- 规则表 ----------
#
# 下面五张表就是**规则库本体**。它们随代码维护，没有数据库、没有后台、
# 没有自动学习。改一条规则 = 改这里的字面量 + 改版本号 + 改文档。


def _keyword_conditions(*keywords: str) -> tuple[RuleCondition, ...]:
    return tuple(RuleCondition(COND_KEYWORD, keyword) for keyword in keywords)


#: 展示目的关键词 → 展示目的。
#:
#: 优先级是显式的：构成 500 > 比较 400 > 趋势 300 > 明细 200 > 总量 100。
#: 命中**多个**展示目的时取优先级高的那个。
#:
#: 禁用条件：纯说明板块（text）不用请求里的话去猜它是哪张图。
INTENT_RULES: tuple[DisplayRule, ...] = (
    DisplayRule(
        rule_id="DR-INTENT-COMPOSITION",
        family=FAMILY_INTENT,
        priority=500,
        applies_when=_keyword_conditions("占比", "构成", "比例", "分布", "组成"),
        suggested_display=DisplayIntent.COMPOSITION.value,
        disabled_when=(RuleCondition(COND_MODEL_TYPE_IS, ComponentType.TEXT.value),),
        explain_template="按展示目的「{intent_label}」判断这块要看什么。",
        reason_template="按展示目的「{intent_label}」",
        match_values=("占比", "构成", "比例", "分布", "组成"),
        payload=DisplayIntent.COMPOSITION,
    ),
    DisplayRule(
        rule_id="DR-INTENT-COMPARISON",
        family=FAMILY_INTENT,
        priority=400,
        applies_when=_keyword_conditions("对比", "比较", "排名", "排行", "排序", "高低"),
        suggested_display=DisplayIntent.COMPARISON.value,
        disabled_when=(RuleCondition(COND_MODEL_TYPE_IS, ComponentType.TEXT.value),),
        explain_template="按展示目的「{intent_label}」判断这块要看什么。",
        reason_template="按展示目的「{intent_label}」",
        match_values=("对比", "比较", "排名", "排行", "排序", "高低"),
        payload=DisplayIntent.COMPARISON,
    ),
    DisplayRule(
        rule_id="DR-INTENT-TREND",
        family=FAMILY_INTENT,
        priority=300,
        applies_when=_keyword_conditions("趋势", "走势", "变化", "随时间", "逐时", "逐日"),
        suggested_display=DisplayIntent.TREND.value,
        disabled_when=(RuleCondition(COND_MODEL_TYPE_IS, ComponentType.TEXT.value),),
        explain_template="按展示目的「{intent_label}」判断这块要看什么。",
        reason_template="按展示目的「{intent_label}」",
        match_values=("趋势", "走势", "变化", "随时间", "逐时", "逐日"),
        payload=DisplayIntent.TREND,
    ),
    DisplayRule(
        rule_id="DR-INTENT-RECORDS",
        family=FAMILY_INTENT,
        priority=200,
        applies_when=_keyword_conditions("列表", "明细", "清单", "记录", "台账", "逐条"),
        suggested_display=DisplayIntent.RECORDS.value,
        disabled_when=(RuleCondition(COND_MODEL_TYPE_IS, ComponentType.TEXT.value),),
        explain_template="按展示目的「{intent_label}」判断这块要看什么。",
        reason_template="按展示目的「{intent_label}」",
        match_values=("列表", "明细", "清单", "记录", "台账", "逐条"),
        payload=DisplayIntent.RECORDS,
    ),
    DisplayRule(
        rule_id="DR-INTENT-TOTAL",
        family=FAMILY_INTENT,
        priority=100,
        applies_when=_keyword_conditions("总量", "总数", "合计", "汇总", "总览"),
        suggested_display=DisplayIntent.TOTAL.value,
        disabled_when=(RuleCondition(COND_MODEL_TYPE_IS, ComponentType.TEXT.value),),
        explain_template="按展示目的「{intent_label}」判断这块要看什么。",
        reason_template="按展示目的「{intent_label}」",
        match_values=("总量", "总数", "合计", "汇总", "总览"),
        payload=DisplayIntent.TOTAL,
    ),
)


#: 展示目的 → 建议的组件类型。
#:
#: 优先级 200（选型的第二档：按展示目的映射）。
#: 禁用条件：建议的类型当前不可渲染 —— 只用现有可渲染的组件类型，
#: 不扩图表库（规格第 9、11 条）。
MAPPING_RULES: tuple[DisplayRule, ...] = (
    DisplayRule(
        rule_id="DR-MAP-TOTAL",
        family=FAMILY_MAPPING,
        priority=200,
        applies_when=(
            RuleCondition(COND_DISPLAY_INTENT_IS, DisplayIntent.TOTAL.value),
        ),
        suggested_display=ComponentType.METRIC_CARD.value,
        disabled_when=(RuleCondition(COND_TYPE_NOT_RENDERABLE),),
        explain_template="按展示目的「{intent_label}」选{component_label}。",
        reason_template="按展示目的「{intent_label}」",
        match_values=(DisplayIntent.TOTAL.value,),
        payload=DisplayIntent.TOTAL,
    ),
    DisplayRule(
        rule_id="DR-MAP-TREND",
        family=FAMILY_MAPPING,
        priority=200,
        applies_when=(
            RuleCondition(COND_DISPLAY_INTENT_IS, DisplayIntent.TREND.value),
        ),
        suggested_display=ComponentType.LINE_CHART.value,
        disabled_when=(RuleCondition(COND_TYPE_NOT_RENDERABLE),),
        explain_template="按展示目的「{intent_label}」选{component_label}。",
        reason_template="按展示目的「{intent_label}」",
        match_values=(DisplayIntent.TREND.value,),
        payload=DisplayIntent.TREND,
    ),
    DisplayRule(
        rule_id="DR-MAP-COMPARISON",
        family=FAMILY_MAPPING,
        priority=200,
        applies_when=(
            RuleCondition(COND_DISPLAY_INTENT_IS, DisplayIntent.COMPARISON.value),
        ),
        suggested_display=ComponentType.BAR_CHART.value,
        disabled_when=(RuleCondition(COND_TYPE_NOT_RENDERABLE),),
        explain_template="按展示目的「{intent_label}」选{component_label}。",
        reason_template="按展示目的「{intent_label}」",
        match_values=(DisplayIntent.COMPARISON.value,),
        payload=DisplayIntent.COMPARISON,
    ),
    DisplayRule(
        rule_id="DR-MAP-COMPOSITION",
        family=FAMILY_MAPPING,
        priority=200,
        applies_when=(
            RuleCondition(COND_DISPLAY_INTENT_IS, DisplayIntent.COMPOSITION.value),
        ),
        suggested_display=ComponentType.PIE_CHART.value,
        disabled_when=(RuleCondition(COND_TYPE_NOT_RENDERABLE),),
        explain_template="按展示目的「{intent_label}」选{component_label}。",
        reason_template="按展示目的「{intent_label}」",
        match_values=(DisplayIntent.COMPOSITION.value,),
        payload=DisplayIntent.COMPOSITION,
    ),
    DisplayRule(
        rule_id="DR-MAP-RECORDS",
        family=FAMILY_MAPPING,
        priority=200,
        applies_when=(
            RuleCondition(COND_DISPLAY_INTENT_IS, DisplayIntent.RECORDS.value),
        ),
        suggested_display=ComponentType.TABLE.value,
        disabled_when=(RuleCondition(COND_TYPE_NOT_RENDERABLE),),
        explain_template="按展示目的「{intent_label}」选{component_label}。",
        reason_template="按展示目的「{intent_label}」",
        match_values=(DisplayIntent.RECORDS.value,),
        payload=DisplayIntent.RECORDS,
    ),
)


def _explicit_rule(
    rule_id: str,
    keyword: str,
    component_type: ComponentType,
) -> DisplayRule:
    """一条「用户明确指定」的规则：类型词 + 指令词，且没被否定。"""
    return DisplayRule(
        rule_id=rule_id,
        family=FAMILY_EXPLICIT,
        priority=300,
        applies_when=(RuleCondition(COND_DIRECTIVE_KEYWORD, keyword),),
        suggested_display=component_type.value,
        disabled_when=(RuleCondition(COND_NEGATED_KEYWORD, keyword),),
        explain_template="你明确指定用{component_label}。",
        reason_template="你明确指定",
        match_values=(keyword,),
        payload=component_type,
    )


#: 用户明确指定的组件类型：**一个写法一条规则**。
#:
#: 优先级 300（选型的第一档：用户明确指定）。同一句里出现多个类型词时
#: 取**最先出现**的那个 —— 用户先说的那个算数；优先级只在同一位置时用来
#: 区分更具体的写法。
EXPLICIT_TYPE_RULES: tuple[DisplayRule, ...] = (
    _explicit_rule("DR-EXPLICIT-METRIC-CARD", "指标卡", ComponentType.METRIC_CARD),
    _explicit_rule("DR-EXPLICIT-KPI", "kpi", ComponentType.METRIC_CARD),
    _explicit_rule("DR-EXPLICIT-LINE-CHART", "折线图", ComponentType.LINE_CHART),
    _explicit_rule("DR-EXPLICIT-LINE-CURVE", "曲线图", ComponentType.LINE_CHART),
    _explicit_rule("DR-EXPLICIT-TREND-CHART", "趋势图", ComponentType.LINE_CHART),
    _explicit_rule("DR-EXPLICIT-LINE", "折线", ComponentType.LINE_CHART),
    _explicit_rule("DR-EXPLICIT-BAR-CHART", "柱状图", ComponentType.BAR_CHART),
    _explicit_rule("DR-EXPLICIT-BAR", "条形图", ComponentType.BAR_CHART),
    _explicit_rule("DR-EXPLICIT-BAR-SHORT", "柱图", ComponentType.BAR_CHART),
    _explicit_rule("DR-EXPLICIT-DONUT", "环形图", ComponentType.PIE_CHART),
    _explicit_rule("DR-EXPLICIT-PIE", "饼图", ComponentType.PIE_CHART),
    _explicit_rule("DR-EXPLICIT-RATIO-CHART", "占比图", ComponentType.PIE_CHART),
    _explicit_rule("DR-EXPLICIT-TABLE", "表格", ComponentType.TABLE),
)


def _column_rule(
    rule_id: str,
    keywords: tuple[str, ...],
    columns: tuple[PresetColumn, ...],
    priority: int,
) -> DisplayRule:
    """一条栏目补齐规则：板块语义关键词 → 一组列。"""
    labels = "、".join(column.label for column in columns)
    return DisplayRule(
        rule_id=rule_id,
        family=FAMILY_COLUMN,
        priority=priority,
        applies_when=_keyword_conditions(*keywords),
        suggested_display=labels,
        disabled_when=(RuleCondition(COND_MODEL_DECLARED_COLUMNS),),
        explain_template="系统为「{section_title}」补齐了栏目：{column_labels}。",
        reason_template="系统补齐栏目",
        match_values=keywords,
        payload=columns,
    )


#: 表格栏目的系统补齐预设。
#:
#: 第一条来自需求稿 4.1：「告警列表」建议设备名称、时间、告警级别；
#: 第二条来自 ADR 0004 §3：「机柜运行状态」对应成套机柜、状态、负载率。
#:
#: 补出来的列必须是**能被渲染出合理内容**的列（规格 US47）。「机房」曾经
#: 在能耗与设备两类预设里，但造数器没有机房列的值类型，它会落进兜底的
#: 「示例值 69」—— 占位观感的列不算合理的列，所以拿掉。
#:
#: 数值列自带单位（规格《契约缺口补充》A2）：载荷的
#: ``payload.columns[].format.unitLabel`` 要有单位，表头才读得出量纲。
#:
#: 优先级是显式的：告警 400 > 机柜 300 > 能耗 200 > 设备 100。
COLUMN_RULES: tuple[DisplayRule, ...] = (
    _column_rule(
        "DR-COLUMN-ALARM",
        ("告警", "alarm"),
        (
            PresetColumn("设备名称"),
            PresetColumn("时间"),
            PresetColumn("告警级别"),
        ),
        400,
    ),
    _column_rule(
        "DR-COLUMN-CABINET",
        ("机柜", "cabinet"),
        (
            PresetColumn("机柜名称"),
            PresetColumn("状态"),
            PresetColumn("负载率", unit="%", precision=1),
        ),
        300,
    ),
    _column_rule(
        "DR-COLUMN-ENERGY",
        ("能耗", "耗电", "用电", "energy"),
        (PresetColumn("设备名称"), PresetColumn("耗电量", unit="kWh", precision=1)),
        200,
    ),
    _column_rule(
        "DR-COLUMN-DEVICE",
        ("设备", "device"),
        (PresetColumn("设备名称"), PresetColumn("状态")),
        100,
    ),
)

#: 用户在需求里点名的栏目（「包含设备名称和时间」）。
_COLUMN_HINT_MARKERS: tuple[str, ...] = ("栏目", "字段", "列", "包含", "含有", "要有")

#: 最后一条栏目补齐规则：用户自己点了名，就照用户说的列。
#:
#: 它不是「推断」—— 用户点名的栏目是**用户声明**，不是系统按标题猜的。
COLUMN_HINT_RULE = DisplayRule(
    rule_id="DR-COLUMN-USER-HINT",
    family=FAMILY_COLUMN,
    priority=50,
    applies_when=(RuleCondition(COND_COLUMN_HINTS_PRESENT),),
    suggested_display="用户点名的栏目",
    disabled_when=(RuleCondition(COND_MODEL_DECLARED_COLUMNS),),
    explain_template="系统为「{section_title}」补齐了栏目：{column_labels}。",
    reason_template="系统补齐栏目",
    match_values=(),
    payload=None,
)


def _grain_rule(
    rule_id: str,
    keywords: tuple[str, ...],
    unit: GrainUnit,
    priority: int,
) -> DisplayRule:
    """一条粒度规则：关键词 → 粒度。"""
    return DisplayRule(
        rule_id=rule_id,
        family=FAMILY_GRAIN,
        priority=priority,
        applies_when=_keyword_conditions(*keywords),
        suggested_display=unit.value,
        disabled_when=(RuleCondition(COND_GRAIN_DECLARED),),
        explain_template="时间粒度按 {suggested_display} 处理。",
        reason_template="时间粒度按 {suggested_display} 处理",
        match_values=keywords,
        payload=unit,
    )


#: 时间粒度关键词 → 粒度。与时间范围的单位无关：范围是「最近 7 天」，
#: 粒度是「按天」。
#:
#: 优先级是显式的：分钟 500 > 小时 400 > 按天 300 > 按周 200 > 按月 100。
GRAIN_RULES: tuple[DisplayRule, ...] = (
    _grain_rule("DR-GRAIN-MINUTE", ("分钟", "分粒"), GrainUnit.MINUTE, 500),
    _grain_rule("DR-GRAIN-HOUR", ("小时", "逐时", "时点"), GrainUnit.HOUR, 400),
    _grain_rule("DR-GRAIN-DAY", ("按天", "每天", "天粒", "日粒", "逐日"), GrainUnit.DAY, 300),
    _grain_rule("DR-GRAIN-WEEK", ("按周", "每周", "周粒"), GrainUnit.WEEK, 200),
    _grain_rule("DR-GRAIN-MONTH", ("按月", "每月", "月粒"), GrainUnit.MONTH, 100),
)

#: 粒度的兜底规则：用户没说粒度时按天推定，并把推定写明。
GRAIN_DEFAULT_RULE = DisplayRule(
    rule_id="DR-GRAIN-DEFAULT",
    family=FAMILY_GRAIN,
    priority=0,
    applies_when=(),
    suggested_display=GrainUnit.DAY.value,
    disabled_when=(RuleCondition(COND_GRAIN_DECLARED),),
    explain_template="未指定时间粒度，按 {suggested_display} 处理。",
    reason_template="未指定时间粒度",
    match_values=(),
    payload=GrainUnit.DAY,
)

#: 没有更具体依据时的兜底。
#:
#: - ``DR-FALLBACK-MODEL``：模型原选（优先级 100，选型的第三档）。
#: - ``DR-FALLBACK-TEXT``：纯说明板块保持文字。
#:
#: 两条规则互为禁用条件：模型选了文字板块就不走「模型原选」那条，
#: 反过来也一样。
FALLBACK_RULES: tuple[DisplayRule, ...] = (
    DisplayRule(
        rule_id="DR-FALLBACK-MODEL",
        family=FAMILY_FALLBACK,
        priority=100,
        applies_when=(),
        suggested_display="模型候选的类型",
        disabled_when=(RuleCondition(COND_MODEL_TYPE_IS, ComponentType.TEXT.value),),
        explain_template="沿用模型给出的{component_label}。",
        reason_template="模型给出的选择",
        match_values=(),
        payload=None,
    ),
    DisplayRule(
        rule_id="DR-FALLBACK-TEXT",
        family=FAMILY_FALLBACK,
        priority=100,
        applies_when=(),
        suggested_display=ComponentType.TEXT.value,
        disabled_when=(RuleCondition(COND_MODEL_TYPE_IS_NOT, ComponentType.TEXT.value),),
        explain_template="纯说明板块保持文字。",
        reason_template="纯说明板块",
        match_values=(),
        payload=ComponentType.TEXT,
    ),
)


# ---------- 条件求值 ----------

def _family_rules(family: str) -> tuple[DisplayRule, ...]:
    """按族取规则。**调用时**读模块全局 —— 测试换掉规则表就能模拟
    「规则库升版」，生产代码不用动。"""
    return {
        FAMILY_INTENT: INTENT_RULES,
        FAMILY_MAPPING: MAPPING_RULES,
        FAMILY_EXPLICIT: EXPLICIT_TYPE_RULES,
        FAMILY_COLUMN: COLUMN_RULES,
        FAMILY_GRAIN: GRAIN_RULES,
        FAMILY_FALLBACK: FALLBACK_RULES,
    }[family]


def _as_component_type(value: Any) -> ComponentType | None:
    if isinstance(value, ComponentType):
        return value
    try:
        return ComponentType(str(value))
    except ValueError:
        return None


def _contains(text: str, phrase: str) -> bool:
    """文本里有没有这个词（忽略大小写与空白差异）。"""
    needle = (phrase or "").strip().lower()
    if not needle:
        return False
    return needle in (text or "").lower()


def _keyword_positions(text: str, keyword: str) -> list[int]:
    """这个词在文本里出现的所有位置。"""
    needle = (keyword or "").strip().lower()
    if not needle:
        return []
    haystack = (text or "").lower()
    positions: list[int] = []
    index = haystack.find(needle)
    while index >= 0:
        positions.append(index)
        index = haystack.find(needle, index + 1)
    return positions


def _negated(text: str, keyword: str, index: int = -1) -> bool:
    """这个词在句子里是不是被否定了（「不要柱状图」）。

    ``index`` 给定时只看那一次出现 —— 同一句里「不要柱状图，改用柱状图」
    这种写法，前一次否定不影响后一次。

    口径与指令词层**一致**：否定词紧挨着类型词、且**这一处前面没有别的
    指令词**时才算被否定。所以「不要柱状图」不算明确指定；而「改用不要
    柱状图」这种病态写法仍按它前面的「改用」算 —— 与改造前的指令词正则
    口径相同（那次改造把否定从指令词正则挪到了禁用条件，本模块第一版
    两层口径没对齐，这里补齐）。
    """
    if not keyword:
        return False
    positions = (
        [index] if index >= 0 else _keyword_positions(text, keyword)
    )
    for position in positions:
        prefix = (text or "")[:position]
        if not any(prefix.endswith(negation) for negation in _NEGATIONS):
            continue
        if not _directive_before(text, position):
            return True
    return False


def _directive_before(text: str, index: int) -> bool:
    """类型词前面不远处有没有指令词。"""
    return bool(_DIRECTIVE_RE.search((text or "")[:index]))


def _condition_holds(
    condition: RuleCondition,
    *,
    rule: DisplayRule,
    context: RuleContext,
) -> bool:
    kind = condition.kind
    if kind == COND_KEYWORD:
        return _contains(context.text, condition.value)
    if kind == COND_DIRECTIVE_KEYWORD:
        keyword = condition.value or context.candidate_keyword
        return any(
            _directive_before(context.text, index)
            for index in _keyword_positions(context.text, keyword)
        )
    if kind == COND_NEGATED_KEYWORD:
        keyword = condition.value or context.candidate_keyword
        return _negated(context.text, keyword, context.candidate_index)
    if kind == COND_TYPE_NOT_RENDERABLE:
        target = _as_component_type(rule.suggested_display)
        return target is None or target not in context.renderable_component_types
    if kind == COND_MODEL_TYPE_IS:
        return context.model_component_type == _as_component_type(condition.value)
    if kind == COND_MODEL_TYPE_IS_NOT:
        return context.model_component_type != _as_component_type(condition.value)
    if kind == COND_DISPLAY_INTENT_IS:
        try:
            return context.display_intent == DisplayIntent(condition.value)
        except ValueError:
            return False
    if kind == COND_MODEL_DECLARED_COLUMNS:
        return context.has_declared_columns
    if kind == COND_GRAIN_DECLARED:
        return context.has_declared_grain
    if kind == COND_COLUMN_HINTS_PRESENT:
        return bool(context.column_hints)
    return False


def _rule_disabled(rule: DisplayRule, context: RuleContext) -> bool:
    """禁用条件：任一成立即不适用。"""
    return any(
        _condition_holds(condition, rule=rule, context=context)
        for condition in rule.disabled_when
    )


def _rule_applies(rule: DisplayRule, context: RuleContext) -> bool:
    """适用条件：任一成立即适用（空 = 恒适用）。"""
    if not rule.applies_when:
        return True
    return any(
        _condition_holds(condition, rule=rule, context=context)
        for condition in rule.applies_when
    )


def _by_priority(rules: Sequence[DisplayRule]) -> list[DisplayRule]:
    """按显式优先级排序（数字大的先）。同级按 rule_id 排，结果确定。"""
    return sorted(rules, key=lambda rule: (-rule.priority, rule.rule_id))


def _render(template: str, values: Mapping[str, Any]) -> str:
    """渲染解释模板。模板里写了不存在的占位符会直接报错 —— 不静默吞掉。"""
    return template.format(**values)


def _hit(
    rule: DisplayRule,
    *,
    detail: str,
    explain: str,
    reason: str,
    suggested_display: str,
    payload: Any,
) -> RuleHit:
    return RuleHit(
        rule_id=rule.rule_id,
        family=rule.family,
        priority=rule.priority,
        detail=detail,
        explain=explain,
        reason=reason,
        suggested_display=suggested_display,
        payload=payload,
    )


# ---------- 命中 ----------


def match_display_intent(
    text: str,
    *,
    context: RuleContext | None = None,
) -> RuleHit | None:
    """从一段话里看出展示目的。

    命中的是**优先级最高**的那条展示目的规则；同一段话命中多条时，
    优先级高的赢。纯说明板块不适用这些规则（禁用条件）。
    """
    base = context or RuleContext()
    ctx = replace(base, text=text)
    for rule in _by_priority(_family_rules(FAMILY_INTENT)):
        if _rule_disabled(rule, ctx):
            continue
        if not _rule_applies(rule, ctx):
            continue
        keyword = next(
            (value for value in rule.match_values if _contains(ctx.text, value)),
            "",
        )
        intent = rule.payload
        label = DISPLAY_INTENT_LABEL.get(intent, intent.value if intent else "")
        values = {"intent_label": label, "suggested_display": rule.suggested_display}
        return _hit(
            rule,
            detail=f"关键词「{keyword}」",
            explain=_render(rule.explain_template, values),
            reason=_render(rule.reason_template, values),
            suggested_display=rule.suggested_display,
            payload=intent,
        )
    return None


def match_explicit_component_type(
    clause: str,
    *,
    context: RuleContext | None = None,
) -> RuleHit | None:
    """这句里用户**明确指定**的组件类型。

    判据：类型词前面不远处有指令词（「用柱状图」是明确指定，「能耗柱状图」
    是板块标题），而且没被否定（「不要柱状图」不算）。模型自己选的类型
    不算 —— 那不是用户的指定。

    同一句里出现多个类型词时取**最先出现**的那个；同一位置命中多个写法
    时取**更具体**的那个（「折线图」比「折线」更具体）—— 与改造前
    「长的先匹配」的口径一致。
    """
    base = replace(context or RuleContext(), text=clause)
    candidates: list[tuple[int, int, int, str, DisplayRule, str]] = []
    for rule in _by_priority(_family_rules(FAMILY_EXPLICIT)):
        for keyword in rule.match_values:
            for index in _keyword_positions(clause, keyword):
                # 禁用条件按**这一次出现**判：「不要柱状图」否定的是那一次，
                # 不影响同一句里后面另一次出现的「用柱状图」。
                scoped = replace(base, candidate_keyword=keyword, candidate_index=index)
                if _rule_disabled(rule, scoped):
                    continue
                if not _directive_before(clause, index):
                    continue
                # 位置优先；同位置取更长的写法（更具体）；再同才按规则 ID。
                candidates.append(
                    (index, -len(keyword), -rule.priority, rule.rule_id, rule, keyword)
                )
                break
    if not candidates:
        return None
    _, _, _, _, rule, keyword = min(candidates)
    component_type = rule.payload
    label = COMPONENT_TYPE_LABEL.get(component_type, component_type.value)
    values = {"component_label": label, "suggested_display": rule.suggested_display}
    return _hit(
        rule,
        detail=f"指令「{keyword}」",
        explain=_render(rule.explain_template, values),
        reason=_render(rule.reason_template, values),
        suggested_display=rule.suggested_display,
        payload=component_type,
    )


def explicit_hit_for_type(
    component_type: ComponentType,
    *,
    detail: str = "",
) -> RuleHit | None:
    """给定组件类型，找到「用户明确指定」这一族里对应的那条规则。

    用在「沿用上一版用户明确指定过的类型」这种情形：类型是确定的，但要
    记下是**哪条规则**给的依据 —— 命中依据不能是空白的。

    同一类型有多个写法时（柱状图 / 条形图 / 柱图），取**该类型标签对应**
    的那条规则（标签就是这一类型的正式叫法）—— 与「明确指定」这一族在
    同一位置挑写法时的口径一致，命中依据不会因为字典序而指到一条更冷僻
    的规则上。
    """
    candidates = [
        rule
        for rule in _family_rules(FAMILY_EXPLICIT)
        if rule.payload is component_type
    ]
    if not candidates:
        return None
    label = COMPONENT_TYPE_LABEL.get(component_type, component_type.value)
    rule = next(
        (item for item in candidates if label in item.match_values),
        max(candidates, key=lambda item: (len(item.match_values[0]), item.rule_id)),
    )
    values = {
        "component_label": label,
        "suggested_display": rule.suggested_display,
    }
    return _hit(
        rule,
        detail=detail or f"组件类型={component_type.value}",
        explain=_render(rule.explain_template, values),
        reason=_render(rule.reason_template, values),
        suggested_display=rule.suggested_display,
        payload=component_type,
    )


def match_component_mapping(
    display_intent: DisplayIntent | None,
    *,
    context: RuleContext | None = None,
) -> RuleHit | None:
    """展示目的 → 建议的组件类型。

    禁用条件：建议的类型当前不可渲染 —— 只用现有可渲染的组件类型，
    不扩图表库。
    """
    if display_intent is None:
        return None
    base = context or RuleContext()
    scoped = replace(base, display_intent=display_intent)
    for rule in _by_priority(_family_rules(FAMILY_MAPPING)):
        if not _rule_applies(rule, scoped):
            continue
        if _rule_disabled(rule, scoped):
            return None
        component_type = _as_component_type(rule.suggested_display)
        if component_type is None:
            return None
        label = COMPONENT_TYPE_LABEL.get(component_type, component_type.value)
        intent_label = DISPLAY_INTENT_LABEL.get(display_intent, display_intent.value)
        values = {
            "component_label": label,
            "intent_label": intent_label,
            "suggested_display": rule.suggested_display,
        }
        # 命中依据要能一路追溯：展示目的**怎么来的**（哪个关键词 / 模型候选）
        # 也要写下来，不只写「展示目的=comparison」。
        source = scoped.intent_source
        detail = (
            f"{source}→展示目的={display_intent.value}"
            if source
            else f"展示目的={display_intent.value}"
        )
        return _hit(
            rule,
            detail=detail,
            explain=_render(rule.explain_template, values),
            reason=_render(rule.reason_template, values),
            suggested_display=rule.suggested_display,
            payload=component_type,
        )
    return None


def match_fallback(
    model_component_type: ComponentType,
    *,
    context: RuleContext | None = None,
) -> RuleHit | None:
    """没有更具体依据时的兜底：模型原选 / 纯说明板块。"""
    base = context or RuleContext()
    scoped = replace(base, model_component_type=model_component_type)
    for rule in _by_priority(_family_rules(FAMILY_FALLBACK)):
        if _rule_disabled(rule, scoped):
            continue
        # 兜底规则分两种：模型原选（建议展示是模型给的那个类型）与
        # 纯说明板块（建议展示写死在规则里）。
        component_type = model_component_type if rule.payload is None else rule.payload
        label = COMPONENT_TYPE_LABEL.get(component_type, component_type.value)
        values = {
            "component_label": label,
            "suggested_display": component_type.value,
        }
        return _hit(
            rule,
            detail=f"模型候选={model_component_type.value}",
            explain=_render(rule.explain_template, values),
            reason=_render(rule.reason_template, values),
            suggested_display=component_type.value,
            payload=component_type,
        )
    return None


def select_component_type(
    *,
    explicit_hit: RuleHit | None,
    display_intent: DisplayIntent | None,
    model_component_type: ComponentType,
    context: RuleContext | None = None,
) -> RuleHit:
    """按规则给一个板块定稿组件类型。

    优先级（显式）：**用户明确指定 300 > 按展示目的映射 200 > 模型原选 100**。
    每一层都必须满足规则的前提条件：禁用的规则不参与定稿。
    """
    candidates: list[RuleHit] = []
    if explicit_hit is not None:
        candidates.append(explicit_hit)
    mapping_hit = match_component_mapping(display_intent, context=context)
    if mapping_hit is not None:
        candidates.append(mapping_hit)
    fallback_hit = match_fallback(model_component_type, context=context)
    if fallback_hit is not None:
        candidates.append(fallback_hit)
    if not candidates:  # pragma: no cover - 兜底规则保证至少有一条
        raise ValueError("没有任何规则能定稿组件类型")
    return sorted(candidates, key=lambda hit: (-hit.priority, hit.rule_id))[0]


# ---------- 栏目 ----------

def column_hints_from_request(request_text: str) -> list[str]:
    """用户在需求里点名的栏目（「包含设备名称和时间」）。"""
    hints: list[str] = []
    for clause in split_clauses(request_text):
        for marker in _COLUMN_HINT_MARKERS:
            index = clause.find(marker)
            if index < 0:
                continue
            tail = clause[index + len(marker) :]
            tokens = [
                token.strip()
                for token in re.split(r"[、,，;；/]|和|以及|还有|加上", tail)
                if token.strip()
            ]
            tokens = [token for token in tokens if len(token) <= 12]
            if len(tokens) < 2:
                continue
            for token in tokens:
                if token not in hints:
                    hints.append(token)
            break
    return hints


def match_column_preset(
    text: str,
    *,
    context: RuleContext | None = None,
    request_text: str = "",
) -> RuleHit | None:
    """系统补齐栏目的依据：预设规则 → 用户点名的栏目。

    预设命中的是板块语义关键词；都没有命中时，看用户在需求里点了哪些
    栏目 —— 那是用户自己的声明，不是系统猜的。
    """
    base = context or RuleContext()
    hints = tuple(column_hints_from_request(request_text))
    scoped = replace(base, text=text, column_hints=hints)
    for rule in _by_priority(_family_rules(FAMILY_COLUMN)):
        if _rule_disabled(rule, scoped):
            continue
        if not _rule_applies(rule, scoped):
            continue
        keyword = next(
            (value for value in rule.match_values if _contains(scoped.text, value)),
            "",
        )
        columns = rule.payload
        labels = "、".join(column.label for column in columns)
        values = {
            "section_title": scoped.section_title,
            "column_labels": labels,
            "suggested_display": rule.suggested_display,
        }
        return _hit(
            rule,
            detail=f"板块语义「{keyword}」",
            explain=_render(rule.explain_template, values),
            reason=_render(rule.reason_template, values),
            suggested_display=rule.suggested_display,
            payload=columns,
        )
    # 预设都没命中：用户点名的栏目。
    if not hints:
        return None
    if _rule_disabled(COLUMN_HINT_RULE, scoped):
        return None
    columns = tuple(PresetColumn(label) for label in hints)
    labels = "、".join(column.label for column in columns)
    values = {
        "section_title": scoped.section_title,
        "column_labels": labels,
        "suggested_display": COLUMN_HINT_RULE.suggested_display,
    }
    return _hit(
        COLUMN_HINT_RULE,
        detail=f"用户点名「{labels}」",
        explain=_render(COLUMN_HINT_RULE.explain_template, values),
        reason=_render(COLUMN_HINT_RULE.reason_template, values),
        suggested_display=labels,
        payload=columns,
    )


# ---------- 粒度 ----------

def match_grain(
    text: str,
    *,
    context: RuleContext | None = None,
) -> RuleHit | None:
    """时间粒度：关键词命中哪条规则就是哪个粒度；都没命中按天推定。"""
    base = context or RuleContext()
    scoped = replace(base, text=text)
    rules = _by_priority(_family_rules(FAMILY_GRAIN))
    for rule in rules:
        if _rule_disabled(rule, scoped):
            continue
        if not _rule_applies(rule, scoped):
            continue
        keyword = next(
            (value for value in rule.match_values if _contains(scoped.text, value)),
            "",
        )
        unit = rule.payload
        values = {"suggested_display": rule.suggested_display}
        return _hit(
            rule,
            detail=f"关键词「{keyword}」",
            explain=_render(rule.explain_template, values),
            reason=_render(rule.reason_template, values),
            suggested_display=rule.suggested_display,
            payload=unit,
        )
    if _rule_disabled(GRAIN_DEFAULT_RULE, scoped):
        return None
    values = {"suggested_display": GRAIN_DEFAULT_RULE.suggested_display}
    return _hit(
        GRAIN_DEFAULT_RULE,
        detail="请求里没说粒度",
        explain=_render(GRAIN_DEFAULT_RULE.explain_template, values),
        reason=_render(GRAIN_DEFAULT_RULE.reason_template, values),
        suggested_display=GRAIN_DEFAULT_RULE.suggested_display,
        payload=GRAIN_DEFAULT_RULE.payload,
    )


def all_rules() -> tuple[DisplayRule, ...]:
    """规则库全表。文档与自检脚本用它核对「表里的规则」与「文档里的规则」。"""
    return (
        *_family_rules(FAMILY_INTENT),
        *_family_rules(FAMILY_MAPPING),
        *_family_rules(FAMILY_EXPLICIT),
        *_family_rules(FAMILY_COLUMN),
        COLUMN_HINT_RULE,
        *_family_rules(FAMILY_GRAIN),
        GRAIN_DEFAULT_RULE,
        *_family_rules(FAMILY_FALLBACK),
    )
