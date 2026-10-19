"""DefinitionBuilderSkill：根据 Agent 产出的"报表计划"组装合法的 ReportDefinitionDraft。

Agent 不能自己拼完整定义，必须通过本 Skill 组装，确保结构严格对齐协议。

参数：
    reportName:   报表名称，由 Agent 根据用户诉求命名
    roomId:       机房 ID
    includeTrend: 是否包含小时趋势图（默认 True）

返回：
    {policyDraft: {...}, definitionDraft: {...}}
"""
from __future__ import annotations

from app.services.report_draft_service import (
    build_definition_draft,
    build_policy_draft,
)


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


def run(reportName: str, roomId: str, includeTrend: bool = True) -> dict:
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
