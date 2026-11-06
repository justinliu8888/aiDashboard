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
# 业务事实：执行实现 → 试做层 → 追踪事件（**同一份字段名**）
# --------------------------------------------------------------------------

#: 业务上到底成没成：``True`` / ``False`` / ``None``（**未取得**）。
#:
#: ``None`` 是「没拿到业务结论」，**不许拿框架状态**（执行器没抛异常）
#: 冒充业务成功。
BUSINESS_OK_FIELD = "businessOk"
#: 真实业务说明（人话）。
BUSINESS_MESSAGE_FIELD = "businessMessage"
#: 真实业务原因码（业务服务给的原因，不是框架码）。
BUSINESS_REASON_FIELD = "businessReason"
#: 结构化业务问题（列表）。
BUSINESS_ISSUES_FIELD = "businessIssues"
#: 额外的结构化业务明细。
BUSINESS_DETAIL_FIELD = "businessDetail"


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

    **格式类错误**额外带结构化定位 —— 全部由共享契约
    （``edit_capabilities.contract``）生成，模型据此只改一处：

    ``field_path``         出错字段的路径（如 ``operations``）
    ``received_type``      收到的类型（``object`` / ``array`` / ``string`` …）
    ``required_type``      要求的类型
    ``submission_location``这一项**该填在哪**（哪个包装能力 / 哪个字段）
    ``example``            最小结构示例（只给格式，不编业务内容）

    **业务事实**（``business``）是增量字段：业务服务给出的真实原因、
    失败说明与结构化问题。既有字段的语义**不变**。
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
        field_path: str = "",
        received_type: str = "",
        required_type: str = "",
        submission_location: str = "",
        example: dict[str, Any] | None = None,
        business: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.step = step
        self.op = op
        self.target = target
        self.constraint = constraint
        self.reason = reason
        self.field_path = field_path
        self.received_type = received_type
        self.required_type = required_type
        self.submission_location = submission_location
        self.example = dict(example or {})
        #: **业务事实**（业务服务给的真实结论）：``businessOk`` /
        #: ``businessMessage`` / ``businessReason`` / ``businessIssues`` /
        #: ``businessDetail``。没有业务结论时是空字典（= 未取得）。
        self.business = dict(business or {})

    def to_dict(self) -> dict[str, Any]:
        """给模型看的结构化错误（直接进工具返回值）。"""
        return {
            "step": self.step,
            "op": self.op,
            "target": self.target,
            "constraint": self.constraint,
            "reason": self.reason,
            "message": self.message,
            "fieldPath": self.field_path,
            "receivedType": self.received_type,
            "requiredType": self.required_type,
            "submissionLocation": self.submission_location,
            "example": dict(self.example),
            "business": dict(self.business),
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

        两条路都走**同一份引用契约**（:meth:`resolve_reference`）：
        正式 regionId 与本次现场的临时 ref 用同一套判定。
        """
        if isinstance(target, dict):
            region_id = str(target.get("regionId") or "").strip()
            ref = str(target.get("ref") or "").strip()
        else:
            region_id = str(getattr(target, "regionId", "") or "").strip()
            ref = str(getattr(target, "ref", "") or "").strip()
        if ref:
            return self.resolve_reference(ref, field="target.ref", intent="ref")
        if not region_id:
            raise CapabilityError(
                "这一步没有给出目标：请写 target.regionId（已有区块）或 target.ref（本次新增的区块）。",
                step=self.step,
                op=self.op,
                target="",
                constraint="每一步都要有明确目标",
                reason="missing_target",
            )
        return self.resolve_reference(region_id, field="target.regionId", intent="region")

    def find_region(self, region_id: str) -> dict[str, Any] | None:
        """按 id 找区块（**递归**：组里的卡片也找得到）。

        用的是**展示服务同一套**递归查找（容器下的 ``children`` 也查），
        但传的是**活区域树** —— 能力执行器要就地改它。
        """
        from app.services.report_presentation_service import find_region as _find_region

        return _find_region({"regions": self._live_regions()}, region_id)

    def _live_regions(self) -> list[dict[str, Any]]:
        """当前定义里的**活**区域列表（不是副本）。"""
        definition = self.definition
        if isinstance(definition, dict):
            regions = definition.get("regions")
        else:
            regions = getattr(definition, "regions", None)
        return regions if isinstance(regions, list) else []

    # -- 引用解析（**一份契约**） ----------------------------------------

    def resolve_reference(
        self, value: Any, *, field: str = "", intent: str = "auto"
    ) -> str:
        """把一个**引用值**解析成真实区块 id。

        契约只有两条路：

        1. 值是**本次修改现场的临时 ref**（``refs`` 表：ref → 真实 id）；
        2. 值是**当前草稿里真实存在的区块 id**（**含组内**的卡片）。

        * ``intent="ref"``：只认第 1 条（调用方明确说这是引用名）；
        * ``intent="region"``：先认第 2 条，再回退到第 1 条
          （模型把引用名写进 ``regionId`` 时也认得出来）；
        * ``intent="auto"``：两条都认；**两条都成立** → 说不清指哪一个
          （``ambiguous_reference``）；**都不成立** → ``unknown_ref``。

        **不按标题猜，也不猜 id**：别的请求的引用名在这里一律不认。
        """
        text = str(value or "").strip()
        if not text:
            raise CapabilityError(
                f"引用字段 {field or '（未标注）'} 是空的。",
                step=self.step,
                op=self.op,
                target="",
                constraint="引用不能为空",
                reason="missing_reference",
            )
        as_ref = str(self.refs.get(text) or "")
        as_region = self.find_region(text)
        if intent == "ref":
            if not as_ref:
                raise CapabilityError(
                    f"引用 {text!r} 在本次修改现场里不存在（新增区块要先执行）。",
                    step=self.step,
                    op=self.op,
                    target=text,
                    constraint="引用必须属于本次修改现场",
                    reason="unknown_ref",
                )
            if self.find_region(as_ref) is None:
                raise CapabilityError(
                    f"引用 {text!r} 指向的区块已经不在当前草稿里了。",
                    step=self.step,
                    op=self.op,
                    target=text,
                    constraint="引用必须指向当前草稿里真实存在的区块",
                    reason="unknown_ref",
                )
            return as_ref
        if intent == "region":
            if as_region is not None:
                return text
            if as_ref:
                return as_ref
            raise CapabilityError(
                f"目标区块 {text!r} 在当前草稿里不存在。",
                step=self.step,
                op=self.op,
                target=text,
                constraint="目标必须存在",
                reason="unknown_region",
            )
        # intent == "auto"：两条路都认，认不出来就说清楚是哪一条不成立。
        if as_ref and as_region is not None:
            raise CapabilityError(
                f"引用 {text!r} 既是一个真实区块 id，又是本次修改现场里的引用名；"
                "说不清指哪一个，请换一种写法。",
                step=self.step,
                op=self.op,
                target=text,
                constraint="一个引用只能指向一个区块",
                reason="ambiguous_reference",
            )
        if as_ref:
            return as_ref
        if as_region is not None:
            return text
        raise CapabilityError(
            f"引用 {text!r} 在当前草稿与本次修改现场里都不存在："
            "它既不是当前草稿里的区块 id，也不是本次修改里 ADD_SECTION 起过的引用名"
            "（别的请求的引用在这里不可用）。",
            step=self.step,
            op=self.op,
            target=text,
            constraint="引用必须是当前草稿里的真实区块 id，或本次修改现场的引用名",
            reason="unknown_ref",
        )

    def resolve_presentation_references(
        self, operations: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """把一批展示操作里的**引用字段**按本次现场的引用表解析一遍。

        引用字段的名字来自**共享契约**（``edit_capabilities.contract``）——
        这里**不另写一份字段表**。

        规则与 :meth:`resolve_reference` 是**同一套**：

        * 值是**本次修改现场的临时 ref** → 换成真实区块 id；
        * 值在当前草稿里真实存在（含组内）→ 原样保留；
        * 一个值**既**是真实 id **又**是本次的 ref → 说不清指哪个，
          报 ``ambiguous_reference``；
        * 两者都不是 → **原样交给展示服务判**（新建的 id 本来就不存在，
          这里不能判死；展示服务会给真实的业务原因）。

        引用表是**本次执行现场**的：试做与用户确认各自跑一遍，
        各自用自己的引用表 —— 不共用、不外泄。
        """
        from app.services.edit_capabilities import contract as shared

        shared_contract = shared.build_contract()
        resolved: list[dict[str, Any]] = []
        for raw in operations or []:
            if not isinstance(raw, dict):
                resolved.append(raw)
                continue
            op_name = str(raw.get("op") or "").strip().upper()
            params = raw.get("params")
            spec = shared_contract.find(op_name)
            if not isinstance(params, dict) or spec is None:
                resolved.append(raw)
                continue
            reference_fields = [item.name for item in spec.fields if item.reference]
            if not reference_fields:
                resolved.append(raw)
                continue
            new_params = dict(params)
            for name in reference_fields:
                if name not in new_params:
                    continue
                value = new_params[name]
                if isinstance(value, list):
                    new_params[name] = [
                        self._resolve_package_reference(item, op=op_name, field=name)
                        for item in value
                    ]
                elif isinstance(value, str):
                    new_params[name] = self._resolve_package_reference(
                        value, op=op_name, field=name
                    )
            resolved.append({**raw, "params": new_params})
        return resolved

    def _resolve_package_reference(self, value: Any, *, op: str, field: str) -> str:
        """展示包里一个引用值 → 真实区块 id（只换**本次现场的临时引用名**）。"""
        text = str(value or "").strip()
        if not text:
            return text
        as_ref = str(self.refs.get(text) or "")
        if not as_ref:
            # 不是本次现场的引用名 → 交给展示服务按业务口径判。
            return text
        if self.find_region(text) is not None:
            raise CapabilityError(
                f"展示操作 {op} 的 {field} 里的 {text!r} 既是一个真实区块 id，"
                "又是本次修改现场里的引用名；说不清指哪一个。",
                step=self.step,
                op=self.op or op,
                target=text,
                constraint="一个引用只能指向一个区块",
                reason="ambiguous_reference",
            )
        return as_ref

    # -- 共享契约（读同一份规则，不另写说明） --------------------------

    def capability_contract(self) -> Any:
        """当前这一步的能力契约（``None`` = 没登记这项能力）。"""
        from app.services.edit_capabilities import contract as shared

        return shared.contract_for(self.op)

    def submission_location(self) -> str:
        """当前这一步**该填在哪**（顶层 / 哪个包装能力的哪个字段）。"""
        from app.services.edit_capabilities import contract as shared

        return shared.submission_location(self.op)

    def contract_example(self) -> dict[str, Any]:
        """当前这一步的**最小结构示例**（只给格式）。"""
        from app.services.edit_capabilities import contract as shared

        contract = self.capability_contract()
        return contract.example() if contract is not None else {}


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

    错误反馈与能力总目录**同源**：字段路径、收到的类型、要求的类型、
    该填在哪、最小结构示例，都由共享契约
    （``edit_capabilities.contract``）生成 —— 目录改了，这里跟着改。
    """
    if capability.params_schema is None:
        return params  # type: ignore[return-value]
    try:
        validated = capability.params_schema.model_validate(params or {})
    except ValidationError as exc:
        raise _params_error(capability, exc) from exc
    _check_package_payload(capability, validated)
    return validated


#: pydantic 的错误类型 → JSON 类型名（契约里认不出字段时的兜底）。
_ERROR_TYPE_TO_JSON: dict[str, str] = {
    "list_type": "array",
    "dict_type": "object",
    "model_type": "object",
    "string_type": "string",
    "int_type": "integer",
    "float_type": "number",
    "bool_type": "boolean",
    "too_short": "array",
    "too_long": "array",
    "greater_than": "number",
    "less_than": "number",
    "greater_than_equal": "number",
    "less_than_equal": "number",
}


def _format_loc(loc: Any) -> str:
    """pydantic 的出错位置 → 字段路径（``operations[0].params.arrangement``）。"""
    text = ""
    for part in loc or ():
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            text = f"{text}.{part}" if text else str(part)
    return text


def _params_error(capability: EditCapability, exc: ValidationError) -> CapabilityError:
    """把 pydantic 的校验错误翻译成**可执行**的反馈。"""
    from app.services.edit_capabilities import contract as shared

    errors = exc.errors()
    first = errors[0] if errors else {}
    path = _format_loc(first.get("loc"))
    received = first.get("input")
    resolved = shared.resolve_path(path, capability.id)
    spec = resolved.get("spec") if resolved else None
    required_type = (
        str(resolved.get("type"))
        if resolved
        else _ERROR_TYPE_TO_JSON.get(str(first.get("type") or ""), "object")
    )
    label = str(resolved.get("label") or "") if resolved else ""
    detail = shared.field_error(
        op=capability.id,
        field_path=path,
        received=received,
        required_type=required_type,
        label=label,
    )
    if not resolved:
        # 契约里认不出这个字段 —— 退回 pydantic 的原始说明（仍带定位）。
        details = "; ".join(
            f"{_format_loc(err.get('loc')) or 'params'}: {err.get('msg')}"
            for err in errors
        )
        message = f"{capability.title}的参数不合法：{details}"
    elif str(first.get("type") or "") == "too_short" and spec is not None and spec.min_items:
        # 列表太短 —— 说清要几项、给了几项（比「类型不对」准确）。
        count = len(received) if isinstance(received, (list, tuple)) else 0
        message = shared.format_min_items_message(label, spec.min_items, count)
    else:
        message = str(detail.get("message") or "")
    return CapabilityError(
        message,
        op=capability.id,
        constraint=str(detail.get("constraint") or ""),
        reason="invalid_params",
        field_path=path,
        received_type=str(detail.get("receivedType") or ""),
        required_type=required_type,
        submission_location=str(detail.get("submissionLocation") or ""),
        example=detail.get("example") if isinstance(detail.get("example"), dict) else {},
    )


def _check_package_payload(capability: EditCapability, validated: BaseModel) -> None:
    """包装能力的载荷：**形状**按共享契约预检一遍。

    查的规则与展示服务**同一份**（op 白名单、每个 op 的允许键）：
    展示服务仍然负责取值域与执行，这里只把「填在哪、填成什么样」
    的错误提前说清楚（带字段路径）。
    """
    from app.services.edit_capabilities import contract as shared

    package = shared.build_contract().package_of(capability.id)
    if package is None or package.op != capability.id:
        return
    payload = getattr(validated, package.payload_field, None)
    if payload is None:
        return
    problems = shared.check_package_members(package, payload)
    if not problems:
        return
    first = problems[0]
    raise CapabilityError(
        str(first.get("message") or ""),
        op=capability.id,
        constraint=str(first.get("constraint") or ""),
        reason=str(first.get("reason") or "invalid_params"),
        field_path=str(first.get("fieldPath") or ""),
        received_type=str(first.get("receivedType") or ""),
        required_type=str(first.get("requiredType") or ""),
        submission_location=str(first.get("submissionLocation") or ""),
        example=first.get("example") if isinstance(first.get("example"), dict) else {},
    )
