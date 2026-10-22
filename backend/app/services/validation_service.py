"""报表定义校验服务（节点 7 VALIDATING）。

真实版会由 Java 后端做 5 层校验：
    - 结构（schema/字段形状）
    - 字段（dataset 引用是否存在、字段类型是否对）
    - 指标（metricRef/metricVersion 在注册表中是否存在）
    - 权限（subjectRef 是否在 IAM 授权范围）
    - 能力（dataSource 是否支持 grain / timeWindow）

本模块只做形状校验，产出符合 ValidationResult 协议的结构，供节点 8
VALIDATION_FAILED 翻译成人话，或节点 9 PREVIEWING 继续预览。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.schemas.agent import ValidationIssue, ValidationResult


logger = logging.getLogger(__name__)


# 本地支持的组件类型 / 图表类型（和前端渲染保持一致）
SUPPORTED_COMPONENT_TYPES = {
    "TEXT",
    "METRIC_CARD",
    "TABLE",
    "CHART",
    "DATA_QUALITY",
    "ANALYSIS",
}
SUPPORTED_CHART_KINDS = {"LINE", "AREA", "BAR", "PIE"}


def fixed_interval_error(window: dict[str, Any]) -> str | None:
    """校验本地具体钟点时间窗；其他窗口类型不在此处处理。"""
    value = window.get("value", window)
    if not isinstance(value, dict) or value.get("type") != "FIXED_INTERVAL":
        return None
    try:
        timezone = ZoneInfo(value["timezone"])
        start = datetime.fromisoformat(value["start"])
        end = datetime.fromisoformat(value["end"])
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or start.utcoffset() != start.astimezone(timezone).utcoffset()
            or end.utcoffset() != end.astimezone(timezone).utcoffset()
            or start >= end
        ):
            return "具体时间窗必须有匹配时区的起止时间，且 start 早于 end。"
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        return "具体时间窗缺少合法的 timezone、start 或 end。"
    return None


def _issue(
    layer: str,
    code: str,
    message: str,
    *,
    path: str | None = None,
    suggestion: str | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        layer=layer, code=code, message=message, path=path, suggestion=suggestion
    )


def validate_definition(definition_draft: dict[str, Any] | None) -> ValidationResult:
    """对保存后的 ReportDefinitionDraft 做形状校验。"""
    if definition_draft is None:
        return ValidationResult(
            passed=False,
            issues=[
                _issue(
                    "STRUCTURE",
                    "DEFINITION_MISSING",
                    "找不到报表定义草稿，请先重新生成。",
                )
            ],
        )

    issues: list[ValidationIssue] = []

    # ---- 结构层 ----
    regions = definition_draft.get("regions") or []
    if not regions:
        issues.append(
            _issue(
                "STRUCTURE",
                "REGIONS_EMPTY",
                "报表没有任何区域，无法预览。",
                path="/regions",
                suggestion="至少保留一个总耗电卡片或趋势图。",
            )
        )

    datasets = definition_draft.get("datasets") or {}
    if not datasets:
        issues.append(
            _issue(
                "STRUCTURE",
                "DATASETS_EMPTY",
                "报表没有定义数据集。",
                path="/datasets",
                suggestion="至少定义一个数据集。",
            )
        )

    seen_ids: set[str] = set()
    for idx, region in enumerate(regions):
        rid = region.get("id") or ""
        window = (region.get("context") or {}).get("timeWindow")
        if isinstance(window, dict):
            window_error = fixed_interval_error(window)
            if window_error:
                issues.append(_issue(
                    "STRUCTURE", "FIXED_INTERVAL_INVALID", window_error,
                    path=f"/regions/{idx}/context/timeWindow",
                ))
        if not rid:
            issues.append(
                _issue(
                    "STRUCTURE",
                    "REGION_ID_MISSING",
                    f"第 {idx + 1} 个区域没有 id。",
                    path=f"/regions/{idx}/id",
                )
            )
        elif rid in seen_ids:
            issues.append(
                _issue(
                    "STRUCTURE",
                    "REGION_ID_DUPLICATED",
                    f"区域 id 重复：{rid}",
                    path=f"/regions/{idx}/id",
                )
            )
        else:
            seen_ids.add(rid)

        role = region.get("role")
        if not role:
            issues.append(
                _issue(
                    "STRUCTURE",
                    "REGION_ROLE_MISSING",
                    f"区域 {rid} 缺少 role 字段。",
                    path=f"/regions/{idx}/role",
                )
            )

        component = region.get("component") or {}
        ctype = component.get("type")
        if not ctype:
            issues.append(
                _issue(
                    "STRUCTURE",
                    "COMPONENT_TYPE_MISSING",
                    f"区域 {rid} 没有 component.type。",
                    path=f"/regions/{idx}/component/type",
                )
            )
        elif ctype not in SUPPORTED_COMPONENT_TYPES:
            issues.append(
                _issue(
                    "CAPABILITY",
                    "COMPONENT_TYPE_UNSUPPORTED",
                    f"区域 {rid} 使用了不支持的组件类型：{ctype}",
                    path=f"/regions/{idx}/component/type",
                    suggestion="改成 METRIC_CARD / TABLE / CHART / DATA_QUALITY 之一。",
                )
            )

        if ctype == "CHART":
            chart_kind = component.get("chartKind")
            if chart_kind not in SUPPORTED_CHART_KINDS:
                issues.append(
                    _issue(
                        "CAPABILITY",
                        "CHART_KIND_UNSUPPORTED",
                        f"区域 {rid} 使用了不支持的图表类型：{chart_kind}",
                        path=f"/regions/{idx}/component/chartKind",
                    suggestion="改成 LINE / AREA / BAR / PIE 之一。",
                    )
                )

        # ---- 字段层：有 data.inputs 的区域要指向已声明的 dataset ----
        inputs = (region.get("data") or {}).get("inputs") or {}
        for input_name, input_cfg in inputs.items():
            ds_ref = input_cfg.get("datasetRef")
            if ds_ref and ds_ref not in datasets:
                issues.append(
                    _issue(
                        "FIELD",
                        "DATASET_REF_UNKNOWN",
                        f"区域 {rid} 的 input {input_name} 引用了未声明的数据集 {ds_ref}",
                        path=f"/regions/{idx}/data/inputs/{input_name}/datasetRef",
                        suggestion=f"在 /datasets 中声明 {ds_ref}。",
                    )
                )

    # ---- 权限层：subjectRef 必须和 defaults.scope.subjectRef 一致 ----
    scope = (definition_draft.get("defaults") or {}).get("scope") or {}
    subject_ref = scope.get("subjectRef")
    if subject_ref and subject_ref not in {"machine_room", "device"}:
        issues.append(
            _issue(
                "PERMISSION",
                "SUBJECT_REF_OUT_OF_AUTH",
                f"当前账号没有 {subject_ref} 的授权。",
                path="/defaults/scope/subjectRef",
                suggestion="改成 machine_room。",
            )
        )

    passed = len(issues) == 0
    logger.info(
        "[validation_service] passed=%s issue_count=%d", passed, len(issues)
    )
    return ValidationResult(passed=passed, issues=issues)


def summarize_issues_for_user(result: ValidationResult) -> str:
    """把 issues 翻译成一段人话（节点 8 用）。"""
    if result.passed:
        return "校验通过"
    lines = []
    for i, issue in enumerate(result.issues, 1):
        line = f"{i}. [{issue.layer}] {issue.message}"
        if issue.suggestion:
            line += f"（建议：{issue.suggestion}）"
        lines.append(line)
    return "\n".join(lines)
