"""目录外指标的**候选**（工单 #65）。

工单 #59 落地了「目录外模拟」的**能力层**：语义类别、值形态、显式假设、
澄清选项、稳定模拟身份（``app.services.simulated_field_semantics``）。

本模块把那一层接到**编辑入口**上：用户在编辑态说「增加一个电脑耗电率」
「加个冰箱明细」时，解析不出真实指标标识 —— **不再以「未登记」拒绝**，
而是转成两种候选之一：

- **可澄清**：含义不清（``电脑耗电率`` 到底是耗电量、占比还是功率？），
  给出**最关键的语义区别**让用户拍板；
- **带显式假设的模拟候选**：语义明确但目录里没有（``冰箱明细``），按
  **显式假设**出数，并把「口径待确认」写在明面上。

**不另起一套语义判定**：口径、单位、澄清选项、稳定模拟身份全部复用
``simulated_field_semantics``；选型复用 ``report_rule_library``（工单 #60）。
目录外指标**不静默映射**到「总耗电」这类已登记指标（规格第 6 条），也
**不伪造** ``metricRef`` —— 它只有一个稳定的**模拟身份**（契约表 §3.5）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.schemas.report_plan import ComponentType
from app.services.simulated_field_semantics import (
    SCOPE_METRIC_FAMILY,
    clarification_candidates,
    clarification_question,
    deterministic_scope,
    metric_family_seed_key,
    resolve_metric_semantics,
    simulated_metric_id,
)


# ---------------------------------------------------------------- 候选状态

#: 含义不清：先澄清口径，再出数。
STATUS_AMBIGUOUS = "ambiguous"

#: 语义明确但目录里没有：按**显式假设**出数，标注口径待确认。
STATUS_OUT_OF_CATALOG = "out_of_catalog"


# ---------------------------------------------------------------- 抽指标意图
#
# 「增加一个电脑耗电率」里要新增的指标是「电脑耗电率」。抽的是**用户
# 原话** —— 系统不替用户改写口径（规格第 6 条）。

#: 「要加一个 X」的说法前缀。抽指标意图时先把它剥掉。
_ADD_PREFIXES: tuple[str, ...] = (
    "再加一个", "再加个", "增加一个", "增加个", "补充一个", "补充个",
    "添加一个", "添加个", "新增一个", "新增个",
    "加一个", "加个", "加张", "加条", "加块",
    "新增", "增加", "添加", "补充", "加",
)

#: 指标意图**前面**的量词 —— 不是指标名的一部分。
_LEADING_COUNTERS: tuple[str, ...] = (
    "一个", "一张", "一条", "一块", "一份", "一组", "个", "张", "条", "块",
)

#: 指标意图**后面**的成分词 —— 说明这块是什么，不是指标名的一部分。
_TRAILING_NOISE: tuple[str, ...] = (
    "的指标", "指标", "的板块", "板块", "的卡片", "卡片",
    "的区域", "区域", "的面板", "面板", "的模块", "模块",
    "的部分", "部分", "section", "区域块",
)

#: 引号 / 书名号 —— 用户自己圈出来的名字。
_QUOTE_PAIRS: tuple[tuple[str, str], ...] = (
    ("「", "」"), ("『", "』"), ("《", "》"), ("“", "”"), ('"', '"'), ("'", "'"),
)


def _strip_quotes(text: str) -> str:
    for left, right in _QUOTE_PAIRS:
        if left == right:
            text = text.replace(left, "")
            continue
        if left in text and right in text:
            head, _, tail = text.partition(left)
            _, _, rest = tail.partition(right)
            text = f"{head}{rest}".strip()
    return text


def _strip_trailing_noise(text: str) -> str:
    changed = True
    while changed:
        changed = False
        for noise in _TRAILING_NOISE:
            if text.endswith(noise) and len(text) > len(noise):
                text = text[: -len(noise)].strip()
                changed = True
    return text


def split_metric_intent(
    text: str, *, strip_add_prefix: bool = True,
) -> tuple[str, str]:
    """从一句话里抽出「指标意图」与**可能的澄清回答**。

    ``strip_add_prefix=True`` 时先剥掉「增加一个 / 加个 / 新增」这类
    说法前缀；``False`` 时只做量词与成分词的清理（用于「换成 X」这种
    新值片段）。

    澄清回答跟在意图**后面**（编排层把上一轮的原话与这句回答拼成一句
    再交给规则层）：「增加一个电脑耗电率 耗电量」→ 意图「电脑耗电率」、
    回答「耗电量」。

    抽不出意图时返回 ``("", "")`` —— 抽不出就不猜，宁可让上层照既有
    口径追问。
    """
    value = _strip_quotes(str(text or "").strip())
    value = value.strip(" 　：:，,。.；;！!？?、")
    if strip_add_prefix:
        for prefix in _ADD_PREFIXES:
            if value.startswith(prefix):
                value = value[len(prefix):]
                break
    value = value.strip(" 　：:，,。.；;！!？?、")
    for counter in _LEADING_COUNTERS:
        if value.startswith(counter) and len(value) > len(counter):
            value = value[len(counter):]
            break
    # 意图与回答：中间用空白分开（中文里一句话就是一段）。
    parts = [part for part in re.split(r"[\s　]+", value.strip()) if part]
    head = parts[0] if parts else ""
    answer = " ".join(parts[1:]) if len(parts) > 1 else ""
    head = _strip_trailing_noise(head.strip())
    head = head.strip(" 　：:，,。.；;！!？?、")
    if len(head) < 2:
        return "", ""
    return head, answer


def extract_metric_intent(text: str, *, strip_add_prefix: bool = True) -> str:
    """只要指标意图（不要澄清回答）。"""
    intent, _ = split_metric_intent(text, strip_add_prefix=strip_add_prefix)
    return intent


# ---------------------------------------------------------------- 候选


@dataclass(frozen=True)
class OutOfCatalogCandidate:
    """一个目录外指标的候选（服务端记录的事实）。"""

    #: 用户原话（指标意图）。系统不替用户改写它。
    intent: str
    #: 语义类别（契约表 §3.2 的取值域）。
    kind: str
    #: 单位；空串 = 口径还没确认。
    unit: str
    #: 候选状态：``ambiguous``（含义不清）/ ``out_of_catalog``（按假设出数）。
    status: str
    #: 显式假设说明（口径待确认）。没有就为空串。
    note: str = ""
    #: 澄清选项（含义不清时才有）：``{"question", "candidates"}``。
    clarification: dict[str, Any] | None = None

    @property
    def label(self) -> str:
        """显示名：用户原话。"""
        return self.intent

    @property
    def is_ambiguous(self) -> bool:
        """是不是**含义不清**（要先澄清口径）。"""
        return self.status == STATUS_AMBIGUOUS

    @property
    def clarification_question(self) -> str:
        """澄清那句话：只问最关键的口径区别。"""
        if self.clarification:
            question = str(self.clarification.get("question") or "").strip()
            if question:
                return question
        return clarification_question(self.intent, clarification_candidates(self.intent))

    def assumption_note(self) -> str:
        """这一块按什么**显式假设**出的数（写进区域说明与回复）。"""
        if self.note:
            return self.note
        return "仅模拟展示、业务口径待确认"

    def as_dict(self) -> dict[str, Any]:
        """候选的**可落库**形态（跟着编辑计划走，接受时用同一份口径）。"""
        return {
            "intent": self.intent,
            "kind": self.kind,
            "unit": self.unit,
            "status": self.status,
            "note": self.note,
        }


def candidate_from_dict(raw: Any) -> OutOfCatalogCandidate | None:
    """从落库的候选还原一份（认不出来就返回 ``None``，不硬造）。"""
    if not isinstance(raw, dict):
        return None
    intent = str(raw.get("intent") or "").strip()
    if not intent:
        return None
    status = str(raw.get("status") or "").strip()
    if status not in (STATUS_AMBIGUOUS, STATUS_OUT_OF_CATALOG):
        return None
    return OutOfCatalogCandidate(
        intent=intent,
        kind=str(raw.get("kind") or "generic"),
        unit=str(raw.get("unit") or ""),
        status=status,
        note=str(raw.get("note") or ""),
    )


def _match_clarification_choice(
    candidates: list[dict[str, Any]], answer: str,
) -> dict[str, Any] | None:
    """用户拍板了哪个口径（按**选项显示名**精确命中）。

    命中不到就返回 ``None`` —— 用户没在这几个口径里选，服务器不替他选。
    """
    text = str(answer or "").strip()
    if not text:
        return None
    for item in candidates or []:
        label = str(item.get("label") or "").strip()
        if label and label in text:
            return item
    return None


def resolve_out_of_catalog_candidate(
    intent: str, *, answer: str = "",
) -> OutOfCatalogCandidate | None:
    """把一句指标意图解析成**目录外候选**。

    ``answer`` 是用户对澄清的回答。含义不清的指标（「电脑耗电率」）在
    用户拍板口径之后就不再澄清，按**用户选的那个口径**出数。

    解析不出来（意图太短 / 根本不是指标）返回 ``None`` —— 那时上层照
    既有口径追问，不硬造一个候选。
    """
    text = str(intent or "").strip()
    if len(text) < 2:
        return None
    semantics = resolve_metric_semantics(intent=text)
    if semantics.status not in ("ambiguous", "out_of_catalog"):
        return None
    if semantics.is_ambiguous and semantics.clarification:
        chosen = _match_clarification_choice(
            list(semantics.clarification.get("candidates") or []), answer
        )
        if chosen is not None:
            label = str(chosen.get("label") or "")
            unit = str(chosen.get("unit") or "")
            return OutOfCatalogCandidate(
                intent=text,
                kind=semantics.kind,
                unit=unit,
                status=STATUS_OUT_OF_CATALOG,
                note=(
                    "仅模拟展示、业务口径待确认：按你选的「"
                    + label
                    + ("（" + unit + "）" if unit else "")
                    + "」示例出数。"
                ),
            )
    return OutOfCatalogCandidate(
        intent=text,
        kind=semantics.kind,
        unit=semantics.unit or "",
        status=semantics.status,
        note=semantics.note or "",
        clarification=semantics.clarification,
    )


# ---------------------------------------------------------------- 区域说明


def out_of_catalog_region_reason(candidate: OutOfCatalogCandidate, *, source_mode: str = "simulated") -> str:
    """目录外指标那一块的**区域说明**（模板层与编辑层共用同一句）。

    复用 ``simulated_field_semantics.out_of_catalog_note``：真实目录的
    限制**不得**被说成「模拟渲染做不到」—— 模拟模式下这一块照样出数。

    这里用的是**已经解析好的候选**（用户拍板过口径的按拍板的结果写），
    不重新按意图解析一遍 —— 两处各解析一次，口径就会漂。
    """
    from app.services.simulated_field_semantics import (
        OUT_OF_CATALOG_REASON_PREFIX,
        out_of_catalog_note,
    )

    reason = OUT_OF_CATALOG_REASON_PREFIX + out_of_catalog_note(source_mode)
    if candidate.note:
        reason = f"{reason} {candidate.note}"
    return reason


# ---------------------------------------------------------------- 模拟身份


def simulated_metric_key(section_id: str, index: int, kind: str) -> str:
    """目录外指标的**取值键**（载荷按它取数）。

    与转换层同一套生成规则（契约表 §3.1）：``metric.<语义类别>.<板块>_<序号>``。
    """
    slug = "".join(
        char if char.isalnum() else "_" for char in str(section_id or "")
    ).strip("_").lower() or "section"
    return f"metric.{kind}.{slug}_{int(index) + 1}"


def simulated_metric_binding(section_id: str, index: int, kind: str, unit: str) -> dict[str, Any]:
    """目录外指标在 ``previewSpec.metrics[]`` 里的那一条（描述，不是数值）。

    契约表 §3.1 / §3.5：``bindingKind = "simulated"``、``simulatedId`` 是
    稳定的模拟身份、``metricRef`` **不伪造**。
    """
    metric_key = simulated_metric_key(section_id, index, kind)
    return {
        "metricKey": metric_key,
        "label": "",
        "kind": kind,
        "unit": unit or "",
        "capabilityPending": True,
        "pendingReason": None,
        "bindingKind": "simulated",
        "simulatedId": simulated_metric_id(section_id, index),
        "deterministicScope": deterministic_scope(
            SCOPE_METRIC_FAMILY, metric_family_seed_key(kind, metric_key, unit or "")
        ),
    }


# ---------------------------------------------------------------- 选型
#
# 选型复用工单 #60 的**规则库**：优先级与方案侧一致 ——
# **用户明确指定 300 > 按展示目的映射 200 > 兜底 100**。
# 命中依据来自**真实命中**（规则 ID + 命中细节），不是事后编的。


@dataclass(frozen=True)
class ComponentSelection:
    """一次选型的结果（组件类型 + 真实命中依据）。"""

    componentType: ComponentType
    ruleId: str
    detail: str
    explain: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        """落进编辑计划的选型依据（跟着候选走，接受时可见）。"""
        return {
            "ruleId": self.ruleId,
            "componentType": self.componentType.value,
            "detail": self.detail,
            "explain": self.explain,
        }


def select_component_type(
    text: str, *, fallback: ComponentType = ComponentType.TABLE,
) -> ComponentSelection:
    """按规则库给一块新板块选组件类型。

    ``text`` 是用户的原话。命中依据由**命中的规则**渲染 —— 规则库改了，
    依据跟着变；历史候选里已经记下的依据不会被追溯改写。
    """
    from app.services.report_rule_library import (
        RuleContext,
        match_display_intent,
        match_explicit_component_type,
        select_component_type as _select,
    )

    base = RuleContext(model_component_type=fallback)
    explicit_hit = match_explicit_component_type(text, context=base)
    intent_hit = match_display_intent(text, context=base)
    display_intent = intent_hit.payload if intent_hit else None
    hit = _select(
        explicit_hit=explicit_hit,
        display_intent=display_intent,
        model_component_type=fallback,
        context=RuleContext(
            intent_source=intent_hit.detail if intent_hit else "",
            model_component_type=fallback,
        ),
    )
    return ComponentSelection(
        componentType=hit.payload,
        ruleId=hit.rule_id,
        detail=hit.detail,
        explain=hit.explain,
        reason=hit.reason,
    )


def selection_for_existing_type(
    component_type: ComponentType, detail: str,
) -> ComponentSelection:
    """用户**自己说了**展示形式时，补一条同类型的真实命中依据。

    类型已经定了（不能被系统静默改写），但**命中依据不能是空白** ——
    这里用规则库里「明确指定」那一族里对应类型的规则，把依据记下来。
    """
    from app.services.report_rule_library import explicit_hit_for_type

    hit = explicit_hit_for_type(component_type, detail=detail)
    if hit is None:
        return ComponentSelection(
            componentType=component_type,
            ruleId="",
            detail=detail,
            explain=detail,
            reason=detail,
        )
    return ComponentSelection(
        componentType=component_type,
        ruleId=hit.rule_id,
        detail=hit.detail,
        explain=hit.explain,
        reason=hit.reason,
    )
