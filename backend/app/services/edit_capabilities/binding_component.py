"""展示绑定能力：输出选择、字段别名、展示过滤、展示排序与前 N 项。

对应第二批范围编号 **B01–B04**。

**这一层做什么**（协议 ``DataBinding``）：

* ``outputRef``          —— 这个区域绑哪个已登记的输出（B01）；
* ``fieldAliases``       —— 展示字段名 → 实际结果字段名（B02）；
* ``presentationFilters``—— 只过滤**展示记录**（B03）；
* ``sort`` / ``topN``    —— 展示排序与前 N 项（B04）。

**边界**（第二批的硬约束）：

* 只作用**本区域**的展示；源输出、全量总计、其他区域的输入**不动**；
* 未登记的引用**拒绝**，不自动新增跨区域数据依赖；
* 展示过滤**不是**统计过滤 —— 它不进 pipeline，不改计算结果；
* 不新增任何计算 / 聚合 / 单位换算能力。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.services.display_binding_service import (
    DisplayBindingError,
    CONDITION_OPERATORS,
    declared_field_candidates,
    require_declared_field,
    validate_condition,
)
from app.services.edit_capabilities.base import (
    CapabilityError,
    CapabilityOutcome,
    EditCapability,
    OperationContext,
    business_facts,
    register,
)


# --------------------------------------------------------------------------
# 参数契约
# --------------------------------------------------------------------------

class SetDataBindingParams(BaseModel):
    """一批**展示绑定**设置。

    每一项都可以单独给；**没给的项保持原样**（不改、不清）。
    """

    outputRef: str = Field(
        default="",
        description="改绑的输出引用（output:<name>）",
    )
    fieldAliases: dict[str, str] = Field(
        default_factory=dict,
        description='展示字段名 → 实际结果字段名，例如 {"时间点":"timestamp"}。右侧必须是已声明字段；也兼容可唯一识别的反向映射。',
    )
    clearFieldAliases: bool = False
    presentationFilters: list[dict[str, Any]] = Field(
        default_factory=list,
        description='只过滤展示记录。条件形如 {"type":"CONDITION","left":{"type":"FIELD","fieldRef":"energyKwh"},"operator":"GT","operands":[{"type":"LITERAL","value":400}]}；组用 type=GROUP、logic=AND/OR/NOT、conditions=[条件]。也接收 fieldRef/operator/value 简写，统一转成上述结构。',
        json_schema_extra={"items": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["CONDITION", "GROUP"]},
            "left": {"type": "object", "properties": {
                "type": {"type": "string", "enum": ["FIELD"]},
                "fieldRef": {"type": "string", "description": "已声明的实际字段名"}}, "required": ["type", "fieldRef"]},
            "operator": {"type": "string", "enum": list(CONDITION_OPERATORS)},
            "operands": {"type": "array", "items": {"type": "object", "properties": {
                "type": {"type": "string", "enum": ["LITERAL"]},
                "value": {"description": "字面量值；数字阈值用数字"}}, "required": ["type", "value"]}},
            "logic": {"type": "string", "enum": ["AND", "OR", "NOT"]},
            "conditions": {"type": "array", "items": {"type": "object", "description": "同结构的 Condition 或 ConditionGroup"}},
        }}},
    )
    clearPresentationFilters: bool = False
    sort: list[dict[str, Any]] = Field(
        default_factory=list,
        description="展示排序字段（fieldRef / direction / nulls）",
    )
    clearSort: bool = False
    topN: int | None = Field(
        default=None,
        description="展示前 N 项；只是展示裁剪，不限制取数",
    )
    clearTopN: bool = False

    @field_validator("outputRef", mode="before")
    @classmethod
    def _output_ref_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("fieldAliases", mode="before")
    @classmethod
    def _aliases(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {
            str(display).strip(): str(actual).strip()
            for display, actual in value.items()
            if str(display or "").strip() and str(actual or "").strip()
        }


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

def _binding_of(region: dict[str, Any]) -> dict[str, Any]:
    """区域的数据绑定（就地取，没有就建一个）。"""
    binding = region.get("binding")
    if not isinstance(binding, dict):
        binding = {}
        region["binding"] = binding
    return binding


def _registered_outputs(region: dict[str, Any]) -> dict[str, Any]:
    data = region.get("data") if isinstance(region, dict) else None
    pipeline = (data or {}).get("pipeline") if isinstance(data, dict) else None
    outputs = (pipeline or {}).get("outputs") if isinstance(pipeline, dict) else None
    return outputs if isinstance(outputs, dict) else {}


def _fail(message: str, *, reason: str, constraint: str, region_id: str, op: str):
    return CapabilityError(
        message,
        op=op,
        target=region_id,
        constraint=constraint,
        reason=reason,
    )


# --------------------------------------------------------------------------
# 执行实现
# --------------------------------------------------------------------------


def _normalize_filter(expr: dict[str, Any]) -> dict[str, Any]:
    """只转换含义明确的条件简写，字段准入仍走统一校验。"""
    out = dict(expr)
    if str(out.get("type", "")).upper() == "GROUP":
        out["conditions"] = [_normalize_filter(e) if isinstance(e, dict) else e
                             for e in out.get("conditions", [])]
        return out
    if "left" not in out and out.get("fieldRef"):
        out["left"] = {"type": "FIELD", "fieldRef": out.pop("fieldRef")}
    left = out.get("left")
    if isinstance(left, dict) and "type" not in left and left.get("fieldRef"):
        out["left"] = {**left, "type": "FIELD"}
    if "operands" not in out:
        if "value" in out:
            out["operands"] = [{"type": "LITERAL", "value": out.pop("value")}]
        elif "right" in out:
            right = out.pop("right")
            out["operands"] = [right if isinstance(right, dict) else {"type": "LITERAL", "value": right}]
    return out


def _set_data_binding(
    ctx: OperationContext, params: SetDataBindingParams
) -> CapabilityOutcome:
    """设置展示绑定：输出 / 别名 / 过滤 / 排序 / 前 N 项。

    **先校验、后写入**：任何一项不合法就**整体拒绝**，原定义一个字节都不改。
    """
    region_id = ctx.resolve_target(ctx.current_target)
    region = ctx.find_region(region_id)
    if region is None:
        raise _fail(
            f"目标区块 {region_id!r} 不存在。",
            reason="unknown_region",
            constraint="目标必须存在",
            region_id=region_id,
            op="SET_DATA_BINDING",
        )

    # 模型常把 source→display 写反；仅在一端是已声明字段时规范化。
    known = set(declared_field_candidates(region))
    aliases = {}
    for display, actual in params.fieldAliases.items():
        if actual not in known and display in known:
            display, actual = actual, display
        if display in aliases and aliases[display] != actual:
            raise _fail("展示别名映射有冲突。", reason="ambiguous_alias", constraint="一个展示名只能映射一个字段", region_id=region_id, op="SET_DATA_BINDING")
        aliases[display] = actual
    params = params.model_copy(update={
        "fieldAliases": aliases,
        "presentationFilters": [_normalize_filter(e) for e in params.presentationFilters],
    })
    binding = _binding_of(region)
    changes: list[dict[str, Any]] = []

    # -- 输出引用（B01） -------------------------------------------------
    if params.outputRef:
        outputs = _registered_outputs(region)
        name = params.outputRef.split(":", 1)[-1]
        if params.outputRef.startswith("output:") and name in outputs:
            before = str(binding.get("outputRef") or "")
            binding["outputRef"] = params.outputRef
            if before != params.outputRef:
                changes.append(
                    {"field": "binding.outputRef", "before": before, "after": params.outputRef}
                )
        else:
            available = "、".join(f"output:{key}" for key in outputs) or "（无）"
            raise _fail(
                f"输出引用 {params.outputRef!r} 没有登记；当前区域可用：{available}。",
                reason="unknown_output_ref",
                constraint="输出引用必须指向当前区域已登记的输出",
                region_id=region_id,
                op="SET_DATA_BINDING",
            )

    # -- 字段别名（B02） -------------------------------------------------
    if params.clearFieldAliases:
        before = dict(binding.get("fieldAliases") or {})
        if before:
            binding.pop("fieldAliases", None)
            changes.append({"field": "binding.fieldAliases", "before": before, "after": None})
    elif params.fieldAliases:
        for display, actual in params.fieldAliases.items():
            try:
                require_declared_field(region, actual, what=f"别名字段 {display!r} 指向的")
            except DisplayBindingError as exc:
                raise _fail(
                    exc.message,
                    reason=exc.reason,
                    constraint=exc.constraint,
                    region_id=region_id,
                    op="SET_DATA_BINDING",
                ) from exc
        before = dict(binding.get("fieldAliases") or {})
        merged = {**before, **params.fieldAliases}
        binding["fieldAliases"] = merged
        if before != merged:
            changes.append(
                {"field": "binding.fieldAliases", "before": before, "after": merged}
            )

    # -- 展示过滤（B03） -------------------------------------------------
    if params.clearPresentationFilters:
        before = list(binding.get("presentationFilters") or [])
        if before:
            binding.pop("presentationFilters", None)
            changes.append(
                {"field": "binding.presentationFilters", "before": before, "after": None}
            )
    elif params.presentationFilters:
        for index, expr in enumerate(params.presentationFilters):
            try:
                validate_condition(expr, region, path=f"presentationFilters[{index}]")
            except DisplayBindingError as exc:
                raise _fail(
                    exc.message,
                    reason=exc.reason,
                    constraint=exc.constraint,
                    region_id=region_id,
                    op="SET_DATA_BINDING",
                ) from exc
        before = list(binding.get("presentationFilters") or [])
        merged = before + list(params.presentationFilters)
        binding["presentationFilters"] = merged
        if before != merged:
            changes.append(
                {
                    "field": "binding.presentationFilters",
                    "before": before,
                    "after": merged,
                }
            )

    # -- 展示排序（B04） -------------------------------------------------
    if params.clearSort:
        before = list(binding.get("sort") or [])
        if before:
            binding.pop("sort", None)
            changes.append({"field": "binding.sort", "before": before, "after": None})
    elif params.sort:
        for index, spec in enumerate(params.sort):
            if not isinstance(spec, dict):
                raise _fail(
                    f"sort[{index}] 必须是对象。",
                    reason="invalid_sort",
                    constraint="排序项必须是对象",
                    region_id=region_id,
                    op="SET_DATA_BINDING",
                )
            field_ref = str(spec.get("fieldRef") or "").strip()
            try:
                require_declared_field(region, field_ref, what=f"排序字段 sort[{index}]")
            except DisplayBindingError as exc:
                raise _fail(
                    exc.message,
                    reason=exc.reason,
                    constraint=exc.constraint,
                    region_id=region_id,
                    op="SET_DATA_BINDING",
                ) from exc
            direction = str(spec.get("direction") or "ASC").upper()
            if direction not in ("ASC", "DESC"):
                raise _fail(
                    f"sort[{index}] 的方向 {direction!r} 不合法；只能是 ASC 或 DESC。",
                    reason="invalid_sort_direction",
                    constraint="排序方向必须在协议取值域内",
                    region_id=region_id,
                    op="SET_DATA_BINDING",
                )
        before = list(binding.get("sort") or [])
        merged = before + [
            {
                "fieldRef": str(item.get("fieldRef") or "").strip(),
                "direction": str(item.get("direction") or "ASC").upper(),
                **(
                    {"nulls": str(item.get("nulls")).upper()}
                    if item.get("nulls")
                    else {}
                ),
            }
            for item in params.sort
            if isinstance(item, dict)
        ]
        binding["sort"] = merged
        if before != merged:
            changes.append({"field": "binding.sort", "before": before, "after": merged})

    # -- 展示前 N 项（B04） ----------------------------------------------
    if params.clearTopN:
        before = binding.get("topN")
        if before is not None:
            binding.pop("topN", None)
            changes.append({"field": "binding.topN", "before": before, "after": None})
    elif params.topN is not None:
        if params.topN < 0:
            raise _fail(
                f"展示前 N 项的 N 不能是负数（给了 {params.topN}）。",
                reason="invalid_topn",
                constraint="前 N 项必须是非负整数",
                region_id=region_id,
                op="SET_DATA_BINDING",
            )
        before = binding.get("topN")
        binding["topN"] = int(params.topN)
        if before != int(params.topN):
            changes.append(
                {"field": "binding.topN", "before": before, "after": int(params.topN)}
            )

    if not changes:
        raise _fail(
            "没有给出任何要改的展示绑定项（输出 / 别名 / 过滤 / 排序 / 前 N 项）。",
            reason="no_changes",
            constraint="至少要给一项要改的设置",
            region_id=region_id,
            op="SET_DATA_BINDING",
        )

    title = str(region.get("name") or region_id)
    message = f"「{title}」的展示绑定已更新（{len(changes)} 项）。"
    return CapabilityOutcome(
        ok=True,
        message=message,
        region_id=region_id,
        title=title,
        detail={
            "regionId": region_id,
            "title": title,
            "changes": changes,
            "outputRef": str(binding.get("outputRef") or ""),
            **business_facts(
                message=message,
                detail={
                    "regionId": region_id,
                    "changedCount": len(changes),
                    "fields": [item["field"] for item in changes],
                },
            ),
        },
    )


# --------------------------------------------------------------------------
# 注册
# --------------------------------------------------------------------------

register(
    EditCapability(
        id="SET_DATA_BINDING",
        family="设置统计与展示",
        title="设置展示绑定",
        description=(
            "设置这个区域**展示**用的绑定：outputRef（换绑已登记的输出）、"
            "fieldAliases（展示字段名 → 实际结果字段名）、"
            "presentationFilters（只过滤展示记录）、sort / topN（展示排序与前 N 项）。"
            "这些**只改展示**：源输出、全量总计和其他区域的输入不变；"
            "未登记的引用会被拒绝，不会自动新增跨区域数据依赖。"
        ),
        params_schema=SetDataBindingParams,
        executor=_set_data_binding,
    )
)
