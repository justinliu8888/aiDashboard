"""会话服务层：把「一轮对话」落成「一条会话 + 一对消息」。

三个 id 的分工（见 07_会话管理系统方案.md §5.1）：
    sessionId        后端生成，一条对话从建到删永不改变，数据库主键
    conversationId   前端生成，一次请求线程；停止生成时会换新
    messageId        前端生成，本条用户消息的幂等键

落库规则（方案 §5.4）：
    1. user + assistant 成对写，且都在请求成功完成后写；
    2. __advance__ 是内部控制消息，不作为 user 消息入库，但其 assistant 回复要留；
    3. user 消息只存用户实际输入的原文；
    4. 按 (session_id, client_msg_id) 幂等 upsert，挡住重发 / 重试造成的重复。

编辑态跨轮状态（节点 5.5）：
    pendingEditPlan     待确认的编辑计划（独立列 sessions.pending_edit_plan）
    clarificationCount  同一项待定修改已追问的轮数（存在 sessions.snapshot 里）
待确认计划独立成列：取走计划必须是「条件删除」的原子操作，
多 worker / 多进程同时点「接受修改」时只能有一次生效。
澄清轮次仍是 snapshot 里的读-改-写，粒度比计划粗，不影响正确性。
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime
from typing import Any, Callable

from pydantic import ValidationError

from app.config import get_settings
from app.schemas.agent import (
    ADVANCE_MESSAGE,
    TURN_KIND_QUERY_ANSWER,
    AgentRequest,
    AgentResponse,
)
from app.schemas.session import (
    CLARIFICATION_COUNT_KEY,
    CLARIFICATION_KEY_KEY,
    CLARIFICATION_QUESTION_KEY,
    PENDING_PLAN_KEY,
    EditClarification,
    PendingEditPlan,
)
from app.services.mock_data_service import render_template, room_id_of_definition
from app.services.preview_service import preview_service
from app.storage.base import SessionRepository
from app.storage.models import SessionRecord, WorkspaceRecord
from app.storage.sqlite_store import SqliteSessionRepository


logger = logging.getLogger(__name__)

# 标题取用户首条消息的前 N 字
TITLE_MAX_LEN = 20

# 编辑态跨轮状态在 snapshot 里的键。重建 snapshot 时要原样带回去，
# 否则一轮对话就会把上一轮存下的澄清进度冲掉。
# 待确认计划不在这里：它已经独立成列（sessions.pending_edit_plan）。
EDIT_STATE_KEYS = (
    CLARIFICATION_COUNT_KEY,
    CLARIFICATION_KEY_KEY,
    CLARIFICATION_QUESTION_KEY,
)

# 澄清轮次的两种结果。只有「asked」占用户澄清轮次。
CLARIFICATION_ASKED = "asked"
CLARIFICATION_TECHNICAL_FAILURE = "technical_failure"


def resolve_placeholders(definition: dict[str, Any]) -> dict[str, Any]:
    """把草稿里的 {{roomName}} 之类占位符换成真实值。

    前端不认占位符，直接显示会露出 `{{roomName}}`。存快照时统一解析一次，
    恢复会话拿到的就是成品文本；老会话重新落库时也会被顺手修好。

    草稿是**服务端存下来的历史数据**，结构可能已经不对（老版本写的、
    手工改过的、被截断的）。这里逐层做类型判断：解析不了的那部分跳过，
    不能因为一个坏区块就让整个会话读不出来 —— 会话读取挂掉时，前端
    刷新就是一片空白，用户连「无法确认」都看不到。
    """
    room_id = room_id_of_definition(definition)
    regions = definition.get("regions") or []
    if not isinstance(regions, list):
        return definition
    for region in regions:
        if not isinstance(region, dict):
            continue
        component = region.get("component")
        content = component.get("content") if isinstance(component, dict) else None
        if isinstance(content, dict):
            template = content.get("template")
            if isinstance(template, str) and "{{" in template:
                content["template"] = render_template(template, room_id)
        name = region.get("name")
        if isinstance(name, str) and "{{" in name:
            region["name"] = render_template(name, room_id)
    return definition


class SessionService:
    """会话编排：id 生成、消息落库、快照维护。"""

    def __init__(self, repository: SessionRepository) -> None:
        self._repo = repository
        # snapshot 整块存一个 JSON 字段，跨轮状态也整块存。同一会话的两次写
        # （落库 vs 更新待确认计划）必须串行，否则后写的会把先写的覆盖掉。
        self._lock = threading.RLock()

    @property
    def repository(self) -> SessionRepository:
        return self._repo

    # ---------- id ----------

    @staticmethod
    def new_session_id() -> str:
        """铸一个会话 id。"""
        return f"sess_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def new_plan_id() -> str:
        """铸一个待确认计划 id。"""
        return f"plan_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def resolve_session_id(request: AgentRequest) -> str:
        """从请求里解析会话 key。

        前端传了 sessionId 就用它；没传则退回 conversationId，
        等价于「一条请求一个会话」的旧行为。
        """
        return request.sessionId or request.conversationId

    # ---------- 落库 ----------

    def record_turn(
        self,
        *,
        session_id: str,
        user_text: str,
        response: AgentResponse,
        client_msg_id: str | None = None,
        request: AgentRequest | None = None,
    ) -> SessionRecord | None:
        """把一轮对话写入存储。返回落库后的会话记录。"""
        is_advance = user_text.strip() == ADVANCE_MESSAGE
        try:
            # 0. 先确保会话行存在（消息表有指向会话行的外键）
            title = self._derive_title(session_id, user_text, is_advance)
            self._repo.upsert_session(
                session_id=session_id,
                state=response.state.value,
                title=title,
            )

            # 1. user 消息：内部控制消息不入库
            if not is_advance:
                self._repo.append_message(
                    session_id=session_id,
                    role="user",
                    content=user_text,
                    clientMsgId=client_msg_id,
                )

            # 2. assistant 消息：payload 存整个响应，恢复时可直接回放渲染
            self._repo.append_message(
                session_id=session_id,
                role="assistant",
                content=response.reply,
                payload=response.model_dump(mode="json"),
                clientMsgId=self._reply_msg_id(client_msg_id),
            )

            # 3. 更新会话
            count = len(self._repo.list_messages(session_id))
            return self._commit_session(
                session_id=session_id,
                response=response,
                request=request,
                message_count=count,
            )
        except Exception:  # noqa: BLE001
            # 落库失败不能影响主链路
            logger.exception("[session_service] 落库失败 sessionId=%s", session_id)
            return None

    def _commit_session(
        self,
        *,
        session_id: str,
        response: AgentResponse,
        request: AgentRequest | None,
        message_count: int,
    ) -> SessionRecord:
        """写回会话行。

        编辑态跨轮状态（待确认计划 / 澄清轮数）在写库前一刻才从库里读出来带回去：
        snapshot 是整块重建的，读早了会被并发的写覆盖掉。

        只读查询轮（``turnKind == "query_answer"``）单独处理：
        它允许追加聊天消息，但**不重建业务对象** —— 草稿、revision、
        已保存定义、预览、待确认计划、澄清轮数一概不动，
        也不能把客户端那份可能过旧的快照写回覆盖服务端最新的草稿。
        """
        with self._lock:
            current = self._repo.get_session(session_id)
            if response.turnKind == TURN_KIND_QUERY_ANSWER:
                return self._commit_query_turn(
                    session_id=session_id,
                    response=response,
                    request=request,
                    message_count=message_count,
                    current=current,
                )
            snapshot = self._build_snapshot(request, response)
            previous_snapshot = (current.snapshot or {}) if current else {}
            previous = previous_snapshot.get("definition")
            incoming = (snapshot or {}).get("definition")
            if isinstance(previous, dict) and not isinstance(incoming, dict):
                # 本轮没有带回草稿（例如待确认期间的一次提问被拒）：
                # 保留库里那份，不能因为一次交互就把草稿丢掉。
                snapshot["definition"] = previous
            elif isinstance(previous, dict) and isinstance(incoming, dict):
                previous_revision = int((previous.get("metadata") or {}).get("revision") or 0)
                incoming_revision = int((incoming.get("metadata") or {}).get("revision") or 0)
                if previous_revision > incoming_revision:
                    # 迟到的只读/重复确认回复不能把已应用的新草稿回滚。
                    snapshot["definition"] = previous
            snapshot = self._carry_over_business_objects(snapshot, previous_snapshot)
            # 老会话的待确认计划还躺在 snapshot.pendingPlan 里：
            # snapshot 是整块重建的，不先搬走就会把计划弄丢。
            self._migrate_legacy_pending_plan(session_id)
            edit_state = self._read_edit_state(session_id)
            if edit_state:
                snapshot = {**(snapshot or {}), **edit_state}
            return self._repo.upsert_session(
                session_id=session_id,
                state=response.state.value,
                designSessionId=response.designSessionId,
                reportDefinitionId=self._extract_definition_id(response),
                previewRunId=self._extract_preview_run_id(response),
                snapshot=snapshot,
                messageCount=message_count,
            )

    @staticmethod
    def _carry_over_business_objects(
        snapshot: dict[str, Any] | None, previous: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """本轮没带回的业务对象，沿用库里那份。

        只读问答轮、待确认期间的拒绝回复，都可能不带草稿回传。
        这时候要把库里的草稿、已保存定义和预览原样带着，
        不能因为一次交互就让恢复出来的会话少东西。
        """
        if not previous:
            return snapshot
        result = dict(snapshot or {})
        for key in ("definition", "policy", "lastReportRun", "previewPayload"):
            if not isinstance(result.get(key), dict) and isinstance(previous.get(key), dict):
                result[key] = previous[key]
        return result

    def _commit_query_turn(
        self,
        *,
        session_id: str,
        response: AgentResponse,
        request: AgentRequest | None,
        message_count: int,
        current: SessionRecord | None,
    ) -> SessionRecord:
        """只读查询轮落库：只加消息，业务对象原样保留。

        刻意**不用** ``response.domainObjects`` 重建快照：
        查询轮的响应里带的是服务端可信对象，回退到请求快照就等于
        用旧客户端快照重建业务状态。
        """
        # 老会话的待确认计划还躺在 snapshot.pendingPlan 里，先搬走再读。
        self._migrate_legacy_pending_plan(session_id)
        current = self._repo.get_session(session_id) or current
        snapshot = dict(current.snapshot or {}) if current else {}
        if request is not None:
            collected = dict(getattr(request, "collectedInfo", None) or {})
            if collected:
                snapshot["collectedInfo"] = collected
        return self._repo.upsert_session(
            session_id=session_id,
            state=current.state if current else response.state.value,
            designSessionId=None,
            reportDefinitionId=None,
            previewRunId=None,
            snapshot=snapshot,
            messageCount=message_count,
        )

    @staticmethod
    def _reply_msg_id(client_msg_id: str | None) -> str | None:
        if not client_msg_id:
            return None
        return f"{client_msg_id}:reply"

    def _derive_title(
        self, session_id: str, user_text: str, is_advance: bool
    ) -> str | None:
        """首条真实用户消息作为标题；后续不改。"""
        if is_advance or not user_text.strip():
            return None
        existing = self._repo.get_session(session_id)
        if existing and existing.title and existing.title != "新对话":
            return None
        title = user_text.strip().replace("\n", " ")
        return title[:TITLE_MAX_LEN]

    @staticmethod
    def _extract_definition_id(response: AgentResponse) -> str | None:
        draft = response.domainObjects.reportDefinitionDraft
        if draft is not None:
            metadata = getattr(draft, "metadata", None)
            if metadata is not None:
                identifier = getattr(metadata, "id", None)
                if identifier:
                    return identifier
        for tool in response.toolRequests:
            if tool.type == "SAVE_DEFINITION_DRAFT":
                identifier = (tool.arguments or {}).get("definitionId")
                if identifier:
                    return str(identifier)
        return None

    @staticmethod
    def _extract_preview_run_id(response: AgentResponse) -> str | None:
        preview = response.domainObjects.reportRunPreview
        return preview.reportRunId if preview is not None else None

    def _build_snapshot(
        self,
        request: AgentRequest | None,
        response: AgentResponse,
    ) -> dict[str, Any] | None:
        """组装恢复现场需要的快照。

        取值优先级：本次响应的 domainObjects > 请求带回来的 snapshots。
        """
        snapshot: dict[str, Any] = {}

        definition = response.domainObjects.reportDefinitionDraft
        policy = response.domainObjects.reportPolicyDraft
        last_run = response.domainObjects.reportRunPreview

        if request is not None:
            incoming = request.snapshots
            if definition is None and incoming.reportDefinition is not None:
                definition = incoming.reportDefinition
            if policy is None and incoming.reportPolicy is not None:
                policy = incoming.reportPolicy
            if last_run is None and incoming.lastReportRun is not None:
                last_run = incoming.lastReportRun

        if definition is not None:
            snapshot["definition"] = resolve_placeholders(definition.model_dump(mode="json"))
        if policy is not None:
            snapshot["policy"] = policy.model_dump(mode="json")
        if last_run is not None:
            snapshot["lastReportRun"] = last_run.model_dump(mode="json")
            # 预览数据在内存态里，后端重启后 /api/report/preview/{runId} 会 404，
            # 所以在这里缓存一份，恢复会话时直接用。
            payload = preview_service.get_result_payload(last_run.reportRunId)
            if payload is not None:
                snapshot["previewPayload"] = payload

        editing_target = (response.debug or {}).get("editingTargetRegionId")
        if editing_target is not None:
            snapshot["editingTargetRegionId"] = editing_target

        # 路由层已收集的槽位。下一轮恢复时带回 Deep Route，
        # 避免重新问用户已经答过的问题。
        if request is not None:
            # 记录这条会话的授权范围。只读查询要按它核对事实来源，
            # 范围对不上就不能把里面的草稿说出去。
            if "scope" not in snapshot:
                snapshot["scope"] = {
                    "projectId": request.context.projectId,
                    "subjectRef": request.context.subjectRef,
                    "subjectId": request.context.subjectId,
                }

            collected = dict(getattr(request, "collectedInfo", None) or {})
            if collected:
                snapshot["collectedInfo"] = collected

        return snapshot or None

    def _read_edit_state(self, session_id: str) -> dict[str, Any]:
        """取回 snapshot 里的编辑态跨轮状态（待确认计划 / 澄清轮数）。"""
        record = self._repo.get_session(session_id)
        if record is None or not record.snapshot:
            return {}
        snapshot = record.snapshot
        return {key: snapshot[key] for key in EDIT_STATE_KEYS if key in snapshot}

    def _mutate_snapshot(
        self,
        session_id: str,
        mutate: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        """对会话 snapshot 做一次读-改-写，返回写回后的 snapshot。

        snapshot 整块存一个 JSON 字段，没有单列可改，只能读全量再写回。
        同一会话的写必须串行，否则后写的会覆盖先写的。
        """
        with self._lock:
            record = self._repo.get_session(session_id)
            snapshot = dict(record.snapshot) if record and record.snapshot else {}
            updated = mutate(snapshot)
            state = record.state if record else "ANALYZING"
            self._repo.upsert_session(
                session_id=session_id,
                state=state,
                snapshot=updated,
            )
            return updated

    # ---------- 已收集信息 ----------

    def load_collected_info(self, session_id: str) -> dict[str, Any]:
        """取回上一轮累计收集到的槽位。"""
        record = self._repo.get_session(session_id)
        if record is None or not record.snapshot:
            return {}
        collected = record.snapshot.get("collectedInfo")
        if not isinstance(collected, dict):
            return {}
        return dict(collected)

    # ---------- 选择锁 ----------

    def save_pending_choice(
        self, session_id: str, choice: dict[str, Any] | None
    ) -> None:
        """写 / 清选择锁。

        选择锁存在库里而不是内存里，这样后端重启后恢复历史会话仍然能拿到锁，
        不会出现「前端放行自由输入、后端拒收」的前后端不一致。
        """
        try:
            self._repo.set_pending_choice(session_id, choice)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[session_service] 写选择锁失败 sessionId=%s", session_id
            )

    def load_pending_choice(self, session_id: str) -> dict[str, Any] | None:
        record = self._repo.get_session(session_id)
        return record.pendingChoice if record else None

    # ---------- 编辑态：待确认计划 ----------

    def save_pending_plan(
        self, session_id: str, plan: PendingEditPlan
    ) -> PendingEditPlan:
        """保存待确认的编辑计划，等用户点「接受修改」。

        计划连同草稿 revision 一起存：接受前要核对草稿有没有变过，
        变了就不能照旧序号执行（方案 §三「确认对象可核对」）。
        """
        if not plan.planId.strip():
            raise ValueError("待确认计划必须有 planId")
        if not plan.createdAt:
            plan = plan.model_copy(
                update={"createdAt": datetime.now().isoformat(timespec="seconds")}
            )
        self._repo.save_pending_edit_plan(session_id, plan.model_dump(mode="json"))
        return plan

    def load_pending_plan(self, session_id: str) -> PendingEditPlan | None:
        """取回当前待确认计划；没有或已损坏返回 None。"""
        record = self._repo.get_session(session_id)
        raw = record.pendingEditPlan if record else None
        if not isinstance(raw, dict):
            # 老会话的计划还留在 snapshot.pendingPlan 里，迁移一次再读。
            raw = self._migrate_legacy_pending_plan(session_id)
        if not isinstance(raw, dict):
            return None
        try:
            return PendingEditPlan.model_validate(raw)
        except ValidationError:
            logger.warning(
                "[session_service] 待确认计划解析失败，已丢弃 sessionId=%s", session_id
            )
            return None

    def save_pending_plan_if_absent(
        self, session_id: str, plan: PendingEditPlan
    ) -> PendingEditPlan:
        """保留首个待确认计划，避免迟到候选覆盖已展示的计划。

        「保留首个」由存储层保证：多个进程同时提交时，
        只有一份计划能进库，后来的拿到的是先到的那份。
        """
        if not plan.planId.strip():
            raise ValueError("待确认计划必须有 planId")
        if not plan.createdAt:
            plan = plan.model_copy(
                update={"createdAt": datetime.now().isoformat(timespec="seconds")}
            )
        raw = self._repo.save_pending_edit_plan_if_absent(
            session_id, plan.model_dump(mode="json")
        )
        return PendingEditPlan.model_validate(raw)

    def clear_pending_plan(self, session_id: str) -> None:
        """清掉待决计划：接受之后、继续调整之后、取消之后。"""
        self._repo.clear_pending_edit_plan(session_id)
        # 老会话的 snapshot 里可能还留着一份，一并清掉，避免两处真相。
        self._mutate_snapshot(
            session_id,
            lambda snapshot: {
                key: value
                for key, value in snapshot.items()
                if key != PENDING_PLAN_KEY
            },
        )

    def take_pending_plan(
        self, session_id: str, plan_id: str
    ) -> PendingEditPlan | None:
        """原子取走待决计划，防止重复点击执行两次。

        取走是数据库层面的条件删除，跨进程也只成功一次。
        """
        raw = self._repo.take_pending_edit_plan(session_id, plan_id)
        if raw is None:
            # 老会话的计划还在 snapshot 里：先迁到独立列再取。
            legacy = self._migrate_legacy_pending_plan(session_id)
            if not isinstance(legacy, dict):
                return None
            if str(legacy.get("planId") or "") != str(plan_id):
                return None
            raw = self._repo.take_pending_edit_plan(session_id, plan_id)
            if raw is None:
                return None
        try:
            return PendingEditPlan.model_validate(raw)
        except ValidationError:
            logger.warning(
                "[session_service] 待确认计划解析失败，已丢弃 sessionId=%s", session_id
            )
            return None

    def _migrate_legacy_pending_plan(self, session_id: str) -> dict[str, Any] | None:
        """把老 snapshot 里的待确认计划搬到独立列。

        老版本把计划存在 sessions.snapshot.pendingPlan。独立成列之后，
        读写都走列；这里做一次兼容搬运，避免升级后老会话的计划丢失。
        """
        record = self._repo.get_session(session_id)
        snapshot = record.snapshot if record else None
        legacy = snapshot.get(PENDING_PLAN_KEY) if isinstance(snapshot, dict) else None
        if not isinstance(legacy, dict):
            return None
        self._repo.save_pending_edit_plan(session_id, legacy)
        self._mutate_snapshot(
            session_id,
            lambda current: {
                key: value
                for key, value in current.items()
                if key != PENDING_PLAN_KEY
            },
        )
        return legacy

    def is_pending_plan_stale(
        self, session_id: str, current_revision: int | None
    ) -> bool:
        """待确认计划是否已过期。

        草稿在计划生成之后又变过（或者说不清当前版本），就不能照旧计划执行，
        得重新核对目标和序号。库里没有待确认计划时不算过期。
        """
        plan = self.load_pending_plan(session_id)
        if plan is None:
            return False
        if current_revision is None:
            return True
        return plan.draftRevision != current_revision

    # ---------- 编辑态：澄清轮次 ----------

    def record_clarification(
        self,
        session_id: str,
        *,
        key: str,
        question: str = "",
        outcome: str = CLARIFICATION_ASKED,
    ) -> EditClarification:
        """记一次编辑态的澄清结果。

        outcome=asked             真的把具体追问抛给了用户，轮次 +1
        outcome=technical_failure 工具或模型故障，不占用户澄清轮次，计数不动

        换 key 表示换了一项待定修改（新需求），轮次从零重新计数。
        """
        if not key.strip():
            raise ValueError("澄清计数必须带 key，才能区分是哪一项待定修改")

        if outcome == CLARIFICATION_TECHNICAL_FAILURE:
            # 故障不算用户澄清：不动计数，也不改 key。
            current = self.load_clarification(session_id)
            return current or EditClarification(key=key, rounds=0)

        def mutate(snapshot: dict[str, Any]) -> dict[str, Any]:
            same_item = snapshot.get(CLARIFICATION_KEY_KEY) == key
            rounds = int(snapshot.get(CLARIFICATION_COUNT_KEY) or 0) if same_item else 0
            snapshot[CLARIFICATION_KEY_KEY] = key
            snapshot[CLARIFICATION_COUNT_KEY] = rounds + 1
            snapshot[CLARIFICATION_QUESTION_KEY] = question
            return snapshot

        snapshot = self._mutate_snapshot(session_id, mutate)
        return EditClarification(
            key=key,
            rounds=int(snapshot.get(CLARIFICATION_COUNT_KEY) or 0),
            lastQuestion=question,
        )

    def load_clarification(self, session_id: str) -> EditClarification | None:
        """取回当前这项待定修改的澄清进度。"""
        snapshot = self._read_edit_state(session_id)
        if CLARIFICATION_KEY_KEY not in snapshot:
            return None
        return EditClarification(
            key=str(snapshot.get(CLARIFICATION_KEY_KEY) or ""),
            rounds=int(snapshot.get(CLARIFICATION_COUNT_KEY) or 0),
            lastQuestion=str(snapshot.get(CLARIFICATION_QUESTION_KEY) or ""),
        )

    def can_ask_clarification(self, session_id: str, *, key: str) -> bool:
        """还能不能再追问一次（同一项修改最多三轮）。"""
        state = self.load_clarification(session_id)
        if state is None or state.key != key:
            return True
        return state.rounds < state.maxRounds

    def reset_clarification(self, session_id: str) -> None:
        """清掉追问计数：这项修改已经说清、或者用户已经放弃了。"""

        def mutate(snapshot: dict[str, Any]) -> dict[str, Any]:
            snapshot.pop(CLARIFICATION_COUNT_KEY, None)
            snapshot.pop(CLARIFICATION_KEY_KEY, None)
            snapshot.pop(CLARIFICATION_QUESTION_KEY, None)
            return snapshot

        self._mutate_snapshot(session_id, mutate)

    # ---------- 项目 ----------

    def list_workspaces(self) -> list[WorkspaceRecord]:
        return self._repo.list_workspaces()
    def create_workspace(
        self,
        *,
        name: str,
        description: str = "",
        subjectRef: str | None = None,
        subjectId: str | None = None,
    ) -> WorkspaceRecord:
        workspace_id = f"ws_{uuid.uuid4().hex[:8]}"
        return self._repo.create_workspace(
            id=workspace_id,
            name=name,
            description=description,
            subjectRef=subjectRef,
            subjectId=subjectId,
        )

    def update_workspace(
        self, workspace_id: str, *, name: str | None = None, description: str | None = None
    ) -> WorkspaceRecord | None:
        return self._repo.update_workspace(
            workspace_id, name=name, description=description
        )

    def delete_workspace(self, workspace_id: str) -> bool:
        return self._repo.delete_workspace(workspace_id)


_repository = SqliteSessionRepository(get_settings().session_db_path)
session_service = SessionService(_repository)
