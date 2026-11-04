import copy

import pytest

from tests.test_night_bug_regressions import _definition, TOTAL_ID, TREND_ID, RANKING_ID
from app.schemas.edit_plan import EditIntent
from app.services.template_edit_engine import template_edit_engine
from app.services.report_presentation_service import apply_presentation, find_region


@pytest.mark.parametrize("selected", [None, TOTAL_ID, RANKING_ID])
def test_short_member_names_create_group_without_missing_group_prompt(selected):
    definition = _definition()
    before = definition.model_dump(mode="json")
    plan = template_edit_engine.parse(
        message='把总览和趋势放进“能耗概览”分组',
        definition=definition, selected_region_id=selected,
    )
    assert plan.intent == EditIntent.APPLY_PRESENTATION
    assert not plan.needsClarification
    ops = plan.operation.presentationOperations
    assert len(ops) == 1 and ops[0]["op"] == "CREATE_GROUP"
    params = ops[0]["params"]
    assert params["name"] == "能耗概览"
    assert params["memberRegionIds"] == [TOTAL_ID, TREND_ID]
    assert definition.model_dump(mode="json") == before
    result = apply_presentation(definition, ops)
    assert result.ok, result.failure
    group = find_region(result.definition, params["groupId"])
    assert [r["id"] for r in group["children"]] == [TOTAL_ID, TREND_ID]
    for region_id in [TOTAL_ID, TREND_ID, RANKING_ID]:
        for field in ["data", "binding", "context", "component"]:
            assert find_region(result.definition, region_id)[field] == \
                find_region(definition, region_id)[field]


def test_ambiguous_short_member_does_not_create_group():
    definition = _definition()
    duplicate = copy.deepcopy(find_region(definition, TREND_ID))
    duplicate["id"] = "another_trend"
    duplicate["name"] = "另一个能耗趋势"
    definition.regions.append(duplicate)
    plan = template_edit_engine.parse(
        message='把总览和趋势放进“能耗概览”分组', definition=definition,
        selected_region_id=TOTAL_ID,
    )
    assert not plan.operation.presentationOperations


def test_screenshot_card_names_resolve_short_members():
    definition = _definition()
    find_region(definition, TOTAL_ID)["name"] = "昨日总耗电"
    find_region(definition, TREND_ID)["name"] = "昨日能耗趋势"
    plan = template_edit_engine.parse(
        message='把总览和趋势放进“能耗概览”分组', definition=definition,
        selected_region_id=None,
    )
    assert plan.operation.presentationOperations[0]["params"]["memberRegionIds"] == \
        [TOTAL_ID, TREND_ID]
