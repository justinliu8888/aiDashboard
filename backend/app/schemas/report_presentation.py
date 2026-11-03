"""展示操作（外观分组 / 排版 / 样式）的结构化结果（契约 0002 §3.2）。

本模块只定义 schema，**不含任何执行逻辑**：
执行在 ``app.services.report_presentation_service`` 里（纯计算）。

两个结构：

* :class:`PresentationOperation` —— 一批展示操作里的**一项**。
  它是**受约束的白名单操作**（不是任意属性路径、不是任意 CSS 文本、
  不是脚本）：``op`` 取值来自契约表 §3.1，``params`` 是该 op 的参数表。
* :class:`PresentationResult` —— 一批展示操作跑完之后的结构。
  **整批原子**：任一步失败 → ``ok=False`` 且 ``definition`` 等于入参内容
  （不留半份定义），``changeSet`` 为 ``None``。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.change_set import ChangeSet
from app.schemas.report_definition import ReportDefinitionDraft


class PresentationOperation(BaseModel):
    """一项**受约束**的展示操作。

    ``extra="forbid"``：多写一个键就是非法操作 —— 白名单靠**结构**兜底，
    不靠调用方自觉。
    """

    model_config = ConfigDict(extra="forbid")

    #: 白名单操作名（契约表 §3.1）。
    op: str
    #: 该操作的参数（键与取值域都由执行器逐项校验）。
    params: dict[str, Any] = Field(default_factory=dict)


class PresentationResult(BaseModel):
    """一批展示操作的结果。"""

    #: 整批是否成功。
    ok: bool
    #: 成功 = 新定义；失败 = 入参定义的等价副本（内容一致，不留半份定义）。
    definition: ReportDefinitionDraft
    #: 成功时给出；失败为 ``None``。
    changeSet: ChangeSet | None = None
    #: 受影响区域（含新建容器 id）。
    affectedRegionIds: list[str] = Field(default_factory=list)
    #: 校验 / 布局问题（``{code, message, regionId?}``）。
    issues: list[dict[str, Any]] = Field(default_factory=list)
    #: 纯展示操作**恒为 False**（不改变任何数据绑定）。
    dataBindingChanged: bool = False
    #: 面向用户的失败说明（成功时为空串）。
    failure: str = ""
    #: 失败分类：``no_target`` / ``invalid_operation`` / ``out_of_range`` /
    #: ``cycle`` / ``unknown_region`` / ``reference_blocked``。
    reason: str = ""
