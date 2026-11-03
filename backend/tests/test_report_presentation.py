"""展示执行（契约 0002 §3）的专项测试。

只测**直接受影响**的范围：分组 / 取消分组 / 移动 / 复制 / 白名单 / 循环，
不堆纯枚举测试，也不跑全仓。
"""
from __future__ import annotations

import copy
from datetime import datetime

import pytest
from pydantic import ValidationError

from app.schemas.edit_plan import EditIntent, EditOperation, EditPlan
from app.schemas.report_definition import ReportDefinitionDraft
from app.schemas.report_presentation import PresentationOperation, PresentationResult
from app.services.report_presentation_service import (
    PresentationError,
    apply_presentation,
    check_deletable,
    collect_region_references,
    effective_simulation_identity,
    find_region,
    format_display_value,
    iter_leaves,
    iter_regions,
    region_authoritative_format,
)
from app.services.template_edit_engine import template_edit_engine
from app.services.validation_service import validate_definition


def _definition() -> ReportDefinitionDraft:
    return ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "测试报表", "revision": 3},
            "page": {"arrangement": {"mode": "GRID", "columns": 12}},
            "theme": {"themeRef": "theme_default", "version": "1.0"},
            "regions": [
                {
                    "id": "region_a",
                    "name": "总耗电概览",
                    "role": "SUMMARY",
                    "component": {"type": "METRIC_CARD"},
                    "templateBinding": {
                        "previewSpec": {
                            "metrics": [{"metricKey": "metric.energy.total", "label": "总耗电"}]
                        }
                    },
                },
                {
                    "id": "region_b",
                    "name": "设备耗电排名",
                    "role": "DETAIL",
                    "component": {"type": "TABLE"},
                },
                {
                    "id": "region_c",
                    "name": "能耗趋势",
                    "role": "TREND",
                    "component": {"type": "CHART"},
                },
            ],
        }
    )


def _create_group_ops(**overrides):
    params = {
        "groupId": "group_1",
        "name": "对比组",
        "memberRegionIds": ["region_a", "region_b"],
        "arrangement": {
            "mode": "GRID",
            "columns": 2,
            "direction": "HORIZONTAL",
            "gap": {"value": 16, "unit": "PX"},
        },
    }
    params.update(overrides)
    return [{"op": "CREATE_GROUP", "params": params}]


# --------------------------------------------------------------------------
# 分组
# --------------------------------------------------------------------------


def test_create_group_moves_regions_into_container_and_keeps_ids():
    result = apply_presentation(_definition(), _create_group_ops())

    assert result.ok is True
    assert result.failure == ""
    definition = result.definition
    container = find_region(definition, "group_1")
    assert container is not None
    assert container["component"]["type"] == "CONTAINER"
    assert container["component"]["arrangement"]["columns"] == 2
    assert container["component"]["arrangement"]["gap"]["value"] == 16
    # 成员在容器里，id 不变
    child_ids = [child["id"] for child in container["children"]]
    assert child_ids == ["region_a", "region_b"]
    # 顶层不再有这两个叶子
    top_ids = [region["id"] for region in definition.regions]
    assert "region_a" not in top_ids
    assert "region_b" not in top_ids
    # 组内的卡片**没有丢**
    assert {region["id"] for region in iter_leaves(definition)} == {
        "region_a",
        "region_b",
        "region_c",
    }
    # 统计口径没被动：组件类型不变
    assert find_region(definition, "region_a")["component"]["type"] == "METRIC_CARD"


def test_create_group_keeps_group_container_first_member_position():
    result = apply_presentation(_definition(), _create_group_ops())
    definition = result.definition
    # 容器插在第一个成员原来所在的位置（region_a 之前）
    assert definition.regions[0]["id"] == "group_1"
    assert definition.regions[1]["id"] == "region_c"


def test_invalid_operation_leaves_definition_untouched():
    before = _definition()
    result = apply_presentation(before, [{"op": "NOT_A_REAL_OP", "params": {}}])

    assert result.ok is False
    assert result.changeSet is None
    assert result.reason == "invalid_operation"
    assert result.definition.model_dump() == before.model_dump()


def test_out_of_range_columns_leaves_definition_untouched():
    before = _definition()
    ops = _create_group_ops()
    ops[0]["params"]["arrangement"]["columns"] = 99
    result = apply_presentation(before, ops)

    assert result.ok is False
    assert result.reason == "out_of_range"
    assert result.definition.model_dump() == before.model_dump()


def test_ungroup_keeps_child_regions():
    grouped = apply_presentation(_definition(), _create_group_ops()).definition
    result = apply_presentation(
        grouped, [{"op": "UNGROUP", "params": {"groupId": "group_1"}}]
    )

    assert result.ok is True
    definition = result.definition
    assert find_region(definition, "group_1") is None
    top_ids = [region["id"] for region in definition.regions]
    assert top_ids == ["region_a", "region_b", "region_c"]


def test_move_region_into_group_preserves_id_and_binding():
    grouped = apply_presentation(_definition(), _create_group_ops()).definition
    result = apply_presentation(
        grouped,
        [
            {
                "op": "MOVE_REGION",
                "params": {"regionId": "region_c", "targetGroupId": "group_1"},
            }
        ],
    )

    assert result.ok is True
    definition = result.definition
    container = find_region(definition, "group_1")
    assert [child["id"] for child in container["children"]] == [
        "region_a",
        "region_b",
        "region_c",
    ]
    moved = find_region(definition, "region_c")
    assert moved["id"] == "region_c"
    assert moved["component"]["type"] == "CHART"


def test_move_region_into_descendant_is_rejected_as_cycle():
    grouped = apply_presentation(_definition(), _create_group_ops()).definition
    # 先把 region_c 放进 group_1，再在它里面建一个子容器 group_2
    nested = apply_presentation(
        grouped,
        [
            {
                "op": "MOVE_REGION",
                "params": {"regionId": "region_c", "targetGroupId": "group_1"},
            },
            {
                "op": "CREATE_GROUP",
                "params": {
                    "groupId": "group_2",
                    "name": "里层组",
                    "memberRegionIds": ["region_c"],
                    "arrangement": {"mode": "GRID", "columns": 1},
                },
            },
        ],
    ).definition
    # group_2 真的在 group_1 里面
    outer = find_region(nested, "group_1")
    assert [child["id"] for child in outer["children"]] == ["region_a", "region_b", "group_2"]

    result = apply_presentation(
        nested,
        [
            {
                "op": "MOVE_REGION",
                "params": {"regionId": "group_1", "targetGroupId": "group_2"},
            }
        ],
    )
    assert result.ok is False
    assert result.reason == "cycle"
    assert result.definition.model_dump() == nested.model_dump()


# --------------------------------------------------------------------------
# 复制 / 数据身份
# --------------------------------------------------------------------------


def test_copy_region_new_id_same_simulation_identity():
    result = apply_presentation(
        _definition(),
        [
            {
                "op": "COPY_REGION",
                "params": {"regionId": "region_a", "newRegionId": "region_a_copy"},
            }
        ],
    )

    assert result.ok is True
    definition = result.definition
    source = find_region(definition, "region_a")
    copy = find_region(definition, "region_a_copy")
    assert copy is not None
    assert copy["id"] == "region_a_copy"
    # 数据身份沿用源的 → 数值一致
    assert effective_simulation_identity(copy) == effective_simulation_identity(source)
    # 统计口径不变
    assert copy["component"]["type"] == source["component"]["type"]
    assert copy["templateBinding"]["previewSpec"]["metrics"] == (
        source["templateBinding"]["previewSpec"]["metrics"]
    )
    # 复制件标记出来：不参与总计翻倍
    assert copy["templateBinding"]["previewSpec"]["duplicateOfRegionId"] == "region_a"


def test_legacy_region_without_simulation_identity_falls_back_to_region_id():
    definition = _definition()
    region = definition.regions[0]
    assert "simulationIdentity" not in region["templateBinding"]["previewSpec"]
    assert effective_simulation_identity(region) == "region_a"


# --------------------------------------------------------------------------
# 白名单与安全
# --------------------------------------------------------------------------


def test_style_value_whitelist_rejects_unsafe_css():
    before = _definition()
    result = apply_presentation(
        before,
        [
            {
                "op": "SET_STYLE",
                "params": {
                    "regionId": "region_a",
                    "style": {"backgroundColor": "url(javascript:alert(1))"},
                },
            }
        ],
    )
    assert result.ok is False
    assert result.reason in ("invalid_operation", "out_of_range")
    assert result.definition.model_dump() == before.model_dump()


def test_style_value_whitelist_rejects_unknown_key():
    result = apply_presentation(
        _definition(),
        [
            {
                "op": "SET_STYLE",
                "params": {"regionId": "region_a", "style": {"madeUpKey": "1"}},
            }
        ],
    )
    assert result.ok is False
    assert result.reason == "invalid_operation"


def test_metadata_only_exposes_name_description_tags():
    result = apply_presentation(
        _definition(),
        [{"op": "SET_METADATA", "params": {"status": "PUBLISHED"}}],
    )
    assert result.ok is False
    assert result.reason == "invalid_operation"


def test_set_metadata_updates_name():
    result = apply_presentation(
        _definition(),
        [{"op": "SET_METADATA", "params": {"name": "新名字"}}],
    )
    assert result.ok is True
    assert result.definition.metadata["name"] == "新名字"


# --------------------------------------------------------------------------
# 显示格式：单位只允许「当前单位或空」（契约 §3.5）
# --------------------------------------------------------------------------


def _unit_definition() -> ReportDefinitionDraft:
    return ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "测试报表", "revision": 3},
            "regions": [
                {
                    "id": "region_a",
                    "name": "总耗电概览",
                    "role": "SUMMARY",
                    "component": {
                        "type": "METRIC_CARD",
                        "metric": {
                            "id": "m_total",
                            "label": "总耗电",
                            "fieldRef": "energyKwh",
                            "format": {
                                "kind": "NUMBER",
                                "precision": 1,
                                "unitLabel": "kWh",
                            },
                        },
                    },
                }
            ],
        }
    )


def test_set_format_rejects_unit_conversion():
    before = _unit_definition()
    result = apply_presentation(
        before,
        [
            {
                "op": "SET_FORMAT",
                "params": {"regionId": "region_a", "format": {"unitLabel": "Wh"}},
            }
        ],
    )

    # kWh -> Wh 属于单位换算，本批不做：整批失败，定义原样不动
    assert result.ok is False
    assert result.reason == "out_of_range"
    assert result.changeSet is None
    assert result.definition.model_dump() == before.model_dump()


def test_set_format_accepts_current_unit_and_empty():
    kept = apply_presentation(
        _unit_definition(),
        [
            {
                "op": "SET_FORMAT",
                "params": {
                    "regionId": "region_a",
                    "format": {"kind": "NUMBER", "precision": 2, "unitLabel": "kWh"},
                },
            }
        ],
    )
    assert kept.ok is True
    assert find_region(kept.definition, "region_a")["format"]["unitLabel"] == "kWh"
    assert find_region(kept.definition, "region_a")["format"]["precision"] == 2

    cleared = apply_presentation(
        _unit_definition(),
        [
            {
                "op": "SET_FORMAT",
                "params": {"regionId": "region_a", "format": {"unitLabel": ""}},
            }
        ],
    )
    assert cleared.ok is True
    assert find_region(cleared.definition, "region_a")["format"]["unitLabel"] == ""


# --------------------------------------------------------------------------
# 契约 §3.2 的字段集是封闭的
# --------------------------------------------------------------------------


def test_presentation_result_field_set_matches_contract():
    assert set(PresentationResult.model_fields) == {
        "ok",
        "definition",
        "changeSet",
        "affectedRegionIds",
        "issues",
        "dataBindingChanged",
        "failure",
        "reason",
    }


def test_presentation_operation_forbids_extra_keys():
    with pytest.raises(ValidationError):
        PresentationOperation(op="SET_STYLE", params={}, unknownKey=1)


# --------------------------------------------------------------------------
# 引擎接线
# --------------------------------------------------------------------------


def test_engine_parses_group_request_into_presentation_plan():
    plan = template_edit_engine.parse(
        message="把前两张卡片组成一组并排显示，间距 16",
        definition=_definition(),
        selected_region_id=None,
    )

    assert plan.intent == EditIntent.APPLY_PRESENTATION
    operations = plan.operation.presentationOperations
    assert operations and operations[0]["op"] == "CREATE_GROUP"
    params = operations[0]["params"]
    assert params["memberRegionIds"] == ["region_a", "region_b"]
    assert params["arrangement"]["columns"] == 2
    assert params["arrangement"]["gap"]["value"] == 16
    assert plan.summary


def test_engine_applies_presentation_plan_atomically():
    definition = _definition()
    plan = template_edit_engine.parse(
        message="把前两张卡片组成一组并排显示，间距 16",
        definition=definition,
        selected_region_id=None,
    )
    result = template_edit_engine.apply(plan=plan, definition=definition)

    assert result.needsClarification is False
    assert result.changeSet is not None
    assert find_region(result.draftAfterEdit, "group_1") is not None


def test_engine_finds_region_inside_group():
    grouped = apply_presentation(_definition(), _create_group_ops()).definition
    plan = template_edit_engine.parse(
        message="把设备耗电排名改成新标题",
        definition=grouped,
        selected_region_id=None,
    )
    # 组内的卡片也能被点名（目标解析不再只看顶层）
    assert plan.target.regionId == "region_b"


def test_engine_edit_plan_carries_presentation_operations():
    plan = EditPlan(
        isTemplateEdit=True,
        intent=EditIntent.APPLY_PRESENTATION,
        operation=EditOperation(
            presentationOperations=[{"op": "UNGROUP", "params": {"groupId": "group_1"}}]
        ),
    )
    assert plan.operation.presentationOperations[0]["op"] == "UNGROUP"


def test_engine_parse_show_beats_hide_for_取消隐藏():
    plan = template_edit_engine.parse(
        message="取消隐藏设备耗电排名",
        definition=_definition(),
        selected_region_id=None,
    )

    assert plan.intent == EditIntent.APPLY_PRESENTATION
    operations = plan.operation.presentationOperations
    assert operations[0]["op"] == "SHOW_REGION"
    assert operations[0]["params"]["regionId"] == "region_b"


def test_engine_parse_ungroup_finds_named_group():
    grouped = apply_presentation(_definition(), _create_group_ops()).definition
    plan = template_edit_engine.parse(
        message="取消对比组的分组",
        definition=grouped,
        selected_region_id=None,
    )

    assert plan.intent == EditIntent.APPLY_PRESENTATION
    operations = plan.operation.presentationOperations
    assert operations[0]["op"] == "UNGROUP"
    assert operations[0]["params"]["groupId"] == "group_1"


# --------------------------------------------------------------------------
# 校验：容器（分组）落进定义后，组内卡片也要被校验
# --------------------------------------------------------------------------


def test_validation_accepts_grouped_definition():
    grouped = apply_presentation(_definition(), _create_group_ops()).definition
    result = validate_definition(grouped.model_dump())

    codes = {issue.code for issue in result.issues}
    # 分组本身与组内卡片都不该报结构问题
    assert "CONTAINER_CHILDREN_EMPTY" not in codes
    assert "CONTAINER_HAS_DATA" not in codes
    assert "CONTAINER_ARRANGEMENT_MISSING" not in codes
    assert "COMPONENT_TYPE_UNSUPPORTED" not in codes


def test_validation_reports_container_without_children_and_with_data():
    broken = {
        "metadata": {"name": "测试报表"},
        "datasets": {"ds_1": {"type": "DATASET"}},
        "regions": [
            {
                "id": "group_1",
                "name": "坏分组",
                "role": "DETAIL",
                "component": {"type": "CONTAINER"},
                "data": {"inputs": {"main": {"type": "DATASET"}}},
                "children": [],
            }
        ],
    }
    result = validate_definition(broken)

    by_code = {issue.code: issue for issue in result.issues}
    assert "CONTAINER_CHILDREN_EMPTY" in by_code
    assert "CONTAINER_HAS_DATA" in by_code
    assert "CONTAINER_ARRANGEMENT_MISSING" in by_code
    # 路径落在容器自己的位置上
    assert by_code["CONTAINER_CHILDREN_EMPTY"].path == "/regions/0/children"


# --------------------------------------------------------------------------
# 删除前的引用影响：被引用的卡片删不得（除非强制删除）
# --------------------------------------------------------------------------


def _referenced_definition() -> ReportDefinitionDraft:
    """一份带**跨卡片引用**的定义。

    * ``region_b`` 在 ``templateBinding.dependencies`` 里显式引用 ``region_a``；
    * ``region_c`` 与 ``region_a`` **共用同一 metricRef**。
    """
    return ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "测试报表", "revision": 3},
            "page": {"arrangement": {"mode": "GRID", "columns": 12}},
            "theme": {"themeRef": "theme_default", "version": "1.0"},
            "regions": [
                {
                    "id": "region_a",
                    "name": "总耗电概览",
                    "role": "SUMMARY",
                    "component": {"type": "METRIC_CARD"},
                    "templateBinding": {
                        "metricRefs": ["metric.machine_room.total_energy"]
                    },
                },
                {
                    "id": "region_b",
                    "name": "设备耗电排名",
                    "role": "DETAIL",
                    "component": {"type": "TABLE"},
                    "templateBinding": {"dependencies": ["region_a"]},
                },
                {
                    "id": "region_c",
                    "name": "能耗趋势",
                    "role": "TREND",
                    "component": {"type": "CHART"},
                    "data": {
                        "pipeline": {
                            "nodes": [
                                {
                                    "id": "node_1",
                                    "type": "METRIC",
                                    "metricRef": "metric.machine_room.total_energy",
                                }
                            ]
                        }
                    },
                },
            ],
        }
    )


def test_delete_referenced_region_is_blocked_with_reference_details():
    before = _referenced_definition()

    with pytest.raises(PresentationError) as excinfo:
        check_deletable(before, ["region_a"])

    error = excinfo.value
    assert error.code == "reference_blocked"
    assert error.reason == "reference_blocked"
    # 消息里点名引用方（名称 + regionId）
    assert "设备耗电排名" in error.message
    assert "region_b" in error.message
    assert "能耗趋势" in error.message
    assert "region_c" in error.message
    # 结构化引用清单
    references = error.details["references"]
    assert {item["regionId"] for item in references} == {"region_b", "region_c"}
    assert {item["name"] for item in references} == {"设备耗电排名", "能耗趋势"}
    for item in references:
        assert {"regionId", "name", "reason"} <= set(item)
        assert item["reason"]


def test_delete_referenced_region_without_force_leaves_definition_untouched():
    before = _referenced_definition()
    snapshot = before.model_dump()

    result = apply_presentation(
        before, [{"op": "DELETE_REGION", "params": {"regionId": "region_a"}}]
    )

    assert result.ok is False
    assert result.reason == "reference_blocked"
    # 定义**逐字节未变**
    assert result.definition.model_dump() == snapshot
    assert before.model_dump() == snapshot
    # 引用清单进 issues（契约 §3.2 的字段集是封闭的，结构化信息走这里）
    failure_issue = result.issues[-1]
    assert failure_issue["code"] == "reference_blocked"
    assert {
        item["regionId"] for item in failure_issue["details"]["references"]
    } == {"region_b", "region_c"}


def test_force_delete_marks_referencing_regions_with_broken_reference():
    result = apply_presentation(
        _referenced_definition(),
        [{"op": "DELETE_REGION", "params": {"regionId": "region_a", "force": True}}],
    )

    assert result.ok is True
    definition = result.definition
    # 被删的卡片没了
    assert find_region(definition, "region_a") is None
    # 其余卡片完好
    assert {region["id"] for region in iter_leaves(definition)} == {
        "region_b",
        "region_c",
    }

    explicit = find_region(definition, "region_b")["errors"][-1]
    assert explicit["code"] == "broken_reference"
    assert explicit["refRegionId"] == "region_a"
    assert "总耗电概览" in explicit["message"]

    shared = find_region(definition, "region_c")["errors"][-1]
    assert shared["code"] == "broken_reference"
    assert shared["refRegionId"] == "region_a"


def test_force_delete_appends_to_existing_errors_instead_of_overwriting():
    definition = _referenced_definition()
    definition.regions[1]["errors"] = [{"code": "other", "message": "原有的问题"}]

    result = apply_presentation(
        definition,
        [{"op": "DELETE_REGION", "params": {"regionId": "region_a", "force": True}}],
    )

    errors = find_region(result.definition, "region_b")["errors"]
    assert errors[0] == {"code": "other", "message": "原有的问题"}
    assert errors[-1]["code"] == "broken_reference"


def test_collect_region_references_lists_only_external_regions():
    definition = _referenced_definition()

    references = collect_region_references(definition, ["region_a"])

    assert {item["regionId"] for item in references} == {"region_b", "region_c"}
    # 自己子树里的区域不算引用方
    assert "region_a" not in {item["regionId"] for item in references}


def test_delete_group_reports_external_references_and_keeps_definition():
    grouped = apply_presentation(
        _referenced_definition(), _create_group_ops()
    ).definition
    snapshot = grouped.model_dump()

    result = apply_presentation(
        grouped, [{"op": "DELETE_GROUP", "params": {"groupId": "group_1"}}]
    )

    assert result.ok is False
    assert result.reason == "reference_blocked"
    # 消息里说清楚「有没有外部区域引用这个组或它的子卡」
    assert "能耗趋势" in result.failure
    assert "region_c" in result.failure
    # 定义逐字节未变
    assert result.definition.model_dump() == snapshot
    assert grouped.model_dump() == snapshot


def test_force_delete_group_removes_children_and_marks_referencing_regions():
    grouped = apply_presentation(
        _referenced_definition(), _create_group_ops()
    ).definition

    result = apply_presentation(
        grouped,
        [{"op": "DELETE_GROUP", "params": {"groupId": "group_1", "force": True}}],
    )

    assert result.ok is True
    definition = result.definition
    # 整组连子卡一起没了
    assert find_region(definition, "group_1") is None
    assert find_region(definition, "region_a") is None
    assert find_region(definition, "region_b") is None
    # 组外的卡片完好，只是被标成失效引用
    outside = find_region(definition, "region_c")
    assert outside is not None
    assert outside["errors"][-1]["code"] == "broken_reference"
    assert outside["errors"][-1]["refRegionId"] == "region_a"


def test_ungroup_after_force_delete_keeps_remaining_cards():
    """回归保护：取消分组**保留子卡**（强制删掉一张之后也一样）。"""
    grouped = apply_presentation(
        _referenced_definition(), _create_group_ops()
    ).definition
    deleted = apply_presentation(
        grouped,
        [{"op": "DELETE_REGION", "params": {"regionId": "region_a", "force": True}}],
    ).definition

    result = apply_presentation(
        deleted, [{"op": "UNGROUP", "params": {"groupId": "group_1"}}]
    )

    assert result.ok is True
    assert find_region(result.definition, "group_1") is None
    top_ids = [region["id"] for region in result.definition.regions]
    assert top_ids == ["region_b", "region_c"]


# --------------------------------------------------------------------------
# 校验：带失效引用标记的区域**不阻断**校验
# --------------------------------------------------------------------------


def _valid_definition_payload() -> dict:
    return {
        "metadata": {"name": "测试报表", "revision": 3},
        "defaults": {"scope": {"subjectRef": "machine_room"}},
        "datasets": {
            "ds_energy": {
                "sourceRef": "catalog.machine_room.energy",
                "fields": {"energyKwh": {"dataType": "DECIMAL"}},
            }
        },
        "regions": [
            {
                "id": "region_a",
                "name": "总耗电概览",
                "role": "SUMMARY",
                "component": {"type": "METRIC_CARD"},
            },
            {
                "id": "region_b",
                "name": "设备耗电排名",
                "role": "DETAIL",
                "component": {"type": "TABLE"},
            },
        ],
    }


def test_region_errors_do_not_block_validation():
    payload = _valid_definition_payload()
    payload["regions"][1]["errors"] = [
        {
            "code": "broken_reference",
            "message": "这张卡片引用了已删除的卡片「总耗电概览」（region_a），引用已失效。",
            "refRegionId": "region_a",
        }
    ]

    result = validate_definition(payload)

    # 页面要可用、这份「错误草稿」要能存 → 校验**不能**因此失败
    assert result.passed is True, [issue.message for issue in result.issues]
    # 但它是**提示级**：问题记下来了，只是不阻断
    codes = {issue.code for issue in result.issues}
    assert "REGION_BROKEN_REFERENCE" in codes


def test_region_errors_key_is_not_treated_as_unknown_key():
    clean = validate_definition(_valid_definition_payload())
    with_errors_payload = _valid_definition_payload()
    with_errors_payload["regions"][1]["errors"] = [
        {"code": "broken_reference", "message": "引用已失效。", "refRegionId": "region_a"}
    ]
    with_errors = validate_definition(with_errors_payload)

    assert clean.passed is True
    assert with_errors.passed is True
    # 只多出一条提示，不多出阻断问题
    assert len(with_errors.issues) == len(clean.issues) + 1


# --------------------------------------------------------------------------
# 共享主题：保存主题（未确认不落库，确认才提交）
# --------------------------------------------------------------------------


class _FakeThemeStorage:
    """假的主题存储：只在内存里，**绝不碰用户库** ``backend/data/haewu.db``。"""

    def __init__(self, affected: list[dict] | None = None) -> None:
        self.themes: dict[str, dict] = {}
        self.affected = affected or []

    def list_theme_ids(self) -> list[str]:
        return list(self.themes)

    def load_theme(self, theme_id: str) -> dict | None:
        return copy.deepcopy(self.themes.get(theme_id))

    def save_theme(
        self,
        theme_id: str,
        *,
        name: str,
        tokens: dict,
        default_style: dict,
        theme_revision: int,
    ) -> dict:
        theme = {
            "themeId": theme_id,
            "name": name,
            "tokens": tokens,
            "defaultStyle": default_style,
            "themeRevision": theme_revision,
            "builtin": False,
            "createdAt": "2026-11-02T00:00:00+08:00",
            "updatedAt": "2026-11-02T00:00:00+08:00",
        }
        self.themes[theme_id] = theme
        return copy.deepcopy(theme)

    def affected_drafts(self, theme_id: str) -> list[dict]:
        return list(self.affected)


@pytest.fixture
def fake_theme_storage():
    from app.services.report_theme_service import theme_service

    original = theme_service.storage
    storage = _FakeThemeStorage(
        affected=[{"draftId": "draft_1", "name": "机房能耗日报"}]
    )
    theme_service.set_storage(storage)
    try:
        yield storage
    finally:
        theme_service.set_storage(original)


def test_save_theme_without_confirmation_asks_and_creates_nothing(fake_theme_storage):
    definition = _definition()

    plan = template_edit_engine.parse(
        message="把这个主题存成 蓝色主题",
        definition=definition,
        selected_region_id=None,
    )

    assert plan.intent == EditIntent.SAVE_THEME
    # 未确认 → 追问；影响摘要说清楚受影响草稿的**数量 + 名称**
    assert plan.needsClarification is True
    assert "机房能耗日报" in plan.clarificationQuestion
    assert "1 份" in plan.clarificationQuestion
    # themeOperations 留着，确认后照它落库
    operation = plan.operation.themeOperations[0]
    assert operation["name"] == "蓝色主题"
    assert operation["confirm"] is False

    result = template_edit_engine.apply(plan=plan, definition=definition)

    assert result.needsClarification is True
    assert "机房能耗日报" in result.clarificationQuestion
    # 主题**没有被创建**
    assert fake_theme_storage.list_theme_ids() == []


def test_save_theme_after_confirmation_commits_and_keeps_definition(fake_theme_storage):
    definition = _definition()
    plan = template_edit_engine.parse(
        message="把这个主题存成 蓝色主题",
        definition=definition,
        selected_region_id=None,
    )
    # 用户确认：confirm 翻成真
    plan.operation.themeOperations[0]["confirm"] = True
    plan.needsClarification = False

    result = template_edit_engine.apply(plan=plan, definition=definition)

    assert result.needsClarification is False
    assert "已保存共享主题" in result.reply
    # 落库了：版本 +1
    theme_id = plan.operation.themeOperations[0]["themeId"]
    saved = fake_theme_storage.load_theme(theme_id)
    assert saved is not None
    assert saved["name"] == "蓝色主题"
    assert saved["themeRevision"] == 1
    # 报表定义**一个字节都没变**，版本也**没有** +1
    assert result.draftAfterEdit.model_dump() == definition.model_dump()
    assert result.draftAfterEdit.metadata["revision"] == definition.metadata["revision"]
    assert result.changeSet is None


def test_save_theme_update_bumps_theme_revision(fake_theme_storage):
    fake_theme_storage.save_theme(
        "theme_x", name="旧主题", tokens={}, default_style={}, theme_revision=1
    )
    plan = EditPlan(isTemplateEdit=True, intent=EditIntent.SAVE_THEME, confidence=0.9)
    plan.operation = EditOperation(
        themeOperations=[
            {
                "themeId": "theme_x",
                "name": "新主题",
                "tokens": {},
                "defaultStyle": {},
                "confirm": True,
            }
        ]
    )

    result = template_edit_engine.apply(plan=plan, definition=_definition())

    assert result.needsClarification is False
    assert fake_theme_storage.load_theme("theme_x")["themeRevision"] == 2


def test_set_theme_operation_still_applies_theme_to_definition():
    """SET_THEME（应用已有主题到报表定义）保持可用。"""
    result = apply_presentation(
        _definition(), [{"op": "SET_THEME", "params": {"themeId": "theme_dark"}}]
    )

    assert result.ok is True
    assert result.definition.theme["themeRef"] == "theme_dark"


# --------------------------------------------------------------------------
# R06：应用主题只清掉**它管的那些**旧局部覆盖
# --------------------------------------------------------------------------


def _themed_definition() -> ReportDefinitionDraft:
    """一份带组 + 子卡、含旧外观覆盖的定义。"""
    return ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "主题回归", "revision": 1},
            "theme": {"themeRef": "theme_default", "version": "1.0"},
            "regions": [
                {
                    "id": "group_1",
                    "name": "对比组",
                    "role": "DETAIL",
                    "component": {
                        "type": "CONTAINER",
                        "arrangement": {"mode": "GRID", "columns": 2},
                    },
                    "style": {"textColor": "#111111", "keepMe": "group-only"},
                    "children": [
                        {
                            "id": "region_a",
                            "name": "总耗电",
                            "role": "SUMMARY",
                            "format": {"kind": "NUMBER", "precision": 2},
                            "layout": {
                                "placement": {"type": "GRID", "column": 1, "columnSpan": 6}
                            },
                            "data": {
                                "inputs": {"main": {"type": "DATASET", "datasetRef": "ds_1"}}
                            },
                            "style": {
                                "textColor": "#FF0000",
                                "borderRadius": 12,
                                "keepMe": "region-only",
                            },
                            "component": {"type": "METRIC_CARD"},
                        }
                    ],
                }
            ],
        }
    )


def test_set_theme_clears_only_theme_managed_style_overrides():
    """应用主题 → 主题管的属性上的旧覆盖清掉，其余一律不动。"""
    result = apply_presentation(
        _themed_definition(),
        [{"op": "SET_THEME", "params": {"themeId": "theme_dark"}}],
    )

    assert result.ok is True
    definition = result.definition
    assert definition.theme["themeRef"] == "theme_dark"
    # 组里的子卡也清了（递归）
    region = find_region(definition, "region_a")
    assert region["style"] == {"keepMe": "region-only"}
    # 组自身同样
    assert find_region(definition, "group_1")["style"] == {"keepMe": "group-only"}
    # **保留**：格式 / 布局 / 数据
    assert region["format"] == {"kind": "NUMBER", "precision": 2}
    assert region["layout"]["placement"]["column"] == 1
    assert region["data"]["inputs"]["main"]["datasetRef"] == "ds_1"


def test_set_theme_does_not_lock_manual_overrides():
    """清覆盖 ≠ 锁死：之后新的手动覆盖仍然允许。"""
    themed = apply_presentation(
        _themed_definition(),
        [{"op": "SET_THEME", "params": {"themeId": "theme_dark"}}],
    ).definition

    result = apply_presentation(
        themed,
        [
            {
                "op": "SET_STYLE",
                "params": {
                    "regionId": "region_a",
                    "style": {"backgroundColor": "#00FF00"},
                },
            }
        ],
    )

    assert result.ok is True
    assert find_region(result.definition, "region_a")["style"] == {
        "keepMe": "region-only",
        "backgroundColor": "#00FF00",
    }


def _four_layer_definition() -> ReportDefinitionDraft:
    """一份**四层样式**都带「主题管理的旧键」+「非管理扩展键」的定义。

    四层 = 前端渲染器真正会应用的那几层（``frontend/pages/workbench_presentation.js``
    的 ``nodeStyleSources()`` / ``nodeComponentStyleSources()``）：
    ``region.style`` / ``region.styleRefs[]`` / ``region.component.style`` /
    ``region.component.styleHints[].style``。
    """
    return ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "四层样式", "revision": 1},
            "theme": {"themeRef": "theme_default", "version": "1.0"},
            "regions": [
                {
                    "id": "region_a",
                    "name": "总耗电",
                    "role": "SUMMARY",
                    "format": {"kind": "NUMBER", "precision": 2},
                    "layout": {
                        "placement": {"type": "GRID", "column": 1, "columnSpan": 6}
                    },
                    "data": {
                        "inputs": {"main": {"type": "DATASET", "datasetRef": "ds_1"}}
                    },
                    # 1. 区域样式层
                    "style": {"textColor": "#FF0000", "keepStyle": "region-ext"},
                    # 2. 样式引用层（数组形态）
                    "styleRefs": [
                        {"backgroundColor": "#00FF00", "keepRef": "ref-ext"},
                        {"borderRadius": 8, "keepRef2": "ref-ext-2"},
                    ],
                    "component": {
                        "type": "METRIC_CARD",
                        # 3. 组件字段样式层
                        "style": {"fontSize": 20, "keepComponent": "component-ext"},
                        # 4. 组件条件样式层
                        "styleHints": [
                            {
                                "when": "RED",
                                "style": {"opacity": 0.5, "keepHint": "hint-ext"},
                            }
                        ],
                    },
                }
            ],
        }
    )


def test_set_theme_clears_the_theme_managed_keys_in_every_style_layer():
    """换主题 → 四层样式里的主题管理键**全部**清掉；扩展位全部保留。"""
    result = apply_presentation(
        _four_layer_definition(),
        [{"op": "SET_THEME", "params": {"themeId": "theme_dark"}}],
    )

    assert result.ok is True
    region = find_region(result.definition, "region_a")

    # 1. region.style（清空了就 pop 掉，只剩扩展位）
    assert region["style"] == {"keepStyle": "region-ext"}
    # 2. region.styleRefs[] —— 每一项都清
    assert region["styleRefs"] == [
        {"keepRef": "ref-ext"},
        {"keepRef2": "ref-ext-2"},
    ]
    # 3. region.component.style
    assert region["component"]["style"] == {"keepComponent": "component-ext"}
    # 4. region.component.styleHints[].style
    assert region["component"]["styleHints"][0]["style"] == {"keepHint": "hint-ext"}

    # **保留**：格式 / 布局 / 数据一个字节没动
    assert region["format"] == {"kind": "NUMBER", "precision": 2}
    assert region["layout"]["placement"] == {
        "type": "GRID",
        "column": 1,
        "columnSpan": 6,
    }
    assert region["data"]["inputs"]["main"]["datasetRef"] == "ds_1"


def test_set_theme_clears_nested_style_payloads_inside_style_refs():
    """``styleRefs`` 写成 ``{"style": {...}}`` 时，清覆盖要清到**内层**那一层。

    前端 ``applyStyleInto`` 取的是内层的 ``style``（同一口径）。服务端要是按
    「ref 本身」去清，内层的主题管理键就清不掉 —— 旧颜色仍然盖在新主题上。
    （浏览器侧实测过：换主题后卡片底色仍是旧的 ``rgb(255, 0, 0)``。）
    """
    definition = ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "嵌套样式", "revision": 1},
            "theme": {"themeRef": "theme_default", "version": "1.0"},
            "regions": [
                {
                    "id": "region_a",
                    "name": "总耗电",
                    "role": "SUMMARY",
                    "style": {"backgroundColor": "#FF0000", "keepStyle": "region-ext"},
                    # 嵌套形态：ref 本身不是样式字典，样式在 ref["style"] 里。
                    "styleRefs": [
                        {
                            "ref": "brand",
                            "style": {
                                "backgroundColor": "#00FF00",
                                "keepRef": "ref-ext",
                            },
                        },
                        {"backgroundColor": "#0000FF"},
                    ],
                    "component": {
                        "type": "METRIC_CARD",
                        "style": {"backgroundColor": "#FF00FF"},
                        "styleHints": [
                            {
                                "when": "RED",
                                "style": {"backgroundColor": "#00FFFF"},
                            }
                        ],
                    },
                }
            ],
        }
    )

    result = apply_presentation(
        definition, [{"op": "SET_THEME", "params": {"themeId": "theme_dark"}}]
    )

    assert result.ok is True
    region = find_region(result.definition, "region_a")

    # 1. 区域样式层：主题管理键清掉，扩展键留着。
    assert region["style"] == {"keepStyle": "region-ext"}
    # 2. 样式引用层：内层的 ``style`` 里的主题管理键**也要清**，
    #    ref 的其它字段（``ref`` / ``keepRef``）一个字节不动。
    assert region["styleRefs"][0] == {
        "ref": "brand",
        "style": {"keepRef": "ref-ext"},
    }
    # 直接就是样式字典的那种 ref，也照清。
    assert region["styleRefs"][1] == {}
    # 3. 组件字段样式层。
    assert region["component"]["style"] == {}
    # 4. 组件条件样式层。
    assert region["component"]["styleHints"][0]["style"] == {}


def test_set_theme_then_set_style_still_adds_the_new_override():
    """清覆盖 ≠ 锁死：清完主题之后再做一次 SET_STYLE，新覆盖仍然加上。"""
    themed = apply_presentation(
        _four_layer_definition(),
        [{"op": "SET_THEME", "params": {"themeId": "theme_dark"}}],
    ).definition

    result = apply_presentation(
        themed,
        [
            {
                "op": "SET_STYLE",
                "params": {
                    "regionId": "region_a",
                    "style": {"backgroundColor": "#0000FF"},
                },
            }
        ],
    )

    assert result.ok is True
    region = find_region(result.definition, "region_a")
    assert region["style"] == {
        "keepStyle": "region-ext",
        "backgroundColor": "#0000FF",
    }


def test_set_theme_does_not_modify_the_input_definition():
    """换主题只改副本：入参定义里的旧覆盖一个字节没动。"""
    definition = _four_layer_definition()
    snapshot = copy.deepcopy(definition.model_dump(mode="json"))

    result = apply_presentation(
        definition,
        [{"op": "SET_THEME", "params": {"themeId": "theme_dark"}}],
    )

    assert result.ok is True
    assert definition.model_dump(mode="json") == snapshot


# --------------------------------------------------------------------------
# R08：复制分组 —— 子节点 id 重建 + 源数据身份
# --------------------------------------------------------------------------


def _grouped_definition() -> ReportDefinitionDraft:
    """一张两卡分组：组内叶子互相引用（测引用映射）。"""
    return ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "复制回归", "revision": 1},
            "regions": [
                {
                    "id": "group_1",
                    "name": "对比组",
                    "role": "DETAIL",
                    "component": {
                        "type": "CONTAINER",
                        "arrangement": {"mode": "GRID", "columns": 2},
                    },
                    "children": [
                        {
                            "id": "leaf_a",
                            "name": "叶子A",
                            "role": "SUMMARY",
                            "component": {"type": "METRIC_CARD"},
                            "templateBinding": {
                                "previewSpec": {
                                    "metrics": [
                                        {
                                            "metricKey": "metric.energy.total",
                                            "label": "总耗电",
                                            "kind": "ENERGY",
                                        }
                                    ]
                                },
                                "dependencies": ["leaf_b"],
                            },
                        },
                        {
                            "id": "leaf_b",
                            "name": "叶子B",
                            "role": "SUMMARY",
                            "component": {"type": "METRIC_CARD"},
                            "templateBinding": {
                                "previewSpec": {
                                    "metrics": [
                                        {
                                            "metricKey": "metric.cost.total",
                                            "label": "总电费",
                                            "kind": "COST",
                                        }
                                    ]
                                }
                            },
                        },
                    ],
                }
            ],
        }
    )


def _copy_group(definition: ReportDefinitionDraft, new_id: str = "group_1_copy"):
    return apply_presentation(
        definition,
        [{"op": "COPY_REGION", "params": {"regionId": "group_1", "newRegionId": new_id}}],
    )


def test_copy_group_rebuilds_all_display_ids_and_keeps_source_untouched():
    """复制分组 → 全部展示 id 重建且唯一；源定义一个字节都没改。"""
    before = _grouped_definition()
    source_before = copy.deepcopy(find_region(before, "group_1"))

    result = _copy_group(before)

    assert result.ok is True
    definition = result.definition
    ids = [region["id"] for region, _ in iter_regions(definition)]
    assert len(ids) == len(set(ids)), ids
    # 规则是定死的：复制根用新 id，后代 = 「父的新 id_原 id」
    copied = find_region(definition, "group_1_copy")
    assert [child["id"] for child in copied["children"]] == [
        "group_1_copy_leaf_a",
        "group_1_copy_leaf_b",
    ]
    # 源件没被动
    assert find_region(definition, "group_1") == source_before


def test_copy_group_remaps_internal_references_to_new_ids():
    """组内引用映射到新 id，不指回源。"""
    result = _copy_group(_grouped_definition())

    assert result.ok is True
    definition = result.definition
    copied_child = find_region(definition, "group_1_copy_leaf_a")
    assert copied_child["templateBinding"]["dependencies"] == ["group_1_copy_leaf_b"]
    # 源件的引用没被改
    assert find_region(definition, "leaf_a")["templateBinding"]["dependencies"] == [
        "leaf_b"
    ]


def test_copy_group_keeps_leaf_data_identity_and_binding():
    """每个叶子的源数据身份与绑定都保留（复制件数值与源件一致）。"""
    result = _copy_group(_grouped_definition())

    assert result.ok is True
    definition = result.definition
    source_leaf = find_region(definition, "leaf_a")
    copied_leaf = find_region(definition, "group_1_copy_leaf_a")
    assert effective_simulation_identity(copied_leaf) == (
        effective_simulation_identity(source_leaf)
    )
    assert effective_simulation_identity(copied_leaf) == "leaf_a"
    # 绑定（指标语义）原样保留
    assert copied_leaf["templateBinding"]["previewSpec"]["metrics"] == (
        source_leaf["templateBinding"]["previewSpec"]["metrics"]
    )
    # 复制件标记：前端按它把复制件排除出总计（不翻倍）
    assert copied_leaf["templateBinding"]["previewSpec"]["duplicateOfRegionId"] == "leaf_a"
    assert copied_leaf["templateBinding"]["duplicateOfRegionId"] == "leaf_a"


def test_copy_legacy_region_without_preview_spec_adds_no_empty_shell():
    """老卡片（缺 previewSpec）复制：身份写在绑定层，不造空壳 previewSpec。"""
    legacy = ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "老卡片", "revision": 1},
            "regions": [
                {
                    "id": "legacy_a",
                    "name": "旧卡",
                    "role": "SUMMARY",
                    "component": {
                        "type": "METRIC_CARD",
                        "metric": {"label": "旧指标", "fieldRef": "energyKwh"},
                    },
                }
            ],
        }
    )

    result = apply_presentation(
        legacy,
        [
            {
                "op": "COPY_REGION",
                "params": {"regionId": "legacy_a", "newRegionId": "legacy_a_copy"},
            }
        ],
    )

    assert result.ok is True
    definition = result.definition
    copied = find_region(definition, "legacy_a_copy")
    source = find_region(definition, "legacy_a")
    # **没有**空壳 previewSpec（空壳会顶掉语义适配 → 整批丢数）
    assert "previewSpec" not in copied.get("templateBinding", {})
    # 身份写在**绑定层**，值 = 源的有效身份
    assert copied["templateBinding"]["simulationIdentity"] == "legacy_a"
    assert effective_simulation_identity(copied) == effective_simulation_identity(source)
    assert effective_simulation_identity(copied) == "legacy_a"
    # 复制件标记（前端按它排除总计）
    assert copied["templateBinding"]["duplicateOfRegionId"] == "legacy_a"
    assert copied["previewSpec"]["duplicateOfRegionId"] == "legacy_a"


# --------------------------------------------------------------------------
# R07：权威显示格式 —— 载荷带上格式，显示按格式，底层值不动
# --------------------------------------------------------------------------


def _payload_definition() -> dict:
    """一份能造出载荷的定义：一张指标卡 + 一张带时间列的趋势表。"""
    return {
        "metadata": {"name": "格式回归", "revision": 1},
        "defaults": {"scope": {"selectors": {"machineRoomId": ["room_a"]}}},
        "regions": [
            {
                "id": "region_a",
                "name": "总耗电",
                "role": "SUMMARY",
                "component": {
                    "type": "METRIC_CARD",
                    "metric": {
                        "label": "总耗电",
                        "fieldRef": "energyKwh",
                        "format": {"kind": "NUMBER", "precision": 4, "unitLabel": "kWh"},
                    },
                },
                "templateBinding": {
                    "previewSpec": {
                        "metrics": [
                            {
                                "metricKey": "metric.energy.total",
                                "label": "总耗电",
                                "kind": "ENERGY",
                            }
                        ]
                    }
                },
            },
            {
                "id": "region_b",
                "name": "趋势明细",
                "role": "TREND",
                "component": {
                    "type": "TABLE",
                    "columns": [
                        {
                            "id": "col_time",
                            "label": "时间",
                            "fieldRef": "section_1_col_time",
                            "valueType": "time",
                        },
                        {
                            "id": "col_value",
                            "label": "耗电",
                            "fieldRef": "energyKwh",
                            "valueType": "energy",
                        },
                    ],
                },
                "templateBinding": {
                    "previewSpec": {
                        "columns": [
                            {
                                "id": "col_time",
                                "label": "时间",
                                "fieldRef": "section_1_col_time",
                                "valueType": "time",
                            },
                            {
                                "id": "col_value",
                                "label": "耗电",
                                "fieldRef": "energyKwh",
                                "valueType": "energy",
                            },
                        ]
                    }
                },
            },
        ],
    }


def _payloads(definition: dict) -> dict:
    """按定义造载荷（纯内存，不落库）。"""
    from app.services.preview_service import (
        build_preview_context,
        build_report_preview_payload,
    )

    preview = build_report_preview_payload(definition, build_preview_context(definition))
    return {item["regionId"]: item["payload"] for item in preview["regions"]}


def test_authoritative_format_only_keeps_whitelisted_keys():
    """载荷里的 format 结构是封闭的：只用格式白名单里的键。"""
    region = {"format": {"kind": "NUMBER", "precision": 2, "madeUpKey": "x"}}
    assert region_authoritative_format(region) == {"kind": "NUMBER", "precision": 2}
    assert region_authoritative_format({"format": "不是对象"}) == {}
    assert region_authoritative_format({}) == {}


def test_format_display_value_applies_precision_separator_and_unit():
    """两位小数 + 千分位 + 合法单位标签。"""
    fmt = {
        "kind": "NUMBER",
        "precision": 2,
        "thousandSeparator": True,
        "unitLabel": "kWh",
    }
    assert format_display_value(12345.6789, fmt) == "12,345.68 kWh"
    # 单位标签可以清空（= 不带单位）
    assert format_display_value(12345.6789, {**fmt, "unitLabel": ""}) == "12,345.68"
    # 舍入方式按格式来
    assert format_display_value(
        2.5, {"kind": "NUMBER", "precision": 0, "roundingMode": "HALF_UP"}
    ) == "3"
    assert format_display_value(
        2.5, {"kind": "NUMBER", "precision": 0, "roundingMode": "FLOOR"}
    ) == "2"


def test_format_display_value_renders_a_date_by_the_set_pattern():
    """日期值按所设日期格式显示。"""
    assert (
        format_display_value(
            "2026-11-03T00:00:00+08:00",
            {"kind": "DATE", "dateTimePattern": "%Y/%m/%d"},
        )
        == "2026/11/03"
    )
    # 认不出来就原样返回，不猜
    assert (
        format_display_value(
            "不是日期", {"kind": "DATE", "dateTimePattern": "%Y/%m/%d"}
        )
        == "不是日期"
    )
    # 没给 pattern 时用默认日期样式
    assert (
        format_display_value("2026-11-03T00:00:00+08:00", {"kind": "DATE"})
        == "2026-11-03"
    )


def test_payload_carries_the_authoritative_format_without_changing_the_value():
    """载荷带上权威格式；显示按格式；**底层数值不变**。"""
    definition = ReportDefinitionDraft.model_validate(_payload_definition())
    before = _payloads(definition.model_dump(mode="json"))

    result = apply_presentation(
        definition,
        [
            {
                "op": "SET_FORMAT",
                "params": {
                    "regionId": "region_a",
                    "format": {
                        "kind": "NUMBER",
                        "precision": 2,
                        "thousandSeparator": True,
                        "unitLabel": "kWh",
                    },
                },
            }
        ],
    )
    assert result.ok is True
    after = _payloads(result.definition.model_dump(mode="json"))

    # 载荷里带上该区域的权威格式
    assert after["region_a"]["format"] == {
        "kind": "NUMBER",
        "precision": 2,
        "unitLabel": "kWh",
        "thousandSeparator": True,
    }
    # 没有格式的区域也带 format（空对象）—— 载荷结构封闭，前端按同一个字段读
    assert after["region_b"]["format"] == {}
    # **底层统计值不变**：显示格式不改数据精度、不改统计结果
    assert after["region_a"]["value"] == before["region_a"]["value"]
    # 显示按格式
    value = after["region_a"]["value"]
    assert after["region_a"]["displayValue"] == f"{value:,.2f} kWh"


def test_payload_renders_a_date_value_by_the_set_date_format():
    """至少一个日期值按所设日期格式显示；底层行不动。"""
    definition = ReportDefinitionDraft.model_validate(_payload_definition())
    before = _payloads(definition.model_dump(mode="json"))

    result = apply_presentation(
        definition,
        [
            {
                "op": "SET_FORMAT",
                "params": {
                    "regionId": "region_b",
                    "format": {"kind": "DATE", "dateTimePattern": "%Y/%m/%d"},
                },
            }
        ],
    )
    assert result.ok is True
    after = _payloads(result.definition.model_dump(mode="json"))

    raw = after["region_b"]["rows"][0]["section_1_col_time"]
    shown = after["region_b"]["displayRows"][0]["section_1_col_time"]
    # 底层行没动
    assert after["region_b"]["rows"] == before["region_b"]["rows"]
    # 显示按所设日期格式
    assert shown == datetime.fromisoformat(raw).strftime("%Y/%m/%d")
    assert shown != raw


# --------------------------------------------------------------------------
# R07 第二轮：日期格式族 + **按列自己的格式优先**
# --------------------------------------------------------------------------


def test_the_date_family_renders_an_iso_date_by_strftime():
    """日期族：ISO 日期值按 strftime 渲染；默认 ``%Y-%m-%d``。"""
    assert format_display_value("2026-11-02", {"kind": "DATE"}) == "2026-11-02"
    assert (
        format_display_value("2026-11-02", {"kind": "DATE", "dateTimePattern": "%Y/%m/%d"})
        == "2026/11/02"
    )
    # 底层值是**字符串**：显示串另开，值本身不动
    value = "2026-11-02"
    format_display_value(value, {"kind": "DATE"})
    assert value == "2026-11-02"


def test_a_table_cell_renders_by_its_own_column_format_and_the_raw_row_is_untouched():
    """表格逐格：**列自己的格式优先**，列没声明才回落到区域格式。

    区域级设了 ``DATE`` 之后，同区域的数值列**不能**被按日期渲染。
    """
    from app.services.simulated_data_service import _attach_authoritative_format

    payload = {
        "columns": [
            {"fieldRef": "day", "format": {"kind": "DATE", "dateTimePattern": "%Y/%m/%d"}},
            {"fieldRef": "energy", "format": {"kind": "NUMBER", "precision": 2}},
        ],
        "rows": [{"day": "2026-11-02", "energy": 123.456}],
    }
    region = {"format": {"kind": "DATE", "dateTimePattern": "%Y.%m.%d"}}

    attached = _attach_authoritative_format(payload, region)

    # 日期格：按**列自己的** strftime 样式渲染（不是区域级的）
    assert attached["displayRows"][0]["day"] == "2026/11/02"
    # **原始值没被就地改写**
    assert attached["rows"][0]["day"] == "2026-11-02"
    # 同一区域的数值格：按列自己的数值格式显示，**没有被按日期渲染**
    assert attached["displayRows"][0]["energy"] == "123.46"
    assert attached["rows"][0]["energy"] == 123.456


def _date_and_number_table_definition() -> dict:
    """一张表 + 一张趋势图：日期列、数值列、图表横轴都带得上。"""
    return {
        "metadata": {"name": "日期格式", "revision": 1},
        "defaults": {"scope": {"selectors": {"machineRoomId": ["room_a"]}}},
        "regions": [
            {
                "id": "region_table",
                "name": "明细",
                "role": "DETAIL",
                "component": {
                    "type": "TABLE",
                    "columns": [
                        {
                            "id": "col_time",
                            "label": "时间",
                            "fieldRef": "section_1_col_time",
                            "valueType": "time",
                            "format": {"kind": "DATE", "dateTimePattern": "%Y/%m/%d"},
                        },
                        {
                            "id": "col_value",
                            "label": "耗电",
                            "fieldRef": "energyKwh",
                            "valueType": "energy",
                            "format": {"kind": "NUMBER", "precision": 2},
                        },
                    ],
                },
                "templateBinding": {
                    "previewSpec": {
                        "columns": [
                            {
                                "id": "col_time",
                                "label": "时间",
                                "fieldRef": "section_1_col_time",
                                "valueType": "time",
                            },
                            {
                                "id": "col_value",
                                "label": "耗电",
                                "fieldRef": "energyKwh",
                                "valueType": "energy",
                            },
                        ]
                    }
                },
            },
            {
                "id": "region_chart",
                "name": "趋势",
                "role": "TREND",
                "component": {"type": "CHART", "chartType": "line"},
                "templateBinding": {
                    "previewSpec": {
                        "metrics": [
                            {
                                "metricKey": "metric.energy.total",
                                "label": "总耗电",
                                "kind": "ENERGY",
                            }
                        ]
                    }
                },
            },
        ],
    }


def test_a_numeric_column_in_a_date_region_is_not_rendered_as_a_date():
    """真实载荷：区域级 DATE 之后，数值列按**列自己的**格式显示；底层行不动。"""
    definition = ReportDefinitionDraft.model_validate(_date_and_number_table_definition())
    before = _payloads(definition.model_dump(mode="json"))

    result = apply_presentation(
        definition,
        [
            {
                "op": "SET_FORMAT",
                "params": {
                    "regionId": "region_table",
                    "format": {"kind": "DATE", "dateTimePattern": "%Y.%m.%d"},
                },
            }
        ],
    )
    assert result.ok is True
    after = _payloads(result.definition.model_dump(mode="json"))

    raw_row = after["region_table"]["rows"][0]
    shown_row = after["region_table"]["displayRows"][0]
    # 底层行一个字节没动
    assert after["region_table"]["rows"] == before["region_table"]["rows"]
    # 日期格：按列自己的 strftime 样式渲染（区域级的 ``%Y.%m.%d`` 不算数）
    assert shown_row["section_1_col_time"] == datetime.fromisoformat(
        raw_row["section_1_col_time"]
    ).strftime("%Y/%m/%d")
    # 数值格：按列自己的 NUMBER 格式显示，**没有被按日期渲染**
    assert shown_row["energyKwh"] == f"{raw_row['energyKwh']:.2f}"
    assert shown_row["energyKwh"] != str(raw_row["energyKwh"])


def test_the_chart_axis_labels_render_dates_by_the_set_pattern():
    """图表横轴：日期显示串按区域格式渲染；底层点没动。"""
    definition = ReportDefinitionDraft.model_validate(_date_and_number_table_definition())
    before = _payloads(definition.model_dump(mode="json"))

    result = apply_presentation(
        definition,
        [
            {
                "op": "SET_FORMAT",
                "params": {
                    "regionId": "region_chart",
                    "format": {"kind": "DATE", "dateTimePattern": "%Y/%m/%d"},
                },
            }
        ],
    )
    assert result.ok is True
    after = _payloads(result.definition.model_dump(mode="json"))

    chart = after["region_chart"]
    raw_points = chart["points"]
    labels = chart["displayXLabels"]
    assert labels, "趋势图应当有横轴标签"
    assert len(labels) == len(raw_points)
    # 底层点没动
    assert chart["points"] == before["region_chart"]["points"]
    # 横轴日期串按 strftime 渲染
    assert labels[0] == datetime.fromisoformat(raw_points[0][chart["xField"]]).strftime(
        "%Y/%m/%d"
    )


def test_the_metric_card_display_value_follows_the_region_format():
    """指标卡：显示串按区域格式；数值不会被日期格式改写。"""
    definition = ReportDefinitionDraft.model_validate(_payload_definition())
    before = _payloads(definition.model_dump(mode="json"))

    result = apply_presentation(
        definition,
        [
            {
                "op": "SET_FORMAT",
                "params": {
                    "regionId": "region_a",
                    "format": {"kind": "NUMBER", "precision": 2, "unitLabel": "kWh"},
                },
            }
        ],
    )
    assert result.ok is True
    after = _payloads(result.definition.model_dump(mode="json"))

    card = after["region_a"]
    assert card["value"] == before["region_a"]["value"]
    assert card["displayValue"] == f"{card['value']:.2f} kWh"


def test_a_metric_card_with_a_date_value_renders_it_by_strftime():
    """指标卡的值是日期时：显示串按 strftime 渲染，底层值不动。"""
    from app.services.simulated_data_service import _attach_authoritative_format

    payload = {"value": "2026-11-02", "unit": ""}
    region = {"format": {"kind": "DATE", "dateTimePattern": "%Y/%m/%d"}}

    attached = _attach_authoritative_format(payload, region)

    assert attached["displayValue"] == "2026/11/02"
    assert attached["value"] == "2026-11-02"


def test_copied_leaf_and_source_leaf_show_the_same_numbers():
    """复制件的叶子与源叶子数值一致（数据身份相同 ⇒ 种子相同）。"""
    definition = _grouped_definition()
    copied = _copy_group(definition).definition

    source_payloads = _payloads(definition.model_dump(mode="json"))
    copied_payloads = _payloads(copied.model_dump(mode="json"))

    assert copied_payloads["group_1_copy_leaf_a"]["value"] == (
        source_payloads["leaf_a"]["value"]
    )
    assert copied_payloads["group_1_copy_leaf_b"]["value"] == (
        source_payloads["leaf_b"]["value"]
    )
    # 复制件带复制标记：前端据此把它排除出总计（总计不翻倍）
    assert copied_payloads["group_1_copy_leaf_a"]["format"] == {}


def test_copied_leaf_can_be_edited_without_touching_the_source():
    """复制件与源件按新 id 独立编辑：改复制件不动源件。"""
    copied = _copy_group(_grouped_definition()).definition

    result = apply_presentation(
        copied,
        [
            {
                "op": "SET_STYLE",
                "params": {
                    "regionId": "group_1_copy_leaf_a",
                    "style": {"backgroundColor": "#00FF00"},
                },
            }
        ],
    )

    assert result.ok is True
    assert find_region(result.definition, "group_1_copy_leaf_a")["style"] == {
        "backgroundColor": "#00FF00"
    }
    assert find_region(result.definition, "leaf_a").get("style") is None


def test_engine_apply_theme_clears_the_theme_managed_overrides():
    """走真实引擎链路（template_edit_engine.apply）：主题的覆盖真的清掉了。"""
    plan = EditPlan(isTemplateEdit=True, intent=EditIntent.APPLY_PRESENTATION, confidence=0.9)
    plan.operation = EditOperation(
        presentationOperations=[
            {"op": "SET_THEME", "params": {"themeId": "theme_dark"}},
        ]
    )

    result = template_edit_engine.apply(plan=plan, definition=_themed_definition())

    assert result.needsClarification is False
    definition = result.draftAfterEdit
    assert definition.theme["themeRef"] == "theme_dark"
    region = find_region(definition, "region_a")
    assert region["style"] == {"keepMe": "region-only"}
    # 格式 / 布局 / 数据没被动
    assert region["format"] == {"kind": "NUMBER", "precision": 2}
    assert region["data"]["inputs"]["main"]["datasetRef"] == "ds_1"


def test_engine_copy_group_rebuilds_ids_through_the_real_engine():
    """走真实引擎链路复制分组：id 唯一、源件不动。"""
    plan = EditPlan(isTemplateEdit=True, intent=EditIntent.APPLY_PRESENTATION, confidence=0.9)
    plan.operation = EditOperation(
        presentationOperations=[
            {
                "op": "COPY_REGION",
                "params": {"regionId": "group_1", "newRegionId": "group_1_copy"},
            }
        ]
    )

    result = template_edit_engine.apply(plan=plan, definition=_grouped_definition())

    assert result.needsClarification is False
    definition = result.draftAfterEdit
    ids = [region["id"] for region, _ in iter_regions(definition)]
    assert len(ids) == len(set(ids)), ids
    assert find_region(definition, "group_1_copy_leaf_a") is not None
    assert find_region(definition, "leaf_a") is not None


# --------------------------------------------------------------------------
# 补齐验收口径：主题真的生效 / 换主题不重置格式 / 总计不翻倍
# --------------------------------------------------------------------------


def _preview_regions(definition: dict) -> list[dict]:
    """载荷里的区域项（含 ``componentType``）。"""
    from app.services.preview_service import (
        build_preview_context,
        build_report_preview_payload,
    )

    preview = build_report_preview_payload(definition, build_preview_context(definition))
    return preview["regions"]


def _duplicate_region_ids(definition: dict) -> set[str]:
    """前端 ``duplicateRegionIds`` 的同一口径：复制件不参与总计。"""
    out: set[str] = set()
    for region, _ in iter_regions(definition):
        binding = region.get("templateBinding") or {}
        spec = binding.get("previewSpec") or {}
        own = region.get("previewSpec") or {}
        if spec.get("duplicateOfRegionId") or own.get("duplicateOfRegionId"):
            out.add(str(region.get("id") or ""))
    return out


def test_set_theme_makes_the_theme_style_effective():
    """R06：清掉旧覆盖后，区域的最终外观按**主题**走（不再被旧局部颜色压住）。"""
    from app.services.report_theme_service import theme_service

    # 接受前：旧局部颜色压着主题
    before = theme_service.resolve_region_style(
        _themed_definition().model_dump(), "region_a"
    )
    assert before["textColor"] == "#FF0000"

    themed = apply_presentation(
        _themed_definition(),
        [{"op": "SET_THEME", "params": {"themeId": "theme_dark"}}],
    )
    assert themed.ok is True

    definition = themed.definition.model_dump()
    after = theme_service.resolve_region_style(definition, "region_a")
    dark = theme_service.resolve_theme(definition, theme_id="theme_dark")
    # 接受后：按主题生效
    assert after["textColor"] == dark["defaultStyle"]["textColor"]
    assert after["textColor"] != "#FF0000"


def test_set_theme_does_not_mutate_the_input_definition():
    """R06：纯计算 —— 应用主题时入参一个字节都不动。"""
    definition = _themed_definition()
    snapshot = copy.deepcopy(definition.model_dump())

    result = apply_presentation(
        definition, [{"op": "SET_THEME", "params": {"themeId": "theme_dark"}}]
    )

    assert result.ok is True
    assert definition.model_dump() == snapshot


def test_theme_change_does_not_reset_the_region_format():
    """R07：换外观主题**不重置**显示格式。"""
    formatted = apply_presentation(
        ReportDefinitionDraft.model_validate(_payload_definition()),
        [
            {
                "op": "SET_FORMAT",
                "params": {
                    "regionId": "region_a",
                    "format": {
                        "kind": "NUMBER",
                        "precision": 2,
                        "thousandSeparator": True,
                        "unitLabel": "kWh",
                    },
                },
            }
        ],
    )
    assert formatted.ok is True

    themed = apply_presentation(
        formatted.definition,
        [{"op": "SET_THEME", "params": {"themeId": "theme_dark"}}],
    )

    assert themed.ok is True
    assert find_region(themed.definition, "region_a")["format"] == {
        "kind": "NUMBER",
        "precision": 2,
        "unitLabel": "kWh",
        "thousandSeparator": True,
    }
    # 载荷里的权威格式也跟着还在
    payloads = _payloads(themed.definition.model_dump(mode="json"))
    assert payloads["region_a"]["format"]["precision"] == 2
    assert payloads["region_a"]["format"]["thousandSeparator"] is True


def test_copied_region_shows_the_same_numbers_as_the_legacy_source():
    """R08：老卡片（缺 previewSpec）复制件与源件数值一致（身份沿用 → 种子相同）。"""
    legacy = ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "老卡片", "revision": 1},
            "regions": [
                {
                    "id": "legacy_a",
                    "name": "旧卡",
                    "role": "SUMMARY",
                    "component": {
                        "type": "METRIC_CARD",
                        "metric": {"label": "旧指标", "fieldRef": "energyKwh"},
                    },
                }
            ],
        }
    )
    copied = apply_presentation(
        legacy,
        [
            {
                "op": "COPY_REGION",
                "params": {"regionId": "legacy_a", "newRegionId": "legacy_a_copy"},
            }
        ],
    )
    assert copied.ok is True

    before = _payloads(legacy.model_dump(mode="json"))
    after = _payloads(copied.definition.model_dump(mode="json"))

    # 数值一致（不漂）
    assert after["legacy_a_copy"]["value"] == before["legacy_a"]["value"]
    # 语义没丢：空壳 previewSpec 会让这里整批丢数
    assert after["legacy_a_copy"]["metrics"]
    assert after["legacy_a_copy"]["metrics"][0]["metricKey"] == (
        before["legacy_a"]["metrics"][0]["metricKey"]
    )


def test_copied_regions_do_not_double_the_report_total():
    """R08：复制件与源件数值一致，但**不参与总计**（总计不翻倍）。"""
    definition = ReportDefinitionDraft.model_validate(_payload_definition())
    copied = apply_presentation(
        definition,
        [
            {
                "op": "COPY_REGION",
                "params": {"regionId": "region_a", "newRegionId": "region_a_copy"},
            }
        ],
    )
    assert copied.ok is True

    copied_definition = copied.definition.model_dump(mode="json")
    before_regions = _preview_regions(definition.model_dump(mode="json"))
    after_regions = _preview_regions(copied_definition)

    # 复制件数值与源件一致
    after_card = next(item for item in after_regions if item["regionId"] == "region_a_copy")
    before_card = next(item for item in before_regions if item["regionId"] == "region_a")
    assert after_card["payload"]["value"] == before_card["payload"]["value"]

    # 前端按复制件标记把它排除出总计 → 总计不翻倍
    excluded = _duplicate_region_ids(copied_definition)
    assert excluded == {"region_a_copy"}

    def _total(regions: list[dict], skip: set[str]) -> float:
        return sum(
            item["payload"]["value"]
            for item in regions
            if item["componentType"] == "METRIC_CARD"
            and item["regionId"] not in skip
        )

    assert _total(after_regions, excluded) == _total(before_regions, set())


# --------------------------------------------------------------------------
# R07 第三轮：区域**没有**格式时，列自己的格式照样生效
#               + 中文日期指令走**真实规则**形成 SET_FORMAT 计划
# --------------------------------------------------------------------------


def test_a_column_format_applies_even_when_the_region_has_no_format():
    """区域级**没有**格式时，**列自己的**格式照样算出显示串；原始值不动。

    改造前：区域格式为空就整批返回 → 列级格式形同虚设，
    页面显示裸 ISO 串 / 裸数值。
    """
    definition = ReportDefinitionDraft.model_validate(_date_and_number_table_definition())
    before = _payloads(definition.model_dump(mode="json"))

    table_before = before["region_table"]
    # 区域级**没有**格式
    assert table_before["format"] == {}
    # 但**列自己的**格式在
    assert table_before["columns"][0]["format"] == {
        "kind": "DATE",
        "dateTimePattern": "%Y/%m/%d",
    }
    assert table_before["columns"][1]["format"] == {"kind": "NUMBER", "precision": 2}

    after = _payloads(definition.model_dump(mode="json"))
    table = after["region_table"]
    # 区域没有格式也**算出了显示串**
    assert "displayRows" in table
    raw_row = table["rows"][0]
    shown_row = table["displayRows"][0]
    # 日期列：按**列自己的** strftime 样式渲染
    assert shown_row["section_1_col_time"] == datetime.fromisoformat(
        raw_row["section_1_col_time"]
    ).strftime("%Y/%m/%d")
    # 数值列：按**列自己的** NUMBER 格式渲染
    assert shown_row["energyKwh"] == f"{raw_row['energyKwh']:.2f}"
    # **底层原始值一个字节都没动**
    assert table["rows"] == table_before["rows"]


def _chinese_date_instruction_definition() -> dict:
    """**两列**的日期明细表（时间列 + 耗电列）：中文日期指令的入口现场。"""
    return {
        "metadata": {"name": "日期格式", "revision": 1},
        "page": {"arrangement": {"mode": "GRID", "columns": 12}},
        "theme": {"themeRef": "theme_default", "version": "1.0"},
        "regions": [
            {
                "id": "region_table_r07",
                "name": "日期明细表",
                "role": "DETAIL",
                "component": {
                    "type": "TABLE",
                    "columns": [
                        {
                            "id": "col_time",
                            "label": "时间",
                            "fieldRef": "section_r07.col_time",
                            "valueType": "time",
                        },
                        {
                            "id": "col_kwh",
                            "label": "耗电",
                            "fieldRef": "section_r07.col_kwh",
                            "valueType": "energy",
                            "semanticKind": "energy",
                            "unit": "kWh",
                            "format": {
                                "kind": "NUMBER",
                                "precision": 1,
                                "unitLabel": "kWh",
                            },
                        },
                    ],
                },
                "templateBinding": {
                    "previewSpec": {
                        "columns": [
                            {
                                "id": "col_time",
                                "label": "时间",
                                "fieldRef": "section_r07.col_time",
                                "valueType": "time",
                            },
                            {
                                "id": "col_kwh",
                                "label": "耗电",
                                "fieldRef": "section_r07.col_kwh",
                                "valueType": "energy",
                                "semanticKind": "energy",
                                "unit": "kWh",
                            },
                        ]
                    }
                },
            }
        ],
    }


def test_the_chinese_date_instruction_lands_a_set_format_on_the_draft():
    """中文日期指令经**真实规则**形成 SET_FORMAT 计划，并**真的落到定义**。

    改造前：这句话被 CHANGE_VISUALIZATION 抢走（置信度 0.9、**空操作**），
    真实入口根本走不到「设置显示格式」。
    """
    definition = ReportDefinitionDraft.model_validate(
        _chinese_date_instruction_definition()
    )

    plan = template_edit_engine.parse(
        message="把日期明细表的时间列显示格式设为日期",
        definition=definition,
        selected_region_id=None,
    )

    assert plan.intent == EditIntent.APPLY_PRESENTATION
    assert plan.needsClarification is False
    operations = list(plan.operation.presentationOperations or [])
    assert len(operations) == 1, operations
    assert operations[0]["op"] == "SET_FORMAT"
    # 目标卡片 + 只写 kind（**不编造** dateTimePattern 之类的键）
    assert operations[0]["params"] == {
        "regionId": "region_table_r07",
        "format": {"kind": "DATE"},
    }

    result = template_edit_engine.apply(plan=plan, definition=definition)
    assert result.needsClarification is False
    region = find_region(result.draftAfterEdit, "region_table_r07")
    assert region["format"] == {"kind": "DATE"}

    # 显示串按 strftime 渲染；**底层原始值不动**
    after = _payloads(result.draftAfterEdit.model_dump(mode="json"))
    table = after["region_table_r07"]
    raw_row = table["rows"][0]
    shown_row = table["displayRows"][0]
    assert shown_row["section_r07.col_time"] == datetime.fromisoformat(
        raw_row["section_r07.col_time"]
    ).strftime("%Y-%m-%d")
    assert raw_row["section_r07.col_time"] != shown_row["section_r07.col_time"]
    # 区域级 DATE **没有污染**数值列（列自带 NUMBER）
    assert shown_row["section_r07.col_kwh"] == f"{raw_row['section_r07.col_kwh']:.1f} kWh"
