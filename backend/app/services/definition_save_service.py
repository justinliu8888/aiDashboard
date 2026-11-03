"""内存版 Java 保存服务：先把"确认草稿 → 保存成功 → 进入校验"链路打通。

真实的 Java 后端接入前，先用进程内字典充当存储：
    - save(): 把草稿存起来，分配 definitionId 和递增 revision；
    - get(): 按 ID 读回最新草稿。

保存后返回的信息会写进 AgentResponse.toolRequests，前端 / 后续节点依赖。

**这是内存版**：进程一重启，`_store` 全丢（见 07_会话管理系统方案.md）。
生产上的草稿保存与撤销走**草稿工作区**
（`app/services/draft_workspace.py` 的 `DraftWorkspace`，落 SQLite）：
    - 一次「接受修改」= 一个可撤销的修改单位（版本 + 历史 + 撤销指针，同一事务）；
    - `revision`（编辑链版本）与 `savedRevision`（显式保存版）**不同义**。

本模块的公开方法（`save` / `get`）与返回字段
（`definitionId` / `revision` / `policyDraft` / `definitionDraft` / `history`）
**保持不变**，现有调用方（`agent_service` / `session_fact_service` /
`recorded_progress_service`）的行为不受影响。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


logger = logging.getLogger(__name__)


@dataclass
class SavedDefinition:
    definitionId: str
    revision: int
    policyDraft: dict[str, Any]
    definitionDraft: dict[str, Any]
    history: list[dict[str, Any]] = field(default_factory=list)


class DefinitionSaveService:
    """内存版"报表定义保存服务"，接口和 Java 真实服务保持一致。

    注意：这是**内存版**（进程重启即丢）。生产保存走 `DraftWorkspace`
    （`app/services/draft_workspace.py`），本类的公开方法与字段保持不变。
    """

    def __init__(self) -> None:
        self._store: dict[str, SavedDefinition] = {}
        self._lock = Lock()

    def save(
        self,
        *,
        definitionId: str | None,
        policyDraft: dict[str, Any],
        definitionDraft: dict[str, Any],
    ) -> SavedDefinition:
        """保存草稿。definitionId 为空时新建；非空时在原记录上追加 revision。"""
        with self._lock:
            if definitionId and definitionId in self._store:
                saved = self._store[definitionId]
                saved.history.append(
                    {
                        "revision": saved.revision,
                        "policyDraft": saved.policyDraft,
                        "definitionDraft": saved.definitionDraft,
                    }
                )
                saved.revision += 1
                saved.policyDraft = policyDraft
                saved.definitionDraft = definitionDraft
                logger.info(
                    "[save_service] updated definitionId=%s new_revision=%s",
                    saved.definitionId,
                    saved.revision,
                )
                return saved

            new_id = definitionId or f"def_{uuid.uuid4().hex[:8]}"
            saved = SavedDefinition(
                definitionId=new_id,
                revision=1,
                policyDraft=policyDraft,
                definitionDraft=definitionDraft,
            )
            self._store[new_id] = saved
            logger.info(
                "[save_service] created definitionId=%s revision=%s",
                saved.definitionId,
                saved.revision,
            )
            return saved

    def get(self, definitionId: str) -> SavedDefinition | None:
        return self._store.get(definitionId)


definition_save_service = DefinitionSaveService()
