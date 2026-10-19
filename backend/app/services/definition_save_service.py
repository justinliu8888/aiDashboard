"""内存版 Java 保存服务：先把"确认草稿 → 保存成功 → 进入校验"链路打通。

真实的 Java 后端接入前，先用进程内字典充当存储：
    - save(): 把草稿存起来，分配 definitionId 和递增 revision；
    - get(): 按 ID 读回最新草稿。

保存后返回的信息会写进 AgentResponse.toolRequests，前端 / 后续节点依赖。
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
    """内存版"报表定义保存服务"，接口和 Java 真实服务保持一致。"""

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
