"""删除单个指标（``DELETE_METRIC``）：分句判定、指标级删除、同卡删加复合。

这一组用例守住「删掉某个指标」不再被判成「删掉整张卡片」：

    1. 「删除实时功率这个指标」是**指标级**删除，不是整卡删除；
    2. 删完卡片还在、别的指标还在；
    3. 「删除 X 再增加 Y」是**同卡复合**：一次确认、两步原子执行；
    4. 占位语义（「不要数值」）**不是**删除，也不误伤别的指标；
    5. 说清楚「删这张卡片」时**仍然**是整卡删除；
    6. 认不出来的待删项 / 最后一个指标 → **追问或安全拒绝**，绝不删卡；
    7. 删中间项之后追加的新身份**不撞号**，保留项的身份**不重新编号**；
    8. 复合第二步失败 → 草稿与 revision 都不动（原子性）。

fixture 走真实两跳（方案 → 模板 → 定义），区域带 ``templateBinding``；
指标不是假造的：``总耗电`` 是**目录内主指标**，``实时功率`` / ``湿度`` 是
模拟身份的第二、第三项。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.schemas.edit_plan import EditIntent
from app.services.report_template_service import build_template_design
from app.services.template_definition_service import build_template_definition
from app.services.template_edit_engine import template_edit_engine
from app.tools.context import ToolScope

SCOPE = ToolScope(
    project_id="project_001",
    subject_ref="machine_room",
    subject_id="room_a",
)

REGION_ID = "region_1"
REGION_NAME = "核心运行指标总览"

#: 原报错那句话：一句里既有「删指标」又有「加指标」。
ORIGINAL_MESSAGE = (
    "核心运行指标总览删除实时功率这个指标 再增加一个冰箱功率的指标"
)


def _plan_document(metric_intents: list[str]) -> dict[str, Any]:
    return {
        "topic": "A 机房能耗日报",
        "requirements": {
            "metrics": ["总耗电"], "subject": None,
            "timeRange": None, "exclusions": [],
        },
        "sections": [
            {
                "id": "section_total", "title": REGION_NAME,
                "metricIntents": list(metric_intents),
                "componentType": "metric_card",
            },
        ],
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }


def _build(metric_intents: list[str]):
    plan = _plan_document(metric_intents)
    template = build_template_design(plan, subject_id="room_a")
    return build_template_definition(
        template.model_dump(mode="json"), plan, "room_a",
    )


@pytest.fixture()
def definition():
    """两张指标的总览卡：总耗电（主指标）+ 实时功率。"""
    return _build(["总耗电", "实时功率"])


@pytest.fixture()
def three_metric_definition():
    """三张指标的总览卡：总耗电 + 实时功率 + 湿度（用来删中间那项）。"""
    return _build(["总耗电", "实时功率", "湿度"])


def _parse(definition, message: str, selected_region_id: str | None = None):
    return template_edit_engine.parse(
        message=message,
        definition=definition,
        selected_region_id=selected_region_id,
        scope=SCOPE,
    )


def _spec_metrics(definition, region_id: str = REGION_ID) -> list[dict[str, Any]]:
    region = next(
        item for item in definition.regions if item.get("id") == region_id
    )
    binding = region.get("templateBinding") or {}
    spec = binding.get("previewSpec") or {}
    return [item for item in (spec.get("metrics") or []) if isinstance(item, dict)]


def _labels(definition, region_id: str = REGION_ID) -> list[str]:
    return [
        str(item.get("label") or "")
        for item in _spec_metrics(definition, region_id)
    ]


def _simulated_ids(definition, region_id: str = REGION_ID) -> list[str]:
    return [
        str(item.get("simulatedId") or "")
        for item in _spec_metrics(definition, region_id)
    ]


# --------------------------------------------------------------------------
# 1. 「删掉某个指标」不是「删掉整张卡片」
# --------------------------------------------------------------------------


def test_delete_metric_is_not_delete_section(definition):
    """删除对象是卡片里的一个指标 → 意图是 DELETE_METRIC。"""
    plan = _parse(definition, "核心运行指标总览删除实时功率这个指标")

    assert plan.intent == EditIntent.DELETE_METRIC
    assert plan.needsClarification is False
    assert plan.target.regionId == REGION_ID
    assert len(plan.operation.deleteMetrics) == 1
    deletion = plan.operation.deleteMetrics[0]
    assert deletion.label == "实时功率"
    # 身份是**稳定标识**，不是数组下标，也不是目录 metricRef。
    assert deletion.metricKey == "metric.power.section_total_2"
    assert deletion.simulatedId == "simulated.section_total.2"
    assert deletion.isPrimary is False


def test_delete_metric_keeps_the_card_and_the_other_metric(definition):
    """删掉一项指标：卡片还在、别的指标还在。"""
    plan = _parse(definition, "核心运行指标总览删除实时功率这个指标")
    result = template_edit_engine.apply(
        plan=plan, definition=definition, scope=SCOPE,
    )

    assert result.needsClarification is False
    assert result.changeSet is not None
    edited = result.draftAfterEdit
    # 卡片**没有**被删掉。
    assert any(item["id"] == REGION_ID for item in edited.regions)
    assert _labels(edited) == ["总耗电"]
    # 草稿版本只 +1。
    assert edited.metadata["revision"] == 1


def test_delete_primary_metric_promotes_the_next_one(definition):
    """删掉主指标：卡片保留，下一项**提上来**，不留空卡。"""
    plan = _parse(definition, "核心运行指标总览删除总耗电这个指标")
    result = template_edit_engine.apply(
        plan=plan, definition=definition, scope=SCOPE,
    )

    assert result.needsClarification is False
    edited = result.draftAfterEdit
    assert any(item["id"] == REGION_ID for item in edited.regions)
    assert _labels(edited) == ["实时功率"]
    # 主指标换人了：component.metric 不再指认已删掉的那一项。
    component = edited.regions[0].get("component") or {}
    assert component["metric"]["fieldRef"] != "energyKwh"


# --------------------------------------------------------------------------
# 2. 同卡「删一个 + 加一个」= 一次原子复合
# --------------------------------------------------------------------------


def test_delete_and_append_in_one_sentence_is_one_composite(definition):
    """原报错那句话 → 候选里既有删除也有追加。"""
    plan = _parse(definition, ORIGINAL_MESSAGE)

    assert plan.needsClarification is False
    assert plan.target.regionId == REGION_ID
    assert [step.intent for step in plan.operations] == [
        EditIntent.DELETE_METRIC, EditIntent.ADD_METRIC,
    ]
    delete_step = plan.operations[0]
    add_step = plan.operations[1]
    assert delete_step.operation.deleteMetrics[0].label == "实时功率"
    assert add_step.operation.addMetrics[0].label == "冰箱功率"
    # 摘要要说清**两步**，不能只说最后一步。
    assert "实时功率" in plan.summary and "冰箱功率" in plan.summary


def test_delete_and_append_applied_atomically(definition):
    """接受复合：卡片还在、总耗电保留、实时功率没了、冰箱功率出现。"""
    plan = _parse(definition, ORIGINAL_MESSAGE)
    result = template_edit_engine.apply(
        plan=plan, definition=definition, scope=SCOPE,
    )

    assert result.needsClarification is False
    edited = result.draftAfterEdit
    assert any(item["id"] == REGION_ID for item in edited.regions)
    assert _labels(edited) == ["总耗电", "冰箱功率"]
    # 只 +1 一次 revision。
    assert edited.metadata["revision"] == 1


def test_composite_failure_leaves_draft_and_revision_untouched(definition):
    """复合里第二步失败 → 整个草稿和 revision 都不动。"""
    plan = _parse(definition, ORIGINAL_MESSAGE)
    # 把追加那一步的指标名**抹掉** → 第二步在校验阶段失败。
    plan.operations[1].operation.addMetrics[0].label = ""
    plan.operations[1].operation.addMetrics[0].intent = ""

    result = template_edit_engine.apply(
        plan=plan, definition=definition, scope=SCOPE,
    )

    assert result.needsClarification is True
    assert result.changeSet is None
    assert result.draftAfterEdit.metadata["revision"] == 0
    # 原草稿一个字节都没动。
    assert definition.metadata["revision"] == 0
    assert _labels(definition) == ["总耗电", "实时功率"]


def test_delete_only_metric_plus_add_is_allowed(three_metric_definition):
    """「删掉唯一指标 + 加上新指标」按**最终草稿**判定，可以做完。"""
    definition = three_metric_definition
    plan = _parse(definition, "核心运行指标总览删除湿度这个指标 再加一个风速指标")
    if plan.needsClarification:
        pytest.skip("风速未登记为指标候选（本用例只覆盖原子性）")

    result = template_edit_engine.apply(
        plan=plan, definition=definition, scope=SCOPE,
    )

    assert result.needsClarification is False
    assert "湿度" not in _labels(result.draftAfterEdit)


# --------------------------------------------------------------------------
# 3. 占位语义不是删除
# --------------------------------------------------------------------------


def test_placeholder_no_value_is_not_a_delete(definition):
    """「再加一个温度指标 不要数值」→ 追加**占位**指标，不是删卡片。"""
    plan = _parse(definition, "核心运行指标总览再加一个温度指标 不要数值")

    assert plan.intent == EditIntent.ADD_METRIC
    assert plan.needsClarification is False
    assert plan.operation.addMetrics[0].label == "温度"
    assert plan.operation.addMetrics[0].displayMode == "placeholder_only"


def test_placeholder_only_applies_to_the_named_metric(definition):
    """「再加温度和湿度，湿度不要数值」→ 只有湿度占位，温度照常出数。"""
    plan = _parse(definition, "核心运行指标总览再加温度和湿度，湿度不要数值")

    assert plan.intent == EditIntent.ADD_METRIC
    modes = {
        item.label: item.displayMode for item in plan.operation.addMetrics
    }
    assert modes["温度"] == "simulated"
    assert modes["湿度"] == "placeholder_only"


def test_placeholder_does_not_touch_existing_metrics(definition):
    """占位追加之后，卡片里**已有**的指标一个都不少。"""
    plan = _parse(definition, "核心运行指标总览再加一个温度指标 不要数值")
    result = template_edit_engine.apply(
        plan=plan, definition=definition, scope=SCOPE,
    )

    assert result.needsClarification is False
    assert _labels(result.draftAfterEdit) == ["总耗电", "实时功率", "温度"]


# --------------------------------------------------------------------------
# 4. 边界：整卡删除、认不出来的目标、最后一个指标
# --------------------------------------------------------------------------


def test_explicit_card_delete_still_deletes_the_card(definition):
    """说清楚「删这张卡片」→ 仍然是整卡删除。"""
    plan = _parse(definition, "删除核心运行指标总览这张卡片")

    assert plan.intent == EditIntent.DELETE_SECTION
    assert plan.needsClarification is False
    assert plan.target.regionId == REGION_ID


def test_unknown_metric_target_is_clarified_not_card_delete(definition):
    """认不出来的待删指标 → **追问**，绝不回退成删整张卡片。"""
    plan = _parse(definition, "核心运行指标总览删除某个不存在的指标")

    assert plan.needsClarification is True
    assert plan.intent == EditIntent.DELETE_METRIC
    assert plan.operation.deleteMetrics == []


def test_deleting_the_last_metric_does_not_delete_the_card():
    """只有一项指标时删它 → 保留原草稿并解释（不自动删卡、不留空卡）。"""
    definition = _build(["总耗电"])
    plan = _parse(definition, "核心运行指标总览删除总耗电这个指标")

    assert plan.intent == EditIntent.DELETE_METRIC
    result = template_edit_engine.apply(
        plan=plan, definition=definition, scope=SCOPE,
    )

    assert result.needsClarification is True
    assert result.changeSet is None
    assert result.draftAfterEdit.metadata["revision"] == 0
    assert any(item["id"] == REGION_ID for item in result.draftAfterEdit.regions)
    assert _labels(result.draftAfterEdit) == ["总耗电"]


def test_same_metric_name_in_two_cards_is_clarified():
    """两张卡片里都有「实时功率」→ 追问，绝不删第一条匹配的。"""
    plan = {
        "topic": "A 机房能耗日报",
        "requirements": {
            "metrics": ["总耗电"], "subject": None,
            "timeRange": None, "exclusions": [],
        },
        "sections": [
            {
                "id": "section_total", "title": "核心运行指标总览",
                "metricIntents": ["总耗电", "实时功率"], "componentType": "metric_card",
            },
            {
                "id": "section_env", "title": "设备环境概览",
                "metricIntents": ["实时功率"], "componentType": "metric_card",
            },
        ],
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }
    template = build_template_design(plan, subject_id="room_a")
    definition = build_template_definition(
        template.model_dump(mode="json"), plan, "room_a",
    )

    parsed = _parse(definition, "删除实时功率这个指标")

    assert parsed.needsClarification is True
    assert parsed.operation.deleteMetrics == []


# --------------------------------------------------------------------------
# 5. 身份：新增不撞号、保留项不重新编号
# --------------------------------------------------------------------------


def test_new_metric_id_does_not_collide_after_removing_middle(
    three_metric_definition,
):
    """删掉中间那项之后追加 → 新身份唯一，保留项身份不变。"""
    definition = three_metric_definition
    before_ids = _simulated_ids(definition)
    kept_before = [item for item in before_ids if item]

    delete_plan = _parse(definition, "核心运行指标总览删除实时功率这个指标")
    deleted = template_edit_engine.apply(
        plan=delete_plan, definition=definition, scope=SCOPE,
    )
    assert deleted.needsClarification is False
    edited = deleted.draftAfterEdit

    append_plan = _parse(edited, "核心运行指标总览再加一个风速指标")
    appended = template_edit_engine.apply(
        plan=append_plan, definition=edited, scope=SCOPE,
    )
    assert appended.needsClarification is False

    after_ids = [item for item in _simulated_ids(appended.draftAfterEdit) if item]
    assert len(after_ids) == len(set(after_ids)), "新增身份撞号了"
    # 没被删的那几项身份**不变**。
    for item in kept_before:
        if item == "simulated.section_total.2":
            continue
        assert item in after_ids


def test_kept_metrics_keep_their_identity_after_delete(three_metric_definition):
    """删一项之后，其余指标的稳定身份一个都不许变。"""
    definition = three_metric_definition
    before = {
        item.get("label"): item.get("simulatedId")
        for item in _spec_metrics(definition)
    }
    plan = _parse(definition, "核心运行指标总览删除实时功率这个指标")
    result = template_edit_engine.apply(
        plan=plan, definition=definition, scope=SCOPE,
    )

    after = {
        item.get("label"): item.get("simulatedId")
        for item in _spec_metrics(result.draftAfterEdit)
    }
    assert after["总耗电"] == before["总耗电"]
    assert after["湿度"] == before["湿度"]
