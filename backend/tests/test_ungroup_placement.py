from app.services.report_presentation_service import apply_presentation, find_region
from tests.test_night_bug_regressions import _definition, TOTAL_ID, TREND_ID, RANKING_ID


def test_ungroup_promotes_members_into_page_coordinates_without_overlap():
    definition = _definition()
    definition.page = {"arrangement": {"mode": "GRID", "columns": 12}}
    grouped = apply_presentation(definition, [{"op": "CREATE_GROUP", "params": {
        "groupId": "overview", "name": "能耗概览",
        "memberRegionIds": [TOTAL_ID, TREND_ID],
        "arrangement": {"mode": "GRID", "columns": 1},
    }}])
    assert grouped.ok
    grouped_definition = grouped.definition.model_dump(mode="json")
    group = find_region(grouped_definition, "overview")
    group["layout"]["placement"] = {
        "type": "GRID", "column": 1, "columnSpan": 12, "row": 1, "rowSpan": 1,
    }
    ranking = find_region(grouped_definition, RANKING_ID)
    ranking["layout"]["placement"] = {
        "type": "GRID", "column": 1, "columnSpan": 12, "row": 2, "rowSpan": 1,
    }
    before = grouped.definition.model_validate(grouped_definition)
    result = apply_presentation(before, [{"op": "UNGROUP", "params": {
        "groupId": "overview",
    }}])
    assert result.ok, result.failure
    assert before.model_dump(mode="json") == grouped_definition
    assert find_region(result.definition, "overview") is None
    for index, region_id in enumerate([TOTAL_ID, TREND_ID, RANKING_ID]):
        card = find_region(result.definition, region_id)
        placement = card["layout"]["placement"]
        assert placement["columnSpan"] == 12
        assert placement["row"] == index + 1
        for field in ["data", "binding", "context", "component"]:
            assert card[field] == find_region(before, region_id)[field]


def test_ungroup_keeps_automatic_rows_and_uses_parent_width():
    definition = _definition()
    definition.page = {"arrangement": {"mode": "GRID", "columns": 12}}
    grouped = apply_presentation(definition, [{"op": "CREATE_GROUP", "params": {
        "groupId": "overview", "name": "Overview",
        "memberRegionIds": [TOTAL_ID, TREND_ID],
        "arrangement": {"mode": "GRID", "columns": 1},
    }}])
    assert grouped.ok
    result = apply_presentation(grouped.definition, [{"op": "UNGROUP", "params": {
        "groupId": "overview",
    }}])
    assert result.ok, result.failure
    for region_id in [TOTAL_ID, TREND_ID, RANKING_ID]:
        placement = find_region(result.definition, region_id)["layout"]["placement"]
        assert placement["columnSpan"] == 12
        assert "row" not in placement
