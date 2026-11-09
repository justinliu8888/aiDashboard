"""图表载荷装饰：把**定义里声明**的图表设置如实搬进载荷。

**为什么有这一层**：图表载荷原先由造数器按「指标口径」合成 ——
定义里声明的 ``encodings`` 只被读作横纵轴的**字段名**；声明的图型、
坐标轴、图例、提示字段与参考线**根本没进载荷**，前端只能按自己的默认画，
于是「堆叠柱状图」画成柱状图、「环形图」画成饼图。

这里做两件事：

1. **声明搬进载荷**（图型逐字、坐标轴、图例、系列配色、提示字段、参考线）；
2. **声明的编码真的驱动取值**（CH02）：声明了纵轴字段，就按那个字段出数；
   **取不到值时不编数** —— 保留原有口径，并在载荷里说清楚为什么。

**纯函数**：不取数、不落库、不调模型，只做一次载荷变换。
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.services.display_binding_service import simulated_field


#: 声明的纵轴字段当前没有可用取值时的说明（载荷里原样带给前端）。
ENCODING_Y_NOTE = "声明的纵轴字段当前没有可用取值，仍按原有口径显示"

#: 声明的横轴字段当前没有可用取值时的说明。
ENCODING_X_NOTE = "声明的横轴字段当前没有可用取值，仍按原有口径显示"

#: 声明的系列字段当前没有可用取值时的说明。
ENCODING_SERIES_NOTE = "声明的系列字段当前没有可用取值，仍按原有口径显示"


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _clean_encodings(raw: Any) -> list[dict[str, Any]]:
    """定义里声明的通道绑定（只留协议里有的三个键）。"""
    result: list[dict[str, Any]] = []
    for item in _as_list(raw):
        if not isinstance(item, dict):
            continue
        channel = str(item.get("channel") or "").strip().upper()
        field_ref = str(item.get("fieldRef") or "").strip()
        if not channel or not field_ref:
            continue
        encoding: dict[str, Any] = {"channel": channel, "fieldRef": field_ref}
        fmt = item.get("format")
        if isinstance(fmt, dict) and fmt:
            encoding["format"] = dict(fmt)
        result.append(encoding)
    return result


def _numeric(value: Any) -> float | None:
    """能当数值用的值（布尔不算）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _stable_ratio(seed: str) -> float:
    """稳定的 0–1 比例（同样的种子永远同一个数）。"""
    digest = hashlib.sha256(str(seed).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _derived_values(
    base_value: float, field_ref: str, seed: str, count: int
) -> list[float]:
    """由声明字段的取值派生出一条**逐点不同**的确定性序列。

    不是把同一个数重复 N 次（那样图上是条直线，等于没画）：
    用「字段取值 × 一个稳定的抖动系数」逐点派生，抖动只取决于
    **种子 + 字段 + 序号** —— 同一个草稿每次刷新都是同一条线。
    """
    values: list[float] = []
    for index in range(max(0, int(count))):
        ratio = _stable_ratio(f"{seed}|{field_ref}|{index}")
        factor = 0.7 + 0.6 * ratio
        values.append(round(base_value * factor, 4))
    return values


def _legacy_points(payload: dict[str, Any]) -> list[dict[str, Any]]:
    points = payload.get("points")
    return [item for item in _as_list(points) if isinstance(item, dict)]


def _x_values_of(payload: dict[str, Any]) -> list[Any]:
    """原有的横轴取值（优先取点里的横轴值，其次取标签）。"""
    x_field = str(payload.get("xField") or "")
    values: list[Any] = []
    for point in _legacy_points(payload):
        values.append(point.get(x_field))
    if not values:
        values = list(_as_list(payload.get("xLabels")))
    return values


def _rebuild_points(
    payload: dict[str, Any], x_field: str, y_field: str
) -> list[dict[str, Any]]:
    """按新的横纵字段重建点（点里的键跟着字段走）。"""
    x_values = _x_values_of(payload)
    y_values = _as_list(payload.get("yValues"))
    points: list[dict[str, Any]] = []
    for index, value in enumerate(y_values):
        point: dict[str, Any] = {}
        if index < len(x_values):
            point[x_field] = x_values[index]
        point[y_field] = value
        points.append(point)
    return points


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def decorate_chart_payload(
    region: dict[str, Any],
    payload: dict[str, Any],
    ctx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把定义里声明的图表设置装饰进载荷（就地改并返回同一个载荷）。

    ``region``  区块定义（读 ``component`` 里的声明）。
    ``payload`` 造数器给的图表载荷（会被就地补齐）。
    ``ctx``     ``{"fieldValues": {...}, "seriesKeys": [...]}``：
                 这个区块**当前可用的单值字段**与**稳定系列标识**。
    """
    region = _as_dict(region)
    payload = _as_dict(payload)
    ctx = _as_dict(ctx)
    component = _as_dict(region.get("component"))
    field_values = _as_dict(ctx.get("fieldValues"))
    series_keys = [str(item) for item in _as_list(ctx.get("seriesKeys"))]

    # ---- 图型：逐字保留（STACKED_BAR 不折成 BAR，DONUT 不折成 PIE） ----
    chart_kind = str(component.get("chartKind") or "").strip().upper()
    if chart_kind:
        payload["chartKind"] = chart_kind

    # ---- 声明搬进载荷 ----
    encodings = _clean_encodings(component.get("encodings"))
    payload["encodings"] = encodings

    axes = _as_dict(component.get("axes"))
    payload["axes"] = {
        str(name): dict(spec) for name, spec in axes.items() if isinstance(spec, dict)
    }

    legend = component.get("legend")
    payload["legend"] = bool(legend) if isinstance(legend, bool) else True

    series_colors = _as_dict(component.get("seriesColors"))
    payload["seriesColors"] = {
        str(name): str(value) for name, value in series_colors.items()
    }

    payload["tooltipFields"] = [
        str(item) for item in _as_list(component.get("tooltipFields")) if str(item).strip()
    ]

    reference_lines: dict[str, Any] = {}
    for name, expr in _as_dict(component.get("referenceLines")).items():
        if isinstance(expr, dict):
            reference_lines[str(name)] = dict(expr)
    payload["referenceLines"] = reference_lines

    # 稳定系列标识（前端按它取配色）：声明的 + 调用方给的。
    payload["seriesKeys"] = series_keys

    # 单值字段（提示框按 fieldRef 取值时读它）。
    payload["fieldValues"] = {
        str(name): value for name, value in field_values.items()
    }

    # ---- CH02：声明的编码真的驱动取值 ----
    by_channel = {item["channel"]: item for item in encodings}
    seed = str(ctx.get("seed") or ctx.get("seedScope") or region.get("id") or "chart")
    x_labels = _as_list(payload.get("xLabels"))
    legacy_y_field = str(payload.get("yField") or "")
    # X/Y 也可标识换图型前已有的分类和值；切为饼图不能因此换一套数。
    # SERIES 仅适用于有轴图型。
    no_axes = str(payload.get("chartKind") or "").strip().upper() in ("PIE", "DONUT")

    # 横轴：字段可用就按声明的字段名。
    x_encoding = by_channel.get("X")
    if x_encoding:
        x_ref = x_encoding["fieldRef"]
        if x_ref in field_values:
            payload["xField"] = x_ref
        else:
            payload["encodingXNote"] = ENCODING_X_NOTE

    # 纵轴：字段可用就按那个字段出数；取不到就**不编数**。
    y_encoding = by_channel.get("Y")
    if y_encoding:
        y_ref = y_encoding["fieldRef"]
        if y_ref not in field_values:
            payload["encodingDriven"] = False
            payload["encodingNote"] = ENCODING_Y_NOTE
        elif y_ref == legacy_y_field:
            # 声明的就是原来这一列：逐点变化保持原样。
            payload["yField"] = y_ref
            payload["encodingDriven"] = True
            payload["encodingSource"] = y_ref
        else:
            base = _numeric(field_values.get(y_ref))
            if base is None or not x_labels:
                payload["encodingDriven"] = False
                payload["encodingNote"] = ENCODING_Y_NOTE
            else:
                payload["yField"] = y_ref
                payload["yValues"] = _derived_values(base, y_ref, seed, len(x_labels))
                payload["points"] = _rebuild_points(
                    payload, str(payload.get("xField") or ""), y_ref
                )
                payload["encodingDriven"] = True
                payload["encodingSource"] = y_ref

    # 系列：字段可用就把单系列挂上稳定键（前端按稳定键取色）。
    series_encoding = by_channel.get("SERIES")
    if series_encoding and not no_axes:
        series_ref = series_encoding["fieldRef"]
        if series_ref in field_values:
            if not _as_list(payload.get("series")):
                catalog = simulated_field(series_ref) or {}
                metrics = _as_list(payload.get("metrics"))
                kind = ""
                if metrics and isinstance(metrics[0], dict):
                    kind = str(metrics[0].get("kind") or "")
                payload["series"] = [
                    {
                        "metricKey": series_ref,
                        "label": str(
                            catalog.get("label")
                            or series_encoding.get("label")
                            or series_ref
                        ),
                        "kind": kind or str(catalog.get("dataType") or "").lower(),
                        "unit": str(
                            catalog.get("unit") or payload.get("unit") or ""
                        ),
                        "yField": str(payload.get("yField") or ""),
                        "yValues": list(_as_list(payload.get("yValues"))),
                        "points": [dict(point) for point in _legacy_points(payload)],
                    }
                ]
        else:
            payload["encodingSeriesNote"] = ENCODING_SERIES_NOTE

    # 单位：数据单位不动；声明里给了单位标签就**另外**带出去。
    y_format = (by_channel.get("Y") or {}).get("format")
    if isinstance(y_format, dict):
        unit_label = str(y_format.get("unitLabel") or "").strip()
        if unit_label:
            payload["encodingUnitLabel"] = unit_label

    return payload
