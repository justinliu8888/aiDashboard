"""模板设计对象：从**已确认的文字方案**生成的可视模板（规格 #18 / 工单 02）。

这一层定义三件事：

    - 区域（``TemplateRegion``）：模板里的一个可视组件，**对应方案里的一个板块**；
    - 每个区域独立标注数据状态：接到了当前能力可取到的数据，还是保留占位；
    - 模板设计（``TemplateDesign``）：模板整体，绑定方案的 id 与 revision。

设计对象**不包含**可执行 SQL、脚本、调度、凭证与数据源绑定 ——
它描述的是「要长什么样、哪些区域有数、哪些还等着数」，不是执行计划。
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RegionDataStatus(str, Enum):
    """区域的数据状态。

    ``BOUND``
        接到了当前已接通能力能取到的数据。
    ``PLACEHOLDER``
        保留占位：没有对应数据，或口径还没确认。占位区域**不填数值**。
    ``TEXT``
        文字块，本来就没有数值。
    """

    BOUND = "bound"
    PLACEHOLDER = "placeholder"
    TEXT = "text"


class TemplateColumn(BaseModel):
    """方案声明的一列表格栏目。

    ``fieldRef`` 是**行对象的稳定取值键**，不是数据源绑定 —— 造数器按它造行、
    前端按它读单元格。改列名不改栏目身份（``id`` 与 ``fieldRef`` 都不变）。

    多余字段（列宽等）保留原样透传：方案侧以后加字段时，转换层不该把
    它们悄悄吃掉。
    """

    model_config = ConfigDict(extra="allow")

    id: str
    label: str
    fieldRef: str
    #: 列的展示格式（精度、单位等），可空。
    format: dict[str, Any] | None = None


class TemplateRegion(BaseModel):
    """模板里的一个可视区域。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    #: 对应的方案板块 id。方案与模板的对应关系靠它，不能只对标题。
    sectionId: str | None = None
    title: str
    componentType: str
    metricIntents: list[str] = Field(default_factory=list)
    #: 这个区域实际落到哪些指标口径上（设计元数据，不是执行绑定）。
    metricRefs: list[str] = Field(default_factory=list)
    #: 方案声明的**定稿栏目**（表格板块）。顺序与方案一致，转换层不按标题猜列。
    columns: list[TemplateColumn] = Field(default_factory=list)
    dataStatus: str
    #: 占位原因，例如「口径待核实」「数据待连接」。
    placeholderReason: str | None = None
    #: 这个区域是不是因为**系统能力尚未接通**而拿不到数。
    #:
    #: 它与「授权范围」「绑定三态」**相互独立**：统计对象越权不算能力缺失，
    #: 指标口径算不出来才算。区域要标「能力待接通」时看它，不看占位原因。
    capabilityPending: bool = False
    #: 区域数据。``dataStatus`` 为 bound 时才有值。
    data: dict[str, Any] | None = None
    #: 数据来源标识（例如 ``mock_energy``）。界面据此标明数据性质 ——
    #: 草稿阶段显示的是**模拟业务数据**，接入真实数据后自动替换。
    dataSource: str | None = None
    dataSourceLabel: str | None = None


class TemplateDesign(BaseModel):
    """从已确认方案生成的模板设计。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    revision: int = Field(ge=1)
    planId: str
    planRevision: int = Field(ge=1)
    title: str
    regions: list[TemplateRegion] = Field(min_length=1)
    layout: dict[str, Any] = Field(default_factory=dict)
    referenceTemplates: list[dict[str, Any]] = Field(default_factory=list)
    #: 整体数据状态：bound（全接上）/ partial（部分接上）/ placeholder（全占位）。
    dataStatus: str
    #: 没有接上的区域，界面要说明为什么。
    pendingNotes: list[str] = Field(default_factory=list)
