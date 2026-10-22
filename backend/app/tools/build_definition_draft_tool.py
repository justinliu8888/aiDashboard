"""build_definition_draft：按计划组装合法的 ReportDefinitionDraft（确定性组装）。

Agent 不能自己拼完整定义，必须通过本工具组装，确保结构严格对齐协议。

参数：
    reportName:   报表名称，由 Agent 根据用户诉求命名
    roomId:       机房 ID
    includeTrend: 是否包含小时趋势图（默认 True）

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
    },
    "required": ["reportName", "roomId"],
}
CATEGORY = ToolCategory.BUILDER
IS_READ_ONLY = True
# 组装的是"一份新草稿"，只在还在收集需求的节点有意义。
# 草稿已经存在时再组装一份，模型很容易拿它覆盖用户刚改好的内容。
APPLICABLE_STATES = DRAFT_PLAN_STATES


def run(reportName: str, roomId: str, includeTrend: bool = True) -> dict[str, Any]:
    policy = build_policy_draft(reportName)
    definition = build_definition_draft(
        report_name=reportName,
        room_id=roomId,
        include_trend=includeTrend,
    )
    return {
        "policyDraft": policy.model_dump(exclude_none=True),
        "definitionDraft": definition.model_dump(exclude_none=True),
    }
