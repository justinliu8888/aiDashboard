"""图表的**维度字段**（分类轴）能力：设备 / 设备类型等维度怎么进图表。

**为什么有这一层（验收 11-01 的根因）**

图表区块原先只有一种取数形态：按时间重采样（``RESAMPLE``），输出里只有
``timestamp`` 与 ``energyKwh``。于是：

* 用户说「横轴用设备名称」→ ``deviceName`` **不是这个区域的已声明字段**
  （``require_declared_field`` 直接拒绝），绑定根本设不上；
* 即便硬写进去，造数器也按「角色是 TREND」出**时间桶**，横轴仍然是 0–23
  的小时序号；
* 方案里那句「设备名称」被当成**指标**登记进 ``previewSpec.metrics``，
  造数器照着多指标出**两条数值系列** → 图例把「设备名称」和「耗电量」
  并列成两个堆叠数值系列（这正是用户截图里看到的）。

所以这一层只做三件事，**都不改变协议**：

1. **认出维度字段**：产品自己登记过的显示名（「设备名称」「设备类型」……）
   对应哪个维度字段（``deviceName`` / ``deviceType`` / ``deviceId``），
   以及数据集里那个字段的数据类型；
2. **把维度字段放进区域的可声明字段**：维度字段来自这个区域的**输入
   数据集**，它已经登记过，只是没进输出；
3. **让区域按维度出数**：横轴绑了维度字段时，管线按维度分组
   （``GROUP_AGGREGATE``），输出里声明维度字段，造数器据此按维度出数。

**不改的东西**：协议（``report-definition-v1.md``）、指标目录口径、用户
原话、以及「目录外指标走通用模拟策略」这条既有决定。
"""
from __future__ import annotations

from typing import Any

from app.services.simulated_field_semantics import PRODUCT_COLUMN_VOCABULARY


#: 设备维度字段（数据集 ``ds_room_energy`` 里真实存在的分类字段）。
DEVICE_DIMENSION_FIELDS: tuple[str, ...] = ("deviceId", "deviceName", "deviceType")

#: 维度字段的数据类型（字符串 / 分类 —— 不是数值）。
_DIMENSION_DATA_TYPES: frozenset[str] = frozenset(
    {"STRING", "CATEGORY", "DATETIME"}
)

#: 时间维度（趋势图的横轴）：也是维度，但不是「设备维度」。
TIME_DIMENSION_FIELD = "timestamp"


def _as_dict(value: Any) -> dict[str, Any]:
    """字典或 pydantic 模型都当成字典看（能力层的定义是模型副本）。"""
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            dumped = dump()
        except Exception:  # pragma: no cover - 防御性
            dumped = None
        if isinstance(dumped, dict):
            return dumped
    return {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def canonical_field_of(text: Any) -> str:
    """显示名 → 产品登记过的规范字段身份（认不出就是空串，不近似匹配）。"""
    key = str(text or "").strip()
    if not key:
        return ""
    return str(PRODUCT_COLUMN_VOCABULARY.get(key) or "")


def dataset_fields(definition: Any, region: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """这个区域的**输入数据集**登记了哪些字段（字段名 → 字段声明）。"""
    definition = _as_dict(definition)
    datasets = _as_dict(definition.get("datasets"))
    data = _as_dict(region.get("data"))
    inputs = _as_dict(data.get("inputs"))
    fields: dict[str, dict[str, Any]] = {}
    for spec in inputs.values():
        dataset_ref = str(_as_dict(spec).get("datasetRef") or "").strip()
        dataset = _as_dict(datasets.get(dataset_ref))
        for name, field in _as_dict(dataset.get("fields")).items():
            if name and name not in fields:
                fields[str(name)] = _as_dict(field)
    return fields


def field_data_type(fields: dict[str, dict[str, Any]], field_ref: str) -> str:
    """字段的数据类型（大写）；认不出来是空串。"""
    field = _as_dict(fields.get(str(field_ref or "").strip()))
    return str(field.get("dataType") or "").strip().upper()


def is_dimension_field(fields: dict[str, dict[str, Any]], field_ref: str) -> bool:
    """这个字段是**维度**（分类 / 时间）而不是数值。"""
    name = str(field_ref or "").strip()
    if not name:
        return False
    if name in DEVICE_DIMENSION_FIELDS:
        return True
    data_type = field_data_type(fields, name)
    return bool(data_type) and data_type in _DIMENSION_DATA_TYPES


def is_device_dimension(field_ref: str) -> bool:
    """是不是**设备维度**（设备名称 / 设备类型 / 设备编号）。"""
    return str(field_ref or "").strip() in DEVICE_DIMENSION_FIELDS


def is_dimension_canonical(field_ref: str) -> bool:
    """是不是**维度**（设备维度或时间）—— 这类字段不是「指标」。"""
    name = str(field_ref or "").strip()
    return name in DEVICE_DIMENSION_FIELDS or name == TIME_DIMENSION_FIELD


def dimension_intent_field(
    intents: list[str], fields: dict[str, dict[str, Any]]
) -> str:
    """从指标意图里认出**维度字段**（横轴该用哪个分类字段）。

    认不出就返回空串 —— **宁可留空，也不猜一个轴出来**。
    """
    for intent in intents:
        canonical = canonical_field_of(intent)
        if canonical and is_dimension_field(fields, canonical):
            return canonical
        for name in fields:
            if name == str(intent or "").strip() and is_dimension_field(fields, name):
                return name
    return ""


def numeric_intent_fields(
    intents: list[str], fields: dict[str, dict[str, Any]]
) -> list[str]:
    """从指标意图里认出**数值字段**（纵轴 / 系列该用哪个量）。"""
    found: list[str] = []
    for intent in intents:
        canonical = canonical_field_of(intent)
        if canonical and canonical not in found and not is_dimension_field(
            fields, canonical
        ):
            found.append(canonical)
    return found


def _pipeline(region: dict[str, Any]) -> dict[str, Any]:
    data = _as_dict(region.get("data"))
    return _as_dict(data.get("pipeline"))


def _output_ref(region: dict[str, Any]) -> str:
    binding = _as_dict(region.get("binding"))
    return str(binding.get("outputRef") or "")


def ensure_device_dimension(
    region: dict[str, Any],
    definition: Any,
    *,
    dimension_field: str,
    value_field: str,
) -> bool:
    """让这个区域**按设备维度出数**（管线分组 + 输出声明维度字段）。

    返回 ``True`` 表示管线被改写过。

    改不成的情形（数据集里没有设备维度 / 没有可汇总的数值字段）**返回
    False，不改** —— 由调用方明确拒绝，不静默改成另一套口径。
    """
    dimension = str(dimension_field or "").strip()
    value = str(value_field or "").strip()
    if not is_device_dimension(dimension) or not value:
        return False
    fields = dataset_fields(definition, region)
    if dimension not in fields:
        return False
    if field_data_type(fields, value) not in ("", "DECIMAL", "INTEGER", "NUMBER"):
        # 数值字段的类型不对（不是数值）→ 不改。
        return False

    data = _as_dict(region.get("data"))
    pipeline = _pipeline(region)
    nodes = [node for node in _as_list(pipeline.get("nodes")) if isinstance(node, dict)]
    if not nodes:
        return False
    output_ref = _output_ref(region)
    output_key = output_ref.split(":", 1)[-1] if output_ref else "main"
    outputs = _as_dict(pipeline.get("outputs"))
    output = _as_dict(outputs.get(output_key))
    declared = _as_dict(output.get("expectedFields"))
    if dimension in declared:
        # 已经声明过了（老草稿 / 已经是排名口径）→ 不用改。
        return False

    # 找到承载时间重采样的那个节点，换成按设备分组。
    target_index = -1
    for index, node in enumerate(nodes):
        if str(node.get("type") or "").upper() == "RESAMPLE":
            target_index = index
            break
    if target_index < 0:
        target_index = 0
    group_node = {
        "id": str(nodes[target_index].get("id") or "n_device"),
        "type": "GROUP_AGGREGATE",
        "inputs": list(_as_list(nodes[target_index].get("inputs"))),
        "groupBy": ["deviceId", "deviceName"],
        "measures": [
            {
                "asField": value,
                "aggregation": "SUM",
                "fieldRef": value,
            }
        ],
    }
    nodes[target_index] = group_node
    outputs[output_key] = {
        **output,
        "nodeRef": str(output.get("nodeRef") or f"node:{group_node['id']}"),
        "expectedFields": {
            **declared,
            "deviceId": {"dataType": "STRING"},
            "deviceName": {"dataType": "STRING"},
            value: {
                "dataType": field_data_type(fields, value) or "DECIMAL",
                "unit": str(_as_dict(fields.get(value)).get("unit") or "kWh"),
            },
        },
    }
    region["data"] = {
        **data,
        "pipeline": {**pipeline, "nodes": nodes, "outputs": outputs},
    }
    return True
