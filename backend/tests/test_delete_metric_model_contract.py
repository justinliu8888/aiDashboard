"""模型入口与规则入口**用同一套删除协议**（契约表 §4.3）。

这一组用例守三件事：

    1. 工具 schema 里真的有 ``DELETE_METRIC`` 与 ``deleteMetrics`` ——
       模型能按同一份契约提交；
    2. 规则路径与模型路径产出的是**同一种结构**（``EditPlan`` +
       ``EditOperation.deleteMetrics``），不是两套并行协议；
    3. **模型给的指标身份不被信任**：身份对不上（目标已经不在卡片里）时，
       服务端拒绝或追问，绝不按名字乱删、也绝不回退成删整张卡片；
    4. 确认卡上说的是中文（「删除指标」），不是把 ``DELETE_METRIC``
       这样的枚举值摆给用户。
"""
from __future__ import annotations

import json

import pytest

from app.agents.agentscope_report_agent import AgentScopeReportAgent
from app.schemas.edit_plan import EditIntent, EditPlan
from app.services.report_template_service import build_template_design
from app.services.template_definition_service import build_template_definition
from app.services.template_edit_engine import template_edit_engine
from app.tools.schemas import EditPlanPayload
from app.tools.submit_edit_plan_tool import run as submit_edit_plan
from app.tools.submission import bind_submission, take_submission
from app.tools.context import ToolScope

SCOPE = ToolScope(
    project_id="project_001",
    subject_ref="machine_room",
    subject_id="room_a",
)

REGION_ID = "region_1"
REGION_NAME = "核心运行指标总览"
MESSAGE = "核心运行指标总览删除实时功率这个指标 再增加一个冰箱功率的指标"


def _build():
    plan = {
        "topic": "A 机房能耗日报",
        "requirements": {
            "metrics": ["总耗电"], "subject": None,
            "timeRange": None, "exclusions": [],
        },
        "sections": [
            {
                "id": "section_total", "title": REGION_NAME,
                "metricIntents": ["总耗电", "实时功率"], "componentType": "metric_card",
            },
        ],
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }
    template = build_template_design(plan, subject_id="room_a")
    return build_template_definition(
        template.model_dump(mode="json"), plan, "room_a",
    )


@pytest.fixture()
def definition():
    return _build()


# --------------------------------------------------------------------------
# 1. 工具 schema：模型能按同一份契约提交
# --------------------------------------------------------------------------


def test_tool_schema_exposes_the_delete_metric_intent():
    """工具 JSON schema 里要有 DELETE_METRIC 与 deleteMetrics。"""
    schema = EditPlanPayload.model_json_schema()
    defs = schema.get("$defs") or {}
    assert "DELETE_METRIC" in (defs.get("EditIntent") or {}).get("enum", [])
    operation = defs.get("EditOperation") or {}
    assert "deleteMetrics" in (operation.get("properties") or {})
    deletion = defs.get("EditMetricDeletion") or {}
    # 身份字段齐了：稳定标识 + 显示名。
    assert set(deletion.get("properties") or {}) >= {
        "metricKey", "simulatedId", "fieldRef", "label", "isPrimary", "intent",
    }


def test_model_tool_accepts_a_delete_metric_payload():
    """模型按同一份契约提交 DELETE_METRIC —— 工具这一关就放行。"""
    bind_submission()
    result = submit_edit_plan(
        intent="DELETE_METRIC",
        target={"regionId": REGION_ID, "matchedText": REGION_NAME, "confidence": 0.9},
        operation={
            "deleteMetrics": [{
                "metricKey": "metric.power.section_total_2",
                "simulatedId": "simulated.section_total.2",
                "label": "实时功率",
            }],
            "baseDefinitionRevision": 0,
        },
        confidence=0.9,
        summary="在「核心运行指标总览」里删掉指标「实时功率」。",
    )

    assert result["accepted"] is True
    payload = (take_submission() or {})["payload"]
    assert payload["intent"] == "DELETE_METRIC"
    operation = payload["operation"]
    assert operation["deleteMetrics"][0]["metricKey"] == "metric.power.section_total_2"
    assert operation["deleteMetrics"][0]["label"] == "实时功率"


# --------------------------------------------------------------------------
# 2. 规则路径与模型路径是同一种结构
# --------------------------------------------------------------------------


def test_rule_path_and_model_path_share_the_same_shape(definition):
    """规则层产出的计划，形状与模型提交的那份**一致**。"""
    rule_plan = template_edit_engine.parse(
        message="核心运行指标总览删除实时功率这个指标",
        definition=definition,
        selected_region_id=None,
        scope=SCOPE,
    )
    model_plan = EditPlan.model_validate({
        "isTemplateEdit": True,
        "intent": "DELETE_METRIC",
        "confidence": 0.9,
        "target": {"regionId": REGION_ID, "matchedText": REGION_NAME, "confidence": 0.9},
        "operation": {
            "deleteMetrics": [{
                "metricKey": "metric.power.section_total_2",
                "simulatedId": "simulated.section_total.2",
                "label": "实时功率",
                "isPrimary": False,
            }],
            "baseDefinitionRevision": 0,
        },
    })

    assert rule_plan.intent == model_plan.intent == EditIntent.DELETE_METRIC
    rule_deletion = rule_plan.operation.deleteMetrics[0]
    model_deletion = model_plan.operation.deleteMetrics[0]
    assert rule_deletion.metricKey == model_deletion.metricKey
    assert rule_deletion.simulatedId == model_deletion.simulatedId
    assert rule_deletion.label == model_deletion.label


def test_rule_composite_uses_the_same_delete_structure(definition):
    """复合（删 + 加）里的删除那一步，结构与模型提交的一致。"""
    rule_plan = template_edit_engine.parse(
        message=MESSAGE,
        definition=definition,
        selected_region_id=None,
        scope=SCOPE,
    )

    delete_step = next(
        step for step in rule_plan.operations if step.intent == EditIntent.DELETE_METRIC
    )
    deletion = delete_step.operation.deleteMetrics[0]
    assert deletion.metricKey == "metric.power.section_total_2"
    assert deletion.simulatedId == "simulated.section_total.2"
    assert deletion.label == "实时功率"


def test_model_submitted_plan_is_applied_by_the_same_engine(definition):
    """模型提交的计划走的是**同一个** apply 路径，结果一致。"""
    model_plan = EditPlan.model_validate({
        "isTemplateEdit": True,
        "intent": "DELETE_METRIC",
        "confidence": 0.9,
        "target": {"regionId": REGION_ID, "matchedText": REGION_NAME, "confidence": 0.9},
        "operation": {
            "deleteMetrics": [{
                "metricKey": "metric.power.section_total_2",
                "simulatedId": "simulated.section_total.2",
                "label": "实时功率",
            }],
            "baseDefinitionRevision": 0,
        },
    })
    result = template_edit_engine.apply(
        plan=model_plan, definition=definition, scope=SCOPE,
    )

    assert result.needsClarification is False
    region = result.draftAfterEdit.regions[0]
    spec = (region.get("templateBinding") or {}).get("previewSpec") or {}
    labels = [str(item.get("label") or "") for item in (spec.get("metrics") or [])]
    assert labels == ["总耗电"]
    # 卡片还在。
    assert any(item["id"] == REGION_ID for item in result.draftAfterEdit.regions)


# --------------------------------------------------------------------------
# 3. 模型给的指标身份不被信任
# --------------------------------------------------------------------------


def test_model_identity_that_does_not_exist_is_refused(definition):
    """模型给了一个**对不上**的身份 → 服务端拒绝，不按名字乱删。"""
    model_plan = EditPlan.model_validate({
        "isTemplateEdit": True,
        "intent": "DELETE_METRIC",
        "confidence": 0.9,
        "target": {"regionId": REGION_ID, "matchedText": REGION_NAME, "confidence": 0.9},
        "operation": {
            "deleteMetrics": [{
                "metricKey": "metric.not.exists",
                "label": "总耗电",
            }],
            "baseDefinitionRevision": 0,
        },
    })
    result = template_edit_engine.apply(
        plan=model_plan, definition=definition, scope=SCOPE,
    )

    assert result.needsClarification is True
    assert result.changeSet is None
    assert result.draftAfterEdit.metadata["revision"] == 0
    # 卡片和它的指标都还在。
    spec = (result.draftAfterEdit.regions[0].get("templateBinding") or {}).get("previewSpec") or {}
    labels = [str(item.get("label") or "") for item in (spec.get("metrics") or [])]
    assert labels == ["总耗电", "实时功率"]


def test_model_without_identity_is_matched_by_label_and_must_be_unique(definition):
    """模型只给名字时按名字找，但必须**唯一**。"""
    model_plan = EditPlan.model_validate({
        "isTemplateEdit": True,
        "intent": "DELETE_METRIC",
        "confidence": 0.9,
        "target": {"regionId": REGION_ID, "matchedText": REGION_NAME, "confidence": 0.9},
        "operation": {
            "deleteMetrics": [{"label": "实时功率"}],
            "baseDefinitionRevision": 0,
        },
    })
    result = template_edit_engine.apply(
        plan=model_plan, definition=definition, scope=SCOPE,
    )

    assert result.needsClarification is False
    spec = (result.draftAfterEdit.regions[0].get("templateBinding") or {}).get("previewSpec") or {}
    labels = [str(item.get("label") or "") for item in (spec.get("metrics") or [])]
    assert labels == ["总耗电"]


def test_model_cannot_turn_a_metric_delete_into_a_card_delete(definition):
    """模型提交 DELETE_METRIC 时，卡片**必须**还在。"""
    model_plan = EditPlan.model_validate({
        "isTemplateEdit": True,
        "intent": "DELETE_METRIC",
        "confidence": 0.9,
        "target": {"regionId": REGION_ID, "matchedText": REGION_NAME, "confidence": 0.9},
        "operation": {
            "deleteMetrics": [{
                "metricKey": "metric.power.section_total_2",
                "label": "实时功率",
            }],
            "baseDefinitionRevision": 0,
        },
    })
    result = template_edit_engine.apply(
        plan=model_plan, definition=definition, scope=SCOPE,
    )

    assert [item["id"] for item in result.draftAfterEdit.regions] == [REGION_ID]


# --------------------------------------------------------------------------
# 4. 确认卡上说的是中文，不是枚举值
# --------------------------------------------------------------------------


def test_react_step_description_is_chinese():
    """ReAct 层给确认卡的描述是中文，不把 DELETE_METRIC 摆给用户。"""
    described = AgentScopeReportAgent._describe_edit_step({
        "intent": "DELETE_METRIC",
        "operation": {
            "deleteMetrics": [{"label": "实时功率"}],
        },
    })

    assert "实时功率" in described
    assert "DELETE_METRIC" not in described
    assert "卡片保留" in described


def test_prompt_tells_the_model_about_metric_deletion():
    """模型提示词里写明了删指标与删卡片的区别。"""
    from pathlib import Path

    prompt = (
        Path(__file__).resolve().parents[1]
        / "app" / "agents" / "prompts" / "template_edit.md"
    ).read_text(encoding="utf-8")

    flat = "".join(prompt.split())
    assert "DELETE_METRIC" in prompt
    assert "deleteMetrics" in prompt
    # 明确写了「卡片本身保留」与「不要回退成删整张卡片」。
    assert "卡片本身保留" in flat
    assert "不要回退成删整张卡片" in flat
