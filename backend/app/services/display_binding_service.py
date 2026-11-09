"""展示绑定与来源解析：**一批组件共用同一份**「哪里来的字段 / 值怎么取」。

**为什么有这一层**：第二批要接的组件（文本、单指标卡、多指标组、表格、
图表）都要回答同一组问题 ——

* 这个区域**当前有哪些输出**可以绑？字段叫什么？类型和单位是什么？
* 我给了一个 ``fieldRef``，它**真的存在**吗？值是几行？多行能确定取哪一行吗？
* 展示别名、���示过滤、排序、前 N 项，按什么口径执行？
* 汇总引用的是哪个输出？是全量口径还是可见行口径？

这些规则**只写一次**在这里，各组件包调用同一份函数 —— 不各造一套同义
实现，也不在组件里手填业务结果。

**这里的事实边界**（不伪装成协议新字段）：

* 来源是**当前区域已登记的**输出：``data.pipeline.outputs`` / ``data.inputs``
  / ``meta:quality``。协议里 ``outputRef`` 的三种写法
  （``output:<name>`` / ``input:<alias>`` / ``meta:quality``）在这里解析。
* 字段清单来自**输出自己声明的** ``expectedFields``；模拟载荷里实际出现的
  字段也一并算作「当前真实存在」。
* 取值按**当前载荷**求值：多行且值不唯一 → **报错并要求澄清**，
  **不擅自取第一行、不求和、不编一个数**。
* 展示裁剪（别名 / 过滤 / 排序 / 前 N 项）**只作用展示记录**：
  源输出、全量总计、其他区域的输入**不动**。

本模块**不取生产数据、不存库、不调模型**，只做纯解析与纯计算。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------
# 错误
# --------------------------------------------------------------------------

class DisplayBindingError(Exception):
    """来源解析 / 取值失败。

    字段与能力层的错误形状一致（``reason`` 是机器可读的原因码），
    由调用方转成 :class:`~app.services.edit_capabilities.base.CapabilityError`。
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = "invalid_binding",
        constraint: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.constraint = constraint
        self.detail = dict(detail or {})


# --------------------------------------------------------------------------
# 模拟字段目录（当前开发阶段可用的**受控单值**字段）
# --------------------------------------------------------------------------
#
# **为什么有它**：第二批要绑的是「**已有输出里的字段**」。当前是模拟数据
# 阶段，没有生产计算引擎，因此本目录就是**当前真正可用的字段清单** ——
# 像图表的 ``SIMULATED_DIMENSIONS`` 一样，它是一份**真��登记的目录**，
# 不是随手编的字段名。
#
# 每个字段给出：**取值键**（稳定）/ 显示名 / 数据类型 / 单位 / 形状。
# ``shape="single"`` 表示**单值**（多行时取值唯一，不产生歧义）。

SIMULATED_FIELD_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "name": "totalAlarmCount",
        "label": "告警总数",
        "dataType": "INTEGER",
        "unit": "条",
        "shape": "single",
    },
    {
        "name": "seriousAlarmCount",
        "label": "严重告警数",
        "dataType": "INTEGER",
        "unit": "条",
        "shape": "single",
    },
    {
        "name": "processingAlarmCount",
        "label": "处理中告警数",
        "dataType": "INTEGER",
        "unit": "条",
        "shape": "single",
    },
    {
        "name": "deviceCount",
        "label": "设备总数",
        "dataType": "INTEGER",
        "unit": "台",
        "shape": "single",
    },
    {
        "name": "runningDeviceCount",
        "label": "运行设备数",
        "dataType": "INTEGER",
        "unit": "台",
        "shape": "single",
    },
    {
        "name": "totalEnergyKwh",
        "label": "总耗电量",
        "dataType": "DECIMAL",
        "unit": "kWh",
        "shape": "single",
    },
    {
        # 模板里那张指标卡绑的就是这个取值键（``energyKwh``）。
        # **必须登记**：没登记就查不到它声明的单位，换字段时旧的单位标签
        # 会被带着走 —— 「5,297 台」（验收 08-03 的单位串掉）。
        "name": "energyKwh",
        "label": "昨日总耗电",
        "dataType": "DECIMAL",
        "unit": "kWh",
        "shape": "single",
    },
    {
        "name": "totalCost",
        "label": "总电费",
        "dataType": "DECIMAL",
        "unit": "元",
        "shape": "single",
    },
    {
        "name": "averagePue",
        "label": "平均 PUE",
        "dataType": "DECIMAL",
        "unit": "",
        "shape": "single",
    },
    {
        "name": "roomName",
        "label": "机房名称",
        "dataType": "TEXT",
        "unit": "",
        "shape": "single",
    },
    {
        "name": "logicalDate",
        "label": "逻辑日期",
        "dataType": "DATE",
        "unit": "",
        "shape": "single",
    },
)


def simulated_field_names() -> list[str]:
    """模拟字段目录的取值键清单。"""
    return [str(item["name"]) for item in SIMULATED_FIELD_CATALOG]


def simulated_field(name: str) -> dict[str, Any] | None:
    """按取值键取一个模拟字段（认不出来就 ``None``，不近似匹配）。"""
    key = str(name or "").strip()
    for item in SIMULATED_FIELD_CATALOG:
        if item["name"] == key:
            return dict(item)
    return None


def simulated_field_catalog() -> list[dict[str, Any]]:
    """模拟字段目录（给模型看的清单）。"""
    return [dict(item) for item in SIMULATED_FIELD_CATALOG]


# --------------------------------------------------------------------------
# 注册图标
# --------------------------------------------------------------------------
#
# 项目**没有**本地图标精灵 / 图标字体：既有图标机制是 vendored 的
# ``iconify-icon`` 组件（``frontend/pages/vendor/iconify-icon.min.js``），
# 页面里既有图标都用 ``<iconify-icon icon="mdi:NAME">``。
#
# 所以「注册图标」= **后端登记的一份固定图标清单**：只认清单里的引用，
# 未知引用一律拒绝；**不接受任意图片 URL**。清单项给出图标键与它对应的
# iconify 名称，前端按后端给的名称渲染。

ICON_REGISTRY: dict[str, dict[str, str]] = {
    "alert": {"icon": "mdi:alert-outline", "label": "告警"},
    "warning": {"icon": "mdi:alert-circle-outline", "label": "警告"},
    "device": {"icon": "mdi:server-outline", "label": "设备"},
    "energy": {"icon": "mdi:flash-outline", "label": "能耗"},
    "temperature": {"icon": "mdi:thermometer", "label": "温度"},
    "cost": {"icon": "mdi:currency-cny", "label": "电费"},
    "quality": {"icon": "mdi:check-circle-outline", "label": "质量"},
    "analysis": {"icon": "mdi:chart-line", "label": "分析"},
}


def icon_catalog() -> list[dict[str, Any]]:
    """注册图标清单（给模型看的）。"""
    return [
        {"ref": key, "icon": spec["icon"], "label": spec["label"]}
        for key, spec in sorted(ICON_REGISTRY.items())
    ]


def resolve_icon_ref(icon_ref: str) -> dict[str, Any]:
    """图标引用 → 注册项；**未注册的引用一律拒绝**。"""
    key = str(icon_ref or "").strip()
    spec = ICON_REGISTRY.get(key)
    if spec is None:
        available = "、".join(sorted(ICON_REGISTRY))
        raise DisplayBindingError(
            f"图标引用 {key!r} 没有注册；可用图标：{available}。",
            reason="unknown_icon_ref",
            constraint="图标必须是注册清单里的引用",
            detail={"iconRef": key, "available": available},
        )
    return {"ref": key, "icon": spec["icon"], "label": spec["label"]}


# --------------------------------------------------------------------------
# 编辑期可核对的字段集合（没有载荷时也能判「字段存不存在」）
# --------------------------------------------------------------------------

def declared_field_candidates(region: dict[str, Any]) -> list[str]:
    """定义里**当前已声明**的全部字段名（编辑期可核对的来源）。

    汇总四处：输出声明的 ``expectedFields``、``previewSpec`` 的列与指标、
    组件自己的列与编码、以及指标项的 ``fieldRef``。
    """
    names: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        names.append(text)

    for spec in _pipeline_outputs(region).values():
        for field in _expected_fields(spec if isinstance(spec, dict) else {}):
            _add(field["name"])
    binding = region.get("templateBinding") if isinstance(region, dict) else None
    spec = (binding or {}).get("previewSpec") if isinstance(binding, dict) else None
    spec = spec if isinstance(spec, dict) else {}
    for column in spec.get("columns") or []:
        if isinstance(column, dict):
            _add(column.get("fieldRef"))
    for metric in spec.get("metrics") or []:
        if isinstance(metric, dict):
            _add(metric.get("metricKey"))
            _add(metric.get("fieldRef"))
    component = region.get("component") if isinstance(region, dict) else None
    component = component if isinstance(component, dict) else {}
    for column in component.get("columns") or []:
        if isinstance(column, dict):
            _add(column.get("fieldRef"))
    for encoding in component.get("encodings") or []:
        if isinstance(encoding, dict):
            _add(encoding.get("fieldRef"))
    metric_item = component.get("metric")
    if isinstance(metric_item, dict):
        _add(metric_item.get("fieldRef"))
    for item in component.get("metrics") or []:
        if isinstance(item, dict):
            _add(item.get("fieldRef"))
    return names


def require_declared_field(
    region: dict[str, Any], field_ref: str, *, what: str = "字段"
) -> str:
    """编辑期：字段必须**已经声明**（或属于模拟字段目录），否则明确报错。

    不按标题猜、不按名字近似匹配、不接受未登记的引用。
    """
    name = str(field_ref or "").strip()
    if not name:
        raise DisplayBindingError(
            f"没有给出{what}名（fieldRef）。",
            reason="missing_field_ref",
            constraint=f"{what}名不能为空",
        )
    if name in declared_field_candidates(region):
        return name
    if simulated_field(name) is not None:
        return name
    known = "、".join(declared_field_candidates(region)) or "（无）"
    catalog = "、".join(simulated_field_names())
    raise DisplayBindingError(
        f"{what} {name!r} 当前不可用：区域已声明的字段是 {known}；"
        f"当前模拟数据阶段可用的单值字段是 {catalog}。",
        reason="unknown_field_ref",
        constraint=f"{what}必须来自当前已登记的输出或模拟字段目录",
        detail={"fieldRef": name, "declared": known, "catalog": catalog},
    )


# --------------------------------------------------------------------------
# 输出引用的三种写法（协议 DataBinding.outputRef）
# --------------------------------------------------------------------------

#: ``output:<name>`` —— 区域 pipeline 里的计算输出。
OUTPUT_PREFIX = "output:"
#: ``input:<alias>`` —— 区域登记的输入。
INPUT_PREFIX = "input:"
#: ``meta:quality`` —— 数据质量元输出。
META_QUALITY_REF = "meta:quality"

#: ``meta:quality`` 的字段（数据质量组件真正产出的那几个）。
META_QUALITY_FIELDS = (
    {"name": "coverage", "dataType": "DECIMAL", "unit": ""},
    {"name": "missing", "dataType": "INTEGER", "unit": ""},
    {"name": "duplicates", "dataType": "INTEGER", "unit": ""},
    {"name": "freshnessSeconds", "dataType": "INTEGER", "unit": "秒"},
)


# --------------------------------------------------------------------------
# 输出 / 字段枚举
# --------------------------------------------------------------------------

def _pipeline_outputs(region: dict[str, Any]) -> dict[str, dict[str, Any]]:
    data = region.get("data") if isinstance(region, dict) else None
    pipeline = (data or {}).get("pipeline") if isinstance(data, dict) else None
    outputs = (pipeline or {}).get("outputs") if isinstance(pipeline, dict) else None
    return outputs if isinstance(outputs, dict) else {}


def _pipeline_inputs(region: dict[str, Any]) -> dict[str, Any]:
    data = region.get("data") if isinstance(region, dict) else None
    inputs = (data or {}).get("inputs") if isinstance(data, dict) else None
    return inputs if isinstance(inputs, dict) else {}


def _expected_fields(output: dict[str, Any]) -> list[dict[str, Any]]:
    """输出自己声明的字段清单（``expectedFields``）。"""
    declared = output.get("expectedFields") if isinstance(output, dict) else None
    fields: list[dict[str, Any]] = []
    if not isinstance(declared, dict):
        return fields
    for name, spec in declared.items():
        spec = spec if isinstance(spec, dict) else {}
        fields.append(
            {
                "name": str(name),
                "dataType": str(spec.get("dataType") or ""),
                "unit": str(spec.get("unit") or ""),
            }
        )
    return fields


def _payload_fields(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """**当前载荷里真实出现**的字段（模拟 / JSON 两条链的产物形状一致）。

    这是「字段真的存在」的**运行期**证据：声明说有的、载荷里没有的，
    取值时按「取不到值」处理，不假装有。
    """
    if not isinstance(payload, dict):
        return []
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(name: Any) -> None:
        text = str(name or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        fields.append({"name": text, "dataType": "", "unit": ""})

    columns = payload.get("columns")
    if isinstance(columns, list):
        for column in columns:
            if isinstance(column, dict):
                _add(column.get("fieldRef"))
                fmt = column.get("format")
                if isinstance(fmt, dict) and fmt.get("unitLabel"):
                    for field in fields:
                        if field["name"] == str(column.get("fieldRef") or ""):
                            field["unit"] = str(fmt.get("unitLabel") or "")
    rows = payload.get("rows")
    if isinstance(rows, list):
        for row in rows[:50]:
            if isinstance(row, dict):
                for key in row:
                    _add(key)
    metrics = payload.get("metrics")
    if isinstance(metrics, list):
        for item in metrics:
            if isinstance(item, dict):
                _add(item.get("fieldRef"))
                _add(item.get("metricKey"))
    for key in ("value", "xField", "yField", "unit", "label"):
        if payload.get(key) is not None and key in ("value",):
            _add(key)
    return fields


def list_available_outputs(
    region: dict[str, Any], payload: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """这个区域**当前可绑定**的输出清单。

    每项形如 ``{ref, kind, name, fields}``。``kind`` 是
    ``output`` / ``input`` / ``meta``。
    """
    outputs: list[dict[str, Any]] = []
    for name, spec in _pipeline_outputs(region).items():
        outputs.append(
            {
                "ref": f"{OUTPUT_PREFIX}{name}",
                "kind": "output",
                "name": str(name),
                "fields": _expected_fields(spec if isinstance(spec, dict) else {}),
            }
        )
    for alias in _pipeline_inputs(region).keys():
        outputs.append(
            {
                "ref": f"{INPUT_PREFIX}{alias}",
                "kind": "input",
                "name": str(alias),
                # 输入没有登记字段清单 —— **不猜**它的字段。
                "fields": [],
            }
        )
    outputs.append(
        {
            "ref": META_QUALITY_REF,
            "kind": "meta",
            "name": "quality",
            "fields": [dict(item) for item in META_QUALITY_FIELDS],
        }
    )
    if payload is not None:
        # 当前载荷里真实存在的字段也并进**区域默认输出**的字段清单 ——
        # 模拟模式下这是「字段真的存在」的唯一运行期证据。
        payload_fields = _payload_fields(payload)
        if payload_fields:
            default_ref = str((region.get("binding") or {}).get("outputRef") or "")
            for item in outputs:
                if item["ref"] == default_ref or (
                    default_ref == "" and item["kind"] == "output"
                ):
                    known = {f["name"] for f in item["fields"]}
                    for field in payload_fields:
                        if field["name"] not in known:
                            item["fields"].append(dict(field))
                    break
    return outputs


def resolve_output_ref(
    region: dict[str, Any], output_ref: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """把 ``outputRef`` 解析成真实存在的输出；认不出来就**明确报错**。

    协议只允许三种写法；这里逐字认，**不按标题猜、不按名字近似匹配**。
    """
    text = str(output_ref or "").strip()
    if not text:
        raise DisplayBindingError(
            "没有给出输出引用（outputRef）。",
            reason="missing_output_ref",
            constraint="输出引用不能为空",
        )
    for item in list_available_outputs(region, payload):
        if item["ref"] == text:
            return dict(item)
    if text.startswith(OUTPUT_PREFIX):
        reason = "unknown_output_ref"
        hint = "这个区域没有登记该计算输出。"
    elif text.startswith(INPUT_PREFIX):
        reason = "unknown_input_ref"
        hint = "这个区域没有登记该输入。"
    elif text == META_QUALITY_REF:
        reason = "unknown_output_ref"
        hint = "这个区域没有可用的数据质量输出。"
    else:
        reason = "invalid_output_ref"
        hint = "输出引用只支持 output:<name>、input:<alias>、meta:quality 三种写法。"
    available = "、".join(
        item["ref"] for item in list_available_outputs(region, payload)
    )
    raise DisplayBindingError(
        f"输出引用 {text!r} 不存在：{hint}当前可用：{available or '（无）'}。",
        reason=reason,
        constraint="输出引用必须指向当前区域已登记的输出",
        detail={"outputRef": text, "available": available},
    )


def list_available_fields(
    region: dict[str, Any],
    output_ref: str = "",
    payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """某个输出下的字段清单（没有指定输出时用区域默认输出）。"""
    ref = str(output_ref or "").strip() or str(
        (region.get("binding") or {}).get("outputRef") or ""
    )
    if not ref:
        # 没有默认输出 → 只能用当前载荷里的真实字段。
        return _payload_fields(payload)
    resolved = resolve_output_ref(region, ref, payload)
    return list(resolved.get("fields") or [])


def field_exists(
    region: dict[str, Any],
    field_ref: str,
    *,
    output_ref: str = "",
    payload: dict[str, Any] | None = None,
) -> bool:
    """这个字段在目标输出里**真的存在**吗。"""
    name = str(field_ref or "").strip()
    if not name:
        return False
    fields = list_available_fields(region, output_ref, payload)
    if any(item["name"] == name for item in fields):
        return True
    # 载荷里的真实字段也算存在（模拟模式的运行期证据）。
    if payload is not None:
        return any(item["name"] == name for item in _payload_fields(payload))
    return False


def require_field(
    region: dict[str, Any],
    field_ref: str,
    *,
    output_ref: str = "",
    payload: dict[str, Any] | None = None,
    what: str = "字段",
) -> str:
    """字段必须存在，否则**明确报错**（不猜第一列、不按标题近似匹配）。"""
    name = str(field_ref or "").strip()
    if not name:
        raise DisplayBindingError(
            f"没有给出{what}名（fieldRef）。",
            reason="missing_field_ref",
            constraint=f"{what}名不能为空",
        )
    if field_exists(region, name, output_ref=output_ref, payload=payload):
        return name
    fields = list_available_fields(region, output_ref, payload)
    known = "、".join(item["name"] for item in fields) or "（无）"
    raise DisplayBindingError(
        f"{what} {name!r} 在当前输出里不存在；可用字段：{known}。",
        reason="unknown_field_ref",
        constraint=f"{what}必须来自当前已登记的输出",
        detail={"fieldRef": name, "available": known},
    )


# --------------------------------------------------------------------------
# 取值：单值解析（多行歧义 → 报错，不擅自求和）
# --------------------------------------------------------------------------

def _distinct(values: list[Any]) -> list[Any]:
    """去重（保持顺序），数值与字符串按显示值比较。"""
    out: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = f"{type(value).__name__}:{value}"
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def collect_field_values(
    payload: dict[str, Any] | None, field_ref: str
) -> list[Any]:
    """当前载荷里某个字段的**全部**取值（按固定顺序找，不猜字段）。"""
    name = str(field_ref or "").strip()
    if not name or not isinstance(payload, dict):
        return []
    values: list[Any] = []
    metrics = payload.get("metrics")
    if isinstance(metrics, list):
        for item in metrics:
            if not isinstance(item, dict):
                continue
            if str(item.get("fieldRef") or "") == name or str(
                item.get("metricKey") or ""
            ) == name:
                values.append(item.get("value"))
    rows = payload.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and name in row:
                values.append(row.get(name))
    points = payload.get("points")
    if isinstance(points, list):
        for point in points:
            if isinstance(point, dict) and name in point:
                values.append(point.get(name))
    series = payload.get("series")
    if isinstance(series, list):
        for item in series:
            if not isinstance(item, dict):
                continue
            if str(item.get("yField") or "") == name:
                values.extend(item.get("yValues") or [])
    if name in payload and not isinstance(payload.get(name), (dict, list)):
        values.append(payload.get(name))
    return values


def resolve_single_value(
    payload: dict[str, Any] | None,
    field_ref: str,
    *,
    what: str = "字段",
) -> Any:
    """取一个**单值**；多行且值不唯一 → 报错要求澄清。

    * 只有一个取值 → 返回它；
    * 多行但**值都相同** → 返回它（口径上仍是同一个值）；
    * 多行且**值不一样** → 报错（``ambiguous_field_value``）——
      **不擅自取第一行、不求和、不编一个数**。
    """
    name = str(field_ref or "").strip()
    values = collect_field_values(payload, name)
    if not values:
        raise DisplayBindingError(
            f"{what} {name!r} 在当前结果里没有取到值。",
            reason="missing_field_value",
            constraint=f"{what}必须能取到值",
            detail={"fieldRef": name},
        )
    distinct = _distinct(values)
    if len(distinct) > 1:
        raise DisplayBindingError(
            f"{what} {name!r} 在当前结果里有 {len(distinct)} 个不同的值"
            f"（共 {len(values)} 条记录），无法确定取哪一个；"
            "请先澄清口径，或改用表格 / 图表组件按记录展示。",
            reason="ambiguous_field_value",
            constraint="多行取值不唯一时必须先澄清",
            detail={"fieldRef": name, "rowCount": len(values)},
        )
    return distinct[0]


# --------------------------------------------------------------------------
# 展示裁剪：别名 / 过滤 / 排序 / 前 N 项
#
# 顺序固定：来源结果 → 别名 → 过滤 → 排序 → 前 N 项。
# **只作用展示记录**：源输出与全量总计不动。
# --------------------------------------------------------------------------

def apply_field_aliases(
    rows: list[dict[str, Any]], aliases: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """展示字段名 → 实际结果字段名（协议 ``DataBinding.fieldAliases``）。

    * 映射是「展示名 → 实际字段名」：``{"设备名称": "deviceName"}``；
    * 别名**只是展示层**，源记录的字段一个都不改；
    * 实际字段不存在 → **该别名不生效**（不伪造一个空列）。
    """
    if not isinstance(aliases, dict) or not aliases:
        return list(rows or [])
    mapping = {
        str(display): str(actual)
        for display, actual in aliases.items()
        if str(display or "").strip() and str(actual or "").strip()
    }
    if not mapping:
        return list(rows or [])
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            out.append(row)
            continue
        new_row = dict(row)
        for display, actual in mapping.items():
            if actual in row:
                new_row[display] = row.get(actual)
        out.append(new_row)
    return out


def _literal_value(expr: Any) -> Any:
    """表达式 → 字面值（只认 LITERAL；别的形态不猜）。"""
    if isinstance(expr, dict):
        if str(expr.get("type") or "").upper() == "LITERAL":
            return expr.get("value")
        return None
    return expr


def _field_ref_of(expr: Any) -> str:
    if isinstance(expr, dict):
        if str(expr.get("type") or "").upper() == "FIELD":
            return str(expr.get("fieldRef") or "")
        return ""
    return ""


def _compare(left: Any, operator: str, operands: list[Any]) -> bool:
    op = str(operator or "").upper()
    values = operands
    if op == "IS_NULL":
        return left is None or left == ""
    if op == "IS_NOT_NULL":
        return not (left is None or left == "")
    if op == "IN":
        return str(left) in {str(v) for v in values}
    if op == "NOT_IN":
        return str(left) not in {str(v) for v in values}
    if op == "CONTAINS":
        return any(str(v) in str(left or "") for v in values)
    if op == "STARTS_WITH":
        return str(left or "").startswith(str(values[0]) if values else "")
    if op == "BETWEEN":
        if len(values) < 2:
            return True
        try:
            number = float(left)
            return float(values[0]) <= number <= float(values[1])
        except (TypeError, ValueError):
            return True
    try:
        left_number = float(left)
        right_number = float(values[0]) if values else 0.0
    except (TypeError, ValueError):
        left_number = right_number = None
    if op == "EQ":
        return str(left) == str(values[0]) if values else False
    if op == "NE":
        return str(left) != str(values[0]) if values else False
    if left_number is None or right_number is None:
        return True
    if op == "GT":
        return left_number > right_number
    if op == "GE":
        return left_number >= right_number
    if op == "LT":
        return left_number < right_number
    if op == "LE":
        return left_number <= right_number
    return True


def _matches_expression(row: dict[str, Any], expr: Any) -> bool:
    """一个条件表达式是否命中一行（协议 Condition / ConditionGroup 形状）。"""
    if not isinstance(expr, dict):
        return True
    kind = str(expr.get("type") or "").upper()
    if kind == "GROUP":
        logic = str(expr.get("logic") or "AND").upper()
        conditions = [
            item for item in (expr.get("conditions") or []) if isinstance(item, dict)
        ]
        if logic == "OR":
            return any(_matches_expression(row, item) for item in conditions)
        if logic == "NOT":
            return not any(_matches_expression(row, item) for item in conditions)
        return all(_matches_expression(row, item) for item in conditions)
    field_ref = _field_ref_of(expr.get("left"))
    operator = str(expr.get("operator") or "EQ")
    operands = [
        _literal_value(item) for item in (expr.get("operands") or [])
    ]
    if not field_ref:
        # 左值不是字段 → 不是展示过滤能判的条件，放行（不静默丢行）。
        return True
    return _compare(row.get(field_ref), operator, operands)


def apply_presentation_filters(
    rows: list[dict[str, Any]], filters: list[Any] | None
) -> list[dict[str, Any]]:
    """展示过滤：**只过滤展示记录**，源输出与全量总计不动。"""
    if not filters:
        return list(rows or [])
    return [
        row
        for row in (rows or [])
        if isinstance(row, dict)
        and all(_matches_expression(row, expr) for expr in filters)
    ]


def apply_sort(
    rows: list[dict[str, Any]], sort: list[Any] | None
) -> list[dict[str, Any]]:
    """展示排序（协议 ``SortField``：``fieldRef`` / ``direction`` / ``nulls``）。"""
    if not sort:
        return list(rows or [])
    out = list(rows or [])
    for spec in reversed(list(sort)):
        if not isinstance(spec, dict):
            continue
        field_ref = str(spec.get("fieldRef") or "")
        if not field_ref:
            continue
        descending = str(spec.get("direction") or "ASC").upper() == "DESC"
        nulls_first = str(spec.get("nulls") or "").upper() == "FIRST"

        def _key(row: dict[str, Any], _field: str = field_ref) -> tuple[int, Any]:
            value = row.get(_field) if isinstance(row, dict) else None
            if value is None or value == "":
                return (0 if nulls_first else 1, 0)
            try:
                return (2, float(value))
            except (TypeError, ValueError):
                return (2, str(value))

        out.sort(key=_key, reverse=descending)
    return out


def apply_topn(
    rows: list[dict[str, Any]], top_n: Any
) -> list[dict[str, Any]]:
    """展示前 N 项（**只是展示裁剪**，不改取数、不改全量总计）。"""
    if top_n is None:
        return list(rows or [])
    try:
        count = int(top_n)
    except (TypeError, ValueError):
        return list(rows or [])
    if count < 0:
        return list(rows or [])
    return list(rows or [])[:count]


def apply_presentation_pipeline(
    rows: list[dict[str, Any]],
    *,
    field_aliases: dict[str, Any] | None = None,
    presentation_filters: list[Any] | None = None,
    sort: list[Any] | None = None,
    top_n: Any = None,
) -> list[dict[str, Any]]:
    """展示处理的**固定顺序**：别名 → 过滤 → 排序 → 前 N 项。

    字段投影**不在这里做** —— 过滤 / 排序 / 分组需要的字段必须留着，
    不能提前把「之后还要用的列」丢掉。
    """
    out = apply_field_aliases(rows, field_aliases)
    out = apply_presentation_filters(out, presentation_filters)
    out = apply_sort(out, sort)
    out = apply_topn(out, top_n)
    return out


#: 协议 ``Condition.operator`` 的取值域。
CONDITION_OPERATORS = (
    "EQ",
    "NE",
    "GT",
    "GE",
    "LT",
    "LE",
    "IN",
    "NOT_IN",
    "BETWEEN",
    "CONTAINS",
    "STARTS_WITH",
    "IS_NULL",
    "IS_NOT_NULL",
)


def validate_condition(
    expr: Any, region: dict[str, Any], *, path: str = "presentationFilters"
) -> None:
    """校验一个展示过滤条件（协议 ``Condition`` / ``ConditionGroup``）。

    * 左值必须是 ``FIELD``，且 ``fieldRef`` 必须**已声明**；
    * 运算符必须在协议取值域内；
    * 条件组的逻辑必须是 ``AND`` / ``OR`` / ``NOT``；
    * 字面量操作数只认 ``LITERAL``（不猜别的形态）。

    不合法就抛 :class:`DisplayBindingError`（能力层转成 ``CapabilityError``）。
    """
    if not isinstance(expr, dict):
        raise DisplayBindingError(
            f"{path} 里的条件必须是对象。",
            reason="invalid_condition",
            constraint="条件必须是对象",
        )
    kind = str(expr.get("type") or "CONDITION").upper()
    if kind == "GROUP":
        logic = str(expr.get("logic") or "").upper()
        if logic not in ("AND", "OR", "NOT"):
            raise DisplayBindingError(
                f"{path} 里的条件组逻辑 {logic!r} 不合法；只能是 AND / OR / NOT。",
                reason="invalid_condition",
                constraint="条件组逻辑必须在协议取值域内",
            )
        conditions = expr.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            raise DisplayBindingError(
                f"{path} 里的条件组没有列出条件。",
                reason="invalid_condition",
                constraint="条件组必须列出条件",
            )
        for index, item in enumerate(conditions):
            validate_condition(item, region, path=f"{path}[{index}]")
        return
    operator = str(expr.get("operator") or "").upper()
    if operator not in CONDITION_OPERATORS:
        raise DisplayBindingError(
            f"{path} 里的运算符 {operator!r} 不合法；可用："
            f"{'、'.join(CONDITION_OPERATORS)}。",
            reason="invalid_condition_operator",
            constraint="运算符必须在协议取值域内",
        )
    left = expr.get("left")
    if not isinstance(left, dict) or str(left.get("type") or "").upper() != "FIELD":
        raise DisplayBindingError(
            f"{path} 里的条件左值必须是字段表达式（FIELD）。",
            reason="invalid_condition_operand",
            constraint="条件左值必须是字段表达式",
        )
    field_ref = str(left.get("fieldRef") or "").strip()
    if not field_ref:
        raise DisplayBindingError(
            f"{path} 里的条件没有给出字段名。",
            reason="missing_field_ref",
            constraint="条件字段名不能为空",
        )
    try:
        require_declared_field(region, field_ref, what="过滤字段")
    except DisplayBindingError as exc:
        raise DisplayBindingError(
            f"{path} 里的过滤字段不可用：{exc.message}",
            reason=exc.reason,
            constraint=exc.constraint,
            detail=exc.detail,
        ) from exc
    for index, operand in enumerate(expr.get("operands") or []):
        if isinstance(operand, dict):
            kind = str(operand.get("type") or "LITERAL").upper()
            if kind != "LITERAL":
                raise DisplayBindingError(
                    f"{path}[{index}] 的操作数只支持字面量（LITERAL）。",
                    reason="unsupported_condition_operand",
                    constraint="操作数必须是字面量",
                )

# --------------------------------------------------------------------------
# 汇总引用（协议 TableSummary）
# --------------------------------------------------------------------------

def resolve_summary_output(
    region: dict[str, Any],
    summary: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """核对一个汇总引用是否可用；**不新增计算、不临时求和**。

    返回 ``{ok, reason, message, outputRef, fieldRef, basis}``。
    """
    output_ref = str(summary.get("outputRef") or "")
    field_ref = str(summary.get("fieldRef") or "")
    basis = str(summary.get("basis") or "").upper()
    if not output_ref:
        return {
            "ok": False,
            "reason": "missing_output_ref",
            "message": "汇总没有给出 outputRef。",
        }
    if not field_ref:
        return {
            "ok": False,
            "reason": "missing_field_ref",
            "message": "汇总没有给出 fieldRef。",
        }
    if basis not in ("FULL_RESULT", "VISIBLE_ROWS"):
        return {
            "ok": False,
            "reason": "invalid_summary_basis",
            "message": (
                f"汇总口径 {basis!r} 不合法；只能是 FULL_RESULT 或 VISIBLE_ROWS。"
            ),
        }
    try:
        resolve_output_ref(region, output_ref, payload)
    except DisplayBindingError as exc:
        return {
            "ok": False,
            "reason": exc.reason,
            "message": exc.message,
        }
    if not field_exists(region, field_ref, output_ref=output_ref, payload=payload):
        return {
            "ok": False,
            "reason": "unknown_field_ref",
            "message": f"汇总字段 {field_ref!r} 在输出 {output_ref!r} 里不存在。",
        }
    return {
        "ok": True,
        "reason": "",
        "message": "",
        "outputRef": output_ref,
        "fieldRef": field_ref,
        "basis": basis,
    }


# --------------------------------------------------------------------------
# 供能力层复用的参数模型（真实 Pydantic 模型 → 能力目录自动生成契约）
# --------------------------------------------------------------------------

class FieldAliasesParam(BaseModel):
    """展示字段别名（展示名 → 实际字段名）。"""

    aliases: dict[str, str] = Field(default_factory=dict)

    @field_validator("aliases", mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {
            str(display).strip(): str(actual).strip()
            for display, actual in value.items()
            if str(display or "").strip() and str(actual or "").strip()
        }


class SortFieldParam(BaseModel):
    """展示排序字段。"""

    fieldRef: str = Field(min_length=1)
    direction: str = Field(default="ASC")
    nulls: str = ""

    @field_validator("fieldRef", mode="before")
    @classmethod
    def _field_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("direction", mode="before")
    @classmethod
    def _direction(cls, value: Any) -> str:
        return str(value or "ASC").strip().upper()

    @field_validator("nulls", mode="before")
    @classmethod
    def _nulls(cls, value: Any) -> str:
        return str(value or "").strip().upper()
