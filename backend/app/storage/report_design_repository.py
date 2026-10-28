"""设计持久化的抽象契约（#19 首波 C）。

这一层只说清「独立设计会话存储」要提供什么：
    - 方案（ReportPlan 的 JSON 形态）
    - 设计阶段（designPhase）
    - 一次设计操作（认领 → 提交 / 失败）

职责边界：
    - 领域语义由 A 的 `apply_candidate` 校验，C 只校验 JSON 可保存、
      基础版本一致、操作状态一致；
    - 模型调用在 B，模型工作在认领与提交之间、事务之外；
    - 聊天 / 会话写入由协调者用 `write_turn(connection)` 回调注入，
      回调必须自己在传入连接上写，不能调用会自行 commit 的旧 Repository 方法。

首波只落方案、阶段与操作三样东西，不建模板参考库，也不建正反例资产表。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from typing import Any, Protocol, TypedDict, runtime_checkable


# ---------- 设计阶段 ----------
#
# 阶段只对新建流程生效；旧草稿的 state 与只读问答的 turnKind 保持原义。

DESIGN_PHASE_PLAN_NEEDED = "PLAN_NEEDED"
DESIGN_PHASE_PLAN_WAITING_CONFIRM = "PLAN_WAITING_CONFIRM"
DESIGN_PHASE_TEMPLATE_READY = "TEMPLATE_READY"

# 首波能落库的阶段：模板生成属于工单 02。
DESIGN_PHASES = (DESIGN_PHASE_PLAN_NEEDED, DESIGN_PHASE_PLAN_WAITING_CONFIRM)


# ---------- 操作状态 ----------

OPERATION_PROCESSING = "PROCESSING"
OPERATION_SUCCEEDED = "SUCCEEDED"
OPERATION_FAILED = "FAILED"


# ---------- 认领结果 ----------

CLAIM_KIND_CLAIMED = "claimed"
CLAIM_KIND_DUPLICATE = "duplicate"
CLAIM_KIND_CONFLICT = "conflict"

# ---------- 提交结果 ----------

COMMIT_KIND_COMMITTED = "committed"
COMMIT_KIND_CONFLICT = "conflict"


class LoadResult(TypedDict):
    """一个会话的设计状态。"""

    designPhase: str
    reportPlan: dict[str, Any] | None


class ClaimResult(TypedDict, total=False):
    """认领结果。

    kind 区分 claimed / duplicate / conflict：
        claimed   本次消息由我方认领，可以去做模型工作；
        duplicate 同一条消息已经处理过，status 说明处理中 / 成功 / 失败；
        conflict  基础版本对不上，没有认领，currentPlan 是当前成功对象。
    """

    kind: str
    operationId: str | None
    sessionId: str
    messageId: str
    expectedRevision: int | None
    status: str
    designPhase: str
    currentPlan: dict[str, Any] | None
    currentRevision: int | None
    planId: str | None
    planRevision: int | None
    plan: dict[str, Any] | None
    error: dict[str, str] | None
    reason: str


class CommitResult(TypedDict, total=False):
    """提交结果。

    kind 区分 committed / conflict：
        committed 设计对象、阶段、操作结果与 write_turn 回调已同事务落库；
        conflict  基础版本已被别的操作推进，本次结果不提交，
                  currentPlan 是仍然保留的上一成功方案。
    """

    kind: str
    operationId: str
    sessionId: str
    planId: str
    revision: int
    designPhase: str
    plan: dict[str, Any] | None
    alreadyCommitted: bool
    currentPlan: dict[str, Any] | None
    currentRevision: int | None
    reason: str
    error: dict[str, str] | None


# write_turn：协调者注入的同事务消息 / 会话写入回调。
# 回调拿到的是本模块正在使用的那条连接，自己不 commit。
WriteTurn = Callable[[sqlite3.Connection], Any]


# ---------- 异常 ----------


class DesignStoreError(Exception):
    """设计存储的调用方式有误（编程错误，不是业务结果）。"""


class DesignPlanInvalidError(DesignStoreError):
    """提交的方案不是可保存的 JSON 形态，或与基础版本不一致。"""


class DesignOperationStateError(DesignStoreError):
    """操作不存在，或当前状态不允许这一步。"""


# ---------- 接口 ----------


@runtime_checkable
class ReportDesignRepository(Protocol):
    """独立设计会话存储的接口。"""

    def initialize_schema(self) -> None:
        """建好 `report_design_` 前缀的表。不动旧 schema，不迁移旧数据。"""

    def load(self, session_id: str) -> LoadResult | None:
        """读会话的设计状态；没进过设计流程时返回 None。"""

    def claim_operation(
        self, session_id: str, message_id: str, expected_revision: int | None
    ) -> ClaimResult:
        """认领一条消息对应的设计操作。

        expected_revision 为 None 表示新建方案；否则必须等于当前成功方案的
        revision。结果区分 claimed / duplicate / conflict。
        """

    def commit_plan(
        self,
        operation_id: str,
        expected_revision: int | None,
        plan: Mapping[str, Any],
        write_turn: WriteTurn | None = None,
    ) -> CommitResult:
        """提交校验后的方案。

        设计对象、阶段、操作结果与 write_turn 回调同事务提交；回调抛错整体回滚，
        上一成功方案保留。结果区分 committed / conflict。
        """

    def fail_operation(
        self, operation_id: str, safe_error: str | Mapping[str, Any]
    ) -> None:
        """把操作标记为失败，保留上一成功方案。"""
