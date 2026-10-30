"""工单 65 / GitHub #65：目录外指标在方案侧与编辑入口接通。

工单 AC 逐条覆盖：

1. 编辑入口允许新增目录外指标；解析不出真实指标标识时**不再以「未登记」
   拒绝**，而是转成可澄清或带显式假设的模拟候选；
2. 方案侧能声明目录外指标的板块与栏目；栏目身份与取值键照常由服务端
   定稿，不按标题猜列；
3. 候选被接受后，**实际出现带模拟数字的对应板块**，并标注模拟来源与
   「能力待接通」或「口径待确认」；
4. 修改形成新版本时关联新的上下文；旧版本的迟到结果不覆盖新版本；
5. 接受前定义不变；版本过期或冲突由服务端拒绝；
6. 选型与规则库一致：选型理由来自真实命中，组件类型不被系统静默改写，
   目录外语义明确的指标通过通用模拟能力展示。

测试分两层：**服务层**（编辑引擎 / 方案 → 模板 → 定义 → 载荷的��两跳）
与**接口层**（真实 ``POST /api/agent/chat`` 编排）。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.schemas.edit_plan import EditIntent
from app.services.effective_combination import combination_of
from app.services.preview_service import (
    build_preview_context,
    build_report_preview_payload,
)
from app.services.report_draft_service import build_definition_draft
from app.services.report_template_service import build_template_design
from app.services.template_definition_service import build_template_definition
from app.services.template_edit_engine import template_edit_engine
from app.services.validation_service import validate_definition
from app.tools.context import ToolScope


SCOPE = ToolScope(project_id="project_001", subject_ref="machine_room", subject_id="room_a")


@pytest.fixture()
def definition():
    return build_definition_draft("A 机房能耗日报", "room_a")


def parse(definition, message: str, selected: str | None = None):
    return template_edit_engine.parse(
        message=message,
        definition=definition,
        selected_region_id=selected,
        scope=SCOPE,
    )


def apply(definition, message: str, selected: str | None = None):
    return template_edit_engine.apply(
        plan=parse(definition, message, selected),
        definition=definition,
        scope=SCOPE,
    )


def preview(definition_dict: dict[str, Any]) -> dict[str, Any]:
    """按定义生成示意载荷（走真实的载荷装配）。"""
    context = build_preview_context(definition_dict)
    return build_report_preview_payload(definition_dict, context)


def _plan(sections: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "rplan_ticket65", "revision": 1, "topic": "A 机房能耗日报",
        "requirements": {
            "metrics": ["总耗电"], "subject": "A 机房",
            "timeRange": "最近 7 天", "exclusions": [],
        },
        "sections": sections,
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }


# ==========================================================================
# AC1：编辑入口允许新增目录外指标 —— 不再以「未登记」拒绝
# ==========================================================================


def test_ambiguous_out_of_catalog_add_becomes_a_clarification(definition):
    """「电脑耗电率」口径不清 → 问**最关键的语义区别**，不是「还缺指标」。"""
    plan = parse(definition, "增加一个电脑耗电率")

    assert plan.intent == EditIntent.ADD_SECTION
    assert plan.needsClarification is True
    assert "电脑耗电率" in plan.clarificationQuestion
    assert "耗电量" in plan.clarificationQuestion
    assert "占比" in plan.clarificationQuestion
    assert "功率" in plan.clarificationQuestion
    # 不是「缺指标」那套说法。
    assert "还缺" not in plan.clarificationQuestion
    assert "未登记" not in plan.clarificationQuestion


def test_clear_out_of_catalog_add_produces_a_candidate_not_a_rejection(definition):
    """「冰箱明细」语义明确但目录里没有 → 出候选，不拒绝。"""
    plan = parse(definition, "加个冰箱明细")

    assert plan.intent == EditIntent.ADD_SECTION
    assert plan.needsClarification is False
    assert plan.operation.metricRef is None
    assert plan.operation.metricIntent == "冰箱明细"
    # 候选带着**已解析的**口径走：接受时不再重新解析一遍。
    assert plan.operation.metricCandidate["intent"] == "冰箱明细"
    assert plan.operation.metricCandidate["status"] == "out_of_catalog"


def test_out_of_catalog_add_is_never_rejected_as_unregistered(definition):
    """「未登记」那句拒绝在目录外指标上不再出现。"""
    result = apply(definition, "加个冰箱明细")

    assert result.needsClarification is False
    assert result.changeSet is not None
    assert "未登记" not in result.reply
    assert "请先选择授权范围内的指标" not in result.reply


def test_clarification_answer_resolves_the_out_of_catalog_metric(definition):
    """用户拍板口径之后 → 按选中的口径出数，不再追问。"""
    plan = parse(definition, "增加一个电脑耗电率 耗电量")

    assert plan.needsClarification is False
    assert plan.operation.metricCandidate["unit"] == "kWh"
    assert plan.operation.metricCandidate["status"] == "out_of_catalog"


def test_change_metric_to_an_out_of_catalog_metric_is_not_rejected(definition):
    """换成一个目录外指标同样不再以「未登记」拒绝。"""
    plan = parse(
        definition, "把设备耗电排名的指标换成电脑耗电率 耗电量",
        selected="region_device_ranking",
    )
    result = template_edit_engine.apply(
        plan=plan, definition=definition, scope=SCOPE,
    )

    assert plan.intent == EditIntent.CHANGE_METRIC
    assert result.needsClarification is False
    assert result.changeSet is not None
    assert "未登记" not in result.reply


# ==========================================================================
# AC2：方案侧能声明目录外指标的板块与栏目（身份与取值键由服务端定稿）
# ==========================================================================


def test_plan_can_declare_an_out_of_catalog_section_with_columns():
    """方案声明目录外指标的板块与栏目 → 走真实的两跳，栏目身份服务端定稿。"""
    plan = _plan([
        {
            "id": "section_fridge", "title": "冰箱明细",
            "metricIntents": ["冰箱明细"], "componentType": "table",
            "columns": [
                {"id": "col_2", "label": "冰箱耗电", "fieldRef": "section_fridge.col_2"},
                {"id": "col_1", "label": "设备名称", "fieldRef": "section_fridge.col_1"},
            ],
        }
    ])
    template = build_template_design(plan, subject_id="room_a")
    definition = build_template_definition(
        template.model_dump(mode="json"), plan, "room_a",
    ).model_dump(mode="json")

    region = definition["regions"][0]
    binding = region["templateBinding"]
    assert binding["dataStatus"] == "placeholder"
    assert binding["metricRefs"] == []
    columns = binding["previewSpec"]["columns"]
    # 顺序按**方案声明**，不按标题排序：冰箱耗电在前。
    assert [item["label"] for item in columns] == ["冰箱耗电", "设备名称"]
    # 栏目身份与取值键是定稿值（服务端给的），不是按标题猜出来的。
    assert [item["id"] for item in columns] == ["col_2", "col_1"]
    assert [item["fieldRef"] for item in columns] == [
        "section_fridge.col_2", "section_fridge.col_1",
    ]


def test_out_of_catalog_metric_is_not_mapped_onto_a_registered_one():
    """目录外指标**不静默映射**到「总耗电」这类已登记指标。"""
    plan = _plan([
        {
            "id": "section_pc", "title": "电脑耗电率",
            "metricIntents": ["电脑耗电率"], "componentType": "metric_card",
        }
    ])
    template = build_template_design(plan, subject_id="room_a")
    definition = build_template_definition(
        template.model_dump(mode="json"), plan, "room_a",
    ).model_dump(mode="json")

    binding = definition["regions"][0]["templateBinding"]
    assert binding["metricRefs"] == []
    metric = binding["previewSpec"]["metrics"][0]
    assert metric["bindingKind"] == "simulated"
    assert metric["simulatedId"]
    # 语义类别是 generic：不共享能耗量的那份基础样本。
    assert metric["kind"] == "generic"


def test_plan_candidate_with_out_of_catalog_metric_gets_server_finalized_columns():
    """方案候选（只给显示名）→ 栏目身份与取值键由**服务端**定稿。

    走真实的方案服务：模型只给显示名，服务端补齐稳定 ``id`` 与行对象
    取值键；不按标题猜列。
    """
    from app.services.report_plan_service import apply_candidate

    candidate = {
        "topic": "A 机房能耗日报",
        "requirements": {
            "metrics": ["总耗电"], "subject": "A 机房",
            "timeRange": "最近 7 天", "exclusions": [],
        },
        "sections": [
            {
                "id": "section_fridge", "title": "冰箱明细",
                "metricIntents": ["冰箱明细"], "componentType": "table",
                "columns": [{"label": "设备名称"}, {"label": "冰箱耗电"}],
            }
        ],
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }
    plan = apply_candidate(
        candidate, None, plan_id="rplan_ticket65", expected_revision=None,
        original_request="来一份冰箱明细",
    ).model_dump(mode="json")

    section = plan["sections"][0]
    columns = section["columns"]
    # 身份与取值键是服务端给的稳定值。
    assert [item["id"] for item in columns] == ["col_1", "col_2"]
    assert [item["fieldRef"] for item in columns] == [
        "section_1.col_1", "section_1.col_2",
    ]
    # 选型理由来自真实命中（规则 ID + 命中细节）。
    evidence = plan["selectionEvidence"][0]
    assert evidence["ruleId"]
    assert evidence["componentType"] == "table"

    template = build_template_design(plan, subject_id="room_a")
    region = template.regions[0]
    assert region.dataStatus == "placeholder"
    assert region.metricRefs == []
    assert [item.fieldRef for item in region.columns] == [
        "section_1.col_1", "section_1.col_2",
    ]


# ==========================================================================
# AC3：接受后真的出现带模拟数字的板块，并标注来源与能力
# ==========================================================================


def test_accepted_candidate_produces_a_block_with_simulated_numbers(definition):
    """接受之后右侧真的多出一块**带数字**的板块，并标「能力待接通」。"""
    result = apply(definition, "加个冰箱明细")
    updated = result.draftAfterEdit.model_dump(mode="json")
    payload = preview(updated)

    before_ids = {item["id"] for item in definition.regions}
    new_ids = [item["regionId"] for item in payload["regions"] if item["regionId"] not in before_ids]
    assert new_ids, payload["regions"]

    region = next(item for item in payload["regions"] if item["regionId"] == new_ids[0])
    assert region["capabilityPending"] is True
    assert region["dataSource"] == "simulated"
    assert region["payload"]["sourceMode"] == "simulated"
    # 有行、不是空图。
    assert region["payload"]["rows"]
    assert region["payload"]["columns"][0]["label"] == "名称"

    added = next(
        item for item in updated["regions"] if item["id"] == new_ids[0]
    )
    reason = added["templateBinding"]["placeholderReason"]
    assert "能力待接通" in reason
    assert "模拟示例" in reason


def test_accepted_candidate_shows_numbers_for_a_known_unit(definition):
    """口径定了（有单位）→ 那一列出的是**数字**。"""
    result = apply(definition, "增加一个电脑耗电率 耗电量")
    updated = result.draftAfterEdit.model_dump(mode="json")
    payload = preview(updated)

    before_ids = {item["id"] for item in definition.regions}
    region = next(
        item for item in payload["regions"] if item["regionId"] not in before_ids
    )
    value_column = region["payload"]["columns"][-1]
    assert value_column["valueType"] == "energy"
    row = region["payload"]["rows"][0]
    value = row[value_column["fieldRef"]]
    assert isinstance(value, (int, float))


def test_definition_stays_free_of_business_numbers(definition):
    """定义里**不落任何业务数值**（ADR 0004 §5.1）。"""
    result = apply(definition, "增加一个电脑耗电率 耗电量")
    updated = result.draftAfterEdit.model_dump(mode="json")

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert key not in ("rows", "points", "yValues", "values"), key
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(updated)


# ==========================================================================
# AC4 / AC5：新版本关联新的上下文；过期与冲突由服务端拒绝
# ==========================================================================


def test_new_version_carries_its_own_effective_combination(definition):
    """修改形成新一版 → 这一版关联自己的生效组合。"""
    result = apply(definition, "加个冰箱明细")
    updated = result.draftAfterEdit.model_dump(mode="json")

    combination = updated["metadata"]["effectiveCombination"]
    assert combination["definitionVersion"] == updated["metadata"]["revision"]
    assert combination["sourceMode"] == "simulated"
    assert combination["inputId"] is None
    assert combination["fieldMapping"] == []


def test_plan_records_the_combination_it_was_based_on(definition):
    """候选记下它所基于的生效组合（接受时据此判过期 / 冲突）。"""
    plan = parse(definition, "加个冰箱明细")

    assert plan.baseCombination is not None
    assert plan.baseCombination == combination_of(definition.model_dump(mode="json"))


def test_definition_is_unchanged_before_acceptance(definition):
    """接受前定义不变：候选阶段不动草稿。"""
    before = definition.model_dump(mode="json")
    parse(definition, "加个冰箱明细")

    assert definition.model_dump(mode="json") == before
    assert definition.metadata["revision"] == 0


def test_stale_plan_is_rejected_by_the_server(definition):
    """旧版本的迟到结果不覆盖新版本：草稿变过 → 服务端直接拒绝。"""
    plan = parse(definition, "加个冰箱明细")
    # 草稿在候选提出之后又变了一版（另一项修改被接受了）。
    definition.metadata["revision"] = 1

    result = template_edit_engine.apply(plan=plan, definition=definition, scope=SCOPE)

    assert result.needsClarification is True
    assert result.changeSet is None
    assert "旧版本" in result.clarificationQuestion
    assert definition.metadata["revision"] == 1


def test_conflicting_combination_is_rejected_by_the_server(definition):
    """生效组合变了（来源模式换了）→ 服务端拒绝，旧生效组合保留。"""
    plan = parse(definition, "加个冰箱明细")
    metadata = dict(definition.metadata or {})
    metadata["sourceMode"] = "user_json"
    definition.metadata = metadata

    result = template_edit_engine.apply(plan=plan, definition=definition, scope=SCOPE)

    assert result.needsClarification is True
    assert result.changeSet is None
    assert "生效组合" in result.clarificationQuestion


# ==========================================================================
# AC6：选型与规则库一致
# ==========================================================================


def test_selection_reason_comes_from_a_real_rule_hit(definition):
    """选型理由来自**真实命中**（规则 ID + 命中细节）。"""
    plan = parse(definition, "加个冰箱明细")

    evidence = plan.operation.componentSelection
    assert evidence["ruleId"]
    assert evidence["detail"]
    assert evidence["componentType"] == "table"


def test_explicit_component_type_is_not_silently_rewritten(definition):
    """用户明确说了柱状图 → 类型就是柱状图，系统不改写。"""
    plan = parse(definition, "加个总耗电用柱状图")

    assert plan.operation.componentType == "CHART"
    assert plan.operation.chartKind == "BAR"


def test_out_of_catalog_metric_uses_the_generic_simulation_capability(definition):
    """目录外指标走**通用模拟能力**：模拟身份 + 明确标注，不伪造口径。"""
    result = apply(definition, "增加一个电脑耗电率 耗电量")
    updated = result.draftAfterEdit.model_dump(mode="json")
    added = next(
        item for item in updated["regions"]
        if item["id"] not in {region["id"] for region in definition.regions}
    )
    binding = added["templateBinding"]
    metric = binding["previewSpec"]["metrics"][0]

    assert binding["metricRefs"] == []
    assert metric["bindingKind"] == "simulated"
    assert metric["simulatedId"]
    assert metric["label"] == "电脑耗电率"       # 用户原话保留
    assert binding["previewSpec"]["capabilityPending"] is True
    # 口径待确认写在明面上。
    assert "口径待确认" in binding["placeholderReason"]


def test_new_region_passes_the_definition_validation(definition):
    """新增的目录外板块过得了既有的定义校验（不是绕过校验）。"""
    result = apply(definition, "增加一个电脑耗电率 耗电量")
    updated = result.draftAfterEdit.model_dump(mode="json")

    outcome = validate_definition(updated)
    assert outcome.passed is True, outcome.issues


# ==========================================================================
# 接口层主缝：真实 POST /api/agent/chat 编排（真实服务 + 真实会话库）
# ==========================================================================


def test_chat_api_adds_the_out_of_catalog_block_after_acceptance(post_chat, definition):
    """界面上真的多出一块带模拟数字的「冰箱明细」板块。

    走真实编排：先出候选（草稿不变），点「接受修改」之后草稿才变，
    右侧的示意载荷里出现那一块，并标着「能力待接通」。
    """
    snapshots = {"reportDefinition": definition.model_dump(mode="json")}

    candidate = post_chat(
        "加个冰箱明细",
        state="USER_EDITING_TEMPLATE",
        snapshots=snapshots,
    ).json()

    # 接受前：只是候选，草稿一个字节都没动。
    assert candidate["uiActions"] == ["pending_edit_plan"]
    assert candidate["domainObjects"]["pendingEditPlan"] is not None
    assert candidate["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 0
    assert candidate["domainObjects"]["changeSet"] is None

    accepted = post_chat(
        "接受修改",
        state="USER_EDITING_TEMPLATE",
        snapshots=snapshots,
    ).json()

    assert accepted["domainObjects"]["changeSet"] is not None
    draft = accepted["domainObjects"]["reportDefinitionDraft"]
    assert draft["metadata"]["revision"] == 1

    # 右侧那一屏：示意载荷里真的多了一块，带数字、标着能力待接通。
    payload = preview(draft)
    added = next(
        item for item in payload["regions"]
        if item["regionId"] not in {region["id"] for region in definition.regions}
    )
    assert added["name"] == "冰箱明细"
    assert added["capabilityPending"] is True
    assert added["dataSource"] == "simulated"
    assert added["payload"]["rows"]

    region = next(
        item for item in draft["regions"] if item["id"] == added["regionId"]
    )
    assert "能力待接通" in region["templateBinding"]["placeholderReason"]


def test_chat_api_asks_for_the_caliber_before_adding(post_chat, definition):
    """「增加一个电脑耗电率」先问口径，不直接拒。"""
    response = post_chat(
        "增加一个电脑耗电率",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": definition.model_dump(mode="json")},
    ).json()

    assert "电脑耗电率" in response["reply"]
    assert "耗电量" in response["reply"]
    assert response["domainObjects"]["pendingEditPlan"] is None
    assert response["domainObjects"]["reportDefinitionDraft"]["metadata"]["revision"] == 0

