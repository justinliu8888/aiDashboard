"""Chinese appearance entry regressions, using only isolated test storage."""
import copy

import pytest

from app.schemas.edit_plan import EditIntent
from app.schemas.report_definition import ReportDefinitionDraft
from app.services import routing_rules
from app.services.draft_workspace import draft_workspace
from app.services.session_service import session_service
from app.services.template_edit_engine import template_edit_engine


ORIGINAL = "把‘昨日能耗总览’的文字调大，右对齐，圆角调小一点"


def _definition():
    return ReportDefinitionDraft.model_validate({
        "metadata": {"name": "能耗日报", "revision": 3},
        "regions": [
            {"id": "overview", "name": "昨日能耗总览", "role": "SUMMARY",
             "component": {"type": "METRIC_CARD"},
             "style": {"fontSize": {"value": 16, "unit": "PX"}, "borderRadius": 12}},
            {"id": "other", "name": "其他能耗", "role": "SUMMARY",
             "component": {"type": "METRIC_CARD"}},
        ],
    })


def _parse(message=ORIGINAL, definition=None, selected=None):
    return template_edit_engine.parse(
        message=message, definition=definition or _definition(), selected_region_id=selected,
    )


def test_original_produces_one_style_candidate_without_mutation():
    definition = _definition()
    before = copy.deepcopy(definition.model_dump())
    plan = _parse(definition=definition, selected="other")
    assert plan.intent == EditIntent.APPLY_PRESENTATION
    assert not plan.needsClarification
    assert plan.operation.presentationOperations == [{
        "op": "SET_STYLE", "params": {"regionId": "overview", "style": {
            "fontSize": {"value": 18, "unit": "PX"},
            "textAlign": "RIGHT", "borderRadius": 10,
        }},
    }]
    assert "18" in plan.summary and "10" in plan.summary and "右对齐" in plan.summary
    assert definition.model_dump() == before


@pytest.mark.parametrize("selected", [None, "overview", "other"])
def test_quoted_overview_alias_resolves_unique_energy_summary(selected):
    definition = _definition()
    definition.regions[0]["name"] = "昨日总耗电"
    definition.regions[0]["component"]["metric"] = {"label": "昨日总耗电", "fieldRef": "energyKwh"}
    before = copy.deepcopy(definition.model_dump())
    plan = _parse(definition=definition, selected=selected)
    assert plan.intent == EditIntent.APPLY_PRESENTATION
    assert plan.operation.presentationOperations[0]["params"]["regionId"] == "overview"
    assert set(plan.operation.presentationOperations[0]["params"]["style"]) == {"fontSize", "textAlign", "borderRadius"}
    assert definition.model_dump() == before


@pytest.mark.parametrize("selected", [None, "overview"])
def test_ambiguous_energy_overview_requires_clarification_even_with_selection(selected):
    definition = _definition()
    definition.regions[0]["name"] = "昨日总耗电"
    second = copy.deepcopy(definition.regions[0])
    second.update(id="second_summary", name="昨日总能耗")
    definition.regions.append(second)
    plan = _parse(definition=definition, selected=selected)
    assert plan.needsClarification
    assert not plan.operation.presentationOperations
    assert "昨日总耗电" in plan.clarificationQuestion
    assert "昨日总能耗" in plan.clarificationQuestion


def test_unknown_quoted_name_does_not_fall_back_to_selection():
    plan = _parse(ORIGINAL.replace("昨日能耗总览", "昨日电费总览"), selected="overview")
    assert plan.needsClarification
    assert not plan.operation.presentationOperations


@pytest.mark.parametrize("message,style", [
    ("文字调小一点", {"fontSize": {"value": 14, "unit": "PX"}}),
    ("字号设为24像素，左对齐，圆角设为0", {
        "fontSize": {"value": 24, "unit": "PX"}, "textAlign": "LEFT", "borderRadius": 0}),
    ("字体调大一点；居中对齐；圆角调大一点", {
        "fontSize": {"value": 18, "unit": "PX"}, "textAlign": "CENTER", "borderRadius": 14}),
])
def test_selected_target_and_absolute_or_relative_values(message, style):
    plan = _parse(message, selected="overview")
    assert plan.intent == EditIntent.APPLY_PRESENTATION
    assert plan.operation.presentationOperations[0]["params"]["style"] == style


@pytest.mark.parametrize("message", [ORIGINAL, ORIGINAL + "，可以吗？", "右对齐", "圆角调小一点"])
def test_appearance_is_a_command_not_readonly_or_feedback(message):
    assert routing_rules.has_operation_command(message)
    assert not routing_rules.is_readonly_question(message)
    assert not routing_rules.detect_issue_feedback(message, subject_required=False)


@pytest.mark.parametrize("message", ["文字大小能调整吗？", "圆角是什么？", "支持右对齐吗？"])
def test_appearance_consultation_is_not_a_command(message):
    assert not routing_rules.has_operation_command(message)


def test_missing_target_does_not_choose_a_region():
    assert _parse("文字调大，右对齐，圆角调小一点").needsClarification


def test_unknown_clause_does_not_silently_apply_only_alignment():
    plan = _parse("右对齐，圆角调成奇怪的形状", selected="overview")
    assert not plan.operation.presentationOperations


def test_relative_values_use_inherited_style_and_respect_limits():
    definition = _definition()
    definition.regions[0].pop("style")
    definition.regions = [{
        "id": "group", "name": "概览组", "component": {"type": "CONTAINER"},
        "style": {"fontSize": {"value": 199, "unit": "PX"}, "borderRadius": 1},
        "children": definition.regions,
    }]
    plan = _parse(definition=definition)
    assert plan.operation.presentationOperations[0]["params"]["style"] == {
        "fontSize": {"value": 200, "unit": "PX"}, "textAlign": "RIGHT", "borderRadius": 0,
    }


def test_invalid_absolute_size_is_rejected_atomically():
    definition = _definition()
    plan = _parse("字号设为201，右对齐", definition, "overview")
    result = template_edit_engine.apply(plan=plan, definition=definition)
    assert result.needsClarification
    assert result.changeSet is None
    assert result.draftAfterEdit.model_dump() == definition.model_dump()


def test_appearance_candidate_still_rejects_stale_acceptance(post_chat):
    definition = _definition().model_dump(mode="json")
    snapshots = {"reportDefinition": definition}
    post_chat(ORIGINAL, state="USER_EDITING_TEMPLATE", snapshots=snapshots)
    accepted = post_chat("接受修改", state="USER_EDITING_TEMPLATE", snapshots=snapshots).json()
    current = {"reportDefinition": accepted["domainObjects"]["reportDefinitionDraft"]}
    post_chat("修改", state="USER_EDITING_TEMPLATE", snapshots=current)
    candidate = post_chat(ORIGINAL, state="USER_EDITING_TEMPLATE", snapshots=current).json()
    assert candidate["domainObjects"]["pendingEditPlan"] is not None
    stale = post_chat("接受修改", state="USER_EDITING_TEMPLATE", snapshots=snapshots).json()
    assert stale["error"]["code"] == "STALE_EDIT_PLAN"
    assert session_service.load_pending_plan("conv_test") is None
    assert session_service.load_snapshot("conv_test")["definition"] == current["reportDefinition"]


def test_chat_acceptance_cancel_and_undo_gate(post_chat, client):
    definition = _definition().model_dump(mode="json")
    snapshots = {"reportDefinition": definition}
    candidate = post_chat(ORIGINAL, state="USER_EDITING_TEMPLATE", snapshots=snapshots).json()
    assert candidate["domainObjects"]["pendingEditPlan"] is not None
    assert candidate["domainObjects"]["changeSet"] is None
    assert candidate["domainObjects"]["reportDefinitionDraft"] == definition
    assert candidate["questions"][0]["selectionOnly"] is True
    draft_id = session_service.load_snapshot("conv_test")["draftId"]
    baseline = draft_workspace.load_draft(draft_id)
    assert not baseline.canUndo

    cancelled = post_chat("取消", state="USER_EDITING_TEMPLATE", snapshots=snapshots).json()
    assert cancelled["domainObjects"]["pendingEditPlan"] is None
    assert cancelled["domainObjects"]["reportDefinitionDraft"] == definition
    post_chat(ORIGINAL, state="USER_EDITING_TEMPLATE", snapshots=snapshots)
    accepted = post_chat("接受修改", state="USER_EDITING_TEMPLATE", snapshots=snapshots).json()
    after = accepted["domainObjects"]["reportDefinitionDraft"]
    assert after["regions"][0]["style"] == {
        "fontSize": {"value": 18, "unit": "PX"}, "textAlign": "RIGHT", "borderRadius": 10,
    }
    assert after["regions"][1] == definition["regions"][1]
    assert after["metadata"]["revision"] == 4
    assert accepted["domainObjects"]["changeSet"] is not None
    applied = draft_workspace.load_draft(draft_id)
    assert applied.canUndo
    assert applied.revision == baseline.revision + 1

    again = post_chat("接受修改", state="USER_EDITING_TEMPLATE", snapshots=snapshots).json()
    assert again["domainObjects"]["changeSet"] is None
    undone = client.post(f"/api/drafts/{draft_id}/actions", json={"action": "UNDO"})
    assert undone.status_code == 200, undone.text
    restored = draft_workspace.load_draft(draft_id)
    assert restored.definition["regions"] == definition["regions"]
    assert restored.revision == applied.revision + 1
    assert not restored.canUndo
