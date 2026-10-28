"""只读查询的事实词汇。

三类问答（查当前对话草稿 / 问已记录进度 / 问实际产品能力）共用同一套
事实口径，事实结果的**状态**必须能区分下面四种情况，否则模型会把
「读不到」说成「没有」：

    FOUND         事实已核实
    ABSENT        确认没有该对象
    UNAVAILABLE   读取失败 / 服务端记录不可得
    FORBIDDEN     请求范围或权限与事实来源不一致

这套词汇由 Agent A 维护（共用契约），C / D 的领域模块直接引用，
不要各自另立一套状态枚举，避免两处语义漂移。
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class FactStatus(str, Enum):
    """事实结果的四种状态。"""

    #: 事实已核实，可以作为回答依据。
    FOUND = "FOUND"
    #: 确认没有该对象（不是「读不到」）。
    ABSENT = "ABSENT"
    #: 读取失败 / 服务端记录不可得 —— 只能说「无法确认」。
    UNAVAILABLE = "UNAVAILABLE"
    #: 请求范围或权限与事实来源不一致 —— 不能泄露，也不能当成「没有」。
    FORBIDDEN = "FORBIDDEN"


#: 事实结果的取值范围。模型只能在这个范围内取事实，不能自己造状态。
FACT_STATUSES = tuple(status.value for status in FactStatus)


class DraftFacts(BaseModel):
    """当前对话报表草稿的可核实事实。

    只包含模型判断需要的字段；完整草稿仍然按需走 ``get_current_region``。

    ``status`` 与 :class:`FactStatus` 同义：草稿这一项单独有状态，
    「会话记录读到了但里面没有草稿」是 ``ABSENT``（确认没有），
    不是会话读不到。
    """

    #: 草稿这一项的事实状态。
    status: FactStatus = FactStatus.ABSENT
    #: 当前对话是否存在报表草稿。
    exists: bool = False
    #: 报表草稿名称（项目词表：报表草稿）。
    reportName: str = ""
    #: 草稿 revision。与「已保存定义」的 revision 不是同一个口径，不能混用。
    revision: int = 0
    #: 草稿区块数量。
    regionCount: int = 0
    #: 区块概览：id / 名称 / 角色 / 组件类型。
    regions: list[dict[str, Any]] = Field(default_factory=list)
    #: 这份草稿当前还差什么才能往下走（按已记录的业务阶段推导）。
    pendingSteps: list[str] = Field(default_factory=list)
    #: 已保存的定义（保存 revision 与草稿 revision 分开）。
    savedDefinition: dict[str, Any] | None = None
    #: 已生成的报表预览。预览是数据快照，不代表实时重新取数。
    preview: dict[str, Any] | None = None
    #: 事实来源：server_session = 服务端会话记录；request_snapshot = 本轮请求快照。
    source: str = "server_session"


class SessionFacts(BaseModel):
    """当前对话的服务端事实集合。

    一次查询需要的全部「当前对话」事实都在这里：草稿、已记录业务阶段、
    待确认操作、已保存定义、预览。模型拿到这一份就能回答三类问题里
    与「当前对话」有关的部分。
    """

    #: 事实状态。见 FactStatus。
    status: FactStatus = FactStatus.UNAVAILABLE
    #: 服务端是否真的存在这条会话记录。
    sessionExists: bool = False
    #: 查询范围。首版固定为当前对话，不自动遍历历史。
    scope: str = "current_session"
    #: 已记录的业务阶段（会话 state）。它是已落库的阶段，不是实时任务状态。
    businessStage: str | None = None
    #: 草稿事实。服务端记录读不到时为 None；读到但没有草稿时 status=ABSENT。
    draft: DraftFacts | None = None
    #: 待确认修改计划的公开摘要。
    pendingEditPlan: dict[str, Any] | None = None
    #: 待确认选项（菜单）。
    pendingChoice: dict[str, Any] | None = None
    #: 已保存的定义。
    savedDefinition: dict[str, Any] | None = None
    #: 已生成的报表预览。
    preview: dict[str, Any] | None = None
    #: 同一项待定修改已追问的轮数。查询不占轮次。
    clarificationRounds: int = 0
    #: 不可得 / 受限时给模型的公开说明。
    reason: str = ""
    #: 服务端保存的完整草稿。只给服务端用（响应组装要用），
    #: 不会进模型上下文 —— 事实工具各自只返回需要的摘要。
    definition: Any | None = None
    #: 服务端保存的完整策略草稿。
    policy: Any | None = None

    @property
    def is_usable(self) -> bool:
        """事实是否可以直接作为回答依据。"""
        return self.status is FactStatus.FOUND


__all__ = [
    "FACT_STATUSES",
    "DraftFacts",
    "FactStatus",
    "SessionFacts",
]
