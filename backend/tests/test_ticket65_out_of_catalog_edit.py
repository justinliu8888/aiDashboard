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

from pathlib import Path
from typing import Any

import pytest

from app.schemas.edit_plan import (
    EditIntent,
    EditMetricAddition,
    EditOperation,
    EditPlan,
    EditTarget,
)
from app.services.effective_combination import COMBINATION_KEYS
from app.services.metric_mock_service import list_metrics
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


def test_add_phrase_in_the_middle_is_not_a_new_metric(definition):
    """句中的「加个两度」不是新增指标 —— 不会凭空多出一块「两度」。"""
    plan = parse(definition, "把温度加个两度")

    assert plan.operation.metricIntent is None


def test_leading_filler_still_counts_as_an_add_request(definition):
    """「帮我加个冰箱明细」也是新增请求。"""
    plan = parse(definition, "帮我加个冰箱明细")

    assert plan.operation.metricIntent == "冰箱明细"
    assert plan.needsClarification is False


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


def _user_json_metadata() -> dict[str, Any]:
    """一份**用户 JSON 模式**的 metadata（#64 写进来的样子）。"""
    return {
        "sourceMode": "user_json",
        "effectiveCombination": {
            "planId": "rplan_ticket65",
            "planRevision": 1,
            "definitionId": "rdef_ticket65",
            "definitionVersion": 1,
            "sourceMode": "user_json",
            "inputId": "json_input_ticket65",
            "inputVersion": 2,
            "fieldMapping": [
                {
                    "regionId": "region_1",
                    "columnId": "col_1",
                    "inputFieldKey": "energyKwh",
                    "inputFieldPath": None,
                }
            ],
            "processingRules": [],
        },
        "jsonInputIssues": {
            "unmappedColumns": [{"regionId": "region_2", "columnId": "col_9"}],
            "dataIssues": [{"columnId": "col_3", "reason": "值缺失"}],
        },
    }


def test_new_version_preserves_the_effective_combination(definition):
    """修改形成新一版 → 生效组合**原样带过去**，不被重建。

    生效组合由定义构建路径（#64）统一写入：里面记着用户 JSON 的输入
    ID 与版本、字段映射、处理规则。编辑链路只读它，不改写它。
    """
    metadata = dict(definition.metadata or {})
    metadata.update(_user_json_metadata())
    definition.metadata = metadata

    result = apply(definition, "加个冰箱明细")
    updated = result.draftAfterEdit.model_dump(mode="json")

    assert updated["metadata"]["revision"] == 1
    assert updated["metadata"]["effectiveCombination"] == metadata["effectiveCombination"]
    assert updated["metadata"]["effectiveCombination"]["inputId"] == "json_input_ticket65"
    assert updated["metadata"]["effectiveCombination"]["inputVersion"] == 2


def test_edit_keeps_json_input_issues_and_user_json_binding(definition):
    """编辑接受后，``metadata.jsonInputIssues`` 与用户 JSON 的输入绑定都在。

    这两个键不在编辑链路上重建—— 丢了的话，用户 JSON 模式预览会因为
    「没有绑定输入」而失败（#64 刻意设计的失败：宁可不渲染，
    也不用模拟数字冒充用户数据）。
    """
    metadata = dict(definition.metadata or {})
    metadata.update(_user_json_metadata())
    definition.metadata = metadata

    result = apply(definition, "加个冰箱明细")
    updated = result.draftAfterEdit.model_dump(mode="json")

    assert updated["metadata"]["jsonInputIssues"] == metadata["jsonInputIssues"]
    assert updated["metadata"]["sourceMode"] == "user_json"
    assert updated["metadata"]["effectiveCombination"]["inputId"] == "json_input_ticket65"


def test_plan_records_the_combination_it_was_based_on(definition):
    """候选记下它所基于的生效组合（接受时据此判过期 / 冲突）。"""
    plan = parse(definition, "加个冰箱明细")

    assert plan.baseCombination is not None
    # 固定 9 键（契约表 §2.1），一个不多一个不少。
    assert set(plan.baseCombination) == set(COMBINATION_KEYS)
    assert plan.baseCombination["sourceMode"] == "simulated"


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


def test_out_of_catalog_trend_gives_multiple_points(definition):
    """目录外指标看趋势：给的是**多个时间点**，不是孤零零一个点。

    规格的用户故事 16：「希望能给目录外的指标生成多个时间点以便看趋势」。
    """
    result = apply(definition, "加个电脑耗电率趋势图 耗电量")
    updated = result.draftAfterEdit.model_dump(mode="json")
    payload = preview(updated)

    before_ids = {item["id"] for item in definition.regions}
    region = next(
        item for item in payload["regions"] if item["regionId"] not in before_ids
    )
    assert region["componentType"] == "CHART"
    assert len(region["payload"]["points"]) > 1
    assert region["payload"]["unit"] == "kWh"
    assert all(isinstance(value, (int, float)) for value in region["payload"]["yValues"])


def test_out_of_catalog_add_on_a_template_bound_definition():
    """真实报表（方案 → 模板 → 定义）上加目录外指标：新板块照样出数。

    新板块的板块 id 接着已有���排下去，不与别人的重号。
    """
    plan = _plan([
        {
            "id": "section_1", "title": "总耗电概览",
            "metricIntents": ["总耗电"], "componentType": "metric_card",
        },
        {
            "id": "section_2", "title": "设备耗电排名",
            "metricIntents": ["设备耗电"], "componentType": "table",
        },
    ])
    template = build_template_design(plan, subject_id="room_a")
    draft = build_template_definition(
        template.model_dump(mode="json"), plan, "room_a",
    )
    before_ids = {item["id"] for item in draft.regions}

    result = template_edit_engine.apply(
        plan=template_edit_engine.parse(
            message="加个冰箱明细",
            definition=draft,
            selected_region_id=None,
            scope=SCOPE,
        ),
        definition=draft,
        scope=SCOPE,
    )
    updated = result.draftAfterEdit.model_dump(mode="json")
    added = next(item for item in updated["regions"] if item["id"] not in before_ids)
    assert added["templateBinding"]["sectionId"] == "section_3"
    assert added["templateBinding"]["metricRefs"] == []
    assert added["templateBinding"]["previewSpec"]["metrics"][0]["bindingKind"] == "simulated"

    payload = preview(updated)
    region = next(
        item for item in payload["regions"] if item["regionId"] == added["id"]
    )
    assert region["capabilityPending"] is True
    assert region["payload"]["rows"]

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


# ==========================================================================
# 工单 #65b：意图门不再把目录外新增挡在编辑快路之外
# ==========================================================================
#
# 工单 #65 把目录外候选层实现**在编辑引擎里**（template_edit_engine →
# out_of_catalog_candidate）。工单 #58 立的意图门对 OUT_OF_CATALOG_ADD
# 直接跳过确定性编辑快路 —— 候选层因此永远走不到，AC1「不再以「未登记」
# 拒绝」对最典型的说法（新增一个温度指标 / 电脑耗电率指标）不成立。
#
# 下面全部走**接口层主缝**（真实 POST /api/agent/chat + 独立临时会话库），
# 断言的是用户看得见的行为：待接受的修改计划 → 接受 → 右侧真的多出一块
# 带模拟数字、标着模拟来源的板块。


def _metric_ref_by_label(label: str) -> str:
    """已登记指标的标识按标签查（不写死 mock 常量）。"""
    for item in list_metrics(subject_ref="machine_room"):
        if item.get("label") == label:
            return str(item.get("metricRef"))
    raise AssertionError(f"mock 指标目录里没有「{label}」")


def _added_block(payload: dict[str, Any], definition) -> dict[str, Any]:
    """右侧载荷里**新多出来**的那一块。"""
    before = {region["id"] for region in definition.regions}
    return next(
        item for item in payload["regions"]
        if item["regionId"] not in before
    )


def test_out_of_catalog_add_is_not_blocked_by_the_intent_gate(post_chat, definition):
    """「新增一个温度指标」不再被拒：产出**待接受的模拟候选**。"""
    response = post_chat(
        "新增一个温度指标",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": definition.model_dump(mode="json")},
    ).json()

    assert response["uiActions"] == ["pending_edit_plan"]
    plan = response["domainObjects"]["pendingEditPlan"]
    assert plan is not None, "目录外新增必须产出待接受的修改计划，而不是被拒绝"

    operation = plan["operations"][0]["operation"]
    assert operation.get("metricRef") is None, "目录外指标不伪造 metricRef"
    assert operation["metricIntent"] == "温度"
    assert operation["metricCandidate"]["status"] == "out_of_catalog"
    assert "未登记" not in response["reply"]
    # 接受之前草稿一个字节都没动。
    assert response["domainObjects"]["changeSet"] is None
    assert definition.metadata["revision"] == 0


def test_ambiguous_out_of_catalog_add_asks_the_caliber_then_offers_a_candidate(
    post_chat, definition
):
    """「新增一个电脑耗电率指标」：口径不清先问清楚，拍板后出模拟候选。

    这一条是 AC1 的**可澄清**分支：不是拒绝，是问最关键的语义区别。
    """
    snapshots = {"reportDefinition": definition.model_dump(mode="json")}

    first = post_chat(
        "新增一个电脑耗电率指标",
        state="USER_EDITING_TEMPLATE",
        snapshots=snapshots,
    ).json()

    assert "电脑耗电率" in first["reply"]
    assert "耗电量" in first["reply"]
    assert "未登记" not in first["reply"]
    assert first["domainObjects"]["pendingEditPlan"] is None

    # 用户拍板口径 → 出候选。
    second = post_chat(
        "耗电量", state="USER_EDITING_TEMPLATE", snapshots=snapshots,
    ).json()

    assert second["uiActions"] == ["pending_edit_plan"]
    operation = second["domainObjects"]["pendingEditPlan"]["operations"][0]["operation"]
    assert operation.get("metricRef") is None
    assert operation["metricIntent"] == "电脑耗电率"
    assert operation["metricCandidate"]["status"] == "out_of_catalog"
    assert operation["metricCandidate"]["unit"] == "kWh"


def test_accepted_out_of_catalog_candidate_adds_a_block_with_simulated_numbers(
    post_chat, definition
):
    """接受之后：右侧真的多出一块**带模拟数字**的板块，并标注模拟来源。"""
    snapshots = {"reportDefinition": definition.model_dump(mode="json")}

    candidate = post_chat(
        "新增一个温度指标",
        state="USER_EDITING_TEMPLATE",
        snapshots=snapshots,
    ).json()
    assert candidate["uiActions"] == ["pending_edit_plan"]

    accepted = post_chat(
        "接受修改", state="USER_EDITING_TEMPLATE", snapshots=snapshots,
    ).json()

    assert accepted["domainObjects"]["changeSet"] is not None
    draft = accepted["domainObjects"]["reportDefinitionDraft"]
    assert draft["metadata"]["revision"] == 1

    payload = preview(draft)
    block = _added_block(payload, definition)
    assert block["name"] == "温度"
    assert block["capabilityPending"] is True
    assert block["dataSource"] == "simulated"
    assert block["payload"]["sourceMode"] == "simulated"

    # 真的出数了，不是空图。
    rows = block["payload"]["rows"]
    assert rows
    value_column = block["payload"]["columns"][-1]
    assert all(
        str(row.get(value_column["fieldRef"]) or "").strip() for row in rows
    ), "每一行都要有模拟值"

    # 标注了模拟来源。
    region = next(item for item in draft["regions"] if item["id"] == block["regionId"])
    reason = region["templateBinding"]["placeholderReason"]
    assert "能力待接通" in reason
    assert "模拟" in reason


def test_accepted_out_of_catalog_candidate_shows_numbers_once_the_caliber_is_set(
    post_chat, definition
):
    """口径定了（有单位）→ 那一列出的是**数字**。

    走完整条链：澄清口径 → 模拟候选 → 接受 → 右侧那一块带数字。
    """
    snapshots = {"reportDefinition": definition.model_dump(mode="json")}

    post_chat(
        "新增一个电脑耗电率指标",
        state="USER_EDITING_TEMPLATE",
        snapshots=snapshots,
    )
    candidate = post_chat(
        "耗电量", state="USER_EDITING_TEMPLATE", snapshots=snapshots,
    ).json()
    assert candidate["uiActions"] == ["pending_edit_plan"]

    accepted = post_chat(
        "接受修改", state="USER_EDITING_TEMPLATE", snapshots=snapshots,
    ).json()
    draft = accepted["domainObjects"]["reportDefinitionDraft"]
    assert draft["metadata"]["revision"] == 1

    payload = preview(draft)
    block = _added_block(payload, definition)
    assert block["name"] == "电脑耗电率"
    assert block["capabilityPending"] is True
    assert block["dataSource"] == "simulated"

    value_column = block["payload"]["columns"][-1]
    assert value_column["valueType"] == "energy"
    row = block["payload"]["rows"][0]
    assert isinstance(row[value_column["fieldRef"]], (int, float))

    region = next(item for item in draft["regions"] if item["id"] == block["regionId"])
    reason = region["templateBinding"]["placeholderReason"]
    assert "能力待接通" in reason
    assert "口径待确认" in reason


def test_known_metric_add_is_still_a_normal_add_section(post_chat, definition):
    """「新增一个总耗电卡片」仍是**正常** ADD_SECTION（已知指标，不该变模拟）。"""
    response = post_chat(
        "新增一个总耗电卡片",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": definition.model_dump(mode="json")},
    ).json()

    assert response["uiActions"] == ["pending_edit_plan"]
    operation = response["domainObjects"]["pendingEditPlan"]["operations"][0]["operation"]
    assert operation["metricRef"] == _metric_ref_by_label("总耗电")
    assert operation.get("metricCandidate") is None, "已知指标不该变成模拟候选"
    assert response["debug"]["editingIntent"] == "ADD_SECTION"


def test_known_ranking_metric_add_is_still_a_normal_add_section(post_chat, definition):
    """「新增一个设备耗电排名卡片」同上：已知指标，仍是正常 ADD_SECTION。"""
    response = post_chat(
        "新增一个设备耗电排名卡片",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": definition.model_dump(mode="json")},
    ).json()

    assert response["uiActions"] == ["pending_edit_plan"]
    operation = response["domainObjects"]["pendingEditPlan"]["operations"][0]["operation"]
    assert operation["metricRef"] == _metric_ref_by_label("设备耗电")
    assert operation.get("metricCandidate") is None, "已知指标不该变成模拟候选"
    assert response["debug"]["editingIntent"] == "ADD_SECTION"


def test_delete_command_still_goes_through_the_modify_fast_path(post_chat, definition):
    """「删除某个卡片」仍是 MODIFY 快路，不受意图门放开的影响。"""
    response = post_chat(
        "删除某个卡片",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": definition.model_dump(mode="json")},
    ).json()

    assert response["debug"]["editingIntent"] == "DELETE_SECTION"
    assert response["domainObjects"]["pendingEditPlan"] is None
    assert response["domainObjects"]["changeSet"] is None


def test_visualization_change_still_goes_through_the_modify_fast_path(
    post_chat, definition
):
    """「把趋势图换成柱状图」仍是 MODIFY 快路，不受意图门放开的影响。"""
    response = post_chat(
        "把趋势图换成柱状图",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": definition.model_dump(mode="json")},
    ).json()

    assert response["debug"]["editingIntent"] == "CHANGE_VISUALIZATION"
    # 目标卡片按当天快照推导，不写死 id。
    trend = next(
        region for region in definition.regions if region.get("role") == "TREND"
    )
    assert response["debug"]["editingTargetRegionId"] == trend["id"]


def test_issue_feedback_still_only_appends_chat(post_chat, definition):
    """「这个图看不懂」仍是**问题反馈**：只追加聊天，不产计划。"""
    response = post_chat(
        "这个图看不懂",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": definition.model_dump(mode="json")},
    ).json()

    assert response["uiActions"] == ["query_answer"]
    assert response["debug"]["userIntent"] == "ISSUE_FEEDBACK"
    assert response["domainObjects"]["pendingEditPlan"] is None
    assert response["domainObjects"]["changeSet"] is None
    assert definition.metadata["revision"] == 0


def test_consultation_still_only_appends_chat(post_chat, definition):
    """「冰箱耗电明细可以用其他展示方法吗？」仍是**咨询**：只追加聊天，不产计划。"""
    response = post_chat(
        "冰箱耗电明细可以用其他展示方法吗？",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": definition.model_dump(mode="json")},
    ).json()

    assert response["uiActions"] == ["query_answer"]
    assert response["domainObjects"]["pendingEditPlan"] is None
    assert response["domainObjects"]["changeSet"] is None
    assert definition.metadata["revision"] == 0


# ==========================================================================
# 工单 #65c：别名命中 ≠ 识别成功
# ==========================================================================
#
# 别名的匹配是**子串**：「设备耗电率」包含别名「设备耗电」—— 但用户问的
# 是**比率**，不是 kWh 的耗电量。引擎原来按别名子串就认定「已登记指标」，
# 紧接着把已经解析好的目录外候选丢掉（`if metric_ref: candidate = None`），
# 于是「设备耗电率」被**静默映射**成「设备耗电」（规格第 6 条）。
#
# 下面两串都要守住：
#   - 「设备耗电率」→ 目录外候选（带可澄清的问句 / 显式假设）；
#   - 「设备耗电」  → 仍然命中已登记指标（既有行为一个字不变）。


def test_rate_metric_add_is_not_mapped_onto_device_energy(definition):
    """「新增一个设备耗电率指标」→ 目录外候选，不挂到已登记的「设备耗电」。"""
    plan = parse(definition, "新增一个设备耗电率指标")

    assert plan.intent == EditIntent.ADD_SECTION
    assert plan.operation.metricRef is None, "目录外指标不该被静默映射到已登记指标"
    assert plan.operation.metricIntent == "设备耗电率"
    assert plan.operation.metricCandidate is not None
    # 口径不清 → 带**可澄清的问句**，不替他选一个当结论。
    assert plan.needsClarification is True
    assert "设备耗电率" in plan.clarificationQuestion
    assert "耗电量" in plan.clarificationQuestion
    assert "占比" in plan.clarificationQuestion
    assert "功率" in plan.clarificationQuestion


def test_rate_metric_add_carries_the_chosen_caliber(definition):
    """用户拍板口径 → 出**带显式假设**的模拟候选（仍然不挂 metricRef）。"""
    plan = parse(definition, "新增一个设备耗电率指标 耗电量")

    assert plan.operation.metricRef is None
    assert plan.operation.metricIntent == "设备耗电率"
    assert plan.operation.metricCandidate["status"] == "out_of_catalog"
    assert plan.operation.metricCandidate["unit"] == "kWh"
    assert "口径待确认" in plan.operation.metricCandidate["note"]
    assert plan.needsClarification is False


def test_device_energy_add_still_hits_the_registered_metric(definition):
    """「新增一个设备耗电指标」「加个设备耗电」→ 仍然命中已登记指标。"""
    for text in ("新增一个设备耗电指标", "加个设备耗电"):
        plan = parse(definition, text)

        assert plan.operation.metricRef == _metric_ref_by_label("设备耗电"), text
        assert plan.operation.metricIntent is None, text
        assert plan.operation.metricCandidate is None, text
        assert plan.needsClarification is False, text


def test_ranking_device_energy_add_still_hits_the_registered_metric(definition):
    """「设备耗电排名」只是加了展示要求，量口径没变 → 仍然命中已登记指标。"""
    plan = parse(definition, "新增一个设备耗电排名卡片")

    assert plan.operation.metricRef == _metric_ref_by_label("设备耗电")
    assert plan.operation.metricCandidate is None


def test_rate_metric_change_is_not_mapped_onto_device_energy(definition):
    """改指标分支同样：换成「设备耗电率」→ 目录外候选，不静默映射。"""
    plan = parse(
        definition, "把设备耗电排名的指标换成设备耗电率",
        selected="region_device_ranking",
    )

    assert plan.intent == EditIntent.CHANGE_METRIC
    assert plan.operation.metricRef is None, "目录外指标不该被静默映射到已登记指标"
    assert plan.operation.metricIntent == "设备耗电率"
    assert plan.operation.metricCandidate is not None
    assert plan.needsClarification is True
    assert "设备耗电率" in plan.clarificationQuestion


def test_device_energy_change_still_hits_the_registered_metric(definition):
    """改指标分支：换成「设备耗电」→ 仍然命中已登记指标（既有行为不变）。"""
    plan = parse(
        definition, "把设备耗电排名的指标换成设备耗电",
        selected="region_device_ranking",
    )

    assert plan.intent == EditIntent.CHANGE_METRIC
    assert plan.operation.metricRef == _metric_ref_by_label("设备耗电")
    assert plan.operation.metricCandidate is None
    assert plan.needsClarification is False


def test_accepted_rate_metric_block_is_marked_capability_pending(definition):
    """接受之后：右侧多出一块**带模拟数字**的板块，标「能力待接通」。"""
    result = apply(definition, "新增一个设备耗电率指标 耗电量")
    updated = result.draftAfterEdit.model_dump(mode="json")
    payload = preview(updated)

    before_ids = {region["id"] for region in definition.regions}
    region = next(
        item for item in payload["regions"]
        if item["regionId"] not in before_ids
    )

    assert region["name"] == "设备耗电率"
    assert region["capabilityPending"] is True
    assert region["dataSource"] == "simulated"
    assert region["payload"]["rows"]

    added = next(item for item in updated["regions"] if item["id"] not in before_ids)
    binding = added["templateBinding"]
    assert binding["metricRefs"] == [], "目录外指标不伪造真实指标绑定"
    assert binding["previewSpec"]["metrics"][0]["bindingKind"] == "simulated"
    assert "能力待接通" in binding["placeholderReason"]


def test_out_of_catalog_rate_metric_is_not_mapped_onto_a_registered_one(
    post_chat, definition
):
    """接口层：「新增一个设备耗电率指标」不静默映射成「设备耗电」。

    用户看得见的行为：先问口径（「耗电量？占比？功率？」），拍板之后才出
    **待接受**的模拟候选 —— 候选里没有真实指标标识。
    """
    snapshots = {"reportDefinition": definition.model_dump(mode="json")}

    first = post_chat(
        "新增一个设备耗电率指标",
        state="USER_EDITING_TEMPLATE",
        snapshots=snapshots,
    ).json()

    # 不静默映射：没有产出任何挂到已登记指标上的修改计划。
    assert first["domainObjects"]["pendingEditPlan"] is None
    assert "设备耗电率" in first["reply"]
    assert "耗电量" in first["reply"]
    assert "未登记" not in first["reply"]
    assert first["domainObjects"]["changeSet"] is None
    assert definition.metadata["revision"] == 0

    # 用户拍板口径 → 出目录外候选：没有 metricRef，带着已选的口径。
    second = post_chat(
        "耗电量", state="USER_EDITING_TEMPLATE", snapshots=snapshots,
    ).json()

    assert second["uiActions"] == ["pending_edit_plan"]
    operation = second["domainObjects"]["pendingEditPlan"]["operations"][0]["operation"]
    assert operation.get("metricRef") is None, "目录外指标不该被静默映射到已登记指标"
    assert operation["metricIntent"] == "设备耗电率"
    assert operation["metricCandidate"]["status"] == "out_of_catalog"
    assert operation["metricCandidate"]["unit"] == "kWh"


def test_known_device_energy_add_is_still_a_registered_metric(post_chat, definition):
    """「新增一个设备耗电指标」仍然是**正常** ADD_SECTION（不变成模拟候选）。"""
    response = post_chat(
        "新增一个设备耗电指标",
        state="USER_EDITING_TEMPLATE",
        snapshots={"reportDefinition": definition.model_dump(mode="json")},
    ).json()

    assert response["uiActions"] == ["pending_edit_plan"]
    operation = response["domainObjects"]["pendingEditPlan"]["operations"][0]["operation"]
    assert operation["metricRef"] == _metric_ref_by_label("设备耗电")
    assert operation.get("metricCandidate") is None, "已知指标不该变成模拟候选"
    assert response["debug"]["editingIntent"] == "ADD_SECTION"



# ==========================================================================
# 工单 20261102（B 包）：目录外指标**追加**到已有卡片 —— 走到渲染，不只查 prompt
# ==========================================================================


def _template_bound_draft():
    """带模板绑定的草稿：卡片上已有真实口径的指标（追加后要一个不少地保留）。"""
    plan = _plan([
        {
            "id": "section_1", "title": "总耗电概览",
            "metricIntents": ["总耗电"], "componentType": "metric_card",
        },
        {
            "id": "section_2", "title": "设备耗电排名",
            "metricIntents": ["设备耗电"], "componentType": "table",
        },
    ])
    template = build_template_design(plan, subject_id="room_a")
    return build_template_definition(template.model_dump(mode="json"), plan, "room_a")


def _append_plan(region_id: str, label: str) -> EditPlan:
    """构造一份「追加指标」计划。

    只给线索（显示名 + 展示模式），**不给**口径：目录外的指标口径由服务端
    自己解析 —— 与模型实际提交的那份一样。
    """
    return EditPlan(
        intent=EditIntent.ADD_METRIC,
        confidence=0.9,
        target=EditTarget(regionId=region_id, confidence=1.0),
        operation=EditOperation(
            addMetrics=[
                EditMetricAddition(
                    intent=label, label=label, displayMode="simulated",
                )
            ],
        ),
        summary=f"追加 {label}",
    )


def test_out_of_catalog_metric_appended_to_a_card_keeps_the_original_metrics():
    """追加目录外指标：卡片与原有指标**一个不少**，新指标没有单位、带口径标注。

    这是用户反馈的那条链路（「增加 abc」被加到卡片上）：
    追加 ≠ 替换，原有指标不能被顶掉。
    """
    draft = _template_bound_draft()
    before = next(
        item for item in draft.regions if item["id"] == "region_1"
    )
    assert [m["label"] for m in before["templateBinding"]["previewSpec"]["metrics"]] == ["总耗电"]

    result = template_edit_engine.apply(
        plan=_append_plan("region_1", "abc"),
        definition=draft,
        scope=SCOPE,
    )

    assert result.changeSet is not None, "目录外指标不该被拒绝"
    updated = result.draftAfterEdit.model_dump(mode="json")
    region = next(item for item in updated["regions"] if item["id"] == "region_1")
    metrics = {
        m["label"]: m
        for m in region["templateBinding"]["previewSpec"]["metrics"]
    }

    # 原有指标保留，新指标加在旁边。
    assert set(metrics) == {"总耗电", "abc"}
    assert metrics["总耗电"]["unit"] == "kWh"
    assert metrics["总耗电"]["bindingKind"] == "catalog"
    # 目录外指标：没有真实口径 → 不给单位，不伪造 metricRef。
    assert metrics["abc"]["unit"] == ""
    assert metrics["abc"]["bindingKind"] == "simulated"
    assert "metric.machine_room" not in str(metrics["abc"]["metricKey"])
    # 口径待确认写在明面上（模拟模式下照样出数，不标「能力待接通」）。
    assert "仅模拟展示、业务口径待确认" in str(metrics["abc"]["pendingReason"])
    assert metrics["abc"]["capabilityPending"] is False


def test_appended_out_of_catalog_metric_renders_a_simulated_number():
    """追加的那一项**出模拟数值**：定义里不塞业务数字，数字由渲染层造。"""
    draft = _template_bound_draft()
    result = template_edit_engine.apply(
        plan=_append_plan("region_1", "abc"),
        definition=draft,
        scope=SCOPE,
    )
    updated = result.draftAfterEdit.model_dump(mode="json")

    # 数值不进定义：列上只有身份与单位。
    region = next(item for item in updated["regions"] if item["id"] == "region_1")
    for column in (region.get("component") or {}).get("columns") or []:
        assert not isinstance(column.get("value"), (int, float))
        assert not isinstance(column.get("values"), list)

    payload = preview(updated)
    target = next(
        item for item in payload["regions"] if item["regionId"] == "region_1"
    )
    metrics = target["payload"].get("metrics") or []
    abc = next((m for m in metrics if m.get("label") == "abc"), None)
    assert abc is not None, target["payload"]
    assert abc.get("unit") == ""
    # 口径待确认的标注跟着这一项走。
    assert "仅模拟展示、业务口径待确认" in str(abc.get("pendingReason") or "")


def test_search_metric_description_does_not_treat_the_catalog_as_a_whitelist():
    """工具说明：目录是**参考**，不是准入名单。

    「必须使用查询到的指标」这类措辞会让模型把目录缺项当成拒绝理由，
    是用户反馈里「授权目录里没有对应的指标口径」的来源之一。
    """
    from app.tools import search_metric_tool

    text = search_metric_tool.DESCRIPTION
    assert "禁止凭空编造指标名或单位" not in text
    assert "必须使用查询到的指标" not in text
    # 新的说法：查不到不等于不能提。
    assert "目录外模拟指标" in text or "查不到" in text


# ==========================================================================
# 工单 20261102（B 包）：「增加 abc」这条链路 —— 目录外指标**真的能被加上**
# ==========================================================================
#
# 用户反馈：选中卡片以后说「增加 abc」，指标加不上（模型直接拒绝），
# 就算加上了也没有口径标注。
#
# 下面这几条钉住**服务端**那一半（模型那一半由协调方跑真实模型验）：
#   1. 目录外指标追加上去 = 通用模拟指标 + 「口径待确认」标注；
#   2. 不伪造口径、不静默映射成已登记指标；
#   3. 已登记指标不被降级成通用模拟指标；
#   4. 用户 JSON 模式的边界不动；
#   5. 编辑态 prompt 不再把目录当准入名单。


def test_appended_out_of_catalog_metric_is_not_mapped_to_a_registered_metric():
    """追加目录外指标：**不伪造口径**，也不静默映射成已登记指标。

    给了能耗量族就等于把陌生指标悄悄换成「总耗电」（规格第 6 条）。
    """
    draft = _template_bound_draft()
    result = template_edit_engine.apply(
        plan=_append_plan("region_1", "abc"),
        definition=draft,
        scope=SCOPE,
    )

    assert result.changeSet is not None
    updated = result.draftAfterEdit.model_dump(mode="json")
    region = next(item for item in updated["regions"] if item["id"] == "region_1")
    metrics = {
        m["label"]: m for m in region["templateBinding"]["previewSpec"]["metrics"]
    }
    appended = metrics["abc"]
    # 没有真实口径 → 不给单位，也不给一个像模像样的 metricRef。
    assert appended["unit"] == ""
    assert not appended.get("metricRef")
    # 语义类别是**通用**：不是能耗量族、不是成本族。
    assert appended["kind"] == "generic"
    # 身份是模拟身份，不是目录里的引用。
    assert appended["simulatedId"].startswith("simulated.")
    assert "metric.machine_room" not in appended["metricKey"]


def test_appended_registered_metric_keeps_its_real_caliber():
    """追加**已登记**指标：照真实口径走，不被降级、也不挂「口径待确认」。

    模型没给 metricRef 时由服务端自己认 —— 认出来了就用真实口径
    （设备耗电 = kWh / energy），别给它编一个空单位，也别标成目录外。
    """
    draft = _template_bound_draft()
    result = template_edit_engine.apply(
        plan=_append_plan("region_1", "设备耗电"),
        definition=draft,
        scope=SCOPE,
    )

    assert result.changeSet is not None
    updated = result.draftAfterEdit.model_dump(mode="json")
    region = next(item for item in updated["regions"] if item["id"] == "region_1")
    metrics = {
        m["label"]: m for m in region["templateBinding"]["previewSpec"]["metrics"]
    }
    appended = metrics["设备耗电"]
    assert appended["unit"] == "kWh"
    assert appended["kind"] == "energy"
    # 已登记指标**不是**目录外指标：不能挂「口径待确认」。
    assert "口径待确认" not in str(appended["pendingReason"])
    assert appended["capabilityPending"] is False


def test_appended_out_of_catalog_metric_in_user_json_mode_stays_capability_pending():
    """用户 JSON 模式：目录外指标**仍然**标「能力待接通」。

    模拟模式放松的是「模拟渲染能不能出数」，不是「真实口径存不存在」——
    用户 JSON 模式下没有真实口径可取，那条边界不能跟着一起放松。
    """
    draft = _template_bound_draft()
    draft.metadata["sourceMode"] = "user_json"
    result = template_edit_engine.apply(
        plan=_append_plan("region_1", "abc"),
        definition=draft,
        scope=SCOPE,
    )

    assert result.changeSet is not None
    updated = result.draftAfterEdit.model_dump(mode="json")
    region = next(item for item in updated["regions"] if item["id"] == "region_1")
    metrics = {
        m["label"]: m for m in region["templateBinding"]["previewSpec"]["metrics"]
    }
    appended = metrics["abc"]
    assert appended["capabilityPending"] is True
    assert str(appended["pendingReason"]).startswith("能力待接通：")


def test_the_edit_prompt_does_not_treat_the_catalog_as_a_whitelist():
    """编辑态 prompt：目录是**参考**，不是准入名单。

    编辑态不再走通用路由（``agent_service`` 里直接短路成
    ``template_edit_react``），模型分支是这条链路真正跑的那一条 ——
    prompt 里留着「目录里没有就不能加」的说法，模型就会直接拒绝。
    """
    prompt = (
        Path(__file__).resolve().parents[1]
        / "app" / "agents" / "prompts" / "template_edit.md"
    ).read_text(encoding="utf-8")

    # 模拟模式下任何指标名都能加进草稿。
    assert "都可以加进草稿" in prompt
    # 不把用户逼回已登记的清单，也不输出那四项菜单。
    assert "四项菜单" in prompt
    assert "总耗电、设备耗电、PUE、能耗成本" not in prompt
    # 目录外指标不编造口径、不静默映射。
    assert "静默映射" in prompt
    # 口径待确认是**标注**，不是需要用户回答的问题。
    assert "口径待确认" in prompt
    # 澄清只问一轮：问完就出候选计划，别让这次修改永远出不来。
    assert "不要再问第二轮" in prompt
    # 服务端把 confidence < 0.7 的计划改判成追问 —— 提示模型别自降信心。
    assert "0.7" in prompt
