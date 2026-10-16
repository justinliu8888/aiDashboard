"""组装机房能耗日报的策略草稿、定义草稿和修改草稿。

字段严格对齐 report-definition-v1.md 1.0；AI 层面只负责提供符合协议的结构，
真实值/口径/权限仍由 Java 后端和注册表校验。
"""
from __future__ import annotations

from typing import Any

from app.data.machine_room_energy import (
    CATALOG_MACHINE_ROOM_ENERGY,
    METRIC_DEVICE_ENERGY,
    METRIC_TOTAL_ENERGY,
)
from app.schemas.change_set import ChangeSet, ChangeSetEntry, ChangeSetUnchanged
from app.schemas.report_definition import ReportDefinitionDraft
from app.schemas.report_policy import ReportPolicyDraft


REGION_IDS = {
    "title": "region_report_title",
    "total": "region_total_energy",
    "ranking": "region_device_ranking",
    "trend": "region_hourly_trend",
    "quality": "region_data_quality",
    "analysis": "region_analysis",
}


def build_policy_draft(report_name: str) -> ReportPolicyDraft:
    return ReportPolicyDraft(
        name=report_name,
        description="由 AI 根据用户输入生成的首版策略草稿",
        subjectRef="machine_room",
        allowedOverrides=["TIME_WINDOW", "GRAIN"],
        executionMode="MANUAL_ONLY",
    )


def _default_time_window_yesterday() -> dict[str, Any]:
    return {
        "mode": "REPLACE",
        "value": {
            "type": "CALENDAR",
            "timezone": "Asia/Shanghai",
            "anchor": {"type": "CONTEXT", "path": "runtime.logicalTime"},
            "unit": "DAY",
            "offset": -1,
            "count": 1,
        },
    }


def _calendar_window(offset: int, count: int) -> dict[str, Any]:
    return {
        "mode": "REPLACE",
        "value": {
            "type": "CALENDAR",
            "timezone": "Asia/Shanghai",
            "anchor": {"type": "CONTEXT", "path": "runtime.logicalTime"},
            "unit": "DAY",
            "offset": offset,
            "count": count,
        },
    }


def _title_region() -> dict[str, Any]:
    return {
        "id": REGION_IDS["title"],
        "name": "报告标题",
        "role": "REPORT_TITLE",
        "layout": {
            "placement": {"type": "GRID", "column": 1, "columnSpan": 12}
        },
        "component": {
            "type": "TEXT",
            "content": {
                "format": "PLAIN",
                "template": "{{roomName}} 昨日能耗日报",
                "bindings": {
                    "roomName": {"type": "CONTEXT", "path": "scope.machineRoomName"}
                },
            },
            "headingLevel": 1,
        },
    }


def _total_energy_region() -> dict[str, Any]:
    return {
        "id": REGION_IDS["total"],
        "name": "昨日总耗电",
        "role": "SUMMARY",
        "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 4}},
        "context": {"timeWindow": _default_time_window_yesterday()},
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
                        "id": "n_total",
                        "type": "METRIC",
                        "inputs": ["input:main"],
                        "metricRef": METRIC_TOTAL_ENERGY["metricRef"],
                        "metricVersion": METRIC_TOTAL_ENERGY["metricVersion"],
                    }
                ],
                "outputs": {
                    "main": {
                        "nodeRef": "node:n_total",
                        "expectedFields": {
                            "energyKwh": {"dataType": "DECIMAL", "unit": "kWh"}
                        },
                    }
                },
            },
        },
        "binding": {"outputRef": "output:main"},
        "component": {
            "type": "METRIC_CARD",
            "metric": {
                "id": "m_total",
                "label": "昨日总耗电",
                "fieldRef": "energyKwh",
                "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
            },
        },
    }


def _ranking_region() -> dict[str, Any]:
    return {
        "id": REGION_IDS["ranking"],
        "name": "设备耗电排名",
        "role": "DETAIL",
        "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12}},
        "context": {"timeWindow": _default_time_window_yesterday()},
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
                        "id": "n_device",
                        "type": "GROUP_AGGREGATE",
                        "inputs": ["input:main"],
                        "groupBy": ["deviceId", "deviceName"],
                        "measures": [
                            {
                                "asField": "energyKwh",
                                "aggregation": "SUM",
                                "fieldRef": "energyKwh",
                            }
                        ],
                    },
                    {
                        "id": "n_sort",
                        "type": "SORT",
                        "inputs": ["node:n_device"],
                        "orderBy": [{"fieldRef": "energyKwh", "direction": "DESC"}],
                    },
                ],
                "outputs": {
                    "main": {
                        "nodeRef": "node:n_sort",
                        "expectedFields": {
                            "deviceId": {"dataType": "STRING"},
                            "deviceName": {"dataType": "STRING"},
                            "energyKwh": {"dataType": "DECIMAL", "unit": "kWh"},
                        },
                    }
                },
            },
        },
        "binding": {
            "outputRef": "output:main",
            "topN": 10,
            "sort": [{"fieldRef": "energyKwh", "direction": "DESC"}],
        },
        "component": {
            "type": "TABLE",
            "columns": [
                {
                    "id": "col_device",
                    "label": "设备",
                    "fieldRef": "deviceName",
                    "format": {"kind": "TEXT"},
                },
                {
                    "id": "col_energy",
                    "label": "耗电 (kWh)",
                    "fieldRef": "energyKwh",
                    "format": {"kind": "NUMBER", "precision": 1, "unitLabel": "kWh"},
                },
            ],
            "rowNumber": True,
        },
    }


def _hourly_trend_region() -> dict[str, Any]:
    return {
        "id": REGION_IDS["trend"],
        "name": "小时趋势",
        "role": "TREND",
        "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12}},
        "context": {
            "timeWindow": _default_time_window_yesterday(),
            "grain": {"mode": "REPLACE", "value": {"unit": "HOUR"}},
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
        "component": {
            "type": "CHART",
            "chartKind": "LINE",
            "encodings": [
                {"channel": "X", "fieldRef": "timestamp"},
                {"channel": "Y", "fieldRef": "energyKwh"},
            ],
            "legend": True,
        },
    }


def _quality_region() -> dict[str, Any]:
    return {
        "id": REGION_IDS["quality"],
        "name": "数据质量",
        "role": "QUALITY",
        "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 6}},
        "component": {
            "type": "DATA_QUALITY",
            "fields": ["COVERAGE", "MISSING", "FRESHNESS"],
            "showDetails": True,
        },
    }


def _analysis_region() -> dict[str, Any]:
    return {
        "id": REGION_IDS["analysis"],
        "name": "分析说明",
        "role": "ANALYSIS",
        "layout": {"placement": {"type": "GRID", "column": 1, "columnSpan": 12}},
        "component": {
            "type": "ANALYSIS",
            "mode": "LLM",
            "goal": "基于总耗电、设备排名和小时趋势，解释关键变化",
            "facts": [
                {
                    "name": "total_energy",
                    "outputRef": f"region:{REGION_IDS['total']}/output:main",
                    "fieldRefs": ["energyKwh"],
                },
                {
                    "name": "device_ranking",
                    "outputRef": f"region:{REGION_IDS['ranking']}/output:main",
                    "fieldRefs": ["deviceId", "deviceName", "energyKwh"],
                },
                {
                    "name": "hourly_trend",
                    "outputRef": f"region:{REGION_IDS['trend']}/output:main",
                    "fieldRefs": ["timestamp", "energyKwh"],
                },
            ],
            "promptTemplateRef": "prompt.machine_room.energy_daily_analysis",
            "requireEvidence": True,
        },
    }


def _default_datasets() -> dict[str, Any]:
    return {
        "ds_room_energy": {
            "sourceRef": CATALOG_MACHINE_ROOM_ENERGY["sourceRef"],
            "sourceVersion": CATALOG_MACHINE_ROOM_ENERGY["sourceVersion"],
            "subjectRef": "machine_room",
            "fields": {
                "deviceId": {
                    "sourceFieldRef": "deviceId",
                    "dataType": "STRING",
                    "semanticType": "IDENTIFIER",
                },
                "deviceName": {
                    "sourceFieldRef": "deviceName",
                    "dataType": "STRING",
                },
                "timestamp": {
                    "sourceFieldRef": "timestamp",
                    "dataType": "DATETIME",
                    "semanticType": "TIMESTAMP",
                },
                "energyKwh": {
                    "sourceFieldRef": "energyKwh",
                    "dataType": "DECIMAL",
                    "unit": "kWh",
                    "semanticType": "INTERVAL_ENERGY",
                },
            },
        }
    }


def _default_intent_bindings() -> list[dict[str, Any]]:
    return [
        {
            "id": "intent_total_energy",
            "sourceText": "总耗电",
            "domain": "METRIC",
            "targetRegionIds": [REGION_IDS["total"]],
            "targetPaths": [f"/regions/{REGION_IDS['total']}"],
            "status": "RESOLVED",
        },
        {
            "id": "intent_device_ranking",
            "sourceText": "设备排名",
            "domain": "METRIC",
            "targetRegionIds": [REGION_IDS["ranking"]],
            "targetPaths": [f"/regions/{REGION_IDS['ranking']}"],
            "status": "RESOLVED",
        },
        {
            "id": "intent_hourly_trend",
            "sourceText": "趋势分析",
            "domain": "CALCULATION",
            "targetRegionIds": [REGION_IDS["trend"]],
            "targetPaths": [f"/regions/{REGION_IDS['trend']}"],
            "status": "RESOLVED",
        },
    ]


def build_definition_draft(
    report_name: str,
    room_id: str,
    include_trend: bool = True,
) -> ReportDefinitionDraft:
    regions: list[dict[str, Any]] = [
        _title_region(),
        _total_energy_region(),
        _ranking_region(),
    ]
    if include_trend:
        regions.append(_hourly_trend_region())
    regions.extend([_quality_region(), _analysis_region()])

    intent_bindings = _default_intent_bindings()
    if not include_trend:
        intent_bindings = [
            b for b in intent_bindings if b["id"] != "intent_hourly_trend"
        ]

    return ReportDefinitionDraft(
        schemaVersion="1.0",
        metadata={
            "name": report_name,
            "description": f"{report_name} 由 AI 生成的首版草稿",
            "revision": 0,
            "status": "DRAFT",
            "tags": ["machine_room", "energy", "daily"],
        },
        parameters={},
        policyBinding={
            "policyRef": "policy_pending",
            "allowedOverrides": ["TIME_WINDOW", "GRAIN"],
            "executionMode": "MANUAL_ONLY",
        },
        defaults={
            "locale": "zh-CN",
            "scope": {
                "subjectRef": "machine_room",
                "selectors": {"machineRoomId": [room_id]},
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
        page={
            "arrangement": {"mode": "GRID", "columns": 12, "gap": {"value": 12, "unit": "PX"}},
            "margins": {
                "top": {"value": 10, "unit": "MM"},
                "right": {"value": 10, "unit": "MM"},
                "bottom": {"value": 10, "unit": "MM"},
                "left": {"value": 10, "unit": "MM"},
            },
        },
        theme={"themeRef": "theme_default", "version": "1.0"},
        datasets=_default_datasets(),
        pipelines={},
        regions=regions,
        outputs={
            "html": {
                "format": "HTML",
                "fileNameTemplate": "machine_room_energy_{{logicalDate}}.html",
            }
        },
        intentBindings=intent_bindings,
        dependencies={
            "dataCatalogVersion": "1.0",
            "componentRegistryVersion": "1.0",
            "operatorRegistryVersion": "1.0",
            "functionRegistryVersion": "1.0",
            "metricRefs": [
                {
                    "ref": METRIC_TOTAL_ENERGY["metricRef"],
                    "version": METRIC_TOTAL_ENERGY["metricVersion"],
                },
                {
                    "ref": METRIC_DEVICE_ENERGY["metricRef"],
                    "version": METRIC_DEVICE_ENERGY["metricVersion"],
                },
            ],
        },
    )


# ---------- ChangeSet 构造 ----------

def build_change_set_trend_7d(base_revision: int) -> ChangeSet:
    """样例 5：把趋势图改成最近 7 天。"""
    return ChangeSet(
        baseDefinitionRevision=base_revision,
        changed=[
            ChangeSetEntry(
                regionId=REGION_IDS["trend"],
                path=f"/regions/{REGION_IDS['trend']}/context/timeWindow",
                after=_calendar_window(offset=-7, count=7),
                summary="趋势图窗口改为近 7 个完整自然日",
            )
        ],
        unchanged=[
            ChangeSetUnchanged(
                regionId=REGION_IDS["total"],
                summary="总耗电卡片仍使用昨日窗口",
            )
        ],
        impact=[
            "需要重新解析窗口",
            "需要重取趋势图数据",
            "不影响昨日总耗电卡片",
        ],
        intentBindings=[
            {
                "id": "intent_trend_7d",
                "sourceText": "把趋势图改成最近 7 天",
                "domain": "TIME_WINDOW",
                "targetRegionIds": [REGION_IDS["trend"]],
                "targetPaths": [
                    f"/regions/{REGION_IDS['trend']}/context/timeWindow"
                ],
                "status": "RESOLVED",
            }
        ],
    )


def build_change_set_remove_trend(base_revision: int) -> ChangeSet:
    """样例 6：删除趋势图，保留总耗电和设备排名。"""
    return ChangeSet(
        baseDefinitionRevision=base_revision,
        changed=[
            ChangeSetEntry(
                regionId=REGION_IDS["trend"],
                path=f"/regions/{REGION_IDS['trend']}",
                after=None,
                summary="删除趋势图区域",
            )
        ],
        unchanged=[
            ChangeSetUnchanged(
                regionId=REGION_IDS["total"], summary="总耗电卡片保留"
            ),
            ChangeSetUnchanged(
                regionId=REGION_IDS["ranking"], summary="设备排名表保留"
            ),
        ],
        impact=["区域列表长度 -1", "不影响总耗电与设备排名计算"],
        intentBindings=[
            {
                "id": "intent_remove_trend",
                "sourceText": "不要趋势图，改成只要总耗电和设备排名",
                "domain": "CONTENT",
                "targetRegionIds": [REGION_IDS["trend"]],
                "targetPaths": [f"/regions/{REGION_IDS['trend']}"],
                "status": "RESOLVED",
            }
        ],
    )
