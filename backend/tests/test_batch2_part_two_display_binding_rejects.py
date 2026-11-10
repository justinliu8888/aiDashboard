"""P14：非预设与非法输入 —— 合法组合没被写死，非法输入按契约拒绝且**无部分生效**。

这一层走的是能力层的真实执行（``edit_trial_service.run_trial`` 在草稿副本上跑），
不碰正式草稿。

合法侧（P14 第一半）由 ``test_batch2_part_two_display_pipeline.py`` 的确定性
fixture 覆盖（不同阈值 / 方向 / topN / 别名）；这里覆盖**非法侧**与
「拒绝后原定义一个字节都不改」。
"""

from __future__ import annotations

import copy

from app.schemas.report_definition import ReportDefinitionDraft
from app.services.edit_trial_service import run_trial


def _draft() -> ReportDefinitionDraft:
    return ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "A 机房昨日能耗日报", "revision": 1},
            "datasets": {
                "ds_room_energy": {
                    "sourceRef": "catalog.machine_room.energy",
                    "sourceVersion": "1.0",
                    "fields": {
                        "deviceId": {"sourceFieldRef": "deviceId", "dataType": "STRING"},
                        "deviceName": {"sourceFieldRef": "deviceName", "dataType": "STRING"},
                        "timestamp": {"sourceFieldRef": "timestamp", "dataType": "DATETIME"},
                        "energyKwh": {
                            "sourceFieldRef": "energyKwh",
                            "dataType": "DECIMAL",
                            "unit": "kWh",
                        },
                    },
                }
            },
            "regions": [
                {
                    "id": "region_3",
                    "name": "设备耗电明细",
                    "role": "TREND",
                    "component": {
                        "type": "TABLE",
                        "columns": [
                            {
                                "id": "col_1",
                                "label": "时间",
                                "fieldRef": "timestamp",
                                "format": {"kind": "DATETIME"},
                            },
                            {
                                "id": "col_2",
                                "label": "设备名称",
                                "fieldRef": "deviceName",
                                "format": {"kind": "TEXT"},
                            },
                            {
                                "id": "col_3",
                                "label": "昨日耗电量",
                                "fieldRef": "energyKwh",
                                "format": {"kind": "NUMBER", "precision": 2},
                            },
                        ],
                    },
                    "data": {
                        "inputs": {
                            "main": {
                                "type": "DATASET",
                                "datasetRef": "ds_room_energy",
                                "periodRef": "CURRENT",
                            }
                        }
                    },
                    "binding": {"outputRef": "output:main"},
                    "templateBinding": {
                        "sectionId": "section_3",
                        "previewSpec": {
                            "kind": "energy",
                            "unit": "kWh",
                            "columns": [
                                {
                                    "id": "col_1",
                                    "label": "时间",
                                    "fieldRef": "timestamp",
                                    "valueType": "time",
                                    "semanticKind": "generic",
                                },
                                {
                                    "id": "col_2",
                                    "label": "设备名称",
                                    "fieldRef": "deviceName",
                                    "valueType": "device_name",
                                    "semanticKind": "generic",
                                },
                                {
                                    "id": "col_3",
                                    "label": "昨日耗电量",
                                    "fieldRef": "energyKwh",
                                    "valueType": "energy",
                                    "semanticKind": "energy",
                                    "unit": "kWh",
                                },
                            ],
                            "metrics": [],
                        },
                    },
                }
            ],
        }
    )


def _set_binding(definition: ReportDefinitionDraft, params: dict) -> object:
    return run_trial(
        definition=definition,
        operations=[
            {
                "op": "SET_DATA_BINDING",
                "target": {"regionId": "region_3"},
                "params": params,
            }
        ],
    )


def _binding(definition: ReportDefinitionDraft) -> dict:
    for region in definition.regions:
        if isinstance(region, dict) and region.get("id") == "region_3":
            return region.get("binding") or {}
    raise AssertionError("草稿里没有 region_3")


# --------------------------------------------------------------------------
# 合法侧：不是写死的
# --------------------------------------------------------------------------

def test_legal_variants_are_accepted():
    """不同别名 / 阈值 / 方向 / topN 都真的落进定义。"""
    result = _set_binding(
        _draft(),
        {
            "fieldAliases": {"时间点": "timestamp", "电量": "energyKwh"},
            "presentationFilters": [
                {
                    "type": "CONDITION",
                    "left": {"type": "FIELD", "fieldRef": "energyKwh"},
                    "operator": "GT",
                    "operands": [{"type": "LITERAL", "value": 600}],
                }
            ],
            "sort": [{"fieldRef": "energyKwh", "direction": "ASC"}],
            "topN": 3,
        },
    )
    assert result.ok, result.errors
    binding = _binding(result.definition)
    assert binding["fieldAliases"] == {"时间点": "timestamp", "电量": "energyKwh"}
    assert binding["topN"] == 3
    assert binding["sort"][0]["direction"] == "ASC"
    assert binding["presentationFilters"][0]["operands"][0]["value"] == 600


# --------------------------------------------------------------------------
# 非法侧：明确拒绝，且**无部分生效**
# --------------------------------------------------------------------------

def test_unknown_field_is_rejected_without_partial_effect():
    """未登记字段：整批拒绝，原定义不变。"""
    before = _draft()
    snapshot = copy.deepcopy(before.model_dump())
    result = _set_binding(before, {"fieldAliases": {"神秘列": "notRegisteredField"}})
    assert not result.ok
    reasons = [str(item.get("reason")) for item in result.errors]
    assert any("unknown" in reason or "field" in reason for reason in reasons)
    # 正式定义没有被改动
    assert before.model_dump() == snapshot


def test_unknown_filter_field_is_rejected():
    """过滤引用未登记字段 → 拒绝。"""
    result = _set_binding(
        _draft(),
        {
            "presentationFilters": [
                {
                    "type": "CONDITION",
                    "left": {"type": "FIELD", "fieldRef": "notRegisteredField"},
                    "operator": "GT",
                    "operands": [{"type": "LITERAL", "value": 400}],
                }
            ]
        },
    )
    assert not result.ok


def test_reversed_alias_is_normalized_to_display_to_actual():
    """模型常把 source→display 写反，服务端要归一化**而不是**记反。

    协议里 fieldAliases 是「展示字段名 → 实际结果字段名」；
    写反时若有一端是已声明字段，就自动纠正。
    """
    result = _set_binding(_draft(), {"fieldAliases": {"timestamp": "时间点"}})
    assert result.ok, result.errors
    binding = _binding(result.definition)
    assert binding["fieldAliases"] == {"时间点": "timestamp"}


def test_invalid_sort_direction_is_rejected():
    """排序方向不在协议取值域内 → 拒绝。"""
    result = _set_binding(
        _draft(),
        {"sort": [{"fieldRef": "energyKwh", "direction": "SIDEWAYS"}]},
    )
    assert not result.ok
    reasons = [str(item.get("reason")) for item in result.errors]
    assert any("invalid_sort" in reason for reason in reasons)


def test_negative_top_n_is_rejected():
    """前 N 项的 N 必须是**非负整数** → 负数拒绝。"""
    result = _set_binding(_draft(), {"topN": -1})
    assert not result.ok
    reasons = [str(item.get("reason")) for item in result.errors]
    assert any("invalid_topn" in reason for reason in reasons)


def test_zero_top_n_is_accepted_as_non_negative_integer():
    """topN=0 是**非负整数**，按既有契约接受（协议只要求 integer）。

    这里**记录既有行为**（展示 0 行），不新定义语义。
    """
    result = _set_binding(_draft(), {"topN": 0})
    assert result.ok, result.errors
    assert _binding(result.definition)["topN"] == 0


def test_empty_binding_request_is_rejected():
    """什么都没给 → 拒绝（不是「成功但什么都没改」）。"""
    result = _set_binding(_draft(), {})
    assert not result.ok
    reasons = [str(item.get("reason")) for item in result.errors]
    assert any("no_changes" in reason for reason in reasons)
