"""文字方案的领域服务：把模型给的候选变成可提交的对象。

契约来源：`05. 并行分发/00. 协调与冻结契约.md` 的「A：领域与方案类型」。

    apply_candidate(candidate, previous=None, *, plan_id=None, expected_revision=None)
        -> ReportPlan

规则：

    - **新建**：调用方给 ``plan_id``，``expected_revision`` 为 null，
      revision 从 1 开始。
    - **修改**：基于 ``previous``，必须核对 ``expected_revision``；
      方案 ID 沿用上一版，已有板块 ID 尽量保留，revision 递增。
    - 原对象（上一版方案、候选）都不就地修改。
    - 结果只是一个**可提交对象**：不写库、不调用模型、不生成报表，
      也不决定一次提交是否最终成功。

ID 与版本的归属：模型不分配提交版本，也不改已有板块 ID；
板块 ID 由本模块生成或沿用，所以 ID 不会因为模型换了措辞而漂移。
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.schemas.report_plan import (
    ComponentType,
    DisplayIntent,
    GrainUnit,
    PlanCandidate,
    PlanClarificationRequired,
    PlanColumn,
    PlanColumnsUnresolvedError,
    PlanRevisionConflictError,
    PlanSection,
    PlanSectionCandidate,
    PlanValidationError,
    ReportPlan,
    validate_section_contract,
)


# 板块 ID 的序号后缀：``section_3`` -> 3。用于继续往后发号，
# 而不是把删掉的板块 ID 回收给别的板块。
_SECTION_ID_PATTERN = re.compile(r"^section_(\d+)$")


def _coerce_candidate(candidate: Any) -> PlanCandidate:
    """把候选变成校验过的 ``PlanCandidate``。"""
    if isinstance(candidate, PlanCandidate):
        # 已是校验过的实例 → componentType 必然合法，直接返回（不做 model_dump 往返）。
        return candidate
    if isinstance(candidate, BaseModel):
        candidate = candidate.model_dump()
    if not isinstance(candidate, Mapping):
        raise PlanValidationError(
            "候选方案必须是 PlanCandidate 或映射",
            code="PLAN_CANDIDATE_INVALID",
        )
    payload = dict(candidate)
    # 板块级契约校验（组件类型 / 指标意图）**先于** pydantic：
    # pydantic 的报错只说「sections.0.componentType: ...」，没有板块标题；
    # 先跑这一层才能给出「板块 2「PUE 趋势」的组件类型非法」这种可定位的原因。
    _validate_candidate_sections(payload.get("sections"))
    return PlanCandidate.model_validate(payload)


def _validate_candidate_sections(sections: Any) -> None:
    """逐板块校验组件类型与指标意图。错误信息带板块标识。"""
    if not isinstance(sections, (list, tuple)):
        return
    for index, section in enumerate(sections):
        validate_section_contract(section, index=index)


def _coerce_previous(previous: Any) -> ReportPlan | None:
    """把上一版方案变成校验过的 ``ReportPlan``。"""
    if previous is None:
        return None
    if isinstance(previous, ReportPlan):
        return previous
    if isinstance(previous, BaseModel):
        previous = previous.model_dump()
    if not isinstance(previous, Mapping):
        raise PlanValidationError(
            "上一版方案必须是 ReportPlan 或映射",
            code="PLAN_PREVIOUS_INVALID",
        )
    return ReportPlan.model_validate(dict(previous))


def _next_section_index(section_ids: list[str]) -> int:
    """下一个可用板块序号：延续已有编号，不回收删掉的编号。"""
    highest = 0
    for section_id in section_ids:
        match = _SECTION_ID_PATTERN.match(section_id)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def _resolve_section_id(
    candidate_section: PlanSectionCandidate,
    *,
    previous_ids: list[str],
    previous_ids_by_title: dict[str, list[str]],
    used_ids: set[str],
    counter: int,
) -> tuple[str, int]:
    """决定一个板块最终用哪个 ID。

    顺序：显式且已存在的 ID > 标题唯一匹配的旧板块 > 新生成的 ID。
    """
    resolved: str | None = None

    if candidate_section.id and candidate_section.id in previous_ids:
        resolved = candidate_section.id
    elif not candidate_section.id:
        matches = previous_ids_by_title.get(candidate_section.title) or []
        if len(matches) == 1 and matches[0] not in used_ids:
            resolved = matches[0]

    if resolved is not None:
        if resolved in used_ids:
            raise PlanValidationError(
                f"板块 ID 重复：{resolved}",
                code="PLAN_DUPLICATE_SECTION_ID",
            )
        return resolved, counter

    while f"section_{counter}" in used_ids or f"section_{counter}" in previous_ids:
        counter += 1
    new_id = f"section_{counter}"
    return new_id, counter + 1


def _build_sections(
    candidate_sections: list[PlanSectionCandidate],
    previous_sections: list[PlanSection],
    resolution: PlanResolution,
) -> list[PlanSection]:
    """把候选板块变成带稳定 ID 的板块列表。"""
    previous_ids = [section.id for section in previous_sections]
    previous_ids_by_title: dict[str, list[str]] = {}
    for section in previous_sections:
        previous_ids_by_title.setdefault(section.title, []).append(section.id)
    previous_by_id = {section.id: section for section in previous_sections}

    used_ids: set[str] = set()
    counter = _next_section_index(previous_ids)
    sections: list[PlanSection] = []

    for index, candidate_section in enumerate(candidate_sections):
        section_id, counter = _resolve_section_id(
            candidate_section,
            previous_ids=previous_ids,
            previous_ids_by_title=previous_ids_by_title,
            used_ids=used_ids,
            counter=counter,
        )
        used_ids.add(section_id)
        selection = (
            resolution.selections[index] if index < len(resolution.selections) else None
        )
        component_type = selection.componentType if selection else candidate_section.componentType
        columns: list[PlanColumn] = []
        if component_type == ComponentType.TABLE:
            previous_section = previous_by_id.get(section_id)
            columns = finalize_columns(
                section_id,
                resolution.columns.get(index, ()),
                previous_section.columns if previous_section else [],
            )
        sections.append(
            PlanSection(
                id=section_id,
                title=candidate_section.title,
                metricIntents=list(candidate_section.metricIntents),
                componentType=component_type,
                displayIntent=selection.displayIntent if selection else None,
                explicitComponentType=(
                    selection.explicitComponentType if selection else None
                ),
                selectionReason=selection.selection_reason if selection else None,
                columns=columns,
            )
        )
    return sections


def _build_requirements(candidate: PlanCandidate):
    """复制需求块，避免结果与候选共享同一份对象。"""
    return candidate.requirements.model_copy(deep=True)


def _normalize_title(text: str | None) -> str:
    """标题归一：去掉所有空白，便于比较。"""
    return re.sub(r"\s+", "", text or "")


def _section_title(section: Any) -> str:
    """板块标题：候选对象与原始映射都能取。"""
    if isinstance(section, Mapping):
        return str(section.get("title") or "")
    return str(getattr(section, "title", "") or "")


def drop_unaccepted_suggestion_sections(
    candidate_sections: list[Any],
    suggestions: list[str],
) -> list[Any]:
    """剔除「同时出现在未接受建议里」的板块。

    冻结契约写明「额外建议未被接受前不得直接进入板块」，工单 01 的验收
    也写明「未接受建议不直接纳入」。提示词已经要求模型把用户没要的板块
    放进 ``suggestions``，但那只在模型守规矩时成立 —— 这里由服务端兜一道。

    判定用**完全相同**的标题，不做模糊匹配：模糊匹配会把用户明确要求
    的板块也误删，而少一个用户要的板块比多一个板块更糟。

    板块全被剔除时**不**产出空方案：那是候选本身有问题，抛校验错误让
    这一轮按失败收尾，不伪造一份「没有板块」的方案。

    模型适配层与领域层共用这一条：两边剔的是同一批板块，选型与栏目
    才对得上。
    """
    if not suggestions:
        return candidate_sections
    suggestion_titles = {
        _normalize_title(item) for item in suggestions if _normalize_title(item)
    }
    if not suggestion_titles:
        return candidate_sections
    kept = [
        section
        for section in candidate_sections
        if _normalize_title(_section_title(section)) not in suggestion_titles
    ]
    if len(kept) == len(candidate_sections):
        return candidate_sections
    if not kept:
        raise PlanValidationError(
            "候选里的板块全部只是未接受的建议，没有真正的板块",
            code="PLAN_ONLY_SUGGESTIONS",
        )
    return kept


# --------------------------------------------------------------------------
# 选型与栏目（20261029 规格「跨工单契约」）
#
# 这一段是**服务端**的确定性规则：模型给语义候选，服务端定稿。
# 模型适配层（report_plan_agent）与领域层（本模块）共用同一份规则，
# 两边不会各判各的。
# --------------------------------------------------------------------------

#: 展示目的 → 组件类型（需求稿 3.3 的对应关系）。
_DISPLAY_INTENT_COMPONENT: dict[DisplayIntent, ComponentType] = {
    DisplayIntent.TOTAL: ComponentType.METRIC_CARD,
    DisplayIntent.TREND: ComponentType.LINE_CHART,
    DisplayIntent.COMPARISON: ComponentType.BAR_CHART,
    DisplayIntent.COMPOSITION: ComponentType.PIE_CHART,
    DisplayIntent.RECORDS: ComponentType.TABLE,
}

_COMPONENT_TYPE_LABEL: dict[ComponentType, str] = {
    ComponentType.METRIC_CARD: "指标卡",
    ComponentType.LINE_CHART: "折线图",
    ComponentType.BAR_CHART: "柱状图",
    ComponentType.PIE_CHART: "占比图",
    ComponentType.TABLE: "表格",
    ComponentType.TEXT: "文字板块",
}

_DISPLAY_INTENT_LABEL: dict[DisplayIntent, str] = {
    DisplayIntent.TOTAL: "看总量",
    DisplayIntent.TREND: "看随时间的变化",
    DisplayIntent.COMPARISON: "比较不同对象",
    DisplayIntent.COMPOSITION: "看构成占比",
    DisplayIntent.RECORDS: "看具体记录",
}

#: 服务端自己写的假设统一用这些前缀开头。下一版方案里按前缀整条替换，
#: 不跟模型写的假设混在一起，也不越滚越多。
SELECTION_REASON_PREFIX = "选型："
COLUMNS_NOTE_PREFIX = "栏目："
GRAIN_NOTE_PREFIX = "粒度："
_AUTO_NOTE_PREFIXES = (SELECTION_REASON_PREFIX, COLUMNS_NOTE_PREFIX, GRAIN_NOTE_PREFIX)

#: 展示目的关键词 → 展示目的。顺序即优先级：先匹配到的赢。
_DISPLAY_INTENT_KEYWORDS: tuple[tuple[tuple[str, ...], DisplayIntent], ...] = (
    (("占比", "构成", "比例", "分布", "组成"), DisplayIntent.COMPOSITION),
    (("对比", "比较", "排名", "排行", "排序", "高低"), DisplayIntent.COMPARISON),
    (("趋势", "走势", "变化", "随时间", "逐时", "逐日"), DisplayIntent.TREND),
    (("列表", "明细", "清单", "记录", "台账", "逐条"), DisplayIntent.RECORDS),
    (("总量", "总数", "合计", "汇总", "总览"), DisplayIntent.TOTAL),
)

#: 用户明确指定的组件类型关键词（长的先匹配）。
_TYPE_KEYWORDS: tuple[tuple[str, ComponentType], ...] = (
    ("指标卡", ComponentType.METRIC_CARD),
    ("kpi", ComponentType.METRIC_CARD),
    ("折线图", ComponentType.LINE_CHART),
    ("曲线图", ComponentType.LINE_CHART),
    ("趋势图", ComponentType.LINE_CHART),
    ("柱状图", ComponentType.BAR_CHART),
    ("条形图", ComponentType.BAR_CHART),
    ("环形图", ComponentType.PIE_CHART),
    ("饼图", ComponentType.PIE_CHART),
    ("占比图", ComponentType.PIE_CHART),
    ("表格", ComponentType.TABLE),
    ("折线", ComponentType.LINE_CHART),
    ("柱图", ComponentType.BAR_CHART),
)

#: 指令词：出现在类型词前面不远处，才算「用户明确指定」。
#: 「不要柱状图」「用户想…」不算指令，所以有否定与量词的排除。
_DIRECTIVE_RE = re.compile(
    r"(?:改用|改成|换成|做成|画成|变成|呈现为|显示为|表示为|展示为|使用|采用|画"
    r"|(?<![不别户使适利运])用"
    r"|(?<![不别])要"
    r"|以)"
    r"[^，,。；;！!？?\n]{0,4}$"
)

#: 句子切分：指令、栏目提示、展示目的都按句子看。
_CLAUSE_SPLIT_RE = re.compile(r"[，,。；;！!？?\n]")

#: 「第一个」「第 2 块」这类序号指代。
_ORDINAL_RE = re.compile(r"第\s*([一二三四五六七八九十\d]+)\s*(?:个|块|项|条|部分|板块)?")
_ORDINAL_VALUES = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


@dataclass(frozen=True)
class _PresetColumn:
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


#: 表格栏目的补齐模板（按板块标题与指标意图判断）。
#: 第一条来自需求稿 4.1：「告警列表」建议设备名称、时间、告警级别；
#: 第二条来自 ADR 0004 §3：「机柜运行状态」对应成套机柜、状态、负载率。
#:
#: 补出来的列必须是**能被渲染出合理内容**的列（规格 US47）。「机房」曾经
#: 在能耗与设备两类预设里，但造数器没有机房列的值类型，它会落进兜底的
#: 「示例值 69」—— 占位观感的列不算合理的列，所以拿掉。
#:
#: 数值列自带单位（规格《契约缺口补充》A2）：载荷的
#: ``payload.columns[].format.unitLabel`` 要有单位，表头才读得出量纲。
_TABLE_COLUMN_PRESETS: tuple[tuple[tuple[str, ...], tuple[_PresetColumn, ...]], ...] = (
    (
        ("告警", "alarm"),
        (_PresetColumn("设备名称"), _PresetColumn("时间"), _PresetColumn("告警级别")),
    ),
    (
        ("机柜", "cabinet"),
        (
            _PresetColumn("机柜名称"),
            _PresetColumn("状态"),
            _PresetColumn("负载率", unit="%", precision=1),
        ),
    ),
    (
        ("能耗", "耗电", "用电", "energy"),
        (_PresetColumn("设备名称"), _PresetColumn("耗电量", unit="kWh", precision=1)),
    ),
    (
        ("设备", "device"),
        (_PresetColumn("设备名称"), _PresetColumn("状态")),
    ),
)

#: 用户在需求里点名栏目的说法。
_COLUMN_HINT_MARKERS = ("栏目", "字段", "列", "包含", "含有", "要有")

#: 时间粒度关键词 → 粒度。与时间范围的单位无关：范围是「最近 7 天」，
#: 粒度是「按天」。
_GRAIN_KEYWORDS: tuple[tuple[tuple[str, ...], GrainUnit], ...] = (
    (("分钟", "分粒"), GrainUnit.MINUTE),
    (("小时", "逐时", "时点"), GrainUnit.HOUR),
    (("按天", "每天", "天粒", "日粒", "逐日"), GrainUnit.DAY),
    (("按周", "每周", "周粒"), GrainUnit.WEEK),
    (("按月", "每月", "月粒"), GrainUnit.MONTH),
)


# ---------- 解析结果 ----------

@dataclass(frozen=True)
class SectionSelection:
    """一个板块的选型结果（服务端定稿）。"""

    componentType: ComponentType
    displayIntent: DisplayIntent | None
    explicitComponentType: ComponentType | None
    #: 核心理由，进方案假设。
    reason: str
    #: 给用户看的完整理由，进板块的 ``selectionReason``。
    selection_reason: str


@dataclass(frozen=True)
class ColumnSpec:
    """一个待定稿的栏目：模型给的，或系统补的。"""

    label: str
    id: str | None = None
    fieldRef: str | None = None
    #: 列的展示格式（精度、单位标注）。模型没声明就是 None。
    format: dict[str, Any] | None = None


@dataclass(frozen=True)
class ColumnResolution:
    """表格栏目解析结果。

    ``kind`` 取值：

    - ``model``：模型给了可用栏目，照用。
    - ``inferred``：模型没给（或给了坏的），系统补齐了。
    - ``clarification``：补不出来，且猜错会影响这张表用途 → 追问用户。
    - ``failure``：模型给的结构不合法，补齐也失败 → 明确失败允许重试。
    """

    kind: str
    specs: tuple[ColumnSpec, ...] = ()
    note: str | None = None
    question: str | None = None
    code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ExplicitTypeResolution:
    """用户明确指定的组件类型 → 落到哪几块板块。"""

    by_index: dict[int, ComponentType] = field(default_factory=dict)
    question: str | None = None


@dataclass(frozen=True)
class PlanResolution:
    """一轮请求的选型与栏目解析结果。"""

    selections: tuple[SectionSelection, ...] = ()
    columns: dict[int, tuple[ColumnSpec, ...]] = field(default_factory=dict)
    column_notes: dict[int, str] = field(default_factory=dict)
    question: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None


# ---------- 文本工具 ----------

def _split_clauses(text: str) -> list[str]:
    """按标点切句，空白句子丢掉。"""
    return [clause.strip() for clause in _CLAUSE_SPLIT_RE.split(text or "") if clause.strip()]


def _bigrams(text: str) -> set[str]:
    norm = _normalize_title(text)
    return {norm[index : index + 2] for index in range(len(norm) - 1)}


def _mentions(text: str, phrase: str) -> bool:
    """句子里是否提到了这个词（忽略空白）。"""
    return bool(_normalize_title(phrase)) and _normalize_title(phrase) in _normalize_title(text)


def _section_matches_clause(section: Mapping[str, Any], clause: str) -> bool:
    """这句说的是不是这块板块。

    认板块靠三个信号：标题原文出现、指标意图出现、标题与句子的字面重合。
    """
    title = str(section.get("title") or "")
    if title and _normalize_title(title) in _normalize_title(clause):
        return True
    for intent in section.get("metricIntents") or []:
        if not isinstance(intent, str):
            continue
        if len(_normalize_title(intent)) >= 2 and _normalize_title(intent) in _normalize_title(clause):
            return True
    title_bigrams = _bigrams(title)
    clause_bigrams = _bigrams(clause)
    if not title_bigrams or not clause_bigrams:
        return False
    hits = len(title_bigrams & clause_bigrams)
    if hits < 2:
        return False
    return hits * 2 >= len(title_bigrams) or hits * 2 >= len(clause_bigrams)


def _find_ordinal(clause: str) -> int | None:
    match = _ORDINAL_RE.search(clause)
    if not match:
        return None
    token = match.group(1)
    if token.isdigit():
        return int(token)
    return _ORDINAL_VALUES.get(token)


def _find_explicit_type(clause: str) -> ComponentType | None:
    """句子里有没有用户**明确指定**的组件类型。

    只有类型词前面不远处有指令词才算 —— 「用柱状图」是明确指定，
    「能耗柱状图」是板块标题，不是指定。模型自己选的类型不算。
    """
    lowered = clause.lower()
    best: tuple[int, ComponentType] | None = None
    for keyword, component_type in _TYPE_KEYWORDS:
        needle = keyword.lower()
        index = lowered.find(needle)
        while index >= 0:
            if _DIRECTIVE_RE.search(clause[:index]):
                if best is None or index < best[0]:
                    best = (index, component_type)
                break
            index = lowered.find(needle, index + 1)
    return best[1] if best else None


def _infer_intent_from_text(text: str) -> DisplayIntent | None:
    """从一段话里看出展示目的。"""
    lowered = (text or "").lower()
    for keywords, intent in _DISPLAY_INTENT_KEYWORDS:
        for keyword in keywords:
            if keyword in lowered:
                return intent
    return None


#: 「改成 X」这类动词：指的是**已有**的那块板块，不是新加的。
_CHANGE_VERBS = ("改成", "换成", "改为", "修改成", "调整成", "变成")


def _target_pool(
    clause: str,
    sections: Sequence[Mapping[str, Any]],
    previous_sections: Sequence[Mapping[str, Any]],
) -> list[int]:
    """这句指令可能指向的板块范围。

    「改成柱状图」说的是**已有**的那块，所以范围只取上一版就有的板块；
    其余说法（「用柱状图」）可以在全部板块里找。
    """
    if previous_sections and any(verb in clause for verb in _CHANGE_VERBS):
        matched = match_previous_sections(sections, previous_sections)
        if matched:
            return sorted(matched)
    return list(range(len(sections)))


def _resolve_instruction_targets(
    clause: str,
    sections: Sequence[Mapping[str, Any]],
    pool: Sequence[int],
) -> list[int] | str | None:
    """这句指令指向哪几块板块。

    返回下标列表；``"all"`` 表示用户明说「都 / 全部 / 每个」；
    ``None`` 表示这句根本没指向某块板块。
    """
    ordinal = _find_ordinal(clause)
    if ordinal is not None:
        index = ordinal - 1
        return [index] if 0 <= index < len(sections) else None
    if any(_mentions(clause, marker) for marker in ("都", "全部", "每个", "所有")):
        return "all"
    hits = [
        index
        for index in pool
        if _section_matches_clause(sections[index], clause)
    ]
    if hits:
        return hits
    if len(pool) == 1:
        return [pool[0]]
    # 说了「用柱状图」却对不上任何一块板块：这是实质歧义，要问用户，
    # 不能悄悄丢掉用户的指令，也不能套给所有板块。
    return None


def _ambiguous_type_question(
    clause: str, sections: Sequence[Mapping[str, Any]], targets: Sequence[int] | None
) -> str:
    """指令对不上板块时，问的那句具体问题。"""
    if targets:
        titles = [str(sections[index].get("title") or "") for index in targets]
    else:
        titles = [str(section.get("title") or "") for section in sections]
    listed = "、".join(title for title in titles if title) or "（当前方案没有板块）"
    return f"你说的「{clause.strip()}」是指哪一块板块？当前方案的板块有：{listed}。"


def resolve_explicit_component_types(
    sections: Sequence[Mapping[str, Any]],
    *,
    request_text: str = "",
    previous_sections: Sequence[Mapping[str, Any]] = (),
) -> ExplicitTypeResolution:
    """把用户明确指定的组件类型落到**对应**板块上。

    - 只有带指令词的类型词才算「明确指定」；模型自己选的类型不算。
    - 一条指令只覆盖它说清楚的那块板块；对不上就澄清，
      **绝不把一句全局指令套给所有板块**。
    - 用户明说「都 / 全部 / 每个」时才覆盖全部板块。
    """
    if not (request_text or "").strip():
        return ExplicitTypeResolution()

    by_index: dict[int, ComponentType] = {}
    for clause in _split_clauses(request_text):
        component_type = _find_explicit_type(clause)
        if component_type is None:
            continue
        pool = _target_pool(clause, sections, previous_sections)
        targets = _resolve_instruction_targets(clause, sections, pool)
        if targets is None:
            # 指令对不上板块：实质歧义 → 澄清，不偷偷改别的板块。
            return ExplicitTypeResolution(
                question=_ambiguous_type_question(clause, sections, None)
            )
        if targets == "all":
            for index in range(len(sections)):
                by_index[index] = component_type
            continue
        if len(targets) == 1:
            by_index[targets[0]] = component_type
            continue
        return ExplicitTypeResolution(
            question=_ambiguous_type_question(clause, sections, targets)
        )
    return ExplicitTypeResolution(by_index=by_index)


def match_previous_sections(
    candidate_sections: Sequence[Mapping[str, Any]],
    previous_sections: Sequence[Mapping[str, Any]],
) -> dict[int, int]:
    """候选板块下标 → 上一版板块下标。

    认人规则与板块 ID 的归属一致：显式且已存在的 id > 标题唯一匹配。
    """
    previous_ids = [str(section.get("id") or "") for section in previous_sections]
    by_title: dict[str, list[int]] = {}
    for index, section in enumerate(previous_sections):
        by_title.setdefault(str(section.get("title") or ""), []).append(index)

    used: set[int] = set()
    matched: dict[int, int] = {}
    for index, section in enumerate(candidate_sections):
        section_id = str(section.get("id") or "").strip()
        title = str(section.get("title") or "")
        found: int | None = None
        if section_id and section_id in previous_ids:
            position = previous_ids.index(section_id)
            if position not in used:
                found = position
        elif not section_id:
            matches = by_title.get(title) or []
            if len(matches) == 1 and matches[0] not in used:
                found = matches[0]
        if found is not None:
            used.add(found)
            matched[index] = found
    return matched


def _resolve_display_intent(
    section: Mapping[str, Any],
    *,
    sections: Sequence[Mapping[str, Any]],
    request_text: str,
) -> DisplayIntent | None:
    """这块板块要看什么。

    顺序：模型给的语义候选 → 请求里点到这块板块的那句话 → 整份方案只有
    一块板块时请求里唯一的展示目的。都看不出来就是 null（此时沿用模型
    选的组件类型）。
    """
    declared = section.get("displayIntent")
    if isinstance(declared, str) and declared.strip():
        try:
            return DisplayIntent(declared.strip())
        except ValueError:
            pass

    raw_type = section.get("componentType")
    try:
        model_type = ComponentType(str(getattr(raw_type, "value", raw_type)))
    except ValueError:
        model_type = ComponentType.TEXT
    if model_type == ComponentType.TEXT:
        # 纯说明板块没有展示目的：不要拿请求里的话去猜它是哪张图。
        return None

    clauses = _split_clauses(request_text)
    mentioned = [
        clause
        for clause in clauses
        if _section_matches_clause(section, clause)
        and sum(1 for other in sections if _section_matches_clause(other, clause)) == 1
    ]
    for clause in mentioned:
        intent = _infer_intent_from_text(clause)
        if intent is not None:
            return intent

    if len(sections) == 1:
        intents = {
            intent
            for intent in (_infer_intent_from_text(clause) for clause in clauses)
            if intent is not None
        }
        if len(intents) == 1:
            return intents.pop()
    return None


def _select_component_type(
    *,
    explicit: ComponentType | None,
    display_intent: DisplayIntent | None,
    model_type: ComponentType,
) -> tuple[ComponentType, str, str]:
    """定稿组件类型，返回 (类型, 核心理由, 完整理由)。

    优先级：**用户明确指定且合法 > 按展示目的映射 > 模型原选**。
    """
    if explicit is not None:
        label = _COMPONENT_TYPE_LABEL[explicit]
        return explicit, "你明确指定", f"你明确指定用{label}。"
    if display_intent is not None:
        mapped = _DISPLAY_INTENT_COMPONENT.get(display_intent)
        if mapped is not None:
            intent_label = _DISPLAY_INTENT_LABEL[display_intent]
            return (
                mapped,
                f"按展示目的「{intent_label}」",
                f"按展示目的「{intent_label}」选{_COMPONENT_TYPE_LABEL[mapped]}。",
            )
    if model_type == ComponentType.TEXT:
        # 没有展示目的的板块保持纯说明：模型说了它是文字板块。
        return ComponentType.TEXT, "纯说明板块", "纯说明板块保持文字。"
    return (
        model_type,
        "模型给出的选择",
        f"沿用模型给出的{_COMPONENT_TYPE_LABEL[model_type]}。",
    )


def _column_hints_from_request(request_text: str) -> list[str]:
    """用户在需求里点名的栏目（「包含设备名称和时间」）。"""
    hints: list[str] = []
    for clause in _split_clauses(request_text):
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


def _infer_columns(
    section: Mapping[str, Any], *, request_text: str = ""
) -> list[_PresetColumn]:
    """系统补齐栏目：先按板块语义，再按用户点名的栏目。

    预设的列自带单位（数值列）；用户点名的列**不知道是什么量**，一律
    不带单位 —— 不写错的单位。
    """
    title = str(section.get("title") or "")
    intents = [
        str(intent)
        for intent in section.get("metricIntents") or []
        if isinstance(intent, str)
    ]
    haystack = " ".join([title, *intents]).lower()
    for keywords, columns in _TABLE_COLUMN_PRESETS:
        if any(keyword in haystack for keyword in keywords):
            return list(columns)
    return [_PresetColumn(label) for label in _column_hints_from_request(request_text)]


def normalize_column_format(raw: Any) -> dict[str, Any] | None:
    """栏目声明的**展示格式**：只认已知键，认不出的丢掉。

    未知键不报错 —— 格式是可选的装饰，认不出就不给单位标注，不因为一个
    多写的键把整轮方案作废。
    """
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, Any] = {}
    kind = raw.get("kind")
    if isinstance(kind, str) and kind.strip():
        out["kind"] = kind.strip()
    precision = raw.get("precision")
    if isinstance(precision, int) and not isinstance(precision, bool) and precision >= 0:
        out["precision"] = precision
    unit = raw.get("unitLabel")
    if isinstance(unit, str) and unit.strip():
        out["unitLabel"] = unit.strip()
    return out or None


def resolve_table_columns(
    section: Mapping[str, Any], *, request_text: str = ""
) -> ColumnResolution:
    """表格板块的栏目：模型给了就用，没给就系统补，补不上才追问或报错。

    「没有真实数据」永远不是追问的理由 —— 这里只认**结构**。
    """
    title = str(section.get("title") or "") or "这张表"
    raw_columns = section.get("columns")
    declared = raw_columns is not None

    if declared and not isinstance(raw_columns, (list, tuple)):
        return ColumnResolution(
            kind="failure",
            code="PLAN_COLUMNS_UNRESOLVED",
            message=f"板块「{title}」的栏目结构不合法，方案生成失败，请重试。",
        )

    specs: list[ColumnSpec] = []
    malformed = False
    for item in raw_columns or []:
        if not isinstance(item, Mapping):
            malformed = True
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            malformed = True
            continue
        specs.append(
            ColumnSpec(
                label=label,
                id=str(item.get("id")).strip() if isinstance(item.get("id"), str) else None,
                fieldRef=(
                    str(item.get("fieldRef")).strip()
                    if isinstance(item.get("fieldRef"), str)
                    else None
                ),
                format=normalize_column_format(item.get("format")),
            )
        )
    if specs:
        return ColumnResolution(kind="model", specs=tuple(specs))

    if malformed:
        # 模型写了栏目但结构是坏的：这是技术错误，不能变成「请你设计这张表」，
        # 更不能**悄悄换成系统补齐的栏目** —— 用户以为模型给的就是他看到的。
        # 判定放在「系统补齐」之前：模型给的栏目结构坏了就该明确报失败，
        # 让用户重试。
        return ColumnResolution(
            kind="failure",
            code="PLAN_COLUMNS_UNRESOLVED",
            message=f"板块「{title}」的栏目无法解析，方案生成失败，请重试。",
        )

    inferred = _infer_columns(section, request_text=request_text)
    if inferred:
        note = f"系统为「{title}」补齐了栏目：{'、'.join(c.label for c in inferred)}。"
        return ColumnResolution(
            kind="inferred",
            specs=tuple(
                ColumnSpec(label=c.label, format=c.format()) for c in inferred
            ),
            note=note,
        )

    return ColumnResolution(
        kind="clarification",
        question=(
            f"「{title}」这张表要展示哪些栏目？"
            "说清楚列名就行（例如「设备名称、时间、告警级别」）。"
        ),
    )


def _previous_explicit_type(previous: Mapping[str, Any]) -> ComponentType | None:
    raw = previous.get("explicitComponentType")
    if not raw:
        return None
    try:
        return ComponentType(str(getattr(raw, "value", raw)).strip())
    except ValueError:
        return None


def resolve_plan(
    candidate_sections: Sequence[Mapping[str, Any]],
    *,
    request_text: str = "",
    previous_sections: Sequence[Mapping[str, Any]] = (),
) -> PlanResolution:
    """把一轮请求的选型与栏目一次性解析出来。

    模型适配层用它决定「要不要追问用户」，领域层用它定稿方案。
    两边共用同一份规则，结果必然一致。
    """
    sections = [dict(section) for section in candidate_sections]
    if not sections:
        return PlanResolution()

    explicit = resolve_explicit_component_types(
        sections,
        request_text=request_text,
        previous_sections=previous_sections,
    )
    if explicit.question:
        return PlanResolution(question=explicit.question)

    previous_by_index = match_previous_sections(sections, previous_sections)

    selections: list[SectionSelection] = []
    columns: dict[int, tuple[ColumnSpec, ...]] = {}
    notes: dict[int, str] = {}

    for index, section in enumerate(sections):
        previous = (
            previous_sections[previous_by_index[index]]
            if index in previous_by_index
            else None
        )
        explicit_type = explicit.by_index.get(index)
        if explicit_type is None and previous is not None:
            # 修改未涉及的板块：保留上一版用户明确指定过的类型。
            explicit_type = _previous_explicit_type(previous)

        raw_model_type = section.get("componentType")
        try:
            model_type = ComponentType(str(getattr(raw_model_type, "value", raw_model_type)))
        except ValueError:
            model_type = ComponentType.TEXT

        display_intent = _resolve_display_intent(
            section, sections=sections, request_text=request_text
        )
        component_type, reason, selection_reason = _select_component_type(
            explicit=explicit_type,
            display_intent=display_intent,
            model_type=model_type,
        )
        selections.append(
            SectionSelection(
                componentType=component_type,
                displayIntent=display_intent,
                explicitComponentType=explicit_type,
                reason=reason,
                selection_reason=selection_reason,
            )
        )

        if component_type != ComponentType.TABLE:
            continue
        resolution = resolve_table_columns(section, request_text=request_text)
        if resolution.kind == "clarification":
            return PlanResolution(question=resolution.question)
        if resolution.kind == "failure":
            return PlanResolution(
                failure_code=resolution.code or "PLAN_COLUMNS_UNRESOLVED",
                failure_message=resolution.message or "方案生成失败，请重试。",
            )
        columns[index] = resolution.specs
        if resolution.note:
            notes[index] = resolution.note

    return PlanResolution(
        selections=tuple(selections),
        columns=columns,
        column_notes=notes,
    )


# ---------- 栏目定稿 ----------

def _next_column_id(used_ids: set[str]) -> str:
    index = 1
    while f"col_{index}" in used_ids:
        index += 1
    return f"col_{index}"


def _make_field_ref(section_id: str, column_id: str, used_refs: set[str]) -> str:
    """取值键：按板块 ID 与栏目 ID 确定性生成。"""
    base = f"{section_id}.{column_id}"
    candidate = base
    index = 2
    while candidate in used_refs:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def _match_previous_column(
    spec: ColumnSpec,
    previous_by_id: Mapping[str, PlanColumn],
    previous_by_label: Mapping[str, list[PlanColumn]],
    used_ids: set[str],
) -> PlanColumn | None:
    """认出一个栏目是不是上一版的那一个。

    顺序：显式 id > 显示名唯一匹配。改显示名时靠 id 认人（模型在修改方案
    时会沿用已有栏目 id），所以**改列名不改变栏目身份**。
    """
    if spec.id and spec.id in previous_by_id:
        matched = previous_by_id[spec.id]
        if matched.id not in used_ids:
            return matched
    matches = previous_by_label.get(spec.label) or []
    if len(matches) == 1 and matches[0].id not in used_ids:
        return matches[0]
    return None


def finalize_columns(
    section_id: str,
    specs: Sequence[ColumnSpec],
    previous: Sequence[PlanColumn] = (),
) -> list[PlanColumn]:
    """把栏目定稿：稳定 id + 显示名 + 行对象取值键。

    - 已有栏目的 id 与取值键保留（改显示名、重排都不影响身份）。
    - 新栏目按板块 ID 与栏目 ID 确定性生成取值键。
    - 取值键重复必须修正；显示名可以重复，取值键不能重复。
    """
    previous_by_id = {column.id: column for column in previous}
    previous_by_label: dict[str, list[PlanColumn]] = {}
    for column in previous:
        previous_by_label.setdefault(column.label, []).append(column)

    used_ids: set[str] = set()
    used_refs: set[str] = set()
    columns: list[PlanColumn] = []

    for spec in specs:
        matched = _match_previous_column(
            spec, previous_by_id, previous_by_label, used_ids
        )
        if matched is not None:
            column_id = matched.id
            field_ref = matched.fieldRef
            label = spec.label
        else:
            column_id = (
                spec.id
                if spec.id and spec.id not in used_ids
                else _next_column_id(used_ids)
            )
            field_ref = spec.fieldRef
            label = spec.label
        if not field_ref or field_ref in used_refs:
            field_ref = _make_field_ref(section_id, column_id, used_refs)
        used_ids.add(column_id)
        used_refs.add(field_ref)
        # 格式跟着栏目走：模型这次声明了就用这次的；没声明就沿用上一版的
        # （改列名不换身份，单位标注也不该跟着改）。
        column_format = spec.format
        if column_format is None and matched is not None:
            column_format = (
                matched.format.model_dump(mode="json") if matched.format else None
            )
        columns.append(
            PlanColumn(
                id=column_id, label=label, fieldRef=field_ref, format=column_format
            )
        )

    return columns


def infer_grain_unit(
    *, declared: str | None = None, request_text: str = ""
) -> tuple[GrainUnit, str | None]:
    """时间粒度：用户说了就用，用户没说就推定，并把推定写明。

    返回 (粒度, 假设文案)。文案为 None 表示用户明确说过，不用写假设。
    """
    if declared:
        try:
            return GrainUnit(declared.strip().upper()), None
        except ValueError:
            pass
    for keywords, unit in _GRAIN_KEYWORDS:
        for keyword in keywords:
            if keyword in (request_text or ""):
                return unit, f"时间粒度按 {unit.value} 处理。"
    return GrainUnit.DAY, f"未指定时间粒度，按 {GrainUnit.DAY.value} 处理。"


def _request_text(original_request: str | None, current_request: str | None) -> str:
    """这一轮能看到的用户原话。

    当前轮的话在前：新一轮的指令覆盖旧指令。没给当前轮就用原始请求。
    """
    parts: list[str] = []
    for text in (current_request, original_request):
        if text and text.strip() and text not in parts:
            parts.append(text)
    return "\n".join(parts)


def _build_assumptions(
    candidate_assumptions: list[str],
    sections: list[PlanSection],
    resolution: PlanResolution,
    grain_note: str | None,
) -> list[str]:
    """方案的假设：模型写的原样留着，服务端自己写的按板块重新生成。

    服务端写的假设带固定前缀，下一版方案里按前缀整条替换 ——
    不会越滚越多，也不会和模型写的混在一起。
    """
    kept = [
        text
        for text in candidate_assumptions
        if not text.startswith(SELECTION_REASON_PREFIX)
        and not text.startswith(GRAIN_NOTE_PREFIX)
    ]
    for section, selection in zip(sections, resolution.selections):
        label = _COMPONENT_TYPE_LABEL[selection.componentType]
        kept.append(
            f"{SELECTION_REASON_PREFIX}「{section.title}」采用{label}（{selection.reason}）。"
        )
    for index in sorted(resolution.column_notes):
        kept.append(f"{COLUMNS_NOTE_PREFIX}{resolution.column_notes[index]}")
    if grain_note:
        kept.append(f"{GRAIN_NOTE_PREFIX}{grain_note}")

    unique: list[str] = []
    for text in kept:
        if text not in unique:
            unique.append(text)
    return unique


def apply_candidate(
    candidate: PlanCandidate | Mapping[str, Any],
    previous: ReportPlan | Mapping[str, Any] | None = None,
    *,
    plan_id: str | None = None,
    expected_revision: int | None = None,
    original_request: str | None = None,
    current_request: str | None = None,
) -> ReportPlan:
    """把候选方案变成一份可提交的 ``ReportPlan``。

    新建（``previous`` 为 None）：
        调用方必须给 ``plan_id``，``expected_revision`` 必须为 null，
        revision 从 1 开始，板块 ID 全部由本模块生成。

    修改（``previous`` 不为 None）：
        ``expected_revision`` 必须与上一版的 revision 一致，否则抛版本冲突；
        方案 ID 沿用上一版，已有板块 ID 保留，revision 递增。

    方案服务要拿到三样输入（20261029 规格「跨工单契约」）：

    - ``original_request``：原始请求。
    - ``current_request``：当前这一轮的修改。
    - ``previous``：已有方案。

    有了这三样，服务端才能逐板块判断「用户明确指定的是哪一块」，
    也能把缺的栏目补齐或问清楚。

    异常：
        - ``PlanValidationError``：候选结构非法，或调用方式不对。
        - ``PlanContentRejectedError``：候选里混进了执行性内容。
        - ``PlanRevisionConflictError``：基础版本与当前方案不一致。
        - ``PlanClarificationRequired``：有一处必须由用户拍板（``question``
          就是给用户的那句具体问题，编排层据此走澄清追问通道）。
        - ``PlanColumnsUnresolvedError``：表格栏目修不了，明确失败允许重试。
    """
    candidate_plan = _coerce_candidate(candidate)
    previous_plan = _coerce_previous(previous)
    request_text = _request_text(original_request, current_request)

    # 调用方式先对不对（版本与方案 ID），再看内容对不对：
    # 版本冲突要报成版本冲突，不能被一句澄清问题盖过去。
    if previous_plan is None:
        if expected_revision is not None:
            raise PlanValidationError(
                "新建方案时 expected_revision 必须为 null",
                code="PLAN_EXPECTED_REVISION_INVALID",
            )
        if plan_id is None or not str(plan_id).strip():
            raise PlanValidationError(
                "新建方案必须由调用方提供 plan_id",
                code="PLAN_ID_REQUIRED",
            )
    else:
        if expected_revision is None:
            raise PlanValidationError(
                "修改方案必须提供 expected_revision",
                code="PLAN_EXPECTED_REVISION_REQUIRED",
            )
        if expected_revision != previous_plan.revision:
            raise PlanRevisionConflictError(
                f"方案版本冲突：期望 revision {expected_revision}，"
                f"当前方案是 revision {previous_plan.revision}",
                expected_revision=expected_revision,
                actual_revision=previous_plan.revision,
            )
        if plan_id is not None and str(plan_id).strip() != previous_plan.id:
            raise PlanValidationError(
                f"修改方案时 plan_id 必须沿用原方案：{previous_plan.id}",
                code="PLAN_ID_MISMATCH",
            )

    # 未接受的建议先剔掉：后面选型与栏目都按**留下来的**板块算。
    kept_sections = drop_unaccepted_suggestion_sections(
        list(candidate_plan.sections), list(candidate_plan.suggestions)
    )
    previous_sections = list(previous_plan.sections) if previous_plan else []

    resolution = resolve_plan(
        [section.model_dump() for section in kept_sections],
        request_text=request_text,
        previous_sections=[section.model_dump() for section in previous_sections],
    )
    if resolution.question:
        raise PlanClarificationRequired(
            resolution.question, question=resolution.question
        )
    if resolution.failure_code:
        raise PlanColumnsUnresolvedError(
            resolution.failure_message or "方案生成失败，请重试。",
            code=resolution.failure_code,
        )

    grain_unit, grain_note = infer_grain_unit(
        declared=(
            candidate_plan.requirements.grainUnit.value
            if candidate_plan.requirements.grainUnit
            else None
        ),
        request_text=request_text,
    )
    requirements = _build_requirements(candidate_plan)
    requirements.grainUnit = grain_unit

    if previous_plan is None:
        sections = _build_sections(kept_sections, [], resolution)
        return ReportPlan(
            id=str(plan_id).strip(),
            revision=1,
            topic=candidate_plan.topic,
            requirements=requirements,
            sections=sections,
            assumptions=_build_assumptions(
                list(candidate_plan.assumptions), sections, resolution, grain_note
            ),
            suggestions=list(candidate_plan.suggestions),
            referenceTemplates=[
                template.model_copy(deep=True)
                for template in candidate_plan.referenceTemplates
            ],
        )

    sections = _build_sections(kept_sections, previous_sections, resolution)
    return ReportPlan(
        id=previous_plan.id,
        revision=previous_plan.revision + 1,
        topic=candidate_plan.topic,
        requirements=requirements,
        sections=sections,
        assumptions=_build_assumptions(
            list(candidate_plan.assumptions), sections, resolution, grain_note
        ),
        suggestions=list(candidate_plan.suggestions),
        referenceTemplates=[
            template.model_copy(deep=True)
            for template in candidate_plan.referenceTemplates
        ],
    )
