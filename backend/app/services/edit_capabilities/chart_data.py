"""模拟分类数据契约 —— 饼图 / 分类统计的**唯一**来源。

前后端共用这一份契约：

* **分类维度目录**（``SIMULATED_DIMENSIONS``）给出每个业务维度有哪些分类、
  每个分类的模拟数值。维度是**数据**，不是「本例特例」：换一句话、换一个维度，
  走的还是同一条通路。
* **载荷形状**（``CATEGORICAL_PAYLOAD_CONTRACT``）给出后端造数与前端渲染
  共同认的字段名。标签与数值**一一对应**，顺序一致。

⚠️ 这里的所有数值都是**模拟**的，载荷里必须带 ``simulated`` 标记，
前端据此标注「模拟数据」；不接真实告警源，也不声称是真实统计。
"""
from __future__ import annotations

from typing import Any


#: 分类绑定在区块里的位置：``region["templateBinding"]["previewSpec"]``。
PREVIEW_SPEC_KEY = "categoricalBinding"

#: 载荷里「这是模拟数据」的标记键。
SIMULATED_KEY = "simulated"

#: 分类字段（横轴）的默认字段名。
DEFAULT_CATEGORY_FIELD = "category"

#: 计数结果字段（纵轴）的默认字段名。
DEFAULT_VALUE_FIELD = "count"


# --------------------------------------------------------------------------
# 分类维度目录
# --------------------------------------------------------------------------

#: 每个维度：key / 中文名 / 别名 / 分类 / 数值 / 单位 / 字段名。
#:
#: 别名是**业务词**（用户嘴里的话），key 是**机器名**。模型可以用别名来指，
#: 服务端解析成 key —— 这样「告警等级」「等级分布」「按等级」都能落到同一个维度。
SIMULATED_DIMENSIONS: dict[str, dict[str, Any]] = {
    "alertLevel": {
        "key": "alertLevel",
        "title": "告警等级",
        "aliases": ["告警等级", "等级", "告警级别", "等级分布", "按等级", "告警等级分布"],
        "categoryField": "alertLevel",
        "valueField": "alertCount",
        "unit": "条",
        "categories": [
            {"label": "严重", "value": 18},
            {"label": "重要", "value": 27},
            {"label": "一般", "value": 41},
            {"label": "提示", "value": 12},
        ],
        "statistic": "按告警等级统计告警数量",
    },
    "alertRegion": {
        "key": "alertRegion",
        "title": "告警区域",
        "aliases": ["告警区域", "区域", "区域分布", "按区域", "告警区域分布"],
        "categoryField": "alertRegion",
        "valueField": "alertCount",
        "unit": "条",
        "categories": [
            {"label": "华北机房", "value": 26},
            {"label": "华东机房", "value": 34},
            {"label": "华南机房", "value": 22},
            {"label": "西部机房", "value": 16},
        ],
        "statistic": "按告警区域统计告警数量",
    },
    "alertType": {
        "key": "alertType",
        "title": "告警类型",
        "aliases": ["告警类型", "类型", "类型分布", "按类型", "告警类型分布"],
        "categoryField": "alertType",
        "valueField": "alertCount",
        "unit": "条",
        "categories": [
            {"label": "温度越限", "value": 29},
            {"label": "电压异常", "value": 21},
            {"label": "通信中断", "value": 17},
            {"label": "设备离线", "value": 31},
        ],
        "statistic": "按告警类型统计告警数量",
    },
}


#: 载荷形状契约（后端造数、前端渲染共用）。
CATEGORICAL_PAYLOAD_CONTRACT: dict[str, Any] = {
    "chartKind": "图表类型（LINE / BAR / PIE / AREA）",
    "xLabels": "分类标签数组（横轴 / 扇区标签），与 yValues 一一对应",
    "yValues": "每个分类的数值数组，与 xLabels 一一对应",
    "xField": "分类字段名",
    "yField": "计数结果字段名",
    "unit": "数值单位（如「条」）",
    "simulated": "恒为 true：这份数值是模拟的，前端必须标注",
    "dataSource": "恒为 simulated",
    "categoryLabel": "分类维度的人话名（如「告警等级」）",
    "statistic": "统计口径的人话描述（如「按告警等级统计告警数量」）",
}


def dimension_catalog() -> list[dict[str, Any]]:
    """给模型看的维度清单（可查证，不靠记忆编）。"""
    result: list[dict[str, Any]] = []
    for dim in SIMULATED_DIMENSIONS.values():
        result.append(
            {
                "dimension": dim["key"],
                "title": dim["title"],
                "aliases": list(dim["aliases"]),
                "unit": dim["unit"],
                "statistic": dim["statistic"],
                "categories": [
                    {"label": item["label"], "value": item["value"]}
                    for item in dim["categories"]
                ],
            }
        )
    return result


def resolve_dimension(name: str) -> dict[str, Any] | None:
    """按 key 或业务别名解析维度；认不出来返回 ``None``。

    认不出来**不猜**：调用方要么让模型给显式分类，要么报「没有这个维度」。
    """
    text = str(name or "").strip()
    if not text:
        return None
    if text in SIMULATED_DIMENSIONS:
        return SIMULATED_DIMENSIONS[text]
    lowered = text.lower()
    for dim in SIMULATED_DIMENSIONS.values():
        if dim["key"].lower() == lowered:
            return dim
    for dim in SIMULATED_DIMENSIONS.values():
        for alias in dim["aliases"]:
            if alias == text or alias in text or text in alias:
                return dim
    return None


def normalize_categories(
    categories: Any, dimension: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """把分类整理成 ``[{"label", "value"}]``。

    维度给了就用维度的；模型给了显式分类就用模型的（显式优先，
    这样「按等级但换个数值」也能表达）。
    """
    if isinstance(categories, list) and categories:
        result: list[dict[str, Any]] = []
        for item in categories:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or item.get("name") or "").strip()
            if not label:
                continue
            raw_value = item.get("value", item.get("count"))
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            result.append({"label": label, "value": int(value) if value.is_integer() else value})
        if result:
            return result
    if dimension:
        return [dict(item) for item in dimension["categories"]]
    return []
