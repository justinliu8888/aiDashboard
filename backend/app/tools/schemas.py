"""终结工具的载荷模型。

终结工具的入参必须有**结构化 schema**，因为它的产出就是本轮的最终结论，
后端要按 schema 校验后再交给确定性层执行。已有 schema 的直接继承，
避免两处定义漂移。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.agent import QuestionOption
from app.schemas.edit_plan import EditPlan


class DraftPlanPayload(BaseModel):
    """节点 1：生成报表的计划。"""

    reportName: str
    roomId: str
    includeTrend: bool = True
    reply: str = ""


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
