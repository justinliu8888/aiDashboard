"""submit_draft_plan：提交本轮生成的报表计划（终结工具，节点 1）。

终结工具：调用即代表本轮结论已定，模型不应再调用其他工具。
产出由确定性层消费：``build_definition_draft`` 组装完整草稿。

本轮扩展：计划里可以带上**结构化条件**（数据源 / 粒度 / 时间窗 / 指标 /
过滤 / TopN / 派生列 / 条件样式 / 排除区域）。这些字段会原样传给
``build_definition_draft``，不会在组装那一步被默认值覆盖掉。

旧字段（reportName / roomId / includeTrend）保持不变，缺省行为与旧版一致。
"""
from __future__ import annotations

from typing import Any

from app.schemas.agent import AgentState
from app.tools.base import DRAFT_PLAN_STATES, ToolCategory
from app.tools.schemas import DraftPlanPayload
from app.tools.submission import submit_payload


NAME = "submit_draft_plan"
DESCRIPTION = (
    "提交本轮生成的报表计划，这是本轮的最终结论，调用后不要再调用其他工具。"
    "reportName 是报表名，roomId 是机房 ID，includeTrend 表示是否包含趋势图。"
    "确认数据源、指标、粒度都查得到之后再提交。"
    "用户明确说过的条件必须一并提交，不要只报名字："
    "sourceRef（数据源）、grainUnit（粒度）、timeWindow（时间窗）、"
    "metricRefs（指标）、componentRequests（要哪些区块）、excludedRegions（不要哪些区块）、"
    "filters（过滤）、topN（排名行数）、derivedColumns（派生列）、styleHints（条件样式）。"
)
PARAMETERS = DraftPlanPayload.model_json_schema()
CATEGORY = ToolCategory.TERMINAL
IS_READ_ONLY = False
# 只有"还在描述需求、还没有草稿"的节点能交草稿计划。
# 在草稿已就绪的节点交新计划会把用户刚改好的草稿整个覆盖掉。
APPLICABLE_STATES = DRAFT_PLAN_STATES
CONCLUSION_STATES = (AgentState.WAITING_USER_CONFIRM.value,)


def run(
    reportName: str,
    roomId: str,
    includeTrend: bool = True,
    reply: str = "",
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
    capabilityEvidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return submit_payload(
        NAME,
        DraftPlanPayload,
        {
            "reportName": reportName,
            "roomId": roomId,
            "includeTrend": includeTrend,
            "reply": reply,
            "sourceRef": sourceRef,
            "grainUnit": grainUnit,
            "timeWindow": timeWindow,
            "metricRefs": metricRefs or [],
            "componentRequests": componentRequests or [],
            "excludedRegions": excludedRegions or [],
            "filters": filters or [],
            "limit": limit,
            "topN": topN,
            "derivedColumns": derivedColumns or [],
            "styleHints": styleHints or [],
            "capabilityEvidence": capabilityEvidence or {},
        },
    )
