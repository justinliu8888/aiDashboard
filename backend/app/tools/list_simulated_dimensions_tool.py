"""list_simulated_dimensions：查当前可用的**模拟分类维度**（只读事实）。

新增分类统计图表（饼图 / 柱状图）时，模型先用它确认维度名与分类，
再把 ``dimension`` 填进新增图表的参数里 —— 不靠记忆编维度，
也不把设备能耗数据换个标题冒充告警分布。

数值全部是**模拟**的，载荷里带 ``simulated`` 标记，前端据此标注。
"""
from __future__ import annotations

from typing import Any

from app.services.edit_capabilities.chart_data import (
    CATEGORICAL_PAYLOAD_CONTRACT,
    dimension_catalog,
)
from app.tools.base import EDIT_PLAN_STATES, ToolCategory


NAME = "list_simulated_dimensions"
DESCRIPTION = (
    "列出当前可用的**模拟分类维度**（如告警等级、告警区域、告警类型），"
    "每个维度有哪些分类、各是多少模拟数值。"
    "新增分类统计图表（饼图 / 柱状图）时先用它确认维度名，"
    "再把 dimension 填进新增图表的参数里。"
    "这些数值都是**模拟**的，不接真实告警源。"
)
PARAMETERS = {
    "type": "object",
    "properties": {},
}
CATEGORY = ToolCategory.FACT
IS_READ_ONLY = True
APPLICABLE_STATES = EDIT_PLAN_STATES


def run() -> dict[str, Any]:
    return {
        "status": "FOUND",
        "dimensions": dimension_catalog(),
        "payloadContract": CATEGORICAL_PAYLOAD_CONTRACT,
        "note": "数值均为模拟数据，会在图上标注；不接真实告警源。",
    }
