"""Part two 定向复验：11-01 图表编码诉求被区域格式快路径吞掉。

对应 [11-第二次手工验收.md] 的第 01 条（用户截图 + 会话
``sess_56e353e62ef6`` / 草稿 ``draft_a5aa8b682a3d`` 已确认）：

* 用户原句：「把「设备耗电排名」图的横轴保持用设备名称，纵轴保持用耗电量，
  纵轴数值格式保留一位小数。」
* 实测：候选里只有一条区域级 ``SET_FORMAT{precision:1}``（对图表**不生效**），
  通道一项都没改；页面上横轴仍是 0–23 的小时序号，图例把「设备名称」和
  「耗电量」并排画成两个**数值系列**。

这里的断言落在**规则层与能力层的真实执行**上（``run_trial`` 在草稿副本上跑），
不碰正式草稿。
"""
from __future__ import annotations

import pytest

from app.schemas.report_definition import ReportDefinitionDraft
from app.services.edit_trial_service import run_trial
from app.services.template_edit_engine import TemplateEditEngine


# --------------------------------------------------------------------------
# 夹具：一份带「设备耗电排名」柱状图的草稿（与用户现场同一形状）
# --------------------------------------------------------------------------

def _draft() -> ReportDefinitionDraft:
    return ReportDefinitionDraft.model_validate(
        {
            "metadata": {"name": "A 机房昨日能耗日报", "revision": 16},
            "datasets": {
                "ds_room_energy": {
                    "sourceRef": "catalog.machine_room.energy",
                    "sourceVersion": "1.0",
                    "fields": {
                        "deviceId": {"sourceFieldRef": "deviceId", "dataType": "STRING"},
                        "deviceName": {"sourceFieldRef": "deviceName", "dataType": "STRING"},
                        "deviceType": {"sourceFieldRef": "deviceType", "dataType": "STRING"},
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
                    "id": "region_4",
                    "name": "设备耗电排名",
                    "role": "TREND",
                    "component": {
                        "type": "CHART",
                        "chartKind": "BAR",
                        # 用户现场：横轴是时间、纵轴是耗电量（错的绑定）。
                        "encodings": [
                            {"channel": "X", "fieldRef": "timestamp"},
                            {"channel": "Y", "fieldRef": "energyKwh"},
                        ],
                        "legend": True,
                    },
                    "data": {
                        "inputs": {
                            "main": {
                                "type": "DATASET",
                                "datasetRef": "ds_room_energy",
                                "periodRef": "CURRENT",
                            }
                        },
                        "pipeline": {
                            "nodes": [
                                {
                                    "id": "n_resample",
                                    "type": "RESAMPLE",
                                    "inputs": ["input:main"],
                                    "timeFieldRef": "timestamp",
                                    "grain": {"unit": "HOUR"},
                                    "measures": [
                                        {
                                            "asField": "energyKwh",
                                            "aggregation": "SUM",
                                            "fieldRef": "energyKwh",
                                        }
                                    ],
                                }
                            ],
                            "outputs": {
                                "main": {
                                    "nodeRef": "node:n_resample",
                                    "expectedFields": {
                                        "timestamp": {"dataType": "DATETIME"},
                                        "energyKwh": {"dataType": "DECIMAL", "unit": "kWh"},
                                    },
                                }
                            },
                        },
                    },
                    "binding": {"outputRef": "output:main"},
                    "templateBinding": {
                        "sectionId": "section_4",
                        "dataStatus": "bound",
                        "metricRefs": ["metric.machine_room.total_energy"],
                        "previewSpec": {
                            "role": "TREND",
                            "kind": "generic",
                            "unit": None,
                            # 方案里那句「设备名称」被当成了**指标**（错）。
                            "metrics": [
                                {
                                    "metricKey": "metric.generic.section_4_1",
                                    "label": "设备名称",
                                    "kind": "generic",
                                    "unit": None,
                                    "capabilityPending": False,
                                    "pendingReason": None,
                                    "bindingKind": "simulated",
                                    "simulatedId": "simulated.section_4.1",
                                    "deterministicScope": {
                                        "scope": "metric_family",
                                        "seedKey": "generic:metric.generic.section_4_1",
                                    },
                                },
                                {
                                    "metricKey": "metric.generic.section_4_2",
                                    "label": "耗电量",
                                    "kind": "generic",
                                    "unit": "kWh",
                                    "capabilityPending": False,
                                    "pendingReason": None,
                                    "bindingKind": "simulated",
                                    "simulatedId": "simulated.section_4.2",
                                    "deterministicScope": {
                                        "scope": "metric_family",
                                        "seedKey": "energy",
                                    },
                                },
                            ],
                        },
                    },
                }
            ],
        }
    )


def _region(definition: ReportDefinitionDraft, region_id: str = "region_4") -> dict:
    for region in definition.regions:
        if isinstance(region, dict):
            if str(region.get("id")) == region_id:
                return region
        elif region.id == region_id:
            return region.model_dump()
    raise AssertionError(f"草稿里没有 {region_id}")


# --------------------------------------------------------------------------
# 规则层：图表的格式诉求不能被区域级数字格式规则接走
# --------------------------------------------------------------------------

def test_number_format_rule_does_not_claim_chart_regions() -> None:
    """图表的数值格式在通道 / 坐标轴上，不在区域级 ``region.format`` 上。"""
    engine = TemplateEditEngine()
    region = {
        "id": "region_4",
        "name": "设备耗电排名",
        "component": {"type": "CHART", "chartKind": "BAR"},
    }
    text = "把「设备耗电排名」图的横轴保持用设备名称，纵轴保持用耗电量，纵轴数值格式保留一位小数。"
    assert engine._parse_number_format(text, region) is None


def test_number_format_rule_still_claims_plain_regions() -> None:
    """其它区块的数字格式规则**不受影响**（没有把能力改瘫）。"""
    engine = TemplateEditEngine()
    region = {
        "id": "region_9",
        "name": "某文本块",
        "component": {"type": "TEXT"},
    }
    operation = engine._parse_number_format("保留两位小数", region)
    assert operation is not None
    assert operation["op"] == "SET_FORMAT"
    assert operation["params"]["format"]["precision"] == 2


# --------------------------------------------------------------------------
# 能力层：横轴绑设备分类字段
# --------------------------------------------------------------------------

def _set_encodings(definition: ReportDefinitionDraft, encodings: list[dict]) -> object:
    return run_trial(
        definition=definition,
        operations=[
            {
                "op": "SET_CHART_ENCODINGS",
                "target": {"regionId": "region_4"},
                "params": {"encodings": encodings},
            }
        ],
    )


def test_x_channel_can_bind_the_device_dimension() -> None:
    """「横轴用设备名称」要能绑上：管线按设备分组、输出声明维度字段。"""
    result = _set_encodings(
        _draft(),
        [
            {"channel": "X", "fieldRef": "deviceName"},
            {"channel": "Y", "fieldRef": "energyKwh"},
        ],
    )
    assert result.ok, result.errors
    region = _region(result.definition)
    encodings = {
        item["channel"]: item["fieldRef"]
        for item in region["component"]["encodings"]
    }
    assert encodings["X"] == "deviceName"
    assert encodings["Y"] == "energyKwh"
    nodes = region["data"]["pipeline"]["nodes"]
    assert any(node["type"] == "GROUP_AGGREGATE" for node in nodes)
    declared = region["data"]["pipeline"]["outputs"]["main"]["expectedFields"]
    assert "deviceName" in declared


def test_device_dimension_is_not_kept_as_a_numeric_metric() -> None:
    """分类字段不再留在指标清单里（否则图例把它画成第二条数值系列）。"""
    result = _set_encodings(
        _draft(),
        [
            {"channel": "X", "fieldRef": "deviceName"},
            {"channel": "Y", "fieldRef": "energyKwh"},
        ],
    )
    assert result.ok, result.errors
    region = _region(result.definition)
    metrics = region["templateBinding"]["previewSpec"]["metrics"]
    labels = [item["label"] for item in metrics]
    assert "设备名称" not in labels
    assert "耗电量" in labels


def test_unknown_field_is_still_rejected() -> None:
    """没登记的字段照旧明确拒绝（不因为这次改动放宽准入）。"""
    result = _set_encodings(
        _draft(),
        [
            {"channel": "X", "fieldRef": "notRegisteredField"},
            {"channel": "Y", "fieldRef": "energyKwh"},
        ],
    )
    assert not result.ok
    reasons = [str(item.get("reason")) for item in result.errors]
    assert any("unknown_field_ref" in reason for reason in reasons)


def test_value_field_on_x_axis_is_rejected() -> None:
    """数值字段不能放在横轴（横轴要分类字段）。"""
    result = _set_encodings(
        _draft(),
        [
            {"channel": "X", "fieldRef": "energyKwh"},
            {"channel": "Y", "fieldRef": "energyKwh"},
        ],
    )
    # 数值字段不是维度字段 → 走「字段不可用」的明确拒绝，不静默接受。
    assert not result.ok


# --------------------------------------------------------------------------
# 设备粒度明细表：时间列必须有值
# --------------------------------------------------------------------------

def test_device_grain_table_time_column_has_value():
    """设备粒度下「时间」列不能是空的。

    Part two 起点草稿的「设备耗电明细」表同时有设备列和时间列；
    设备粒度走的是「每台设备一行」分支，那条分支原来把 bucket 传成 None，
    时间列于是渲染成空白格（用户看到的是一张有列名、没内容的表）。
    """
    from app.services.preview_service import (
        build_report_preview_payload,
        resolve_preview_context,
    )

    definition = {
        "metadata": {"name": "设备明细", "revision": 1},
        "defaults": {
            "locale": "zh-CN",
            "scope": {"subjectRef": "machine_room"},
            "source": {
                "sourceRef": "catalog.machine_room.energy",
                "sourceVersion": "1.0",
                "subjectRef": "machine_room",
            },
            "timeWindow": {
                "type": "CALENDAR",
                "timezone": "Asia/Shanghai",
                "anchor": {"type": "CONTEXT", "path": "runtime.logicalTime"},
                "unit": "DAY",
                "offset": -1,
                "count": 1,
            },
            "grain": {"unit": "HOUR"},
        },
        "datasets": {
            "ds_room_energy": {
                "sourceRef": "catalog.machine_room.energy",
                "sourceVersion": "1.0",
                "subjectRef": "machine_room",
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
                    "summaries": [
                        {
                            "label": "耗电量合计",
                            "outputRef": "output:main",
                            "fieldRef": "energyKwh",
                            "basis": "FULL_RESULT",
                        }
                    ],
                },
                "data": {
                    "inputs": {
                        "main": {
                            "type": "DATASET",
                            "datasetRef": "ds_room_energy",
                            "periodRef": "CURRENT",
                        }
                    },
                    "pipeline": {
                        "nodes": [
                            {
                                "id": "n_resample",
                                "type": "RESAMPLE",
                                "inputs": ["input:main"],
                                "timeFieldRef": "timestamp",
                                "grain": {"unit": "HOUR"},
                                "measures": [
                                    {
                                        "asField": "energyKwh",
                                        "aggregation": "SUM",
                                        "fieldRef": "energyKwh",
                                    }
                                ],
                            }
                        ],
                        "outputs": {
                            "main": {
                                "nodeRef": "node:n_resample",
                                "expectedFields": {
                                    "timestamp": {"dataType": "DATETIME"},
                                    "energyKwh": {"dataType": "DECIMAL", "unit": "kWh"},
                                },
                            }
                        },
                    },
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

    context = resolve_preview_context(definition, plan=None, existing=None)
    payload = build_report_preview_payload(definition, context, plan=None)
    table = next(
        r for r in payload["regions"] if r.get("componentType") == "TABLE"
    )
    rows = table["payload"]["rows"]

    # 设备粒度：每台设备一行。
    assert len(rows) == 10
    # 设备名是真的设备名（不是目录外的通用示意值）。
    assert all(str(row["deviceName"]).strip() for row in rows)
    # **时间列有值**（原来这里是空字符串）。
    assert all(str(row["timestamp"]).strip() for row in rows)
    # 数值列有值，且设备级数值里既有 >400 也有 <400 的记录。
    values = [row["energyKwh"] for row in rows]
    assert any(v > 400 for v in values)
    assert any(v < 400 for v in values)
    # 全量合计还在。
    summary = table["payload"]["summaries"][0]
    assert summary["basis"] == "FULL_RESULT"
    assert round(summary["value"], 1) == round(sum(values), 1)

# --------------------------------------------------------------------------
# 载荷与图形：声明的横轴类别、纵轴字段都要真的生效
# --------------------------------------------------------------------------

def _chart_payload(definition: ReportDefinitionDraft) -> dict:
    """按当前定义生成预览载荷，取「设备耗电排名」那块图表的载荷。"""
    from app.services.preview_service import (
        build_report_preview_payload,
        resolve_preview_context,
    )

    raw = definition.model_dump()
    context = resolve_preview_context(raw, plan=None, existing=None)
    payload = build_report_preview_payload(raw, context, plan=None)
    for region in payload["regions"]:
        if region.get("name") == "设备耗电排名":
            return region["payload"]
    raise AssertionError("预览载荷里没有「设备耗电排名」")


def test_declared_device_axis_gives_device_names_not_indices():
    """横轴绑设备名称 → 载荷的横轴标签就是**设备名**（不是序号、不是小时）。"""
    result = _set_encodings(
        _draft(),
        [
            {"channel": "X", "fieldRef": "deviceName"},
            {"channel": "Y", "fieldRef": "energyKwh"},
        ],
    )
    assert result.ok, result.errors
    payload = _chart_payload(result.definition)
    labels = payload.get("xLabels") or []
    assert len(labels) == 10
    assert all(isinstance(item, str) and item.strip() for item in labels)
    # 设备名（不是 0..9 这种序号）
    assert not all(item.isdigit() for item in labels)
    assert any("机架" in item or "配电" in item or "空调" in item for item in labels)


def test_declared_value_field_drives_the_values():
    """纵轴声明耗电量 → 每个点的值就是**各设备的能耗**（不是通用示意值）。

    区域里的指标登记成「通用示意指标」时，也不能拿示意值冒充耗电量。
    """
    result = _set_encodings(
        _draft(),
        [
            {"channel": "X", "fieldRef": "deviceName"},
            {"channel": "Y", "fieldRef": "energyKwh"},
        ],
    )
    assert result.ok, result.errors
    payload = _chart_payload(result.definition)
    assert payload.get("yField") == "energyKwh"
    values = payload.get("yValues") or []
    assert len(values) == 10
    # 设备能耗是几百到几千的量级；通用示意值只有 10~90。
    assert all(v > 100 for v in values), values
    # 载荷自己说「按声明出数」。
    assert payload.get("encodingDriven") is True
