"""「回复评价与反馈收件箱」的请求 / 响应模型。

协议边界（20261104 确认稿）：

* **公开的评价目标**只有四个字段：``id`` / ``kind`` / ``createdAt`` / ``available``。
  目标里那些「结构性定位信息」是**服务端内部**的，不进协议、不进前端。
* 旧客户端不认识 ``feedbackTarget`` —— 它是**可选新增**字段，缺了不影响。
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------- 评价目标 ----------

class FeedbackTargetKind(str, Enum):
    """评价目标的种类。

    ``reply``            一轮**最终回复**（方案 / 草稿 / 修改建议 / 修改完成 /
                        问答 / 澄清 / 失败都算）。
    ``client_failure``   客户端观察到的**传输失败**（不是服务端失败）。
    ``client_stopped``   用户**主动停止**、没有最终回复的那一次请求。
    """

    REPLY = "reply"
    CLIENT_FAILURE = "client_failure"
    CLIENT_STOPPED = "client_stopped"


class FeedbackTarget(BaseModel):
    """公开的评价目标。**只有这四项进协议。**"""

    id: str
    kind: str
    createdAt: str = ""
    available: bool = True


# ---------- 提交 ----------

class FeedbackRating(str, Enum):
    """满意度：三档，必选。"""

    SATISFIED = "satisfied"
    NEUTRAL = "neutral"
    DISSATISFIED = "dissatisfied"


#: 不满意时可选的原因（选填）。
FEEDBACK_REASONS = (
    "not_understood",   # 没理解需求
    "wrong_result",     # 结果不对
    "bad_display",      # 展示不好
    "operation_failed", # 操作失败
    "other",            # 其他
)


class FeedbackSubmissionResponse(BaseModel):
    """提交结果。``submitted=True`` 表示「已经有反馈了」（含幂等重试）。"""

    feedbackId: str
    submitted: bool = True


class FeedbackStatusResponse(BaseModel):
    """当前匿名浏览器自己的已提交标记。

    **只返回自己的**：不返回意见、署名、附件，也不返回别人的反馈。
    """

    submitted: dict[str, str] = Field(default_factory=dict)


# ---------- 客户端事件 ----------

class ClientEventRequest(BaseModel):
    """客户端事件（失败 / 停止观察）。

    **严格字段封闭**（``extra="forbid"``）：客户端**不能**塞任何
    「后台事实」进来 —— 服务端能核实的字段自己读，核实不了的只记成
    受限的客户端观察。
    """

    model_config = ConfigDict(extra="forbid")

    kind: FeedbackTargetKind
    #: 本次尝试的幂等标识（前端生成，同一次事件重试复用同一个值）。
    clientAttemptId: str = Field(min_length=1, max_length=96)
    #: 关联线索（可选）：服务端能核实就用，核实不了就当未知。
    sessionId: str | None = Field(default=None, max_length=128)
    messageId: str | None = Field(default=None, max_length=128)
    taskId: str | None = Field(default=None, max_length=128)
    #: 客户端看到的**粗粒度**观察（受限枚举，不是后台事实）。
    observation: str | None = Field(default=None, max_length=64)


# ---------- 管理端 ----------

class FeedbackStatus(str, Enum):
    """处理状态（维护者手动设置）。"""

    PENDING = "pending"          # 待处理
    IN_PROGRESS = "in_progress"  # 处理中
    RESOLVED = "resolved"        # 已解决


class AdminLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class AdminLoginResponse(BaseModel):
    ok: bool = True
    username: str = ""


class AdminLogoutResponse(BaseModel):
    ok: bool = True


class AdminItemSummary(BaseModel):
    """列表项：摘要字段，不含完整意见与截图。"""

    feedbackId: str
    targetId: str
    kind: str
    rating: str
    reason: str | None = None
    commentExcerpt: str = ""
    contact: str | None = None
    hasAttachment: bool = False
    status: str
    note: str = ""
    createdAt: str = ""
    updatedAt: str = ""
    businessState: str | None = None


class AdminItemListResponse(BaseModel):
    items: list[AdminItemSummary] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


class AdminItemDetail(BaseModel):
    """详情：受限的原消息引用 + 白名单定位快照。

    **不返回**完整 response / snapshot / 原始 JSON / checkpoint / 图表数值。
    """

    feedbackId: str
    targetId: str
    kind: str
    rating: str
    reason: str | None = None
    comment: str = ""
    contact: str | None = None
    status: str
    note: str = ""
    createdAt: str = ""
    updatedAt: str = ""
    hasAttachment: bool = False
    #: 受限引用：原需求与最终回复的**截断**摘录 + 内容指纹。
    requestExcerpt: str = ""
    replyExcerpt: str = ""
    replyFingerprint: str = ""
    #: 原消息被覆盖 / 过期时如实标注，不拿后来的正文冒充旧回复。
    referenceState: str = "ok"
    #: 白名单定位快照（只有结构性信息与缺失项）。
    locator: dict[str, Any] = Field(default_factory=dict)
    missingItems: list[str] = Field(default_factory=list)
    factSource: str = "server"


class AdminItemUpdate(BaseModel):
    """只允许改处理状态与备注 —— **不能**改评价内容。"""

    model_config = ConfigDict(extra="forbid")

    status: FeedbackStatus | None = None
    note: str | None = Field(default=None, max_length=2000)


class AdminItemDeleteResponse(BaseModel):
    """删除结果。

    * ``ok``：删掉了就是 ``true``；
    * ``attachmentRemoved``：**私有截图文件**是否也一并删掉了
      （文件可能早就没了，这时是 ``false``，但记录照样删干净）。
    """

    ok: bool = True
    feedbackId: str = ""
    attachmentRemoved: bool = False
