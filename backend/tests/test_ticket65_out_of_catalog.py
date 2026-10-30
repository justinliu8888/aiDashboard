"""工单 65 / GitHub #65：目录外指标在编辑入口的**候选**解析。

这一票把 #59 的「目录外模拟」能力层接到编辑入口上：解析不出真实指标
标识时**不再以「未登记」拒绝**，而是转成可澄清或带显式假设的模拟候选。

本文件测的是候选层本身（意图抽取、语义判定、澄清回答、选型依据）；
编辑引擎与接口层的用例见 ``test_ticket65_out_of_catalog_edit.py``。
"""
from __future__ import annotations

from app.schemas.report_plan import ComponentType
from app.services.out_of_catalog_candidate import (
    STATUS_AMBIGUOUS,
    STATUS_OUT_OF_CATALOG,
    extract_metric_intent,
    resolve_out_of_catalog_candidate,
    select_component_type,
    selection_for_existing_type,
    simulated_metric_id,
    split_metric_intent,
)


# --------------------------------------------------------------------------
# 1. 指标意图抽取：用户原话，不替用户改写口径
# --------------------------------------------------------------------------


def test_extract_metric_intent_keeps_the_users_own_words():
    """抽的是用户原话（「电脑耗电率」），系统不静默改成「总耗电」。"""
    assert extract_metric_intent("增加一个电脑耗电率") == "电脑耗电率"
    assert extract_metric_intent("加个冰箱明细") == "冰箱明细"
    assert extract_metric_intent("新增一个设备温度指标") == "设备温度"


def test_extract_metric_intent_strips_counters_and_noise_words():
    """量词与「指标 / 板块」这类成分词不是指标名的一部分。"""
    assert extract_metric_intent("加一个总耗电板块") == "总耗电"
    assert extract_metric_intent("新增一个设备温度指标") == "设备温度"


def test_extract_metric_intent_returns_empty_when_there_is_none():
    """抽不出就不猜 —— 返回空串，让上层照既有口径追问。"""
    assert extract_metric_intent("加个") == ""
    assert extract_metric_intent("") == ""


def test_split_metric_intent_separates_the_clarification_answer():
    """澄清回答跟在意图后面（编排层把原话与回答拼成一句再交给规则层）。"""
    intent, answer = split_metric_intent("增加一个电脑耗电率 耗电量")

    assert intent == "电脑耗电率"
    assert answer == "耗电量"


# --------------------------------------------------------------------------
# 2. 候选：含义不清 → 可澄清；语义明确 → 带显式假设
# --------------------------------------------------------------------------


def test_ambiguous_intent_becomes_a_clarifiable_candidate():
    """「电脑耗电率」口径不清 → 给最关键的语义区别，不替他选一个。"""
    candidate = resolve_out_of_catalog_candidate("电脑耗电率")

    assert candidate is not None
    assert candidate.status == STATUS_AMBIGUOUS
    assert candidate.is_ambiguous is True
    assert candidate.intent == "电脑耗电率"
    question = candidate.clarification_question
    assert "电脑耗电率" in question
    assert "耗电量" in question and "占比" in question and "功率" in question


def test_clear_out_of_catalog_intent_becomes_an_explicit_assumption():
    """「冰箱明细」语义明确但目录里没有 → 按显式假设出数。"""
    candidate = resolve_out_of_catalog_candidate("冰箱明细")

    assert candidate is not None
    assert candidate.status == STATUS_OUT_OF_CATALOG
    assert candidate.is_ambiguous is False
    assert candidate.intent == "冰箱明细"


def test_clarification_answer_resolves_the_ambiguity():
    """用户拍板口径之后就不再澄清，按选中的口径出数。"""
    candidate = resolve_out_of_catalog_candidate("电脑耗电率", answer="占比")

    assert candidate is not None
    assert candidate.is_ambiguous is False
    assert candidate.unit == "%"
    assert "占比" in candidate.note


def test_unrelated_clarification_answer_keeps_the_ambiguity():
    """用户没在这几个口径里选 → 服务器不替他选，仍然要澄清。"""
    candidate = resolve_out_of_catalog_candidate("电脑耗电率", answer="随便")

    assert candidate is not None
    assert candidate.is_ambiguous is True


def test_candidate_is_never_mapped_onto_a_registered_metric():
    """目录外指标**不静默映射**到「总耗电」这类已登记指标。"""
    candidate = resolve_out_of_catalog_candidate("总耗电")

    assert candidate is not None
    # 语义类别是 generic（不是 energy）：共享能耗量的基础样本就等于
    # 把陌生指标当成「总耗电」。
    assert candidate.kind == "generic"


def test_simulated_identity_is_stable_and_not_a_metric_ref():
    """目录外指标只有**稳定模拟身份**，不伪造 metricRef。"""
    first = simulated_metric_id("section_5", 0)
    second = simulated_metric_id("section_5", 0)

    assert first == second
    assert first == "simulated.section_5.1"
    assert "metric.machine_room" not in first


# --------------------------------------------------------------------------
# 3. 选型：理由来自真实命中，组件类型不被静默改写
# --------------------------------------------------------------------------


def test_selection_reason_comes_from_a_real_rule_hit():
    """「明细」命中展示目的规则 → 按 records 选表格，依据是命中的规则。"""
    selection = select_component_type("加个冰箱明细")

    assert selection.componentType is ComponentType.TABLE
    assert selection.ruleId
    assert "明细" in selection.detail


def test_explicit_type_is_not_rewritten_by_the_system():
    """用户明确说了柱状图 → 类型就是柱状图，系统不改写。"""
    selection = select_component_type("加个总耗电用柱状图")

    assert selection.componentType is ComponentType.BAR_CHART
    assert selection.ruleId


def test_selection_for_an_existing_type_still_records_a_real_basis():
    """类型已经定了，依据不能是空白 —— 补一条同类型的真实命中。"""
    selection = selection_for_existing_type(
        ComponentType.BAR_CHART, "用户原话「柱状图」"
    )

    assert selection.componentType is ComponentType.BAR_CHART
    assert selection.ruleId
    assert selection.detail == "用户原话「柱状图」"
