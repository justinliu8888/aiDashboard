"""终结工具的载荷模型。

终结工具的入参必须有**结构化 schema**，因为它的产出就是本轮的最终结论，
后端要按 schema 校验后再交给确定性层执行。已有 schema 的直接继承，
避免两处定义漂移。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import AliasChoices

from app.schemas.agent import QuestionOption
from app.schemas.edit_plan import EditPlan


class TimeWindowSpec(BaseModel):
    """时间窗：以 ``runtime.logicalTime`` 为锚点的相对窗口。

    ``unit=DAY, count=7, offset=-7`` 表示"过去 7 个完整自然日"
    （不含今天，从 D-7 到 D-1）。
    """

    unit: str = "DAY"
    count: int = Field(default=1, ge=1)
    # None 表示按 count 推导（offset = -count），即"完整自然日"口径。
    offset: int | None = None
    timezone: str = "Asia/Shanghai"
    includeCurrent: bool = False


class FilterSpec(BaseModel):
    """一条过滤条件。"""

    model_config = ConfigDict(populate_by_name=True)

    fieldRef: str = Field(
        default="",
        validation_alias=AliasChoices("fieldRef", "field", "fieldName", "column"),
    )
    op: str = Field(
        default="IN",
        validation_alias=AliasChoices("op", "operator", "comparison"),
    )
    values: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("values", "value", "members"),
    )

    @field_validator("fieldRef", mode="before")
    @classmethod
    def accept_field_alias(cls, value: Any) -> Any:
        if value in (None, ""):
            return ""
        return value

    @field_validator("op", mode="before")
    @classmethod
    def normalize_op(cls, value: Any) -> Any:
        if not value:
            return "IN"
        return str(value).strip().upper().replace("-", "_")

    @field_validator("values", mode="before")
    @classmethod
    def normalize_values(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, (str, int, float)):
            return [str(value)]
        if isinstance(value, (list, tuple, set)):
            return [str(v) for v in value]
        return value

    def resolved_field(self) -> str:
        return self.fieldRef


class DerivedColumnSpec(BaseModel):
    """一个派生列（上一周期值 / 增长率 / 占比等）。"""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default="", validation_alias=AliasChoices("id", "columnId"))
    label: str = Field(default="", validation_alias=AliasChoices("label", "name", "title"))
    fieldRef: str = Field(
        default="",
        validation_alias=AliasChoices("fieldRef", "asField", "field", "columnRef"),
    )
    # PREVIOUS_PERIOD / GROWTH_RATE / SHARE_OF_TOTAL
    kind: str = Field(default="", validation_alias=AliasChoices("kind", "type", "derive"))
    sourceFieldRef: str = Field(
        default="energyKwh",
        validation_alias=AliasChoices("sourceFieldRef", "sourceField", "baseFieldRef"),
    )
    format: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind", mode="before")
    @classmethod
    def normalize_kind(cls, value: Any) -> Any:
        if not value:
            return ""
        return str(value).strip().upper().replace("-", "_")

    @field_validator("format", mode="before")
    @classmethod
    def normalize_format(cls, value: Any) -> Any:
        return value or {}


class StyleHintSpec(BaseModel):
    """条件样式：例如"日增长率严格超过 20% 的行标红"。"""

    model_config = ConfigDict(populate_by_name=True)

    targetRegionId: str = Field(
        default="",
        validation_alias=AliasChoices("targetRegionId", "regionId", "target"),
    )
    # HIGHLIGHT_ABOVE_THRESHOLD / HIGHLIGHT_BELOW_THRESHOLD
    rule: str = Field(
        default="HIGHLIGHT_ABOVE_THRESHOLD",
        validation_alias=AliasChoices("rule", "type"),
    )
    fieldRef: str = Field(
        default="",
        validation_alias=AliasChoices("fieldRef", "field", "columnRef"),
    )
    comparison: str = Field(
        default="GT",
        validation_alias=AliasChoices("comparison", "op", "operator"),
    )
    threshold: float = Field(
        default=0.0,
        validation_alias=AliasChoices("threshold", "value", "limit"),
    )
    style: str = Field(default="RED", validation_alias=AliasChoices("style", "styleClass"))

    @field_validator("comparison", mode="before")
    @classmethod
    def normalize_comparison(cls, value: Any) -> Any:
        if not value:
            return "GT"
        return str(value).strip().upper()

    @field_validator("threshold", mode="before")
    @classmethod
    def normalize_threshold(cls, value: Any) -> Any:
        if value is None or value == "":
            return 0.0
        return value


class DraftPlanPayload(BaseModel):
    """节点 1：生成报表的计划。

    旧字段（reportName / roomId / includeTrend）保持不变；
    新字段全部可选，用于把路由层已经解析出来的结构化条件
    （数据源 / 粒度 / 时间窗 / 指标 / 过滤 / TopN / 派生列 / 排除区域）
    完整地带进确定性组装层，不再被默认值覆盖。
    """

    reportName: str
    roomId: str
    includeTrend: bool = True
    reply: str = ""

    # ---- 结构化条件（可选；缺省时行为与旧版完全一致）----
    sourceRef: str | None = None
    grainUnit: str | None = None
    # dict 形态 {"unit","count","offset","timezone"}，也接受自然语言串
    timeWindow: dict[str, Any] | str | None = None
    metricRefs: list[str] = Field(default_factory=list)
    # 组件请求：total / ranking / trend / quality / analysis，可带 ":粒度"
    componentRequests: list[str] = Field(default_factory=list)
    # 明确不要的业务区域：ranking / trend / analysis / quality / total
    excludedRegions: list[str] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    limit: int | None = None
    topN: int | None = None
    derivedColumns: list[DerivedColumnSpec] = Field(default_factory=list)
    styleHints: list[StyleHintSpec] = Field(default_factory=list)
    capabilityEvidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("sourceRef", "grainUnit", mode="before")
    @classmethod
    def blank_to_none(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("grainUnit", mode="before")
    @classmethod
    def normalize_grain(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper() or None
        return value

    @field_validator("metricRefs", "componentRequests", "excludedRegions", mode="before")
    @classmethod
    def normalize_str_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(v) for v in value]

    @field_validator("limit", "topN", mode="before")
    @classmethod
    def normalize_positive_int(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None



class ClarificationField(BaseModel):
    """需要补的一个信息项。"""

    field: str
    label: str = ""
    expects: str = "TEXT"  # TEXT / SINGLE_CHOICE / MULTI_CHOICE / DATE_RANGE
    options: list[QuestionOption] = Field(default_factory=list)
    required: bool = True


class ClarificationPayload(BaseModel):
    """节点 2：缺信息，需要追问。"""

    missingInfo: list[ClarificationField] = Field(default_factory=list)
    nextQuestion: str = ""
    autoFillable: dict[str, Any] | None = None
    reply: str = ""


class BlockedPayload(BaseModel):
    """节点 3：能力不支持 / 权限越界 / 无法处理。"""

    layer: str = "CAPABILITY"  # PERMISSION / CAPABILITY / UNSUPPORTED
    code: str = ""
    message: str = ""
    alternatives: list[str] = Field(default_factory=list)
    reply: str = ""


class ChangeProposal(BaseModel):
    """一处修改提议。"""

    regionId: str
    path: str
    after: Any = None
    summary: str = ""


class ChangePlanPayload(BaseModel):
    """节点 4：对已有草稿的修改方案。"""

    baseDefinitionRevision: int = 0
    changes: list[ChangeProposal] = Field(default_factory=list)
    reply: str = ""


class EditPlanPayload(EditPlan):
    """节点 5.5：模板编辑计划。

    继承现有 ``EditPlan``，只多一个 ``options``：用户表达含糊时
    （例如"改好看点"）把候选解释列出来交给用户选，而不是擅自改。
    """

    options: list[QuestionOption] = Field(default_factory=list)
    reply: str = ""


class EditAnswerPayload(BaseModel):
    """编辑态查询的只回答结论，不承载任何修改操作。"""

    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1)
    facts: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)

    @field_validator("reply")
    @classmethod
    def non_blank_reply(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("回答不能为空")
        return value.strip()


class FixProposal(BaseModel):
    """针对一条校验问题的修复动作。"""

    issueCode: str
    regionId: str | None = None
    action: str = ""
    summary: str = ""


class FixPlanPayload(BaseModel):
    """节点 8：校验失败后的修复方案。"""

    fixes: list[FixProposal] = Field(default_factory=list)
    reply: str = ""
