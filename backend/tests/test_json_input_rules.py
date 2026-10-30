"""用户 JSON 输入层：「累计电表读数不默认求和」的词表（code-review R2 🟠-2）。

契约表 §4.3.1 ③ 与规格第 25 / 37 条点名的那一类值 ——
**温度、PUE、累计电表读数、编号不默认求和**。

改前 ``NON_SUMMABLE_FIELD_TOKENS`` 只收了 ``累计电表读数`` / ``电表读数`` /
``meterReading`` 这类**整名**写法，常见的 meter 族写法全部漏网：

    meter / 电表 / meterTotal / total_meter / meter_total /
    electricity_meter / 电表累计

于是 ``is_summable_field("meter")`` 返回 True → 生成 ``SUM`` 规则 →
累计读数 100 / 200 / 300 被**求和成 600**，而载荷里的口径说明还写着
「按求和展示（依据：方案声明的总量口径，用户已确认）」——
**把没依据的合计说成用户已确认**。

判定仍然按**词段精确匹配**（不是裸子串）：裸子串会把 ``grid``（含 ``id``）、
``notify``（含 ``no``）、``postcode``（含 ``code``）误判成编号类字段。
本文件同时钉住这两头：meter 族命中、编号类误伤**不**发生。

本文件只做**单元与接口层**断言；不新增端点、不新增测试框架。
"""
from __future__ import annotations

import json
from typing import Any

from app.services import json_input_service as json_svc


#: 契约表 §4.3.1 ③ 点名的 meter 族写法 —— 都要命中「不默认求和」。
_METER_FAMILY: tuple[str, ...] = (
    "meter",
    "电表",
    "meterTotal",
    "total_meter",
    "meter_total",
    "electricity_meter",
    "电表累计",
)

#: 对照组：这些**必须仍然可以求和**。
#:
#: 前三个是裸子串会误伤的（``grid`` 含 ``id``、``notify`` 含 ``no``、
#: ``postcode`` 含 ``code``）；``parameter`` / ``thermometer`` 含 ``meter``；
#: 后面几个是目录里的能量字段。
_NOT_IDENTIFIER_FIELDS: tuple[str, ...] = (
    "grid",
    "notify",
    "postcode",
    "grid_energy",
    "parameter",
    "thermometer",
    "energy",
    "energyKwh",
    "total_energy",
    "energy_reading",
)


# --------------------------------------------------------------------------
# 词表：meter 族命中
# --------------------------------------------------------------------------


def test_meter_family_is_not_summable() -> None:
    """meter 族写法全部命中「不默认求和」。"""
    for key in _METER_FAMILY:
        assert json_svc.is_summable_field(key) is False, key


def test_meter_family_matching_stays_segment_exact() -> None:
    """判定**仍然是词段精确匹配**，不是裸子串。

    ``parameter`` / ``thermometer`` 里含 ``meter``，``grid`` / ``notify`` /
    ``postcode`` 里含 ``id`` / ``no`` / ``code`` —— 裸子串会把它们全判成
    不可求和，本可求和的字段退化成明细。
    """
    for key in _NOT_IDENTIFIER_FIELDS:
        assert json_svc.is_summable_field(key) is True, key


# --------------------------------------------------------------------------
# 执行侧：求和打在不可求和字段上 → 这条规则不执行
# --------------------------------------------------------------------------


def test_sum_rule_on_meter_field_is_not_executed() -> None:
    """累计读数 100 / 200 / 300 **不被求和成 600**。

    改前这里会得到 ``[{"meter": 600.0}]``。
    """
    rows = [{"meter": 100}, {"meter": 200}, {"meter": 300}]
    rules = [
        {
            "ruleId": "rule_section_total_col_meter_sum",
            "operation": "SUM",
            "targetFieldKey": "meter",
            "params": {},
        }
    ]
    result = json_svc.apply_processing_rules(rows, rules)
    assert result == rows
    assert all(row["meter"] in (100, 200, 300) for row in result), result


# --------------------------------------------------------------------------
# 人话口径：说清是「累计电表读数」，不是默认兜底文案
# --------------------------------------------------------------------------


def test_non_summable_reason_names_meter_readings() -> None:
    """原因要说清「这是累计电表读数」，不许落到默认兜底文案上。"""
    for key in _METER_FAMILY:
        reason = json_svc.non_summable_reason(key)
        assert reason, key
        assert "累计电表读数" in reason, (key, reason)
        assert "求和" in reason, (key, reason)
        assert "不适合求和的字段" not in reason, (key, reason)


def test_every_non_summable_token_has_a_reason_label() -> None:
    """词表里的**每一个**词都有人话标签。

    词表与标签一一对应 —— 新加的词不许落到默认兜底文案
    「不适合求和的字段」上。
    """
    missing = [
        token
        for token in json_svc.NON_SUMMABLE_FIELD_TOKENS
        if token not in json_svc.NON_SUMMABLE_REASON_LABELS
    ]
    assert missing == []


# --------------------------------------------------------------------------
# 接口层：方案声明了「总量」意图时，累计电表读数**不生成求和规则**
# --------------------------------------------------------------------------


def _meter_sections() -> list[dict[str, Any]]:
    """一份「总量」意图的方案：指标意图里点名了 meter 字段。"""
    return [
        {
            "id": "section_total",
            "title": "电表读数概览",
            "metricIntents": ["总电表读数（字段 meter）"],
            "componentType": "metric_card",
            "displayIntent": "total",
        }
    ]


def test_meter_field_in_a_total_plan_gets_no_sum_rule() -> None:
    """方案声明了「总量」，累计电表读数**仍然不生成 SUM 规则**。

    改前这条规则是 ``SUM``，读数会被求和；改后退回 ``PASSTHROUGH``
    （明细展示），并把原因写进数据问题说明。
    """
    from tests.support.acceptance_harness import (
        ScriptedModel,
        acceptance_harness,
        plan_candidate,
    )

    rows = [
        {"ts": "2024-01-01", "meter": 100},
        {"ts": "2024-01-02", "meter": 200},
        {"ts": "2024-01-03", "meter": 300},
    ]
    model = ScriptedModel(plan_candidate(sections=_meter_sections()))
    with acceptance_harness(model=model) as harness:
        harness.send(f"这是昨天的数据 {json.dumps(rows, ensure_ascii=False)}")
        plan = harness.read_session()["snapshot"]["reportPlan"]
        confirmed = harness.confirm_plan(plan)
        definition = confirmed["domainObjects"]["reportDefinitionDraft"]
        assert definition is not None

        metadata = definition.get("metadata") or {}
        combination = metadata.get("effectiveCombination") or {}
        rules = combination.get("processingRules") or []
        assert rules
        assert all(
            str(rule.get("operation")) == "PASSTHROUGH" for rule in rules
        ), rules

        # 原因写进数据问题说明（用户得知道这个字段为什么没被求和）。
        issues = metadata.get("jsonInputIssues") or {}
        data_issues = issues.get("dataIssues") or []
        assert any(
            "meter" in str(item) and "累计电表读数" in str(item)
            for item in data_issues
        ), data_issues
