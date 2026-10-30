"""生效组合（effective combination，契约表 §2）。

**一组绑定，一组提交。**

来源模式、输入 ID 与版本、字段映射、处理规则与定义版本
**绑成一个不可拆的整体**（规格第 31 条）：报表的数字从哪来、拿哪一版数据、
哪一列对应哪个字段、按什么口径处理、属于哪一版定义 —— 这五件事必须同生共死。

**固定 9 键**（契约表 §2.1）::

    planId, planRevision, definitionId, definitionVersion,
    sourceMode, inputId, inputVersion, fieldMapping, processingRules

落点是 ``ReportDefinitionDraft.metadata.effectiveCombination``：
随定义落库（``report_design_definitions.definition_json``），并进会话快照。

**候选与生效分开**（规格第 30 条）：候选可以保存用于继续设计，
但绝不覆盖已确认的输入、来源与字段映射。只有接受动作把整组一起原子切过去；
取消、解析 / 校验失败、造数失败、版本冲突一律**保持旧生效组合**。

本模块只做**纯函数**：组一份、验一份、比一份。它不落库、不提交 ——
提交由定义层的写入路径负责。
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from app.services.data_source_mode import (
    DEFAULT_SOURCE_MODE,
    SOURCE_MODE_SIMULATED,
    SOURCE_MODE_USER_JSON,
    normalize_source_mode,
)
from app.services.json_input_service import (
    PROCESSING_OPERATIONS,
    validate_field_mapping,
    validate_processing_rules,
)


#: 生效组合的**固定 9 键**（契约表 §2.1）。
COMBINATION_KEYS: tuple[str, ...] = (
    "planId",
    "planRevision",
    "definitionId",
    "definitionVersion",
    "sourceMode",
    "inputId",
    "inputVersion",
    "fieldMapping",
    "processingRules",
)

#: 字段映射条目的键（契约表 §2.1）。
FIELD_MAPPING_KEYS: tuple[str, ...] = (
    "regionId",
    "columnId",
    "inputFieldKey",
    "inputFieldPath",
)

#: 处理规则条目的键（契约表 §2.1）。
PROCESSING_RULE_KEYS: tuple[str, ...] = (
    "ruleId",
    "operation",
    "targetFieldKey",
    "params",
)


def empty_combination(
    *,
    plan_id: str | None = None,
    plan_revision: int | None = None,
    definition_id: str | None = None,
    definition_version: int | None = None,
) -> dict[str, Any]:
    """空组合（契约表 §2.2 的老数据默认行为）。

    老定义没有 ``effectiveCombination`` → 按空组合处理：
    ``sourceMode = "simulated"``、``inputId = None``、``inputVersion = None``、
    ``fieldMapping = []``、``processingRules = []``；
    方案与定义的身份、版本从既有 ``metadata.templateBinding`` 回填。
    """
    return {
        "planId": plan_id or "",
        "planRevision": int(plan_revision or 0),
        "definitionId": definition_id or "",
        "definitionVersion": int(definition_version or 0),
        "sourceMode": DEFAULT_SOURCE_MODE,
        "inputId": None,
        "inputVersion": None,
        "fieldMapping": [],
        "processingRules": [],
    }


def normalize_combination(value: Any) -> dict[str, Any]:
    """把任意结构归一成**固定 9 键**的组合。

    缺的键按空组合补齐；多的键丢掉 —— 组合的形状是封闭的。
    """
    base = empty_combination()
    if not isinstance(value, Mapping):
        return base
    source_mode = normalize_source_mode(value.get("sourceMode")) or DEFAULT_SOURCE_MODE
    field_mapping = [
        {
            "regionId": str(item.get("regionId") or ""),
            "columnId": str(item.get("columnId") or ""),
            "inputFieldKey": str(item.get("inputFieldKey") or ""),
            "inputFieldPath": (
                str(item.get("inputFieldPath"))
                if item.get("inputFieldPath") is not None
                else None
            ),
        }
        for item in (value.get("fieldMapping") or [])
        if isinstance(item, Mapping)
    ]
    processing_rules = [
        {
            "ruleId": str(item.get("ruleId") or ""),
            "operation": str(item.get("operation") or ""),
            "targetFieldKey": str(item.get("targetFieldKey") or ""),
            "params": dict(item.get("params") or {})
            if isinstance(item.get("params"), Mapping)
            else {},
        }
        for item in (value.get("processingRules") or [])
        if isinstance(item, Mapping)
    ]
    input_id = value.get("inputId")
    input_version = value.get("inputVersion")
    base.update(
        {
            "planId": str(value.get("planId") or ""),
            "planRevision": int(value.get("planRevision") or 0),
            "definitionId": str(value.get("definitionId") or ""),
            "definitionVersion": int(value.get("definitionVersion") or 0),
            "sourceMode": source_mode,
            "inputId": str(input_id) if input_id else None,
            "inputVersion": int(input_version) if input_version is not None else None,
            "fieldMapping": field_mapping,
            "processingRules": processing_rules,
        }
    )
    return base


def build_combination(
    *,
    plan_id: str,
    plan_revision: int,
    definition_id: str,
    definition_version: int,
    source_mode: str,
    input_id: str | None = None,
    input_version: int | None = None,
    field_mapping: Sequence[Mapping[str, Any]] | None = None,
    processing_rules: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """组一份生效组合（固定 9 键）。"""
    combination = empty_combination(
        plan_id=plan_id,
        plan_revision=plan_revision,
        definition_id=definition_id,
        definition_version=definition_version,
    )
    combination["sourceMode"] = normalize_source_mode(source_mode) or DEFAULT_SOURCE_MODE
    combination["inputId"] = str(input_id) if input_id else None
    combination["inputVersion"] = int(input_version) if input_version is not None else None
    combination["fieldMapping"] = [
        {
            "regionId": str(item.get("regionId") or ""),
            "columnId": str(item.get("columnId") or ""),
            "inputFieldKey": str(item.get("inputFieldKey") or ""),
            "inputFieldPath": (
                str(item.get("inputFieldPath"))
                if item.get("inputFieldPath") is not None
                else None
            ),
        }
        for item in (field_mapping or [])
    ]
    combination["processingRules"] = [
        {
            "ruleId": str(item.get("ruleId") or ""),
            "operation": str(item.get("operation") or ""),
            "targetFieldKey": str(item.get("targetFieldKey") or ""),
            "params": dict(item.get("params") or {})
            if isinstance(item.get("params"), Mapping)
            else {},
        }
        for item in (processing_rules or [])
    ]
    return combination


def validate_combination(
    combination: Any, *, field_keys: Sequence[str] | None = None
) -> list[str]:
    """校验一份生效组合，返回问题清单（空 = 通过）。

    校验内容：

    - 形状必须是固定 9 键；
    - 来源模式取值合法；
    - 用户 JSON 模式**必须**有输入 ID 与版本；模拟模式必须没有；
    - 字段映射引用的字段确实存在于当前输入（逐项核对）；
    - 处理规则只走受控操作枚举与标量参数。
    """
    problems: list[str] = []
    if not isinstance(combination, Mapping):
        return ["生效组合不是对象。"]
    normalized = normalize_combination(combination)
    for key in COMBINATION_KEYS:
        if key not in combination:
            problems.append(f"生效组合缺少「{key}」。")
    source_mode = normalized["sourceMode"]
    if source_mode not in (SOURCE_MODE_SIMULATED, SOURCE_MODE_USER_JSON):
        problems.append(f"来源模式「{source_mode}」不是合法取值。")
    if source_mode == SOURCE_MODE_USER_JSON:
        if not normalized["inputId"]:
            problems.append("用户 JSON 模式必须有输入记录 ID。")
        if normalized["inputVersion"] is None:
            problems.append("用户 JSON 模式必须有输入版本。")
    else:
        if normalized["inputId"] or normalized["inputVersion"] is not None:
            problems.append("模拟示例模式不应绑定 JSON 输入。")
    if field_keys is not None:
        problems.extend(validate_field_mapping(normalized["fieldMapping"], field_keys))
        problems.extend(validate_processing_rules(normalized["processingRules"], field_keys))
    else:
        # 没有当前输入的字段清单时，按组合自己声明的字段核对：
        # 规则指向的字段必须在映射里出现过（不能凭空多一个字段）。
        known = {
            str(item.get("inputFieldKey") or "")
            for item in normalized["fieldMapping"]
        }
        problems.extend(validate_processing_rules(normalized["processingRules"], sorted(known)))
    return problems


def combination_matches(left: Any, right: Any) -> bool:
    """两份组合是否**逐字段一致**（契约表 §4.2 的接受条件）。

    用于「待确认修改计划所基于的组合 == 当前生效组合吗」。
    """
    return combination_fingerprint(left) == combination_fingerprint(right)


def combination_fingerprint(combination: Any) -> str:
    """组合的稳定指纹（逐字段比较用）。

    只对**结构**取指纹：键顺序固定、列表按给定顺序。
    """
    normalized = normalize_combination(combination)
    payload = {key: normalized[key] for key in COMBINATION_KEYS}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def combination_of_definition(definition: Any) -> dict[str, Any]:
    """从报表定义里读出生效组合（老定义按空组合处理）。"""
    if not isinstance(definition, Mapping):
        return empty_combination()
    metadata = definition.get("metadata")
    if not isinstance(metadata, Mapping):
        return empty_combination()
    combination = metadata.get("effectiveCombination")
    if not isinstance(combination, Mapping) or not combination:
        binding = metadata.get("templateBinding")
        binding = binding if isinstance(binding, Mapping) else {}
        return empty_combination(
            plan_id=str(binding.get("planId") or ""),
            plan_revision=binding.get("planRevision"),
            definition_id=str(binding.get("definitionId") or ""),
            definition_version=binding.get("definitionVersion"),
        )
    return normalize_combination(combination)


def processing_rules_for(
    combination: Any, *, region_id: str, column_id: str
) -> list[dict[str, Any]]:
    """取某个「板块 + 列」上的处理规则。"""
    normalized = normalize_combination(combination)
    return [
        rule
        for rule in normalized["processingRules"]
        if rule.get("ruleId")
        and f"{rule['ruleId']}" == f"rule_{region_id}_{column_id}".lower()
    ]


def is_controlled_operation(operation: Any) -> bool:
    """这个操作是不是受控枚举里的一个。"""
    return str(operation or "") in PROCESSING_OPERATIONS
