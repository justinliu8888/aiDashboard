"""报表编辑能力模块的公共底座。

**为什么有这一层**：能力（新增区块、改展示类型、设置统计口径……）原先散落在
提示词、规则快路和执行器三处，各写一份参数说明与校验 —— 同一项能力在模型
工具里和在规则快路里为两件事。

这里把**一项能力 = 一条记录**收在一起：

    使用说明（description）
    参数与校验契约（params_schema）
    执行实现（executor）

模型工具与规则快路都从**同一个注册表**取能力，拿同一套校验与同一份执行实现。
参数规则**只定义一次**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ValidationError


# --------------------------------------------------------------------------
# 能力分组（对应需求稿「按能力分组」那张表）
# --------------------------------------------------------------------------

FAMILY_READ = "读取草稿与区块"
FAMILY_STYLE = "设置样式"
FAMILY_LAYOUT = "设置布局"
FAMILY_BLOCK = "新增与删除区块"
FAMILY_STAT = "设置统计与展示"
FAMILY_SUBMIT = "检查并提交候选"


# --------------------------------------------------------------------------
# 错误：一项能力被拒绝时要说清「哪一步、哪个目标、哪条约束、为什么」
# --------------------------------------------------------------------------

class CapabilityError(Exception):
    """能力执行失败。

    字段与需求稿要求的错误形状一一对应：

    ``step``      第几步（0 基）
    ``op``        哪项能力
    ``target``    目标引用（regionId 或 ref）
    ``constraint``被违反的约束（人类可读）
    ``reason``    机器可读的原因码
    """

    def __init__(
        self,
        message: str,
        *,
        step: int | None = None,
        op: str = "",
        target: str = "",
        constraint: str = "",
        reason: str = "invalid_operation",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.step = step
        self.op = op
        self.target = target
        self.constraint = constraint
        self.reason = reason

    def to_dict(self) -> dict[str, Any]:
        """给模型看的结构化错误（直接进工具返回值）。"""
        return {
            "step": self.step,
            "op": self.op,
            "target": self.target,
            "constraint": self.constraint,
            "reason": self.reason,
            "message": self.message,
        }


# --------------------------------------------------------------------------
# 执行现场
# --------------------------------------------------------------------------

@dataclass
class OperationContext:
    """一次能力执行能看到的全部东西。

    ``definition`` 是**隔离副本**：能力只许改它，不许碰权威草稿。
    ``refs`` 是本次修改现场里的稳定引用表（ref → regionId）。
    """

    definition: Any
    scope: Any = None
    refs: dict[str, str] = field(default_factory=dict)
    base_revision: int = 0
    preview_context: dict[str, Any] = field(default_factory=dict)
    step: int = 0
    op: str = ""
    #: 当前这一步的目标（由调用方在执行前填好）。
    current_target: Any = None
    #: 同一个位置锚点已经插过几块（保证多次插入的先后顺序）。
    counters: dict[str, int] = field(default_factory=dict)

    def resolve_target(self, target: Any) -> str:
        """把目标引用解析成真实 regionId。

        解析顺序：``ref``（本次现场新建的稳定引用）→ ``regionId``（权威草稿里的
        既有区块）。两者都认不出来就报错 —— **不按标题猜目标**。
        """
        if isinstance(target, dict):
            region_id = str(target.get("regionId") or "").strip()
            ref = str(target.get("ref") or "").strip()
        else:
            region_id = str(getattr(target, "regionId", "") or "").strip()
            ref = str(getattr(target, "ref", "") or "").strip()
        if ref:
            resolved = self.refs.get(ref)
            if not resolved:
                raise CapabilityError(
                    f"引用 {ref!r} 在本次修改现场里不存在（新增区块要先执行）。",
                    step=self.step,
                    op=self.op,
                    target=ref,
                    constraint="引用必须属于本次修改现场",
                    reason="unknown_ref",
                )
            return resolved
        if not region_id:
            raise CapabilityError(
                "这一步没有给出目标：请写 target.regionId（已有区块）或 target.ref（本次新增的区块）。",
                step=self.step,
                op=self.op,
                target="",
                constraint="每一步都要有明确目标",
                reason="missing_target",
            )
        if not self.find_region(region_id):
            raise CapabilityError(
                f"目标区块 {region_id!r} 在当前草稿里不存在。",
                step=self.step,
                op=self.op,
                target=region_id,
                constraint="目标必须存在",
                reason="unknown_region",
            )
        return region_id

    def find_region(self, region_id: str) -> dict[str, Any] | None:
        """按 id 找区块（只查顶层，与既有执行器口径一致）。"""
        for region in self.definition.regions or []:
            if str(region.get("id") or "") == region_id:
                return region
        return None


@dataclass
class CapabilityOutcome:
    """一项能力执行完的结果。"""

    ok: bool
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    #: 这次操作实际落在哪个区块上（新增时是新 id）。
    region_id: str = ""
    title: str = ""
    chart_kind: str = ""
    error: CapabilityError | None = None

    @classmethod
    def failed(cls, error: CapabilityError) -> "CapabilityOutcome":
        return cls(ok=False, message=error.message, error=error)


# --------------------------------------------------------------------------
# 能力记录
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class EditCapability:
    """一项报表编辑能力。

    ``executor`` 的签名固定：``(ctx, params) -> CapabilityOutcome``。
    参数校验在调用 executor **之前**完成（``params_schema``），
    校验不过就直接抛 :class:`CapabilityError`，不执行。
    """

    id: str
    family: str
    title: str
    description: str
    params_schema: type[BaseModel] | None
    executor: Callable[[OperationContext, BaseModel], CapabilityOutcome]
    #: 这项能力是否需要一个目标区块。
    needs_target: bool = True
    #: 是否允许目标为空（自己新建区块）。
    creates_target: bool = False


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------

_REGISTRY: dict[str, EditCapability] = {}


def register(capability: EditCapability) -> EditCapability:
    """把一项能力登记进统一入口。"""
    if capability.id in _REGISTRY:
        raise ValueError(f"能力 id 重复：{capability.id}")
    _REGISTRY[capability.id] = capability
    return capability


def get_capability(capability_id: str) -> EditCapability | None:
    return _REGISTRY.get(str(capability_id or "").strip().upper())


def all_capabilities() -> list[EditCapability]:
    return list(_REGISTRY.values())


def capabilities_by_family(family: str) -> list[EditCapability]:
    return [c for c in _REGISTRY.values() if c.family == family]


def validate_params(
    capability: EditCapability, params: Any
) -> BaseModel:
    """按能力自己的契约校验参数。

    校验不过抛 :class:`CapabilityError`（带字段名与原因），
    **不静默忽略非法参数**。
    """
    if capability.params_schema is None:
        return params  # type: ignore[return-value]
    try:
        return capability.params_schema.model_validate(params or {})
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'params'}: {err['msg']}"
            for err in exc.errors()
        )
        raise CapabilityError(
            f"{capability.title}的参数不合法：{details}",
            op=capability.id,
            constraint="参数必须符合能力契约",
            reason="invalid_params",
        ) from exc
