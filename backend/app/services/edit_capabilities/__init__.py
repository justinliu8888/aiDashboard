"""报表编辑能力目录 —— 统一注册入口。

模型工具与规则快路都从这里取能力：

* 模型工具：``app.tools.edit_operations_tool``（把能力暴露给 Agent）
* 规则快路 / 试做执行：``app.services.edit_trial_service``（同一份执行实现）

**参数与校验规则只定义一次**：能力记录里的 ``params_schema`` 是权威契约，
执行前统一校验，非法参数不静默忽略。
"""
from __future__ import annotations

from app.services.edit_capabilities.base import (  # noqa: F401
    CapabilityError,
    CapabilityOutcome,
    EditCapability,
    OperationContext,
    FAMILY_BLOCK,
    FAMILY_LAYOUT,
    FAMILY_READ,
    FAMILY_STAT,
    FAMILY_STYLE,
    FAMILY_SUBMIT,
    all_capabilities,
    capabilities_by_family,
    get_capability,
    register,
    validate_params,
)
from app.services.edit_capabilities import blocks as _blocks  # noqa: F401
from app.services.edit_capabilities import binding_component as _binding_component  # noqa: F401
from app.services.edit_capabilities import chart_component as _chart_component  # noqa: F401
from app.services.edit_capabilities import table_component as _table_component  # noqa: F401
from app.services.edit_capabilities import text_metric as _text_metric  # noqa: F401
from app.services.edit_capabilities.chart_data import (  # noqa: F401
    CATEGORICAL_PAYLOAD_CONTRACT,
    SIMULATED_DIMENSIONS,
    dimension_catalog,
    normalize_categories,
    resolve_dimension,
)


def capability_catalog_text() -> str:
    """给模型看的能力目录（按能力分组）。

    这段文字是**从注册表生成的**：能力增删这里自动跟着变，
    不会像旧提示词那样「承诺了执行器不支持的参数」。
    """
    lines: list[str] = []
    for family in (
        FAMILY_READ,
        FAMILY_BLOCK,
        FAMILY_STAT,
        FAMILY_STYLE,
        FAMILY_LAYOUT,
        FAMILY_SUBMIT,
    ):
        items = capabilities_by_family(family)
        if not items:
            continue
        lines.append(f"【{family}】")
        for capability in items:
            lines.append(f"- {capability.id}（{capability.title}）：{capability.description}")
        lines.append("")
    return "\n".join(lines).strip()


__all__ = [
    "CATEGORICAL_PAYLOAD_CONTRACT",
    "CapabilityError",
    "CapabilityOutcome",
    "EditCapability",
    "OperationContext",
    "SIMULATED_DIMENSIONS",
    "all_capabilities",
    "capabilities_by_family",
    "capability_catalog_text",
    "dimension_catalog",
    "get_capability",
    "normalize_categories",
    "register",
    "resolve_dimension",
    "validate_params",
]
