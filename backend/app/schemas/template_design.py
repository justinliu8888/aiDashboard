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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    dataStatus: str
    #: 占位原因，例如「口径待核实」「数据待连接」。
    placeholderReason: str | None = None
    #: 区域数据。``dataStatus`` 为 bound 时才有值。
    data: dict[str, Any] | None = None
    #: 数据来源标识（例如能耗 Mock 源）。界面据此标明数据性质。
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


class GridPlacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["GRID"]
    column: int = Field(strict=True, ge=1, le=12)
    row: int = Field(strict=True, ge=1)
    columnSpan: int = Field(strict=True, ge=1, le=12)
    rowSpan: int = Field(strict=True, ge=1)

    @model_validator(mode="after")
    def check_column_boundary(self) -> GridPlacement:
        if self.column + self.columnSpan - 1 > 12:
            raise ValueError("布局不能超出 12 列")
        return self


class TemplateLayoutItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    regionId: str = Field(min_length=1)
    placement: GridPlacement


class UpdateTemplateLayoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    templateId: str = Field(min_length=1)
    baseRevision: int = Field(strict=True, ge=1)
    items: list[TemplateLayoutItem] = Field(min_length=1)

    @model_validator(mode="after")
    def check_unique_non_overlapping_regions(self) -> UpdateTemplateLayoutRequest:
        region_ids = [item.regionId for item in self.items]
        if len(region_ids) != len(set(region_ids)):
            raise ValueError("布局区域不能重复")
        for index, item in enumerate(self.items):
            a = item.placement
            for other in self.items[:index]:
                b = other.placement
                # Half-open rectangles allow shared edges without overlap.
                if (
                    a.column < b.column + b.columnSpan
                    and b.column < a.column + a.columnSpan
                    and a.row < b.row + b.rowSpan
                    and b.row < a.row + a.rowSpan
                ):
                    raise ValueError("布局区域不能重叠")
        return self


class UpdateTemplateLayoutResponse(BaseModel):
    templateDesign: TemplateDesign
