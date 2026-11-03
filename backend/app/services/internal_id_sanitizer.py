"""用户可见文字里的**内部标识**清洗。

模型写的摘要里常带着它自己看得懂的内部标识（``metric.machine_room.pue``、
``region_1``、``capabilityPending``），用户看不懂也不该看。这个模块把
这些形状**通用地**换掉：

- 认得出的（目录里登记过的指标 / 数据源）→ 换成业务名（「总耗电」）；
- 认不出的 → 直接去掉，并清掉留下的空括号、空引号、多余标点。

判据是**形状**，不认具体名字 —— 这里没有任何业务词表：

- ``前缀 + 分隔符 + 标识``：``metric.x.y`` / ``ds_xxx`` / ``region_1``；
- **camelCase 字段名**：``[a-z]{3,}[A-Z][a-z]{1,}``（``capabilityPending``、
  ``metricRef``…）。单位 ``kWh``、``iPhone`` 这类不在这个形状里，保留；
- **全大写下划线枚举值**：``SINGLE_CHOICE``、``PLACEHOLDER_ONLY`` 这类。

规则路径（``agent_service``）与模型路径（``agentscope_report_agent``）
都走这里，避免两边口径不一致。
"""

from __future__ import annotations

import re
from typing import Any

from app.services.catalog_mock_service import get_catalog
from app.services.metric_mock_service import list_metrics


#: ``前缀 + 分隔符 + 标识`` —— 指标 / 数据源 / 卡片 / 字段的内部 id。
_INTERNAL_ID_PATTERN = re.compile(
    r"(?<![\w.])(?:metric|metricDef|ds|dataset|field|region|def)[._][A-Za-z0-9_.]+"
)

#: camelCase 字段名（``capabilityPending`` / ``metricRef`` / ``planId``…）。
#:
#: 形状要求「≥3 个小写字母 + ≥1 个大写字母 + ≥1 个小写字母」——
#: ``kWh``（1+1+1）、``iPhone``（1+1+4）都不满足，保留原样。
#:
#: 后面跟着 ``=值`` / ``: 值`` 时**连值一起去掉**（``bindingKind=simulated``），
#: 免得留下一个悬空的等号。
_CAMEL_FIELD_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])[a-z]{3,}[A-Z][a-z]{1,}(?:\s*[:=]\s*[A-Za-z0-9_.]+)?(?![A-Za-z0-9_])"
)

#: 全大写下划线的枚举值（``SINGLE_CHOICE`` / ``PLACEHOLDER_ONLY``）。
_SCREAMING_ENUM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])[A-Z]{2,}(?:_[A-Z]{2,})+(?![A-Za-z0-9_])"
)

#: ``字段名: 枚举值`` 的整段（``expects: SINGLE_CHOICE``）—— 整段去掉，
#: 免得留下一个悬空的冒号。
_LABELLED_ENUM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z]{3,}\s*[:=]\s*[A-Z]{2,}(?:_[A-Z]{2,})+(?![A-Za-z0-9_])"
)


def metric_display_label(metric_ref: str | None) -> str:
    """指标的业务说法（**不点内部标识**）。

    目录里登记过的指标带一句业务名（「总耗电」），直接取；
    目录外 / 认不出的返回空串（调用方据此把它从文字里去掉）。
    """
    ref = str(metric_ref or "")
    if not ref:
        return ""
    for item in list_metrics():
        if str(item.get("metricRef") or "") == ref:
            return str(item.get("label") or "")
    return ""


def source_display_label(source_ref: str | None) -> str:
    """数据源的业务说法（**不点内部标识**）。

    目录里登记的数据源都带一句业务描述（「EAS 能耗明细：按设备、分钟级
    采样的区间电量」），取描述的**前半句**当名字；查不到就返回空串。
    """
    ref = str(source_ref or "")
    if not ref:
        return ""
    catalog = get_catalog(ref) or {}
    return str(catalog.get("description") or "").split("：")[0].strip()


def _replace_prefixed_id(match: re.Match[str]) -> str:
    token = match.group(0)
    prefix = token.split(".", 1)[0].split("_", 1)[0]
    if prefix in {"metric", "metricDef"}:
        # 先按**完整**标识找（``metric.machine_room.pue``），找不到再
        # 退回去掉前缀的那截。
        return metric_display_label(token) or metric_display_label(
            token.split(".", 1)[1] if "." in token else token
        )
    if prefix in {"ds", "dataset", "field"}:
        ref = token.split(".", 1)[1] if "." in token else token
        return source_display_label(ref)
    # 卡片 id（region_x）没有单独的业务名表 —— 句子里通常已经写了
    # 卡片名，这里直接去掉这个标识。
    return ""


def sanitize_internal_ids(text: str) -> str:
    """把用户可见文字里的**内部标识**换成业务说法。

    认得出来的换成业务名（「总耗电」），认不出来的直接去掉，
    并清掉留下的空括号、空引号、多余标点。
    """
    if not text:
        return ""
    value = str(text)

    value = _INTERNAL_ID_PATTERN.sub(_replace_prefixed_id, value)
    # 内部枚举 / 字段名：一律去掉（连着它们的取值一起去）。
    value = _LABELLED_ENUM_PATTERN.sub("", value)
    value = _CAMEL_FIELD_PATTERN.sub("", value)
    value = _SCREAMING_ENUM_PATTERN.sub("", value)

    # 去掉替换后留下的空括号 / 空引号 / 多余空格 / 悬空标点。
    value = re.sub(r"[（(]\s*[）)]", "", value)
    value = re.sub(r"[「『]\s*[」』]", "", value)
    # 只剩一个等号 / 冒号的括号：标识先被替换、字段名后被去掉时留下的残渣
    # （「（metricKey=metric.power.section_1_2）」→「（=）」）。整组去掉。
    value = re.sub(r"[（(]\s*[=＝:：]\s*[）)]", "", value)
    value = re.sub(r"[（(]\s*[，,]\s*", "（", value)
    value = re.sub(r"\s*[，,]\s*[）)]", "）", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"\s+([，。；：、）])", r"\1", value)
    return value.strip()


def sanitize_change_set_summaries(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把一组操作里的 ``summary`` 也洗一遍（返回新列表，不改原对象）。"""
    out: list[dict[str, Any]] = []
    for item in operations or []:
        if not isinstance(item, dict):
            out.append(item)
            continue
        copied = dict(item)
        if isinstance(copied.get("summary"), str):
            copied["summary"] = sanitize_internal_ids(copied["summary"])
        out.append(copied)
    return out
