"""接口层验收夹具（工单 08 / GitHub #54，缝 1：接口层主缝）。

规格 #46「Testing Decisions → 缝 1：接口层（主缝，最高）」的硬要求是：

    入口：真实的聊天公开入口；真实编排、真实服务、真实 SQLite（独立临时库）；
    **只替换外部模型调用**这一处。

本模块把这条口径落成一套可复用夹具，缝 1 的每条用例都从它出发：

- **独立临时库**：每条用例一份 ``tempfile.mkdtemp`` 下的独立 SQLite 文件，
  用完即删（``shutil.rmtree``）；不指向仓库里的任何真实 db。
- **真实编排**：``POST /api/agent/chat`` 打到真实应用，链路是
  agent 编排 → 方案服务 → 转换层 → 造数器 → 响应组装 → 会话保存 / 读取。
- **只替换外部模型调用**：唯一被替换的是 ``report_design_service`` 的
  ``model_call``（``app/services/report_design_service.py:371`` 的
  ``model_call`` 属性，经 ``reset_for_tests(model_call=...)`` 注入，
  最终在 ``app/services/report_design_service.py:1257`` 传给
  ``report_plan_agent.generate_candidate``）—— 那是冻结契约里唯一的
  外部边界。业务 service、转换层、造数器、响应组装一律**不替换**。
- **会话恢复**：``GET /api/sessions/{sessionId}`` 走真实读回路径。

用法::

    with acceptance_harness(model=ScriptedModel(plan_candidate())) as harness:
        plan = harness.request_plan("生成 A 机房最近 7 天能耗报表")
        confirmed = harness.confirm_plan(plan)
        detail = harness.read_session()
        assert "reportPreviewPayload" in confirmed
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from fastapi.testclient import TestClient

from app.main import app
from app.schemas.agent import AgentResponse
from app.services.agent_service import agent_service
from app.services.context_engine import context_engine
from app.services.report_design_service import report_design_service
from app.services.session_service import session_service
from app.storage.sqlite_store import SqliteSessionRepository


# --------------------------------------------------------------------------
# 契约常量
# --------------------------------------------------------------------------

#: 票05（GitHub #51）接入后，``AgentResponse`` 才有的字段名。
#: 规格 #46 的跨工单契约写死了这个字段名，这里只做**存在性探测**，
#: 不用它来断言具体数值（数值由造数器决定，规格也说不该断言具体值）。
REPORT_PREVIEW_PAYLOAD_FIELD = "reportPreviewPayload"

#: 规格 #46「模拟结果与试运行结果的存储隔离」给出的**会话持久化快照**
#: 允许列表。落库侧只写这些键；``reportPreviewPayload`` 不在其中。
#:
#: ``lastReportRun`` 是会话存储自己写的**试运行结果**引用
#: （``app/services/session_service.py`` 的 ``_build_snapshot``），
#: 与模拟业务结果无关，列进来是为了让「快照里没有没登记的键」这条
#: 断言成立。
SNAPSHOT_ALLOWED_KEYS: tuple[str, ...] = (
    "definition",
    "policy",
    "reportPlan",
    "templateDesign",
    "designPhase",
    "previewContext",
    "previewPayload",
    "lastReportRun",
)

#: 允许列表里**不依赖票05**就该存在的键。
#: ``previewContext`` 由票05 引入，票05 没合入的基线上不存在，
#: 所以按契约存在与否分别断言（见 ``snapshot_required_keys()``）。
SNAPSHOT_ALLOWED_KEYS_BEFORE_TICKET_05: tuple[str, ...] = (
    "definition",
    "policy",
    "reportPlan",
    "templateDesign",
    "designPhase",
)


def snapshot_required_keys() -> tuple[str, ...]:
    """当前基线上、草稿阶段**应当**存在的允许列表键。

    - ``previewContext``：票05（GitHub #51）引入；票05 的基线上必须有，
      票05 之前的基线上没有 —— 按契约存在与否分别断言，不写死错值。
    - ``previewPayload``：**允许**但不是草稿阶段必需 —— 它装的是**试运行
      结果**，要走过「同意 → 校验 → 试运行」才出现。
    """
    if supports_report_preview_payload():
        return SNAPSHOT_ALLOWED_KEYS_BEFORE_TICKET_05 + ("previewContext",)
    return SNAPSHOT_ALLOWED_KEYS_BEFORE_TICKET_05


#: 定义层里**不该出现**的业务数值字段（ADR 0004 §2.2）。
#:
#: 只在**业务结果位置**（区域的 ``data``）上检查：``page.margins.*.value``
#: 这类版面配置数字是允许的 —— 规格写明「『定义不含数值』指不含模拟业务
#: 结果；不禁止结构配置里的数字（列宽、版本号、时间粒度等）」。
BUSINESS_VALUE_KEYS: tuple[str, ...] = (
    "value",
    "values",
    "yValues",
    "xLabels",
    "rows",
    "points",
    "yoyPercent",
    "comparisonAvailable",
)

#: 临时库里允许出现的表：会话存储 + 设计存储。
#: 规格说「不建业务数值表」—— 这里用白名单把「多了业务数值表」变成可断言的事实。
KNOWN_TABLES: frozenset[str] = frozenset(
    {
        "workspaces",
        "sessions",
        "messages",
        "sqlite_sequence",
        "sqlite_master",
        "report_design_sessions",
        "report_design_operations",
        "report_design_templates",
        "report_design_confirmations",
        "report_design_definitions",
    }
)

#: SQLite 授权回调里「会碰到表」的动作。
_SQLITE_TABLE_ACTIONS: frozenset[int] = frozenset(
    action
    for action in (
        getattr(sqlite3, "SQLITE_READ", None),
        getattr(sqlite3, "SQLITE_INSERT", None),
        getattr(sqlite3, "SQLITE_UPDATE", None),
        getattr(sqlite3, "SQLITE_DELETE", None),
        getattr(sqlite3, "SQLITE_CREATE_TABLE", None),
        getattr(sqlite3, "SQLITE_DROP_TABLE", None),
        getattr(sqlite3, "SQLITE_ALTER_TABLE", None),
    )
    if action is not None
)


def supports_report_preview_payload() -> bool:
    """票05（GitHub #51）是否已经接入 ``reportPreviewPayload``。

    探测方式看**响应契约本身**（``AgentResponse`` 的字段），
    不看某个具体响应里有没有值 —— 后者会把「这一轮恰好没出数」
    误判成「功能没接入」。
    """
    return REPORT_PREVIEW_PAYLOAD_FIELD in AgentResponse.model_fields


def report_preview_payload_pending_reason() -> str:
    """票05 未接入时的统一说明（用例 skip 时用它做 reason）。"""
    return (
        "票05（GitHub #51）尚未接入：AgentResponse 还没有 "
        f"{REPORT_PREVIEW_PAYLOAD_FIELD} 字段，方案确认轮带不回报表示意载荷，"
        "本用例先跳过。票05 合入后标记自动失效，断言立即生效。"
    )


# --------------------------------------------------------------------------
# 外部模型调用替身（唯一的替换点）
# --------------------------------------------------------------------------


def plan_candidate(
    *,
    topic: str = "A 机房能耗日报",
    metrics: Sequence[str] | None = None,
    subject: str | None = "A 机房",
    time_range: str | None = "最近 7 天",
    exclusions: Sequence[str] | None = None,
    sections: Sequence[dict[str, Any]] | None = None,
    assumptions: Sequence[str] | None = None,
    suggestions: Sequence[str] | None = None,
) -> dict[str, Any]:
    """造一份**合法的方案候选**（模型输出的形状）。

    这里只提供「模型会说的内容」：板块标题、指标意图、组件类型候选。
    组件类型与表格栏目仍由服务端定稿（规格：模型说了不算）。
    """
    return {
        "kind": "plan_candidate",
        "candidate": {
            "topic": topic,
            "requirements": {
                "metrics": list(metrics) if metrics is not None else ["总耗电"],
                "subject": subject,
                "timeRange": time_range,
                "exclusions": list(exclusions) if exclusions is not None else [],
            },
            "sections": list(sections)
            if sections is not None
            else [
                {
                    "id": "section_total",
                    "title": "总耗电概览",
                    "metricIntents": ["总耗电"],
                    "componentType": "metric_card",
                }
            ],
            "assumptions": list(assumptions) if assumptions is not None else [],
            "suggestions": list(suggestions) if suggestions is not None else [],
            "referenceTemplates": [],
        },
    }


class ScriptedModel:
    """外部模型调用替身：按脚本返回结果，并记录收到的消息。

    它替换的是**模型请求那一层**，不替服务端做领域校验、
    不替服务端分配版本、不落库 —— 与冻结契约一致。
    """

    def __init__(
        self,
        *results: dict[str, Any],
        responder: Any | None = None,
    ) -> None:
        self.results = list(results)
        self.responder = responder
        self.calls: list[list[dict[str, Any]]] = []

    async def __call__(self, messages: list[dict[str, Any]]) -> str:
        self.calls.append(messages)
        if self.responder is not None:
            return json.dumps(self.responder(messages), ensure_ascii=False)
        if not self.results:
            return json.dumps(plan_candidate(), ensure_ascii=False)
        result = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        return json.dumps(result, ensure_ascii=False)


# --------------------------------------------------------------------------
# 夹具本体
# --------------------------------------------------------------------------


@dataclass
class AcceptanceHarness:
    """一条用例的验收现场。"""

    client: TestClient
    db_path: Path
    tmp_dir: Path
    session_id: str
    conversation_id: str
    model: ScriptedModel
    repository: SqliteSessionRepository
    sql_statements: list[str] = field(default_factory=list)
    db_accesses: list[tuple[int, str | None, str | None]] = field(default_factory=list)
    agent_invocations: list[str] = field(default_factory=list)
    _trace_on: bool = field(default=True, repr=False)
    _message_seq: int = field(default=0, repr=False)

    # ---------- 发一条用户消息 ----------

    def next_message_id(self) -> str:
        self._message_seq += 1
        return f"msg_{self._message_seq}"

    def build_body(
        self,
        message: str,
        *,
        state: str = "ANALYZING",
        message_id: str | None = None,
        context: dict[str, Any] | None = None,
        snapshots: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """按公开协议拼请求体（前端发什么，这里就发什么）。"""
        body: dict[str, Any] = {
            "agentProtocolVersion": "1.0",
            "conversationId": self.conversation_id,
            "sessionId": self.session_id,
            "messageId": message_id or self.next_message_id(),
            "state": state,
            "message": message,
            "context": {
                "projectId": "project_001",
                "subjectRef": "machine_room",
                "subjectId": "room_a",
                **(context or {}),
            },
        }
        if snapshots is not None:
            body["snapshots"] = snapshots
        return body

    def send(
        self,
        message: str,
        *,
        state: str = "ANALYZING",
        message_id: str | None = None,
        context: dict[str, Any] | None = None,
        snapshots: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发一条用户消息，拿到响应 JSON。"""
        response = self.client.post(
            "/api/agent/chat",
            json=self.build_body(
                message,
                state=state,
                message_id=message_id,
                context=context,
                snapshots=snapshots,
            ),
        )
        assert response.status_code == 200, response.text
        return response.json()

    def request_plan(
        self, message: str, **kwargs: Any
    ) -> dict[str, Any]:
        """发一条需求，拿到**文字方案**（``domainObjects.reportPlan``）。"""
        payload = self.send(message, **kwargs)
        plan = (payload.get("domainObjects") or {}).get("reportPlan")
        assert plan is not None, payload
        return plan

    def confirm_plan(
        self, plan: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        """点「确认方案」：走公开的 ``designAction`` 入口。"""
        context = {
            "designAction": "CONFIRM_PLAN",
            "planId": plan["id"],
            "basePlanRevision": plan["revision"],
            **(kwargs.pop("context", None) or {}),
        }
        return self.send(kwargs.pop("message", "确认"), context=context, **kwargs)

    def agree_draft(self, **kwargs: Any) -> dict[str, Any]:
        """草稿阶段点「同意」。"""
        return self.send(kwargs.pop("message", "同意"), **kwargs)

    def advance(self, state: str, **kwargs: Any) -> dict[str, Any]:
        """内部控制消息 ``__advance__``：跳过路由，按状态推进。"""
        kwargs.setdefault("state", state)
        return self.send("__advance__", **kwargs)

    # ---------- 读回会话 ----------

    def read_session(self) -> dict[str, Any]:
        """走真实读回入口取会话详情（刷新 / 重启恢复走的就是它）。"""
        response = self.client.get(f"/api/sessions/{self.session_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def read_session_snapshot(self) -> dict[str, Any]:
        """读回会话里的**快照**。"""
        return self.read_session().get("snapshot") or {}

    def persisted_snapshot(self) -> dict[str, Any]:
        """库里那一份快照（**接口返回前**的原始落库内容）。

        读回接口会临时挂上重新生成的示意载荷；这里要看的是
        「真的落进库里的东西」，所以直接读仓库。
        """
        record = self.repository.get_session(self.session_id)
        return dict(record.snapshot or {}) if record else {}

    def session_record(self) -> Any:
        return self.repository.get_session(self.session_id)

    def assistant_messages(self) -> list[Any]:
        """本会话的 assistant 消息（含落库后的响应体）。"""
        return [
            message
            for message in self.repository.list_messages(self.session_id)
            if message.role == "assistant"
        ]

    # ---------- 库 ----------

    def table_names(self) -> list[str]:
        """临时库里现有的全部表（含 SQLite 内部表）。"""
        with self._paused_trace():
            rows = self.repository.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        return [row["name"] for row in rows]

    def table_columns(self) -> dict[str, list[str]]:
        """每张表的列名。"""
        with self._paused_trace():
            result: dict[str, list[str]] = {}
            for name in self.table_names():
                rows = self.repository.conn.execute(
                    f'PRAGMA table_info("{name}")'
                ).fetchall()
                result[name] = [row["name"] for row in rows]
        return result

    def dump_all_tables(self) -> dict[str, list[dict[str, Any]]]:
        """把**所有表**的所有行读出来（存储隔离用例要逐格检查）。"""
        with self._paused_trace():
            result: dict[str, list[dict[str, Any]]] = {}
            for name in self.table_names():
                rows = self.repository.conn.execute(
                    f'SELECT * FROM "{name}"'
                ).fetchall()
                result[name] = [dict(row) for row in rows]
        return result

    def referenced_tables(self) -> set[str]:
        """请求链路里被访问过的表。

        来源是 SQLite 的**授权回调**（结构化事实，不是解析 SQL 文本）：
        每一次读表 / 写表 / 建表都会回调一次，能精确回答
        「这一轮到底碰了哪些表」。
        """
        tables: set[str] = set()
        for _action, table, _column in self.db_accesses:
            if table:
                tables.add(table)
        return tables

    def accessed_columns(self) -> set[tuple[str, str]]:
        """请求链路里被访问过的「表.列」。"""
        return {
            (table, column)
            for _action, table, column in self.db_accesses
            if table and column
        }

    def reset_sql_trace(self) -> None:
        self.sql_statements.clear()
        self.db_accesses.clear()

    @contextmanager
    def spy_on_simulated_data_generator(self) -> Iterator["GeneratorSpy"]:
        """给「造数器 + 载荷组装层」套一层只记录、不改写的壳。

        用途：证明响应里的数值**只可能**来自确定性造数器 —— 响应里的
        ``reportPreviewPayload`` 与组装层算出来的那一份完全一致，
        而组装层的输入又来自造数器，就不可能来自别处。

        每一处都要打：模块里的原函数，以及调用方模块里**按名字导入**的
        那份引用（票05 的 ``preview_service`` / ``report_design_service``
        用的就是后者）。
        """
        from app.services import preview_service, report_design_service
        from app.services import simulated_data_service

        spy = GeneratorSpy()
        patched: list[tuple[Any, str, Any]] = []

        def patch(module: Any, name: str, replacement: Any) -> None:
            if not hasattr(module, name):
                return
            patched.append((module, name, getattr(module, name)))
            setattr(module, name, replacement)

        original_generator = simulated_data_service.generate_simulated_data

        def generator_wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            result = original_generator(*args, **kwargs)
            spy.generator_calls.append(result)
            return result

        patch(simulated_data_service, "generate_simulated_data", generator_wrapper)
        patch(preview_service, "generate_simulated_data", generator_wrapper)

        original_builder = getattr(preview_service, "build_report_preview_payload", None)
        if original_builder is not None:

            def builder_wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
                result = original_builder(*args, **kwargs)
                spy.payload_calls.append(result)
                return result

            patch(preview_service, "build_report_preview_payload", builder_wrapper)
            patch(
                report_design_service, "build_report_preview_payload", builder_wrapper
            )

        try:
            yield spy
        finally:
            for module, name, previous in patched:
                setattr(module, name, previous)

    @contextmanager
    def _paused_trace(self) -> Iterator[None]:
        """夹具自己做的库内省不该混进「请求链路执行过的 SQL」。"""
        previous = self._trace_on
        self._trace_on = False
        try:
            yield
        finally:
            self._trace_on = previous


@dataclass
class GeneratorSpy:
    """造数器与载荷组装层的调用记录（只记录，不改写行为）。"""

    #: 造数器每次返回的 ``{regionId: payload}``。
    generator_calls: list[dict[str, Any]] = field(default_factory=list)
    #: 组装层每次返回的 ``{"previewContext": ..., "regions": [...]}``。
    payload_calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def regions(self) -> list[dict[str, Any]]:
        """最后一次组装出来的区域载荷。"""
        if not self.payload_calls:
            return []
        return list(self.payload_calls[-1].get("regions") or [])


@contextmanager
def acceptance_harness(
    *,
    model: ScriptedModel | None = None,
    session_id: str = "sess_acceptance_1",
    conversation_id: str = "conv_acceptance",
    tmp_root: str | Path | None = None,
) -> Iterator[AcceptanceHarness]:
    """建立一条用例的验收现场，退出时**用完即删**。

    参数：
        model: 外部模型调用替身（默认返回一份合法方案候选）。
        session_id: 会话 id（一条用例一份，互不干扰）。
        tmp_root: 临时库的父目录（默认用系统临时目录）。
    """
    tmp_dir = Path(
        tempfile.mkdtemp(prefix="acceptance-", dir=str(tmp_root) if tmp_root else None)
    )
    db_path = tmp_dir / "sessions.db"
    repository = SqliteSessionRepository(db_path)

    original_repo = session_service._repo
    original_client = context_engine._client
    original_agent = agent_service._agent
    original_model_call = report_design_service._model_call
    original_agent_attrs: dict[str, Any] = {}

    harness = AcceptanceHarness(
        client=TestClient(app),
        db_path=db_path,
        tmp_dir=tmp_dir,
        session_id=session_id,
        conversation_id=conversation_id,
        model=model or ScriptedModel(plan_candidate()),
        repository=repository,
    )

    try:
        # ① 独立临时库：会话存储与设计存储都落到这一个文件。
        session_service._repo = repository
        # 设计存储跟随会话存储（同一条连接、同一把锁）。
        report_design_service.reset_for_tests()
        # ② 只替换外部模型调用这一处。
        report_design_service.reset_for_tests(model_call=harness.model)
        # 路由层同样不发外部模型请求（没有模型客户端 → 走规则兜底）。
        context_engine._client = None
        # ③ 记录业务 Agent 有没有被调用：
        #    方案流程若真的走了编排，模型替身才是唯一的外部调用。
        for attr in ("handle", "ahandle"):
            bound = getattr(original_agent, attr, None)
            if bound is None:
                continue
            original_agent_attrs[attr] = original_agent.__dict__.get(attr, ...)
            setattr(original_agent, attr, _make_agent_spy(harness, attr, bound))

        # ④ 打开观测：SQL 语句轨迹 + SQLite 授权回调（谁碰了哪张表）。
        connection = repository.conn
        connection.set_trace_callback(
            lambda statement: harness.sql_statements.append(statement)
            if harness._trace_on
            else None
        )
        connection.set_authorizer(_make_db_authorizer(harness))
        yield harness
    finally:
        for attr, previous in original_agent_attrs.items():
            if previous is ...:
                try:
                    delattr(original_agent, attr)
                except AttributeError:  # pragma: no cover - 防御
                    pass
            else:
                setattr(original_agent, attr, previous)
        report_design_service._model_call = original_model_call
        report_design_service.reset_for_tests()
        context_engine._client = original_client
        session_service._repo = original_repo
        try:
            repository.close()
        except Exception:  # noqa: BLE001 - 清理不能影响用例结果
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _make_db_authorizer(harness: AcceptanceHarness) -> Any:
    """SQLite 授权回调：只记录，不拦截。

    回调返回 ``SQLITE_OK`` → 行为与不装回调时完全一样。
    它给的是**结构化事实**：这一轮读写了哪张表的哪一列。
    """

    def authorizer(
        action: int,
        arg1: str | None,
        arg2: str | None,
        db_name: str | None,
        trigger_name: str | None,
    ) -> int:
        if action in _SQLITE_TABLE_ACTIONS:
            table = arg2 if action == sqlite3.SQLITE_ALTER_TABLE else arg1
            if harness._trace_on:
                harness.db_accesses.append((action, table, arg2))
        return sqlite3.SQLITE_OK

    return authorizer


def _make_agent_spy(harness: AcceptanceHarness, name: str, bound: Any) -> Any:
    """给业务 Agent 的入口套一层只记录不改写的壳。"""

    def spy(*args: Any, **kwargs: Any) -> Any:
        harness.agent_invocations.append(name)
        return bound(*args, **kwargs)

    return spy
