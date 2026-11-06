"""共享契约层：**唯一**一份「能力怎么填、填在哪」的规则。

**为什么有这一层**：同一项能力过去有三份说法 ——
能力总目录（给模型看的说明）、工具参数契约（给模型调用的 schema）、
后端校验（真正拦人的规则）。三份各写各的，改一处就漂一处：
目录说「取值由后台校验」，模型就猜；模型把包装操作当顶层能力提交，
后端只回一句「能力没注册」。

这里把三份合成**一份**：

    提交位置（submission）   —— 顶层能力，还是某个包装能力内部的子操作
    包装要求（envelope）     —— {"op": "<名字>", "params": {...}}
    参数契约（fields）       —— 字段名 / 类型 / 嵌套 / 必填 / 取值域 / 单位 / 引用
    最小示例（example）      —— 只给**格式**，不编业务内容

**规则从哪来**（全部是真实数据，不是手抄表）：

* 顶层能力：``edit_capabilities.base`` 的注册表 + 能力自己的 pydantic 参数模型；
* 包内操作：``report_presentation_service.ALLOWED_OPERATIONS`` 与
  ``_HANDLERS``（必填 / 可选键从 ``_require_keys(...)`` 的真实调用里读出来）；
* 取值域 / 范围 / 单位：``report_presentation_service`` 导出的**契约事实**
  常量（校验函数与本模块读同一份数字）。

**包装能力是自动认出来的**，不按名字写死：能力的执行器里如果调用了
``normalize_presentation_operations(...)``，它就是展示操作的**包装能力**，
被包装的载荷字段 = 那个调用传入的属性名（``params.operations``）。
新增一个包装能力时，只要它走同一个规范化入口，这里自动认得。

**导入方向**：本模块**不**在模块级 import 任何 ``app.*``（避免与
``edit_capabilities.base`` 互相 import 成环），所有真实数据都在函数里
按需取；也**不** import 目录服务与 agent 层。
"""
from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any


# --------------------------------------------------------------------------
# 提交位置
# --------------------------------------------------------------------------

#: 顶层能力：直接放在 ``submit_edit_plan.operations`` 里。
SUBMISSION_TOP_LEVEL = "top_level"

#: 包内操作：必须放在某个包装能力的载荷列表里（不能作为顶层项）。
SUBMISSION_PACKAGE_INTERNAL = "package_internal"


# --------------------------------------------------------------------------
# 参数契约
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ParamContract:
    """一个参数字段的**真实**契约。

    ``type`` 用 JSON Schema 的类型名（``string`` / ``integer`` / ``number`` /
    ``boolean`` / ``object`` / ``array``）—— 目录里的 JSON Schema、错误反馈里
    的「要求类型」都用同一套名字。
    """

    name: str
    type: str = "string"
    required: bool = False
    description: str = ""
    enum: tuple[Any, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    min_items: int | None = None
    max_items: int | None = None
    unit: str = ""
    #: 引用规则：这个字段装的是**当前草稿里的真实 id**时的说明。
    reference: str = ""
    default: Any = None
    #: 对象字段的子字段。
    properties: tuple["ParamContract", ...] = ()
    #: 数组字段的元素契约（元素是对象时才有子字段）。
    items: tuple["ParamContract", ...] = ()
    items_type: str = ""
    #: 该字段是否**自由对象**（没有声明子字段 —— 键由别的层校验）。
    free_form: bool = False

    def field(self, name: str) -> "ParamContract | None":
        """按名字取子字段。"""
        for item in self.properties:
            if item.name == name:
                return item
        return None

    def label(self) -> str:
        """给模型看的中文标签：取描述的第一句，没有描述就用字段名。"""
        text = str(self.description or "").strip()
        if not text:
            return self.name
        for sep in ("：", "（", "(", "。"):
            if sep in text:
                text = text.split(sep, 1)[0]
                break
        return text.strip() or self.name

    def to_json_schema(self) -> dict[str, Any]:
        """转成 JSON Schema 片段（目录里直接用这个渲染参数）。"""
        schema: dict[str, Any] = {"type": self.type}
        if self.description:
            schema["description"] = self.description
        if self.enum:
            schema["enum"] = list(self.enum)
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.min_length is not None:
            schema["minLength"] = self.min_length
        if self.max_length is not None:
            schema["maxLength"] = self.max_length
        if self.min_items is not None:
            schema["minItems"] = self.min_items
        if self.max_items is not None:
            schema["maxItems"] = self.max_items
        if self.default is not None:
            schema["default"] = self.default
        if self.unit:
            schema["x-unit"] = self.unit
        if self.reference:
            schema["x-reference"] = self.reference
        if self.properties:
            schema["properties"] = {
                item.name: item.to_json_schema() for item in self.properties
            }
            schema["additionalProperties"] = False
        elif self.type == "object":
            schema["additionalProperties"] = False
        if self.type == "array":
            schema["items"] = (
                {"type": self.items_type or "object"}
                if not self.items
                else {
                    "type": "object",
                    "properties": {
                        item.name: item.to_json_schema() for item in self.items
                    },
                    "additionalProperties": False,
                }
            )
        return schema

    def to_dict(self) -> dict[str, Any]:
        """机器可读形态（目录元数据 / 错误反馈用）。"""
        data: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "required": self.required,
        }
        if self.description:
            data["description"] = self.description
        if self.enum:
            data["enum"] = list(self.enum)
        if self.minimum is not None:
            data["minimum"] = self.minimum
        if self.maximum is not None:
            data["maximum"] = self.maximum
        if self.min_length is not None:
            data["minLength"] = self.min_length
        if self.max_length is not None:
            data["maxLength"] = self.max_length
        if self.min_items is not None:
            data["minItems"] = self.min_items
        if self.max_items is not None:
            data["maxItems"] = self.max_items
        if self.unit:
            data["unit"] = self.unit
        if self.reference:
            data["reference"] = self.reference
        if self.properties:
            data["properties"] = [item.to_dict() for item in self.properties]
        if self.items:
            data["items"] = [item.to_dict() for item in self.items]
        elif self.items_type:
            data["itemsType"] = self.items_type
        if self.free_form:
            data["freeForm"] = True
        return data


@dataclass(frozen=True)
class OperationContract:
    """一项能力 / 一个展示操作的完整契约。"""

    op: str
    title: str = ""
    description: str = ""
    #: 提交位置：``top_level`` 或 ``package_internal``。
    submission: str = SUBMISSION_TOP_LEVEL
    #: 包内操作：所属包装能力与载荷字段（顶层能力为空）。
    package_op: str = ""
    package_field: str = ""
    #: 包装要求：这一项在载荷里长什么样。
    envelope: dict[str, Any] = field(default_factory=dict)
    fields: tuple[ParamContract, ...] = ()
    #: 引用规则（哪些字段装的是当前草稿里的真实 id）。
    references: tuple[str, ...] = ()
    #: 真实限制（取值域 / 范围 / 单位 / 形状）。
    limits: tuple[str, ...] = ()
    available: bool = True
    source: str = ""

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.fields if item.required)

    @property
    def optional(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.fields if not item.required)

    def field(self, name: str) -> ParamContract | None:
        for item in self.fields:
            if item.name == name:
                return item
        return None

    def example(self) -> dict[str, Any]:
        """**最小结构示例**（只给格式，不编业务内容）。"""
        return dict(self.envelope)

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "title": self.title,
            "submission": self.submission,
            "packageOp": self.package_op,
            "packageField": self.package_field,
            "envelope": dict(self.envelope),
            "required": list(self.required),
            "optional": list(self.optional),
            "fields": [item.to_dict() for item in self.fields],
            "references": list(self.references),
            "limits": list(self.limits),
            "available": self.available,
            "source": self.source,
        }


@dataclass(frozen=True)
class PackageContract:
    """一个**包装能力**：它的载荷列表里装的是包内操作。"""

    op: str
    title: str = ""
    description: str = ""
    #: 载荷字段名（``operations``）。
    payload_field: str = ""
    #: 载荷字段的类型（``array``）与最小项数。
    payload_type: str = "array"
    payload_min_items: int | None = None
    #: 包内操作。
    members: tuple[OperationContract, ...] = ()
    #: 包装要求（顶层这一项长什么样）。
    envelope: dict[str, Any] = field(default_factory=dict)
    available: bool = True
    source: str = ""

    def member(self, op: str) -> OperationContract | None:
        for item in self.members:
            if item.op == op:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "title": self.title,
            "payloadField": self.payload_field,
            "payloadType": self.payload_type,
            "payloadMinItems": self.payload_min_items,
            "members": [item.op for item in self.members],
            "envelope": dict(self.envelope),
            "available": self.available,
            "source": self.source,
        }


@dataclass(frozen=True)
class Contract:
    """一份完整的共享契约。"""

    #: 顶层能力（可以直接放进 ``submit_edit_plan.operations``）。
    operations: tuple[OperationContract, ...] = ()
    #: 包装能力。
    packages: tuple[PackageContract, ...] = ()
    #: 包内操作（扁平视图，便于查找）。
    internal: tuple[OperationContract, ...] = ()

    def find(self, op: str) -> OperationContract | None:
        """按名字找能力 / 操作（顶层与包内都找）。"""
        key = str(op or "").strip().upper()
        for item in self.operations:
            if item.op == key:
                return item
        for item in self.internal:
            if item.op == key:
                return item
        return None

    def package_of(self, op: str) -> PackageContract | None:
        """某个包内操作属于哪个包装能力。"""
        key = str(op or "").strip().upper()
        for package in self.packages:
            if package.op == key:
                return package
            if package.member(key) is not None:
                return package
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operations": [item.to_dict() for item in self.operations],
            "packages": [item.to_dict() for item in self.packages],
            "internal": [item.to_dict() for item in self.internal],
        }


# --------------------------------------------------------------------------
# 类型名与中文说法
# --------------------------------------------------------------------------

#: JSON Schema 类型 → 中文说法（错误反馈里给模型看的）。
_TYPE_LABELS: dict[str, str] = {
    "string": "字符串",
    "integer": "整数",
    "number": "数字",
    "boolean": "布尔值",
    "object": "对象",
    "array": "列表",
    "null": "空值",
}


def type_label(type_name: str) -> str:
    """类型名的中文说法。"""
    return _TYPE_LABELS.get(str(type_name or "").strip(), str(type_name or "值"))


def describe_type(value: Any) -> str:
    """收到的值的 JSON 类型名（``object`` / ``array`` / ``string`` …）。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, tuple):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "object"


# --------------------------------------------------------------------------
# 最小结构示例
# --------------------------------------------------------------------------

#: 占位符 —— 示例里只出现**格式**，不出现业务内容。
_PLACEHOLDER_OPEN = "<"
_PLACEHOLDER_CLOSE = ">"


def _placeholder(spec: ParamContract) -> Any:
    """按字段契约造一个**占位**值（只给形状）。"""
    if spec.enum:
        return spec.enum[0]
    if spec.type == "string":
        return f"{_PLACEHOLDER_OPEN}{spec.name}{_PLACEHOLDER_CLOSE}"
    if spec.type == "integer":
        return 0
    if spec.type == "number":
        return 0
    if spec.type == "boolean":
        return False
    if spec.type == "array":
        if spec.items:
            return [
                {child.name: _placeholder(child) for child in spec.items}
            ]
        if spec.items_type:
            return [
                f"{_PLACEHOLDER_OPEN}{spec.name}{_PLACEHOLDER_CLOSE}"
            ]
        return []
    if spec.type == "object":
        if spec.properties:
            return {
                child.name: _placeholder(child)
                for child in spec.properties
                if child.required
            }
        return {}
    return f"{_PLACEHOLDER_OPEN}{spec.name}{_PLACEHOLDER_CLOSE}"


def _minimal_params(specs: tuple[ParamContract, ...]) -> dict[str, Any]:
    """必填字段的最小参数对象。"""
    return {
        spec.name: _placeholder(spec)
        for spec in specs
        if spec.required
    }


# --------------------------------------------------------------------------
# 展示操作：必填 / 可选键（从真实处理函数里读）
# --------------------------------------------------------------------------

def require_keys_of(func: Any) -> tuple[list[str], list[str]]:
    """从展示操作的处理函数里抽出真实的必填 / 可选参数键。

    展示操作没有 pydantic 参数模型，键是 ``_require_keys(params, 必填, 可选)``
    声明的 —— 这里从源码里读出来，不手抄。
    """
    try:
        source = inspect.getsource(func)
        tree = ast.parse(source)
    except Exception:  # noqa: BLE001
        return [], []
    module_globals = getattr(inspect.getmodule(func), "__dict__", {}) if inspect.getmodule(func) else {}

    def _keys(node: ast.AST | None) -> list[str]:
        if node is None:
            return []
        if isinstance(node, (ast.Tuple, ast.List)):
            return [str(item.value) for item in node.elts if isinstance(item, ast.Constant)]
        if isinstance(node, ast.Name):
            value = module_globals.get(node.id)
            if isinstance(value, (tuple, list)):
                return [str(item) for item in value]
        return []

    required: list[str] = []
    optional: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "_require_keys":
            continue
        req = _keys(node.args[1] if len(node.args) > 1 else None)
        opt = _keys(node.args[2] if len(node.args) > 2 else None)
        for key in req:
            if key not in required:
                required.append(key)
        for key in opt:
            if key not in optional:
                optional.append(key)
    return required, optional


# --------------------------------------------------------------------------
# 包装能力：自动认出来
# --------------------------------------------------------------------------

#: 包装能力的执行器会调用这些规范化入口 —— 认到它就是「包装能力」。
#:
#: 新增包装能力时走同一个规范化入口，这里自动认得；换入口就同步改这里。
_PACKAGE_NORMALIZERS = ("normalize_presentation_operations",)


def _package_payload_field(executor: Any) -> str:
    """包装能力的载荷字段名（从执行器的真实调用里读）。

    ``normalize_presentation_operations(params.operations, ...)`` → ``operations``。
    读不到就返回空串（不是包装能力）。
    """
    try:
        source = inspect.getsource(executor)
        tree = ast.parse(source)
    except Exception:  # noqa: BLE001
        return ""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = ""
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name not in _PACKAGE_NORMALIZERS:
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Attribute) and first.attr:
            return first.attr
        if isinstance(first, ast.Subscript) and isinstance(first.value, ast.Attribute):
            return first.value.attr
    return ""


# --------------------------------------------------------------------------
# JSON Schema → 参数契约
# --------------------------------------------------------------------------

def _resolve_ref(spec: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """展开 ``$ref``（只看本文件里的 ``$defs``）。"""
    ref = spec.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        return spec
    target = defs.get(ref.split("/")[-1])
    if not isinstance(target, dict):
        return spec
    merged = {key: value for key, value in target.items() if key != "title"}
    for key, value in spec.items():
        if key == "$ref":
            continue
        merged[key] = value
    return merged


def _spec_type(spec: dict[str, Any]) -> str:
    """取字段类型（处理 ``anyOf`` 里带 null 的写法）。"""
    declared = spec.get("type")
    if isinstance(declared, str):
        return declared
    for key in ("anyOf", "oneOf"):
        options = spec.get(key)
        if not isinstance(options, list):
            continue
        for option in options:
            if not isinstance(option, dict):
                continue
            option_type = option.get("type")
            if option_type == "null":
                continue
            if isinstance(option_type, str):
                return option_type
    return "string"


def _field_from_spec(
    name: str,
    spec: dict[str, Any],
    defs: dict[str, Any],
    required: bool,
) -> ParamContract:
    """一个 JSON Schema 字段 → 参数契约。"""
    spec = _resolve_ref(spec if isinstance(spec, dict) else {}, defs)
    for part in spec.get("allOf") or []:
        if isinstance(part, dict):
            merged = _resolve_ref(part, defs)
            spec = {**merged, **{k: v for k, v in spec.items() if k != "allOf"}}
    field_type = _spec_type(spec)
    enum_values = spec.get("enum")
    allowed = spec.get("x-allowedValues")
    enum: tuple[Any, ...] = ()
    if isinstance(enum_values, list) and enum_values:
        enum = tuple(enum_values)
    elif isinstance(allowed, list) and allowed:
        enum = tuple(allowed)
    properties: tuple[ParamContract, ...] = ()
    nested_required = set(spec.get("required") or [])
    raw_properties = spec.get("properties")
    if isinstance(raw_properties, dict):
        properties = tuple(
            _field_from_spec(key, value, defs, key in nested_required)
            for key, value in raw_properties.items()
        )
    items: tuple[ParamContract, ...] = ()
    items_type = ""
    raw_items = spec.get("items")
    if isinstance(raw_items, dict):
        raw_items = _resolve_ref(raw_items, defs)
        items_type = _spec_type(raw_items)
        raw_item_properties = raw_items.get("properties")
        if isinstance(raw_item_properties, dict):
            item_required = set(raw_items.get("required") or [])
            items = tuple(
                _field_from_spec(key, value, defs, key in item_required)
                for key, value in raw_item_properties.items()
            )
    free_form = field_type == "object" and not properties
    minimum = spec.get("minimum")
    maximum = spec.get("maximum")
    return ParamContract(
        name=name,
        type=field_type,
        required=required,
        description=str(spec.get("description") or ""),
        enum=enum,
        minimum=minimum if isinstance(minimum, (int, float)) else None,
        maximum=maximum if isinstance(maximum, (int, float)) else None,
        min_length=spec.get("minLength") if isinstance(spec.get("minLength"), int) else None,
        max_length=spec.get("maxLength") if isinstance(spec.get("maxLength"), int) else None,
        min_items=spec.get("minItems") if isinstance(spec.get("minItems"), int) else None,
        max_items=spec.get("maxItems") if isinstance(spec.get("maxItems"), int) else None,
        default=spec.get("default"),
        properties=properties,
        items=items,
        items_type=items_type,
        free_form=free_form,
    )


def _fields_from_schema(schema: dict[str, Any]) -> tuple[ParamContract, ...]:
    """一份 JSON Schema → 字段契约列表。"""
    defs = schema.get("$defs") if isinstance(schema.get("$defs"), dict) else {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return ()
    required = set(schema.get("required") or [])
    return tuple(
        _field_from_spec(key, value, defs, key in required)
        for key, value in properties.items()
    )


# --------------------------------------------------------------------------
# 展示操作的取值域（读展示服务导出的契约事实）
# --------------------------------------------------------------------------

def _presentation_module() -> Any:
    """展示服务模块（真实约束的出处）。"""
    from app.services import report_presentation_service

    return report_presentation_service


def _length_fields(
    presentation: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> tuple[ParamContract, ...]:
    """长度类字段：``{"value": 数字, "unit": "PX"}``。"""
    units = tuple(presentation.LENGTH_UNITS)
    return (
        ParamContract(
            name="value",
            type="integer",
            description="长度数值",
            minimum=minimum,
            maximum=maximum,
        ),
        ParamContract(
            name="unit",
            type="string",
            description="长度单位（只能 PX 或 PERCENT）",
            enum=units,
        ),
    )


def _arrangement_fields(presentation: Any) -> tuple[ParamContract, ...]:
    """排列参数（``arrangement``）的真实字段契约。"""
    return (
        ParamContract(
            name="mode",
            type="string",
            description="排列模式",
            enum=tuple(presentation.ALLOWED_ARRANGEMENT_MODES),
            default="GRID",
        ),
        ParamContract(
            name="columns",
            type="integer",
            description="列数",
            minimum=presentation.ARRANGEMENT_COLUMNS_MIN,
            maximum=presentation.ARRANGEMENT_COLUMNS_MAX,
            default=2,
        ),
        ParamContract(
            name="direction",
            type="string",
            description="排列方向",
            enum=tuple(presentation.ALLOWED_DIRECTIONS),
            default="HORIZONTAL",
        ),
        ParamContract(
            name="gap",
            type="object",
            description="间距（长度类：value + unit）",
            properties=_length_fields(
                presentation,
                minimum=presentation.ARRANGEMENT_GAP_MIN,
                maximum=presentation.ARRANGEMENT_GAP_MAX,
            ),
            unit="PX",
        ),
    )


def _placement_fields(presentation: Any) -> tuple[ParamContract, ...]:
    """定位参数（``placement``）的真实字段契约。"""
    return (
        ParamContract(
            name="type",
            type="string",
            description="定位方式",
            enum=tuple(presentation.ALLOWED_PLACEMENT_TYPES),
        ),
        ParamContract(
            name="column",
            type="integer",
            description="网格定位的起始列",
            minimum=presentation.PLACEMENT_COLUMN_MIN,
            maximum=presentation.PLACEMENT_COLUMN_MAX,
        ),
        ParamContract(
            name="columnSpan",
            type="integer",
            description="网格定位的列跨度",
            minimum=presentation.PLACEMENT_COLUMN_MIN,
            maximum=presentation.PLACEMENT_COLUMN_MAX,
        ),
        ParamContract(
            name="row",
            type="integer",
            description="网格定位的行",
            minimum=presentation.PLACEMENT_ROW_MIN,
            maximum=presentation.PLACEMENT_ROW_MAX,
        ),
        ParamContract(
            name="rowSpan",
            type="integer",
            description="网格定位的行跨度",
            minimum=presentation.PLACEMENT_ROW_SPAN_MIN,
            maximum=presentation.PLACEMENT_ROW_SPAN_MAX,
        ),
        ParamContract(
            name="align",
            type="string",
            description="流式定位的对齐方式",
            enum=tuple(presentation.PLACEMENT_ALIGNMENTS),
        ),
        ParamContract(
            name="width",
            type="object",
            description="流式定位的宽度（长度类：value + unit）",
            properties=_length_fields(presentation),
            unit="PX",
        ),
        ParamContract(
            name="minHeight",
            type="object",
            description="最小高度（长度类：value + unit）",
            properties=_length_fields(presentation),
            unit="PX",
        ),
        ParamContract(
            name="x",
            type="integer",
            description="固定定位的横坐标",
            minimum=presentation.PLACEMENT_ABSOLUTE_MIN,
            maximum=presentation.PLACEMENT_ABSOLUTE_MAX,
        ),
        ParamContract(
            name="y",
            type="integer",
            description="固定定位的纵坐标",
            minimum=presentation.PLACEMENT_ABSOLUTE_MIN,
            maximum=presentation.PLACEMENT_ABSOLUTE_MAX,
        ),
        ParamContract(
            name="height",
            type="integer",
            description="固定定位的高度（整数像素）",
            minimum=presentation.PLACEMENT_COLUMN_MIN,
            maximum=presentation.PLACEMENT_ABSOLUTE_MAX,
        ),
        ParamContract(
            name="zIndex",
            type="integer",
            description="固定定位的层级",
            minimum=presentation.PLACEMENT_ZINDEX_MIN,
            maximum=presentation.PLACEMENT_ZINDEX_MAX,
        ),
    )


def _style_fields(presentation: Any) -> tuple[ParamContract, ...]:
    """样式参数（``style``）的真实字段契约。"""
    from app.services.report_theme_service import (
        STYLE_SPEC_KEYS,
        THEME_MANAGED_STYLE_KEYS,
    )

    specs: dict[str, ParamContract] = {
        "fontFamily": ParamContract(
            name="fontFamily",
            type="string",
            description="字体名称",
            max_length=200,
        ),
        "fontSize": ParamContract(
            name="fontSize",
            type="object",
            description="字号（长度类：value + unit）",
            properties=_length_fields(
                presentation,
                minimum=presentation.STYLE_FONT_SIZE_MIN,
                maximum=presentation.STYLE_FONT_SIZE_MAX,
            ),
            unit="PX",
        ),
        "fontWeight": ParamContract(
            name="fontWeight",
            type="integer",
            description="字重（纯数字，不是长度）",
            minimum=presentation.STYLE_FONT_WEIGHT_MIN,
            maximum=presentation.STYLE_FONT_WEIGHT_MAX,
        ),
        "textColor": ParamContract(
            name="textColor",
            type="string",
            description="文字颜色",
            max_length=64,
        ),
        "backgroundColor": ParamContract(
            name="backgroundColor",
            type="string",
            description="背景颜色",
            max_length=64,
        ),
        "textAlign": ParamContract(
            name="textAlign",
            type="string",
            description="水平对齐",
            enum=tuple(presentation.ALLOWED_TEXT_ALIGN),
        ),
        "verticalAlign": ParamContract(
            name="verticalAlign",
            type="string",
            description="垂直对齐",
            enum=tuple(presentation.ALLOWED_VERTICAL_ALIGN),
        ),
        "lineHeight": ParamContract(
            name="lineHeight",
            type="object",
            description="行高（长度类：value + unit）",
            properties=_length_fields(
                presentation,
                minimum=presentation.STYLE_LINE_HEIGHT_MIN,
                maximum=presentation.STYLE_LINE_HEIGHT_MAX,
            ),
            unit="PX",
        ),
        "padding": ParamContract(
            name="padding",
            type="object",
            description="内边距（长度类：value + unit）",
            properties=_length_fields(presentation),
            unit="PX",
        ),
        "margin": ParamContract(
            name="margin",
            type="object",
            description="外边距（长度类：value + unit）",
            properties=_length_fields(
                presentation,
                minimum=presentation.STYLE_MARGIN_MIN,
                maximum=presentation.STYLE_MARGIN_MAX,
            ),
            unit="PX",
        ),
        "border": ParamContract(
            name="border",
            type="object",
            description="边框",
            properties=(
                ParamContract(
                    name="width",
                    type="integer",
                    description="边框宽度（纯数字像素）",
                    minimum=presentation.STYLE_BORDER_WIDTH_MIN,
                    maximum=presentation.STYLE_BORDER_WIDTH_MAX,
                ),
                ParamContract(
                    name="style",
                    type="string",
                    description="边框线型",
                    enum=tuple(presentation.ALLOWED_BORDER_STYLE),
                ),
                ParamContract(
                    name="color",
                    type="string",
                    description="边框颜色",
                    max_length=64,
                ),
            ),
        ),
        "borderRadius": ParamContract(
            name="borderRadius",
            type="integer",
            description="圆角（纯数字像素）",
            minimum=presentation.STYLE_BORDER_RADIUS_MIN,
            maximum=presentation.STYLE_BORDER_RADIUS_MAX,
        ),
        "opacity": ParamContract(
            name="opacity",
            type="number",
            description="透明度（0 到 1）",
            minimum=presentation.STYLE_OPACITY_MIN,
            maximum=presentation.STYLE_OPACITY_MAX,
        ),
    }
    # 白名单外的键不开放 —— 顺序按白名单，保持与真实校验一致。
    #
    # **顺序取 ``STYLE_SPEC_KEYS``（有序元组）**，不取
    # ``THEME_MANAGED_STYLE_KEYS``（同一批键的 ``frozenset``）：
    # 集合的迭代顺序随进程哈希种子变化，会让契约指纹在**同一份代码**
    # 下每次启动都不同 —— 目录版本就再也对不上了。
    # 键的**集合**仍以 ``THEME_MANAGED_STYLE_KEYS`` 为准（同一个出处）。
    return tuple(
        specs[key] for key in STYLE_SPEC_KEYS if key in specs and key in THEME_MANAGED_STYLE_KEYS
    )


def _format_fields(presentation: Any) -> tuple[ParamContract, ...]:
    """显示格式（``format``）的真实字段契约。"""
    specs: dict[str, ParamContract] = {
        "kind": ParamContract(
            name="kind",
            type="string",
            description="显示格式类型",
            enum=tuple(presentation.ALLOWED_FORMAT_KIND),
        ),
        "precision": ParamContract(
            name="precision",
            type="integer",
            description="小数位数",
            minimum=presentation.FORMAT_PRECISION_MIN,
            maximum=presentation.FORMAT_PRECISION_MAX,
        ),
        "unitLabel": ParamContract(
            name="unitLabel",
            type="string",
            description="单位标签（只能保持当前单位或留空）",
            max_length=64,
        ),
        "thousandSeparator": ParamContract(
            name="thousandSeparator",
            type="boolean",
            description="是否加千分位",
        ),
        "dateTimePattern": ParamContract(
            name="dateTimePattern",
            type="string",
            description="日期时间格式",
            max_length=64,
        ),
        "nullText": ParamContract(
            name="nullText",
            type="string",
            description="空值显示文本",
            max_length=64,
        ),
        "roundingMode": ParamContract(
            name="roundingMode",
            type="string",
            description="舍入方式",
            enum=tuple(presentation.ALLOWED_ROUNDING_MODE),
        ),
    }
    return tuple(specs[key] for key in presentation.ALLOWED_FORMAT_KEYS if key in specs)


def _value_object_fields(presentation: Any, key: str) -> tuple[ParamContract, ...]:
    """参数键 → 值对象的真实字段契约（认不出来的键不展开）。"""
    if key == "arrangement":
        return _arrangement_fields(presentation)
    if key == "placement":
        return _placement_fields(presentation)
    if key == "style":
        return _style_fields(presentation)
    if key == "format":
        return _format_fields(presentation)
    if key in ("webMaxWidth", "minHeight", "width"):
        return _length_fields(
            presentation,
            minimum=presentation.LENGTH_MIN,
            maximum=presentation.LENGTH_MAX,
        )
    return ()


# --------------------------------------------------------------------------
# 展示操作契约
# --------------------------------------------------------------------------

def _presentation_op_contracts() -> tuple[OperationContract, ...]:
    """展示操作（包内操作）的契约 —— 键与取值域都来自展示服务。"""
    presentation = _presentation_module()
    references = dict(presentation.PRESENTATION_REFERENCE_FIELDS)
    contracts: list[OperationContract] = []
    for op in presentation.ALLOWED_OPERATIONS:
        handler = presentation._HANDLERS.get(op)
        if handler is None:
            raise ValueError(f"展示操作 {op} 没有对应的执行函数")
        required, optional = require_keys_of(handler)
        list_fields = set(getattr(presentation, "PRESENTATION_REFERENCE_LIST_FIELDS", ()))
        fields: list[ParamContract] = []
        for key in required + optional:
            nested = _value_object_fields(presentation, key)
            is_list = key in list_fields
            fields.append(
                ParamContract(
                    name=key,
                    type="object" if nested else ("array" if is_list else "string"),
                    required=key in required,
                    description=(
                        f"{key}（对象，字段见下方）"
                        if nested
                        else f"{key}（取值见展示服务的校验）"
                    ),
                    properties=nested,
                    items_type="string" if is_list else "",
                    reference=references.get(key, ""),
                )
            )
        limits = _presentation_limits(presentation, fields)
        purpose = str(inspect.getdoc(handler) or "").strip()
        contracts.append(
            OperationContract(
                op=op,
                title=op,
                description=purpose or f"受约束的展示操作 {op}。",
                submission=SUBMISSION_PACKAGE_INTERNAL,
                envelope={"op": op, "params": _minimal_params(tuple(fields))},
                fields=tuple(fields),
                references=tuple(
                    f"{key}：{text}" for key, text in references.items() if key in set(required + optional)
                ),
                limits=limits,
                available=True,
                source=f"app/services/report_presentation_service.py::{handler.__name__}",
            )
        )
    return tuple(contracts)


def _presentation_limits(
    presentation: Any, fields: tuple[ParamContract, ...]
) -> tuple[str, ...]:
    """展示操作的真实限制（取值域 / 范围 / 单位）。"""
    limits: list[str] = []
    for spec in fields:
        if spec.enum:
            limits.append(
                f"{spec.name} 取值域：{'、'.join(str(item) for item in spec.enum)}"
            )
        if spec.minimum is not None or spec.maximum is not None:
            low = "-∞" if spec.minimum is None else spec.minimum
            high = "∞" if spec.maximum is None else spec.maximum
            limits.append(f"{spec.name} 取值范围：{low} 到 {high}")
        for child in spec.properties:
            if child.enum:
                limits.append(
                    f"{spec.name}.{child.name} 取值域："
                    f"{'、'.join(str(item) for item in child.enum)}"
                )
            if child.minimum is not None or child.maximum is not None:
                low = "-∞" if child.minimum is None else child.minimum
                high = "∞" if child.maximum is None else child.maximum
                limits.append(f"{spec.name}.{child.name} 取值范围：{low} 到 {high}")
            for grand in child.properties:
                if grand.enum:
                    limits.append(
                        f"{spec.name}.{child.name}.{grand.name} 取值域："
                        f"{'、'.join(str(item) for item in grand.enum)}"
                    )
                if grand.minimum is not None or grand.maximum is not None:
                    low = "-∞" if grand.minimum is None else grand.minimum
                    high = "∞" if grand.maximum is None else grand.maximum
                    limits.append(
                        f"{spec.name}.{child.name}.{grand.name} 取值范围：{low} 到 {high}"
                    )
    return tuple(limits)


# --------------------------------------------------------------------------
# 顶层能力契约
# --------------------------------------------------------------------------

def _capability_contracts() -> tuple[tuple[OperationContract, ...], tuple[PackageContract, ...]]:
    """顶层能力 + 包装能力的契约。"""
    from app.services.edit_capabilities.base import all_capabilities

    operations: list[OperationContract] = []
    packages: list[PackageContract] = []
    for capability in all_capabilities():
        schema: dict[str, Any] = {}
        if capability.params_schema is not None:
            schema = capability.params_schema.model_json_schema()
        fields = _fields_from_schema(schema)
        payload_field = _package_payload_field(capability.executor)
        example_params = _minimal_params(fields)
        envelope = {
            "op": capability.id,
            "target": {"regionId": f"{_PLACEHOLDER_OPEN}regionId{_PLACEHOLDER_CLOSE}"},
            "params": example_params,
        }
        if not capability.needs_target:
            envelope.pop("target", None)
        if payload_field:
            # 包装能力：载荷字段必须是「非空的对象列表」。
            members = _presentation_op_contracts()
            package = PackageContract(
                op=capability.id,
                title=capability.title,
                description=capability.description,
                payload_field=payload_field,
                payload_type="array",
                payload_min_items=1,
                members=members,
                envelope={"op": capability.id, "params": {payload_field: [{"op": members[0].op, "params": members[0].envelope.get("params", {})}]}} if members else envelope,
                available=True,
                source=(
                    "app/services/edit_capabilities/base.py::register"
                ),
            )
            packages.append(package)
            operations.append(
                OperationContract(
                    op=capability.id,
                    title=capability.title,
                    description=capability.description,
                    submission=SUBMISSION_TOP_LEVEL,
                    envelope=package.envelope,
                    fields=_enrich_payload_field(fields, payload_field, members),
                    references=(),
                    limits=tuple(_capability_limits(capability, fields)),
                    available=True,
                    source="app/services/edit_capabilities/base.py::register",
                )
            )
            continue
        operations.append(
            OperationContract(
                op=capability.id,
                title=capability.title,
                description=capability.description,
                submission=SUBMISSION_TOP_LEVEL,
                envelope=envelope,
                fields=fields,
                references=(),
                limits=tuple(_capability_limits(capability, fields)),
                available=True,
                source="app/services/edit_capabilities/base.py::register",
            )
        )
    return tuple(operations), tuple(packages)


def _enrich_payload_field(
    fields: tuple[ParamContract, ...],
    payload_field: str,
    members: tuple[OperationContract, ...],
) -> tuple[ParamContract, ...]:
    """把包装能力的载荷字段写成「非空的对象列表」的真实形状。

    列表项的形状：``{"op": <允许的展示操作名>, "params": {...}}`` ——
    ``op`` 的取值域就是包装能力真正接受的那些操作名。
    """
    enriched: list[ParamContract] = []
    for spec in fields:
        if spec.name != payload_field:
            enriched.append(spec)
            continue
        enriched.append(
            ParamContract(
                name=spec.name,
                type="array",
                required=spec.required,
                description=spec.description,
                min_items=1,
                items=(
                    ParamContract(
                        name="op",
                        type="string",
                        required=True,
                        description="展示操作名（包装能力里允许的那些）",
                        enum=tuple(item.op for item in members),
                    ),
                    ParamContract(
                        name="params",
                        type="object",
                        required=False,
                        description="该展示操作的参数对象",
                        free_form=True,
                    ),
                ),
                items_type="object",
            )
        )
    return tuple(enriched)


def _capability_limits(capability: Any, fields: tuple[ParamContract, ...]) -> list[str]:
    """顶层能力的真实限制（取值域 / 范围 / 必填 / 目标要求）。"""
    limits: list[str] = []
    for spec in fields:
        if spec.enum:
            limits.append(
                f"{spec.name} 取值域：{'、'.join(str(item) for item in spec.enum)}"
            )
        if spec.minimum is not None or spec.maximum is not None:
            low = "-∞" if spec.minimum is None else spec.minimum
            high = "∞" if spec.maximum is None else spec.maximum
            limits.append(f"{spec.name} 取值范围：{low} 到 {high}")
        if spec.min_length is not None:
            limits.append(f"{spec.name} 至少 {spec.min_length} 个字符")
        if spec.min_items is not None:
            limits.append(f"{spec.name} 至少 {spec.min_items} 项")
        if spec.properties:
            for child in spec.properties:
                if child.enum:
                    limits.append(
                        f"{spec.name}.{child.name} 取值域："
                        f"{'、'.join(str(item) for item in child.enum)}"
                    )
    if capability.needs_target:
        limits.append(
            "每一步都要有明确目标：target.regionId（草稿里已有区块）"
            "或 target.ref（本次新增区块的引用名）"
        )
    else:
        limits.append("不需要目标（自己新建区块 / 整批作用于草稿）")
    if capability.creates_target:
        limits.append("新增的区块可用 params.ref 起稳定引用名，后续步骤用 target.ref 指它")
    return limits


# --------------------------------------------------------------------------
# 契约入口
# --------------------------------------------------------------------------

@lru_cache(maxsize=1)
def build_contract() -> Contract:
    """构造**完整**的共享契约（真实注册项 → 契约）。

    纯函数：不落盘、不改任何注册项。

    **按进程缓存**：契约来自**已加载**的注册项，进程不重载就不会变 ——
    与目录版本的语义一致（源码改了但没重载 → 契约不变 → 版本不变）。
    """
    operations, packages = _capability_contracts()
    internal: list[OperationContract] = []
    for package in packages:
        internal.extend(package.members)
    return Contract(
        operations=operations,
        packages=packages,
        internal=tuple(internal),
    )


def contract_for(op: str) -> OperationContract | None:
    """按名字找一项能力 / 操作的契约。"""
    return build_contract().find(op)


def package_for(op: str) -> PackageContract | None:
    """按名字找包装能力（含「某个包内操作属于哪个包装能力」）。"""
    return build_contract().package_of(op)


def allowed_operations() -> tuple[str, ...]:
    """展示服务的**真实**操作白名单（可导入数据，不是手抄的）。"""
    return tuple(_presentation_module().ALLOWED_OPERATIONS)


def minimal_example(op: str) -> dict[str, Any]:
    """某项能力 / 操作的**最小结构示例**（只给格式）。"""
    contract = contract_for(op)
    if contract is None:
        return {}
    return contract.example()


# --------------------------------------------------------------------------
# 错误反馈：同一份契约生成
# --------------------------------------------------------------------------

def submission_location(op: str) -> str:
    """这一项**该填在哪**（人话 + 路径）。"""
    contract = build_contract()
    target = contract.find(op)
    if target is None:
        return "submit_edit_plan 的 operations 里"
    package = contract.package_of(target.op)
    if target.submission == SUBMISSION_TOP_LEVEL:
        if package is not None:
            return (
                f"submit_edit_plan 的 operations 里 {package.op} 这一项的 "
                f"params.{package.payload_field} 里"
                '（每一项形如 {"op": "<展示操作名>", "params": {...}}）'
            )
        return (
            "submit_edit_plan 的 operations 里直接给一项 "
            '{"op": "<能力名>", "target": {...}, "params": {...}}'
        )
    package_op = package.op if package else ""
    package_field = target.package_field or (package.payload_field if package else "")
    return (
        f"submit_edit_plan 的 operations 里 {package_op} 的 params.{package_field} 里"
        f'（每项形如 {{"op": "{target.op}", "params": {{...}}}}）'
    )


def format_type_message(
    label: str,
    required_type: str,
    received_type: str,
) -> str:
    """人话反馈：**保持原方案，只改这一处格式**。

    文案由**类型**生成（契约给出要求类型与收到的类型），不是逐案例手写。
    """
    return (
        f"{label}必须是{type_label(required_type)}。"
        f"你提交的是一个{type_label(received_type)}。"
        "请保留原方案，只修正这一处格式。"
    )


def format_min_items_message(label: str, min_items: int, count: int) -> str:
    """列表太短 —— 说清要几项、给了几项。"""
    return (
        f"{label}至少要有 {min_items} 项。"
        f"你提交的是 {count} 项。"
        "请保留原方案，只修正这一处格式。"
    )


def field_error(
    *,
    op: str,
    field_path: str,
    received: Any,
    required_type: str,
    label: str = "",
    reason: str = "invalid_params",
    constraint: str = "",
) -> dict[str, Any]:
    """造一条**结构化**的格式错误（错误反馈与目录同源）。

    返回的键：``op`` / ``fieldPath`` / ``receivedType`` / ``requiredType`` /
    ``submissionLocation`` / ``example`` / ``message`` / ``reason`` / ``constraint``。
    """
    received_type = describe_type(received)
    contract = build_contract()
    target = contract.find(op)
    if not label:
        label = field_path or op
    example: dict[str, Any] = {}
    if target is not None:
        example = target.example()
    return {
        "op": op,
        "fieldPath": field_path,
        "receivedType": received_type,
        "requiredType": required_type,
        "submissionLocation": submission_location(op),
        "example": example,
        "message": format_type_message(label, required_type, received_type),
        "reason": reason,
        "constraint": constraint or f"{field_path} 必须是{type_label(required_type)}",
    }


# --------------------------------------------------------------------------
# 包装载荷的结构预检（形状规则来自同一份契约）
# --------------------------------------------------------------------------

def _member_label(payload_label: str) -> str:
    """「外观修改包里的操作名单」→「外观修改包里的每一项」。"""
    head = str(payload_label or "").split("里的", 1)[0].strip()
    return f"{head}里的每一项" if head else "每一项"


def format_unknown_op_message(given: str, allowed: tuple[str, ...]) -> str:
    """包里没有这个操作名 —— 说清可用的是哪些。"""
    names = "、".join(allowed) if allowed else "（无）"
    return (
        f"外观修改包里没有这个操作名：{given or '(空)'}。"
        f"可用的是：{names}。"
        "请保留原方案，只修正这一处。"
    )


def format_unknown_key_message(given: str, allowed: tuple[str, ...]) -> str:
    """这个参数键不开放 —— 说清可用的是哪些。"""
    names = "、".join(allowed) if allowed else "（无）"
    return (
        f"不支持的参数：{given}。"
        f"可用的是：{names}。"
        "请保留原方案，只修正这一处。"
    )


def check_package_members(
    package: PackageContract, payload: Any
) -> list[dict[str, Any]]:
    """按共享契约检查包装载荷的**每一项**（形状规则，不碰取值域）。

    只查展示服务**同样会拒**的形状：项必须是对象、``op`` 必须在白名单里、
    ``params`` 必须是对象、参数键必须在该 op 的允许键里 ——
    这些键来自真实处理函数的 ``_require_keys(...)``，不是手抄的。

    返回结构化错误列表（空列表 = 通过）。取值域仍由展示服务判定。
    """
    errors: list[dict[str, Any]] = []
    if not isinstance(payload, list):
        return errors
    payload_field = package.payload_field or "operations"
    payload_spec = None
    contract = build_contract()
    package_op = contract.find(package.op)
    if package_op is not None:
        payload_spec = package_op.field(payload_field)
    payload_label = payload_spec.label() if payload_spec is not None else payload_field
    member_label = _member_label(payload_label)
    allowed_ops = tuple(item.op for item in package.members)
    example = package.envelope.get("params", {}).get(payload_field) if isinstance(
        package.envelope.get("params"), dict
    ) else None

    def _base(field_path: str, received: Any, required_type: str) -> dict[str, Any]:
        return {
            "op": package.op,
            "fieldPath": field_path,
            "receivedType": describe_type(received),
            "requiredType": required_type,
            "submissionLocation": submission_location(package.op),
            "example": example if isinstance(example, dict) else {},
            "reason": "invalid_params",
            "constraint": f"{field_path} 必须是{type_label(required_type)}",
        }

    for index, member in enumerate(payload):
        base_path = f"{payload_field}[{index}]"
        if not isinstance(member, dict):
            detail = _base(base_path, member, "object")
            detail["message"] = format_type_message(member_label, "object", describe_type(member))
            errors.append(detail)
            continue
        op_name = str(member.get("op") or "").strip()
        if not op_name:
            detail = _base(f"{base_path}.op", member.get("op"), "string")
            detail["message"] = (
                f"{member_label}都要写 op（展示操作名）。"
                f"可用的是：{'、'.join(allowed_ops)}。"
                "请保留原方案，只修正这一处。"
            )
            errors.append(detail)
            continue
        member_contract = package.member(op_name)
        if member_contract is None:
            detail = _base(f"{base_path}.op", op_name, "string")
            detail["message"] = format_unknown_op_message(op_name, allowed_ops)
            detail["constraint"] = "op 必须是展示服务白名单里的操作名"
            errors.append(detail)
            continue
        params = member.get("params")
        if params is not None and not isinstance(params, dict):
            detail = _base(f"{base_path}.params", params, "object")
            detail["message"] = format_type_message(
                f"{member_label}的 params 参数", "object", describe_type(params)
            )
            errors.append(detail)
            continue
        if isinstance(params, dict):
            allowed_keys = tuple(member_contract.required) + tuple(member_contract.optional)
            for key in params:
                if key not in allowed_keys:
                    detail = _base(f"{base_path}.params.{key}", key, "string")
                    detail["message"] = format_unknown_key_message(key, allowed_keys)
                    detail["constraint"] = "参数键必须在该展示操作允许的键里"
                    errors.append(detail)
    return errors


def resolve_field(path: str, op: str = "", member_op: str = "") -> ParamContract | None:
    """按字段路径找参数契约（如 ``operations``、``arrangement.columns``）。"""
    resolved = resolve_path(path, op=op, member_op=member_op)
    return resolved.get("spec") if resolved else None


def _path_segments(path: str) -> list[Any]:
    """字段路径 → 段序列（下标保留成整数）。

    ``operations[0].params.arrangement.columns``
    → ``["operations", 0, "params", "arrangement", "columns"]``
    """
    segments: list[Any] = []
    for part in str(path or "").split("."):
        name = part
        while "[" in name and name.endswith("]"):
            head, _, rest = name.partition("[")
            index, _, tail = rest.partition("]")
            if head:
                segments.append(head)
            try:
                segments.append(int(index))
            except ValueError:
                segments.append(index)
            name = tail
        if name:
            segments.append(name)
    return segments


def resolve_path(path: str, op: str = "", member_op: str = "") -> dict[str, Any] | None:
    """按字段路径解析出「要求的类型」与「人话标签」。

    返回 ``{"spec": ParamContract | None, "type": str, "label": str}``；
    路径认不出来时返回 ``None``。
    """
    contract = build_contract()
    target = contract.find(member_op) if member_op else contract.find(op)
    if target is None:
        return None
    segments = _path_segments(path)
    if not segments:
        return None
    current: ParamContract | None = None
    label = ""
    for segment in segments:
        if isinstance(segment, int):
            if current is None:
                return None
            if current.type != "array":
                return None
            # ``items`` 是**列表项对象的子字段** —— 列表项本身是对象。
            element_type = "object" if current.items else (
                current.items_type or "object"
            )
            label = _element_label(current, label)
            current = ParamContract(
                name=current.name,
                type=element_type,
                description=label,
                required=current.required,
            )
            continue
        if segment == "params":
            continue
        if current is None:
            current = target.field(segment)
        else:
            current = current.field(segment)
        if current is None:
            return None
        label = current.label()
    if current is None:
        return None
    return {"spec": current, "type": current.type, "label": label}


def _element_label(parent: ParamContract, parent_label: str) -> str:
    """数组里那一项的中文标签。"""
    contract = build_contract()
    for package in contract.packages:
        for operation in contract.operations:
            if operation.op != package.op:
                continue
            spec = operation.field(package.payload_field)
            if spec is not None and spec.name == parent.name:
                return _member_label(parent_label)
    return f"{parent_label}里的一项"


__all__ = [
    "Contract",
    "OperationContract",
    "PackageContract",
    "ParamContract",
    "SUBMISSION_PACKAGE_INTERNAL",
    "SUBMISSION_TOP_LEVEL",
    "allowed_operations",
    "build_contract",
    "check_package_members",
    "contract_for",
    "describe_type",
    "field_error",
    "format_type_message",
    "format_unknown_key_message",
    "format_unknown_op_message",
    "minimal_example",
    "package_for",
    "require_keys_of",
    "resolve_field",
    "resolve_path",
    "submission_location",
    "type_label",
]
