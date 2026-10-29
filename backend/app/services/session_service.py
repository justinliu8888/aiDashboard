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

import json
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


def _dump_snapshot(value: Any) -> str | None:
    """把快照 / payload 序列化成 JSON 文本。"""
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover - 极端情况
        return None


def _response_preview_context(response: AgentResponse) -> dict[str, Any] | None:
    """响应里带的**示意上下文**（没有就 None）。

    上下文只有定义版本、时间窗、粒度这些**结构**，没有业务数值 ——
    所以它可以落库（ADR 0004 允许保留 previewContext）。
    """
    preview_payload = response.reportPreviewPayload
    if not isinstance(preview_payload, dict):
        return None
    context = preview_payload.get("previewContext")
    return context if isinstance(context, dict) else None


def _definition_is_template_mode(definition: Any) -> bool:
    """这份定义是不是**模板态**。

    模板态的定义带 ``metadata.templateBinding``（模板转换层落的）。

    模板态下渲染出来的业务数值**一律是模拟业务数据**（ADR 0004
    「模板态一律用模拟业务数据渲染」），后台的试运行也不例外 ——
    服务端自己的回复就写着「本次试运行同样使用能耗模拟数据，
    不是生产事实表」。
    """
    if definition is None:
        return False
    dump = getattr(definition, "model_dump", None)
    if callable(dump):
        definition = dump(mode="json")
    if not isinstance(definition, dict):
        return False
    metadata = definition.get("metadata")
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get("templateBinding"))


def _payload_is_simulated(payload: Any) -> bool:
    """这份结果载荷自己是不是**模拟数据**（带来源标记）。

    造数器（``simulated_data_service``）产出的载荷带 ``dataSource ==
    "simulated"``：区域级（``regions[].dataSource``）与载荷级
    （``regions[].payload.dataSource``）两种形态都认。带这个标记的结果
    一律不落库。
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("dataSource") == "simulated":
        return True
    regions = payload.get("regions")
    if not isinstance(regions, list):
        return False
    for region in regions:
        if not isinstance(region, dict):
            continue
        if region.get("dataSource") == "simulated":
            return True
        region_payload = region.get("payload")
        if (
            isinstance(region_payload, dict)
            and region_payload.get("dataSource") == "simulated"
        ):
            return True
    return False


def _persisted_response_payload(response: AgentResponse) -> dict[str, Any]:
    """消息落库用的响应体：**不含模拟业务结果**。

    ADR 0004：模拟业务数据只存在于响应与内存，**不落盘**。这一轮响应
    里带着 ``reportPreviewPayload``（区域模拟数值），落库时要把数值
    去掉 —— 但**保留** ``previewContext``（那是上下文：定义版本、时间窗、
    粒度，不是业务数值），恢复会话时靠它把同样的数重新生成出来。

    注意：这里只动 ``reportPreviewPayload``。既有的 ``previewPayload``
    （试运行结果）语义不变，那是另一条链路。
    """
    payload = response.model_dump(mode="json")
    preview_payload = payload.get("reportPreviewPayload")
    if isinstance(preview_payload, dict):
        context = preview_payload.get("previewContext")
        payload["reportPreviewPayload"] = (
            {"previewContext": context} if isinstance(context, dict) else None
        )
    return payload


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
                state=self._state_to_store(session_id, response),
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
            # （模拟业务数值不进库，见 _persisted_response_payload）。
            self._repo.append_message(
                session_id=session_id,
                role="assistant",
                content=response.reply,
                payload=_persisted_response_payload(response),
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

    def _state_to_store(self, session_id: str, response: AgentResponse) -> str:
        """这一轮要落库的业务状态。

        正常业务轮：落响应里的状态 —— 那是状态机推进的结果。

        只读查询轮：**沿用库里那份**。查询轮的响应状态取的是客户端带来的
        ``request.state``（为了「本轮不推进」），如果拿它落库，一次提问
        就能把会话从「待确认」改成「分析中」—— 业务状态是服务端事实，
        不能被客户端声称的状态改写。库里还没有行时（新会话）才用响应状态，
        那是在建会话，不是在改状态。
        """
        if response.turnKind != TURN_KIND_QUERY_ANSWER:
            return response.state.value
        current = self._repo.get_session(session_id)
        if current is not None and current.state:
            return current.state
        return response.state.value

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
            previous_snapshot = (current.snapshot or {}) if current else {}
            snapshot = self._build_snapshot(
                request,
                response,
                previous_snapshot=previous_snapshot,
            )
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

        设计阶段与文字方案也在其中：一次失败轮、一次提问轮之后，
        会话详情里那份方案必须还在 —— 恢复不能靠聊天正文。
        """
        if not previous:
            return snapshot
        result = dict(snapshot or {})
        for key in ("definition", "policy", "lastReportRun"):
            if not isinstance(result.get(key), dict) and isinstance(previous.get(key), dict):
                result[key] = previous[key]
        # ``previewPayload``（试运行结果）**只在非模板态**沿用：
        # 模板态的试运行跑的是模拟业务数据，结果只留内存（ADR 0004），
        # 不能因为沿用库里那份**历史遗留**的模拟结果又把它写回持久化存储
        # ——「已有历史数据不删改」不等于「继续写」。
        definition = result.get("definition") or previous.get("definition")
        if not _definition_is_template_mode(definition):
            if not isinstance(result.get("previewPayload"), dict) and isinstance(
                previous.get("previewPayload"), dict
            ):
                result["previewPayload"] = previous["previewPayload"]
        else:
            result.pop("previewPayload", None)
        # 设计阶段与方案是**原样带过**，不是「缺了才补」：
        # 本轮响应没带设计状态时，库里那份就是权威。
        for key in ("designPhase", "reportPlan", "templateDesign"):
            if key not in result and key in previous:
                result[key] = previous[key]
        # 示意上下文（#51）同样原样带过：冻结下来的时间窗不能因为一轮
        # 没带载荷的交互就丢掉 —— 丢了就会按新的锚点重新生成，数字会跳。
        if "previewContext" not in result and isinstance(
            previous.get("previewContext"), dict
        ):
            result["previewContext"] = previous["previewContext"]
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

    # ---------- 同事务写入（设计流） ----------

    def write_turn_inline(
        self,
        connection: Any,
        *,
        session_id: str,
        user_text: str,
        response: AgentResponse,
        client_msg_id: str | None = None,
        request: AgentRequest | None = None,
    ) -> None:
        """在**给定连接**上写这一轮，**不自行 commit**。

        设计流（#19）需要「方案 + 阶段 + 操作结果 + 本轮消息」落进
        **同一笔事务**：要么一起成功，要么一起回滚。

        为什么不能直接用 ``record_turn``：它以及它下面的
        ``upsert_session`` / ``append_message`` 都会自己 ``commit()``。
        一旦被调用，事务就提前结束了，同事务提交不再成立 ——
        就会出现「方案写进去了、消息没写进去」这种半截状态。

        所以这里直接用传入的连接写 SQL，不碰任何会 commit 的旧方法。
        """
        import sqlite3

        assert isinstance(connection, sqlite3.Connection)
        now = datetime.now().isoformat(timespec="seconds")
        is_advance = user_text.strip() == ADVANCE_MESSAGE

        title = self._derive_title(session_id, user_text, is_advance)
        state = self._state_to_store(session_id, response)
        current = self._repo.get_session(session_id)
        previous_snapshot = (current.snapshot if current else None) or {}
        snapshot = (
            self._build_snapshot(
                request,
                response,
                previous_snapshot=previous_snapshot,
            )
            or {}
        )
        snapshot = self._carry_over_business_objects(snapshot, previous_snapshot)
        snapshot = {**(snapshot or {}), **self._read_edit_state(session_id)}

        existing = connection.execute(
            "SELECT id FROM sessions WHERE id = ? AND deleted_at IS NULL",
            (session_id,),
        ).fetchone()
        if existing is None:
            # 已软删的行不能被 UPDATE 复活：先物理清掉再重新建行。
            connection.execute(
                "DELETE FROM sessions WHERE id = ? AND deleted_at IS NOT NULL",
                (session_id,),
            )
            connection.execute(
                """
                INSERT INTO sessions (id, workspace_id, title, state, design_session_id,
                                      report_definition_id, preview_run_id, message_count,
                                      snapshot, pending_choice, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    session_id,
                    None,
                    title or "新对话",
                    state,
                    response.designSessionId,
                    self._extract_definition_id(response),
                    self._extract_preview_run_id(response),
                    0,
                    _dump_snapshot(snapshot),
                    now,
                    now,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE sessions
                   SET state = ?, title = COALESCE(?, title),
                       design_session_id = COALESCE(?, design_session_id),
                       report_definition_id = COALESCE(?, report_definition_id),
                       preview_run_id = COALESCE(?, preview_run_id),
                       snapshot = ?, updated_at = ?
                 WHERE id = ?
                """,
                (
                    state,
                    title,
                    response.designSessionId,
                    self._extract_definition_id(response),
                    self._extract_preview_run_id(response),
                    _dump_snapshot(snapshot),
                    now,
                    session_id,
                ),
            )

        # user 消息：内部控制消息不入库。
        if not is_advance:
            self._append_message_inline(
                connection,
                session_id=session_id,
                role="user",
                content=user_text,
                client_msg_id=client_msg_id,
            )
        # assistant 消息：payload 存整个响应，恢复时可直接回放渲染。
        # 模拟业务数值不进库（见 _persisted_response_payload）。
        self._append_message_inline(
            connection,
            session_id=session_id,
            role="assistant",
            content=response.reply,
            payload=_persisted_response_payload(response),
            client_msg_id=self._reply_msg_id(client_msg_id),
        )

        count = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        connection.execute(
            "UPDATE sessions SET message_count = ? WHERE id = ?", (count, session_id)
        )

    @staticmethod
    def _append_message_inline(
        connection: Any,
        *,
        session_id: str,
        role: str,
        content: str,
        payload: dict[str, Any] | None = None,
        client_msg_id: str | None = None,
    ) -> None:
        """写一条消息，不 commit。幂等口径与旧 ``append_message`` 一致。"""
        now = datetime.now().isoformat(timespec="seconds")
        if client_msg_id:
            existing = connection.execute(
                "SELECT id FROM messages WHERE session_id = ? AND client_msg_id = ?",
                (session_id, client_msg_id),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    UPDATE messages SET role = ?, content = ?, payload = ?, created_at = ?
                     WHERE id = ?
                    """,
                    (role, content, _dump_snapshot(payload), now, existing["id"]),
                )
                return
        next_seq = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO messages (session_id, seq, role, content, payload,
                                  client_msg_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                next_seq,
                role,
                content,
                _dump_snapshot(payload),
                client_msg_id,
                now,
            ),
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

    @staticmethod
    def _is_simulated_run(
        *,
        request: AgentRequest | None,
        response: AgentResponse,
        previous_snapshot: dict[str, Any] | None,
        payload: dict[str, Any] | None,
    ) -> bool:
        """这次试运行的结果是不是**模拟业务数据**。

        判据是**来源**，不是字段名（规格 #46：不得按字段名剔除
        ``previewPayload``，要按内容的来源判断）：

        1. **结果自己带来源标记**：载荷上带 ``dataSource == "simulated"``
           （造数器的标记）→ 模拟数据，不落库；
        2. **跑它的定义是模板态**：模板态一律用模拟业务数据渲染
           （ADR 0004），后台试运行也不例外 → 模拟数据，不落库。

        「跑它的定义」按**服务端实际用的那份**取，优先级与
        ``agent_service._handle_previewing`` 一致：

        1. ``request.snapshots.reportDefinition`` —— 试运行就是拿它跑的；
        2. ``response.domainObjects.reportDefinitionDraft`` —— 本轮响应带的草稿；
        3. **库里上一轮落下的定义**（``previous_snapshot["definition"]``）。

        第 3 条是模板态的**兜底**：试运行那一轮的响应里没有草稿、请求也
        未必带得回来，但「确认方案」那一轮已经把带
        ``metadata.templateBinding`` 的定义落进库了 —— 恢复不依赖内存里的
        runId，也不依赖客户端把草稿带回来。

        三处都取不到定义时按「不是模拟」处理：既有历史数据不迁移、不删改，
        非模板态的既有链路照旧。
        """
        if _payload_is_simulated(payload):
            return True
        definition: Any = None
        if request is not None and request.snapshots.reportDefinition is not None:
            definition = request.snapshots.reportDefinition
        elif response.domainObjects.reportDefinitionDraft is not None:
            definition = response.domainObjects.reportDefinitionDraft
        else:
            definition = (previous_snapshot or {}).get("definition")
        return _definition_is_template_mode(definition)

    def restore_trial_preview_payload(
        self,
        snapshot: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """恢复会话时把**内存里**的试运行结果挂回响应（**不落库**）。

        模板态的试运行结果不进持久化快照（ADR 0004：模拟业务数据只存在于
        响应与内存），但它是**响应级**的 —— 读会话时按内存里的运行结果挂回
        响应，前端拿到的结构与落库前完全一样（非模板态那条既有链路不受影响：
        快照里已经有 ``previewPayload`` 时原样保留）。

        - 快照里已有 ``previewPayload`` → 原样保留（非模板态的既有语义）；
        - 快照里没有、内存里还有这次运行的结果 → 挂回来；
        - 内存里也没有（后端重启过）→ 不挂：重启后按定义/上下文重新生成的是
          **示意载荷**（``reportPreviewPayload``），不是试运行产物。
        """
        if not isinstance(snapshot, dict):
            return snapshot
        if isinstance(snapshot.get("previewPayload"), dict):
            return snapshot
        last_run = snapshot.get("lastReportRun")
        if not isinstance(last_run, dict):
            return snapshot
        run_id = last_run.get("reportRunId")
        if not run_id:
            return snapshot
        payload = preview_service.get_result_payload(str(run_id))
        if payload is None:
            return snapshot
        result = dict(snapshot)
        result["previewPayload"] = payload
        return result

    def _build_snapshot(
        self,
        request: AgentRequest | None,
        response: AgentResponse,
        *,
        previous_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """组装恢复现场需要的快照。

        取值优先级：本次响应的 domainObjects > 请求带回来的 snapshots。

        **按允许列表写入**（ADR 0004 的存储边界）：

        - 写：``definition`` / ``policy`` / ``reportPlan`` /
          ``templateDesign`` / ``designPhase`` / ``previewContext``，
          以及**非模拟**时的 ``previewPayload``（**只装试运行结果**，语义不变）；
        - 不写：``reportPreviewPayload`` —— 模拟业务结果只在响应与内存里。

        注意这里**不是**「按字段名把某些键剔掉」，而是**只写允许的键**：
        ``previewPayload`` 同时承载试运行结果，按字段名剔除会误伤那条链路。

        ``previewPayload`` 的去留按**来源**判断（见 ``_is_simulated_run``）：
        这次试运行的结果是模拟业务数据 → 不写；不是 → 按原语义写。
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
            # 所以非模板态的既有链路在这里缓存一份，恢复会话时直接用。
            payload = preview_service.get_result_payload(last_run.reportRunId)
            # 但**模拟业务结果不落库**（ADR 0004 / 规格 #46「模拟业务数据只
            # 存在于响应与内存」）：模板态的试运行跑的是模拟数据，结果只留在
            # 内存与响应里；恢复会话时按内存里的运行结果挂回响应
            # （见 ``restore_trial_preview_payload``），不靠落库。
            if payload is not None and not self._is_simulated_run(
                request=request,
                response=response,
                previous_snapshot=previous_snapshot,
                payload=payload,
            ):
                snapshot["previewPayload"] = payload

        # 示意上下文（#51）：**上下文**，不是业务数值 —— 落库是为了让
        # 同一定义版本跨天、刷新、重启都复用同一份时间窗，数字不跳。
        preview_context = _response_preview_context(response)
        if preview_context is not None:
            snapshot["previewContext"] = preview_context

        editing_target = (response.debug or {}).get("editingTargetRegionId")
        if editing_target is not None:
            snapshot["editingTargetRegionId"] = editing_target

        # 设计阶段与文字方案（#19）。恢复不能只依赖聊天正文、debug
        # 或浏览器本地变量：方案与阶段必须跟着会话一起存下来。
        if response.designPhase is not None:
            snapshot["designPhase"] = response.designPhase
            plan = response.domainObjects.reportPlan
            if plan is not None:
                snapshot["reportPlan"] = plan
            # 模板设计（工单 02）同理：刷新后要能拿回同一份模板，
            # 而不是让用户重新确认一次。
            template = response.domainObjects.templateDesign
            if template is not None:
                snapshot["templateDesign"] = template

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
