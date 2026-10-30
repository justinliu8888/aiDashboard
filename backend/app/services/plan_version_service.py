"""方案版本记录（工单 #63）：会话里每一版方案正文的可查询序列。

以前方案是「整份覆盖」写进 ``report_design_sessions.plan_json`` 的：旧版正文
只残留在历史消息的响应载荷里，既查不到也还原不回来。没有版本序列，
「底部追加最新方案」「旧卡片留原地当历史」「刷新后还原每一版」都做不出来。

本模块给出一条**可查询的版本序列**：

    - 每次**成功提交**产生一条版本记录：稳定 ID + 版本 + 正文 + 操作标识；
    - **澄清与失败不产生版本**（澄清不是提交，失败更不是）；
    - 重复响应、乱序响应与重连**不重复追加**，也不夺取确认资格；
    - 旧版正文保留且**不改写** —— 只更新「历史 / 当前」的标记；
    - 老会话没有版本历史时，按**实际存储**恢复可用方案，**不伪造**过去的版本。

边界（照既有约定，本票不越界）：

    - **不改确认边界**：新建文字方案的确认与模板编辑计划的接受仍是两种
      独立确认（契约表 §4.2）。版本记录只记「这一版正文是什么」，
      不决定哪一版生效；
    - **不存模拟载荷与业务数值**：正文是纯方案结构（设计意图），
      模拟图表载荷与业务数值仍然只在响应与内存（ADR 0004 §5.1）；
    - **不动方案契约层**（``schemas/report_plan.py``）与方案装配层
      （``report_plan_service.py``）：本模块只读它们写好的东西。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from app.services.session_service import session_service
from app.storage.report_design_repository import (
    PlanVersionRecord,
    ReportDesignRepository,
    plan_version_id,
)
from app.storage.report_design_sqlite import SqliteReportDesignRepository


logger = logging.getLogger(__name__)


#: 版本序列里「这一版是当前方案」的标记键。
IS_CURRENT_KEY = "isCurrent"
#: 版本序列里「这一版已被确认过」的标记键。
CONFIRMED_KEY = "confirmed"
#: 版本序列里「这一版是从会话当前方案恢复出来的」的标记键。
RECOVERED_KEY = "recovered"


def response_plan_version_id(
    session_id: str, plan: Mapping[str, Any] | None
) -> str | None:
    """从**响应里那份方案正文**推出它这一版的稳定 ID。

    聊天响应要能说出「本次提交的是哪一版」—— 前端按这个 ID 去重
    （工单 #66 AC4：重复响应、乱序响应与重连**不重复追加**同一份方案）。

    派生口径与写路径**完全一致**（``plan_version_id``）：同一版方案
    永远是同一个 ID —— 响应里带的与版本序列里那条对得上，前端拿两处
    的 ID 直接比就行，不用再各自推一遍。

    读不出 ``id`` / ``revision`` 就返回 ``None``：**不猜、不编**。
    """
    if not session_id or not isinstance(plan, Mapping):
        return None
    plan_id = str(plan.get("id") or "")
    revision = plan.get("revision")
    if not plan_id:
        return None
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        return None
    return plan_version_id(session_id, plan_id, revision)


class PlanVersionService:
    """方案版本序列的读取方。

    写路径在设计存储里（提交方案时与当前方案同一笔事务追加），
    本类只负责**读**：按会话给出版本序列，并补上「当前 / 已确认 / 恢复」
    这几个读时才知道的标记。
    """

    def __init__(self, repository: ReportDesignRepository | None = None) -> None:
        """注入设计存储；不注入就跟随会话存储（与设计编排同一条连接）。"""
        self._injected_repository = repository
        self._repository: ReportDesignRepository | None = None
        self._repository_source: Any = None

    # ---------- 依赖 ----------

    @property
    def repository(self) -> ReportDesignRepository:
        """设计存储。

        连接与锁都跟会话存储**同一条**、**同一把** —— 与设计编排层一致，
        这样版本序列和当前方案读到的是同一份数据。
        """
        if self._injected_repository is not None:
            return self._injected_repository

        session_repo = session_service.repository
        if self._repository is None or self._repository_source is not session_repo:
            self._repository = SqliteReportDesignRepository(
                connection=session_repo.conn,
                lock=session_repo.lock,
            )
            self._repository.initialize_schema()
            self._repository_source = session_repo
        return self._repository

    def reset_for_tests(self, *, repository: ReportDesignRepository | None = None) -> None:
        """换掉依赖（测试用）。"""
        self._injected_repository = repository
        self._repository = None
        self._repository_source = None

    # ---------- 读取 ----------

    def list_versions(self, session_id: str) -> list[PlanVersionRecord]:
        """按**真实保存顺序**返回会话的方案版本序列。

        返回的每一项都带：

        - ``versionId``：这一版的稳定 ID；
        - ``planId`` / ``planRevision``：这一版是哪个方案的哪个版本；
        - ``reportPlan``：这一版的正文（纯方案结构）；
        - ``operationId``：这一版是**哪次操作**提交上来的；
        - ``sequence``：同一会话里的保存序号（顺序靠它，不靠时间戳）；
        - ``createdAt``：这一版落库的时间；
        - ``isCurrent`` / ``confirmed`` / ``recovered``：读时才知道的标记。

        老会话没有版本历史时，这里按**实际存储**恢复当前那一版可用方案，
        并且标成 ``recovered``；**不伪造**过去的版本（不会凭空多出
        「第 1 版、第 2 版」）。
        """
        if not session_id:
            raise ValueError("session_id 不能为空")

        repository = self.repository
        stored = list(repository.list_plan_versions(session_id))
        current = self._current_plan(session_id)

        versions: list[PlanVersionRecord] = [dict(item) for item in stored]
        if current is not None:
            plan_id = current.get("id")
            revision = current.get("revision")
            if isinstance(plan_id, str) and plan_id and isinstance(revision, int):
                known = any(
                    item["planId"] == plan_id and item["planRevision"] == revision
                    for item in versions
                )
                if not known:
                    # 老会话：没有版本历史。按实际存储恢复当前这一版，
                    # 只恢复这一版 —— 不编造过去的版本序列。
                    next_sequence = max(
                        (int(item.get("sequence") or 0) for item in versions),
                        default=0,
                    ) + 1
                    versions.append(
                        self._recovered_version(session_id, current, next_sequence)
                    )

        for item in versions:
            item[IS_CURRENT_KEY] = bool(
                current
                and item["planId"] == current.get("id")
                and item["planRevision"] == current.get("revision")
            )
            item[CONFIRMED_KEY] = self._is_confirmed(
                session_id, item["planId"], item["planRevision"]
            )
            item.setdefault(RECOVERED_KEY, False)

        versions.sort(key=lambda item: item.get("sequence") or 0)
        return versions

    def latest_version(self, session_id: str) -> PlanVersionRecord | None:
        """会话当前的那一版方案（最新的一版）。

        没有方案时返回 None —— 不返回「空的一版」。
        """
        versions = self.list_versions(session_id)
        if not versions:
            return None
        for item in versions:
            if item.get(IS_CURRENT_KEY):
                return item
        return versions[-1]

    def version_reference(
        self, version: PlanVersionRecord
    ) -> dict[str, Any]:
        """版本引用：只带身份，不带正文。

        需要「指向某一版」而不把正文带出来时用这个（ADR 0004 §5.1：
        载荷里少带一份是一份）。
        """
        return {
            "versionId": version.get("versionId"),
            "planId": version.get("planId"),
            "planRevision": version.get("planRevision"),
        }

    # ---------- 内部 ----------

    def _current_plan(self, session_id: str) -> dict[str, Any] | None:
        """会话当前方案（正文）。没有就返回 None。"""
        state = self.repository.load(session_id)
        if not state:
            return None
        plan = state.get("reportPlan")
        return plan if isinstance(plan, dict) else None

    def _recovered_version(
        self, session_id: str, plan: dict[str, Any], sequence: int
    ) -> PlanVersionRecord:
        """从会话当前方案恢复出一条版本记录。

        恢复出来的这一版带 ``recovered=True``：它是从**实际存储**里读出来的
        那一版，不是编出来的历史版本。落库时间查不到就留空（``createdAt``
        为空字符串），**不猜**。
        """
        plan_id = str(plan.get("id") or "")
        revision = int(plan.get("revision") or 1)
        operation_id = self.repository.find_plan_operation(
            session_id, plan_id, revision
        )
        return {
            "versionId": plan_version_id(session_id, plan_id, revision),
            "sessionId": session_id,
            "planId": plan_id,
            "planRevision": revision,
            "operationId": operation_id,
            "sequence": sequence,
            "reportPlan": dict(plan),
            "createdAt": "",
            RECOVERED_KEY: True,
        }

    def _is_confirmed(
        self, session_id: str, plan_id: str, plan_revision: int
    ) -> bool:
        """这一版是不是已经被确认过（既有确认边界，本票不改）。"""
        try:
            confirmation = self.repository.find_confirmation(
                session_id, plan_id, plan_revision
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "[plan_version] 读确认关系失败，按未确认处理 session=%s", session_id
            )
            return False
        return confirmation is not None


#: 进程内共用的版本序列读取方。
#:
#: 跟随会话存储的**同一条连接、同一把锁**（见 ``PlanVersionService.repository``），
#: 所以「会话当前方案」与「版本序列」读到的是同一份数据 —— 恢复出来的
#: 最新一版与库里当前那一版对得上。
plan_version_service = PlanVersionService()
