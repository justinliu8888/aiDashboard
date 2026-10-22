"""EditPlan：节点 5.5 USER_EDITING_TEMPLATE 下用户编辑意图的结构化描述。

ContextEngine 负责把用户的自然语言拆成 EditPlan；
TemplateEditEngine 根据 EditPlan 修改 ReportDefinitionDraft 并产出 ChangeSet。

本文件只定义 schema，不含规则 / LLM 调用，便于被 ContextEngine 和 TemplateEditEngine 共用。
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EditIntent(str, Enum):
    RENAME_REPORT = "RENAME_REPORT"
    DELETE_SECTION = "DELETE_SECTION"
    RENAME_SECTION = "RENAME_SECTION"
    ADD_SECTION = "ADD_SECTION"
    CHANGE_DATA_SOURCE = "CHANGE_DATA_SOURCE"
    CHANGE_VISUALIZATION = "CHANGE_VISUALIZATION"
    SUGGEST_VISUALIZATION = "SUGGEST_VISUALIZATION"
    CHANGE_COMPILATION_GRAIN = "CHANGE_COMPILATION_GRAIN"
    CHANGE_TIME_RANGE = "CHANGE_TIME_RANGE"
    CHANGE_METRIC = "CHANGE_METRIC"
    CHANGE_TOP_N = "CHANGE_TOP_N"
    CHANGE_SORT = "CHANGE_SORT"
    UNKNOWN = "UNKNOWN"


class EditTarget(BaseModel):
    """用户想改哪个 section。"""

    regionId: str = ""
    matchedText: str = ""
    confidence: float = 0.0


class EditOperation(BaseModel):
    """具体改成什么。字段按 intent 不同用到的子集不同；这里统一松结构承载。"""

    # RENAME_SECTION
    newTitle: str | None = None

    # ADD_SECTION / CHANGE_VISUALIZATION
    componentType: str | None = None  # TEXT / METRIC_CARD / TABLE / CHART / DATA_QUALITY / ANALYSIS
    chartKind: str | None = None  # LINE / BAR / PIE / AREA ...
    sectionTitle: str | None = None
    insertAfterRegionId: str | None = None

    # CHANGE_DATA_SOURCE / ADD_SECTION
    sourceRef: str | None = None
    sourceVersion: str | None = None
    fieldMappingRequired: bool | None = None

    # CHANGE_METRIC / ADD_SECTION
    metricRef: str | None = None
    metricVersion: str | None = None

    # CHANGE_TIME_RANGE
    timeWindow: dict[str, Any] | None = None
    timeWindowLabel: str | None = None

    # CHANGE_COMPILATION_GRAIN
    grain: dict[str, Any] | None = None
    supported: bool | None = None
    supportedGrains: list[str] = Field(default_factory=list)

    # CHANGE_TOP_N / CHANGE_SORT
    topN: int | None = None
    sortField: str | None = None
    sortDirection: str | None = None  # ASC / DESC

    # SUGGEST_VISUALIZATION
    dataShape: str | None = None
    displayOptions: list[dict[str, Any]] = Field(default_factory=list)
    disabledOptions: list[dict[str, Any]] = Field(default_factory=list)

    # 其他说明 / 预览文案
    markdownPreview: str | None = None
    reason: str | None = None


class EditOperationStep(BaseModel):
    """组合修改中的一步：一次原子修改里的一个确定性动作。

    单项编辑不需要它；只有「换源 + 改粒度」这类组合计划才用到。
    """

    intent: EditIntent
    operation: EditOperation = Field(default_factory=EditOperation)
    summary: str = ""


class EditPlan(BaseModel):
    isTemplateEdit: bool = True
    intent: EditIntent = EditIntent.UNKNOWN
    confidence: float = 0.0
    target: EditTarget = Field(default_factory=EditTarget)
    operation: EditOperation = Field(default_factory=EditOperation)
    needsClarification: bool = False
    clarificationQuestion: str = ""

    # 组合修改的操作列表（按执行顺序）。
    # 非空即表示这是一次原子组合修改：所有步骤在临时草稿上依次校验并应用，
    # 任一步失败则整体不生效，原草稿保持不变。
    # 此时 intent / operation 仍指向用户真正要求的那一步（列表的最后一步），
    # 便于现有单步消费方照常读到目标和操作。
    operations: list[EditOperationStep] = Field(default_factory=list)

    # 面向用户的修改摘要；确认菜单直接展示这句话。
    summary: str = ""
