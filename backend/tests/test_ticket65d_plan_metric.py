"""工单 65 / GitHub #65：**方案侧**的静默映射缺口（#65d）。

#65c 已经修好**编辑入口**那一侧（「别名命中 ≠ 识别成功」）。这一票修
**方案侧**的同一个缺陷：方案里声明「设备耗电率」，服务端把它绑成
**已登记**的 ``metric.machine_room.device_energy`` —— 用户看到的不是
「新增一个模拟指标」，而是「把已有的设备耗电换了个名字」。

规格第 6 条 / 契约表 §3.5：目录外指标**不静默映射**到「总耗电」这类
已有指标；判据是「**量口径变没变**」，不是「字符串差了几个字」。

判定谓词**只有一份**（``simulated_field_semantics``），
编辑侧与方案侧用**同一套** —— 两处各写一份必然漂移。

测试分三层：

1. **方案侧**（方案 → 模板 → 定义 → 载荷）：断言**用户看得见**的结果 ——
   ``metricRefs`` / ``bindingKind`` / ``dataStatus``；
2. **编辑侧 vs 方案侧**：同一句话，两侧判定必须一致；
3. **一份判定**：两侧用的是**同一个**谓词对象。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.services.preview_service import (
    build_preview_context,
    build_report_preview_payload,
)
from app.services.report_draft_service import build_definition_draft
from app.services.report_template_service import build_template_design
from app.services.simulated_data_service import generate_simulated_data
from app.services.template_definition_service import build_template_definition
from app.services.template_edit_engine import template_edit_engine
from app.tools.context import ToolScope


SCOPE = ToolScope(project_id="project_001", subject_ref="machine_room", subject_id="room_a")

DEVICE_ENERGY_REF = "metric.machine_room.device_energy"
TOTAL_ENERGY_REF = "metric.machine_room.total_energy"


# --------------------------------------------------------------------------
# 夹具：走真实的两跳（方案 → 模板 → 定义），不 mock 判定逻辑
# --------------------------------------------------------------------------


def _plan(intents: list[str], section_id: str = "section_1") -> dict[str, Any]:
    return {
        "id": "rplan_ticket65d", "revision": 1, "topic": "A 机房能耗日报",
        "requirements": {
            "metrics": list(intents), "subject": "A 机房",
            "timeRange": "最近 7 天", "exclusions": [],
        },
        "sections": [
            {
                "id": section_id, "title": intents[0],
                "metricIntents": list(intents), "componentType": "metric_card",
            }
        ],
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }


def _build(intents: list[str]) -> tuple[Any, dict[str, Any]]:
    """方案 → 模板 → 定义草稿（走真实的两跳）。

    返回 ``(模板区域, 定义草稿)``。
    """
    plan = _plan(intents)
    template = build_template_design(plan, subject_id="room_a")
    draft = build_template_definition(
        template.model_dump(mode="json"), plan, "room_a"
    )
    return template.regions[0], draft.model_dump(mode="json")


def _spec(definition: dict[str, Any], index: int = 0) -> dict[str, Any]:
    return definition["regions"][index]["templateBinding"]["previewSpec"]


def _preview(definition: dict[str, Any]) -> dict[str, Any]:
    context = build_preview_context(definition)
    return build_report_preview_payload(definition, context)


# --------------------------------------------------------------------------
# 1. 方案侧：「设备耗电率」按目录外处理，**不静默映射**
# --------------------------------------------------------------------------


def test_plan_rate_metric_is_out_of_catalog_and_not_bound_to_device_energy():
    """方案声明「设备耗电率」→ 目录外：**不绑**「设备耗电」。

    这是本票的核心缺陷：静默映射会让用户看到「把已有的设备耗电换了
    个名字」，而不是「新增一个模拟指标」。
    """
    region, definition = _build(["设备耗电率"])

    # 模板层：这一块是占位，**没有任何**指标引用。
    assert region.dataStatus == "placeholder"
    assert region.metricRefs == []
    assert region.capabilityPending is True

    # 定义层：走**目录外模拟**路径 —— 不伪造 metricRef，只给稳定模拟身份。
    metric = _spec(definition)["metrics"][0]
    assert metric["bindingKind"] == "simulated"
    assert metric["simulatedId"].startswith("simulated.")
    assert not metric["metricKey"].startswith("metric.machine_room.")
    # 用户原话保留：系统不悄悄把「设备耗电率」改名成别的指标。
    assert metric["label"] == "设备耗电率"

    # 整份定义里**没有**「设备耗电」这条已登记指标的任何引用。
    assert DEVICE_ENERGY_REF not in str(definition)


def test_plan_rate_metric_asks_the_key_question_instead_of_guessing():
    """含义不清 → 出**可澄清**的问句（契约表 §3.5 / §7.3）。"""
    _, definition = _build(["设备耗电率"])

    clarification = _spec(definition).get("clarification")
    assert clarification is not None
    question = str(clarification.get("question") or "")
    assert "设备耗电率" in question
    for label in ("耗电量", "占比", "功率"):
        assert label in question, question


def test_plan_rate_metric_region_still_renders_simulated_numbers():
    """目录外**照样出数**（模拟路径），不阻断、不空白。"""
    _, definition = _build(["设备耗电率"])

    payloads = generate_simulated_data(
        definition, build_preview_context(definition)
    )
    assert isinstance(payloads["region_1"]["value"], (int, float))

    region = _preview(definition)["regions"][0]
    assert region["componentType"] == "METRIC_CARD"
    assert region["capabilityPending"] is True


# --------------------------------------------------------------------------
# 2. 方案侧：**既有行为一个字都不许变**
# --------------------------------------------------------------------------


def test_plan_device_energy_still_binds_the_registered_metric():
    """方案声明「设备耗电」→ 仍然绑上已登记指标（既有行为不变）。"""
    region, definition = _build(["设备耗电"])

    assert region.dataStatus == "bound"
    assert region.metricRefs == [DEVICE_ENERGY_REF]
    assert region.capabilityPending is False

    metric = _spec(definition)["metrics"][0]
    assert metric["bindingKind"] == "catalog"
    assert metric["simulatedId"] is None
    assert metric["metricKey"] == DEVICE_ENERGY_REF


def test_plan_device_energy_ranking_still_binds_the_registered_metric():
    """「设备耗电排名」只加了展示要求，量口径没变 → 仍然命中。

    既有测试 ``test_known_ranking_metric_add_is_still_a_normal_add_section``
    钉着编辑侧那一条；方案侧这一条是同一个不变式。
    """
    region, definition = _build(["设备耗电排名"])

    assert region.dataStatus == "bound"
    assert region.metricRefs == [DEVICE_ENERGY_REF]

    metric = _spec(definition)["metrics"][0]
    assert metric["bindingKind"] == "catalog"
    assert metric["simulatedId"] is None


def test_plan_total_energy_with_quantity_suffix_still_binds():
    """「总耗电量」是同义加后缀 → 仍然命中「总耗电」（既有行为不变）。"""
    region, definition = _build(["总耗电量"])

    assert region.dataStatus == "bound"
    assert region.metricRefs == [TOTAL_ENERGY_REF]

    metric = _spec(definition)["metrics"][0]
    assert metric["bindingKind"] == "catalog"
    assert metric["simulatedId"] is None


def test_plan_unknown_rate_metric_stays_out_of_catalog():
    """「电脑耗电率」本来就对：别把它弄坏。"""
    region, definition = _build(["电脑耗电率"])

    assert region.dataStatus == "placeholder"
    assert region.metricRefs == []

    metric = _spec(definition)["metrics"][0]
    assert metric["bindingKind"] == "simulated"
    assert metric["label"] == "电脑耗电率"


def test_plan_substring_misfire_keeps_its_existing_behaviour():
    """「设备耗电成本」这类既有子串误命中**保持原样**。

    改它属于变更既有业务输出，超出本票范围 —— 这里钉住现状，
    免得以后有人顺手改掉。
    """
    region, _ = _build(["设备耗电成本"])

    assert region.dataStatus == "bound"
    assert region.metricRefs == [DEVICE_ENERGY_REF]


# --------------------------------------------------------------------------
# 3. 编辑侧 vs 方案侧：同一句话，判定必须一致
# --------------------------------------------------------------------------


def _edit_side_verdict(phrase: str) -> str:
    """编辑入口对这句话的判定（走真实的编辑引擎解析）。"""
    definition = build_definition_draft("A 机房能耗日报", "room_a")
    plan = template_edit_engine.parse(
        message=f"新增一个{phrase}卡片",
        definition=definition,
        selected_region_id=None,
        scope=SCOPE,
    )
    metric_ref = getattr(plan.operation, "metricRef", None)
    if metric_ref:
        return f"registered:{metric_ref}"
    return "out_of_catalog"


def _plan_side_verdict(phrase: str) -> str:
    """方案侧对同一句话的判定（走真实的方案 → 模板两跳）。"""
    region, _ = _build([phrase])
    if region.metricRefs:
        return f"registered:{region.metricRefs[0]}"
    return "out_of_catalog"


@pytest.mark.parametrize(
    "phrase",
    [
        "设备耗电率",     # 口径对不上 → 两侧都当目录外
        "设备耗电",       # 就是那个指标 → 两侧都命中
        "设备耗电排名",   # 只加展示要求 → 两侧都命中
        "总耗电量",       # 同义加后缀 → 两侧都命中
        "电脑耗电率",     # 本来就登记不到 → 两侧都当目录外
    ],
)
def test_edit_side_and_plan_side_judge_the_same_phrase_the_same_way(phrase: str):
    """**同一套判定**：编辑入口与方案侧对同一句话的结论必须一致。"""
    assert _edit_side_verdict(phrase) == _plan_side_verdict(phrase)


# --------------------------------------------------------------------------
# 4. 一份判定：两侧用的是**同一个**谓词
# --------------------------------------------------------------------------


def test_both_sides_use_the_same_shared_predicate():
    """判定谓词只有一份，放在共享语义层（契约表 §3.5）。

    两处各写一份必然漂移 —— 这条钉住「只有一份」这个事实本身。
    """
    from app.services import report_template_service, template_edit_engine
    from app.services import simulated_field_semantics

    shared = simulated_field_semantics.intent_names_registered_metric
    assert report_template_service.intent_names_registered_metric is shared
    assert template_edit_engine.intent_names_registered_metric is shared
    # 编辑引擎里**不再**留一份模块私有的判定。
    assert not hasattr(template_edit_engine, "_intent_names_registered_metric")
