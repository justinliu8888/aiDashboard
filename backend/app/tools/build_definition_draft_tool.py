"""build_definition_draft：按计划组装合法的 ReportDefinitionDraft（确定性组装）。

Agent 不能自己拼完整定义，必须通过本工具组装，确保结构严格对齐协议。

参数（旧版仍然可用）：
    reportName:   报表名称，由 Agent 根据用户诉求命名
    roomId:       机房 ID
    includeTrend: 是否包含小时趋势图（默认 True）

参数（本轮新增，全部可选，用于让路由层解析出来的条件真正进入定义）：
    sourceRef:          数据源（如 eas_energy_detail）；不给就用默认机房能耗数据源
    grainUnit:          时间粒度（MINUTE / HOUR / DAY ...）
    timeWindow:         相对时间窗，{"unit","count","offset"} 或自然语言串
    metricRefs:         指标引用列表
    componentRequests:  要生成的区块（total / ranking / trend / quality / analysis）
    excludedRegions:    明确不要的区块
    filters:            过滤条件 [{"fieldRef","op","values"}]
    limit / topN:       排名行数
    derivedColumns:     派生列（前一日值 / 增长率 / 占比）
    styleHints:         条件样式（超过阈值标红）
    capabilityEvidence: 模型查证到的能力证据，原样留档

参数（系统填充，模型不需要也不应该自己填）：
    unsupportedStyleRequests: 当前模板不支持的版式/样式诉求清单。
        由路由层从**用户原话**确定性识别得到（独占一行 / 左右等宽 / 柱状图 /
        标题字号·对齐·颜色），模型怎么表达都覆盖得到。组装层会把它写进
        metadata.unsupportedStyleRequests，供上层明确披露「没有生效」。
        不在 PARAMETERS 里暴露给模型：这是系统的判定，不是模型的条件。

条件不被目录 / 指标 / 授权范围支持时抛 ``DefinitionBuildError``：
调用方必须据此阻断，不能退回默认草稿。

返回：
    {policyDraft: {...}, definitionDraft: {...}}
"""
from __future__ import annotations

from typing import Any

from app.services.report_draft_service import (
    build_definition_draft,
    build_policy_draft,
)
from app.tools.base import DRAFT_PLAN_STATES, ToolCategory


NAME = "build_definition_draft"
DESCRIPTION = (
    "根据报表计划组装完整的 ReportPolicyDraft 和 ReportDefinitionDraft。"
    "Agent 必须通过本工具生成草稿，禁止自行拼接协议结构。"
    "用户明确指定的数据源、粒度、时间窗、过滤、TopN、派生列必须通过参数传进来，"
    "不要依赖默认值；条件查不到或不支持时本工具会报错，此时应改条件或明确告知用户不支持。"
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "reportName": {"type": "string", "description": "报表名称"},
        "roomId": {"type": "string", "description": "机房 ID，如 room_a"},
        "includeTrend": {
            "type": "boolean",
            "description": "是否包含小时趋势图",
            "default": True,
        },
        "sourceRef": {
            "type": "string",
            "description": (
                "数据源 ID，如 catalog.machine_room.energy 或 eas_energy_detail。"
                "用户点名了数据源就必须原样传入。"
            ),
        },
        "grainUnit": {
            "type": "string",
            "enum": ["MINUTE", "HOUR", "DAY", "WEEK", "MONTH"],
            "description": "时间粒度。用户说“分钟/小时/每天”时必须传。",
        },
        "timeWindow": {
            "type": "object",
            "description": (
                "相对时间窗，形如 {\"unit\":\"DAY\",\"count\":7,\"offset\":-7} "
                "表示过去 7 个完整自然日；offset 省略时按 -count 推导。"
            ),
            "properties": {
                "unit": {"type": "string", "enum": ["MINUTE", "HOUR", "DAY", "WEEK", "MONTH"]},
                "count": {"type": "integer", "minimum": 1},
                "offset": {"type": "integer"},
                "timezone": {"type": "string"},
            },
        },
        "metricRefs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "指标引用列表，如 metric.machine_room.total_energy",
        },
        "componentRequests": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "要生成的区块：total / ranking / trend / quality / analysis。"
                "用户明确说“只要某几块”时按白名单传；可写成 \"trend:DAY\" 指定粒度。"
            ),
        },
        "excludedRegions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "用户明确不要的区块，例如 ranking / trend / analysis",
        },
        "filters": {
            "type": "array",
            "description": "过滤条件，形如 [{\"fieldRef\":\"deviceName\",\"op\":\"IN\",\"values\":[\"CRAC-01 空调\"]}]",
            "items": {
                "type": "object",
                "properties": {
                    "fieldRef": {"type": "string"},
                    "op": {"type": "string", "enum": ["IN", "NOT_IN", "EQ", "NE", "GT", "LT"]},
                    "values": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["fieldRef"],
            },
        },
        "limit": {
            "type": "integer",
            "description": "排名显示行数（如 Top3 传 3）",
        },
        "topN": {
            "type": "integer",
            "description": "排名显示行数（与 limit 等价）",
        },
        "derivedColumns": {
            "type": "array",
            "description": (
                "派生列。kind 取 PREVIOUS_PERIOD（前一日值）/ GROWTH_RATE（增长率）"
                "/ SHARE_OF_TOTAL（占机房总耗电比例）"
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "fieldRef": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["PREVIOUS_PERIOD", "GROWTH_RATE", "SHARE_OF_TOTAL"],
                    },
                    "sourceFieldRef": {"type": "string"},
                    "format": {"type": "object"},
                },
                "required": ["kind"],
            },
        },
        "styleHints": {
            "type": "array",
            "description": (
                "条件样式，形如 "
                "[{\"targetRegionId\":\"region_device_ranking\",\"fieldRef\":\"growthRatePercent\","
                "\"comparison\":\"GT\",\"threshold\":20,\"style\":\"RED\"}]"
            ),
            "items": {
                "type": "object",
                "properties": {
                    "targetRegionId": {"type": "string"},
                    "fieldRef": {"type": "string"},
                    "comparison": {"type": "string", "enum": ["GT", "GTE", "LT", "LTE", "EQ"]},
                    "threshold": {"type": "number"},
                    "style": {"type": "string"},
                },
                "required": ["fieldRef"],
            },
        },
        "capabilityEvidence": {
            "type": "object",
            "description": "模型查证到的能力证据（如 check_capability 的返回），原样留档",
        },
    },
    "required": ["reportName", "roomId"],
}
CATEGORY = ToolCategory.BUILDER
IS_READ_ONLY = True
# 组装的是"一份新草稿"，只在还在收集需求的节点有意义。
# 草稿已经存在时再组装一份，模型很容易拿它覆盖用户刚改好的内容。
APPLICABLE_STATES = DRAFT_PLAN_STATES


def run(
    reportName: str,
    roomId: str,
    includeTrend: bool = True,
    sourceRef: str | None = None,
    grainUnit: str | None = None,
    timeWindow: dict[str, Any] | str | None = None,
    metricRefs: list[str] | None = None,
    componentRequests: list[str] | None = None,
    excludedRegions: list[str] | None = None,
    filters: list[Any] | None = None,
    limit: int | None = None,
    topN: int | None = None,
    derivedColumns: list[Any] | None = None,
    styleHints: list[Any] | None = None,
    unsupportedStyleRequests: list[Any] | None = None,
    capabilityEvidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = build_policy_draft(reportName)
    definition = build_definition_draft(
        report_name=reportName,
        room_id=roomId,
        include_trend=includeTrend,
        sourceRef=sourceRef,
        grainUnit=grainUnit,
        timeWindow=timeWindow,
        metricRefs=metricRefs,
        componentRequests=componentRequests,
        excludedRegions=excludedRegions,
        filters=filters,
        limit=limit,
        topN=topN,
        derivedColumns=derivedColumns,
        styleHints=styleHints,
        unsupportedStyleRequests=unsupportedStyleRequests,
        capabilityEvidence=capabilityEvidence,
    )
    return {
        "policyDraft": policy.model_dump(exclude_none=True),
        "definitionDraft": definition.model_dump(exclude_none=True),
    }
