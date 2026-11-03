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
from app.services.data_source_mode import (
    DEFAULT_SOURCE_MODE,
    SOURCE_MODE_KEY,
    normalize_source_mode,
)
from app.services.effective_combination import (
    normalize_combination,
    validate_combination,
)


logger = logging.getLogger(__name__)


# 本地支持的组件类型 / 图表类型（和前端渲染保持一致）
SUPPORTED_COMPONENT_TYPES = {
    "TEXT",
    "METRIC_CARD",
    "TABLE",
    "CHART",
    "DATA_QUALITY",
    "ANALYSIS",
    # 容器（分组）：只管排列与阅读顺序，**没有数据**（契约 0002 §1）。
    "CONTAINER",
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

#: **提示级**问题的层：它**不阻断**校验（页面要可用、草稿要能存、撤销要能用）。
WARNING_LAYER = "WARNING"

#: 阻断校验的层。只有这些层里的问题会让 ``passed`` 变成 False。
#:
#: 历史上所有问题都落在这几层里 —— 所以这条规则**不改变**既有判定，
#: 只是给「提示级」问题（``WARNING``）留一条不阻断的路。
BLOCKING_LAYERS = frozenset({"STRUCTURE", "FIELD", "METRIC", "PERMISSION", "CAPABILITY"})

#: 区域上**失效引用**标记的键（展示执行层写进来的）。
#:
#: 这个键是**允许**的：它不是未知键，校验层不因此判失败。
REGION_ERRORS_KEY = "errors"


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


def _is_template_row_key_column(region: dict[str, Any]) -> bool:
    """这一区域的表格列是**模板态方案声明的行对象取值键**，不是数据源字段。

    判据：区域挂了 ``templateBinding.previewSpec`` 且它声明了栏目。转换层把
    同一份定稿列同时落进 ``component.columns`` 与 ``previewSpec.columns``
    （工单 03 的三跳），所以两边都有栏目时，组件里的列就是方案声明的列。

    这类列的 ``fieldRef`` 是取值键（工单 02 / 03 的 schema 契约：「不是真实
    数据源绑定」），**不按数据源字段校验** —— 真实取数通道是后续独立决策
    （ADR 0004 §5.9）。
    """
    if not isinstance(region, dict):
        return False
    binding = region.get("templateBinding")
    if not isinstance(binding, dict):
        return False
    spec = binding.get("previewSpec")
    if not isinstance(spec, dict):
        return False
    return bool(spec.get("columns"))


#: 容器（分组）组件类型：容器只管排列与阅读顺序，**没有数据**。
CONTAINER_COMPONENT_TYPE = "CONTAINER"


def _flatten_regions_for_validation(
    regions: list[Any] | None, prefix: str = ""
) -> list[tuple[str, dict[str, Any]]]:
    """把区域树摊平成 ``(path, region)``，路径形如 ``/0``、``/0/children/1``。

    分组落地之后，**组内的卡片也要被校验**：它们的 id、角色、组件类型、
    数据绑定与顶层卡片一样受同一套规则约束。
    容器本身不校验数据域（它没有数据）。
    """
    flat: list[tuple[str, dict[str, Any]]] = []
    for index, region in enumerate(regions or []):
        if not isinstance(region, dict):
            continue
        path = f"{prefix}/{index}"
        flat.append((path, region))
        children = region.get("children")
        if isinstance(children, list):
            flat.extend(_flatten_regions_for_validation(children, f"{path}/children"))
    return flat


def _is_container_region(region: dict[str, Any]) -> bool:
    component = region.get("component") or {}
    if not isinstance(component, dict):
        return False
    return str(component.get("type") or "").upper() == CONTAINER_COMPONENT_TYPE


def _region_error_entries(region: dict[str, Any]) -> list[dict[str, Any]]:
    """区域上的**失效引用**标记（``errors`` 键）。

    这是展示执行层写进来的（强制删除后，引用方被标成引用失效）。
    它**不是**未知键 —— 校验层认它，但只当**提示**看，不阻断校验：
    页面要能用、这份「错误草稿」要能存、撤销也要能用。
    """
    errors = region.get(REGION_ERRORS_KEY)
    if not isinstance(errors, list):
        return []
    return [item for item in errors if isinstance(item, dict)]


def _validate_source_modes(
    definition_draft: dict[str, Any], issues: list[ValidationIssue]
) -> None:
    """来源模式（``sourceMode``）的一致性校验。

    契约表 §1：来源模式是**服务端记录的一等事实**，三处同名同类型 ——
    定义级 ``metadata.sourceMode``（权威）、区域级
    ``templateBinding.sourceMode``、区域级 ``templateBinding.previewSpec.sourceMode``。

    两条规则：

    1. 记下来的值必须是合法取值（``simulated`` / ``user_json``）——
       认不出来的来源模式**不许**混进定义；
    2. 区域级与定义级**不得不同**（不同即校验失败）：一份报表不混两种来源
       的数字（规格 US51）。

    老定义没有记录 → 按默认口径（模拟示例，ADR 0004）处理，不因此失败。
    """
    metadata = definition_draft.get("metadata") or {}
    definition_mode = normalize_source_mode(metadata.get(SOURCE_MODE_KEY))
    if SOURCE_MODE_KEY in metadata and definition_mode is None:
        issues.append(
            _issue(
                "FIELD",
                "SOURCE_MODE_INVALID",
                f"报表定义的来源模式 {metadata.get(SOURCE_MODE_KEY)!r} 不是合法取值。",
                path=f"/metadata/{SOURCE_MODE_KEY}",
                suggestion="改成 simulated 或 user_json。",
            )
        )
    effective = definition_mode or DEFAULT_SOURCE_MODE

    for order, (idx, region) in enumerate(
        _flatten_regions_for_validation(definition_draft.get("regions") or [])
    ):
        if not isinstance(region, dict):
            continue
        binding = region.get("templateBinding")
        binding = binding if isinstance(binding, dict) else {}
        candidates = [
            (binding.get(SOURCE_MODE_KEY), f"/regions{idx}/templateBinding/{SOURCE_MODE_KEY}"),
            (
                (binding.get("previewSpec") or {}).get(SOURCE_MODE_KEY),
                f"/regions{idx}/templateBinding/previewSpec/{SOURCE_MODE_KEY}",
            ),
        ]
        for value, path in candidates:
            if value is None:
                continue
            mode = normalize_source_mode(value)
            if mode is None:
                issues.append(
                    _issue(
                        "FIELD",
                        "SOURCE_MODE_INVALID",
                        f"区域 {region.get('id') or order + 1} 的来源模式 {value!r} 不是合法取值。",
                        path=path,
                        suggestion="改成 simulated 或 user_json。",
                    )
                )
                continue
            if mode != effective:
                issues.append(
                    _issue(
                        "FIELD",
                        "SOURCE_MODE_MISMATCH",
                        f"区域 {region.get('id') or order + 1} 的来源模式是 {mode}，"
                        f"与报表定义的 {effective} 不一致。",
                        path=path,
                        suggestion="一份报表只用一种来源：让区域与定义保持一致。",
                    )
                )


def _validate_effective_combination(
    definition_draft: dict[str, Any], issues: list[ValidationIssue]
) -> None:
    """生效组合（契约表 §2）的一致性校验。

    这一组是**绑在一起**的：来源模式、输入 ID 与版本、字段映射、
    处理规则与定义版本。校验三件事：

    1. 组合本身的形状与取值合法（固定 9 键、来源模式合法、用户 JSON 模式
       必须绑定输入）；
    2. 组合里的来源模式与定义级 ``metadata.sourceMode`` 一致，
       定义身份与版本与 ``metadata.templateBinding`` 一致 ——
       不一致说明这份定义是拼出来的；
    3. 字段映射引用的字段**确实存在于当前绑定的输入**（逐项核对）。

    老定义没有 ``effectiveCombination`` → 按空组合处理，不因此失败
    （契约表 §2.2）。
    """
    metadata = definition_draft.get("metadata") or {}
    if not isinstance(metadata, dict):
        return
    raw = metadata.get("effectiveCombination")
    if raw is None:
        return
    if not isinstance(raw, dict):
        issues.append(
            _issue(
                "FIELD",
                "EFFECTIVE_COMBINATION_INVALID",
                "报表定义的生效组合不是对象。",
                path="/metadata/effectiveCombination",
                suggestion="重新生成报表，让服务端重建这一组绑定。",
            )
        )
        return

    combination = normalize_combination(raw)
    for problem in validate_combination(combination):
        issues.append(
            _issue(
                "FIELD",
                "EFFECTIVE_COMBINATION_INVALID",
                f"生效组合不合法：{problem}",
                path="/metadata/effectiveCombination",
                suggestion="重新生成报表，让服务端重建这一组绑定。",
            )
        )

    definition_mode = normalize_source_mode(metadata.get(SOURCE_MODE_KEY))
    effective = definition_mode or DEFAULT_SOURCE_MODE
    if combination["sourceMode"] != effective:
        issues.append(
            _issue(
                "FIELD",
                "EFFECTIVE_COMBINATION_MISMATCH",
                f"生效组合的来源模式是 {combination['sourceMode']}，"
                f"与报表定义的 {effective} 不一致。",
                path="/metadata/effectiveCombination/sourceMode",
                suggestion="一份报表只用一种来源：让组合与定义保持一致。",
            )
        )

    binding = metadata.get("templateBinding")
    binding = binding if isinstance(binding, dict) else {}
    if binding:
        for key, combination_key in (
            ("definitionId", "definitionId"),
            ("definitionVersion", "definitionVersion"),
        ):
            expected = binding.get(key)
            actual = combination[combination_key]
            if expected is None:
                continue
            if str(expected) != str(actual):
                issues.append(
                    _issue(
                        "FIELD",
                        "EFFECTIVE_COMBINATION_MISMATCH",
                        f"生效组合里的 {key} 是 {actual}，"
                        f"与定义的 {expected} 不是同一版。",
                        path=f"/metadata/effectiveCombination/{combination_key}",
                        suggestion="重新确认方案，让定义与生效组合一起提交。",
                    )
                )

    # 字段映射逐项核对：绑定的输入版本里必须真的有这些字段。
    input_id = combination.get("inputId")
    input_version = combination.get("inputVersion")
    if input_id and input_version is not None:
        field_keys = _input_field_keys(str(input_id), int(input_version))
        if field_keys is not None:
            for item in combination["fieldMapping"]:
                key = str(item.get("inputFieldKey") or "")
                if key and key not in field_keys:
                    issues.append(
                        _issue(
                            "FIELD",
                            "FIELD_MAPPING_UNKNOWN_FIELD",
                            f"生效组合把列绑定到了输入里不存在的字段「{key}」。",
                            path="/metadata/effectiveCombination/fieldMapping",
                            suggestion="重新确认方案，按当前输入的字段重建映射。",
                        )
                    )


def _input_field_keys(input_id: str, input_version: int) -> set[str] | None:
    """绑定的那一版输入里有哪些字段。

    找不到输入记录时返回 ``None``（不据此判失败 —— 输入缺失由渲染层
    明确报错，这里不重复报）。
    """
    try:
        from app.services.user_json_service import user_json_service

        record = user_json_service.get(input_id, input_version)
    except Exception:  # noqa: BLE001
        return None
    if record is None:
        return None
    return {
        str(item.get("key"))
        for item in (record.schema.get("fields") or [])
        if item.get("key")
    }


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

    # ---- 来源模式层：定义级与区域级必须一致 ----
    _validate_source_modes(definition_draft, issues)
    _validate_effective_combination(definition_draft, issues)

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
    for idx, region in _flatten_regions_for_validation(regions):
        is_container = _is_container_region(region)
        rid = region.get("id") or ""
        context = region.get("context") or {}
        window = context.get("timeWindow")
        if isinstance(window, dict):
            window_error = fixed_interval_error(window)
            if window_error:
                issues.append(_issue(
                    "STRUCTURE", "FIXED_INTERVAL_INVALID", window_error,
                    path=f"/regions{idx}/context/timeWindow",
                ))
        if not rid:
            issues.append(
                _issue(
                    "STRUCTURE",
                    "REGION_ID_MISSING",
                    f"第 {order + 1} 个区域没有 id。",
                    path=f"/regions{idx}/id",
                )
            )
        elif rid in seen_ids:
            issues.append(
                _issue(
                    "STRUCTURE",
                    "REGION_ID_DUPLICATED",
                    f"区域 id 重复：{rid}",
                    path=f"/regions{idx}/id",
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
                    path=f"/regions{idx}/role",
                )
            )

        # 区域上的**失效引用**标记：只报**提示**，**不阻断**校验。
        #
        # 它是展示执行层（强制删除引用方仍在）写进来的：这份定义照样能用、
        # 能存、能撤销 —— 所以问题落 ``WARNING`` 层，不进阻断层。
        for entry in _region_error_entries(region):
            issues.append(
                _issue(
                    WARNING_LAYER,
                    "REGION_BROKEN_REFERENCE",
                    f"区域 {rid} 的引用已失效："
                    f"{entry.get('message') or entry.get('code') or '引用失效'}",
                    path=f"/regions{idx}/{REGION_ERRORS_KEY}",
                    suggestion="重新绑定这张卡片，或把它删掉。",
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
                    path=f"/regions{idx}/component/type",
                )
            )
        elif ctype not in SUPPORTED_COMPONENT_TYPES:
            issues.append(
                _issue(
                    "CAPABILITY",
                    "COMPONENT_TYPE_UNSUPPORTED",
                    f"区域 {rid} 使用了不支持的组件类型：{ctype}",
                    path=f"/regions{idx}/component/type",
                    suggestion="改成 METRIC_CARD / TABLE / CHART / DATA_QUALITY 之一。",
                )
            )

        if is_container:
            # 容器（分组）：**没有数据**，只管排列与阅读顺序。
            children = region.get("children")
            if not isinstance(children, list) or not children:
                issues.append(
                    _issue(
                        "STRUCTURE",
                        "CONTAINER_CHILDREN_EMPTY",
                        f"分组 {rid} 里没有卡片。",
                        path=f"/regions{idx}/children",
                        suggestion="把卡片移进来，或者取消这个分组。",
                    )
                )
            if region.get("data") or region.get("binding"):
                issues.append(
                    _issue(
                        "STRUCTURE",
                        "CONTAINER_HAS_DATA",
                        f"分组 {rid} 不该带数据或绑定：分组只管外观。",
                        path=f"/regions{idx}/data",
                        suggestion="把数据留在卡片上。",
                    )
                )
            arrangement = (component or {}).get("arrangement") or {}
            if not isinstance(arrangement, dict) or not arrangement:
                issues.append(
                    _issue(
                        "STRUCTURE",
                        "CONTAINER_ARRANGEMENT_MISSING",
                        f"分组 {rid} 缺少排列设置。",
                        path=f"/regions{idx}/component/arrangement",
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
                        path=f"/regions{idx}/component/chartKind",
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
                        path=f"/regions{idx}/data/inputs/{input_name}/datasetRef",
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
                            path=f"/regions{idx}/data/pipeline/nodes/{node_idx}/metricRef",
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
                                path=f"/regions{idx}/data/pipeline/nodes/{node_idx}/metricVersion",
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
                                f"/regions{idx}/data/pipeline/nodes/{node_idx}"
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
                                f"/regions{idx}/data/pipeline/nodes/{node_idx}"
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
                                f"/regions{idx}/data/pipeline/nodes/{node_idx}"
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
                        path=f"/regions{idx}/binding/derivedColumns",
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
                            path=f"/regions{idx}/context/grain",
                            suggestion=f"改成 {'、'.join(supported)} 之一。",
                        )
                    )

        # 组件列引用的字段必须拿得到
        #
        # 例外：**模板态**方案声明的栏目，它的 ``fieldRef`` 是**行对象取值键**
        # （工单 02 / 03 的契约：``PlanColumn.fieldRef`` 与 ``TemplateColumn.fieldRef``
        # 都写明「不是真实数据源绑定」，造数器按它造行、前端按它读单元格）。
        # 真实取数通道是后续独立决策（ADR 0004 §5.9），所以模板态的列**不按
        # 数据源字段校验**；数据源字段是真实取数那一步才落的绑定。
        for col_idx, column in enumerate(component.get("columns") or []):
            field_ref = column.get("fieldRef")
            if (
                field_ref
                and available_fields
                and field_ref not in available_fields
                and not _is_template_row_key_column(region)
            ):
                issues.append(
                    _issue(
                        "FIELD",
                        "COLUMN_FIELD_UNKNOWN",
                        f"区域 {rid} 的列 {column.get('label') or column.get('id')} "
                        f"引用了拿不到的字段 {field_ref}。",
                        path=f"/regions{idx}/component/columns/{col_idx}/fieldRef",
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
                        path=f"/regions{idx}/component/styleHints/{hint_idx}/fieldRef",
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
                            path=f"/regions{idx}/component/styleHints/{hint_idx}/comparison",
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
                        path=f"/regions{idx}/component/styleHints/{hint_idx}/comparison",
                        suggestion="改成 GT / GTE / LT / LTE / EQ / NE 之一。",
                    )
                )
            if hint.get("threshold") is None:
                issues.append(
                    _issue(
                        "STRUCTURE",
                        "STYLE_HINT_THRESHOLD_MISSING",
                        f"区域 {rid} 的条件样式缺少 threshold。",
                        path=f"/regions{idx}/component/styleHints/{hint_idx}/threshold",
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

    # 只有**阻断层**的问题才让校验失败。
    #
    # 提示级（``WARNING``）的问题只是提醒：带失效引用标记的卡片**不阻断**
    # 校验 —— 页面要可用、这份「错误草稿」要能存、撤销也要能用。
    passed = not any(issue.layer in BLOCKING_LAYERS for issue in issues)
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
