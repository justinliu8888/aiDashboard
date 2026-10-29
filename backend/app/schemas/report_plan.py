"""ReportPlan / PlanCandidate：#19「先确认，再生成」的文字方案领域类型。

契约来源：`05. 并行分发/00. 协调与冻结契约.md` 的「冻结的数据契约 v1」。

本模块的边界（与冻结契约一致）：

    - 方案是**设计意图**，不是可执行定义：不含真实数值、数据源绑定、
      SQL、脚本或凭证。
    - 模型不分配提交版本，也不改已有板块 ID；ID 与 revision 由领域服务生成。
    - 本模块只定义类型与校验，不调用模型、不写库、不生成报表，
      也不决定一次提交是否最终成功。
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ComponentType(str, Enum):
    """首波支持的六种占位组件类型（冻结契约枚举）。"""

    METRIC_CARD = "metric_card"
    LINE_CHART = "line_chart"
    BAR_CHART = "bar_chart"
    PIE_CHART = "pie_chart"
    TABLE = "table"
    TEXT = "text"


#: 组件类型的合法取值集合。**唯一来源**是上面的 ``ComponentType`` 枚举 ——
#: 模型适配层、模板生成层、草稿转换层都从这里取，不要再各抄一份（#39）。
COMPONENT_TYPE_VALUES: tuple[str, ...] = tuple(item.value for item in ComponentType)


class DisplayIntent(str, Enum):
    """板块的**展示目的**：用户想看什么，不是用哪种图看。

    模型给的是语义候选，服务端据此定稿组件类型（20261029 规格「跨工单契约」）：
    用户明确指定 > 按展示目的映射 > 模型原选。

    取值与需求稿 3.3 的对应关系一一对应：

    - ``total``       看总量或关键指标 → 指标卡
    - ``trend``       看随时间的变化 → 趋势图
    - ``comparison``  比较不同对象或排名 → 柱状图
    - ``composition`` 看构成或占比 → 占比图
    - ``records``     看具体记录和属性 → 表格
    """

    TOTAL = "total"
    TREND = "trend"
    COMPARISON = "comparison"
    COMPOSITION = "composition"
    RECORDS = "records"


#: 展示目的的合法取值集合。唯一来源是上面的 ``DisplayIntent`` 枚举。
DISPLAY_INTENT_VALUES: tuple[str, ...] = tuple(item.value for item in DisplayIntent)


class GrainUnit(str, Enum):
    """既有时间粒度枚举：MINUTE / HOUR / DAY / WEEK / MONTH。

    与工具层（``check_capability_tool`` / ``build_definition_draft_tool``）
    的取值一致，不另起一套。方案里的 ``requirements.grainUnit`` 与
    **时间范围单位是两回事**：范围是「最近 7 天」，粒度是「按天」。
    """

    MINUTE = "MINUTE"
    HOUR = "HOUR"
    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"


#: 粒度的合法取值集合。唯一来源是上面的 ``GrainUnit`` 枚举。
GRAIN_UNIT_VALUES: tuple[str, ...] = tuple(item.value for item in GrainUnit)

#: 纯说明板块的组件类型：它不需要指标意图（#39 硬要求「纯说明板块明确声明为文本」）。
TEXT_COMPONENT_TYPE = ComponentType.TEXT.value


class ReportPlanError(Exception):
    """方案领域错误基类。

    协调者按 ``ReportPlanError`` 捕获，再看 ``code`` 区分处理方式。
    """

    code = "PLAN_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class PlanValidationError(ReportPlanError):
    """方案结构非法：字段缺失、类型不对、取值越界或用法错误。"""

    code = "PLAN_INVALID_STRUCTURE"


class PlanContentRejectedError(ReportPlanError):
    """方案里混进了不该出现的执行性内容（SQL、脚本、凭证、真实数值等）。"""

    code = "PLAN_UNSAFE_CONTENT"


class PlanContractError(ReportPlanError):
    """方案契约非法：组件类型缺失/非法，或取数板块没有指标意图。

    错误信息**必须带板块标识**（序号 + 标题）。编排层（#36）会把 ``message``
    原样透传给用户 —— 用户要能一眼看出是哪个板块出的问题，不能只拿到
    「生成失败，请重试」。
    """

    code = "PLAN_CONTRACT_INVALID"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        section_id: str | None = None,
        section_title: str | None = None,
        section_index: int | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.section_id = section_id
        self.section_title = section_title
        self.section_index = section_index


def _section_label(index: int | None, title: str | None, section_id: str | None) -> str:
    """板块标识：序号 + 标题（+ id）。错误信息里必须出现它。"""
    parts = [f"板块 {index + 1}" if index is not None else "板块"]
    if title:
        parts.append(f"「{title}」")
    label = "".join(parts)
    if section_id:
        label += f"（id={section_id}）"
    return label


def validate_section_contract(section: Any, *, index: int | None = None) -> None:
    """板块级契约校验：组件类型必须合法；取数板块必须有指标意图。

    这是方案契约的**唯一权威判据**，写路径与模板层共用。

    抛 ``PlanContractError``，信息里带板块标识。``section`` 可以是原始
    映射或 pydantic 模型（模型会先 dump 成映射）。
    """
    if isinstance(section, BaseModel):
        section = section.model_dump()
    if not isinstance(section, Mapping):
        return  # 结构问题交给 pydantic，那时拿不到板块标识

    raw_title = section.get("title")
    title = str(raw_title).strip() if isinstance(raw_title, str) else None
    raw_id = section.get("id")
    section_id = str(raw_id).strip() if isinstance(raw_id, str) else None
    label = _section_label(index, title, section_id)

    raw_type = section.get("componentType")
    component_type = str(getattr(raw_type, "value", raw_type) or "").strip()
    if not component_type:
        raise PlanContractError(
            f"{label}缺少组件类型（componentType）：合法取值为 "
            f"{' / '.join(COMPONENT_TYPE_VALUES)}。",
            section_id=section_id, section_title=title, section_index=index,
        )
    if component_type not in COMPONENT_TYPE_VALUES:
        raise PlanContractError(
            f"{label}的组件类型非法：{component_type!r}；合法取值为 "
            f"{' / '.join(COMPONENT_TYPE_VALUES)}。",
            section_id=section_id, section_title=title, section_index=index,
        )

    if component_type == TEXT_COMPONENT_TYPE:
        return  # 纯说明板块不强制指标意图

    raw_intents = section.get("metricIntents")
    intents = raw_intents if isinstance(raw_intents, (list, tuple)) else []
    if not [item for item in intents if isinstance(item, str) and item.strip()]:
        raise PlanContractError(
            f"{label}是取数板块（{component_type}），必须声明至少一条指标意图（metricIntents）。",
            section_id=section_id, section_title=title, section_index=index,
        )


class PlanRevisionConflictError(ReportPlanError):
    """版本冲突：基础版本与当前方案不一致。"""

    code = "PLAN_REVISION_CONFLICT"

    def __init__(
        self,
        message: str,
        *,
        expected_revision: int | None = None,
        actual_revision: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


class PlanClarificationRequired(ReportPlanError):
    """方案里有一处必须由用户拍板，服务端不替用户猜。

    ``question`` 就是要塞给用户的那句**具体**问题。编排层（05）据此走
    **澄清追问通道**：问题出现在左侧对话区，用户回答后再完善方案、
    再让用户确认。

    出现这条错误的前提是「猜错会影响这张表 / 这块板块的用途」——
    「没有真实数据」永远不是追问的理由。
    """

    code = "PLAN_CLARIFICATION_REQUIRED"

    def __init__(
        self,
        message: str,
        *,
        question: str | None = None,
        section_id: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.question = question or message
        self.section_id = section_id


class PlanColumnsUnresolvedError(ReportPlanError):
    """表格栏目修不了：模型给的结构不合法，补齐也失败。

    这条错误**不是**给用户的设计问题，是技术问题：不能把模型写坏的
    JSON 结构变成「请你设计这张表」。编排层（05）据此明确报生成失败
    并允许重试，绝不给一张空表还宣称成功。
    """

    code = "PLAN_COLUMNS_UNRESOLVED"

    def __init__(
        self,
        message: str,
        *,
        section_id: str | None = None,
        section_title: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.section_id = section_id
        self.section_title = section_title


def _format_validation_error(exc: ValidationError) -> str:
    """把 pydantic 的错误整理成一行可读信息。"""
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()))
        message = str(error.get("msg", ""))
        parts.append(f"{location}: {message}" if location else message)
    return "方案结构非法：" + "；".join(parts)


def _require_text(value: str) -> str:
    """非空文本；去掉首尾空白后仍为空即视为非法。"""
    if not isinstance(value, str):  # pragma: no cover - pydantic 已先做类型校验
        return value
    text = value.strip()
    if not text:
        raise ValueError("不能是空字符串")
    return text


def _require_key(value: str) -> str:
    """稳定键（栏目 id / 取值键）：非空、不带空白。"""
    text = _require_text(value)
    if not _KEY_PATTERN.match(text):
        raise ValueError("不能包含空白字符")
    return text


# ---------- 危险执行内容 ----------

#: 稳定键的合法形态：非空、不带空白。取值键要能当行对象的键用。
_KEY_PATTERN = re.compile(r"^\S+$")

# 方案里一旦出现这些字段名，说明模型把「执行绑定」塞进了设计稿：
# 数据源、调度、SQL、脚本、凭证都不属于方案契约。
_FORBIDDEN_FIELD_NAMES = {
    # 执行绑定 / 数据源 / 调度
    "execution",
    "executionplan",
    "executor",
    "schedule",
    "cron",
    "pipeline",
    "workflow",
    "datasource",
    "datasourceref",
    "dataset",
    "datasetref",
    "sourceref",
    "binding",
    "databinding",
    "metricref",
    "metricversion",
    "timewindow",
    "table",
    "query",
    "sql",
    "sqltext",
    "statement",
    # 脚本 / 代码 / 公式
    "script",
    "shell",
    "bash",
    "command",
    "cmd",
    "code",
    "formula",
    "expression",
    # 凭证
    "credential",
    "credentials",
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "accesskey",
    "secretkey",
    "privatekey",
    "connectionstring",
    "authorization",
    # 中文同义字段
    "数据源",
    "数据源绑定",
    "执行计划",
    "执行绑定",
    "查询语句",
    "脚本",
    "密码",
    "口令",
    "密钥",
    "凭证",
    "令牌",
}

_SQL_PATTERN = re.compile(
    r"\b(?:select\s+[\w*,\s.]+\s+from\s"
    r"|insert\s+into\s"
    r"|update\s+[\w.]+\s+set\s"
    r"|delete\s+from\s"
    r"|drop\s+(?:table|view|database|schema)\b"
    r"|truncate\s+table\b"
    r"|union\s+(?:all\s+)?select\s"
    r"|alter\s+table\s"
    r"|create\s+(?:table|view|index)\b)",
    re.IGNORECASE,
)

_SCRIPT_PATTERN = re.compile(
    r"(?:\b(?:ba|z|k)?sh\s+-c\b"
    r"|\bpython[0-9.]*\s+-c\b"
    r"|\bnode\s+-e\b"
    r"|\bos\.system\s*\("
    r"|\bsubprocess\.[a-z_]+\s*\("
    r"|\beval\s*\("
    r"|\bexec\s*\("
    r"|\brm\s+-rf\b"
    r"|\bcurl\b[^|;]{0,120}\|\s*(?:ba)?sh\b"
    r"|\bchmod\s+\+x\b"
    r"|\bimport\s+(?:os|sys|subprocess)\b)",
    re.IGNORECASE,
)

_CREDENTIAL_PATTERN = re.compile(
    r"(?:\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|secret[_-]?key"
    r"|private[_-]?key|credential|credentials|authorization)\b\s*[:=：]\s*\S"
    r"|-----BEGIN[A-Z ]*PRIVATE KEY-----"
    r"|\bBearer\s+[A-Za-z0-9._\-]{8,}"
    r"|(?:密码|口令|密钥|凭证|访问令牌)\s*[:=：]\s*\S)",
    re.IGNORECASE,
)

# 带业务单位的数字：说明方案里混进了实测值。
_NUMBER_WITH_UNIT_PATTERN = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:kwh|kw|mw|wh|万元|元|%|％|亿)",
    re.IGNORECASE,
)

# 纯数字（可带单位）：指标位置出现这种值就是实测值，不是指标意图。
_PURE_NUMBER_PATTERN = re.compile(
    r"^[-+]?\d+(?:[.,]\d+)?\s*(?:kwh|kw|mw|wh|万元|元|%|％)?$",
    re.IGNORECASE,
)

# 小数：指标意图里出现小数基本就是抄了实测值。
_DECIMAL_NUMBER_PATTERN = re.compile(r"\d+[.,]\d+")

# 指标意图里的赋值号：``PUE = IT 能耗 / 总能耗`` 是执行公式，不是指标意图。
_FORMULA_PATTERN = re.compile(r"[=÷×]")

# 指标意图字段：这里最严格。
_STRICT_METRIC_FIELDS = {"metrics", "metricIntents"}


def _normalize_field_name(name: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]", "", str(name).lower())


def _reject_forbidden_fields(payload: Any, path: str = "") -> None:
    """字段名里出现执行绑定 / 脚本 / 凭证就直接拒。"""
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_text = str(key)
            if _normalize_field_name(key_text) in _FORBIDDEN_FIELD_NAMES:
                raise PlanContentRejectedError(
                    f"方案不得包含执行绑定、脚本或凭证字段：{path}{key_text}"
                )
            _reject_forbidden_fields(value, f"{path}{key_text}.")
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            _reject_forbidden_fields(item, f"{path}[{index}].")


def _scan_text(text: str, path: str, *, strict: bool) -> None:
    if _SQL_PATTERN.search(text):
        raise PlanContentRejectedError(f"方案里不能出现 SQL：{path}")
    if _SCRIPT_PATTERN.search(text):
        raise PlanContentRejectedError(f"方案里不能出现可执行脚本：{path}")
    if _CREDENTIAL_PATTERN.search(text):
        raise PlanContentRejectedError(f"方案里不能出现凭证内容：{path}")
    if _NUMBER_WITH_UNIT_PATTERN.search(text):
        raise PlanContentRejectedError(f"方案里不能出现真实数值：{path}")
    if strict:
        if _PURE_NUMBER_PATTERN.match(text.strip()):
            raise PlanContentRejectedError(f"指标意图不能是真实数值：{path}")
        if _DECIMAL_NUMBER_PATTERN.search(text):
            raise PlanContentRejectedError(f"指标意图不能是真实数值：{path}")
        if _FORMULA_PATTERN.search(text):
            raise PlanContentRejectedError(f"指标意图不能是执行公式：{path}")


def _scan_value(value: Any, path: str, *, strict: bool = False) -> None:
    if isinstance(value, str):
        _scan_text(value, path, strict=strict)
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            _scan_value(
                getattr(value, name),
                f"{path}{name}",
                strict=strict or name in _STRICT_METRIC_FIELDS,
            )
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan_value(item, f"{path}[{index}]", strict=strict)
    elif isinstance(value, dict):
        for key, item in value.items():
            _scan_value(item, f"{path}{key}", strict=strict)


class _PlanModel(BaseModel):
    """方案类型基类：字段封闭（多余字段直接判非法），并统一异常类型。"""

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> "_PlanModel":
        _reject_forbidden_fields(obj)
        try:
            return super().model_validate(obj, **kwargs)  # type: ignore[return-value]
        except ValidationError as exc:
            raise PlanValidationError(_format_validation_error(exc)) from exc

    @classmethod
    def model_validate_json(cls, json_data: str | bytes, **kwargs: Any) -> "_PlanModel":
        try:
            payload = json.loads(json_data)
        except (TypeError, ValueError):
            payload = None
        if payload is not None:
            _reject_forbidden_fields(payload)
        try:
            return super().model_validate_json(json_data, **kwargs)  # type: ignore[return-value]
        except ValidationError as exc:
            raise PlanValidationError(_format_validation_error(exc)) from exc

    @model_validator(mode="after")
    def _reject_execution_content(self) -> "_PlanModel":
        _scan_value(self, "")
        return self


class PlanRequirements(_PlanModel):
    """方案里用户**明确**表达的要求：指标、对象、时间范围与排除项。

    没说的字段留空（``None`` / 空数组），**不允许**用默认机房或默认日期
    悄悄补齐 —— 补齐会让用户以为那是他提的要求。
    """

    metrics: list[str] = Field(default_factory=list)
    subject: str | None = None
    timeRange: str | None = None
    exclusions: list[str] = Field(default_factory=list)
    #: 时间粒度（既有粒度枚举）。**与 timeRange 的单位不是一回事**：
    #: ``timeRange`` 是范围（「最近 7 天」），``grainUnit`` 是粒度（「按天」）。
    #: 用户没说时由服务端在确认前推定，并在假设里写明。
    grainUnit: GrainUnit | None = None

    @field_validator("metrics", "exclusions")
    @classmethod
    def _check_items(cls, value: list[str]) -> list[str]:
        return [_require_text(item) for item in value]

    @field_validator("subject", "timeRange")
    @classmethod
    def _check_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_text(value)


class PlanColumnCandidate(_PlanModel):
    """模型给出的栏目候选：**可以只有显示名**。

    模型不负责分配稳定身份 —— ``id`` 与 ``fieldRef`` 由服务端定稿时补齐
    （20261029 规格「栏目与取值键」）。模型带上 ``id`` 是为了在修改方案时
    保住栏目身份（改列名不换身份）。
    """

    id: str | None = None
    label: str
    fieldRef: str | None = None

    @field_validator("id", "fieldRef")
    @classmethod
    def _check_optional_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_key(value)

    @field_validator("label")
    @classmethod
    def _check_label(cls, value: str) -> str:
        return _require_text(value)


class PlanColumn(_PlanModel):
    """定稿栏目：稳定身份 + 显示名 + 行对象取值键。

    - ``id``：栏目身份。改显示名、重排都不改它。
    - ``label``：显示名，给人看的。
    - ``fieldRef``：**行对象里的稳定取值键**，不是真实数据源绑定。
      造数器按它造行，渲染层按它读单元格。
    """

    id: str
    label: str
    fieldRef: str

    @field_validator("id", "fieldRef")
    @classmethod
    def _check_key(cls, value: str) -> str:
        return _require_key(value)

    @field_validator("label")
    @classmethod
    def _check_label(cls, value: str) -> str:
        return _require_text(value)


class ReferenceTemplate(_PlanModel):
    """参考的本地模板资产。首波没有模板库时整个数组为空，不伪造来源。"""

    assetId: str
    version: int = Field(ge=1)
    name: str
    reason: str

    @field_validator("assetId", "name", "reason")
    @classmethod
    def _check_text(cls, value: str) -> str:
        return _require_text(value)


class PlanSectionCandidate(_PlanModel):
    """候选板块：新板块可以不带 id；更新板块可以带已存在的 id。"""

    id: str | None = None
    title: str
    metricIntents: list[str] = Field(default_factory=list)
    componentType: ComponentType
    #: 展示目的（语义候选）。模型给不给都行，服务端定稿时补齐。
    displayIntent: DisplayIntent | None = None
    #: 表格板块的栏目声明（仅表格板块使用）。模型可以只给 ``label``。
    columns: list[PlanColumnCandidate] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _check_optional_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_text(value)

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str) -> str:
        return _require_text(value)

    @field_validator("metricIntents")
    @classmethod
    def _check_intents(cls, value: list[str]) -> list[str]:
        return [_require_text(item) for item in value]


class PlanSection(_PlanModel):
    """已定稿的方案板块：id 由领域服务分配。

    定稿比候选多三样东西，都是**服务端**定的，模型说了不算：

    - ``displayIntent``：这块要看什么（语义）。
    - ``explicitComponentType``：用户明确指定的组件类型（没有指定就是 null）。
    - ``selectionReason``：为什么选了这个组件类型。

    另外 ``columns`` 是表格板块的定稿栏目：每项都有稳定 ``id``、显示名
    ``label`` 与行对象取值键 ``fieldRef``。
    """

    id: str
    title: str
    metricIntents: list[str] = Field(default_factory=list)
    componentType: ComponentType
    displayIntent: DisplayIntent | None = None
    explicitComponentType: ComponentType | None = None
    selectionReason: str | None = None
    columns: list[PlanColumn] = Field(default_factory=list)

    @field_validator("id", "title")
    @classmethod
    def _check_text(cls, value: str) -> str:
        return _require_text(value)

    @field_validator("metricIntents")
    @classmethod
    def _check_intents(cls, value: list[str]) -> list[str]:
        return [_require_text(item) for item in value]


class PlanCandidate(_PlanModel):
    """模型给出的方案候选：与方案同内容字段，但没有顶层 id / revision。

    顶层 id 与 revision 由服务端分配，模型给了也不算数 —— 所以这里
    直接把它们当多余字段判非法。
    """

    topic: str
    requirements: PlanRequirements
    sections: list[PlanSectionCandidate] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    referenceTemplates: list[ReferenceTemplate] = Field(default_factory=list)

    @field_validator("topic")
    @classmethod
    def _check_topic(cls, value: str) -> str:
        return _require_text(value)

    @field_validator("assumptions", "suggestions")
    @classmethod
    def _check_items(cls, value: list[str]) -> list[str]:
        return [_require_text(item) for item in value]


class ReportPlan(_PlanModel):
    """一份可提交的简短文字方案。

    序列化字段与冻结契约一一对应：``id``、``revision``、``topic``、
    ``requirements``、``sections``、``assumptions``、``suggestions``、
    ``referenceTemplates``。
    """

    id: str
    revision: int = Field(ge=1)
    topic: str
    requirements: PlanRequirements
    sections: list[PlanSection] = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    referenceTemplates: list[ReferenceTemplate] = Field(default_factory=list)

    @field_validator("id", "topic")
    @classmethod
    def _check_text(cls, value: str) -> str:
        return _require_text(value)

    @field_validator("assumptions", "suggestions")
    @classmethod
    def _check_items(cls, value: list[str]) -> list[str]:
        return [_require_text(item) for item in value]
