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
    PlanClarificationRequired,
    PlanRevisionConflictError,
    PlanValidationError,
    ReportPlan,
    ReportPlanError,
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
        "grainUnit",
    }
    assert set(plan.model_dump(mode="json")["sections"][0]) == {
        "id",
        "title",
        "metricIntents",
        "componentType",
        "displayIntent",
        "explicitComponentType",
        "selectionReason",
        "columns",
    }


# ---------- 选型：服务端确定性定稿 ----------


def _section(title: str, component_type: str, **extra):
    section = {
        "title": title,
        "metricIntents": [title],
        "componentType": component_type,
    }
    section.update(extra)
    return section


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        ("total", "metric_card"),
        ("trend", "line_chart"),
        ("comparison", "bar_chart"),
        ("composition", "pie_chart"),
        ("records", "table"),
    ],
)
def test_component_type_is_selected_by_the_display_purpose(intent, expected):
    """看总量给指标卡、看变化给趋势图、比较对象给柱状图、构成给占比图、明细给表格。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("耗电情况", "metric_card", displayIntent=intent, metricIntents=["耗电"]),
    ]

    plan = apply_candidate(payload, plan_id="plan_001")

    assert plan.sections[0].componentType.value == expected
    assert plan.sections[0].displayIntent.value == intent
    assert plan.sections[0].selectionReason


def test_an_explicit_user_type_wins_over_the_display_purpose():
    """用户明确说了「用柱状图」就用柱状图。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("耗电情况", "metric_card", displayIntent="total", metricIntents=["耗电"]),
    ]

    plan = apply_candidate(
        payload, plan_id="plan_001", current_request="耗电情况用柱状图看"
    )

    assert plan.sections[0].componentType.value == "bar_chart"
    assert plan.sections[0].explicitComponentType.value == "bar_chart"
    assert "你明确指定" in plan.sections[0].selectionReason


def test_the_explicit_type_only_covers_the_section_it_names():
    """一条指令只覆盖它说清楚的那块板块，不套给所有板块。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("总耗电", "metric_card", displayIntent="total"),
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
    ]

    plan = apply_candidate(
        payload, plan_id="plan_001", current_request="各机房耗电对比用柱状图"
    )

    assert plan.sections[0].componentType.value == "metric_card"
    assert plan.sections[0].explicitComponentType is None
    assert plan.sections[1].componentType.value == "bar_chart"
    assert plan.sections[1].explicitComponentType.value == "bar_chart"


def test_an_ambiguous_explicit_type_asks_the_user():
    """指令对不上板块就澄清，不偷偷改别的板块。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("总耗电", "metric_card", displayIntent="total"),
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
    ]

    with pytest.raises(PlanClarificationRequired) as excinfo:
        apply_candidate(payload, plan_id="plan_001", current_request="用柱状图看")

    assert excinfo.value.question
    assert "柱状图" in excinfo.value.question


def test_the_selection_reason_is_written_into_the_assumptions():
    """系统选了什么、为什么选，写在方案的假设里，用户能看到并能改。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
    ]

    plan = apply_candidate(payload, plan_id="plan_001")

    assert any(
        line.startswith("选型：") and "柱状图" in line for line in plan.assumptions
    )


def test_the_model_choice_is_the_fallback_when_no_purpose_is_declared():
    """模型没给展示目的时，沿用它选的组件类型。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("PUE 总览", "metric_card", metricIntents=["PUE"]),
    ]

    plan = apply_candidate(payload, plan_id="plan_001")

    assert plan.sections[0].componentType.value == "metric_card"
    assert plan.sections[0].displayIntent is None
    assert "模型" in plan.sections[0].selectionReason


# ---------- 表格栏目 ----------


def test_the_model_columns_are_finalized_with_ids_and_field_refs():
    """模型给了栏目就照用，服务端补齐稳定 id 与取值键。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section(
            "当前告警列表",
            "table",
            displayIntent="records",
            columns=[{"label": "设备名称"}, {"label": "告警级别"}],
        )
    ]

    plan = apply_candidate(payload, plan_id="plan_001")

    columns = plan.sections[0].columns
    assert [column.label for column in columns] == ["设备名称", "告警级别"]
    assert [column.id for column in columns] == ["col_1", "col_2"]
    assert [column.fieldRef for column in columns] == [
        "section_1.col_1",
        "section_1.col_2",
    ]


def test_missing_columns_are_completed_by_the_system_and_shown_in_the_plan():
    """模型漏写栏目时系统替他补上，补的内容出现在方案里，用户能看见。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("当前告警列表", "table", displayIntent="records"),
    ]

    plan = apply_candidate(payload, plan_id="plan_001", current_request="给我一份告警列表")

    assert [column.label for column in plan.sections[0].columns] == [
        "设备名称",
        "时间",
        "告警级别",
    ]
    assert all(column.fieldRef for column in plan.sections[0].columns)
    assert any(
        line.startswith("栏目：") and "告警级别" in line for line in plan.assumptions
    )


def test_columns_that_cannot_be_guessed_ask_the_user_instead_of_guessing():
    """实在猜不出来才追问用户 —— 问的是具体问题，不是「请重述」。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("运维工单明细", "table", displayIntent="records"),
    ]

    with pytest.raises(PlanClarificationRequired) as excinfo:
        apply_candidate(payload, plan_id="plan_001")

    assert "运维工单明细" in excinfo.value.question
    assert "栏目" in excinfo.value.question


def test_a_broken_column_structure_never_produces_a_silent_empty_table():
    """模型给的栏目结构坏了：明确报错，不给一张空表还宣称成功。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("当前告警列表", "table", displayIntent="records", columns="设备名称,时间"),
    ]

    with pytest.raises(ReportPlanError):
        apply_candidate(payload, plan_id="plan_001")


def test_the_table_columns_survive_a_save_and_a_reread():
    """栏目从模型候选到方案保存、再读回来都不丢。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section(
            "当前告警列表",
            "table",
            displayIntent="records",
            columns=[{"label": "设备名称"}, {"label": "时间"}],
        )
    ]

    plan = apply_candidate(payload, plan_id="plan_001")
    reread = ReportPlan.model_validate(plan.model_dump(mode="json"))

    assert reread.sections[0].columns == plan.sections[0].columns
    assert reread.sections[0].displayIntent == plan.sections[0].displayIntent
    assert reread.sections[0].selectionReason == plan.sections[0].selectionReason


# ---------- 栏目身份 ----------


def _plan_with_columns() -> ReportPlan:
    payload = _candidate_payload()
    payload["sections"] = [
        _section(
            "当前告警列表",
            "table",
            displayIntent="records",
            columns=[{"label": "设备名称"}, {"label": "时间"}, {"label": "告警级别"}],
        )
    ]
    return apply_candidate(payload, plan_id="plan_001")


def test_renaming_a_column_keeps_its_identity_and_its_key():
    """改列名不改变栏目身份，取值键也不换。"""
    previous = _plan_with_columns()
    payload = _candidate_payload()
    payload["sections"] = [
        _section(
            "当前告警列表",
            "table",
            displayIntent="records",
            columns=[
                {"id": "col_1", "label": "设备名称"},
                {"id": "col_2", "label": "发生时间"},
                {"id": "col_3", "label": "告警级别"},
            ],
        )
    ]

    plan = apply_candidate(payload, previous=previous, expected_revision=1)

    renamed = plan.sections[0].columns[1]
    assert renamed.label == "发生时间"
    assert renamed.id == "col_2"
    assert renamed.fieldRef == "section_1.col_2"


def test_reordering_columns_keeps_ids_and_keys():
    """重排保留 ID 和键。"""
    previous = _plan_with_columns()
    payload = _candidate_payload()
    payload["sections"] = [
        _section(
            "当前告警列表",
            "table",
            displayIntent="records",
            columns=[
                {"id": "col_3", "label": "告警级别"},
                {"id": "col_1", "label": "设备名称"},
                {"id": "col_2", "label": "时间"},
            ],
        )
    ]

    plan = apply_candidate(payload, previous=previous, expected_revision=1)

    assert [(column.id, column.label, column.fieldRef) for column in plan.sections[0].columns] == [
        ("col_3", "告警级别", "section_1.col_3"),
        ("col_1", "设备名称", "section_1.col_1"),
        ("col_2", "时间", "section_1.col_2"),
    ]


def test_a_new_column_gets_a_new_key():
    """新增栏目生成新键，不动老栏目的键。"""
    previous = _plan_with_columns()
    payload = _candidate_payload()
    payload["sections"] = [
        _section(
            "当前告警列表",
            "table",
            displayIntent="records",
            columns=[
                {"id": "col_1", "label": "设备名称"},
                {"id": "col_2", "label": "时间"},
                {"id": "col_3", "label": "告警级别"},
                {"label": "处理状态"},
            ],
        )
    ]

    plan = apply_candidate(payload, previous=previous, expected_revision=1)

    added = plan.sections[0].columns[-1]
    assert added.label == "处理状态"
    assert added.id == "col_4"
    assert added.fieldRef == "section_1.col_4"
    assert plan.sections[0].columns[0].fieldRef == "section_1.col_1"


def test_duplicate_labels_keep_different_keys():
    """重复显示名可以保留，但取值键必须不同。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section(
            "当前告警列表",
            "table",
            displayIntent="records",
            columns=[{"label": "设备名称"}, {"label": "设备名称"}],
        )
    ]

    plan = apply_candidate(payload, plan_id="plan_001")

    columns = plan.sections[0].columns
    assert [column.label for column in columns] == ["设备名称", "设备名称"]
    assert columns[0].fieldRef != columns[1].fieldRef
    assert columns[0].id != columns[1].id


def test_duplicate_field_refs_are_fixed():
    """键重复必须修正。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section(
            "当前告警列表",
            "table",
            displayIntent="records",
            columns=[
                {"label": "设备名称", "fieldRef": "device"},
                {"label": "时间", "fieldRef": "device"},
            ],
        )
    ]

    plan = apply_candidate(payload, plan_id="plan_001")

    refs = [column.fieldRef for column in plan.sections[0].columns]
    assert len(set(refs)) == 2


# ---------- 时间粒度 ----------


def test_the_declared_grain_unit_is_kept():
    payload = _candidate_payload()
    payload["requirements"]["grainUnit"] = "HOUR"

    plan = apply_candidate(payload, plan_id="plan_001")

    assert plan.requirements.grainUnit.value == "HOUR"


def test_a_missing_grain_unit_is_presumed_and_written_down():
    """缺省粒度在确认前推定并写明，不在转换或渲染时临时收窄。"""
    payload = _candidate_payload()

    plan = apply_candidate(payload, plan_id="plan_001")

    assert plan.requirements.grainUnit.value == "DAY"
    assert any(
        line.startswith("粒度：") and "DAY" in line for line in plan.assumptions
    )


def test_the_grain_unit_is_independent_of_the_time_range():
    """时间范围是「最近 7 天」，粒度是「按天」—— 两回事。"""
    payload = _candidate_payload()
    payload["requirements"]["timeRange"] = "最近 7 天"

    plan = apply_candidate(payload, plan_id="plan_001", current_request="按小时看")

    assert plan.requirements.timeRange == "最近 7 天"
    assert plan.requirements.grainUnit.value == "HOUR"


def test_an_untouched_section_keeps_the_type_the_user_specified_before():
    """修改未涉及的板块保留已有指定；新指令只覆盖对应板块。"""
    previous_payload = _candidate_payload()
    previous_payload["sections"] = [
        _section("总耗电", "metric_card", displayIntent="total"),
        _section("各机房耗电对比", "bar_chart", displayIntent="comparison"),
    ]
    previous = apply_candidate(
        previous_payload,
        plan_id="plan_001",
        current_request="各机房耗电对比用柱状图",
    )
    assert previous.sections[1].explicitComponentType.value == "bar_chart"

    update = _candidate_payload()
    update["sections"] = [
        {"id": "section_1", "title": "总耗电", "metricIntents": ["总耗电"],
         "componentType": "metric_card", "displayIntent": "total"},
        {"id": "section_2", "title": "各机房耗电对比", "metricIntents": ["总耗电"],
         "componentType": "bar_chart", "displayIntent": "comparison"},
    ]

    plan = apply_candidate(
        update, previous=previous, expected_revision=1, current_request="主题改一下"
    )

    assert plan.sections[1].explicitComponentType.value == "bar_chart"
    assert plan.sections[1].componentType.value == "bar_chart"
    assert plan.sections[0].explicitComponentType is None


def test_saying_all_sections_covers_all_of_them():
    """用户明说「都用柱状图」才覆盖全部板块。"""
    payload = _candidate_payload()
    payload["sections"] = [
        _section("总耗电", "metric_card", displayIntent="total"),
        _section("各机房耗电对比", "metric_card", displayIntent="comparison"),
    ]

    plan = apply_candidate(
        payload, plan_id="plan_001", current_request="这两块都用柱状图"
    )

    assert [section.componentType.value for section in plan.sections] == [
        "bar_chart",
        "bar_chart",
    ]
