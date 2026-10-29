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


# ---------- 危险执行内容 ----------

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
    """已定稿的方案板块：id 由领域服务分配。"""

    id: str
    title: str
    metricIntents: list[str] = Field(default_factory=list)
    componentType: ComponentType

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
