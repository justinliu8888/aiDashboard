"""报表定义校验服务（节点 7 VALIDATING）。

真实版会由 Java 后端做 5 层校验：
    - 结构（schema/字段形状）
    - 字段（dataset 引用是否存在、字段类型是否对）
    - 指标（metricRef/metricVersion 在注册表中是否存在）
    - 权限（subjectRef 是否在 IAM 授权范围）
    - 能力（dataSource 是否支持 grain / timeWindow）

本模块做同样的 5 层校验（mock 阶段用本地注册表当真值），产出符合
ValidationResult 协议的结构，供节点 8 VALIDATION_FAILED 翻译成人话，
或节点 9 PREVIEWING 继续预览。

**关键约束**：校验不过就停在 VALIDATION_FAILED，绝不能继续预览。
预览假数据比不预览更糟 —— 用户会以为那就是真实结果。
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

# 派生列类型（与 report_draft_service 的常量保持一致）
SUPPORTED_DERIVED_KINDS = {
    "PREVIOUS_PERIOD",
    "GROWTH_RATE",
    "SHARE_OF_TOTAL",
}

# 条件样式的比较符
SUPPORTED_COMPARISONS = {"GT", "GTE", "LT", "LTE", "EQ", "NE"}
# 排序指令的比较符
SUPPORTED_SORT_COMPARISONS = {"ASC", "DESC"}


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


def _catalog(source_ref: str) -> dict[str, Any] | None:
    from app.services.catalog_mock_service import get_catalog

    return get_catalog(source_ref)


def _metric(metric_ref: str) -> dict[str, Any] | None:
    from app.services.metric_mock_service import get_metric

    return get_metric(metric_ref)


def _dataset_fields(dataset: dict[str, Any] | None) -> set[str]:
    if not dataset:
        return set()
    return set((dataset.get("fields") or {}).keys())


def _grain_of(node: dict[str, Any]) -> str | None:
    """从一个**容器**节点里取它声明的粒度（node["grain"]）。"""
    if not isinstance(node, dict):
        return None
    grain = node.get("grain")
    if isinstance(grain, dict):
        value = grain.get("value")
        if isinstance(value, dict) and value.get("unit"):
            return str(value["unit"]).upper()
        if grain.get("unit"):
            return str(grain["unit"]).upper()
    return None


def validate_definition(definition_draft: dict[str, Any] | None) -> ValidationResult:
    """对保存后的 ReportDefinitionDraft 做结构 / 字段 / 指标 / 权限 / 能力校验。"""
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

    # ---- 权限层：scope 必须在授权范围内 ----
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

    _validate_scope_selectors(scope, issues)

    # ---- 能力层：数据集声明的数据源必须存在且支持所用的粒度 ----
    dataset_sources: dict[str, dict[str, Any]] = {}
    for ds_name, dataset in datasets.items():
        source_ref = (dataset or {}).get("sourceRef")
        if not source_ref:
            issues.append(
                _issue(
                    "CAPABILITY",
                    "SOURCE_REF_MISSING",
                    f"数据集 {ds_name} 没有声明 sourceRef。",
                    path=f"/datasets/{ds_name}/sourceRef",
                )
            )
            continue
        catalog = _catalog(source_ref)
        if catalog is None:
            issues.append(
                _issue(
                    "CAPABILITY",
                    "SOURCE_REF_UNKNOWN",
                    f"数据集 {ds_name} 引用的数据源 {source_ref} 不在目录里。",
                    path=f"/datasets/{ds_name}/sourceRef",
                    suggestion="改用已登记的数据源，或重新生成草稿。",
                )
            )
            continue
        dataset_sources[ds_name] = catalog
        catalog_subject = catalog.get("subjectRef")
        if subject_ref and catalog_subject and catalog_subject != subject_ref:
            issues.append(
                _issue(
                    "PERMISSION",
                    "SOURCE_SUBJECT_MISMATCH",
                    f"数据源 {source_ref} 的业务对象是 {catalog_subject}，"
                    f"与报表范围 {subject_ref} 不一致。",
                    path=f"/datasets/{ds_name}/sourceRef",
                )
            )

    # defaults.grain 必须被数据源支持
    defaults_grain = _grain_of(definition_draft.get("defaults") or {})
    if defaults_grain:
        for ds_name, catalog in dataset_sources.items():
            supported = list(catalog.get("supportedGrains") or [])
            if supported and defaults_grain not in supported:
                issues.append(
                    _issue(
                        "CAPABILITY",
                        "GRAIN_NOT_SUPPORTED",
                        f"数据源 {catalog.get('sourceRef')} 不支持 {defaults_grain} 粒度，"
                        f"它支持的是 {'、'.join(supported)}。",
                        path="/defaults/grain",
                        suggestion=f"改成 {'、'.join(supported)} 之一。",
                    )
                )

    # defaults.source 必须和数据集声明的数据源一致：
    # 两处写不同的源 = 卡片口径和表格口径不是一回事。
    declared_source = (definition_draft.get("defaults") or {}).get("source") or {}
    declared_ref = declared_source.get("sourceRef")
    dataset_refs = {
        (dataset or {}).get("sourceRef")
        for dataset in datasets.values()
        if (dataset or {}).get("sourceRef")
    }
    if declared_ref and dataset_refs and declared_ref not in dataset_refs:
        issues.append(
            _issue(
                "FIELD",
                "SOURCE_REF_INCONSISTENT",
                f"defaults.source 写的是 {declared_ref}，"
                f"但数据集用的是 {'、'.join(sorted(dataset_refs))}。",
                path="/defaults/source/sourceRef",
                suggestion="让默认数据源和数据集保持一致。",
            )
        )

    seen_ids: set[str] = set()
    for idx, region in enumerate(regions):
        rid = region.get("id") or ""
        context = region.get("context") or {}
        window = context.get("timeWindow")
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
        data = region.get("data") or {}
        inputs = data.get("inputs") or {}
        region_datasets: list[dict[str, Any]] = []
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
            elif ds_ref:
                region_datasets.append(datasets.get(ds_ref) or {})

        # 区域能用的字段 = 数据集字段 ∪ pipeline 输出字段 ∪ 派生列字段
        available_fields: set[str] = set()
        for dataset in region_datasets:
            available_fields |= _dataset_fields(dataset)

        pipeline = data.get("pipeline") or {}
        nodes = pipeline.get("nodes") or []
        for node_idx, node in enumerate(nodes):
            node_type = node.get("type")
            # 指标必须已登记
            metric_ref = node.get("metricRef")
            if metric_ref:
                metric = _metric(metric_ref)
                if metric is None:
                    issues.append(
                        _issue(
                            "FIELD",
                            "METRIC_REF_UNKNOWN",
                            f"区域 {rid} 的节点 {node.get('id')} 引用了未登记的指标 {metric_ref}。",
                            path=f"/regions/{idx}/data/pipeline/nodes/{node_idx}/metricRef",
                            suggestion="改用目录里已登记的指标。",
                        )
                    )
                else:
                    version = node.get("metricVersion")
                    if version and version != metric.get("metricVersion"):
                        issues.append(
                            _issue(
                                "FIELD",
                                "METRIC_VERSION_MISMATCH",
                                f"区域 {rid} 的指标 {metric_ref} 版本是 {version}，"
                                f"注册表里是 {metric.get('metricVersion')}。",
                                path=f"/regions/{idx}/data/pipeline/nodes/{node_idx}/metricVersion",
                            )
                        )
            # 过滤字段必须存在
            for cond_idx, condition in enumerate(node.get("conditions") or []):
                field_ref = condition.get("fieldRef")
                if not field_ref:
                    continue
                if available_fields and field_ref not in available_fields:
                    issues.append(
                        _issue(
                            "FIELD",
                            "FILTER_FIELD_UNKNOWN",
                            f"区域 {rid} 按 {field_ref} 过滤，但数据源里没有这个字段。",
                            path=(
                                f"/regions/{idx}/data/pipeline/nodes/{node_idx}"
                                f"/conditions/{cond_idx}/fieldRef"
                            ),
                            suggestion=f"可用字段：{'、'.join(sorted(available_fields))}。",
                        )
                    )
            # 派生列的来源字段必须存在
            for col_idx, column in enumerate(node.get("columns") or []):
                kind = str(column.get("kind") or "").upper()
                if kind and kind not in SUPPORTED_DERIVED_KINDS:
                    issues.append(
                        _issue(
                            "CAPABILITY",
                            "DERIVED_KIND_UNSUPPORTED",
                            f"区域 {rid} 使用了不支持的派生列类型 {kind}。",
                            path=(
                                f"/regions/{idx}/data/pipeline/nodes/{node_idx}"
                                f"/columns/{col_idx}/kind"
                            ),
                            suggestion="改成 PREVIOUS_PERIOD / GROWTH_RATE / SHARE_OF_TOTAL 之一。",
                        )
                    )
                source_field = column.get("sourceFieldRef")
                if (
                    source_field
                    and available_fields
                    and source_field not in available_fields
                ):
                    issues.append(
                        _issue(
                            "FIELD",
                            "DERIVED_SOURCE_FIELD_UNKNOWN",
                            f"区域 {rid} 的派生列基于 {source_field}，但数据源里没有这个字段。",
                            path=(
                                f"/regions/{idx}/data/pipeline/nodes/{node_idx}"
                                f"/columns/{col_idx}/sourceFieldRef"
                            ),
                        )
                    )

        # pipeline 声明的输出字段
        outputs = pipeline.get("outputs") or {}
        for output in outputs.values():
            available_fields |= set((output.get("expectedFields") or {}).keys())

        # 派生列（binding.derivedColumns）本身也是可用字段
        binding = region.get("binding") or {}
        for column in binding.get("derivedColumns") or []:
            field_ref = column.get("fieldRef")
            if field_ref:
                available_fields.add(field_ref)
            kind = str(column.get("kind") or "").upper()
            if kind and kind not in SUPPORTED_DERIVED_KINDS:
                issues.append(
                    _issue(
                        "CAPABILITY",
                        "DERIVED_KIND_UNSUPPORTED",
                        f"区域 {rid} 使用了不支持的派生列类型 {kind}。",
                        path=f"/regions/{idx}/binding/derivedColumns",
                    )
                )

        # 区域粒度必须被数据源支持
        region_grain = _grain_of(context or {})
        if region_grain:
            for catalog in dataset_sources.values():
                supported = list(catalog.get("supportedGrains") or [])
                if supported and region_grain not in supported:
                    issues.append(
                        _issue(
                            "CAPABILITY",
                            "GRAIN_NOT_SUPPORTED",
                            f"区域 {rid} 用了 {region_grain} 粒度，"
                            f"数据源 {catalog.get('sourceRef')} 只支持 {'、'.join(supported)}。",
                            path=f"/regions/{idx}/context/grain",
                            suggestion=f"改成 {'、'.join(supported)} 之一。",
                        )
                    )

        # 组件列引用的字段必须拿得到
        for col_idx, column in enumerate(component.get("columns") or []):
            field_ref = column.get("fieldRef")
            if field_ref and available_fields and field_ref not in available_fields:
                issues.append(
                    _issue(
                        "FIELD",
                        "COLUMN_FIELD_UNKNOWN",
                        f"区域 {rid} 的列 {column.get('label') or column.get('id')} "
                        f"引用了拿不到的字段 {field_ref}。",
                        path=f"/regions/{idx}/component/columns/{col_idx}/fieldRef",
                        suggestion=f"可用字段：{'、'.join(sorted(available_fields))}。",
                    )
                )

        # 条件样式的字段必须拿得到
        for hint_idx, hint in enumerate(component.get("styleHints") or []):
            field_ref = hint.get("fieldRef")
            if field_ref and available_fields and field_ref not in available_fields:
                issues.append(
                    _issue(
                        "FIELD",
                        "STYLE_HINT_FIELD_UNKNOWN",
                        f"区域 {rid} 的条件样式引用了拿不到的字段 {field_ref}。",
                        path=f"/regions/{idx}/component/styleHints/{hint_idx}/fieldRef",
                        suggestion=f"可用字段：{'、'.join(sorted(available_fields))}。",
                    )
                )
            rule = str(hint.get("rule") or "HIGHLIGHT_ABOVE_THRESHOLD").upper()
            comparison = str(hint.get("comparison") or "GT").upper()
            if rule == "SORT":
                # 排序指令的比较符是 ASC / DESC，不是阈值比较。
                if comparison not in SUPPORTED_SORT_COMPARISONS:
                    issues.append(
                        _issue(
                            "CAPABILITY",
                            "STYLE_HINT_COMPARISON_UNSUPPORTED",
                            f"区域 {rid} 的排序指令用了不支持的比较符 {comparison}。",
                            path=f"/regions/{idx}/component/styleHints/{hint_idx}/comparison",
                            suggestion="改成 ASC / DESC 之一。",
                        )
                    )
                continue
            if comparison not in SUPPORTED_COMPARISONS:
                issues.append(
                    _issue(
                        "CAPABILITY",
                        "STYLE_HINT_COMPARISON_UNSUPPORTED",
                        f"区域 {rid} 的条件样式用了不支持的比较符 {comparison}。",
                        path=f"/regions/{idx}/component/styleHints/{hint_idx}/comparison",
                        suggestion="改成 GT / GTE / LT / LTE / EQ / NE 之一。",
                    )
                )
            if hint.get("threshold") is None:
                issues.append(
                    _issue(
                        "STRUCTURE",
                        "STYLE_HINT_THRESHOLD_MISSING",
                        f"区域 {rid} 的条件样式缺少 threshold。",
                        path=f"/regions/{idx}/component/styleHints/{hint_idx}/threshold",
                    )
                )

    # ---- 全局 preFilters 的字段必须存在 ----
    for filter_idx, condition in enumerate(
        (definition_draft.get("defaults") or {}).get("preFilters") or []
    ):
        field_ref = condition.get("fieldRef")
        known: set[str] = set()
        for dataset in datasets.values():
            known |= _dataset_fields(dataset)
        if field_ref and known and field_ref not in known:
            issues.append(
                _issue(
                    "FIELD",
                    "FILTER_FIELD_UNKNOWN",
                    f"全局过滤按 {field_ref} 筛选，但数据源里没有这个字段。",
                    path=f"/defaults/preFilters/{filter_idx}/fieldRef",
                    suggestion=f"可用字段：{'、'.join(sorted(known))}。",
                )
            )

    passed = len(issues) == 0
    logger.info(
        "[validation_service] passed=%s issue_count=%d", passed, len(issues)
    )
    return ValidationResult(passed=passed, issues=issues)


def _validate_scope_selectors(
    scope: dict[str, Any], issues: list[ValidationIssue]
) -> None:
    """scope.selectors 里的实例必须在授权范围内。"""
    from app.services.scope_service import is_subject_authorized

    selectors = scope.get("selectors") or {}
    subject_ref = scope.get("subjectRef") or "machine_room"
    for subject_id in selectors.get("machineRoomId") or []:
        if not is_subject_authorized(subject_ref, str(subject_id)):
            issues.append(
                _issue(
                    "PERMISSION",
                    "SCOPE_INSTANCE_NOT_AUTHORIZED",
                    f"当前账号没有 {subject_id} 的授权，无法统计该机房的能耗。",
                    path="/defaults/scope/selectors/machineRoomId",
                    suggestion="改成当前账号有权限的机房。",
                )
            )



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
