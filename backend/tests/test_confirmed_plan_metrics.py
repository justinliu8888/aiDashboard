"""Confirmed metric intents must survive template conversion and preview."""
from copy import deepcopy

import pytest

from app.services.preview_service import build_preview_context, build_report_preview_payload
from app.services.report_template_service import build_template_design
from app.services.template_definition_service import build_template_definition


@pytest.mark.parametrize("intents", [
    ["实时功率", "总电费", "总实时功率", "运行中服务器数", "告警总数"],
    ["总耗电", "冷冻柜数量（台）", "试验线温度（℃）", "PUE"],
])
def test_confirmed_metrics_reach_the_simulated_preview(intents):
    plan = _plan(intents)
    template = build_template_design(plan, subject_id="room_a")
    definition = build_template_definition(
        template.model_dump(mode="json"), plan, "room_a"
    ).model_dump(mode="json")
    spec = definition["regions"][0]["templateBinding"]["previewSpec"]
    assert [m["label"] for m in spec["metrics"]] == intents
    assert len({m["metricKey"] for m in spec["metrics"]}) == len(intents)
    preview = build_report_preview_payload(definition, build_preview_context(definition))
    metrics = preview["regions"][0]["payload"]["metrics"]
    assert [m["label"] for m in metrics] == intents
    assert all(isinstance(m["value"], (int, float)) for m in metrics)
    assert [m["unit"] for m in metrics] == [m["unit"] or "" for m in spec["metrics"]]
    if "总电费" in intents:
        assert next(m for m in metrics if m["label"] == "总电费")["unit"] == "元"


def test_confirmed_plan_takes_precedence_over_stale_template_intents():
    plan = _plan(["总电费"])
    template = build_template_design(plan, subject_id="room_a").model_dump(mode="json")
    confirmed = deepcopy(plan)
    confirmed["sections"][0]["metricIntents"].append("新流水线数量（条）")
    definition = build_template_definition(template, confirmed, "room_a")
    metrics = definition.regions[0]["templateBinding"]["previewSpec"]["metrics"]
    assert [m["label"] for m in metrics] == confirmed["sections"][0]["metricIntents"]


def _plan(intents):
    return {
        "id": "rplan_confirmed_metrics", "revision": 1, "topic": "运行日报",
        "requirements": {"metrics": intents, "subject": "A 机房", "timeRange": "昨日"},
        "sections": [{
            "id": "section_1", "title": "核心运行指标总览",
            "componentType": "metric_card", "metricIntents": intents,
        }],
        "assumptions": [], "suggestions": [], "referenceTemplates": [],
    }
