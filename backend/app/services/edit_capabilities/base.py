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

import json
from dataclasses import dataclass, field
from types import UnionType
from typing import Any, Callable, Union, get_args, get_origin

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
# 入口归一（**无损**）：写法不同，但说的是同一件事
# --------------------------------------------------------------------------

#: ``target`` 里**区域 id** 的常见别名。
#:
#: 模型偶尔把区域 id 写成 ``target`` / ``id`` / ``region``（而不是契约里的
#: ``regionId``）。这类写法**说的是同一件事** —— 归一掉它，比整轮试做失败
#: 再让用户重说一句要划算得多。
_TARGET_REGION_KEYS: tuple[str, ...] = ("regionId", "region", "id", "target")

#: ``target`` 里**本次新增区块引用**的常见别名。
_TARGET_REF_KEYS: tuple[str, ...] = ("ref", "reference")


def target_parts(target: Any) -> tuple[str, str]:
    """把目标引用归一成 ``(regionId, ref)``。

    认得：``{"regionId": ...}``、``{"ref": ...}``、``{"target": ...}``、
    ``{"id": ...}``、``{"region": ...}``，以及多包一层的
    ``{"target": {"regionId": ...}}``。**不认**的是：按标题猜（契约不按标题
    猜目标，别在这里破例）。
    """
    if isinstance(target, dict):
        region_id = ""
        ref = ""
        for key in _TARGET_REGION_KEYS:
            value = target.get(key)
            if value is None:
                continue
            if isinstance(value, dict):
                nested_region, nested_ref = target_parts(value)
                region_id = region_id or nested_region
                ref = ref or nested_ref
                continue
            text = str(value).strip()
            if text:
                region_id = region_id or text
        for key in _TARGET_REF_KEYS:
            text = str(target.get(key) or "").strip()
            if text:
                ref = ref or text
        return region_id, ref
    region_id = str(getattr(target, "regionId", "") or "").strip()
    ref = str(getattr(target, "ref", "") or "").strip()
    return region_id, ref


#: 字符串 → 布尔值的**等价**写法（只认这几个，其余照样报错）。
_TRUE_WORDS: frozenset[str] = frozenset({"true", "yes", "1", "是", "开", "on"})
_FALSE_WORDS: frozenset[str] = frozenset({"false", "no", "0", "否", "关", "off"})


def as_number(value: Any) -> int | float | None:
    """**数字的等价写法**收成数字；不是数字就返回 ``None``。

    模型常把数字写成字符串（``"1"`` / ``"220"`` / ``" 16 "``）或写成
    整数值的浮点（``1.0``）。那是**同一个值**的另一种写法，不是别的值 ——
    收下它，别因为一个引号把整轮修改判死。

    真不是数字（``"两列"``、``""``、布尔）就返回 ``None``，由调用方按契约
    明确拒绝。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _is_bool_field(annotation: Any) -> bool:
    return annotation is bool


def _is_list_field(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is list:
        return True
    return any(get_origin(arg) is list for arg in get_args(annotation))


def _is_optional_list_field(annotation: Any) -> bool:
    """这个列表字段**可以不���**（``list[...] | None``）。

    「可以不���」和「给了就是这份清单」是**两件事**：前者里一个空字符串
    的意思是「没给」（按缺省处理），后者里空字符串的意思是「空清单」。
    """
    origin = get_origin(annotation)
    # ``X | None``（PEP 604）得到的是 ``types.UnionType``，不是
    # ``typing.Union`` —— 两种都要认。
    if origin is not Union and origin is not UnionType:
        return False
    args = get_args(annotation)
    return type(None) in args and any(get_origin(arg) is list for arg in args)


def tolerate_params(schema: Any, params: Any) -> Any:
    """把**常见的等价写法**归一成契约认得的形状。

    只做两件无损的事：

    1. 布尔值写成 ``"true"`` / ``"是"`` / ``1`` → 真值；
    2. 列表写成 JSON 字符串（``"[{...}]"``）→ 真列表。

    **认不出来的形状照样报错** —— 归一不是放水。归一之后仍然不合契约的，
    由 pydantic 给出带字段路径、收到类型、要求类型的可执行反馈。
    """
    if not isinstance(params, dict) or schema is None:
        return params
    fields = getattr(schema, "model_fields", None) or {}
    if not fields:
        return params
    out = dict(params)
    for name, field_info in fields.items():
        if name not in out:
            continue
        value = out[name]
        annotation = getattr(field_info, "annotation", None)
        if _is_bool_field(annotation) and not isinstance(value, bool):
            text = str(value).strip().lower()
            if text in _TRUE_WORDS:
                out[name] = True
                continue
            if text in _FALSE_WORDS:
                out[name] = False
                continue
        if isinstance(value, str) and _is_list_field(annotation):
            text = value.strip()
            if text.startswith("[") and text.endswith("]"):
                try:
                    parsed = json.loads(text)
                except ValueError:
                    parsed = None
                if isinstance(parsed, list):
                    out[name] = parsed
                continue
            if not text:
                # **空数组被写成空字符串**：模型想表达「这份清单是空的」，
                # 却写成 ``""``。现场实证（主链最后一轮）：模型提交
                # ``presentationFilters: ""`` → 契约判「必须是列表」→
                # 整轮试做失败 → 用户只看到一句「修改计划在预执行校验时
                # 没有通过，原草稿保持不变」。
                #
                # 「可以不���」的字段（``list | None``）：空字符串 = **没给**
                # （按缺省处理，**不是**空清单 —— 缺省清空会把用户没提的
                # 东西悄悄抹掉）；其余的列表字段：空字符串 = **空清单**。
                out[name] = (
                    None if _is_optional_list_field(annotation) else []
                )
            continue
        if isinstance(value, dict) and _is_list_field(annotation):
            # 列表写成**对象**：模型有时把数组序列化成 ``{"0": {...}, "1": {...}}``
            # 或直接给单个对象。两种都能**无损**还原成列表 ——
            # 还原不了就保持原样，交给契约报错（不猜、不放水）。
            items = _as_item_list(value)
            if items is not None:
                out[name] = items
    return out


def _as_item_list(value: dict[str, Any]) -> list[Any] | None:
    """对象 → 列表：能**无损**还原才还原，还原不了返回 ``None``。"""
    if not value:
        return None
    values = list(value.values())
    # 形如 {"0": {...}, "1": {...}}：按键排好序就是那份清单。
    if all(str(key).isdigit() for key in value.keys()):
        return [value[key] for key in sorted(value.keys(), key=lambda k: int(k))]
    # 形如 {"items": [...]} / {"metrics": [...]}：**包了一层壳**，壳里就是
    # 那份清单 —— 现场实证模型就是这么写的（"double-wrapped"），壳本身
    # 不带信息，拆掉它就是同一个清单。
    if len(value) == 1:
        only = values[0]
        if isinstance(only, list):
            return list(only)
    # 形如 {"总告警数": {...}, "严重告警数": {...}}：按名字还原成清单。
    if all(isinstance(item, dict) for item in values):
        return values
    # 单个对象：它就是清单里的一项。
    if any(key in value for key in ("label", "fieldRef", "id")):
        return [value]
    return None


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
            region_id, ref = target_parts(target)
        else:
            region_id = str(getattr(target, "regionId", "") or "").strip()
            ref = str(getattr(target, "ref", "") or "").strip()
        if ref:
            # ``ref`` 认得的是**本次修改现场**的引用名。模型常把**区块 id**
            # 写进 ``ref``（或两个键一起写、键名写错）：这时只要这个值在
            # 当前草稿里真实存在，就**按区块 id 认** —— 不认的话用户只会看到
            # 「引用必须属于本次修改现场」，却不知道自己只是写错了键。
            # 两条都不成立时，仍然按契约明确拒绝。
            if str(self.refs.get(str(ref).strip()) or "").strip():
                return self.resolve_reference(ref, field="target.ref", intent="ref")
            for candidate in (region_id, str(ref).strip()):
                if candidate and self.find_region(candidate) is not None:
                    return candidate
            return self.resolve_reference(ref, field="target.ref", intent="ref")
        if not region_id:
            raise CapabilityError(
                "这一步没有给出目标：请写 target.regionId（已有区块）或 target.ref（本次新增的区块）。"
                + self._region_hint(),
                step=self.step,
                op=self.op,
                target="",
                constraint="每一步都要有明确目标",
                reason="missing_target",
            )
        return self.resolve_reference(region_id, field="target.regionId", intent="region")

    def _region_hint(self) -> str:
        """当前草稿里**可选的区块**（id + 名称）。

        目标写错时，把可选项直接列出来 —— 模型下一轮就能选对，
        用户也不必再问一次「改哪一张」。
        """
        items: list[str] = []
        for region in self._live_regions():
            if not isinstance(region, dict):
                continue
            region_id = str(region.get("id") or "").strip()
            if not region_id:
                continue
            name = str(region.get("name") or "").strip()
            items.append(f"{region_id}（{name}）" if name else region_id)
        if not items:
            return ""
        return "当前草稿里的区块：" + "、".join(items) + "。"

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

def business_facts(
    *,
    message: str,
    detail: dict[str, Any] | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """能力执行结果里的**业务事实**（追踪层按这五个字段读真实业务结论）。

    ``businessOk`` 是三态：``True`` / ``False`` / ``None``（**未取得**）。
    这里写的是**执行器真的做了什么** —— 不是「没抛异常」。

    各组件包的能力统一调这一份 —— 不各写一份同义实现。
    """
    return {
        BUSINESS_OK_FIELD: True,
        BUSINESS_MESSAGE_FIELD: message,
        BUSINESS_REASON_FIELD: "",
        BUSINESS_ISSUES_FIELD: [item for item in (issues or []) if isinstance(item, dict)],
        BUSINESS_DETAIL_FIELD: dict(detail or {}),
    }


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
    # **先归一再校验**：等价写法（"true" / JSON 字符串列表）不该换来一次
    # 整轮试做失败 —— 归一只处理**无损**的写法，其余仍由契约把关。
    params = tolerate_params(capability.params_schema, params)
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
