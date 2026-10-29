"""apply_candidate 的领域行为测试（#19 A 子任务）。

这里验证的是**对协调者可见的行为**：

    - 新建方案从 revision 1 开始，方案 ID 由调用方给
    - 修改方案保持方案 ID 与已有板块 ID，revision 递增
    - 基础版本不匹配时给出版本冲突，而不是悄悄覆盖
    - 原对象（上一版方案、候选）都不就地修改
    - 未接受的建议不会自己变成板块
    - PUE、对象范围、时间与排除项在修改后仍然保留

期望值来自冻结契约与本文件的样例，不从实现里反推。
"""
from __future__ import annotations

import pytest

from app.schemas.report_plan import (
    PlanRevisionConflictError,
    PlanValidationError,
    ReportPlan,
)
from app.services.report_plan_service import apply_candidate


def _candidate_payload(**overrides):
    payload = {
        "topic": "PUE 专题报表",
        "requirements": {
            "metrics": ["PUE"],
            "subject": None,
            "timeRange": None,
            "exclusions": ["设备耗电排名"],
        },
        "sections": [
            {"title": "PUE 总览", "metricIntents": ["PUE"], "componentType": "metric_card"}
        ],
        "assumptions": ["PUE 统计口径待核实，数据待连接"],
        "suggestions": [],
        "referenceTemplates": [],
    }
    payload.update(overrides)
    return payload


def _previous_plan():
    return ReportPlan.model_validate(
        {
            "id": "plan_001",
            "revision": 1,
            "topic": "PUE 专题报表",
            "requirements": {
                "metrics": ["PUE"],
                "subject": None,
                "timeRange": None,
                "exclusions": ["设备耗电排名"],
            },
            "sections": [
                {
                    "id": "section_pue",
                    "title": "PUE 总览",
                    "metricIntents": ["PUE"],
                    "componentType": "metric_card",
                }
            ],
            "assumptions": ["PUE 统计口径待核实，数据待连接"],
            "suggestions": [],
            "referenceTemplates": [],
        }
    )


# ---------- 新建 ----------


def test_new_plan_starts_at_revision_one_with_the_given_plan_id():
    plan = apply_candidate(_candidate_payload(), plan_id="plan_001")

    assert isinstance(plan, ReportPlan)
    assert plan.id == "plan_001"
    assert plan.revision == 1
    assert plan.topic == "PUE 专题报表"


def test_new_plan_requires_the_caller_to_supply_a_plan_id():
    with pytest.raises(PlanValidationError) as excinfo:
        apply_candidate(_candidate_payload())

    assert excinfo.value.code == "PLAN_ID_REQUIRED"


def test_new_plan_rejects_a_non_null_expected_revision():
    with pytest.raises(PlanValidationError) as excinfo:
        apply_candidate(_candidate_payload(), plan_id="plan_001", expected_revision=1)

    assert excinfo.value.code == "PLAN_EXPECTED_REVISION_INVALID"


def test_new_plan_gives_every_section_an_id():
    payload = _candidate_payload()
    payload["sections"] = [
        {"title": "PUE 总览", "metricIntents": ["PUE"], "componentType": "metric_card"},
        {"title": "PUE 趋势", "metricIntents": ["PUE"], "componentType": "line_chart"},
    ]

    plan = apply_candidate(payload, plan_id="plan_001")

    section_ids = [section.id for section in plan.sections]
    assert all(section_ids)
    assert len(set(section_ids)) == len(section_ids)


# ---------- 修改 ----------


def test_update_keeps_the_plan_id_and_increments_the_revision():
    plan = apply_candidate(
        _candidate_payload(),
        previous=_previous_plan(),
        expected_revision=1,
    )

    assert plan.id == "plan_001"
    assert plan.revision == 2


def test_update_keeps_the_id_of_an_existing_section():
    payload = _candidate_payload()
    payload["sections"] = [
        {
            "id": "section_pue",
            "title": "PUE 总览",
            "metricIntents": ["PUE"],
            "componentType": "metric_card",
        }
    ]

    plan = apply_candidate(payload, previous=_previous_plan(), expected_revision=1)

    assert [section.id for section in plan.sections] == ["section_pue"]


def test_update_gives_a_new_section_an_id_of_its_own():
    payload = _candidate_payload()
    payload["sections"] = [
        {
            "id": "section_pue",
            "title": "PUE 总览",
            "metricIntents": ["PUE"],
            "componentType": "metric_card",
        },
        {"title": "PUE 趋势", "metricIntents": ["PUE"], "componentType": "line_chart"},
    ]

    plan = apply_candidate(payload, previous=_previous_plan(), expected_revision=1)

    section_ids = [section.id for section in plan.sections]
    assert section_ids[0] == "section_pue"
    assert section_ids[1]
    assert section_ids[1] != "section_pue"


def test_update_requires_the_expected_revision():
    with pytest.raises(PlanValidationError) as excinfo:
        apply_candidate(_candidate_payload(), previous=_previous_plan())

    assert excinfo.value.code == "PLAN_EXPECTED_REVISION_REQUIRED"


def test_update_with_a_stale_expected_revision_raises_a_conflict():
    with pytest.raises(PlanRevisionConflictError) as excinfo:
        apply_candidate(_candidate_payload(), previous=_previous_plan(), expected_revision=5)

    assert excinfo.value.code == "PLAN_REVISION_CONFLICT"
    assert excinfo.value.expected_revision == 5
    assert excinfo.value.actual_revision == 1


# ---------- 原对象不被就地修改 ----------


def test_update_does_not_modify_the_previous_plan():
    previous = _previous_plan()
    before = previous.model_dump(mode="json")

    payload = _candidate_payload()
    payload["sections"] = [
        {
            "id": "section_pue",
            "title": "PUE 总览（改）",
            "metricIntents": ["PUE"],
            "componentType": "metric_card",
        }
    ]
    apply_candidate(payload, previous=previous, expected_revision=1)

    assert previous.model_dump(mode="json") == before
    assert previous.revision == 1
    assert previous.sections[0].title == "PUE 总览"


def test_update_does_not_modify_the_candidate():
    from app.schemas.report_plan import PlanCandidate

    candidate = PlanCandidate.model_validate(_candidate_payload())
    before = candidate.model_dump(mode="json")

    apply_candidate(candidate, previous=_previous_plan(), expected_revision=1)

    assert candidate.model_dump(mode="json") == before


# ---------- 内容语义 ----------


def test_unaccepted_suggestions_do_not_become_sections():
    payload = _candidate_payload()
    payload["suggestions"] = ["可增加同比趋势板块"]

    plan = apply_candidate(payload, plan_id="plan_001")

    assert plan.suggestions == ["可增加同比趋势板块"]
    assert [section.title for section in plan.sections] == ["PUE 总览"]


def test_pue_scope_time_and_exclusions_survive_an_update():
    payload = _candidate_payload()
    payload["requirements"] = {
        "metrics": ["PUE"],
        "subject": "A 机房",
        "timeRange": "过去 7 天",
        "exclusions": ["设备耗电排名", "UPS 电池组"],
    }

    plan = apply_candidate(payload, previous=_previous_plan(), expected_revision=1)

    assert plan.requirements.metrics == ["PUE"]
    assert plan.requirements.subject == "A 机房"
    assert plan.requirements.timeRange == "过去 7 天"
    assert plan.requirements.exclusions == ["设备耗电排名", "UPS 电池组"]


# ---------- 板块 ID 与版本细节 ----------


def test_update_keeps_the_section_id_even_when_the_candidate_omits_it():
    payload = _candidate_payload()
    payload["sections"] = [
        {"title": "PUE 总览", "metricIntents": ["PUE"], "componentType": "metric_card"}
    ]

    plan = apply_candidate(payload, previous=_previous_plan(), expected_revision=1)

    assert [section.id for section in plan.sections] == ["section_pue"]


def test_update_rejects_a_plan_id_that_is_not_the_previous_one():
    with pytest.raises(PlanValidationError) as excinfo:
        apply_candidate(
            _candidate_payload(),
            previous=_previous_plan(),
            plan_id="plan_other",
            expected_revision=1,
        )

    assert excinfo.value.code == "PLAN_ID_MISMATCH"


def test_update_rejects_a_repeated_section_id():
    payload = _candidate_payload()
    payload["sections"] = [
        {
            "id": "section_pue",
            "title": "PUE 总览",
            "metricIntents": ["PUE"],
            "componentType": "metric_card",
        },
        {
            "id": "section_pue",
            "title": "PUE 趋势",
            "metricIntents": ["PUE"],
            "componentType": "line_chart",
        },
    ]

    with pytest.raises(PlanValidationError) as excinfo:
        apply_candidate(payload, previous=_previous_plan(), expected_revision=1)

    assert excinfo.value.code == "PLAN_DUPLICATE_SECTION_ID"


def test_update_accepts_a_previous_plan_loaded_from_json():
    previous = _previous_plan().model_dump(mode="json")

    plan = apply_candidate(
        _candidate_payload(),
        previous=previous,
        expected_revision=1,
    )

    assert plan.id == "plan_001"
    assert plan.revision == 2


def test_result_does_not_share_state_with_the_candidate():
    from app.schemas.report_plan import PlanCandidate

    candidate = PlanCandidate.model_validate(_candidate_payload())
    plan = apply_candidate(candidate, plan_id="plan_001")

    plan.requirements.metrics.append("总耗电")
    plan.sections[0].title = "改过的标题"

    assert candidate.requirements.metrics == ["PUE"]
    assert candidate.sections[0].title == "PUE 总览"


def test_result_serializes_with_the_frozen_top_level_field_set():
    plan = apply_candidate(_candidate_payload(), plan_id="plan_001")

    assert set(plan.model_dump(mode="json")) == {
        "id",
        "revision",
        "topic",
        "requirements",
        "sections",
        "assumptions",
        "suggestions",
        "referenceTemplates",
    }
    assert set(plan.model_dump(mode="json")["requirements"]) == {
        "metrics",
        "subject",
        "timeRange",
        "exclusions",
    }
    assert set(plan.model_dump(mode="json")["sections"][0]) == {
        "id",
        "title",
        "metricIntents",
        "componentType",
    }
