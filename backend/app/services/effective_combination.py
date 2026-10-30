"""生效组合（effectiveCombination，契约表 §2）。

**这份报表当前生效的那一组绑定**：用哪个方案、哪个定义版本、来源模式是
模拟示例还是用户 JSON、输入是哪一版、列怎么映射、走了哪些处理规则。

它存在的理由是**版本判据**：候选保存的是「它所基于的那一份组合」，
接受时逐字段比对 —— 对不上就是**过期 / 冲突**，服务端直接拒绝，
旧生效组合原样保留（契约表 §2.2 / §4.2）。

**老数据不迁移**：没有这一项的定义按**空组合**处理（来源模式取默认的
模拟示例、输入为空、映射与规则为空），方案与定义的身份从既有的
``metadata.templateBinding`` 回填 —— 读得懂就行，不改写历史。

落点：``ReportDefinitionDraft.metadata.effectiveCombination``（随定义落库）。
"""
from __future__ import annotations

from typing import Any, Mapping

from app.services.data_source_mode import source_mode_of_definition


#: 生效组合的字段（契约表 §2.1）。比较时**逐字段**比。
COMBINATION_FIELDS: tuple[str, ...] = (
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

#: 生效组合在定义里的**落点**。
COMBINATION_KEY = "effectiveCombination"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def combination_of(definition: Any) -> dict[str, Any]:
    """这份定义当前**生效**的组合。

    没有记录过的定义按**空组合**处理（契约表 §2.2）：来源模式取默认的
    模拟示例、输入为空、映射与规则为空；方案与定义的身份从既有的
    ``metadata.templateBinding`` 回填。

    ``definitionVersion`` 是**这份定义自己的版本**（草稿 revision）——
    编辑一次就是新一版；老定义没有 revision 时回退到方案版本。
    """
    metadata: Mapping[str, Any] = {}
    if isinstance(definition, Mapping):
        raw_metadata = definition.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
    recorded = metadata.get(COMBINATION_KEY)
    recorded = recorded if isinstance(recorded, Mapping) else {}
    binding = metadata.get("templateBinding")
    binding = binding if isinstance(binding, Mapping) else {}

    revision = metadata.get("revision")
    if revision is None:
        definition_version = _as_int(
            binding.get("definitionVersion") or recorded.get("definitionVersion")
        )
    else:
        definition_version = _as_int(revision)

    field_mapping = recorded.get("fieldMapping")
    processing_rules = recorded.get("processingRules")
    return {
        "planId": str(binding.get("planId") or recorded.get("planId") or ""),
        "planRevision": _as_int(binding.get("planRevision") or recorded.get("planRevision")),
        "definitionId": str(
            binding.get("definitionId")
            or recorded.get("definitionId")
            or metadata.get("id")
            or ""
        ),
        "definitionVersion": definition_version,
        "sourceMode": str(
            recorded.get("sourceMode") or source_mode_of_definition(definition).mode
        ),
        "inputId": recorded.get("inputId"),
        "inputVersion": recorded.get("inputVersion"),
        "fieldMapping": list(field_mapping) if isinstance(field_mapping, list) else [],
        "processingRules": (
            list(processing_rules) if isinstance(processing_rules, list) else []
        ),
    }


def record_combination(metadata: Any, combination: Mapping[str, Any]) -> None:
    """把生效组合**记进**定义 metadata（随定义落库）。

    只写契约表 §2.1 定的九个键 —— 别的一概不写。
    """
    if not isinstance(metadata, dict):
        return
    metadata[COMBINATION_KEY] = {
        field: combination.get(field) for field in COMBINATION_FIELDS
    }


def combinations_match(left: Any, right: Any) -> bool:
    """两份生效组合**逐字段**一致吗（契约表 §4.2 的版本校验）。

    任何一个字段对不上 → 候选过期 / 冲突 → 服务端拒绝接受。
    """
    first = left if isinstance(left, Mapping) else {}
    second = right if isinstance(right, Mapping) else {}
    for field in COMBINATION_FIELDS:
        if first.get(field) != second.get(field):
            return False
    return True
