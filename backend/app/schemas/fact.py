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


#: 领域模块自有词表 -> 共用事实状态的映射。
#:
#: 各领域模块可以在自己的契约里保留更细的词表（进度用「已记录阶段」，
#: 能力用「目录登记 / 链路支持」），但**对共用层暴露的状态**必须落回这四态，
#: 否则「读不到」和「确实没有」会被混为一谈。
#:
#: 进度模块 ``availability``：
#:     SUCCESS / NO_OBJECT / UNAVAILABLE / PERMISSION_DENIED
#: 能力模块 ``outcome``：
#:     OK / REGISTERED_NOT_SUPPORTED / NOT_REGISTERED / NOT_AUTHORIZED
#:     / MISSING_CONTEXT
DOMAIN_STATUS_TO_FACT_STATUS: dict[str, FactStatus] = {
    # 进度
    "SUCCESS": FactStatus.FOUND,
    "NO_OBJECT": FactStatus.ABSENT,
    "UNAVAILABLE": FactStatus.UNAVAILABLE,
    "PERMISSION_DENIED": FactStatus.FORBIDDEN,
    # 能力
    "OK": FactStatus.FOUND,
    # 目录登记了但当前链路不支持：事实本身**查到了**，限制在别处披露。
    "REGISTERED_NOT_SUPPORTED": FactStatus.FOUND,
    "NOT_REGISTERED": FactStatus.ABSENT,
    "NOT_AUTHORIZED": FactStatus.FORBIDDEN,
    # 上下文不足：事实读到了一部分，缺的那部分走澄清。
    "MISSING_CONTEXT": FactStatus.FOUND,
}


def to_fact_status(domain_status: str | None) -> FactStatus:
    """把领域模块的状态折成共用事实状态。

    未登记的取值按「不可得」处理：宁可说读不到，也不要当成「没有」。
    """
    if not domain_status:
        return FactStatus.UNAVAILABLE
    return DOMAIN_STATUS_TO_FACT_STATUS.get(
        str(domain_status).strip().upper(), FactStatus.UNAVAILABLE
    )


#: 回答「做到哪一步了」这类问题时，用户可见答案里必须说清的边界。
#:
#: 工单 #03：「回答明确是已记录状态，不冒充实时任务监控」；
#: 父规格 #12：「数据可能不是实时的，应以用户可理解的方式说明」。
#:
#: 这是**共用层的口径**：进度领域模块（C）说这条边界时用同一句，
#: 共用层在服务端补进用户可见答案时也用同一句 —— 一处口径，两处引用。
RECORDED_STAGE_DISCLOSURE = "这是服务端已记录的业务阶段，不是实时任务进度。"

#: 答案里出现其中之一，才算说清了「这是已记录的状态」。
#:
#: 只看「已记录」不够：日常说法（「你的需求已记录」）也含这几个字。
RECORDED_STATE_MARKERS: tuple[str, ...] = (
    "已记录",
    "已存档",
    "记录的业务阶段",
    "会话记录里",
)

#: 答案里出现其中之一，才算说清了「不是实时进度」。
RECORDED_NOT_LIVE_MARKERS: tuple[str, ...] = (
    "不是实时",
    "非实时",
    "没有实时",
    "不是正在跑",
)

#: 答案里出现其中之一，就是**冒充实时任务监控**（工单 #03 的红线）。
#:
#: 注意「不是实时进度」「无法给出百分比」这类**否定**说法不在这里 ——
#: 它们恰恰是在守红线。判定时先剥掉否定式说法再扫这些词。
LIVE_PROGRESS_CLAIM_MARKERS: tuple[str, ...] = (
    "正在跑",
    "正在生成",
    "正在执行",
    "实时进度",
    "实时监控",
    "百分之",
)



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
    "DOMAIN_STATUS_TO_FACT_STATUS",
    "FACT_STATUSES",
    "LIVE_PROGRESS_CLAIM_MARKERS",
    "RECORDED_NOT_LIVE_MARKERS",
    "RECORDED_STAGE_DISCLOSURE",
    "RECORDED_STATE_MARKERS",
    "DraftFacts",
    "FactStatus",
    "SessionFacts",
    "to_fact_status",
]
