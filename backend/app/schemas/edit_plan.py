"""EditPlan：节点 5.5 USER_EDITING_TEMPLATE 下用户编辑意图的结构化描述。

ContextEngine 负责把用户的自然语言拆成 EditPlan；
TemplateEditEngine 根据 EditPlan 修改 ReportDefinitionDraft 并产出 ChangeSet。

本文件只定义 schema，不含规则 / LLM 调用，便于被 ContextEngine 和 TemplateEditEngine 共用。
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class EditIntent(str, Enum):
    RENAME_REPORT = "RENAME_REPORT"
    DELETE_SECTION = "DELETE_SECTION"
    RENAME_SECTION = "RENAME_SECTION"
    ADD_SECTION = "ADD_SECTION"
    #: 在**已有区块**里**追加**一个/多个指标（保留原有指标）。
    #:
    #: 与 ``CHANGE_METRIC``（替换）严格分开：用户说「再加 X」是**追加**，
    #: 只有明确说「替换 / 只留 / 换成」才是替换。
    ADD_METRIC = "ADD_METRIC"
    CHANGE_DATA_SOURCE = "CHANGE_DATA_SOURCE"
    CHANGE_VISUALIZATION = "CHANGE_VISUALIZATION"
    SUGGEST_VISUALIZATION = "SUGGEST_VISUALIZATION"
    CHANGE_COMPILATION_GRAIN = "CHANGE_COMPILATION_GRAIN"
    CHANGE_TIME_RANGE = "CHANGE_TIME_RANGE"
    CHANGE_METRIC = "CHANGE_METRIC"
    CHANGE_TOP_N = "CHANGE_TOP_N"
    CHANGE_SORT = "CHANGE_SORT"
    UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------
# 展示模式（写进定义，供渲染层读）
# --------------------------------------------------------------------------

#: 这一项**有数值**：按虚拟示例出数。
DISPLAY_MODE_SIMULATED = "simulated"

#: 这一项**只占位**：只显示名称，不塞数值。
#:
#: 只有用户**明确**要求「先占位 / 先加标签」时才用；默认一律是有数值的那一种。
DISPLAY_MODE_PLACEHOLDER_ONLY = "placeholder_only"

DISPLAY_MODES = (DISPLAY_MODE_SIMULATED, DISPLAY_MODE_PLACEHOLDER_ONLY)


class EditMetricAddition(BaseModel):
    """要**追加**到某个已有区块里的一个指标。

    「追加」与「替换」是两种不同的意图：追加**保留**该区块原有指标，
    把新指标加进去；替换才把原来的换掉。

    每个待追加指标至少能表达：用户原话意图、显示名、单位、展示模式。
    """

    #: 用户原话里的指标意图（系统不替用户改写口径）。
    intent: str = ""
    #: 显示名（用户看到的名字）。
    label: str = ""
    #: 单位；空串 = 口径还没确认。
    unit: str = ""
    #: 展示模式：``simulated``（有数值）/ ``placeholder_only``（只占位）。
    displayMode: str = DISPLAY_MODE_SIMULATED
    #: 目录内指标的引用；目录外指标**不伪造**它。
    metricRef: str | None = None
    #: 目录外指标的**服务端解析结果**（候选）：
    #: ``{"intent", "kind", "unit", "status", "note"}``。
    metricCandidate: dict[str, Any] | None = None
    #: 面向用户的补充说明（写进建议文案）。
    note: str = ""

    @field_validator("displayMode", mode="before")
    @classmethod
    def _normalize_display_mode(cls, value: Any) -> str:
        """取值域只认两个；认不出来按「有数值」处理。"""
        text = str(value or "").strip().lower()
        return text if text in DISPLAY_MODES else DISPLAY_MODE_SIMULATED


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
    #: 目录外指标的**用户原话**（指标意图）。
    #:
    #: 解析不出真实指标标识时用它：不再以「未登记」拒绝，而是转成可澄清
    #: 或带显式假设的模拟候选（工单 #65）。有它时 ``metricRef`` 为空 ——
    #: 目录外指标**不伪造** ``metricRef``，它只有一个稳定的模拟身份。
    metricIntent: str | None = None
    #: 目录外指标的**服务端解析结果**（候选）：``{"intent", "kind",
    #: "unit", "status", "note"}``。
    #:
    #: 用户拍板过口径之后（耗电量？占比？功率？），以**这一份**为准 ——
    #: 接受时不再按原话重新解析一遍，口径不会漂。
    metricCandidate: dict[str, Any] | None = None

    #: 候选所基于的**草稿版本**。
    #:
    #: 接受时用它判过期：候选是基于旧版本草稿做的，服务端直接拒绝
    #: （工单 #65，契约表 §4.2）。为空 = 不做这项校验（当场解析当场应用）。
    baseDefinitionRevision: int | None = None

    # ADD_METRIC
    #
    #: 要**追加**到目标区块里的指标（一个或多个）。
    #:
    #: 一次可以追加多个 —— 引擎把它们当**一次原子修改**处理：
    #: 全部成功才落到草稿，任一步失败则整体不生效，原草稿不变。
    addMetrics: list[EditMetricAddition] = Field(default_factory=list)

    #: 选型依据：``{"ruleId", "componentType", "detail", "explain"}``。
    #:
    #: 命中依据来自**真实规则命中**（工单 #60 的规则库），不是事后编的；
    #: 组件类型由用户原话或规则定稿，系统**不静默改写**（工单 #65）。
    componentSelection: dict[str, Any] | None = None

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

    #: 这份计划所基于的**生效组合快照**（契约表 §2.1 的结构）。
    #:
    #: 接受时逐字段比对当前生效组合：对不上就是**过期 / 冲突**，服务端
    #: 直接拒绝，旧生效组合原样保留（契约表 §4.2）。
    baseCombination: dict[str, Any] | None = None

    # 面向用户的修改摘要；确认菜单直接展示这句话。
    summary: str = ""
