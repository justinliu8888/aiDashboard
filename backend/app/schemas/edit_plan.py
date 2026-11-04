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
    #: 在**已有卡片**里**删掉某一个指标**（卡片本身保留）。
    #:
    #: 与 ``DELETE_SECTION``（删整张卡片）严格分开：用户说「把实时功率这个
    #: 指标删掉」说的是**卡片里的那一项**，不是卡片。
    DELETE_METRIC = "DELETE_METRIC"
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
    #: **展示批次**（契约 0002 §3）：分组 / 排版 / 样式 / 格式 / 页面 / 主题。
    #:
    #: 一批操作是**一个修改单位**：整批成功才生效，任一步失败整批放弃
    #: （不留下半份定义）。操作本身受白名单约束，不是任意属性路径、
    #: 不是任意 CSS 文本、不是脚本。
    APPLY_PRESENTATION = "APPLY_PRESENTATION"
    #: **保存共享主题**（契约 0002 §8.2）。
    #:
    #: 与 ``APPLY_PRESENTATION`` 的 ``SET_THEME`` 严格分开：那条是
    #: **把已有主题应用到这份报表**（改报表定义），这条是**把当前外观
    #: 存成 / 更新一个共享主题**（改主题本身）。
    #:
    #: 两条硬约束：
    #:
    #: 1. **未确认不落库**：先算影响摘要并追问，用户确认后才提交；
    #: 2. **不改报表定义**：主题是共享对象，不是报表内容修改 ——
    #:    regions / layout / format 一律不动，定义版本也**不 bump**。
    SAVE_THEME = "SAVE_THEME"
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


class EditMetricDeletion(BaseModel):
    """要**删除**的一个指标 —— 卡片内的**一个指标实例**。

    「删掉某个指标」与「删掉整张卡片」是两件事（契约表 §4.3）：目标必须
    指向卡片内**真实存在**的那一项指标。

    身份用**稳定标识**记（``metricKey`` / ``simulatedId`` / ``fieldRef``），
    **不记数组下标** —— 删掉中间一项之后下标会漂移；``label`` 只是给人看
    的显示名，**不作为定位依据**（同名指标要澄清，不能删第一条匹配的）。
    """

    #: 卡片内指标实例的**稳定身份**（``previewSpec.metrics[].metricKey``）。
    metricKey: str = ""
    #: 目录外指标的**稳定模拟身份**（``previewSpec.metrics[].simulatedId``）。
    simulatedId: str | None = None
    #: 该指标在定义里的**字段引用**（``component.columns[].fieldRef`` 或
    #: ``component.metric.fieldRef``）。
    fieldRef: str | None = None
    #: 给人看的显示名（``previewSpec.metrics[].label``）。
    label: str = ""
    #: 是不是这张卡片的**主指标**（``component.metric``）。
    isPrimary: bool = False
    #: 用户原话里的指标意图（保留原始表达，便于回显与追问）。
    intent: str = ""


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

    # DELETE_METRIC
    #
    #: 要从目标卡片里**删掉的指标**（一个或多个）。
    #:
    #: 同样按**一次原子修改**处理：全部成功才落到草稿，任一步失败则整体
    #: 不生效，原草稿不变（与 ``addMetrics`` 同一套约定）。
    deleteMetrics: list[EditMetricDeletion] = Field(default_factory=list)

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

    #: 其他说明 / 预览文案
    markdownPreview: str | None = None
    reason: str | None = None

    # APPLY_PRESENTATION
    #
    #: 一批**受约束**的展示操作（契约 0002 §3.1）。
    #:
    #: 每项形如 ``{"op": "CREATE_GROUP", "params": {...}}``：
    #: ``op`` 取白名单里的值（CREATE_GROUP / MOVE_REGION / UNGROUP /
    #: SET_GROUP_ARRANGEMENT / SET_PLACEMENT / SET_STYLE / SET_FORMAT /
    #: SET_PAGE / SET_METADATA / COPY_REGION / DELETE_REGION /
    #: HIDE_REGION / SHOW_REGION / DELETE_GROUP / SET_THEME），
    #: ``params`` 的键与取值域由 ``report_presentation_service`` 校验。
    #:
    #: 这批操作**不改**统计口径、不改时间范围、不改设备范围 ——
    #: 分组只管外观。
    #:
    #: **形状约束**：每项必须是 ``{"op": <操作名>, "params": {...}}`` ——
    #: ``op`` 是非空字符串，``params`` 是对象（缺省算空对象）。
    #: 只是**形状**上的约束：不改内容、不改顺序、不补默认值 ——
    #: 规则引擎与模型路径现有的写法照旧通过；
    #: 嵌套参数（字号 / 间距 / 主题名…）的取值域仍由
    #: ``report_presentation_service`` 校验，不在这里另写一份。
    presentationOperations: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("presentationOperations", mode="before")
    @classmethod
    def _check_presentation_operations(cls, value: Any) -> Any:
        """展示操作必须是 ``{"op": str, "params": dict}``。

        只卡**形状**（契约 §1.2 N7：未知 op / 未知参数键仍然报错），
        不在这里重写取值域 —— 那是执行器与规范化层的职责，
        两边各写一份必然漂。
        """
        if value is None:
            return []
        if not isinstance(value, list):
            # 类型不对就交给 pydantic 自己报错（文案更标准）。
            return value
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise ValueError(f"presentationOperations[{index}] 必须是对象。")
            op = item.get("op")
            if not isinstance(op, str) or not op.strip():
                raise ValueError(f"presentationOperations[{index}] 缺少操作名 op。")
            params = item.get("params")
            if params is None:
                continue
            if not isinstance(params, dict):
                raise ValueError(
                    f"presentationOperations[{index}] 的 params 必须是对象。"
                )
        return value

    # SAVE_THEME
    #
    #: 要保存 / 更新的**共享主题**操作（契约 0002 §8.2）。
    #:
    #: 每项形如
    #: ``{"themeId": "theme_xxx", "name": "蓝色主题", "tokens": {...},
    #: "defaultStyle": {...}, "expectedThemeRevision": 3, "confirm": True}``：
    #:
    #: * ``themeId``：留空 = 新建主题（确认时服务端给具体 id）；
    #: * ``tokens`` / ``defaultStyle``：主题的取值域由主题服务校验；
    #: * ``expectedThemeRevision``：乐观锁 —— 与当前版本不一致就拒绝；
    #: * ``confirm``：**未确认不落库**。为假时只算影响摘要并追问。
    themeOperations: list[dict[str, Any]] = Field(default_factory=list)


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
