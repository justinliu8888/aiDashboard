"""check_product_capability：核对一项报表能力在当前产品链路的真实边界（只读事实）。

用户问「你支持分钟趋势吗」「能耗成本能出吗」「PUE 支持吗」这类问题时，
用这个工具拿**可核实的事实**，不要凭印象承诺。

参数：
    sourceRef:  可选，数据源 ID
    metricRef:  可选，指标 ID
    grainUnit:  可选，时间粒度（MINUTE / HOUR / DAY / WEEK / MONTH）
    component:  可选，报表区块（total / ranking / trend / quality / analysis）

返回：
    {outcome, catalog, metric, execution, dataSource, alternatives, clarification, ...}

    outcome 五态，含义互不重叠：
        OK                        事实已查到，当前链路支持
        REGISTERED_NOT_SUPPORTED  目录里登记了，但当前链路不支持
        NOT_REGISTERED            当前授权目录里确实没有
        NOT_AUTHORIZED            存在但当前账号无权限（不等于不存在）
        MISSING_CONTEXT           上下文不足，已给产品范围并提出澄清

``check_capability`` 只回答「这个源支不支持某个粒度」；
本工具回答的是更完整的「这项能力当前到底能不能用」，
包含目录登记、生成 / 编辑 / 预览链路的差异，以及模拟数据与生产接通的边界。

本工具只读：不生成、不修改、不保存、不预览、不推进任何业务对象。
"""
from __future__ import annotations

from typing import Any

from app.services import product_capability_service
from app.tools.base import LOOKUP_STATES, ToolCategory
from app.tools.context import require_scope


NAME = "check_product_capability"
DESCRIPTION = (
    "核对一项报表能力在当前产品链路的真实边界：目录是否登记、生成/编辑/预览链路是否真的支持、"
    "预览数值来自模拟业务数据还是已接通生产取数。"
    "用户问「支持分钟趋势吗」「能耗成本或 PUE 能出吗」「这个源能做趋势和排名吗」时用它。"
    "只回答能力边界，不创建、不修改任何报表。"
    "返回 outcome 区分：OK（支持）、REGISTERED_NOT_SUPPORTED（目录登记但链路不支持）、"
    "NOT_REGISTERED（授权目录里没有）、NOT_AUTHORIZED（无权限）、"
    "MISSING_CONTEXT（上下文不足，已给产品范围）。"
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "sourceRef": {
            "type": "string",
            "description": "可选，数据源 ID，如 catalog.machine_room.energy 或 eas_energy_detail",
        },
        "metricRef": {
            "type": "string",
            "description": "可选，指标 ID，如 metric.machine_room.total_energy",
        },
        "grainUnit": {
            "type": "string",
            "enum": ["MINUTE", "HOUR", "DAY", "WEEK", "MONTH"],
            "description": "可选，时间粒度",
        },
        "component": {
            "type": "string",
            "enum": ["total", "ranking", "trend", "quality", "analysis"],
            "description": "可选，报表区块",
        },
    },
}
CATEGORY = ToolCategory.FACT
IS_READ_ONLY = True
APPLICABLE_STATES = LOOKUP_STATES


def run(
    sourceRef: str | None = None,
    metricRef: str | None = None,
    grainUnit: str | None = None,
    component: str | None = None,
) -> dict[str, Any]:
    """核对能力边界。

    授权范围从本轮上下文取：没有范围就查不了，宁可查不到，
    也不能让模型问到当前账号看不到的数据源。

    工具失败（注册表不可用等）直接向上抛，由统一注册层转成 error 结果：
    失败绝不能降级成「不支持」或「数据不存在」。
    """
    scope = require_scope()
    return product_capability_service.describe_capability(
        project_id=scope.project_id,
        subject_ref=scope.subject_ref,
        subject_id=scope.subject_id,
        source_ref=sourceRef,
        metric_ref=metricRef,
        grain_unit=grainUnit,
        component=component,
    )
